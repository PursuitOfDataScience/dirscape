"""What every root looked like on one run, kept as a bounded lineage.

This is the memory behind the tool's headline feature, "show me what is NEW",
and the whole of that feature rests on one thing: **newness is decided by the
lineage and by nothing else.** Two site-specific measurements forced that, and
both are recorded here so nobody re-adds the heuristic they disprove.

**1. Birth time is not available.** ``stat -c %W`` returns ``0`` and ``%w``
returns ``-`` on every GPFS path here and on ``/home``, and Python's
``os.stat_result`` has no ``st_birthtime`` on this platform. There is no
creation timestamp to read, so a root cannot be dated from its own metadata.

**2. Directory mtime is a decoy, not a fallback.** ``/project/aarnold``'s
fileset first appears in the site quota archive between 2026-03-01 and
2026-04-01 while its directory mtime reads 2026-05-15, two months late. An
mtime heuristic would therefore have called a two month old allocation "new",
and would call a busy decade old directory new every time somebody writes in
it. `first_seen` is carried forward from the lineage instead, and never
recomputed.

**Why a lineage rather than one file per run.** ``--since 90d`` has to have
something to compare against, and a per-run file leaves the caller to glob a
directory, sort it, and decide which of thirty files is the baseline. One file
holds the whole history, so the retention policy is enforced in one place and
is visible in one place. Measured for the sizing: a per-root record is around
400 bytes of compact JSON, `quota` on this login node lists 13 rows today and
full discovery adds ``/home``, ``/scratch``, ``/project``, ``/project2``,
``/software``, ``/programs``, ``/cfs3``, ``/collie3``, ``/tmp``, ``/dev/shm``
and local disk, so 20 to 40 roots per entry. A 31 entry lineage at 40 roots
each measures under 512 KiB, loads in 4.5 ms and dumps in 6.3 ms, which is
0.06% of `runner.Budget`'s default 8 s allowance.

**The order a caller uses these in**, which is the only order in which
`first_seen` comes out right::

    lineage  = Lineage.load(fingerprint=identity.fingerprint)
    current  = Snapshot.from_roots(roots, identity=identity, lineage=lineage)
    previous = lineage.baseline_for(cutoff_for(90 * 86400))
    result   = diff(previous, current)
    lineage.append(current)
    lineage.save()

`Snapshot.from_roots` consults the lineage BEFORE the new entry is appended, so
"the earliest run that saw this root" cannot accidentally become "this run".

**This package depends on `model` and on nothing else.** The cluster
fingerprint, the hostname and the node class are all produced by
`dirscape.discover.identity`, and they arrive here as an argument rather than
as an import: `state/` must not reach into discovery, and an `Identity` is
duck typed on ``.fingerprint``, ``.hostname`` and ``.node_class`` so a test can
pass a stub.

Python 3.6 compatible, and stdlib only: `json`, `os`, `time`. `tempfile` is
deliberately not imported for the atomic write, and `socket` is deliberately not
imported for the hostname, because the deployability claim is about the bare
``/usr/bin/python3`` of a login node and every module this file does not need is
one less thing to be wrong about there.
"""

import json
import os
import time
from typing import Any, Dict, List, Optional, Sequence, Tuple

from ..model import (
    QuotaSnapshot,
    Reach,
    Root,
    Verdict,
    VerdictCategory,
    sanitize,
    unknown,
)

__all__ = [
    "SCHEMA",
    "TOOL",
    "KEEP_RECENT",
    "MAX_BYTES",
    "LOGIN",
    "COMPUTE",
    "UNKNOWN_CLASS",
    "RootRecord",
    "Snapshot",
    "Lineage",
    "current_hostname",
    "cutoff_for",
    "default_path",
    "state_dir",
    "fingerprint_mismatch",
    "same_vantage",
    "vantage_warning",
]


#: Bumped when the shape of a stored entry changes in a way that an older or
#: newer dirscape could MISREAD rather than merely fail to read. A file whose
#: schema does not match is discarded as a baseline, never salvaged: half
#: understood history is how a diff comes to claim a change that did not happen.
SCHEMA = 1

#: Written into the file so a stray JSON document that happens to live at this
#: path is not mistaken for a lineage.
TOOL = "dirscape"

#: Entries retained, plus the oldest one as a permanent anchor. Thirty daily
#: runs is a month of history, and the anchor is what keeps ``--since 90d`` from
#: silently degrading into ``--since 30d`` once the window fills.
KEEP_RECENT = 30

#: Ceiling for the whole file. See the module docstring for the measurement: a
#: full 31 entry lineage of 40 roots is under 512 KiB, so 1 MiB is roughly
#: double the expected worst case and leaves a login node that mounts several
#: clusters some headroom before any entry is dropped for size.
MAX_BYTES = 1024 * 1024

#: The node class vocabulary. These three strings are exactly what
#: `dirscape.discover.mounts.node_class` returns. They are duplicated here
#: rather than imported, so that `state/` keeps its one way dependency on
#: `model`; `test_state_vantage.py` asserts the two agree, which is what makes
#: the duplication safe instead of a drift waiting to happen.
LOGIN = "login"
COMPUTE = "compute"
UNKNOWN_CLASS = "unknown"

#: Compact separators, and sorted keys so the file is byte stable across runs
#: that found the same thing. Stability matters for a file a user may keep in a
#: dotfile repo and for anyone diffing two lineages by hand.
_COMPACT = (",", ":")

#: Caps on the free text carried per root. `reach_reason` is the probe's own
#: words and is what lets `diff` name the probe rather than restate the label,
#: so it is worth its bytes; 160 characters is a full sentence of diagnostic and
#: about 40% of a record.
_REASON_LIMIT = 160
_SOURCE_LIMIT = 64
#: More than four discovery probes finding one root adds no evidence, and the
#: list is only ever used to say which probe saw it first.
_MAX_SOURCES = 4

#: Every category the contract defines. Derived from the class rather than
#: listed, because a hardcoded list is how a guard like `_restore_verdict` comes
#: to pass vacuously after a category is added.
_KNOWN_CATEGORIES = frozenset(
    value
    for name, value in vars(VerdictCategory).items()
    if not name.startswith("_") and isinstance(value, str)
)

_KNOWN_REACH = frozenset(Reach.ORDER)

#: Bytes of envelope around the entry list, measured once rather than guessed,
#: so `_prune` can bound the file without serialising it repeatedly.
_ENVELOPE_BYTES = len(
    json.dumps({"tool": TOOL, "schema": SCHEMA, "entries": []}, separators=_COMPACT)
)


# --------------------------------------------------------------------------
# Reducing and restoring a verdict
# --------------------------------------------------------------------------


def _reduce_verdict(verdict):
    # type: (Optional[Verdict]) -> List[Any]
    """A verdict as the ``[value, category]`` pair the snapshot stores.

    **The category half is not optional.** Storing ``allocated: true`` and
    dropping the category is nodetop's NT-1 exactly: a `PROBE_TIMEOUT` would
    come back indistinguishable from an `OK`, and 21 queues that had been
    measured as refusing came back rendered as fine. Here the same loss would
    let a timed out probe reload as a confirmed answer, which the diff would
    then compare against and report as a change.
    """
    if verdict is None:
        return [None, VerdictCategory.NOT_PROBED]
    return [verdict.value, verdict.category]


def _restore_verdict(pair):
    # type: (Any) -> Verdict
    """The pair back into a `Verdict`, treating anything unexpected as unknown.

    Constructed directly rather than through `refuted`, which correctly refuses
    a transient category: a stored pair may legitimately be
    ``(None, PROBE_TIMEOUT)`` and reconstructing it must not raise.

    An unrecognised category is forced to `UNKNOWN`. A truncated or hand edited
    file could otherwise carry ``(False, "GARBAGE")``, and since `durable` is
    defined as "not in the transient set", an unknown token would read as a
    DURABLE refusal. Corrupt input must degrade to "I do not know", never to
    "no".
    """
    value = None  # type: Optional[bool]
    category = VerdictCategory.UNKNOWN
    if isinstance(pair, (list, tuple)) and len(pair) == 2:
        if pair[0] is not None:
            value = bool(pair[0])
        token = pair[1]
        if isinstance(token, str) and token in _KNOWN_CATEGORIES:
            category = token
    return Verdict(value, category)


def _restore_reach(token):
    # type: (Any) -> str
    """A stored reach string, or `Reach.UNKNOWN` for anything unrecognised."""
    if isinstance(token, str) and token in _KNOWN_REACH:
        return token
    return Reach.UNKNOWN


def _restore_number(value):
    # type: (Any) -> Optional[int]
    """A stored byte or inode count, or None.

    None rather than 0 for anything unreadable. A zero would render as an empty
    quota, which reads as "plenty of room" and is the same class of lie as
    reporting an unmeasured quota as unlimited.
    """
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return int(value)
    return None


def _restore_time(value):
    # type: (Any) -> Optional[float]
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, (int, float)):
        return float(value)
    return None


# --------------------------------------------------------------------------
# Pulling the two numbers out of a quota reading
# --------------------------------------------------------------------------


def _quota_numbers(snap, path, fileset, kind):
    # type: (Optional[QuotaSnapshot], str, str, str) -> Tuple[Optional[int], Optional[int]]
    """``(used, limit)`` for one path and one kind, or ``(None, None)``.

    Three selectors, in order, and the order is the point. rapiDU's RD-18 was
    exactly this: a device and a fileset are different identities, and one GPFS
    device here is mounted at ``/home``, ``/project``, ``/software`` and
    ``/programs``, which are four different quotas. So the mount published by
    the backend is tried first, the fileset name second, and a bare single row
    only when there is exactly one candidate.

    **Ambiguity returns unknown rather than a guess.** More than one row of the
    right kind with nothing tying either of them to this path means the tool
    does not know which number belongs here, and a wrong quota figure is a
    worse answer than an absent one.
    """
    if snap is None or not snap.available:
        # An unavailable reading has no numbers, and its rows are empty by
        # construction. Returning zeros here would report an unmeasured quota
        # as an empty one.
        return (None, None)
    for_path = [row for row in snap.rows_for_path(path) if row.kind == kind]
    if for_path:
        return (for_path[0].used, for_path[0].limit)
    if fileset:
        named = [row for row in snap.rows if row.kind == kind and row.fileset == fileset]
        if len(named) == 1:
            return (named[0].used, named[0].limit)
    of_kind = [row for row in snap.rows if row.kind == kind]
    if len(of_kind) == 1:
        return (of_kind[0].used, of_kind[0].limit)
    return (None, None)


def _quota_source(root):
    # type: (Root) -> str
    """Which backend produced this root's numbers, for `diff`'s `because`.

    A `because` string has to name the probe rather than restate the label,
    because it is what ``dirscape why`` prints. The previous run's backend is
    not knowable from anywhere else once the run is over, so it is stored.
    """
    for snap in (root.quota, root.inode_quota):
        if snap is not None and snap.source:
            return sanitize(snap.source, limit=_SOURCE_LIMIT)
    return ""


# --------------------------------------------------------------------------
# One root, as recorded
# --------------------------------------------------------------------------


def root_key(root):
    # type: (Root) -> Tuple[str, str]
    """The snapshot key for a live `Root`. Must agree with `RootRecord.key`.

    It exists because `Snapshot.from_roots` used to compute
    `(root.device, root.path)` inline while `RootRecord.key` had its own rule.
    The two drifted the moment pathless roots got a location fallback, and the
    dedupe kept collapsing six allocations into one record while the records
    themselves keyed distinctly. Two definitions of identity is one too many.
    """
    if root.path:
        return (root.device, root.path)
    return ("allocation", str((root.policy or {}).get("allocation_location") or "?"))


class RootRecord(object):
    """One root on one run, reduced to what a diff needs.

    **Keyed on ``(device, path)``, with ``(st_dev, st_ino)`` stored alongside.**
    Both, because either one alone loses a case that matters:

    * keying on the path alone misses a renamed directory, which then reads as
      one root lost and another gained;
    * keying on the inode alone misses a fileset that was deleted and
      recreated, which then reads as no change at all.

    Storing both lets `diff` tell a rename from a replacement, and tell either
    of them from a root that really went away.
    """

    __slots__ = (
        "path",
        "role",
        "device",
        "fileset",
        "identity",
        "reach",
        "reach_reason",
        "allocated",
        "mounted",
        "present",
        "used_bytes",
        "limit_bytes",
        "used_inodes",
        "limit_inodes",
        "stranded",
        "location",
        "sources",
        "quota_source",
        "first_seen",
    )

    def __init__(
        self,
        path,  # type: str
        device="",  # type: str
        role="",  # type: str
        fileset="",  # type: str
        identity=None,  # type: Optional[Tuple[int, int]]
        reach=Reach.UNKNOWN,  # type: str
        reach_reason="",  # type: str
        allocated=None,  # type: Optional[Verdict]
        mounted=None,  # type: Optional[Verdict]
        present=None,  # type: Optional[Verdict]
        used_bytes=None,  # type: Optional[int]
        limit_bytes=None,  # type: Optional[int]
        used_inodes=None,  # type: Optional[int]
        limit_inodes=None,  # type: Optional[int]
        stranded=False,  # type: bool
        location="",  # type: str
        sources=(),  # type: Sequence[str]
        quota_source="",  # type: str
        first_seen=None,  # type: Optional[float]
    ):
        # type: (...) -> None
        self.path = path
        self.device = device
        self.role = role
        self.fileset = fileset
        # Normalised to a 2-tuple of ints here rather than trusted, so a caller
        # that hands over a list, or a JSON payload that carries one, cannot
        # make the identity index miss on a value that is equal but not equal.
        self.identity = (
            (int(identity[0]), int(identity[1]))
            if identity is not None and len(identity) == 2
            else None
        )  # type: Optional[Tuple[int, int]]
        self.reach = reach if reach in _KNOWN_REACH else Reach.UNKNOWN
        self.reach_reason = sanitize(reach_reason, limit=_REASON_LIMIT)
        # Defaulted to NOT_PROBED rather than to a confirmation, so a record
        # built by hand or by an older dirscape claims nothing.
        self.allocated = allocated or unknown(VerdictCategory.NOT_PROBED)
        self.mounted = mounted or unknown(VerdictCategory.NOT_PROBED)
        self.present = present or unknown(VerdictCategory.NOT_PROBED)
        self.used_bytes = used_bytes
        self.limit_bytes = limit_bytes
        self.used_inodes = used_inodes
        self.limit_inodes = limit_inodes
        self.stranded = bool(stranded)
        # Only set for a root with no path: the allocation database's own name
        # for the storage, which is the only stable identity such a root has.
        self.location = location
        self.sources = list(sources)[:_MAX_SOURCES]
        self.quota_source = quota_source
        self.first_seen = first_seen

    @property
    def key(self):
        # type: () -> Tuple[str, str]
        """The identity a diff matches on. See the class docstring.

        A root with no path keys on its allocation location instead. Without
        that fallback every pathless root shares the key `("", "")`, and
        `Snapshot.from_roots` drops all but the first as a duplicate:
        measured, **six allocations collapsed to one record**, so five of them
        were invisible to the state layer for ever and a genuinely new
        allocation could never be reported. The one survivor also churned,
        which is where a phantom "1 change since the baseline" came from on
        every single run.
        """
        if self.path:
            return (self.device, self.path)
        return ("allocation", self.location or "?")

    # `root_key` above is the same rule for a live `Root`; a test asserts the
    # two agree on every root of a real run.

    @classmethod
    def from_root(cls, root, first_seen=None):
        # type: (Root, Optional[float]) -> "RootRecord"
        used_bytes, limit_bytes = _quota_numbers(root.quota, root.path, root.fileset, "blocks")
        used_inodes, limit_inodes = _quota_numbers(
            root.inode_quota, root.path, root.fileset, "files"
        )
        if used_inodes is None and limit_inodes is None:
            # A backend that answers both kinds in one call leaves
            # `inode_quota` unset and puts the "files" rows in `quota`, which is
            # what the GPFS and the site wrapper paths both do. Looking there
            # second costs nothing and is the difference between having inode
            # history and not.
            used_inodes, limit_inodes = _quota_numbers(root.quota, root.path, root.fileset, "files")
        return cls(
            path=root.path,
            device=root.device,
            location=str((root.policy or {}).get("allocation_location") or ""),
            role=root.role,
            fileset=root.fileset,
            identity=root.identity,
            reach=root.reach,
            reach_reason=root.reach_reason,
            allocated=root.allocated,
            mounted=root.mounted,
            present=root.present,
            used_bytes=used_bytes,
            limit_bytes=limit_bytes,
            used_inodes=used_inodes,
            limit_inodes=limit_inodes,
            stranded=bool(root.stranded),
            sources=list(root.sources),
            quota_source=_quota_source(root),
            first_seen=first_seen,
        )

    def to_json(self):
        # type: () -> Dict[str, Any]
        # Absent fields are omitted rather than written as null, which is both
        # smaller and matches `model.Root.to_json`. Every omitted field reloads
        # as its conservative default, so a short record claims less rather
        # than more.
        out = {
            "path": self.path,
            "device": self.device,
            "reach": self.reach,
            "allocated": _reduce_verdict(self.allocated),
            "mounted": _reduce_verdict(self.mounted),
            "present": _reduce_verdict(self.present),
        }  # type: Dict[str, Any]
        if self.role:
            out["role"] = self.role
        if self.fileset:
            out["fileset"] = self.fileset
        if self.identity is not None:
            out["identity"] = list(self.identity)
        if self.reach_reason:
            out["reach_reason"] = self.reach_reason
        if self.used_bytes is not None:
            out["used_bytes"] = self.used_bytes
        if self.limit_bytes is not None:
            out["limit_bytes"] = self.limit_bytes
        if self.used_inodes is not None:
            out["used_inodes"] = self.used_inodes
        if self.limit_inodes is not None:
            out["limit_inodes"] = self.limit_inodes
        if self.stranded:
            out["stranded"] = True
        if self.location:
            # Persisted because it is part of `key` for a pathless root. Left
            # out of the file, the key would be stable within a run and
            # different after a reload, so every allocation would read as new
            # on the next run for ever.
            out["location"] = self.location
        if self.sources:
            out["sources"] = list(self.sources)
        if self.quota_source:
            out["quota_source"] = self.quota_source
        if self.first_seen is not None:
            out["first_seen"] = self.first_seen
        return out

    @classmethod
    def from_json(cls, payload):
        # type: (Dict[str, Any]) -> Optional["RootRecord"]
        """One record, or None when the payload is not one.

        None rather than a raise, and None rather than a partial record: a file
        with one unreadable entry should cost the user that entry and not
        their whole baseline.

        A record needs a path OR a location. Requiring a path discarded every
        allocation-only record on reload, so storage that has no path on this
        node could never acquire a history: it would read as new on every run
        for ever. `key` keys those on their location for the same reason.
        """
        if not isinstance(payload, dict):
            return None
        path = payload.get("path")
        path = path if isinstance(path, str) else ""
        location = payload.get("location")
        location = location if isinstance(location, str) else ""
        if not path and not location:
            return None
        identity = payload.get("identity")
        pair = None  # type: Optional[Tuple[int, int]]
        if (
            isinstance(identity, (list, tuple))
            and len(identity) == 2
            and all(isinstance(part, int) and not isinstance(part, bool) for part in identity)
        ):
            pair = (int(identity[0]), int(identity[1]))
        sources = payload.get("sources")
        return cls(
            path=path,
            device=str(payload.get("device") or ""),
            location=location,
            role=str(payload.get("role") or ""),
            fileset=str(payload.get("fileset") or ""),
            identity=pair,
            reach=_restore_reach(payload.get("reach")),
            reach_reason=str(payload.get("reach_reason") or ""),
            allocated=_restore_verdict(payload.get("allocated")),
            mounted=_restore_verdict(payload.get("mounted")),
            present=_restore_verdict(payload.get("present")),
            used_bytes=_restore_number(payload.get("used_bytes")),
            limit_bytes=_restore_number(payload.get("limit_bytes")),
            used_inodes=_restore_number(payload.get("used_inodes")),
            limit_inodes=_restore_number(payload.get("limit_inodes")),
            stranded=bool(payload.get("stranded")),
            sources=[str(s) for s in sources] if isinstance(sources, list) else (),
            quota_source=str(payload.get("quota_source") or ""),
            first_seen=_restore_time(payload.get("first_seen")),
        )

    def __repr__(self):
        # type: () -> str
        return "RootRecord(%r, reach=%s)" % (self.path, self.reach)


# --------------------------------------------------------------------------
# One run
# --------------------------------------------------------------------------


def current_hostname(environ=None):
    # type: (Optional[Dict[str, str]]) -> str
    """This node's short name, without importing `socket`.

    ``os.uname()`` is in the three module budget this package holds itself to
    and answers the same question `socket.gethostname` does. The environment is
    the fallback rather than the primary, because ``HOSTNAME`` is inherited by
    anything the shell exports and an `srun` environment leak is a measured
    hazard on this cluster.
    """
    uname = getattr(os, "uname", None)
    if uname is not None:
        try:
            return sanitize(uname()[1], limit=128)
        except OSError:  # pragma: no cover - uname does not fail on Linux
            pass
    env = os.environ if environ is None else environ
    return sanitize(env.get("HOSTNAME") or "", limit=128)


def cutoff_for(seconds, now=None):
    # type: (float, Optional[float]) -> float
    """A lookback in seconds as an absolute epoch cutoff.

    `diff` and `Lineage.baseline_for` take an absolute timestamp, deliberately,
    rather than accepting either form and guessing which was meant from its
    magnitude. The caller knows which it has; a function that infers it is
    wrong the one time somebody passes a small epoch.
    """
    base = time.time() if now is None else now
    return base - float(seconds)


def _identity_fields(identity):
    # type: (Any) -> Tuple[str, str, str]
    """``(hostname, node_class, fingerprint)`` from anything that carries them.

    Duck typed rather than importing `dirscape.discover.identity.Identity`,
    which would make `state/` depend on discovery and would stop a test from
    passing a three field stub. A mapping is accepted too, so a payload that
    has already been through JSON works without being rebuilt.
    """
    if identity is None:
        return ("", "", "")
    if isinstance(identity, dict):
        return (
            str(identity.get("hostname") or ""),
            str(identity.get("node_class") or ""),
            str(identity.get("fingerprint") or identity.get("cluster_fingerprint") or ""),
        )
    return (
        str(getattr(identity, "hostname", "") or ""),
        str(getattr(identity, "node_class", "") or ""),
        str(
            getattr(identity, "fingerprint", "")
            or getattr(identity, "cluster_fingerprint", "")
            or ""
        ),
    )


class Snapshot(object):
    """Every root one run discovered, plus where the run was standing.

    ``node_class`` and ``hostname`` are recorded because **mount sets differ by
    node class**: ``/cfs3`` is mounted on login nodes only, so a root absent
    from a compute node run is usually not a root that went away. Without this
    field a diff across the two would report a whole filesystem as removed.

    ``discovery`` is the run's own verdict on whether the sweep completed, and
    it defaults to `NOT_PROBED`. `diff` needs positive evidence that the
    current run LOOKED before it may call an absent root `gone`, and the
    absence of a record cannot supply that evidence. Left unset, no `gone` can
    ever be emitted, which is the right way round: the tool does not get to
    claim a loss until something proves it looked.
    """

    __slots__ = (
        "schema",
        "taken_at",
        "hostname",
        "node_class",
        "cluster_fingerprint",
        "discovery",
        "records",
        "roots",
        "_by_key",
        "_by_identity",
    )

    def __init__(
        self,
        taken_at=None,  # type: Optional[float]
        hostname="",  # type: str
        node_class="",  # type: str
        cluster_fingerprint="",  # type: str
        records=None,  # type: Optional[Sequence[RootRecord]]
        discovery=None,  # type: Optional[Verdict]
        schema=SCHEMA,  # type: int
        roots=None,  # type: Optional[Dict[Tuple[str, str], Root]]
    ):
        # type: (...) -> None
        self.schema = schema
        self.taken_at = time.time() if taken_at is None else float(taken_at)
        self.hostname = sanitize(hostname, limit=128)
        self.node_class = node_class or UNKNOWN_CLASS
        self.cluster_fingerprint = sanitize(cluster_fingerprint, limit=128)
        self.discovery = discovery or unknown(VerdictCategory.NOT_PROBED)
        self.records = list(records or [])  # type: List[RootRecord]
        # Live `Root` objects, by key, when this snapshot was built from them.
        # Never serialised. They are here so `diff` can write its findings back
        # onto the objects the renderer is holding, which is what
        # `Root.labels` and `Root.renamed_from` exist for. A snapshot loaded
        # from disk has none, and `diff` simply does not write back.
        self.roots = dict(roots or {})  # type: Dict[Tuple[str, str], Root]
        self._by_key = None  # type: Optional[Dict[Tuple[str, str], RootRecord]]
        self._by_identity = None  # type: Optional[Dict[Tuple[int, int], RootRecord]]

    # -- construction ------------------------------------------------------

    @classmethod
    def from_roots(
        cls,
        roots,  # type: Sequence[Root]
        identity=None,  # type: Any
        lineage=None,  # type: Optional["Lineage"]
        taken_at=None,  # type: Optional[float]
        discovery=None,  # type: Optional[Verdict]
        hostname=None,  # type: Optional[str]
        node_class="",  # type: str
        cluster_fingerprint="",  # type: str
    ):
        # type: (...) -> "Snapshot"
        """Record a run, taking `first_seen` from the lineage.

        Call this BEFORE appending to the lineage, so "the earliest run that
        saw this root" cannot become "this run".

        Writes `first_seen` back onto each `Root`, because the lineage is the
        only thing that knows it and the renderer holds the `Root`. That keeps
        ``--json`` and the diff from disagreeing about how old a root is.
        """
        stamp = time.time() if taken_at is None else float(taken_at)
        host, klass, fingerprint = _identity_fields(identity)
        if hostname is not None:
            host = hostname
        elif not host:
            host = current_hostname()
        records = []  # type: List[RootRecord]
        live = {}  # type: Dict[Tuple[str, str], Root]
        for root in roots:
            key = root_key(root)
            if key in live:
                # Deduping by identity is discovery's job, and it has the mount
                # table to do it with. Here a repeated key would only make the
                # index ambiguous, so the first one wins and the rest are
                # dropped rather than silently overwriting it.
                continue
            seen = None  # type: Optional[float]
            if lineage is not None:
                seen = lineage.first_seen_for(key, root.identity)
            candidates = [t for t in (seen, root.first_seen) if t is not None]
            # The earliest, never the latest and never a fresh clock read: a
            # root that was already known keeps the date it was first known.
            root.first_seen = min(candidates) if candidates else stamp
            records.append(RootRecord.from_root(root, first_seen=root.first_seen))
            live[key] = root
        return cls(
            taken_at=stamp,
            hostname=host,
            node_class=node_class or klass or UNKNOWN_CLASS,
            cluster_fingerprint=cluster_fingerprint or fingerprint,
            records=records,
            discovery=discovery,
            roots=live,
        )

    # -- lookup ------------------------------------------------------------

    def _index(self):
        # type: () -> None
        if self._by_key is not None:
            return
        by_key = {}  # type: Dict[Tuple[str, str], RootRecord]
        by_identity = {}  # type: Dict[Tuple[int, int], RootRecord]
        for record in self.records:
            by_key.setdefault(record.key, record)
            if record.identity is not None:
                by_identity.setdefault(record.identity, record)
        self._by_key = by_key
        self._by_identity = by_identity

    def record_for(self, device, path):
        # type: (str, str) -> Optional[RootRecord]
        """The record at this device and path.

        Kept for callers that genuinely have a path. It CANNOT find a pathless
        record, because those key on their allocation location instead, so a
        caller holding a record should use `match` rather than taking the
        record apart and putting it back together.
        """
        self._index()
        assert self._by_key is not None
        return self._by_key.get((device, path))

    def match(self, record):
        # type: (RootRecord) -> Optional[RootRecord]
        """The record with the same identity as ``record``, if any.

        Uses `RootRecord.key`, which is the only definition of identity here.
        The diff used to rebuild the key as `(record.device, record.path)`,
        which is a THIRD definition and the reason six allocations were
        reported as `new` and then `unknown` on every single run: pathless
        records key on their location, the rebuilt key was `("", "")`, the
        lookup missed, and the diff concluded it had never seen them.
        """
        self._index()
        assert self._by_key is not None
        return self._by_key.get(record.key)

    def by_identity(self, identity):
        # type: (Optional[Tuple[int, int]]) -> Optional[RootRecord]
        """The record with this ``(st_dev, st_ino)``, if any.

        Returns None for a None identity rather than matching anything. A probe
        that could not `stat` a path produces no identity, and treating that as
        a match would let one failed `stat` claim that two unrelated roots are
        the same object.
        """
        if identity is None:
            return None
        self._index()
        assert self._by_identity is not None
        return self._by_identity.get(tuple(identity))  # type: ignore[arg-type]

    def root_for(self, key):
        # type: (Tuple[str, str]) -> Optional[Root]
        """The live `Root` behind a record, when this snapshot was built from one."""
        return self.roots.get(key)

    # -- wire --------------------------------------------------------------

    def to_json(self):
        # type: () -> Dict[str, Any]
        return {
            "schema": self.schema,
            "taken_at": self.taken_at,
            "hostname": self.hostname,
            "node_class": self.node_class,
            "cluster_fingerprint": self.cluster_fingerprint,
            "discovery": _reduce_verdict(self.discovery),
            "roots": [record.to_json() for record in self.records],
        }

    @classmethod
    def from_json(cls, payload):
        # type: (Any) -> Optional["Snapshot"]
        """One entry, or None when the payload is not a usable one."""
        if not isinstance(payload, dict):
            return None
        taken_at = _restore_time(payload.get("taken_at"))
        if taken_at is None:
            # An entry with no timestamp cannot be ordered, cannot be selected
            # by `--since`, and cannot date a `first_seen`. It is not a usable
            # baseline, so it is dropped rather than carried with a made up
            # time.
            return None
        rows = payload.get("roots")
        records = []  # type: List[RootRecord]
        if isinstance(rows, list):
            for row in rows:
                record = RootRecord.from_json(row)
                if record is not None:
                    records.append(record)
        schema = payload.get("schema")
        return cls(
            taken_at=taken_at,
            hostname=str(payload.get("hostname") or ""),
            node_class=str(payload.get("node_class") or "") or UNKNOWN_CLASS,
            cluster_fingerprint=str(payload.get("cluster_fingerprint") or ""),
            records=records,
            discovery=_restore_verdict(payload.get("discovery")),
            schema=schema if isinstance(schema, int) and not isinstance(schema, bool) else 0,
        )

    def __repr__(self):
        # type: () -> str
        return "Snapshot(%s, %s, %d roots)" % (
            self.hostname or "?",
            self.node_class,
            len(self.records),
        )


# --------------------------------------------------------------------------
# Comparability: is this even the same cluster, and the same vantage point
# --------------------------------------------------------------------------


def fingerprint_mismatch(snapshot, identity):
    # type: (Optional[Snapshot], Any) -> Optional[str]
    """A sentence naming why these two are not comparable, or None.

    ``identity`` is anything carrying a ``fingerprint``, in practice a
    `dirscape.discover.identity.Identity`, and a bare string is accepted too.

    **A missing fingerprint on either side is not a mismatch.** An older
    lineage predates the field, and refusing to diff on that basis would throw
    away the only baseline the user has. Same rule as everywhere else in this
    package: an unanswered question is not a negative answer.
    """
    if snapshot is None:
        return None
    want = identity if isinstance(identity, str) else _identity_fields(identity)[2]
    have = snapshot.cluster_fingerprint or ""
    if not want or not have or want == have:
        return None
    return (
        "the baseline was taken on cluster %s and this run is on cluster %s, so "
        "no root in one is the same root in the other" % (have, want)
    )


def same_vantage(previous, current):
    # type: (Optional[Snapshot], Optional[Snapshot]) -> bool
    """Whether an absent root can be told from an unmounted one.

    Mount visibility is a property of the node, not of the filesystem, so this
    is the gate on `diff`'s `gone` label. True when either:

    * the two runs were on the same host, which is the strongest case and the
      one that keeps `gone` reachable even where the node class could not be
      determined; or
    * both runs classified their node and agree on the class.

    An `unknown` class on either side is not agreement, even against another
    `unknown`: two unclassifiable hosts are not thereby the same kind of host.
    """
    if previous is None or current is None:
        return False
    if previous.hostname and previous.hostname == current.hostname:
        return True
    pair = (previous.node_class or UNKNOWN_CLASS, current.node_class or UNKNOWN_CLASS)
    return pair[0] == pair[1] and UNKNOWN_CLASS not in pair


def vantage_warning(previous, current):
    # type: (Optional[Snapshot], Optional[Snapshot]) -> Optional[str]
    """The sentence to print when the two runs were not standing in the same place."""
    if previous is None or current is None or same_vantage(previous, current):
        return None
    return (
        "the baseline was taken on a %s node (%s) and this run is on a %s node (%s); "
        "mount sets differ by node class, so a root missing here is reported as "
        "unknown rather than as removed"
        % (
            previous.node_class or UNKNOWN_CLASS,
            previous.hostname or "unnamed host",
            current.node_class or UNKNOWN_CLASS,
            current.hostname or "unnamed host",
        )
    )


# --------------------------------------------------------------------------
# Where the file lives
# --------------------------------------------------------------------------


def state_dir(environ=None):
    # type: (Optional[Dict[str, str]]) -> str
    """``$XDG_STATE_HOME/dirscape``, else ``~/.local/state/dirscape``."""
    env = os.environ if environ is None else environ
    root = (env.get("XDG_STATE_HOME") or "").strip()
    if not root:
        home = (env.get("HOME") or "").strip() or os.path.expanduser("~")
        root = os.path.join(home, ".local", "state")
    return os.path.join(root, TOOL)


def _safe_name(fingerprint):
    # type: (str) -> str
    """A filename component that cannot leave the directory.

    A fingerprint may be a hash, but it may also be a cluster name derived from
    a hostname or from device names, so it is foreign text. Anything outside a
    conservative set becomes an underscore: a component containing ``/`` or
    ``..`` would otherwise write outside the state directory.

    The filter is not injective, and that is safe here because the fingerprint
    is also stored INSIDE the file: two names that collapse to one filename are
    caught by `fingerprint_mismatch` on load, which refuses the diff, rather
    than being silently compared against each other.
    """
    keep = "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789._-"
    cleaned = "".join(ch if ch in keep else "_" for ch in (fingerprint or ""))
    cleaned = cleaned.strip("._-")
    return cleaned[:64] or "default"


def default_path(fingerprint, environ=None):
    # type: (str, Optional[Dict[str, str]]) -> str
    """One file per cluster, so two clusters cannot overwrite each other."""
    return os.path.join(state_dir(environ), "%s.json" % (_safe_name(fingerprint),))


# --------------------------------------------------------------------------
# The lineage
# --------------------------------------------------------------------------


class Lineage(object):
    """The retained history for one cluster, oldest entry first.

    Retention is two independent bounds, because one is not enough:

    1. **Count.** The newest `KEEP_RECENT` entries, plus the oldest one as a
       permanent anchor. The anchor is what makes ``--since 90d`` still able to
       answer after thirty daily runs have filled the window.
    2. **Size.** If the file would exceed `MAX_BYTES` anyway, entries are
       dropped from the MIDDLE, at index 1, until it fits. Never the anchor and
       never the recent tail: resolution matters most near the present, so the
       gap opens just after the anchor rather than in the last week. The size
       bound exists for the case the count bound cannot see, which is one run
       that discovered hundreds of roots on a login node mounting several
       clusters.
    """

    __slots__ = ("entries", "path", "schema", "notes")

    def __init__(self, entries=None, path="", schema=SCHEMA):
        # type: (Optional[Sequence[Snapshot]], str, int) -> None
        self.entries = list(entries or [])  # type: List[Snapshot]
        self.path = path
        self.schema = schema
        # Why the lineage is shorter than the user expects, if it is. Read by
        # the renderer; never raised.
        self.notes = []  # type: List[str]

    # -- reading -----------------------------------------------------------

    @classmethod
    def load(cls, path=None, fingerprint="", environ=None):
        # type: (Optional[str], str, Optional[Dict[str, str]]) -> "Lineage"
        """Read the lineage, or return an empty one. Never raises.

        A missing, truncated, or foreign file all mean the same thing: no
        baseline. Salvaging half of a damaged file would be worse than having
        none, because a diff against half a baseline reports changes that did
        not happen.
        """
        where = path or default_path(fingerprint, environ)
        lineage = cls(path=where)
        try:
            with open(where) as handle:
                payload = json.load(handle)
        except (OSError, ValueError):
            # ValueError covers JSONDecodeError, which is a subclass of it and
            # is spelled differently across the versions this has to run on.
            return lineage
        if not isinstance(payload, dict) or payload.get("tool") != TOOL:
            lineage.notes.append("%s is not a dirscape lineage; ignoring it" % (where,))
            return lineage
        schema = payload.get("schema")
        if schema != SCHEMA:
            lineage.notes.append(
                "lineage at %s is schema %r, this dirscape writes %d; starting a new "
                "history rather than misreading the old one" % (where, schema, SCHEMA)
            )
            return lineage
        rows = payload.get("entries")
        if isinstance(rows, list):
            for row in rows:
                entry = Snapshot.from_json(row)
                if entry is not None:
                    lineage.entries.append(entry)
        # Sorted on read rather than trusted, so a hand edited file cannot make
        # `latest()` return something that is not the latest.
        lineage.entries.sort(key=lambda entry: entry.taken_at)
        return lineage

    # -- querying ----------------------------------------------------------

    def latest(self):
        # type: () -> Optional[Snapshot]
        return self.entries[-1] if self.entries else None

    def oldest(self):
        # type: () -> Optional[Snapshot]
        return self.entries[0] if self.entries else None

    def baseline_for(self, cutoff=None):
        # type: (Optional[float]) -> Optional[Snapshot]
        """The entry to diff against, given an absolute epoch cutoff.

        The newest entry that is still at or before the cutoff, so the
        comparison spans the whole window the user asked for rather than only
        since yesterday.

        When nothing is that old the anchor is returned instead: ``--since 90d``
        against a two week old lineage should compare against the oldest thing
        there is, and the caller can see it is younger than the cutoff from its
        own `taken_at`.
        """
        if not self.entries:
            return None
        if cutoff is None:
            return self.entries[-1]
        older = [entry for entry in self.entries if entry.taken_at <= cutoff]
        if older:
            return older[-1]
        return self.entries[0]

    def first_seen_for(self, key, identity=None):
        # type: (Tuple[str, str], Optional[Tuple[int, int]]) -> Optional[float]
        """The earliest run that saw this root, across the WHOLE lineage.

        Scanned over every entry rather than read from the previous one. A root
        whose probe timed out for one run is absent from that entry, and taking
        the previous entry alone would then re-date it as new the moment it came
        back. One gap must be harmless.

        The identity is a second chance, not a first one: a renamed directory
        misses on ``(device, path)`` and hits on ``(st_dev, st_ino)``, and it is
        the same root, so it keeps the date it was first seen.
        """
        best = None  # type: Optional[float]
        for entry in self.entries:
            record = entry.record_for(key[0], key[1])
            if record is None:
                record = entry.by_identity(identity)
            if record is None or record.first_seen is None:
                continue
            if best is None or record.first_seen < best:
                best = record.first_seen
        return best

    # -- writing -----------------------------------------------------------

    def append(self, snapshot):
        # type: (Snapshot) -> None
        """Add a run and enforce both retention bounds."""
        self.entries.append(snapshot)
        self._prune()

    def _prune(self):
        # type: () -> None
        if len(self.entries) > KEEP_RECENT + 1:
            self.entries = [self.entries[0], *self.entries[-KEEP_RECENT:]]
        # Each entry is serialised once, not once per iteration: dropping k
        # entries would otherwise re-serialise the whole file k times, and at
        # 6.3 ms a dump that is a measurable part of the run's 8 s budget.
        sizes = [
            len(json.dumps(entry.to_json(), separators=_COMPACT, sort_keys=True))
            for entry in self.entries
        ]
        # One comma per entry is one more than a list actually needs, which
        # keeps this estimate on the conservative side of the real file.
        total = _ENVELOPE_BYTES + sum(sizes) + len(sizes)
        while total > MAX_BYTES and len(self.entries) > 2:
            total -= sizes.pop(1) + 1
            del self.entries[1]
            self.notes.append("dropped a mid history entry to stay under the size ceiling")

    def to_json(self):
        # type: () -> Dict[str, Any]
        return {
            "tool": TOOL,
            "schema": SCHEMA,
            "entries": [entry.to_json() for entry in self.entries],
        }

    @classmethod
    def from_json(cls, payload):
        # type: (Any) -> "Lineage"
        lineage = cls()
        if not isinstance(payload, dict):
            return lineage
        rows = payload.get("entries")
        if isinstance(rows, list):
            for row in rows:
                entry = Snapshot.from_json(row)
                if entry is not None:
                    lineage.entries.append(entry)
        lineage.entries.sort(key=lambda entry: entry.taken_at)
        return lineage

    def save(self, path=None, fingerprint="", environ=None):
        # type: (Optional[str], str, Optional[Dict[str, str]]) -> bool
        """Write the file atomically. Never raises; returns whether it worked.

        A state file that cannot be written must cost the reader nothing but
        next run's baseline. A full quota, a read only home and a directory
        owned by somebody else are all ordinary on a cluster, and none of them
        is a reason to fail a run that has already done its work. The boolean is
        so the renderer can say "no baseline was saved" rather than leave the
        user to discover it next time.

        Written to a sibling temporary file and then `os.replace`d, so a run
        interrupted mid write leaves the previous lineage intact rather than a
        truncated one. `tempfile` would do the same thing and is not imported,
        to hold this package to `json`, `os` and `time`.

        The directory is created 0o700 and the file 0o600: the lineage lists
        every path a user can reach, with usage figures, which is a map of
        somebody's research that no other account needs.
        """
        where = path or self.path or default_path(fingerprint, environ)
        blob = json.dumps(self.to_json(), separators=_COMPACT, sort_keys=True)
        directory = os.path.dirname(where) or "."
        temporary = "%s.%d.tmp" % (where, os.getpid())
        try:
            if not os.path.isdir(directory):
                os.makedirs(directory, 0o700)
            with open(temporary, "w") as handle:
                handle.write(blob)
            os.chmod(temporary, 0o600)
            os.replace(temporary, where)
        except OSError:
            # `contextlib.suppress` is what ruff's SIM105 asks for here and it
            # is declined on purpose: importing contextlib for one cleanup would
            # widen this package's import surface past the `json`, `os`, `time`
            # that `test_state_py36.py` asserts, and that surface is the
            # deployability claim. A failed cleanup is also genuinely nothing:
            # the temporary file is named after this pid and the next run
            # overwrites it.
            try:  # noqa: SIM105
                os.unlink(temporary)
            except OSError:
                pass
            return False
        self.path = where
        return True

    def __len__(self):
        # type: () -> int
        return len(self.entries)

    def __iter__(self):
        # type: () -> Any
        return iter(self.entries)

    def __repr__(self):
        # type: () -> str
        return "Lineage(%d entries, %r)" % (len(self.entries), self.path)
