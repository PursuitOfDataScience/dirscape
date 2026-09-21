"""Site plugins: where one cluster's peculiarities are allowed to live.

`sitecfg` handles everything a site can DESCRIBE declaratively. A plugin
handles what a site can only EXECUTE: an allocation CLI with its own output
format, a membership rule encoded in a local Python script, a historical quota
archive that happens to be world-readable. Those cannot be expressed in an INI
file and they must not be hardcoded in the core, so they get a plugin.

The contract is deliberately thin, and every hook is optional:

    detect(runner, mounts)        -> bool     is this that site?
    allocations(runner, budget)   -> [Alloc]  storage an allocation DB claims
    owns_fileset(fileset, groups) -> Optional[bool]
    first_seen(fileset, runner)   -> Optional[float]
    describe()                    -> str

Two rules keep this honest.

**A plugin may only ADD facts, never remove them.** A plugin that could veto a
root would be able to hide storage the user really does have, and debugging
that from the outside is close to impossible.

**A plugin's answers carry the same three states as everything else.**
`owns_fileset` returns `None` for "I do not know", not `False`. A site rule
that does not recognise a fileset has not established that the user lacks it.
"""

from typing import Dict, List, Optional, Sequence

__all__ = ["Allocation", "SitePlugin", "available_plugins", "detect_plugins"]


class Allocation(object):
    """Storage an allocation database says you have.

    Kept separate from a mounted root on purpose, and this is the whole reason
    the class exists. Measured on this cluster: `allocs storage` reports
    allocations on `cfs1`, `cfs2`, `cfs4` and `project3`, and **none of those
    paths exist on the node that printed them**. An allocation, a mount and an
    accessible directory are three independent facts, and a tool that collapses
    them tells a user their data is gone when it is merely elsewhere.
    """

    __slots__ = ("account", "location", "path", "size_gb", "kind", "start", "end", "source")

    def __init__(
        self,
        account,  # type: str
        location,  # type: str
        path=None,  # type: Optional[str]
        size_gb=None,  # type: Optional[float]
        kind="",  # type: str
        start="",  # type: str
        end="",  # type: str
        source="",  # type: str
    ):
        # type: (...) -> None
        self.account = account
        # The location as the allocation database names it, e.g. "cfs4/hpc-staff".
        # Not necessarily a path, and deliberately not coerced into one.
        self.location = location
        # A filesystem path, only when the plugin can establish one honestly.
        self.path = path
        self.size_gb = size_gb
        self.kind = kind
        self.start = start
        self.end = end
        self.source = source

    def to_json(self):
        # type: () -> Dict[str, object]
        return {
            "account": self.account,
            "location": self.location,
            "path": self.path,
            "size_gb": self.size_gb,
            "kind": self.kind,
            "start": self.start,
            "end": self.end,
            "source": self.source,
        }

    def __repr__(self):
        # type: () -> str
        return "Allocation(%r, %r)" % (self.account, self.location)


class SitePlugin(object):
    """Base class. Every hook is optional and defaults to knowing nothing."""

    name = "base"

    def detect(self, runner, mounts):
        # type: (object, Sequence[object]) -> bool
        """Is this that site? Must be cheap and must not run anything slow."""
        return False

    def describe(self):
        # type: () -> str
        return self.name

    def site_defaults(self):
        # type: () -> Dict[str, object]
        """Config values this site would otherwise need a `site.conf` to state.

        Merged at the LOWEST precedence, below `/etc` and below the user's own
        file, so a site administrator who disagrees with a plugin can always
        override it without editing the package.
        """
        return {}

    def allocations(self, runner, budget):
        # type: (object, object) -> List[Allocation]
        return []

    def owns_fileset(self, fileset, groups):
        # type: (str, Sequence[str]) -> Optional[bool]
        """Whether the user has this fileset, or None when the rule does not say."""
        return None

    def first_seen(self, fileset, runner, budget=None):
        # type: (str, object, object) -> Optional[float]
        """A creation date for a fileset, when the site keeps history."""
        return None

    def policy_for(self, path):
        # type: (str) -> Dict[str, object]
        return {}


def available_plugins():
    # type: () -> List[SitePlugin]
    """Every plugin that ships with the package.

    Imported lazily inside the function rather than at module import, so a
    plugin with a syntax error cannot take down the whole tool, and so the
    import cost is only paid when plugins are actually consulted.
    """
    found = []  # type: List[SitePlugin]
    try:
        from .hpc import HPCPlugin

        found.append(HPCPlugin())
    except Exception:
        # A broken plugin is not a reason to fail a diagnostic run. The core
        # works without any plugin at all; losing one costs labels, not
        # correctness.
        pass
    return found


def detect_plugins(runner, mounts, enabled=None):
    # type: (object, Sequence[object], Optional[Sequence[str]]) -> List[SitePlugin]
    """Plugins that claim this site.

    `enabled` forces a specific set by name, which is how the test suite runs
    the HPC rules against a recorded meadow2 transcript from anywhere.
    """
    candidates = available_plugins()
    if enabled is not None:
        wanted = set(enabled)
        return [p for p in candidates if p.name in wanted]

    active = []
    for plugin in candidates:
        try:
            if plugin.detect(runner, mounts):
                active.append(plugin)
        except Exception:
            continue
    return active
