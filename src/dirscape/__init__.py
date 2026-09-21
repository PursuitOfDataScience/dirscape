"""dirscape: every storage root you can actually reach, and what is new.

The public surface is deliberately small. Everything a consumer needs lives in
`dirscape.model`; the rest is implementation.
"""

try:
    from ._version import __version__
except ImportError:  # pragma: no cover - source checkout without setuptools_scm
    __version__ = "0+unknown"

__all__ = ["__version__"]
