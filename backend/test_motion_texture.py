"""Synthetic tests for the three CV factors in `motion_texture.py` — no camera required.

Each test builds frames in memory (a tracked random texture for the flow factors, a
checkerboard for texture) and asserts the factor's core guarantee:

  * freeze_shutter_s       — a bigger frame-to-frame shift yields a *faster* freeze shutter;
                             an identical (static) pair imposes effectively no floor.
  * subject_motion_floor_s — a fast subject on a static background gives a fast floor; a
                             moving background with a *still subject box* gives no/slow floor.
  * texture_score          — checkerboard high (>0.7), uniform gray ~0 (<0.05), blurred
                             checkerboard between the two.

Run with:

    cd /path/to/camera-autopilot
    backend/.venv-ml/bin/python backend/test_motion_texture.py
"""
from __future__ import annotations

import sys
from pathlib import Path

import cv2
import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent))

import motion_texture as mt

_RNG = np.random.default_rng(42)
_SIZE = 320
# Time between the two synthetic frames. The absolute value is arbitrary for the tests;
# what matters is that a bigger pixel shift at the same dt yields a faster shutter.
_DT_S = 0.01


def _texture(size: int = _SIZE) -> np.ndarray:
    """A trackable BGR texture: random noise smoothed just enough that Farneback has
    continuous gradients to follow (pure per-pixel noise has no coherent motion to track)."""
    noise = _RNG.integers(0, 256, (size, size), dtype=np.uint8)
    smoothed = cv2.GaussianBlur(noise, (0, 0), sigmaX=1.0)
    return cv2.cvtColor(smoothed, cv2.COLOR_GRAY2BGR)


def _shift(image: np.ndarray, dx: float, dy: float = 0.0) -> np.ndarray:
    """Translate an image by (dx, dy) pixels, reflecting at the border (no black edges)."""
    matrix = np.float32([[1, 0, dx], [0, 1, dy]])
    height, width = image.shape[:2]
    return cv2.warpAffine(image, matrix, (width, height), borderMode=cv2.BORDER_REFLECT)


def _checkerboard(size: int = _SIZE, square: int = 16) -> np.ndarray:
    """A hard black/white checkerboard — maximal high-frequency detail."""
    rows, cols = np.indices((size, size))
    board = ((((rows // square) + (cols // square)) % 2) * 255).astype(np.uint8)
    return cv2.cvtColor(board, cv2.COLOR_GRAY2BGR)


# --------------------------------------------------------------------------- #
# OPTICAL FLOW: bigger shift -> faster freeze shutter; static -> no floor.
# --------------------------------------------------------------------------- #
def test_freeze_shutter_scales_with_shift() -> None:
    base = _texture()
    flow_small = mt.optical_flow_field(base, _shift(base, 2.0))
    flow_large = mt.optical_flow_field(base, _shift(base, 8.0))

    shutter_small = mt.freeze_shutter_s(flow_small, _DT_S, max_blur_px=1.0)
    shutter_large = mt.freeze_shutter_s(flow_large, _DT_S, max_blur_px=1.0)

    # A faster-moving subject must demand a faster (shorter) shutter to freeze it.
    assert shutter_large < shutter_small, (
        f"bigger shift should need a faster shutter: 8px -> {shutter_large:.5f}s "
        f"vs 2px -> {shutter_small:.5f}s")
    # Both must be finite, real shutter floors (motion was present).
    assert 0 < shutter_large < shutter_small < float("inf")
    # And roughly the right factor: ~4x the shift -> ~1/4 the shutter (Farneback recovers
    # the true displacement, so this is a real px-velocity -> shutter check, not just order).
    ratio = shutter_small / shutter_large
    assert 3.0 < ratio < 5.0, f"expected ~4x faster shutter for 4x the shift, got {ratio:.2f}x"
    print(f"  optical flow: 2px -> 1/{1/shutter_small:.0f}s, 8px -> 1/{1/shutter_large:.0f}s "
          f"({ratio:.1f}x faster)")


def test_static_pair_imposes_no_floor() -> None:
    base = _texture()
    flow = mt.optical_flow_field(base, base.copy())  # identical frames: zero true motion
    shutter = mt.freeze_shutter_s(flow, _DT_S, max_blur_px=1.0)

    # A moving subject at this dt freezes around 1/100s–1/800s; a static scene must land far
    # slower than that (here ~sub-1s from residual sub-pixel noise) -> effectively no floor.
    assert shutter > 0.1, f"static pair should impose ~no motion floor, got {shutter:.5f}s"
    print(f"  static pair:  freeze shutter {shutter:.3f}s (effectively no floor)")


# --------------------------------------------------------------------------- #
# SUBJECT MOTION: fast subject -> fast floor; moving bg + still subject -> none.
# --------------------------------------------------------------------------- #
# The subject is a 140 px textured patch; the query box (80 px) sits *inside* it. That
# mirrors a real subject-detection box, which lands on the subject's interior, not exactly
# on its moving silhouette — so the box never straddles the subject/background motion edge
# (where Farneback's smoothing window would otherwise bleed background motion inward).
_PATCH_ORIGIN = 90
_PATCH_SIZE = 140
_SUBJECT_BOX = (120, 120, 80, 80)  # (x, y, w, h), comfortably within the patch


def _place_patch(frame: np.ndarray, patch: np.ndarray) -> np.ndarray:
    """Composite a patch at _PATCH_ORIGIN into a copy of frame."""
    out = frame.copy()
    end = _PATCH_ORIGIN + _PATCH_SIZE
    out[_PATCH_ORIGIN:end, _PATCH_ORIGIN:end] = patch
    return out


def test_subject_box_isolates_a_moving_subject() -> None:
    # Static background; the subject patch shifts between frames -> the box interior moves.
    background = _texture()
    patch = _texture(size=_PATCH_SIZE)

    frame_a = _place_patch(background, patch)
    frame_b = _place_patch(background, _shift(patch, 7.0))  # subject moves 7 px

    flow = mt.optical_flow_field(frame_a, frame_b)
    subject_floor = mt.subject_motion_floor_s(flow, _DT_S, subject_box=_SUBJECT_BOX, max_blur_px=1.0)
    whole_frame = mt.freeze_shutter_s(flow, _DT_S, max_blur_px=1.0)

    # The subject box sees real motion -> a fast, finite floor.
    assert 0 < subject_floor < 0.05, (
        f"moving subject should give a fast floor, got {subject_floor:.5f}s")
    # That floor is what the brain would receive as subject_motion_floor_s.
    print(f"  subject move: subject floor 1/{1/subject_floor:.0f}s "
          f"(whole-frame 1/{1/whole_frame:.0f}s)")


def test_still_subject_ignores_moving_background() -> None:
    # The inverse: a panning background, but the subject patch holds still. The subject box
    # must NOT be dragged fast by background motion the photographer may want blurred.
    background = _texture()
    patch = _texture(size=_PATCH_SIZE)

    frame_a = _place_patch(background, patch)
    frame_b = _place_patch(_shift(background, 9.0), patch)  # bg pans 9 px, SAME subject pixels

    flow = mt.optical_flow_field(frame_a, frame_b)
    subject_floor = mt.subject_motion_floor_s(flow, _DT_S, subject_box=_SUBJECT_BOX, max_blur_px=1.0)
    whole_frame = mt.freeze_shutter_s(flow, _DT_S, max_blur_px=1.0)

    # Background motion makes the whole-frame factor demand a fast shutter...
    assert whole_frame < 0.02, (
        f"moving background should give a fast whole-frame floor, got {whole_frame:.5f}s")
    # ...but the still subject box imposes essentially no floor, so the river/crowd can blur
    # while the subject stays sharp.
    assert subject_floor > 0.1, (
        f"still subject should impose ~no floor despite moving bg, got {subject_floor:.5f}s")
    assert subject_floor > 5 * whole_frame, (
        "subject floor must be far slower than the whole-frame floor when only the "
        f"background moves: subject {subject_floor:.4f}s vs frame {whole_frame:.4f}s")
    print(f"  moving bg:    subject floor {subject_floor:.2f}s (slow / no floor) vs "
          f"whole-frame 1/{1/whole_frame:.0f}s (fast)")


# --------------------------------------------------------------------------- #
# TEXTURE: checkerboard high, uniform gray ~0, blurred checkerboard between.
# --------------------------------------------------------------------------- #
def test_texture_score_ranks_detail() -> None:
    sharp = mt.texture_score(_checkerboard())
    flat = mt.texture_score(np.full((_SIZE, _SIZE, 3), 128, dtype=np.uint8))
    blurred = mt.texture_score(cv2.GaussianBlur(_checkerboard(), (0, 0), sigmaX=4.0))

    assert sharp > 0.7, f"sharp checkerboard should read high, got {sharp:.3f}"
    assert flat < 0.05, f"uniform gray should read ~0, got {flat:.3f}"
    # "In between": clearly below the sharp board and clearly above the flat field.
    assert flat < blurred < sharp, (
        f"blurred checkerboard should land between flat and sharp: "
        f"flat {flat:.3f} < blurred {blurred:.3f} < sharp {sharp:.3f}")
    assert blurred < 0.5, f"a well-blurred board should read clearly soft, got {blurred:.3f}"
    print(f"  texture:      checker {sharp:.3f} > blurred {blurred:.3f} > gray {flat:.3f}")


def _run() -> None:
    test_freeze_shutter_scales_with_shift()
    test_static_pair_imposes_no_floor()
    test_subject_box_isolates_a_moving_subject()
    test_still_subject_ignores_moving_background()
    test_texture_score_ranks_detail()
    print("motion_texture tests: PASS")


if __name__ == "__main__":
    _run()
