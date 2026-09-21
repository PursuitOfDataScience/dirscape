"""Access probes: the tri-state, and the rule that a hang is never a refusal.

Everything here is built from ``tmp_path``, so the permission cases are real
directories with real modes rather than mocks of them. The one thing that
cannot be built from a real directory is a wedged network mount, so the timeout
tests replace the syscall with one that sleeps.
"""

import errno
import os
import time

import pytest

from dirscape.discover import access
from dirscape.discover.access import (
    probe_present,
    probe_reach,
    probe_writable,
    symlink_info,
    with_deadline,
)
from dirscape.model import Reach, VerdictCategory

# Running as root defeats every permission test: root bypasses the checks, so
# a 0o111 directory is readable and the assertions would be measuring nothing.
skip_if_root = pytest.mark.skipif(
    os.geteuid() == 0, reason="root bypasses the permission bits these tests set"
)


# --------------------------------------------------------------------------
# The tri-state
# --------------------------------------------------------------------------


def test_a_normal_directory_is_listable(tmp_path):
    result = probe_reach(str(tmp_path))
    assert result.state == Reach.LISTABLE
    assert "read" in result.reason


@skip_if_root
def test_traverse_only_is_its_own_state(tmp_path):
    """Mode 0o111: you can cd through it and you cannot list it.

    This is the middle state the whole `Reach` type exists for. Measured on
    this cluster at mode 2771, which gives a non-member ``--x``:
    ``/project/marsh`` is exactly this, ``os.access`` reports R=False X=True,
    and ``listdir`` really does raise EACCES. Collapsing it into "denied" would
    hide storage the user can reach.
    """
    target = tmp_path / "traverse"
    target.mkdir()
    target.chmod(0o111)
    try:
        # The premise: a real listdir refuses, so LISTABLE would be a lie.
        with pytest.raises(OSError):
            os.listdir(str(target))

        result = probe_reach(str(target))
        assert result.state == Reach.TRAVERSE
        assert result.state != Reach.CLOSED
        assert "execute without read" in result.reason
    finally:
        target.chmod(0o755)


@skip_if_root
def test_no_permission_at_all_is_closed(tmp_path):
    target = tmp_path / "closed"
    target.mkdir()
    target.chmod(0o000)
    try:
        result = probe_reach(str(target))
        assert result.state == Reach.CLOSED
    finally:
        target.chmod(0o755)


@skip_if_root
def test_readable_without_execute_is_not_listable(tmp_path):
    """opendir needs both bits, so r-- is not a listable directory."""
    target = tmp_path / "readonly"
    target.mkdir()
    target.chmod(0o444)
    try:
        assert probe_reach(str(target)).state == Reach.CLOSED
    finally:
        target.chmod(0o755)


def test_a_missing_path_is_unknown_not_closed(tmp_path):
    """ "Not there" is not a statement about permission.

    `probe_present` answers existence. Reporting CLOSED here would be a durable
    claim about access drawn from no evidence about access at all.
    """
    result = probe_reach(str(tmp_path / "nope"))
    assert result.state == Reach.UNKNOWN
    assert result.state != Reach.CLOSED
    assert "not present" in result.reason


# --------------------------------------------------------------------------
# Deadlines. A hang must never read as a refusal.
# --------------------------------------------------------------------------


def test_a_timeout_gives_unknown_and_never_closed(tmp_path, monkeypatch):
    """The inverted NT-1 failure, and the one this module exists to prevent.

    A ``stat`` on a wedged network mount blocks with no way to interrupt it. If
    that came back as CLOSED the atlas would report a filesystem as permission
    denied because one metadata server was busy, and the user would go asking
    for access they already have.
    """

    def hangs(*_args, **_kwargs):
        time.sleep(10)
        return True

    monkeypatch.setattr(access.os, "access", hangs)
    started = time.time()
    result = probe_reach(str(tmp_path), deadline_s=0.05)
    elapsed = time.time() - started

    assert result.state == Reach.UNKNOWN
    assert result.state != Reach.CLOSED, "a hang must never be reported as closed"
    assert elapsed < 5.0, "the deadline must be enforced, not merely recorded"
    assert "did not finish" in result.reason


def test_a_stat_timeout_is_a_transient_verdict(tmp_path, monkeypatch):
    def hangs(*_args, **_kwargs):
        time.sleep(10)

    monkeypatch.setattr(access.os, "lstat", hangs)
    verdict = probe_present(str(tmp_path), deadline_s=0.05)
    assert verdict.category == VerdictCategory.PROBE_TIMEOUT
    assert verdict.durable is False
    assert verdict.refuted is False, "a timeout must not read as a refusal"
    assert verdict.value is None


def test_with_deadline_reports_that_it_gave_up():
    finished, value, exc, elapsed = with_deadline(lambda: time.sleep(10), 0.05)
    assert finished is False
    assert value is None
    assert exc is None
    assert elapsed < 5.0


def test_with_deadline_passes_an_exception_back():
    """Swallowing it would turn a real error into a silent timeout."""

    def boom():
        raise ValueError("no")

    finished, _value, exc, _elapsed = with_deadline(boom, 1.0)
    assert finished is True
    assert isinstance(exc, ValueError)


def test_with_deadline_returns_the_value():
    finished, value, exc, _elapsed = with_deadline(lambda: 42, 1.0)
    assert (finished, value, exc) == (True, 42, None)


def test_a_zero_deadline_does_not_run_the_probe():
    """An exhausted budget must not be spent, and must not answer either."""
    calls = []
    finished, _value, _exc, _elapsed = with_deadline(lambda: calls.append(1), 0.0)
    assert finished is False
    assert calls == []


# --------------------------------------------------------------------------
# present
# --------------------------------------------------------------------------


def test_present_is_confirmed_for_a_real_directory(tmp_path):
    verdict = probe_present(str(tmp_path))
    assert verdict.confirmed is True
    assert verdict.source == "os.lstat"


def test_enoent_is_a_durable_refusal(tmp_path):
    verdict = probe_present(str(tmp_path / "missing"))
    assert verdict.refuted is True
    assert verdict.category == VerdictCategory.NOT_PRESENT
    assert verdict.durable is True


def test_a_dangling_symlink_is_present_as_a_symlink(tmp_path):
    """lstat, not stat: the link exists even though its target does not.

    Following it would also let a link into a wedged filesystem hang a probe
    that was only ever asked about the link.
    """
    link = tmp_path / "dangling"
    link.symlink_to(str(tmp_path / "nowhere"))
    assert probe_present(str(link)).confirmed is True


@skip_if_root
def test_a_stat_denied_by_its_parent_is_unknown_not_refuted(tmp_path):
    """The path may well exist. Saying it does not would be a false negative."""
    parent = tmp_path / "locked"
    parent.mkdir()
    (parent / "child").mkdir()
    parent.chmod(0o000)
    try:
        verdict = probe_present(str(parent / "child"))
        assert verdict.refuted is False
        assert verdict.known is False
    finally:
        parent.chmod(0o755)


# --------------------------------------------------------------------------
# writable
# --------------------------------------------------------------------------


def test_the_default_answers_only_for_a_directory_you_own(tmp_path):
    verdict = probe_writable(str(tmp_path))
    assert verdict.confirmed is True
    assert verdict.source == "os.access"
    assert "owns" in verdict.reason


def test_the_default_refuses_to_trust_w_ok_for_a_directory_you_do_not_own(tmp_path):
    """Because ``os.access(W_OK)`` lies under a root-squashed export.

    The client evaluates the mode bits locally and the server then refuses the
    write, so the answer is yes and the write fails. Ownership is what removes
    the ambiguity, since squashing remaps uid 0 and nothing else. That
    configuration is not present on this cluster and is present at other sites,
    which is exactly when to be careful rather than to wait for the bug report.
    """
    verdict = probe_writable(str(tmp_path), uid=os.getuid() + 12345)
    assert verdict.category == VerdictCategory.NOT_PROBED
    assert verdict.value is None
    assert verdict.known is False
    assert "root-squashed" in verdict.reason


def test_the_default_never_trusts_w_ok_for_root(tmp_path):
    """uid 0 is the identity squashing remaps, so it is never unambiguous."""
    verdict = probe_writable(str(tmp_path), uid=0)
    assert verdict.category == VerdictCategory.NOT_PROBED


@skip_if_root
def test_no_write_permission_is_a_durable_refusal(tmp_path):
    target = tmp_path / "readonly"
    target.mkdir()
    target.chmod(0o555)
    try:
        verdict = probe_writable(str(target))
        assert verdict.refuted is True
        assert verdict.category == VerdictCategory.ACCESS_DENIED
    finally:
        target.chmod(0o755)


def test_allow_write_settles_it_and_records_the_method(tmp_path):
    """Which method answered is part of the answer.

    "O_TMPFILE said yes" and "os.access said yes" are not equally strong
    evidence, so a reader of the JSON has to be able to tell them apart.
    """
    before = sorted(os.listdir(str(tmp_path)))
    verdict = probe_writable(str(tmp_path), allow_write=True)
    assert verdict.confirmed is True
    assert verdict.source in ("O_TMPFILE", "dotfile")
    # Whichever method ran, it must leave nothing behind.
    assert sorted(os.listdir(str(tmp_path))) == before


@skip_if_root
def test_allow_write_reports_denial_as_denial(tmp_path):
    target = tmp_path / "locked"
    target.mkdir()
    target.chmod(0o555)
    try:
        verdict = probe_writable(str(target), allow_write=True)
        assert verdict.refuted is True
        assert verdict.category == VerdictCategory.ACCESS_DENIED
    finally:
        target.chmod(0o755)


def test_a_full_filesystem_is_writable_but_over_quota(tmp_path, monkeypatch):
    """Two different problems with two different fixes.

    "Denied" sends a user to their PI for group membership; "over quota" sends
    them to delete files. Collapsing them wastes somebody's afternoon.
    """

    def out_of_quota(*_args, **_kwargs):
        raise OSError(errno.EDQUOT, "Disk quota exceeded")

    monkeypatch.setattr(access.os, "open", out_of_quota)
    verdict = probe_writable(str(tmp_path), allow_write=True)
    assert verdict.refuted is True
    assert verdict.category == VerdictCategory.QUOTA_EXCEEDED


def test_an_unsupported_o_tmpfile_falls_back_to_a_dotfile(tmp_path, monkeypatch):
    """GPFS answers O_TMPFILE with EOPNOTSUPP, which is not an answer.

    Treating it as a refusal would report every GPFS directory as unwritable.
    """
    real_open = access.os.open
    seen = []

    def refuse_tmpfile(path, flags, *rest):
        if flags & getattr(os, "O_TMPFILE", 0) == getattr(os, "O_TMPFILE", 0):
            seen.append("tmpfile")
            raise OSError(errno.EOPNOTSUPP, "Operation not supported")
        return real_open(path, flags, *rest)

    monkeypatch.setattr(access.os, "open", refuse_tmpfile)
    verdict = probe_writable(str(tmp_path), allow_write=True)
    assert seen == ["tmpfile"], "O_TMPFILE must be tried first"
    assert verdict.confirmed is True
    assert verdict.source == "dotfile"
    assert os.listdir(str(tmp_path)) == []


# --------------------------------------------------------------------------
# symlinks
# --------------------------------------------------------------------------


def test_symlink_info_is_none_for_a_real_directory(tmp_path):
    assert symlink_info(str(tmp_path)) is None


def test_symlink_info_resolves_one_level(tmp_path):
    real = tmp_path / "real"
    real.mkdir()
    link = tmp_path / "link"
    link.symlink_to(str(real))

    info = symlink_info(str(link))
    assert info is not None
    assert info.resolved == str(real)
    assert info.is_dir is True
    # Same filesystem here, so no device crossing. The measured caveat is that
    # this field is also False for ~/.cache -> /project, because /home and
    # /project are two mountpoints of one GPFS device; candidates.py compares
    # mountpoints for that reason.
    assert info.crosses_device is False
    assert info.link_dev == info.target_dev


def test_a_relative_symlink_is_resolved_against_its_parent(tmp_path):
    (tmp_path / "sub").mkdir()
    (tmp_path / "sub" / "target").mkdir()
    link = tmp_path / "sub" / "link"
    link.symlink_to("target")

    info = symlink_info(str(link))
    assert info is not None
    assert info.resolved == str(tmp_path / "sub" / "target")


def test_symlink_info_survives_a_dangling_target(tmp_path):
    link = tmp_path / "dangling"
    link.symlink_to(str(tmp_path / "gone"))
    info = symlink_info(str(link))
    assert info is not None
    assert info.is_dir is False
    assert info.target_dev is None
    assert info.crosses_device is False
