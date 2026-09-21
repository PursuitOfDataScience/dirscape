"""Who you are, and which cluster you are standing on.

Two jobs, and each one exists because of a measured failure.

**Name resolution has to be allowed to fail.** On meadow2 compute nodes a uid
cannot be resolved to a name at all: ``getent passwd $UID`` returns empty and
Slurm itself renders the owner of a running job as ``nobody``. So every lookup
here is wrapped and degrades to the numeric id. A storage tool that raises
``KeyError: 'getpwuid(): uid not found'`` on a compute node is a tool that
cannot run where the jobs run.

**The snapshot key must identify the CLUSTER, not the node.** `cluster_fingerprint`
hashes the set of network storage device names, which is a property of the
storage fabric and reads the same from every node attached to it. The hostname
is deliberately excluded: including it would give every compute node its own
baseline, so nothing would ever be "new since last time" and the entire diff
layer would be decoration. Keying on the fabric is also what keeps meadow2,
meadow3, collie3 and Brook snapshots from colliding, since no two of them
export the same device names.
"""

import hashlib
import os
import socket
from typing import Dict, List, Optional, Sequence

from .mounts import MountTable, node_class

__all__ = [
    "Identity",
    "cluster_fingerprint",
    "group_names",
    "read_identity",
    "user_name",
]

try:
    import grp
except ImportError:  # pragma: no cover - non-POSIX, which this tool does not target
    grp = None  # type: ignore[assignment]

try:
    import pwd
except ImportError:  # pragma: no cover
    pwd = None  # type: ignore[assignment]


def user_name(uid=None, notes=None):
    # type: (Optional[int], Optional[List[str]]) -> str
    """The login name for a uid, falling back to the uid as a string.

    Never raises. The fallback is recorded in ``notes`` when one is passed, so
    the snapshot shows that the name was unavailable instead of quietly
    presenting a number as if it were a name.
    """
    if uid is None:
        uid = os.getuid()
    if pwd is not None:
        try:
            return pwd.getpwuid(uid).pw_name
        except (KeyError, OSError, OverflowError):
            # Measured on meadow2 compute nodes: name service is unavailable
            # there, so this is a normal state and not an error.
            if notes is not None:
                notes.append("uid %d does not resolve to a name on this node" % (uid,))
    return str(uid)


def group_names(gids=None, notes=None):
    # type: (Optional[Sequence[int]], Optional[List[str]]) -> List[str]
    """Group names for a list of gids, each falling back to its number.

    Order is preserved and duplicates removed, because callers probe candidate
    paths in this order and the primary group is the likeliest hit.
    """
    if gids is None:
        gids = os.getgroups()
    out = []  # type: List[str]
    unresolved = 0
    for gid in gids:
        name = None  # type: Optional[str]
        if grp is not None:
            try:
                name = grp.getgrgid(gid).gr_name
            except (KeyError, OSError, OverflowError):
                name = None
        if name is None:
            unresolved += 1
            name = str(gid)
        if name not in out:
            out.append(name)
    if unresolved and notes is not None:
        notes.append("%d of %d gids do not resolve to names" % (unresolved, len(gids)))
    return out


def cluster_fingerprint(mounts, length=10):
    # type: (MountTable, int) -> str
    """A short stable hash identifying the storage fabric.

    Hashes the sorted, deduplicated set of NETWORK device names. That set is
    identical on every node of a cluster and different on every cluster, which
    is exactly the property a snapshot key needs.

    ``sha256`` rather than ``sha1``: this is not a security use, but ``sha1``
    raises on a FIPS-enabled build and a tool that cannot compute its own
    snapshot key on a hardened host is broken on that host.

    Returns a ``(fingerprint, basis)`` pair via `read_identity`; on its own it
    returns just the digest. When there are no network devices at all (a laptop,
    a container) the non-pseudo mountpoints are hashed instead, so the key stays
    stable rather than collapsing to the hash of an empty string, which every
    such machine would share.
    """
    digest, _ = _fingerprint_with_basis(mounts, length)
    return digest


def _fingerprint_with_basis(mounts, length=10):
    # type: (MountTable, int) -> tuple
    devices = mounts.network_devices()
    basis = "network-devices"
    if not devices:
        devices = sorted({m.mountpoint for m in mounts.non_pseudo() if m.mountpoint})
        basis = "mountpoints"
    if not devices:
        basis = "empty"
    payload = "\n".join(devices).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()[:length], basis


class Identity(object):
    """The user, the node, and the cluster key, as one recordable object."""

    __slots__ = (
        "uid",
        "gid",
        "user",
        "gids",
        "groups",
        "hostname",
        "node_class",
        "fingerprint",
        "fingerprint_basis",
        "cluster",
        "notes",
    )

    def __init__(
        self,
        uid,  # type: int
        gid,  # type: int
        user,  # type: str
        gids=(),  # type: Sequence[int]
        groups=(),  # type: Sequence[str]
        hostname="",  # type: str
        node_class_="unknown",  # type: str
        fingerprint="",  # type: str
        fingerprint_basis="",  # type: str
        cluster="",  # type: str
        notes=(),  # type: Sequence[str]
    ):
        # type: (...) -> None
        self.uid = uid
        self.gid = gid
        self.user = user
        self.gids = list(gids)  # type: List[int]
        self.groups = list(groups)  # type: List[str]
        self.hostname = hostname
        # Recorded in the snapshot so a diff can say "this baseline was taken on
        # a login node" rather than reporting a login-only mount as removed.
        self.node_class = node_class_
        self.fingerprint = fingerprint
        self.fingerprint_basis = fingerprint_basis
        self.cluster = cluster
        self.notes = list(notes)  # type: List[str]

    @property
    def gid_set(self):
        # type: () -> frozenset
        """The gids to test directory ownership against.

        A frozenset because the ``dir-owner`` probe tests it once per directory
        entry, about 1,600 times on this cluster.
        """
        return frozenset(self.gids)

    def to_json(self):
        # type: () -> Dict[str, object]
        return {
            "uid": self.uid,
            "gid": self.gid,
            "user": self.user,
            "gids": list(self.gids),
            "groups": list(self.groups),
            "hostname": self.hostname,
            "node_class": self.node_class,
            "fingerprint": self.fingerprint,
            "fingerprint_basis": self.fingerprint_basis,
            "cluster": self.cluster,
            "notes": list(self.notes),
        }

    def __repr__(self):
        # type: () -> str
        return "Identity(%r, %s, %s)" % (self.user, self.node_class, self.fingerprint)


def read_identity(mounts, env=None, hostname=None, site=None):
    # type: (MountTable, Optional[Dict[str, str]], Optional[str], Optional[object]) -> Identity
    """Collect everything about the caller and the node, raising nothing.

    ``env`` and ``hostname`` are injectable so the whole object can be built
    for a recorded cluster from anywhere, which is what lets the discovery tests
    run off this host.

    ``site`` is an optional `sitecfg.Site`; only its ``name`` is consulted, and
    only as the preferred cluster label. It is never loaded here: the caller
    owns config loading, so this package has no opinion about ``/etc``.
    """
    environ = os.environ if env is None else env
    notes = []  # type: List[str]

    uid = os.getuid()
    gid = os.getgid()
    user = user_name(uid, notes)
    # An explicit USER in the environment is not trusted over the kernel: it is
    # trivially wrong inside an su or a container, and the uid is not.
    gids = list(os.getgroups())
    if gid not in gids:
        # getgroups() omits the primary group on some kernels. A missing primary
        # gid would make the dir-owner probe blind to the user's own directories.
        gids.insert(0, gid)
    groups = group_names(gids, notes)

    if hostname is None:
        try:
            hostname = socket.gethostname()
        except OSError:
            hostname = ""
            notes.append("hostname unavailable")

    fingerprint, basis = _fingerprint_with_basis(mounts)
    if basis != "network-devices":
        notes.append("fingerprint keyed on %s: no network storage devices found" % (basis,))

    cluster = ""
    site_name = getattr(site, "name", "") if site is not None else ""
    if site_name:
        cluster = str(site_name)
    else:
        # Imported here rather than at module scope to keep the dependency one
        # way: sitecfg knows nothing about discovery, and discovery reaches for
        # exactly one helper out of it.
        from ..sitecfg import guess_cluster_name

        cluster = guess_cluster_name(hostname or "", mounts.network_devices())

    return Identity(
        uid=uid,
        gid=gid,
        user=user,
        gids=gids,
        groups=groups,
        hostname=hostname or "",
        node_class_=node_class(environ, hostname),
        fingerprint=fingerprint,
        fingerprint_basis=basis,
        cluster=cluster,
        notes=notes,
    )
