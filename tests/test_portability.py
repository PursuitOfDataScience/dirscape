"""Whole-pipeline runs on clusters that are not the development one.

The package's central claim is that it works on a site nobody here has an
account on. That cannot be tested by asserting on GPFS output, so each test
below builds a FAKE CLUSTER: real directories under `tmp_path`, a synthetic
mount table naming them, and a `RecordedRunner` that answers for exactly the
tools that site has and raises for anything else.

The raise matters. A backend that reaches for a tool the fixture did not
record is a backend making an assumption about the site, and `NotRecorded`
turns that into a test failure instead of a silent wrong answer.
"""

import os
import re

import pytest

from dirscape import cli
from dirscape.discover import discover, read_identity, read_mount_table
from dirscape.model import Reach
from dirscape.quota import default_backends, read_all
from dirscape.render import render_atlas, render_json, render_matrix, render_tree
from dirscape.runner import Budget, RecordedRunner
from dirscape.sitecfg import load_site


def _cluster(tmp_path, fstype, device, mounts=("home", "work")):
    """Real directories plus a mount table that claims a given filesystem."""
    made = {}
    for name in mounts:
        target = tmp_path / name / "me"
        target.mkdir(parents=True)
        made[name] = target
    lines = []
    for name in mounts:
        lines.append("%s %s %s rw,relatime 0 0" % (device, tmp_path / name, fstype))
    # A pseudo mount, because every real table has them and they must be
    # classified out rather than probed.
    lines.append("proc /proc proc rw,nosuid 0 0")
    return made, read_mount_table(text="\n".join(lines) + "\n")


def _sweep(table, runner, tmp_path):
    site = load_site(paths=[])
    me = read_identity(table, site=site)
    budget = Budget(total_s=20.0)
    roots = discover(runner, table, me, budget, site)
    attempts = read_all(default_backends(site), runner, table, budget, ["/"])
    return roots, attempts, site, me


# --------------------------------------------------------------------------
# A site with no quota tooling whatsoever
# --------------------------------------------------------------------------


def test_a_site_with_no_quota_tool_at_all_still_reports_its_storage(tmp_path):
    """The Brook case: an NFS export with no quota system anywhere.

    Every backend must decline, and declining is not failing. The tool's job
    here is to report the storage it found and say honestly that no quota
    exists, which `statvfs` then turns into real free space.
    """
    made, table = _cluster(tmp_path, "nfs4", "fileserver:/export")
    runner = RecordedRunner(
        [],
        probes={
            "quota": None,
            "mmlsquota": None,
            "mmlsattr": None,
            "lfs": None,
            "xfs_quota": None,
            "xfs_io": None,
            "accounts": None,
            "rcchelp": None,
        },
    )

    roots, attempts, site, me = _sweep(table, runner, tmp_path)

    assert roots, "storage that exists must be reported even with no quota tool"
    assert all(not snap.available or not snap.rows for snap in attempts), (
        "no backend can have produced rows on a site with none of their tools"
    )
    for snap in attempts:
        if not snap.available:
            assert snap.reason, "a declining backend must say why"


def test_the_whole_pipeline_renders_on_a_quotaless_site(tmp_path):
    """And the table is useful rather than a column of question marks."""
    made, table = _cluster(tmp_path, "nfs4", "fileserver:/export")
    runner = RecordedRunner(
        [],
        probes=dict.fromkeys(
            ("quota", "mmlsquota", "mmlsattr", "lfs", "xfs_quota", "xfs_io", "accounts"), None
        ),
    )

    run = cli.Run()
    run.site = load_site(paths=[])
    run.mounts = table
    run.identity = read_identity(table, site=run.site)
    run.roots = discover(runner, table, run.identity, Budget(20.0), run.site)
    run.quota_attempts = read_all(default_backends(run.site), runner, table, Budget(20.0), ["/"])
    cli._attach_quota(run, Budget(20.0), runner)
    cli._mark_stranded(run)

    shown, hidden = cli._visible(run, show_all=False)
    text = render_atlas(shown, group=True, all_roots=run.roots)

    assert text.strip(), "a quotaless site still gets a table"
    # `statvfs` answers where no quota exists, so the figures are not all
    # question marks. Asserted on a FIGURE rather than on the word "free",
    # which used to be part of the cell (`886G free`) and is now a column
    # heading: these fixture paths are long enough to fall back to the
    # stacked layout, and that layout has no headings.
    assert re.search(r"\d[\d.]*[KMGTP]?B?\b", text), "statvfs should supply free space: %r" % (
        text,
    )


# --------------------------------------------------------------------------
# A Lustre site
# --------------------------------------------------------------------------

LFS_QUOTA = """\
Disk quotas for usr me (uid 1000):
     Filesystem  kbytes   quota   limit   grace   files   quota   limit   grace
      /lustre/work  1048576  4194304  8388608       -    1024   10000   20000       -
"""


def test_a_lustre_site_reads_its_quota(tmp_path):
    """The one backend with no live coverage anywhere in this project.

    No Lustre filesystem was reachable where this was written and `lfs` is not
    installed, so this is the only thing standing between that code and a
    first run on somebody else's cluster.
    """
    made, table = _cluster(tmp_path, "lustre", "mds@tcp:/work", mounts=("work",))
    work = str(tmp_path / "work")
    runner = RecordedRunner(
        [
            {"argv": ["/usr/bin/lfs", "quota", "-u", "me", work], "stdout": LFS_QUOTA},
            {"argv": ["/usr/bin/lfs", "quota", "-g", "me", work], "stdout": LFS_QUOTA},
            {"argv": ["/usr/bin/lfs", "project", "-d", work], "stdout": "0 P %s\n" % (work,)},
        ],
        probes={
            "lfs": "/usr/bin/lfs",
            "quota": None,
            "mmlsquota": None,
            "mmlsattr": None,
            "xfs_quota": None,
            "xfs_io": None,
            "accounts": None,
        },
        strict=False,
    )

    roots, attempts, site, me = _sweep(table, runner, tmp_path)

    assert roots
    sources = [snap.source for snap in attempts]
    assert any("lfs" in (src or "") for src in sources), "the Lustre backend never ran: %s" % (
        sources,
    )


# --------------------------------------------------------------------------
# Things that must never happen anywhere
# --------------------------------------------------------------------------


def test_no_backend_reaches_for_a_tool_the_site_does_not_have(tmp_path):
    """Strict mode turns an assumption about the site into a failure.

    A backend that runs a command the fixture never recorded is a backend
    guessing what is installed. On a real foreign cluster that guess is a
    wrong answer nobody can see; here it is `NotRecorded`.
    """
    made, table = _cluster(tmp_path, "ext4", "/dev/sda1")
    runner = RecordedRunner(
        [],
        probes=dict.fromkeys(
            (
                "quota",
                "mmlsquota",
                "mmlsattr",
                "mmlsfileset",
                "lfs",
                "xfs_quota",
                "xfs_io",
                "accounts",
                "rcchelp",
            ),
            None,
        ),
        strict=True,
    )

    # No raise is the assertion.
    _sweep(table, runner, tmp_path)


def test_a_pseudo_mount_is_never_offered_as_storage(tmp_path):
    made, table = _cluster(tmp_path, "ext4", "/dev/sda1")
    runner = RecordedRunner(
        [],
        probes=dict.fromkeys(
            ("quota", "mmlsquota", "mmlsattr", "lfs", "xfs_quota", "xfs_io", "accounts"), None
        ),
    )
    roots, _, _, _ = _sweep(table, runner, tmp_path)
    assert not any(r.path == "/proc" for r in roots)


@pytest.mark.parametrize("view", ["atlas", "matrix", "tree", "json"])
def test_every_view_renders_on_a_foreign_cluster(tmp_path, view):
    """A view that crashes on an unfamiliar site is worse than no view."""
    made, table = _cluster(tmp_path, "nfs4", "fileserver:/export")
    runner = RecordedRunner(
        [],
        probes=dict.fromkeys(
            ("quota", "mmlsquota", "mmlsattr", "lfs", "xfs_quota", "xfs_io", "accounts"), None
        ),
    )
    roots, _, site, _ = _sweep(table, runner, tmp_path)

    render = {
        "atlas": lambda: render_atlas(roots, group=True, all_roots=roots),
        "matrix": lambda: render_matrix(roots, site=site),
        "tree": lambda: render_tree(roots),
        "json": lambda: render_json(roots),
    }[view]
    out = render()
    assert out.strip(), "%s rendered nothing" % (view,)


def test_a_root_the_user_cannot_enter_is_reported_as_closed(tmp_path):
    """The access probe has to work on a filesystem nobody here has seen."""
    if os.getuid() == 0:
        pytest.skip("root can enter anything")
    made, table = _cluster(tmp_path, "ext4", "/dev/sda1", mounts=("home", "locked"))
    shut = tmp_path / "locked" / "me"
    shut.chmod(0o000)
    try:
        runner = RecordedRunner(
            [],
            probes=dict.fromkeys(
                ("quota", "mmlsquota", "mmlsattr", "lfs", "xfs_quota", "xfs_io", "accounts"), None
            ),
        )
        roots, _, _, _ = _sweep(table, runner, tmp_path)
        found = {r.path: r for r in roots}
        assert str(shut) in found, "the closed directory must still be reported"
        assert found[str(shut)].reach == Reach.CLOSED
    finally:
        shut.chmod(0o755)
