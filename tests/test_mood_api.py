"""API-level contract for mood preview and shot mood cards.

Run standalone:
    python3 tests/test_mood_api.py
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "backend"))
os.environ["CAMERA_ENGINE"] = "mock"
os.environ["OLLAMA_HOST"] = "http://127.0.0.1:9"

import app as backend_app


def test_preview_route_returns_visible_mood_card() -> None:
    response = backend_app.intent_preview(
        backend_app.IntentPreviewRequest(intent="cool blue winter morning")
    )

    assert response["mood"]["name"] == "Cool Blue"
    assert response["mood"]["settings"]["white_balance"] == "cool"
    assert response["mood"]["settings"]["brightness_target"] == int(response["target_brightness"])


def test_variants_route_returns_selected_same_scene_moods() -> None:
    response = backend_app.intent_variants(
        backend_app.IntentPreviewRequest(intent="moody noir film still")
    )

    assert response["selected"] == "moody_noir"
    assert len(response["variants"]) == 4
    selected = next(variant for variant in response["variants"] if variant["id"] == response["selected"])
    assert selected["mood"]["name"] == "Moody Noir"
    assert selected["settings"]["exposure_bias"] == "under"
    assert selected["preview"]["recipe"] == "brightness target 82 / auto WB"
    assert selected["settings"]["brightness_target"] == 82


def test_shoot_route_returns_mood_card_for_the_captured_result() -> None:
    response = backend_app.shoot(backend_app.ShootRequest(intent="dreamy golden-hour portrait"))

    assert response["ok"] is True
    assert (backend_app.SHOTS_DIR / response["image"]).is_file()
    assert response["summary"] == "Demo preview — kept it dreamy and warm."
    assert response["evidence"]["physical_capture"] is False
    assert response["mood"]["heard"] == "dreamy golden-hour portrait"
    assert response["mood"]["name"] == "Dreamy Golden Hour"
    assert response["mood"]["settings"]["white_balance"] == "warm"


def test_record_route_returns_autopilot_video_report() -> None:
    response = backend_app.record_video(
        backend_app.RecordVideoRequest(intent="cool blue winter morning", seconds=3.0)
    )

    assert response["ok"] is True
    assert (backend_app.SHOTS_DIR / response["image"]).is_file()
    assert response["summary"] == "Recorded it — kept it cool and quiet."
    assert response["mood"]["name"] == "Cool Blue"
    assert response["recording"]["recorded_seconds"] == 3.0
    assert response["recording"]["saved_to"] == "demo recording (mock)"
    assert response["recording"]["samples"][0]["settings"]["shutter"] == "1/60"


def main() -> int:
    tests = [
        test_preview_route_returns_visible_mood_card,
        test_variants_route_returns_selected_same_scene_moods,
        test_record_route_returns_autopilot_video_report,
        test_shoot_route_returns_mood_card_for_the_captured_result,
    ]
    failures: list[str] = []
    for test in tests:
        try:
            test()
        except Exception as exc:
            failures.append(f"{test.__name__}: {type(exc).__name__}: {exc}")

    if failures:
        print("MOOD API TESTS: FAIL")
        for failure in failures:
            print("  " + failure)
        return 1

    print(f"MOOD API TESTS: PASS ({len(tests)}/{len(tests)})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
