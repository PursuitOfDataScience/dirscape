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

from dirscape import interactive
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


def test_a_bare_escape_leaves_rather_than_stepping_back():
    """Escape is not a movement, so it means what a reader means by it."""
    assert read_key(_chars("\x1bz")) == Key.QUIT


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
    """Redrawing everything beats patching the changed line: a partial repaint
    that gets one cell wrong is much harder to notice than a slower redraw.
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
            if not sent and b"used / quota" in out:
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

    if "used / quota" not in text:
        pytest.skip("no storage discoverable here, so there is nothing to browse")

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
