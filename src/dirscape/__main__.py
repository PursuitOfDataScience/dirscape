"""Allow `python -m dirscape`, for a login node where the console script is
not on PATH because pip installed it into a directory the user never added.
"""

import sys

from .cli import main

if __name__ == "__main__":
    sys.exit(main())
