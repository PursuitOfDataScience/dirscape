"""Roots as rows, capabilities as columns. The honest-unknown rule, on screen.

    exists | mounted | list | read | write | quota | purge | backup

Every cell is one character and there are THREE of them, not two: confirmed
yes, confirmed no, and "I could not determine". That third character is the
product. A tool that renders an unanswered question as a no is `nodetop`'s
NT-1, where 21 partitions measured as refusing came back looking merely
unchecked, and this view is the shape that mistake cannot take.

Two of the eight columns have a different provenance and the legend says so.
`exists` through `quota` are probes. `purge` and `backup` are published site
policy, read through `Site.policy_for`, and they go through
`fields.policy_glyph` rather than through a `Verdict`: the contract has no
durable category for "the site publishes no such policy", so forging one would
put `OK` beside a `false` in `--json`. Same three characters, different source,
stated rather than blurred.

`extra` lets the plugin layer supply real verdicts for a column the model has
no field for, without this view inventing any.
"""

import re
from typing import Dict, List, Optional, Sequence

from ..model import Root, Verdict
from . import fields
from .style import Style, legend, table, wrap

__all__ = ["render", "COLUMNS", "PROBED", "FROM_POLICY", "DROP_PRIORITY"]


#: In order. Read as a sentence: does it exist, is it here, can I look, can I
#: read, can I write, is there a quota, will it be purged, is it backed up.
COLUMNS = (
    "exists",
    "mounted",
    "list",
    "read",
    "write",
    "quota",
    "purge",
    "backup",
)

#: Columns answered by a probe.
PROBED = COLUMNS[:6]

#: Columns answered by published site configuration.
FROM_POLICY = COLUMNS[6:]

#: Given up first when the window is narrow: the advisory columns before the
#: probed ones, and the least-probed probe before the core three. The path is
#: never dropped and never truncated.
DROP_PRIORITY = ("backup", "purge", "read", "write", "quota", "list", "mounted")


def _verdicts(root):
    # type: (Root) -> Dict[str, Verdict]
    return {
        "exists": root.present,
        "mounted": root.mounted,
        "list": fields.list_verdict(root),
        "read": fields.read_verdict(root),
        "write": root.writable,
        "quota": fields.quota_verdict(root),
    }


def _paint(glyph, style):
    # type: (str, Style) -> str
    """Colour a cell by what it says, and only ever as reinforcement.

    The three glyphs already differ, so colour adds nothing a reader needs.
    That is on purpose: under `NO_COLOR`, on a 16 colour console and in a piped
    log the grid has to read exactly the same.
    """
    if glyph == style.g.ok:
        return style.ok(glyph)
    if glyph == style.g.bad:
        return style.bad(glyph)
    return style.dim(glyph)


def _cells(root, style, site, extra):
    # type: (Root, Style, object, Optional[Dict[str, Dict[str, Verdict]]]) -> List[str]
    g = style.g
    override = (extra or {}).get(root.path) or {}
    verdicts = _verdicts(root)
    policy = fields.merged_policy(root, site)
    out = []  # type: List[str]
    for name in COLUMNS:
        if name in override:
            out.append(_paint(fields.verdict_glyph(override[name], g), style))
        elif name in verdicts:
            out.append(_paint(fields.verdict_glyph(verdicts[name], g), style))
        elif name == "purge":
            # `purge_days` is the key a site writes; `purge` is accepted too,
            # because a sysadmin writing the column heading they saw is not
            # wrong about what they meant.
            value = policy.get("purge_days", policy.get("purge"))
            out.append(_paint(fields.policy_glyph(value, g), style))
        else:
            out.append(_paint(fields.policy_glyph(policy.get(name), g), style))
    return out


_ANSI = re.compile("\033\\[[0-9;?]*[A-Za-z]")


def _plain(text):
    # type: (str) -> str
    """The glyph a reader sees, with the colour removed."""
    return _ANSI.sub("", text)


def render(roots, site=None, style=None, size=None, extra=None):
    # type: (Sequence[Root], object, Optional[Style], Optional[int], object) -> str
    """The capability grid, as one string."""
    style = style or Style()
    window = size if size else style.size
    g = style.g
    if not roots:
        return style.dim("no roots were handed to this view")

    # Lower case, to match the other five headings in this very row and the
    # atlas's. `PATH` in capitals beside `exists`, `mounted` and `quota` was
    # one table shouting at itself.
    headers = ["path"] + list(COLUMNS)
    # The path in the PRIMARY tier, as everywhere else. It was unstyled here,
    # which means the terminal's default foreground: the one column carrying
    # the row's identity inheriting whatever tone the surrounding shell uses,
    # while every cell beside it was deliberately placed in a tier.
    rows = [
        [style.text(root.path) if root.path else fields.UNKNOWN] + _cells(root, style, site, extra)
        for root in roots
    ]

    # Drop any column that is the unknown mark on EVERY row. Three of them
    # were, on a site with no published policy: `read` by construction, since
    # this tool never opens a file inside a directory, and `purge` and
    # `backup` because nobody had written a `site.conf`. Three columns of
    # solid `?` is what teaches a reader that the marks mean nothing, and the
    # legend below still explains the ones that survive.
    unknown_cell = fields.UNKNOWN
    keepable = [0]
    for index in range(1, len(headers)):
        seen = {_plain(row[index]).strip() for row in rows}
        if seen != {unknown_cell}:
            keepable.append(index)
    if len(keepable) > 1:
        blank = [headers[i] for i in range(len(headers)) if i not in keepable]
        headers = [headers[i] for i in keepable]
        rows = [[row[i] for i in keepable] for row in rows]
    else:
        blank = []
    body, dropped = table(
        headers,
        rows,
        aligns=["left"] + ["center"] * len(COLUMNS),
        style=style,
        size=window,
        keep=(0,),
        # The path is atomic: an ellipsis inside a path makes a different path,
        # and this view's rows are identified by nothing else.
        atomic=(0,),
        priority=[headers.index(name) for name in DROP_PRIORITY if name in headers],
        drop_empty=False,
    )
    out = [body, ""]
    if dropped:
        out.append(
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
    out.append(
        legend(
            [
                "%s measured yes" % (style.ok(g.ok),),
                "%s measured no" % (style.bad(g.bad),),
                "%s could not determine" % (style.dim(fields.UNKNOWN),),
            ],
            style,
            size=window,
        )
    )
    # Explain only the columns that SURVIVED. The legend used to describe
    # `purge`, `backup` and `read` unconditionally, so on a site with no
    # published policy it spent three lines explaining three columns the view
    # had just dropped for being entirely unknown.
    shown = set(headers)
    notes = []  # type: List[str]
    if any(name in shown for name in FROM_POLICY):
        notes.append(
            "%s to %s come from probes; %s and %s come from published site policy"
            % (PROBED[0], PROBED[-1], FROM_POLICY[0], FROM_POLICY[1])
        )
    if "read" in shown:
        notes.append(
            "read means a file inside, which this tool does not open, so a column of %s "
            "here is an answer and not a gap" % (fields.UNKNOWN,)
        )
    if blank:
        notes.append(
            "%s: not shown, because %s unknown on every row"
            % (
                ", ".join(blank),
                "it was" if len(blank) == 1 else "they were",
            )
        )
    for text in notes:
        for line in wrap(text, indent="  ", size=window, style=style).splitlines():
            out.append(style.dim(line))
    return "\n".join(out)
