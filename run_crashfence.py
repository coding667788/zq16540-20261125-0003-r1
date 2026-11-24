#!/usr/bin/env python3
"""Repository-root launcher: ``python -I run_crashfence.py ...``.

Running a script puts this repository root on ``sys.path`` even under Python's
isolated ``-I`` mode, unlike ``python -I -m crashfence`` from some launchers.
"""

from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parent))

from crashfence.cli import main

if __name__ == "__main__":
    raise SystemExit(main())
