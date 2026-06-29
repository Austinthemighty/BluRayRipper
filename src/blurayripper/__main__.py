"""Enable `python -m blurayripper ...` (handy when the console script isn't on PATH)."""

import sys

from .cli import main

if __name__ == "__main__":
    sys.exit(main())
