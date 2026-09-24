"""Stock ``quota -s``, and the two ways it says "you have no quota".

The least interesting backend and the one most likely to be all a site has. It
is last in the default order because it knows nothing of parallel filesystems:
on the cluster this was written on, ``/usr/bin/quota -s`` exits 1 and prints
nothing at all while GPFS is holding the answer one backend up.

Three failures this parser exists to avoid, all of which threw away the row
that mattered:

**Every table, not just the first.** rapiDU broke out of the loop after one
``Filesystem`` header, so the ``Disk quotas for group ...`` section was
silently dropped. A group quota is routinely the binding limit on a shared
project directory, which is exactly where an HPC user runs out of space.

**The scope is read, not assumed.** Every row used to be labelled ``user``, so
a group figure would have claimed to be a personal one.

**A row with one grace timer is read.** Graces print only for an exceeded
limit, so 6 figures means nothing is over, 8 means both are, and **7 means
exactly one is**, which is the commonest over-quota shape there is. Counting
fields cannot tell which of the two is present; the position of the
non-numeric token can.

Exit 127 gets its own care. It has two causes needing opposite answers, and
reporting the wrong one is rapiDU's RD-2: a shell wrapper whose inner command
is missing also exits 127, so ``quota`` was on PATH and running exactly as
installed while the tool said "not on PATH" and discarded the one line naming
the real cause. `Completed.diagnostic` is kept either way.
"""

import re
from typing import List, Optional, Sequence, Tuple

from ..model import QuotaRow, QuotaSnapshot, VerdictCategory, unavailable_quota
from .base import (
    BLOCKS,
    FILES,
    Backend,
    charge,
    clean_grace,
    is_figure,
    is_mount_point,
    mounts_for_device,
    norm_scope,
    now,
    one_line,
    parse_count,
    parse_limit,
    parse_size,
    slice_of,
    snapshot,
)

# One-way dependency: the tabular format belongs to the site wrapper, and this
# module borrows the parser rather than keeping a second copy of it. Two homes
# for one rule is how they drift apart, which this package says elsewhere and
# is worth obeying here.
from .wrapper import parse_wrapper_table

__all__ = [
    "PosixQuotaBackend",
    "parse_stock_quota",
    "NO_QUOTA_RE",
    "explain_exit",
]


# `Disk quotas for user someone (uid 1000):` - whose figures the table below is.
_SCOPE_RE = re.compile(r"disk\s+quotas\s+for\s+(user|group|project)\b", re.IGNORECASE)

# The two ways quota-tools says "this account has no quota anywhere", both
# observed rather than guessed: `none` on the clusters here, and `no limited
# resources used` on a Lustre login node running quota 4.x. It is an ANSWER,
# and reporting it as "could not parse quota output" blames a command that
# worked for a failure that did not happen. For a tool whose question is "why
# is my quota full", "you have no quota" is a useful reply.
NO_QUOTA_RE = re.compile(
    r"disk\s+quotas\s+for\s+\w+\s+[^\n:]*:\s*(?:none|no\s+limited\s+resources\s+used)\s*$",
    re.IGNORECASE | re.MULTILINE,
)


def parse_stock_quota(text, mounts):
    # type: (str, object) -> List[QuotaRow]
    """Rows from the stock layout: a ``Filesystem`` header then one row per fs.

    The block column is headed ``blocks`` by plain ``quota`` and ``space`` by
    ``quota -s``, and **the header also states the unit**: under ``blocks`` the
    figures are raw 1 KiB blocks, under ``space`` they are human-readable with
    a suffix. Reading a ``blocks`` figure as bytes under-reports by 1024x, so a
    30 GiB home quota would print as 30 MiB. The header decides the scale and
    the magnitude never does.
    """
    rows = []  # type: List[QuotaRow]
    lines = (text or "").splitlines()
    scope = "user"
    for index, line in enumerate(lines):
        found = _SCOPE_RE.search(line)
        if found:
            scope = norm_scope(found.group(1))
            continue
        if "Filesystem" not in line:
            continue
        if "blocks" not in line and "space" not in line:
            continue
        kib_units = "space" not in line
        rows.extend(_stock_table(lines[index + 1 :], scope, kib_units, mounts))
    return rows


def _stock_table(lines, scope, kib_units, mounts):
    # type: (Sequence[str], str, bool, object) -> List[QuotaRow]
    rows = []  # type: List[QuotaRow]
    pending = None  # type: Optional[str]

    def block_bytes(token):
        # type: (str) -> Optional[int]
        value = parse_size(token)
        if value is None:
            return None
        return value * 1024 if kib_units else value

    def block_limit(token):
        # type: (str) -> Optional[int]
        return parse_limit(token, block_bytes)

    for raw in lines:
        parts = raw.split()
        if not parts:
            break
        # `quota` wraps a long device name onto its own line and indents the
        # figures onto the next. Requiring name-and-numbers on one line lost
        # every row at sites whose device names are long, which is most NFS.
        if len(parts) == 1 and not is_figure(parts[0]):
            pending = parts[0]
            continue
        if pending is not None:
            device, figures = pending, parts
            pending = None
        else:
            device, figures = parts[0], parts[1:]

        # Six figures plus a grace timer for each limit that is over. The end
        # of the table is anything else, including the next section's `Disk
        # quotas for group ...`, whose tokens are not figures. The test this
        # replaces was "the first column starts with /", which rejected the
        # whole table at any site whose device is `server:/export` or
        # `//host/share`, that is every NFS and CIFS mount, before a single
        # number was read.
        graces = [n for n, token in enumerate(figures) if not is_figure(token)]
        if len(figures) == 6 and not graces:
            block_at, files_at, block_grace, files_grace = 0, 3, "", ""
        elif len(figures) == 7 and graces == [3]:
            block_at, files_at, block_grace, files_grace = 0, 4, figures[3], ""
        elif len(figures) == 7 and graces == [6]:
            block_at, files_at, block_grace, files_grace = 0, 3, "", figures[6]
        elif len(figures) >= 8 and graces[:2] == [3, 7]:
            block_at, files_at, block_grace, files_grace = 0, 4, figures[3], figures[7]
        else:
            break

        used = block_bytes(figures[block_at])
        if used is None:
            break
        points, guessed, note = _attribute(mounts, device)
        rows.append(
            QuotaRow(
                device,
                BLOCKS,
                scope,
                used,
                block_limit(figures[block_at + 1]),
                block_limit(figures[block_at + 2]),
                clean_grace(block_grace),
                points[0] if points else None,
                device=device,
                guessed=guessed,
                note=note,
            )
        )
        rows[-1].mounts = list(points)
        files_used = parse_count(figures[files_at])
        if files_used is not None:
            rows.append(
                QuotaRow(
                    device,
                    FILES,
                    scope,
                    files_used,
                    parse_limit(figures[files_at + 1], parse_count),
                    parse_limit(figures[files_at + 2], parse_count),
                    clean_grace(files_grace),
                    points[0] if points else None,
                    device=device,
                    guessed=guessed,
                    note=note,
                )
            )
            rows[-1].mounts = list(points)
    return rows


def _attribute(mounts, device):
    # type: (object, str) -> Tuple[List[str], bool, str]
    """Where a stock row's device is mounted.

    The first column of stock ``quota`` is a **device**, never a directory, so
    testing it with ``isdir`` mapped nothing and dropped every correctly
    parsed row for want of a mount. ``/proc/self/mounts`` is keyed by exactly
    that string and already knows the answer.
    """
    points = mounts_for_device(mounts, device)
    if points:
        return points, False, ""
    # Some builds print the mount point in that column instead of the device.
    # Accepted only when the kernel calls it a mount point.
    if device.startswith("/") and is_mount_point(mounts, device):
        return [device], False, ""
    return (
        [],
        False,
        "%s is not in this node's mount table, so this row has no local path" % (device,),
    )


def explain_exit(runner, command, result, extra_dirs=()):
    # type: (object, str, object, Sequence[str]) -> str
    """Why a command failed, distinguishing absent from present-and-broken.

    ``not_found`` really is "no such command". An exit of 127 from a command
    that exists is a wrapper whose inner command is missing, and then the
    remedy is entirely different. The stderr is carried either way, because a
    backend's own account of its own failure is the most useful thing this
    module can pass on, and it is the only signal a user gets when the working
    ``quota`` at their site is a shell alias a subprocess cannot see.
    """
    diagnostic = getattr(result, "diagnostic", "") or "failed with no message"
    if getattr(result, "not_found", False):
        return "%s is not on PATH" % (command,)
    if getattr(result, "returncode", None) == 127:
        if runner.available(command, extra_dirs=tuple(extra_dirs or ())):
            return "%s is on PATH but exited 127: %s" % (command, diagnostic)
        return "%s is not on PATH: %s" % (command, diagnostic)
    return diagnostic


class PosixQuotaBackend(Backend):
    """``quota -s``, in the stock layout and in a site's tabular one."""

    name = "quota -s"
    stock = True

    def __init__(
        self,
        command="quota",  # type: str
        args=("-s",),  # type: Sequence[str]
        extra_dirs=(),  # type: Sequence[str]
    ):
        # type: (...) -> None
        self.command = command
        # `-s` asks for human-readable figures, and the parser reads the unit
        # off the header either way, so this is a preference and not a
        # dependency.
        self.args = tuple(args or ())
        self.extra_dirs = tuple(extra_dirs or ())

    def supported(self, runner):
        # type: (object) -> Optional[str]
        return runner.available(self.command, extra_dirs=self.extra_dirs)

    def read(self, runner, mounts, budget, paths):
        # type: (object, object, object, Sequence[str]) -> QuotaSnapshot
        exe = self.supported(runner)
        if not exe:
            return unavailable_quota(
                self.name,
                VerdictCategory.NO_QUOTA_BACKEND,
                "%s is not installed on this node" % (self.command,),
            )
        result = runner.run([exe] + list(self.args), timeout=slice_of(budget))
        charge(budget, result)
        if result.failed:
            return unavailable_quota(
                self.name,
                VerdictCategory.PROBE_TIMEOUT
                if getattr(result, "timed_out", False)
                else VerdictCategory.BACKEND_FAILED,
                explain_exit(runner, self.command, result, self.extra_dirs),
            )

        text = result.stdout or ""
        rows = parse_stock_quota(text, mounts)
        notes = []  # type: List[str]
        if not rows:
            # A site wrapper on the name `quota` prints a table this parser
            # does not recognise, so the other format is tried before
            # concluding anything. One parser per format, borrowed rather than
            # duplicated.
            rows, _taken, notes, _figure = parse_wrapper_table(text, mounts)
        read_at = now()
        return snapshot(
            self.name,
            rows,
            _empty_category(text),
            _empty_reason(text, result),
            reason=one_line("; ".join(notes)),
            taken_at=read_at if rows else None,
            read_at=read_at,
            time_note="read live from quota(1)" if rows else "",
        )


def _empty_category(text):
    # type: (str) -> str
    if NO_QUOTA_RE.search(text or ""):
        return VerdictCategory.NO_QUOTA_ENFORCED
    return VerdictCategory.BACKEND_FAILED


def _empty_reason(text, result):
    # type: (str, object) -> str
    if NO_QUOTA_RE.search(text or ""):
        return "quota reports no limit set for this account on any filesystem it knows about"
    if not (text or "").strip():
        # Measured: `/usr/bin/quota -s` here exits 1 and prints nothing at
        # all, which is not the same as "you have no quota" and must not be
        # rendered as it.
        return one_line(
            "quota produced no output: %s" % (getattr(result, "diagnostic", "") or "no message",)
        )
    return one_line("could not parse the output of quota: %s" % (_first_row(text),))


def _first_row(text):
    # type: (str) -> str
    """One representative line, so a parse failure can be diagnosed at all.

    Without it the reason says only "could not parse", which tells a
    maintainer nothing about which format arrived. Truncated by `sanitize` at
    construction, so an enormous line cannot blow up the field.
    """
    for line in (text or "").splitlines():
        stripped = line.strip()
        if stripped and not stripped.lower().startswith("disk quotas"):
            return stripped
    return "no non-empty line"
