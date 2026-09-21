<h1 align="center">dirscape</h1>

<p align="center">
  <strong>Where can I put my data on this cluster, and what changed?</strong><br>
  GPFS &middot; Lustre &middot; XFS &middot; NFS &middot; anything with a mount table
</p>

You land on a new cluster. Where does 2 TB go? What gets purged on Friday? Nobody hands
you that list, and `du` cannot find it because `du` needs the path you are missing.

```
$ dirscape

╭──────────────────────────────────────────────────────────────────────────────────────╮
│ dirscape  ·  jdoe42  ·  meadow3-0200                                                 │
│                                                                                      │
│ ──────────────────────────────────────────────────────────────────────────────────── │
│    role        path                       reach                    space             │
│    home        /home/jdoe42               rwx                      859M / 30G (3%)   │
│    project     /project/hpc               rwx                       11T used         │
│                /project2/hpc              rwx                      928K used         │
│    scratch     /scratch/collie3/jdoe42    rwx                        0B / 400G (0%)  │
│                /scratch/local/jdoe42      rwx                      886G free         │
│                /scratch/meadow2/jdoe42    rwx                       25T free         │
│                /scratch/meadow3/jdoe42    rwx                       22G / 100G (22%) │
│    dataset     /project2/reference        r-x                       23T used         │
│    software    /software                  rwx                      314G used         │
│    local       /tmp                       rwx                      886G free         │
╰──────────────────────────────────────────────────────────────────────────────────────╯
```

One box, one row per place you can put data, and nothing else. **In a terminal it is
interactive**, like `nodetop`: arrows or `jk` move the highlight, Enter opens a row, `q`
leaves, `esc` steps back out of a row. Piped or redirected it prints the table above and
stops. Counts and teasers are behind `--summary`.

Sizes come from quota backends, never a tree walk, so it finishes in seconds. For bytes
per directory use `rdu` or `ncdu`.

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
| `dirscape why <path>` | One path as a field list: quota source, access, policy |
| `dirscape --all` | Every root, including aliases and filesystem roots |
| `dirscape matrix` | Yes / no / **could not determine**, per capability |
| `dirscape tree` | Which filesets share a device, and symlinks that cross a quota |
| `dirscape --json` | The same facts, with a reason code on every unknown |

## ⚠️ What will bite you

| | |
| :- | :- |
| **Nothing is "new" on the first run.** | There is no baseline yet, and it says so rather than calling everything new. Run it twice. |
| **Rows get folded.** | If the whole of a tree is yours, one row says so and the rest are held back. `--all` lists them. |
| **The `space` column says which kind of number it is.** | `859M / 30G (3%)` is your usage against your quota. `11T used` is your usage where no quota is set. `886G free` is the whole filesystem's headroom, shared with everyone on the node, and not yours. |
| **`?` never means zero.** | It means the tool could not find out, and it never becomes a number, a blank or a `0%`. |
| **Mounts depend on the node.** | `/cfs3` exists on login nodes and not on compute. The header states where it ran, and it refuses to diff across node classes. |
| **Write access is not probed by default.** | `os.access` lies under root-squashed NFS. Pass `--probe-write` to find out for real. |

## 🛠️ For site administrators

One file gives every user on the cluster correct labels, purge warnings and quota
backends with no flags. It grants and denies nothing: access is always measured with
`os.access` at runtime.

```bash
dirscape --site-template > /etc/dirscape/site.conf   # commented starting point
```

MIT licensed.
