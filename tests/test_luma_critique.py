"""Deterministic luma critique fallback (backend/luma_critique.py).

When the vision model host is unreachable, Refine must still judge and correct exposure from the
captured JPEG itself. These tests drive ``critique_from_image`` with synthetic solid-gray JPEGs of
known brightness and assert its verdicts, stop errors, clipping report, determinism, and that its
output keys match the ``vision_critique.critique`` contract that ``reflexion`` consumes.

Run standalone:
    python3 tests/test_luma_critique.py
"""

from __future__ import annotations

import math
import sys
import tempfile
from pathlib import Path

from PIL import Image

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "backend"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from luma_critique import DEFAULT_TOLERANCE_STOPS, critique_from_image

# The exact keys vision_critique.critique emits; reflexion.critique_is_satisfied / corrected_target
# read a subset of these, so the fallback must supply all of them.
VISION_CONTRACT_KEYS = {"matches_intent", "exposure", "brightness_delta", "issues", "suggestion"}

TARGET = 64.0  # mid mean-brightness goal used across the cases


def _gray_jpeg(directory: Path, value: int, name: str) -> Path:
    """Write a flat solid-gray JPEG whose mean luma is ``value`` (quality/subsampling kept exact)."""
    path = directory / name
    Image.new("RGB", (96, 96), (value, value, value)).save(path, "JPEG", quality=100, subsampling=0)
    return path


def test_mid_frame_is_good():
    with tempfile.TemporaryDirectory() as tmp:
        image = _gray_jpeg(Path(tmp), int(TARGET), "mid.jpg")
        critique = critique_from_image(image, TARGET)
    assert critique["exposure"] == "good"
    assert critique["matches_intent"] is True
    assert critique["issues"] == []
    assert abs(critique["stop_error"]) <= DEFAULT_TOLERANCE_STOPS
    assert critique["source"] == "luma_fallback"


def test_two_stops_bright_is_too_bright_and_reports_clipping():
    with tempfile.TemporaryDirectory() as tmp:
        # ~4x the target mean -> ~+2.0 stops, and every pixel is blown so clipping is reported.
        image = _gray_jpeg(Path(tmp), 255, "bright.jpg")
        critique = critique_from_image(image, TARGET)
    assert critique["exposure"] == "too bright"
    assert abs(critique["stop_error"] - 2.0) < 0.1, critique["stop_error"]
    assert any("clipped highlights" in issue for issue in critique["issues"]), critique["issues"]
    # Measured, not a fixed nudge: the shift is target - mean (strongly negative here).
    assert critique["brightness_delta"] < -100


def test_three_stops_dark_is_too_dark():
    with tempfile.TemporaryDirectory() as tmp:
        # 1/8 the target mean -> ~-3.0 stops.
        image = _gray_jpeg(Path(tmp), 8, "dark.jpg")
        critique = critique_from_image(image, TARGET)
    assert critique["exposure"] == "too dark"
    assert abs(critique["stop_error"] + 3.0) < 0.1, critique["stop_error"]
    assert critique["brightness_delta"] > 0  # raise the target to brighten


def test_tolerance_boundary_is_inclusive():
    """A frame is 'good' exactly when |stop_error| <= tolerance; just below flips to off-target."""
    with tempfile.TemporaryDirectory() as tmp:
        image = _gray_jpeg(Path(tmp), 96, "boundary.jpg")  # ~+0.58 stops from target
        measured = critique_from_image(image, TARGET)["stop_error"]
        # Tolerance equal to the measured error -> inclusive 'good'.
        inclusive = critique_from_image(image, TARGET, tolerance_stops=abs(measured))
        # A hair tighter than the measured error -> too bright.
        tighter = critique_from_image(image, TARGET, tolerance_stops=abs(measured) - 0.01)
    assert inclusive["exposure"] == "good"
    assert tighter["exposure"] == "too bright"


def test_determinism():
    with tempfile.TemporaryDirectory() as tmp:
        image = _gray_jpeg(Path(tmp), 140, "det.jpg")
        first = critique_from_image(image, TARGET)
        second = critique_from_image(image, TARGET)
    assert first == second


def test_output_keys_match_vision_contract():
    with tempfile.TemporaryDirectory() as tmp:
        image = _gray_jpeg(Path(tmp), int(TARGET), "keys.jpg")
        critique = critique_from_image(image, TARGET)
    assert VISION_CONTRACT_KEYS <= set(critique), set(critique)
    assert isinstance(critique["issues"], list)
    assert isinstance(critique["brightness_delta"], (int, float))


def main() -> int:
    tests = [
        test_mid_frame_is_good,
        test_two_stops_bright_is_too_bright_and_reports_clipping,
        test_three_stops_dark_is_too_dark,
        test_tolerance_boundary_is_inclusive,
        test_determinism,
        test_output_keys_match_vision_contract,
    ]
    failures: list[str] = []
    for test in tests:
        try:
            test()
        except AssertionError as exc:
            failures.append(f"{test.__name__}: {exc}")
    if failures:
        print("LUMA CRITIQUE TESTS: FAIL")
        for failure in failures:
            print("  " + failure)
        return 1
    print(f"LUMA CRITIQUE TESTS: PASS ({len(tests)}/{len(tests)})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())


def test_good_exposure_with_clipping_does_not_force_a_reshoot(tmp_path):
    """A bright window in an on-target frame is reported, not treated as a reason to shoot again."""
    import numpy as np
    from PIL import Image
    frame = np.full((60, 80, 3), 112, dtype=np.uint8)
    frame[:, :12] = 255                      # 15% blown window on an otherwise on-target frame
    path = tmp_path / "window.jpg"; Image.fromarray(frame).save(path, quality=95)
    result = critique_from_image(str(path), 112.0 * 0.98)
    assert result["exposure"] == "good" and result["issues"] == []
    assert any("clipped" in item for item in result["observations"])
    import reflexion
    assert reflexion.critique_is_satisfied(result)
