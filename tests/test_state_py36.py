"""Guard this package's own hard constraints, scoped to `src/dirscape/state/`.

Three claims are made about these two modules and none of them is checkable by
running the suite, because the suite runs on 3.11 with everything installed:

1. **They parse and run on Python 3.6.** ``requires-python = ">=3.6"`` is not a
   courtesy. The moment this tool is most useful is the first hour on an
   unfamiliar cluster, on the bare ``/usr/bin/python3`` of a login node, before
   any conda env exists. On RHEL8 that is 3.6.8, and it is present on this node,
   so the real interpreter is used rather than only a syntax proxy. rapiDU broke
   this exact claim once: setuptools-scm's stock ``version_file`` template opens
   with ``from __future__ import annotations``, a SyntaxError on 3.6, which took
   down every import of the package.
2. **They import nothing outside `json`, `os`, `time` and `typing`.** The
   package's deployability rests on it, and one ``import rich`` would end it on
   an interpreter where the user cannot install anything.
3. **There is no em-dash anywhere in them.** A style rule for the whole repo,
   and the only place it can be enforced is a test.
"""

import ast
import os
import subprocess
import sys

import pytest

STATE = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src", "dirscape", "state"
)
SRC = os.path.dirname(os.path.dirname(STATE))
TESTS = os.path.dirname(os.path.abspath(__file__))

MIN_FEATURE_VERSION = (3, 6)

#: The whole allowed import surface. `typing` is invisible to ruff because these
#: modules use type COMMENTS rather than annotations, which is itself forced by
#: the 3.6 floor, but mypy reads those comments and needs the names imported, so
#: the import is load-bearing and not dead.
ALLOWED_IMPORTS = frozenset(["json", "os", "time", "typing"])

#: Stdlib modules that did NOT exist at 3.6. `sys.stdlib_module_names` is the
#: RUNNING interpreter's, so it answers "is this stdlib in 3.11" and cannot
#: answer "was it stdlib in 3.6": `tomllib` would pass a third-party audit while
#: raising ModuleNotFoundError on the login node interpreter the whole claim is
#: about.
TOO_NEW_FOR_THE_FLOOR = {
    "dataclasses": (3, 7),
    "contextvars": (3, 7),
    "zoneinfo": (3, 9),
    "graphlib": (3, 9),
    "tomllib": (3, 11),
}

#: Built from codepoints rather than written out, so enforcing the rule does not
#: break it. U+2014 em-dash, U+2013 en-dash, U+2015 horizontal bar: the last two
#: are what somebody reaches for next when the first one is taken away.
BANNED_DASHES = (
    ("em-dash", chr(0x2014)),
    ("en-dash", chr(0x2013)),
    ("horizontal bar", chr(0x2015)),
)


def _sources():
    return sorted(os.path.join(STATE, name) for name in os.listdir(STATE) if name.endswith(".py"))


def _test_files():
    return sorted(
        os.path.join(TESTS, name)
        for name in os.listdir(TESTS)
        if name.startswith("test_state_") and name.endswith(".py")
    )


def _read(path):
    with open(path) as handle:
        return handle.read()


def test_there_are_sources_to_check():
    """A silent glob miss would make every test below vacuously pass."""
    names = [os.path.basename(path) for path in _sources()]
    assert "snapshot.py" in names
    assert "diff.py" in names
    assert "__init__.py" in names


@pytest.mark.parametrize("path", _sources(), ids=os.path.basename)
def test_parses_under_python36(path):
    """`feature_version` is 3.8+, so on the floor itself the real parser is used.

    A guard for the floor interpreter that cannot run on the floor interpreter
    is rapiDU's RD-10. Where the parameter is unavailable, the running
    interpreter's own parser answers the same question.
    """
    kwargs = {}
    if sys.version_info >= (3, 8):
        kwargs["feature_version"] = MIN_FEATURE_VERSION
    try:
        ast.parse(_read(path), filename=path, **kwargs)
    except SyntaxError as exc:
        pytest.fail(
            "%s is not valid Python 3.6 syntax: %s (line %s)"
            % (os.path.basename(path), exc.msg, exc.lineno)
        )


@pytest.mark.parametrize("path", _sources(), ids=os.path.basename)
def test_no_future_annotations_import(path):
    """`ast.parse(feature_version=...)` does not reject it, so it needs its own check."""
    for node in ast.walk(ast.parse(_read(path))):
        if isinstance(node, ast.ImportFrom) and node.module == "__future__":
            names = [alias.name for alias in node.names]
            assert "annotations" not in names, (
                "%s imports `annotations` from __future__, a SyntaxError on 3.6"
                % (os.path.basename(path),)
            )


@pytest.mark.parametrize("path", _sources(), ids=os.path.basename)
def test_no_pep604_unions_and_no_dataclasses(path):
    """`int | str` raises at runtime before 3.10, and dataclasses do not exist at 3.6.

    Annotations are evaluated eagerly without the future import, so a PEP 604
    union in a module or class level annotation raises on import rather than
    failing a type check.
    """
    tree = ast.parse(_read(path))
    for node in ast.walk(tree):
        annotation = getattr(node, "annotation", None) or getattr(node, "returns", None)
        if annotation is None:
            continue
        for sub in ast.walk(annotation):
            assert not (isinstance(sub, ast.BinOp) and isinstance(sub.op, ast.BitOr)), (
                "%s uses a PEP 604 union, which raises on Python < 3.10" % (os.path.basename(path),)
            )
    for node in ast.walk(tree):
        for decorator in getattr(node, "decorator_list", []):
            assert "dataclass" not in ast.dump(decorator), "%s uses a dataclass, which is 3.7+" % (
                os.path.basename(path),
            )


@pytest.mark.parametrize("path", _sources(), ids=os.path.basename)
def test_the_import_surface_is_exactly_the_declared_one(path):
    """Absolute imports must stay inside `ALLOWED_IMPORTS`.

    Covers both halves at once: a third-party package fails, and so does a
    stdlib module that post-dates the floor, which a `sys.stdlib_module_names`
    audit on a modern interpreter cannot catch.

    Relative imports are not checked here; `test_state_layering` below has an
    opinion about those.
    """
    found = set()
    for node in ast.walk(ast.parse(_read(path))):
        if isinstance(node, ast.Import):
            found |= {alias.name.split(".")[0] for alias in node.names}
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            found.add(node.module.split(".")[0])
    outside = sorted(found - ALLOWED_IMPORTS)
    assert outside == [], "%s imports %s; the declared surface is %s" % (
        os.path.basename(path),
        outside,
        sorted(ALLOWED_IMPORTS),
    )
    too_new = sorted(name for name in found if name in TOO_NEW_FOR_THE_FLOOR)
    assert too_new == [], "%s imports %s, which post-date the 3.6 floor" % (
        os.path.basename(path),
        too_new,
    )


@pytest.mark.parametrize("path", _sources(), ids=os.path.basename)
def test_state_layering(path):
    """`state/` may import `model` and its own siblings, and nothing else.

    The cluster fingerprint, the hostname and the node class all come from
    `discover/`, and they arrive as arguments rather than as an import. Reaching
    into discovery would make the dependency two way and would stop a renderer
    test from building a snapshot without a mount table.
    """
    for node in ast.walk(ast.parse(_read(path))):
        if not isinstance(node, ast.ImportFrom) or not node.level:
            continue
        target = "%s%s" % ("." * node.level, node.module or "")
        assert target in (
            "..model",
            ".snapshot",
            ".diff",
        ), "%s imports %s; state/ depends on model and on its own modules only" % (
            os.path.basename(path),
            target,
        )


@pytest.mark.parametrize("path", _sources() + _test_files(), ids=os.path.basename)
def test_no_em_dash_anywhere(path):
    """A repo wide style rule, enforceable only here."""
    text = _read(path)
    for name, char in BANNED_DASHES:
        assert char not in text, "%s contains a %s" % (os.path.basename(path), name)


def test_the_dash_guard_would_notice_one(tmp_path):
    """The control: a guard built from codepoints must still match the character."""
    probe = tmp_path / "probe.py"
    probe.write_text("# a comment with an %s in it\n" % (chr(0x2014),))
    text = probe.read_text()
    assert any(char in text for _name, char in BANNED_DASHES)


@pytest.mark.skipif(
    not os.path.exists("/usr/bin/python3"), reason="no system python3 to test against"
)
def test_it_actually_works_under_the_system_interpreter():
    """The real claim. Every check above is a fast proxy for this one.

    Not just an import: a save, a load and a diff, because a package that
    imports on 3.6 and then raises on a dict ordering assumption or on a
    `subprocess` keyword is no more deployable than one that will not import.
    """
    script = """
import json, os, sys, tempfile
from dirscape.model import Reach, Root, VerdictCategory, confirmed, unknown
from dirscape.state.snapshot import Lineage, Snapshot
from dirscape.state.diff import diff

def root(path, reach, present):
    r = Root(path, role="project", device="gpfs1")
    r.identity = (2049, 101)
    r.reach = reach
    r.reach_reason = "os.access reports read and execute"
    r.present = present
    r.add_source("mounts")
    return r

ok = confirmed("stat succeeded", source="os.stat")
directory = tempfile.mkdtemp()
where = os.path.join(directory, "lineage.json")

lineage = Lineage.load(path=where)
first = Snapshot.from_roots([root("/project/x", Reach.LISTABLE, ok)], lineage=lineage,
                            taken_at=1000.0, hostname="h1", node_class="login",
                            cluster_fingerprint="fp")
lineage.append(first)
assert lineage.save(path=where) is True

lineage = Lineage.load(path=where)
assert len(lineage) == 1
timed_out = unknown(VerdictCategory.PROBE_TIMEOUT, "stat hung")
second = Snapshot.from_roots([root("/project/x", Reach.UNKNOWN, timed_out)], lineage=lineage,
                             taken_at=2000.0, hostname="h1", node_class="login",
                             cluster_fingerprint="fp")
result = diff(lineage.latest(), second)
assert [c.label for c in result] == ["unknown"], [c.label for c in result]
assert result.records[0].current == Reach.LISTABLE
assert "closed" not in json.dumps(result.to_json())
assert second.record_for("gpfs1", "/project/x").first_seen == 1000.0
print("ok on " + ".".join(str(n) for n in sys.version_info[:3]))
"""
    env = dict(os.environ)
    env["PYTHONPATH"] = SRC
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    proc = subprocess.Popen(
        ["/usr/bin/python3", "-c", script],
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        universal_newlines=True,
        env=env,
    )
    out, _ = proc.communicate()
    assert proc.returncode == 0, "the state package failed under /usr/bin/python3:\n%s" % (out,)
    assert out.startswith("ok on 3."), out


def test_the_system_interpreter_really_is_old_enough_to_be_a_test():
    """If /usr/bin/python3 were modern, the test above would pass trivially.

    Stated rather than assumed: on this node it is 3.6.8, which is the floor
    itself, so that test is the strongest possible form of the claim. Where it
    is newer, this records that the check has become weaker.
    """
    if not os.path.exists("/usr/bin/python3"):
        pytest.skip("no system python3")
    proc = subprocess.Popen(
        ["/usr/bin/python3", "-c", "import sys; print('%d.%d' % sys.version_info[:2])"],
        stdout=subprocess.PIPE,
        universal_newlines=True,
    )
    out, _ = proc.communicate()
    major, minor = (int(part) for part in out.strip().split("."))
    assert (major, minor) >= MIN_FEATURE_VERSION
    if (major, minor) > (3, 6):
        pytest.skip("system python3 is %s, newer than the 3.6 floor claimed" % (out.strip(),))
