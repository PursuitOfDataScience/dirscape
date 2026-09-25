"""The stock `quota -s` backend, and when it stands aside for a site wrapper.

A site very often REPLACES `quota` with its own script under the same name, so
the stock backend and the site wrapper backend can resolve to one file. Measured
on the development cluster: `quota` on PATH is `/software/bin/quota`, the site's
wrapper, and both backends ran it for byte-identical reports, about a third of
every run. These run STRICT, so a command the fixture never recorded raises.
"""

import os

from dirscape.discover import read_mount_table
from dirscape.model import VerdictCategory
from dirscape.quota import posix, read_all, wrapper
from dirscape.runner import Budget, RecordedRunner

FIXTURES = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fixtures")

TABLE = read_mount_table(text="/dev/sda1 /home ext4 rw 0 0\n")

STOCK = (
    "Disk quotas for user me (uid 1000): \n"
    "     Filesystem   space   quota   limit   grace   files   quota   limit   grace\n"
    "      /dev/sda1  12.3G     50G     55G            1234       0       0\n"
)


def _wrapper_report():
    with open(os.path.join(FIXTURES, "site_wrapper_no_mount_clause.txt")) as handle:
        return handle.read()


def _backends():
    return [wrapper.SiteWrapperBackend(script_paths=()), posix.PosixQuotaBackend()]


def test_the_stock_quota_is_not_asked_again_for_the_wrapper_it_turns_out_to_be():
    # Only the wrapper's own call is recorded: `quota -s` running at all would
    # raise NotRecorded.
    runner = RecordedRunner(
        [{"argv": ["/opt/site/bin/quota"], "returncode": 0, "stdout": _wrapper_report()}],
        probes={"quota": "/opt/site/bin/quota"},
    )
    attempts = read_all(_backends(), runner, TABLE, Budget(total_s=20.0), ["/"])

    assert [a.source for a in attempts] == ["site quota wrapper", "quota -s"]
    assert attempts[0].rows, "the wrapper still answers"
    assert attempts[1].category == VerdictCategory.NOT_PROBED
    assert "/opt/site/bin/quota" in attempts[1].reason
    assert "already read" in attempts[1].reason


def test_a_stock_quota_still_answers_when_the_wrapper_could_not_read_it():
    """The other half, and the one a careless fix breaks.

    Where `quota` IS the stock tool, the wrapper backend tries it as well,
    since a wrapper on that name is exactly what it looks for, and reads no
    rows from it. Standing aside on the executable alone would then leave
    the site with no quota reading at all.
    """
    runner = RecordedRunner(
        [
            {"argv": ["/usr/bin/quota"], "returncode": 0, "stdout": STOCK},
            {"argv": ["/usr/bin/quota", "-s"], "returncode": 0, "stdout": STOCK},
        ],
        probes={"quota": "/usr/bin/quota"},
    )
    attempts = read_all(_backends(), runner, TABLE, Budget(total_s=20.0), ["/"])

    assert not attempts[0].rows
    blocks = [row for row in attempts[1].rows if row.kind == "blocks"]
    assert [(row.fileset, row.mount) for row in blocks] == [("/dev/sda1", "/home")]
    assert blocks[0].used == int(12.3 * 1024**3)


def test_two_routes_to_one_script_are_one_executable(tmp_path):
    """A symlink on PATH and the script it names are the same program."""
    script = tmp_path / "quota-report"
    script.write_text("#!/bin/sh\n")
    script.chmod(0o755)
    link = tmp_path / "quota"
    link.symlink_to(script)
    runner = RecordedRunner(
        [{"argv": [str(script)], "returncode": 0, "stdout": _wrapper_report()}],
        probes={"quota": str(link), str(script): str(script)},
    )
    backends = [
        wrapper.SiteWrapperBackend(name_on_path=str(script), script_paths=()),
        posix.PosixQuotaBackend(),
    ]
    attempts = read_all(backends, runner, TABLE, Budget(total_s=20.0), ["/"])

    assert attempts[0].rows
    assert attempts[1].category == VerdictCategory.NOT_PROBED


class _Waits(object):
    """A backend that answers only once ``other`` has started, or says it waited."""

    stock = False
    per_path = False
    read_from = None

    def __init__(self, name, started, other=None, hang=None):
        self.name = name
        self.started = started
        self.other = other
        self.hang = hang

    def supported(self, runner):
        return "/bin/" + self.name

    def read(self, runner, mounts, budget, paths):
        from dirscape.quota.base import unavailable_quota

        self.started.set()
        if self.hang is not None:
            self.hang.wait(10)
        met = self.other.wait(5) if self.other is not None else True
        return unavailable_quota(
            self.name, VerdictCategory.NO_QUOTA_BACKEND, "met" if met else "waited alone"
        )


def test_the_backends_are_asked_at_the_same_time_and_answer_in_order():
    """Asked in turn, the start waited for each: six `mmlsquota` calls, then the
    site wrapper, back to back. Each of these answers only once the other has
    started, which asked in turn would take five seconds and say so."""
    import threading

    first, second = threading.Event(), threading.Event()
    backends = [_Waits("one", first, other=second), _Waits("two", second, other=first)]
    attempts = read_all(backends, None, TABLE, Budget(total_s=20.0), ["/"])
    assert [a.source for a in attempts] == ["one", "two"]
    assert [a.reason for a in attempts] == ["met", "met"]


def test_a_backend_that_hangs_is_given_up_on_and_the_rest_still_answer(monkeypatch):
    import threading
    import time

    from dirscape.quota import base

    monkeypatch.setattr(base, "JOIN_GRACE_S", 0.2)
    release = threading.Event()
    backends = [_Waits("stuck", threading.Event(), hang=release), _Waits("fine", threading.Event())]
    started = time.time()
    attempts = read_all(backends, None, TABLE, Budget(total_s=0.3), ["/"])
    release.set()
    assert time.time() - started < 3.0
    assert attempts[0].category == VerdictCategory.PROBE_TIMEOUT
    assert attempts[1].reason == "met"


def test_the_stock_quota_stands_aside_only_for_a_backend_before_it():
    """The order still decides: a wrapper listed AFTER the stock `quota` was not
    read yet when the stock one was asked, so it cannot stand in for it."""
    runner = RecordedRunner(
        [
            {"argv": ["/opt/site/bin/quota"], "returncode": 0, "stdout": _wrapper_report()},
            {"argv": ["/opt/site/bin/quota", "-s"], "returncode": 0, "stdout": STOCK},
        ],
        probes={"quota": "/opt/site/bin/quota"},
    )
    backends = [posix.PosixQuotaBackend(), wrapper.SiteWrapperBackend(script_paths=())]
    attempts = read_all(backends, runner, TABLE, Budget(total_s=20.0), ["/"])
    assert [a.source for a in attempts] == ["quota -s", "site quota wrapper"]
    assert attempts[0].category != VerdictCategory.NOT_PROBED, "asked, since nothing before it read"
