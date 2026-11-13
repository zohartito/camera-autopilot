"""Exposure fusion must blend the brackets at every scale, including the whole frame.

Written 2026-09-20, after a merge whose deghost blotches had already been fixed was still
rejected on sight: flat overall, crunchy up close. The cause was pyramid depth. Tiled fusion
carried only `_STACK_PYRAMID_LEVELS` levels, so on a 6000x4000 frame the brackets were
blended at scales up to 16 px and no further. The low frequencies never moved between
exposures, which flattened global tonality, and every exposure decision was forced into fine
scales, which reads as halos and crunch.

Measured over four real bracket sessions on both bodies, the shallow fusion ran 1.3x to 3.6x
the best bracket's Laplacian detail while losing global contrast and saturation. **It also
scored far better on highlight clipping** (0.22 % against the bracket's 3.32 % on the A7 III
daylight room) because flattening a photograph is one way to stop clipping it. That is the
second time on this algorithm that a defect has propped up the metric meant to police it,
which is why the load-bearing test below is an equivalence against a reference implementation
rather than another quality score.
"""
import sys
from pathlib import Path

import cv2
import numpy as np
import pytest

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "backend"))
import stacking  # noqa: E402


def _bracket(tmp_path, height=704, width=896):
    """Three exposures of a scene with whole-frame structure, a clipping window and texture.

    Larger than one tile in both axes, so the merge genuinely exercises the tiled path.
    """
    rng = np.random.default_rng(3)
    ramp = np.linspace(40, 150, width, dtype=np.float32)[None, :, None]
    base = np.repeat(np.repeat(ramp, height, axis=0), 3, axis=2)
    base[:, int(width * 0.78):] = 252.0                     # window: clips in the brighter frames
    base[80:300, 40:360] *= 0.4                             # a dim corner
    base[120:560, 80:700] += rng.normal(0, 10, (440, 620, 1))   # detail that must survive
    paths, frames = [], []
    for index, gain in enumerate((0.35, 1.0, 2.6)):
        frame = np.clip(base * gain + rng.normal(0, 1.0, base.shape), 0, 255).astype(np.uint8)
        path = tmp_path / f"frame_{index}.jpg"
        cv2.imwrite(str(path), frame, [cv2.IMWRITE_JPEG_QUALITY, 98])
        paths.append(str(path))
        frames.append(cv2.imread(str(path)))
    return paths, frames


def _reference_fusion(frames):
    """Textbook Mertens: one non-tiled pyramid deep enough to reach the whole frame."""
    levels = max(0, int(np.log2(min(frames[0].shape[:2]))) - 1)
    return np.clip(stacking._fuse_tile(frames, levels) * 255., 0, 255).astype(np.uint8)


def _shallow(paths, **kwargs):
    """The pre-2026-09-20 algorithm: tiles only, no coarse base."""
    real = stacking._coarse_frames
    try:
        stacking._coarse_frames = lambda *args, **kwargs_: None
        return stacking.hdr_merge(paths, **kwargs)
    finally:
        stacking._coarse_frames = real


def test_the_tiled_merge_equals_a_full_depth_non_tiled_fusion(tmp_path):
    """The whole claim of the bounded implementation, stated as an equivalence.

    The tiled merge exists to hold memory flat, not to be a different algorithm. Whatever a
    single non-tiled full-depth pyramid would have produced is what it must produce.
    """
    paths, frames = _bracket(tmp_path)
    merged = stacking.hdr_merge(paths, deghost=False, tonality=0.0, recover=False)
    difference = np.abs(merged.astype(np.float32) - _reference_fusion(frames).astype(np.float32))
    assert difference.max() == 0, f"tiled merge drifted from the reference: max {difference.max()}"


def test_the_shallow_pyramid_is_what_that_equivalence_catches(tmp_path):
    """Pin the defect, so the equivalence above cannot quietly stop testing anything."""
    paths, frames = _bracket(tmp_path)
    reference = _reference_fusion(frames).astype(np.float32)
    shallow = _shallow(paths, deghost=False, tonality=0.0, recover=False).astype(np.float32)
    assert np.abs(shallow - reference).mean() > 10, "the shallow path should be visibly different"


def test_the_coarse_level_matches_the_whole_image_pyramid(tmp_path):
    """Interiors are exact because of the halo, so stitching them reproduces the real level.

    If this drifts, every tile gets a subtly wrong base and seams appear.
    """
    paths, _frames = _bracket(tmp_path)
    with stacking._staged_stack(paths, hdr=True) as (files, shape, _histograms):
        levels = 4
        coarse = stacking._coarse_frames(files, shape, levels)
        assert coarse is not None, "this geometry divides evenly; the fallback should not fire"
        images, weights = coarse
        expected_weights = stacking._fusion_weights(
            [np.array(np.memmap(path, dtype=np.uint8, mode="r", shape=shape)) for path in files])
        for index, path in enumerate(files):
            whole = stacking._unit(np.array(np.memmap(path, dtype=np.uint8, mode="r", shape=shape)))
            inner = (slice(2, -2), slice(2, -2))
            assert np.abs(images[index][inner]
                          - stacking._gaussian_pyramid(whole, levels)[-1][inner]).max() < 2e-3
            # The weights must be the full-resolution maps downsampled, NOT maps recomputed
            # on a downsampled frame: |Laplacian| of a small image is a different quantity.
            assert np.abs(weights[index][inner]
                          - stacking._gaussian_pyramid(expected_weights[index],
                                                       levels)[-1][inner]).max() < 2e-3


def test_the_merge_holds_the_window_the_camera_could_not(tmp_path):
    """Fusion must still earn its place: the clipped window comes back."""
    paths, frames = _bracket(tmp_path)
    luma = lambda image: cv2.cvtColor(image, cv2.COLOR_BGR2GRAY).astype(np.float32)
    clipped = lambda image: float((luma(image) >= 250).mean())
    assert clipped(stacking.hdr_merge(paths)) < clipped(frames[2])


def test_an_ill_fitting_base_is_refused_rather_than_resized():
    """A silently resized base would misalign the low frequencies between adjacent tiles."""
    images = [np.full((64, 64, 3), value, dtype=np.uint8) for value in (60, 120, 200)]
    with pytest.raises(ValueError, match="does not match pyramid base"):
        stacking._fuse_tile(images, 4, base=np.zeros((3, 3, 3), dtype=np.float32))
