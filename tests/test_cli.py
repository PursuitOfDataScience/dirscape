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


def test_a_family_of_silent_children_folds_into_its_parent():
    """`/project2/reference` has twenty collections in one fileset, so the
    parent holds the only figure and each child is a `?`. Twenty rows for one
    fact was the worst case in the default view.
    """
    parent = _measured(_root("/project2/reference", "project2-reference"))
    children = [
        _root("/project2/reference/%s" % name, "project2-reference")
        for name in ("newsome", "brook", "pdb", "gtdb")
    ]

    kept, folded = cli._collapse_families([parent] + children)

    assert [r.path for r in kept] == ["/project2/reference"]
    assert folded == 4
    assert parent.policy["contains"] == 4


def test_a_child_with_something_to_say_is_never_folded():
    """The count is a summary of silence, not of content."""
    parent = _measured(_root("/project2/reference", "project2-reference"))
    loud = _measured(_root("/project2/reference/newsome", "project2-newsome"))
    quiet = _root("/project2/reference/pdb", "project2-reference")

    kept, folded = cli._collapse_families([parent, loud, quiet])

    assert "/project2/reference/newsome" in [r.path for r in kept]
    assert folded == 1


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
