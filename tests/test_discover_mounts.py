"""The mount table: escapes, aliases, classification and node class.

Every table in here is inline text. Nothing reads ``/proc/self/mounts`` and
nothing reads the hostname of the machine running the suite, which is rapiDU's
RD-10: a test that pinned the development cluster's identity failed on every
other host. The device names below are invented for the test.
"""

from dirscape.discover.mounts import (
    KIND_LOCAL,
    KIND_NETWORK,
    KIND_OTHER,
    KIND_PSEUDO,
    MountTable,
    classify_fstype,
    node_class,
    read_mount_table,
    unescape_field,
)

# A two-cluster table in the shape /proc/self/mounts really produces: one device
# mounted at several places, the canonical path and the aliases side by side.
TWO_CLUSTER_TABLE = """\
sysfs /sys sysfs rw,relatime 0 0
proc /proc proc rw,relatime 0 0
cap_one /gpfs/one/cap gpfs rw,relatime 0 0
perf_one /gpfs/one/perf gpfs rw,relatime 0 0
cap_one /home gpfs rw,relatime 0 0
cap_one /project gpfs rw,relatime 0 0
cap_one /software gpfs rw,relatime 0 0
perf_one /scratch/one gpfs rw,relatime 0 0
cap_two /gpfs/two/cap gpfs rw,relatime 0 0
cap_two /project2 gpfs rw,relatime 0 0
/dev/sda1 /tmp xfs rw,relatime,noquota 0 0
/dev/sda1 /scratch/local xfs rw,relatime,noquota 0 0
tmpfs /dev/shm tmpfs rw,nosuid,nodev 0 0
tmpfs /sys/fs/cgroup tmpfs ro,nosuid,nodev,noexec,mode=755 0 0
"""


# --------------------------------------------------------------------------
# The four kernel escapes
# --------------------------------------------------------------------------


def test_all_four_kernel_octal_escapes_are_decoded():
    """Space, tab, newline and backslash, which are the four the kernel emits.

    A mountpoint of ``/data/my share`` arrives as ``/data/my\\040share``, and
    a tool that does not decode it fails every path comparison against that
    mount while looking like it parsed the line fine.
    """
    assert unescape_field(r"/data/my\040share") == "/data/my share"
    assert unescape_field(r"/data/tab\011here") == "/data/tab\there"
    assert unescape_field(r"/data/line\012break") == "/data/line\nbreak"
    assert unescape_field(r"/data/back\134slash") == "/data/back\\slash"


def test_backslash_escape_is_not_double_decoded():
    """The ordering trap: ``\\134040`` is a backslash then "040", not a space.

    A path that really contains ``\\040`` has its backslash escaped by the
    kernel, so a decoder that rescans its own output turns the literal text
    ``\\040`` into a space and silently renames the mount.
    """
    assert unescape_field(r"/data/x\134040y") == "/data/x\\040y"


def test_escapes_are_decoded_in_every_field():
    table = read_mount_table(text="dev\\040one /mnt/with\\040space ext4 rw,x\\011y 0 0\n")
    mount = table.mounts[0]
    assert mount.device == "dev one"
    assert mount.mountpoint == "/mnt/with space"
    assert mount.options == "rw,x\ty"


def test_a_mountpoint_with_a_space_is_still_matched():
    """The reason the decoding matters at all."""
    table = read_mount_table(text="cap /mnt/my\\040share gpfs rw 0 0\n")
    found = table.enclosing_mount("/mnt/my share/subdir")
    assert found is not None
    assert found.mountpoint == "/mnt/my share"


# --------------------------------------------------------------------------
# The aliases df cannot see
# --------------------------------------------------------------------------


def test_df_invisible_aliases_are_all_parsed():
    """``df -hT`` shows only the canonical /gpfs paths and hides the rest.

    Measured on this cluster: ``df -hT`` prints six gpfs rows, every one of
    them ``/gpfs/<cluster>/<pool>``, while ``/proc/self/mounts`` has fifteen.
    The nine it hides are the ones users type. This pins that the parser keeps
    all of them, including several mountpoints sharing one device.
    """
    table = read_mount_table(text=TWO_CLUSTER_TABLE)
    points = [m.mountpoint for m in table.mounts if m.fstype == "gpfs"]
    for hidden in ("/home", "/project", "/software", "/scratch/one", "/project2"):
        assert hidden in points
    # One device, four mountpoints, all four kept.
    assert [m.mountpoint for m in table.mounts if m.device == "cap_one"] == [
        "/gpfs/one/cap",
        "/home",
        "/project",
        "/software",
    ]


def test_devices_of_type_deduplicates_the_aliases():
    """Four mountpoints of one device are one device.

    The fingerprint is built from this list, so a repeat would make the key
    depend on how many aliases a particular node happened to mount.
    """
    table = read_mount_table(text=TWO_CLUSTER_TABLE)
    assert table.devices_of_type(["gpfs"]) == ["cap_one", "cap_two", "perf_one"]
    assert table.network_devices() == ["cap_one", "cap_two", "perf_one"]


def test_devices_of_type_accepts_a_bare_string():
    """A string would otherwise iterate as characters and match nothing."""
    table = read_mount_table(text=TWO_CLUSTER_TABLE)
    assert table.devices_of_type("gpfs") == table.devices_of_type(["gpfs"])


# --------------------------------------------------------------------------
# enclosing_mount
# --------------------------------------------------------------------------


def test_enclosing_mount_longest_match_wins():
    """Mountpoints nest, so the first prefix hit is the wrong answer."""
    table = read_mount_table(text=TWO_CLUSTER_TABLE + "root / ext4 rw 0 0\n")
    assert table.enclosing_mount("/scratch/local/jdoe42").mountpoint == "/scratch/local"
    assert table.enclosing_mount("/project/hpc/data").mountpoint == "/project"
    assert table.enclosing_mount("/elsewhere/thing").mountpoint == "/"


def test_enclosing_mount_is_none_when_nothing_encloses():
    """The NOT_MOUNTED_HERE signal, and it must not fall back to anything."""
    table = read_mount_table(text=TWO_CLUSTER_TABLE)
    assert table.enclosing_mount("/cfs3/dataset") is None


def test_enclosing_mount_does_not_match_a_sibling_by_prefix():
    table = read_mount_table(text="cap /project gpfs rw 0 0\n")
    assert table.enclosing_mount("/project2/hpc") is None


def test_later_line_wins_an_overmount_tie():
    """Two mounts on one mountpoint: the second is the one you reach."""
    table = read_mount_table(text="first /mnt ext4 rw 0 0\nsecond /mnt xfs rw 0 0\n")
    assert table.enclosing_mount("/mnt/file").device == "second"


# --------------------------------------------------------------------------
# Classification
# --------------------------------------------------------------------------


def test_network_local_and_pseudo_are_separated():
    assert classify_fstype("gpfs") == KIND_NETWORK
    assert classify_fstype("lustre") == KIND_NETWORK
    assert classify_fstype("nfs4") == KIND_NETWORK
    assert classify_fstype("beegfs") == KIND_NETWORK
    assert classify_fstype("ceph") == KIND_NETWORK
    assert classify_fstype("panfs") == KIND_NETWORK
    assert classify_fstype("xfs") == KIND_LOCAL
    assert classify_fstype("ext4") == KIND_LOCAL
    assert classify_fstype("btrfs") == KIND_LOCAL
    assert classify_fstype("overlay") == KIND_LOCAL
    assert classify_fstype("proc") == KIND_PSEUDO
    assert classify_fstype("sysfs") == KIND_PSEUDO
    assert classify_fstype("cgroup") == KIND_PSEUDO
    assert classify_fstype("devtmpfs") == KIND_PSEUDO


def test_pseudo_mounts_are_skippable():
    table = read_mount_table(text=TWO_CLUSTER_TABLE)
    points = [m.mountpoint for m in table.non_pseudo()]
    assert "/sys" not in points
    assert "/proc" not in points
    assert "/home" in points


def test_a_tmpfs_under_sys_is_pseudo_by_location():
    """Measured: ``/sys/fs/cgroup`` is a tmpfs, and ``/dev/shm`` is too.

    Classifying on fstype alone keeps eight mounts nobody can have an
    allocation on, because tmpfs is otherwise real local storage.
    """
    table = read_mount_table(text=TWO_CLUSTER_TABLE)
    points = [m.mountpoint for m in table.non_pseudo()]
    assert "/sys/fs/cgroup" not in points
    assert "/dev/shm" not in points
    # A tmpfs somewhere real stays.
    real = read_mount_table(text="tmpfs /scratch/ram tmpfs rw 0 0\n")
    assert [m.mountpoint for m in real.non_pseudo()] == ["/scratch/ram"]


def test_an_unknown_filesystem_type_is_kept_not_dropped():
    """The asymmetric failure. Dropping it would hide real storage.

    A filesystem this package has never heard of is far more likely to be
    storage at somebody else's site than to be kernel plumbing, so it is kept
    and flagged instead of filtered out.
    """
    table = read_mount_table(text="wekafs /mnt/weka wekafs rw 0 0\nmystery /mnt/x zzfs rw 0 0\n")
    kept = [m.mountpoint for m in table.non_pseudo()]
    assert "/mnt/weka" in kept, "a known network fs must be kept"
    assert "/mnt/x" in kept, "an UNKNOWN fs must still be kept"
    unknown_mount = table.at("/mnt/x")[0]
    assert unknown_mount.kind == KIND_OTHER
    assert unknown_mount.unclassified is True


def test_mount_options_are_readable_as_a_set():
    table = read_mount_table(text=TWO_CLUSTER_TABLE)
    tmp = table.at("/tmp")[0]
    assert tmp.has_option("noquota") is True
    assert tmp.has_option("prjquota") is False
    assert tmp.read_only is False
    cgroup = table.at("/sys/fs/cgroup")[0]
    assert cgroup.read_only is True
    # A key=value option must not be mistaken for a bare flag.
    assert cgroup.has_option("mode") is False


# --------------------------------------------------------------------------
# Reading the table
# --------------------------------------------------------------------------


def test_read_mount_table_reads_a_real_file(tmp_path):
    path = tmp_path / "mounts"
    path.write_text(TWO_CLUSTER_TABLE)
    table = read_mount_table(str(path))
    assert len(table) == len(TWO_CLUSTER_TABLE.strip().splitlines())
    assert table.source == str(path)


def test_an_unreadable_table_is_empty_rather_than_an_exception(tmp_path):
    """A diagnostic tool has to start even where /proc is not what it expects."""
    table = read_mount_table(str(tmp_path / "does-not-exist"))
    assert len(table) == 0
    assert table.enclosing_mount("/anything") is None


def test_short_and_blank_lines_are_skipped():
    table = read_mount_table(text="\n\ngarbage\ncap /home gpfs rw 0 0\n")
    assert [m.mountpoint for m in table.mounts] == ["/home"]


def test_a_line_with_no_option_field_still_parses():
    table = read_mount_table(text="cap /home gpfs\n")
    assert table.mounts[0].options == ""
    assert table.mounts[0].fstype == "gpfs"


# --------------------------------------------------------------------------
# node_class
# --------------------------------------------------------------------------


def test_slurm_env_means_compute():
    """The strong signal, and it beats the hostname.

    Checked with a login-shaped hostname on purpose: inside a job step the
    Slurm variables are the evidence and the name is not.
    """
    assert node_class({"SLURM_JOB_ID": "12345"}, "anything-login1") == "compute"
    assert node_class({"SLURM_NODEID": "0"}, "whatever") == "compute"


def test_an_empty_slurm_variable_is_not_evidence():
    """An exported-but-empty variable is what a login shell often has."""
    assert node_class({"SLURM_JOB_ID": ""}, "cluster-login2") == "login"


def test_hostname_fallback_recognises_login_and_compute():
    assert node_class({}, "cluster-login1.example.org") == "login"
    assert node_class({}, "cluster-0200.example.org") == "compute"
    assert node_class({}, "cluster-bigmem1") == "compute"
    assert node_class({}, "cluster-gpu4") == "compute"


def test_an_unrecognised_hostname_is_unknown_not_a_guess():
    """Wrong is worse than unknown here.

    Calling a login node "compute" would make a login-only mount look like it
    had disappeared from the filesystem rather than from this node.
    """
    assert node_class({}, "my-laptop") == "unknown"
    assert node_class({}, "") == "unknown"
    assert node_class({}, "storage.example.org") == "unknown"


def test_mount_table_json_round_trips():
    table = read_mount_table(text=TWO_CLUSTER_TABLE)
    payload = table.to_json()
    assert len(payload["mounts"]) == len(table)
    assert payload["mounts"][2]["kind"] == KIND_NETWORK


def test_an_empty_mount_table_is_usable():
    table = MountTable()
    assert len(table) == 0
    assert table.non_pseudo() == []
    assert table.network_devices() == []
