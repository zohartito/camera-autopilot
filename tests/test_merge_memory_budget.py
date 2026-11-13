"""Per-merge memory budget: the peak estimator, the refusal guard and the streamed crowd merge.

The budget (stacking.MAX_STACK_WORKING_BYTES, 2 GiB) is what one merge may add to its process on
the 8 GB CM5. These tests pin that every job the validators accept is estimated inside it, that
the estimate stays above the peaks measured on the Mac, that an oversized job is refused with a
reason before any pixel is decoded, and that streaming the automatic crowd merge through disk
did not change a single output pixel. Peak RSS itself is measured by tools/puck_workload_bench.py
in fresh processes; the in-suite RSS check is opt-in (see the last test).
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))

import cv2  # noqa: E402
import numpy as np  # noqa: E402
import pytest  # noqa: E402

import crowd_auto_mask  # noqa: E402
import image_safety  # noqa: E402
import stack_session  # noqa: E402
import stacking  # noqa: E402

MODES = ("long", "crowd", "focus", "hdr", "night", "crowd_auto")
GIB = 1024 ** 3
# The largest frame the decoder accepts (image_safety.MAX_IMAGE_PIXELS) at 3:2, both sides
# divisible by 16 so the HDR coarse path applies.
MAX_WIDTH, MAX_HEIGHT = 7728, 5152
# 40 MP at the sensor shapes cameras write: 3:2, 4:3, 16:9 and 1:1.
SENSOR_SHAPES = [(7728, 5152), (7296, 5472), (8400, 4725), (6320, 6320)]
MAX_COUNT = {"crowd_auto": stack_session.CROWD_AUTO_MAX_FRAMES}


def _max_count(mode: str) -> int:
    return MAX_COUNT.get(mode, stack_session.MAX_STACK_FRAMES)


def test_budget_is_two_gib():
    assert stacking.MAX_STACK_WORKING_BYTES == 2 * GIB


@pytest.mark.parametrize("mode", MODES)
def test_estimate_is_monotone_in_size_and_count(mode):
    # The largest accepted job bounds every smaller one only if this holds.
    base = stacking.estimate_merge_peak(mode, 3000, 2000, 4)
    assert stacking.estimate_merge_peak(mode, 3001, 2000, 4) >= base
    assert stacking.estimate_merge_peak(mode, 3000, 2001, 4) >= base
    assert stacking.estimate_merge_peak(mode, 3000, 2000, 5) > base


def test_unknown_mode_is_refused():
    with pytest.raises(ValueError, match="unknown merge mode"):
        stacking.estimate_merge_peak("panorama", 100, 100, 2)


@pytest.mark.parametrize("width,height", SENSOR_SHAPES)
@pytest.mark.parametrize("mode", MODES)
def test_largest_accepted_camera_frame_fits_the_budget(mode, width, height):
    # Camera-shaped frames only: the crowd estimator's row bands grow with width, so an extreme
    # strip of the same area (40000x1000) is estimated over budget and refused, by design.
    assert width * height <= image_safety.MAX_IMAGE_PIXELS
    estimate = stacking.estimate_merge_peak(mode, width, height, _max_count(mode))
    assert estimate <= stacking.MAX_STACK_WORKING_BYTES, f"{mode}: {estimate / GIB:.2f} GiB"


def test_band_height_is_part_of_the_crowd_estimate(tmp_path, monkeypatch):
    # A caller-chosen band taller than the default must be charged for before decode.
    assert (stacking.estimate_merge_peak("crowd_auto", 8000, 5000, 8, band_rows=5000)
            > stacking.estimate_merge_peak("crowd_auto", 8000, 5000, 8))
    paths = _write(tmp_path, [np.full((32, 48, 3), 90 + index, np.uint8) for index in range(8)])
    monkeypatch.setattr(image_safety, "image_size", lambda path: (8000, 5000))
    monkeypatch.setattr(stacking, "MAX_STACK_WORKING_BYTES",
                        stacking.estimate_merge_peak("crowd_auto", 8000, 5000, 8))
    monkeypatch.setattr(stacking, "_read_one", lambda *args: pytest.fail("decoded before the memory guard"))
    with pytest.raises(ValueError, match="per-merge budget"):
        crowd_auto_mask.estimate_background_masks(paths, tmp_path / "masks", tile_rows=5000)


# Fresh-process peak RSS, the largest of the runs before and after this change, minus the
# 0.06 GiB import-only process; Apple M4 Max, tools/puck_workload_bench.py, receipts in
# artifacts/tiled-budget/. The estimate must stay at least 20 % above every one of them.
MEASURED_MAC_GIB = [
    ("hdr", 4240, 2832, 3, 0.35), ("hdr", 6000, 4000, 3, 0.57), ("hdr", 6000, 4000, 5, 0.60),
    ("hdr", 6000, 4000, 9, 0.66), ("hdr", 7728, 5152, 3, 0.91), ("hdr", 7728, 5152, 16, 0.94),
    # With the recovery motion repair rewriting brackets (F01; synthetic window + a subject in
    # every dark bracket, receipts in artifacts/hdrghost/bench*_b*.json).
    ("hdr", 6000, 4000, 9, 0.73), ("hdr", 7728, 5152, 16, 1.43),
    ("night", 4240, 2832, 16, 0.37), ("night", 6000, 4000, 8, 0.70), ("night", 6000, 4000, 16, 0.71),
    ("night", 7728, 5152, 16, 0.87),
    ("long", 4240, 2832, 16, 0.16), ("long", 6000, 4000, 16, 0.29), ("long", 7728, 5152, 16, 0.45),
    ("crowd", 6000, 4000, 16, 0.38), ("crowd", 7728, 5152, 16, 0.56),
    ("focus", 6000, 4000, 5, 0.42), ("focus", 7728, 5152, 5, 0.55),
    # The streamed session crowd merge (it measured 1.42 and 2.24 GiB at 24 and 40 MP before).
    ("crowd_auto", 4240, 2832, 8, 0.36), ("crowd_auto", 6000, 4000, 8, 0.75),
    ("crowd_auto", 7728, 5152, 8, 1.02),
]


@pytest.mark.parametrize("mode,width,height,count,measured_gib", MEASURED_MAC_GIB)
def test_estimate_stays_above_measured_peaks(mode, width, height, count, measured_gib):
    assert stacking.estimate_merge_peak(mode, width, height, count) >= 1.2 * measured_gib * GIB


def _write(tmp_path, images, suffix=".png"):
    paths = []
    for index, image in enumerate(images):
        path = tmp_path / f"frame_{index:02d}{suffix}"
        assert cv2.imwrite(str(path), image)
        paths.append(str(path))
    return paths


MERGES = {
    "long": lambda paths: stacking.long_exposure(paths),
    "night": lambda paths: stacking.long_exposure_with_report(paths, deghost=True),
    "hdr": lambda paths: stacking.hdr_merge(paths),
    "focus": lambda paths: stacking.focus_stack(paths),
    "crowd": lambda paths: stacking.crowd_remove(paths),
    "crowd_auto": None,  # the session merge, below
}


@pytest.mark.parametrize("mode", MODES)
def test_oversized_job_is_refused_with_a_reason_before_decode(tmp_path, monkeypatch, mode):
    paths = _write(tmp_path, [np.full((32, 48, 3), 90 + index, np.uint8) for index in range(3)])
    monkeypatch.setattr(image_safety, "image_size", lambda path: (MAX_WIDTH, MAX_HEIGHT))
    estimate = stacking.estimate_merge_peak(mode, MAX_WIDTH, MAX_HEIGHT, 3)
    # One byte short of this mode's own estimate: a check that used another mode's (smaller)
    # coefficients, or no check at all, would reach the decoder.
    monkeypatch.setattr(stacking, "MAX_STACK_WORKING_BYTES", estimate - 1)
    monkeypatch.setattr(stacking, "_read_one", lambda *args: pytest.fail("decoded before the memory guard"))
    with pytest.raises(ValueError) as refused:
        if mode == "crowd_auto":
            stack_session._merge_crowd_remove(paths, str(tmp_path / "out" / "result.jpg"))
        else:
            MERGES[mode](paths)
    message = str(refused.value)
    assert "aggregate working-memory budget" in message
    assert f"a {mode} merge of 3 {MAX_WIDTH}x{MAX_HEIGHT} frames" in message
    assert f"{estimate / GIB:.2f} GiB" in message and "per-merge budget" in message


def _crowd_frames(count=6, height=600, width=704, seed=11):
    """A textured static scene larger than one 512 px tile and several 128-row bands, with a
    block that moves across it, so every frame has foreground somewhere else."""
    rng = np.random.default_rng(seed)
    background = cv2.GaussianBlur(rng.integers(30, 220, (height, width, 3), dtype=np.uint8), (5, 5), 0)
    frames = []
    for index in range(count):
        frame = background.copy()
        top = 40 + index * 90
        frame[top:top + 120, 60 + index * 100:180 + index * 100] = (20 + index * 30, 200, 240 - index * 25)
        frames.append(frame)
    return frames


def test_streamed_masks_match_the_single_band_reference(tmp_path):
    (tmp_path / "src").mkdir()
    paths = _write(tmp_path / "src", _crowd_frames())
    reference = crowd_auto_mask.estimate_background_masks(paths, tmp_path / "whole", tile_rows=600)
    for rows in (stacking.STACK_BAND_ROWS, 37):
        banded = crowd_auto_mask.estimate_background_masks(paths, tmp_path / f"rows{rows}", tile_rows=rows)
        assert np.array_equal(reference.support_count, banded.support_count)
        for whole, streamed in zip(reference.mask_paths, banded.mask_paths):
            assert np.array_equal(cv2.imread(str(whole), cv2.IMREAD_GRAYSCALE),
                                  cv2.imread(str(streamed), cv2.IMREAD_GRAYSCALE))
    # The fixture must actually exercise labels on both sides of the band edges.
    assert 0 < np.count_nonzero(reference.support_count < len(paths)) < reference.support_count.size


def test_band_accumulated_median_histogram_equals_the_whole_frame_one():
    rng = np.random.default_rng(4)
    median = rng.integers(0, 256, (601, 333, 3), dtype=np.uint8)
    histograms = [np.bincount(rng.integers(0, 256, 5000), minlength=256).astype(np.float64) for _ in range(3)]
    # The formula before streaming: one whole-frame gray conversion and bincount.
    gray = cv2.cvtColor(median, cv2.COLOR_BGR2GRAY)
    median_cdf = np.cumsum(np.bincount(gray.ravel(), minlength=256).astype(np.float64)) / gray.size
    expected = [np.searchsorted(median_cdf, np.cumsum(hist) / hist.sum()).clip(0, 255).astype(np.uint8)
                for hist in histograms]
    luts = crowd_auto_mask._global_luma_luts(None, median.shape, histograms, median)
    assert all(np.array_equal(lut, reference) for lut, reference in zip(luts, expected))


def test_crowd_merge_outputs_equal_the_in_memory_formulas(tmp_path, monkeypatch):
    (tmp_path / "src").mkdir()
    paths = _write(tmp_path / "src", _crowd_frames())
    captured, written = {}, {}
    original_auto = crowd_auto_mask.recover_background_auto

    def capture_outcome(*args, **kwargs):
        captured["outcome"] = original_auto(*args, **kwargs)
        return captured["outcome"]

    original_imwrite = cv2.imwrite

    def capture_write(path, image, *args):
        written[Path(path).name] = np.array(image, copy=True)
        return original_imwrite(path, image, *args)

    monkeypatch.setattr(crowd_auto_mask, "recover_background_auto", capture_outcome)
    monkeypatch.setattr(cv2, "imwrite", capture_write)
    out = tmp_path / "session" / "result.jpg"
    out.parent.mkdir()
    receipt = stack_session._merge_crowd_remove(paths, str(out))
    result = captured["outcome"]["result"]
    # The formulas the merge used before it was streamed: np.where into a third frame, and
    # uint32 / int64 map arithmetic.
    fallback = stacking.crowd_remove(paths)
    expected = np.where((result.valid_mask == 255)[:, :, None], result.image, fallback)
    assert np.array_equal(written["result.jpg"], expected)
    support = np.asarray(result.support_count, dtype=np.uint32)
    floor = crowd_auto_mask.min_frames_for_visibility(len(paths))
    assert np.array_equal(written["support_count.png"], (support * 255 // len(paths)).astype(np.uint8))
    assert np.array_equal(written["never_observed.png"], ((support == 0) * 255).astype(np.uint8))
    assert np.array_equal(written["not_cleared.png"], ((support < floor) * 255).astype(np.uint8))
    assert receipt["not_cleared_fraction"] == float((support < floor).mean())
    assert receipt["revealed_fraction"] == float((support >= floor).mean())
    # The recovery must have filled part of the frame and left part to the fallback.
    valid = result.valid_mask == 255
    assert 0 < np.count_nonzero(valid) < valid.size
    assert np.any(written["support_count.png"] == 255)


@pytest.mark.skipif(os.environ.get("CAMERA_MEMORY_TESTS") != "1",
                    reason="peak RSS on a shared dev host moves with allocator retention and "
                           "concurrent load; set CAMERA_MEMORY_TESTS=1 to run the 24 MP fresh-process "
                           "check (receipts: tools/puck_workload_bench.py)")
@pytest.mark.parametrize("mode", ["hdr", "night", "crowd_auto"])
def test_measured_24mp_peak_stays_under_the_estimate(tmp_path, mode):
    rng = np.random.default_rng(3)
    base = cv2.GaussianBlur(rng.integers(20, 235, (4000, 6000, 3), dtype=np.uint8), (0, 0), 1.5)
    count = 8 if mode != "hdr" else 3
    images = []
    for index in range(count):
        frame = np.roll(base, index * 3, axis=1)
        if mode == "hdr":
            frame = np.clip(frame.astype(np.float32) * (0.5 + 0.5 * index), 0, 255).astype(np.uint8)
        images.append(frame)
    paths = _write(tmp_path, images, suffix=".jpg")
    del images, base
    baseline = subprocess.run(
        [sys.executable, "-c", "import resource,sys; sys.path.insert(0, sys.argv[1]); "
         "import cv2, numpy, stacking, stack_session, crowd_auto_mask; "
         "print(resource.getrusage(resource.RUSAGE_SELF).ru_maxrss)", str(ROOT / "backend")],
        capture_output=True, text=True, check=True)
    scale = 1024 if sys.platform == "linux" else 1
    baseline_bytes = int(baseline.stdout) * scale
    receipt = tmp_path / "receipt.json"
    subprocess.run([sys.executable, str(ROOT / "tools/puck_workload_bench.py"), *paths,
                    "--mode", mode, "--timeout", "600", "--output", str(receipt)], check=True)
    run = json.loads(receipt.read_text())["runs"][0]
    assert run["status"] == "completed", run
    estimate = stacking.estimate_merge_peak(mode, 6000, 4000, count)
    assert run["peak_rss_bytes"] - baseline_bytes <= estimate
