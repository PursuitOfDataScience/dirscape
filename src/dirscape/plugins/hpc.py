"""ExampleU HPC: the three things about this site that cannot be config.

Everything declarative about HPC belongs in `/etc/dirscape/site.conf`. Three
things are not declarative and so they live here:

1. **The membership rule is code, not a convention.** Group name, directory
   name and fileset name are three different strings here. `/project/hpc` is
   group-owned by `hpc-staff` and lives in fileset `project-hpc`. The
   authoritative rule is in the site's own `sitequota.py`, which is what the
   `quota` command actually runs, and it is reproduced in `owns_fileset` below.

2. **An allocation CLI with its own table format.** `allocs storage` reports
   allocations on `cfs1`, `cfs2`, `cfs4` and `project3`, and none of those
   paths exist on the node that printed them. That mismatch is the single most
   useful thing this tool reports, and it is only visible if something parses
   that table.

3. **A world-readable historical quota archive.** `/project/hpc/usagedata/gpfsdumps`
   holds daily cluster-wide `mmrepquota` dumps back to 2021-02-08. Bisecting
   them dates a fileset's creation to the day, which is otherwise impossible
   here: birth time is unavailable on GPFS (`stat -c %W` returns 0) and
   directory mtime is a decoy (`/project/aarnold`'s fileset first appears in
   this archive in March 2026 while its directory mtime reads 2026-05-15, two
   months late).

Point 3 is a happy accident of one site. The core's newness mechanism is its
own snapshot lineage and must never depend on anything here.
"""

import os
import re
import time
from typing import Dict, List, Optional, Sequence, Tuple

from . import Allocation, SitePlugin

__all__ = ["HPCPlugin", "parse_accounts_storage", "parse_mmrepquota_parsable"]


# The historical archive. Read-only, world-readable through a `--x` traversal
# on /project/hpc, and entirely optional: every method that touches it degrades
# to None when it is absent, which is what happens on every other cluster.
_ARCHIVE_DIR = "/project/hpc/usagedata/gpfsdumps"
_ARCHIVE_LATEST = "last-gpfs-parsable.quota2"
_ARCHIVE_DAILY = re.compile(r"^(\d{8})-gpfs\.quota$")

# Site CLIs. Absolute paths as well as bare names, because a site wrapper is
# sometimes a SHELL ALIAS and a subprocess cannot see one: on meadow2 the
# working `quota` is an alias to a script while the binary on PATH exits 127.
_SITE_BIN = "/opt/site/bin"
_WRAPPER_PATHS = (
    "/opt/site/bin/quota",
    "/project2/hpc/admin/bin/quota.py",
)

# Fileset prefixes that encode a group name, longest first so `project2-` is
# tried before `project-` and does not get half-stripped.
_FILESET_PREFIXES = ("project2-", "project3-", "collie3-", "project-")


def parse_accounts_storage(text):
    # type: (str) -> List[Allocation]
    """Parse the `allocs storage` ASCII table.

    Shape, as measured:

        +-----------+------+---------+--------+-------------------+
        |  Account  |  ID  |  Type   | GB(s)  |     Location      |
        +-----------+------+---------+--------+-------------------+
        | hpc-staff | 301  | Special | 256000 |  cfs1/hpc-staff   |

    Parsed by locating the columns from the HEADER row rather than by fixed
    offsets, so a widened Account column does not shift every field. Rows whose
    cell count does not match the header are skipped with no attempt to guess,
    because a half-parsed allocation is worse than a missing one.
    """
    allocations = []  # type: List[Allocation]
    header = None  # type: Optional[List[str]]

    for line in text.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("+"):
            continue
        if not stripped.startswith("|"):
            continue
        cells = [cell.strip() for cell in stripped.strip("|").split("|")]
        if header is None:
            header = [cell.lower() for cell in cells]
            continue
        if len(cells) != len(header):
            continue

        row = dict(zip(header, cells))
        location = row.get("location", "")
        if not location:
            continue

        size = None  # type: Optional[float]
        for key in ("gb(s)", "gb", "size"):
            if row.get(key):
                try:
                    size = float(row[key])
                except ValueError:
                    size = None
                break

        allocations.append(
            Allocation(
                account=row.get("account", ""),
                location=location,
                # Deliberately NOT derived into a path. "cfs4/hpc-staff" looks
                # like it should be "/cfs4/hpc-staff" and often is, but the
                # allocation database is not a mount table and inventing a path
                # here would be exactly the guess that rapiDU's RD-3 made when
                # it attributed a /scratch walk to the wrong cluster.
                path=None,
                size_gb=size,
                kind=row.get("type", ""),
                start=row.get("start", ""),
                end=row.get("end", ""),
                source="allocs storage",
            )
        )

    return allocations


def parse_mmrepquota_parsable(text):
    # type: (str) -> List[Dict[str, str]]
    """Parse the colon-delimited `mmrepquota` dump, keyed off its own HEADER.

    The file declares its own field names:

        mmrepquota::HEADER:version:reserved:reserved:filesystemName:quotaType:
        id:name:blockUsage:...:filesetname:

    So the parser reads that line and maps by name. Hardcoding indices would
    work today and break silently the day IBM inserts a field, turning every
    `filesetname` into a `fid`. Twenty-six fields is too many to index by hand
    and be sure.
    """
    rows = []  # type: List[Dict[str, str]]
    fields = None  # type: Optional[List[str]]
    device = ""

    for line in text.splitlines():
        if line.startswith("*** Report"):
            # `*** Report for USR GRP FILESET quotas on collie3_cap`
            device = line.rsplit(" on ", 1)[-1].strip() if " on " in line else ""
            continue
        if not line.startswith("mmrepquota:"):
            continue
        parts = line.split(":")
        if len(parts) > 2 and parts[2] == "HEADER":
            fields = parts
            continue
        if fields is None:
            continue
        row = {}  # type: Dict[str, str]
        for index, name in enumerate(fields):
            if not name or name in ("mmrepquota", "HEADER", "reserved"):
                continue
            if index < len(parts):
                row[name] = parts[index]
        if row:
            # `setdefault` is wrong here and was a bug: the key is PRESENT with
            # an empty value on rows where mmrepquota omits the filesystem, so
            # setdefault leaves the blank in place and the row loses its
            # device. Test the value, not the key.
            if not row.get("filesystemName"):
                row["filesystemName"] = device
            rows.append(row)

    return rows


def _fileset_names_in_daily(text):
    # type: (str) -> set
    """Fileset names in one dated human-format dump.

    The dated files are fixed-column, not colon-delimited, with the layout

        Name       fileset    type   KB  quota  limit  in_doubt  grace | ...

    so a whitespace split puts the type in position 2. Only FILESET rows are
    taken: USR and GRP rows repeat the same fileset many times over.
    """
    names = set()
    device = ""
    for line in text.splitlines():
        if line.startswith("*** Report"):
            device = line.rsplit(" on ", 1)[-1].strip() if " on " in line else ""
            continue
        parts = line.split()
        if len(parts) > 3 and parts[2] == "FILESET":
            names.add("%s:%s" % (device, parts[0]))
    return names


class HPCPlugin(SitePlugin):
    """ExampleU HPC (meadow2, meadow3, collie3)."""

    name = "hpc"

    def __init__(self):
        # type: () -> None
        self._archive_days = None  # type: Optional[List[Tuple[str, str]]]
        self._first_seen_cache = {}  # type: Dict[str, Optional[float]]

    # -- detection -------------------------------------------------------

    def detect(self, runner, mounts):
        # type: (object, Sequence[object]) -> bool
        """Cheap, and never a hostname check.

        Two independent signals, either of which is enough: the site's own CLI
        directory, or a storage device named after one of the clusters. A
        hostname check would be the wrong instrument, for the same reason
        nodetop filters GPU nodes on GRES rather than on a hostname prefix:
        `collie3-bigmem1` is a CPU node despite its name.
        """
        if os.path.isdir(_SITE_BIN) and os.path.isfile(os.path.join(_SITE_BIN, "accounts")):
            return True
        for mount in mounts or ():
            device = str(getattr(mount, "device", "") or "")
            if device.startswith(("meadow", "collie3")):
                return True
        return False

    def describe(self):
        # type: () -> str
        return "ExampleU HPC (meadow2 / meadow3 / collie3)"

    def site_defaults(self):
        # type: () -> Dict[str, object]
        """Config this site would otherwise need a `site.conf` to state."""
        return {
            "fileset_prefixes": list(_FILESET_PREFIXES),
            "group_prefixes": ["pi-"],
            "wrapper_paths": [p for p in _WRAPPER_PATHS if os.path.isfile(p)],
            "extra_bin_dirs": ["/usr/lpp/mmfs/bin", _SITE_BIN],
            "dataset_roots": [p for p in ("/project2/reference",) if os.path.isdir(p)],
        }

    # -- allocations -----------------------------------------------------

    def allocations(self, runner, budget):
        # type: (object, object) -> List[Allocation]
        """Read `allocs storage`.

        Returns an empty list on any failure rather than raising. An allocation
        listing is an enrichment: without it the tool still reports every
        mounted root correctly, it just cannot say "allocated, not mounted
        here" about the ones that are missing.
        """
        exe = runner.available("accounts", extra_dirs=(_SITE_BIN,))  # type: ignore[attr-defined]
        if not exe:
            return []
        result = runner.run([exe, "storage"])  # type: ignore[attr-defined]
        if result.failed:
            return []
        return parse_accounts_storage(result.stdout)

    # -- membership ------------------------------------------------------

    def owns_fileset(self, fileset, groups):
        # type: (str, Sequence[str]) -> Optional[bool]
        """The site's own rule, reproduced from its `sitequota.py` (lines 377-408).

        The original, which is what the `quota` command runs:

            name = proj.replace('project-', '').replace('collie3-', '')
            if proj == 'project-hpc' and 'hpc-staff' not in groups:
                continue
            if name not in groups and 'pi-' + name not in groups:
                continue

        Reproduced rather than reimplemented, including the `project-hpc`
        special case, because this rule is the site's definition of the answer
        and a cleaner rule would be a different, wrong answer. Verified against
        the cluster-wide fileset dump: it returns exactly the three filesets
        the site `quota` command prints for this account.

        Returns None, not False, for a fileset the rule does not recognise. A
        rule that has never heard of a fileset has not established that the
        user lacks it, and the caller must keep looking rather than print "no".
        """
        if not fileset:
            return None
        group_set = set(groups or ())

        # The special case comes first in the original and the order matters:
        # `project-hpc` strips to `hpc`, which IS a real group here, so testing
        # the general rule first would grant it to every member of `hpc`.
        if fileset == "project-hpc":
            return "hpc-staff" in group_set

        name = fileset
        for prefix in _FILESET_PREFIXES:
            if name.startswith(prefix):
                name = name[len(prefix) :]
                break
        else:
            # No recognised prefix means this is not a project fileset at all
            # (`home`, `software`, `scratch`), and this rule has nothing to say
            # about it. Those are reached through the mount table instead.
            return None

        if name in group_set or ("pi-" + name) in group_set:
            return True
        return False

    # -- history ---------------------------------------------------------

    def _daily_files(self):
        # type: () -> List[Tuple[str, str]]
        """Dated archive files as (YYYYMMDD, path), oldest first."""
        if self._archive_days is not None:
            return self._archive_days

        found = []  # type: List[Tuple[str, str]]
        try:
            with os.scandir(_ARCHIVE_DIR) as entries:
                for entry in entries:
                    match = _ARCHIVE_DAILY.match(entry.name)
                    if match:
                        found.append((match.group(1), entry.path))
        except OSError:
            # Absent on every other cluster, and unreadable to a user without
            # traversal on /project/hpc. Both are normal.
            found = []
        found.sort()
        self._archive_days = found
        return found

    def first_seen(self, fileset, runner, budget=None):
        # type: (str, object, object) -> Optional[float]
        """Date a fileset's creation by bisecting the daily archive.

        Returns a unix timestamp for the first archived day the fileset appears
        in, or None when the archive is absent or the fileset is present in the
        oldest file (in which case it predates the archive and the honest
        answer is "at least this old", not a date).

        Bisection rather than a linear scan because the archive holds about
        2,000 files: 11 reads instead of 2,000. Each read is a few hundred KB.
        """
        if fileset in self._first_seen_cache:
            return self._first_seen_cache[fileset]

        days = self._daily_files()
        answer = None  # type: Optional[float]
        if len(days) >= 2:
            def present(index):
                # type: (int) -> bool
                try:
                    with open(days[index][1], "r") as handle:
                        text = handle.read()
                except OSError:
                    # An unreadable midpoint breaks the bisection's invariant,
                    # so raise out and return None rather than guessing a
                    # direction and landing on a confidently wrong date.
                    raise
                names = _fileset_names_in_daily(text)
                return any(name.endswith(":" + fileset) for name in names)

            try:
                if present(0):
                    # Present in the oldest dump: it predates the archive and
                    # no creation date can be established from it.
                    answer = None
                elif not present(len(days) - 1):
                    # Not in the newest dump either: nothing to date.
                    answer = None
                else:
                    low, high = 0, len(days) - 1
                    while high - low > 1:
                        mid = (low + high) // 2
                        if present(mid):
                            high = mid
                        else:
                            low = mid
                    stamp = days[high][0]
                    answer = time.mktime(time.strptime(stamp, "%Y%m%d"))
            except (OSError, ValueError):
                answer = None

        self._first_seen_cache[fileset] = answer
        return answer

    def latest_archive_rows(self):
        # type: () -> List[Dict[str, str]]
        """The current cluster-wide dump, for cross-checking a live reading.

        Useful because it sees every fileset on the cluster rather than only
        the ones this user holds blocks in, which is how a fileset the user has
        group access to but has never written to becomes visible.
        """
        path = os.path.join(_ARCHIVE_DIR, _ARCHIVE_LATEST)
        try:
            with open(path, "r") as handle:
                return parse_mmrepquota_parsable(handle.read())
        except OSError:
            return []
