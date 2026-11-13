"""HDR fusion must produce a photograph, not a flat blotchy render.

Written after 2026-09-17, when a merge with excellent clipping numbers (0.18% highlights,
0.22% shadows) was rejected on sight: flat, detail-poor, with swirling blotches on the walls
that no bracket contained. Two causes, both fixed here and both pinned by these tests.

Revised 2026-09-20, when a third cause was found behind the same complaint: the fusion
pyramid was too shallow to blend low frequencies at all (see `test_hdr_fusion_scale.py`).
Two things in this file had been calibrated against that defective output and are re-based
here with the measurements that justify them. The synthetic bracket was also rebuilt: at
320x480 it was smaller than one 512 px tile, so none of these tests ever exercised the tiled
path, its "flat wall" sat 12 px under a hard synthetic edge whose halo the swirl metric read
as blotching, and its mid frame clipped 19% of the frame, which inflated the very baseline
the contrast bar is expressed against.
"""
import sys
from pathlib import Path

import cv2
import numpy as np
import pytest

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "backend"))
import stacking  # noqa: E402

_REAL = REPO / "backend" / "shots" / "sessions" / "02290746a9614e1b9bf74040e9a417ce"


# A flat region far from every hard edge in the scene: the swirl metric below is a 6-to-20 px
# bandpass, so a region near an edge reads that edge's halo rather than invented structure.
_WALL = (slice(500, 660), slice(140, 640))


def _bracket(tmp_path):
    """Three exposures of a room: continuous falloff, a clipping window, a dim corner, texture.

    Deliberately larger than one 512 px tile in both axes so the tiled merge path is exercised,
    and deliberately clipping ~4.8% of the mid frame, which is what the real bracket sessions
    on both bodies measure (3.2% to 4.8%). Tone compression is judged against that frame, so a
    synthetic scene that clips a fifth of itself makes the comparison meaningless.
    """
    rng = np.random.default_rng(5)
    height, width = 704, 896
    rows, cols = np.mgrid[0:height, 0:width].astype(np.float32)
    base = (90 + 70 * (cols / width) + 30 * np.sin(rows / height * 2.2))[:, :, None].repeat(3, axis=2)
    base[60:380, 800:] = 252.0                 # window: clips in the brighter frames
    base[:240, :280] *= 0.35                   # a dim corner
    base[120:380, 120:660] += rng.normal(0, 13, (260, 540, 1))  # detail that must survive
    paths = []
    for index, gain in enumerate((0.45, 1.0, 2.1)):
        frame = np.clip(base * gain + rng.normal(0, 1.4, base.shape), 0, 255).astype(np.uint8)
        path = tmp_path / f"frame_{index}.jpg"
        cv2.imwrite(str(path), frame, [cv2.IMWRITE_JPEG_QUALITY, 97])
        paths.append(str(path))
    return paths


def _luma(image):
    return cv2.cvtColor(image, cv2.COLOR_BGR2GRAY).astype(np.float32)


def test_the_merge_keeps_the_contrast_of_the_best_bracket(tmp_path):
    paths = _bracket(tmp_path)
    merged = _luma(stacking.hdr_merge(paths))
    best = _luma(cv2.imread(paths[1]))
    # Exposure fusion compresses tonality; the restoration must bring it back near the
    # bracket the camera exposed best rather than leaving a washed-out frame.
    assert merged.std() > best.std() * 0.85, (merged.std(), best.std())


def test_tonality_restoration_is_what_supplies_that_contrast(tmp_path):
    # Only meaningful for the fused render. The default merge keeps the metered bracket as the
    # photograph, so its tonality comes from that bracket and this curve never runs.
    paths = _bracket(tmp_path)
    restored = _luma(stacking.hdr_merge(paths, recover=False))
    flat = _luma(stacking.hdr_merge(paths, recover=False, tonality=0.0))
    assert restored.std() > flat.std(), "tonality=0 must be the uncorrected, flatter fusion"


def test_the_merge_does_not_invent_structure_on_a_flat_wall(tmp_path):
    paths = _bracket(tmp_path)
    merged = stacking.hdr_merge(paths)
    wall = _WALL
    # A region that is uniform in every bracket must not gain low-frequency blotches.
    def swirl(gray):
        return float((cv2.GaussianBlur(gray, (0, 0), 6) - cv2.GaussianBlur(gray, (0, 0), 20)).std())
    merged_swirl = swirl(_luma(merged)[wall[0], wall[1]])
    worst_input = max(swirl(_luma(cv2.imread(p))[wall[0], wall[1]]) for p in paths)
    assert merged_swirl <= max(worst_input * 2.0, 1.0), (merged_swirl, worst_input)


def test_the_fusion_weight_uses_well_exposedness(tmp_path):
    # The missing Mertens term is what made flat neutral areas noise-decided. Guard it.
    source = (REPO / "backend" / "stacking.py").read_text(encoding="utf-8")
    start = source.index("def _fusion_weights(")
    body = source[start:source.index("\ndef ", start + 10)]
    assert "well_exposed" in body, "contrast x saturation alone goes degenerate on a grey wall"
    assert "_FUSE_WEIGHT_FLOOR" in body and "_FUSE_WEIGHT_SMOOTH" in body


def test_highlight_recovery_is_not_given_back(tmp_path):
    paths = _bracket(tmp_path)
    merged = _luma(stacking.hdr_merge(paths))
    brightest = _luma(cv2.imread(paths[2]))
    clipped = lambda g: float((g >= 250).mean())
    assert clipped(merged) < clipped(brightest), "the merge must still hold the window"


@pytest.mark.skipif(not (_REAL / "frame_0001.jpg").is_file(),
                    reason="private daylight HDR session 02290746 is not on this machine")
def test_real_daylight_bracket_beats_its_own_best_frame(tmp_path):
    paths = [str(_REAL / f"frame_{i:04d}.jpg") for i in range(3)]
    merged = stacking.hdr_merge(paths)
    merged_luma, best = _luma(merged), _luma(cv2.imread(paths[1]))
    detail = lambda g: float((g - cv2.GaussianBlur(g, (0, 0), 3))[(g > 20) & (g < 235)].std())
    # Global std must clear the same 0.85-of-best bar as the synthetic sibling above. Measured
    # 0.97 here on 2026-09-20, and 0.93 to 0.98 across the four real bracket sessions.
    assert merged_luma.std() > best.std() * 0.85, (merged_luma.std(), best.std())
    # DETAIL IS A BAND, NOT A FLOOR, and this is the correction that matters. It used to read
    # `detail(merged) > detail(best)`, which the shallow pyramid passed easily by manufacturing
    # crunch: it ran 3.58x the best bracket's Laplacian detail on this very session. Fusion
    # that neither loses nor invents detail sits at parity, and measures 0.98 here.
    ratio = detail(merged_luma) / detail(best)
    assert 0.9 < ratio < 1.5, f"detail must stay near the bracket, not be lost or manufactured: {ratio:.2f}"
    # Highlights: this was `/ 5` until 2026-09-20, a bar only the flattened merge could clear
    # (0.22% against the bracket's 3.32%). Flattening a photograph is one way to stop clipping
    # it. The honest recovery on this scene is 1.74% against 3.32%, so hold it to 1.5x.
    assert (merged_luma >= 250).mean() < (best >= 250).mean() / 1.5, "highlights must still be held"
