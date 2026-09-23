"""Shared test setup.

`sys.path` is adjusted rather than requiring an editable install, so the suite
runs from a fresh clone with nothing but pytest. That matters for a package
whose selling point is that it works on a machine where you cannot install
anything.
"""

import os
import sys

SRC = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src")
if SRC not in sys.path:
    sys.path.insert(0, SRC)

import pytest  # noqa: E402

from dirscape.runner import RecordedRunner  # noqa: E402

FIXTURES = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fixtures")


@pytest.fixture(autouse=True)
def _a_person_at_the_keyboard(monkeypatch):
    """Every test runs as a person, on no site config, unless it says otherwise.

    The suite is often launched from an agent harness, which exports
    `AI_AGENT` or `CLAUDECODE`, and under one of those `main` behaves
    differently on purpose: no browser, a pointer on stderr, no baseline
    written. A test written for a person would otherwise pass or fail
    depending on where it was started.
    """
    from dirscape import cli

    for name in cli.AGENT_VARIABLES:
        monkeypatch.delenv(name, raising=False)
    # Nor does a test read the machine's own site config. A developer's
    # `~/.config/dirscape/config.conf` can switch the site plugin on, and a
    # suite that passes on one login node and fails on a clean runner is
    # testing the node. `load_site` skips a path that is not a regular file.
    monkeypatch.setenv("DIRSCAPE_CONFIG", os.devnull)


@pytest.fixture
def recorded():
    """Build a RecordedRunner from an inline transcript.

    Every backend test uses this rather than the live cluster. A test that
    reads the host it runs on cannot be run anywhere else, which is rapiDU's
    RD-10: a test that pinned the development cluster's identity failed on
    every other host.
    """

    def build(commands, probes=None, strict=True):
        return RecordedRunner(commands, probes or {}, strict=strict)

    return build


@pytest.fixture
def cmd():
    """One transcript entry, with sane defaults."""

    def build(argv, stdout="", stderr="", returncode=0, timed_out=False, not_found=False):
        return {
            "argv": list(argv),
            "stdout": stdout,
            "stderr": stderr,
            "returncode": returncode,
            "timed_out": timed_out,
            "not_found": not_found,
            "elapsed_s": 0.01,
        }

    return build
