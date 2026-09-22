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
│    kind                path                               access                  used             quota             files │
│    home                /home/jdoe42                       read + write            868M               30G               37k │
│    project             /project/hpc                       read + write             11T              none              3.1M │
│                        /project2/hpc                      read + write            928K              none                66 │
│    scratch             /scratch/collie3/jdoe42            read + write              0B              400G                 7 │
│                        /scratch/local/jdoe42              read + write              0B              none                 0 │
│                        /scratch/meadow2/jdoe42            read + write              0B              100G                 1 │
│                        /scratch/meadow3/jdoe42            read + write             22G              100G              3.7k │
│    dataset             /project2/reference                read only                23T              none               33k │
│    software            /software                          read + write            314G              none              2.6M │
│    local               /tmp                               read + write            1.2G              none              2.2k │
╰────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────╯
```

One box, one row per place you can put data, and nothing else. **In a terminal it is
interactive**, like `nodetop`: arrows or `jk` move the highlight, Enter opens a row, `q`
leaves. **Enter opens a row and lists what is inside it**, and you can keep going down as
far as the tree goes; `esc` comes back up. Piped or redirected it prints the table above
and stops. Counts and teasers are behind `--summary`.

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
| **`used` and `limit` are what the filesystem reported.** | Nothing is derived. There is no free column and no percentage: take the difference yourself if you want it. |
| **`used` and `quota` are YOUR figures.** | Your usage and your personal cap. A directory can also carry a group quota that is not shown here: `ds why <path>` names the scope of whatever governs it. |
| **Files have a quota too.** | A home here allows 300,000 files against 30G, so a tree of small files runs out of inodes long before bytes. The table shows the count; `ds why <path>` shows the ceiling. |
| **`read` is not `read only`.** | `read` means nobody checked whether you can write. `os.access` lies under root-squashed NFS, so pass `--probe-write` to settle it by writing a file. |
| **A `?` is never a zero.** | It means nobody could measure it. Roots with no quota system are added up by a bounded walk instead; if the tree is too big to count quickly the `?` stays rather than being reported short. `--no-measure` skips the walk. |
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
