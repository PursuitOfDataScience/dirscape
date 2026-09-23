"""GPFS backend, against a real transcript from a GPFS site.

`tests/fixtures/gpfs_login.json` is a captured run, replayed through
`RecordedRunner`. Nothing here touches a live cluster and nothing reads the
host it runs on: the device names, filesets and figures asserted below are all
fixture data, so this file passes on a laptop. Reading the running host is
rapiDU's RD-10, where a test pinned the development cluster's identity and
failed everywhere else.
"""

import json
import os

import pytest

from dirscape.discover.mounts import read_mount_table
from dirscape.model import TRANSIENT_CATEGORIES, VerdictCategory
from dirscape.quota import gpfs
from dirscape.runner import Budget, RecordedRunner

FIXTURES = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fixtures")

# The device the captured account holds most of its data on. Fixture data, not
# a lookup: it is the string the recorded `mmlsquota` output contains.
DEVICE = "meadow3_cap"


def fixture(name):
    with open(os.path.join(FIXTURES, name)) as handle:
        return handle.read()


def transcript():
    with open(os.path.join(FIXTURES, "gpfs_login.json")) as handle:
        return json.load(handle)


@pytest.fixture
def gpfs_runner():
    return RecordedRunner.from_json(transcript())


@pytest.fixture
def gpfs_mounts():
    return read_mount_table(text=fixture("mounts_gpfs_login.txt"))


def read(runner, mounts, path="/home/someone", **kwargs):
    return gpfs.GpfsBackend(**kwargs).read(runner, mounts, None, [path])


# --------------------------------------------------------------------------
# The reading itself
# --------------------------------------------------------------------------


def test_blocks_and_inodes_are_both_emitted(gpfs_runner, gpfs_mounts):
    """Two rows per fileset. Reporting only blocks is rapiDU's RD-9/RD-15.

    On this very account the inode figure is the one nearer the wall: the home
    fileset is at 2.7% of its block quota and 12.0% of its inode quota, so a
    blocks-only report leaves out the number that will stop the writes.
    """
    snap = read(gpfs_runner, gpfs_mounts)
    kinds = {row.kind for row in snap.rows}
    assert kinds == {"blocks", "files"}

    home_blocks = one(snap, "home", "blocks")
    home_files = one(snap, "home", "files")
    assert home_blocks.fraction < 0.03
    assert home_files.fraction > 0.11
    assert home_files.fraction > home_blocks.fraction


def test_kb_columns_are_normalised_to_bytes(gpfs_runner, gpfs_mounts):
    """The KB column is KB. Reading it as bytes under-reports by 1024x.

    Expectations are derived from the transcript rather than written out, so
    re-capturing the fixture on a day the usage has moved does not break a
    test about the unit.
    """
    snap = read(gpfs_runner, gpfs_mounts)
    record = record_for("home")
    home = one(snap, "home", "blocks")
    assert home.used == int(record["blockusage"]) * 1024
    assert home.soft == int(record["blockquota"]) * 1024
    assert home.hard == int(record["blocklimit"]) * 1024
    # The inode row is a count and is never scaled.
    assert one(snap, "home", "files").used == int(record["filesusage"])


def test_a_zero_limit_is_unlimited_and_not_full(gpfs_runner, gpfs_mounts):
    """`0` means no limit. Reading it as a limit of zero reports 100% full.

    Six of the eight filesets in this transcript have `0 0` for both limits,
    including one holding 11.2 TiB. If a zero were treated as a limit every
    one of them would render as over quota, and if it were folded to None the
    backend's own statement that there is no limit would be lost.
    """
    snap = read(gpfs_runner, gpfs_mounts)
    software = one(snap, "software", "blocks")
    assert software.used == int(record_for("software")["blockusage"]) * 1024
    assert software.soft == 0
    assert software.hard == 0
    assert software.limit is None
    assert software.fraction is None


def test_unlimited_is_kept_as_zero_not_none(gpfs_runner, gpfs_mounts):
    """Zero and None are different claims and both must survive the parse."""
    snap = read(gpfs_runner, gpfs_mounts)
    for row in snap.rows:
        assert row.soft is not None, row.label
        assert row.hard is not None, row.label


def test_in_doubt_is_populated_and_raises_figure_doubt(gpfs_runner, gpfs_mounts):
    """`in_doubt` is the honest reason a quota and a du walk disagree.

    Measured on this transcript: 2.18 GiB in doubt against 831 MiB used, so
    the gap is 2.7x the usage. It is doubt about the FIGURE, so it belongs in
    `figure_note` and not in `time_note`.
    """
    snap = read(gpfs_runner, gpfs_mounts)
    record = record_for("home")
    home = one(snap, "home", "blocks")
    assert home.in_doubt == int(record["blockindoubt"]) * 1024
    assert home.in_doubt > home.used * 2
    assert one(snap, "home", "files").in_doubt == int(record["filesindoubt"])
    assert "in doubt" in snap.figure_note
    assert "in doubt" not in snap.time_note


def test_the_reading_is_live_and_says_so(gpfs_runner, gpfs_mounts):
    """A live query has an age of about zero, not an unknown age.

    Leaving `taken_at` unset made `age_seconds` None in rapiDU, which
    permanently tripped its "published no timestamp" blocker and made one
    verdict unreachable on every GPFS site.
    """
    snap = read(gpfs_runner, gpfs_mounts)
    assert snap.age_seconds == 0.0
    assert "live" in snap.time_note


# --------------------------------------------------------------------------
# Fileset enumeration, the feature
# --------------------------------------------------------------------------


def test_filesets_seen_lists_what_the_user_holds_blocks_in(gpfs_runner, gpfs_mounts):
    """Including filesets the user has no unix group for.

    This is the only route to storage somebody is charged for and can no
    longer reach: five of these are `project-*` filesets the captured account
    cannot list, together holding about 19 GB.

    The list spans EVERY gpfs device the node mounts, not just the one the
    asked-about path lives on. That is deliberate and it is the point of the
    feature: stranded space on meadow2 costs the user exactly as much as
    stranded space on meadow3, and a per-device answer would hide it. The
    captured node mounts six devices across three clusters.
    """
    snap = read(gpfs_runner, gpfs_mounts)
    seen = gpfs.filesets_seen(snap)

    # meadow3_cap, where the five unreachable project filesets are.
    assert seen[:8] == [
        "software",
        "home",
        "project-hpc",
        "project-mdgreenwood",
        "project-abe",
        "project-dahlias",
        "project-bard",
        "project-pelican",
    ]
    # ... and the other devices' filesets follow, rather than being dropped.
    assert set(seen[8:]) == {"scratch", "project2-hpc", "project2-reference"}
    assert len(seen) == len(set(seen)), "a fileset must not be listed twice"


def test_filesets_seen_does_not_invent_paths(gpfs_runner, gpfs_mounts):
    """Names only. Resolving one to a path is the discovery layer's job."""
    snap = read(gpfs_runner, gpfs_mounts)
    for name in gpfs.filesets_seen(snap):
        assert not name.startswith("/")


# --------------------------------------------------------------------------
# Mount attribution
# --------------------------------------------------------------------------


def test_a_fileset_is_narrowed_to_one_of_its_own_devices_mounts(gpfs_runner, gpfs_mounts):
    """The device is mounted five times here and the filesets differ.

    Handing every fileset the device's whole mount list, first entry wins, put
    a 329 GB figure that lives in /software onto a row labelled /home.
    """
    snap = read(gpfs_runner, gpfs_mounts)
    assert one(snap, "home", "blocks").mount == "/home"
    assert one(snap, "software", "blocks").mount == "/software"
    # `project-hpc` spells /project/hpc, which is not itself a mount, so the
    # longest device mount that is a prefix of it wins.
    assert one(snap, "project-hpc", "blocks").mount == "/project"


def test_a_narrowed_mount_is_flagged_and_names_its_confirmation(gpfs_runner, gpfs_mounts):
    """An inference says so, and says what would settle it."""
    row = one(read(gpfs_runner, gpfs_mounts), "home", "blocks")
    assert row.guessed is True
    assert "mmlsattr" in row.note


def test_rows_map_the_path_that_was_asked_about(gpfs_runner, gpfs_mounts):
    snap = read(gpfs_runner, gpfs_mounts, path="/home/someone")
    hits = snap.rows_for_path("/home/someone")
    assert hits
    assert {row.fileset for row in hits} == {"home"}


def test_a_device_absent_from_the_mount_table_still_yields_rows(gpfs_runner):
    """No mount table entry is not a reason to discard a parsed figure."""
    mounts = read_mount_table(text="meadow3_cap /gpfs/other gpfs rw 0 0\n")
    snap = gpfs.GpfsBackend().read(gpfs_runner, mounts, None, ["/"])
    assert snap.available
    single = one(snap, "home", "blocks")
    # One mount for the device, so the kernel answered and nothing is guessed.
    assert single.mount == "/gpfs/other"
    assert single.guessed is False


def test_mmlsattr_confirms_a_fileset_and_clears_the_guess(gpfs_runner, gpfs_mounts):
    """`mmlsattr -L` is unprivileged and turns the inference into evidence."""
    snap = read(gpfs_runner, gpfs_mounts)
    assert gpfs.read_path_fileset(gpfs_runner, "/home/jdoe42") == "home"
    assert gpfs.confirm_path_fileset(gpfs_runner, snap, "/home/jdoe42") == "home"
    row = one(snap, "home", "blocks")
    assert row.guessed is False
    assert "confirmed by mmlsattr" in row.note


def test_mmlsattr_unmaps_a_row_it_contradicts(gpfs_runner, gpfs_mounts):
    """A row narrowed onto a mount the probe disowns loses that mount.

    The contradiction has to be a real one. One `meadow3_cap` fileset row is
    forced onto `/home`, which the probe then reports as fileset `home`, so
    that row is claiming a path it demonstrably does not govern and must give
    it up. This is rapiDU's RD-3 (a guessed mount confidently attributed to
    the wrong place) and the probe exists to end it.

    An earlier version of this test confirmed `/project/hpc` and expected the
    `home` row to be unmapped, which was wrong: learning the fileset of
    `/project/hpc` says nothing about a row mounted at `/home`. The assertion
    passed only because the equality case in the contradiction check was
    missing, so the check never fired at all.
    """
    snap = read(gpfs_runner, gpfs_mounts)
    row = one(snap, "project-hpc", "blocks")
    row.mount = "/home"
    row.mounts = ["/home"]
    row.guessed = True

    assert gpfs.confirm_path_fileset(gpfs_runner, snap, "/home") == "home"

    assert row.mount is None
    assert row.mounts == []
    assert "not project-hpc" in row.note


def test_mmlsattr_leaves_an_uncontradicted_row_alone(gpfs_runner, gpfs_mounts):
    """The control, and it is the half that keeps the tool useful.

    Confirming `/project/hpc` must not strip the `home` row's mount. Unmapping
    every guessed row on the device would leave home usage unattributable,
    which is a worse answer than an inference honestly labelled as one.
    """
    snap = read(gpfs_runner, gpfs_mounts)
    home = one(snap, "home", "blocks")
    assert home.mount == "/home"

    gpfs.confirm_path_fileset(gpfs_runner, snap, "/project/hpc")

    assert home.mount == "/home"
    assert home.guessed is True, "still an inference, and still labelled as one"


def test_a_nested_fileset_is_not_a_contradiction(gpfs_runner, gpfs_mounts):
    """Nested filesets are normal on GPFS, so the check is one-directional.

    A row mounted at `/project/hpc` is not contradicted by learning that its
    PARENT `/project` is fileset `project`. Only a probe at or below the row's
    own mount can disown it.
    """
    snap = read(gpfs_runner, gpfs_mounts)
    row = one(snap, "project-hpc", "blocks")
    row.mount = "/project/hpc"
    row.mounts = ["/project/hpc"]
    row.guessed = True

    gpfs.confirm_path_fileset(gpfs_runner, snap, "/project")

    assert row.mount == "/project/hpc"


# --------------------------------------------------------------------------
# Absence: five states, and none of them is an empty reading
# --------------------------------------------------------------------------


def test_header_only_output_is_absence_and_not_zero_usage(gpfs_runner):
    """The measured hazard: one line, exit 0, empty stderr, no records.

    `Completed.failed` is False here, so nothing about the command's failure
    can tell you this is not a reading of zero usage. Success is asserted
    positively instead, and the answer says what GPFS actually reported.
    """
    mounts = read_mount_table(text="collie3_cap /collie3 gpfs rw 0 0\n")
    snap = gpfs.GpfsBackend().read(gpfs_runner, mounts, None, ["/collie3"])
    assert snap.rows == []
    assert snap.available is False
    assert snap.category == VerdictCategory.NO_QUOTA_ENFORCED
    assert "only filesets with usage" in snap.reason


def test_the_header_only_transcript_really_is_a_success(gpfs_runner):
    """Guard on the fixture, not the parser: rc=0 and nothing on stderr."""
    result = gpfs_runner.run(["/usr/lpp/mmfs/bin/mmlsquota", "-Y", "collie3_cap"])
    assert result.returncode == 0
    assert result.stderr == ""
    assert result.failed is False
    assert len(result.stdout.splitlines()) == 1


def test_exit_zero_with_stderr_is_still_a_failure(recorded, cmd, gpfs_mounts):
    """GPFS on another build exits 0 while failing, so `failed` is the gate.

    The reason must name the cause and not the pointer line: "GPFS is down on
    this node" tells a reader to use another filesystem, while "Examine
    previous error messages" tells them to read the lines this module just
    discarded.
    """
    down = fixture("gpfs_mmlsquota_down.txt")
    runner = recorded(
        [cmd(["/usr/lpp/mmfs/bin/mmlsquota", "-Y", DEVICE], stderr=down, returncode=0)],
        {"mmlsquota": "/usr/lpp/mmfs/bin/mmlsquota"},
    )
    mounts = read_mount_table(text=DEVICE + " /home gpfs rw 0 0\n")
    snap = gpfs.GpfsBackend().read(runner, mounts, None, ["/home"])
    assert snap.available is False
    assert snap.category == VerdictCategory.BACKEND_FAILED
    assert "GPFS is down on this node" in snap.reason
    assert "Examine previous error messages" not in snap.reason


def test_a_missing_tool_is_a_different_state_from_a_failure(recorded):
    runner = recorded([], {"mmlsquota": None})
    mounts = read_mount_table(text="fsA /mnt/alpha gpfs rw 0 0\n")
    snap = gpfs.GpfsBackend().read(runner, mounts, None, ["/mnt/alpha"])
    assert snap.category == VerdictCategory.NO_QUOTA_BACKEND
    assert snap.category not in TRANSIENT_CATEGORIES
    assert "/usr/lpp/mmfs/bin" in snap.reason


def test_no_gpfs_mounted_is_not_reported_as_a_broken_tool(gpfs_runner):
    mounts = read_mount_table(text="/dev/root / xfs rw 0 0\n")
    snap = gpfs.GpfsBackend().read(gpfs_runner, mounts, None, ["/"])
    assert snap.category == VerdictCategory.NO_QUOTA_BACKEND
    assert "no gpfs filesystem is mounted" in snap.reason


def test_asking_about_another_user_is_refused_transiently(gpfs_runner, gpfs_mounts):
    """`mmlsquota -u root` prints Operation not permitted and exits 1.

    That is not "root has no quota". It is a question this process is not
    allowed to ask, so it is transient and renders as unknown.
    """
    snap = gpfs.read_user_quota(gpfs_runner, gpfs_mounts, None, "root", devices=[DEVICE])
    assert snap.available is False
    assert snap.category == VerdictCategory.PERMISSION_TO_ASK_DENIED
    assert snap.category in TRANSIENT_CATEGORIES
    assert "not permitted" in snap.reason.lower()


def test_an_exhausted_budget_is_not_probed(gpfs_runner, gpfs_mounts):
    spent = Budget(0.0)
    snap = gpfs.GpfsBackend().read(gpfs_runner, gpfs_mounts, spent, ["/home/someone"])
    assert snap.available is False
    assert "budget ran out" in snap.reason


def test_the_device_cap_reports_itself(gpfs_runner, gpfs_mounts):
    """A bound that silently drops filesystems reads as "you have no quota"."""
    snap = gpfs.GpfsBackend(max_devices=1).read(gpfs_runner, gpfs_mounts, None, ["/project/hpc"])
    assert snap.available
    assert "1 of 6 gpfs filesystems were asked" in snap.reason
    # And the one asked is the device that actually holds the path, not the
    # alphabetically first, which is what makes the cap a bound on cost.
    assert "meadow3_cap first" in snap.reason


# --------------------------------------------------------------------------
# The human table, which is the fallback and has more than one layout
# --------------------------------------------------------------------------


def test_the_human_table_parses_where_parsable_output_is_unavailable(gpfs_runner):
    """The same figures, from the table this build prints for a human.

    Scoped to the one device whose human-form output the transcript holds: an
    unrecorded command raises rather than returning an empty success, which is
    the whole reason `RecordedRunner` is strict.
    """
    mounts = read_mount_table(text=DEVICE + " /home gpfs rw 0 0\n")
    snap = gpfs.GpfsBackend(prefer_parsable=False).read(
        gpfs_runner, mounts, None, ["/home/someone"]
    )
    assert snap.available
    table_home = one(snap, "home", "blocks")
    assert table_home.used == int(table_record("home")["blockusage"]) * 1024
    assert one(snap, "home", "files").soft == int(table_record("home")["filesquota"])


def test_both_measured_column_layouts_parse_the_same_way():
    """13 columns here, 11 in the vendor layout. Neither is counted.

    The header publishes the schema, which is why a layout with no `grace`
    columns and a layout with two of them both come out right. Counting
    columns would have silently shifted in_doubt into grace.
    """
    wide, wide_header, _ = gpfs.parse_table(_human_table(RecordedRunner.from_json(transcript())))
    narrow, narrow_header, _ = gpfs.parse_table(fixture("gpfs_mmlsquota_table_narrow.txt"))
    assert wide_header and narrow_header
    by_name = {r["filesetname"]: r for r in wide}
    for record in narrow:
        mate = by_name[record["filesetname"]]
        assert record["blockquota"] == mate["blockquota"]
        assert record["blocklimit"] == mate["blocklimit"]
        # The narrow layout has no grace column at all, and its trailing
        # Remarks field must not be read as one.
        assert record.get("blockgrace", "") in ("", "none")
        assert "DSS" not in record.get("blockgrace", "")


def test_the_group_preamble_is_not_read_as_a_row():
    """`-g` output opens with `Disk quotas for group hpc (gid 20008):`."""
    records, header, _ = gpfs.parse_table(fixture("gpfs_mmlsquota_group.txt"))
    assert header
    assert [r["filesetname"] for r in records] == ["home", "project-hpc"]
    assert {r["quotatype"] for r in records} == {"GRP"}


def test_no_limits_is_an_answer_not_a_parse_failure(recorded, cmd):
    """`mmlsquota -j home` prints the words `no limits` where figures go."""
    records, header, notes = gpfs.parse_table(fixture("gpfs_mmlsquota_no_limits.txt"))
    assert header
    assert records == []
    assert notes and "no limits set" in notes[0]

    runner = recorded(
        [
            cmd(
                ["/usr/lpp/mmfs/bin/mmlsquota", "-Y", "-j", "home", "fsA"],
                stdout=fixture("gpfs_mmlsquota_no_limits.txt"),
            )
        ],
        {"mmlsquota": "/usr/lpp/mmfs/bin/mmlsquota"},
    )
    snap = gpfs.read_fileset_quota(runner, None, None, "home", devices=["fsA"])
    assert snap.available is False
    assert snap.category == VerdictCategory.NO_QUOTA_ENFORCED
    assert "no limits set" in snap.reason


def test_group_scope_is_asked_with_the_group_flag(recorded, cmd, gpfs_mounts):
    runner = recorded(
        [
            cmd(
                ["/usr/lpp/mmfs/bin/mmlsquota", "-Y", "-g", "somegroup", DEVICE],
                stdout=fixture("gpfs_mmlsquota_group.txt"),
            )
        ],
        {"mmlsquota": "/usr/lpp/mmfs/bin/mmlsquota"},
    )
    snap = gpfs.read_group_quota(runner, gpfs_mounts, None, "somegroup", devices=[DEVICE])
    assert snap.available
    assert {row.scope for row in snap.rows} == {"group"}


# --------------------------------------------------------------------------
# Format details
# --------------------------------------------------------------------------


def test_parsable_field_names_are_read_case_insensitively():
    """IBM documents `filesetName`; this build prints `filesetname`.

    An exact-case lookup returns "" there, which would name every row after
    its filesystem and merge two labs' quotas under one label.
    """
    text = _parsable(RecordedRunner.from_json(transcript()))
    shouty = text.replace("filesetname:", "FileSetName:", 1)
    records, header = gpfs.parse_parsable(shouty)
    assert header
    assert records[0]["filesetname"] == "software"


def test_percent_encoded_values_are_decoded():
    """A colon inside a value is escaped, or it would split the record."""
    text = _parsable(RecordedRunner.from_json(transcript())).replace(
        ":software:", ":odd%3Aname:", 1
    )
    records, _ = gpfs.parse_parsable(text)
    assert records[0]["filesetname"] == "odd:name"


def test_a_diagnostic_cannot_veto_a_record_that_merely_mentions_it():
    """A long line is data; only a short line may veto the output."""
    text = _parsable(RecordedRunner.from_json(transcript())).replace(
        ":software:", ":command failed:", 1
    )
    assert gpfs.trouble(text) == ""
    records, header = gpfs.parse_parsable(text)
    assert header and len(records) == 8


def test_trouble_keeps_the_pointer_when_it_is_all_there_is():
    """A vetoing line still has to veto, even when it names no cause."""
    assert "Command failed" in gpfs.trouble(
        "mmlsquota: Command failed. Examine previous error messages to determine cause."
    )


# --------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------


def one(snap, fileset, kind, device=DEVICE):
    """The single row for a device, fileset and kind, asserting it is single.

    Keyed on the device as well as the name, for the reason `QuotaRow.label`
    gives: a fileset name is unique within a filesystem and not across one.
    This transcript has a `scratch` on two devices, and an earlier version of
    this helper matched on the name alone and found both.
    """
    found = [
        row
        for row in snap.rows
        if row.fileset == fileset and row.kind == kind and row.device == device
    ]
    assert len(found) == 1, "expected one %s row for %s:%s, got %d" % (
        kind,
        device,
        fileset,
        len(found),
    )
    return found[0]


def record_for(fileset, device=DEVICE):
    """One `-Y` record straight from the transcript, for deriving expectations."""
    runner = RecordedRunner.from_json(transcript())
    records, _ = gpfs.parse_parsable(
        runner.run(["/usr/lpp/mmfs/bin/mmlsquota", "-Y", device]).stdout
    )
    found = [r for r in records if r["filesetname"] == fileset]
    assert len(found) == 1
    return found[0]


def table_record(fileset, device=DEVICE):
    """One record from the human table, for deriving expectations."""
    runner = RecordedRunner.from_json(transcript())
    records, _header, _notes = gpfs.parse_table(
        runner.run(["/usr/lpp/mmfs/bin/mmlsquota", device]).stdout
    )
    found = [r for r in records if r["filesetname"] == fileset]
    assert len(found) == 1
    return found[0]


def _parsable(runner):
    return runner.run(["/usr/lpp/mmfs/bin/mmlsquota", "-Y", DEVICE]).stdout


def _human_table(runner):
    return runner.run(["/usr/lpp/mmfs/bin/mmlsquota", DEVICE]).stdout


# --------------------------------------------------------------------------
# Path-sensitive backends, found by running on a Lustre cluster
# --------------------------------------------------------------------------


def test_mmlsquota_is_not_path_sensitive_and_lustre_is():
    """One sweep before discovery is enough on GPFS and not on Lustre.

    `mmlsquota <device>` lists every fileset the reader holds usage in
    whatever path named it. A project quota is a property of the DIRECTORY:
    `lfs project -d /lus/egret` returns 0 while
    `lfs project -d /lus/egret/projects/lanternlab-exampleu` returns 13579,
    so a sweep that asked about the mount never asks for the project scope at
    all and a 29.58T allocation reported `?`.
    """
    from dirscape.quota.gpfs import GpfsBackend
    from dirscape.quota.lustre import LustreBackend
    from dirscape.quota.xfs import XfsBackend

    assert GpfsBackend().per_path is False
    assert LustreBackend().per_path is True
    assert XfsBackend().per_path is True


def test_lustre_filters_for_lustre_before_applying_its_path_cap():
    """Slicing first meant a few non-Lustre paths starved the backend.

    On a Cray login node the caller's unanswered roots begin `/`,
    `/admin_home`, `/boot`, so all the slots were spent before any Lustre
    path was reached, the target list came back empty, and the fallback asked
    about the mounts instead. The reader's 29.58T allocation therefore
    reported `?` while the backend re-read figures it already had.
    """
    from dirscape.discover.mounts import read_mount_table
    from dirscape.quota.lustre import LustreBackend

    mounts = read_mount_table(
        text="rootdev / ext4 rw 0 0\n"
        "srv:/ah /admin_home nfs rw 0 0\n"
        "egret /lus/egret lustre rw 0 0\n"
    )
    asked = ["/", "/admin_home", "/boot", "/lus/egret/projects/mine"]

    targets = LustreBackend(max_paths=3)._targets(mounts, asked)

    assert targets == ["/lus/egret/projects/mine"]


def test_the_lustre_path_cap_still_bounds_the_fan_out():
    from dirscape.discover.mounts import read_mount_table
    from dirscape.quota.lustre import LustreBackend

    mounts = read_mount_table(text="egret /lus/egret lustre rw 0 0\n")
    asked = ["/lus/egret/projects/p%d" % (i,) for i in range(20)]

    assert len(LustreBackend(max_paths=3)._targets(mounts, asked)) == 3
