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
import contextlib
import os
import sys

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

    The filter started at four keys and was short by six. A 200 column run put
    `crosses_to=['/project/hpc/jdoe42']`, `contains=2`, `free_bytes=...` and
    `size_bytes=...` in the column, and every one of those is already rendered
    properly elsewhere in the same view: the fold count is the `+2` on the
    path, the free figure is the `886G free` in the quota column, and the
    crossing is the symlink note. So the column was printing raw internals
    beside their own formatted selves, and it only stayed unseen because the
    column is the first one dropped when the window is narrow.
    """
    from dirscape.render import fields

    root = _root("/project/hpc", "project-hpc")
    root.policy = {
        "rank": "primary",
        "rank_reason": "the mount table named it",
        "allocation_location": "cfs4/hpc-staff",
        "allocation_gb": 25600.0,
        "allocation_accounts": ["hpc-staff"],
        "free_bytes": 951_720_603_648,
        "size_bytes": 959_727_210_496,
        "contains": 2,
        "crosses_to": ["/project/hpc/jdoe42"],
        "fileset_is_filesystem_root": True,
        "purge_days": 30,
    }
    merged = fields.merged_policy(root, site=None)

    leaks = (
        "rank",
        "rank_reason",
        "allocation_location",
        "allocation_gb",
        "allocation_accounts",
        "free_bytes",
        "size_bytes",
        "contains",
        "crosses_to",
        "fileset_is_filesystem_root",
    )
    for leaked in leaks:
        assert leaked not in merged
    assert merged["purge_days"] == 30, "real policy must survive the filter"

    cell = fields.policy_cell(root, site=None)
    assert "rank" not in cell
    assert "primary" not in cell
    for leaked in leaks:
        assert leaked not in cell, "%s reached the rendered cell" % (leaked,)


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


def _measured(root, used=1000, limit=2000, inodes=None, inode_limit=0):
    row = QuotaRow(root.fileset or "fs", "blocks", "user", used, hard=limit)
    root.quota = QuotaSnapshot("mmlsquota", [row])
    if inodes is not None:
        # The default view only shows the file count where there is room for
        # it, so a fixture testing that has to supply one.
        files = QuotaRow(root.fileset or "fs", "files", "user", inodes, hard=inode_limit)
        root.inode_quota = QuotaSnapshot("mmlsquota", [files])
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


def _content(text):
    """The panel's content lines, with the border and any colour removed.

    The default view is one framed block now, so every row arrives as
    `| ... |` and a test that reads the first word of a line reads the border.
    Rather than each test learning the frame, they all come through here: the
    border characters come off, the padding goes, and what is left is what the
    reader sees inside the box.
    """
    import re

    out = []
    for line in re.sub(r"\033\[[0-9;?]*[A-Za-z]", "", text).splitlines():
        bare = line.strip()
        if not bare or set(bare) <= set("+-|"):
            # The top and bottom borders, and an empty framed line.
            continue
        if bare[0] in "|\u2502" and bare[-1] in "|\u2502":
            bare = bare[1:-1]
        if not bare.strip() or set(bare.strip()) <= set("-\u2500"):
            continue
        out.append(bare.rstrip())
    return out


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

    # Read out of the ROLE COLUMN, not out of the whole string: the word
    # "project" also appears inside `/project/a`, `/project/b` and
    # `/project/c`, so a naive `text.count` measures the paths and not the
    # blanking. The column is located from the heading rather than by counting
    # spaces, because the table is indented inside a frame now and every row
    # begins with whitespace.
    lines = _content(text)
    heading = next(ln for ln in lines if ln.split()[:1] == ["role"])
    at = heading.index("path")
    body = [ln for ln in lines if ln != heading and "/" in ln[at:]]
    roles = [ln[:at].strip() for ln in body]
    assert [r for r in roles if r] == ["project", "home"], (
        "each role belongs on the first row of its run, got %r" % (roles,)
    )
    assert sum(1 for r in roles if not r) == 2, "two rows inherit their role"


def test_figures_align_on_the_separator():
    """So the column reads as a set of magnitudes a reader can compare."""
    # All three carry a real limit. An explicit zero limit used to render
    # `11T / no limit`, which put a separator in the cell and made it a valid
    # sample here; it renders `11T used` since the owner asked why one row said
    # `no limit` and another said `free`, so a no-limit row has no separator to
    # align and belongs in the capacity test below.
    roots = [
        _measured(_root("/a", "fa"), used=876_543_210, limit=32_212_254_720),
        _measured(_root("/b", "fb"), used=11_000_000_000_000, limit=99_000_000_000_000),
        _measured(_root("/c", "fc"), used=950_272, limit=1_073_741_824),
    ]
    for root in roots:
        root.role = "project"

    # The heading carries "space", so rows are taken by their path. The
    # comparison is case-sensitive and the heading went lower case, which made
    # this filter stop excluding it: with the heading in the sample the
    # separator offsets were two and the test was measuring the wrong thing.
    lines = [ln for ln in _content(_render_default(roots)) if " / " in ln and "space" not in ln]
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
    # Lower case since the headings changed, which matters: `"WHERE" not in
    # text` passed for the wrong reason the moment the heading did.
    assert "where" not in text
    assert "path" in text, "and the test is still looking at a real table"


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

    # `summary=True`, because the teasers are behind `--summary` now: three
    # lines of chrome on every run was the owner's complaint, not a feature.
    text = _render_default(shown, all_roots=shown + [held, away], summary=True)
    assert "dirscape stranded" not in _render_default(shown, all_roots=shown + [held, away]), (
        "the default view carries no teaser at all"
    )

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
        match = re.search(r"(\d[\d.]*[KMGTPB]?)(?= / | free| used)", line)
        assert match, "no figure found in %r" % (line,)
        edges.add(match.end())
    assert len(edges) == 1, "figures end at columns %s" % (sorted(edges),)


def test_the_selected_row_is_one_flat_band_with_no_colour_in_it():
    """The owner's bug, measured rather than looked at.

    "the highlightor gets truncated by `▎▒░░░░░░   3%` which looks so ugly",
    and then "it seems like `▎▒░░░░░░   3%` is on top of the highlightor".
    Both are one cause: under inverse video an explicit FOREGROUND colour is
    painted as the BACKGROUND, so every coloured run inside the band came out
    as a block of its own colour sitting on the selection. Measured in a pty
    before the fix: six of seven frames carried `\x1b[38;2;...m` inside the
    band.

    Two assertions, because the band needs both: no foreground code survives
    inside it, and every row's band is the same display width, or the cursor
    is a different shape on every line instead of a band moving down a
    column.
    """
    import re

    from dirscape.interactive import highlight
    from dirscape.render import atlas, resolve_style
    from dirscape.render.style import width as measure

    style = resolve_style(color="always", ascii_only=False, stream=None)
    roots = [
        _measured(_root("/home/me", "fs-home"), used=900_000_000, limit=32_212_254_720),
        _measured(_root("/project/lab", "fs-lab"), used=11_000_000_000_000, limit=0),
        _measured(_root("/scratch/me", "fs-scratch"), used=0, limit=400_000_000_000),
    ]
    roots[0].role = "home"
    roots[1].role = "project"
    roots[2].role = "scratch"

    # Unframed, which is what `cli._browse` highlights: the band goes on a
    # content line and the border is drawn around the result.
    lines = atlas.render(roots, style=style, group=True, frame=False).splitlines()
    assert any("\033[38;" in line for line in lines), (
        "the fixture has to have colour in it or this test proves nothing"
    )

    foreground = re.compile(r"\033\[(?:38;|9[0-7]m|3[0-7]m|39m)")
    widths = set()
    for index in range(len(lines)):
        painted = highlight(lines, index)[index]
        body = painted[len("\033[0m\033[7m") : -len("\033[0m")]
        assert not foreground.search(body), (
            "a foreground code inside the band becomes its background: %r" % (body,)
        )
        assert "\033" not in body, "no escape of any kind belongs inside the band: %r" % (body,)
        widths.add(measure(body))
    assert len(widths) == 1, "the band is %s columns wide on different rows" % (sorted(widths),)


def test_the_table_draws_no_bar_and_keeps_the_percentage():
    """Eight cells of blocks for a lossy copy of the number beside them.

    "why do we need this bar here? ... if you can't [fix it], just get rid of
    it." It was also blank on five of the ten rows of the live view, since an
    unlimited quota has no fraction and a capacity fallback has no quota, so
    the one thing a meter column is for, being scanned down, it could not do.
    The graded colour on the percentage carries fullness now.
    """
    from dirscape.render import atlas, resolve_style

    style = resolve_style(color="always", ascii_only=False, stream=None)
    root = _measured(_root("/home/me", "fs-home"), used=900_000_000, limit=32_212_254_720)
    root.role = "home"
    text = atlas.render([root], style=style, group=True)

    for glyph in "▏▎▍▌▋▊▉█░":
        assert glyph not in text, "the bar is gone, and %r is one of its cells" % (glyph,)
    assert "3%" in text, "the percentage is what replaced it"
    # And the colour is on the percentage, which is the whole substitution.
    assert "\033[38;" in text


def test_ascii_mode_emits_nothing_above_codepoint_127():
    """`--ascii` and `DIRSCAPE_ASCII=1` are for a `LANG=C` console.

    Checked on the WHOLE view rather than on the glyph table, because the
    frame, the rule and the separators are all drawn from it and a new glyph
    with no twin is invisible until something renders it.
    """
    from dirscape.render import atlas, resolve_style
    from dirscape.render.style import _GLYPHS

    for name, _rich, plain_twin in _GLYPHS:
        assert all(ord(ch) < 128 for ch in plain_twin), "glyph %r has no ASCII twin: %r" % (
            name,
            plain_twin,
        )

    roots = [
        _measured(_root("/home/me", "fs-home"), used=900_000_000, limit=32_212_254_720),
        _measured(_root("/project/lab", "fs-lab"), used=11_000_000_000_000, limit=0),
    ]
    roots[0].role = "home"
    roots[1].role = "project"
    for env in ({"DIRSCAPE_ASCII": "1"}, {}):
        style = resolve_style(
            color="always", ascii_only=None if env else True, stream=None, env=env
        )
        text = atlas.render(roots, style=style, group=True, summary=True, legend_on=True)
        bad = sorted({ch for ch in text if ord(ch) >= 128})
        assert not bad, "non-ASCII escaped into --ascii output: %r" % (bad,)
        assert "+--" in text, "the frame still has to be a frame"


def test_no_color_and_a_dumb_terminal_suppress_every_escape():
    """Including the frame's gradient, which is the newest thing that could
    have leaked one.

    `--color always` does not override either: the variable is the user's
    standing instruction about their own terminal, and a flag that beat it
    would make `NO_COLOR` advice rather than a setting.
    """
    from dirscape.render import atlas, resolve_style

    roots = [_measured(_root("/home/me", "fs-home"), used=900_000_000, limit=32_212_254_720)]
    roots[0].role = "home"
    for env in ({"NO_COLOR": "1"}, {"TERM": "dumb"}):
        for want in ("auto", "always"):
            style = resolve_style(color=want, ascii_only=False, stream=None, env=env)
            text = atlas.render(roots, style=style, group=True, summary=True, legend_on=True)
            assert "\033" not in text, "an escape survived %r with --color %s" % (env, want)
            assert "╭" in text, "the frame is still drawn, just without colour"


def test_the_frame_opens_and_closes_around_every_row():
    """A box that is a box everywhere. An unclosed frame reads as a crash."""
    from dirscape.render import atlas, resolve_style

    style = resolve_style(color="never", ascii_only=False, stream=None)
    roots = [_measured(_root("/home/me", "fs-home")), _measured(_root("/project/lab", "fs-lab"))]
    roots[0].role = "home"
    roots[1].role = "project"
    lines = atlas.render(roots, style=style, group=True, size=100).splitlines()

    assert lines[0].startswith("╭") and lines[0].endswith("╮")
    assert lines[-1].startswith("╰") and lines[-1].endswith("╯")
    for line in lines[1:-1]:
        assert line.startswith("│") and line.endswith("│"), "unclosed row: %r" % (line,)
    assert len({len(line) for line in lines}) == 1, "the border has to be rectangular"


def test_a_title_that_wraps_does_not_break_the_frame_open():
    """Found at 60 columns, and it is the frame's sharpest edge.

    `legend` breaks the title to a second line when the next item will not
    fit, and the atlas was handing that string to `panel` as ONE content
    line. The border closed after `compute` and the rest of the title landed
    outside the box with the right-hand border stuck on the end of it:

        | dirscape  .  jdoe42  .  meadow3-0200  .  compute
        9 devi... |
    """
    from dirscape.render import atlas, resolve_style

    style = resolve_style(color="never", ascii_only=False, stream=None)
    roots = [_measured(_root("/home/me", "fs-home"))]
    roots[0].role = "home"
    # The wrap is forced with a long user and host rather than with the node
    # class and device count, which used to be on this line and were removed:
    # the owner read them and asked what they meant. The GUARANTEE this test
    # exists for is unchanged and still bites, because a long login name or a
    # long hostname wraps the title exactly the same way.
    meta = {
        "host": "an-unusually-long-hostname-for-a-login-node",
        "user": "a-rather-long-login-name",
    }
    lines = atlas.render(roots, meta=meta, style=style, group=True, size=60).splitlines()

    assert lines[1].count("dirscape") == 1
    for line in lines[1:-1]:
        assert line.startswith("│") and line.endswith("│"), "the frame opened: %r" % (line,)
    assert len({len(line) for line in lines}) == 1
    # And the title really did need two lines, or this proves nothing.
    assert any("an-unusually-long-hostname" in line for line in lines[1:4])
    assert "an-unusually-long-hostname" not in lines[1]


def test_a_path_too_wide_for_a_frame_is_printed_whole_and_unframed():
    """The frame gives way, not the path.

    A shortened path is a different path and the reader cannot tell which
    characters went, so the stacked fallback prints it whole and lets the
    terminal wrap. A border would have to cut it to hold it.
    """
    from dirscape.render import atlas, resolve_style

    style = resolve_style(color="never", ascii_only=False, stream=None)
    deep = "/project/lab/" + "a" * 90
    root = _measured(_root(deep, "fs-lab"))
    root.role = "project"
    text = atlas.render([root], style=style, group=True, size=40)

    assert deep in text, "not one character of a path may go"
    assert "╭" not in text and "│" not in text, "the frame comes off rather than cutting it"


def test_the_drop_order_is_the_documented_one():
    """Each stage gives up the least urgent thing left, and the order is the
    view's policy rather than an accident of how it was written.

    The stage that dropped the usage BAR went with the bar itself.
    """
    from dirscape.render import atlas

    assert atlas.DROP_STAGES[0] == ()
    names = [[atlas.COLUMNS[i] for i in stage] for stage in atlas.DROP_STAGES]
    assert names == [
        [],
        ["policy"],
        ["policy", "files / limit"],
        ["policy", "files / limit", "role"],
        ["policy", "files / limit", "role", "reach"],
        ["policy", "files / limit", "role", "reach", "where"],
    ]
    for earlier, later in zip(atlas.DROP_STAGES, atlas.DROP_STAGES[1:]):
        assert set(earlier) < set(later), "a stage may only add to the one before it"
    for stage in atlas.DROP_STAGES:
        for kept in atlas.KEEP_COLUMNS:
            assert kept not in stage, "path and the figure are never dropped"


def test_width_is_not_spent_on_a_column_that_gets_dropped_as_constant():
    """The fitting bug: ROLE lost to three columns that never rendered.

    WHERE reads `here` on every row of an ordinary run and drops as constant,
    and FILES and POLICY are empty on a site with no `site.conf`. The stage
    loop measured every candidate set against all seven columns anyway, so
    their headings alone pushed each stage over budget until the one that
    gives up ROLE, and the caller then removed the three empty columns. The
    reader lost the only column in that group carrying information.

    The widths are the live ones from a meadow3 login node, because the bug
    only bites once the real path and figure columns are in play: the four
    surviving columns need 67 display columns of a 76 column body, and the
    three empty ones cost 28 more with nothing in them.
    """
    from dirscape.render.atlas import (
        _FILES,
        _PATH,
        _POLICY,
        _REACH,
        _ROLE,
        _USED,
        _WHERE,
        _plan,
    )

    live = [
        ("home", "/home/jdoe42", "857M / 30G (3%)  "),
        ("project", "/project/hpc", " 11T / no limit  "),
        ("scratch", "/scratch/collie3/jdoe42", "  0B / 400G (0%) "),
        ("scratch", "/scratch/meadow3/jdoe42", " 22G / 100G (22%)"),
        ("software", "/software", "314G / no limit  "),
    ]
    rows = []
    for role, path, used in live:
        row = [""] * 7
        row[_ROLE], row[_PATH], row[_USED] = role, path, used
        row[_REACH] = "rwx"
        # Constant on every row, which is exactly why they get removed, and
        # exactly why their widths must not be charged for: WHERE reads `here`
        # wherever the storage is attached, the caller suppresses inode figures
        # in the default view outright, and POLICY is `?` at a site that
        # publishes no purge or backup rules.
        row[_WHERE] = "here"
        row[_FILES] = "37k / 300k"
        row[_POLICY] = "?"
        rows.append(row)

    empty = [_WHERE, _FILES, _POLICY]

    blind, _ = _plan(rows, 76)
    assert _ROLE not in blind, (
        "the bug this guards: measured against the empty columns too, the only "
        "stage that fits is the one that gives up role"
    )

    columns, stacked = _plan(rows, 76, skip=empty)
    assert not stacked
    assert _ROLE in columns, "role fits at 80 columns and must not be dropped for blanks"
    assert _PATH in columns and _USED in columns and _REACH in columns


def test_spare_width_buys_a_column_and_never_a_gutter():
    """Padding is not use, and this is the test that says so.

    The first attempt at "take the whole width" made `table` stretch the
    gutter before the last column until the box reached the window. At a 126
    column terminal that was a single 40 space gap between `reach` and
    `space`, and the owner's verdict was immediate: "a lot of space is
    available and unoccupied, why is there still ..." and then "the space
    should be utilized well. but now it's terrible". They were right. A reader
    cannot track a row across a gulf, and the box reaching the edge bought
    nothing.

    Spare width goes to a real column instead. `files / limit` has data on
    every row of a live run and was being suppressed unconditionally as
    detail, so it is the column the room buys.

    Both halves are asserted, because either alone is satisfiable by doing
    nothing: the column has to APPEAR when there is room and GO when there is
    not, and no row may contain a gulf at any width.
    """
    from dirscape.render import atlas, resolve_style
    from dirscape.render.style import width as measure

    style = resolve_style(color="never", ascii_only=False, stream=None)
    # The figures VARY per row on purpose. A column whose value is identical
    # everywhere is dropped as a caption, so a fixture that repeats itself
    # tests the constant-column rule instead of the width rule.
    roots = []
    for offset, (path, role) in enumerate(
        (
            ("/home/me", "home"),
            ("/project/one", "project"),
            ("/scratch/meadow3/me", "scratch"),
            ("/software", "software"),
        )
    ):
        root = _measured(
            _root(path, "fs" + role),
            used=876_543_210 * (offset + 1),
            limit=32_212_254_720,
            inodes=37_000 * (offset + 1),
            inode_limit=300_000,
        )
        root.role = role
        roots.append(root)

    narrow = atlas.render(roots, style=style, group=True, size=72)
    wide = atlas.render(roots, style=style, group=True, size=110)

    assert "files" not in narrow, "at 72 columns the file count is the first thing to go"
    assert "files" in wide, "at 110 columns the room must buy a column, not whitespace"

    # And the box is sized to what it holds, never stretched to the window.
    # This is the half that forbids the gulf: a frame that has to reach the
    # right edge can only get there by padding once every column that has
    # data is already on screen.
    for window, text in ((72, narrow), (110, wide)):
        lines = text.splitlines()
        edge = measure(lines[0])
        content = max(measure(ln) for ln in lines[1:-1])
        assert edge == content, "the frame is %d wide around %d of content at window %d" % (
            edge,
            content,
            window,
        )
        assert edge <= window, "the frame overflowed the window"


def test_the_interactive_frame_does_not_truncate_the_last_column():
    """The off-by-four the layout change exposed.

    `_browse` renders the atlas with `frame=False` and draws the panel itself,
    so the content has to be laid out against the window MINUS the four
    columns the border and its padding take. It was passing the full window,
    and while the table was narrower than the window anyway nothing showed.
    The moment the layout used the width it was given, every line came out
    four columns too wide and `panel` truncated the last cell: the figure read
    `865M / 30G (` and an ellipsis in the interactive view while the static
    print of the same table was correct.

    Asserted on the rendered block rather than through a pty, so it runs
    everywhere: every line of a framed block is exactly the window wide and
    none of them carries the truncation mark.
    """
    from dirscape.render import resolve_style
    from dirscape.render.style import width as measure

    style = resolve_style(color="never", ascii_only=False, stream=None)
    run = cli.Run()
    run.roots = []
    for path, role in (
        ("/home/me", "home"),
        ("/project/one", "project"),
        ("/scratch/meadow3/me", "scratch"),
    ):
        root = _measured(
            _root(path, "fs" + role),
            used=876_543_210,
            limit=32_212_254_720,
            inodes=37_000,
            inode_limit=300_000,
        )
        root.role = role
        run.roots.append(root)

    for window in (80, 110, 126):
        lines = cli._table_frame(run.roots, 0, run=run, style=style, width=window)
        assert lines, "no frame at %d columns" % (window,)
        for line in lines:
            assert measure(line) <= window, "%r exceeds %d columns" % (line, window)
            assert style.g.ellipsis not in line, "the last cell was truncated at %d columns: %r" % (
                window,
                line,
            )
        # Every row of the block is the same width, which is what makes the
        # selection band a band and not ragged emphasis.
        assert len({measure(ln) for ln in lines}) == 1, "the block is ragged at %d columns: %s" % (
            window,
            sorted({measure(ln) for ln in lines}),
        )

        # And the detail view it opens into holds the same property.
        detail = cli._detail(run, run.roots[0], style, cols=window, window=40)
        for line in detail:
            assert measure(line) <= window, "%r exceeds %d columns" % (line, window)

    # The CONTRACT, not just the symptom. A symptom test cannot see this bug
    # while the table happens to be narrower than the window: the truncation
    # only fires once the layout actually uses the width it is handed, which
    # is how it stayed hidden through a whole suite of static-render tests and
    # then appeared the moment the columns filled out. So the size handed to
    # the renderer is asserted directly, and it must leave room for the border
    # this function draws itself.
    seen = []
    real = cli.render_atlas

    def spy(*args, **kwargs):
        seen.append(kwargs.get("size"))
        return real(*args, **kwargs)

    cli.render_atlas = spy
    try:
        for window in (80, 110, 126):
            seen[:] = []
            cli._table_frame(run.roots, 0, run=run, style=style, width=window)
            assert seen == [window - 4], (
                "the unframed atlas must be laid out 4 columns narrower than the frame "
                "that wraps it, got %r at window %d" % (seen, window)
            )
    finally:
        cli.render_atlas = real


def test_a_no_limit_figure_says_used_rather_than_pairing_with_a_limit():
    """`11T / no limit` next to `886G free` was one column, two measurements.

    The owner read the two side by side and asked "why is there no / in front
    of free? what does free mean here? there is no limit, but why is there
    also free?" The `/` promised two numbers where there was one, and the
    shared `used / quota` heading claimed both rows measured the same thing.

    Three shapes now, each naming itself, under a heading true of all three:

        876M / 30G (3%)   your usage against your quota
        11T used          your usage, with no quota set
        886G free         the filesystem's headroom, shared
    """
    from dirscape.render import atlas, resolve_style

    style = resolve_style(color="never", ascii_only=False, stream=None)

    quota = _measured(_root("/a", "fa"), used=876_543_210, limit=32_212_254_720)
    unlimited = _measured(_root("/b", "fb"), used=11_000_000_000_000, limit=0)
    capacity = _root("/c", "fc")
    capacity.reach = Reach.LISTABLE
    capacity.writable = confirmed()
    capacity.policy = {"free_bytes": 951_000_000_000}
    for root in (quota, unlimited, capacity):
        root.role = "project"

    text = atlas.render([quota, unlimited, capacity], style=style, group=True, size=100)

    assert "space" in text, "one heading, true of all three shapes"
    assert "no limit" not in text, "the words that prompted the question are gone"
    row = next(ln for ln in text.splitlines() if "/b" in ln)
    assert "used" in row and " / " not in row, "a no-limit row has one number: %r" % (row,)
    free = next(ln for ln in text.splitlines() if "/c" in ln)
    assert "free" in free and " / " not in free, "a capacity row has one number: %r" % (free,)
    both = next(ln for ln in text.splitlines() if "/a" in ln)
    assert " / " in both, "a real quota still pairs used with its limit: %r" % (both,)


def test_the_detail_view_is_fields_and_not_paragraphs():
    """The owner's verdict on the old one: "this chunk of verbose text makes
    no fucking sense. it says the figures above. what figures?"

    Two separate defects in that sentence. Prose that points at other lines
    ("the figures above") assumes the reader is going top to bottom, which
    nobody does in a detail view. And three paragraphs of mechanism were
    costing six lines on every path to say what naming the quota says.

    The property asserted is structural rather than a word count: every line
    is either the heading, blank, a `label value` field, or the one footer.
    Nothing wraps, which is what makes it scannable and also what keeps
    `_browse`'s repaint arithmetic true.
    """
    run = cli.Run()
    root = _measured(_root("/project/lab", "project-lab"), used=876_543_210, limit=0)
    root.role = "project"
    root.writable = confirmed()
    run.roots = [root]

    from dirscape.render import resolve_style

    text, _ = cli._why(run, "/project/lab", resolve_style(color="never", stream=None))
    lines = text.splitlines()

    assert len(lines) <= 14, "a detail view is a field list, not a page: %d lines" % (len(lines),)
    body = [ln for ln in lines[1:] if ln.strip()]
    for line in body:
        assert len(line.split()) <= 9, "%r is a sentence, not a field" % (line,)
    assert "the figures above" not in text, "prose must not point at other lines"
    assert "counted by the filesystem itself" not in text, "the mechanism paragraph is gone"
    assert "  quota     " in text, "the quota source survives as a field"


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
    # Matched against the whitespace-normalised text, because the sentence is
    # wrapped to the window and the line it breaks on moves with the width.
    # Re-anchored when the bullet became a `symlinks` field. The count and
    # the "billed there" consequence are the two things this test is about, and
    # both survived the rewrite; the sentence around them did not.
    flat = " ".join(text.split())
    assert "symlinks 6 into" in flat, flat
    assert "billed there" in flat, "the consequence is the point, not the count"
    assert len(text.splitlines()) < 20, "why is a screen, not a transcript"


def test_why_omits_a_probe_that_never_ran():
    """`? allocated not probed` appeared on every mounted root.

    The allocation database is only consulted for storage with no path here,
    so a question mark against a question nobody asked teaches a reader to
    skip the column.

    Reworded, not weakened: the view no longer prints the model's labels
    ("allocated", "mounted") at a reader, so the assertion moved onto what it
    was always about. The allocation probe never ran and must be silent; the
    reachability probe answered and must show its answer.

    Moved a second time when a CONFIRMED attachment went silent too, for the
    reason `_findings` records: a row that has just printed a quota figure and
    a write answer has demonstrated the storage is attached, so the sentence
    restated it behind an unexplained `✓`. That makes the attachment axis a
    poor witness for "the answered probe shows", so the witness is now the
    access line, which is the reachability probe speaking in the same run. The
    attachment axis gets its own case below, on the reading where it matters.
    """
    run = cli.Run()
    root = _measured(_root("/project/lab", "project-lab"))
    root.reach = Reach.LISTABLE
    root.writable = confirmed()
    run.roots = [root]

    from dirscape.render import resolve_style

    text, _ = cli._why(run, "/project/lab", resolve_style(color="never", stream=None))

    assert "not probed" not in text, "a probe that never ran was reported anyway"
    assert "allocation" not in text, "the unasked allocation question must be silent"
    # The reach phrases became words when the owner ruled out verbose text,
    # so the witness is the field rather than the sentence it used to hold.
    assert "access    read + write" in text, "the answered probe must show"
    assert "\u2713" not in text, "a confirmed axis has no mark to explain"


def test_why_speaks_up_when_the_storage_is_not_attached_here():
    """The attachment axis earns a sentence on the reading that matters.

    Silence on a confirmed mount is the whole point of the previous test, and
    it would be satisfied just as well by code that never mentioned attachment
    at all. This is the case the axis exists for: `/cfs3` is there from a login
    node and absent from a compute one, and a reader looking at a path they
    cannot use needs to be told it is a fact about the machine.
    """
    run = cli.Run()
    root = _root("/cfs3/kestrel-lab", "cfs3-night", device="cfs3")
    root.mounted = refuted(VerdictCategory.NOT_MOUNTED_HERE)
    run.roots = [root]

    from dirscape.render import resolve_style

    text, _ = cli._why(run, "/cfs3/kestrel-lab", resolve_style(color="never", stream=None))

    assert "not attached to the machine you are on" in text
    assert "fact about this machine" in text, "and not a fact about the storage"


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


def test_the_fold_count_left_the_table_and_lives_in_json():
    """`+2` is gone from the path cell, deliberately.

    It reversed an earlier fix in this same file, and the reversal is the
    right way round. The count WAS being stored and never shown, which was a
    real defect, so it was rendered as `/project/hpc +2`. Then the owner read
    that and asked what it meant, and the honest answer was "a number you
    cannot use": it names no path, and the only action available is `--all`,
    which lists the folded rows properly.

    So the fact stays and the glyph goes. `--json` still carries `contains`
    and `--summary` still counts the hidden rows, which is what keeps this a
    presentation change rather than a loss of information. Verified live:
    `--json` reports `[('/project/hpc', 2)]`.
    """
    from dirscape.render import atlas

    root = _measured(_root("/project2/reference", "project2-reference"))
    root.policy = dict(root.policy or {})
    root.policy["contains"] = 20

    cell = atlas._path_cell(root)
    assert "+20" not in cell, "the fold count must not be back in the table"
    assert cell.endswith("/project2/reference")

    # And it is still in the machine-readable output.
    assert root.to_json()["policy"]["contains"] == 20


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


def test_run_warnings_reach_the_user_on_stderr():
    """They were collected into `Run.warnings` and only ever reached `--json`,
    so a malformed site.conf, a damaged baseline and a failed plugin were all
    silent in the view a user actually reads.

    They went into the table next, and then out of it again: the table is one
    framed panel of storage facts and a run-level failure is not one of them,
    and it must not move behind `--summary` with the counts either, or a failed
    save is silent again. So the channel is stderr, which also keeps
    `dirscape | grep` clean. What this pins is that the text still gets OUT.
    """
    run = cli.Run()
    run.warnings = ["ignored malformed config /etc/dirscape/site.conf"]
    assert cli._surfaceable(run) == ["ignored malformed config /etc/dirscape/site.conf"]

    from dirscape.render import atlas

    root = _measured(_root("/project/lab", "project-lab"))
    root.role = "project"
    assert "malformed" not in atlas.render([root], group=True), (
        "a run-level warning is not a row of the table"
    )


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
    assert "an allocation, not a directory you can use from this machine" in text
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


# --------------------------------------------------------------------------
# The detail view has to fit the window it repaints in
# --------------------------------------------------------------------------


def _detail_fixture():
    """A root carrying the fields that made the block overflow in real use.

    The 157 character writable reason is the actual string `discover.access`
    returns, and it is what made `_why("/project/hpc")` occupy 19 rows while
    reporting 18 lines. The notes and the long fileset name are here because
    `_fit` has to hold for a line with no space in it as well as for prose.
    """
    run = cli.Run()
    root = _measured(_root("/project/hpc", "project-hpc"), used=11765797488, limit=0)
    root.role = "project"
    root.reach = Reach.LISTABLE
    root.writable = confirmed(
        "os.access reports write; not owner-confirmed, and W_OK can be wrong under a "
        "root-squashed export, so pass --probe-write to settle it by writing",
        source="os.access",
    )
    root.add_source("group-template")
    root.add_source("dir-owner")
    root.add_source("quota-fileset")
    root.add_note("matched group hpc")
    root.add_note("directory hpc is group-owned by a group you are in")
    root.add_note("holds the fileset project-hpc")
    root.add_note("also reachable at /gpfs/meadow3/cap/project/hpc/a/very/long/alias/path/indeed")
    run.roots = [root]
    return run, root


def _physical_rows(lines, columns):
    """How many rows a terminal ``columns`` wide gives these lines.

    The arithmetic `interactive.select` gets wrong when it is not one to one:
    it moves the cursor up by `len(lines)` and the terminal has consumed this
    many rows instead.
    """
    from dirscape.render.style import width

    total = 0
    for line in lines:
        span = width(line)
        total += max(1, -(-span // columns))
    return total


@pytest.mark.parametrize("color", ["never", "always"])
@pytest.mark.parametrize("columns", [80, 90, 100, 110, 120, 42])
def test_the_detail_block_takes_one_row_per_line(color, columns):
    """The header-repeat bug, measured rather than looked at.

    `interactive.select` repaints by moving the cursor up by the number of
    lines it last WROTE. Measured on the previous `_why("/project/hpc")`: 18
    lines written against 19 rows occupied, at every width from 80 to 120,
    because the writable verdict's reason ran to 157 characters and wrapped.
    The cursor stopped a row short, the erase began a row too low, and one row
    survived every repaint: thirteen presses of Down left thirteen copies of
    `/project/hpc` stacked above the detail.
    """
    from dirscape.render import resolve_style

    run, root = _detail_fixture()
    style = resolve_style(color=color, stream=None, size=columns)
    lines = cli._detail(run, root, style, cols=columns, window=0)

    assert lines, "the detail block cannot be empty"
    assert _physical_rows(lines, columns) == len(lines), "%d lines occupy %d rows at %d columns" % (
        len(lines),
        _physical_rows(lines, columns),
        columns,
    )


def test_the_detail_block_leaves_the_window_a_spare_row():
    """A block taller than the window has scrolled before it is erased.

    The cursor-up then lands at the top of the WINDOW rather than the top of
    the block, and the erase takes whatever the reader had above it. That is
    the "entire terminal turns empty" report, one level below the table it was
    first found on.
    """
    from dirscape.render import resolve_style

    run, root = _detail_fixture()
    style = resolve_style(color="never", stream=None, size=100)
    for window in (10, 14, 24, 40):
        lines = cli._detail(run, root, style, cols=100, window=window)
        assert len(lines) + 1 <= window, "%d lines in a %d row window" % (len(lines), window)
    # And the reader is told the screen was cut, with the command that is not.
    short = " ".join(" ".join(cli._detail(run, root, style, cols=100, window=14)).split())
    assert "dirscape why /project/hpc" in short


def test_up_and_down_do_not_repaint_a_one_row_view():
    """There is nowhere to move, so a repaint is pure cost.

    `select` wraps the cursor round onto the same row and paints the same
    block again, which is one more chance for the cursor arithmetic to be
    wrong for no benefit at all.
    """
    import re

    from dirscape import interactive

    keys = iter(
        [
            interactive.Key.DOWN,
            interactive.Key.DOWN,
            interactive.Key.UP,
            interactive.Key.QUIT,
        ]
    )
    written = []  # type: list
    outcome = interactive.select(
        lambda i: ["only one row"],
        1,
        keys=cli._still(lambda: next(keys)),
        write=written.append,
        raw=False,
    )
    text = "".join(written)

    assert outcome == interactive.Key.QUIT
    # One cursor-up in the whole session, and it is the erase on the way out.
    assert len(re.findall(r"\033\[\d+A", text)) == 1, text.replace("\033", "ESC")
    assert text.count("only one row") == 1, "the block was painted more than once"


def test_the_movement_keys_still_move_the_table():
    """The guard above is for a ONE ROW view and must not reach the atlas."""
    import re

    from dirscape import interactive

    keys = iter([interactive.Key.DOWN, interactive.Key.QUIT])
    written = []  # type: list
    interactive.select(
        lambda i: ["row %d" % (i,)],
        3,
        keys=keys.__next__,
        write=written.append,
        raw=False,
    )
    text = "".join(written)
    assert len(re.findall(r"\033\[\d+A", text)) == 2, "one repaint and one erase"
    assert "row 1" in text, "Down did not move the cursor"


# --------------------------------------------------------------------------
# The detail view's vocabulary is the reader's, not the model's
# --------------------------------------------------------------------------


def test_no_discovery_source_token_reaches_the_prose():
    """`found by group-template, dir-owner, quota-fileset` was the worst line
    on the old screen: three internal constants shown to a human.

    `model.py` already owns the fix (`category_label`, after nodetop printed
    raw enum members in a prose column), so the sources got the same
    treatment and the tokens stay in `--json`.
    """
    from dirscape import discover
    from dirscape.render import resolve_style

    run, root = _detail_fixture()
    text, _ = cli._why(run, "/project/hpc", resolve_style(color="never", stream=None))

    for token in discover.SOURCE_LABELS:
        assert token not in text, "%r is a wire token and reached the screen" % (token,)
    flat = " ".join(text.split())
    assert "found " in flat, "the sources are a field now, not a sentence"
    # The SHORT form, because the `found` field takes noun phrases. The long
    # clause form still exists and `--json` and the long views use it; what
    # this test guards either way is that the wire token never appears and its
    # human twin does.
    assert discover.source_label("dir-owner", short=True) in flat


def test_no_verdict_category_token_reaches_the_detail_view():
    """The invariant `render/__init__` states: a `VerdictCategory` never
    reaches a prose column, `category_label()` does.
    """
    from dirscape.render import resolve_style

    run, root = _detail_fixture()
    root.allocated = refuted(VerdictCategory.ALLOCATED_ELSEWHERE, "nothing here")
    root.present = unknown(VerdictCategory.PROBE_TIMEOUT, "took too long")
    text, _ = cli._why(run, "/project/hpc", resolve_style(color="never", stream=None))

    for name in dir(VerdictCategory):
        if name.startswith("_"):
            continue
        token = getattr(VerdictCategory, name)
        if isinstance(token, str):
            assert token not in text, "%r is a wire token and reached the screen" % (token,)


def test_the_detail_view_repeats_nothing_discovery_already_said():
    """A source label and the note that explains the same source are one fact.

    `/project/hpc` carried "matched group hpc", "directory hpc is group-owned
    by a group you are in" and "holds the fileset project-hpc" underneath the
    three labels that say exactly that.
    """
    from dirscape.render import resolve_style

    run, root = _detail_fixture()
    text, _ = cli._why(run, "/project/hpc", resolve_style(color="never", stream=None))

    assert "matched group hpc" not in text
    assert "holds the fileset" not in text
    # A note that is NOT a source restatement survives, because dropping it
    # would cost a fact rather than a repetition.
    assert "also reachable at" in " ".join(text.split())


def test_an_unmeasured_figure_is_not_explained_as_a_measured_one():
    """The honesty rule, on the sentence that says where a figure came from.

    With no quota reading and no `statvfs` fallback there is no figure, so
    there is nothing to attribute, and the view has to say that rather than
    describe accounting that did not happen.
    """
    from dirscape.render import resolve_style

    run = cli.Run()
    root = _root("/project/lab", "project-lab")
    root.reach = Reach.LISTABLE
    run.roots = [root]

    text, _ = cli._why(run, "/project/lab", resolve_style(color="never", stream=None))
    flat = " ".join(text.split())

    assert "space     ?" in text, "an unmeasured figure is the unknown mark: %r" % (text,)
    assert "quota none measured" in flat
    assert "counted by the filesystem itself" not in flat


def test_an_unpublished_policy_is_not_reported_as_no_policy():
    """Silence from a site is not a claim that nothing is backed up."""
    from dirscape.render import resolve_style

    run, root = _detail_fixture()
    text, _ = cli._why(run, "/project/hpc", resolve_style(color="never", stream=None))
    assert "not published" in text
    assert "not backed up" not in text


def test_a_published_policy_is_spelled_out():
    """And when a site does publish one, it is a sentence and not a token."""
    from dirscape.render import resolve_style

    run, root = _detail_fixture()
    root.policy = dict(root.policy or {})
    root.policy.update({"purge_days": 30, "backup": False})
    text, _ = cli._why(run, "/project/hpc", resolve_style(color="never", stream=None))
    flat = " ".join(text.split())

    assert "not backed up" in flat
    assert "deleted 30 days after writing" in flat
    assert "purge_days" not in flat


@pytest.mark.skipif(not hasattr(os, "fork"), reason="needs fork")
def test_a_real_pty_does_not_repaint_the_detail_view():
    """The owner's report, end to end in an actual terminal.

    Thirteen presses of Down in the detail view left thirteen copies of the
    path stacked above it, one per repaint, because the block occupied one row
    more than the number of lines the repaint arithmetic was counting.

    Two counts, and neither depends on what the views render:

    * **Three raw sessions.** One per `select` call, so three proves the Enter
      opened the detail view and the Left came back out of it. Without this the
      test would pass by having its keystrokes dropped, which is exactly what
      an output-driven harness does once the repaint it was waiting on is gone.
    * **Three cursor-up sequences**, which are the three erases: leaving the
      table, leaving the detail, and leaving the table again. The four Down
      presses in between add none. Before the fix they added one each.
    """
    pty = pytest.importorskip("pty")
    import re
    import select as sel
    import struct
    import time

    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    pid, fd = pty.fork()
    if pid == 0:  # pragma: no cover - the child execs
        os.environ["TERM"] = "xterm"
        os.environ["PYTHONPATH"] = os.path.join(root, "src")
        os.execv(sys.executable, [sys.executable, "-m", "dirscape", "--no-state"])

    import fcntl
    import termios

    fcntl.ioctl(fd, termios.TIOCSWINSZ, struct.pack("HHHH", 40, 110, 0, 0))

    out = b""
    # Keys go on a wall clock and not on output, because the fix under test
    # REMOVES the output a repaint-driven harness would wait for.
    script = [b"\r", b"\x1b[B", b"\x1b[B", b"\x1b[B", b"\x1b[B", b"\x1b[D", b"q"]
    sent = 0
    opened = None
    deadline = time.time() + 90
    try:
        while time.time() < deadline:
            ready, _, _ = sel.select([fd], [], [], 0.2)
            if ready:
                try:
                    chunk = os.read(fd, 65536)
                except OSError:
                    break
                if not chunk:
                    break
                out += chunk
            if opened is None:
                if b"\033[?25l" in out:
                    opened = time.time() + 0.6
                continue
            if time.time() < opened or sent >= len(script):
                continue
            os.write(fd, script[sent])
            sent += 1
            opened = time.time() + 0.5
        text = out.decode("utf-8", "replace")
    finally:
        with contextlib.suppress(OSError):
            os.close(fd)
        with contextlib.suppress(OSError):
            os.waitpid(pid, os.WNOHANG)

    if "\033[?25l" not in text:
        pytest.skip("the browse never started here, so there is nothing to measure")

    assert text.count("\033[?25l") == 3, (
        "expected three raw sessions (table, detail, table): the keystrokes did not land"
    )
    ups = re.findall(r"\033\[(\d+)A", text)
    assert len(ups) == 3, "one erase per level and no repaint per keypress, got %r" % (ups,)
