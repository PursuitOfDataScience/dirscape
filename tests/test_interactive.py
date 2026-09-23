"""Keyboard selection.

Driven by scripted keys and a capturing writer, so the suite needs no terminal
and no typing. The two navigation lessons `nodetop` learned from being used
(Left at the root, Right at a leaf) each have a test here, because they are the
kind of thing that gets "simplified" back into a bug.
"""

import contextlib
import os
import sys

import pytest

from dirscape import cli, interactive
from dirscape.interactive import Key, highlight, read_key, select, supported


def _reader(sequence):
    """A key source that yields a scripted sequence then quits.

    Quitting at the end rather than raising keeps a test that navigates too
    far from hanging: the failure is an assertion, not a timeout.
    """
    keys = list(sequence)

    def read():
        return keys.pop(0) if keys else Key.QUIT

    return read


def _chars(text):
    """A single-character reader over a string, for `read_key`."""
    buffer = list(text)

    def readch():
        return buffer.pop(0) if buffer else ""

    return readch


# --------------------------------------------------------------------------
# Key decoding
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "text,expected",
    [
        ("\x1b[A", Key.UP),
        ("\x1b[B", Key.DOWN),
        ("\x1b[C", Key.RIGHT),
        ("\x1b[D", Key.LEFT),
        ("\r", Key.ENTER),
        ("\n", Key.ENTER),
        ("q", Key.QUIT),
        ("Q", Key.QUIT),
        ("j", Key.DOWN),
        ("k", Key.UP),
        ("h", Key.LEFT),
        ("l", Key.RIGHT),
        ("z", Key.OTHER),
    ],
)
def test_keys_decode(text, expected):
    assert read_key(_chars(text)) == expected


def test_an_arrow_is_decoded_as_one_key_and_not_three():
    """A lone `ESC` read as the first byte of an arrow would swallow the next
    keypress, which is why the sequence is decoded here and not by the caller.
    """
    readch = _chars("\x1b[B\x1b[B")
    assert read_key(readch) == Key.DOWN
    assert read_key(readch) == Key.DOWN


def test_a_bare_escape_steps_back_and_leaves_only_from_the_root():
    """This REVERSES an earlier decision, and the reversal is the right way.

    Escape used to decode to QUIT, on the reading that it is not a movement so
    "leave" is the honest translation. Wrong in the one place it matters: in a
    detail view it closed the program instead of returning to the table, which
    is the opposite of Escape in every nested view a reader has used. Owner's
    report: "esc doesn't work for going back".

    Decoding it as BACK is only half the answer, because BACK from the top
    level would be a key that does nothing, and a key that does nothing reads
    as a hung program. `select` resolves it against its own depth, which is
    the only place that knows: it pops a level where there is one, and leaves
    at the root.
    """
    assert read_key(_chars("\x1bz")) == Key.BACK

    # Nested: Escape pops one level.
    assert (
        select(
            lambda i: ["a", "b"],
            2,
            keys=_reader([Key.BACK]),
            write=lambda text: None,
            escapable=True,
            raw=False,
        )
        == Key.BACK
    )
    # At the root there is nothing to pop, so it leaves.
    assert (
        select(
            lambda i: ["a", "b"],
            2,
            keys=_reader([Key.BACK]),
            write=lambda text: None,
            escapable=False,
            raw=False,
        )
        == Key.QUIT
    )


def test_end_of_input_is_a_quit_and_not_a_hang():
    assert read_key(_chars("")) == Key.QUIT


def test_ctrl_c_still_interrupts():
    """cbreak leaves it to us on some platforms, so it is raised explicitly.

    A browse that cannot be interrupted on a login node is a browse that has
    to be killed from another session.
    """
    with pytest.raises(KeyboardInterrupt):
        read_key(_chars("\x03"))


# --------------------------------------------------------------------------
# Navigation
# --------------------------------------------------------------------------


def _run(keys, count=3, **kw):
    written = []
    outcome = select(
        lambda i: ["row %d" % (i,)],
        count,
        keys=_reader(keys),
        write=written.append,
        raw=False,
        **kw,
    )
    return outcome, written


def test_enter_returns_the_highlighted_row():
    assert _run([Key.DOWN, Key.DOWN, Key.ENTER])[0] == 2


def test_right_opens_the_same_as_enter():
    """Right means deeper everywhere in this tool."""
    assert _run([Key.DOWN, Key.RIGHT])[0] == 1


def test_the_cursor_wraps_at_both_ends():
    """So a long list can be reached from either direction."""
    assert _run([Key.UP, Key.ENTER], count=3)[0] == 2, "up from the top wraps to the end"
    assert _run([Key.DOWN, Key.DOWN, Key.DOWN, Key.ENTER], count=3)[0] == 0, (
        "down from the end wraps to the top"
    )


def test_initial_puts_the_cursor_where_the_caller_left_it():
    """Stepping out of a nested view lands on the row you came from."""
    assert _run([Key.ENTER], initial=2)[0] == 2


def test_left_at_the_root_does_nothing():
    """It used to return, and returning at the root exits, so one stray press
    took the whole program down.
    """
    outcome, _ = _run([Key.LEFT, Key.LEFT, Key.DOWN, Key.ENTER], escapable=False)
    assert outcome == 1, "Left must not have moved or exited"


def test_left_below_the_root_steps_back():
    """The control for the test above: it is the ROOT that is special."""
    assert _run([Key.LEFT], escapable=True)[0] == Key.BACK


def test_right_at_a_leaf_does_nothing():
    """It used to return the index, and a caller with nothing deeper to open
    read that as "step back", so Right at the detail view bounced the reader
    into the same view again.
    """
    outcome, _ = _run([Key.RIGHT, Key.ENTER, Key.QUIT], openable=False)
    assert outcome == Key.QUIT, "neither Right nor Enter opens at a leaf"


def test_q_quits_from_anywhere():
    assert _run([Key.DOWN, Key.QUIT])[0] == Key.QUIT


def test_an_unknown_key_is_ignored_rather_than_acted_on():
    outcome, _ = _run([Key.OTHER, Key.OTHER, Key.ENTER])
    assert outcome == 0, "a stray keypress must not move the cursor"


def test_an_empty_list_quits_instead_of_dividing_by_zero():
    assert select(lambda i: [], 0, keys=_reader([]), write=lambda t: None, raw=False) == (Key.QUIT)


def test_interrupt_during_a_browse_leaves_cleanly():
    def boom():
        raise KeyboardInterrupt

    assert select(lambda i: ["x"], 2, keys=boom, write=lambda t: None, raw=False) == (Key.QUIT)


def test_the_frame_is_erased_on_the_way_out():
    """Each level replaces the last rather than scrolling it away, so there is
    one screen and not a transcript of screens.
    """
    _, written = _run([Key.QUIT])
    assert any("\033[J" in chunk for chunk in written)


def test_nothing_is_erased_when_erase_is_off():
    _, written = _run([Key.QUIT], erase=False)
    assert not any("\033[J" in chunk for chunk in written)


def test_the_renderer_is_called_again_on_every_keypress():
    """The whole block is RENDERED on every keypress, so the view cannot drift
    from the renderer; `repaint` then decides which of its lines to send.
    """
    seen = []

    select(
        lambda i: seen.append(i) or ["row %d" % (i,)],
        3,
        keys=_reader([Key.DOWN, Key.DOWN, Key.QUIT]),
        write=lambda t: None,
        raw=False,
    )
    assert seen == [0, 1, 2]


# --------------------------------------------------------------------------
# Highlighting
# --------------------------------------------------------------------------


def test_the_highlight_is_inverse_video_and_not_a_colour():
    """Inverse survives NO_COLOR, a 16 colour console and a light background
    alike, and does not collide with the colours the table already uses to
    mean something.
    """
    painted = highlight(["a", "b", "c"], 1)
    assert painted[0] == "a"
    assert "\033[7m" in painted[1]
    assert painted[1].endswith("\033[0m")
    assert painted[2] == "c"


def test_the_highlight_resets_before_it_starts():
    """A line that already ends mid-colour cannot leak into the highlight."""
    painted = highlight(["\033[31mred"], 0)
    assert painted[0].startswith("\033[0m\033[7m")


def test_highlighting_out_of_range_changes_nothing():
    assert highlight(["a", "b"], 7) == ["a", "b"]


# --------------------------------------------------------------------------
# The gate
# --------------------------------------------------------------------------


class _Stream(object):
    def __init__(self, tty):
        self._tty = tty

    def isatty(self):
        return self._tty


def test_a_dumb_terminal_is_unsupported(monkeypatch):
    monkeypatch.setenv("TERM", "dumb")
    assert supported(_Stream(True)) is False


def test_a_redirected_stdout_is_unsupported(monkeypatch):
    monkeypatch.delenv("TERM", raising=False)
    monkeypatch.delenv("DIRSCAPE_NO_INTERACTIVE", raising=False)
    assert supported(_Stream(False)) is False


def test_a_redirected_stdin_is_unsupported(monkeypatch):
    """Otherwise a run with stdin from a file consumes that file as keystrokes."""
    monkeypatch.delenv("TERM", raising=False)
    monkeypatch.delenv("DIRSCAPE_NO_INTERACTIVE", raising=False)
    monkeypatch.setattr(sys, "stdin", _Stream(False))
    assert supported(_Stream(True)) is False


def test_the_env_override_turns_it_off(monkeypatch):
    """So a script, a demo recording or a test can pin the static print."""
    monkeypatch.setenv("DIRSCAPE_NO_INTERACTIVE", "1")
    monkeypatch.setattr(sys, "stdin", _Stream(True))
    assert supported(_Stream(True)) is False


def test_a_short_terminal_falls_back_to_the_printout(monkeypatch):
    """Rather than to a screen that repaints on top of itself."""
    monkeypatch.delenv("TERM", raising=False)
    monkeypatch.delenv("DIRSCAPE_NO_INTERACTIVE", raising=False)
    monkeypatch.setattr(sys, "stdin", _Stream(True))

    class Size(object):
        lines = interactive.MIN_LINES - 1
        columns = 80

    monkeypatch.setattr(interactive.shutil, "get_terminal_size", lambda *a: Size())
    assert supported(_Stream(True)) is False


def test_supported_when_both_ends_are_a_terminal(monkeypatch):
    monkeypatch.delenv("TERM", raising=False)
    monkeypatch.delenv("DIRSCAPE_NO_INTERACTIVE", raising=False)
    monkeypatch.setattr(sys, "stdin", _Stream(True))

    class Size(object):
        lines = 40
        columns = 120

    monkeypatch.setattr(interactive.shutil, "get_terminal_size", lambda *a: Size())
    assert supported(_Stream(True)) is True


# --------------------------------------------------------------------------
# A real terminal
# --------------------------------------------------------------------------


@pytest.mark.skipif(not hasattr(os, "fork"), reason="needs fork")
def test_a_real_pty_paints_moves_and_exits():
    """End to end in an actual terminal, because scripted keys cannot prove it.

    Everything above drives `select` through injected readers and writers,
    which verifies the logic and says nothing about whether the escape
    sequences land, the terminal mode is restored, or the drill-down opens. A
    pty answers all three, and it is the only test here that would catch a
    tool that leaves a login node's terminal without a cursor.

    Runs the package through `-m` rather than the console script, so it tests
    this checkout and not whatever happens to be installed.
    """
    pty = pytest.importorskip("pty")
    import select as sel
    import subprocess
    import time

    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    pid, fd = pty.fork()
    if pid == 0:  # pragma: no cover - the child execs
        os.environ["TERM"] = "xterm"
        # A person at a terminal, which is what this measures: an agent
        # harness running the suite exports variables that turn the browse
        # off on purpose (`cli.agent_driven`), and the test would then skip.
        for name in cli.AGENT_VARIABLES:
            os.environ.pop(name, None)
        os.environ["PYTHONPATH"] = os.path.join(root, "src")
        os.environ["DIRSCAPE_TEST_ROWS"] = "1"
        os.execv(sys.executable, [sys.executable, "-m", "dirscape", "--no-state"])

    out = b""
    sent = False
    deadline = time.time() + 90
    try:
        while time.time() < deadline:
            ready, _, _ = sel.select([fd], [], [], 1.0)
            if not ready:
                continue
            try:
                chunk = os.read(fd, 65536)
            except OSError:
                break
            if not chunk:
                break
            out += chunk
            # Waits for the KEY HINTS, not for a column heading.
            #
            # This trigger went stale three times, as `used / quota`, then
            # `space`, then `limit`, and each time it failed silently in the
            # worst way: no keys are sent, the loop spins to its 90 second
            # deadline, and the assertions below pass on the first paint
            # alone, so the suite went from 14 seconds to 100 while claiming
            # to test a drill-down it never performed. Column headings are
            # exactly the thing this project keeps rewording.
            #
            # The hint line is the interactive contract rather than a label
            # choice: if it is gone, there is no interactive mode to test and
            # the assertions below say so directly.
            if not sent and b"quit" in out:
                time.sleep(0.4)
                os.write(fd, b"\x1b[B\x1b[B")  # down, down
                time.sleep(0.4)
                os.write(fd, b"\r")  # open the row
                time.sleep(0.6)
                os.write(fd, b"q")  # and leave
                sent = True
        text = out.decode("utf-8", "replace")
    finally:
        with contextlib.suppress(OSError):
            os.close(fd)
        with contextlib.suppress(OSError):
            os.waitpid(pid, os.WNOHANG)

    if "\033[?25l" not in text:
        pytest.skip("no interactive frame was drawn here, so there is nothing to browse")

    # **The trigger fired.** Without this the test is silently vacuous when
    # the heading it waits for is renamed: it sends no keys, spins for 90
    # seconds and then passes on the first paint. That happened twice.
    assert sent, "the keypresses were never sent, so nothing below was exercised"

    assert "\033[7m" in text, "no row was highlighted"
    assert "\033[?25l" in text, "the cursor was never hidden"
    assert "\033[?25h" in text, "the cursor was never restored"
    assert "\033[J" in text, "no frame was erased, so frames would stack up"
    assert "move" in text and "open" in text, "the key hints were not shown"


# --------------------------------------------------------------------------
# The highlight has to be one band, not ragged emphasis
# --------------------------------------------------------------------------


def _plain(text):
    import re

    return re.sub(r"\033\[[0-9;?]*[A-Za-z]", "", text)


def test_the_band_is_the_same_width_on_every_row():
    """Otherwise the cursor looks like a different shape on each line.

    Measured in a pty before the fix: three rows highlighted at 47, 69 and 74
    columns, each stopping exactly where its own text ran out.
    """
    rows = ["short", "a much longer row than that one", "middle length"]
    widths = set()
    for index in range(len(rows)):
        painted = highlight(rows, index)
        widths.add(len(_plain(painted[index])))
    assert len(widths) == 1, "bands of %s columns" % (sorted(widths),)
    assert widths == {len(max(rows, key=len))}


def test_the_band_carries_no_sgr_of_its_own():
    """The load-bearing one, and it replaces a weaker guarantee.

    The table's cells carry their own colours. The first version of this test
    asserted that every `\\033[0m` embedded in the row was followed by a fresh
    inverse, which kept the band CONTINUOUS: without it the band switched off
    at the first embedded reset and the highlight stopped mid-row.

    Continuity was never the whole problem. Under inverse video an explicit
    FOREGROUND becomes the background, so the surviving colours painted a
    differently coloured block per coloured run, on top of the band. Owner's
    words: "the highlightor gets truncated by `▎▒░░░░░░   3%` which looks so
    ugly". The row's own styling is stripped now, which subsumes the old
    assertion: a body with no escapes in it cannot contain a reset that ends
    the band, and cannot contain a colour that recolours it.
    """
    line = "left \033[31mred\033[0m middle \033[32mgreen\033[0m right"
    painted = highlight([line], 0)[0]

    body = painted[len(interactive.RESET + interactive.INVERSE) : -len(interactive.RESET)]
    assert "\033" not in body, "the band must carry no escape of its own: %r" % (body,)
    assert body == "left red middle green right", "and not one character may be lost"


def test_the_band_covers_the_padding_and_then_stops():
    painted = highlight(["ab", "abcdef"], 0)[0]
    assert painted.startswith(interactive.RESET + interactive.INVERSE)
    assert painted.endswith(interactive.RESET)
    assert _plain(painted) == "ab    ", "the pad is inside the band"


def test_pad_to_overrides_the_measured_width():
    """So a caller that knows the window can fill it."""
    painted = highlight(["ab"], 0, pad_to=10)[0]
    assert len(_plain(painted)) == 10


def test_window_rows_is_separate_from_supported():
    """They answer different questions: whether anyone can type, and whether
    what is about to be drawn will fit.
    """
    rows = interactive.window_rows()
    assert isinstance(rows, int)
    assert rows >= 0


def test_a_frame_taller_than_the_window_is_the_caller_s_problem(monkeypatch):
    """`supported()` cannot catch it: it knows the window height but not what
    is about to be drawn in it.

    Measured: a `--all` run in a 14 row terminal asked to move the cursor up
    71 lines, which lands at the top of the WINDOW rather than the top of the
    block because the block has scrolled, and the erase that follows wipes
    whatever the user had above. That is the "entire terminal turns empty"
    report.
    """

    class Size(object):
        lines = 14
        columns = 100

    monkeypatch.setattr(interactive.shutil, "get_terminal_size", lambda *a: Size())
    assert interactive.window_rows() == 14
    # A 71 line block does not fit, and the caller is expected to notice.
    assert interactive.window_rows() < 71 + 1


# --------------------------------------------------------------------------
# A held arrow: the stride ramp, and the coalescing that makes it safe
# --------------------------------------------------------------------------


def _hold(key, seconds, hz, held=None):
    """Press ``key`` at ``hz`` for ``seconds``. Returns rows travelled."""
    held = held or interactive.HeldKey()
    rows = 0
    for tick in range(int(seconds * hz)):
        rows += held.stride(key, tick / float(hz))
    return rows, held


def test_a_tap_moves_exactly_one_row():
    """The ramp must not take away reading down a list one row at a time."""
    held = interactive.HeldKey()
    assert held.stride(Key.DOWN, 0.0) == 1
    assert held.stride(Key.DOWN, 0.03) == 1


def test_nothing_changes_until_the_key_has_been_held():
    held = interactive.HeldKey()
    for tick in range(int(interactive.ACCEL_AFTER * 30)):
        assert held.stride(Key.DOWN, tick / 30.0) == 1
    assert held.stride(Key.DOWN, interactive.ACCEL_AFTER + 0.01) == interactive.ACCEL_FIRST


def test_a_held_key_covers_ground_and_a_tapped_one_never_does():
    """The distinction `ACCEL_GAP` alone cannot draw.

    A held key repeats at 25-33 Hz on every common setting; a reader pressing
    deliberately manages three or four a second. Six seconds of each:
    """
    holding, _ = _hold(Key.DOWN, 6.0, 30)
    tapping, _ = _hold(Key.DOWN, 6.0, 4)

    assert holding > 2000, "six seconds of holding should cross a large directory"
    assert tapping == 24, "a deliberate tapper moves one row per press, for ever"


def test_the_ramp_is_capped():
    """The exponent comes off the wall clock, so a minute must not overflow it."""
    held = interactive.HeldKey()
    for tick in range(60 * 30):
        step = held.stride(Key.DOWN, tick / 30.0)
    assert step == interactive.ACCEL_MAX


def test_reversing_stops_a_run_dead():
    """How somebody stops after overshooting.

    Speed built up going down is never inherited by the arrow going up, or
    the correction would fly past the row they were aiming at.
    """
    _rows, held = _hold(Key.DOWN, 4.0, 30)
    assert held.stride(Key.DOWN, 4.0) > 1, "the run was accelerating"
    assert held.stride(Key.UP, 4.01) == 1


def test_letting_go_stops_a_run_dead():
    _rows, held = _hold(Key.DOWN, 4.0, 30)
    assert held.stride(Key.DOWN, 4.0 + interactive.ACCEL_GAP + 0.01) == 1


def test_any_other_key_ends_the_run():
    _rows, held = _hold(Key.DOWN, 4.0, 30)
    assert held.stride(Key.ENTER, 4.0) == 1
    assert held.stride(Key.DOWN, 4.01) == 1


def test_a_tap_wraps_and_a_hold_clamps():
    """Wrapping is a shortcut when you meant it and an accident when you did not.

    One press off the top meaning "jump to the bottom" is worth keeping. The
    same wrap arriving two seconds into a hold throws the reader back to the
    other end of a directory they were reading down.
    """
    assert _run([Key.UP, Key.ENTER], count=50)[0] == 49, "a tap still wraps"

    held = interactive.HeldKey()
    cursor = 3
    for tick in range(int((interactive.ACCEL_AFTER + 1.0) * 30)):
        step = held.stride(Key.UP, tick / 30.0)
        cursor = (cursor - 1) % 50 if step == 1 else max(0, cursor - step)
    assert cursor == 0, "a held key stops at the end instead of wrapping past it"


def test_a_key_already_waiting_is_folded_into_the_same_frame():
    """Coalescing is what keeps the accelerator from outrunning the repaint.

    This loop redraws the whole block per key. Without folding, a held arrow
    fills the terminal's input buffer, the cursor keeps flying for a second
    after the reader lets go, and the list stops where nobody asked.

    Counted at `render`, which runs exactly once per frame. Counting writes
    would count two per repaint, because `paint` emits the cursor-up erase
    and the block separately.
    """
    drawn = []
    waiting = [True, True, True, False]

    outcome = select(
        lambda i: drawn.append(i) or ["row %d" % (i,)],
        10,
        keys=_reader([Key.DOWN, Key.DOWN, Key.DOWN, Key.DOWN, Key.ENTER]),
        write=lambda text: None,
        raw=False,
        pending=lambda: waiting.pop(0) if waiting else False,
    )

    assert outcome == 4, "every key still moved the cursor"
    assert drawn == [0, 4], "the initial frame, then one for all four moves"


def test_coalescing_is_off_when_a_test_drives_the_keys():
    """`raw=False` means nobody is typing at a file descriptor.

    Asking `select.select` about stdin there answers a question about the
    wrong thing, so the default must be "nothing is waiting".
    """
    drawn = []
    select(
        lambda i: drawn.append(i) or ["row %d" % (i,)],
        10,
        keys=_reader([Key.DOWN, Key.DOWN, Key.ENTER]),
        write=lambda text: None,
        raw=False,
    )
    assert drawn == [0, 1, 2], "one frame to start and one per key"


# --------------------------------------------------------------------------
# Repainting without flicker
# --------------------------------------------------------------------------


class _Terminal(object):
    """A minimal VT100: exactly the sequences `repaint` and `Screen` emit.

    CR, LF (with the tty's ONLCR, so it returns to column 0), cursor up, erase
    below, and SGR and private modes, which change nothing about the text.
    Enough to check what a real terminal would SHOW after each write, without
    a dependency. Writing the last column leaves the cursor pending a wrap, as
    xterm does, so a frame that relies on that rule is exercised by it.
    """

    def __init__(self, width=48, height=40):
        self.width, self.height = width, height
        self.rows = [[" "] * width for _ in range(height)]
        self.row = self.col = 0
        self.pending_wrap = False

    def feed(self, data):
        import re

        index = 0
        escape = re.compile(r"\033\[(\??)([0-9;]*)([A-Za-z])")
        while index < len(data):
            match = escape.match(data, index)
            if match:
                private, params, final = match.groups()
                if final == "A" and not private:
                    self.row = max(0, self.row - int(params or "1"))
                    self.pending_wrap = False
                elif final == "J" and not private:
                    self.rows[self.row][self.col :] = [" "] * (self.width - self.col)
                    for below in range(self.row + 1, self.height):
                        self.rows[below] = [" "] * self.width
                index = match.end()
                continue
            char = data[index]
            index += 1
            if char == "\r":
                self.col, self.pending_wrap = 0, False
            elif char == "\n":
                self.col, self.pending_wrap = 0, False
                if self.row == self.height - 1:
                    self.rows = self.rows[1:] + [[" "] * self.width]
                else:
                    self.row += 1
            else:
                if self.pending_wrap:
                    raise AssertionError("wrote past the last column")
                self.rows[self.row][self.col] = char
                if self.col == self.width - 1:
                    self.pending_wrap = True
                else:
                    self.col += 1

    def text(self):
        return ["".join(row).rstrip() for row in self.rows]


def _frames(seed, width):
    """Blocks of every shape a browse produces: longer, shorter, wider, styled."""
    import random

    rng = random.Random(seed)
    out = []
    for _ in range(40):
        lines = []
        for _row in range(rng.randint(1, 12)):
            text = "".join(rng.choice("ab #|") for _c in range(rng.randint(0, width)))
            if rng.random() < 0.3:
                text = "\033[38;5;61m" + text + "\033[0m"
            lines.append(text)
        if out and rng.random() < 0.5:
            # A highlight move: the same block with one line changed.
            lines = list(out[-1])
            lines[rng.randrange(len(lines))] = "\033[7m" + "x" * rng.randint(0, width) + "\033[0m"
        out.append(lines)
    return out


@pytest.mark.parametrize("seed", range(8))
def test_every_repaint_leaves_exactly_the_new_frame_on_screen(seed):
    """The property the flicker fix must not trade away: correctness.

    Lines are skipped, padded over and cleared rather than erased and redrawn,
    so a mistake would leave a stale cell. Every transition between random
    blocks, including full-width lines ending in the last column, is checked
    against what a terminal would then display.
    """
    from dirscape.render.style import plain

    width = 48
    term = _Terminal(width=width)
    term.feed("prompt$ dirscape\n")
    start = term.row
    previous = []
    for frame in _frames(seed, width):
        term.feed(interactive.repaint(previous, frame))
        shown = term.text()
        top = term.row - len(frame)
        assert top >= 0
        assert shown[top : term.row] == [plain(line).rstrip() for line in frame]
        assert all(not line for line in shown[term.row :]), "stale rows below the block"
        assert top == start or start > top, "the block drifted down the screen"
        previous = frame


def test_a_moved_highlight_rewrites_only_the_two_rows_that_changed():
    """Measured: 465 KB for 87 arrows in the table before, 43 KB after."""
    before = ["row %d" % i for i in range(30)]
    after = list(before)
    after[3], after[4] = "\033[7mrow 3\033[0m", "row 4 now"
    data = interactive.repaint(before, after)
    assert "row 3" in data and "row 4 now" in data
    assert "row 2" not in data and "row 29" not in data, "unchanged rows must not be resent"
    assert interactive.repaint(before, before).count("row") == 0


def test_nothing_is_erased_before_it_is_replaced():
    """The flicker: `ESC[<n>A ESC[J` went out, then the frame, in two writes.

    In a 40 x 120 pty, 85 of 261 screen states during a run of arrows showed
    the table erased. An erase now only ever follows the new content, and only
    when the block got shorter.
    """
    grown = interactive.repaint(["a", "b"], ["c", "d", "e"])
    assert "\033[J" not in grown
    shrunk = interactive.repaint(["a", "b", "c"], ["d"])
    assert shrunk.index("\033[J") > shrunk.index("d")
    assert grown.startswith(interactive.SYNC_BEGIN) and grown.endswith(interactive.SYNC_END)


def test_each_frame_is_one_write():
    written = []
    select(
        lambda i: ["row %d" % n + (" <" if n == i else "") for n in range(5)],
        5,
        keys=_reader([Key.DOWN, Key.DOWN, Key.QUIT]),
        write=written.append,
        raw=False,
    )
    frames = [chunk for chunk in written if chunk.startswith(interactive.SYNC_BEGIN)]
    assert len(frames) == 3, "one write per frame, first paint and two moves"
    assert len(written) == 4, "and the erase on the way out"


def test_a_shared_screen_is_drawn_over_rather_than_erased_between_views():
    """Opening a row erased the table and left the screen blank while the
    listing was read, which for a directory of ten thousand entries shows.
    """
    written = []
    screen = interactive.Screen(write=written.append)
    opened = select(
        lambda i: ["table %d" % n for n in range(4)],
        4,
        keys=_reader([Key.DOWN, Key.ENTER]),
        raw=False,
        screen=screen,
    )
    assert opened == 1
    assert screen.lines, "the table stays on screen for the next view to replace"
    assert not any(chunk.endswith("\033[J") and "table" not in chunk for chunk in written)

    back = select(
        lambda i: ["listing"],
        1,
        keys=_reader([Key.BACK]),
        raw=False,
        screen=screen,
    )
    assert back == Key.BACK
    listing = written[-1]
    assert listing.startswith(interactive.SYNC_BEGIN + "\r\033[4A"), "drawn over the table"
    assert listing.index("listing") < listing.index("\033[J"), "then the leftover rows cleared"

    assert (
        select(lambda i: ["x"], 1, keys=_reader([Key.QUIT]), raw=False, screen=screen) == Key.QUIT
    )
    assert written[-1].endswith("\033[J") and screen.lines == [], "quitting erases"
