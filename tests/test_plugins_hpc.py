"""The HPC site plugin.

These tests exist to protect two things: the site's own membership rule, which
must not drift from the rule the site's `quota` command applies, and the
boundary that keeps this plugin from being able to hide storage.

Every fixture here is inline text. Nothing reads the live cluster, so the suite
runs on a laptop.
"""

import pytest

from dirscape.plugins import Allocation, SitePlugin, detect_plugins
from dirscape.plugins.hpc import (
    HPCPlugin,
    parse_accounts_storage,
    parse_mmrepquota_parsable,
)

# Verbatim from `allocs storage` on a login node, trimmed to four rows.
ACCOUNTS_STORAGE = """\
Storage allocations for jdoe42:
+-----------+------+---------+--------+-------------------+------------+------------+
|  Account  |  ID  |  Type   | GB(s)  |     Location      |   Start    |    End     |
+-----------+------+---------+--------+-------------------+------------+------------+
| hpc-staff | 301  | Special | 256000 |  cfs1/hpc-staff   | 2016-03-02 | 2100-01-01 |
| hpc-staff | 1204 | Special |  2048  |  project/biokit   | 2017-12-01 | 9999-12-31 |
| hpc-staff | 1583 | Special | 207200 |   project3/hpc    | 0000-00-00 | 9999-12-31 |
| hpc-staff | 2718 |   PAY   | 20480  |  cfs4/hpc-staff   | 2026-04-27 | 2026-10-01 |
+-----------+------+---------+--------+-------------------+------------+------------+
"""

# The colon-delimited archive format, header line verbatim.
MMREPQUOTA = (
    "*** Report for USR GRP FILESET quotas on meadow3_cap\n"
    "mmrepquota::HEADER:version:reserved:reserved:filesystemName:quotaType:id:name:"
    "blockUsage:blockQuota:blockLimit:blockInDoubt:blockGrace:filesUsage:filesQuota:"
    "filesLimit:filesInDoubt:filesGrace:remarks:quota:defQuota:fid:filesetname:\n"
    "mmrepquota::0:1:::meadow3_cap:FILESET:1:project-hpc:"
    "82817707504:0:217264947200:0:none:3118908:0:0:0:none:::0:1:project-hpc:\n"
    "mmrepquota::0:1:::meadow3_cap:USR:912345678:jdoe42:"
    "858080:31457280:36700160:2229360:none:35939:300000:1000000:0:none:::0:2:home:\n"
)


# --------------------------------------------------------------------------
# The membership rule
# --------------------------------------------------------------------------

GROUPS = ["jdoe42", "hpc", "hpc-staff", "hpc-software", "data-newsome", "collie3-users"]


def test_the_project_hpc_special_case_needs_hpc_staff_not_hpc():
    """Reproduced from the site's own sitequota.py, including the special case.

    The original tests `project-hpc` BEFORE the general rule, and the order
    matters: `project-hpc` strips to `hpc`, which is a real group here, so
    applying the general rule first would grant it to every member of `hpc`.
    """
    plugin = HPCPlugin()
    assert plugin.owns_fileset("project-hpc", ["hpc"]) is False
    assert plugin.owns_fileset("project-hpc", ["hpc", "hpc-staff"]) is True


def test_the_general_rule_accepts_the_group_or_its_pi_form():
    plugin = HPCPlugin()
    assert plugin.owns_fileset("project-newsome", ["newsome"]) is True
    assert plugin.owns_fileset("project-smith", ["pi-smith"]) is True
    assert plugin.owns_fileset("project-other", GROUPS) is False


@pytest.mark.parametrize(
    "fileset",
    ["project2-lab", "project3-lab", "collie3-lab", "project-lab"],
)
def test_every_declared_prefix_is_stripped(fileset):
    assert HPCPlugin().owns_fileset(fileset, ["lab"]) is True


def test_project2_is_stripped_before_project():
    """Prefix order is load-bearing.

    `project2-lab` stripped by the shorter `project-` prefix would leave
    `2-lab`, which matches no group, so a user with real access would be told
    they have none.
    """
    assert HPCPlugin().owns_fileset("project2-lab", ["lab"]) is True
    assert HPCPlugin().owns_fileset("project2-lab", ["2-lab"]) is False


@pytest.mark.parametrize("fileset", ["home", "software", "scratch", ""])
def test_a_non_project_fileset_returns_none_not_false(fileset):
    """None and False mean different things and confusing them hides storage.

    A rule that has never heard of `home` has not established that the user
    lacks it. Returning False would let the plugin veto a root reached
    perfectly well through the mount table, and a plugin must only add facts.
    """
    assert HPCPlugin().owns_fileset(fileset, GROUPS) is None


def test_the_rule_matches_what_the_site_command_prints():
    """Regression pin against the real account.

    Measured: for this group set the site `quota` command prints exactly three
    project filesets. If a later refactor of the rule changes that set, this
    test is the thing that notices.
    """
    plugin = HPCPlugin()
    all_filesets = [
        "project-hpc",
        "project2-hpc",
        "collie3-hpc-staff",
        "project-dahlias",
        "project-abe",
        "project-bard",
        "project-marsh",
        "project2-reference",
    ]
    owned = [f for f in all_filesets if plugin.owns_fileset(f, GROUPS) is True]
    assert owned == ["project-hpc", "project2-hpc", "collie3-hpc-staff"]


# --------------------------------------------------------------------------
# Allocation parsing
# --------------------------------------------------------------------------


def test_accounts_storage_parses_every_row():
    allocations = parse_accounts_storage(ACCOUNTS_STORAGE)
    assert len(allocations) == 4
    assert allocations[0].account == "hpc-staff"
    assert allocations[0].location == "cfs1/hpc-staff"
    assert allocations[0].size_gb == 256000.0
    assert allocations[3].kind == "PAY"


def test_an_allocation_location_is_never_turned_into_a_path():
    """`cfs4/hpc-staff` looks like it should be `/cfs4/hpc-staff` and often is.

    Inventing the path is the guess that rapiDU's RD-3 made when it
    confidently attributed a /scratch walk to the wrong cluster's filesystem.
    The mount table decides whether a path exists, not string surgery on an
    allocation database.
    """
    for allocation in parse_accounts_storage(ACCOUNTS_STORAGE):
        assert allocation.path is None


def test_accounts_storage_survives_a_widened_column():
    """Parsed from the header row, not fixed offsets.

    A longer account name shifts every subsequent column, and a fixed-offset
    parser would start reading the ID column as the type.
    """
    widened = ACCOUNTS_STORAGE.replace("hpc-staff", "a-very-long-account-name")
    allocations = parse_accounts_storage(widened)
    assert len(allocations) == 4
    assert allocations[0].account == "a-very-long-account-name"
    assert allocations[0].location == "cfs1/a-very-long-account-name"


def test_a_row_with_the_wrong_cell_count_is_skipped_not_guessed():
    broken = ACCOUNTS_STORAGE.replace(
        "| hpc-staff | 1204 | Special |  2048  |  project/biokit   | 2017-12-01 | 9999-12-31 |",
        "| hpc-staff | 1204 | Special |",
    )
    allocations = parse_accounts_storage(broken)
    assert len(allocations) == 3
    assert all(a.location for a in allocations)


def test_empty_input_yields_nothing_rather_than_raising():
    assert parse_accounts_storage("") == []
    assert parse_accounts_storage("command not found") == []


# --------------------------------------------------------------------------
# Archive parsing
# --------------------------------------------------------------------------


def test_mmrepquota_is_keyed_off_its_own_header():
    rows = parse_mmrepquota_parsable(MMREPQUOTA)
    assert len(rows) == 2
    fileset_rows = [r for r in rows if r.get("quotaType") == "FILESET"]
    assert len(fileset_rows) == 1
    assert fileset_rows[0]["name"] == "project-hpc"
    assert fileset_rows[0]["filesystemName"] == "meadow3_cap"
    assert fileset_rows[0]["blockLimit"] == "217264947200"


def test_mmrepquota_survives_an_inserted_field():
    """The reason the parser reads the HEADER line instead of indexing.

    Twenty-six positional fields is too many to index by hand and be sure, and
    the day IBM inserts one, a hardcoded parser turns every `filesetname` into
    a `fid` without erroring.
    """
    shifted = MMREPQUOTA.replace(
        "mmrepquota::HEADER:version:reserved:reserved:filesystemName:",
        "mmrepquota::HEADER:version:reserved:reserved:newField:filesystemName:",
    ).replace("mmrepquota::0:1:::meadow3_cap:", "mmrepquota::0:1:::EXTRA:meadow3_cap:")
    rows = parse_mmrepquota_parsable(shifted)
    fileset_rows = [r for r in rows if r.get("quotaType") == "FILESET"]
    assert fileset_rows[0]["name"] == "project-hpc"
    assert fileset_rows[0]["newField"] == "EXTRA"


def test_rows_before_a_header_are_ignored():
    assert parse_mmrepquota_parsable("mmrepquota::0:1:::dev:FILESET:1:x:\n") == []


def test_the_device_comes_from_the_report_banner():
    """`*** Report for ... on <device>` is the only place the device appears
    for rows whose own field is empty.
    """
    text = MMREPQUOTA.replace(":meadow3_cap:FILESET:", "::FILESET:")
    rows = parse_mmrepquota_parsable(text)
    fileset_rows = [r for r in rows if r.get("quotaType") == "FILESET"]
    assert fileset_rows[0]["filesystemName"] == "meadow3_cap"


# --------------------------------------------------------------------------
# The plugin boundary
# --------------------------------------------------------------------------


def test_detection_never_consults_the_hostname():
    """A hostname is the least reliable signal available.

    nodetop filters GPU nodes on GRES rather than a hostname prefix because
    `collie3-bigmem1` is a CPU node despite its name. Same instrument, same
    reason: detection here uses the site CLI directory or a storage device
    name.
    """
    import inspect

    # The docstring explains that it does NOT use the hostname, so scanning
    # the raw source would match its own explanation. Strip the docstring and
    # the comments and scan only what executes.
    source = inspect.getsource(HPCPlugin.detect)
    body = source.split('"""')
    code = body[0] + ("".join(body[2:]) if len(body) > 2 else "")
    code = "\n".join(
        line for line in code.splitlines() if not line.strip().startswith("#")
    ).lower()

    for forbidden in ("hostname", "gethostname", "uname", "fqdn", "nodename"):
        assert forbidden not in code, "detect() reads %s, which is unreliable" % forbidden


def test_detection_accepts_a_device_name_without_the_site_cli():
    class FakeMount(object):
        def __init__(self, device):
            self.device = device

    plugin = HPCPlugin()
    assert plugin.detect(None, [FakeMount("meadow3_cap")]) is True
    assert plugin.detect(None, [FakeMount("collie3_perf")]) is True


def test_detection_declines_an_unrelated_cluster(monkeypatch):
    """The plugin must not claim a site that is not HPC.

    The site-CLI branch fires on the development host, so it is pointed at a
    nonexistent directory to isolate the device branch, which is the half that
    has to be right when the tool is run somewhere else entirely.
    """
    import dirscape.plugins.hpc as hpc

    monkeypatch.setattr(hpc, "_SITE_BIN", "/nonexistent/site/bin")

    class FakeMount(object):
        def __init__(self, device):
            self.device = device

    plugin = HPCPlugin()
    foreign = [FakeMount("procyon_home"), FakeMount("theta_fs0"), FakeMount("lustre1")]
    assert plugin.detect(None, foreign) is False
    assert plugin.detect(None, []) is False
    assert plugin.detect(None, [FakeMount("meadow3_cap")]) is True


def test_the_base_plugin_knows_nothing():
    """Every hook is optional, so an unknown site costs labels, not correctness."""
    base = SitePlugin()
    assert base.detect(None, []) is False
    assert base.allocations(None, None) == []
    assert base.owns_fileset("project-x", ["x"]) is None
    assert base.first_seen("project-x", None) is None
    assert base.site_defaults() == {}
    assert base.policy_for("/x") == {}


def test_plugins_can_be_forced_by_name_for_testing():
    """So the HPC rules can be exercised against a recorded transcript from a
    machine that is not HPC.
    """
    active = detect_plugins(None, [], enabled=["hpc"])
    assert [p.name for p in active] == ["hpc"]
    assert detect_plugins(None, [], enabled=[]) == []


def test_first_seen_returns_none_when_the_archive_is_absent(monkeypatch):
    """Which is the state on every cluster except one."""
    import dirscape.plugins.hpc as hpc

    monkeypatch.setattr(hpc, "_ARCHIVE_DIR", "/nonexistent/archive/path")
    plugin = HPCPlugin()
    assert plugin.first_seen("project-anything", None) is None


def test_allocation_json_round_trip():
    allocation = Allocation("acct", "cfs4/acct", size_gb=100.0, kind="PAY")
    payload = allocation.to_json()
    assert payload["location"] == "cfs4/acct"
    assert payload["path"] is None
