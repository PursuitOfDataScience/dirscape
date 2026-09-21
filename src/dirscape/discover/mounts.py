"""The mount table: what is mounted here, and what kind of thing each mount is.

Read from ``/proc/self/mounts``, **not** from ``df``. That is a measured
decision, not a preference. On this cluster ``df -hT`` prints six gpfs rows,
all of them ``/gpfs/<cluster>/<pool>``, and hides ``/home``, ``/project``,
``/project2``, ``/scratch/meadow3``, ``/scratch/meadow2``, ``/scratch/collie3``,
``/software``, ``/programs`` and ``/collie3``. Those nine are real mounts of the
same devices, they are the paths every user actually types, and a tool built on
``df`` cannot see any of them. ``/proc/self/mounts`` lists all fifteen.

Three further things this module exists to get right:

**Kernel escapes.** A mount field containing a space, tab, newline or backslash
is written by the kernel as a three-digit octal escape. Unescaping is not
cosmetic: a mountpoint of ``/data/my share`` arrives as ``/data/my\\040share``
and any path comparison against it silently fails.

**An unknown filesystem type is kept, not dropped.** The classification below
is generous but it cannot be complete, and the failure directions are not
symmetric: mislabelling a pseudo filesystem as storage costs one noisy row,
while dropping an unrecognised real filesystem (weka, daos, virtiofs, cifs)
hides storage the user has. So anything unclassified is kept with
``unclassified`` set, and `non_pseudo` returns it.

**Mount visibility is a property of the node.** ``/cfs3`` is mounted on login
nodes only, so the same filesystem is present or absent depending on where you
are standing. `node_class` is here so the snapshot can record which kind of node
produced it, rather than reporting a login-only mount as vanished.

Python 3.6 compatible: type comments, no dataclasses, no f-strings.
"""

import os
import re
import socket
from typing import Dict, Iterator, List, Optional, Sequence

__all__ = [
    "MOUNT_TABLE_PATH",
    "NETWORK_FSTYPES",
    "LOCAL_FSTYPES",
    "PSEUDO_FSTYPES",
    "PSEUDO_LOCATIONS",
    "KIND_NETWORK",
    "KIND_LOCAL",
    "KIND_PSEUDO",
    "KIND_OTHER",
    "Mount",
    "MountTable",
    "classify_fstype",
    "read_mount_table",
    "unescape_field",
    "node_class",
]


MOUNT_TABLE_PATH = "/proc/self/mounts"


# Network and parallel filesystems: the ones with a quota backend worth asking
# about and a latency worth putting a deadline on.
NETWORK_FSTYPES = frozenset(
    [
        "gpfs",
        "lustre",
        "nfs",
        "nfs4",
        "beegfs",
        "ceph",
        "panfs",
        # Present at other sites; harmless here and cheaper than a bug report.
        "cifs",
        "smb3",
        "glusterfs",
        "orangefs",
        "pvfs2",
        "afs",
        "daos",
        "wekafs",
        "9p",
        "virtiofs",
        "fuse.sshfs",
        "fuse.glusterfs",
        "fuse.ceph",
    ]
)

# Local disk and memory filesystems. tmpfs is here rather than in the pseudo set
# because ``/dev/shm`` and a node-local ``/tmp`` are real space a job can fill.
LOCAL_FSTYPES = frozenset(
    [
        "xfs",
        "ext2",
        "ext3",
        "ext4",
        "btrfs",
        "zfs",
        "f2fs",
        "jfs",
        "reiserfs",
        "nilfs2",
        "vfat",
        "exfat",
        "ntfs",
        "ntfs3",
        "tmpfs",
        "ramfs",
        "overlay",
        "squashfs",
        "iso9660",
    ]
)

# Kernel bookkeeping. Nobody has an allocation on any of these. The list is
# long because the live table here carries 16 cgroup mounts plus tracefs,
# pstore, mqueue, hugetlbfs, debugfs, configfs, bpf, binfmt_misc, autofs,
# rpc_pipefs, efivarfs, securityfs and devpts: 30 of 56 lines are noise.
PSEUDO_FSTYPES = frozenset(
    [
        "proc",
        "sysfs",
        "cgroup",
        "cgroup2",
        "devtmpfs",
        "devpts",
        "securityfs",
        "selinuxfs",
        "tracefs",
        "debugfs",
        "configfs",
        "pstore",
        "bpf",
        "mqueue",
        "hugetlbfs",
        "binfmt_misc",
        "autofs",
        "rpc_pipefs",
        "efivarfs",
        "fusectl",
        "nsfs",
        "pipefs",
        "sockfs",
        "bdev",
        "rootfs",
    ]
)

# A tmpfs or fuse mount under one of these is kernel plumbing whatever its
# fstype says. Measured on this node: ``/sys/fs/cgroup`` is a tmpfs and
# ``/run/user/<uid>/doc`` is a fuse mount, so classifying on fstype alone keeps
# eight mounts nobody could have an allocation on.
PSEUDO_LOCATIONS = ("/proc", "/sys", "/dev", "/run")

KIND_NETWORK = "network"
KIND_LOCAL = "local"
KIND_PSEUDO = "pseudo"
KIND_OTHER = "other"


# Exactly the four sequences ``mangle_path`` in the kernel emits are space
# (\040), tab (\011), newline (\012) and backslash (\134). The pattern matches
# any three octal digits because that is the same amount of code and tolerates a
# kernel that grows a fifth. Crucially, ``re.sub`` scans left to right and never
# rescans its own output, so ``\134040`` decodes to a literal backslash followed
# by "040" rather than being double-decoded into a space.
_OCTAL_ESCAPE = re.compile(r"\\([0-7]{3})")


def unescape_field(field):
    # type: (str) -> str
    """Decode the kernel's octal escapes in one mount-table field."""
    if "\\" not in field:
        return field
    return _OCTAL_ESCAPE.sub(lambda match: chr(int(match.group(1), 8)), field)


def classify_fstype(fstype, mountpoint=""):
    # type: (str, str) -> str
    """One of the four KIND_* constants.

    Unknown types come back as ``KIND_OTHER`` rather than being lumped in with
    pseudo, so an unrecognised real filesystem stays visible. See the module
    docstring for why the two failure directions are not equivalent.
    """
    lowered = (fstype or "").lower()
    if lowered in PSEUDO_FSTYPES:
        return KIND_PSEUDO
    if lowered in ("tmpfs", "ramfs") or lowered == "fuse" or lowered.startswith("fuse."):
        for prefix in PSEUDO_LOCATIONS:
            if mountpoint == prefix or mountpoint.startswith(prefix + "/"):
                return KIND_PSEUDO
    if lowered in NETWORK_FSTYPES:
        return KIND_NETWORK
    if lowered in LOCAL_FSTYPES:
        return KIND_LOCAL
    return KIND_OTHER


class Mount(object):
    """One line of the mount table, unescaped and classified."""

    __slots__ = ("device", "mountpoint", "fstype", "options", "kind", "order")

    def __init__(self, device, mountpoint, fstype, options="", order=0):
        # type: (str, str, str, str, int) -> None
        self.device = device
        self.mountpoint = mountpoint
        self.fstype = fstype
        self.options = options
        self.kind = classify_fstype(fstype, mountpoint)
        # Position in the table. Kept because when two mounts share a
        # mountpoint the later line is the one you actually reach, which is the
        # kernel's own overmount rule.
        self.order = order

    @property
    def is_network(self):
        # type: () -> bool
        return self.kind == KIND_NETWORK

    @property
    def is_local(self):
        # type: () -> bool
        return self.kind == KIND_LOCAL

    @property
    def is_pseudo(self):
        # type: () -> bool
        return self.kind == KIND_PSEUDO

    @property
    def unclassified(self):
        # type: () -> bool
        """True when this filesystem type is not in any of the three sets.

        Surfaced rather than hidden: the caller adds a note saying the type was
        not recognised, which is how a site running something this package has
        never heard of finds out why its label is vague.
        """
        return self.kind == KIND_OTHER

    @property
    def option_list(self):
        # type: () -> List[str]
        return [part for part in self.options.split(",") if part]

    def has_option(self, name):
        # type: (str) -> bool
        """Whether a bare option is present, ignoring ``key=value`` options.

        Used for ``noquota`` / ``prjquota``, which is how the xfs branch of
        `attribute` refutes the quota question without running anything.
        """
        return name in self.option_list

    @property
    def read_only(self):
        # type: () -> bool
        return self.has_option("ro")

    def to_json(self):
        # type: () -> Dict[str, object]
        return {
            "device": self.device,
            "mountpoint": self.mountpoint,
            "fstype": self.fstype,
            "options": self.options,
            "kind": self.kind,
        }

    def __repr__(self):
        # type: () -> str
        return "Mount(%r, %r, %s)" % (self.device, self.mountpoint, self.fstype)


class MountTable(object):
    """Every mount, with the lookups the rest of the package needs."""

    __slots__ = ("mounts", "source")

    def __init__(self, mounts=(), source=""):
        # type: (Sequence[Mount], str) -> None
        self.mounts = list(mounts)  # type: List[Mount]
        self.source = source

    def __iter__(self):
        # type: () -> Iterator[Mount]
        return iter(self.mounts)

    def __len__(self):
        # type: () -> int
        return len(self.mounts)

    def mounts_of_type(self, fstypes):
        # type: (Sequence[str]) -> List[Mount]
        wanted = self._as_set(fstypes)
        return [m for m in self.mounts if m.fstype.lower() in wanted]

    def devices_of_type(self, fstypes):
        # type: (Sequence[str]) -> List[str]
        """Sorted, deduplicated device names for the given filesystem types.

        Deduplicated because one device is mounted at four paths here, so the
        raw list would repeat ``meadow3_cap`` four times and any fingerprint
        built from it would depend on how many aliases a node happened to mount.
        """
        return sorted({m.device for m in self.mounts_of_type(fstypes) if m.device})

    def network_devices(self):
        # type: () -> List[str]
        return sorted({m.device for m in self.mounts if m.is_network and m.device})

    def non_pseudo(self):
        # type: () -> List[Mount]
        """Every mount that could plausibly hold a user's data."""
        return [m for m in self.mounts if not m.is_pseudo]

    def at(self, mountpoint):
        # type: (str) -> List[Mount]
        normalised = _normalise(mountpoint)
        return [m for m in self.mounts if m.mountpoint == normalised]

    def enclosing_mount(self, path):
        # type: (str) -> Optional[Mount]
        """The mount that governs a path, longest matching mountpoint winning.

        Longest wins because the mountpoints nest: ``/scratch/meadow3`` is a
        different device from ``/`` and a prefix scan that stopped at the first
        hit would attribute every scratch path to the root filesystem.

        Deliberately does NOT resolve symlinks. ``realpath`` on a wedged network
        mount blocks, and the caller asked about the path it named.
        """
        target = _normalise(path)
        best = None  # type: Optional[Mount]
        best_len = -1
        for mount in self.mounts:
            point = mount.mountpoint
            if target == point or target.startswith(point.rstrip("/") + "/"):
                # ">=" so a later line wins a tie: two mounts on one mountpoint
                # means the second one is overmounted on the first, and the
                # second is the one you reach.
                if len(point) >= best_len:
                    best, best_len = mount, len(point)
        return best

    def to_json(self):
        # type: () -> Dict[str, object]
        return {"source": self.source, "mounts": [m.to_json() for m in self.mounts]}

    @staticmethod
    def _as_set(fstypes):
        # type: (Sequence[str]) -> frozenset
        # A bare string would iterate as characters and match nothing, silently.
        if isinstance(fstypes, str):
            fstypes = (fstypes,)
        return frozenset(f.lower() for f in fstypes)


def _normalise(path):
    # type: (str) -> str
    if not path:
        return ""
    cleaned = os.path.normpath(path)
    return cleaned


def read_mount_table(path=MOUNT_TABLE_PATH, text=None):
    # type: (str, Optional[str]) -> MountTable
    """Parse a mount table.

    ``text`` short-circuits the file read so a test can supply a table from
    another cluster without owning a node on it. An unreadable file yields an
    empty table rather than raising: a tool whose job is to report what it found
    should still start when ``/proc`` is not where it expected.
    """
    source = path if text is None else "text"
    if text is None:
        try:
            with open(path, "r") as handle:
                text = handle.read()
        except OSError:
            return MountTable([], source=path)

    mounts = []  # type: List[Mount]
    for index, line in enumerate(text.splitlines()):
        if not line.strip():
            continue
        # Splitting on whitespace is safe precisely because the kernel escapes
        # every space it emits inside a field.
        fields = line.split()
        if len(fields) < 3:
            continue
        mounts.append(
            Mount(
                device=unescape_field(fields[0]),
                mountpoint=unescape_field(fields[1]),
                fstype=unescape_field(fields[2]),
                options=unescape_field(fields[3]) if len(fields) > 3 else "",
                order=index,
            )
        )
    return MountTable(mounts, source=source)


# --------------------------------------------------------------------------
# Which kind of node is this
# --------------------------------------------------------------------------

# Compute node names, after the domain is stripped. Deliberately narrow: an
# unrecognised name returns "unknown" rather than guessing, because the only
# thing worse than not knowing the node class is being confidently wrong about
# it and attributing a missing login-only mount to a change in the filesystem.
_COMPUTE_NAME = re.compile(
    r"^[a-z][a-z0-9]*[-_](?:\d+|bigmem\d*|gpu\d*|amd\d*|himem\d*|mem\d*)$",
    re.IGNORECASE,
)

# Every variable Slurm sets inside a job step. Presence of any one of them is
# taken as proof of a compute node.
_SLURM_JOB_VARS = ("SLURM_JOB_ID", "SLURM_NODEID", "SLURM_JOBID", "SLURM_STEP_ID")


def node_class(env=None, hostname=None):
    # type: (Optional[Dict[str, str]], Optional[str]) -> str
    """ "login", "compute" or "unknown".

    Exists because mount visibility is a property of the node, not of the
    filesystem: ``/cfs3`` is mounted on login nodes only, so a snapshot taken on
    a compute node must record that fact or the next diff reports a whole
    filesystem as having disappeared.

    Slurm's environment is the strong signal and the hostname pattern is a weak
    fallback, in that order. Both are injectable so no test has to read the host
    it runs on, which is rapiDU's RD-10.

    Measured case for keeping the fallback weak: this package was developed on
    ``meadow3-0200``, a compute node reached through a durable tmux where no
    ``SLURM_*`` variable survives in the environment. The hostname is the only
    evidence there, and it is still only evidence.
    """
    environ = os.environ if env is None else env
    for name in _SLURM_JOB_VARS:
        if str(environ.get(name) or "").strip():
            return "compute"

    if hostname is None:
        try:
            hostname = socket.gethostname()
        except OSError:
            hostname = ""
    short = (hostname or "").split(".")[0]
    if not short:
        return "unknown"
    lowered = short.lower()
    # Checked before the compute pattern: "meadow3-login1" would otherwise fall
    # through to the digit-suffix rule and be called a compute node.
    if "login" in lowered or lowered.startswith(("bastion", "gateway", "jump")):
        return "login"
    if _COMPUTE_NAME.match(short):
        return "compute"
    return "unknown"
