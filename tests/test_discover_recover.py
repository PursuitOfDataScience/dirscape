"""Snapshot discovery: the two layouts, the three answers, and the traps.

Every test builds its own filesystem under ``tmp_path`` and describes it with
an inline mount table, so nothing here reads the cluster the suite happens to
run on. That is rapiDU's RD-10.

The three answers this module must keep apart, because conflating any two of
them is how a user loses data:

    copies found                    -> confirmed  ``✓``
    snapshot directory, nothing in it -> refuted  ``✗``   (the scratch answer)
    no snapshot directory at all    -> unknown    ``?``   (tape may still exist)
"""

import os
import time

import pytest

from dirscape.discover.mounts import read_mount_table
from dirscape.discover.recover import (
    SNAPSHOT_DIRS,
    SnapshotIndex,
    copies_for_path,
    find_snapshots,
    parse_snapshot_time,
)
from dirscape.model import Reach, Root, VerdictCategory

# --------------------------------------------------------------------------
# Worlds
# --------------------------------------------------------------------------


# The suite builds its snapshot trees under a name of its own, and that is
# forced rather than preferred. **GPFS conjures an empty, READ-ONLY
# `.snapshots` inside every directory**, including a freshly created
# `tmp_path`: `os.path.isdir(d + "/.snapshots")` is True and `os.mkdir` inside
# it raises `PermissionError`. So on the filesystem this suite actually runs
# on, a fake snapshot tree cannot be built under the real name at all, and a
# test that tried would fail for a reason that has nothing to do with the code.
# Overriding the name exercises the identical code path against a directory the
# filesystem will not intercept. `test_the_real_snapshot_names_are_the_defaults`
# pins the production list.
SNAPDIR = ".dirscape-test-snapshots"


def index_for(snapdirs=(SNAPDIR,)):
    return SnapshotIndex(snapdirs=snapdirs)


def probe(path, mounts, device="", index=None):
    """`copies_for_path`, always with the test snapshot directory name."""
    return copies_for_path(str(path), mounts, device=device, index=index or index_for())


def gpfs_world(tmp_path, snaps=("daily-2026-09-20.05h30", "daily-2026-09-21.05h30")):
    """A GPFS shape: one device, a fileset junction, snapshots at the fs ROOT.

    The layout measured on meadow3, and the one a naive implementation gets
    wrong. ``/home`` and ``/gpfs/cap`` are one device; ``/home/.snapshots``
    exists and is EMPTY, while the populated tree is at
    ``/gpfs/cap/.snapshots/<snap>/home/me`` and is keyed by the ABSOLUTE path
    rather than by anything relative to ``/home``.
    """
    fsroot = tmp_path / "gpfs" / "cap"
    home = tmp_path / "home"
    (home / "me").mkdir(parents=True, exist_ok=True)
    (home / SNAPDIR).mkdir(exist_ok=True)  # exists, empty, a decoy
    for snap in snaps:
        # Note the doubled path component: inside the snapshot the tree is
        # laid out by absolute path, so the junction name appears again.
        copy = fsroot / SNAPDIR / snap / str(home).lstrip("/") / "me"
        copy.mkdir(parents=True, exist_ok=True)
        (copy / "thesis.tex").write_text("draft\n")
    fsroot.mkdir(parents=True, exist_ok=True)

    text = "cap {home} gpfs rw 0 0\ncap {fsroot} gpfs rw 0 0\n".format(home=home, fsroot=fsroot)
    return read_mount_table(text=text), home, fsroot


def netapp_world(tmp_path, snaps=("hourly.2026-09-21_0530",)):
    """A NetApp/ZFS shape: snapshots at the mountpoint, keyed RELATIVELY."""
    mount = tmp_path / "vol"
    (mount / "me").mkdir(parents=True, exist_ok=True)
    for snap in snaps:
        copy = mount / SNAPDIR / snap / "me"
        copy.mkdir(parents=True, exist_ok=True)
        (copy / "thesis.tex").write_text("draft\n")
    text = "vol0 {mount} nfs rw 0 0\n".format(mount=mount)
    return read_mount_table(text=text), mount


def make_root(path, device="cap"):
    root = Root(str(path), device=device, fstype="gpfs")
    root.reach = Reach.LISTABLE
    return root


# --------------------------------------------------------------------------
# parse_snapshot_time
# --------------------------------------------------------------------------


def test_the_site_naming_scheme_parses_to_the_minute():
    stamp = parse_snapshot_time("daily-2026-09-20.05h30")
    assert stamp is not None
    assert time.strftime("%Y-%m-%d %H:%M", time.localtime(stamp)) == "2026-09-20 05:30"


@pytest.mark.parametrize(
    "name,expected",
    [
        ("weekly-2026-09-20.04h30", "2026-09-20 04:30"),
        ("hourly.2026.09.20-0530", "2026-09-20 05:30"),
        ("snap_20260920", "2026-09-20 00:00"),
        ("autosnap_2026-09-20_05:30:00", "2026-09-20 05:30"),
        ("@GMT-2026.09.20-05.30.00", "2026-09-20 05:30"),
    ],
)
def test_the_other_naming_schemes_in_the_wild_parse(name, expected):
    stamp = parse_snapshot_time(name)
    assert stamp is not None, name
    assert time.strftime("%Y-%m-%d %H:%M", time.localtime(stamp)) == expected


def test_an_unreadable_name_has_no_time_rather_than_a_wrong_one():
    """`None`, never a fabricated stamp and never the directory's mtime.

    The mtime of every snapshot under `/gpfs/meadow3/cap/.snapshots` reads
    `2021-08-04 05:23:07` whether it was taken this morning or a month ago,
    so a fallback to metadata would date the whole retention window to one
    day five years ago.
    """
    assert parse_snapshot_time("latest") is None
    assert parse_snapshot_time("") is None
    assert parse_snapshot_time("snap-2026-13-45") is None


# --------------------------------------------------------------------------
# The two layouts
# --------------------------------------------------------------------------


def test_the_gpfs_layout_is_found_through_the_filesystem_root(tmp_path):
    """The case a shortest-mountpoint heuristic gets exactly backwards.

    `/home` is shorter than `/gpfs/cap` and carries an EMPTY `.snapshots`, so
    picking one mountpoint per device reports "this filesystem keeps nothing"
    about a filesystem with two snapshots. Every mountpoint of the device is
    tried instead.
    """
    mounts, home, fsroot = gpfs_world(tmp_path)
    copies, verdict = probe(home / "me", mounts, device="cap")

    assert verdict.confirmed
    assert len(copies) == 2
    assert all(os.path.isdir(copy.path) for copy in copies)
    assert str(fsroot) in copies[0].path


def test_the_netapp_layout_is_found_relative_to_its_mountpoint(tmp_path):
    mounts, mount = netapp_world(tmp_path)
    copies, verdict = probe(mount / "me", mounts, device="vol0")

    assert verdict.confirmed
    assert [copy.name for copy in copies] == ["hourly.2026-09-21_0530"]
    assert copies[0].path == str(mount / SNAPDIR / "hourly.2026-09-21_0530" / "me")


def test_the_losing_layout_is_tried_once_per_snapshot_directory(tmp_path):
    """The proven layout is cached, so ten roots do not each pay for the miss."""
    mounts, home, _fsroot = gpfs_world(tmp_path)
    index = index_for()
    probe(home / "me", mounts, device="cap", index=index)

    seen = set(index._layout.values())
    assert seen == {"absolute"}


# --------------------------------------------------------------------------
# The three answers
# --------------------------------------------------------------------------


def test_an_empty_snapshot_directory_is_a_measured_no(tmp_path):
    """The scratch answer. Durable, so it may be rendered as `✗`."""
    mount = tmp_path / "scratch"
    (mount / "me").mkdir(parents=True, exist_ok=True)
    (mount / SNAPDIR).mkdir(exist_ok=True)
    mounts = read_mount_table(text="perf {m} gpfs rw 0 0\n".format(m=mount))

    copies, verdict = probe(mount / "me", mounts, device="perf")

    assert copies == []
    assert verdict.refuted
    assert verdict.durable
    assert verdict.category == VerdictCategory.NOT_PRESENT
    assert "gone" in verdict.reason


def test_no_snapshot_directory_is_unknown_and_never_a_no(tmp_path):
    """Silence means unpublished. A site can back up to tape and expose nothing."""
    mount = tmp_path / "plain"
    (mount / "me").mkdir(parents=True, exist_ok=True)
    mounts = read_mount_table(text="dev0 {m} xfs rw 0 0\n".format(m=mount))

    copies, verdict = probe(mount / "me", mounts, device="dev0")

    assert copies == []
    assert not verdict.refuted
    assert not verdict.durable
    assert verdict.category == VerdictCategory.NOT_SUPPORTED
    assert verdict.glyph() == "?"


def test_snapshots_that_predate_the_path_are_a_no_with_a_count(tmp_path):
    mounts, home, _fsroot = gpfs_world(tmp_path)
    copies, verdict = probe(home / "someone-else", mounts, device="cap")

    assert copies == []
    assert verdict.refuted
    assert "2 snapshot(s)" in verdict.reason


# --------------------------------------------------------------------------
# The point of the whole feature
# --------------------------------------------------------------------------


def test_a_path_that_no_longer_exists_still_finds_its_copies(tmp_path):
    """The entire use case. The live file is gone; the copies are the answer."""
    mounts, home, _fsroot = gpfs_world(tmp_path)
    target = home / "me" / "thesis.tex"
    assert not target.exists(), "the live file was never created in this world"

    copies, verdict = probe(target, mounts, device="cap")

    assert verdict.confirmed
    assert len(copies) == 2
    for copy in copies:
        with open(copy.path) as handle:
            assert handle.read() == "draft\n"


def test_a_deleted_file_is_found_with_no_device_supplied(tmp_path):
    """A gone path has no Root and so no device. The mount table still knows."""
    mounts, home, _fsroot = gpfs_world(tmp_path)

    copies, verdict = probe(home / "me" / "thesis.tex", mounts)

    assert verdict.confirmed
    assert len(copies) == 2


def test_copies_are_newest_first(tmp_path):
    mounts, home, _fsroot = gpfs_world(
        tmp_path, snaps=("daily-2026-09-18.05h30", "daily-2026-09-21.05h30")
    )
    copies, _verdict = probe(home / "me", mounts, device="cap")

    assert [copy.name for copy in copies] == [
        "daily-2026-09-21.05h30",
        "daily-2026-09-18.05h30",
    ]
    assert copies[0].taken_at > copies[1].taken_at


# --------------------------------------------------------------------------
# find_snapshots over a set of roots
# --------------------------------------------------------------------------


def test_find_snapshots_annotates_every_reachable_root(tmp_path):
    mounts, home, _fsroot = gpfs_world(tmp_path)
    root = make_root(home / "me")

    find_snapshots([root], mounts, index=index_for())

    assert root.recoverable.confirmed
    assert len(root.snapshots) == 2
    assert root.to_json()["snapshots"][0]["name"] == "daily-2026-09-21.05h30"


def test_an_unreachable_root_is_not_probed(tmp_path):
    """Snapshots preserve permissions, so a closed directory is closed there too.

    Spending the budget to confirm that would be a stat per snapshot per
    forbidden root, and every one of them would come back denied.
    """
    mounts, home, _fsroot = gpfs_world(tmp_path)
    root = make_root(home / "me")
    root.reach = Reach.CLOSED

    find_snapshots([root], mounts, index=index_for())

    assert root.snapshots == []
    assert root.recoverable.category == VerdictCategory.NOT_PROBED


def test_one_snapshot_directory_is_read_once_for_many_roots(tmp_path, monkeypatch):
    """Cost is per device, not per root. Ten roots, three directory reads.

    The claim is about the FILESYSTEM, so the count is taken at `_listdir`
    and not at `SnapshotIndex.names`: the method is called thirty times here
    and reads the disk three times, and it is the three that the promise
    "proportional to roots, never to files" rests on.
    """
    from dirscape.discover import recover as recover_module

    mounts, home, _fsroot = gpfs_world(tmp_path)
    roots = [make_root(home / "me") for _ in range(10)]

    reads = []
    real = recover_module._listdir

    def counted(path, deadline_s):
        reads.append(path)
        return real(path, deadline_s)

    monkeypatch.setattr(recover_module, "_listdir", counted)
    find_snapshots(roots, mounts, index=index_for())

    assert all(root.recoverable.confirmed for root in roots)
    # Two mountpoints of one device plus the root's own path, times the one
    # snapshot directory name in play, and each read taken exactly ONCE
    # however many roots wanted the answer.
    assert len(reads) == len(set(reads)) == 3


def test_a_root_with_no_path_is_skipped(tmp_path):
    """An allocation with nowhere to stand cannot be asked about snapshots."""
    mounts, _home, _fsroot = gpfs_world(tmp_path)
    root = Root("")

    find_snapshots([root], mounts, index=index_for())

    assert root.recoverable.category == VerdictCategory.NOT_PROBED


def test_an_exhausted_budget_yields_a_timeout_and_never_a_no(tmp_path):
    class Spent(object):
        exhausted = True

    mounts, home, _fsroot = gpfs_world(tmp_path)
    root = make_root(home / "me")

    find_snapshots([root], mounts, budget=Spent(), index=index_for())

    assert not root.recoverable.durable
    assert root.recoverable.category == VerdictCategory.PROBE_TIMEOUT


# --------------------------------------------------------------------------
# The inode trap
# --------------------------------------------------------------------------


def test_a_copy_is_never_offered_as_a_root(tmp_path):
    """Rule 1, as a type assertion rather than as a comment.

    Measured on GPFS: the live `/home/jdoe42` and its copies in three
    different snapshots all report `dev=54 ino=212501245`. `candidates._dedupe`
    keys on exactly that pair, so a snapshot added to the candidate basket is
    folded onto the live path and disappears. Keeping copies off the `Root`
    type is what makes that impossible rather than merely avoided.
    """
    from dirscape.model import SnapshotCopy

    mounts, home, _fsroot = gpfs_world(tmp_path)
    root = make_root(home / "me")
    find_snapshots([root], mounts, index=index_for())

    assert all(isinstance(copy, SnapshotCopy) for copy in root.snapshots)
    assert not any(isinstance(copy, Root) for copy in root.snapshots)


def test_the_real_snapshot_names_are_the_defaults():
    """The production list, pinned, since every other test overrides it.

    Four conventions, and this site needs three of them at once: GPFS
    `.snapshots` for meadow and collie3, `.zfs/snapshot` for the /cfs and
    /cfs2 tiers, `.snap` for /cfs3.
    """
    assert SNAPSHOT_DIRS == (".snapshots", ".snapshot", ".zfs/snapshot", ".snap")
    assert SnapshotIndex().snapdirs == SNAPSHOT_DIRS


# --------------------------------------------------------------------------
# Site-declared snapshot roots: the login-node route
# --------------------------------------------------------------------------


def published_world(tmp_path, shape, snaps=("daily-2026-09-21.05h30", "daily-2026-09-22.05h30")):
    """A `/snapshots` tree in one of the two shapes this site publishes.

    Neither is reachable from the mount table: `/snapshots` is a plain
    top-level directory belonging to no device, so `_bases_for` can never
    arrive at it and it has to be declared.
    """
    home = tmp_path / "home" / "me"
    home.mkdir(parents=True, exist_ok=True)
    published = tmp_path / "snapshots"
    for snap in snaps:
        if shape == "direct":  # meadow3: /snapshots/<SNAP>/home/me
            leaf = published / snap / str(home).lstrip("/")
        else:  # meadow2: /snapshots/home/<SNAP>/home/me
            leaf = published / "home" / snap / str(home).lstrip("/")
        leaf.mkdir(parents=True, exist_ok=True)
        (leaf / "thesis.tex").write_text("draft\n")
    mounts = read_mount_table(text="cap {h} gpfs rw 0 0\n".format(h=tmp_path / "home"))
    return mounts, home, published


@pytest.mark.parametrize("shape", ["direct", "per-filesystem"])
def test_a_published_snapshot_root_is_read_in_either_shape(tmp_path, shape):
    """Told where the tree is, not what shape it has.

    Meadow3 puts the snapshots straight under `/snapshots`; meadow2 puts one
    directory per filesystem in between. `containers()` tells them apart by
    asking whether the entry names parse as snapshot names, so a site states
    the top of the tree and nothing else.
    """
    mounts, home, published = published_world(tmp_path, shape)

    copies, verdict = copies_for_path(
        str(home / "thesis.tex"),
        mounts,
        index=SnapshotIndex(snapdirs=(), roots=(str(published),)),
    )

    assert verdict.confirmed
    assert len(copies) == 2
    assert copies[0].name == "daily-2026-09-22.05h30"
    with open(copies[0].path) as handle:
        assert handle.read() == "draft\n"


def test_a_published_root_pointed_somewhere_useless_costs_one_listing(tmp_path):
    """A misconfigured root yields nothing and must not turn into a sweep."""
    empty = tmp_path / "nothing"
    (empty / "a" / "b" / "c").mkdir(parents=True)

    index = SnapshotIndex(snapdirs=(), roots=(str(empty),))

    # One level down and no further: `a` is offered as a container, `a/b` is
    # never opened.
    assert index.containers() == [str(empty / "a")]


def test_a_published_root_that_does_not_exist_is_silent(tmp_path):
    index = SnapshotIndex(snapdirs=(), roots=(str(tmp_path / "absent"),))
    assert index.containers() == []


def test_a_published_root_appears_in_the_full_listing_and_never_the_table(tmp_path):
    """Reported, never recommended.

    It is reachable storage a reader can copy out of, so leaving it off
    `--all` is a lie by omission; it is read-only and holds no allocation, so
    a row in the default table would break that view's one promise.
    """
    from dirscape.discover.candidates import (
        RANK_SECONDARY,
        SOURCE_SNAPSHOT_ROOT,
        _Basket,
        _from_snapshot_roots,
    )
    from dirscape.sitecfg import Site

    published = tmp_path / "snapshots"
    published.mkdir()
    site = Site()
    site.snapshot_roots = [str(published), str(tmp_path / "absent")]

    basket = _Basket()
    _from_snapshot_roots(basket, site, 1.0, None)
    found = list(basket)

    assert [c.path for c in found] == [str(published)]
    assert SOURCE_SNAPSHOT_ROOT in found[0].sources
    assert found[0].rank == RANK_SECONDARY


def test_one_snapshot_is_one_row_however_many_routes_reach_it(tmp_path):
    """NetApp exposes `.snapshot` inside EVERY directory.

    So a path under a NetApp mount is found twice, once from the mountpoint
    and once from its own base. Measured on an ACME login node, where
    `dirscape recover /soft/applications` reported "6 copies" that were three
    snapshots listed twice each.
    """
    mount = tmp_path / "soft"
    target = mount / "applications"
    target.mkdir(parents=True)
    for snap in ("daily.2026-09-22_0010", "hourly.2026-09-22_1705"):
        (mount / SNAPDIR / snap / "applications").mkdir(parents=True)
        (target / SNAPDIR / snap).mkdir(parents=True)
    mounts = read_mount_table(text="srv:/soft {m} nfs rw 0 0\n".format(m=mount))

    copies, verdict = probe(target, mounts, device="srv:/soft")

    assert verdict.confirmed
    assert [copy.name for copy in copies] == [
        "hourly.2026-09-22_1705",
        "daily.2026-09-22_0010",
    ]


def test_deduplication_is_by_name_and_not_by_inode(tmp_path):
    """The obvious key is the wrong one.

    GPFS gives every snapshot of a directory the SAME inode as the live path,
    measured as `dev=54 ino=212501245` across three dates, so an inode key
    would collapse eleven genuinely different snapshots into one. Reproduced
    here with hard links, which is the only way to make one inode appear at
    several paths on an ordinary filesystem.
    """
    mounts, home, fsroot = gpfs_world(
        tmp_path, snaps=("daily-2026-09-20.05h30", "daily-2026-09-21.05h30")
    )
    target = home / "me" / "thesis.tex"
    leaves = [
        fsroot / SNAPDIR / snap / str(home).lstrip("/") / "me" / "thesis.tex"
        for snap in ("daily-2026-09-20.05h30", "daily-2026-09-21.05h30")
    ]
    # `gpfs_world` already wrote a copy into each snapshot; replace the
    # second with a hard link to the first so one inode sits at both dates.
    leaves[0].write_text("draft\n")
    os.unlink(str(leaves[1]))
    os.link(str(leaves[0]), str(leaves[1]))
    assert os.stat(str(leaves[0])).st_ino == os.stat(str(leaves[1])).st_ino

    copies, verdict = probe(target, mounts, device="cap")

    assert verdict.confirmed
    assert [copy.name for copy in copies] == [
        "daily-2026-09-21.05h30",
        "daily-2026-09-20.05h30",
    ], "one inode, two dates, two rows"
