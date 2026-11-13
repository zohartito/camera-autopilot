"""Focus stacking must not manufacture grain on surfaces that are flat in every frame.

Found by the 2026-09-17 merge audit, which looked for the HDR defect family elsewhere. The
selection was a per-pixel hard argmax on a sharpness map. Where every frame is equally
(un)sharp the winner is decided by sensor noise, and taking the pixelwise maximum of noise
manufactures texture no input contains: measured +30-38% grain on a flat cabinet face. Fixed
by smoothing the sharpness map so the choice is regional, and by averaging rather than
selecting where no frame is meaningfully sharper.
"""
import sys
from pathlib import Path

import cv2
import numpy as np

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "backend"))
import stacking  # noqa: E402


def _frames(tmp_path, count=3):
    """Each frame is sharp in a different band; the lower half is flat in all of them."""
    rng = np.random.default_rng(11)
    height, width = 300, 420
    paths = []
    for index in range(count):
        base = np.full((height, width, 3), 150.0)
        for band in range(count):
            y0, y1 = 20 + band * 50, 60 + band * 50
            detail = rng.normal(0, 30, (y1 - y0, width, 1))
            if band != index:                      # out-of-focus bands are blurred
                detail = cv2.GaussianBlur(detail, (0, 0), 4)[:, :, None]
            base[y0:y1] += detail
        base += rng.normal(0, 2.2, base.shape)     # sensor noise, different every frame
        path = tmp_path / f"f{index}.jpg"
        cv2.imwrite(str(path), np.clip(base, 0, 255).astype(np.uint8), [cv2.IMWRITE_JPEG_QUALITY, 97])
        paths.append(str(path))
    return paths


def _grain(gray, region):
    patch = gray[region]
    return float((patch - cv2.GaussianBlur(patch, (0, 0), 2)).std())


def _luma(image):
    return cv2.cvtColor(image, cv2.COLOR_BGR2GRAY).astype(np.float32)


def test_a_flat_region_does_not_gain_grain_the_inputs_never_had(tmp_path):
    paths = _frames(tmp_path)
    flat = (slice(200, 290), slice(20, 400))       # below every sharp band
    merged = _grain(_luma(stacking.focus_stack(paths)), flat)
    inputs = [_grain(_luma(cv2.imread(p)), flat) for p in paths]
    assert merged <= min(inputs) * 1.05, (merged, inputs)


def test_the_sharp_band_still_comes_from_the_frame_that_is_sharp_there(tmp_path):
    paths = _frames(tmp_path)
    stacked = _luma(stacking.focus_stack(paths))
    for band, path in enumerate(paths):
        region = (slice(22 + band * 50, 58 + band * 50), slice(20, 400))
        sharp_input = _luma(cv2.imread(path))
        detail = lambda g: float((g[region] - cv2.GaussianBlur(g[region], (0, 0), 2)).std())
        # The merge must keep most of the detail the in-focus frame had in that band.
        assert detail(stacked) > detail(sharp_input) * 0.7, (band, detail(stacked), detail(sharp_input))


def test_selection_is_regional_and_has_an_averaging_floor(tmp_path):
    source = (REPO / "backend" / "stacking.py").read_text(encoding="utf-8")
    start = source.index("def focus_stack(")
    body = source[start:source.index("\ndef ", start + 10)]
    assert "_FOCUS_DECISION_FLOOR" in body, "flat regions must average, not argmax on noise"
    assert "take = sharpness > best" not in body, "the per-pixel hard argmax is the defect"
    assert stacking._FOCUS_BLUR_KERNEL >= 15, "a 5px window leaves the choice per-pixel"


def test_a_stack_of_identical_frames_reduces_noise_rather_than_amplifying_it(tmp_path):
    rng = np.random.default_rng(3)
    base = np.full((240, 320, 3), 140.0)
    paths = []
    for index in range(4):
        frame = np.clip(base + rng.normal(0, 3.0, base.shape), 0, 255).astype(np.uint8)
        path = tmp_path / f"same_{index}.png"       # lossless, so only the merge is under test
        cv2.imwrite(str(path), frame)
        paths.append(str(path))
    whole = (slice(30, 210), slice(30, 290))
    merged = _grain(_luma(stacking.focus_stack(paths)), whole)
    inputs = [_grain(_luma(cv2.imread(p)), whole) for p in paths]
    assert merged < min(inputs), (merged, inputs)
