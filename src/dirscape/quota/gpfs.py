"""GPFS, through ``mmlsquota``. The backend with the most measured detail.

Everything asserted here was run on a GPFS cluster on 2026-09-21 and the
transcripts are in ``tests/test_quota_gpfs.py``.

**The tools are not on PATH and are world-executable.**::

    $ command -v mmlsquota
    $ ls -l /usr/lpp/mmfs/bin/mmlsquota
    -r-xr-xr-x 1 root root 24328 mmlsquota

So the probe is ``runner.available("mmlsquota", extra_dirs=("/usr/lpp/mmfs/bin",))``
and nothing else. A backend that probes with PATH alone concludes GPFS is
absent on a GPFS cluster.

**The device argument is mandatory.** A bare call asks for "the default
quota-enabled filesystem" and a great many sites have none::

    $ /usr/lpp/mmfs/bin/mmlsquota -Y ; echo rc=$?
    No quota enabled file system found.
    mmlsquota: tslsquota  -Y  failed. Error code 22.
    mmlsquota: Command failed. Examine previous error messages to determine cause.
    rc=22

There is therefore no bare call at all: devices come from the mount table and
each is asked in turn, with the device holding the caller's path asked first so
the fan-out cap bounds cost rather than correctness.

**Exit status is not the signal, in either direction.** On this build the
failures above exit 22, ``-u root`` exits 1 and ``mmlsfileset`` exits 2, all
with their text on stderr, so `Completed.failed` catches them. Other builds are
reported to exit 0 while failing. Neither is relied on: success is asserted
positively, by the ``:HEADER:`` line the ``-Y`` format always emits plus at
least one record, and diagnostics veto the output wherever they appear. The
input that makes this necessary is measured::

    $ /usr/lpp/mmfs/bin/mmlsquota -Y collie3_cap ; echo rc=$?
    mmlsquota:user:HEADER:version:reserved:...:filesetname:
    rc=0

One line, exit 0, nothing on stderr, no records. That is a real answer meaning
"you hold no blocks on this filesystem" and it must not render as zero usage.

**Fileset enumeration is the feature.** ``mmlsquota <device>`` lists every
fileset the user holds blocks in, including filesets they have no unix group
for. Measured on one account: ``project-dahlias`` (11.7 GB), ``project-abe``
(6.8 GB), ``project-bard``, ``project-mdgreenwood`` and ``project-pelican``,
none of which the user can reach. `filesets_seen` exposes those names so the
discovery layer can find storage somebody is charged for and can no longer get
to. It does not resolve them to paths: that is the discovery layer's job and
guessing it is rapiDU's RD-3.

``mmlsfileset``, which would enumerate filesets properly, is not available:
``No filesets found owned by this user`` for an unprivileged caller.
"""

import re
from typing import Dict, List, Optional, Sequence, Tuple

from ..model import QuotaRow, QuotaSnapshot, VerdictCategory, unavailable_quota
from .base import (
    BLOCKS,
    FILES,
    Backend,
    charge,
    clean_grace,
    dedupe_mounts,
    device_order,
    grouped_failures,
    is_figure,
    looks_like_grace,
    mounts_for_device,
    mounts_for_fileset,
    norm_scope,
    now,
    one_line,
    parse_count,
    parse_kb,
    parse_limit,
    primary_group,
    slice_of,
    snapshot,
)

__all__ = [
    "GPFS_BIN_DIRS",
    "GPFS_FSTYPES",
    "MAX_DEVICES",
    "GpfsBackend",
    "filesets_seen",
    "parse_parsable",
    "parse_table",
    "trouble",
    "read_path_fileset",
    "confirm_path_fileset",
    "read_user_quota",
    "read_group_quota",
    "read_fileset_quota",
]


GPFS_BIN_DIRS = ("/usr/lpp/mmfs/bin",)
GPFS_FSTYPES = ("gpfs",)

# Six GPFS devices in the mount table is an ordinary login node here and seven
# was measured on meadow2, so the cap is above both. Each call is a full mm
# command round trip, which is why there is a cap at all, and `device_order`
# puts the device that can answer the caller's question inside it.
MAX_DEVICES = 8

# Diagnostics that mean "this output is not a record set", whatever the exit
# status says. Applied to stdout and stderr alike, because a build that writes
# them to stdout with rc=0 would otherwise have them parsed as data.
_FAILURE_MARKERS = (
    "no quota enabled file system found",
    "command failed",
    "failed. error code",
    "not permitted",
    "no such file or directory",
    # The cause line of a down client. `Failed to connect to file system
    # daemon: No such process` is deliberately NOT a marker: it is the symptom
    # printed above this one, and matching it first would truncate the message
    # to a line that tells the reader nothing they can act on.
    "gpfs is down",
)

# The markers that mean "you are not allowed to ask this", which is a different
# state from a failure: it is transient in the contract's sense, renders as
# unknown, and never as "this user has no quota". Measured:
# `mmlsquota -u root <device>` -> `Operation not permitted`, exit 1.
_PERMISSION_MARKERS = ("not permitted", "permission denied", "not authorized")

# GPFS's closing line is a pointer to the lines above it, not a cause, and it
# is the only one of the three that matches a marker. Returning the first match
# reported it as the reason, which told the reader to examine messages this
# module had just discarded and dropped the one fact that changes what they do
# next ("GPFS is down on this node").
_POINTER = "examine previous error messages"

# A `-Y` record carries around twenty colon-separated fields; a diagnostic
# carries one or two. Requiring a line to be short in that sense before it may
# veto the output means a fileset or Remarks field whose text happens to
# contain a marker cannot discard a whole good reading.
_RECORD_FIELDS = 5

# GPFS percent-encodes characters that would break its own colon-delimited
# format. Without decoding, a fileset named `a:b` arrives as `a%3Ab` and any
# later comparison against a real name fails.
_PERCENT = re.compile(r"%([0-9A-Fa-f]{2})")

# The line `mmlsattr -L` prints the fileset on. Case- and spacing-tolerant
# because the label is a vendor's prose, not a protocol: builds print
# `fileset name:  home` and `Fileset Name: home`.
_MMLSATTR_FILESET = re.compile(r"^\s*fileset\s+name\s*:\s*(?P<name>\S+)", re.IGNORECASE | re.M)

# The scope column of the human table, which is also how the parser finds where
# the leading name columns end.
_TABLE_SCOPES = ("USR", "GRP", "FILESET")

# What the human table prints instead of figures when a fileset has no limits
# set. Measured: `mmlsquota -j home meadow3_cap` prints
# `meadow3_cap FILESET     no limits                       DSS.hpc.local`.
_NO_LIMITS = "no limits"

# In-doubt space beyond this fraction of usage gets said out loud. GPFS
# allocates space to a node before accounting it to a fileset, so `in_doubt` is
# the honest reason a quota figure and a `du` walk disagree. Measured on one
# home fileset: 831 MiB used against 2.18 GiB in doubt, which is 2.7x the
# usage and nobody cross-checking those two numbers would guess why.
_IN_DOUBT_FLOOR = 0.01


def _decode(value):
    # type: (str) -> str
    if "%" not in value:
        return value
    return _PERCENT.sub(lambda m: chr(int(m.group(1), 16)), value)


def trouble(text):
    # type: (str) -> str
    """The diagnostic in ``text`` that says this is not data, if there is one.

    Prefers the line that names the cause. A GPFS failure is three lines and
    the last one is a redirection::

        Failed to connect to file system daemon: No such process
        mmlsquota: GPFS is down on this node.
        mmlsquota: Command failed. Examine previous error messages to determine cause.

    Only the third matches a marker, so returning the first match reported
    that as the reason. When the match is the pointer, the earlier non-record
    lines are the answer, which is doing what it says rather than printing it.
    The pointer is kept only when it is all there is, because a vetoing line
    still has to veto.
    """
    prose = [
        line.strip()
        for line in (text or "").splitlines()
        if line.strip() and len(line.split(":")) < _RECORD_FIELDS
    ]
    for index, line in enumerate(prose):
        low = line.lower()
        if not any(marker in low for marker in _FAILURE_MARKERS):
            continue
        if _POINTER in low:
            return one_line(" ".join(prose[:index])) or line
        return line
    return ""


def _is_permission(text):
    # type: (str) -> bool
    low = (text or "").lower()
    return any(marker in low for marker in _PERMISSION_MARKERS)


# --------------------------------------------------------------------------
# Parsing
# --------------------------------------------------------------------------


def parse_parsable(text):
    # type: (str) -> Tuple[List[Dict[str, str]], bool]
    """Records from ``mmlsquota -Y`` output, and whether a header was present.

    The header flag is returned separately because it is the positive success
    signal: a header with no records is a real answer ("you hold nothing
    here") and no header at all means this output is not a ``-Y`` record set.

    Field names are lower-cased before use. IBM documents the column as
    ``filesetName`` and this build prints ``filesetname``, and an exact-case
    lookup silently returns "" there, which would name every row after its
    filesystem and merge two labs' quotas under one label.
    """
    records = []  # type: List[Dict[str, str]]
    header = None  # type: Optional[List[str]]
    for line in (text or "").splitlines():
        fields = line.split(":")
        if len(fields) < 3:
            continue
        if fields[2] == "HEADER":
            header = [f.strip().lower() for f in fields]
            continue
        if header is None:
            continue
        record = {}  # type: Dict[str, str]
        for name, value in zip(header, fields):
            if name and name not in ("reserved", "mmlsquota"):
                record[name] = _decode(value)
        records.append(record)
    return records, header is not None


def _table_header(line):
    # type: (str) -> Optional[Tuple[bool, List[str], List[str]]]
    """Column names from the human table's header line.

    The table publishes its own schema, and the schema varies: this build
    prints ``KB quota limit in_doubt grace | files quota limit in_doubt grace
    Remarks`` while another documented layout prints ``KB quota limit in_doubt
    | files quota limit Remarks``. Reading the header instead of counting
    columns is what makes both parse, and it is also how the ``-j`` layout,
    which has no ``Fileset`` column at all, announces itself.
    """
    tokens = line.split()
    if not tokens or tokens[0].lower() != "filesystem":
        return None
    lowered = [t.lower() for t in tokens]
    if "type" not in lowered:
        return None
    type_at = lowered.index("type")
    has_fileset = type_at > 1
    rest = tokens[type_at + 1 :]
    if "|" in rest:
        split_at = rest.index("|")
        left, right = rest[:split_at], rest[split_at + 1 :]
    else:
        left, right = rest, []
    return has_fileset, [t.lower() for t in left], [t.lower() for t in right]


def parse_table(text):
    # type: (str) -> Tuple[List[Dict[str, str]], bool, List[str]]
    """Records from the human ``mmlsquota`` table, plus header-seen and notes.

    Returned in the same record shape as `parse_parsable` so exactly one
    function turns records into rows. Notes carry the filesets the table
    reported as having no limits at all, which is an answer rather than a
    parse failure.
    """
    records = []  # type: List[Dict[str, str]]
    notes = []  # type: List[str]
    header = None  # type: Optional[Tuple[bool, List[str], List[str]]]
    for raw in (text or "").splitlines():
        line = raw.strip()
        if not line or line.startswith("---"):
            continue
        lowered = line.lower()
        # `-g` output opens with `Disk quotas for group hpc (gid 20008):`, and
        # the first line of every layout is the `Block Limits | File Limits`
        # banner. Neither is a row and neither is the schema.
        if lowered.startswith("disk quotas for") or "block limits" in lowered:
            continue
        found = _table_header(line)
        if found is not None:
            header = found
            continue
        if header is None:
            continue
        record, note = _table_row(line, header)
        if note:
            notes.append(note)
        if record is not None:
            records.append(record)
    return records, header is not None, notes


def _table_row(line, header):
    # type: (str, Tuple[bool, List[str], List[str]]) -> Tuple[Optional[Dict[str, str]], str]
    has_fileset, left_names, right_names = header
    if "|" in line:
        left_text, right_text = line.split("|", 1)
    else:
        left_text, right_text = line, ""
    left = left_text.split()
    right = right_text.split()
    if not left:
        return None, ""

    device = left[0]
    scope_at = -1
    for index, token in enumerate(left):
        if token.upper() in _TABLE_SCOPES:
            scope_at = index
            break
    if scope_at < 1:
        return None, ""
    # A fileset name cannot contain whitespace in GPFS, so everything between
    # the device and the scope is the fileset when the header says there is
    # one. Joined rather than indexed so a build that pads differently cannot
    # silently drop part of a name.
    fileset = " ".join(left[1:scope_at]) if has_fileset else ""
    scope = left[scope_at]
    values = left[scope_at + 1 :]

    if " ".join(values[:2]).lower().startswith(_NO_LIMITS):
        named = "%s:%s" % (device, fileset) if fileset else device
        return None, "%s has no limits set" % (named,)

    record = {"filesystemname": device, "filesetname": fileset, "quotatype": scope}
    _zip_values(record, left_names, values, "block")
    _zip_values(record, right_names, right, "files")
    return record, ""


def _zip_values(record, names, values, prefix):
    # type: (Dict[str, str], Sequence[str], Sequence[str], str) -> None
    """Map one side of a table row onto the names its header published.

    Falls back to position when the header is unhelpful, and stops at the
    shorter of the two so a missing trailing column (Remarks, or a grace
    column this build does not print) is tolerated rather than fatal.
    """
    if names and len(names) >= 3:
        for name, value in zip(names, values):
            if name in ("remarks", "|"):
                continue
            key = _NAME_MAP.get(name)
            if key:
                record[prefix + key] = value
        return
    # No usable header: used, soft and hard are the first three figures, an
    # optional fourth figure is in_doubt, and a trailing non-figure is grace
    # only if it reads like a timer. `DSS.hpc.local` in the Remarks column is
    # not a timer, and reading it as one would paint a false IN GRACE warning.
    figures = []  # type: List[str]
    grace = ""
    for token in values:
        if is_figure(token) and not grace:
            figures.append(token)
        elif looks_like_grace(token) and not grace:
            grace = token
    for key, index in (("usage", 0), ("quota", 1), ("limit", 2), ("indoubt", 3)):
        if index < len(figures):
            record[prefix + key] = figures[index]
    if grace:
        record[prefix + "grace"] = grace


# Header column name to the suffix used in the record keys, which are the `-Y`
# field names minus their prefix. One vocabulary for both parsers.
_NAME_MAP = {
    "kb": "usage",
    "blocks": "usage",
    "block": "usage",
    "used": "usage",
    "files": "usage",
    "quota": "quota",
    "soft": "quota",
    "limit": "limit",
    "hard": "limit",
    "in_doubt": "indoubt",
    "indoubt": "indoubt",
    "grace": "grace",
}


def _rows_from_records(records, mounts, notes):
    # type: (Sequence[Dict[str, str]], object, List[str]) -> List[QuotaRow]
    """Two rows per record: one for blocks, one for inodes.

    Both kinds always, because the inode limit is what users actually hit and
    reporting only blocks is rapiDU's RD-9/RD-15 confusion. Measured on the
    home fileset here: 2.7% of the block quota against 12.0% of the inode
    quota, so the number nearer the wall is the one a blocks-only report
    leaves out.
    """
    rows = []  # type: List[QuotaRow]
    for record in records:
        device = (record.get("filesystemname") or "").strip()
        fileset = (record.get("filesetname") or "").strip()
        scope = norm_scope(record.get("quotatype", ""))
        points, guessed, note = _attribute(mounts, device, fileset)
        if note and note not in notes:
            notes.append(note)
        for kind, prefix, parser in ((BLOCKS, "block", parse_kb), (FILES, "files", parse_count)):
            used = parser(record.get(prefix + "usage", ""))
            if used is None:
                continue
            row = QuotaRow(
                fileset or device,
                kind,
                scope,
                used,
                parse_limit(record.get(prefix + "quota", ""), parser),
                parse_limit(record.get(prefix + "limit", ""), parser),
                clean_grace(record.get(prefix + "grace", "")),
                points[0] if points else None,
                device=device,
                guessed=guessed,
                note=note,
                in_doubt=parser(record.get(prefix + "indoubt", "")),
            )
            row.mounts = list(points)
            rows.append(row)
    return rows


def _attribute(mounts, device, fileset):
    # type: (object, str, str) -> Tuple[List[str], bool, str]
    """Which mounts a row governs, and how confident that mapping is.

    The device comes from the backend and the device-to-mounts map comes from
    the kernel, so a single-mount device is a published fact. Narrowing a
    multi-mount device down to one fileset is an inference from a name, so it
    is flagged and it names the command that settles it. `mmlsattr -L` is
    unprivileged and works here, which is why `confirm_path_fileset` exists
    rather than this pretending to be certain.
    """
    device_points = mounts_for_device(mounts, device)
    if not device_points:
        return (
            [],
            False,
            "%s is not in this node's mount table, so these figures have no local path"
            % (device or "this filesystem",),
        )
    if len(device_points) == 1:
        return device_points, False, ""
    narrowed = mounts_for_fileset(fileset, device_points)
    if narrowed:
        return (
            narrowed,
            True,
            "mount inferred from the fileset name out of %d that %s is mounted at; "
            "confirm with mmlsattr -L" % (len(device_points), device),
        )
    return (
        device_points,
        True,
        "fileset %s could not be tied to any one of the %d mounts of %s"
        % (fileset or "(unnamed)", len(device_points), device),
    )


def filesets_seen(snap):
    # type: (QuotaSnapshot) -> List[str]
    """Every fileset name a reading mentioned, in the order it appeared.

    This is a feature and not a side effect. ``mmlsquota <device>`` lists
    every fileset the user holds blocks in, **including filesets they have no
    unix group for**, which is the only route this tool has to storage
    somebody is charged for and can no longer reach. Measured on one account:
    five ``project-*`` filesets totalling ~19 GB that the user cannot list.

    Names only, deliberately. Resolving one to a path is the discovery
    layer's job and inferring it from the name is rapiDU's RD-3.
    """
    seen = []  # type: List[str]
    for row in snap.rows:
        name = row.fileset
        if name and name != row.device and name not in seen:
            seen.append(name)
    return seen


# --------------------------------------------------------------------------
# The backend
# --------------------------------------------------------------------------


class GpfsBackend(Backend):
    """``mmlsquota`` once per GPFS device, ``-Y`` preferred."""

    name = "mmlsquota"

    def __init__(
        self,
        max_devices=MAX_DEVICES,  # type: int
        extra_dirs=GPFS_BIN_DIRS,  # type: Sequence[str]
        user=None,  # type: Optional[str]
        prefer_parsable=True,  # type: bool
    ):
        # type: (...) -> None
        self.max_devices = max_devices
        self.extra_dirs = tuple(extra_dirs or ())
        self._user = user
        self.prefer_parsable = prefer_parsable

    def supported(self, runner):
        # type: (object) -> Optional[str]
        return runner.available("mmlsquota", extra_dirs=self.extra_dirs)

    def read(self, runner, mounts, budget, paths):
        # type: (object, object, object, Sequence[str]) -> QuotaSnapshot
        exe = self.supported(runner)
        if not exe:
            return unavailable_quota(
                self.name,
                VerdictCategory.NO_QUOTA_BACKEND,
                "mmlsquota is not installed here, including at %s"
                % (", ".join(self.extra_dirs) or "any named directory",),
            )
        devices = mounts.devices_of_type(GPFS_FSTYPES) if mounts is not None else []
        if not devices:
            return unavailable_quota(
                self.name,
                VerdictCategory.NO_QUOTA_BACKEND,
                "mmlsquota is installed but no gpfs filesystem is mounted on this node",
            )

        ordered = device_order(mounts, devices, paths[0] if paths else "/")
        asked = ordered[: self.max_devices]
        notes = []  # type: List[str]
        if len(ordered) > len(asked):
            # Said out loud. A bound that silently drops filesystems reads as
            # "this site has no quota" when what happened is that nobody asked.
            notes.append(
                "%d of %d gpfs filesystems were asked (%s first, as it holds this path); "
                "raise the cap to ask the rest" % (len(asked), len(ordered), asked[0])
            )

        records = []  # type: List[Dict[str, str]]
        failures = []  # type: List[Tuple[str, str]]
        refused = []  # type: List[Tuple[str, str]]
        answered_empty = []  # type: List[str]
        for device in asked:
            if _spent(budget):
                notes.append("the quota budget ran out before every filesystem was asked")
                break
            found, outcome, detail = self._ask(runner, budget, exe, device)
            records.extend(found)
            if outcome == "refused":
                refused.append((device, detail))
            elif outcome == "failed":
                failures.append((device, detail))
            elif outcome == "empty":
                answered_empty.append(device)

        rows = _rows_from_records(records, mounts, notes)
        read_at = now()
        return snapshot(
            self.name,
            rows,
            self._empty_category(failures, refused, answered_empty),
            self._empty_reason(failures, refused, answered_empty, notes),
            reason=one_line("; ".join(notes)),
            taken_at=read_at if rows else None,
            read_at=read_at,
            # A live query, not a cached report, so the figure is as current as
            # the filesystem's own accounting. Leaving taken_at unset made
            # age_seconds None in rapiDU, which permanently tripped its
            # "published no timestamp" blocker and made one verdict unreachable
            # on every GPFS site.
            time_note=(
                "read live from mmlsquota; GPFS accounting can lag a write by "
                "up to a minute, so treat a small difference as timing"
                if rows
                else ""
            ),
            figure_note=_in_doubt_note(rows),
        )

    def _ask(self, runner, budget, exe, device):
        # type: (object, object, str, str) -> Tuple[List[Dict[str, str]], str, str]
        """One device. Returns its records and how the call went."""
        argv = [exe]
        if self.prefer_parsable:
            argv.append("-Y")
        argv.extend(self._who() + [device])
        result = runner.run(argv, timeout=slice_of(budget))
        charge(budget, result)
        if result.failed:
            detail = trouble(result.stderr) or trouble(result.stdout) or result.diagnostic
            if result.timed_out:
                return [], "failed", detail
            return [], "refused" if _is_permission(detail) else "failed", detail

        # Exited without failing, so the text has to be examined on its own
        # terms. A marker vetoes it even now, because a build that prints
        # diagnostics to stdout with rc=0 would otherwise have them parsed.
        veto = trouble(result.stdout)
        if veto:
            return [], "refused" if _is_permission(veto) else "failed", veto

        if self.prefer_parsable:
            records, saw_header = parse_parsable(result.stdout)
            if saw_header:
                return records, ("records" if records else "empty"), ""
            # No `:HEADER:` at all means this build does not speak `-Y`. The
            # human table is the fallback rather than a second guess at the
            # same format.
            return self._ask_table(runner, budget, exe, device)
        return self._ask_table(runner, budget, exe, device)

    def _who(self):
        # type: () -> List[str]
        """``-u <user>`` only when a user was explicitly named.

        A bare ``mmlsquota <device>`` already reports the calling user, so
        ``-u <yourself>`` is redundant, and redundancy here is not free: on a
        node whose name service cannot resolve our own uid, and meadow2
        compute nodes cannot, the name would be a numeric string that this
        code had to construct for no reason. It also keeps the self-query to
        one argv shape, which is the one a captured transcript records.
        """
        return ["-u", self._user] if self._user else []

    def _ask_table(self, runner, budget, exe, device):
        # type: (object, object, str, str) -> Tuple[List[Dict[str, str]], str, str]
        result = runner.run([exe] + self._who() + [device], timeout=slice_of(budget))
        charge(budget, result)
        if result.failed:
            detail = trouble(result.stderr) or trouble(result.stdout) or result.diagnostic
            return [], "refused" if _is_permission(detail) else "failed", detail
        veto = trouble(result.stdout)
        if veto:
            return [], "refused" if _is_permission(veto) else "failed", veto
        records, saw_header, notes = parse_table(result.stdout)
        if not saw_header:
            return [], "failed", "mmlsquota printed no table this parser recognises"
        if records:
            return records, "records", ""
        # A header with no rows, or a `no limits` row: the command answered and
        # the answer is that nothing here constrains this user.
        return [], "empty", one_line("; ".join(notes))

    @staticmethod
    def _empty_category(failures, refused, answered_empty):
        # type: (Sequence[object], Sequence[object], Sequence[str]) -> str
        """Which kind of absence this was, when there are no rows.

        Ordered by how much the question went unanswered. A device that failed
        outranks one that answered emptily, because a failure is not evidence
        that no quota exists; a refusal outranks nothing and is its own state,
        transient, and never presented as "you have no quota".
        """
        if failures:
            return VerdictCategory.BACKEND_FAILED
        if refused:
            return VerdictCategory.PERMISSION_TO_ASK_DENIED
        if answered_empty:
            return VerdictCategory.NO_QUOTA_ENFORCED
        return VerdictCategory.BACKEND_FAILED

    @staticmethod
    def _empty_reason(failures, refused, answered_empty, notes):
        # type: (Sequence[Tuple[str, str]], Sequence[Tuple[str, str]], Sequence[str], Sequence[str]) -> str
        parts = []  # type: List[str]
        if failures or refused:
            parts.append(grouped_failures(list(failures) + list(refused)))
        if answered_empty and not failures and not refused:
            parts.append(
                "mmlsquota answered for %s and named no fileset this user holds blocks in. "
                "GPFS lists only filesets with usage, so this is not proof that no limit "
                "exists" % (", ".join(answered_empty),)
            )
        parts.extend(notes)
        return one_line("; ".join(p for p in parts if p)) or "mmlsquota returned no rows"


def _in_doubt_note(rows):
    # type: (Sequence[QuotaRow]) -> str
    """Doubt about the FIGURES, when GPFS says a material amount is unaccounted.

    `figure_note` rather than `time_note`: the number is uncertain, its age is
    not. GPFS hands a node space before accounting it to a fileset, so the
    quota figure and a `du` walk legitimately differ by this much.
    """
    worst = None  # type: Optional[QuotaRow]
    for row in rows:
        if row.kind != BLOCKS or not row.in_doubt or not row.used:
            continue
        if row.in_doubt < row.used * _IN_DOUBT_FLOOR:
            continue
        if worst is None or (row.in_doubt or 0) > (worst.in_doubt or 0):
            worst = row
    if worst is None:
        return ""
    return (
        "gpfs reports %d bytes in doubt against %d used on %s, which is space "
        "allocated but not yet accounted to a fileset, so a du walk will "
        "legitimately disagree by about that much"
        % (worst.in_doubt or 0, worst.used or 0, worst.label)
    )


def _spent(budget):
    # type: (object) -> bool
    return budget is not None and bool(getattr(budget, "exhausted", False))


# --------------------------------------------------------------------------
# The other three queries a normal user may make
# --------------------------------------------------------------------------


def read_user_quota(runner, mounts, budget, user, devices=None, extra_dirs=GPFS_BIN_DIRS):
    # type: (object, object, object, str, Optional[Sequence[str]], Sequence[str]) -> QuotaSnapshot
    """``mmlsquota -u <user> <device>``, for a named user.

    Asking about anybody but yourself is refused. Measured:
    ``mmlsquota -u root meadow3_cap`` prints ``Operation not permitted`` and
    exits 1, so this maps to `PERMISSION_TO_ASK_DENIED`, which is transient and
    renders as unknown rather than as "root has no quota".
    """
    backend = GpfsBackend(extra_dirs=extra_dirs, user=user)
    wanted = devices
    if wanted is None:
        wanted = mounts.devices_of_type(GPFS_FSTYPES) if mounts is not None else []
    return _fan_out(runner, mounts, budget, backend, wanted, "mmlsquota -u %s" % (user,))


def read_group_quota(runner, mounts, budget, group=None, devices=None, extra_dirs=GPFS_BIN_DIRS):
    # type: (object, object, object, Optional[str], Optional[Sequence[str]], Sequence[str]) -> QuotaSnapshot
    """``mmlsquota -g <group> <device>``. Works for a normal user.

    The group quota is routinely the binding limit on a shared project
    directory, which is where an HPC user actually runs out of space, and it
    is a different number from the personal one: measured on this account,
    ``-g`` reports 16 KB on the home fileset where ``-u`` reports 831 MiB.
    """
    return _scoped(
        runner,
        mounts,
        budget,
        ["-g", group or primary_group() or ""],
        devices,
        extra_dirs,
        "mmlsquota -g",
    )


def read_fileset_quota(runner, mounts, budget, fileset, devices=None, extra_dirs=GPFS_BIN_DIRS):
    # type: (object, object, object, str, Optional[Sequence[str]], Sequence[str]) -> QuotaSnapshot
    """``mmlsquota -j <fileset> <device>``. Works for a normal user.

    This layout has no ``Fileset`` column and prints the literal text
    ``no limits`` where the figures belong when nothing is set, which is an
    answer (`NO_QUOTA_ENFORCED`) and not a parse failure.
    """
    return _scoped(
        runner,
        mounts,
        budget,
        ["-j", fileset],
        devices,
        extra_dirs,
        "mmlsquota -j %s" % (fileset,),
    )


def _scoped(runner, mounts, budget, flags, devices, extra_dirs, source):
    # type: (object, object, object, Sequence[str], Optional[Sequence[str]], Sequence[str], str) -> QuotaSnapshot
    exe = runner.available("mmlsquota", extra_dirs=tuple(extra_dirs or ()))
    if not exe:
        return unavailable_quota(
            source, VerdictCategory.NO_QUOTA_BACKEND, "mmlsquota is not installed here"
        )
    if not [f for f in flags if f]:
        return unavailable_quota(
            source, VerdictCategory.UNKNOWN, "no name to ask about could be resolved"
        )
    wanted = (
        list(devices)
        if devices is not None
        else (mounts.devices_of_type(GPFS_FSTYPES) if mounts is not None else [])
    )
    if not wanted:
        return unavailable_quota(
            source,
            VerdictCategory.NO_QUOTA_BACKEND,
            "no gpfs filesystem is mounted on this node",
        )

    records = []  # type: List[Dict[str, str]]
    notes = []  # type: List[str]
    failures = []  # type: List[Tuple[str, str]]
    refused = []  # type: List[Tuple[str, str]]
    empty = []  # type: List[str]
    for device in wanted[:MAX_DEVICES]:
        if _spent(budget):
            notes.append("the quota budget ran out before every filesystem was asked")
            break
        result = runner.run([exe, "-Y"] + list(flags) + [device], timeout=slice_of(budget))
        charge(budget, result)
        detail = trouble(result.stderr) or trouble(result.stdout)
        if result.failed or detail:
            message = detail or result.diagnostic
            (refused if _is_permission(message) else failures).append((device, message))
            continue
        found, saw_header = parse_parsable(result.stdout)
        if not saw_header:
            table, saw_table, table_notes = parse_table(result.stdout)
            notes.extend(table_notes)
            if not saw_table:
                failures.append((device, "no parseable output"))
                continue
            found = table
        if found:
            records.extend(found)
        else:
            empty.append(device)

    rows = _rows_from_records(records, mounts, notes)
    read_at = now()
    return snapshot(
        source,
        rows,
        GpfsBackend._empty_category(failures, refused, empty),
        GpfsBackend._empty_reason(failures, refused, empty, notes),
        reason=one_line("; ".join(notes)),
        taken_at=read_at if rows else None,
        read_at=read_at,
        figure_note=_in_doubt_note(rows),
    )


def _fan_out(runner, mounts, budget, backend, devices, source):
    # type: (object, object, object, GpfsBackend, Sequence[str], str) -> QuotaSnapshot
    """Run one backend against an explicit device list rather than the table."""
    records = []  # type: List[Dict[str, str]]
    notes = []  # type: List[str]
    failures = []  # type: List[Tuple[str, str]]
    refused = []  # type: List[Tuple[str, str]]
    empty = []  # type: List[str]
    exe = backend.supported(runner)
    if not exe:
        return unavailable_quota(
            source, VerdictCategory.NO_QUOTA_BACKEND, "mmlsquota is not installed here"
        )
    if not devices:
        return unavailable_quota(
            source,
            VerdictCategory.NO_QUOTA_BACKEND,
            "no gpfs filesystem is mounted on this node",
        )
    for device in list(devices)[:MAX_DEVICES]:
        if _spent(budget):
            notes.append("the quota budget ran out before every filesystem was asked")
            break
        found, outcome, detail = backend._ask(runner, budget, exe, device)
        records.extend(found)
        if outcome == "refused":
            refused.append((device, detail))
        elif outcome == "failed":
            failures.append((device, detail))
        elif outcome == "empty":
            empty.append(device)
    rows = _rows_from_records(records, mounts, notes)
    read_at = now()
    return snapshot(
        source,
        rows,
        GpfsBackend._empty_category(failures, refused, empty),
        GpfsBackend._empty_reason(failures, refused, empty, notes),
        reason=one_line("; ".join(notes)),
        taken_at=read_at if rows else None,
        read_at=read_at,
        figure_note=_in_doubt_note(rows),
    )


# --------------------------------------------------------------------------
# Which fileset a path is in, asked rather than inferred
# --------------------------------------------------------------------------


def read_path_fileset(runner, path, budget=None, extra_dirs=GPFS_BIN_DIRS):
    # type: (object, str, object, Sequence[str]) -> Optional[str]
    """``mmlsattr -L <path>``: which GPFS fileset a path is in.

    Unprivileged and measured working here: ``/home/jdoe42`` gives ``home``
    and ``/project/hpc`` gives ``project-hpc``. This is the one route from a
    path to a fileset that is evidence rather than a naming convention, which
    is why `QuotaRow`'s own contract says the fileset comes from here.

    ``None`` when the command is absent, fails, or prints no fileset line, and
    the caller keeps whatever inference it already had. A confirmation that
    does not arrive must not remove information.
    """
    exe = runner.available("mmlsattr", extra_dirs=tuple(extra_dirs or ()))
    if not exe:
        return None
    result = runner.run([exe, "-L", path], timeout=slice_of(budget))
    charge(budget, result)
    # `failed` is checked, and then the answer is asserted positively anyway:
    # the fileset line is present or there is no answer.
    found = _MMLSATTR_FILESET.search(result.stdout or "")
    if not found:
        return None
    name = found.group("name").strip()
    # GPFS spells "not in a named fileset" as `root`, which is a real fileset
    # that mmlsquota reports under that name, so it is returned as-is.
    return name or None


def _governs(mount, path):
    # type: (str, str) -> bool
    """Whether a row claiming ``mount`` also claims to govern ``path``.

    True when ``path`` IS the mount or sits below it. The equality half was
    missing and that was a real defect: `"/home".startswith("/home/")` is
    False, so confirming `/home` against a row whose guessed mount was exactly
    `/home` did nothing, and a `project-hpc` row kept a `/home` mount that
    `mmlsattr` had just contradicted. That is rapiDU's RD-3 (a guessed mount
    confidently attributed to the wrong place) surviving the very probe added
    to kill it.

    Deliberately one-directional. A row mounted at `/project/hpc` is NOT
    contradicted by learning that `/project` is fileset `project`, because
    nested filesets are normal on GPFS and the row never claimed the parent.
    """
    if not mount or not path:
        return False
    base = mount.rstrip("/") or "/"
    target = path.rstrip("/") or "/"
    if base == "/":
        return True
    return target == base or target.startswith(base + "/")


def confirm_path_fileset(runner, snap, path, budget=None, extra_dirs=GPFS_BIN_DIRS):
    # type: (object, QuotaSnapshot, str, object, Sequence[str]) -> Optional[str]
    """Pin the rows governing ``path`` to the fileset ``mmlsattr`` reports.

    Turns an inference into a measurement: rows whose fileset matches keep the
    mount and lose ``guessed``, and rows for other filesets on the same device
    lose the mount they had merely been narrowed onto. Without this, a
    filesystem cut into filesets leaves every row on a multi-mount device
    equally plausible, which is the hedge rapiDU shipped instead.
    """
    name = read_path_fileset(runner, path, budget=budget, extra_dirs=extra_dirs)
    if not name:
        return None
    for row in snap.rows:
        if not row.guessed:
            continue
        if row.fileset == name:
            row.mount = path if not row.mount else row.mount
            row.mounts = dedupe_mounts(list(row.mounts) or [row.mount or path])
            row.guessed = False
            row.note = "fileset confirmed by mmlsattr -L %s" % (path,)
        elif row.mount and _governs(row.mount, path):
            row.note = (
                "mmlsattr -L says %s is in fileset %s, not %s, so this row does "
                "not govern that path" % (path, name, row.fileset)
            )
            row.mount = None
            row.mounts = []
    return name
