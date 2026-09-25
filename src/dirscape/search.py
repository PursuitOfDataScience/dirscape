"""Find names anywhere under one directory, as fast as the filesystem lists them.

**Names only, never a `stat`.** A search asks what things are CALLED, and the
answer is in the directory itself: `getdents` returns every name with its type,
so telling a folder from a file costs nothing and a tree is read at the speed
the filesystem lists it. A size needs a `stat` per entry, a round trip to a
metadata server on a parallel filesystem, which is what makes counting a large
folder slow; this never pays it, except for the few matches on screen.

**Read once, asked many times.** Every name read is kept, all the folder names
in one string and all the file names in another, joined on ``/``, the one
character no name can hold, with the offset where each folder's names begin.
A query is then ONE compiled pattern run over each string in C, so a changed
query, one more letter typed, is answered from memory: 1.36 million names in
a fraction of a second, where matching folder by folder in Python took up to
a second per keystroke. What is kept is bounded (see `Finder`).

**Shallow first.** Directories are read breadth first, so the matches nearest
the directory being searched are found, and shown, before the deep ones.

A query is a case-insensitive piece of a name (``era5`` finds ``ERA5-2020.nc``),
or a shell pattern for the whole name when it holds ``*``, ``?`` or ``[``
(``*.nc``). Folders are listed ahead of the path they would be opened with, so
``raw/`` finds only folders, and ``data/raw`` finds names holding ``raw`` in a
folder whose path holds ``data``.

Stdlib only, and Python 3.6: the deployability claim is the login node's own
``/usr/bin/python3``.
"""

import bisect
import os
import re
import threading
import time
from collections import deque
from typing import Any, Deque, Dict, Iterator, List, Optional, Sequence, Tuple  # noqa: F401

__all__ = ["KEEP_NAMES", "KEEP_FOLDERS", "MAX_MATCHES", "Query", "Finder"]

#: Names kept in memory for re-asking: about 50 bytes each with the lower-case
#: copy a query searches, so 200 MB at most. Past this nothing more is kept,
#: and a changed query reads the rest of the tree again.
KEEP_NAMES = 4000000

#: Folders kept, each with its path, which is the other thing that grows.
KEEP_FOLDERS = 1000000

#: Matches kept for display. More are counted, never stored: a query that
#: matches half a tree wants refining, not a list of millions to scroll.
MAX_MATCHES = 20000

#: What a joined listing is split on, and why it can be: no name holds it.
SEP = "/"


def _glob(pattern):
    # type: (str) -> str
    """A shell pattern as a regex over ONE name: nothing in it can cross a ``/``."""
    out = []  # type: List[str]
    i, n = 0, len(pattern)
    while i < n:
        char = pattern[i]
        i += 1
        if char == "*":
            out.append("[^/]*")
        elif char == "?":
            out.append("[^/]")
        elif char == "[":
            j = i
            if j < n and pattern[j] in "!^":
                j += 1
            if j < n and pattern[j] == "]":
                j += 1
            while j < n and pattern[j] != "]":
                j += 1
            if j >= n:
                out.append("\\[")
                continue
            inside = pattern[i:j].replace("\\", "\\\\")
            i = j + 1
            if inside[:1] in ("!", "^"):
                out.append("[^/" + inside[1:] + "]")
            else:
                out.append("[" + inside + "]")
        else:
            out.append(re.escape(char))
    return "".join(out)


class Query(object):
    """What the reader typed, compiled once: a pattern over names, and where it applies."""

    def __init__(self, text):
        # type: (str) -> None
        self.text = text
        raw = text.strip()
        #: A trailing slash asks for folders only.
        self.folders_only = raw.endswith(SEP) and bool(raw.strip(SEP))
        parts = [part for part in raw.split(SEP) if part]
        #: What the folder's path, relative to the search, must hold.
        self.within = SEP.join(parts[:-1]).lower()
        name = parts[-1] if parts else ""
        self.empty = not name
        #: The whole-name check for a shell pattern, else None: a piece of a
        #: name is all a plain query asks for.
        self.whole = None  # type: Any
        if self.empty:
            self.pattern = None  # type: Any
            self.needle = ""
        elif any(char in name for char in "*?["):
            # The whole name, so anchored at a separator or an end of the string.
            self.pattern = re.compile("(?:^|(?<=/))" + _glob(name) + "(?=/|$)", re.IGNORECASE)
            self.whole = re.compile(_glob(name) + r"\Z", re.IGNORECASE)
            self.needle = _longest_literal(name).lower()
        else:
            self.pattern = re.compile(re.escape(name), re.IGNORECASE)
            self.needle = name.lower()
        #: Whether the names past the display limit can be counted in bulk:
        #: a plain query with no folder path, where holding the needle is all
        #: a name has to do. See `count_from`.
        self.countable = self.whole is None and not self.within and bool(self.needle)

    def count_from(self, lower, position):
        # type: (str, int) -> int
        """How many names in ``lower`` from ``position`` on hold the needle.

        One split and one `in` a name, which counted 420,083 of 1.36 million
        names in 0.16s where stepping from match to match in Python took half
        a second and a regex over whole names longer still.
        """
        needle = self.needle
        return sum(1 for name in lower[position:].split(SEP) if needle in name)

    def names_in(self, joined):
        # type: (str) -> List[str]
        """Each name in a ``/``-joined listing that matches, once, in listing order."""
        return [name for _start, name in _names(self, joined, _lowered(joined))]

    def applies_to(self, folder):
        # type: (str) -> bool
        """Whether names in ``folder`` (relative, "" for the top) can match at all."""
        return not self.within or self.within in folder.lower()


def _longest_literal(pattern):
    # type: (str) -> str
    """The longest run of a shell pattern with no wildcard in it, which every match holds."""
    runs = re.split(r"\*|\?|\[[^\]]*\]?", pattern)
    return max(runs, key=len) if runs else ""


def _lowered(joined):
    # type: (str) -> Optional[str]
    """``joined`` in lower case, where that keeps every character in its place, else None.

    True of all ASCII, which is nearly every file name there is. Some letters
    lower into two (`İ` becomes `i̇`), and then a position in the copy is not a
    position in the name, so such a listing is matched the slow way instead.
    """
    lower = joined.lower()
    return lower if len(lower) == len(joined) else None


def _names(query, joined, lower=None):
    # type: (Query, str, Optional[str]) -> Iterator[Tuple[int, str]]
    """``(where it starts, name)`` for each name in ``joined`` that ``query`` matches.

    **The fast path is `str.find` over a lower-cased copy**, ``lower``: a
    case-insensitive regex scans in C too, but letter by letter with case
    folding, and took 0.2s over 1.36 million names where `find` takes a
    fraction of that. A shell pattern finds its longest literal piece the
    same way, and only the names holding it are checked in full. Each name is
    taken once: after a hit the search carries on from the end of that name.
    """
    if query.pattern is None or not joined:
        return
    if lower is not None and query.needle:
        needle = query.needle
        whole = query.whole
        size = len(joined)
        position = lower.find(needle)
        while position >= 0:
            start = lower.rfind(SEP, 0, position) + 1
            end = lower.find(SEP, position + len(needle))
            if end < 0:
                end = size
            name = joined[start:end]
            if whole is None or whole.match(name):
                yield start, name
            position = lower.find(needle, end)
        return
    last = -1
    for hit in query.pattern.finditer(joined):
        start = joined.rfind(SEP, 0, hit.start()) + 1
        if start == last:
            continue  # a second hit inside a name already taken
        end = joined.find(SEP, hit.end())
        yield start, joined[start : end if end >= 0 else len(joined)]
        last = start


class _Text(object):
    """Names of one kind from many folders: one string, and whose each stretch is.

    Built from chunks and joined only when asked, so adding a folder is an
    append and not a copy of everything before it; the joined string is kept
    until more arrive, and then only the new chunks are added to it.
    """

    def __init__(self):
        # type: () -> None
        self._chunks = []  # type: List[str]
        self._joined = ""
        self._lower = ""  # type: Optional[str]
        self._used = 0
        self._length = 0
        #: Where each folder's stretch begins, and which folder it is.
        self.starts = []  # type: List[int]
        self.owners = []  # type: List[int]

    def add(self, owner, names):
        # type: (int, List[str]) -> None
        if not names:
            return
        chunk = SEP.join(names)
        if self._length:
            self._length += 1
        self.starts.append(self._length)
        self.owners.append(owner)
        self._chunks.append(chunk)
        self._length += len(chunk)

    def snapshot(self):
        # type: () -> Tuple[str, Optional[str], List[int], List[int]]
        """The joined text so far, lower-cased too, with its starts and owners. Under the lock."""
        if self._used < len(self._chunks):
            fresh = SEP.join(self._chunks[self._used :])
            lowered = _lowered(fresh)
            if self._joined:
                self._joined += SEP + fresh
                if self._lower is not None and lowered is not None:
                    self._lower += SEP + lowered
                else:
                    self._lower = None
            else:
                self._joined, self._lower = fresh, lowered
            self._used = len(self._chunks)
        return self._joined, self._lower, list(self.starts), list(self.owners)


class Finder(object):
    """One directory's tree, read in the background and searched on demand.

    ``start`` begins reading; ``ask`` sets the query and returns at once, the
    matches among everything read so far arriving within milliseconds and
    the rest as it is read; ``snapshot`` is what a view paints. ``skip``
    holds absolute paths never entered: the mount points below the top, so a
    search of a project folder does not wander into another filesystem
    mounted inside it.

    **Memory is bounded, not the tree.** Names are kept, and each folder's
    path, until ``keep_names`` names or ``keep_folders`` folders; past that a
    folder is still read and matched but nothing of it is kept, so the
    search of a 40-million-name tree costs what a 4-million-name one does. A
    query changed after that answers the kept part from memory at once and
    reads the rest again, from the folders where keeping stopped.
    """

    def __init__(
        self,
        top,  # type: str
        threads=16,  # type: int
        skip=(),  # type: Sequence[str]
        keep_names=KEEP_NAMES,  # type: int
        max_matches=MAX_MATCHES,  # type: int
        keep_folders=KEEP_FOLDERS,  # type: int
    ):
        # type: (...) -> None
        self.top = top.rstrip(SEP) or SEP
        self.threads = max(1, int(threads))
        self.keep_names = keep_names
        self.keep_folders = keep_folders
        self.max_matches = max_matches
        self._skip = {path.rstrip(SEP) or SEP for path in skip if path}
        self._skip.discard(self.top)
        self._lock = threading.Condition()
        # (relative folder, its parent was kept, which walk it belongs to).
        self._todo = deque([("", True, 0)])  # type: Deque[Tuple[str, bool, int]]
        self._walk = 0
        self._active = 0
        self._alive = 0
        self._stopped = threading.Event()
        self._started = False
        #: Every folder kept, in the order read: an index into this is how the
        #: texts below say whose a name is.
        self._order = []  # type: List[str]
        self._texts = {True: _Text(), False: _Text()}
        self._kept = 0
        #: The first folders NOT kept, each the top of a stretch of the tree
        #: nothing of which is in memory: what a changed query reads again.
        self._frontier = []  # type: List[str]
        self._generation = 0
        self._query = Query("")
        self._matches = []  # type: List[Tuple[str, bool]]
        self._found = 0
        self._busy = 0
        self._version = 0
        self._sorted = (-1, [])  # type: Tuple[int, List[Tuple[str, bool]]]
        #: Names and folders the first reading found, folders that could not
        #: be read, when it began and when it finished.
        self.names = 0
        self.folders = 0
        self.unreadable = 0
        self.began = time.time()
        self.finished_at = None  # type: Optional[float]
        #: Names read again, since the last changed query, past what is kept.
        self.reread = 0

    # -- reading -----------------------------------------------------------

    def start(self):
        # type: () -> None
        with self._lock:
            if self._started:
                return
            self._started = True
        self._staff()

    def _staff(self):
        # type: () -> None
        """Readers up to `threads`: each one leaves once there is nothing to read."""
        with self._lock:
            wanted = self.threads - self._alive
            self._alive += max(0, wanted)
        for _ in range(max(0, wanted)):
            worker = threading.Thread(target=self._read)
            worker.daemon = True
            worker.start()

    def stop(self):
        # type: () -> None
        """The reader left: every reader stops at its next directory."""
        self._stopped.set()
        with self._lock:
            self._lock.notify_all()

    @property
    def done(self):
        # type: () -> bool
        """Whether the tree has been read, or reading stopped, and the query answered."""
        if self._stopped.is_set():
            return not self._busy
        return self.finished_at is not None and not self._busy and not self._rereading()

    @property
    def capped(self):
        # type: () -> bool
        """Whether part of the tree is past what is kept in memory."""
        return bool(self._frontier)

    def _rereading(self):
        # type: () -> bool
        return self._walk > 0 and (bool(self._todo) or bool(self._active))

    def _path(self, folder):
        # type: (str) -> str
        if not folder:
            return self.top
        return (self.top if self.top != SEP else "") + SEP + folder

    def _listing(self, folder):
        # type: (str) -> Optional[Tuple[List[str], List[str]]]
        folders = []  # type: List[str]
        files = []  # type: List[str]
        try:
            with os.scandir(self._path(folder)) as entries:
                for entry in entries:
                    try:
                        is_dir = entry.is_dir(follow_symlinks=False)
                    except OSError:
                        is_dir = False
                    (folders if is_dir else files).append(entry.name)
        except OSError:
            return None
        return folders, files

    def _read(self):
        # type: () -> None
        try:
            self._reading()
        finally:
            with self._lock:
                self._alive -= 1
                self._lock.notify_all()

    def _reading(self):
        # type: () -> None
        while True:
            with self._lock:
                while not self._todo and self._active and not self._stopped.is_set():
                    self._lock.wait(0.25)
                if self._stopped.is_set() or not self._todo:
                    if not self._active and self.finished_at is None:
                        self.finished_at = time.time()
                        # Joined now, by the last reader, so the first query
                        # typed after the reading is not the one that pays.
                        for text in self._texts.values():
                            text.snapshot()
                    self._lock.notify_all()
                    return
                folder, parent_kept, walk = self._todo.popleft()
                self._active += 1
                popped = self._walk
            try:
                got = self._listing(folder)
            except Exception:
                got = None
            query = None  # type: Optional[Query]
            generation = 0
            with self._lock:
                self._active -= 1
                # Read for a query that has since changed: a later walk from
                # the frontier reads this too, whether it is one of an older
                # walk's or the first reading's below what is kept. The first
                # reading's own folders are never outdated: nothing else reads
                # them, and they are kept, or found past it, here or not at all.
                if (walk != self._walk) if walk else (not parent_kept and popped != self._walk):
                    self._lock.notify_all()
                    continue
                if got is None:
                    if walk == 0:
                        self.unreadable += 1
                else:
                    folders, files = got
                    if walk == 0:
                        self.names += len(folders) + len(files)
                        self.folders += 1
                    else:
                        self.reread += len(folders) + len(files)
                    keep = (
                        walk == 0
                        and parent_kept
                        and self._kept + len(folders) + len(files) <= self.keep_names
                        and len(self._order) < self.keep_folders
                    )
                    if keep:
                        owner = len(self._order)
                        self._order.append(folder)
                        self._texts[True].add(owner, folders)
                        self._texts[False].add(owner, files)
                        self._kept += len(folders) + len(files)
                    elif walk == 0 and parent_kept:
                        self._frontier.append(folder)
                    prefix = folder + SEP if folder else ""
                    for name in folders:
                        child = prefix + name
                        if self._path(child) not in self._skip:
                            self._todo.append((child, keep, walk))
                    query, generation = self._query, self._generation
                self._lock.notify_all()
            if got is not None and query is not None and not query.empty:
                hits = self._match(query, folder, got[0], got[1])
                if hits:
                    with self._lock:
                        if generation == self._generation and walk in (0, self._walk):
                            self._take(hits)

    # -- asking ------------------------------------------------------------

    def ask(self, text):
        # type: (str) -> None
        """Search for ``text``. Returns at once; the matches arrive on `snapshot`."""
        query = Query(text)
        rewalk = False
        with self._lock:
            if text == self._query.text:
                return
            # One step, under one lock: the new query, the new walk, and what
            # was kept up to now. A folder read after this is matched by its
            # reader, which sees the new query, and is not in the snapshot.
            self._generation += 1
            generation = self._generation
            self._query = query
            self._matches = []
            self._found = 0
            self._version += 1
            if self._frontier:
                # The tree past the kept part is read again for this query,
                # from where keeping stopped. What the first reading had not
                # reached yet stays the first reading's, kept or found past what
                # is kept as it would have been: handed to this walk instead,
                # none of it was either, and the next query's walk dropped it.
                # A search of 300 folders typed into while they were read found
                # 44 of its 300 matches on a two-core node.
                pending = [item[0] for item in self._todo if item[1] and item[2] == 0]
                self._walk += 1
                self.reread = 0
                self._todo = deque((folder, False, self._walk) for folder in self._frontier)
                self._todo.extend((folder, True, 0) for folder in pending)
                self._lock.notify_all()
                rewalk = self._started and not self._stopped.is_set()
            if not query.empty:
                order = list(self._order)
                texts = [(kind, self._texts[kind].snapshot()) for kind in (True, False)]
                self._busy += 1
        if rewalk:
            self._staff()
        if query.empty:
            return
        worker = threading.Thread(target=self._rematch, args=(query, generation, order, texts))
        worker.daemon = True
        worker.start()

    def _rematch(self, query, generation, order, texts):
        # type: (Query, int, List[str], List[Tuple[bool, Tuple[str, Optional[str], List[int], List[int]]]]) -> None
        """Everything kept before the query changed, matched against it in one pass."""
        try:
            for is_dir, (text, lower, starts, owners) in texts:
                if is_dir is False and query.folders_only:
                    continue
                hits = []  # type: List[Tuple[str, bool]]
                extra = 0
                for seen, (start, name) in enumerate(_names(query, text, lower)):
                    if not (seen & 4095) and generation != self._generation:
                        return  # typed on: a newer query is being answered
                    folder = order[owners[bisect.bisect_right(starts, start) - 1]]
                    if not query.applies_to(folder):
                        continue
                    if len(hits) < self.max_matches:
                        hits.append(((folder + SEP + name) if folder else name, is_dir))
                    elif query.countable and lower is not None:
                        # Everything kept for display: the rest is only counted.
                        extra = 1 + query.count_from(lower, start + len(name) + 1)
                        break
                    else:
                        extra += 1
                if generation != self._generation or self._stopped.is_set():
                    return
                with self._lock:
                    if generation != self._generation:
                        return
                    self._take(hits)
                    self._found += extra
        finally:
            with self._lock:
                self._busy -= 1
                self._lock.notify_all()

    def _match(self, query, folder, folders, files):
        # type: (Query, str, Any, Any) -> List[Tuple[str, bool]]
        """Matches in one folder's listing, as (relative path, is a folder)."""
        if not query.applies_to(folder):
            return []
        if not isinstance(folders, str):
            folders = SEP.join(folders)
        if not isinstance(files, str):
            files = SEP.join(files)
        prefix = folder + SEP if folder else ""
        hits = [(prefix + name, True) for name in query.names_in(folders)]
        if not query.folders_only:
            hits.extend((prefix + name, False) for name in query.names_in(files))
        return hits

    def _take(self, hits):
        # type: (List[Tuple[str, bool]]) -> None
        """Under the lock: count every hit, keep the first `max_matches`."""
        self._found += len(hits)
        room = self.max_matches - len(self._matches)
        if room > 0:
            self._matches.extend(hits[:room])
        self._version += 1

    def snapshot(self):
        # type: () -> Tuple[str, List[Tuple[str, bool]], int]
        """``(query, matches kept, matches found)``, the matches shallowest first."""
        with self._lock:
            query = self._query.text
            version = self._version
            found = self._found
            if self._sorted[0] == version:
                return query, self._sorted[1], found
            matches = list(self._matches)
        matches.sort(key=lambda hit: (hit[0].count(SEP), hit[0].lower()))
        with self._lock:
            if self._version == version:
                self._sorted = (version, matches)
        return query, matches, found

    def wait(self, timeout=None):
        # type: (Optional[float]) -> bool
        """Block until everything is read and answered, or ``timeout``; True if done."""
        stop = None if timeout is None else time.time() + timeout
        with self._lock:
            while not self.done:
                left = None if stop is None else stop - time.time()
                if left is not None and left <= 0:
                    return False
                self._lock.wait(0.05 if left is None else min(0.05, left))
        return True
