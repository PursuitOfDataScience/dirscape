<h1 align="center">dirscape</h1>

<p align="center">
  <strong>Every storage root you can actually reach on a cluster, and what is new since last time.</strong>
</p>

<p align="center">
  GPFS &middot; Lustre &middot; XFS &middot; NFS &middot; anything with a mount table
</p>

You land on a new cluster. Where can you put 2 TB? What gets purged on Friday? Why can
your collaborator not read your data? Nobody hands you that list, and `du` cannot find it
because `du` needs the path you are missing.

```
$ dirscape

 dirscape · meadow3-0200 · login node · 6 devices · 0.31s · baseline 7d ago

 ROLE      PATH                      WHERE      REACH  USED / QUOTA         POLICY
 home      /home/jdoe42              here       rwx    ▏      838M / 30G    backed up
 project   /project/hpc              here       rwx    ███▊   77.1T / 202T   backed up
 project   /project2/hpc             here       rwx    ██     199T / 500T    backed up
 scratch   /scratch/meadow3/jdoe42   here       rwx    ██▏    22.1G / 100G   purge
 dataset   /project2/reference       here       r-x    ?          ?          read-only
 software  /software                 here       rwx    ?      329G          no quota
 archive   /cfs3/kestrel-lab         ELSEWHERE  ?      █████████ 155T / 165T login nodes only
 archive   /cfs4/hpc-staff           ELSEWHERE  ?      ▏      27.9M / 25T    allocated, not mounted

 NEW SINCE 2026-09-14
 + project  /project/aarnold    new       created 2026-03-13
 ! project  /project/dahlias    stranded  you hold 11.7G here and can no longer list it

 3 roots have no quota backend. `dirscape why /software` explains each.
```

## 🚀 Install

```bash
pip install dirscape          # zero dependencies, Python 3.6+
dirscape                      # no flags, no config, no setup
```

## 🔍 What else it does

| Command | Answers |
| :- | :- |
| `dirscape new` | What changed since the last run |
| `dirscape why <path>` | Every probe it ran on one path, and what each said |
| `dirscape matrix` | A grid of yes / no / **could not determine** per capability |
| `dirscape tree` | Which filesets share a device, and which symlinks cross a quota |
| `dirscape --json` | The same facts, with a reason code on every unknown |
| `dirscape --ncdu /path` | Hand a root to `ncdu` or `gdu` for browsing |

## ⚠️ What will bite you

| | |
| :- | :- |
| **Nothing is "new" on the first run.** | There is no baseline yet, and it says so rather than calling everything new. Run it twice. |
| **`?` never means zero.** | It means the tool could not find out. A quota it could not read is never drawn as an empty bar. |
| **Mounts depend on the node.** | `/cfs3` exists on login nodes and not on compute. The header states where it ran, and it refuses to diff across node classes. |
| **It never walks a tree.** | Sizes come from quota backends, so it finishes in under a second. For bytes per directory use `rdu` or `ncdu`. |
| **Write access is not probed by default.** | `os.access` lies under root-squashed NFS. Pass `--probe-write` to find out for real. |

## 🛠️ For site administrators

One file at `/etc/dirscape/site.conf` gives every user on the cluster correct labels,
purge warnings and quota backends with no flags. It grants and denies nothing: access is
always measured with `os.access` at runtime.

```bash
dirscape --site-template > /etc/dirscape/site.conf   # commented starting point
```

MIT licensed.
