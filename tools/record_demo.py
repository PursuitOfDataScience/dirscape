"""Render the README's demo GIF by driving the real `ds` interface in a terminal.

Not shipped in the wheel: a maintenance script. It runs the command in a
pseudo-terminal, presses the keys a reader would press, replays every byte the
program wrote through a terminal emulator, and draws each screen.

**Over an invented cluster, never a real one.** `demo_cluster.py` serves a
made-up sweep and made-up directory listings to the unmodified interface, so
the renderer, the browser and its keys are real and every name and figure is
not. A recording of a live cluster puts its paths, groups and usage into a
public image, and that cannot be taken back once it is pushed. As a second
line, a frame showing any path outside the invented roots, or the recording
user's own name, stops the script before a GIF is written.

    pip install pyte pillow
    python tools/record_demo.py              # writes assets/demo.gif

Linux only, because it needs `pty`.
"""

import fcntl
import os
import pty
import re
import select
import signal
import struct
import sys
import termios
import time
from collections import Counter
from pathlib import Path

import pyte
from PIL import Image, ImageDraw, ImageFont

ROOT = Path(__file__).resolve().parents[1]
TARGET = ROOT / "assets" / "demo.gif"
COLS, ROWS = 100, 26

#: The storyboard. Rows are found by their text, never by index, so the
#: invented cluster can change without the storyboard counting rows again.
TABLE_ROW = "/software"
DESCENT = ("R-4.4.1/", "lib64/", "R/", "library/")
WHY_PATH = "/scratch/jdoe42"
USER = os.environ.get("USER") or "me"
DRIVER = ROOT / "tools" / "demo_cluster.py"

#: Every path a frame may show: the invented roots and what is under them.
ALLOWED = ("/home/jdoe42", "/project/astro-lab", "/project/genomics-core", "/scratch/jdoe42")
ALLOWED += ("/datasets/reference", "/software", "/tmp")

FONTS = (
    "/usr/share/fonts/dejavu/DejaVuSansMono.ttf",
    "/usr/share/fonts/truetype/dejavu/DejaVuSansMono.ttf",
)
SIZE = 15
BG = (13, 17, 23)
FG = (201, 209, 217)
BAR = (22, 27, 34)
PROMPT = "\033[38;2;126;231;135m$\033[0m "

KEYS = {"down": b"\033[B", "up": b"\033[A", "enter": b"\r", "esc": b"\033", "q": b"q"}
AGENT_VARIABLES = (
    "AI_AGENT",
    "CLAUDECODE",
    "GEMINI_CLI",
    "CODEX_THREAD_ID",
    "CODEX_SANDBOX_NETWORK_DISABLED",
    "OPENCODE",
    "DIRSCAPE_AGENT",
)

_BASE16 = [
    (0, 0, 0), (205, 49, 49), (13, 188, 121), (229, 229, 16),
    (36, 114, 200), (188, 63, 188), (17, 168, 205), (229, 229, 229),
    (102, 102, 102), (241, 76, 76), (35, 209, 139), (245, 245, 67),
    (59, 142, 234), (214, 112, 214), (41, 184, 219), (255, 255, 255),
]  # fmt: skip
_NAMED = {
    name: _BASE16[i]
    for i, name in enumerate(("black", "red", "green", "brown", "blue", "magenta", "cyan", "white"))
}
_NAMED.update({"bright" + k: _BASE16[i + 8] for i, k in enumerate(list(_NAMED))})


class Terminal:
    """One emulated screen, shared by every command the storyboard runs."""

    def __init__(self):
        self.screen = pyte.Screen(COLS, ROWS)
        self.stream = pyte.ByteStream(self.screen)
        self.frames = []  # (rows of cells, cursor or None, milliseconds)
        self.pending = b""
        self.fd = None
        self.pid = None

    # -- recording ------------------------------------------------------------
    def shot(self, ms, cursor=False):
        rows = [[self.screen.buffer[y][x] for x in range(COLS)] for y in range(ROWS)]
        where = (self.screen.cursor.x, self.screen.cursor.y) if cursor else None
        self.frames.append((rows, where, ms))

    def feed(self, text):
        self.stream.feed(text.encode("utf-8"))

    def type(self, command):
        """A prompt, then the command one keystroke at a time."""
        self.feed(PROMPT)
        self.shot(700, cursor=True)
        for ch in command:
            self.feed(ch)
            self.shot(55, cursor=True)
        self.shot(450, cursor=True)
        self.feed("\r\n")

    # -- the program ----------------------------------------------------------
    def spawn(self, argv):
        env = {k: v for k, v in os.environ.items() if k not in AGENT_VARIABLES}
        env.update(
            TERM="xterm-256color",
            COLORTERM="truecolor",
            PYTHONPATH=str(ROOT / "src"),
            PYTHONIOENCODING="utf-8",
        )
        env.pop("NO_COLOR", None)
        pid, fd = pty.fork()
        if pid == 0:  # the child: size the window before anything reads it
            fcntl.ioctl(0, termios.TIOCSWINSZ, struct.pack("HHHH", ROWS, COLS, 0, 0))
            os.execvpe(sys.executable, [sys.executable, str(DRIVER), *argv], env)
        self.pid, self.fd = pid, fd

    def pump(self, quiet=0.35, limit=40.0, soft=False):
        """Feed output until the program has said nothing for `quiet` seconds.

        ``soft`` returns after ``limit`` instead of failing, for a view that is
        animating and so never goes quiet: a spinner or a fade writes a frame
        every tenth of a second while something is being counted.
        """
        deadline = time.time() + limit
        last = time.time()
        while time.time() < deadline:
            ready, _, _ = select.select([self.fd], [], [], 0.05)
            if ready:
                try:
                    data = os.read(self.fd, 65536)
                except OSError:  # EIO: the child has exited
                    return False
                if not data:
                    return False
                self.feed_frames(data)
                last = time.time()
            elif time.time() - last > quiet:
                return True
        if soft:
            return True
        raise SystemExit("the program never went quiet")

    def feed_frames(self, data):
        """Feed whole synchronized frames only, as a terminal honouring DEC 2026 shows them.

        pyte ignores the mode, so a frame read in two pieces would otherwise be
        filmed half drawn.
        """
        self.pending += data
        opened = self.pending.rfind(b"\033[?2026h")
        closed = self.pending.rfind(b"\033[?2026l")
        if opened > closed:
            ready, self.pending = self.pending[:opened], self.pending[opened:]
        else:
            ready, self.pending = self.pending, b""
        if ready:
            self.stream.feed(ready)

    def film(self, seconds, every=0.1):
        """Record ``seconds`` of whatever moves, one frame each ``every`` seconds."""
        end = time.time() + seconds
        while time.time() < end:
            if not self.pump(quiet=every, limit=every, soft=True):
                break
            self.shot(int(every * 1000))

    def wait_for(self, text, limit=40.0, every=None):
        """Until ``text`` is on screen, filming meanwhile if ``every`` is given."""
        deadline = time.time() + limit
        while text not in "\n".join(self.screen.display):
            if time.time() > deadline:
                raise SystemExit("never saw %r on screen" % (text,))
            self.pump(quiet=0.1, limit=every or 0.1, soft=True)
            if every:
                self.shot(int(every * 1000))
        self.pump(soft=True, limit=1.0)

    def press(self, key, ms):
        os.write(self.fd, KEYS[key])
        alive = self.pump(soft=True, limit=1.0)
        self.shot(ms)
        return alive

    def finish(self):
        while self.pump(limit=40.0):
            pass
        if self.pending:
            self.stream.feed(self.pending)
            self.pending = b""
        os.close(self.fd)
        os.waitpid(self.pid, 0)

    def selected(self):
        """The text of the row painted in inverse video, or ''.

        The row with the most inverse cells, not one inverse across half the
        width: with a share column the band stops just past the percent, so
        the bars keep their colour.
        """
        best, text = 8, ""
        for y in range(ROWS):
            row = [self.screen.buffer[y][x] for x in range(COLS)]
            lit = sum(1 for c in row if c.reverse)
            if lit > best:
                best, text = lit, "".join(c.data for c in row)
        return text

    def move_to(self, text, ms=170, limit=60):
        for _ in range(limit):
            if text in self.selected():
                return
            self.press("down", ms)
        raise SystemExit("no row for %r" % (text,))


def storyboard(term):
    term.type("ds")
    term.spawn(["--no-state"])
    term.wait_for("q quit", every=0.1)  # the startup board, then the table
    term.shot(2600)

    term.move_to(TABLE_ROW, ms=150)
    term.shot(500)
    term.press("enter", 100)  # what is inside /software, counted while you watch
    term.film(9.5)
    term.shot(900)
    for depth, name in enumerate(DESCENT):  # and down, as far as the tree goes
        term.move_to(name, ms=80 if depth == 0 else 220)
        term.shot(350)
        term.press("enter", 100)
        term.film(2.4 if depth == len(DESCENT) - 1 else 1.2)
    for _ in DESCENT:  # esc climbs one level at a time
        term.press("esc", 320)
    term.press("esc", 1100)  # back at the table
    term.press("q", 800)
    term.finish()

    term.type("ds why " + WHY_PATH)
    term.spawn(["--no-state", "why", WHY_PATH])
    term.finish()
    term.feed(PROMPT)
    term.shot(4200, cursor=True)


# -- pixels ---------------------------------------------------------------------
#: Box drawing as geometry. The glyphs are shorter than the line pitch, so a
#: border drawn with the font is a dashed line; these reach the cell's edges.
_ARMS = {
    "─": "lr", "│": "ud", "┌": "rd", "┐": "ld", "└": "ru", "┘": "lu",
    "├": "udr", "┤": "udl", "┬": "lrd", "┴": "lru", "┼": "lrud",
    "╭": "rd", "╮": "ld", "╰": "ru", "╯": "lu",
}  # fmt: skip


def box(draw, ch, left, top, cw, lh, fill):
    """Draw a box drawing character edge to edge; False if `ch` is not one."""
    if ch == "━":
        # The heavy rule an opened directory's top edge is filled with while
        # its folders are counted: the light rule's line, three pixels thick.
        cy = top + lh // 2
        draw.line((left, cy, left + cw - 1, cy), fill=fill, width=3)
        return True
    arms = _ARMS.get(ch)
    if arms is None:
        return False
    cx, cy = left + cw // 2, top + lh // 2
    right, bottom = left + cw - 1, top + lh - 1
    if ch in "╭╮╰╯":
        r = cw // 2 - 1
        dx = r if "r" in arms else -r
        dy = r if "d" in arms else -r
        ox, oy = cx + dx, cy + dy  # the arc's centre
        draw.line((cx, oy, cx, bottom if dy > 0 else top), fill=fill)
        draw.line((ox, cy, right if dx > 0 else left, cy), fill=fill)
        start = {"rd": 180, "ld": 270, "lu": 0, "ru": 90}[arms]
        draw.arc((ox - r, oy - r, ox + r, oy + r), start, start + 90, fill=fill)
        return True
    ends = {"l": (left, cy), "r": (right, cy), "u": (cx, top), "d": (cx, bottom)}
    for arm in arms:
        draw.line((cx, cy) + ends[arm], fill=fill)
    return True


def colour(name, default):
    if name == "default":
        return default
    if name in _NAMED:
        return _NAMED[name]
    try:
        return tuple(int(name[i : i + 2], 16) for i in (0, 2, 4))
    except ValueError:
        return default


def render(frames, path):
    font_path = next((f for f in FONTS if os.path.exists(f)), None)
    if font_path is None:
        raise SystemExit("DejaVu Sans Mono not found")
    plain = ImageFont.truetype(font_path, SIZE)
    heavy = ImageFont.truetype(font_path.replace(".ttf", "-Bold.ttf"), SIZE)
    # The mono face has no braille, which is what every spinner is drawn in;
    # the proportional face of the same family has the whole block.
    braille = ImageFont.truetype(font_path.replace("SansMono", "Sans"), SIZE)
    # An integer cell: a fractional advance accumulates across a row, and a
    # run of box drawing picks up a one pixel gap wherever the fraction wraps.
    cw = round(plain.getlength("M"))
    lh = SIZE + 5
    pad, bar = 16, 30
    width, height = cw * COLS + pad * 2, lh * ROWS + pad * 2 + bar

    images = []
    for rows, cursor, _ms in frames:
        img = Image.new("RGB", (width, height), BG)
        draw = ImageDraw.Draw(img)
        draw.rectangle((0, 0, width, bar), fill=BAR)
        for i, dot in enumerate(((255, 95, 86), (255, 189, 46), (39, 201, 63))):
            x = 18 + i * 20
            draw.ellipse((x - 6, bar // 2 - 6, x + 6, bar // 2 + 6), fill=dot)
        title = "dirscape"
        draw.text(
            ((width - plain.getlength(title)) / 2, 7), title, font=plain, fill=(139, 148, 158)
        )
        for y, row in enumerate(rows):
            top = bar + pad + y * lh
            for x, cell in enumerate(row):
                fg, bg = colour(cell.fg, FG), colour(cell.bg, BG)
                if cell.reverse:
                    fg, bg = bg, fg
                left = pad + x * cw
                if bg != BG:
                    draw.rectangle((left, top, left + cw - 1, top + lh - 1), fill=bg)
                if cell.data.strip() and not box(draw, cell.data, left, top, cw, lh, fg):
                    font = heavy if cell.bold else plain
                    if "\u2800" <= cell.data <= "\u28ff":
                        font = braille
                    draw.text((left, top + 2), cell.data, font=font, fill=fg)
        if cursor is not None:
            left, top = pad + cursor[0] * cw, bar + pad + cursor[1] * lh
            draw.rectangle((left, top + 1, left + cw - 1, top + lh - 2), fill=FG)
        images.append(img)

    # One palette for every frame, so consecutive frames differ only where the
    # screen did and the encoder can store just that rectangle. The colours are
    # the ones actually drawn, most used first, so the window chrome and every
    # text tier survive exactly and only antialiasing fringes are approximated.
    counts = Counter()
    for img in images:
        for n, rgb in img.getcolors(maxcolors=1 << 24):
            counts[rgb] += n
    chosen = [rgb for rgb, _n in counts.most_common(256)]
    palette = Image.new("P", (1, 1))
    palette.putpalette([v for rgb in chosen for v in rgb] + [0] * (768 - 3 * len(chosen)))
    frames_p = [img.quantize(palette=palette, dither=Image.Dither.NONE) for img in images]
    frames_p[0].save(
        path,
        save_all=True,
        append_images=frames_p[1:],
        duration=[ms for _rows, _cursor, ms in frames],
        loop=0,
        optimize=False,
        disposal=1,
    )


def main():
    signal.signal(signal.SIGCHLD, signal.SIG_DFL)
    term = Terminal()
    storyboard(term)
    for rows, _cursor, _ms in term.frames:
        for row in rows:
            text = "".join(cell.data for cell in row)
            if len(USER) > 2 and USER in text:
                raise SystemExit("a frame names %r; not writing the GIF" % (USER,))
            for path in re.findall(r"(?<![\w.])/[\w.+-]+(?:/[\w.+-]*)*", text):
                # A prefix of an invented root is what the typing animation shows.
                if not path.startswith(ALLOWED) and not any(r.startswith(path) for r in ALLOWED):
                    raise SystemExit("a frame shows %r; not writing the GIF" % (path,))
    TARGET.parent.mkdir(exist_ok=True)
    render(term.frames, TARGET)
    seconds = sum(ms for _r, _c, ms in term.frames) / 1000.0
    sys.stdout.write(
        "%s: %d frames, %.1fs, %d KiB\n"
        % (TARGET, len(term.frames), seconds, TARGET.stat().st_size // 1024)
    )


if __name__ == "__main__":
    main()
