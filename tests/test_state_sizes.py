"""The size index: finished counts kept between runs, and never a wrong one."""

import json
import os
import stat
import time

from dirscape.state import sizes as sizes_mod
from dirscape.state.sizes import SizeIndex, sizes_path

#: A filesystem every node mounts: its records are read on any of them.
FS = "gpfs:proj"


def _saved(tmp_path, *records, **kwargs):
    """An index at a file in ``tmp_path`` holding ``records``, flushed and reloaded."""
    where = str(tmp_path / "state" / "x.sizes.jsonl")
    index = SizeIndex.load(path=where, host=kwargs.get("host", "login1"))
    for record in records:
        index.put(*record, filesystem=kwargs.get("filesystem", FS))
    assert index.flush()
    return where


def test_a_finished_count_survives_the_run(tmp_path):
    now = time.time()
    where = _saved(tmp_path, ("/project/a", 4096, 7, now - 60, 11))
    again = SizeIndex.load(path=where, host="login2")
    assert again.get("/project/a", filesystem=FS) == (4096, 7, round(now - 60, 3))
    assert again.get("/project/a", ino=11, filesystem=FS) is not None
    assert again.get("/project/b", filesystem=FS) is None


def test_the_last_count_of_a_folder_wins(tmp_path):
    now = time.time()
    where = _saved(tmp_path, ("/p/a", 1, 1, now - 90), ("/p/a", 2, 2, now - 30))
    assert SizeIndex.load(path=where).get("/p/a", filesystem=FS)[:2] == (2, 2)


def test_a_folder_made_again_under_the_same_name_is_not_the_old_one(tmp_path):
    """A path is not a folder: the inode number the listing reads must match."""
    where = _saved(tmp_path, ("/p/a", 1, 1, time.time(), 11))
    index = SizeIndex.load(path=where)
    assert index.get("/p/a", ino=12, filesystem=FS) is None
    assert index.get("/p/a", ino=11, filesystem=FS) is not None
    assert index.get("/p/a", filesystem=FS) is not None, "no inode known: taken as it is"


def test_a_node_local_figure_is_never_read_on_another_node(tmp_path):
    """`/tmp/x` on one node and `/tmp/x` on the next are two folders."""
    now = time.time()
    where = str(tmp_path / "x.sizes.jsonl")
    index = SizeIndex.load(path=where, host="node1")
    index.put("/tmp/x", 5, 5, now)
    index.put("/project/x", 6, 6, now, filesystem=FS)
    index.flush()
    elsewhere = SizeIndex.load(path=where, host="node2")
    assert elsewhere.get("/tmp/x") is None
    assert elsewhere.get("/project/x", filesystem=FS) is not None, "a shared one travels"
    assert SizeIndex.load(path=where, host="node1").get("/tmp/x") is not None


def test_a_figure_is_read_wherever_its_filesystem_is_mounted_and_nowhere_else(tmp_path):
    """Polaris and Sophia share one home and one `/lus/eagle`: a folder counted
    on one opens at once on the other. The same path on another filesystem is
    another folder."""
    where = _saved(tmp_path, ("/lus/eagle/p", 7, 7, time.time()), filesystem="lustre:n:/eagle")
    sophia = SizeIndex.load(path=where, host="sophia-login-01")
    assert sophia.get("/lus/eagle/p", filesystem="lustre:n:/eagle") is not None
    assert sophia.get("/lus/eagle/p", filesystem="lustre:n:/grand") is None
    assert sophia.get("/lus/eagle/p") is None, "nor as a node-local folder"


def test_a_month_old_figure_and_one_from_the_future_are_dropped(tmp_path):
    now = time.time()
    where = _saved(
        tmp_path,
        ("/p/old", 1, 1, now - sizes_mod.KEEP_S - 5),
        ("/p/soon", 1, 1, now + 3 * 86400),
        ("/p/ok", 1, 1, now),
    )
    index = SizeIndex.load(path=where)
    assert index.get("/p/old", filesystem=FS) is None
    assert index.get("/p/soon", filesystem=FS) is None
    assert index.get("/p/ok", filesystem=FS) is not None


def test_a_line_cut_short_by_a_killed_run_is_skipped(tmp_path):
    where = _saved(tmp_path, ("/p/a", 1, 1, time.time()))
    with open(where, "a") as handle:
        handle.write('{"b": 5, "f": 5, "p": "/p/b", "t": 1')
    index = SizeIndex.load(path=where)
    assert index.get("/p/a", filesystem=FS) is not None and index.get("/p/b") is None


def test_a_file_that_is_not_an_index_is_neither_read_nor_written(tmp_path):
    where = tmp_path / "x.sizes.jsonl"
    where.write_text('{"something": "else"}\n{"b": 1, "f": 1, "p": "/p/a", "t": 1}\n')
    before = where.read_text()
    index = SizeIndex.load(path=str(where))
    assert index.get("/p/a") is None and index.notes
    index.put("/p/b", 1, 1, time.time())
    assert not index.flush(), "nothing to write to"
    assert where.read_text() == before


def test_an_index_from_another_schema_is_left_alone(tmp_path):
    where = tmp_path / "x.sizes.jsonl"
    header = {"kind": "sizes", "schema": sizes_mod.SIZES_SCHEMA + 1, "tool": "dirscape"}
    where.write_text(json.dumps(header) + "\n")
    before = where.read_text()
    index = SizeIndex.load(path=str(where))
    index.put("/p/b", 1, 1, time.time())
    index.flush()
    assert where.read_text() == before and "schema" in index.notes[0]


def test_superseded_lines_are_compacted_away(tmp_path):
    now = time.time()
    records = [("/p/a", n, n, now - 1000 + n) for n in range(400)]
    where = _saved(tmp_path, *records)
    with open(where) as handle:
        assert len(handle.read().splitlines()) == 401
    index = SizeIndex.load(path=where)
    assert index.get("/p/a", filesystem=FS)[:2] == (399, 399)
    with open(where) as handle:
        lines = handle.read().splitlines()
    assert len(lines) == 2, "the header and the one live record"
    assert json.loads(lines[0])["kind"] == "sizes"


def test_only_the_newest_folders_are_kept(tmp_path, monkeypatch):
    monkeypatch.setattr(sizes_mod, "MAX_RECORDS", 3)
    now = time.time()
    where = _saved(tmp_path, *[("/p/%d" % n, 1, 1, now - 100 + n) for n in range(5)])
    index = SizeIndex.load(path=where)
    assert len(index) == 3
    assert index.get("/p/0", filesystem=FS) is None
    assert index.get("/p/4", filesystem=FS) is not None


def test_an_unwritable_state_directory_costs_the_figures_and_nothing_else(tmp_path):
    blocked = tmp_path / "blocked"
    blocked.write_text("a file where the directory should be")
    index = SizeIndex.load(path=str(blocked / "x.sizes.jsonl"))
    index.put("/p/a", 1, 1, time.time())
    assert index.flush() is False
    assert index.get("/p/a") is not None, "this run still has it"


def test_the_file_is_private(tmp_path):
    """It lists every folder the reader opened, with its size."""
    where = _saved(tmp_path, ("/p/a", 1, 1, time.time()))
    assert stat.S_IMODE(os.stat(where).st_mode) == 0o600


def test_an_index_in_memory_only_still_answers():
    index = SizeIndex()
    index.put("/p/a", 1, 2, time.time())
    assert index.get("/p/a")[:2] == (1, 2)
    assert index.flush() is False


def test_the_index_lives_in_the_state_directory(tmp_path):
    environ = {"XDG_STATE_HOME": str(tmp_path)}
    assert sizes_path(environ) == str(tmp_path / "dirscape" / "sizes.jsonl")
