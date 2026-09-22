"""The command line: one pipeline, six views.

Everything here is orchestration. The facts come from `discover`, `quota`,
`state` and the site plugins; the words come from `render`. This module decides
the ORDER, and the order has three constraints that are not obvious and that
each cost a wrong answer if broken:

1. **Quota runs before discovery.** A GPFS per-device quota listing enumerates
   every fileset the user holds blocks in, including ones they have no unix
   group for, and that list is a discovery source no other probe can supply.
   It is the only route to `stranded` storage.
2. **`Snapshot.from_roots` runs before `lineage.append`.** Otherwise "the
   earliest run that saw this root" becomes "this run" and nothing is ever new.
3. **Rendering runs last and reads nothing.** Every value it prints was
   measured by a layer above it, so a field nobody supplied prints `?` rather
   than being filled in at the last moment.

Exit codes, so a script can branch on them:

    0   the run completed and printed a view
    1   bad usage, or an unexpected internal error
    2   a path the user named could not be used
    3   nothing was discovered at all, which on a working cluster means the
        probes were all refused rather than that the storage is empty
"""

import argparse
import contextlib
import errno
import os
import shutil
import sys
import time
from typing import Callable, Dict, List, Optional, Sequence, Tuple

from . import __version__, interactive
from .discover import (
    RANK_PRIMARY,
    attribute_all,
    discover,
    node_class,
    read_identity,
    read_mount_table,
    restates_source,
    source_label,
)
from .model import Reach, VerdictCategory, confirmed, refuted, sanitize, unknown
from .plugins import detect_plugins
from .quota import default_backends, filesets_seen, read_all, select_snapshot
from .render import fields as render_fields
from .render import (
    render_atlas,
    render_json,
    render_matrix,
    render_ncdu,
    render_tree,
    render_treemap,
    resolve_style,
)
from .render import style as render_style
from .render.atlas import _MIN_BODY
from .render.style import panel, plain
from .runner import Budget, RecordedRunner, SubprocessRunner
from .sitecfg import SITE_TEMPLATE, guess_cluster_name, load_site
from .state import Lineage, Snapshot, cutoff_for, diff

EXIT_OK = 0
EXIT_USAGE = 1
EXIT_PATH = 2
EXIT_NOTHING = 3

#: Total wall-clock allowance for one run. Eight seconds is generous for a tool
#: that stats tens of paths; it exists so one wedged mount cannot make the
#: command hang, not as a performance target. Measured on a six-device GPFS
#: login node: 46 roots in about two seconds including every quota backend.
DEFAULT_TIMEOUT_S = 8.0

_DURATION_UNITS = {"s": 1, "m": 60, "h": 3600, "d": 86400, "w": 604800}


def parse_duration(text):
    # type: (str) -> float
    """`30d`, `12h`, `90` (seconds). Raises ValueError on anything else."""
    original = (text or "").strip()
    raw = original.lower()
    if not raw:
        raise ValueError("empty duration")
    unit = 1
    if raw[-1] in _DURATION_UNITS:
        unit = _DURATION_UNITS[raw[-1]]
        raw = raw[:-1]
    try:
        value = float(raw)
    except ValueError:
        # Report what the USER typed. Letting float's own message out said
        # "could not convert string to float: 'bogu'", having silently eaten
        # the last character as a unit, which sends a reader looking for a
        # typo they did not make.
        # `from None` rather than chaining: float's own message is exactly the
        # thing being replaced, so keeping it in the traceback puts the
        # misleading text back.
        raise ValueError(
            "%r is not a duration; use a number optionally followed by %s"
            % (original, "/".join(sorted(_DURATION_UNITS)))
        ) from None
    if value < 0:
        raise ValueError("a duration cannot be negative")
    return value * unit


# --------------------------------------------------------------------------
# Argument parsing
# --------------------------------------------------------------------------


def _absent(suppress):
    # type: (bool) -> object
    """The default for a SUBCOMMAND copy of a global flag.

    `argparse.SUPPRESS`, not `None`, and this is load bearing. With `None` the
    subparser writes its own default over whatever the top-level parser already
    stored, so `dirscape --json new` came out with `json=None` while
    `dirscape new --json` worked. That is the nodetop `parents=[...]` bug in a
    different costume: building fresh action objects per parser is necessary
    and not sufficient, because the second parse still runs and still assigns.
    SUPPRESS makes an absent flag leave the namespace untouched.
    """
    return argparse.SUPPRESS if suppress else None


def _add_global_args(parser, suppress=False):
    # type: (argparse.ArgumentParser, bool) -> None
    """Register the global flags, building FRESH action objects each call.

    Called once for the top-level parser and once per subcommand, which is how
    `dirscape --json new` and `dirscape new --json` both work. It deliberately
    does NOT use `parents=[common]`: argparse stores the same action objects in
    both parsers then, so whichever parser parses last wins and the other
    silently discards the flag. nodetop shipped that bug and `nodetop --json
    status` ignored `--json` because of it.

    `suppress` keeps the subcommand copies out of the top-level help, so the
    same flag is not documented seven times.
    """
    helps = argparse.SUPPRESS if suppress else None

    def h(text):
        # type: (str) -> str
        return argparse.SUPPRESS if helps is argparse.SUPPRESS else text

    parser.add_argument(
        "--json",
        action="store_true",
        default=_absent(suppress),
        help=h("emit the native JSON schema, with a reason code on every unknown"),
    )
    parser.add_argument(
        "--ascii",
        action="store_true",
        default=_absent(suppress),
        help=h("ASCII only, no box drawing or block characters"),
    )
    parser.add_argument(
        "--color",
        choices=("auto", "always", "never"),
        default=_absent(suppress),
        help=h("colour output (default auto; NO_COLOR and TERM=dumb are honoured)"),
    )
    parser.add_argument(
        "--all",
        action="store_true",
        default=_absent(suppress),
        help=h("include secondary roots: filesystem roots, aliases and pseudo mounts"),
    )
    parser.add_argument(
        "--probe-write",
        action="store_true",
        default=_absent(suppress),
        help=h("actually test writability by creating and removing a temporary file"),
    )
    parser.add_argument(
        "--timeout",
        type=float,
        default=_absent(suppress),
        metavar="SECONDS",
        help=h("total allowance for the whole run (default %.0f)" % (DEFAULT_TIMEOUT_S,)),
    )
    parser.add_argument(
        "--replay",
        metavar="FILE",
        default=_absent(suppress),
        help=h("replay a captured transcript instead of running commands"),
    )
    parser.add_argument(
        "--no-state",
        action="store_true",
        default=_absent(suppress),
        help=h("do not read or write the snapshot lineage"),
    )
    parser.add_argument(
        "--timing",
        action="store_true",
        default=_absent(suppress),
        help=h("report how long each stage took"),
    )
    parser.add_argument(
        "--legend",
        action="store_true",
        default=_absent(suppress),
        help=h("explain the reach letters and the figure marks"),
    )
    parser.add_argument(
        "--summary",
        action="store_true",
        default=_absent(suppress),
        help=h("add the stranded, elsewhere and hidden-row counts under the table"),
    )
    parser.add_argument(
        "-v",
        "--verbose",
        action="store_true",
        default=_absent(suppress),
        help=h("in `why`, add where each figure came from and how the path was found"),
    )
    parser.add_argument(
        "--no-measure",
        action="store_true",
        default=_absent(suppress),
        help=h("skip the bounded walk of roots that have no quota, leaving them unknown"),
    )


def build_parser():
    # type: () -> argparse.ArgumentParser
    parser = argparse.ArgumentParser(
        prog="dirscape",
        description=(
            "Every storage root you can actually reach on this cluster, "
            "and what is new since last time."
        ),
        epilog=(
            "dirscape never walks a directory tree: its cost is the number of "
            "roots, not the number of files. For bytes per directory use `rdu` "
            "or `ncdu`."
        ),
    )
    parser.add_argument("-V", "--version", action="version", version="dirscape " + __version__)
    parser.add_argument(
        "--site-template",
        action="store_true",
        help="print a commented /etc/dirscape/site.conf and exit",
    )
    _add_global_args(parser)

    subs = parser.add_subparsers(dest="command", metavar="COMMAND")

    def sub(name, help_text):
        # type: (str, str) -> argparse.ArgumentParser
        child = subs.add_parser(name, help=help_text, description=help_text)
        _add_global_args(child, suppress=True)
        return child

    sub("atlas", "the default table: one row per reachable root")

    new = sub("new", "what changed since the last run")
    new.add_argument(
        "--since",
        metavar="AGE",
        default=None,
        help="compare against the oldest snapshot at least this old, e.g. 30d",
    )

    why = sub("why", "every probe run on one path, and what each said")
    why.add_argument("path", help="the path to explain")

    sub("matrix", "roots by capability, as confirmed / refused / could not determine")
    sub("tree", "device, then fileset, then paths, with quota-crossing symlinks marked")
    sub("map", "a treemap of where your bytes live, across every root at once")
    sub("stranded", "space you hold in filesets you cannot reach")
    sub("elsewhere", "allocations with no path on this node")
    sub("snapshot", "record a baseline without printing a table")

    export = sub("ncdu", "export one root as ncdu-compatible JSON")
    export.add_argument("path", help="the root to export")

    return parser


def _merge_flag(namespace, name, default):
    # type: (argparse.Namespace, str, object) -> object
    """Resolve a flag that may have been given on either side of the verb.

    Both copies default to None, so "the user said nothing" is distinguishable
    from "the user said false", and the first non-None wins.
    """
    value = getattr(namespace, name, None)
    return default if value is None else value


# --------------------------------------------------------------------------
# The pipeline
# --------------------------------------------------------------------------


class Run(object):
    """One invocation's collected facts, so the views share a single sweep."""

    __slots__ = (
        "roots",
        "identity",
        "mounts",
        "site",
        "plugins",
        "allocations",
        "quota_attempts",
        "changes",
        "lineage",
        "snapshot",
        "meta",
        "warnings",
        "timings",
        "discovery",
        "baseline_at",
        "saved",
    )

    def __init__(self):
        # type: () -> None
        self.roots = []  # type: List[object]
        self.identity = None  # type: object
        self.mounts = None  # type: object
        self.site = None  # type: object
        self.plugins = []  # type: List[object]
        self.allocations = []  # type: List[object]
        self.quota_attempts = []  # type: List[object]
        self.changes = None  # type: object
        self.lineage = None  # type: object
        self.snapshot = None  # type: object
        self.meta = None  # type: object
        self.warnings = []  # type: List[str]
        self.timings = []  # type: List[Tuple[str, float]]
        self.discovery = unknown(VerdictCategory.NOT_PROBED)
        self.baseline_at = None  # type: Optional[float]
        # False once a save has been attempted and failed.
        self.saved = True


def _apply_plugin_defaults(site, plugins):
    # type: (object, Sequence[object]) -> None
    """Merge each plugin's `site_defaults` at the LOWEST precedence.

    Appended rather than assigned, so anything a real config file said stays
    in front of a plugin's guess and an administrator can always override a
    plugin without editing the package.
    """
    for plugin in plugins:
        try:
            defaults = plugin.site_defaults()
        except Exception:
            continue
        for key, value in (defaults or {}).items():
            current = getattr(site, key, None)
            if isinstance(current, list) and isinstance(value, (list, tuple)):
                for item in value:
                    if item not in current:
                        current.append(item)


def _owned_by_caller(path):
    # type: (str) -> bool
    """Whether this process's uid owns the directory.

    A measurement, not a name match. `/project/hpc/jdoe42` being named after
    the user is a convention of one site; owning it is a fact everywhere.
    """
    try:
        return os.stat(path).st_uid == os.getuid()
    except OSError:
        return False


def _attach_quota(run, budget, runner, measure=False, show_all=False):
    # type: (Run, Budget, object, bool, bool) -> None
    """Give every root the quota reading that actually governs it.

    One `read_all` sweep for the whole run, then per-root selection, rather
    than one `read_best` per root: the backends are the expensive part and
    asking each of them once per root would multiply a two-second run by forty.
    """
    # Reuses the sweep's readings rather than asking again. A GPFS per-device
    # listing already returns every row the user has on that device whatever
    # path was named, so a second pass bought nothing and paid for every
    # backend twice. It also clobbered `run.quota_attempts`, which made this
    # function impossible to test without a live cluster.
    if not run.quota_attempts:
        backends = default_backends(run.site)
        paths = [r.path for r in run.roots if getattr(r, "path", "")]
        run.quota_attempts = read_all(backends, runner, run.mounts, budget, paths or ["/"])

    # A quota governs a FILESET, not a directory, so it is shown on the root
    # where that fileset begins and nowhere else. Without this, every
    # subdirectory of a fileset repeats the fileset's whole figure: an
    # integration run showed `/project2/reference/fin` holding 23T, which is
    # the entire shared dataset area, and `/project/hpc/jdoe42/.cache/tmp`
    # holding 11T. Both are true of the fileset and false of the directory,
    # and a number that is true of something else is the kind of wrongness
    # this tool exists to avoid.
    #
    # WHICH root in the fileset shows the figure depends on the row's scope,
    # because the two scopes mean different things:
    #
    # * a USER-scoped row is your own usage inside the fileset, so it belongs
    #   on the deepest directory you can actually write to. Putting it on the
    #   junction is backwards from the reader's point of view: an earlier pass
    #   showed `835M / 30G` against `/home`, which nobody can write to, while
    #   `/home/jdoe42` read `?`.
    # * a FILESET or GROUP scoped row is the whole fileset's usage, so it
    #   belongs at the junction where the fileset enters the namespace.
    # Keyed on (device, fileset), NEVER on the fileset name alone. A fileset
    # name is unique within a filesystem and not across one: this node mounts
    # `scratch` on meadow3_perf, meadow2_perf AND collie3_perf, and `home` and
    # `software` collide the same way. Keying on the name alone let the
    # collie3 scratch claim the junction for all three, so
    # `/scratch/meadow3/jdoe42` reported `?` while holding 22G. This is the
    # exact collision `QuotaRow.label` qualifies against, made in the consumer
    # after the model had already warned about it.
    junction = {}  # type: Dict[Tuple[str, str], str]
    accessible = {}  # type: Dict[Tuple[str, str], str]
    owned = {}  # type: Dict[Tuple[str, str], str]
    writable_only = {}  # type: Dict[Tuple[str, str], str]
    for root in run.roots:
        name = getattr(root, "fileset", "") or ""
        path = getattr(root, "path", "") or ""
        if not name or not path:
            continue
        key = (getattr(root, "device", "") or "", name)
        current = junction.get(key)
        if current is None or len(path) < len(current):
            junction[key] = path
        # "Yours" is decided by measured OWNERSHIP first and write access
        # second, never by a name match against the username, which would be
        # wrong for a shared group directory and for anyone whose directory is
        # not named after them.
        #
        # Ownership matters because the figure is user-scoped: it is YOUR
        # usage. `/project/hpc` is owned by uid 0 and `/project/hpc/jdoe42` by
        # the caller, so putting the 11T on the former says the group holds
        # 11T, which is a different and wrong claim. An earlier version keyed
        # on write access alone, and the moment the write probe started
        # answering for group directories the figure jumped up a level.
        #
        # Shallowest of the owned candidates, because the figure describes a
        # whole subtree. Deepest was tried and is absurd: it put the 11T on
        # `/project/hpc/jdoe42/.cache/tmp`.
        # The figure must land on the SAME root the table decides to show,
        # which is the highest fully-accessible directory in the subtree. When
        # these two rules disagreed the number vanished: the quota attached to
        # `/project/hpc/jdoe42` on an ownership preference while the table
        # kept `/project/hpc`, so the 11T was folded out of sight and the row
        # fell back to the filesystem's free space.
        #
        # Ownership is kept as the tie-break below it, for the case where no
        # root in the fileset is fully accessible and the choice is between
        # somebody else's directory and your own.
        if _full_access(root):
            best = accessible.get(key)
            if best is None or len(path) < len(best):
                accessible[key] = path
        writable = getattr(root, "writable", None)
        if writable is not None and writable.confirmed:
            mine = _owned_by_caller(path)
            bucket = owned if mine else writable_only
            best = bucket.get(key)
            if best is None or len(path) < len(best):
                bucket[key] = path

    for root in run.roots:
        if not getattr(root, "path", ""):
            continue
        snap = select_snapshot(run.quota_attempts, root.path)
        if snap is None:
            continue
        rows = _rows_governing(snap, root)
        if not rows:
            continue

        name = getattr(root, "fileset", "") or ""
        key = (getattr(root, "device", "") or "", name)
        scopes = {row.scope for row in rows}
        if name and scopes and scopes <= {"user"}:
            owner = (
                accessible.get(key) or owned.get(key) or writable_only.get(key) or junction.get(key)
            )
        else:
            owner = junction.get(key)
        if name and owner and owner != root.path:
            # Inside the fileset but not at its junction. Say where the figure
            # lives rather than repeating it here.
            root.add_note("quota is a property of fileset %s, reported on %s" % (name, owner))
            continue

        blocks = [row for row in rows if row.kind == "blocks"]
        files = [row for row in rows if row.kind == "files"]
        if blocks:
            root.quota = _single_row_snapshot(snap, blocks[0])
        if files:
            root.inode_quota = _single_row_snapshot(snap, files[0])

    _attach_capacity(run)
    if measure:
        # After the backends and the capacity fallback, so it only ever walks
        # what nothing else could answer for.
        _measure(run, budget, show_all=show_all)


def _attach_capacity(run):
    # type: (Run) -> None
    """Last resort for a root no quota backend could speak for: `statvfs`.

    Four rows of the default view were a bare `?`: `/tmp`, `/.nodelog/log`,
    `/scratch/local/jdoe42` and one GPFS scratch the wrapper could not
    attribute. Two of those are XFS mounted `noquota`, so no quota exists to
    read and `?` was the literal truth and useless anyway: the filesystem
    knows exactly how much room is left and `df` prints it.

    Stored as free BYTES rather than as a quota row, because it is not your
    usage: it is the whole filesystem's headroom, shared with everyone else on
    the node. Labelling it as a quota would be the fabrication this tool
    exists to avoid; withholding it when `df` would answer is just unhelpful.

    **Read for EVERY root now, not only the ones with no quota.** It used to
    skip anything a backend had spoken for, which left the `free` column
    reading `?` on six of ten rows: every uncapped fileset knows its usage and
    has no allowance to subtract, so the filesystem's headroom is the only
    answer available to "how much can I still put here". Where both are known
    `free_cell` takes the smaller, because a 40T allowance on a filesystem
    with 2T left is 2T of writes. One `statvfs` per root is a single syscall
    against a mount that is already known to be present.
    """
    for root in run.roots:
        if not root.path:
            continue
        if not root.present.confirmed:
            continue
        try:
            stats = os.statvfs(root.path)
        except OSError:
            continue
        if not stats.f_frsize or stats.f_blocks <= 0:
            continue
        root.policy = dict(root.policy or {})
        root.policy["free_bytes"] = int(stats.f_bavail) * int(stats.f_frsize)
        root.policy["size_bytes"] = int(stats.f_blocks) * int(stats.f_frsize)


#: Per-root ceiling for the measuring walk, in seconds. A walk is the only way
#: to a number on a filesystem with no quota accounting, and it is also the
#: one operation in this package whose cost is the number of FILES rather than
#: the number of roots, so it is bounded at both ends and gives up rather than
#: hanging. Measured on the two roots that need it here: `/scratch/local` is
#: empty and `/tmp` is 2,220 files, both in 0.01s.
WALK_SECONDS = 1.5

#: And a ceiling on ENTRIES, which is the bound that actually protects a login
#: node. A deadline alone means a pathological tree costs its full
#: `WALK_SECONDS` before being abandoned, once per root; a count lets the walk
#: recognise within milliseconds that this is not the kind of directory it can
#: add up, and stop. 100k entries is about two orders of magnitude above the
#: largest root here and still finishes in well under the deadline.
WALK_ENTRIES = 100_000

#: And a ceiling on the WHOLE measuring pass, because the per-root one does
#: not bound the run: four roots with no quota would be four times
#: `WALK_SECONDS` added to a tool that otherwise finishes in under three.
#: Measured against the worst tree on this node, `/software`, which is
#: quota-bearing and so never walked: it burns a full `WALK_SECONDS` without
#: finishing, so this is the difference between one slow root costing 1.5s and
#: several costing 6.
WALK_TOTAL_SECONDS = 2.5


def _measure(run, budget=None, show_all=False):
    # type: (Run, Optional[Budget], bool) -> None
    """Walk the roots no quota system can speak for. On by default, bounded.

    The owner asked the right question about the `?` marks: "do you have a way
    to tell the exact number? having too many ? can impact user experience,
    and they will think you don't know things."

    Three of the four were answerable and are answered elsewhere: one was an
    attribution bug (`_device_wide`), and the figures for every quota-bearing
    root come from the backends. The rest are `noquota` mounts, measured:
    `/tmp` and `/scratch/local` on this node are XFS mounted with `noquota`,
    so there is no per-user accounting anywhere in the kernel to ask. The only
    remaining source of truth is adding the files up.

    **It was opt-in and is now the default, because a flag is a bad answer to
    "why are there still question marks".** The owner saw the two remaining
    `?` rows and said "something is wrong", which is exactly the reaction the
    marks were meant to avoid: a reader cannot tell "nobody could measure
    this" from "this tool did not try".

    The package's claim is that its cost is the number of roots and not the
    number of files, and that still holds, because the walk is bounded at both
    ends and applies only to roots no backend could answer for. Two bounds
    rather than one: `WALK_SECONDS` per root, and `WALK_ENTRIES`, which is what
    actually protects a login node. A deadline alone means a pathological tree
    costs its full deadline before being abandoned; a count lets the walk see
    within milliseconds that this is not a directory it can add up.

    Either bound tripping leaves the `?` in place with a reason rather than
    reporting a partial sum as a total. **A number that is quietly too small
    is worse than no number**, and this is the one place in the package where
    a wrong figure is cheap to produce. `--no-measure` turns it off.

    `st_blocks`, not `st_size`: this is space CHARGED, so a sparse file counts
    what it occupies and the figure is comparable with a quota reading.
    """
    from .model import QuotaRow

    # **Only the rows the default view will actually show**, which is the
    # difference between walking two directories and walking forty.
    #
    # The first version iterated every discovered root and took 8.7 seconds
    # while producing no figures at all. The candidate list included
    # `/scratch/meadow3`, `/project2/reference/pdb` and `/project2/biokit`:
    # whole shared dataset trees, none of which appear in the default view,
    # every one of them enormous, and each burning its full deadline to
    # produce a timed-out partial that was then correctly discarded. Walking
    # storage the reader is not looking at is pure cost.
    # **The rows the default view shows come FIRST, then everything else.**
    #
    # Ordering, not filtering, and the difference matters at both ends. The
    # first version walked every discovered root and spent its whole budget on
    # shared dataset trees nobody was looking at; the second walked only the
    # visible set and left `--all` with 144 question marks, including
    # `/project/hpc/jdoe42`, which is a directory the reader owns and whose
    # size a walk answers in milliseconds.
    #
    # Sorting by visibility gets both: the main view is never degraded by a
    # slow root it does not show, and the plumbing views get whatever time is
    # left. `/` and the `/gpfs/*` aliases are in that tail and will mostly run
    # out, which is the honest outcome for a filesystem root nobody has a
    # per-user quota on.
    # **The rows this run will actually show, and no others.**
    #
    # Three versions of this scope, and the measurements are why it ended
    # here. Walking every discovered root spent the whole budget on shared
    # dataset trees nobody was looking at and filled in nothing. Walking the
    # default-visible set left `--all` with 144 question marks. Walking
    # everything with the visible rows sorted first kept the default view
    # correct and doubled the run to 5.6s while removing three of those 144,
    # because the tail is `/`, `/home`, `/project` and seven `/gpfs/*`
    # aliases: filesystem roots with no per-user quota, each burning its full
    # deadline to produce a partial that is correctly discarded.
    #
    # **Letting `--all` opt into the tail was tried and is the reason this
    # says `show_all=False`.** It took the `--all` run from 2.9s to 15s and
    # removed one question mark out of 144, because the deadline cannot
    # interrupt a single `scandir`: one call against a GPFS directory with
    # hundreds of entries, each needing a `stat`, runs for seconds before the
    # clock is looked at again. The bounds hold at the level they are checked
    # and that level is coarse, so the only safe policy is not to start.
    #
    # What stays unmeasured in `--all` is filesystem roots and aliases (`/`,
    # `/home`, `/project`, seven `/gpfs/*`), and other people's project
    # directories. None of those has a per-user quota to report, so `?` there
    # is the correct answer rather than a gap: the number does not exist, and
    # the only way to invent one is the tree walk this package refuses.
    shown, _hidden = _visible(run, show_all=False)
    targets = [root for root in shown if root.quota is None and root.path]
    overall = time.time() + WALK_TOTAL_SECONDS

    for root in targets:
        if time.time() >= overall:
            root.add_note(
                "there was not enough time left to add this directory up, so its size "
                "stays unknown: rdu or du will measure it"
            )
            continue
        if not root.present.confirmed or root.reach != Reach.LISTABLE:
            continue
        # **Its own clock, not the leftovers of the global budget.** The
        # global budget is nearly spent by the time the backends have all
        # answered, so taking `min(WALK_SECONDS, budget.remaining)` set the
        # deadline to the current instant and every single walk reported
        # "timed out" without reading a directory. `--measure` is an explicit
        # request for work the tool otherwise refuses to do, so it is paid for
        # separately; the per-root ceiling is what stops it running away.
        deadline = min(time.time() + WALK_SECONDS, overall)
        used, files, complete = _walk(root.path, deadline, WALK_ENTRIES)
        root.policy = dict(root.policy or {})
        if not complete:
            root.add_note(
                "this directory is too large to add up quickly (over %s entries or %.1fs), "
                "so its size stays unknown rather than being reported short: rdu or du "
                "will measure it properly" % (render_fields.human_count(WALK_ENTRIES), WALK_SECONDS)
            )
            continue
        root.policy["walked"] = True
        # `soft=0, hard=0` is the model's way of saying no limit is enforced,
        # as opposed to a limit nobody measured. Walking answers how much is
        # THERE and says nothing about a ceiling, and on these mounts there is
        # no ceiling to find: they are the ones with no quota system, which is
        # why they are being walked. Leaving the limits unset made the cell
        # fall back to `?` and undid the answer the mount table had already
        # given two lines earlier.
        root.quota = _single_row_snapshot(
            _WalkSource,
            # **No fileset name.** It used to key on `root.path`, which gave
            # each walked root a fileset of its own: `dirscape tree` groups by
            # fileset, so `/tmp` and `/scratch/local/jdoe42`, two mounts of
            # one `/dev/sda1`, became two nodes with conflicting names and the
            # view fell back to `?` for the device. A walk measures a
            # DIRECTORY, not a quota scope, and saying so is the honest shape.
            QuotaRow("", "blocks", "user", used, soft=0, hard=0, mount=root.path),
        )
        root.inode_quota = _single_row_snapshot(
            _WalkSource,
            QuotaRow("", "files", "user", files, soft=0, hard=0, mount=root.path),
        )
        root.add_note(
            "no quota system exists here, so these figures were measured by walking the "
            "directory (%s files), which is what du does" % (render_fields.human_count(files),)
        )


class _WalkSource(object):
    """The `source` a walked figure reports, so `why` can name where it came
    from without pretending a quota backend answered.

    Shaped to satisfy `_single_row_snapshot`, which reads every doubt channel
    off whatever it is handed. A walk has no staleness (it happened just now)
    and no figure doubt of the GPFS kind, but it does carry one real caveat
    and the right place for it is a note on the root, not this object.
    """

    source = "walk"
    category = VerdictCategory.OK
    reason = "measured by walking the directory, because no quota exists here"
    taken_at = None
    read_at = None
    time_note = ""
    figure_note = ""


def _walk(top, deadline, ceiling):
    # type: (str, float, int) -> Tuple[int, int, bool]
    """Bytes charged and files counted under ``top``, or as far as time allowed.

    Iterative rather than recursive, so a pathological depth cannot blow the
    stack, and `scandir` rather than `walk` so each entry's type comes from
    the directory read instead of a second `stat`. Symlinks are never
    followed: they are counted where they point, by whichever root owns that
    storage, and following them here would double-count a home directory whose
    dotfiles live in `/project`.
    """
    total = 0
    files = 0
    seen = 0
    stack = [top]
    while stack:
        if seen > ceiling or time.time() > deadline:
            return total, files, False
        current = stack.pop()
        try:
            entries = list(os.scandir(current))
        except OSError:
            # A directory inside a readable tree that we cannot enter is one
            # subtree missing from the sum, not a failure of the whole walk.
            continue
        seen += len(entries)
        if seen > ceiling:
            # Checked AFTER the read as well as before it. Checking only at
            # the top of the loop meant the bound could not see a single
            # directory holding more entries than the ceiling: the first pass
            # starts at zero, scans the whole thing, and finds an empty stack.
            # One flat directory with millions of entries is the realistic
            # shape for a scratch or `/tmp` tree, so it was the case the bound
            # most needed to catch and the one case it missed.
            return total, files, False
        for entry in entries:
            try:
                if entry.is_dir(follow_symlinks=False):
                    stack.append(entry.path)
                    continue
                stat = entry.stat(follow_symlinks=False)
            except OSError:
                continue
            # **`st_blocks`, falling back to `st_size` when it is zero.**
            #
            # Blocks are the right unit: it is space CHARGED, so a sparse file
            # counts what it occupies and the figure is comparable with a
            # quota reading. But GPFS reports `st_blocks == 0` for a file
            # small enough to live in its inode, and with a 4 MiB block size
            # that is most small files. Measured on this cluster: a 4096 byte
            # file is `st_size=4096, st_blocks=0` on GPFS and
            # `st_size=4096, st_blocks=8` on XFS.
            #
            # The bug that found this was a walked directory of three 4 KiB
            # files reporting `0B used`, which is the worst failure available
            # to this code: not an error, not a `?`, a confident zero. Zero
            # blocks with a non-empty file means the bytes are somewhere the
            # block count cannot see them, and `st_size` is the only other
            # answer. Sparse files keep their block figure, because theirs is
            # non-zero.
            blocks = int(getattr(stat, "st_blocks", 0)) * 512
            total += blocks if blocks else int(getattr(stat, "st_size", 0))
            files += 1
    return total, files, True


def _rows_governing(snap, root):
    # type: (object, object) -> List[object]
    """The rows that govern THIS root, matched on fileset and not on prefix.

    `QuotaSnapshot.rows_for_path` is a path-prefix match, and on its own it is
    wrong here in a way that produces a confidently incorrect number. A
    `project-hpc` row whose mount was only ever INFERRED as `/project` matches
    every `/project/*` path by prefix, so an integration run reported
    `/project/abe`, `/project/bard`, `/project/dahlias`, `/project/mdgreenwood`
    and `/project/pelican` as each holding 11T, which is the usage of a
    different PI's directory entirely. Same shape on `/project2/reference/*`,
    where nineteen dataset directories all read 928K.

    That is rapiDU's RD-3 and RD-18 arriving at the attach step after both had
    been fixed inside the backend, which is the lesson: the backend can be
    right about a row and the consumer can still misapply it.

    So the rule is narrow, and it prefers reporting nothing to reporting
    somebody else's bytes:

    * the root's fileset is known and a row names the same fileset  -> use it;
    * the root's fileset is known and no row names it               -> nothing;
    * the root's fileset is unknown and a row's mount is EXACTLY the root's
      path                                                          -> use it;
    * anything else                                                 -> nothing.

    "Nothing" renders as `?`, which is the honest answer for a path whose
    quota this tool could not attribute.
    """
    rows = snap.rows_for_path(root.path)
    if not rows:
        return []

    fileset = getattr(root, "fileset", "") or ""
    if fileset:
        named = [row for row in rows if row.fileset and row.fileset == fileset]
        if named:
            return named
        return _device_wide(rows, root)

    target = root.path.rstrip("/") or "/"
    return [row for row in rows if (row.mount or "").rstrip("/") == target]


def _device_wide(rows, root):
    # type: (Sequence[object], object) -> List[object]
    """A user quota on the whole DEVICE, for a path with no fileset of its own.

    The fourth `?` the owner asked about, and it was a bug rather than a limit
    of what the filesystem knows. `/scratch/meadow2/jdoe42` read `?` for both
    figures while `mmlsquota -u jdoe42 meadow2_perf` reports 0 used against a
    100G quota and a 5T hard limit. Nobody was withholding it: the two sides
    could not be joined.

    `mmlsattr -L` says this path is in the `root` fileset, which GPFS uses for
    a filesystem's own top level and which `attribute.py` already flags as not
    a real scope. The backend, reading a device-wide USR row that names no
    fileset, labels it with the DEVICE (`meadow2_perf`). So the equality test
    in the caller compared `root` against `meadow2_perf` and returned nothing.

    Attributing it is CORRECT rather than a convenient guess, and the
    distinction matters because the caller's whole purpose is refusing to
    report somebody else's bytes. A user-scope quota on a device applies to
    that user everywhere on the device, so it necessarily covers this path.
    Four conditions, all required:

    * the root has no fileset of its own (`fileset_is_filesystem_root`), so
      there is no narrower scope this could be shadowing;
    * the row is user-scoped, not fileset or group scoped;
    * the row is device-wide, which the backend marks by naming the device as
      the fileset;
    * and it is the SAME device.

    The figure is the user's usage across the whole device, which may be more
    than this one directory holds, so a caveat says so and `why` prints it.
    """
    if not (root.policy or {}).get("fileset_is_filesystem_root"):
        return []
    device = getattr(root, "device", "") or ""
    if not device:
        return []
    out = []  # type: List[object]
    for row in rows:
        if getattr(row, "scope", "") != "user":
            continue
        if (getattr(row, "device", "") or "") != device:
            continue
        if (getattr(row, "fileset", "") or "") != device:
            continue
        out.append(row)
    if out and not root.policy.get("device_wide_quota"):
        root.policy["device_wide_quota"] = True
        root.add_note(
            "this quota covers your usage across the whole of %s, not only this "
            "directory, because the filesystem has no per-directory scope here" % (device,)
        )
    return out


def _single_row_snapshot(source, row):
    # type: (object, object) -> object
    """A one-row view of a backend reading, carrying its doubt channels.

    The notes travel with the row rather than being dropped, because "this
    figure is half an hour old" and "this figure excludes an OST I could not
    reach" are exactly what a user needs when a number looks wrong.
    """
    from .model import QuotaSnapshot

    return QuotaSnapshot(
        source.source,
        rows=[row],
        available=True,
        category=source.category,
        reason=source.reason,
        taken_at=source.taken_at,
        read_at=source.read_at,
        time_note=source.time_note,
        figure_note=source.figure_note,
    )


def _mark_stranded(run):
    # type: (Run) -> int
    """Flag filesets the user holds blocks in but cannot reach.

    The comparison is on the FILESET, not the path, because that is the whole
    point: stranded space has no reachable path by definition, so a
    path-keyed check would find nothing. Measured on one account: five
    `project-*` filesets holding about 19 GB with no group to reach them.
    """
    reachable = set()
    for root in run.roots:
        if getattr(root, "fileset", "") and root.reach in (Reach.LISTABLE, Reach.TRAVERSE):
            reachable.add(root.fileset)

    held = []  # type: List[str]
    for snap in run.quota_attempts:
        try:
            held.extend(filesets_seen(snap))
        except Exception:
            continue

    stranded = [name for name in held if name and name not in reachable]
    if not stranded:
        return 0

    by_fileset = {}  # type: Dict[str, object]
    for root in run.roots:
        name = getattr(root, "fileset", "")
        if name:
            by_fileset.setdefault(name, root)

    count = 0
    for name in stranded:
        root = by_fileset.get(name)
        if root is None:
            continue
        root.stranded = True
        root.add_note("you hold space in fileset %s and cannot list any path in it" % (name,))
        count += 1
    run.warnings.extend(
        [
            "holds space in %d fileset(s) with no reachable path: %s"
            % (len(stranded), ", ".join(sorted(set(stranded))[:6]))
        ]
        if stranded
        else []
    )
    return count


def sweep(opts, runner=None):
    # type: (argparse.Namespace, Optional[object]) -> Run
    """Run every probe once and return the collected facts."""
    run = Run()
    started = time.time()

    def mark(label):
        # type: (str) -> None
        run.timings.append((label, time.time() - started))

    warnings = []  # type: List[str]
    run.site = load_site(warn=warnings)
    run.warnings.extend(warnings)
    mark("config")

    run.mounts = read_mount_table()
    mark("mounts")

    budget = Budget(total_s=float(_merge_flag(opts, "timeout", DEFAULT_TIMEOUT_S)))
    if runner is None:
        replay = _merge_flag(opts, "replay", None)
        if replay:
            import json

            with open(str(replay), "r") as handle:
                runner = RecordedRunner.from_json(json.load(handle), strict=False)
        else:
            runner = SubprocessRunner(budget=budget)

    run.plugins = detect_plugins(runner, list(run.mounts))
    _apply_plugin_defaults(run.site, run.plugins)
    mark("plugins")

    run.identity = read_identity(run.mounts, site=run.site)

    for plugin in run.plugins:
        try:
            run.allocations.extend(plugin.allocations(runner, budget) or [])
        except Exception as exc:
            run.warnings.append("plugin %s could not list allocations: %s" % (plugin.name, exc))
    mark("allocations")

    # Quota FIRST, because its fileset enumeration is a discovery source.
    backends = default_backends(run.site)
    run.quota_attempts = read_all(backends, runner, run.mounts, budget, ["/"])
    held = []  # type: List[str]
    for snap in run.quota_attempts:
        try:
            held.extend(filesets_seen(snap))
        except Exception:
            continue
    mark("quota")

    run.roots = discover(
        runner,
        run.mounts,
        run.identity,
        budget,
        run.site,
        filesets=held,
        allocations=run.allocations,
        allow_write=bool(_merge_flag(opts, "probe_write", False)),
    )
    # The sweep completed, which is the only thing that can make `gone`
    # reachable in the diff. A run that crashed or ran out of budget before
    # here leaves this at NOT_PROBED and the diff refuses to claim a loss.
    if budget.exhausted:
        run.discovery = unknown(
            VerdictCategory.PROBE_TIMEOUT,
            "the run's time allowance ran out before discovery finished",
        )
    else:
        run.discovery = confirmed("discovery swept every candidate", source="discover")
    mark("discover")

    attribute_all(run.roots, runner, run.mounts, budget, run.site)
    _label_allocations(run)
    mark("attribute")

    _attach_quota(
        run,
        budget,
        runner,
        measure=not _merge_flag(opts, "no_measure", False),
        show_all=bool(_merge_flag(opts, "all", False)),
    )
    _mark_stranded(run)
    mark("quota-attach")

    if not _merge_flag(opts, "no_state", False):
        _record_state(run, opts)
        mark("state")

    # Real storage devices, not mount entries. Counting every line of
    # /proc/self/mounts put "28 devices" in the header of a node with six,
    # because it counted tmpfs, devtmpfs, cgroup and the rest.
    devices = len(
        {
            getattr(m, "device", "")
            for m in run.mounts
            if getattr(m, "device", "") and not getattr(m, "is_pseudo", False)
        }
    )
    baseline = run.baseline_at
    run.meta = render_fields.RunMeta(
        tool="dirscape",
        version=__version__,
        host=getattr(run.identity, "hostname", "") or None,
        node_class=getattr(run.identity, "node_class", "") or None,
        cluster=getattr(run.identity, "cluster", "") or None,
        user=getattr(run.identity, "user", "") or None,
        devices=devices or None,
        elapsed_s=time.time() - started,
        baseline_at=baseline,
        # `now` is supplied rather than read inside the renderer, because
        # `fields.age_phrase` refuses to invent it: given a date and no "now"
        # it returns the unknown mark, on the grounds that the caller has said
        # WHEN and not HOW LONG AGO. Passing it is what turns the header's
        # `baseline 2026-09-21 (?)` into `(3 minutes ago)`.
        now=time.time(),
    )
    return run


def _label_allocations(run):
    # type: (Run) -> None
    """Give an allocation-only root a role, inferred from its location name.

    These roots have no path by design, so the path-based role heuristic never
    fires and every one of them renders as `?` in the ROLE column, which makes
    six distinct allocations look identical. The location string is the only
    evidence available, so it is used to guess a LABEL and nothing else. The
    role is advisory throughout this tool and never decides access, which is
    what makes guessing it acceptable here when guessing a path was not.
    """
    for root in run.roots:
        if root.role or root.path:
            continue
        location = (root.policy or {}).get("allocation_location")
        if not location:
            continue
        # Passed through the ordinary heuristic as if it were a path, which is
        # why `cfs4/hpc-staff` comes out `archive` and `project3/hpc` comes out
        # `project`. A leading slash is added for the glob's benefit only and
        # is never stored.
        root.role = run.site.role_for("/" + str(location).lstrip("/"))


def _record_state(run, opts):
    # type: (Run, argparse.Namespace) -> None
    """Load the lineage, diff against it, then append and save.

    `from_roots` is called before `append`, which is load bearing: the
    snapshot takes `first_seen` from the lineage, so appending first would
    make every root's first sighting the current run and nothing would ever be
    new.
    """
    fingerprint = getattr(run.identity, "fingerprint", "") or ""
    try:
        run.lineage = Lineage.load(fingerprint=fingerprint)
    except Exception as exc:
        run.warnings.append("could not read the snapshot lineage: %s" % (exc,))
        run.lineage = Lineage()

    # The lineage records why it could not use a file it found. Nothing was
    # reading these, so a damaged or foreign baseline was discarded in silence.
    run.warnings.extend(getattr(run.lineage, "notes", []) or [])

    previous = run.lineage.latest()
    since = None
    raw_since = getattr(opts, "since", None)
    if raw_since:
        try:
            since = cutoff_for(parse_duration(str(raw_since)))
        except ValueError as exc:
            run.warnings.append("ignored --since %r: %s" % (raw_since, exc))
        else:
            older = run.lineage.baseline_for(since)
            if older is not None:
                previous = older

    run.snapshot = Snapshot.from_roots(
        run.roots,
        identity=run.identity,
        lineage=run.lineage,
        discovery=run.discovery,
        node_class=getattr(run.identity, "node_class", "") or node_class(),
        cluster_fingerprint=fingerprint,
    )
    run.changes = diff(previous, run.snapshot)
    run.warnings.extend(getattr(run.changes, "warnings", []) or [])
    # Remembered here, BEFORE the append. Afterwards the lineage's newest entry
    # is this run, so asking the lineage for a baseline returns the run being
    # recorded and the header reads `baseline ?` while a baseline plainly
    # exists.
    if previous is not None:
        run.baseline_at = getattr(previous, "taken_at", None)

    run.lineage.append(run.snapshot)
    try:
        saved = run.lineage.save(fingerprint=fingerprint)
    except Exception as exc:
        saved = False
        run.warnings.append("could not save the snapshot lineage: %s" % (exc,))
    if not saved:
        # `save` RETURNS False rather than raising when it cannot write, and
        # only the raise was handled. So on an unwritable state directory the
        # run reported "a baseline has been recorded" while nothing reached
        # the disk, and the next run said the same thing again, for ever.
        run.warnings.append(
            "the baseline was NOT saved to %s, so the next run will have "
            "nothing to compare against" % (run.lineage.path or "the state directory",)
        )
        run.saved = False


#: The order a reader's eye should travel: your own space first, shared data
#: after it, machinery last. Not alphabetical and not mount-table order, both
#: of which interleave `/gpfs/collie3/cap` with your home directory.
_ROLE_ORDER = (
    "home",
    "project",
    "scratch",
    "dataset",
    "software",
    "archive",
    "local",
    "other",
)


def _says_something(root):
    # type: (object) -> bool
    """Whether a row carries information a reader can act on.

    This is the filter that turned a 63-row default into a handful. Of those
    63 rows on one real account, **50 were `?` in every data column**: nine
    `/gpfs/<cluster>/<tier>` aliases, `/`, `/.nodelog/log`, `/programs`, and
    twenty `/project2/reference/*` collections whose quota belongs to their
    parent fileset. A row that says nothing is not evidence of anything, and
    fifty of them bury the six rows that matter.

    A row is kept when it answers one of the questions the tool exists for:

    * can I put data here?            (write access was confirmed)
    * how full is it?                 (a quota figure was measured)
    * did something change?           (a delta label, or stranded)
    * do I have space with no path?   (allocated elsewhere)

    Everything else is counted and reachable with `--all`, never discarded.
    """
    # Stranded and elsewhere rows are DELIBERATELY not kept here. Each gets a
    # one-line summary under the table with its own subcommand, and listing
    # them in the table as well put eleven rows of other people's directories
    # and pathless allocations above the four places this user can actually
    # write. A row that is already summarised does not earn a second showing.
    if getattr(root, "stranded", False) or getattr(root, "elsewhere", False):
        return False
    if getattr(root, "labels", None):
        return True
    writable = getattr(root, "writable", None)
    if writable is not None and writable.confirmed:
        return True
    for snap in (getattr(root, "quota", None), getattr(root, "inode_quota", None)):
        if snap is not None and snap.available and snap.rows:
            return True
    return False


def _nearest_ancestor(path, paths):
    # type: (str, set) -> str
    """The closest strict ancestor of ``path`` that is itself a root, or "".

    Walks up rather than testing `dirname` once, because the intermediate
    directories of a deep path are usually not roots themselves: nothing
    discovered `/project/hpc/jdoe42/.cache`, so its child could never find
    `/project/hpc`.
    """
    if not path or path == "/":
        return ""
    current = path.rstrip("/")
    while True:
        parent = os.path.dirname(current)
        if not parent or parent == current:
            return ""
        if parent in paths:
            return parent
        current = parent


def _full_access(root):
    # type: (object) -> bool
    """Whether this root is yours outright: listable and writable."""
    writable = getattr(root, "writable", None)
    return (
        getattr(root, "reach", None) == Reach.LISTABLE
        and writable is not None
        and writable.confirmed
    )


def _collapse_families(roots):
    # type: (List[object]) -> Tuple[List[object], int]
    """Keep the HIGHEST directory you have full access to, and stop there.

    The rule a reader actually wants, and the one that scales. If you have
    full access to `/project/xyz` then `/project/xyz` is the answer and its
    subdirectories are an implementation detail of your own filing. The table
    should say "this whole tree is yours" in one row rather than enumerate it.

    A descendant survives only when it says something its ancestor does not:

    * the ancestor is NOT fully accessible, so the subtree is not uniformly
      yours and the reachable parts are the real answer (a PI directory you
      can read but not write, with one writable subdirectory in it);
    * the descendant carries a delta or a stranded flag;
    * the descendant is on a different device or fileset, so it is a different
      piece of storage that merely happens to be mounted underneath.

    That last exception matters more than it looks: `/scratch` is a plain
    directory holding three clusters' filesystems, so a rule that folded on
    path alone would hide two of them behind the first.

    Three earlier rules each left `?` rows behind and are recorded so they are
    not retried: immediate-parent folding missed
    `/project/hpc/jdoe42/.cache/tmp`, whose intervening directory nothing
    discovered; ancestor folding missed `/project/hpc`, whose figure sits on a
    descendant; and keying on "who holds the fileset's figure" hid a
    perfectly good directory whenever the figure landed on a sibling.
    """
    covered = {}  # type: Dict[str, object]
    for root in roots:
        path = getattr(root, "path", "")
        if path and _full_access(root):
            covered[path.rstrip("/") or "/"] = root

    kept = []  # type: List[object]
    folded = {}  # type: Dict[str, int]
    for root in roots:
        path = getattr(root, "path", "")
        host = ""
        if path:
            # The OUTERMOST accessible ancestor, not the nearest. `_ancestors`
            # returns nearest first, so the loop keeps going rather than
            # breaking: a nearer ancestor may itself be folded away, and its
            # count would vanish with it. Measured on a four-level tree, the
            # nearest-match version reported 4 folded rows while the surviving
            # row's own count read 3.
            for ancestor in _ancestors(path):
                owner = covered.get(ancestor)
                if owner is None or owner is root:
                    continue
                same_storage = getattr(owner, "device", "") == getattr(
                    root, "device", ""
                ) and getattr(owner, "fileset", "") == getattr(root, "fileset", "")
                if same_storage:
                    host = ancestor
        speaks = getattr(root, "labels", None) or getattr(root, "stranded", False)
        if host and not speaks:
            folded[host] = folded.get(host, 0) + 1
            continue
        kept.append(root)

    for root in kept:
        count = folded.get(getattr(root, "path", "").rstrip("/") or "/")
        if count:
            root.policy = dict(root.policy or {})
            root.policy["contains"] = count
    return kept, sum(folded.values())


def _ancestors(path):
    # type: (str) -> List[str]
    """Every strict ancestor of a path, nearest first."""
    out = []  # type: List[str]
    current = path.rstrip("/")
    while True:
        parent = os.path.dirname(current)
        if not parent or parent == current:
            return out
        out.append(parent)
        current = parent


def _visible(run, show_all):
    # type: (Run, bool) -> Tuple[List[object], int]
    """The rows to render, and how many were held back.

    Secondary roots (filesystem roots, `/gpfs/*` aliases of filesystems
    already shown at their fileset junctions, pseudo mounts) are real, so they
    are counted and kept reachable rather than dropped.
    """
    if show_all:
        return list(run.roots), 0

    # Read from `policy`, which is where discovery puts it. This was
    # `getattr(r, "rank", RANK_PRIMARY)` against a `Root` that has no `rank`
    # attribute, so the default was returned for every root and the whole
    # rank filter did nothing: memory filesystems and the `/gpfs/*` plumbing
    # views were only kept out of the default view by the later
    # `_says_something` test, which is a different question and let a 94G
    # tmpfs through as somewhere to put data.
    primary = [r for r in run.roots if (r.policy or {}).get("rank", RANK_PRIMARY) == RANK_PRIMARY]
    kept, folded = _collapse_families(primary)
    speaking = [r for r in kept if _says_something(r)]

    if not speaking:
        # Never render an empty table when the only thing wrong was the
        # filter: a user with no measurable storage still needs to see what
        # was found.
        return kept, folded

    order = {name: i for i, name in enumerate(_ROLE_ORDER)}
    speaking.sort(key=lambda r: (order.get(getattr(r, "role", "") or "other", 99), r.path))
    hidden = len(run.roots) - len(speaking)
    return speaking, hidden


# --------------------------------------------------------------------------
# Views
# --------------------------------------------------------------------------

# The detail view's vocabulary lives in the next few helpers, and it is a
# rewrite rather than a tidy-up. The owner's words on the version it replaces:
# "do you actually know what the text here means? it's so confusing. what does
# mount mean here? everything needs to be accessible."
#
# The reader is a researcher deciding where their data can live, not a storage
# administrator, so the screen answers their questions in their order: how much
# room is there and how full is it, can I read and write here, is it kept or
# deleted, where does the figure come from (so a `du` that disagrees makes
# sense), and why is dirscape showing me this directory at all. Everything
# else moved to `--json`, which every view now names at the foot.
#
# Two mechanical rules came out of the same rewrite:
#
# * **Quiet on a good answer, loud on a bad one.** `✓ present ok` said nothing
#   twice, once in a label taken from the model and once in the word `ok`, so a
#   confirmed probe with nothing to add now prints no line at all.
# * **Every line fits the window.** `interactive.select` repaints by moving the
#   cursor up by the number of lines it WROTE, so one line the terminal wraps
#   makes the block a row taller than the count. See `_fit`.


def _room(style, size=None):
    # type: (object, Optional[int]) -> int
    """Columns this block has to fit inside.

    The floor is 20 rather than `MIN_WIDTH`: clamping UP to 32 in a 24 column
    terminal would hand back a width wider than the window, which is the exact
    mismatch the wrapping is there to prevent.
    """
    if size:
        return max(20, int(size))
    return max(20, int(getattr(style, "size", 0) or render_style.FALLBACK_WIDTH))


def _prose(style, room, text, indent="  "):
    # type: (object, int, str, str) -> List[str]
    """Wrapped prose, dimmed one line at a time.

    Tinted per line rather than once over the paragraph. `style.dim` wraps the
    whole string in a single escape pair, so wrapping afterwards leaves the
    opening escape on the first line and the reset on the last, and every line
    between them comes out undimmed.
    """
    wrapped = render_style.wrap(text, indent=indent, size=room, style=style)
    return [style.dim(line) for line in wrapped.splitlines()]


def _field(style, room, label, value):
    # type: (object, int, str, str) -> List[str]
    """A labelled line, folding UNDER its own label rather than off the edge.

    The value is returned UNTOUCHED whenever it fits, because the space and
    files figures arrive pre-formatted from `render.fields`, which lines a
    column up with runs of spaces and pads inside the percentage, and `wrap`
    normalises whitespace. Folding only happens where the alternative is a
    line the terminal breaks for us in the wrong place.
    """
    head = "  %-9s " % (label,)
    if render_style.width(head + value) <= room:
        return [head + value]
    lead = render_style.width(head)
    pieces = render_style.wrap(value, indent="", size=room - lead, style=style).splitlines()
    if not pieces:
        return [head.rstrip()]
    return [head + pieces[0]] + [" " * lead + piece for piece in pieces[1:]]


def _bullet(style, room, glyph, sentence):
    # type: (object, int, str, str) -> List[str]
    """A glyph and a sentence, with the sentence hanging under itself."""
    head = "  %s " % (glyph,)
    lead = render_style.width(head)
    pieces = render_style.wrap(sentence, indent="", size=room - lead, style=style).splitlines()
    if not pieces:
        return []
    return [head + style.dim(pieces[0])] + [" " * lead + style.dim(piece) for piece in pieces[1:]]


def _kind_phrase(root):
    # type: (object) -> str
    """What sort of place this is, in one clause.

    Replaces `project  gpfs  meadow3_cap:project-hpc`, whose third field was
    `device:fileset` and was explained nowhere on the screen. Both halves of it
    now appear once, inside the sentence about where the space figure comes
    from, which is the only question they answer for a reader.
    """
    role = (getattr(root, "role", "") or "").strip()
    fstype = (getattr(root, "fstype", "") or "").strip()
    if role and fstype:
        return "%s directory, %s" % (role, fstype)
    if role:
        return "%s directory" % (role,)
    if fstype:
        return "%s filesystem" % (fstype,)
    return ""


#: Reach, as something a reader can act on. `Reach.label` answers the model's
#: question ("listable") and this answers the reader's ("can I read it"), which
#: is the same split `category_label` makes for a verdict.
#:
#: These used to be sentences, and `TRAVERSE` was a twenty-word one. A field
#: whose label is `access` does not need its value to restate the question, so
#: each is now the shortest phrase that is still unambiguous. `traverse only`
#: is the one that trades a little clarity for brevity and it earns it: a
#: reader who does not know the word has a directory they can `cd` into and
#: cannot `ls`, which no short phrase conveys anyway, and `--json` carries the
#: full reason.
_REACH_WORDS = {
    Reach.LISTABLE: "read",
    Reach.TRAVERSE: "traverse only, no listing",
    Reach.CLOSED: "no access",
    Reach.UNKNOWN: "unknown",
}


def _access_phrase(root):
    # type: (object) -> str
    """What you can do here, in the SAME words the table's column prints.

    It used to have its own vocabulary, which is how the two views came to
    disagree about the one thing both are for: the table said `rwx` and this
    said "you can see what is in this directory, and you can write to it",
    fourteen words for two bits of information in a field whose label already
    asks the question. Both are now `render.fields.access_words`, so a reader
    who opens a row sees the phrase they were just looking at.

    The write verdict's REASON is still not printed here. It is a paragraph of
    administration detail ("os.access reports write; not owner-confirmed, and
    W_OK can be wrong under a root-squashed export, so pass --probe-write to
    settle it by writing"), it was the line that broke the interactive repaint
    at 157 characters, and it is reachable through the `--probe-write` and
    `--json` pointers at the foot of the view.
    """
    text = render_fields.access_words(root)
    if text != render_fields.UNKNOWN:
        return text
    write = getattr(root, "writable", None)
    if write is not None and not write.durable and write.category != VerdictCategory.NOT_PROBED:
        # Nothing was settled, and the reason why is the useful part.
        return "unknown (%s)" % (write.label,)
    return text


def _keeping_phrase(root, site):
    # type: (object, object) -> str
    """Backed up, deleted on a schedule, or simply unpublished. In words.

    Silence means UNPUBLISHED and never "no". A site that says nothing about
    backups has not told us there are none, and of every field on this screen
    this is the one a reader is most likely to act on by leaving data
    somewhere, so the unknown says so in words.
    """
    policy = render_fields.merged_policy(root, site)
    parts = []  # type: List[str]

    if "backup" in policy:
        value = policy.get("backup")
        if isinstance(value, bool):
            parts.append("backed up" if value else "not backed up")
        else:
            parts.append("backups: %s" % (render_fields.safe(value, limit=32) or "?",))

    if "purge_days" in policy:
        days = policy.get("purge_days")
        # `isinstance(True, int)` is True, so the bool test comes first or a
        # site writing `purge_days=yes` prints "deleted after 1 days".
        if isinstance(days, bool) or days is None:
            parts.append("a deletion rule dirscape could not read")
        elif isinstance(days, (int, float)) and days > 0:
            parts.append("deleted %d days after writing" % (int(days),))
        elif isinstance(days, (int, float)):
            parts.append("no scheduled deletion")
        else:
            parts.append("deleted: %s" % (render_fields.safe(days, limit=32) or "?",))
    elif "purge" in policy:
        parts.append("deleted: %s" % (render_fields.safe(policy.get("purge"), limit=32) or "?",))

    if policy.get("readonly"):
        parts.append("published read-only")

    parts = [part for part in parts if part]
    if not parts:
        # Leads with the STATE and not with a reassurance. "not published" is
        # what is true; "nothing is deleted here" is what a reader would infer
        # from silence, and it is the inference that loses data. The colon and
        # the ten words that followed it restated the label, so they went.
        return "not published"
    return "; ".join(parts)


def _findings(root, style, axes=("mounted", "present", "allocated")):
    # type: (object, object, Sequence[str]) -> List[Tuple[str, str]]
    """One (glyph, sentence) per axis with something to say.

    "mounted" is gone from the wording. It is the word the owner picked out as
    meaningless here, and what it means to a reader is whether the storage is
    attached to the machine they are typing on: on this cluster `/cfs3` is
    there from a login node and absent from a compute one, which is the whole
    reason the axis exists.

    A confirmed `present` prints nothing. The figures and the access line above
    it cannot be there for a directory that is not, so the line only ever said
    `ok` about something already visible.

    **A confirmed `mounted` prints nothing either, for the same reason and one
    more.** Reason one: a row that just printed `0B / 400G` and `you can write
    to it` has already demonstrated the storage is attached, so a sentence
    asserting it is a restatement. Reason two: it was the last unexplained
    mark left in this view. It rendered as a bare `✓` opening a sentence, and
    a reader who has to ask what a mark means has been handed a puzzle instead
    of an answer, which is the standing rule that removed the rest of them.
    The axis still speaks when it is refuted or in doubt, which is the case
    worth a sentence: `/cfs3` from a compute node.
    """
    g = style.g
    out = []  # type: List[Tuple[str, str]]
    for axis in axes:
        verdict = getattr(root, axis, None)
        if verdict is None or verdict.category == VerdictCategory.NOT_PROBED:
            continue
        glyph = render_fields.verdict_glyph(verdict, g)
        if axis == "mounted":
            if verdict.confirmed:
                continue
            if verdict.refuted:
                # Two endings, because only one of them is always true. A row
                # with no path here has nothing to measure, and saying so is
                # the whole answer for an allocation; a row that HAS a path
                # may still be carrying a figure from the quota layer, and
                # "nothing could be measured" would contradict the number
                # printed four lines above it.
                if getattr(root, "path", ""):
                    ending = "so you cannot use this path from where you are standing"
                else:
                    ending = "and there is no path for it here to measure"
                out.append(
                    (
                        style.bad(glyph),
                        "This storage is not attached to the machine you are on, %s. That is a "
                        "fact about this machine and not about the storage." % (ending,),
                    )
                )
            else:
                out.append(
                    (
                        glyph,
                        "dirscape could not tell whether this storage is attached to the "
                        "machine you are on (%s)." % (verdict.label,),
                    )
                )
        elif axis == "present":
            if verdict.confirmed:
                continue
            if verdict.refuted:
                out.append(
                    (style.bad(glyph), "The directory is not there (%s)." % (verdict.label,))
                )
            else:
                out.append(
                    (
                        glyph,
                        "dirscape could not check that the directory is there (%s)."
                        % (verdict.label,),
                    )
                )
        elif axis == "allocated":
            if verdict.confirmed:
                out.append((style.ok(glyph), "An allocation record lists this space as yours."))
            elif verdict.refuted:
                out.append(
                    (
                        style.bad(glyph),
                        "No allocation record names this space as yours (%s)." % (verdict.label,),
                    )
                )
            else:
                out.append(
                    (
                        glyph,
                        "dirscape could not read the allocation records (%s)." % (verdict.label,),
                    )
                )
    return out


def _scope_phrase(row):
    # type: (object) -> str
    """Whose usage a quota row counts. The distinction a reader acts on.

    A figure that counts the whole group's usage and one that counts only the
    caller's are different answers to "how much room do I have left", and the
    scope is the only field that says which was measured.
    """
    return {
        # `user` returns nothing on purpose. It is the assumption a reader
        # already brings to a quota figure, so saying it adds a clause to
        # every row and distinguishes nothing; the other three CONTRADICT that
        # assumption, which is the only reason this field exists.
        "user": "",
        "group": "your group's usage",
        "fileset": "everyone's files, not only yours",
        "project": "everyone's files, not only yours",
    }.get(getattr(row, "scope", "") or "", "")


def _quota_source(root):
    # type: (object) -> str
    """Which quota produced the figures, as a value and not a paragraph.

    This was three sentences of mechanism:

        The figures above are your own usage under the quota named project-hpc
        on the filesystem meadow3_cap, counted by the filesystem itself rather
        than by walking this directory, so du can report a different number.
        The filesystem did not say which directory this quota covers, so
        dirscape matched the two by name.

    The owner's verdict was blunt and correct: "this chunk of verbose text
    makes no fucking sense. it says the figures above. what figures?" Prose
    that points at other lines on the screen ("the figures above") only works
    if the reader is reading top to bottom, and nobody reads a detail view
    that way. As a labelled field it points at nothing and needs no anchor:

        quota     project-hpc on meadow3_cap, matched by name

    The mechanism sentence went entirely. "Counted by the filesystem rather
    than by walking the directory" is implied by naming a quota as the source,
    it is stated once in the README where a reader meets the tool, and it was
    costing two lines on every single path.
    """
    if (root.policy or {}).get("walked"):
        # A walked figure names the walk. Without this it read `source /tmp`,
        # because `_measure` keys its synthetic row on the path and this
        # function's job is to name a fileset and a device: it would have
        # presented a `du`-style sum as though a quota backend had published
        # it, which is the one thing this field exists to prevent.
        return "counted by walking this directory, because no quota exists here"
    row, how, _why = render_fields.pick_row(getattr(root, "quota", None), root.path, "blocks")
    if row is None:
        free = (root.policy or {}).get("free_bytes")
        if isinstance(free, int) and free >= 0:
            # `free` is its own field now, so this says whose figure it is.
            return "none; free is the whole filesystem, shared"
        return "none measured"

    fileset = getattr(row, "fileset", "") or ""
    device = getattr(row, "device", "") or ""
    if fileset and device and fileset != device:
        where = "%s on %s" % (fileset, device)
    elif fileset or device:
        # One name, and it is the name of a QUOTA. Calling it the filesystem
        # would be a claim about which of the two the backend answered for,
        # and `QuotaRow` keeps them apart precisely because one device here is
        # mounted at four places with four different quotas.
        where = fileset or device
    else:
        where = "reported for this path"

    # Whose usage this counts, but only where it is not the reader's own. A
    # fileset-scoped figure is the whole group's data and a reader who takes
    # it for their own has the wrong number by however much everybody else
    # stored, which is the one thing in this field worth a clause.
    scope = _scope_phrase(row)
    if scope:
        where += ", " + scope
    if how == "inferred":
        where += ", attributed by dirscape"
    elif getattr(row, "guessed", False):
        where += ", matched by name"
    return where


def _uncounted(root):
    # type: (object) -> str
    """Space handed out and not yet charged, as a figure.

    `blockInDoubt` / `filesInDoubt`: measured on this home fileset at 2.4 GB
    against 858 MB used, nearly three times the figure it qualifies, so it is
    the difference a reader cross-checking with `du` actually sees. It is the
    one number worth keeping out of the paragraph that used to explain it.

    Conditioned on the FACT, never on a glyph being on screen. The earlier
    version was `if g.doubt in figures`, which tied the explanation to the
    mark appearing in the rendered figure; the mark was later removed from the
    table and the explanation silently left with it, so a fileset with 2.4G
    handed out reported that nowhere at all.
    """
    held = []  # type: List[str]
    blocks = render_fields.in_doubt_of(root)
    if blocks:
        held.append(render_fields.human_bytes(blocks))
    files_row, _, _ = render_fields.pick_row(getattr(root, "inode_quota", None), root.path, "files")
    if files_row is not None and files_row.in_doubt:
        held.append("%s files" % (render_fields.human_count(files_row.in_doubt),))
    return ", ".join(held)


def _because_phrase(root):
    # type: (object) -> str
    """Why this directory is on the reader's screen at all.

    `found by group-template, dir-owner, quota-fileset` was the worst line on
    the old screen: three internal constants from `discover.candidates`, which
    is a wire vocabulary shown to a human. `model.py` already owns the fix for
    that mistake (`category_label`, after nodetop's NT-5), so the sources got
    the same treatment and `discover.source_label` holds the wording.

    It was then a sentence, and the sentence was 25 words for a three item
    list: "dirscape shows you this directory because its name matches your
    user name or one of your groups, a group you belong to owns it, and the
    filesystem's own records say you hold space in it." It is a `found` field
    now, and `source_label(short=True)` supplies the noun phrases.
    """
    clauses = [source_label(name, short=True) for name in getattr(root, "sources", ()) or ()]
    return ", ".join([clause for clause in clauses if clause])


def _why_allocation(root, style, size=None):
    # type: (object, object, Optional[int]) -> str
    """Explain a root that the allocation database names and this node lacks.

    Separate from `_why` because every probe it would print is inapplicable:
    with no path there is nothing to stat, so no reach, no fileset and no
    quota. What is known is the account, the size and the fact that the
    absence is about this node rather than about the storage.
    """
    policy = root.policy or {}
    location = str(policy.get("allocation_location") or "?")
    room = _room(style, size)
    out = [
        style.head(location),
        style.dim("  an allocation, not a directory you can use from this machine"),
        "",
    ]

    gb = policy.get("allocation_gb")
    if isinstance(gb, (int, float)) and gb > 0:
        size_text = render_fields.human_bytes(int(gb * 1000 * 1000 * 1000))
        out.extend(_field(style, room, "size", size_text))
    accounts = policy.get("allocation_accounts")
    if isinstance(accounts, (list, tuple)) and accounts:
        out.extend(_field(style, room, "account", ", ".join(str(a) for a in accounts)))
    if root.role:
        out.extend(_field(style, room, "kind", "%s storage" % (root.role,)))
    out.append("")

    # Allocation first, then the machine. In that order the two lines read as
    # one statement: the space is yours, and the reason you cannot see it is
    # where you are standing. `present` is left out entirely, because "the
    # directory is not there" about a row that has no path is a restatement
    # dressed up as a second finding.
    for glyph, sentence in _findings(root, style, axes=("allocated", "mounted")):
        out.extend(_bullet(style, room, glyph, sentence))
    return "\n".join(out)


def _common_dir(paths):
    # type: (Sequence[str]) -> str
    """The deepest directory every path is inside, by COMPONENT not by string.

    `os.path.commonprefix` is a character operation and it showed: ten
    symlinks into `/project/hpc/jdoe42/.cache`, `.../.conda` and friends share
    the characters `/project/hpc/jdoe42/.`, so the view named a directory with
    a trailing dot that does not exist. Splitting on the separator first is
    the whole fix.
    """
    if not paths:
        return ""
    parts = [p.strip("/").split("/") for p in paths]
    shared = []  # type: List[str]
    for pieces in zip(*parts):
        if len(set(pieces)) != 1:
            break
        shared.append(pieces[0])
    return "/" + "/".join(shared) if shared else sorted(paths)[0]


def _why(run, path, style, size=None, verbose=False):
    # type: (Run, str, object, Optional[int], bool) -> Tuple[str, int]
    """One path, explained in a screen you can read.

    The first version printed everything it knew: raw byte counts, every
    backend note verbatim, and one line per symlink out of the directory. On a
    home directory with eleven relocated dotfiles that was thirty-odd lines,
    ten of them the same sentence with a different path in it, and the two
    numbers a reader actually wanted were in the middle in bytes.

    So: the headline first, the verdicts as a block, and anything repetitive
    collapsed to a count with a command that expands it. `--json` still
    carries every field for anyone who wants the lot.

    The second version printed the right facts in the tool's own vocabulary:
    `device:fileset` with no explanation, `mounted`, `found by dir-owner`. The
    helpers above hold the rewrite; `size` is here so the caller that has to
    know the block's exact height (`_browse`) can fix the width it wraps at.
    """
    target = os.path.abspath(os.path.expanduser(path))
    # Sanitised for DISPLAY only, and matched on the raw value. A path from
    # argv is foreign text: `Root.path` is cleaned at construction but this
    # string never was, so a directory whose name contains a newline forged a
    # table row in this very view and an ESC sequence reached the terminal.
    # That is rapiDU's RD-6 arriving through the one string the model does not
    # own. Measured with a directory literally named
    # "evil\n/project/FORGED  999T  100%\x1b[31m".
    shown = sanitize(target, limit=4096)
    match = None
    for root in run.roots:
        if getattr(root, "path", "") == target:
            match = root
            break

    if match is None:
        # An allocation LOCATION, which is what `dirscape elsewhere` prints and
        # what a reader will therefore paste back in. It is not a path, so the
        # loop above cannot find it and `os.path.abspath` had already turned
        # `cfs4/hpc-staff` into `$PWD/cfs4/hpc-staff` and reported that as
        # missing. Matched on the raw argument, before that mangling.
        typed = (path or "").strip().strip("/")
        if typed:
            for root in run.roots:
                location = str((root.policy or {}).get("allocation_location") or "")
                if location and location.strip("/") == typed:
                    return _why_allocation(root, style, size), EXIT_OK
    if match is None:
        best = ""
        for root in run.roots:
            candidate = getattr(root, "path", "")
            if candidate and target.startswith(candidate.rstrip("/") + "/"):
                if len(candidate) > len(best):
                    best, match = candidate, root

    if match is not None and match.path != target and not os.path.exists(target):
        # Only when we FELL BACK to an enclosing root. The fallback is right
        # for a path inside a root and wrong for one that is not there at all:
        # `dirscape why /nope/nope` walked up, found `/`, and printed a
        # confident explanation of `/` with exit 0, so a reader asked about one
        # path and was answered about another.
        #
        # An exact root match is never second-guessed here. Discovery already
        # settled whether it is present, and re-checking the filesystem made
        # the tool contradict its own probe and emit "X does not exist. The
        # enclosing root is X", naming the same path twice.
        return (
            "%s does not exist.\n\n"
            "The enclosing root is %s, if that is what you meant." % (shown, match.path or "?"),
            EXIT_PATH,
        )

    if match is None:
        if not os.path.exists(target):
            return ("%s does not exist." % (shown,), EXIT_PATH)
        return (
            "dirscape found no root at or above %s.\n\n"
            "That is a statement about discovery and not about the path: it\n"
            "exists and may be perfectly readable. Try `dirscape --all`." % (shown,),
            EXIT_PATH,
        )

    room = _room(style, size)
    out = []  # type: List[str]
    out.append(style.head(match.path))
    if match.path != target:
        out.extend(
            _prose(style, room, "the nearest directory dirscape knows about, above %s" % (shown,))
        )
    kind = _kind_phrase(match)
    if kind:
        out.append(style.dim("  " + kind))
    out.append("")

    # 1. How much room, and how full. The question that brought the reader
    #    here, so it is the first thing on the screen.
    #
    #    The SAME four fields the table's columns carry, named the same way.
    #    This used to print one composed `space` cell, so the detail view and
    #    the table disagreed about how to say the one thing both are for, and
    #    a reader who opened a row saw the figures reshuffled.
    figure, _caveat = render_fields.used_cell(match, style)
    out.extend(_field(style, room, "used", figure))
    # `quota`, matching the table and the word the site's own tool prints over
    # the same figure. This said `limit` while the column said `quota`, which
    # is the drift that had the two views naming one fact two ways.
    out.extend(_field(style, room, "quota", render_fields.limit_cell(match, style)))
    out.extend(_field(style, room, "free", render_fields.free_cell(match, style)))
    inodes = render_fields.file_count_cell(match, style)
    if inodes and inodes != render_fields.UNKNOWN:
        out.extend(_field(style, room, "files", inodes))
        # **Files have a quota too, and it is not always the one that bites
        # last.** Owner: "does file usually have limit?" On this cluster, yes:
        # 300,000 on a home directory against 30G of space, and a home full of
        # small files hits the inode ceiling long before the byte one. The
        # table has room for the count only; the ceiling belongs here.
        ceiling = render_fields.inode_limit_cell(match, style)
        if ceiling and ceiling != render_fields.UNKNOWN:
            # Nine characters, which is what `_field` pads to. `file quota`
            # is ten and pushed its value one column right of every value
            # above it, which is the alignment complaint this view has already
            # been through twice.
            out.extend(_field(style, room, "max files", ceiling))

    # 2. What the reader can do here, and 3. whether it is kept. Both are one
    #    sentence in a labelled row, because the label is a word a researcher
    #    would use and it is doing work.
    out.extend(_field(style, room, "access", _access_phrase(match)))
    out.extend(_field(style, room, "backups", _keeping_phrase(match, run.site)))

    # Why `used` is a question mark, in the one case where it always is.
    #
    # The owner, looking at two scratch rows reading `?` for used and files:
    # "why is this place having so many '?'? what does it mean? you can't even
    # get the numbers? or what?" The answer is literally yes, we cannot: these
    # are mounted with no quota system, so nothing is accounting for per-user
    # usage and the only way to find out is to walk the directory, which this
    # tool does not do at any price. `?` is therefore correct and must not
    # soften, but a reader should not have to guess that from the mark. One
    # line, and only on the rows that have it.
    row, _how, _reason = render_fields.pick_row(getattr(match, "quota", None), match.path, "blocks")
    if row is None and (match.policy or {}).get("free_bytes") is not None:
        out.extend(
            _field(
                style,
                room,
                "note",
                "no quota is enforced here, so nothing is counting your usage, and "
                "this directory was too large to add up quickly.",
            )
        )

    # 3. The one piece of provenance that is a fact about the STORAGE rather
    #    than about dirscape, so it stays in the default view.
    #
    #    A home directory whose dotfiles are symlinks into `/project` holds
    #    almost nothing while `du ~` reports gigabytes, and the space is
    #    charged to the project quota. That surprises people badly enough to
    #    be worth a line. The line itself was unreadable:
    #
    #      symlinks  10 into /project/hpc/jdoe42/.cache,
    #                /project/hpc/jdoe42/.cache/R-library, ..., billed there
    #                (dirscape tree)
    #
    #    Two full paths, an ellipsis, a passive verb and a command, for what
    #    is one sentence: ten folders here are stored somewhere else. It names
    #    the DIRECTORY the space lands in, once, and leaves the inventory to
    #    `dirscape tree`.
    crossings = [n for n in match.notes if "resolves to" in n]
    if crossings:
        targets = sorted({n.split("resolves to")[1].split(",")[0].strip() for n in crossings})
        home = _common_dir(targets)
        out.extend(
            _field(
                style,
                room,
                "note",
                "%d folder%s here are really stored in %s, and count against its space"
                % (len(crossings), "" if len(crossings) == 1 else "s", home),
            )
        )
    if match.labels:
        out.extend(_field(style, room, "changed", ", ".join(match.labels)))

    # 4. Everything that is about HOW DIRSCAPE KNOWS, behind `--verbose`.
    #
    #    Owner, reading `quota home on meadow3_cap, matched by name`,
    #    `uncounted 2.4G, 1.5k files` and `found an environment variable`:
    #    "do you think these things users can understand what they are? it
    #    makes no fucking sense."
    #
    #    They are all correct and none of them answers a question a researcher
    #    arrived with. `home on meadow3_cap` is a fileset name and a device
    #    name, `matched by name` is an attribution method, `uncounted` is a
    #    GPFS internal (`blockInDoubt`), and `found` explains dirscape's own
    #    discovery. They are what a support ticket needs, so they are one flag
    #    away and every one of them is in `--json` unconditionally.
    if verbose:
        out.append("")
        # Labels no longer than the others. `_field` pads to nine columns, so
        # `measured by` and `not yet counted` pushed their own values one and
        # six columns right of every value above them, which is the alignment
        # complaint this view has already been through once.
        out.extend(_field(style, room, "source", _quota_source(match)))
        uncounted = _uncounted(match)
        if uncounted:
            out.extend(_field(style, room, "in doubt", "%s, so du can disagree" % (uncounted,)))
        found = _because_phrase(match)
        if found:
            out.extend(_field(style, room, "found by", found))
        if crossings:
            out.extend(_field(style, room, "symlinks", ", ".join(sorted(set(targets))[:4])))

    # 4. The axes with something to say. Nothing prints for a probe that was
    #    never run, or for a confirmed `present`.
    findings = _findings(match, style)
    if findings:
        out.append("")
        for glyph, sentence in findings:
            out.extend(_bullet(style, room, glyph, sentence))

    # 5. Anything the backends said that is not already a field above. Two at
    #    most, and every one dropped is behind the `--json` pointer. These are
    #    the tool's own words about an unusual site (`mounted noquota, so no
    #    project scope exists here`), which is the same register as the block
    #    above it, so they went the same way.
    others = [
        n for n in match.notes if "resolves to" not in n and not restates_source(n, match.sources)
    ]
    if verbose and others:
        out.append("")
        for note in others[:2]:
            out.extend(_prose(style, room, note))
        if len(others) > 2:
            out.extend(
                _prose(style, room, "%d more notes are in dirscape --json." % (len(others) - 2,))
            )

    # 8. One escape hatch, one line. Every caveat this screen dropped is
    #    behind it, which is the trade the rewrite makes: the administration
    #    detail off the screen and one line saying where it went.
    out.append("")
    hatch = style.dim("dirscape why %s --json" % (match.path or shown,))
    if not verbose:
        hatch += style.dim("   -v for where these figures came from")
    elif match.writable.confirmed and match.writable.source == "os.access":
        hatch += style.dim("   --probe-write to test writing for real")
    out.append("  " + hatch)

    return "\n".join(out), EXIT_OK


def _elsewhere(run):
    # type: (Run) -> str
    """Allocations the database names and this node has no path for.

    Purpose-built rather than run through the atlas, because every column the
    atlas would draw is the unknown mark: with no path there is nothing to
    stat, so no reach, no quota and no fileset. What IS known is the account
    and the size the allocation database published, and a short list of those
    is a view where a table of six question marks was not.
    """
    rows = [r for r in run.roots if r.elsewhere]
    if not rows:
        return "Every allocation has a path on this node."

    entries = []  # type: List[Tuple[str, str, str]]
    for root in rows:
        policy = root.policy or {}
        location = str(policy.get("allocation_location") or "?")
        gb = policy.get("allocation_gb")
        size = "?"
        if isinstance(gb, (int, float)) and gb > 0:
            # The database publishes GB in decimal, so it is converted in
            # decimal. Treating it as binary would overstate a 256000 GB
            # allocation by about 10%.
            size = render_fields.human_bytes(int(gb * 1000 * 1000 * 1000))
        entries.append((root.role or "?", location, size))

    room = max(len(e[0]) for e in entries)
    span = max(len(e[1]) for e in entries)
    out = [
        "  %d allocation%s with no path on this node"
        % (len(entries), "" if len(entries) == 1 else "s"),
        "",
    ]
    for role, location, size in entries:
        out.append("  %s  %s  %s" % (role.ljust(room), location.ljust(span), size.rjust(6)))
    out.append("")
    out.append("  Named by the allocation database. Nothing here could be measured,")
    out.append("  because no path for it exists on this node, which is a fact about")
    out.append("  where you are standing and not about the storage.")
    return "\n".join(out)


def _render(run, opts, command, style, width):
    # type: (Run, argparse.Namespace, str, object, Optional[int]) -> Tuple[str, int]
    show_all = bool(_merge_flag(opts, "all", False))
    roots, hidden = _visible(run, show_all)
    changes = list(run.changes or [])
    legend_on = bool(_merge_flag(opts, "legend", False))
    summary_on = bool(_merge_flag(opts, "summary", False))

    if _merge_flag(opts, "json", False):
        caveats = list(run.warnings)
        # A command that FILTERS roots must filter them here too. `--json`
        # used to ignore the verb entirely, so `dirscape stranded --json`
        # emitted every root and a script asking for stranded storage had to
        # re-implement the filter. `matrix`, `tree` and `map` are presentation
        # variants of the same set and are left alone.
        subject = roots
        if command == "stranded":
            subject = [r for r in run.roots if r.stranded]
        elif command == "elsewhere":
            subject = [r for r in run.roots if r.elsewhere]
        elif command == "new":
            changed = {c.path for c in changes if getattr(c, "path", "")}
            subject = [r for r in run.roots if r.path and r.path in changed]
        return (
            render_json(subject, meta=run.meta, changes=changes, caveats=caveats),
            EXIT_OK,
        )

    if command == "why":
        return _why(run, opts.path, style, verbose=bool(_merge_flag(opts, "verbose", False)))

    if command == "ncdu":
        target = os.path.abspath(os.path.expanduser(opts.path))
        for root in run.roots:
            if getattr(root, "path", "") == target:
                return render_ncdu(root), EXIT_OK
        return (
            "dirscape has no root at %s, so there is nothing to export.\n"
            "Run `dirscape` to see the roots it found." % (sanitize(target, limit=4096),),
            EXIT_PATH,
        )

    if command == "elsewhere":
        return _elsewhere(run), EXIT_OK

    if command == "stranded":
        wanted = [r for r in run.roots if r.stranded]
        if not wanted:
            return (
                "Nothing stranded: every fileset you hold space in is reachable.",
                EXIT_OK,
            )
        return (
            render_atlas(
                wanted,
                meta=run.meta,
                site=run.site,
                style=style,
                size=width,
                legend_on=legend_on,
                summary=summary_on,
                # No changes and an empty census on purpose: a detail view must
                # not reprint the summary line that sent the reader to it.
                # Three alert lines under the very list they point at is the
                # table telling you to go look at itself.
                all_roots=[],
                group=True,
            ),
            EXIT_OK,
        )

    if command == "matrix":
        return render_matrix(roots, site=run.site, style=style, size=width), EXIT_OK
    if command == "tree":
        return render_tree(roots, style=style, size=width), EXIT_OK
    if command == "map":
        return render_treemap(roots, style=style, size=width), EXIT_OK
    if command == "snapshot":
        if run.snapshot is None:
            # It reported "Recorded 0 root(s) as a baseline" here, which is a
            # claim to have done the one thing `--no-state` exists to prevent.
            return (
                "Nothing recorded: state tracking is off (--no-state), so "
                "there is no baseline to compare against later.",
                EXIT_USAGE,
            )
        count = len(run.snapshot.records)
        if not run.saved:
            return (
                "Could not record a baseline: %s"
                % ("; ".join(run.warnings[-1:]) or "the state file is not writable",),
                EXIT_USAGE,
            )
        return (
            "Recorded %d root(s) as a baseline. Run `dirscape new` after the "
            "next change." % (count,),
            EXIT_OK,
        )
    if command == "new":
        if run.changes is None:
            return (
                "Nothing to compare: state tracking is off (--no-state).",
                EXIT_OK,
            )
        if getattr(run.changes, "no_baseline", False):
            if not run.saved:
                return (
                    "No baseline yet, and this run could not save one: %s"
                    % ("; ".join(run.warnings[-1:]) or "the state file is not writable",),
                    EXIT_USAGE,
                )
            return (
                "No baseline yet, so nothing can honestly be called new. "
                "A baseline of %d root(s) has been recorded; run this again "
                "after something changes." % (len(roots),),
                EXIT_OK,
            )
        # `stranded` is a STANDING condition, not a change: the state layer
        # emits it on every run because the space is still unreachable, so
        # including it here made `dirscape new` show the same five rows of
        # other people's filesets for ever, under a heading that said "1
        # change since the baseline". The table and its own count disagreed.
        # It keeps its alert line and its own subcommand.
        # Warnings are appended to the short-message paths below as well.
        # Without that, `dirscape new --since bogus` answered "No change since
        # the last run" and never mentioned that the flag had been ignored, so
        # a typo silently changed which baseline was compared against.
        alerts = _surfaceable(run)
        note = (
            ("\n\n" + "\n".join("  %s %s" % (chr(0x25B2), w) for w in alerts[:2])) if alerts else ""
        )

        moved = [c for c in changes if getattr(c, "label", "") != "stranded"]
        if not moved:
            standing = len(changes) - len(moved)
            if standing:
                return (
                    "No change since the last run. %d fileset%s still hold "
                    "space you cannot reach: dirscape stranded%s"
                    % (standing, "" if standing == 1 else "s", note),
                    EXIT_OK,
                )
            return ("No change since the last run." + note, EXIT_OK)
        # The atlas renders the delta panel, so it is reused rather than
        # reimplemented. It is handed the roots the changes REFER TO, not an
        # empty list: with no rows the atlas has nothing to hang the panel on
        # and prints "no roots were handed to this view", which is what an
        # earlier version of this branch did. Showing the changed rows is also
        # simply better, since a label without its quota and reach is half an
        # answer.
        changed_paths = {c.path for c in moved if getattr(c, "path", "")}
        subset = [r for r in run.roots if r.path and r.path in changed_paths]
        changes = moved
        if not subset:
            # Every change is on a root with no path (an allocation), so there
            # is no row to show. `subset or roots` fell back to the entire
            # atlas here, which answered "what changed?" with "here is
            # everything", and dropped the grouping and the census with it.
            return (
                render_atlas(
                    [],
                    meta=run.meta,
                    changes=changes,
                    site=run.site,
                    style=style,
                    size=width,
                    legend_on=legend_on,
                    summary=summary_on,
                    all_roots=run.roots,
                    group=True,
                ),
                EXIT_OK,
            )
        return (
            render_atlas(
                subset,
                meta=run.meta,
                changes=changes,
                site=run.site,
                style=style,
                size=width,
                legend_on=legend_on,
                summary=summary_on,
                # The change records one line each. `new` exists to show them,
                # so they are its content; the default view counts them and
                # points here, which inside this view would point at itself.
                deltas=True,
                all_roots=run.roots,
                group=True,
            ),
            EXIT_OK,
        )

    return (
        render_atlas(
            roots,
            meta=run.meta,
            changes=changes,
            site=run.site,
            style=style,
            size=width,
            hidden=hidden,
            legend_on=legend_on,
            summary=summary_on,
            # The unfiltered list, so the summary lines can count the rows the
            # default view deliberately holds back.
            all_roots=run.roots,
            group=not show_all,
        ),
        EXIT_OK,
    )


# --------------------------------------------------------------------------
# Entry point
# --------------------------------------------------------------------------


def _write(text):
    # type: (str) -> None
    """Write through `sys.stdout.write`, and survive a closed pipe.

    `dirscape | head` closes the pipe early, and an unhandled EPIPE prints a
    traceback over the output the user asked for.
    """
    try:
        sys.stdout.write(text)
        if text and not text.endswith("\n"):
            sys.stdout.write("\n")
        sys.stdout.flush()
    except IOError as exc:
        if exc.errno == errno.EPIPE:
            with contextlib.suppress(Exception):
                sys.stdout.close()
            return
        raise


def _surfaceable(run):
    # type: (Run) -> List[str]
    """Warnings worth telling the user about, whatever the view is.

    The stranded summary is excluded: it has its own subcommand, and repeating
    it as a warning said the same thing twice. The seeded-baseline note is
    excluded for the same reason, since the `new` view says it in full.

    **These go to stderr, not into the table.** A malformed
    `/etc/dirscape/site.conf`, a damaged baseline and a baseline that could not
    be written are the tool saying it could not do its job, so they are not
    chrome and they cannot move behind `--summary` with the counts. stderr is
    where they belong for the same reason `--timing` uses it: `dirscape | grep`
    should get a clean table, and a failure should still reach the person at
    the terminal.
    """
    skip = ("holds space in ", "no baseline yet, seeded")
    out = []  # type: List[str]
    for text in run.warnings:
        if any(text.startswith(prefix) for prefix in skip):
            continue
        if text not in out:
            out.append(text)
    return out


# --------------------------------------------------------------------------
# Making a block fit the window it is repainted in
# --------------------------------------------------------------------------

#: The escape byte, spelled once. `_atoms` and `_sgr_state` both reason about
#: where an escape sequence starts, and the literal in three places is three
#: chances to mistype it.
ESC = "\033"


def _window_cols(style):
    # type: (object) -> int
    """Columns the repaint arithmetic has to live inside.

    `style.size` comes from `term_width`, which CLAMPS to [MIN_WIDTH,
    MAX_WIDTH]: a 120 column window is laid out at 120 and a 300 column one at
    200, and wrapping at either is safe because both are at most the real
    width. A window NARROWER than MIN_WIDTH is the case that clamp gets wrong
    for this purpose, since it hands back 32 for a 24 column terminal while
    the terminal still wraps at 24, so the measured figure wins here.
    """
    room = int(getattr(style, "size", 0) or render_style.FALLBACK_WIDTH)
    try:
        columns = int(shutil.get_terminal_size().columns)
    except Exception:  # pragma: no cover - nothing to ask
        columns = 0
    if columns > 0:
        room = min(room, columns)
    return max(8, room)


def _atoms(text):
    # type: (str) -> List[Tuple[str, int]]
    """One (piece, display width) pair per character, each ANSI escape whole.

    The unit a hard break is allowed to happen between. Breaking on characters
    alone would cut an escape sequence in half and send its tail to the
    terminal as text.
    """
    out = []  # type: List[Tuple[str, int]]
    index = 0
    size = len(text)
    while index < size:
        if text[index] == ESC:
            end = index + 1
            if end < size and text[end] == "[":
                end += 1
                while end < size and not (0x40 <= ord(text[end]) <= 0x7E):
                    end += 1
            out.append((text[index : end + 1], 0))
            index = end + 1
            continue
        out.append((text[index], render_style.width(text[index])))
        index += 1
    return out


def _sgr_state(atoms, start=""):
    # type: (Sequence[Tuple[str, int]], str) -> str
    """The colour run still open after these atoms, "" when none is."""
    state = start
    for piece, span in atoms:
        if span == 0 and piece.startswith(ESC):
            state = "" if piece in (interactive.RESET, ESC + "[m") else state + piece
    return state


def _fit(lines, size, hang="    "):
    # type: (Sequence[str], int, str) -> List[str]
    """Break every line to ``size`` columns, so LOGICAL lines equal physical rows.

    `interactive.select` repaints by moving the cursor up by the number of
    lines it last WROTE and clearing from there. A line the terminal wraps
    occupies two rows and counts as one, so the cursor stops a row short, the
    erase starts a row too low, and one row survives every repaint.

    Measured on `_why("/project/hpc")` before the detail view was rewritten:
    18 lines written against 19 rows occupied, at every width from 80 to 120,
    because the writable verdict's reason ran to 157 characters. Thirteen
    presses of Down left thirteen copies of the header stacked above the
    detail, which is the bug this exists for.

    Breaks at the last space that fits, and mid-token only when a token is
    itself too long. That case is the one that matters: a path contains no
    spaces, and `render.style.wrap` leaves an over-long word hanging off the
    edge rather than splitting it. The colour run open at a break is closed
    and re-opened, so a wrapped coloured sentence keeps its tint on the second
    row.
    """
    room = max(8, int(size))
    out = []  # type: List[str]
    for line in lines:
        if render_style.width(line) <= room:
            out.append(line)
            continue
        indent = line[: len(line) - len(line.lstrip(" "))] + hang
        prefix = ""
        carry = ""
        held = []  # type: List[Tuple[str, int]]
        used = 0
        cut = -1
        for atom in _atoms(line):
            piece, span = atom
            # At least four columns of content per row whatever the indent
            # measures, so a pathological leading indent cannot stall this.
            limit = max(4, room - render_style.width(prefix))
            if used + span > limit and held:
                head, tail = (held[:cut], held[cut:]) if cut > 0 else (held, [])
                while head and head[-1][0] == " ":
                    head.pop()
                state = _sgr_state(head, carry)
                body = "".join(text for text, _ in head)
                out.append(prefix + carry + body + (interactive.RESET if state else ""))
                carry = state
                prefix = indent
                held = tail
                used = sum(span_of for _, span_of in tail)
                cut = -1
            held.append(atom)
            used += span
            if piece == " ":
                cut = len(held)
        if held:
            out.append(prefix + carry + "".join(text for text, _ in held))
    return out


def _still(reader=None):
    # type: (Optional[Callable[[], str]]) -> Callable[[], str]
    """A key reader that turns Up and Down into a key nothing repaints for.

    A one row view has nowhere to move, so a repaint on Up or Down is pure
    cost: `select` wraps the cursor round onto the same row and paints the
    same block again. `Key.OTHER` is the one decoded key `select` ignores
    without repainting, which is why these map onto it.

    Done from the caller rather than inside `select`, which has to keep
    treating Up and Down as movement for the table above.
    """

    def read():
        # type: () -> str
        key = (reader or interactive.read_key)()
        if key in (interactive.Key.UP, interactive.Key.DOWN):
            return interactive.Key.OTHER
        return key

    return read


#: How many children one directory listing will show. A `/project` with 668
#: entries is not a screen, and the reader is looking for one of them rather
#: than reading all of them; the count of what was held back is printed.
CHILD_LIMIT = 200


def _children(path, limit=CHILD_LIMIT):
    # type: (str, int) -> Tuple[List[Dict[str, object]], int]
    """The sub-directories of one path, with what a listing can cheaply know.

    **One `scandir` and one `stat` per entry, and no walking.** Size per child
    is deliberately absent: it would mean a walk of each subtree, which for a
    `/project/hpc` holding 11T is the operation this whole package exists to
    avoid. What is here comes from the directory read itself, so a listing of
    a few hundred children costs milliseconds.

    Files are left out. This is a browser for places to put data, and the
    reader is descending towards a directory; a home with 300 dotfiles in it
    would bury the four directories that matter.
    """
    out = []  # type: List[Dict[str, object]]
    try:
        entries = sorted(os.scandir(path), key=lambda e: e.name)
    except OSError:
        return [], 0
    held = 0
    for entry in entries:
        try:
            if not entry.is_dir(follow_symlinks=False):
                continue
        except OSError:
            continue
        if len(out) >= limit:
            held += 1
            continue
        child = entry.path
        try:
            inner = len(os.listdir(child))
        except OSError:
            # Readable as an entry, not readable as a directory: that is the
            # traverse-only case and it is a fact worth showing, not an error.
            inner = None
        out.append(
            {
                "name": entry.name,
                "path": child,
                "items": inner,
                "readable": os.access(child, os.R_OK | os.X_OK),
                "writable": os.access(child, os.W_OK),
                "enterable": os.access(child, os.X_OK),
            }
        )
    return out, held


def _listing(path, style, cols=None, window=None, cursor=0, kids=None):
    # type: (str, object, Optional[int], Optional[int], int, Optional[List[Dict[str, object]]]) -> Tuple[List[str], List[Dict[str, object]], int]
    """One directory's children, as the same framed table as the main view.

    The owner's description of what opening a row should do: "there should be
    all the sub-dirs shown just like the main ui and you can constantly zoom
    in if there is sub dirs within these sub-dirs." What it did instead was
    print that one root's facts as a field list, which answers a different
    question ("tell me about this directory") from the one Enter asks ("what
    is inside it"), and the second had no answer anywhere in the interactive
    view: "this is weird. i don't need to know this kind of info." The field
    list is still `dirscape why <path>`, and the footer says so.

    **Returns a WINDOW of rows around the cursor, not all of them.** The first
    version rendered every child and `/project/hpc` has 668, so the block was
    twenty times the height of the terminal: the frame was truncated to fit,
    which cut the rows off the bottom, which meant the selection band had
    nothing to land on and never painted at all. A listing that cannot show
    its own selection is not a listing. The third return value is the index of
    the highlighted row WITHIN the returned lines, so the caller does not have
    to guess where the rows begin.
    """
    cols = _window_cols(style) if cols is None else max(8, int(cols))
    inner = max(8, cols - 4)
    if kids is None:
        kids, _held = _children(path)

    head = [style.head(sanitize(path, limit=4096))]
    tail_keys = "   %s open   %s back   %s quit" % (
        style.accent("enter"),
        style.accent("esc/left"),
        style.accent("q"),
    )
    if not kids:
        lines = head + [
            style.dim("  nothing to open inside this directory"),
            "",
            style.dim("   %s back   %s quit" % (style.accent("esc/left"), style.accent("q"))),
        ]
        return _fit(panel(lines, style=style, size=cols, shrink=False).splitlines(), cols), [], -1

    # Nine rows of chrome plus the one `select` leaves the cursor on: two
    # borders, the path, a blank, the rule, the column heading, the "N above,
    # N below" counter, a blank and the key hints. COUNTED against a rendered
    # block rather than estimated, because the first guess was 8 and produced
    # 31 lines in a 30 row terminal, and one line over is not a cosmetic
    # error: `select` repaints by moving the cursor up by the number of lines
    # it wrote, so a block taller than the window has scrolled by the time it
    # is erased and takes the reader's scrollback with it.
    room = max(1, int(window) - 10) if window else len(kids)
    first = max(0, min(cursor - room // 2, len(kids) - room))
    shown = kids[first : first + room]

    rows = []
    for kid in shown:
        if kid["readable"]:
            access = "read + write" if kid["writable"] else "read only"
        elif kid["enterable"]:
            access = "enter only"
        else:
            access = "no access"
        items = kid["items"]
        rows.append(
            [
                kid["name"] + "/",
                access,
                render_fields.UNKNOWN if items is None else render_fields.human_count(items),
            ]
        )

    body, _dropped = render_style.table(
        ["name", "access", "items"],
        rows,
        aligns=["left", "left", "right"],
        style=style,
        size=inner,
        indent="   ",
        gutter="    ",
        underline=False,
        drop_empty=False,
        spread=True,
    )
    lines = list(head)
    lines.append("")
    lines.append(style.dim(style.g.h * max(1, inner - 2)))
    body_at = len(lines) + 1  # the table's heading row comes first
    lines.extend(body.splitlines())
    above, below = first, len(kids) - (first + len(shown))
    if above or below:
        lines.append(
            style.dim("   %d of %d, %d above, %d below" % (cursor + 1, len(kids), above, below))
        )
    lines.append("")
    lines.append(style.dim(tail_keys))

    framed = _fit(panel(lines, style=style, size=cols, shrink=False).splitlines(), cols)
    # `panel` adds exactly one line at the top, so the row's index in the
    # framed block is its index here plus one. Asserted by construction rather
    # than searched for: the earlier version located rows by matching a `/`,
    # which the path line at the top also contains and a child named without
    # one would not.
    band = body_at + (cursor - first) + 1
    if not 0 <= band < len(framed):
        band = -1
    return framed, kids, band


def _detail(run, root, style, cols=None, window=None):
    # type: (Run, object, object, Optional[int], Optional[int]) -> List[str]
    """One row's `why`, framed and guaranteed to fit the window it repaints in.

    A function rather than six lines inside `_browse` because the property it
    has to hold is testable and was broken: **every line is at most one
    physical row, and the whole block is at most one row shorter than the
    window.** `interactive.select` repaints by moving the cursor up by the
    number of lines it wrote, so a block that occupies more rows than it has
    lines leaves one behind on every keypress, and a block taller than the
    window has scrolled by the time it is erased. See `_fit`.

    Four columns of every row belong to the frame `panel` draws, so the detail
    is built and wrapped to what is left. Wrapping here rather than leaving it
    to the terminal also keeps `panel` from having to truncate, which on this
    block would eat an ellipsis into a path.
    """
    cols = _window_cols(style) if cols is None else max(8, int(cols))
    inner = max(8, cols - 4)
    window = interactive.window_rows() if window is None else int(window)
    detail, _ = _why(run, root.path or "/", style, size=inner)
    lines = _fit(
        detail.splitlines()
        + [
            "",
            style.dim(
                "   %s back   %s quit" % (style.accent("esc/left"), style.accent("q")),
            ),
        ],
        inner,
    )
    if window:
        # Three rows are spoken for: the frame's two borders, and the one
        # `select` leaves the cursor on below the block. Dropping the tail
        # keeps the arithmetic true, and the line that replaces it names the
        # command that prints the screen in full.
        fits = max(1, window - 3)
        if len(lines) > fits:
            marker = _fit(
                _prose(
                    style,
                    inner,
                    "The window is too short for the rest. dirscape why %s prints it in full."
                    % (root.path or "/",),
                ),
                inner,
            )
            lines = (lines[: max(1, fits - len(marker))] + marker)[:fits]
    # `_fit` again over the framed block, as the backstop: if `panel` ever
    # returns a row wider than the window it was given, a broken looking box
    # is a far smaller failure than a repaint that wipes the scrollback.
    # `shrink=False`: the frame is the WINDOW wide, not the detail's content.
    #
    # The owner caught this as a bug and it is one: "when going to different
    # dirs, the ui will shrink the horizontal spacing, which is annoying. that
    # shouldn't change." The table fills the window, and this panel wrapped
    # tight to whatever the opened row happened to say, so the box jumped
    # narrower on the way in and wider on the way out, and a different width
    # again for each row. A frame that resizes as the cursor moves reads as
    # the layout breaking, and the two views are one screen replacing another
    # in place rather than two separate printouts.
    return _fit(panel(lines, style=style, size=cols, shrink=False).splitlines(), cols)


def _table_frame(
    roots,  # type: Sequence[object]
    cursor,  # type: int
    run,  # type: Run
    style,  # type: object
    width,  # type: Optional[int]
    footer=(),  # type: Sequence[str]
    changes=(),  # type: Sequence[object]
    hidden=0,  # type: int
    legend_on=False,  # type: bool
    summary_on=False,  # type: bool
    show_all=False,  # type: bool
):
    # type: (...) -> List[str]
    """The interactive table with row ``cursor`` highlighted, framed.

    **Module level, and that is the point of it.** This was a closure inside
    `_browse`, reachable only by driving a pty, and it shipped a defect that
    the whole static-render test suite could not see: it laid the atlas out
    against the FULL window while drawing the frame itself, so every line came
    out four columns too wide and `panel` truncated the last cell. Nothing
    caught it because nothing could call it. Untestable code is where the bugs
    live, so it is a function now.

    **Unframed atlas, framed here.** The band is painted on a CONTENT line and
    the panel is drawn around the result, so the selection sits inside the
    border. Highlighting the finished view instead would invert the two border
    characters along with the row and pad the band past them.
    """
    window = width or style.size
    text = render_atlas(
        roots,
        meta=run.meta,
        changes=list(changes),
        site=run.site,
        style=style,
        # **Minus the frame, because this call does not draw it.** With
        # `frame=False` the atlas lays its content out against the size it is
        # given, and `panel` below needs four of those columns for its border
        # and padding. Passing the full window was the truncation bug above.
        size=max(_MIN_BODY, window - 4),
        hidden=hidden,
        legend_on=legend_on,
        summary=summary_on,
        all_roots=run.roots,
        group=not show_all,
        frame=False,
    )
    lines = text.splitlines() + list(footer)
    # The cursor indexes ROOTS, and the block has a title, a blank line, a rule
    # and a column header above the first row. Located by matching the row's
    # own path rather than by counting chrome, because the chrome changes with
    # the window and a counted offset would put the highlight on the wrong line
    # at the one width nobody tested.
    #
    # Matched against the line with its escapes REMOVED, because a cell may
    # carry colour and the path is then not a substring of the line it is
    # printed on.
    target = roots[cursor].path or (roots[cursor].policy or {}).get("allocation_location", "")
    for position, line in enumerate(lines):
        if target and target in plain(line):
            lines = interactive.highlight(lines, position)
            break
    # `shrink=False` for the same reason `_detail` uses it: every frame in the
    # interactive session is the window wide, so none of them changes size as
    # the reader moves or drills in. The table's content already fills the
    # window, so this only matters on the degraded paths where it does not.
    return panel(lines, style=style, size=window, shrink=False).splitlines()


def _descend(start, style, width):
    # type: (str, object, Optional[int]) -> object
    """Walk down a directory tree, one listing at a time, until the reader leaves.

    A STACK rather than recursion, so depth costs nothing and "back" is a pop.
    The reader can go as deep as the tree goes, which is what was asked for:
    "you can constantly zoom in if there is sub dirs within these sub-dirs."

    Returns `QUIT` when the reader wants out of the program entirely, and
    anything else when they have merely come back up past the top of this
    tree, which returns them to the table they opened it from.
    """
    stack = [start]
    cursors = {}  # type: Dict[str, int]
    while stack:
        here = stack[-1]
        rows = interactive.window_rows()
        kids, _held = _children(here)
        lines, kids, _band = _listing(here, style, cols=width, window=rows, kids=kids)
        if not kids:
            # Nothing to open. Show the listing (which says so) and treat any
            # key except `q` as "back", because there is nowhere to go but up.
            outcome = interactive.select(
                lambda i, block=lines: block,
                1,
                keys=_still(),
                initial=0,
                escapable=True,
                openable=False,
            )
            if outcome == interactive.Key.QUIT:
                return interactive.Key.QUIT
            stack.pop()
            continue

        def paint(index, where=here, entries=kids, height=rows):
            # type: (int, str, List[Dict[str, object]], int) -> List[str]
            # Rebuilt per keypress rather than painted over a fixed block,
            # because the visible window of rows MOVES with the cursor: a
            # directory with 668 children cannot be shown at once, and a band
            # that can only travel as far as the first screenful is a listing
            # the reader cannot reach the bottom of.
            block, _kids, band = _listing(
                where, style, cols=width, window=height, cursor=index, kids=entries
            )
            return interactive.highlight(block, band) if band >= 0 else block

        choice = interactive.select(
            paint,
            len(kids),
            initial=min(cursors.get(here, 0), len(kids) - 1),
            escapable=True,
        )
        if choice == interactive.Key.QUIT:
            return interactive.Key.QUIT
        if choice == interactive.Key.BACK:
            stack.pop()
            continue
        index = int(choice)  # type: ignore[arg-type]
        cursors[here] = index
        stack.append(str(kids[index]["path"]))
    return interactive.Key.BACK


def _browse(run, opts, style, width):
    # type: (Run, argparse.Namespace, object, Optional[int]) -> int
    """The atlas, with a highlight you can move and open.

    Two levels: the table, and `why` for the row you open. The block is
    rendered once per keypress by the SAME renderer the static print uses, so
    the interactive view cannot drift from the printed one; the only thing
    this adds is inverse video on one line.

    Falls back to printing on anything unexpected. A browse that fails should
    leave the user with the report, not with a traceback where the report was.
    """
    show_all = bool(_merge_flag(opts, "all", False))
    roots, hidden = _visible(run, show_all)
    changes = list(run.changes or [])
    legend_on = bool(_merge_flag(opts, "legend", False))
    summary_on = bool(_merge_flag(opts, "summary", False))

    def frame(cursor):
        # type: (int) -> List[str]
        return _table_frame(
            roots,
            cursor,
            run=run,
            style=style,
            width=width,
            footer=footer,
            changes=changes,
            hidden=hidden,
            legend_on=legend_on,
            summary_on=summary_on,
            show_all=show_all,
        )

    footer = [
        "",
        style.dim(
            "   %s move   %s open   %s quit"
            % (
                style.accent("up/down"),
                style.accent("enter"),
                style.accent("q"),
            )
        ),
    ]

    def leave():
        # type: () -> int
        """Put the report back on screen on the way out.

        The interactive frame is erased when it exits, and erasing the last
        frame left the user looking at a blank terminal where their scrollback
        used to be: "the entire terminal turns empty rather than staying at
        where it was". Printing the static report once on exit means a browse
        ends the way a plain run ends, with the table in the scrollback, and it
        is robust to the cursor arithmetic being off by a line.
        """
        text, _ = _render(run, opts, "atlas", style, width)
        _write(text)
        return EXIT_OK

    # A frame taller than the window cannot be repainted in place. Moving the
    # cursor up by the block's height lands at the TOP OF THE WINDOW rather
    # than the top of the block, because the block has scrolled, and the erase
    # that follows then wipes whatever the user had above it. Measured: a
    # `--all` run in a 14 row terminal asked to move up 71 lines, which is the
    # "entire terminal turns empty" symptom.
    #
    # `interactive.supported()` cannot catch this: it knows the window height
    # but not what is about to be drawn in it.
    rows = interactive.window_rows()
    first = frame(0)
    if rows and len(first) + 1 > rows:
        text, _ = _render(run, opts, "atlas", style, width)
        _write(text)
        sys.stderr.write(
            "\n(%d rows to show in a %d row window, so this is the static "
            "report; widen the window or use dirscape --all less)\n" % (len(first), rows)
        )
        return EXIT_OK

    cursor = 0
    while True:
        chosen = interactive.select(
            frame,
            len(roots),
            initial=cursor,
            escapable=False,
        )
        if chosen in (interactive.Key.QUIT, interactive.Key.BACK):
            return leave()
        cursor = int(chosen)  # type: ignore[arg-type]
        if _descend(roots[cursor].path or "/", style, width) == interactive.Key.QUIT:
            return leave()


def main(argv=None):
    # type: (Optional[Sequence[str]]) -> int
    parser = build_parser()
    opts = parser.parse_args(list(argv) if argv is not None else None)

    timeout = getattr(opts, "timeout", None)
    if timeout is not None and timeout <= 0:
        # Silently accepted before, and the result was a run where every probe
        # came back NOT_PROBED and the table was sixty rows of `?`. A budget of
        # zero cannot answer anything, so saying so beats performing a run
        # that cannot work.
        sys.stderr.write(
            "dirscape: --timeout must be greater than zero (got %g). "
            "A zero or negative budget cannot probe anything.\n" % (timeout,)
        )
        return EXIT_USAGE

    if opts.site_template:
        _write(SITE_TEMPLATE)
        return EXIT_OK

    command = opts.command or "atlas"

    ascii_only = bool(_merge_flag(opts, "ascii", False)) or bool(os.environ.get("DIRSCAPE_ASCII"))
    style = resolve_style(
        color=str(_merge_flag(opts, "color", "auto")),
        ascii_only=ascii_only,
        stream=sys.stdout,
    )

    try:
        run = sweep(opts)
    except KeyboardInterrupt:
        # 130 is the shell's convention for SIGINT and it matters here: a user
        # who interrupts a slow probe should not see a traceback, and a script
        # should be able to tell that from a real failure.
        sys.stderr.write("\ninterrupted\n")
        return 130
    except OSError as exc:
        sys.stderr.write("dirscape: %s\n" % (exc,))
        return EXIT_USAGE

    if not run.roots:
        sys.stderr.write(
            "dirscape found no storage roots at all.\n"
            "On a working cluster that means every probe was refused rather "
            "than that there is no storage. Try `dirscape why /` or "
            "`dirscape --all`.\n"
        )
        return EXIT_NOTHING

    width = None

    # Interactive when there is somebody to type at it and nothing that would
    # be broken by a repaint: never under `--json`, never when replaying a
    # transcript, and never for a view whose whole output is one paragraph.
    if (
        command == "atlas"
        and not _merge_flag(opts, "json", False)
        and not _merge_flag(opts, "replay", None)
        and interactive.supported()
    ):
        try:
            return _browse(run, opts, style, width)
        except Exception:
            # Fall through to the static print. A failed browse must leave the
            # user holding the report.
            pass

    text, code = _render(run, opts, command, style, width)
    _write(text)

    # Never silent, and never in the table. See `_surfaceable`.
    for note in _surfaceable(run):
        sys.stderr.write("dirscape: %s\n" % (note,))

    if _merge_flag(opts, "timing", False):
        sys.stderr.write("\nstage timings:\n")
        last = 0.0
        for label, elapsed in run.timings:
            sys.stderr.write("  %-14s %6.3fs\n" % (label, elapsed - last))
            last = elapsed
        sys.stderr.write("  %-14s %6.3fs\n" % ("total", last))

    return code


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
