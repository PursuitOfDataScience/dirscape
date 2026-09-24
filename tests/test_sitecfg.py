"""Site configuration: the layer that has to make this tool work on a cluster
the author has never logged into.

The tests that matter most here are the ones proving the tool is USEFUL WITH NO
CONFIG AT ALL, because that is the state every new site starts in.
"""

import pytest

from dirscape.sitecfg import (
    ROLES,
    SITE_TEMPLATE,
    Site,
    config_search_path,
    guess_cluster_name,
    load_site,
)

# --------------------------------------------------------------------------
# Zero config is a supported state
# --------------------------------------------------------------------------


def test_an_empty_site_still_classifies_paths():
    """With no config the tool must still label the obvious cases.

    A site administrator should never be required before a user gets value,
    because on most clusters nobody will ever write the config file.
    """
    site = load_site(paths=[])
    assert site.role_for("/home/alice") == "home"
    assert site.role_for("/scratch/cluster/alice") == "scratch"
    assert site.role_for("/software") == "software"
    assert site.role_for("/project/lab") == "project"
    assert site.role_for("/project2/reference") == "dataset"
    assert site.role_for("/tmp", fstype="xfs") == "local"


def test_an_unrecognised_path_is_other_rather_than_a_guess():
    """A path the heuristics do not recognise must be labelled `other`.

    Not guessed into the nearest plausible role: a wrong label is worse than
    an honest absence of one, and the role column is advisory anyway.
    """
    site = load_site(paths=[])
    assert site.role_for("/zfsvol/experiment-4") == "other"
    assert site.role_for("/mnt/vendorbox") == "other"


def test_opt_is_anchored_not_globbed():
    """`/opt` is a software convention; `<project>/opt` is somebody's data."""
    site = load_site(paths=[])
    assert site.role_for("/opt/intel") == "software"
    assert site.role_for("/project/lab/opt/inputs") == "project"


def test_every_role_the_heuristics_emit_is_in_the_declared_vocabulary():
    """A role outside the vocabulary would reach the renderer as a surprise."""
    site = load_site(paths=[])
    probes = [
        "/home/a",
        "/scratch/x",
        "/software",
        "/project/p",
        "/project2/reference",
        "/cfs3/set",
        "/tmp",
        "/nowhere",
    ]
    for path in probes:
        assert site.role_for(path) in ROLES


def test_empty_site_has_no_site_specific_knowledge():
    """The core must not ship one cluster's paths.

    This is the test that keeps the tool cluster-agnostic: if somebody
    hardcodes a site path into the defaults, this fails.
    """
    site = load_site(paths=[])
    blob = repr(site.to_json()).lower()
    for site_specific in ("meadow", "collie", "hpc", "cfs3", "exampleu", "slurm"):
        assert site_specific not in blob, "core config leaked a site name: %s" % site_specific
    assert site.templates == []
    assert site.dataset_roots == []
    assert site.fileset_prefixes == []


def test_quota_expected_is_false_only_for_filesystems_that_have_none():
    site = load_site(paths=[])
    assert site.quota_expected("gpfs") is True
    assert site.quota_expected("lustre") is True
    assert site.quota_expected("nfs4") is True
    assert site.quota_expected("tmpfs") is False


# --------------------------------------------------------------------------
# Parsing
# --------------------------------------------------------------------------


def _write(tmp_path, name, text):
    path = tmp_path / name
    path.write_text(text)
    return str(path)


def test_lists_accept_commas_and_newlines_and_inline_comments(tmp_path):
    """Both separators, because a sysadmin writing paths reaches for one per
    line and a sysadmin writing prefixes reaches for commas. Accepting only
    one produces a config that silently does nothing.
    """
    path = _write(
        tmp_path,
        "site.conf",
        "[roots]\n"
        "templates =\n"
        "  /project/{group}\n"
        "  # a comment inside a multi-line value\n"
        "  /scratch/{cluster}/{user}\n"
        "[filesets]\n"
        "prefixes = project-, project2-, collie3-\n",
    )
    site = load_site(paths=[path])
    assert site.templates == ["/project/{group}", "/scratch/{cluster}/{user}"]
    assert site.fileset_prefixes == ["project-", "project2-", "collie3-"]


def test_the_shipped_template_parses(tmp_path):
    """`dirscape --site-template` must emit something that actually loads.

    A template that does not parse is worse than none: it teaches the wrong
    syntax to the one person willing to write the file.
    """
    path = _write(tmp_path, "site.conf", SITE_TEMPLATE)
    warnings = []
    site = load_site(paths=[path], warn=warnings)
    assert warnings == []
    assert path in site.sources


def test_role_globs_override_the_builtin_heuristics(tmp_path):
    """A site that calls its scratch something odd must win."""
    path = _write(tmp_path, "site.conf", "[roles]\n/flash/* = scratch\n/home/* = project\n")
    site = load_site(paths=[path])
    assert site.role_for("/flash/alice") == "scratch"
    assert site.role_for("/home/alice") == "project"


def test_policy_merges_least_specific_first(tmp_path):
    """So a site can set a filesystem-wide default and override one subtree
    without restating the rest.
    """
    path = _write(
        tmp_path,
        "site.conf",
        "[policy]\n"
        "/scratch/* = backup=no; speed=fast; purge_days=30\n"
        "/scratch/special/* = purge_days=90\n",
    )
    site = load_site(paths=[path])
    merged = site.policy_for("/scratch/special/data")
    assert merged["purge_days"] == 90
    assert merged["backup"] is False
    assert merged["speed"] == "fast"


def test_a_malformed_policy_field_costs_one_field_not_the_row(tmp_path):
    path = _write(tmp_path, "site.conf", "[policy]\n/x/* = purge_days=soon; backup=yes\n")
    site = load_site(paths=[path])
    policy = site.policy_for("/x/y")
    assert "purge_days" not in policy
    assert policy["backup"] is True


def test_a_malformed_config_warns_rather_than_raising(tmp_path):
    """A diagnostic tool must not be bricked by a stray character in /etc.

    Refusing to start would make a config file a single point of failure for
    the tool you reach for when something is already wrong.
    """
    path = _write(tmp_path, "site.conf", "[roots\nthis is not ini at all ][")
    warnings = []
    site = load_site(paths=[path], warn=warnings)
    assert len(warnings) == 1
    assert "malformed" in warnings[0]
    assert site.role_for("/home/a") == "home"


def test_a_missing_config_file_is_silent(tmp_path):
    warnings = []
    load_site(paths=[str(tmp_path / "absent.conf")], warn=warnings)
    assert warnings == []


def test_json_config_is_accepted(tmp_path):
    path = _write(
        tmp_path,
        "config.json",
        '{"name": "testsite", "templates": ["/p/{group}"], "roles": {"/p/*": "project"}}',
    )
    site = load_site(paths=[path])
    assert site.name == "testsite"
    assert site.role_for("/p/lab") == "project"


def test_later_files_layer_on_earlier_ones(tmp_path):
    """Site-wide config first, then the user's, which is the documented order."""
    etc = _write(tmp_path, "site.conf", "[filesets]\nprefixes = project-\n")
    user = _write(tmp_path, "config.conf", "[filesets]\nprefixes = collie3-\n")
    site = load_site(paths=[etc, user])
    assert site.fileset_prefixes == ["project-", "collie3-"]


def test_a_site_declaring_group_prefixes_replaces_the_default(tmp_path):
    """Replace, not extend.

    A site listing its own conventions is making a statement, and silently
    keeping our `pi-` default would generate candidate paths its administrator
    did not ask for.
    """
    path = _write(tmp_path, "site.conf", "[filesets]\ngroup_prefixes = grp_, lab-\n")
    site = load_site(paths=[path])
    assert site.group_prefixes == ["grp_", "lab-"]
    assert "pi-" not in site.group_prefixes


def test_config_env_override_wins_outright(tmp_path, monkeypatch):
    monkeypatch.setenv("DIRSCAPE_CONFIG", "/some/pinned/file.conf")
    assert config_search_path() == ["/some/pinned/file.conf"]


def test_search_path_prefers_user_config_last(monkeypatch):
    monkeypatch.delenv("DIRSCAPE_CONFIG", raising=False)
    monkeypatch.setenv("XDG_CONFIG_HOME", "/xdg")
    paths = config_search_path()
    assert paths[0].startswith("/etc/")
    assert paths[-1].startswith("/xdg/")


# --------------------------------------------------------------------------
# Name transforms
# --------------------------------------------------------------------------


def test_group_aliases_work_in_both_directions():
    """Membership of `pi-smith` can grant access to a directory called
    `smith`, and a fileset called `project-smith` can correspond to a group
    called either. No single transform connects them, so both are candidates
    and only os.access decides.
    """
    site = load_site(paths=[])
    assert site.group_aliases("smith") == ["smith", "pi-smith"]
    assert site.group_aliases("pi-smith") == ["pi-smith", "smith"]


def test_group_aliases_keep_the_original_first():
    """Callers probe in order, and the unmodified name is the likeliest hit."""
    assert load_site(paths=[]).group_aliases("lab")[0] == "lab"


def test_strip_fileset_prefix_is_a_no_op_without_a_match():
    site = Site()
    site.fileset_prefixes = ["project-"]
    assert site.strip_fileset_prefix("project-lab") == "lab"
    assert site.strip_fileset_prefix("home") == "home"


def test_expand_templates_covers_every_group_and_alias():
    site = Site()
    site.templates = ["/project/{group}", "/scratch/{cluster}/{user}"]
    paths = site.expand_templates("alice", ["lab", "pi-other"], cluster="c1")
    assert "/project/lab" in paths
    assert "/project/pi-lab" in paths
    assert "/project/pi-other" in paths
    assert "/project/other" in paths
    assert "/scratch/c1/alice" in paths


def test_expand_templates_deduplicates():
    site = Site()
    site.templates = ["/project/{group}", "/project/{group}/"]
    assert len(site.expand_templates("a", ["lab"])) == 2  # lab and pi-lab, once each


def test_ignore_patterns_are_globs(tmp_path):
    path = _write(tmp_path, "site.conf", "[site]\nignore = /net/*, /mnt/hangs\n")
    site = load_site(paths=[path])
    assert site.is_ignored("/net/anything") is True
    assert site.is_ignored("/mnt/hangs") is True
    assert site.is_ignored("/project/fine") is False


# --------------------------------------------------------------------------
# Cluster naming
# --------------------------------------------------------------------------


def test_cluster_name_prefers_device_names_over_the_hostname():
    """A device name is a property of the storage fabric and reads the same
    from any node. A hostname changes per node and per login round robin.
    """
    name = guess_cluster_name("some-login-07.example.edu", ["mw3_cap", "mw3_perf"])
    assert name == "mw3"


def test_cluster_name_falls_back_to_a_stripped_hostname():
    assert guess_cluster_name("cluster-0200.local", []) == "cluster"
    assert guess_cluster_name("frontend.example.edu", []) == "frontend"


def test_cluster_name_ignores_a_two_character_coincidence():
    """Two shared characters is a coincidence of alphabetical neighbours, not
    a name, so the hostname is the better answer there.
    """
    assert guess_cluster_name("realname-01", ["abx_cap", "abz_perf"]) == "realname"


@pytest.mark.parametrize("hostname", ["", "x"])
def test_cluster_name_never_raises_on_a_degenerate_hostname(hostname):
    assert isinstance(guess_cluster_name(hostname, []), str)


def test_a_site_can_publish_a_snapshot_tree(tmp_path):
    """`[snapshots] roots` covers the case the filesystem cannot answer for.

    The hidden `.snapshots`, `.snapshot`, `.zfs/snapshot` and `.snap` trees
    inside a filesystem need no configuration. A site that publishes its
    snapshots somewhere else does: `/snapshots` here is a plain top-level
    directory belonging to no device, and nothing in the mount table leads
    to it.
    """
    from dirscape.sitecfg import load_site

    conf = tmp_path / "site.conf"
    conf.write_text("[snapshots]\nroots = /snapshots, /old-snapshots\n")

    site = load_site([str(conf)])

    assert site.snapshot_roots == ["/snapshots", "/old-snapshots"]
    assert site.to_json()["snapshot_roots"] == ["/snapshots", "/old-snapshots"]


def test_the_site_template_documents_the_snapshots_section():
    from dirscape.sitecfg import SITE_TEMPLATE

    assert "[snapshots]" in SITE_TEMPLATE


def test_a_heuristic_extension_is_checked_where_the_built_in_one_is(tmp_path):
    """`[heuristics]` extends one built-in group in place; `[roles]` overrides.

    A site's own word for archive storage sits beside `*archive*`: checked
    after the home and scratch patterns, and case-insensitively. As a `[roles]`
    glob it would be checked first and claim `/vault3/home/x` as archive.
    """
    from dirscape.sitecfg import load_site

    assert load_site(paths=[]).role_for("/vault3/set") == "other"

    conf = tmp_path / "site.conf"
    conf.write_text("[heuristics]\narchive = */vault*, */Keep*\nnonsense = */x*\n")
    site = load_site([str(conf)])

    assert site.role_heuristics == [("archive", "*/vault*"), ("archive", "*/Keep*")]
    assert site.role_for("/vault3/set") == "archive"
    assert site.role_for("/VAULT3/set") == "archive", "case-insensitive, like the built-ins"
    assert site.role_for("/keeper/set") == "archive"
    assert site.role_for("/vault3/home/x") == "home", "home is still checked first"
    assert site.to_json()["role_heuristics"] == [["archive", "*/vault*"], ["archive", "*/Keep*"]]


def test_heuristics_and_decimal_mounts_are_read_from_json(tmp_path):
    from dirscape.sitecfg import load_site

    conf = tmp_path / "config.json"
    conf.write_text(
        '{"heuristics": {"archive": ["*/vault*"]}, "decimal_suffix_mounts": ["/grant"],'
        ' "plugin": {"fileset_prefixes": ["project-"]}}'
    )
    site = load_site([str(conf)])
    assert site.role_for("/vault/x") == "archive"
    assert site.decimal_suffix_mounts == ["/grant"]
    assert site.plugin == {"fileset_prefixes": ["project-"]}


def test_decimal_suffix_mounts_reach_the_wrapper_backend(tmp_path):
    """No site's mount is decimal by default; a site names its own."""
    from dirscape.quota import default_backends, wrapper
    from dirscape.sitecfg import load_site

    def wrapper_of(site):
        backends = default_backends(site)
        (backend,) = [b for b in backends if isinstance(b, wrapper.SiteWrapperBackend)]
        return backend

    assert wrapper.DECIMAL_SUFFIX_MOUNTS == {}
    assert not wrapper_of(load_site(paths=[])).decimal_mounts

    conf = tmp_path / "site.conf"
    conf.write_text("[quota]\ndecimal_suffix_mounts = /grant, /other\n")
    site = load_site([str(conf)])
    assert site.decimal_suffix_mounts == ["/grant", "/other"]
    assert wrapper_of(site).decimal_mounts == {
        "/grant": wrapper.DECIMAL_SUFFIX_NOTE,
        "/other": wrapper.DECIMAL_SUFFIX_NOTE,
    }


def test_the_site_template_documents_every_new_section():
    from dirscape.sitecfg import SITE_TEMPLATE

    for needle in ("[plugin]", "[heuristics]", "decimal_suffix_mounts"):
        assert needle in SITE_TEMPLATE


# --------------------------------------------------------------------------
# A line of config that does nothing says so
# --------------------------------------------------------------------------


def _loaded(tmp_path, text, name="site.conf"):
    warnings = []
    site = load_site(paths=[_write(tmp_path, name, text)], warn=warnings)
    return site, warnings


def _said(warnings, *needles):
    return any(all(needle in line for needle in needles) for line in warnings)


def test_a_misspelt_section_or_key_is_named_with_the_word_it_meant(tmp_path):
    """`[rolez]` was read, thrown away, and never mentioned, in `paths` or `why`."""
    _site, warnings = _loaded(
        tmp_path, "[rolez]\n/x/* = scratch\n[site]\nnmae = acme\n[plugin]\ndescripton = x\n"
    )
    assert _said(warnings, "site.conf: unknown section [rolez]", "(did you mean [roles]?)")
    assert _said(warnings, "unknown key nmae in [site]", "(did you mean name?)")
    assert _said(warnings, "unknown key descripton in [plugin]", "(did you mean description?)")
    assert len(warnings) == 3


def test_a_role_that_is_not_a_role_is_named_rather_than_dropped(tmp_path):
    site, warnings = _loaded(tmp_path, "[roles]\n/flash/* = scrach\n[heuristics]\nscrach = */x*\n")
    assert site.role_globs == [] and site.role_heuristics == []
    assert _said(warnings, "unknown role scrach for /flash/* in [roles]", "did you mean scratch?")
    assert _said(warnings, "unknown role scrach in [heuristics]", "did you mean scratch?")


def test_a_policy_line_that_sets_nothing_is_named(tmp_path):
    """`purge_days=30d` cost the row the one fact the policy column exists for."""
    site, warnings = _loaded(
        tmp_path, "[policy]\nscrach = 60d\n/scratch/* = purge_days=30d; backup=no\n"
    )
    assert site.policy_globs == [("/scratch/*", {"backup": False})]
    assert _said(warnings, "no key=value pair in [policy] scrach = 60d")
    assert _said(warnings, "purge_days=30d is not a whole number of days in [policy] /scratch/*")


def test_the_documented_quota_order_names_take_effect(tmp_path):
    """The template says `order = wrapper, gpfs`, and those names matched nothing."""
    from dirscape.quota import default_backends

    site, warnings = _loaded(tmp_path, "[quota]\norder = wrapper, posix, gpfss, Mmlsquota\n")
    names = [backend.name for backend in default_backends(site)]
    assert names[:3] == ["site quota wrapper", "quota -s", "mmlsquota"]
    assert len(names) == len(set(names)) == 6, "every backend once, none dropped"
    assert warnings == [
        "%s: unknown quota backend gpfss in [quota] order, ignored (did you mean gpfs?)"
        % (tmp_path / "site.conf",)
    ]


def test_every_documented_quota_backend_is_a_backend():
    import re

    from dirscape.quota import default_backends
    from dirscape.sitecfg import QUOTA_BACKENDS

    assert sorted(full for _short, full in QUOTA_BACKENDS) == sorted(
        backend.name for backend in default_backends(None)
    )
    listed = re.search(r"Known backends:(.*?)Leave blank", SITE_TEMPLATE, re.S).group(1)
    names = [word.strip(" #\n.") for word in listed.replace("\n#", " ").split(",")]
    assert names == [short for short, _full in QUOTA_BACKENDS]


def test_the_plugin_keys_are_the_ones_the_plugin_reads_and_the_template_documents():
    """One list in three places, so a key added to the plugin cannot go unchecked."""
    import inspect
    import re

    from dirscape.plugins import site as plugin_site
    from dirscape.sitecfg import PLUGIN_KEYS

    read = re.findall(r'(?:_text|_list|settings\.get)\("([a-z_]+)"', inspect.getsource(plugin_site))
    assert set(read) == set(PLUGIN_KEYS)
    documented = re.findall(r"^#\s+([a-z_]+)\s+=", SITE_TEMPLATE.split("[plugin]", 1)[1], re.M)
    assert documented == list(PLUGIN_KEYS)


def test_a_json_config_names_what_it_ignores(tmp_path):
    text = (
        '{"nmae": "acme", "roles": {"/flash/*": "scrach"}, "quota_order": ["gpfss"],'
        ' "policy": {"/s/*": "purge"}, "plugin": {"descripton": "x"}}'
    )
    site, warnings = _loaded(tmp_path, text, name="config.json")
    assert site.role_globs == [] and site.policy_globs == []
    for needles in (
        ("unknown key nmae", "did you mean name?"),
        ("unknown role scrach for /flash/* in roles", "did you mean scratch?"),
        ("unknown quota backend gpfss in quota_order", "did you mean gpfs?"),
        ("policy for /s/* is not an object",),
        ("unknown key descripton in plugin", "did you mean description?"),
    ):
        assert _said(warnings, *needles), needles
    assert len(warnings) == 5
