"""The command line, and specifically the defects integration found.

Every test below pins a bug that unit tests on the individual packages could
not have caught, because each one lived in the WIRING: a backend was right
about a quota row and the consumer misapplied it. They are the most valuable
tests in the suite for that reason, and they are written from the live runs
that produced the wrong output.

Nothing here touches the cluster. The helpers under test are pure, and the one
end-to-end check replays a captured transcript.
"""

import argparse

import pytest

from dirscape import cli
from dirscape.model import (
    QuotaRow,
    QuotaSnapshot,
    Reach,
    Root,
    VerdictCategory,
    confirmed,
    refuted,
    unknown,
)


def _root(path, fileset="", device="meadow3_cap", writable=False, reach=Reach.LISTABLE):
    root = Root(path, role="project", device=device, fstype="gpfs")
    root.fileset = fileset
    root.reach = reach
    root.present = confirmed()
    root.mounted = confirmed()
    root.writable = confirmed() if writable else unknown(VerdictCategory.NOT_PROBED)
    return root


# --------------------------------------------------------------------------
# The quota leak: the worst defect integration found
# --------------------------------------------------------------------------


def test_a_guessed_mount_does_not_leak_onto_sibling_directories():
    """The headline integration bug.

    A `project-hpc` row whose mount was only INFERRED as `/project` matches
    every `/project/*` path by prefix, so the first live run reported
    `/project/abe`, `/project/bard`, `/project/dahlias`, `/project/mdgreenwood`
    and `/project/pelican` as each holding 11T, which is a different PI's
    usage entirely. Attribution goes through the fileset, never the prefix.
    """
    row = QuotaRow("project-hpc", "blocks", "user", 11765797488, mount="/project", guessed=True)
    snap = QuotaSnapshot("mmlsquota", [row])

    mine = _root("/project/hpc", fileset="project-hpc")
    theirs = _root("/project/abe", fileset="project-abe")

    assert cli._rows_governing(snap, mine) == [row]
    assert cli._rows_governing(snap, theirs) == [], "another fileset must get nothing"


def test_an_unknown_fileset_requires_an_exact_mount_match():
    """With no fileset to match on, prefix inheritance is refused.

    Reporting nothing renders `?`, which is the honest answer for a path whose
    quota could not be attributed. Reporting the parent's figure would be a
    number that is true of something else.
    """
    row = QuotaRow("cap", "blocks", "user", 500, mount="/project")
    snap = QuotaSnapshot("mmlsquota", [row])

    exact = _root("/project", fileset="")
    below = _root("/project/anything", fileset="")

    assert cli._rows_governing(snap, exact) == [row]
    assert cli._rows_governing(snap, below) == []


# --------------------------------------------------------------------------
# The fileset-name collision
# --------------------------------------------------------------------------


def test_the_fileset_map_is_keyed_on_device_and_not_on_the_bare_name():
    """`scratch` is a fileset name on three devices here.

    Keyed on the name alone, the collie3 scratch claimed the junction for all
    of them and `/scratch/meadow3/jdoe42` printed `?` while holding 22G. This
    is the collision `QuotaRow.label` qualifies against, made in the consumer
    after the model had already warned about it in a docstring.
    """
    run = cli.Run()
    run.roots = [
        _root("/scratch/meadow3/jdoe42", "scratch", device="meadow3_perf", writable=True),
        _root("/scratch/collie3/jdoe42", "scratch", device="collie3_perf", writable=True),
    ]
    for root in run.roots:
        device = root.device
        row = QuotaRow("scratch", "blocks", "user", 100, mount=root.path, device=device)
        run.quota_attempts.append(QuotaSnapshot("mmlsquota", [row]))

    cli._attach_quota(run, budget=None, runner=None)

    for root in run.roots:
        assert root.quota is not None, "%s lost its quota to a name collision" % (root.path,)


def test_a_user_scoped_row_lands_on_the_highest_writable_point():
    """Not the deepest, which was tried and is absurd.

    In fileset `project-hpc` the deepest writable path is
    `/project/hpc/jdoe42/.cache/tmp`, so an 11T figure landed on a cache
    directory. The figure describes a subtree, so it belongs at the top of it.
    """
    run = cli.Run()
    top = _root("/project/hpc", "project-hpc", writable=True)
    mid = _root("/project/hpc/jdoe42", "project-hpc", writable=True)
    deep = _root("/project/hpc/jdoe42/.cache/tmp", "project-hpc", writable=True)
    run.roots = [deep, mid, top]

    row = QuotaRow("project-hpc", "blocks", "user", 11_000_000, mount="/project", guessed=True)
    run.quota_attempts = [QuotaSnapshot("mmlsquota", [row])]

    cli._attach_quota(run, budget=None, runner=None)

    assert top.quota is not None
    assert mid.quota is None
    assert deep.quota is None, "a cache directory must not carry the subtree's figure"


def test_a_subdirectory_is_told_where_its_figure_lives():
    """Rather than silently showing nothing.

    `?` alone would read as "this could not be measured" when in fact it was
    measured and belongs one level up.
    """
    run = cli.Run()
    top = _root("/project/hpc", "project-hpc", writable=True)
    sub = _root("/project/hpc/jdoe42", "project-hpc", writable=True)
    run.roots = [top, sub]
    row = QuotaRow("project-hpc", "blocks", "user", 5, mount="/project", guessed=True)
    run.quota_attempts = [QuotaSnapshot("mmlsquota", [row])]

    cli._attach_quota(run, budget=None, runner=None)

    assert any("quota is a property of fileset" in note for note in sub.notes)


# --------------------------------------------------------------------------
# Allocation rows
# --------------------------------------------------------------------------


def test_an_allocation_without_a_path_shows_its_location():
    """Six ELSEWHERE rows printed `?` in every column and were identical.

    The location is deliberately not turned into a path, but rendering it as
    `?` throws away the only identifying thing about the row.
    """
    from dirscape.render import atlas

    root = Root("", role="", device="", fstype="")
    root.policy = {"allocation_location": "cfs4/hpc-staff"}
    cell = atlas._path_cell(root)
    assert "cfs4/hpc-staff" in cell
    assert "location" in cell
    assert not cell.startswith("/"), "a location must not be dressed up as a path"


def test_a_root_with_neither_path_nor_location_is_unknown():
    from dirscape.render import atlas, fields

    assert atlas._path_cell(Root("")) == fields.UNKNOWN


def test_internal_bookkeeping_never_reaches_the_policy_column():
    """`rank=primary` appeared in the POLICY cell of every row.

    It is discovery's own bookkeeping, it tells a reader nothing, and it looks
    like a leaked internal because it is one.
    """
    from dirscape.render import fields

    root = _root("/project/hpc", "project-hpc")
    root.policy = {
        "rank": "primary",
        "allocation_location": "cfs4/hpc-staff",
        "allocation_gb": 25600.0,
        "allocation_accounts": ["hpc-staff"],
        "purge_days": 30,
    }
    merged = fields.merged_policy(root, site=None)

    for leaked in ("rank", "allocation_location", "allocation_gb", "allocation_accounts"):
        assert leaked not in merged
    assert merged["purge_days"] == 30, "real policy must survive the filter"

    cell = fields.policy_cell(root, site=None)
    assert "rank" not in cell
    assert "primary" not in cell


# --------------------------------------------------------------------------
# Argument handling
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "argv",
    [
        ["--json", "new"],
        ["new", "--json"],
        ["--json", "matrix"],
        ["matrix", "--json"],
    ],
)
def test_a_global_flag_works_on_either_side_of_the_verb(argv):
    """nodetop shipped the `parents=[...]` bug where `--json status` was
    silently dropped, because argparse stored one action object in two parsers
    and the later parse overwrote it. Fresh actions per parser fix it.
    """
    opts = cli.build_parser().parse_args(argv)
    assert cli._merge_flag(opts, "json", False) is True


def test_an_unset_flag_is_distinguishable_from_a_false_one():
    opts = cli.build_parser().parse_args(["matrix"])
    assert opts.json is None
    assert cli._merge_flag(opts, "json", False) is False
    assert cli._merge_flag(opts, "json", "sentinel") == "sentinel"


@pytest.mark.parametrize(
    "text,seconds",
    [
        ("90", 90.0),
        ("30s", 30.0),
        ("5m", 300.0),
        ("12h", 43200.0),
        ("30d", 2592000.0),
        ("2w", 1209600.0),
    ],
)
def test_parse_duration_accepts_the_documented_forms(text, seconds):
    assert cli.parse_duration(text) == seconds


@pytest.mark.parametrize("text", ["", "   ", "tomorrow", "-5", "5y", "d"])
def test_parse_duration_refuses_anything_else(text):
    with pytest.raises(ValueError):
        cli.parse_duration(text)


def test_the_site_template_is_printed_and_nothing_else_runs(capsys):
    """It must not need a cluster: an administrator runs it to GET one."""
    code = cli.main(["--site-template"])
    out = capsys.readouterr().out
    assert code == cli.EXIT_OK
    assert "[roots]" in out
    assert "dirscape site configuration" in out


def test_the_default_command_is_the_atlas():
    opts = cli.build_parser().parse_args([])
    assert opts.command is None
    opts = cli.build_parser().parse_args(["atlas"])
    assert opts.command == "atlas"


# --------------------------------------------------------------------------
# The first run
# --------------------------------------------------------------------------


def test_the_first_run_says_so_rather_than_calling_everything_new():
    """Birth time is absent on GPFS and directory mtime is measurably wrong by
    months, so the honest first-run answer is that there is no baseline.
    """

    class FakeChanges(object):
        no_baseline = True
        records = ()
        warnings = ()

        def __iter__(self):
            return iter(())

        def __len__(self):
            return 0

    run = cli.Run()
    run.roots = [_root("/project/hpc", "project-hpc")]
    run.changes = FakeChanges()
    opts = cli.build_parser().parse_args(["new"])

    text, code = cli._render(run, opts, "new", style=None, width=None)

    assert code == cli.EXIT_OK
    assert "No baseline yet" in text
    assert "nothing can honestly be called new" in text
    # No delta panel and no change labels, because there are no changes.
    assert "stranded" not in text
    assert "opened" not in text


def test_state_tracking_off_reports_that_rather_than_no_change():
    """ "Nothing changed" and "nothing was compared" are different answers."""
    run = cli.Run()
    run.roots = [_root("/project/hpc", "project-hpc")]
    run.changes = None
    opts = cli.build_parser().parse_args(["new", "--no-state"])

    text, code = cli._render(run, opts, "new", style=None, width=None)

    assert code == cli.EXIT_OK
    assert "--no-state" in text


# --------------------------------------------------------------------------
# Stranded
# --------------------------------------------------------------------------


def test_stranded_is_matched_on_the_fileset_and_not_on_a_path():
    """Stranded space has no reachable path BY DEFINITION.

    A path-keyed check would find nothing, which is why the comparison is on
    the fileset. Measured on one account: five `project-*` filesets holding
    about 19 GB with no group to reach them.
    """
    run = cli.Run()
    reachable = _root("/project/hpc", "project-hpc", reach=Reach.LISTABLE)
    closed = _root("/project/dahlias", "project-dahlias", reach=Reach.CLOSED)
    run.roots = [reachable, closed]

    rows = [
        QuotaRow("project-hpc", "blocks", "user", 10, device="meadow3_cap"),
        QuotaRow("project-dahlias", "blocks", "user", 11_700_000, device="meadow3_cap"),
    ]
    run.quota_attempts = [QuotaSnapshot("mmlsquota", rows)]

    count = cli._mark_stranded(run)

    assert count == 1
    assert closed.stranded is True
    assert reachable.stranded is False


def test_a_traversable_fileset_is_not_stranded():
    """Traverse-only still means you can reach a path you know."""
    run = cli.Run()
    root = _root("/project/marsh", "project-marsh", reach=Reach.TRAVERSE)
    run.roots = [root]
    run.quota_attempts = [
        QuotaSnapshot(
            "mmlsquota",
            [QuotaRow("project-marsh", "blocks", "user", 5, device="meadow3_cap")],
        )
    ]

    cli._mark_stranded(run)

    assert root.stranded is False


# --------------------------------------------------------------------------
# The default view: what earns a row
# --------------------------------------------------------------------------


def _measured(root, used=1000, limit=2000):
    row = QuotaRow(root.fileset or "fs", "blocks", "user", used, hard=limit)
    root.quota = QuotaSnapshot("mmlsquota", [row])
    return root


def test_a_row_that_says_nothing_is_not_shown():
    """The first live run printed 63 rows of which 50 were `?` everywhere.

    Nine `/gpfs/<cluster>/<tier>` aliases, `/`, `/programs` and twenty dataset
    collections whose quota belongs to their parent. Fifty silent rows buried
    the six that mattered.
    """
    silent = _root("/gpfs/meadow3/cap", reach=Reach.LISTABLE)
    assert cli._says_something(silent) is False


@pytest.mark.parametrize(
    "prepare,why",
    [
        (lambda r: setattr(r, "writable", confirmed()), "you can put data here"),
        (lambda r: _measured(r), "a figure was measured"),
        (lambda r: setattr(r, "labels", ["new"]), "something changed"),
    ],
)
def test_a_row_earns_its_place_by_answering_a_question(prepare, why):
    root = _root("/project/lab", "project-lab")
    prepare(root)
    assert cli._says_something(root) is True, why


def test_a_summarised_row_is_not_also_tabled():
    """Stranded and elsewhere rows each get a one-line summary with their own
    subcommand. Listing them in the table too put eleven rows of other
    people's directories and pathless allocations above the four places this
    user can actually write.
    """
    stranded = _measured(_root("/project/dahlias", "project-dahlias", reach=Reach.CLOSED))
    stranded.stranded = True
    assert cli._says_something(stranded) is False

    elsewhere = Root("", role="archive")
    elsewhere.allocated = confirmed()
    elsewhere.mounted = refuted(VerdictCategory.NOT_MOUNTED_HERE)
    assert elsewhere.elsewhere is True
    assert cli._says_something(elsewhere) is False


def _mine(root):
    """Full access: listable and confirmed writable."""
    root.reach = Reach.LISTABLE
    root.writable = confirmed()
    return root


def test_the_highest_directory_you_fully_own_is_the_only_row():
    """If the whole tree is yours, the tree is the answer.

    Enumerating your own filing is not information. One row saying "this is
    yours" scales to a project with ten thousand subdirectories; a row per
    subdirectory does not.
    """
    top = _mine(_measured(_root("/project/xyz", "project-xyz")))
    children = [
        _mine(_root("/project/xyz/%s" % name, "project-xyz"))
        for name in ("data", "code", "runs/2026", "data/raw/batch1")
    ]

    kept, folded = cli._collapse_families([top] + children)

    assert [r.path for r in kept] == ["/project/xyz"]
    assert folded == 4
    assert top.policy["contains"] == 4


def test_when_the_parent_is_not_fully_yours_the_reachable_parts_are_shown():
    """The case that makes the rule right rather than merely short.

    A PI directory you can read but not write is not "yours", so folding your
    one writable subdirectory into it would hide the only place in that tree
    you can actually put data.
    """
    pi = _root("/project/theirs", "project-theirs", reach=Reach.LISTABLE)
    pi.writable = refuted(VerdictCategory.ACCESS_DENIED, "not in the group")
    mine = _mine(_measured(_root("/project/theirs/shared", "project-theirs")))

    kept, folded = cli._collapse_families([pi, mine])

    assert [r.path for r in kept] == ["/project/theirs", "/project/theirs/shared"]
    assert folded == 0


def test_a_child_on_different_storage_is_never_folded():
    """`/scratch` is a plain directory holding three clusters' filesystems.

    Folding on path alone would hide two of them behind the first, which is
    the same conflation that makes `/scratch` not a filesystem.
    """
    parent = _mine(_measured(_root("/scratch", "scratch", device="meadow3_perf")))
    other = _mine(_measured(_root("/scratch/collie3/me", "scratch", device="collie3_perf")))

    kept, folded = cli._collapse_families([parent, other])

    assert len(kept) == 2, "a different device is different storage"
    assert folded == 0


def test_a_child_with_a_delta_is_never_folded():
    """The count is a summary of silence, not of content."""
    top = _mine(_measured(_root("/project/xyz", "project-xyz")))
    changed = _mine(_root("/project/xyz/new", "project-xyz"))
    changed.labels = ["new"]

    kept, folded = cli._collapse_families([top, changed])

    assert "/project/xyz/new" in [r.path for r in kept]
    assert folded == 0


def test_a_stranded_child_is_never_folded():
    top = _mine(_measured(_root("/project/xyz", "project-xyz")))
    held = _root("/project/xyz/gone", "project-xyz", reach=Reach.CLOSED)
    held.stranded = True

    kept, _ = cli._collapse_families([top, held])

    assert "/project/xyz/gone" in [r.path for r in kept]


def test_the_figure_lands_on_the_row_the_table_keeps():
    """These two rules have to agree or the number disappears.

    The quota once attached to `/project/hpc/jdoe42` on an ownership
    preference while the table kept `/project/hpc`, so the figure was folded
    out of sight and the row fell back to showing the filesystem's free space.
    """
    run = cli.Run()
    top = _mine(_root("/project/hpc", "project-hpc"))
    sub = _mine(_root("/project/hpc/jdoe42", "project-hpc"))
    run.roots = [sub, top]
    row = QuotaRow("project-hpc", "blocks", "user", 11_000_000, mount="/project", guessed=True)
    run.quota_attempts = [QuotaSnapshot("mmlsquota", [row])]

    cli._attach_quota(run, budget=None, runner=None)
    kept, _ = cli._collapse_families(run.roots)

    assert top.quota is not None, "the figure belongs on the row that survives"
    assert [r.path for r in kept] == ["/project/hpc"]


def test_the_default_view_orders_by_role_not_by_mount_table():
    """Your own space first, shared data after it, machinery last.

    Mount-table order interleaves `/gpfs/collie3/cap` with your home
    directory, which is the order the kernel happens to list mounts in and
    carries nothing for a reader.
    """
    run = cli.Run()
    run.roots = [
        _measured(_root("/tmp", "", device="local")),
        _measured(_root("/scratch/x/me", "scratch")),
        _measured(_root("/home/me", "home")),
        _measured(_root("/project/lab", "project-lab")),
    ]
    for root in run.roots:
        root.role = cli.load_site(paths=[]).role_for(root.path)

    shown, _ = cli._visible(run, show_all=False)

    assert [r.role for r in shown] == ["home", "project", "scratch", "local"]


def test_show_all_holds_nothing_back():
    run = cli.Run()
    run.roots = [_root("/gpfs/meadow3/cap"), _measured(_root("/home/me", "home"))]
    shown, hidden = cli._visible(run, show_all=True)
    assert len(shown) == 2
    assert hidden == 0


def test_the_hidden_count_is_reported_rather_than_silent():
    run = cli.Run()
    run.roots = [_root("/gpfs/meadow3/cap"), _measured(_root("/home/me", "home"))]
    shown, hidden = cli._visible(run, show_all=False)
    assert [r.path for r in shown] == ["/home/me"]
    assert hidden == 1


def test_a_filter_that_hides_everything_falls_back_to_showing_something():
    """A user with no measurable storage still needs to see what was found."""
    run = cli.Run()
    run.roots = [_root("/gpfs/meadow3/cap"), _root("/programs")]
    shown, _ = cli._visible(run, show_all=False)
    assert shown, "an empty table is never the right answer"


# --------------------------------------------------------------------------
# The default view: how it looks
# --------------------------------------------------------------------------


def _render_default(roots, **kw):
    from dirscape.render import atlas

    return atlas.render(roots, group=True, **kw)


def test_a_repeated_role_is_printed_once():
    """Nine rows reading `project` is the table stuttering.

    A fourth root with a different role is included on purpose: with one role
    across every row the column is CONSTANT and gets dropped entirely, which
    is also correct but is a different rule and would make this test pass
    without exercising the blanking at all.
    """
    roots = [
        _measured(_root("/project/a", "fs-a")),
        _measured(_root("/project/b", "fs-b")),
        _measured(_root("/project/c", "fs-c")),
        _measured(_root("/home/me", "fs-home")),
    ]
    for root in roots[:3]:
        root.role = "project"
    roots[3].role = "home"

    text = _render_default(roots)

    # Counted in the ROLE COLUMN, not in the whole string: the word "project"
    # also appears inside `/project/a`, `/project/b` and `/project/c`, so a
    # naive `text.count` measures the paths and not the blanking.
    body = [ln for ln in text.splitlines() if "/" in ln and "PATH" not in ln]
    roles = [ln.split()[0] for ln in body if not ln.startswith(" ")]
    assert roles == ["project", "home"], "each role belongs on the first row of its run, got %r" % (
        roles,
    )
    assert sum(1 for ln in body if ln.startswith(" ")) == 2, "two rows inherit their role"


def test_figures_align_on_the_separator():
    """So the column reads as a set of magnitudes a reader can compare."""
    roots = [
        _measured(_root("/a", "fa"), used=876_543_210, limit=32_212_254_720),
        _measured(_root("/b", "fb"), used=11_000_000_000_000, limit=0),
        _measured(_root("/c", "fc"), used=950_272, limit=0),
    ]
    for root in roots:
        root.role = "project"

    # The header carries "USED / QUOTA", so rows are taken by their path.
    lines = [ln for ln in _render_default(roots).splitlines() if " / " in ln and "QUOTA" not in ln]
    assert len(lines) == 3
    offsets = {ln.index(" / ") for ln in lines}
    assert len(offsets) == 1, "every separator must sit in one column: %s" % (offsets,)


def test_a_column_with_one_value_everywhere_is_dropped():
    """`WHERE` reads `here` on every row of a filtered view, which spends nine
    characters of the window saying nothing.
    """
    roots = [_measured(_root("/a", "fa")), _measured(_root("/b", "fb"))]
    for root in roots:
        root.role = "project"
    text = _render_default(roots)
    assert "WHERE" not in text


def test_path_and_used_survive_even_when_constant():
    """They are the answer, not the context."""
    from dirscape.render import atlas

    rows = [["project", "/a", "here", "rwx", "1G / 2G", "?", "?"]] * 2
    constant = atlas._constant_columns(rows)
    assert atlas._PATH not in constant
    assert atlas._USED not in constant


def test_the_legend_is_off_unless_asked_for():
    """A legend reprinted on every run is read once and skipped forever."""
    roots = [_measured(_root("/a", "fa"))]
    roots[0].role = "home"
    assert "reach:" not in _render_default(roots)
    assert "reach:" in _render_default(roots, legend_on=True)


def test_the_summary_lines_count_the_unfiltered_roots():
    """Counting the filtered subset made the `elsewhere` line vanish and
    stripped the byte figure off the `stranded` line, because those are
    exactly the rows the default view holds back.
    """
    shown = [_measured(_root("/home/me", "home"))]
    shown[0].role = "home"

    held = _measured(_root("/project/dahlias", "project-dahlias"), used=11_700_000_000)
    held.stranded = True
    away = Root("", role="archive")
    away.allocated = confirmed()
    away.mounted = refuted(VerdictCategory.NOT_MOUNTED_HERE)

    text = _render_default(shown, all_roots=shown + [held, away])

    assert "dirscape stranded" in text
    assert "dirscape elsewhere" in text
    assert "11G" in text, "the stranded total must be summed and shown"


def test_a_capacity_figure_aligns_with_the_quota_figures():
    """The comparison that failed was `word == "free"` on a DIMMED cell.

    The cell is `\\x1b[38;5;248m886G free\\x1b[0m`, so the last token is
    `free\\x1b[0m` and the equality test silently missed, which is why the
    capacity rows were never aligned at all: `886G free` sat four columns in
    while every quota figure sat five. Nothing raised; they were just wrong.
    """
    import re

    from dirscape.render import atlas, resolve_style

    style = resolve_style(color="always", ascii_only=False, stream=None)

    quota = _measured(_root("/a", "fa"), used=950_272, limit=0)
    quota.role = "project"
    bare = _root("/b", "fb")
    bare.role = "project"
    bare.reach = Reach.LISTABLE
    bare.writable = confirmed()
    bare.policy = {"free_bytes": 951_000_000_000}

    text = atlas.render([quota, bare], style=style, group=True)

    plain = [re.sub(r"\033\[[0-9;?]*[A-Za-z]", "", ln) for ln in text.splitlines()]
    rows = [ln for ln in plain if "/a" in ln or "/b" in ln]
    assert len(rows) == 2

    # Both figures must END at the same column of the line. Anchored on the
    # figure itself rather than on a neighbouring column, because a column
    # whose value is identical on every row is dropped and an anchor there
    # disappears with it.
    edges = set()
    for line in rows:
        match = re.search(r"(\d[\d.]*[KMGTPB]?)(?= / | free)", line)
        assert match, "no figure found in %r" % (line,)
        edges.add(match.end())
    assert len(edges) == 1, "figures end at columns %s" % (sorted(edges),)


def test_why_does_not_print_one_line_per_symlink():
    """A home directory with eleven relocated dotfiles produced eleven
    near-identical `note` lines, which was most of a thirty-line screen.
    """
    run = cli.Run()
    root = _measured(_root("/home/me", "home"))
    root.role = "home"
    root.reach = Reach.LISTABLE
    root.writable = confirmed()
    for name in (".cache", ".local", ".conda", ".triton", ".codex", ".nv"):
        root.add_note(
            "/home/me/%s resolves to /project/lab/me/%s, so its contents are "
            "billed against /project/lab/me and not against this root" % (name, name)
        )
    run.roots = [root]

    from dirscape.render import resolve_style

    text, code = cli._why(run, "/home/me", resolve_style(color="never", stream=None))

    assert code == cli.EXIT_OK
    assert text.count("resolves to") == 0, "the per-symlink lines must be collapsed"
    assert "6 paths here are symlinks billed elsewhere" in text
    assert len(text.splitlines()) < 20, "why is a screen, not a transcript"


def test_why_omits_a_probe_that_never_ran():
    """`? allocated not probed` appeared on every mounted root.

    The allocation database is only consulted for storage with no path here,
    so a question mark against a question nobody asked teaches a reader to
    skip the column.
    """
    run = cli.Run()
    root = _measured(_root("/project/lab", "project-lab"))
    root.reach = Reach.LISTABLE
    root.writable = confirmed()
    run.roots = [root]

    from dirscape.render import resolve_style

    text, _ = cli._why(run, "/project/lab", resolve_style(color="never", stream=None))

    assert "allocated" not in text
    assert "mounted" in text


# --------------------------------------------------------------------------
# The bug-hunt round: things that were silently wrong
# --------------------------------------------------------------------------


def test_the_rank_filter_reads_the_place_discovery_writes():
    """It was `getattr(root, "rank", ...)` against a Root with no such
    attribute, so the default came back for every root and the whole rank
    filter did nothing. A 94G tmpfs was offered as somewhere to put data.
    """
    run = cli.Run()
    good = _measured(_root("/project/lab", "project-lab"))
    good.role = "project"
    good.policy = {"rank": "primary"}
    ram = _measured(_root("/dev/shm", "", device="tmpfs"))
    ram.role = "local"
    ram.policy = {"rank": "secondary"}
    run.roots = [good, ram]

    shown, hidden = cli._visible(run, show_all=False)

    assert [r.path for r in shown] == ["/project/lab"]
    assert hidden == 1
    assert len(cli._visible(run, show_all=True)[0]) == 2


def test_a_missing_rank_is_treated_as_primary():
    """A root discovery did not rank must not vanish."""
    run = cli.Run()
    root = _measured(_root("/project/lab", "project-lab"))
    root.role = "project"
    root.policy = {}
    run.roots = [root]
    assert [r.path for r in cli._visible(run, show_all=False)[0]] == ["/project/lab"]


def test_a_non_positive_timeout_is_refused():
    """It was accepted, and the run came back as sixty rows of `?`."""
    for value in ("0", "-5"):
        assert cli.main(["--timeout", value]) == cli.EXIT_USAGE


def test_snapshot_does_not_claim_to_have_recorded_anything_with_no_state():
    """It said "Recorded 0 root(s) as a baseline", which is a claim to have
    done the one thing `--no-state` exists to prevent.
    """
    run = cli.Run()
    run.roots = [_measured(_root("/project/lab", "project-lab"))]
    run.snapshot = None
    opts = cli.build_parser().parse_args(["snapshot", "--no-state"])

    text, code = cli._render(run, opts, "snapshot", style=None, width=None)

    assert code == cli.EXIT_USAGE
    assert "Nothing recorded" in text
    assert "Recorded 0" not in text


def test_why_on_a_path_that_does_not_exist():
    """It walked up, found `/`, and explained `/` with exit 0, so a reader
    asked about one path and was answered about another.
    """
    run = cli.Run()
    root = _measured(_root("/", "", device="rootfs"))
    run.roots = [root]

    from dirscape.render import resolve_style

    text, code = cli._why(run, "/definitely/not/here", resolve_style(color="never", stream=None))

    assert code == cli.EXIT_PATH
    assert "does not exist" in text


def test_why_does_not_second_guess_an_exact_root():
    """Re-checking the filesystem for an exact match made the tool contradict
    its own probe and print "X does not exist. The enclosing root is X".
    """
    run = cli.Run()
    # A synthetic root whose path is not on this disk: discovery already
    # settled that it is present, and `why` must trust that.
    root = _measured(_root("/synthetic/root/that/is/not/on/disk", "fs"))
    root.reach = Reach.LISTABLE
    root.writable = confirmed()
    run.roots = [root]

    from dirscape.render import resolve_style

    text, code = cli._why(
        run, "/synthetic/root/that/is/not/on/disk", resolve_style(color="never", stream=None)
    )

    assert code == cli.EXIT_OK
    assert "does not exist" not in text


def test_the_fold_count_is_actually_rendered():
    """It was stored in `policy["contains"]` and never displayed, so twenty
    dataset collections folded into one row and nothing on screen said so.
    """
    from dirscape.render import atlas

    root = _measured(_root("/project2/reference", "project2-reference"))
    root.policy = dict(root.policy or {})
    root.policy["contains"] = 20
    assert "+20" in atlas._path_cell(root)


def test_stranded_is_not_reported_as_a_change():
    """It is a standing condition the state layer re-emits on every run, so
    `dirscape new` showed the same five rows for ever under a heading that
    said "1 change since the baseline".
    """

    class Rec(object):
        def __init__(self, label, path):
            self.label = label
            self.path = path
            self.because = ""

    class Changes(object):
        no_baseline = False
        warnings = ()

        def __init__(self, records):
            self.records = records

        def __iter__(self):
            return iter(self.records)

        def __len__(self):
            return len(self.records)

    run = cli.Run()
    run.roots = [_measured(_root("/project/abe", "project-abe"))]
    run.changes = Changes([Rec("stranded", "/project/abe")])
    opts = cli.build_parser().parse_args(["new"])

    text, code = cli._render(run, opts, "new", style=None, width=None)

    assert code == cli.EXIT_OK
    assert "No change since the last run" in text
    assert "still hold space you cannot reach" in text


def test_an_argv_path_is_sanitised_before_it_is_printed():
    """A path from argv is foreign text, and this view printed it raw.

    Measured with a directory literally named
    `evil\\n/project/FORGED  999T  100%\\x1b[31m`: the newline broke the line
    and forged a table row inside `why`, and the escape sequence reached the
    terminal. `Root.path` is cleaned at construction; this string never was.
    """
    run = cli.Run()
    run.roots = [_measured(_root("/tmp", "", device="xfs"))]

    from dirscape.render import resolve_style

    hostile = "/tmp/evil\n/project/FORGED  999T  100%\x1b[31m"
    text, _ = cli._why(run, hostile, resolve_style(color="never", stream=None))

    assert "\n/project/FORGED" not in text, "a newline forged a row"
    assert "\x1b" not in text, "an escape sequence reached the output"
    assert "FORGED" in text, "the name itself is still shown, just defused"


def test_a_hostile_path_is_sanitised_in_the_ncdu_error_too():
    run = cli.Run()
    run.roots = [_measured(_root("/tmp", "", device="xfs"))]
    opts = cli.build_parser().parse_args(["ncdu", "/tmp/x\nFORGED"])

    from dirscape.render import resolve_style

    text, code = cli._render(run, opts, "ncdu", resolve_style(color="never", stream=None), None)

    assert code == cli.EXIT_PATH
    assert "\nFORGED" not in text


def test_a_failed_save_is_reported_rather_than_claimed():
    """`Lineage.save` returns False rather than raising when it cannot write,
    and only the raise was handled, so the run reported "a baseline has been
    recorded" while nothing reached the disk.
    """

    class Changes(object):
        no_baseline = True
        records = ()
        warnings = ()

        def __iter__(self):
            return iter(())

        def __len__(self):
            return 0

    run = cli.Run()
    run.roots = [_measured(_root("/project/lab", "project-lab"))]
    run.changes = Changes()
    run.saved = False
    run.warnings = ["the baseline was NOT saved to /nowhere/x.json"]
    opts = cli.build_parser().parse_args(["new"])

    text, code = cli._render(run, opts, "new", style=None, width=None)

    assert code == cli.EXIT_USAGE
    assert "could not save" in text
    assert "has been recorded" not in text


def test_run_warnings_reach_the_table():
    """They were collected into `Run.warnings` and only ever reached `--json`,
    so a malformed site.conf, a damaged baseline and a failed plugin were all
    silent in the view a user actually reads.
    """
    from dirscape.render import atlas

    root = _measured(_root("/project/lab", "project-lab"))
    root.role = "project"
    text = atlas.render(
        [root], group=True, warnings=["ignored malformed config /etc/dirscape/site.conf"]
    )
    assert "malformed config" in text


def test_the_stranded_summary_is_not_repeated_as_a_warning():
    """It already has its own alert line with a subcommand."""
    run = cli.Run()
    run.warnings = [
        "holds space in 5 fileset(s) with no reachable path: project-abe",
        "no baseline yet, seeded one from this run (61 roots)",
        "ignored malformed config /etc/dirscape/site.conf",
    ]
    surfaced = cli._surfaceable(run)
    assert surfaced == ["ignored malformed config /etc/dirscape/site.conf"]


@pytest.mark.parametrize("bad", ["bogus", "5y", "", "  ", "d", "-3"])
def test_a_bad_duration_names_what_the_user_typed(bad):
    """float's own message said "could not convert string to float: 'bogu'",
    having silently eaten the last character as a unit, which sends a reader
    looking for a typo they did not make.
    """
    with pytest.raises(ValueError) as caught:
        cli.parse_duration(bad)
    message = str(caught.value)
    if bad.strip():
        assert repr(bad.strip()) in message or "negative" in message, message


def test_an_ignored_since_flag_is_mentioned_rather_than_swallowed():
    """`dirscape new --since bogus` answered "No change since the last run"
    and never said the flag had been ignored, so a typo silently changed which
    baseline was compared against.
    """

    class Changes(object):
        no_baseline = False
        warnings = ()
        records = ()

        def __iter__(self):
            return iter(())

        def __len__(self):
            return 0

    run = cli.Run()
    run.roots = [_measured(_root("/project/lab", "project-lab"))]
    run.changes = Changes()
    run.warnings = ["ignored --since 'bogus': 'bogus' is not a duration"]
    opts = cli.build_parser().parse_args(["new", "--since", "bogus"])

    text, code = cli._render(run, opts, "new", style=None, width=None)

    assert code == cli.EXIT_OK
    assert "No change since the last run" in text
    assert "ignored --since" in text, "the ignored flag must be mentioned"


def test_the_treemap_and_the_table_agree_about_what_was_measured():
    """The treemap said "no quota reading was taken" for the very roots the
    atlas was showing a figure for, because it knew only about quota rows and
    the capacity fallback lives elsewhere. Two views of one run disagreeing
    about whether anything was measured is worse than either answer.
    """
    from dirscape.render import fields

    root = _root("/tmp", "", device="xfs")
    root.policy = {"free_bytes": 951_000_000_000}

    reason = fields.trouble(root)
    assert "no quota reading was taken" not in reason
    assert "free" in reason, reason
    # And the cell stays unsized: statvfs reports the whole filesystem's
    # headroom, so sizing a tile by it would let a shared /tmp dwarf the
    # user's own project directory.
    assert "nothing of yours to size" in reason


def test_json_respects_a_command_that_filters_roots():
    """`dirscape stranded --json` emitted every root, so a script asking for
    stranded storage had to re-implement the filter.
    """
    import json as jsonlib

    run = cli.Run()
    held = _measured(_root("/project/dahlias", "project-dahlias", reach=Reach.CLOSED))
    held.stranded = True
    away = Root("", role="archive")
    away.allocated = confirmed()
    away.mounted = refuted(VerdictCategory.NOT_MOUNTED_HERE)
    away.policy = {"allocation_location": "cfs4/acct"}
    ordinary = _measured(_root("/home/me", "home"))
    ordinary.role = "home"
    run.roots = [held, away, ordinary]

    for command, expected in (("stranded", 1), ("elsewhere", 1)):
        opts = cli.build_parser().parse_args([command, "--json"])
        text, code = cli._render(run, opts, command, style=None, width=None)
        assert code == cli.EXIT_OK
        payload = jsonlib.loads(text)
        assert len(payload["roots"]) == expected, "%s emitted %d roots" % (
            command,
            len(payload["roots"]),
        )


def test_why_accepts_an_allocation_location():
    """It is what `dirscape elsewhere` prints, so it is what a reader pastes
    back in. `os.path.abspath` had already turned `cfs4/hpc-staff` into
    `$PWD/cfs4/hpc-staff` and reported that as missing.
    """
    from dirscape.render import resolve_style

    run = cli.Run()
    away = Root("", role="archive")
    away.allocated = confirmed("acct allocation on cfs4/acct")
    away.mounted = refuted(VerdictCategory.NOT_MOUNTED_HERE, "no filesystem here")
    away.policy = {
        "allocation_location": "cfs4/acct",
        "allocation_gb": 20480.0,
        "allocation_accounts": ["acct"],
    }
    run.roots = [away]

    text, code = cli._why(run, "cfs4/acct", resolve_style(color="never", stream=None))

    assert code == cli.EXIT_OK
    assert "cfs4/acct" in text
    assert "an allocation, not a path on this node" in text
    # 20480 DECIMAL GB is 20.48e12 bytes, which is 18.6 TiB and renders as
    # 19T. The database publishes decimal GB and the display is binary, so the
    # number a reader sees is smaller than the one in the allocation table;
    # that is correct, and asserting 20T here was my arithmetic being wrong
    # rather than the code.
    assert "19T" in text, "the allocated size should be shown: %r" % (text,)
    assert "does not exist" not in text


def test_why_on_a_location_with_a_leading_slash_also_matches():
    """A reader may type it either way."""
    from dirscape.render import resolve_style

    run = cli.Run()
    away = Root("", role="archive")
    away.allocated = confirmed()
    away.mounted = refuted(VerdictCategory.NOT_MOUNTED_HERE)
    away.policy = {"allocation_location": "cfs4/acct"}
    run.roots = [away]

    text, code = cli._why(run, "/cfs4/acct", resolve_style(color="never", stream=None))
    assert code == cli.EXIT_OK
    assert "an allocation" in text
