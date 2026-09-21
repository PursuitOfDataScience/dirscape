"""Terminal primitives: width, glyphs, colour, tables and meters.

Hand rolled against the standard library, because a tool whose best hour is the
first hour on an unfamiliar cluster cannot require anything to be installed.
That is the same argument `pyproject.toml` makes for the 3.6 floor, so there is
no `rich`, no `textual`, and nothing imported from a sibling package either.

Three things here are correctness rather than decoration, and all three are
lessons from `nodetop`:

* **Display width is not string length.** Padding a cell with ``len()`` breaks
  alignment for any wide character and for the escapes we emit ourselves, so
  :func:`width` measures what the terminal will actually show.
* **Not every terminal speaks UTF-8.** Every glyph has an ASCII twin, listed
  beside it in :data:`_GLYPHS` so a missing twin is visible on the page, and
  the set is chosen from the real stdout encoding or from ``--ascii`` /
  ``DIRSCAPE_ASCII``.
* **Colour is a spectrum, not a boolean.** Truecolor, 256 colour and 16 colour
  terminals all exist, and `NO_COLOR` or ``TERM=dumb`` means none of them.

One rule is this package's own. **Colour is never load bearing.** Every state
this tool can report is distinguishable with colour off and with ASCII glyphs,
because the whole product is the difference between yes, no and "I could not
determine", and a reader piping to a file must still see all three.
"""

import os
import shutil
import sys
import unicodedata
from typing import Dict, List, Optional, Sequence, Tuple

__all__ = [
    "MIN_WIDTH",
    "FALLBACK_WIDTH",
    "MAX_WIDTH",
    "Glyphs",
    "Style",
    "resolve_style",
    "width",
    "plain",
    "pad",
    "truncate",
    "term_width",
    "table",
    "panel",
    "rule",
    "legend",
    "wrap",
]


#: Below this no view can lay out at all, so it is the floor every caller
#: clamps to rather than a width anything is designed for.
MIN_WIDTH = 32

#: What to assume when there is no terminal to ask: a pipe, a file, a CI log.
FALLBACK_WIDTH = 100

#: A terminal claiming more than this is a terminal lying, and every cell of
#: every table is padded to whatever we believe.
MAX_WIDTH = 200


# --------------------------------------------------------------------------
# width
# --------------------------------------------------------------------------


def _strip_ansi(text):
    # type: (str) -> str
    """Drop CSI escapes so a coloured cell measures like an uncoloured one."""
    # The common case is a cell with no escape at all, and `in` on a string is
    # a C level scan where the loop below is a Python one per character.
    if "\033" not in text:
        return text
    out = []  # type: List[str]
    i = 0
    while i < len(text):
        if text[i] == "\033":
            j = i + 1
            if j < len(text) and text[j] == "[":
                j += 1
                while j < len(text) and not (0x40 <= ord(text[j]) <= 0x7E):
                    j += 1
            i = j + 1
            continue
        out.append(text[i])
        i += 1
    return "".join(out)


def width(text):
    # type: (str) -> int
    """Columns this string occupies in a terminal.

    Ignores ANSI escapes, counts East Asian wide and fullwidth characters as
    two columns, and combining marks as none. Using ``len()`` instead is what
    makes a coloured or non-Latin table drift.
    """
    if not text:
        return 0
    stripped = _strip_ansi(text)
    try:
        # ASCII cannot be wide and cannot combine, so its display width IS its
        # length. `str.isascii()` would be the obvious test and it arrived in
        # 3.7, which this package cannot use; `encode` is the 3.6 spelling and
        # is also a C level scan.
        stripped.encode("ascii")
    except UnicodeEncodeError:
        pass
    else:
        return len(stripped)
    total = 0
    for ch in stripped:
        if unicodedata.combining(ch):
            continue
        total += 2 if unicodedata.east_asian_width(ch) in ("W", "F") else 1
    return total


def plain(text):
    # type: (str) -> str
    """The text with every escape removed, exactly what :func:`width` measures.

    Public because two callers outside this module need the STRING and not
    just its width. `interactive.highlight` paints a selected row as one
    uniform inverse band, and any surviving foreground code inside that band
    becomes the BACKGROUND under inverse video: measured in a pty, the usage
    bar's three coloured segments painted three differently coloured blocks
    and the band appeared to stop at the bar. `cli._browse` locates a row by
    its path, which no longer survives as a substring once the path cell
    dims its parent directories.
    """
    return _strip_ansi(text)


def pad(text, size, align="left", have=None):
    # type: (str, int, str, Optional[int]) -> str
    """Pad to ``size`` display columns. ``have`` is the width, if already known."""
    gap = max(0, size - (width(text) if have is None else have))
    if align == "right":
        return " " * gap + text
    if align == "center":
        left = gap // 2
        return " " * left + text + " " * (gap - left)
    return text + " " * gap


def truncate(text, limit, ellipsis="..."):
    # type: (str, int, str) -> str
    """Cut to ``limit`` display columns, keeping any ANSI styling intact.

    Never called on a path. A shortened path is a DIFFERENT path rather than a
    smaller version of the same one, and the reader cannot tell which
    characters went, so the views mark path columns atomic and degrade by
    dropping or stacking instead. See `atlas` for the order.
    """
    if limit <= 0:
        return ""
    if width(text) <= limit:
        return text
    keep = max(0, limit - width(ellipsis))
    out = []  # type: List[str]
    used = 0
    styled = False
    i = 0
    while i < len(text):
        ch = text[i]
        if ch == "\033":
            j = i + 1
            if j < len(text) and text[j] == "[":
                j += 1
                while j < len(text) and not (0x40 <= ord(text[j]) <= 0x7E):
                    j += 1
            out.append(text[i : j + 1])
            styled = True
            i = j + 1
            continue
        if unicodedata.combining(ch):
            step = 0
        else:
            step = 2 if unicodedata.east_asian_width(ch) in ("W", "F") else 1
        if used + step > keep:
            break
        out.append(ch)
        used += step
        i += 1
    # A cut inside a styled run would bleed its colour into the rest of the
    # line, so the reset is re-armed.
    return "".join(out) + ellipsis + ("\033[0m" if styled else "")


def term_width(default=FALLBACK_WIDTH, cap=MAX_WIDTH):
    # type: (int, int) -> int
    """Usable columns, clamped to a range a layout can work in.

    The only environment read for LAYOUT rather than for data. Nothing in this
    package reads the clock, the hostname or the filesystem to fill a cell: a
    field the caller did not supply is rendered as the unknown mark, which is
    the same rule the rest of the tool lives by applied to its own inputs.
    """
    try:
        columns = shutil.get_terminal_size((default, 24)).columns
    except Exception:  # pragma: no cover - a hostile COLUMNS value
        columns = default
    return max(MIN_WIDTH, min(columns or default, cap))


# --------------------------------------------------------------------------
# glyphs
# --------------------------------------------------------------------------

#: ``(attribute, unicode, ascii)``, one line per glyph.
#:
#: The twin sits beside the glyph deliberately: a new glyph with no ASCII form
#: is then visible on the page rather than discovered by a user on a `LANG=C`
#: console, and `tests/test_render_style.py` asserts the third column is pure
#: ASCII for every row.
#:
#: Each name means ONE thing. `trough`, `doubt` and `tile` are three different
#: shades because they answer three different questions (room left, space the
#: backend has not accounted for, area on the treemap), and reusing one for
#: another is how a reader learns to distrust all three.
_GLYPHS = (
    ("h", "─", "-"),
    ("v", "│", "|"),
    ("tl", "╭", "+"),
    ("tr", "╮", "+"),
    ("bl", "╰", "+"),
    ("br", "╯", "+"),
    ("branch", "├─", "|-"),
    ("last", "╰─", "`-"),
    ("pipe", "│ ", "| "),
    # `ok` and `bad` match `Verdict.glyph(ascii_only=True)`, which answers "y"
    # and "n". The model owns that vocabulary and this set agrees with it
    # rather than inventing a second one.
    ("ok", "✓", "y"),
    ("bad", "✗", "n"),
    # The unknown mark is the SAME character in both sets, on purpose. It is
    # the one thing a user greps for, and a mark that changed shape with the
    # terminal would make "grep for the question marks" wrong half the time.
    ("unknown", "?", "?"),
    ("warn", "▲", "!"),
    ("arrow", "→", "->"),
    ("sep", "·", "-"),
    ("ellipsis", "…", "..."),
    ("bullet", "⏺", "*"),
    # Space the backend says is allocated but has not accounted for
    # (`blockInDoubt`), marked on the used figure itself. There used to be a
    # matching shade inside a usage bar so the picture and the note agreed;
    # the bar is gone (see `fields._figure_cell`) and the marker is the whole
    # of that fact now.
    ("doubt", "▒", "+"),
    # A treemap cell's fill.
    ("tile", "▓", "="),
)


class Glyphs(object):
    """The character set to draw with. Two complete sets, never mixed."""

    __slots__ = tuple(name for name, _, _ in _GLYPHS) + ("unicode",)

    def __init__(self, unicode_ok=True):
        # type: (bool) -> None
        for name, rich, plain in _GLYPHS:
            setattr(self, name, rich if unicode_ok else plain)
        self.unicode = unicode_ok

    @classmethod
    def ascii(cls):
        # type: () -> "Glyphs"
        return cls(False)

    @classmethod
    def detect(cls, stream=None, ascii_only=None, env=None):
        # type: (object, Optional[bool], Optional[Dict[str, str]]) -> "Glyphs"
        """Unicode when the caller allows it and stdout can encode it.

        ``ascii_only`` of None means "decide for me": `DIRSCAPE_ASCII` first,
        because a user who exported it did so for every invocation, then the
        stream's own encoding. `--ascii` is the one flag present in all five
        sibling packages, so the flag and the variable both have to work.
        """
        environ = os.environ if env is None else env
        if ascii_only:
            return cls.ascii()
        if ascii_only is None and str(environ.get("DIRSCAPE_ASCII") or "").strip():
            return cls.ascii()
        target = sys.stdout if stream is None else stream
        encoding = str(getattr(target, "encoding", None) or "").lower()
        if "utf" in encoding:
            return cls(True)
        try:
            # A terminal that cannot encode the glyph would raise or print
            # replacement characters, and ASCII is better than either.
            "─✓█".encode(encoding or "ascii")
        except (LookupError, UnicodeEncodeError):
            return cls.ascii()
        return cls(True)


# --------------------------------------------------------------------------
# colour
# --------------------------------------------------------------------------

#: ``(truecolor rgb, 256 colour index, 16 colour SGR)`` per semantic role.
#:
#: A role is a category, so it is told apart by hue; a magnitude is an order,
#: so :data:`_RAMP` is told apart by lightness. Mixing the two is what makes a
#: table unreadable: a green verdict dot beside a green quantity reads as one
#: thing said twice. The values are `nodetop`'s, which were measured against a
#: dark terminal for WCAG AA contrast and for dE2000 separation between the
#: verdict trio; copied rather than imported, since these packages share no
#: code by design.
_PALETTE = {
    "accent": ((242, 132, 200), 212, 95),
    "ok": ((114, 218, 104), 77, 92),
    "warn": ((236, 151, 0), 172, 33),
    "bad": ((233, 82, 60), 196, 91),
    "info": ((144, 166, 247), 111, 94),
    "dim": ((119, 124, 131), 244, 90),
    "text": ((217, 219, 221), 253, 97),
    "muted": ((163, 168, 174), 248, 37),
    "track": ((55, 62, 69), 237, 90),
}  # type: Dict[str, Tuple[Tuple[int, int, int], int, int]]

#: Four steps of increasing lightness for "how full is this", used below the
#: warning threshold. Monotonic in lightness so the eye reads it as a quantity,
#: and it stops well short of green, amber and red, which carry verdicts here.
_RAMP = (
    ((90, 140, 220), 68, 94),
    ((86, 170, 222), 74, 96),
    ((84, 196, 214), 80, 96),
    ((96, 214, 200), 86, 96),
)  # type: Tuple[Tuple[Tuple[int, int, int], int, int], ...]

#: At and above this share of the enforced limit the bar stops being a
#: quantity and becomes a warning.
#:
#: Deliberately unlike `nodetop`, which refuses to threshold its meters because
#: "40% of GPUs are free" is a warning about nothing. A quota limit is not a
#: preference: the filesystem enforces it, and a write fails at 100%. So the
#: threshold is the filesystem's, not the tool's.
WARN_FRACTION = 0.9


def _depth(env=None, stream=None):
    # type: (Optional[Dict[str, str]], object) -> int
    """0 = no colour, 4 = 16 colour, 8 = 256 colour, 24 = truecolor."""
    environ = os.environ if env is None else env
    if environ.get("NO_COLOR"):
        return 0
    term = environ.get("TERM", "")
    if term == "dumb":
        return 0
    target = sys.stdout if stream is None else stream
    isatty = getattr(target, "isatty", None)
    if not (isatty and isatty()):
        return 0
    if str(environ.get("COLORTERM", "")).lower() in ("truecolor", "24bit"):
        return 24
    if "256color" in term or "direct" in term:
        return 8
    return 4 if term else 0


class Style(object):
    """Semantic colour and glyph access, degrading cleanly.

    Call sites ask for meaning (``st.ok``, ``st.bad``) rather than for a
    colour, so dropping to a 16 colour terminal or to no colour at all does not
    ripple outward.
    """

    __slots__ = ("depth", "g", "enabled", "size", "_escapes")

    def __init__(self, color=False, glyphs=None, size=None, depth=8):
        # type: (bool, Optional[Glyphs], Optional[int], int) -> None
        self.depth = depth if color else 0
        self.enabled = self.depth > 0
        self.g = glyphs if glyphs is not None else Glyphs.detect()
        self.size = size if size else term_width()
        self._escapes = {}  # type: Dict[Tuple[object, ...], str]

    # -- primitives -------------------------------------------------------

    def _sgr(self, tone):
        # type: (Tuple[Tuple[int, int, int], int, int]) -> str
        hit = self._escapes.get(tone)
        if hit is None:
            rgb, c256, c16 = tone
            if self.depth >= 24:
                hit = "\033[38;2;%d;%d;%dm" % rgb
            elif self.depth >= 8:
                hit = "\033[38;5;%dm" % (c256,)
            else:
                hit = "\033[%dm" % (c16,)
            self._escapes[tone] = hit
        return hit

    def paint(self, role, text, bold=False):
        # type: (str, str, bool) -> str
        if not self.enabled or not text or role not in _PALETTE:
            return text
        prefix = ("\033[1m" if bold else "") + self._sgr(_PALETTE[role])
        return prefix + text + "\033[0m"

    def tint(self, text, fraction):
        # type: (str, Optional[float]) -> str
        """Paint by how full something is. An unmeasured share gets NO colour.

        The colourless branch is part of the honest-unknown rule: a hue on the
        unknown mark would imply a reading behind it, and there is none.

        **Bold above `WARN_FRACTION`, and only there.** With the usage bar
        gone from the table the graded colour is the only picture of fullness
        left, and four ramp steps of increasing lightness are a gentle
        gradient: a reader scanning ten rows for the one that is about to
        stop their writes needs the crossing of the filesystem's own
        threshold to be a step change rather than one more shade. Weight is
        the axis that is still free, since hue is spoken for by the ramp and
        by the verdict trio.
        """
        role = self.role_for(fraction)
        if not role:
            return text
        if role.startswith("ramp"):
            if not self.enabled or not text:
                return text
            return self._sgr(_RAMP[int(role[4:])]) + text + "\033[0m"
        return self.paint(role, text, bold=True)

    @staticmethod
    def role_for(fraction):
        # type: (Optional[float]) -> str
        """Which semantic role a share of a limit earns, "" for unmeasured."""
        if fraction is None:
            return ""
        if fraction >= 1.0:
            return "bad"
        if fraction >= WARN_FRACTION:
            return "warn"
        step = int(max(0.0, fraction) / WARN_FRACTION * len(_RAMP))
        return "ramp%d" % (min(len(_RAMP) - 1, step),)

    # -- semantic roles ---------------------------------------------------

    def ok(self, text):
        # type: (str) -> str
        return self.paint("ok", text)

    def bad(self, text):
        # type: (str) -> str
        return self.paint("bad", text)

    def warn(self, text):
        # type: (str) -> str
        return self.paint("warn", text)

    def info(self, text):
        # type: (str) -> str
        return self.paint("info", text)

    def dim(self, text):
        # type: (str) -> str
        return self.paint("dim", text)

    def muted(self, text):
        # type: (str) -> str
        return self.paint("muted", text)

    def track(self, text):
        # type: (str) -> str
        return self.paint("track", text)

    def accent(self, text):
        # type: (str) -> str
        return self.paint("accent", text)

    def head(self, text):
        # type: (str) -> str
        return self.paint("text", text, bold=True)

    def column(self, text):
        # type: (str) -> str
        """A column HEADING, which is a label rather than a finding.

        Deliberately not `head`. Bold white on every heading of every table
        made the labels the brightest thing on screen, competing with the
        figures underneath them for the one row a reader is actually hunting
        for. `muted` keeps them legible and puts them behind the numbers,
        which leaves three weights in the table instead of two: findings at
        full brightness, headings muted, the rule under them dim.
        """
        return self.paint("muted", text, bold=True)


def resolve_style(color="auto", ascii_only=None, stream=None, size=None, env=None):
    # type: (str, Optional[bool], object, Optional[int], Optional[Dict[str, str]]) -> Style
    """Decide colour and glyphs for one invocation.

    ``color`` is ``auto`` / ``always`` / ``never``, matching every sibling
    package's ``--color`` flag.

    **`NO_COLOR` and `TERM=dumb` win over ``always``.** The variable is the
    user's standing instruction about their own terminal, and a flag that
    overrode it would make `NO_COLOR` advice rather than a setting. `nodetop`
    resolves the precedence the same way, so a reader who learns it once knows
    it for both.
    """
    environ = os.environ if env is None else env
    depth = _depth(environ, stream)
    if color == "never":
        depth = 0
    elif color == "always" and depth == 0:
        # `always` cannot revive a depth of zero that NO_COLOR or TERM=dumb
        # asked for, and there is no terminal to interrogate when output is a
        # pipe, so 256 colours is the safe middle of the ladder.
        if not environ.get("NO_COLOR") and environ.get("TERM", "") != "dumb":
            depth = 8
    return Style(
        color=depth > 0,
        depth=depth or 8,
        glyphs=Glyphs.detect(stream=stream, ascii_only=ascii_only, env=environ),
        size=size,
    )


# --------------------------------------------------------------------------
# tables
# --------------------------------------------------------------------------


def table(
    headers,  # type: Sequence[str]
    rows,  # type: Sequence[Sequence[object]]
    aligns=None,  # type: Optional[Sequence[str]]
    style=None,  # type: Optional[Style]
    size=None,  # type: Optional[int]
    indent="",  # type: str
    keep=(),  # type: Sequence[int]
    priority=(),  # type: Sequence[int]
    atomic=(),  # type: Sequence[int]
    drop_empty=True,  # type: bool
    gutter="  ",  # type: str
    underline=True,  # type: bool
    spread=False,  # type: bool
):
    # type: (...) -> Tuple[str, List[str]]
    """An aligned table, measured in display columns, fitted by DROPPING.

    Returns ``(text, dropped_headers)`` so the caller can say what went, in its
    own words. A table that silently loses columns reads as a table that never
    had them.

    ``priority`` is the order columns are given up in, by index, and it is the
    view's policy rather than this function's: see `atlas` for the documented
    order and the reason behind each place in it. ``keep`` indices are never
    dropped, ``atomic`` indices never shrink and never truncate. A path column
    is always both, because an ellipsis inside a path produces a different path
    rather than a shorter one.

    ``gutter`` is the run of spaces between columns. Two is tight enough that
    a right-aligned figure sits almost against the cell on its left, which is
    what made the atlas read as one dense block instead of as columns; the
    atlas asks for four. ``underline`` draws the dashed rule under the
    headings, and a view that rules ABOVE its headings instead turns it off
    rather than getting two rules.

    ``spread`` widens the gutter before the LAST column by whatever room is
    left over, so the table reaches the full window instead of ending wherever
    its content happens to. One gutter and not all of them, which is the part
    that took two attempts to get right: dividing the slack evenly across
    every gutter put 20 spaces between `role` and `path` at a 120 column
    terminal, and a reader cannot track a row across that. Spent on the last
    gutter alone it reads as one left group and one right column, which is
    what a dashboard does with a metric, and the figures end up flush against
    the frame where the eye can compare them down the column.
    """
    style = style or Style()
    window = size if size else style.size
    cells = [[("" if c is None else str(c)) for c in row] for row in rows]
    heads = [str(h) for h in headers]
    ncol = len(heads)
    align = list(aligns or ["left"] * ncol)
    align += ["left"] * (ncol - len(align))
    live = list(range(ncol))
    dropped = []  # type: List[str]

    if drop_empty:
        # A column blank on every row carries nothing, so it is removed before
        # the width driven drops and hands its room to a column that does.
        # The unknown mark is CONTENT and is never blank, which is the whole
        # point of printing it.
        for index in reversed([i for i in live if i not in keep]):
            if not any(_strip_ansi(row[index]).strip() for row in cells if index < len(row)):
                live.remove(index)

    def sizes_for(columns):
        # type: (Sequence[int]) -> List[int]
        out = []
        for index in columns:
            widest = width(heads[index])
            for row in cells:
                if index < len(row):
                    widest = max(widest, width(row[index]))
            out.append(widest)
        return out

    def total(columns):
        # type: (Sequence[int]) -> int
        if not columns:
            return 0
        return sum(sizes_for(columns)) + width(gutter) * (len(columns) - 1) + width(indent)

    for index in priority:
        if total(live) <= window:
            break
        if index in keep or index not in live:
            continue
        live.remove(index)
        dropped.append(heads[index])

    widths = sizes_for(live)
    # Whatever still does not fit is taken from the widest column that is
    # allowed to give, down to a floor that keeps its header legible.
    floors = []
    for offset, index in enumerate(live):
        if index in atomic:
            floors.append(widths[offset])
        else:
            floors.append(min(width(heads[index]), 6) or 3)
    guard = 0
    available = window - width(indent) - width(gutter) * max(0, len(live) - 1)
    while sum(widths) > available and guard < 4096:
        slack = [widths[i] - floors[i] for i in range(len(live))]
        if max(slack) <= 0:
            break
        widths[slack.index(max(slack))] -= 1
        guard += 1

    # One gutter string per join position, so `spread` can widen exactly one
    # of them. Equal to `[gutter] * n` unless it does.
    gutters = [gutter] * max(0, len(live) - 1)
    if spread and gutters:
        room = window - (sum(widths) + width(gutter) * (len(live) - 1) + width(indent))
        if room > 0:
            gutters[-1] = gutter + " " * room

    def join(pieces):
        # type: (Sequence[str]) -> str
        out = pieces[0] if pieces else ""
        for offset in range(1, len(pieces)):
            out += gutters[offset - 1] + pieces[offset]
        return out

    lines = []
    head_cells = []
    for offset, index in enumerate(live):
        text = heads[index]
        if index not in atomic and width(text) > widths[offset]:
            text = truncate(text, widths[offset], style.g.ellipsis)
        head_cells.append(style.column(pad(text, widths[offset], align[index])))
    lines.append((indent + join(head_cells)).rstrip())
    if underline:
        lines.append(indent + join([style.dim(style.g.h * widths[i]) for i in range(len(live))]))
    for row in cells:
        out = []
        for offset, index in enumerate(live):
            text = row[index] if index < len(row) else ""
            if index not in atomic and width(text) > widths[offset]:
                text = truncate(text, widths[offset], style.g.ellipsis)
            out.append(pad(text, widths[offset], align[index]))
        lines.append((indent + join(out)).rstrip())
    return "\n".join(lines), dropped


# --------------------------------------------------------------------------
# frames
# --------------------------------------------------------------------------

#: The frame gradient, as anchor colours to interpolate between. Every one of
#: them is a LIGHT colour, and that is the whole point.
#:
#: A frame that sweeps light to deep puts the highlight at the top left like
#: gloss on a card, and puts the darkest end of the ramp at the bottom right,
#: where on a dark terminal it simply disappears. A gradient whose range leaves
#: the visible band is not a gradient with a subtle end, it is one that is
#: broken for half its length. So the sweep moves in HUE and stays put in
#: brightness.
#:
#: Periwinkle to lilac to light orchid, which is at least dE2000 21 from every
#: step of :data:`_RAMP` and from `ok`, `warn` and `bad`. It deliberately
#: avoids the cyans: the ramp lives there, and a cyan frame around a cyan
#: column is chrome competing with the content it is supposed to contain.
#:
#: Taken verbatim from `nodetop`, which measured them, so the two tools read as
#: one family. Copied rather than imported, since these packages share no code
#: by design, which is the same argument :data:`_PALETTE` carries.
_FRAME_ANCHORS = ((195, 209, 246), (200, 194, 246), (215, 183, 234), (230, 190, 222))

#: The same sweep on the xterm-256 cube, held to the same rule: nothing below
#: the bright band, or the bottom border vanishes. The cube is thin on pale
#: violets, so this is three tones rather than ten, which is what a frame
#: needs, the gradient being a texture and not a scale.
_FRAME_256 = (189, 189, 189, 189, 183, 183, 182, 182, 182, 182)

#: Sixteen colours, which is what `TERM=screen` and most tmux defaults
#: advertise, and the depth with no room to be clever. Bright variants only:
#: plain blue at this depth is a murky navy that disappears against a dark
#: background, and because the sweep runs diagonally that is exactly where the
#: bottom border lands.
_FRAME_16 = (94, 95)

#: Steps to quantise the truecolor sweep into: fine enough that the bands are
#: invisible, coarse enough that runs of equal colour still group into one
#: escape sequence instead of one per column.
_FRAME_STEPS = 24


def _frame_ramp(style):
    # type: (Style) -> List[Tuple[Tuple[int, int, int], int, int]]
    """Tones for the frame gradient, lightest first; empty when colour is off.

    Empty is what makes `NO_COLOR` and `TERM=dumb` work: the caller falls back
    to `paint`, which is a no-op at depth zero, so the frame degrades to bare
    box characters rather than to box characters wearing an escape sequence.
    """
    if not style.enabled:
        return []
    if style.depth >= 24:
        span = len(_FRAME_ANCHORS) - 1
        out = []  # type: List[Tuple[Tuple[int, int, int], int, int]]
        for i in range(_FRAME_STEPS):
            scaled = (i / float(_FRAME_STEPS - 1)) * span
            low = min(span, int(scaled))
            high = min(span, low + 1)
            fraction = scaled - low
            rgb = (
                int(
                    round(
                        _FRAME_ANCHORS[low][0]
                        + (_FRAME_ANCHORS[high][0] - _FRAME_ANCHORS[low][0]) * fraction
                    )
                ),
                int(
                    round(
                        _FRAME_ANCHORS[low][1]
                        + (_FRAME_ANCHORS[high][1] - _FRAME_ANCHORS[low][1]) * fraction
                    )
                ),
                int(
                    round(
                        _FRAME_ANCHORS[low][2]
                        + (_FRAME_ANCHORS[high][2] - _FRAME_ANCHORS[low][2]) * fraction
                    )
                ),
            )
            out.append((rgb, 0, 0))
        return out
    if style.depth >= 8:
        return [((0, 0, 0), code, 0) for code in _FRAME_256]
    return [((0, 0, 0), 0, code) for code in _FRAME_16]


def panel(lines, style=None, size=None, shrink=True, role=None):
    # type: (Sequence[str], Optional[Style], Optional[int], bool, Optional[str]) -> str
    """A framed block, for content that should read as one unit.

    The border carries a DIAGONAL colour sweep: hue advances with ``x + y``, so
    the lightest point is the top left corner and the sweep travels round to
    the bottom right the way a highlight falls across a glossy surface. It is
    drawn in runs of equal tone rather than per character, which costs about
    ten escape sequences per border instead of one per column.

    ``shrink`` sizes the frame to its widest line instead of stretching it to
    the window. A box ruled out to 200 columns around 76 columns of content
    reads as an empty room; sized to the content it reads as one object, which
    is the only reason to draw a frame at all.

    ``role`` forces one flat palette colour instead, for a frame that has to
    mean something. Deliberately not the default: the frame is chrome, and
    chrome that shouts a semantic colour competes with the numbers inside it.

    **A content line wider than the frame is truncated, which is a backstop
    and not a policy.** A path must never be shortened, so the caller that
    cannot fit one is expected to stop framing rather than to hand it over and
    have an ellipsis eaten into it. See `atlas`, which drops to an unframed
    stacked layout at exactly that point.
    """
    style = style or Style()
    g = style.g
    window = size if size else style.size
    rows = list(lines)
    if shrink and rows:
        window = min(window, max([width(line) for line in rows]) + 4)
    window = max(window, MIN_WIDTH // 2)
    inner = window - 4
    height = len(rows) + 2
    ramp = [] if role is not None else _frame_ramp(style)

    def tone_at(x, y):
        # type: (int, int) -> Optional[str]
        """Hue for one frame cell, sweeping diagonally from the top left."""
        if not ramp:
            return None
        across = x / float(window - 1) if window > 1 else 0.0
        down = y / float(height - 1) if height > 1 else 0.0
        at = 0.5 * across + 0.5 * down
        return style._sgr(ramp[min(len(ramp) - 1, max(0, int(at * len(ramp))))])

    def cell(text, x, y):
        # type: (str, int, int) -> str
        prefix = tone_at(x, y)
        if prefix is None:
            return style.paint(role or "dim", text)
        return prefix + text + "\033[0m"

    def sweep(text, y):
        # type: (str, int) -> str
        """A horizontal border run, grouping equal tones into one escape."""
        if not ramp:
            return style.paint(role or "dim", text)
        out = []  # type: List[str]
        buffered = []  # type: List[str]
        current = None  # type: Optional[str]
        for i, ch in enumerate(text):
            tone = tone_at(i, y)
            if current is not None and tone != current:
                out.append(current + "".join(buffered) + "\033[0m")
                buffered = []
            current = tone
            buffered.append(ch)
        if buffered and current is not None:
            out.append(current + "".join(buffered) + "\033[0m")
        return "".join(out)

    # The frame is unbroken, and a title lives INSIDE it as a content line. A
    # title inlaid into the top edge cuts the border where the eye expects it
    # to continue, and a box that is a box everywhere is worth more than a
    # label saving one line.
    out = [sweep(g.tl + g.h * (window - 2) + g.tr, 0)]
    for i, line in enumerate(rows):
        fitted = pad(truncate(line, inner, g.ellipsis), inner)
        out.append(cell(g.v, 0, i + 1) + " " + fitted + " " + cell(g.v, window - 1, i + 1))
    out.append(sweep(g.bl + g.h * (window - 2) + g.br, height - 1))
    return "\n".join(out)


def rule(title="", style=None, size=None):
    # type: (str, Optional[Style], Optional[int]) -> str
    """A horizontal rule with an optional title on the left."""
    style = style or Style()
    window = size if size else style.size
    if not title:
        return style.dim(style.g.h * window)
    room = max(0, window - width(title) - 1)
    return style.head(title) + " " + style.dim(style.g.h * room)


def legend(items, style=None, size=None, indent="  "):
    # type: (Sequence[str], Optional[Style], Optional[int], str) -> str
    """Join short items with a separator, wrapping when the next will not fit."""
    style = style or Style()
    window = size if size else style.size
    if not items:
        return ""
    joiner = "  " + style.dim(style.g.sep) + "  "
    lines = []  # type: List[str]
    current = ""
    for item in items:
        candidate = item if not current else current + joiner + item
        if current and width(indent) + width(candidate) > window:
            lines.append(indent + current)
            current = item
        else:
            current = candidate
    lines.append(indent + current)
    return "\n".join(lines)


def wrap(text, indent="  ", size=None, style=None):
    # type: (str, str, Optional[int], Optional[Style]) -> str
    """Wrap prose on spaces, keeping a hanging indent.

    Hand rolled rather than `textwrap`, which measures an escape sequence as
    visible text and so wraps a coloured sentence short.
    """
    style = style or Style()
    window = size if size else style.size
    room = max(20, window - width(indent))
    lines = []  # type: List[str]
    current = ""
    for word in text.split():
        candidate = word if not current else current + " " + word
        if current and width(candidate) > room:
            lines.append(indent + current)
            current = word
        else:
            current = candidate
    if current:
        lines.append(indent + current)
    return "\n".join(lines)
