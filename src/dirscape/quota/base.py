"""The backend interface, the selection rule, and the figure parsers.

Three things live here because every backend needs them and none of them may
have two implementations.

**1. Selection is not first-success-wins.** A very common HPC layout is an NFS
or local ``$HOME`` beside a GPFS or Lustre scratch. There the site ``quota``
command reports the home and knows nothing of the parallel filesystem, but it
produced *rows*, so a first-success rule returns it and never runs the backend
that could have answered. A walk of the scratch path then says "no quota row
maps to this path" while the next backend down was holding the answer. So a
backend wins by mapping the path the caller actually asked about, and a backend
that merely produced rows is kept as the fallback. Copied from rapiDU's
`read_best` (quota.py:1974), which exists because answering about a different
path is not an answer to the question asked.

**2. Absence has five states and none of them is an empty reading.** Every
backend returns through `snapshot`, which routes a rows-less result to
`unavailable_quota` with a category, and `check_snapshot` raises on the one
combination that would lie: ``available=True`` with no rows. The input that
forces this is measured, on this cluster, 2026-09-21::

    $ /usr/lpp/mmfs/bin/mmlsquota -Y collie3_cap ; echo rc=$?
    mmlsquota:user:HEADER:version:reserved:...:filesetname:
    rc=0

One line, 220 bytes, nothing on stderr, exit 0. `Completed.failed` is False and
there are no records, so nothing about the *failure* of the command can tell
you this is not a reading of zero usage. Success is therefore asserted
positively by every backend here: the marker its format always emits, plus at
least one data row.

**3. Units.** GPFS and ``lfs quota`` report KB, ``quota -s`` and
``xfs_quota -h`` report human suffixes, and inode counts are not bytes at all.
Everything block-shaped is normalised to bytes on the way in and inode counts
stay separate as ``kind="files"``, because the inode limit is the one users
actually hit: on the measured home fileset here, blocks were at 2.7% of quota
while inodes were at 12.0%.

Python 3.6 compatible: type comments, no dataclasses, no f-strings, stdlib only.
"""

import os
import re
import time
from typing import Callable, Dict, List, Optional, Sequence, Tuple

from ..discover.identity import group_names, user_name
from ..discover.mounts import MountTable
from ..model import (
    TRANSIENT_CATEGORIES,
    QuotaRow,
    QuotaSnapshot,
    VerdictCategory,
    unavailable_quota,
)
from ..runner import NotRecorded

__all__ = [
    "Backend",
    "EmptyReadingError",
    "check_snapshot",
    "snapshot",
    "read_all",
    "read_best",
    "select_snapshot",
    "default_backends",
    "slice_of",
    "charge",
    "now",
    "BLOCKS",
    "FILES",
    "parse_size",
    "parse_kb",
    "parse_count",
    "parse_limit",
    "strip_marks",
    "is_figure",
    "looks_like_grace",
    "clean_grace",
    "in_grace",
    "norm_scope",
    "current_user",
    "primary_group",
    "all_groups",
    "one_line",
    "grouped_failures",
    "mount_points",
    "mounts_for_device",
    "is_mount_point",
    "enclosing_mount_of_type",
    "device_order",
    "mounts_for_fileset",
    "dedupe_mounts",
]


# The two row kinds. Named constants because "files" versus "inodes" versus
# "count" is exactly the sort of drift that makes a renderer miss half the rows.
BLOCKS = "blocks"
FILES = "files"


# --------------------------------------------------------------------------
# Figures
# --------------------------------------------------------------------------

# A leading sign is not accepted: no backend here prints one, and accepting it
# would turn the grace timer `-` into the number 0.
_SIZE_RE = re.compile(r"^([0-9]*\.?[0-9]+)\s*([KMGTPE]?)(?:i?B)?$", re.IGNORECASE)
_SUFFIXES = ("", "K", "M", "G", "T", "P", "E")

# Every spelling of "no limit is set" that a backend in this package emits.
# `unlimited` is the site wrapper's, and it is not a guess: its source at
# /opt/site/bin/admtool/sitequota.py does `if data[1] == '0': data[1] = 'unlimited'`
# before printing, so a zero limit reaches us as a word where a figure belongs.
_NO_LIMIT_WORDS = frozenset(["unlimited", "unlim", "none", "-", "n/a", "no", "nolimit"])

# Every spelling of "no grace timer is running", lower-cased. Verified against
# the vocabularies the real tools use: quota-tools writes `none`, GPFS writes
# `none`, `lfs` writes `-`, and xfs_quota writes `[--------]`. One tuple,
# because two homes for this rule is how a renderer comes to paint "IN GRACE"
# on a filesystem at 2.9% of its quota.
_NOT_IN_GRACE = ("", "-", "none", "0", "n/a", "--", "[------]", "[--------]")

# A running grace timer, in every spelling the tools use: `6days`, `7 days`,
# `13:20`, `2weeks`, `0seconds`. Needed to tell a grace column from a Remarks
# column when a table prints one trailing text field and no header to name it.
_GRACE_RE = re.compile(
    r"^\[?\s*\d+\s*(?:second|sec|minute|min|hour|day|week|month)s?\s*\]?$|^\d+:\d{2}(?::\d{2})?$",
    re.IGNORECASE,
)


def strip_marks(token):
    # type: (str) -> str
    """A figure with the decorations backends put on it removed.

    ``*`` marks a figure over its limit and ``[N]`` marks one ``lfs quota``
    could not verify. Stripping the whole mark set from both ends is
    order-independent, which is the point: rapiDU stripped them in sequence,
    handled ``[N*]`` and left ``[N]*`` as ``N]*``, which ``int`` rejects and
    which dropped the row. The two marks are the one combination that matters,
    since a degraded OST is what makes a figure unverifiable and being over
    quota is why anyone is reading this.
    """
    return (token or "").strip().strip("[]*")


def parse_size(token, base=1024):
    # type: (str, int) -> Optional[int]
    """Parse ``830.62M`` / ``1024.00G`` / ``0.00K`` / ``35.00G*`` into bytes.

    ``base`` is the divisor the *producer* used for its display steps, and it
    exists for one measured site quirk. The HPC wrapper formats most sections
    with a 1024-based helper and the ``/shared`` section with a 1000-based one
    (``to_binary`` versus ``to_decimal`` in its own source), while the figure it feeds
    both is a count of 1024-byte KB blocks. So a ``7.52T`` in that one section
    is ``7.52 * 1000**3`` KB, and reading it as binary overstates it by 7.4%
    (1024^3 / 1000^3 = 1.0737). The first step is always a 1024-byte block
    because that is the input unit; only the later divisions vary, which is
    why the scale below is ``1024 * base ** (index - 1)`` rather than
    ``base ** index``.
    """
    match = _SIZE_RE.match(strip_marks(token))
    if not match:
        return None
    index = _SUFFIXES.index(match.group(2).upper())
    scale = 1 if index == 0 else 1024 * base ** (index - 1)
    return int(float(match.group(1)) * scale)


def parse_kb(token):
    # type: (str) -> Optional[int]
    """A KB column as bytes. GPFS and ``lfs quota`` both report KB.

    Reading a KB figure as bytes under-reports by 1024x, which would print a
    30 GiB home quota as 30 MiB, so the unit is never inferred from the
    magnitude of the number.
    """
    cleaned = strip_marks(token)
    try:
        return int(cleaned) * 1024
    except ValueError:
        # Some builds print a suffixed figure in a column documented as KB.
        # `parse_size` handles that, and a bare integer never reaches it.
        value = parse_size(cleaned)
        return None if value is None else value * 1024


def parse_count(token):
    # type: (str) -> Optional[int]
    """An inode column. A count, never scaled."""
    cleaned = strip_marks(token)
    try:
        return int(cleaned)
    except ValueError:
        return None


def parse_limit(token, parser=parse_kb):
    # type: (str, Callable[[str], Optional[int]]) -> Optional[int]
    """A limit column, mapping every spelling of "no limit" to ``0``.

    ``0`` and ``None`` are kept apart deliberately and neither is ever read as
    a limit of zero. ``0`` means the backend said there is no limit, which
    `QuotaRow.limit` already discards and `QuotaRow.fraction` already returns
    ``None`` for; ``None`` means the figure could not be read. Getting this
    backwards would report all six unlimited project filesets measured here as
    100% full, and folding ``0`` into ``None`` would report a real unlimited
    row as unreadable.
    """
    cleaned = strip_marks(token).lower()
    if cleaned in _NO_LIMIT_WORDS:
        return 0
    return parser(token)


def is_figure(token):
    # type: (str) -> bool
    """Does this token read as one of the numeric columns?

    A grace timer never does: every spelling ``quota`` uses (``6days``,
    ``13:20``, ``2weeks``, ``none``) fails this, while every numeric column
    passes it under either header. That asymmetry is what lets a row carrying
    exactly one grace timer be read instead of discarded, which is the
    commonest over-quota shape there is.
    """
    return parse_size(strip_marks(token)) is not None


def looks_like_grace(token):
    # type: (str) -> bool
    """Is this trailing text field a grace timer rather than a Remarks column?

    Needed because the human ``mmlsquota`` table prints ``none`` for grace and
    ``DSS.hpc.local`` for Remarks, both as bare trailing words, and one
    measured layout of it prints Remarks with no grace column at all. Where
    the table publishes a header we use that instead; this is the fallback.
    """
    cleaned = (token or "").strip().lower()
    if cleaned in _NOT_IN_GRACE:
        return True
    return bool(_GRACE_RE.match(cleaned))


def clean_grace(raw):
    # type: (str) -> str
    """A grace field with every backend's spelling of "not in grace" removed.

    Normalised at the parser rather than at each consumer. The site wrapper
    prints the literal word ``none``, and a truthiness test on that string
    reads as "in grace", which is how a 2.9%-full home came to raise an alarm
    in rapiDU. This is the only warning in the tool that means writes are
    about to stop, so a false positive spends the one alarm that matters.
    """
    text = (raw or "").strip()
    if text.lower() in _NOT_IN_GRACE:
        return ""
    return text


def in_grace(raw):
    # type: (str) -> bool
    """Is this grace field reporting a running timer?"""
    return bool(clean_grace(raw))


# Every spelling of a quota scope a backend here publishes, mapped to the four
# this codebase reasons about. Lower-casing `USR` yields `usr`, which matches
# nothing, and in rapiDU that made a personal GPFS quota get compared against
# every file in a shared tree. An unrecognised scope stays verbatim rather
# than being coerced, which is the conservative direction.
_SCOPE_ALIASES = {
    "usr": "user",
    "user": "user",
    "u": "user",
    "grp": "group",
    "group": "group",
    "g": "group",
    "fileset": "fileset",
    "filset": "fileset",
    "prj": "project",
    "proj": "project",
    "project": "project",
    "p": "project",
    "filesystem": "filesystem",
}


def norm_scope(raw):
    # type: (str) -> str
    """A backend's spelling of a quota scope, in this codebase's vocabulary."""
    lowered = (raw or "").strip().lower()
    return _SCOPE_ALIASES.get(lowered, lowered)


# --------------------------------------------------------------------------
# Identity and prose
# --------------------------------------------------------------------------


def current_user():
    # type: () -> str
    """The name to ask a quota backend about: who this process actually is.

    `discover.identity.user_name` resolves our own uid through the passwd
    database and degrades to the numeric uid, which every backend here
    accepts. Deliberately not ``$USER``: a stale ``export USER=somebody`` from
    a wrapper or a module file makes ``mmlsquota -u`` return somebody else's
    quota, which the report would then present as yours.
    """
    return user_name()


def primary_group():
    # type: () -> Optional[str]
    """The name of our primary group, or None when it does not resolve."""
    names = group_names([os.getgid()])
    return names[0] if names else None


def all_groups():
    # type: () -> List[str]
    """Every group we are in, primary first."""
    return group_names()


def one_line(text):
    # type: (str) -> str
    """A backend's message collapsed to one line.

    GPFS writes three lines of diagnostics for one failure and `reason` is a
    one-line field. `sanitize` in the model already does this at construction,
    so this is for text that is still being assembled.
    """
    return " ".join((text or "").split())


def grouped_failures(failures):
    # type: (Sequence[Tuple[str, str]]) -> str
    """``(subject, message)`` pairs as one line, grouped by message.

    Two backends here ask the same question several times over: GPFS once per
    device, Lustre once per scope. Joining the messages and dropping the
    subject makes three devices failing three different ways unreadable, since
    *which filesystem needs an admin* is the whole content of one of them.
    Naming the subject on every line instead repeats one fact per subject when
    they all fail alike. Grouping does both.

    Order is tracked explicitly rather than taken from dict insertion: CPython
    3.6 happens to preserve it, but the guarantee starts at 3.7 and this
    package supports 3.6.
    """
    order = []  # type: List[str]
    subjects = {}  # type: Dict[str, List[str]]
    for subject, message in failures:
        message = one_line(message) or "failed with no message"
        if message not in subjects:
            subjects[message] = []
            order.append(message)
        if subject and subject not in subjects[message]:
            subjects[message].append(subject)
    parts = []  # type: List[str]
    for message in order:
        named = subjects[message]
        parts.append("%s: %s" % (", ".join(named), message) if named else message)
    return one_line("; ".join(parts))


# --------------------------------------------------------------------------
# Mount lookups, over the table `discover.mounts` already read
# --------------------------------------------------------------------------
#
# Every function below takes the MountTable the caller was given. Nothing here
# opens /proc/self/mounts: that file is read once, by `discover.mounts`, and a
# second reader would be a second parse of the kernel's octal escaping and a
# second chance to get it wrong.


def mount_points(mounts):
    # type: (Optional[MountTable]) -> List[str]
    """Every path the kernel calls a mount point.

    The distinction between this and "a directory that exists" is load
    bearing. rapiDU's RD-3 returned ``/scratch`` for a fileset named
    ``scratch`` because ``os.path.isdir`` said yes, and ``/scratch`` here is
    merely the parent directory holding three clusters' scratch filesystems,
    so a meadow3 walk was reconciled against meadow2's quota.
    """
    if mounts is None:
        return []
    return [m.mountpoint for m in mounts]


def mounts_for_device(mounts, device):
    # type: (Optional[MountTable], str) -> List[str]
    """Every path a device is mounted at, shortest first.

    One device is routinely mounted several times: ``meadow3_cap`` is at
    ``/home``, ``/project``, ``/software``, ``/programs`` and
    ``/gpfs/meadow3/cap`` on this node, and a row that remembers only the
    first of them fails to map a walk of any of the others.
    """
    if mounts is None or not device:
        return []
    found = [m.mountpoint for m in mounts if m.device == device]
    return dedupe_mounts(sorted(found, key=lambda point: (len(point), point)))


def dedupe_mounts(points):
    # type: (Sequence[str]) -> List[str]
    out = []  # type: List[str]
    for point in points:
        if point and point not in out:
            out.append(point)
    return out


def is_mount_point(mounts, path):
    # type: (Optional[MountTable], str) -> bool
    if not path:
        return False
    return os.path.normpath(path) in mount_points(mounts)


def enclosing_mount_of_type(mounts, path, fstypes):
    # type: (Optional[MountTable], str, Sequence[str]) -> str
    """The longest mount of one of ``fstypes`` that contains ``path``.

    Used only as a fallback for a backend whose output does not name its own
    mount point. Returns ``""`` rather than a guess, so the caller degrades to
    "unmapped" instead of to a confident mis-attribution.
    """
    if mounts is None or not path:
        return ""
    wanted = frozenset(f.lower() for f in fstypes)
    target = os.path.normpath(path)
    best = ""
    for mount in mounts:
        if wanted and mount.fstype.lower() not in wanted:
            continue
        stem = mount.mountpoint.rstrip("/") or "/"
        if target == stem or target.startswith(stem.rstrip("/") + "/"):
            if len(stem) > len(best):
                best = stem
    return best


def device_order(mounts, devices, path):
    # type: (Optional[MountTable], Sequence[str], str) -> List[str]
    """Devices with the one governing ``path`` first, then the rest by name.

    The GPFS fan-out is capped, and a cap only stays harmless while the device
    that can answer the caller's question is inside it. Six GPFS devices is an
    ordinary login node here and a site with twenty is not exotic, where
    alphabetical order would decide by luck whether the walked path's own
    filesystem got asked about at all. This orders the query; it does not
    scope it.
    """
    target = os.path.normpath(path or "/")

    def rank(device):
        # type: (str) -> Tuple[int, str]
        covers = -1
        for point in mounts_for_device(mounts, device):
            stem = point.rstrip("/") or "/"
            if target == stem or target.startswith(stem.rstrip("/") + "/"):
                covers = max(covers, len(stem))
        return (-covers, device)

    return sorted(devices, key=rank)


def mounts_for_fileset(fileset, device_mounts):
    # type: (str, Sequence[str]) -> List[str]
    """Which of a device's own mounts belong to ``fileset``, if that is decidable.

    A GPFS filesystem is mounted in several places and cut into filesets, and
    the two carve it up differently: ``meadow3_cap`` is mounted at ``/home``
    and ``/software`` and holds filesets ``home`` and ``software``. Handing
    every fileset the device's whole mount list, first entry wins, put a
    329 GB figure that lives in ``/software`` on a row labelled ``/home``.

    Three rules, in order, and **every candidate must already be one of
    ``device_mounts``**:

    1. a mount whose path spells the fileset name, with the site convention
       that ``-`` becomes ``/`` (``project-hpc`` for ``/project/hpc``);
    2. a mount whose basename is the fileset name (``software``);
    3. the device mount that is the longest prefix of the path the name spells
       (``/project`` for ``project-hpc``, which is where that fileset lives on
       the measured node).

    rapiDU had a rule between 2 and 3 that tested the spelled path against the
    *host's* whole mount list, and that is how a fileset came to be pinned to a
    mount belonging to a different filesystem entirely: on a meadow3 login node
    it resolved the fileset ``collie3`` to ``/collie3`` and ``project2`` to
    ``/project2``, wrote them with ``guessed=False``, and reconciled a walk
    against another cluster's quota with no caveat at all. The rule is gone
    rather than intersected with ``device_mounts``, because intersecting it
    makes it rule 1 by another spelling.

    Returns ``[]`` when nothing matches, and the caller then keeps the
    device's full list: an unmatched fileset is not evidence for any
    particular mount, and a wrong one is what this exists to stop.
    """
    name = (fileset or "").strip().lower()
    if not name:
        return []
    spelled = "/" + name.replace("-", "/").strip("/")
    wanted = {spelled, "/" + name.strip("/")}
    hit = [m for m in device_mounts if (m.rstrip("/").lower() or "/") in wanted]
    if hit:
        return list(hit)
    hit = [m for m in device_mounts if os.path.basename(m.rstrip("/")).lower() == name]
    if hit:
        return list(hit)
    parents = [
        m
        for m in device_mounts
        if spelled == m.rstrip("/") or spelled.startswith(m.rstrip("/") + "/")
    ]
    if parents:
        return [max(parents, key=lambda m: len(m.rstrip("/")))]
    return []


# --------------------------------------------------------------------------
# Snapshot construction, and the invariant
# --------------------------------------------------------------------------


class EmptyReadingError(AssertionError):
    """A snapshot claimed to be available while carrying no rows.

    Raised rather than repaired, for the reason `model.refuted` raises on a
    transient category: a reading that says "available" with nothing in it
    renders as zero usage, and zero usage is a specific and wrong claim about
    a filesystem somebody may be about to fill. Better to fail a test than to
    print it.
    """


def check_snapshot(snap):
    # type: (QuotaSnapshot) -> QuotaSnapshot
    """Assert the one invariant this package cannot violate.

    Called on every snapshot leaving `read_all`, so a backend that grows the
    bug later is caught in the production path and not only in its own test.
    """
    if snap.available and not snap.rows:
        raise EmptyReadingError(
            "%s returned available=True with zero rows. Build absence with "
            "unavailable_quota(source, category, reason) so it cannot read as "
            "no usage." % (snap.source,)
        )
    if not snap.available and snap.rows:
        raise EmptyReadingError(
            "%s returned available=False while carrying %d rows, so real "
            "figures would be rendered as an absent reading." % (snap.source, len(snap.rows))
        )
    return snap


def snapshot(
    source,  # type: str
    rows,  # type: Sequence[QuotaRow]
    empty_category,  # type: str
    empty_reason,  # type: str
    category=VerdictCategory.OK,  # type: str
    reason="",  # type: str
    taken_at=None,  # type: Optional[float]
    read_at=None,  # type: Optional[float]
    time_note="",  # type: str
    figure_note="",  # type: str
):
    # type: (...) -> QuotaSnapshot
    """The only way a backend here builds its result.

    With rows it is an available reading; without them it is
    `unavailable_quota` carrying the category and reason the caller supplied
    for that case. There is deliberately no third path, so "no rows" can never
    silently become "no usage".
    """
    if rows:
        return QuotaSnapshot(
            source,
            rows=list(rows),
            available=True,
            category=category,
            reason=reason,
            taken_at=taken_at,
            read_at=read_at,
            time_note=time_note,
            figure_note=figure_note,
        )
    return unavailable_quota(
        source,
        empty_category,
        empty_reason,
        taken_at=taken_at,
        read_at=read_at,
        time_note=time_note,
        figure_note=figure_note,
    )


# --------------------------------------------------------------------------
# The backend interface
# --------------------------------------------------------------------------


class Backend(object):
    """One way of asking a filesystem what your quota is.

    Subclasses do exactly two things: say whether they can run here, and
    return one `QuotaSnapshot`. Neither may raise, and neither may call
    `subprocess`: every external command goes through the injected `Runner` so
    that the whole package is replayable from a transcript captured on a
    cluster the author has no account on.
    """

    name = ""  # type: str

    def supported(self, runner):
        # type: (object) -> Optional[str]
        """The resolved path of the executable this backend needs, or None.

        A path rather than a bool, because the path is what gets run.
        `runner.available(name, extra_dirs=...)` is the only correct probe:
        GPFS's tools are world-executable at ``/usr/lpp/mmfs/bin`` and are
        **not on PATH** here, so a backend that probes with PATH alone
        concludes GPFS is absent on a GPFS cluster.
        """
        raise NotImplementedError

    def read(self, runner, mounts, budget, paths):
        # type: (object, Optional[MountTable], object, Sequence[str]) -> QuotaSnapshot
        raise NotImplementedError

    def describe(self):
        # type: () -> str
        return self.name


# --------------------------------------------------------------------------
# Selection
# --------------------------------------------------------------------------


def read_all(backends, runner, mounts, budget, paths):
    # type: (Sequence[Backend], object, Optional[MountTable], object, Sequence[str]) -> List[QuotaSnapshot]
    """Every backend's answer, in order, each one checked.

    Returned in full rather than reduced, because the reduction throws away
    the most useful thing this tool produces. The site wrapper is the only
    backend that knows about filesystems which are **not mounted on this
    node** (measured: it reports ``/cfs3``, ``/cfs4`` and ``/shared`` from a
    node where none of the three paths exists), and it loses the selection in
    `read_best` to any live backend that maps the asked path. The discovery
    layer needs those rows, so it calls this and keeps them all.
    """
    out = []  # type: List[QuotaSnapshot]
    for backend in backends:
        if _exhausted(budget):
            out.append(
                unavailable_quota(
                    backend.name,
                    VerdictCategory.NOT_PROBED,
                    "the quota budget ran out before this backend was asked",
                )
            )
            continue
        try:
            found = backend.supported(runner)
        except Exception as exc:  # a probe must never take the tool down
            out.append(
                unavailable_quota(
                    backend.name,
                    VerdictCategory.BACKEND_FAILED,
                    "could not probe for %s: %s" % (backend.name, exc),
                )
            )
            continue
        if not found:
            out.append(
                unavailable_quota(
                    backend.name,
                    VerdictCategory.NO_QUOTA_BACKEND,
                    "%s is not installed on this node" % (backend.name,),
                )
            )
            continue
        try:
            snap = backend.read(runner, mounts, budget, paths)
        except (NotRecorded, EmptyReadingError):
            # Both are bugs rather than cluster conditions, and both must stay
            # loud. Swallowing `NotRecorded` would turn a gap in a fixture into
            # a cluster's answer, which is nodetop's NT-1 exactly.
            raise
        except Exception as exc:
            # Anything else is a backend misbehaving on unfamiliar output. The
            # tool keeps going and says which backend and why, because a quota
            # reading is an enrichment and losing it must not lose the atlas.
            snap = unavailable_quota(
                backend.name,
                VerdictCategory.BACKEND_FAILED,
                "%s raised %s: %s" % (backend.name, type(exc).__name__, exc),
            )
        out.append(check_snapshot(snap))
    return out


def read_best(backends, runner, mounts, budget, path):
    # type: (Sequence[Backend], object, Optional[MountTable], object, str) -> QuotaSnapshot
    """The backend that can answer for ``path``, else any that answered at all.

    Not first-success-wins; see the module docstring. Two refinements on top of
    the fixed order:

    * among backends that all map the path, a reading whose category is ``OK``
      beats one that is merely ``NO_QUOTA_ENFORCED``, so the ``statvfs``
      capacity fallback can never displace a real quota that was actually set;
    * a backend that produced rows for other paths is the fallback, which is
      still the right answer for a whole-account listing with no path.
    """
    attempts = read_all(backends, runner, mounts, budget, [path])
    return select_snapshot(attempts, path)


def select_snapshot(attempts, path):
    # type: (Sequence[QuotaSnapshot], str) -> QuotaSnapshot
    """The selection rule of `read_best`, over snapshots already collected."""
    winner = None  # type: Optional[QuotaSnapshot]
    fallback = None  # type: Optional[QuotaSnapshot]
    for snap in attempts:
        if not snap.available or not snap.rows:
            continue
        if snap.rows_for_path(path):
            if winner is None:
                winner = snap
            elif winner.category != VerdictCategory.OK and snap.category == VerdictCategory.OK:
                # Capacity from statvfs maps the path too, and it is not a
                # quota. An enforced limit outranks it however the order runs.
                winner = snap
        elif fallback is None:
            fallback = snap
    if winner is not None:
        return winner
    if fallback is not None:
        return fallback
    return _combined_absence(attempts, path)


def _combined_absence(attempts, path):
    # type: (Sequence[QuotaSnapshot], str) -> QuotaSnapshot
    """One unavailable snapshot naming every backend and why each declined.

    The category is the *weakest* claim any attempt supports. If even one
    backend left the question unanswered the combined answer is UNKNOWN, since
    a transient failure is not evidence that no quota exists. Only when every
    attempt came back durable may this say so.
    """
    if not attempts:
        return unavailable_quota(
            "quota",
            VerdictCategory.NOT_PROBED,
            "no quota backend was asked about %s" % (path,),
        )
    categories = [a.category for a in attempts]
    if any(c in TRANSIENT_CATEGORIES for c in categories):
        category = VerdictCategory.UNKNOWN
    elif VerdictCategory.NO_QUOTA_ENFORCED in categories:
        category = VerdictCategory.NO_QUOTA_ENFORCED
    else:
        category = VerdictCategory.NO_QUOTA_BACKEND
    reason = grouped_failures(
        [(a.source, a.reason or "no rows and no reason given") for a in attempts]
    )
    # The age of a cached report survives a failure to use it: a wrapper whose
    # figures we could not map to this path still published when they were
    # taken, and that is a fact the renderer can use.
    taken_at = next((a.taken_at for a in attempts if a.taken_at is not None), None)
    read_at = next((a.read_at for a in attempts if a.read_at is not None), None)
    time_note = next((a.time_note for a in attempts if a.time_note), "")
    figure_note = next((a.figure_note for a in attempts if a.figure_note), "")
    return unavailable_quota(
        "quota",
        category,
        reason,
        taken_at=taken_at,
        read_at=read_at,
        time_note=time_note,
        figure_note=figure_note,
    )


def default_backends(site=None):
    # type: (Optional[object]) -> List[Backend]
    """Every backend, in a fixed order, wired from the site config.

    Ordered by **precision of attribution** rather than by likelihood of
    success, because the order only decides among backends that all map the
    asked path:

    1. ``mmlsquota``   live, per fileset, publishes the device
    2. ``lfs quota``   live, per scope, publishes the mount point
    3. ``xfs_quota``   project quotas, then statvfs capacity as a labelled floor
    4. site wrapper    cached, but the only backend that sees unmounted allocations
    5. ``quota -s``    stock, last because it knows nothing of parallel filesystems

    A site may override the order with ``quota_order``, which is how a cluster
    whose wrapper is authoritative puts it first without editing the package.
    """
    from . import gpfs, lustre, posix, wrapper, xfs

    extra_dirs = tuple(_site_list(site, "extra_bin_dirs")) or None
    wrapper_paths = tuple(_site_list(site, "wrapper_paths")) or None
    built = [
        gpfs.GpfsBackend(extra_dirs=extra_dirs or gpfs.GPFS_BIN_DIRS),
        lustre.LustreBackend(extra_dirs=extra_dirs or ()),
        xfs.XfsBackend(extra_dirs=extra_dirs or xfs.XFS_BIN_DIRS),
        wrapper.SiteWrapperBackend(
            script_paths=wrapper_paths or wrapper.KNOWN_WRAPPER_PATHS,
            extra_dirs=extra_dirs or (),
        ),
        posix.PosixQuotaBackend(extra_dirs=extra_dirs or ()),
    ]
    order = _site_list(site, "quota_order")
    if not order:
        return built
    by_name = {}  # type: Dict[str, Backend]
    for backend in built:
        by_name[backend.name] = backend
    chosen = [by_name[name] for name in order if name in by_name]
    # Anything the site did not name keeps its default position at the end,
    # rather than being silently dropped: a partial order is a preference, not
    # a whitelist, and reading it as a whitelist would remove a working backend
    # because a config file forgot to mention it.
    return chosen + [b for b in built if b not in chosen]


def _site_list(site, attribute):
    # type: (Optional[object], str) -> List[str]
    values = getattr(site, attribute, None) if site is not None else None
    if not values:
        return []
    return [str(v) for v in values if v]


def _exhausted(budget):
    # type: (object) -> bool
    """Whether the shared budget is spent, tolerating no budget at all."""
    if budget is None:
        return False
    return bool(getattr(budget, "exhausted", False))


def slice_of(budget, share=0.5, floor=0.35, ceiling=4.0):
    # type: (object, float, float, float) -> Optional[float]
    """How long one command may take, given what is left of the run's budget.

    ``None`` when there is no budget, which lets the runner apply its own.
    """
    if budget is None:
        return None
    getter = getattr(budget, "slice_for", None)
    if getter is None:
        return None
    return getter(share, floor, ceiling)


def charge(budget, result):
    # type: (object, object) -> None
    """Bill one command's elapsed time to the shared budget."""
    if budget is None:
        return
    biller = getattr(budget, "charge", None)
    if biller is None:
        return
    biller(float(getattr(result, "elapsed_s", 0.0) or 0.0))


def now():
    # type: () -> float
    """Wall clock, in one place so a test can see the same value twice."""
    return time.time()
