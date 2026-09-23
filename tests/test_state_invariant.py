"""THE invariant: a failed probe never produces a state-change claim.

These are the most important tests in the package, and the failure they prevent
is specific. A user is told they have lost access to a directory when in truth
one ``stat`` timed out. They start restoring from backup, or open a ticket, or
believe a filesystem has gone. That is a false alarm about data loss, which is
the worst output this tool could produce, and it is the inverse of nodetop's
NT-1, where a replay restored a "the probe ran" flag without the probe's result
and 21 queues measured as refusing came back rendered as fine.

Test 1 and test 3 are a matched pair and neither is worth much alone. Test 1
asserts that an inconclusive run produces `unknown` and never `closed`; test 3
asserts that a genuinely refused run still produces `closed`. A bug that
suppressed the label entirely would pass test 1 and fail test 3, and a diff that
never claims anything is not safe, it is useless.
"""

import ast
import json
import os
import sys

import pytest

from dirscape.model import (
    TRANSIENT_CATEGORIES,
    QuotaRow,
    QuotaSnapshot,
    Reach,
    Root,
    VerdictCategory,
    confirmed,
    refuted,
    unknown,
)
from dirscape.state.diff import (
    CLOSED,
    GONE,
    LABELS,
    NEW,
    OPENED,
    UNKNOWN,
    describe,
    diff,
)
from dirscape.state.snapshot import Snapshot

T0 = 1757000000.0  # a fixed clock, so no assertion here depends on "now"
T1 = T0 + 86400.0

DIFF_SOURCE = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "src",
    "dirscape",
    "state",
    "diff.py",
)


def _root(
    path="/project/x",
    reach=Reach.LISTABLE,
    reach_reason="os.access reports read and execute",
    present=None,
    device="gpfs1",
    identity=(2049, 101),
    used=None,
    limit=None,
    stranded=False,
):
    """A `Root` shaped the way discovery produces one."""
    root = Root(path, role="project", device=device)
    root.fileset = "proj-x"
    root.identity = identity
    root.reach = reach
    root.reach_reason = reach_reason
    root.mounted = confirmed("present in /proc/self/mounts", source="mounts")
    root.allocated = confirmed("listed by the allocation database", source="allocation")
    root.present = present or confirmed("stat succeeded", source="os.stat")
    root.stranded = stranded
    root.add_source("mounts")
    if used is not None:
        row = QuotaRow(
            "proj-x", "blocks", "group", used=used, hard=limit, mount=path, device=device
        )
        root.quota = QuotaSnapshot("mmlsquota", [row])
    return root


def _snapshot(roots, taken_at=T0, hostname="meadow3-login3", node_class="login", discovery=None):
    return Snapshot.from_roots(
        roots,
        taken_at=taken_at,
        hostname=hostname,
        node_class=node_class,
        cluster_fingerprint="ab12cd34ef",
        discovery=discovery,
    )


def _blob(result):
    """Everything the result can put in front of a user, as one string."""
    return json.dumps(result.to_json()) + " ".join(change.because for change in result)


# --------------------------------------------------------------------------
# 1. Inconclusive now, conclusive before
# --------------------------------------------------------------------------


def test_a_timed_out_reach_probe_is_unknown_and_never_closed():
    """Test 1. The run could not answer, so the row says so and carries the old answer.

    This is the exact shape of the bug this package is written to avoid: the
    baseline measured LISTABLE, this run's `stat` timed out, and the naive
    comparison of two reach strings puts UNKNOWN below CLOSED in the ordering
    and reports a regression.
    """
    previous = _snapshot([_root()], taken_at=T0)
    current_root = _root(
        reach=Reach.UNKNOWN,
        reach_reason="probe did not finish within 1.00s",
        present=unknown(VerdictCategory.PROBE_TIMEOUT, "stat hung on a wedged mount"),
    )
    current = _snapshot([current_root], taken_at=T1)

    result = diff(previous, current)

    assert result.labels() == [UNKNOWN]
    change = result.records[0]
    assert change.current == Reach.LISTABLE, "the previous verdict must be carried forward"
    assert change.previous == Reach.LISTABLE
    assert change.observed == Reach.UNKNOWN, "and what this run measured must stay visible"
    assert change.carried_forward is True
    # The `because` names the probe rather than restating the label, because it
    # is what `dirscape why` prints.
    assert "probe did not finish within 1.00s" in change.because

    assert CLOSED not in result.labels()
    assert "closed" not in _blob(result), "an inconclusive run must not mention losing access"
    assert current_root.labels == [UNKNOWN]
    assert "closed" not in current_root.labels


@pytest.mark.parametrize("category", sorted(TRANSIENT_CATEGORIES))
def test_no_transient_category_can_produce_closed(category):
    """Every way of saying "I do not know", not just the timeout.

    `PROBE_TIMEOUT` is the one a developer thinks of; `NOT_PROBED` after a
    budget ran out and `PERMISSION_TO_ASK_DENIED` from a site wrapper are the
    ones that arrive in production. The invariant is about the whole transient
    set, so the test is too.
    """
    previous = _snapshot([_root()], taken_at=T0)
    current = _snapshot(
        [_root(reach=Reach.UNKNOWN, reach_reason="", present=unknown(category, "no answer"))],
        taken_at=T1,
    )
    result = diff(previous, current)
    assert result.labels() == [UNKNOWN]
    assert "closed" not in _blob(result)
    assert result.records[0].current == Reach.LISTABLE


def test_the_carried_forward_reach_is_not_written_onto_this_runs_root():
    """The carry forward must not become a claim that this run measured it.

    Writing the previous reach onto `Root.reach` would make the live object
    report a value with no probe behind it, which is NT-1 itself rather than its
    inverse. The carried value belongs on the `Change`, which is labelled
    `unknown`.
    """
    previous = _snapshot([_root()], taken_at=T0)
    current_root = _root(reach=Reach.UNKNOWN, present=unknown(VerdictCategory.PROBE_TIMEOUT))
    current = _snapshot([current_root], taken_at=T1)

    diff(previous, current)

    assert current_root.reach == Reach.UNKNOWN, "this run measured nothing and must still say so"
    assert current_root.reachable is True, "and an unanswered question must not remove the row"


# --------------------------------------------------------------------------
# 2. Conclusive now, inconclusive before
# --------------------------------------------------------------------------


def test_no_opened_without_a_conclusive_baseline():
    """Test 2. The symmetric case, and the one that looks like a missed feature.

    Learning that a root is listable is not the same as learning that access
    improved. `opened` is a claim about a change, and there is no conclusive
    baseline for it to have changed from, so the honest output is no row at all.
    """
    previous = _snapshot(
        [_root(reach=Reach.UNKNOWN, present=unknown(VerdictCategory.PROBE_TIMEOUT))],
        taken_at=T0,
    )
    current = _snapshot([_root(reach=Reach.LISTABLE)], taken_at=T1)

    result = diff(previous, current)

    assert OPENED not in result.labels()
    assert "opened" not in _blob(result)
    assert result.no_baseline is False, "there WAS a previous run; it just knew nothing"
    assert list(result) == []


def test_two_inconclusive_runs_report_nothing_rather_than_a_change():
    """Still unmeasured is not news, in either direction."""
    previous = _snapshot([_root(reach=Reach.UNKNOWN)], taken_at=T0)
    current = _snapshot([_root(reach=Reach.UNKNOWN)], taken_at=T1)
    result = diff(previous, current)
    assert list(result) == []


# --------------------------------------------------------------------------
# 3. The control: a real refusal still gets reported
# --------------------------------------------------------------------------


def test_a_durable_refusal_does_produce_closed():
    """Test 3. The control that proves test 1 is not passing vacuously.

    The probe answered, and the answer was no: `os.access` reported neither read
    nor execute on a path that is present and mounted. That is a durable
    refusal, and suppressing it would make the tool useless in the one case a
    user most needs it.
    """
    previous = _snapshot([_root(reach=Reach.LISTABLE)], taken_at=T0)
    current_root = _root(
        reach=Reach.CLOSED,
        reach_reason="os.access reports neither read nor execute",
        present=confirmed("stat succeeded", source="os.stat"),
    )
    current = _snapshot([current_root], taken_at=T1)

    result = diff(previous, current)

    assert result.labels() == [CLOSED]
    change = result.records[0]
    assert change.previous == Reach.LISTABLE
    assert change.current == Reach.CLOSED
    assert change.carried_forward is False
    assert "os.access" in change.because, "the evidence must name the probe"
    assert current_root.labels == [CLOSED]


def test_a_durable_access_denied_is_not_softened_into_unknown():
    """A stored verdict carrying ACCESS_DENIED is a refusal, not a gap.

    `ACCESS_DENIED` is deliberately outside `TRANSIENT_CATEGORIES`: re-asking
    gives the same answer. A diff that treated any non-OK category as doubt
    would swallow this.
    """
    previous = _snapshot([_root(reach=Reach.LISTABLE)], taken_at=T0)
    current = _snapshot(
        [
            _root(
                reach=Reach.CLOSED,
                reach_reason="os.access reports neither read nor execute",
                present=refuted(VerdictCategory.ACCESS_DENIED, "EACCES on the parent"),
            )
        ],
        taken_at=T1,
        discovery=confirmed("swept 12 mounts", source="mounts"),
    )
    result = diff(previous, current)
    assert CLOSED in result.labels()


def test_traverse_only_is_a_regression_from_listable():
    """The middle state is real, and moving into it is a real loss.

    Measured on 3 of 668 entries under one /project and 13 of 894 under one
    /project2: mode 2771 without the group gives `--x`, so a path you already
    know still works and you can no longer list it. A user who cannot enumerate
    their own directory has lost something.
    """
    previous = _snapshot([_root(reach=Reach.LISTABLE)], taken_at=T0)
    current = _snapshot(
        [_root(reach=Reach.TRAVERSE, reach_reason="os.access reports execute without read")],
        taken_at=T1,
    )
    result = diff(previous, current)
    assert result.labels() == [CLOSED]
    assert result.records[0].current == Reach.TRAVERSE


# --------------------------------------------------------------------------
# A move is not a loss
# --------------------------------------------------------------------------


def test_a_rename_is_one_arrival_and_never_a_loss():
    """The same invariant in the path domain: claiming a loss for data that is there.

    A renamed directory misses on ``(device, path)`` and hits on
    ``(st_dev, st_ino)``. Reported naively that is one root gone and another
    arrived, and the `gone` row is a false alarm about data loss for a tree that
    is sitting right there under a new name. The sweep is confirmed and the
    vantage point is the same here deliberately, so the `gone` would really fire
    if it were not suppressed.
    """
    previous = _snapshot([_root(path="/project/old")], taken_at=T0)
    current_root = _root(path="/project/new")  # same (st_dev, st_ino)
    current = _snapshot(
        [current_root],
        taken_at=T1,
        discovery=confirmed("swept 12 mounts", source="mounts"),
    )

    result = diff(previous, current)

    assert result.labels() == [NEW]
    change = result.records[0]
    assert change.path == "/project/new"
    assert change.renamed_from == "/project/old"
    assert GONE not in result.labels()
    assert "gone" not in _blob(result)
    assert "st_dev, st_ino" in change.because, "the evidence must name what matched"
    # `Root.renamed_from` is documented as being set by the diff layer, so the
    # renderer holding the live object can print the move as a move.
    assert current_root.renamed_from == "/project/old"


def test_a_replacement_at_the_same_path_is_a_warning_not_a_label():
    """A fileset deleted and recreated has no word in the vocabulary.

    `new` would claim a path the user has had for a year is new, and `gone`
    would claim a loss for a path that is right there, so neither is honest.
    A warning is the channel for something noticed that is not an event.
    """
    previous = _snapshot([_root(identity=(2049, 101))], taken_at=T0)
    current = _snapshot([_root(identity=(2049, 777))], taken_at=T1)

    result = diff(previous, current)

    assert list(result) == []
    assert result.warnings
    assert "/project/x" in result.warnings[0]
    assert "replaced" in result.warnings[0]


def test_a_missing_identity_never_reads_as_a_replacement():
    """A probe that could not `stat` produces no identity, which is not a change.

    Comparing against a None would let one failed `stat` report that the fileset
    behind a path was recreated, which is the same fabrication as reporting a
    timeout as a lost directory.
    """
    previous = _snapshot([_root(identity=(2049, 101))], taken_at=T0)
    current = _snapshot([_root(identity=None)], taken_at=T1)

    result = diff(previous, current)

    assert list(result) == []
    assert result.warnings == []


# --------------------------------------------------------------------------
# The structural guard: one construction site
# --------------------------------------------------------------------------


def _string_value(node):
    """A string literal's text, on every Python this package supports, else None.

    Python 3.6 and 3.7 parse a string literal as `ast.Str` and only 3.8 made it
    `ast.Constant`, so testing for `Constant` alone found no literals at all on
    the RHEL 8 and SLES 15 system Pythons this package is built for, and the
    guard below failed there while passing everywhere it was written.
    `ast.Str` is only touched below 3.8: it warns from 3.12 and is gone in 3.14.
    """
    if isinstance(node, ast.Constant):
        return node.value if isinstance(node.value, str) else None
    if sys.version_info < (3, 8) and isinstance(node, ast.Str):
        return node.s
    return None


def _docstring_nodes(tree):
    """Every string constant that is a docstring, so prose is not mistaken for code."""
    out = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            body = getattr(node, "body", [])
            if body and isinstance(body[0], ast.Expr):
                value = body[0].value
                if _string_value(value) is not None:
                    out.add(id(value))
    return out


def test_only_one_function_can_return_closed_or_opened():
    """The invariant must not be one edit away from a second code path.

    Every guard in this file is behavioural, and behavioural guards only cover
    the scenarios somebody thought of. This one is structural: `_reach_change`
    delegates to `Reach.regressed` and `Reach.improved`, which refuse UNKNOWN,
    so as long as it is the only source of these two labels the invariant holds
    for scenarios nobody has written a test for yet.
    """
    with open(DIFF_SOURCE) as handle:
        tree = ast.parse(handle.read())

    offenders = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.FunctionDef):
            continue
        for sub in ast.walk(node):
            if not isinstance(sub, ast.Return) or sub.value is None:
                continue
            names = {n.id for n in ast.walk(sub.value) if isinstance(n, ast.Name)}
            if names & {"CLOSED", "OPENED"} and node.name != "_reach_change":
                offenders.append(node.name)

    assert offenders == [], (
        "these functions return CLOSED or OPENED and must not: %s. The labels that "
        "claim a user's access changed come from _reach_change and nowhere else." % (offenders,)
    )


def test_the_label_closed_is_never_written_as_a_bare_string():
    """A literal would bypass the structural guard above entirely.

    ``Change(label="closed", ...)`` reads as harmless and is exactly how the one
    construction site becomes two. Docstrings are excluded, since the invariant
    is documented in prose in several places and that is the point.
    """
    with open(DIFF_SOURCE) as handle:
        tree = ast.parse(handle.read())
    docstrings = _docstring_nodes(tree)

    literals = [
        node
        for node in ast.walk(tree)
        if _string_value(node) in ("closed", "opened") and id(node) not in docstrings
    ]
    # Exactly two: the `CLOSED = "closed"` and `OPENED = "opened"` definitions.
    assert len(literals) == 2, (
        "found %d bare 'closed'/'opened' literals outside docstrings; only the two "
        "constant definitions may exist" % (len(literals),)
    )


def test_every_label_a_diff_emits_is_in_the_vocabulary():
    """The label set is a wire format, so a typo must be a test failure.

    A label outside the documented set reaches `--json` and a consumer that
    switches on it, and `label_rank` would sort it last rather than raise, so
    nothing else in the system would notice.
    """
    previous = _snapshot(
        [_root(), _root(path="/project/gone"), _root(path="/project/shrink", used=10 * 1024**3)],
        taken_at=T0,
    )
    current = _snapshot(
        [
            _root(reach=Reach.CLOSED, reach_reason="os.access reports neither read nor execute"),
            _root(path="/project/new-one", identity=(2049, 999)),
            _root(path="/project/shrink", used=1 * 1024**3),
            _root(path="/project/held", stranded=True, used=12 * 1024**3, reach=Reach.CLOSED),
        ],
        taken_at=T1,
        discovery=confirmed("swept 12 mounts", source="mounts"),
    )
    result = diff(previous, current)
    assert result.labels(), "this fixture must produce some changes or it proves nothing"
    unexpected = [label for label in result.labels() if label not in LABELS]
    assert unexpected == []
    # And the whole vocabulary really is exercised by something here.
    assert {CLOSED, GONE} <= set(result.labels())


def test_describe_orders_the_alarming_labels_first():
    """A user reading three lines must get the three that matter.

    `closed` and `stranded` are the two that cost somebody their afternoon, and
    `unknown` is a gap rather than an event, so it goes last.
    """
    previous = _snapshot(
        [_root(), _root(path="/project/grow", used=1 * 1024**3), _root(path="/project/slow")],
        taken_at=T0,
    )
    current = _snapshot(
        [
            _root(reach=Reach.CLOSED, reach_reason="os.access reports neither read nor execute"),
            _root(path="/project/grow", used=40 * 1024**3),
            _root(path="/project/slow", reach=Reach.UNKNOWN),
            _root(path="/project/held", stranded=True, used=12 * 1024**3, reach=Reach.CLOSED),
        ],
        taken_at=T1,
    )
    labels = [change.label for change in describe(diff(previous, current).records)]
    assert labels[0] == CLOSED
    assert labels[-1] == UNKNOWN
    assert labels.index("stranded") < labels.index("grew")
