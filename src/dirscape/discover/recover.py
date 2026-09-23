"""Read-only copies of a path that the filesystem is still keeping.

The question this answers is the one people arrive with after `rm -rf`, and
until now the tool could not answer it at all: **is there a copy of this
directory from before, and what is the literal path to it?**

Snapshots are invisible to every other discovery source and that is not an
oversight in those sources, it is what a snapshot directory IS. Measured on
this cluster:

* **Not a mount.** `/proc/self/mounts` has sixteen GPFS entries and none of
  them is a snapshot. The mount source cannot see one.
* **Not owned by anyone you can match.** `/gpfs/meadow3/cap/.snapshots` is
  `root:root`, so the dir-owner source skips it, and no group template
  produces the name.
* **Not where the documentation says.** The site publishes
  `/snapshots/<SNAP>/home/<user>` and states it is login-node only. On a
  compute node `/snapshots` genuinely does not exist, and yet
  `/gpfs/meadow3/cap/.snapshots/<SNAP>/home/<user>` is right there and
  readable. A user in a batch job is told recovery is impossible when it is
  one stat away.
* **Free to enumerate.** `ls /gpfs/meadow3/cap/.snapshots` is 0.001s: it is a
  directory read of eleven entries, not a tree walk, so this module keeps the
  package's promise that cost is proportional to roots and never to files.

Four rules came out of measuring it, and each one is a trap avoided:

**1. `(st_dev, st_ino)` is NOT unique across snapshots, so a copy can never be
a `Root`.** Measured: the live `/home/jdoe42` and its copies inside
`daily-2026-09-18`, `daily-2026-09-19` and `daily-2026-09-20` all report
`dev=54 ino=212501245`. The identical quadruple. `candidates._dedupe` keys on
exactly that pair, so any snapshot offered as a candidate root is silently
folded onto the live path and vanishes. Copies are therefore an ATTRIBUTE of
a root and never a row of their own. This is the opposite of the filesystem
root case that `_rank_of_mount` documents, where the inodes correctly differ.

**2. The path inside a snapshot has two possible shapes and guessing is
wrong.** GPFS snapshots the whole filesystem, so the copy of `/home/jdoe42`
sits at `<fsroot>/.snapshots/<snap>/home/jdoe42`: the ABSOLUTE path, minus its
leading slash, even though `/home` is a fileset junction mounted somewhere
else entirely. NetApp and ZFS snapshot a mounted dataset, so the copy sits at
`<mount>/.snapshot/<snap>/<path relative to that mount>`. Both forms are
probed and whichever opens is the answer, cached per snapshot directory so the
loser is tried once rather than once per root.

**3. An empty snapshot directory is a REFUSAL and the most valuable thing
here.** `/gpfs/meadow3/perf/.snapshots` and `/gpfs/collie3/perf/.snapshots`
both exist and both hold zero snapshots, and those two filesystems are every
`/scratch` on this cluster. "Deleted from scratch is gone" is a durable,
measured no, and it is worth more to a user than any of the yes answers.

**4. No snapshot directory at all is NOT a refusal.** A site can back up to
tape, to `mmbackup`, to anything, without exposing a snapshot tree. Silence
here means unpublished, exactly as it does for the `backup` policy field, so
it stays `unknown` and renders as `?`.

**5. A site can publish its snapshots somewhere the filesystem does not, and
then nothing in the mount table leads you there.** This one exposes
`/snapshots` on login nodes: a plain, unhidden, top-level directory that is
not a subdirectory of any root and does not belong to any one device. It is
therefore unreachable from `_bases_for`, which works outward from the root's
own device, so it has to be DECLARED, either in `site.conf` under
`[snapshots] roots` or by a site plugin. Two shapes are both in use here and
both are recognised by looking rather than by configuring:

    /snapshots/<SNAP>/home/<user>            meadow3: snapshots directly
    /snapshots/home/<SNAP>/home/<user>       meadow2: one level per filesystem

`SnapshotIndex.containers` tells them apart by asking whether the entries
parse as snapshot names, and descends exactly one level when they do not.
"""

import os
import re
import time
from typing import Dict, List, Optional, Sequence, Tuple

from ..model import Root, SnapshotCopy, Verdict, VerdictCategory, confirmed, refuted, unknown
from ..runner import Budget
from .access import DEFAULT_DEADLINE_S, with_deadline
from .mounts import SNAPSHOT_DIRS, MountTable

__all__ = [
    "SNAPSHOT_DIRS",
    "SNAPSHOT_CAP",
    "parse_snapshot_time",
    "SNAPSHOT_ROOT_FANOUT",
    "SnapshotIndex",
    "copies_for_path",
    "find_snapshots",
]


# `SNAPSHOT_DIRS` itself lives in `mounts`, imported above, because the mount
# table is where a snapshot first appears and the cluster key has to be able
# to leave snapshot mounts out without importing this module.

# Upper bound on subdirectories descended into when a declared snapshot root
# turns out to hold one directory per filesystem rather than the snapshots
# themselves. A site has a handful of filesystems, not hundreds, and a root
# that has been pointed at the wrong directory must not turn into a sweep.
SNAPSHOT_ROOT_FANOUT = 32

# Upper bound on snapshots read from one directory. A retention policy is
# normally tens of entries; a misconfigured hourly policy can be thousands,
# and this module's promise is that it costs a directory read, not a sweep.
SNAPSHOT_CAP = 200

# A date in a snapshot name, in the three separator styles seen in the wild:
# `daily-2026-09-20.05h30`, `hourly.2026.09.20-0530`, `snap_20260920`.
_DATE = re.compile(r"(?<!\d)(\d{4})[-._]?(\d{2})[-._]?(\d{2})(?!\d)")
# The time part, if there is one. `05h30` is this site's; `05.30`, `05:30`,
# `0530` and `05-30` are the other forms, and all five are read the same way.
_TIME = re.compile(r"(?<!\d)([01]\d|2[0-3])[h:._-]?([0-5]\d)(?!\d)")


def parse_snapshot_time(name):
    # type: (str) -> Optional[float]
    """When a snapshot was taken, read from its NAME, or None.

    **The name is the only honest source and the metadata is a decoy.**
    Measured here: every directory under `/gpfs/meadow3/cap/.snapshots` stats
    as `mtime 2021-08-04 05:23:07`, down to the microsecond, whether it was
    taken this morning or four weeks ago. That is the fileset's creation time
    showing through, so `st_mtime` would date the whole retention window to
    one day in 2021 and an "age" column built on it would be confidently
    wrong for every row.

    A name this cannot read leaves the time unknown. Ordering then falls back
    to the name itself, which sorts correctly for every scheme that leads with
    a date and is no worse than a fabricated timestamp for the rest.
    """
    date = _DATE.search(name or "")
    if not date:
        return None
    year, month, day = (int(part) for part in date.groups())
    # Checked rather than left to `mktime`, which NORMALISES instead of
    # raising: `2026-13-45` becomes 2027-02-14 and a nonsense name would get a
    # confident, wrong date a renderer would then print as an age.
    if not (1 <= month <= 12 and 1 <= day <= 31):
        return None
    hour, minute = 0, 0
    clock = _TIME.search(name[date.end() :])
    if clock:
        hour, minute = int(clock.group(1)), int(clock.group(2))
    try:
        # Local time, because that is what a site names its snapshots in and
        # the figure is only ever rendered as an age or a date to a user
        # standing on the same cluster.
        return time.mktime((year, month, day, hour, minute, 0, 0, 1, -1))
    except (ValueError, OverflowError):
        return None


def _listdir(path, deadline_s):
    # type: (str, Optional[float]) -> Optional[List[str]]
    """Entry names, or None when the directory could not be read.

    None and `[]` are different answers and the caller depends on it: an
    unreadable snapshot directory is unknown, an empty one is a refusal.
    """

    def read():
        # type: () -> List[str]
        return sorted(os.listdir(path))[:SNAPSHOT_CAP]

    finished, value, exc, _elapsed = with_deadline(read, deadline_s)
    if not finished or exc is not None or value is None:
        return None
    return value  # type: ignore[return-value]


def _isdir(path, deadline_s):
    # type: (str, Optional[float]) -> bool
    """Whether ``path`` is a directory. Used only when descending a root."""
    finished, value, exc, _elapsed = with_deadline(lambda: os.path.isdir(path), deadline_s)
    return bool(finished and exc is None and value)


def _exists(path, deadline_s):
    # type: (str, Optional[float]) -> bool
    """Whether anything is at ``path``, without following a final symlink.

    `os.path.isdir` was wrong here and the reason is the whole point of
    `dirscape recover`: the thing a user is looking for has usually been
    DELETED, and it was as often a file as a directory. `lexists` also keeps
    a copy of a dangling symlink, which is still evidence of what was there.
    """
    finished, value, exc, _elapsed = with_deadline(lambda: os.path.lexists(path), deadline_s)
    return bool(finished and exc is None and value)


def _relative_to(path, base):
    # type: (str, str) -> Optional[str]
    """``path`` expressed relative to ``base``, or None if it is not under it."""
    base = base.rstrip("/")
    path = path.rstrip("/")
    if not base or base == "/":
        return path.lstrip("/")
    if path == base:
        return ""
    if path.startswith(base + "/"):
        return path[len(base) + 1 :]
    return None


class SnapshotIndex(object):
    """Every snapshot directory on this node, read at most once each.

    One filesystem carries many roots. `/home`, `/project` and `/software` are
    three mountpoints of `meadow3_cap` here and a user holds roots under all
    three, so a per-root listing would read the same eleven-entry directory
    once per root. The index reads it once and remembers the layout it proved.
    """

    __slots__ = ("_snaps", "_layout", "_containers", "deadline_s", "snapdirs", "roots")

    def __init__(self, deadline_s=DEFAULT_DEADLINE_S, snapdirs=SNAPSHOT_DIRS, roots=()):
        # type: (Optional[float], Sequence[str], Sequence[str]) -> None
        # (base, snapdir) -> list of names, or None when unreadable.
        self._snaps = {}  # type: Dict[Tuple[str, str], Optional[List[str]]]
        # (base, snapdir) -> "absolute" | "relative", once one has been proven.
        self._layout = {}  # type: Dict[Tuple[str, str], str]
        self.deadline_s = deadline_s
        # Injectable, and the test suite depends on it for a reason worth
        # recording: **GPFS conjures an empty `.snapshots` inside EVERY
        # directory**, including a fresh `tmp_path`, and that directory is
        # read-only, so a test cannot build a fake snapshot tree under the
        # real name on the filesystem this suite runs on. Overriding the name
        # exercises the same code against a directory the filesystem will not
        # intercept.
        self.snapdirs = tuple(snapdirs)
        # Site-declared containers, resolved once by `containers()`.
        self.roots = tuple(roots)
        self._containers = None  # type: Optional[List[str]]

    def names(self, base, snapdir):
        # type: (str, str) -> Optional[List[str]]
        """Snapshot names under ``<base>/<snapdir>``, newest LAST, or None.

        None means the directory is absent or unreadable. An empty list means
        it exists and the filesystem is keeping nothing, which is a durable
        answer and not a failure.
        """
        key = (base, snapdir)
        if key not in self._snaps:
            where = os.path.join(base, snapdir) if snapdir else base
            self._snaps[key] = _listdir(where, self.deadline_s)
        return self._snaps[key]

    def containers(self):
        # type: () -> List[str]
        """The declared snapshot roots, resolved to directories of snapshots.

        A declared root is used as-is when its own entries look like snapshot
        names, and otherwise each of its subdirectories is tried one level
        down. Both shapes are in use on this site and neither is worth making
        an administrator describe:

            /snapshots/daily-2026-09-22.05h30/...   the root IS the container
            /snapshots/home/daily-.../...           one container per filesystem

        Deciding by LOOKING rather than by configuration also means a site
        that changes shape does not need its `site.conf` edited, and a root
        pointed somewhere useless costs one listing and yields nothing.
        """
        if self._containers is not None:
            return self._containers

        found = []  # type: List[str]
        for root in self.roots:
            root = (root or "").rstrip("/")
            if not root or root in found:
                continue
            names = _listdir(root, self.deadline_s)
            if names is None:
                continue
            if any(parse_snapshot_time(name) is not None for name in names):
                found.append(root)
                continue
            for name in names[:SNAPSHOT_ROOT_FANOUT]:
                child = os.path.join(root, name)
                if child not in found and _isdir(child, self.deadline_s):
                    found.append(child)
        self._containers = found
        return found

    def copy_path(self, base, snapdir, name, target):
        # type: (str, str, str, str) -> Optional[str]
        """Where ``target`` lives inside one snapshot, if it lives there.

        Tries the proven layout first and falls back to the other one, so the
        two-form probe of rule 2 costs a wasted stat once per snapshot
        directory rather than once per root.
        """
        stem = os.path.join(base, snapdir, name) if snapdir else os.path.join(base, name)
        forms = []  # type: List[Tuple[str, str]]
        absolute = os.path.join(stem, target.lstrip("/"))
        forms.append(("absolute", absolute))
        relative = _relative_to(target, base)
        if relative is not None:
            candidate = os.path.join(stem, relative) if relative else stem
            if candidate != absolute:
                forms.append(("relative", candidate))

        proven = self._layout.get((base, snapdir))
        if proven:
            forms.sort(key=lambda pair: 0 if pair[0] == proven else 1)

        for layout, candidate in forms:
            if _exists(candidate, self.deadline_s):
                self._layout[(base, snapdir)] = layout
                return candidate
        return None


def _bases_for(path, device, mounts):
    # type: (str, str, Optional[MountTable]) -> List[str]
    """Directories that could hold a snapshot tree covering this root.

    **Every mountpoint of the same device, not a guess at which one is the
    filesystem root.** There is no way to pick the root out of the mount table
    by inspection and the obvious rules are both wrong here:

    * *shortest path*: `meadow3_cap` is mounted at `/home`, `/project`,
      `/programs`, `/software` and `/gpfs/meadow3/cap`. `/home` is the
      shortest and it is a fileset junction, whose `.snapshots` exists and is
      EMPTY. Choosing it reports "this filesystem keeps nothing" about a
      filesystem with eleven snapshots. That was the first version of this
      function and it got `/home/jdoe42` exactly backwards.
    * *deepest path*: correct here and wrong on any site that mounts its
      filesystem root at `/` and its filesets below it.

    So the question goes to the filesystem instead. Each mountpoint is tried
    and the one with a populated snapshot tree answers; a `.snapshots` that
    exists and is empty contributes only the knowledge that the mechanism is
    exposed. Every listing is cached in the `SnapshotIndex` by `(base,
    snapdir)`, so the cost is per device and not per root: five mountpoints
    times four directory names is twenty stats for `meadow3_cap`, once, no
    matter how many roots sit on it.

    The enclosing mountpoint and the root's own path are added last, for a
    filesystem that exposes a snapshot directory beside the data rather than
    only at the top.
    """
    path = (path or "").rstrip("/")
    if not path:
        return []
    found = []  # type: List[str]

    def offer(candidate):
        # type: (str) -> None
        candidate = (candidate or "").rstrip("/") or "/"
        if candidate and candidate not in found:
            found.append(candidate)

    enclosing = None
    if mounts is not None:
        enclosing = mounts.enclosing_mount(path)
        if not device and enclosing is not None:
            # A target that does not exist has no Root and therefore no
            # device. The mount it WOULD have lived in still names one, and
            # that is the whole point of `dirscape recover`: the file is gone.
            device = getattr(enclosing, "device", "")
        if device:
            same = [
                m.mountpoint
                for m in mounts.non_pseudo()
                if getattr(m, "device", "") == device and m.mountpoint
            ]
            # Shortest first only so the order is stable between runs, which
            # keeps `--json` diffable. Nothing depends on which one wins.
            for mountpoint in sorted(same, key=lambda p: (len(p.rstrip("/")), p)):
                offer(mountpoint)
        if enclosing is not None:
            offer(getattr(enclosing, "mountpoint", ""))
    offer(path)
    return found


def find_snapshots(
    roots,  # type: Sequence[Root]
    mounts=None,  # type: Optional[MountTable]
    budget=None,  # type: Optional[Budget]
    deadline_s=DEFAULT_DEADLINE_S,  # type: Optional[float]
    index=None,  # type: Optional[SnapshotIndex]
    snapshot_roots=(),  # type: Sequence[str]
):
    # type: (...) -> SnapshotIndex
    """Attach `snapshots` and `recoverable` to every root. Returns the index.

    Roots with no path (an allocation with nowhere to stand) and roots that
    were never reachable are skipped rather than probed: asking the filesystem
    about a path this node does not have would spend the budget to learn
    nothing, and `unknown` is already the honest starting state.
    """
    index = index or SnapshotIndex(deadline_s=deadline_s, roots=snapshot_roots)
    for root in roots:
        if budget is not None and getattr(budget, "exhausted", False):
            root.recoverable = unknown(
                VerdictCategory.PROBE_TIMEOUT,
                "the run's time allowance ran out before snapshots were checked",
                source="snapshots",
            )
            continue
        if not root.path or not getattr(root, "reachable", True):
            continue
        root.snapshots, root.recoverable = copies_for_path(
            root.path, mounts, getattr(root, "device", ""), index
        )
    return index


def copies_for_path(
    path,  # type: str
    mounts=None,  # type: Optional[MountTable]
    device="",  # type: str
    index=None,  # type: Optional[SnapshotIndex]
    deadline_s=DEFAULT_DEADLINE_S,  # type: Optional[float]
    snapshot_roots=(),  # type: Sequence[str]
):
    # type: (...) -> Tuple[List[SnapshotCopy], Verdict]
    """``(copies newest first, verdict)`` for any path, existing or not.

    **The path does not have to exist, and that is the point.** A user reaches
    for this after deleting something, so the live path is gone, no `Root`
    describes it and no quota scope owns it. Everything needed to find the
    copies survives anyway: the mount it used to live in names the device, the
    device names the mountpoints, and the snapshot trees are under those.
    """
    index = index or SnapshotIndex(deadline_s=deadline_s, roots=snapshot_roots)
    started = time.time()
    copies = []  # type: List[SnapshotCopy]
    kept_anywhere = 0
    saw_directory = False

    # `("", container)` pairs as well as `(base, snapdir)` ones: a declared
    # container IS the directory of snapshots, so there is no subdirectory
    # name to append. Site-declared roots come last, so a filesystem that
    # answers for itself decides the layout cache before a site-wide tree
    # gets a say.
    places = [
        (base, snapdir) for base in _bases_for(path, device, mounts) for snapdir in index.snapdirs
    ]
    places.extend((container, "") for container in index.containers())

    # **One snapshot, one row, however many routes reach it.** NetApp exposes
    # `.snapshot` inside EVERY directory, so a path under a NetApp mount is
    # found twice: once from the mountpoint and once from the path's own
    # base. Measured on an ACME login node, where `dirscape recover
    # /soft/applications` reported "6 copies" that were three snapshots
    # listed twice each:
    #
    #     hourly.2026-09-22_1705   /soft/.snapshot/hourly.../applications
    #     hourly.2026-09-22_1705   /soft/applications/.snapshot/hourly...
    #
    # Deduplicated by NAME and deliberately not by `(st_dev, st_ino)`, which
    # is the obvious key and the wrong one: rule 1 above records that GPFS
    # gives every snapshot of a directory the SAME inode as the live path, so
    # an inode key would collapse eleven genuinely different dates into one.
    # A snapshot name is unique within a filesystem, and one absolute path
    # cannot be inside two filesystems, so the name is enough.
    seen = set()
    for base, snapdir in places:
        declared = not snapdir
        if declared and not path.strip("/"):
            # A declared container holds snapshots of SOME filesystem, and
            # `<container>/<snapshot>/` joined with `/` is the snapshot
            # directory itself, which always exists. Measured on a meadow2
            # login node: `/`, a tmpfs, reported "18 snapshot copies of this
            # path can be read back" out of `/snapshots/home`.
            continue
        names = index.names(base, snapdir)
        if names is None:
            continue
        if not declared:
            # Only the filesystem's OWN snapshot directories say what this
            # filesystem keeps. A site-wide tree may cover other filesystems
            # entirely, so counting it told `/tmp`, a tmpfs, that "18
            # snapshot(s) exist on this filesystem".
            saw_directory = True
            kept_anywhere += len(names)
        for name in names:
            if name in seen:
                continue
            found = index.copy_path(base, snapdir, name, path)
            if found:
                seen.add(name)
                copies.append(SnapshotCopy(name, found, parse_snapshot_time(name)))

    elapsed = time.time() - started

    if copies:
        # Newest first, which is the order a person restoring a file wants and
        # the order every renderer here assumes. Undated names sort by name
        # descending, which is right for every scheme that leads with a date
        # and arbitrary but stable for the rest.
        copies.sort(key=lambda c: (c.taken_at is not None, c.taken_at or 0.0, c.name), reverse=True)
        return copies, confirmed(
            "%d snapshot cop%s of this path can be read back, newest at %s"
            % (len(copies), "y" if len(copies) == 1 else "ies", copies[0].path),
            source="snapshots",
            elapsed_s=elapsed,
        )

    if not saw_directory:
        # Rule 4. Not a no.
        return copies, unknown(
            VerdictCategory.NOT_SUPPORTED,
            "no snapshot directory (%s) is exposed on this filesystem, which "
            "does not mean the site keeps no backups" % (", ".join(index.snapdirs),),
            source="snapshots",
            elapsed_s=elapsed,
        )

    if kept_anywhere == 0:
        # Rule 3. The scratch answer, and a measured one.
        return copies, refuted(
            VerdictCategory.NOT_PRESENT,
            "this filesystem exposes a snapshot directory and is keeping "
            "nothing in it, so a deleted file here is gone",
            source="snapshots",
            elapsed_s=elapsed,
        )

    return copies, refuted(
        VerdictCategory.NOT_PRESENT,
        "%d snapshot(s) exist on this filesystem and none of them contains "
        "this path" % (kept_anywhere,),
        source="snapshots",
        elapsed_s=elapsed,
    )
