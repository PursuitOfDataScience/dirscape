"""The configured site plugin.

These tests exist to protect two things: a site's own membership rule, which
must not drift from the rule the site's `quota` command applies, and the
boundary that keeps a plugin from being able to hide storage.

The site here is invented and configured the way a real one would be, through
a `[plugin]` section, so the package itself never names a cluster. Every
fixture is inline text and nothing reads a live cluster.
"""

import os

import pytest

from dirscape.plugins import Allocation, SitePlugin, available_plugins, detect_plugins
from dirscape.plugins.site import (
    ConfiguredSite,
    parse_allocation_table,
    parse_mmrepquota_parsable,
)
from dirscape.sitecfg import load_site

# An allocation command's table, trimmed to four rows.
ALLOCATIONS = """\
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

#: The invented site, as its `[plugin]` section would describe it.
SETTINGS = {
    "description": "Example HPC (meadow2 / meadow3 / collie3)",
    "detect_files": "/nonexistent/opt/site/bin/allocs",
    "detect_device_prefixes": "meadow, collie3",
    "bin_dir": "/opt/site/bin",
    "allocation_command": "allocs storage",
    "fileset_prefixes": "project2-, project3-, collie3-, project-",
    "group_prefixes": "pi-",
    "fileset_groups": "project-hpc:hpc-staff",
    "roles": "/collie3:project, /collie3/*:project",
}

SITE_CONF = """\
[plugin]
description            = Example HPC (meadow2 / meadow3 / collie3)
detect_device_prefixes = meadow, collie3
fileset_prefixes       = project2-, project3-, collie3-, project-
fileset_groups         = project-hpc:hpc-staff
roles                  = /collie3:project, /collie3/*:project
"""


def _plugin(**overrides):
    settings = dict(SETTINGS)
    settings.update(overrides)
    return ConfiguredSite(settings)


class FakeMount(object):
    def __init__(self, device):
        self.device = device


# --------------------------------------------------------------------------
# The membership rule
# --------------------------------------------------------------------------

GROUPS = ["jdoe42", "hpc", "hpc-staff", "hpc-software", "data-newsome", "collie3-users"]


def test_a_special_case_is_checked_before_the_general_rule():
    """The order matters, exactly as in the site's own rule.

    `project-hpc` strips to `hpc`, which is a real group here, so applying the
    general rule first would grant it to every member of `hpc`.
    """
    plugin = _plugin()
    assert plugin.owns_fileset("project-hpc", ["hpc"]) is False
    assert plugin.owns_fileset("project-hpc", ["hpc", "hpc-staff"]) is True


def test_the_general_rule_accepts_the_group_or_its_pi_form():
    plugin = _plugin()
    assert plugin.owns_fileset("project-newsome", ["newsome"]) is True
    assert plugin.owns_fileset("project-smith", ["pi-smith"]) is True
    assert plugin.owns_fileset("project-other", GROUPS) is False


@pytest.mark.parametrize(
    "fileset",
    ["project2-lab", "project3-lab", "collie3-lab", "project-lab"],
)
def test_every_declared_prefix_is_stripped(fileset):
    assert _plugin().owns_fileset(fileset, ["lab"]) is True


def test_project2_is_stripped_before_project():
    """Prefix order is load-bearing, and it is the order the site wrote.

    `project2-lab` stripped by the shorter `project-` prefix would leave
    `2-lab`, which matches no group, so a user with real access would be told
    they have none.
    """
    assert _plugin().owns_fileset("project2-lab", ["lab"]) is True
    assert _plugin().owns_fileset("project2-lab", ["2-lab"]) is False


@pytest.mark.parametrize("fileset", ["home", "software", "scratch", ""])
def test_a_non_project_fileset_returns_none_not_false(fileset):
    """None and False mean different things and confusing them hides storage.

    A rule that has never heard of `home` has not established that the user
    lacks it. Returning False would let the plugin veto a root reached
    perfectly well through the mount table, and a plugin must only add facts.
    """
    assert _plugin().owns_fileset(fileset, GROUPS) is None


def test_the_rule_matches_what_the_site_command_prints():
    """Regression pin: for this group set the site's `quota` command prints
    exactly three project filesets. If a later refactor of the rule changes
    that set, this test is the thing that notices.
    """
    plugin = _plugin()
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


def test_group_prefixes_come_from_config():
    plugin = _plugin(group_prefixes="grp-")
    assert plugin.owns_fileset("project-lab", ["grp-lab"]) is True
    assert plugin.owns_fileset("project-lab", ["pi-lab"]) is False


def test_an_unconfigured_rule_has_nothing_to_say():
    assert ConfiguredSite({"description": "x"}).owns_fileset("project-lab", ["lab"]) is None


# --------------------------------------------------------------------------
# Allocation parsing
# --------------------------------------------------------------------------


def test_the_allocation_table_parses_every_row():
    allocations = parse_allocation_table(ALLOCATIONS)
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
    for allocation in parse_allocation_table(ALLOCATIONS):
        assert allocation.path is None


def test_the_allocation_table_survives_a_widened_column():
    """Parsed from the header row, not fixed offsets.

    A longer account name shifts every subsequent column, and a fixed-offset
    parser would start reading the ID column as the type.
    """
    widened = ALLOCATIONS.replace("hpc-staff", "a-very-long-account-name")
    allocations = parse_allocation_table(widened)
    assert len(allocations) == 4
    assert allocations[0].account == "a-very-long-account-name"
    assert allocations[0].location == "cfs1/a-very-long-account-name"


def test_a_row_with_the_wrong_cell_count_is_skipped_not_guessed():
    broken = ALLOCATIONS.replace(
        "| hpc-staff | 1204 | Special |  2048  |  project/biokit   | 2017-12-01 | 9999-12-31 |",
        "| hpc-staff | 1204 | Special |",
    )
    allocations = parse_allocation_table(broken)
    assert len(allocations) == 3
    assert all(a.location for a in allocations)


def test_empty_input_yields_nothing_rather_than_raising():
    assert parse_allocation_table("") == []
    assert parse_allocation_table("command not found") == []


def test_allocations_run_the_configured_command(recorded, cmd):
    runner = recorded(
        [cmd(["/opt/site/bin/allocs", "storage"], stdout=ALLOCATIONS)],
        probes={"allocs": "/opt/site/bin/allocs"},
    )
    allocations = _plugin().allocations(runner, None)
    assert [a.location for a in allocations][:2] == ["cfs1/hpc-staff", "project/biokit"]
    assert allocations[0].source == "allocs storage"


def test_no_command_configured_means_no_allocations(recorded):
    assert _plugin(allocation_command="").allocations(recorded([]), None) == []


def test_a_command_that_is_not_installed_means_no_allocations(recorded):
    assert _plugin().allocations(recorded([], probes={"allocs": None}), None) == []


# --------------------------------------------------------------------------
# Archive parsing and bisection
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


def _daily(names):
    lines = ["*** Report for USR GRP FILESET quotas on meadow3_cap"]
    lines += ["%-24s root  FILESET  1  0  0  0  none" % name for name in names]
    return "\n".join(lines) + "\n"


def test_first_seen_bisects_the_daily_archive_to_the_day(tmp_path):
    archive = tmp_path / "archive"
    archive.mkdir()
    days = ["20260101", "20260102", "20260103", "20260104", "20260105", "20260106"]
    for index, day in enumerate(days):
        names = ["project-old"] + (["project-new"] if index >= 3 else [])
        (archive / ("%s-gpfs.quota" % day)).write_text(_daily(names))
    (archive / "unrelated.txt").write_text("not a dump\n")

    plugin = _plugin(quota_archive=str(archive))
    stamp = plugin.first_seen("project-new", None)
    assert stamp is not None
    assert os.path.basename(plugin._daily_files()[3][1]) == "20260104-gpfs.quota"
    import time

    assert time.strftime("%Y%m%d", time.localtime(stamp)) == "20260104"
    assert plugin.first_seen("project-old", None) is None, "older than the archive"
    assert plugin.first_seen("project-never", None) is None, "not in the newest dump"


def test_the_daily_file_pattern_comes_from_config(tmp_path):
    archive = tmp_path / "archive"
    archive.mkdir()
    for index, day in enumerate(("20260101", "20260102")):
        names = ["project-a"] + (["project-b"] if index else [])
        (archive / ("dump.%s" % day)).write_text(_daily(names))
    plugin = _plugin(quota_archive=str(archive), quota_archive_daily=r"^dump\.(\d{8})$")
    assert plugin.first_seen("project-b", None) is not None


def test_first_seen_returns_none_when_the_archive_is_absent():
    """Which is the state on every node that does not mount one."""
    plugin = _plugin(quota_archive="/nonexistent/archive/path")
    assert plugin.first_seen("project-anything", None) is None
    assert _plugin().first_seen("project-anything", None) is None


def test_the_latest_dump_is_read_by_the_configured_name(tmp_path):
    (tmp_path / "latest.quota").write_text(MMREPQUOTA)
    plugin = _plugin(quota_archive=str(tmp_path), quota_archive_latest="latest.quota")
    assert [r["name"] for r in plugin.latest_archive_rows()] == ["project-hpc", "jdoe42"]
    assert _plugin(quota_archive=str(tmp_path)).latest_archive_rows() == []


# --------------------------------------------------------------------------
# The plugin boundary
# --------------------------------------------------------------------------


def test_detection_never_consults_the_hostname():
    """A hostname is the least reliable signal available.

    nodetop filters GPU nodes on GRES rather than a hostname prefix because a
    node named like a big-memory GPU box can be a CPU node. Same instrument,
    same reason: detection uses a file only the site has, or a device name.
    """
    import inspect

    # The docstring explains that it does NOT use the hostname, so scanning
    # the raw source would match its own explanation. Strip the docstring and
    # the comments and scan only what executes.
    source = inspect.getsource(ConfiguredSite.detect)
    body = source.split('"""')
    code = body[0] + ("".join(body[2:]) if len(body) > 2 else "")
    code = "\n".join(line for line in code.splitlines() if not line.strip().startswith("#")).lower()

    for forbidden in ("hostname", "gethostname", "uname", "fqdn", "nodename"):
        assert forbidden not in code, "detect() reads %s, which is unreliable" % forbidden


def test_detection_accepts_a_device_name_without_the_site_command():
    plugin = _plugin()
    assert plugin.detect(None, [FakeMount("meadow3_cap")]) is True
    assert plugin.detect(None, [FakeMount("collie3_perf")]) is True


def test_detection_accepts_a_file_only_the_site_has(tmp_path):
    marker = tmp_path / "allocs"
    marker.write_text("")
    assert _plugin(detect_files=str(marker)).detect(None, []) is True


def test_detection_declines_an_unrelated_cluster():
    """The plugin must not claim a site its config does not describe."""
    plugin = _plugin()
    foreign = [FakeMount("procyon_home"), FakeMount("theta_fs0"), FakeMount("lustre1")]
    assert plugin.detect(None, foreign) is False
    assert plugin.detect(None, []) is False
    assert plugin.detect(None, [FakeMount("meadow3_cap")]) is True


def test_no_plugin_section_means_no_plugin():
    """The package ships knowing no site at all."""
    assert ConfiguredSite().detect(None, [FakeMount("meadow3_cap")]) is False
    assert ConfiguredSite().site_defaults()["fileset_prefixes"] == []
    assert detect_plugins(None, [FakeMount("meadow3_cap")]) == []


def test_a_section_with_no_signal_applies_wherever_it_is_read():
    assert ConfiguredSite({"description": "here"}).detect(None, []) is True


def test_site_defaults_keep_only_paths_that_exist(tmp_path):
    """One config serves every node, and some directories exist on one class
    of node only: a root that is not there must not become a row."""
    present = tmp_path / "datasets"
    present.mkdir()
    wrapper = tmp_path / "quota"
    wrapper.write_text("#!/bin/sh\n")
    plugin = _plugin(
        dataset_roots="%s, /nonexistent/datasets" % present,
        snapshot_roots="/nonexistent/snapshots",
        wrapper_paths="%s, /nonexistent/quota" % wrapper,
        extra_bin_dirs="/usr/lpp/mmfs/bin, /opt/site/bin",
    )
    defaults = plugin.site_defaults()
    assert defaults["dataset_roots"] == [str(present)]
    assert defaults["snapshot_roots"] == []
    assert defaults["wrapper_paths"] == [str(wrapper)]
    assert defaults["extra_bin_dirs"] == ["/usr/lpp/mmfs/bin", "/opt/site/bin"]
    assert defaults["fileset_prefixes"] == ["project2-", "project3-", "collie3-", "project-"]
    assert defaults["role_globs"] == [("/collie3", "project"), ("/collie3/*", "project")]


def test_the_plugin_section_is_read_from_site_config(tmp_path):
    conf = tmp_path / "site.conf"
    conf.write_text(SITE_CONF)
    site = load_site(paths=[str(conf)])
    assert site.plugin["fileset_groups"] == "project-hpc:hpc-staff"
    (plugin,) = available_plugins(site)
    assert plugin.describe() == "Example HPC (meadow2 / meadow3 / collie3)"
    assert plugin.detect(None, [FakeMount("collie3_cap")]) is True
    assert plugin.owns_fileset("project-hpc", ["hpc-staff"]) is True
    active = detect_plugins(None, [FakeMount("meadow2_perf")], site=site)
    assert [p.name for p in active] == ["site"]


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
    """So a site's rules can be exercised against a recorded transcript from a
    machine that is not that site.
    """
    active = detect_plugins(None, [], enabled=["site"])
    assert [p.name for p in active] == ["site"]
    assert detect_plugins(None, [], enabled=[]) == []


def test_allocation_json_round_trip():
    allocation = Allocation("acct", "cfs4/acct", size_gb=100.0, kind="PAY")
    payload = allocation.to_json()
    assert payload["location"] == "cfs4/acct"
    assert payload["path"] is None
