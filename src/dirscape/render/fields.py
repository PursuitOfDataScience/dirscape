"""One cell each, and ONE place the unknown mark is decided.

Every view in this package composes its rows out of the functions here. That is
deliberate: the rule this whole package exists to enforce is that **the
renderer may not print a value it was not given**, and a rule implemented in
six places is a rule with six chances to be wrong.

So there is no code path here from an unknown to a number, a blank or a zero:

* a root with no quota backend gets the unknown mark, never "no limit"
* a `QuotaRow.fraction` of None gets the unknown mark and NO percentage,
  because an invented 0% reads as plenty of room
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
from .style import Style, plain

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


def _limit_text(row, formatter, style=None):
    # type: (QuotaRow, object, Optional[Style]) -> str
    """The limit, "no limit" for an explicit zero, unknown when unreported.

    The two are different facts and the model collapses them in `limit`, which
    returns None for both. A backend that printed ``0`` said "no limit is
    enforced here"; a backend that printed nothing said nothing. The word
    "unlimited" appears nowhere in this package: it is the word that carries
    the lie when a quota was simply not measured.

    **"no limit" is DIM, and the words stay.** It was on five of ten rows of
    the live default view at the same weight as the figures, so the loudest
    repeated token in the table was the one place there is no number to read.
    Dim is the context tier, which is what it is: not a measurement, and not
    the absence of one either. Replacing it with a marker was considered and
    refused, because `?` already means "nobody measured this" and the two must
    not converge.
    """
    if row.limit is not None:
        return formatter(row.limit)  # type: ignore[operator]
    if row.soft == 0 or row.hard == 0:
        return (style or Style()).dim("no limit")
    return UNKNOWN


def _figure_cell(root, snapshot, kind, formatter, style, percent=False):
    # type: (Root, Optional[QuotaSnapshot], str, object, Style, bool) -> Tuple[str, str]
    row, how, why = pick_row(snapshot, root.path, kind)
    if row is None:
        # No quota to report. Before giving up, say what the FILESYSTEM has
        # left, when something measured it: a `?` where `df` would answer is
        # technically true and practically useless.
        #
        # Rendered as "<n> free", never as `used / limit`, because this is the
        # whole filesystem's headroom shared with every other user on the
        # node, and not this caller's usage. Dimmed so a reader scanning the
        # column can see at a glance which figures are theirs.
        free = (root.policy or {}).get("free_bytes")
        if kind == "blocks" and isinstance(free, int) and free >= 0:
            return style.muted("%s free" % (human_bytes(free),)), ""
        # The whole point of the package: no figure, no invented figure. The
        # caveat is empty rather than carrying `why`, because a caveat
        # qualifies a figure that WAS shown. Why a figure is missing is a
        # different question and `trouble()` answers it, in the footer, once
        # per root instead of once per column.
        return UNKNOWN, ""
    g = style.g
    used = formatter(row.used)  # type: ignore[operator]
    # The doubt marker goes on the USED figure, because usage is what is in
    # doubt. Appended after the limit it produced `11T / no limit▒`, which
    # reads as a mark against the limit or simply as a typo.
    if row.in_doubt:
        # The in-doubt glyph is NOT drawn on the figure any more. It carries a
        # real fact (GPFS had handed out 2.4G against a home showing 858M
        # used, which is three times the figure it sat beside and the reason a
        # `du` disagrees) and it carried it in one character that nobody can
        # decode. The owner asked what it was, which settles it: a mark that
        # has to be explained to be read is not communicating on a table this
        # dense. `why` states it in a sentence, `--legend` names it, and
        # `--json` carries `in_doubt` for anything mechanical.
        used = used
    limit = _limit_text(row, formatter, style)
    if plain(limit) == "no limit":
        # **`11T / no limit` became `11T used`.** The owner read this column
        # and asked why one row said `no limit` while another said `free`,
        # with no `/` in front of it: "there is no limit, but why is there
        # also free?" The column was carrying two different measurements in
        # two different shapes, and the shared `used / quota` heading claimed
        # both were the same thing.
        #
        # There are three things this column can honestly say, and each cell
        # now says which one it is in a word rather than in punctuation:
        #
        #     839M / 30G (3%)   your usage against your quota
        #     11T used          your usage, with no quota set here
        #     886G free         the filesystem's headroom, shared with everyone
        #
        # `used` and `free` are opposites, so no reader mistakes one for the
        # other, and the `/` now appears only where there really are two
        # numbers to divide. The heading is `space`, because that is the only
        # word true of all three.
        text = "%s %s" % (used, style.dim("used"))
    else:
        text = "%s / %s" % (used, limit)
    caveats = []  # type: List[str]
    if how == "inferred":
        # `~` before the figure, so a reader scanning the column sees which
        # numbers were attributed rather than published.
        text = "~" + text
        caveats.append(why)
    if row.guessed:
        caveats.append("the mount for this row was inferred from its name")
    fraction = row.fraction
    if percent and fraction is not None:
        # **There is no bar here any more, and that is the whole point.**
        #
        # It was eight cells of block characters plus the spaces to align
        # them, spent on a lossy picture of the exact percentage printed
        # immediately to their right, in the widest column of the table. It
        # was also blank on five of the ten rows of the live default view (an
        # unlimited quota has no fraction and a capacity fallback has no
        # quota), so the one thing a meter column is for, being scanned down,
        # it could not do. At 3% it drew a single thin glyph that read as
        # dirt, at 0% it was eight cells of trough saying what `0B` already
        # said, and under the interactive highlight its foreground colours
        # became the band's BACKGROUND and painted coloured blocks over the
        # selection. Owner's verdict: "why do we need this bar here".
        #
        # The graded colour survives and now carries fullness on its own,
        # which is what `tint` was always for. Two spaces before the figure,
        # never one: `atlas._align_figures` splits the tail on the first run
        # of two, and with a single space a bare `100%` left it nothing to
        # split on and the column lost its alignment.
        #
        # Width 3 so 3%, 22% and 100% share a right edge. Left-aligned they
        # formed a ragged fringe down the column, which is the one place a
        # percentage is worth reading next to its neighbours.
        # Parenthesised and ADJACENT, not flung to the right edge of a wide
        # cell. The alignment pass right-hangs this column, so a bare `3%`
        # ended up far from the `839M / 30G` it describes and under no heading
        # of its own, and the owner asked twice what it meant. Beside the two
        # numbers, in brackets, it reads as what it is: those two divided.
        text += " " + style.tint("(%d%%)" % (int(round(fraction * 100)),), fraction)
        if fraction >= 1.0:
            text += style.bad(g.warn)
    if row.in_doubt:
        caveats.append(
            "%s is allocated and not yet accounted for, so a du walk will disagree"
            % (formatter(row.in_doubt),)  # type: ignore[operator]
        )
    if row.note:
        caveats.append(row.note)
    return text, "; ".join(c for c in caveats if c)


def quota_cell(root, style=None):
    # type: (Root, Optional[Style]) -> Tuple[str, str]
    """``used / limit`` and a graded percentage, or the unknown mark.

    Plus any caveat.
    """
    style = style or Style()
    return _figure_cell(root, root.quota, "blocks", human_bytes, style, percent=True)


def inode_cell(root, style=None):
    # type: (Root, Optional[Style]) -> Tuple[str, str]
    """``files / limit``, with no percentage.

    One graded figure per row is a ranking; two is a reader deciding which of
    them the row was sorted by. Bytes are the axis that stops the writes, so
    bytes get the colour.
    """
    style = style or Style()
    return _figure_cell(root, root.inode_quota, "files", human_count, style)


#: Explicitly uncapped, as opposed to unmeasured. Two different facts and the
#: package has never allowed them to converge: `?` means nobody measured this
#: and must never soften into a blank, a zero or a word. A backend that printed
#: `0` for the limit said "no limit is enforced here", which is knowledge.
NO_LIMIT = "none"


def _governing(root):
    # type: (Root) -> Tuple[object, str]
    """The block quota row for this root, and how it was attributed."""
    row, how, _why = pick_row(getattr(root, "quota", None), root.path, "blocks")
    return row, how


def used_cell(root, style=None):
    # type: (Root, Optional[Style]) -> Tuple[str, str]
    """Your usage. ONE token, or the unknown mark.

    **This is the column that used to hold three different shapes**, which is
    the defect the split fixes. It read `866M / 30G (3%)` on one row, `11T
    used` on the next and `886G free` on a third, under a heading that claimed
    all three were the same measurement. The owner, twice: "why is there no /
    in front of free? ... there is no limit, but why is there also free?" and
    then "simply saying 11T used but no cap is very confusing. all the entries
    in space aren't consistent at all."

    Both readings were right, and adding a word to each cell (`used`, `free`)
    did not fix it, because the shapes still differed. Three facts were being
    packed into one cell, so they are three columns now: `used`, `limit`,
    `free`. Every cell in every one of them is a single figure or `?`, which
    is what makes a numeric column scannable, and `limit` says `none` where
    there is genuinely no cap rather than leaving the reader to infer it from
    a missing second number.
    """
    style = style or Style()
    row, how = _governing(root)
    if row is None:
        return UNKNOWN, ""
    text = human_bytes(row.used)
    caveats = []  # type: List[str]
    if how == "inferred":
        # `~` before the figure, so a reader scanning the column sees which
        # numbers were attributed rather than published.
        text = "~" + text
    if row.guessed:
        caveats.append("the mount for this row was inferred from its name")
    # **One tone for every cell in the column, and no grading.** This used to
    # tint by fullness where a fraction existed and mute where none did, so on
    # the live table `866M` (3% of a 30G quota) was a graded blue and `11T`
    # (no cap, so no fraction) was grey, side by side in one column. The owner:
    # "in the used column, both entries have different colors. which is shit."
    #
    # They are right, and the reason is structural rather than a matter of
    # taste: a grading that only half the rows can carry is not a scale, it is
    # two categories the reader has to decode before comparing two numbers.
    # `free` now carries "how much room is left" as a figure on every row,
    # which is what the grading was approximating, and this package's standing
    # rule is that colour is never load bearing.
    return style.muted(text), "; ".join(caveats)


def limit_cell(root, style=None):
    # type: (Root, Optional[Style]) -> str
    """Your cap: a figure, `none` when uncapped, `?` when unmeasured."""
    style = style or Style()
    row, _how = _governing(root)
    if row is None:
        # No quota row, but the mount table may have settled it: a filesystem
        # mounted `noquota` enforces no limit, and saying so is not a guess.
        if (root.policy or {}).get("no_quota_enforced"):
            return style.dim(NO_LIMIT)
        return UNKNOWN
    if row.limit is not None:
        return style.muted(human_bytes(row.limit))
    if row.soft == 0 or row.hard == 0:
        return style.dim(NO_LIMIT)
    return UNKNOWN


def free_cell(root, style=None):
    # type: (Root, Optional[Style]) -> str
    """What you can still write here, which is the question the tool is for.

    Two sources, and the choice between them is the whole content of this
    cell. Under a quota the answer is your own remaining allowance; with no
    quota it is the filesystem's headroom, shared with everyone on the node.
    The SMALLER of the two wins where both are known, because a 40T allowance
    on a filesystem with 2T left is 2T of writes and reporting 40T would be
    the fabrication this package exists to avoid.
    """
    style = style or Style()
    room = None  # type: Optional[int]
    row, _how = _governing(root)
    if row is not None and row.limit is not None and row.used is not None:
        room = max(0, int(row.limit) - int(row.used))
    disk = (root.policy or {}).get("free_bytes")
    if isinstance(disk, int) and disk >= 0:
        room = disk if room is None else min(room, disk)
    if room is None:
        return UNKNOWN
    return style.muted(human_bytes(room))


def file_count_cell(root, style=None):
    # type: (Root, Optional[Style]) -> str
    """How many files you hold. The COUNT only, and no limit beside it.

    The heading was `files / limit`, which the owner named directly: "these
    column names are so ugly". It was also the second cell in the table
    carrying two numbers in one box, and an inode ceiling is the rarest thing
    on this screen to be near. `--json` and `why` carry the limit.
    """
    style = style or Style()
    row, _how, _why = pick_row(getattr(root, "inode_quota", None), root.path, "files")
    if row is None:
        return UNKNOWN
    return style.muted(human_count(row.used))


def inode_limit_cell(root, style=None):
    # type: (Root, Optional[Style]) -> str
    """How many files you may hold: a figure, `none`, or the unknown mark.

    The same three states as `limit_cell` and for the same reason. An inode
    ceiling is the one people forget: a home directory here allows 300,000
    files against 30G of space, so a tree of small files exhausts the count
    long before the bytes, and the error message when it happens says nothing
    about files.
    """
    style = style or Style()
    row, _how, _why = pick_row(getattr(root, "inode_quota", None), root.path, "files")
    if row is None:
        if (root.policy or {}).get("no_quota_enforced"):
            return style.dim(NO_LIMIT)
        return UNKNOWN
    if row.limit is not None:
        return style.muted(human_count(row.limit))
    if row.soft == 0 or row.hard == 0:
        return style.dim(NO_LIMIT)
    return UNKNOWN


def in_doubt_of(root):
    # type: (Root) -> Optional[int]
    """Bytes the backend says are allocated but not yet accounted for."""
    row, _, _ = pick_row(root.quota, root.path, "blocks")
    return row.in_doubt if row is not None else None


def unmeasured(root):
    # type: (Root) -> bool
    """True when nothing at all could be said about this root's space.

    A root the `statvfs` fallback answered for is NOT unmeasured: the footer
    reported "4 unmeasured" while every row in the table showed a figure,
    which is a footer contradicting the table directly above it.
    """
    row, _, _ = pick_row(root.quota, root.path, "blocks")
    if row is not None and row.used is not None:
        return False
    free = (root.policy or {}).get("free_bytes")
    return not isinstance(free, int)


def trouble(root):
    # type: (Root) -> str
    """Why a root's figure is missing or suspect, in one sentence.

    Prose, so it goes through `category_label` and never shows a wire token.
    """
    snap = root.quota
    if snap is None:
        free = (root.policy or {}).get("free_bytes")
        if isinstance(free, int):
            # The treemap said "no quota reading was taken" for the very roots
            # the atlas was showing a figure for, because it only knew about
            # quota rows and the capacity fallback lives elsewhere. Two views
            # of one run disagreeing about whether anything was measured is
            # worse than either answer.
            #
            # The cell stays UNSIZED on purpose: `statvfs` reports the whole
            # filesystem's headroom, shared with everyone on the node, and
            # sizing a tile by that would let a shared `/tmp` dwarf the user's
            # own project directory. Saying which figure exists is the fix,
            # not sizing by the wrong one.
            return (
                "no per-user figure here, so there is nothing of yours to size; "
                "the filesystem reports %s free" % (human_bytes(free),)
            )
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


#: What you can do here, in words, and the SAME words `why` prints.
#:
#: This replaced `rwx` / `r-x` / `r?x` / `-?x` / `---`, a POSIX-shaped triple
#: with one slot per question. That encoding was compact, exact and correct,
#: and it was addressed to somebody who already reads `ls -l` output. Owner:
#: "since we have a lot of horizontal spacing, don't use rwx, just use regular
#: words so that it's new user friendly. utilize the space optimally." There
#: is room for twelve characters, and a new user should not have to decode a
#: cell to learn they can write to their own home directory.
#:
#: Keyed on the pair, because the two questions are independent and either can
#: be unanswered. The distinction the triple existed to preserve is preserved:
#: `read` means nobody checked whether you can write, which is a different
#: answer from `read only`, and `?` is still never a blank and never a "no".
ACCESS_WORDS = {
    (Reach.LISTABLE, "yes"): "read + write",
    (Reach.LISTABLE, "no"): "read only",
    (Reach.LISTABLE, ""): "read",
    (Reach.TRAVERSE, "yes"): "enter + write",
    (Reach.TRAVERSE, "no"): "enter only",
    (Reach.TRAVERSE, ""): "enter only",
    (Reach.CLOSED, "yes"): "write only",
    (Reach.CLOSED, "no"): "no access",
    (Reach.CLOSED, ""): "no access",
}


def _write_state(root):
    # type: (Root) -> str
    """``yes``, ``no``, or the empty string for unanswered."""
    write = getattr(root, "writable", None)
    if write is None:
        return ""
    if write.confirmed:
        return "yes"
    if write.refuted:
        return "no"
    return ""


def access_words(root):
    # type: (Root) -> str
    """The access phrase, or the unknown mark. Shared with `why`."""
    reach = getattr(root, "reach", Reach.UNKNOWN)
    write = getattr(root, "writable", None)
    if reach == Reach.UNKNOWN and not (write is not None and write.durable):
        return UNKNOWN
    if reach == Reach.UNKNOWN:
        return UNKNOWN
    return ACCESS_WORDS.get((reach, _write_state(root)), UNKNOWN)


def reach_cell(root, style=None):
    # type: (Root, Optional[Style]) -> str
    """What you can do here, in words.

    Every ANSWERED state reads the same weight, `read + write` and `read only`
    alike. Muting only the commonest one was well meant and misfired: with
    eight of ten rows reading the same thing, the one read-only dataset was
    the only bright cell in the column and looked flagged. The owner asked
    "why is r-x a different color?", which is the question a reader should
    never have to ask about a cell that is merely normal.

    Colour is reserved for the two states genuinely worth stopping on:
    `no access` in `bad`, and traverse-only in `warn` with its own glyph,
    because being able to `cd` through a directory you cannot list is worth
    noticing. Not being able to write somewhere is ordinary.
    """
    style = style or Style()
    text = access_words(root)
    if text == UNKNOWN:
        return UNKNOWN
    if root.reach == Reach.TRAVERSE:
        return style.warn(text + style.g.warn)
    if root.reach == Reach.CLOSED:
        return style.bad(text)
    return style.muted(text)


def role_cell(root, style=None):
    # type: (Root, Optional[Style]) -> str
    """The advisory role, or the unknown mark.

    Advisory: `sitecfg` derives it from path patterns, and `model` says it is
    "never used to decide access". It is still the first thing a reader looks
    for, which is why it leads the row and is also the first identity-shaped
    column the atlas gives up when the window is narrow.

    **Dim, because it is a guess derived from the path beside it.** A word in
    the context tier still groups a run of rows at a glance, which is the job;
    at full weight it was competing with the figures for attention it had not
    earned. The word itself stays: a coloured marker in its place would put
    the only copy of the role in the colour channel, and `NO_COLOR` has to
    tell every state of this tool apart.
    """
    text = safe(root.role, limit=32)
    if not text:
        return UNKNOWN
    return (style or Style()).dim(text)


# --------------------------------------------------------------------------
# policy cells
# --------------------------------------------------------------------------

#: Values that mean a site published a negative answer. Kept as a closed set so
#: an unrecognised value is never guessed at: it renders as content instead.
_POLICY_NO = frozenset(["no", "never", "off", "false", "0", "none"])


def total_stranded(roots):
    # type: (Sequence[Root]) -> str
    """The bytes held in unreachable filesets, as one figure, or "".

    Summed so the atlas can say "5 filesets hold 18G you cannot reach" in one
    line instead of five. Returns "" rather than "0B" when nothing could be
    measured, because a zero would read as "nothing is stranded" when the
    truth is that the amount is unknown.
    """
    total = 0
    measured = False
    for root in roots:
        if not getattr(root, "stranded", False):
            continue
        snap = getattr(root, "quota", None)
        if snap is None:
            continue
        for row in snap.rows:
            if row.kind == "blocks" and row.used is not None:
                total += int(row.used)
                measured = True
    return human_bytes(total) if measured else ""


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
    # **An ALLOWLIST on the root's side, and it replaced a blacklist.** The
    # POLICY column shows published policy, and `root.policy` is also where
    # the discovery layer keeps its bookkeeping, so the two have to be told
    # apart. Naming the bookkeeping was tried first and rotted twice: it
    # started at `rank`, was found short by six when a 200 column run printed
    # `crosses_to=[...]`, `contains=2`, `free_bytes=...` and `size_bytes=...`
    # in the POLICY cell of every row, and then short by two more the moment
    # `_device_wide` and `_measure` each set a flag. Every one of those leaks
    # was a raw internal sitting beside its own formatted self somewhere else
    # in the same view.
    #
    # A blacklist has to be updated by whoever adds a key, which is the wrong
    # person to rely on, so the root's side is now limited to the vocabulary
    # `sitecfg` documents for a `[policy]` entry. Bookkeeping cannot leak by
    # being forgotten, only by deliberately using a policy name.
    #
    # The SITE's side is not filtered. `sitecfg` accepts arbitrary keys there
    # on purpose, so an administrator can publish something this package has
    # never heard of and have it shown.
    published_vocabulary = (
        "purge_days",
        "purge",
        "backup",
        "readonly",
        "speed",
        "archive",
        "snapshots",
        "note",
        "label",
    )
    if callable(lookup):
        published = lookup(root.path)
        if isinstance(published, dict):
            merged.update(published)
    if root.policy:
        merged.update({k: v for k, v in root.policy.items() if k in published_vocabulary})
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
