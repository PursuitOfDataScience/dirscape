"""The native `--json` schema: everything the views know, machine readable.

Three rules make this different from dumping the objects:

1. **Every unknown carries a reason code.** `Verdict.to_json()` already emits
   `category`, which is a wire token a consumer may switch on, and `unknowns`
   below indexes every unanswered question in one place so a consumer does not
   have to walk the tree looking for them. A quota snapshot's category is not a
   `Verdict` and would otherwise be reachable by a different route, so the
   index is where the two become uniform.
2. **`schema_version` is at the top level**, because a consumer that has to
   guess the shape will guess wrong exactly once.
3. **`caveats` separates inference from measurement.** A quota row attributed
   to a path the backend published no mount for, a mount name that was guessed,
   a reading whose age is suspect, space the backend has not accounted for, a
   role derived from a path pattern: all of these are real and none of them is
   a measurement of the thing it describes.

There is deliberately no `counts` block. A derived count on the wire is a
second source of truth that can disagree with the array it counts, and every
count a consumer might want is `len()` of something already here.
"""

import json
from typing import Dict, List, Optional, Sequence

from ..model import Reach, Root
from . import fields

__all__ = ["render", "payload", "SCHEMA_VERSION"]


#: Bumped when a consumer would have to change. Adding a key is not a bump;
#: removing or re-meaning one is.
SCHEMA_VERSION = 1


def _run(meta):
    # type: (fields.RunMeta) -> Dict[str, object]
    """Run metadata, with every unsupplied field ABSENT rather than null.

    Absent and null would both have to be read as "not known", and one of them
    is enough. Absent is the one that does not tempt a consumer into printing
    the word null in a column.
    """
    out = {}  # type: Dict[str, object]
    for name in (
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
    ):
        value = getattr(meta, name, None)
        if value is not None:
            out[name] = value
    return out


def _unknowns(root):
    # type: (Root) -> List[Dict[str, object]]
    out = []  # type: List[Dict[str, object]]
    questions = [
        ("allocated", root.allocated),
        ("mounted", root.mounted),
        ("present", root.present),
        ("writable", root.writable),
        ("list", fields.list_verdict(root)),
        ("read", fields.read_verdict(root)),
        ("quota", fields.quota_verdict(root)),
        ("inode_quota", fields.snapshot_verdict(root.inode_quota, root.path, "files")),
    ]
    for name, verdict in questions:
        if verdict.known:
            continue
        entry = {
            "path": root.path,
            "field": name,
            # The wire token, which is the machine readable reason code, and
            # the human string beside it so a consumer never has to invent one
            # and print an enum member in a column (nodetop's NT-5).
            "category": verdict.category,
            "label": verdict.label,
            "durable": verdict.durable,
        }  # type: Dict[str, object]
        if verdict.reason:
            entry["reason"] = verdict.reason
        out.append(entry)
    return out


def _caveats(roots):
    # type: (Sequence[Root]) -> List[Dict[str, object]]
    out = []  # type: List[Dict[str, object]]
    for root in roots:
        for kind, snapshot in (("quota", root.quota), ("inode_quota", root.inode_quota)):
            if snapshot is None:
                continue
            row, how, why = fields.pick_row(
                snapshot, root.path, "blocks" if kind == "quota" else "files"
            )
            if row is not None and how == "inferred":
                out.append(
                    {
                        "kind": "inferred_row",
                        "path": root.path,
                        "field": kind,
                        "because": why,
                    }
                )
            if row is not None and row.guessed:
                out.append(
                    {
                        "kind": "guessed_mount",
                        "path": root.path,
                        "field": kind,
                        "because": "the mount for this row was inferred from its name",
                    }
                )
            if row is not None and row.in_doubt:
                out.append(
                    {
                        "kind": "space_in_doubt",
                        "path": root.path,
                        "field": kind,
                        "in_doubt": row.in_doubt,
                        "because": "allocated and not yet accounted for, so a du walk "
                        "will disagree",
                    }
                )
            if snapshot.time_note:
                out.append(
                    {
                        "kind": "reading_age",
                        "path": root.path,
                        "field": kind,
                        "because": snapshot.time_note,
                    }
                )
            if snapshot.figure_note:
                out.append(
                    {
                        "kind": "suspect_figure",
                        "path": root.path,
                        "field": kind,
                        "because": snapshot.figure_note,
                    }
                )
        if root.reach == Reach.CLOSED:
            out.append(
                {
                    "kind": "deduced",
                    "path": root.path,
                    "field": "read",
                    "because": "no file inside was opened; a directory that cannot be "
                    "entered holds nothing readable",
                }
            )
    if any(root.role for root in roots):
        out.append(
            {
                "kind": "advisory_role",
                "because": "a role comes from path patterns and site config, so it is a "
                "label rather than a measurement, and nothing decides access from it",
            }
        )
    return out


def _changes(roots, changes):
    # type: (Sequence[Root], Sequence[object]) -> List[Dict[str, object]]
    by_path = {r.path: r for r in roots if r.path}
    out = []  # type: List[Dict[str, object]]
    for record in changes:
        label, path, because = fields.change_fields(record)
        entry = {"label": label, "path": path}  # type: Dict[str, object]
        if because:
            entry["because"] = because
        root = by_path.get(path)
        if root is not None and root.renamed_from:
            # One move rather than a loss plus an arrival, which is the whole
            # reason the model carries the field.
            entry["renamed_from"] = root.renamed_from
        if label not in fields.CHANGE_LABELS:
            # Kept and flagged rather than dropped or silently normalised: a
            # label this version does not know is still a change the state
            # layer reported.
            entry["unrecognised_label"] = True
        out.append(entry)
    return out


def payload(roots, meta=None, changes=(), caveats=()):
    # type: (Sequence[Root], object, Sequence[object], Sequence[object]) -> Dict[str, object]
    """The whole run as one JSON-ready dict."""
    info = fields.RunMeta.of(meta)
    tool = {"name": info.tool}  # type: Dict[str, object]
    if info.version:
        tool["version"] = info.version
    unknowns = []  # type: List[Dict[str, object]]
    for root in roots:
        unknowns.extend(_unknowns(root))
    extra = []  # type: List[Dict[str, object]]
    for item in caveats:
        if isinstance(item, dict):
            extra.append(dict(item))
        else:
            extra.append({"kind": "note", "because": fields.safe(item)})
    out = {
        "schema_version": SCHEMA_VERSION,
        "tool": tool,
        "roots": [root.to_json() for root in roots],
        "changes": _changes(roots, changes),
        "unknowns": unknowns,
        "caveats": _caveats(roots) + extra,
    }  # type: Dict[str, object]
    run = _run(info)
    if run:
        out["run"] = run
    return out


def render(roots, meta=None, changes=(), caveats=(), indent=2):
    # type: (Sequence[Root], object, Sequence[object], Sequence[object], Optional[int]) -> str
    """The JSON text. Keys sorted, so two runs diff cleanly."""
    return json.dumps(
        payload(roots, meta=meta, changes=changes, caveats=caveats),
        indent=indent,
        sort_keys=True,
    )
