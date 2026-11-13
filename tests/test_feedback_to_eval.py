"""Tests for the feedback -> eval loop (backend/feedback_to_eval.py).

Proves the contract the autopilot's self-improvement relies on:
  * N good/bad feedback entries produce the RIGHT eval cases (good -> graded regression case in the
    eval_set shape; bad -> a watch case that is NEVER graded).
  * Re-running is IDEMPOTENT — feeding the same feedback twice produces no duplicate cases.
  * Malformed / blank feedback lines are skipped, not crashed on.
  * The learned cases load cleanly through the SAME path tests/eval_intent.py uses, and the curated
    eval still passes unchanged (its existing tests are imported and run here).

Everything runs against TEMP files — the repo's real backend/feedback.jsonl and
tests/eval_set.learned.json are never read or written.

Run standalone (no pytest needed):
    CAMERA_ENGINE=mock python3 tests/test_feedback_to_eval.py
Or under pytest:
    CAMERA_ENGINE=mock pytest tests/test_feedback_to_eval.py
"""

from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "backend"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import feedback_to_eval as fte
from intent import target_from_intent

# --- helpers ----------------------------------------------------------------------------------

def _write_feedback(path: Path, rows: list[str]) -> None:
    """Write raw lines (already-serialized or deliberately malformed) to a feedback file."""
    path.write_text("\n".join(rows) + "\n", encoding="utf-8")


def _row(intent, verdict, image="shot.jpg") -> str:
    return json.dumps({"timestamp": "2026-06-27T00:00:00+00:00", "intent": intent,
                       "verdict": verdict, "image": image})


# --- N good/bad entries -> the right cases ----------------------------------------------------

def test_good_and_bad_produce_right_cases():
    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)
        fb = tmp / "feedback.jsonl"
        learned = tmp / "eval_set.learned.json"
        _write_feedback(fb, [
            _row("moody noir film still", "up"),
            _row("bright and airy for instagram", "up"),
            _row("cozy candlelit dinner", "down"),
        ])
        result = fte.generate(feedback_file=fb, learned_file=learned, write=True)

        # 2 thumbs-up -> 2 graded regression cases; 1 thumbs-down -> 1 watch case.
        good = result["model_graded"]
        watch = result["watch"]
        assert len(good) == 2, f"expected 2 regression cases, got {len(good)}"
        assert len(watch) == 1, f"expected 1 watch case, got {len(watch)}"

        # Each regression case is in the model_graded SHAPE the curated eval grades.
        for case in good:
            for field in ("id", "vibe", "brightness_range", "wb_allowed"):
                assert field in case, f"regression case missing {field}: {case}"
            low, high = case["brightness_range"]
            assert 60.0 <= low <= high <= 180.0, f"band {case['brightness_range']} escaped clamp"
            # Band must bracket the keyword-map target for that intent (so it holds on fallback).
            target = float(target_from_intent(case["vibe"]))
            assert low <= target <= high, f"band {case['brightness_range']} excludes target {target}"

        # The watch case carries a reason and is tagged so it is never graded.
        assert watch[0]["vibe"] == "cozy candlelit dinner"
        assert watch[0]["reason"]
        assert watch[0]["verdict"] == "down"

        # File was actually written and is valid JSON with both sections.
        on_disk = json.loads(learned.read_text())
        assert len(on_disk["model_graded"]) == 2
        assert len(on_disk["watch"]) == 1


# --- idempotency ------------------------------------------------------------------------------

def test_rerun_is_idempotent_no_duplicates():
    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)
        fb = tmp / "feedback.jsonl"
        learned = tmp / "eval_set.learned.json"
        _write_feedback(fb, [
            _row("moody noir film still", "up"),
            _row("cozy candlelit dinner", "down"),
        ])
        first = fte.generate(feedback_file=fb, learned_file=learned, write=True)
        second = fte.generate(feedback_file=fb, learned_file=learned, write=True)

        assert len(second["model_graded"]) == len(first["model_graded"]) == 1
        assert len(second["watch"]) == len(first["watch"]) == 1
        # Stable keys are unique within each section.
        good_keys = [c["key"] for c in second["model_graded"]]
        watch_keys = [c["key"] for c in second["watch"]]
        assert len(good_keys) == len(set(good_keys))
        assert len(watch_keys) == len(set(watch_keys))


def test_duplicate_lines_within_one_run_dedupe():
    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)
        fb = tmp / "feedback.jsonl"
        learned = tmp / "eval_set.learned.json"
        # Same intent twice (different casing/spacing) -> ONE case.
        _write_feedback(fb, [
            _row("Moody   Noir film still", "up"),
            _row("moody noir film still", "up"),
        ])
        result = fte.generate(feedback_file=fb, learned_file=learned, write=True)
        assert len(result["model_graded"]) == 1


def test_flipped_verdict_supersedes():
    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)
        fb = tmp / "feedback.jsonl"
        learned = tmp / "eval_set.learned.json"
        # Tester changed their mind: down then up -> ends up a regression case, no watch.
        _write_feedback(fb, [
            _row("golden hour warm glow", "down"),
            _row("golden hour warm glow", "up"),
        ])
        result = fte.generate(feedback_file=fb, learned_file=learned, write=True)
        assert len(result["model_graded"]) == 1
        assert len(result["watch"]) == 0


# --- malformed / blank input ------------------------------------------------------------------

def test_malformed_and_blank_lines_skipped():
    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)
        fb = tmp / "feedback.jsonl"
        learned = tmp / "eval_set.learned.json"
        fb.write_text(
            "\n".join([
                _row("moody noir film still", "up"),
                "",                       # blank
                "   ",                    # whitespace only
                "{not valid json",        # malformed
                "[1, 2, 3]",              # valid JSON but not a dict
                '{"intent": "no verdict field here"}',  # no usable verdict -> skipped
                _row(None, "up"),         # no intent -> skipped
                _row("bright and airy", "up"),
            ]) + "\n",
            encoding="utf-8",
        )
        result = fte.generate(feedback_file=fb, learned_file=learned, write=True)
        # Only the two well-formed, intent-bearing thumbs-up rows produced cases.
        assert len(result["model_graded"]) == 2
        assert len(result["watch"]) == 0


def test_missing_feedback_file_is_safe():
    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)
        fb = tmp / "does_not_exist.jsonl"
        learned = tmp / "eval_set.learned.json"
        result = fte.generate(feedback_file=fb, learned_file=learned, write=True)
        assert result["model_graded"] == []
        assert result["watch"] == []


# --- learned cases flow through the eval loader ------------------------------------------------

def test_learned_cases_load_through_eval_intent_path():
    """A learned file written by the generator loads cleanly via the same _load_learned() the eval
    uses, and the cases are in the model_graded shape eval_intent grades."""
    import eval_intent

    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)
        fb = tmp / "feedback.jsonl"
        learned = tmp / "eval_set.learned.json"
        _write_feedback(fb, [_row("moody noir film still", "up"), _row("blurry mess", "down")])
        fte.generate(feedback_file=fb, learned_file=learned, write=True)

        # Point eval_intent's loader at the temp learned file and confirm it merges additively.
        original_file = eval_intent._LEARNED_FILE
        try:
            eval_intent._LEARNED_FILE = learned
            reloaded = eval_intent._load_learned()
            assert len(reloaded["model_graded"]) == 1
            assert len(reloaded["watch"]) == 1
            # Every learned case must have what _check_model_case reads.
            for case in reloaded["model_graded"]:
                assert "vibe" in case and "brightness_range" in case and "wb_allowed" in case
        finally:
            eval_intent._LEARNED_FILE = original_file


def test_absent_learned_file_leaves_curated_eval_unchanged():
    """With no learned file, _load_learned returns empty and the curated model-graded set is the
    only source — the curated eval behaves exactly as before."""
    import eval_intent

    original_file = eval_intent._LEARNED_FILE
    try:
        with tempfile.TemporaryDirectory() as tmp:
            eval_intent._LEARNED_FILE = Path(tmp) / "absent-learned.json"
            learned = eval_intent._load_learned()
        assert learned == {"model_graded": [], "watch": []}
        merged = list(eval_intent.EVAL_SET["model_graded"]) + list(learned["model_graded"])
        assert len(merged) == len(eval_intent.EVAL_SET["model_graded"])
    finally:
        eval_intent._LEARNED_FILE = original_file


# --- the curated eval still passes -------------------------------------------------------------

def test_curated_deterministic_eval_still_passes():
    """The existing curated deterministic suite must still pass 100% — additive changes only."""
    import eval_intent

    passed, total, failures = eval_intent.run_deterministic()
    assert not failures, "curated deterministic eval regressed:\n  " + "\n  ".join(failures)
    assert passed == total


if __name__ == "__main__":
    from _runner import run_module_tests

    raise SystemExit(run_module_tests(__name__))
