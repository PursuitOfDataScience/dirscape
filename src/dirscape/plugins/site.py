"""A site's non-declarative knowledge, driven entirely by `[plugin]` config.

`sitecfg` covers what a site can DESCRIBE. Three things a site may only be able
to EXECUTE live here instead, and every one of them is configured rather than
written into the package, so the package names no cluster at all:

1. **A fileset membership rule.** Group name, directory name and fileset name
   can be three different strings: a directory `/project/ops` group-owned by
   `ops-staff` and living in fileset `project-ops`. `owns_fileset` strips the
   configured prefixes, honours the configured special cases first, and
   accepts either the bare group or a `pi-` style group.

2. **An allocation command with its own table format.** Some sites print what
   storage an account has been granted, including allocations with no path on
   the node that printed them. That mismatch is the single most useful thing
   this tool reports, and it is only visible if something parses that table.

3. **A readable history of daily quota dumps.** Bisecting a directory of daily
   `mmrepquota` dumps dates a fileset's creation to the day, which is otherwise
   impossible where birth time is unavailable (`stat -c %W` returns 0 on GPFS)
   and directory mtime is a decoy that can read months late.

Point 3 is an accident of whichever site keeps such an archive. The core's
newness mechanism is its own snapshot lineage and never depends on it.

Every key is optional, and a site with no `[plugin]` section gets no plugin:

    [plugin]
    description            = Example Cluster
    detect_files           = /opt/site/bin/allocs
    detect_device_prefixes = example
    bin_dir                = /opt/site/bin
    allocation_command     = allocs storage
    fileset_prefixes       = project2-, project-
    group_prefixes         = pi-
    fileset_groups         = project-ops:ops-staff
    wrapper_paths          = /opt/site/bin/quota
    extra_bin_dirs         = /usr/lpp/mmfs/bin, /opt/site/bin
    dataset_roots          = /datasets
    snapshot_roots         = /snapshots
    roles                  = /work:project, /work/*:project
    quota_archive          = /var/lib/quota-history
    quota_archive_latest   = latest-parsable.quota
    quota_archive_daily    = ^(\\d{8})-gpfs\\.quota$
"""

import os
import re
import time
from typing import Dict, List, Optional, Sequence, Tuple

from . import Allocation, SitePlugin

__all__ = ["ConfiguredSite", "parse_allocation_table", "parse_mmrepquota_parsable"]

#: What a daily dump is called when `quota_archive_daily` does not say.
DEFAULT_ARCHIVE_DAILY = r"^(\d{8})-gpfs\.quota$"


def _split(value):
    # type: (object) -> List[str]
    """A config value as a list: comma or newline separated, or a JSON list."""
    if isinstance(value, (list, tuple)):
        return [str(item).strip() for item in value if str(item).strip()]
    out = []  # type: List[str]
    for line in str(value or "").replace(",", "\n").splitlines():
        line = line.strip()
        if line and not line.startswith("#"):
            out.append(line)
    return out


def _pairs(value):
    # type: (object) -> List[Tuple[str, str]]
    """`left:right` entries, split on the LAST colon so a glob keeps its own."""
    out = []  # type: List[Tuple[str, str]]
    for entry in _split(value):
        if ":" in entry:
            left, right = entry.rsplit(":", 1)
            if left.strip() and right.strip():
                out.append((left.strip(), right.strip()))
    return out


def parse_allocation_table(text, source="allocations"):
    # type: (str, str) -> List[Allocation]
    """Parse an allocation command's ASCII table.

    Shape, as measured at one site:

        +-----------+------+---------+--------+-------------------+
        |  Account  |  ID  |  Type   | GB(s)  |     Location      |
        +-----------+------+---------+--------+-------------------+
        | ops-staff | 301  | Special | 256000 |  cfs1/ops-staff   |

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
                # Deliberately NOT derived into a path. "cfs4/ops-staff" looks
                # like it should be "/cfs4/ops-staff" and often is, but the
                # allocation database is not a mount table and inventing a path
                # here would be exactly the guess that rapiDU's RD-3 made when
                # it attributed a /scratch walk to the wrong cluster.
                path=None,
                size_gb=size,
                kind=row.get("type", ""),
                start=row.get("start", ""),
                end=row.get("end", ""),
                source=source,
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


class ConfiguredSite(SitePlugin):
    """Whatever the `[plugin]` section of the site config describes."""

    name = "site"

    def __init__(self, settings=None):
        # type: (Optional[Dict[str, object]]) -> None
        self.settings = dict(settings or {})
        self._archive_days = None  # type: Optional[List[Tuple[str, str]]]
        self._first_seen_cache = {}  # type: Dict[str, Optional[float]]

    def _list(self, key):
        # type: (str) -> List[str]
        return _split(self.settings.get(key))

    def _text(self, key, default=""):
        # type: (str, str) -> str
        value = self.settings.get(key)
        return str(value).strip() if value not in (None, "") else default

    # -- detection -------------------------------------------------------

    def detect(self, runner, mounts):
        # type: (object, Sequence[object]) -> bool
        """Cheap, and never a hostname check.

        No `[plugin]` section means no plugin. With one, either configured
        signal is enough: a file only this site has, or a storage device named
        after one of its clusters. A section that names neither applies
        wherever it is read. A hostname check would be the wrong instrument,
        for the same reason nodetop filters GPU nodes on GRES rather than on a
        hostname prefix: a node called `...-bigmem1` can be a CPU node.
        """
        if not self.settings:
            return False
        files = self._list("detect_files")
        prefixes = tuple(self._list("detect_device_prefixes"))
        if not files and not prefixes:
            return True
        if any(os.path.isfile(path) for path in files):
            return True
        for mount in mounts or ():
            device = str(getattr(mount, "device", "") or "")
            if prefixes and device.startswith(prefixes):
                return True
        return False

    def describe(self):
        # type: () -> str
        return self._text("description", "a configured site")

    def site_defaults(self):
        # type: () -> Dict[str, object]
        """Config values, merged at the LOWEST precedence.

        Each path list is existence-checked, because one config serves every
        node of a site and a directory can exist on one class of node only:
        a snapshot tree published on login nodes is absent on every compute
        node, and a root that is not there must not become a row.
        """
        return {
            "fileset_prefixes": self._list("fileset_prefixes"),
            "group_prefixes": self._list("group_prefixes"),
            "wrapper_paths": [p for p in self._list("wrapper_paths") if os.path.isfile(p)],
            "extra_bin_dirs": self._list("extra_bin_dirs"),
            "dataset_roots": [p for p in self._list("dataset_roots") if os.path.isdir(p)],
            "snapshot_roots": [p for p in self._list("snapshot_roots") if os.path.isdir(p)],
            # A LABEL, and that is the whole effect: a project area named by
            # no built-in pattern scores the fallback role `other` and sorts
            # to the bottom of the table under that heading. Discovery does
            # not depend on the role, so the root is found either way.
            "role_globs": _pairs(self.settings.get("roles")),
        }

    # -- allocations -----------------------------------------------------

    def allocations(self, runner, budget):
        # type: (object, object) -> List[Allocation]
        """Run the configured allocation command and parse its table.

        Returns an empty list on any failure rather than raising. An allocation
        listing is an enrichment: without it the tool still reports every
        mounted root correctly, it just cannot say "allocated, not mounted
        here" about the ones that are missing.
        """
        command = self._text("allocation_command").split()
        if not command:
            return []
        bin_dir = self._text("bin_dir")
        extra = (bin_dir,) if bin_dir else ()
        exe = runner.available(command[0], extra_dirs=extra)  # type: ignore[attr-defined]
        if not exe:
            return []
        result = runner.run([exe] + command[1:])  # type: ignore[attr-defined]
        if result.failed:
            return []
        return parse_allocation_table(result.stdout, source=" ".join(command))

    # -- membership ------------------------------------------------------

    def owns_fileset(self, fileset, groups):
        # type: (str, Sequence[str]) -> Optional[bool]
        """The site's own rule, applied to one fileset.

        Special cases come first and the order matters: `project-ops` strips
        to `ops`, which can be a real group too, so testing the general rule
        first would grant it to every member of `ops`. Reproduced exactly
        rather than tidied, because the site's rule is the site's definition
        of the answer and a cleaner rule would be a different, wrong answer.

        Returns None, not False, for a fileset the rule does not recognise. A
        rule that has never heard of a fileset has not established that the
        user lacks it, and the caller must keep looking rather than print "no".
        """
        if not fileset:
            return None
        group_set = set(groups or ())

        for special, needed in _pairs(self.settings.get("fileset_groups")):
            if fileset == special:
                return needed in group_set

        name = fileset
        for prefix in self._list("fileset_prefixes"):
            if name.startswith(prefix):
                name = name[len(prefix) :]
                break
        else:
            # No recognised prefix means this is not a project fileset at all
            # (`home`, `software`, `scratch`), and this rule has nothing to say
            # about it. Those are reached through the mount table instead.
            return None

        group_prefixes = self._list("group_prefixes") or ["pi-"]
        return bool(name in group_set or any(p + name in group_set for p in group_prefixes))

    # -- history ---------------------------------------------------------

    def _daily_files(self):
        # type: () -> List[Tuple[str, str]]
        """Dated archive files as (YYYYMMDD, path), oldest first."""
        if self._archive_days is not None:
            return self._archive_days

        found = []  # type: List[Tuple[str, str]]
        directory = self._text("quota_archive")
        pattern = re.compile(self._text("quota_archive_daily", DEFAULT_ARCHIVE_DAILY))
        if directory:
            try:
                with os.scandir(directory) as entries:
                    for entry in entries:
                        match = pattern.match(entry.name)
                        if match:
                            found.append((match.group(1), entry.path))
            except OSError:
                # Absent on every node that does not mount it, and unreadable
                # to a user without traversal on its parent. Both are normal.
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

        Bisection rather than a linear scan because an archive can hold about
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
        directory = self._text("quota_archive")
        latest = self._text("quota_archive_latest")
        if not directory or not latest:
            return []
        try:
            with open(os.path.join(directory, latest), "r") as handle:
                return parse_mmrepquota_parsable(handle.read())
        except OSError:
            return []
