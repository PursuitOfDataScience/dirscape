"""The shared vocabulary: what a root is, and how the tool says "I do not know".

Every other module in this package produces or consumes the types here, so this
file is the contract and it is deliberately dependency-free.

Three design rules, each of which exists because a sibling package got it wrong
first:

1. **A verdict has three states, not two.** "Confirmed yes", "confirmed no" and
   "I could not determine" are all representable, and the third one is not an
   error. `nodetop`'s replay restored `probe=True` and so 21 partitions that had
   been *measured* as refusing came back looking merely unchecked (NT-1). The
   fix is that a refusal only counts when it is `durable`.

2. **The wire vocabulary and the display vocabulary are separate.** The category
   constants below go into `--json` and must never change casually; the human
   strings live in `CATEGORY_LABELS`. `nodetop` printed raw enum members beside
   prose in a user-facing column (NT-5) because it had only one of the two.

3. **Anything that came from another program's output is hostile.** A filename
   with an embedded newline forges a table row and an escape sequence executes
   (rapidu RD-6), so `sanitize` runs over every field that originated outside
   this process.

Python 3.6 compatible on purpose, matching rapiDU: the whole deployability
argument for a storage tool is that it runs on the bare ``/usr/bin/python3`` of
an unfamiliar login node, before any conda env exists. That rules out
dataclasses, `from __future__ import annotations`, and the walrus operator.
"""

from typing import Dict, List, Optional, Tuple

__all__ = [
    "VerdictCategory",
    "unavailable_quota",
    "CATEGORY_LABELS",
    "TRANSIENT_CATEGORIES",
    "category_label",
    "Verdict",
    "confirmed",
    "refuted",
    "unknown",
    "Reach",
    "QuotaRow",
    "QuotaSnapshot",
    "SnapshotCopy",
    "Root",
    "sanitize",
]


# --------------------------------------------------------------------------
# Sanitising foreign text
# --------------------------------------------------------------------------

# Control characters are stripped rather than escaped. Escaping would keep the
# bytes and leave every downstream consumer responsible for not re-emitting
# them; stripping makes the string safe once, here.
_CONTROL = frozenset(chr(c) for c in list(range(0x00, 0x20)) + [0x7F])


def sanitize(text, limit=512):
    # type: (Optional[str], int) -> str
    """Make text from another program's stdout/stderr safe to print.

    Removes control characters (a newline forges a table row, an ESC sequence
    executes) and caps the length, because a backend that dumps a 40 KB stack
    trace into `reason` should not be able to blow up a one-line table cell.
    """
    if text is None:
        return ""
    if not isinstance(text, str):
        text = str(text)
    cleaned = "".join(" " if ch in _CONTROL else ch for ch in text)
    # Collapse the runs of spaces the substitution above can create, so a
    # multi-line backend message reads as one sentence instead of a gap.
    cleaned = " ".join(cleaned.split())
    if len(cleaned) > limit:
        cleaned = cleaned[: limit - 3] + "..."
    return cleaned


#: How much of a device string survives `sanitize`. Generous on purpose: a
#: truncated device is not a shorter name for the same thing, it is a key that
#: can collide with another filesystem's.
DEVICE_LIMIT = 1024


# --------------------------------------------------------------------------
# Verdict categories: the wire vocabulary
# --------------------------------------------------------------------------


class VerdictCategory(object):
    """String constants, deliberately not an ``Enum``.

    These tokens are part of the `--json` output, so they are a wire format
    that consumers may switch on. A plain class of strings serialises with no
    encoder, compares cheaply, and cannot accidentally render as
    ``<VerdictCategory.OK: 'OK'>`` in a prose column.
    """

    # Confirmed-good
    OK = "OK"

    # Confirmed-bad, and durable: re-asking will give the same answer.
    ACCESS_DENIED = "ACCESS_DENIED"
    TRAVERSE_ONLY = "TRAVERSE_ONLY"
    NOT_PRESENT = "NOT_PRESENT"
    NOT_MOUNTED_HERE = "NOT_MOUNTED_HERE"
    ALLOCATED_ELSEWHERE = "ALLOCATED_ELSEWHERE"
    NO_QUOTA_BACKEND = "NO_QUOTA_BACKEND"
    NO_QUOTA_ENFORCED = "NO_QUOTA_ENFORCED"
    QUOTA_EXCEEDED = "QUOTA_EXCEEDED"
    STRANDED = "STRANDED"

    # Could-not-determine. Every one of these means "the question went
    # unanswered", and none of them may be rendered as a negative answer.
    BACKEND_FAILED = "BACKEND_FAILED"
    PROBE_TIMEOUT = "PROBE_TIMEOUT"
    NOT_PROBED = "NOT_PROBED"
    NOT_SUPPORTED = "NOT_SUPPORTED"
    PERMISSION_TO_ASK_DENIED = "PERMISSION_TO_ASK_DENIED"
    UNKNOWN = "UNKNOWN"


# A separate human form per category, so the wire token never leaks into a
# prose column. Tested for exhaustiveness: a new category with no label is a
# test failure, not a runtime KeyError in front of a user.
CATEGORY_LABELS = {
    VerdictCategory.OK: "ok",
    VerdictCategory.ACCESS_DENIED: "permission denied",
    VerdictCategory.TRAVERSE_ONLY: "traverse only, cannot list",
    VerdictCategory.NOT_PRESENT: "path does not exist here",
    VerdictCategory.NOT_MOUNTED_HERE: "not mounted on this node",
    VerdictCategory.ALLOCATED_ELSEWHERE: "allocated, but no path on this node",
    VerdictCategory.NO_QUOTA_BACKEND: "no quota backend on this filesystem",
    VerdictCategory.NO_QUOTA_ENFORCED: "filesystem enforces no quota here",
    VerdictCategory.QUOTA_EXCEEDED: "over quota",
    VerdictCategory.STRANDED: "you hold space here but cannot reach it",
    VerdictCategory.BACKEND_FAILED: "the backend failed to answer",
    VerdictCategory.PROBE_TIMEOUT: "the probe timed out",
    VerdictCategory.NOT_PROBED: "not probed",
    VerdictCategory.NOT_SUPPORTED: "not supported here",
    VerdictCategory.PERMISSION_TO_ASK_DENIED: "not allowed to ask",
    VerdictCategory.UNKNOWN: "could not determine",
}


# The formal "I could not determine this" set. A verdict in this set is NOT
# evidence of absence, and `Verdict.durable` is False for every member.
#
# NO_QUOTA_BACKEND is deliberately NOT here: "this filesystem has no quota
# system" is a durable, re-askable-with-the-same-answer fact about the site
# (Brook's NFS home has no quota at all). It still renders the *number* as
# unknown, which is what `QuotaSnapshot.available` carries. Two different
# questions, two different fields.
TRANSIENT_CATEGORIES = frozenset(
    [
        VerdictCategory.BACKEND_FAILED,
        VerdictCategory.PROBE_TIMEOUT,
        VerdictCategory.NOT_PROBED,
        VerdictCategory.NOT_SUPPORTED,
        VerdictCategory.PERMISSION_TO_ASK_DENIED,
        VerdictCategory.UNKNOWN,
    ]
)


def category_label(category):
    # type: (str) -> str
    """The human form of a category, falling back to the token itself.

    The fallback is lossy on purpose rather than raising: an unlabelled
    category is a bug the test suite catches, and it should not take down a
    user's terminal in the meantime.
    """
    return CATEGORY_LABELS.get(category, category.replace("_", " ").lower())


# --------------------------------------------------------------------------
# Verdict
# --------------------------------------------------------------------------


class Verdict(object):
    """One answer, plus how much it is worth.

    ``value`` is the answer when there is one, and ``None`` when there is not.
    Consumers must branch on ``.known`` rather than on ``value is None``, since
    ``False`` is a perfectly good answer.
    """

    __slots__ = ("value", "category", "reason", "source", "elapsed_s")

    def __init__(
        self,
        value,  # type: Optional[bool]
        category=VerdictCategory.UNKNOWN,  # type: str
        reason="",  # type: str
        source="",  # type: str
        elapsed_s=None,  # type: Optional[float]
    ):
        # type: (...) -> None
        self.value = value
        self.category = category
        # Sanitised here, at the boundary, so no caller has to remember to.
        self.reason = sanitize(reason)
        self.source = sanitize(source, limit=128)
        self.elapsed_s = elapsed_s

    @property
    def known(self):
        # type: () -> bool
        """True when this verdict actually answers the question."""
        return self.value is not None and self.category not in TRANSIENT_CATEGORIES

    @property
    def durable(self):
        # type: () -> bool
        """True when re-asking would give the same answer.

        The load-bearing property. Only a durable negative may be presented to
        a user as "no"; a transient one means the question went unanswered.
        """
        return self.category not in TRANSIENT_CATEGORIES

    @property
    def confirmed(self):
        # type: () -> bool
        """True only for a measured yes."""
        return self.value is True and self.category == VerdictCategory.OK

    @property
    def refuted(self):
        # type: () -> bool
        """True only for a measured no. A timeout is not a refusal."""
        return self.value is False and self.durable

    @property
    def label(self):
        # type: () -> str
        return category_label(self.category)

    def glyph(self, ascii_only=False):
        # type: (bool) -> str
        """The one-character rendering: yes, no, or unknown."""
        if self.confirmed:
            return "y" if ascii_only else "✓"
        if self.refuted:
            return "n" if ascii_only else "✗"
        return "?"

    def to_json(self):
        # type: () -> Dict[str, object]
        out = {
            "value": self.value,
            "category": self.category,
            "known": self.known,
            "durable": self.durable,
        }  # type: Dict[str, object]
        if self.reason:
            out["reason"] = self.reason
        if self.source:
            out["source"] = self.source
        if self.elapsed_s is not None:
            out["elapsed_s"] = round(self.elapsed_s, 4)
        return out

    def __repr__(self):
        # type: () -> str
        return "Verdict(%r, %s)" % (self.value, self.category)

    def __eq__(self, other):
        # type: (object) -> bool
        if not isinstance(other, Verdict):
            return NotImplemented
        return self.value == other.value and self.category == other.category

    def __ne__(self, other):
        # type: (object) -> bool
        result = self.__eq__(other)
        if result is NotImplemented:
            return result
        return not result

    def __hash__(self):
        # type: () -> int
        return hash((self.value, self.category))


def confirmed(reason="", source="", elapsed_s=None):
    # type: (str, str, Optional[float]) -> Verdict
    """A measured yes."""
    return Verdict(True, VerdictCategory.OK, reason, source, elapsed_s)


def refuted(category, reason="", source="", elapsed_s=None):
    # type: (str, str, str, Optional[float]) -> Verdict
    """A measured no. The category must be durable, and that is enforced.

    Passing a transient category here is a programming error: it would create a
    verdict that reads as a refusal while claiming to be unanswerable. Better
    to raise in a test than to print "closed" about a directory that merely
    timed out.
    """
    if category in TRANSIENT_CATEGORIES:
        raise ValueError(
            "refuted() needs a durable category, got %r. "
            "Use unknown() for a question that went unanswered." % (category,)
        )
    return Verdict(False, category, reason, source, elapsed_s)


def unknown(category=VerdictCategory.UNKNOWN, reason="", source="", elapsed_s=None):
    # type: (str, str, str, Optional[float]) -> Verdict
    """The question went unanswered. Never renders as a negative answer."""
    return Verdict(None, category, reason, source, elapsed_s)


# --------------------------------------------------------------------------
# Reach: the access tri-state
# --------------------------------------------------------------------------


class Reach(object):
    """How far into a directory you can actually get.

    Three states rather than two, because the middle one is real and common:
    a directory at mode ``2771`` that you are not in the group for gives you
    ``--x``, so you can `cd` through it and use a path you already know, and
    cannot list it. Measured on 3 of 668 entries under one ``/project`` and 13
    of 894 under one ``/project2``, so it is not a corner case.
    """

    LISTABLE = "LISTABLE"  # R and X: you can enumerate it
    TRAVERSE = "TRAVERSE"  # X only: you can pass through, not list
    CLOSED = "CLOSED"  # neither
    UNKNOWN = "UNKNOWN"  # the probe did not complete

    ORDER = (UNKNOWN, CLOSED, TRAVERSE, LISTABLE)

    @staticmethod
    def label(state):
        # type: (str) -> str
        return {
            Reach.LISTABLE: "listable",
            Reach.TRAVERSE: "traverse only",
            Reach.CLOSED: "closed",
            Reach.UNKNOWN: "could not determine",
        }.get(state, state.lower())

    @staticmethod
    def improved(old, new):
        # type: (str, str) -> bool
        """True when reach got strictly better between two conclusive states.

        Returns False whenever either side is UNKNOWN. A run that could not
        determine reach must not be reported as a change in either direction,
        which is the invariant the whole diff layer rests on.
        """
        if Reach.UNKNOWN in (old, new):
            return False
        return Reach.ORDER.index(new) > Reach.ORDER.index(old)

    @staticmethod
    def regressed(old, new):
        # type: (str, str) -> bool
        """True when reach got strictly worse between two conclusive states."""
        if Reach.UNKNOWN in (old, new):
            return False
        return Reach.ORDER.index(new) < Reach.ORDER.index(old)


# --------------------------------------------------------------------------
# Quota
# --------------------------------------------------------------------------


class QuotaRow(object):
    """One used/soft/hard triple, for blocks or for inodes.

    ``device`` and ``fileset`` are kept apart on purpose. They are different
    identities and conflating them is a wrong answer rather than a cosmetic
    one: one GPFS device here is mounted at ``/home``, ``/project``,
    ``/software`` and ``/programs``, and those are four different quotas.
    rapiDU's RD-18 is exactly this bug, so ``fileset`` here is populated from a
    per-path lookup (``mmlsattr -L``) and never inferred from the device.
    """

    __slots__ = (
        "fileset",
        "device",
        "kind",
        "scope",
        "used",
        "soft",
        "hard",
        "grace",
        "mount",
        "mounts",
        "guessed",
        "note",
        "in_doubt",
    )

    def __init__(
        self,
        fileset,  # type: str
        kind,  # type: str
        scope,  # type: str
        used,  # type: Optional[int]
        soft=None,  # type: Optional[int]
        hard=None,  # type: Optional[int]
        grace="",  # type: str
        mount=None,  # type: Optional[str]
        device="",  # type: str
        guessed=False,  # type: bool
        note="",  # type: str
        in_doubt=None,  # type: Optional[int]
    ):
        # type: (...) -> None
        self.fileset = sanitize(fileset, limit=128)
        # `DEVICE_LIMIT`, not 128: a Lustre device is its whole NID list and
        # the filesystem name comes LAST. See `Root.__init__`.
        self.device = sanitize(device or fileset, limit=DEVICE_LIMIT)
        self.kind = kind  # "blocks" | "files"
        self.scope = scope  # "user" | "group" | "fileset" | "project" | ""
        self.used = used
        self.soft = soft
        self.hard = hard
        self.grace = sanitize(grace, limit=64)
        self.mount = mount
        self.mounts = [mount] if mount else []  # type: List[str]
        # True when the mount was inferred from a name rather than published by
        # the backend. An inferred mount is dropped on ambiguity.
        self.guessed = guessed
        self.note = sanitize(note)
        # GPFS publishes `blockInDoubt` / `filesInDoubt`: space that is
        # allocated but not yet accounted to a fileset. It is the honest reason
        # a quota figure and a `du` walk disagree, and the gap is not small.
        # Measured on one home fileset: 831 MiB used against 2.18 GiB in doubt,
        # so a tool that hides this field looks simply wrong to anyone who
        # cross-checks it.
        self.in_doubt = in_doubt

    @property
    def label(self):
        # type: () -> str
        """What to print: ``device:fileset`` wherever both are known.

        Qualified unconditionally, not only on a visible collision. A fileset
        name is unique within a filesystem and not across one: on a login node
        mounting three clusters, ``scratch``, ``home`` and ``software`` are all
        fileset names on more than one device. Qualifying only when a collision
        happens to be visible would make a label depend on the host's mount
        table.
        """
        if self.fileset and self.device and self.fileset != self.device:
            return "%s:%s" % (self.device, self.fileset)
        return self.fileset or self.device

    @property
    def limit(self):
        # type: () -> Optional[int]
        """The number a user is actually stopped by.

        The SMALLER of the two non-zero limits, not simply the hard one. A soft
        limit below the hard limit is the one that starts the grace clock, so
        it is what the user hits first and what a percentage should be measured
        against. rapiDU converged on this after a fileset sitting at 100% of
        its enforced limit was rendered at 40.0%, which is the most misleading
        single number this tool could print.

        Zero means unlimited in every backend here, so zeros are discarded
        rather than treated as a floor of nothing.
        """
        limits = [value for value in (self.soft, self.hard) if value]
        if not limits:
            return None
        return min(limits)

    @property
    def fraction(self):
        # type: () -> Optional[float]
        """Used over limit, or None when there is no limit to divide by.

        Returns None rather than 0.0 for an unlimited row. A zero would render
        as an empty bar, which reads as "plenty of room" and is the same class
        of lie as reporting an unmeasured quota as unlimited.
        """
        lim = self.limit
        if not lim or self.used is None:
            return None
        return float(self.used) / float(lim)

    def to_json(self):
        # type: () -> Dict[str, object]
        return {
            "fileset": self.fileset,
            "device": self.device,
            "label": self.label,
            "kind": self.kind,
            "scope": self.scope,
            "used": self.used,
            "soft": self.soft,
            "hard": self.hard,
            "grace": self.grace,
            "mount": self.mount,
            "mounts": list(self.mounts),
            "guessed": self.guessed,
            "note": self.note,
            "in_doubt": self.in_doubt,
        }


class QuotaSnapshot(object):
    """Quota rows from one backend, plus how much to trust them.

    Two independent doubt channels, copied from rapiDU because collapsing them
    loses information a user needs:

    * ``time_note``  the reading's AGE is suspect (a site wrapper that reports
      a figure refreshed by a cron half an hour ago).
    * ``figure_note``  the NUMBER itself is suspect (``lfs quota`` bracketing
      OSTs it could not reach).

    A reading can be fresh and wrong, or stale and exact, and a single
    "confidence" field cannot say which.
    """

    __slots__ = (
        "source",
        "rows",
        "available",
        "category",
        "reason",
        "taken_at",
        "read_at",
        "time_note",
        "figure_note",
    )

    def __init__(
        self,
        source,  # type: str
        rows=None,  # type: Optional[List[QuotaRow]]
        available=True,  # type: bool
        category=VerdictCategory.OK,  # type: str
        reason="",  # type: str
        taken_at=None,  # type: Optional[float]
        read_at=None,  # type: Optional[float]
        time_note="",  # type: str
        figure_note="",  # type: str
    ):
        # type: (...) -> None
        self.source = sanitize(source, limit=128)
        self.rows = list(rows or [])
        self.available = available
        self.category = category
        self.reason = sanitize(reason)
        self.taken_at = taken_at
        self.read_at = read_at
        self.time_note = sanitize(time_note)
        self.figure_note = sanitize(figure_note)

    @property
    def age_seconds(self):
        # type: () -> Optional[float]
        if self.taken_at is None or self.read_at is None:
            return None
        return max(0.0, self.read_at - self.taken_at)

    def rows_for_path(self, path):
        # type: (str) -> List[QuotaRow]
        """Rows governing a path, longest matching mount first.

        A backend only wins the selection in `read_best` when this returns
        something, which is why it lives on the snapshot: producing rows for
        some other path is not the same as answering the question asked.
        """
        hits = []  # type: List[Tuple[int, QuotaRow]]
        for row in self.rows:
            best = -1
            for mount in row.mounts or ([row.mount] if row.mount else []):
                if not mount:
                    continue
                if path == mount or path.startswith(mount.rstrip("/") + "/"):
                    best = max(best, len(mount))
            if best >= 0:
                hits.append((best, row))
        hits.sort(key=lambda pair: pair[0], reverse=True)
        return [row for _, row in hits]

    def to_json(self):
        # type: () -> Dict[str, object]
        out = {
            "source": self.source,
            "available": self.available,
            "category": self.category,
            "rows": [r.to_json() for r in self.rows],
        }  # type: Dict[str, object]
        if self.reason:
            out["reason"] = self.reason
        if self.age_seconds is not None:
            out["age_seconds"] = round(self.age_seconds, 1)
        if self.time_note:
            out["time_note"] = self.time_note
        if self.figure_note:
            out["figure_note"] = self.figure_note
        return out


def unavailable_quota(
    source,  # type: str
    category,  # type: str
    reason,  # type: str
    taken_at=None,  # type: Optional[float]
    read_at=None,  # type: Optional[float]
    time_note="",  # type: str
    figure_note="",  # type: str
):
    # type: (...) -> QuotaSnapshot
    """A quota reading that did not happen, with the reason attached.

    The only way to construct an empty snapshot, so there is no path through
    the code where "no rows" silently means "no usage".

    It still accepts the timing and doubt channels, because a FAILED read of a
    cached report is not timeless: a site wrapper that reads a file refreshed
    by a half-hourly cron can fail to parse while the age of what it tried to
    parse remains a known and useful fact. Dropping it would make "the report
    is stale" and "the report is unreadable" look identical.
    """
    return QuotaSnapshot(
        source,
        rows=[],
        available=False,
        category=category,
        reason=reason,
        taken_at=taken_at,
        read_at=read_at,
        time_note=time_note,
        figure_note=figure_note,
    )


# --------------------------------------------------------------------------
# SnapshotCopy: one read-only copy of a path, as of some past moment
# --------------------------------------------------------------------------


class SnapshotCopy(object):
    """One filesystem snapshot, and where THIS path is inside it.

    Not to be confused with `state.Snapshot`, which is this tool's own
    memory of a previous run. The collision is unfortunate and the words are
    both correct: the one here is the filesystem's read-only copy of your
    data, and it is the thing a user reaches for after `rm -rf`.

    ``taken_at`` is parsed from the snapshot's NAME and never from its
    metadata. Measured on GPFS here: every snapshot directory under
    ``/gpfs/meadow3/cap/.snapshots`` stats as ``mtime 2021-08-04``, which is
    the fileset's creation date, identical for a copy taken this morning and
    one taken four weeks ago. Reading mtime would therefore date every
    snapshot to the same day in 2021, so a name that does not parse leaves
    this ``None`` rather than borrowing a number that is wrong.
    """

    __slots__ = ("name", "path", "taken_at")

    def __init__(self, name, path, taken_at=None):
        # type: (str, str, Optional[float]) -> None
        self.name = sanitize(name, limit=256)
        self.path = sanitize(path, limit=4096)
        self.taken_at = taken_at

    def to_json(self):
        # type: () -> Dict[str, object]
        out = {"name": self.name, "path": self.path}  # type: Dict[str, object]
        if self.taken_at is not None:
            out["taken_at"] = self.taken_at
        return out

    def __repr__(self):
        # type: () -> str
        return "SnapshotCopy(%r)" % (self.path,)


# --------------------------------------------------------------------------
# Root: the thing this tool is about
# --------------------------------------------------------------------------


class Root(object):
    """One storage location, and everything known about your relationship to it.

    The four axes are independent and all four are needed, which is the single
    most important thing this tool gets right and the site wrappers do not:

    * ``allocated``  an allocation database says you have space here
    * ``mounted``    it is mounted on the node you are standing on
    * ``present``    the path exists and can be stat'd
    * ``reach``      what you can actually do (the `Reach` tri-state)

    A row reading "allocated yes, mounted no" is not an error. It is the most
    useful thing the tool can tell a confused user, and on this site it is the
    true state of ``/cfs3`` from a compute node and of ``/cfs4`` from anywhere.
    """

    __slots__ = (
        "path",
        "role",
        "device",
        "fstype",
        "fileset",
        "identity",
        "allocated",
        "mounted",
        "present",
        "reach",
        "reach_reason",
        "writable",
        "quota",
        "inode_quota",
        "symlink_target",
        "crosses_boundary",
        "policy",
        "sources",
        "notes",
        "first_seen",
        "labels",
        "stranded",
        "renamed_from",
        "snapshots",
        "recoverable",
    )

    def __init__(self, path, role="", device="", fstype=""):
        # type: (str, str, str, str) -> None
        # Sanitised here for the same reason Verdict and QuotaRow are: a path
        # can arrive from another program's stdout (a quota row, a fileset
        # listing, a config file), and an ESC or a newline inside one forges a
        # table row exactly as rapiDU's RD-6 filename did. Doing it at
        # construction means no renderer has to remember to.
        self.path = sanitize(path, limit=4096)
        # "home" | "project" | "scratch" | "software" | "dataset" | "archive"
        # | "local" | "" . Advisory only, derived by heuristic plus site config,
        # and never used to decide access.
        self.role = sanitize(role, limit=32)
        # **Long enough for a Lustre device, whose identity is at the END.**
        # Measured on ACME: `/home` is mounted from
        # `192.0.2.185@o2ib26,192.0.2.190@o2ib26:...:192.0.2.193@o2ib26:/acorn/home`,
        # eight NIDs and 162 characters, and `/lus/acorn` from the same list
        # ending `:/acorn`. At 128 both were cut to one identical prefix plus
        # `...`, so two mounts became one device key and the part saying which
        # filesystem it was, the only part that differs, was the part lost.
        self.device = sanitize(device, limit=DEVICE_LIMIT)
        self.fstype = sanitize(fstype, limit=32)
        self.fileset = ""
        # (st_dev, st_ino). The dedupe key, because one device here is mounted
        # at four paths and the same tree is reachable under two more.
        self.identity = None  # type: Optional[Tuple[int, int]]

        self.allocated = unknown(VerdictCategory.NOT_PROBED)
        self.mounted = unknown(VerdictCategory.NOT_PROBED)
        self.present = unknown(VerdictCategory.NOT_PROBED)
        self.reach = Reach.UNKNOWN
        self.reach_reason = ""
        self.writable = unknown(VerdictCategory.NOT_PROBED)

        self.quota = None  # type: Optional[QuotaSnapshot]
        self.inode_quota = None  # type: Optional[QuotaSnapshot]

        self.symlink_target = None  # type: Optional[str]
        # True when following this path leaves the quota scope its parent
        # implies, which is how ~/.cache can silently bill against /project.
        self.crosses_boundary = False

        self.policy = {}  # type: Dict[str, object]
        # Which discovery probes found this root. More than one is normal and
        # is itself evidence; exactly one is worth noting in `why`.
        self.sources = []  # type: List[str]
        self.notes = []  # type: List[str]
        self.first_seen = None  # type: Optional[float]
        # Delta labels from the diff layer: new, opened, closed, grew, ...
        self.labels = []  # type: List[str]
        # True when the user holds blocks in a scope they cannot reach. Set by
        # the quota layer, which is the only thing that can see it: a GPFS
        # per-device quota listing enumerates every fileset the user has usage
        # in, INCLUDING ones they have no group for. Measured on one account:
        # five such filesets holding 11.7 GB, 6.8 GB and three smaller. That is
        # storage being charged for and no longer reachable, and no other tool
        # reports it.
        self.stranded = False
        # Set by the diff layer when this root's (st_dev, st_ino) was seen at a
        # different path last run. Carried so a move is reported as one move
        # rather than as a loss plus an arrival, since claiming a loss for data
        # that is visible elsewhere is the false alarm the whole design avoids.
        self.renamed_from = None  # type: Optional[str]
        # Read-only copies of THIS path that the filesystem is keeping, newest
        # first. Empty is not the same as "no backups": see `recoverable`.
        self.snapshots = []  # type: List[SnapshotCopy]
        # Whether a copy of this path can be read back today. Confirmed only
        # when a copy was opened; refuted only when the filesystem exposes a
        # snapshot directory and it holds nothing for this path; unknown when
        # no snapshot mechanism was found, because a site can back up to tape
        # without exposing one and silence must not be rendered as "no".
        self.recoverable = unknown(VerdictCategory.NOT_PROBED)

    @property
    def reachable(self):
        # type: () -> bool
        """True unless reach was DURABLY refused.

        UNKNOWN counts as reachable-until-shown-otherwise on purpose. Treating
        an unanswered question as a refusal is the NT-1 failure inverted, and it
        would make a timing-out mount disappear from the atlas.
        """
        return self.reach != Reach.CLOSED

    @property
    def elsewhere(self):
        # type: () -> bool
        """Allocated to you, but with no usable path on this node."""
        return self.allocated.confirmed and self.mounted.refuted

    def add_note(self, note):
        # type: (str) -> None
        clean = sanitize(note)
        if clean and clean not in self.notes:
            self.notes.append(clean)

    def add_source(self, source):
        # type: (str) -> None
        if source and source not in self.sources:
            self.sources.append(source)

    def to_json(self):
        # type: () -> Dict[str, object]
        out = {
            "path": self.path,
            "role": self.role,
            "device": self.device,
            "fstype": self.fstype,
            "fileset": self.fileset,
            "allocated": self.allocated.to_json(),
            "mounted": self.mounted.to_json(),
            "present": self.present.to_json(),
            "reach": self.reach,
            "reach_label": Reach.label(self.reach),
            "writable": self.writable.to_json(),
            "elsewhere": self.elsewhere,
            "sources": list(self.sources),
            "labels": list(self.labels),
        }  # type: Dict[str, object]
        if self.identity is not None:
            out["identity"] = list(self.identity)
        if self.reach_reason:
            out["reach_reason"] = self.reach_reason
        if self.quota is not None:
            out["quota"] = self.quota.to_json()
        if self.inode_quota is not None:
            out["inode_quota"] = self.inode_quota.to_json()
        if self.symlink_target:
            out["symlink_target"] = self.symlink_target
            out["crosses_boundary"] = self.crosses_boundary
        if self.policy:
            out["policy"] = dict(self.policy)
        if self.notes:
            out["notes"] = list(self.notes)
        if self.first_seen is not None:
            out["first_seen"] = self.first_seen
        if self.stranded:
            out["stranded"] = True
        if self.renamed_from:
            out["renamed_from"] = self.renamed_from
        out["recoverable"] = self.recoverable.to_json()
        if self.snapshots:
            out["snapshots"] = [copy.to_json() for copy in self.snapshots]
        return out

    def __repr__(self):
        # type: () -> str
        return "Root(%r, reach=%s)" % (self.path, self.reach)
