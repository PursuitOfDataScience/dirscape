"""What moves on screen while dirscape waits on something, and nothing else.

Motion follows the rule this package keeps about colour: **it is never load
bearing.** Every state a view reports is on screen in glyphs and words with
motion off (`--no-motion`, `DIRSCAPE_NO_MOTION`), and each kind of motion
means exactly one thing:

* a SPIN: work is in progress on this thing, right now;
* a FLASH, a fade from the accent colour: this figure just arrived;
* a fill, drawn by `style.panel`: this share of a known whole is done;
* a SHINE running once: a long wait just finished (looping, on the fill: it is
  still growing);
* stillness: settled, and that includes every `?`, which never moves.

**A frame is never rendered in order to animate it.** Measured on this
cluster, one render of an opened directory of 200 folders takes 41 ms on
Python 3.12 and 75 ms on 3.6 (median of 60 renders each), so ten renders a
second would take most of a core from the very walk a spinner reports on. The
renderer marks what moves with zero-width markers instead, `Screen` keeps the
rendered block, and a frame only resolves the markers against the clock
(`Motion.overlay`): string work on the few lines that carry one.

**A counting spinner turns only when the count under it moved.** A spinner on
a timer keeps turning on a hung mount, which is the moment its reader most
needs to know nothing is happening. This one steps when the entry counter
changed since its last step, so a stalled walk stops it dead.

Python 3.6 and the standard library, as everywhere in this package.
"""

import contextlib
import math
import os
import sys
import threading
import time
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

from .render.style import _PALETTE, pad, plain, truncate, width

__all__ = [
    "FRAME_S",
    "FLASH_S",
    "GROW_S",
    "BURST_FRAME_S",
    "SMOOTH_FRAME_S",
    "FLASH_AFTER_S",
    "SPIN_DELAY_S",
    "SHINE_S",
    "STALL_S",
    "BOARD_DELAY_S",
    "SLOW_S",
    "NOTIFY_AFTER_S",
    "MARK",
    "SPIN",
    "FLASH",
    "SHINE",
    "TEXT",
    "allowed",
    "foreground",
    "strip_marks",
    "keep_spins",
    "escape_end",
    "stopwatch",
    "sparkline",
    "Meter",
    "Motion",
    "Chrome",
    "Board",
    "Quiet",
]

#: Seconds per frame. Ten a second is where a braille spinner reads as turning
#: rather than flickering; slurmwatch runs its own at eight.
FRAME_S = 0.1

#: How long a figure that just landed takes to fade from the accent colour
#: back into its own: continuously and eased, never in visible steps.
FLASH_S = 0.8

#: A figure fades in only if its count took at least this long. The quick
#: counts of a first pass land a dozen a second, and a table of figures each
#: fading on its own is a table flickering pink; the figure worth marking is
#: the one somebody waited for.
FLASH_AFTER_S = 1.0

#: A count starts to spin once it has run this long: the second after which a
#: wait deserves a sign of life, and before which one that shows for a frame
#: and is gone is a flicker, not news. Measured at 0.3s, half the spinners on
#: `/project2/rcc` showed for 0.1s or less. (Only the long count spins at
#: all: see `cli._Sizes.walking`.)
SPIN_DELAY_S = 1.0

#: How long the share bars take to grow in, once every folder is counted.
GROW_S = 0.4

#: Seconds per frame while the bars grow: those are RENDERED, since the
#: renderer draws the bars, so this is a ceiling. A frame of 200 rows that
#: takes 41 ms to render simply comes later, and the growth, which is timed
#: by the clock and not by frames, is never slower for it.
BURST_FRAME_S = 1.0 / 30

#: How long the one shine along the title takes when the last folder lands.
SHINE_S = 0.9

#: The glow that drifts along a growing top edge: slow, soft and seldom. It
#: crosses the edge at `SHEEN_CELLS_S`, `SHEEN_WIDTH` cells from side to side
#: with a cosine falloff, its brightest cell lifted `SHEEN_LIFT` of the way to
#: the highlight, and rests `SHEEN_REST_S` before the next pass. Its place
#: comes from the clock alone and is measured along the whole edge, so the
#: filled part growing under it never makes it jump. It replaced a streak
#: that looped every half second on a short bar and moved five cells a
#: frame: measured, the edge changed on every frame, and it read as flicker.
SHEEN_CELLS_S = 20.0
SHEEN_WIDTH = 12.0
SHEEN_LIFT = 0.4
SHEEN_REST_S = 2.5

#: Seconds per frame while a glow is moving: a cell a frame at
#: `SHEEN_CELLS_S`. Overlay frames only, which render nothing.
SMOOTH_FRAME_S = 0.05

#: What a glow lifts toward: the frame's periwinkle, and the title's orchid.
_SHEEN_EDGE = (214, 206, 255)
_SHEEN_TITLE = (240, 190, 244)

#: A count that has read nothing for this long says so.
STALL_S = 5.0

#: How long startup stays blank before it shows its board. A board that
#: flashes up for 300 ms and vanishes reads as a glitch, not as information.
BOARD_DELAY_S = 0.5

#: When the board adds a line naming the slowest thing in flight.
SLOW_S = 5.0

#: A count longer than this is one its reader has probably stopped watching,
#: so its end is worth a notification where the terminal takes one.
NOTIFY_AFTER_S = 120.0

RESET = "\033[0m"
INVERSE = "\033[7m"

# --------------------------------------------------------------------------
# markers
# --------------------------------------------------------------------------

#: Every marker starts with this, open or close: what a line is searched for.
MARK = "\033[?770"

#: ``ESC [ ? 7700 ; kind ; id z`` opens a span and ``ESC [ ? 7701 z`` closes it.
#:
#: Private CSI sequences, so everything that measures text in this package
#: already reads them as zero width (`style.width`, `truncate`, `plain`) and a
#: table pads a marked cell exactly as it pads the same cell unmarked. No
#: terminal defines a `?`-prefixed `z` sequence, so one that ever escaped would
#: be ignored rather than acted on; `Screen` resolves every one before it
#: writes, and `cli._write` strips any from what is printed.
_HEAD = "\033[?7700;"
_OPEN = _HEAD + "%d;%dz"
_CLOSE = "\033[?7701z"

SPIN = 1
FLASH = 2
SHINE = 3
TEXT = 4


def strip_marks(text):
    # type: (str) -> str
    """The text with every motion marker taken out and the content left."""
    if MARK not in text:
        return text
    out = []  # type: List[str]
    index = 0
    while True:
        at = text.find(MARK, index)
        if at < 0:
            out.append(text[index:])
            break
        out.append(text[index:at])
        end = text.find("z", at)
        if end < 0:
            break
        index = end + 1
    return "".join(out)


def escape_end(text, index):
    # type: (str, int) -> int
    """Where the escape sequence starting at ``index`` ends, as `style.plain` reads it."""
    j = index + 1
    if j < len(text) and text[j] == "[":
        j += 1
        while j < len(text) and not (0x40 <= ord(text[j]) <= 0x7E):
            j += 1
    return j + 1


def keep_spins(text):
    # type: (str) -> str
    """`style.plain`, keeping the SPIN markers and nothing else.

    For the highlight band, which strips a row's colours because under inverse
    video a foreground code becomes a block of background: the spinner of the
    row being counted has to keep turning inside the band, and it is glyphs
    only, while a FLASH or SHINE is colour and goes with the rest of it.
    """
    out = []  # type: List[str]
    keeping = False
    index = 0
    while index < len(text):
        char = text[index]
        if char != "\033":
            out.append(char)
            index += 1
            continue
        end = escape_end(text, index)
        sequence = text[index:end]
        if sequence.startswith(_HEAD):
            keeping = _parse(sequence)[0] == SPIN
            if keeping:
                out.append(sequence)
        elif sequence == _CLOSE and keeping:
            out.append(sequence)
            keeping = False
        index = end
    return "".join(out)


def _parse(sequence):
    # type: (str) -> Tuple[int, int]
    """``(kind, id)`` of an open marker, or ``(0, 0)`` for anything else."""
    try:
        kind, ident = sequence[len(_HEAD) : -1].split(";")[:2]
        return int(kind), int(ident)
    except ValueError:
        return 0, 0


def _cells(styled):
    # type: (str) -> List[Tuple[str, str]]
    """Each visible character of ``styled``, with the SGR in force where it stands."""
    out = []  # type: List[Tuple[str, str]]
    state = ""
    index = 0
    while index < len(styled):
        char = styled[index]
        if char == "\033":
            end = escape_end(styled, index)
            sequence = styled[index:end]
            if sequence.endswith("m"):
                state = "" if sequence in (RESET, "\033[m") else state + sequence
            index = end
            continue
        out.append((state, char))
        index += 1
    return out


def _recolor(cells, paint):
    # type: (Sequence[Tuple[str, str]], Callable[[int, str], str]) -> str
    """Rebuild styled text, with ``paint(i, state)`` choosing each cell's SGR."""
    out = []  # type: List[str]
    current = None  # type: Optional[str]
    for position, (state, char) in enumerate(cells):
        want = paint(position, state)
        if want != current:
            out.append(RESET + want)
            current = want
        out.append(char)
    out.append(RESET)
    return "".join(out)


# --------------------------------------------------------------------------
# small pieces
# --------------------------------------------------------------------------


def allowed(no_motion=False, agent=False, env=None):
    # type: (bool, bool, Optional[Dict[str, str]]) -> bool
    """Whether this run may animate at all. Each view still checks its own stream."""
    environ = os.environ if env is None else env
    if no_motion or agent:
        return False
    if str(environ.get("DIRSCAPE_NO_MOTION") or "").strip():
        return False
    return environ.get("TERM", "") != "dumb"


def foreground(stream):
    # type: (Any) -> bool
    """Whether ``stream`` is a terminal this process owns the foreground of.

    A background job that draws on the terminal draws over whatever its
    reader is typing, and one that changes the terminal's mode is stopped by
    the kernel (`SIGTTOU`) until it is brought back: neither is acceptable for
    a spinner, so neither happens unless this is true.
    """
    try:
        if not stream.isatty():
            return False
        return os.tcgetpgrp(stream.fileno()) == os.getpgrp()
    except Exception:
        return False


def stopwatch(seconds):
    # type: (float) -> str
    """``0:24``, ``12:03``, ``1:02:03``: elapsed time as a clock face reads it."""
    whole = int(max(0.0, seconds))
    hours, rest = divmod(whole, 3600)
    minutes, secs = divmod(rest, 60)
    if hours:
        return "%d:%02d:%02d" % (hours, minutes, secs)
    return "%d:%02d" % (minutes, secs)


def sparkline(history, glyphs):
    # type: (Sequence[Optional[int]], Sequence[str]) -> str
    """One cell per second of ``history``, tallest for the busiest second.

    A second from before the first sample is left out rather than drawn as the
    lowest bar: no reading is not a reading of nothing, so the line grows in
    from nothing. Anything read at all is at least the second step, so a flat
    line along the bottom means exactly zero.
    """
    known = [value for value in history if value is not None]
    peak = max(known) if known else 0
    top = len(glyphs) - 1
    out = []  # type: List[str]
    for value in known:
        if value <= 0 or peak <= 0:
            out.append(glyphs[0])
        else:
            out.append(glyphs[max(1, min(top, int(round(float(value) / peak * top))))])
    return "".join(out)


def _printable(text, limit):
    # type: (str, int) -> str
    """Text safe inside an OSC string: no control characters, and short."""
    clean = "".join(ch if " " <= ch != "\x7f" else " " for ch in str(text))
    return clean.strip()[:limit]


def _blend(low, high, mix):
    # type: (Tuple[int, int, int], Tuple[int, int, int], float) -> Tuple[int, int, int]
    mix = max(0.0, min(1.0, mix))
    return (
        int(round(low[0] + (high[0] - low[0]) * mix)),
        int(round(low[1] + (high[1] - low[1]) * mix)),
        int(round(low[2] + (high[2] - low[2]) * mix)),
    )


def _rgb_of(state):
    # type: (str) -> Optional[Tuple[int, int, int]]
    """The truecolor foreground an SGR state sets, if it sets one."""
    at = state.rfind("38;2;")
    if at < 0:
        return None
    try:
        red, green, blue = state[at + 5 :].split("m")[0].split(";")[:3]
        return int(red), int(green), int(blue)
    except ValueError:
        return None


# --------------------------------------------------------------------------
# a count's pace
# --------------------------------------------------------------------------


class Meter(object):
    """A count's pace, from samples of its running total: rate, history, stalls.

    Samples are kept only when the total moved, so the record is a step
    function of time and a stall costs nothing to remember. One `Meter` per
    view; a new ``key`` (another folder) starts it over.
    """

    __slots__ = ("key", "_samples", "_moved_at")

    #: Seconds of history, one sparkline cell each.
    CELLS = 8

    def __init__(self):
        # type: () -> None
        self.key = None  # type: Optional[str]
        self._samples = []  # type: List[Tuple[float, int]]
        self._moved_at = None  # type: Optional[float]

    def sample(self, key, now, count):
        # type: (Optional[str], float, Optional[int]) -> None
        if key != self.key:
            self.key, self._samples, self._moved_at = key, [], now
        if count is None:
            return
        if self._samples and self._samples[-1][1] == count:
            return
        self._samples.append((now, int(count)))
        self._moved_at = now
        horizon = now - (self.CELLS + 3)
        while len(self._samples) > 2 and self._samples[1][0] < horizon:
            self._samples.pop(0)

    def _at(self, moment):
        # type: (float) -> Optional[int]
        found = None  # type: Optional[int]
        for when, count in self._samples:
            if when > moment:
                break
            found = count
        return found

    def rate(self, now, span=2.0):
        # type: (float, float) -> Optional[float]
        """Entries a second over the last ``span`` whole seconds, or None this early.

        Whole seconds of the clock, so the figure changes once a second and
        not with every frame that happens to sample it.
        """
        if not self._samples:
            return None
        end = math.floor(now)
        start = max(self._samples[0][0], end - span)
        if end - start < 1.0:
            return None
        before, after = self._at(start), self._at(end)
        if before is None or after is None:
            return None
        return max(0.0, (after - before) / (end - start))

    def history(self, now):
        # type: (float) -> List[Optional[int]]
        """Entries read in each of the last `CELLS` whole seconds, oldest first.

        Whole seconds for the same reason as `rate`: a window sliding with
        every frame redrew the whole sparkline ten times a second.
        """
        end = math.floor(now)
        out = []  # type: List[Optional[int]]
        for back in range(self.CELLS, 0, -1):
            low, high = self._at(end - back), self._at(end - back + 1)
            out.append(None if low is None or high is None else max(0, high - low))
        return out

    def still(self, now):
        # type: (float) -> float
        """Seconds since the count last moved."""
        return 0.0 if self._moved_at is None else max(0.0, now - self._moved_at)


# --------------------------------------------------------------------------
# the overlay
# --------------------------------------------------------------------------


class Motion(object):
    """Everything that moves in one live session: its clock, markers and effects.

    The renderer asks for a marked span (`spin`, `flash`, `shine`, `text`),
    and `overlay` resolves every span on a block of finished lines against the
    clock, which is all a frame does. What the last `overlay` found is what
    `animating` answers from, so a view with nothing moving on it asks for no
    frames and costs nothing.
    """

    def __init__(self, style, clock=None, chrome=None):
        # type: (Any, Optional[Callable[[], float]], Optional[Chrome]) -> None
        self.style = style
        self.clock = clock if clock is not None else time.monotonic
        self.chrome = chrome
        self._ids = {}  # type: Dict[Tuple[int, str], int]
        self._keys = {}  # type: Dict[int, str]
        self._flashes = {}  # type: Dict[int, float]
        self._shines = {}  # type: Dict[int, Tuple[Optional[float], bool, Optional[int]]]
        self._texts = {}  # type: Dict[int, Callable[[int, float], Optional[str]]]
        #: SPIN keys that turn on the clock: nothing to be honest to.
        self._always = set()  # type: set
        #: key -> [frame, count at the last step, when it stepped]
        self._spinners = {}  # type: Dict[str, List[Any]]
        self._follow = None  # type: Optional[Callable[[], Tuple[Optional[str], Optional[int]]]]
        self._burst_until = 0.0
        self._live = (None, None)  # type: Tuple[Optional[str], Optional[int]]
        #: When the key in flight became the one in flight, for `SPIN_DELAY_S`.
        self._live_since = 0.0
        self._frames = {}  # type: Dict[str, int]
        # spins, live, texts, loops, fading until, smooth: see `overlay`.
        self._found = [False, False, False, False, 0.0, False]  # type: List[Any]

    # -- asked by the renderer --------------------------------------------

    def now(self):
        # type: () -> float
        return self.clock()

    def _ident(self, kind, key):
        # type: (int, str) -> int
        ident = self._ids.get((kind, key))
        if ident is None:
            ident = len(self._ids) + 1
            self._ids[(kind, key)] = ident
            self._keys[ident] = key
        return ident

    def spin(self, key, text, always=False):
        # type: (str, str, bool) -> str
        """``text``, turning while ``key`` is the one in flight (`follow`), or ``always``."""
        if always:
            self._always.add(key)
        return _OPEN % (SPIN, self._ident(SPIN, key)) + text + _CLOSE

    def flash(self, key, since, text):
        # type: (str, Optional[float], str) -> str
        """``text``, fading in from the accent colour for `FLASH_S` after ``since``."""
        if not self.style.enabled or not text or since is None:
            return text
        if self.now() - since >= FLASH_S:
            return text
        ident = self._ident(FLASH, key)
        self._flashes[ident] = since
        return _OPEN % (FLASH, ident) + text + _CLOSE

    def shine(self, key, text, since=None, loop=False, track=None):
        # type: (str, str, Optional[float], bool, Optional[int]) -> str
        """``text`` with a soft glow drifting along it, once from ``since`` or on a loop.

        ``track`` is how far a looping glow travels when ``text`` is only part
        of its way: along the whole top edge, of which ``text`` is the filled
        part. Truecolor only, where the glow is a smooth gradient; in fewer
        colours it could only be drawn in steps, which is what flickers.
        """
        if self.style.depth < 24 or not self.style.enabled or not text:
            return text
        if not loop and (since is None or self.now() - since >= SHINE_S):
            return text
        ident = self._ident(SHINE, key)
        self._shines[ident] = (since, loop, track)
        return _OPEN % (SHINE, ident) + text + _CLOSE

    def text(self, key, text, source):
        # type: (str, str, Callable[[int, float], Optional[str]]) -> str
        """``text``, replaced every frame by ``source(columns, now)``, cut to its width."""
        ident = self._ident(TEXT, key)
        self._texts[ident] = source
        return _OPEN % (TEXT, ident) + text + _CLOSE

    def follow(self, source):
        # type: (Optional[Callable[[], Tuple[Optional[str], Optional[int]]]]) -> None
        """Where to ask which key is in flight, and its count so far."""
        self._follow = source

    def burst(self, seconds):
        # type: (float) -> None
        """Render every frame for ``seconds``: for motion that is not a marker."""
        self._burst_until = max(self._burst_until, self.now() + seconds)

    def bursting(self):
        # type: () -> bool
        return self.now() < self._burst_until

    def spinner(self, key, cells):
        # type: (Optional[str], int) -> Optional[str]
        """The frame ``key``'s spinner shows now, ``cells`` wide, or None while it is still.

        For text a `text` source builds itself, so a status line turns in step
        with the rows it describes. Only meaningful during `overlay`.
        """
        if key is None:
            return None
        if key in self._always:
            return self._glyph(int(self.now() / FRAME_S), cells)
        live, count = self._live
        if key != live or self.now() - self._live_since < SPIN_DELAY_S:
            return None
        return self._glyph(self._step(key, count, self.now()), cells)

    # -- asked by the view ------------------------------------------------

    def animating(self, running=False):
        # type: (bool) -> bool
        """Whether the next frame can differ from this one.

        ``running`` is whether walks are in flight: a still ellipsis on screen
        may then be the next thing to turn.
        """
        spins, live, texts, loops, fading, smooth = self._found
        now = self.now()
        return bool(
            now < self._burst_until
            or live
            or texts
            or loops
            or smooth
            or now < fading
            or (running and spins)
        )

    def until_next(self):
        # type: () -> float
        """Seconds to the next frame boundary: sooner while a glow is moving."""
        if self.bursting():
            step = BURST_FRAME_S
        elif self._found[5]:
            step = SMOOTH_FRAME_S
        else:
            step = FRAME_S
        return max(0.005, step - (self.now() % step))

    def overlay(self, lines):
        # type: (Sequence[str]) -> List[str]
        """``lines`` with every span resolved for this moment, and no marker left."""
        now = self.now()
        live = (None, None)  # type: Tuple[Optional[str], Optional[int]]
        source = self._follow
        if source is not None:
            try:
                key, count = source()
                live = (key, count)
            except Exception:
                live = (None, None)
        if live[0] != self._live[0]:
            self._live_since = now
        self._live = live
        self._frames = {}
        found = [False, False, False, False, 0.0, False]  # type: List[Any]
        out = [self._resolve(line, now, found) if MARK in line else line for line in lines]
        self._found = found
        return out

    # -- resolution -------------------------------------------------------

    def _resolve(self, line, now, found):
        # type: (str, float, List[Any]) -> str
        out = []  # type: List[str]
        index = 0
        while True:
            at = line.find(MARK, index)
            if at < 0:
                out.append(line[index:])
                break
            out.append(line[index:at])
            end = line.find("z", at)
            if end < 0:
                break
            sequence = line[at : end + 1]
            index = end + 1
            if not sequence.startswith(_HEAD):
                continue  # a close with no open before it
            kind, ident = _parse(sequence)
            close = line.find(_CLOSE, index)
            content = strip_marks(line[index:] if close < 0 else line[index:close])
            index = len(line) if close < 0 else close + len(_CLOSE)
            banded = line.rfind(INVERSE, 0, at) > line.rfind(RESET, 0, at)
            try:
                out.append(self._span(kind, ident, content, now, found, banded))
            except Exception:
                # A span must never cost the frame: it is shown as it was drawn.
                out.append(content)
        return "".join(out)

    def _span(self, kind, ident, content, now, found, banded):
        # type: (int, int, str, float, List[Any], bool) -> str
        if kind == SPIN:
            return self._spun(ident, content, now, found, banded)
        if kind == FLASH:
            return self._flashed(ident, content, now, found)
        if kind == SHINE:
            return self._shone(ident, content, now, found)
        if kind == TEXT:
            return self._texted(ident, content, now, found)
        return content

    def _spun(self, ident, content, now, found, banded):
        # type: (int, str, float, List[Any], bool) -> str
        found[0] = True
        key = self._keys.get(ident)
        live, count = self._live
        if key in self._always:
            frame = int(now / FRAME_S)
        elif key is not None and key == live:
            # Live even while it waits to show, so its first frame is asked for.
            found[1] = True
            if now - self._live_since < SPIN_DELAY_S:
                return content
            frame = self._step(key, count, now)
        else:
            return content
        found[1] = True
        glyph = self._glyph(frame, width(content))
        if banded or not self.style.enabled:
            return glyph
        # The live spinner in the accent colour, so the one row that is moving
        # is also the one that stands out; the dim of the cell resumes after it.
        return self.style._sgr(_PALETTE["accent"]) + glyph

    def _step(self, key, count, now):
        # type: (str, Optional[int], float) -> int
        """The honest spinner: one step per frame, and only when the count moved."""
        cached = self._frames.get(key)
        if cached is not None:
            return cached
        state = self._spinners.get(key)
        if state is None:
            state = self._spinners[key] = [0, count, now]
        if count is None:
            frame = int(now / FRAME_S)  # nothing to be honest to
        else:
            # At most one step a frame of `FRAME_S`, however often frames
            # come: a glow elsewhere on screen asks for them twice as often.
            if count != state[1] and now - state[2] >= FRAME_S * 0.9:
                state[0] += 1
                state[1] = count
                state[2] = now
            frame = state[0]
        self._frames[key] = frame
        return frame

    def _glyph(self, frame, cells):
        # type: (int, int) -> str
        g = self.style.g
        frames = g.dots if cells == width(g.ellipsis) else g.spin
        return pad(truncate(frames[frame % len(frames)], cells, ""), cells)

    def _flashed(self, ident, content, now, found):
        # type: (int, str, float, List[Any]) -> str
        since = self._flashes.get(ident)
        if since is None:
            return content
        age = now - since
        if age < 0 or age >= FLASH_S:
            return content
        found[4] = max(found[4], since + FLASH_S)
        tone = self._flash_tone(age)
        return tone + plain(content) + RESET if tone else content

    def _flash_tone(self, age):
        # type: (float) -> str
        depth = self.style.depth
        if depth >= 24:
            # Eased out, in eighths: smooth to the eye, and a line rewritten
            # only when its colour has really moved.
            eased = 1.0 - (1.0 - min(1.0, age / FLASH_S)) ** 2
            return "\033[38;2;%d;%d;%dm" % _blend(
                _PALETTE["accent"][0], _PALETTE["info"][0], int(eased * 8) / 8.0
            )
        if depth >= 8:
            return "\033[38;5;%dm" % (_PALETTE["accent"][1],) if age < FLASH_S / 2 else ""
        if depth > 0:
            return "\033[1;%dm" % (_PALETTE["accent"][2],) if age < FLASH_S / 4 else ""
        return ""

    def _shone(self, ident, content, now, found):
        # type: (int, str, float, List[Any]) -> str
        entry = self._shines.get(ident)
        if entry is None:
            return content
        since, loop, track = entry
        cells = _cells(content)
        count = len(cells)
        if not count:
            return content
        half = SHEEN_WIDTH / 2.0
        if loop:
            found[3] = True
            travel = float(track or count) + SHEEN_WIDTH
            period = travel / SHEEN_CELLS_S + SHEEN_REST_S
            center = (now % period) * SHEEN_CELLS_S - half
            lift, tone = SHEEN_LIFT, _SHEEN_EDGE
        else:
            if since is None or not 0.0 <= now - since < SHINE_S:
                return content
            found[4] = max(found[4], since + SHINE_S)
            share = (now - since) / SHINE_S
            center = share * share * (3.0 - 2.0 * share) * (count + SHEEN_WIDTH) - half
            lift, tone = 0.7, _SHEEN_TITLE
        if center + half <= 0.0 or center - half >= count:
            # Resting, or past the part it lights: exactly as drawn, so the
            # line is not written again for a frame that shows nothing new.
            return content
        found[5] = True

        def glow(position, state):
            # type: (int, str) -> str
            distance = abs(position + 0.5 - center) / half
            if distance >= 1.0:
                return state
            amount = lift * (0.5 + 0.5 * math.cos(math.pi * distance))
            base = _rgb_of(state) or tone
            weight = "\033[1m" if "\033[1m" in state else ""
            return weight + "\033[38;2;%d;%d;%dm" % _blend(base, tone, amount)

        return _recolor(cells, glow)

    def _texted(self, ident, content, now, found):
        # type: (int, str, float, List[Any]) -> str
        source = self._texts.get(ident)
        if source is None:
            return content
        found[2] = True
        cells = width(content)
        text = source(cells, now)
        if text is None:
            return content
        return pad(truncate(text, cells, ""), cells)


# --------------------------------------------------------------------------
# the terminal around the view
# --------------------------------------------------------------------------


class Chrome(object):
    """The terminal around the view: its title, its tab's progress bar, a notification.

    Each is sent only to a terminal known to take it, because the same bytes
    mean different things elsewhere: OSC 9 is a progress bar to Windows
    Terminal and a desktop notification to iTerm2, and `4;1;60` is not a
    message anybody wants popped up once a second. Inside tmux or screen none
    of it is sent, since the outer terminal is not the one being addressed.
    Everything set is put back by `close`.
    """

    #: TERM prefixes of emulators that set their title from OSC 0.
    TITLE_TERMS = (
        "xterm",
        "rxvt",
        "alacritty",
        "foot",
        "wezterm",
        "putty",
        "konsole",
        "st-",
        "contour",
        "mintty",
    )

    _CLEAR = "\033]9;4;0;0\007"

    def __init__(self, write, env=None):
        # type: (Callable[[str], Any], Optional[Dict[str, str]]) -> None
        environ = os.environ if env is None else env
        term = str(environ.get("TERM", "")).lower()
        program = str(environ.get("TERM_PROGRAM", ""))
        nested = bool(environ.get("TMUX") or environ.get("STY")) or term.startswith(
            ("screen", "tmux")
        )
        ghostty = program.lower() == "ghostty" or term == "xterm-ghostty"
        self._write = write
        self.titles = not nested and term.startswith(self.TITLE_TERMS)
        self.bar = not nested and bool(
            environ.get("WT_SESSION") or environ.get("ConEmuPID") or ghostty
        )
        self.notice = None  # type: Optional[str]
        if not nested:
            if program in ("iTerm.app", "WezTerm") or environ.get("LC_TERMINAL") == "iTerm2":
                self.notice = "9"
            elif term == "xterm-kitty":
                self.notice = "99"
            elif ghostty or term.startswith("foot"):
                self.notice = "777"
        self.bell = bool(str(environ.get("DIRSCAPE_BELL") or "").strip())
        self._title = None  # type: Optional[str]
        self._pushed = False
        self._bar = None  # type: Optional[str]

    def _emit(self, sequence):
        # type: (str) -> None
        with contextlib.suppress(Exception):
            self._write(sequence)

    def title(self, text):
        # type: (str) -> None
        """The window's title, saved on the terminal's own stack the first time."""
        if not self.titles:
            return
        text = _printable(text, 120)
        if text == self._title:
            return
        prefix = "" if self._pushed else "\033[22;0t"
        self._pushed = True
        self._title = text
        self._emit(prefix + "\033]0;" + text + "\007")

    def progress(self, fraction=None, busy=False):
        # type: (Optional[float], bool) -> None
        """The tab's progress bar: a fraction, a busy bar with none, or cleared."""
        if not self.bar:
            return
        if fraction is not None:
            share = int(round(100 * max(0.0, min(1.0, float(fraction)))))
            sequence = "\033]9;4;1;%d\007" % (share,)
        elif busy:
            sequence = "\033]9;4;3;0\007"
        else:
            if self._bar in (None, self._CLEAR):
                return
            sequence = self._CLEAR
        if sequence != self._bar:
            self._bar = sequence
            self._emit(sequence)

    def notify(self, text):
        # type: (str) -> None
        """A desktop notification where the terminal takes one, else the bell if asked for."""
        text = _printable(text, 200).replace(";", ",")
        if self.notice == "9":
            self._emit("\033]9;%s\007" % (text,))
        elif self.notice == "99":
            self._emit("\033]99;;%s\033\\" % (text,))
        elif self.notice == "777":
            self._emit("\033]777;notify;dirscape;%s\033\\" % (text,))
        elif self.bell:
            self._emit("\a")

    def close(self):
        # type: () -> None
        """Put the terminal back: the bar cleared, and the saved title restored."""
        self.progress(None, busy=False)
        if self._pushed:
            self._pushed = False
            self._title = None
            self._emit("\033[23;0t")


# --------------------------------------------------------------------------
# the startup board
# --------------------------------------------------------------------------

#: The startup's phases, in the order the board lists them: each is ``(name,
#: the sweep stages it covers, the stage whose end begins it)``. `allocations`
#: has no place in the order, since it runs beside `quota` on its own thread.
PHASES = (
    ("mounts", ("config", "mounts", "plugins"), None),
    ("quota", ("quota",), "plugins"),
    ("allocations", ("allocations",), None),
    ("paths", ("discover", "attribute"), "allocations"),
    ("snapshots", ("snapshots",), "attribute"),
    ("sizes", ("quota-attach",), "snapshots"),
)


def _label(argv):
    # type: (Sequence[str]) -> str
    """A command as the board names it: its program, and a subcommand word."""
    if not argv:
        return "?"
    name = os.path.basename(str(argv[0])) or str(argv[0])
    if len(argv) > 1:
        word = str(argv[1])
        if word.isalpha() and word.islower() and len(word) <= 12:
            return name + " " + word
    return name


def _seconds(value):
    # type: (float) -> str
    value = max(0.0, value)
    if value < 60:
        return "%.1fs" % (value,)
    return "%dm%02ds" % (int(value // 60), int(value % 60))


class Quiet(object):
    """cbreak with no echo, for a board on stderr, without writing to stdout.

    `interactive.raw_session` hides the cursor on stdout, which is right for
    the browser and wrong here: stdout may be a file, and an escape sequence
    in the middle of `ds paths --json > out.json` is corruption.

    **Set with `TCSANOW`, not `tty`'s default `TCSAFLUSH`**, which discards
    whatever is waiting to be read: the next command a reader typed ahead
    while this one started belongs to their shell, and restoring the mode
    afterwards hands it back there intact.
    """

    def __init__(self, stream):
        # type: (Any) -> None
        self._stream = stream
        self._saved = None  # type: Any
        self._fd = -1

    def __enter__(self):
        # type: () -> Quiet
        try:
            import termios
            import tty

            self._fd = sys.stdin.fileno()
            self._saved = termios.tcgetattr(self._fd)
            tty.setcbreak(self._fd, termios.TCSANOW)
        except Exception:
            self._saved = None
        return self

    def __exit__(self, *exc):
        # type: (*Any) -> None
        if self._saved is not None:
            try:
                import termios

                termios.tcsetattr(self._fd, termios.TCSADRAIN, self._saved)
            except Exception:
                pass


class Board(object):
    """The startup's live board, and the observer `cli.sweep` reports to.

    The sweep calls `stage` at each stage's end, `claim` from a thread whose
    commands belong to one phase, and the runner calls `started` and
    `finished` around every command. A daemon thread draws: nothing for
    `BOARD_DELAY_S`, so a fast start stays exactly as it was, then a frame
    every `FRAME_S` until `close`.

    Two shapes. Given a ``screen``, a framed board on stdout, the width of the
    table that replaces it, which draws its first frame over this one. Without
    one, a single line on ``write``, stderr, for a command that prints its
    answer and exits.
    """

    def __init__(
        self,
        style,  # type: Any
        write,  # type: Callable[[str], Any]
        screen=None,  # type: Any
        allowance=20.0,  # type: float
        chrome=None,  # type: Optional[Chrome]
        clock=None,  # type: Optional[Callable[[], float]]
        delay=BOARD_DELAY_S,  # type: float
        rows=0,  # type: int
        columns=0,  # type: int
        quiet=None,  # type: Any
    ):
        # type: (...) -> None
        self.style = style
        self.screen = screen
        self.allowance = allowance
        self.chrome = chrome
        self.clock = clock if clock is not None else time.monotonic
        self.delay = delay
        self.rows = rows
        self.columns = columns
        self.shown = False
        self._write = write
        self._quiet = quiet
        self._lock = threading.Lock()
        self._began = self.clock()
        self._marks = {}  # type: Dict[str, float]
        self._notes = {}  # type: Dict[str, str]
        self._claims = {}  # type: Dict[int, str]
        self._spans = {}  # type: Dict[str, List[Optional[float]]]
        #: [phase, label, started, ended, outcome]
        self._commands = []  # type: List[List[Any]]
        self._stop = threading.Event()
        self._thread = None  # type: Optional[threading.Thread]
        self._closed = False
        self._line = False

    # -- the observer -----------------------------------------------------

    def stage(self, label):
        # type: (str) -> None
        with self._lock:
            self._marks[label] = self.clock()

    def note(self, phase, text):
        # type: (str, str) -> None
        with self._lock:
            self._notes[phase] = text

    def claim(self, phase):
        # type: (str) -> None
        """The calling thread's commands belong to ``phase`` until `release`."""
        with self._lock:
            self._claims[threading.current_thread().ident or 0] = phase
            self._spans.setdefault(phase, [self.clock(), None])

    def release(self, phase):
        # type: (str) -> None
        with self._lock:
            self._claims.pop(threading.current_thread().ident or 0, None)
            span = self._spans.setdefault(phase, [self._began, None])
            span[1] = self.clock()

    def started(self, argv):
        # type: (Sequence[str]) -> List[Any]
        with self._lock:
            phase = self._claims.get(threading.current_thread().ident or 0)
            if phase is None:
                phase = self._current()
            record = [phase, _label(argv), self.clock(), None, ""]
            self._commands.append(record)
            return record

    def finished(self, record, completed):
        # type: (List[Any], Any) -> None
        with self._lock:
            record[3] = self.clock()
            if completed is None:
                record[4] = "failed"
            elif getattr(completed, "not_found", False):
                record[4] = "missing"
            elif getattr(completed, "timed_out", False):
                record[4] = "timed out"
            elif getattr(completed, "returncode", 0) not in (0, None):
                record[4] = "failed"

    # -- phases -----------------------------------------------------------

    def _current(self):
        # type: () -> str
        """The phase the sweep's own thread is in. Under the lock."""
        for name, stages, _after in PHASES:
            if name != "allocations" and not all(s in self._marks for s in stages):
                return name
        return PHASES[-1][0]

    def _phase(self, name, stages, after, now):
        # type: (str, Sequence[str], Optional[str], float) -> Tuple[str, float]
        """``(state, seconds)`` of one phase: `done`, `running` or `waiting`. Under the lock."""
        if name == "allocations":
            span = self._spans.get(name)
            end = self._marks.get("allocations")
            if span is None:
                return ("done", 0.0) if end is not None else ("waiting", 0.0)
            start = span[0] if span[0] is not None else self._began
            stop = span[1] if span[1] is not None else end
            if stop is not None:
                return "done", stop - start
            return "running", now - start
        began = self._began if after is None else self._marks.get(after)
        if all(s in self._marks for s in stages):
            return "done", max(self._marks[s] for s in stages) - (began or self._began)
        if began is not None and name == self._current():
            return "running", now - began
        return "waiting", 0.0

    def _detail(self, name):
        # type: (str) -> str
        """What a phase ran: its note, then its commands grouped by program. Under the lock."""
        counts = {}  # type: Dict[str, List[int]]
        order = []  # type: List[str]
        for phase, label, _start, _end, outcome in self._commands:
            if phase != name or outcome == "missing":
                continue
            if label not in counts:
                counts[label] = [0, 0]
                order.append(label)
            counts[label][0] += 1
            # Only a command that never answered is news. A non-zero exit IS
            # an answer, and a routine one: `mmlsattr -L` says 1 of every path
            # that is not a fileset, which is most of the paths it is asked.
            if outcome == "timed out":
                counts[label][1] += 1
        parts = [self._notes[name]] if self._notes.get(name) else []
        g = self.style.g
        for label in order:
            total, bad = counts[label]
            text = label + (" %s%d" % (g.times, total) if total > 1 else "")
            if bad:
                text += " (%d did not answer)" % (bad,)
            parts.append(text)
        return (" %s " % (g.sep,)).join(parts)

    def _slowest(self, now):
        # type: (float) -> Optional[Tuple[str, float]]
        """The command in flight longest, or the running phase. Under the lock."""
        waiting = [
            (now - start, label) for _p, label, start, end, _o in self._commands if end is None
        ]
        if waiting:
            spent, label = max(waiting)
            return label, spent
        for name, stages, after in PHASES:
            state, spent = self._phase(name, stages, after, now)
            if state == "running":
                return name, spent
        return None

    # -- drawing ----------------------------------------------------------

    def _mark(self, state, now):
        # type: (str, float) -> str
        style = self.style
        if state == "done":
            return style.ok(style.g.ok)
        if state == "running":
            return style.accent(style.g.spin[int(now / FRAME_S) % len(style.g.spin)])
        return style.dim(style.g.sep)

    def lines(self, now=None):
        # type: (Optional[float]) -> List[str]
        """The board's content lines, unframed."""
        style = self.style
        now = self.clock() if now is None else now
        inner = max(24, (self.columns or style.size) - 4)
        with self._lock:
            phases = []
            for name, stages, after in PHASES:
                state, spent = self._phase(name, stages, after, now)
                if name == "allocations" and state == "done" and not self._detail(name):
                    continue  # no allocations plugin here: a line that says nothing
                phases.append((name, state, spent, self._detail(name)))
            slow = self._slowest(now) if now - self._began >= SLOW_S else None
        elapsed = _seconds(now - self._began)
        head = style.accent(style.g.spin[int(now / FRAME_S) % len(style.g.spin)])
        head += " " + style.head("reading the cluster")
        out = [pad(head, inner - len(elapsed)) + style.dim(elapsed), ""]
        for name, state, seconds, detail in phases:
            clock = _seconds(seconds) if state != "waiting" else ""
            room = max(0, inner - 18 - len(clock) - 2)
            body = "  %s %s  " % (self._mark(state, now), pad(style.text(name), 12))
            body += style.dim(truncate(detail, room, style.g.ellipsis)) if detail else ""
            out.append(pad(body, inner - len(clock)) + style.dim(clock))
        if slow is not None:
            label, seconds = slow
            out.append("")
            out.append(
                style.dim(
                    "  slowest: %s, %s of the %gs allowance"
                    % (label, _seconds(seconds), self.allowance)
                )
            )
        return out

    def line(self, now=None):
        # type: (Optional[float]) -> str
        """The board as one line, for stderr."""
        style = self.style
        now = self.clock() if now is None else now
        with self._lock:
            current = self._current()
            detail = self._detail(current)
            slow = self._slowest(now) if now - self._began >= SLOW_S else None
        glyph = style.g.spin[int(now / FRAME_S) % len(style.g.spin)]
        text = "%s reading the cluster: %s" % (glyph, current)
        if detail:
            text += " (%s)" % (detail,)
        text += " %s %s" % (style.g.sep, _seconds(now - self._began))
        if slow is not None:
            text += ", slowest %s %s of %gs" % (slow[0], _seconds(slow[1]), self.allowance)
        room = max(10, (self.columns or style.size) - 1)
        return style.dim(truncate(text, room, style.g.ellipsis))

    def _draw(self):
        # type: () -> None
        now = self.clock()
        if self.screen is not None:
            from .render.style import panel

            framed = panel(
                self.lines(now), style=self.style, size=self.columns or None, shrink=False
            )
            block = framed.splitlines()
            if self.rows and len(block) + 1 > self.rows:
                return  # a board taller than the window cannot be drawn in place
            self.screen.paint(block)
        else:
            self._write("\r" + self.line(now) + "\033[K")
            self._line = True
        if not self.shown:
            self.shown = True
            if self.chrome is not None:
                self.chrome.title("ds %s reading the cluster" % (self.style.g.sep,))
                self.chrome.progress(None, busy=True)

    def _loop(self):
        # type: () -> None
        try:
            if self._stop.wait(self.delay):
                return
            while True:
                self._draw()
                if self._stop.wait(max(0.005, FRAME_S - (self.clock() % FRAME_S))):
                    return
        except Exception:
            # A board that cannot draw costs its frames, never the run.
            return

    # -- lifecycle --------------------------------------------------------

    def start(self):
        # type: () -> Board
        if self._quiet is not None:
            try:
                self._quiet.__enter__()
            except Exception:
                self._quiet = None
        self._thread = threading.Thread(target=self._loop, name="dirscape-board")
        self._thread.daemon = True
        self._thread.start()
        return self

    def close(self, keep=False):
        # type: (bool) -> None
        """Stop drawing, and take the board off the screen unless ``keep``.

        ``keep`` is for the browser, whose first frame is drawn over the board
        on the same `Screen`, so nothing blank ever stands between the two.
        """
        if self._closed:
            return
        self._closed = True
        self._stop.set()
        if self._thread is not None:
            self._thread.join(1.0)
        try:
            if self.shown and not keep:
                if self.screen is not None:
                    self.screen.erase()
                elif self._line:
                    self._write("\r\033[K")
            if self.shown and self.chrome is not None:
                self.chrome.progress(None, busy=False)
        finally:
            if self._quiet is not None:
                with contextlib.suppress(Exception):
                    self._quiet.__exit__(None, None, None)
