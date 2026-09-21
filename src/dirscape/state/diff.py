"""What changed between two runs, and the one rule that outranks everything.

**THE INVARIANT: a failed probe never produces a state-change claim.** If this
run could not determine a root's reach, the row carries forward the previous
run's verdict and is labelled `unknown`. It must never be labelled `closed`.

The precedent is nodetop's NT-1, where a replay restored a "the probe ran" flag
without the probe's result, so 21 queues that had been measured as refusing came
back rendered as fine. Inverted, which is the shape it would take here, the same
mistake tells a user they have lost access to a directory when in truth one
``stat`` timed out. That is a false alarm about data loss, and it is the worst
output this tool could produce: a user who believes a filesystem has gone will
start restoring from backup, or open a ticket, or panic, over nothing.

Three mechanisms enforce it, not one, because a single guard is one edit away
from being bypassed:

1. `Reach.improved` and `Reach.regressed` already refuse to compare anything
   against `UNKNOWN`, and they are the only comparison used here.
2. `_reach_change` is the ONLY function in this module that can return `CLOSED`
   or `OPENED`. `test_state_invariant.py` parses this file and fails if any
   other function returns either of them, so a second code path cannot be added
   quietly.
3. `gone` is gated on positive evidence that this run LOOKED: see `_gone_or_unknown`.

The same principle runs through the numeric side, where it is easier to miss. An
unmeasured quota is not a usage of zero, so a `None` on either side of a
comparison produces no `grew` and no `shrank` rather than a change from nothing.

Python 3.6 compatible, stdlib only, and `model` is the only package imported.
"""

import time
from typing import Any, Dict, List, Optional, Sequence, Tuple

from ..model import Reach, Root, VerdictCategory, category_label, sanitize
from .snapshot import (
    RootRecord,
    Snapshot,
    fingerprint_mismatch,
    same_vantage,
    vantage_warning,
)

__all__ = [
    "NEW",
    "OPENED",
    "CLOSED",
    "GREW",
    "SHRANK",
    "STRANDED",
    "UNKNOWN",
    "GONE",
    "LABELS",
    "LABEL_ORDER",
    "DELTA_FRACTION",
    "DELTA_BYTES_FLOOR",
    "DELTA_INODES_FLOOR",
    "Change",
    "DiffResult",
    "diff",
    "describe",
    "label_rank",
]


# --------------------------------------------------------------------------
# The label vocabulary, which is a wire format
# --------------------------------------------------------------------------

#: A root never seen anywhere in this lineage.
NEW = "new"
#: Reach improved, with both runs conclusive.
OPENED = "opened"
#: Reach regressed, with both runs conclusive. The only label that can alarm a
#: user about losing access, and therefore the one with the most guards on it.
CLOSED = "closed"
#: Usage moved past both thresholds.
GREW = "grew"
SHRANK = "shrank"
#: The user holds blocks in a scope they cannot reach. A standing condition
#: rather than a delta; see `_stranded_change`.
STRANDED = "stranded"
#: This run could not determine the root's state. Carries the previous verdict.
UNKNOWN = "unknown"
#: Absent from this run, AND this run was conclusive about that.
GONE = "gone"

#: Most significant first. This is the order `describe` sorts in and the order a
#: renderer should print: a lost directory and space a user cannot reach come
#: before an allocation that grew, and an unmeasured root comes last because it
#: is a gap rather than an event.
LABEL_ORDER = (CLOSED, STRANDED, NEW, OPENED, GONE, GREW, SHRANK, UNKNOWN)

LABELS = frozenset(LABEL_ORDER)


# --------------------------------------------------------------------------
# Thresholds
# --------------------------------------------------------------------------

#: Relative gate: a move smaller than this share of the previous reading is not
#: reported.
DELTA_FRACTION = 0.05

#: Absolute gate for blocks, and both gates must fire. Calibrated against the
#: quotas actually in force here, read with `quota` on a login node: the
#: smallest block quota a user can hit is the 30 G home, so 64 MiB is 0.21% of
#: the smallest limit that exists and cannot move anyone measurably toward it.
#: The upper side matters too: that home holds 830 MB today, where 5% of used is
#: only 41 MB, so this floor is what stops a `~/.cache` refresh or a
#: `__pycache__` sweep from being reported as news. On the larger roots the
#: relative gate dominates and this one never binds (scratch holds 22 G, where
#: 5% is 1.1 G; /project holds 77 T, where 5% is 3.9 T).
DELTA_BYTES_FLOOR = 64 * 1024 * 1024

#: Absolute gate for inodes, by the same argument. The smallest inode quota
#: measured here is the home's 300,000 files, so 1,000 files is 0.33% of it.
#: Below that the relative gate would fire on noise: two of the roots on this
#: node hold 1 and 230 files, where 5% is a single file and one temporary file
#: would be an event.
DELTA_INODES_FLOOR = 1000


# --------------------------------------------------------------------------
# Formatting the evidence
# --------------------------------------------------------------------------

_UNITS = ("B", "KiB", "MiB", "GiB", "TiB", "PiB")


def _bytes(value):
    # type: (Optional[int]) -> str
    """A byte count for a `because` sentence.

    Local to this module on purpose, and deliberately minimal: `render/` owns
    the real column formatting, and a `because` string needs a readable number
    inside a sentence rather than a padded cell. If `render/` grows a canonical
    formatter this should call it instead of keeping a second one.
    """
    if value is None:
        return "an unmeasured amount"
    size = float(value)
    for unit in _UNITS:
        if abs(size) < 1024.0 or unit == _UNITS[-1]:
            if unit == "B":
                return "%d B" % (int(size),)
            return "%.1f %s" % (size, unit)
        size /= 1024.0
    return "%.1f %s" % (size, _UNITS[-1])


def _count(value):
    # type: (Optional[int]) -> str
    if value is None:
        return "an unmeasured number of files"
    return "%s files" % (format(int(value), ","),)


def _when(stamp):
    # type: (Optional[float]) -> str
    """A timestamp for a human standing at the cluster.

    Local time rather than UTC: the reader is at the site, and a `why`
    explanation that says 19:02 for a run they remember making at 14:02 reads
    as being about some other run.
    """
    if stamp is None:
        return "an earlier run"
    return time.strftime("%Y-%m-%d %H:%M", time.localtime(stamp))


def _pct(previous, current):
    # type: (Optional[int], Optional[int]) -> str
    if previous is None or current is None:
        return ""
    if previous == 0:
        return " (from nothing)"
    return " (%+.0f%%)" % (100.0 * (current - previous) / float(abs(previous)),)


def _inconclusive_reason(record):
    # type: (Optional[RootRecord]) -> str
    """Why this run could not answer, in the probe's own words where possible.

    `reach_reason` is what the access probe wrote, so it names the probe:
    "probe did not finish within 1.00s", "os.access reports read and execute".
    That is the whole reason the snapshot stores it. Where it is empty the axis
    verdicts are asked instead, and their categories are the next best thing:
    a `PROBE_TIMEOUT` on `present` says which question went unanswered.
    """
    if record is None:
        return "the root was not reported by this run"
    if record.reach_reason:
        return record.reach_reason
    for verdict in (record.present, record.mounted, record.allocated):
        if verdict.category in (VerdictCategory.NOT_PROBED,):
            continue
        if not verdict.durable:
            return category_label(verdict.category)
    return "the probe reported no reason"


def _probe_name(record):
    # type: (Optional[RootRecord]) -> str
    """Which discovery probe found a root, for a `new` row's evidence."""
    if record is None or not record.sources:
        return "discovery"
    return ", ".join(record.sources)


# --------------------------------------------------------------------------
# One change
# --------------------------------------------------------------------------


class Change(object):
    """One labelled difference, with the evidence that produced it.

    ``previous`` and ``current`` are the values, and ``axis`` names what they
    are, so a renderer never has to infer from the label whether it is holding a
    reach state or a byte count.

    They carry the WIRE vocabulary, never the display form: a `Reach` token
    such as ``LISTABLE``, a `VerdictCategory` token, or an integer. These land
    in ``--json`` where a consumer may switch on them, and `model` keeps the two
    vocabularies apart for exactly this reason (nodetop's NT-5 printed raw enum
    members beside prose in a user-facing column). The human form of the same
    facts is in ``because``, which is prose and nothing else.

    ``current`` and ``observed`` are separate, and the separation is the
    invariant made visible. When this run was inconclusive, ``current`` is the
    CARRIED FORWARD previous value (what the user should still believe) while
    ``observed`` is the `UNKNOWN` this run actually measured (what was
    measured). Collapsing them would either hide the gap or lose the last known
    state, and the row needs both.
    """

    __slots__ = (
        "label",
        "path",
        "device",
        "axis",
        "previous",
        "current",
        "observed",
        "carried_forward",
        "because",
        "first_seen",
        "renamed_from",
        "identity_changed",
    )

    def __init__(
        self,
        label,  # type: str
        path,  # type: str
        because,  # type: str
        axis="",  # type: str
        device="",  # type: str
        previous=None,  # type: Any
        current=None,  # type: Any
        observed=None,  # type: Any
        carried_forward=False,  # type: bool
        first_seen=None,  # type: Optional[float]
        renamed_from=None,  # type: Optional[str]
        identity_changed=False,  # type: bool
    ):
        # type: (...) -> None
        self.label = label
        self.path = path
        self.device = device
        self.axis = axis
        self.previous = previous
        self.current = current
        self.observed = observed
        self.carried_forward = carried_forward
        # Sanitised because a `because` can quote a backend's stderr, and
        # rapiDU's RD-6 is a filename with an embedded newline forging a table
        # row. Generous limit: this is a full sentence of explanation.
        self.because = sanitize(because, limit=400)
        self.first_seen = first_seen
        self.renamed_from = renamed_from
        self.identity_changed = identity_changed

    def to_json(self):
        # type: () -> Dict[str, Any]
        out = {
            "label": self.label,
            "path": self.path,
            "previous": self.previous,
            "current": self.current,
            "because": self.because,
        }  # type: Dict[str, Any]
        if self.device:
            out["device"] = self.device
        if self.axis:
            out["axis"] = self.axis
        if self.carried_forward:
            out["carried_forward"] = True
            out["observed"] = self.observed
        if self.first_seen is not None:
            out["first_seen"] = self.first_seen
        if self.renamed_from:
            out["renamed_from"] = self.renamed_from
        if self.identity_changed:
            out["identity_changed"] = True
        return out

    def __repr__(self):
        # type: () -> str
        return "Change(%s, %r)" % (self.label, self.path)


class DiffResult(object):
    """The change list, plus what the renderer needs to explain its own state.

    Behaves as the list of records (`len`, iteration, indexing, truthiness), so
    a caller that wants the list has one, while the flags that cannot be
    expressed as a record still have somewhere to live. The first run is the
    case that forces this: it has zero records and something important to say.
    """

    __slots__ = ("records", "no_baseline", "cross_cluster", "same_vantage", "warnings")

    def __init__(
        self,
        records=None,  # type: Optional[Sequence[Change]]
        no_baseline=False,  # type: bool
        cross_cluster=False,  # type: bool
        same_vantage=True,  # type: bool
        warnings=(),  # type: Sequence[str]
    ):
        # type: (...) -> None
        self.records = list(records or [])  # type: List[Change]
        self.no_baseline = no_baseline
        self.cross_cluster = cross_cluster
        self.same_vantage = same_vantage
        self.warnings = [sanitize(w, limit=400) for w in warnings]  # type: List[str]

    def labels(self):
        # type: () -> List[str]
        return [record.label for record in self.records]

    def with_label(self, label):
        # type: (str) -> List[Change]
        return [record for record in self.records if record.label == label]

    def to_json(self):
        # type: () -> Dict[str, Any]
        return {
            "no_baseline": self.no_baseline,
            "cross_cluster": self.cross_cluster,
            "same_vantage": self.same_vantage,
            "warnings": list(self.warnings),
            "changes": [record.to_json() for record in self.records],
        }

    def __len__(self):
        # type: () -> int
        return len(self.records)

    def __iter__(self):
        # type: () -> Any
        return iter(self.records)

    def __getitem__(self, index):
        # type: (Any) -> Any
        return self.records[index]

    def __repr__(self):
        # type: () -> str
        return "DiffResult(%d changes, no_baseline=%s)" % (len(self.records), self.no_baseline)


# --------------------------------------------------------------------------
# The single source of `closed` and `opened`
# --------------------------------------------------------------------------


def _reach_change(previous_reach, current_reach):
    # type: (str, str) -> Optional[str]
    """The ONLY place in this module that `CLOSED` or `OPENED` can come from.

    Delegates entirely to `Reach.regressed` and `Reach.improved`, which refuse
    to compare anything against `UNKNOWN`, so an inconclusive run on either
    side returns None here and is handled as an unknown by the caller.

    Concentrated into one function deliberately. The invariant is easy to state
    and easy to re-break from a second site that "just" compares two reach
    strings, so `test_state_invariant.py` parses this module and fails if any
    other function returns either label. Keeping this function tiny is what
    makes that check meaningful.
    """
    if Reach.regressed(previous_reach, current_reach):
        return CLOSED
    if Reach.improved(previous_reach, current_reach):
        return OPENED
    return None


def _because_reach(current, previous, previous_stamp):
    # type: (RootRecord, RootRecord, Optional[float]) -> str
    """Evidence for a reach change, in one shape for both directions.

    No branch on the direction: the sentence is "what this run measured, where
    the previous run measured something else", which reads correctly whether
    access was gained or lost. One sentence builder means the two directions
    cannot drift into describing themselves differently.
    """
    measured = current.reach_reason or "the access probe reported %s" % (
        Reach.label(current.reach),
    )
    return "%s, where the run at %s measured %s" % (
        measured,
        _when(previous_stamp),
        Reach.label(previous.reach),
    )


# --------------------------------------------------------------------------
# The numeric side
# --------------------------------------------------------------------------


def _crossed(previous, current, floor):
    # type: (Optional[int], Optional[int], int) -> bool
    """Whether a usage move is worth a row. BOTH gates must fire.

    **A `None` on either side is not a change.** An unmeasured quota is not a
    usage of zero, so a backend that failed this run must not produce a
    "shrank to nothing" row, which is the numeric form of the same false alarm
    this module exists to prevent.

    `max(abs(previous), 1)` rather than a division, so a root that went from
    zero to something is decided by the absolute floor alone instead of by a
    division by zero.
    """
    if previous is None or current is None:
        return False
    delta = abs(current - previous)
    if delta < floor:
        return False
    return delta >= DELTA_FRACTION * max(abs(previous), 1)


def _usage_changes(current, previous, previous_stamp):
    # type: (RootRecord, RootRecord, Optional[float]) -> List[Change]
    """`grew` and `shrank` rows for blocks and for inodes.

    Up to one row per axis, so a path can produce two. That is deliberate and
    not noise: 2 TB arriving in 40 files and 2 TB arriving in four million
    files are different events with different consequences, and an inode quota
    is hit long before a block quota on this site.
    """
    out = []  # type: List[Change]
    backend = current.quota_source or previous.quota_source or "the quota backend"
    axes = (
        ("used_bytes", current.used_bytes, previous.used_bytes, DELTA_BYTES_FLOOR, _bytes),
        ("used_inodes", current.used_inodes, previous.used_inodes, DELTA_INODES_FLOOR, _count),
    )
    for axis, now, before, floor, show in axes:
        # The None checks are spelled here as well as inside `_crossed` because
        # this is where the two values are used as numbers. `_crossed` owns the
        # rule; this owns the narrowing.
        if before is None or now is None or not _crossed(before, now, floor):
            continue
        label = GREW if now > before else SHRANK
        out.append(
            Change(
                label=label,
                path=current.path,
                device=current.device,
                axis=axis,
                previous=before,
                current=now,
                first_seen=current.first_seen,
                because="%s reports %s, where the run at %s reported %s%s"
                % (backend, show(now), _when(previous_stamp), show(before), _pct(before, now)),
            )
        )
    return out


def _stranded_change(current, previous):
    # type: (RootRecord, Optional[RootRecord]) -> Optional[Change]
    """A `stranded` row whenever the flag is set, not only when it appears.

    The one label here that is a standing alarm rather than a delta. The user is
    being charged for space they cannot see until they act on it, so suppressing
    the row on every run after the first would mean the tool stops mentioning
    it. The flag itself is set by the quota layer, which is the only thing that
    can see it: a GPFS per-device listing enumerates every fileset the user has
    usage in, including ones they have no group for.
    """
    if not current.stranded:
        return None
    where = current.fileset or current.device or current.path
    backend = current.quota_source or "the quota backend"
    return Change(
        label=STRANDED,
        path=current.path,
        device=current.device,
        axis="used_bytes",
        previous=previous.used_bytes if previous is not None else None,
        current=current.used_bytes,
        first_seen=current.first_seen,
        because="%s reports %s held in %s, which this run's access probe cannot reach (%s)"
        % (backend, _bytes(current.used_bytes), where, Reach.label(current.reach)),
    )


# --------------------------------------------------------------------------
# Absence
# --------------------------------------------------------------------------


def _was_there(record):
    # type: (RootRecord) -> bool
    """Whether the baseline conclusively established that the root existed.

    Without a conclusive baseline there is nothing to have lost. A root that was
    already unreadable, or already only inferred from an allocation database, is
    not evidence that anything went away when it stops being reported.
    """
    return record.present.confirmed or record.reach in (Reach.LISTABLE, Reach.TRAVERSE)


def _gone_or_unknown(
    previous_record,  # type: RootRecord
    current_snapshot,  # type: Snapshot
    previous_stamp,  # type: Optional[float]
    vantage,  # type: bool
    current_record=None,  # type: Optional[RootRecord]
):
    # type: (...) -> Change
    """`gone` only with positive evidence that THIS run looked. Otherwise `unknown`.

    A root missing from the current run is not evidence that it went away. Two
    things can supply that evidence, and both also require the same vantage
    point, because mount sets differ by node class and ``/cfs3`` is mounted on
    login nodes only:

    * the root IS in this run and its `present` verdict is a durable refusal,
      which means the probe stat'd the path and the path was not there; or
    * the root is absent from this run and the run's `discovery` verdict
      confirms the sweep completed, so the sweep looked and did not find it.

    Anything else, including a `discovery` that was never set, produces
    `unknown` with the previous verdict carried forward. That default is the
    invariant: the tool does not get to claim a loss until something proves it
    looked.
    """
    reason = ""
    if not vantage:
        reason = (
            "this run was on a different vantage point (%s) than the baseline, and "
            "mount sets differ by node class, so its absence here is not evidence "
            "that it went away" % (current_snapshot.node_class,)
        )
    elif not _was_there(previous_record):
        reason = (
            "the run at %s never conclusively found it either (%s), so there is no "
            "baseline for it to have been lost from"
            % (_when(previous_stamp), Reach.label(previous_record.reach))
        )
    elif current_record is not None and current_record.present.refuted:
        return Change(
            label=GONE,
            path=previous_record.path,
            device=previous_record.device,
            axis="presence",
            previous=previous_record.reach,
            current=current_record.present.category,
            first_seen=previous_record.first_seen,
            because="the presence probe on this host reports %s, where the run at %s "
            "measured %s"
            % (
                category_label(current_record.present.category),
                _when(previous_stamp),
                Reach.label(previous_record.reach),
            ),
        )
    elif current_record is None and current_snapshot.discovery.confirmed:
        return Change(
            label=GONE,
            path=previous_record.path,
            device=previous_record.device,
            axis="presence",
            previous=previous_record.reach,
            # No verdict token to report: this run produced no record for the
            # path at all, and the evidence is the completed sweep named in
            # `because` rather than a per-root probe.
            current=None,
            first_seen=previous_record.first_seen,
            because="the discovery sweep on this host completed and did not find it, "
            "where the run at %s found it via %s"
            % (_when(previous_stamp), _probe_name(previous_record)),
        )
    else:
        reason = (
            "this run's discovery sweep did not confirm that it completed (%s), so an "
            "absent root cannot be told from an unfinished search"
            % (category_label(current_snapshot.discovery.category),)
        )
    return Change(
        label=UNKNOWN,
        path=previous_record.path,
        device=previous_record.device,
        axis="reach",
        previous=previous_record.reach,
        current=previous_record.reach,
        observed=Reach.UNKNOWN,
        carried_forward=True,
        first_seen=previous_record.first_seen,
        because="%s; carrying forward %s from the run at %s"
        % (reason, Reach.label(previous_record.reach), _when(previous_stamp)),
    )


# --------------------------------------------------------------------------
# The diff
# --------------------------------------------------------------------------


def _write_back(snapshot, record, change):
    # type: (Snapshot, RootRecord, Change) -> None
    """Put the finding on the live `Root`, when there is one.

    `Root.labels` is documented as "delta labels from the diff layer" and
    `Root.renamed_from` as being set here, so this is that. A snapshot loaded
    from disk has no live roots and nothing happens.

    **The carried forward reach is deliberately NOT written to `root.reach`.**
    That would make this run's object claim a reach it did not measure, which is
    NT-1 exactly: a restored value with no probe behind it. The carried value
    lives on the `Change`, which is labelled `unknown`, so a renderer can say
    "listable as of yesterday" without this run pretending it measured that.
    """
    root = snapshot.root_for(record.key)
    if root is None:
        return
    if change.label not in root.labels:
        root.labels.append(change.label)
    if change.renamed_from:
        root.renamed_from = change.renamed_from


def diff(previous, current, since=None):
    # type: (Optional[Snapshot], Snapshot, Optional[float]) -> DiffResult
    """Compare two runs.

    ``since`` is an absolute epoch cutoff, from `snapshot.cutoff_for`. It
    restricts `new` to roots first seen at or after it; the other labels are
    differences between the two snapshots the caller chose, and choosing the
    right baseline for a window is `Lineage.baseline_for`'s job.

    Returns a `DiffResult`, which behaves as the list of `Change` records and
    also carries the flags a renderer needs when there are no records to show.
    """
    # The first run, handled explicitly. Not one `new` row per root: on a first
    # run everything is new by definition, which is a page of noise carrying no
    # information, and it would train a user to ignore the one label that means
    # "look at this". A previous snapshot that found nothing is the same case:
    # it is not a baseline, so nothing can be compared against it.
    if previous is None or not previous.records:
        return DiffResult(
            [],
            no_baseline=True,
            warnings=[
                "no baseline yet, seeded one from this run (%d roots); the next run "
                "will report what changed" % (len(current.records),)
            ],
        )

    # Refused rather than warned. Across clusters every label this could emit
    # would be a claim about a different filesystem: every root would read as
    # `gone` and every root here as `new`. `fingerprint_mismatch` is also
    # exported so a caller can make this decision before spending the time.
    mismatch = fingerprint_mismatch(previous, current.cluster_fingerprint)
    if mismatch:
        return DiffResult([], no_baseline=True, cross_cluster=True, warnings=[mismatch])

    vantage = same_vantage(previous, current)
    warnings = []  # type: List[str]
    warning = vantage_warning(previous, current)
    if warning:
        warnings.append(warning)

    records = []  # type: List[Change]
    matched = set()  # type: set
    stamp = previous.taken_at

    # Sorted by path so the output is stable across runs that found the same
    # things in a different order, which is what makes two `--json` outputs
    # comparable by a script.
    for record in sorted(current.records, key=lambda r: (r.path, r.device)):
        before = previous.match(record)
        renamed_from = None  # type: Optional[str]
        if before is None:
            # Second chance on the inode. A renamed directory misses on the
            # path and hits here, and it is the same object, so it must not be
            # reported as one root lost and another gained.
            by_inode = previous.by_identity(record.identity)
            if by_inode is not None:
                before = by_inode
                renamed_from = by_inode.path
        if before is not None:
            matched.add(before.key)

        for change in _changes_for(record, before, stamp, renamed_from, current, vantage):
            records.append(change)
            _write_back(current, record, change)

    # Roots the baseline had and this run did not report at all.
    for record in sorted(previous.records, key=lambda r: (r.path, r.device)):
        if record.key in matched:
            continue
        change = _gone_or_unknown(record, current, stamp, vantage, None)
        records.append(change)

    if since is not None:
        # A `new` row whose `first_seen` predates the window is not new to the
        # question the user asked. A row with no `first_seen` at all is kept:
        # an unknown age is not evidence of being old.
        records = [
            change
            for change in records
            if change.label != NEW or change.first_seen is None or change.first_seen >= since
        ]

    return DiffResult(
        describe(records),
        no_baseline=False,
        same_vantage=vantage,
        warnings=warnings + _identity_notes(current, previous, records),
    )


def _changes_for(
    record,  # type: RootRecord
    before,  # type: Optional[RootRecord]
    stamp,  # type: Optional[float]
    renamed_from,  # type: Optional[str]
    current,  # type: Snapshot
    vantage,  # type: bool
):
    # type: (...) -> List[Change]
    """Every row one current root produces."""
    out = []  # type: List[Change]

    if before is None:
        out.append(
            Change(
                label=NEW,
                path=record.path,
                device=record.device,
                axis="presence",
                previous=None,
                current=record.reach,
                first_seen=record.first_seen,
                because="no run in this lineage has seen it before; found this run by %s"
                % (_probe_name(record),),
            )
        )
        stranded = _stranded_change(record, None)
        if stranded is not None:
            out.append(stranded)
        return out

    # A replacement: same path, new object behind it. Compared only when both
    # sides actually have an identity, because a probe that could not `stat`
    # produces None and a None must never read as "the fileset was recreated".
    identity_changed = (
        record.identity is not None
        and before.identity is not None
        and record.identity != before.identity
    )

    if renamed_from:
        out.append(
            Change(
                label=NEW,
                path=record.path,
                device=record.device,
                axis="presence",
                # The old path is in `renamed_from`, not here: `previous` holds
                # the previous VALUE on this axis, and a path is not one.
                previous=None,
                current=record.reach,
                first_seen=record.first_seen,
                renamed_from=renamed_from,
                because="this is %s under a new name: same (st_dev, st_ino) as the run "
                "at %s recorded there, so nothing was lost" % (renamed_from, _when(stamp)),
            )
        )

    stranded = _stranded_change(record, before)
    if stranded is not None:
        out.append(stranded)

    out.extend(_reach_rows(record, before, stamp, identity_changed))

    # A path whose presence was durably refused this run is a loss, not a reach
    # change, so it is asked about here and gated the same way absence is.
    if record.present.refuted and _was_there(before):
        out.append(_gone_or_unknown(before, current, stamp, vantage, record))

    out.extend(_usage_changes(record, before, stamp))

    if identity_changed:
        for change in out:
            change.identity_changed = True
    return out


def _reach_rows(record, before, stamp, identity_changed):
    # type: (RootRecord, RootRecord, Optional[float], bool) -> List[Change]
    """The reach comparison, and the carry forward when this run could not answer.

    Four cases, and only one of them may produce a label about access:

    ==========================  ================================================
    baseline / this run         result
    ==========================  ================================================
    conclusive / conclusive     `_reach_change`, so `closed`, `opened` or nothing
    conclusive / UNKNOWN        `unknown`, carrying the baseline's verdict
    UNKNOWN / conclusive        nothing: no conclusive baseline to have moved from
    UNKNOWN / UNKNOWN           nothing: still unmeasured, and that is not news
    ==========================  ================================================

    The third row is the one that looks like a missed feature and is not. A run
    that finally measures a root as listable has not learned that access
    IMPROVED, only that it is listable now, and `opened` is a claim about a
    change. `Reach.improved` enforces this and this table documents it.
    """
    if record.reach == Reach.UNKNOWN:
        if before.reach == Reach.UNKNOWN:
            return []
        return [
            Change(
                label=UNKNOWN,
                path=record.path,
                device=record.device,
                axis="reach",
                previous=before.reach,
                current=before.reach,
                observed=Reach.UNKNOWN,
                carried_forward=True,
                first_seen=record.first_seen,
                identity_changed=identity_changed,
                because="this run did not determine reach (%s); carrying forward %s "
                "from the run at %s"
                % (
                    _inconclusive_reason(record),
                    Reach.label(before.reach),
                    _when(stamp),
                ),
            )
        ]

    label = _reach_change(before.reach, record.reach)
    if label is None:
        return []
    return [
        Change(
            label=label,
            path=record.path,
            device=record.device,
            axis="reach",
            previous=before.reach,
            current=record.reach,
            observed=record.reach,
            first_seen=record.first_seen,
            identity_changed=identity_changed,
            because=_because_reach(record, before, stamp),
        )
    ]


def _identity_notes(current, previous, records):
    # type: (Snapshot, Snapshot, Sequence[Change]) -> List[str]
    """Warnings for a path whose object was replaced with nothing else to say.

    A fileset deleted and recreated at the same path, with the same reach and
    the same usage, produces no labelled change, and the vocabulary has no word
    for it. Inventing one would be worse than a sentence: `new` would claim a
    path the user has had for a year is new, and `gone` would claim a loss for
    a path that is right there. So it is a warning, which is the channel for
    something noticed that is not an event.
    """
    noted = {change.path for change in records}
    out = []  # type: List[str]
    for record in sorted(current.records, key=lambda r: r.path):
        if record.path in noted or record.identity is None:
            continue
        before = previous.match(record)
        if before is None or before.identity is None:
            continue
        if before.identity != record.identity:
            out.append(
                "%s is a different directory than it was at %s (st_dev, st_ino) moved "
                "from %s to %s, so the fileset behind the path was replaced"
                % (record.path, _when(previous.taken_at), before.identity, record.identity)
            )
    return out


# --------------------------------------------------------------------------
# Ordering
# --------------------------------------------------------------------------


def label_rank(label):
    # type: (str) -> int
    """Position in `LABEL_ORDER`, with anything unrecognised sorted last.

    Falls back rather than raising: a label from a newer dirscape reading an
    older one's output should sort badly, not take down the render.
    """
    try:
        return LABEL_ORDER.index(label)
    except ValueError:
        return len(LABEL_ORDER)


def describe(records):
    # type: (Sequence[Change]) -> List[Change]
    """The records, most significant first, then by path.

    A new list rather than a sort in place, so a caller can keep the discovery
    order if it wants it.
    """
    return sorted(records, key=lambda change: (label_rank(change.label), change.path, change.axis))
