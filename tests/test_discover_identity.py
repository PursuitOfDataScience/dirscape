"""Identity and the cluster fingerprint.

The fingerprint tests are the load-bearing ones. A snapshot key that varies per
node would give every compute node its own baseline, so nothing would ever be
reported as new and the whole diff layer would be decoration.
"""

import os

from dirscape.discover.identity import (
    Identity,
    cluster_fingerprint,
    group_names,
    read_identity,
    user_name,
)
from dirscape.discover.mounts import read_mount_table

CLUSTER_ONE = """\
sysfs /sys sysfs rw 0 0
one_cap /gpfs/one/cap gpfs rw 0 0
one_cap /home gpfs rw 0 0
one_perf /scratch/one gpfs rw 0 0
/dev/sda1 /tmp xfs rw 0 0
"""

# The same fabric seen from a node that mounts fewer of the aliases, which is
# what a compute node looks like next to a login node.
CLUSTER_ONE_FEWER_ALIASES = """\
one_cap /gpfs/one/cap gpfs rw 0 0
one_perf /scratch/one gpfs rw 0 0
"""

CLUSTER_TWO = """\
two_cap /gpfs/two/cap gpfs rw 0 0
two_cap /home gpfs rw 0 0
two_perf /scratch/two gpfs rw 0 0
"""

NO_NETWORK = """\
/dev/sda1 / ext4 rw 0 0
/dev/sda2 /home ext4 rw 0 0
tmpfs /dev/shm tmpfs rw 0 0
"""


# --------------------------------------------------------------------------
# The fingerprint
# --------------------------------------------------------------------------


def test_fingerprint_is_independent_of_the_hostname():
    """The whole point. Two nodes of one cluster share a baseline."""
    mounts = read_mount_table(text=CLUSTER_ONE)
    first = read_identity(mounts, env={}, hostname="cluster-login1")
    second = read_identity(mounts, env={"SLURM_JOB_ID": "9"}, hostname="cluster-0407")
    assert first.fingerprint == second.fingerprint
    assert first.hostname != second.hostname
    assert first.node_class != second.node_class


def test_fingerprint_does_not_contain_the_hostname():
    mounts = read_mount_table(text=CLUSTER_ONE)
    me = read_identity(mounts, env={}, hostname="verydistinctivehost")
    assert "verydistinctive" not in me.fingerprint
    assert me.fingerprint == cluster_fingerprint(mounts)


def test_fingerprint_differs_between_clusters():
    """So meadow2, meadow3, collie3 and Brook snapshots cannot collide."""
    one = cluster_fingerprint(read_mount_table(text=CLUSTER_ONE))
    two = cluster_fingerprint(read_mount_table(text=CLUSTER_TWO))
    assert one != two


def test_fingerprint_survives_a_node_mounting_fewer_aliases():
    """One device at four mountpoints is one device.

    A login node mounts aliases a compute node does not, and both are the same
    cluster with the same baseline.
    """
    full = cluster_fingerprint(read_mount_table(text=CLUSTER_ONE))
    partial = cluster_fingerprint(read_mount_table(text=CLUSTER_ONE_FEWER_ALIASES))
    assert full == partial


def test_fingerprint_ignores_local_disks():
    """A node-local device name would vary per node."""
    with_local = cluster_fingerprint(read_mount_table(text=CLUSTER_ONE))
    without = cluster_fingerprint(
        read_mount_table(text=CLUSTER_ONE.replace("/dev/sda1 /tmp xfs rw 0 0\n", ""))
    )
    assert with_local == without


def test_fingerprint_is_short_and_stable():
    mounts = read_mount_table(text=CLUSTER_ONE)
    digest = cluster_fingerprint(mounts)
    assert len(digest) == 10
    assert digest == cluster_fingerprint(mounts)
    assert cluster_fingerprint(mounts, length=6) == digest[:6]


def test_a_machine_with_no_network_storage_still_gets_a_key():
    """Otherwise every laptop and container would share one baseline."""
    mounts = read_mount_table(text=NO_NETWORK)
    me = read_identity(mounts, env={}, hostname="laptop")
    assert me.fingerprint
    assert me.fingerprint_basis == "mountpoints"
    assert any("no network storage" in note for note in me.notes)
    # And a different local layout is a different key.
    other = read_mount_table(text=NO_NETWORK.replace("/home", "/users"))
    assert cluster_fingerprint(other) != me.fingerprint


def test_network_basis_is_recorded_when_there_is_one():
    me = read_identity(read_mount_table(text=CLUSTER_ONE), env={}, hostname="h")
    assert me.fingerprint_basis == "network-devices"
    assert me.notes == []


# --------------------------------------------------------------------------
# Names that will not resolve
# --------------------------------------------------------------------------


def test_group_names_degrade_to_numbers_rather_than_raising():
    """Measured: meadow2 compute nodes cannot resolve a uid to a name at all.

    ``getent passwd`` returns empty there and Slurm renders the job owner as
    ``nobody``. A storage tool that raises KeyError cannot run where the jobs
    run, so an unresolvable id becomes its own number.
    """
    notes = []
    # Deliberately absurd gids. Nothing resolves these at any site.
    names = group_names([4294967290, 4294967291], notes)
    assert names == ["4294967290", "4294967291"]
    assert notes and "do not resolve" in notes[0]


def test_group_names_deduplicate_and_keep_order():
    gid = os.getgid()
    assert group_names([gid, gid]) == group_names([gid])


def test_user_name_degrades_to_the_uid():
    notes = []
    assert user_name(4294967290, notes) == "4294967290"
    assert notes and "does not resolve" in notes[0]


def test_user_name_resolves_the_caller():
    """Whatever the answer is, it must be a non-empty string and not raise."""
    assert user_name()


# --------------------------------------------------------------------------
# The rest of the identity
# --------------------------------------------------------------------------


def test_identity_records_what_the_snapshot_needs():
    mounts = read_mount_table(text=CLUSTER_ONE)
    me = read_identity(mounts, env={"SLURM_JOB_ID": "7"}, hostname="cluster-0001")
    assert me.hostname == "cluster-0001"
    assert me.node_class == "compute"
    assert me.uid == os.getuid()
    assert me.user
    assert me.groups
    payload = me.to_json()
    for key in ("uid", "user", "hostname", "node_class", "fingerprint", "cluster"):
        assert key in payload


def test_the_primary_gid_is_always_in_the_gid_set():
    """getgroups() omits it on some kernels.

    A missing primary gid makes the dir-owner source blind to the user's own
    directories, which are the ones they care about most.
    """
    me = read_identity(read_mount_table(text=CLUSTER_ONE), env={}, hostname="h")
    assert os.getgid() in me.gid_set


def test_cluster_name_is_guessed_from_the_devices_not_the_hostname():
    """A device name is a property of the fabric; a hostname is not."""
    mounts = read_mount_table(text=CLUSTER_ONE)
    from_login = read_identity(mounts, env={}, hostname="cluster-login1")
    from_compute = read_identity(mounts, env={}, hostname="cluster-0407")
    assert from_login.cluster == from_compute.cluster
    assert from_login.cluster == "one"


def test_a_site_name_overrides_the_guess():
    class FakeSite(object):
        name = "declared-name"

    me = read_identity(read_mount_table(text=CLUSTER_ONE), env={}, hostname="h", site=FakeSite())
    assert me.cluster == "declared-name"


def test_identity_can_be_constructed_directly_for_a_recorded_cluster():
    """The constructor has to be usable without touching this host at all."""
    me = Identity(uid=1000, gid=1000, user="someone", gids=[1000, 2000], groups=["a", "b"])
    assert me.gid_set == frozenset([1000, 2000])
    assert me.to_json()["groups"] == ["a", "b"]
