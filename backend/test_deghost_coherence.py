"""Spatial-coherence + seed guard on the deghost moving-mask (backend/stacking.py).

Written after 2026-09-20, when a three-frame A7 II HDR bracket (session 7c27b568) merged
with large dark splatter blotches across a static sofa, cushions, cane doors and rug that
existed in no input frame. The heavily clipped bright bracket matched poorly to the dark
reference, so exposure-match error and JPEG chroma noise cleared the pointwise motion floor
on ordinary midtone texture; deghosting pasted the dark reference over those pixels. The
metrics passed (merged detail and clipping both improved) while the photograph was obviously
broken. The mask now requires a moving region to be BOTH spatially contiguous (drop small
connected components) AND seeded by a decisive disagreement (drop weak just-over-threshold
blobs), and that decision is made once on the whole frame so tiling cannot cut a real mover.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

import cv2
import numpy as np
import pytest

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "backend"))
import stacking  # noqa: E402

_RNG = np.random.default_rng(2026)
_HDR_BLOTCH_SESSION = REPO / "backend" / "shots" / "sessions" / "7c27b5683d134a59a40d766fcc8a0c3d"


def _gray_texture(height: int, width: int, low: int = 96, high: int = 160) -> np.ndarray:
    """A neutral mid-tone texture (all three channels equal, so it stays grey)."""
    luma = _RNG.integers(low, high, (height, width, 1), dtype=np.uint8)
    return np.repeat(luma, 3, axis=2)


def test_contiguous_moving_object_is_fully_masked():
    # A single solid, strongly-coloured region that "moved" (present in the frame, absent in
    # the reference). It is both large and decisively different, so both guards keep it and
    # its interior must be masked at full strength.
    reference = _gray_texture(600, 600)
    frame = reference.copy()
    frame[220:380, 220:380] = (40, 40, 220)   # a solid red block: strong luma + chroma change
    mask = stacking._moving_mask(frame, reference)
    interior = mask[250:350, 250:350]
    assert interior.min() > 0.9, f"a contiguous mover must be fully masked (min {interior.min():.2f})"
    # And nothing lights up on the untouched background.
    assert float(mask[:150, :150].max()) == 0.0


def test_scattered_speckle_and_small_blobs_are_rejected():
    # The SAME decisive colour disagreement as above, so the seed test would keep every pixel,
    # but broken into isolated single pixels and tiny blobs well below the min-area floor.
    # Spatial coherence alone must reject all of it (this is the blotch-speckle failure mode).
    size = 2000
    min_area = max(1, round(stacking._DEGHOST_MIN_AREA_FRACTION * size * size))
    reference = _gray_texture(size, size)
    frame = reference.copy()
    blob = 4  # 16 px, comfortably below the ~40 px floor at this resolution
    assert blob * blob < min_area
    for y in range(60, size - 60, 90):
        for x in range(60, size - 60, 90):
            frame[y:y + blob, x:x + blob] = (40, 40, 220)      # scattered red specks
            frame[y + 30, x + 30] = (40, 40, 220)              # and lone single pixels
    mask = stacking._moving_mask(frame, reference)
    assert float(mask.mean()) < 1e-4, f"scattered speckle must be rejected (mask mean {mask.mean():.5f})"
    assert float(mask.max()) == 0.0


def test_static_high_texture_pair_gives_near_zero_mask():
    # Two frames of ONE static high-contrast texture with independent sensor noise: no motion.
    # The pointwise residual fires on noise, but neither guard lets any of it through.
    base = _RNG.integers(20, 235, (500, 700, 3), dtype=np.uint8)
    reference = np.clip(base.astype(np.int16) + _RNG.normal(0, 5, base.shape), 0, 255).astype(np.uint8)
    frame = np.clip(base.astype(np.int16) + _RNG.normal(0, 5, base.shape), 0, 255).astype(np.uint8)
    mask = stacking._moving_mask(frame, reference, suppress_clipped=False)
    assert float(mask.mean()) < 1e-3, f"a static textured pair must stay near-zero (mask mean {mask.mean():.5f})"


def _straddling_stack(tmp_path: Path) -> list[str]:
    """Three equal-exposure frames: a static texture plus a bright square that sits at a
    different place in each. The reference (middle) square is centred on x=64 so it straddles
    a 64 px tile boundary."""
    size = 200
    base = _gray_texture(size, size, 70, 150)
    squares = [(90, 20), (80, 44), (30, 150)]   # frame 1 (reference) square straddles x=64
    paths = []
    for index, (top, left) in enumerate(squares):
        frame = base.copy()
        frame[top:top + 40, left:left + 40] = 240
        path = tmp_path / f"frame_{index:04d}.png"
        assert cv2.imwrite(str(path), frame)
        paths.append(str(path))
    return paths


def test_mover_straddling_a_tile_boundary_is_not_cut(tmp_path, monkeypatch):
    monkeypatch.setattr(stacking, "_registration", lambda *a, **k: np.eye(3))
    paths = _straddling_stack(tmp_path)

    # The coherence/seed decision is made once on the whole frame, so the result must be
    # identical whether the frame is processed as one tile or in many small tiles that cut
    # straight through the mover — the definitive proof that no tile edge can sever it.
    monkeypatch.setattr(stacking, "STACK_TILE_SIZE", 4096)
    _, whole = stacking.long_exposure_with_report(paths, deghost=True)
    monkeypatch.setattr(stacking, "STACK_TILE_SIZE", 64)
    _, tiled = stacking.long_exposure_with_report(paths, deghost=True)
    assert np.array_equal(whole["mask"], tiled["mask"]), "tiling changed the mask"

    # The reference square (top=80, left=44, straddling the x=64 seam) is fully and evenly
    # masked, with no seam-column dip where the tiles meet.
    reference_square = tiled["mask"][86:114, 50:78]
    assert reference_square.min() == 255, "the straddling mover was not fully masked"
    seam = tiled["mask"][86:114, 62:66].astype(np.int16)
    assert int(seam.max() - seam.min()) == 0, "a tile-edge discontinuity appeared across the mover"


@pytest.mark.skipif(not (_HDR_BLOTCH_SESSION / "frame_0000.jpg").is_file()
                    or os.environ.get("CAMERA_REAL_DATA_TESTS") != "1",
                    reason="private A7 II HDR session 7c27b568 absent or CAMERA_REAL_DATA_TESTS!=1")
def test_real_a7ii_hdr_bracket_no_longer_blotches_the_static_sofa():
    paths = [str(_HDR_BLOTCH_SESSION / f"frame_{i:04d}.jpg") for i in range(3)]
    luma = lambda bgr: cv2.cvtColor(bgr, cv2.COLOR_BGR2GRAY).astype(np.float32)
    deghosted = luma(stacking.hdr_merge(paths, deghost=True))
    plain = luma(stacking.hdr_merge(paths, deghost=False))
    # A flat mid-tone patch of the sofa arm that the shipped deghost splattered with dark
    # blotches (measured 2026-09-20: ~29% of the patch darkened by >25 levels, peak 203).
    sofa = (slice(2900, 3800), slice(4700, 5600))
    darkening = plain[sofa] - deghosted[sofa]
    assert float((darkening > 25).mean()) < 0.01, "the static sofa is still being blotched dark"
    assert deghosted[sofa].mean() > plain[sofa].mean() - 5.0, "deghost darkened the flat sofa"
