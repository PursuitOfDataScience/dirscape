"""The Lustre backend, against transcripts captured live on ACME Procyon.

Every string below is verbatim `lfs` output from a Procyon login node
(SLES 15, Lustre client, 2026-09-22), including the wrapped form `lfs` uses
when a path is too long for its first column.
"""

from dirscape.discover.mounts import read_mount_table
from dirscape.quota import filesets_seen
from dirscape.quota.lustre import LustreBackend, parse_lfs_quota
from dirscape.runner import Budget, RecordedRunner

USER_HOME = """\
Disk quotas for usr jdoe42 (uid 31415):
     Filesystem  kbytes   quota   limit   grace   files   quota   limit   grace
          /home 37414052  358847931 391470470       -   32959       0       0       -
uid 31415 is using default file quota setting
"""

GROUP_HOME = """\
Disk quotas for grp users (gid 100):
     Filesystem  kbytes   quota   limit   grace   files   quota   limit   grace
          /home 78176338828       0       0       - 369329513       0       0       -
gid 100 is using default block quota setting
gid 100 is using default file quota setting
"""

USER_GROVE = """\
Disk quotas for usr jdoe42 (uid 31415):
     Filesystem  kbytes   quota   limit   grace   files   quota   limit   grace
     /lus/grove 4375680528       0       0       -  216168       0       0       -
uid 31415 is using default block quota setting
uid 31415 is using default file quota setting
"""

PROJECT = """\
Disk quotas for prj 13579 (pid 13579):
     Filesystem  kbytes   quota   limit   grace   files   quota   limit   grace
/lus/egret/projects/lanternlab-exampleu
                31725411384  53687091200 59055800320       - 8214572       0       0       -
pid 13579 is using default file quota setting
"""


def test_a_user_figure_names_no_fileset():
    """`lfs quota -u` covers the whole FILESYSTEM, so it has no fileset.

    It used to be labelled with the basename of the path asked about, which
    invented `home`, `grove`, `acorn` and `egret` as quota scopes. Those
    names then fed discovery and the stranded check, and on Sylvia four of
    them were reported as "held with no reachable path" while all four were
    mounted and listable.
    """
    rows = parse_lfs_quota(USER_HOME, "user")
    assert [row.kind for row in rows] == ["blocks", "files"]
    blocks = rows[0]
    assert blocks.fileset == ""
    assert blocks.scope == "user"
    assert blocks.used == 37414052 * 1024
    assert blocks.soft == 358847931 * 1024
    assert blocks.hard == 391470470 * 1024
    # `lfs` prints the path it was asked about, which is also the mount here.
    assert blocks.mount == "/home"
    assert rows[1].used == 32959
    assert filesets_seen(type("S", (), {"rows": rows})()) == []


def test_a_group_figure_names_no_fileset_either():
    rows = parse_lfs_quota(GROUP_HOME, "group")
    assert rows and all(row.fileset == "" and row.scope == "group" for row in rows)


def test_a_project_figure_is_named_by_its_id():
    """The same string `lfs project -d` gives the directory, so the two meet.

    It was filed under the directory's basename, `lanternlab-exampleu`, while
    the root carried `13579`, and the reader's only project was reported as
    held with no reachable path.
    """
    rows = parse_lfs_quota(PROJECT, "project")
    assert [row.fileset for row in rows] == ["13579", "13579"]
    assert rows[0].scope == "project"
    assert rows[0].mount == "/lus/egret/projects/lanternlab-exampleu"
    assert rows[0].used == 31725411384 * 1024
    assert rows[0].hard == 59055800320 * 1024
    assert rows[1].used == 8214572


def test_the_backend_reads_every_scope_the_site_answers(tmp_path, cmd):
    """User, group and project for one project directory, as the re-ask does."""
    project = tmp_path / "lus" / "egret" / "projects" / "lanternlab-exampleu"
    project.mkdir(parents=True)
    path = str(project)
    mounts = read_mount_table(
        text="192.0.2.102@o2ib22:/egret %s lustre rw,flock 0 0\n" % (tmp_path / "lus" / "egret",)
    )
    runner = RecordedRunner(
        [
            cmd(["/usr/bin/lfs", "quota", "-u", "me", path], stdout=USER_GROVE),
            cmd(["/usr/bin/lfs", "quota", "-g", "users", path], stdout=GROUP_HOME),
            cmd(["/usr/bin/lfs", "project", "-d", path], stdout="13579 P %s\n" % (path,)),
            cmd(["/usr/bin/lfs", "quota", "-p", "13579", path], stdout=PROJECT),
        ],
        probes={"lfs": "/usr/bin/lfs"},
    )
    snap = LustreBackend(user="me", group="users").read(runner, mounts, Budget(10.0), [path])
    scopes = {(row.scope, row.fileset) for row in snap.rows if row.kind == "blocks"}
    assert scopes == {("user", ""), ("group", ""), ("project", "13579")}
