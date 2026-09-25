"""The search: names anywhere under a folder, found without a `stat`, asked again from memory."""

import os

import pytest

from dirscape.search import Finder, Query


def _tree(root, files):
    """``files`` are paths under ``root``; a trailing slash makes a folder."""
    for path in files:
        where = root / path
        if path.endswith("/"):
            where.mkdir(parents=True, exist_ok=True)
        else:
            where.parent.mkdir(parents=True, exist_ok=True)
            where.write_bytes(b"x")
    return root


def _found(finder, text):
    finder.ask(text)
    assert finder.wait(10)
    _query, matches, count = finder.snapshot()
    return [path + ("/" if is_dir else "") for path, is_dir in matches], count


@pytest.fixture
def lab(tmp_path):
    root = _tree(
        tmp_path / "lab",
        [
            "README.md",
            "data/ERA5-2020.nc",
            "data/era5-2021.nc",
            "data/raw/era5-1999.grib",
            "data/raw/notes.txt",
            "runs/era5/",
            "runs/2021/log.txt",
            "deep/a/b/c/d/era5.nc",
        ],
    )
    finder = Finder(str(root), threads=4)
    finder.start()
    assert finder.wait(10)
    return root, finder


def test_a_piece_of_a_name_finds_files_and_folders_at_any_depth_shallowest_first(lab):
    _root, finder = lab
    found, count = _found(finder, "Era5")
    assert found == [
        "data/ERA5-2020.nc",
        "data/era5-2021.nc",
        "runs/era5/",
        "data/raw/era5-1999.grib",
        "deep/a/b/c/d/era5.nc",
    ]
    assert count == 5


def test_a_shell_pattern_matches_the_whole_name(lab):
    _root, finder = lab
    assert _found(finder, "*.nc")[0] == [
        "data/ERA5-2020.nc",
        "data/era5-2021.nc",
        "deep/a/b/c/d/era5.nc",
    ]
    assert _found(finder, "era5")[1] == 5 and _found(finder, "era5?")[1] == 0
    assert _found(finder, "[a-f]ata")[0] == ["data/"]
    assert _found(finder, "[!d]ata")[0] == []


def test_a_trailing_slash_asks_for_folders_and_a_path_narrows_the_folder(lab):
    _root, finder = lab
    assert _found(finder, "era5/")[0] == ["runs/era5/"]
    assert _found(finder, "raw/era5")[0] == ["data/raw/era5-1999.grib"]
    assert _found(finder, "data/notes")[0] == ["data/raw/notes.txt"]


def test_asking_before_the_tree_is_read_finds_what_asking_after_does(tmp_path):
    root = _tree(tmp_path / "t", ["x%d/y%d/hit-%d.txt" % (i, i, i) for i in range(40)])
    early = Finder(str(root), threads=4)
    early.ask("hit")
    early.start()
    assert early.wait(10)
    late = Finder(str(root), threads=4)
    late.start()
    assert late.wait(10)
    assert _found(early, "hit") == _found(late, "hit")
    assert _found(late, "hit")[1] == 40


def test_typing_on_answers_from_memory_without_reading_again(lab, monkeypatch):
    _root, finder = lab
    monkeypatch.setattr(finder, "_listing", lambda folder: pytest.fail("read again"))
    for text in ("e", "er", "era", "era5", "era5-2"):
        _found(finder, text)
    assert _found(finder, "era5-2")[1] == 2


def test_past_the_names_kept_a_changed_query_reads_the_files_again(tmp_path):
    root = _tree(tmp_path / "t", ["a/one.txt", "b/two.txt", "c/three.txt"])
    finder = Finder(str(root), threads=1, keep_names=1)
    finder.start()
    assert finder.wait(10)
    assert sorted(_found(finder, ".txt")[0]) == ["a/one.txt", "b/two.txt", "c/three.txt"]


def test_matches_past_the_display_limit_are_counted_not_kept(tmp_path):
    root = _tree(tmp_path / "t", ["hit-%03d" % i for i in range(50)])
    finder = Finder(str(root), threads=2, max_matches=10)
    finder.start()
    assert finder.wait(10)
    found, count = _found(finder, "hit")
    assert len(found) == 10 and count == 50


def test_a_mount_point_below_the_top_is_not_entered(tmp_path):
    root = _tree(tmp_path / "t", ["mnt/other/deep-hit.txt", "keep/near-hit.txt"])
    finder = Finder(str(root), threads=2, skip=[str(root / "mnt")])
    finder.start()
    assert finder.wait(10)
    assert _found(finder, "hit")[0] == ["keep/near-hit.txt"]


def test_an_unreadable_folder_is_counted_and_the_rest_still_searched(tmp_path):
    if os.geteuid() == 0:
        pytest.skip("root reads everything")
    root = _tree(tmp_path / "t", ["open/hit.txt", "closed/hidden-hit.txt"])
    os.chmod(str(root / "closed"), 0)
    try:
        finder = Finder(str(root), threads=2)
        finder.start()
        assert finder.wait(10)
        assert _found(finder, "hit")[0] == ["open/hit.txt"]
        assert finder.unreadable == 1
    finally:
        os.chmod(str(root / "closed"), 0o755)


def test_names_that_do_not_lower_in_place_still_match(tmp_path):
    """`İ` lowers into two characters, so that listing takes the regex path."""
    root = _tree(tmp_path / "t", ["İstanbul-hit.txt", "café-hit.txt", "plain-hit.txt"])
    finder = Finder(str(root), threads=1)
    finder.start()
    assert finder.wait(10)
    assert sorted(_found(finder, "hit")[0]) == sorted(
        ["İstanbul-hit.txt", "café-hit.txt", "plain-hit.txt"]
    )
    assert _found(finder, "CAFÉ")[0] == ["café-hit.txt"]


def test_a_plain_query_is_taken_literally():
    assert Query("a+b").names_in("a+b.txt/aab.txt") == ["a+b.txt"]
    assert Query("  ").empty and Query("/").empty


def test_stopping_ends_the_reading(tmp_path):
    root = _tree(tmp_path / "t", ["d%d/f" % i for i in range(200)])
    finder = Finder(str(root), threads=2)
    finder.stop()
    finder.start()
    assert finder.wait(5)


def _wide(root, folders=300, files=5):
    for i in range(folders):
        where = root / ("g%d" % (i % 7)) / ("d%03d" % i)
        where.mkdir(parents=True, exist_ok=True)
        for j in range(files):
            (where / ("f%03d_%d.txt" % (i, j))).write_bytes(b"")
    return root


def test_a_tree_past_what_is_kept_is_still_searched_in_full_and_once(tmp_path):
    """Past `keep_folders` nothing more is kept; a changed query answers the
    kept part from memory and reads the rest again, never twice."""
    root = _wide(tmp_path / "t")
    finder = Finder(str(root), threads=4, keep_folders=40)
    finder.ask("f1")
    finder.start()
    for text in ("_", "_3", "f0", "_3.t"):
        finder.ask(text)
    found, count = _found(finder, "_3.txt")
    assert finder.capped
    assert count == 300 and len(found) == len(set(found)) == 300
    assert _found(finder, "d12")[1] == 10, "d120 to d129, and nothing twice"


def test_readers_leave_once_the_tree_is_read(tmp_path):
    import time

    root = _wide(tmp_path / "t", folders=20)
    finder = Finder(str(root), threads=4)
    finder.start()
    assert finder.wait(10)
    stop = time.time() + 5
    while finder._alive and time.time() < stop:
        time.sleep(0.01)
    assert finder._alive == 0, "no reader waits about once there is nothing to read"
