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
import functools
import json
import os
import shlex
import shutil
import stat as stat_module
import sys
import threading
import time
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

from . import __version__, interactive
from . import search as search_module
from .discover import (
    DEFAULT_DEADLINE_S,
    RANK_PRIMARY,
    attribute_all,
    classify_fstype,
    copies_for_path,
    discover,
    find_snapshots,
    inside_snapshot_tree,
    node_class,
    read_identity,
    read_mount_table,
    restates_source,
    source_label,
    with_deadline,
)
from .discover.mounts import KIND_NETWORK, lustre_filesystem
from .model import QuotaRow, Reach, Root, VerdictCategory, confirmed, refuted, sanitize, unknown
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
from .render.atlas import _MIN_BODY, _SHARE_PERCENT, _title_lines
from .render.style import panel, plain
from .runner import Budget, RecordedRunner, SubprocessRunner
from .sitecfg import SITE_TEMPLATE, guess_cluster_name, load_site
from .state import Lineage, Snapshot, cutoff_for, diff
from .state.sizes import SizeIndex

EXIT_OK = 0
EXIT_USAGE = 1
EXIT_PATH = 2
EXIT_NOTHING = 3

#: Total wall-clock allowance for one run. It exists so one wedged mount cannot
#: make the command hang, not as a performance target: a healthy run finishes
#: in one to four seconds and stops there.
#:
#: Eight was not enough, and the way it failed is why this is twenty. Measured
#: on a meadow2 login node, the first run of a session needed about 8.5s of
#: budgeted work with cold GPFS caches (4.2s warm), ran out part-way through
#: discovery, and printed `/project 851M 30G`: a home quota from another
#: cluster, on a directory nobody had probed. A backstop the healthy case can
#: reach is not a backstop.
DEFAULT_TIMEOUT_S = 20.0

#: Of that allowance, the share the quota sweep may spend before discovery
#: starts. The sweep runs first because its fileset list feeds discovery, and
#: with no ceiling one slow wrapper could spend the whole run, leaving every
#: root unprobed. Probing roots is the cheap part and the part everything else
#: stands on, so it keeps the rest.
QUOTA_SHARE = 0.6

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
        help=h("do not read or write the snapshot lineage or saved folder sizes"),
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
        help=h("explain the access words and the marks the table uses"),
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
            "Sizes come from quotas, so dirscape's cost is the number of roots, "
            "not the number of files. The one walk it makes is capped, of a root "
            "no quota covers, and --no-measure skips it. For bytes per directory "
            "use `rdu` or `ncdu`. "
            "Scripts and agents: `dirscape paths --json` lists every place with "
            "exact figures, `dirscape why PATH --json` explains one path, and "
            "`dirscape mcp` serves the same answers as MCP tools."
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

    # Named `recover` and not `snapshot`, which this tool already spends on
    # recording its own baseline. Two meanings of one word on one command line
    # is how `dirscape snapshot ~/thesis.tex` would quietly overwrite a
    # baseline instead of finding a file.
    recover = sub("recover", "read-only copies of one path the filesystem still keeps")
    recover.add_argument("path", help="the path to look for copies of; it need not still exist")

    sub("matrix", "roots by capability, as confirmed / refused / could not determine")
    sub("tree", "device, then fileset, then paths, with quota-crossing symlinks marked")
    sub("map", "a treemap of where your bytes live, across every root at once")
    sub("stranded", "space you hold in filesets you cannot reach")
    sub("elsewhere", "allocations with no path on this node")
    sub("snapshot", "record a baseline without printing a table")

    export = sub("ncdu", "export one root as ncdu-compatible JSON")
    export.add_argument("path", help="the root to export")

    # The agent's two doors. `paths` is the table's rows as data: one path per
    # line for a shell, records with exact figures under `--json`. `mcp` is
    # the same answers as tools an agent calls natively. Neither records a
    # baseline, so an agent looking never moves what `dirscape new` compares
    # against.
    paths = sub(
        "paths",
        "the places you can put data, one path per line; --json for exact figures, "
        "--all for every root",
    )
    paths.add_argument(
        "--writable",
        action="store_true",
        default=False,
        help="only places where writing was confirmed",
    )
    paths.add_argument(
        "--kind",
        metavar="KIND",
        default=None,
        help="only this kind (home, project, scratch, dataset, software, archive, local, "
        "other); comma-separate several",
    )
    paths.add_argument(
        "--min-free",
        metavar="SIZE",
        default=None,
        help="only places with at least this much free, e.g. 500G or 2T (binary, as du -h)",
    )
    sub("mcp", "serve these answers to an agent over MCP (stdio)")

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
        "baseline_since",
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
        # The `--since` that chose the baseline, or None when it is the last run.
        self.baseline_since = None  # type: Optional[str]
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

    _place_rows(run, junction, accessible, owned, writable_only)

    # **Ask the path-sensitive backends again, now that the roots are known.**
    #
    # The sweep above runs BEFORE discovery, because its fileset enumeration
    # is itself a discovery source, and it asks about `/`. That is enough for
    # `mmlsquota`, which lists every fileset the reader holds on a device
    # whatever path named it, and it is not enough for a project quota, which
    # is a property of the DIRECTORY. Measured: `lfs project -d /lus/egret`
    # returns 0 and `lfs project -d /lus/egret/projects/lanternlab-exampleu`
    # returns 13579, so the project scope was never asked and a 29.58T
    # allocation against a 50T quota rendered `? ? ?`.
    #
    # Only backends that declare `per_path`, only the paths that came back
    # empty, and only once. A root that already has a figure is not re-asked,
    # so on a GPFS cluster this list is empty and nothing runs.
    # Skipping a path the sweep already asked about by name. On Lustre that is
    # every mountpoint, and after `_may_carry` a read-only mount keeps no
    # figure, so without this each run asked `lfs` the same three questions
    # about `/home`, `/lus/egret` and `/lus/grove` a second time and threw the
    # identical answers away again.
    asked = set()
    for snap in run.quota_attempts or ():
        for row in getattr(snap, "rows", None) or ():
            if getattr(row, "mount", None) and not getattr(row, "guessed", False):
                asked.add(row.mount.rstrip("/") or "/")
    unanswered = [
        r.path
        for r in run.roots
        if r.path and r.quota is None and r.reachable and (r.path.rstrip("/") or "/") not in asked
    ]
    if unanswered:
        extra = [b for b in default_backends(run.site) if getattr(b, "per_path", False)]
        for backend in extra:
            if budget is not None and getattr(budget, "exhausted", False):
                break
            try:
                snap = backend.read(runner, run.mounts, budget, unanswered)
            except Exception as exc:
                run.warnings.append("%s could not be re-asked: %s" % (backend.name, exc))
                continue
            if snap is not None and getattr(snap, "rows", None):
                run.quota_attempts.append(snap)
        _place_rows(run, junction, accessible, owned, writable_only)

    _attach_capacity(run)
    if measure:
        # After the backends and the capacity fallback, so it only ever walks
        # what nothing else could answer for.
        _measure(run, budget, show_all=show_all)


def _prefer_enforced(rows):
    # type: (Sequence[object]) -> List[object]
    """Rows reordered so one carrying an enforced LIMIT comes first.

    When several scopes all describe the same directory, the one with a limit
    is the one the reader needs, because it is the one that will stop their
    job. Measured on a Lustre project directory, where three scopes all name
    the same path and only one of them is the allocation:

        user     used 4.1T    limit none    <- this reader across the whole filesystem
        group    used 2.7P    limit none    <- every member of the group, everywhere
        project  used 29.5T   limit 55T     <- the allocation this directory IS

    Taking the first row produced `4.1T of none` against a directory whose
    real answer is `29.5T of 55T`: a true figure about something else, which
    is the failure this module is most careful about everywhere else.

    Stable, so where nothing is enforced the backend's own order survives and
    the user-scoped row stays first, which is the right default when no
    figure is a limit: it is the only one that is about this reader alone.
    """
    return sorted(rows, key=lambda row: 0 if (row.hard or row.soft) else 1)


def _governing_snapshot(attempts, root):
    # type: (Sequence[object], object) -> Tuple[object, List[object]]
    """The first attempt that actually GOVERNS this root, and its rows.

    `select_snapshot` chooses on `rows_for_path`, which is a path-prefix
    test, while `_rows_governing` is far stricter. So a snapshot could win
    the selection and then govern nothing, and the root fell back to `?` with
    a better answer sitting unread in the next attempt.

    That gap is what made the per-path re-ask useless on Lustre. The sweep's
    `lfs quota` snapshot holds a row for the mount `/lus/egret`, which is a
    prefix of `/lus/egret/projects/lanternlab-exampleu`, so it won the
    selection for that path and governed none of it; the re-asked snapshot,
    which had the project row, was never looked at.

    Attempt order is still precision of attribution, and a snapshot that
    measured something outranks one that merely could not say no, so the
    OK-category pass runs first.
    """
    fallback = (None, [])  # type: Tuple[object, List[object]]
    for snap in attempts or ():
        if not getattr(snap, "available", False) or not getattr(snap, "rows", None):
            continue
        rows = _rows_governing(snap, root)
        if not rows:
            continue
        if snap.category == VerdictCategory.OK:
            return snap, rows
        if fallback[0] is None:
            fallback = (snap, rows)
    return fallback


def _place_rows(run, junction, accessible, owned, writable_only):
    # type: (Run, Dict[Tuple[str, str], str], Dict[Tuple[str, str], str], Dict[Tuple[str, str], str], Dict[Tuple[str, str], str]) -> None
    """Give each root the rows that govern it, on the one root that shows them.

    Split out of `_attach_quota` so it can run a second time after the
    path-sensitive backends have been re-asked. Idempotent: a root that
    already carries a figure is left alone.
    """
    for root in run.roots:
        if not getattr(root, "path", ""):
            continue
        if root.quota is not None and root.inode_quota is not None:
            continue
        present = getattr(root, "present", None)
        if present is not None and present.category == VerdictCategory.NOT_PROBED:
            # **Nothing is placed on a directory nobody probed.** Placement
            # reads ownership and write access, and an unprobed root has
            # neither, so every rule below falls through to the junction: on a
            # meadow2 login node whose run ran out of time, `/project` showed
            # `851M of 30G`, which is a home quota from another cluster, and
            # `/home` showed the reader's own home figure. Unknown is the
            # truthful state for a root the run never reached.
            continue
        snap, rows = _governing_snapshot(run.quota_attempts, root)
        if snap is None or not rows:
            continue

        name = getattr(root, "fileset", "") or ""
        key = (getattr(root, "device", "") or "", name)
        scopes = {row.scope for row in rows}
        if scopes and scopes <= {"user"}:
            owner = (
                accessible.get(key) or owned.get(key) or writable_only.get(key) or junction.get(key)
            )
        else:
            owner = junction.get(key)
        # `name` is no longer required here, and dropping it is what makes a
        # filesystem with NO fileset concept work at all. On Lustre every root
        # arrives with `fileset == ""`, so the old guard skipped this whole
        # step and a user-scoped row would have been repeated on `/home` and
        # on `/home/jdoe42` alike. The key is still `(device, fileset)`, which
        # with an empty fileset groups the roots of one filesystem, and that
        # is exactly the set one user-scoped figure describes.
        if owner and owner != root.path:
            # Inside the fileset but not at its junction. Say where the figure
            # lives rather than repeating it here. Kept as data too, so the
            # agent view can follow it instead of answering `?` for a path
            # whose quota is one row up the table.
            root.add_note("quota is a property of fileset %s, reported on %s" % (name, owner))
            root.policy = dict(root.policy or {})
            root.policy["quota_on"] = owner
            continue

        blocks = _prefer_enforced([row for row in rows if row.kind == "blocks"])
        files = _prefer_enforced([row for row in rows if row.kind == "files"])
        # **One row describes ONE scope.** Having chosen a scope for the byte
        # figure, the file count comes from the same scope or not at all.
        # Measured on a Lustre project directory where the file counts carry
        # no limit and the bytes do: bytes came from the project scope and
        # files from the user scope, so the row read `30T of 50T` beside
        # `216k`, which is this reader's file count across the whole
        # filesystem and not the allocation's 8.2M.
        if blocks:
            scoped = [row for row in files if row.scope == blocks[0].scope]
            if scoped:
                files = scoped
        if blocks and root.quota is None:
            root.quota = _single_row_snapshot(snap, blocks[0])
        if files and root.inode_quota is None:
            root.inode_quota = _single_row_snapshot(snap, files[0])


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
    targets = []  # type: List[object]
    for root in shown:
        if root.quota is not None or not root.path:
            continue
        if _worth_walking(root):
            targets.append(root)
        else:
            root.add_note(
                "not added up: it is a shared directory someone else owns, so a walk would "
                "count their files, and its filesystem's own records could not be tied to it"
            )
    # This node's own disks first: they are the cheapest to read and the roots
    # most likely to have no quota system at all, so they are the ones a
    # shortage of time should never cost.
    targets.sort(key=lambda r: 0 if (r.policy or {}).get("node_local") else 1)
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
        owner = os.getuid() if _shared_by_everyone(root.path) else None
        used, files, complete = _walk(root.path, deadline, WALK_ENTRIES, owner=owner)
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
        # THERE and says nothing about a ceiling, so "none" is only written
        # where the mount table has already said no limit exists
        # (`no_quota_enforced`); leaving it unset there made the cell fall
        # back to `?` and undid that answer.
        #
        # **Everywhere else the limit stays unknown.** A root reaches this walk
        # when no backend spoke for it, which is not the same as there being
        # no quota: measured with the quota sweep cut short by `--timeout 1`,
        # `/scratch/meadow3/jdoe42` was walked and shown as `22G of none`
        # while GPFS enforces 100G on it.
        cap = 0 if (root.policy or {}).get("no_quota_enforced") else None
        root.quota = _single_row_snapshot(
            _WalkSource,
            # **No fileset name.** It used to key on `root.path`, which gave
            # each walked root a fileset of its own: `dirscape tree` groups by
            # fileset, so `/tmp` and `/scratch/local/jdoe42`, two mounts of
            # one `/dev/sda1`, became two nodes with conflicting names and the
            # view fell back to `?` for the device. A walk measures a
            # DIRECTORY, not a quota scope, and saying so is the honest shape.
            QuotaRow("", "blocks", "user", used, soft=cap, hard=cap, mount=root.path),
        )
        root.inode_quota = _single_row_snapshot(
            _WalkSource,
            QuotaRow("", "files", "user", files, soft=cap, hard=cap, mount=root.path),
        )
        if owner is not None:
            root.policy["walked_own"] = True
            root.add_note(
                "no quota system exists here and everyone on this node shares the directory, "
                "so these figures count only what you own in it (%s), added up as du "
                "would" % (_files_phrase(files),)
            )
        else:
            root.add_note(
                "no quota system exists here, so these figures were measured by walking the "
                "directory (%s), which is what du does" % (_files_phrase(files),)
            )


def _files_phrase(count):
    # type: (int) -> str
    """`1 file`, `12 files`, `3.1k files`: a count with its noun agreeing."""
    return "%s %s" % (render_fields.human_count(count), "file" if count == 1 else "files")


def _worth_walking(root):
    # type: (object) -> bool
    """Whether adding a directory up could produce the reader's own figure.

    Three kinds qualify: a directory on this node's own disks, one the reader
    owns, and a shared drop like `/tmp`, which is walked for the reader's
    files only. Anything else is somebody else's directory, usually a group
    allocation on a parallel filesystem, and walking it is both slow and
    beside the point: measured on a meadow2 login node, the walk spent its
    whole 2.5s on `/collie3/hpc-staff` and `/cfs3/kestrel-lab`, a 149G GPFS
    fileset and a 155T CephFS tree, finished neither, and left no time for
    `/tmp`.
    """
    if (root.policy or {}).get("node_local"):
        return True
    path = getattr(root, "path", "") or ""
    return _owned_by_caller(path) or _shared_by_everyone(path)


def _shared_by_everyone(path):
    # type: (str) -> bool
    """Whether a directory is a shared drop like `/tmp`: anyone may write in it.

    Walking one of these adds up every user's files, which is neither this
    reader's usage nor, usually, possible. Measured on a Sylvia login node:
    `/tmp` holds 2.0 million entries from every account on the node and `find`
    needs 17.7s to count them, so the bounded walk gave up and the default
    table showed `? ? ?`, while the reader's own share was 159 entries. On the
    GPFS cluster the same walk finished and reported `1.2G`, which was the
    whole node's `/tmp` presented under the heading `used`, a column the
    README promises is YOUR figure.

    World-writable is the test, and the sticky bit is not part of it: this
    development node's `/tmp` is `drwxrwxrwx`, mode 777 with no sticky bit,
    and it is every bit as shared as a 1777 one.
    """
    try:
        mode = os.stat(path).st_mode
    except OSError:
        return False
    return bool(mode & stat_module.S_IWOTH)


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


def _walk(top, deadline, ceiling, owner=None, stop=None, parts=None):
    # type: (str, float, int, Optional[int], Optional[Any], Optional[Dict[str, Tuple[int, int]]]) -> Tuple[int, int, bool]
    """Bytes charged and files counted under ``top``, or as far as time allowed.

    ``stop``, a `threading.Event`, abandons the walk at the next directory,
    which is how the browser drops a walk the reader has moved away from.

    ``parts``, a dict, receives ``(bytes, files)`` for every directory directly
    inside ``top`` whose whole subtree was walked, finished or not: the figures
    the browser shows when the reader opens ``top`` next, for no second walk.
    The walk is depth first, so one subtree is finished before the next is
    begun, and a walk cut short still hands over every subtree it completed.

    Iterative rather than recursive, so a pathological depth cannot blow the
    stack, and `scandir` rather than `walk` so each entry's type comes from
    the directory read instead of a second `stat`. Symlinks are never
    followed: they are counted where they point, by whichever root owns that
    storage, and following them here would double-count a home directory whose
    dotfiles live in `/project`.

    ``owner`` narrows the walk to one uid's files, and to directories that uid
    owns, which is how a shared `/tmp` is measured: see `_shared_by_everyone`.
    Other people's directories are not entered at all, so the cost is the top
    level plus the reader's own subtrees rather than the whole node's.
    """
    total = 0
    files = 0
    seen = 0
    # Each directory to read, with the child of `top` it lies under ("" for
    # `top` itself, and always "" when nobody asked for `parts`).
    stack = [(top, "")]
    # Per child of `top`: [bytes, files, its directories not read yet].
    under_top = {}  # type: Dict[str, List[int]]
    while stack:
        if seen > ceiling or time.time() > deadline or (stop is not None and stop.is_set()):
            return total, files, False
        current, under = stack.pop()
        try:
            entries = list(os.scandir(current))
        except OSError:
            # A directory inside a readable tree that we cannot enter is one
            # subtree missing from the sum, not a failure of the whole walk.
            if under:
                _walked_one(under_top, under, 0, 0, parts)
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
        here_bytes = 0
        here_files = 0
        for entry in entries:
            try:
                if entry.is_dir(follow_symlinks=False):
                    if owner is None or entry.stat(follow_symlinks=False).st_uid == owner:
                        if parts is None:
                            stack.append((entry.path, ""))
                        else:
                            child = under or entry.path
                            stack.append((entry.path, child))
                            under_top.setdefault(child, [0, 0, 0])[2] += 1
                    continue
                stat = entry.stat(follow_symlinks=False)
            except OSError:
                continue
            if owner is not None and stat.st_uid != owner:
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
            here_bytes += blocks if blocks else int(getattr(stat, "st_size", 0))
            here_files += 1
        total += here_bytes
        files += here_files
        if under:
            _walked_one(under_top, under, here_bytes, here_files, parts)
    return total, files, True


def _walked_one(under_top, under, here_bytes, here_files, parts):
    # type: (Dict[str, List[int]], str, int, int, Optional[Dict[str, Tuple[int, int]]]) -> None
    """One directory under ``under`` read: hand the subtree over once it is all read."""
    tally = under_top[under]
    tally[0] += here_bytes
    tally[1] += here_files
    tally[2] -= 1
    if not tally[2] and parts is not None:
        parts[under] = (tally[0], tally[1])


#: Directories a threaded count reads at once on a network filesystem. rapidu's
#: own default, which its author measured on GPFS: 16 threads walked a 1.19M
#: inode tree in 36.6s against 46.9s at 8, and 24 bought 8% on one tree and
#: nothing on another, while being "as much as is polite to ask of a shared
#: node's metanode".
WALK_THREADS = 16

#: How long a stopped threaded count waits for its walkers to put down the
#: directory in hand. One blocked in `scandir` on a hung mount never will, and
#: is left behind as a daemon, as `with_deadline` leaves its probes.
WALK_STOP_GRACE_S = 2.0


class _Tally(object):
    """A count in flight, for the status line: entries read so far.

    rapidu's `Progress` has the same ``inodes``, so the line reads one way
    whichever walk is counting.
    """

    def __init__(self):
        # type: () -> None
        self.inodes = 0


def _walk_threads(top, deadline, ceiling, stop=None, parts=None, threads=WALK_THREADS, tally=None):
    # type: (str, float, int, Optional[Any], Optional[Dict[str, Tuple[int, int]]], int, Optional[_Tally]) -> Tuple[int, int, bool]
    """`_walk` with ``threads`` directories in hand at once, and the same figures.

    **What the browser counts with when rapidu is not installed**, on a network
    filesystem, where every `stat` is a round trip to a metadata server and
    `_walk` waits on each in turn: its first walk of a 15,346-file folder here
    took 10.7s. Sixteen in flight hide sixteen round trips at once. Everything
    else is `_walk`'s, entry for entry: `st_blocks`, falling back to
    `st_size` for a file that has none, directories entered and not charged,
    symlinks never followed, and ``parts`` handed over for each subtree walked
    to the end. On local storage `_walk` is used as it is, since there a
    `stat` costs nothing to hide and threads cost more than they save.

    The walk stops when ``deadline`` passes, ``stop`` is set, or it has read
    more than ``ceiling`` entries, and a directory being read then is checked
    every 1024 entries. ``tally`` counts entries read, for the status line.
    """
    lock = threading.Condition()
    todo = [(top, "")]  # type: List[Tuple[str, str]]
    under_top = {}  # type: Dict[str, List[int]]
    # Everything shared, changed only under `lock`: bytes, files, entries read,
    # walkers holding a directory, and whether the walk was cut short.
    totals = {"bytes": 0, "files": 0, "seen": 0, "active": 0}
    halt = [False]
    finished = threading.Event()

    def read(current, under):
        # type: (str, str) -> Optional[Tuple[int, int, int, List[Tuple[str, str]]]]
        """One directory: its bytes, files, entries and subdirectories, or None if cut."""
        try:
            entries = list(os.scandir(current))
        except OSError:
            return 0, 0, 0, []
        here_bytes = 0
        here_files = 0
        found = []  # type: List[Tuple[str, str]]
        for position, entry in enumerate(entries):
            if not (position & 1023) and position and halt[0]:
                return None
            try:
                if entry.is_dir(follow_symlinks=False):
                    found.append((entry.path, (under or entry.path) if parts is not None else ""))
                    continue
                stat = entry.stat(follow_symlinks=False)
            except OSError:
                continue
            # See `_walk` for why a file with no blocks is charged its length.
            blocks = int(getattr(stat, "st_blocks", 0)) * 512
            here_bytes += blocks if blocks else int(getattr(stat, "st_size", 0))
            here_files += 1
        return here_bytes, here_files, len(entries), found

    def work():
        # type: () -> None
        while True:
            with lock:
                while not todo and totals["active"] and not halt[0]:
                    lock.wait(0.25)
                # Checked before every directory, as `_walk` checks them.
                if time.time() > deadline or (stop is not None and stop.is_set()):
                    halt[0] = True
                if halt[0] or not todo:
                    if not totals["active"]:
                        finished.set()
                    lock.notify_all()
                    return
                current, under = todo.pop()
                totals["active"] += 1
            try:
                got = read(current, under)
            except BaseException:
                got = None
                halt[0] = True
            with lock:
                totals["active"] -= 1
                if got is not None:
                    here_bytes, here_files, seen, found = got
                    totals["bytes"] += here_bytes
                    totals["files"] += here_files
                    totals["seen"] += seen
                    if tally is not None:
                        tally.inodes = totals["seen"]
                    for _path, child in found:
                        if child:
                            under_top.setdefault(child, [0, 0, 0])[2] += 1
                    todo.extend(found)
                    if under:
                        _walked_one(under_top, under, here_bytes, here_files, parts)
                    if totals["seen"] > ceiling:
                        halt[0] = True
                lock.notify_all()

    pool = [threading.Thread(target=work) for _ in range(max(1, int(threads)))]
    for walker in pool:
        walker.daemon = True
        walker.start()
    while not finished.wait(max(0.0, min(0.1, deadline - time.time()))):
        if time.time() > deadline or (stop is not None and stop.is_set()):
            with lock:
                halt[0] = True
                lock.notify_all()
            until = time.time() + WALK_STOP_GRACE_S
            for walker in pool:
                walker.join(max(0.0, until - time.time()))
            break
    with lock:
        complete = not halt[0] and not todo and not totals["active"]
        return totals["bytes"], totals["files"], complete


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

    * a row names the root's own fileset                            -> use it;
    * a row names a mount and a scope, and `<mount>/<scope>` is EXACTLY this
      root's path                                                   -> use it;
    * the root's fileset is known and no row does either            -> nothing;
    * the root's fileset is unknown and a row's mount is EXACTLY the root's
      path, and that row is not really about a directory inside it  -> use it;
    * anything else                                                 -> nothing.

    "Nothing" renders as `?`, which is the honest answer for a path whose
    quota this tool could not attribute.

    **The `<mount>/<scope>` clause is what fills in the question marks**, and
    it needs no configuration at all. A wrapper row says "in the tree mounted
    HERE, the scope called THIS uses that much", and joining the two names
    exactly one directory, which is the whole reason it is safe.

    It fixes two shapes that exact fileset matching could not reach, and both
    were live:

    * **One fileset spelled two ways.** `mmlsattr -L /collie3/hpc-staff` says
      the fileset is `collie3-hpc-staff` while the wrapper prints the same
      allocation as `hpc-staff`, under the heading "Capacity Filesystem:
      project (Collie3 GPFS mounted at /collie3)". The row read `? ? ?` and
      the measuring walk then spent its whole 1.5s deadline failing to add up
      a 149 GiB tree the wrapper had already reported to the byte. An earlier
      fix stripped the site's configured fileset prefixes to match the two
      names up; this rule gets there without being told any prefixes, so that
      one is gone.
    * **No fileset at all.** `mmlsattr` is a GPFS tool, so every non-GPFS
      root arrives with `fileset == ""`, which on a login node is the entire
      cost-effective storage tier. Measured, with the wrapper holding every
      figure the whole time:

          /cfs3/kestrel-lab   read + write   ?   ?   ?     <- what was shown
          mount=/cfs3  scope=kestrel-lab  used=155T  limit=165T  <- what was read

      Owner: "why so many `?`? i told you not to have them. why can't you
      retrieve the numbers?" Six of the eight `?` rows on that screen.

    The bare-mount clause below is kept for the rows where the scope is not a
    directory name: the wrapper prints `scratch/meadow3` as the scope of the
    row whose mount IS `/scratch/meadow3`, so joining the two gives
    `/scratch/meadow3/scratch/meadow3`, which is nothing. `_names_a_subdirectory`
    is what keeps those two cases apart, and it does it with a `stat` because
    the strings cannot.
    """
    rows = snap.rows_for_path(root.path)
    if not rows:
        return []

    target = root.path.rstrip("/") or "/"
    fileset = getattr(root, "fileset", "") or ""
    if fileset:
        named = [row for row in rows if row.fileset and row.fileset == fileset]
        if named:
            return named
        named = _named_by_mount_and_scope(rows, root)
        if named:
            return named
        # **An exact mount match outranks a fileset name that did not
        # agree.** Two filesystems name one allocation two different ways
        # and neither is wrong: `mmlsattr` and `lfs project` both identify
        # the directory, but Lustre's identifier is a NUMBER and the row
        # `lfs quota` prints alongside it carries a name.
        #
        #     root.fileset  = "13579"                  (lfs project -d)
        #     row.fileset   = "lanternlab-exampleu"    (lfs quota -p 13579)
        #     row.mount     = "/lus/egret/projects/lanternlab-exampleu"
        #
        # So the fileset clauses above both miss, and a 29.58T project
        # allocation against a 50T quota fell through to `_device_wide` and
        # rendered `? ? ?`. A row whose mount IS this exact path is about
        # this exact path whatever either side calls it, which is a stronger
        # signal than a name comparison, not a weaker one.
        #
        # **`guessed` is what makes this safe, and leaving it out was a live
        # regression.** A row's mount is sometimes INFERRED from its fileset
        # name rather than measured, and an inferred mount is not evidence of
        # anything. Measured on both sides, which is why the flag is the
        # right discriminator and a path comparison is not:
        #
        #     mmlsquota  fileset='project-hpc'  mount='/project'  guessed=True
        #     lfs quota  fileset='lanternlab-'  mount='/lus/.../lanternlab-exampleu'  guessed=False
        #
        # Without the flag this clause handed `/project` the 11T belonging to
        # `/project/hpc`, which is rapiDU's RD-3 arriving through a door this
        # module had already locked twice.
        exact = [
            row
            for row in rows
            if not row.guessed
            and (row.mount or "").rstrip("/") == target
            and not _names_a_subdirectory(row)
            and _may_carry(row, root)
        ]
        if exact:
            return exact
        return _device_wide(rows, root)

    named = _named_by_mount_and_scope(rows, root)
    if named:
        return named

    exact = [
        row
        for row in rows
        if (row.mount or "").rstrip("/") == target
        and not _names_a_subdirectory(row)
        and _may_carry(row, root)
    ]
    if exact:
        return exact

    # **A user-scoped figure is yours wherever you stand in that filesystem.**
    # Measured on a Lustre cluster, where nothing else connects the two: `lfs
    # quota` reports against the MOUNT, so the row reads `mount=/home
    # scope=user used=35.7G hard=373G`, while the root a reader cares about is
    # `/home/jdoe42`. Lustre has no fileset to match on and no project id is
    # set on a home directory, so every clause above came up empty and a home
    # with 35.7 GB in it rendered `? ? ?` next to a `free` figure for the whole
    # 157T filesystem.
    #
    # **Two conditions, and the second is what keeps this from being prefix
    # inheritance again.**
    #
    # USER scope only: a group or project row describes a shared allocation
    # and belongs at the top of it, which the clauses above already place. A
    # user row describes this reader and nobody else.
    #
    # And the root has to be THEIRS, by ownership or a confirmed write. That
    # is the measurable difference between the two cases, and both are live:
    # `/home/jdoe42` on Lustre is owned by the caller and is where their
    # 35.7 GB belongs, while `/project/anything` under a `/project` row is
    # somebody else's directory and must stay `?` rather than inherit a
    # figure that is true of a different tree. Without the second condition
    # this is exactly the RD-3 leak the fileset clauses above exist to
    # prevent, arriving through the one branch that has no fileset to check.
    #
    # `_attach_quota` still chooses the single root that displays it, so
    # `/home` and `/home/jdoe42` cannot both print the same number.
    if not (_mine(root) and _owned_or_writable(root)):
        return []
    return [row for row in rows if row.scope == "user" and _under(target, row.mount)]


def _mine(root):
    # type: (object) -> bool
    """Whether a write here was confirmed."""
    verdict = getattr(root, "writable", None)
    return verdict is not None and verdict.confirmed


def _filesystem_wide(row):
    # type: (object) -> bool
    """A user or group figure that covers a whole filesystem and no directory.

    Lustre's `lfs quota -u` and `-g` are this shape: one number for the whole
    filesystem, printed against whichever path it was asked about. The backend
    marks them by leaving the fileset empty, which no other backend does.
    """
    return not (getattr(row, "fileset", "") or "") and getattr(row, "scope", "") in (
        "user",
        "group",
    )


def _may_carry(row, root):
    # type: (object, object) -> bool
    """Whether a row asked about exactly this path may be shown on it.

    Always, except for a filesystem-wide figure on a directory that is not the
    reader's. Measured on ACME, where `lfs quota -u` is asked once per Lustre
    mount and so answers "at" each of them: `/lus/grove`, a read-only clone
    holding nothing of this reader's, showed `4.1T used` and `216k` files,
    which is their usage across the whole of Egret, and on Sylvia `/lus/acorn`
    repeated the home directory's `36G of 342G` in a second row because
    `/home` is mounted out of it. `dirscape map` then added the 4.1T to the
    project's 30T and reported 34T across two roots.

    The same figure is right on a directory the reader owns or can write,
    which is where `_rows_governing` puts it, so nothing is lost: it lands on
    `/home/jdoe42` instead of on `/home` and `/lus/acorn`.
    """
    if not _filesystem_wide(row):
        return True
    return _mine(root) and _owned_or_writable(root)


def _owned_or_writable(root):
    # type: (object) -> bool
    """Confirmed writable AND on the reader's own side of the filesystem.

    Ownership is checked as well as the write bit because a group directory
    can be writable by everyone in the group, and a user-scoped figure is not
    theirs collectively. A directory the caller owns outright, or one they
    can write that nobody else owns either, is as close as this gets.
    """
    path = getattr(root, "path", "") or ""
    return _owned_by_caller(path) or _mine(root)


def _under(path, mount):
    # type: (str, Optional[str]) -> bool
    """Whether ``path`` sits inside ``mount``. Both may be the same directory."""
    if not mount:
        return False
    mount = mount.rstrip("/")
    if not mount:
        return True
    return path == mount or path.startswith(mount + "/")


def _named_by_mount_and_scope(rows, root):
    # type: (Sequence[object], object) -> List[object]
    """Rows whose `<mount>/<scope name>` is exactly this root's path.

    A wrapper row says "in the tree mounted HERE, the scope called THIS uses
    that much", and on this site the scope is very often the name of a
    directory one level down. Joining the two identifies one path and no
    other, which is the only reason this is safe to do at all.
    """
    target = (getattr(root, "path", "") or "").rstrip("/")
    if not target:
        return []
    out = []  # type: List[object]
    for row in rows:
        if not row.mount or not row.fileset:
            continue
        if os.path.join(row.mount.rstrip("/"), row.fileset) == target:
            out.append(row)
    return out


def _names_a_subdirectory(row):
    # type: (object) -> bool
    """Whether this row is really about `<mount>/<scope>` rather than `<mount>`.

    Settled by a `stat`, because the strings cannot settle it. Two wrapper
    rows on one login node, identical in shape and opposite in meaning:

        mount=/cfs3             scope=kestrel-lab     -> /cfs3/kestrel-lab exists
        mount=/scratch/meadow3  scope=scratch/meadow3 -> that path does not

    So the first row describes a directory inside the mount and must not also
    be handed to the mount itself, and the second describes the mount and
    must be. Without the test `/cfs3` claimed the `155T of 165T` belonging to
    a different group entirely, which is the RD-3 shape one level up.
    """
    if not row.mount or not row.fileset:
        return False
    joined = os.path.join(row.mount.rstrip("/"), row.fileset)
    if joined.rstrip("/") == row.mount.rstrip("/"):
        return False
    try:
        return os.path.isdir(joined)
    except OSError:
        return False


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
    reachable_paths = set()
    for root in run.roots:
        if root.reach not in (Reach.LISTABLE, Reach.TRAVERSE):
            continue
        if getattr(root, "fileset", ""):
            reachable.add(root.fileset)
        if getattr(root, "path", ""):
            reachable_paths.add(root.path.rstrip("/") or "/")

    held = []  # type: List[str]
    for snap in run.quota_attempts:
        try:
            held.extend(filesets_seen(snap))
        except Exception:
            continue
        # **A fileset whose own row names a reachable directory is reachable**,
        # whatever the directory's fileset is called. Two tools can name one
        # allocation two ways: on ACME `lfs project -d` gives the directory
        # project `13579` and the quota row is filed under the directory's
        # name, so the reader's only project was reported as held with no
        # reachable path while it was the second row of the table. The mount
        # has to be one the backend PUBLISHED; an inferred one proves nothing.
        for row in getattr(snap, "rows", None) or ():
            mount = getattr(row, "mount", None)
            if row.fileset and mount and not row.guessed:
                if (mount.rstrip("/") or "/") in reachable_paths:
                    reachable.add(row.fileset)

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


def sweep(opts, runner=None, save_state=True):
    # type: (argparse.Namespace, Optional[object], bool) -> Run
    """Run every probe once and return the collected facts.

    ``save_state=False`` still reads the lineage, so change labels and the
    rows they keep in the table are the same as a person's run would show,
    and never writes it. That is what an agent's query needs: asking where the
    storage is must not move the baseline `dirscape new` compares against, or
    the person who runs it next is told nothing changed because their agent
    looked five minutes ago.
    """
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

    run.plugins = detect_plugins(runner, list(run.mounts), site=run.site)
    _apply_plugin_defaults(run.site, run.plugins)
    mark("plugins")

    run.identity = read_identity(run.mounts, site=run.site)

    # The allocation listing runs WHILE the quota backends are asked, since
    # neither needs the other and both are commands that mostly wait: on this
    # cluster `accounts storage` took 0.36s and the quota reads 1.28s, one
    # after the other, of a 1.97s start. Its answers and warnings land in the
    # run only after it is joined, so they come out in the same order as
    # before.
    listed = []  # type: List[Any]
    listing_notes = []  # type: List[str]

    def list_allocations():
        # type: () -> None
        for plugin in run.plugins:
            try:
                listed.extend(plugin.allocations(runner, budget) or [])
            except Exception as exc:
                listing_notes.append(
                    "plugin %s could not list allocations: %s" % (plugin.name, exc)
                )

    listing = threading.Thread(target=list_allocations)
    listing.daemon = True
    listing.start()

    # Quota FIRST, because its fileset enumeration is a discovery source, on a
    # budget of its own so it cannot starve the probes. See `QUOTA_SHARE`.
    backends = default_backends(run.site)
    quota_budget = Budget(total_s=budget.remaining * QUOTA_SHARE)
    run.quota_attempts = read_all(backends, runner, run.mounts, quota_budget, ["/"])
    held = []  # type: List[str]
    for snap in run.quota_attempts:
        try:
            held.extend(filesets_seen(snap))
        except Exception:
            continue
    mark("quota")

    # Its commands are held to slices of the run's budget by the runner, so
    # this wait is bounded by it; the grace covers a plugin that blocks
    # outside a command.
    listing.join(budget.remaining + 2.0)
    if listing.is_alive():
        listing_notes.append("the allocation listing did not finish in the time allowed")
    run.allocations.extend(list(listed))
    run.warnings.extend(listing_notes)
    mark("allocations")

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
        unprobed = sum(
            1 for r in run.roots if r.path and r.present.category == VerdictCategory.NOT_PROBED
        )
        # Said out loud. The table that follows is built from what WAS probed
        # and is correct as far as it goes, but a reader who is not told it is
        # partial will take a missing row for a missing directory.
        run.warnings.append(
            "the %gs time allowance ran out before every directory was checked, so %d "
            "went unprobed and are left out; run again, or pass --timeout %d"
            % (budget.total_s, unprobed, int(budget.total_s * 2))
        )
    else:
        run.discovery = confirmed("discovery swept every candidate", source="discover")
    mark("discover")

    attribute_all(run.roots, runner, run.mounts, budget, run.site)
    _label_allocations(run)
    mark("attribute")

    # After attribution, because `_bases_for` picks the filesystem root out of
    # the mount table by DEVICE and the device is set by then, and before the
    # quota sweep, because this costs a directory read per filesystem (0.028s
    # for every snapshot tree on this node) while the quota backends are the
    # part that can actually exhaust the budget.
    find_snapshots(
        run.roots,
        run.mounts,
        budget,
        snapshot_roots=getattr(run.site, "snapshot_roots", ()) or (),
    )
    mark("snapshots")

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
        _record_state(run, opts, save=save_state)
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


def _record_state(run, opts, save=True):
    # type: (Run, argparse.Namespace, bool) -> None
    """Load the lineage, diff against it, then append and save.

    `from_roots` is called before `append`, which is load bearing: the
    snapshot takes `first_seen` from the lineage, so appending first would
    make every root's first sighting the current run and nothing would ever be
    new.

    ``save=False`` stops after the diff. See `sweep`.
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
                run.baseline_since = str(raw_since).strip()
                if older.taken_at > since:
                    # `baseline_for` falls back to the oldest run kept when
                    # none is old enough, and nothing said so: `new --since
                    # 30d` on a lineage begun that afternoon answered "No
                    # change since the last run" about a two hour window.
                    run.warnings.append(
                        "no run is %s old yet, so --since %s compared against the "
                        "oldest one kept" % (run.baseline_since, run.baseline_since)
                    )

    run.snapshot = Snapshot.from_roots(
        run.roots,
        identity=run.identity,
        lineage=run.lineage,
        discovery=run.discovery,
        node_class=getattr(run.identity, "node_class", "") or node_class(),
        cluster_fingerprint=fingerprint,
    )
    run.changes = diff(previous, run.snapshot)
    notes = list(getattr(run.changes, "warnings", []) or [])
    if not save:
        # "seeded one from this run" is a claim about the append below, which
        # a read-only run does not make.
        notes = [note for note in notes if not note.startswith("no baseline yet, seeded")]
    run.warnings.extend(notes)
    # Remembered here, BEFORE the append. Afterwards the lineage's newest entry
    # is this run, so asking the lineage for a baseline returns the run being
    # recorded and the header reads `baseline ?` while a baseline plainly
    # exists.
    if previous is not None:
        run.baseline_at = getattr(previous, "taken_at", None)

    if not save:
        return
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


def _fold_covered_parents(roots):
    # type: (List[object]) -> Tuple[List[object], int]
    """Drop a read-only root whose writable subtree is already on screen.

    The mirror of `_collapse_families`, which folds the other way. That one
    says "if the whole tree is yours, one row says so"; this one says "if the
    tree is NOT yours but part of it is, the part that is, is the answer".
    Measured on a login node, where the omission was loud:

        archive   /cfs3               read only    155T   165T      ?
                  /cfs3/kestrel-lab   read + write    ?      ?       ?
                  /cfs3/hpc-staff     read + write    ?      ?       ?

    Owner: "/cfs3 i only have 2 dirs that i can access and both of them are
    listed but why /cfs3 should be shown here?" The parent is a fileset root
    nobody can write to, and its `155T of 165T` is the whole tier's usage
    across every group on the cluster, not this reader's. Same shape on
    `/cfs4`, `/shared`, `/collie3`, `/home` and `/project`.

    Three things keep it narrow:

    * **Same device only.** `/scratch` is a plain directory holding three
      clusters' filesystems, so a rule folding on path alone would hide two of
      them behind the first. That is the trap `_collapse_families` records and
      it applies identically here.
    * **A descendant, strictly.** The parent is only silent because something
      inside it speaks.
    * **Nothing that carries a delta or a stranded flag is folded**, because
      those are facts about the parent that no child restates.

    A folded row is counted and stays in `--all`, like every other row this
    view holds back. Nothing is discarded.
    """
    paths = [(root, (getattr(root, "path", "") or "").rstrip("/")) for root in roots]

    def writable(root):
        # type: (object) -> bool
        verdict = getattr(root, "writable", None)
        return verdict is not None and verdict.confirmed

    kept = []  # type: List[object]
    folded = 0
    for root, path in paths:
        speaks = getattr(root, "labels", None) or getattr(root, "stranded", False)
        if not path or writable(root) or speaks:
            kept.append(root)
            continue
        device = getattr(root, "device", "")
        covered = any(
            other is not root
            and other_path.startswith(path + "/")
            and getattr(other, "device", "") == device
            and writable(other)
            for other, other_path in paths
        )
        if covered:
            folded += 1
            continue
        kept.append(root)
    return kept, folded


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
    kept, covered = _fold_covered_parents(kept)
    folded += covered
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
    # The LABEL is chrome and the value is content, which is the tier split
    # the table already uses. Both were the terminal's default foreground, so
    # the labels were as bright as the figures they introduce and the view read
    # as a flat block: the eye had nothing to skip. Padded before painting,
    # because the escapes would otherwise be counted as width.
    plain_head = "  %-9s " % (label,)
    head = "  " + style.dim("%-9s" % (label,)) + " " if style is not None else plain_head
    if style is not None and "\033" not in value:
        # A value that carries no escapes of its own is a WORD value (`read +
        # write`, `not published`), and unstyled means the terminal's default
        # foreground: brighter in most themes than the figures two lines
        # above, so the prose outshone the numbers. Cells that arrive
        # pre-painted keep their own tiers.
        value = style.muted(value)
    if render_style.width(plain_head + value) <= room:
        return [head + value]
    lead = render_style.width(plain_head)
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


def _snapshot_phrase(root, now=None):
    # type: (object, Optional[float]) -> str
    """Whether a copy of this path can be read back, in one line.

    Separate from `_keeping_phrase` and deliberately so. That one reports what
    the SITE PUBLISHED about backups; this one reports what was MEASURED on
    the filesystem a moment ago. They can disagree, and when they do the
    disagreement is the useful part: a site that has published nothing can
    still be keeping eleven snapshots you could restore from this afternoon.
    """
    verdict = getattr(root, "recoverable", None)
    copies = list(getattr(root, "snapshots", None) or [])
    if verdict is None or verdict.category == VerdictCategory.NOT_PROBED:
        # Empty, and the caller omits the whole line. A probe that never ran
        # has said nothing, and `? (not probed)` is a row of chrome that
        # teaches a reader the marks on this screen mean nothing. Same rule
        # the allocation line follows two fields up.
        return ""
    if copies:
        newest = copies[0]
        when = render_fields.age_phrase(newest.taken_at, now or time.time())
        if when == render_fields.UNKNOWN:
            when = newest.name
        return "%d %s kept, newest %s" % (
            len(copies),
            "copy" if len(copies) == 1 else "copies",
            when,
        )
    if verdict.refuted:
        return verdict.reason or "none kept"
    return "%s (%s)" % (render_fields.UNKNOWN, verdict.reason or verdict.label)


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
    if _filesystem_wide(row) and device:
        # A Lustre user or group figure: no scope name to give, and the device
        # column is only the path `lfs` was asked about. What it IS matters
        # more, because it is the whole filesystem and not this directory.
        where = "your %s quota on the filesystem at %s" % (row.scope, device)
    elif fileset and device and fileset != device:
        # A Lustre or XFS project is named by a number, and `13579 on ...`
        # reads as a figure rather than as the name of a quota.
        name = (
            "project %s" % (fileset,) if row.scope == "project" and fileset.isdigit() else fileset
        )
        where = "%s on %s" % (name, device)
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


def _resolved(path):
    # type: (str) -> str
    """``realpath`` of an existing path when it differs, else "".

    Under a deadline, because resolving a symlink into a wedged mount blocks
    exactly as a `stat` does, and `why` is often run on the path that is
    misbehaving.
    """
    finished, value, exc, _elapsed = with_deadline(
        lambda: os.path.realpath(path) if os.path.exists(path) else "", DEFAULT_DEADLINE_S
    )
    if not finished or exc is not None or not value:
        return ""
    value = str(value)
    return value if value != path else ""


class _Located(object):
    """Where one typed path landed among the roots, or why it could not land.

    `why`, `why --json` and the agent's `explain_path` all answer "which root
    governs this path", and they must give the same answer, so the search is
    done once, here, and each caller only decides how to say it.
    """

    __slots__ = ("target", "shown", "match", "resolved", "basis", "allocation", "message")

    def __init__(self, target, shown):
        # type: (str, str) -> None
        self.target = target
        self.shown = shown
        # Duck typed, as every root in `Run.roots` is.
        self.match = None  # type: Any
        self.resolved = ""
        self.basis = target
        # True when the typed text was an allocation LOCATION, not a path.
        self.allocation = False
        # Set when no root may answer for the path. Always an EXIT_PATH.
        self.message = ""

    def fail(self, message):
        # type: (str) -> "_Located"
        self.message = message
        return self


def _locate(run, path, allow_missing=False):
    # type: (Run, str, bool) -> _Located
    """The root that governs ``path``, by the rules `why` has always used.

    ``allow_missing`` answers for a path that does not exist YET with the root
    it would be created in, which is the question an agent asks before writing
    output somewhere. `why` keeps it off: a person who typed a path that is
    not there is more likely to have mistyped it than to be planning ahead,
    and is told so.
    """
    target = os.path.abspath(os.path.expanduser(path))
    # Sanitised for DISPLAY only, and matched on the raw value. A path from
    # argv is foreign text: `Root.path` is cleaned at construction but this
    # string never was, so a directory whose name contains a newline forged a
    # table row in this very view and an ESC sequence reached the terminal.
    # That is rapiDU's RD-6 arriving through the one string the model does not
    # own. Measured with a directory literally named
    # "evil\n/project/FORGED  999T  100%\x1b[31m".
    spot = _Located(target, sanitize(target, limit=4096))
    shown = spot.shown
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
                    spot.match = root
                    spot.allocation = True
                    return spot
    # **Where the path really is, when that differs from how it was typed.**
    # ACME documents project space as `/egret/<project>`, and `/egret` is a
    # symlink to `/lus/egret/projects`. Matched as typed, `why
    # /egret/lanternlab-exampleu` walked up to `/`, the overlay the login node
    # boots from, and explained that instead: `read only`, `used ?`, "holds no
    # allocation", about the reader's 30T project. The quota that governs a
    # path is the one where its bytes live, so the resolved path decides.
    resolved = ""
    if match is None:
        resolved = _resolved(target)
        if not resolved and allow_missing and not os.path.exists(target):
            # A path that does not exist yet lives wherever its nearest
            # existing ancestor really is, so `/egret/<project>/new` lands in
            # the Lustre project and not on the `/` the symlink hangs from.
            anchor = _nearest_existing(target)
            real = _resolved(anchor)
            if real:
                resolved = os.path.normpath(os.path.join(real, os.path.relpath(target, anchor)))
        if resolved:
            for root in run.roots:
                if getattr(root, "path", "") == resolved:
                    match = root
                    break
    basis = resolved or target
    if match is None:
        best = ""
        for root in run.roots:
            candidate = getattr(root, "path", "")
            if candidate and basis.startswith(candidate.rstrip("/") + "/"):
                if len(candidate) > len(best):
                    best, match = candidate, root
    spot.match, spot.resolved, spot.basis = match, resolved, basis

    # The directory the mount test is made from. For a path that exists that
    # is the path; for one that does not exist yet it is the nearest ancestor
    # that does, which is where the new path's bytes would land.
    probe = basis
    if allow_missing and match is not None and not os.path.exists(basis):
        probe = _nearest_existing(basis)
    if match is not None and match.path != basis and os.path.exists(probe):
        # An enclosing root on ANOTHER filesystem does not govern this path.
        # `/dev/shm` is its own tmpfs, which discovery leaves out as kernel
        # plumbing, and the walk up from it reached `/` and printed `/`'s
        # figures as the answer. Saying which mount it is on is the truth.
        here = run.mounts.enclosing_mount(probe) if run.mounts is not None else None
        there = run.mounts.enclosing_mount(match.path) if run.mounts is not None else None
        if here is not None and there is not None and here.mountpoint != there.mountpoint:
            if inside_snapshot_tree(here.mountpoint):
                return spot.fail(
                    "%s is inside a read-only snapshot mounted at %s.\n\n"
                    "Copy out of it, never into it: `dirscape recover <path>` lists every\n"
                    "copy of a path and the command to restore one." % (shown, here.mountpoint)
                )
            return spot.fail(
                "%s is on the %s mount at %s, which dirscape does not list as storage,\n"
                "so no root it reports covers this path." % (shown, here.fstype, here.mountpoint)
            )

    if (
        match is not None
        and match.path != target
        and not os.path.exists(target)
        and not allow_missing
    ):
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
        return spot.fail(
            "%s does not exist.\n\n"
            "The enclosing root is %s, if that is what you meant." % (shown, match.path or "?")
        )

    if match is None:
        if not os.path.exists(target):
            return spot.fail("%s does not exist." % (shown,))
        return spot.fail(
            "dirscape found no root at or above %s.\n\n"
            "That is a statement about discovery and not about the path: it\n"
            "exists and may be perfectly readable. Try `dirscape --all`." % (shown,)
        )
    return spot


def _nearest_existing(path):
    # type: (str) -> str
    """The deepest ancestor of ``path`` that exists, which is at worst `/`."""
    current = path
    while current and not os.path.exists(current):
        parent = os.path.dirname(current.rstrip("/")) or "/"
        if parent == current:
            break
        current = parent
    return current or "/"


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
    Which root answers is `_locate`'s decision, shared with `why --json`.
    """
    spot = _locate(run, path)
    if spot.allocation:
        return _why_allocation(spot.match, style, size), EXIT_OK
    if spot.message:
        return spot.message, EXIT_PATH
    match = spot.match
    target, shown, resolved, basis = spot.target, spot.shown, spot.resolved, spot.basis

    room = _room(style, size)
    out = []  # type: List[str]
    out.append(style.head(match.path))
    if resolved and resolved != target:
        out.extend(_prose(style, room, "%s leads here, to %s" % (shown, sanitize(resolved))))
    if match.path != basis:
        out.extend(
            _prose(
                style,
                room,
                "the nearest directory dirscape knows about, above %s" % (sanitize(basis),),
            )
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
    # Measured, and the one line on this screen that decides whether a lost
    # file is recoverable, so it names the literal path rather than a command
    # to go and find it.
    snapshots = _snapshot_phrase(match)
    if snapshots:
        out.extend(_field(style, room, "snapshots", snapshots))
    newest = (getattr(match, "snapshots", None) or [None])[0]
    if newest is not None and _mine(match):
        # Only where the reader can write. On ACME `/soft` keeps NetApp
        # snapshots and is read-only to users, and the line told them to
        # `cp -a` a snapshot over the live software tree, which cannot work
        # and would be a bad idea if it could.
        #
        # And `-n`, so it puts back what is missing without rolling anything
        # back. Without it the line copied the whole snapshot over the live
        # directory: on meadow2 the newest home snapshot is 47 days old, and
        # running it would have replaced every file edited since.
        out.extend(_field(style, room, "restore", _copy_back(newest.path, match.path)))

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
        # Each half only when it was established. This used to be one fixed
        # sentence, and it told a reader that `/soft` on ACME enforces no quota
        # and was too large to add up, when nothing had asked either question:
        # an NFS tree nobody walked, on a server nobody queried.
        enforced_none = bool((match.policy or {}).get("no_quota_enforced"))
        too_big = any("too large to add up" in note for note in match.notes)
        if enforced_none:
            said = "no quota is enforced here, so nothing is counting your usage"
        else:
            said = "no quota system reported a figure for this directory"
        if too_big:
            said += ", and it was too large to add up quickly"
        out.extend(_field(style, room, "note", said + "."))

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


def _recover(run, target, style, size=None, as_json=False):
    # type: (Run, str, object, Optional[int], bool) -> Tuple[str, int]
    """Every readable copy of one path, newest first.

    The only view in this tool that does NOT require its subject to be a
    discovered root, and the only one whose subject is usually gone. Nothing
    here consults `run.roots`: a deleted file has no root, no fileset and no
    quota scope, and refusing to answer until discovery has heard of it would
    make the command useless in exactly the moment it is wanted. The mount
    table is enough.
    """
    path = os.path.abspath(os.path.expanduser(target))
    shown = sanitize(path, limit=4096)
    copies, verdict = copies_for_path(
        path,
        run.mounts,
        snapshot_roots=getattr(run.site, "snapshot_roots", ()) or (),
    )

    if as_json:
        return (
            json.dumps(
                {
                    "path": path,
                    "exists_now": os.path.lexists(path),
                    "recoverable": verdict.to_json(),
                    "copies": [copy.to_json() for copy in copies],
                },
                indent=2,
                sort_keys=True,
            ),
            EXIT_OK,
        )

    room = _room(style, size)
    out = [style.head(shown)]
    if not os.path.lexists(path):
        out.extend(_prose(style, room, "nothing is at this path now"))
    out.append("")

    if not copies:
        out.extend(_prose(style, room, verdict.reason or verdict.label, indent="  "))
        out.append("")
        if verdict.refuted:
            # A durable no about recovery is the single most expensive fact
            # in this tool to get wrong in the reassuring direction, so it is
            # said in the words a person would use.
            out.append(style.bad("  There is no copy of this to restore."))
        else:
            out.append(style.dim('  That is not the same as "there is no backup": ask the site.'))
        return "\n".join(out), EXIT_PATH if verdict.refuted else EXIT_OK

    now = time.time()
    width = max(len(copy.name) for copy in copies)
    out.append(
        "  %d cop%s, newest first. They are READ ONLY: copy out, never edit in place."
        % (len(copies), "y" if len(copies) == 1 else "ies")
    )
    out.append("")
    for copy in copies:
        age = render_fields.age_phrase(copy.taken_at, now)
        out.append(
            "  %s  %s  %s"
            % (style.text(copy.name.ljust(width)), style.dim(age.rjust(12)), copy.path)
        )
    out.append("")
    how, command = _restore_line(path, copies[0].path, copies[0].name)
    out.append(style.dim("  " + how))
    out.append("  " + command)
    return "\n".join(out), EXIT_OK


def _copy_back(snapshot, path):
    # type: (str, str) -> str
    """`cp -an SNAP/. DIR/`: put back what is missing, leaving newer files alone.

    Quoted for the shell, because an agent runs this line rather than reading
    it, and a directory name with a space in it turned one copy into two
    arguments. `shlex.quote` leaves an ordinary path exactly as it was.
    """
    return "cp -an %s %s" % (
        shlex.quote(snapshot.rstrip("/") + "/."),
        shlex.quote(path.rstrip("/") + "/"),
    )


def _restore_line(path, newest, name=""):
    # type: (str, str, str) -> Tuple[str, str]
    """``(what it does, the command)`` to restore ``path`` from ``newest``.

    Shared by `recover` and the agent's `recover_path`, so the command a
    person is shown and the one an agent runs cannot drift apart.

    **No command this returns can overwrite anything: every form is
    `cp -an`.** Only the directory form had `-n`, because `isdir` was the only
    question asked. A file that still existed fell through to `cp -a SNAP
    FILE`, so `recover README.md` printed the line that replaces the live
    README with the older copy, and `recover_path` handed an agent that same
    line to run. A file that is still there is restored BESIDE itself, named
    after the snapshot (``name``), which leaves the reader two files to
    compare instead of choosing for them.
    """
    here = path.rstrip("/") or "/"
    if os.path.isdir(here):
        # The directory is still there. `cp -a SNAP DIR` would nest the copy
        # inside it as DIR/<name>, and a plain `SNAP/. DIR/` would overwrite
        # every file changed since the snapshot; `-n` puts back only what is
        # missing, which is what a partial loss needs.
        if not os.access(here, os.W_OK):
            return _copy_out(here, newest)
        return "put back what is missing, leaving newer files alone, with", _copy_back(newest, here)
    parent = os.path.dirname(here) or "/"
    if os.path.lexists(here):
        if not os.access(parent, os.W_OK):
            return _copy_out(parent, newest)
        beside = "%s.%s" % (here, name or "snapshot")
        return (
            "it is still here, so put the copy beside it rather than over it, with",
            "cp -an %s %s" % (shlex.quote(newest), shlex.quote(beside)),
        )
    # Gone, and after an `rm -rf` of a whole tree its directory is gone too.
    # `os.access` is False for a directory that does not exist, which printed
    # "is read-only to you" about one nobody can see any more, so the nearest
    # directory that does exist decides, and the missing ones are recreated.
    anchor = parent
    while anchor != "/" and not os.path.isdir(anchor):
        anchor = os.path.dirname(anchor) or "/"
    if not os.access(anchor, os.W_OK):
        return _copy_out(anchor, newest)
    command = "cp -an %s %s" % (shlex.quote(newest), shlex.quote(here))
    if anchor != parent:
        return (
            "restore the newest, recreating the directories above it, with",
            "mkdir -p %s && %s" % (shlex.quote(parent), command),
        )
    return "restore the newest with", command


def _copy_out(where, newest):
    # type: (str, str) -> Tuple[str, str]
    """Read-only to this reader (ACME's `/soft`, say), so restoring in place
    cannot work: the useful command copies it out to where they are, and
    `-n` keeps that from replacing a file of the same name there."""
    return (
        "%s is read-only to you, so copy the newest out with" % (where,),
        "cp -an %s ." % (shlex.quote(newest),),
    )


def _paths(run, opts):
    # type: (Run, argparse.Namespace) -> Tuple[str, int]
    """`dirscape paths`: the table's rows for a shell or an agent.

    One path per line by default, because that is what `xargs`, a `for` loop
    and `head -1` take, and `--json` for the records with exact figures. The
    filters were checked in `main` before the sweep, so a typo costs a message
    rather than a five second run; they are parsed again here because this is
    also reachable through `_render` directly.
    """
    from . import agent

    try:
        kinds = agent.parse_kinds(getattr(opts, "kind", None))
        raw = getattr(opts, "min_free", None)
        min_free = agent.parse_size(raw) if raw not in (None, "") else None
    except ValueError as exc:
        return "dirscape: %s" % (exc,), EXIT_USAGE
    payload = agent.paths_payload(
        run,
        show_all=bool(_merge_flag(opts, "all", False)),
        writable=bool(getattr(opts, "writable", False)),
        kinds=kinds,
        min_free=min_free,
    )
    if _merge_flag(opts, "json", False):
        return json.dumps(payload, indent=2), EXIT_OK
    return "\n".join(str(item["path"]) for item in payload["paths"]), EXIT_OK


def _why_json(run, path, changes, caveats):
    # type: (Run, str, Sequence[object], Sequence[object]) -> Tuple[str, int]
    """`why PATH --json`: the native record of the ONE root that answers.

    It emitted every visible root, the whole table, whatever path was named,
    so a script asking about one path had to redo `why`'s search to learn
    which record was the answer, and the view's footer, which sends a reader
    here for "every field", was pointing at the wrong thing.

    The answer is the agent's `explain_path`, so the two machine surfaces
    cannot disagree: `asked` says how the path was matched, `place` is the
    record `dirscape paths --json` gives with the `why -v` layer added, and a
    path that does not exist YET is answered for the place it would be
    created in, with `asked.exists` false. Only the text view refuses such a
    path, because a person who typed it more likely mistyped it.
    """
    from . import agent
    from .render import jsonout

    try:
        root, answer = agent.explain(run, path)
    except agent.PathError as exc:
        payload = jsonout.payload([], meta=run.meta, changes=[], caveats=caveats)
        payload["asked"] = {"path": sanitize(os.path.abspath(os.path.expanduser(path)), 4096)}
        payload["error"] = str(exc)
        return json.dumps(payload, indent=2, sort_keys=True), EXIT_PATH
    about = getattr(root, "path", "")
    mine = [c for c in changes if about and render_fields.change_fields(c)[1] == about]
    payload = jsonout.payload([root], meta=run.meta, changes=mine, caveats=caveats)
    payload["asked"] = {
        "path": answer.get("path") or answer.get("asked"),
        "exists": answer.get("exists"),
        "nearest_existing": answer.get("nearest_existing"),
        "resolves_to": answer.get("resolves_to"),
        "root": answer.get("root"),
        "allocation": "allocation" in answer,
    }
    for key in ("place", "allocation", "quota_from", "quota_note"):
        if key in answer:
            payload[key] = answer[key]
    return json.dumps(payload, indent=2, sort_keys=True), EXIT_OK


def _render(run, opts, command, style, width):
    # type: (Run, argparse.Namespace, str, object, Optional[int]) -> Tuple[str, int]
    show_all = bool(_merge_flag(opts, "all", False))
    roots, hidden = _visible(run, show_all)
    changes = list(run.changes or [])
    legend_on = bool(_merge_flag(opts, "legend", False))
    summary_on = bool(_merge_flag(opts, "summary", False))

    # Ahead of the `--json` branch on purpose: a recovery answer is about one
    # path and its copies, not about a set of roots, so it cannot be squeezed
    # through a renderer whose payload is a root list.
    if command == "recover":
        return _recover(
            run,
            opts.path,
            style,
            size=width,
            as_json=bool(_merge_flag(opts, "json", False)),
        )

    if command == "paths":
        return _paths(run, opts)

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
        elif command == "why":
            return _why_json(run, opts.path, changes, caveats)
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
            head = "No change since %s." % (_baseline_words(run),)
            standing = len(changes) - len(moved)
            if standing:
                return (
                    "%s %d fileset%s still hold space you cannot reach: dirscape stranded%s"
                    % (head, standing, "" if standing == 1 else "s", note),
                    EXIT_OK,
                )
            return (head + note, EXIT_OK)
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
            # Every change is on a root with no row to show: an allocation with
            # no path, or a root that has gone. `subset or roots` fell back to
            # the entire atlas here, which answered "what changed?" with "here
            # is everything", and dropped the grouping and the census with it.
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
                    deltas=True,
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


def _baseline_words(run):
    # type: (Run) -> str
    """The run `new` compared against: ``the last run, at 2026-09-24 09:58 (3m ago)``.

    "No change since the last run" named no time, and under `--since` it was
    not even the last run. The stamp is written the way the change lines
    write it ("where the run at 2026-09-21 10:01 reported"), so one run is not
    dated two ways on one screen.
    """
    at = getattr(run, "baseline_at", None)
    if at is None:
        return "the last run"
    which = "the run" if getattr(run, "baseline_since", None) else "the last run,"
    now = getattr(run.meta, "now", None) or time.time()
    return "%s at %s (%s)" % (
        which,
        time.strftime("%Y-%m-%d %H:%M", time.localtime(at)),
        render_fields.age_phrase(at, now),
    )


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

#: How long reading the listed directory itself may take. Measured on
#: `/scratch/midway3`: 13,914 entries in 0.07s, so a healthy directory is far
#: inside this, and the only thing that reaches it is a mount that has hung.
LIST_READ_S = 3.0

#: The allowance for the probes of every child in one listing, together. Each
#: child also gets at most `DEFAULT_DEADLINE_S` of it, so one hung mount costs
#: a second and the rest of the listing still answers.
LIST_PROBES_S = 3.0


def _children(path, limit=CHILD_LIMIT, read_s=LIST_READ_S, probes_s=LIST_PROBES_S):
    # type: (str, int, Optional[float], float) -> Tuple[List[Dict[str, object]], int, bool]
    """The sub-directories of one path, with what a listing can cheaply know.

    **One `scandir` and one access probe per entry, and no walking.** A
    child's size is not here: `_Sizes` adds children up behind the view once
    it is on screen, bounded the way the table's own walk is, because walking
    every subtree first would put a `/project/hpc` holding 11T between the
    reader and the listing. What is here costs milliseconds for a few hundred
    children.

    Files are left out. This is a browser for places to put data, and the
    reader is descending towards a directory; a home with 300 dotfiles in it
    would bury the four directories that matter.

    **Every call that touches a filesystem runs under a deadline**, the
    directory read and each child's probe, on `with_deadline`'s abandoned
    thread. They ran on the UI thread with none, so one hung mount anywhere
    under the listed directory froze the whole browser, which is exactly what
    `discover.access` exists to prevent everywhere else. A child that did not
    answer in time says so in its access cell. The third value is False when
    the directory itself did not.
    """

    def read():
        # type: () -> List[Tuple[str, str, int]]
        found = []  # type: List[Tuple[str, str, int]]
        with os.scandir(path) as entries:
            for entry in entries:
                try:
                    if entry.is_dir(follow_symlinks=False):
                        # The inode number comes with the name, from `getdents`,
                        # and is what a stored size is checked against.
                        found.append((entry.name, entry.path, entry.inode()))
                except OSError:
                    continue
        return sorted(found)

    finished, value, exc, _elapsed = with_deadline(read, read_s)
    if not finished:
        return [], 0, False
    if exc is not None:
        if isinstance(exc, OSError):
            return [], 0, True
        raise exc
    found = list(value) if isinstance(value, list) else []  # type: List[Tuple[str, str, int]]

    out = []  # type: List[Dict[str, object]]
    stop = time.time() + probes_s
    for name, child, ino in found[:limit]:
        left = stop - time.time()
        done, facts, failed, _spent = (
            with_deadline(functools.partial(_child_facts, child), min(DEFAULT_DEADLINE_S, left))
            if left > 0
            else (False, None, None, 0.0)
        )
        if done and failed is None and isinstance(facts, dict):
            out.append(dict(facts, name=name, path=child, ino=ino))
        else:
            out.append(
                {
                    "name": name,
                    "path": child,
                    "ino": ino,
                    "readable": False,
                    "writable": False,
                    "enterable": False,
                    # What the access cell says instead of an answer. A child
                    # past the allowance was never asked, which is not the
                    # same as one that was asked and hung.
                    "unknown": "did not answer" if left > 0 else "not checked",
                }
            )
    return out, max(0, len(found) - limit), True


def _child_facts(child):
    # type: (str) -> Dict[str, object]
    """What a listing knows about one child before adding it up: your access.

    The one-level entry count it used to carry is gone with the `items`
    column. Owner: "it only shows items, a confusing word", and it was: the
    number of names directly inside, which is neither a size nor a file count.
    `used` and `files` replace it, the same two columns the table has.
    """
    return {
        "readable": os.access(child, os.R_OK | os.X_OK),
        "writable": os.access(child, os.W_OK),
        "enterable": os.access(child, os.X_OK),
    }


def _window_top(cursor, count, room, top=None):
    # type: (int, int, int, Optional[int]) -> int
    """Where the visible slice starts. **The band travels; the list holds.**

    The bug this replaces: `first = cursor - room // 2` recomputed from the
    cursor on every repaint, which pins the highlight to the middle of the
    window for ever. Owner, five rows from the end of an 84 item directory:
    "the highlightor isn't at the bottom when scrolling down, it's somewhere
    in the middle." The counter said `64 of 84, 52 above, 10 below`, and ten
    rows the reader could see were below a band that would not move onto them.

    Edge-triggered instead, which is what every list a reader has ever used
    does: the highlight walks down through the rows until it reaches the last
    one on screen, and only then does the list scroll under it. So the bottom
    row is reachable, the top row is reachable, and a short list never scrolls
    at all.

    ``top`` is the caller's remembered position, which is what makes this
    edge-triggered rather than centred: the answer depends on where the window
    already was, and a function handed only the cursor cannot know. Passing
    None asks for a window centred on the cursor, which is the right answer
    for a first paint and for any caller with nothing to remember.
    """
    if room >= count:
        return 0
    if top is None:
        return max(0, min(cursor - room // 2, count - room))
    top = max(0, min(int(top), count - room))
    if cursor < top:
        top = cursor
    elif cursor >= top + room:
        top = cursor - room + 1
    return max(0, min(top, count - room))


#: How long the first pass over an opened directory may spend GLANCING at its
#: children, all of them together: each glance is held to the table's own
#: `WALK_SECONDS` and `WALK_ENTRIES`, so a directory of two hundred small
#: folders is exact in seconds and two hundred large ones cannot delay the
#: second pass. See `_Sizes`.
LIST_SIZING_S = 15.0

#: The shortest glance the first pass gives a folder when it has many to reach.
GLANCE_MIN_S = 0.25

#: And how long one visit to a directory may go on BEGINNING counts, first
#: pass and second; a count already under way runs to its end (see `_Sizes`).
#: The walks run behind the view and stop when the reader leaves, so this
#: bounds the login node's work, not anyone's wait. Half an hour, because one
#: folder here, `/project/rcc/youzhi`, is over 10T in more than 3 million
#: files, and rapidu had not finished it after two and a half minutes.
VISIT_SIZING_S = 1800.0

#: How old a stored size may be and still be the answer, with no count behind
#: the view. Older ones stay on screen too, and are counted again once every
#: folder without a figure has one. An hour, so a folder opened twice in an
#: afternoon costs one count, and one opened the next day is counted afresh.
FRESH_S = 3600.0


def _listed_root(kid, parent, site, known):
    # type: (Dict[str, object], Optional[Root], Any, Dict[str, Root]) -> Root
    """One child of an opened directory, as a `Root` the table's cells render.

    A child the sweep already found and measured IS that root, so it reads
    exactly as it does in the table. Anything else is built from the listing's
    own probes: present and mounted, because the directory above it listed it;
    access from `os.access`; its kind and policy from the same site rules as
    every row of the table.
    """
    path = str(kid["path"])
    found = known.get(path)
    if found is not None and plain(render_fields.used_cell(found)[0]) != render_fields.UNKNOWN:
        return found
    fstype = str(getattr(parent, "fstype", "") or "")
    role = getattr(found, "role", "") or (site.role_for(path, fstype) if site is not None else "")
    root = Root(path, role=role, device=str(getattr(parent, "device", "") or ""), fstype=fstype)
    root.present = confirmed("listed by the directory above it")
    root.mounted = confirmed("under the same mount as the directory above it")
    if kid.get("unknown"):
        root.reach = Reach.UNKNOWN
        root.writable = unknown(VerdictCategory.PROBE_TIMEOUT, str(kid["unknown"]))
        root.policy["unanswered"] = str(kid["unknown"])
        return root
    if kid["readable"]:
        root.reach = Reach.LISTABLE
    elif kid["enterable"]:
        root.reach = Reach.TRAVERSE
    else:
        root.reach = Reach.CLOSED
    if kid["writable"]:
        root.writable = confirmed("os.access reports write", source="os.access")
    else:
        root.writable = refuted(
            VerdictCategory.ACCESS_DENIED,
            "os.access reports no write permission",
            source="os.access",
        )
    return root


def _nearest(path, known):
    # type: (str, Dict[str, Root]) -> Optional[Root]
    """The deepest discovered root at or above ``path``: whose mount it is on."""
    here = path.rstrip("/") or "/"
    while True:
        if here in known:
            return known[here]
        if here == "/":
            return None
        here = os.path.dirname(here) or "/"


def _rapidu_walk():
    # type: () -> Optional[Callable[..., Any]]
    """rapidu's threaded walker, when rapidu is installed beside dirscape, else None."""
    try:
        from rapidu import walk as module  # type: ignore[import-not-found]
    except Exception:
        return None
    return getattr(module, "walk", None)


def _size_of(path, deadline, ceiling, stop, box=None, progress=None, parts=None, threads=1):
    # type: (str, float, int, Any, Optional[List[Any]], Any, Optional[Dict[str, Tuple[int, int]]], int) -> Tuple[int, int, bool]
    """``(bytes, files, finished)`` for one child of an opened directory.

    **rapidu's threaded walk when it is installed, the table's own `_walk`
    when it is not**, so dirscape keeps its promise of no dependencies and a
    reader who has rapidu gets it. Owner: "for the large dirs, it's too slow".
    A cold GPFS tree is slow because every `stat` is a round trip to a metadata
    server, and one thread waits on each in turn: the first walk of a
    15,346-file directory here took 10.7s on `_walk`. rapidu keeps sixteen
    in flight. It is NOT faster on a warm tree, where each `stat` is answered
    from the node's cache and threads cost more than they save: over 30 paired,
    interleaved walks of that directory warm, rapidu took a median 0.37s to
    `_walk`'s 0.16s, 0.43x [95% CI 0.42, 0.43] and slower in every pair. Here the
    walk runs behind the view, so the warm loss is invisible and the cold win
    is the one a reader waits for. Its figures are `du`'s, directory blocks and
    hard links counted once, and `files` counts every entry that is not a
    directory, as `_walk` does. That is rapidu's own `files`: its `symlinks`
    and `specials` are PART of it, not beside it, and adding them in as well
    had a tree of 148,402 files read 148,727.

    ``deadline`` stops it through rapidu's own `stop` event, set by a timer;
    ``box`` holds that event while it runs, so leaving the view stops it too.
    ``progress`` is a rapidu `Progress`, which the status line reads live.
    ``parts`` receives each finished subdirectory's figures, as from `_walk`.
    Without rapidu, ``threads`` above one counts with `_walk_threads`, which
    the caller asks for on a network filesystem; rapidu is handed a count
    only when it is below its own default, which is the half a count set
    aside leaves free.
    """
    walker = _rapidu_walk()
    halt = threading.Event()
    if box is not None:
        box[0] = halt
    if walker is None:
        # The same `halt` in ``box``, so `m` can stop this walk as it stops rapidu's.
        either = _Either(stop, halt)
        try:
            return _count_here(path, deadline, ceiling, either, parts, threads, progress)
        finally:
            if box is not None:
                box[0] = None
    if stop.is_set():
        halt.set()
    # Capped, because a timer that far out overflows the platform's `time_t`.
    timer = threading.Timer(max(0.0, min(deadline - time.time(), 86400.0)), halt.set)
    timer.daemon = True
    timer.start()
    try:
        options = {"depth": 1, "one_file_system": True, "stop": halt}
        if progress is not None:
            options["progress"] = progress
        if 1 < threads < WALK_THREADS:
            # Asked for fewer than its default, beside a count set aside.
            options["threads"] = threads
        res = walker(path, **options)
    except TypeError:
        # A rapidu from before these arguments: the table's own walk instead.
        return _count_here(path, deadline, ceiling, _Either(stop, halt), parts, threads, progress)
    except Exception:
        return 0, 0, False
    finally:
        timer.cancel()
        if box is not None:
            box[0] = None
    if parts is not None:
        _rapidu_parts(res, parts)
    # `partial` and not `complete`: rapidu's `complete` is also False when a
    # subdirectory could not be read, which `_walk` counts as a finished walk
    # of what is readable, and a `?` there would say "too big" about a folder
    # that is merely partly private.
    return (
        int(getattr(res, "size", 0) or 0),
        int(getattr(res, "files", 0) or 0),
        not getattr(res, "partial", True),
    )


class _Either(object):
    """Set when any of its events is: the reader leaving, or `m` wanting the walk."""

    def __init__(self, *events):
        # type: (*Any) -> None
        self.events = events

    def is_set(self):
        # type: () -> bool
        return any(event.is_set() for event in self.events)


def _count_here(path, deadline, ceiling, stop, parts, threads, progress):
    # type: (str, float, int, Any, Optional[Dict[str, Tuple[int, int]]], int, Any) -> Tuple[int, int, bool]
    """dirscape's own count: threaded where asked, else `_walk` exactly."""
    if threads > 1:
        tally = progress if isinstance(progress, _Tally) else None
        return _walk_threads(path, deadline, ceiling, stop, parts, threads, tally)
    return _walk(path, deadline, ceiling, stop=stop, parts=parts)


def _threads_for(root):
    # type: (Root) -> int
    """How many directories a count of ``root`` reads at once without rapidu."""
    network = classify_fstype(getattr(root, "fstype", "") or "") == KIND_NETWORK
    return WALK_THREADS if network else 1


def _rapidu_parts(res, parts):
    # type: (Any, Dict[str, Tuple[int, int]]) -> None
    """Each directory directly under a rapidu walk's root that it walked to the end.

    rapidu keeps these anyway, one `Entry` per child at `depth=1`, and each is
    cumulative: what walking that child alone would count, apart from a hard
    link shared with a sibling, which is charged to whichever the walk met
    first, as `du a b` charges it. `is_finished` is what makes a walk that was
    cut short still worth reading: every child it finished is exact.
    """
    root = getattr(res, "root", "")
    finished = getattr(res, "is_finished", None)
    for entry in list((getattr(res, "dir_agg", None) or {}).values()):
        where = getattr(entry, "path", "")
        if not getattr(entry, "is_dir", False) or os.path.dirname(where) != root:
            continue
        if finished is not None and not finished(entry):
            continue
        parts[where] = (int(entry.size), int(entry.files))


class _Sizes(object):
    """Counts an opened directory's children behind the view, to the end.

    **A figure is shown only when its count has FINISHED.** Until then the
    row reads `…`, and a status line says which folder is being counted and
    how many files it has reached. An earlier version printed what an
    unfinished walk had counted, marked `+`, and the owner's own
    `/project/rcc/youzhi` read `288G+` for a folder holding more than 10T in
    over 3 million files: "this is just laughable shit". A partial count is
    not a size, however it is marked, and sorting and sharing by one made the
    whole table wrong with it.

    **Two passes.** First every child gets a glance, held to the table's own
    `WALK_SECONDS` and `WALK_ENTRIES` and shortened when there are many, so a
    small folder is exact within seconds of opening. Every child the glance
    did not finish is then counted to the end, ONE AT A TIME and never
    restarted, the one the glance found smallest first, so the most rows turn
    exact the soonest. `m` counts the highlighted folder next, interrupting
    the count in flight, which starts over after it. No count is begun once
    the visit has spent `VISIT_SIZING_S`, and whatever was never begun then
    reads `?`, with a line saying so. A count already under way runs to its
    end: cut off at the allowance, the work it had done was thrown away, and a
    folder larger than one visit's allowance, `/project/rcc/pnsinha`'s 17
    million files on a cold node, could never be counted at all.

    **Opening a folder does not throw away the count in flight.** The level
    being left carries on with it behind the view, `aside`, while the folder
    opened is counted with half the threads, so the two together stay within
    rapidu's own ceiling of 24. Coming back, the row it was counting reads `…`
    until it lands. Opening the very folder being counted stops it instead,
    since its folders are then counted where the reader is looking.

    **Nothing is counted twice.** Every walk also hands back the figures of
    the folders directly inside the one it walked (`_walk`, `_rapidu_parts`),
    so opening a folder that was counted shows its children at once, and
    every finished count goes into ``index``, the `SizeIndex` kept between
    runs. A folder opened again shows the figures it had, from the moment it
    is drawn: `/project/rcc` took 22.8 minutes to count the first time and
    reads in full at once after that. A stored figure older than ``fresh_s``
    stays on screen while the folder is counted again, after every folder
    that has no figure at all, and a note says how old the figures are.

    Everything here is keyed by FOLDER, never by row number, because the rows
    re-sort as the figures land (`_Order`) and a row number names a different
    folder after every sort.

    The walks run on one daemon thread, which is also what keeps a hung mount
    from freezing the view: it can stop the counting, never the keys. Every
    change to a `Root` happens on the UI thread, in `refresh`, because the
    renderer iterates a root's policy while it paints, and a second thread
    writing into that dict is how a repaint dies of "dictionary changed size".

    ``cache`` maps a path to ``(bytes, files)`` for counts FINISHED this
    session and outlives this object, so a directory the reader comes back to
    is not counted again. ``inodes`` maps each row's path to the inode number
    the listing read, which is how a stored figure is known to be this
    folder's and not an older one's that had the same name.
    """

    def __init__(
        self,
        roots,  # type: Sequence[Root]
        cache,  # type: Dict[str, Tuple[int, int]]
        per_child_s=WALK_SECONDS,  # type: float
        entries=WALK_ENTRIES,  # type: int
        glance_s=LIST_SIZING_S,  # type: float
        total_s=VISIT_SIZING_S,  # type: float
        index=None,  # type: Optional[SizeIndex]
        inodes=None,  # type: Optional[Dict[str, Optional[int]]]
        fresh_s=FRESH_S,  # type: float
        aside=None,  # type: Optional[_Sizes]
        glanced=None,  # type: Optional[Dict[str, int]]
    ):
        # type: (...) -> None
        self.roots = list(roots)
        self.cache = cache
        self.per_child_s = per_child_s
        self.entries = entries
        self.glance_s = glance_s
        self.total_s = total_s
        self.index = index
        #: The folder the view last painted highlighted, which is what `m` means.
        self.cursor = None  # type: Optional[Root]
        self._inodes = dict(inodes or {})
        self._lock = threading.Lock()
        self._todo = []  # type: List[Root]
        self._full = []  # type: List[Root]
        self._first = []  # type: List[Root]
        # Rows showing a stored figure old enough to be counted again, which
        # they are once every row without a figure has one.
        self._stale = []  # type: List[Root]
        # What the glance counted of each folder it did not finish: the order
        # the second pass takes them in, and never a figure on screen. Shared
        # by every level of one tree, so a level the reader comes back to goes
        # straight to its long counts instead of glancing at them again.
        self._glanced = {} if glanced is None else glanced  # type: Dict[str, int]
        self._gave_up = set()  # type: set
        self._visible = set()  # type: set
        self._running = False
        self._spent = 0.0
        self._ran_out = False
        self._preempted = False
        self._changed = threading.Event()
        self._stop = threading.Event()
        # The stop event of a rapidu walk in flight, so `stop` and `m` can reach it.
        self._halt = [None]  # type: List[Any]
        # The second-pass count in flight, and its live counter, for the status line.
        self._current = None  # type: Optional[Root]
        self._progress = None  # type: Any
        self._applied = set()  # type: set
        # Rows showing a figure from the index, not counted this session:
        # path to when it was counted.
        self._stored = {}  # type: Dict[str, float]
        # The level the reader left with a count still in flight, and the rows
        # here that count is for: they wait for it rather than count again.
        self._aside = aside
        self._awaiting = set()  # type: set
        # Set when the reader leaves with a count in flight: finish that one,
        # begin nothing new. See `set_aside`.
        self._detached = False
        elsewhere = aside.counting()[0] if aside is not None and aside.busy() else None
        now = time.time()
        for root in self.roots:
            if not self._addable(root):
                continue
            if root.path in self.cache:
                self._apply(root, *self.cache[root.path])
                self._applied.add(root.path)
                continue
            kept = (
                index.get(root.path, self._inodes.get(root.path), _filesystem_of(root))
                if index
                else None
            )
            counted_elsewhere = elsewhere is not None and root.path == elsewhere.path
            if kept is not None:
                used, files, counted = kept
                self._apply(root, used, files, counted)
                self._stored[root.path] = counted
                if counted_elsewhere:
                    self._awaiting.add(root.path)
                elif now - counted >= fresh_s:
                    self._stale.append(root)
                continue
            root.policy[render_fields.MEASURING] = True
            if counted_elsewhere:
                self._awaiting.add(root.path)
            elif root.path in self._glanced:
                self._full.append(root)
            else:
                self._todo.append(root)
        # Smallest first, as the second pass takes the rows with no figure.
        self._stale.sort(key=lambda root: self._stored_bytes(root))
        # The glance SHRINKS with the number of folders, so the first pass
        # reaches every row in about `glance_s` however many there are.
        count = len(self._todo)
        if count and glance_s > 0:
            self.per_child_s = max(
                min(GLANCE_MIN_S, per_child_s), min(per_child_s, glance_s / count)
            )
            self.glance_s = max(glance_s, count * self.per_child_s)

    @staticmethod
    def _addable(root):
        # type: (Root) -> bool
        return bool(root.path) and root.reach == Reach.LISTABLE and root.quota is None

    @staticmethod
    def _stored_bytes(root):
        # type: (Root) -> int
        row, _how, _why = render_fields.pick_row(root.quota, root.path, "blocks")
        return int(row.used) if row is not None and row.used is not None else 0

    @staticmethod
    def _apply(root, used, files, counted=None):
        # type: (Root, int, int, Optional[float]) -> None
        """A finished count onto its root. The UI thread only.

        ``counted`` is when a stored figure was counted, which its source
        carries as the reading's age; a count from this session has none.
        """
        source = _WalkSource  # type: Any
        if counted is not None:
            source = _StoredSource(counted)
        root.quota = _single_row_snapshot(
            source, QuotaRow("", "blocks", "user", used, mount=root.path)
        )
        root.inode_quota = _single_row_snapshot(
            source, QuotaRow("", "files", "user", files, mount=root.path)
        )
        root.policy.pop(render_fields.MEASURING, None)
        root.policy.pop("not_added", None)

    def refresh(self):
        # type: () -> None
        """Everything that finished since the last paint, onto its row."""
        requeued = False
        if self._awaiting and not self._aside_busy():
            # The count set aside has ended. Idle is read FIRST, since it puts
            # its figure in the cache before it goes idle: whatever it did not
            # answer, stopped or cut short, is counted here after all, first.
            with self._lock:
                for root in self.roots:
                    if root.path in self._awaiting and root.path not in self.cache:
                        self._first.append(root)
                        requeued = True
                self._awaiting = set()
        for root in self.roots:
            if root.path in self.cache and root.path not in self._applied:
                self._apply(root, *self.cache[root.path])
                self._applied.add(root.path)
                self._stored.pop(root.path, None)
                self._awaiting.discard(root.path)
            elif root.path in self._gave_up and root.policy.get(render_fields.MEASURING):
                root.policy.pop(render_fields.MEASURING, None)
                root.policy["not_added"] = True
        if requeued:
            self.start()
        if self._ran_out:
            with self._lock:
                left = self._todo + self._full
                self._todo, self._full = [], []
            for root in left:
                root.policy.pop(render_fields.MEASURING, None)
                root.policy["not_added"] = True

    def look(self, paths):
        # type: (Sequence[str]) -> None
        """The folders on screen, which get their glance before the rest."""
        with self._lock:
            self._visible = set(paths)

    def start(self):
        # type: () -> None
        with self._lock:
            if self._running or not (self._todo or self._full or self._first or self._stale):
                return
            self._running = True
        worker = threading.Thread(target=self._run)
        worker.daemon = True
        worker.start()

    def stop(self):
        # type: () -> None
        """The reader left: the walk in hand stops at its next directory."""
        self._stop.set()
        halt = self._halt[0]
        if halt is not None:
            halt.set()

    def busy(self):
        # type: () -> bool
        return self._running or (bool(self._awaiting) and self._aside_busy())

    def _aside_busy(self):
        # type: () -> bool
        aside = self._aside
        return aside is not None and aside.busy()

    def set_aside(self, opening=None):
        # type: (Optional[Root]) -> bool
        """The reader is opening ``opening``: keep counting the folder in flight.

        True when there was one and it goes on, behind the view; the caller
        then keeps this object as the next level's ``aside``. False when there
        was none, or it is ``opening`` itself, and everything here stops.
        """
        with self._lock:
            current = self._current
            keep = (
                self._running
                and current is not None
                and (opening is None or current.path != opening.path)
            )
            if keep:
                self._detached = True
        if not keep:
            self.stop()
        return keep

    def counting(self):
        # type: () -> Tuple[Optional[Root], Optional[int]]
        """The folder being counted to the end now, and its files so far, if known."""
        progress = self._progress
        current = self._current
        if current is None and self._awaiting and self._aside_busy():
            return self._aside.counting()  # type: ignore[union-attr]
        inodes = getattr(progress, "inodes", None) if progress is not None else None
        return current, inodes

    def stored(self):
        # type: () -> Optional[Tuple[float, bool]]
        """How old the oldest stored figure on screen is, and whether it is being counted again.

        None when every figure on screen was counted this session.
        """
        stored = dict(self._stored)
        if not stored:
            return None
        with self._lock:
            waiting = [root.path for root in self._stale + self._first]
        current = self._current
        if current is not None:
            waiting.append(current.path)
        again = any(path in stored for path in waiting)
        return max(0.0, time.time() - min(stored.values())), again

    def changed(self):
        # type: () -> bool
        """Whether a figure landed since the last time this was asked.

        A row waiting on the count set aside has landed once its figure is in
        the cache, or has to be counted here once that count has ended.
        """
        if self._changed.is_set():
            self._changed.clear()
            return True
        if self._awaiting:
            if not self._aside_busy():
                return True
            return any(path in self.cache for path in list(self._awaiting))
        return False

    def measure(self, root=None):
        # type: (Optional[Root]) -> bool
        """`m`: count ``root``, the highlighted folder unless told, next.

        A stored figure is counted again, and stays on screen while it is;
        one counted this session is already the answer.
        """
        root = root if root is not None else self.cursor
        if root is None or root.reach != Reach.LISTABLE:
            return False
        if root.path in self._awaiting:
            return True  # being counted now, by the level set aside
        stored = root.path in self._stored
        if root.path in self.cache and not stored:
            return False
        if root.quota is not None and not stored:
            return False  # a root the table measured already
        if root is self._current:
            return True  # being counted now
        with self._lock:
            for queue in (self._todo, self._full, self._stale):
                if root in queue:
                    queue.remove(root)
            if root not in self._first:
                self._first.insert(0, root)
            self._gave_up.discard(root.path)
            halt = self._halt[0] if self._current is not None else None
            if halt is not None:
                self._preempted = True
        if halt is not None:
            halt.set()
        if not stored:
            root.policy[render_fields.MEASURING] = True
        root.policy.pop("not_added", None)
        self.start()
        return True

    def _next(self):
        # type: () -> Tuple[Optional[Root], str]
        """The next folder, and how: `first`, `glance` or `full`. Under the lock."""
        if self._first:
            return self._first.pop(0), "first"
        if self._todo and self._spent < self.glance_s:
            for position, root in enumerate(self._todo):
                if root.path in self._visible:
                    return self._todo.pop(position), "glance"
            return self._todo.pop(0), "glance"
        # The glances are spent: anything never glanced at is counted in full.
        self._full.extend(self._todo)
        self._todo = []
        if self._full and self._spent < self.total_s:
            chosen = min(
                range(len(self._full)), key=lambda i: self._glanced.get(self._full[i].path, 0)
            )
            return self._full.pop(chosen), "full"
        if self._full:
            self._ran_out = True
            return None, ""
        # Every row has a figure: the stored ones that are old are counted again.
        if self._stale and self._spent < self.total_s:
            return self._stale.pop(0), "full"
        return None, ""

    def _run(self):
        # type: () -> None
        try:
            self._work()
        except Exception:
            # A walk that failed in a way `_walk` does not expect costs the
            # figures, never the view: a traceback from this thread would be
            # printed straight across the frame.
            pass
        finally:
            self._current, self._progress = None, None
            # Flagged BEFORE the thread reads as idle, so a reader that sees
            # it idle is guaranteed to see the last figure too.
            self._changed.set()
            with self._lock:
                self._running = False

    def _work(self):
        # type: () -> None
        while not self._stop.is_set() and not self._detached:
            with self._lock:
                root, how = self._next()
            if root is None:
                return
            progress = None
            if how == "glance":
                cap, ceiling = min(self.per_child_s, self.glance_s - self._spent), self.entries
            else:
                # Once begun, to the end: see the class docstring. The reader
                # leaving, or `m`, is what stops it.
                cap, ceiling = float(10**9), 10**15
                progress = _progress_counter()
                self._current, self._progress = root, progress
                self._changed.set()
            started = time.time()
            parts = {}  # type: Dict[str, Tuple[int, int]]
            used, files, complete = _size_of(
                root.path,
                started + cap,
                ceiling,
                self._stop,
                self._halt,
                progress,
                parts,
                self._threads(root),
            )
            self._spent += time.time() - started
            self._current, self._progress = None, None
            # Kept even from a walk that was stopped: each part is a subtree
            # it finished, so it is exact whatever became of the rest.
            self._keep(root, parts, complete and (used, files))
            with self._lock:
                preempted, self._preempted = self._preempted, False
            if self._stop.is_set() and not complete:
                return
            if complete:
                self.cache[root.path] = (used, files)
            elif how == "glance":
                with self._lock:
                    self._glanced[root.path] = used
                    self._full.append(root)
            elif preempted:
                # `m` stopped it for another folder: it is counted again after,
                # and a stored figure back among the stored ones, never as a
                # folder with no figure at all.
                with self._lock:
                    if root.path in self._stored:
                        self._stale.insert(0, root)
                    else:
                        self._full.insert(0, root)
            else:
                self._gave_up.add(root.path)
            self._changed.set()

    def _threads(self, root):
        # type: (Root) -> int
        """`_threads_for`, halved while the count set aside is still running."""
        threads = _threads_for(root)
        if self._aside_busy() and threads > 1:
            return threads // 2
        return threads

    def _keep(self, root, parts, whole):
        # type: (Root, Dict[str, Tuple[int, int]], Any) -> None
        """Remember a walk's figures: this session's cache, then the index.

        Only the parts a listing of ``root`` would show, the first
        `CHILD_LIMIT` by name, which also bounds what one folder of a hundred
        thousand directories can put in either.
        """
        shown = sorted(parts)[:CHILD_LIMIT]
        for path in shown:
            self.cache.setdefault(path, parts[path])
        index = self.index
        if index is None or not (shown or whole):
            return
        now = time.time()
        where = _filesystem_of(root)
        if whole:
            used, files = whole
            ino = self._inodes.get(root.path)
            index.put(root.path, used, files, now, ino=ino, filesystem=where)
        for path in shown:
            used, files = parts[path]
            index.put(path, used, files, now, ino=_inode_of(path), filesystem=where)
        index.flush()


class _StoredSource(object):
    """The `source` of a figure the index kept: a walk, counted at ``counted``.

    `_WalkSource` with an age, so `why` and `--json` say how old the reading
    is, as they do for a quota report read from a cache.
    """

    source = "walk"
    category = VerdictCategory.OK
    reason = "measured by walking the directory, on an earlier visit"
    time_note = ""
    figure_note = ""

    def __init__(self, counted):
        # type: (float) -> None
        self.taken_at = counted
        self.read_at = time.time()


def _filesystem_of(root):
    # type: (Root) -> str
    """The filesystem ``root`` is on, named as every node that mounts it names it, or "".

    What a stored size is tied to (see `SizeIndex`). A network filesystem's
    device is the same string on each client, a Lustre one once the directory
    it mounts is cut off (`lustre_filesystem`), and "" for anything else means
    the figure is this node's only.
    """
    fstype = (getattr(root, "fstype", "") or "").lower()
    device = getattr(root, "device", "") or ""
    if not device or classify_fstype(fstype) != KIND_NETWORK:
        return ""
    if fstype == "lustre":
        device = lustre_filesystem(device)
    return "%s:%s" % (fstype, device)


def _inode_of(path):
    # type: (str) -> Optional[int]
    """The inode number of ``path``, or None: what a stored figure is checked against."""
    try:
        return os.lstat(path).st_ino
    except OSError:
        return None


def _progress_counter():
    # type: () -> Any
    """A rapidu `Progress` for the status line, or dirscape's own `_Tally` without rapidu."""
    if _rapidu_walk() is None:
        return _Tally()
    try:
        from rapidu import walk as module  # type: ignore[import-not-found]

        return module.Progress(getattr(module, "MAX_THREADS", 24))
    except Exception:
        return None


def _size_order(root):
    # type: (Root) -> Tuple[int, int]
    """Where a folder sorts in size order: see `_Order`."""
    if (root.policy or {}).get(render_fields.MEASURING):
        return (0, 0)
    row, _how, _why = render_fields.pick_row(root.quota, root.path, "blocks")
    if row is not None and row.used is not None:
        return (1, -int(row.used))
    return (2, 0)


class _Order(object):
    """The order an opened directory's rows are in: largest first, or by name.

    **Largest first by default.** The question one level down is where the
    space went, and in name order the first screen of a home directory was
    twenty dot-directories at `<1%` with the folder holding a third of it
    forty rows further down. Owner: "the share sort of lost its meaning". `s`
    puts the rows in name order and back again.

    In size order the rows re-sort as figures land, and the highlight follows
    the folder it was on (`follow`), except before the reader has moved at
    all, when it stays on the top row. Folders still being counted come
    FIRST, because a folder the glance could not finish is usually one of the
    largest, and the reader should see which; then the counted ones, largest
    first; then everything with no figure coming. Ties keep name order, since
    the sort is stable.
    """

    def __init__(self, roots, by_size=True):
        # type: (Sequence[Root], bool) -> None
        self.names = list(roots)
        self.by_size = by_size
        #: Whether the highlight has ever left the top row.
        self.moved = False
        self.rows = []  # type: List[Root]
        self.arrange()

    def toggle(self):
        # type: () -> None
        self.by_size = not self.by_size

    def arrange(self):
        # type: () -> List[Root]
        self.rows = sorted(self.names, key=_size_order) if self.by_size else list(self.names)
        return self.rows

    def follow(self, cursor):
        # type: (int) -> int
        """Re-arrange, and say where the highlight goes: with its folder."""
        held = self.rows[cursor] if 0 <= cursor < len(self.rows) else None
        after = self.arrange()
        if held is None or not self.moved:
            return 0
        return after.index(held)


#: How often the count in flight repaints its "entries so far" line.
LIVE_S = 1.0


def _sizing_keys(
    sizes, order=None, reader=None, waiting=None, slice_s=0.2, live_s=LIVE_S, clock=time.time
):
    # type: (_Sizes, Optional[_Order], Optional[Callable[[], str]], Optional[Callable[[float], bool]], float, float, Callable[[], float]) -> Callable[[], str]
    """Keys for an opened directory whose sizes are still arriving.

    A figure landing is a repaint (`REDRAW`), `m` adds up the highlighted
    row in full, and `s` flips the order. While walks are running, input is
    awaited in short slices so a finished walk appears without a keypress;
    once they are done, this blocks exactly as the table does and costs
    nothing while the reader reads.

    **A count in flight repaints every ``live_s``** as well, so its status
    line moves. It repainted only when a figure landed, and one folder counted
    alone lands nothing for minutes, so the line kept the figure it had when the
    count began: the owner's `/project/rcc` read "counting mehta5/: 2.4k entries
    so far" for a folder of 1.36 million entries, which reads as a count that
    has hung.
    """
    read = reader or interactive.read_key
    wait = waiting or interactive.input_waiting
    painted = [clock()]

    def keys():
        # type: () -> str
        key = next_key()
        painted[0] = clock()
        return key

    def next_key():
        # type: () -> str
        while True:
            # Idle is read BEFORE the change flag: the worker raises the flag
            # before it goes idle, so seeing it idle means its last figure is
            # already flagged, and this cannot block with a figure unpainted.
            running = sizes.busy()
            if sizes.changed():
                return interactive.Key.REDRAW
            if running and not wait(slice_s):
                if sizes.counting()[0] is not None and clock() - painted[0] >= live_s:
                    return interactive.Key.REDRAW
                continue
            key = read()
            if key == interactive.Key.MEASURE:
                sizes.measure()
                return interactive.Key.REDRAW
            if key == interactive.Key.SORT and order is not None:
                order.toggle()
                return interactive.Key.REDRAW
            return key

    return keys


def _row_starts(lines, roots, labels=None):
    # type: (Sequence[str], Sequence[Root], Optional[Dict[str, str]]) -> List[int]
    """The rendered line each root's row starts on, in order, or [] if any is missing."""
    flat = [plain(line) for line in lines]
    out = []  # type: List[int]
    position = 0
    for root in roots:
        target = getattr(root, "path", "") or ""
        target = (labels or {}).get(target, target)
        found = next((i for i in range(position, len(flat)) if _whole_path_at(flat[i], target)), -1)
        if found < 0:
            return []
        out.append(found)
        position = found + 1
    return out


def _dir_notes(roots, style, counting=None, entries=None, stored=None):
    # type: (Sequence[Root], Any, Optional[Root], Optional[int], Optional[Tuple[float, bool]]) -> List[str]
    """The count in flight, how old any stored figure is, then each reason for a `?`.

    ``stored`` is `_Sizes.stored`: the age of the oldest figure on screen that
    an earlier visit counted, and whether it is being counted again. Stored
    figures look like any other, so this line is the only place their age is
    said, and it goes when the last of them has been counted again.
    """
    lines = []  # type: List[str]
    if counting is not None:
        name = os.path.basename(counting.path.rstrip("/")) + "/"
        lines.append(
            style.dim(
                "   %s counting %s%s"
                % (
                    style.g.ellipsis,
                    name,
                    ": %s entries so far" % (render_fields.human_count(entries),)
                    if entries
                    else "",
                )
            )
        )
    if stored is not None:
        age, again = stored
        lines.append(
            style.dim(
                "   sizes as counted %s ago%s"
                % (
                    render_fields.human_duration(age),
                    ", counting again behind the view"
                    if again
                    else ": %s counts the highlighted row again" % (style.accent("m"),),
                )
            )
        )
    policies = [getattr(root, "policy", None) or {} for root in roots]
    if any(p.get("not_added") for p in policies):
        lines.append(
            style.dim(
                "   ? not counted in the %d min this directory gets: %s counts the "
                "highlighted row" % (VISIT_SIZING_S // 60, style.accent("m"))
            )
        )
    if any(p.get("unanswered") for p in policies):
        lines.append(style.dim("   ? did not answer in time, which is what a hung mount does"))
    return lines


def _shares(roots, style):
    # type: (Sequence[Root], Any) -> Tuple[Dict[str, object], Optional[str]]
    """Each child's part of what this directory holds, and the note saying of what.

    **Only once every folder is counted.** A share is a part of a whole, and
    while any folder is still being counted the whole is not known: shares
    of what had been counted so far put a 40G folder at 60% of a directory
    holding 10T. Until then every row reads `…` and the note counts the
    folders done. After, a folder that could not be read is simply not part
    of the whole, and the note says how many are.
    """
    sizes = {}  # type: Dict[str, int]
    for root in roots:
        row, _how, _why = render_fields.pick_row(root.quota, root.path, "blocks")
        if row is not None and row.used is not None:
            sizes[root.path] = int(row.used)
    coming = [r for r in roots if (r.policy or {}).get(render_fields.MEASURING)]
    out = {}  # type: Dict[str, object]
    if coming:
        for root in roots:
            known = root.path in sizes or root in coming
            out[root.path] = style.dim(style.g.ellipsis) if known else render_fields.UNKNOWN
        return out, "counting, %d of %d folders done" % (len(roots) - len(coming), len(roots))
    total = sum(sizes.values())
    for root in roots:
        if root.path in sizes:
            out[root.path] = sizes[root.path] / float(total) if total else 0.0
        else:
            out[root.path] = render_fields.UNKNOWN
    if not sizes:
        return out, None
    held = "%d folder%s" % (len(roots), "" if len(roots) == 1 else "s")
    if len(sizes) < len(roots):
        held = "%d of %d folders" % (len(sizes), len(roots))
    return out, "%s in %s" % (render_fields.human_bytes(total), held)


def _dir_frame(
    here,
    roots,
    cursor,
    run,
    style,
    width,
    height=None,
    top=None,
    held=0,
    notes=(),
    by_size=True,
):
    # type: (str, Sequence[Root], int, Run, Any, Optional[int], Optional[int], Optional[int], int, Sequence[str], bool) -> Tuple[List[str], int, int]
    """An opened directory, drawn as the start screen's own table.

    The owner, on the listing this replaces: "what the sub-dirs should show
    should be the exact same structure as what the app shows at the start".
    So it IS that table: `render_atlas` over the children, with the same
    columns, the same cells and the same rule for dropping a column that says
    one thing on every row, headed by the directory instead of by the user.
    `files` is kept even when every row reads the same, because while the
    figures are arriving they all read the same and the column would appear
    under the reader the moment the first one landed.

    What this adds is a WINDOW of it, because a directory can hold two
    hundred children and a block taller than the terminal cannot be repainted
    in place. The rows around the cursor are cut out of the table rendered
    whole, so every column is laid out against every row and nothing shifts
    as the reader moves.

    Returns ``(lines, top, room)``: the framed block, where the visible slice
    starts, and how many rows it holds.
    """
    window = width or style.size
    # Each row names its child, not the whole path: the title says where.
    labels = {r.path: os.path.basename(r.path.rstrip("/")) + "/" for r in roots if r.path}
    shares, note = _shares(roots, style)
    text = render_atlas(
        roots,
        meta=run.meta,
        site=run.site,
        style=style,
        size=max(_MIN_BODY, window - 4),
        all_roots=roots,
        group=True,
        frame=False,
        title=here,
        keep=("files",),
        labels=labels,
        share=shares,
        note=note,
        # One level down every child is the kind of its parent, so the column
        # says one word on every row, broken up by the size order into blanks.
        hide=("kind",),
    )
    lines = text.splitlines()
    keys = [
        "",
        style.dim(
            "   %s move   %s open   %s back   %s %s   %s measure   %s quit"
            % (
                style.accent("up/down"),
                style.accent("enter"),
                style.accent("esc/left"),
                style.accent("s"),
                "by name" if by_size else "by size",
                style.accent("m"),
                style.accent("q"),
            )
        ),
    ]
    starts = _row_starts(lines, roots, labels)
    if not starts:
        # Not one row per root (a layout this did not expect), so no window
        # can be cut out of it: the whole block, which is still correct.
        block = lines + list(notes) + keys
        return panel(block, style=style, size=window, shrink=False).splitlines(), 0, len(roots)
    tall = starts[1] - starts[0] if len(starts) > 1 else 1
    end = starts[-1] + tall
    head, tail = lines[: starts[0]], lines[end:]
    # Two borders, the counter, and the line `select` leaves the cursor on.
    chrome = len(head) + len(tail) + len(notes) + len(keys) + 4
    room = max(1, (int(height) - chrome) // tall) if height else len(roots)
    room = min(room, len(roots))
    first = _window_top(cursor, len(roots), room, top)
    last = first + room
    body = lines[starts[first] : starts[last] if last < len(roots) else end]
    counter = []  # type: List[str]
    if room < len(roots) or held:
        where = "   %d of %d, %d above, %d below" % (
            cursor + 1,
            len(roots),
            first,
            len(roots) - last,
        )
        if held:
            # `_children` stops at `CHILD_LIMIT`, and this line said `1 of 200`
            # about a Sylvia `/tmp` holding 10,401 entries, as if 200 were all.
            where += "; %s more not listed" % (render_fields.human_count(held),)
        counter = [style.dim(where)]
    block = head + body + counter + tail + list(notes) + keys
    # **Highlighted BEFORE framing**, as the table is, so the band sits inside
    # the border instead of inverting the border characters with the row.
    band = len(head) + starts[cursor] - starts[first]
    stop = _share_stop(head)
    if stop is None:
        block = interactive.highlight(block, band)
    else:
        block = _band_before_bar(block, band, stop, style)
    return panel(block, style=style, size=window, shrink=False).splitlines(), first, room


def _share_stop(head):
    # type: (Sequence[str]) -> Optional[int]
    """The column the highlight stops at: just past the share's percent, or None."""
    for line in reversed(head):
        text = plain(line)
        if " share" in text and "path" in text:
            return text.index(" share") + 1 + _SHARE_PERCENT + 1
    return None


def _band_before_bar(block, index, stop, style):
    # type: (Sequence[str], int, int, Any) -> List[str]
    """The highlight over the figures only, with the share bar left as it is.

    Owner: "the highlightor has the problem covering the bar plot". Inverse
    video swaps every cell's colours, and a full block drawn in inverse is a
    block of the BACKGROUND colour, so the band had a dark bar-shaped hole
    punched through it, as long as the share was large. The band ends at the
    same column on every row, one past the percent, which keeps it one even
    shape as it moves, and the bar after it keeps its own colour.
    """
    out = list(block)
    bare = plain(out[index])
    cut, seen = 0, 0
    while cut < len(bare) and seen < stop:
        seen += render_style.width(bare[cut])
        cut += 1
    head, tail = bare[:cut], bare[cut:].strip()
    gap = " " * max(0, stop - render_style.width(head))
    band = interactive.RESET + interactive.INVERSE + head + gap + interactive.RESET
    out[index] = band + (" " + style.info(tail) if tail else "")
    return out


def _empty_frame(here, style, width=None, answered=True):
    # type: (str, Any, Optional[int], bool) -> List[str]
    """A directory with nothing to open in it, or one that did not answer."""
    cols = _window_cols(style) if width is None else max(8, int(width))
    empty = (
        "nothing to open inside this directory"
        if answered
        else "this directory did not answer within %gs, so what is inside it is unknown"
        % (LIST_READ_S,)
    )
    lines = [
        style.head(sanitize(here, limit=4096)),
        style.dim("  " + empty),
        "",
        style.dim("   %s back   %s quit" % (style.accent("esc/left"), style.accent("q"))),
    ]
    return _fit(panel(lines, style=style, size=cols, shrink=False).splitlines(), cols)


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


def _whole_path_at(text, target):
    # type: (str, str) -> bool
    """Whether ``target`` appears in ``text`` as a WHOLE cell, not a substring.

    **A path can be a suffix of another path, and one of this cluster's is.**
    Owner: "when the highlightor is on the gpfs row and when i press the down
    arrow, it will skip /software and jump directly to /cfs. something is
    wrong."

    Nothing was being skipped. `/software` is a substring of
    `/gpfs/meadow2/perf2/software`, which renders one row ABOVE it, so a
    substring search for the cursor's path found the wrong line first and
    repainted the band exactly where it already was. The band appeared not to
    move, the next press moved the cursor to `/cfs/hpc-staff`, and one row of
    an eleven row table was unreachable:

        software   /gpfs/meadow2/perf2/software   read + write   14G   none
                   /software                      read + write     ?      ?   <- unreachable
        archive    /cfs/hpc-staff                 read + write     ?      ?

    A table cell is delimited by whitespace, so requiring whitespace or an
    edge on both sides is exactly the test: `2` precedes the match inside
    `perf2/software` and rejects it, while the real row has a gutter on each
    side. The path column is `atomic` in this renderer and is never
    ellipsised, so a whole match is always available to find.
    """
    if not target:
        return False
    start = 0
    while True:
        at = text.find(target, start)
        if at < 0:
            return False
        before = text[at - 1] if at else " "
        end = at + len(target)
        after = text[end] if end < len(text) else " "
        if before.isspace() and after.isspace():
            return True
        start = at + 1


def _row_line(lines, target):
    # type: (Sequence[str], str) -> int
    """Which rendered line carries this row, or -1.

    Escapes are stripped before matching, because a cell may carry colour and
    the path is then not a plain substring of the line it is printed on.
    """
    for position, line in enumerate(lines):
        if _whole_path_at(plain(line), target):
            return position
    return -1


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
    target = roots[cursor].path or (roots[cursor].policy or {}).get("allocation_location", "")
    position = _row_line(lines, target)
    if position >= 0:
        lines = interactive.highlight(lines, position)
    # `shrink=False` for the same reason `_detail` uses it: every frame in the
    # interactive session is the window wide, so none of them changes size as
    # the reader moves or drills in. The table's content already fills the
    # window, so this only matters on the degraded paths where it does not.
    return panel(lines, style=style, size=window, shrink=False).splitlines()


def _descend(start, style, width, screen=None, run=None, cache=None, index=None):
    # type: (str, object, Optional[int], Optional[interactive.Screen], Optional[Run], Optional[Dict[str, Tuple[int, int]]], Optional[SizeIndex]) -> object
    """Walk down a directory tree, one listing at a time, until the reader leaves.

    A STACK rather than recursion, so depth costs nothing and "back" is a pop.
    The reader can go as deep as the tree goes, which is what was asked for:
    "you can constantly zoom in if there is sub dirs within these sub-dirs."

    Every level is the start screen's own table (`_dir_frame`), with its
    sizes added up behind it (`_Sizes`). ``run`` supplies the roots the sweep
    already measured, which a level shows exactly as the table does, and
    ``cache`` the sizes added up so far this session, so a level the reader
    comes back to is not walked again, and ``index`` the sizes counted on
    earlier runs, so a level opened before is drawn with its figures.

    Returns `QUIT` when the reader wants out of the program entirely, and
    anything else when they have merely come back up past the top of this
    tree, which returns them to the table they opened it from.
    """
    # The count a level carries on with after the reader opened a folder in it,
    # which stops when the reader leaves the tree altogether.
    aside = [None]  # type: List[Optional[_Sizes]]
    try:
        return _levels(start, style, width, screen, run, cache, index, aside, {})
    finally:
        if aside[0] is not None:
            aside[0].stop()


def _levels(start, style, width, screen, run, cache, index, aside, glanced):
    # type: (str, object, Optional[int], Optional[interactive.Screen], Optional[Run], Optional[Dict[str, Tuple[int, int]]], Optional[SizeIndex], List[Optional[_Sizes]], Dict[str, int]) -> object
    """`_descend`'s levels, one listing at a time, sharing ``aside`` and ``glanced``."""
    run = run if run is not None else Run()
    cache = {} if cache is None else cache
    known = {}  # type: Dict[str, Root]
    for found in run.roots or ():
        if getattr(found, "path", ""):
            known[found.path] = found
    stack = [start]
    cursors = {}  # type: Dict[str, str]
    # The last search from each folder, so `/` again takes it up where it was.
    searches = {}  # type: Dict[str, Tuple[Any, str]]
    while stack:
        here = stack[-1]
        rows = interactive.window_rows()
        kids, held, answered = _children(here)
        parent = _nearest(here, known)
        roots = [_listed_root(kid, parent, run.site, known) for kid in kids]
        if not roots:
            # Nothing to open. Show the listing (which says so) and treat any
            # key except `q` as "back", because there is nowhere to go but up.
            lines = _empty_frame(here, style, width, answered)
            outcome = interactive.select(
                lambda i, block=lines: block,
                1,
                keys=_still(),
                initial=0,
                escapable=True,
                openable=False,
                screen=screen,
            )
            if outcome == interactive.Key.QUIT:
                return interactive.Key.QUIT
            stack.pop()
            continue

        sizes = _Sizes(
            roots,
            cache,
            index=index,
            inodes={str(kid["path"]): kid.get("ino") for kid in kids},  # type: ignore[misc]
            aside=aside[0],
            glanced=glanced,
        )
        order = _Order(roots)
        paths = [root.path for root in order.rows]
        initial = paths.index(cursors[here]) if cursors.get(here) in paths else 0
        order.moved = initial > 0
        # The window's own position, remembered ACROSS repaints, so the band
        # travels and the list holds (see `_window_top`): without it every
        # repaint could only centre the band, which is the "highlightor isn't
        # at the bottom" bug.
        viewport = [None]  # type: List[Optional[int]]

        def paint(
            index, where=here, height=rows, seen=viewport, more=held, sizing=sizes, arranged=order
        ):
            # type: (int, str, int, List[Optional[int]], int, _Sizes, _Order) -> List[str]
            if index:
                arranged.moved = True
            sizing.cursor = arranged.rows[index]
            sizing.refresh()
            block, first, room = _dir_frame(
                where,
                arranged.rows,
                index,
                run,
                style,
                width,
                height=height,
                top=seen[0],
                held=more,
                notes=_dir_notes(arranged.rows, style, *sizing.counting(), stored=sizing.stored()),
                by_size=arranged.by_size,
            )
            seen[0] = first
            sizing.look([root.path for root in arranged.rows[first : first + room]])
            return block

        def remap(cursor, sizing=sizes, arranged=order):
            # type: (int, _Sizes, _Order) -> int
            sizing.refresh()
            return arranged.follow(cursor)

        sizes.start()
        choice = None  # type: object
        try:
            choice = interactive.select(
                paint,
                len(roots),
                keys=_sizing_keys(sizes, order),
                initial=initial,
                escapable=True,
                screen=screen,
                remap=remap,
                searchable=True,
            )
        finally:
            # Opening a folder, or searching from here, keeps the count in
            # flight going behind the view if nothing else already is: see
            # `_Sizes`. Any other way out of this level stops it.
            opening = None  # type: Optional[Root]
            leaving = (None, interactive.Key.QUIT, interactive.Key.BACK, interactive.Key.SEARCH)
            if choice not in leaving:
                opening = order.rows[int(choice)]  # type: ignore[call-overload]
            free = aside[0] is None or not aside[0].busy()
            keep = opening is not None or choice == interactive.Key.SEARCH
            if keep and free and sizes.set_aside(opening):
                aside[0] = sizes
            else:
                sizes.stop()
        if choice == interactive.Key.QUIT:
            return interactive.Key.QUIT
        if choice == interactive.Key.BACK:
            stack.pop()
            continue
        if choice == interactive.Key.SEARCH:
            found = _search_from(here, parent, style, width, screen, run, cache, searches, aside[0])
            if found == interactive.Key.QUIT:
                return interactive.Key.QUIT
            if isinstance(found, str):
                stack.append(found)
            continue
        chosen = int(choice)  # type: ignore[arg-type]
        cursors[here] = str(order.rows[chosen].path)
        stack.append(str(order.rows[chosen].path))
    return interactive.Key.BACK


class _FileSizes(object):
    """The size of each file a search has on screen: one `lstat` each, behind the view.

    Only the rows in view are ever asked for, so a search that found a
    hundred thousand files costs forty `stat` calls, not a hundred thousand,
    and a hung mount can hold up a figure but never the keys. A size is
    `_walk`'s: blocks, or the length of a file that has none.
    """

    def __init__(self):
        # type: () -> None
        #: Path to bytes, or None when the file could not be read.
        self.sizes = {}  # type: Dict[str, Optional[int]]
        self._want = []  # type: List[str]
        self._lock = threading.Condition()
        self._stopped = False
        worker = threading.Thread(target=self._run)
        worker.daemon = True
        worker.start()

    def want(self, paths):
        # type: (Sequence[str]) -> None
        """The files on screen now; anything asked for before and gone from view is dropped."""
        with self._lock:
            self._want = [path for path in paths if path not in self.sizes]
            self._lock.notify()

    def busy(self):
        # type: () -> bool
        return bool(self._want)

    def stop(self):
        # type: () -> None
        with self._lock:
            self._stopped = True
            self._lock.notify()

    def _run(self):
        # type: () -> None
        while True:
            with self._lock:
                while not self._want and not self._stopped:
                    self._lock.wait()
                if self._stopped:
                    return
                path = self._want.pop(0)
            try:
                stat = os.lstat(path)
                blocks = int(getattr(stat, "st_blocks", 0)) * 512
                self.sizes[path] = blocks if blocks else int(stat.st_size)
            except OSError:
                self.sizes[path] = None


def _search_frame(
    top, query, matches, found, cursor, first_row, finder, style, width, height, sizes, cache
):
    # type: (str, str, Sequence[Tuple[str, bool]], int, int, Optional[int], Any, Any, Optional[int], Optional[int], Dict[str, Optional[int]], Dict[str, Tuple[int, int]]) -> Tuple[List[str], int, int]
    """The search, drawn as the directory view is: a prompt, the matches, what was read.

    Each match is its path below ``top``, a folder ending in ``/``, cut from
    the LEFT when it is too long, since the end of a path is the name the
    reader was looking for. A file shows its size once `_FileSizes` has it;
    a folder shows the figure this session counted for it, if any.

    Returns ``(lines, first, room)`` as `_dir_frame` does.
    """
    window = width or style.size
    inner = max(_MIN_BODY, window - 4)
    used_w = 8
    path_w = max(10, inner - 3 - 2 - used_w)
    head = _title_lines("search " + top, style, inner)
    if query and not search_module.Query(query).empty:
        head[-1] += style.dim("  %s  %s found" % (style.g.sep, render_fields.human_count(found)))
    lines = head + ["", render_style.rule("", style, inner), ""]
    cursor_mark = style.accent(style.g.caret)
    if query:
        prompt = "   / " + sanitize(query, limit=512) + cursor_mark
    else:
        prompt = "   / " + cursor_mark + style.dim("  a piece of a name, or a pattern like *.nc")
    lines += [prompt, ""]

    def row(path, is_dir):
        # type: (str, bool) -> str
        shown = path + ("/" if is_dir else "")
        if render_style.width(shown) > path_w:
            shown = style.g.ellipsis + shown[-(path_w - 1) :]
        full = os.path.join(top, path)
        if is_dir:
            counted = cache.get(full)
            used = render_fields.human_bytes(counted[0]) if counted else ""
        else:
            size = sizes.get(full, -1)
            used = style.g.ellipsis if size == -1 else render_fields.human_bytes(size)
        used = render_style.pad(used, used_w, "right")
        return "   " + render_style.pad(shown, path_w) + "  " + used

    status = []  # type: List[str]
    if not finder.done:
        status.append(
            style.dim(
                "   %s reading: %s names in %s folders"
                % (
                    style.g.ellipsis,
                    render_fields.human_count(finder.names),
                    render_fields.human_count(finder.folders),
                )
            )
        )
    else:
        spent = (finder.finished_at or time.time()) - finder.began
        read = "   read %s names in %s folders in %s" % (
            render_fields.human_count(finder.names),
            render_fields.human_count(finder.folders),
            render_fields.human_duration(spent),
        )
        if finder.unreadable:
            read += "; %s could not be read" % (render_fields.human_count(finder.unreadable),)
        status.append(style.dim(read))
    if found > len(matches):
        status.append(
            style.dim(
                "   the first %s of them shown: type more to narrow it"
                % (render_fields.human_count(len(matches)),)
            )
        )
    keys = [
        "",
        style.dim(
            "   %s to search   %s move   %s open   %s back"
            % (
                style.accent("type"),
                style.accent("up/down"),
                style.accent("enter"),
                style.accent("esc"),
            )
        ),
    ]
    heading = "   " + render_style.pad("path", path_w) + "  " + "used".rjust(used_w)
    headings = [style.dim(heading), ""]
    chrome = len(lines) + len(headings) + len(status) + len(keys) + 1 + 4
    # Only the rows in the window are drawn: a search can keep twenty
    # thousand matches, and formatting all of them on every repaint is how a
    # view falls behind its own keys.
    count = len(matches)
    room = max(1, int(height) - chrome) if height else max(1, count)
    room = min(room, count) if count else 0
    first = _window_top(cursor, count, room, first_row) if count else 0
    block = list(lines)
    if count:
        block += headings + [row(path, is_dir) for path, is_dir in matches[first : first + room]]
        if room < count:
            block.append(
                style.dim(
                    "   %d of %d, %d above, %d below"
                    % (cursor + 1, count, first, count - first - room)
                )
            )
    elif query and finder.done:
        block.append(style.dim("   nothing under here has that in its name"))
    block += status + keys
    if count:
        block = interactive.highlight(block, len(lines) + len(headings) + cursor - first)
    return panel(block, style=style, size=window, shrink=False).splitlines(), first, room


def _search_view(top, style, width, screen, cache, finder, query=""):
    # type: (str, Any, Optional[int], Any, Dict[str, Tuple[int, int]], Any, str) -> Tuple[object, str]
    """Search everything under ``top`` until the reader opens a match or leaves.

    Returns ``(outcome, query)``: the outcome is the folder to open (a folder
    match, or the folder a file match is in), `BACK` or `QUIT`, and the query
    is what the reader had typed, so the next `/` from here starts from it.
    """
    finder.start()
    if query:
        finder.ask(query)
    sizes = _FileSizes()
    cursor = 0
    viewport = [None]  # type: List[Optional[int]]
    height = interactive.window_rows()
    canvas = screen if screen is not None else interactive.Screen()
    try:
        with interactive.raw_session():
            while True:
                _asked, matches, found = finder.snapshot()
                cursor = max(0, min(cursor, len(matches) - 1)) if matches else 0
                lines, first, room = _search_frame(
                    top, query, matches, found, cursor, viewport[0], finder, style, width,
                    height, sizes.sizes, cache,
                )  # fmt: skip
                viewport[0] = first
                canvas.paint(lines)
                shown = matches[first : first + room]
                sizes.want([os.path.join(top, p) for p, is_dir in shown if not is_dir])
                busy = not finder.done or sizes.busy()
                if not interactive.input_waiting(0.12 if busy else 1.0):
                    continue
                key = interactive.read_text()
                if key == interactive.Key.QUIT:
                    return interactive.Key.QUIT, query
                if key == interactive.Key.BACK:
                    return interactive.Key.BACK, query
                if key == interactive.Key.ENTER:
                    if not matches:
                        continue
                    path, is_dir = matches[cursor]
                    full = os.path.join(top, path)
                    return (full if is_dir else os.path.dirname(full)), query
                if key == interactive.Key.UP:
                    cursor = max(0, cursor - 1)
                elif key == interactive.Key.DOWN:
                    cursor = min(max(0, len(matches) - 1), cursor + 1)
                elif key in (interactive.Key.ERASE, interactive.Key.CLEAR):
                    query = "" if key == interactive.Key.CLEAR else query[:-1]
                    finder.ask(query)
                    cursor, viewport[0] = 0, None
                elif len(key) == 1:
                    query += key
                    finder.ask(query)
                    cursor, viewport[0] = 0, None
    except KeyboardInterrupt:
        return interactive.Key.QUIT, query
    finally:
        sizes.stop()


def _search_from(here, parent, style, width, screen, run, cache, searches, aside):
    # type: (str, Optional[Root], Any, Optional[int], Any, Run, Dict[str, Tuple[int, int]], Dict[str, Tuple[Any, str]], Optional[_Sizes]) -> object
    """`/` from a view of ``here``: search, then the folder to open, `BACK` or `QUIT`.

    The same filesystem rule as a count: sixteen directories read at once on
    a network filesystem, where each read waits on a server, one on a local
    one, where threads cost more than they hide, and half of sixteen while a
    count carries on behind the view, so the two stay within rapidu's 24.
    Mount points below ``here`` are not entered. A search that read its
    whole tree is kept, and `/` from here again answers from it at once.
    """
    kept = searches.get(here)
    finder, query = kept if kept is not None else (None, "")
    if finder is None:
        threads = _threads_for(parent) if parent is not None else 1
        if aside is not None and aside.busy() and threads > 1:
            threads //= 2
        below = here.rstrip("/") + "/"
        skip = [
            mount.mountpoint
            for mount in (run.mounts or ())
            if getattr(mount, "mountpoint", "").startswith(below)
        ]
        finder = search_module.Finder(here, threads=threads, skip=skip)
    outcome, query = _search_view(here, style, width, screen, cache, finder, query)
    if finder.finished_at is not None:
        searches[here] = (finder, query)
    else:
        finder.stop()
        searches.pop(here, None)
    return outcome


def _size_index(run, opts):
    # type: (Run, argparse.Namespace) -> Optional[SizeIndex]
    """The sizes kept from earlier runs on this cluster, or None under `--no-state`."""
    if _merge_flag(opts, "no_state", False):
        return None
    try:
        return SizeIndex.load()
    except Exception:
        return None


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
    # The row the table last painted highlighted, which is what `/` searches.
    painted = [0]

    def frame(cursor):
        # type: (int) -> List[str]
        painted[0] = cursor
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
            "   %s move   %s open   %s search   %s quit"
            % (
                style.accent("up/down"),
                style.accent("enter"),
                style.accent("/"),
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
        screen.erase()
        text, _ = _render(run, opts, "atlas", style, width)
        _write(text)
        return EXIT_OK

    # One screen for the table and every listing opened from it, so each view
    # is drawn over the last instead of after a blank one. See
    # `interactive.Screen`.
    screen = interactive.Screen()

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
    # Sizes added up in any directory opened this session, so going back into
    # one does not walk it a second time, and those kept from earlier runs,
    # read the first time a directory is opened. See `_Sizes`.
    sizes = {}  # type: Dict[str, Tuple[int, int]]
    index = [None]  # type: List[Optional[SizeIndex]]
    # The searches made from the table, kept as `_levels` keeps its own.
    searches = {}  # type: Dict[str, Tuple[Any, str]]
    try:
        while True:
            chosen = interactive.select(
                frame,
                len(roots),
                initial=cursor,
                escapable=False,
                screen=screen,
                searchable=True,
            )
            if chosen in (interactive.Key.QUIT, interactive.Key.BACK):
                return leave()
            if index[0] is None:
                index[0] = _size_index(run, opts)
            if chosen == interactive.Key.SEARCH:
                # Everything under the highlighted row; a match opens its folder.
                cursor = painted[0]
                row = roots[cursor]
                found = _search_from(
                    row.path or "/", row, style, width, screen, run, sizes, searches, None
                )
                if found == interactive.Key.QUIT:
                    return leave()
                if not isinstance(found, str):
                    continue
                target = found
            else:
                cursor = int(chosen)  # type: ignore[arg-type]
                target = roots[cursor].path or "/"
            opened = _descend(
                target,
                style,
                width,
                screen,
                run=run,
                cache=sizes,
                index=index[0],
            )
            if opened == interactive.Key.QUIT:
                return leave()
    except Exception:
        # `main` falls back to the static print, which must not land under a
        # frame left on screen for a next view that is never coming.
        screen.erase()
        raise


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

    if command == "mcp":
        # Before the sweep: the server sweeps per request, on its own cache,
        # and nothing may reach stdout that is not a protocol message.
        from . import mcp

        return mcp.serve(opts)

    if command == "paths":
        # Checked BEFORE the sweep, so `--kind scrach` costs a message and not
        # a five second run that ends in the same message.
        from . import agent

        try:
            agent.parse_kinds(getattr(opts, "kind", None))
            if getattr(opts, "min_free", None) not in (None, ""):
                agent.parse_size(opts.min_free)
        except ValueError as exc:
            sys.stderr.write("dirscape: %s\n" % (exc,))
            return EXIT_USAGE

    ascii_only = bool(_merge_flag(opts, "ascii", False)) or bool(os.environ.get("DIRSCAPE_ASCII"))
    style = resolve_style(
        color=str(_merge_flag(opts, "color", "auto")),
        ascii_only=ascii_only,
        stream=sys.stdout,
    )

    try:
        # The keyword only where it changes something, so a caller that
        # replaces `sweep` with a stand-in keeps working for every other view.
        run = sweep(opts, save_state=False) if looks_only(command) else sweep(opts)
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
        # An agent harness that runs commands in a pty would otherwise get the
        # browser, and nobody is there to press `q`: the command hangs until
        # the harness gives up on it.
        and not agent_driven()
        and interactive.supported()
    ):
        try:
            code = _browse(run, opts, style, width)
        except Exception:
            # Fall through to the static print. A failed browse must leave the
            # user holding the report.
            pass
        else:
            # The warnings too, and this is what the browse used to skip. It
            # returned straight out of here, so in a terminal, the one place a
            # person is reading, a run that ran out of time printed a table of
            # `?` with no word of why: measured on a meadow2 login node, where
            # a slow automount spent the allowance and the only symptom was
            # 48 rows of question marks.
            _report(run, opts, "")
            return code

    text, code = _render(run, opts, command, style, width)
    _write(text)
    _report(run, opts, text)
    if command == "atlas" and not _merge_flag(opts, "json", False) and agent_driven():
        # Said on stderr and only to an agent, so the person's table and
        # anything piped from it are untouched. The printed table rounds every
        # figure and folds rows away, and an agent reading it is working from
        # the lossy copy without knowing a better one exists.
        sys.stderr.write(
            "dirscape: for exact figures an agent can parse, run `dirscape paths --json`\n"
        )
    return code


#: Commands that only LOOK. They still read the lineage, so their rows match
#: the table's, and never write it: see `sweep`.
_LOOK_ONLY = ("paths",)


def looks_only(command, environ=None):
    # type: (str, Optional[Dict[str, str]]) -> bool
    """Whether this run may read the lineage but must not write it.

    `paths` never writes, and under an agent harness nothing does except
    `snapshot`, whose whole job is to record one. Measured before this: an
    agent's `why --json`, `--json`, `new` or `matrix` each moved the baseline,
    so the person who ran `dirscape new` next was told nothing changed because
    their agent had looked five minutes earlier.
    """
    if command in _LOOK_ONLY:
        return True
    return command != "snapshot" and agent_driven(environ)


#: Environment variables an agent harness sets in the shells it runs. The
#: first two are what Claude Code exports (`AI_AGENT` is the cross-vendor
#: convention it follows, `CLAUDECODE` its own); `GEMINI_CLI` is Gemini CLI's.
#: Codex exports `CODEX_THREAD_ID` to every command it runs (read back from a
#: Codex session's own `env`) and `CODEX_SANDBOX_NETWORK_DISABLED` inside its
#: sandbox; opencode sets `OPENCODE=1` in its own environment at startup, so
#: every shell it spawns inherits it. Without these, either one moved the
#: `new` baseline on every look. Not `OPENCODE_API_KEY` or `CODEX_HOME`: a
#: person's own shell profile exports those. `DIRSCAPE_AGENT` is for
#: everything else, and for testing.
AGENT_VARIABLES = (
    "AI_AGENT",
    "CLAUDECODE",
    "GEMINI_CLI",
    "CODEX_THREAD_ID",
    "CODEX_SANDBOX_NETWORK_DISABLED",
    "OPENCODE",
    "DIRSCAPE_AGENT",
)


def agent_driven(environ=None):
    # type: (Optional[Dict[str, str]]) -> bool
    """Whether an agent harness is running this command rather than a person."""
    env = os.environ if environ is None else environ
    return any(str(env.get(name) or "").strip() for name in AGENT_VARIABLES)


def _report(run, opts, text):
    # type: (Run, argparse.Namespace, str) -> None
    """The warnings and `--timing`, on stderr, after whatever view was shown.

    Never silent, and never in the table. See `_surfaceable`. Said once:
    `dirscape new` already folds its warnings into its answer, and the same
    sentence arriving again on stderr straight underneath it was the note
    twice in a row.
    """
    shown = plain(text)
    for note in _surfaceable(run):
        if note not in shown:
            sys.stderr.write("dirscape: %s\n" % (note,))

    if _merge_flag(opts, "timing", False):
        sys.stderr.write("\nstage timings:\n")
        last = 0.0
        for label, elapsed in run.timings:
            sys.stderr.write("  %-14s %6.3fs\n" % (label, elapsed - last))
            last = elapsed
        sys.stderr.write("  %-14s %6.3fs\n" % ("total", last))


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
