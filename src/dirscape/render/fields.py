"""One cell each, and ONE place the unknown mark is decided.

Every view in this package composes its rows out of the functions here. That is
deliberate: the rule this whole package exists to enforce is that **the
renderer may not print a value it was not given**, and a rule implemented in
six places is a rule with six chances to be wrong.

So there is no code path here from an unknown to a number, a blank or a zero:

* a root with no quota backend gets the unknown mark, never "no limit"
* a `QuotaRow.fraction` of None gets the unknown mark and NO bar, because an
  empty bar reads as plenty of room
* a `Verdict` that is neither confirmed nor refuted gets the unknown mark,
  never "no"
* a `VerdictCategory` never reaches a prose column: `category_label()` does,
  which is `nodetop`'s NT-5 (it printed `ACCOUNTS_UNTRIED` beside English)

The two verdict bridges (`list_verdict`, `read_verdict`) and `RunMeta` are the
only things here that look like model code. They are noted in the report as
belonging in the shared contract; they live here for now so that no view
re-derives them differently.
"""

import time
from typing import Dict, List, Optional, Sequence, Tuple

from ..model import (
    TRANSIENT_CATEGORIES,
    QuotaRow,
    QuotaSnapshot,
    Reach,
    Root,
    Verdict,
    VerdictCategory,
    category_label,
    confirmed,
    refuted,
    sanitize,
    unknown,
)
from .style import Style, bar

__all__ = [
    "UNKNOWN",
    "CHANGE_LABELS",
    "CHANGE_ORDER",
    "RunMeta",
    "safe",
    "human_bytes",
    "human_count",
    "human_duration",
    "date_text",
    "age_phrase",
    "pick_row",
    "quota_cell",
    "inode_cell",
    "where_cell",
    "reach_cell",
    "role_cell",
    "policy_cell",
    "merged_policy",
    "policy_glyph",
    "verdict_glyph",
    "list_verdict",
    "read_verdict",
    "quota_verdict",
    "snapshot_verdict",
    "unmeasured",
    "trouble",
    "in_doubt_of",
    "billed_to",
    "change_fields",
    "device_count",
]


#: The mark for a question that went unanswered. One character, the same in
#: ASCII and Unicode, so "grep for the question marks" is always right.
UNKNOWN = "?"


#: The delta vocabulary the state layer emits.
CHANGE_LABELS = (
    "new",
    "opened",
    "closed",
    "grew",
    "shrank",
    "stranded",
    "gone",
    "unknown",
)

#: Display order, by how much it should worry the reader, which is NOT the
#: order above. `stranded` leads because it is the one no other tool reports:
#: space you are charged for in a scope you can no longer reach. `unknown` is
#: last because an unanswered question is not an alarm.
CHANGE_ORDER = (
    "stranded",
    "gone",
    "closed",
    "shrank",
    "new",
    "opened",
    "grew",
    "unknown",
)


def safe(text, limit=200):
    # type: (Optional[object], int) -> str
    """Sanitise at the print boundary.

    `Root`, `Verdict`, `QuotaRow` and `QuotaSnapshot` all sanitise at
    construction, so this is not load bearing for their fields. It covers what
    the model does not own: `Root.policy` values and `Root.labels` entries put
    there by a site plugin, and change records handed in from the state layer.
    A newline in one of those forges a table row exactly as rapiDU's RD-6
    filename did.
    """
    if text is None:
        return ""
    return sanitize(text if isinstance(text, str) else str(text), limit=limit)


# --------------------------------------------------------------------------
# numbers
# --------------------------------------------------------------------------

_BYTE_UNITS = ("B", "K", "M", "G", "T", "P", "E")
_BYTE_UNITS_LONG = ("B", "KiB", "MiB", "GiB", "TiB", "PiB", "EiB")


def human_bytes(n, compact=True):
    # type: (Optional[int], bool) -> str
    """Bytes in binary multiples, or the unknown mark.

    ``compact`` gives `du -h`'s single letter form (``831M``, ``2.0G``), which
    is what every reader on this cluster already reads, and which fits a table
    column. The letters mean binary multiples, exactly as `du -h`'s do.

    None is NOT zero. A caller with no measurement gets the unknown mark, never
    ``0 B``.
    """
    if n is None:
        return UNKNOWN
    units = _BYTE_UNITS if compact else _BYTE_UNITS_LONG
    negative = n < 0
    value = float(abs(n))
    for index, unit in enumerate(units):
        places = 0 if index == 0 else 1
        # The ROUNDED value is compared, not the raw one. Otherwise a figure
        # just under a boundary passes the test and is then rounded up across
        # it, so one byte under a GiB prints as "1024.0 MiB": 1024 of a unit
        # that has a larger sibling, which no formatter should emit. `du -h`
        # and `numfmt --to=iec` both print 1.0G there.
        if index == len(units) - 1 or round(value, places) < 1024.0:
            if index == 0:
                text = "%d%s" % (int(value), unit if not compact else "B")
            elif value >= 10 or not compact:
                text = ("%.0f%s" if compact else "%.1f %s") % (value, unit)
            else:
                text = "%.1f%s" % (value, unit)
            return "-" + text if negative else text
        value /= 1024.0
    return UNKNOWN  # pragma: no cover - the loop always returns


_COUNT_UNITS = ("", "k", "M", "G", "T")


def human_count(n, compact=True):
    # type: (Optional[int], bool) -> str
    """A count, or the unknown mark.

    DECIMAL steps, unlike :func:`human_bytes`: an inode count is not a binary
    quantity and a limit of 1,000,000 files is written as a round million in
    every quota table there is, so scaling it by 1024 would print ``976k``
    for a limit the backend called a million.
    """
    if n is None:
        return UNKNOWN
    if not compact:
        return "{:,}".format(n)
    negative = n < 0
    value = float(abs(n))
    for index, unit in enumerate(_COUNT_UNITS):
        if index == len(_COUNT_UNITS) - 1 or round(value, 1) < 1000.0:
            if not unit:
                text = "%d" % (int(value),)
            elif value >= 10:
                text = "%.0f%s" % (value, unit)
            else:
                text = "%.1f%s" % (value, unit)
            return "-" + text if negative else text
        value /= 1000.0
    return UNKNOWN  # pragma: no cover - the loop always returns


def human_duration(seconds):
    # type: (Optional[float]) -> str
    """A short duration, or the unknown mark."""
    if seconds is None:
        return UNKNOWN
    value = float(seconds)
    if value < 0:
        value = 0.0
    if value < 1.0:
        return "%dms" % (int(round(value * 1000)),)
    if value < 60.0:
        return "%.1fs" % (value,)
    if value < 3600.0:
        return "%dm" % (int(value // 60),)
    if value < 86400.0:
        return "%dh" % (int(value // 3600),)
    return "%dd" % (int(value // 86400),)


def date_text(epoch):
    # type: (Optional[float]) -> str
    """A calendar date, or the unknown mark.

    Local time, because it is read by a person sitting in front of the
    terminal. Formatting a timestamp the caller supplied is not the same as
    reading the clock, which nothing in this package does.
    """
    if epoch is None:
        return UNKNOWN
    try:
        return time.strftime("%Y-%m-%d", time.localtime(float(epoch)))
    except (ValueError, OSError, OverflowError):
        return UNKNOWN


def age_phrase(then, now):
    # type: (Optional[float], Optional[float]) -> str
    """How long ago ``then`` was, or the unknown mark.

    Both sides are required. A caller that supplied a baseline date but no
    "now" has told us when, not how long ago, and inventing the second from
    the clock is exactly the fabrication this package refuses.
    """
    if then is None or now is None:
        return UNKNOWN
    delta = float(now) - float(then)
    if delta < 0:
        # A baseline in the future is a clock disagreement between two nodes,
        # not an age. Saying so is more useful than a negative number.
        return "dated ahead of this node"
    if delta < 60.0:
        return "under a minute ago"
    return "%s ago" % (human_duration(delta),)


# --------------------------------------------------------------------------
# run metadata
# --------------------------------------------------------------------------


class RunMeta(object):
    """What the header says about the run, all of it optional.

    Optional is the point. The renderer reads nothing from the clock, the
    hostname or the filesystem, so a field the caller did not pass renders as
    the unknown mark. That makes every view a pure function of its arguments,
    which is also why the tests need no fixtures on disk.

    `of` accepts a `RunMeta`, a dict, None, or any object carrying
    `hostname` / `node_class` / `cluster` / `user` attributes, which is what
    `discover.identity.Identity` is. Duck typed rather than imported, so the
    render layer keeps its one-way dependency on `model` alone.
    """

    __slots__ = (
        "tool",
        "version",
        "host",
        "node_class",
        "cluster",
        "user",
        "devices",
        "elapsed_s",
        "baseline_at",
        "baseline_label",
        "now",
        "source",
    )

    def __init__(
        self,
        tool="dirscape",  # type: str
        version=None,  # type: Optional[str]
        host=None,  # type: Optional[str]
        node_class=None,  # type: Optional[str]
        cluster=None,  # type: Optional[str]
        user=None,  # type: Optional[str]
        devices=None,  # type: Optional[int]
        elapsed_s=None,  # type: Optional[float]
        baseline_at=None,  # type: Optional[float]
        baseline_label=None,  # type: Optional[str]
        now=None,  # type: Optional[float]
        source=None,  # type: Optional[str]
    ):
        # type: (...) -> None
        self.tool = tool or "dirscape"
        self.version = version
        self.host = host
        self.node_class = node_class
        self.cluster = cluster
        self.user = user
        self.devices = devices
        self.elapsed_s = elapsed_s
        self.baseline_at = baseline_at
        self.baseline_label = baseline_label
        self.now = now
        self.source = source

    @classmethod
    def of(cls, obj):
        # type: (object) -> "RunMeta"
        if obj is None:
            return cls()
        if isinstance(obj, cls):
            return obj
        if isinstance(obj, dict):
            fields = {}  # type: Dict[str, object]
            for name in cls.__slots__:
                if name in obj:
                    fields[name] = obj[name]
            # `hostname` is what Identity and the snapshot both call it.
            if "host" not in fields and "hostname" in obj:
                fields["host"] = obj["hostname"]
            return cls(**fields)  # type: ignore[arg-type]
        return cls(
            host=getattr(obj, "hostname", None) or getattr(obj, "host", None),
            node_class=getattr(obj, "node_class", None),
            cluster=getattr(obj, "cluster", None),
            user=getattr(obj, "user", None),
        )


def device_count(roots, meta=None):
    # type: (Sequence[Root], Optional[RunMeta]) -> str
    """How many distinct devices these roots sit on, as a phrase.

    Counted from what the caller handed over, and `?` when not one root names
    a device. A count of zero is never printed as a number, because zero
    devices with roots on them is not a measurement, it is a gap.
    """
    if meta is not None and meta.devices is not None:
        # The mount table sees devices carrying no discovered root, so a
        # caller that counted there knows better than this function can.
        return "%d device%s" % (meta.devices, "" if meta.devices == 1 else "s")
    named = {r.device for r in roots if r.device}
    blank = sum(1 for r in roots if not r.device)
    if not named:
        return "%s devices" % (UNKNOWN,)
    text = "%d device%s" % (len(named), "" if len(named) == 1 else "s")
    if blank:
        return "%s (%s undetermined)" % (text, blank)
    return text


# --------------------------------------------------------------------------
# quota cells
# --------------------------------------------------------------------------


def pick_row(snapshot, path, kind="blocks"):
    # type: (Optional[QuotaSnapshot], str, str) -> Tuple[Optional[QuotaRow], str, str]
    """The row governing ``path``: ``(row, how, why)``.

    ``how`` is ``"published"`` when the backend itself said this row covers
    this path, ``"inferred"`` when we attributed it, and ``""`` when there is
    no row to show. An inferred attribution is marked in every view and
    recorded in `--json` as a caveat, because attributing a figure is not the
    same as measuring one.
    """
    if snapshot is None:
        return None, "", "no quota reading was taken"
    if not snapshot.available:
        return None, "", snapshot.reason or category_label(snapshot.category)
    rows = [r for r in snapshot.rows_for_path(path) if r.kind == kind]
    if rows:
        return rows[0], "published", ""
    same_kind = [r for r in snapshot.rows if r.kind == kind]
    if len(same_kind) == 1:
        return (
            same_kind[0],
            "inferred",
            "the backend published no mount for this row, and it returned only this one",
        )
    if same_kind:
        return (
            None,
            "",
            "%d rows came back and the backend published no mount for this path"
            % (len(same_kind),),
        )
    return None, "", "the backend answered without a row for this path"


def _limit_text(row, formatter):
    # type: (QuotaRow, object) -> str
    """The limit, "no limit" for an explicit zero, unknown when unreported.

    The two are different facts and the model collapses them in `limit`, which
    returns None for both. A backend that printed ``0`` said "no limit is
    enforced here"; a backend that printed nothing said nothing. The word
    "unlimited" appears nowhere in this package: it is the word that carries
    the lie when a quota was simply not measured.
    """
    if row.limit is not None:
        return formatter(row.limit)  # type: ignore[operator]
    if row.soft == 0 or row.hard == 0:
        return "no limit"
    return UNKNOWN


def _figure_cell(root, snapshot, kind, formatter, style, bar_size, show_bar):
    # type: (Root, Optional[QuotaSnapshot], str, object, Style, int, bool) -> Tuple[str, str]
    row, how, why = pick_row(snapshot, root.path, kind)
    if row is None:
        # The whole point of the package: no figure, no invented figure. The
        # caveat is empty rather than carrying `why`, because a caveat
        # qualifies a figure that WAS shown. Why a figure is missing is a
        # different question and `trouble()` answers it, in the footer, once
        # per root instead of once per column.
        return UNKNOWN, ""
    g = style.g
    used = formatter(row.used)  # type: ignore[operator]
    text = "%s / %s" % (used, _limit_text(row, formatter))
    caveats = []  # type: List[str]
    if how == "inferred":
        # `~` before the figure, so a reader scanning the column sees which
        # numbers were attributed rather than published.
        text = "~" + text
        caveats.append(why)
    if row.guessed:
        caveats.append("the mount for this row was inferred from its name")
    fraction = row.fraction
    if fraction is not None:
        doubt_share = None  # type: Optional[float]
        limit = row.limit
        if row.in_doubt and limit:
            doubt_share = float(row.in_doubt) / float(limit)
        if show_bar:
            text += "  " + bar(fraction, bar_size, style, doubt=doubt_share)
        text += " " + style.tint("%d%%" % (int(round(fraction * 100)),), fraction)
        if fraction >= 1.0:
            text += style.bad(g.warn)
    if row.in_doubt:
        # The marker is the same shade the bar draws the doubt segment in, so
        # the picture and the mark are one statement.
        text += style.muted(g.doubt)
        caveats.append(
            "%s is allocated and not yet accounted for, so a du walk will disagree"
            % (formatter(row.in_doubt),)  # type: ignore[operator]
        )
    if row.note:
        caveats.append(row.note)
    return text, "; ".join(c for c in caveats if c)


def quota_cell(root, style=None, bar_size=8, show_bar=True):
    # type: (Root, Optional[Style], int, bool) -> Tuple[str, str]
    """``used / limit`` with a bar, or the unknown mark. Plus any caveat."""
    style = style or Style()
    return _figure_cell(root, root.quota, "blocks", human_bytes, style, bar_size, show_bar)


def inode_cell(root, style=None):
    # type: (Root, Optional[Style]) -> Tuple[str, str]
    """``files / limit``, no bar: the bytes column already carries the picture."""
    style = style or Style()
    return _figure_cell(root, root.inode_quota, "files", human_count, style, 0, False)


def in_doubt_of(root):
    # type: (Root) -> Optional[int]
    """Bytes the backend says are allocated but not yet accounted for."""
    row, _, _ = pick_row(root.quota, root.path, "blocks")
    return row.in_doubt if row is not None else None


def unmeasured(root):
    # type: (Root) -> bool
    """True when this root's usage figure could not be measured."""
    row, _, _ = pick_row(root.quota, root.path, "blocks")
    return row is None or row.used is None


def trouble(root):
    # type: (Root) -> str
    """Why a root's figure is missing or suspect, in one sentence.

    Prose, so it goes through `category_label` and never shows a wire token.
    """
    snap = root.quota
    if snap is None:
        return "no quota reading was taken"
    parts = []  # type: List[str]
    row, _, why = pick_row(snap, root.path, "blocks")
    if row is None:
        parts.append(why or category_label(snap.category))
    elif row.used is None:
        parts.append("the row for this path carries no usage figure")
    if snap.time_note:
        parts.append(snap.time_note)
    if snap.figure_note:
        parts.append(snap.figure_note)
    age = snap.age_seconds
    if age is not None:
        # A FAILED read still has an age worth showing: a wrapper that cannot
        # parse a cached report knows how old the report was, and "stale" and
        # "unreadable" are different problems.
        parts.append("the reading it used was %s old" % (human_duration(age),))
    if snap.source:
        parts.append("from %s" % (snap.source,))
    return "; ".join(parts) or category_label(snap.category)


# --------------------------------------------------------------------------
# verdict cells
# --------------------------------------------------------------------------


def verdict_glyph(verdict, glyphs):
    # type: (Optional[Verdict], object) -> str
    """`Verdict.glyph`, with None treated as the question never being asked."""
    if verdict is None:
        return UNKNOWN
    return verdict.glyph(ascii_only=not getattr(glyphs, "unicode", True))


def list_verdict(root):
    # type: (Root) -> Verdict
    """ "Can I enumerate this directory", as a Verdict.

    A bridge, because `Reach` is a four state string and every view needs the
    same answer in the same shape. Traverse-only is a MEASURED no to listing,
    which is why it comes back refuted rather than unknown.
    """
    if root.reach == Reach.LISTABLE:
        return confirmed(root.reach_reason, "reach probe")
    if root.reach == Reach.TRAVERSE:
        return refuted(VerdictCategory.TRAVERSE_ONLY, root.reach_reason, "reach probe")
    if root.reach == Reach.CLOSED:
        return refuted(VerdictCategory.ACCESS_DENIED, root.reach_reason, "reach probe")
    category = VerdictCategory.UNKNOWN if root.reach_reason else VerdictCategory.NOT_PROBED
    return unknown(category, root.reach_reason, "reach probe")


def read_verdict(root):
    # type: (Root) -> Verdict
    """ "Can I read a FILE inside this directory", as a Verdict.

    Deduced from a refusal and never from a success. A directory you cannot
    enter holds nothing you can read, which is sound; a directory you can list
    says nothing about the mode of the files in it, and this tool does not open
    one. So the column is honestly unknown for most roots, and a column of
    question marks is a statement rather than a defect.
    """
    if root.reach == Reach.CLOSED:
        return refuted(
            VerdictCategory.ACCESS_DENIED,
            root.reach_reason or "the directory itself cannot be entered",
            "deduction",
        )
    return unknown(VerdictCategory.NOT_PROBED, "no file inside is opened by this tool", "deduction")


def snapshot_verdict(snapshot, path, kind="blocks"):
    # type: (Optional[QuotaSnapshot], str, str) -> Verdict
    """ "Is a figure of this kind available for this path", as a Verdict.

    A snapshot carries a category that is NOT a Verdict, and every view and the
    JSON index need the answer in the same shape. The branch that matters is
    the last one: a durable category (no quota backend, no quota enforced) is a
    measured no, and a transient one is an unanswered question, and the model's
    `refuted()` refuses to blur them.
    """
    if snapshot is None:
        return unknown(VerdictCategory.NOT_PROBED, "", "quota layer")
    if snapshot.available:
        row, how, why = pick_row(snapshot, path, kind)
        if row is not None and how == "published":
            return confirmed(snapshot.reason, snapshot.source)
        return unknown(VerdictCategory.UNKNOWN, why, snapshot.source)
    if snapshot.category in TRANSIENT_CATEGORIES:
        return unknown(snapshot.category, snapshot.reason, snapshot.source)
    return refuted(snapshot.category, snapshot.reason, snapshot.source)


def quota_verdict(root):
    # type: (Root) -> Verdict
    """ "Is a block quota figure available for this path", as a Verdict."""
    return snapshot_verdict(root.quota, root.path, "blocks")


def where_cell(root, style=None):
    # type: (Root, Optional[Style]) -> str
    """``here`` / ``ELSEWHERE`` / ``not here`` / ``?``.

    The column exists because the site quota wrapper reports `/cfs3` and
    `/cfs4` from a node where neither path exists, and "allocated, and not
    mounted here" is the most useful thing this tool says.

    ``ELSEWHERE`` shouts because it is the surprising answer, and it claims an
    allocation: it is only used when one was confirmed. A mount refused with no
    confirmed allocation is ``not here``, which is a smaller and true claim.
    """
    style = style or Style()
    if root.mounted.confirmed:
        return style.ok("here")
    if root.elsewhere:
        return style.warn("ELSEWHERE")
    if root.mounted.refuted:
        return style.warn("not here")
    return UNKNOWN


def reach_cell(root, style=None):
    # type: (Root, Optional[Style]) -> str
    """POSIX-shaped reach: ``rwx``, ``r-x``, ``r?x``, ``-?x!``, ``---``, ``?``.

    Three independent slots, each of which can say "I do not know". So a
    listable directory whose write bit was never probed is ``r?x`` and not
    ``r-x``: the difference between "you cannot write here" and "nobody
    checked" is the difference this tool is for.

    Traverse-only additionally carries the warning glyph, because ``-?x``
    differs from ``r-x`` in one character and this state is worth noticing: you
    can `cd` through the directory using a path you already know and you cannot
    list it. Colour is not used to make that distinction, since colour is never
    load bearing here.
    """
    style = style or Style()
    g = style.g
    if root.reach == Reach.UNKNOWN and not root.writable.durable:
        # Nothing at all was determined, so the cell is the one-character mark
        # rather than three of them.
        return UNKNOWN
    if root.reach == Reach.LISTABLE:
        r, x = "r", "x"
    elif root.reach == Reach.TRAVERSE:
        r, x = "-", "x"
    elif root.reach == Reach.CLOSED:
        r, x = "-", "-"
    else:
        r, x = UNKNOWN, UNKNOWN
    if root.writable.confirmed:
        w = "w"
    elif root.writable.refuted:
        w = "-"
    else:
        w = UNKNOWN
    text = r + w + x
    if root.reach == Reach.TRAVERSE:
        return style.warn(text + g.warn)
    if root.reach == Reach.CLOSED:
        return style.bad(text)
    return text


def role_cell(root):
    # type: (Root) -> str
    """The advisory role, or the unknown mark.

    Advisory: `sitecfg` derives it from path patterns, and `model` says it is
    "never used to decide access". It is still the first thing a reader looks
    for, which is why it leads the row and is also the first identity-shaped
    column the atlas gives up when the window is narrow.
    """
    return safe(root.role, limit=32) or UNKNOWN


# --------------------------------------------------------------------------
# policy cells
# --------------------------------------------------------------------------

#: Values that mean a site published a negative answer. Kept as a closed set so
#: an unrecognised value is never guessed at: it renders as content instead.
_POLICY_NO = frozenset(["no", "never", "off", "false", "0", "none"])


def merged_policy(root, site=None):
    # type: (Root, object) -> Dict[str, object]
    """Site policy for this path, overlaid with the root's own.

    Least specific first: a `Site.policy_for` glob answers for a whole
    filesystem, and anything the discovery layer attached to this particular
    root is more specific evidence than a glob. Both are advisory, and neither
    decides access.
    """
    merged = {}  # type: Dict[str, object]
    lookup = getattr(site, "policy_for", None)
    # Keys the discovery layer stores on `root.policy` as its own bookkeeping.
    # They are not policy and they must never reach a user-facing column: an
    # integration run printed `rank=primary` in the POLICY cell of every row,
    # which tells a reader nothing and looks like a leaked internal, because
    # it is one.
    internal = ("rank", "allocation_location", "allocation_accounts", "allocation_gb")
    if callable(lookup):
        published = lookup(root.path)
        if isinstance(published, dict):
            merged.update(published)
    if root.policy:
        merged.update(root.policy)
    for key in internal:
        merged.pop(key, None)
    return merged


def policy_glyph(value, glyphs):
    # type: (object, object) -> str
    """A published policy value as yes, no or unknown.

    The SAME three characters as `Verdict.glyph`, and a different provenance:
    these come from site configuration rather than from a probe, which the
    matrix legend states outright. Forcing a policy through `refuted()` was
    the alternative and it is worse: the model has no durable category for
    "the site publishes no such policy", so the wire format would carry `OK`
    beside a `false`.
    """
    ok = getattr(glyphs, "ok", "y")
    bad = getattr(glyphs, "bad", "n")
    if value is None:
        return UNKNOWN
    if isinstance(value, bool):
        return ok if value else bad
    if isinstance(value, (int, float)):
        return ok if value else bad
    text = str(value).strip().lower()
    if not text:
        return UNKNOWN
    return bad if text in _POLICY_NO else ok


def policy_cell(root, site=None):
    # type: (Root, object) -> str
    """The advisory policy, compactly, or the unknown mark.

    Absent means unknown, not absent: nobody told us whether this filesystem
    is purged, and saying "not purged" from silence is the same mistake as
    reporting an unmeasured quota as having no limit.
    """
    policy = merged_policy(root, site)
    if not policy:
        return UNKNOWN
    parts = []  # type: List[str]
    if "purge_days" in policy:
        days = policy.get("purge_days")
        if isinstance(days, bool) or days is None:
            parts.append("purge %s" % (UNKNOWN,))
        elif isinstance(days, (int, float)) and days > 0:
            parts.append("purge %dd" % (int(days),))
        elif isinstance(days, str) and days.strip():
            parts.append("purge %s" % (safe(days, limit=16),))
        else:
            parts.append("no purge")
    elif "purge" in policy:
        value = policy.get("purge")
        parts.append("purge %s" % (safe(value, limit=16) or UNKNOWN,))
    if "backup" in policy:
        value = policy.get("backup")
        if isinstance(value, bool):
            parts.append("backup yes" if value else "backup no")
        else:
            parts.append("backup %s" % (safe(value, limit=16) or UNKNOWN,))
    if "readonly" in policy:
        parts.append("ro" if policy.get("readonly") else "rw")
    if policy.get("speed"):
        parts.append(safe(policy.get("speed"), limit=16))
    for key in sorted(policy):
        if key in ("purge_days", "purge", "backup", "readonly", "speed", "note", "node_class"):
            continue
        parts.append("%s=%s" % (safe(key, limit=24), safe(policy.get(key), limit=24)))
    return " ".join(p for p in parts if p) or UNKNOWN


# --------------------------------------------------------------------------
# relationships
# --------------------------------------------------------------------------


def billed_to(root, roots):
    # type: (Root, Sequence[Root]) -> str
    """Which quota a boundary-crossing symlink actually bills to.

    Found by looking for the root that contains the link's target, and the
    unknown mark when no such root is in hand. Naming a guess here would be
    worse than saying nothing: the whole trap is that `~/.cache` bills against
    `/project` rather than against home, and a wrong name sends the reader to
    clean up the wrong filesystem.
    """
    target = root.symlink_target
    if not target:
        return UNKNOWN
    best = None  # type: Optional[Root]
    for other in roots:
        if other is root or not other.path:
            continue
        if target == other.path or target.startswith(other.path.rstrip("/") + "/"):
            if best is None or len(other.path) > len(best.path):
                best = other
    if best is None:
        return UNKNOWN
    row, _, _ = pick_row(best.quota, best.path, "blocks")
    if row is not None and row.label:
        return row.label
    if best.fileset and best.device:
        return "%s:%s" % (best.device, best.fileset)
    return best.fileset or best.device or best.path


def change_fields(record):
    # type: (object) -> Tuple[str, str, str]
    """``(label, path, because)`` from a change record.

    The state layer owns the record type, and the render layer must not care
    whether it arrives as an object or as a dict out of a snapshot file, so
    both are read. An unrecognised label is kept verbatim rather than dropped:
    a change the state layer reported and the atlas hid would be the worst of
    both designs.
    """

    def field(*names):
        # type: (*str) -> str
        for name in names:
            value = record.get(name) if isinstance(record, dict) else getattr(record, name, None)
            if value:
                return safe(value)
        return ""

    label = field("label", "kind", "change").lower() or "unknown"
    return label, field("path", "root"), field("because", "reason", "why")
