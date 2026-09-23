"""CephFS figures from the metadata server's own xattrs.

The tree below is the HPC `/cfs3` tier as a meadow2 login node reported it on
2026-09-22 (`os.getxattr` on a kernel CephFS mount), described through an
injected reader so no CephFS mount is needed to run it.
"""

import errno

import pytest

from dirscape import cli
from dirscape.discover.attribute import attribute_ceph, ceph_quota_dir
from dirscape.discover.mounts import read_mount_table
from dirscape.model import Reach, Root, VerdictCategory, confirmed
from dirscape.quota.ceph import CephBackend, read_ceph_dir
from dirscape.runner import Budget

MOUNTS = (
    "203.0.113.90,203.0.113.91,203.0.113.92,203.0.113.93,203.0.113.94:/ /cfs3 ceph "
    "rw,relatime,name=cephfs,secret=<hidden>,acl,mds_namespace=cfs3,wsize=16777216 0 0\n"
    "meadow2_perf2 /home gpfs rw 0 0\n"
)

XATTRS = {
    "/cfs3": {"ceph.dir.rbytes": 1653225770135428, "ceph.dir.rfiles": 320273672},
    "/cfs3/kestrel-lab": {
        "ceph.dir.rbytes": 170551918755602,
        "ceph.dir.rfiles": 5024085,
        "ceph.quota.max_bytes": 181419418583040,
    },
    "/cfs3/hpc-staff": {
        "ceph.dir.rbytes": 61718383,
        "ceph.dir.rfiles": 40,
        "ceph.quota.max_bytes": 10737418240,
    },
    "/cfs3/hpc-staff/jdoe42": {"ceph.dir.rbytes": 1024, "ceph.dir.rfiles": 2},
}


def fake_getxattr(path, name):
    """The live answers, including ENODATA's None for a quota nobody set."""
    return XATTRS.get(path.rstrip("/") or "/", {}).get(name)


def test_a_directory_with_its_own_quota_is_its_own_scope():
    assert ceph_quota_dir("/cfs3/kestrel-lab", "/cfs3", fake_getxattr) == "/cfs3/kestrel-lab"


def test_a_directory_under_a_quota_names_the_ancestor():
    assert ceph_quota_dir("/cfs3/hpc-staff/jdoe42", "/cfs3", fake_getxattr) == "/cfs3/hpc-staff"


def test_a_directory_with_no_quota_above_it_has_no_scope():
    assert ceph_quota_dir("/cfs3", "/cfs3", fake_getxattr) == ""


def test_the_figures_are_the_governing_directorys():
    scope, used, files, max_bytes, max_files = read_ceph_dir(
        "/cfs3/hpc-staff/jdoe42", "/cfs3", fake_getxattr
    )
    assert scope == "/cfs3/hpc-staff"
    assert (used, files, max_bytes, max_files) == (61718383, 40, 10737418240, 0)


def test_the_backend_reports_one_row_per_quota_scope():
    """Two asked directories under one quota are one scope, so one row each kind."""
    mounts = read_mount_table(text=MOUNTS)
    backend = CephBackend(getxattr=fake_getxattr)
    snap = backend.read(
        None,
        mounts,
        Budget(10.0),
        ["/cfs3/kestrel-lab", "/cfs3/hpc-staff", "/cfs3/hpc-staff/jdoe42", "/home/me"],
    )
    blocks = {row.fileset: row for row in snap.rows if row.kind == "blocks"}
    assert set(blocks) == {"/cfs3/kestrel-lab", "/cfs3/hpc-staff"}
    night = blocks["/cfs3/kestrel-lab"]
    assert (night.used, night.hard, night.scope) == (170551918755602, 181419418583040, "fileset")
    assert night.mount == "/cfs3/kestrel-lab"
    assert night.device == "CephFS cfs3", "named by the filesystem, not the monitor list"


def test_a_tree_with_no_quota_reports_usage_against_none():
    mounts = read_mount_table(text=MOUNTS)
    snap = CephBackend(getxattr=fake_getxattr).read(None, mounts, Budget(10.0), ["/cfs3"])
    blocks = [row for row in snap.rows if row.kind == "blocks"]
    assert len(blocks) == 1
    assert blocks[0].fileset == ""
    assert blocks[0].hard == 0, "no quota anywhere above it is the model's `none`"


def test_nothing_on_cephfs_asked_is_not_a_failure():
    mounts = read_mount_table(text=MOUNTS)
    snap = CephBackend(getxattr=fake_getxattr).read(None, mounts, Budget(10.0), ["/", "/home/me"])
    assert not snap.available
    assert snap.category == VerdictCategory.NO_QUOTA_BACKEND


def test_a_directory_that_publishes_nothing_is_reported_as_a_failure():
    mounts = read_mount_table(text=MOUNTS)

    def refuses(path, name):
        raise OSError(errno.EOPNOTSUPP, "Operation not supported")

    snap = CephBackend(getxattr=refuses).read(None, mounts, Budget(10.0), ["/cfs3/kestrel-lab"])
    assert not snap.available
    assert snap.category == VerdictCategory.BACKEND_FAILED
    assert "/cfs3/kestrel-lab" in snap.reason


def test_the_attribute_pass_gives_the_root_the_same_scope_name(tmp_path):
    """So `_rows_governing` meets the row on the fileset, and `tree` groups by it.

    Without it both `/cfs3` directories sat under one "no fileset" line in
    `dirscape tree`, showing the first one's `155T of 165T` for both.
    """
    mounts = read_mount_table(text=MOUNTS)
    root = Root("/cfs3/hpc-staff/jdoe42", device="203.0.113.90:/", fstype="ceph")
    verdict = attribute_ceph(root, mounts, Budget(10.0), getxattr=fake_getxattr)
    assert verdict.confirmed
    assert root.fileset == "/cfs3/hpc-staff"
    assert any("/cfs3/hpc-staff" in note for note in root.notes)


@pytest.mark.parametrize(
    "path, expected",
    [
        ("/cfs3/kestrel-lab", (170551918755602, 181419418583040)),
        ("/cfs3/hpc-staff", (61718383, 10737418240)),
    ],
)
def test_the_figure_lands_on_its_own_directory(path, expected):
    """End to end through the attach step, which is where attribution fails."""
    mounts = read_mount_table(text=MOUNTS)
    run = cli.Run()
    run.mounts = mounts
    roots = []
    for where in ("/cfs3/kestrel-lab", "/cfs3/hpc-staff"):
        root = Root(where, role="archive", device=mounts.at("/cfs3")[0].device, fstype="ceph")
        root.fileset = where
        root.reach = Reach.LISTABLE
        root.present = confirmed()
        root.writable = confirmed()
        roots.append(root)
    run.roots = roots
    run.quota_attempts = [
        CephBackend(getxattr=fake_getxattr).read(
            None, mounts, Budget(10.0), [r.path for r in roots]
        )
    ]
    cli._place_rows(run, {(r.device, r.fileset): r.path for r in roots}, {}, {}, {})
    got = {r.path: r for r in run.roots}[path]
    assert got.quota is not None
    assert (got.quota.rows[0].used, got.quota.rows[0].hard) == expected
