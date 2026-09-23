"""An invented cluster for the README demo: the real `ds`, over made-up storage.

`record_demo.py` runs this in a pseudo-terminal. Every root, figure, user and
directory name below is invented, so the recording shows no real site, person
or path. The interface is the real one: `cli.main`, the renderer, the browser
and its keys are unmodified, and only two things are served from the tables
here instead of the machine: the sweep's result and the directory listing.

    python tools/demo_cluster.py --no-state            # the table
    python tools/demo_cluster.py --no-state why /scratch/jdoe42
"""

import os
import sys
import time

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "src"))

from dirscape import __version__, cli  # noqa: E402
from dirscape.discover.identity import Identity  # noqa: E402
from dirscape.model import (  # noqa: E402
    QuotaRow,
    QuotaSnapshot,
    Reach,
    Root,
    VerdictCategory,
    confirmed,
    refuted,
)
from dirscape.render.fields import RunMeta  # noqa: E402
from dirscape.sitecfg import Site  # noqa: E402

USER = "jdoe42"
G = 1024**3
T = 1024**4


def _root(path, role, writable=True, fileset="", device="dev0", fstype="gpfs"):
    root = Root(path, role=role, device=device, fstype=fstype)
    root.fileset = fileset
    root.reach = Reach.LISTABLE
    root.present = confirmed()
    root.mounted = confirmed()
    if writable:
        root.writable = confirmed(source="os.access")
    else:
        root.writable = refuted(VerdictCategory.ACCESS_DENIED, "read only")
    return root


def _quota(root, used, limit=None, scope="user", files=None, file_limit=None):
    row = QuotaRow(root.fileset, "blocks", scope, used, soft=limit, hard=limit, mount=root.path)
    root.quota = QuotaSnapshot("mmlsquota", [row])
    if files is not None:
        inodes = QuotaRow(root.fileset, "files", scope, files, soft=file_limit, hard=file_limit)
        inodes.mount = root.path
        inodes.mounts = [root.path]
        root.inode_quota = QuotaSnapshot("mmlsquota", [inodes])


def cluster():
    """The run a sweep of the invented cluster would return."""
    home = _root("/home/" + USER, "home", fileset="home")
    _quota(home, int(2.1 * G), 30 * G, files=41200, file_limit=300000)

    astro = _root("/project/astro-lab", "project", fileset="astro-lab", device="dev1")
    _quota(astro, int(3.4 * T), 10 * T, scope="fileset", files=1210000, file_limit=20000000)

    genomics = _root("/project/genomics-core", "project", fileset="genomics-core", device="dev1")
    _quota(genomics, 812 * G, 5 * T, scope="fileset", files=388000, file_limit=10000000)

    scratch = _root("/scratch/" + USER, "scratch", fileset="scratch", device="dev2")
    _quota(scratch, 412 * G, 1 * T, files=88300, file_limit=10000000)
    scratch.recoverable = refuted(
        VerdictCategory.NOT_PRESENT,
        "this filesystem exposes a snapshot directory and is keeping nothing in it, "
        "so a deleted file here is gone",
    )

    reference = _root("/datasets/reference", "dataset", writable=False, device="dev3")
    _quota(reference, int(23.6 * T), 0, files=33100)

    software = _root("/software", "software", writable=False, device="dev4")
    _quota(software, 318 * G, 0, files=2640000)

    tmp = _root("/tmp", "local", device="sda1", fstype="xfs")
    _quota(tmp, int(1.2 * G), 0, files=2480)
    tmp.policy["node_local"] = True

    # What the invented site's `[policy]` section would say.
    site = Site()
    site.name = "meadow"
    site.policy_globs = [
        ("/home/*", {"backup": True}),
        ("/project/*", {"backup": True}),
        ("/scratch/*", {"purge_days": 60, "backup": False}),
        ("/datasets/*", {"readonly": True}),
        ("/software", {"readonly": True}),
        ("/tmp", {"backup": False}),
    ]

    run = cli.Run()
    run.site = site
    run.roots = [home, astro, genomics, scratch, reference, software, tmp]
    run.identity = Identity(1000, 1000, USER, gids=[1000], groups=[USER], cluster="meadow")
    run.meta = RunMeta(
        tool="dirscape", version=__version__, user=USER, cluster="meadow", now=time.time()
    )
    return run


#: path -> [(name, items)]: what `ds` lists when a row is opened.
R_PACKAGES = sorted(
    [
        "abind",
        "askpass",
        "backports",
        "base64enc",
        "BH",
        "bit",
        "bit64",
        "blob",
        "brew",
        "brio",
        "broom",
        "bslib",
        "cachem",
        "callr",
        "cellranger",
        "cli",
        "clipr",
        "colorspace",
        "commonmark",
        "cpp11",
        "crayon",
        "curl",
        "data.table",
        "DBI",
        "dbplyr",
        "desc",
        "devtools",
        "diffobj",
        "digest",
        "dplyr",
        "ellipsis",
        "evaluate",
        "fansi",
        "farver",
        "fastmap",
        "fontawesome",
        "forcats",
        "fs",
        "gargle",
        "generics",
        "ggplot2",
        "gh",
        "glue",
        "gtable",
        "haven",
        "highr",
        "hms",
        "htmltools",
        "httr",
        "isoband",
        "jquerylib",
        "jsonlite",
        "knitr",
        "labeling",
        "lifecycle",
        "lubridate",
        "magrittr",
        "MASS",
        "Matrix",
        "memoise",
        "mgcv",
        "mime",
        "modelr",
        "munsell",
        "nlme",
        "openssl",
        "pillar",
        "pkgconfig",
        "purrr",
        "R6",
        "rappdirs",
        "RColorBrewer",
        "Rcpp",
        "readr",
        "readxl",
        "rlang",
        "rmarkdown",
        "rvest",
        "sass",
        "scales",
        "stringi",
        "stringr",
        "survival",
        "sys",
        "tibble",
        "tidyr",
        "tidyselect",
        "tidyverse",
        "tinytex",
        "utf8",
        "vctrs",
        "viridisLite",
        "vroom",
        "withr",
        "xfun",
        "xml2",
        "yaml",
    ],
    key=str.lower,
)
TREE = {
    "/software": [
        ("R-4.4.1", 3),
        ("cuda-12.4", 6),
        ("gcc-13.2.0", 5),
        ("julia-1.10.4", 4),
        ("matlab-2024a", 11),
        ("openmpi-5.0.3", 5),
        ("paraview-5.12", 4),
        ("python-3.12.4", 5),
        ("rstudio-2024.04", 3),
        ("samtools-1.20", 3),
    ],
    "/software/R-4.4.1": [("bin", 2), ("lib64", 2), ("share", 1)],
    "/software/R-4.4.1/lib64": [("R", 8), ("pkgconfig", 1)],
    "/software/R-4.4.1/lib64/R": [
        ("bin", 12),
        ("doc", 9),
        ("etc", 6),
        ("include", 31),
        ("lib", 3),
        ("library", len(R_PACKAGES)),
        ("modules", 4),
        ("share", 7),
    ],
    "/software/R-4.4.1/lib64/R/library": [(name, 9) for name in R_PACKAGES],
    "/home/" + USER: [("envs", 3), ("notebooks", 12), ("papers", 7), ("runs", 24)],
    "/scratch/" + USER: [("checkpoints", 18), ("sweep-2026-09", 64), ("tmp", 2)],
}


def children(path, limit=cli.CHILD_LIMIT):
    """`cli._children`, served from `TREE`."""
    entries = TREE.get(path.rstrip("/") or "/", [])
    out = []
    for name, items in entries[:limit]:
        out.append(
            {
                "name": name,
                "path": path.rstrip("/") + "/" + name,
                "items": items,
                "readable": True,
                "writable": not path.startswith(("/software", "/datasets")),
                "enterable": True,
            }
        )
    return out, max(0, len(entries) - limit)


def main(argv=None):
    run = cluster()
    cli.sweep = lambda opts, runner=None, save_state=True: run
    cli._children = children
    return cli.main(argv)


if __name__ == "__main__":
    sys.exit(main())
