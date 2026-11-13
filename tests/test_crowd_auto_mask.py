"""Automatic background-mask estimation feeding the explicit-mask recovery.

These tests reuse the visibility fixtures from test_crowd_recovery. The estimator
must recover the true background where it is a clear majority (75%), refuse to
claim success where the background is not a majority (25%/50%), never label a
moving subject as background, write masks the explicit recovery already accepts,
leave sources untouched, and be independent of row-band size.
"""
from pathlib import Path
import hashlib
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))
import cv2
import numpy as np
import pytest

import crowd_recovery
import crowd_auto_mask
from test_crowd_recovery import fixture_frames

NEVER_BOX = (slice(42, 55), slice(50, 65))


def _paths(images):
    return [str(p) for p in images]


def _mad(image, background, selector):
    if not np.any(selector):
        return 0.0
    return float(np.abs(image[selector].astype(np.float64) - background[selector].astype(np.float64)).mean())


def test_75_percent_recovers_true_background_with_majority_support(tmp_path):
    background, images, _ = fixture_frames(tmp_path / "src", visible=3)
    outcome = crowd_auto_mask.recover_background_auto(_paths(images), tmp_path / "out")
    result = outcome["result"]

    assert outcome["status"] == "ok"
    # The clean majority-of-three pixels are recovered exactly as the true background.
    clean = result.support_count == 3
    assert clean.mean() > 0.9
    assert np.array_equal(result.image[clean], background[clean])
    # Recovery is exact wherever the method claims validity; the few undecided
    # pixels are explicit never-observed placeholders, not silent guesses.
    valid = result.valid_mask == 255
    assert _mad(result.image, background, valid) < 1.0
    # The pixels no frame ever shows as background stay explicitly unknown.
    assert np.all(result.support_count[NEVER_BOX] == 0)
    assert np.all(result.never_observed_mask[NEVER_BOX] == 255)
    assert np.all(result.image[NEVER_BOX] == 0)
    # Provenance is the automatic estimator, and the recovery's own caller-asserted
    # string is preserved rather than overwritten.
    assert outcome["evidence"]["background_labels"] == crowd_auto_mask.PROVENANCE
    assert outcome["evidence"]["recovery_background_labels"].startswith("caller_asserted")


def test_50_percent_reports_insufficient_not_false_success(tmp_path):
    _, images, _ = fixture_frames(tmp_path / "src", visible=2)
    outcome = crowd_auto_mask.recover_background_auto(_paths(images), tmp_path / "out")
    result = outcome["result"]

    # At exactly 50% visibility the per-pixel median is not a trustworthy majority
    # background, so the estimator must not claim full success.
    assert outcome["status"] == "insufficient_background_visibility"
    assert outcome["low_confidence_fraction"] > 0.5
    assert outcome["estimator"]["fraction_pixels_with_majority_support"] < 0.5
    # Undecided pixels are consistent explicit placeholders, never fabricated.
    assert np.all(result.image[result.support_count == 0] == 0)
    assert np.all(result.support_count[NEVER_BOX] == 0)


def test_25_percent_insufficient_and_no_false_success(tmp_path):
    background, images, _ = fixture_frames(tmp_path / "src", visible=1)
    outcome = crowd_auto_mask.recover_background_auto(_paths(images), tmp_path / "out")
    result = outcome["result"]

    # The crowd is the majority: the median is the crowd, and every crowd frame
    # agrees with it. The stability guard must catch this and refuse.
    assert outcome["status"] == "insufficient_background_visibility"
    assert outcome["low_confidence_fraction"] > 0.5
    assert outcome["estimator"]["fraction_never_observed"] > 0.9
    # No false success: it neither recovers the true background nor passes the
    # crowd off as verified background.
    assert not np.array_equal(result.image, background)
    assert (result.valid_mask == 255).mean() < 0.05


def test_moving_white_box_recovers_background_and_flags_subject(tmp_path):
    rng = np.random.default_rng(42)
    size, box = 256, 48
    background = rng.integers(70, 186, (size, size, 3), dtype=np.uint8)
    positions = [(10, 10), (10, 150), (150, 10), (150, 150), (80, 80), (40, 190), (190, 40)]
    src = tmp_path / "src"
    src.mkdir()
    paths = []
    for index, (top, left) in enumerate(positions):
        frame = background.copy()
        frame[top:top + box, left:left + box] = 255
        path = src / f"frame_{index:02d}.png"
        assert cv2.imwrite(str(path), frame)
        paths.append(str(path))
    before = {p: hashlib.sha256(Path(p).read_bytes()).hexdigest() for p in paths}

    outcome = crowd_auto_mask.recover_background_auto(paths, tmp_path / "out")
    result = outcome["result"]

    assert outcome["status"] == "ok"
    valid = result.valid_mask == 255
    assert valid.mean() > 0.95
    assert _mad(result.image, background, valid) < 1.0
    # In each frame, the region occupied by the box must be labeled non-background.
    for index, (top, left) in enumerate(positions):
        mask = cv2.imread(outcome["mask_paths"][index], cv2.IMREAD_GRAYSCALE)
        box_region = mask[top:top + box, left:left + box]
        assert (box_region == 255).mean() < 0.01
    # Sources are never modified by estimation or recovery.
    after = {p: hashlib.sha256(Path(p).read_bytes()).hexdigest() for p in paths}
    assert before == after


def test_masks_are_valid_for_explicit_read_mask(tmp_path):
    _, images, _ = fixture_frames(tmp_path / "src", visible=3)
    estimate = crowd_auto_mask.estimate_background_masks(_paths(images), tmp_path / "out")
    height, width = estimate.shape
    assert len(estimate.mask_paths) == len(images)
    for path in estimate.mask_paths:
        pixels = cv2.imread(str(path), cv2.IMREAD_UNCHANGED)
        assert pixels.ndim == 2 and pixels.shape == (height, width)
        assert np.all((pixels == 0) | (pixels == 255))
        # The explicit recovery's own strict reader must accept every mask.
        mask, receipt = crowd_recovery._read_mask(path, (width, height))
        assert set(np.unique(mask)).issubset({0, 1})
        assert receipt["sha256"]


def _crowd_stack(root, occupied_frames, region, n_frames=10, size=(40, 60), seed=17):
    """Write ``n_frames`` registered PNGs sharing one background.

    Inside ``region`` (a (rows, cols) slice pair) the frames listed in ``occupied_frames`` carry a
    colour-varying occupant (a moving crowd of different people); the others show the background.
    Everywhere outside ``region`` every frame shows the background, so it clears trivially.
    """
    root.mkdir(parents=True, exist_ok=True)
    rng = np.random.default_rng(seed)
    background = rng.integers(30, 220, (size[0], size[1], 3), dtype=np.uint8)
    occupied = set(occupied_frames)
    paths = []
    for index in range(n_frames):
        frame = background.copy()
        if index in occupied:
            # A different colour each frame, spanning the range, so the median at this region is a
            # coincidental mix its supporters disagree on — the estimator's contamination guard.
            colour = np.array([255 * index // (n_frames - 1),
                               255 * (n_frames - 1 - index) // (n_frames - 1), 128], np.uint8)
            frame[region[0], region[1]] = colour
        path = root / f"frame_{index:02d}.png"
        assert cv2.imwrite(str(path), frame)
        paths.append(str(path))
    return background, paths


def test_static_occupant_in_ninety_percent_at_one_spot_is_reported_not_cleared(tmp_path):
    region = (slice(10, 20), slice(20, 35))  # 10x15 = 150 of 2400 px (6.25%)
    _, paths = _crowd_stack(tmp_path / "src", occupied_frames=range(9), region=region, n_frames=10)
    floor = crowd_auto_mask.min_frames_for_visibility(10)  # 3 of 10 == >= 25%
    outcome = crowd_auto_mask.recover_background_auto(paths, tmp_path / "out", minimum_observations=floor)
    support = np.asarray(outcome["result"].support_count)

    # Background peeked through only 1 of 10 frames here (< 25%): the spot cannot be cleared, and
    # the output must NOT pass the moving occupants off as recovered background.
    assert (support[region[0], region[1]] < floor).mean() > 0.9
    assert (outcome["result"].valid_mask[region[0], region[1]] == 0).mean() > 0.9
    # Everywhere the crowd never stood clears fine.
    outside = np.ones(support.shape, bool)
    outside[region[0], region[1]] = False
    assert (support[outside] >= floor).mean() > 0.95
    # The honest report names the uncleared share (~6% of the frame).
    assert 0.03 < outcome["fraction_not_cleared"] < 0.12
    assert abs(outcome["fraction_revealed"] + outcome["fraction_not_cleared"] - 1.0) < 1e-9
    assert outcome["evidence"]["fraction_not_cleared"] == outcome["fraction_not_cleared"]


def test_occupant_in_forty_percent_is_cleared(tmp_path):
    region = (slice(10, 20), slice(20, 35))
    # Occupant present in 4 of 10 frames -> background visible in 6 (60% >= 25%).
    _, paths = _crowd_stack(tmp_path / "src", occupied_frames=[0, 1, 2, 3], region=region, n_frames=10)
    floor = crowd_auto_mask.min_frames_for_visibility(10)
    outcome = crowd_auto_mask.recover_background_auto(paths, tmp_path / "out", minimum_observations=floor)
    support = np.asarray(outcome["result"].support_count)

    assert (support[region[0], region[1]] >= floor).mean() > 0.9
    assert outcome["fraction_revealed"] > 0.98 and outcome["fraction_not_cleared"] < 0.02


def test_static_scene_reports_fully_revealed(tmp_path):
    # No occupant anywhere: the background is visible in every frame, so ~100% clears.
    _, paths = _crowd_stack(tmp_path / "src", occupied_frames=[], region=(slice(0, 1), slice(0, 1)), n_frames=10)
    floor = crowd_auto_mask.min_frames_for_visibility(10)
    outcome = crowd_auto_mask.recover_background_auto(paths, tmp_path / "out", minimum_observations=floor)
    assert outcome["fraction_revealed"] > 0.99 and outcome["fraction_not_cleared"] < 0.01


def test_motionless_person_is_not_reported_as_uncleared_so_the_summary_states_the_limit(tmp_path):
    import stack_session
    # A person who never moves: identical pixels in every frame. The estimator cannot tell them from
    # the background, so the uncleared share stays low while they remain in the photo; the summary
    # must say so instead of implying everything was cleared.
    rng = np.random.default_rng(23)
    frame = rng.integers(30, 220, (40, 60, 3), dtype=np.uint8)
    frame[10:30, 25:35] = [200, 40, 180]  # the motionless person, same in every frame
    paths = []
    for index in range(6):
        path = tmp_path / f"frame_{index:02d}.png"
        assert cv2.imwrite(str(path), frame)
        paths.append(str(path))
    receipt = stack_session._merge_crowd_remove(paths, str(tmp_path / "result.jpg"))
    assert receipt["not_cleared_fraction"] < 0.05
    assert crowd_auto_mask.STATIC_SUBJECT_LIMIT in receipt["summary"]
    assert receipt["summary"].endswith(
        "Anyone who stayed still in every frame can't be told apart from the background and will remain.")


def test_min_frames_for_visibility_is_the_twenty_five_percent_floor():
    assert crowd_auto_mask.MIN_BACKGROUND_VISIBLE_FRACTION == 0.25
    assert crowd_auto_mask.min_frames_for_visibility(4) == 1
    assert crowd_auto_mask.min_frames_for_visibility(8) == 2
    assert crowd_auto_mask.min_frames_for_visibility(10) == 3
    for bad in (0, -1, 2.5, True):
        with pytest.raises(ValueError):
            crowd_auto_mask.min_frames_for_visibility(bad)


def test_row_band_processing_matches_whole_image_bit_for_bit(tmp_path):
    _, images, _ = fixture_frames(tmp_path / "src", visible=3)
    height = 97
    whole = crowd_auto_mask.estimate_background_masks(_paths(images), tmp_path / "whole", tile_rows=height)
    banded = crowd_auto_mask.estimate_background_masks(_paths(images), tmp_path / "banded", tile_rows=11)

    assert np.array_equal(whole.support_count, banded.support_count)
    for a, b in zip(whole.mask_paths, banded.mask_paths):
        assert np.array_equal(cv2.imread(str(a), cv2.IMREAD_GRAYSCALE),
                              cv2.imread(str(b), cv2.IMREAD_GRAYSCALE))
