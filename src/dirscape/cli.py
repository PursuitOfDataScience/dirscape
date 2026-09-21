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
import sys
import time
from typing import Dict, List, Optional, Sequence, Tuple

from . import __version__, interactive
from .discover import (
    RANK_PRIMARY,
    attribute_all,
    discover,
    node_class,
    read_identity,
    read_mount_table,
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
        help=h("explain the reach letters and the bar marks"),
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


def _attach_quota(run, budget, runner):
    # type: (Run, Budget, object) -> None
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


def _attach_capacity(run):
    # type: (Run) -> None
    """Last resort for a root no quota backend could speak for: `statvfs`.

    Four rows of the default view were a bare `?`: `/tmp`, `/.nodelog/log`,
    `/scratch/local/jdoe42` and one GPFS scratch the wrapper could not
    attribute. Two of those are XFS mounted `noquota`, so no quota exists to
    read and `?` was the literal truth and useless anyway: the filesystem
    knows exactly how much room is left and `df` prints it.

    Stored as free BYTES rather than as a quota row, and rendered as
    "<n> free" rather than as `used / limit`, because it is not your usage.
    It is the whole filesystem's headroom, shared with everyone else on the
    node. Labelling it as a quota would be the fabrication this tool exists to
    avoid; withholding it when `df` would answer is just unhelpful.
    """
    for root in run.roots:
        if root.quota is not None or not root.path:
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
        return [row for row in rows if row.fileset and row.fileset == fileset]

    target = root.path.rstrip("/") or "/"
    return [row for row in rows if (row.mount or "").rstrip("/") == target]


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

    _attach_quota(run, budget, runner)
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


def _why_allocation(root, style):
    # type: (object, object) -> str
    """Explain a root that the allocation database names and this node lacks.

    Separate from `_why` because every probe it would print is inapplicable:
    with no path there is nothing to stat, so no reach, no fileset and no
    quota. What is known is the account, the size and the fact that the
    absence is about this node rather than about the storage.
    """
    policy = root.policy or {}
    location = str(policy.get("allocation_location") or "?")
    out = [style.head(location), style.dim("  an allocation, not a path on this node"), ""]

    gb = policy.get("allocation_gb")
    if isinstance(gb, (int, float)) and gb > 0:
        out.append("  %-11s %s" % ("size", render_fields.human_bytes(int(gb * 1000 * 1000 * 1000))))
    accounts = policy.get("allocation_accounts")
    if isinstance(accounts, (list, tuple)) and accounts:
        out.append("  %-11s %s" % ("account", ", ".join(str(a) for a in accounts)))
    if root.role:
        out.append("  %-11s %s" % ("role", root.role))
    out.append("")
    out.append(
        "  %s %-9s %s"
        % (
            root.allocated.glyph(),
            "allocated",
            style.dim(root.allocated.reason or root.allocated.label),
        )
    )
    out.append(
        "  %s %-9s %s"
        % (root.mounted.glyph(), "mounted", style.dim(root.mounted.reason or root.mounted.label))
    )
    out.append("")
    out.append(
        style.dim(
            "  Nothing here could be measured, because no path for it exists on\n"
            "  this node. That is a fact about where you are standing and not\n"
            "  about the storage."
        )
    )
    return "\n".join(out)


def _why(run, path, style):
    # type: (Run, str, object) -> Tuple[str, int]
    """One path, explained in a screen you can read.

    The first version printed everything it knew: raw byte counts, every
    backend note verbatim, and one line per symlink out of the directory. On a
    home directory with eleven relocated dotfiles that was thirty-odd lines,
    ten of them the same sentence with a different path in it, and the two
    numbers a reader actually wanted were in the middle in bytes.

    So: the headline first, the verdicts as a block, and anything repetitive
    collapsed to a count with a command that expands it. `--json` still
    carries every field for anyone who wants the lot.
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
                    return _why_allocation(root, style), EXIT_OK
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

    out = []  # type: List[str]
    out.append(style.head(match.path))
    if match.path != target:
        out.append(style.dim("  the enclosing root of %s" % (shown,)))

    where = [x for x in (match.role, match.fstype, match.fileset) if x]
    if match.device and match.fileset:
        where = [x for x in (match.role, match.fstype) if x]
        where.append("%s:%s" % (match.device, match.fileset))
    out.append(style.dim("  " + "  ".join(where)))
    out.append("")

    # The headline: the two numbers and the one-word access summary.
    figure, caveat = render_fields.quota_cell(match, style, show_bar=True)
    out.append("  %-9s %s" % ("space", figure))
    inodes, _ = render_fields.inode_cell(match, style)
    if inodes and inodes != render_fields.UNKNOWN:
        out.append("  %-9s %s" % ("files", inodes))
    out.append(
        "  %-9s %s%s"
        % (
            "access",
            Reach.label(match.reach),
            ", writable" if match.writable.confirmed else "",
        )
    )
    if match.elsewhere:
        out.append("  %-9s %s" % ("where", style.warn("allocated, but not mounted on this node")))
    out.append("")

    # The probes, one line each, with the glyph doing the work.
    for label, verdict in (
        ("mounted", match.mounted),
        ("present", match.present),
        ("writable", match.writable),
        ("allocated", match.allocated),
    ):
        # A probe that was never run has nothing to report. `? allocated not
        # probed` appeared on every mounted root, because the allocation
        # database is only consulted for storage that has no path here, and a
        # question mark against a question nobody asked is noise that teaches
        # a reader to skip the column.
        if verdict.category == VerdictCategory.NOT_PROBED:
            continue
        detail = verdict.reason or verdict.label
        out.append("  %s %-9s %s" % (verdict.glyph(), label, style.dim(detail)))
    out.append("")

    # Provenance, short.
    if match.quota is not None and match.quota.source:
        out.append("  %-9s %s" % ("quota", style.dim(match.quota.source)))
    if match.sources:
        out.append("  %-9s %s" % ("found by", style.dim(", ".join(match.sources))))
    if match.labels:
        out.append("  %-9s %s" % ("changed", style.dim(", ".join(match.labels))))

    # The repetitive part, collapsed. Eleven symlinks out of a home directory
    # are one fact about that directory, not eleven facts.
    crossings = [n for n in match.notes if "resolves to" in n]
    others = [n for n in match.notes if "resolves to" not in n]
    if crossings:
        targets = sorted({n.split("resolves to")[1].split(",")[0].strip() for n in crossings})
        out.append("")
        out.append(
            "  %s %d path%s here are symlinks billed elsewhere: %s"
            % (
                style.warn(style.g.warn),
                len(crossings),
                "" if len(crossings) == 1 else "s",
                ", ".join(targets[:2]) + ("..." if len(targets) > 2 else ""),
            )
        )
        out.append(style.dim("    %s for the whole picture" % (style.accent("dirscape tree"),)))
    for note in others[:3]:
        out.append("  %-9s %s" % ("note", style.dim(note)))
    if len(others) > 3:
        out.append(
            style.dim(
                "  %-9s %d more (%s)" % ("", len(others) - 3, style.accent("dirscape --json"))
            )
        )
    if caveat:
        # Deduplicated. Each backend appends its own caveat and the blocks and
        # inodes rows append the same ones again, so the raw string said "the
        # mount for this row was inferred from its name" twice in one line and
        # then said it a third time in longer words.
        # One caveat, not four. Each backend appends its own and the blocks
        # and inodes rows append the same ones again, so the raw string made
        # the same point about an inferred mount three times in three
        # different phrasings, which substring dedupe cannot catch and which
        # was the longest line on the screen. The rest are in `--json`.
        pieces = [piece.strip() for piece in caveat.split("; ") if piece.strip()]
        out.append("  %-9s %s" % ("caveat", style.dim(pieces[0])))
        if len(pieces) > 1:
            out.append(
                style.dim(
                    "  %-9s %d more (%s)" % ("", len(pieces) - 1, style.accent("dirscape --json"))
                )
            )

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
        return _why(run, opts.path, style)

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
            # The unfiltered list, so the summary lines can count the rows the
            # default view deliberately holds back.
            all_roots=run.roots,
            group=not show_all,
            warnings=_surfaceable(run),
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
    """Warnings worth a line in the table.

    The stranded summary is excluded: it already has its own alert line with a
    subcommand, and repeating it as a warning said the same thing twice. The
    seeded-baseline note is excluded for the same reason, since the `new` view
    says it in full.
    """
    skip = ("holds space in ", "no baseline yet, seeded")
    out = []  # type: List[str]
    for text in run.warnings:
        if any(text.startswith(prefix) for prefix in skip):
            continue
        if text not in out:
            out.append(text)
    return out


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

    def frame(cursor):
        # type: (int) -> List[str]
        text = render_atlas(
            roots,
            meta=run.meta,
            changes=changes,
            site=run.site,
            style=style,
            size=width,
            hidden=hidden,
            legend_on=legend_on,
            all_roots=run.roots,
            group=not show_all,
        )
        lines = text.splitlines()
        # The cursor indexes ROOTS, and the block has a header, a blank line,
        # a column header and a rule above the first row. Located by matching
        # the row's own path rather than by counting chrome, because the
        # chrome changes with the window and a counted offset would put the
        # highlight on the wrong line at the one width nobody tested.
        target = roots[cursor].path or (roots[cursor].policy or {}).get("allocation_location", "")
        for position, line in enumerate(lines):
            if target and target in line:
                return interactive.highlight(lines, position)
        return lines

    footer = [
        "",
        style.dim(
            "  %s move   %s open   %s quit"
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
    first = frame(0) + footer
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
            lambda i: frame(i) + footer,
            len(roots),
            initial=cursor,
            escapable=False,
        )
        if chosen in (interactive.Key.QUIT, interactive.Key.BACK):
            return leave()
        cursor = int(chosen)  # type: ignore[arg-type]
        root = roots[cursor]
        detail, _ = _why(run, root.path or "/", style)
        lines = detail.splitlines() + [
            "",
            style.dim("  %s back   %s quit" % (style.accent("left"), style.accent("q"))),
        ]
        # openable=False: this is the bottom, and Right here would otherwise
        # read as "step back" and bounce the reader into the same view again.
        # `block` is bound as a default rather than captured: `lines` is
        # reassigned on every pass of this loop, and a closure over it would
        # show whichever frame the loop last reached. It happens to work today
        # only because `select` is called immediately, which is exactly the
        # kind of accident that survives until somebody adds a line between
        # the two.
        outcome = interactive.select(
            lambda i, block=lines: block,
            1,
            initial=0,
            escapable=True,
            openable=False,
        )
        if outcome == interactive.Key.QUIT:
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
