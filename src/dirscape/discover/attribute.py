"""Which quota scope actually governs a root.

A path's quota is not a property of its device. On this cluster one GPFS device,
``meadow3_cap``, is mounted at ``/home``, ``/project``, ``/software`` and
``/programs``, and those are four different filesets with four different
quotas. Keying on the device is rapiDU's RD-18: ``rapidu -Q ~`` and
``rapidu -Q /software`` return byte-identical output there, for two directories
that have nothing to do with each other. So the scope is looked up **per path**.

For GPFS the lookup is ``mmlsattr -L <path>``, and that is the only correct
route for an unprivileged user. The obvious alternative does not work:

    $ mmlsfileset meadow3_cap
    No filesets found owned by this user jdoe42
    mmlsfileset: tslsfileset failed. Error code 2.
    mmlsfileset: Command failed. Examine previous error messages.
    $ echo $?
    0

Note the exit status. **The exit status is never consulted anywhere in this
module**, and a test enforces that by reading this file. ``Completed.failed``
is consulted instead, because it also treats "exited 0, said nothing on stdout,
complained on stderr" as the failure it plainly is.

That is still not enough on its own, which is the one hole worth naming: a
command that exits 0 with *partial* stdout and an error on stderr slips past
``failed``, because stdout is not empty. So every backend here also requires its
own parse to have produced something. Two independent gates, and the row stays
``unknown`` unless both agree.
"""

import errno
import os
from typing import Callable, Dict, List, Optional, Sequence

from ..model import Root, Verdict, VerdictCategory, confirmed, unknown
from ..runner import Budget, Runner
from .access import with_deadline
from .mounts import Mount, MountTable

__all__ = [
    "CEPH_FSTYPES",
    "attribute_ceph",
    "ceph_quota_dir",
    "ceph_xattr",
    "GPFS_TOOL_DIRS",
    "GPFS_ROOT_FILESET",
    "XFS_TOOL_DIRS",
    "parse_mmlsattr",
    "parse_lfs_project",
    "parse_xfs_lsproj",
    "attribute",
    "attribute_gpfs",
    "attribute_lustre",
    "attribute_xfs",
]


# GPFS tools are not on PATH here and are world-executable in this directory:
#
#     $ command -v mmlsattr            -> (nothing)
#     $ ls -l /usr/lpp/mmfs/bin/mmlsattr
#     -r-xr-xr-x 1 root root 24328 mmlsattr
#
# A probe that used `shutil.which` alone would conclude GPFS is absent on a
# GPFS cluster. `Runner.available` takes the extra directories for this reason.
GPFS_TOOL_DIRS = ("/usr/lpp/mmfs/bin",)

# xfs_io and xfs_quota live in sbin, which is not on a normal user's PATH on
# every distribution even though the binaries are executable by anyone.
XFS_TOOL_DIRS = ("/usr/sbin", "/sbin")

# What GPFS calls a filesystem's own top level. It is a fileset name in the
# backends' output and it is not a scope anybody was granted, so it is flagged
# rather than presented as one.
GPFS_ROOT_FILESET = "root"


def parse_mmlsattr(stdout):
    # type: (str) -> str
    """The fileset name out of ``mmlsattr -L`` output, or "" if it is not there.

    Real output, measured on this cluster:

        file name:            /project/hpc
        metadata replication: 1 max 2
        storage pool name:    system
        fileset name:         project-hpc
        snapshot name:

    Matched on an exact field name rather than on a substring, because
    ``snapshot name:`` is two lines below and a looser match picks up the wrong
    one (usually empty, which would read as "no fileset").
    """
    for line in stdout.splitlines():
        label, separator, value = line.partition(":")
        if not separator:
            continue
        if label.strip().lower() == "fileset name":
            return value.strip()
    return ""


def parse_lfs_project(stdout):
    # type: (str) -> str
    """The project id out of ``lfs project -d`` output.

    Documented format is the id, a flag column, then the path:

        1234   P /lustre/project/thing

    Returns "" for a missing or unparseable id, and "" for project 0, which is
    Lustre's "no project assigned" and is not a scope.

    **Unverified against a live filesystem.** There is no Lustre mount on this
    cluster, so this parser is driven by fixtures and by the documented format
    only. Flagged here rather than in a commit message because the next person
    to touch it should know it has never run against the real tool.
    """
    for line in stdout.splitlines():
        parts = line.split()
        if not parts:
            continue
        try:
            projid = int(parts[0])
        except ValueError:
            continue
        if projid == 0:
            return ""
        return str(projid)
    return ""


def parse_xfs_lsproj(stdout):
    # type: (str) -> str
    """The project id out of ``xfs_io -c lsproj`` output.

    Measured: ``xfs_io -c lsproj /tmp`` prints ``projid = 0`` and exits 0 for an
    unprivileged caller, so unlike the GPFS case this needs no special access.
    Project 0 means no project, and comes back as "".
    """
    for line in stdout.splitlines():
        label, separator, value = line.partition("=")
        if not separator:
            continue
        if label.strip().lower() in ("projid", "project id"):
            stripped = value.strip()
            if stripped and stripped != "0":
                return stripped
    return ""


def _extra_dirs(site, defaults):
    # type: (Optional[object], Sequence[str]) -> Sequence[str]
    """Tool directories from the site config, with the built-ins appended.

    Site first: a site that names its own directory has a reason, and the
    built-in is still tried afterwards so naming one does not lose the other.
    """
    dirs = []  # type: List[str]
    for directory in getattr(site, "extra_bin_dirs", ()) or ():
        if directory and directory not in dirs:
            dirs.append(str(directory))
    for directory in defaults:
        if directory not in dirs:
            dirs.append(directory)
    return tuple(dirs)


def _no_budget(budget):
    # type: (Optional[Budget]) -> bool
    return budget is not None and budget.exhausted


def attribute(root, runner, mounts, budget=None, site=None):
    # type: (Root, Runner, MountTable, Optional[Budget], Optional[object]) -> Verdict
    """Resolve the quota scope governing ``root`` and set ``root.fileset``.

    The return value describes whether the scope was determined; the mutation of
    ``root.fileset`` and ``root.notes`` is the product. A filesystem with no
    fileset concept is a legitimate state, not an error: the fileset stays empty
    and the verdict is ``NOT_SUPPORTED``, which is a transient category so
    nothing downstream can render it as a refusal.
    """
    fstype = (root.fstype or "").lower()
    if not fstype:
        mount = mounts.enclosing_mount(root.path) if mounts is not None else None
        if mount is not None:
            fstype = (mount.fstype or "").lower()

    if not root.path:
        return unknown(VerdictCategory.NOT_PROBED, "no path to attribute")
    if _no_budget(budget):
        return unknown(VerdictCategory.NOT_PROBED, "time budget exhausted")

    if fstype == "gpfs":
        return attribute_gpfs(root, runner, budget, site)
    if fstype == "lustre":
        return attribute_lustre(root, runner, budget, site)
    if fstype in CEPH_FSTYPES:
        return attribute_ceph(root, mounts, budget)
    if fstype in ("xfs", "ext4", "ext3", "ext2"):
        return attribute_xfs(root, runner, mounts, budget, site)

    note = "%s has no fileset or project scope to look up" % (fstype or "this filesystem",)
    root.add_note(note)
    return unknown(VerdictCategory.NOT_SUPPORTED, note)


def attribute_gpfs(root, runner, budget=None, site=None):
    # type: (Root, Runner, Optional[Budget], Optional[object]) -> Verdict
    """``mmlsattr -L <path>``, gated on both ``failed`` and the parse."""
    tool = runner.available("mmlsattr", extra_dirs=_extra_dirs(site, GPFS_TOOL_DIRS))
    if not tool:
        note = "mmlsattr not found, so the GPFS fileset for this path is unknown"
        root.add_note(note)
        return unknown(VerdictCategory.NOT_SUPPORTED, note, source="mmlsattr")

    timeout = budget.slice_for(share=0.25, floor=0.35, ceiling=2.0) if budget else None
    result = runner.run([tool, "-L", root.path], timeout=timeout)

    # Gate one: the runner's own verdict, which knows that a zero exit status is
    # not evidence of success for this family of tools.
    if result.failed:
        return unknown(
            VerdictCategory.BACKEND_FAILED,
            result.diagnostic,
            source="mmlsattr",
            elapsed_s=result.elapsed_s,
        )

    fileset = parse_mmlsattr(result.stdout)

    # Gate two: the parse. This is the hole gate one cannot close. `failed` is
    # False when a command exits 0 with SOME stdout and an error on stderr,
    # because stdout is not empty; without this second check that case would set
    # an empty fileset and report success.
    if not fileset:
        return unknown(
            VerdictCategory.BACKEND_FAILED,
            "mmlsattr gave no fileset name: %s" % (result.diagnostic,),
            source="mmlsattr",
            elapsed_s=result.elapsed_s,
        )

    root.fileset = fileset
    if fileset == GPFS_ROOT_FILESET:
        # GPFS calls a filesystem's own top level the "root" fileset, which is
        # not a quota scope anybody was granted. Measured: every
        # /gpfs/<cluster>/<tier> mountpoint reports this, and so do
        # /scratch/meadow2 and its subdirectories, because meadow2_perf has no
        # fileset structure at all. The name is kept because the quota backends
        # really do report a fileset called "root" and a later join needs the
        # key, but the flag and the note let a renderer print "no fileset"
        # instead of a word that reads like a scope.
        root.policy["fileset_is_filesystem_root"] = True
        root.add_note(
            "this filesystem reports no fileset for this path: %s is the "
            "filesystem's own top level, not a quota scope" % (GPFS_ROOT_FILESET,)
        )
    return confirmed("fileset %s" % (fileset,), source="mmlsattr -L", elapsed_s=result.elapsed_s)


def attribute_lustre(root, runner, budget=None, site=None):
    # type: (Root, Runner, Optional[Budget], Optional[object]) -> Verdict
    """``lfs project -d <path>`` for the project id. Fixture-verified only."""
    tool = runner.available("lfs", extra_dirs=_extra_dirs(site, XFS_TOOL_DIRS))
    if not tool:
        note = "lfs not found, so the Lustre project for this path is unknown"
        root.add_note(note)
        return unknown(VerdictCategory.NOT_SUPPORTED, note, source="lfs")

    timeout = budget.slice_for(share=0.25, floor=0.35, ceiling=2.0) if budget else None
    result = runner.run([tool, "project", "-d", root.path], timeout=timeout)
    if result.failed:
        return unknown(
            VerdictCategory.BACKEND_FAILED,
            result.diagnostic,
            source="lfs project",
            elapsed_s=result.elapsed_s,
        )

    projid = parse_lfs_project(result.stdout)
    if not projid:
        note = "no Lustre project id set on this path"
        root.add_note(note)
        return unknown(
            VerdictCategory.NOT_SUPPORTED,
            note,
            source="lfs project",
            elapsed_s=result.elapsed_s,
        )

    root.fileset = projid
    root.add_note("Lustre project id %s" % (projid,))
    return confirmed("project %s" % (projid,), source="lfs project -d", elapsed_s=result.elapsed_s)


# --------------------------------------------------------------------------
# CephFS: a quota is an attribute of a directory
# --------------------------------------------------------------------------

#: The kernel client, and ceph-fuse under both names it has used.
CEPH_FSTYPES = ("ceph", "fuse.ceph", "fuse.ceph-fuse")


def ceph_xattr(path, name):
    # type: (str, str) -> Optional[int]
    """One CephFS virtual xattr as an integer, or None when it is not set.

    ``ENODATA`` is the normal answer for a quota nobody set, so it is None and
    not an error. Anything else propagates: a filesystem that does not know
    the attribute at all is not CephFS, and the caller wants to know.
    """
    try:
        raw = os.getxattr(path, name)
    except OSError as exc:
        if exc.errno in (errno.ENODATA, getattr(errno, "ENOATTR", errno.ENODATA)):
            return None
        raise
    text = raw.decode("ascii", "replace").strip().rstrip("\x00")
    return int(text) if text.isdigit() else None


def ceph_quota_dir(path, mountpoint, getxattr=None):
    # type: (str, str, Optional[Callable[[str, str], Optional[int]]]) -> str
    """The directory whose CephFS quota governs ``path``, or "" when none does.

    The path itself when it carries `ceph.quota.max_bytes` or `max_files`,
    else the nearest ancestor that does, up to and including the mountpoint.
    O(depth) xattr reads, each answered from the metadata server's cache.
    """
    read = getxattr or ceph_xattr
    top = (mountpoint or "/").rstrip("/") or "/"
    here = (path or "").rstrip("/") or "/"
    while True:
        if read(here, "ceph.quota.max_bytes") or read(here, "ceph.quota.max_files"):
            return here
        if here == top or not here.startswith(top.rstrip("/") + "/"):
            return ""
        here = os.path.dirname(here)


def attribute_ceph(root, mounts, budget=None, getxattr=None):
    # type: (Root, Optional[MountTable], Optional[Budget], Optional[Callable[[str, str], Optional[int]]]) -> Verdict
    """The CephFS quota directory governing this root, as its scope.

    Named by its PATH, because a CephFS quota can sit on any directory and two
    of them may share a basename; the path is the one name that cannot
    collide. Measured on HPC's `/cfs3`: `/cfs3/kestrel-lab` and
    `/cfs3/hpc-staff` each carry their own quota, and with no scope recorded
    `dirscape tree` grouped them as one "no fileset" line showing the first
    one's `155T of 165T` for both.
    """
    mount = mounts.enclosing_mount(root.path) if mounts is not None else None
    top = mount.mountpoint if mount is not None else "/"
    timeout = budget.slice_for(share=0.25, floor=0.35, ceiling=2.0) if budget else None
    finished, value, exc, elapsed = with_deadline(
        lambda: ceph_quota_dir(root.path, top, getxattr), timeout
    )
    if not finished or exc is not None:
        reason = "the CephFS metadata server did not answer" if not finished else str(exc)
        return unknown(VerdictCategory.BACKEND_FAILED, reason, source="ceph xattrs")
    scope = str(value or "")
    if not scope:
        note = "no CephFS quota is set on this directory or any directory above it"
        root.add_note(note)
        return unknown(VerdictCategory.NOT_SUPPORTED, note, source="ceph xattrs", elapsed_s=elapsed)
    root.fileset = scope
    if scope != root.path.rstrip("/"):
        root.add_note("under the CephFS quota set on %s" % (scope,))
    return confirmed("CephFS quota on %s" % (scope,), source="ceph xattrs", elapsed_s=elapsed)


# The mount options under which ext2/3/4 enforce a limit. `usrjquota=` and
# `grpjquota=` name journalled quota files and imply the matching type.
_EXT_QUOTA_OPTIONS = ("quota", "usrquota", "grpquota", "prjquota", "usrjquota", "grpjquota")


def attribute_xfs(root, runner, mounts, budget=None, site=None):
    # type: (Root, Runner, MountTable, Optional[Budget], Optional[object]) -> Verdict
    """Best-effort XFS project id, refused from the mount options when possible.

    Measured: both xfs mounts on this node carry ``noquota`` in their options,
    so the mount table alone settles the question and no command is run at all.
    That is the cheapest correct answer available and it costs nothing to check.
    """
    mount = mounts.enclosing_mount(root.path) if mounts is not None else None
    fstype = ((mount.fstype if mount is not None else root.fstype) or "xfs").lower()
    if mount is not None and isinstance(mount, Mount):
        has_project_quota = mount.has_option("prjquota") or mount.has_option("pquota")
        # ext2/3/4 print no `noquota`; they enforce a limit only when mounted
        # with a quota option, which the kernel then lists in the mount table
        # (`ext4_enable_quotas` turns limits on per `usrquota`, `grpquota` and
        # `prjquota`). So on ext* the ABSENCE of all of them is the same
        # evidence `noquota` is on XFS. Measured on a Sylvia login node:
        # `/tmp` is `ext4 rw,relatime,stripe=64`, and `quota -v` there lists
        # no quota for it.
        unquoted_ext = fstype.startswith("ext") and not any(
            option == name or option.startswith(name + "=")
            for option in mount.option_list
            for name in _EXT_QUOTA_OPTIONS
        )
        if (mount.has_option("noquota") and not has_project_quota) or unquoted_ext:
            if unquoted_ext:
                note = "mounted with no quota option, so no limit is enforced here"
            else:
                note = "mounted noquota, so no project scope exists here"
            root.add_note(note)
            # **Recorded as KNOWLEDGE, not only as a note.** `noquota` in the
            # mount options is positive evidence that no limit is enforced
            # here, which is a different fact from nobody having measured one,
            # and the renderer has always kept those apart (`none` against
            # `?`). Without the flag the limit cell read `?` on two rows where
            # the mount table had already settled the question, which is the
            # tool withholding something it knows.
            #
            # One direction only: the ABSENCE of `noquota` says nothing, so
            # nothing is inferred from it.
            root.policy["no_quota_enforced"] = True
            return unknown(VerdictCategory.NOT_SUPPORTED, note, source="/proc/self/mounts")

    label = "XFS" if fstype == "xfs" else fstype
    tool = runner.available("xfs_io", extra_dirs=_extra_dirs(site, XFS_TOOL_DIRS))
    if not tool:
        note = "xfs_io not found, so any %s project id for this path is unknown" % (label,)
        root.add_note(note)
        return unknown(VerdictCategory.NOT_SUPPORTED, note, source="xfs_io")

    timeout = budget.slice_for(share=0.25, floor=0.35, ceiling=2.0) if budget else None
    result = runner.run([tool, "-c", "lsproj", root.path], timeout=timeout)
    if result.failed:
        return unknown(
            VerdictCategory.BACKEND_FAILED,
            result.diagnostic,
            source="xfs_io lsproj",
            elapsed_s=result.elapsed_s,
        )

    projid = parse_xfs_lsproj(result.stdout)
    if not projid:
        note = "no %s project id set on this path" % (label,)
        root.add_note(note)
        return unknown(
            VerdictCategory.NOT_SUPPORTED,
            note,
            source="xfs_io lsproj",
            elapsed_s=result.elapsed_s,
        )

    root.fileset = projid
    root.add_note("%s project id %s" % (label, projid))
    return confirmed("project %s" % (projid,), source="xfs_io lsproj", elapsed_s=result.elapsed_s)


def attribute_all(roots, runner, mounts, budget=None, site=None):
    # type: (Sequence[Root], Runner, MountTable, Optional[Budget], Optional[object]) -> Dict[str, Verdict]
    """Attribute a list of roots, one command per root, keyed by path.

    One command per root and not one per device, which is the whole point of
    RD-18. Stops spending as soon as the budget is gone, and the remaining roots
    get ``NOT_PROBED`` rather than being dropped from the result.
    """
    out = {}  # type: Dict[str, Verdict]
    for root in roots:
        out[root.path] = attribute(root, runner, mounts, budget, site)
    return out
