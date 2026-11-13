"""Body+lens rig resolution and its effect on the live brain and engine profile (no camera)."""
from __future__ import annotations

import json
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "backend"))

import brain
import camera_caps
import physics
import rig_profile
import sensor_noise
from test_final_wave_engine import rig  # shared fake-camera engine fixture

# A small measured JPEG-noise table: base sigma 2.0, 2.5x bar = 5.0 -> ceiling ISO 1600.
MEASURED = {
    "body": "ILCE-7M3", "base_iso": 100, "ceiling_threshold_ratio": 2.5,
    "sensor": {"width_mm": 35.6, "height_mm": 23.8, "width_px": 6000, "height_px": 4000},
    "noise_table": [{"iso": 100, "sigma_luma_8bit": 2.0, "sigma_chroma_8bit": 1.0},
                    {"iso": 400, "sigma_luma_8bit": 3.0, "sigma_chroma_8bit": 1.4},
                    {"iso": 1600, "sigma_luma_8bit": 4.5, "sigma_chroma_8bit": 2.0},
                    {"iso": 6400, "sigma_luma_8bit": 9.0, "sigma_chroma_8bit": 4.0}],
    "provenance": {"method": "jpeg_flat_patch_residual", "date": "2026-09-07",
                   "lens": "FE 24-105mm F4 G OSS", "frames": []},
}


def test_a7ii_with_body_exif_tamron_alias_uses_existing_profile():
    profile = rig_profile.resolve_rig(
        "Sony ILCE-7M2 (Control)", lens_model="E 28-75mm F2.8-2.8",
        lens_specification=(28, 75, 2.8, 2.8), max_aperture_value=2.96875)
    assert profile.calibration_status == "existing_profile"
    assert (profile.lens_limits.min_f, profile.lens_limits.max_f) == (2.8, 22.0)
    assert profile.noise is sensor_noise.A7II_NOISE
    assert profile.sensor is physics.A7II and profile.lens_profiled and profile.ibis


def test_a7ii_with_other_lens_stays_uncalibrated_and_does_not_borrow_noise():
    profile = rig_profile.resolve_rig("Sony ILCE-7M2 (Control)", lens_model="FE 50mm F1.8")
    assert profile.calibration_status == "uncalibrated"
    assert profile.noise is None and not profile.lens_profiled


def test_a7iii_without_json_is_geometry_only_with_spec_lens_limits(tmp_path):
    profile = rig_profile.resolve_rig(
        "Sony Alpha-A7 III (PC Control)", lens_model="FE 24-105mm F4 G OSS",
        lens_specification=(24, 105, 4, 4), profiles_dir=tmp_path)
    assert profile.calibration_status == "geometry_only"
    assert (profile.lens_limits.min_f, profile.lens_limits.max_f) == (4.0, 22.0)
    assert profile.lens_limits.focal_range == (24.0, 105.0)
    assert profile.noise is None and profile.sensor is physics.A7III and profile.ibis


def test_a7iii_with_measured_json_is_measured_with_ceiling_from_table(tmp_path):
    (tmp_path / "ilce-7m3.json").write_text(json.dumps(MEASURED))
    profile = rig_profile.resolve_rig(
        "Sony Alpha-A7 III (PC Control)", lens_model="FE 24-105mm F4 G OSS",
        lens_specification=(24, 105, 4, 4), profiles_dir=tmp_path)
    assert profile.calibration_status == "measured_jpeg_noise"
    assert profile.noise is not None and profile.noise.max_usable_iso() == 1600
    assert profile.sensor is physics.A7III


def test_unknown_body_is_uncalibrated_with_no_sensor():
    profile = rig_profile.resolve_rig("Nikon Z6", lens_model="Z 50mm f/1.8 S")
    assert profile.calibration_status == "uncalibrated"
    assert profile.sensor is None and profile.noise is None


def test_unknown_lens_derives_widest_aperture_from_exif_only():
    profile = rig_profile.resolve_rig(
        "Sony Alpha-A7 III (PC Control)", lens_model="Sigma 35mm F1.4 DG DN",
        lens_specification=(35, 35, 1.4, 1.4))
    assert profile.lens_limits.min_f == pytest.approx(1.4)
    assert profile.lens_limits.max_f is None                     # narrowest opening unknown
    assert profile.lens_limits.focal_range == (35.0, 35.0)


def _a7iii_caps():
    return camera_caps.from_live_choices(
        "Sony Alpha-A7 III (PC Control)",
        ["100", "200", "400", "800", "1600", "3200", "6400", "12800"],
        ["2.8", "4", "5.6", "8", "11", "16", "22"],
        ["1/8000", "1/2000", "1/1000", "1/500", "1/250", "1/125", "1/60", "1/30"])


def test_recommend_live_with_measured_rig_caps_iso_at_table_ceiling():
    caps = _a7iii_caps()
    measured = rig_profile.resolve_rig("Sony Alpha-A7 III (PC Control)")
    rig = rig_profile.RigProfile(
        body_id="ILCE-7M3", sensor=physics.A7III, noise=sensor_noise.load_measured_noise(MEASURED),
        lens_id="Sony FE 24-105mm F4 G OSS", lens_limits=measured.lens_limits, ibis=True,
        calibration_status="measured_jpeg_noise", sources=["measured_jpeg_flat_patch_noise"],
        notes=["A7 III with measured JPEG-domain noise."], lens_profiled=False)
    with patch.object(brain.embed_index, "recommend", return_value={"f_number": 4}):
        # A very dark scene would want ISO past the ceiling; the measured ceiling must bind.
        rec = brain.recommend_live("unused.jpg", -6, caps.name, caps, 4, 50, rig=rig)
    assert rec.iso <= 1600 and rec.iso in caps.isos
    assert any("measured" in note.lower() for note in rec.notes)
    assert rec.card("ISO").sub_factors[0].status == "pending"          # DR never measured here
    assert "1600" in rec.card("ISO").sub_factors[1].readout


def test_geometry_only_rig_keeps_the_conservative_iso6400_policy(tmp_path):
    caps = _a7iii_caps()
    rig = rig_profile.resolve_rig("Sony Alpha-A7 III (PC Control)",
                                  lens_model="FE 24-105mm F4 G OSS", profiles_dir=tmp_path)
    with patch.object(brain.embed_index, "recommend", return_value={"f_number": 4}):
        rec = brain.recommend_live("unused.jpg", -6, caps.name, caps, 4, 50, rig=rig)
    assert rec.iso <= 6400
    assert any("conservative" in note.lower() for note in rec.notes)
    assert rec.card("ISO").sub_factors[0].status == "pending"


def test_engine_profile_reports_geometry_only_for_a7iii(rig, tmp_path, monkeypatch):
    rig.camera.model = "Sony Alpha-A7 III (PC Control)"
    rig.camera.lens_model = "FE 24-105mm F4 G OSS"
    monkeypatch.setattr(rig_profile, "_DATA_DIR", tmp_path)  # no measured table shipped
    shot = rig.real.shoot(None, variant_id="natural")
    profile = shot.evidence["profile"]
    assert profile["calibration_status"] == "geometry_only"
    assert profile["noise_model"] is None
    assert profile["lens_id"] == "Sony FE 24-105mm F4 G OSS"
    assert "a7iii_spec_geometry" in profile["calibration_sources"]


def test_engine_profile_uses_shipped_measured_a7iii_table(rig):
    """The repo ships data/sensor_profiles/ilce-7m3.json measured from a real ISO ladder."""
    rig.camera.model = "Sony Alpha-A7 III (PC Control)"
    rig.camera.lens_model = "FE 24-105mm F4 G OSS"
    shot = rig.real.shoot(None, variant_id="natural")
    profile = shot.evidence["profile"]
    assert profile["calibration_status"] == "measured_jpeg_noise"
    assert "measured_jpeg_flat_patch_noise" in profile["calibration_sources"]


def test_engine_profile_reports_existing_profile_for_a7ii_tamron(rig):
    rig.camera.model = "Sony ILCE-7M2 (Control)"
    rig.camera.lens_model = "E 28-75mm F2.8-2.8"
    rig.camera.lens_specification = (28.0, 75.0, 2.8, 2.8)  # the generic Sony form needs corroboration
    shot = rig.real.shoot(None, variant_id="natural")
    profile = shot.evidence["profile"]
    assert profile["calibration_status"] == "existing_profile"
    assert profile["noise_model"] == "A7II_NOISE"
    assert profile["lens_id"] == "Tamron 28-75mm F2.8"
    assert profile["lens_profile"] == "TAMRON_28_75_OPTICS"


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q"]))


# One lens normalisation (29 Sep): focus_bracket_capture --lens "Tamron 28-75mm F2.8" resolved to
# unknown_lens_kept_current while "Tamron 28-75 f/2.8" was profiled.
@pytest.mark.parametrize("spelling", [
    "Tamron 28-75 f/2.8", "Tamron 28-75mm F2.8", "tamron 28-75mm f2.8", "TAMRON 28-75 MM F/2.8",
    "Tamron 28-75mm F / 2.8", "Tamron 28-75mm F2.8 Di III RXD", "Tamron 28-75mm f/2.8 Di III RXD",
    "28-75mm F2.8 Di III RXD",
])
def test_every_tamron_aperture_spelling_resolves_to_the_profiled_lens(spelling):
    profile = rig_profile.resolve_rig("Sony ILCE-7M2 (Control)", lens_model=spelling)
    assert profile.lens_id == "Tamron 28-75mm F2.8"
    assert profile.lens_profiled is True
    assert profile.calibration_status == "existing_profile"


@pytest.mark.parametrize("spelling", ["F2.8", "f2.8", "f/2.8", "F/2.8", "f / 2.8"])
def test_normalize_lens_writes_the_aperture_one_way(spelling):
    assert rig_profile.normalize_lens(f"Tamron 28-75mm {spelling}") == "tamron 28-75 f2.8"


def test_the_exif_lensmodel_form_still_needs_its_lens_specification():
    corroborated = rig_profile.resolve_rig("Sony ILCE-7M2 (Control)", lens_model="E 28-75mm F2.8-2.8",
                                           lens_specification=(28, 75, 2.8, 2.8))
    bare = rig_profile.resolve_rig("Sony ILCE-7M2 (Control)", lens_model="E 28-75mm F2.8-2.8")
    assert corroborated.lens_profiled is True
    assert bare.lens_profiled is False


@pytest.mark.parametrize("lens_model", ["Tamron 28-75mm F/2.8 Di III VXD G2", "FE 50mm F1.8",
                                        "Tamron 28-200mm F2.8-5.6"])
def test_other_lenses_are_not_normalised_into_the_tamron(lens_model):
    assert rig_profile.resolve_rig("Sony ILCE-7M2 (Control)", lens_model=lens_model).lens_profiled is False


def test_body_names_are_unchanged_by_the_lens_normalisation():
    assert rig_profile.body_id("Sony Alpha-A7 III (PC Control)") == "ILCE-7M3"
    assert rig_profile.body_id("Sony ILCE-7M2 (Control)") == "ILCE-7M2"
