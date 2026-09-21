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
