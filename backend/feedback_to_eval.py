"""Turn confirmed tester reactions into eval cases — the feedback -> eval loop.

A DELIBERATE command (never auto-run). Real use generates reactions in `backend/feedback.jsonl`
(one row per thumbs up/down, see feedback.py). Running this reads those reactions and writes
*learned* eval cases to `tests/eval_set.learned.json`, in the SAME shapes the curated
`tests/eval_set.json` uses, so the existing eval can grade them. The curated set is never touched.

What it generates
-----------------
  "up"   reaction for intent X  ->  a model_graded REGRESSION case for vibe X: assert the brain
         still lands in a sensible brightness band (derived from the keyword map's own target for
         that intent, so it holds even on the deterministic fallback) with a permissive wb set.
         A confirmed-good shot becomes a guardrail: "for this real intent, don't regress."

  "down" reaction for intent X  ->  a WATCH case: visible, carries the reason, but NEVER graded as
         a pass/fail, so a thumbs-down can't falsely fail the suite. It's a flag for a human to
         curate a real assertion later.

Idempotent
----------
Re-running never duplicates a case. Each case carries a stable `key` derived from
(verdict-class, normalized intent); existing learned cases are loaded first and merged on that key,
so feeding the same reaction twice is a no-op. Malformed / blank feedback lines are skipped.

Run it (deliberately):
    python3 backend/feedback_to_eval.py            # generate/merge learned cases
    python3 backend/feedback_to_eval.py --dry-run  # show what it WOULD write, don't write
    python3 backend/feedback_to_eval.py --print     # print the merged learned set to stdout

This module is import-safe (no side effects on import); the generation only runs from main() or the
public functions below.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

# Make the keyword map importable whether run from repo root or backend/.
_BACKEND_DIR = Path(__file__).resolve().parent
if str(_BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(_BACKEND_DIR))

from intent import target_from_intent

# --- paths ------------------------------------------------------------------------------------
ROOT = _BACKEND_DIR.parent
FEEDBACK_FILE = _BACKEND_DIR / "feedback.jsonl"
LEARNED_FILE = ROOT / "tests" / "eval_set.learned.json"

# Brightness clamp the brain guarantees — learned bands are kept inside it.
_CLAMP_LOW = 60.0
_CLAMP_HIGH = 180.0
# How far around the keyword-map target a learned "still in range" band reaches. Wide enough that a
# live model legitimately differing from the deterministic target still passes, narrow enough to
# catch a real regression (e.g. a moody intent drifting bright).
_BAND_RADIUS = 30.0

# Verdicts that count as a confirmed-good reaction. Anything else that isn't a known "bad" verdict
# is treated as unconfirmed and skipped (we only learn from clear signal).
_GOOD_VERDICTS = {"up"}
_BAD_VERDICTS = {"down"}


def _normalize_intent(intent: str | None) -> str | None:
    """Collapse whitespace + lowercase so 'Moody  Noir' and 'moody noir' dedupe to one key."""
    if not intent or not intent.strip():
        return None
    return " ".join(intent.split()).lower()


def _band_for(intent: str) -> list[float]:
    """A [low, high] brightness band around the keyword-map target for this intent, clamped to the
    brain's guaranteed [60, 180]. Used as the regression assertion for a confirmed-good intent."""
    target = float(target_from_intent(intent))
    low = max(_CLAMP_LOW, target - _BAND_RADIUS)
    high = min(_CLAMP_HIGH, target + _BAND_RADIUS)
    return [low, high]


def _stable_key(verdict_class: str, normalized_intent: str) -> str:
    """The dedup key: one learned case per (verdict-class, normalized intent)."""
    return f"{verdict_class}:{normalized_intent}"


def _read_feedback_rows(feedback_file: Path) -> list[dict]:
    """Read feedback.jsonl, skipping blank and malformed lines. Never raises on bad input."""
    if not feedback_file.exists():
        return []
    rows: list[dict] = []
    try:
        text = feedback_file.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return []
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            continue  # malformed line — skip
        if isinstance(row, dict):
            rows.append(row)
    return rows


def build_cases(rows: list[dict]) -> tuple[list[dict], list[dict]]:
    """Turn feedback rows into (model_graded regression cases, watch cases).

    Only rows with a usable intent AND a known good/bad verdict produce a case. Within this call,
    later rows for the same key win (a tester who flipped their verdict gets the newest outcome),
    and each key appears at most once — so the output is already internally deduped.
    """
    good_by_key: dict[str, dict] = {}
    watch_by_key: dict[str, dict] = {}

    for row in rows:
        normalized = _normalize_intent(row.get("intent"))
        if normalized is None:
            continue  # no intent to learn from (e.g. auto / no-intent shots)
        verdict = row.get("verdict")

        if verdict in _GOOD_VERDICTS:
            key = _stable_key("good", normalized)
            low, high = _band_for(normalized)
            good_by_key[key] = {
                "key": key,
                "id": f"learned_good_{_slug(normalized)}",
                "vibe": normalized,
                "why": (
                    "Learned from a confirmed-good (thumbs up) shot: the brain must keep producing "
                    "in-range settings for this real intent (regression guard)."
                ),
                "brightness_range": [low, high],
                "wb_allowed": ["warm", "cool", None],
                "source": "feedback",
            }
            # A fresh good reaction supersedes a stale watch for the same intent.
            watch_by_key.pop(_stable_key("bad", normalized), None)

        elif verdict in _BAD_VERDICTS:
            key = _stable_key("bad", normalized)
            watch_by_key[key] = {
                "key": key,
                "id": f"learned_watch_{_slug(normalized)}",
                "vibe": normalized,
                "reason": (
                    "Learned from a thumbs-down shot: flagged for a human to curate a real "
                    "assertion. Not graded, so it cannot falsely fail the suite."
                ),
                "verdict": "down",
                "source": "feedback",
            }
            good_by_key.pop(_stable_key("good", normalized), None)

    return list(good_by_key.values()), list(watch_by_key.values())


def _slug(text: str) -> str:
    """A filesystem/id-safe slug from a normalized intent (already lowercased)."""
    return "".join(ch if ch.isalnum() else "_" for ch in text).strip("_") or "intent"


def _load_learned(learned_file: Path) -> dict:
    """Load the existing learned set, or an empty skeleton. Tolerant of a missing/garbage file."""
    skeleton = {
        "_about": (
            "AUTO-GENERATED learned eval cases derived from backend/feedback.jsonl by "
            "backend/feedback_to_eval.py. Do not hand-edit; re-run the generator instead. "
            "Loaded additively by tests/eval_intent.py when present."
        ),
        "model_graded": [],
        "watch": [],
    }
    if not learned_file.exists():
        return skeleton
    try:
        data = json.loads(learned_file.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, UnicodeDecodeError):
        return skeleton
    if not isinstance(data, dict):
        return skeleton
    data.setdefault("model_graded", [])
    data.setdefault("watch", [])
    if not isinstance(data["model_graded"], list):
        data["model_graded"] = []
    if not isinstance(data["watch"], list):
        data["watch"] = []
    data["_about"] = skeleton["_about"]
    return data


def _merge_on_key(existing: list[dict], incoming: list[dict]) -> list[dict]:
    """Merge two case lists, deduped on `key` (incoming wins). Order: existing first, then new keys."""
    by_key: dict[str, dict] = {}
    order: list[str] = []
    for case in existing:
        key = case.get("key")
        if not isinstance(key, str):
            continue
        if key not in by_key:
            order.append(key)
        by_key[key] = case
    for case in incoming:
        key = case["key"]
        if key not in by_key:
            order.append(key)
        by_key[key] = case  # incoming (freshest) overrides
    return [by_key[key] for key in order]


def generate(
    feedback_file: Path = FEEDBACK_FILE,
    learned_file: Path = LEARNED_FILE,
    write: bool = True,
) -> dict:
    """Read feedback, build learned cases, merge with any existing learned set (idempotent), and
    optionally write the result. Returns the merged learned dict."""
    rows = _read_feedback_rows(feedback_file)
    good_cases, watch_cases = build_cases(rows)

    learned = _load_learned(learned_file)
    learned["model_graded"] = _merge_on_key(learned["model_graded"], good_cases)
    learned["watch"] = _merge_on_key(learned["watch"], watch_cases)

    if write:
        learned_file.parent.mkdir(parents=True, exist_ok=True)
        learned_file.write_text(json.dumps(learned, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    return learned


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Turn confirmed feedback reactions into eval cases.")
    parser.add_argument("--dry-run", action="store_true", help="Show what would be written; don't write.")
    parser.add_argument("--print", dest="do_print", action="store_true", help="Print merged learned set.")
    args = parser.parse_args(argv)

    learned = generate(write=not args.dry_run)
    good = learned["model_graded"]
    watch = learned["watch"]

    print("=" * 70)
    print("FEEDBACK -> EVAL")
    print(f"  feedback file : {FEEDBACK_FILE}")
    print(f"  learned file  : {LEARNED_FILE}{'  (DRY RUN — not written)' if args.dry_run else ''}")
    print(f"  regression cases (from thumbs-up): {len(good)}")
    print(f"  watch cases      (from thumbs-down): {len(watch)}")
    print("=" * 70)
    for case in good:
        low, high = case["brightness_range"]
        print(f"  GOOD   {case['vibe']!r} -> brightness in [{low:.0f}, {high:.0f}]")
    for case in watch:
        print(f"  WATCH  {case['vibe']!r} -> flagged (not graded)")
    if args.do_print:
        print("-" * 70)
        print(json.dumps(learned, indent=2, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
