"""PyInstaller entry point (the package's __main__ uses relative imports)."""

import multiprocessing
import sys

from sdrscanner.__main__ import main

if __name__ == "__main__":
    multiprocessing.freeze_support()
    sys.exit(main())
