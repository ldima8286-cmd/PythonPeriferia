"""The script PyInstaller actually freezes.

It exists only because a frozen build needs a file, not an entry point. Keeping
it this thin means every decision about what to import lives in window.main,
which the tests already exercise.
"""

from __future__ import annotations

import multiprocessing
import sys


def _run() -> int:
    from periferia.gui.window import main

    return main()


if __name__ == "__main__":
    # Without this a frozen build re-runs itself in every worker, which matters
    # the moment anything spawns a process.
    multiprocessing.freeze_support()
    sys.exit(_run())
