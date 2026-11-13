"""Night Assist (A23) planner tests — no camera, no model, pure math + one synthetic JPEG.

The plan is chosen from a body's MEASURED noise table (or the A7 II electron model), not a
fixed recipe: ISO under the measured ceiling, shutter under the transport/motion/handheld cap,
and a frame count N so averaging brings sigma/sqrt(N) back near base-ISO noise.
"""
from __future__ import annotations

import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "backend"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import camera_caps
import night_assist
import physics
import sensor_noise
from _runner import run_module_tests


@dataclass
class _Rig:
    """Minimal stand-in for rig_profile.RigProfile: only .noise and .sensor are read here."""
    noise: object
    sensor: object


def _measured(rows, *, threshold=2.5):
    return sensor_noise.load_measured_noise({
        "body": "TEST", "base_iso": 100, "ceiling_threshold_ratio": threshold,
        "noise_table": [{"iso": iso, "sigma_luma_8bit": s} for iso, s in rows]})


def _caps(isos=("100", "400", "1600", "3200", "6400"),
          apertures=("f/1.8", "f/2.8", "f/8"),
          shutters=("1", "1/2", "1/4", "1/60", "1/100", "1/250", "1/2000")):
    return camera_caps.from_live_choices("TEST", list(isos), list(apertures), list(shutters))


# A synthetic measured table: base sigma 2.0; the 2.5x bar (5.0) puts the usable ceiling at ISO
# 1600 (4.5 <= 5.0) since ISO 3200's 6.0 exceeds it.
_TABLE = [(100, 2.0), (400, 3.0), (1600, 4.5), (3200, 6.0)]


def test_measured_table_ceiling_and_exact_frame_count():
    """Dark scene drives ISO to the measured ceiling (1600); N follows sigma/sqrt(N) exactly.
    sigma(1600)/sigma(100) = 4.5/2.0 = 2.25; N = ceil((2.25/1.25)^2) = ceil(3.24) = 4."""
    rig = _Rig(_measured(_TABLE), physics.A7III)
    plan = night_assist.plan_night(-2.3, rig, _caps())
    assert plan.status == "ok", plan
    assert plan.iso == 1600, plan.iso                       # capped at the measured ceiling
    assert plan.frames == 4, plan.frames                    # exact sigma-math frame count
    assert plan.evidence["noise_source"] == "measured_jpeg_noise"
    assert plan.evidence["ceiling"] == 1600
    assert abs(plan.evidence["noise_ratio_to_base"] - 2.25) < 1e-6
    assert abs(plan.evidence["sigma_after_average"] - 4.5 / 2.0) < 1e-6  # 4.5/sqrt(4) = 2.25


def test_iso_stays_below_measured_ceiling():
    """The ISO ladder offers 3200/6400 but the measured ceiling (1600) forbids them."""
    rig = _Rig(_measured(_TABLE), physics.A7III)
    plan = night_assist.plan_night(-6.0, rig, _caps())  # very dark
    # Too dark to expose even at the ceiling -> insufficient, and the ISO never exceeds 1600.
    assert plan.status == "insufficient_light", plan
    assert plan.evidence["iso"] == 1600 and plan.evidence["ceiling"] == 1600


def test_a7ii_noise_model_path():
    """The A7 II electron NoiseModel yields a ceiling (6400) and an inverse-SNR noise ratio;
    it has no 8-bit luma sigma, so sigma_at_iso stays None but the plan is still bounded."""
    rig = _Rig(sensor_noise.A7II_NOISE, physics.A7II)
    plan = night_assist.plan_night(-3.0, rig, _caps())
    assert plan.status == "ok", plan
    assert plan.iso <= 6400 and plan.evidence["ceiling"] == 6400
    assert plan.evidence["noise_source"] == "a7ii_electron_noise_model"
    assert plan.evidence["sigma_at_iso"] is None            # electron model has no JPEG luma sigma
    assert plan.evidence["noise_ratio_to_base"] > 1.0       # high ISO is noisier than base
    assert night_assist.MIN_FRAMES <= plan.frames <= 16


def test_insufficient_light_reports_ev_shortfall_and_no_frames():
    rig = _Rig(_measured(_TABLE), physics.A7III)
    plan = night_assist.plan_night(-9.0, rig, _caps())
    assert plan.status == "insufficient_light", plan
    assert plan.frames == 0 and plan.iso is None
    assert plan.shortfall_ev is not None and plan.shortfall_ev > 0
    assert any("below the darkest" in line for line in plan.why)


def test_handheld_refuses_long_shutter():
    """tripod=False caps the shutter at the handheld floor 1/(2*focal); here 1/(2*50)=1/100 s."""
    rig = _Rig(_measured(_TABLE, threshold=100.0), physics.A7III)  # loose ceiling so ISO isn't the binder
    tripod_plan = night_assist.plan_night(6.0, rig, _caps(), focal_mm=50.0, tripod=True)
    hand_plan = night_assist.plan_night(6.0, rig, _caps(), focal_mm=50.0, tripod=False)
    assert hand_plan.status == "ok", hand_plan
    assert hand_plan.shutter_s <= 1.0 / 100.0 + 1e-9, hand_plan.shutter_s
    assert hand_plan.shutter_label == "1/100"
    assert hand_plan.shutter_s < tripod_plan.shutter_s      # handheld is strictly shorter
    assert any("handheld floor" in line for line in hand_plan.why)


def test_subject_motion_floor_caps_shutter():
    """A 1/250 s subject-motion floor binds the shutter tighter than the 1 s transport cap."""
    rig = _Rig(_measured(_TABLE, threshold=100.0), physics.A7III)
    plan = night_assist.plan_night(6.0, rig, _caps(), subject_motion_floor_s=1.0 / 250.0)
    assert plan.status == "ok", plan
    assert plan.shutter_s <= 1.0 / 250.0 + 1e-9, plan.shutter_s
    assert plan.shutter_label == "1/250"
    assert any("subject-motion floor" in line for line in plan.why)


def test_frames_clamp_to_max_with_stated_residual():
    """A steep table needs far more than max_frames; N clamps and the residual sigma is stated.
    sigma(6400)/sigma(100) = 10; N would be ceil((10/1.25)^2)=64, clamped to 16 -> residual 10/4."""
    rig = _Rig(_measured([(100, 1.0), (6400, 10.0)], threshold=100.0), physics.A7III)
    caps = _caps(isos=("100", "6400"), apertures=("f/1.8",), shutters=("1", "1/2000"))
    plan = night_assist.plan_night(-4.3, rig, caps, max_frames=16)
    assert plan.status == "ok" and plan.iso == 6400, plan
    assert plan.frames == 16, plan.frames
    assert abs(plan.evidence["sigma_after_average"] - 10.0 / 4.0) < 1e-6   # 10/sqrt(16) = 2.5
    assert any("clamped" in line for line in plan.why)


def test_target_snr_gain_db_raises_frame_count():
    """An explicit averaging-gain floor lifts N even when the noise target is already met."""
    rig = _Rig(_measured(_TABLE), physics.A7III)
    base = night_assist.plan_night(2.0, rig, _caps())                     # bright: N is the 2-frame min
    boosted = night_assist.plan_night(2.0, rig, _caps(), target_snr_gain_db=9.0)  # 10^0.9 ~= 7.94 -> 8
    assert base.frames == night_assist.MIN_FRAMES, base.frames
    assert boosted.frames == 8, boosted.frames
    assert boosted.noise_reduction_db >= 9.0 - 1e-9


def test_widest_aperture_default_and_override():
    rig = _Rig(_measured(_TABLE), physics.A7III)
    widest = night_assist.plan_night(2.0, rig, _caps())
    assert widest.aperture == 1.8 and widest.aperture_label == "f/1.8"
    stopped = night_assist.plan_night(2.0, rig, _caps(), aperture_label="f/8")
    assert stopped.aperture == 8.0 and stopped.aperture_label == "f/8"


def test_deterministic():
    rig = _Rig(_measured(_TABLE), physics.A7III)
    first = night_assist.plan_night(-2.3, rig, _caps(), subject_motion_floor_s=1.0 / 250.0)
    second = night_assist.plan_night(-2.3, rig, _caps(), subject_motion_floor_s=1.0 / 250.0)
    assert first == second


def test_to_session_request_matches_session_create_fields():
    """The body must carry exactly the SessionCreateRequest fields the app validates."""
    rig = _Rig(_measured(_TABLE), physics.A7III)
    plan = night_assist.plan_night(-2.3, rig, _caps())
    body = night_assist.to_session_request(plan, "Sony ILCE-7M2 (Control)", "usb:001,001", "A7II00SERIAL")
    assert body == {"mode": "night", "frames": plan.frames, "ev_step": 0.0, "interval_s": 0.0,
                    "camera_model": "Sony ILCE-7M2 (Control)", "camera_port": "usb:001,001",
                    "camera_serial": "A7II00SERIAL"}
    assert 2 <= body["frames"] <= 16
    # Every key is a real SessionCreateRequest field (see backend/app.py).
    for key in ("mode", "frames", "ev_step", "interval_s", "camera_model", "camera_port", "camera_serial"):
        assert key in body

    import pytest
    with pytest.raises(ValueError):
        night_assist.to_session_request(
            night_assist.plan_night(-9.0, rig, _caps()), "m", "usb:001,001")  # insufficient -> refuse


def _write_jpeg(path: Path, rgb, *, iso=6400, f_number=4.0, shutter_s=1 / 60,
                lens="FE 24-105mm F4 G OSS", spec=(24.0, 105.0, 4.0, 4.0)):
    from PIL import Image
    exif = Image.Exif()
    exif[42036] = lens
    exif[42034] = spec
    exif[37381] = 4.0
    exif[34855] = iso
    exif[33437] = f_number
    exif[33434] = shutter_s
    Image.new("RGB", (64, 48), rgb).save(path, exif=exif)
    return path


_A7III_MODEL = "Sony Alpha-A7 III (PC Control)"
_ISO_CHOICES = ["100", "400", "1600", "3200", "6400", "12800"]
_AP_CHOICES = ["f/4", "f/5.6", "f/8"]
_SH_CHOICES = ["1", "1/2", "1/4", "1/60", "1/250", "1/2000"]


def test_plan_from_meter_frame_measured_rig():
    """A metered A7 III frame resolves the measured rig (real ilce-7m3.json ceiling 3200) and
    plans; the why lines name the metered EV and the measured calibration."""
    with tempfile.TemporaryDirectory() as tmp:
        jpeg = _write_jpeg(Path(tmp) / "meter.jpg", (90, 90, 90))
        plan = night_assist.plan_from_meter_frame(
            str(jpeg), _A7III_MODEL, _ISO_CHOICES, _AP_CHOICES, _SH_CHOICES)
    assert plan.status in ("ok", "insufficient_light"), plan
    assert plan.evidence["reliability"]["status"] == "ok"
    assert any("Metered scene EV100" in line for line in plan.why)
    assert plan.evidence["ceiling"] == 3200                 # from data/sensor_profiles/ilce-7m3.json
    assert plan.evidence["noise_source"] == "measured_jpeg_noise"


def test_plan_from_meter_frame_low_reliability_black_frame():
    """A nearly black meter frame is flagged: status meter_unreliable and a why line saying so."""
    with tempfile.TemporaryDirectory() as tmp:
        jpeg = _write_jpeg(Path(tmp) / "black.jpg", (1, 1, 1))
        plan = night_assist.plan_from_meter_frame(
            str(jpeg), _A7III_MODEL, _ISO_CHOICES, _AP_CHOICES, _SH_CHOICES)
    assert plan.status == "meter_unreliable", plan
    assert plan.evidence["reliability"]["status"] == "low"
    assert any("unreliable" in line.lower() for line in plan.why)


if __name__ == "__main__":
    raise SystemExit(run_module_tests(__name__))
