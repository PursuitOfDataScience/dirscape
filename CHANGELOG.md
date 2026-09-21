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
anywhere), an ext4 site, a closed directory, and every view rendered on each.
The load-bearing one runs in STRICT mode, where a backend reaching for a tool
the fixture never recorded raises instead of guessing: on a real foreign
cluster that guess is a wrong answer nobody can see.

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
