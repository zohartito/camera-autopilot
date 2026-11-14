"""A Fixed stack meters first; it never inherits whatever exposure the last capture left behind.

Field defect (private/unpublished field notes, 2026-09-17): a night ISO 3200 baseline left on
the body blew out an entire daylight HDR bracket, because a Fixed stack bracketed around the body's current exposure
with no meter photograph. Now one meter frame, through the same metering path as /shoot (including
the bounded re-meter of a saturated or black frame), sets the baseline. The owner may still ask to
keep the body's exposure (``baseline: "current"``). Fake cameras only; nothing opens USB.
"""
from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
import sys

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tests"))
sys.path.insert(0, str(ROOT / "backend"))

import capture_policy  # noqa: E402
import constraint_cards  # noqa: E402
import engine  # noqa: E402
import product_capture  # noqa: E402
import product_sessions  # noqa: E402
import remeter  # noqa: E402
import sonyremote  # noqa: E402
from test_final_wave_engine import FakeCamera, rig  # noqa: E402,F401  (fake-camera engine fixture)

MODEL = "Sony Alpha-A7 III (PC Control)"
PORT = "usb:001,009"
SERIAL = "A7III0SERIAL"
NIGHT = {"iso": "3200", "aperture": "8", "shutter": "1/15"}


class WideLadderCamera(FakeCamera):
    """The engine fake with a full daylight-to-night ladder and an exact USB port."""

    def info(self):
        return SimpleNamespace(model=self.model, port=PORT)

    def list_options(self, setting):
        if setting == "iso":
            return ["100", "200", "400", "800", "1600", "3200"]
        if setting == "shutter":
            return ["1/2000", "1/1000", "1/500", "1/250", "1/125", "1/60", "1/30", "1/15", "1/8",
                    "1/4", "1/2", "1"]
        return super().list_options(setting)


@pytest.fixture
def stack(rig, monkeypatch):
    camera = WideLadderCamera(rig.camera.root, MODEL)
    camera.settings.update(NIGHT)          # the last ordinary capture was a night shot
    real = rig.real
    real.camera_model, real.camera_port, real.camera_serial = MODEL, PORT, SERIAL
    monkeypatch.setattr(real, "_camera", lambda: camera)
    monkeypatch.setattr(sonyremote, "Camera", lambda **kwargs: camera)
    # A daylight scene: the meter recommends ISO 100 · f/8 · 1/500.
    card = constraint_cards.ConstraintCard("SHUTTER", "1/500s", "Daylight test scene", [
        constraint_cards.SubFactor("Scene Movement", "unavailable", "pending")])
    rig.a7iii.return_value = constraint_cards.Recommendation(
        100, 8, 1 / 500, [card], iso_label="100", aperture_label="8", shutter_label="1/500")
    monkeypatch.setattr(product_capture.intent_brain, "interpret",
                        lambda *a, **kw: {"target_brightness": 112, "mood": {}, "why": "fixture"})
    return SimpleNamespace(camera=camera, real=real, a7iii=rig.a7iii)


def _frames(shot):
    state = product_sessions.runner().read(shot.evidence["stack_policy"]["session_id"])
    return state, [frame["applied"] for frame in state["frames"]]


def test_night_body_with_a_daylight_meter_brackets_around_the_metered_exposure(stack):
    shot = product_capture.shoot(stack.real, stack_policy={"mode": "fixed", "stack_mode": "hdr", "frames": 3})
    assert shot.ok, shot.evidence
    state, applied = _frames(shot)
    # Reverting to the inherited baseline would bracket at ISO 3200 around 1/15.
    assert [frame["iso"] for frame in applied] == ["100", "100", "100"]
    assert [frame["shutter"] for frame in applied] == ["1/1000", "1/500", "1/250"]
    assert state["settings"]["iso"] == "100" and state["settings"]["shutter"] == "1/500"
    baseline = shot.evidence["stack_policy"]["baseline"]
    assert baseline["source"] == "metered"
    assert baseline["inherited"] == NIGHT
    assert baseline["metered"] == {"iso": "100", "aperture": "8", "shutter": "1/500"}
    assert len(baseline["meter_passes"]) == 1
    # One meter frame plus three bracket frames, and the request leaves the body as it found it.
    assert stack.camera.capture_count == 4
    assert shot.evidence["physical_exposure_count"] == 4
    assert state["restore_baseline"]["iso"] == "3200" and state["restore"] == "completed"
    assert {key: stack.camera.settings[key] for key in NIGHT} == NIGHT


def test_keep_current_baseline_skips_the_meter_and_keeps_the_old_behaviour(stack):
    shot = product_capture.shoot(stack.real, stack_policy={
        "mode": "fixed", "stack_mode": "hdr", "frames": 3, "baseline": "current"})
    assert shot.ok, shot.evidence
    _, applied = _frames(shot)
    assert [frame["iso"] for frame in applied] == ["3200", "3200", "3200"]
    assert [frame["shutter"] for frame in applied] == ["1/30", "1/15", "1/8"]
    stack.a7iii.assert_not_called()
    assert stack.camera.capture_count == 3
    assert shot.evidence["physical_exposure_count"] == 3
    baseline = shot.evidence["stack_policy"]["baseline"]
    assert baseline["source"] == "inherited"
    assert baseline["inherited"] == NIGHT and baseline["metered"] is None
    assert shot.evidence["stack_policy"]["budget"]["max_physical_exposures"] == 3


def test_a_saturated_meter_frame_is_remetered_before_it_sets_the_baseline(stack):
    stack.camera.frame_values = [255, 112]
    shot = product_capture.shoot(stack.real, stack_policy={"mode": "fixed", "stack_mode": "hdr", "frames": 3})
    baseline = shot.evidence["stack_policy"]["baseline"]
    assert [entry["reliability"]["status"] for entry in baseline["meter_passes"]] == ["low", "ok"]
    assert stack.camera.capture_count == 2 + 3
    assert shot.evidence["physical_exposure_count"] == 5
    # The re-meter moved the body; the stack still restores the exposure it held before metering.
    state, _ = _frames(shot)
    assert {key: state["restore_baseline"][key] for key in NIGHT} == NIGHT
    assert {key: stack.camera.settings[key] for key in NIGHT} == NIGHT


@pytest.mark.parametrize("frames", [2, 3, 9])
def test_the_declared_budget_counts_the_meter_frame(frames):
    metered = capture_policy.exposure_budget(
        capture_policy.validate_stack_policy({"mode": "fixed", "frames": frames}))
    assert metered["baseline_meter_exposures"] == 1
    assert metered["extra_meter_exposures"] == remeter.MAX_REMETER_PASSES
    assert metered["max_physical_exposures"] == frames + 1 + remeter.MAX_REMETER_PASSES
    assert metered["fixed_uses_current_camera_settings"] is False
    current = capture_policy.exposure_budget(
        capture_policy.validate_stack_policy({"mode": "fixed", "frames": frames, "baseline": "current"}))
    assert current["baseline_meter_exposures"] == 0 and current["extra_meter_exposures"] == 0
    assert current["max_physical_exposures"] == frames
    assert current["fixed_uses_current_camera_settings"] is True


def test_shoot_evidence_declares_the_meter_frame_in_its_budget(stack):
    shot = product_capture.shoot(stack.real, stack_policy={"mode": "fixed", "stack_mode": "night", "frames": 4})
    budget = shot.evidence["stack_policy"]["budget"]
    assert budget["max_physical_exposures"] == 4 + 1 + remeter.MAX_REMETER_PASSES
    assert shot.evidence["physical_exposure_count"] <= budget["max_physical_exposures"]


def test_a_failed_meter_frame_stops_before_any_bracket_frame(stack):
    stack.camera.missing_capture = 1
    with pytest.raises(sonyremote.CameraError) as raised:
        product_capture.shoot(stack.real, stack_policy={"mode": "fixed", "stack_mode": "hdr", "frames": 3})
    assert getattr(raised.value, "retry_safe", True) is False       # never an automatic retry
    assert stack.camera.capture_count == 1
    assert not (engine.SHOTS_DIR / "sessions").exists() or not product_sessions.runner().list()


def test_a_failed_recommendation_stops_before_any_bracket_frame(stack):
    stack.a7iii.side_effect = RuntimeError("fixture recommendation failure")
    with pytest.raises(sonyremote.CameraError) as raised:
        product_capture.shoot(stack.real, stack_policy={"mode": "fixed", "stack_mode": "hdr", "frames": 3})
    assert getattr(raised.value, "retry_safe", True) is False
    assert stack.camera.capture_count == 1
    assert not (engine.SHOTS_DIR / "sessions").exists() or not product_sessions.runner().list()


def test_incomplete_body_settings_stop_before_the_meter_photograph(stack):
    del stack.camera.settings["focus_mode"]
    with pytest.raises(sonyremote.CameraError, match="no meter photograph"):
        product_capture.shoot(stack.real, stack_policy={"mode": "fixed", "stack_mode": "hdr", "frames": 3})
    assert stack.camera.capture_count == 0


@pytest.mark.parametrize("policy", [
    {"mode": "fixed", "baseline": "metred"},
    {"mode": "fixed", "baseline": None},
    {"mode": "auto", "baseline": "current"},
    {"mode": "off", "baseline": "current"},
])
def test_invalid_baseline_is_rejected_before_any_capture(stack, policy):
    with pytest.raises(ValueError, match="baseline"):
        product_capture.shoot(stack.real, stack_policy=policy)
    assert stack.camera.capture_count == 0


def test_the_first_manifest_write_carries_the_restore_baseline_and_meter_evidence(stack, monkeypatch):
    # Codex review: the restore baseline was saved in a second write after the session existed; a
    # failure between them left a resumable session that restored the meter exposure instead.
    from stack_session import SessionRunner
    real_run = product_sessions.run
    monkeypatch.setattr(product_sessions, "run", lambda session_id: (_ for _ in ()).throw(RuntimeError("interrupted")))
    monkeypatch.setattr(SessionRunner, "save", lambda self, state: (_ for _ in ()).throw(OSError("disk full")))
    stack.camera.frame_values = [255, 112]                        # the re-meter moves the body
    shot = product_capture.shoot(stack.real, stack_policy={"mode": "fixed", "stack_mode": "hdr", "frames": 3})
    assert not shot.ok and stack.camera.capture_count == 2
    state = product_sessions.runner().read(shot.evidence["stack_policy"]["session_id"])
    assert {key: state["restore_baseline"][key] for key in NIGHT} == NIGHT
    assert state["settings"] == {"iso": "100", "aperture": "8", "shutter": "1/500"}
    recorded = state["metadata"]["stack_baseline"]
    assert recorded["source"] == "metered" and recorded["meter_physical_exposures"] == 2
    assert [entry["pass"] for entry in recorded["meter_passes"]] == [0, 1]
    # Resuming the saved session brackets around the metered exposure and restores the night one.
    monkeypatch.undo()
    monkeypatch.setattr(sonyremote, "Camera", lambda **kwargs: stack.camera)
    resumed = real_run(state["id"])
    assert resumed["status"] == "completed" and resumed["restore"] == "completed"
    assert [frame["applied"]["iso"] for frame in resumed["frames"]] == ["100", "100", "100"]
    assert {key: stack.camera.settings[key] for key in NIGHT} == NIGHT
    sidecar = __import__("json").loads((engine.SHOTS_DIR / f"session_{state['id']}.json").read_text())
    assert sidecar["evidence"]["stack_baseline"]["meter_physical_exposures"] == 2
    assert sidecar["evidence"]["stack_baseline"]["inherited"] == NIGHT


def test_a_session_that_cannot_be_saved_after_the_meter_is_terminal(stack, monkeypatch):
    from stack_session import SessionRunner
    monkeypatch.setattr(SessionRunner, "create", lambda self, *a, **kw: (_ for _ in ()).throw(OSError("disk full")))
    with pytest.raises(sonyremote.CameraError) as raised:
        product_capture.shoot(stack.real, stack_policy={"mode": "fixed", "stack_mode": "hdr", "frames": 3})
    assert getattr(raised.value, "retry_safe", True) is False
    assert stack.camera.capture_count == 1


def test_a_failure_after_a_re_meter_restores_the_pre_meter_exposure(stack):
    # PR #72 review: the re-meter moved the body, the recommendation failed, and no session existed
    # to restore it, so the body was left at the meter exposure.
    stack.camera.frame_values = [255, 112]
    stack.a7iii.side_effect = RuntimeError("fixture recommendation failure")
    with pytest.raises(sonyremote.CameraError, match="pre-meter exposure was restored") as raised:
        product_capture.shoot(stack.real, stack_policy={"mode": "fixed", "stack_mode": "hdr", "frames": 3})
    assert raised.value.meter_restore["status"] == "completed"
    assert stack.camera.capture_count == 2
    assert {key: stack.camera.settings[key] for key in NIGHT} == NIGHT


def test_auto_iso_replaced_for_metering_is_put_back_when_the_session_cannot_be_saved(stack, monkeypatch):
    from stack_session import SessionRunner
    stack.camera.settings["iso"] = "Auto ISO"
    monkeypatch.setattr(SessionRunner, "create", lambda self, *a, **kw: (_ for _ in ()).throw(OSError("disk full")))
    with pytest.raises(sonyremote.CameraError):
        product_capture.shoot(stack.real, stack_policy={"mode": "fixed", "stack_mode": "hdr", "frames": 3})
    assert {"iso": "100"} in stack.camera.exposure_writes              # the meter's fixed ISO
    assert stack.camera.settings["iso"] == "Auto ISO"


def test_a_successful_meter_leaves_the_body_at_its_pre_meter_exposure(stack):
    stack.camera.frame_values = [255, 112]
    receipt = stack.real.meter_baseline(None)
    assert receipt["meter_restore"]["status"] == "completed"
    assert {key: stack.camera.settings[key] for key in NIGHT} == NIGHT
    assert receipt["metered"] == {"iso": "100", "aperture": "8", "shutter": "1/500"}


def test_an_identity_refusal_never_writes_the_restore_onto_that_body(stack):
    stack.camera.frame_values = [255, 112]
    stack.a7iii.side_effect = sonyremote.CameraIdentityError("fixture refusal", reason="serial_mismatch")
    with pytest.raises(sonyremote.CameraIdentityError):
        product_capture.shoot(stack.real, stack_policy={"mode": "fixed", "stack_mode": "hdr", "frames": 3})
    assert {key: stack.camera.settings[key] for key in NIGHT} != NIGHT   # left as the re-meter set it


def test_a_failed_restore_is_reported_when_the_session_also_cannot_be_saved(stack, monkeypatch):
    # PR #72 review: meter_baseline reported meter_restore "failed", then sessions.create raised,
    # and the error dropped the restore result, so the owner never learned the body was left at the
    # meter exposure.
    from stack_session import SessionRunner
    stack.camera.frame_values = [255, 112]                        # the re-meter moves the body
    write = stack.camera.set_exposure

    def refuse_the_restore(**settings):
        if settings.get("iso") == NIGHT["iso"]:
            raise sonyremote.CameraError("fixture restore write refused")
        write(**settings)
    monkeypatch.setattr(stack.camera, "set_exposure", refuse_the_restore)
    monkeypatch.setattr(SessionRunner, "create", lambda self, *a, **kw: (_ for _ in ()).throw(OSError("disk full")))
    with pytest.raises(sonyremote.CameraError) as raised:
        product_capture.shoot(stack.real, stack_policy={"mode": "fixed", "stack_mode": "hdr", "frames": 3})
    assert raised.value.retry_safe is False
    assert raised.value.meter_restore["status"] == "failed"
    assert "fixture restore write refused" in raised.value.meter_restore["reason"]
    message = str(raised.value)
    assert "could not be restored" in message and "fixture restore write refused" in message
    assert "metering exposure" in message


def test_shoot_returns_the_meter_restore_result_with_the_uncertain_outcome(monkeypatch, tmp_path):
    import importlib
    from fastapi.testclient import TestClient
    monkeypatch.setenv("CAMERA_ENGINE", "mock")
    api = importlib.import_module("app")
    monkeypatch.setattr(api, "API_TOKEN", "fixture-credential-1234567890")
    monkeypatch.setattr(api, "_capture_budget", api.RequestBudget(limit=100))
    restore = {"status": "failed", "reason": "CameraError: fixture restore write refused"}

    def uncertain(*args, **kwargs):
        failure = sonyremote.CaptureOutcomeUncertain("fixture: session could not be saved")
        failure.meter_restore = restore
        raise failure
    monkeypatch.setattr(product_capture, "shoot", uncertain)
    response = TestClient(api.app).post("/shoot", json={}, headers={"X-Camera-Autopilot-Token": api.API_TOKEN})
    assert response.status_code == 409, response.text
    body = response.json()
    assert body["retry_capture"] is False and body["meter_restore"] == restore


def test_a_synchronous_fixed_shoot_keeps_stack_baseline_in_its_final_record(stack):
    # PR #72 review: publication wrote evidence.stack_baseline into session_<id>.json, then
    # _save_policy_result rewrote that sidecar from the returned Shot, which lacked it.
    import json
    shot = product_capture.shoot(stack.real, stack_policy={"mode": "fixed", "stack_mode": "hdr", "frames": 3})
    assert shot.ok
    state = product_sessions.runner().read(shot.evidence["stack_policy"]["session_id"])
    assert shot.evidence["stack_baseline"] == state["metadata"]["stack_baseline"]
    record = json.loads((engine.SHOTS_DIR / f"{Path(shot.image).stem}.json").read_text())
    assert record["evidence"]["stack_baseline"] == state["metadata"]["stack_baseline"]
    assert record["evidence"]["stack_baseline"]["inherited"] == NIGHT
    assert record["evidence"]["stack_policy"]["baseline"]["source"] == "metered"


# --- PR #72 review: a refusal before any trigger is not an uncertain capture ----------------------

def _refusing_camera(monkeypatch, tmp_path):
    """A real sonyremote.Camera object whose trigger paths fail the test if they are reached."""
    import sonyremote.camera as camera_mod
    cam = camera_mod.Camera.__new__(camera_mod.Camera)
    monkeypatch.setattr(camera_mod.subprocess, "run", lambda *a, **k: pytest.fail("gphoto2 CLI was invoked"))
    monkeypatch.setattr(cam, "disconnect", lambda: pytest.fail("USB was released for a trigger"), raising=False)
    monkeypatch.setattr(cam, "info", lambda: SimpleNamespace(model=MODEL, port=PORT), raising=False)
    monkeypatch.setattr(cam, "_serial_receipt", lambda: {}, raising=False)
    return camera_mod, cam


def test_missing_gphoto2_is_a_pre_trigger_refusal(monkeypatch, tmp_path):
    camera_mod, cam = _refusing_camera(monkeypatch, tmp_path)
    monkeypatch.setattr(camera_mod.shutil, "which", lambda name: None)
    with pytest.raises(camera_mod.CapturePreconditionError, match="gphoto2 CLI") as refused:
        cam._capture_sony_action(dest_dir=tmp_path)
    assert refused.value.may_have_fired is False
    assert not isinstance(refused.value, camera_mod.CaptureOutcomeUncertain)


@pytest.mark.parametrize("shutter", ["Bulb", "30", None])
def test_an_unbounded_shutter_is_a_pre_trigger_refusal(monkeypatch, tmp_path, shutter):
    camera_mod, cam = _refusing_camera(monkeypatch, tmp_path)
    monkeypatch.setattr(camera_mod.shutil, "which", lambda name: "/fake/gphoto2")
    monkeypatch.setattr(cam, "get_settings", lambda: {"shutter": shutter}, raising=False)
    with pytest.raises(camera_mod.CapturePreconditionError, match="no shutter was issued"):
        cam._capture_sony_action(dest_dir=tmp_path)


class _PreconditionOnCapture(WideLadderCamera):
    """Refuses capture number ``refuse_at`` before any trigger; ``generic`` raises a plain error."""
    refuse_at = 1
    generic = False
    triggers = 0

    identity = False

    def capture(self, download=True):
        if self.capture_count + 1 == self.refuse_at:
            if self.identity:                          # refused before its trigger, by serial
                raise sonyremote.CameraIdentityError("fixture swapped body", reason="serial_mismatch")
            if self.generic:
                self.triggers += 1                       # an unknown error after the trigger call
                raise sonyremote.CameraError("fixture: transport failed after the trigger")
            raise sonyremote.CapturePreconditionError("fixture: gphoto2 CLI missing; no shutter was issued")
        self.triggers += 1
        return super().capture(download)


@pytest.fixture
def refusing(stack, monkeypatch):
    camera = _PreconditionOnCapture(stack.camera.root, MODEL)
    camera.settings.update(NIGHT)
    monkeypatch.setattr(stack.real, "_camera", lambda: camera)
    monkeypatch.setattr(sonyremote, "Camera", lambda **kwargs: camera)
    return SimpleNamespace(camera=camera, real=stack.real)


def test_a_first_meter_refused_before_the_trigger_is_not_uncertain(refusing):
    with pytest.raises(sonyremote.CapturePreconditionError) as refused:
        product_capture.shoot(refusing.real, stack_policy={"mode": "fixed", "stack_mode": "hdr", "frames": 3})
    assert not isinstance(refused.value, sonyremote.CaptureOutcomeUncertain)
    assert refusing.camera.triggers == 0 and refusing.camera.capture_count == 0
    assert not (engine.SHOTS_DIR / "sessions").exists() or not product_sessions.runner().list()


def test_an_ordinary_shoot_refused_before_the_trigger_is_not_uncertain(refusing):
    with pytest.raises(sonyremote.CapturePreconditionError) as refused:
        refusing.real.shoot(None, variant_id="natural")
    assert not isinstance(refused.value, sonyremote.CaptureOutcomeUncertain)
    assert refusing.camera.triggers == 0


def test_a_refused_re_meter_after_a_fired_meter_is_still_uncertain(refusing):
    refusing.camera.frame_values = [255]
    refusing.camera.refuse_at = 2                        # the first meter frame fired
    with pytest.raises(sonyremote.CaptureOutcomeUncertain) as raised:
        product_capture.shoot(refusing.real, stack_policy={"mode": "fixed", "stack_mode": "hdr", "frames": 3})
    assert raised.value.retry_safe is False
    assert refusing.camera.triggers == 1


def test_a_generic_error_from_the_meter_capture_stays_uncertain(refusing):
    refusing.camera.generic = True
    with pytest.raises(sonyremote.CaptureOutcomeUncertain) as raised:
        product_capture.shoot(refusing.real, stack_policy={"mode": "fixed", "stack_mode": "hdr", "frames": 3})
    assert raised.value.retry_safe is False


# --- PR #72 review: a failed meter restore is surfaced on every path after the session exists ----

def _refuse_restore_writes(stack, monkeypatch):
    stack.camera.frame_values = [255, 112]                        # the re-meter moves the body
    write = stack.camera.set_exposure

    def refuse_the_restore(**settings):
        if settings.get("iso") == NIGHT["iso"] and not getattr(stack.camera, "allow_restore", False):
            raise sonyremote.CameraError("fixture restore write refused")
        write(**settings)
    monkeypatch.setattr(stack.camera, "set_exposure", refuse_the_restore)


def test_a_failed_meter_restore_is_noted_even_when_the_stack_succeeds(stack, monkeypatch):
    _refuse_restore_writes(stack, monkeypatch)
    real_restore = product_sessions._restore

    def session_restore(cam, baseline):
        stack.camera.allow_restore = True                        # the session's own restore works
        return real_restore(cam, baseline)
    monkeypatch.setattr(product_sessions, "_restore", session_restore)
    shot = product_capture.shoot(stack.real, stack_policy={"mode": "fixed", "stack_mode": "hdr", "frames": 3})
    assert shot.ok
    assert shot.evidence["stack_baseline"]["meter_restore"]["status"] == "failed"
    assert shot.evidence["meter_restore"]["status"] == "failed"
    assert "restoring the pre-meter exposure right after metering failed" in shot.summary
    assert "the stack session restored it" in shot.summary


def test_a_failed_meter_restore_is_a_warning_when_session_setup_then_fails(stack, monkeypatch):
    # The session exists, then fails before any frame (for example the body will not wake).
    _refuse_restore_writes(stack, monkeypatch)
    wakes = []

    def wake():                                   # the meter wakes the body; the session cannot
        wakes.append(1)
        return len(wakes) == 1
    monkeypatch.setattr(stack.camera, "wake", wake)
    shot = product_capture.shoot(stack.real, stack_policy={"mode": "fixed", "stack_mode": "hdr", "frames": 3})
    assert not shot.ok and stack.camera.capture_count == 2 and len(wakes) == 2
    assert shot.evidence["meter_restore"]["status"] == "failed"
    assert "fixture restore write refused" in shot.evidence["meter_restore"]["reason"]
    assert "Warning: the pre-meter exposure could not be restored" in shot.summary
    assert "may still be at the metering exposure" in shot.summary


def test_a_successful_meter_restore_adds_no_warning(stack):
    shot = product_capture.shoot(stack.real, stack_policy={"mode": "fixed", "stack_mode": "hdr", "frames": 3})
    assert "meter_restore" not in shot.evidence
    assert "pre-meter exposure" not in shot.summary


# --- astra review of 57702fb5: precondition refusals restore, report and respect earlier shutters --

def test_an_ordinary_precondition_refusal_restores_what_metering_changed(refusing):
    # Metering replaced Auto ISO and a 30 s shutter before the refused first capture.
    refusing.camera.settings.update({"iso": "Auto ISO", "shutter": "30"})
    with pytest.raises(sonyremote.CapturePreconditionError) as refused:
        refusing.real.shoot(None, variant_id="natural")
    assert refusing.camera.triggers == 0
    assert {"iso": "100"} in refusing.camera.exposure_writes              # the meter's changes
    assert (refusing.camera.settings["iso"], refusing.camera.settings["shutter"]) == ("Auto ISO", "30")
    assert refused.value.meter_restore == {"status": "completed"}


def test_an_ordinary_precondition_refusal_reports_a_failed_restore(refusing, monkeypatch):
    refusing.camera.settings.update({"iso": "Auto ISO"})
    write = refusing.camera.set_exposure

    def refuse_the_restore(**settings):
        if settings.get("iso") == "Auto ISO":
            raise sonyremote.CameraError("fixture restore write refused")
        write(**settings)
    monkeypatch.setattr(refusing.camera, "set_exposure", refuse_the_restore)
    with pytest.raises(sonyremote.CapturePreconditionError) as refused:
        refusing.real.shoot(None, variant_id="natural")
    assert refused.value.meter_restore["status"] == "failed"
    assert "fixture restore write refused" in refused.value.meter_restore["reason"]


def _critique_too_dark(monkeypatch):
    import vision_critique
    monkeypatch.setattr(vision_critique, "critique", lambda *args, **kwargs: {
        "exposure": "too dark", "brightness_delta": 15, "issues": ["dim"]})


def test_a_refinement_refused_after_the_first_attempt_fired_is_uncertain(refusing, monkeypatch):
    _critique_too_dark(monkeypatch)
    refusing.camera.refuse_at = 3                  # attempt 1 fired meter + final; attempt 2's meter refused
    with pytest.raises(sonyremote.CaptureOutcomeUncertain) as raised:
        product_capture.shoot(refusing.real, refine=True)
    assert raised.value.retry_safe is False
    assert refusing.camera.triggers == 2
    earlier = Path(raised.value.capture_path)
    assert earlier.parent == engine.SHOTS_DIR and earlier.is_file()      # the published first image


def test_a_refinement_refused_before_its_trigger_counts_the_earlier_attempts_exposures(refusing, monkeypatch):
    # F-72-prior-count: the uncertain wrapper reports what the request already fired, as the
    # identity refusal does (attempt 1 fired its meter frame and its final photo).
    _critique_too_dark(monkeypatch)
    refusing.camera.refuse_at = 3                  # attempt 2's meter refused before its trigger
    with pytest.raises(sonyremote.CaptureOutcomeUncertain) as raised:
        product_capture.shoot(refusing.real, refine=True)
    assert refusing.camera.triggers == 2
    assert raised.value.prior_exposures == 2


def test_a_refinement_refused_on_its_first_capture_is_still_not_uncertain(refusing, monkeypatch):
    _critique_too_dark(monkeypatch)
    with pytest.raises(sonyremote.CapturePreconditionError) as refused:
        product_capture.shoot(refusing.real, refine=True)
    assert not isinstance(refused.value, sonyremote.CaptureOutcomeUncertain)
    assert refusing.camera.triggers == 0


@pytest.mark.parametrize("restore", [None, {"status": "completed"},
                                     {"status": "failed", "reason": "CameraError: fixture restore write refused"}])
def test_shoot_answers_a_precondition_refusal_explicitly(monkeypatch, restore):
    import importlib
    from fastapi.testclient import TestClient
    monkeypatch.setenv("CAMERA_ENGINE", "mock")
    api = importlib.import_module("app")
    monkeypatch.setattr(api, "API_TOKEN", "fixture-credential-1234567890")
    monkeypatch.setattr(api, "_capture_budget", api.RequestBudget(limit=100))

    def refused(*args, **kwargs):
        failure = sonyremote.CapturePreconditionError("fixture: gphoto2 CLI missing; no shutter was issued")
        if restore is not None:
            failure.meter_restore = restore
        raise failure
    monkeypatch.setattr(product_capture, "shoot", refused)
    response = TestClient(api.app).post("/shoot", json={}, headers={"X-Camera-Autopilot-Token": api.API_TOKEN})
    assert response.status_code == 503, response.text
    body = response.json()
    assert body["may_have_fired"] is False and body["retry_capture"] is False
    assert "gphoto2 CLI missing" in body["detail"]
    assert body.get("meter_restore") == restore
    if restore and restore["status"] == "failed":
        assert "could not be restored" in body["warning"] and "fixture restore write refused" in body["warning"]
        # The browser shows only detail/error (index.html apiError): the warning must be there too.
        assert body["warning"] in body["detail"]
    else:
        assert "warning" not in body and "could not be restored" not in body["detail"]


# --- Codex P1: a metered HDR baseline leaves headroom for its positive offsets ----------------------

def _recommend(stack, iso, shutter, seconds):
    card = constraint_cards.ConstraintCard("SHUTTER", f"{shutter}s", "Dim test scene", [
        constraint_cards.SubFactor("Scene Movement", "unavailable", "pending")])
    stack.a7iii.return_value = constraint_cards.Recommendation(
        int(iso), 8, seconds, [card], iso_label=iso, aperture_label="8", shutter_label=shutter)


def test_a_one_second_metered_baseline_moves_hdr_headroom_to_iso(stack):
    _recommend(stack, "400", "1", 1.0)            # a dim scene metered to the 1 s USB cap
    shot = product_capture.shoot(stack.real, stack_policy={"mode": "fixed", "stack_mode": "hdr", "frames": 3})
    assert shot.ok, shot.evidence
    state, applied = _frames(shot)
    assert [frame["shutter"] for frame in applied] == ["1/4", "1/2", "1"]      # every frame within 1 s
    assert {frame["iso"] for frame in applied} == {"800"}                   # one stop moved to ISO
    assert {frame["aperture"] for frame in applied} == {"8"}
    assert state["settings"] == {"iso": "800", "aperture": "8", "shutter": "1/2"}
    headroom = shot.evidence["stack_baseline"]["bracket_headroom"]
    assert headroom["recommended"] == {"iso": "400", "aperture": "8", "shutter": "1"}
    assert headroom["adjusted"] == {"iso": "800", "aperture": "8", "shutter": "1/2"}
    assert headroom["max_positive_ev"] == 1.0 and headroom["stops_moved_to_iso"] == pytest.approx(1.0)


def test_hdr_headroom_that_iso_cannot_absorb_is_refused_before_any_bracket_frame(stack):
    _recommend(stack, "3200", "1", 1.0)           # already at this body's ISO ceiling
    with pytest.raises(sonyremote.CaptureOutcomeUncertain, match="ISO") as raised:
        product_capture.shoot(stack.real, stack_policy={"mode": "fixed", "stack_mode": "hdr", "frames": 3})
    assert raised.value.retry_safe is False
    assert stack.camera.capture_count == 1                                  # the meter frame only
    assert not (engine.SHOTS_DIR / "sessions").exists() or not product_sessions.runner().list()
    assert {key: stack.camera.settings[key] for key in NIGHT} == NIGHT       # the body is put back


def test_a_bright_hdr_baseline_and_a_night_stack_are_unchanged(stack):
    shot = product_capture.shoot(stack.real, stack_policy={"mode": "fixed", "stack_mode": "hdr", "frames": 3})
    assert shot.evidence["stack_baseline"]["bracket_headroom"] is None
    _recommend(stack, "400", "1", 1.0)            # no positive offsets: the 1 s baseline stands
    night = product_capture.shoot(stack.real, stack_policy={"mode": "fixed", "stack_mode": "night", "frames": 3})
    assert night.evidence["stack_baseline"]["bracket_headroom"] is None
    assert product_sessions.runner().read(night.evidence["stack_policy"]["session_id"])["settings"]["shutter"] == "1"


# --- astra review of d88a7940 ---------------------------------------------------------------------

def _sony_third_stop_caps():
    import camera_caps
    fast = [8000, 6400, 5000, 4000, 3200, 2500, 2000, 1600, 1250, 1000, 800, 640, 500, 400, 320, 250,
            200, 160, 125, 100, 80, 60, 50, 40, 30, 25, 20, 15, 13, 10, 8, 6, 5, 4, 3]
    shutters = [f"1/{value}" for value in fast] + ["0.4", "0.5", "0.6", "0.8", "1", "1.3", "1.6", "2",
                                                  "2.5", "3.2", "4", "5", "6", "8", "10", "13", "15", "20", "25", "30"]
    isos = ["100", "125", "160", "200", "250", "320", "400", "500", "640", "800", "1000", "1250", "1600",
            "2000", "2500", "3200", "4000", "5000", "6400", "8000", "10000", "12800", "16000", "20000",
            "25600", "32000", "40000", "51200"]
    return camera_caps.from_live_choices(MODEL, isos, ["8"], shutters)


def test_hdr_headroom_judges_the_snapped_frames_the_session_will_apply():
    # A 9-frame bracket around 1/15 s wants 16/15 s for its last frame; the session snaps that to
    # 1 s, within the cap. Judging the unsnapped 1.067 s forced 1/20 s and an ISO above the ceiling.
    # (A profile whose usable ceiling is 51200.)
    offsets = [index - 4 for index in range(9)]
    headroom = engine.reserve_bracket_headroom({"iso": "3200", "aperture": "8", "shutter": "1"},
                                               _sony_third_stop_caps(), offsets, 1.0, min_iso=100, max_iso=51200)
    assert headroom["adjusted"] == {"iso": "51200", "aperture": "8", "shutter": "1/15"}
    assert abs(headroom["iso_stops_achieved"] - headroom["stops_moved_to_iso"]) <= 0.1


def test_hdr_headroom_stays_inside_the_uncalibrated_usable_iso_ceiling():
    # Codex P1: the same 9-frame bracket on an uncalibrated rig needs ISO > 6400 and must refuse,
    # not push every frame past the recommendation's quality gate.
    import brain
    ceiling = brain.usable_iso_ceiling(None)
    assert ceiling == 6400
    offsets = [index - 4 for index in range(9)]
    with pytest.raises(sonyremote.CameraError, match="usable ISO ceiling 6400"):
        engine.reserve_bracket_headroom({"iso": "3200", "aperture": "8", "shutter": "1"},
                                        _sony_third_stop_caps(), offsets, 1.0, min_iso=100, max_iso=ceiling)


def test_hdr_headroom_still_refuses_what_iso_cannot_absorb():
    offsets = [index - 4 for index in range(9)]
    with pytest.raises(sonyremote.CameraError, match="usable ISO ceiling"):
        engine.reserve_bracket_headroom({"iso": "51200", "aperture": "8", "shutter": "1"},
                                        _sony_third_stop_caps(), offsets, 1.0, min_iso=100, max_iso=51200)


def test_usable_iso_ceiling_is_the_one_recommend_live_applies():
    import brain
    import rig_profile
    rig = rig_profile.resolve_rig(MODEL, lens_model="", lens_specification=None, max_aperture_value=None)
    assert brain.usable_iso_ceiling(rig) == int(rig.noise.usable_iso_ceiling(base_iso=rig.sensor.base_iso))
    assert brain.usable_iso_ceiling(None, existing_profile=True) == 6400      # the A7 II DR-derived ceiling


class HighIsoCamera(WideLadderCamera):
    def list_options(self, setting):
        if setting == "iso":
            return ["100", "200", "400", "800", "1600", "3200", "6400", "12800", "25600", "51200"]
        return super().list_options(setting)


def test_a_metered_hdr_at_the_rig_ceiling_is_refused_not_pushed_past_it(stack, monkeypatch):
    # The live ladder offers ISO 6400, but this rig's measured usable ceiling is below it.
    camera = HighIsoCamera(stack.camera.root, MODEL)
    camera.settings.update(NIGHT)
    monkeypatch.setattr(stack.real, "_camera", lambda: camera)
    monkeypatch.setattr(sonyremote, "Camera", lambda **kwargs: camera)
    _recommend(stack, "3200", "1", 1.0)
    with pytest.raises(sonyremote.CaptureOutcomeUncertain, match="usable ISO ceiling") as raised:
        product_capture.shoot(stack.real, stack_policy={"mode": "fixed", "stack_mode": "hdr", "frames": 3})
    assert raised.value.retry_safe is False
    assert camera.capture_count == 1                                        # the meter frame only
    assert not (engine.SHOTS_DIR / "sessions").exists() or not product_sessions.runner().list()
    assert {key: camera.settings[key] for key in NIGHT} == NIGHT              # the body is put back


def test_a_refinement_identity_refusal_reports_the_earlier_photo_and_exposures(refusing, monkeypatch):
    # The refused operation fired nothing and stays a clean identity refusal; the request's earlier
    # attempt did fire twice and published a photo, and the response must say so.
    _critique_too_dark(monkeypatch)
    refusing.camera.identity = True
    refusing.camera.refuse_at = 3
    with pytest.raises(sonyremote.CameraIdentityError) as refused:
        product_capture.shoot(refusing.real, refine=True)
    assert not isinstance(refused.value, sonyremote.CaptureOutcomeUncertain)
    assert refusing.camera.triggers == 2
    earlier = Path(refused.value.capture_path)
    assert earlier.parent == engine.SHOTS_DIR and earlier.is_file()
    body = refused.value.as_dict()
    assert body["may_have_fired"] is False and body["retry_capture"] is False
    assert body["earlier_capture_path"] == str(earlier)
    assert body["prior_exposures"] == 2


# --- Codex review of 4f051738: fast-end headroom and a session identity refusal after the meter ----

def test_a_bright_hdr_at_the_fastest_shutter_moves_headroom_to_a_lower_iso():
    # 1/8000 s with a -2 EV frame would need 1/32000 s; the session would clamp it back onto the
    # baseline. Lengthen the baseline and lower ISO instead, so every frame stays distinct.
    headroom = engine.reserve_bracket_headroom({"iso": "400", "aperture": "8", "shutter": "1/8000"},
                                               _sony_third_stop_caps(), [-2, 0, 2], 1.0, min_iso=100, max_iso=51200)
    assert headroom["adjusted"] == {"iso": "100", "aperture": "8", "shutter": "1/2000"}
    assert headroom["stops_moved_to_iso"] == pytest.approx(-2.0)
    caps = _sony_third_stop_caps()
    frames = [caps.snap_shutter(1 / 2000 * 2 ** ev)[1] for ev in (-2, 0, 2)]
    assert frames == ["1/8000", "1/2000", "1/500"]


def test_a_bright_hdr_already_at_the_lowest_iso_is_refused():
    with pytest.raises(sonyremote.CameraError, match="lowest ISO"):
        engine.reserve_bracket_headroom({"iso": "100", "aperture": "8", "shutter": "1/8000"},
                                        _sony_third_stop_caps(), [-2, 0, 2], 1.0, min_iso=100, max_iso=51200)


def test_a_bright_metered_hdr_brackets_three_distinct_frames(stack):
    _recommend(stack, "400", "1/2000", 1 / 2000)   # this fake body's fastest shutter
    shot = product_capture.shoot(stack.real, stack_policy={"mode": "fixed", "stack_mode": "hdr", "frames": 3})
    assert shot.ok, shot.evidence
    _, applied = _frames(shot)
    assert [frame["shutter"] for frame in applied] == ["1/2000", "1/1000", "1/500"]
    assert {frame["iso"] for frame in applied} == {"200"}
    assert shot.evidence["stack_baseline"]["bracket_headroom"]["stops_moved_to_iso"] == pytest.approx(-1.0)


def test_a_bright_metered_hdr_at_the_lowest_iso_is_refused_before_any_bracket_frame(stack):
    _recommend(stack, "100", "1/2000", 1 / 2000)
    with pytest.raises(sonyremote.CaptureOutcomeUncertain, match="lowest ISO") as raised:
        product_capture.shoot(stack.real, stack_policy={"mode": "fixed", "stack_mode": "hdr", "frames": 3})
    assert raised.value.retry_safe is False
    assert stack.camera.capture_count == 1
    assert not (engine.SHOTS_DIR / "sessions").exists() or not product_sessions.runner().list()
    assert {key: stack.camera.settings[key] for key in NIGHT} == NIGHT


def test_a_session_identity_refusal_after_the_meter_counts_the_meter_exposure(stack, monkeypatch):
    # The body changed between the meter and the session's reconnect: a clean identity refusal for
    # the session, but the request's meter frame did fire.
    def swapped(**kwargs):
        raise sonyremote.CameraIdentityError("fixture swapped body", reason="serial_mismatch")
    monkeypatch.setattr(sonyremote, "Camera", swapped)
    with pytest.raises(sonyremote.CameraIdentityError) as refused:
        product_capture.shoot(stack.real, stack_policy={"mode": "fixed", "stack_mode": "hdr", "frames": 3})
    assert not isinstance(refused.value, sonyremote.CaptureOutcomeUncertain)
    assert stack.camera.capture_count == 1
    body = refused.value.as_dict()
    assert body["may_have_fired"] is False and body["retry_capture"] is False
    assert body["prior_exposures"] == 1


# --- Codex review of 1dbf2370: every pre-trigger setup failure, and a re-meter identity refusal ----

def _to_the_trigger(monkeypatch, tmp_path, camera_mod, cam):
    """Let _capture_sony_action run up to its CLI call with fakes (no USB, no sleep, no CLI)."""
    monkeypatch.setattr(camera_mod.shutil, "which", lambda name: "/fake/gphoto2")
    monkeypatch.setattr(cam, "get_settings", lambda: {"shutter": "1/60"}, raising=False)
    released = []
    monkeypatch.setattr(cam, "disconnect", lambda: released.append(1), raising=False)
    monkeypatch.setattr(cam, "_reconnect_with_backoff", lambda: None, raising=False)
    monkeypatch.setattr(camera_mod.time, "sleep", lambda seconds: None)
    monkeypatch.setattr(camera_mod, "_release_macos_camera_service", lambda: None)
    monkeypatch.setattr(cam, "_quarantine_root", lambda: tmp_path / "quarantine", raising=False)
    return released


@pytest.mark.parametrize("site", ["capture_directory", "info", "serial_proof", "staging"])
def test_every_setup_failure_before_the_cli_is_a_precondition_refusal(monkeypatch, tmp_path, site):
    camera_mod, cam = _refusing_camera(monkeypatch, tmp_path)
    _to_the_trigger(monkeypatch, tmp_path, camera_mod, cam)
    if site == "capture_directory":
        monkeypatch.setattr(camera_mod, "_capture_directory",
                            lambda dest: (_ for _ in ()).throw(OSError("fixture: read-only volume")))
    elif site == "info":
        monkeypatch.setattr(cam, "info", lambda: (_ for _ in ()).throw(camera_mod.CameraError("fixture: link dropped")),
                            raising=False)
    elif site == "serial_proof":
        cam._expected_serial = SERIAL
        monkeypatch.setattr(camera_mod, "verify_serial_at_port",
                            lambda *a: (_ for _ in ()).throw(camera_mod.CameraError("fixture: ioreg unavailable")))
    else:
        monkeypatch.setattr(camera_mod, "_make_staging_dir",
                            lambda root: (_ for _ in ()).throw(OSError("fixture: disk full")))
    with pytest.raises(camera_mod.CapturePreconditionError, match="no shutter was issued") as refused:
        cam._capture_sony_action(dest_dir=tmp_path)
    assert not isinstance(refused.value, camera_mod.CaptureOutcomeUncertain)   # subprocess.run fails the test


def test_a_serial_refusal_before_the_cli_stays_an_identity_refusal(monkeypatch, tmp_path):
    camera_mod, cam = _refusing_camera(monkeypatch, tmp_path)
    _to_the_trigger(monkeypatch, tmp_path, camera_mod, cam)
    cam._expected_serial = SERIAL
    monkeypatch.setattr(camera_mod, "verify_serial_at_port", lambda *a: (_ for _ in ()).throw(
        camera_mod.CameraIdentityError("fixture swapped body", reason="serial_mismatch")))
    with pytest.raises(camera_mod.CameraIdentityError):
        cam._capture_sony_action(dest_dir=tmp_path)


def test_a_failure_at_or_after_the_cli_trigger_is_never_a_precondition_refusal(monkeypatch, tmp_path):
    camera_mod, cam = _refusing_camera(monkeypatch, tmp_path)
    _to_the_trigger(monkeypatch, tmp_path, camera_mod, cam)
    monkeypatch.setattr(camera_mod.subprocess, "run",
                        lambda *a, **k: (_ for _ in ()).throw(OSError("fixture: CLI failed after launch")))
    with pytest.raises(camera_mod.CaptureOutcomeUncertain) as raised:
        cam._capture_sony_action(dest_dir=tmp_path)
    assert not isinstance(raised.value, camera_mod.CapturePreconditionError)


def test_an_identity_refusal_on_the_re_meter_counts_the_fired_meter_frame(stack, monkeypatch):
    camera = _PreconditionOnCapture(stack.camera.root, MODEL)
    camera.settings.update(NIGHT)
    camera.frame_values = [255]                  # the first meter frame is pinned: re-meter follows
    camera.identity, camera.refuse_at = True, 2
    monkeypatch.setattr(stack.real, "_camera", lambda: camera)
    with pytest.raises(sonyremote.CameraIdentityError) as refused:
        product_capture.shoot(stack.real, stack_policy={"mode": "fixed", "stack_mode": "hdr", "frames": 3})
    assert not isinstance(refused.value, sonyremote.CaptureOutcomeUncertain)
    assert camera.triggers == 1
    assert refused.value.as_dict()["prior_exposures"] == 1


def test_an_ordinary_identity_refusal_after_the_meter_counts_it(refusing):
    refusing.camera.identity, refusing.camera.refuse_at = True, 2      # the final capture is refused
    with pytest.raises(sonyremote.CameraIdentityError) as refused:
        refusing.real.shoot(None, variant_id="natural")
    assert refusing.camera.triggers == 1
    assert refused.value.as_dict()["prior_exposures"] == 1


def test_a_refine_identity_refusal_adds_earlier_attempts_to_this_attempts_meter(refusing, monkeypatch):
    _critique_too_dark(monkeypatch)
    refusing.camera.identity, refusing.camera.refuse_at = True, 4      # attempt 2's final capture
    with pytest.raises(sonyremote.CameraIdentityError) as refused:
        product_capture.shoot(refusing.real, refine=True)
    assert refusing.camera.triggers == 3
    assert refused.value.as_dict()["prior_exposures"] == 3            # 2 from attempt 1, 1 meter now


# --- astra pass on d3574f5c/93851213/1dbf2370 ------------------------------------------------------

def _distinct_on_ladder(caps, base_label, offsets):
    import camera_caps
    base = camera_caps._shutter_seconds(base_label)
    return [caps.snap_shutter(base * 2 ** ev)[1] for ev in offsets]


def test_third_stop_hdr_frames_stay_distinct_after_headroom():
    # 4 frames at 1/3 EV from 1 s: 0.8 s fits the cap but applies 0.6, 0.8, 1, 1 (a duplicate).
    offsets = [(index - 1.5) / 3 for index in range(4)]
    caps = _sony_third_stop_caps()
    headroom = engine.reserve_bracket_headroom({"iso": "400", "aperture": "8", "shutter": "1"},
                                               caps, offsets, 1.0, min_iso=100, max_iso=51200)
    frames = _distinct_on_ladder(caps, headroom["adjusted"]["shutter"], offsets)
    assert len(set(frames)) == 4, frames
    assert all(camera_caps_seconds(label) <= 1.0 for label in frames)


def camera_caps_seconds(label):
    import camera_caps
    return camera_caps._shutter_seconds(label)


@pytest.mark.parametrize("frames", [6, 8])
def test_even_hdr_brackets_on_a_coarse_ladder_apply_distinct_frames(stack, frames):
    _recommend(stack, "400", "1/250", 1 / 250)
    shot = product_capture.shoot(stack.real, stack_policy={"mode": "fixed", "stack_mode": "hdr", "frames": frames})
    assert shot.ok, shot.evidence
    _, applied = _frames(shot)
    shutters = [frame["shutter"] for frame in applied]
    assert len(set(shutters)) == frames, shutters


def test_an_out_of_range_iso_is_refused_even_when_the_shutter_fits():
    with pytest.raises(sonyremote.CameraError, match="usable ISO ceiling"):
        engine.reserve_bracket_headroom({"iso": "8000", "aperture": "8", "shutter": "0.8"},
                                        _sony_third_stop_caps(), [-0.5, 0.5], 1.0, min_iso=100, max_iso=6400)


def test_a_mid_session_identity_refusal_records_the_prior_exposures(stack, monkeypatch):
    # One meter frame and one accepted bracket frame, then the body changes: SessionRunner records
    # identity_refused and returns (it does not raise).
    frames = []
    capture = stack.camera.capture

    def swap_after_first_frame(download=True):
        if stack.camera.capture_count == 2:
            raise sonyremote.CameraIdentityError("fixture swapped body", reason="serial_mismatch")
        frames.append(1)
        return capture(download)
    monkeypatch.setattr(stack.camera, "capture", swap_after_first_frame)
    shot = product_capture.shoot(stack.real, stack_policy={"mode": "fixed", "stack_mode": "hdr", "frames": 3})
    assert not shot.ok and stack.camera.capture_count == 2
    state = product_sessions.runner().read(shot.evidence["stack_policy"]["session_id"])
    assert state["status"] == "identity_refused"
    assert state["identity_refusal"]["prior_exposures"] == 2
    assert shot.evidence["stack_policy"]["identity_refusal"]["prior_exposures"] == 2


# --- Codex review of a5d389f0: a precondition refusal mid-session is resumable, not ambiguous -------

def _runner_session(tmp_path, monkeypatch):
    from stack_session import SessionRunner
    import numpy as np
    import cv2
    runner = SessionRunner(tmp_path / "sessions")
    state = runner.create("hdr", 3, ev_step=1.0)
    calls = []

    def write(index, destination):
        rng = np.random.default_rng(index)
        assert cv2.imwrite(str(destination), rng.integers(20, 230, (32, 32, 3), dtype=np.uint8))
        return destination
    return runner, state, calls, write


def test_a_precondition_refusal_mid_session_leaves_the_frame_pending_and_resumable(tmp_path, monkeypatch):
    from stack_session import merge_session_frames
    runner, state, calls, write = _runner_session(tmp_path, monkeypatch)

    def refused_on_second(index, spec, destination):
        calls.append(index)
        if index == 1:
            raise sonyremote.CapturePreconditionError("fixture: staging unavailable; no shutter was issued")
        return write(index, destination)
    merge = lambda paths, out: merge_session_frames("hdr", paths, out)
    result = runner.run(state["id"], refused_on_second, merge)
    assert result["status"] == "precondition_refused"
    assert [frame["status"] for frame in result["frames"]] == ["accepted", "pending", "pending"]
    assert "staging unavailable" in result["precondition_refusal"]["detail"]
    assert result["precondition_refusal"]["frame"] == 1
    assert calls == [0, 1]                                   # never retried within the run
    resumed = runner.run(state["id"], lambda index, spec, destination: (calls.append(index), write(index, destination))[1], merge)
    assert resumed["status"] == "completed" and "precondition_refusal" not in resumed
    assert calls == [0, 1, 1, 2]


def test_a_genuinely_uncertain_frame_still_needs_reconciliation(tmp_path, monkeypatch):
    runner, state, calls, write = _runner_session(tmp_path, monkeypatch)

    def uncertain(index, spec, destination):
        if index == 1:
            raise sonyremote.CaptureOutcomeUncertain("fixture: CLI failed after launch")
        return write(index, destination)
    result = runner.run(state["id"], uncertain, None)
    assert result["status"] == "needs_reconciliation"
    assert result["frames"][1]["status"] == "ambiguous"


def test_a_fixed_stack_resumes_after_a_mid_session_precondition_refusal(stack, monkeypatch):
    capture = stack.camera.capture

    def staging_fails_once(download=True):
        if stack.camera.capture_count == 2 and not getattr(stack.camera, "refused_once", False):
            stack.camera.refused_once = True
            raise sonyremote.CapturePreconditionError("fixture: staging unavailable; no shutter was issued")
        return capture(download)
    monkeypatch.setattr(stack.camera, "capture", staging_fails_once)
    shot = product_capture.shoot(stack.real, stack_policy={"mode": "fixed", "stack_mode": "hdr", "frames": 3})
    assert not shot.ok and shot.evidence["stack_policy"]["may_have_fired"] is False
    session_id = shot.evidence["stack_policy"]["session_id"]
    state = product_sessions.runner().read(session_id)
    assert state["status"] == "precondition_refused" and state["restore"] == "completed"
    assert [frame["status"] for frame in state["frames"]] == ["accepted", "pending", "pending"]
    resumed = product_sessions.run(session_id)
    assert resumed["status"] == "completed"
    assert stack.camera.capture_count == 4                   # meter + 3 frames, the refused one never fired


# --- Codex review of 07a072b6: HDR compensation stays at or above the sensor base ISO ---------------

A7II = "Sony ILCE-7M2 (Control)"
EXTENDED_ISOS = ["50", "64", "80", "100", "125", "160", "200", "250", "320", "400", "500", "640", "800",
                 "1000", "1250", "1600", "3200", "6400", "12800", "25600"]


def test_usable_iso_range_is_the_one_both_recommendations_apply():
    import brain
    import rig_profile
    assert brain.usable_iso_range(None, existing_profile=True) == (100, 6400)   # A7 II base and DR ceiling
    assert brain.usable_iso_range(None) == (100, 6400)                           # uncalibrated policy
    rig = rig_profile.resolve_rig(MODEL, lens_model="", lens_specification=None, max_aperture_value=None)
    assert brain.usable_iso_range(rig) == (rig.sensor.base_iso, brain.usable_iso_ceiling(rig))


def test_a7ii_extended_low_isos_are_not_used_for_hdr_compensation():
    import camera_caps
    caps = camera_caps.from_live_choices(A7II, EXTENDED_ISOS, ["8"], _sony_third_stop_caps().shutter_labels)
    # 1/8000 at ISO 200 with -2 EV needs ISO 50: an extended value below the A7 II's base ISO 100.
    with pytest.raises(sonyremote.CameraError, match="lowest ISO 100"):
        engine.reserve_bracket_headroom({"iso": "200", "aperture": "8", "shutter": "1/8000"},
                                        caps, [-2, 0, 2], 1.0, min_iso=100, max_iso=6400)
    ok = engine.reserve_bracket_headroom({"iso": "400", "aperture": "8", "shutter": "1/8000"},
                                         caps, [-2, 0, 2], 1.0, min_iso=100, max_iso=6400)
    assert ok["adjusted"]["iso"] == "100"


class ExtendedIsoCamera(WideLadderCamera):
    def list_options(self, setting):
        if setting == "iso":
            return ["50", "64", "80", "100", "200", "400", "800", "1600", "3200"]
        return super().list_options(setting)


def test_a_bright_metered_hdr_is_refused_rather_than_using_extended_iso(stack, monkeypatch):
    camera = ExtendedIsoCamera(stack.camera.root, MODEL)
    camera.settings.update(NIGHT)
    monkeypatch.setattr(stack.real, "_camera", lambda: camera)
    monkeypatch.setattr(sonyremote, "Camera", lambda **kwargs: camera)
    _recommend(stack, "100", "1/2000", 1 / 2000)   # the body's fastest shutter at its base ISO
    with pytest.raises(sonyremote.CaptureOutcomeUncertain, match="lowest ISO 100") as raised:
        product_capture.shoot(stack.real, stack_policy={"mode": "fixed", "stack_mode": "hdr", "frames": 3})
    assert raised.value.retry_safe is False
    assert camera.capture_count == 1
    assert {key: camera.settings[key] for key in NIGHT} == NIGHT


# --- merge with #79: a placeholder read is a clean no-fire refusal in the meter-first path ---------

PLACEHOLDER = {"iso": "0", "aperture": "f/0", "shutter": "65535/65535"}


@pytest.fixture
def no_settle(monkeypatch):
    import sonyremote.camera as camera_mod
    monkeypatch.setattr(camera_mod.time, "sleep", lambda seconds: None)


def test_a_placeholder_body_is_refused_before_the_stack_meter_fires(stack, monkeypatch, no_settle):
    monkeypatch.setattr(stack.camera, "get_settings", lambda: {**stack.camera.settings, **PLACEHOLDER})
    with pytest.raises(sonyremote.PlaceholderSettingsError):
        product_capture.shoot(stack.real, stack_policy={"mode": "fixed", "stack_mode": "hdr", "frames": 3})
    assert stack.camera.capture_count == 0 and not stack.camera.exposure_writes


def test_a_placeholder_read_after_the_meter_fired_is_clean_and_counts_it(stack, monkeypatch, no_settle):
    # The first meter frame is pinned white; the re-meter's preflight readback answers placeholders.
    stack.camera.frame_values = [255]
    read = stack.camera.get_settings

    def placeholder_after_meter():
        return {**read(), **PLACEHOLDER} if stack.camera.capture_count >= 1 else read()
    monkeypatch.setattr(stack.camera, "get_settings", placeholder_after_meter)
    with pytest.raises(sonyremote.PlaceholderSettingsError) as refused:
        product_capture.shoot(stack.real, stack_policy={"mode": "fixed", "stack_mode": "hdr", "frames": 3})
    assert not isinstance(refused.value, sonyremote.CaptureOutcomeUncertain)
    assert stack.camera.capture_count == 1
    body = refused.value.as_dict()
    assert body["may_have_fired"] is False and body["prior_exposures"] == 1
    assert body["earlier_capture_path"]
    # No restore write onto a body answering placeholders: the last write is the re-meter's.
    assert NIGHT["iso"] not in [w.get("iso") for w in stack.camera.exposure_writes[1:]]


def test_a_refine_placeholder_refusal_reports_the_earlier_attempt(refusing, monkeypatch, no_settle):
    _critique_too_dark(monkeypatch)
    refusing.camera.refuse_at = 0                  # no capture refusal: only the settings read
    read = refusing.camera.get_settings

    def placeholder_on_second_attempt():
        return {**read(), **PLACEHOLDER} if refusing.camera.capture_count >= 2 else read()
    monkeypatch.setattr(refusing.camera, "get_settings", placeholder_on_second_attempt)
    with pytest.raises(sonyremote.PlaceholderSettingsError) as refused:
        product_capture.shoot(refusing.real, refine=True)
    assert refusing.camera.triggers == 2
    body = refused.value.as_dict()
    assert body["prior_exposures"] == 2
    assert Path(body["earlier_capture_path"]).parent == engine.SHOTS_DIR


# --- Codex review of 44059c46: a placeholder refusal says the exposure was not put back ------------

def test_a_placeholder_after_metering_writes_reports_the_skipped_restore(stack, monkeypatch, no_settle):
    # Auto ISO was replaced for metering; the re-meter's readback then answers placeholders. No
    # restore is written onto that body, and the refusal says the exposure may still be changed.
    stack.camera.settings["iso"] = "Auto ISO"
    stack.camera.frame_values = [255]
    read = stack.camera.get_settings

    def placeholder_after_meter():
        return {**read(), **PLACEHOLDER} if stack.camera.capture_count >= 1 else read()
    monkeypatch.setattr(stack.camera, "get_settings", placeholder_after_meter)
    with pytest.raises(sonyremote.PlaceholderSettingsError) as refused:
        product_capture.shoot(stack.real, stack_policy={"mode": "fixed", "stack_mode": "hdr", "frames": 3})
    restore = refused.value.meter_restore
    assert restore["status"] == "skipped_unverified_body" and "placeholder" in restore["reason"]
    assert not any(write.get("iso") == "Auto ISO" for write in stack.camera.exposure_writes)


def test_a_placeholder_before_any_metering_write_carries_no_restore_receipt(stack, monkeypatch, no_settle):
    monkeypatch.setattr(stack.camera, "get_settings", lambda: {**stack.camera.settings, **PLACEHOLDER})
    with pytest.raises(sonyremote.PlaceholderSettingsError) as refused:
        product_capture.shoot(stack.real, stack_policy={"mode": "fixed", "stack_mode": "hdr", "frames": 3})
    assert getattr(refused.value, "meter_restore", None) is None


@pytest.mark.parametrize("restore", [None, {"status": "skipped_unverified_body",
                                            "reason": "the body answered placeholder exposure"}])
def test_shoot_shows_the_skipped_restore_on_a_placeholder_refusal(monkeypatch, restore):
    import importlib
    from fastapi.testclient import TestClient
    monkeypatch.setenv("CAMERA_ENGINE", "mock")
    api = importlib.import_module("app")
    monkeypatch.setattr(api, "API_TOKEN", "fixture-credential-1234567890")
    monkeypatch.setattr(api, "_capture_budget", api.RequestBudget(limit=100))

    def refused(*args, **kwargs):
        failure = sonyremote.PlaceholderSettingsError("fixture", purpose="re-meter preflight readback", reads=[])
        if restore is not None:
            failure.meter_restore = restore
        raise failure
    monkeypatch.setattr(product_capture, "shoot", refused)
    response = TestClient(api.app).post("/shoot", json={}, headers={"X-Camera-Autopilot-Token": api.API_TOKEN})
    assert response.status_code == 409, response.text
    body = response.json()
    assert body["may_have_fired"] is False and body["retry_capture"] is False
    if restore:
        assert body["meter_restore"] == restore
        assert "check the camera" in body["warning"] and body["warning"] in body["detail"]
    else:
        assert "meter_restore" not in body and "warning" not in body


# --- merge with #94 (per-frame evidence sidecars) and #86 (whole-ladder precheck) -------------------

def test_session_detail_keeps_the_meter_evidence_after_sidecar_compaction(stack):
    shot = product_capture.shoot(stack.real, stack_policy={"mode": "fixed", "stack_mode": "hdr", "frames": 3})
    assert shot.ok, shot.evidence
    session_id = shot.evidence["stack_policy"]["session_id"]
    runner = product_sessions.runner()
    for state in (runner.read(session_id), runner.read_detail(session_id)):
        assert state["metadata"]["stack_baseline"]["source"] == "metered"
        assert state["metadata"]["stack_baseline"]["inherited"] == NIGHT
        assert state["restore_baseline"]["iso"] == NIGHT["iso"]
        assert [frame["applied"]["iso"] for frame in state["frames"]] == ["100"] * 3
    assert "ladder_refusal" not in runner.read(session_id)       # the metered baseline fits #86's check


def test_a_headroom_adjusted_baseline_passes_the_whole_ladder_precheck(stack):
    _recommend(stack, "400", "1", 1.0)            # metered at the 1 s cap: adjusted to 1/2 s, ISO 800
    shot = product_capture.shoot(stack.real, stack_policy={"mode": "fixed", "stack_mode": "hdr", "frames": 3})
    assert shot.ok, shot.evidence
    state = product_sessions.runner().read(shot.evidence["stack_policy"]["session_id"])
    assert "ladder_refusal" not in state and state["settings"]["shutter"] == "1/2"


# --- Codex review of 3283fea6: an identity refusal after a metering write says so too --------------

def test_an_identity_refusal_after_a_metering_write_reports_the_skipped_restore(stack, monkeypatch):
    camera = _PreconditionOnCapture(stack.camera.root, MODEL)
    camera.settings.update(NIGHT)
    camera.settings["iso"] = "Auto ISO"          # metering writes a fixed ISO before the meter frame
    camera.frame_values = [255]
    camera.identity, camera.refuse_at = True, 2  # the re-meter capture is refused by serial
    monkeypatch.setattr(stack.real, "_camera", lambda: camera)
    with pytest.raises(sonyremote.CameraIdentityError) as refused:
        product_capture.shoot(stack.real, stack_policy={"mode": "fixed", "stack_mode": "hdr", "frames": 3})
    assert not isinstance(refused.value, sonyremote.CaptureOutcomeUncertain)
    restore = refused.value.meter_restore
    assert restore["status"] == "skipped_unverified_body" and "identity" in restore["reason"]
    assert not any(write.get("iso") == "Auto ISO" for write in camera.exposure_writes)   # no write-back


def test_an_identity_refusal_before_any_metering_write_carries_no_restore_receipt(refusing):
    refusing.camera.identity = True              # the first meter capture is refused; nothing written
    with pytest.raises(sonyremote.CameraIdentityError) as refused:
        product_capture.shoot(refusing.real, stack_policy={"mode": "fixed", "stack_mode": "hdr", "frames": 3})
    assert getattr(refused.value, "meter_restore", None) is None
    assert not refusing.camera.exposure_writes


@pytest.mark.parametrize("restore", [None, {"status": "skipped_unverified_body",
                                            "reason": "the camera's identity could not be verified"}])
def test_shoot_shows_the_skipped_restore_on_an_identity_refusal(monkeypatch, restore):
    import importlib
    from fastapi.testclient import TestClient
    monkeypatch.setenv("CAMERA_ENGINE", "mock")
    api = importlib.import_module("app")
    monkeypatch.setattr(api, "API_TOKEN", "fixture-credential-1234567890")
    monkeypatch.setattr(api, "_capture_budget", api.RequestBudget(limit=100))

    def refused(*args, **kwargs):
        failure = sonyremote.CameraIdentityError("fixture swapped body", reason="serial_mismatch")
        if restore is not None:
            failure.meter_restore = restore
        raise failure
    monkeypatch.setattr(product_capture, "shoot", refused)
    response = TestClient(api.app).post("/shoot", json={}, headers={"X-Camera-Autopilot-Token": api.API_TOKEN})
    assert response.status_code == 409, response.text
    body = response.json()
    assert body["may_have_fired"] is False and body["retry_capture"] is False
    if restore:
        assert body["meter_restore"] == restore
        assert "check the camera's ISO and shutter" in body["warning"] and body["warning"] in body["detail"]
    else:
        assert "meter_restore" not in body and "warning" not in body


# --- Codex review of 7a679c56: the uncertain re-meter refusal keeps the fired meter count ----------

def test_a_re_meter_refused_before_its_trigger_reports_the_fired_meter_frame(refusing, monkeypatch):
    refusing.camera.frame_values = [255]
    refusing.camera.refuse_at = 2                  # the first meter frame fired; the re-meter is refused
    with pytest.raises(sonyremote.CaptureOutcomeUncertain) as raised:
        product_capture.shoot(refusing.real, stack_policy={"mode": "fixed", "stack_mode": "hdr", "frames": 3})
    assert raised.value.retry_safe is False and refusing.camera.triggers == 1
    assert raised.value.prior_exposures == 1


def test_shoot_returns_prior_exposures_with_an_uncertain_outcome(monkeypatch):
    import importlib
    from fastapi.testclient import TestClient
    monkeypatch.setenv("CAMERA_ENGINE", "mock")
    api = importlib.import_module("app")
    monkeypatch.setattr(api, "API_TOKEN", "fixture-credential-1234567890")
    monkeypatch.setattr(api, "_capture_budget", api.RequestBudget(limit=100))

    def uncertain(*args, **kwargs):
        failure = sonyremote.CaptureOutcomeUncertain("fixture: metering stopped after the meter photograph")
        failure.prior_exposures = 1
        raise failure
    monkeypatch.setattr(product_capture, "shoot", uncertain)
    response = TestClient(api.app).post("/shoot", json={}, headers={"X-Camera-Autopilot-Token": api.API_TOKEN})
    assert response.status_code == 409, response.text
    body = response.json()
    assert body["may_have_fired"] is True and body["retry_capture"] is False
    assert body["prior_exposures"] == 1
