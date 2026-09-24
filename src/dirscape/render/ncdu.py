"""An ncdu-compatible export, so this tool gets a browser it did not write.

    [1, 2, {"progname": ..., "progver": ..., "timestamp": ...}, [ {dir}, {entry}, ... ]]

Why bother: `ncdu -f`, `gdu` and `ncdu-compare` already exist and are already
installed on clusters. Writing this array is how `dirscape` gains an
interactive browser and a historical tree diff without shipping either one.

**This is a SHALLOW export and it is not a substitute for `ncdu -o`.** This
tool does not walk trees for their sizes: its one walk is capped, and only of a
root no quota covers. What lands here is the root plus whatever one level of
entries the caller already knows, and the usage figure is the quota backend's,
or that capped walk's. For bytes-by-directory, run `rdu` or `ncdu` itself. The
consequence is visible in the output rather than hidden in this docstring: the
root's info block carries ``read_error`` unless the caller states the listing
is complete, which is the format's own way of saying "some items may be missing
from this listing".

Format notes, checked against the ncdu JSON format specification
(`dev.yorhel.nl/ncdu/jsonfmt`) and round-tripped through the installed ncdu
2.1.2:

* major version must be 1; minor 2 is ncdu 1.16 and later, which added `nlink`
* the first element of a directory array must be that directory's info block;
  subdirectories are nested arrays, plain entries are objects
* the root's `name` is a full absolute path, and children carry relative names
* `asize` and `dsize` default to 0 **when absent**, which is the hook this
  module hangs its honesty on: an unknown size is OMITTED and flagged rather
  than written as a zero. ncdu will still display an absent size as 0 B beside
  its error marker, and that is the format's limit, not a choice made here
* `excluded` is a closed vocabulary (`pattern`, `otherfs`, `kernfs`,
  `frmlink`), so a dirscape-specific reason is never smuggled into it. The
  reason codes live in `--json`, which is the format that has room for them
* an unrecognised key is IGNORED on import rather than rejected (measured: a
  file carrying `dirscape_reason` imported cleanly and came back without it).
  So a custom key is safe and also pointless, since it does not survive to any
  consumer, and this module writes none
"""

import json
from typing import Dict, List, Optional, Sequence

from ..model import Root
from . import fields

__all__ = ["render", "payload", "FORMAT_MAJOR", "FORMAT_MINOR", "EXCLUDED_REASONS"]


FORMAT_MAJOR = 1

#: 0 is ncdu 1.9-1.12, 1 added the extended mode in 1.13, 2 added `nlink` in
#: 1.16. We write 2 and use none of its extras, because a consumer old enough
#: to reject it predates every ncdu anyone still runs.
FORMAT_MINOR = 2

#: The whole closed set the format allows. Nothing else may go in that field.
EXCLUDED_REASONS = ("pattern", "otherfs", "kernfs", "frmlink")


def _size_fields(root):
    # type: (Root) -> Dict[str, object]
    """``asize``/``dsize`` from the quota row, or NOTHING at all.

    Both keys are omitted together when there is no figure. The format says an
    absent size defaults to zero, so omitting is the closest this format comes
    to saying "unknown", and it is paired with `read_error` so a consumer sees
    a flagged entry rather than a confident zero.
    """
    row, _, _ = fields.pick_row(root.quota, root.path, "blocks")
    if row is None or row.used is None:
        return {}
    # One figure for both: a quota backend reports blocks charged against the
    # fileset, which is the disk consumption, and it has no apparent-size
    # counterpart. Writing the same number twice is honest here; inventing a
    # different apparent size would not be.
    return {"asize": int(row.used), "dsize": int(row.used)}


def _entry(item):
    # type: (object) -> object
    """One caller-supplied child, as an info block or a one-element directory.

    Accepts a dict or a bare name. Recognised keys: ``name`` (required),
    ``used`` / ``asize`` / ``dsize``, ``is_dir``, ``read_error``, ``notreg``,
    ``nlink``, ``dev``, ``ino``, ``excluded``.

    A child with no size is NOT written as a zero: the keys are left out and
    `read_error` goes on, because "we did not measure this" and "this is
    empty" must not look the same. That is the same rule the root follows.
    """
    if not isinstance(item, dict):
        item = {"name": item}
    name = fields.safe(item.get("name"), limit=4096)
    block = {"name": name}  # type: Dict[str, object]
    used = item.get("used")
    if used is None:
        used = item.get("dsize", item.get("asize"))
    if used is None:
        block["read_error"] = True
    else:
        block["asize"] = int(item.get("asize", used))
        block["dsize"] = int(item.get("dsize", used))
    for key in ("dev", "ino"):
        if item.get(key) is not None:
            block[key] = int(item[key])
    # `nlink` only travels WITH an inode number, and that is not fussiness.
    # Measured against ncdu 2.1.2: an entry carrying `"nlink": 2` and no `ino`
    # was imported as a hard link with inode 0 (`"ino":0,"hlnkc":true` came
    # back on re-export), which invites the browser to count two unrelated
    # files as one. A link count with nothing to identify the link by is worse
    # than no link count.
    if item.get("nlink") is not None and item.get("ino") is not None:
        block["nlink"] = int(item["nlink"])
    if item.get("notreg"):
        block["notreg"] = True
    if item.get("read_error"):
        block["read_error"] = True
    reason = item.get("excluded")
    if reason in EXCLUDED_REASONS:
        block["excluded"] = reason
    if item.get("is_dir"):
        # A directory is an ARRAY whose first element is its info block. A
        # directory written as a bare object would be imported as a file.
        return [block]
    return block


def payload(root, entries=(), progname="dirscape", progver=None, timestamp=None, listed=False):
    # type: (Root, Sequence[object], str, Optional[str], Optional[float], bool) -> List[object]
    """The array, ready for `json.dumps`.

    ``listed`` is the caller stating that ``entries`` is the complete one-level
    listing of this root. It defaults to False and the default is the truth for
    this tool: nothing here walks, and a refused `scandir` must not come out
    looking like an empty directory.
    """
    meta = {"progname": progname}  # type: Dict[str, object]
    if progver:
        meta["progver"] = fields.safe(progver, limit=64)
    if timestamp is not None:
        meta["timestamp"] = int(timestamp)

    info = {"name": root.path}  # type: Dict[str, object]
    info.update(_size_fields(root))
    if root.identity is not None:
        dev, ino = root.identity
        info["dev"] = int(dev)
        info["ino"] = int(ino)
    if "dsize" not in info or not listed:
        # Either the size is unknown or the listing is partial. Both mean the
        # same thing to a consumer, which is that what it sees here is
        # incomplete, and the format has exactly one flag for that.
        info["read_error"] = True

    children = [_entry(item) for item in entries]
    return [FORMAT_MAJOR, FORMAT_MINOR, meta, [info] + children]


def render(
    root,  # type: Root
    entries=(),  # type: Sequence[object]
    progname="dirscape",  # type: str
    progver=None,  # type: Optional[str]
    timestamp=None,  # type: Optional[float]
    listed=False,  # type: bool
    indent=None,  # type: Optional[int]
):
    # type: (...) -> str
    """The export as JSON text. Compact by default, as ncdu's own export is."""
    return json.dumps(
        payload(
            root,
            entries=entries,
            progname=progname,
            progver=progver,
            timestamp=timestamp,
            listed=listed,
        ),
        indent=indent,
    )
