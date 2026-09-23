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


def _device_label(device, lustre):
    # type: (str, bool) -> str
    """A device heading a reader can take in.

    A Lustre device is every server's network id and then the filesystem,
    162 characters on ACME before the word `acorn`, so the heading was cut off
    exactly where it said which filesystem it was. The name after `:/`, with
    any subdirectory the mount shows, is what `lfs df` and the site's docs use.
    """
    if lustre and ":/" in (device or ""):
        return "%s (lustre)" % (device.rpartition(":/")[2].rstrip("/"),)
    return device


def _quota_line(roots, style):
    # type: (Sequence[Root], Style) -> Tuple[str, str]
    """The group's figures: one quota, or the sum of several measurements.

    **First-that-has-any is right for a fileset and wrong for a walk**, and
    the difference is what the grouping means. Every path inside a fileset
    shares one quota, so any member reports the same number and the first will
    do. Walked figures are per DIRECTORY, so `/dev/sda1` holding
    `/scratch/local/jdoe42` at 0B and `/tmp` at 1.2G reported `0B used`: the
    first member's figure presented as the device's.
    """
    walked = [root for root in roots if (root.policy or {}).get("walked")]
    if walked and len(walked) == len(roots):
        total = 0
        for root in walked:
            row, _how, _why = fields.pick_row(root.quota, root.path, "blocks")
            if row is not None and row.used is not None:
                total += int(row.used)
        return fields.figure(fields.human_bytes(total), style) + style.dim(" used"), ""
    for root in roots:
        used, caveat = fields.used_cell(root, style)
        if used == fields.UNKNOWN:
            continue
        # **Composed from the same cells the table uses.** This called
        # `quota_cell`, the old single-cell form with a percentage baked in,
        # which is the shape the table gave up two rounds ago: so the tree
        # showed `868M / 30G (3%)` with the digits in the terminal's default
        # foreground and the percentage in its own blue, beside a table that
        # had neither. One view per fact, one rendering per fact.
        cap = fields.limit_cell(root, style)
        if fields.plain(cap) in (fields.NO_LIMIT, fields.UNKNOWN):
            # "11T of none" is not a sentence. With no ceiling to divide by
            # there is one figure, and it says what it is.
            return "%s %s" % (used, style.dim("used")), caveat
        return "%s %s %s" % (used, style.dim("of"), cap), caveat
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
        fstype = ""
        for _fileset, members in filesets:
            fstype = fstype or next((m.fstype for m in members if m.fstype), "")
        lustre = fstype.lower() == "lustre"
        label = _device_label(device, lustre) or fields.UNKNOWN
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
            # **An absent fileset is not an unknown one.** This printed `?`
            # and "(fileset not determined)", which is the mark this package
            # reserves for "nobody could measure it", against a device that
            # simply has no fileset structure: `/dev/sda1` is XFS, XFS has no
            # filesets, and the figures beside it were measured by walking the
            # directory. Nothing failed, so nothing should read as failed.
            walked = any((member.policy or {}).get("walked") for member in members)
            if fileset and lustre and fileset.isdigit():
                # `lfs project -d` names a project by number, and a bare
                # `13579` on a line of its own reads as a figure.
                name, note = "project %s" % (fileset,), ""
            elif fileset:
                name, note = fileset, ""
            elif walked:
                name, note = "measured", "  (added up by walking, no quota here)"
            elif lustre:
                # Not "no quota scopes": Lustre has project quotas, this
                # directory just carries none, and a user quota covers it.
                name, note = "no project", "  (a user quota covers the whole filesystem)"
            else:
                name, note = "no fileset", "  (this filesystem has no quota scopes)"
            line = "  %s %s" % (style.dim(f_stem), style.accent(name))
            if note:
                line += style.dim(note)
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
