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
KEEP_COLUMNS = (_PATH, _WHERE)

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
    ((_POLICY, _FILES, _ROLE, _REACH, _USED), False),
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
    return [_PATH, _WHERE], False, True


def _header(roots, meta, style):
    # type: (Sequence[Root], fields.RunMeta, Style) -> str
    name = meta.tool
    if meta.version:
        name = "%s %s" % (name, fields.safe(meta.version, limit=32))
    node = fields.safe(meta.node_class, limit=32)
    if not node or node == "unknown":
        # `discover.mounts.node_class` answers the literal string "unknown".
        # That is the tool's own vocabulary for an unanswered question, and on
        # screen the answer to an unanswered question is the mark.
        node = fields.UNKNOWN
    items = [
        style.accent(name),
        "host %s" % (fields.safe(meta.host, limit=64) or fields.UNKNOWN,),
        "%s node" % (node,),
        fields.device_count(roots, meta),
        "took %s" % (fields.human_duration(meta.elapsed_s),),
    ]
    if meta.baseline_at is None:
        items.append("baseline %s" % (fields.UNKNOWN,))
    else:
        items.append(
            "baseline %s (%s)"
            % (
                fields.date_text(meta.baseline_at),
                fields.age_phrase(meta.baseline_at, meta.now),
            )
        )
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


def _footer(roots, style, dropped, window):
    # type: (Sequence[Root], Style, Sequence[str], int) -> List[str]
    g = style.g
    lines = []  # type: List[str]
    if dropped:
        lines.append(
            style.dim(
                "  %s %d more column%s (widen, or --json): %s"
                % (
                    g.ellipsis,
                    len(dropped),
                    "" if len(dropped) == 1 else "s",
                    ", ".join(dropped),
                )
            )
        )
    stuck = [r for r in roots if fields.unmeasured(r)]
    if stuck:
        target = stuck[0].path if len(stuck) == 1 else "<path>"
        lines.append(
            "  %s could not be measured: %s"
            % (
                style.warn("%d root%s" % (len(stuck), "" if len(stuck) == 1 else "s")),
                style.accent("dirscape why %s" % (target,)),
            )
        )
    # The handoff, and it is deliberate: this tool never walks a tree, so
    # bytes-by-directory is a sibling tool's job and saying so is better than
    # having a user wait for a walk that is not coming.
    lines.append("  what is filling a root? %s" % (style.accent("rdu <path>"),))
    # Wrapped, because a legend that overflows the window is the one line
    # guaranteed to wrap badly and it explains the columns above it.
    for text in (
        "reach: r list, w write, x traverse, - refused, %s not determined, "
        "%s traverse only" % (fields.UNKNOWN, g.warn),
        "marks: ~ figure attributed rather than published, %s space allocated and "
        "not yet accounted for" % (g.doubt,),
    ):
        for line in wrap(text, indent="  ", size=window, style=style).splitlines():
            lines.append(style.dim(line))
    return lines


def render(roots, meta=None, changes=(), site=None, style=None, size=None):
    # type: (Sequence[Root], object, Sequence[object], object, Optional[Style], Optional[int]) -> str
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

    columns, show_bar, stacked = _plan(rows_bar, rows_flat, window)
    rows = rows_bar if show_bar else rows_flat
    dropped = [COLUMNS[i] for i in range(len(COLUMNS)) if i not in columns]

    if stacked:
        out.append("")
        for row in rows:
            # The path alone on its line, never cut. Everything else follows
            # indented, so the two read as one entry.
            out.append(style.head(row[_PATH]))
            facts = [row[i] for i in (_WHERE, _REACH, _USED) if row[i]]
            out.append("    " + "  ".join(facts))
        out.append("")
        out.extend(_footer(roots, style, dropped, window))
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

    stranded, deltas = _delta_lines(roots, changes, style)
    if stranded:
        out.append("")
        out.append(
            "%s %s"
            % (
                style.bad(style.g.bullet),
                style.head("stranded: space you hold here and cannot reach"),
            )
        )
        out.extend(stranded)
    if deltas:
        out.append("")
        heading = "changes"
        if info.baseline_at is not None:
            heading = "changes since %s" % (fields.date_text(info.baseline_at),)
        out.append("%s %s" % (style.accent(style.g.bullet), style.head(heading)))
        out.extend(deltas)

    notes = _notes(roots, caveats, style)
    if notes:
        out.append("")
        out.append("%s %s" % (style.accent(style.g.bullet), style.head("notes")))
        out.extend(notes)

    out.append("")
    out.extend(_footer(roots, style, dropped, window))
    return "\n".join(out)
