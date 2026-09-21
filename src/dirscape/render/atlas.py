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
COLUMNS = (
    "role",
    "path",
    "where",
    "reach",
    "used / quota",
    "files / limit",
    "policy",
)

_ROLE, _PATH, _WHERE, _REACH, _USED, _FILES, _POLICY = range(7)

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
    # WHERE goes before USED, and that ordering is the point. The last stage
    # used to drop USED, so at 40 columns the table degraded to a bare list of
    # paths with no number anywhere on it, which is not a smaller version of
    # this tool's answer but the absence of one. WHERE is context and reads
    # `here` on nearly every row anyway; USED is the answer.
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

_ALIGNS = ("left", "left", "left", "left", "left", "right", "left")

#: Neither figure column may be squeezed. Six columns of `1744/2000G` is
#: `1744/...`, a fraction with its denominator eaten, which is not a smaller
#: version of the fact but a different and false one.
_ATOMIC = (_PATH, _USED, _FILES)

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
        cell = root.path
        held = (root.policy or {}).get("contains")
        if isinstance(held, int) and held > 0:
            # The fold count, which was being stored and never shown. The
            # selection rule hides a root's subdirectories when the whole tree
            # is yours, and the promise was that the parent keeps a count so
            # the information is not lost. It was lost: twenty dataset
            # collections folded into one row and nothing on screen said so.
            cell = "%s +%d" % (root.path, held)
        if style is None or not style.enabled:
            return cell
        lead, sep, leaf = root.path.rpartition("/")
        fold = cell[len(root.path) :]
        return style.dim(lead + sep) + leaf + style.dim(fold)
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
    used, caveat = fields.quota_cell(root, style)
    files, inode_caveat = fields.inode_cell(root, style)
    cells = [
        fields.role_cell(root, style),
        _path_cell(root, style),
        fields.where_cell(root, style),
        fields.reach_cell(root, style),
        used,
        files,
        fields.policy_cell(root, site),
    ]
    both = "; ".join(c for c in (caveat, inode_caveat) if c)
    return cells, both


_ANSI = re.compile("\033\\[[0-9;?]*[A-Za-z]")


def _strip(text):
    # type: (str) -> str
    """The text a reader sees, with the escapes removed."""
    return _ANSI.sub("", text).strip()


def _align_figures(blocks, index):
    # type: (Sequence[List[str]], int) -> None
    """Right-align the used and limit figures inside an already-built cell.

    `used / quota` is composed per row as one string, so the numbers land
    wherever their own width puts them and a reader cannot compare down the
    column:

        836M / 30G    3%
        11T / no limit
        928K / no limit
        22G / 100G   22%

    Aligning on the separator makes the same four rows read as a column of
    magnitudes, which is the entire reason to put numbers in a table:

         836M / 30G          3%
          11T / no limit
         928K / no limit
          22G / 100G        22%

    Done here rather than by splitting the cell into two real columns, because
    the figure, its limit and its percentage are one statement and `_ATOMIC`
    already treats them as one unit for fitting. Widths are measured with
    `width`, not `len`, since the cells carry colour.
    """
    parts = []  # type: List[Optional[Tuple[str, str, str]]]
    # The third element is the limit token for a quota figure and the trailing
    # word for a capacity fallback; the middle element says which.
    for block in blocks:
        for row in block:
            cell = row[index]
            head, sep, tail = cell.partition(" / ")
            if sep:
                parts.append((head, sep, tail))
                continue
            # A capacity fallback reads "886G free" and has no separator, so
            # the split above skips it and the number sits hard against the
            # column edge while every quota figure is right-aligned. Treated
            # as a figure with an empty limit so it joins the same column.
            bare, space, word = cell.rpartition(" ")
            # Compared with the escapes stripped. The cell is dimmed, so the
            # last token is `free\x1b[0m` and an equality test against "free"
            # silently failed, which is why the capacity rows were never
            # aligned at all: `886G free` sat four columns in while every
            # quota figure sat five.
            if space and _strip(word) == "free":
                parts.append((bare, "", word))
                continue
            parts.append(None)

    lead = max([measure(p[0]) for p in parts if p] or [0])
    # The limit is padded to the widest limit TOKEN, not the widest tail: the
    # tail includes the bar and the percentage, and padding to that would push
    # short rows into a gulf of whitespace.
    limits = []  # type: List[int]
    for p in parts:
        if p:
            limits.append(measure(p[2].split("  ")[0]))
    room = max(limits or [0])

    cursor = 0
    for block in blocks:
        for row in block:
            p = parts[cursor]
            cursor += 1
            if not p:
                continue
            head, sep, tail = p
            pad = " " * max(0, lead - measure(head))
            if not sep:
                # The capacity fallback: aligned on the number, and the word
                # follows it rather than a limit.
                row[index] = "%s%s %s" % (pad, head, tail)
                continue
            token, gap, rest = tail.partition("  ")
            padded = token + " " * max(0, room - measure(token))
            row[index] = "%s%s / %s%s%s" % (pad, head, padded, gap, rest)


def _constant_columns(rows):
    # type: (Sequence[Sequence[str]]) -> set
    """Column indexes whose value never varies, excluding the ones that must stay.

    PATH is never dropped, and USED is never dropped even if every row happens
    to read the same, because both are the answer rather than the context.
    """
    if len(rows) < 2:
        return set()
    keep = {_PATH, _USED}
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


def _plan(rows, window):
    # type: (Sequence[Sequence[str]], int) -> Tuple[List[int], bool]
    """Choose the column set: ``(columns, stacked)``.

    The first stage that fits wins. When none does, the caller stacks, which is
    the only degradation left that does not shorten a path.
    """
    for dropped in DROP_STAGES:
        columns = [i for i in range(len(COLUMNS)) if i not in dropped]
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
    host = fields.safe(meta.host, limit=64) or fields.UNKNOWN
    user = fields.safe(meta.user, limit=64) or fields.UNKNOWN
    items = [
        style.head(fields.safe(meta.tool, limit=32) or "dirscape"),
        style.accent(user),
        style.accent(host.split(".")[0]),
        style.muted(node),
        style.muted(fields.device_count(roots, meta)),
    ]
    if meta.baseline_at is not None:
        items.append(style.dim("baseline %s" % (fields.age_phrase(meta.baseline_at, meta.now),)))
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
        for text in (
            "reach: r list, w write, x traverse, - refused, %s not determined, "
            "%s traverse only" % (fields.UNKNOWN, g.warn),
            "marks: ~ figure attributed rather than published, %s space allocated "
            "and not yet accounted for" % (g.doubt,),
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
        _align_figures((rows,), _USED)
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
        # Inode figures are detail, not headline. The default view answers
        # "where can I put data and how full is it"; a file count belongs in
        # `--all`, `--json` and `why`, where a reader has already asked for
        # more than a glance.
        constant = set(constant) | {_FILES}

    columns, stacked = _plan(rows, budget)
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
            facts = [row[i] for i in (_WHERE, _REACH, _USED) if row[i]]
            out.append(_INDENT + "  ".join(facts))
        out.append("")
        out.extend(
            _footer(
                roots, style, dropped, window, hidden=hidden, legend_on=legend_on, counts=summary
            )
        )
        return "\n".join(out)

    body, extra = table(
        [COLUMNS[i] for i in columns],
        [[row[i] for i in columns] for row in rows],
        aligns=[_ALIGNS[i] for i in columns],
        style=style,
        size=budget,
        keep=[columns.index(i) for i in KEEP_COLUMNS if i in columns],
        atomic=[columns.index(i) for i in _ATOMIC if i in columns],
        drop_empty=False,
        indent=_INDENT,
        gutter=_GUTTER,
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
    return panel(lines, style=style, size=window)


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
