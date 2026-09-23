"""Finding the roots. Five independent sources, unioned and then deduplicated.

No source is sufficient alone, and that is the whole design. Each one was added
because something measured here is invisible to the others:

* **mounts** sees every mounted filesystem and misses every subdirectory of a
  shared one, which is where all the project space actually is.
* **env** sees ``$HOME`` and ``$SCRATCH`` and the symlinks out of a home
  directory, which is how ``~/.cache`` silently bills against ``/project``.
* **group-template** turns group membership into candidate paths, and is wrong
  often enough to need the next source: 14 of this user's 21 groups have no
  storage directory at all, and two of them (``amber``, ``lumerical``) are
  software licence groups.
* **dir-owner** reads the directory entries instead of guessing their names.
  This is the one that cannot be replaced by a template, because on this
  cluster ``/project/hpc`` is group-owned by ``hpc-staff``, its fileset is
  ``project-hpc``, and its directory is called ``hpc``: group name, directory
  name and fileset name are three different strings and no transform connects
  them.
* **quota-fileset** takes the names the quota layer found and maps them back to
  paths. It is the only source that can find storage a user holds blocks in
  without having group access to it.

**Deduplication is by ``(st_dev, st_ino)``, never by path string.** Measured
here: ``/home``, ``/project``, ``/software`` and ``/programs`` are four
mountpoints of one device and each is reachable a second time under
``/gpfs/meadow3/cap/``, while ``/tmp`` and ``/scratch/local`` are one xfs
directory mounted twice, identical down to the inode. A path-string dedupe
reports nine roots where there are four, and each duplicate carries the same
quota, so the totals double.

**Nothing here walks a tree.** Every source is either a table read, a fixed
list, or one level of ``os.scandir``. The cost of the whole module is
proportional to the number of roots, not to the number of files, which is what
lets it finish in a budget a user will wait through.
"""

import os
from collections import OrderedDict
from typing import Dict, Iterable, List, Optional, Sequence, Tuple

from ..model import (
    Reach,
    Root,
    VerdictCategory,
    confirmed,
    refuted,
    unknown,
)
from ..runner import Budget, Runner
from ..sitecfg import ROLES, Site
from .access import (
    DEFAULT_DEADLINE_S,
    probe_present,
    probe_reach,
    probe_writable,
    symlink_info,
    with_deadline,
)
from .identity import Identity
from .mounts import Mount, MountTable, inside_snapshot_tree

__all__ = [
    "SOURCE_MOUNTS",
    "SOURCE_ENV",
    "SOURCE_GROUP_TEMPLATE",
    "SOURCE_DIR_OWNER",
    "SOURCE_QUOTA_FILESET",
    "SOURCE_DATASET_ROOT",
    "SOURCE_SNAPSHOT_ROOT",
    "SOURCE_ALLOCATION",
    "SOURCE_LABELS",
    "SOURCE_RESTATEMENTS",
    "source_label",
    "restates_source",
    "ENV_VARS",
    "PROJECT_LIKE_ROLES",
    "TEMPLATE_ROLES",
    "SCANDIR_CAP",
    "RANK_PRIMARY",
    "RANK_SECONDARY",
    "role_for_path",
    "inside_snapshot_tree",
    "discover",
]


SOURCE_MOUNTS = "mounts"
SOURCE_ENV = "env"
SOURCE_GROUP_TEMPLATE = "group-template"
SOURCE_DIR_OWNER = "dir-owner"
SOURCE_QUOTA_FILESET = "quota-fileset"
# Site-declared shared collections, from `Site.dataset_roots`. Empty unless a
# site names them, so this adds nothing on a cluster with no config.
SOURCE_DATASET_ROOT = "dataset-root"
# A site-declared snapshot tree, from `Site.snapshot_roots`. Always ranked
# SECONDARY: it is real, reachable storage and it is not a place to put data,
# so it belongs in `--all` and never in the table that answers "where can my
# 2 TB go". Owner, on the first version that omitted it entirely: "/snapshots
# is still not shown, even when running it on the login node."
SOURCE_SNAPSHOT_ROOT = "snapshot-root"
# An allocation database's claim. Never derived into a path by this module.
SOURCE_ALLOCATION = "allocation"


# The tokens above are a WIRE vocabulary: they go into `--json` and into the
# state file, so a consumer may switch on them and they must not change
# casually. The human form therefore lives BESIDE them instead of being
# derived from them, which is `model.CATEGORY_LABELS` applied to this
# vocabulary and for the same reason. `dirscape why` printed "found by
# group-template, dir-owner, quota-fileset" at a user, which is nodetop's NT-5
# (raw enum members in a prose column) arriving through a second column.
#
# Each label completes the sentence "dirscape shows you this directory
# because ...", so they are clauses and not nouns.
SOURCE_LABELS = {
    SOURCE_MOUNTS: "the machine you are on mounts it",
    SOURCE_ENV: "a standard environment variable leads here",
    SOURCE_GROUP_TEMPLATE: "its name matches your user name or one of your groups",
    SOURCE_DIR_OWNER: "a group you belong to owns it",
    SOURCE_QUOTA_FILESET: "the filesystem's own records say you hold space in it",
    SOURCE_DATASET_ROOT: "it sits in a shared data collection this site publishes",
    SOURCE_SNAPSHOT_ROOT: "this site publishes read-only snapshots here",
    SOURCE_ALLOCATION: "an allocation record names it",
}

# Notes this module writes for the sole purpose of explaining one of those
# sources, by leading text. A view that prints the label AND the note says one
# thing twice: `/project/hpc` carried "matched group hpc", "directory hpc is
# group-owned by a group you are in" and "holds the fileset project-hpc",
# which is the three labels again in the tool's own words.
#
# Keyed by source, so a note only disappears when the source that writes it is
# actually on the root, and unmatched text is KEPT. The failure mode of a
# prefix that goes stale is therefore one redundant line rather than a lost
# fact, which is the direction this package errs in everywhere else.
SOURCE_RESTATEMENTS = {
    SOURCE_ENV: ("from $",),
    SOURCE_GROUP_TEMPLATE: ("matched ",),
    SOURCE_DIR_OWNER: ("directory ",),
    SOURCE_QUOTA_FILESET: (
        "holds the fileset ",
        "the quota backend published this path for fileset ",
    ),
    SOURCE_DATASET_ROOT: ("in the shared area ",),
    SOURCE_ALLOCATION: ("the allocation database names this storage ",),
}


#: The same six sources as noun phrases, for a `found` field rather than a
#: sentence. `SOURCE_LABELS` reads as a clause after "because", which is how
#: `why` used to print it; three of those clauses joined into a 25 word
#: sentence for what is really a three item list, so the short forms exist to
#: be comma-joined. Both tables are kept because `--json` and the long views
#: still want the clause.
SOURCE_SHORT = {
    SOURCE_MOUNTS: "a mount point",
    SOURCE_ENV: "an environment variable",
    SOURCE_GROUP_TEMPLATE: "your name or group",
    SOURCE_DIR_OWNER: "group ownership",
    SOURCE_QUOTA_FILESET: "the quota records",
    SOURCE_DATASET_ROOT: "a shared data collection",
    SOURCE_SNAPSHOT_ROOT: "a published snapshot tree",
    SOURCE_ALLOCATION: "an allocation record",
}


def source_label(source, short=False):
    # type: (str, bool) -> str
    """The human clause for a discovery source, never the token itself.

    Falls back to the de-hyphenated token rather than raising, exactly as
    `model.category_label` does: a source with no label is a test failure, and
    it should not take down a user's terminal in the meantime.

    ``short`` returns the noun phrase instead of the clause, for the `found`
    field in `why`.
    """
    table = SOURCE_SHORT if short else SOURCE_LABELS
    return table.get(source, (source or "").replace("-", " "))


def restates_source(note, sources):
    # type: (str, Sequence[str]) -> bool
    """True when a note only repeats a source label that is already on screen."""
    for source in sources or ():
        for prefix in SOURCE_RESTATEMENTS.get(source, ()):
            if note.startswith(prefix):
                return True
    return False


# ``$PROJECT`` and ``$WORK`` are unset on this cluster and set on many others.
# Reading them costs nothing and not reading them makes the tool site-specific.
ENV_VARS = ("HOME", "SCRATCH", "TMPDIR", "PROJECT", "WORK")

# Which mount roots the `dir-owner` scan and the name templates may read.
#
# **Stated as what to SKIP, never as what to allow, and that inversion is the
# whole point.** A role is an advisory label from a glob heuristic, so an
# allowlist of recognised roles silently means "storage this heuristic has
# never heard of does not exist". Measured, and this is the bug the inversion
# fixes: `/collie3` matches no built-in pattern and so scored the fallback role
# `other`, which kept it out of both sources; `/collie3/hpc-staff` is
# `drwxrws--- root hpc-staff`, is writable by this user, and appeared nowhere
# in `dirscape`, `dirscape --all` or `--json`. One unrecognised mountpoint hid
# a whole writable allocation. A denylist cannot fail that way: an unknown root
# is scanned, and the cost of being wrong is one cheap listing.
#
# Each exclusion below is a measurement, not a guess:
#
# * `software`: scanning `/software` matches 713 of its 749 entries, because
#   they are group-owned by `hpc-software` and this user is a member. Group
#   ownership is a poor ownership signal exactly where a group distributes
#   software rather than storage.
# * `scratch`: `/scratch/meadow3` holds 13,909 entries and `/scratch/meadow2`
#   14,446, which is 0.2s of stat apiece for a directory whose per-user path
#   the template source finds in ONE stat. The `<root>/<user>` probe in
#   `_from_group_template` covers these instead, so `scratch` stays in
#   TEMPLATE_ROLES and is only kept out of the scandir.
# * `home`: 13,915 entries, and your own home arrives from `$HOME`.
# * `local`: a memory or node-local filesystem holds no allocation, and `/tmp`
#   here is mode 1777, so a write test on its entries says nothing about
#   ownership.
# * `dataset`: `_from_dataset_roots` already reads these one level deep and
#   deliberately does NOT filter by ownership, which is the right rule for a
#   shared collection and the wrong one here.
#
# Cost of the widening, measured on this node: 0.028s for every `other` and
# `archive` mountpoint on the cluster, adding two candidates, one of which
# (`/gpfs/meadow3/cap/software`) the `(st_dev, st_ino)` dedupe then folds onto
# `/software`.
_DIR_OWNER_SKIP = frozenset(["home", "scratch", "software", "dataset", "local"])
_TEMPLATE_SKIP = frozenset(["home", "software", "dataset", "local"])

#: Roles whose mountpoint gets a one-level, group-ownership-filtered scandir.
#: Derived from `ROLES` by subtraction so a role added to the vocabulary later
#: is scanned by default rather than silently ignored.
PROJECT_LIKE_ROLES = tuple(role for role in ROLES if role not in _DIR_OWNER_SKIP)

#: Roles worth expanding a `<root>/<name>` template against.
TEMPLATE_ROLES = tuple(role for role in ROLES if role not in _TEMPLATE_SKIP)

# Upper bound on entries read from any single directory. A one-level scandir is
# O(entries), not O(tree), but a shared root with a hundred thousand entries
# would still dominate the budget, and this module's promise is O(roots).
SCANDIR_CAP = 4000

# How many top-level entries a mount may have before the `<root>/<child>/<group>`
# template is given up on. A container directory has a handful of entries; a
# directory of allocations has hundreds, and it is the allocations themselves
# that the plain `<root>/<group>` template already finds.
#
# Measured, and the two shapes are far apart enough that the bound is not a
# close call. Under the cap: `/lus/egret` has 11 top-level entries and the
# reader's allocation is at `/lus/egret/projects/lanternlab-exampleu`, two
# levels down, where no other source reaches it. Over it: `/project` has 669,
# `/project2` 892, `/scratch/meadow3` 13,909 and `/scratch/meadow2` 14,446,
# and on all four the allocation is the FIRST level, so descending would cost
# thousands of stats to find what one template already found.
NESTED_FANOUT = 32


def role_for_path(path, fstype="", site=None):
    # type: (str, str, Optional[Site]) -> str
    """The advisory role for a path.

    Delegates to `sitecfg.Site.role_for` rather than carrying a second copy of
    the heuristics, so a site's override applies everywhere and there is one
    place to be wrong. **Advisory only: no access decision anywhere in this
    package reads a role.** Access is settled by `os.access`, and the role only
    decides what a row is called and which roots a template is expanded against.
    """
    if site is None:
        # A bare Site is the documented "no config at all" state and gives the
        # built-in heuristics. This is not `load_site`: nothing is read from
        # /etc here, because the caller owns config loading.
        site = Site()
    return site.role_for(path, fstype)


class _Candidate(object):
    """A path some source proposed, before anything has been probed about it."""

    __slots__ = ("path", "sources", "notes", "fileset", "crossings", "rank", "rank_reason")

    def __init__(self, path):
        # type: (str) -> None
        self.path = path
        self.sources = []  # type: List[str]
        self.notes = []  # type: List[str]
        # Set only by the quota-fileset source, which knows the name before the
        # path and should not have to look it up again.
        self.fileset = ""
        # (link, target, billed_to) for each symlink out of this root that
        # lands in another quota scope. Annotations on the root, not roots of
        # their own: see `_from_env`.
        self.crossings = []  # type: List[Tuple[str, str, str]]
        self.rank = ""
        self.rank_reason = ""

    def add(self, source, note=""):
        # type: (str, str) -> None
        if source and source not in self.sources:
            self.sources.append(source)
        if note and note not in self.notes:
            self.notes.append(note)


class _Basket(object):
    """Ordered candidate collection, keyed on the normalised path string.

    Ordered so that the output is reproducible run to run, which the snapshot
    diff depends on: an unstable order turns every run into a page of spurious
    changes.
    """

    def __init__(self):
        # type: () -> None
        self._items = OrderedDict()  # type: Dict[str, _Candidate]

    def add(self, path, source, note=""):
        # type: (str, str, str) -> Optional[_Candidate]
        if not path:
            return None
        key = os.path.normpath(path)
        if not os.path.isabs(key):
            # A relative path would be resolved against the current directory,
            # which is not a fact about storage. Allocation locations such as
            # "cfs4/hpc-staff" arrive in this shape and are handled separately,
            # without ever being turned into a path.
            return None
        candidate = self._items.get(key)
        if candidate is None:
            candidate = _Candidate(key)
            self._items[key] = candidate
        candidate.add(source, note)
        return candidate

    def get(self, path):
        # type: (str) -> Optional[_Candidate]
        return self._items.get(os.path.normpath(path))

    def __iter__(self):
        # type: () -> Iterable[_Candidate]
        return iter(list(self._items.values()))

    def __len__(self):
        # type: () -> int
        return len(self._items)


# --------------------------------------------------------------------------
# Guarded filesystem helpers. Nothing here recurses.
# --------------------------------------------------------------------------


def _deadline(budget):
    # type: (Optional[Budget]) -> float
    """How long one probe may take.

    The `Budget.slice_for` default share of 0.5 would hand four seconds of an
    eight second budget to a single `stat`, which is not a sensible unit of
    spend for a metadata read. A 5% share with a 1 second ceiling leaves room
    for the twenty to forty probes a real mount table needs.
    """
    if budget is None:
        return DEFAULT_DEADLINE_S
    return budget.slice_for(share=0.05, floor=0.15, ceiling=1.0)


def _is_dir(path, deadline_s):
    # type: (str, Optional[float]) -> Optional[bool]
    """Deadline-guarded ``isdir``. ``None`` means the question went unanswered.

    Guarded because candidate generation touches paths on every mounted
    filesystem, including one that may have stopped answering, and an unguarded
    `os.path.isdir` there would hang the run before any deadline logic is
    reached.
    """
    finished, value, exc, _elapsed = with_deadline(lambda: os.path.isdir(path), deadline_s)
    if not finished or exc is not None:
        return None
    return bool(value)


def _scandir_one_level(path, deadline_s, cap=SCANDIR_CAP):
    # type: (str, Optional[float], int) -> List[Tuple[str, str, Optional[int], bool, Optional[int]]]
    """One level of a directory as ``(name, path, gid, is_dir, uid)`` tuples.

    Never recurses, and never follows a symlink when stat'ing an entry: a
    symlink into a wedged filesystem inside a directory being listed would
    otherwise block the listing of the rest.
    """

    def read():
        # type: () -> List[Tuple[str, str, Optional[int], bool, Optional[int]]]
        found = []  # type: List[Tuple[str, str, Optional[int], bool, Optional[int]]]
        with os.scandir(path) as entries:
            for entry in entries:
                if len(found) >= cap:
                    break
                gid = None  # type: Optional[int]
                uid = None  # type: Optional[int]
                is_dir = False
                try:
                    stat_result = entry.stat(follow_symlinks=False)
                    gid = stat_result.st_gid
                    uid = stat_result.st_uid
                    is_dir = entry.is_dir(follow_symlinks=False)
                except OSError:
                    # One unreadable entry must not lose the other 668.
                    pass
                found.append((entry.name, entry.path, gid, is_dir, uid))
        return found

    finished, value, exc, _elapsed = with_deadline(read, deadline_s)
    if not finished or exc is not None or value is None:
        return []
    return value  # type: ignore[return-value]


def _names_one_level(path, deadline_s, cap=NESTED_FANOUT):
    # type: (str, Optional[float], int) -> Optional[List[str]]
    """Entry names one level down, or None when there are more than ``cap``.

    `os.listdir` rather than `_scandir_one_level` because this needs names
    and not metadata: the scandir helper stats every entry, which is 0.2s on
    a directory of 14,000 and exactly the cost the cap exists to avoid. A
    bare listdir of the same directory is a few milliseconds, so the count
    can be checked before anything expensive is decided.
    """

    def read():
        # type: () -> List[str]
        return sorted(os.listdir(path))

    finished, value, exc, _elapsed = with_deadline(read, deadline_s)
    if not finished or exc is not None or value is None:
        return None
    if len(value) > cap:
        return None
    return value  # type: ignore[return-value]


def _identity_of(path, deadline_s):
    # type: (str, Optional[float]) -> Optional[Tuple[int, int]]
    """``(st_dev, st_ino)`` of a path, following symlinks.

    Follows symlinks on purpose: that is what makes an aliased path collapse
    onto the real one, which is the entire point of keying on the inode.
    """

    def read():
        # type: () -> Tuple[int, int]
        stat_result = os.stat(path)
        return (stat_result.st_dev, stat_result.st_ino)

    finished, value, exc, _elapsed = with_deadline(read, deadline_s)
    if not finished or exc is not None or value is None:
        return None
    return value  # type: ignore[return-value]


# --------------------------------------------------------------------------
# The five sources
# --------------------------------------------------------------------------


RANK_PRIMARY = "primary"
RANK_SECONDARY = "secondary"

# Filesystems nobody holds an allocation on. Kept and ranked down rather than
# dropped, because they are real and a job can fill them.
#
# Two kinds, both measured. Memory filesystems: this node's ``/`` is a tmpfs
# (it is diskless) and ``/.nodelog/log`` is a Slurm log tmpfs, so a user asking
# where their data can go got two rows of noise at the top. Read-only images:
# a Cray login node mounts four squashfs images, one of which is ``/root_ro``,
# the OS root. Nothing there is storage, and descending it produced a row
# reading ``/root_ro/egret/lanternlab-exampleu`` for the reader's real
# allocation, because ``/root_ro/egret`` is a symlink to
# ``/lus/egret/projects`` and the shorter path won the dedupe.
_EPHEMERAL_FSTYPES = frozenset(["tmpfs", "ramfs", "rootfs", "overlay", "squashfs", "iso9660"])

# What each of those IS, for the sentence that says why the row is held back.
# One phrase served all six and printed "a overlay memory filesystem" about a
# Cray login node's `/`, which is an overlay on a squashfs image: neither
# memory nor grammatical.
_EPHEMERAL_KIND = {
    "tmpfs": "a tmpfs memory filesystem",
    "ramfs": "a ramfs memory filesystem",
    "rootfs": "the node's in-memory root filesystem",
    "overlay": "an overlay on the node's system image",
    "squashfs": "a read-only squashfs image",
    "iso9660": "a read-only disc image",
}


def _rank_of_mount(mount, mounts, site):
    # type: (Mount, MountTable, Site) -> Tuple[str, str]
    """``(rank, reason)`` for a mountpoint, so a default view can hide noise.

    Two things get ranked down, and neither is wrong, which is why they are
    marked rather than dropped:

    * A memory filesystem. Nobody has an allocation on one.
    * A filesystem whose own root is mounted where the same filesystem is also
      available under a more useful name. Measured: the six ``/gpfs/<cluster>/<tier>``
      roots here are the same filesystems already visible at ``/home``,
      ``/project``, ``/software`` and ``/scratch/*``, mounted at their
      filesystem root rather than at a fileset junction. Their fileset reads
      ``root``. Note that ``(st_dev, st_ino)`` dedupe correctly does NOT collapse
      them, because a filesystem root and a fileset junction inside it are
      different directories.

    The second test is depth plus role, not a path pattern: among the
    mountpoints of one device, a deeper one with no recognisable role is the
    plumbing view. ``/collie3`` (depth 1) therefore stays primary while
    ``/gpfs/collie3/cap`` (depth 3) does not, and four sibling mountpoints at
    equal depth (``/home``, ``/project``, ``/software``, ``/programs``) all stay.
    """
    fstype = (mount.fstype or "").lower()
    if fstype in _EPHEMERAL_FSTYPES:
        return (
            RANK_SECONDARY,
            "%s, so it holds no allocation" % (_EPHEMERAL_KIND.get(fstype, "a " + fstype),),
        )

    if inside_snapshot_tree(mount.mountpoint):
        return (
            RANK_SECONDARY,
            "a read-only snapshot of another directory, so it is somewhere to "
            "copy FROM; `dirscape recover <path>` reads it",
        )

    # **`/` is the machine, not an allocation.** On a login node it is a real
    # 312G disk that `statvfs` and the measuring walk will happily report on,
    # so it arrived in the default table reading `read only 20G 312G 311k`
    # under the heading `other`, between a user's project space and their
    # scratch. Owner: "there is something else such as `/`. do you think these
    # should be trimmed down and polished?"
    #
    # Anywhere under it that a user can actually write is its own mount and
    # gets its own row, so nothing is lost. Conditional on another mountpoint
    # existing, because on a single-filesystem machine `/` IS the storage and
    # a table with no rows is worse than an imprecise one.
    if not mount.mountpoint.rstrip("/") and len(mounts.non_pseudo()) > 1:
        return (
            RANK_SECONDARY,
            "the root of the machine's own filesystem; anywhere under it you can "
            "write is mounted separately and has its own row",
        )

    role = site.role_for(mount.mountpoint, mount.fstype)
    if role in ("", "other"):
        depth = mount.mountpoint.rstrip("/").count("/")
        # `filesystem`, not `device`: a Lustre subdirectory mount names its
        # directory inside the device string, so ACME's `/lus/acorn` and the
        # `/home` mounted out of it read as two devices and the plumbing view
        # stayed in the default table carrying the home directory's quota.
        for other in mounts.mounts:
            if other.filesystem != mount.filesystem or other.mountpoint == mount.mountpoint:
                continue
            if other.mountpoint.rstrip("/").count("/") < depth:
                return (
                    RANK_SECONDARY,
                    "the filesystem root of %s, which is also mounted at %s"
                    % (_device_name(mount), other.mountpoint),
                )
    return (RANK_PRIMARY, "")


def _device_name(mount):
    # type: (Mount) -> str
    """A device as a reader can take it in: a Lustre filesystem by its name.

    A Lustre device leads with every server's network id, 162 characters on
    ACME before the part that says `acorn`, so the sentence it sat in was one
    long address. The fsname is what `lfs df` and the site's own docs call it.
    """
    if (mount.fstype or "").lower() == "lustre":
        return "the Lustre filesystem %s" % (mount.filesystem.rpartition(":/")[2] or mount.device,)
    return mount.device


def _from_mounts(basket, mounts, site):
    # type: (_Basket, MountTable, Site) -> None
    """Source (a): every non-pseudo mountpoint, except a mounted snapshot.

    NetApp mounts every snapshot a reader touches, one per retained copy per
    directory, and they come and go with the automounter and the hourly
    rotation. Measured on Procyon: 30 of the 49 rows of `--all` were
    `/admin_home/<person>/.snapshot/daily...` and its siblings, and each new
    hour's copy would have been a `new` root in `dirscape new` and the
    expired one a `gone`. A row per retained snapshot is eleven rows of one
    directory at eleven dates, which `_from_snapshot_roots` already declines
    to produce for a declared snapshot tree; `dirscape recover` reads them,
    one path at a time, from the mount table itself.
    """
    for mount in mounts.non_pseudo():
        if not mount.mountpoint or site.is_ignored(mount.mountpoint):
            continue
        if inside_snapshot_tree(mount.mountpoint):
            continue
        note = ""
        if mount.unclassified:
            # Said out loud rather than silently dropped. An unrecognised
            # filesystem type is far more likely to be real storage this package
            # has not heard of than it is to be kernel plumbing.
            note = "filesystem type %s is not classified; treated as real storage" % (mount.fstype,)
        candidate = basket.add(mount.mountpoint, SOURCE_MOUNTS, note)
        if candidate is not None and not candidate.rank:
            candidate.rank, candidate.rank_reason = _rank_of_mount(mount, mounts, site)


def _owner_of(path, deadline_s):
    # type: (str, Optional[float]) -> Optional[int]
    """``st_uid`` of a path, or None when it cannot be read."""
    finished, value, exc, _elapsed = with_deadline(lambda: os.stat(path).st_uid, deadline_s)
    if not finished or exc is not None or value is None:
        return None
    return int(value)  # type: ignore[arg-type]


def _owned_ancestor(target, mount_point, uid, deadline_s, max_depth=16):
    # type: (str, str, int, Optional[float], int) -> str
    """The shallowest directory between a mountpoint and ``target`` that ``uid`` owns.

    This is what turns a symlink target into a storage root worth naming.
    Measured case: eleven symlinks out of this home directory land on
    ``/project/hpc/jdoe42/.cache``, ``/project/hpc/jdoe42/.cache/nv``,
    ``/project/hpc/jdoe42/.codex`` and so on. None of those is a storage root.
    Their owned ancestor is ``/project/hpc/jdoe42``, which is one root, is the
    single most useful row in the table, and is what all eleven collapse onto.

    Walking a path's own ancestry is O(depth) and bounded by ``max_depth``. It
    is not a tree walk: no directory is ever listed.

    Falls back to the first directory below the mountpoint (the fileset junction
    level on this cluster, for instance ``/project/hpc``) and then to the
    mountpoint itself, so a target nobody owns still resolves to something real.
    """
    base = mount_point.rstrip("/")
    if not target.startswith(base + "/"):
        return target if target == base else ""
    parts = [part for part in target[len(base) + 1 :].split("/") if part][:max_depth]

    path = base
    fallback = ""
    for part in parts:
        path = path + "/" + part
        if not fallback:
            fallback = path
        owner = _owner_of(path, deadline_s)
        if owner is not None and owner == uid:
            return path
    return fallback or base


def _from_env(basket, mounts, identity, environ, site, deadline_s):
    # type: (_Basket, MountTable, Identity, Dict[str, str], Site, float) -> None
    """Source (b): the standard variables, plus the symlinks out of ``$HOME``.

    The ``$HOME`` scan catches the home-quota workaround: a symlink from
    ``~/.cache`` to a project directory moves the bytes into another quota
    scope, and the user who set it up a year ago has forgotten. One level of
    scandir, no recursion.

    **A symlink target is an annotation, not a root.** An earlier version added
    the target itself, which produced eleven rows including
    ``/project/hpc/jdoe42/.cache/nv`` and ``/project/hpc/jdoe42/.cache/triton``
    while ``/project/hpc/jdoe42`` was missing entirely: a cache subdirectory is
    not a storage root, and the user's own project directory is the row that
    matters most. So the crossing is recorded on the home root and the target's
    owned ancestor is what gets promoted.
    """
    for name in ENV_VARS:
        value = str(environ.get(name) or "").strip()
        if value and not site.is_ignored(value):
            basket.add(value, SOURCE_ENV, "from $%s" % (name,))

    home = str(environ.get("HOME") or "").strip()
    if not home:
        return
    home_mount = mounts.enclosing_mount(home)
    home_point = home_mount.mountpoint if home_mount is not None else ""
    home_candidate = basket.get(home)

    for _name, entry_path, _gid, _is_dir, _uid in _scandir_one_level(home, deadline_s):
        info = symlink_info(entry_path)
        if info is None or not info.is_dir:
            continue
        target_mount = mounts.enclosing_mount(info.resolved)
        target_point = target_mount.mountpoint if target_mount is not None else ""
        if not target_point or target_point == home_point:
            continue
        if site.is_ignored(info.resolved):
            continue

        # The mountpoint comparison, not the st_dev one. Measured: ~/.cache ->
        # /project/hpc/jdoe42/.cache changes which quota scope is billed while
        # both sides sit on st_dev 54, because /home and /project are two
        # mountpoints of one GPFS device.
        promoted = _owned_ancestor(info.resolved, target_point, identity.uid, deadline_s)
        if home_candidate is not None:
            home_candidate.crossings.append((entry_path, info.resolved, promoted or target_point))

        if promoted and not site.is_ignored(promoted):
            basket.add(
                promoted,
                SOURCE_ENV,
                "holds what %s points at, from %s"
                % (entry_path, home_point or "the home filesystem"),
            )


def _template_roots(mounts, site, roles=TEMPLATE_ROLES):
    # type: (MountTable, Site, Sequence[str]) -> List[str]
    """Mountpoints worth expanding a name template against.

    Memory filesystems are dropped here as well as ranked down in
    `_rank_of_mount`. Both roles now arrive from a denylist, so a diskless
    node whose `/` reports `rootfs` rather than `tmpfs` scores the fallback
    role `other` and would otherwise have its whole top level scanned.
    Nobody has an allocation on a tmpfs, so there is nothing to find there.
    """
    found = []  # type: List[str]
    for mount in mounts.non_pseudo():
        if site.is_ignored(mount.mountpoint):
            continue
        if (mount.fstype or "").lower() in _EPHEMERAL_FSTYPES:
            continue
        if inside_snapshot_tree(mount.mountpoint):
            continue
        if site.role_for(mount.mountpoint, mount.fstype) in roles:
            if mount.mountpoint not in found:
                found.append(mount.mountpoint)
    return found


def _from_group_template(basket, mounts, identity, site, deadline_s, budget):
    # type: (_Basket, MountTable, Identity, Site, float, Optional[Budget]) -> None
    """Source (c): ``<root>/<group>`` for every group, plus the site templates.

    ``Site.group_aliases`` supplies the name variants, which is where the
    ``pi-`` convention is handled in both directions: membership of ``pi-smith``
    can grant access to a directory called ``smith``.

    Existence-checked, because group membership on its own is a bad predictor.
    Measured on this account: 14 of 21 groups have no storage directory at all.

    **And one level deeper, but only where the flat template found nothing.**
    A second cluster puts allocations two levels down:
    `/lus/egret/projects/lanternlab-exampleu` is 29.58T against a 50T project
    quota, it is the reader's only writable allocation there, and it appeared
    in no view at all. The flat template looks for
    `/lus/egret/lanternlab-exampleu`, the dir-owner scan reads one level, and
    neither reaches two.

    Running it only on the misses is what makes it free. A root whose flat
    template already produced a directory is a root where allocations live at
    the first level, so there is nothing below worth listing: on the GPFS
    cluster `/project` and `/project2` both hit flat, and listing them to
    learn they have 669 and 892 entries was 0.2s of a run that finishes in
    three. `NESTED_FANOUT` is the second bound, for a root that misses flat
    and is still large.
    """
    roots = _template_roots(mounts, site)
    scratch_roots = _template_roots(mounts, site, ("scratch",))
    # Never a scratch root: a scratch allocation is `<root>/<user>` and the
    # single stat below already finds it. And never a SECONDARY mount, which
    # is the plumbing view of a filesystem already visible at its junctions:
    # descending the six `/gpfs/<cluster>/<tier>` roots here probed 2,688
    # paths to rediscover directories the `(st_dev, st_ino)` dedupe then
    # folds away, and cost 0.21s of a three second run to do it.
    nestable = set()
    for mount in mounts.non_pseudo():
        if mount.mountpoint not in _template_roots(mounts, site, PROJECT_LIKE_ROLES):
            continue
        rank, _reason = _rank_of_mount(mount, mounts, site)
        if rank == RANK_PRIMARY:
            nestable.add(mount.mountpoint)

    def offer(path, why):
        # type: (str, str) -> bool
        # Checked per offer and not only per root: one root is every group
        # alias times every child, which on meadow2 is thousands of `stat`s,
        # so a per-root check let a single root run the allowance out.
        if budget is not None and budget.exhausted:
            return False
        if site.is_ignored(path):
            return False
        if not _is_dir(path, deadline_s):
            return False
        basket.add(path, SOURCE_GROUP_TEMPLATE, "matched %s" % (why,))
        return True

    def aliases():
        # type: () -> List[Tuple[str, str]]
        out = []  # type: List[Tuple[str, str]]
        for group in identity.groups:
            for alias in site.group_aliases(group):
                out.append((alias, group))
        return out

    names = aliases()
    for root in roots:
        if budget is not None and budget.exhausted:
            # A speculative candidate that was never established to exist is
            # dropped rather than recorded as unknown. Recording it would put
            # a path on the report that nothing has ever seen, which is a
            # different and worse failure than omitting a guess.
            return
        hit = False
        for alias, group in names:
            if offer(os.path.join(root, alias), "group %s" % (group,)):
                hit = True
        if hit or root not in nestable:
            continue
        children = _names_one_level(root, deadline_s)
        for child in children or ():
            if budget is not None and budget.exhausted:
                return
            for alias, group in names:
                offer(
                    os.path.join(root, child, alias),
                    "group %s, under %s" % (group, child),
                )

    # A per-user directory under each scratch root. One stat, and it is how the
    # 13,907-entry scandir of /scratch/meadow3 is avoided entirely.
    for root in scratch_roots:
        if budget is not None and budget.exhausted:
            return
        if identity.user:
            offer(os.path.join(root, identity.user), "user %s" % (identity.user,))

    for path in site.expand_templates(identity.user, identity.groups, identity.cluster):
        if budget is not None and budget.exhausted:
            return
        offer(path, "site template")


def _from_dir_owner(basket, mounts, identity, site, deadline_s, budget):
    # type: (_Basket, MountTable, Identity, Site, float, Optional[Budget]) -> None
    """Source (d): one level of each project root, filtered by group ownership.

    Essential and not replaceable by the template source. ``/project/hpc`` is
    group-owned by ``hpc-staff`` while its directory is called ``hpc`` and its
    fileset is ``project-hpc``; only reading the directory entries connects
    those three strings.
    """
    gids = identity.gid_set
    if not gids:
        return
    for root in _template_roots(mounts, site, PROJECT_LIKE_ROLES):
        if budget is not None and budget.exhausted:
            return
        matched = [
            (name, entry_path, gid, uid)
            for name, entry_path, gid, is_dir, uid in _scandir_one_level(root, deadline_s)
            if is_dir and gid is not None and gid in gids and not site.is_ignored(entry_path)
        ]
        everyones = _default_groups(matched, identity.uid)
        for name, entry_path, gid, uid in matched:
            if gid in everyones and uid != identity.uid:
                continue
            basket.add(
                entry_path,
                SOURCE_DIR_OWNER,
                "directory %s is group-owned by a group you are in" % (name,),
            )


#: How many OTHER people's directories one group may own in a single listing
#: before it is read as their default group rather than as a grant.
DEFAULT_GROUP_OWNERS = 4


def _default_groups(matched, me):
    # type: (Sequence[Tuple[str, str, int, Optional[int]]], int) -> frozenset
    """Gids that own everybody's directories here, and so prove nothing.

    Group ownership is evidence of a GRANT only when the group is not
    everyone's. On ACME every account's primary group is `users` (gid 100),
    so `/admin_home` there is ninety personal directories, each owned by a
    different person and every one group-owned by a group this reader is in.
    They all matched, and `--all` grew ninety rows of other people's homes
    (sixty-two of them `no access`), with a caveat apiece in `--json`.

    The shape tells the two apart without knowing any group's name. An
    allocation is one directory per group, owned by root or by its PI: on the
    GPFS cluster the largest match is two directories of `hpc-staff` under
    `/project2`, both owned by root. A default group owns one directory per
    PERSON. So a gid that owns the directories of `DEFAULT_GROUP_OWNERS` or
    more different people in one listing, root and the reader not counted, is
    set aside, and only the reader's own directories survive under it.
    """
    owners = {}  # type: Dict[int, set]
    for _name, _path, gid, uid in matched:
        if uid is None or uid in (0, me):
            continue
        owners.setdefault(gid, set()).add(uid)
    return frozenset(gid for gid, people in owners.items() if len(people) >= DEFAULT_GROUP_OWNERS)


def _from_dataset_roots(basket, site, deadline_s, budget):
    # type: (_Basket, Site, float, Optional[Budget]) -> None
    """Site-declared shared collections, one level deep.

    Not filtered by ownership: the point of a shared dataset area is that the
    directories belong to somebody else. Empty unless a site names its roots.
    """
    for root in getattr(site, "dataset_roots", ()) or ():
        if budget is not None and budget.exhausted:
            return
        for _name, entry_path, _gid, is_dir, _uid in _scandir_one_level(root, deadline_s):
            if is_dir and not site.is_ignored(entry_path):
                basket.add(entry_path, SOURCE_DATASET_ROOT, "in the shared area %s" % (root,))


def _from_snapshot_roots(basket, site, deadline_s, budget):
    # type: (_Basket, Site, float, Optional[Budget]) -> None
    """Site-declared snapshot trees, as secondary roots.

    They are reported and never recommended. A snapshot tree is reachable
    storage the reader can copy out of, so leaving it off `--all` is a lie by
    omission; it is also read-only and holds no allocation, so putting it in
    the default table would add a row to the one view whose whole promise is
    that every row is somewhere you can write.

    Only the declared root itself is offered, not the snapshots inside it.
    `dirscape recover` enumerates those per path, which is the question a
    reader actually has, and a row per retained snapshot would be eleven rows
    of the same directory at eleven dates.
    """
    for path in getattr(site, "snapshot_roots", ()) or ():
        if budget is not None and budget.exhausted:
            return
        if not path or site.is_ignored(path):
            continue
        if not _is_dir(path, deadline_s):
            continue
        candidate = basket.add(
            path, SOURCE_SNAPSHOT_ROOT, "read-only snapshots of other directories"
        )
        if candidate is not None:
            candidate.rank = RANK_SECONDARY
            candidate.rank_reason = (
                "a read-only snapshot tree, so it is somewhere to copy FROM and never "
                "somewhere to put data; `dirscape recover <path>` reads it"
            )


def _fileset_path_candidates(name, mounts, site):
    # type: (str, MountTable, Site) -> List[str]
    """Paths a fileset name could correspond to, most likely first.

    Derived from the fileset names measured on this cluster, where the mapping
    is a real pattern and not a coincidence:

        home        -> /home            mountpoint basename matches the name
        project     -> /project         likewise
        project-hpc -> /project/hpc     the prefix names the parent mountpoint
        software    -> /software        and also /programs, one fileset twice
        scratch     -> unresolvable     three devices share this fileset name

    Every candidate must have its head confirmed against a real mountpoint. An
    earlier version joined the tail of a split name onto every project-like
    root, which turned the fileset ``cfs9-ghost`` into the invented paths
    ``/project/ghost`` and ``/scratch/meadow3/ghost``. Inventing a
    plausible-looking path is the RD-3 mistake in miniature, so the only
    transformations left here are ones a mountpoint basename agrees with.

    Existence is checked by the caller. Nothing here claims a path exists.
    """
    if not name:
        return []
    out = []  # type: List[str]

    def offer(path):
        # type: (str) -> None
        if path and path not in out:
            out.append(path)

    points = [m.mountpoint for m in mounts.non_pseudo() if m.mountpoint]
    for point in points:
        if os.path.basename(point.rstrip("/")) == name:
            offer(point)

    # A site-declared prefix is authoritative; the generic first-dash split is
    # the fallback for a site that has declared none. Either way the head has
    # to match a mountpoint basename before anything is offered.
    heads = []  # type: List[Tuple[str, str]]
    for prefix in getattr(site, "fileset_prefixes", ()) or ():
        if prefix and name.startswith(prefix) and len(name) > len(prefix):
            heads.append((prefix.rstrip("-_"), name[len(prefix) :]))
    if not heads and "-" in name:
        head, _, tail = name.partition("-")
        if head and tail:
            heads.append((head, tail))

    for head, tail in heads:
        for point in points:
            if os.path.basename(point.rstrip("/")) == head:
                offer(os.path.join(point, tail))

    # The whole name under a project-like root, which is the convention on a
    # site whose filesets are named after the directory rather than prefixed.
    for root in _template_roots(mounts, site):
        offer(os.path.join(root, name))
    return out


def _from_quota_filesets(basket, mounts, site, filesets, deadline_s, budget):
    # type: (_Basket, MountTable, Site, Sequence[object], float, Optional[Budget]) -> List[Tuple[str, str]]
    """Source (e): fileset names the quota layer found, mapped back to paths.

    The names are **passed in**. This module never calls the quota layer: a
    discovery pass that triggered a quota read would make the cheap half of the
    tool depend on the expensive half.

    Each item is either a bare fileset name or a ``(name, path)`` pair. The pair
    form exists because the quota layer usually already knows the mount for a
    row, and a path the backend published beats any amount of name inference.
    A published path is trusted even when nothing is there, because the backend
    saying so is evidence; an inferred path is only used when it is confirmed to
    exist, because inference is not.

    **Ambiguity drops the candidate.** A bare name matching two existing paths
    is not placed at either, following the rule `QuotaRow.guessed` already
    states for inferred mounts. Measured reason: ``scratch``, ``home`` and
    ``software`` are fileset names on more than one device here, so a name
    alone cannot say which one a row belongs to.

    Returns ``(name, anchor)`` for every name that could not be placed, where
    the anchor is the real mountpoint the name pointed into, or "" when the name
    pointed nowhere on this node.
    """
    unplaced = []  # type: List[Tuple[str, str]]
    for item in filesets or ():
        if budget is not None and budget.exhausted:
            break
        name = ""
        published_path = ""
        if isinstance(item, (tuple, list)) and item:
            name = str(item[0] or "")
            published_path = str(item[1] or "") if len(item) > 1 else ""
        else:
            name = str(item or "")
        if not name:
            continue

        if published_path:
            candidate = basket.add(
                published_path,
                SOURCE_QUOTA_FILESET,
                "the quota backend published this path for fileset %s" % (name,),
            )
            if candidate is not None:
                candidate.fileset = name
                continue

        paths = [
            path
            for path in _fileset_path_candidates(name, mounts, site)
            if not site.is_ignored(path)
        ]
        existing = [path for path in paths if _is_dir(path, deadline_s)]

        if len(existing) == 1:
            candidate = basket.add(
                existing[0], SOURCE_QUOTA_FILESET, "holds the fileset %s" % (name,)
            )
            if candidate is not None:
                candidate.fileset = name
            continue

        # Either nothing matched or too much did. No path is invented in either
        # case: the name is handed back so the caller can report it against a
        # mountpoint that really exists, and the quota layer keeps the fileset
        # in its own rows regardless. Losing a row here would cost a user the
        # STRANDED case; inventing a path would cost them a wrong one.
        anchor = ""
        for path in paths:
            mount = mounts.enclosing_mount(path)
            if mount is not None and mount.mountpoint != "/":
                anchor = mount.mountpoint
                break
        unplaced.append((name, anchor))
    return unplaced


# --------------------------------------------------------------------------
# Probing and assembly
# --------------------------------------------------------------------------


def _build_root(candidate, mounts, site, budget, allow_write):
    # type: (_Candidate, MountTable, Site, Optional[Budget], bool) -> Root
    """Turn a candidate path into a probed `Root`."""
    path = candidate.path
    mount = mounts.enclosing_mount(path)
    fstype = mount.fstype if mount is not None else ""
    device = mount.device if mount is not None else ""

    root = Root(path, role=site.role_for(path, fstype), device=device, fstype=fstype)
    root.fileset = candidate.fileset
    root.policy = dict(site.policy_for(path))
    if mount is not None and mount.is_local:
        # This node's own disk or memory. Recorded so the state layer can
        # refuse to compare it with a baseline another node took: `/tmp` on
        # `sylvia-login-01` is not the `/tmp` on `sylvia-login-02`, though the
        # path, the device name and even `(st_dev, st_ino)` can all agree.
        root.policy["node_local"] = True
    for source in candidate.sources:
        root.add_source(source)
    for note in candidate.notes:
        root.add_note(note)

    if mount is None:
        root.mounted = refuted(
            VerdictCategory.NOT_MOUNTED_HERE,
            "no mount in the table encloses this path",
            source="/proc/self/mounts",
        )
    else:
        root.mounted = confirmed(
            "under the %s mount %s" % (mount.fstype, mount.mountpoint),
            source="/proc/self/mounts",
        )

    # The budget check is here, before every probe, and produces NOT_PROBED
    # rather than skipping the root. A root that vanishes because time ran out
    # is indistinguishable from a root that does not exist, and those are very
    # different answers.
    if budget is not None and budget.exhausted:
        root.present = unknown(VerdictCategory.NOT_PROBED, "time budget exhausted")
        root.reach = Reach.UNKNOWN
        root.reach_reason = "time budget exhausted before this root was probed"
        root.writable = unknown(VerdictCategory.NOT_PROBED, "time budget exhausted")
        return root

    deadline = _deadline(budget)
    root.present = probe_present(path, deadline)
    if root.present.elapsed_s and budget is not None:
        budget.charge(root.present.elapsed_s)

    if not root.present.confirmed:
        root.reach = Reach.UNKNOWN
        root.reach_reason = root.present.reason or "not probed further"
        if root.present.refuted:
            root.writable = refuted(VerdictCategory.NOT_PRESENT, "the path does not exist here")
        return root

    root.identity = _identity_of(path, deadline)

    info = symlink_info(path)
    if info is not None:
        root.symlink_target = info.resolved
        target_mount = mounts.enclosing_mount(info.resolved)
        target_point = target_mount.mountpoint if target_mount is not None else ""
        here_mount = mounts.enclosing_mount(os.path.dirname(path) or "/")
        here_point = here_mount.mountpoint if here_mount is not None else ""
        # Mountpoint first, st_dev only as a fallback. See `symlink_info`: the
        # device test is blind to the /home to /project crossing that matters
        # most here, because both are one GPFS device.
        if target_point and here_point:
            root.crosses_boundary = target_point != here_point
        else:
            root.crosses_boundary = info.crosses_device
        if root.crosses_boundary:
            root.add_note(
                "symlink leaves %s for %s, so the space is billed there"
                % (here_point or "its parent filesystem", target_point or info.resolved)
            )

    # Symlinks OUT of this root into another quota scope. Recorded here rather
    # than promoted into rows of their own: this is the ~/.cache trap, where the
    # bytes a user believes are in their home quota are billed somewhere else.
    if candidate.crossings:
        root.crosses_boundary = True
        if root.symlink_target is None:
            root.symlink_target = candidate.crossings[0][1]
        billed = []  # type: List[str]
        for link, target, promoted in candidate.crossings:
            root.add_note(
                "%s resolves to %s, so its contents are billed against %s and "
                "not against this root" % (link, target, promoted)
            )
            if promoted not in billed:
                billed.append(promoted)
        # The renderer joins this with the promoted root's own fileset, which
        # the attribution pass fills in later. Discovery cannot name the fileset
        # itself without running a command, and it runs none.
        root.policy["crosses_to"] = billed

    rank, rank_reason = candidate.rank, candidate.rank_reason
    fstype_lower = (fstype or "").lower()
    if not rank and fstype_lower in _EPHEMERAL_FSTYPES:
        # A directory INSIDE a memory filesystem, not the mount itself, which
        # only `_rank_of_mount` sees. Measured on a meadow2 login node, which
        # is diskless: `/` is a tmpfs, `$TMPDIR` is `/tmp`, and `/tmp` is a
        # plain directory in it, so it came in through the environment with
        # no rank and sat in the default table as `local /tmp ? ? ?`.
        rank = RANK_SECONDARY
        rank_reason = "inside %s, so it holds no allocation" % (
            _EPHEMERAL_KIND.get(fstype_lower, "a " + fstype_lower),
        )

    # Always set, so a renderer can switch on it without a default of its own.
    root.policy["rank"] = rank or RANK_PRIMARY
    if rank_reason:
        root.policy["rank_reason"] = rank_reason
        root.add_note(rank_reason)

    result = probe_reach(path, deadline)
    root.reach = result.state
    root.reach_reason = result.reason

    root.writable = probe_writable(path, allow_write=allow_write, deadline_s=deadline)
    return root


#: Of what is left when discovery starts, the share the searching sources
#: may spend. See `discover`.
SEARCH_SHARE = 0.5


def _probe_priority(candidate):
    # type: (_Candidate) -> int
    """Which candidates to probe first when time may run out.

    The environment's own paths (`$HOME`, `$SCRATCH`) and what the searches
    found, then mountpoints, and the plumbing views of those last.
    """
    if SOURCE_ENV in candidate.sources:
        return 0
    if candidate.sources != [SOURCE_MOUNTS]:
        return 1
    return 3 if candidate.rank == RANK_SECONDARY else 2


def _dedupe(roots):
    # type: (Sequence[Root]) -> List[Root]
    """Collapse roots that are the same directory, keeping the shortest path.

    Keyed on ``(st_dev, st_ino)`` where it is known and on the path string only
    where it is not, because an unprobed path has no identity yet and two
    unprobed paths are not evidence of being the same thing.

    **A path that is its own realpath beats one that is not**, and only then
    does the shortest win, with ties broken lexicographically so the choice is
    stable across runs. The symlink test comes first because shortest-path
    alone picks the wrong name whenever an alias is a symlink into a longer
    tree, which is not hypothetical: a Cray login node symlinks
    ``/root_ro/egret`` to ``/lus/egret/projects``, so the reader's allocation
    was reported as ``/root_ro/egret/lanternlab-exampleu``, 4 characters
    shorter and inside a read-only OS image, rather than at the path their
    site documents and their jobs use.

    `os.path.realpath` is called only inside a group that actually collided,
    so the cost is per duplicate and not per root.

    Every collapsed alias is recorded as a note: a user who typed
    ``/gpfs/meadow3/cap/home`` needs to see that it is the row called ``/home``,
    not to conclude their path is missing.
    """
    groups = OrderedDict()  # type: Dict[object, List[Root]]
    for root in roots:
        key = root.identity if root.identity is not None else ("path", root.path)
        groups.setdefault(key, []).append(root)

    kept = []  # type: List[Root]
    for members in groups.values():
        if len(members) > 1:
            members = sorted(members, key=lambda r: (_is_alias(r.path), len(r.path), r.path))
        keeper = members[0]
        for alias in members[1:]:
            for source in alias.sources:
                keeper.add_source(source)
            for note in alias.notes:
                keeper.add_note(note)
            keeper.add_note("also reachable at %s" % (alias.path,))
            # A more specific fileset from an alias is still the same fileset;
            # take it rather than lose it to the shortest-path rule.
            if not keeper.fileset and alias.fileset:
                keeper.fileset = alias.fileset
        kept.append(keeper)
    return kept


def _is_alias(path):
    # type: (str) -> bool
    """Whether reaching ``path`` went through a symlink. 1 sorts after 0.

    An unreadable path answers "not an alias", which keeps a probe failure
    from silently demoting the only name a directory has.
    """
    try:
        return 0 if os.path.realpath(path) == path.rstrip("/") or not path else 1
    except OSError:
        return 0


def _location_matches(location, path):
    # type: (str, str) -> bool
    """Whether an allocation location names this path.

    Compares path components, and only ever RECOGNISES a path that another
    source already found. It does not derive one: turning ``cfs4/hpc-staff``
    into ``/cfs4/hpc-staff`` looks obvious and is a guess, and the allocation
    database is not a mount table. That guess is rapiDU's RD-3, where a
    ``/scratch`` measurement was attributed to the wrong cluster.
    """
    wanted = [part for part in str(location or "").strip("/").split("/") if part]
    have = [part for part in str(path or "").strip("/").split("/") if part]
    return bool(wanted) and have == wanted


def _merge_allocations(allocations):
    # type: (Sequence[object]) -> List[Tuple[str, Optional[str], List[object]]]
    """Group allocations by the storage they name, keeping order.

    Merged because the allocation database issues one record per grant, not per
    filesystem. Measured at this site: ``cfs4/hpc-staff`` appears twice, as
    allocation 2718 and allocation 2719, and two rows for one directory is a
    reporting bug rather than two pieces of storage.
    """
    order = []  # type: List[str]
    groups = OrderedDict()  # type: Dict[str, Tuple[str, Optional[str], List[object]]]
    for allocation in allocations or ():
        location = str(getattr(allocation, "location", "") or "")
        claimed = getattr(allocation, "path", None)
        key = os.path.normpath(str(claimed)) if claimed else location
        if not key:
            continue
        if key not in groups:
            groups[key] = (location, str(claimed) if claimed else None, [])
            order.append(key)
        groups[key][2].append(allocation)
    return [groups[key] for key in order]


def _from_allocations(roots, allocations, site):
    # type: (List[Root], Sequence[object], Site) -> List[Root]
    """Mark allocated roots, and add a row for each allocation with no path.

    The "allocated yes, mounted no" row is the most useful thing this tool
    prints and the reason `Root` keeps the four axes apart. Measured at this
    site: the allocation database reports space on ``cfs1``, ``cfs2``, ``cfs4``
    and ``project3``, and none of those paths exist on the node that printed
    them.

    **The location never becomes ``Root.path``.** A location such as
    ``cfs4/hpc-staff`` is how an allocation database names storage; it has no
    leading slash and it is not a filesystem path. Putting it in ``path``
    produced rows that looked like relative paths, so it lives in
    ``policy["allocation_location"]`` and ``path`` stays empty. A renderer can
    then show it in a column that does not promise to be a path.
    """
    extra = []  # type: List[Root]
    for location, claimed, members in _merge_allocations(allocations):
        accounts = []  # type: List[str]
        sizes = []  # type: List[float]
        source_name = "allocation"
        for allocation in members:
            account = str(getattr(allocation, "account", "") or "")
            if account and account not in accounts:
                accounts.append(account)
            size = getattr(allocation, "size_gb", None)
            if size:
                sizes.append(float(size))
            source_name = str(getattr(allocation, "source", "") or source_name)

        reason = "%s allocation on %s" % (
            ", ".join(accounts) or "an",
            location or claimed or "unnamed storage",
        )

        matched = None  # type: Optional[Root]
        for root in roots:
            if claimed and os.path.normpath(str(claimed)) == root.path:
                matched = root
                break
            if location and _location_matches(location, root.path):
                matched = root
                break
        if matched is not None:
            matched.allocated = confirmed(reason, source=source_name)
            matched.add_source(SOURCE_ALLOCATION)
            matched.policy["allocation_location"] = location
            if sizes:
                matched.policy["allocation_gb"] = sum(sizes)
            continue

        # Nothing on this node corresponds to it. The row is still worth having,
        # and it carries no path at all rather than a path-shaped guess.
        path = os.path.normpath(str(claimed)) if claimed else ""
        if path and site.is_ignored(path):
            continue
        root = Root(path, role=site.role_for(path, "") if path else "")
        root.add_source(SOURCE_ALLOCATION)
        root.allocated = confirmed(reason, source=source_name)
        root.mounted = refuted(
            VerdictCategory.NOT_MOUNTED_HERE,
            "no filesystem for this allocation on this node",
            source=source_name,
        )
        root.present = refuted(
            VerdictCategory.NOT_PRESENT,
            "nothing to stat: the allocation names %s" % (location or path,),
            source=source_name,
        )
        root.reach = Reach.UNKNOWN
        root.reach_reason = "no path on this node to probe"
        root.policy["rank"] = RANK_PRIMARY
        if location:
            root.policy["allocation_location"] = location
            root.add_note(
                "the allocation database names this storage %s, which is a "
                "location and not a path on this node" % (location,)
            )
        if accounts:
            root.policy["allocation_accounts"] = list(accounts)
        if sizes:
            root.policy["allocation_gb"] = sum(sizes)
            if len(sizes) > 1:
                root.add_note(
                    "%d allocations on this location, totalling %s GB" % (len(sizes), sum(sizes))
                )
            else:
                root.add_note("allocation size %s GB" % (sizes[0],))
        extra.append(root)
    return extra


def discover(
    runner,  # type: Runner
    mounts,  # type: MountTable
    identity,  # type: Identity
    budget=None,  # type: Optional[Budget]
    site=None,  # type: Optional[Site]
    filesets=None,  # type: Optional[Sequence[object]]
    allocations=None,  # type: Optional[Sequence[object]]
    env=None,  # type: Optional[Dict[str, str]]
    allow_write=False,  # type: bool
):
    # type: (...) -> List[Root]
    """Every storage root this node can offer, deduplicated and probed.

    ``runner`` is accepted for symmetry with the rest of the package and because
    a future source may need a command; nothing in the current set runs one,
    which is deliberate. Discovery is all table reads, `os.access` and one level
    of `scandir`, so it stays affordable enough to run unconditionally.

    ``site`` is a `sitecfg.Site` supplied by the caller. This package never
    calls `load_site`: which config files exist is not discovery's business.

    ``filesets`` comes from the quota layer, as names or ``(name, path)`` pairs.
    ``allocations`` comes from a plugin, as `plugins.Allocation` objects.
    """
    if site is None:
        site = Site()
    environ = os.environ if env is None else env
    deadline = _deadline(budget)

    basket = _Basket()
    _from_mounts(basket, mounts, site)
    _from_env(basket, mounts, identity, environ, site, deadline)
    # **The searching sources get a share, and the probing keeps the rest.**
    # They are speculative, thousands of `stat`s of names that mostly do not
    # exist, and on a cold GPFS they can run as long as they are allowed to.
    # Measured on a meadow2 login node: a slow run spent the whole allowance
    # searching, every root then came back unprobed, and the table was 48
    # rows of `?`, the reader's home included.
    looking = Budget(total_s=budget.remaining * SEARCH_SHARE) if budget is not None else None
    _from_group_template(basket, mounts, identity, site, deadline, looking)
    _from_dir_owner(basket, mounts, identity, site, deadline, looking)
    _from_dataset_roots(basket, site, deadline, looking)
    _from_snapshot_roots(basket, site, deadline, looking)
    unplaced = _from_quota_filesets(basket, mounts, site, filesets or (), deadline, looking)

    # Probed most-wanted first, so a run that does run short loses the
    # plumbing mounts and not the reader's home. The order of the output is
    # unaffected: it is sorted by path below.
    roots = [
        _build_root(c, mounts, site, budget, allow_write)
        for c in sorted(basket, key=_probe_priority)
    ]
    roots = _dedupe(roots)
    roots.extend(_from_allocations(roots, allocations or (), site))

    # A fileset the quota layer named and this node cannot place gets a note on
    # the mountpoint it pointed into, when that mountpoint is itself a real
    # discovered root. Never on an unrelated row and never at an invented path:
    # a name that points nowhere here produces no note, because there is nothing
    # on this node it is a fact about. The caller still holds the list it passed
    # in and can see which of its filesets came back on a root.
    by_path = {}  # type: Dict[str, Root]
    for root in roots:
        by_path.setdefault(root.path, root)
    for name, anchor in unplaced:
        host = by_path.get(anchor) if anchor else None
        if host is not None:
            host.add_note(
                "the quota fileset %s could not be placed at a single path under %s"
                % (name, anchor)
            )

    # Pathless allocation rows last: they are the "allocated elsewhere" tail of
    # the table, and sorting "" first would put them above the user's home.
    roots.sort(key=lambda r: (r.path == "", r.path))
    return roots
