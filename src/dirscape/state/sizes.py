"""Folder sizes the browser has counted, kept from one run to the next.

**The only way a very large folder can open at once.** A size is one `stat`
per entry, and on a parallel filesystem every one is a round trip to a
metadata server. Measured here: opening `/project/rcc`, 84 folders holding 37.7
million files, took 22.8 minutes to count with rapidu's sixteen threads, 90%
of the rows landing in the first five minutes and the last folder, 17 million
files on its own, taking the rest. No walker makes that first time fast:
rapidu measures itself within 6% of the `scandir` + `stat` floor, and more
threads than it uses is more than is polite to ask of a shared metadata
server. What can be fast is every time after it.

So every count the browser finishes is kept here, with when it finished, and
a folder opened again shows its figures at once. They come from FINISHED
counts only, never a partial one, so the browser's rule that a row shows `…`
until its count is done holds for these too. The view says how old they are
and counts them again behind the view once they are older than its
freshness window: a stored figure is where the view starts, not the last word.

**A record** is one JSON object per line, appended::

    {"b": 1190673744281, "d": "gpfs:proj", "f": 3355714, "h": "", "i": 12,
     "p": "/project/x", "t": 1790000000.0}

``p`` the path, ``b`` bytes, ``f`` files, ``t`` when the count finished, ``i``
the directory's inode number, ``d`` the filesystem it is on, and ``h`` the
host for a filesystem only one node mounts. The last line for a path wins.
Appending is what keeps a record cheap: a finished count costs one short write
instead of a rewrite of the file, and a run killed mid-write loses at most its
own last line, which the reader skips.

**A path is not a folder.** A directory removed and made again under the same
name, or `/tmp/x` on another node, is a different folder at the same path, and
its old figure would be a confident wrong answer. So a record carries the
inode number, which the listing reads for free from `getdents`, and a lookup
that knows the inode must match it. A record names the FILESYSTEM it was
counted on, the way every node that mounts it names it, and is used wherever
that filesystem is: one file serves every cluster that shares the home it is
in, so `/lus/eagle` counted on Polaris opens at once on Sophia. A node-local
filesystem has no such name, so its records carry the host instead and are
read on that host only.

**Bounds.** At most `MAX_RECORDS` folders, the most recently counted kept. The
file is rewritten, atomically as the lineage is, once appends have made it
more than twice what it holds or larger than `MAX_BYTES`, and a record older
than `KEEP_S` is dropped on load.

**One writer per index.** `put` and `flush` are called from the thread that
counts, `get` from the one that paints. No lock is needed for that and none
is taken, because this package imports `json`, `os` and `time` and nothing
else (`test_state_py36.py`): a dict item assignment is atomic, and `flush`
takes each pending line with `list.pop`, which hands a line to exactly one
caller even if two ever flush at once.

Never raises. A state file that cannot be read or written costs the reader the
stored figures and nothing else.
"""

import json
import os
import time
from typing import Dict, List, Optional, Tuple

from .snapshot import TOOL, current_hostname, state_dir

__all__ = [
    "SIZES_SCHEMA",
    "MAX_RECORDS",
    "MAX_BYTES",
    "KEEP_S",
    "SizeIndex",
    "sizes_path",
]

#: Bumped when a record changes shape in a way an older dirscape could misread.
#: A file of another schema is left alone: neither read nor written.
SIZES_SCHEMA = 1

#: Folders kept. Opening `/project/rcc` recorded 811, its 82 folders and the
#: folders directly inside each, and a walk now keeps up to 50 more from below
#: those (`cli.DEEP_KEPT`), so a tree that size is up to 5,000. `MAX_BYTES`
#: binds first, at about 17,000, which is three such trees before the oldest go.
MAX_RECORDS = 20000

#: When the file is rewritten without its superseded lines. A record is about
#: 120 bytes, so `MAX_RECORDS` of them is a little over 2 MiB.
MAX_BYTES = 4 * 1024 * 1024

#: A record older than this is dropped when the file is read: a figure a month
#: old describes a folder that has had a month to change.
KEEP_S = 30 * 86400.0

_COMPACT = (",", ":")

_HEADER = {"kind": "sizes", "schema": SIZES_SCHEMA, "tool": TOOL}

#: A record's fields, as stored in memory: bytes, files, counted at, inode,
#: host, filesystem.
Record = Tuple[int, int, float, Optional[int], str, str]


def sizes_path(environ=None):
    # type: (Optional[Dict[str, str]]) -> str
    """One file for every cluster sharing this home: records name their filesystem."""
    return os.path.join(state_dir(environ), "sizes.jsonl")


def _whole(value):
    # type: (object) -> Optional[int]
    """A non-negative integer from JSON, or None. A bool is not a count."""
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        return None
    return value


def _record(payload, now):
    # type: (object, float) -> Optional[Tuple[str, Record]]
    """One line's record, or None for anything that is not a sound one."""
    if not isinstance(payload, dict):
        return None
    path = payload.get("p")
    used = _whole(payload.get("b"))
    files = _whole(payload.get("f"))
    counted = payload.get("t")
    if not isinstance(path, str) or not path.startswith("/") or used is None or files is None:
        return None
    if isinstance(counted, bool) or not isinstance(counted, (int, float)):
        return None
    # A record from the future is a clock that was wrong, and its age would
    # read as negative: not a figure to show as fresh.
    if counted < now - KEEP_S or counted > now + 86400.0:
        return None
    ino = payload.get("i")
    host = payload.get("h")
    filesystem = payload.get("d")
    return path, (
        used,
        files,
        float(counted),
        _whole(ino),
        host if isinstance(host, str) else "",
        filesystem if isinstance(filesystem, str) else "",
    )


def _line(path, record):
    # type: (str, Record) -> str
    used, files, counted, ino, host, filesystem = record
    return json.dumps(
        {
            "b": used,
            "d": filesystem,
            "f": files,
            "h": host,
            "i": ino,
            "p": path,
            "t": round(counted, 3),
        },
        separators=_COMPACT,
        sort_keys=True,
    )


class SizeIndex(object):
    """The finished counts one user's browser has made on one cluster."""

    def __init__(self, path="", host=""):
        # type: (str, str) -> None
        #: Where the records live, or "" to keep them in memory only.
        self.path = path
        self.host = host
        #: Why a file on disk was not used, for anyone who asks.
        self.notes = []  # type: List[str]
        self._records = {}  # type: Dict[str, Record]
        self._pending = []  # type: List[str]

    @classmethod
    def load(cls, path=None, environ=None, host=None, now=None):
        # type: (Optional[str], Optional[Dict[str, str]], Optional[str], Optional[float]) -> SizeIndex
        """The index at ``path``, or at this home's own file. Never raises."""
        where = path or sizes_path(environ)
        index = cls(where, current_hostname(environ) if host is None else host)
        try:
            index._read(time.time() if now is None else now)
        except Exception as exc:
            # Unreadable in some way nobody planned for: start empty, and do
            # not write over a file that may hold something worth keeping.
            index.notes.append("could not read %s: %s" % (where, exc))
            index._records = {}
            index.path = ""
        return index

    def __len__(self):
        # type: () -> int
        return len(self._records)

    def get(self, path, ino=None, filesystem=""):
        # type: (str, Optional[int], str) -> Optional[Tuple[int, int, float]]
        """``(bytes, files, counted_at)`` for the folder at ``path``, if one is kept.

        ``ino`` is the folder's inode number as the listing read it, and
        ``filesystem`` the filesystem it is on, "" for one only this node
        mounts: a record from another is a different folder with that path.
        """
        record = self._records.get(path)
        if record is None:
            return None
        used, files, counted, kept_ino, host, kept_fs = record
        if kept_fs != filesystem:
            return None
        if not kept_fs and host != self.host:
            return None
        if ino is not None and kept_ino is not None and ino != kept_ino:
            return None
        return used, files, counted

    def put(self, path, used, files, counted_at, ino=None, filesystem=""):
        # type: (str, int, int, float, Optional[int], str) -> None
        """Keep one finished count; with no ``filesystem``, for this host only."""
        record = (
            max(0, int(used)),
            max(0, int(files)),
            float(counted_at),
            ino if isinstance(ino, int) and not isinstance(ino, bool) else None,
            "" if filesystem else self.host,
            filesystem,
        )  # type: Record
        self._records[path] = record
        self._pending.append(_line(path, record))

    def flush(self):
        # type: () -> bool
        """Write every record kept since the last flush. Never raises."""
        lines = []  # type: List[str]
        while True:
            try:
                lines.append(self._pending.pop(0))
            except IndexError:
                break
        if not lines or not self.path:
            return bool(self.path) or not lines
        try:
            self._append(lines)
        except OSError:
            return False
        return True

    # -- the file ----------------------------------------------------------

    def _read(self, now):
        # type: (float) -> None
        try:
            with open(self.path, "rb") as handle:
                raw = handle.read()
        except (IOError, OSError):
            return  # none yet, which is how every index starts
        lines = raw.split(b"\n")
        try:
            header = json.loads(lines[0].decode("utf-8"))
        except ValueError:
            header = None
        ours = isinstance(header, dict) and header.get("tool") == TOOL
        if not ours or header.get("kind") != "sizes":
            self.notes.append("%s is not a dirscape size index, so it was left alone" % self.path)
            self.path = ""
            return
        if header.get("schema") != SIZES_SCHEMA:
            self.notes.append(
                "%s was written by another dirscape (schema %r), so it was left alone"
                % (self.path, header.get("schema"))
            )
            self.path = ""
            return
        kept = 0
        for line in lines[1:]:
            if not line.strip():
                continue
            kept += 1
            try:
                found = _record(json.loads(line.decode("utf-8")), now)
            except ValueError:
                continue  # a line cut short by a run that was killed
            if found is not None:
                self._records[found[0]] = found[1]
        bloated = kept > 2 * len(self._records) + 100 or len(raw) > MAX_BYTES
        if bloated or len(self._records) > MAX_RECORDS:
            self._compact()

    def _compact(self):
        # type: () -> None
        """Rewrite the file with one line per folder, the newest `MAX_RECORDS`."""
        newest = sorted(self._records.items(), key=lambda item: item[1][2], reverse=True)
        newest = newest[:MAX_RECORDS]
        lines = [_line(path, record) for path, record in newest]
        while lines and sum(len(line) + 1 for line in lines) > MAX_BYTES // 2:
            lines.pop()
            newest.pop()
        self._records = dict(newest)
        blob = "\n".join([json.dumps(_HEADER, separators=_COMPACT, sort_keys=True)] + lines) + "\n"
        temporary = "%s.%d.tmp" % (self.path, os.getpid())
        try:
            with open(temporary, "w") as handle:
                handle.write(blob)
            os.chmod(temporary, 0o600)
            os.replace(temporary, self.path)
        except OSError:
            # See `Lineage.save` for why this is not `contextlib.suppress`.
            try:  # noqa: SIM105
                os.unlink(temporary)
            except OSError:
                pass

    def _append(self, lines):
        # type: (List[str]) -> None
        directory = os.path.dirname(self.path) or "."
        if not os.path.isdir(directory):
            os.makedirs(directory, 0o700)
        handle = os.open(self.path, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
        try:
            blob = "\n".join(lines) + "\n"
            if os.fstat(handle).st_size == 0:
                blob = json.dumps(_HEADER, separators=_COMPACT, sort_keys=True) + "\n" + blob
            data = blob.encode("utf-8")
            while data:
                data = data[os.write(handle, data) :]
        finally:
            os.close(handle)
