"""Quota backends: six ways to ask, one way to say "I could not".

Ask through `read_best` for one path, or `read_all` when you want every
backend's answer. The difference matters: `read_best` returns the one reading
that actually governs the path you named, while `read_all` keeps the site
wrapper's rows for filesystems that are not mounted on this node, which is
where "allocated, but no path here" comes from.

    from dirscape.quota import default_backends, read_best
    snap = read_best(default_backends(site), runner, mounts, budget, "/project/hpc")

Three rules the whole package obeys:

* **Absence is never an empty reading.** A missing backend
  (`NO_QUOTA_BACKEND`), a failed one (`BACKEND_FAILED`), a filesystem that
  enforces nothing (`NO_QUOTA_ENFORCED`), a refused question
  (`PERMISSION_TO_ASK_DENIED`) and an unasked one (`NOT_PROBED`) are five
  different states, and every one of them is built with `unavailable_quota`.
  `base.check_snapshot` raises on the one combination that would lie.
* **Exit status is not the signal.** Every branch gates on `Completed.failed`,
  and success is asserted positively from the marker each format always emits.
* **Every command goes through the injected `Runner`**, so the whole package
  replays from a transcript captured on a cluster nobody here has an account
  on.

Backends are ordered by precision of attribution, not by likelihood of
success, because the order only decides between backends that both answer for
the asked path. `lustre` is the one backend with no live coverage: no Lustre
filesystem was reachable where this was written, so it is built from recorded
output shapes and says so in its own docstring.
"""

from .base import (
    BLOCKS,
    FILES,
    Backend,
    EmptyReadingError,
    check_snapshot,
    default_backends,
    read_all,
    read_best,
    select_snapshot,
    snapshot,
)
from .ceph import CephBackend
from .gpfs import GpfsBackend, filesets_seen, read_path_fileset
from .lustre import LustreBackend
from .posix import PosixQuotaBackend
from .wrapper import SiteWrapperBackend
from .xfs import XfsBackend, statvfs_capacity

__all__ = [
    "Backend",
    "BLOCKS",
    "FILES",
    "EmptyReadingError",
    "check_snapshot",
    "snapshot",
    "read_all",
    "read_best",
    "select_snapshot",
    "default_backends",
    "GpfsBackend",
    "LustreBackend",
    "CephBackend",
    "XfsBackend",
    "PosixQuotaBackend",
    "SiteWrapperBackend",
    "filesets_seen",
    "read_path_fileset",
    "statvfs_capacity",
]
