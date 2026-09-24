<div align="center">

# 🗺️ dirscape

**Every place you can put data on a cluster, how full it is, and what changed since last time.**

GPFS · Lustre · CephFS · XFS · NFS · anything with a mount table

<a href="https://github.com/PursuitOfDataScience/dirscape/actions/workflows/ci.yml"><img src="https://github.com/PursuitOfDataScience/dirscape/actions/workflows/ci.yml/badge.svg" alt="CI"></a>
<a href="https://pypi.org/project/dirscape/"><img src="https://img.shields.io/pypi/v/dirscape.svg" alt="PyPI"></a>
<a href="https://pypi.org/project/dirscape/"><img src="https://img.shields.io/endpoint?url=https://raw.githubusercontent.com/PursuitOfDataScience/dirscape/badges/downloads.json" alt="PyPI downloads per month"></a>
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
```

## 🤖 For agents

| Command | Gives |
| :- | :- |
| `ds paths --json` | Every place, with exact bytes |
| `ds why <path> --json` | Which place a path bills to, even before it exists |
| `ds mcp` | The same answers, as MCP tools |

```bash
ds paths --writable --kind scratch --min-free 2T   # filter the places
claude mcp add --scope user dirscape -- ds mcp     # register the MCP server once
```

Piped, `ds` prints the table and exits; an agent never gets the browser or moves the `ds new`
baseline. Exit codes: `0` answered, `1` bad usage, `2` no place covers that path, `3` nothing found.

## 📌 Good to know

| | |
| :- | :- |
| ⚡ **Quota sizes** | Sizes come from quotas, so it takes seconds. For `du`, use `rdu`. |
| 🚀 **Big folders** | `pip install "dirscape[fast]"` adds up opened folders faster. |
| ❓ **`?` is not zero** | Nothing could measure it, and `--json` gives the reason. |
| 🆕 **First run** | Nothing is "new" until a baseline exists: run it twice. |
| 📁 **File limits** | Small files can hit the file quota first; `ds why` shows it. |
| 🗂️ **Folded rows** | A tree that is all yours is one row; `--all` lists the rest. |
| ✍️ **`read`** | Writing was not tested; `--probe-write` tests it for real. |
| 📸 **Snapshots** | They are not backups, and on scratch there are often none. |
| 🖥️ **Per node** | Login and compute nodes see different roots. |
| 🐍 **Python 3.6** | Installs the wheel; building a checkout needs 3.8+. |

## 🛠️ For site administrators

`dirscape --site-template > /etc/dirscape/site.conf` writes a commented config that gives every
user correct labels, purge warnings and quota backends. It grants nothing: access is measured live.

## License

MIT
