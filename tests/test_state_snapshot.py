"""The lineage: where `first_seen` comes from, and what keeps the file small.

Newness is decided by this file and by nothing else, because this cluster
supplies no usable creation timestamp. Both disproving measurements are in
`snapshot.py`'s docstring and one of them is asserted here as a source level
guard: birth time is unavailable (``stat -c %W`` returns 0 and ``%w`` returns
``-`` on every GPFS path and on /home) and directory mtime is a decoy
(/project/aarnold's fileset first appears in the site quota archive between
2026-03-01 and 2026-04-01 while its directory mtime reads two months later).

So `first_seen` is carried forward, and a test that it survives repeated saves
is a test of the headline feature: recomputing it would make everything
permanently new, which is the same as having no feature at all.
"""

import ast
import json
import os
import stat

from dirscape.model import (
    QuotaRow,
    QuotaSnapshot,
    Reach,
    Root,
    confirmed,
)
from dirscape.state.diff import diff
from dirscape.state.snapshot import (
    KEEP_RECENT,
    MAX_BYTES,
    Lineage,
    Snapshot,
    cutoff_for,
    default_path,
    state_dir,
)

T0 = 1757000000.0
DAY = 86400.0

SOURCES = [
    os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
        "src",
        "dirscape",
        "state",
        name,
    )
    for name in ("snapshot.py", "diff.py", "__init__.py")
]


def _root(
    path="/project/x",
    device="gpfs1",
    identity=(2049, 101),
    reach=Reach.LISTABLE,
    used=None,
    limit=None,
    inodes=None,
    inode_limit=None,
):
    root = Root(path, role="project", device=device)
    root.fileset = "proj-x"
    root.identity = identity
    root.reach = reach
    root.reach_reason = "os.access reports read and execute"
    root.mounted = confirmed("present in /proc/self/mounts", source="mounts")
    root.present = confirmed("stat succeeded", source="os.stat")
    root.add_source("mounts")
    rows = []
    if used is not None:
        rows.append(
            QuotaRow("proj-x", "blocks", "group", used=used, hard=limit, mount=path, device=device)
        )
    if inodes is not None:
        rows.append(
            QuotaRow(
                "proj-x",
                "files",
                "group",
                used=inodes,
                hard=inode_limit,
                mount=path,
                device=device,
            )
        )
    if rows:
        root.quota = QuotaSnapshot("mmlsquota", rows)
    return root


def _snapshot(roots, taken_at=T0, lineage=None, hostname="meadow3-login3"):
    return Snapshot.from_roots(
        roots,
        lineage=lineage,
        taken_at=taken_at,
        hostname=hostname,
        node_class="login",
        cluster_fingerprint="ab12cd34ef",
    )


# --------------------------------------------------------------------------
# 4. The first run
# --------------------------------------------------------------------------


def test_the_first_run_reports_nothing_and_says_why():
    """Test 4. No baseline means no changes, plus an explanation.

    Labelling every root `new` on a first run is the tempting alternative and it
    is wrong twice over: it is a page of rows carrying no information, and it
    teaches a user to skim past the one label that means "look at this". A
    silent empty list is also wrong, because the user asked what changed and
    deserves to know why the answer is nothing.
    """
    current = _snapshot([_root(), _root(path="/project/y", identity=(2049, 102))])

    result = diff(None, current)

    assert list(result) == []
    assert len(result) == 0
    assert result.no_baseline is True
    assert result.warnings, "the renderer needs something to print"
    assert "baseline" in result.warnings[0]
    assert "2 roots" in result.warnings[0], "and it should say what was seeded"


def test_a_previous_run_that_found_nothing_is_not_a_baseline():
    """An empty snapshot cannot be compared against.

    A run whose discovery failed entirely stores zero roots. Treating that as a
    baseline would label every root `new` on the next run, which is the same
    false claim as doing it on the first run, arriving through a different door.
    """
    empty = Snapshot(taken_at=T0, hostname="h", node_class="login", records=[])
    result = diff(empty, _snapshot([_root()], taken_at=T0 + DAY))
    assert list(result) == []
    assert result.no_baseline is True


# --------------------------------------------------------------------------
# 5. first_seen
# --------------------------------------------------------------------------


def test_first_seen_survives_three_saves(tmp_path):
    """Test 5. The headline feature, asserted end to end through the file.

    Recomputing `first_seen` on every run would make every root permanently
    new. The value has to come out of the lineage, survive a save and a load,
    and keep coming out of it.
    """
    path = str(tmp_path / "lineage.json")

    for run, when in enumerate((T0, T0 + DAY, T0 + 2 * DAY)):
        lineage = Lineage.load(path=path)
        snapshot = _snapshot([_root()], taken_at=when, lineage=lineage)
        record = snapshot.record_for("gpfs1", "/project/x")
        assert record is not None
        assert record.first_seen == T0, (
            "run %d dated the root %r; first_seen must stay at the first run that "
            "saw it" % (run + 1, record.first_seen)
        )
        lineage.append(snapshot)
        assert lineage.save(path=path) is True

    assert len(Lineage.load(path=path)) == 3


def test_first_seen_is_written_back_onto_the_live_root(tmp_path):
    """`Root.first_seen` must agree with the lineage, not stay empty.

    The renderer holds the `Root`, and the lineage is the only thing that knows
    the date, so `--json` and the diff would otherwise disagree about how old a
    root is.
    """
    path = str(tmp_path / "lineage.json")
    lineage = Lineage.load(path=path)
    lineage.append(_snapshot([_root()], taken_at=T0, lineage=lineage))
    lineage.save(path=path)

    lineage = Lineage.load(path=path)
    root = _root()
    assert root.first_seen is None
    _snapshot([root], taken_at=T0 + DAY, lineage=lineage)
    assert root.first_seen == T0


def test_a_one_run_gap_does_not_re_date_a_root():
    """A root whose probe timed out for one run must not come back as new.

    `first_seen_for` scans the whole lineage rather than the previous entry
    alone, which is what makes a single gap harmless. Reading only the previous
    entry would re-date the root and report it as `new`, from a timeout.
    """
    lineage = Lineage()
    lineage.append(_snapshot([_root()], taken_at=T0, lineage=lineage))
    # Run two never saw it at all.
    lineage.append(
        _snapshot(
            [_root(path="/project/other", identity=(2049, 7))], taken_at=T0 + DAY, lineage=lineage
        )
    )
    back = _snapshot([_root()], taken_at=T0 + 2 * DAY, lineage=lineage)
    record = back.record_for("gpfs1", "/project/x")
    assert record is not None
    assert record.first_seen == T0


def test_a_renamed_directory_keeps_its_first_seen():
    """Matched on the inode when the path misses, because it is the same object."""
    lineage = Lineage()
    lineage.append(_snapshot([_root(path="/project/old")], taken_at=T0, lineage=lineage))
    moved = _snapshot([_root(path="/project/new")], taken_at=T0 + DAY, lineage=lineage)
    record = moved.record_for("gpfs1", "/project/new")
    assert record is not None
    assert record.first_seen == T0, "a rename is not a new allocation"


def test_a_genuinely_new_root_is_dated_by_this_run():
    """The control: carrying forward must not mean never dating anything."""
    lineage = Lineage()
    lineage.append(_snapshot([_root()], taken_at=T0, lineage=lineage))
    later = _snapshot(
        [_root(), _root(path="/project/fresh", identity=(2049, 500))],
        taken_at=T0 + DAY,
        lineage=lineage,
    )
    fresh = later.record_for("gpfs1", "/project/fresh")
    assert fresh is not None
    assert fresh.first_seen == T0 + DAY


BANNED_TIMESTAMPS = ("st_mtime", "st_ctime", "st_birthtime", "st_atime", "getmtime", "getctime")


def _code_identifiers(path):
    """Every name, attribute and non-docstring literal in a module.

    Docstrings and comments are excluded deliberately, because naming the
    disproven fields in prose is the whole point of documenting why they are not
    used. Only code counts, so the guard reads `os.stat(p).st_mtime` as an
    attribute and `getattr(s, "st_mtime")` as a literal, and reads the module
    docstring as nothing at all.
    """
    with open(path) as handle:
        tree = ast.parse(handle.read())
    docstrings = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            body = getattr(node, "body", [])
            if body and isinstance(body[0], ast.Expr):
                value = body[0].value
                if isinstance(value, ast.Constant) and isinstance(value.value, str):
                    docstrings.add(id(value))
    found = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Attribute):
            found.add(node.attr)
        elif isinstance(node, ast.Name):
            found.add(node.id)
        elif (
            isinstance(node, ast.Constant)
            and isinstance(node.value, str)
            and id(node) not in docstrings
        ):
            found.add(node.value)
    return found


def test_no_source_file_reads_a_filesystem_timestamp():
    """The guard that stops the disproven heuristic being re-added.

    Measured and recorded in `snapshot.py`'s docstring: there is no birth time
    to read here, and directory mtime runs two months behind the truth on at
    least one measured fileset. A future contributor reaching for `st_mtime` to
    answer "is this new" would be reintroducing a known wrong answer, so the
    tokens are banned in code rather than only argued against in prose.
    """
    for path in SOURCES:
        identifiers = _code_identifiers(path)
        found = sorted(token for token in BANNED_TIMESTAMPS if token in identifiers)
        assert found == [], "%s reads %s in code; newness comes from the lineage" % (
            os.path.basename(path),
            found,
        )


def test_the_timestamp_guard_would_notice_a_real_use(tmp_path):
    """The control: a guard that cannot fail is not a guard.

    The banned tokens appear in `snapshot.py`'s docstring by design, so a naive
    substring scan fails on the documentation and a scan that excludes too much
    passes on anything. This pins both ends.
    """
    probe = tmp_path / "probe.py"
    probe.write_text(
        '"""A docstring mentioning st_birthtime, which is allowed."""\n'
        "import os\n"
        "def age(path):\n"
        "    return os.stat(path).st_mtime\n"
    )
    identifiers = _code_identifiers(str(probe))
    assert "st_mtime" in identifiers, "the guard must see a real attribute access"
    assert "st_birthtime" not in identifiers, "and must not see a docstring"


# --------------------------------------------------------------------------
# 7. Retention
# --------------------------------------------------------------------------


def _forty_roots(run):
    """A realistic entry: the 13 quota rows this login node lists, plus mounts."""
    roots = []
    for index in range(40):
        roots.append(
            _root(
                path="/project/proj%02d" % (index,),
                identity=(2049, 1000 + index),
                used=(index + 1) * 1024**3 + run,
                limit=200 * 1024**3,
                inodes=10000 + index,
                inode_limit=300000,
            )
        )
    return roots


def test_retention_keeps_the_newest_n_plus_the_oldest(tmp_path):
    """Test 7. Fifty runs, and the file stays bounded and still spans the history.

    The anchor is the point. Thirty daily runs fill the recent window, and
    without the oldest entry `--since 90d` would silently become `--since 30d`
    and answer a question the user did not ask.
    """
    path = str(tmp_path / "lineage.json")
    lineage = Lineage()
    for run in range(50):
        lineage.append(_snapshot(_forty_roots(run), taken_at=T0 + run * DAY))
    assert lineage.save(path=path) is True

    reloaded = Lineage.load(path=path)
    assert len(reloaded) == KEEP_RECENT + 1, "the newest %d plus the anchor" % (KEEP_RECENT,)
    assert reloaded.entries[0].taken_at == T0, "the oldest run is the anchor and is kept"
    assert reloaded.entries[-1].taken_at == T0 + 49 * DAY, "and the newest run is the newest"
    # The middle is what was dropped: run 1 through run 18 are gone.
    stamps = [entry.taken_at for entry in reloaded]
    assert stamps == sorted(stamps)
    assert T0 + DAY not in stamps

    size = os.path.getsize(path)
    assert size < MAX_BYTES, "the lineage is %d bytes, over the %d ceiling" % (size, MAX_BYTES)


def test_the_size_ceiling_drops_from_the_middle_not_the_ends(tmp_path):
    """A run that finds hundreds of roots is what the count bound cannot see.

    Dropping from index 1 keeps the anchor and keeps the recent tail dense,
    because resolution matters most near the present: `--since 7d` is asked far
    more often than `--since 90d`, and the anchor is all the latter needs.
    """
    lineage = Lineage()
    for run in range(12):
        roots = [
            _root(
                path="/project/p%04d" % (index,),
                identity=(2049, index),
                used=index * 1024**3,
                limit=10 * 1024**4,
            )
            for index in range(400)
        ]
        lineage.append(_snapshot(roots, taken_at=T0 + run * DAY))

    blob = json.dumps(lineage.to_json(), separators=(",", ":"))
    assert len(blob) <= MAX_BYTES
    assert len(lineage) < 12, "something had to be dropped for this to be a test"
    assert lineage.entries[0].taken_at == T0, "never the anchor"
    assert lineage.entries[-1].taken_at == T0 + 11 * DAY, "never the newest"
    assert lineage.notes, "and the user is told the history was thinned"


def test_baseline_for_prefers_the_newest_entry_inside_the_window():
    """`--since 90d` must compare against the whole window, not against yesterday."""
    lineage = Lineage()
    for run in range(5):
        lineage.append(_snapshot([_root()], taken_at=T0 + run * DAY))

    cutoff = cutoff_for(2 * DAY, now=T0 + 4 * DAY)
    chosen = lineage.baseline_for(cutoff)
    assert chosen is not None
    assert chosen.taken_at == T0 + 2 * DAY

    # Nothing that old: the anchor is the best available, and the caller can see
    # it is younger than the cutoff from its own taken_at.
    older = lineage.baseline_for(cutoff_for(900 * DAY, now=T0 + 4 * DAY))
    assert older is not None
    assert older.taken_at == T0


# --------------------------------------------------------------------------
# 8. Round trip
# --------------------------------------------------------------------------


def test_save_load_and_diff_against_itself_is_silent(tmp_path):
    """Test 8. A round trip must not invent a change.

    Every field that survives the file has to survive it exactly, because the
    next run compares against what was written and not against what was in
    memory. A dropped `reach`, a number that reloads as 0 instead of None, or a
    verdict that loses its category all show up here as a change that did not
    happen.
    """
    path = str(tmp_path / "lineage.json")
    roots = [
        _root(used=4 * 1024**3, limit=200 * 1024**3, inodes=36008, inode_limit=300000),
        _root(path="/project/y", identity=(2049, 102), reach=Reach.TRAVERSE),
        _root(path="/project/z", identity=(2049, 103), reach=Reach.CLOSED),
    ]
    original = _snapshot(roots, taken_at=T0)
    lineage = Lineage()
    lineage.append(original)
    assert lineage.save(path=path) is True

    reloaded = Lineage.load(path=path).latest()
    assert reloaded is not None

    assert list(diff(reloaded, reloaded)) == [], "a snapshot must not differ from itself"
    assert list(diff(reloaded, original)) == [], "nor from what it was written from"
    assert list(diff(original, reloaded)) == []

    # And the fields really did survive, rather than all collapsing to defaults
    # in a way that happens to compare equal.
    record = reloaded.record_for("gpfs1", "/project/x")
    assert record is not None
    assert record.reach == Reach.LISTABLE
    assert record.identity == (2049, 101)
    assert record.used_bytes == 4 * 1024**3
    assert record.used_inodes == 36008
    assert record.limit_inodes == 300000
    assert record.present.confirmed is True
    assert record.first_seen == T0
    assert reloaded.record_for("gpfs1", "/project/z").reach == Reach.CLOSED


def test_an_unreadable_verdict_reloads_as_unknown_not_as_a_refusal(tmp_path):
    """A corrupt category must degrade to "I do not know", never to "no".

    `durable` is defined as "not in the transient set", so an unrecognised
    token would read as a durable refusal and a hand edited or truncated file
    could manufacture a `closed`.
    """
    path = tmp_path / "lineage.json"
    lineage = Lineage()
    lineage.append(_snapshot([_root()], taken_at=T0))
    lineage.save(path=str(path))

    payload = json.loads(path.read_text())
    payload["entries"][0]["roots"][0]["present"] = [False, "TOTAL_NONSENSE"]
    payload["entries"][0]["roots"][0]["reach"] = "WIDE_OPEN"
    path.write_text(json.dumps(payload))

    record = Lineage.load(path=str(path)).latest().record_for("gpfs1", "/project/x")
    assert record is not None
    assert record.present.durable is False
    assert record.present.refuted is False
    assert record.reach == Reach.UNKNOWN


def test_a_foreign_or_future_file_is_not_used_as_a_baseline(tmp_path):
    """Half understood history is worse than none: it reports changes that did not happen."""
    path = tmp_path / "lineage.json"

    path.write_text(json.dumps({"tool": "something-else", "entries": []}))
    assert len(Lineage.load(path=str(path))) == 0
    assert Lineage.load(path=str(path)).notes

    path.write_text(json.dumps({"tool": "dirscape", "schema": 9999, "entries": [{}]}))
    lineage = Lineage.load(path=str(path))
    assert len(lineage) == 0
    assert "schema" in lineage.notes[0]

    path.write_text("{not json at all")
    assert len(Lineage.load(path=str(path))) == 0

    assert len(Lineage.load(path=str(tmp_path / "does-not-exist.json"))) == 0


# --------------------------------------------------------------------------
# Where the file lives, and who can read it
# --------------------------------------------------------------------------


def test_the_default_location_honours_xdg_state_home(monkeypatch, tmp_path):
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "xdg"))
    assert state_dir() == str(tmp_path / "xdg" / "dirscape")
    assert default_path("ab12cd34ef") == str(tmp_path / "xdg" / "dirscape" / "ab12cd34ef.json")

    monkeypatch.delenv("XDG_STATE_HOME", raising=False)
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    assert state_dir() == str(tmp_path / "home" / ".local" / "state" / "dirscape")


def test_the_state_directory_and_file_are_private(monkeypatch, tmp_path):
    """0o700 and 0o600, because the lineage is a map of somebody's research.

    It lists every path the user can reach, with usage figures. On a shared
    cluster with world readable homes that is not a file another account needs.
    """
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "xdg"))
    lineage = Lineage()
    lineage.append(_snapshot([_root()], taken_at=T0))
    assert lineage.save(fingerprint="ab12cd34ef") is True

    where = default_path("ab12cd34ef")
    assert os.path.exists(where)
    assert stat.S_IMODE(os.stat(os.path.dirname(where)).st_mode) == 0o700
    assert stat.S_IMODE(os.stat(where).st_mode) == 0o600
    assert not [name for name in os.listdir(os.path.dirname(where)) if name.endswith(".tmp")]


def test_a_fingerprint_cannot_escape_the_state_directory(monkeypatch, tmp_path):
    """The fingerprint is foreign text: it can be derived from a hostname.

    A component containing a separator would write outside the state directory.
    The filter is not injective, and that is safe because the fingerprint is
    also stored inside the file, so a collision is caught by
    `fingerprint_mismatch` on load rather than silently diffed.
    """
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "xdg"))
    where = default_path("../../etc/passwd")
    assert os.path.dirname(where) == state_dir()
    assert ".." not in os.path.basename(where)
    assert default_path("").endswith("default.json")


def test_save_returns_false_rather_than_raising(tmp_path):
    """A state file that cannot be written costs next run's baseline, not this run.

    A full quota, a read only home and a directory owned by somebody else are
    all ordinary on a cluster, and none of them is a reason to fail a run that
    has already done its work. The boolean is so the renderer can say so.
    """
    blocked = tmp_path / "blocked"
    blocked.mkdir()
    os.chmod(str(blocked), 0o500)
    try:
        lineage = Lineage()
        lineage.append(_snapshot([_root()], taken_at=T0))
        assert lineage.save(path=str(blocked / "sub" / "lineage.json")) is False
    finally:
        os.chmod(str(blocked), 0o700)


def test_an_unmeasured_quota_is_not_a_usage_of_zero(tmp_path):
    """None must survive the round trip as None.

    A zero renders as an empty bar, which reads as "plenty of room", and it
    would make the next run report a `grew` from nothing the moment the backend
    starts answering again.
    """
    root = _root()
    assert root.quota is None
    path = str(tmp_path / "lineage.json")
    lineage = Lineage()
    lineage.append(_snapshot([root], taken_at=T0))
    lineage.save(path=path)

    record = Lineage.load(path=path).latest().record_for("gpfs1", "/project/x")
    assert record is not None
    assert record.used_bytes is None
    assert record.limit_bytes is None

    # And a backend that failed outright is the same answer, not a zero.
    failed = _root(path="/project/f", identity=(2049, 9))
    failed.quota = QuotaSnapshot("mmlsquota", [], available=False, reason="tslsquota failed")
    snapshot = _snapshot([failed], taken_at=T0)
    assert snapshot.record_for("gpfs1", "/project/f").used_bytes is None
