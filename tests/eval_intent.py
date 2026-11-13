"""Golden EVAL for the intent brain: scene/intent -> settings.

Two suites, loaded from ``tests/eval_set.json``:

  DETERMINISTIC (runs anywhere, gates the brain's contract)
    Feeds synthetic "model replies" through ``intent_brain._coerce`` and asserts the post-conditions
    that MUST hold for ANY model output: brightness clamped to [60, 180], bad/missing brightness
    falling back to the keyword-map target, white_balance restricted to {warm, cool, null}, and
    'feel' never left blank. These don't need Ollama and can't be flaky — they pin the layer the
    camera actually trusts.

  MODEL-GRADED (runs on a box with a reachable Ollama; outcome-graded, not exact)
    Calls ``interpret()`` for real and asserts the result lands inside an ACCEPTABLE RANGE / bucket
    (not an exact number — two good models legitimately differ). If the brain is on its keyword
    fallback (Ollama unreachable), these are SKIPPED, never fabricated and never scored as the model.

Run standalone (no pytest needed):
    python3 tests/eval_intent.py            # both suites; model-graded skipped if no Ollama
Or under pytest:
    pytest tests/eval_intent.py
With a live model on his box:
    OLLAMA_HOST=http://localhost:11434 OLLAMA_MODEL=qwen2.5:14b-instruct python3 tests/eval_intent.py
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "backend"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import intent_brain
from intent_brain import _coerce, interpret

EVAL_SET = json.loads((Path(__file__).resolve().parent / "eval_set.json").read_text())


# --- learned cases (additive, guarded) --------------------------------------------------------
# Cases distilled from real tester reactions by backend/feedback_to_eval.py, written to
# tests/eval_set.learned.json. ADDITIVE and OPTIONAL: if the file is absent (a fresh clone, or
# nobody has run the generator yet) NOTHING changes — the curated eval behaves exactly as before.
# Learned "model_graded" cases are graded the same way as curated ones; learned "watch" cases are
# only reported, never graded, so a thumbs-down can't falsely fail the suite.
_LEARNED_FILE = Path(__file__).resolve().parent / "eval_set.learned.json"


def _load_learned() -> dict:
    """Return {'model_graded': [...], 'watch': [...]} from the learned file, or empty lists if it is
    absent or unreadable. Never raises — a broken learned file must not break the curated eval."""
    empty = {"model_graded": [], "watch": []}
    if not _LEARNED_FILE.exists():
        return empty
    try:
        data = json.loads(_LEARNED_FILE.read_text())
    except (OSError, ValueError):
        return empty
    if not isinstance(data, dict):
        return empty
    learned = dict(empty)
    for section in ("model_graded", "watch"):
        value = data.get(section)
        if isinstance(value, list):
            learned[section] = value
    return learned


LEARNED = _load_learned()


def _model_graded_cases() -> list[dict]:
    """Curated model-graded cases plus any learned ones (additive)."""
    return list(EVAL_SET["model_graded"]) + list(LEARNED["model_graded"])


# --- deterministic suite ----------------------------------------------------------------------

def _check_deterministic_case(case: dict) -> None:
    """Run one deterministic case through _coerce and assert each expected post-condition."""
    plan = _coerce(case["model_reply"], case["intent"])
    for field, expected in case["expect"].items():
        actual = plan[field]
        assert actual == expected, (
            f"[{case['id']}] {field}: got {actual!r}, expected {expected!r} ({case['why']})"
        )
    # Universal invariants that hold for EVERY coerced plan, regardless of the case's own expects.
    bounds = EVAL_SET["clamp_bounds"]
    assert bounds["low"] <= plan["target_brightness"] <= bounds["high"], (
        f"[{case['id']}] brightness {plan['target_brightness']} escaped the clamp"
    )
    assert plan["white_balance"] in ("warm", "cool", None), (
        f"[{case['id']}] white_balance {plan['white_balance']!r} not in {{warm, cool, null}}"
    )
    assert isinstance(plan["feel"], str) and plan["feel"].strip(), (
        f"[{case['id']}] feel must never be blank"
    )


def run_deterministic() -> tuple[int, int, list[str]]:
    """Run the whole deterministic suite. Returns (passed, total, failures)."""
    cases = EVAL_SET["deterministic"]
    failures: list[str] = []
    for case in cases:
        try:
            _check_deterministic_case(case)
        except AssertionError as exc:
            failures.append(str(exc))
    return len(cases) - len(failures), len(cases), failures


# --- model-graded suite (live model; skipped on fallback) -------------------------------------

def _plan_is_fallback(plan: dict, vibe) -> bool:
    """True if THIS specific plan came from the deterministic keyword fallback, not the model.

    Honest, per-result detection (NOT a one-time global probe): current plans carry an explicit
    source. The legacy heuristic remains for older plan shapes so a TRANSIENT Ollama/JSON hiccup
    mid-run can't slip a fabricated fallback answer through as if it were model output.
    """
    if plan.get("source") in {"fallback", "preview"}:
        return True
    if plan.get("source") == "model":
        return False
    why = plan.get("why", "")
    templated = ("You asked to" in why and "mean brightness of about" in why) or (
        vibe is None and why == "Read the light and set a natural, balanced exposure for you."
    )
    return templated and plan.get("white_balance") is None


def _on_keyword_fallback() -> bool:
    """True if the brain is on its keyword map right now (Ollama unreachable). One probe with a vibe
    the keyword map can't satisfy ('golden hour' -> map returns wb=None + templated why)."""
    return _plan_is_fallback(interpret("golden hour warm glow"), "golden hour warm glow")


def _check_model_case(case: dict) -> None:
    """Grade one case. Raises AssertionError on a range/wb miss; raises _Skip if THIS call fell back
    to the keyword map (so the caller counts it as skipped, never as a model pass/fail)."""
    plan = interpret(case["vibe"])
    if _plan_is_fallback(plan, case["vibe"]):
        raise _Skip(f"[{case['id']}] interpret() fell back to keyword map this run — not graded")
    low, high = case["brightness_range"]
    target = plan["target_brightness"]
    assert low <= target <= high, (
        f"[{case['id']}] brightness {target} outside [{low}, {high}] ({case['why']})"
    )
    allowed = case["wb_allowed"]
    assert plan["white_balance"] in allowed, (
        f"[{case['id']}] white_balance {plan['white_balance']!r} not in {allowed} ({case['why']})"
    )


class _Skip(Exception):
    """A single model-graded case that silently fell back this run — counted as skipped, not graded."""


def run_model_graded() -> tuple[int, int, list[str], bool, int]:
    """Run the model-graded suite IF a live model is answering.

    Returns (passed, total, failures, all_skipped, per_case_skips). ``all_skipped`` is True when the
    whole suite is skipped because Ollama is unreachable. ``per_case_skips`` counts individual cases
    that transiently fell back mid-run (graded against the model -> NEVER; counted as skipped).
    """
    cases = _model_graded_cases()
    if _on_keyword_fallback():
        return 0, len(cases), [], True, 0
    failures: list[str] = []
    skips = 0
    for case in cases:
        try:
            _check_model_case(case)
        except _Skip:
            skips += 1
        except AssertionError as exc:
            failures.append(str(exc))
    graded = len(cases) - skips
    return graded - len(failures), graded, failures, False, skips


# --- pytest entry points ----------------------------------------------------------------------

def test_deterministic_suite():
    """The deterministic contract must hold 100% — these gate the layer the camera trusts."""
    passed, total, failures = run_deterministic()
    assert not failures, "deterministic eval failures:\n  " + "\n  ".join(failures)
    assert passed == total


def test_model_graded_suite():
    """Outcome-graded against the LIVE model; auto-skipped (xfail-free) when on keyword fallback."""
    passed, graded, failures, all_skipped, per_case_skips = run_model_graded()
    if all_skipped:
        # Not a failure: the model just isn't reachable in this environment. Pytest reports a skip.
        try:
            import pytest

            pytest.skip("Ollama unreachable — model-graded cases skipped (run on his box)")
        except ImportError:
            return
    assert not failures, "model-graded eval failures:\n  " + "\n  ".join(failures)
    assert passed == graded


# --- standalone runner ------------------------------------------------------------------------

def main() -> int:
    print("=" * 74)
    print("INTENT EVAL — scene/intent -> settings (golden)")
    print(f"brain: {Path(intent_brain.__file__).resolve()}")
    print(f"OLLAMA_HOST={intent_brain.OLLAMA_HOST}  OLLAMA_MODEL={intent_brain.OLLAMA_MODEL}")
    print("=" * 74)

    d_passed, d_total, d_failures = run_deterministic()
    print(f"\nDETERMINISTIC (model-independent, must be 100%): {d_passed}/{d_total}")
    for failure in d_failures:
        print(f"  XX {failure}")

    m_passed, m_graded, m_failures, all_skipped, per_case_skips = run_model_graded()
    total_cases = len(_model_graded_cases())
    learned_count = len(LEARNED["model_graded"])
    watch_count = len(LEARNED["watch"])
    if learned_count or watch_count:
        print(
            f"\nLEARNED (from feedback): {learned_count} regression case(s) folded into model-graded; "
            f"{watch_count} watch case(s) flagged (not graded)."
        )
    if all_skipped:
        print(f"\nMODEL-GRADED: SKIPPED — Ollama unreachable, {total_cases} cases need a live model.")
        print("  Run on the box with Ollama:")
        print("  OLLAMA_HOST=http://localhost:11434 OLLAMA_MODEL=qwen2.5:14b-instruct \\")
        print("    python3 tests/eval_intent.py")
    else:
        skip_note = f"  ({per_case_skips} fell back mid-run -> skipped, not graded)" if per_case_skips else ""
        print(f"\nMODEL-GRADED (live model, outcome-ranged): {m_passed}/{m_graded}{skip_note}")
        for failure in m_failures:
            print(f"  XX {failure}")

    print("\n" + "=" * 74)
    # Exit code reflects ONLY what we actually verified: the deterministic suite always, and the
    # model-graded cases that actually hit the live model. Skips (whole-suite or per-case) are not
    # failures.
    failed = bool(d_failures) or (not all_skipped and bool(m_failures))
    verdict = "PASS" if not failed else "FAIL"
    detail = "deterministic clean" if not d_failures else f"{len(d_failures)} deterministic failures"
    if all_skipped:
        detail += "; model-graded skipped (no Ollama)"
    elif not m_failures:
        detail += f"; model-graded clean ({m_graded}/{total_cases} graded)"
    else:
        detail += f"; {len(m_failures)} model-graded failures"
    print(f"VERDICT: {verdict} — {detail}")
    print("=" * 74)
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
