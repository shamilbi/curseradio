import curses
from curseradio.curseradio import OPMLBrowser

def main():
    curses.wrapper(OPMLBrowser)

if __name__ == '__main__':
    main()
