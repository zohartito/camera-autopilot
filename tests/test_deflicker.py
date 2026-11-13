"""Camera-free proofs for Holy-Grail timelapse deflicker.

Synthetic solid-colour JPEG frames stand in for a real day-to-night ramp: a solid frame's
mean luminance is (essentially) its fill value, so we can construct an exact log-luma ramp
with a known +/- alternating flicker, correct it, and measure the residual. No hardware.
"""
import hashlib
import math
from pathlib import Path
import sys

import pytest
from PIL import Image

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))
import deflicker


def _log2(value):
    return math.log2(value)


def _solid(path, value):
    Image.new("RGB", (96, 64), (value, value, value)).save(path, format="JPEG", quality=95)
    return path


def _flicker_frames(tmp_path, n=12, base=70.0, ramp_stops=0.03, flicker_stops=0.3):
    """n solid frames on a gentle log-luma ramp with a per-frame +/- flicker."""
    paths, base_log = [], []
    for i in range(n):
        base_log.append(_log2(base) + ramp_stops * i)
        value = round(2 ** (base_log[-1] + flicker_stops * (-1) ** i))
        paths.append(str(_solid(tmp_path / f"frame_{i:04d}.jpg", max(1, min(255, value)))))
    return paths, base_log


# --- deflicker_gains: the numeric heart -------------------------------------

def test_flicker_is_removed_ramp_preserved_endpoints_unbiased(tmp_path):
    ramp_stops = 0.03
    paths, base_log = _flicker_frames(tmp_path, ramp_stops=ramp_stops)
    luma = deflicker.luminance_series(paths)
    gains = deflicker.deflicker_gains(luma, window=7, max_gain_stops=1.0)
    corrected = [math.log2(luma[i]) + math.log2(gains[i]) for i in range(len(luma))]
    # Interior residual frame-to-frame deviation (minus the expected ramp step) is tiny.
    interior = [abs((corrected[i + 1] - corrected[i]) - ramp_stops) for i in range(1, len(corrected) - 2)]
    assert max(interior) < 0.05
    # Endpoints stay within 0.1 stop of the underlying (flicker-free) ramp trend.
    assert abs(corrected[0] - base_log[0]) < 0.1
    assert abs(corrected[-1] - base_log[-1]) < 0.1
    # The overall upward ramp is preserved, not flattened.
    assert corrected[-1] - corrected[0] > 0.5 * (base_log[-1] - base_log[0])


def test_smooth_series_yields_unit_gains():
    gains = deflicker.deflicker_gains([120.0] * 9, window=7, max_gain_stops=1.0)
    assert gains == [1.0] * 9
    # A gentle flicker-free ramp is likewise left essentially untouched.
    ramp = [2 ** (6.0 + 0.02 * i) for i in range(10)]
    assert max(abs(math.log2(g)) for g in deflicker.deflicker_gains(ramp)) < 0.05


def test_gains_are_clamped_to_max_gain_stops():
    spiky = [8.0, 250.0, 8.0, 250.0, 8.0, 250.0, 8.0]
    for cap in (0.25, 0.5, 1.0):
        gains = deflicker.deflicker_gains(spiky, window=5, max_gain_stops=cap)
        assert max(abs(math.log2(g)) for g in gains) <= cap + 1e-9


def test_deflicker_gains_is_deterministic():
    luma = [40.0, 82.0, 41.0, 80.0, 43.0, 79.0, 44.0]
    assert deflicker.deflicker_gains(luma, window=5) == deflicker.deflicker_gains(luma, window=5)


def test_empty_series_and_single_frame():
    assert deflicker.deflicker_gains([]) == []
    assert deflicker.deflicker_gains([123.0]) == [1.0]


@pytest.mark.parametrize("window", [2, 0, -1, 8, 4])
def test_invalid_window_rejected(window):
    with pytest.raises(ValueError):
        deflicker.deflicker_gains([10.0, 20.0, 30.0], window=window)


@pytest.mark.parametrize("cap", [0.0, -1.0, float("inf"), float("nan")])
def test_invalid_max_gain_stops_rejected(cap):
    with pytest.raises(ValueError):
        deflicker.deflicker_gains([10.0, 20.0, 30.0], max_gain_stops=cap)


# --- luminance_series: bounded, deterministic measurement --------------------

def test_luminance_series_tracks_brightness_and_is_deterministic(tmp_path):
    paths = [str(_solid(tmp_path / f"f{i}.jpg", value)) for i, value in enumerate((30, 90, 200))]
    first = deflicker.luminance_series(paths)
    assert first == deflicker.luminance_series(paths)  # deterministic
    assert first[0] < first[1] < first[2]  # monotonic with fill value
    assert all(abs(measured - expected) < 3 for measured, expected in zip(first, (30, 90, 200)))


# --- apply_gains_to_frames: non-destructive, scaled, auditable ---------------

def test_apply_leaves_originals_byte_identical(tmp_path):
    paths = [str(_solid(tmp_path / f"frame_{i:04d}.jpg", 60 + i * 20)) for i in range(3)]
    before = [hashlib.sha256(Path(p).read_bytes()).hexdigest() for p in paths]
    out_dir = tmp_path / "deflicker"
    deflicker.apply_gains_to_frames(paths, [1.5, 0.5, 1.0], out_dir)
    after = [hashlib.sha256(Path(p).read_bytes()).hexdigest() for p in paths]
    assert before == after
    assert {p.name for p in out_dir.iterdir()} == {"frame_0000.jpg", "frame_0001.jpg", "frame_0002.jpg"}


def test_apply_scales_luminance_and_clamps_highlights(tmp_path):
    paths = [str(_solid(tmp_path / "frame_0000.jpg", 100)), str(_solid(tmp_path / "frame_0001.jpg", 200))]
    produced = deflicker.apply_gains_to_frames(paths, [1.4, 1.6], tmp_path / "out")
    corrected = deflicker.luminance_series([str(p) for p in produced])
    assert abs(corrected[0] - 140) < 3            # 100 * 1.4
    assert 250 < corrected[1] <= 255              # 200 * 1.6 clamps at the ceiling


def test_apply_rejects_misaligned_gains(tmp_path):
    paths = [str(_solid(tmp_path / "frame_0000.jpg", 100))]
    with pytest.raises(ValueError):
        deflicker.apply_gains_to_frames(paths, [1.0, 1.1], tmp_path / "out")


def test_frame_receipts_record_source_gain_and_output_hashes(tmp_path):
    paths = [str(_solid(tmp_path / "frame_0000.jpg", 90)), str(_solid(tmp_path / "frame_0001.jpg", 110))]
    gains = [1.2, 0.9]
    produced = deflicker.apply_gains_to_frames(paths, gains, tmp_path / "out")
    receipts = deflicker.frame_receipts(paths, produced, gains)
    assert [r["gain"] for r in receipts] == gains
    for source, out, receipt in zip(paths, produced, receipts):
        assert receipt["source_sha256"] == hashlib.sha256(Path(source).read_bytes()).hexdigest()
        assert receipt["out_sha256"] == hashlib.sha256(Path(out).read_bytes()).hexdigest()
        assert receipt["source_sha256"] != receipt["out_sha256"]
