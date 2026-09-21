"""Where the run was standing, and why that decides whether a root can be `gone`.

Mount visibility is a property of the NODE, not of the filesystem. On this site
``/cfs3`` is mounted on login nodes only, so a snapshot taken on a login node
and compared against a compute node run sees a whole filesystem vanish. The
correct label there is `unknown`, and the correct extra output is a warning that
says why.

This is the same invariant as the timeout case in `test_state_invariant.py`,
arriving through a different door: the current run did not fail to answer, it
was never asked, because the path does not exist from where it stood.
"""

import json

from dirscape.discover.identity import Identity, read_identity
from dirscape.discover.mounts import node_class, read_mount_table
from dirscape.model import Reach, Root, VerdictCategory, confirmed, refuted, unknown
from dirscape.state.diff import GONE, UNKNOWN, diff
from dirscape.state.snapshot import (
    COMPUTE,
    LOGIN,
    UNKNOWN_CLASS,
    Snapshot,
    fingerprint_mismatch,
    same_vantage,
    vantage_warning,
)

T0 = 1757000000.0
DAY = 86400.0

FINGERPRINT = "ab12cd34ef"

#: A mount table shaped like this cluster's, for the integration test at the
#: bottom. The tab separated form is what /proc/self/mounts emits.
MOUNTS = "\n".join(
    [
        "meadow3_cap /home gpfs rw,relatime 0 0",
        "meadow3_cap /project gpfs rw,relatime 0 0",
        "meadow3_perf /scratch/meadow3 gpfs rw,relatime 0 0",
        "cfs3_store /cfs3 nfs4 rw,relatime 0 0",
        "proc /proc proc rw,relatime 0 0",
        "tmpfs /dev/shm tmpfs rw,nosuid 0 0",
    ]
)


def _root(path, reach=Reach.LISTABLE, device="gpfs1", identity=(2049, 101), present=None):
    root = Root(path, role="project", device=device)
    root.identity = identity
    root.reach = reach
    root.reach_reason = "os.access reports read and execute"
    root.mounted = confirmed("present in /proc/self/mounts", source="mounts")
    root.present = present or confirmed("stat succeeded", source="os.stat")
    root.add_source("mounts")
    return root


def _snapshot(roots, hostname, klass, taken_at=T0, discovery=None, fingerprint=FINGERPRINT):
    return Snapshot.from_roots(
        roots,
        taken_at=taken_at,
        hostname=hostname,
        node_class=klass,
        cluster_fingerprint=fingerprint,
        discovery=discovery,
    )


def _swept():
    """A discovery verdict saying the sweep really did complete."""
    return confirmed("swept 6 mounts and 3 allocations", source="mounts")


# --------------------------------------------------------------------------
# 6. Login against compute
# --------------------------------------------------------------------------


def test_a_login_only_mount_is_unknown_on_a_compute_node_not_gone():
    """Test 6. The measured case: /cfs3 is mounted on login nodes only.

    Reporting it as removed would tell a user that an archive filesystem holding
    their data has gone, when the truth is that they ran the tool from a
    different kind of node. The warning is as important as the label: without
    it the user is left with a row they cannot interpret.
    """
    previous = _snapshot(
        [_root("/cfs3/kestrel-lab", device="cfs3_store", identity=(2050, 7)), _root("/project/x")],
        hostname="meadow3-login3",
        klass=LOGIN,
        taken_at=T0,
    )
    current = _snapshot(
        [_root("/project/x")],
        hostname="meadow3-0200",
        klass=COMPUTE,
        taken_at=T0 + DAY,
        discovery=_swept(),
    )

    result = diff(previous, current)

    assert result.same_vantage is False
    assert result.warnings, "a cross vantage diff must explain itself"
    assert "node class" in result.warnings[0]
    assert "login" in result.warnings[0] and "compute" in result.warnings[0]

    assert result.labels() == [UNKNOWN]
    change = result.records[0]
    assert change.path == "/cfs3/kestrel-lab"
    assert change.current == Reach.LISTABLE, "the login node's verdict is carried forward"
    assert change.carried_forward is True
    assert GONE not in result.labels()
    assert "gone" not in json.dumps(result.to_json())
    assert "different vantage point" in change.because


def test_the_same_vantage_control_does_report_gone():
    """The control that proves the test above is not suppressing everything.

    Two login nodes of one cluster see the same mounts, so a root that was there
    yesterday and is not there today, on a run whose sweep completed, really is
    gone. Without this the vantage gate could be a blanket "never say gone" and
    both tests would pass.
    """
    previous = _snapshot(
        [_root("/project/retired"), _root("/project/x")],
        hostname="meadow3-login3",
        klass=LOGIN,
        taken_at=T0,
    )
    current = _snapshot(
        [_root("/project/x")],
        hostname="meadow3-login4",
        klass=LOGIN,
        taken_at=T0 + DAY,
        discovery=_swept(),
    )

    result = diff(previous, current)

    assert result.same_vantage is True
    assert result.warnings == []
    assert result.labels() == [GONE]
    change = result.records[0]
    assert change.path == "/project/retired"
    assert change.previous == Reach.LISTABLE
    assert "discovery sweep" in change.because, "the evidence must name what looked"


def test_gone_is_impossible_until_something_proves_the_sweep_completed():
    """The default must be unable to claim a loss.

    `Snapshot.discovery` defaults to NOT_PROBED, so a caller that never sets it
    can only ever produce `unknown`. That is the right way round: absence of a
    record is not evidence that anything was looked for, and this tool does not
    get to claim a loss until something proves it looked.
    """
    previous = _snapshot([_root("/project/retired")], hostname="h1", klass=LOGIN, taken_at=T0)
    current = _snapshot([], hostname="h1", klass=LOGIN, taken_at=T0 + DAY)

    result = diff(previous, current)

    assert current.discovery.category == VerdictCategory.NOT_PROBED
    assert result.labels() == [UNKNOWN]
    assert "did not confirm that it completed" in result.records[0].because
    assert result.records[0].current == Reach.LISTABLE


def test_a_durable_presence_refusal_on_the_same_host_is_gone():
    """The other evidence path: the probe stat'd the path and it was not there.

    This one does not need the sweep, because the per-root probe answered
    directly. It still needs the same vantage point: NOT_MOUNTED_HERE from a
    different node class is expected rather than news.
    """
    previous = _snapshot([_root("/project/deleted")], hostname="h1", klass=LOGIN, taken_at=T0)
    current = _snapshot(
        [
            _root(
                "/project/deleted",
                reach=Reach.UNKNOWN,
                present=refuted(VerdictCategory.NOT_PRESENT, "ENOENT from stat"),
            )
        ],
        hostname="h1",
        klass=LOGIN,
        taken_at=T0 + DAY,
    )

    result = diff(previous, current)

    assert GONE in result.labels()
    gone = result.with_label(GONE)[0]
    assert gone.current == VerdictCategory.NOT_PRESENT, "the field carries the wire token"
    assert "presence probe" in gone.because


def test_not_mounted_here_across_node_classes_is_not_a_loss():
    """The same durable refusal, from the wrong vantage point, is not news.

    A compute node reporting NOT_MOUNTED_HERE for /cfs3 is describing the node,
    not the filesystem, which is the whole reason `Root` keeps `mounted` and
    `present` as separate axes.
    """
    previous = _snapshot(
        [_root("/cfs3/kestrel-lab", device="cfs3_store")],
        hostname="meadow3-login3",
        klass=LOGIN,
        taken_at=T0,
    )
    current = _snapshot(
        [
            _root(
                "/cfs3/kestrel-lab",
                device="cfs3_store",
                reach=Reach.UNKNOWN,
                present=refuted(VerdictCategory.NOT_MOUNTED_HERE, "absent from /proc/self/mounts"),
            )
        ],
        hostname="meadow3-0200",
        klass=COMPUTE,
        taken_at=T0 + DAY,
        discovery=_swept(),
    )

    result = diff(previous, current)
    assert GONE not in result.labels()
    assert UNKNOWN in result.labels()


# --------------------------------------------------------------------------
# same_vantage: the rule itself
# --------------------------------------------------------------------------


def test_two_unknown_node_classes_are_not_agreement():
    """An unclassifiable host is not thereby the same kind of host as another one.

    `node_class` returns "unknown" whenever neither the Slurm environment nor
    the hostname pattern answers, and two such hosts could be a login node and
    a compute node. Treating the string equality as agreement would let the
    weakest signal authorise the strongest claim.
    """
    previous = Snapshot(taken_at=T0, hostname="hostA", node_class=UNKNOWN_CLASS)
    current = Snapshot(taken_at=T0 + DAY, hostname="hostB", node_class=UNKNOWN_CLASS)
    assert same_vantage(previous, current) is False
    assert vantage_warning(previous, current)


def test_the_same_host_is_the_same_vantage_whatever_the_class():
    """The escape hatch that keeps `gone` reachable where classification fails.

    This package was developed on meadow3-0200, reached through a durable tmux
    where no SLURM_* variable survives, so "unknown" is a state that really
    happens on a real node. One host's mount set is one host's mount set
    however it was labelled.
    """
    previous = Snapshot(taken_at=T0, hostname="meadow3-0200", node_class=UNKNOWN_CLASS)
    current = Snapshot(taken_at=T0 + DAY, hostname="meadow3-0200", node_class=UNKNOWN_CLASS)
    assert same_vantage(previous, current) is True
    assert vantage_warning(previous, current) is None


def test_the_node_class_vocabulary_agrees_with_discovery():
    """`state/` duplicates three strings rather than importing from `discover/`.

    The duplication keeps this package's dependency on `model` one way, and this
    test is what makes it safe: a drift in either module fails here rather than
    silently disabling the vantage gate, which would turn every cross node diff
    into a page of `gone` rows.
    """
    assert node_class({"SLURM_JOB_ID": "1234567"}, "meadow3-0200") == COMPUTE
    assert node_class({}, "meadow3-login3") == LOGIN
    assert node_class({}, "") == UNKNOWN_CLASS
    assert {COMPUTE, LOGIN, UNKNOWN_CLASS} == {"compute", "login", "unknown"}


# --------------------------------------------------------------------------
# Refusing to diff across clusters
# --------------------------------------------------------------------------


def test_a_cross_cluster_diff_is_refused_rather_than_reported():
    """Every label would be a claim about a different filesystem.

    Diffing meadow3 against collie3 would read as every root lost and every
    root gained. The fingerprint is keyed on the storage fabric precisely so
    this is detectable, and the honest output is a refusal with a reason.
    """
    previous = _snapshot([_root("/project/x")], hostname="h1", klass=LOGIN, taken_at=T0)
    current = _snapshot(
        [_root("/collie3/y")],
        hostname="h2",
        klass=LOGIN,
        taken_at=T0 + DAY,
        fingerprint="99deadbeef",
        discovery=_swept(),
    )

    result = diff(previous, current)

    assert list(result) == []
    assert result.cross_cluster is True
    assert result.warnings and "different" not in result.warnings[0].split()[0]
    assert "ab12cd34ef" in result.warnings[0] and "99deadbeef" in result.warnings[0]


def test_fingerprint_mismatch_accepts_an_identity_a_string_or_a_mapping():
    """The caller should not have to reshape what it already has."""
    snapshot = _snapshot([_root("/project/x")], hostname="h1", klass=LOGIN)
    identity = Identity(1000, 1000, "jdoe42", fingerprint=FINGERPRINT)

    assert fingerprint_mismatch(snapshot, identity) is None
    assert fingerprint_mismatch(snapshot, FINGERPRINT) is None
    assert fingerprint_mismatch(snapshot, {"fingerprint": FINGERPRINT}) is None

    other = Identity(1000, 1000, "jdoe42", fingerprint="99deadbeef")
    assert fingerprint_mismatch(snapshot, other)
    assert fingerprint_mismatch(snapshot, {"fingerprint": "99deadbeef"})


def test_a_missing_fingerprint_is_not_a_mismatch():
    """An unanswered question is not a negative answer, here too.

    An older lineage predates the field, and refusing to diff on that basis
    would throw away the only baseline the user has.
    """
    unkeyed = Snapshot(taken_at=T0, hostname="h1", node_class=LOGIN, cluster_fingerprint="")
    assert fingerprint_mismatch(unkeyed, FINGERPRINT) is None
    keyed = _snapshot([_root("/project/x")], hostname="h1", klass=LOGIN)
    assert fingerprint_mismatch(keyed, "") is None
    assert fingerprint_mismatch(None, FINGERPRINT) is None


# --------------------------------------------------------------------------
# Against the real Identity, not a stub
# --------------------------------------------------------------------------


def test_a_real_identity_populates_the_snapshot_header():
    """The duck typing has to work against the class it was written for.

    `state/` never imports `discover/`, so nothing but a test can catch the two
    drifting apart. `read_identity` is driven off a supplied mount table, so
    this runs anywhere.
    """
    mounts = read_mount_table(text=MOUNTS)
    identity = read_identity(mounts, env={}, hostname="meadow3-login3.hpc.local")

    snapshot = Snapshot.from_roots([_root("/project/x")], identity=identity, taken_at=T0)

    assert snapshot.hostname == "meadow3-login3.hpc.local"
    assert snapshot.node_class == LOGIN
    assert snapshot.cluster_fingerprint == identity.fingerprint
    assert snapshot.cluster_fingerprint, "the fabric hash must actually be populated"
    assert fingerprint_mismatch(snapshot, identity) is None

    # A compute node of the same cluster: same fabric, different vantage.
    compute = read_identity(
        mounts, env={"SLURM_JOB_ID": "1234567"}, hostname="meadow3-0200.hpc.local"
    )
    other = Snapshot.from_roots([_root("/project/x")], identity=compute, taken_at=T0 + DAY)
    assert other.node_class == COMPUTE
    assert fingerprint_mismatch(snapshot, compute) is None, "same cluster, so comparable"
    assert same_vantage(snapshot, other) is False, "different node class, so not the same view"


def test_an_unknown_reach_on_a_new_root_is_recorded_without_a_claim():
    """A root discovered for the first time whose probe failed is still `new`.

    `new` is a statement about the lineage, not about access, so it is the one
    label a failed probe does not suppress. The reach it reports is UNKNOWN,
    which is what was measured.
    """
    previous = _snapshot([_root("/project/x")], hostname="h1", klass=LOGIN, taken_at=T0)
    current = _snapshot(
        [
            _root("/project/x"),
            _root(
                "/project/slow",
                reach=Reach.UNKNOWN,
                identity=None,
                present=unknown(VerdictCategory.PROBE_TIMEOUT, "stat hung"),
            ),
        ],
        hostname="h1",
        klass=LOGIN,
        taken_at=T0 + DAY,
        discovery=_swept(),
    )
    result = diff(previous, current)
    assert result.labels() == ["new"]
    assert result.records[0].current == Reach.UNKNOWN
