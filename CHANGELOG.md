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
