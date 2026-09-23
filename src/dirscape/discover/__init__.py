"""Discovery: which storage roots exist here, and what you can do with them.

Five modules, in dependency order:

    mounts      the mount table, parsed and classified
    identity    uid, groups, node class, and the cluster fingerprint
    access      the os.access probes, each under a deadline
    attribute   which quota scope governs a path
    candidates  the five candidate sources, unioned and deduplicated
    recover     read-only snapshot copies of a root, if the filesystem keeps any

The whole package is **O(number of roots)**. Nothing here walks a tree: the
most it ever does is read one level of a directory with `os.scandir`. That
bound is the reason discovery can run unconditionally on every invocation
instead of behind a flag.

Typical use, with the caller owning config and the runner:

    mounts = read_mount_table()
    me = read_identity(mounts, site=site)
    roots = discover(runner, mounts, me, budget, site)
    for root in roots:
        attribute(root, runner, mounts, budget, site)
"""

from .access import (
    DEFAULT_DEADLINE_S,
    ReachResult,
    SymlinkInfo,
    probe_present,
    probe_reach,
    probe_writable,
    symlink_info,
    with_deadline,
)
from .attribute import (
    GPFS_ROOT_FILESET,
    GPFS_TOOL_DIRS,
    XFS_TOOL_DIRS,
    attribute,
    attribute_all,
    attribute_gpfs,
    attribute_lustre,
    attribute_xfs,
    parse_lfs_project,
    parse_mmlsattr,
    parse_xfs_lsproj,
)
from .candidates import (
    ENV_VARS,
    RANK_PRIMARY,
    RANK_SECONDARY,
    SOURCE_ALLOCATION,
    SOURCE_DATASET_ROOT,
    SOURCE_DIR_OWNER,
    SOURCE_ENV,
    SOURCE_GROUP_TEMPLATE,
    SOURCE_LABELS,
    SOURCE_MOUNTS,
    SOURCE_QUOTA_FILESET,
    SOURCE_RESTATEMENTS,
    SOURCE_SNAPSHOT_ROOT,
    discover,
    restates_source,
    role_for_path,
    source_label,
)
from .identity import Identity, cluster_fingerprint, group_names, read_identity, user_name
from .mounts import (
    LOCAL_FSTYPES,
    NETWORK_FSTYPES,
    PSEUDO_FSTYPES,
    Mount,
    MountTable,
    classify_fstype,
    inside_snapshot_tree,
    lustre_filesystem,
    node_class,
    read_mount_table,
    unescape_field,
)
from .recover import (
    SNAPSHOT_CAP,
    SNAPSHOT_DIRS,
    SnapshotIndex,
    copies_for_path,
    find_snapshots,
    parse_snapshot_time,
)

__all__ = [
    # mounts
    "Mount",
    "MountTable",
    "read_mount_table",
    "unescape_field",
    "classify_fstype",
    "inside_snapshot_tree",
    "lustre_filesystem",
    "node_class",
    "NETWORK_FSTYPES",
    "LOCAL_FSTYPES",
    "PSEUDO_FSTYPES",
    # identity
    "Identity",
    "read_identity",
    "cluster_fingerprint",
    "group_names",
    "user_name",
    # access
    "ReachResult",
    "SymlinkInfo",
    "DEFAULT_DEADLINE_S",
    "with_deadline",
    "probe_reach",
    "probe_present",
    "probe_writable",
    "symlink_info",
    # attribute
    "attribute",
    "attribute_all",
    "attribute_gpfs",
    "attribute_lustre",
    "attribute_xfs",
    "parse_mmlsattr",
    "parse_lfs_project",
    "parse_xfs_lsproj",
    "GPFS_TOOL_DIRS",
    "GPFS_ROOT_FILESET",
    "XFS_TOOL_DIRS",
    # candidates
    "discover",
    "role_for_path",
    "ENV_VARS",
    "SOURCE_MOUNTS",
    "SOURCE_ENV",
    "SOURCE_GROUP_TEMPLATE",
    "SOURCE_DIR_OWNER",
    "SOURCE_QUOTA_FILESET",
    "SOURCE_DATASET_ROOT",
    "SOURCE_SNAPSHOT_ROOT",
    "SOURCE_ALLOCATION",
    "SOURCE_LABELS",
    "SOURCE_RESTATEMENTS",
    "source_label",
    # recover
    "copies_for_path",
    "find_snapshots",
    "parse_snapshot_time",
    "SnapshotIndex",
    "SNAPSHOT_DIRS",
    "SNAPSHOT_CAP",
    "restates_source",
    "RANK_PRIMARY",
    "RANK_SECONDARY",
]
