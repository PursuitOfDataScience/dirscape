"""Site quota wrappers: the most valuable backend and the least portable.

A site wrapper is the only backend that can see storage which is **not mounted
on the node you are standing on**. Measured on 2026-09-21, a login-style node
whose ``quota`` reports three filesystems that do not exist there::

    >>> Low-Cost Filesystem: cfs3 (Ceph mounted at /cfs3)
    kestrel-lab      blocks (group)      155.12T    165.00T    165.00T     none
    hpc-staff        blocks (group)       58.86M     10.00G     10.00G     none

    $ test -e /cfs3 && echo yes || echo MISSING
    MISSING

Those rows are kept, with a note saying the mount was not found locally. They
are not dropped and no path is invented for them, because the discovery layer
turns them into "allocated, not mounted here", which is the single most useful
line this tool prints.

**Finding the wrapper is half the problem.** On one cluster here the working
``quota`` is a **bash alias** to ``/project2/hpc/admin/bin/quota.py`` while the
binary on PATH exits 127. A subprocess cannot see a shell alias, so the script
path list is the only way to reach it, and a backend that gives up after the
PATH lookup reports "no quota command" on a cluster where ``quota`` works
perfectly for the user typing it.

**One regex will not do.** Two forms of the same wrapper, measured::

    >>> Capacity Filesystem: project (Meadow3 GPFS mounted at /project)
    Capacity Filesystem: project2 (GPFS)

Its source settles why (``/opt/site/bin/admtool/sitequota.py``)::

    output += f'\\n>>> Capacity Filesystem: project ({filesystem_names[filesystem]
               if filesystem in filesystem_names else filesystem})'

The parenthetical is the real identity: either a published mount point, or a
bare device name, or just the word ``GPFS``. The name after the colon is a
display label, and it is not unique: two sections in one run here are both
called ``project``, one mounted at ``/collie3``. So the parenthetical is read
first and a published mount wins outright, and where nothing can be
established the row keeps a note instead of a guess. rapiDU's RD-3 was a
``_guess_mount`` fallback that confidently attributed a ``/scratch`` walk to
the wrong cluster's filesystem, and this module has no equivalent: it never
consults ``expanduser``, never consults the hostname, and never accepts a
directory that merely exists.

**The figures are a cron snapshot.** The wrapper prints
``Quota information updated at : <local timestamp>``, which its source builds
from ``os.path.getmtime`` of a file a half-hourly job writes. That is the
``time_note`` channel, with the wrapper's own warning text as the evidence.
"""

import re
import time
from typing import Dict, List, Optional, Sequence, Tuple

from ..model import QuotaRow, QuotaSnapshot, VerdictCategory, unavailable_quota
from .base import (
    BLOCKS,
    FILES,
    Backend,
    charge,
    clean_grace,
    dedupe_mounts,
    grouped_failures,
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

__all__ = [
    "KNOWN_WRAPPER_PATHS",
    "DECIMAL_SUFFIX_MOUNTS",
    "SiteWrapperBackend",
    "parse_wrapper_table",
    "parse_updated_at",
    "section_mounts",
    "TIME_NOTE",
]


# Absolute paths as well as the bare name, because a site wrapper is often not
# a binary on PATH at all: where the working `quota` is a shell alias, the
# binary on PATH can exit 127 while a script elsewhere is the one that works.
# A site names its own in `[quota] wrapper_paths`; this is the generic place.
KNOWN_WRAPPER_PATHS = ("/usr/local/bin/quota",)

# Why a section's figures are in doubt when the producer formatted them with
# 1000-based steps. Measured at one site: its wrapper formats every section
# with a 1024-based helper except one, and the input to both is a count of
# 1024-byte KB blocks, so a `7.52T` there is 7.52 * 1000**3 KB, and reading the
# suffix as binary overstates it by 7.4% (1024**3 / 1000**3 = 1.0737), which on
# that row is 553 GB.
DECIMAL_SUFFIX_NOTE = (
    "the wrapper formats this section with 1000-based steps while its "
    "input is 1024-byte blocks, so reading the suffix as binary would "
    "overstate a T figure by 7.4%"
)

# Sections whose figures the producer formatted with 1000-based steps, keyed by
# the mount point it publishes. Data rather than logic, and empty unless a site
# lists its mounts in `[quota] decimal_suffix_mounts`, because there is no
# signal in the output itself to detect it.
DECIMAL_SUFFIX_MOUNTS = {}  # type: Dict[str, str]

TIME_NOTE = (
    "this is a cached figure: the wrapper prints a report refreshed by a "
    "half-hourly job, so a write made since then does not appear in it yet"
)

# "Quota information updated at :  2026-09-21 09:02:28". Local time, which is
# not an assumption: the wrapper's source renders it with
# `str(datetime.datetime.fromtimestamp(os.path.getmtime(...)))`.
_UPDATED_RE = re.compile(
    r"updated\s+at\s*:?\s*([0-9]{4}-[0-9]{2}-[0-9]{2}[ T][0-9]{2}:[0-9]{2}:[0-9]{2})",
    re.IGNORECASE,
)

# The published mount inside a section's parenthetical. Tolerates the trailing
# space the wrapper emits after the closing bracket on three of its sections.
_MOUNT_RE = re.compile(r"mounted\s+at\s+(/\S+?)\)?\s*$", re.IGNORECASE)

# The parenthetical itself, which carries the real identity.
_PAREN_RE = re.compile(r"\(([^)]*)\)")

# The display name after the colon: `Capacity Filesystem: project2 (GPFS)`.
# The adjective before "Filesystem" varies ("Capacity", "Low-Cost",
# "Grant-Funded"), so the pattern anchors on the word and not on the phrase.
_FS_NAME_RE = re.compile(r"filesystem\s*:\s*([^\s(]+)", re.IGNORECASE)

# How close an age must sit to the UTC offset before we call it suspicious. A
# real snapshot age landing within three minutes of the offset, to the minute,
# is a coincidence worth one line of doubt.
_TZ_SUSPICION_S = 180.0


def parse_updated_at(text):
    # type: (str) -> Optional[float]
    """The timestamp the wrapper published, as a unix time, or None.

    Read as LOCAL time, which is correct for this wrapper and is stated rather
    than assumed. A backend publishing UTC would displace the age by exactly
    the UTC offset, and `_timezone_suspicion` says so instead of silently
    correcting it, because a bare timestamp carries no zone and a correction
    would be a guess dressed as a measurement.
    """
    found = _UPDATED_RE.search(text or "")
    if not found:
        return None
    stamp = found.group(1).replace("T", " ")
    try:
        return time.mktime(time.strptime(stamp, "%Y-%m-%d %H:%M:%S"))
    except (ValueError, OverflowError):
        return None


def _utc_offset(when):
    # type: (float) -> int
    """Seconds local time is ahead of UTC at ``when``. Negative west of UTC."""
    return -(time.altzone if time.localtime(when).tm_isdst else time.timezone)


def _timezone_suspicion(age, read_at):
    # type: (Optional[float], float) -> str
    """Does this age look like a timezone mis-parse rather than a real age?

    East of UTC a UTC-publishing backend reads as hours stale, nothing errors,
    and the feature quietly stops working; west of UTC the age goes negative.
    Neither can be proved from a bare timestamp, so what can be said is that
    an age sitting on the UTC offset to the minute is more likely a zone
    mismatch than a coincidence. We say that and leave the number alone.
    """
    if age is None:
        return ""
    offset = _utc_offset(read_at)
    if not offset or abs(age - offset) > _TZ_SUSPICION_S:
        return ""
    return (
        "this age is within a few minutes of the %+.1fh UTC offset, so the "
        "backend may be publishing UTC where a local timestamp is assumed; "
        "treat the age, not the reading, as unproven" % (offset / 3600.0,)
    )


def _age_note(taken_at, read_at):
    # type: (Optional[float], float) -> str
    """The time channel for a cached report: how old, and how sure of that.

    A wrapper that published no timestamp gets "unknown", never "now". The
    whole reason this backend carries a separate time channel is that a figure
    from a half-hourly job can be perfectly correct and half an hour out of
    date, and the two are different problems for the reader.
    """
    if taken_at is None:
        return one_line(
            TIME_NOTE + ". This wrapper published no timestamp, so the age of these "
            "figures is unknown and must not be reported as fresh"
        )
    return one_line(TIME_NOTE + ". " + _timezone_suspicion(max(0.0, read_at - taken_at), read_at))


def section_mounts(header, mounts):
    # type: (str, object) -> Tuple[List[str], bool, str]
    """The mounts a section header names, whether inferred, and any doubt.

    Strongest evidence first, and each step is named in the note it produces
    so a reader can tell a published mapping from an inferred one:

    1. ``mounted at /path`` in the parenthetical: published. Wins outright,
       because the display name is not unique and the mount is.
    2. the parenthetical as a device name in the mount table: the kernel's own
       answer about a device that exists, but the association is inferred.
    3. the display name as a device name in the mount table.
    4. the display name spelled as ``/name``, accepted only if the kernel
       calls it a mount point. This is the form measured on a cluster whose
       wrapper prints ``(GPFS)`` and no mount clause at all.
    5. nothing, and the caller keeps a note rather than a guess.
    """
    published = _MOUNT_RE.search(header)
    if published:
        return [published.group(1)], False, ""

    paren = _PAREN_RE.findall(header)
    label = (paren[-1].strip() if paren else "").strip()
    if label:
        by_device = mounts_for_device(mounts, label)
        if by_device:
            return by_device, True, "mount resolved from the device name %s" % (label,)

    named = _FS_NAME_RE.search(header)
    name = named.group(1).strip().rstrip(":") if named else ""
    if name:
        by_device = mounts_for_device(mounts, name)
        if by_device:
            return by_device, True, "mount resolved from the filesystem name %s" % (name,)
        spelled = "/" + name.strip("/")
        if is_mount_point(mounts, spelled):
            return (
                [spelled],
                True,
                "mount inferred from the filesystem name %s, which the kernel "
                "does call a mount point" % (name,),
            )

    return (
        [],
        False,
        "this section published no mount point (%s) and nothing in the mount "
        "table matches it, so these figures have no local path"
        % (one_line(header.lstrip(">").strip()) or "no header",),
    )


def parse_wrapper_table(text, mounts, decimal_mounts=None):
    # type: (str, object, Optional[Dict[str, str]]) -> Tuple[List[QuotaRow], Optional[float], List[str], str]
    """Rows, the published timestamp, notes, and any figure doubt.

    Tolerant by construction. Anything that is not a recognised row is skipped
    rather than treated as an error, because this format differs per cluster
    and per version of one site's own script, and a parser that fails on an
    unknown line reports "no quota" on a cluster where the command works.
    """
    decimal_mounts = DECIMAL_SUFFIX_MOUNTS if decimal_mounts is None else decimal_mounts
    rows = []  # type: List[QuotaRow]
    notes = []  # type: List[str]
    figure_notes = []  # type: List[str]
    current = []  # type: List[str]
    inferred = False
    section_note = ""
    over_quota = False

    for raw in (text or "").splitlines():
        line = raw.strip()
        if not line or line.startswith("---") or line.startswith("==="):
            continue
        if line.startswith(">>>"):
            current, inferred, section_note = section_mounts(line, mounts)
            if section_note and section_note not in notes:
                notes.append(section_note)
            continue
        if line.startswith(">") or line.endswith("<"):
            # The over-quota banner the wrapper appends, whose lines are
            # wrapped in `>` and `<`. Not a row, but it IS a signal: the
            # wrapper only prints it when some limit is exceeded.
            if "quota limits exceeded" in line.lower():
                over_quota = True
            continue
        lowered = line.lower()
        if lowered.startswith(("fileset", "quota information", "filesystem")):
            continue
        row = _wrapper_row(
            line, current, inferred, section_note, decimal_mounts, figure_notes, mounts
        )
        if row is not None:
            rows.append(row)

    _note_unmounted(rows, mounts, notes)
    _drop_ambiguous(rows, notes)
    if over_quota:
        notes.append("the wrapper reports at least one limit exceeded")
    return rows, parse_updated_at(text), notes, one_line("; ".join(dedupe_mounts(figure_notes)))


def _wrapper_row(
    line,  # type: str
    mounts_for_row,  # type: Sequence[str]
    inferred,  # type: bool
    section_note,  # type: str
    decimal_mounts,  # type: Dict[str, str]
    figure_notes,  # type: List[str]
    mount_table,  # type: object
):
    # type: (...) -> Optional[QuotaRow]
    parts = line.split()
    if len(parts) < 5:
        return None
    fileset = parts[0]
    kind = parts[1].lower()
    if kind not in (BLOCKS, FILES):
        return None
    index = 2
    scope = ""
    if parts[2].startswith("(") and parts[2].endswith(")"):
        scope = norm_scope(parts[2].strip("()"))
        index = 3
    figures = parts[index:]
    if len(figures) < 3:
        return None

    base = 1024
    doubt = ""
    for mount in mounts_for_row:
        if mount in decimal_mounts:
            base = 1000
            doubt = decimal_mounts[mount]
    if doubt and doubt not in figure_notes:
        figure_notes.append(doubt)

    used = parse_count(figures[0]) if kind == FILES else parse_size(figures[0], base=base)
    if used is None:
        return None

    def limit(token):
        # type: (str) -> Optional[int]
        if kind == FILES:
            return parse_limit(token, parse_count)
        # `unlimited` is the wrapper's own word for a zero limit: its source
        # substitutes it before printing. Mapped back to 0, which means "no
        # limit was set", and never to None, which would mean "unreadable".
        return parse_limit(token, lambda t: parse_size(t, base=base))

    points = list(mounts_for_row)
    note = section_note
    guessed = inferred
    if not points:
        # No section header, or one that named nothing this kernel knows. The
        # fileset label is all that is left, and it is accepted only when it
        # spells a path the kernel itself calls a mount point. Measured on the
        # unheadered leading section here: `scratch/meadow3` resolves and
        # `Meadow2-home` does not, which is the correct pair of answers on a
        # node where /home belongs to a different cluster's filesystem.
        # rapiDU mapped names like that through `expanduser` and a hostname
        # tie-break, and reconciled one cluster's walk against another
        # cluster's quota (RD-3).
        points = _mounts_from_name(fileset, mount_table)
        if points:
            guessed = True
            note = one_line(
                "mount inferred from the fileset label %s, which the kernel "
                "does call a mount point" % (fileset,)
            )
        else:
            note = note or one_line(
                "the wrapper published no mount for %s and its label does not "
                "spell one, so these figures have no local path" % (fileset,)
            )

    row = QuotaRow(
        fileset,
        kind,
        scope,
        used,
        limit(figures[1]),
        limit(figures[2]),
        clean_grace(figures[3] if len(figures) > 3 else ""),
        points[0] if points else None,
        guessed=guessed,
        note=note,
    )
    row.mounts = points
    return row


def _mounts_from_name(fileset, mounts):
    # type: (str, object) -> List[str]
    """A mount point the fileset label spells, or nothing.

    Two spellings, both required to be real mount points: the label as a path
    (``scratch/meadow3``) and the label with ``-`` read as ``/``
    (``project-hpc``). A directory that merely exists is never accepted: that
    distinction is the whole safety of this function, and bending it is how
    ``/scratch``, the parent directory holding three clusters' scratch
    filesystems, came to be returned for a fileset living on one of them.
    """
    name = (fileset or "").strip().strip("/")
    if not name:
        return []
    for candidate in (
        "/" + name,
        "/" + name.lower(),
        "/" + name.replace("-", "/"),
        "/" + name.lower().replace("-", "/"),
    ):
        if is_mount_point(mounts, candidate):
            return [candidate]
    return []


def _note_unmounted(rows, mounts, notes):
    # type: (Sequence[QuotaRow], object, List[str]) -> None
    """Mark rows whose published mount does not exist on this node.

    Kept rather than dropped, and no path is invented for them. Measured: the
    wrapper reports ``/cfs3``, ``/cfs4`` and ``/shared`` from a node where
    none of the three exists, which is real usage against a real allocation
    that this node simply cannot reach.
    """
    for row in rows:
        if not row.mount or row.guessed:
            continue
        if is_mount_point(mounts, row.mount):
            continue
        row.note = one_line(
            "%s is allocated to you here, but %s is not mounted on this node"
            % (row.fileset, row.mount)
        )
        if row.note not in notes:
            notes.append(row.note)


def _drop_ambiguous(rows, notes):
    # type: (Sequence[QuotaRow], List[str]) -> None
    """Unmap an inferred mount that more than one fileset claims.

    rapiDU broke this tie with the hostname, which makes the output depend on
    which node ran the tool: the same recorded input produced one mapping on a
    node mounting one cluster and a different one on a login node that sees
    three. That is RD-10 in a milder form, so there is no tie-break here at
    all. An ambiguous mount is dropped and the reason is stated, because an
    unmapped row is honest and a confidently wrong one is not.
    """
    claims = {}  # type: Dict[str, List[str]]
    for row in rows:
        if row.mount and row.guessed:
            names = claims.setdefault(row.mount, [])
            if row.fileset not in names:
                names.append(row.fileset)
    for mount, filesets in sorted(claims.items()):
        if len(filesets) < 2:
            continue
        note = one_line(
            "%d filesets (%s) all resolve to %s and could not be told apart"
            % (len(filesets), ", ".join(sorted(filesets)), mount)
        )
        if note not in notes:
            notes.append(note)
        for row in rows:
            if row.guessed and row.mount == mount:
                row.mount = None
                row.mounts = []
                row.note = note


class SiteWrapperBackend(Backend):
    """A site's own ``quota`` script, found on PATH or by absolute path."""

    name = "site quota wrapper"

    def __init__(
        self,
        name_on_path="quota",  # type: str
        script_paths=KNOWN_WRAPPER_PATHS,  # type: Sequence[str]
        args=(),  # type: Sequence[str]
        extra_dirs=(),  # type: Sequence[str]
        decimal_mounts=None,  # type: Optional[Dict[str, str]]
    ):
        # type: (...) -> None
        self.name_on_path = name_on_path
        self.script_paths = tuple(script_paths or ())
        # No arguments by default. `-s` means "human readable" to stock quota
        # and "silent" to the site wrapper measured here, whose source branches
        # on `getarg('s', 'silent')`, so a flag that is helpful at one site can
        # suppress the whole report at another. The stock flags belong to the
        # stock backend.
        self.args = tuple(args or ())
        self.extra_dirs = tuple(extra_dirs or ())
        self.decimal_mounts = decimal_mounts

    def candidates(self, runner):
        # type: (object) -> List[str]
        """Every wrapper worth trying, in order, deduplicated.

        The name on PATH first, then the configured scripts. Both, not either:
        the PATH binary existing is not evidence that it works, which is the
        measured case this order exists for.
        """
        found = []  # type: List[str]
        on_path = runner.available(self.name_on_path, extra_dirs=self.extra_dirs)
        if on_path:
            found.append(on_path)
        for path in self.script_paths:
            resolved = runner.available(path)
            if resolved and resolved not in found:
                found.append(resolved)
        return found

    def supported(self, runner):
        # type: (object) -> Optional[str]
        candidates = self.candidates(runner)
        return candidates[0] if candidates else None

    def read(self, runner, mounts, budget, paths):
        # type: (object, object, object, Sequence[str]) -> QuotaSnapshot
        candidates = self.candidates(runner)
        if not candidates:
            return unavailable_quota(
                self.name,
                VerdictCategory.NO_QUOTA_BACKEND,
                "no site quota wrapper was found: %s is not on PATH and none of "
                "%s exists" % (self.name_on_path, ", ".join(self.script_paths) or "(none)"),
            )

        failures = []  # type: List[Tuple[str, str]]
        taken_at = None  # type: Optional[float]
        for candidate in candidates:
            if budget is not None and getattr(budget, "exhausted", False):
                failures.append(("", "the quota budget ran out before every wrapper was tried"))
                break
            result = runner.run([candidate] + list(self.args), timeout=slice_of(budget))
            charge(budget, result)
            # The timestamp survives a failure to use the rest: a report we
            # could not parse still says when it was taken, and that is a fact
            # the renderer can use. This is why `unavailable_quota` carries the
            # timing channels.
            taken_at = parse_updated_at(result.stdout) or taken_at
            if result.failed:
                failures.append((candidate, self._explain(runner, candidate, result)))
                continue
            rows, published, notes, figure_note = parse_wrapper_table(
                result.stdout, mounts, self.decimal_mounts
            )
            taken_at = published or taken_at
            if not rows:
                failures.append(
                    (candidate, one_line("; ".join(notes)) or "printed nothing this parser read")
                )
                continue
            read_at = now()
            return snapshot(
                self.name,
                rows,
                VerdictCategory.BACKEND_FAILED,
                grouped_failures(failures) or "the wrapper printed no rows",
                reason=one_line("; ".join(notes)),
                taken_at=taken_at,
                read_at=read_at,
                time_note=_age_note(taken_at, read_at),
                figure_note=figure_note,
            )

        read_at = now()
        return unavailable_quota(
            self.name,
            VerdictCategory.BACKEND_FAILED,
            grouped_failures(failures) or "no site quota wrapper produced any rows",
            taken_at=taken_at,
            read_at=read_at,
            time_note=TIME_NOTE if taken_at is not None else "",
        )

    def _explain(self, runner, candidate, result):
        # type: (object, str, object) -> str
        """Why this candidate failed, keeping what it said about itself.

        Exit 127 has two causes needing opposite answers, and rapiDU's RD-2
        reported the wrong one: a shell wrapper whose inner command is missing
        also exits 127, so ``quota`` was on PATH and running exactly as
        installed while the tool said "not on PATH" and discarded the one line
        that named the real cause::

            /opt/site/bin/quota: line 3: /srv/adm/quotarept: No such file or directory

        The diagnostic is carried either way: a backend's own account of its
        own failure is the most useful thing this module can pass on.
        """
        diagnostic = getattr(result, "diagnostic", "") or "failed with no message"
        if getattr(result, "returncode", None) == 127 or getattr(result, "not_found", False):
            return "%s exists but exited 127: %s" % (candidate, diagnostic)
        return diagnostic
