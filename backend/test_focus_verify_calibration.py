"""Calibration tests for focus_verify.sharpness_evidence's soft/sharp verdict.

The verdict earlier thresholded a frame's detail as a percentage of its OWN sharpest tile (60% =
sharp). Measured 2026-09-20 across every recent real capture on both Sony bodies, that ratio labelled
7 of 8 good photographs "soft" — it mostly counts how much smooth wall/ceiling/sofa a scene contains,
not whether anything is in focus. The verdict now anchors on the ABSOLUTE detail of the frame's (or
subject's) most-detailed region: a focused frame has at least one genuinely crisp region, a defocused
one has none anywhere.

Two groups of cases:
  * The eight real captures from the 2026-09-20 calibration set. Present only on Zohar's machine, so
    the test skips cleanly when they are absent or CAMERA_REAL_DATA_TESTS != 1 (same gate as
    test_night_deghost). The visibly-defocused frame must be "soft"; every visibly-sharp one must not.
  * Synthetic frames that pin the behaviour anywhere: a sharp textured frame, the SAME frame blurred,
    a genuinely low-detail (but in-focus) scene that must NOT be called soft merely for being plain,
    and a frame sharp in one region and smooth elsewhere (the interior case the old ratio failed on).
"""

from __future__ import annotations

import os
from pathlib import Path

import cv2
import numpy as np
import pytest
from PIL import Image

import focus_verify

_SHOTS = Path(__file__).resolve().parent / "shots"
# The 2026-09-20 calibration set: (filename, is_visibly_blurry). The first A7 II frame is visibly
# mush; the other seven are photographs Zohar inspected and confirmed sharp.
_REAL_SHOTS: list[tuple[str, bool]] = [
    ("shot_1789933782863875000.jpg", True),   # A7 II, visibly out of focus
    ("shot_1789934864818178000.jpg", False),  # A7 II
    ("shot_1789935307329366000.jpg", False),  # A7 II, visibly sharp
    ("shot_1789935363315065000.jpg", False),  # A7 III, sharp
    ("shot_1789691534205830000.jpg", False),  # A7 III, sharp
    ("shot_1789695188912720000.jpg", False),  # A7 III, sharp
    ("shot_1789539547846732000.jpg", False),  # sharp (low-detail scene)
    ("shot_1789539480483297000.jpg", False),  # sharp
]

_REAL_PRESENT = all((_SHOTS / name).is_file() for name, _ in _REAL_SHOTS)


def _save(path: Path, gray: np.ndarray) -> str:
    Image.fromarray(np.clip(gray, 0, 255).astype("uint8")).save(path)
    return str(path)


def _structured_frame() -> np.ndarray:
    """A frame-wide field of crisp high-contrast rectangles: sharp everywhere, and — unlike per-pixel
    noise — it degrades gracefully under blur, so a moderate defocus lands in the soft band instead of
    collapsing straight through it to undetermined."""
    rng = np.random.default_rng(5)
    frame = np.full((900, 1600), 128, dtype=np.float64)
    for _ in range(120):
        x, y = int(rng.integers(0, 1500)), int(rng.integers(0, 800))
        w, h = int(rng.integers(60, 220)), int(rng.integers(60, 220))
        frame[y : y + h, x : x + w] = rng.choice([0, 255])
    return frame


# ---------------------------------------------------------------------------------------------------
# Real calibration set
# ---------------------------------------------------------------------------------------------------
@pytest.mark.skipif(
    not _REAL_PRESENT or os.environ.get("CAMERA_REAL_DATA_TESTS") != "1",
    reason="private 2026-09-20 calibration shots absent or CAMERA_REAL_DATA_TESTS!=1",
)
def test_real_shots_separate_blurry_from_sharp():
    verdicts = {}
    for name, blurry in _REAL_SHOTS:
        ev = focus_verify.sharpness_evidence(str(_SHOTS / name))
        verdicts[name] = (ev["verdict"], ev["best_tile_detail"], blurry)
        if blurry:
            assert ev["verdict"] == "soft", (name, ev)
        else:
            # The whole point of the recalibration: a genuinely sharp photo must NOT cry wolf.
            assert ev["verdict"] != "soft", (name, ev)
    print("  real calibration set:")
    for name, (verdict, best_tile, blurry) in verdicts.items():
        tag = "BLURRY" if blurry else "sharp "
        print(f"    {tag} {name}: {verdict:12} (best-tile {best_tile:.0f})")


# ---------------------------------------------------------------------------------------------------
# Synthetic cases that pin the behaviour without any private data
# ---------------------------------------------------------------------------------------------------
def test_sharp_textured_frame_is_sharp(tmp_path):
    ev = focus_verify.sharpness_evidence(_save(tmp_path / "sharp.png", _structured_frame()))
    assert ev["verdict"] == "sharp", ev
    assert ev["best_tile_detail"] >= focus_verify._SHARP_DETAIL
    assert ev["calibration"] == "uncalibrated_relative"


def test_same_frame_gaussian_blurred_is_soft(tmp_path):
    """The exact frame from the sharp case, defocused: the verdict must flip to soft."""
    blurred = cv2.GaussianBlur(_structured_frame(), (0, 0), 1.5)
    ev = focus_verify.sharpness_evidence(_save(tmp_path / "blurred.png", blurred))
    assert ev["verdict"] == "soft", ev
    # Above the texture floor (there IS content) but below the detail floor (nothing is crisp).
    assert focus_verify._TEXTURE_FLOOR <= ev["best_tile_detail"] < focus_verify._SHARP_DETAIL, ev


def test_low_detail_focused_scene_is_not_soft(tmp_path):
    """A smooth gradient is in focus but genuinely carries almost no detail. It must NOT be called soft
    just for being plain — the honest verdict is 'undetermined' (too flat to judge focus)."""
    gradient = np.tile(np.linspace(0, 255, 1600), (900, 1))
    ev = focus_verify.sharpness_evidence(_save(tmp_path / "gradient.png", gradient))
    assert ev["verdict"] != "soft", ev
    assert ev["verdict"] == "undetermined", ev
    assert "floor" in ev["reason"]


def test_sharp_region_amid_smooth_is_sharp(tmp_path):
    """The interior case the old ratio failed on: one crisply focused region, lots of smooth area. A
    focused region is present, so the frame is sharp — the smooth walls must not drag it to soft."""
    rng = np.random.default_rng(11)
    frame = np.full((900, 1600), 128, dtype=np.float64)
    frame[:, :400] = rng.integers(0, 256, (900, 400))  # sharp left quarter, smooth elsewhere
    ev = focus_verify.sharpness_evidence(_save(tmp_path / "region.png", frame))
    assert ev["verdict"] == "sharp", ev


def test_subject_box_catches_soft_subject_in_a_sharp_scene(tmp_path):
    """With a subject box we anchor on the subject's own best tile, using the whole frame as context.
    A soft subject in an otherwise-detailed scene must still be flagged soft (the false-negative
    direction that matters most), while the frame's sharp region reads sharp on its own box."""
    rng = np.random.default_rng(13)
    frame = np.full((900, 1600), 128, dtype=np.float64)
    frame[:, :400] = rng.integers(0, 256, (900, 400))  # crisp left quarter
    src = _save(tmp_path / "scene.png", frame)

    on_sharp = focus_verify.sharpness_evidence(src, subject_box=(0, 0, 400, 900))
    on_smooth = focus_verify.sharpness_evidence(src, subject_box=(700, 0, 800, 900))
    assert on_sharp["verdict"] == "sharp", on_sharp
    assert on_smooth["verdict"] == "soft", on_smooth
    # Context proves focus was achievable (frame best tile is high) yet the subject never gets crisp.
    assert on_smooth["best_tile_detail"] >= focus_verify._SHARP_DETAIL
    assert on_smooth["subject_best_tile_detail"] < focus_verify._SHARP_DETAIL


if __name__ == "__main__":
    import tempfile

    with tempfile.TemporaryDirectory() as d:
        base = Path(d)
        test_sharp_textured_frame_is_sharp(base)
        test_same_frame_gaussian_blurred_is_soft(base)
        test_low_detail_focused_scene_is_not_soft(base)
        test_sharp_region_amid_smooth_is_sharp(base)
        test_subject_box_catches_soft_subject_in_a_sharp_scene(base)
    print("focus_verify calibration synthetic tests: PASS")
