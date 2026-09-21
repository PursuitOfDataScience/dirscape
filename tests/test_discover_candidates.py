"""Candidate discovery: the five sources, the dedupe, and the budget.

The whole world each test runs against is built under ``tmp_path`` and described
to the code through an inline mount table whose mountpoints point into it.
Nothing reads this cluster's mount table, hostname or device names, so the suite
is portable: that is rapiDU's RD-10, where a test pinning the development
cluster's identity failed on every other host.
"""

import os

import pytest

from dirscape.discover import candidates as candidates_module
from dirscape.discover.candidates import (
    SOURCE_ALLOCATION,
    SOURCE_DIR_OWNER,
    SOURCE_ENV,
    SOURCE_GROUP_TEMPLATE,
    SOURCE_MOUNTS,
    SOURCE_QUOTA_FILESET,
    discover,
    role_for_path,
)
from dirscape.discover.identity import Identity
from dirscape.discover.mounts import read_mount_table
from dirscape.model import Reach, VerdictCategory
from dirscape.plugins import Allocation
from dirscape.runner import Budget, Runner
from dirscape.sitecfg import Site

skip_if_root = pytest.mark.skipif(
    os.geteuid() == 0, reason="root bypasses the permission bits these tests set"
)


class NoRunner(Runner):
    """Discovery must not run a single external command.

    It is all table reads, os.access and one level of scandir, which is what
    keeps it affordable enough to run unconditionally. Any command at all fails
    the test rather than slipping through.
    """

    def available(self, name, extra_dirs=()):
        raise AssertionError("discovery must not probe for %r" % (name,))

    def run(self, argv, timeout=None, env=None):
        raise AssertionError("discovery must not run %r" % (argv,))


def run(mounts, identity, budget=None, site=None, env=None, **kwargs):
    """Call `discover` with an EXPLICIT environment, always.

    Defaulting to ``os.environ`` inside a test would let the real ``$HOME``,
    ``$SCRATCH`` and ``$TMPDIR`` of whoever runs the suite become roots, so the
    assertions would depend on the machine. That is the RD-10 trap, and it
    caught this file once already: the budget test below failed only because the
    developer's real home directory had wandered into the result.
    """
    return discover(
        NoRunner(), mounts, identity, budget, site, env={} if env is None else env, **kwargs
    )


def build_world(tmp_path):
    """A small cluster under tmp_path, with the shapes measured on a real one.

    Returns ``(mount_table_text, paths)``. Two mountpoints share one device in
    the table, as /home and /project really do here.
    """
    root = tmp_path / "world"
    paths = {}
    for name in ("home", "project", "scratch", "software"):
        (root / name).mkdir(parents=True)
        paths[name] = root / name
    (root / "home" / "me").mkdir()
    paths["home_me"] = root / "home" / "me"

    text = (
        "sysfs /sys sysfs rw 0 0\n"
        "proc /proc proc rw 0 0\n"
        "cap {home} gpfs rw,relatime 0 0\n"
        "cap {project} gpfs rw,relatime 0 0\n"
        "cap {software} gpfs rw,relatime 0 0\n"
        "perf {scratch} gpfs rw,relatime 0 0\n"
    ).format(
        home=paths["home"],
        project=paths["project"],
        software=paths["software"],
        scratch=paths["scratch"],
    )
    return text, paths


def make_identity(groups=(), gids=None, user="me"):
    """An identity with no connection to the account running the suite."""
    return Identity(
        uid=os.getuid(),
        gid=os.getgid(),
        user=user,
        gids=list(gids if gids is not None else [os.getgid()]),
        groups=list(groups),
        hostname="test-node",
        node_class_="login",
        fingerprint="deadbeef01",
        cluster="testcluster",
    )


def site_with_roles(paths):
    """Roles declared rather than guessed, so tmp_path names cannot decide them."""
    site = Site()
    site.role_globs = [
        (str(paths["home"]) + "*", "home"),
        (str(paths["project"]) + "*", "project"),
        (str(paths["scratch"]) + "*", "scratch"),
        (str(paths["software"]) + "*", "software"),
    ]
    return site


def find(roots, path):
    for root in roots:
        if root.path == str(path):
            return root
    return None


# --------------------------------------------------------------------------
# Source (a): mounts
# --------------------------------------------------------------------------


def test_every_non_pseudo_mountpoint_becomes_a_root(tmp_path):
    text, paths = build_world(tmp_path)
    mounts = read_mount_table(text=text)
    roots = run(mounts, make_identity(), None, site_with_roles(paths))

    for key in ("home", "project", "software", "scratch"):
        root = find(roots, paths[key])
        assert root is not None, "%s must be discovered" % (key,)
        assert SOURCE_MOUNTS in root.sources
        assert root.mounted.confirmed is True
        assert root.present.confirmed is True
        assert root.reach == Reach.LISTABLE


def test_pseudo_mounts_are_not_roots(tmp_path):
    text, paths = build_world(tmp_path)
    roots = run(read_mount_table(text=text), make_identity(), None, site_with_roles(paths))
    assert find(roots, "/sys") is None
    assert find(roots, "/proc") is None


def test_device_and_fstype_come_from_the_enclosing_mount(tmp_path):
    text, paths = build_world(tmp_path)
    roots = run(read_mount_table(text=text), make_identity(), None, site_with_roles(paths))
    home = find(roots, paths["home"])
    assert (home.device, home.fstype) == ("cap", "gpfs")
    scratch = find(roots, paths["scratch"])
    assert scratch.device == "perf"


# --------------------------------------------------------------------------
# Source (b): env
# --------------------------------------------------------------------------


def test_env_variables_become_roots(tmp_path):
    text, paths = build_world(tmp_path)
    env = {"HOME": str(paths["home_me"]), "SCRATCH": str(paths["scratch"])}
    roots = run(
        read_mount_table(text=text),
        make_identity(),
        None,
        site_with_roles(paths),
        env=env,
    )
    home_me = find(roots, paths["home_me"])
    assert home_me is not None
    assert SOURCE_ENV in home_me.sources
    assert any("$HOME" in note for note in home_me.notes)


def test_a_home_symlink_that_leaves_the_filesystem_is_followed(tmp_path):
    """The home-quota workaround, and why crosses_boundary is not st_dev.

    Measured here: ``~/.cache -> /project/hpc/jdoe42/.cache`` moves the bytes
    into the ``project-hpc`` fileset, and ``/home`` and ``/project`` are two
    mountpoints of ONE GPFS device, both st_dev 54. So the device comparison
    reports no crossing for the exact case the field exists for, and the
    mountpoint comparison is what gets it right.
    """
    text, paths = build_world(tmp_path)
    target = paths["project"] / "me-cache"
    target.mkdir()
    link = paths["home_me"] / ".cache"
    link.symlink_to(str(target))

    mounts = read_mount_table(text=text)
    # The premise: both sides really are one device, so st_dev cannot see it.
    assert os.stat(str(paths["home"])).st_dev == os.stat(str(paths["project"])).st_dev

    roots = run(
        mounts,
        make_identity(),
        None,
        site_with_roles(paths),
        env={"HOME": str(paths["home_me"])},
    )
    found = find(roots, target)
    assert found is not None, "a symlink out of $HOME must be followed"
    assert SOURCE_ENV in found.sources
    assert any("symlink" in note for note in found.notes)


def test_a_symlink_target_is_an_annotation_not_a_row_of_its_own(tmp_path):
    """The defect this replaced produced eleven junk rows and lost the good one.

    Measured output before the fix: rows for ``/project/hpc/jdoe42/.cache``,
    ``/project/hpc/jdoe42/.cache/nv``, ``/project/hpc/jdoe42/.cache/triton``,
    ``/project/hpc/jdoe42/.codex`` and seven more, all tagged ``env``, while
    ``/project/hpc/jdoe42`` itself was absent. A cache subdirectory is not a
    storage root and the user's own project directory is the row that matters.
    """
    text, paths = build_world(tmp_path)
    owned = paths["project"] / "me"
    owned.mkdir()
    cache = owned / ".cache"
    cache.mkdir()
    deep = cache / "nv"
    deep.mkdir()
    (paths["home_me"] / ".cache").symlink_to(str(cache))
    (paths["home_me"] / ".nv").symlink_to(str(deep))

    roots = run(
        read_mount_table(text=text),
        make_identity(),
        None,
        site_with_roles(paths),
        env={"HOME": str(paths["home_me"])},
    )

    # The owned ancestor is promoted, once, however many symlinks point into it.
    promoted = find(roots, owned)
    assert promoted is not None, "the owned project directory must be a root"
    assert SOURCE_ENV in promoted.sources

    # And the cache directories are not roots.
    assert find(roots, cache) is None, "a cache directory is not a storage root"
    assert find(roots, deep) is None

    # The crossing is recorded on the home root instead.
    home = find(roots, paths["home_me"])
    assert home.crosses_boundary is True
    assert home.policy["crosses_to"] == [str(owned)]
    assert any(".cache" in note and "billed against" in note for note in home.notes)


def test_the_promoted_ancestor_falls_back_when_nobody_owns_anything(tmp_path):
    """A shared dataset target still resolves to something real."""
    text, paths = build_world(tmp_path)
    shared = paths["project"] / "somebody-else" / "deep" / "deeper"
    shared.mkdir(parents=True)
    (paths["home_me"] / "link").symlink_to(str(shared))

    roots = run(
        read_mount_table(text=text),
        make_identity(gids=[os.getgid() + 99999]),
        None,
        site_with_roles(paths),
        env={"HOME": str(paths["home_me"])},
    )
    # The immediate child of the mountpoint, which is the fileset junction
    # level on this cluster, rather than the four-deep target.
    assert find(roots, paths["project"] / "somebody-else") is not None
    assert find(roots, shared) is None


def test_a_home_symlink_inside_the_same_filesystem_is_not_a_crossing(tmp_path):
    text, paths = build_world(tmp_path)
    inside = paths["home"] / "elsewhere-in-home"
    inside.mkdir()
    link = paths["home_me"] / "link"
    link.symlink_to(str(inside))

    roots = run(
        read_mount_table(text=text),
        make_identity(),
        None,
        site_with_roles(paths),
        env={"HOME": str(paths["home_me"])},
    )
    for root in roots:
        assert root.crosses_boundary is False


# --------------------------------------------------------------------------
# Sources (c) and (d): the one the templates cannot replace
# --------------------------------------------------------------------------


def test_dir_owner_finds_a_directory_the_group_template_misses(tmp_path):
    """The measured reason the dir-owner source is not optional.

    On this cluster ``/project/hpc`` is group-owned by ``hpc-staff``, its
    directory is called ``hpc`` and its fileset is ``project-hpc``: group name,
    directory name and fileset name are three different strings and no
    transform connects them. Only reading the directory entries connects them.

    Built so the template source CANNOT win: the only group name the identity
    carries is one no directory is named after.
    """
    text, paths = build_world(tmp_path)
    owned = paths["project"] / "differently-named"
    owned.mkdir()

    identity = make_identity(groups=["a-group-with-no-directory"], gids=[os.getgid()])
    roots = run(read_mount_table(text=text), identity, None, site_with_roles(paths))

    found = find(roots, owned)
    assert found is not None, "dir-owner must find a directory owned by one of my groups"
    assert SOURCE_DIR_OWNER in found.sources
    assert SOURCE_GROUP_TEMPLATE not in found.sources
    # And the template source really did draw a blank, which is the point.
    assert find(roots, paths["project"] / "a-group-with-no-directory") is None


def test_dir_owner_skips_entries_owned_by_other_groups(tmp_path):
    """14 of 21 groups here have no storage directory, and two are licence
    groups (``amber``, ``lumerical``), so a loose filter invents roots."""
    text, paths = build_world(tmp_path)
    mine = paths["project"] / "mine"
    mine.mkdir()
    theirs = paths["project"] / "theirs"
    theirs.mkdir()

    # A gid set that deliberately excludes the real one.
    identity = make_identity(gids=[os.getgid() + 99999])
    roots = run(read_mount_table(text=text), identity, None, site_with_roles(paths))
    assert find(roots, mine) is None
    assert find(roots, theirs) is None


def test_dir_owner_does_not_scan_a_software_root(tmp_path):
    """Measured: 713 of 746 entries under /software match this user's gid set.

    They are group-owned by ``hpc-software`` and the user is a member, so group
    ownership is a useless signal there and scanning it would flood the report
    with 713 false roots.
    """
    text, paths = build_world(tmp_path)
    entry = paths["software"] / "some-package"
    entry.mkdir()

    roots = run(read_mount_table(text=text), make_identity(), None, site_with_roles(paths))
    assert find(roots, entry) is None, "a software root must not be scanned by dir-owner"


def test_group_template_finds_a_directory_named_after_a_group(tmp_path):
    text, paths = build_world(tmp_path)
    named = paths["project"] / "teamname"
    named.mkdir()

    identity = make_identity(groups=["teamname"], gids=[os.getgid() + 99999])
    roots = run(read_mount_table(text=text), identity, None, site_with_roles(paths))
    found = find(roots, named)
    assert found is not None
    assert SOURCE_GROUP_TEMPLATE in found.sources


def test_group_template_strips_a_pi_prefix(tmp_path):
    """Membership of ``pi-smith`` can grant access to a directory ``smith``."""
    text, paths = build_world(tmp_path)
    named = paths["project"] / "smith"
    named.mkdir()

    identity = make_identity(groups=["pi-smith"], gids=[os.getgid() + 99999])
    roots = run(read_mount_table(text=text), identity, None, site_with_roles(paths))
    assert find(roots, named) is not None


def test_a_group_with_no_directory_produces_no_root(tmp_path):
    text, paths = build_world(tmp_path)
    identity = make_identity(groups=["amber", "lumerical"], gids=[os.getgid() + 99999])
    roots = run(read_mount_table(text=text), identity, None, site_with_roles(paths))
    assert find(roots, paths["project"] / "amber") is None
    assert find(roots, paths["project"] / "lumerical") is None


def test_a_per_user_scratch_directory_is_found_by_template(tmp_path):
    """One stat instead of the 13,907-entry scandir of a real /scratch."""
    text, paths = build_world(tmp_path)
    mine = paths["scratch"] / "me"
    mine.mkdir()
    roots = run(
        read_mount_table(text=text),
        make_identity(user="me"),
        None,
        site_with_roles(paths),
    )
    found = find(roots, mine)
    assert found is not None
    assert SOURCE_GROUP_TEMPLATE in found.sources


# --------------------------------------------------------------------------
# The dedupe
# --------------------------------------------------------------------------


def test_two_paths_to_one_directory_collapse_to_one_root(tmp_path):
    """Keyed on ``(st_dev, st_ino)``, never on the path string.

    Measured cases on this cluster: ``/home``, ``/project``, ``/software`` and
    ``/programs`` are each reachable a second time under
    ``/gpfs/meadow3/cap/``, and ``/tmp`` and ``/scratch/local`` are one xfs
    directory mounted twice, identical down to the inode. A path-string dedupe
    reports nine roots where there are four, each carrying the same quota, so
    every total doubles.
    """
    real = tmp_path / "real"
    real.mkdir()
    alias = tmp_path / "an-alias-path"
    alias.symlink_to(str(real))

    text = "cap %s gpfs rw 0 0\ncap %s gpfs rw 0 0\n" % (real, alias)
    mounts = read_mount_table(text=text)

    # The premise: two different strings, one inode.
    assert str(real) != str(alias)
    left, right = os.stat(str(real)), os.stat(str(alias))
    assert (left.st_dev, left.st_ino) == (right.st_dev, right.st_ino)

    roots = run(mounts, make_identity(), None, Site())
    matching = [r for r in roots if r.path in (str(real), str(alias))]
    assert len(matching) == 1, "one directory must produce one root, not two"

    keeper = matching[0]
    assert keeper.path == str(real), "the shortest path is the one kept"
    assert any("also reachable at" in note for note in keeper.notes)
    assert str(alias) in " ".join(keeper.notes), "the alias must still be recorded"


def test_two_mountpoints_of_one_device_are_not_collapsed(tmp_path):
    """One device is not one directory.

    ``/home`` and ``/project`` share st_dev 54 here and are different trees
    with different quotas, so a dedupe on the device would merge two unrelated
    roots. The inode is what separates them.
    """
    text, paths = build_world(tmp_path)
    roots = run(read_mount_table(text=text), make_identity(), None, site_with_roles(paths))
    home, project = find(roots, paths["home"]), find(roots, paths["project"])
    assert home is not None and project is not None
    assert home.device == project.device == "cap"
    assert home.identity != project.identity


def test_sources_are_merged_when_two_sources_find_one_root(tmp_path):
    """More than one source finding a root is normal and is itself evidence."""
    text, paths = build_world(tmp_path)
    named = paths["project"] / "teamname"
    named.mkdir()
    identity = make_identity(groups=["teamname"], gids=[os.getgid()])
    roots = run(read_mount_table(text=text), identity, None, site_with_roles(paths))
    found = find(roots, named)
    assert SOURCE_GROUP_TEMPLATE in found.sources
    assert SOURCE_DIR_OWNER in found.sources


def test_output_is_sorted_and_stable(tmp_path):
    """An unstable order turns every diff into a page of spurious changes."""
    text, paths = build_world(tmp_path)
    mounts = read_mount_table(text=text)
    site = site_with_roles(paths)
    first = [r.path for r in run(mounts, make_identity(), None, site)]
    second = [r.path for r in run(mounts, make_identity(), None, site)]
    assert first == sorted(first)
    assert first == second


# --------------------------------------------------------------------------
# Source (e): quota filesets
# --------------------------------------------------------------------------


def test_a_fileset_name_is_mapped_to_a_path(tmp_path):
    text, paths = build_world(tmp_path)
    # The measured convention: fileset `project-<group>` lives at
    # `<project mount>/<group>`.
    owned = paths["project"] / "hpc"
    owned.mkdir()

    site = site_with_roles(paths)
    site.fileset_prefixes = ["project-"]
    mounts = read_mount_table(text=text)
    roots = run(
        mounts,
        make_identity(gids=[os.getgid() + 99999]),
        None,
        site,
        filesets=["project-" + os.path.basename(str(paths["project"]))],
    )
    # The head of the fileset name has to match a real mountpoint basename, and
    # here the mountpoint basename is "project", so build the name from it.
    del roots

    named = "%s-hpc" % (os.path.basename(str(paths["project"])),)
    roots = run(
        mounts,
        make_identity(gids=[os.getgid() + 99999]),
        None,
        site,
        filesets=[named],
    )
    found = find(roots, owned)
    assert found is not None, "a fileset name must map onto its directory"
    assert SOURCE_QUOTA_FILESET in found.sources
    assert found.fileset == named


def test_an_unplaceable_fileset_never_invents_a_path(tmp_path):
    """Inventing a plausible path is the RD-3 mistake in miniature.

    An earlier version joined the tail of a split name onto every project-like
    root, which turned the fileset ``cfs9-ghost`` into ``/project/ghost``: a
    path nothing has ever seen, printed as a root.
    """
    text, paths = build_world(tmp_path)
    roots = run(
        read_mount_table(text=text),
        make_identity(),
        None,
        site_with_roles(paths),
        filesets=["cfs9-ghost"],
    )
    for root in roots:
        assert "ghost" not in root.path, "no path may be invented for %s" % (root.path,)


def test_a_path_the_backend_published_is_trusted_even_when_absent(tmp_path):
    """The backend saying so is evidence; inference is not.

    This is the stranded case: the quota layer can see the fileset and the
    directory is not here.
    """
    text, paths = build_world(tmp_path)
    missing = paths["project"] / "gone-from-this-node"
    roots = run(
        read_mount_table(text=text),
        make_identity(),
        None,
        site_with_roles(paths),
        filesets=[("some-fileset", str(missing))],
    )
    found = find(roots, missing)
    assert found is not None
    assert found.fileset == "some-fileset"
    assert found.present.refuted is True
    assert found.present.category == VerdictCategory.NOT_PRESENT
    # Mounted, because the mount that would hold it is right there.
    assert found.mounted.confirmed is True
    assert found.reach == Reach.UNKNOWN


def test_an_ambiguous_fileset_name_is_dropped_rather_than_guessed(tmp_path):
    """``scratch``, ``home`` and ``software`` are fileset names on more than
    one device here, so a bare name cannot say which device a row belongs to.

    `QuotaRow.guessed` already states the rule for an inferred mount: drop on
    ambiguity. This follows it.
    """
    text, paths = build_world(tmp_path)
    # Two project-like roots that both contain a directory of the same name.
    (paths["project"] / "shared").mkdir()
    (paths["scratch"] / "shared").mkdir()
    roots = run(
        read_mount_table(text=text),
        make_identity(gids=[os.getgid() + 99999]),
        None,
        site_with_roles(paths),
        filesets=["shared"],
    )
    placed = [r for r in roots if r.fileset == "shared"]
    assert placed == [], "an ambiguous name must not be placed at either path"


# --------------------------------------------------------------------------
# Allocations
# --------------------------------------------------------------------------


def test_an_allocation_with_no_path_here_becomes_an_elsewhere_row(tmp_path):
    """The most useful row this tool prints.

    Measured at this site: the allocation database reports space on ``cfs1``,
    ``cfs2``, ``cfs4`` and ``project3``, and none of those paths exist on the
    node that printed them. "Allocated yes, mounted no" is not an error, it is
    the answer.
    """
    text, paths = build_world(tmp_path)
    allocation = Allocation(
        account="some-account",
        location="cfs4/some-account",
        size_gb=256000.0,
        source="allocs storage",
    )
    roots = run(
        read_mount_table(text=text),
        make_identity(),
        None,
        site_with_roles(paths),
        allocations=[allocation],
    )
    elsewhere = [r for r in roots if r.elsewhere]
    assert len(elsewhere) == 1
    row = elsewhere[0]
    assert row.allocated.confirmed is True
    assert row.mounted.refuted is True
    assert row.mounted.category == VerdictCategory.NOT_MOUNTED_HERE
    assert row.present.refuted is True
    assert row.present.category == VerdictCategory.NOT_PRESENT
    assert SOURCE_ALLOCATION in row.sources

    # The location is NOT a path and must not sit in the path field. Putting it
    # there produced rows reading `cfs4/hpc-staff`, with no leading slash,
    # looking like relative paths a user could cd into.
    assert row.path == ""
    assert row.policy["allocation_location"] == "cfs4/some-account"
    assert row.policy["allocation_gb"] == 256000.0
    assert any("location and not a path" in note for note in row.notes)


def test_allocations_on_one_location_are_merged(tmp_path):
    """Measured: ``cfs4/hpc-staff`` is granted twice, as 2718 and 2719.

    The database issues one record per grant, not per filesystem, so two rows
    for one directory is a reporting bug rather than two pieces of storage.
    """
    text, paths = build_world(tmp_path)
    roots = run(
        read_mount_table(text=text),
        make_identity(),
        None,
        site_with_roles(paths),
        allocations=[
            Allocation(account="acct", location="cfs4/acct", size_gb=100.0),
            Allocation(account="acct", location="cfs4/acct", size_gb=150.0),
        ],
    )
    rows = [r for r in roots if r.policy.get("allocation_location") == "cfs4/acct"]
    assert len(rows) == 1, "two grants on one location are one row"
    assert rows[0].policy["allocation_gb"] == 250.0
    assert any("2 allocations" in note for note in rows[0].notes)


def test_a_pathless_allocation_row_sorts_after_the_real_roots(tmp_path):
    text, paths = build_world(tmp_path)
    roots = run(
        read_mount_table(text=text),
        make_identity(),
        None,
        site_with_roles(paths),
        allocations=[Allocation(account="acct", location="cfs4/acct")],
    )
    assert roots[-1].path == "", "an empty path must not sort above the user's home"


def test_an_allocation_that_names_a_real_root_marks_it_allocated(tmp_path):
    """Recognising a path another source found is not deriving one."""
    text, paths = build_world(tmp_path)
    owned = paths["project"] / "teamname"
    owned.mkdir()
    identity = make_identity(groups=["teamname"], gids=[os.getgid()])
    location = "%s/teamname" % (os.path.basename(str(paths["project"])),)

    roots = run(
        read_mount_table(text=text),
        identity,
        None,
        site_with_roles(paths),
        allocations=[Allocation(account="a", location=str(owned).lstrip("/"))],
    )
    found = find(roots, owned)
    assert found is not None
    assert found.allocated.confirmed is True
    assert found.elsewhere is False, "a mounted root is not elsewhere"
    assert SOURCE_ALLOCATION in found.sources
    del location


def test_an_allocation_with_a_real_path_is_matched_on_it(tmp_path):
    text, paths = build_world(tmp_path)
    roots = run(
        read_mount_table(text=text),
        make_identity(),
        None,
        site_with_roles(paths),
        allocations=[Allocation(account="a", location="whatever", path=str(paths["project"]))],
    )
    found = find(roots, paths["project"])
    assert found.allocated.confirmed is True


def test_allocated_is_left_alone_when_no_allocation_source_ran(tmp_path):
    """A later layer sets it. Discovery must not answer a question it was not
    asked, and NOT_PROBED is that answer."""
    text, paths = build_world(tmp_path)
    roots = run(read_mount_table(text=text), make_identity(), None, site_with_roles(paths))
    for root in roots:
        assert root.allocated.category == VerdictCategory.NOT_PROBED
        assert root.allocated.value is None


# --------------------------------------------------------------------------
# Reach, roles and the budget
# --------------------------------------------------------------------------


@skip_if_root
def test_a_traverse_only_root_is_reported_and_still_counts_as_reachable(tmp_path):
    text, paths = build_world(tmp_path)
    locked = paths["project"] / "traverse-only"
    locked.mkdir()
    locked.chmod(0o111)
    try:
        identity = make_identity(groups=["traverse-only"], gids=[os.getgid() + 99999])
        roots = run(read_mount_table(text=text), identity, None, site_with_roles(paths))
        found = find(roots, locked)
        assert found is not None, "a traverse-only directory must not disappear"
        assert found.reach == Reach.TRAVERSE
        assert found.reachable is True
    finally:
        locked.chmod(0o755)


@skip_if_root
def test_the_role_does_not_change_the_measured_reach(tmp_path):
    """Roles are advisory. No access verdict may be decided from one.

    The role does legitimately steer WHICH roots get a one-level scan, since
    scanning a software root is measurably useless here. What it must never do
    is change the answer about a root once found, so this discovers the same
    directory through a site template, which is role-independent, and then
    labels it two different ways.
    """
    text, paths = build_world(tmp_path)
    locked = paths["project"] / "traverse-only"
    locked.mkdir()
    locked.chmod(0o111)
    try:
        identity = make_identity(gids=[os.getgid() + 99999])
        mounts = read_mount_table(text=text)

        as_project = site_with_roles(paths)
        as_project.templates = [str(locked)]
        as_archive = site_with_roles(paths)
        as_archive.templates = [str(locked)]
        as_archive.role_globs = [(str(paths["project"]) + "*", "archive")]

        first = find(run(mounts, identity, None, as_project), locked)
        second = find(run(mounts, identity, None, as_archive), locked)
        assert first is not None and second is not None
        assert (first.role, second.role) == ("project", "archive")
        assert first.reach == second.reach == Reach.TRAVERSE
        assert first.present.confirmed is second.present.confirmed is True
    finally:
        locked.chmod(0o755)


def test_an_exhausted_budget_produces_not_probed_rather_than_dropping_a_root(tmp_path):
    """A root that vanished because time ran out is indistinguishable from a
    root that does not exist, and those are very different answers."""
    text, paths = build_world(tmp_path)
    spent = Budget(0.0)
    assert spent.exhausted is True

    roots = run(read_mount_table(text=text), make_identity(), spent, site_with_roles(paths))
    assert roots, "the roots must still be reported"
    for root in roots:
        assert root.present.category == VerdictCategory.NOT_PROBED
        assert root.present.value is None
        assert root.reach == Reach.UNKNOWN
        assert root.reach_reason
        assert root.writable.category == VerdictCategory.NOT_PROBED
        # Still a truthful statement about the mount table, which cost nothing.
        assert root.mounted.confirmed is True


def test_a_root_that_is_not_mounted_here_is_distinguished_from_one_that_is_missing(tmp_path):
    """Two different answers: no filesystem, versus no directory on one."""
    text, paths = build_world(tmp_path)
    site = site_with_roles(paths)
    site.templates = ["/cfs3/nothing-here", str(paths["project"]) + "/absent"]

    roots = run(
        read_mount_table(text=text),
        make_identity(),
        None,
        site,
        filesets=[("a", "/cfs3/nothing-here"), ("b", str(paths["project"]) + "/absent")],
    )
    unmounted = find(roots, "/cfs3/nothing-here")
    assert unmounted.mounted.refuted is True
    assert unmounted.mounted.category == VerdictCategory.NOT_MOUNTED_HERE

    absent = find(roots, str(paths["project"]) + "/absent")
    assert absent.mounted.confirmed is True, "the filesystem IS mounted"
    assert absent.present.refuted is True


def test_ignored_paths_are_never_probed(tmp_path):
    """A site can name a mount known to hang, and it must not be touched."""
    text, paths = build_world(tmp_path)
    site = site_with_roles(paths)
    site.ignore = [str(paths["scratch"]) + "*"]
    roots = run(read_mount_table(text=text), make_identity(), None, site)
    assert find(roots, paths["scratch"]) is None
    assert find(roots, paths["home"]) is not None


def test_low_value_roots_are_ranked_down_and_not_dropped(tmp_path):
    """Noise for a user asking where their data can go, but not wrong.

    Marked so a default view can hide them and ``--all`` can show them. Two
    kinds, both measured on this node: a memory filesystem (``/`` here is a
    tmpfs, because the node is diskless, and ``/.nodelog/log`` is a Slurm log
    tmpfs), and a filesystem's own root mounted where the same filesystem is
    also available under a better name (the six ``/gpfs/<cluster>/<tier>``
    mountpoints, whose fileset reads ``root``).
    """
    text, paths = build_world(tmp_path)
    plumbing = tmp_path / "gpfs" / "one" / "cap"
    plumbing.mkdir(parents=True)
    ram = tmp_path / "ram"
    ram.mkdir()
    extra = "cap %s gpfs rw 0 0\ntmpfs %s tmpfs rw 0 0\n" % (plumbing, ram)

    # The role is declared, not inherited from where tmp_path happens to live.
    # On this cluster TMPDIR sits under /project, so the built-in heuristic
    # would read "project" from the ambient path and the test would pass or fail
    # depending on the machine: RD-10 again.
    site = site_with_roles(paths)
    site.role_globs.append((str(plumbing), "other"))

    roots = run(read_mount_table(text=text + extra), make_identity(), None, site)

    ranked_down = find(roots, plumbing)
    assert ranked_down is not None, "it must be kept, only ranked down"
    assert ranked_down.policy["rank"] == "secondary"
    assert "filesystem root" in ranked_down.policy["rank_reason"]

    memory = find(roots, ram)
    assert memory.policy["rank"] == "secondary"
    assert "memory filesystem" in memory.policy["rank_reason"]

    # The real roots stay primary, including the four siblings on one device.
    for key in ("home", "project", "software"):
        assert find(roots, paths[key]).policy["rank"] == "primary"


def test_a_shallower_mountpoint_of_the_same_device_stays_primary(tmp_path):
    """``/collie3`` is depth 1 and real; ``/gpfs/collie3/cap`` is depth 3 and
    plumbing. The rule is depth plus role, so the useful name survives."""
    text, paths = build_world(tmp_path)
    shallow = tmp_path / "collie3"
    shallow.mkdir()
    deep = tmp_path / "gpfs" / "collie3" / "cap"
    deep.mkdir(parents=True)
    extra = "b3 %s gpfs rw 0 0\nb3 %s gpfs rw 0 0\n" % (shallow, deep)

    site = site_with_roles(paths)
    site.role_globs.append((str(shallow), "other"))
    site.role_globs.append((str(deep), "other"))

    roots = run(read_mount_table(text=text + extra), make_identity(), None, site)
    assert find(roots, shallow).policy["rank"] == "primary"
    assert find(roots, deep).policy["rank"] == "secondary"


def test_role_for_path_delegates_to_the_site():
    site = Site()
    site.role_globs = [("/weird/*", "project")]
    assert role_for_path("/weird/thing", "gpfs", site) == "project"
    # With no site at all the built-in heuristics still answer.
    assert role_for_path("/tmp", "tmpfs") == "local"


def test_discovery_never_walks_a_tree(tmp_path, monkeypatch):
    """The O(roots) promise, enforced by counting.

    A one-level scandir per project root is the most this package may do. A
    scandir of a directory INSIDE a project root would mean it had started
    walking, and on a real /project that is 669 subtrees.
    """
    text, paths = build_world(tmp_path)
    deep = paths["project"] / "mine" / "deeper" / "deepest"
    deep.mkdir(parents=True)

    scanned = []
    real_scandir = os.scandir

    def counting_scandir(path="."):
        scanned.append(str(path))
        return real_scandir(path)

    monkeypatch.setattr(candidates_module.os, "scandir", counting_scandir)
    run(
        read_mount_table(text=text),
        make_identity(),
        None,
        site_with_roles(paths),
        env={"HOME": str(paths["home_me"])},
    )

    assert str(paths["project"] / "mine") not in scanned, "scanned a subdirectory: that is a walk"
    assert str(deep) not in scanned
    # One level of the project root and of $HOME is the whole allowance.
    assert sorted(set(scanned)) == sorted({str(paths["project"]), str(paths["home_me"])})


def test_discovery_runs_no_external_commands(tmp_path):
    """NoRunner raises on any call, so reaching the end proves it.

    Discovery has to stay cheap enough to run unconditionally: the moment it
    shells out, it needs a flag, and a flag means nobody runs it.
    """
    text, paths = build_world(tmp_path)
    roots = run(read_mount_table(text=text), make_identity(), None, site_with_roles(paths))
    assert roots


def test_a_root_json_round_trips(tmp_path):
    text, paths = build_world(tmp_path)
    roots = run(read_mount_table(text=text), make_identity(), None, site_with_roles(paths))
    payload = roots[0].to_json()
    assert payload["path"] == roots[0].path
    assert payload["mounted"]["known"] is True
    assert "identity" in payload


# --------------------------------------------------------------------------
# The human form of a source
# --------------------------------------------------------------------------


def test_every_source_has_a_human_label():
    """Exhaustive, the way `CATEGORY_LABELS` is, and for the same reason.

    These tokens are a wire vocabulary: they go into `--json` and into the
    state file. A token with no label is a token that reaches a user's screen,
    which is what `dirscape why` did when it printed "found by group-template,
    dir-owner, quota-fileset" at a reader. A new source with no sentence must
    be a test failure here, not a discovery in a bug report.
    """
    tokens = [
        value
        for name, value in vars(candidates_module).items()
        if name.startswith("SOURCE_") and isinstance(value, str)
    ]
    assert tokens, "no source constants found, so this test is not testing anything"
    for token in tokens:
        assert token in candidates_module.SOURCE_LABELS, "%r has no human label" % (token,)
        label = candidates_module.source_label(token)
        assert label and label != token
        # The label completes "dirscape shows you this directory because ...",
        # so it is a clause: lower case, no trailing stop.
        assert label[0].islower(), "%r does not read as a clause" % (label,)
        assert not label.endswith("."), "%r carries its own full stop" % (label,)


def test_a_source_with_no_label_falls_back_instead_of_raising():
    """Lossy on purpose. An unlabelled source is a bug the test above catches,
    and it should not take down a user's terminal in the meantime.
    """
    assert candidates_module.source_label("brand-new-source") == "brand new source"
    assert candidates_module.source_label("") == ""


def test_a_note_that_only_restates_its_source_is_recognised():
    """So a view can print the label or the note and never both.

    `/project/hpc` carried "matched group hpc", "directory hpc is group-owned
    by a group you are in" and "holds the fileset project-hpc" under the three
    labels that say exactly that.
    """
    sources = [SOURCE_GROUP_TEMPLATE, SOURCE_DIR_OWNER, SOURCE_QUOTA_FILESET]
    for note in (
        "matched group hpc",
        "directory hpc is group-owned by a group you are in",
        "holds the fileset project-hpc",
    ):
        assert candidates_module.restates_source(note, sources)
    # A fact no label carries survives, and a prefix only counts for a source
    # the root actually has.
    assert not candidates_module.restates_source("also reachable at /gpfs/x", sources)
    assert not candidates_module.restates_source("matched group hpc", [SOURCE_MOUNTS])
