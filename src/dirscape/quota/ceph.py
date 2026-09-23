"""CephFS, through the directory statistics its metadata servers keep.

**No command at all.** CephFS maintains recursive totals on every directory
and publishes them as virtual extended attributes, so the answer is one
``getxattr`` per figure, served from the MDS's own accounting:

    ceph.dir.rbytes       bytes under the directory, recursively
    ceph.dir.rfiles       files under it, recursively
    ceph.quota.max_bytes  the directory's byte quota, when one is set
    ceph.quota.max_files  its file-count quota, likewise

Measured on a meadow2 login node against the HPC `/cfs3` tier, a kernel
CephFS mount: `/cfs3/kestrel-lab` reads 170,551,918,755,602 bytes against a
181,419,418,583,040 byte quota, which is the `155T of 165T` the site's quota
wrapper prints for it on meadow3, and `/cfs3/hpc-staff` reads 61.7 MB of
10 GiB. Before this backend both rows were `? ? ?`, and the measuring walk
spent its whole allowance failing to add up a 155T tree the filesystem had
already added up.

**A quota here belongs to a directory, and to everyone's files in it**, so
each row is one quota SCOPE, named by the directory that carries it, the way
a GPFS row is one fileset: `why` says "everyone's files, not only yours". An
asked directory with no quota of its own reports the nearest ancestor that
has one, figures and all, and `attribute_ceph` gives the root that same scope
name so the two meet. A directory with no quota anywhere above it reports its
own recursive usage against no limit.

Every read is deadline-guarded, because a `getxattr` against a metadata server
that has stopped answering blocks exactly as a `stat` does.
"""

import functools
import os
from typing import Callable, List, Optional, Sequence, Tuple

from ..discover.access import with_deadline
from ..discover.attribute import CEPH_FSTYPES, ceph_quota_dir, ceph_xattr
from ..discover.mounts import MountTable
from ..model import QuotaRow, QuotaSnapshot, VerdictCategory, unavailable_quota
from .base import BLOCKS, FILES, Backend, now, one_line, slice_of, snapshot

__all__ = [
    "CEPH_FSTYPES",
    "CephBackend",
    "read_ceph_dir",
]


# Directories asked about per run. Each costs four `getxattr` calls plus one
# per ancestor, all answered from the MDS cache, so this bounds a pathological
# root list rather than a realistic one.
MAX_PATHS = 16

TIME_NOTE = (
    "read live from the CephFS metadata servers, whose recursive totals can lag "
    "a write by a few seconds"
)


def read_ceph_dir(path, mountpoint, getxattr=None):
    # type: (str, str, Optional[Callable[[str, str], Optional[int]]]) -> Tuple[str, Optional[int], Optional[int], int, int]
    """``(scope, bytes, files, max_bytes, max_files)`` for the quota over ``path``.

    ``scope`` is the directory carrying the quota that governs ``path`` (the
    path itself, or its nearest quota'd ancestor), and the figures are that
    directory's. With no quota anywhere above it, ``scope`` is "" and the
    figures are the path's own recursive totals against limits of 0, which the
    model reads as "none enforced". ``getxattr`` is injectable so a test can
    describe a CephFS tree without mounting one.
    """
    read = getxattr or ceph_xattr
    scope = ceph_quota_dir(path, mountpoint, read)
    where = scope or path
    return (
        scope,
        read(where, "ceph.dir.rbytes"),
        read(where, "ceph.dir.rfiles"),
        read(where, "ceph.quota.max_bytes") or 0,
        read(where, "ceph.quota.max_files") or 0,
    )


class CephBackend(Backend):
    """``ceph.dir.*`` and ``ceph.quota.*`` for every asked directory on CephFS."""

    name = "ceph xattrs"

    # A CephFS quota is a property of the DIRECTORY, like a Lustre project,
    # so `cli._attach_quota` asks again once the roots are known.
    per_path = True

    def __init__(self, getxattr=None, max_paths=MAX_PATHS):
        # type: (Optional[Callable[[str, str], Optional[int]]], int) -> None
        self._getxattr = getxattr
        self.max_paths = max_paths

    def supported(self, runner):
        # type: (object) -> Optional[str]
        # No executable: the kernel answers. `os.getxattr` is Linux-only, and
        # the only platform this package targets, but saying so costs nothing.
        return "os.getxattr" if (self._getxattr or hasattr(os, "getxattr")) else None

    def read(self, runner, mounts, budget, paths):
        # type: (object, Optional[MountTable], object, Sequence[str]) -> QuotaSnapshot
        targets = []  # type: List[Tuple[str, str, str]]
        for path in paths or ():
            if len(targets) >= self.max_paths:
                break
            # The mount that GOVERNS the path, and only if it is CephFS: a
            # longest-CephFS-prefix test would claim a tmpfs mounted inside
            # the tree for Ceph.
            mount = mounts.enclosing_mount(path) if mounts is not None else None
            if mount is not None and (mount.fstype or "").lower() in CEPH_FSTYPES:
                targets.append((path, mount.mountpoint, _device_label(mount)))
        if not targets:
            # Not "no quota": nothing on CephFS was asked about, which is every
            # run on a cluster without it and the first sweep on one with it.
            return unavailable_quota(
                self.name,
                VerdictCategory.NO_QUOTA_BACKEND,
                "no directory on a CephFS mount was asked about",
            )

        rows = []  # type: List[QuotaRow]
        failures = []  # type: List[str]
        reported = set()  # type: set
        for path, mountpoint, device in targets:
            if budget is not None and getattr(budget, "exhausted", False):
                failures.append("%s: the quota budget ran out" % (path,))
                break
            finished, value, exc, _elapsed = with_deadline(
                functools.partial(read_ceph_dir, path, mountpoint, self._getxattr),
                slice_of(budget, share=0.25, floor=0.35, ceiling=2.0),
            )
            if not finished:
                failures.append("%s: the metadata server did not answer in time" % (path,))
                continue
            if exc is not None or not isinstance(value, tuple):
                failures.append("%s: %s" % (path, exc))
                continue
            scope, used, files, max_bytes, max_files = value
            where = scope or path
            if where in reported:
                # Two asked directories under one quota: one scope, one row.
                continue
            if used is None:
                failures.append(
                    "%s: no ceph.dir.rbytes, so this is not a CephFS directory" % (path,)
                )
                continue
            reported.add(where)
            rows.append(
                QuotaRow(scope, BLOCKS, "fileset", used, hard=max_bytes, mount=where, device=device)
            )
            if files is not None:
                rows.append(
                    QuotaRow(
                        scope, FILES, "fileset", files, hard=max_files, mount=where, device=device
                    )
                )

        read_at = now()
        return snapshot(
            self.name,
            rows,
            VerdictCategory.BACKEND_FAILED,
            one_line("; ".join(failures)) or "CephFS published no directory statistics",
            taken_at=read_at if rows else None,
            read_at=read_at,
            time_note=TIME_NOTE if rows else "",
        )


def _device_label(mount):
    # type: (object) -> str
    """What to call the filesystem in a sentence: its CephFS name when mounted with one.

    The device string is the monitor list (`203.0.113.90,...,203.0.113.94:/`
    on HPC), which says where the cluster is and not which filesystem this
    is; `mds_namespace=` or `fs=` in the options names it (`cfs3`).
    """
    for option in getattr(mount, "option_list", ()) or ():
        key, _, value = option.partition("=")
        if key in ("mds_namespace", "fs") and value:
            return "CephFS %s" % (value,)
    return getattr(mount, "mountpoint", "") or ""
