"""Tests for focus_verify — sharpness scoring, best-of-N selection, the relative acceptance gate."""
import sys
import tempfile
from pathlib import Path

import cv2
import numpy as np
from PIL import Image

sys.path.insert(0, str(Path(__file__).resolve().parent))

import focus_verify

_CAP = "captures/20260621-112540_capt_DSC00511.JPG"  # optional local capture; synthesized or skipped when absent


def test_sharp_beats_blurred() -> None:
    with tempfile.TemporaryDirectory() as d:
        src = _CAP
        if not Path(src).exists():  # synthesize a textured frame if no real capture is around
            src = str(Path(d) / "src.jpg")
            noise = np.random.default_rng(0).integers(0, 255, (600, 800, 3), dtype="uint8")
            Image.fromarray(noise).save(src)
        sharp = focus_verify.sharpness(src)
        blur_path = str(Path(d) / "blur.jpg")
        blurred_bgr = cv2.GaussianBlur(cv2.imread(src), (0, 0), 6)
        Image.fromarray(cv2.cvtColor(blurred_bgr, cv2.COLOR_BGR2RGB)).save(blur_path)
        blurred = focus_verify.sharpness(blur_path)
    assert sharp > blurred * 1.5, f"sharp ({sharp:.0f}) must beat blurred ({blurred:.0f})"
    assert focus_verify.sharpest([blurred, sharp]) == 1
    assert focus_verify.in_focus(sharp, sharp) and not focus_verify.in_focus(blurred, sharp)
    print(f"  sharp beats blurred: ok (sharp {sharp:.0f} vs blurred {blurred:.0f})")


def test_region_crop() -> None:
    if not Path(_CAP).exists():
        print("  region: skipped (sample capture missing)")
        return
    full = focus_verify.sharpness(_CAP)
    sub = focus_verify.sharpness(_CAP, region=(2051, 0, 3949, 4000))  # the lamp box from the depth model
    assert full > 0 and sub > 0, (full, sub)
    print(f"  region crop scores: ok (full {full:.0f}, subject {sub:.0f})")


if __name__ == "__main__":
    test_sharp_beats_blurred()
    test_region_crop()
    print("focus_verify tests: PASS")
