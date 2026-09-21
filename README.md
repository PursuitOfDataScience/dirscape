<h1 align="center">dirscape</h1>

<p align="center">
  <strong>Where can I put my data on this cluster, and what changed?</strong>
</p>

<p align="center">
  GPFS &middot; Lustre &middot; XFS &middot; NFS &middot; anything with a mount table
</p>

You land on a new cluster. Where does 2 TB go? What gets purged on Friday? Why can your
collaborator not read your files? Nobody hands you that list, and `du` cannot find it
because `du` needs the path you are missing.

```
$ dirscape

meadow3-0200  ·  login  ·  9 devices  ·  2.8s  ·  baseline 3 days ago

ROLE     PATH                     REACH  USED / QUOTA
───────  ───────────────────────  ─────  ─────────────────────────────
home     /home/jdoe42             rwx     836M / 30G     ▎░░░░░░░   3%
project  /project/hpc/jdoe42      rwx      11T / no limit
         /project2/hpc            r-x     928K / no limit
scratch  /scratch/meadow3/jdoe42  rwx      22G / 100G    █▊░░░░░░  22%
         /scratch/collie3/jdoe42  rwx       0B / 400G    ░░░░░░░░   0%
dataset  /project2/reference      r-x      23T / no limit

  ▲ 5 filesets hold 18G you cannot reach   dirscape stranded
  ▲ 6 allocations with no path here        dirscape elsewhere
  ▲ 1 change since the baseline            dirscape new

  52 hidden (--all) · 3 unmeasured (dirscape why <path>)
```

Nine lines of table. Everything else is one line with a command to see more.

**In a terminal it is interactive**, like `nodetop`: arrows or `jkl` move a highlight down
the rows you are already looking at, Enter opens the one you want, `q` leaves. Piped,
redirected or under `--json` it prints the table above and nothing else.

## 🚀 Install

```bash
pip install dirscape          # zero dependencies, Python 3.6+
dirscape                      # no flags, no config, no setup
```

## 🔍 Going deeper

| Command | Answers |
| :- | :- |
| `dirscape new` | What changed since the last run |
| `dirscape stranded` | Space you hold in filesets you can no longer reach |
| `dirscape elsewhere` | Allocations with no path on this node |
| `dirscape why <path>` | Every probe it ran on one path, and what each said |
| `dirscape --all` | Every root, including aliases and filesystem roots |
| `dirscape matrix` | Yes / no / **could not determine**, per capability |
| `dirscape tree` | Which filesets share a device, and symlinks that cross a quota |
| `dirscape --json` | The same facts, with a reason code on every unknown |
| `dirscape --legend` | Explain the reach letters and the bar marks |

## ⚠️ What will bite you

| | |
| :- | :- |
| **Nothing is "new" on the first run.** | There is no baseline yet, and it says so rather than calling everything new. Run it twice. |
| **`?` never means zero.** | It means the tool could not find out. A quota it could not read is never drawn as an empty bar. |
| **Mounts depend on the node.** | `/cfs3` exists on login nodes and not on compute. The header states where it ran, and it refuses to diff across node classes. |
| **It never walks a tree.** | Sizes come from quota backends, so it finishes in seconds. For bytes per directory use `rdu` or `ncdu`. |
| **Write access is not probed by default.** | `os.access` lies under root-squashed NFS. Pass `--probe-write` to find out for real. |

## 🛠️ For site administrators

One file at `/etc/dirscape/site.conf` gives every user on the cluster correct labels,
purge warnings and quota backends with no flags. It grants and denies nothing: access is
always measured with `os.access` at runtime.

```bash
dirscape --site-template > /etc/dirscape/site.conf   # commented starting point
```

MIT licensed.
