"""Run the whole suite without pytest:  python3 tests/run_all.py"""

import importlib
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import _bootstrap  # noqa: F401,E402  (path + dep stubs)
import _runner  # noqa: E402

HERE = os.path.dirname(os.path.abspath(__file__))


def main():
    files = sorted(f[:-3] for f in os.listdir(HERE)
                   if f.startswith("test_") and f.endswith(".py"))
    total_p = total_f = 0
    for mod_name in files:
        print(f"\n[{mod_name}]")
        mod = importlib.import_module(mod_name)
        if hasattr(mod, "main") and not any(n.startswith("test_") for n in vars(mod)):
            # script-style test (e.g. test_agent_loop): call main()
            try:
                mod.main()
                print("  ✓ main"); total_p += 1
            except Exception:
                import traceback; traceback.print_exc(); print("  ✗ main"); total_f += 1
            continue
        p, f = _runner.run(vars(mod), label="  → ")
        total_p += p
        total_f += f
    print(f"\n==== TOTAL: {total_p} passed, {total_f} failed ====")
    sys.exit(1 if total_f else 0)


if __name__ == "__main__":
    main()
