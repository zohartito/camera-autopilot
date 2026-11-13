"""Deterministic contract for spoken mood/vibe intent cards.

Run standalone:
    python3 tests/test_mood_intent.py
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "backend"))

from intent_brain import _coerce, preview, variants


def test_preview_builds_mood_card_for_spoken_golden_hour_vibe() -> None:
    plan = preview("dreamy golden-hour portrait")

    assert plan["feel"] == "kept it dreamy and warm"
    assert plan["white_balance"] == "warm"
    assert plan["mood"]["heard"] == "dreamy golden-hour portrait"
    assert plan["mood"]["name"] == "Dreamy Golden Hour"
    assert "warm" in plan["mood"]["color"].lower()
    assert plan["mood"]["settings"]["brightness_target"] == int(plan["target_brightness"])
    assert plan["mood"]["settings"]["white_balance"] == "warm"


def test_coerce_fills_mood_card_from_the_safe_camera_plan() -> None:
    plan = _coerce(
        {
            "target_brightness": 77,
            "feel": "kept it cold and quiet",
            "why": "Cool low light keeps the scene quiet without hiding the subject.",
            "white_balance": "cool",
        },
        "cold lonely blue morning",
    )

    assert plan["target_brightness"] == 77.0
    assert plan["mood"]["name"] == "Cool Blue"
    assert plan["mood"]["settings"]["brightness_target"] == 77
    assert plan["mood"]["settings"]["white_balance"] == "cool"
    assert plan["mood"]["settings"]["exposure_bias"] == "under"


def test_preview_stays_neutral_when_no_vibe_is_spoken() -> None:
    plan = preview(None)

    assert plan["mood"]["heard"] is None
    assert plan["mood"]["name"] == "Natural"
    assert plan["mood"]["settings"]["white_balance"] == "auto"
    assert plan["mood"]["settings"]["exposure_bias"] == "neutral"


def test_preview_understands_natural_balanced_as_natural() -> None:
    plan = preview("natural balanced photo")

    assert plan["mood"]["name"] == "Natural"
    assert plan["mood"]["settings"]["white_balance"] == "auto"
    assert plan["mood"]["settings"]["exposure_bias"] == "neutral"


def test_variants_return_four_stable_same_scene_moods() -> None:
    plan = variants("dreamy golden-hour portrait")

    assert plan["selected"] == "dreamy_golden_hour"
    assert [variant["id"] for variant in plan["variants"]] == [
        "natural",
        "dreamy_golden_hour",
        "moody_noir",
        "cool_blue",
    ]
    for variant in plan["variants"]:
        assert variant["intent"]
        assert variant["mood"]["name"]
        assert variant["settings"]["brightness_target"] == variant["mood"]["settings"]["brightness_target"]
        assert variant["preview"]["css_filter"]
        assert variant["preview"]["recipe"]


def test_variants_select_cool_blue_from_spoken_intent() -> None:
    plan = variants("cool blue winter morning")

    selected = next(variant for variant in plan["variants"] if variant["id"] == plan["selected"])
    assert plan["selected"] == "cool_blue"
    assert selected["intent"] == "cool blue winter morning"
    assert selected["mood"]["settings"]["white_balance"] == "cool"


def test_variants_select_natural_without_spoken_vibe() -> None:
    plan = variants(None)

    assert plan["selected"] == "natural"
    assert plan["variants"][0]["name"] == "Natural"


def main() -> int:
    tests = [
        test_preview_builds_mood_card_for_spoken_golden_hour_vibe,
        test_coerce_fills_mood_card_from_the_safe_camera_plan,
        test_preview_stays_neutral_when_no_vibe_is_spoken,
        test_preview_understands_natural_balanced_as_natural,
        test_variants_return_four_stable_same_scene_moods,
        test_variants_select_cool_blue_from_spoken_intent,
        test_variants_select_natural_without_spoken_vibe,
    ]
    failures: list[str] = []
    for test in tests:
        try:
            test()
        except AssertionError as exc:
            failures.append(f"{test.__name__}: {exc}")

    if failures:
        print("MOOD INTENT TESTS: FAIL")
        for failure in failures:
            print("  " + failure)
        return 1

    print(f"MOOD INTENT TESTS: PASS ({len(tests)}/{len(tests)})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
