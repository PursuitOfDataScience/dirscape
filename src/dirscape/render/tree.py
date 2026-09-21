"""Device, then fileset, then paths. The view that makes RD-18 a fact.

One device is not one quota. The GPFS device here is mounted at `/home`,
`/project`, `/software` and `/programs`, and those are four filesets with four
different limits. rapiDU's RD-18 was exactly this conflation, so this view
nests the three identities in the order they actually contain one another and
prints the quota on the FILESET line, where it belongs, rather than on the
device.

It is also where a symlink that leaves its quota scope gets named. `~/.cache`
pointing into `/project` bills against `/project`, and a user cleaning up their
home directory to get under a home quota will not find the space. The line says
which quota it is billed to, or the unknown mark when no root in hand can
answer that, because sending somebody to clean the wrong filesystem is worse
than saying nothing.
"""

from typing import Dict, List, Optional, Sequence, Tuple

from ..model import Root
from . import fields
from .style import Style, pad, width

__all__ = ["render", "group"]


def group(roots):
    # type: (Sequence[Root]) -> List[Tuple[str, List[Tuple[str, List[Root]]]]]
    """``[(device, [(fileset, [root, ...]), ...]), ...]``, in first-seen order.

    First-seen rather than sorted, because the caller's order carries
    information (the mount table's order, then the allocation database's) and a
    blank device is never merged into a named one: "I do not know which device
    this is on" is its own group, and folding it into another would be a claim.
    """
    devices = []  # type: List[Tuple[str, List[Tuple[str, List[Root]]]]]
    index = {}  # type: Dict[str, Dict[str, List[Root]]]
    for root in roots:
        device = root.device or ""
        fileset = root.fileset or ""
        if device not in index:
            index[device] = {}
            devices.append((device, []))
        bucket = index[device]
        if fileset not in bucket:
            bucket[fileset] = []
            for name, filesets in devices:
                if name == device:
                    filesets.append((fileset, bucket[fileset]))
                    break
        bucket[fileset].append(root)
    return devices


def _quota_line(roots, style):
    # type: (Sequence[Root], Style) -> Tuple[str, str]
    """The fileset's own figures, from the first root that has any."""
    for root in roots:
        text, caveat = fields.quota_cell(root, style)
        if text != fields.UNKNOWN:
            return text, caveat
    return fields.UNKNOWN, ""


def render(roots, style=None, size=None):
    # type: (Sequence[Root], Optional[Style], Optional[int]) -> str
    """The nesting, as one string."""
    style = style or Style()
    # Caveats already stated in this view, so each is said once.
    said = set()
    window = size if size else style.size
    g = style.g
    if not roots:
        return style.dim("no roots were handed to this view")

    # One path column width for the whole view, so the reach and where cells
    # line up across devices. Paths are not truncated, so a long one widens
    # the column rather than losing characters.
    path_room = max(width(r.path or fields.UNKNOWN) for r in roots)
    path_room = min(path_room, max(16, window - 24))

    out = []  # type: List[str]
    grouped = group(roots)
    for device, filesets in grouped:
        label = device or fields.UNKNOWN
        head = style.head(label)
        if not device:
            head += style.dim("  (device not determined)")
        out.append(head)
        if len(filesets) > 1:
            # The RD-18 statement, said in words as well as in shape.
            out.append(
                style.dim("  carries %d filesets, each with its own quota" % (len(filesets),))
            )
        for f_index, (fileset, members) in enumerate(filesets):
            last_fileset = f_index == len(filesets) - 1
            f_stem = g.last if last_fileset else g.branch
            # The continuation bar under a fileset that still has siblings, so
            # a reader can see which device a path three lines down belongs to.
            cont = "  " + ("  " if last_fileset else style.dim(g.pipe)) + "  "
            figures, caveat = _quota_line(members, style)
            name = fileset or fields.UNKNOWN
            line = "  %s %s" % (style.dim(f_stem), style.accent(name))
            if not fileset:
                line += style.dim("  (fileset not determined)")
            out.append("%s   %s" % (line, figures))
            if caveat:
                # First fragment only. Each backend appends its own caveat and
                # the blocks and inodes rows append the same ones again, so the
                # raw string ran to about two hundred characters and said the
                # same thing about an inferred mount three times. Repeated once
                # per fileset it was most of this view.
                first = caveat.split(";")[0].strip()
                # Said ONCE per view. Every fileset on a multi-mount device
                # carries the same inferred-mount caveat, so it appeared seven
                # times in a thirty-line view, which is the point at which a
                # caveat stops being read.
                if first and first not in said:
                    said.add(first)
                    out.append("%s%s %s" % (cont, style.muted(g.sep), style.muted(first)))
            for r_index, root in enumerate(members):
                last_root = r_index == len(members) - 1
                r_stem = g.last if last_root else g.branch
                path = root.path or fields.UNKNOWN
                out.append(
                    "%s%s %s  %s  %s"
                    % (
                        cont,
                        style.dim(r_stem),
                        pad(path, path_room),
                        pad(fields.reach_cell(root, style), 5),
                        fields.where_cell(root, style),
                    )
                )
                target = root.symlink_target
                if target:
                    deeper = cont + ("   " if last_root else style.dim(g.pipe) + " ")
                    out.append("%s%s %s" % (deeper, g.arrow, style.muted(fields.safe(target, 512))))
                    if root.crosses_boundary:
                        out.append(
                            "%s%s %s"
                            % (
                                deeper,
                                style.warn(g.warn),
                                style.warn(
                                    "crosses a quota boundary, billed to %s"
                                    % (fields.billed_to(root, roots),)
                                ),
                            )
                        )
    return "\n".join(out)
