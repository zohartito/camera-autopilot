"""Regression test for model-vs-fallback detection after mood WB was added.

Run standalone:
    python3 tests/test_mood_eval_detection.py
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "backend"))
sys.path.insert(0, str(ROOT / "tests"))

from eval_intent import _plan_is_fallback
from intent_brain import _fallback


def test_warm_fallback_is_still_detected_as_fallback() -> None:
    plan = _fallback("golden hour warm glow")

    assert plan["white_balance"] == "warm"
    assert _plan_is_fallback(plan, "golden hour warm glow") is True


def main() -> int:
    try:
        test_warm_fallback_is_still_detected_as_fallback()
    except AssertionError as exc:
        print(f"MOOD EVAL DETECTION TESTS: FAIL\n  {exc}")
        return 1

    print("MOOD EVAL DETECTION TESTS: PASS (1/1)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
