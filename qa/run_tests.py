#!/usr/bin/env python3 -I
"""Direct QA runner for isolated mode.

Because ``-I`` removes both cwd and PYTHONPATH from ``sys.path``, this
file lives inside the ``qa`` package directory (which is always on the
path for a directly executed script) and bootstraps the workspace paths
before importing anything. Usage from the repository root::

    python3 -I qa/run_tests.py
    python3 -I qa/run_tests.py --mutations
    python3 -I qa/run_tests.py --record
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
WORKSPACE = HERE.parent
for path in (str(WORKSPACE / "src"), str(WORKSPACE), str(HERE)):
    if path not in sys.path:
        sys.path.insert(0, path)


def main() -> int:
    if "--record" in sys.argv:
        from qa.run_all import record_snapshots
        written = record_snapshots()
        print("recorded snapshots:")
        for path in written:
            print(" ", path)

    loader = unittest.TestLoader()
    suite = loader.loadTestsFromName("qa.test_properties")
    result = unittest.TextTestRunner(verbosity=2).run(suite)
    return 0 if result.wasSuccessful() else 1


if __name__ == "__main__":
    raise SystemExit(main())
