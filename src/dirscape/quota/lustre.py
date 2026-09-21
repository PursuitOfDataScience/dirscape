"""Lustre, through ``lfs quota``. Three scopes, and one honest doubt channel.

**Fixture-only.** No Lustre is mounted on the cluster this package was written
on and ``lfs`` is not installed there, so this backend has never run live: it
is built from the output shapes rapiDU's parser and docstrings record, and its
tests replay recorded transcripts. Everything here is therefore a claim about
``lfs quota``'s documented and previously observed behaviour rather than a
measurement of this site, and it is marked as such on purpose.

Two things it gets right that a one-scope reader cannot:

**Project quotas are the point.** ``-u`` alone was wrong in the case that
matters, because a project quota is the standard mechanism for a per-lab or
per-directory allocation at a Lustre site and it is what a shared research
directory is charged against. A user over their project quota who asked got
their personal user quota instead: a real number, correctly parsed, that is
not the one stopping them from writing.

**Bracketed figures set `figure_note`, never `time_note`.** When ``lfs`` cannot
reach an OST it wraps the figure it could not verify in brackets and prints
``The data in "[]" is inaccurate``. That is doubt about the NUMBER; the reading
is as fresh as any other. The contract keeps the two channels apart because a
reading can be fresh and wrong or stale and exact, and one "confidence" field
cannot say which.
"""

import os
import re
from typing import List, Optional, Sequence, Tuple

from ..model import QuotaRow, QuotaSnapshot, VerdictCategory, unavailable_quota
from .base import (
    BLOCKS,
    FILES,
    Backend,
    charge,
    clean_grace,
    current_user,
    enclosing_mount_of_type,
    grouped_failures,
    norm_scope,
    now,
    one_line,
    parse_count,
    parse_kb,
    parse_limit,
    primary_group,
    slice_of,
    snapshot,
    strip_marks,
)

__all__ = [
    "LUSTRE_FSTYPES",
    "LustreBackend",
    "parse_lfs_quota",
    "project_id",
    "unverified",
    "bracketed",
    "FIGURE_NOTE",
    "TIME_NOTE",
]


LUSTRE_FSTYPES = ("lustre",)

# More than a handful of paths turns one backend into a fan-out of its own, and
# `lfs quota` asks three scopes per path.
MAX_PATHS = 3

# What `lfs quota` prints when it could not reach every OST or MDT. The figures
# it could not verify come back in brackets and this line explains them.
_INACCURATE = 'the data in "[]" is inaccurate'

# Phrases that mean the filesystem is answering "there is no quota here", which
# is a durable answer and not a failure.
_QUOTA_OFF = ("quota is off", "quotas are not enabled", "quota is not enabled")

_PERMISSION_MARKERS = ("operation not permitted", "permission denied", "not authorized")

FIGURE_NOTE = (
    "lfs quota could not reach every device and bracketed the figures it could "
    "not verify, so these numbers are a reading of an incomplete filesystem; "
    "check lfs check servers"
)

TIME_NOTE = (
    "read live from lfs quota; Lustre accounting is updated asynchronously by "
    "the OSTs, so treat a small difference as timing"
)

# `Disk quotas for usr foo (uid 1000):` / `... for prj 1234 (pid 1234):`. The
# scope is read rather than assumed, so a build that answers a different scope
# than the one asked cannot be silently relabelled.
_SCOPE_LINE = re.compile(r"disk\s+quotas\s+for\s+(\w+)\s", re.IGNORECASE)


def unverified(text):
    # type: (str) -> bool
    """Did ``lfs`` disown any of the figures it just printed?

    Two signals, either of which is enough: the warning line it prints when a
    device is down or deactivated, and a bracketed figure in the data area.
    Both are ordinary on a site with a degraded OST, which is the same
    afternoon somebody reaches for a tool like this.
    """
    lowered = (text or "").lower()
    if _INACCURATE in lowered:
        return True
    for line in (text or "").splitlines():
        if "Filesystem" in line or "Disk quotas" in line:
            continue
        if "[" in line and "]" in line:
            return True
    return False


def parse_lfs_quota(text, scope="", mount_hint=""):
    # type: (str, str, str) -> List[QuotaRow]
    """The data line under Lustre's ``Filesystem ... kbytes`` header.

    Two things here produced confident wrong answers in rapiDU and are fixed:

    **The mount point is ``lfs``'s own first column, not the queried path.**
    Storing the queried path made "does this walk cover the whole quota'd
    tree" true by construction, so the verdict that exists to say *you walked
    a subdirectory of a much larger quota'd filesystem* became unreachable on
    Lustre and every subdirectory scan reported the rest of the filesystem as
    an unexplained difference.

    **Wrapped rows are read.** ``lfs`` puts a long filesystem name on its own
    line and the eight figures on the next. Requiring nine fields on one line
    meant those sites parsed to zero rows, reported as "could not parse", on
    precisely the paths whose names are long enough to wrap.
    """
    rows = []  # type: List[QuotaRow]
    lines = [line for line in (text or "").splitlines() if line.strip()]
    found_scope = scope
    for index, line in enumerate(lines):
        named = _SCOPE_LINE.search(line)
        if named:
            found_scope = norm_scope(named.group(1)) or scope
            continue
        if "Filesystem" not in line or "kbytes" not in line:
            continue
        rest = lines[index + 1 :]
        if not rest:
            break
        parts = rest[0].split()
        if len(parts) == 1 and len(rest) > 1:
            parts = [parts[0]] + rest[1].split()
        if len(parts) < 9:
            break
        name = parts[0]
        # `lfs` names the filesystem by its mount point. Fall back to the
        # kernel when it does not, and to nothing at all when neither can say:
        # an unmapped row is honest, a row mounted at the walk root is not.
        mount = name if name.startswith("/") else mount_hint
        label = os.path.basename((mount or "").rstrip("/")) or name
        rows.append(
            QuotaRow(
                label,
                BLOCKS,
                found_scope,
                parse_kb(parts[1]),
                parse_limit(parts[2], parse_kb),
                parse_limit(parts[3], parse_kb),
                clean_grace(parts[4]),
                mount or None,
                device=name,
            )
        )
        rows.append(
            QuotaRow(
                label,
                FILES,
                found_scope,
                parse_count(parts[5]),
                parse_limit(parts[6], parse_count),
                parse_limit(parts[7], parse_count),
                clean_grace(parts[8]),
                mount or None,
                device=name,
            )
        )
        break
    # A figure that could not be read at all drops its row rather than
    # reporting None as usage, which `QuotaRow.fraction` would render as an
    # empty bar.
    return [row for row in rows if row.used is not None]


def project_id(runner, path, budget=None, extra_dirs=()):
    # type: (object, str, object, Sequence[str]) -> Optional[str]
    """The Lustre project id ``path`` is charged to, if it carries one.

    ``lfs project -d <dir>`` prints ``<projid> <flags> <path>``. Project id 0
    means "no project", which is not a quota worth asking about.
    """
    exe = runner.available("lfs", extra_dirs=tuple(extra_dirs or ()))
    if not exe:
        return None
    result = runner.run([exe, "project", "-d", path], timeout=slice_of(budget))
    charge(budget, result)
    if result.failed:
        return None
    parts = (result.stdout or "").split()
    if parts and parts[0].isdigit() and parts[0] != "0":
        return parts[0]
    return None


class LustreBackend(Backend):
    """``lfs quota`` for user, group and project scope."""

    name = "lfs quota"

    def __init__(
        self,
        user=None,  # type: Optional[str]
        group=None,  # type: Optional[str]
        extra_dirs=(),  # type: Sequence[str]
        max_paths=MAX_PATHS,  # type: int
    ):
        # type: (...) -> None
        self._user = user
        self._group = group
        self.extra_dirs = tuple(extra_dirs or ())
        self.max_paths = max_paths

    def supported(self, runner):
        # type: (object) -> Optional[str]
        return runner.available("lfs", extra_dirs=self.extra_dirs)

    def read(self, runner, mounts, budget, paths):
        # type: (object, object, object, Sequence[str]) -> QuotaSnapshot
        exe = self.supported(runner)
        if not exe:
            return unavailable_quota(
                self.name,
                VerdictCategory.NO_QUOTA_BACKEND,
                "lfs is not installed on this node",
            )
        targets = self._targets(mounts, paths)
        if not targets:
            return unavailable_quota(
                self.name,
                VerdictCategory.NO_QUOTA_BACKEND,
                "lfs is installed but no lustre filesystem is mounted on this node",
            )

        rows = []  # type: List[QuotaRow]
        failures = []  # type: List[Tuple[str, str]]
        refused = []  # type: List[Tuple[str, str]]
        quota_off = []  # type: List[str]
        empty = []  # type: List[str]
        doubted = False
        for target in targets:
            for scope, flags in self._scopes(runner, budget, target):
                if budget is not None and getattr(budget, "exhausted", False):
                    failures.append(("", "the quota budget ran out before every scope was asked"))
                    break
                result = runner.run(
                    [exe, "quota"] + list(flags) + [target], timeout=slice_of(budget)
                )
                charge(budget, result)
                subject = "%s %s" % (scope, target)
                if result.failed:
                    message = result.diagnostic
                    if _is_off(message):
                        quota_off.append(subject)
                    elif _is_permission(message):
                        refused.append((subject, message))
                    else:
                        failures.append((subject, message))
                    continue
                if _is_off(result.stdout):
                    quota_off.append(subject)
                    continue
                found = parse_lfs_quota(
                    result.stdout,
                    scope,
                    enclosing_mount_of_type(mounts, target, LUSTRE_FSTYPES),
                )
                if not found:
                    empty.append(subject)
                    continue
                rows.extend(found)
                if unverified(result.stdout):
                    doubted = True

        read_at = now()
        return snapshot(
            self.name,
            rows,
            _empty_category(failures, refused, quota_off, empty),
            _empty_reason(failures, refused, quota_off, empty),
            taken_at=read_at if rows else None,
            read_at=read_at,
            time_note=TIME_NOTE if rows else "",
            # The number is suspect, the age is not. Two separate channels, on
            # purpose: a stale figure needs waiting and a disowned one needs
            # the filesystem fixed.
            figure_note=FIGURE_NOTE if doubted else "",
        )

    def _targets(self, mounts, paths):
        # type: (object, Sequence[str]) -> List[str]
        """Which paths to ask about, each one on a lustre filesystem.

        A path that is not on Lustre is not asked about at all rather than
        being sent to ``lfs`` anyway: the answer would be an error message,
        and an error message from a question that should not have been asked
        reads as a broken backend.
        """
        out = []  # type: List[str]
        for path in list(paths or [])[: self.max_paths]:
            if enclosing_mount_of_type(mounts, path, LUSTRE_FSTYPES):
                out.append(path)
        if out:
            return out
        if mounts is None:
            return []
        # No asked path is on Lustre, so ask about the filesystem itself. The
        # rows still map by mount point afterwards, so this cannot attribute
        # them to the caller's path.
        return [m.mountpoint for m in mounts.mounts_of_type(LUSTRE_FSTYPES)][: self.max_paths]

    def _scopes(self, runner, budget, path):
        # type: (object, object, str) -> List[Tuple[str, List[str]]]
        scopes = [("user", ["-u", self._user or current_user()])]
        group = self._group or primary_group()
        if group:
            scopes.append(("group", ["-g", group]))
        projid = project_id(runner, path, budget=budget, extra_dirs=self.extra_dirs)
        if projid:
            scopes.append(("project", ["-p", projid]))
        return scopes


def _is_off(text):
    # type: (str) -> bool
    lowered = (text or "").lower()
    return any(marker in lowered for marker in _QUOTA_OFF)


def _is_permission(text):
    # type: (str) -> bool
    lowered = (text or "").lower()
    return any(marker in lowered for marker in _PERMISSION_MARKERS)


def _empty_category(failures, refused, quota_off, empty):
    # type: (Sequence[object], Sequence[object], Sequence[str], Sequence[str]) -> str
    if failures:
        return VerdictCategory.BACKEND_FAILED
    if refused:
        return VerdictCategory.PERMISSION_TO_ASK_DENIED
    if quota_off or empty:
        return VerdictCategory.NO_QUOTA_ENFORCED
    return VerdictCategory.BACKEND_FAILED


def _empty_reason(failures, refused, quota_off, empty):
    # type: (Sequence[Tuple[str, str]], Sequence[Tuple[str, str]], Sequence[str], Sequence[str]) -> str
    parts = []  # type: List[str]
    if failures or refused:
        parts.append(grouped_failures(list(failures) + list(refused)))
    if quota_off:
        parts.append("lustre reports quota disabled for %s" % (", ".join(quota_off),))
    if empty and not failures and not refused:
        parts.append("lfs quota answered for %s with no figures" % (", ".join(empty),))
    return one_line("; ".join(p for p in parts if p)) or "lfs quota returned no rows"


def bracketed(token):
    # type: (str) -> bool
    """Whether this one figure is one ``lfs`` could not verify.

    Per-figure rather than per-snapshot, for a caller that wants to mark the
    individual number. The stripping itself is `strip_marks`, which removes
    ``[``, ``]`` and ``*``
    from both ends in one pass so ``[N*]`` and ``[N]*`` both parse. Handling
    them in sequence left the second spelling as ``N]*``, which int rejects
    and which dropped the row: the exact failure the stripping exists to stop,
    reappearing on the worse of the two inputs.
    """
    raw = (token or "").strip()
    return "[" in raw and "]" in raw and strip_marks(raw).isdigit()
