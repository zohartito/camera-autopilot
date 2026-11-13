"""Evaluation harness for the intent brain.

Run this BEFORE putting `interpret()` in front of real beginners. It feeds a labeled set of
representative photo vibes through the brain and scores each result against an EXPECTED DIRECTION
(brightness bucket + allowed white-balance), then runs a small consistency check and prints a
scorecard with an overall pass-rate and a one-line verdict.

Stdlib only, no new deps. Hits the local Ollama if reachable; otherwise the brain falls back to its
deterministic keyword map (this harness detects and reports which backend answered).

Usage:
    cd ~/Code/camera-autopilot/backend && python eval_brain.py
"""

from __future__ import annotations

import statistics
from pathlib import Path

from intent_brain import interpret

# --- Labeled test set -------------------------------------------------------------------------
# Each case: the vibe (intent passed to interpret), the expected brightness bucket(s), and the
# allowed white-balance values. "any" white_balance always passes. Brightness buckets:
#   low  = target < 100   (moodier/darker)
#   mid  = 100 <= target <= 135  (neutral-ish)
#   high = target > 135   (brighter/airier)
# Multiple buckets / wb values are pipe-separated and mean "any of these is acceptable".
TEST_SET = [
    {"vibe": "moody noir film still", "brightness": "low", "wb": "warm|none"},
    {"vibe": "dark and dramatic portrait", "brightness": "low", "wb": "any"},
    {"vibe": "bright and airy for instagram", "brightness": "high", "wb": "cool|none"},
    {"vibe": "clean bright product shot on white", "brightness": "high", "wb": "cool|none"},
    {"vibe": "golden hour warm glow", "brightness": "low|mid", "wb": "warm"},
    {"vibe": "cozy candlelit dinner", "brightness": "low|mid", "wb": "warm"},
    {"vibe": "cool blue winter morning", "brightness": "mid|high", "wb": "cool"},
    {"vibe": "freeze my kid running around", "brightness": "mid|high", "wb": "any"},
    {"vibe": "fast action sports, sharp and crisp", "brightness": "mid|high", "wb": "any"},
    {"vibe": "creamy blurred background portrait", "brightness": "low|mid|high", "wb": "any"},
    {"vibe": "natural everyday snapshot", "brightness": "mid", "wb": "any"},
    {"vibe": "just take a normal photo", "brightness": "mid", "wb": "any"},
    {"vibe": "high-key fashion editorial, very bright", "brightness": "high", "wb": "any"},
    {"vibe": "low-key shadowy still life", "brightness": "low", "wb": "any"},
    {"vibe": None, "brightness": "mid", "wb": "none"},  # auto-balance, no intent
]

# Vibes re-run 3x each to measure stability (the brain uses temperature 0.4, so spread should be low).
CONSISTENCY_VIBES = ["moody noir film still", "bright and airy for instagram", "freeze my kid running around"]
CONSISTENCY_RUNS = 3
SPREAD_OK = 20.0  # acceptable max-min target_brightness spread across repeats


def bucket(target: float) -> str:
    """Bucket a brightness target into low / mid / high."""
    if target < 100:
        return "low"
    if target <= 135:
        return "mid"
    return "high"


def sanity_check(plan: dict) -> list[str]:
    """Return a list of sanity problems (empty list = the plan is structurally valid)."""
    problems = []
    target = plan.get("target_brightness")
    if not isinstance(target, (int, float)) or isinstance(target, bool):
        problems.append("target_brightness not a number")
    elif not 60 <= target <= 180:
        problems.append(f"target_brightness {target} out of [60,180]")
    for field in ("feel", "why"):
        value = plan.get(field)
        if not isinstance(value, str) or not value.strip():
            problems.append(f"{field} empty")
    if plan.get("white_balance") not in ("warm", "cool", None):
        problems.append(f"white_balance {plan.get('white_balance')!r} invalid")
    return problems


def wb_label(white_balance) -> str:
    """Map the white_balance value to the label used in the allowed-set check."""
    return "none" if white_balance is None else white_balance


def direction_check(plan: dict, case: dict) -> tuple[bool, str]:
    """Check brightness bucket and white-balance against the expected direction.

    Returns (passed, note). The note explains the first failure, or confirms the match.
    """
    target = plan["target_brightness"]
    got_bucket = bucket(target)
    allowed_buckets = case["brightness"].split("|")
    if got_bucket not in allowed_buckets:
        return False, f"brightness {got_bucket} (want {case['brightness']})"

    allowed_wb = case["wb"].split("|")
    got_wb = wb_label(plan["white_balance"])
    if "any" not in allowed_wb and got_wb not in allowed_wb:
        return False, f"wb {got_wb} (want {case['wb']})"

    return True, "ok"


def looks_like_fallback(plan: dict, vibe) -> bool:
    """True when the answer came from the deterministic fallback rather than the live model."""
    if plan.get("source") in {"fallback", "preview"}:
        return True
    if plan.get("source") == "model":
        return False
    # Legacy heuristic for older plan shapes without source.
    why = plan.get("why", "")
    templated = ("You asked to" in why and "mean brightness of about" in why) or (
        vibe is None and why == "Read the light and set a natural, balanced exposure for you."
    )
    return templated and plan.get("white_balance") is None


def run_scorecard() -> dict:
    """Run every test case once, score it, and return the collected results."""
    rows = []
    passes = 0
    fallback_hits = 0
    for case in TEST_SET:
        vibe = case["vibe"]
        plan = interpret(vibe)
        problems = sanity_check(plan)
        if problems:
            ok, note = False, "; ".join(problems)
        else:
            ok, note = direction_check(plan, case)
        passes += ok
        fallback_hits += looks_like_fallback(plan, vibe)
        rows.append(
            {
                "vibe": vibe if vibe is not None else "(none / auto)",
                "target": plan.get("target_brightness"),
                "wb": wb_label(plan.get("white_balance")),
                "ok": ok,
                "note": note,
            }
        )
    return {"rows": rows, "passes": passes, "fallback_hits": fallback_hits}


def run_consistency() -> list[dict]:
    """Re-run a few vibes several times and measure the target_brightness spread."""
    results = []
    for vibe in CONSISTENCY_VIBES:
        targets = [interpret(vibe)["target_brightness"] for _ in range(CONSISTENCY_RUNS)]
        spread = max(targets) - min(targets)
        stdev = statistics.pstdev(targets) if len(targets) > 1 else 0.0
        results.append({"vibe": vibe, "targets": targets, "spread": spread, "stdev": stdev})
    return results


def fmt_target(target) -> str:
    return f"{target:.0f}" if isinstance(target, (int, float)) and not isinstance(target, bool) else str(target)


def print_report(scorecard: dict, consistency: list[dict]) -> None:
    rows = scorecard["rows"]
    total = len(rows)
    passes = scorecard["passes"]
    rate = 100.0 * passes / total if total else 0.0
    fallback_hits = scorecard["fallback_hits"]

    print("=" * 78)
    print("INTENT BRAIN EVAL — does it make good photography calls for a beginner?")
    print("=" * 78)

    backend = "keyword fallback (Ollama unreachable)" if fallback_hits == total else (
        "LLM (Ollama) — model answers" if fallback_hits == 0 else
        f"MIXED — {total - fallback_hits}/{total} look like the model, {fallback_hits} fell back"
    )
    print(f"Backend in use: {backend}")
    print()

    header = f"{'vibe':<40} {'target':>6} {'wb':>5}  {'dir':>3}  note"
    print(header)
    print("-" * len(header))
    for row in rows:
        mark = "OK " if row["ok"] else "XX "
        print(f"{row['vibe']:<40} {fmt_target(row['target']):>6} {row['wb']:>5}  {mark:>3}  {row['note']}")

    print()
    print(f"Pass rate: {passes}/{total}  ({rate:.0f}%)")
    print()

    print("Consistency (same vibe x3, temperature 0.4 -> spread should be small):")
    worst_spread = 0.0
    for entry in consistency:
        targets = ", ".join(fmt_target(value) for value in entry["targets"])
        worst_spread = max(worst_spread, entry["spread"])
        flag = "" if entry["spread"] <= SPREAD_OK else "  <-- HIGH"
        print(f"  {entry['vibe']:<38} targets=[{targets}]  spread={entry['spread']:.0f}  sd={entry['stdev']:.1f}{flag}")
    print()

    failed = [row["vibe"] for row in rows if not row["ok"]]
    consistency_ok = worst_spread <= SPREAD_OK
    trustworthy = rate >= 80.0 and consistency_ok

    if trustworthy and not failed:
        verdict = "brain looks trustworthy — directions and stability both pass."
    elif trustworthy:
        verdict = f"brain looks mostly trustworthy ({rate:.0f}%), watch: {', '.join(failed)}."
    else:
        reasons = []
        if rate < 80.0:
            reasons.append(f"pass rate {rate:.0f}% < 80%")
        if not consistency_ok:
            reasons.append(f"unstable (max spread {worst_spread:.0f} > {SPREAD_OK:.0f})")
        detail = "; ".join(reasons)
        weak = f" weak vibes: {', '.join(failed)}" if failed else ""
        verdict = f"needs work: {detail}.{weak}"

    print(f"VERDICT: {verdict}")
    print("=" * 78)


def main() -> None:
    print(f"Evaluating intent brain at: {Path(__file__).resolve()}")
    print()
    scorecard = run_scorecard()
    consistency = run_consistency()
    print_report(scorecard, consistency)


if __name__ == "__main__":
    main()
