"""Deterministic checks for the JPEG-domain sensor-noise calibration tool (no camera)."""
from __future__ import annotations

import sys
from fractions import Fraction
from pathlib import Path

import numpy as np
import pytest
from PIL import Image

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))
sys.path.insert(0, str(ROOT / "backend"))

import calibrate_sensor_noise as cal
import sensor_noise

# Injected 8-bit pixel noise per ISO; strongly increasing so the ceiling crossing is unambiguous.
LADDER = {100: 3.0, 200: 4.0, 400: 6.0, 800: 12.0, 1600: 24.0, 3200: 40.0}


def _write_frame(path: Path, iso: int, sigma_px: float, *, ev100=8.0, f=4.0,
                 shutter_override=None) -> None:
    """A flat mid-tone frame with seeded gaussian noise and matching EXIF. Shutter compensates
    ISO so the metered exposure (achieved EV100) is held constant unless overridden."""
    shutter_s = shutter_override if shutter_override is not None else f * f / (2 ** ev100 * iso / 100)
    rng = np.random.default_rng(iso)                     # seeded -> byte-deterministic
    base = np.full((512, 640), 120.0)
    arr = np.clip(base + rng.normal(0, sigma_px, base.shape), 0, 255).astype(np.uint8)
    image = Image.fromarray(arr, "L").convert("RGB")
    exif = Image.Exif()
    ifd = exif.get_ifd(0x8769)
    ifd[0x8827] = int(iso)                               # ISOSpeedRatings
    frac = Fraction(shutter_s).limit_denominator(1_000_000)
    ifd[0x829A] = (frac.numerator, frac.denominator)     # ExposureTime
    ifd[0x829D] = float(f)                               # FNumber
    exif[0x8769] = ifd
    image.save(path, quality=98, subsampling=0, exif=exif)


def _ladder_dir(tmp_path: Path, overrides: dict | None = None) -> Path:
    overrides = overrides or {}
    directory = tmp_path / "ladder"
    directory.mkdir(parents=True)
    for iso, sigma in LADDER.items():
        _write_frame(directory / f"iso{iso}.jpg", iso, sigma, shutter_override=overrides.get(iso))
    return directory


def _build(directory: Path):
    return cal.build_profile(directory, body="ILCE-7M3", lens="FE 24-105mm F4 G OSS",
                             cells=cal.DEFAULT_CELLS, tolerance_stops=cal.DEFAULT_TOLERANCE_STOPS,
                             threshold_ratio=cal.DEFAULT_THRESHOLD_RATIO)


def test_exif_exposure_reader_recovers_iso_shutter_and_aperture(tmp_path):
    path = tmp_path / "f.jpg"
    _write_frame(path, 400, 5.0)
    exposure = cal.read_exposure(path)
    assert exposure["iso"] == 400
    assert exposure["f_number"] == pytest.approx(4.0)
    assert exposure["shutter_s"] == pytest.approx(4 * 4 / (2 ** 8 * 4), rel=1e-3)


def test_noise_table_is_monotonic_and_yields_a_usable_ceiling(tmp_path):
    profile = _build(_ladder_dir(tmp_path))
    isos = [row["iso"] for row in profile["noise_table"]]
    lumas = [row["sigma_luma_8bit"] for row in profile["noise_table"]]
    assert isos == sorted(LADDER)                                  # sorted by ISO
    assert all(b > a for a, b in zip(lumas, lumas[1:])), lumas     # noise rises with ISO
    assert all(row["sigma_luma_8bit"] > 0 for row in profile["noise_table"])
    # Base sigma * 2.5 bar: the heavily-noised high ISOs are excluded, base ISO always allowed.
    assert profile["base_iso"] == 100
    assert profile["max_usable_iso"] in isos
    assert 100 <= profile["max_usable_iso"] < 800


def test_profile_schema_and_provenance_are_complete(tmp_path):
    profile = _build(_ladder_dir(tmp_path))
    assert profile["body"] == "ILCE-7M3"
    assert profile["sensor"]["width_px"] == 6000
    for key in ("base_iso", "max_usable_iso", "ceiling_threshold_ratio", "noise_table", "provenance"):
        assert key in profile
    prov = profile["provenance"]
    assert prov["method"] == "jpeg_flat_patch_residual"
    assert prov["lens"] == "FE 24-105mm F4 G OSS"
    assert "not RAW read noise" in prov["limitations"]
    assert len(prov["frames"]) == len(LADDER)
    assert all(len(frame["sha256"]) == 64 for frame in prov["frames"])
    assert all({"iso", "sigma_luma_8bit", "sigma_chroma_8bit"} <= row.keys()
               for row in profile["noise_table"])


def test_build_is_deterministic(tmp_path):
    first = _build(_ladder_dir(tmp_path / "a"))
    second = _build(_ladder_dir(tmp_path / "b"))
    assert first["noise_table"] == second["noise_table"]
    assert first["max_usable_iso"] == second["max_usable_iso"]


def test_inconsistent_exposure_is_refused(tmp_path):
    # Double one frame's shutter -> +1 stop metered exposure -> outside the tolerance band.
    directory = _ladder_dir(tmp_path, {800: 4 * 4 / (2 ** 8 * 800 / 100) * 2})
    with pytest.raises(ValueError, match="not held constant"):
        _build(directory)


def test_too_few_frames_is_refused(tmp_path):
    directory = tmp_path / "sparse"
    directory.mkdir()
    _write_frame(directory / "iso100.jpg", 100, 3.0)
    _write_frame(directory / "iso200.jpg", 200, 4.0)
    with pytest.raises(ValueError, match="at least 3"):
        _build(directory)


def test_profile_loads_back_into_the_measured_noise_model(tmp_path):
    profile = _build(_ladder_dir(tmp_path))
    measured = sensor_noise.load_measured_noise(profile)
    assert measured.max_usable_iso() == profile["max_usable_iso"]
    assert measured.base_iso == profile["base_iso"]
    # Interpolation stays within the measured envelope.
    lo = profile["noise_table"][0]["sigma_luma_8bit"]
    hi = profile["noise_table"][-1]["sigma_luma_8bit"]
    assert lo <= measured.sigma_luma_8bit(500) <= hi


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q"]))
