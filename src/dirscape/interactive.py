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
  root exits, so one stray press took the whole program down. Escape is what
  leaves from the root, and inside a nested view it pops one level: a reader
  who presses it in a detail view means "back to the table", never "close the
  program", and `select` resolves which it is from its own depth.
* **Right at a leaf does nothing.** It used to return the index, and a caller
  with nothing deeper to open read that as "step back", so Right at the detail
  view landed back on the list. Right means deeper everywhere, and at the
  bottom deeper is nowhere.
"""

import os
import shutil
import sys
import time
from typing import Callable, List, Optional, Sequence

__all__ = [
    "Key",
    "supported",
    "read_key",
    "select",
    "raw_session",
    "highlight",
    "window_rows",
    "Screen",
    "repaint",
    "SYNC_BEGIN",
    "SYNC_END",
    "HeldKey",
    "MIN_LINES",
    "ACCEL_AFTER",
    "ACCEL_GAP",
    "ACCEL_MIN_RATE",
    "ACCEL_FIRST",
    "ACCEL_DOUBLE",
    "ACCEL_MAX",
]

#: A terminal shorter than this cannot hold a frame, so the static print is
#: better than a screen that repaints on top of itself.
MIN_LINES = 10

#: SGR reset and inverse. Named because `highlight` has to reason about where
#: they appear inside text it did not write.
RESET = "\033[0m"
INVERSE = "\033[7m"

#: DEC private mode 2026, "synchronized update". A terminal that supports it
#: holds the screen while a frame arrives and then shows it at once; one that
#: does not ignores it, as it ignores every private mode it lacks. So a frame
#: is atomic where the terminal can make it so and no worse anywhere else.
SYNC_BEGIN = "\033[?2026h"
SYNC_END = "\033[?2026l"


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
    #: `m`: add up the highlighted directory in full. Only a directory listing
    #: acts on it; everywhere else it is ignored like any other letter.
    MEASURE = "measure"
    #: `s`: put an opened directory's rows in the other order, size or name.
    SORT = "sort"
    #: Not a keypress: the view changed behind the reader (a size arrived) and
    #: wants painting again, with the cursor where it is.
    REDRAW = "redraw"


def window_rows():
    # type: () -> int
    """The terminal's height, or 0 when it cannot be determined.

    Separate from `supported` because the two answer different questions:
    `supported` asks whether anybody can type at all, and this asks whether
    what a caller is about to draw will fit. A caller that skips the second
    check repaints a block taller than the window and the cursor arithmetic
    lands in the wrong place.
    """
    try:
        return int(shutil.get_terminal_size().lines)
    except Exception:  # pragma: no cover
        return 0


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


#: How long to wait for the rest of an escape sequence before concluding the
#: key was a bare Escape. An arrow's `[` follows its `ESC` in the same read
#: from the terminal driver, so any real sequence is already buffered and this
#: never actually elapses; a human cannot press two keys inside it either way.
ESCAPE_GRACE = 0.05


def _readch():
    # type: () -> str
    """One byte, straight off the file descriptor.

    **Not `sys.stdin.read(1)`, and the difference is load bearing.** That is a
    `TextIOWrapper` over a buffered reader, so asking it for one character
    pulls a whole chunk off the fd into a Python-level buffer. `_pending` then
    asks the KERNEL whether more input is waiting and is told no, because the
    rest of the arrow sequence is sitting in the buffer rather than in the
    terminal. Measured in a pty: a down arrow decoded as `back` followed by
    `other`, so every arrow press was read as an Escape.

    Reading the fd directly keeps the two in agreement: whatever has not been
    decoded yet is still where `select` can see it.
    """
    try:
        return os.read(sys.stdin.fileno(), 1).decode("utf-8", "replace")
    except Exception:  # pragma: no cover - closed or non-readable stdin
        return ""


def _pending(timeout=ESCAPE_GRACE):
    # type: (float) -> bool
    """Is there more input already waiting on the terminal?

    The question `read_key` cannot answer with a blocking read, and the whole
    reason Escape did not work. See the note there.

    Asks about the file DESCRIPTOR, which is only a correct answer because
    `_readch` also reads the descriptor. Pairing this with a buffered reader
    is the trap documented above.
    """
    try:
        import select

        ready, _, _ = select.select([sys.stdin.fileno()], [], [], timeout)
        return bool(ready)
    except Exception:  # pragma: no cover - no select, or a closed stdin
        # Assume more is coming, which degrades to the old blocking behaviour
        # rather than misreading an arrow as an Escape. A stuck Escape is a
        # worse failure than an arrow that jumps out of the view.
        return True


def input_waiting(timeout):
    # type: (float) -> bool
    """Whether a keypress arrives within ``timeout`` seconds, without reading it.

    For a view whose content changes while the reader looks at it: it waits
    for input in short slices and repaints between them, and only calls
    `read_key`, which blocks, once a key is really there.
    """
    return _pending(timeout)


def read_key(readch=None, pending=None):
    # type: (Optional[Callable[[], str]], Optional[Callable[[], bool]]) -> str
    """One decoded keypress.

    Arrows arrive as three bytes (`ESC [ A`), so a bare Escape is only Escape
    once no bracket follows it. A lone `ESC` read as the first byte of an arrow
    would swallow the next keypress, which is why the sequence is decoded here
    rather than by the caller.

    **And that is why Escape appeared not to work at all.** Deciding what an
    `ESC` means took a second BLOCKING read, so a bare Escape sat waiting for
    a byte that was never coming: the view did not move, and the keypress was
    only consumed when the reader pressed something else, at which point that
    second key was eaten deciding the first. Owner's report, twice: "esc
    doesn't work". The earlier fix to this made Escape mean BACK instead of
    QUIT, which was necessary and did nothing on its own, because the key was
    never reaching the branch.

    It was also invisible to every test here. A scripted `readch` returning
    `"\x1bz"` always has a next character, so the decode looked correct; only
    a real terminal with nothing more to give exposes it. `pending` is
    injectable for exactly that reason and the test drives it.
    """
    if readch is None:
        readch = _readch
    if pending is None:
        pending = _pending

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
    if char in ("m", "M"):
        return Key.MEASURE
    if char in ("s", "S"):
        return Key.SORT
    if char == "\x03":  # Ctrl-C, which cbreak leaves to us on some platforms
        raise KeyboardInterrupt
    if char != "\x1b":
        return Key.OTHER

    # Nothing more waiting means the reader pressed Escape and let go. Asked
    # BEFORE the read, because the read is the thing that would block.
    if not pending():
        return Key.BACK

    nxt = readch()
    if nxt != "[":
        # A bare Escape, and it means BACK rather than QUIT. It used to return
        # QUIT on the reading that Escape is not a movement, so "leave" was
        # the honest translation. That reading was wrong in the one place it
        # matters: pressing Escape inside a detail view closed the whole
        # program instead of returning to the table, which is the opposite of
        # what Escape does in every other nested view a reader has used, and
        # the owner reported it as "esc doesn't work for going back".
        #
        # `select` resolves it against its own depth: BACK pops a level where
        # there is one to pop, and falls through to QUIT at the root, so
        # Escape still leaves from the top. Nothing else can decide this,
        # because only the caller knows whether it is nested.
        return Key.BACK
    final = readch()
    return {
        "A": Key.UP,
        "B": Key.DOWN,
        "C": Key.RIGHT,
        "D": Key.LEFT,
    }.get(final, Key.OTHER)


#: How long an arrow has to be HELD before the highlight starts covering
#: ground. Below this nothing changes at all: one press, or a short burst of
#: them, still moves exactly one row, because reading down a list a row at a
#: time is what the key is for and a ramp that started immediately would take
#: that away.
#:
#: Shorter than the 3.0s slurmpast uses, and deliberately: its longest list is
#: a 29,617-row job history where three seconds is 2% of the journey, while
#: the longest thing here is a directory of a few hundred children, where it
#: would be most of one.
ACCEL_AFTER = 1.5

#: The gap that ends a run of one held key. Generous, because the thing most
#: likely to open a gap is this program: a repaint of a full window costs far
#: more than the ~12 ms a short one does. `ACCEL_MIN_RATE` is what stops a
#: slow tapper inheriting a held key's speed, and it is a better test for it.
ACCEL_GAP = 0.5

#: Keys per second a run has to average before it may accelerate. A held key
#: repeats at 25-33 Hz on every common setting and never below 10; a reader
#: pressing an arrow deliberately manages three or four a second. So this
#: separates "held" from "pressed a lot", which the gap alone cannot.
#:
#: Tested ONCE, when the run first crosses `ACCEL_AFTER`, and latched both
#: ways. Re-testing continuously fails in both directions: the slower frames
#: that accelerating causes would pull a held key back under the bar, and a
#: tapper who kept going long enough would eventually clear any running count.
ACCEL_MIN_RATE = 10.0

#: Rows per press the moment the threshold is crossed. Meant to be felt, not
#: eased into. At a 30 Hz repeat it is 120 rows a second.
ACCEL_FIRST = 4

#: ...and it doubles every this many seconds after that.
ACCEL_DOUBLE = 1.0

#: Where the ramp stops: 960 rows a second at 30 Hz, which crosses the largest
#: directory on this cluster in about fifteen seconds. Past this the names are
#: a blur and a longer stride buys nothing but overshoot on the way back.
ACCEL_MAX = 32

_ACCELERATING_KEYS = frozenset([Key.UP, Key.DOWN])


class HeldKey(object):
    """How far one arrow moves, given how long it has been held.

    A remote control's fast-forward, and asked for in those words: tap it and
    it steps one row, hold it and after a moment it starts covering ground.
    Ported from `slurmpast`'s `_HeldKey`, which the owner named as the
    reference, with the threshold shortened for much smaller lists.

    A run ends the moment the key changes or the gap between presses exceeds
    `ACCEL_GAP`, so speed built up in one direction is never inherited by the
    next thing pressed, including the arrow the other way. That is how
    somebody stops after overshooting.

    **Clockless: the caller passes the time.** So the whole ramp is testable
    at every point on it without a running terminal or a sleeping test.
    """

    __slots__ = ("_key", "_started", "_last", "_count", "_verdict")

    def __init__(self):
        # type: () -> None
        self._key = ""
        self._started = 0.0
        self._last = 0.0
        self._count = 0
        self._verdict = None  # type: Optional[bool]

    def stride(self, key, now):
        # type: (str, float) -> int
        """Rows this press should move. Always 1 until the key has been held."""
        if key not in _ACCELERATING_KEYS:
            self._key = ""
            return 1
        if key != self._key or now - self._last > ACCEL_GAP:
            self._key = key
            self._started = now
            self._count = 0
            self._verdict = None
        self._last = now
        self._count += 1
        held = now - self._started
        if held < ACCEL_AFTER:
            return 1
        if self._verdict is None:
            self._verdict = self._count >= held * ACCEL_MIN_RATE
        if not self._verdict:
            return 1
        # Capped before the shift, not after: the exponent comes off the wall
        # clock and a key held for a minute would otherwise build a 2**57 that
        # `min` then throws away.
        doublings = min(int((held - ACCEL_AFTER) / ACCEL_DOUBLE), 16)
        return min(ACCEL_MAX, ACCEL_FIRST << doublings)


def repaint(previous, lines):
    # type: (Sequence[str], Sequence[str]) -> str
    """The bytes that turn the block ``previous`` into ``lines``, drawn in place.

    **This is the flicker fix, and the old repaint is why it is needed.** Every
    arrow used to send `ESC[<n>A ESC[J` (up to the top of the block, erase
    everything below) and then the new frame, as two separately flushed
    writes. Between them the terminal holds an ERASED block, and whenever it
    renders in that gap, which over ssh and through tmux it often does, the
    reader sees the table vanish and come back. Measured in a 40 x 120 pty
    with a terminal emulator fed every chunk the program wrote: during a run
    of taps and a 30 Hz held arrow, 174 of 261 screen states showed a partial
    table and 85 showed it gone entirely.

    So nothing is erased before it is replaced. Each line is overwritten where
    it stands; a line identical to the one already there is stepped over, so a
    move of the highlight rewrites the two rows that changed and not all
    thirty; a line narrower than its predecessor is padded over the rest of
    it. Only rows the new block no longer uses are cleared, and only after
    everything else is drawn. The whole frame is one string, written once and
    wrapped in `SYNC_BEGIN` / `SYNC_END`.

    Padding rather than `ESC[K` for the remainder of a line, because a line
    that ends in the last column leaves the cursor pending a wrap, and there
    erase-in-line takes out the character just written, which is the right
    border of this view.

    The cursor starts and ends on the row below the block, in column 0, which
    is where writing a block and a newline leaves it.
    """
    from .render.style import width as measure

    out = []  # type: List[str]
    if previous:
        out.append("\r\033[%dA" % (len(previous),))
    for index, line in enumerate(lines):
        old = previous[index] if index < len(previous) else None
        if old is not None and old == line:
            out.append("\n")
            continue
        out.append(line)
        if old is not None:
            gap = measure(old) - measure(line)
            if gap > 0:
                out.append(RESET + " " * gap)
        out.append("\n")
    if len(previous) > len(lines):
        out.append("\033[J")
    return SYNC_BEGIN + "".join(out) + SYNC_END


class Screen(object):
    """The block this module has on screen, so the next frame can be drawn over it.

    One per `select` by default, which erases it on the way out. A caller that
    moves between views, as the browse does from the table into a listing and
    back, passes ONE screen to every `select`: then only quitting erases, and
    each view's first frame is drawn over the last view's final one. Erasing
    on every exit was a second, smaller flicker: the table vanished when a row
    was opened and stayed gone while the listing was read, which for a
    directory of ten thousand entries is long enough to see.
    """

    __slots__ = ("lines", "_write")

    def __init__(self, write=None):
        # type: (Optional[Callable[[str], object]]) -> None
        self.lines = []  # type: List[str]
        self._write = write if write is not None else _emit

    def paint(self, lines):
        # type: (Sequence[str]) -> None
        frame = list(lines)
        self._write(repaint(self.lines, frame))
        self.lines = frame

    def erase(self):
        # type: () -> None
        """Take the block off the screen, leaving the cursor where it began."""
        if self.lines:
            self._write("\r\033[%dA\033[J" % (len(self.lines),))
        self.lines = []


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
    pending=None,  # type: Optional[Callable[[], bool]]
    clock=None,  # type: Optional[Callable[[], float]]
    screen=None,  # type: Optional[Screen]
    remap=None,  # type: Optional[Callable[[int], int]]
):
    # type: (...) -> object
    """Move a highlight over ``count`` rows; return the index, BACK or QUIT.

    ``remap`` is asked where the cursor goes on every `REDRAW`, for a view
    whose rows can change ORDER behind the reader: an opened directory sorted
    by size re-sorts as its sizes land, and the highlight has to stay on the
    folder it was on rather than on whatever row now has its index.

    ``render(i)`` returns the whole block to display with row ``i``
    highlighted, and is called again on every keypress. The whole block is
    rendered every time, so the view cannot drift from the renderer, and
    `repaint` then sends only the LINES that changed, each rewritten in full:
    nothing is patched below the granularity of a line, which is where a
    partial repaint gets a cell wrong.

    ``screen`` is shared across views by a caller that moves between them; see
    `Screen`. With one, only QUIT erases the block, and any other exit leaves
    it for the next view to draw over.

    Three outcomes rather than two, because a nested view needs "out of here"
    to be different from "out of the program": the caller pops a level on
    ``BACK`` and unwinds entirely on ``QUIT``.

    ``erase`` clears the block on the way out, which is what makes the whole
    interaction happen in one place: each level replaces the last rather than
    scrolling it away, so there is one screen and not a transcript of them.

    **A held arrow is handled in two ways at once, and it needs both.**

    `HeldKey` grows the STRIDE, so a long hold covers more ground than the
    keyboard's own repeat rate allows. On its own that makes the second
    problem worse: this loop repaints the entire block on every key, and once
    the stride outruns the repaint the terminal's input buffer fills, the
    cursor keeps flying for a second after the reader lets go, and the list
    stops where nobody asked. So `pending` is consulted before each repaint
    and a key already waiting is folded into the same frame. Coalescing bounds
    the backlog at zero by construction, which is what makes the accelerator
    safe rather than merely fast.
    """
    emit = write if write is not None else _emit
    reader = keys if keys is not None else read_key
    now = clock if clock is not None else time.monotonic
    if pending is None:
        # Only on a real terminal. With `raw=False` this is a test driving a
        # scripted key list, where "is there more input waiting" is a question
        # about a file descriptor nobody is typing at.
        pending = (lambda: _pending(0.0)) if raw else (lambda: False)
    held = HeldKey()

    if count <= 0:
        return Key.QUIT

    cursor = max(0, min(initial, count - 1))
    shared = screen is not None
    canvas = screen if screen is not None else Screen(emit)

    def paint():
        # type: () -> None
        canvas.paint(render(cursor))

    def leave(final=False):
        # type: (bool) -> None
        # A private screen is erased on every exit, as it always was. A shared
        # one only on QUIT: otherwise the next view draws over it, and erasing
        # here would put a blank screen between the two.
        if erase and (final or not shared):
            canvas.erase()

    session = raw_session() if raw else _Nothing()
    with session:
        paint()
        while True:
            try:
                key = reader()
            except KeyboardInterrupt:
                leave(final=True)
                return Key.QUIT

            if key == Key.QUIT or (key == Key.BACK and not escapable):
                # Escape at the root leaves, because there is no level to pop
                # and a key that does nothing reads as a hung program.
                leave(final=True)
                return Key.QUIT
            if key == Key.BACK:
                leave()
                return Key.BACK
            if key in (Key.UP, Key.DOWN):
                # **Wrapping is for a TAP and clamping is for a HOLD.** A
                # single press off the top meaning "jump to the bottom" is a
                # deliberate shortcut worth keeping; the same wrap arriving in
                # the middle of a two-second hold is an accident that throws
                # the reader back to the other end of a directory they were
                # reading down.
                step = held.stride(key, now())
                if key == Key.UP:
                    cursor = (cursor - 1) % count if step == 1 else max(0, cursor - step)
                else:
                    cursor = (cursor + 1) % count if step == 1 else min(count - 1, cursor + step)
            elif key == Key.LEFT:
                # Nothing at the root: returning here would exit, so a stray
                # press would take the whole program down.
                if not escapable:
                    continue
                leave()
                return Key.BACK
            elif key in (Key.RIGHT, Key.ENTER):
                # Nothing at a leaf: deeper is nowhere, and returning the
                # index would read as "step back" to a caller with nothing to
                # open.
                if not openable:
                    continue
                leave()
                return cursor
            elif key == Key.REDRAW:
                # Painted where it stands: the rows changed, the cursor did not,
                # unless the rows moved, in which case it follows its row.
                if remap is not None:
                    cursor = max(0, min(count - 1, int(remap(cursor))))
            else:
                continue
            if pending():
                # Another key is already waiting. Fold it into this frame
                # rather than drawing one the reader will never see.
                continue
            paint()


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

    **The row's OWN colours are stripped, not preserved, and that is the fix
    for the band that "gets truncated by `▎▒░░░░░░   3%`".** Under inverse
    video an explicit foreground code becomes the BACKGROUND, so each coloured
    run inside the band painted as a block of its own colour sitting on top of
    the selection: measured in a pty, six of seven frames carried
    `\033[38;2;90;140;220m` and friends inside the band and the usage bar
    rendered as three differently coloured tiles where the band should have
    been flat.

    An earlier fix re-asserted the inverse after every embedded reset, which
    kept the band CONTINUOUS and could do nothing about its colour, because
    the colour was never the reset's fault. Stripping is the whole answer and
    it costs nothing: a selected row does not need per-cell colour, the band
    is the signal, and this package's standing rule is that colour is never
    load bearing, so every state the row reports is still on the line in
    glyphs and words.
    """
    from .render.style import plain
    from .render.style import width as measure

    room = pad_to
    if room is None:
        room = max([measure(line) for line in lines] or [0])

    out = []  # type: List[str]
    for position, line in enumerate(lines):
        if position == index:
            bare = plain(line)
            gap = " " * max(0, room - measure(bare))
            out.append(RESET + INVERSE + bare + gap + RESET)
        else:
            out.append(line)
    return out
