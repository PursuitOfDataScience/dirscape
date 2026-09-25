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
import re
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
from dirscape.render import fields as render_fields
from dirscape.render.style import Style


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
    heading = next(ln for ln in lines if ln.split()[:1] == ["kind"])
    at = heading.index("path")
    body = [ln for ln in lines if ln != heading and "/" in ln[at:]]
    roles = [ln[:at].strip() for ln in body]
    assert [r for r in roles if r] == ["project", "home"], (
        "each role belongs on the first row of its run, got %r" % (roles,)
    )
    assert sum(1 for r in roles if not r) == 2, "two rows inherit their role"


def test_every_figure_column_is_right_aligned_and_one_token_per_cell():
    """What `_align_figures` used to fake, real columns do for free.

    The figures were composed into ONE cell (`866M / 30G (3%)`) and a helper
    then right-aligned the parts inside it on the separator, so the column
    read as a set of magnitudes. It could only ever half work, because the
    cell had three different shapes depending on what was known: a pair with
    a percentage, `11T used` where no cap was set, `886G free` where no quota
    existed at all, and `?`. The owner, having asked twice about it: "simply
    saying 11T used but no cap is very confusing. all the entries in space
    aren't consistent at all."

    `used`, `limit` and `free` are separate columns now. `table` right-aligns
    each, `_align_figures` is deleted, and the property to hold is the one
    that made the cell unreadable: every cell in a figure column is a SINGLE
    token, so nothing has to be parsed before two rows can be compared.
    """
    import re

    from dirscape.render import atlas, resolve_style

    style = resolve_style(color="never", ascii_only=False, stream=None)
    roots = [
        # One of each state the figure cell can be in, which is the set that
        # used to produce four different shapes in one column.
        _measured(_root("/a", "fa"), used=876_543_210, limit=32_212_254_720),
        _measured(_root("/b", "fb"), used=11_000_000_000_000, limit=0),
        _measured(_root("/c", "fc"), used=950_272, limit=1_073_741_824),
    ]
    bare = _root("/d", "fd")
    bare.reach = Reach.LISTABLE
    bare.writable = confirmed()
    bare.policy = {"free_bytes": 951_000_000_000}
    roots.append(bare)
    for root in roots:
        root.role = "project"

    lines = _content(atlas.render(roots, style=style, group=True, size=100))
    # The headings for these two are data-dependent (`your use` / `your limit`
    # when every row on screen is user-scoped), so the column is located by
    # the word both forms share.
    heading = next(ln for ln in lines if "used" in ln and "quota" in ln)
    body = [ln for ln in lines if re.search(r"/[abcd]\b", ln)]
    assert len(body) == 4

    for column in ("used", "quota"):
        at = heading.index(column) + len(column)
        for line in body:
            cell = line[:at]
            assert cell.endswith(tuple("0123456789BKMGTP?e")), (
                "%r does not end at the %r column's right edge: %r" % (cell[-12:], column, line)
            )

    # One token per cell: no figure cell pairs two numbers, which is what
    # `11T used`, `886G free` and `866M / 30G (3%)` each did.
    figures_at = heading.index("used")
    for line in body:
        assert " / " not in line[figures_at:], "a figure cell is pairing two numbers again"
        assert "%" not in line, "the percentage was folded into free"

    # And the heading is one word per column. `_content` strips the frame, so
    # line widths vary here by design; the uniform-width guarantee belongs to
    # the framed block and is asserted where the frame is drawn.
    for word in heading.split():
        assert "/" not in word, "%r is a compound heading again" % (word,)


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
    # Anchored on `access:`, because the legend stopped describing `reach`
    # when the column did. Kept as one word the legend opens with rather than
    # a phrase, so a rewording of the sentence after it does not break this.
    assert "access:" not in _render_default(roots)
    assert "access:" in _render_default(roots, legend_on=True)


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


# `test_a_capacity_figure_aligns_with_the_quota_figures` lived here. It guarded
# a bug in `_align_figures`, which right-aligned the parts of one composed
# `used / limit` cell and compared the trailing word against "free" WITHOUT
# stripping the cell's colour, so `free\x1b[0m` never matched and every
# capacity row sat one column short of every quota row. Nothing raised; the
# numbers were just misaligned.
#
# Both the helper and the composed cell are gone. `used`, `limit` and `free`
# are real columns that `table` right-aligns, so there is no trailing word to
# compare and no second code path for a capacity figure to fall down.
# `test_every_figure_column_is_right_aligned_and_one_token_per_cell` asserts
# the property that replaced it, on all four states the figure cell can be in.


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


def test_the_table_draws_no_bar_and_no_percentage_either():
    """Eight cells of blocks for a lossy copy of the number beside them.

    "why do we need this bar here? ... if you can't [fix it], just get rid of
    it." It was also blank on five of the ten rows of the live view, since an
    unlimited quota has no fraction and a capacity fallback has no quota, so
    the one thing a meter column is for, being scanned down, it could not do.

    **The percentage that replaced it has since gone the same way.** It was
    one of the three shapes crowded into a single `space` cell, and `free`
    answers what it was for ("how much can I still put here") as a figure in
    the unit the reader acts in rather than as a ratio they have to multiply
    back out. Fullness survives as the graded COLOUR on the used figure, which
    is what the bar and the percentage were both approximating.
    """
    from dirscape.render import atlas, resolve_style

    style = resolve_style(color="always", ascii_only=False, stream=None)
    root = _measured(_root("/home/me", "fs-home"), used=900_000_000, limit=32_212_254_720)
    root.role = "home"
    text = atlas.render([root], style=style, group=True)

    for glyph in "▏▎▍▌▋▊▉█░":
        assert glyph not in text, "the bar is gone, and %r is one of its cells" % (glyph,)
    assert "%" not in text, "the percentage went with it"
    # Compared on the PLAIN form, because a figure is now two painted runs:
    # the magnitude in the primary tier and the unit one tier down, so `30G`
    # is not a contiguous substring of the coloured output. `plain()` is what
    # every assertion about content should have been using here.
    assert "30G" in cli.plain(text), "the quota is a figure the backend reported, not a ratio"
    # The grading survives, on the used figure.
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
    # The wrap is forced with a long USER, which is all that is left on the
    # line. The node class, the device count, the baseline age and finally the
    # hostname have each come off in turn, every one of them because the owner
    # read it and asked what it was for. The GUARANTEE this test exists for is
    # unchanged and still bites: a login name long enough to wrap does to the
    # frame exactly what a long hostname used to.
    # Long enough to wrap off the title line (13 + 44 > 56), short enough to
    # fit whole on a line of its own, so the assertion below is about WRAPPING
    # and not about truncation.
    meta = {"user": "a-rather-long-login-name-for-just-one-person"}
    lines = atlas.render(roots, meta=meta, style=style, group=True, size=60).splitlines()

    assert lines[1].count("dirscape") == 1
    for line in lines[1:-1]:
        assert line.startswith("│") and line.endswith("│"), "the frame opened: %r" % (line,)
    assert len({len(line) for line in lines}) == 1
    assert "an-unusually-long" not in " ".join(lines), (
        "the hostname is off the default header: the owner asked what the node was for"
    )
    # And the title really did need two lines, or this proves nothing.
    # The tail of the wrapped title is on a line of its own, INSIDE the box.
    assert any("just-one-person" in line for line in lines[1:4]), (
        "the wrapped remainder vanished instead of taking a second line"
    )
    assert "just-one-person" not in lines[1], "it must not still be on the first line"


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
    # Indexed by the internal name, which is still `_ROLE`; the HEADING reads
    # `kind` because that is what the column holds, and the two are allowed to
    # differ exactly as `--json`'s `role` key and this heading do.
    # Indexed by the INTERNAL names, which are still `_ROLE` and `_REACH`.
    # The headings read `kind` and `access` because that is what a reader
    # calls them, and the two vocabularies are allowed to differ exactly as
    # `--json`'s `role` and `reach` keys and these headings do.
    display = {"kind": "role", "access": "reach"}
    names = [
        [display.get(atlas.COLUMNS[i], atlas.COLUMNS[i]) for i in stage]
        for stage in atlas.DROP_STAGES
    ]
    assert names == [
        [],
        ["policy"],
        ["policy", "files"],
        ["policy", "files", "role"],
        ["policy", "files", "role", "reach"],
        ["policy", "files", "role", "reach", "where"],
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
        _LIMIT,
        _PATH,
        _POLICY,
        _REACH,
        _ROLE,
        _USED,
        _WHERE,
        COLUMNS,
        _plan,
    )

    live = [
        ("home", "/home/jdoe42", "857M", "30G"),
        ("project", "/project/hpc", "11T", "none"),
        ("scratch", "/scratch/collie3/jdoe42", "0B", "400G"),
        ("scratch", "/scratch/meadow3/jdoe42", "22G", "100G"),
        ("software", "/software", "314G", "none"),
    ]
    rows = []
    for role, path, used, limit in live:
        row = [""] * len(COLUMNS)
        row[_ROLE], row[_PATH], row[_USED] = role, path, used
        row[_LIMIT] = limit
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

    blind, _ = _plan(rows, 68)
    assert _ROLE not in blind, (
        "the bug this guards: measured against the empty columns too, the only "
        "stage that fits is the one that gives up role"
    )

    columns, stacked = _plan(rows, 68, skip=empty)
    assert not stacked
    assert _ROLE in columns, "role fits once the blanks are not charged for"
    assert _PATH in columns and _USED in columns and _REACH in columns


def test_spare_width_buys_a_column_first_and_then_fills_the_window():
    """Both halves of the width policy, and it took three tries to get here.

    The owner asked for the view to use the whole horizontal space, and the
    first two answers were bad in the same way. Stretching one gutter gave a
    single 40 space gap at 126 columns; stretching them all gave 20 spaces
    between `role` and `path`. Both were padding, and the owner's verdict was
    "the space should be utilized well. but now it's terrible."

    The reason both failed was upstream of the stretching: the view had four
    columns because the figures were crowded into one cell and the file count
    was suppressed outright, so there was nothing to spread BETWEEN. With
    seven columns the same leftover room is a few characters per gutter.

    So the policy is ordered, and both halves are asserted, because either
    alone is satisfiable by doing nothing:

    1. spare width buys a real column, so `files` appears when it fits;
    2. what is left over fills the window, so the frame reaches the edge.
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
        # Limits and inode limits vary too. A column identical on every row is
        # dropped as a caption, and a fixture that repeats one is measuring
        # the constant-column rule rather than the width rule: with one shared
        # limit, `limit` vanished and `files` fitted at 72 columns, which made
        # this test fail for a reason that had nothing to do with it.
        root = _measured(
            _root(path, "fs" + role),
            used=876_543_210 * (offset + 1),
            limit=32_212_254_720 * (offset + 1),
            inodes=37_000 * (offset + 1),
            inode_limit=300_000 * (offset + 1),
        )
        root.role = role
        roots.append(root)

    # 60, measured: with `limit` off the table this fixture fits `files` from
    # 64 columns up, so 60 is the first width below that. The point is to be
    # under the threshold, wherever the threshold currently sits.
    narrow = atlas.render(roots, style=style, group=True, size=60)
    wide = atlas.render(roots, style=style, group=True, size=110)

    assert "files" not in narrow, "at 60 columns the file count is the first thing to go"
    assert "files" in wide, "at 110 columns the room must buy a column before whitespace"

    for window, text in ((60, narrow), (110, wide)):
        widths = {measure(line) for line in text.splitlines()}
        assert widths == {window}, "at %d columns the block is %s wide" % (
            window,
            sorted(widths),
        )


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


def test_an_uncapped_quota_says_none_and_never_a_missing_number():
    """Three facts, three columns, and `none` is not `?`.

    This replaces two earlier attempts at the same complaint. The cell first
    read `11T / no limit`, then `11T used`, and the owner's answer to the
    second was "simply saying 11T used but no cap is very confusing". Both
    tried to say two things in one box.

    Split, the only remaining question is what the `limit` cell says when
    there is no cap, and the answer must not be a blank or `?`: a backend that
    printed `0` told us no limit is enforced, which is KNOWLEDGE, and `?`
    means nobody measured it. The package has never let those two converge.
    """
    from dirscape.render import atlas, resolve_style

    style = resolve_style(color="never", ascii_only=False, stream=None)

    capped = _measured(_root("/a", "fa"), used=876_543_210, limit=32_212_254_720)
    uncapped = _measured(_root("/b", "fb"), used=11_000_000_000_000, limit=0)
    unmeasured = _root("/c", "fc")
    unmeasured.reach = Reach.LISTABLE
    unmeasured.writable = confirmed()
    for root in (capped, uncapped, unmeasured):
        root.role = "project"

    # Asserted through the cell rather than by slicing the rendered row, so
    # the guarantee survives the column moving on and off the table, which it
    # has now done twice.
    from dirscape.render import fields

    assert fields.plain(fields.limit_cell(capped, style)) == "30G", "a real cap is the figure"
    assert fields.plain(fields.limit_cell(uncapped, style)) == "none", (
        "an explicit zero means no limit is enforced"
    )
    assert fields.plain(fields.limit_cell(unmeasured, style)) == "?", (
        "and nobody measuring it is a different fact"
    )

    table = atlas.render([capped, uncapped, unmeasured], style=style, group=True, size=100)
    assert "no limit" not in table, "the two-word form prompted the question"
    assert "none" in table, "and the one-word form is on the table"


def test_every_interactive_frame_is_the_same_width():
    """The frame must not resize as the reader moves or drills in.

    Owner: "when going to different dirs, the ui will shrink the horizontal
    spacing, which is annoying. that shouldn't change." It was shrink-wrapping
    the detail panel to whatever the opened row happened to say while the
    table filled the window, so the box jumped narrower on the way in, wider
    on the way out, and to a different width for each row. The two views are
    one screen replacing another in place, so a width that moves reads as the
    layout breaking.

    Asserted across every row and three windows, because a single row cannot
    show a width that varies BETWEEN rows, which is what the reader saw.
    """
    from dirscape.render import resolve_style
    from dirscape.render.style import width as measure

    style = resolve_style(color="never", ascii_only=False, stream=None)
    run = cli.Run()
    run.roots = []
    for offset, (path, role) in enumerate(
        (
            ("/home/me", "home"),
            # A long path and a short one, so the content width really does
            # differ between rows.
            ("/project/a-long-project-directory-name/me", "project"),
            ("/tmp", "local"),
        )
    ):
        root = _measured(
            _root(path, "fs" + role),
            used=876_543_210 * (offset + 1),
            limit=32_212_254_720 * (offset + 1),
        )
        root.role = role
        root.writable = confirmed()
        run.roots.append(root)

    for window in (80, 100, 118):
        seen = {}
        seen["table"] = {
            measure(line)
            for line in cli._table_frame(run.roots, 0, run=run, style=style, width=window)
        }
        for index, root in enumerate(run.roots):
            seen["row %d" % index] = {
                measure(line) for line in cli._detail(run, root, style, cols=window, window=40)
            }
        for name, widths in seen.items():
            assert widths == {window}, "%s is %s wide in a %d column window" % (
                name,
                sorted(widths),
                window,
            )


def test_a_device_wide_user_quota_reaches_a_path_with_no_fileset():
    """One of the question marks was a bug, not a limit of the filesystem.

    `/scratch/meadow2/jdoe42` read `?` for both figures while
    `mmlsquota -u jdoe42 meadow2_perf` reported 0 used against a 100G quota.
    Nobody was withholding it: `mmlsattr -L` calls the path's fileset `root`
    (GPFS's name for a filesystem's own top level) and the backend labels a
    device-wide USR row with the DEVICE, so the caller compared `root`
    against `meadow2_perf` and matched nothing.

    Attributing it is correct rather than convenient: a user-scope quota on a
    device covers that user everywhere on the device, so it necessarily covers
    this path.
    """
    row = QuotaRow(
        "meadow2_perf", "blocks", "user", 0, soft=107_374_182_400, mount="/scratch/meadow2"
    )
    row.device = "meadow2_perf"
    snap = QuotaSnapshot("mmlsquota", [row])

    root = _root("/scratch/meadow2/me", fileset="root", device="meadow2_perf")
    root.policy = {"fileset_is_filesystem_root": True}
    assert cli._rows_governing(snap, root) == [row], "the device-wide row governs this path"

    # And the caveat travels with it, because the figure is the user's usage
    # across the whole device rather than this directory's.
    assert any("whole of meadow2_perf" in note for note in root.notes), root.notes


def test_a_device_wide_quota_does_not_reach_a_path_that_has_its_own_fileset():
    """The guard that keeps the fix from becoming the leak it sits next to.

    `_rows_governing` exists because a `project-hpc` row once reported its
    11T against five other PIs' directories. A root WITH a fileset of its own
    must never fall back to a device-wide row: the narrower scope is the
    answer, and its absence means nothing was measured for it.
    """
    row = QuotaRow("meadow3_cap", "blocks", "user", 500, soft=1000, mount="/project")
    row.device = "meadow3_cap"
    snap = QuotaSnapshot("mmlsquota", [row])

    mine = _root("/project/hpc", fileset="project-hpc", device="meadow3_cap")
    assert cli._rows_governing(snap, mine) == [], "a real fileset is never shadowed"

    # Nor does a GROUP-scoped device row qualify for a fileset-less path.
    grouped = QuotaRow("meadow3_cap", "blocks", "group", 500, soft=1000, mount="/project")
    grouped.device = "meadow3_cap"
    top = _root("/gpfs/meadow3/cap", fileset="root", device="meadow3_cap")
    top.policy = {"fileset_is_filesystem_root": True}
    assert cli._rows_governing(QuotaSnapshot("mmlsquota", [grouped]), top) == [], (
        "only a user-scope row can be attributed this way"
    )


def test_measure_walks_only_the_rows_the_view_shows(tmp_path):
    """`--measure` answers the unknowns that no quota system can.

    Owner: "do you have a way to tell the exact number? having too many ? can
    impact user experience, and they will think you don't know things." On a
    mount with `noquota` there is genuinely nothing to ask, so the only source
    of truth is adding the files up, and that is offered rather than assumed.

    The first version walked every discovered root and took 8.7 seconds while
    producing no figures at all: the candidates included whole shared dataset
    trees nobody was looking at, each burning its deadline on a partial sum
    that was then correctly discarded. It walks the shown rows only.
    """
    home = tmp_path / "home"
    home.mkdir()
    (home / "a").write_bytes(b"x" * 4096)
    (home / "sub").mkdir()
    (home / "sub" / "b").write_bytes(b"y" * 4096)

    # A second root that the default view HOLDS BACK, standing in for the
    # shared dataset trees the first version walked. Without it this test
    # passes whether the scope is `shown` or `run.roots`, because they are the
    # same list, and the defect it guards is precisely that they are not.
    other = tmp_path / "shared"
    other.mkdir()
    (other / "big").write_bytes(b"q" * 8192)

    run = cli.Run()
    root = _root(str(home))
    root.role = "home"
    root.quota = None
    root.writable = confirmed()
    # The mount table's evidence that no limit exists, as `attribute_xfs` sets
    # it for a `noquota` mount. The walk does not supply that fact itself.
    root.policy = {"no_quota_enforced": True}
    secondary = _root(str(other))
    secondary.role = "dataset"
    secondary.quota = None
    secondary.policy = {"rank": "secondary"}
    run.roots = [root, secondary]

    cli._measure(run)

    assert secondary.quota is None, (
        "a root the default view holds back must not be walked: that is what made "
        "the first version take 8.7 seconds on shared dataset trees"
    )
    assert root.quota is not None, "a walk must produce a figure"
    used, _caveat = render_fields.used_cell(root, Style())
    assert "?" not in used, "the unknown must be gone: %r" % (used,)
    assert root.inode_quota is not None
    assert "2" in render_fields.file_count_cell(root, Style()), "two files were counted"
    # Uncapped, not unmeasured: walking says how much is there and there is no
    # ceiling on a filesystem with no quota system.
    assert render_fields.plain(render_fields.limit_cell(root, Style())) == "none"


def test_a_walk_never_claims_there_is_no_limit_on_its_own(tmp_path):
    """Measured with the quota sweep cut short by `--timeout 1`.

    `/scratch/meadow3/jdoe42` reached the walk because no backend had spoken
    for it YET, was added up, and rendered `22G of none` while GPFS enforces
    100G on it. A walk says how much is there; whether a ceiling exists is a
    different question and, without the mount table's word, stays unknown.
    """
    home = tmp_path / "scratch" / "me"
    home.mkdir(parents=True)
    (home / "a").write_bytes(b"x" * 4096)

    run = cli.Run()
    root = _root(str(home))
    root.role = "scratch"
    root.quota = None
    root.writable = confirmed()
    run.roots = [root]

    cli._measure(run)

    assert root.quota is not None, "the usage is still measured"
    assert "?" not in render_fields.used_cell(root, Style())[0]
    assert render_fields.plain(render_fields.limit_cell(root, Style())) == "?", (
        "no evidence of a missing quota, so the limit must stay unknown"
    )


def test_a_walk_that_runs_out_of_time_leaves_the_unknown_alone(tmp_path):
    """A partial sum reported as a total is worse than no number at all."""
    home = tmp_path / "home"
    home.mkdir()
    for index in range(40):
        part = home / ("d%d" % index)
        part.mkdir()
        (part / "f").write_bytes(b"z" * 512)

    run = cli.Run()
    root = _root(str(home))
    root.role = "home"
    root.quota = None
    run.roots = [root]

    saved = cli.WALK_SECONDS
    cli.WALK_SECONDS = -1.0  # already past the deadline on the first check
    try:
        cli._measure(run)
    finally:
        cli.WALK_SECONDS = saved

    assert root.quota is None, "an unfinished walk must not publish a figure"
    assert any("too large to add up quickly" in note for note in root.notes), root.notes


def test_a_tree_too_large_to_count_is_abandoned_on_the_ENTRY_bound(tmp_path):
    """Two bounds, and this is the one that protects a login node.

    A deadline alone means a pathological tree costs its full `WALK_SECONDS`
    before being abandoned, once per root. The entry count lets the walk
    recognise within milliseconds that this is not a directory it can add up.
    Measured against `/software`: it gives up in 2ms at a ceiling of 50 and
    burns the whole 1.5s deadline at 100k without finishing.

    Tested with the DEADLINE left generous on purpose. The sibling test sets a
    deadline in the past, so it exercises the clock and would pass whether or
    not this bound existed at all.
    """
    home = tmp_path / "home"
    home.mkdir()
    for index in range(30):
        (home / ("f%d" % index)).write_bytes(b"z" * 512)

    run = cli.Run()
    root = _root(str(home))
    root.role = "home"
    root.quota = None
    run.roots = [root]

    saved = cli.WALK_ENTRIES
    cli.WALK_ENTRIES = 1
    try:
        cli._measure(run)
    finally:
        cli.WALK_ENTRIES = saved

    assert root.quota is None, "a walk stopped by the entry bound publishes nothing"
    assert any("too large to add up quickly" in note for note in root.notes), root.notes

    # And with the bound restored the same tree is counted.
    cli._measure(run)
    assert root.quota is not None
    assert "?" not in render_fields.used_cell(root, Style())[0]


def test_noquota_in_the_mount_options_is_knowledge_not_a_shrug(tmp_path):
    """`none` and `?` are different facts, and the mount table settles one.

    A filesystem mounted `noquota` enforces no limit. That is knowledge, so
    the limit cell says `none`; without the flag it read `?` on two rows where
    the mount table had already answered, which is the tool withholding what
    it knows. The absence of `noquota` implies nothing and nothing is inferred
    from it.
    """
    bare = _root("/tmp/whatever")
    assert render_fields.plain(render_fields.limit_cell(bare, Style())) == "?"

    known = _root("/tmp/whatever")
    known.policy = {"no_quota_enforced": True}
    assert render_fields.plain(render_fields.limit_cell(known, Style())) == "none"


def test_access_reads_as_words_and_the_two_views_agree():
    """`rwx` was correct, exact, and addressed to somebody who reads `ls -l`.

    Owner: "since we have a lot of horizontal spacing, don't use rwx, just use
    regular words so that it's new user friendly." There is room for twelve
    characters and a new user should not have to decode a cell to learn they
    can write to their own home directory.

    The distinction the POSIX triple existed to preserve is preserved: `read`
    means nobody checked whether you can write, which is a different answer
    from `read only`. That was the whole reason for `r?x` and it is the one
    thing a word form could quietly lose.

    Both views take the phrase from one place, because they used to disagree
    about the single thing both are for: the table said `rwx` and `why` said
    "you can see what is in this directory, and you can write to it".
    """
    from dirscape.render import atlas, resolve_style

    style = resolve_style(color="never", ascii_only=False, stream=None)

    writable = _measured(_root("/a", "fa"), used=1000, limit=2000)
    writable.writable = confirmed()
    readonly = _measured(_root("/b", "fb"), used=1000, limit=2000)
    readonly.writable = refuted(VerdictCategory.ACCESS_DENIED)
    unchecked = _measured(_root("/c", "fc"), used=1000, limit=2000)
    for root in (writable, readonly, unchecked):
        root.role = "project"

    assert render_fields.access_words(writable) == "read + write"
    assert render_fields.access_words(readonly) == "read only"
    assert render_fields.access_words(unchecked) == "read", (
        "an unprobed write must not read as a refused one"
    )

    traverse = _root("/d", reach=Reach.TRAVERSE)
    assert render_fields.access_words(traverse) == "enter only"
    shut = _root("/e", reach=Reach.CLOSED)
    assert render_fields.access_words(shut) == "no access"
    nothing = _root("/f", reach=Reach.UNKNOWN)
    assert render_fields.access_words(nothing) == "?"

    text = atlas.render([writable, readonly, unchecked], style=style, group=True, size=110)
    assert "rwx" not in text and "r-x" not in text, "no POSIX triple survives on the table"
    assert "read + write" in text and "read only" in text

    # And `why` prints the same phrase the table just showed.
    run = cli.Run()
    run.roots = [writable]
    detail, _ = cli._why(run, "/a", style)
    assert "access    read + write" in detail


def test_the_quota_heading_is_the_word_the_site_itself_uses():
    """`limit` was ambiguous and `your limit` sounded cheap.

    Owner, twice. First: "what does limit mean? does it mean there is no user
    level limit or the dir has some ceiling but there is no restriction on the
    user side?" The ambiguity was real rather than a wording slip:
    `QuotaRow.scope` is `user`, `group` or `fileset`, so the same cell can be a
    personal allowance or the ceiling on everything in a directory. Then, of
    the fix: "don't use `your use` / `your limit`. it sounds cheap."

    `quota` is what the site's own tool prints over the same figure and the
    word a researcher uses for it, and the scope question is answered in `why`
    where there is room for it. Verified against the live wrapper: it heads
    that column `quota` and the one beside it `used`.
    """
    from dirscape.render import atlas, resolve_style

    style = resolve_style(color="never", ascii_only=False, stream=None)
    root = _measured(_root("/a", "fa"), used=1000, limit=2000)
    root.role = "project"
    text = atlas.render([root], style=style, group=True, size=110)

    assert "used" in text and "quota" in text
    assert "your" not in text, "a heading should not have to insist whose number it is"
    assert "limit" not in text, "the ambiguous word is gone from the table"


def _plain_style():
    from dirscape.render import resolve_style

    return resolve_style(color="never", ascii_only=False, stream=None)


def _settle(sizes, limit=20.0):
    """Wait for the walks behind a view, then fold them in as a repaint would."""
    import time

    stop = time.time() + limit
    while sizes.busy() and time.time() < stop:
        time.sleep(0.02)
    assert not sizes.busy(), "the sizes never settled"
    sizes.refresh()


def _opened(path, cursor=0, width=100, height=40, sized=True, cache=None, **sizing):
    """Open one directory as `_descend` does, with its sizes added up first."""
    run = cli.Run()
    kids, held, _answered = cli._children(str(path))
    roots = [cli._listed_root(kid, None, run.site, {}) for kid in kids]
    sizes = cli._Sizes(roots, {} if cache is None else cache, **sizing)
    if sized:
        sizes.start()
        _settle(sizes)
    style = _plain_style()
    lines, _top, _room = cli._dir_frame(
        str(path),
        roots,
        cursor,
        run,
        style,
        width,
        height=height,
        held=held,
        notes=cli._dir_notes(roots, style),
    )
    return lines, roots, sizes


def test_opening_a_row_shows_the_same_table_one_level_down(tmp_path):
    """Owner: "what the sub-dirs should show should be the exact same structure
    as what the app shows at the start". It showed a table of its own, `name`,
    `access` and `items`, and `items` was "a confusing word": the number of
    names directly inside, which is neither a size nor a file count. Now it is
    the start screen's own table, with `used` and `files` added up per child.
    """
    root = tmp_path / "project"
    for name in ("alpha", "beta", "gamma"):
        (root / name).mkdir(parents=True)
    (root / "alpha" / "inner").mkdir()
    (root / "a-file.txt").write_bytes(b"x")
    for index in range(3):
        (root / "beta" / ("f%d" % index)).write_bytes(b"x" * 100)

    lines, roots, _sizes = _opened(root)
    text = cli.plain("\n".join(lines))

    assert [r.path for r in roots] == [str(root / n) for n in ("alpha", "beta", "gamma")]
    heading = [line for line in text.splitlines() if "path" in line and "used" in line]
    assert heading and "files" in heading[0], "the table's own headings"
    assert "items" not in text
    assert "a-file.txt" not in text, "files are not places to descend into"
    assert "inner" not in text, "only direct children, never a walk"
    assert "…" not in text, "every figure landed"
    names = ("alpha/", "beta/", "gamma/")
    rows = {
        p[1]: p
        for p in (line.split() for line in text.splitlines())
        if p[1:2] in [[n] for n in names]
    }
    assert sorted(rows) == list(names), "each row names its child, and the title the rest"
    assert rows["beta/"][3] == "3", "beta's three files, added up"
    assert rows["beta/"][4] == "100%", "and all of what the directory holds"
    # The band is on the first row and INSIDE the frame, as the table's is.
    banded = [line for line in lines if "\033[7m" in line]
    assert len(banded) == 1 and "alpha/" in cli.plain(banded[0])
    before, _, _inverted = banded[0].partition("\033[7m")
    assert "│" in before, "the left border must not be inverted with the row"


def test_a_figure_that_is_coming_says_so_and_is_not_a_question_mark(tmp_path):
    (tmp_path / "d").mkdir()
    lines, roots, _sizes = _opened(tmp_path, sized=False)
    assert "…" in cli.plain("\n".join(lines))
    assert roots[0].policy.get("measuring") is True


def test_an_opened_directory_scrolls_instead_of_outgrowing_the_window(tmp_path):
    """A directory with more children than the terminal has rows.

    A block taller than the window cannot be repainted in place: `select`
    moves the cursor up by the lines it wrote, and a block that has scrolled
    takes the reader's scrollback with it. So the band tracks the cursor however
    far down it goes, and the block never outgrows the window.
    """
    root = tmp_path / "many"
    for index in range(80):
        (root / ("child%02d" % index)).mkdir(parents=True)
    run = cli.Run()
    kids, _held, _answered = cli._children(str(root))
    roots = [cli._listed_root(kid, None, None, {}) for kid in kids]
    _settle_all = cli._Sizes(roots, {})
    _settle_all.start()
    _settle(_settle_all)

    window = 24
    top = None
    for cursor in (0, 40, 79):
        lines, top, room = cli._dir_frame(
            str(root), roots, cursor, run, _plain_style(), 100, height=window, top=top
        )
        assert len(lines) < window, "a %d line block in a %d row window" % (len(lines), window)
        banded = [line for line in lines if "\033[7m" in line]
        assert len(banded) == 1, "the band vanished at cursor %d" % (cursor,)
        assert "child%02d" % cursor in cli.plain(banded[0])
        assert "of 80" in cli.plain("\n".join(lines)), "the reader is told what is off screen"
        assert room < 80


def test_a_directory_with_nothing_inside_says_so(tmp_path):
    """Rather than an empty frame the reader has to interpret."""
    lines = cli._empty_frame(str(tmp_path), _plain_style(), 100)
    assert "nothing to open" in cli.plain("\n".join(lines))


def test_a_child_too_big_for_the_glance_is_walked_to_the_end_behind_it(tmp_path):
    """Owner, on a `/project` where all but 21 of 84 folders read `?`: "it
    looks annoying". The glance leaves a floor, and the second pass finishes."""
    (tmp_path / "big").mkdir()
    for index in range(5):
        (tmp_path / "big" / ("f%d" % index)).write_bytes(b"x")

    _lines, roots, _sizes = _opened(tmp_path, entries=2)
    assert "floor" not in roots[0].policy
    assert cli.plain(cli.render_fields.file_count_cell(roots[0])) == "5"


def test_the_glance_shrinks_so_the_first_pass_reaches_every_folder(tmp_path):
    """At a fixed 1.5s, 50 of `/project/rcc`'s 84 folders still had no figure a
    minute in, while the second pass spent the time on the big ones."""
    roots = [_sized("d%02d" % i) for i in range(84)]
    sizes = cli._Sizes(roots, {}, per_child_s=1.5, glance_s=15.0)
    assert sizes.per_child_s == cli.GLANCE_MIN_S
    assert sizes.glance_s >= 84 * cli.GLANCE_MIN_S, "every folder gets its glance"
    few = cli._Sizes([_sized("a"), _sized("b")], {}, per_child_s=1.5, glance_s=15.0)
    assert few.per_child_s == 1.5, "a small directory keeps the full glance"


def test_an_unfinished_count_never_shows_a_number(tmp_path):
    """The owner's `/project/rcc/youzhi`, over 10T in 3M files, read `288G+`: what
    a glance had counted, printed as a size. "this is just laughable shit"."""
    (tmp_path / "big").mkdir()
    for index in range(5):
        (tmp_path / "big" / ("f%d" % index)).write_bytes(b"x")

    lines, roots, sizes = _opened(tmp_path, entries=2, total_s=0.0)
    row = [line.split() for line in cli.plain("\n".join(lines)).splitlines() if " big/ " in line][0]
    assert row[2:4] == ["?", "?"], "no partial figure, however it is marked"
    assert "not counted" in cli.plain("\n".join(lines))
    assert str(tmp_path / "big") not in sizes.cache, "an unfinished count is not remembered"

    assert sizes.measure(roots[0]), "m counts it to the end, even past the allowance"
    _settle(sizes)
    assert cli.plain(cli.render_fields.file_count_cell(roots[0])) == "5"


def test_the_status_line_names_the_folder_being_counted(tmp_path):
    style = _plain_style()
    big = _root(str(tmp_path / "big"), reach=Reach.LISTABLE)
    lines = [cli.plain(line) for line in cli._dir_notes([big], style, big, 3_100_000)]
    assert lines == ["   \u2026 counting big/: 3.1M entries so far"]
    assert cli._dir_notes([big], style) == [], "nothing to say when nothing is counting"


def test_m_interrupts_the_count_in_flight_and_that_one_is_counted_after(tmp_path, monkeypatch):
    import threading
    import time

    (tmp_path / "huge").mkdir()
    (tmp_path / "wanted").mkdir()
    (tmp_path / "wanted" / "f").write_bytes(b"x")
    order = []
    release = threading.Event()

    def walk(path, **kwargs):
        order.append(path.rsplit("/", 1)[1])
        if path.endswith("huge") and len(order) == 1:
            kwargs["stop"].wait(10)
            return argparse.Namespace(size=0, files=0, symlinks=0, specials=0, partial=True)
        return argparse.Namespace(size=10, files=1, symlinks=0, specials=0, partial=False)

    monkeypatch.setattr(cli, "_rapidu_walk", lambda: walk)
    roots = [cli._listed_root(k, None, None, {}) for k in cli._children(str(tmp_path))[0]]
    sizes = cli._Sizes(roots, {}, glance_s=0.0)
    huge, wanted = roots
    sizes.start()
    stop = time.time() + 5
    while sizes.counting()[0] is not huge and time.time() < stop:
        time.sleep(0.01)
    assert sizes.measure(wanted)
    _settle(sizes)
    release.set()
    assert order == ["huge", "wanted", "huge"], "interrupted, the wanted one, then counted again"
    assert str(tmp_path / "huge") in sizes.cache and str(tmp_path / "wanted") in sizes.cache


def test_a_directory_out_of_time_says_so_and_can_still_be_measured(tmp_path):
    (tmp_path / "late").mkdir()
    lines, roots, sizes = _opened(tmp_path, glance_s=0.0, total_s=0.0)
    assert roots[0].policy.get("not_added") is True
    assert "not counted" in cli.plain("\n".join(lines))
    assert sizes.measure(roots[0])
    _settle(sizes)
    assert cli.plain(cli.render_fields.file_count_cell(roots[0])) == "0"


def test_sizes_are_not_walked_twice_in_one_session(tmp_path):
    (tmp_path / "d").mkdir()
    (tmp_path / "d" / "f").write_bytes(b"x")
    cache = {}
    _opened(tmp_path, cache=cache)
    assert str(tmp_path / "d") in cache

    _lines, roots, sizes = _opened(tmp_path, cache=cache, sized=False)
    assert not sizes.busy() and not sizes._todo, "nothing left to walk"
    assert cli.plain(cli.render_fields.file_count_cell(roots[0])) == "1"


#: The real one, taken before conftest swaps it out for every test.
_REAL_SIZE_INDEX = cli._size_index


def _open_level(path, cache, index, **sizing):
    """One level as `_descend` builds it: the listing's roots, inodes and sizer."""
    kids, _held, _answered = cli._children(str(path))
    roots = [cli._listed_root(kid, None, None, {}) for kid in kids]
    inodes = {str(kid["path"]): kid.get("ino") for kid in kids}
    return roots, cli._Sizes(roots, cache, index=index, inodes=inodes, **sizing)


def _files_cell(root):
    return cli.plain(cli.render_fields.file_count_cell(root))


def test_a_folder_counted_on_an_earlier_run_opens_with_its_figures(tmp_path):
    """Owner, on `/project/rcc`: "this dir is so slow". Its first count took
    22.8 minutes, and no walker makes that fast. Every open after it shows the
    figures from the moment it is drawn, and one counted within `FRESH_S` is
    not walked at all."""
    from dirscape.state.sizes import SizeIndex

    (tmp_path / "lab" / "a").mkdir(parents=True)
    (tmp_path / "lab" / "a" / "f").write_bytes(b"x" * 10)
    where = str(tmp_path / "sizes.jsonl")
    roots, sizes = _open_level(tmp_path / "lab", {}, SizeIndex(where, host="login1"))
    sizes.start()
    _settle(sizes)
    assert _files_cell(roots[0]) == "1"

    roots, sizes = _open_level(tmp_path / "lab", {}, SizeIndex.load(path=where, host="login1"))
    assert _files_cell(roots[0]) == "1", "on screen before anything is walked"
    assert not (sizes._todo or sizes._stale), "counted within the hour: nothing to walk"
    age, again = sizes.stored()
    assert age < 60 and not again
    note = cli.plain("\n".join(cli._dir_notes(roots, _plain_style(), stored=sizes.stored())))
    assert "sizes as counted" in note and "m counts the highlighted row again" in note


def test_an_old_stored_figure_stays_on_screen_while_it_is_counted_again(tmp_path):
    import time

    from dirscape.state.sizes import SizeIndex

    folder = tmp_path / "lab" / "a"
    folder.mkdir(parents=True)
    for n in range(3):
        (folder / ("f%d" % n)).write_bytes(b"x")
    index = SizeIndex(str(tmp_path / "s.jsonl"), host="h")
    index.put(str(folder), 1, 1, time.time() - 2 * cli.FRESH_S, ino=os.lstat(str(folder)).st_ino)
    roots, sizes = _open_level(tmp_path / "lab", {}, index)
    assert _files_cell(roots[0]) == "1", "the old figure, at once"
    assert not roots[0].policy.get(render_fields.MEASURING), "a figure, never an ellipsis"
    assert sizes.stored()[1], "and it is being counted again"
    note = cli.plain("\n".join(cli._dir_notes(roots, _plain_style(), stored=sizes.stored())))
    assert "counting again behind the view" in note

    sizes.start()
    _settle(sizes)
    assert _files_cell(roots[0]) == "3"
    assert sizes.stored() is None, "the age line goes with the last stored figure"
    assert index.get(str(folder))[1] == 3, "and the new count is the one kept"


def test_folders_with_no_figure_are_counted_before_old_ones_are_counted_again(
    tmp_path, monkeypatch
):
    import time

    from dirscape.state.sizes import SizeIndex

    for name in ("new", "old"):
        (tmp_path / "lab" / name).mkdir(parents=True)
    old = str(tmp_path / "lab" / "old")
    index = SizeIndex()
    index.put(old, 1, 1, time.time() - 2 * cli.FRESH_S)
    order = []
    real = cli._size_of

    def spy(path, *args, **kwargs):
        order.append(os.path.basename(path))
        return real(path, *args, **kwargs)

    monkeypatch.setattr(cli, "_size_of", spy)
    _roots, sizes = _open_level(tmp_path / "lab", {}, index)
    sizes.start()
    _settle(sizes)
    assert order == ["new", "old"]


def test_a_stored_figure_for_a_folder_made_again_is_not_its_figure(tmp_path):
    """Same name, another inode: the old folder's figure would be a confident lie."""
    import time

    from dirscape.state.sizes import SizeIndex

    folder = tmp_path / "lab" / "a"
    folder.mkdir(parents=True)
    index = SizeIndex()
    index.put(str(folder), 5, 5, time.time(), ino=os.lstat(str(folder)).st_ino + 1)
    roots, sizes = _open_level(tmp_path / "lab", {}, index)
    assert roots[0].policy.get(render_fields.MEASURING) and sizes.stored() is None


def test_a_counted_folder_opens_with_its_own_folders_already_counted(tmp_path):
    """The walk of `a` saw everything under it, so opening `a` walks nothing."""
    inner = tmp_path / "lab" / "a"
    for name, count in (("x", 1), ("y", 2)):
        (inner / name).mkdir(parents=True)
        for n in range(count):
            (inner / name / ("f%d" % n)).write_bytes(b"x")
    cache = {}
    _roots, sizes = _open_level(tmp_path / "lab", cache, None)
    sizes.start()
    _settle(sizes)
    roots, sizes = _open_level(inner, cache, None)
    assert not sizes._todo, "every folder in it was counted by the walk above"
    assert [_files_cell(root) for root in roots] == ["1", "2"]


def test_m_counts_a_stored_figure_again_and_keeps_it_on_screen(tmp_path):
    import time

    from dirscape.state.sizes import SizeIndex

    folder = tmp_path / "lab" / "a"
    folder.mkdir(parents=True)
    (folder / "f").write_bytes(b"x")
    index = SizeIndex()
    index.put(str(folder), 1, 9, time.time())
    roots, sizes = _open_level(tmp_path / "lab", {}, index)
    assert not sizes._stale, "fresh, so nothing would count it"
    assert sizes.measure(roots[0])
    assert not roots[0].policy.get(render_fields.MEASURING)
    _settle(sizes)
    assert _files_cell(roots[0]) == "1"


def test_running_out_of_time_leaves_a_stored_figure_standing(tmp_path):
    import time

    from dirscape.state.sizes import SizeIndex

    for name in ("new", "old"):
        (tmp_path / "lab" / name).mkdir(parents=True)
    index = SizeIndex()
    index.put(str(tmp_path / "lab" / "old"), 7, 7, time.time() - 2 * cli.FRESH_S)
    roots, sizes = _open_level(tmp_path / "lab", {}, index, glance_s=0.0, total_s=0.0)
    sizes.start()
    _settle(sizes)
    by_name = {os.path.basename(root.path): root for root in roots}
    assert by_name["new"].policy.get("not_added")
    assert not by_name["old"].policy.get("not_added") and _files_cell(by_name["old"]) == "7"


def test_the_walk_hands_over_every_subtree_it_finished(tmp_path):
    import threading

    for name, count in (("a", 1), ("b", 3)):
        (tmp_path / name / "deep").mkdir(parents=True)
        for n in range(count):
            (tmp_path / name / "deep" / ("f%d" % n)).write_bytes(b"x" * 100)
    (tmp_path / "top-file").write_bytes(b"x")
    parts = {}
    used, files, done = cli._walk(str(tmp_path), 1e18, 10**9, stop=threading.Event(), parts=parts)
    assert done and files == 5
    top = [path for path in parts if os.path.dirname(path) == str(tmp_path)]
    assert sorted(top) == [str(tmp_path / "a"), str(tmp_path / "b")]
    assert sorted(set(parts) - set(top)) == [
        str(tmp_path / "a" / "deep"),
        str(tmp_path / "b" / "deep"),
    ]
    for path, figures in parts.items():
        assert figures == cli._walk(path, 1e18, 10**9)[:2], "what walking it alone counts"
    assert sum(parts[p][1] for p in top) == files - 1, "the file at the top is nobody's part"


def test_a_walk_cut_short_hands_over_only_what_it_finished(tmp_path):
    (tmp_path / "small").mkdir()
    (tmp_path / "small" / "f").write_bytes(b"x")
    (tmp_path / "big").mkdir()
    for n in range(50):
        (tmp_path / "big" / ("d%d" % n)).mkdir()
    parts = {}
    _used, _files, done = cli._walk(str(tmp_path), 1e18, 20, parts=parts)
    assert not done
    assert str(tmp_path / "big") not in parts
    for path, figures in parts.items():
        assert figures == cli._walk(path, 1e18, 10**9)[:2]


def test_rapidu_hands_over_the_folders_it_finished_directly_under_its_root():
    entry = argparse.Namespace
    root = "/lab/a"
    finished = {"x"}
    res = argparse.Namespace(
        root=root,
        dir_agg={
            "/lab/a/x": entry(path="/lab/a/x", is_dir=True, size=10, files=2),
            "/lab/a/y": entry(path="/lab/a/y", is_dir=True, size=20, files=3),
            "/lab/a/f": entry(path="/lab/a/f", is_dir=False, size=5, files=1),
        },
        is_finished=lambda e: os.path.basename(e.path) in finished,
    )
    parts = {}
    cli._rapidu_parts(res, parts)
    assert parts == {"/lab/a/x": (10, 2)}, "unfinished and plain files are not parts"


def test_the_size_index_reaches_every_level_the_reader_opens(tmp_path, monkeypatch):
    """`_descend` named the row it opened `index`, the parameter's own name, so
    from the second level down the sizer was handed a row number for an index."""
    from dirscape import interactive
    from dirscape.state.sizes import SizeIndex

    for name in ("a/inner", "b/inner"):
        (tmp_path / "lab" / name).mkdir(parents=True)
    index = SizeIndex()
    seen = []
    real = cli._Sizes

    class Spy(real):
        def __init__(self, roots, cache, **kwargs):
            seen.append(kwargs.get("index"))
            real.__init__(self, roots, cache, **kwargs)

    monkeypatch.setattr(cli, "_Sizes", Spy)
    answers = iter([1, interactive.Key.BACK, interactive.Key.QUIT])

    def select(paint, count, **kwargs):
        paint(0)
        return next(answers)

    monkeypatch.setattr(cli.interactive, "select", select)
    outcome = cli._descend(str(tmp_path / "lab"), _plain_style(), 100, index=index)
    assert outcome == interactive.Key.QUIT
    assert len(seen) == 3 and all(found is index for found in seen)


def _tree(root, shape):
    """Directories and files under ``root``: {"a/b": 3} is `a/b` holding three files."""
    for where, count in shape.items():
        (root / where).mkdir(parents=True, exist_ok=True)
        for n in range(count):
            (root / where / ("f%d" % n)).write_bytes(b"x" * (100 + n))
    return root


def test_the_threaded_walk_counts_exactly_what_the_serial_one_does(tmp_path):
    """Without rapidu, a network filesystem is counted sixteen directories at a
    time. It must not change one figure, parts included."""
    root = _tree(tmp_path / "t", {"a": 3, "a/b": 2, "a/b/c": 1, "d": 0, "e/f": 5, ".": 2})
    (root / "a" / "link").symlink_to("b")
    serial_parts, threaded_parts = {}, {}
    serial = cli._walk(str(root), 1e18, 10**9, parts=serial_parts)
    tally = cli._Tally()
    threaded = cli._walk_threads(str(root), 1e18, 10**9, parts=threaded_parts, tally=tally)
    assert threaded == serial and serial[2]
    assert threaded_parts == serial_parts
    top = [path for path in threaded_parts if os.path.dirname(path) == str(root)]
    assert len(top) == 3 and len(threaded_parts) == 6, "a, d and e, and below them b, c and f"
    assert tally.inodes > serial[1], "entries read, directories among them"


def test_the_threaded_walk_stops_at_its_deadline_its_stop_and_its_ceiling(tmp_path):
    import threading
    import time

    root = _tree(tmp_path / "t", {"a": 30, "b": 30})
    assert not cli._walk_threads(str(root), time.time() - 1, 10**9)[2], "past the deadline"
    stop = threading.Event()
    stop.set()
    assert not cli._walk_threads(str(root), 1e18, 10**9, stop=stop)[2], "stopped"
    parts = {}
    assert not cli._walk_threads(str(root), 1e18, 5, parts=parts)[2], "over the ceiling"
    for path, figures in parts.items():
        assert figures == cli._walk(path, 1e18, 10**9)[:2], "only what it finished"


def test_without_rapidu_a_network_filesystem_is_counted_with_threads(tmp_path, monkeypatch):
    import threading

    used = []
    monkeypatch.setattr(
        cli, "_walk_threads", lambda *args, **kwargs: used.append(args[5]) or (1, 1, True)
    )
    gpfs = Root(str(tmp_path), fstype="gpfs")
    local = Root(str(tmp_path), fstype="xfs")
    assert (cli._threads_for(gpfs), cli._threads_for(local)) == (cli.WALK_THREADS, 1)
    assert cli._size_of(str(tmp_path), 1e18, 10, threading.Event(), threads=16) == (1, 1, True)
    assert used == [16]
    cli._size_of(str(tmp_path), 1e18, 10, threading.Event(), threads=1)
    assert used == [16], "local storage keeps the serial walk"


def test_m_stops_dirscapes_own_walk_as_it_stops_rapidus(tmp_path, monkeypatch):
    """Without rapidu, `m` had nothing to stop: the folder asked for waited for
    the whole count in flight, however long that was."""
    import time

    for name in ("big", "wanted"):
        (tmp_path / name).mkdir()
    order = []

    def count(path, deadline, ceiling, stop, parts, threads, progress):
        order.append(os.path.basename(path))
        while os.path.basename(path) == "big" and not stop.is_set():
            time.sleep(0.01)
        return (1, 1, os.path.basename(path) != "big")

    monkeypatch.setattr(cli, "_count_here", count)
    roots = [cli._listed_root(kid, None, None, {}) for kid in cli._children(str(tmp_path))[0]]
    sizes = cli._Sizes(roots, {}, glance_s=0.0)
    sizes.start()
    stop = time.time() + 5
    while sizes.counting()[0] is None and time.time() < stop:
        time.sleep(0.01)
    assert os.path.basename(sizes.counting()[0].path) == "big"
    assert sizes.measure(roots[1])
    stop = time.time() + 5
    while "wanted" not in order and time.time() < stop:
        time.sleep(0.01)
    assert order[:2] == ["big", "wanted"], "the wanted folder next, not after `big`"
    sizes.stop()
    _settle(sizes)


class _SlowWalk(object):
    """rapidu's `walk`, where ``slow`` holds until released or stopped."""

    def __init__(self, slow):
        import threading

        self.slow = slow
        self.release = threading.Event()
        self.calls = []

    def __call__(self, path, **kwargs):
        self.calls.append((os.path.basename(path), kwargs.get("threads")))
        stop = kwargs["stop"]
        if os.path.basename(path) == self.slow:
            while not (self.release.is_set() or stop.is_set()):
                stop.wait(0.01)
        return argparse.Namespace(
            root=path, size=10, files=1, symlinks=0, specials=0, partial=stop.is_set(), dir_agg={}
        )


def _gpfs_level(tmp_path, names):
    for name in names:
        (tmp_path / name).mkdir(exist_ok=True)
    parent = Root(str(tmp_path), fstype="gpfs")
    return [cli._listed_root(kid, parent, None, {}) for kid in cli._children(str(tmp_path))[0]]


def _until(check, limit=5.0):
    import time

    stop = time.time() + limit
    while not check() and time.time() < stop:
        time.sleep(0.01)
    assert check()


def test_a_count_under_way_is_not_cut_off_by_the_visits_allowance(tmp_path, monkeypatch):
    """Cut off at the allowance, the work was thrown away, and a folder larger
    than one visit's allowance could never be counted at all."""
    import threading

    walk = _SlowWalk("big")
    monkeypatch.setattr(cli, "_rapidu_walk", lambda: walk)
    roots = _gpfs_level(tmp_path, ["big"])
    sizes = cli._Sizes(roots, {}, glance_s=0.0, total_s=0.05)
    sizes.start()
    _until(lambda: sizes.counting()[0] is not None)
    threading.Timer(0.3, walk.release.set).start()
    _settle(sizes)
    assert str(tmp_path / "big") in sizes.cache, "counted to the end, past the allowance"


def test_opening_a_folder_keeps_the_count_in_flight_going(tmp_path, monkeypatch):
    walk = _SlowWalk("big")
    monkeypatch.setattr(cli, "_rapidu_walk", lambda: walk)
    cache = {}
    first = _gpfs_level(tmp_path, ["big", "small"])
    level = cli._Sizes(first, cache, glance_s=0.0)
    level.start()
    _until(lambda: level.counting()[0] is not None)
    big = level.counting()[0]
    other = [root for root in first if root is not big][0]
    assert level.set_aside(opening=other), "the reader opened another folder"

    # Back up to the same directory: the row being counted waits for that count.
    again = _gpfs_level(tmp_path, ["big", "small"])
    back = cli._Sizes(again, cache, glance_s=0.0, aside=level)
    waiting = [root for root in again if root.path == big.path][0]
    assert waiting.policy.get(render_fields.MEASURING) and back.busy()
    assert back.counting()[0].path == big.path, "the status line names it"
    assert back.measure(waiting), "`m` on it: it is being counted already"
    back.start()
    _until(lambda: ("small", 8) in walk.calls)
    walk.release.set()
    _until(lambda: not level.busy())
    assert back.changed()
    _settle(back)
    assert _files_cell(waiting) == "1", "its figure lands where the reader is"
    assert [name for name, _ in walk.calls].count("big") == 1, "counted once, not again"


def test_opening_the_folder_being_counted_stops_that_count(tmp_path, monkeypatch):
    walk = _SlowWalk("big")
    monkeypatch.setattr(cli, "_rapidu_walk", lambda: walk)
    level = cli._Sizes(_gpfs_level(tmp_path, ["big"]), {}, glance_s=0.0)
    level.start()
    _until(lambda: level.counting()[0] is not None)
    assert not level.set_aside(opening=level.counting()[0])
    _settle(level)


def test_a_count_set_aside_that_ends_unanswered_is_counted_where_the_reader_is(
    tmp_path, monkeypatch
):
    walk = _SlowWalk("big")
    monkeypatch.setattr(cli, "_rapidu_walk", lambda: walk)
    cache = {}
    level = cli._Sizes(_gpfs_level(tmp_path, ["big"]), cache, glance_s=0.0)
    level.start()
    _until(lambda: level.counting()[0] is not None)
    assert level.set_aside()
    again = _gpfs_level(tmp_path, ["big"])
    back = cli._Sizes(again, cache, glance_s=0.0, aside=level)
    level.stop()
    _until(lambda: not level.busy())
    walk.release.set()
    assert back.changed()
    back.refresh()
    _settle(back)
    assert _files_cell(again[0]) == "1"
    assert [name for name, _ in walk.calls] == ["big", "big"]


def test_the_browser_counts_on_behind_a_folder_the_reader_opened(tmp_path, monkeypatch):
    """Owner, on `/project/rcc`: "this dir is so slow". Opening one of its
    folders to look inside stopped the count in flight, and coming back began
    it again from nothing: minutes of a large folder's count, every time."""
    from dirscape import interactive

    walk = _SlowWalk("big")
    monkeypatch.setattr(cli, "_rapidu_walk", lambda: walk)
    for name in ("big/inner", "small/inner"):
        (tmp_path / "lab" / name).mkdir(parents=True)
    monkeypatch.setattr(cli, "_nearest", lambda path, known: Root(path, fstype="gpfs"))
    made = []
    real = cli._Sizes

    class Spy(real):
        def __init__(self, roots, cache, **kwargs):
            real.__init__(self, roots, cache, **kwargs)
            made.append(self)

    monkeypatch.setattr(cli, "_Sizes", Spy)
    landed = []

    def select(paint, count, **kwargs):
        level = made[-1]
        if len(made) == 1:
            _until(lambda: getattr(level.counting()[0], "path", "").endswith("big"))
            return 1  # `small`: the row being counted sorts first
        if len(made) == 2:
            assert made[0].busy(), "the count of `big` goes on behind the view"
            return interactive.Key.BACK
        walk.release.set()
        _until(lambda: level.changed())
        level.refresh()
        landed.extend(r.path for r in level.roots if r.quota is not None)
        return interactive.Key.QUIT

    monkeypatch.setattr(cli.interactive, "select", select)
    assert cli._descend(str(tmp_path / "lab"), _plain_style(), 100) == interactive.Key.QUIT
    assert str(tmp_path / "lab" / "big") in landed
    assert [name for name, _ in walk.calls].count("big") <= 2, "a glance, then one count"


def test_coming_back_to_a_level_goes_straight_to_its_long_counts(tmp_path, monkeypatch):
    """Each return to `/project/rcc` glanced again, for fifteen seconds, at the
    very folders the first glance had found too big to finish."""
    walk = _SlowWalk("big")
    monkeypatch.setattr(cli, "_rapidu_walk", lambda: walk)
    glanced = {}
    first = cli._Sizes(_gpfs_level(tmp_path, ["big"]), {}, per_child_s=0.05, glanced=glanced)
    first.start()
    _until(lambda: first.counting()[0] is not None)
    first.stop()
    _settle(first)
    assert str(tmp_path / "big") in glanced
    again = cli._Sizes(_gpfs_level(tmp_path, ["big"]), {}, per_child_s=0.05, glanced=glanced)
    assert not again._todo and [r.path for r in again._full] == [str(tmp_path / "big")]


def test_the_size_index_is_off_under_no_state(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path))
    run = cli.Run()
    off = cli.build_parser().parse_args(["--no-state"])
    assert _REAL_SIZE_INDEX(run, off) is None
    on = _REAL_SIZE_INDEX(run, cli.build_parser().parse_args([]))
    assert on is not None and on.path.startswith(str(tmp_path))


def test_a_child_the_table_measured_is_the_tables_row(tmp_path):
    """Its figures are the quota's, not a walk's, and the same as the table shows."""
    (tmp_path / "lab").mkdir()
    measured = _measured(_root(str(tmp_path / "lab"), "project-lab"), inodes=7)
    kid = {
        "name": "lab",
        "path": str(tmp_path / "lab"),
        "readable": True,
        "writable": True,
        "enterable": True,
    }
    assert cli._listed_root(kid, None, None, {measured.path: measured}) is measured
    assert not cli._Sizes([measured], {})._todo, "nothing to walk"


def _hang_on(monkeypatch, names):
    """Make `_child_facts` block, as a wedged mount would, for these children."""
    import threading

    release = threading.Event()
    real = cli._child_facts

    def facts(child):
        if os.path.basename(child) in names:
            release.wait(30)
        return real(child)

    monkeypatch.setattr(cli, "_child_facts", facts)
    return release


def test_a_child_that_hangs_costs_its_deadline_and_not_the_browser(tmp_path, monkeypatch):
    """One hung mount under the listed directory froze the whole browser.

    Every probe ran on the UI thread with no deadline, so a `listdir` on a
    wedged mount never returned and no key could be pressed. Each child is
    asked on an abandoned thread now, and the one that did not answer says so.
    """
    import time

    root = tmp_path / "project"
    for name in ("alpha", "stuck", "zeta"):
        (root / name).mkdir(parents=True)
    monkeypatch.setattr(cli, "DEFAULT_DEADLINE_S", 0.2)
    release = _hang_on(monkeypatch, {"stuck"})
    started = time.time()
    try:
        kids, held, answered = cli._children(str(root))
    finally:
        release.set()

    assert time.time() - started < 5.0
    assert answered and held == 0
    notes = {kid["name"]: kid.get("unknown") for kid in kids}
    assert notes == {"alpha": None, "stuck": "did not answer", "zeta": None}
    roots = [cli._listed_root(kid, None, None, {}) for kid in kids]
    assert roots[1].reach == cli.Reach.UNKNOWN
    style = _plain_style()
    assert any("did not answer" in cli.plain(line) for line in cli._dir_notes(roots, style))


def test_children_past_the_allowance_are_not_checked_rather_than_waited_for(tmp_path, monkeypatch):
    root = tmp_path / "project"
    for name in ("a", "b", "c", "d"):
        (root / name).mkdir(parents=True)
    release = _hang_on(monkeypatch, {"b", "c", "d"})
    try:
        kids, _held, _answered = cli._children(str(root), probes_s=0.3)
    finally:
        release.set()

    notes = [kid.get("unknown") for kid in kids]
    assert notes[0] is None, "the first child answered at once"
    assert None not in notes[1:]
    assert notes[-1] == "not checked", "once the allowance is spent nothing more is asked"


def test_a_directory_that_hangs_is_reported_rather_than_waited_for(tmp_path, monkeypatch):
    import threading

    release = threading.Event()
    real = os.scandir
    # Scoped, because this is the real `os.scandir` for the whole process.
    with monkeypatch.context() as patch:
        patch.setattr(cli.os, "scandir", lambda path: release.wait(30) and real(path))
        try:
            kids, held, answered = cli._children(str(tmp_path), read_s=0.2)
        finally:
            release.set()

    assert (kids, held, answered) == ([], 0, False)
    text = cli.plain("\n".join(cli._empty_frame(str(tmp_path), _plain_style(), 100, answered)))
    assert "did not answer" in text and "nothing to open" not in text


def test_a_walk_the_reader_left_stops_at_the_next_directory(tmp_path):
    import threading

    for index in range(3):
        (tmp_path / ("d%d" % index)).mkdir()
    stop = threading.Event()
    stop.set()
    assert cli._walk(str(tmp_path), 1e18, 10**9, stop=stop) == (0, 0, False)


class _FakeSizes(object):
    def __init__(self, changes=(), running=False):
        self.changes = list(changes)
        self.running = running
        self.cursor = 7
        self.measured = []

    def busy(self):
        return self.running

    def changed(self):
        return self.changes.pop(0) if self.changes else False

    def measure(self, root=None):
        self.measured.append(self.cursor if root is None else root)
        return True

    def counting(self):
        return None, None


def test_a_landed_figure_repaints_and_m_measures_the_highlighted_row():
    from dirscape.interactive import Key

    fake = _FakeSizes(changes=[True])
    pressed = iter([Key.MEASURE, Key.DOWN])
    keys = cli._sizing_keys(fake, reader=lambda: next(pressed), waiting=lambda t: True)
    assert keys() == Key.REDRAW, "a figure landed"
    assert keys() == Key.REDRAW and fake.measured == [7], "m on the highlighted row"
    assert keys() == Key.DOWN, "every other key passes through"


def test_a_count_in_flight_repaints_its_status_line_every_second():
    """Owner's `/project/rcc` read "counting mehta5/: 2.4k entries so far" for a
    folder of 1.36 million entries: the line repainted only on a landing."""
    from dirscape.interactive import Key

    class Counting(_FakeSizes):
        def counting(self):
            return "mehta5", 2400

    now = [0.0]

    def waiting(seconds):
        now[0] += seconds
        return False

    keys = cli._sizing_keys(
        Counting(running=True), reader=lambda: Key.UP, waiting=waiting, clock=lambda: now[0]
    )
    assert keys() == Key.REDRAW and 1.0 <= now[0] < 1.3
    assert keys() == Key.REDRAW and 2.0 <= now[0] < 2.5, "and again a second later"
    idle = cli._sizing_keys(
        _FakeSizes(running=True),
        reader=lambda: Key.UP,
        waiting=lambda t: now.__setitem__(0, now[0] + t) or now[0] > 10,
        clock=lambda: now[0],
    )
    assert idle() == Key.UP, "nothing in flight: no repaint, only keys"


def test_while_walks_run_a_key_is_read_only_once_one_is_waiting():
    from dirscape.interactive import Key

    fake = _FakeSizes(running=True)
    waits = iter([False, False, True])
    keys = cli._sizing_keys(fake, reader=lambda: Key.UP, waiting=lambda t: next(waits))
    assert keys() == Key.UP


def _share_view(size=100, ascii_only=False, share=None, note="3.0K in 3 folders"):
    """The share column rendered on its own, over three measured children."""
    from dirscape.render import render_atlas, resolve_style

    style = resolve_style(color="never", ascii_only=ascii_only, stream=None)
    roots = []
    for name, used in (("big", 2048), ("half", 1024), ("none", 0)):
        root = _measured(_root("/lab/" + name, reach=Reach.LISTABLE), used=used, limit=None)
        roots.append(root)
    if share is None:
        share = {"/lab/big": 2 / 3.0, "/lab/half": 1 / 3.0, "/lab/none": 0.0}
    text = render_atlas(
        roots,
        style=style,
        size=size,
        frame=False,
        title="/lab",
        note=note,
        labels={r.path: r.path.rsplit("/", 1)[1] + "/" for r in roots},
        share=share,
    )
    return cli.plain(text).splitlines()


def test_an_opened_directory_shows_each_childs_share_as_a_bar():
    """The question one level down is where the space went, and the room the
    table used to spread into fifty-space gaps is where the answer goes."""
    lines = _share_view()
    heading = [line for line in lines if "path" in line and "share" in line][0]
    assert heading.split() == ["path", "used", "share"]
    rows = {line.split()[0]: line for line in lines if line.strip().startswith(("big/", "half/"))}
    assert re.search(r"67%  \u2587+", rows["big/"]) and re.search(r"33%  \u2587+", rows["half/"])
    assert rows["big/"].count("\u2587") > rows["half/"].count("\u2587") > 0
    none = [line for line in lines if line.strip().startswith("none/")][0]
    assert none.rstrip().endswith("0%") and "\u2587" not in none
    assert lines[0] == "/lab \u00b7 3.0K in 3 folders", "the title says what the whole is"


def test_the_longest_bar_is_the_biggest_folder_and_the_rest_are_in_proportion():
    """Drawn against the largest share, not 100%: a biggest folder holding a
    third of the directory left two thirds of every row empty."""
    lines = _share_view(size=100, share={"/lab/big": 0.3, "/lab/half": 0.15, "/lab/none": 0.0})
    rows = {
        line.split()[0]: line.rstrip()
        for line in lines
        if line.split()[:1] in (["big/"], ["half/"])
    }
    assert 100 - 4 <= len(rows["big/"]) <= 100 - 2, "the biggest spans the room, short of the edge"
    big, half = rows["big/"].count("\u2587"), rows["half/"].count("\u2587")
    assert abs(half - big / 2.0) <= 1, "half the share, half the bar"
    assert re.search(r"30%  \u2587+$", rows["big/"]), "and the percent is still the true share"


def test_the_share_column_keeps_the_gutters_their_own_width():
    """Spreading three columns across the window is what opened fifty-space gaps."""
    heading = [line for line in _share_view(size=140) if "path" in line and "share" in line][0]
    gaps = [
        len(gap)
        for gap in heading.strip()
        .replace("path", "|")
        .replace("used", "|")
        .replace("share", "|")
        .split("|")
        if gap
    ]
    assert max(gaps) < 30, heading
    rule = [line for line in _share_view(size=140) if set(line.strip()) == {"\u2500"}][0]
    assert len(rule.strip()) == 140, "the rule still spans the panel"


def test_the_share_bar_has_an_ascii_twin_and_marks_for_what_is_not_known():
    lines = _share_view(
        ascii_only=True, share={"/lab/big": 0.5, "/lab/half": "...", "/lab/none": "?"}
    )
    rows = {
        line.split()[0]: line.rstrip()
        for line in lines
        if line.split()[:1] in (["big/"], ["half/"], ["none/"])
    }
    assert re.search(r"50%  #+$", rows["big/"]), "the percent, then the bar"
    assert rows["half/"].endswith("...") and rows["none/"].endswith("?")


def test_a_window_too_narrow_for_a_bar_leaves_the_share_out():
    heading = [line for line in _share_view(size=30) if "path" in line][0]
    assert "share" not in heading


def test_the_start_screen_has_no_share_column_and_still_spreads():
    """The share is the opened directory's; the start screen is unchanged."""
    from dirscape.render import render_atlas, resolve_style

    style = resolve_style(color="never", ascii_only=False, stream=None)
    roots = [_measured(_root("/lab/a")), _measured(_root("/lab/b"), used=5000, limit=9000)]
    text = cli.plain(render_atlas(roots, style=style, size=140, frame=False))
    heading = [line for line in text.splitlines() if "path" in line][0]
    assert "share" not in heading
    assert len(heading.rstrip()) > 120, "the table still spans the window"


@pytest.mark.parametrize(
    "value, text",
    [(0.0, "0%"), (0.004, "<1%"), (0.02, "2%"), (1 / 3.0, "33%"), (0.999, "100%"), (1.0, "100%")],
)
def test_a_share_is_a_whole_percent_and_never_rounds_a_part_to_nothing(value, text):
    from dirscape.render import atlas

    assert atlas._percent(value) == text


def test_shares_wait_until_the_whole_is_known(tmp_path):
    """A share of what had been counted so far put a 40G folder at 60% of a
    directory holding 10T, so no share is shown until every folder is done."""
    style = _plain_style()
    done, coming, lost = (
        _measured(_root("/lab/done", reach=Reach.LISTABLE), used=300, limit=None),
        _root("/lab/coming", reach=Reach.LISTABLE),
        _root("/lab/lost", reach=Reach.CLOSED),
    )
    coming.policy["measuring"] = True
    shares, note = cli._shares([done, coming, lost], style)
    assert cli.plain(shares["/lab/done"]) == cli.plain(shares["/lab/coming"]) == "\u2026"
    assert shares["/lab/lost"] == "?"
    assert note == "counting, 2 of 3 folders done"
    coming.policy.pop("measuring")
    _measured(coming, used=100, limit=None)
    shares, note = cli._shares([done, coming, lost], style)
    assert shares["/lab/done"] == 0.75 and shares["/lab/lost"] == "?"
    assert note == "400B in 2 of 3 folders"
    assert cli._shares([done], style)[1] == "300B in 1 folder"


def _sized(name, used=None, **policy):
    root = _root("/lab/" + name, reach=Reach.LISTABLE)
    if used is not None:
        _measured(root, used=used, limit=None)
    root.policy.update(policy)
    return root


def test_an_opened_directory_is_largest_first_with_what_has_no_figure_after():
    """Owner, on name order: "the share sort of lost its meaning". What is
    still being counted leads, since a folder the glance could not finish is
    usually one of the largest; then the counted ones, largest first."""
    rows = [
        _sized("a-small", used=10),
        _sized("b-coming", measuring=True),
        _sized("c-huge", used=5000),
        _sized("d-big", used=900),
        _sized("e-shut"),
        _sized("f-tie", used=10),
    ]
    order = cli._Order(rows)
    assert [r.path.rsplit("/", 1)[1] for r in order.rows] == [
        "b-coming",
        "c-huge",
        "d-big",
        "a-small",
        "f-tie",
        "e-shut",
    ]
    order.toggle()
    order.arrange()
    assert [r.path for r in order.rows] == [r.path for r in rows], "`s`: back to names"


def test_the_highlight_follows_its_folder_when_the_rows_re_sort():
    grows, stays = _sized("a-grows", used=1), _sized("b-stays", used=5)
    order = cli._Order([grows, stays])
    assert order.rows == [stays, grows]
    assert order.follow(1) == 0, "before the reader moves, the top row stays highlighted"
    order.moved = True
    _measured(grows, used=50, limit=None)
    assert order.follow(1) == 0 and order.rows[0] is grows, "it moved up, and so did the band"


def test_s_flips_the_order_and_repaints():
    from dirscape.interactive import Key

    order = cli._Order([_sized("a", used=1), _sized("b", used=2)])
    keys = cli._sizing_keys(_FakeSizes(), order, reader=lambda: Key.SORT, waiting=lambda t: True)
    assert keys() == Key.REDRAW and order.by_size is False


def _banded_frame(tmp_path, color, share_of_first):
    from dirscape.render import resolve_style

    style = resolve_style(color=color, ascii_only=False, stream=None)
    big = _measured(_root(str(tmp_path / "big"), reach=Reach.LISTABLE), used=900, limit=None)
    small = _measured(_root(str(tmp_path / "small"), reach=Reach.LISTABLE), used=1, limit=None)
    rows = [big, small] if share_of_first else [small, big]
    lines, _top, _room = cli._dir_frame(str(tmp_path), rows, 0, cli.Run(), style, 100, height=30)
    return [line for line in lines if "\033[7m" in line][0]


def test_the_highlight_stops_before_the_share_bar_instead_of_punching_a_hole_in_it(tmp_path):
    """Owner: "the highlightor has the problem covering the bar plot", then "very
    ugly. the main ui doesn't have the issue". A full block in inverse video is
    a block of the background colour, so the band had a bar-shaped hole in it."""
    line = _banded_frame(tmp_path, "always", share_of_first=True)
    band, _, after = line.partition("\033[7m")[2].partition("\033[0m")
    assert "\u2587" not in band and "100%" in band, "the percent is inside the band"
    assert "\u2587" in cli.plain(after), "the bar is drawn after it"
    assert "\033[" in after.split("\u2587")[0], "in its own colour, not inverted"


def test_the_band_is_one_width_whatever_the_share(tmp_path):
    def reach(line):
        band = line.partition("\033[7m")[2].partition("\033[0m")[0]
        return len(cli.plain(line.partition("\033[7m")[0])) + len(band)

    with_bar = _banded_frame(tmp_path / "a", "never", share_of_first=True)
    without = _banded_frame(tmp_path / "b", "never", share_of_first=False)
    assert reach(with_bar) == reach(without)


class _FakeWalk(object):
    """rapidu's `walk`, recorded: what it was asked, and a canned answer."""

    def __init__(self, answer=None, hang=False, error=None):
        self.answer = answer or {}
        self.hang = hang
        self.error = error
        self.calls = []

    def __call__(self, path, **kwargs):
        self.calls.append((path, kwargs))
        if self.error is not None:
            raise self.error
        if self.hang:
            kwargs["stop"].wait(10)
        result = argparse.Namespace(size=0, files=0, symlinks=0, specials=0, partial=False)
        result.__dict__.update(self.answer)
        if self.hang:
            result.partial = kwargs["stop"].is_set()
        return result


def test_rapidu_sizes_a_child_when_it_is_installed(tmp_path, monkeypatch):
    """Owner: "for the large dirs, it's too slow". rapidu keeps sixteen stats in
    flight where `_walk` waits on one, which is what a cold tree costs."""
    import threading

    fake = _FakeWalk({"size": 4096, "files": 10, "symlinks": 2, "specials": 1})
    monkeypatch.setattr(cli, "_rapidu_walk", lambda: fake)
    used, files, done = cli._size_of(str(tmp_path), 1e18, 10, threading.Event())
    # rapidu's `files` already holds its symlinks and specials. Adding them in
    # again had a tree of 148,402 files read 148,727.
    assert (used, files, done) == (4096, 10, True), "every entry that is not a directory, once"
    ((path, kwargs),) = fake.calls
    assert path == str(tmp_path) and kwargs["one_file_system"] is True
    assert isinstance(kwargs["stop"], threading.Event)


def test_rapidu_is_stopped_at_the_deadline_and_the_figure_is_left_unknown(tmp_path, monkeypatch):
    import threading
    import time

    monkeypatch.setattr(cli, "_rapidu_walk", lambda: _FakeWalk(hang=True))
    started = time.time()
    _used, _files, done = cli._size_of(str(tmp_path), time.time() + 0.2, 10, threading.Event())
    assert not done and time.time() - started < 5.0


def test_leaving_the_view_stops_a_rapidu_walk_in_flight(tmp_path, monkeypatch):
    import threading
    import time

    fake = _FakeWalk(hang=True)
    monkeypatch.setattr(cli, "_rapidu_walk", lambda: fake)
    (tmp_path / "big").mkdir()
    roots = [cli._listed_root(kid, None, None, {}) for kid in cli._children(str(tmp_path))[0]]
    sizes = cli._Sizes(roots, {}, glance_s=0.0)
    sizes.start()
    stop = time.time() + 5
    while not fake.calls and time.time() < stop:
        time.sleep(0.01)
    sizes.stop()
    _settle(sizes, limit=5.0)


def test_a_rapidu_without_these_arguments_falls_back_to_the_tables_walk(tmp_path, monkeypatch):
    import threading

    (tmp_path / "f").write_bytes(b"x")
    monkeypatch.setattr(cli, "_rapidu_walk", lambda: _FakeWalk(error=TypeError("old rapidu")))
    _used, files, done = cli._size_of(str(tmp_path), 1e18, 10, threading.Event())
    assert (files, done) == (1, True)


def test_without_rapidu_the_tables_own_walk_answers(tmp_path):
    """The suite's default, set in conftest, so a result never depends on
    whether rapidu happens to be installed where the tests run."""
    import threading

    (tmp_path / "f").write_bytes(b"x")
    assert cli._rapidu_walk() is None
    assert cli._size_of(str(tmp_path), 1e18, 10, threading.Event())[1:] == (1, True)


def test_the_tree_sums_walked_figures_and_never_calls_them_unknown(tmp_path):
    """Two defects in one node of `dirscape tree`, both from the same cause.

    Walked figures were keyed on the directory's own path as a fileset name,
    so `/tmp` and `/scratch/local/jdoe42`, two mounts of one `/dev/sda1`,
    became two nodes with conflicting names and the view fell back to `?` for
    the device. A walk measures a DIRECTORY, not a quota scope, so it names no
    fileset now, and an absent fileset is labelled for what it is rather than
    printed as the mark this package reserves for "nobody could measure it".

    And the figure was the FIRST member's, not the group's: `0B used` for a
    device holding 1.2G. Every path in a fileset shares one quota so the first
    will do; walked figures are per directory and have to be added.
    """
    from dirscape.render import render_tree, resolve_style

    style = resolve_style(color="never", ascii_only=False, stream=None)
    roots = []
    for name, payload in (("empty", 0), ("full", 3)):
        directory = tmp_path / name
        directory.mkdir()
        for index in range(payload):
            (directory / ("f%d" % index)).write_bytes(b"x" * 4096)
        root = _root(str(directory), device="/dev/sda1")
        root.role = "local"
        root.fstype = "xfs"
        root.quota = None
        roots.append(root)

    for root in roots:
        # One run each, so `_visible`'s folding of two sibling directories
        # cannot leave one of them unwalked. What is under test here is the
        # tree's aggregation, not the measuring pass's scope.
        run = cli.Run()
        run.roots = [root]
        cli._measure(run)
        assert root.quota is not None, "the fixture needs both roots measured"

    text = render_tree(roots, style=style)
    assert "?" not in text, "an absent fileset is not an unknown one: %r" % (text,)
    assert "no quota here" in text, "it says why there is no fileset"
    # 3 files in the second directory only, and the device node must report
    # the pair rather than whichever came first. What 3 small files occupy is
    # the filesystem's business (GPFS can charge a fresh 4k file 0 blocks and
    # a flushed one 12k), so the expected figure is the two walks' own sum.
    from dirscape.render.fields import human_bytes

    walked = [row.used for root in roots for row in root.quota.rows]
    assert walked[0] == 0 and walked[1] > 0, walked
    assert "%s used" % (human_bytes(sum(walked)),) in text, text


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
    # The last line is the `--json` pointer, which is a command and not a
    # field, so it is measured for LENGTH above and excluded here.
    body = [ln for ln in lines[1:-1] if ln.strip()]
    for line in body:
        assert len(line.split()) <= 9, "%r is a sentence, not a field" % (line,)
    assert "the figures above" not in text, "prose must not point at other lines"
    assert "counted by the filesystem itself" not in text, "the mechanism paragraph is gone"
    # The quota's source survives, one flag away: it names a fileset and a
    # device, which is provenance rather than a fact about the reader's
    # storage. It is also in `--json` unconditionally.
    verbose, _ = cli._why(
        run, "/project/lab", resolve_style(color="never", stream=None), verbose=True
    )
    assert "  source    " in verbose, "the quota source survives as a -v field"


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
    # Reworded again when the owner asked whether a reader could understand
    # these lines. The count and the consequence are still the two things this
    # test is about; the two full paths and the `(dirscape tree)` pointer that
    # used to sit between them are behind `-v`.
    assert "note 6 folders here are really stored in" in flat, flat
    assert "count against its space" in flat, "the consequence is the point, not the count"
    verbose, _ = cli._why(run, "/home/me", resolve_style(color="never", stream=None), verbose=True)
    assert "symlinks" in verbose, "-v still lists which paths they are"
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
    # `found by` is provenance, so it moved behind `-v` with the rest of it:
    # "do you think these things users can understand what they are?"
    text, _ = cli._why(run, "/project/hpc", resolve_style(color="never", stream=None), verbose=True)
    flat = " ".join(text.split())
    for token in discover.SOURCE_LABELS:
        assert token not in text, "%r is a wire token and reached the screen" % (token,)
    assert "found by " in flat, "the sources are a field now, not a sentence"
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
    text, _ = cli._why(run, "/project/hpc", resolve_style(color="never", stream=None), verbose=True)
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

    # All three figure fields, because the split gave the mark three places to
    # soften into a blank instead of one.
    for field in ("used", "quota", "free"):
        assert "%-9s ?" % (field,) in text, "an unmeasured %s is the unknown mark: %r" % (
            field,
            text,
        )
    verbose, _ = cli._why(
        run, "/project/lab", resolve_style(color="never", stream=None), verbose=True
    )
    assert "source none measured" in " ".join(verbose.split())
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

    **The measurement changed when the inner view did, and the reason is worth
    recording.** It used to count cursor-up sequences and require exactly
    three, on the grounds that the detail view was static so Down should
    repaint nothing. Opening a row now lists the directory's children, where
    Down MOVES and repainting is the point, so that count is no longer a
    defect signal.

    What still is, and what actually caused the stacking, is a block occupying
    more rows than the repaint arithmetic counts: `select` moves the cursor up
    by the number of lines it wrote, so one line over and the erase starts in
    the wrong place. So the assertion is now on every repaint in the session:
    each must move up by fewer lines than the window has rows. Measured at 40
    rows, which is where the original report came from.

    Three raw sessions are still required, because without them the test would
    pass by having its keystrokes dropped, which is what an output-driven
    harness does the moment the output it waits on changes shape.

    **With motion off**, because motion changes the shape this counts on: the
    startup board holds the terminal in a raw session of its own before the
    table's, and keys sent then are discarded when the table's begins. The
    same property with motion on is `test_a_real_pty_animates_and_every_frame_fits`.
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
        # A person at a terminal, which is what this measures: an agent
        # harness running the suite exports variables that turn the browse
        # off on purpose (`cli.agent_driven`), and the test would then skip.
        for name in cli.AGENT_VARIABLES:
            os.environ.pop(name, None)
        os.environ["PYTHONPATH"] = os.path.join(root, "src")
        os.execv(sys.executable, [sys.executable, "-m", "dirscape", "--no-state", "--no-motion"])

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
        "expected three raw sessions (table, listing, table): the keystrokes did not land"
    )
    ups = [int(n) for n in re.findall(r"\033\[(\d+)A", text)]
    assert ups, "nothing was ever repainted, so nothing was measured"
    assert max(ups) < 40, (
        "a repaint moved up %d lines in a 40 row window, so the block was taller than "
        "the terminal and the erase started in the wrong place: %r" % (max(ups), ups)
    )


def test_a_figure_is_two_tiers_and_an_absence_is_one():
    """The text hierarchy, asserted rather than left to each caller.

    Every figure cell in every view was `muted`, the same tier as the labels
    beside it, so ten rows of numbers had no hierarchy for the eye to use and
    the owner read the result as drab. Current TUI practice is to design the
    text tiers first and spend colour as a resource: content in the primary
    tier, context below it, chrome below that.

    Three properties, and the third is what keeps colour honest here:

    * a figure's MAGNITUDE and its UNIT are different tiers, which gives a
      numeric column texture at no semantic cost;
    * an absence (`none`) is chrome, not content, so it is not the brightest
      thing in a column of numbers;
    * and `plain()` is identical either way, because nothing in this package
      may depend on colour to be read.
    """
    from dirscape.render import fields
    from dirscape.render.style import Style, plain

    style = Style(color=True, depth=8)

    painted = fields.figure("868M", style)
    assert plain(painted) == "868M"
    assert painted.count("\033[") >= 2, "two tiers means two escapes: %r" % (painted,)
    assert style.info("868") in painted, "the magnitude is the content tier"
    assert style.dim("M") in painted, "the unit is one tier down"

    absent = fields.figure("none", style)
    assert plain(absent) == "none"
    assert absent == style.dim("none"), "an absence is chrome, not content"

    off = Style(color=False)
    assert fields.figure("868M", off) == "868M"
    assert fields.figure("none", off) == "none"


def test_every_colour_role_survives_all_three_depths():
    """Usable at 16 colours, beautiful at true colour.

    The three depths are independent by design, so a role added for its
    truecolor value and never checked at 4 bits is a role that vanishes on a
    `TERM=linux` console. Asserted for every role in the palette rather than
    the handful a view happens to use.
    """
    from dirscape.render.style import _PALETTE, Style

    for role in _PALETTE:
        for depth in (4, 8, 24):
            painted = Style(color=True, depth=depth).paint(role, "x")
            assert painted.startswith("\033["), "%s at %d bits: %r" % (role, depth, painted)
            assert painted.endswith("x\033[0m"), "%s at %d bits: %r" % (role, depth, painted)


# --------------------------------------------------------------------------
# Snapshots: the recovery answer, in the three views that carry it
# --------------------------------------------------------------------------


def _with_copies(root, names=("daily-2026-09-22.05h30", "daily-2026-09-21.05h30")):
    from dirscape.discover.recover import parse_snapshot_time
    from dirscape.model import SnapshotCopy

    root.snapshots = [
        SnapshotCopy(name, "/fs/.snapshots/%s%s" % (name, root.path), parse_snapshot_time(name))
        for name in names
    ]
    root.recoverable = confirmed("copies found", source="snapshots")
    return root


def test_why_names_the_literal_path_to_the_newest_copy():
    """A command a reader can paste, not a pointer to another command.

    This is the line the whole feature exists for, and an instruction to "see
    the snapshot documentation" would be worth nothing at the moment somebody
    needs it.
    """
    run = cli.Run()
    root = _with_copies(_measured(_root("/project/lab", "project-lab"), inodes=5))
    root.writable = confirmed()
    run.roots = [root]

    from dirscape.render import resolve_style

    text, _ = cli._why(run, "/project/lab", resolve_style(color="never", stream=None))

    assert "snapshots" in text
    assert "2 copies kept" in text
    assert "/fs/.snapshots/daily-2026-09-22.05h30/project/lab" in text
    assert "cp -a" in text


def test_why_says_gone_rather_than_going_quiet_when_nothing_is_kept():
    """The scratch case. A durable no is louder than an unknown, not quieter."""
    run = cli.Run()
    root = _measured(_root("/scratch/me", "scratch"))
    root.writable = confirmed()
    root.recoverable = refuted(
        VerdictCategory.NOT_PRESENT,
        "this filesystem exposes a snapshot directory and is keeping nothing "
        "in it, so a deleted file here is gone",
        source="snapshots",
    )
    run.roots = [root]

    from dirscape.render import resolve_style

    text, _ = cli._why(run, "/scratch/me", resolve_style(color="never", stream=None))

    assert "gone" in text
    assert "cp -a" not in text, "there is nothing to copy and no command to offer"


def test_why_omits_the_snapshot_line_when_snapshots_were_never_checked():
    """Same rule as every other probe on this screen: silence, not `?`.

    A root replayed from a state file written before this field existed has
    not been asked, and `snapshots ? (not probed)` is a line of chrome that
    teaches a reader the marks on this view mean nothing.
    """
    run = cli.Run()
    root = _measured(_root("/project/lab", "project-lab"))
    root.writable = confirmed()
    run.roots = [root]

    from dirscape.render import resolve_style

    text, _ = cli._why(run, "/project/lab", resolve_style(color="never", stream=None))

    assert "snapshots" not in text
    assert "not probed" not in text


def test_the_matrix_carries_a_snapshot_column_beside_the_policy_one():
    """Measured recovery and published policy are different columns.

    On this cluster every home and project row reads `?` for `backup` and a
    measured `y` for `snapshot`, which is exactly right: nobody wrote a
    backup policy into a config file, and eleven restorable copies exist
    regardless. Folding the measurement into the policy column would have
    made the tool assert a site policy it had never read.
    """
    from dirscape.render.matrix import COLUMNS, FROM_POLICY, PROBED, render

    assert "snapshot" in COLUMNS
    assert "snapshot" in PROBED
    assert "snapshot" not in FROM_POLICY

    kept = _with_copies(_measured(_root("/project/lab", "project-lab", writable=True)))
    lost = _measured(_root("/scratch/me", "scratch", writable=True))
    lost.recoverable = refuted(VerdictCategory.NOT_PRESENT, "nothing kept", source="snapshots")

    style = Style(color=False)
    text = render([kept, lost], style=style, size=140)
    lines = {line.split()[0]: line for line in text.splitlines() if line.startswith("/")}

    header = [line for line in text.splitlines() if "snapshot" in line][0]
    column = header.index("snapshot")
    assert lines["/project/lab"][column : column + 8].strip() == style.g.ok
    assert lines["/scratch/me"][column : column + 8].strip() == style.g.bad


def test_recover_is_not_spelled_snapshot():
    """`dirscape snapshot` already records a baseline, and a collision here
    would let `dirscape snapshot ~/thesis.tex` overwrite one while a user
    was trying to find a deleted file."""
    parser = cli.build_parser()
    args = parser.parse_args(["recover", "/home/me/thesis.tex"])
    assert args.command == "recover"
    assert args.path == "/home/me/thesis.tex"

    baseline = parser.parse_args(["snapshot"])
    assert baseline.command == "snapshot"
    assert not hasattr(baseline, "path")


# --------------------------------------------------------------------------
# The two spellings of one fileset
# --------------------------------------------------------------------------


def _wrapper_snapshot(fileset, mount, used=100, hard=200):
    rows = [
        QuotaRow(fileset, "blocks", "group", used, hard=hard, mount=mount),
        QuotaRow(fileset, "files", "group", 5, hard=10, mount=mount),
    ]
    return QuotaSnapshot("site quota wrapper", rows)


def test_a_fileset_spelled_two_ways_is_still_matched():
    """`mmlsattr` says `collie3-hpc-staff`; the site wrapper says `hpc-staff`.

    Two sources, one allocation, and exact matching found nothing: the row
    rendered `? ? ?` while the wrapper had already reported 149 GiB against a
    1 TiB limit, and the measuring walk then burned its whole 1.5s deadline
    failing to add up the tree by hand. Joining the row's mount to its scope
    name identifies the directory without anyone configuring a prefix.
    """
    root = _root("/collie3/hpc-staff", "collie3-hpc-staff", device="collie3_cap")
    snap = _wrapper_snapshot("hpc-staff", "/collie3")

    rows = cli._rows_governing(snap, root)

    assert [row.kind for row in rows] == ["blocks", "files"]


def test_a_root_with_no_fileset_at_all_is_matched_the_same_way():
    """`mmlsattr` is a GPFS tool, so every other filesystem arrives blank.

    On a login node that is the whole cost-effective storage tier, and it was
    six of the eight `?` rows on screen. Owner: "why so many `?`? i told you
    not to have them. why can't you retrieve the numbers?"
    """
    root = _root("/cfs3/kestrel-lab", fileset="", device="cfs3")
    snap = _wrapper_snapshot("kestrel-lab", "/cfs3", used=170556243700613)

    rows = cli._rows_governing(snap, root)

    assert [row.used for row in rows] == [170556243700613, 5]


def test_the_scope_name_cannot_claim_a_sibling_directory():
    """The exact-path guard, which is what keeps the clause narrow.

    Without it a scope name is a prefix match, and a prefix match is how an
    integration run once reported five different PIs' directories as each
    holding 11T.
    """
    sibling = _root("/collie3/someone-else", "collie3-someone-else", device="collie3_cap")
    snap = _wrapper_snapshot("hpc-staff", "/collie3")

    assert cli._rows_governing(snap, sibling) == []


def test_a_row_about_a_subdirectory_does_not_also_claim_its_mount(tmp_path):
    """`/cfs3` was printing `155T of 165T`, which belongs to another group.

    The row says `mount=/cfs3 scope=kestrel-lab`, so it is about
    `/cfs3/kestrel-lab`. Handing it to `/cfs3` as well is the same
    misattribution one level up. Settled by a `stat`, because the strings
    cannot: the wrapper also prints `mount=/scratch/meadow3
    scope=scratch/meadow3`, where joining the two names nothing and the row
    really is about the mount.
    """
    tier = tmp_path / "cfs3"
    (tier / "kestrel-lab").mkdir(parents=True)

    parent = _root(str(tier), fileset="", device="cfs3")
    snap = _wrapper_snapshot("kestrel-lab", str(tier))
    assert cli._rows_governing(snap, parent) == []

    # The other shape, where the scope is not a directory: the row IS about
    # the mount and must still be handed to it.
    scratch = _root(str(tmp_path / "scratch"), fileset="", device="perf")
    (tmp_path / "scratch").mkdir()
    mount_row = _wrapper_snapshot("scratch/meadow3", str(tmp_path / "scratch"), used=22)
    assert [row.used for row in cli._rows_governing(mount_row, scratch)] == [22, 5]


def test_an_exact_fileset_match_still_wins_outright():
    root = _root("/project/hpc", "project-hpc")
    exact = _wrapper_snapshot("project-hpc", "/project", used=7, hard=8)

    rows = cli._rows_governing(exact, root)

    assert [row.used for row in rows] == [7, 5]


# --------------------------------------------------------------------------
# Trimming: the login node's table was 22 rows, most of them unusable
# --------------------------------------------------------------------------


def _readable(path, device="cfs3"):
    root = _root(path, device=device)
    root.writable = refuted(VerdictCategory.ACCESS_DENIED, "no write bit")
    return root


def test_a_read_only_parent_folds_behind_its_writable_children():
    """`/cfs3` printed `155T of 165T`, which is every group's usage, not yours.

    Owner, looking at it beside the two directories they can actually write
    to: "/cfs3 i only have 2 dirs that i can access and both of them are
    listed but why /cfs3 should be shown here?"
    """
    parent = _readable("/cfs3")
    mine = _root("/cfs3/hpc-staff", device="cfs3", writable=True)
    theirs = _root("/cfs3/kestrel-lab", device="cfs3", writable=True)

    kept, folded = cli._fold_covered_parents([parent, mine, theirs])

    assert [r.path for r in kept] == ["/cfs3/hpc-staff", "/cfs3/kestrel-lab"]
    assert folded == 1


def test_a_read_only_parent_with_no_writable_child_stays():
    """`/project2/reference` is the answer for a shared collection.

    Nothing inside it is writable, so folding it would remove the only row
    that names 23T of reference data.
    """
    parent = _readable("/project2/reference", device="meadow2_cap")
    child = _readable("/project2/reference/pdb", device="meadow2_cap")

    kept, folded = cli._fold_covered_parents([parent, child])

    assert [r.path for r in kept] == ["/project2/reference", "/project2/reference/pdb"]
    assert folded == 0


def test_folding_never_crosses_a_device():
    """`/scratch` is a plain directory holding three clusters' filesystems.

    Folding on the path alone would hide two of them behind the first, which
    is the trap `_collapse_families` already records.
    """
    parent = _readable("/scratch", device="plain")
    child = _root("/scratch/meadow3/jdoe42", device="meadow3_perf", writable=True)

    kept, folded = cli._fold_covered_parents([parent, child])

    assert len(kept) == 2
    assert folded == 0


def test_a_parent_carrying_its_own_news_is_never_folded():
    parent = _readable("/cfs3")
    parent.labels = ["new"]
    mine = _root("/cfs3/hpc-staff", device="cfs3", writable=True)

    kept, _folded = cli._fold_covered_parents([parent, mine])

    assert "/cfs3" in [r.path for r in kept]


def test_the_machine_root_is_not_an_allocation():
    """`/` came out `other  read only  20G  312G  311k` on a login node.

    It is the OS disk. Anywhere under it a user can write is its own mount
    with its own row, so nothing is lost by ranking it down.
    """
    from dirscape.discover.candidates import RANK_PRIMARY, RANK_SECONDARY, _rank_of_mount
    from dirscape.discover.mounts import read_mount_table
    from dirscape.sitecfg import Site

    mounts = read_mount_table(text="rootdev / xfs rw 0 0\ncap /project gpfs rw 0 0\n")
    slash = [m for m in mounts.non_pseudo() if m.mountpoint == "/"][0]
    rank, reason = _rank_of_mount(slash, mounts, Site())
    assert rank == RANK_SECONDARY
    assert "root of the machine" in reason

    # ...unless it is the only storage there is. An empty table is worse than
    # an imprecise row.
    alone = read_mount_table(text="rootdev / xfs rw 0 0\n")
    only = [m for m in alone.non_pseudo() if m.mountpoint == "/"][0]
    rank, _reason = _rank_of_mount(only, alone, Site())
    assert rank == RANK_PRIMARY


def test_the_heading_row_has_air_above_and_below_it():
    """Owner: "the vertical spacing is too narrow, especially the column row
    and the first row." Five lines of chrome with no gap anywhere in them."""
    from dirscape.render.atlas import render

    root = _measured(_root("/project/lab", "project-lab", writable=True), inodes=5)
    text = render([root], style=Style(color=False), size=120, frame=False)
    lines = [line.rstrip() for line in text.splitlines()]

    heading = next(i for i, line in enumerate(lines) if "kind" in line and "path" in line)
    first = next(i for i, line in enumerate(lines) if "/project/lab" in line)
    assert lines[heading - 1] == "", "the heading sits directly on the rule"
    assert lines[heading + 1] == "", "the heading sits directly on the first row"
    assert first == heading + 2


# --------------------------------------------------------------------------
# The listing window: the band travels, the list holds
# --------------------------------------------------------------------------


def _walk(count, room, start=0):
    """Where the window sits as the cursor walks the whole list."""
    top = cli._window_top(start, count, room, None)
    seen = []
    for cursor in range(count):
        top = cli._window_top(cursor, count, room, top)
        seen.append((top, cursor - top))
    return seen


def test_the_band_reaches_the_bottom_row():
    """The bug, in the owner's words: "the highlightor isn't at the bottom
    when scrolling down, it's somewhere in the middle."

    The counter read `64 of 84, 52 above, 10 below`: ten rows the reader
    could see, below a band that would not move onto them. The window was
    recomputed as `cursor - room // 2` on every repaint, which pins the
    highlight to the middle for ever.
    """
    seen = _walk(84, 22)

    assert seen[0] == (0, 0), "the first row is the top row"
    assert seen[21] == (0, 21), "the band walks down to the last visible row"
    assert seen[22] == (1, 21), "only then does the list scroll under it"
    assert seen[83] == (62, 21), "the last row is reachable and is the bottom row"
    assert all(offset == 21 for _top, offset in seen[21:]), (
        "the band stays pinned to the bottom edge while the list moves"
    )


def test_the_band_walks_back_up_to_the_top_row():
    top = 62
    for cursor in range(83, -1, -1):
        top = cli._window_top(cursor, 84, 22, top)
    assert top == 0, "scrolling back up reaches the first row"


def test_a_list_that_fits_never_scrolls():
    assert all(top == 0 for top, _offset in _walk(8, 22))


def test_the_first_paint_centres_because_there_is_nothing_to_remember():
    """`None` means "no remembered position", which is a first paint or a
    caller with no state. Centring is the right answer there: it shows the
    rows on both sides of wherever the reader is resuming.
    """
    assert cli._window_top(50, 84, 22, None) == 39
    assert cli._window_top(0, 84, 22, None) == 0
    assert cli._window_top(83, 84, 22, None) == 62


def test_a_remembered_position_out_of_range_is_clamped():
    """The window shrinks when the terminal does, and the old top survives."""
    assert cli._window_top(3, 20, 10, 999) == 3
    assert cli._window_top(3, 20, 10, -5) == 0


# --------------------------------------------------------------------------
# One path is a suffix of another, and the band went to the wrong line
# --------------------------------------------------------------------------


def test_every_row_can_be_highlighted_when_one_path_suffixes_another():
    """Owner: "when the highlightor is on the gpfs row and when i press the
    down arrow, it will skip /software and jump directly to /cfs."

    Nothing was being skipped. `/software` is a substring of
    `/gpfs/meadow2/perf2/software`, which renders one row above it, so a
    substring search for the cursor's path found the wrong line first and
    repainted the band where it already was. One row of the table was
    unreachable with the arrow keys.
    """
    from dirscape.render.style import plain

    run = cli.Run()
    shadowed = _measured(_root("/software", "software", writable=True))
    shadowing = _measured(_root("/gpfs/meadow2/perf2/software", "sw2", writable=True))
    other = _measured(_root("/cfs/hpc-staff", "cfs", writable=True))
    roots = [shadowing, shadowed, other]
    run.roots = roots

    style = Style(color=False)
    seen = []
    for cursor in range(len(roots)):
        block = cli._table_frame(roots, cursor, run=run, style=style, width=110)
        banded = [i for i, line in enumerate(block) if "\033[7m" in line]
        assert len(banded) == 1, "exactly one line is highlighted"
        seen.append(plain(block[banded[0]]))

    assert cli._whole_path_at(seen[0], "/gpfs/meadow2/perf2/software")
    assert cli._whole_path_at(seen[1], "/software")
    assert cli._whole_path_at(seen[2], "/cfs/hpc-staff")
    assert len(set(seen)) == 3, "three cursor positions must land on three rows"


def test_a_shorter_path_never_matches_a_longer_one():
    assert cli._whole_path_at("   /software   read + write", "/software")
    assert not cli._whole_path_at("   /gpfs/meadow2/perf2/software   read", "/software")
    assert not cli._whole_path_at("   /cfs3/kestrel-lab   read", "/cfs")
    assert not cli._whole_path_at("   /cfs3   read only", "/cfs3/kestrel-lab")
    assert not cli._whole_path_at("   /software   read", "")


# --------------------------------------------------------------------------
# Found on two other clusters: Lustre, NetApp and a Cray read-only root
# --------------------------------------------------------------------------


def _lustre_rows(mount, path_fileset="lanternlab-exampleu"):
    """The three scopes `lfs quota` returns for one project directory.

    Measured on an ACME login node. Only the project scope carries a limit;
    the other two are accounting totals for this reader and this group across
    the whole filesystem.
    """
    return QuotaSnapshot(
        "lfs quota",
        [
            QuotaRow(path_fileset, "blocks", "user", 4480696360960, mount=mount),
            QuotaRow(path_fileset, "files", "user", 216137, mount=mount),
            QuotaRow(path_fileset, "blocks", "group", 2723414250631168, mount=mount),
            QuotaRow(path_fileset, "files", "group", 429696207, mount=mount),
            QuotaRow(
                path_fileset, "blocks", "project", 32481673543680, hard=60473139527680, mount=mount
            ),
            QuotaRow(path_fileset, "files", "project", 8214545, mount=mount),
        ],
    )


def test_an_exact_mount_beats_a_fileset_name_that_disagrees():
    """Lustre names an allocation with a NUMBER and its rows with a name.

        root.fileset = "13579"                  (lfs project -d)
        row.fileset  = "lanternlab-exampleu"    (lfs quota -p 13579)

    Both identify the same directory, neither is wrong, and comparing them
    finds nothing. A row whose mount IS this exact path is about this exact
    path whatever either side calls it.
    """
    path = "/lus/egret/projects/lanternlab-exampleu"
    root = _root(path, fileset="13579", device="egret")
    rows = cli._rows_governing(_lustre_rows(path), root)

    assert [row.scope for row in rows] == ["user", "user", "group", "group", "project", "project"]


def test_a_guessed_mount_may_not_use_that_shortcut():
    """The guard that keeps the clause above from being RD-3 again.

    A row's mount is sometimes INFERRED from its fileset name rather than
    measured, and an inferred mount is not evidence of anything. Measured on
    both sides, which is why the flag is the discriminator and a path
    comparison is not:

        mmlsquota  fileset='project-hpc'  mount='/project'  guessed=True
        lfs quota  fileset='lanternlab-'  mount='/lus/.../lanternlab-exampleu'  guessed=False

    Without it, `/project` claimed the 11T belonging to `/project/hpc`.
    """
    row = QuotaRow("project-hpc", "blocks", "user", 11 * 1024**4, mount="/project", guessed=True)
    snap = QuotaSnapshot("mmlsquota", [row])
    junction = _root("/project", fileset="root", device="meadow3_cap")

    assert cli._rows_governing(snap, junction) == []


def test_an_enforced_limit_outranks_an_accounting_total():
    """Three scopes name one directory and only one of them is the allocation.

    Taking the first row produced `4.1T of none` against a directory whose
    real answer is `30T of 50T`: a true figure about something else.
    """
    rows = _lustre_rows("/lus/egret/projects/lanternlab-exampleu")
    blocks = [row for row in rows.rows if row.kind == "blocks"]

    ordered = cli._prefer_enforced(blocks)

    assert ordered[0].scope == "project"
    assert ordered[0].hard == 60473139527680


def test_ordering_is_stable_when_nothing_is_enforced():
    """Then the backend's order survives and the user row stays first, which
    is the right default: it is the only figure about this reader alone."""
    rows = [
        QuotaRow("fs", "blocks", "user", 10, mount="/m"),
        QuotaRow("fs", "blocks", "group", 20, mount="/m"),
    ]
    assert [row.scope for row in cli._prefer_enforced(rows)] == ["user", "group"]


def test_bytes_and_files_come_from_one_scope():
    """A row describes one scope or it describes nothing coherent.

    Bytes came from the project scope and files from the user scope, so the
    row read `30T of 50T` beside `216k`, which is this reader's file count
    across the whole filesystem rather than the allocation's 8.2M.
    """
    path = "/lus/egret/projects/lanternlab-exampleu"
    run = cli.Run()
    root = _root(path, fileset="13579", device="egret", writable=True)
    run.roots = [root]
    run.quota_attempts = [_lustre_rows(path)]

    cli._place_rows(run, {}, {}, {}, {})

    assert root.quota.rows[0].scope == "project"
    assert root.inode_quota.rows[0].scope == "project"
    assert root.inode_quota.rows[0].used == 8214545


def test_the_snapshot_chosen_is_one_that_actually_governs():
    """`select_snapshot` chooses on a path PREFIX and `_rows_governing` is far
    stricter, so a snapshot could win the selection and govern nothing.

    That gap made the per-path re-ask useless on Lustre: the sweep's snapshot
    holds a row for the mount `/lus/egret`, which is a prefix of the project
    path, so it won and governed none of it while the re-asked snapshot with
    the project row was never looked at.
    """
    path = "/lus/egret/projects/lanternlab-exampleu"
    root = _root(path, fileset="13579", device="egret")
    prefix_only = QuotaSnapshot(
        "lfs quota", [QuotaRow("egret", "blocks", "user", 7, mount="/lus/egret")]
    )

    snap, rows = cli._governing_snapshot([prefix_only, _lustre_rows(path)], root)

    assert rows, "the second attempt is the one that governs"
    assert rows[0].mount == path


def test_a_user_scoped_row_reaches_the_directory_that_is_yours():
    """On Lustre nothing else connects the two.

    `lfs quota` reports against the MOUNT, so the row reads `mount=/home
    scope=user`, while the root a reader cares about is `/home/jdoe42`.
    There is no fileset and no project id on a home directory, so every other
    clause came up empty and a home with 35.7 GB in it rendered `? ? ?`.
    """
    row = QuotaRow("home", "blocks", "user", 38310621184, hard=400865761280, mount="/home")
    snap = QuotaSnapshot("lfs quota", [row])

    mine = _root("/home/jdoe42", fileset="", device="acorn", writable=True)
    assert cli._rows_governing(snap, mine) == [row]

    # ...and only to a directory that IS yours. Somebody else's home under
    # the same mount must stay `?` rather than inherit this reader's figure.
    theirs = _root("/home/someone", fileset="", device="acorn")
    assert cli._rows_governing(snap, theirs) == []


# --------------------------------------------------------------------------
# Search
# --------------------------------------------------------------------------


def _searchable(tmp_path):
    for path in ("data/ERA5-2020.nc", "data/raw/era5-1999.grib", "runs/era5/log.txt", "notes.txt"):
        (tmp_path / "lab" / path).parent.mkdir(parents=True, exist_ok=True)
        (tmp_path / "lab" / path).write_bytes(b"x" * 100)
    return str(tmp_path / "lab")


def _typing(monkeypatch, keys, finder):
    """Drive `_search_view` from a script: Enter only once the search has finished."""
    from dirscape import interactive

    script = list(keys)

    def read():
        if not script:
            return interactive.Key.BACK
        key = script.pop(0)
        if key == interactive.Key.ENTER and not finder.done:
            # A key that changes nothing, so the view paints what was found
            # before Enter is pressed on it, as a reader would see it first.
            assert finder.wait(10)
            script.insert(0, key)
            return interactive.Key.OTHER
        return key

    monkeypatch.setattr(interactive, "read_text", read)
    monkeypatch.setattr(interactive, "input_waiting", lambda timeout: True)
    # `interactive._Nothing`, not `contextlib.nullcontext`, which is 3.7+.
    monkeypatch.setattr(interactive, "raw_session", interactive._Nothing)


def test_a_search_finds_names_below_every_folder_and_draws_them_as_the_view_does(tmp_path):
    from dirscape.search import Finder

    top = _searchable(tmp_path)
    finder = Finder(top, threads=2)
    finder.start()
    finder.ask("era5")
    assert finder.wait(10)
    _query, matches, found = finder.snapshot()
    style = _plain_style()
    lines, _first, _room = cli._search_frame(
        top, "era5", matches, found, 0, None, finder, style, 100, 40, {}, {}
    )
    text = cli.plain("\n".join(lines))
    title = "".join(line.strip("│ ").split("  ")[0] for line in text.splitlines()[1:4])
    assert title.startswith("search " + top) and "3 found" in text, "the title wraps at a /"
    assert "/ era5" in text
    rows = [line.strip("│ ").split()[0] for line in text.splitlines() if "era5" in line.lower()]
    assert "data/ERA5-2020.nc" in rows and "runs/era5/" in rows
    assert "data/raw/era5-1999.grib" in rows
    assert "read 8 names in 5 folders" in text


def test_typing_a_name_and_pressing_enter_opens_the_folder_it_is_in(tmp_path, monkeypatch):
    from dirscape import interactive
    from dirscape.search import Finder

    top = _searchable(tmp_path)
    finder = Finder(top, threads=2)
    _typing(monkeypatch, list("1999") + [interactive.Key.ENTER], finder)
    out = []
    outcome, query = cli._search_view(
        top, _plain_style(), 100, interactive.Screen(out.append), {}, finder
    )
    assert (outcome, query) == (os.path.join(top, "data", "raw"), "1999")


def test_a_folder_match_opens_that_folder_and_escape_goes_back(tmp_path, monkeypatch):
    from dirscape import interactive
    from dirscape.search import Finder

    top = _searchable(tmp_path)
    finder = Finder(top, threads=2)
    _typing(monkeypatch, list("runs/") + [interactive.Key.ENTER], finder)
    outcome, _query = cli._search_view(
        top, _plain_style(), 100, interactive.Screen(lambda s: None), {}, finder
    )
    assert outcome == os.path.join(top, "runs")
    finder = Finder(top, threads=2)
    _typing(monkeypatch, list("zzz") + [interactive.Key.ENTER, interactive.Key.BACK], finder)
    outcome, query = cli._search_view(
        top, _plain_style(), 100, interactive.Screen(lambda s: None), {}, finder
    )
    assert (outcome, query) == (interactive.Key.BACK, "zzz"), "enter on nothing does nothing"


def test_slash_in_a_folder_searches_it_and_slash_again_takes_the_search_up_again(
    tmp_path, monkeypatch
):
    """A million files is not something to scroll: `/` from any folder searches
    every folder below it, and coming back to `/` finds the query as it was."""
    from dirscape import interactive

    top = _searchable(tmp_path)
    views = []

    def view(here, style, width, screen, cache, finder, query=""):
        views.append((here, query, finder))
        finder.start()
        assert finder.wait(10)
        return (os.path.join(here, "data") if len(views) == 1 else interactive.Key.BACK), "era5"

    monkeypatch.setattr(cli, "_search_view", view)
    answers = iter([interactive.Key.SEARCH, interactive.Key.BACK, interactive.Key.SEARCH])

    def select(paint, count, **kwargs):
        paint(0)
        return next(answers, interactive.Key.QUIT)

    monkeypatch.setattr(cli.interactive, "select", select)
    assert cli._descend(top, _plain_style(), 100) == interactive.Key.QUIT
    assert [(here, query) for here, query, _f in views] == [(top, ""), (top, "era5")]
    assert views[0][2] is views[1][2], "the tree read once is asked again"


# --------------------------------------------------------------------------
# Live views: what moves while dirscape waits
# --------------------------------------------------------------------------


class _Clock(object):
    def __init__(self, now=1000.0):
        self.now = now

    def __call__(self):
        return self.now


def _live_style(ascii_only=False, clock=None, chrome=None):
    """A truecolor style carrying a `Motion`, as the browser's own copy does."""
    from dirscape import motion
    from dirscape.render.style import Glyphs

    style = Style(color=True, depth=24, glyphs=Glyphs(not ascii_only), size=100)
    moving = motion.Motion(style, clock=clock or _Clock(), chrome=chrome)
    return style.moving(moving), moving


def _waiting_rows(tmp_path, names=("a", "b", "c")):
    roots = []
    for name in names:
        (tmp_path / name).mkdir()
        kid = {
            "name": name,
            "path": str(tmp_path / name),
            "readable": True,
            "writable": True,
            "enterable": True,
        }
        root = cli._listed_root(kid, None, None, {})
        root.policy[render_fields.MEASURING] = True
        roots.append(root)
    return roots


class _FakeMotion(object):
    def __init__(self, moving=True, bursts=0):
        self.moving, self.bursts = moving, bursts

    def animating(self, running=False):
        return self.moving

    def until_next(self):
        return 0.1

    def bursting(self):
        if self.bursts:
            self.bursts -= 1
            return True
        return False


def test_while_something_moves_the_keys_ask_for_frames_and_a_key_comes_first():
    from dirscape.interactive import Key

    keys = cli._sizing_keys(
        _FakeSizes(running=True), reader=lambda: Key.UP, waiting=lambda t: False,
        motion=_FakeMotion(),
    )  # fmt: skip
    assert keys() == Key.TICK, "a frame, and nothing rendered for it"
    keys = cli._sizing_keys(
        _FakeSizes(running=True), reader=lambda: Key.UP, waiting=lambda t: True,
        motion=_FakeMotion(),
    )  # fmt: skip
    assert keys() == Key.UP, "a key waiting is read before any frame"


def test_a_burst_is_rendered_frame_by_frame_and_settled_once():
    from dirscape.interactive import Key

    keys = cli._sizing_keys(
        _FakeSizes(), reader=lambda: Key.UP, waiting=lambda t: False,
        motion=_FakeMotion(bursts=2),
    )  # fmt: skip
    assert [keys() for _ in range(4)] == [Key.REDRAW, Key.REDRAW, Key.REDRAW, Key.TICK]


def test_the_row_being_counted_spins_and_the_rows_waiting_their_turn_do_not(tmp_path):
    from dirscape import motion

    clock = _Clock()
    roots = _waiting_rows(tmp_path)
    live, moving = _live_style(clock=clock)
    moving.follow(lambda: (str(tmp_path / "b"), 42))
    lines, _top, _room = cli._dir_frame(str(tmp_path), roots, 0, cli.Run(), live, 100, height=40)
    moving.overlay(lines)
    clock.now += motion.SPIN_DELAY_S + motion.FRAME_S
    shown = [cli.plain(line) for line in moving.overlay(lines)]
    assert not any(motion.MARK in line for line in moving.overlay(lines))
    row = {name: next(line for line in shown if " %s/ " % name in line) for name in "abc"}
    spinning = [ch for ch in row["b"] if "⠀" <= ch <= "⣿"]
    assert len(spinning) == 2, "the used and files cells of the row in flight both turn"
    for name in "ac":
        assert row[name].count("…") >= 2 and not any("⠀" <= ch <= "⣿" for ch in row[name])


def test_an_opened_directory_moves_in_ascii_under_ascii(tmp_path):
    roots = _waiting_rows(tmp_path)
    live, moving = _live_style(ascii_only=True)
    moving.follow(lambda: (str(tmp_path / "a"), 7))
    lines, _top, _room = cli._dir_frame(
        str(tmp_path), roots, 1, cli.Run(), live, 100, height=40, progress=1 / 3.0
    )
    text = "\n".join(cli.plain(line) for line in moving.overlay(lines))
    assert all(ord(ch) < 128 for ch in text)
    assert "=" * 10 in text.splitlines()[0], "the top edge fills with the ASCII heavy rule"


def test_the_top_edge_is_the_share_of_folders_counted_and_goes_when_all_are(tmp_path):
    from dirscape import motion

    roots = _waiting_rows(tmp_path)
    live, moving = _live_style()
    view = cli._Live(moving, str(tmp_path), cli._Sizes(roots, {}), live)
    assert view.update(roots) == 0.0
    roots[0].policy.pop(render_fields.MEASURING)
    assert view.update(roots) == pytest.approx(1 / 3.0)
    lines, _top, _room = cli._dir_frame(
        str(tmp_path), roots, 0, cli.Run(), live, 100, height=40, progress=1 / 3.0
    )
    edge = cli.plain(motion.strip_marks(lines[0]))
    span = len(edge) - 2
    assert edge.count("━") == round(span / 3.0)
    assert edge.count("─") == span - edge.count("━")
    for root in roots:
        root.policy.pop(render_fields.MEASURING, None)
    assert view.update(roots) is None, "every folder counted: the edge is a border again"


def test_a_figure_that_lands_fades_in_and_then_is_drawn_as_ever(tmp_path):
    from dirscape import motion

    clock = _Clock()
    roots = _waiting_rows(tmp_path, names=("a",))
    live, moving = _live_style(clock=clock)
    sizes = cli._Sizes(roots, {})
    view = cli._Live(moving, str(tmp_path), sizes, live)
    cli._Sizes._apply(roots[0], 4096, 3)
    sizes.landed.append(roots[0])
    view.update(roots)
    used, _caveat = render_fields.used_cell(roots[0], live)
    assert "\033[?7700;%d;" % (motion.FLASH,) in used
    clock.now += 2 * motion.FLASH_S
    used, _caveat = render_fields.used_cell(roots[0], live)
    assert motion.MARK not in used, "faded: the cell is exactly what it always was"


def test_the_bars_grow_in_once_the_last_folder_lands_and_only_after_a_count(tmp_path):
    from dirscape import motion

    clock = _Clock()
    roots = _waiting_rows(tmp_path, names=("a", "b"))
    live, moving = _live_style(clock=clock)
    view = cli._Live(moving, str(tmp_path), cli._Sizes(roots, {}), live)
    view.update(roots)
    assert view.grow() == 1.0 and view.finished is None
    for root in roots:
        root.policy.pop(render_fields.MEASURING)
    view.update(roots)
    assert view.finished == clock.now and moving.bursting()
    clock.now += motion.GROW_S / 2
    assert 0.0 < view.grow() < 1.0
    clock.now += motion.GROW_S
    assert view.grow() == 1.0

    complete = _waiting_rows(tmp_path / "x" if (tmp_path / "x").mkdir() is None else tmp_path)
    for root in complete:
        root.policy.pop(render_fields.MEASURING)
    opened_complete = cli._Live(moving, str(tmp_path), cli._Sizes(complete, {}), live)
    opened_complete.update(complete)
    assert opened_complete.finished is None, "nothing was waited for: nothing to celebrate"


def test_the_bars_grow_from_nothing_and_the_percent_is_true_throughout():
    from dirscape.render import atlas

    style = _plain_style()
    half = cli.plain(atlas._share_cell(0.5, 40, style, peak=0.5, grow=0.5))
    whole = cli.plain(atlas._share_cell(0.5, 40, style, peak=0.5, grow=1.0))
    assert half.split()[0] == whole.split()[0] == "50%"
    assert whole.count("▇") == 40 and half.count("▇") == 20


def test_a_count_long_enough_to_walk_away_from_ends_with_a_notification(tmp_path):
    notes = []

    class Chrome(object):
        def title(self, text):
            pass

        def progress(self, fraction=None, busy=False):
            pass

        def notify(self, text):
            notes.append(text)

    from dirscape import motion

    roots = _waiting_rows(tmp_path, names=("a",))
    live, moving = _live_style(chrome=Chrome())
    wall = [0.0]
    view = cli._Live(moving, str(tmp_path), cli._Sizes(roots, {}), live, wall=lambda: wall[0])
    view.update(roots)
    roots[0].policy.pop(render_fields.MEASURING)
    cli._Sizes._apply(roots[0], 4096, 3)
    wall[0] = motion.NOTIFY_AFTER_S + 5
    view.update(roots)
    assert len(notes) == 1 and str(tmp_path) in notes[0] and "2:05" in notes[0]

    quick = _waiting_rows(tmp_path / "q" if (tmp_path / "q").mkdir() is None else tmp_path)
    notes[:] = []
    wall[0] = 0.0
    fast = cli._Live(moving, str(tmp_path), cli._Sizes(quick, {}), live, wall=lambda: wall[0])
    fast.update(quick)
    for root in quick:
        root.policy.pop(render_fields.MEASURING)
    wall[0] = 3.0
    fast.update(quick)
    assert notes == [], "a count that was watched to its end needs no notification"


def test_the_status_line_tells_the_pace_the_time_and_a_wait_on_the_filesystem(tmp_path):
    from dirscape import motion

    root = cli._listed_root(
        {"name": "big", "path": str(tmp_path / "big"), "readable": True, "writable": True,
         "enterable": True},
        None, None, {},
    )  # fmt: skip
    entries = [0]

    class Sizes(object):
        def live_count(self):
            return root, entries[0], 100.0

    clock = _Clock()
    live, moving = _live_style(clock=clock)
    moving.follow(lambda: (root.path, entries[0]))
    wall = [100.0]
    line = cli._live_status(Sizes(), live, motion.Meter(), wall=lambda: wall[0])
    for _second in range(4):
        entries[0] += 20000
        clock.now += 1.0
        wall[0] += 1.0
        text = cli.plain(line(100, clock.now))
    assert "counting big/: 80k entries so far" in text
    assert "0:04" in text and "20k/s" in text
    assert any(ch in text for ch in live.g.spark)
    clock.now += motion.STALL_S + 1
    wall[0] += motion.STALL_S + 1
    assert "waiting on the filesystem for 0:06" in cli.plain(line(100, clock.now))
    assert cli.render_style.width(line(40, clock.now)) <= 40, "pieces go before the line overflows"


def test_a_slow_listing_spins_the_row_being_opened_and_a_fast_one_draws_nothing(monkeypatch):
    import time

    from dirscape import interactive, motion

    # The real clock: the spinner on a row being opened turns on time, since a
    # listing has no count to be honest to.
    live, moving = _live_style(clock=time.monotonic)
    monkeypatch.setattr(cli.interactive, "raw_session", lambda: interactive._Nothing())
    written = []
    screen = interactive.Screen(write=written.append, motion=moving)
    band = interactive.highlight(["   home   /home/me   901M", "   project   /lab   3T"], 0)
    screen.paint(band)
    before = len(written)

    monkeypatch.setattr(cli, "_children", lambda path: ([], 0, True))
    assert cli._listing("/home/me", screen, moving) == ([], 0, True)
    assert len(written) == before, "fast: the new view is its own answer"

    def slow(path):
        time.sleep(0.7)
        return ["kids"], 0, True

    monkeypatch.setattr(cli, "_children", slow)
    assert cli._listing("/home/me", screen, moving) == (["kids"], 0, True)
    drawn = "".join(written[before:])
    assert motion.MARK not in drawn
    turned = {ch for ch in cli.plain(drawn) if "⠀" <= ch <= "⣿"}
    assert len(turned) >= 2, "the spinner in the row's margin turned while it waited"

    def broken(path):
        time.sleep(0.2)
        raise RuntimeError("the listing broke")

    monkeypatch.setattr(cli, "_children", broken)
    with pytest.raises(RuntimeError):
        cli._listing("/home/me", screen, moving)


def test_ctrl_c_while_a_folder_is_being_listed_quits_instead_of_raising(tmp_path, monkeypatch):
    from dirscape import interactive

    def interrupted(here, screen, motion):
        raise KeyboardInterrupt

    monkeypatch.setattr(cli, "_listing", interrupted)
    assert cli._descend(str(tmp_path), _plain_style(), 100) == interactive.Key.QUIT


def test_the_sweep_tells_its_board_every_stage_and_what_each_found(tmp_path, monkeypatch):
    from dirscape.discover import read_mount_table
    from dirscape.runner import RecordedRunner
    from dirscape.sitecfg import Site

    (tmp_path / "work" / "me").mkdir(parents=True)
    table = read_mount_table(text="fs:/export %s nfs4 rw 0 0\n" % (tmp_path / "work",))
    monkeypatch.setattr(cli, "read_mount_table", lambda: table)
    monkeypatch.setattr(cli, "load_site", lambda warn=None: Site())
    told = []

    class Board(object):
        def __getattr__(self, name):
            return lambda *args: told.append((name,) + args)

    opts = cli.build_parser().parse_args(["--no-state", "--no-measure"])
    cli.sweep(opts, runner=RecordedRunner([], strict=False), observer=Board())
    stages = [entry[1] for entry in told if entry[0] == "stage"]
    assert stages == [
        "config", "mounts", "plugins", "quota", "allocations", "discover", "attribute",
        "snapshots", "quota-attach",
    ]  # fmt: skip
    assert ("note", "mounts", "1 filesystem") in told
    assert any(entry[:2] == ("note", "paths") for entry in told)
    assert ("claim", "allocations") in told and ("release", "allocations") in told


def test_a_board_that_raises_costs_the_sweep_nothing(tmp_path, monkeypatch):
    from dirscape.discover import read_mount_table
    from dirscape.runner import RecordedRunner
    from dirscape.sitecfg import Site

    monkeypatch.setattr(cli, "read_mount_table", lambda: read_mount_table(text=""))
    monkeypatch.setattr(cli, "load_site", lambda warn=None: Site())

    class Broken(object):
        def __getattr__(self, name):
            def fail(*args):
                raise RuntimeError("the board broke")

            return fail

    opts = cli.build_parser().parse_args(["--no-state"])
    run = cli.sweep(opts, runner=RecordedRunner([], strict=False), observer=Broken())
    assert [label for label, _at in run.timings][:3] == ["config", "mounts", "plugins"]


def test_the_runner_tells_its_observer_and_nothing_it_does_changes_the_result():
    from dirscape.runner import SubprocessRunner

    seen = []

    class Observer(object):
        def started(self, argv):
            seen.append(("started", argv[-1]))
            return "token"

        def finished(self, token, completed):
            seen.append(("finished", token, completed.returncode))

    runner = SubprocessRunner()
    runner.observer = Observer()
    result = runner.run([sys.executable, "-c", "print('hi')"])
    assert result.stdout.strip() == "hi"
    assert seen == [("started", "print('hi')"), ("finished", "token", 0)]

    class Broken(object):
        def started(self, argv):
            raise RuntimeError("no")

        def finished(self, token, completed):
            raise RuntimeError("no")

    runner.observer = Broken()
    assert runner.run([sys.executable, "-c", "print('hi')"]).stdout.strip() == "hi"


@pytest.mark.parametrize(
    "argv,env",
    [
        (["--no-motion"], {}),
        ([], {"DIRSCAPE_NO_MOTION": "1"}),
        ([], {"DIRSCAPE_AGENT": "1"}),
        (["--json"], {}),
        (["--replay", "x.json"], {}),
    ],
)
def test_no_board_where_nothing_may_move(monkeypatch, argv, env):
    from dirscape import motion

    for name in cli.AGENT_VARIABLES:
        monkeypatch.delenv(name, raising=False)
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    monkeypatch.setenv("TERM", "xterm-256color")
    monkeypatch.setattr(motion, "foreground", lambda stream: True)
    monkeypatch.setattr(cli, "_is_pipe", lambda stream: False)
    opts = cli.build_parser().parse_args(argv)
    assert cli._startup_board(opts, "atlas", _plain_style()) is None


def test_the_line_board_is_put_away_before_the_answer_is_printed(monkeypatch, capsys):
    import time

    from dirscape import motion

    for name in cli.AGENT_VARIABLES:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("TERM", "xterm-256color")
    monkeypatch.delenv("DIRSCAPE_NO_MOTION", raising=False)
    monkeypatch.setattr(motion, "foreground", lambda stream: True)
    monkeypatch.setattr(cli, "_is_pipe", lambda stream: False)
    monkeypatch.setattr(cli.interactive, "supported", lambda stream=None: False)
    run = cli.Run()
    run.roots = [_measured(_root("/home/me", "home"), used=10, limit=100)]

    def slow(opts, runner=None, save_state=True, observer=None):
        time.sleep(motion.BOARD_DELAY_S + 0.8)
        return run

    monkeypatch.setattr(cli, "sweep", slow)
    assert cli.main(["paths", "--no-state"]) == cli.EXIT_OK
    out, err = capsys.readouterr()
    assert "reading the cluster" in err, "the line was drawn while the sweep ran"
    assert err.endswith("\r\033[K"), "and taken away again before anything else"
    assert out.strip() == "/home/me" and "\033" not in out


def test_a_printed_view_never_carries_a_marker(capsys):
    from dirscape import motion

    marked = motion.Motion(object()).spin("/lab/a", "...")
    cli._write("figure " + marked)
    out = capsys.readouterr().out
    assert motion.MARK not in out and "figure ..." in out


def test_a_large_directory_moves_the_count_before_its_last_entry(tmp_path):
    """A folder of 100k files on GPFS is seconds of `stat` calls in one directory."""
    import threading

    for index in range(2500):
        (tmp_path / ("f%d" % index)).write_bytes(b"")
    seen = []

    class Tally(cli._Tally):
        def __setattr__(self, name, value):
            if name == "inodes":
                seen.append(value)
            object.__setattr__(self, name, value)

    used, files, done = cli._walk_threads(
        str(tmp_path), 1e18, 10**9, threading.Event(), None, 2, Tally()
    )
    assert done and files == 2500
    assert any(0 < value < 2500 for value in seen), "the count moved inside the directory"
    assert seen[-1] == 2500


def test_the_serial_walk_counts_its_entries_for_the_status_line(tmp_path):
    for index in range(5):
        (tmp_path / ("f%d" % index)).write_bytes(b"x")
    (tmp_path / "d").mkdir()
    tally = cli._Tally()
    cli._walk(str(tmp_path), 1e18, 10**9, tally=tally)
    assert tally.inodes == 6


def test_the_spinner_follows_the_long_count_and_never_a_glance(tmp_path, monkeypatch):
    """A glance is over in a quarter of a second when there are many folders:
    a spinner shown for a frame before it is cut is flicker."""
    import threading
    import time

    (tmp_path / "slow").mkdir()
    release = threading.Event()
    walking = []

    def count(path, deadline, ceiling, stop, parts, threads, progress):
        if progress is not None:
            progress.inodes = 17
        walking.append(sizes.walking())
        release.wait(5)
        return 1, 1, True

    monkeypatch.setattr(cli, "_count_here", count)
    monkeypatch.setattr(cli, "_rapidu_walk", lambda: None)
    roots = [cli._listed_root(kid, None, None, {}) for kid in cli._children(str(tmp_path))[0]]

    sizes = cli._Sizes(roots, {})
    sizes.start()
    stop = time.time() + 5
    while not walking and time.time() < stop:
        time.sleep(0.01)
    assert walking == [(None, None)], "the glance in flight is not followed"
    release.set()
    _settle(sizes)

    release.clear()
    walking[:] = []
    roots = [cli._listed_root(kid, None, None, {}) for kid in cli._children(str(tmp_path))[0]]
    sizes = cli._Sizes(roots, {}, glance_s=0.0)
    sizes.start()
    stop = time.time() + 5
    while sizes.walking()[0] is None and time.time() < stop:
        time.sleep(0.01)
    assert sizes.walking() == (str(tmp_path / "slow"), 17), "the long count is"
    release.set()
    _settle(sizes)
    assert sizes.walking() == (None, None)


def test_a_real_pty_animates_and_every_frame_fits():
    """Motion end to end in an actual terminal: the board, then the table, then a count.

    The sweep is slowed past `BOARD_DELAY_S` so the board is drawn wherever
    this runs, including a CI runner whose sweep takes milliseconds. What must
    hold with motion on is what held without it: every repaint moves up by
    fewer lines than the window has rows, the cursor comes back, and no marker
    ever reaches the terminal. The window title is saved and put back.
    """
    pty = pytest.importorskip("pty")
    import fcntl
    import select as sel
    import struct
    import termios
    import time

    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    driver = (
        "import sys, time\n"
        "sys.path.insert(0, %r)\n"
        "from dirscape import cli\n"
        "real = cli.sweep\n"
        "def slow(*args, **kwargs):\n"
        "    time.sleep(0.9)\n"
        "    return real(*args, **kwargs)\n"
        "cli.sweep = slow\n"
        "sys.exit(cli.main(['--no-state']))\n" % (os.path.join(root, "src"),)
    )
    pid, fd = pty.fork()
    if pid == 0:  # pragma: no cover - the child execs
        os.environ["TERM"] = "xterm-256color"
        for name in cli.AGENT_VARIABLES + ("TMUX", "STY", "DIRSCAPE_NO_MOTION"):
            os.environ.pop(name, None)
        os.execv(sys.executable, [sys.executable, "-c", driver])

    fcntl.ioctl(fd, termios.TIOCSWINSZ, struct.pack("HHHH", 40, 110, 0, 0))
    out = b""
    script = [b"\r", b"\x1b[B", b"q"]
    sent, ready_at = 0, None
    deadline = time.time() + 90
    try:
        while time.time() < deadline:
            ready, _, _ = sel.select([fd], [], [], 0.1)
            if ready:
                try:
                    chunk = os.read(fd, 65536)
                except OSError:
                    break
                if not chunk:
                    break
                out += chunk
            if ready_at is None:
                if b"quit" in out:
                    ready_at = time.time() + 0.6
                continue
            if time.time() < ready_at or sent >= len(script):
                continue
            os.write(fd, script[sent])
            sent += 1
            ready_at = time.time() + 1.2
        text = out.decode("utf-8", "replace")
    finally:
        with contextlib.suppress(OSError):
            os.close(fd)
        with contextlib.suppress(OSError):
            os.waitpid(pid, os.WNOHANG)

    if "\033[?25l" not in text:
        pytest.skip("no interactive frame was drawn here, so there is nothing to measure")
    assert sent == len(script), "the keystrokes were never all sent"
    assert "reading the cluster" in text, "the board never drew"
    assert text.index("reading the cluster") < text.index("quit"), "board first, then the table"
    assert "\033[?770" not in text, "a motion marker reached the terminal"
    ups = [int(n) for n in re.findall(r"\033\[(\d+)A", text)]
    assert ups and max(ups) < 40, "a repaint moved up past the top of a 40 row window"
    assert text.rfind("\033[?25h") > text.rfind("\033[?25l"), "the cursor was left hidden"
    assert "\033[22;0t" in text and text.rfind("\033[23;0t") > text.rfind("\033]0;")


def test_landings_are_drawn_a_few_times_a_second_and_keys_are_never_held():
    """Every landing re-sorts the rows: drawn one by one, a first pass
    reshuffled the table a dozen times a second."""
    from dirscape.interactive import Key

    now = [0.0]

    class Landing(_FakeSizes):
        """Figures that land at given moments, and a walk that ends with the last."""

        def __init__(self, times):
            _FakeSizes.__init__(self, running=True)
            self.times = list(times)

        def busy(self):
            return bool(self.times)

        def changed(self):
            if self.times and self.times[0] <= now[0]:
                self.times = [t for t in self.times if t > now[0]]
                return True
            return False

    def waiting(seconds):
        now[0] += seconds
        return False

    keys = cli._sizing_keys(
        Landing([0.0, 0.05, 0.1, 0.5]), reader=lambda: Key.QUIT, waiting=waiting,
        clock=lambda: now[0], motion=_FakeMotion(moving=False),
    )  # fmt: skip
    drawn = []
    while True:
        key = keys()
        if key != Key.REDRAW:
            break
        drawn.append(now[0])
    gaps = [b - a for a, b in zip(drawn, drawn[1:])]
    assert len(drawn) == 3, "the two that landed close together are drawn as one"
    assert all(gap >= cli.LANDING_GAP_S - 1e-9 for gap in gaps), gaps

    held = cli._sizing_keys(
        _FakeSizes(changes=[True, True], running=True), reader=lambda: Key.UP,
        waiting=lambda t: True, clock=lambda: now[0], motion=_FakeMotion(moving=False),
    )  # fmt: skip
    assert held() == Key.REDRAW
    assert held() == Key.UP, "a key waiting is read at once, landing or not"


def test_without_motion_every_landing_is_drawn_as_it_always_was():
    from dirscape.interactive import Key

    keys = cli._sizing_keys(
        _FakeSizes(changes=[True, True]), reader=lambda: Key.UP, waiting=lambda t: True
    )
    assert [keys(), keys()] == [Key.REDRAW, Key.REDRAW]


def test_a_quick_figure_lands_quietly_and_one_that_was_waited_for_fades_in(tmp_path):
    from dirscape import motion

    roots = _waiting_rows(tmp_path, names=("quick", "slow", "elsewhere"))
    live, moving = _live_style()
    sizes = cli._Sizes(roots, {})
    view = cli._Live(moving, str(tmp_path), sizes, live)
    sizes.took = {roots[0].path: 0.1, roots[1].path: motion.FLASH_AFTER_S + 0.5}
    for root in roots:
        root.policy.pop(render_fields.MEASURING)
        cli._Sizes._apply(root, 4096, 3)
    sizes.landed.extend(roots)
    view.update(roots)
    marked = [render_fields.LANDED in root.policy for root in roots]
    assert marked == [False, True, True], (
        "only the waited-for fade in; the aside's counts were long"
    )


# --------------------------------------------------------------------------
# Counting less and sooner: freshness, deeper figures, the first pass together
# --------------------------------------------------------------------------


def test_a_large_folder_stays_fresh_longer_and_every_figure_ages_out_in_a_week():
    assert cli._fresh_for(10) == cli.FRESH_S
    assert cli._fresh_for(cli.FRESH_FILES) == cli.FRESH_S
    assert cli._fresh_for(20 * cli.FRESH_FILES) == 20 * cli.FRESH_S
    assert cli._fresh_for(10**9) == cli.FRESH_MAX_S


def test_a_big_folder_counted_yesterday_is_not_walked_again_and_a_small_one_is(tmp_path):
    """`/project/rcc` read "sizes as counted 19h ago, counting again behind the
    view": the whole tree walked again because a day had passed."""
    import time

    from dirscape.state.sizes import SizeIndex

    for name in ("big", "small"):
        (tmp_path / "lab" / name).mkdir(parents=True)
    index = SizeIndex()
    then = time.time() - 19 * 3600
    for name, files in (("big", 3 * 10**6), ("small", 50)):
        path = tmp_path / "lab" / name
        index.put(str(path), 10**12, files, then, ino=os.lstat(str(path)).st_ino)
    roots, sizes = _open_level(tmp_path / "lab", {}, index)
    stale = [os.path.basename(root.path) for root in sizes._stale]
    assert stale == ["small"], "the 3 million file folder is still fresh at 19 hours"
    assert sizes.stored()[1] is True, "and the small one is being counted again"


def test_a_walk_keeps_the_figures_below_its_folders_largest_first(tmp_path):
    parts = {}
    top = str(tmp_path)
    for name in ("a", "b"):
        parts[os.path.join(top, name)] = (100, 1)
        for n in range(cli.DEEP_KEPT + 20):
            parts[os.path.join(top, name, "d%03d" % n)] = (n, 1)
    kept = cli._kept_parts(top, parts)
    assert kept[:2] == [os.path.join(top, "a"), os.path.join(top, "b")], "the first level, all"
    deeper = kept[2:]
    assert len(deeper) == cli.DEEP_KEPT
    assert min(parts[path][0] for path in deeper) >= max(
        parts[path][0]
        for path in parts
        if path not in kept and path.count(os.sep) > top.count(os.sep) + 1
    ), "the largest below"


def test_only_what_a_listing_can_open_is_kept(tmp_path):
    top = str(tmp_path)
    parts = {os.path.join(top, "f%04d" % n): (1, 1) for n in range(cli.CHILD_LIMIT + 50)}
    kept = cli._kept_parts(top, parts)
    assert kept == sorted(parts)[: cli.CHILD_LIMIT], "a listing shows the first by name"


def test_a_folder_counted_once_opens_two_levels_down_without_a_walk(tmp_path):
    """After `/project/rcc` was counted, `lykhin/` opened at once and
    `lykhin/runs/` was walked from scratch."""
    import time

    from dirscape.state.sizes import SizeIndex

    root = _tree(tmp_path / "lab", {"x/runs/a": 2, "x/runs/b": 1, "x/other": 1})
    index = SizeIndex()
    roots, sizes = _open_level(root, {}, index)
    sizes.start()
    _settle(sizes)
    for sub in ("x/runs", "x/runs/a", "x/runs/b", "x/other"):
        path = str(root / sub)
        kept = index.get(path, os.lstat(path).st_ino)
        assert kept is not None and kept[:2] == cli._walk(path, 1e18, 10**9)[:2], sub
        assert kept[2] <= time.time()

    walked = []
    real = cli._size_of

    def spy(path, *args, **kwargs):
        walked.append(path)
        return real(path, *args, **kwargs)

    cli_size_of = cli._size_of
    cli._size_of = spy
    try:
        _roots, deeper = _open_level(root / "x" / "runs", {}, index)
        deeper.start()
        _settle(deeper)
    finally:
        cli._size_of = cli_size_of
    assert walked == [], "its folders came from the walk of the level above"


def _network_rows(tmp_path, names):
    parent = Root(str(tmp_path), fstype="gpfs")
    rows = []
    for name in names:
        (tmp_path / name).mkdir()
        kid = {"name": name, "path": str(tmp_path / name), "readable": True,
               "writable": True, "enterable": True}  # fmt: skip
        rows.append(cli._listed_root(kid, parent, None, {}))
    return rows


def test_the_first_pass_glances_at_folders_together_and_the_second_counts_one_at_a_time(
    tmp_path, monkeypatch
):
    import threading
    import time

    lock = threading.Lock()
    flight = {"glance": [0, 0], "full": [0, 0]}
    walkers = {"glance": set(), "full": set()}

    def count(path, deadline, ceiling, stop, parts, threads, progress):
        kind = "full" if deadline - time.time() > 100 else "glance"
        with lock:
            flight[kind][0] += 1
            flight[kind][1] = max(flight[kind][1], flight[kind][0])
            walkers[kind].add(threads)
        try:
            time.sleep(0.15)
            done = kind == "full" or not os.path.basename(path).startswith("big")
            return 1, 1, done
        finally:
            with lock:
                flight[kind][0] -= 1

    monkeypatch.setattr(cli, "_count_here", count)
    monkeypatch.setattr(cli, "_rapidu_walk", lambda: None)
    monkeypatch.setattr(cli, "GLANCE_TOGETHER", 2)
    rows = _network_rows(tmp_path, ["a", "b", "c", "d", "e", "f", "big1", "big2", "big3"])
    sizes = cli._Sizes(rows, {})
    sizes.start()
    _settle(sizes)
    assert flight["glance"][1] > 1, "several glances at once"
    assert flight["glance"][1] <= cli.GLANCE_WORKERS
    assert flight["full"][1] == 1, "the long counts one at a time"
    assert all(path in sizes.cache for path in (row.path for row in rows)), "every figure landed"
    assert walkers["glance"] == {cli.WALK_THREADS // cli.GLANCE_WORKERS}, (
        "sixteen walkers shared out"
    )
    assert walkers["full"] == {cli.WALK_THREADS}, "and a long count has all of them"


def test_local_storage_glances_one_folder_at_a_time(tmp_path, monkeypatch):
    import threading
    import time

    lock = threading.Lock()
    flight = [0, 0]

    def count(path, deadline, ceiling, stop, parts, threads, progress):
        with lock:
            flight[0] += 1
            flight[1] = max(flight[1], flight[0])
        time.sleep(0.05)
        with lock:
            flight[0] -= 1
        return 1, 1, True

    monkeypatch.setattr(cli, "_count_here", count)
    monkeypatch.setattr(cli, "_rapidu_walk", lambda: None)
    monkeypatch.setattr(cli, "GLANCE_TOGETHER", 2)
    for name in "abcd":
        (tmp_path / name).mkdir()
    rows = [cli._listed_root(kid, None, None, {}) for kid in cli._children(str(tmp_path))[0]]
    sizes = cli._Sizes(rows, {})
    sizes.start()
    _settle(sizes)
    assert flight[1] == 1


def test_with_rapidu_the_glances_in_flight_split_its_threads(tmp_path, monkeypatch):
    import threading
    import time

    asked = []
    lock = threading.Lock()

    class Result(object):
        size, files, partial, root, dir_agg = 1, 1, False, "", {}

    def walk(path, **options):
        with lock:
            asked.append(options.get("threads"))
        time.sleep(0.05)
        return Result()

    monkeypatch.setattr(cli, "_rapidu_walk", lambda: walk)
    monkeypatch.setattr(cli, "GLANCE_TOGETHER", 2)
    rows = _network_rows(tmp_path, ["a", "b", "c", "d", "e", "f"])
    sizes = cli._Sizes(rows, {})
    sizes.start()
    _settle(sizes)
    assert asked and set(asked) == {cli.WALK_THREADS // cli.GLANCE_WORKERS}
    assert len(sizes.cache) == 6


def test_leaving_stops_every_glance_in_flight(tmp_path, monkeypatch):
    import threading
    import time

    stopped = []

    def count(path, deadline, ceiling, stop, parts, threads, progress):
        while not stop.is_set() and time.time() < deadline:
            time.sleep(0.01)
        stopped.append(stop.is_set())
        return 0, 0, False

    monkeypatch.setattr(cli, "_count_here", count)
    monkeypatch.setattr(cli, "_rapidu_walk", lambda: None)
    monkeypatch.setattr(cli, "GLANCE_TOGETHER", 2)
    rows = _network_rows(tmp_path, ["a", "b", "c", "d"])
    sizes = cli._Sizes(rows, {}, per_child_s=30.0, glance_s=120.0)
    sizes.start()
    stop = time.time() + 5
    while sizes._glancing < 2 and time.time() < stop:
        time.sleep(0.01)
    assert sizes._glancing >= 2
    sizes.stop()
    stop = time.time() + 5
    while sizes.busy() and time.time() < stop:
        time.sleep(0.01)
    assert not sizes.busy() and stopped and all(stopped)


def test_a_folder_the_short_glance_did_not_finish_gets_a_longer_one(tmp_path, monkeypatch):
    """Short glances fill in the small folders fast; the middling ones get a
    second, longer glance side by side instead of waiting their turn for a
    long count."""
    import threading
    import time

    lock = threading.Lock()
    calls = []

    def count(path, deadline, ceiling, stop, parts, threads, progress):
        cap = deadline - time.time()
        name = os.path.basename(path)
        with lock:
            calls.append((name, "full" if cap > 100 else "glance", threads, cap))
        if cap > 100:
            return 1, 1, True
        needs = {"small": 0.0, "mid": 1.5 * sizes.per_child_s}.get(name[:-1], 1e9)
        return 1, 1, cap >= needs

    monkeypatch.setattr(cli, "_count_here", count)
    monkeypatch.setattr(cli, "_rapidu_walk", lambda: None)
    monkeypatch.setattr(cli, "GLANCE_TOGETHER", 2)
    rows = _network_rows(tmp_path, ["small1", "small2", "mid1", "mid2", "big1", "big2"])
    sizes = cli._Sizes(rows, {}, per_child_s=0.1)
    sizes.start()
    _settle(sizes)
    assert all(row.path in sizes.cache for row in rows), "every figure landed"

    def of(name):
        return [(how, threads, cap) for who, how, threads, cap in calls if who == name]

    share = cli.GLANCE_WORKERS
    assert [how for how, _, _ in of("small1")] == ["glance"]
    assert [how for how, _, _ in of("mid1")] == ["glance", "glance"], "no long count"
    assert [how for how, _, _ in of("big1")] == ["glance", "glance", "full"]
    short, longer = of("mid1")
    assert short[1] == longer[1] == cli.WALK_THREADS // share
    assert longer[2] > (share - 0.5) * sizes.per_child_s, "as long as its walkers are few"
    assert of("big1")[-1][1] == cli.WALK_THREADS, "the long count has every walker"


def test_the_first_pass_ends_when_one_glance_at_a_time_could_have(tmp_path, monkeypatch):
    """Two glances for every big folder must not keep a directory of big
    folders from its long counts any longer than one glance each did."""
    import threading
    import time

    lock = threading.Lock()
    began = []
    t0 = time.time()

    def count(path, deadline, ceiling, stop, parts, threads, progress):
        if deadline - time.time() > 100:
            with lock:
                began.append(time.time() - t0)
            return 1, 1, True
        while time.time() < deadline and not stop.is_set():
            time.sleep(0.005)
        return 1, 1, False

    monkeypatch.setattr(cli, "_count_here", count)
    monkeypatch.setattr(cli, "_rapidu_walk", lambda: None)
    monkeypatch.setattr(cli, "GLANCE_TOGETHER", 2)
    rows = _network_rows(tmp_path, ["big%d" % i for i in range(5)])
    sizes = cli._Sizes(rows, {}, per_child_s=0.3)
    t0 = time.time()
    sizes.start()
    _settle(sizes)
    one_at_a_time = 5 * sizes.per_child_s
    assert len(began) == 5 and all(row.path in sizes.cache for row in rows)
    # Unbounded, the second glances would hold them to about 2.7s.
    assert min(began) < one_at_a_time + 0.6, "long counts begin on time"


def test_fewer_folders_than_it_takes_are_glanced_at_one_at_a_time(tmp_path, monkeypatch):
    """Below `GLANCE_TOGETHER` each glance is long enough for sixteen walkers
    to finish a middling folder alone, which a quarter of them do not."""
    import threading
    import time

    lock = threading.Lock()
    asked, flight = [], [0, 0]

    def count(path, deadline, ceiling, stop, parts, threads, progress):
        with lock:
            asked.append(threads)
            flight[0] += 1
            flight[1] = max(flight)
        try:
            return 1, 1, deadline - time.time() > 100
        finally:
            with lock:
                flight[0] -= 1

    monkeypatch.setattr(cli, "_count_here", count)
    monkeypatch.setattr(cli, "_rapidu_walk", lambda: None)
    names = ["big%02d" % i for i in range(cli.GLANCE_TOGETHER - 1)]
    sizes = cli._Sizes(_network_rows(tmp_path, names), {})
    sizes.start()
    _settle(sizes)
    assert len(sizes.cache) == len(names)
    assert set(asked) == {cli.WALK_THREADS} and flight[1] == 1, "every walker, one at a time"
    assert len(asked) == 2 * len(names), "one glance each, then its long count"


def test_glances_side_by_side_spend_the_visit_by_the_clock(tmp_path, monkeypatch):
    """The visit's allowance is wall time: four glances at once for a moment
    have spent a moment, not four."""
    import time

    def count(path, deadline, ceiling, stop, parts, threads, progress):
        time.sleep(0.3)
        return 1, 1, True

    monkeypatch.setattr(cli, "_count_here", count)
    monkeypatch.setattr(cli, "_rapidu_walk", lambda: None)
    monkeypatch.setattr(cli, "GLANCE_TOGETHER", 2)
    sizes = cli._Sizes(_network_rows(tmp_path, ["a", "b", "c", "d"]), {})
    sizes.start()
    _settle(sizes)
    assert len(sizes.cache) == 4
    assert 0.25 < sizes._spent < 0.9
