"""The agent's view: `dirscape paths`, `why --json`, and the records behind both.

The property every test here protects is the module's one rule: **an agent is
told what the table says.** A record is built from the table's own cell
functions, so the first test checks that literally, cell by cell, and the rest
pin the conventions a consumer relies on without reading the source: a null
beside `?` and beside `none`, never a zero; every root accounted for exactly
once; a filter that can only narrow; and an agent's look that never moves the
baseline a person's `dirscape new` compares against.

Nothing here touches the cluster. The runs are built by hand, as in
`test_cli.py`, and `sweep` is replaced wherever `main` is driven.
"""

import argparse
import json
import os

import pytest

from dirscape import agent, cli
from dirscape.model import (
    QuotaRow,
    QuotaSnapshot,
    Reach,
    Root,
    SnapshotCopy,
    VerdictCategory,
    confirmed,
    refuted,
    unknown,
)
from dirscape.render import fields
from dirscape.render.style import Style, plain


def _root(path, role, writable=True, fileset="", device="dev0", reach=Reach.LISTABLE):
    root = Root(path, role=role, device=device, fstype="gpfs")
    root.fileset = fileset
    root.reach = reach
    root.present = confirmed()
    root.mounted = confirmed()
    if writable is True:
        root.writable = confirmed(source="os.access")
    elif writable is False:
        root.writable = refuted(VerdictCategory.ACCESS_DENIED, "read only")
    else:
        root.writable = unknown(VerdictCategory.NOT_PROBED)
    return root


def _quota(root, used, soft=None, hard=None, scope="user", files=None, file_limit=None):
    rows = [QuotaRow(root.fileset, "blocks", scope, used, soft=soft, hard=hard, mount=root.path)]
    root.quota = QuotaSnapshot("mmlsquota", rows)
    if files is not None:
        row = QuotaRow(root.fileset, "files", scope, files, soft=file_limit, hard=file_limit)
        row.mount = root.path
        row.mounts = [root.path]
        root.inode_quota = QuotaSnapshot("mmlsquota", [row])


G = 1024**3
T = 1024**4


def _run():
    """A small cluster with one of everything the view has to say."""
    home = _root("/home/me", "home", fileset="home")
    _quota(home, 855 * 1024**2, soft=30 * G, hard=30 * G, files=37000, file_limit=300000)
    home.recoverable = confirmed("copies were opened")
    home.snapshots = [
        SnapshotCopy("daily-2", "/snap/daily-2/home/me"),
        SnapshotCopy("daily-1", "/snap/daily-1/home/me"),
    ]
    home.policy["crosses_to"] = ["/project/lab/me"]
    home.policy["symlinks_out"] = [
        {"link": "/home/me/.conda", "target": "/project/lab/me/conda", "billed_to": "/project/lab"}
    ]

    lab = _root("/project/lab", "project", fileset="lab")
    _quota(lab, 11 * T, soft=0, hard=0, scope="fileset")
    lab.policy["free_bytes"] = 126 * T

    mine = _root("/project/lab/me", "project", fileset="lab")
    mine.policy["quota_on"] = "/project/lab"

    scratch = _root("/scratch/me", "scratch", fileset="scratch", device="dev1")
    _quota(scratch, 22 * G, soft=100 * G, hard=100 * G)
    scratch.policy["purge_days"] = 30
    scratch.recoverable = refuted(VerdictCategory.NOT_PRESENT, "the snapshot tree keeps nothing")

    data = _root("/data", "dataset", writable=False, fileset="data", device="dev2")
    _quota(data, 23 * T, soft=0, hard=0)

    tmp = _root("/tmp", "local", writable=None, device="dev3")
    tmp.policy["free_bytes"] = 40 * G
    tmp.policy["node_local"] = True

    theirs = _root("/project/other", "project", writable=False, fileset="other")
    theirs.reach = Reach.CLOSED
    _quota(theirs, 6 * G)
    theirs.stranded = True

    away = Root("", role="archive")
    away.allocated = confirmed()
    away.mounted = refuted(VerdictCategory.NOT_MOUNTED_HERE)
    away.policy["allocation_location"] = "cfs4/me"
    away.policy["allocation_gb"] = 23000
    away.policy["allocation_accounts"] = ["lab"]

    top = _root("/", "local", writable=False, device="root")
    top.policy["rank"] = "secondary"

    run = cli.Run()
    run.roots = [home, lab, mine, scratch, data, tmp, theirs, away, top]
    return run


def _by_path(payload):
    return {record["path"]: record for record in payload["paths"]}


# --------------------------------------------------------------------------
# The rule: the record says what the table says
# --------------------------------------------------------------------------


def test_every_record_carries_the_tables_own_cells():
    """Built from the cell functions, so the two can only disagree by a bug here."""
    style = Style()
    for root in _run().roots:
        if not root.path:
            continue
        record = agent.place(root)
        assert record["used"] == plain(fields.used_cell(root, style)[0])
        assert record["quota"] == plain(fields.limit_cell(root, style))
        assert record["free"] == plain(fields.free_cell(root, style))
        assert record["files"] == plain(fields.file_count_cell(root, style))
        assert record["max_files"] == plain(fields.inode_limit_cell(root, style))
        assert record["access"] == fields.access_words(root)


def test_a_null_sits_beside_a_mark_never_beside_a_figure():
    """`?` and `none` both carry a null number, and the cell says which it was."""
    records = _by_path(agent.paths_payload(_run(), show_all=True))
    home, lab, tmp = records["/home/me"], records["/project/lab"], records["/tmp"]

    assert (home["quota"], home["quota_bytes"]) == ("30G", 30 * G)
    assert (lab["quota"], lab["quota_bytes"]) == ("none", None), "no limit is not a number"
    assert (tmp["used"], tmp["used_bytes"]) == ("?", None), "unmeasured is not zero"
    for record in records.values():
        for text, number in (
            ("used", "used_bytes"),
            ("quota", "quota_bytes"),
            ("free", "free_bytes"),
            ("files", "files_count"),
            ("max_files", "max_files_count"),
        ):
            if record[text] in ("?", "none"):
                assert record[number] is None, (record["path"], text)
            else:
                assert isinstance(record[number], int), (record["path"], text)


def test_the_payload_explains_its_own_marks():
    """An agent handed this by a shell has read neither the README nor the code."""
    legend = agent.paths_payload(_run())["legend"]
    for mark in ("`?`", "`none`", "`~`", "quota_scope", "free_limited_by", "can_write"):
        assert mark in legend, mark


def test_every_record_has_every_key():
    """So a consumer indexes without guarding, whatever the row is."""
    shapes = {tuple(agent.place(root)) for root in _run().roots if root.path}
    assert len(shapes) == 1


def test_write_access_is_three_states_and_unsettled_is_not_a_no():
    records = _by_path(agent.paths_payload(_run(), show_all=True))
    assert records["/home/me"]["can_write"] is True
    assert records["/data"]["can_write"] is False
    assert records["/tmp"]["can_write"] is None
    assert records["/tmp"]["access"] == "read", "`read` means nobody checked writing"


def test_free_says_whose_headroom_it_is():
    """Your allowance and the filesystem's shared headroom are different answers."""
    records = _by_path(agent.paths_payload(_run(), show_all=True))
    assert records["/home/me"]["free_limited_by"] == "quota"
    assert records["/home/me"]["free_bytes"] == 30 * G - 855 * 1024**2
    assert records["/project/lab"]["free_limited_by"] == "filesystem"
    assert records["/project/lab"]["quota_scope"] == "fileset", "everyone's usage, not yours"


def test_a_smaller_filesystem_wins_over_a_larger_allowance():
    """A 40T allowance on a filesystem with 2T left is 2T of writes."""
    root = _root("/p", "project")
    _quota(root, 0, soft=40 * T, hard=40 * T)
    root.policy["free_bytes"] = 2 * T
    assert fields.free_space(root) == (2 * T, "filesystem")
    assert fields.free_cell(root) == "2.0T"


def test_snapshots_are_a_count_a_measured_zero_or_unknown():
    records = _by_path(agent.paths_payload(_run(), show_all=True))
    assert records["/home/me"]["snapshots"] == 2
    assert records["/scratch/me"]["snapshots"] == 0, "a snapshot tree that keeps nothing"
    assert records["/data"]["snapshots"] is None, "never checked is not none kept"


def test_purge_and_symlinks_are_data():
    records = _by_path(agent.paths_payload(_run(), show_all=True))
    assert records["/scratch/me"]["purge_days"] == 30
    assert records["/home/me"]["purge_days"] is None
    assert records["/home/me"]["symlinked_to"] == ["/project/lab/me"]


# --------------------------------------------------------------------------
# The list
# --------------------------------------------------------------------------


def test_the_default_rows_are_the_tables_rows_in_the_tables_order():
    run = _run()
    shown, _hidden = cli._visible(run, show_all=False)
    payload = agent.paths_payload(run)
    assert [r["path"] for r in payload["paths"]] == [r.path for r in shown]
    assert "/project/lab/me" not in _by_path(payload), "folded under /project/lab, as the table"


def test_every_root_is_accounted_for_exactly_once():
    """Listed, stranded, elsewhere or hidden: nothing dropped, nothing twice."""
    for show_all in (False, True):
        run = _run()
        payload = agent.paths_payload(run, show_all=show_all)
        total = (
            len(payload["paths"])
            + len(payload["stranded"])
            + len(payload["elsewhere"])
            + payload["hidden"]
        )
        assert total == len(run.roots), show_all
    assert agent.paths_payload(_run(), show_all=True)["hidden"] == 0


def test_stranded_and_elsewhere_have_their_own_lists():
    payload = agent.paths_payload(_run())
    assert [s["path"] for s in payload["stranded"]] == ["/project/other"]
    assert payload["stranded"][0]["used"] == "6.0G"
    away = payload["elsewhere"][0]
    assert away["location"] == "cfs4/me"
    assert away["size_bytes"] == 23000 * 1000**3, "the database's GB are decimal"
    assert away["accounts"] == ["lab"]


def test_filters_only_narrow():
    run = _run()
    everything = _by_path(agent.paths_payload(run))
    writable = agent.paths_payload(run, writable=True)
    assert set(_by_path(writable)) <= set(everything)
    assert all(r["can_write"] is True for r in writable["paths"])
    assert writable["filtered_out"] == len(everything) - len(writable["paths"])

    scratch = agent.paths_payload(run, kinds=("scratch",))
    assert [r["path"] for r in scratch["paths"]] == ["/scratch/me"]


def test_min_free_drops_a_place_nobody_measured():
    """An unknown is not enough room."""
    root = _root("/u", "scratch")
    root.policy = {}
    run = cli.Run()
    run.roots = [root]
    assert agent.paths_payload(run, min_free=1)["paths"] == []
    enough = agent.paths_payload(_run(), show_all=True, min_free=50 * G)
    assert "/tmp" not in _by_path(enough), "/tmp has 40G"
    assert "/scratch/me" in _by_path(enough), "78G left of a 100G quota"


@pytest.mark.parametrize(
    "text, size",
    [
        ("2T", 2 * T),
        ("2t", 2 * T),
        ("2TB", 2 * T),
        ("2TiB", 2 * T),
        ("1.5G", int(1.5 * G)),
        ("500", 500),
        ("0", 0),
        (" 30G ", 30 * G),
        ("2 T", 2 * T),
        (1024, 1024),
    ],
)
def test_a_size_uses_the_tables_units(text, size):
    assert agent.parse_size(text) == size


@pytest.mark.parametrize("bad", ["", "lots", "2X", "-1G", "2GT", "1.2.3G", True, -5, None])
def test_a_size_that_is_not_one_is_refused(bad):
    with pytest.raises(ValueError):
        agent.parse_size(bad)


def test_a_misspelt_kind_is_refused_rather_than_answered_empty():
    assert agent.parse_kinds("project, scratch") == ("project", "scratch")
    assert agent.parse_kinds(None) == ()
    with pytest.raises(ValueError, match="scrach"):
        agent.parse_kinds("scrach")


# --------------------------------------------------------------------------
# `dirscape paths` and `why --json`, through main
# --------------------------------------------------------------------------


def _drive(monkeypatch, capsys, argv, run=None):
    calls = []

    def fake(opts, runner=None, save_state=True):
        calls.append(save_state)
        return run if run is not None else _run()

    monkeypatch.setattr(cli, "sweep", fake)
    code = cli.main(argv)
    out, err = capsys.readouterr()
    return code, out, err, calls


def test_paths_prints_one_path_per_line(monkeypatch, capsys):
    code, out, _err, _calls = _drive(monkeypatch, capsys, ["paths"])
    assert code == cli.EXIT_OK
    assert out.splitlines() == [r["path"] for r in agent.paths_payload(_run())["paths"]]


def test_paths_json_is_the_payload(monkeypatch, capsys):
    code, out, _err, _calls = _drive(
        monkeypatch, capsys, ["paths", "--json", "--kind", "home,scratch"]
    )
    payload = json.loads(out)
    assert code == cli.EXIT_OK
    assert payload["view"] == "paths" and payload["schema_version"] == agent.SCHEMA_VERSION
    assert [r["kind"] for r in payload["paths"]] == ["home", "scratch"]


def test_a_bad_filter_fails_before_the_sweep(monkeypatch, capsys):
    """A typo costs a message, not a five second run ending in one."""
    for argv in (["paths", "--kind", "scrach"], ["paths", "--min-free", "lots"]):
        code, out, err, calls = _drive(monkeypatch, capsys, argv)
        assert code == cli.EXIT_USAGE
        assert calls == [], "the sweep must not run"
        assert "dirscape:" in err and not out


def test_paths_never_saves_a_baseline(monkeypatch, capsys):
    """An agent looking must not move what `dirscape new` compares against."""
    _code, _out, _err, calls = _drive(monkeypatch, capsys, ["paths"])
    assert calls == [False]
    _code, _out, _err, calls = _drive(monkeypatch, capsys, ["--json"])
    assert calls == [True], "a person's run still records one"


def test_an_agent_never_moves_the_baseline(monkeypatch, capsys):
    """Every view an agent runs reads the lineage and writes none of it.

    Measured before the fix: under an agent harness `why --json`, `--json`,
    `new` and `matrix` each recorded a baseline, so a person's next
    `dirscape new` compared against whatever their agent had looked at.
    """
    monkeypatch.setenv("DIRSCAPE_AGENT", "1")
    for argv in (["--json"], ["why", "/home/me", "--json"], ["new"], ["matrix"], ["tree"]):
        _code, _out, _err, calls = _drive(monkeypatch, capsys, argv)
        assert calls == [False], argv
    _code, _out, _err, calls = _drive(monkeypatch, capsys, ["snapshot"])
    assert calls == [True], "recording is what `snapshot` was asked to do"


def test_which_commands_look_only():
    person, harness = {}, {"AI_AGENT": "x"}
    assert cli.looks_only("paths", person) and cli.looks_only("paths", harness)
    assert not cli.looks_only("atlas", person) and cli.looks_only("atlas", harness)
    assert not cli.looks_only("snapshot", person) and not cli.looks_only("snapshot", harness)


def test_a_look_only_run_reads_the_lineage_and_writes_nothing(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path))
    run = _run()
    run.identity = None
    cli._record_state(run, argparse.Namespace(since=None), save=False)
    assert run.changes is not None and getattr(run.changes, "no_baseline", False)
    assert not any(files for _dir, _subdirs, files in os.walk(str(tmp_path))), "nothing written"
    assert not any("seeded one" in w for w in run.warnings), "nothing was seeded"


def _lineage_of(tmp_path, monkeypatch, ages_days):
    """A real lineage on disk, one saved run per age, oldest first."""
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path))
    for _age in ages_days:
        run = _run()
        run.identity = None
        cli._record_state(run, argparse.Namespace(since=None), save=True)
    (state,) = [os.path.join(d, f) for d, _s, fs in os.walk(str(tmp_path)) for f in fs]
    with open(state) as handle:
        payload = json.load(handle)
    for entry, age in zip(payload["entries"], ages_days):
        entry["taken_at"] -= age * 86400.0
    with open(state, "w") as handle:
        json.dump(payload, handle)


def _new(since):
    run = _run()
    run.identity = None
    cli._record_state(run, argparse.Namespace(since=since), save=False)
    argv = ["new"] + (["--since", since] if since else [])
    text, _code = cli._render(run, cli.build_parser().parse_args(argv), "new", None, None)
    return run, text


def test_new_names_the_run_it_compared_against(tmp_path, monkeypatch):
    """`No change since the last run` said nothing about when that was."""
    _lineage_of(tmp_path, monkeypatch, [0])
    _, text = _new(None)
    assert text.startswith("No change since the last run, at 20")
    assert "ago)." in text


def test_since_says_so_when_no_run_is_that_old(tmp_path, monkeypatch):
    """`new --since 30d` against a lineage begun that day fell back to the
    oldest run without a word, and answered "No change since the last run"
    about a window of minutes.
    """
    _lineage_of(tmp_path, monkeypatch, [2, 0])
    run, text = _new("30d")
    assert run.baseline_since == "30d"
    assert text.startswith("No change since the run at ")
    assert "(2d ago)" in text, "the oldest run kept, and it says how old"
    assert "no run is 30d old yet" in text


def test_since_is_quiet_when_the_window_is_covered(tmp_path, monkeypatch):
    _lineage_of(tmp_path, monkeypatch, [40, 10, 0])
    run, text = _new("30d")
    assert "(40d ago)" in text
    assert not any("old yet" in w for w in run.warnings)


def test_why_json_is_about_the_one_path_asked(tmp_path, monkeypatch, capsys):
    """It used to emit every visible root whatever path was named."""
    (tmp_path / "home" / "me").mkdir(parents=True)
    home = _root(str(tmp_path / "home" / "me"), "home")
    _quota(home, 10, soft=100, hard=100)
    other = _root(str(tmp_path / "home"), "home", writable=False)
    run = cli.Run()
    run.roots = [other, home]

    code, out, _err, _calls = _drive(monkeypatch, capsys, ["why", home.path, "--json"], run=run)
    payload = json.loads(out)
    assert code == cli.EXIT_OK
    assert [r["path"] for r in payload["roots"]] == [home.path]
    assert payload["asked"]["root"] == home.path
    assert payload["place"]["used_bytes"] == 10


def test_why_json_answers_for_a_path_that_does_not_exist_yet(tmp_path, monkeypatch, capsys):
    """The same answer the MCP tool gives, so the two machine surfaces agree."""
    home = _root(str(tmp_path), "home")
    run = cli.Run()
    run.roots = [home]
    later = str(tmp_path / "run42" / "out.h5")
    code, out, _err, _calls = _drive(monkeypatch, capsys, ["why", later, "--json"], run=run)
    payload = json.loads(out)
    assert code == cli.EXIT_OK
    assert payload["asked"]["exists"] is False
    assert payload["asked"]["nearest_existing"] == str(tmp_path)
    assert [r["path"] for r in payload["roots"]] == [home.path]


def test_why_json_with_no_root_above_is_an_error_with_a_reason(tmp_path, monkeypatch, capsys):
    run = cli.Run()
    run.roots = [_root(str(tmp_path / "a"), "home")]
    code, out, _err, _calls = _drive(
        monkeypatch, capsys, ["why", "/definitely/not/here", "--json"], run=run
    )
    payload = json.loads(out)
    assert code == cli.EXIT_PATH
    assert payload["roots"] == []
    assert "does not exist" in payload["error"]


# --------------------------------------------------------------------------
# One path, for an agent
# --------------------------------------------------------------------------


def _lab_on_disk(tmp_path):
    """`/project/lab` and `/project/lab/me` as real directories under tmp_path."""
    lab = tmp_path / "project" / "lab"
    (lab / "me").mkdir(parents=True)
    group = _root(str(lab), "project", fileset="lab")
    _quota(group, 11 * T, soft=0, hard=0, scope="fileset")
    mine = _root(str(lab / "me"), "project", fileset="lab")
    mine.policy["quota_on"] = str(lab)
    run = cli.Run()
    run.roots = [group, mine]
    return run, lab


def test_explain_answers_for_a_path_that_does_not_exist_yet(tmp_path):
    """Asked BEFORE the output directory exists, which is when it matters."""
    run, lab = _lab_on_disk(tmp_path)
    answer = agent.explain_payload(run, str(lab / "me" / "run42" / "out.h5"))
    assert answer["exists"] is False
    assert answer["root"] == str(lab / "me")
    assert answer["nearest_existing"] == str(lab / "me")


def test_explain_follows_a_quota_reported_one_row_up(tmp_path):
    """`why` prints `?` and a note; an agent asking "is there room" needs the figure."""
    run, lab = _lab_on_disk(tmp_path)
    answer = agent.explain_payload(run, str(lab / "me"))
    assert answer["place"]["used"] == "?"
    assert answer["quota_from"]["path"] == str(lab)
    assert answer["quota_from"]["used"] == "11T"


def test_why_still_refuses_a_path_that_does_not_exist(tmp_path):
    """The allowance is the agent's. A person who typed a missing path is told."""
    run, lab = _lab_on_disk(tmp_path)
    text, code = cli._why(run, str(lab / "me" / "typo"), Style())
    assert code == cli.EXIT_PATH
    assert "does not exist" in text


def test_explain_on_a_path_no_root_covers_raises_with_whys_sentence(tmp_path):
    run = cli.Run()
    run.roots = [_root(str(tmp_path / "a"), "home")]
    with pytest.raises(agent.PathError, match="does not exist"):
        agent.explain_payload(run, "/definitely/not/here")


def test_the_detail_layer_names_its_sources():
    record = agent.place(_run().roots[0], detail=True)
    assert record["write_checked_by"] == "os.access"
    assert record["restore"] == "cp -an /snap/daily-2/home/me/. /home/me/"
    assert record["symlinks_out"][0]["link"] == "/home/me/.conda"
    assert record["unknown"] == {}


def test_a_restore_command_survives_a_space_in_the_path():
    """An agent RUNS this line, so it must be one argument per path."""
    assert cli._copy_back("/snap/a b", "/home/me/my data") == (
        "cp -an '/snap/a b/.' '/home/me/my data/'"
    )


# --------------------------------------------------------------------------
# Knowing an agent is driving
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "environ, driven",
    [
        ({}, False),
        ({"CLAUDECODE": "1"}, True),
        ({"AI_AGENT": "claude-code_2-1_agent"}, True),
        ({"GEMINI_CLI": "1"}, True),
        # What a Codex shell and an opencode shell really carry.
        ({"CODEX_THREAD_ID": "019d9c1e-25a4-79e2-9c10-c4a9e17e78ee"}, True),
        ({"CODEX_SANDBOX_NETWORK_DISABLED": "1"}, True),
        ({"OPENCODE": "1"}, True),
        ({"DIRSCAPE_AGENT": "1"}, True),
        ({"CLAUDECODE": ""}, False),
        # A person's own profile exports these, so they prove nothing.
        ({"OPENCODE_API_KEY": "sk-x", "CODEX_HOME": "/home/me/.codex"}, False),
    ],
)
def test_an_agent_harness_is_recognised(environ, driven):
    assert cli.agent_driven(environ) is driven


def test_an_agent_is_pointed_at_the_exact_figures_and_a_person_is_not(monkeypatch, capsys):
    for name in cli.AGENT_VARIABLES:
        monkeypatch.delenv(name, raising=False)
    _code, _out, err, _calls = _drive(monkeypatch, capsys, ["--no-state"])
    assert "paths --json" not in err

    monkeypatch.setenv("DIRSCAPE_AGENT", "1")
    _code, out, err, _calls = _drive(monkeypatch, capsys, ["--no-state"])
    assert "dirscape paths --json" in err
    assert "paths --json" not in out, "the table itself is untouched"


def test_an_agent_never_gets_the_browser(monkeypatch, capsys):
    """In a pty nobody is there to press q, so the command would hang."""
    monkeypatch.setenv("DIRSCAPE_AGENT", "1")
    monkeypatch.setattr(cli.interactive, "supported", lambda stream=None: True)

    def browse(*_args, **_kwargs):
        raise AssertionError("the browser must not start under an agent")

    monkeypatch.setattr(cli, "_browse", browse)
    code, out, _err, _calls = _drive(monkeypatch, capsys, [])
    assert code == cli.EXIT_OK and "/home/me" in out
