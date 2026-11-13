"""Static contract for the voice mood UI in backend/web/index.html.

Run standalone:
    python3 tests/test_web_voice_ui.py
"""

from __future__ import annotations

from pathlib import Path

INDEX = Path(__file__).resolve().parent.parent / "backend" / "web" / "index.html"


def test_voice_controls_and_mood_cards_exist() -> None:
    html = INDEX.read_text(encoding="utf-8")

    assert 'id="voice-button"' in html
    assert 'id="voice-status"' in html
    assert 'id="mood-card"' in html
    assert 'id="result-mood"' in html
    assert "SpeechRecognition" in html
    assert 'fetch("/intent/preview"' in html
    assert "renderMoodCard" in html


def test_video_mode_controls_and_record_route_exist() -> None:
    html = INDEX.read_text(encoding="utf-8")

    assert 'id="mode-photo"' in html
    assert 'id="mode-video"' in html
    assert 'captureRequest("/record"' in html
    assert "recordVideo" in html
    assert "Recording" in html
    assert "Secure camera access is required" in html


def test_mood_variant_strip_and_reshoot_controls_exist() -> None:
    html = INDEX.read_text(encoding="utf-8")

    assert 'id="mood-variants"' in html
    assert 'id="apply-variant"' in html
    assert 'fetch("/intent/variants"' in html
    assert "renderMoodVariants" in html
    assert "selectedVariantIntent" in html
    assert "applySelectedVariant" in html
    assert "fetchMoodPreviews" in html  # server-rendered previews replaced the CSS filter (2026-09-07)
    assert "variant_id: variantId" in html
    assert "Look previews only" in html
    assert "variant.intent" in html


def main() -> int:
    tests = [
        test_voice_controls_and_mood_cards_exist,
        test_video_mode_controls_and_record_route_exist,
        test_mood_variant_strip_and_reshoot_controls_exist,
    ]
    failures: list[str] = []
    for test in tests:
        try:
            test()
        except AssertionError as exc:
            failures.append(f"{test.__name__}: {exc}")

    if failures:
        print("WEB VOICE UI TESTS: FAIL")
        for failure in failures:
            print("  " + failure)
        return 1

    print(f"WEB VOICE UI TESTS: PASS ({len(tests)}/{len(tests)})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
