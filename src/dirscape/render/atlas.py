"""The default view: one row per root, and what you can actually do with it.

    ROLE | PATH | WHERE | REACH | USED / QUOTA | FILES / LIMIT | POLICY

`WHERE` is the column this tool exists for. The site quota wrapper here reports
`/cfs3` and `/cfs4` from a node where neither path exists, and "allocated, and
not mounted on this node" is the most useful sentence the tool can produce. So
the node class is in the header beside the hostname: mount visibility is a
property of the node you are standing on, not of the filesystem.

**Narrow terminals are handled by dropping columns, never by truncating a
path.** A shortened path is a different path, and the reader cannot tell which
characters went. The stages, in order, with the reason for each place:

===== ====================== =====================================================
stage what goes              why it goes there
===== ====================== =====================================================
1     POLICY                 advisory site text, the widest cell, least urgent
2     FILES / LIMIT          inodes are the second quota axis, bytes are the first
3     ROLE                   a heuristic label, and the path already implies it
4     the bar                drop the picture, keep the digits it illustrates
5     REACH                  three characters, and `dirscape why` can say more
6     USED / QUOTA           the last fact to go
===== ====================== =====================================================

`PATH` and `WHERE` are never dropped. Below the width those two need, the view
STACKS instead: the full path on its own line with the remaining cells indented
beneath it. A path wider than the window is printed whole and left to the
terminal's own wrap, which loses no characters.
"""

import re
from typing import Dict, List, Optional, Sequence, Tuple

from ..model import Root, VerdictCategory, category_label
from . import fields
from .style import Style, legend, table, wrap
from .style import width as measure

__all__ = ["render", "COLUMNS", "DROP_STAGES", "KEEP_COLUMNS"]


#: Column headings, in order. The index of each is what `DROP_STAGES` names.
COLUMNS = (
    "ROLE",
    "PATH",
    "WHERE",
    "REACH",
    "USED / QUOTA",
    "FILES / LIMIT",
    "POLICY",
)

_ROLE, _PATH, _WHERE, _REACH, _USED, _FILES, _POLICY = range(7)

#: Never dropped. The path identifies the row and WHERE carries the answer this
#: tool is for.
KEEP_COLUMNS = (_PATH, _USED)

#: Each stage is ``(columns to drop, draw the bar)``. Walked in order until one
#: fits the window. Documented in the module docstring, and asserted in
#: `tests/test_render_atlas.py` so the order cannot drift silently.
DROP_STAGES = (
    ((), True),
    ((_POLICY,), True),
    ((_POLICY, _FILES), True),
    ((_POLICY, _FILES, _ROLE), True),
    ((_POLICY, _FILES, _ROLE), False),
    ((_POLICY, _FILES, _ROLE, _REACH), False),
    # WHERE goes before USED, and that ordering is the point. The last stage
    # used to drop USED, so at 40 columns the table degraded to a bare list of
    # paths with no number anywhere on it, which is not a smaller version of
    # this tool's answer but the absence of one. WHERE is context and reads
    # `here` on nearly every row anyway; USED is the answer.
    ((_POLICY, _FILES, _ROLE, _REACH, _WHERE), False),
)

_ALIGNS = ("left", "left", "left", "left", "left", "right", "left")

#: Neither figure column may be squeezed. Six columns of `1744/2000G` is
#: `1744/...`, a fraction with its denominator eaten, which is not a smaller
#: version of the fact but a different and false one.
_ATOMIC = (_PATH, _USED, _FILES)

_NOTE_LIMIT = 4


def _path_cell(root):
    # type: (Root) -> str
    """The path, or the allocation location when there is no path.

    An allocated root that is not mounted here HAS no path, deliberately:
    turning `cfs4/hpc-staff` into `/cfs4/hpc-staff` is a guess, and the
    allocation database is not a mount table. But rendering it as `?` throws
    away the only identifying thing about the row, so the location is shown
    with a marker saying what it is. Without this the ELSEWHERE rows printed
    `?` in every column and were indistinguishable from each other.
    """
    if root.path:
        held = (root.policy or {}).get("contains")
        if isinstance(held, int) and held > 0:
            # The fold count, which was being stored and never shown. The
            # selection rule hides a root's subdirectories when the whole tree
            # is yours, and the promise was that the parent keeps a count so
            # the information is not lost. It was lost: twenty dataset
            # collections folded into one row and nothing on screen said so.
            return "%s +%d" % (root.path, held)
        return root.path
    location = root.policy.get("allocation_location") if root.policy else None
    if location:
        # The trailing marker is not decoration. It is the difference between
        # "you can cd here" and "your allocation database calls it this".
        return "%s (location)" % (location,)
    return fields.UNKNOWN


def _row(root, style, site, show_bar):
    # type: (Root, Style, object, bool) -> Tuple[List[str], str]
    used, caveat = fields.quota_cell(root, style, show_bar=show_bar)
    files, inode_caveat = fields.inode_cell(root, style)
    cells = [
        fields.role_cell(root),
        _path_cell(root),
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

    `USED / QUOTA` is composed per row as one string, so the numbers land
    wherever their own width puts them and a reader cannot compare down the
    column:

        836M / 30G  ...
        11T / no limit
        928K / no limit
        22G / 100G  ...

    Aligning on the separator makes the same four rows read as a column of
    magnitudes, which is the entire reason to put numbers in a table:

         836M / 30G       ...
          11T / no limit
         928K / no limit
          22G / 100G      ...

    Done here rather than by splitting the cell into two real columns, because
    the figure, its limit and its bar are one statement and `_ATOMIC` already
    treats them as one unit for fitting. Widths are measured with `width`, not
    `len`, since the cells carry colour and block characters.
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


def _column_width(headers, rows, columns, indent=""):
    # type: (Sequence[str], Sequence[Sequence[str]], Sequence[int], str) -> int
    if not columns:
        return 0
    total = 2 * (len(columns) - 1) + measure(indent)
    for index in columns:
        widest = measure(headers[index])
        for row in rows:
            widest = max(widest, measure(row[index]))
        total += widest
    return total


def _plan(rows_bar, rows_flat, window):
    # type: (Sequence[Sequence[str]], Sequence[Sequence[str]], int) -> Tuple[List[int], bool, bool]
    """Choose the column set: ``(columns, show_bar, stacked)``.

    The first stage that fits wins. When none does, the caller stacks, which is
    the only degradation left that does not shorten a path.
    """
    for dropped, show_bar in DROP_STAGES:
        columns = [i for i in range(len(COLUMNS)) if i not in dropped]
        rows = rows_bar if show_bar else rows_flat
        if _column_width(COLUMNS, rows, columns) <= window:
            return columns, show_bar, False
    return [_PATH, _USED], False, True


def _header(roots, meta, style):
    # type: (Sequence[Root], fields.RunMeta, Style) -> str
    # One line, and only facts that change what a reader does next. The tool's
    # own name and version were dropped: a user who wants them types
    # `--version`, and printing them on every run costs a line of the window
    # for something nobody reads twice.
    node = fields.safe(meta.node_class, limit=32)
    if not node or node == "unknown":
        # `discover.mounts.node_class` answers the literal string "unknown".
        # That is the tool's own vocabulary for an unanswered question, and on
        # screen the answer to an unanswered question is the mark.
        node = fields.UNKNOWN
    host = fields.safe(meta.host, limit=64) or fields.UNKNOWN
    items = [
        style.accent(host.split(".")[0]),
        node,
        fields.device_count(roots, meta),
        fields.human_duration(meta.elapsed_s),
    ]
    if meta.baseline_at is not None:
        items.append("baseline %s" % (fields.age_phrase(meta.baseline_at, meta.now),))
    return legend(items, style, size=style.size, indent="")


def _delta_lines(roots, changes, style):
    # type: (Sequence[Root], Sequence[object], Style) -> Tuple[List[str], List[str]]
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
                    "  %s %s  %s"
                    % (
                        colour(g.warn),
                        path or fields.UNKNOWN,
                        style.muted(because or fields.UNKNOWN),
                    )
                )
                continue
            mark = g.warn if name == "gone" else " "
            deltas.append(
                "  %s %s  %s  %s"
                % (
                    colour(mark),
                    colour(name.ljust(room)),
                    path or fields.UNKNOWN,
                    style.muted(because or fields.UNKNOWN),
                )
            )
    return stranded, deltas


def _notes(roots, caveats, style):
    # type: (Sequence[Root], Sequence[Tuple[str, str]], Style) -> List[str]
    lines = []  # type: List[str]
    for path, text in caveats:
        lines.append("  %s %s  %s" % (style.muted(style.g.sep), path, style.muted(text)))
    for root in roots:
        if root.symlink_target and root.crosses_boundary:
            lines.append(
                "  %s %s %s %s  %s"
                % (
                    style.warn(style.g.warn),
                    root.path,
                    style.g.arrow,
                    root.symlink_target,
                    style.warn(
                        "crosses a quota boundary, billed to %s" % (fields.billed_to(root, roots),)
                    ),
                )
            )
    if len(lines) > _NOTE_LIMIT:
        hidden = len(lines) - _NOTE_LIMIT
        lines = lines[:_NOTE_LIMIT]
        lines.append(style.dim("  %s %d more (see --json)" % (style.g.sep, hidden)))
    return lines


def _footer(roots, style, dropped, window, hidden=0, legend_on=False, notes=0):
    # type: (Sequence[Root], Style, Sequence[str], int, int, bool) -> List[str]
    """At most two lines, and often none.

    The previous version printed six: a dropped-column notice, an unmeasured
    count, the `rdu` handoff and a two-line glyph legend, every single run.
    That is four lines of chrome under a table, and a legend reprinted on every
    invocation is read once and skipped forever after. The glyphs are now
    explained by `--legend`, and the rest is folded into one line.
    """
    g = style.g
    bits = []  # type: List[str]

    if hidden:
        bits.append("%d hidden (%s)" % (hidden, style.accent("--all")))
    stuck = [r for r in roots if fields.unmeasured(r)]
    if stuck:
        bits.append("%d unmeasured (%s)" % (len(stuck), style.accent("dirscape why <path>")))
    if dropped:
        bits.append(
            "%d column%s hidden (%s)"
            % (len(dropped), "" if len(dropped) == 1 else "s", style.accent("--json"))
        )

    lines = []  # type: List[str]
    if bits:
        lines.append(style.dim("  " + (" %s " % (g.sep,)).join(bits)))

    if legend_on:
        for text in (
            "reach: r list, w write, x traverse, - refused, %s not determined, "
            "%s traverse only" % (fields.UNKNOWN, g.warn),
            "marks: ~ figure attributed rather than published, %s space allocated "
            "and not yet accounted for" % (g.doubt,),
        ):
            for line in wrap(text, indent="  ", size=window, style=style).splitlines():
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
    """
    style = style or Style()
    window = size if size else style.size
    info = fields.RunMeta.of(meta)
    # The summary lines count every root, not the filtered subset. Counting the
    # subset made the `elsewhere` line vanish and stripped the byte figure off
    # the `stranded` line, because the rows those lines describe are exactly
    # the ones the default view holds back.
    census = list(all_roots) if all_roots is not None else list(roots)
    out = [_header(roots, info, style)]

    if not roots:
        out.append(style.dim("  no roots were handed to this view"))
        return "\n".join(out)

    rows_bar = []  # type: List[List[str]]
    rows_flat = []  # type: List[List[str]]
    caveats = []  # type: List[Tuple[str, str]]
    for root in roots:
        cells, caveat = _row(root, style, site, True)
        rows_bar.append(cells)
        rows_flat.append(_row(root, style, site, False)[0])
        if caveat:
            caveats.append((root.path, caveat))

    # A column with one distinct value across every row is a caption, not a
    # column. On a filtered default view WHERE reads `here` on all of them,
    # which spends nine characters of the window saying nothing. Dropped here
    # rather than in `_plan`, because `_plan` is about fitting and this is
    # about content.
    constant = _constant_columns(rows_bar)

    if group:
        _align_figures((rows_bar, rows_flat), _USED)
        # The role is printed once per run of rows that share it. Nine rows
        # reading `project`, `project`, `project` is the table stuttering: the
        # word carries information the first time and is visual noise after
        # that. Blanking the repeat turns the column into a quiet grouping
        # without moving a single cell, which keeps every number in the same
        # place a reader last saw it.
        for block in (rows_bar, rows_flat):
            previous = None
            for row in block:
                current = row[_ROLE]
                row[_ROLE] = "" if current == previous else current
                previous = current
        # Inode figures are detail, not headline. The default view answers
        # "where can I put data and how full is it"; a file count belongs in
        # `--all`, `--json` and `why`, where a reader has already asked for
        # more than a glance.
        constant = set(constant) | {_FILES}

    columns, show_bar, stacked = _plan(rows_bar, rows_flat, window)
    rows = rows_bar if show_bar else rows_flat
    columns = [i for i in columns if i not in constant] or columns
    dropped = [COLUMNS[i] for i in range(len(COLUMNS)) if i not in columns]
    # A dropped-because-constant column is not news: the reader lost nothing.
    dropped = [name for name in dropped if COLUMNS.index(name) not in constant]

    if stacked:
        out.append("")
        for row in rows:
            # The path alone on its line, never cut. Everything else follows
            # indented, so the two read as one entry.
            out.append(style.head(row[_PATH]))
            facts = [row[i] for i in (_WHERE, _REACH, _USED) if row[i]]
            out.append("    " + "  ".join(facts))
        out.append("")
        out.extend(_footer(roots, style, dropped, window, hidden=hidden, legend_on=legend_on))
        return "\n".join(out)

    body, extra = table(
        [COLUMNS[i] for i in columns],
        [[row[i] for i in columns] for row in rows],
        aligns=[_ALIGNS[i] for i in columns],
        style=style,
        size=window,
        keep=[columns.index(i) for i in KEEP_COLUMNS if i in columns],
        atomic=[columns.index(i) for i in _ATOMIC if i in columns],
        drop_empty=False,
    )
    out.append("")
    out.append(body)
    dropped.extend(extra)

    stranded, deltas = _delta_lines(census, changes, style)

    # Panels are SUMMARISED by default and expanded by a subcommand. The
    # previous version printed one line per stranded fileset, one per change
    # and one per note, and on a real account that was nineteen lines under
    # the table restating what the table had already implied. A reader scans
    # a summary; nobody reads nineteen.
    alerts = []  # type: List[Tuple[str, str]]
    if stranded:
        held = fields.total_stranded(census)
        alerts.append(
            (
                "%s %s"
                % (
                    style.bad(style.g.warn),
                    style.head(
                        "%d fileset%s hold%s %s you cannot reach"
                        % (
                            len(stranded),
                            "" if len(stranded) == 1 else "s",
                            "s" if len(stranded) == 1 else "",
                            held or "space",
                        )
                    ),
                ),
                "dirscape stranded",
            )
        )
    elsewhere = [r for r in census if r.elsewhere]
    if elsewhere:
        alerts.append(
            (
                "%s %s"
                % (
                    style.warn(style.g.warn),
                    style.head(
                        "%d allocation%s with no path here"
                        % (len(elsewhere), "" if len(elsewhere) == 1 else "s")
                    ),
                ),
                "dirscape elsewhere",
            )
        )
    if deltas:
        alerts.append(
            (
                "%s %s"
                % (
                    style.info(style.g.warn),
                    style.head(
                        "%d change%s since the baseline"
                        % (len(deltas), "" if len(deltas) == 1 else "s")
                    ),
                ),
                "dirscape new",
            )
        )
    if alerts:
        out.append("")
        # Padded so the commands form a column. Three lines whose pointers
        # start at three different offsets read as three unrelated sentences;
        # aligned, they read as a menu.
        room = max(measure(text) for text, _ in alerts)
        for text, command in alerts:
            pad = " " * max(1, room - measure(text) + 3)
            out.append("  %s%s%s" % (text, pad, style.accent(command)))

    # The note count joins the footer rather than claiming a line of its own.
    # Two separate one-line advisories both ending in `dirscape why <path>` is
    # the same sentence twice.
    notes = len(_notes(roots, caveats, style))
    tail = _footer(roots, style, dropped, window, hidden=hidden, legend_on=legend_on, notes=notes)
    if tail:
        out.extend(tail)
    return "\n".join(out)
