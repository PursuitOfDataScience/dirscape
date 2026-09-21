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

╭───────────────────────────────────────────────────────────────────────────╮
│ dirscape  ·  jdoe42  ·  meadow3-0200  ·  compute  ·  9 devices            │
│                                                                           │
│ ───────────────────────────────────────────────────────────────────────── │
│    role        path                       reach    used / quota           │
│    home        /home/jdoe42               rwx      854M▒ / 30G         3% │
│    project     /project/hpc +2            rwx       11T▒ / no limit       │
│                /project2/hpc              rwx       928K / no limit       │
│    scratch     /scratch/collie3/jdoe42    rwx         0B / 400G        0% │
│                /scratch/local/jdoe42      rwx       886G free             │
│                /scratch/meadow2/jdoe42    rwx        25T free             │
│                /scratch/meadow3/jdoe42    rwx        22G / 100G       22% │
│    dataset     /project2/reference        r-x        23T / no limit       │
│    software    /software                  rwx       314G / no limit       │
│    local       /tmp                       rwx       886G free             │
╰───────────────────────────────────────────────────────────────────────────╯
```

One box, one row per place you can put data, and nothing else. The counts and the
teasers are behind `--summary`; every other view has its own command below.

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
| `dirscape --legend` | Explain the reach letters and the figure marks |
| `dirscape --summary` | Add the stranded, elsewhere and hidden-row counts under the table |

## ⚠️ What will bite you

| | |
| :- | :- |
| **Nothing is "new" on the first run.** | There is no baseline yet, and it says so rather than calling everything new. Run it twice. |
| **`+2` means rows were folded.** | If the whole of a tree is yours, one row says so and the count is what it absorbed. `--all` lists them. |
| **`886G free` is not your usage.** | Where no quota exists, `statvfs` reports the whole filesystem's headroom, shared with everyone on the node. A quota figure reads `used / limit` instead. |
| **`▒` means space in doubt.** | GPFS has allocated it and not yet accounted for it, which is why a `du` walk will legitimately disagree. |
| **`?` never means zero.** | It means the tool could not find out, and it never becomes a number, a blank or a `0%`. |
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
