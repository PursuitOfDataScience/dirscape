<h1 align="center">dirscape</h1>

<p align="center">
  <strong>Where can I put my data on this cluster, and what changed?</strong><br>
  GPFS &middot; Lustre &middot; XFS &middot; NFS &middot; anything with a mount table
</p>

You land on a new cluster. Where does 2 TB go? What gets purged on Friday? Nobody hands
you that list, and `du` cannot find it because `du` needs the path you are missing.

```
$ ds

╭────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────╮
│ dirscape  ·  jdoe42                                                                                                        │
│                                                                                                                            │
│ ────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────── │
│    role              path                              reach           used           limit           free           files │
│    home              /home/jdoe42                      rwx             867M             30G            29G             37k │
│    project           /project/hpc                      rwx              11T            none           126T            3.1M │
│                      /project2/hpc                     rwx             928K            none           766T              66 │
│    scratch           /scratch/collie3/jdoe42           rwx               0B            400G           400G               7 │
│                      /scratch/local/jdoe42             rwx                ?               ?           886G               ? │
│                      /scratch/meadow2/jdoe42           rwx                ?               ?            25T               ? │
│                      /scratch/meadow3/jdoe42           rwx              22G            100G            78G            3.7k │
│    dataset           /project2/reference               r-x              23T            none           766T             33k │
│    software          /software                         rwx             314G            none           1.5P            2.6M │
│    local             /tmp                              rwx                ?               ?           886G               ? │
╰────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────╯
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
ds                            # no flags, no config, no setup
```

`ds` is the short name; `dscape` and `dirscape` run the same thing.

## 🔍 Going deeper

| Command | Answers |
| :- | :- |
| `ds new` | What changed since the last run |
| `ds stranded` | Space you hold in filesets you can no longer reach |
| `ds elsewhere` | Allocations with no path on this node |
| `ds why <path>` | One path as a field list; add `-v` for where each figure came from |
| `ds --all` | Every root, including aliases and filesystem roots |
| `ds matrix` | Yes / no / **could not determine**, per capability |
| `ds tree` | Which filesets share a device, and symlinks that cross a quota |
| `ds --json` | The same facts, with a reason code on every unknown |

## ⚠️ What will bite you

| | |
| :- | :- |
| **Nothing is "new" on the first run.** | There is no baseline yet, and it says so rather than calling everything new. Run it twice. |
| **Rows get folded.** | If the whole of a tree is yours, one row says so and the rest are held back. `--all` lists them. |
| **`free` is not always yours alone.** | Under a quota it is your remaining allowance. With no quota it is the whole filesystem's headroom, shared with everyone on the node. Where both are known it shows the smaller, because that is what you can actually write. |
| **`limit: none` is not `limit: ?`.** | `none` means the filesystem told us no quota is enforced here. `?` means nobody could measure it. |
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
