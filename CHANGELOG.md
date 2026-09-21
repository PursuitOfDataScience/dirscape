# Changelog

All notable changes to `dirscape` are recorded here, newest first, following
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/).

## [Unreleased]

### Added

- First release. `dirscape` discovers every storage root you can actually reach
  on a cluster and reports what changed since the last run.
- **Four independent axes per root** rather than one access bit: `allocated`,
  `mounted here`, `present`, and a `reach` tri-state of listable / traverse-only
  / closed. A row reading "allocated, not mounted here" is a real and useful
  state: on the development cluster `allocs storage` lists allocations on
  `cfs1`, `cfs2`, `cfs4` and `project3`, and none of those paths exist on the
  node that printed them.
- **`stranded` detection.** A GPFS per-device quota listing enumerates every
  fileset the user holds blocks in, including ones they have no group for.
  Measured on one account: five such filesets holding 11.7 GB, 6.8 GB and three
  smaller. That is storage being charged for and no longer reachable, and no
  other tool reports it.
- **Traverse-only is a first-class reach state.** A directory at mode 2771 that
  you are not in the group for gives `--x`: you can pass through it and cannot
  list it. Measured on 3 of 668 entries under one `/project` and 13 of 894 under
  one `/project2`, so it is not a corner case.
- Views: the atlas table, `new`, `why <path>`, `matrix`, `tree`, `map`,
  `--json` with a reason code on every unknown, and `--ncdu` export so `ncdu`,
  `gdu` and `ncdu-compare` can consume the output.
- `--site-template` prints a commented `/etc/dirscape/site.conf` so a site
  administrator can describe a cluster's layout without patching the package.

### Fixed during integration

These are defects that unit tests on the individual packages could not have
caught, because each one lived in the wiring: a backend was right about a quota
row and the consumer misapplied it. All six were found by running the tool
against a live cluster, and each now has a regression test in `tests/test_cli.py`.

- **Quota figures leaked across sibling directories.** A `project-hpc` row
  whose mount had only been INFERRED as `/project` matched every `/project/*`
  path by prefix, so `/project/abe`, `/project/bard`, `/project/dahlias`,
  `/project/mdgreenwood` and `/project/pelican` each reported holding 11T,
  which is a different PI's usage. Nineteen directories under
  `/project2/reference` all read 928K the same way. Attribution now goes
  through the fileset and never the path prefix, and a root whose fileset
  cannot be established reports `?` rather than inheriting a parent's number.
- **`mmlsattr` did not unmap a row it contradicted.** The contradiction test
  was `path.startswith(row.mount + "/")`, which is False when the path IS the
  mount, so confirming `/home` against a row whose guessed mount was exactly
  `/home` did nothing and a `project-hpc` row kept a `/home` mount the probe
  had just disowned.
- **A fileset map was keyed on the bare fileset name.** `scratch` is a fileset
  name on three of this node's six devices, so the collie3 scratch claimed the
  junction for all of them and `/scratch/meadow3/jdoe42` printed `?` while
  holding 22G. Keyed on `(device, fileset)` now, which is the collision
  `QuotaRow.label` already qualified against.
- **A quota was repeated on every subdirectory of its fileset.** A quota is a
  property of a fileset, so it is reported once, at the point the fileset
  enters the namespace for a fileset-scoped row and at the highest writable
  point for a user-scoped one. Other roots in the fileset get a note saying
  where the figure lives. Choosing the DEEPEST writable point was tried first
  and put an 11T figure on `/project/hpc/jdoe42/.cache/tmp`.
- **Global flags were dropped before the verb.** `dirscape new --json` worked
  and `dirscape --json new` did not: the subcommand's copy of the flag wrote
  its own default over the value the top-level parser had already stored.
  Building fresh action objects per parser is necessary and not sufficient; the
  subcommand copies now default to `argparse.SUPPRESS`.
- **Internal bookkeeping reached a user-facing column.** Every row's POLICY
  cell read `rank=primary`. Discovery's own keys are filtered out of the
  rendered policy.

Also corrected: the header counted mount-table entries rather than storage
devices and reported 28 on a node with 9; allocation rows with no path printed
`?` in every column and were indistinguishable, and now show their location
marked as a location; and the quota backends were being asked twice per run.

### Known limits

- **Nothing can be called new on the first run**, and the tool says so instead
  of labelling everything new. Newness comes from its own snapshot lineage
  because the filesystem cannot supply it: birth time is unavailable on GPFS
  (`stat -c %W` returns 0, `%w` returns `-`, Python's `st_birthtime` is absent),
  and directory `mtime` is a decoy. `/project/aarnold`'s fileset first appears
  in the site quota archive on 2026-03-13 while its directory mtime reads
  2026-05-15, two months late.
- **The Lustre backend has never run live.** No Lustre is mounted on the
  development cluster and `lfs` is not installed, so that backend is built and
  tested entirely from recorded fixtures.
- Write access is reported as unknown unless `--probe-write` is passed, because
  `os.access(W_OK)` lies under root-squashed NFS.
