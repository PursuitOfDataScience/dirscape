# Changelog

All notable changes to `dirscape` are recorded here, newest first, following
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/).

## [Unreleased]

### Added

- **`/` searches every name below the folder in view**, or below the
  highlighted row of the table: files and folders at any depth, by a piece of
  the name (`era5`) or a shell pattern for the whole of it (`*.nc`), with
  `raw/` for folders only and `data/raw` for names in a folder whose path
  holds `data`. Matches arrive shallowest first while the rest of the tree is
  still read, and Enter opens the folder a match is in. It reads names only,
  never a `stat`, so a tree is searched at the speed it can be listed: 1.36
  million names in 11.6s where `find` took 38.8s, 0.31x [95% CI 0.25, 0.37]
  over 10 paired, interleaved runs with every answer the same. What was read
  is kept, up to 4 million names, so a changed query is answered from memory:
  a median 0.041s over 60 queries on that tree. On Polaris's Lustre it reads
  33 thousand names a second.

### Changed

- **A folder opened before opens with its figures.** Every count the browser
  finishes is kept, with when it finished, in the state directory beside the
  lineage, and a folder opened again is drawn with those figures at once:
  sorted, shared and barred from the first frame, with a line saying how old
  they are. Figures over an hour old stay on screen while the folder is
  counted again behind the view, after every folder with no figure at all, and
  `m` counts the highlighted one again. `/project/rcc`, 84 folders and 37.7
  million files, took 22.8 minutes to count the first time and 0.03s to open
  complete after that; over 10 paired, interleaved opens of a 1.2M-file
  folder, 45.8s became 0.023s median, the slowest pair 0.0009x. A record
  carries its folder's inode number, so a folder made again under an old name
  is not given the old figure, and one from a node-local filesystem is read on
  its own node only. One file serves every cluster that shares the home it is
  in, and a record is tied to its filesystem rather than to a cluster, so
  `/lus/eagle` counted on Polaris opened on Sophia with those figures in 0.8s.
  `--no-state` keeps none.
- **A counted folder opens with its own folders counted.** The walk that
  counts a folder also keeps the figure of each folder directly inside it, so
  opening it next shows them at once, and a walk stopped part way still keeps
  every folder it finished: `/project/rcc/youzhi` opens with all 97 rows.
- **No count's work is thrown away.** A count under way now runs to its end
  instead of being cut off by the visit's 30-minute allowance, which only
  stops new ones from starting: cut off, a folder larger than one visit's
  allowance, 17 million files on a cold node, could never be counted at all.
  Opening a folder no longer stops the count in flight either: the level
  left carries on with it behind the view while the folder opened is counted
  with half the threads, so the two stay within rapidu's own ceiling of 24,
  and coming back shows it landing. Coming back also goes straight to the
  long counts, where it glanced again for 15 seconds at the folders the first
  glance had found too big to finish.
- **Without rapidu, a network filesystem is counted sixteen directories at a
  time**, rapidu's own default, with exactly the serial walk's figures: a
  173k-file folder on GPFS in 3.1s instead of 15.1s, 0.20x [95% CI 0.18, 0.23]
  over 10 paired, interleaved walks. Local storage keeps the serial walk.
- **Start-up asks the quota backends at the same time**, and lists allocations
  beside them, where it asked each in turn and waited for each: on this
  cluster six `mmlsquota` calls, the site wrapper and `accounts storage` ran
  back to back. A run went from 2.03s to 1.36s median, 0.67x [95% CI 0.66,
  0.67] over 40 paired, interleaved runs, the slowest pair 0.69x.
- **The line naming the folder being counted moves.** It repainted only when a
  figure landed, so a folder counted alone kept the figure it had at the
  start: `counting mehta5/: 2.4k entries so far`, for 1.36 million entries. It
  now repaints every second.

### Fixed

- **With rapidu installed, `files` counted every symlink twice.** rapidu's
  `files` already holds its symlinks and special files, and adding them in
  again had a tree of 148,402 files read 148,727.

## [0.2.0] (2026-09-24)

### Changed

- **Opening a row shows the start screen's own table, one level down.** The
  listing had a table of its own, `name`, `access` and `items`, and `items`
  was the number of names directly inside each child: neither a size nor a
  file count, and "a confusing word". Each child is now a row of the same
  table, drawn by the same renderer with the same cells, headed by the
  directory and naming each child. The same rule drops a column that reads
  the same on every row, now for a one-row directory too, and `kind` is left
  out one level down, where it is the parent's kind on every row. A child the
  sweep already measured shows the table's own figures. A deep directory's
  title wraps at a `/` instead of being cut.
- **Every child is counted behind the view, to the end, and a figure is
  shown only when its count has finished.** The directory is drawn at once,
  `…` where a figure is coming, the folders on screen first. A first pass
  glances at every child, held to the table's walk bounds and shortened when
  there are many, so small folders are exact within seconds: on
  `/project/rcc`, 58 of its 84 folders at 30s. Every folder the glance did not
  finish is then counted to the end, one at a time and never restarted, the
  smallest first, for up to 30 minutes a visit, with a status line naming the
  folder in progress and its entries so far; `m` counts the highlighted one
  next. What an unfinished count had reached is never printed as a size: an
  earlier cut of this showed the owner's own folder, over 10T in 3 million
  files, as `288G+`. Folders still being counted sort first, since the glance
  leaves the largest unfinished. Sizes are kept for the session, walking
  stops when the reader leaves, and a hung mount can stop a figure but never
  the keys.
- **Each child's share of what the directory holds, largest first, once the
  whole is known.** A last `share` column gives the percent beside `used` and
  `files`, then a bar drawn against the largest share as ncdu draws its own,
  ending two cells short of the frame. The title says what the whole is,
  `/home/jdoe42 · 894M in 52 folders`, and `counting, 60 of 84 folders done`
  until then, because shares of a partial whole put a 40G folder at 60% of a
  directory holding 10T. In name order the share meant little, since a home
  directory's first screen was twenty dot-directories at `<1%`, so rows sort
  by size as figures land, the highlight kept on its folder, and `s` switches
  to name order and back. The gutters keep their own width, where three
  columns spread across the window had opened gaps of fifty spaces. The
  highlight stops one column past the percent, since inverse video turns a
  bar into a hole in the band, and the bar is the seven-eighths block, so bars
  on neighbouring rows keep a hairline apart. The start screen is unchanged.
- **rapidu sizes an opened directory's children when it is installed**
  (`pip install "dirscape[fast]"`), and nothing requires it. A cold GPFS tree
  is slow because each `stat` is a round trip to a metadata server and one
  thread waits on each in turn: the first walk of a 15,346-file folder took
  10.7s on dirscape's own walk. rapidu keeps sixteen in flight, and with it
  `/beagle3/rcc-staff` was exact in 11s, 20 GiB and 129k files in its largest
  folder. It is not faster on a warm tree: over 30 paired, interleaved walks
  of that folder warm, it took a median 0.37s to 0.16s, 0.43x [95% CI 0.42,
  0.43], slower in every pair, which a walk running behind the view never
  shows. Its figures are `du`'s, directory blocks included and hard links
  counted once.

## [0.1.1] (2026-09-24)

### Fixed

- **`recover` no longer prints a command that overwrites a file you still
  have.** Only a directory got the no-clobber form, because `isdir` was the
  only question asked, so `ds recover README.md` printed
  `cp -a <snapshot>/README.md README.md`, which replaces the live file with the
  older copy, and the MCP `recover_path` tool handed an agent the same line in
  `restore` to run. Every restore line is `cp -an` now. A file that still
  exists is restored beside itself under the snapshot's name
  (`README.md.daily-2026-09-23`), and a deleted file whose directory went with
  it gets a `mkdir -p` first, where it used to be told that the missing
  directory was "read-only to you".
- **`new` says which run it compared against.** "No change since the last
  run" named no time, and under `--since 30d` it was not even the last run:
  with no run that old, the oldest one kept was used without a word. It reads
  `No change since the last run, at 2026-09-24 10:01 (13m ago).` now, and a
  window the lineage cannot cover adds `no run is 30d old yet, so --since 30d
  compared against the oldest one kept`.
- **A line of config that does nothing says so, with the word it probably
  meant.** `[rolez]`, `nmae` in `[site]`, a role `scrach` in `[roles]` or
  `[heuristics]`, a `[policy]` line with no `key=value`, `purge_days=30d` and
  an unknown `[quota] order` backend were all read and dropped in silence,
  in `paths` and in `why`. Each is a warning now, on stderr and in every agent
  payload: `site.conf: unknown section [rolez], ignored (did you mean
  [roles]?)`. JSON configs are checked the same way, and the shipped template
  still loads with none.
- **`[quota] order` takes the names the template documents.** Only the names
  `why` prints (`mmlsquota`, `site quota wrapper`) ever matched, so
  `order = wrapper, gpfs` as documented changed nothing. Both spellings work
  now, each backend is asked once however often it is named, and `ceph` is in
  the documented list.
- **The stock `quota -s` stands aside for the site wrapper it turns out to
  be.** Where a site installs its wrapper as `quota`, both backends ran the
  same script and got byte-identical reports. The stock backend is skipped
  when the wrapper already read rows from the same file, and still runs where
  `quota` is the real tool, which the wrapper backend also tries and cannot
  read. On the development cluster, over 30 paired, interleaved runs of
  `ds paths --json` after 3 discarded warmups, the median run went from 2.97s
  to 2.03s: 0.94s saved [95% CI 0.93, 0.96], a median 1.46x [1.456, 1.472],
  and 1.43x in the worst pair (Wilcoxon p = 2e-9).
- **A hung mount can no longer freeze the browser.** Opening a row read the
  directory and probed each child on the UI thread with no deadline, so one
  wedged mount under it froze the session. Both run under deadlines now (3s
  for the directory, 1s for each child and 3s for all of them), and a child
  that did not answer says `did not answer` in its access cell.
- **Codex and opencode are recognised as agents**, by the `CODEX_THREAD_ID`
  Codex exports to every command it runs, the `CODEX_SANDBOX_NETWORK_DISABLED`
  of its sandbox, and the `OPENCODE=1` opencode sets for its shells. Either
  one moved the `new` baseline on every look and could get the browser in a
  pty.
- **Docs that had drifted from the code.** `--legend` no longer promises
  "reach letters". The `--help` epilog and the README no longer say there is
  no tree walk: there is one, capped, of a root no quota covers, and
  `--no-measure` skips it. The notes for 0.1.0 below have their own heading,
  and its known limits are the ones it shipped with.

## [0.1.0] (2026-09-23)

### Changed

- **The site plugin knows no site.** Everything it did for one cluster, the
  allocation table it parses, the fileset membership rule and its special
  case, the daily quota archive it bisects, the wrapper scripts, dataset and
  snapshot roots and role labels it supplies, now comes from a `[plugin]`
  section of `site.conf`, and with no such section there is no plugin. The
  code is the same code: its constants moved into configuration, and the
  output on the cluster it was written for is unchanged, checked view by view
  against the previous release with that cluster's settings in a config file.
  `dirscape --site-template` documents every key.
- **`[heuristics]` extends a built-in role in place.** `archive = */vault*`
  is checked where `*archive*` is, after the home and scratch patterns and
  case-insensitively, where a `[roles]` glob would override everything. Two
  archive words that only ever described one site left the built-ins for it.
- **`[quota] decimal_suffix_mounts`** names the wrapper sections printed in
  1000-based steps. No mount is decimal by default any more, and the wrapper
  paths the package searches on its own are down to `/usr/local/bin/quota`.
- The test suite no longer reads the machine's own site config, so a
  developer's `~/.config/dirscape/config.conf` cannot change what it tests.

### Added

- **`dirscape recover <path>`: every read-only copy the filesystem still
  keeps, newest first, with the literal path to each and a `cp` line to
  restore from.** The path does not have to exist, which is the whole point:
  a user reaches for this after `rm`, when the live file is gone, no root
  describes it and no quota scope owns it. The mount table still names the
  device, the device names the mountpoints, and the snapshot trees are under
  those.
- **A held arrow accelerates.** Tap it and the highlight steps one row; hold
  it and after 1.5 seconds it starts covering ground, 4 rows per press and
  doubling to 32. `HeldKey` is ported from `slurmpast`'s `_HeldKey`, which
  the owner named as the reference, with the threshold shortened because the
  longest thing here is a directory of a few hundred children rather than a
  29,617-row job history. A deliberate tapper never accelerates: a held key
  repeats at 25-33 Hz and a reader pressing an arrow manages three or four a
  second, so the run has to average `ACCEL_MIN_RATE` before the ramp is
  allowed, tested once when the threshold is crossed and latched both ways.
  Reversing, letting go, or pressing anything else ends the run, which is how
  somebody stops after overshooting. The whole ramp is clockless and the
  caller passes the time, so every point on it is tested without a terminal
  or a sleeping test.

  **Wrapping now applies to a tap and not to a hold.** One press off the top
  meaning "jump to the bottom" is a deliberate shortcut worth keeping; the
  same wrap arriving two seconds into a hold throws the reader back to the
  other end of a directory they were reading down.

  `select` also folds a keypress that is ALREADY WAITING into the current
  frame instead of drawing one nobody will see. That is not a refinement, it
  is what makes the accelerator safe: this loop repaints the whole block per
  key, so without coalescing a held arrow fills the terminal's input buffer,
  the cursor keeps flying for a second after the reader lets go, and the list
  stops where nobody asked. Coalescing bounds the backlog at zero by
  construction.
- **`/snapshots`, and any other snapshot tree a site publishes outside its
  filesystems.** `[snapshots] roots` in `site.conf`, supplied automatically by
  the site plugin where the directory exists. The hidden `.snapshots`,
  `.snapshot`, `.zfs/snapshot` and `.snap` trees inside a filesystem still
  need no configuration; this covers the case they cannot reach, and on this
  site that case is the only route a LOGIN node offers. `/snapshots` is a
  plain top-level directory belonging to no device, so nothing in the mount
  table leads to it and `_bases_for`, which works outward from a root's own
  device, can never arrive there. Owner: "/snapshots is still not shown, even
  when running it on the login node."

  A declared root is told where the tree is, not what shape it has, because
  both shapes are in use here and neither is worth making an administrator
  describe:

      /snapshots/<SNAP>/home/<user>          meadow3: snapshots directly
      /snapshots/home/<SNAP>/home/<user>     meadow2: one level per filesystem

  `SnapshotIndex.containers` tells them apart by asking whether the entry
  names parse as snapshot names, and descends exactly one level when they do
  not, capped at 32 so a root pointed somewhere useless costs one listing.

  The directory itself is a discovery source of its own and is always ranked
  SECONDARY: it is reachable storage a reader can copy out of, so leaving it
  off `--all` would be a lie by omission, and it is read-only and holds no
  allocation, so a row in the default table would break that view's one
  promise. `dirscape recover <path>` is where it does the work.
- **Snapshots are now a measured axis on every root**, shown as a `snapshots`
  line in `dirscape why`, a `snapshot` column in `dirscape matrix`, and
  `recoverable` plus `snapshots` in `--json`. Three answers, kept apart
  because conflating any two of them is how somebody loses data: copies were
  opened (`✓`), the filesystem exposes a snapshot directory and is keeping
  nothing in it (`✗`), or no snapshot mechanism was found at all (`?`, since a
  site can back up to tape without exposing one).

  Why this was invisible before: a snapshot directory is not a mount, is owned
  by root, and matches no group template, so none of the five discovery
  sources could see one. On the development cluster the documentation also
  publishes only `/snapshots/<SNAP>/...` and says it is login-node only, while
  `/gpfs/meadow3/cap/.snapshots/<SNAP>/home/<user>` is readable from a compute
  node right now. A user in a batch job was being told recovery was impossible
  when it was one `stat` away.

  Four measurements shaped the implementation and each is recorded in
  `discover/recover.py`:

  - **A copy can never be a `Root`.** The live `/home/jdoe42` and its copies in
    three different snapshots all report `dev=54 ino=212501245`, the identical
    pair that `candidates._dedupe` keys on, so any snapshot offered as a
    candidate root is silently folded onto the live path and vanishes. Copies
    are an attribute of a root instead.
  - **Two path layouts, both probed, neither guessed.** GPFS snapshots a whole
    filesystem, so the copy of `/home/jdoe42` is at
    `<fsroot>/.snapshots/<snap>/home/jdoe42`, keyed by absolute path even
    though `/home` is a junction mounted elsewhere. NetApp and ZFS key
    relative to the mountpoint. The winning layout is cached per snapshot
    directory.
  - **Which mountpoint is the filesystem root cannot be guessed either.**
    `meadow3_cap` is mounted at `/home`, `/project`, `/programs`, `/software`
    and `/gpfs/meadow3/cap`; `/home` is the shortest and its `.snapshots` is
    empty, so a shortest-path rule reports "this filesystem keeps nothing"
    about a filesystem with eleven snapshots. Every mountpoint of the device
    is tried and the filesystem answers.
  - **Timestamps come from the snapshot NAME, never its metadata.** Every
    directory under `/gpfs/meadow3/cap/.snapshots` stats as
    `mtime 2021-08-04 05:23:07`, which is the fileset's creation time, so
    `st_mtime` would date this morning's snapshot and last month's to the same
    day in 2021.

  Cost: 0.049s for the whole node, because it is a directory read per
  filesystem and the listings are cached by `(base, snapshot directory)`. Ten
  roots on one device produce three reads, not thirty.
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

### An interface for agents

The table is for a person, and an agent was left two bad options: scrape a
box of rounded figures with rows folded away, or read `--json`, which is the
whole model. On the development node that is 87 KB for the default view's
eleven rows and 282 KB with `--all`, most of it backend provenance from which
the agent would have to re-derive the very cells the table already shows,
`pick_row` and the quota placement rules included. Both doors now have a
third beside them, and everything in it is the table's own answer.

- **`dirscape paths`** prints the table's paths, one per line, in the table's
  order, so `xargs`, a `for` loop and `head -1` take it directly. `--json`
  gives one record per place: `used`, `quota`, `free`, `files` and
  `max_files` exactly as the table and `why` print them, and beside each the
  exact number (`used_bytes`, `quota_bytes`, `free_bytes`, `files_count`,
  `max_files_count`), null exactly where the cell is `?` or `none`, so a
  null is never ambiguous. `can_read` and `can_write` are three-state, and null is
  "not settled", never a no. `quota_scope` says whether `used` is yours or
  everyone's on that quota, `free_limited_by` whether `free` is your
  allowance or the filesystem's shared headroom, and `symlinked_to` names the
  places a home's symlinked folders are quietly filling. 10 KB for the same
  eleven rows, 35 KB for all 61. Every record is built from the cell functions the table uses,
  and a test compares the two cell by cell, so an agent and the person beside
  it cannot be told different figures.
- **`--writable`, `--kind` and `--min-free`** filter it. A misspelt kind or a
  size that is not one fails before the sweep, and a place whose free space
  is unknown never passes `--min-free`: "where fits 2T" is not answered with
  a place nobody measured.
- **`dirscape mcp`** serves the same answers as four read-only MCP tools over
  stdio: `list_paths`, `explain_path`, `recover_path` and `list_changes`.
  Standard library only, so it runs on the same bare `/usr/bin/python3` as
  the rest of the package, and checked against the official MCP Python SDK
  client (1.28.1, protocol 2025-11-25) under both 3.11 and the login node's
  3.6.8. One sweep answers for 60 seconds, so listing the storage and then
  asking about three paths costs one sweep, and every tool takes `refresh`.
  A bad argument comes back as a tool error the model can read and correct,
  not a protocol error it never sees; stdout carries protocol messages and
  nothing else.
- **`explain_path` answers for a path that does not exist yet**, with the
  place it would be created in and `exists: false`, because "can I write my
  output to `.../run42`" is asked before `run42` exists. Where a directory's
  quota is reported one row up, as `/project/hpc/jdoe42` under
  `/project/hpc` is, the figure is followed instead of answered with `?`.
- **An agent never moves the baseline.** `paths`, every MCP tool, and every
  other command run under an agent harness read the lineage, so their rows
  and change labels match the table's, and never write it. `snapshot` is the
  one exception, because recording is what it was asked to do. Otherwise the
  person who runs `dirscape new` next is told nothing changed because their
  agent looked five minutes ago.
- **An agent harness never gets the browser.** `AI_AGENT`, `CLAUDECODE` and
  `GEMINI_CLI` (and `DIRSCAPE_AGENT` for anything else) turn it off; in a pty
  nobody is there to press `q` and the command would hang. Under one of those
  the printed table is followed, on stderr only, by a pointer to `paths
  --json`.

Fixed on the way:

- **`dirscape why <path> --json` ignored the path** and printed every
  visible root, so a script asking about one directory had to redo `why`'s
  search to learn which record was the answer, and the view's own footer
  pointed there for "every field". It is now that root's native record plus
  `asked` (how the path was matched) and `place`, and it shares the agent's
  answer for a path that does not exist yet. The text view still refuses
  such a path, since a person who typed it more likely mistyped it.
- **Restore commands are quoted for the shell.** `cp -an SNAP/. DIR/` split
  in two at a space in a directory name, and an agent runs that line rather
  than reading it. An ordinary path prints exactly as before.

### Fixed by running on two other clusters

Everything below was found by running this package on ACME Procyon and
Sylvia, which are Lustre and NetApp where the development cluster is GPFS,
and whose login nodes run Python 3.6.15 and 3.9.25. **Procyon is the floor
this package claims**, so it is now a tested claim rather than a stated one.
On a first run there the reader's only writable allocation was missing, their
home read `? ? ?`, and `--all` listed sixty frozen copies of other people's
directories.

- **A user-scoped figure never reached the directory that was the reader's.**
  `lfs quota` reports against the MOUNT, so the row reads `mount=/home
  scope=user used=35.7G hard=373G`, while the root a reader cares about is
  `/home/jdoe42`. Lustre has no fileset and sets no project id on a home
  directory, so every matching clause came up empty and a home with 35.7 GB
  in it rendered `? ? ?` beside a `free` figure for the whole 157T
  filesystem. A user-scoped row now carries down, under two conditions that
  keep it from being prefix inheritance: USER scope only, and only onto a
  directory the reader owns or can write. Somebody else's home under the same
  mount still gets nothing.
- **An allocation two levels down was invisible.**
  `/lus/egret/projects/lanternlab-exampleu` is 29.58T against a 50T project
  quota and is the reader's only writable allocation on that cluster; the
  flat template looks for `/lus/egret/lanternlab-exampleu` and the dir-owner
  scan reads one level, so neither reached two. The group template now also
  tries `<root>/<child>/<group>`, but **only where the flat pass found
  nothing**, which is what makes it free: a root whose flat template already
  produced a directory keeps its allocations at the first level, and listing
  `/project` and `/project2` to learn they hold 669 and 892 entries cost 0.2s
  of a three second run. Bounded again by `NESTED_FANOUT`, and never run on a
  scratch root or on a secondary plumbing mount.
- **A project quota was never asked for.** The quota sweep runs before
  discovery, because its fileset enumeration is itself a discovery source,
  and it asks about `/`. That is enough for `mmlsquota`, which lists every
  fileset the reader holds on a device whatever path named it, and it is not
  enough for a project quota, which is a property of the DIRECTORY: `lfs
  project -d /lus/egret` returns 0 while the same command on the allocation
  returns 13579. Backends now declare `per_path`, and the ones that do are
  re-asked once with the roots that came back empty.
- **`lfs quota` capped its path list before filtering it.** Slicing to
  `MAX_PATHS` first meant a few non-Lustre paths at the front starved the
  backend completely: the unanswered roots begin `/`, `/admin_home`,
  `/boot`, so every slot was spent before a Lustre path was reached and the
  fallback asked about the mounts instead. Filtered first, capped second, and
  the cap is 8 rather than 3 now that the caller passes real roots.
- **An exact mount match now outranks a fileset name that disagrees.** Lustre
  identifies an allocation with a NUMBER and labels its rows with a name:
  `root.fileset` is `13579` and `row.fileset` is `lanternlab-exampleu`, both
  correct, and comparing them finds nothing. Guarded by `row.guessed`, which
  is not optional: a row's mount is sometimes inferred from its fileset name
  rather than measured, and without the guard this clause handed `/project`
  the 11T belonging to `/project/hpc`. Caught by a before-and-after control
  on the GPFS cluster before it shipped.
- **The wrong scope was displayed when several described one directory.**
  Three scopes name a Lustre project directory and only one is the
  allocation: `user 4.1T no limit`, `group 2.7P no limit`, `project 29.5T of
  55T`. Taking the first row printed `4.1T of none` against a directory whose
  answer is `30T of 50T`. A row carrying an enforced limit now sorts first,
  stably, so where nothing is enforced the user-scoped row still leads. And
  the file count is taken from the SAME scope as the byte figure, which it
  was not: the row read `30T of 50T` beside `216k`, this reader's file count
  across the whole filesystem rather than the allocation's 8.2M.
- **A snapshot could win the quota selection and then govern nothing.**
  `select_snapshot` chooses on a path prefix while `_rows_governing` is far
  stricter, so a root fell back to `?` with a better answer unread in the
  next attempt. That gap is what made the per-path re-ask useless: the
  sweep's snapshot holds a row for the mount `/lus/egret`, a prefix of the
  project path, so it won and governed none of it.
- **Snapshot trees were being offered as places to put data.** On NetApp
  every retained snapshot is its own NFS mount, so the mount table itself
  offers them up, and the dir-owner scan then read one level of each. `--all`
  listed three snapshot mounts and all twenty frozen homes inside each of
  them: sixty rows of other people's directories, in the view that answers
  where the reader can put 2 TB. Anything inside a `.snapshot`,
  `.snapshots`, `.zfs/snapshot` or `.snap` component is ranked secondary and
  never scanned. The mounts themselves remain in `--all`, correctly labelled,
  because they are real mounts and `--all` means all.
- **`dirscape recover` listed every NetApp snapshot twice**, once from the
  mountpoint and once from the path's own base, because NetApp exposes
  `.snapshot` inside every directory. Deduplicated by NAME and deliberately
  not by `(st_dev, st_ino)`, which would collapse eleven genuinely different
  GPFS dates into one, since GPFS gives every snapshot of a directory the
  same inode as the live path.
- **A read-only OS image was searched for allocations, and won the dedupe.**
  A Cray login node mounts four squashfs images, one of them `/root_ro`, and
  `/root_ro/egret` is a symlink to `/lus/egret/projects`. So the reader's
  allocation was reported as `/root_ro/egret/lanternlab-exampleu`: four
  characters shorter than the real path, which is all "shortest path wins"
  needed. Two fixes, because either alone leaves the other bug: `squashfs`
  and `iso9660` join the filesystems nobody holds an allocation on, and the
  dedupe now prefers a path that is its own `realpath` before it prefers a
  short one.

### The interactive view stops flickering

- **An arrow no longer blanks the screen.** Every keypress sent
  `ESC[<n>A ESC[J`, up to the top of the table and erase everything below,
  and then the new frame, as two separately flushed writes. Whenever the
  terminal rendered between them, which over ssh and through tmux it often
  does, the table vanished and came back. Measured in a 40 x 120 pty with a
  terminal emulator fed every chunk the program wrote, over 12 taps and a
  2.5 second held arrow, five runs each: before, 172 to 174 of about 260
  screen states showed a partial table and 83 to 87 showed it gone entirely;
  after, none of either, in the table and in a listing. `interactive.repaint`
  now draws each frame over the last in place, rewrites only the lines that
  changed (a moved highlight is two lines, not thirty: 43 KB instead of
  465 KB for the same keys), pads a narrower line over its predecessor
  rather than erasing it, clears rows only after the new frame is drawn, and
  sends the frame as one write inside a synchronized update (DEC mode 2026),
  which terminals that support it show atomically and the rest ignore.
- **Opening a row or stepping back draws over the view instead of after a
  blank one.** One `interactive.Screen` is shared by the table and every
  listing, so only quitting erases; the table used to disappear the moment a
  row was opened and stay gone while the directory was read.
- **Warnings reach the terminal.** `main` returned straight out of the
  interactive view, so in a terminal, where somebody is reading, a run that
  ran out of time showed a table of `?` and never said why. Measured on a
  meadow2 login node: 48 rows of `?`.
- **A slow search cannot starve the probes.** The searching sources (name
  templates, group ownership) may spend half of what is left when discovery
  starts, checked per `stat` rather than per mount, and roots are probed
  most-wanted first, so a run that does run short loses plumbing mounts and
  not the reader's home. That meadow2 run had spent the whole allowance
  searching and probed nothing.

### Fixed on a second pass across Procyon, Sylvia and meadow2

Run again on ACME Procyon (SLES 15, Python 3.6.15) and Sylvia (RHEL 9,
Python 3.9.25), and for the first time on HPC meadow2 (RHEL 7, Python 3.6.8,
pip 9.0.3). The interactive view was driven at a real terminal on both ACME
machines, and `new` was run from a second Sylvia login node. The suite now
passes on all three system Pythons and on 3.13, and the wheel installs with
pip 9, 20, 21 and 26.

- **A figure for a whole filesystem sat on directories that were not the
  reader's.** `lfs quota -u` covers the filesystem and is printed against
  whichever path it was asked about, so Procyon showed `/lus/grove read only
  4.1T none 216k` (the reader's usage across all of Egret, on a clone holding
  nothing of theirs), Sylvia repeated the home's `36G of 342G` on `/lus/acorn`,
  and `dirscape map` reported 34T across two roots. User and group rows now
  carry no fileset, and a filesystem-wide row only lands on a directory the
  reader owns or can write. The mount basenames that used to label them
  (`home`, `grove`, `acorn`, `egret`) fed discovery and the stranded check as
  if they were quota scopes; a project row is now named by its id, the same
  string `lfs project -d` gives the directory.
- **Storage in the table was reported as "held with no reachable path".**
  Sylvia's `--json` listed four such filesets, every one mounted and
  listable. A fileset whose published row names a reachable root is now
  reachable, whatever the two tools call it.
- **Lustre subdirectory mounts are one filesystem.** `/home` is
  `<nids>:/acorn/home` and `/lus/acorn` is `<nids>:/acorn`; ranking compares
  `Mount.filesystem`, so `/lus/acorn` is held back as the filesystem root.
  Device strings are also no longer cut at 128 characters: an eight-NID
  device lost its `:/acorn` suffix, which merged two mounts into one key.
- **The cluster key moved every time the tool ran.** Eight state files
  appeared in four minutes from six runs, because NetApp mounts each snapshot
  a reader touches and `recover` touches them. The key now hashes
  `MountTable.fabric()`: no snapshot mounts, an NFS export reduced to its
  server, a Lustre mount to its filesystem. A GPFS-only cluster keeps the key
  it had.
- **Everyone's primary group made everyone's directory a candidate.** Every
  ACME account's primary group is `users`, so `/admin_home` contributed 75
  rows to Sylvia's `--all`, most of them `no access`. A gid that owns the
  directories of four or more other people in one listing is read as a
  default group; only the reader's own directories survive under it. The
  largest real match on meadow3 is two root-owned directories.
- **Mounted snapshots are no longer roots.** This reverses the entry above
  that kept them in `--all`: they were 30 of 49 rows on Procyon, and with the
  hourly rotation each would have been `new` in one run and `gone` in the
  next. `recover` reads them from the mount table, and `why` on a path inside
  one points there.
- **A run that ran out of time printed wrong figures, not unknown ones.** The
  first run of a session on meadow2 needed about 8.5s of budgeted work
  against an 8s allowance and printed `/project 851M 30G`, a home quota from
  another cluster, on a directory nobody had probed. Nothing is placed on an
  unprobed root now, the quota sweep may spend only 60% of the allowance
  before discovery starts, a cut-short run says so, and the default is 20s.
- **A walk no longer claims there is no limit.** With the sweep cut short, a
  walked `/scratch/meadow3/jdoe42` read `22G of none` while GPFS enforces
  100G. A walked figure says `none` only where the mount table does.
- **A shared drop like `/tmp` is walked for the reader's files only.**
  Sylvia's `/tmp` holds 2.0 million entries from every account (`find` needs
  17.7s) and 159 of the reader's, so the walk gave up and the table showed
  `? ? ?`; on meadow3 it finished and reported the whole node's `1.2G` under
  `used`. World-writable is the test, since meadow3-0200's `/tmp` is `777`
  with no sticky bit. The walk also stops spending its time on other
  people's group trees (2.5s per meadow2 run on a 149G fileset and a 155T
  CephFS tree, neither finished) and does local disks first. A meadow2 run
  went from 5 to 8.6s to 3.6s.
- **CephFS figures come from CephFS.** A new backend reads `ceph.dir.rbytes`,
  `ceph.dir.rfiles` and `ceph.quota.*`, with no command at all:
  `/cfs3/kestrel-lab 155T of 165T` and `/cfs3/hpc-staff 59M of 10G` on
  meadow2, where both read `? ? ?`. Each quota directory is its own scope, so
  `tree` no longer groups them under one line with the first one's quota.
- **A directory inside a memory filesystem is held back.** meadow2 login
  nodes are diskless: `$TMPDIR` is `/tmp`, a plain directory in a tmpfs `/`,
  and it sat in the default table as `local /tmp ? ? ?`.
- **ext2/3/4 mounted without a quota option enforce no limit**, and saying
  so turned Sylvia's `/tmp` quota from `?` into `none`. Notes on ext4 say
  `ext4` rather than `XFS`.
- **`why` follows the path to where it lives.** ACME documents
  `/egret/<project>`, a symlink, and `why` walked up to `/` and explained the
  node's system image instead of the reader's 30T project. It also refuses to
  explain a path on a mount it does not report (`/dev/shm` explained `/`).
- **`dirscape new` across round-robin login nodes.** NFS and tmpfs number
  `st_dev` per client, so the second Sylvia login node reported four network
  roots as "replaced", and node-local roots were compared across two
  different disks. Across hosts, identity is now the inode alone and a
  node-local root is not compared (with one line only when it would have
  differed). `new` also printed an empty box when every change was `gone`.
- **PBS, LSF and Flux jobs are compute nodes**, as are HPE Cray xnames
  (`x1234c0s13b0n0`) and `sylvia-gpu-07`. The cluster reads `procyon`, not
  `procyon-login`.
- **Restore hints that work.** `recover` suggested `cp -a SNAP DIR` onto a
  directory that still exists, which nests the copy as `DIR/DIR`; it now
  suggests `cp -an SNAP/. DIR/`, and copying out to `.` where the tree is
  read-only. `why` stopped offering to copy a snapshot over ACME's read-only
  `/soft`, and uses `-n` so a 47-day-old snapshot cannot roll a home back. A
  declared snapshot tree no longer vouches for `/` (meadow2's `/` claimed 18
  copies out of `/snapshots/home`).
- Smaller: `1 copy` in the singular, an overlay is no longer "a memory
  filesystem", a listing says how many entries it left out and its rule
  reaches the border, and a warning `new` already printed is not repeated on
  stderr.

Known: building from the sdist needs Python 3.7 or newer (`setuptools>=64`),
so Python 3.6 installs from the wheel, which pip prefers anyway.

### Fixed

- **One row of the table could not be reached with the arrow keys.** Owner:
  "when the highlightor is on the gpfs row and when i press the down arrow,
  it will skip /software and jump directly to /cfs."

      software   /gpfs/meadow2/perf2/software   read + write   14G   none
                 /software                      read + write     ?      ?   <- unreachable
      archive    /cfs/hpc-staff                 read + write     ?      ?

  Nothing was being skipped and the cursor was always right. `_table_frame`
  locates the band by finding the cursor's path in the rendered text, and it
  did so with a plain substring search: `/software` occurs inside
  `/gpfs/meadow2/perf2/software`, which renders one row ABOVE it, so the band
  was repainted exactly where it already was. To a reader that is a key that
  did nothing, and the next press moved on.

  Matched on a whole table cell now, which means whitespace or an edge on
  both sides of the path: the `2` in front of `perf2/software` rejects it,
  and the real row has a gutter on each side. Locating by text rather than by
  counting chrome is still right, because the chrome changes with the window;
  the search just has to be as precise as the thing it is searching for. The
  path column is `atomic` in this renderer and is never ellipsised, so a
  whole match is always there to find.
- **The highlight would not travel to the bottom of a listing.** Owner, ten
  rows from the end of an 84 item directory: "the highlightor isn't at the
  bottom when scrolling down, it's somewhere in the middle." The counter said
  `64 of 84, 52 above, 10 below`, so ten rows were on screen below a band
  that would not move onto them.

  `_listing` is called afresh on every keypress and handed only the cursor,
  and it computed the visible slice as `cursor - room // 2`, which pins the
  highlight to the middle of the window for ever. The window's position is
  now remembered across repaints by the caller and moved only when the cursor
  reaches an edge, which is what every list a reader has ever used does: the
  band walks down to the last visible row, and only then does the list scroll
  under it. So the bottom row is reachable, the top row is reachable, and a
  list that fits never scrolls at all.
- **Six of the eight `?` rows on a login node were figures the tool had
  already read.** Owner: "why so many `?`? i told you not to have them. why
  can't you retrieve the numbers?"

      /cfs3/kestrel-lab   read + write   ?   ?   ?        <- what was shown
      mount=/cfs3  scope=kestrel-lab  used=155T  limit=165T   <- what was read

  `mmlsattr` is a GPFS tool, so every non-GPFS root arrives with no fileset
  at all, which on a login node is the whole cost-effective storage tier. The
  only remaining clause then required a row's mount to equal the path
  exactly, and a row describing `<mount>/<scope>` does not describe
  `<mount>`. Rows are now also matched when `<row mount>/<row scope>` is
  exactly the root's path, which names one directory and no other. This
  replaces the fileset-prefix-stripping clause added earlier in this release:
  the same join covers `collie3-hpc-staff` against the wrapper's `hpc-staff`
  without any site needing to configure prefixes, so that code is gone.

  The bare-mount clause is kept, because the wrapper also prints
  `scratch/meadow3` as the scope of the row whose mount IS
  `/scratch/meadow3`, where joining the two names nothing. `stat` is what
  tells the two apart, since the strings cannot: a row whose
  `<mount>/<scope>` is a real directory is about that directory and must not
  also be handed to the mount. Without that test `/cfs3` claimed the `155T of
  165T` belonging to a different group entirely.
- **The table had no air in it.** Owner: "the vertical spacing is too narrow,
  especially the column row and the first row." Title, blank, rule, headings,
  data: five lines of chrome with a gap in only one place, so
  `kind path access used quota files` sat directly on the rule above it and
  directly on `/home/jdoe42` below it and the eye had nothing to separate the
  labels from the figures. One blank line on each side of the heading row.
  Safe for the interactive view because `_table_frame` locates the highlight
  by matching the row's path in the rendered text rather than by counting
  chrome.
- **A directory you can only read is folded away when its writable subtree is
  already on screen.** The login node printed 22 rows of which most were
  unusable, including this:

      archive   /cfs3               read only    155T   165T      ?
                /cfs3/kestrel-lab   read + write    ?      ?       ?
                /cfs3/hpc-staff     read + write    ?      ?       ?

  Owner: "/cfs3 i only have 2 dirs that i can access and both of them are
  listed but why /cfs3 should be shown here?" The parent is a fileset root
  nobody can write to and its `155T of 165T` is every group's usage on the
  cluster, not the reader's. `_fold_covered_parents` is the mirror of
  `_collapse_families`: that one says "if the whole tree is yours, one row
  says so", this one says "if the tree is not yours but part of it is, the
  part that is, is the answer". Narrow by three conditions: same device only
  (`/scratch` holds three clusters' filesystems and folding on path alone
  would hide two of them), a strict descendant, and never a row carrying a
  delta or a stranded flag. Folded rows are counted and stay in `--all`.
  Measured on the login node's root set: 12 rows become 8, and every one that
  remains is somewhere the reader can write.
- **`/` was being offered as a place to put data.** On a login node it is a
  real 312G disk, so `statvfs` and the measuring walk reported on it happily
  and it landed in the default table as `other  read only  20G  312G  311k`,
  between a user's project space and their scratch. It is the machine's own
  filesystem; anywhere under it a user can write is mounted separately and
  has its own row. Ranked secondary now, but only when another mountpoint
  exists, because on a single-filesystem machine `/` IS the storage and an
  empty table is worse than an imprecise one.
- **An unrecognised mountpoint hid a whole writable allocation.** The
  dir-owner scan and the name templates were gated on an ALLOWLIST of
  recognised roles, and a role is an advisory label from a glob heuristic, so
  the gate quietly meant "storage this heuristic has never heard of does not
  exist". `/collie3` matches no built-in pattern and scored the fallback role
  `other`, so nothing ever looked inside it and `/collie3/hpc-staff`
  (`drwxrws--- root hpc-staff`, writable by this user, 149 GiB against a 1 TiB
  group quota) appeared in no view at all: not the table, not `--all`, not
  `--json`.

  The gate is a denylist now, derived from `ROLES` by subtraction so a role
  added later is scanned by default rather than silently ignored. Every
  exclusion that remains is a measurement, not a guess: `software` because 713
  of its 749 entries are group-owned by `hpc-software`; `scratch` and `home`
  because they hold 13,909 and 13,915 entries and the per-user path is found
  by one stat instead; `local` because `/tmp` is mode 1777; `dataset` because
  it has its own unfiltered source. Memory filesystems are dropped before the
  role is consulted, so a diskless node whose `/` reports `rootfs` does not
  get its whole top level scanned. Cost of the widening: 0.028s.
- **One fileset spelled two ways reported `? ? ?` and then spent 1.5s proving
  it.** `mmlsattr -L /collie3/hpc-staff` names the fileset
  `collie3-hpc-staff`; the site's own `quota` wrapper prints the same
  allocation as `hpc-staff`. Exact matching found nothing, so the row showed
  no figures and the measuring walk burned its whole deadline failing to add
  up a 149 GiB tree the wrapper had already reported to the byte. Fixed by
  the `<mount>/<scope>` join described below, which gets there without any
  site configuring anything.
- `/collie3` is labelled `project` rather than `other` by the site plugin,
  which is the site's own word for it: its `quota` command prints "Capacity
  Filesystem: project (Collie3 GPFS mounted at /collie3)". A label only;
  discovery no longer depends on the role.

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

### The default view, redesigned

The first working version printed **80 lines** on a real account: 63 table rows
of which **50 were `?` in every data column**, a glyph legend reprinted on every
run, and a notes block that repeated the same sentence twice per line. That is
not a view, it is a dump. The default is now **20 lines** and answers the
question in the first nine.

- **A row must say something to earn its place.** Kept when write access was
  confirmed, a quota figure was measured, or something changed. The nine
  `/gpfs/<cluster>/<tier>` aliases, `/`, `/.nodelog/log` and `/programs` are
  counted and reachable with `--all`, never discarded.
- **Rows that are already summarised are no longer also tabled.** Stranded
  filesets and pathless allocations each get one summary line with their own
  subcommand, where before eleven rows of other people's directories sat above
  the four places the user could actually write.
- **Twenty dataset collections fold into their parent.** They share one fileset,
  so the parent holds the only figure and each child was a `?`.
- **A repeated role is printed once.** Nine rows reading `project` is the table
  stuttering.
- **A column with one value on every row is dropped.** `WHERE` read `here`
  everywhere and spent nine characters saying nothing.
- **Figures align on the separator**, so the column reads as a set of magnitudes
  a reader can compare rather than four numbers at four offsets.
- **Inode counts, the glyph legend and per-root notes left the default.** They
  are detail, and `--all`, `--legend`, `--json` and `why` are where detail
  belongs.
- **Ordered by role**, your own space first and machinery last, rather than by
  mount-table order, which interleaves `/gpfs/collie3/cap` with your home
  directory.

New: `dirscape stranded`, `dirscape elsewhere` and `--legend`. `elsewhere` is
purpose-built rather than a table, because with no path there is nothing to
stat and every column the table would draw is the unknown mark; it lists the
account, the location and the size the allocation database published.

### Default view, second pass: no question marks, and one row per tree

The 20-line view still had two problems a reader named immediately: things did
not line up, and a column of `?` was confusing. Both are fixed, and the row
selection was rebuilt on a rule that scales.

- **Show the highest directory you have full access to, and stop.** If the
  whole of `/project/xyz` is yours, that is the answer; enumerating your own
  filing is not information. A descendant survives only when it says something
  its ancestor cannot: the ancestor is not fully accessible (a PI directory you
  can read but not write, with one writable subdirectory in it), or the
  descendant carries a delta, or it sits on a different device or fileset and
  is therefore different storage that merely happens to be mounted underneath.
  Three earlier rules are recorded in the code so they are not retried.
- **`os.access(W_OK)` now answers instead of shrugging.** Returning
  `NOT_PROBED` for a directory somebody else owns printed `r?x` against every
  group directory on the cluster, trading an answer the tool has for a question
  mark about a root-squashed export this site does not have. The caveat moved
  into the reason and `--probe-write` still settles it by writing. uid 0 stays
  unknown, because uid 0 is the identity that hazard is actually about.
- **`statvfs` answers where no quota exists.** `/tmp`, `/.nodelog/log` and the
  node-local scratch are XFS mounted `noquota`, so `?` was the literal truth
  and useless: `df` knows the headroom. Shown as "886G free", never as
  `used / limit`, because it is the whole filesystem's room and not your usage.
- **The footer stopped contradicting the table**, which claimed "4 unmeasured"
  while every row showed a figure.

Alignment, all of it measured against the rendered output:

- Percentages are width 3, so 3%, 22% and 100% share a right edge instead of
  forming a ragged fringe.
- The capacity fallback joins the figure column instead of sitting hard against
  the column edge while every quota figure was right-aligned.
- The in-doubt mark is drawn ONCE, on the used figure. It was appended after
  the limit as well, so `11T / no limit▒` read as a mark against the limit or
  as a typo, and `836M / 30G ▎▒░░░░░░ 3%▒` marked one fact twice.
- The note count joined the footer rather than claiming its own line, since
  two one-line advisories both ending in `dirscape why <path>` is the same
  sentence twice.

Two bugs found while doing it. The quota owner and the row selection have to
agree or the figure disappears: the quota attached to `/project/hpc/jdoe42` on
an ownership preference while the table kept `/project/hpc`, so the 11T was
folded out of sight and the row fell back to free space. And nested folding
lost counts, because a child folded into its NEAREST accessible ancestor, which
could itself be folded away; the outermost surviving ancestor takes the count,
measured on a four-level tree that reported 4 folded rows against a surviving
count of 3.

### Interactive, and it degrades to the printout

In a terminal the table is now browsable: arrows or `jkl` move a highlight down
the rows already on screen, Enter opens `why` for the selected row, `q` leaves.
Piped, redirected, under `--json`, replaying a transcript, in a dumb terminal,
in a window under ten lines, or on a platform without `termios`, it prints the
static report. `supported()` is the single gate and `DIRSCAPE_NO_INTERACTIVE=1`
pins the print for a script or a recording.

Built the way `nodetop` built its own, and for its reasons: no dependencies,
because this runs on a login node where a full-screen library is not
installable at the moment it is needed; and **no second renderer**, because
nothing in the interactive path renders anything. It takes the finished lines
and paints one in inverse video, so the browsable view cannot drift from the
printed one.

The key semantics deliberately match `nodetop`'s so a user with both tools does
not learn two sets of arrows, including the two lessons that package learned
from being used: **Left at the root does nothing** (it used to return, and
returning at the root exits, so one stray press took the whole program down)
and **Right at a leaf does nothing** (it used to return the index, which a
caller with nothing to open read as "step back", bouncing the reader into the
view they were already in). Both have a test.

Inverse video rather than a colour, because it survives `NO_COLOR`, a
16-colour console and a light background alike, and does not collide with the
colours the table already uses to mean something. The cursor is hidden for the
duration, since a block cursor parked mid-table reads as a second, wrong
highlight, and the terminal mode is restored on every exit path including
`Ctrl-C`: a tool that leaves a login node without echo has done more damage
than the report was worth.

46 tests, including a **real pty** that proves the escape sequences land, a row
gets highlighted, a frame gets erased, the drill-down opens and the cursor is
restored. Scripted readers verify the logic and can say nothing about any of
that.

### Three UI defects, all reported from real use

- **The highlight was a different width on every row.** The cause was not
  padding, it was that the table's cells carry their own colours, so a row
  contains `\033[0m` several times along its length; wrapping such a line in
  inverse video turns the band OFF at the first embedded reset and the
  highlight stops mid-row. Measured in a pty: three rows highlighted at 47, 69
  and 74 columns, each stopping exactly where its first coloured cell ended.
  Every embedded reset now re-asserts the inverse, and the line is padded to
  the block width so the band is one shape moving down a column.
- **`why` was a wall of text.** Thirty-odd lines on a home directory, eleven of
  them the same sentence with a different path in it, raw byte counts
  (`used=874348544`) where a reader wanted `834M`, and the same in-doubt fact
  stated three times in three phrasings. Now nineteen lines: the two numbers
  and a one-word access summary first, the probes as a block, the symlinks
  collapsed to a count with `dirscape tree` to expand them, and one caveat
  rather than four. A probe that never ran is omitted entirely, since
  `? allocated not probed` on every mounted root teaches a reader to skip the
  column.
- **Quitting left a blank terminal.** Erasing the last interactive frame wiped
  the screen the user had been looking at. A browse now ends the way a plain
  run ends, by printing the report once on the way out, which also makes it
  robust to the cursor arithmetic being off by a line.

And one alignment bug that had been silently wrong since the capacity fallback
landed: the check for it was `word == "free"` against a DIMMED cell, so the
real token was `free\x1b[0m`, the equality missed, and the branch never ran.
Nothing raised; `886G free` simply sat four columns in while every quota figure
sat five. Every figure now ends at the same column, verified by measuring the
rendered output rather than by reading it.

### Bug hunt: eleven defects, four of them silent

Found by exercising every command and flag rather than by reading the code, and
the worst were the ones that produced plausible output.

**A chain of four around one broken key.** Every root with no path keyed on
`("", "")`, so the six allocations collapsed to ONE snapshot record: five were
invisible to the state layer for ever and a genuinely new allocation could
never have been reported. `RootRecord.from_json` then discarded any record with
no path, so even a correctly keyed one would not survive a reload. And the diff
rebuilt the key as `(record.device, record.path)`, a third definition, so it
could not find what it had just written and emitted six `new` plus six
`unknown` records on every single run. That is where the phantom "1 change
since the baseline" came from, and then "12 changes". There is now one
definition of identity, `root_key` for a live root and `RootRecord.key` for a
record, with a test that they agree.

**The rank filter never worked.** It read `getattr(root, "rank", ...)` against
a `Root` that has no `rank` attribute, so the default came back for every root
and the whole classification was inert. Discovery writes it to
`policy["rank"]`. The visible effect was a 94G tmpfs offered as somewhere to
put data, alongside six `/gpfs/<cluster>/<tier>` plumbing views.

**The fold count was stored and never rendered.** The selection rule hides a
root's subdirectories when the whole tree is yours, and the promise was that
the parent keeps a count. It was kept in `policy["contains"]` and displayed
nowhere, so twenty dataset collections folded into one row and nothing on
screen said so. The path cell shows `+20` now.

Also fixed:

- `dirscape why /nope/nope` walked up, found `/`, and printed a confident
  explanation of `/` with exit 0. It exits 2 and says the path does not exist.
  The first version of that fix over-reached and re-checked the filesystem for
  an exact root match too, which made the tool contradict its own probe and
  print "X does not exist. The enclosing root is X".
- `--timeout 0` and `--timeout -5` were accepted, and the run came back as
  sixty rows of `?`. A non-positive budget is a usage error.
- `dirscape snapshot --no-state` reported "Recorded 0 root(s) as a baseline",
  a claim to have done the one thing `--no-state` prevents.
- `dirscape new` showed the same five stranded rows for ever, under a heading
  that said "1 change". Stranded is a standing condition, so it keeps its
  alert line and leaves the change list.
- At 40 columns the last drop stage sacrificed `USED`, degrading the table to a
  bare list of paths with no number on it, which is not a smaller answer but
  the absence of one. `WHERE` goes first now.
- `tree` printed the same 200-character inferred-mount caveat once per fileset,
  seven times in a thirty-line view. Said once, and shortened.
- `matrix` showed three columns that were `?` on every row (`read` by
  construction, `purge` and `backup` for want of a `site.conf`) and then spent
  three lines of legend explaining them. Columns that are entirely unknown are
  dropped and named once.

### Portability, tested rather than claimed

`tests/test_portability.py` runs the whole pipeline on clusters this machine is
not: real directories under `tmp_path`, a synthetic mount table, and a
`RecordedRunner` that answers for exactly the tools that site has. A site with
no quota tooling at all, a Lustre site (the one backend with no live coverage
until Procyon), an ext4 site, a closed directory, and every view rendered on each.
The load-bearing one runs in STRICT mode, where a backend reaching for a tool
the fixture never recorded raises instead of guessing: on a real foreign
cluster that guess is a wrong answer nobody can see.

### Second hunt: the silent-failure round

Six more, and the theme is that a failure nobody is told about is worse than a
crash.

- **A path from argv was printed unsanitised**, so a directory named
  `evil\n/project/FORGED  999T  100%\x1b[31m` forged a table row inside `why`
  and put an escape sequence on the terminal. `Root.path` is cleaned at
  construction; this string never was. That is rapiDU's RD-6 arriving through
  the one string the model does not own. Two of the four sites were missed on
  the first pass and caught by the test.
- **A failed save was silent and then claimed success.** `Lineage.save`
  returns False rather than raising when it cannot write, and only the raise
  was handled, so on an unwritable state directory the run reported "a
  baseline has been recorded" while nothing reached the disk, and said it again
  on every run afterwards. It exits 1 and says so.
- **A damaged baseline was discarded without a word**, while a FOREIGN file
  three lines away did warn. Same outcome, so the same courtesy: a user who
  has been running this for a month and is told "no baseline yet" needs to know
  the file was unreadable. "Absent" and "present but unreadable" are now
  different answers.
- **`Run.warnings` reached `--json` and nothing else.** A malformed
  `/etc/dirscape/site.conf`, a damaged baseline, a plugin that failed to list
  allocations and a baseline that could not be written were every one of them
  invisible in the view a user actually reads. The table shows up to two with a
  count.
- **The interactive frame could be taller than the window.** A `--all` run in
  a 14-row terminal asked to move the cursor up 71 lines, which lands at the
  top of the WINDOW rather than the top of the block because the block has
  scrolled, and the erase that follows wipes whatever was above it. That is the
  "entire terminal turns empty" report, and `supported()` could not catch it
  because it knows the window height and not what is about to be drawn in it.
  A frame that does not fit falls back to the static print and says why.
- The stranded summary was about to be repeated as a warning, having already
  earned its own alert line.

Verified rather than assumed, on the real 61-root set rather than synthetic
records: lineage retention keeps 30 entries plus the oldest anchor, 643 KB
against the 1 MiB ceiling, a 49-hour span with the 20-hour gap sitting just
after the anchor, so `--since 30d` still has something to compare against.

### Third hunt: the quiet-flag round

- **`--since bogus` was accepted and ignored in silence.** `dirscape new`
  answered "No change since the last run" and never said the flag had not been
  understood, so a typo quietly changed which baseline was compared against.
  The short-message paths carry their warnings now.
- **A bad duration reported a mangled string.** Letting `float`'s own error
  out said "could not convert string to float: 'bogu'", having silently eaten
  the last character as a unit, which sends a reader hunting for a typo they
  did not make. It names what was typed and lists the units.
- **The treemap and the table disagreed about what had been measured.** The
  map said "no quota reading was taken" for the very roots the atlas was
  showing a figure for, because it knew only about quota rows while the
  capacity fallback lives elsewhere. The tile stays unsized on purpose, since
  `statvfs` reports the whole filesystem's headroom and sizing by it would let
  a shared `/tmp` dwarf the user's own project directory, but it now says
  which figure exists instead of denying there is one.

Checked and found sound, recorded so nobody re-tests them: eight concurrent
runs leave the state file valid JSON (last writer wins, two snapshots lost, no
corruption); a symlink loop through `$TMPDIR` terminates; a baseline dated
three days into the future reads as "dated ahead of this node" rather than as a
negative age; and the `elsewhere` sizes are decimal GB matching the site tool
exactly (256000 GB renders as 233T, which is 232.8 TiB).

### Fourth hunt: two gaps between the views and the flags

- **`--json` ignored the verb.** `dirscape stranded --json` emitted every
  root, so a script asking for stranded storage had to re-implement the
  filter, and `dirscape elsewhere --json` did the same. The three commands
  that FILTER roots now filter them in JSON too; `matrix`, `tree` and `map`
  are presentation variants of one set and are left alone.
- **`why` could not consume what `elsewhere` prints.** An allocation location
  such as `cfs4/hpc-staff` is exactly what a reader pastes back in, and
  `os.path.abspath` had already turned it into `$PWD/cfs4/hpc-staff` and
  reported that as missing. It matches on the raw argument now, with or
  without a leading slash, and explains the allocation: size, account, and the
  fact that the absence is about this node rather than about the storage.

### The detail view, rewritten for the reader

`dirscape why <path>` and the row you open from the browse are one block, and
it was written in the tool's own vocabulary. Reported from real use: "do you
actually know what the text here means? it's so confusing. what does mount
mean here? everything needs to be accessible."

- **Pressing Down in the detail view repeated the header.** Thirteen presses
  left thirteen copies of `/project/hpc` stacked above the detail. The
  interactive layer repaints by moving the cursor up by the number of lines it
  WROTE, and the block occupied one row more than that: measured at 18 lines
  against 19 rows at every width from 80 to 120, because the writable
  verdict's reason ran to 157 characters and the terminal wrapped it. The
  block is now hard wrapped to the window before it is handed over, so lines
  and rows are one to one (measured at 42, 80, 90, 100, 110 and 120 columns,
  with colour on and off), and Up or Down in a one row view no longer repaints
  at all.
- **A detail block taller than the window is cut rather than left to scroll**,
  with a line saying so and naming the command that prints it in full. A block
  that has scrolled puts the erase at the top of the window instead of the top
  of the block, which is the "entire terminal turns empty" failure the table
  above it already guarded against.
- **`found by group-template, dir-owner, quota-fileset` was three internal
  constants shown to a user.** Those are the values discovery puts in `--json`
  and in the state file, so they are a wire vocabulary, and this is exactly
  the mistake `category_label()` exists for. Each source now has a human
  clause and the screen reads "dirscape shows you this directory because its
  name matches your user name or one of your groups, a group you belong to
  owns it, and the filesystem's own records say you hold space in it". The
  tokens are unchanged in `--json`.
- **"mounted" and "the gpfs mount /project" are gone.** What the axis means to
  a reader is whether the storage is attached to the machine they are typing
  on, which is why `/cfs3` is there from a login node and absent from a
  compute one, and that is what the line says now.
- **`meadow3_cap:project-hpc` was device:fileset, explained nowhere.** Both
  halves now appear inside the sentence that answers the question a reader
  actually brings to a quota figure: "the figures above are your own usage
  under the quota named project-hpc on the filesystem meadow3_cap, counted by
  the filesystem itself rather than by walking this directory, so du can
  report a different number".
- **The marks on a figure are explained the first time they appear**, and only
  when they appear: `▒` says how much space and how many files the filesystem
  has handed out and not yet counted (6.7G and 1.5k files on one project
  directory here), `~` says the figure was matched to this directory rather
  than published for it.
- **Backups and deletion are on the screen**, since a researcher deciding
  where data can live asks that before anything else. Silence from a site
  reads as "not published", never as "not backed up".
- **The `writable` paragraph shrank to its conclusion.** The 157 character
  sentence about `os.access`, root-squashed exports and `W_OK` is what
  `--probe-write` and `--json` are for, and both are named at the foot of the
  view.
- **`✓ present ok` is gone**, along with `quota mmlsquota` and the `note`
  lines that restated a source label. A confirmed probe with nothing to add
  prints no line, so a screen with nothing wrong is short and a problem is the
  only thing that is loud.

### The view, redesigned against `nodetop`, and the selection band fixed

Three owner reports, and they turned out to be one problem seen from three
angles: "the highlightor gets truncated by `▎▒░░░░░░   3%` which looks very ugly",
"the ui isn't cool at all. it looks drab and boring", and "the text colors look
so weird ... you should learn from `nodetop`".

- **The selected row is one flat band again.** The cause was not the padding
  and not the embedded resets, both of which had already been fixed: it was
  that the row's own FOREGROUND colours survived inside the band, and under
  inverse video a foreground is painted as the background. So the usage bar's
  three coloured segments came out as three differently coloured tiles sitting
  on top of the selection, and the band appeared to stop at the bar. Measured
  in a pty at 110 columns: six of seven frames carried `\x1b[38;2;...m` inside
  the band before, none of nine after, and the band is one uniform 73 columns
  on every row. The row's styling is stripped now rather than re-armed, which
  costs nothing, because colour is never load bearing in this package and
  every state a row reports is still on the line in words and glyphs.
- **The usage bar is gone.** Eight cells of block characters plus the spaces to
  align them, spent on a lossy picture of the exact percentage printed
  immediately to their right, in the widest column of the table. It was also
  blank on five of the ten rows of the live view, since an unlimited quota has
  no fraction and a `statvfs` fallback has no quota, so the one thing a meter
  column exists for, being scanned down, it could not do. At 3% it drew a
  single thin glyph and at 0% it was eight cells of trough saying what `0B`
  already said. `style.bar()` and the `blocks` and `trough` glyphs went with
  it; the in-doubt marker `▒` stays on the used figure, where the fact belongs.
  Ten columns came back and went to the figures.
- **The whole view is one framed panel now**, titled, ruled and closed under the
  last row, with the pale violet diagonal gradient and the `╭ ─ ╮ │ ╰ ╯` set
  taken from `nodetop` so the two tools read as one family. The palette was
  already `nodetop`'s values verbatim; what was missing was the frame and the
  discipline of its tiers. `muted` is a measurement that is not the one the
  view was ranked by, `dim` is context, and a figure is never `dim`: so the
  role word, the directories leading up to the last one and `no limit` are
  context, a granted `rwx` and `886G free` are secondary measurements, and only
  the figures and the graded percentage are at full weight. The headings are
  lower case and indented, and the gutter went from two spaces to four, which
  is the "horizontal spacing is also an issue" report.
- **The alert teasers and the counts are behind `--summary`.** "don't show
  things like [that], it looks ugly and uninformative at all. if they want,
  they can display it when starting the app with the right flag." Three lines
  of chrome under a ten-line table, on every run, about things the reader had
  not asked about. Nothing is lost: `dirscape stranded`, `dirscape elsewhere`
  and `dirscape --all` are first-class commands that give the detail rather
  than a one-line teaser for it. Run-level warnings did NOT move there with
  them, because a malformed `site.conf` or a baseline that could not be
  written is the tool saying it could not do its job: those go to stderr, so
  they cannot become silent again and a piped table stays clean.
- **`dirscape new` prints its changes instead of counting them.** The delta
  records were built on every run and only their LENGTH was ever used, so the
  view whose entire purpose is to show changes showed a count and a pointer to
  itself. The reasons wrap onto a hanging indent rather than being cut, because
  the useful half of a `shrank` is the second half.

And two defects found while doing it:

- **Discovery's bookkeeping was leaking into the POLICY column.** The filter
  held four keys and needed ten: a 200 column run printed
  `crosses_to=['/project/hpc/jdoe42']`, `contains=2`, `free_bytes=951720603648`
  and `size_bytes=959727210496` at the user, and every one of those is already
  rendered properly elsewhere in the same view (the fold count is the `+2` on
  the path, the free figure is the `886G free` in the quota column, the
  crossing is the symlink note). It stayed unseen because POLICY is the first
  column dropped when the window is narrow, so it only appeared past about 130
  columns. This is the `rank=primary` defect again, with six more keys.
- **The interactive view printed its key hints twice**, once inside the frame
  and once below it, because the block already carried them when the footer was
  appended a second time.

`--ascii`, `NO_COLOR` and `TERM=dumb` are all pinned by tests now rather than by
docstrings: every codepoint of an `--ascii` render is under 128 and every glyph
in the table has an ASCII twin, and `NO_COLOR` or `TERM=dumb` emits not one
escape byte, frame and gradient included, with `--color always` unable to
override either.

### Fifth pass: if a reader has to ask, it goes

The owner read the finished table and asked, one by one, what `▒` was, what
`compute` and `9 devices` and `baseline 47m ago` meant, what `3%` was, and why
a path said `+2`. Having to ask is the whole answer, and the fix is deletion
rather than a legend.

- **`▒` is off the figures.** It marked GPFS space handed out and not yet
  counted, which on this home was 2.4G against 858M used, nearly three times
  the figure it qualified. A real fact in one undecodable character on every
  row. `why` now states it in a sentence, `--legend` names it, `--json` carries
  `in_doubt`.
- **`+2` is off the paths.** This reverses a fix from two rounds ago, and the
  reversal is the right way round: the count WAS being stored and never shown,
  which was a genuine defect, so it was rendered. Then a reader saw
  `/project/hpc +2` and asked what it meant, and the honest answer was "a
  number you cannot use": it names no path, and the only action is `--all`.
  It survives in `--json` and `--summary`.
- **The header is three facts**, down from six: which tool, whose quota, which
  machine. `compute` matters only when it changes, which the diff already
  refuses to do across node classes. `9 devices` was the tool finding itself
  interesting. `baseline 47m ago` is `dirscape new`'s business and that view
  prints it properly.

`3%` stayed, because a percentage explains itself.

One regression caused and fixed in the same pass. The in-doubt sentence in
`why` was conditioned on `if g.doubt in figures`, so it was tied to the mark
appearing on screen; removing the mark deleted the explanation with it, and a
fileset with 2.4G unaccounted reported that nowhere. It keys on the measurement
now. An explanation that depends on its own decoration can be deleted by
accident.

### Sixth pass: one tone per column, and width stops being spent on blanks

Four more questions off the finished table, and the last of them turned out to
be a real defect rather than a matter of taste.

- **`r-x` is the same weight as `rwx`.** The reach cell muted only the string
  `rwx`, so on a ten row table the eight ordinary rows were grey and the one
  read-only dataset was drawn in full brightness. Nothing was wrong with it.
  Every answered reach state is now one tone, and the letters carry the
  difference, which is what letters are for.
- **The path is one colour.** It was rendered as a dim parent and a bright
  leaf, so `/project2/reference` read as two facts in two greys. A path is one
  string and a reader tracks a column by its tone.
- **The percentage sits next to its numbers**, as `858M / 30G (3%)`, not
  right-hung four columns away in a cell with no heading of its own. Detached
  and unlabelled it read as a fourth number with no relationship to the three
  beside it.
- **Width is no longer spent on columns that get dropped.** WHERE reads `here`
  on every row of an ordinary run and is removed as constant; FILES is
  suppressed in the default view by policy; POLICY is `?` at a site that
  publishes no purge rules. The fitting loop measured every candidate column
  set against all seven anyway, so those three pushed each stage over budget
  until the stage that gives up ROLE, and the caller then deleted them. At an
  80 column terminal the reader lost the only informative column of the group:
  the four survivors need 67 display columns of the 76 available. The constant
  set is now excluded before fitting, so ROLE appears from 72 columns up.
- **A confirmed attachment says nothing.** `why` opened with a bare `✓` and
  "This storage is attached to the machine you are on" on a row that had just
  printed a quota figure and a write answer, which had already demonstrated it.
  That was the last unexplained mark in the view. The axis still speaks when
  the storage is NOT attached here, which is the reading it exists for.

**The frame stays sized to its content, and the alternative was measured rather
than argued.** Filling the window was tried first, because a border stopping
short of the right edge reads as a mistake. It reads worse: four columns come
to about 68 display columns, so a 120 column terminal got a box ruled out to
120 around a table hugging its left half, and the inner rule then had to choose
between spanning the frame (a rule over nothing) or spanning the table (a
second, shorter frame inside the first). There is no column set available to
fill the gap with, DEVICE being the only unused one with real content and
naming filesets like `meadow3_cap` that a reader has to ask about. Sized to the
content there is no gap and the rule spans the text area by construction. It
also keeps the static print and the interactive frame the same width, which
they were not while the two disagreed.

The README's sample output, its `▒` note and its `+2` note described a table
that no longer exists, so all three were replaced from a live run.

### Seventh pass: one column, one kind of number, and no paragraphs left

Four reports, and the first one found a genuine modelling mistake rather than
a wording problem.

- **The `space` column carried two different measurements in one shape.** It
  read `11T / no limit` on one row and `886G free` on the next, under a
  `used / quota` heading claiming both were the same thing. The owner asked
  the obvious question: "why is there no `/` in front of free? what does free
  mean here? there is no limit, but why is there also free?" The `/` was
  promising two numbers where there was one. There are three things this
  column can honestly say and each cell now names its own:

  | Cell | Means |
  | :- | :- |
  | `859M / 30G (3%)` | your usage against your quota |
  | `11T used` | your usage, with no quota set here |
  | `886G free` | the whole filesystem's headroom, shared with everyone |

  `used` and `free` are opposites, so no reader mistakes one for the other,
  and the heading is `space`, the only word true of all three.

- **Escape steps back instead of closing the program.** It decoded to QUIT on
  the reading that Escape is not a movement, so "leave" was the honest
  translation. Wrong in the one place it matters: pressing it in a detail view
  closed the whole program instead of returning to the table. It decodes to
  BACK now, and `select` resolves it against its own depth, because BACK from
  the top level would be a key that does nothing and a key that does nothing
  reads as a hung program. Escape still leaves from the root, and the detail
  view's hint names it.

- **The table fills the window.** Asked twice, so it does. `render.style.table`
  takes a `spread` flag that puts the leftover room in the gutter before the
  last column, which keeps the left group tight and right-flushes the figures
  against the frame. Dividing the slack evenly across every gutter was tried
  first and put 20 spaces between `role` and `path` at 120 columns, which no
  reader can track a row across. This supersedes the previous entry's
  reasoning for a content-sized frame: the frame is still sized to its
  content, and the content is now the window.

- **The detail view is a field list.** It was four paragraphs under the
  fields, and the owner's verdict was "this chunk of verbose text makes no
  fucking sense. it says the figures above. what figures?" Two defects in that
  one sentence. Prose that points at other lines on the screen assumes a
  reader going top to bottom, and nobody reads a detail view that way. And
  three sentences of mechanism ("counted by the filesystem itself rather than
  by walking this directory, so du can report a different number") were
  costing six lines on every path to say what naming the quota already says.
  Nineteen lines became twelve, every one of them a field:

  ```
  /project/hpc
    project directory, gpfs

    space     11T used
    files     3.1M used
    access    read + write
    quota     project-hpc on meadow3_cap, matched by name
    uncounted 6.7G, 1.5k files
    backups   not published
    found     your name or group, group ownership, the quota records

    dirscape why /project/hpc --json   --probe-write to test writing for real
  ```

  Nothing measured was dropped. The in-doubt figure became the `uncounted`
  field, the discovery sources became `found` (with `source_label(short=True)`
  supplying noun phrases instead of clauses), the symlink bullet became
  `symlinks`, and the quota's scope is still printed where it is NOT the
  reader's own usage, which is the case that contradicts what a reader
  assumes. `access` went from "you can see what is in this directory, and you
  can write to it" to `read + write`, and `backups` dropped the ten words that
  restated its own label.

A structural test rather than a word count guards it: every line of the view
is the heading, a blank, a `label value` field, or the one footer, and none of
them wraps. That is also what keeps `_browse`'s repaint arithmetic true, which
is the property a prose block broke twice.

### Eighth pass: padding is not use, and a `ds` shortcut

The previous entry's "the table fills the window" was wrong and is reversed
here. It reached the edge by stretching one gutter, which at a 126 column
terminal was a single 40 space gap between `reach` and `space`. The owner:
"a lot of space is available and unoccupied, why is there still ..." and then
"the space should be utilized well. but now it's terrible". Correct on both
counts. A reader cannot track a row across a gulf, and a box touching the
right edge bought nothing.

- **Spare width buys a real column.** `files / limit` was suppressed from the
  default view unconditionally as detail. It is suppressed only when the
  window is actually tight now, so a wide terminal gets a fifth column of
  real data instead of whitespace, and the box is sized to its content with
  whatever is left over as margin. The `spread` option is gone from
  `render.style.table` entirely rather than left switched off, because
  padding-to-fill is a rejected design and a flag for it is an invitation.
- **The interactive view truncated its last column, and that was a real
  bug.** `_browse` drew the frame itself and handed the atlas the FULL window
  to lay out in, so every line came out four columns too wide and `panel` cut
  the last cell: `865M / 30G (` and an ellipsis in the interactive view while
  the static print of the same table was correct. It had been latent since the
  frame was introduced and only surfaced once the layout used the width it was
  given.
- **That closure is now `_table_frame`, at module level.** The defect was
  invisible to a suite of 538 tests for one reason: it lived in a function
  nothing could call without driving a pty. It takes its inputs as arguments
  now, and the test asserts the CONTRACT (the size handed to the renderer
  leaves room for the border) rather than the symptom, because the symptom
  only appears at widths where the table happens to fill its budget.

**`ds` is now an entry point**, alongside `dscape` and `dirscape`. This
reverses the note that rejected it as "too generic to claim", and the check
that reversed it: the PyPI name `ds` is held by a 0.0.1 sdist of asciimoo/ds
that declares no console script, so nothing installable from there collides,
and nothing named `ds` exists in coreutils, in RHEL8, or on a login node's
PATH. That is a different situation from `dsc`, which is still rejected
because the colliding package ships a `dsc` command. `dirs` also stays
rejected: it is a bash builtin, so a script of that name never runs.

### Ninth pass: three facts, three columns, and arrows that were decoding as Escape

One cell was carrying three different measurements and the reader had to work
out which before comparing two rows. The owner, across two rounds: "simply
saying 11T used but no cap is very confusing. all the entries in space aren't
consistent at all", then "space has no /? these column names are so ugly".

**`space` and `files / limit` became `used`, `limit`, `free` and `files`.** One
word per heading, one figure per cell, every figure column right-aligned:

```
   role       path                       reach     used     limit      free    files
   home       /home/jdoe42               rwx       867M       30G       29G      37k
   project    /project/hpc               rwx        11T      none      126T     3.1M
              /scratch/local/jdoe42      rwx          ?         ?      886G        ?
```

- **`free` is the column that was missing**, and it is the one the tool is
  for: how much can I still put here. Under a quota it is the remaining
  allowance; with no quota it is the filesystem's headroom; where both are
  known it is the SMALLER, because a 40T allowance on a filesystem with 2T
  left is 2T of writes. `statvfs` is read for every root now rather than only
  for roots no backend spoke for, which is what made the column answerable on
  the six rows that had a usage figure and no cap.
- **`limit: none` and `limit: ?` stay distinct.** A backend that printed `0`
  said no quota is enforced, which is knowledge; `?` is the absence of a
  measurement. Collapsing them is the failure this package exists to avoid and
  a test now asserts all three states of that cell.
- **The percentage is gone** along with the bar before it. `free` answers what
  both were approximating, as a figure in the unit the reader acts in rather
  than a ratio to multiply back out. Fullness survives as the graded colour on
  the used figure.
- **`_align_figures` is deleted.** It right-aligned the parts of a composed
  cell on the `/` separator, which real columns do for free. It had also
  carried a silent bug: it compared a cell's trailing word against `"free"`
  without stripping colour, so every capacity row sat one column short of
  every quota row and nothing raised.
- **`why` uses the same four field names**, so opening a row no longer
  reshuffles the figures it was showing.

**The table fills the window, and this time it is not padding.** Asked for a
third time, and the two earlier answers failed for a reason upstream of the
stretching: with four columns there was nothing to spread BETWEEN, so the
leftover room became one 40 space gap. With seven columns the same room is a
few characters per gutter. The rule is ordered: spare width buys a real column
first (`files` returns when it fits), and only what remains is shared out
evenly, remainder to the rightmost gutters so the wider gaps fall between
figures rather than between the role and the path.

**Two interactive bugs, and the second was the worse one.**

- **A bare Escape did nothing.** Deciding what an `ESC` byte meant took a
  second blocking read, so Escape sat waiting for a byte that was never
  coming: the view did not move, and the key was only consumed when the
  reader pressed something else, which was then eaten deciding the first.
  Owner, twice: "esc doesn't work". The previous round's fix (Escape means
  BACK, resolved against the view's depth) was necessary and did nothing on
  its own, because the key never reached that branch.
- **Then every arrow decoded as Escape.** The obvious fix is to ask `select`
  whether more input is waiting, and paired with `sys.stdin.read(1)` that is
  wrong: a `TextIOWrapper` pulls a whole chunk off the descriptor into a
  Python-level buffer, so the kernel is asked about a descriptor whose
  remaining bytes are sitting in userspace and answers no. Measured in a pty:
  a down arrow decoded as `back` then `other`. Reading the descriptor
  directly with `os.read` keeps the two in agreement. Every key is now
  verified in a real pty: CR, bare Escape, and all four arrows.
- **The pty test that should have caught the arrows was silently vacuous.** It
  waits for a column heading before sending keys, and the heading had been
  renamed twice, so it sent nothing, spun to its 90 second deadline and passed
  on the first paint alone. It asserts that its own trigger fired now, and the
  suite went from 100 seconds back to 14.

**The hostname came off the header**, which is now `dirscape · jdoe42`. Owner:
"this meadow node needs to be shown? for what reason?" The reason is real and
is not the reader's: mounts are per node, so a SNAPSHOT has to record where it
was taken or a diff would compare a login node against a compute node.
`state.same_vantage` already enforces that by refusing to compare across node
classes, and somebody looking at their own storage on the machine they are
typing on knows which machine it is. Still in `--json`, `why`, `new` and every
snapshot record.

### Tenth pass: provenance behind `-v`, and one frame width

Owner, reading `quota home on meadow3_cap, matched by name`, `uncounted 2.4G,
1.5k files` and `found an environment variable`: "do you think these things
users can understand what they are? it makes no fucking sense."

All three were correct and none of them answered a question a researcher
arrived with. `home on meadow3_cap` is a fileset name and a device name,
`matched by name` is an attribution method, `uncounted` is a GPFS internal
(`blockInDoubt`), and `found` explains dirscape's own discovery rather than
the storage. **They are what a support ticket needs, so they are one flag
away**, behind `-v` / `--verbose`, and every one of them stays in `--json`
unconditionally. The default `why` is now eight lines of things a reader can
act on: used, limit, free, files, access, backups.

- **The symlink line survived, reworded, because it is a fact about the
  STORAGE.** A home directory whose dotfiles point into `/project` holds
  almost nothing while `du ~` reports gigabytes, and the space is charged to
  the project quota. It read `symlinks 10 into /project/hpc/jdoe42/.cache,
  /project/hpc/jdoe42/.cache/R-library, ..., billed there (dirscape tree)`:
  two full paths, an ellipsis, a passive verb and a command. It now reads
  `note  10 folders here are really stored in /project/hpc/jdoe42, and count
  against its space`, and the inventory is under `-v`.
- **A bug on the way: `os.path.commonprefix` is a CHARACTER operation.** Ten
  symlinks into `.cache`, `.conda` and friends share the characters
  `/project/hpc/jdoe42/.`, so the note named a directory with a trailing dot
  that does not exist. `_common_dir` splits on the separator first.
- **The `-v` labels are no longer than the others.** `measured by` and `not
  yet counted` are 11 and 15 characters against a 9 column label field, so
  their values sat one and six columns right of every value above them. They
  are `source`, `in doubt` and `found by`.

**Every frame in the interactive session is the window wide.** Owner: "when
going to different dirs, the ui will shrink the horizontal spacing, which is
annoying. that shouldn't change." It was a real bug: the table fills the
window, and the detail panel shrink-wrapped to whatever the opened row
happened to say, so the box jumped narrower on the way in, wider on the way
out, and to a different width for each row. The two views are one screen
replacing another in place, so a width that moves reads as the layout
breaking. Asserted across every row at three window sizes, because a
single-row test cannot see a width that varies BETWEEN rows, which is what
the reader saw.

### Eleventh pass: no derived column, one tone, and `kind`

- **`limit` came off the table, which is what makes `free` a real number.**
  The owner: "why is the free column really needed? it makes no sense. it's
  just a product of the two previous columns." On a capped row it was exactly
  `limit` minus `used`, and a table that prints a subtraction next to its own
  operands is padding with arithmetic. Of the two, `free` is the one to keep:
  it answers the question that brought the reader ("can I put 2 TB here"), it
  is the number they act on, and it is answerable on every row. `limit` was
  answerable on six rows of ten. It stays in `why` and in `--json`, and
  `used` plus `free` reconstructs it.
- **Half the question marks went with it**, from eight to four. The four that
  remain are real and now say why. Owner: "why is this place having so many
  '?'? what does it mean? you can't even get the numbers? or what?" The
  answer is literally yes, we cannot: those mounts have no quota system, so
  nothing is accounting for per-user usage and the only way to find out is to
  walk the tree, which this tool does not do at any price. `why` says that in
  one line on exactly the rows that have it, and points at `du` or `rdu`.
- **The `used` column is one tone.** It was tinted by fullness where a
  fraction existed and muted where none did, so `866M` (3% of a 30G quota)
  was a graded blue beside `11T` (uncapped, so no fraction) in grey. Owner:
  "in the used column, both entries have different colors. which is shit."
  Correct, and structurally rather than as a matter of taste: a grading only
  half the rows can carry is not a scale, it is two categories a reader has
  to decode before comparing two numbers. `free` carries "how much room is
  left" as a figure on every row, which is what the grading approximated.
- **`role` became `kind`.** Owner: "the word role is poorly chosen." The
  column holds `home`, `project`, `scratch`, `dataset`, `software`, `local`,
  which is what sort of place each row is; nobody asks what role their
  scratch directory plays. `--json` still carries `role`, because a consumer
  may switch on it, which is the same wire-versus-display split as
  `CATEGORY_LABELS`.

**The pty test's trigger went stale for the THIRD time, and it is now
anchored on something that cannot rot.** It waits for a marker in the output
before sending keys, and that marker was a column heading: `used / quota`,
then `space`, then `limit`. Each rename broke it silently in the worst
possible way, because no keys are sent, the loop spins to its 90 second
deadline, and the assertions pass on the first paint alone, so the suite went
from 14 seconds to 100 while claiming to test a drill-down it never
performed. It waits for the key hint line now, which is the interactive
contract rather than a label choice, and the `pytest.skip` guard keys on the
cursor-hide escape for the same reason. Column headings are precisely the
thing this project keeps rewording.

### Twelfth pass: `access`, and the derived column is gone for real

- **`reach` became `access`.** Owner: "reach doesn't make sense either." It
  was the model's word for a tri-state (listable / traverse-only / closed)
  and it leaked onto a heading, which is the mistake `role` made one round
  earlier. A reader asks what they can DO here, and `why` has labelled that
  field `access` all along, so the table and the detail view now use one word
  for one thing. `Reach` stays the type's name and `--json` still carries
  `reach`.
- **`free` is off the table, and this time the right half was dropped.** The
  previous round read the objection ("it's just a product of the two previous
  columns") as a choice between `limit` and `free` and kept the wrong one.
  The owner said so plainly: "why is free column still there. makes no
  sense." `used` and `limit` are what a quota backend measures and prints.
  `free` was a subtraction this view performed and then displayed next to its
  own operands, which is the same reason there is no percentage column: a
  table shows what was measured and a reader can take a difference.

  **The cost is real and accepted.** On a mount with no quota system both
  cells read `?`, and the filesystem's own headroom that `free` used to show
  is no longer on the table. It was never a per-user figure, so it never sat
  honestly beside two that are. It is in `why`, labelled, and in `--json`,
  and `why` states in one line why the two cells cannot be filled: nothing is
  counting, and finding out means walking the tree.

The default view is now `kind`, `path`, `access`, `used`, `limit`, `files`:
six columns, every heading one word, every figure cell one token, and nothing
on it computed from anything else on it.

### Thirteenth pass: answering the question marks instead of explaining them

Owner: "regarding ?, do you have a way to tell the exact number? having too
many ? can impact user experience, and they will think you don't know things."

Eight `?` cells on the live table, and they were not all the same problem.
Taken one at a time, three of the four rows are now answered.

- **One was an attribution BUG, not a limit of what the filesystem knows.**
  `/scratch/meadow2/jdoe42` read `?` for both figures while
  `mmlsquota -u jdoe42 meadow2_perf` reports 0 used against a 100G quota and a
  5T hard limit. `mmlsattr -L` calls the path's fileset `root`, which is
  GPFS's name for a filesystem's own top level, and the backend labels a
  device-wide USR row with the DEVICE, so `_rows_governing` compared `root`
  against `meadow2_perf` and matched nothing. `_device_wide` joins them, under
  four conditions that keep it from becoming the quota leak it sits beside:
  the root must have no fileset of its own, and the row must be user-scoped,
  device-wide and on the same device. A user quota on a device covers that
  user everywhere on it, so this is correct rather than convenient, and a
  caveat records that the figure spans the whole device.
- **Two were `noquota` mounts, where nothing in the kernel is counting.**
  `/tmp` and `/scratch/local` are XFS mounted `noquota`, so there is no
  per-user accounting to ask for and the only source of truth is adding the
  files up. **`--measure` does that**, and it is opt-in rather than default,
  because the package's headline claim is that its cost is the number of
  roots and not the number of files. Each root gets a 3 second ceiling, and a
  walk that runs out leaves the `?` in place with a reason: a partial sum
  reported as a total is worse than no number. `st_blocks`, so the figure is
  space charged and comparable with a quota reading, and symlinks are never
  followed, so a home directory whose dotfiles live in `/project` is not
  double counted.
- **And the `limit` cell on those rows now reads `none`, with no flag at
  all.** `noquota` in the mount options is positive evidence that no limit is
  enforced, which is a different fact from nobody having measured one, and
  the renderer has always kept those apart. `attribute_xfs` already detected
  it and recorded it only as a note, so the table was withholding something
  the mount table had settled. One direction only: the absence of `noquota`
  implies nothing.

The default view is down to four `?` cells from eight, each with a one-line
explanation in `why` naming `--measure`; with `--measure` there are none.

**Two bugs found while building it.**

- **`--measure` walked everything and produced nothing.** The first version
  iterated every discovered root, so the candidates included
  `/scratch/meadow3`, `/project2/reference/pdb` and `/project2/biokit`: whole
  shared dataset trees that the default view does not show, each burning its
  full deadline on a partial that was then correctly discarded. It took 8.7
  seconds and filled in zero cells. It walks the shown rows only, and takes
  its own clock rather than the global budget's leftovers, which by that point
  in the run is near zero and set every deadline to the current instant.
- **The POLICY column's leak guard was a blacklist, and it rotted again.** It
  names the keys discovery stores on `root.policy` as bookkeeping; it started
  at `rank`, was found short by six, and went short by two more the moment
  `_device_wide` and `_measure` each set a flag, which put
  `device_wide_quota=True` in the POLICY cell of a live run. A blacklist has
  to be updated by whoever adds a key, which is the wrong person to rely on,
  so the root's side of `merged_policy` is now an ALLOWLIST of the vocabulary
  `sitecfg` documents. The site's own side stays unfiltered, because an
  administrator is meant to be able to publish a key this package has never
  heard of.

### Fourteenth pass: words instead of mode bits, and no question marks left

- **The measuring walk is on by default, bounded at three ends.** It was
  opt-in behind `--measure`, and the owner's reaction to the two `?` rows that
  remained was "why are there still '?'. something is wrong." That is the
  right reaction and a flag is a bad answer to it: a reader cannot tell
  "nobody could measure this" from "this tool did not try". The default view
  now has no `?` on this cluster.

  The package's claim that its cost is the number of roots and not the number
  of files still holds, because the walk only touches roots no backend could
  answer for and gives up rather than running: `WALK_SECONDS` per root,
  `WALK_TOTAL_SECONDS` for the whole pass so four slow roots cannot cost four
  times one, and `WALK_ENTRIES`, which is the bound that actually protects a
  login node. Measured against `/software`, the worst tree here: it abandons
  in 2ms at a ceiling of 50 and burns the full 1.5s deadline at 100k without
  finishing. Any bound tripping leaves the `?` in place with a reason, because
  a partial sum reported as a total is worse than no number. `--no-measure`
  turns it off.

  **A bug in that bound, found by a test with teeth.** The entry count was
  checked only at the top of the loop, so a single directory holding more
  entries than the ceiling was never caught: the first pass starts at zero,
  scans the whole thing, and finds an empty stack. One flat directory with
  millions of entries is the realistic shape for a scratch or `/tmp` tree, so
  it was the case the bound most needed to catch and the one case it missed.

- **`rwx` became `read + write`.** Owner: "since we have a lot of horizontal
  spacing, don't use rwx, just use regular words so that it's new user
  friendly. utilize the space optimally." The POSIX triple was compact, exact
  and addressed to somebody who already reads `ls -l`. The distinction it
  existed for is preserved and is the one a word form could quietly lose:
  `read` means nobody checked whether you can write, which is not `read only`.
  Both views draw from `fields.access_words` now, because they had drifted
  into two vocabularies for the one thing both are for: the table said `rwx`
  and `why` said "you can see what is in this directory, and you can write to
  it".

- **`used` and `limit` say whose they are.** Owner: "what does limit mean?
  does it mean there is no user level limit or the dir has some ceiling but
  there is no restriction on the user side?" A fair question with no answer on
  screen, and the ambiguity was real rather than a wording slip:
  `QuotaRow.scope` is `user`, `group` or `fileset`, so the same cell can be a
  personal allowance or the ceiling on everything in a directory. Every row of
  this cluster's default view is user-scoped, so the headings read `your use`
  and `your limit`. **The claim is checked against the rows rather than
  assumed**: if any row on screen is group or fileset scoped they fall back to
  `used` and `limit` and `why` names the scope, because one wrong heading is
  worse than a vague one and this package must not tell a site nobody has an
  account on a lie about its own quotas.

- **`--legend` describes the table that exists.** It explained `r`, `w`, `x`
  and `-`, which the access column stopped using, and the in-doubt block,
  which came off the figures two rounds earlier. A legend for a view that has
  moved on is worse than none: a reader who cannot find the character it
  describes has to work out whether they are in the wrong column or reading
  stale documentation.

### Fifteenth pass: opening a row shows what is inside it

**Every figure on the default view was verified against the site's own tools
before anything else changed**, because the owner asked for that first. They
all match. `mmlsquota -u jdoe42 meadow3_cap` reports 867.2M against a 30G
quota and 36,837 files for `home`, 10.98T and 3,135,427 files for
`project-hpc`, 314.2G for `software`; the site wrapper agrees on all of them
and on the four scratch filesets. The `quota` column shows the SOFT quota
(30G) rather than the hard limit (35G), which is what the site's own tool
puts under the same heading and what a user is actually held to.

One thing the check did turn up: `none` is true of the user's cap and not of
the directory. `/project/hpc` carries a GROUP quota of 202.34T with 77.15T
used, which only the site wrapper reports and which the table does not show.
Both figures are right; the ambiguity was the heading, dealt with below.

- **Enter lists the directory's children, and keeps going.** Owner: "when
  zooming into each main dir, there should be all the sub-dirs shown just
  like the main ui and you can constantly zoom in if there is sub dirs within
  these sub-dirs." It printed that one root's figures as a field list, which
  answers "tell me about this directory" rather than "what is inside it":
  "this is weird. i don't need to know this kind of info." The field list is
  still `dirscape why <path>`, and the listing's footer says so.

  Name, access and a direct-entry count, from one `scandir` plus one `stat`
  per child. No size per child: that would mean walking each subtree, which
  for a `/project/hpc` holding 11T is the operation this package exists to
  avoid. Files are left out, because a home with 300 dotfiles would bury the
  four directories a reader is descending towards.

  **It scrolls.** The first version rendered every child, and `/project/hpc`
  has 84 while `/project` has 668, so the block was many times the height of
  the terminal: the frame was truncated to fit, which cut the rows off the
  bottom, which left the selection band with nothing to land on so it never
  painted at all. A listing that cannot show its own selection is not a
  listing. It now returns a window of rows around the cursor and the index of
  the highlighted row within it, so the caller never has to guess where the
  rows begin. Nine rows of chrome plus the one `select` leaves the cursor on,
  counted against a rendered block rather than estimated: the first guess was
  eight and produced 31 lines in a 30 row terminal.

- **`used` and `quota`**, replacing `your use` and `your limit`, which lasted
  one round: "it sounds cheap". Fair. A heading that has to insist whose
  number it is reads like a label apologising for its column, and the frame
  says `jdoe42` two lines above. `quota` is also the word the site's own tool
  prints over the same figure.

- **`why` reports the inode ceiling.** Owner: "does file usually have limit?"
  On this cluster, yes, and it is the one people forget: a home allows
  300,000 files against 30G of space, so a tree of small files exhausts the
  count long before the bytes and the error when it happens says nothing
  about files. The table has room for the count; the ceiling is a field in
  `why`, and `why`'s `limit` field became `quota` to stop the two views
  naming one fact two ways.

The pty repaint test changed what it measures, and the reason is worth
keeping. It counted cursor-up sequences and required exactly three, on the
grounds that the inner view was static so Down should repaint nothing. Down
now moves through a listing and repainting is the point. What still signals
the original defect is a block occupying more rows than the repaint
arithmetic counts, so it asserts that every repaint in the session moves up
by fewer lines than the window has rows.

### Sixteenth pass: a confident zero, and the last of the question marks

**The walk reported `0B` for a directory of small files on GPFS, and that is
the worst failure in this codebase's vocabulary: not an error, not a `?`, a
confident wrong number.** `st_blocks` is the right unit, because it is space
CHARGED and therefore comparable with a quota reading, but GPFS returns
`st_blocks == 0` for a file small enough to live in its inode and with a 4 MiB
block size that is most small files. Measured: a 4096 byte file is
`st_size=4096, st_blocks=0` on GPFS and `st_size=4096, st_blocks=8` on XFS.
Zero blocks against a non-empty file means the bytes are somewhere the block
count cannot see them, so the walk falls back to `st_size` there. Sparse files
keep their block figure, because theirs is non-zero.

**Every figure on the default view was re-verified against `mmlsquota -u` on
all five devices**, row by row, including the file counts: 867.5M / 30G /
36,849 for home, 10.96T / 3,143,273 for `project-hpc`, 314.2G / 2,608,466 for
`software`, 22.88T / 32,609 for `project2-reference`, 928K / 66 for
`project2-hpc`, and the four scratch filesets. All ten rows match.

- **`dirscape tree` had two defects in one node**, both from walked figures
  being keyed on the directory's own path as a fileset name. `/tmp` and
  `/scratch/local/jdoe42`, two mounts of one `/dev/sda1`, became two nodes
  with conflicting names and the view fell back to `?` for the device; and the
  figure shown was the first member's rather than the group's, so a device
  holding 1.2G read `0B used`. A walk measures a DIRECTORY and not a quota
  scope, so it names no fileset, walked members are summed, and an absent
  fileset is labelled for what it is. `?` is reserved for "nobody could
  measure it" and a filesystem with no quota scopes is not that.

- **Letting `--all` walk its extra roots was tried and reverted.** It took
  that view from 2.9s to 15s and removed one question mark out of 144, because
  the deadline cannot interrupt a single `scandir`: one call against a GPFS
  directory with hundreds of entries, each needing a `stat`, runs for seconds
  before the clock is looked at again. The bounds hold only at the level they
  are checked, and that level is coarse, so the policy is not to start.

  What stays unmeasured in `--all` is filesystem roots and aliases (`/`,
  `/home`, `/project`, seven `/gpfs/*`) and other people's project
  directories. None has a per-user quota to report, so `?` there is the
  correct answer rather than a gap: the number does not exist, and the only
  way to invent one is the tree walk this package refuses.

Question marks per view, on this cluster: the default table, `--summary`,
`tree`, `stranded`, `elsewhere` and `new` have none. `matrix` has one, in the
line that defines the symbol. `--all` keeps them, for the reason above.

### Seventeenth pass: a text hierarchy, and colour spent where it counts

The layout was kept, as asked. What changed is the tier every element sits in,
and the diagnosis came from reading how current TUIs are built rather than
from taste: design the text tiers first, treat colour as a resource rather
than a paintbrush, keep one accent plus semantic states, and never encode
meaning in colour alone.

**The view was drab for one measurable reason: everything was in the same two
greys.** Every figure in every column was `muted` (248), the same tier as the
labels beside them, and the palette's primary tier (`text`, 253) was reached
for by nothing but a bold title. Ten rows of numbers with no hierarchy for the
eye to use. The border was worse: nodetop's frame anchors sit near L* 84,
which is BRIGHTER than this package's own primary text, so the box was the
lightest thing on screen and the figures inside it were dimmer than the
chrome around them.

Four tiers now, and each one does a job:

| tier | 256 | what is in it |
| :- | -: | :- |
| content | 253 / 111 | the path, and every figure's magnitude |
| context | 248 | the access phrase, the notes |
| chrome | 244 | column headings, the `kind` label, units, `none` |
| structure | 237 / 61-97 | the inner rule and the border |

- **A figure is two tiers in one cell**, magnitude then unit: `868` in the
  content tier and `M` one below. Taken from nodetop's own output, which
  renders a numerator in saturated blue and its denominator in muted grey, so
  the two tools now read as one family rather than merely agreeing on a
  layout. One tone across the column and no ramp: nodetop can grade its blue
  because every row of `cores free` has a fraction to grade by, and `used`
  has no denominator on half these rows. A grading only some rows can carry
  is the defect this package has already been through twice.
- **An absence is chrome.** `none` was landing in the content tier, which
  made "there is no number here" the brightest thing in a column of numbers.
- **The border came down about forty points of lightness** and kept its hue
  sweep, so it reads as structure behind the content instead of a frame in
  front of it. The inner rule went with it: `dim` at 244 across the full
  width, brighter than the box containing it, read as a second heading.
- **Column headings dropped a tier too.** Bold at `muted` was chosen when the
  whole table was muted and it was the only way to separate a heading from
  its column; with content at 253 it read as loud as the numbers it labels.

Applied to every view, because four of them were still rendering in the
terminal's default foreground, which is not a decision but whatever tone the
surrounding shell happens to use: the `why` field list (labels were as bright
as the figures they introduce), the directory listing, `matrix`'s path column,
and `tree`, which was still composing figures through `quota_cell`, the
single-cell form with a percentage baked in that the table gave up two rounds
ago. `tree` also read `11T of none`, which is not a sentence.

Two tests pin this so it cannot drift back: one asserts the magnitude and unit
are different tiers, that an absence is chrome, and that `plain()` is
unchanged either way; the other paints every role in the palette at 4, 8 and
24 bits, because the depths are independent and a role added for its truecolor
value and never checked at 4 bits is a role that vanishes on a `TERM=linux`
console.

Sources for the design guidance: the terminal-renaissance write-up on tiered
palettes and semantic colour slots, and nodetop's measured output.

### Known limits

- **Nothing can be called new on the first run**, and the tool says so instead
  of labelling everything new. Newness comes from its own snapshot lineage
  because the filesystem cannot supply it: birth time is unavailable on GPFS
  (`stat -c %W` returns 0, `%w` returns `-`, Python's `st_birthtime` is absent),
  and directory `mtime` is a decoy. `/project/aarnold`'s fileset first appears
  in the site quota archive on 2026-03-13 while its directory mtime reads
  2026-05-15, two months late.
- **Lustre has run live only on ACME Procyon and Sylvia.** No Lustre is
  mounted on the development cluster, so the backend's tests replay recorded
  fixtures, and the fixes under "Fixed by running on two other clusters" are
  what those live runs found.
- **Write access comes from `os.access`**, which a root-squashed NFS export can
  answer wrongly. `--json` carries that caveat with the answer, and
  `--probe-write` settles it by writing. Only for uid 0, the one identity such
  an export remaps, does the table read `read` instead of answering.
