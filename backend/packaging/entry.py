"""PyInstaller entry script for the frozen `scrapy-awesome` binary.

Everything is dispatched by `scrapy_awesome.cli.main`, which also handles the
sub-process modes the server re-executes itself with (`--worker`, `--login-window`).
"""

import multiprocessing
import sys

if __name__ == "__main__":
    multiprocessing.freeze_support()
    from scrapy_awesome.cli import main

    sys.exit(main())
