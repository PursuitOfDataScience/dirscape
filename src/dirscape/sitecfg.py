"""Site configuration: the layer that keeps this tool cluster-agnostic.

The core of `dirscape` knows nothing about any particular cluster. It knows
about mount tables, group membership, quota backends and POSIX access. Every
fact that is true of one site and false of another lives here, in three layers
that override each other in order:

    1. built-in defaults        no site names, no site paths, heuristics only
    2. /etc/dirscape/site.conf  a sysadmin drops this in once, for everyone
    3. ~/.config/dirscape/...   a user's own additions

Layer 2 is the important one and it is why this file exists. A tool that has to
be edited and re-released to support a new cluster is a tool that supports one
cluster. A site administrator who can describe their layout in twelve lines of
config can deploy it in an afternoon, and `dirscape --site-template` prints a
starting point for them.

**Config format is INI, via `configparser`.** Not TOML: `tomllib` arrived in
3.11 and `tomli` is a third-party dependency, and this package promises to run
on the bare 3.6 interpreter of a login node with no ability to install
anything. JSON is also accepted for machine-generated config, since `json` is
equally stdlib. A sysadmin gets the format they expect and the package keeps its
zero-dependency promise.

Nothing here decides access. Roles, policies and templates are advisory: they
tell the renderer what to call a root and what to warn about. Whether you can
read a directory is settled by `os.access`, never by a config file.
"""

import fnmatch
import json
import os
import re
from typing import Dict, List, Optional, Sequence, Tuple

try:
    import configparser
except ImportError:  # pragma: no cover - Python 2 only, which we do not support
    configparser = None  # type: ignore[assignment]

__all__ = [
    "Site",
    "load_site",
    "SITE_TEMPLATE",
    "config_search_path",
]


# Roles are advisory labels for the renderer. Deliberately a small closed set:
# an open-ended vocabulary would tempt a site into encoding policy in the name.
ROLES = (
    "home",
    "project",
    "scratch",
    "software",
    "dataset",
    "archive",
    "local",
    "other",
)


# Built-in role heuristics. These are PATTERNS ON THE LAST PATH COMPONENTS, not
# site paths, and they are matched case-insensitively. They are wrong sometimes,
# which is exactly why a site can override them, and why nothing that matters
# is decided from them.
_DEFAULT_ROLE_PATTERNS = (
    ("home", ("*/home", "*/home/*", "*/users/*", "*/u/*")),
    ("scratch", ("*scratch*", "*/tmpwork*", "*/flash/*", "*/nobackup*")),
    # /opt is anchored rather than globbed as `*/opt/*`: the loose form also
    # claims a project directory that merely happens to contain a component
    # called "opt", and a mislabelled role is the kind of small wrongness that
    # makes a user distrust the whole table.
    ("software", ("*/software*", "*/apps", "*/apps/*", "*/modules*", "/opt", "/opt/*")),
    ("dataset", ("*databases*", "*datasets*", "*/data/shared*", "*/reference*")),
    ("archive", ("*archive*", "*/tape*", "*/cold*")),
    ("project", ("*/project*", "*/proj*", "*/work*", "*/groups/*", "*/lab/*")),
    ("local", ("/tmp", "/tmp/*", "/var/tmp*", "/dev/shm*", "*/local")),
)


# Filesystem types that carry no quota system worth asking about. Reported as a
# durable "no quota enforced" rather than as a failure.
_NO_QUOTA_FSTYPES = frozenset(["tmpfs", "devtmpfs", "overlay", "squashfs", "iso9660"])


SITE_TEMPLATE = """\
# dirscape site configuration.
#
# Drop this at /etc/dirscape/site.conf and every user on the cluster gets the
# right labels, policies and quota backends with no flags. Nothing here grants
# or denies access: access is measured with os.access() at runtime. This file
# only tells dirscape what to CALL things and what to WARN about.

[site]
# A short name for this cluster. Appears in the header and in the snapshot
# lineage, so a user with accounts on three clusters keeps three baselines.
name =
# Comma-separated. Paths dirscape should never probe, for instance a mount
# known to hang or an automount map you do not want triggered.
ignore =

[roots]
# Path templates to probe, one per line. Placeholders: {user}, {group},
# {cluster}. A {group} template is expanded once per group the user belongs to.
# These ADD to what the mount table already reveals; they do not replace it.
templates =
#   /project/{group}
#   /scratch/{cluster}/{user}

[roles]
# glob = role . Roles: home, project, scratch, software, dataset, archive,
# local, other. Overrides the built-in heuristics, which guess from path names.
#   /project/* = project
#   /scratch/* = scratch

[filesets]
# Comma-separated prefixes to strip from a fileset name to recover the group
# name it belongs to. On a GPFS site whose filesets are named project-<group>
# this is what lets dirscape connect a quota row to a group you are in.
prefixes =
# Comma-separated prefixes a group may carry when it owns a directory, for
# sites where membership of "pi-smith" grants access to the "smith" project.
group_prefixes = pi-

[quota]
# Comma-separated, in preference order. Known backends: wrapper, gpfs, lustre,
# xfs, posix. Leave blank for automatic detection.
order =
# Absolute paths to site quota wrapper scripts, one per line. This exists
# because a wrapper is sometimes a SHELL ALIAS, and a subprocess cannot see a
# shell alias: naming the script directly is the only way to reach it.
wrapper_paths =
# Extra directories to search for backend executables, comma-separated.
# GPFS tools are commonly installed outside PATH, in /usr/lpp/mmfs/bin.
extra_bin_dirs = /usr/lpp/mmfs/bin
# Mount points whose wrapper section is printed in 1000-based steps while the
# wrapper counts 1024-byte blocks, comma-separated. Nothing in the output
# itself says so, so a T there is overstated by 7.4% unless it is listed here.
decimal_suffix_mounts =

[datasets]
# Shared collection directories to list one level deep, comma-separated, so a
# new dataset appearing in a shared area shows up as a new root.
roots =

[snapshots]
# Directories that ARE a snapshot container, comma-separated. Only needed
# where the site publishes snapshots somewhere the filesystem does not: the
# hidden `.snapshots`, `.snapshot`, `.zfs/snapshot` and `.snap` trees inside a
# filesystem are found without any configuration. Each entry may hold the
# snapshots directly (`/snapshots/<SNAP>/home/<user>`) or one directory per
# filesystem above them (`/snapshots/home/<SNAP>/home/<user>`); both shapes
# are recognised, so state the top of the tree and nothing else.
roots =

[policy]
# glob = key=value; key=value . Keys: purge_days, backup, speed, readonly,
# node_class, note. Purely advisory, and shown in the POLICY column.
#   /scratch/* = purge_days=30; backup=no; speed=fast
#   /software  = readonly=yes; backup=yes

[heuristics]
# role = patterns . EXTENDS one built-in heuristic in place, where [roles]
# overrides everything: checked after the home, scratch, software and dataset
# patterns, and case-insensitively, exactly like the built-in words.
#   archive = */vault*

[plugin]
# What a site can only EXECUTE rather than describe. Every key is optional,
# and with no [plugin] section there is no plugin. Lists are comma-separated;
# fileset_groups and roles take left:right pairs.
#   description            = Example Cluster
#   detect_files           = /opt/site/bin/allocs
#   detect_device_prefixes = example
#   bin_dir                = /opt/site/bin
#   allocation_command     = allocs storage
#   fileset_prefixes       = project2-, project-
#   group_prefixes         = pi-
#   fileset_groups         = project-ops:ops-staff
#   wrapper_paths          = /opt/site/bin/quota
#   extra_bin_dirs         = /usr/lpp/mmfs/bin, /opt/site/bin
#   dataset_roots          = /datasets
#   snapshot_roots         = /snapshots
#   roles                  = /work:project, /work/*:project
#   quota_archive          = /var/lib/quota-history
#   quota_archive_latest   = latest-parsable.quota
#   quota_archive_daily    = ^(\\d{8})-gpfs\\.quota$
"""


def config_search_path():
    # type: () -> List[str]
    """Config files in increasing order of precedence.

    `DIRSCAPE_CONFIG` wins outright when set, so a test or a one-off run can
    pin an exact file without touching anything global.
    """
    override = os.environ.get("DIRSCAPE_CONFIG")
    if override:
        return [override]

    paths = []
    for directory in ("/etc/dirscape", "/etc"):
        paths.append(os.path.join(directory, "site.conf"))
        paths.append(os.path.join(directory, "dirscape.conf"))

    xdg = os.environ.get("XDG_CONFIG_HOME") or os.path.expanduser("~/.config")
    paths.append(os.path.join(xdg, "dirscape", "config.conf"))
    paths.append(os.path.join(xdg, "dirscape", "config.json"))
    return paths


def _split_list(raw):
    # type: (Optional[str]) -> List[str]
    """Split a config value on commas AND newlines.

    Both, because a sysadmin writing a list of paths reaches for one per line
    and a sysadmin writing a list of prefixes reaches for commas. Accepting
    only one of the two produces a config that silently does nothing, which is
    the worst failure mode a config file has.
    """
    if not raw:
        return []
    parts = []  # type: List[str]
    for line in raw.replace(",", "\n").splitlines():
        item = line.strip()
        # Allow a commented line inside a multi-line value, which configparser
        # itself does not strip.
        if item and not item.startswith("#"):
            parts.append(item)
    return parts


def _parse_policy_value(raw):
    # type: (str) -> Dict[str, object]
    """Parse `purge_days=30; backup=no; speed=fast` into a dict."""
    out = {}  # type: Dict[str, object]
    for chunk in raw.split(";"):
        if "=" not in chunk:
            continue
        key, _, value = chunk.partition("=")
        key = key.strip().lower()
        value = value.strip()
        if not key:
            continue
        if key == "purge_days":
            try:
                out[key] = int(value)
            except ValueError:
                # A malformed number is dropped with the rest of the entry
                # kept. A config typo should cost one field, not the whole row.
                continue
        elif key in ("backup", "readonly"):
            out[key] = value.lower() in ("1", "yes", "true", "on")
        else:
            out[key] = value
    return out


class Site(object):
    """Everything site-specific, in one advisory object.

    Constructed empty by default, which is the point: with no config at all the
    tool still works from the mount table, group membership and `os.access`,
    and merely labels things less precisely.
    """

    __slots__ = (
        "name",
        "sources",
        "ignore",
        "templates",
        "role_globs",
        "fileset_prefixes",
        "group_prefixes",
        "quota_order",
        "wrapper_paths",
        "extra_bin_dirs",
        "dataset_roots",
        "snapshot_roots",
        "policy_globs",
        "role_heuristics",
        "decimal_suffix_mounts",
        "plugin",
    )

    def __init__(self):
        # type: () -> None
        self.name = ""
        # Which files this config came from, so `dirscape why` can say where a
        # surprising label was decided.
        self.sources = []  # type: List[str]
        self.ignore = []  # type: List[str]
        self.templates = []  # type: List[str]
        self.role_globs = []  # type: List[Tuple[str, str]]
        self.fileset_prefixes = []  # type: List[str]
        # `pi-` by default because the convention is widespread, and it costs
        # nothing where it does not apply: a group prefix that matches no group
        # produces no candidate paths.
        self.group_prefixes = ["pi-"]  # type: List[str]
        self.quota_order = []  # type: List[str]
        self.wrapper_paths = []  # type: List[str]
        self.extra_bin_dirs = ["/usr/lpp/mmfs/bin"]  # type: List[str]
        self.dataset_roots = []  # type: List[str]
        # Absolute paths that ARE a snapshot container, as opposed to the
        # hidden `.snapshots` a filesystem grows inside its own tree. Needed
        # because a site can publish its snapshots somewhere the filesystem
        # does not: this one exposes `/snapshots` on login nodes only, and
        # nothing about `/home` or the mount table leads you to it.
        self.snapshot_roots = []  # type: List[str]
        self.policy_globs = []  # type: List[Tuple[str, Dict[str, object]]]
        # (role, pattern) pairs that EXTEND one built-in heuristic group, where
        # a `[roles]` glob would override everything: a site's own word for
        # archive storage belongs beside `*archive*`, checked in the same
        # place and case-insensitively, not ahead of the home and scratch
        # patterns that would otherwise have claimed a subdirectory first.
        self.role_heuristics = []  # type: List[Tuple[str, str]]
        # Mount points whose wrapper figures use 1000-based steps. See
        # `quota.wrapper.DECIMAL_SUFFIX_NOTE`.
        self.decimal_suffix_mounts = []  # type: List[str]
        # The `[plugin]` section, raw. Read by `plugins.site.ConfiguredSite`,
        # which is how a site's non-declarative knowledge stays out of the
        # package: see that module.
        self.plugin = {}  # type: Dict[str, object]

    # -- lookups ---------------------------------------------------------

    def is_ignored(self, path):
        # type: (str) -> bool
        return any(fnmatch.fnmatch(path, pattern) for pattern in self.ignore)

    def role_for(self, path, fstype=""):
        # type: (str, str) -> str
        """The advisory role for a path. Site config beats the built-ins.

        Site patterns are checked in the order they were written, so a site can
        put its most specific rule first and rely on it. The built-in
        heuristics are only consulted when no site rule matches at all.
        """
        for pattern, role in self.role_globs:
            if fnmatch.fnmatch(path, pattern):
                return role

        if fstype in ("tmpfs", "devtmpfs") or path in ("/tmp", "/var/tmp", "/dev/shm"):
            return "local"

        lowered = path.rstrip("/").lower() or "/"
        for role, patterns in _DEFAULT_ROLE_PATTERNS:
            extra = [p.lower() for r, p in self.role_heuristics if r == role]
            for pattern in tuple(patterns) + tuple(extra):
                if fnmatch.fnmatch(lowered, pattern):
                    return role
        return "other"

    def policy_for(self, path):
        # type: (str) -> Dict[str, object]
        """Merged policy for a path, least specific first.

        Merged rather than first-match so a site can set `backup=yes` for a
        whole filesystem and then override `purge_days` on one subtree without
        restating the rest.
        """
        merged = {}  # type: Dict[str, object]
        for pattern, policy in sorted(self.policy_globs, key=lambda pair: len(pair[0])):
            if fnmatch.fnmatch(path, pattern):
                merged.update(policy)
        return merged

    def quota_expected(self, fstype):
        # type: (str) -> bool
        """Whether a filesystem of this type should have a quota at all.

        Used so a root on tmpfs reports "no quota enforced here" as a durable
        fact rather than as a backend failure. Brook's NFS home is the case
        that matters in the other direction: a 14 GB whole-export with no user
        quota is a real state, and the answer there is still "no quota", not
        "the backend broke".
        """
        return fstype not in _NO_QUOTA_FSTYPES

    def group_aliases(self, group):
        # type: (str) -> List[str]
        """Names a group might appear under in a path or a fileset.

        Bidirectional on purpose. Membership of `pi-smith` can grant access to
        a directory called `smith`, and a fileset called `project-smith` can
        correspond to a group called either. Measured here: `/project/hpc` is
        group-owned by `hpc-staff` and lives in fileset `project-hpc`, so group
        name, directory name and fileset name are three different strings and
        no single transform connects them. This returns the candidates; only
        `os.access` decides which are real.
        """
        names = [group]
        for prefix in self.group_prefixes:
            if group.startswith(prefix):
                stripped = group[len(prefix) :]
                if stripped:
                    names.append(stripped)
            else:
                names.append(prefix + group)
        # De-duplicate while keeping the original first, since callers probe in
        # order and the unmodified name is the likeliest hit.
        seen = set()
        out = []
        for name in names:
            if name not in seen:
                seen.add(name)
                out.append(name)
        return out

    def strip_fileset_prefix(self, fileset):
        # type: (str) -> str
        """Recover the group name a fileset name encodes.

        Returns the fileset unchanged when no prefix matches, which is the
        common case on sites that do not use the convention.
        """
        for prefix in self.fileset_prefixes:
            if prefix and fileset.startswith(prefix):
                return fileset[len(prefix) :]
        return fileset

    def expand_templates(self, user, groups, cluster=""):
        # type: (str, Sequence[str], str) -> List[str]
        """Expand root templates into concrete candidate paths.

        A `{group}` template expands once per group, and a template with no
        placeholder is returned as-is. Nothing is checked for existence here;
        that is the discovery layer's job and it does it with `os.access`.
        """
        out = []  # type: List[str]
        for template in self.templates:
            if "{group}" in template:
                for group in groups:
                    for alias in self.group_aliases(group):
                        out.append(
                            template.replace("{group}", alias)
                            .replace("{user}", user)
                            .replace("{cluster}", cluster)
                        )
            else:
                out.append(template.replace("{user}", user).replace("{cluster}", cluster))
        seen = set()
        deduped = []
        for path in out:
            normalised = os.path.normpath(path)
            if normalised not in seen:
                seen.add(normalised)
                deduped.append(normalised)
        return deduped

    def to_json(self):
        # type: () -> Dict[str, object]
        return {
            "name": self.name,
            "sources": list(self.sources),
            "templates": list(self.templates),
            "fileset_prefixes": list(self.fileset_prefixes),
            "group_prefixes": list(self.group_prefixes),
            "quota_order": list(self.quota_order),
            "wrapper_paths": list(self.wrapper_paths),
            "extra_bin_dirs": list(self.extra_bin_dirs),
            "dataset_roots": list(self.dataset_roots),
            "snapshot_roots": list(self.snapshot_roots),
            "ignore": list(self.ignore),
            "role_heuristics": [list(pair) for pair in self.role_heuristics],
            "decimal_suffix_mounts": list(self.decimal_suffix_mounts),
            "plugin": dict(self.plugin),
        }


# --------------------------------------------------------------------------
# Loading
# --------------------------------------------------------------------------


def _merge_ini(site, text, source):
    # type: (Site, str, str) -> None
    parser = configparser.ConfigParser(
        # Keys are globs and paths; lower-casing them would break every
        # case-sensitive path on a Linux filesystem.
        delimiters=("=",),
        comment_prefixes=("#", ";"),
        interpolation=None,
    )
    parser.optionxform = str  # type: ignore[assignment,method-assign]
    parser.read_string(text, source=source)

    if parser.has_section("site"):
        site.name = parser.get("site", "name", fallback="").strip() or site.name
        site.ignore.extend(_split_list(parser.get("site", "ignore", fallback="")))

    if parser.has_section("roots"):
        site.templates.extend(_split_list(parser.get("roots", "templates", fallback="")))

    if parser.has_section("roles"):
        for pattern, role in parser.items("roles"):
            role = role.strip().lower()
            if role in ROLES:
                site.role_globs.append((pattern.strip(), role))

    if parser.has_section("filesets"):
        site.fileset_prefixes.extend(_split_list(parser.get("filesets", "prefixes", fallback="")))
        extra_groups = _split_list(parser.get("filesets", "group_prefixes", fallback=""))
        if extra_groups:
            # Replace rather than extend: a site that lists its own group
            # prefixes is making a statement about its conventions, and
            # silently keeping our `pi-` default would generate candidate
            # paths its administrator did not ask for.
            site.group_prefixes = extra_groups

    if parser.has_section("heuristics"):
        for role, patterns in parser.items("heuristics"):
            role = role.strip().lower()
            if role in ROLES:
                for pattern in _split_list(patterns):
                    site.role_heuristics.append((role, pattern))

    if parser.has_section("quota"):
        site.quota_order.extend(_split_list(parser.get("quota", "order", fallback="")))
        site.wrapper_paths.extend(_split_list(parser.get("quota", "wrapper_paths", fallback="")))
        site.decimal_suffix_mounts.extend(
            _split_list(parser.get("quota", "decimal_suffix_mounts", fallback=""))
        )
        extra_dirs = _split_list(parser.get("quota", "extra_bin_dirs", fallback=""))
        for directory in extra_dirs:
            if directory not in site.extra_bin_dirs:
                site.extra_bin_dirs.append(directory)

    if parser.has_section("datasets"):
        site.dataset_roots.extend(_split_list(parser.get("datasets", "roots", fallback="")))
    if parser.has_section("snapshots"):
        site.snapshot_roots.extend(_split_list(parser.get("snapshots", "roots", fallback="")))

    if parser.has_section("policy"):
        for pattern, value in parser.items("policy"):
            policy = _parse_policy_value(value)
            if policy:
                site.policy_globs.append((pattern.strip(), policy))

    if parser.has_section("plugin"):
        # Raw strings. The plugin splits the lists itself, because only it
        # knows which keys are lists, which are pairs and which are a regex.
        for key, value in parser.items("plugin"):
            site.plugin[key.strip()] = value.strip()


def _merge_json(site, text, source):
    # type: (Site, str, str) -> None
    payload = json.loads(text)
    if not isinstance(payload, dict):
        raise ValueError("config must be a JSON object")
    site.name = str(payload.get("name") or site.name)
    for key, target in (
        ("ignore", site.ignore),
        ("templates", site.templates),
        ("fileset_prefixes", site.fileset_prefixes),
        ("quota_order", site.quota_order),
        ("wrapper_paths", site.wrapper_paths),
        ("dataset_roots", site.dataset_roots),
        ("snapshot_roots", site.snapshot_roots),
        ("decimal_suffix_mounts", site.decimal_suffix_mounts),
    ):
        value = payload.get(key)
        if isinstance(value, list):
            target.extend(str(item) for item in value)
    groups = payload.get("group_prefixes")
    if isinstance(groups, list) and groups:
        site.group_prefixes = [str(item) for item in groups]
    for directory in payload.get("extra_bin_dirs") or []:
        if str(directory) not in site.extra_bin_dirs:
            site.extra_bin_dirs.append(str(directory))
    roles = payload.get("roles")
    if isinstance(roles, dict):
        for pattern, role in roles.items():
            if str(role).lower() in ROLES:
                site.role_globs.append((str(pattern), str(role).lower()))
    policies = payload.get("policy")
    if isinstance(policies, dict):
        for pattern, policy in policies.items():
            if isinstance(policy, dict):
                site.policy_globs.append((str(pattern), dict(policy)))
    heuristics = payload.get("heuristics")
    if isinstance(heuristics, dict):
        for role, patterns in heuristics.items():
            if str(role).lower() in ROLES and isinstance(patterns, list):
                for pattern in patterns:
                    site.role_heuristics.append((str(role).lower(), str(pattern)))
    plugin = payload.get("plugin")
    if isinstance(plugin, dict):
        site.plugin.update(plugin)


def load_site(paths=None, warn=None):
    # type: (Optional[Sequence[str]], Optional[List[str]]) -> Site
    """Load and merge site config, lowest precedence first.

    A malformed config file is a warning, never an exception. The tool's job on
    an unfamiliar cluster is to tell you what it found; refusing to start
    because somebody left a stray bracket in `/etc` would make a config file a
    single point of failure for a diagnostic tool. Collected warnings go into
    `warn` so `dirscape why` can surface them.
    """
    site = Site()
    if configparser is None:  # pragma: no cover
        return site

    for path in paths if paths is not None else config_search_path():
        if not path or not os.path.isfile(path):
            continue
        try:
            with open(path, "r") as handle:
                text = handle.read()
        except OSError as exc:
            if warn is not None:
                warn.append("could not read %s: %s" % (path, exc))
            continue
        try:
            if path.endswith(".json"):
                _merge_json(site, text, path)
            else:
                _merge_ini(site, text, path)
        except Exception as exc:
            if warn is not None:
                warn.append("ignored malformed config %s: %s" % (path, exc))
            continue
        site.sources.append(path)

    return site


# --------------------------------------------------------------------------
# Cluster naming
# --------------------------------------------------------------------------

# Trailing digits and a node-position suffix, so `meadow3-0200` becomes
# `meadow3`. Derived from the hostname only as a LAST resort, after site config
# and the mount table have both declined to name the cluster: hostnames are the
# least reliable signal there is, which is why nodetop filters GPU nodes on
# GRES rather than on a hostname prefix.
_NODE_SUFFIX = re.compile(r"[-_]?\d+$")

# What a node is FOR, at the end of its name once the number is gone:
# `procyon-login-02` is a login node of `procyon`, not a cluster called
# `procyon-login`, which is what ACME's header read before this existed.
_NODE_ROLE = re.compile(r"[-_]?(?:login|gpu|cpu|compute|node|bigmem|himem)$", re.IGNORECASE)

# A bare device name, the kind GPFS uses (`meadow3_cap`). Only these are worth
# a common prefix: Lustre and NFS devices lead with NIDs and hostnames, so on
# a Lustre-only site the "shared prefix" of two devices was `172.2`.
_BARE_DEVICE = re.compile(r"^[A-Za-z][A-Za-z0-9_.-]*$")

# An HPE Cray xname (`x1234c0s13b0n0`) says where a blade sits and nothing
# about which machine it belongs to.
_XNAME = re.compile(r"^x\d+c\d+s\d+b\d+n\d+$", re.IGNORECASE)


def guess_cluster_name(hostname, mount_devices=()):
    # type: (str, Sequence[str]) -> str
    """A short cluster name for the header and the snapshot lineage.

    Prefers the longest common prefix of the network device names, because a
    device name is a property of the storage fabric and survives being read
    from any node, whereas a hostname changes per node and per login round
    robin.
    """
    names = [d for d in mount_devices if d and _BARE_DEVICE.match(d)]
    if names:
        shared = os.path.commonprefix(sorted(names)).strip("_-")
        # Two characters is not a name, it is a coincidence of alphabetical
        # neighbours.
        if len(shared) >= 3:
            return shared

    labels = [label for label in (hostname or "").split(".") if label]
    if not labels or _XNAME.match(labels[0]):
        return ""
    # The node's number, then what the node is for, and nothing more: a digit
    # left after that belongs to the machine (`meadow3`, `collie3`), which is
    # how `meadow3-0200` once came out as `meadow`.
    name = _NODE_ROLE.sub("", _NODE_SUFFIX.sub("", labels[0]))
    if name:
        return name
    # The short name was nothing BUT a role and a number (`login01`), so the
    # machine is named one label up, as in `login01.frontera.example.edu`.
    return labels[1] if len(labels) > 1 else labels[0]
