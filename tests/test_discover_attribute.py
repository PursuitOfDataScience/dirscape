"""Which quota scope governs a path, and the two gates that decide it.

Every command here is replayed from an inline transcript through
`RecordedRunner`, so the GPFS cases run on a machine with no GPFS. The
transcripts are copies of real output measured on this cluster; the Lustre one
is the documented format, because there is no Lustre mount here to measure.
"""

import os

from dirscape.discover.attribute import (
    GPFS_TOOL_DIRS,
    attribute,
    attribute_gpfs,
    parse_lfs_project,
    parse_mmlsattr,
    parse_xfs_lsproj,
)
from dirscape.discover.mounts import read_mount_table
from dirscape.model import Root, VerdictCategory
from dirscape.runner import Completed, Runner

GPFS_TABLE = "cap /project gpfs rw,relatime 0 0\ncap /home gpfs rw,relatime 0 0\n"
XFS_NOQUOTA_TABLE = "/dev/sda1 /tmp xfs rw,relatime,noquota 0 0\n"
XFS_PRJQUOTA_TABLE = "/dev/sda1 /data xfs rw,relatime,prjquota 0 0\n"
LUSTRE_TABLE = "fs@tcp:/fs /lustre lustre rw 0 0\n"

# Real `mmlsattr -L /project/hpc` output, measured.
MMLSATTR_OK = """\
file name:            /project/hpc
metadata replication: 1 max 2
immutable:            no
appendOnly:           no
flags:
storage pool name:    system
fileset name:         project-hpc
snapshot name:
creation time:        Wed Dec 23 05:34:10 2020
Misc attributes:      DIRECTORY
Encrypted:            no
"""


def _gpfs_root(path="/project/hpc"):
    return Root(path, role="project", device="cap", fstype="gpfs")


# --------------------------------------------------------------------------
# Parsers
# --------------------------------------------------------------------------


def test_parse_mmlsattr_takes_the_fileset_name():
    assert parse_mmlsattr(MMLSATTR_OK) == "project-hpc"


def test_parse_mmlsattr_does_not_confuse_snapshot_name_for_fileset_name():
    """``snapshot name:`` is two lines below and is usually empty.

    A substring match picks it up and returns "", which reads as "no fileset"
    for a path that has one.
    """
    reordered = "snapshot name:        \nfileset name:         home\n"
    assert parse_mmlsattr(reordered) == "home"


def test_parse_mmlsattr_returns_empty_for_unrelated_output():
    assert parse_mmlsattr("") == ""
    assert parse_mmlsattr("mmlsattr: some other complaint\n") == ""
    assert parse_mmlsattr("fileset name:\n") == ""


def test_parse_lfs_project_reads_the_id_and_treats_zero_as_none():
    assert parse_lfs_project("    1234 P /lustre/project/thing\n") == "1234"
    assert parse_lfs_project("       0 - /lustre/scratch/thing\n") == ""
    assert parse_lfs_project("lfs: unrecognised\n") == ""


def test_parse_xfs_lsproj_reads_projid():
    assert parse_xfs_lsproj("projid = 42\n") == "42"
    # Measured: `xfs_io -c lsproj /tmp` prints exactly this, unprivileged.
    assert parse_xfs_lsproj("projid = 0\n") == ""


# --------------------------------------------------------------------------
# GPFS
# --------------------------------------------------------------------------


def test_gpfs_fileset_is_read_per_path(recorded, cmd):
    """Per path, never per device. This is rapiDU's RD-18.

    One device here is mounted at /home, /project, /software and /programs,
    and those are four filesets with four quotas. Keying on the device makes
    ``rapidu -Q ~`` and ``rapidu -Q /software`` print byte-identical output.
    """
    root = _gpfs_root()
    runner = recorded(
        [cmd(["/usr/lpp/mmfs/bin/mmlsattr", "-L", "/project/hpc"], stdout=MMLSATTR_OK)],
        {"mmlsattr": "/usr/lpp/mmfs/bin/mmlsattr"},
    )
    verdict = attribute(root, runner, read_mount_table(text=GPFS_TABLE))

    assert root.fileset == "project-hpc"
    assert verdict.confirmed is True
    assert verdict.source == "mmlsattr -L"


def test_two_paths_on_one_device_get_different_filesets(recorded, cmd):
    """The RD-18 case, stated as two calls with two answers."""
    home = Root("/home", device="cap", fstype="gpfs")
    project = Root("/project", device="cap", fstype="gpfs")
    runner = recorded(
        [
            cmd(
                ["/usr/lpp/mmfs/bin/mmlsattr", "-L", "/home"],
                stdout="fileset name:         home\n",
            ),
            cmd(
                ["/usr/lpp/mmfs/bin/mmlsattr", "-L", "/project"],
                stdout="fileset name:         project\n",
            ),
        ],
        {"mmlsattr": "/usr/lpp/mmfs/bin/mmlsattr"},
    )
    mounts = read_mount_table(text=GPFS_TABLE)
    attribute(home, runner, mounts)
    attribute(project, runner, mounts)

    assert home.device == project.device
    assert home.fileset != project.fileset
    assert (home.fileset, project.fileset) == ("home", "project")


def test_exit_zero_with_an_error_on_stderr_is_a_failure(recorded, cmd):
    """GPFS `mm*` wrappers exit 0 on failure. Measured today:

        $ mmlsfileset meadow3_cap
        No filesets found owned by this user jdoe42
        mmlsfileset: tslsfileset failed. Error code 2.
        $ echo $?
        0

    So the exit status is not evidence of success, and a tool that gates on it
    reports an empty fileset as a successful lookup.
    """
    root = _gpfs_root()
    runner = recorded(
        [
            cmd(
                ["/usr/lpp/mmfs/bin/mmlsattr", "-L", "/project/hpc"],
                stdout="",
                stderr="mmlsattr: tslsattr failed. Error code 22.",
                returncode=0,
            )
        ],
        {"mmlsattr": "/usr/lpp/mmfs/bin/mmlsattr"},
    )
    verdict = attribute(root, runner, read_mount_table(text=GPFS_TABLE))

    assert root.fileset == "", "a failed lookup must not set a fileset"
    assert verdict.confirmed is False
    assert verdict.category == VerdictCategory.BACKEND_FAILED
    assert verdict.durable is False, "a backend failure is not a refusal"


def test_exit_zero_with_partial_stdout_is_also_a_failure(recorded, cmd):
    """The hole ``Completed.failed`` alone cannot close.

    ``failed`` treats "exited 0, empty stdout, non-empty stderr" as failure.
    It cannot treat this as failure, because stdout is NOT empty: the command
    printed some of its output and then broke. Without the parse as a second
    gate this sets an empty fileset and reports success.
    """
    root = _gpfs_root()
    partial = "file name:            /project/hpc\nmetadata replication: 1 max 2\n"
    runner = recorded(
        [
            cmd(
                ["/usr/lpp/mmfs/bin/mmlsattr", "-L", "/project/hpc"],
                stdout=partial,
                stderr="mmlsattr: tslsattr failed. Error code 22.",
                returncode=0,
            )
        ],
        {"mmlsattr": "/usr/lpp/mmfs/bin/mmlsattr"},
    )

    # The premise of the test: gate one really does pass here.
    probe = Completed(["mmlsattr"], 0, stdout=partial, stderr="mmlsattr: tslsattr failed.")
    assert probe.failed is False, "if this ever becomes True the second gate is untested"

    verdict = attribute(root, runner, read_mount_table(text=GPFS_TABLE))
    assert root.fileset == ""
    assert verdict.category == VerdictCategory.BACKEND_FAILED
    assert "no fileset name" in verdict.reason


def test_a_missing_mmlsattr_is_not_supported_rather_than_failed(recorded):
    root = _gpfs_root()
    runner = recorded([], {"mmlsattr": None})
    verdict = attribute(root, runner, read_mount_table(text=GPFS_TABLE))

    assert root.fileset == ""
    assert verdict.category == VerdictCategory.NOT_SUPPORTED
    assert verdict.refuted is False
    assert any("mmlsattr not found" in note for note in root.notes)


def test_gpfs_tools_are_looked_for_outside_path():
    """They are not on PATH here and are world-executable in /usr/lpp/mmfs/bin.

        $ command -v mmlsattr          -> (nothing)
        $ ls -l /usr/lpp/mmfs/bin/mmlsattr
        -r-xr-xr-x 1 root root 24328 mmlsattr

    A probe that used PATH alone concludes GPFS is absent on a GPFS cluster.
    """
    seen = {}

    class SpyRunner(Runner):
        def available(self, name, extra_dirs=()):
            seen[name] = tuple(extra_dirs)
            return None

        def run(self, argv, timeout=None, env=None):
            raise AssertionError("must not run a tool it could not find")

    attribute_gpfs(_gpfs_root(), SpyRunner())
    assert "/usr/lpp/mmfs/bin" in seen["mmlsattr"]
    assert GPFS_TOOL_DIRS == ("/usr/lpp/mmfs/bin",)


def test_a_site_bin_dir_is_searched_before_the_builtin():
    seen = {}

    class SpyRunner(Runner):
        def available(self, name, extra_dirs=()):
            seen[name] = tuple(extra_dirs)
            return None

        def run(self, argv, timeout=None, env=None):
            raise AssertionError("not reached")

    class FakeSite(object):
        extra_bin_dirs = ["/opt/site/bin"]

    attribute_gpfs(_gpfs_root(), SpyRunner(), None, FakeSite())
    assert seen["mmlsattr"][0] == "/opt/site/bin"
    assert "/usr/lpp/mmfs/bin" in seen["mmlsattr"], "the builtin must not be lost"


# --------------------------------------------------------------------------
# Lustre and XFS
# --------------------------------------------------------------------------


def test_lustre_project_id_becomes_the_scope(recorded, cmd):
    root = Root("/lustre/thing", fstype="lustre")
    runner = recorded(
        [
            cmd(
                ["/usr/bin/lfs", "project", "-d", "/lustre/thing"],
                stdout="  1234 P /lustre/thing\n",
            )
        ],
        {"lfs": "/usr/bin/lfs"},
    )
    verdict = attribute(root, runner, read_mount_table(text=LUSTRE_TABLE))
    assert root.fileset == "1234"
    assert verdict.confirmed is True


def test_lustre_project_zero_is_a_legitimate_empty_scope(recorded, cmd):
    root = Root("/lustre/thing", fstype="lustre")
    runner = recorded(
        [cmd(["/usr/bin/lfs", "project", "-d", "/lustre/thing"], stdout="  0 - /lustre/thing\n")],
        {"lfs": "/usr/bin/lfs"},
    )
    verdict = attribute(root, runner, read_mount_table(text=LUSTRE_TABLE))
    assert root.fileset == ""
    assert verdict.category == VerdictCategory.NOT_SUPPORTED
    assert verdict.refuted is False


def test_a_noquota_xfs_mount_is_refused_without_running_anything(recorded):
    """The mount options settle it, so no command is worth spending.

    Measured: both xfs mounts on this node carry ``noquota``. The strict
    recorded runner has no transcripts at all here, so if the code ran anything
    it would raise NotRecorded and this test would fail rather than pass
    quietly.
    """
    root = Root("/tmp", fstype="xfs")
    verdict = attribute(root, recorded([], {}), read_mount_table(text=XFS_NOQUOTA_TABLE))
    assert root.fileset == ""
    assert verdict.category == VerdictCategory.NOT_SUPPORTED
    assert "noquota" in verdict.reason


def test_an_xfs_mount_with_prjquota_is_asked(recorded, cmd):
    root = Root("/data/thing", fstype="xfs")
    runner = recorded(
        [cmd(["/usr/sbin/xfs_io", "-c", "lsproj", "/data/thing"], stdout="projid = 42\n")],
        {"xfs_io": "/usr/sbin/xfs_io"},
    )
    verdict = attribute(root, runner, read_mount_table(text=XFS_PRJQUOTA_TABLE))
    assert root.fileset == "42"
    assert verdict.confirmed is True


# --------------------------------------------------------------------------
# Everything else
# --------------------------------------------------------------------------


def test_a_filesystem_with_no_fileset_concept_is_not_an_error(recorded):
    """ "This filesystem has no scope to look up" is a legitimate state.

    It is reported as NOT_SUPPORTED, which is transient, so nothing downstream
    can render it as a refusal about the user's storage.
    """
    root = Root("/dev/shm", fstype="tmpfs")
    verdict = attribute(
        root, recorded([], {}), read_mount_table(text="tmpfs /dev/shm tmpfs rw 0 0\n")
    )
    assert root.fileset == ""
    assert verdict.value is None
    assert verdict.refuted is False
    assert verdict.category == VerdictCategory.NOT_SUPPORTED
    assert root.notes, "the reason has to be recorded on the root"


def test_the_fstype_is_taken_from_the_mount_table_when_the_root_lacks_one(recorded, cmd):
    root = Root("/project/hpc")
    runner = recorded(
        [cmd(["/usr/lpp/mmfs/bin/mmlsattr", "-L", "/project/hpc"], stdout=MMLSATTR_OK)],
        {"mmlsattr": "/usr/lpp/mmfs/bin/mmlsattr"},
    )
    assert attribute(root, runner, read_mount_table(text=GPFS_TABLE)).confirmed is True
    assert root.fileset == "project-hpc"


def test_an_exhausted_budget_stops_before_spending(recorded):
    class Spent(object):
        exhausted = True

    root = _gpfs_root()
    verdict = attribute(root, recorded([], {}), read_mount_table(text=GPFS_TABLE), Spent())
    assert verdict.category == VerdictCategory.NOT_PROBED
    assert root.fileset == ""


def test_the_module_never_reads_an_exit_status():
    """A source-level guard, because this is the rule most easily regressed.

    ``if result.returncode:`` looks correct and is wrong for this family of
    tools, and nothing in a passing test suite would notice.
    """
    here = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    source_path = os.path.join(here, "src", "dirscape", "discover", "attribute.py")
    with open(source_path, "r") as handle:
        source = handle.read()
    assert ".returncode" not in source
    assert ".failed" in source
