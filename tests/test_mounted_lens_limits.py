"""Sony driver apertures are not mounted-lens capability evidence."""
from dataclasses import replace
from PIL import Image
import pytest
from test_final_wave_engine import rig
from camera_caps import from_live_choices, constrain_mounted_lens
from sonyremote import CaptureOutcomeUncertain


def _caps():
    return from_live_choices("Sony ILCE-7M2 (Control)", ["100", "200"],
                             ["f/1", "f/1.2", "f/2", "f/2.8", "f/4", "f/5.6", "f/8", "f/22", "f/64"], ["1/60"])


def test_current_jpeg_f28_lens_excludes_f1_driver_advertisement():
    raw = _caps()
    usable, evidence = constrain_mounted_lens(raw, "f/5.6",
        lens_specification=(28, 75, 2.8, 2.8), max_aperture_value=2.96875)
    assert usable.apertures == [2.8, 4, 5.6]
    assert usable.snap_aperture(1.2) == (2.8, "f/2.8")
    assert evidence["raw_driver_apertures"] == raw.apertures
    assert evidence["usable_apertures"] == [2.8, 4, 5.6]
    assert raw.apertures[0] == 1 and raw.apertures[-1] == 64
    assert evidence["unverified_stop_down_beyond_current_blocked"]


@pytest.mark.parametrize("metadata", [{}, {"lens_specification": (28, 75, 0, 0)},
    {"max_aperture_value": float("nan")}, {"lens_specification": (28, 75, 8, 8)}])
def test_unknown_or_conflicting_lens_only_permits_current_confirmed_aperture(metadata):
    usable, evidence = constrain_mounted_lens(_caps(), "f/5.6", **metadata)
    assert usable.apertures == [5.6]
    assert usable.snap_aperture(1.2) == (5.6, "f/5.6")
    assert evidence["sources"] == ["current_readback_only"]


def test_verified_lens_profile_excludes_both_generic_extremes():
    usable, evidence = constrain_mounted_lens(_caps(), "f/5.6", verified_profile=(2.8, 22))
    assert usable.apertures == [2.8, 4, 5.6, 8, 22]
    assert "verified_lens_profile" in evidence["sources"]


@pytest.mark.parametrize("has_exif", [False, True])
def test_engine_uses_only_mounted_lens_apertures_and_no_extra_probe(rig, has_exif):
    rig.camera.settings["aperture"] = "f/5.6"
    original_options = rig.camera.list_options
    rig.camera.list_options = lambda key: ["f/1", "f/1.2", "f/2.8", "f/4", "f/5.6", "f/64"] if key == "aperture" else original_options(key)
    original_capture = rig.camera.capture
    def capture(*args, **kwargs):
        path = original_capture(*args, **kwargs)
        if has_exif:
            exif = Image.Exif()
            exif[42034] = (28.0, 75.0, 2.8, 2.8)
            exif[37381] = 2.96875
            exif[42036] = "E 28-75mm F2.8-2.8"
            Image.new("RGB", (64, 48), (112, 112, 112)).save(path, exif=exif)
        return path
    rig.camera.capture = capture
    recommendation = rig.a7iii.return_value
    def choose(*args, **kwargs):
        caps = args[3]
        aperture, label = caps.snap_aperture(1.2)
        return replace(recommendation, aperture=aperture, aperture_label=label)
    rig.a7iii.side_effect = choose
    shot = rig.real.shoot("natural", variant_id="natural")
    expected = "f/2.8" if has_exif else "f/5.6"
    assert shot.settings["aperture"] == expected
    assert all(write.get("aperture", expected) == expected for write in rig.camera.exposure_writes)
    assert rig.camera.capture_count == 2
    limits = shot.evidence["profile"]["mounted_lens_limits"]
    assert limits["usable_apertures"][0] == (2.8 if has_exif else 5.6)
    assert limits["raw_driver_apertures"][0] == 1


def test_invalid_recommendation_cannot_bypass_lens_limits(rig):
    rig.a7iii.return_value = replace(rig.a7iii.return_value, aperture=1.2, aperture_label="f/1.2")
    with pytest.raises(CaptureOutcomeUncertain, match="mounted-lens limits"):
        rig.real.shoot("natural", variant_id="natural")
    assert rig.camera.capture_count == 1
    assert not rig.camera.exposure_writes
