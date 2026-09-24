"""Defects that only a second, third and fourth cluster could show.

The package was written on meadow3 and then run, unmodified, on ACME Procyon
(SLES 15, Python 3.6, PBS), ACME Sylvia (RHEL 9, Python 3.9, PBS) and HPC
meadow2 (RHEL 7, Python 3.6). Every test here pins something that went wrong
on one of those, and the mount lines are verbatim `/proc/self/mounts` from the
node that produced them, trimmed of options that do not matter.
"""

import os
import re

import pytest

from dirscape.discover import candidates as candidates_module
from dirscape.discover.candidates import (
    RANK_SECONDARY,
    _default_groups,
    _rank_of_mount,
    discover,
    inside_snapshot_tree,
)
from dirscape.discover.identity import Identity, cluster_fingerprint
from dirscape.discover.mounts import (
    inside_snapshot_tree as mounts_inside_snapshot_tree,
)
from dirscape.discover.mounts import (
    lustre_filesystem,
    node_class,
    read_mount_table,
)
from dirscape.discover.recover import SNAPSHOT_DIRS
from dirscape.runner import Runner
from dirscape.sitecfg import Site, guess_cluster_name

ACORN = (
    "192.0.2.185@o2ib26,192.0.2.190@o2ib26:192.0.2.187@o2ib26,192.0.2.192@o2ib26:"
    "192.0.2.186@o2ib26,192.0.2.191@o2ib26:192.0.2.188@o2ib26,192.0.2.193@o2ib26"
)
EGRET = "192.0.2.102@o2ib22:192.0.2.103@o2ib22"

PROCYON = (
    "\n".join(
        [
            "/dev/loop0 /root_ro squashfs ro,relatime 0 0",
            "tmpfs /rootfs.rw tmpfs rw,relatime 0 0",
            "overlay / overlay rw,relatime,lowerdir=/root_ro,upperdir=/rootfs.rw/upperdir 0 0",
            "tmpfs /tmp tmpfs rw,relatime 0 0",
            "filer-02-procyon.site.example.org:/soft_procyon /soft nfs ro,noatime,vers=3 0 0",
            "filer-02-procyon.site.example.org:/procyon_admin_home /admin_home "
            "nfs4 rw,vers=4.2 0 0",
            "%s:/acorn/home /home lustre rw,flock,user_xattr 0 0" % (ACORN,),
            "%s:/egret/clone/g2 /lus/grove lustre rw,flock 0 0" % (EGRET,),
            "%s:/egret /lus/egret lustre rw,flock 0 0" % (EGRET,),
            "filer-02-procyon.site.example.org:/soft_procyon/.snapshot/snap.2026-01-07_150846 "
            "/soft/.snapshot/snap.2026-01-07_150846 nfs ro,noatime,vers=3 0 0",
            "filer-02-procyon.site.example.org:"
            "/procyon_admin_home/.snapshot/hourly.2026-09-22_1305 "
            "/admin_home/.snapshot/hourly.2026-09-22_1305 nfs4 rw,vers=4.2 0 0",
        ]
    )
    + "\n"
)

#: The same node an hour later: the automounter has let one snapshot go,
#: mounted two new ones (one for another person's admin home), and nothing
#: about the cluster has changed.
PROCYON_LATER = PROCYON.replace(
    "/admin_home/.snapshot/hourly.2026-09-22_1305 nfs4",
    "/admin_home/.snapshot/hourly.2026-09-22_1405 nfs4",
).replace("hourly.2026-09-22_1305", "hourly.2026-09-22_1405") + (
    "filer-02-procyon.site.example.org:"
    "/procyon_admin_home/.snapshot/daily.2026-09-21_0010/person2 "
    "/admin_home/person2/.snapshot/daily.2026-09-21_0010 nfs4 rw,vers=4.2 0 0\n"
    "filer-02-procyon.site.example.org:/procyon_admin_home/somebody "
    "/admin_home/somebody nfs4 rw 0 0\n"
)

SYLVIA_EXTRA = "%s:/acorn /lus/acorn lustre rw,flock,user_xattr 0 0\n" % (ACORN,)


# --------------------------------------------------------------------------
# The mount table
# --------------------------------------------------------------------------


def test_a_lustre_subdirectory_mount_is_the_filesystem_it_came_from():
    assert lustre_filesystem(ACORN + ":/acorn/home") == ACORN + ":/acorn"
    assert lustre_filesystem(EGRET + ":/egret/clone/g2/") == EGRET + ":/egret"
    assert lustre_filesystem(EGRET + ":/egret") == EGRET + ":/egret"
    assert lustre_filesystem("meadow3_cap") == "meadow3_cap", "a GPFS name is left alone"

    table = read_mount_table(text=PROCYON + SYLVIA_EXTRA)
    home, acorn = table.at("/home")[0], table.at("/lus/acorn")[0]
    assert home.device != acorn.device
    assert home.filesystem == acorn.filesystem


def test_the_snapshot_tree_test_lives_with_the_mount_table():
    """Moved so the cluster key can use it without importing the recovery layer."""
    assert mounts_inside_snapshot_tree is inside_snapshot_tree
    assert SNAPSHOT_DIRS == (".snapshots", ".snapshot", ".zfs/snapshot", ".snap")


def test_the_fabric_ignores_snapshot_mounts_and_automounted_exports():
    """Measured: eight state files in four minutes from six runs on ACME.

    The raw network-device list changed between runs a minute apart, because
    NetApp mounts every snapshot a reader touches and `dirscape recover`
    touches them, so the tool moved its own cluster key by running.
    """
    now, later = read_mount_table(text=PROCYON), read_mount_table(text=PROCYON_LATER)
    assert now.network_devices() != later.network_devices(), "the premise: the raw list moves"
    assert now.fabric() == later.fabric()
    assert cluster_fingerprint(now) == cluster_fingerprint(later)
    fabric = now.fabric()
    assert "nfs filer-02-procyon.site.example.org" in fabric, "an NFS export is its server"
    assert "lustre %s:/egret" % (EGRET,) in fabric
    assert not any(".snapshot" in name for name in fabric)


def test_two_clusters_sharing_storage_still_get_two_keys():
    """Procyon and Sylvia share /home and /lus/egret and nothing else."""
    procyon = read_mount_table(text=PROCYON)
    sylvia = read_mount_table(
        text=PROCYON.replace("filer-02-procyon", "filer-01-softserv") + SYLVIA_EXTRA
    )
    assert cluster_fingerprint(procyon) != cluster_fingerprint(sylvia)


def test_a_gpfs_cluster_keeps_the_key_it_always_had():
    """The fabric is the device list on GPFS, so no existing baseline is orphaned."""
    text = "meadow3_cap /home gpfs rw 0 0\nmeadow3_perf /scratch/meadow3 gpfs rw 0 0\n"
    table = read_mount_table(text=text)
    assert table.fabric() == table.network_devices()


@pytest.mark.parametrize(
    "env, host, expected",
    [
        ({"PBS_JOBID": "1937870.procyon-pbs-01"}, "x1234c0s13b0n0", "compute"),
        ({"LSB_JOBID": "42"}, "anything", "compute"),
        ({"FLUX_JOB_ID": "f2a"}, "anything", "compute"),
        ({}, "x1234c0s13b0n0", "compute"),
        ({}, "sylvia-gpu-07", "compute"),
        ({}, "procyon-login-02", "login"),
        ({}, "collie3-bigmem1", "compute"),
        ({}, "dtn-data", "unknown"),
    ],
)
def test_the_node_class_knows_more_than_slurm(env, host, expected):
    """PBS on ACME sets `PBS_JOBID` and no `SLURM_*`, so every job was unknown."""
    assert node_class(env, host) == expected


@pytest.mark.parametrize(
    "host, devices, expected",
    [
        ("procyon-login-02", (), "procyon"),
        ("sylvia-login-02", (), "sylvia"),
        ("sylvia-gpu-07", (), "sylvia"),
        ("meadow3-0200", (), "meadow3"),
        ("meadow2-login2.hpc.local", (), "meadow2"),
        ("login01.frontera.example.edu", (), "frontera"),
        ("x1234c0s13b0n0", (), ""),
        ("h", ("meadow3_cap", "meadow3_perf"), "meadow3"),
        # Lustre and NFS devices lead with addresses, which have no name in them.
        ("procyon-login-02", ("lustre %s:/egret" % (EGRET,), "nfs 203.0.113.71"), "procyon"),
    ],
)
def test_the_cluster_name_is_the_machine_not_the_node(host, devices, expected):
    assert guess_cluster_name(host, devices) == expected


# --------------------------------------------------------------------------
# Discovery
# --------------------------------------------------------------------------


def test_a_default_group_owns_everybody_and_so_proves_nothing():
    """ACME: every account's primary group is `users`, and `/admin_home` is
    one directory per person, each group-owned by it."""
    me = 1000
    admin_homes = [("p%d" % i, "/admin_home/p%d" % i, 100, 5000 + i) for i in range(6)]
    allocations = [
        ("hpc", "/project/hpc", 20011, 0),
        ("hpc-staff", "/project2/hpc", 20011, 0),
        ("mine", "/project/mine", 100, me),
    ]
    flagged = _default_groups(admin_homes + allocations, me)
    assert flagged == frozenset([100])
    assert _default_groups(allocations, me) == frozenset(), "root-owned grants never trip it"


def _site_for(tmp_path):
    """Roles said explicitly, so they cannot depend on where `tmp_path` is.

    Under `/tmp`, as pytest puts it on an ACME login node, the heuristics call
    every fake mount `local`, and discovery rightly never scans those.
    """
    site = Site()
    site.role_globs = [(str(tmp_path) + "/*", "project")]
    return site


class _NoCommands(Runner):
    def available(self, name, extra_dirs=()):
        raise AssertionError("discovery must not probe for %r" % (name,))

    def run(self, argv, timeout=None, env=None):
        raise AssertionError("discovery must not run %r" % (argv,))


def _identity(gids, groups=("users",)):
    return Identity(
        uid=os.getuid(),
        gid=gids[0],
        user="me",
        gids=list(gids),
        groups=list(groups),
        hostname="test",
        node_class_="login",
        fingerprint="f",
        cluster="c",
    )


def test_dir_owner_keeps_my_own_directory_under_a_default_group(tmp_path, monkeypatch):
    shared = tmp_path / "admin_home"
    shared.mkdir()
    mine = shared / "me"
    mine.mkdir()
    text = "srv:/admin_home %s nfs4 rw 0 0\n" % (shared,)

    real = candidates_module._scandir_one_level

    def listing(path, deadline_s, cap=candidates_module.SCANDIR_CAP):
        if path != str(shared):
            return real(path, deadline_s, cap)
        others = [("p%d" % i, str(shared / ("p%d" % i)), 100, True, 5000 + i) for i in range(6)]
        return others + [("me", str(mine), 100, True, os.getuid())]

    monkeypatch.setattr(candidates_module, "_scandir_one_level", listing)
    roots = discover(
        _NoCommands(),
        read_mount_table(text=text),
        _identity([100]),
        None,
        _site_for(tmp_path),
        env={},
    )
    paths = {root.path for root in roots}
    assert str(mine) in paths
    assert not any(path.startswith(str(shared) + "/p") for path in paths)


def test_a_mounted_snapshot_is_not_a_root(tmp_path):
    """Procyon: 30 of 49 `--all` rows were `/admin_home/<person>/.snapshot/...`."""
    live = tmp_path / "soft"
    frozen = live / ".snapshot" / "hourly.2026-09-22_1305"
    frozen.mkdir(parents=True)
    text = "srv:/soft %s nfs ro 0 0\nsrv:/soft/.snapshot/hourly %s nfs ro 0 0\n" % (live, frozen)
    roots = discover(
        _NoCommands(), read_mount_table(text=text), _identity([4242]), None, Site(), env={}
    )
    assert {root.path for root in roots} == {str(live)}


def test_the_reason_a_system_image_is_held_back_is_true():
    """It read "a overlay memory filesystem" about a Cray login node's `/`."""
    table = read_mount_table(text=PROCYON)
    _rank, reason = _rank_of_mount(table.at("/")[0], table, Site())
    assert reason.startswith("an overlay on the node's system image")
    _rank, reason = _rank_of_mount(table.at("/root_ro")[0], table, Site())
    assert reason.startswith("a read-only squashfs image")


def test_the_filesystem_root_of_a_lustre_home_is_held_back():
    """Sylvia's `/lus/acorn` repeated the home's `36G of 342G` in a second row."""
    table = read_mount_table(text=PROCYON + SYLVIA_EXTRA)
    rank, reason = _rank_of_mount(table.at("/lus/acorn")[0], table, Site())
    assert rank == RANK_SECONDARY
    assert "the Lustre filesystem acorn, which is also mounted at /home" in reason
    rank, _reason = _rank_of_mount(table.at("/lus/grove")[0], table, Site())
    assert rank != RANK_SECONDARY, "a sibling at the same depth is not plumbing"


def test_a_directory_inside_a_memory_filesystem_is_held_back(tmp_path):
    """meadow2 login nodes are diskless: `/tmp` is a directory in a tmpfs `/`."""
    scratch = tmp_path / "tmp"
    scratch.mkdir()
    text = "rootfs %s tmpfs rw 0 0\nsrv:/home %s nfs rw 0 0\n" % (tmp_path, tmp_path / "home")
    roots = discover(
        _NoCommands(),
        read_mount_table(text=text),
        _identity([4242]),
        None,
        Site(),
        env={"TMPDIR": str(scratch)},
    )
    found = {root.path: root for root in roots}[str(scratch)]
    assert found.policy["rank"] == RANK_SECONDARY
    assert "inside a tmpfs memory filesystem" in found.policy["rank_reason"]
    assert found.policy.get("node_local") is True


def test_node_local_is_recorded_for_local_disks_only(tmp_path):
    disk, net = tmp_path / "scratch", tmp_path / "home"
    disk.mkdir()
    net.mkdir()
    text = "/dev/sda1 %s xfs rw,noquota 0 0\nsrv:/home %s nfs rw 0 0\n" % (disk, net)
    roots = {
        r.path: r
        for r in discover(
            _NoCommands(), read_mount_table(text=text), _identity([1]), None, Site(), env={}
        )
    }
    assert roots[str(disk)].policy.get("node_local") is True
    assert "node_local" not in roots[str(net)].policy


# --------------------------------------------------------------------------
# Quota attribution and the views, through `cli`
# --------------------------------------------------------------------------

from dirscape import cli  # noqa: E402
from dirscape.discover.attribute import attribute_xfs  # noqa: E402
from dirscape.model import (  # noqa: E402
    QuotaRow,
    QuotaSnapshot,
    Reach,
    Root,
    SnapshotCopy,
    VerdictCategory,
    confirmed,
    refuted,
    unknown,
)
from dirscape.render import fields as render_fields  # noqa: E402
from dirscape.render.style import Style  # noqa: E402


def _root(path, fileset="", device="dev", writable=False, fstype="lustre"):
    root = Root(path, role="project", device=device, fstype=fstype)
    root.fileset = fileset
    root.reach = Reach.LISTABLE
    root.present = confirmed()
    root.mounted = confirmed()
    root.writable = confirmed() if writable else refuted(VerdictCategory.ACCESS_DENIED, "no")
    return root


def test_a_filesystem_wide_figure_stays_off_a_directory_that_is_not_yours():
    """Procyon showed `/lus/grove read only 4.1T none 216k` in the default table.

    That is `lfs quota -u` asked at the mount: the reader's usage across all of
    Egret, printed against a read-only clone that holds nothing of theirs.
    """
    user = QuotaRow(
        "", "blocks", "user", 4480696553472, soft=0, hard=0, mount="/lus/grove", device="/lus/grove"
    )
    snap = QuotaSnapshot("lfs quota", [user])
    grove = _root("/lus/grove")
    assert cli._rows_governing(snap, grove) == []


def test_the_same_figure_still_lands_on_the_readers_own_home(tmp_path):
    home = tmp_path / "home" / "me"
    home.mkdir(parents=True)
    user = QuotaRow(
        "",
        "blocks",
        "user",
        38311297024,
        soft=367460281344,
        hard=400865761280,
        mount=str(tmp_path / "home"),
        device=str(tmp_path / "home"),
    )
    snap = QuotaSnapshot("lfs quota", [user])
    mine = _root(str(home), writable=True)
    assert cli._rows_governing(snap, mine) == [user]
    assert cli._rows_governing(snap, _root(str(tmp_path / "home"))) == [], "not on /home itself"


def test_a_fileset_is_reachable_when_its_published_row_names_a_reachable_root():
    """The project row is filed under `lanternlab-exampleu`; the root is `13579`."""
    run = cli.Run()
    project = _root("/lus/egret/projects/lanternlab-exampleu", fileset="13579", writable=True)
    run.roots = [project]
    row = QuotaRow("lanternlab-exampleu", "blocks", "project", 1, mount=project.path)
    run.quota_attempts = [QuotaSnapshot("lfs quota", [row])]
    assert cli._mark_stranded(run) == 0
    assert not any("holds space in" in warning for warning in run.warnings)


def test_nothing_is_placed_on_a_root_nobody_probed():
    """meadow2, budget spent: `/project 851M 30G` was another cluster's home quota."""
    run = cli.Run()
    unprobed = Root("/project", role="project", device="meadow3_cap", fstype="gpfs")
    unprobed.present = unknown(VerdictCategory.NOT_PROBED, "time budget exhausted")
    run.roots = [unprobed]
    row = QuotaRow(
        "home", "blocks", "user", 891912192, hard=32212254720, mount="/project", guessed=True
    )
    run.quota_attempts = [QuotaSnapshot("mmlsquota", [row])]
    cli._place_rows(run, {}, {}, {}, {})
    assert unprobed.quota is None


def test_why_follows_a_symlink_to_the_root_that_governs_it(tmp_path):
    """ACME documents `/egret/<project>`; `/egret` links to `/lus/egret/projects`.

    Matched as typed, `why /egret/lanternlab-exampleu` walked up to `/` and
    explained the node's system image instead of the reader's 30T project.
    """
    real = tmp_path / "lus" / "egret" / "projects" / "proj"
    real.mkdir(parents=True)
    link = tmp_path / "egret"
    link.symlink_to(tmp_path / "lus" / "egret" / "projects")
    run = cli.Run()
    run.mounts = read_mount_table(
        text="%s:/egret %s lustre rw 0 0\n" % (EGRET, tmp_path / "lus" / "egret")
    )
    project = _root(str(real), writable=True)
    top = _root("/", fstype="overlay")
    run.roots = [top, project]
    text, code = cli._why(run, str(link / "proj"), Style(color=False))
    assert code == cli.EXIT_OK
    assert text.splitlines()[0] == str(real)
    assert "leads here" in text


def test_why_refuses_to_answer_for_a_path_on_a_mount_it_does_not_report(tmp_path):
    """`why /dev/shm` explained `/`, which is a different filesystem."""
    shm = tmp_path / "shm"
    shm.mkdir()
    run = cli.Run()
    run.mounts = read_mount_table(
        text="/dev/sda1 %s ext4 rw 0 0\ntmpfs %s tmpfs rw 0 0\n" % (tmp_path, shm)
    )
    run.roots = [_root(str(tmp_path), fstype="ext4")]
    text, code = cli._why(run, str(shm), Style(color=False))
    assert code == cli.EXIT_PATH
    assert "tmpfs mount at %s" % (shm,) in text


def test_why_points_a_path_inside_a_snapshot_at_recover(tmp_path):
    frozen = tmp_path / "soft" / ".snapshot" / "daily"
    frozen.mkdir(parents=True)
    run = cli.Run()
    run.mounts = read_mount_table(
        text="srv:/soft %s nfs ro 0 0\nsrv:/soft/.snapshot/daily %s nfs ro 0 0\n"
        % (tmp_path / "soft", frozen)
    )
    run.roots = [_root(str(tmp_path / "soft"), fstype="nfs")]
    text, code = cli._why(run, str(frozen), Style(color=False))
    assert code == cli.EXIT_PATH
    assert "read-only snapshot" in text and "dirscape recover" in text


def _recover_run(tmp_path, snap_dir):
    run = cli.Run()
    run.mounts = read_mount_table(text="/dev/sda1 %s xfs rw 0 0\n" % (tmp_path,))
    run.site = Site()
    return run


def test_recover_never_nests_a_restored_directory_or_clobbers_newer_files(tmp_path, monkeypatch):
    """`cp -a SNAP/d live/d` with `live/d` present makes `live/d/d`: measured."""
    live = tmp_path / "live" / "d"
    live.mkdir(parents=True)
    copy = SnapshotCopy("daily", str(tmp_path / "snap" / "d"), None)
    monkeypatch.setattr(cli, "copies_for_path", lambda *a, **k: ([copy], confirmed("1 copy")))
    text, _code = cli._recover(_recover_run(tmp_path, None), str(live), Style(color=False))
    assert "cp -an %s/. %s/" % (copy.path, live) in text


def test_recover_suggests_copying_out_of_a_read_only_tree(tmp_path, monkeypatch):
    if os.geteuid() == 0:
        pytest.skip("root can write anywhere")
    locked = tmp_path / "soft"
    locked.mkdir()
    target = locked / "applications"
    target.mkdir()
    locked.chmod(0o555)
    target.chmod(0o555)
    copy = SnapshotCopy("snap", str(tmp_path / ".snapshot" / "applications"), None)
    monkeypatch.setattr(cli, "copies_for_path", lambda *a, **k: ([copy], confirmed("1 copy")))
    try:
        text, _code = cli._recover(_recover_run(tmp_path, None), str(target), Style(color=False))
    finally:
        target.chmod(0o755)
        locked.chmod(0o755)
    assert "read-only to you" in text
    # `-n`: copying out must not replace a file of the same name where they are.
    assert "cp -an %s ." % (copy.path,) in text


def _restore_command(tmp_path, monkeypatch, target, copy):
    """What `recover` prints, and what `recover_path` hands an agent to run."""
    from dirscape import agent

    found = lambda *a, **k: ([copy], confirmed("1 copy"))  # noqa: E731
    monkeypatch.setattr(cli, "copies_for_path", found)
    monkeypatch.setattr(agent, "copies_for_path", found)
    text, _code = cli._recover(_recover_run(tmp_path, None), str(target), Style(color=False))
    record = agent.recover_payload(_recover_run(tmp_path, None), str(target))
    assert record["restore"] in text, "the person and the agent are told the same thing"
    assert re.search(r"\bcp -a(?!n)", record["restore"]) is None, "every form is no-clobber"
    return text, record["restore"]


def test_recover_never_overwrites_a_file_that_is_still_there(tmp_path, monkeypatch):
    """`recover README.md` printed `cp -a SNAP/README.md README.md`: measured.

    Run as given, that replaces the live file with the older copy, and the
    MCP `recover_path` tool returned the same line in `restore` for an agent
    to run. A file that is still there is restored beside itself.
    """
    live = tmp_path / "live" / "README.md"
    live.parent.mkdir(parents=True)
    live.write_text("edited since the snapshot\n")
    copy = SnapshotCopy("daily-2026-09-23", str(tmp_path / "snap" / "README.md"), None)

    text, command = _restore_command(tmp_path, monkeypatch, live, copy)

    assert command == "cp -an %s %s.daily-2026-09-23" % (copy.path, live)
    assert "beside it" in text


def test_recover_puts_a_deleted_file_back_where_it_was(tmp_path, monkeypatch):
    target = tmp_path / "results.csv"
    copy = SnapshotCopy("daily", str(tmp_path / "snap" / "results.csv"), None)

    _text, command = _restore_command(tmp_path, monkeypatch, target, copy)

    assert command == "cp -an %s %s" % (copy.path, target)


def test_recover_recreates_a_deleted_tree_instead_of_calling_it_read_only(tmp_path, monkeypatch):
    """After `rm -rf proj`, the file's directory is gone as well.

    `os.access` is False for a directory that does not exist, so this said
    "is read-only to you" about a directory nobody can see any more.
    """
    target = tmp_path / "proj" / "data" / "results.csv"
    copy = SnapshotCopy("daily", str(tmp_path / "snap" / "proj" / "data" / "results.csv"), None)

    text, command = _restore_command(tmp_path, monkeypatch, target, copy)

    assert "read-only" not in text
    assert command == "mkdir -p %s && cp -an %s %s" % (target.parent, copy.path, target)


def test_a_shared_drop_is_walked_for_the_readers_files_only(tmp_path):
    """Sylvia's `/tmp`: 2.0M entries from every account, 159 of them yours."""
    drop = tmp_path / "tmp"
    drop.mkdir()
    (drop / "mine").write_bytes(b"x" * 4096)
    (drop / "sub").mkdir()
    (drop / "sub" / "also-mine").write_bytes(b"y" * 4096)
    drop.chmod(0o777)
    assert cli._shared_by_everyone(str(drop)), "world-writable, sticky or not"
    total, files, complete = cli._walk(str(drop), float("inf"), 1000, owner=os.getuid())
    assert (files, complete) == (2, True)
    total, files, complete = cli._walk(str(drop), float("inf"), 1000, owner=os.getuid() + 1)
    assert (total, files) == (0, 0), "somebody else's files are not counted and not entered"
    (drop).chmod(0o755)
    assert not cli._shared_by_everyone(str(drop))


def test_the_walk_leaves_somebody_elses_group_tree_alone(tmp_path):
    """meadow2: 2.5s spent failing to add up a 149G fileset and a 155T tree."""
    group = tmp_path / "group"
    group.mkdir()
    run = cli.Run()
    theirs = _root(str(group), writable=True, fstype="gpfs")
    theirs.quota = None
    run.roots = [theirs]
    real = cli._owned_by_caller
    try:
        cli._owned_by_caller = lambda path: False
        cli._measure(run)
    finally:
        cli._owned_by_caller = real
    assert theirs.quota is None
    assert any("not added up" in note for note in theirs.notes)


def test_a_why_view_says_copy_in_the_singular(tmp_path):
    root = _root(str(tmp_path), writable=True)
    root.snapshots = [SnapshotCopy("daily", str(tmp_path / ".snap" / "daily"), None)]
    root.recoverable = confirmed("1 snapshot copy")
    assert cli._snapshot_phrase(root).startswith("1 copy kept")


def test_the_source_line_names_a_lustre_figure_for_what_it_is():
    home = _root("/home/me", writable=True)
    home.quota = QuotaSnapshot(
        "lfs quota", [QuotaRow("", "blocks", "user", 1, hard=2, mount="/home", device="/home")]
    )
    assert cli._quota_source(home) == "your user quota on the filesystem at /home"
    project = _root("/lus/egret/projects/p", fileset="13579", writable=True)
    project.quota = QuotaSnapshot(
        "lfs quota",
        [
            QuotaRow(
                "13579", "blocks", "project", 1, hard=2, mount=project.path, device=project.path
            )
        ],
    )
    assert cli._quota_source(project).startswith("project 13579 on ")


def test_an_ext4_mount_with_no_quota_option_enforces_no_limit(tmp_path):
    """Sylvia's `/tmp` is `ext4 rw,relatime,stripe=64`: the limit cell read `?`."""
    table = read_mount_table(
        text="/dev/mapper/system-tmp %s ext4 rw,relatime,stripe=64 0 0\n" % (tmp_path,)
    )
    root = _root(str(tmp_path), fstype="ext4")
    attribute_xfs(root, _NoCommands(), table)
    assert root.policy.get("no_quota_enforced") is True
    quoted = read_mount_table(text="/dev/sdb1 %s ext4 rw,usrquota 0 0\n" % (tmp_path,))
    other = _root(str(tmp_path), fstype="ext4")

    class NoXfsIo(_NoCommands):
        def available(self, name, extra_dirs=()):
            return None

    attribute_xfs(other, NoXfsIo(), quoted)
    assert "no_quota_enforced" not in other.policy
    assert any("ext4 project id" in note for note in other.notes), "not `XFS` on ext4"


def test_the_listing_says_how_many_entries_it_left_out(tmp_path):
    """Sylvia's `/tmp` listing read `1 of 200`, as if 200 were all of 10,401."""
    kids = [
        {
            "name": "d%03d" % i,
            "path": str(tmp_path / ("d%03d" % i)),
            "items": 0,
            "readable": True,
            "writable": True,
            "enterable": True,
        }
        for i in range(200)
    ]
    lines, _kids, _band = cli._listing(
        str(tmp_path), Style(color=False), cols=80, window=20, kids=kids, held=10201
    )
    assert any("10k more not listed" in line or "10.2k more not listed" in line for line in lines)


def test_a_warning_the_view_already_printed_is_not_printed_again(tmp_path, monkeypatch, capsys):
    run = cli.Run()
    run.roots = [_root(str(tmp_path), writable=True)]
    run.warnings = [
        "the baseline was taken on node-a, so 1 difference(s) on this node's own disks "
        "were left out: they are different disks"
    ]
    monkeypatch.setattr(cli, "sweep", lambda opts, runner=None: run)
    monkeypatch.setattr(
        cli,
        "_render",
        lambda run, opts, command, style, width: ("No change.\n\n  ! " + run.warnings[0], 0),
    )
    cli.main(["new", "--color", "never"])
    captured = capsys.readouterr()
    assert captured.out.count("different disks") == 1
    assert "different disks" not in captured.err


# --------------------------------------------------------------------------
# Run to run, across login nodes
# --------------------------------------------------------------------------

from dirscape.discover.recover import SnapshotIndex, copies_for_path  # noqa: E402
from dirscape.render import render_atlas, render_tree  # noqa: E402
from dirscape.state.diff import diff  # noqa: E402
from dirscape.state.snapshot import RootRecord, Snapshot  # noqa: E402


def _live(path, used, identity, local=False, device="dev"):
    root = _root(path, device=device, writable=True)
    root.identity = identity
    if local:
        root.policy["node_local"] = True
    root.quota = QuotaSnapshot("walk", [QuotaRow("", "blocks", "user", used, mount=path)])
    return root


def _snap(roots, host):
    return Snapshot.from_roots(
        roots,
        taken_at=1.0 if host == "a" else 2.0,
        hostname=host,
        node_class="login",
        cluster_fingerprint="fp",
        discovery=confirmed("swept"),
    )


def test_another_login_node_is_not_compared_disk_for_disk():
    """Sylvia: `/tmp` on login-01 and login-02 are two disks with one name."""
    gib = 1024**3
    before = _snap([_live("/tmp", 5 * gib, (64770, 2), local=True)], "sylvia-login-02")
    after = _snap([_live("/tmp", 0, (64770, 2), local=True)], "sylvia-login-01")
    result = diff(before, after)
    assert list(result) == []
    assert any("different disks" in warning for warning in result.warnings)


def test_a_quiet_hop_between_login_nodes_says_nothing():
    before = _snap([_live("/tmp", 44 * 1024, (64770, 2), local=True)], "sylvia-login-02")
    after = _snap([_live("/tmp", 0, (64770, 2), local=True)], "sylvia-login-01")
    assert diff(before, after).warnings == []


def test_another_client_numbers_the_same_nfs_directory_differently():
    """`/admin_home` read as replaced on the second Sylvia login node.

    NFS `st_dev` numbers are handed out per client: (63, 1902510144) on one
    node and (60, 1902510144) on the other, for one directory on one server.
    """
    before = _snap([_live("/admin_home", 1, (63, 1902510144))], "sylvia-login-02")
    after = _snap([_live("/admin_home", 1, (60, 1902510144))], "sylvia-login-01")
    result = diff(before, after)
    assert not any("replaced" in warning for warning in result.warnings)
    assert not any(change.identity_changed for change in result)
    # On one host a moved device number is still news.
    moved = _snap([_live("/admin_home", 1, (60, 1902510144))], "sylvia-login-02")
    assert any("replaced" in w for w in diff(before, moved).warnings)


def test_node_local_survives_the_state_file():
    root = _live("/tmp", 1, (1, 2), local=True)
    again = RootRecord.from_json(RootRecord.from_root(root).to_json())
    assert again.node_local is True
    assert (
        RootRecord.from_json(RootRecord.from_root(_live("/home", 1, (1, 3))).to_json()).node_local
        is False
    )


def test_new_shows_a_root_that_has_gone_instead_of_an_empty_box():
    """It printed "no roots were handed to this view" when every change was a `gone`."""
    before = _snap([_live("/scratch/old", 1, (1, 2))], "a")
    after = _snap([], "a")
    changes = list(diff(before, after))
    assert changes and changes[0].label == "gone"
    text = render_atlas([], changes=changes, deltas=True, style=Style(color=False), size=100)
    assert "/scratch/old" in text
    assert "no roots were handed" not in text


def test_the_tree_names_lustre_by_filesystem_and_project_by_word():
    home = _root("/home/me", device=ACORN + ":/acorn/home", writable=True)
    home.quota = QuotaSnapshot(
        "lfs quota", [QuotaRow("", "blocks", "user", 1, hard=2, mount="/home", device="/home")]
    )
    project = _root(
        "/lus/egret/projects/p", fileset="13579", device=EGRET + ":/egret", writable=True
    )
    text = render_tree([home, project], style=Style(color=False), size=120)
    assert "acorn/home (lustre)" in text
    assert "egret (lustre)" in text
    assert "project 13579" in text
    assert "@o2ib" not in text, "no NID list in a heading"
    assert "this filesystem has no quota scopes" not in text


def test_a_declared_snapshot_tree_does_not_vouch_for_unrelated_paths(tmp_path):
    """meadow2: `/` (a tmpfs) claimed "18 snapshot copies" out of `/snapshots/home`."""
    container = tmp_path / "snapshots" / "home"
    (container / "daily-2026-08-06.05h07" / "jdoe42").mkdir(parents=True)
    table = read_mount_table(text="rootfs / tmpfs rw 0 0\n")
    index = SnapshotIndex(roots=[str(tmp_path / "snapshots")])
    copies, verdict = copies_for_path("/", table, "rootfs", index)
    assert copies == []
    assert not verdict.confirmed
    copies, verdict = copies_for_path("/tmp", table, "rootfs", index)
    assert "exist on this filesystem" not in (verdict.reason or "")


# --------------------------------------------------------------------------
# The whole pipeline on an ACME-shaped Lustre site
# --------------------------------------------------------------------------

from dirscape.discover import read_identity  # noqa: E402
from dirscape.quota import default_backends, read_all  # noqa: E402
from dirscape.quota.base import current_user, primary_group  # noqa: E402
from dirscape.runner import Budget, RecordedRunner  # noqa: E402


def _lfs_user(path, kbytes, files, soft=0, hard=0):
    return (
        "Disk quotas for usr me (uid 1):\n"
        "     Filesystem  kbytes   quota   limit   grace   files   quota   limit   grace\n"
        "%s %d %d %d - %d 0 0 -\n" % (path, kbytes, soft, hard, files)
    )


def _lfs_project(path, projid, kbytes, files, hard):
    return (
        "Disk quotas for prj %s (pid %s):\n"
        "     Filesystem  kbytes   quota   limit   grace   files   quota   limit   grace\n"
        "%s\n %d %d %d - %d 0 0 -\n" % (projid, projid, path, kbytes, hard, hard, files)
    )


@pytest.mark.skipif(os.geteuid() == 0, reason="root bypasses the permission bits")
def test_an_acme_shaped_site_shows_the_two_rows_that_are_true(tmp_path, cmd):
    """Sylvia, reproduced: a Lustre home mounted out of `acorn`, the `acorn`
    root beside it, a project on `egret`, and `grove` as a read-only clone.

    Before: five rows, two of them carrying figures that were true of
    something else. After: the home and the project, each with its own.
    """
    home_mount, acorn, egret, grove = (tmp_path / p for p in ("home", "acorn", "egret", "grove"))
    me = home_mount / "me"
    project = egret / "projects" / "proj"
    for d in (me, acorn, project, grove):
        d.mkdir(parents=True, exist_ok=True)
    for d in (home_mount, acorn, egret, egret / "projects", grove):
        d.chmod(0o555)
    text = (
        "\n".join(
            [
                "%s:/acorn/home %s lustre rw,flock 0 0" % (ACORN, home_mount),
                "%s:/acorn %s lustre rw,flock 0 0" % (ACORN, acorn),
                "%s:/egret %s lustre rw,flock 0 0" % (EGRET, egret),
                "%s:/egret/clone/g2 %s lustre rw,flock 0 0" % (EGRET, grove),
            ]
        )
        + "\n"
    )
    table = read_mount_table(text=text)
    user, group = current_user(), primary_group()
    gib = 1024 * 1024
    answers = []
    for mount, kb, files, soft, hard in (
        (home_mount, 36 * gib, 32959, 342 * gib, 373 * gib),
        (acorn, 36 * gib, 32959, 342 * gib, 373 * gib),
        (egret, 4100 * gib, 216168, 0, 0),
        (grove, 4100 * gib, 216168, 0, 0),
        (project, 4100 * gib, 216168, 0, 0),
    ):
        answers.append(
            cmd(
                ["/usr/bin/lfs", "quota", "-u", user, str(mount)],
                stdout=_lfs_user(mount, kb, files, soft, hard),
            )
        )
        answers.append(
            cmd(
                ["/usr/bin/lfs", "quota", "-g", group, str(mount)],
                stdout=_lfs_user(mount, 2700000 * gib, 1, 0, 0).replace("usr me", "grp users"),
            )
        )
        answers.append(
            cmd(["/usr/bin/lfs", "project", "-d", str(mount)], stdout="0 - %s\n" % (mount,))
        )
    answers = [a for a in answers if a["argv"][2:] != ["-d", str(project)]]
    answers.append(
        cmd(["/usr/bin/lfs", "project", "-d", str(project)], stdout="13579 P %s\n" % (project,))
    )
    answers.append(
        cmd(
            ["/usr/bin/lfs", "quota", "-p", "13579", str(project)],
            stdout=_lfs_project(project, 13579, 29 * 1024 * gib, 8214572, 50 * 1024 * gib),
        )
    )
    runner = RecordedRunner(
        answers,
        probes={
            "lfs": "/usr/bin/lfs",
            "mmlsquota": None,
            "mmlsattr": None,
            "xfs_quota": None,
            "xfs_io": None,
            "quota": None,
            "accounts": None,
            "rcchelp": None,
        },
        strict=False,
    )
    try:
        run = cli.Run()
        run.site = _site_for(tmp_path)
        run.site.role_globs.insert(0, (str(home_mount) + "*", "home"))
        run.mounts = table
        # Groups that own nothing here, as on ACME where every directory under
        # these roots belongs to root or to somebody else.
        run.identity = _identity([4242], groups=("users", "proj"))
        budget = Budget(30.0)
        run.quota_attempts = read_all(default_backends(run.site), runner, table, budget, ["/"])
        run.roots = discover(runner, table, run.identity, budget, run.site, env={"HOME": str(me)})
        cli.attribute_all(run.roots, runner, table, budget, run.site)
        cli._attach_quota(run, budget, runner, measure=False)
        cli._mark_stranded(run)
        shown, _hidden = cli._visible(run, show_all=False)
    finally:
        for d in (home_mount, acorn, egret, egret / "projects", grove):
            d.chmod(0o755)

    by_path = {r.path: r for r in run.roots}
    assert [r.path for r in shown] == [str(me), str(project)]
    assert by_path[str(me)].quota.rows[0].used == 36 * gib * 1024
    project_row = by_path[str(project)].quota.rows[0]
    assert (project_row.scope, project_row.fileset) == ("project", "13579")
    for other in (grove, acorn, egret):
        assert by_path[str(other)].quota is None, "%s carried a figure about something else" % (
            other,
        )
    # (`acorn` is not held back HERE only because this fake world puts it at
    # the same depth as the home mount; the real layout is pinned above.)
    assert not any("holds space in" in w for w in run.warnings)


def test_the_interactive_view_still_reports_its_warnings(tmp_path, monkeypatch, capsys):
    """`main` returned straight out of the browse, so in a terminal a run that
    ran out of time showed 48 rows of `?` on meadow2 and never said why."""
    run = cli.Run()
    run.roots = [_root(str(tmp_path), writable=True)]
    run.warnings = ["the 20s time allowance ran out before every directory was checked"]
    monkeypatch.setattr(cli, "sweep", lambda opts, runner=None: run)
    monkeypatch.setattr(cli.interactive, "supported", lambda stream=None: True)
    monkeypatch.setattr(cli, "_browse", lambda run, opts, style, width: 0)
    assert cli.main(["--color", "never"]) == 0
    assert "time allowance ran out" in capsys.readouterr().err


def test_a_slow_search_cannot_starve_the_probes(tmp_path, monkeypatch):
    """The same run: the searching sources spent the allowance and no root,
    the reader's home included, was probed at all."""
    import time as clock

    home = tmp_path / "home" / "me"
    home.mkdir(parents=True)
    project = tmp_path / "project"
    project.mkdir()
    text = "cap %s gpfs rw 0 0\ncap %s gpfs rw 0 0\n" % (tmp_path / "home", project)

    def slow(path, deadline_s):
        clock.sleep(0.05)
        return False

    monkeypatch.setattr(candidates_module, "_is_dir", slow)
    budget = Budget(1.0)
    roots = discover(
        _NoCommands(),
        read_mount_table(text=text),
        _identity([4242], groups=["g%d" % i for i in range(60)]),
        budget,
        _site_for(tmp_path),
        env={"HOME": str(home)},
    )
    mine = {r.path: r for r in roots}[str(home)]
    assert mine.present.confirmed, "the home directory must still have been probed"
