"""Run to run memory: what was here last time, and what changed.

Two modules, and the split is where the risk is:

* `snapshot` records a run and keeps a bounded lineage of them. It owns
  `first_seen`, which is the only honest answer to "is this new" on a
  filesystem with no usable creation timestamp.
* `diff` compares two runs. It owns the invariant that a failed probe never
  produces a state-change claim, which is the most important rule in this
  codebase.

Both are stdlib only and import nothing from this package except `model`.
"""

from .diff import (
    CLOSED,
    DELTA_BYTES_FLOOR,
    DELTA_FRACTION,
    DELTA_INODES_FLOOR,
    GONE,
    GREW,
    LABEL_ORDER,
    LABELS,
    NEW,
    OPENED,
    SHRANK,
    STRANDED,
    UNKNOWN,
    Change,
    DiffResult,
    describe,
    diff,
    label_rank,
)
from .snapshot import (
    COMPUTE,
    KEEP_RECENT,
    LOGIN,
    MAX_BYTES,
    SCHEMA,
    UNKNOWN_CLASS,
    Lineage,
    RootRecord,
    Snapshot,
    current_hostname,
    cutoff_for,
    default_path,
    fingerprint_mismatch,
    root_key,
    same_vantage,
    state_dir,
    vantage_warning,
)

__all__ = [
    # snapshot
    "SCHEMA",
    "KEEP_RECENT",
    "MAX_BYTES",
    "LOGIN",
    "COMPUTE",
    "UNKNOWN_CLASS",
    "RootRecord",
    "root_key",
    "Snapshot",
    "Lineage",
    "current_hostname",
    "cutoff_for",
    "default_path",
    "state_dir",
    "fingerprint_mismatch",
    "same_vantage",
    "vantage_warning",
    # diff
    "NEW",
    "OPENED",
    "CLOSED",
    "GREW",
    "SHRANK",
    "STRANDED",
    "UNKNOWN",
    "GONE",
    "LABELS",
    "LABEL_ORDER",
    "DELTA_FRACTION",
    "DELTA_BYTES_FLOOR",
    "DELTA_INODES_FLOOR",
    "Change",
    "DiffResult",
    "diff",
    "describe",
    "label_rank",
]
