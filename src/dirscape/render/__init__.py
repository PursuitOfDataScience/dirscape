"""Views. Six of them, over one rule.

**The renderer may not print a value it was not given.** There is no code path
in this package that turns an unknown into a number, a blank or a zero:

* a root with no quota backend renders `?`, never "no limit" and never a
  figure of any kind
* a `QuotaRow.fraction` of None renders `?` and NO percentage, because an
  invented 0% reads as plenty of room. There used to be a `style.bar()` here
  that raised rather than draw an empty meter, which was the same rule in
  picture form; the bar is gone (see `fields._figure_cell`) and the rule is
  now simply that a percentage is printed only where a fraction was measured
* a refused `scandir` renders `?`, and the `ncdu` export flags it, rather than
  looking like an empty directory
* a `Verdict` that is neither confirmed nor refuted renders `?`, never "no"
* a `VerdictCategory` never reaches a prose column; `category_label()` does

| module | view |
| :- | :- |
| `atlas` | the default table, one row per root, plus the delta panel |
| `matrix` | roots by capability, three glyphs, with a legend |
| `tree` | device, then fileset, then paths |
| `treemap` | a squarified glance, sized by used bytes |
| `jsonout` | the native `--json` schema |
| `ncdu` | a shallow ncdu-compatible export |

`style` and `fields` are the support layer: terminal capability in one, and the
single place the unknown mark is decided in the other.
"""

# Import order is dependency order and it is load bearing on Python 3.6, which
# this package supports. `atlas` does `from . import fields`, and until 3.7 a
# partially initialised package could not satisfy that: the submodule has to
# already be an attribute of this module. Alphabetising these lines breaks the
# import on the oldest interpreter we promise to run on.
from . import style  # noqa: I001
from . import fields
from . import atlas
from . import jsonout
from . import matrix
from . import ncdu
from . import tree
from . import treemap
from .fields import UNKNOWN, RunMeta
from .style import Glyphs, Style, resolve_style

#: One name per view, for a caller that does not want to hold six modules.
render_atlas = atlas.render
render_matrix = matrix.render
render_tree = tree.render
render_treemap = treemap.render
render_json = jsonout.render
render_ncdu = ncdu.render

__all__ = [
    "UNKNOWN",
    "Glyphs",
    "RunMeta",
    "Style",
    "atlas",
    "fields",
    "jsonout",
    "matrix",
    "ncdu",
    "render_atlas",
    "render_json",
    "render_matrix",
    "render_ncdu",
    "render_tree",
    "render_treemap",
    "resolve_style",
    "style",
    "tree",
    "treemap",
]
