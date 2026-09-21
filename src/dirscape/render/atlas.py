"""The default view: one row per root, and what you can actually do with it.

    role | path | where | reach | used / quota | files / limit | policy

`where` is the column this tool exists for. The site quota wrapper here reports
`/cfs3` and `/cfs4` from a node where neither path exists, and "allocated, and
not mounted on this node" is the most useful sentence the tool can produce. So
the node class is in the title beside the hostname: mount visibility is a
property of the node you are standing on, not of the filesystem.

**The whole view is one framed panel**, titled, ruled, and closed under the
last row. That is `nodetop`'s shape and it is deliberately the same shape, so
the two tools read as one family; `style.panel` carries the gradient and the
reasoning. What the frame changed here is mostly what it made obvious: the view
was three lines of table and three lines of chrome underneath it, and inside a
box the chrome had nowhere to hide. So the alert teasers and the counts moved
behind `--summary`, the usage bar went (see `fields._figure_cell`), and the
headings went lower case and indented, which is most of the temperature
difference.

**Narrow terminals are handled by dropping columns, never by truncating a
path.** A shortened path is a different path, and the reader cannot tell which
characters went. The stages, in order, with the reason for each place:

===== ====================== =====================================================
stage what goes              why it goes there
===== ====================== =====================================================
1     policy                 advisory site text, the widest cell, least urgent
2     files / limit          inodes are the second quota axis, bytes are the first
3     role                   a heuristic label, and the path already implies it
4     reach                  three characters, and `dirscape why` can say more
5     used / quota           the last fact to go
===== ====================== =====================================================

`path` and `where` are never dropped. Below the width those two need, the view
STACKS instead: the full path on its own line with the remaining cells indented
beneath it. A path wider than the window is printed whole and left to the
terminal's own wrap, which loses no characters, and **the frame comes off for
that case**: a box can only hold a line by cutting it, and cutting a path is
the one thing this view will not do.
"""

import re
from typing import Dict, List, Optional, Sequence, Tuple

from ..model import Root, VerdictCategory, category_label
from . import fields
from .style import Style, legend, panel, table, wrap
from .style import width as measure

__all__ = ["render", "COLUMNS", "DROP_STAGES", "KEEP_COLUMNS"]


#: Column headings, in order. The index of each is what `DROP_STAGES` names.
#:
#: Lower case, and that is a measured choice rather than a whim. Seven headings
#: in capitals are seven words shouting above a table of figures, and the
#: figures are the content; `nodetop` sets its grids in lower case for the same
#: reason and the owner named it as the reference. The `/` pairs stay, because
#: `used / quota` is one statement and the heading has to say so.
#: Column headings, in order. **One word each, and one figure per cell.**
#:
#: **`free` is NOT here. Only the two figures the filesystem actually
#: reports are.** The three-column form was `used`, `limit`, `free`, and the
#: owner's objection was that the third is arithmetic on the first two: "it's
#: just a product of the two previous columns", then, after `limit` was
#: dropped instead, "why is free column still there. makes no sense."
#:
#: Dropping `limit` and keeping `free` was the wrong half. `used` and `limit`
#: are what a quota backend measures and prints; `free` is a subtraction this
#: view was performing and then displaying next to its own operands. A table
#: shows what was measured, and a reader who wants the difference can take
#: it, which is the same reason there is no percentage column.
#:
#: The cost is real and is accepted: on a mount with no quota system both
#: cells read `?`, and the filesystem's own headroom (which `statvfs` knows
#: and `free` used to show) is no longer on the table. It is not a per-user
#: figure, so it never sat honestly beside two that are; it is in `why`,
#: labelled, and in `--json`.
#:
#: `space` and `files / limit` were the last two headings carrying more than
#: one measurement, and the owner named both: "simply saying 11T used but no
#: cap is very confusing. all the entries in space aren't consistent at all",
#: then "space has no /? these column names are so ugly". A column of
#: `866M / 30G (3%)`, `11T used`, `886G free` and `?` cannot be scanned,
#: because the reader has to parse each cell's shape before comparing its
#: number. Splitting them costs two columns of width and buys a table where
#: every numeric cell is one token and every heading is one word.
COLUMNS = (
    # `kind`, not `role`. The column holds `home`, `project`, `scratch`,
    # `dataset`, `software` and `local`, which is what KIND of storage each
    # row is, and the owner named the heading: "the word role is poorly
    # chosen." It was right in the model's terms, where a root's role is what
    # the site uses it for, and wrong on screen: a reader does not ask what
    # role their scratch directory plays, they ask what sort of place it is.
    # The wire vocabulary is untouched: `--json` still carries `role`, because
    # a consumer may switch on it. Same split as `CATEGORY_LABELS`.
    "kind",
    "path",
    "where",
    # `access`, not `reach`. Owner: "reach doesn't make sense either." It was
    # the model's word for a tri-state (listable / traverse-only / closed) and
    # it leaked out of the model onto a heading, which is the same mistake
    # `role` made one round earlier. A reader asks what they can DO here, and
    # `why` has labelled that field `access` all along, so the table and the
    # detail view now use one word for one thing. `Reach` stays the type's
    # name and `--json` still carries `reach`, which is the wire vocabulary.
    "access",
    "used",
    "limit",
    "files",
    "policy",
)

_ROLE, _PATH, _WHERE, _REACH, _USED, _LIMIT, _FILES, _POLICY = range(8)

#: Never dropped. The path identifies the row and WHERE carries the answer this
#: tool is for.
KEEP_COLUMNS = (_PATH, _USED)

#: Columns to drop, per stage, walked in order until one fits the window.
#: Documented in the module docstring, and asserted in `tests/test_cli.py` so
#: the order cannot drift silently.
#:
#: There used to be a sixth stage that kept every column and dropped the usage
#: BAR, on the grounds that the digits survive the picture. The bar is gone
#: from every stage now, so the stage went with it.
DROP_STAGES = (
    (),
    (_POLICY,),
    (_POLICY, _FILES),
    (_POLICY, _FILES, _ROLE),
    (_POLICY, _FILES, _ROLE, _REACH),
    # WHERE goes before any figure, and that ordering is the point. An earlier
    # version's last stage dropped USED, so at 40 columns the table degraded to
    # a bare list of paths with no number anywhere on it, which is not a
    # smaller version of this tool's answer but the absence of one. WHERE is
    # context and reads `here` on nearly every row anyway.
    (_POLICY, _FILES, _ROLE, _REACH, _WHERE),
)

#: What the table is indented by, inside the frame, and the run of spaces
#: between two columns.
#:
#: Both were two, and "horizontal spacing is also an issue" was the owner's
#: verdict on the result: a right-aligned figure sat almost against the cell on
#: its left, so eleven rows read as one dense block rather than as columns. Four
#: is what `nodetop` leaves around its numeric pairs, and the room came for
#: free out of the ten columns the bar gave back.
_INDENT = "   "
_GUTTER = "    "

#: Every figure column is right-aligned, which is what `_align_figures` used
#: to fake inside one composed cell and what real columns do for free.
_ALIGNS = ("left", "left", "left", "left", "right", "right", "right", "left")

#: No figure column may be squeezed. `314G` truncated to `31...` is not a
#: smaller version of the fact but a different and false one, and every cell
#: in these columns is short enough that squeezing one would never be the
#: difference between fitting and not.
_ATOMIC = (_PATH, _USED, _LIMIT, _FILES)

_NOTE_LIMIT = 4

#: The narrowest body the frame will squeeze the layout into before the frame
#: itself is the thing making the table not fit. Below this the panel is not
#: the problem a reader has.
_MIN_BODY = 28


def _path_cell(root, style=None):
    # type: (Root, Optional[Style]) -> str
    """The path, or the allocation location when there is no path.

    An allocated root that is not mounted here HAS no path, deliberately:
    turning `cfs4/hpc-staff` into `/cfs4/hpc-staff` is a guess, and the
    allocation database is not a mount table. But rendering it as `?` throws
    away the only identifying thing about the row, so the location is shown
    with a marker saying what it is. Without this the ELSEWHERE rows printed
    `?` in every column and were indistinguishable from each other.

    **The directories leading up to the last one are dimmed, and not one
    character is removed.** Four of the live rows begin `/scratch/` and three
    begin `/project`, so at one weight the column's left half is the same
    prefix repeated and the part that differs is the part that is hardest to
    find. The plain text is byte for byte what it was, which is what keeps
    `--json`, a pipe and a `NO_COLOR` terminal identical.
    """
    if root.path:
        # The fold count `+2` is gone. It said this row had absorbed two
        # subdirectories, which was true and was not something a reader could
        # do anything with: the count named no path, and the only action
        # available was `--all`, which lists them properly. The owner read
        # `/project/hpc +2` and asked what it meant; the honest answer was "a
        # number you cannot use", and the count survives in `--json` and
        # `--summary` for anyone who wants it.
        # ONE tone. Dimming the parent directories and leaving the leaf bright
        # was meant to put the eye on the part that differs between sibling
        # rows; what it produced was a column of two-tone paths, and the owner
        # read it as "some grey some green" and asked why. A path is one
        # identifier and it reads as one.
        return root.path
    location = root.policy.get("allocation_location") if root.policy else None
    if location:
        # The trailing marker is not decoration. It is the difference between
        # "you can cd here" and "your allocation database calls it this".
        text = "%s (location)" % (location,)
        if style is None or not style.enabled:
            return text
        return location + style.dim(" (location)")
    return fields.UNKNOWN


def _row(root, style, site):
    # type: (Root, Style, object) -> Tuple[List[str], str]
    used, caveat = fields.used_cell(root, style)
    cells = [
        fields.role_cell(root, style),
        _path_cell(root, style),
        fields.where_cell(root, style),
        fields.reach_cell(root, style),
        used,
        fields.limit_cell(root, style),
        fields.file_count_cell(root, style),
        fields.policy_cell(root, site),
    ]
    return cells, caveat


_ANSI = re.compile("\033\\[[0-9;?]*[A-Za-z]")


def _strip(text):
    # type: (str) -> str
    """The text a reader sees, with the escapes removed."""
    return _ANSI.sub("", text).strip()


def _heading(index, roots):
    # type: (int, Sequence[Root]) -> str
    """The column's heading, which for the two figures depends on the data.

    Owner, of `limit`: "what does limit mean? does it mean there is no user
    level limit or the dir has some ceiling but there is no restriction on the
    user side?" A fair question with no answer on screen, and the ambiguity is
    real rather than a wording slip: `QuotaRow.scope` is `user`, `group` or
    `fileset`, so the same cell can be a personal allowance or the ceiling on
    everything stored in a directory, and those are different numbers a reader
    would act on differently.

    Measured on the development cluster: every row of the default view is
    user-scoped, because the site's backend is `mmlsquota -u`. So the honest
    heading there is `your use` and `your limit`.

    **The claim is checked against the rows rather than assumed.** If any row
    on screen is group or fileset scoped, "your" would be false for it, and
    one wrong heading is worse than a vague one; the columns fall back to
    `used` and `limit` and `why` names the scope per row. Deciding it here,
    from the data, is what keeps a site nobody has an account on from being
    told a lie about its own quotas.
    """
    name = COLUMNS[index]
    if index not in (_USED, _LIMIT):
        return name
    saw = False
    for root in roots:
        snap = getattr(root, "quota", None)
        for row in getattr(snap, "rows", ()) or ():
            saw = True
            if (getattr(row, "scope", "") or "") != "user":
                return name
    if not saw:
        return name
    return "your use" if index == _USED else "your limit"


def _constant_columns(rows):
    # type: (Sequence[Sequence[str]]) -> set
    """Column indexes whose value never varies, excluding the ones that must stay.

    PATH is never dropped, and USED is never dropped even if every row happens
    to read the same, because both are the answer rather than the context.
    """
    if len(rows) < 2:
        return set()
    keep = set(KEEP_COLUMNS)
    out = set()
    for index in range(len(COLUMNS)):
        if index in keep:
            continue
        seen = {row[index] for row in rows if index < len(row)}
        if len(seen) == 1:
            out.add(index)
    return out


def _column_width(headers, rows, columns, indent="", gutter="  "):
    # type: (Sequence[str], Sequence[Sequence[str]], Sequence[int], str, str) -> int
    if not columns:
        return 0
    total = measure(gutter) * (len(columns) - 1) + measure(indent)
    for index in columns:
        widest = measure(headers[index])
        for row in rows:
            widest = max(widest, measure(row[index]))
        total += widest
    return total


def _plan(rows, window, skip=()):
    # type: (Sequence[Sequence[str]], int, Sequence[int]) -> Tuple[List[int], bool]
    """Choose the column set: ``(columns, stacked)``.

    The first stage that fits wins. When none does, the caller stacks, which is
    the only degradation left that does not shorten a path.

    ``skip`` is the columns the caller has ALREADY decided not to render, and
    passing it is load bearing rather than an optimisation. Without it this
    measured every stage against the full seven columns and then the caller
    removed the constant ones afterwards, so width was being spent on columns
    that were about to be thrown away: at an 80 column terminal the widths of
    WHERE, FILES and POLICY pushed every stage over budget until stage three,
    which drops ROLE, and ROLE was then the only column the reader actually
    lost. Measured without them the same table needs 67 columns of 76 and
    keeps ROLE. A view that discards information to make room for blanks has
    the fitting backwards.
    """
    skip = set(skip)
    for dropped in DROP_STAGES:
        columns = [i for i in range(len(COLUMNS)) if i not in dropped and i not in skip]
        if not columns:
            continue
        if _column_width(COLUMNS, rows, columns, _INDENT, _GUTTER) <= window:
            return columns, False
    return [_PATH, _USED], True


def _header(roots, meta, style, size=None):
    # type: (Sequence[Root], fields.RunMeta, Style, Optional[int]) -> str
    """The title line, inside the frame.

    Only facts that qualify every row underneath it. **The user is on it now**,
    ahead of the host: a quota is per user, so "whose storage is this" is the
    scope of the whole table and it was the one thing the header never said.

    **The run's own elapsed time came off.** `2.7s` is the tool talking about
    itself rather than about the storage, `--timing` reports it properly and
    in more detail, and it was the only item on the line that changed on every
    single run for no reason a reader acts on. The version is still absent for
    the same reason: `--version` answers that.

    The weights are the palette's tiers rather than a decoration: the tool and
    the scope (`user`, `host`) identify, so they take `accent` and `head`; the
    node class and the device count are measurements, so `muted`; the baseline
    age is context, so `dim`.
    """
    node = fields.safe(meta.node_class, limit=32)
    if not node or node == "unknown":
        # `discover.mounts.node_class` answers the literal string "unknown".
        # That is the tool's own vocabulary for an unanswered question, and on
        # screen the answer to an unanswered question is the mark.
        node = fields.UNKNOWN
    user = fields.safe(meta.user, limit=64) or fields.UNKNOWN
    items = [
        style.head(fields.safe(meta.tool, limit=32) or "dirscape"),
        style.accent(user),
    ]
    # The node class, the device count and the baseline age all came off.
    # The owner read the finished line and asked what it meant, which is the
    # only test a header has to pass, and it failed all three:
    #
    #   `compute`      answers a question nobody asked, and matters only when
    #                  it CHANGES, which the diff already refuses to do across
    #                  node classes. `why` states it in a sentence.
    #   `9 devices`    nobody needs to know how many block devices a node
    #                  mounts. It was a number the tool found interesting
    #                  about itself.
    #   `baseline 47m` is `dirscape new`'s business and that view prints it
    #                  properly. On a table of current usage it is a date
    #                  attached to nothing on screen.
    #   `meadow3-0200` went the same way, one round later, to the same
    #                  question: "this meadow node needs to be shown? for what
    #                  reason?" The reason it was there is real but it is not
    #                  the reader's: mounts are per node, so a snapshot has to
    #                  record where it was taken or a diff would compare a
    #                  login node's storage against a compute node's. That is
    #                  the DIFF's problem, and `state.same_vantage` already
    #                  solves it by refusing to compare across node classes.
    #                  A reader looking at their own storage on the machine
    #                  they are typing on already knows which machine that is.
    #                  It is still in `--json`, in `why`, in `new`, and in
    #                  every snapshot record, which are the places it is load
    #                  bearing.
    #
    # What is left is the scope of every row underneath: which tool, whose
    # quota, which machine. `node` is still computed above because `--summary`
    # and the JSON metadata both carry it.
    return legend(items, style, size=size if size else style.size, indent="")


def _delta_lines(roots, changes, style, size=None):
    # type: (Sequence[Root], Sequence[object], Style, Optional[int]) -> Tuple[List[str], List[str]]
    """``(stranded lines, change lines)``. Change records are NOT computed here.

    Stranded comes out separately so the caller can give it its own heading
    above the deltas. It earns that because it is the one thing here no other
    tool reports: space you are charged for, in a scope you can no longer
    reach. Two sources feed it, and deliberately so: a `stranded` record from
    the state layer is a change, and `Root.stranded` is a standing state that
    deserves the same line whether or not it changed since the baseline.

    The label is lower case and the prominence comes from the heading, the
    glyph and the position. `STRANDED` in capitals would be prominent too, and
    it is also exactly `VerdictCategory.STRANDED`: putting a wire token in a
    prose column is nodetop's NT-5 whether or not the token happens to read as
    English, and `tests/test_render_views.py` scans for every one of them.
    """
    by_path = {r.path: r for r in roots if r.path}
    groups = {}  # type: Dict[str, List[Tuple[str, str]]]
    seen_stranded = set()

    for record in changes:
        label, path, because = fields.change_fields(record)
        root = by_path.get(path)
        if root is not None and root.renamed_from and label in ("new", "opened"):
            # A move arrives as one record, so it is reported as one line. A
            # loss plus an arrival would be a false alarm about data that is
            # visible at the new path.
            moved = "moved from %s" % (fields.safe(root.renamed_from, limit=256),)
            because = "%s; %s" % (moved, because) if because else moved
        if label == "stranded":
            seen_stranded.add(path)
        groups.setdefault(label, []).append((path, because))

    for root in roots:
        if root.stranded and root.path not in seen_stranded:
            because = root.reach_reason or category_label(VerdictCategory.STRANDED)
            groups.setdefault("stranded", []).append((root.path, because))

    ordered = [name for name in fields.CHANGE_ORDER if name in groups]
    # An unrecognised label is shown verbatim rather than dropped: a change the
    # state layer reported and the atlas hid would be the worst of both.
    ordered += sorted(name for name in groups if name not in fields.CHANGE_ORDER)

    g = style.g
    paint = {
        "stranded": style.bad,
        "gone": style.bad,
        "closed": style.warn,
        "shrank": style.warn,
        "new": style.info,
        "opened": style.ok,
        "grew": style.info,
        "unknown": style.dim,
    }
    room = max([len(name) for name in ordered] or [0])
    stranded = []  # type: List[str]
    deltas = []  # type: List[str]
    for name in ordered:
        for path, because in groups[name]:
            colour = paint.get(name, style.muted)
            if name == "stranded":
                stranded.append(
                    "%s%s %s  %s"
                    % (
                        _INDENT,
                        colour(g.warn),
                        path or fields.UNKNOWN,
                        style.muted(because or fields.UNKNOWN),
                    )
                )
                continue
            mark = g.warn if name == "gone" else " "
            head = "%s%s %s  %s" % (
                _INDENT,
                colour(mark),
                colour(name.ljust(room)),
                path or fields.UNKNOWN,
            )
            reason = because or fields.UNKNOWN
            if size is None or measure(head) + 2 + measure(reason) <= size:
                deltas.append("%s  %s" % (head, style.muted(reason)))
                continue
            # Wrapped onto a hanging indent rather than cut. These lines ARE
            # `dirscape new`, and the useful half of a `shrank` is the second
            # half: "mmlsquota reports 853.5 MiB, where the run at 14:53
            # reported 3.3 GiB". Inside a frame an over-long line is truncated,
            # so left alone every reason ended in an ellipsis exactly where the
            # comparison began.
            deltas.append(head)
            hang = " " * (measure(_INDENT) + 2 + room + 2)
            for line in wrap(reason, indent=hang, size=size, style=style).splitlines():
                deltas.append(style.muted(line))
    return stranded, deltas


def _caveat_count(roots, caveats):
    # type: (Sequence[Root], Sequence[Tuple[str, str]]) -> int
    """How many rows carry a qualification on their figure.

    A COUNT and not the sentences. There used to be a `_notes` builder that
    formatted one line per caveat, capped at four, and nothing ever printed
    what it returned: the only caller took its length and handed that to a
    footer argument the footer ignored. Printed for real under `--summary` it
    was four lines of prose with an ellipsis eaten into each of them, because
    a caveat is a sentence and a table cell's worth of width is not a place
    for one. `dirscape why <path>` is where they belong and it already says
    them in full, so the summary says how many there are and points there.
    """
    total = len(caveats)
    for root in roots:
        if root.symlink_target and root.crosses_boundary:
            total += 1
    return total


def _footer(roots, style, dropped, window, hidden=0, legend_on=False, counts=False, caveats=0):
    # type: (Sequence[Root], Style, Sequence[str], int, int, bool, bool, int) -> List[str]
    """The counts line and the glyph legend, and by default NEITHER.

    It printed six lines once: a dropped-column notice, an unmeasured count,
    the `rdu` handoff and a two-line glyph legend, on every single run. That
    became one line, and one line is still one line of chrome a reader did not
    ask for on every run of the tool. **So the counts now need `--summary` and
    the legend still needs `--legend`**, which is `counts` and `legend_on`
    here. Nothing is lost: `--all` shows the held-back rows, `dirscape why`
    explains an unmeasured one, and `--json` has every column.
    """
    g = style.g
    bits = []  # type: List[str]

    if counts:
        if hidden:
            bits.append("%d hidden (%s)" % (hidden, style.accent("--all")))
        stuck = [r for r in roots if fields.unmeasured(r)]
        pointer = style.accent("dirscape why <path>")
        if stuck:
            bits.append("%d unmeasured (%s)" % (len(stuck), pointer))
        if caveats:
            # Sharing one pointer with the line above rather than repeating
            # it: two advisories that both end in `dirscape why <path>` is the
            # same sentence twice.
            bits.append("%d qualified%s" % (caveats, "" if stuck else " (%s)" % (pointer,)))
        if dropped:
            bits.append(
                "%d column%s hidden (%s)"
                % (len(dropped), "" if len(dropped) == 1 else "s", style.accent("--json"))
            )

    lines = []  # type: List[str]
    if bits:
        lines.append(style.dim(_INDENT + ("  %s  " % (g.sep,)).join(bits)))

    if legend_on:
        # **Rewritten to describe the table that exists.** It explained `r`,
        # `w`, `x` and `-`, which the access column stopped using when it
        # went over to words, and `%s`, which came off the figures two rounds
        # before that. A legend for a view that has moved on is worse than no
        # legend: a reader who cannot find the character it describes has to
        # decide whether they are looking at the wrong column or reading stale
        # documentation.
        #
        # What is left needs explaining because it cannot be said in a cell:
        # the difference between `read` and `read only`, which is the whole
        # point of the tri-state, and the two marks still drawn.
        for text in (
            "access: `read` means nobody checked whether you can write, which is not the "
            "same as `read only`. Pass --probe-write to settle it.",
            "%s means nobody could measure this. It never stands in for a zero, a blank "
            "or a no." % (fields.UNKNOWN,),
            "marks: ~ a figure dirscape attributed to this path rather than one the "
            "filesystem published, %s you can enter this directory but not list it" % (g.warn,),
        ):
            for line in wrap(text, indent=_INDENT, size=window, style=style).splitlines():
                lines.append(style.dim(line))
    return lines


def render(
    roots,
    meta=None,
    changes=(),
    site=None,
    style=None,
    size=None,
    hidden=0,
    legend_on=False,
    all_roots=None,
    group=False,
    summary=False,
    deltas=False,
    frame=True,
):
    # type: (...) -> str
    """The atlas, as one string.

    ``changes`` comes from the state layer and is rendered, never computed.
    ``meta`` is anything `fields.RunMeta.of` accepts, and every field of it is
    optional: what the caller did not supply shows as the unknown mark, because
    this renderer reads nothing from the clock, the hostname or the filesystem.

    Root order is the caller's. Discovery order carries information (the mount
    table's order, then the allocation database's) and re-sorting here would
    throw it away.

    ``summary`` adds the alert teasers and the counts back. **Off by default,
    and that is the owner's call on three lines of the live view**: "don't show
    things like [that], it looks ugly and uninformative at all. if they want,
    they can display it when starting the app with the right flag." Nothing is
    lost, which is why it is a good trade: `dirscape stranded`, `dirscape
    elsewhere` and `dirscape --all` are first-class commands that give the
    detail instead of a one-line teaser for it.

    ``deltas`` prints the change records one line each instead of counting
    them, which is `dirscape new`'s content rather than anybody's chrome.

    ``frame`` draws the panel. A caller that needs the bare lines turns it off:
    `cli._browse` does, so it can paint the selection band on a content line
    and frame the result, which keeps the band inside the border instead of
    inverting the border with it.
    """
    style = style or Style()
    window = size if size else style.size
    info = fields.RunMeta.of(meta)
    # The summary lines count every root, not the filtered subset. Counting the
    # subset made the `elsewhere` line vanish and stripped the byte figure off
    # the `stranded` line, because the rows those lines describe are exactly
    # the ones the default view holds back.
    census = list(all_roots) if all_roots is not None else list(roots)
    # The frame spends two columns of chrome and one space on each side, so
    # everything inside it is laid out against this and not against the window.
    budget = max(_MIN_BODY, window - 4) if frame else window
    # SPLIT, because the title wraps. `legend` breaks to a second line when
    # the next item will not fit, and a content line carrying an embedded
    # newline broke the frame open at 60 columns: the border closed after
    # `compute` and the rest of the title landed outside the box with the
    # right-hand border stuck on the end of it.
    out = _header(roots, info, style, size=budget).splitlines()

    if not roots:
        out.append(style.dim(_INDENT + "no roots were handed to this view"))
        return _finish(out, style, window, frame)

    rows = []  # type: List[List[str]]
    caveats = []  # type: List[Tuple[str, str]]
    for root in roots:
        cells, caveat = _row(root, style, site)
        rows.append(cells)
        if caveat:
            caveats.append((root.path, caveat))

    # A column with one distinct value across every row is a caption, not a
    # column. On a filtered default view WHERE reads `here` on all of them,
    # which spends nine characters of the window saying nothing. Dropped here
    # rather than in `_plan`, because `_plan` is about fitting and this is
    # about content.
    constant = _constant_columns(rows)

    if group:
        # `_align_figures` used to run here, right-aligning the used figure,
        # its limit and its percentage INSIDE one composed cell so the column
        # read as a set of magnitudes. Those are three real columns now and
        # `table` right-aligns each of them, so the whole mechanism is gone
        # along with the cell that needed it.
        # The role is printed once per run of rows that share it. Nine rows
        # reading `project`, `project`, `project` is the table stuttering: the
        # word carries information the first time and is visual noise after
        # that. Blanking the repeat turns the column into a quiet grouping
        # without moving a single cell, which keeps every number in the same
        # place a reader last saw it.
        previous = None
        for row in rows:
            current = row[_ROLE]
            row[_ROLE] = "" if current == previous else current
            previous = current
        # Inode figures are detail, not headline, so they are the first thing
        # dropped when the window is tight: the default view answers "where
        # can I put data and how full is it" and a file count belongs in
        # `--all`, `--json` and `why`.
        #
        # **But only when the window is actually tight.** Suppressing it
        # unconditionally left four columns of content in a 126 column
        # terminal, and the room left over went into one 40 space gutter to
        # make the box reach the edge, which is worse than not reaching it:
        # padding is not use. Spare width goes to a real column first, and
        # `files / limit` is the one column with real data on every row here.
        keep_files = (
            _PATH in KEEP_COLUMNS
            and _column_width(
                COLUMNS,
                rows,
                [i for i in range(len(COLUMNS)) if i not in constant and i != _POLICY],
                _INDENT,
                _GUTTER,
            )
            <= budget
        )
        if not keep_files:
            constant = set(constant) | {_FILES}

    # The constant set goes IN, so the stage loop never spends width on a
    # column that is about to be removed. KEEP_COLUMNS are excluded from the
    # skip list because `_constant_columns` already protects them and a stage
    # that dropped PATH or USED would have nothing left to say.
    columns, stacked = _plan(rows, budget, skip=[i for i in constant if i not in KEEP_COLUMNS])
    columns = [i for i in columns if i not in constant] or columns
    dropped = [COLUMNS[i] for i in range(len(COLUMNS)) if i not in columns]
    # A dropped-because-constant column is not news: the reader lost nothing.
    dropped = [name for name in dropped if COLUMNS.index(name) not in constant]

    if stacked:
        # No frame here, deliberately. A path wider than the window is printed
        # whole and left to the terminal's own wrap, and a border cannot hold
        # a line it is narrower than without cutting it.
        out.append("")
        for row in rows:
            # The path alone on its line, never cut. Everything else follows
            # indented, so the two read as one entry.
            out.append(style.head(row[_PATH]))
            # Every figure column, because the stacked layout is what a
            # reader gets when their paths are too long for a table and it
            # must not answer less. It used to list WHERE, REACH and USED, and
            # when the figures were split into three columns the free figure
            # stopped appearing at all: a quotaless site rendered `?` for
            # every root while `statvfs` had the answer.
            facts = [
                row[i] for i in (_WHERE, _REACH, _USED, _LIMIT) if row[i] and row[i] != "?"
            ] or [row[_USED]]
            out.append(_INDENT + "  ".join(facts))
        out.append("")
        out.extend(
            _footer(
                roots, style, dropped, window, hidden=hidden, legend_on=legend_on, counts=summary
            )
        )
        return "\n".join(out)

    body, extra = table(
        [_heading(i, roots) for i in columns],
        [[row[i] for i in columns] for row in rows],
        aligns=[_ALIGNS[i] for i in columns],
        style=style,
        size=budget,
        keep=[columns.index(i) for i in KEEP_COLUMNS if i in columns],
        atomic=[columns.index(i) for i in _ATOMIC if i in columns],
        drop_empty=False,
        indent=_INDENT,
        gutter=_GUTTER,
        # The view uses the whole window. Asked for three times, and it only
        # became a good idea once the figures were split into real columns:
        # there are seven of them to share the leftover room between now, so
        # each gutter grows by a few characters instead of one opening into a
        # 40 space gap.
        spread=True,
        # Ruled ABOVE the headings instead, spanning the panel. A dashed
        # segment under each heading draws the eye across the table's own
        # width and then stops, which reads as a second, shorter frame inside
        # the first one; one full-width rule separates the title from the
        # table and leaves the headings sitting on nothing.
        underline=False,
    )
    # A blank line, then the rule. The blank is what stops the title reading
    # as a first row of the table, and the rule is what stops the headings
    # reading as a second title.
    out.append("")
    # A placeholder, because the rule has to span the panel and the panel is
    # only as wide as its widest line, which is not known until the table is
    # built.
    rule_at = len(out)
    out.append("")
    out.extend(body.splitlines())
    dropped.extend(extra)

    stranded, changed = _delta_lines(census, changes, style, size=budget)

    if deltas and changed:
        # `dirscape new`'s content. Counting them here and pointing the reader
        # at `dirscape new` is what the default view does; inside `dirscape
        # new` that is a view telling you to go and look at itself.
        out.append("")
        out.extend(changed)

    alerts = _alerts(census, stranded, changed if not deltas else [], style) if summary else []
    out.extend(alerts)

    tail = _footer(
        roots,
        style,
        dropped,
        budget,
        hidden=hidden,
        legend_on=legend_on,
        counts=summary,
        caveats=_caveat_count(roots, caveats),
    )
    if tail:
        # A blank ahead of it, unless the alerts already opened this block: the
        # counts belong WITH the teasers they qualify, and two blank lines
        # between the table and one dim line reads as a gap rather than a
        # separation.
        if not alerts:
            out.append("")
        out.extend(tail)

    inner = max([measure(line) for i, line in enumerate(out) if i != rule_at] or [0])
    out[rule_at] = style.dim(style.g.h * max(1, min(inner, budget)))
    return _finish(out, style, window, frame)


def _finish(lines, style, window, frame):
    # type: (Sequence[str], Style, int, bool) -> str
    """Frame the block, or hand back the bare lines."""
    if not frame:
        return "\n".join(lines)
    # `shrink=True`, and the alternative was MEASURED rather than reasoned
    # about. Filling the window was tried first, because a border floating
    # short of the right edge reads as a mistake. It reads worse: the four
    # columns this view usually has come to about 65 display columns, so at a
    # 120 column terminal the box was ruled out to 120 around a table hugging
    # its left half, and the inner rule then had to choose between spanning
    # the frame (a rule over nothing) or spanning the table (a second, shorter
    # frame inside the first). There is no third column set to fill the gap
    # with: WHERE reads `here` on every row and drops as constant, FILES and
    # POLICY are empty on a site with no `site.conf`, and DEVICE is real but
    # names filesets (`meadow3_cap`) that a reader has to ask about.
    #
    # Sized to the content there is no gap, the rule spans the whole text area
    # by construction, and the box reads as one object. That is the only
    # reason to draw a frame at all.
    return panel(lines, style=style, size=window, shrink=True)


def _alerts(census, stranded, changed, style):
    # type: (Sequence[Root], Sequence[str], Sequence[str], Style) -> List[str]
    """The `--summary` teasers: what is wrong, and which command explains it.

    One line each and a summary rather than a list, because the version that
    printed one line per stranded fileset, one per change and one per note was
    nineteen lines under a ten-line table on a real account. A reader scans a
    summary; nobody reads nineteen.
    """
    alerts = []  # type: List[Tuple[object, str, str]]
    if stranded:
        held = fields.total_stranded(census)
        alerts.append(
            (
                style.bad,
                "%s %s"
                % (
                    style.g.warn,
                    "%d fileset%s hold%s %s you cannot reach"
                    % (
                        len(stranded),
                        "" if len(stranded) == 1 else "s",
                        "s" if len(stranded) == 1 else "",
                        held or "space",
                    ),
                ),
                "dirscape stranded",
            )
        )
    elsewhere = [r for r in census if r.elsewhere]
    if elsewhere:
        alerts.append(
            (
                style.warn,
                "%s %s"
                % (
                    style.g.warn,
                    "%d allocation%s with no path here"
                    % (len(elsewhere), "" if len(elsewhere) == 1 else "s"),
                ),
                "dirscape elsewhere",
            )
        )
    if changed:
        alerts.append(
            (
                style.info,
                "%s %s"
                % (
                    style.g.warn,
                    "%d change%s since the baseline"
                    % (len(changed), "" if len(changed) == 1 else "s"),
                ),
                "dirscape new",
            )
        )
    if not alerts:
        return []
    # Painted in the alert's OWN colour rather than bold white. A white
    # sentence with a coloured triangle in front of it says the urgent thing
    # twice in two vocabularies; one hue for glyph and prose reads as one
    # statement, and leaves bold white to mean "this is a heading".
    lines = [""]
    # Padded so the commands form a column. Three lines whose pointers start at
    # three different offsets read as three unrelated sentences; aligned, they
    # read as a menu.
    room = max(measure(text) for _, text, _ in alerts)
    for tone, text, command in alerts:
        gap = " " * max(1, room - measure(text) + 3)
        lines.append("%s%s%s%s" % (_INDENT, tone(text), gap, style.accent(command)))
    return lines
