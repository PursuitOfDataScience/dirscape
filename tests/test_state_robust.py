"""What happens when the state file is not what we left there.

Every case here degrades to "no baseline", which is the right outcome. The
tests are about whether the user is TOLD, because silently starting over
discards a history somebody may have been building for a month and then
answers their "what changed?" with "nothing, ever".
"""

import json
import os

import pytest

from dirscape.state import Lineage, Snapshot


def _seed(tmp_path):
    path = str(tmp_path / "lineage.json")
    lineage = Lineage(path=path)
    lineage.append(Snapshot(taken_at=1000.0, hostname="h", cluster_fingerprint="fp"))
    assert lineage.save(path=path) is True
    return path


# --------------------------------------------------------------------------
# Reading something unexpected
# --------------------------------------------------------------------------


def test_a_missing_file_is_silent():
    """The normal first run. Nothing to report."""
    loaded = Lineage.load(path="/definitely/not/here/lineage.json")
    assert len(loaded) == 0
    assert loaded.notes == [], "a first run must not warn about anything"


def test_a_damaged_file_says_so(tmp_path):
    """It returned silently while a FOREIGN file warned: same outcome, so the
    same courtesy. A user who has run this for a month and is told "no
    baseline yet" deserves to know the file was unreadable.
    """
    path = _seed(tmp_path)
    with open(path, "w") as handle:
        handle.write("not json at all {{{")

    loaded = Lineage.load(path=path)

    assert len(loaded) == 0
    assert loaded.notes, "a damaged baseline must be reported"
    assert "damaged" in loaded.notes[0]


def test_a_truncated_file_says_so(tmp_path):
    path = _seed(tmp_path)
    with open(path) as handle:
        head = handle.read(80)
    with open(path, "w") as handle:
        handle.write(head)

    loaded = Lineage.load(path=path)
    assert len(loaded) == 0
    assert loaded.notes


def test_a_foreign_file_says_so(tmp_path):
    path = _seed(tmp_path)
    with open(path, "w") as handle:
        json.dump({"tool": "somethingelse", "schema": 1}, handle)

    loaded = Lineage.load(path=path)
    assert len(loaded) == 0
    assert any("not a dirscape lineage" in note for note in loaded.notes)


def test_a_future_schema_says_so_rather_than_misreading_it(tmp_path):
    path = _seed(tmp_path)
    with open(path) as handle:
        payload = json.load(handle)
    payload["schema"] = 999
    with open(path, "w") as handle:
        json.dump(payload, handle)

    loaded = Lineage.load(path=path)
    assert len(loaded) == 0
    assert any("schema" in note for note in loaded.notes)


@pytest.mark.skipif(os.getuid() == 0, reason="root can read anything")
def test_an_unreadable_file_says_so(tmp_path):
    """Distinguished from absent, which is the whole point of the branch."""
    path = _seed(tmp_path)
    os.chmod(path, 0o000)
    try:
        loaded = Lineage.load(path=path)
        assert len(loaded) == 0
        assert any("could not read" in note for note in loaded.notes)
    finally:
        os.chmod(path, 0o600)


def test_one_bad_entry_does_not_cost_the_whole_baseline(tmp_path):
    """A file with one unreadable record should cost that record."""
    path = _seed(tmp_path)
    with open(path) as handle:
        payload = json.load(handle)
    payload["entries"].append({"nonsense": True})
    with open(path, "w") as handle:
        json.dump(payload, handle)

    loaded = Lineage.load(path=path)
    assert len(loaded) == 1, "the good entry must survive"


# --------------------------------------------------------------------------
# Writing where we cannot
# --------------------------------------------------------------------------


@pytest.mark.skipif(os.getuid() == 0, reason="root can write anywhere")
def test_saving_into_an_unwritable_directory_returns_false(tmp_path):
    """It RETURNS False rather than raising, and only the raise was handled.

    So on an unwritable state directory the run reported "a baseline has been
    recorded" while nothing reached the disk, and said it again on every run
    afterwards.
    """
    shut = tmp_path / "shut"
    shut.mkdir()
    shut.chmod(0o500)
    try:
        lineage = Lineage()
        lineage.append(Snapshot(taken_at=1.0, hostname="h", cluster_fingerprint="fp"))
        assert lineage.save(path=str(shut / "lineage.json")) is False
    finally:
        shut.chmod(0o700)


def test_saving_where_the_path_is_a_directory_returns_false(tmp_path):
    blocked = tmp_path / "lineage.json"
    blocked.mkdir()
    lineage = Lineage()
    lineage.append(Snapshot(taken_at=1.0, hostname="h", cluster_fingerprint="fp"))
    assert lineage.save(path=str(blocked)) is False


def test_a_successful_save_round_trips(tmp_path):
    """The control: the failure paths above are not the only outcome."""
    path = _seed(tmp_path)
    loaded = Lineage.load(path=path)
    assert len(loaded) == 1
    assert loaded.notes == []
    assert loaded.latest().taken_at == 1000.0
