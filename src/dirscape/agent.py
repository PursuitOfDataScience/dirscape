"""The agent's view: the answers the table gives, as data an agent can act on.

A person reads the table, and an agent should not have to scrape it. `--json`
is the other extreme: the whole model, 87 KB for the eleven rows of the
default view on the development node, most of it backend provenance from which
a consumer would have to re-derive the very cells the table already shows.
Neither is what an agent asking "where can I put 2 TB" needs.

So this module builds one flat record per place, **from the same cell
functions the table and `why` use**, and derives nothing of its own. That is
the rule that keeps the two audiences from being told different things: a
figure an agent quotes is the figure the person beside it is looking at.

Three conventions, stated once so no consumer has to guess:

* `used`, `quota`, `free`, `files` and `max_files` are the cells the table
  and `why` print, verbatim: `855M`, `30G`, `none` where no limit is
  enforced, `?` where nobody could measure it, and a leading `~` where
  dirscape attributed a figure the backend did not publish for that path.
* `used_bytes`, `quota_bytes`, `free_bytes`, `files_count` and
  `max_files_count` are the exact numbers, and null exactly where the cell is
  `?` or `none`. The cell beside each says which, so a null is never
  ambiguous.
* `can_read` and `can_write` are true, false, or null for "not settled". A
  null is never a no, which is this package's first rule.

`cli` owns the pipeline and the view helpers these records reuse, so this
module imports it at the top and `cli` imports this one lazily, inside the
commands that need it. The cycle therefore never exists at import time, which
matters on 3.6, where a partially initialised module cannot satisfy a
`from . import` of itself.
"""

import os
import re
import time
from typing import Any, Dict, List, Optional, Sequence, Tuple

from . import __version__, cli
from .discover import copies_for_path, restates_source
from .model import Reach, VerdictCategory, sanitize
from .render import fields
from .render.style import Glyphs, Style, plain
from .sitecfg import ROLES

__all__ = [
    "KINDS",
    "LEGEND",
    "SCHEMA_VERSION",
    "PathError",
    "changes_payload",
    "explain",
    "explain_payload",
    "parse_kinds",
    "parse_size",
    "paths_payload",
    "place",
    "recover_payload",
]

#: Bumped when a consumer would have to change, exactly as `jsonout`'s is.
#: Adding a key is not a bump; removing or re-meaning one is.
SCHEMA_VERSION = 1

#: What `kind` can be, in the order the table groups by.
KINDS = tuple(cli._ROLE_ORDER) + tuple(role for role in ROLES if role not in cli._ROLE_ORDER)

#: The conventions, carried IN the payload, because an agent handed this JSON
#: by a shell has not read this docstring or the README, and `quota_scope:
#: fileset` read without it is the claim "you have used 155T".
LEGEND = (
    "used, quota, free, files and max_files are the text dirscape prints: `?` means nobody "
    "could measure it and is never zero, `none` means no limit is enforced, a leading `~` "
    "means dirscape attributed the figure. *_bytes and *_count are exact, null beside `?` or "
    "`none`. can_read and can_write null means not settled, not no. quota_scope other than "
    "user means the figures count everyone sharing that quota. free_limited_by filesystem "
    "means free is the filesystem's headroom, shared with everyone on it."
)

#: Colourless and ASCII, so every cell comes back as plain text whatever the
#: terminal is. The size is fixed because nothing here wraps.
_PLAIN = Style(color=False, glyphs=Glyphs.ascii(), size=200)


class PathError(ValueError):
    """A path no root answers for. The message is the one `why` prints."""


# --------------------------------------------------------------------------
# Arguments
# --------------------------------------------------------------------------

_SIZE = re.compile(r"^(\d+(?:\.\d*)?|\.\d+)([kmgtpe]?)(?:i?b)?$")
_MULTIPLE = {"": 0, "k": 1, "m": 2, "g": 3, "t": 4, "p": 5, "e": 6}


def parse_size(text):
    # type: (object) -> int
    """`2T`, `500G`, `1.5t`, `2TiB`, `2TB` or a plain byte count, as bytes.

    Binary multiples throughout, the same `du -h` letters the table prints, so
    `--min-free 30G` keeps exactly the rows whose `free` cell reads 30G or
    more. `TB` is read as `T` rather than as a decimal terabyte: the two differ
    by 10%, and a filter that disagreed with the column beside it by 10% would
    be worse than either convention. Raises ValueError, naming what was typed.
    """
    if isinstance(text, bool):
        raise ValueError("%r is not a size" % (text,))
    if isinstance(text, int):
        if text < 0:
            raise ValueError("a size cannot be negative")
        return text
    raw = str(text if text is not None else "").strip()
    found = _SIZE.match(raw.lower().replace(" ", ""))
    if not found:
        raise ValueError(
            "%r is not a size; use a number optionally followed by K, M, G, T or P "
            "(binary, as du -h prints them), e.g. 500G or 2T" % (raw,)
        )
    return int(float(found.group(1)) * (1024 ** _MULTIPLE[found.group(2)]))


def parse_kinds(text):
    # type: (object) -> Tuple[str, ...]
    """`scratch` or `project,scratch`, checked against the vocabulary.

    Checked rather than passed through, because a typo is otherwise an empty
    answer, and an agent told "there is no scratch here" about `scrach` goes
    on to act on it.
    """
    if text is None or text == "":
        return ()
    items = text if isinstance(text, (list, tuple)) else str(text).split(",")
    kinds = []  # type: List[str]
    for item in items:
        kind = str(item).strip().lower()
        if not kind:
            continue
        if kind not in KINDS:
            raise ValueError("%r is not a kind; use one of %s" % (kind, ", ".join(KINDS)))
        if kind not in kinds:
            kinds.append(kind)
    return tuple(kinds)


# --------------------------------------------------------------------------
# One place
# --------------------------------------------------------------------------


def _number(row, name):
    # type: (Any, str) -> Optional[int]
    """``row.<name>`` as an int, or None when there is no row or no figure."""
    if row is None:
        return None
    value = getattr(row, name, None)
    if value is None or isinstance(value, bool):
        return None
    return int(value)


def _can_read(root):
    # type: (Any) -> Optional[bool]
    reach = getattr(root, "reach", Reach.UNKNOWN)
    if reach == Reach.LISTABLE:
        return True
    if reach in (Reach.TRAVERSE, Reach.CLOSED):
        return False
    return None


def _can_write(root):
    # type: (Any) -> Optional[bool]
    verdict = getattr(root, "writable", None)
    if verdict is None:
        return None
    if verdict.confirmed:
        return True
    if verdict.refuted:
        return False
    return None


def _purge_days(root, site):
    # type: (Any, Any) -> Optional[int]
    """Days after writing that the site deletes files here, when it says so.

    Null both when nothing is published and when the site publishes "never";
    `retention`, beside it, says which of the two it was.
    """
    days = fields.merged_policy(root, site).get("purge_days")
    if isinstance(days, bool) or not isinstance(days, (int, float)) or days <= 0:
        return None
    return int(days)


def _snapshot_count(root):
    # type: (Any) -> Optional[int]
    """Copies the filesystem is keeping: a count, 0 for "keeps none", or null.

    Zero only for a MEASURED none, a snapshot directory that holds nothing for
    this path. No snapshot mechanism found is null, because a site can back up
    to tape without exposing one and silence must not read as "no copies".
    """
    verdict = getattr(root, "recoverable", None)
    if verdict is None or verdict.category == VerdictCategory.NOT_PROBED:
        return None
    copies = list(getattr(root, "snapshots", None) or [])
    if copies:
        return len(copies)
    if verdict.refuted:
        return 0
    return None


def place(root, run=None, detail=False):
    # type: (Any, Any, bool) -> Dict[str, object]
    """One root as the table's answers. ``detail`` adds what `why -v` says.

    Every key is present on every record, null where there is nothing to say,
    so a consumer can index without guarding and `jq` never meets a missing
    field on one row of twenty.
    """
    site = getattr(run, "site", None)
    policy = getattr(root, "policy", None) or {}
    path = getattr(root, "path", "") or ""
    block, _how, _why = fields.pick_row(getattr(root, "quota", None), path, "blocks")
    files, _fhow, _fwhy = fields.pick_row(getattr(root, "inode_quota", None), path, "files")
    used, _caveat = fields.used_cell(root, _PLAIN)
    room, limited_by = fields.free_space(root)
    record = {
        "path": path,
        "kind": getattr(root, "role", "") or None,
        "fstype": getattr(root, "fstype", "") or None,
        "access": fields.access_words(root),
        "can_read": _can_read(root),
        "can_write": _can_write(root),
        "used": plain(used),
        "used_bytes": _number(block, "used"),
        "quota": plain(fields.limit_cell(root, _PLAIN)),
        "quota_bytes": _number(block, "limit"),
        # Whose usage `used` counts. `user` is yours alone; `group`, `fileset`
        # and `project` count everyone sharing the quota, which is the
        # difference between "you have written 29T" and "the project has".
        "quota_scope": (getattr(block, "scope", "") or None) if block is not None else None,
        "free": plain(fields.free_cell(root, _PLAIN)),
        "free_bytes": room,
        # `quota` when your own allowance is what runs out first, `filesystem`
        # when the filesystem's headroom is, and that is space you share.
        "free_limited_by": limited_by or None,
        "files": plain(fields.file_count_cell(root, _PLAIN)),
        "files_count": _number(files, "used"),
        "max_files": plain(fields.inode_limit_cell(root, _PLAIN)),
        "max_files_count": _number(files, "limit"),
        "retention": cli._keeping_phrase(root, site),
        "purge_days": _purge_days(root, site),
        "snapshots": _snapshot_count(root),
        "changes": [fields.safe(label, limit=32) for label in getattr(root, "labels", ()) or ()],
        # Other places whose quota the symlinked folders in here are billed
        # against: the `~/.cache` trap, where a home looks empty to `du` from
        # the inside and its conda envs are filling `/project`.
        "symlinked_to": [fields.safe(p, limit=4096) for p in policy.get("crosses_to") or ()],
    }  # type: Dict[str, object]
    if detail:
        record.update(_detail(root, record))
    return record


def _detail(root, record):
    # type: (Any, Dict[str, object]) -> Dict[str, object]
    """The `why -v` layer: where each figure came from, and what is odd here."""
    policy = getattr(root, "policy", None) or {}
    path = getattr(root, "path", "") or ""
    copies = list(getattr(root, "snapshots", None) or [])
    newest = copies[0] if copies else None
    restore = None  # type: Optional[str]
    if newest is not None and cli._mine(root):
        # Only where the reader can write, for the reason `why` gives: on a
        # read-only tree the command cannot work and should not be offered.
        restore = cli._copy_back(newest.path, path)
    writable = getattr(root, "writable", None)
    notes = [
        note
        for note in getattr(root, "notes", ()) or ()
        if "resolves to" not in note and not restates_source(note, getattr(root, "sources", ()))
    ]
    return {
        "device": getattr(root, "device", "") or None,
        "fileset": getattr(root, "fileset", "") or None,
        "source": cli._quota_source(root),
        "write_checked_by": (writable.source or None)
        if writable is not None and writable.known
        else None,
        "in_doubt": cli._uncounted(root) or None,
        "found_by": cli._because_phrase(root) or None,
        "newest_snapshot": newest.path if newest is not None else None,
        "restore": restore,
        "symlinks_out": [dict(item) for item in policy.get("symlinks_out") or ()],
        "findings": [sentence for _glyph, sentence in cli._findings(root, _PLAIN)],
        "unknown": _unknown(root, record),
        "notes": notes[:6],
    }


def _unknown(root, record):
    # type: (Any, Dict[str, object]) -> Dict[str, str]
    """Why each null that matters is null, in a sentence per field.

    The table shows `?` and moves on; an agent has to tell a person whether to
    wait, retry, or ask the site, and that depends on the reason.
    """
    out = {}  # type: Dict[str, str]
    if record.get("used") == fields.UNKNOWN:
        out["used"] = fields.trouble(root)
    elif record.get("quota") == fields.UNKNOWN:
        out["quota"] = fields.trouble(root)
    if record.get("can_write") is None:
        verdict = getattr(root, "writable", None)
        said = (verdict.reason or verdict.label) if verdict is not None else "not probed"
        out["can_write"] = "%s; `dirscape --probe-write` settles it by writing a file" % (said,)
    verdict = getattr(root, "recoverable", None)
    if (
        record.get("snapshots") is None
        and verdict is not None
        and verdict.category != VerdictCategory.NOT_PROBED
    ):
        out["snapshots"] = verdict.reason or verdict.label
    return out


# --------------------------------------------------------------------------
# Payloads
# --------------------------------------------------------------------------


def _iso(epoch):
    # type: (Optional[float]) -> Optional[str]
    """Local time as RFC 3339, `2026-09-23T05:10:02-05:00`, or None."""
    if epoch is None:
        return None
    try:
        stamp = time.strftime("%Y-%m-%dT%H:%M:%S%z", time.localtime(float(epoch)))
    except (TypeError, ValueError, OSError, OverflowError):
        return None
    # `%z` is `-0500`; RFC 3339 wants the colon.
    if len(stamp) > 5 and stamp[-5] in "+-" and stamp[-4:].isdigit():
        stamp = stamp[:-2] + ":" + stamp[-2:]
    return stamp


def _header(run, view):
    # type: (Any, str) -> Dict[str, Any]
    """Which machine, which user, and when: the facts every answer depends on.

    The node matters more than it looks. `/cfs3` is mounted on login nodes
    and not on compute ones, so an answer is about the machine it was measured
    on, and an agent passing it to a batch job has to know which that was.
    """
    meta = getattr(run, "meta", None)
    identity = getattr(run, "identity", None)

    def pick(name, fallback=""):
        # type: (str, str) -> Optional[object]
        value = getattr(meta, name, None) if meta is not None else None
        if not value and identity is not None:
            value = getattr(identity, fallback or name, None)
        return value or None

    return {
        "tool": "dirscape",
        "version": __version__,
        "schema_version": SCHEMA_VERSION,
        "view": view,
        "host": pick("host", "hostname"),
        "cluster": pick("cluster"),
        "node_class": pick("node_class"),
        "user": pick("user"),
        "measured_at": _iso(getattr(meta, "now", None) if meta is not None else None),
    }


def _wanted(record, writable, kinds, min_free):
    # type: (Dict[str, object], bool, Sequence[str], Optional[int]) -> bool
    if writable and record.get("can_write") is not True:
        return False
    if kinds and (record.get("kind") or "other") not in kinds:
        return False
    if min_free is not None:
        # An unknown is not enough room. Keeping it would answer "where fits
        # 2T" with a place nobody measured.
        room = record.get("free_bytes")
        if not isinstance(room, int) or room < min_free:
            return False
    return True


def _stranded(root):
    # type: (Any) -> Dict[str, object]
    block, _how, _why = fields.pick_row(getattr(root, "quota", None), root.path, "blocks")
    used, _caveat = fields.used_cell(root, _PLAIN)
    return {
        "path": root.path or None,
        "fileset": getattr(root, "fileset", "") or None,
        "used": plain(used),
        "used_bytes": _number(block, "used"),
    }


def _elsewhere(root):
    # type: (Any) -> Dict[str, object]
    """An allocation the database names and this machine has no path for.

    GB decimal, as the database publishes it, which is how `dirscape
    elsewhere` converts it too.
    """
    policy = getattr(root, "policy", None) or {}
    gb = policy.get("allocation_gb")
    size = None  # type: Optional[int]
    if isinstance(gb, (int, float)) and not isinstance(gb, bool) and gb > 0:
        size = int(gb * 1000 * 1000 * 1000)
    accounts = policy.get("allocation_accounts")
    return {
        "location": fields.safe(policy.get("allocation_location"), limit=256) or None,
        "path": getattr(root, "path", "") or None,
        "kind": getattr(root, "role", "") or None,
        "size": fields.human_bytes(size),
        "size_bytes": size,
        "accounts": [fields.safe(a, limit=64) for a in accounts]
        if isinstance(accounts, (list, tuple))
        else [],
    }


def _sorted(roots):
    # type: (Sequence[Any]) -> List[Any]
    order = {name: index for index, name in enumerate(cli._ROLE_ORDER)}
    return sorted(
        roots,
        key=lambda r: (order.get(getattr(r, "role", "") or "other", 99), r.path or ""),
    )


def paths_payload(run, show_all=False, writable=False, kinds=(), min_free=None):
    # type: (Any, bool, bool, Sequence[str], Optional[int]) -> Dict[str, Any]
    """Every place you can put data, as records, plus what the list leaves out.

    The rows are the table's rows, in the table's order, unless ``show_all``.
    Every root lands in exactly one of `paths`, `stranded`, `elsewhere` or the
    `hidden` count, so nothing the sweep found is silently dropped and nothing
    is counted twice.
    """
    rows, _held = cli._visible(run, show_all)  # type: List[Any], int
    if show_all:
        rows = _sorted(rows)
    listed = [r for r in rows if r.path and not r.stranded and not r.elsewhere]
    stranded = [r for r in run.roots if r.stranded and not r.elsewhere]
    elsewhere = [r for r in run.roots if r.elsewhere]
    accounted = {id(r) for r in listed} | {id(r) for r in stranded} | {id(r) for r in elsewhere}
    hidden = sum(1 for r in run.roots if id(r) not in accounted)

    records = [place(r, run) for r in listed]
    kept = [r for r in records if _wanted(r, writable, kinds, min_free)]

    out = _header(run, "paths")
    out["legend"] = LEGEND
    out["paths"] = kept
    if writable or kinds or min_free is not None:
        out["filters"] = {
            "writable": bool(writable),
            "kinds": list(kinds),
            "min_free_bytes": min_free,
        }
        out["filtered_out"] = len(records) - len(kept)
    out["hidden"] = hidden
    if hidden:
        out["hidden_note"] = (
            "%d more roots are folded out of this list: filesystem roots, aliases, and "
            "subdirectories of a place already listed. Ask for all of them "
            "(`dirscape paths --all`, or all=true) to list every one." % (hidden,)
        )
    out["stranded"] = [_stranded(r) for r in stranded]
    out["elsewhere"] = [_elsewhere(r) for r in elsewhere]
    out["warnings"] = cli._surfaceable(run)
    return out


def explain_payload(run, path):
    # type: (Any, str) -> Dict[str, Any]
    """Which place governs one path, with everything `why -v` would say.

    A path that does not exist yet is answered for the place it would be
    created in, because "can I write my output to /scratch/.../run42" is asked
    BEFORE the directory exists. `exists` says which case this is.

    Raises PathError when no root may answer, with `why`'s own sentence.
    """
    return explain(run, path)[1]


def explain(run, path):
    # type: (Any, str) -> Tuple[Any, Dict[str, Any]]
    """`explain_payload`, and the root it answered with, for `why --json`."""
    spot = cli._locate(run, path, allow_missing=True)
    out = _header(run, "explain")
    out["asked"] = sanitize(path, limit=4096)
    if spot.allocation:
        out["allocation"] = _elsewhere(spot.match)
        return spot.match, out
    if spot.message or spot.match is None:
        raise PathError(" ".join((spot.message or "no root covers this path").split()))
    root = spot.match
    out["path"] = spot.shown
    out["exists"] = os.path.exists(spot.target)
    if not out["exists"]:
        # Where the existing tree stops. A typo in a user name shows up here
        # as `/scratch/meadow3` rather than as a directory the agent then
        # tries to create at the wrong level.
        out["nearest_existing"] = sanitize(cli._nearest_existing(spot.target), limit=4096)
    out["resolves_to"] = sanitize(spot.resolved, limit=4096) or None
    out["root"] = root.path
    record = place(root, run, detail=True)
    out["place"] = record
    owner = (root.policy or {}).get("quota_on")
    if owner and owner != root.path and record.get("used_bytes") is None:
        # The quota is the fileset's and the table shows it one row up. `why`
        # prints `?` here and says so in a note; an agent asked "is there
        # room" needs the figure, so it is followed.
        holder = next((r for r in run.roots if getattr(r, "path", "") == owner), None)
        if holder is not None:
            out["quota_from"] = place(holder, run)
            out["quota_note"] = (
                "bytes written here count against the quota reported on %s, so its "
                "used, quota and free figures are the ones that apply" % (owner,)
            )
    out["warnings"] = cli._surfaceable(run)
    return root, out


def recover_payload(run, path):
    # type: (Any, str) -> Dict[str, Any]
    """Read-only copies of one path, newest first, and the command to restore.

    Needs no root: the path is usually gone, and the mount table is enough to
    find the snapshot trees, exactly as `dirscape recover` does.
    """
    target = os.path.abspath(os.path.expanduser(path))
    copies, verdict = copies_for_path(
        target,
        getattr(run, "mounts", None),
        snapshot_roots=getattr(getattr(run, "site", None), "snapshot_roots", ()) or (),
    )
    out = _header(run, "recover")
    out["path"] = sanitize(target, limit=4096)
    out["exists_now"] = os.path.lexists(target)
    out["recoverable"] = verdict.to_json()
    out["copies"] = [
        dict(copy.to_json(), taken=_iso(getattr(copy, "taken_at", None))) for copy in copies
    ]
    if copies:
        how, command = cli._restore_line(target, copies[0].path)
        out["restore"] = command
        out["restore_note"] = how
    else:
        out["restore"] = None
        out["restore_note"] = (
            "there is no copy of this to restore"
            if verdict.refuted
            else 'no snapshot was found, which is not the same as "no backup": ask the site'
        )
    return out


def changes_payload(run):
    # type: (Any) -> Dict[str, Any]
    """What changed since the baseline, never moving the baseline.

    `stranded` is left out as `dirscape new` leaves it out: it is a standing
    condition reported on every run, and `paths` lists it.
    """
    out = _header(run, "changes")
    result = getattr(run, "changes", None)
    if result is None:
        out["tracking"] = False
        out["changes"] = []
        out["note"] = "state tracking is off (--no-state), so there is nothing to compare"
        return out
    out["tracking"] = True
    out["no_baseline"] = bool(getattr(result, "no_baseline", False))
    out["baseline_at"] = _iso(getattr(run, "baseline_at", None))
    records = []  # type: List[Dict[str, object]]
    for change in result:
        label, where, because = fields.change_fields(change)
        if label == "stranded":
            continue
        entry = {"label": label, "path": where or None}  # type: Dict[str, object]
        if because:
            entry["because"] = because
        renamed = getattr(change, "renamed_from", None)
        if renamed:
            entry["renamed_from"] = fields.safe(renamed, limit=4096)
        records.append(entry)
    out["changes"] = records
    if out["no_baseline"]:
        out["note"] = (
            "no baseline is recorded for this cluster yet, so nothing can honestly be "
            "called new; every `dirscape` run by a person records one, and "
            "`dirscape snapshot` records one on purpose"
        )
    out["warnings"] = cli._surfaceable(run)
    return out
