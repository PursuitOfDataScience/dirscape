"""Command execution, and the one lesson that cost the sibling packages most:
an exit code is not a success signal.
"""


import sys

import pytest

from dirscape.runner import (
    Budget,
    CapturingRunner,
    Completed,
    NotRecorded,
    RecordedRunner,
    SubprocessRunner,
    key_for,
)


# --------------------------------------------------------------------------
# Exit codes lie
# --------------------------------------------------------------------------


def test_exit_zero_with_only_stderr_counts_as_failure():
    """The GPFS case, measured.

    A bare `mmlsquota` with no device prints

        No quota enabled file system found.
        mmlsquota: tslsquota failed. Error code 22.

    and exits **0**. A backend gating on `returncode != 0` reads that as an
    empty but successful quota listing, which is how a quota'd filesystem gets
    reported as having no quota.
    """
    result = Completed(
        ["mmlsquota"],
        0,
        stdout="",
        stderr="No quota enabled file system found.\nError code 22.",
    )
    assert result.failed is True


def test_exit_zero_with_output_is_success():
    """The control. Without this, `failed` could just return True always."""
    assert Completed(["quota"], 0, stdout="home 1 2 3\n").failed is False


def test_exit_zero_with_stdout_and_a_warning_on_stderr_is_success():
    """A tool that warns and still answers has answered.

    Only the empty-stdout case is a failure, because plenty of site wrappers
    write a deprecation notice to stderr on every run.
    """
    result = Completed(["quota"], 0, stdout="home 1 2 3\n", stderr="warning: deprecated\n")
    assert result.failed is False


@pytest.mark.parametrize(
    "kwargs",
    [
        {"returncode": 1},
        {"returncode": 127},
        {"returncode": None},
        {"returncode": 0, "timed_out": True},
        {"returncode": 0, "not_found": True},
    ],
)
def test_failure_modes_all_report_failed(kwargs):
    assert Completed(["x"], **kwargs).failed is True


def test_timeout_and_not_found_stay_distinguishable():
    """Three different states the caller must tell apart.

    "This site has no such tool" is durable, "the tool hung" is transient, and
    "the tool answered no" is durable. Collapsing them into one error is how a
    missing backend gets reported as an empty quota.
    """
    timed = Completed(["mmlsquota"], None, timed_out=True, elapsed_s=4.0)
    missing = Completed(["mmlsquota"], None, not_found=True)
    answered = Completed(["mmlsquota"], 1, stderr="no such fileset")

    assert (timed.timed_out, timed.not_found) == (True, False)
    assert (missing.timed_out, missing.not_found) == (False, True)
    assert (answered.timed_out, answered.not_found) == (False, False)
    assert "timed out" in timed.diagnostic
    assert "not found" in missing.diagnostic


def test_diagnostic_keeps_stderr_rather_than_guessing():
    """rapiDU's RD-2: an exit of 127 was reported as "quota is not on PATH"
    while discarding the stderr that named the real cause, which was a site
    wrapper pointing at a directory that no longer existed.
    """
    result = Completed(
        ["quota"],
        127,
        stderr="/opt/site/bin/quota: line 3: /srv/adm/quotarept: No such file or directory",
    )
    assert "quotarept" in result.diagnostic
    assert "PATH" not in result.diagnostic


def test_diagnostic_never_returns_empty():
    assert Completed(["x"], 0).diagnostic
    assert Completed(["x"], 3).diagnostic


# --------------------------------------------------------------------------
# Recorded mode: an absent recording is a gap, never an answer
# --------------------------------------------------------------------------


def test_recorded_runner_raises_on_an_unrecorded_command():
    """This raise is the NT-1 guard.

    Returning an empty success for a command with no transcript would make a
    fixture's GAPS look like a cluster's ANSWERS, which is exactly the replay
    bug that rendered 21 measured-as-refusing queues as merely unchecked.
    """
    runner = RecordedRunner([{"argv": ["quota"], "stdout": "ok"}])
    assert runner.run(["quota"]).stdout == "ok"
    with pytest.raises(NotRecorded):
        runner.run(["mmlsquota", "meadow3_cap"])


def test_recorded_runner_non_strict_still_refuses_to_fabricate_success():
    """Even the lenient mode must not invent an answer."""
    runner = RecordedRunner([], strict=False)
    result = runner.run(["mmlsquota"])
    assert result.failed is True
    assert result.timed_out is True
    assert result.stdout == ""
    assert runner.unrecorded == ["mmlsquota"]


def test_recorded_availability_is_recorded_too():
    """A fixture must be able to say "GPFS is absent here" explicitly.

    Otherwise every backend probe on a replayed transcript silently reports the
    tool as missing, and a test that meant to exercise the GPFS path passes by
    never entering it.
    """
    runner = RecordedRunner([], probes={"mmlsquota": "/usr/lpp/mmfs/bin/mmlsquota"})
    assert runner.available("mmlsquota") == "/usr/lpp/mmfs/bin/mmlsquota"
    runner = RecordedRunner([], probes={"lfs": None})
    assert runner.available("lfs") is None
    with pytest.raises(NotRecorded):
        runner.available("xfs_quota")


def test_transcript_key_distinguishes_one_argument_from_two():
    """`mmlsquota --block-size auto` and `mmlsquota "--block-size auto"` are
    different commands, and a space-joined key would confuse them.
    """
    assert key_for(["a b"]) != key_for(["a", "b"])


def test_round_trip_capture_to_replay():
    """A capture on an unfamiliar cluster must become a usable fixture."""
    inner = RecordedRunner([{"argv": ["echo", "hi"], "stdout": "hi\n"}])
    capturing = CapturingRunner(inner=inner)
    capturing.run(["echo", "hi"])
    payload = capturing.to_json()

    replayed = RecordedRunner.from_json(payload)
    assert replayed.run(["echo", "hi"]).stdout == "hi\n"


# --------------------------------------------------------------------------
# Budget
# --------------------------------------------------------------------------


def test_budget_slice_respects_a_floor():
    """A slice below the floor guarantees a timeout that teaches nobody
    anything: a GPFS wrapper takes about 0.3s just to start.
    """
    budget = Budget(total_s=10.0)
    assert budget.slice_for(share=0.001, floor=0.35) >= 0.35


def test_budget_slice_respects_a_ceiling():
    budget = Budget(total_s=600.0)
    assert budget.slice_for(share=0.9, ceiling=4.0) == 4.0


def test_an_exhausted_budget_yields_no_time():
    """Once the allowance is gone, further probes must become NOT_PROBED
    rather than overrunning the runtime the tool promises.
    """
    budget = Budget(total_s=0.0)
    assert budget.exhausted is True
    assert budget.slice_for() == 0.0


def test_a_zero_slice_short_circuits_without_spawning():
    """A zero allowance must not reach `Popen` at all."""
    runner = SubprocessRunner(budget=Budget(total_s=0.0))
    result = runner.run([sys.executable, "-c", "print(1)"])
    assert result.timed_out is True
    assert result.stdout == ""


def test_budget_charges_what_was_spent():
    budget = Budget(total_s=5.0)
    runner = SubprocessRunner(budget=budget)
    runner.run([sys.executable, "-c", "pass"])
    assert budget.spent > 0.0


# --------------------------------------------------------------------------
# Executable lookup
# --------------------------------------------------------------------------


def test_available_searches_extra_dirs_after_path(tmp_path):
    """GPFS tools are NOT on PATH here and ARE world-executable in
    /usr/lpp/mmfs/bin. A backend probing with PATH alone concludes GPFS is
    absent on a GPFS cluster.
    """
    tool = tmp_path / "dirscape-mmlsquota-fixture"
    tool.write_text("#!/bin/sh\nexit 0\n")
    tool.chmod(0o755)

    runner = SubprocessRunner()
    name = "dirscape-mmlsquota-fixture"
    assert runner.available(name) is None
    assert runner.available(name, extra_dirs=(str(tmp_path),)) == str(tool)


def test_available_rejects_a_non_executable_file(tmp_path):
    # The tool name is deliberately unique rather than a plausible one like
    # `quota`. An earlier version of this test used `quota` and failed on the
    # development cluster, where /opt/site/bin/quota really is on PATH, so the
    # PATH hit shadowed the fixture. That is rapiDU's RD-10 in miniature: a
    # test that encodes an assumption about the host it runs on.
    name = "dirscape-nonexecutable-fixture"
    plain = tmp_path / name
    plain.write_text("not executable")
    plain.chmod(0o644)
    assert SubprocessRunner().available(name, extra_dirs=(str(tmp_path),)) is None


def test_missing_command_is_not_found_rather_than_an_exception():
    result = SubprocessRunner().run(["definitely-not-a-real-binary-xyzzy"])
    assert result.not_found is True
    assert result.failed is True


def test_locale_is_pinned_so_parsers_see_stable_output():
    """A localised decimal separator or a translated error string breaks every
    parser downstream, and pinning C is cheaper than making each one tolerant.
    """
    result = SubprocessRunner().run(
        [sys.executable, "-c", "import os; print(os.environ.get('LC_ALL'))"]
    )
    assert result.stdout.strip() == "C"
