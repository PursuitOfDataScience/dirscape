"""Keyboard selection over an already-rendered table.

The table `dirscape` prints is read and then acted on by retyping a path into
`dirscape why`, which is the friction this module removes: move a highlight
down the rows already on screen and press Enter.

Four constraints shaped it, and the first three rule out reaching for curses or
a TUI library:

* **No dependencies.** This runs on a login node in the first hour on an
  unfamiliar cluster. `termios` and `tty` are standard library; a full-screen
  library is not installable at the moment it is needed.
* **No second renderer.** Nothing here renders anything. It takes the finished
  lines and paints one of them in inverse video, so the interactive view cannot
  drift from the printed one.
* **It must degrade to the printout.** Redirected, piped, in a dumb terminal,
  or on a platform without `termios`, the answer is the static report and not
  an error. `supported()` is the single gate.
* **Python 3.6.** No `from __future__ import annotations`, no dataclasses.

The key semantics deliberately match `nodetop`'s, so a user who has both tools
does not have to learn two sets of arrows, including two lessons that package
learned from being used:

* **Left at the root does nothing.** It used to return, and returning at the
  root exits, so one stray press took the whole program down. Escape is not a
  movement, so at the root Escape is what leaves.
* **Right at a leaf does nothing.** It used to return the index, and a caller
  with nothing deeper to open read that as "step back", so Right at the detail
  view landed back on the list. Right means deeper everywhere, and at the
  bottom deeper is nowhere.
"""

import os
import shutil
import sys
from typing import Callable, List, Optional, Sequence

__all__ = ["Key", "supported", "read_key", "select", "raw_session", "MIN_LINES"]

#: A terminal shorter than this cannot hold a frame, so the static print is
#: better than a screen that repaints on top of itself.
MIN_LINES = 10

#: SGR reset and inverse. Named because `highlight` has to reason about where
#: they appear inside text it did not write.
RESET = "\033[0m"
INVERSE = "\033[7m"


class Key(object):
    """Decoded keypresses. Names rather than bytes, so callers read cleanly."""

    UP = "up"
    DOWN = "down"
    LEFT = "left"
    RIGHT = "right"
    ENTER = "enter"
    QUIT = "quit"
    BACK = "back"
    OTHER = "other"


def supported(stream=None):
    # type: (Optional[object]) -> bool
    """Can this session take keystrokes at all?

    Both streams matter, for different reasons. Output must be a terminal or
    there is nothing to paint a highlight on, and *input* must be a terminal
    or there is nobody to read from: a run with stdin redirected from a file
    would otherwise consume that file as keystrokes.

    `NO_COLOR` is deliberately not consulted. A highlight is structure rather
    than decoration, and inverse video is how it degrades.
    """
    out = stream if stream is not None else sys.stdout
    if os.environ.get("TERM", "") == "dumb":
        return False
    if os.environ.get("DIRSCAPE_NO_INTERACTIVE"):
        return False
    try:
        import termios  # noqa: F401
        import tty  # noqa: F401
    except Exception:  # pragma: no cover - non-POSIX
        return False
    try:
        if shutil.get_terminal_size().lines < MIN_LINES:
            return False
    except Exception:  # pragma: no cover
        return False
    return bool(
        getattr(out, "isatty", lambda: False)() and getattr(sys.stdin, "isatty", lambda: False)()
    )


class _RawMode(object):
    """Put the terminal in cbreak for the duration of a `with` block.

    Restored in a `finally` and on any exception, because a tool that leaves a
    login node's terminal without echo has done more damage than the report
    was worth.
    """

    def __init__(self):
        # type: () -> None
        self._saved = None  # type: Optional[object]

    def __enter__(self):
        # type: () -> "_RawMode"
        try:
            import termios
            import tty

            self._fd = sys.stdin.fileno()
            self._saved = termios.tcgetattr(self._fd)
            # cbreak rather than full raw: signals keep working, so Ctrl-C
            # still interrupts a browse the way a user expects.
            tty.setcbreak(self._fd)
        except Exception:
            self._saved = None
        # The cursor is hidden for the duration; a block cursor parked in the
        # middle of a table reads as a second, wrong highlight.
        _emit("\033[?25l")
        return self

    def __exit__(self, *exc):
        # type: (*object) -> bool
        _emit("\033[?25h")
        if self._saved is not None:
            try:
                import termios

                termios.tcsetattr(self._fd, termios.TCSADRAIN, self._saved)
            except Exception:
                pass
        return False


def raw_session():
    # type: () -> _RawMode
    return _RawMode()


def _emit(text):
    # type: (str) -> None
    try:
        sys.stdout.write(text)
        sys.stdout.flush()
    except Exception:
        pass


def read_key(readch=None):
    # type: (Optional[Callable[[], str]]) -> str
    """One decoded keypress.

    Arrows arrive as three bytes (`ESC [ A`), so a bare Escape is only Escape
    once no bracket follows it. A lone `ESC` read as the first byte of an arrow
    would swallow the next keypress, which is why the sequence is decoded here
    rather than by the caller.
    """
    if readch is None:

        def readch():
            # type: () -> str
            return sys.stdin.read(1)

    char = readch()
    if not char:
        return Key.QUIT
    if char in ("\r", "\n"):
        return Key.ENTER
    if char in ("q", "Q"):
        return Key.QUIT
    # Vim keys as well as arrows: this is a terminal tool and half its users
    # will reach for hjkl without thinking about it.
    if char in ("j",):
        return Key.DOWN
    if char in ("k",):
        return Key.UP
    if char in ("h",):
        return Key.LEFT
    if char in ("l",):
        return Key.RIGHT
    if char == "\x03":  # Ctrl-C, which cbreak leaves to us on some platforms
        raise KeyboardInterrupt
    if char != "\x1b":
        return Key.OTHER

    nxt = readch()
    if nxt != "[":
        # A bare Escape. Not a movement, so it means "leave" rather than
        # "step back", which is what a reader means by it.
        return Key.QUIT
    final = readch()
    return {
        "A": Key.UP,
        "B": Key.DOWN,
        "C": Key.RIGHT,
        "D": Key.LEFT,
    }.get(final, Key.OTHER)


def select(
    render,  # type: Callable[[int], Sequence[str]]
    count,  # type: int
    keys=None,  # type: Optional[Callable[[], str]]
    write=None,  # type: Optional[Callable[[str], object]]
    initial=0,  # type: int
    erase=True,  # type: bool
    escapable=True,  # type: bool
    openable=True,  # type: bool
    raw=True,  # type: bool
):
    # type: (...) -> object
    """Move a highlight over ``count`` rows; return the index, BACK or QUIT.

    ``render(i)`` returns the whole block to display with row ``i``
    highlighted, and is called again on every keypress. Redrawing everything
    rather than patching the changed line is deliberate: a partial repaint that
    gets one cell wrong is far harder to notice than a redraw that is merely
    slower, and at a dozen rows it is imperceptible.

    Three outcomes rather than two, because a nested view needs "out of here"
    to be different from "out of the program": the caller pops a level on
    ``BACK`` and unwinds entirely on ``QUIT``.

    ``erase`` clears the block on the way out, which is what makes the whole
    interaction happen in one place: each level replaces the last rather than
    scrolling it away, so there is one screen and not a transcript of them.
    """
    emit = write if write is not None else _emit
    reader = keys if keys is not None else read_key

    if count <= 0:
        return Key.QUIT

    cursor = max(0, min(initial, count - 1))
    painted = 0

    def paint():
        # type: () -> int
        lines = list(render(cursor))
        if painted:
            # Up to the top of the previous block and clear from there, so the
            # frames replace one another in place.
            emit("\033[%dA\033[J" % (painted,))
        emit("\n".join(lines) + "\n")
        return len(lines)

    session = raw_session() if raw else _Nothing()
    with session:
        painted = paint()
        while True:
            try:
                key = reader()
            except KeyboardInterrupt:
                if erase and painted:
                    emit("\033[%dA\033[J" % (painted,))
                return Key.QUIT

            if key == Key.QUIT:
                if erase and painted:
                    emit("\033[%dA\033[J" % (painted,))
                return Key.QUIT
            if key == Key.UP:
                cursor = (cursor - 1) % count
            elif key == Key.DOWN:
                cursor = (cursor + 1) % count
            elif key == Key.LEFT:
                # Nothing at the root: returning here would exit, so a stray
                # press would take the whole program down.
                if not escapable:
                    continue
                if erase and painted:
                    emit("\033[%dA\033[J" % (painted,))
                return Key.BACK
            elif key in (Key.RIGHT, Key.ENTER):
                # Nothing at a leaf: deeper is nowhere, and returning the
                # index would read as "step back" to a caller with nothing to
                # open.
                if not openable:
                    continue
                if erase and painted:
                    emit("\033[%dA\033[J" % (painted,))
                return cursor
            else:
                continue
            painted = paint()


class _Nothing(object):
    """A `with` block that does nothing, for `raw=False` in tests."""

    def __enter__(self):
        # type: () -> "_Nothing"
        return self

    def __exit__(self, *exc):
        # type: (*object) -> bool
        return False


def highlight(lines, index, style=None, pad_to=None):
    # type: (Sequence[str], int, Optional[object], Optional[int]) -> List[str]
    """Paint one line of an already-rendered block in inverse video.

    Inverse rather than a colour, because it survives `NO_COLOR`, a 16-colour
    console and a light background alike, and because it does not collide with
    the colours the table already uses to mean something.

    **The line is PADDED to the block's width first.** Without that the bar of
    inverse video is as long as whatever text the row happened to contain, so
    a row reading `24T free` highlighted about half as wide as one carrying a
    usage bar, and the cursor looked like a different shape on every row
    instead of a band moving down a column. Padding is what makes it read as
    one selection rather than as ragged emphasis.
    """
    from .render.style import width as measure

    room = pad_to
    if room is None:
        room = max([measure(line) for line in lines] or [0])

    out = []  # type: List[str]
    for position, line in enumerate(lines):
        if position == index:
            gap = " " * max(0, room - measure(line))
            # Every reset ALREADY IN the line has to re-assert the inverse,
            # and this is the whole trick. The table's cells carry their own
            # colours, so a row ends up with `\033[0m` several times along its
            # length; wrapping such a line in inverse turns the band off at
            # the first embedded reset and the highlight stops mid-row. That
            # is what made the cursor look like a different width on every
            # line: measured in a pty, three rows highlighted at 47, 69 and 74
            # columns, each stopping exactly where its first coloured cell
            # ended.
            body = line.replace(RESET, RESET + INVERSE)
            out.append(RESET + INVERSE + body + gap + RESET)
        else:
            out.append(line)
    return out
