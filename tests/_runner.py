"""Tiny zero-dependency test runner (works without pytest).

Each test file calls ``_runner.run(globals())`` under ``__main__`` to run its own
``test_*`` functions; ``run_all.py`` runs every ``test_*.py`` in this directory.
"""

import traceback


def run(ns, label=""):
    tests = sorted((n, f) for n, f in ns.items() if n.startswith("test_") and callable(f))
    passed = failed = 0
    for name, fn in tests:
        try:
            fn()
            print(f"  ✓ {name}")
            passed += 1
        except Exception:
            print(f"  ✗ {name}")
            traceback.print_exc()
            failed += 1
    print(f"{label}{passed} passed, {failed} failed")
    return passed, failed
