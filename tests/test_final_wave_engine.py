"""Capture orchestration contract with isolated fake cameras; no USB/network calls."""
from __future__ import annotations

from contextlib import ExitStack
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
import math
import sys

from PIL import Image
import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "backend"))
import brain
import constraint_cards
import engine
import live_verify
import vision_banana
from sonyremote import CameraError


class FakeCamera:
    def __init__(self, root, model):
        self.model = model
        self.root = root
        self.settings = {"iso": "100", "aperture": "8", "shutter": "1/125",
                         "white_balance": "Color Temperature", "focus_mode": "Automatic"}
        self.exposure_writes = []
        self.setting_writes = []
        self.capture_count = 0
        self.missing_capture = None
        self.malformed_capture = None
        self.lens_model = None
        self.frame_values = None   # optional per-capture 8-bit grey level; None keeps mid-grey

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def info(self):
        return SimpleNamespace(model=self.model, port="usb:fixture")

    def wake(self):
        return True

    def shooting_mode(self):
        return "M"

    def get_settings(self):
        return dict(self.settings)

    def list_options(self, setting):
        return {"iso": ["Auto ISO", "100", "200", "400"],
                "aperture": ["4", "5.6", "8"],
                "shutter": ["1/500", "1/250", "1/125", "1/60"],
                "white_balance": ["Automatic", "Daylight", "Color Temperature"]}[setting]

    def set_exposure(self, **settings):
        self.exposure_writes.append(settings)
        self.settings.update(settings)

    def set_setting(self, setting, value):
        self.setting_writes.append((setting, value))
        self.settings[setting] = value

    def set_color_temp(self, value):
        self.settings["white_balance"] = "Color Temperature"
        self.settings["color_temperature"] = value
        return True

    def capture(self, download=True):
        self.capture_count += 1
        path = self.root / f"frame-{self.capture_count}.jpg"
        if self.capture_count != self.missing_capture:
            if self.capture_count == self.malformed_capture:
                path.write_bytes(b"not a camera image")
            else:
                exif = Image.Exif()
                if self.lens_model:
                    exif[42036] = self.lens_model
                if getattr(self, "lens_specification", None):
                    exif[42034] = tuple(self.lens_specification)
                value = 112
                if self.frame_values:
                    value = self.frame_values[min(self.capture_count - 1, len(self.frame_values) - 1)]
                Image.new("RGB", (64, 48), (value, value, value)).save(path, exif=exif)
        return path


@pytest.fixture
def rig(tmp_path):
    """Patch only expensive image intelligence; camera and engine contracts remain real."""
    camera = FakeCamera(tmp_path, "Sony Alpha-A7 III (PC Control)")
    real = engine.RealEngine()
    card = constraint_cards.ConstraintCard("SHUTTER", "1/125s", "Computed test floor", [
        constraint_cards.SubFactor("Scene Movement", "unavailable", "pending")])
    rec = constraint_cards.Recommendation(200, 8, 1/125, [card],
        iso_label="200", aperture_label="8", shutter_label="1/125")
    motion = {"status": "unavailable", "reason": "sample interval exceeds limit",
              "subject_motion_floor_s": None, "outcome_verified": False}

    def meter(path, *args, **kwargs):
        if not Path(path).is_file():
            raise FileNotFoundError(path)
        return 10.0, 0.0

    with ExitStack() as stack:
        stack.enter_context(patch.object(engine, "_free_usb"))
        stack.enter_context(patch.object(engine, "SHOTS_DIR", tmp_path / "gallery"))
        stack.enter_context(patch.object(real, "_camera", return_value=camera))
        stack.enter_context(patch.object(vision_banana, "analyze", return_value=None))
        for name in ("metering_region", "subject_distance_m", "dof_depth_m", "scene_class"):
            stack.enter_context(patch.object(vision_banana, name, return_value=None))
        stack.enter_context(patch.object(live_verify, "meter_ev100", side_effect=meter))
        stack.enter_context(patch.object(live_verify, "focal_from_exif", return_value=50.0))
        a7ii = stack.enter_context(patch.object(brain, "recommend_settings", return_value=rec))
        a7iii = stack.enter_context(patch.object(brain, "recommend_live", return_value=rec))
        yield SimpleNamespace(camera=camera, real=real, a7ii=a7ii, a7iii=a7iii, motion=motion)


@pytest.mark.parametrize("model", ["Sony ILCE-7M2 (Control)", "Sony Alpha-A7 III (PC Control)"])
def test_target_bias_reaches_the_selected_body_brain_with_its_own_live_caps(rig, model):
    rig.camera.model = model
    rig.camera.lens_model = "Tamron 28-75 f/2.8"
    shot = rig.real.shoot("natural", target_override=82, variant_id="natural")
    selected, other = (rig.a7ii, rig.a7iii) if "7M2" in model else (rig.a7iii, rig.a7ii)
    selected.assert_called_once()
    other.assert_not_called()
    assert selected.call_args.args[1] == pytest.approx(10 - 2.2 * math.log2(82/112))
    caps = selected.call_args.kwargs["caps"] if "7M2" in model else selected.call_args.args[3]
    assert caps.name == model
    assert caps.isos == [100, 200, 400]
    assert caps.apertures == [4, 5.6, 8]
    assert rig.camera.exposure_writes[-1] == {"iso": "200", "aperture": "8", "shutter": "1/125"}
    assert shot.applied["target_brightness"] == 82
    assert (engine.SHOTS_DIR / shot.image).is_file()


@pytest.mark.parametrize("variant", ["natural", "moody_noir"])
def test_exact_variant_resets_previous_kelvin_white_balance_to_auto(rig, variant):
    shot = rig.real.shoot("conflicting warm text", variant_id=variant)
    assert ("white_balance", "Automatic") in rig.camera.setting_writes
    assert shot.settings["white_balance"] == "Automatic"
    assert shot.applied["white_balance"]["verified"] is True
    expected = 112 if variant == "natural" else 82
    assert shot.applied["target_brightness"] == expected


def test_plain_request_resets_a_leftover_kelvin_white_balance_to_auto(rig):
    # Live 2026-09-22: an A7 III left at a fixed colour temperature by an earlier "warm" shot made
    # a later "natural" request come out pink-orange, because only warm/cool/variant requests
    # touched white balance. A request with no colour direction must set Auto, not inherit it.
    assert rig.camera.settings["white_balance"] == "Color Temperature"
    shot = rig.real.shoot("a natural photo of my living room")
    assert ("white_balance", "Automatic") in rig.camera.setting_writes
    assert shot.settings["white_balance"] == "Automatic"
    assert shot.applied["white_balance"]["requested"] == "auto"
    assert shot.applied["white_balance"]["verified"] is True


def test_a_warm_request_still_sets_its_kelvin_rather_than_auto(rig):
    shot = rig.real.shoot("warm golden hour glow")
    assert ("white_balance", "Automatic") not in rig.camera.setting_writes
    assert shot.applied["white_balance"]["kelvin"] == 7500


class HalfAppliedKelvin:
    """The A7 II on 2026-09-29: the Kelvin white-balance choice takes, the 7500 K value does not."""

    def __init__(self, camera):
        self.camera, self.calls = camera, []
        self.wb_at_capture = []
        capture = camera.capture

        def recording_capture(*args, **kwargs):
            self.wb_at_capture.append(camera.settings["white_balance"])
            return capture(*args, **kwargs)
        camera.capture = recording_capture

    def __call__(self, kelvin):
        self.calls.append(kelvin)
        self.camera.settings["white_balance"] = "Choose Color Temperature"
        self.camera.settings["color_temperature"] = 4000  # the body kept its own Kelvin value
        return False


@pytest.mark.parametrize("before, restored", [("Daylight", "Daylight"),
                                              ("Color Temperature", "Automatic")])  # a leftover Kelvin -> Auto
def test_a_kelvin_value_that_does_not_take_restores_white_balance_before_the_shutter(rig, before, restored):
    rig.camera.settings["white_balance"] = before
    rig.camera.set_color_temp = kelvin = HalfAppliedKelvin(rig.camera)
    shot = rig.real.shoot("a warm photo of this room")
    assert kelvin.calls == [7500]
    assert kelvin.wb_at_capture and set(kelvin.wb_at_capture) == {restored}  # never the half-applied mode
    assert ("white_balance", restored) in rig.camera.setting_writes
    wb = shot.applied["white_balance"]
    assert wb["requested"] == "warm" and wb["kelvin"] == 7500 and wb["verified"] is False
    assert wb["white_balance_before"] == before and wb["restored_white_balance"] == restored
    assert "did not take 7500 K" in wb["reason"]
    assert shot.settings["white_balance"] == restored
    assert "The warm colour was not applied: this camera did not take 7500 K" in shot.summary


def test_a_failed_white_balance_restore_refuses_the_shot_before_any_shutter(rig):
    rig.camera.settings["white_balance"] = "Daylight"
    rig.camera.set_color_temp = HalfAppliedKelvin(rig.camera)
    rig.camera.set_setting = lambda setting, value: None  # the write never takes: readback stays Kelvin
    from sonyremote import CapturePreconditionError
    with pytest.raises(CapturePreconditionError, match="warm white balance was not applied.*No shutter was issued") \
            as refused:
        rig.real.shoot("a warm photo of this room")
    assert refused.value.may_have_fired is False and rig.camera.capture_count == 0


def test_a_failed_restore_on_a_refine_attempt_keeps_the_earlier_photo_and_is_never_retried(rig, monkeypatch):
    import product_capture
    import vision_critique
    from sonyremote import CaptureOutcomeUncertain
    monkeypatch.setattr(vision_critique, "critique", lambda *args, **kwargs: {
        "exposure": "too dark", "brightness_delta": 15, "issues": ["dim"]})
    rig.camera.settings["white_balance"] = "Daylight"
    half_applied = HalfAppliedKelvin(rig.camera)
    attempts = []

    def set_color_temp(kelvin):  # the first attempt takes 7500 K; the refine attempt does not
        attempts.append(kelvin)
        if len(attempts) == 1:
            rig.camera.settings.update(white_balance="Color Temperature", color_temperature=kelvin)
            return True
        rig.camera.set_setting = lambda setting, value: None  # and the restore write never takes
        return half_applied(kelvin)
    rig.camera.set_color_temp = set_color_temp
    with pytest.raises(CaptureOutcomeUncertain) as raised:
        product_capture.shoot(rig.real, "a warm photo of this room", refine=True)
    assert len(attempts) == 2 and raised.value.retry_safe is False
    assert "earlier photo is kept" in str(raised.value) and "No shutter was issued for this capture" in str(raised.value)
    assert Path(raised.value.capture_path).is_file()
    assert set(half_applied.wb_at_capture) <= {"Color Temperature"}  # nothing fired in the half-applied mode


def test_a_verified_kelvin_is_kept_and_the_summary_is_unchanged(rig):
    shot = rig.real.shoot("a cool photo of this room")
    wb = shot.applied["white_balance"]
    assert wb == {"requested": "cool", "kelvin": 4000, "verified": True}
    assert rig.camera.settings["color_temperature"] == 4000
    assert "colour was not applied" not in shot.summary


def test_a_movie_never_records_with_a_half_applied_kelvin_mode(rig):
    rig.camera.model = "Sony ILCE-7M2 (Control)"
    rig.camera.settings["white_balance"] = "Automatic"
    rig.camera.set_color_temp = HalfAppliedKelvin(rig.camera)
    at_start = []
    rig.camera.record_native = lambda seconds: (at_start.append(rig.camera.settings["white_balance"]),
                                                {"recorded_seconds": seconds})[1]
    with patch("sonyremote.assert_recording_allowed"):
        recording = rig.real.record("a warm clip of this room", seconds=1)
    assert at_start == ["Automatic"]
    assert recording.recording["white_balance"]["restored_white_balance"] == "Automatic"
    assert "The warm colour was not applied" in recording.summary


def test_unavailable_motion_is_reported_without_being_counted_as_measured(rig):
    shot = rig.real.shoot(None, variant_id="natural")
    assert shot.evidence["motion"]["status"] == "unavailable"
    assert shot.evidence["motion"]["outcome_verified"] is False
    assert rig.a7iii.call_args.kwargs["subject_motion_floor_s"] is None
    assert shot.cards[0]["outcome_verified"] is False


def test_freeze_request_adds_conservative_floor_without_claiming_measured_motion(rig):
    shot = rig.real.shoot("freeze a runner", variant_id="natural")
    assert rig.a7iii.call_args.kwargs["subject_motion_floor_s"] <= 1/500
    assert shot.evidence["motion"]["status"] == "unavailable"
    assert "froze" not in shot.summary.lower()


@pytest.mark.parametrize("missing_capture", [1, 2])
def test_missing_capture_fails_without_starting_another_exposure_loop(rig, missing_capture):
    rig.camera.missing_capture = missing_capture
    with patch.object(rig.real, "_autoexpose_shoot", side_effect=AssertionError("extra capture fallback")) as fallback:
        with pytest.raises(CameraError):
            rig.real.shoot(None, variant_id="natural")
    fallback.assert_not_called()
    assert rig.camera.capture_count == missing_capture


@pytest.mark.parametrize("model", ["Sony ILCE-7M2 (Control)", "Sony Alpha-A7 III (PC Control)"])
def test_ordinary_usb_capture_has_one_meter_and_one_final(rig, model):
    rig.camera.model = model
    shot = rig.real.shoot(None, variant_id="natural")
    assert rig.camera.capture_count == 2
    assert len(shot.evidence["capture_files"]) == 2
    assert shot.evidence["physical_exposure_count"] == 2
    assert "USB still-transfer" in shot.evidence["motion"]["reason"]


@pytest.mark.parametrize("malformed_capture", [1, 2])
def test_malformed_capture_fails_without_any_additional_shutter(rig, malformed_capture):
    rig.camera.malformed_capture = malformed_capture
    with patch.object(rig.real, "_autoexpose_shoot", side_effect=AssertionError("extra capture fallback")) as fallback:
        with pytest.raises(CameraError, match="image validation failed"):
            rig.real.shoot(None, variant_id="natural")
    fallback.assert_not_called()
    assert rig.camera.capture_count == malformed_capture


@pytest.mark.parametrize("lens_model", [None, "FE 50mm F1.8", "Tamron 28-75mm F/2.8 Di III VXD G2"])
def test_a7ii_unknown_or_other_lens_does_not_borrow_tamron_calibration(rig, lens_model):
    rig.camera.model = "Sony ILCE-7M2 (Control)"
    rig.camera.lens_model = lens_model
    shot = rig.real.shoot(None, variant_id="natural")
    rig.a7ii.assert_not_called()
    rig.a7iii.assert_called_once()
    assert shot.evidence["source"] == "live_capabilities_conservative_physics"
    assert shot.evidence["profile"]["lens_profile"] is None
    assert shot.evidence["profile"]["noise_model"] is None
    assert shot.evidence["profile"]["calibration_status"] == "uncalibrated"


def test_a7ii_identified_existing_tamron_lens_can_use_its_profile(rig):
    rig.camera.model = "Sony ILCE-7M2 (Control)"
    rig.camera.lens_model = "Tamron 28-75 f/2.8"
    shot = rig.real.shoot(None, variant_id="natural")
    rig.a7ii.assert_called_once()
    rig.a7iii.assert_not_called()
    assert shot.evidence["profile"]["lens_identity_verified"] is True
    assert shot.evidence["profile"]["lens_profile"] == "TAMRON_28_75_OPTICS"


def test_product_refinement_is_capped_at_four_usb_exposures(rig):
    import product_capture
    import vision_critique
    with patch.object(vision_critique, "critique", return_value={
            "exposure": "too dark", "brightness_delta": 15, "issues": ["dim"]}):
        shot = product_capture.shoot(rig.real, variant_id="natural", refine=True)
    assert rig.camera.capture_count == 4
    assert shot.evidence["refinement"]["attempts"] == 2


def test_pre_capture_brain_failure_fallback_limits_exposure_iterations_and_keeps_failure(rig, tmp_path):
    final = tmp_path / "fallback.jpg"
    Image.new("RGB", (16, 16), (30, 30, 30)).save(final)
    report = {"ok": False, "final_image": str(final), "final_settings": rig.camera.get_settings(),
              "history": [{"iter": 1}, {"iter": 2}], "note": "Did not converge in 2 shots"}
    with patch.object(rig.real, "_brain_shoot", side_effect=ImportError("model unavailable")), \
            patch("sonyremote.autoexpose.auto_expose", return_value=report) as auto:
        shot = rig.real.shoot(None, variant_id="natural")
    assert auto.call_args.kwargs["max_iter"] == 2
    assert shot.ok is False
    assert shot.evidence["exposure_converged"] is False


def test_applied_plan_is_reused_without_model_reinterpretation_or_mutation(rig):
    plan = engine.intent_brain.preview("cool blue")
    with patch.object(engine.intent_brain, "interpret", side_effect=AssertionError("second interpretation")):
        shot = rig.real.shoot("conflicting warm", applied_plan=plan, target_override=90)
    assert plan["target_brightness"] == 108
    assert "_camera_started" not in plan
    assert shot.applied["target_brightness"] == 90


def test_final_reconnect_failure_preserves_image_and_before_capture_settings(rig):
    original = rig.camera.capture
    def capture(**kwargs):
        path = original(**kwargs)
        if rig.camera.capture_count == 2:
            rig.camera.last_capture_receipt = {"reconnect_ok": False, "warning": "offline", "path": str(path)}
            rig.camera.get_settings = lambda: (_ for _ in ()).throw(CameraError("disconnected"))
            rig.camera.info = lambda: (_ for _ in ()).throw(CameraError("disconnected"))
        return path
    rig.camera.capture = capture
    shot = rig.real.shoot(None, variant_id="natural")
    assert (engine.SHOTS_DIR / shot.image).is_file()
    assert shot.settings["iso"] == "200"
    assert shot.evidence["settings_readback"]["source"] == "before_final"
    assert shot.evidence["settings_readback"]["after_final"] == "unavailable"
    assert rig.camera.capture_count == 2


def test_meter_reconnect_failure_preserves_path_and_does_not_fire_final(rig):
    from sonyremote import CaptureOutcomeUncertain
    original = rig.camera.capture
    def capture(**kwargs):
        path = original(**kwargs)
        rig.camera.last_capture_receipt = {"reconnect_ok": False, "path": str(path)}
        return path
    rig.camera.capture = capture
    with pytest.raises(CaptureOutcomeUncertain) as caught:
        rig.real.shoot(None, variant_id="natural")
    assert caught.value.capture_path.is_file()
    assert caught.value.may_have_fired is True
    assert caught.value.retry_safe is False
    assert rig.camera.capture_count == 1


def test_gallery_copy_failure_retains_final_source_and_typed_uncertainty(rig):
    from sonyremote import CaptureOutcomeUncertain
    with patch.object(engine, "_save_into_shots", side_effect=OSError("disk unavailable")):
        with pytest.raises(CaptureOutcomeUncertain) as caught:
            rig.real.shoot(None, variant_id="natural")
    assert caught.value.capture_path.name == "frame-2.jpg"
    assert caught.value.capture_path.is_file()
    assert rig.camera.capture_count == 2


def test_native_recording_writes_private_receipt_without_fake_poster(rig):
    import json
    import stat
    rig.camera.model = "Sony ILCE-7M2 (Control)"
    rig.camera.record_native = lambda seconds: {"recorded_seconds": seconds, "saved_to": "camera SD"}
    with patch("sonyremote.assert_recording_allowed"), \
            patch.object(engine, "_save_into_shots", side_effect=AssertionError("fake poster")):
        recording = rig.real.record(None, seconds=1)
    assert recording.image == ""
    receipt = engine.SHOTS_DIR / recording.recording["receipt"]
    assert receipt.is_file()
    assert stat.S_IMODE(receipt.stat().st_mode) == 0o600
    saved = json.loads(receipt.read_text())
    assert saved["clip_playback_verified"] is False
    assert saved["poster_frame"] is None
    assert not list(receipt.parent.glob("*.tmp"))


def test_fallback_first_capture_reconnect_failure_is_typed_and_never_retried(rig):
    from sonyremote import CaptureOutcomeUncertain
    original = rig.camera.capture
    def capture(**kwargs):
        path = original(**kwargs)
        rig.camera.last_capture_receipt = {"reconnect_ok": False, "path": str(path)}
        rig.camera.get_settings = lambda: (_ for _ in ()).throw(CameraError("disconnected"))
        return path
    rig.camera.capture = capture
    with patch.object(rig.real, "_brain_shoot", side_effect=ImportError("model unavailable")):
        with pytest.raises(CaptureOutcomeUncertain) as caught:
            rig.real.shoot(None, variant_id="natural")
    assert caught.value.capture_path.is_file()
    assert caught.value.may_have_fired is True
    assert caught.value.retry_safe is False
    assert rig.camera.capture_count == 1


def test_fallback_after_capture_meter_failure_preserves_exact_path(rig):
    from sonyremote import CaptureOutcomeUncertain
    with patch.object(rig.real, "_brain_shoot", side_effect=ImportError("model unavailable")), \
            patch("sonyremote.autoexpose.measure_jpeg", side_effect=ValueError("invalid pixels")):
        with pytest.raises(CaptureOutcomeUncertain) as caught:
            rig.real.shoot(None, variant_id="natural")
    assert caught.value.capture_path.name == "frame-1.jpg"
    assert caught.value.capture_path.is_file()
    assert rig.camera.capture_count == 1


def test_completed_movie_readback_failure_returns_receipt_without_record_retry(rig):
    rig.camera.model = "Sony ILCE-7M2 (Control)"
    before = rig.camera.get_settings()
    action = SimpleNamespace(count=0)
    def record(seconds):
        action.count += 1
        rig.camera.get_settings = lambda: (_ for _ in ()).throw(CameraError("reconnect failed"))
        return {"recorded_seconds": seconds, "saved_to": "camera SD"}
    rig.camera.record_native = record
    with patch("sonyremote.assert_recording_allowed"):
        recording = rig.real.record(None, seconds=1)
    assert action.count == 1
    assert recording.ok is True
    assert recording.image == ""
    assert recording.settings == before
    assert recording.recording["settings_readback"]["after_recording"] == "unavailable"
    assert recording.recording["native_action_completed"] is True
    assert recording.recording["retry_record"] is False
    assert recording.recording["clip_playback_verified"] is False
    assert (engine.SHOTS_DIR / recording.recording["receipt"]).is_file()


def test_completed_movie_receipt_disk_failure_returns_action_outcome_without_retry(rig):
    rig.camera.model = "Sony ILCE-7M2 (Control)"
    action = SimpleNamespace(count=0)
    def record(seconds):
        action.count += 1
        return {"recorded_seconds": seconds, "saved_to": "camera SD"}
    rig.camera.record_native = record
    with patch("sonyremote.assert_recording_allowed"), \
            patch.object(engine.os, "open", side_effect=OSError("disk full")):
        recording = rig.real.record(None, seconds=1)
    assert action.count == 1
    assert recording.ok is True
    assert recording.image == ""
    assert recording.recording["retry_record"] is False
    assert recording.recording["native_action_completed"] is True
    assert recording.recording["receipt_persisted"] is False
    assert recording.recording["receipt"] is None
    assert "disk full" in recording.recording["receipt_persistence_warning"]
    assert recording.recording["clip_playback_verified"] is False


def test_missing_final_return_does_not_identify_meter_as_final_recovery(rig, monkeypatch):
    from sonyremote import CaptureOutcomeUncertain
    capture = rig.camera.capture
    def missing_final(*args, **kwargs):
        path = capture(*args, **kwargs)
        return None if rig.camera.capture_count == 2 else path
    monkeypatch.setattr(rig.camera, 'capture', missing_final)
    with pytest.raises(CaptureOutcomeUncertain) as failure:
        rig.real.shoot(None, variant_id='natural')
    assert rig.camera.capture_count == 2
    assert failure.value.capture_path is None
