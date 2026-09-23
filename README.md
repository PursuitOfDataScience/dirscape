<div align="center">

# 🗺️ dirscape

**Every place you can put data on a cluster, how full it is, and what changed since last time.**

GPFS · Lustre · CephFS · XFS · NFS · anything with a mount table

<a href="https://github.com/PursuitOfDataScience/dirscape/actions/workflows/ci.yml"><img src="https://github.com/PursuitOfDataScience/dirscape/actions/workflows/ci.yml/badge.svg" alt="CI"></a>
<a href="https://pypi.org/project/dirscape/"><img src="https://img.shields.io/pypi/v/dirscape.svg" alt="PyPI"></a>
<img src="https://img.shields.io/badge/python-3.6%2B-blue.svg" alt="Python 3.6+">

<img src="https://raw.githubusercontent.com/PursuitOfDataScience/dirscape/main/assets/demo.gif" width="900" alt="ds lists every storage root on a cluster with its usage, quota and file count, opens /software four levels deep and climbs back out, then explains one scratch directory with ds why.">

</div>

You land on a new cluster. Where does 2 TB go? What gets purged on Friday? `du` cannot tell
you, because `du` needs the path you are missing.

## ✨ Install

```bash
pip install dirscape
ds                  # or: dscape, dirscape
```

No dependencies and Python 3.6+, so the system Python on a login node is enough.

## 🧰 Use

```bash
ds                        # the table: arrows move, enter opens a row, esc goes back, q quits
ds why .                  # this directory: which quota it bills to, free space, file limit
ds new                    # what changed since the last run
ds recover results.csv    # snapshot copies of a deleted file, and how to restore one
ds stranded               # space you hold in filesets you can no longer reach
ds elsewhere              # allocations with no path on this node
ds --all                  # every root, including aliases and filesystem roots
```

## 🤖 For agents

| Command | Gives |
| :- | :- |
| `ds paths --json` | One record per place with exact bytes; filter with `--writable`, `--kind scratch`, `--min-free 2T` |
| `ds why <path> --json` | Which place a path bills to, even before the path exists |
| `ds mcp` | The same as MCP tools: `claude mcp add --scope user dirscape -- ds mcp` |

Piped, `ds` prints the table and exits. An agent never gets the interactive browser and never
moves the baseline `ds new` compares against. Exit codes: `0` answered, `1` bad usage, `2` no
place covers that path, `3` nothing found.

## 📌 Good to know

| | |
| :- | :- |
| ⚡ **Sizes come from the quota system** | Not a tree walk, so it finishes in seconds. For bytes per directory use `rdu` or `ncdu`. |
| ❓ **A `?` is never a zero** | Nothing could measure it, and `--json` gives the reason code. |
| 🆕 **Nothing is "new" on the first run** | There is no baseline yet: run it twice. |
| 📁 **Files have a quota too** | A home can allow 300,000 files against 30G, so small files run out first. `ds why` shows the ceiling. |
| 🗂️ **Rows get folded** | A tree that is all yours is one row, and a parent you can only read gives way to the child you can write. `--all` shows both. |
| ✍️ **`read` is not `read only`** | `os.access` lies under root-squashed NFS; `--probe-write` settles it by writing a file. |
| 📸 **Snapshots are not backups** | `ds recover` lists what the filesystem still keeps, and on scratch that is often nothing. |
| 🖥️ **Mounts depend on the node** | Login and compute nodes see different roots, so `ds new` will not compare across them. |
| 🐍 **3.6 installs the wheel** | Building from a checkout needs 3.8+. On 3.6, run one in place: `PYTHONPATH=src python3 -m dirscape`. |

## 🛠️ For site administrators

`dirscape --site-template > /etc/dirscape/site.conf` writes a commented file that gives every user
correct labels, purge warnings and quota backends, with no flags. It grants nothing: access is
always measured at runtime.

## License

MIT
