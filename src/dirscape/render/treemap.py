"""A squarified treemap across roots, sized by used bytes. A glance, not a browser.

Deliberately small: it fits 80 columns, it holds a dozen cells, and it answers
one question, which is "where is my space". Browsing belongs to `ncdu`, and the
`ncdu` export in this package is how you get there.

**A root whose usage is unknown cannot be sized, so it is listed beside the map
and never drawn.** Giving it a zero-area cell would hide it, and giving it any
area at all would invent a figure: both are the lie this package exists to
avoid. A root MEASURED at a size too small to draw is listed separately from
one that could not be measured, because "tiny" and "unknown" are different
answers and a single list would blur them.

Two channels, two meanings. **Area is bytes used. Colour is how full the quota
is**, where a quota is known. Neither is load bearing on its own: the legend
prints every figure the map encodes, so the view survives `NO_COLOR`, ASCII
glyphs and a reader who cannot distinguish the hues.
"""

from typing import List, Optional, Sequence, Tuple

from ..model import Root
from . import fields
from .style import Style, width, wrap

__all__ = ["render", "squarify"]


#: A terminal cell is roughly twice as tall as it is wide, so the layout is
#: computed on a canvas stretched vertically and then squashed back. Without
#: this every cell comes out a wide letterbox and the squarifying is wasted.
CELL_ASPECT = 2.0

#: Below this a cell cannot hold a border and a key letter, so its root is
#: moved to the "too small to draw" list instead of being drawn as a sliver
#: nobody can read or click.
MIN_CELL_W = 5
MIN_CELL_H = 3

_KEYS = "abcdefghijklmnopqrstuvwxyz"


def _worst(row, length):
    # type: (Sequence[float], float) -> float
    total = sum(row)
    if total <= 0 or length <= 0:
        return float("inf")
    biggest = max(row)
    smallest = min(row)
    if smallest <= 0:
        return float("inf")
    return max(
        (length * length * biggest) / (total * total),
        (total * total) / (length * length * smallest),
    )


def _place(areas, x, y, dx, dy, out):
    # type: (List[float], float, float, float, float, List[Tuple[float, float, float, float]]) -> None
    if not areas:
        return
    if len(areas) == 1 or dx <= 0 or dy <= 0:
        out.append((x, y, dx, dy))
        for _ in areas[1:]:
            out.append((x, y, 0.0, 0.0))
        return
    length = dy if dx >= dy else dx
    row = [areas[0]]
    index = 1
    while index < len(areas):
        if _worst(row + [areas[index]], length) <= _worst(row, length):
            row.append(areas[index])
            index += 1
        else:
            break
    total = sum(row)
    if total <= 0:
        for _ in areas:
            out.append((x, y, 0.0, 0.0))
        return
    if dx >= dy:
        side = total / dy
        offset = y
        for area in row:
            height = dy * area / total
            out.append((x, offset, side, height))
            offset += height
        _place(areas[len(row) :], x + side, y, dx - side, dy, out)
    else:
        side = total / dx
        offset = x
        for area in row:
            span = dx * area / total
            out.append((offset, y, span, side))
            offset += span
        _place(areas[len(row) :], x, y + side, dx, dy - side, out)


def squarify(values, w, h):
    # type: (Sequence[float], int, int) -> List[Tuple[int, int, int, int]]
    """Integer ``(x, y, w, h)`` cells for ``values``, largest first.

    The Bruls squarified layout, on a canvas stretched by :data:`CELL_ASPECT`
    so the algorithm reasons about squares and the result reads as squares in a
    terminal. Values must be positive and sorted descending; a zero would ask
    for a cell of no area, which this view refuses to draw.
    """
    total = float(sum(values))
    if total <= 0 or w <= 0 or h <= 0:
        return []
    tall = h * CELL_ASPECT
    scale = (w * tall) / total
    areas = [float(v) * scale for v in values]
    raw = []  # type: List[Tuple[float, float, float, float]]
    _place(list(areas), 0.0, 0.0, float(w), float(tall), raw)
    cells = []
    for x, y, dx, dy in raw:
        x0 = int(round(x))
        y0 = int(round(y / CELL_ASPECT))
        x1 = int(round(x + dx))
        y1 = int(round((y + dy) / CELL_ASPECT))
        cells.append((x0, y0, max(0, x1 - x0), max(0, y1 - y0)))
    return cells


def _sized(roots):
    # type: (Sequence[Root]) -> Tuple[List[Tuple[Root, int, Optional[float]]], List[Root]]
    """Split into ``(measured, unsized)``. A measured zero is still measured."""
    measured = []  # type: List[Tuple[Root, int, Optional[float]]]
    unsized = []  # type: List[Root]
    for root in roots:
        row, _, _ = fields.pick_row(root.quota, root.path, "blocks")
        if row is None or row.used is None:
            unsized.append(root)
        else:
            measured.append((root, row.used, row.fraction))
    return measured, unsized


def _draw(cells, style, labels):
    # type: (Sequence[Tuple[int, int, int, int]], Style, Sequence[Tuple[str, str, Optional[float]]]) -> List[str]
    g = style.g
    if not cells:
        return []
    canvas_w = max(x + w for x, _, w, _ in cells)
    canvas_h = max(y + h for _, y, _, h in cells)
    grid = [[" "] * canvas_w for _ in range(canvas_h)]
    tones = [[None] * canvas_w for _ in range(canvas_h)]  # type: List[List[Optional[float]]]

    for index, (x, y, w, h) in enumerate(cells):
        key, label, fraction = labels[index]
        for row in range(y, y + h):
            for col in range(x, x + w):
                edge_top = row == y
                edge_bottom = row == y + h - 1
                edge_left = col == x
                edge_right = col == x + w - 1
                if edge_top and edge_left:
                    ch = g.tl
                elif edge_top and edge_right:
                    ch = g.tr
                elif edge_bottom and edge_left:
                    ch = g.bl
                elif edge_bottom and edge_right:
                    ch = g.br
                elif edge_top or edge_bottom:
                    ch = g.h
                elif edge_left or edge_right:
                    ch = g.v
                else:
                    ch = g.tile
                grid[row][col] = ch
                tones[row][col] = fraction
        # The label goes inside, on the first interior row. When the cell is
        # too narrow for the text the key letter goes in instead, and the
        # legend resolves it: a clipped path in a picture would be a guess the
        # reader cannot check.
        inner_w = w - 2
        if h >= 3 and inner_w >= 1:
            text = label if width(label) <= inner_w else key
            if width(text) <= inner_w:
                start = x + 1
                for offset, ch in enumerate(text):
                    # Bounded by the cell's own right border rather than by
                    # `width`, since a combining mark measures one column and
                    # occupies two list slots.
                    if start + offset >= x + w - 1:
                        break
                    grid[y + 1][start + offset] = ch
                    tones[y + 1][start + offset] = fraction

    out = []  # type: List[str]
    for row in range(canvas_h):
        pieces = []  # type: List[str]
        run = ""
        run_tone = tones[row][0] if canvas_w else None
        for col in range(canvas_w):
            tone = tones[row][col]
            if tone != run_tone:
                pieces.append(style.tint(run, run_tone) if run_tone is not None else run)
                run = ""
                run_tone = tone
            run += grid[row][col]
        pieces.append(style.tint(run, run_tone) if run_tone is not None else run)
        out.append("".join(pieces).rstrip())
    return out


def render(roots, style=None, size=None, height=None):
    # type: (Sequence[Root], Optional[Style], Optional[int], Optional[int]) -> str
    """The treemap, its legend, and the roots it could not size."""
    style = style or Style()
    window = size if size else style.size
    g = style.g
    if not roots:
        return style.dim("no roots were handed to this view")

    measured, unsized = _sized(roots)
    measured.sort(key=lambda item: item[1], reverse=True)
    canvas_w = max(MIN_CELL_W * 2, min(window - 2, 76))
    # Eight rows, not twelve: this is a glance view and a map taller than the
    # legend beside it starts to look like an explorer, which it is not.
    canvas_h = height if height else 8

    drawn = []  # type: List[Tuple[Root, int, Optional[float]]]
    too_small = []  # type: List[Tuple[Root, int]]
    candidates = [item for item in measured if item[1] > 0]
    too_small.extend((root, used) for root, used, _ in measured if used <= 0)

    # Lay out, then retire any cell that came back unreadably thin and lay out
    # again. Retiring rather than shrinking the others keeps every drawn cell
    # proportional to its bytes, which is the only thing that makes area mean
    # anything.
    cells = []  # type: List[Tuple[int, int, int, int]]
    while candidates:
        cells = squarify([item[1] for item in candidates], canvas_w, canvas_h)
        thin = [
            index for index, (_, _, w, h) in enumerate(cells) if w < MIN_CELL_W or h < MIN_CELL_H
        ]
        if not thin:
            drawn = list(candidates)
            break
        for index in reversed(thin):
            root, used, _ = candidates.pop(index)
            too_small.append((root, used))
    else:
        cells = []

    out = []  # type: List[str]
    total = sum(used for _, used, _ in drawn)
    if drawn:
        labels = []  # type: List[Tuple[str, str, Optional[float]]]
        for index, (root, used, fraction) in enumerate(drawn):
            key = _KEYS[index] if index < len(_KEYS) else "*"
            tail = root.path.rstrip("/").rsplit("/", 1)[-1] or root.path
            labels.append((key, "%s %s" % (tail, fields.human_bytes(used)), fraction))
        out.extend(_draw(cells, style, labels))
        out.append("")
        for index, (root, used, fraction) in enumerate(drawn):
            key = _KEYS[index] if index < len(_KEYS) else "*"
            share = ""
            if total > 0:
                share = " %d%% of what was sized" % (int(round(100.0 * used / total)),)
            out.append(
                "  %s %s  %s%s"
                % (
                    style.accent(key),
                    root.path,
                    style.tint(fields.human_bytes(used), fraction),
                    style.dim(share),
                )
            )
        out.append(
            style.dim(
                "  %s sized across %d root%s"
                % (
                    fields.human_bytes(total),
                    len(drawn),
                    "" if len(drawn) == 1 else "s",
                )
            )
        )
    elif too_small:
        out.append(style.dim("  every measured root is too small to draw at this size"))
    else:
        out.append(style.dim("  nothing here could be sized, so there is no map"))

    if too_small:
        out.append("")
        out.append(style.head("  too small to draw"))
        for root, used in too_small:
            out.append("  %s %s  %s" % (style.dim(g.sep), root.path, fields.human_bytes(used)))

    if unsized:
        out.append("")
        # Beside the map, never in it. This list is the honest alternative to
        # an invented area.
        out.append(style.head("  not sized, because usage is unknown"))
        for root in unsized:
            out.append("  %s %s  %s" % (style.warn(fields.UNKNOWN), root.path, fields.UNKNOWN))
            # Wrapped rather than clipped: this sentence is the only place the
            # reader learns why the map is missing a root.
            for line in wrap(
                fields.trouble(root), indent="      ", size=window, style=style
            ).splitlines():
                out.append(style.muted(line))

    out.append("")
    out.append(
        style.dim("  area is bytes used, colour is how full the quota is where one is known")
    )
    return "\n".join(out)
