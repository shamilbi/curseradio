#!/usr/bin/env python3

"""
Curses application for navigating and playing radio streams (using the
tunein directory at http://opml.radiotime.com/).

Uses `mpv` to play streams. Works for any stream which works when invoked
as `mpv <stream-location>`.

A favourites file (also stored as OPML) is written to
$XDG_DATA_HOME/curseradio/favourites.opml

Controls:
 * UP, DOWN, PAGEUP, PAGEDOWN, HOME, END - navigate the source list
 * ENTER - expand/contract items, play streams
 * q - exit
 * k - kill the current stream
 * f - mark as favourite
"""

import pathlib
import configparser
import subprocess
from abc import ABC, abstractmethod
import curses
import lxml.etree
import requests
import xdg.BaseDirectory


CONFIG_DEFAULT = {
    'opml': {'root': "https://opml.radiotime.com/"},
    'playback': {'command': '/usr/bin/mpv'},
    'interface': {'keymap': 'default'},
    'keymap.default': {
        'up': 'KEY_UP',
        'down': 'KEY_DOWN',
        'start': 'KEY_HOME',
        'end': 'KEY_END',
        'pageup': 'KEY_PPAGE',
        'pagedown': 'KEY_NPAGE',
        'enter': 'KEY_ENTER',
        'stop': 'k',
        'exit': 'q',
        'favourite': 'f',
        'left': 'KEY_LEFT',
        'right': 'KEY_RIGHT',
    }
}


class OPMLNode(ABC):
    """
    Represents an OPML <outline> element. Only instantiate subclasses.
    """
    @classmethod
    def from_xml(cls, url, text="", attr=None):
        """
        Load an OPML XML file. Returns a fake parent OPMLOutline node with
        children set to all the outlines contained in the file. (The header
        is currently discarded).
        """
        if attr is None:
            attr = {}
        if isinstance(url, pathlib.Path):
            tree = lxml.etree.parse(url)
        else:
            r = requests.get(url)
            tree = lxml.etree.XML(r.content)
        result = cls(text=text, attr=attr)
        result.children = [OPMLNode.from_element(o)
                           for o in tree.xpath('/opml/body/outline')]
        return result

    @classmethod
    def from_element(cls, element):
        """
        Converts a single <outline> node into the appropriate OPMLOutline
        subclass depending on attributes. Currently detects only plain
        outlines (simple folders), links (deferred folders) and audio leaf
        elements.

        TODO: Support other leaf element types.
        """
        text = element.get('text')
        attr = dict(element.attrib)
        if 'URL' in attr and (url := attr['URL']).startswith('http://'):
            attr['URL'] = f'{url[:4]}s{url[4:]}'    # https://
        type_ = attr.get('type', None)
        if type_ is None and len(element) > 0 or type_ == 'text':
            # text: No stations or shows available
            type_ = "outline"

        if type_ == "outline":
            node = OPMLOutline(text=text, attr=attr)
            for child in element.xpath('./outline'):
                node.children.append(cls.from_element(child))
        elif type_ == "link":
            node = OPMLOutlineLink(text=text, attr=attr)
            assert len(element) == 0
        elif type_ == "audio":
            node = OPMLAudio(text=text, attr=attr)
            assert len(element) == 0
        return node

    def __init__(self, text, attr):
        self.text = text
        self.attr = attr
        self.children = []

    @abstractmethod
    def render(self):
        """
        Return a 4-tuple of text to display (text truncated if too long)
         * main title (~50% width)
         * subtext (~40% width)
         * data0 (4 chars)
         * data1 (5 chars)
        """

    @abstractmethod
    def activate(self):
        """
        Action when the item is selected and enter pressed. Yield either
        strings (progress messages) or a list (command list for popen).
        """

    def flatten(self, result, depth=0):
        """
        Visitor method to return a flattened ordered list of (obj, depth)
        tuples in menu order, respecting current collapse settings.
        """
        result.append((self, depth))
        return result

    def to_element(self):
        """
        Return the object and its children as an <outline> element.
        """
        return lxml.etree.Element("outline", attrib=self.attr)

    def to_xml(self):
        """
        Return the element wrapped in an <opml> toplevel element.
        """
        opml = lxml.etree.Element("opml")
        body = lxml.etree.SubElement(opml, "body")
        body.append(self.to_element())
        return opml


class OPMLAudio(OPMLNode):
    """
    Audio leaf node (<outline> with type=audio). URL attribute is expected to
    return a list of playlist URLs when accessed which can be passed to a
    player command. `bitrate`, `reliability`, `current_track` and `subtext`
    attributes are considered.
    """
    def __init__(self, text, attr):
        super().__init__(text, attr)
        self.url = attr['URL']
        self.bitrate = int(attr.get('bitrate', 0))
        self.reliability = int(attr.get('reliability', 0))
        self.formats = attr.get('formats', '')
        self.leaf = True
        if 'current_track' in attr:
            self.secondary = attr['current_track']
        elif 'subtext' in attr:
            self.secondary = attr['subtext']
        else:
            self.secondary = ""

    def activate(self):
        yield "Fetching playlist"
        r = requests.get(self.url)
        playlist = r.text.split("\n")[0]
        yield [playlist]

    def render(self):
        return (self.text, self.secondary,
                f"{self.bitrate}k", '|'*(self.reliability//20))


class OPMLOutline(OPMLNode):
    """
    Simple branch-level element, filled from the host file at creation time.
    """
    def __init__(self, text, attr):
        super().__init__(text, attr)
        self.children = []
        self.collapsed = True
        self.leaf = False

    def activate(self):
        self.collapsed = not self.collapsed
        yield from ()

    def flatten(self, result, depth=0):
        result.append((self, depth))
        if not self.collapsed:
            for c in self.children:
                c.flatten(result, depth+1)
        return result

    def render(self):
        collapsed = "+" if self.collapsed else "-"
        return (f"{collapsed} {self.text}", "", "", "")

    def to_element(self):
        elem = super().to_element()
        for c in self.children:
            elem.append(c.to_element())
        return elem


class OPMLOutlineLink(OPMLOutline):
    """
    Branch level node with type=link. Upon activation, the URL is fetched,
    parsed as OPML and all top-level outlines added as children of this node.
    """
    def __init__(self, text, attr):
        super().__init__(text, attr)
        self.url = attr['URL']
        self.ready = False

    def activate(self):
        if not self.ready:
            yield f"Loading {self.url}"
            fakeroot = OPMLOutline.from_xml(self.url)
            self.children = fakeroot.children
            self.ready = True
            yield "Loading... done"
        self.collapsed = not self.collapsed

class OPMLFavourites(OPMLOutline):
    """
    A special outline subclass representing a locally stored favourites list
    which tracks whether it has been altered so it can be saved if necessary.
    """
    def __init__(self, text, attr):
        super().__init__("Favourites", {})
        self.dirty = False

    def toggle(self, other):
        self.dirty = True
        if other in self.children:
            self.children.remove(other)
        else:
            self.children.append(other)

    def to_xml(self):
        """
        For the favourites object, we skip generating an extra <outline>
        (corresponding to this object), and just place the children (ie,
        the actual favourite items) as the toplevel.
        """
        opml = lxml.etree.Element("opml")
        body = lxml.etree.SubElement(opml, "body")
        for c in self.children:
            body.append(c.to_element())
        return opml

class OPMLBrowser:
    """
    Curses browser for an OPML tree. Includes simple keyboard navigation
    and launching child commands based on OPML leaf nodes.
    """
    def __init__(self, screen):
        """
        This is intended to be invoked using curses.wrapper. The first
        argument is the curses window and the second the OPML root URL.
        """
        self.config = self.load_config()
        self.keymap = self.get_keymap()
        self.root = OPMLOutline.from_xml(self.config['opml']['root'])
        self.root.collapsed = False
        self.favourites = self.load_favourites()
        self.root.children.insert(0, self.favourites)
        self.screen = screen
        self.selected = self.root
        self.cursor = 0
        self.top = 0
        self.flat = self.root.flatten([])
        self.maxy, self.maxx = self.screen.getmaxyx()
        self.child = None
        self.status = ""

        self.display()
        self.interact()

    def load_favourites(self):
        for path in xdg.BaseDirectory.load_data_paths("curseradio"):
            opmlpath = pathlib.Path(path, "favourites.opml")
            if opmlpath.exists():
                #return OPMLFavourites.from_xml(str(opmlpath))
                return OPMLFavourites.from_xml(opmlpath)
        return OPMLFavourites("", {})

    def save_favourites(self):
        path = xdg.BaseDirectory.save_data_path("curseradio")
        if self.favourites.dirty:
            opmlpath = pathlib.Path(path, "favourites.opml")
            opml = lxml.etree.ElementTree(self.favourites.to_xml())
            opml.write(str(opmlpath))

    def load_config(self):
        config = configparser.ConfigParser(strict=True)
        config.read_dict(CONFIG_DEFAULT)
        for path in xdg.BaseDirectory.load_config_paths("curseradio"):
            configpath = pathlib.Path(path, "curseradio.cfg")
            if configpath.exists():
                config.read(str(configpath))
        return config

    def get_keymap(self):
        keymap = {}
        chosen = self.config['interface']['keymap']
        section = f'keymap.{chosen}'
        if self.config.has_section(section):
            keysrc = self.config[section]
        else:
            keysrc = self.config['keymap.default']
        default = CONFIG_DEFAULT['keymap.default']
        current = self.config['keymap.default']
        for key in default:
            if key in current:
                default_value = current[key]
            else:
                default_value = default[key]
            value = keysrc.get(key, default_value)
            if value.startswith('KEY_'):
                keymap[key] = getattr(curses, value)
            else:
                keymap[key] = ord(value)
        return keymap

    def display(self, msg=None):
        """
        Redraw the screen, possibly showing a message on the bottom row.
        """
        self.screen.clear()

        width0 = 6*(self.maxx - 10)//10
        width1 = 4*(self.maxx - 10)//10

        showobjs = self.flat[self.top:self.top+self.maxy-1]
        for i, (obj, depth) in enumerate(showobjs):
            text = obj.render()
            style = curses.A_BOLD if i == self.cursor else curses.A_NORMAL
            self.screen.addstr(i, depth*2, text[0][:width0-depth*2], style)
            self.screen.addstr(i, width0+2, text[1][:width1-4])
            self.screen.addstr(i, width0+width1, text[2][:4])
            self.screen.addstr(i, width0+width1+5, text[3][:5])

        if msg is not None:
            self.screen.addstr(self.maxy-1, 0,
                               msg[:self.maxx-1], curses.A_BOLD)
        else:
            self.screen.addstr(self.maxy-1, 0,
                               self.status[:self.maxx-1], curses.A_BOLD)

        self.screen.refresh()
        curses.doupdate()

    def move(self, rel=None, to=None):
        """
        Recalculate screen scrolling after movement.
        """
        if to is not None:
            if to == "start":
                target = 0
            elif to == "end":
                target = len(self.flat) - 1
            elif to == "parent":
                target = self.top + self.cursor
                showobjs = self.flat[0:target]
                cdepth = self.flat[target][1]
                for _, (_, depth) in tuple(reversed(list(enumerate(showobjs)))):
                    if target >= 0:
                        target = target - 1
                        if depth == cdepth - 1:
                            break

        elif rel is not None:
            target = self.top + self.cursor + rel

        target = min(target, len(self.flat)-1)
        target = max(target, 0)
        self.selected = self.flat[target][0]

        if target < self.top:
            self.top = target
            self.cursor = 0
        elif target > self.top + self.maxy - 1:
            self.top = target - (self.maxy - 2)
            self.cursor = self.maxy - 2
        else:
            self.cursor = target - self.top

    def stop_child(self):
        if self.child is not None:
            self.child.terminate()
            self.child.wait()

    def enter(self):
        for msg in self.selected.activate():
            if isinstance(msg, str):
                self.display(msg=msg)
            elif isinstance(msg, list):  # command to run
                self.stop_child()

                command = [self.config['playback']['command']] + msg
                self.child = subprocess.Popen(command, stdout=subprocess.DEVNULL,
                                              stderr=subprocess.DEVNULL, stdin=subprocess.DEVNULL)
                self.status = f"Playing {self.selected.text}"

        self.flat = self.root.flatten([])
        self.move(rel=0)

    def interact(self):
        """
        Main loop. Listen for keyboard input and respond.
        """
        while True:
            ch = self.screen.getch()
            if ch == curses.KEY_RESIZE:
                self.maxy, self.maxx = self.screen.getmaxyx()
            elif ch == self.keymap['up']:
                self.move(rel=-1)
            elif ch == self.keymap['down']:
                self.move(rel=1)
            elif ch == self.keymap['start']:
                self.move(to="start")
            elif ch == self.keymap['end']:
                self.move(to="end")
            elif ch == self.keymap['pageup']:  # page up
                self.move(rel=-self.maxy)
            elif ch == self.keymap['pagedown']:  # page down
                self.move(rel=self.maxy)
            elif ch == self.keymap['left']:
                if isinstance(self.selected, (OPMLFavourites, OPMLOutline)):
                    if self.selected.collapsed:
                        self.move(to="parent")
                    else:
                        self.enter()
                else:
                    self.move(to="parent")
            elif ch == self.keymap['right']:
                if isinstance(self.selected, (OPMLFavourites, OPMLOutline)) \
                        and self.selected.collapsed:
                    self.enter()
            elif ch == self.keymap['enter'] or ch == ord('\n'):
                self.enter()
            elif ch == self.keymap['exit']:
                self.stop_child()
                self.save_favourites()
                return
            elif ch == self.keymap['stop']:
                self.stop_child()
            elif ch == self.keymap['favourite']:
                self.favourites.toggle(self.selected)
                self.flat = self.root.flatten([])
                self.move(rel=0)

            if self.child is not None:
                if self.child.poll() is not None:
                    self.child = None
                    self.status = ""

            self.display()
