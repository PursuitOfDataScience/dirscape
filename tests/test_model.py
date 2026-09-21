"""The shared contract. Every other package in this tool depends on these
invariants holding, so these tests come before any of the feature tests.
"""

import pytest

from dirscape.model import (
    CATEGORY_LABELS,
    TRANSIENT_CATEGORIES,
    QuotaRow,
    QuotaSnapshot,
    Reach,
    Root,
    Verdict,
    VerdictCategory,
    category_label,
    confirmed,
    refuted,
    sanitize,
    unavailable_quota,
    unknown,
)


def _all_categories():
    return sorted(
        value
        for name, value in vars(VerdictCategory).items()
        if not name.startswith("_") and isinstance(value, str)
    )


# --------------------------------------------------------------------------
# The three-state rule
# --------------------------------------------------------------------------


def test_every_category_has_a_human_label():
    """An unlabelled category would print its wire token at a user.

    This is nodetop's NT-5, where raw enum members appeared beside English in a
    user-facing column. The label table has to be exhaustive by test, not by
    hope.
    """
    missing = [c for c in _all_categories() if c not in CATEGORY_LABELS]
    assert missing == [], "categories with no human label: %s" % (missing,)


def test_transient_categories_are_never_durable():
    for category in TRANSIENT_CATEGORIES:
        verdict = unknown(category)
        assert verdict.durable is False
        assert verdict.known is False
        assert verdict.refuted is False, "%s must not read as a refusal" % (category,)


def test_durable_categories_are_durable():
    durable = [c for c in _all_categories() if c not in TRANSIENT_CATEGORIES]
    assert durable, "there must be some durable categories"
    for category in durable:
        assert Verdict(False, category).durable is True


def test_refuted_rejects_a_transient_category():
    """The guard that makes the central invariant unbypassable.

    Constructing a refusal from a transient category would produce a verdict
    that reads as "no" while claiming to be unanswerable. Raising here means
    the mistake is a test failure rather than a false claim printed at a user.
    """
    for category in TRANSIENT_CATEGORIES:
        with pytest.raises(ValueError):
            refuted(category, "should not be possible")


def test_unknown_is_not_a_negative_answer():
    verdict = unknown(VerdictCategory.PROBE_TIMEOUT, "stat hung on a wedged mount")
    assert verdict.value is None
    assert verdict.known is False
    assert verdict.confirmed is False
    assert verdict.refuted is False
    assert verdict.glyph() == "?"
    assert verdict.glyph(ascii_only=True) == "?"


def test_confirmed_and_refuted_render_distinctly():
    yes, no, maybe = (
        confirmed(),
        refuted(VerdictCategory.ACCESS_DENIED),
        unknown(),
    )
    glyphs = {yes.glyph(), no.glyph(), maybe.glyph()}
    assert len(glyphs) == 3, "yes, no and unknown must be visibly different"
    ascii_glyphs = {yes.glyph(True), no.glyph(True), maybe.glyph(True)}
    assert len(ascii_glyphs) == 3
    assert all(ord(ch) < 128 for g in ascii_glyphs for ch in g)


def test_category_label_falls_back_without_raising():
    """An unknown category must not take down a terminal mid-render."""
    assert category_label("SOME_FUTURE_TOKEN") == "some future token"


# --------------------------------------------------------------------------
# Reach: the tri-state, and its refusal to compare against UNKNOWN
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "old,new",
    [
        (Reach.UNKNOWN, Reach.LISTABLE),
        (Reach.LISTABLE, Reach.UNKNOWN),
        (Reach.UNKNOWN, Reach.CLOSED),
        (Reach.CLOSED, Reach.UNKNOWN),
        (Reach.UNKNOWN, Reach.UNKNOWN),
    ],
)
def test_reach_never_claims_a_change_against_unknown(old, new):
    """The most important assertion in the file.

    A run that could not determine reach must not be reported as a change in
    either direction. Without this, one timed-out `stat` tells a user they lost
    access to a directory, which is a false alarm about data loss.
    """
    assert Reach.improved(old, new) is False
    assert Reach.regressed(old, new) is False


def test_reach_detects_a_real_change_in_both_directions():
    """The control that proves the test above is not passing vacuously."""
    assert Reach.improved(Reach.CLOSED, Reach.TRAVERSE) is True
    assert Reach.improved(Reach.TRAVERSE, Reach.LISTABLE) is True
    assert Reach.regressed(Reach.LISTABLE, Reach.TRAVERSE) is True
    assert Reach.regressed(Reach.TRAVERSE, Reach.CLOSED) is True
    assert Reach.improved(Reach.LISTABLE, Reach.LISTABLE) is False


def test_traverse_only_sits_between_closed_and_listable():
    """Ordering is load-bearing: the middle state is real and measured.

    A directory at mode 2771 that you are not in the group for gives `--x`, so
    you can pass through it and cannot list it. Measured on 3 of 668 entries
    under one /project and 13 of 894 under one /project2.
    """
    order = Reach.ORDER
    assert order.index(Reach.CLOSED) < order.index(Reach.TRAVERSE)
    assert order.index(Reach.TRAVERSE) < order.index(Reach.LISTABLE)


# --------------------------------------------------------------------------
# Quota
# --------------------------------------------------------------------------


def test_zero_limit_means_unlimited_not_full():
    """GPFS reports an unlimited fileset as quota 0 and limit 0.

    Reading that as a zero allowance would render every unlimited fileset at
    100% and send users to a help desk about a quota they do not have.
    """
    row = QuotaRow("software", "blocks", "user", used=329430160, soft=0, hard=0)
    assert row.limit is None
    assert row.fraction is None, "an unlimited row has no fraction, not a zero one"


def test_fraction_is_none_rather_than_zero_when_unmeasurable():
    """None and 0.0 must not be confused: an empty bar reads as 'plenty of
    room', which is the same lie as reporting an unmeasured quota as
    unlimited.
    """
    assert QuotaRow("f", "blocks", "user", used=None, hard=100).fraction is None
    assert QuotaRow("f", "blocks", "user", used=0, hard=100).fraction == 0.0


def test_quota_label_is_qualified_unconditionally():
    """A fileset name is unique within a filesystem, not across one.

    On a login node mounting three clusters, `scratch`, `home` and `software`
    are all fileset names on more than one device. Qualifying only when a
    collision happens to be visible would make a label depend on the host's
    mount table.
    """
    row = QuotaRow("scratch", "blocks", "user", 1, device="meadow3_perf")
    assert row.label == "meadow3_perf:scratch"
    same = QuotaRow("meadow2_perf", "blocks", "user", 1, device="meadow2_perf")
    assert same.label == "meadow2_perf"


def test_rows_for_path_prefers_the_longest_mount():
    shallow = QuotaRow("cap", "blocks", "user", 1, mount="/project")
    deep = QuotaRow("proj-hpc", "blocks", "user", 2, mount="/project/hpc")
    snap = QuotaSnapshot("test", [shallow, deep])
    hits = snap.rows_for_path("/project/hpc/jdoe42")
    assert [r.fileset for r in hits] == ["proj-hpc", "cap"]


def test_rows_for_path_does_not_match_a_sibling_prefix():
    """`/project2` must not match a `/project` mount by string prefix."""
    row = QuotaRow("cap", "blocks", "user", 1, mount="/project")
    assert QuotaSnapshot("t", [row]).rows_for_path("/project2/hpc") == []


def test_absence_is_constructible_only_with_a_reason():
    snap = unavailable_quota("gpfs", VerdictCategory.NO_QUOTA_BACKEND, "no GPFS here")
    assert snap.available is False
    assert snap.rows == []
    assert snap.reason
    assert snap.to_json()["available"] is False


def test_the_two_doubt_channels_stay_separate():
    """A reading can be fresh and wrong, or stale and exact.

    rapiDU keeps these apart because a single confidence field cannot say
    which, and the user's next action differs: re-run for a stale figure,
    distrust the number for a bracketed one.
    """
    snap = QuotaSnapshot(
        "wrapper",
        [QuotaRow("home", "blocks", "user", 1)],
        taken_at=1000.0,
        read_at=2800.0,
        time_note="site wrapper refreshes on a 30 minute cron",
        figure_note="",
    )
    assert snap.age_seconds == 1800.0
    payload = snap.to_json()
    assert "time_note" in payload
    assert "figure_note" not in payload


# --------------------------------------------------------------------------
# Root: the three axes
# --------------------------------------------------------------------------


def test_a_fresh_root_claims_nothing():
    """Default state is "not probed", not "absent"."""
    root = Root("/project/x")
    for axis in (root.allocated, root.mounted, root.present, root.writable):
        assert axis.known is False
        assert axis.category == VerdictCategory.NOT_PROBED
    assert root.reach == Reach.UNKNOWN


def test_allocated_but_not_mounted_is_representable():
    """The case the site quota wrapper creates and no other tool reports.

    Measured: `allocs storage` lists allocations on cfs1, cfs2, cfs4 and
    project3 from a node where none of those paths exist.
    """
    root = Root("/cfs4/hpc-staff", role="archive")
    root.allocated = confirmed("allocs storage", source="accounts")
    root.mounted = refuted(VerdictCategory.NOT_MOUNTED_HERE, "absent from /proc/self/mounts")
    assert root.elsewhere is True
    assert root.to_json()["elsewhere"] is True


def test_unknown_reach_does_not_make_a_root_unreachable():
    """NT-1 inverted. An unanswered question must not remove a row.

    If a timed-out probe made a root unreachable, a wedged mount would vanish
    from the atlas rather than be reported as unmeasured.
    """
    root = Root("/project/slow")
    root.reach = Reach.UNKNOWN
    assert root.reachable is True
    root.reach = Reach.CLOSED
    assert root.reachable is False


def test_notes_and_sources_deduplicate():
    root = Root("/project/x")
    root.add_source("mounts")
    root.add_source("mounts")
    root.add_note("seen twice")
    root.add_note("seen twice")
    assert root.sources == ["mounts"]
    assert root.notes == ["seen twice"]


# --------------------------------------------------------------------------
# sanitize: foreign text is hostile
# --------------------------------------------------------------------------


def test_sanitize_defuses_a_forged_table_row():
    """rapiDU's RD-6: a filename with a newline forges a row, an escape
    sequence executes.
    """
    hostile = "real.txt\n/project/fake   999T   100%\x1b[31m"
    clean = sanitize(hostile)
    assert "\n" not in clean
    assert "\x1b" not in clean
    assert "real.txt" in clean


def test_sanitize_caps_length():
    assert len(sanitize("x" * 5000, limit=64)) == 64


def test_sanitize_handles_none_and_non_strings():
    assert sanitize(None) == ""
    assert sanitize(12345) == "12345"


def test_verdict_sanitizes_at_construction():
    """So no caller has to remember to."""
    verdict = unknown(VerdictCategory.BACKEND_FAILED, "line one\nline two")
    assert "\n" not in verdict.reason
