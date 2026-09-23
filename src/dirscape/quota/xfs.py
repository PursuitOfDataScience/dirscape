"""XFS, through ``xfs_quota``, with ``statvfs`` as a labelled floor.

Net-new: rapiDU has no XFS backend, so there is nothing to port and every
decision here is first-hand.

**The measured case for asking the mount table first.** Both xfs mounts on the
node this was written on are mounted ``noquota``::

    /dev/sda1 /scratch/local xfs rw,relatime,attr2,inode64,...,noquota 0 0
    /dev/sda1 /tmp           xfs rw,relatime,attr2,inode64,...,noquota 0 0

With that option the kernel cannot answer a quota question at all, so running
the command is guaranteed waste, and the waste is not small::

    $ xfs_quota -x -c "report -h" /scratch/local ; echo rc=$?
    rc=0
    stdout: 0 bytes
    stderr: 817798 bytes of "XFS_GETQUOTA: Operation not permitted"

Exit 0, empty stdout, 800 KB of stderr. `Completed.failed` catches it because
stdout is empty and stderr is not, and `Completed.diagnostic` reduces it to one
line, which is why the reason a user sees is a sentence and not a screenful.
The mount option is checked first anyway, because a refusal that was predicted
is a better answer than a refusal that was provoked.

**``statvfs`` is capacity, and capacity is not a quota.** On a filesystem with
no per-user limit it reports the whole filesystem; on an export that enforces
one, the server reports the *quota* through the same fields, and Isilon and
NetApp both do this (rapiDU measured a Brook login node where a 14 GiB
"filesystem" was in fact a 14 GiB home quota at 48%). Nothing in ``statvfs``
distinguishes the two, so the figure is offered with that ambiguity stated:
`VerdictCategory.NO_QUOTA_ENFORCED`, the distinction spelled out in the
snapshot's ``reason``, and a note on every row. rapiDU's ``mount_report()``
documents the same boundary and for the same reason declines to call it a
quota.
"""

import os
import re
from typing import Callable, List, Optional, Sequence, Tuple

from ..model import QuotaRow, QuotaSnapshot, VerdictCategory, unavailable_quota
from .base import (
    BLOCKS,
    FILES,
    Backend,
    charge,
    clean_grace,
    grouped_failures,
    norm_scope,
    now,
    one_line,
    parse_count,
    parse_limit,
    parse_size,
    slice_of,
    snapshot,
)

__all__ = [
    "XFS_FSTYPES",
    "XFS_BIN_DIRS",
    "XfsBackend",
    "parse_xfs_report",
    "statvfs_capacity",
    "CAPACITY_REASON",
    "CAPACITY_NOTE",
]


XFS_FSTYPES = ("xfs",)

# xfs_quota lives in /usr/sbin, which is on a login shell's PATH but not
# necessarily on a batch job's. Measured here: /usr/sbin/xfs_quota, and
# `command -v xfs_quota` did find it, but naming the directories costs nothing
# and a sbatch script with a trimmed PATH is the normal case.
XFS_BIN_DIRS = ("/usr/sbin", "/sbin", "/usr/local/sbin")

# `Project quota on /work (/dev/sdb1)`. The section header publishes the scope,
# the mount point and the device, which is three facts this backend would
# otherwise have to infer.
_SECTION = re.compile(r"^(user|group|project)\s+quota\s+on\s+(\S+)\s*\(([^)]*)\)", re.IGNORECASE)

# The units line under the section header: `Blocks` or `Inodes`. It decides the
# row kind, so it is read rather than assumed from which command was run.
_UNITS = re.compile(r"^\s*(blocks|inodes)\s*$", re.IGNORECASE)

_PERMISSION_MARKERS = ("operation not permitted", "permission denied", "not authorized")

CAPACITY_REASON = (
    "this is the filesystem's own capacity from statvfs, not a quota anybody "
    "set for you. On a filesystem with no limit it reports the whole device; "
    "on an export that enforces one, the server reports the quota through the "
    "same fields, and nothing in statvfs tells the two apart"
)

CAPACITY_NOTE = "capacity from statvfs, not an enforced quota"


def _parse_count_h(token):
    # type: (str) -> Optional[int]
    """An inode count that may carry a suffix.

    ``xfs_quota -h`` abbreviates counts as well as sizes, so an inode column
    can read ``1.2k``. The scale is 1024-based, which is what xfs_quota's own
    formatter uses, so `parse_size` is the right reader for the suffixed form.
    """
    exact = parse_count(token)
    if exact is not None:
        return exact
    return parse_size(token)


def parse_xfs_report(text, device_hint=""):
    # type: (str, str) -> List[QuotaRow]
    """Rows from ``xfs_quota -c "report -h"`` output, any number of sections.

    Every section, not just the first: a filesystem can report user, group and
    project quotas in one run and the project section is the one this backend
    exists for, while the user section is frequently the one that is actually
    set. Taking the first would make which of them you see depend on the order
    xfs_quota happens to print.
    """
    rows = []  # type: List[QuotaRow]
    scope = ""
    mount = ""
    device = device_hint
    kind = BLOCKS
    for raw in (text or "").splitlines():
        line = raw.strip()
        if not line or line.startswith("---"):
            continue
        section = _SECTION.match(line)
        if section:
            scope = norm_scope(section.group(1))
            mount = section.group(2)
            device = section.group(3).strip() or device_hint
            continue
        units = _UNITS.match(line)
        if units:
            kind = BLOCKS if units.group(1).lower() == "blocks" else FILES
            continue
        lowered = line.lower()
        if lowered.startswith(("project id", "user id", "group id")) or lowered.startswith("id "):
            continue
        if not scope:
            continue
        row = _report_row(line, scope, mount, device, kind)
        if row is not None:
            rows.append(row)
    return rows


def _report_row(line, scope, mount, device, kind):
    # type: (str, str, str, str, str) -> Optional[QuotaRow]
    parts = line.split()
    if len(parts) < 4:
        return None
    name = parts[0]
    parser = parse_size if kind == BLOCKS else _parse_count_h  # type: Callable[[str], Optional[int]]
    used = parser(parts[1])
    if used is None:
        return None
    soft = parse_limit(parts[2], parser)
    hard = parse_limit(parts[3], parser)
    # What remains is a warning counter and the grace timer, which xfs_quota
    # prints as `00 [--------]` when nothing is running and `00 [6 days]` when
    # something is. The timer is two tokens in the second case, so the tail is
    # joined rather than indexed, and the counter is dropped by position only
    # when it really is a bare count: some builds omit it entirely, and
    # assuming it would read the grace timer as the warning count and lose the
    # one field that means writes are about to stop.
    tail = list(parts[4:])
    if tail and tail[0].isdigit():
        tail = tail[1:]
    grace = clean_grace(" ".join(tail).strip("[]").strip())
    row = QuotaRow(
        name,
        kind,
        scope,
        used,
        soft,
        hard,
        grace,
        mount or None,
        device=device or name,
    )
    return row


def statvfs_capacity(path, mount="", statvfs=os.statvfs, source="statvfs"):
    # type: (str, str, Callable[[str], object], str) -> QuotaSnapshot
    """Capacity for the filesystem holding ``path``, labelled as not a quota.

    ``statvfs`` is injected so no test depends on a live mount. The
    denominator is ``used + avail`` rather than the whole filesystem, which is
    what ``df`` puts in ``Use%``: reserved blocks are in neither, and dividing
    by the total reported a filesystem with ``f_bavail`` at zero as
    comfortably short of full. A default ext4 holds 5% back, so that is a
    false all-clear on a filesystem nobody but root can write to.

    ``f_files`` of 0 means the filesystem reports no inode counts, which comes
    back as no inode row rather than as a limit of zero.
    """
    try:
        stat = statvfs(path)
    except OSError as exc:
        return unavailable_quota(
            source,
            VerdictCategory.BACKEND_FAILED,
            "statvfs(%s) failed: %s" % (path, exc),
        )
    frsize = getattr(stat, "f_frsize", 0) or getattr(stat, "f_bsize", 0) or 0
    blocks = getattr(stat, "f_blocks", 0) or 0
    if not frsize or not blocks:
        return unavailable_quota(
            source,
            VerdictCategory.NO_QUOTA_ENFORCED,
            "statvfs(%s) reports no block counts, so there is no capacity to "
            "report either" % (path,),
        )
    total = blocks * frsize
    avail = (getattr(stat, "f_bavail", 0) or 0) * frsize
    used = max(0, total - (getattr(stat, "f_bfree", 0) or 0) * frsize)
    reserved = max(0, (getattr(stat, "f_bfree", 0) or 0) - (getattr(stat, "f_bavail", 0) or 0))
    label = os.path.basename((mount or path).rstrip("/")) or (mount or path)

    note = CAPACITY_NOTE
    if reserved:
        # Said out loud because the three figures otherwise do not add up, and
        # a reader subtracting two of them from the third finds bytes missing
        # with nothing to blame.
        note += "; %d blocks are reserved for root and are in neither figure" % (reserved,)
    rows = [
        QuotaRow(
            label,
            BLOCKS,
            "filesystem",
            used,
            None,
            used + avail,
            "",
            mount or None,
            device=str(getattr(stat, "f_fsid", "") or ""),
            note=note,
        )
    ]
    files_total = getattr(stat, "f_files", 0) or 0
    if files_total:
        ffree = getattr(stat, "f_ffree", 0) or 0
        favail = getattr(stat, "f_favail", ffree) or 0
        files_used = max(0, files_total - ffree)
        rows.append(
            QuotaRow(
                label,
                FILES,
                "filesystem",
                files_used,
                None,
                files_used + favail,
                "",
                mount or None,
                note=CAPACITY_NOTE,
            )
        )
    read_at = now()
    return snapshot(
        source,
        rows,
        VerdictCategory.NO_QUOTA_ENFORCED,
        "statvfs(%s) reported no usable capacity" % (path,),
        # available=True because the figures are real and measured; the
        # category says what they are NOT. Those are two different questions
        # and the contract carries them in two different fields.
        category=VerdictCategory.NO_QUOTA_ENFORCED,
        reason=CAPACITY_REASON,
        taken_at=read_at,
        read_at=read_at,
    )


class XfsBackend(Backend):
    """``xfs_quota`` project and user reports, then ``statvfs`` as a floor."""

    name = "xfs_quota"

    # A project quota is a property of the directory, not the filesystem,
    # so this backend has to be asked again once the roots are known.
    per_path = True

    def __init__(
        self,
        extra_dirs=XFS_BIN_DIRS,  # type: Sequence[str]
        statvfs=os.statvfs,  # type: Callable[[str], object]
        with_inodes=True,  # type: bool
        capacity_fallback=True,  # type: bool
    ):
        # type: (...) -> None
        self.extra_dirs = tuple(extra_dirs or ())
        self.statvfs = statvfs
        self.with_inodes = with_inodes
        self.capacity_fallback = capacity_fallback

    def supported(self, runner):
        # type: (object) -> Optional[str]
        """Present when the tool is there, or when statvfs can still answer.

        The fallback is why this returns a marker rather than None on a node
        with no ``xfs_quota``: capacity from the kernel needs no tool at all,
        and on a machine whose only storage is a local disk it is the only
        reading anybody can get.
        """
        found = runner.available("xfs_quota", extra_dirs=self.extra_dirs)
        if found:
            return found
        return "statvfs" if self.capacity_fallback else None

    def read(self, runner, mounts, budget, paths):
        # type: (object, object, object, Sequence[str]) -> QuotaSnapshot
        exe = runner.available("xfs_quota", extra_dirs=self.extra_dirs)
        targets = self._targets(mounts, paths)
        if not targets:
            return unavailable_quota(
                self.name,
                VerdictCategory.NO_QUOTA_BACKEND,
                "no xfs filesystem holds any of the paths asked about",
            )

        rows = []  # type: List[QuotaRow]
        failures = []  # type: List[Tuple[str, str]]
        refused = []  # type: List[Tuple[str, str]]
        disabled = []  # type: List[str]
        for mount, device, noquota in targets:
            if noquota:
                # The kernel published the answer, so the command is not run.
                disabled.append(mount)
                continue
            if not exe or (budget is not None and getattr(budget, "exhausted", False)):
                break
            for command in self._commands():
                result = runner.run([exe, "-x", "-c", command, mount], timeout=slice_of(budget))
                charge(budget, result)
                if result.failed:
                    message = result.diagnostic
                    subject = "%s %s" % (mount, command)
                    if _is_permission(message):
                        refused.append((subject, message))
                    else:
                        failures.append((subject, message))
                    continue
                rows.extend(parse_xfs_report(result.stdout, device))

        if rows:
            read_at = now()
            return snapshot(
                self.name,
                rows,
                _empty_category(failures, refused, disabled),
                _why_no_quota(exe, disabled, failures, refused),
                taken_at=read_at,
                read_at=read_at,
                # Kept even on success: one mount can answer while another
                # refuses, and dropping the refusal would present a partial
                # reading as a complete one.
                reason=one_line(grouped_failures(list(failures) + list(refused))),
                time_note="read live from xfs_quota",
            )

        if self.capacity_fallback:
            first = targets[0]
            capacity = statvfs_capacity(
                first[0], mount=first[0], statvfs=self.statvfs, source=self.name + " (statvfs)"
            )
            if capacity.rows:
                capacity.reason = one_line(
                    CAPACITY_REASON + ". " + _why_no_quota(exe, disabled, failures, refused)
                )
                return capacity

        return unavailable_quota(
            self.name,
            _empty_category(failures, refused, disabled),
            _why_no_quota(exe, disabled, failures, refused),
        )

    def _commands(self):
        # type: () -> List[str]
        # Blocks and inodes are two separate reports, and the inode one is not
        # optional in spirit: an inode limit is the one that bites without
        # warning, which is why this package emits both kinds everywhere.
        if self.with_inodes:
            return ["report -h", "report -h -i"]
        return ["report -h"]

    def _targets(self, mounts, paths):
        # type: (object, Sequence[str]) -> List[Tuple[str, str, bool]]
        """The xfs mounts governing the asked paths, with their quota options.

        ``(mountpoint, device, noquota)``. A path not on xfs yields nothing:
        ``xfs_quota`` on a GPFS path is a question that should not be asked.
        """
        if mounts is None:
            return []
        out = []  # type: List[Tuple[str, str, bool]]
        seen = set()
        for path in paths or []:
            best = None
            for mount in mounts.mounts_of_type(XFS_FSTYPES):
                stem = mount.mountpoint.rstrip("/") or "/"
                target = os.path.normpath(path)
                if target == stem or target.startswith(stem.rstrip("/") + "/"):
                    if best is None or len(stem) > len(best.mountpoint.rstrip("/")):
                        best = mount
            if best is not None and best.mountpoint not in seen:
                seen.add(best.mountpoint)
                out.append(
                    (
                        best.mountpoint,
                        best.device,
                        best.has_option("noquota") or not _quota_option(best),
                    )
                )
        return out


def _quota_option(mount):
    # type: (object) -> bool
    """Whether this mount advertises any quota enforcement at all.

    xfs names them explicitly: ``uquota``, ``gquota``, ``pquota`` and the
    ``*noenforce`` variants, plus the ``usrquota`` / ``grpquota`` /
    ``prjquota`` spellings. Absence is not proof (a filesystem can have quotas
    enabled without the option appearing in ``/proc/self/mounts`` on every
    kernel), so this only ever *adds* confidence: `noquota` is the refutation,
    and this is what stops the code from claiming a positive.
    """
    options = getattr(mount, "option_list", [])
    return any(
        name.startswith(("uquota", "gquota", "pquota", "usrquota", "grpquota", "prjquota"))
        for name in options
    )


def _is_permission(text):
    # type: (str) -> bool
    lowered = (text or "").lower()
    return any(marker in lowered for marker in _PERMISSION_MARKERS)


def _empty_category(failures, refused, disabled):
    # type: (Sequence[object], Sequence[object], Sequence[str]) -> str
    if disabled and not failures and not refused:
        return VerdictCategory.NO_QUOTA_ENFORCED
    if refused and not failures:
        return VerdictCategory.PERMISSION_TO_ASK_DENIED
    if failures:
        return VerdictCategory.BACKEND_FAILED
    return VerdictCategory.NO_QUOTA_BACKEND


def _why_no_quota(exe, disabled, failures, refused):
    # type: (Optional[str], Sequence[str], Sequence[Tuple[str, str]], Sequence[Tuple[str, str]]) -> str
    parts = []  # type: List[str]
    if disabled:
        parts.append(
            "the kernel mounts %s with noquota, so no quota can be enforced there"
            % (", ".join(disabled),)
        )
    if not exe:
        parts.append("xfs_quota is not installed on this node")
    if failures or refused:
        parts.append(grouped_failures(list(failures) + list(refused)))
    return one_line("; ".join(p for p in parts if p)) or "xfs_quota reported nothing"
