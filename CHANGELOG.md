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
