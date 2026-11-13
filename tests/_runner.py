"""Tiny zero-dependency test runner so the suites run with plain `python3` in any sandbox.

The test files are also valid pytest modules (functions named ``test_*``). This runner lets them
run WITHOUT pytest installed: import it, collect every ``test_*`` callable from the calling module,
run them, print a scorecard, and exit non-zero on any failure.

    if __name__ == "__main__":
        from _runner import run_module_tests
        raise SystemExit(run_module_tests(__name__))
"""

from __future__ import annotations

import sys
import traceback


def run_module_tests(module_name: str) -> int:
    """Run every ``test_*`` function in the named (already-imported) module. Return exit code."""
    module = sys.modules[module_name]
    tests = sorted(
        (name, obj)
        for name, obj in vars(module).items()
        if name.startswith("test_") and callable(obj)
    )
    failures = []
    print(f"Running {len(tests)} tests from {module_name}")
    print("-" * 70)
    for name, fn in tests:
        try:
            fn()
        except AssertionError as exc:
            failures.append((name, exc))
            print(f"  FAIL  {name}: {exc}")
        except Exception as exc:  # noqa: BLE001 — a crashing test is a failing test
            failures.append((name, exc))
            print(f"  ERROR {name}: {exc!r}")
            traceback.print_exc()
        else:
            print(f"  ok    {name}")
    print("-" * 70)
    passed = len(tests) - len(failures)
    print(f"{passed}/{len(tests)} passed")
    return 1 if failures else 0
