"""Motion: spinners, fades and the startup board.

Everything here runs on an injected clock and a capturing writer, so no test
sleeps to watch a spinner turn and none needs a terminal. The real terminal is
`test_cli.test_a_real_pty_animates_and_every_frame_fits`.
"""

import threading

import pytest

from dirscape import interactive, motion
from dirscape.render.style import _FRAMES, Glyphs, Style, panel, plain, truncate, width


class _Clock(object):
    def __init__(self, now=1000.0):
        self.now = now

    def __call__(self):
        return self.now

    def tick(self, seconds=motion.FRAME_S):
        self.now += seconds


def _style(depth=24, unicode_ok=True):
    return Style(color=depth > 0, depth=depth or 8, glyphs=Glyphs(unicode_ok), size=100)


def _moving(depth=24, unicode_ok=True, clock=None):
    style = _style(depth, unicode_ok)
    return style, motion.Motion(style, clock=clock or _Clock())


# --------------------------------------------------------------------------
# frames and markers
# --------------------------------------------------------------------------


def test_every_frame_has_an_ascii_twin_and_all_of_an_entry_are_one_width():
    """A frame wider than its neighbour shifts the whole row it sits in."""
    for name, moving, still in _FRAMES:
        assert all(ord(ch) < 128 for frame in still for ch in frame), name
        assert len({width(frame) for frame in moving}) == 1, name
        assert len({width(frame) for frame in still}) == 1, name


def test_the_counting_spinner_is_exactly_as_wide_as_the_ellipsis_it_replaces():
    for unicode_ok in (True, False):
        g = Glyphs(unicode_ok)
        assert width(g.dots[0]) == width(g.ellipsis)
        assert width(g.spin[0]) == 1


def test_a_marker_takes_no_column_and_leaves_no_trace_in_plain_text():
    _style_, moving = _moving()
    cell = moving.spin("/lab/a", "…")
    assert width(cell) == 1
    assert plain(cell) == "…"
    assert truncate(cell, 1) == cell, "a marked cell that fits is not cut"


def test_stripping_leaves_the_content_of_every_kind_of_span():
    clock = _Clock()
    _style_, moving = _moving(clock=clock)
    spans = [
        moving.spin("k", "…"),
        moving.flash("k", clock(), "12G"),
        moving.shine("k", "title", since=clock()),
        moving.text("k", "status", lambda cells, now: "x"),
    ]
    assert motion.strip_marks("|".join(spans)) == "…|12G|title|status"
    assert motion.strip_marks("torn \033[?7700;1") == "torn "
    assert motion.strip_marks("plain") == "plain"


# --------------------------------------------------------------------------
# the honest spinner
# --------------------------------------------------------------------------


def _past_the_delay(moving, clock, lines):
    """The first frame a key is in flight, then time enough for it to show."""
    moving.overlay(lines)
    clock.tick(motion.SPIN_DELAY_S + motion.FRAME_S / 2)


def test_only_the_key_in_flight_turns_and_the_rest_stay_still():
    clock = _Clock()
    style, moving = _moving(clock=clock)
    count = [0]
    moving.follow(lambda: ("/lab/a", count[0]))
    line = moving.spin("/lab/a", "…") + " " + moving.spin("/lab/b", "…")
    _past_the_delay(moving, clock, [line])
    first = moving.overlay([line])[0]
    assert motion.MARK not in first
    live, queued = plain(first).split(" ")
    assert live in style.g.dots, "the row being counted shows a spinner"
    assert queued == "…", "a row waiting its turn keeps the still ellipsis"


def test_the_spinner_steps_only_when_the_count_moved():
    """A spinner on a timer keeps turning on a hung mount."""
    clock = _Clock()
    _style_, moving = _moving(clock=clock)
    count = [0]
    moving.follow(lambda: ("/lab/a", count[0]))
    line = moving.spin("/lab/a", "…")
    _past_the_delay(moving, clock, [line])
    seen = []
    for _ in range(5):
        clock.tick()
        seen.append(plain(moving.overlay([line])[0]))
    assert len(set(seen)) == 1, "nothing read: the spinner stands still"
    for _ in range(5):
        count[0] += 100
        clock.tick()
        seen.append(plain(moving.overlay([line])[0]))
    assert len(set(seen[5:])) == 5, "each frame that read something is one step on"


def test_a_spinner_with_no_counter_turns_on_the_clock():
    clock = _Clock()
    _style_, moving = _moving(clock=clock)
    moving.follow(lambda: ("/lab/a", None))
    line = moving.spin("/lab/a", "…")
    _past_the_delay(moving, clock, [line])
    seen = set()
    for _ in range(4):
        clock.tick()
        seen.add(plain(moving.overlay([line])[0]))
    assert len(seen) == 4


def test_the_live_spinner_is_accent_outside_the_band_and_bare_inside_it():
    clock = _Clock()
    style, moving = _moving(clock=clock)
    moving.follow(lambda: ("/lab/a", 1))
    cell = style.dim(moving.spin("/lab/a", "…"))
    _past_the_delay(moving, clock, [cell])
    outside = moving.overlay([cell])[0]
    assert style._sgr(motion._PALETTE["accent"]) in outside
    band = interactive.highlight(["   a/  " + cell], 0)[0]
    inside = moving.overlay([band])[0]
    assert "\033[38;" not in inside, "a colour inside the band paints a block"
    assert plain(inside).strip()[-1] in style.g.dots


def test_a_count_that_ends_quickly_never_spins():
    """A first pass gives 121 folders a quarter of a second each: a spinner
    showing for a frame and then jumping to the next row is flicker."""
    clock = _Clock()
    _style_, moving = _moving(clock=clock)
    key = ["/lab/a"]
    moving.follow(lambda: (key[0], 5))
    rows = [moving.spin("/lab/%s" % name, "…") for name in "abcd"]
    shown = []
    for name in "abcd":
        key[0] = "/lab/%s" % name
        for _ in range(2):
            shown.append(plain(" ".join(moving.overlay(rows))))
            clock.tick()
    assert set(shown) == {"… … … …"}, "each was in flight too briefly to spin"
    for _ in range(int(motion.SPIN_DELAY_S / motion.FRAME_S) + 1):
        clock.tick()
        shown.append(plain(" ".join(moving.overlay(rows))))
    assert shown[-1].split()[-1] != "…", "one that stays in flight starts to turn"


def test_the_band_keeps_a_spinner_and_drops_a_fade():
    clock = _Clock()
    _style_, moving = _moving(clock=clock)
    line = "a " + moving.spin("k", "…") + " " + moving.flash("k", clock(), "\033[31m12G\033[0m")
    kept = motion.keep_spins(line)
    assert "\033[?7700;%d;" % (motion.SPIN,) in kept
    assert "\033[?7700;%d;" % (motion.FLASH,) not in kept
    assert "\033[31m" not in kept
    assert plain(kept) == plain(line)


# --------------------------------------------------------------------------
# fades, shines and text
# --------------------------------------------------------------------------


def test_a_landed_figure_fades_smoothly_and_then_is_itself():
    clock = _Clock()
    style, moving = _moving(clock=clock)
    content = style.info("12") + style.dim("G")
    since = clock()
    cell = moving.flash("/lab/a", since, content)
    tones = []
    # Mid-step, so no step boundary is left to floating point.
    for step in range(4):
        clock.now = since + (step + 0.5) * motion.FLASH_S / 4
        shown = moving.overlay([cell])[0]
        assert plain(shown) == "12G"
        tones.append(shown)
    assert len(set(tones)) == 4
    clock.now = since + motion.FLASH_S * 1.1
    assert moving.overlay([cell])[0] == content, "once faded, exactly the cell as drawn"


@pytest.mark.parametrize("depth", [8, 4, 0])
def test_a_fade_degrades_with_the_colour_depth(depth):
    clock = _Clock()
    style, moving = _moving(depth=depth, clock=clock)
    content = style.info("12G")
    cell = moving.flash("/lab/a", clock(), content)
    if depth == 0:
        assert cell == content, "no colour: nothing to fade, so no span at all"
        return
    assert moving.overlay([cell])[0] != content
    clock.tick(motion.FLASH_S)
    assert moving.overlay([cell])[0] == content


def test_a_figure_that_landed_long_ago_is_not_marked_at_all():
    clock = _Clock()
    style, moving = _moving(clock=clock)
    assert moving.flash("/lab/a", clock() - 2 * motion.FLASH_S, "12G") == "12G"


def test_a_shine_runs_once_and_the_edge_glow_keeps_coming_back():
    clock = _Clock()
    style, moving = _moving(clock=clock)
    title = style.head("/project/lab") + style.muted(" · 3T in 9 folders")
    once = moving.shine("title", title, since=clock())
    clock.tick(motion.SHINE_S / 2)
    lit = moving.overlay([once])[0]
    assert plain(lit) == plain(title) and lit != title
    clock.tick(motion.SHINE_S)
    assert moving.overlay([once])[0] == title

    edge = moving.shine("border", style.info("━" * 40), loop=True)
    frames = []
    for _ in range(80):
        frames.append(moving.overlay([edge])[0])
        clock.tick()
    assert all(plain(f) == "━" * 40 for f in frames)
    assert len(set(frames)) > 5, "the glow moves along the edge"
    rest = style.info("━" * 40)
    assert frames.count(rest) > 10, "and rests between passes, drawn exactly as it was"


def test_a_text_span_is_redrawn_to_its_own_width_and_a_failure_shows_as_drawn():
    clock = _Clock()
    _style_, moving = _moving(clock=clock)
    wide = moving.text("status", "x" * 10, lambda cells, now: "y" * 30)
    assert moving.overlay([wide])[0] == "y" * 10
    short = moving.text("status2", "x" * 10, lambda cells, now: "z")
    assert moving.overlay([short])[0] == "z" + " " * 9

    def broken(cells, now):
        raise RuntimeError("the source broke")

    kept = moving.text("status3", "as drawn", broken)
    assert moving.overlay([kept])[0] == "as drawn"


def test_animating_is_answered_from_what_the_last_frame_held():
    clock = _Clock()
    style, moving = _moving(clock=clock)
    moving.overlay(["nothing moves here"])
    assert not moving.animating(running=True)
    still = moving.spin("/lab/a", "…")
    moving.overlay([still])
    assert not moving.animating(running=False), "a still ellipsis, and nothing running"
    assert moving.animating(running=True), "a walk may reach it next"
    moving.overlay([moving.flash("/lab/a", clock(), "12G")])
    assert moving.animating()
    clock.tick(motion.FLASH_S)
    moving.overlay(["12G"])
    assert not moving.animating()
    moving.burst(motion.GROW_S)
    assert moving.animating() and moving.bursting()
    assert moving.until_next() <= motion.BURST_FRAME_S


# --------------------------------------------------------------------------
# pace
# --------------------------------------------------------------------------


def test_the_meter_reads_rate_history_and_a_stall():
    meter = motion.Meter()
    t = 100.0
    for step in range(10):
        meter.sample("/lab/a", t + step, step * 1000)
    now = t + 9
    assert meter.rate(now) == pytest.approx(1000.0)
    assert meter.history(now)[-1] == 1000
    assert meter.still(now) == 0
    assert meter.still(now + 6) == 6, "no new sample: the count has stood still"
    meter.sample("/lab/b", now + 7, 5)
    assert meter.rate(now + 7) is None, "a new folder starts over"


def test_the_sparkline_grows_in_and_zero_is_the_floor():
    glyphs = Glyphs(True).spark
    assert motion.sparkline([None, None, 5, 10], glyphs) == glyphs[4] + glyphs[-1]
    assert motion.sparkline([0, 0, 0], glyphs) == glyphs[0] * 3
    assert motion.sparkline([1, 1000], glyphs)[0] == glyphs[1], "anything read is off the floor"
    assert motion.sparkline([None, None], glyphs) == ""


def test_the_stopwatch():
    assert motion.stopwatch(24.9) == "0:24"
    assert motion.stopwatch(723) == "12:03"
    assert motion.stopwatch(3723) == "1:02:03"


# --------------------------------------------------------------------------
# when motion is allowed at all
# --------------------------------------------------------------------------


def test_each_switch_turns_motion_off():
    assert motion.allowed(env={"TERM": "xterm"})
    assert not motion.allowed(no_motion=True, env={"TERM": "xterm"})
    assert not motion.allowed(agent=True, env={"TERM": "xterm"})
    assert not motion.allowed(env={"TERM": "xterm", "DIRSCAPE_NO_MOTION": "1"})
    assert not motion.allowed(env={"TERM": "dumb"})


def test_a_stream_that_is_not_a_terminal_is_never_the_foreground():
    class Stream(object):
        def isatty(self):
            return False

    assert not motion.foreground(Stream())
    assert not motion.foreground(object())


# --------------------------------------------------------------------------
# the terminal around the view
# --------------------------------------------------------------------------


def _chrome(**env):
    written = []
    return motion.Chrome(written.append, env=env), written


def test_the_title_is_saved_once_changed_and_put_back():
    chrome, written = _chrome(TERM="xterm-256color")
    chrome.title("ds")
    chrome.title("ds")
    chrome.title("ds · counting 3/9 · lab")
    assert written == [
        "\033[22;0t\033]0;ds\007",
        "\033]0;ds · counting 3/9 · lab\007",
    ]
    chrome.close()
    assert written[-1] == "\033[23;0t"
    chrome.close()
    assert written.count("\033[23;0t") == 1


@pytest.mark.parametrize(
    "env",
    [
        {"TERM": "screen-256color"},
        {"TERM": "xterm-256color", "TMUX": "/tmp/tmux-1/default,1,0"},
        {"TERM": "xterm-256color", "STY": "1.pts-0"},
    ],
)
def test_nothing_reaches_a_terminal_that_is_not_the_one_being_addressed(env):
    chrome, written = _chrome(LC_TERMINAL="iTerm2", WT_SESSION="x", **env)
    chrome.title("ds")
    chrome.progress(0.5)
    chrome.notify("done")
    chrome.close()
    assert written == []


def test_a_console_that_is_no_emulator_gets_no_title():
    chrome, written = _chrome(TERM="linux")
    chrome.title("ds")
    chrome.close()
    assert written == []


def test_the_progress_bar_only_where_it_is_one():
    """OSC 9 is a progress bar to Windows Terminal and a notification to iTerm2."""
    iterm, written = _chrome(TERM="xterm-256color", TERM_PROGRAM="iTerm.app")
    iterm.progress(0.5)
    assert written == []
    wt, written = _chrome(TERM="xterm-256color", WT_SESSION="abc")
    wt.progress(None, busy=True)
    wt.progress(0.5)
    wt.progress(0.5)
    wt.progress(None)
    wt.progress(None)
    assert written == ["\033]9;4;3;0\007", "\033]9;4;1;50\007", "\033]9;4;0;0\007"]


@pytest.mark.parametrize(
    "env,expected",
    [
        ({"TERM_PROGRAM": "iTerm.app"}, "\033]9;lab counted, in 2:05\007"),
        ({"TERM": "xterm-kitty"}, "\033]99;;lab counted, in 2:05\033\\"),
        ({"TERM": "xterm-ghostty"}, "\033]777;notify;dirscape;lab counted, in 2:05\033\\"),
        ({"TERM": "xterm-256color", "DIRSCAPE_BELL": "1"}, "\a"),
        ({"TERM": "xterm-256color"}, None),
    ],
)
def test_a_notification_goes_where_the_terminal_takes_one(env, expected):
    chrome, written = _chrome(**env)
    chrome.notify("lab counted; in 2:05\x07")
    assert written == ([expected] if expected else [])


# --------------------------------------------------------------------------
# the startup board
# --------------------------------------------------------------------------


class _Completed(object):
    def __init__(self, returncode=0, timed_out=False, not_found=False):
        self.returncode, self.timed_out, self.not_found = returncode, timed_out, not_found


def _board(clock=None, **kw):
    written = []
    style = _style(depth=0)
    board = motion.Board(style, written.append, clock=clock or _Clock(), **kw)
    return board, written


def test_the_board_follows_the_sweep_stage_by_stage():
    clock = _Clock()
    board, _written = _board(clock)
    for label in ("config", "mounts", "plugins"):
        board.stage(label)
    board.note("mounts", "6 filesystems")
    six = [board.started(["/usr/lpp/mmfs/bin/mmlsquota", "-Y", "fs%d" % i]) for i in range(6)]
    wrapper = board.started(["/software/bin/quota"])
    clock.tick(1.2)
    for record in six:
        board.finished(record, _Completed())
    text = plain("\n".join(board.lines()))
    assert "✓ mounts" in text and "6 filesystems" in text
    assert "mmlsquota ×6 · quota" in text
    board.finished(wrapper, _Completed(returncode=1))
    board.stage("quota")
    board.stage("allocations")
    lines = [plain(line) for line in board.lines()]
    quota = next(line for line in lines if " quota " in line and "mmlsquota" in line)
    assert "did not answer" not in quota, "a non-zero exit is an answer"
    assert not any("allocations" in line for line in lines), "no plugin: no line"


def test_a_command_that_never_answered_is_named_and_the_slowest_after_a_while():
    clock = _Clock()
    board, _written = _board(clock, allowance=20.0)
    for label in ("config", "mounts", "plugins"):
        board.stage(label)
    record = board.started(["lfs", "quota", "-u", "me", "/lus/eagle"])
    clock.tick(motion.SLOW_S + 1)
    lines = [plain(line) for line in board.lines()]
    assert any("slowest: lfs quota, 6.0s of the 20s allowance" in line for line in lines)
    board.finished(record, _Completed(timed_out=True))
    assert any("did not answer" in plain(line) for line in board.lines())


def test_a_thread_that_claims_a_phase_owns_its_commands():
    clock = _Clock()
    board, _written = _board(clock)
    for label in ("config", "mounts", "plugins"):
        board.stage(label)
    done = threading.Event()

    def listing():
        board.claim("allocations")
        board.finished(board.started(["/software/bin/accounts", "storage"]), _Completed())
        board.release("allocations")
        done.set()

    worker = threading.Thread(target=listing)
    worker.start()
    worker.join(5)
    assert done.is_set()
    lines = [plain(line) for line in board.lines()]
    allocations = next(line for line in lines if "allocations" in line)
    assert "accounts storage" in allocations
    assert not any("accounts storage" in line for line in lines if " quota " in line)


def test_nothing_is_drawn_before_the_delay_and_nothing_is_left_after():
    clock = _Clock()
    board, written = _board(clock, delay=30.0)
    board.start()
    board.close()
    assert written == [], "a fast start stays exactly as it was"

    board, written = _board(delay=0.0)
    board.start()
    deadline = threading.Event()
    for _ in range(100):
        if written:
            break
        deadline.wait(0.02)
    board.close()
    assert written and written[-1] == "\r\033[K", "the line goes before the answer"


def test_a_framed_board_stays_for_the_table_when_asked():
    out = []
    screen = interactive.Screen(write=out.append)
    board = motion.Board(_style(depth=0), out.append, screen=screen, delay=0.0)
    board.start()
    for _ in range(100):
        if screen.lines:
            break
        threading.Event().wait(0.02)
    board.close(keep=True)
    assert screen.lines, "kept: the table draws over it"
    assert not any(chunk.endswith("\033[J") for chunk in out[-1:])
    board.close()
    assert screen.lines, "closing twice changes nothing"


def test_a_board_that_cannot_draw_costs_its_frames_and_nothing_else():
    def broken(_text):
        raise RuntimeError("stderr is gone")

    board = motion.Board(_style(depth=0), broken, delay=0.0)
    board.start()
    threading.Event().wait(0.1)
    board.close()


def test_the_board_line_is_ascii_under_ascii():
    clock = _Clock()
    style = _style(depth=0, unicode_ok=False)
    board = motion.Board(style, lambda text: None, clock=clock)
    for label in ("config", "mounts", "plugins"):
        board.stage(label)
    for i in range(3):
        board.started(["mmlsquota", "-Y", "fs%d" % i])
    text = board.line() + "\n".join(board.lines())
    assert all(ord(ch) < 128 for ch in text)
    assert "mmlsquota x3" in text


# --------------------------------------------------------------------------
# the top edge as a progress bar
# --------------------------------------------------------------------------


@pytest.mark.parametrize("depth", [24, 0])
def test_the_top_edge_fills_with_the_heavy_rule_and_keeps_its_width(depth):
    style = _style(depth=depth)
    plain_edge = plain(panel(["x"], style=style, size=42, shrink=False).splitlines()[0])
    half = plain(panel(["x"], style=style, size=42, shrink=False, progress=0.5).splitlines()[0])
    assert len(half) == len(plain_edge) == 42
    assert half.count("━") == 20 and half.count("─") == 20


def test_the_glow_rides_the_edge_only_while_it_grows_and_only_in_truecolor():
    clock = _Clock()
    style, moving = _moving(clock=clock)
    live = style.moving(moving)
    growing = panel(["x"], style=live, size=42, shrink=False, progress=0.5).splitlines()[0]
    assert motion.MARK in growing
    full = panel(["x"], style=live, size=42, shrink=False, progress=1.0).splitlines()[0]
    assert motion.MARK not in full
    grey = _style(depth=0).moving(moving)
    assert motion.MARK not in panel(["x"], style=grey, size=42, shrink=False, progress=0.5)


def test_a_moving_style_is_a_copy_that_leaves_the_original_still():
    style = _style()
    moving = motion.Motion(style)
    live = style.moving(moving)
    assert live.motion is moving and style.motion is None
    assert (live.depth, live.g, live.size) == (style.depth, style.g, style.size)


def test_the_glow_is_soft_slow_and_never_moved_by_the_bar_growing_under_it():
    """The streak it replaced looped every half second on a short bar, five
    cells a frame, and measured, the edge changed on every frame."""
    clock = _Clock()
    style, moving = _moving(clock=clock)
    positions = []
    for filled in (16, 17, 40):
        lit = []
        for _ in range(200):
            edge = moving.shine("border", style.info("━" * filled), loop=True, track=120)
            shown = moving.overlay([edge])[0]
            cells = list(motion._cells(shown))
            lit.append(
                tuple(i for i, (state, _ch) in enumerate(cells) if "38;2;144;166;247" not in state)
            )
            clock.tick(motion.SMOOTH_FRAME_S)
        positions.append(lit)
        clock.now -= 200 * motion.SMOOTH_FRAME_S
    short, longer, longest = positions
    assert all(a == b[: len(a)] or set(a) <= set(b) for a, b in zip(short, longer))
    steps = [abs(min(b) - min(a)) for a, b in zip(longest, longest[1:]) if a and b]
    assert steps and max(steps) <= 2, "a cell or so a frame, never a jump"


def test_a_glow_needs_truecolor_to_be_smooth():
    for depth in (8, 4):
        style, moving = _moving(depth=depth)
        assert moving.shine("border", "━" * 20, loop=True) == "━" * 20
        assert moving.shine("title", "/lab", since=moving.now()) == "/lab"


def test_frames_come_sooner_only_while_a_glow_moves():
    """Just past a frame boundary, where the time left is the whole step."""
    clock = _Clock(now=10000.01)
    style, moving = _moving(clock=clock)
    moving.overlay(["still"])
    assert moving.until_next() > motion.SMOOTH_FRAME_S
    since = clock.now - motion.SHINE_S / 2
    moving.overlay([moving.shine("title", style.head("/lab/data/archive"), since=since)])
    assert moving.until_next() <= motion.SMOOTH_FRAME_S


def test_a_spinner_steps_at_most_once_a_frame_however_often_frames_come():
    clock = _Clock()
    _style_, moving = _moving(clock=clock)
    count = [0]
    moving.follow(lambda: ("/lab/a", count[0]))
    line = moving.spin("/lab/a", "…")
    _past_the_delay(moving, clock, [line])
    seen = []
    for _ in range(20):
        count[0] += 1
        clock.tick(motion.SMOOTH_FRAME_S)
        seen.append(plain(moving.overlay([line])[0]))
    changes = sum(1 for a, b in zip(seen, seen[1:]) if a != b)
    assert changes <= 10, "twenty frames in a second, and no more than ten steps"


def test_the_rate_and_the_sparkline_move_on_whole_seconds():
    meter = motion.Meter()
    now = 100.0
    shown = []
    while now < 104.0:
        meter.sample("/lab/a", now, int((now - 100.0) * 1000))
        shown.append((meter.rate(now), tuple(meter.history(now))))
        now += 0.1
    changes = sum(1 for a, b in zip(shown, shown[1:]) if a != b)
    assert changes <= 4, "once a second, not every frame"
