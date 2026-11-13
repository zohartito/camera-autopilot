"""The product engine routes a long-exposure request to ONE long exposure, only when gated on.

Uses the camera-free `rig` (tests/test_final_wave_engine.py): a fake body, patched metering and no
image intelligence. The meter frame is an ordinary <= 1 s capture; only the final photograph uses
the separately gated long route (`Camera.capture_long_exposure`). Ordinary /shoot keeps its 1 s cap.
"""
from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import patch

import pytest
from PIL import Image

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "backend"))

import camera_caps
import constraint_cards
import engine
import live_verify
from sonyremote import CameraError, CaptureOutcomeUncertain
from test_final_wave_engine import rig  # noqa: F401 — the camera-free engine fixture
from test_final_wave_product_api import product  # noqa: F401 — the camera-free app fixture

TRAILS = "light trails of the room at night"
A7II = "Sony ILCE-7M2 (Control)"


def _long_body(rig, *, ev100=3.0):
    """Give the rig's fake body long shutters, a profiled lens, and the long route."""
    camera = rig.camera
    camera.model = A7II
    camera.lens_model = "Tamron 28-75 f/2.8"
    camera.calls = []
    camera.long_captures = []
    camera.long_error = None
    camera.long_refusal = None
    base_options = camera.list_options

    def list_options(setting):
        if setting == "shutter":
            return ["30", "15", "8", "4", "32/10", "2", "13/10", "1", "1/2", "1/60", "1/125", "1/250"]
        if setting == "aperture":
            return ["2.8", "4", "5.6", "8", "11", "16", "22"]
        return base_options(setting)

    def set_exposure(**settings):
        camera.calls.append(("set_exposure", dict(settings)))
        camera.exposure_writes.append(settings)
        camera.settings.update(settings)

    def set_setting(setting, value):
        camera.calls.append(("set_setting", setting, value))
        camera.setting_writes.append((setting, value))
        camera.settings[setting] = value

    def reconnect():
        camera.calls.append(("reconnect",))
        return camera.info()

    def capture_long_exposure(shutter, dest_dir=None):
        camera.calls.append(("capture_long_exposure", shutter))
        camera.long_captures.append(dict(camera.settings))
        seconds = float(shutter.split("/")[0]) / float(shutter.split("/")[1]) if "/" in shutter else float(shutter)
        if camera.long_refusal is not None:   # refused before the trigger, as the real route does
            camera.last_capture_receipt = {"route": "long_exposure", "trigger_commands": 0}
            raise CameraError(camera.long_refusal)
        if camera.long_error is not None:
            camera.last_capture_receipt = {"route": "long_exposure", "reconnect_ok": False,
                                           "timings": {"trigger_and_download_s": 1.0}}
            raise camera.long_error
        path = camera.root / f"long-{len(camera.long_captures)}.jpg"
        Image.new("RGB", (64, 48), (90, 90, 90)).save(path)
        camera.last_capture_receipt = {"route": "long_exposure", "reconnect_ok": True, "capture_confirmed": True,
                                       "timings": {"trigger_and_download_s": 2 * seconds + 15}}
        return path

    camera.list_options = list_options
    camera.set_exposure = set_exposure
    camera.set_setting = set_setting
    camera.reconnect = reconnect
    camera.capture_long_exposure = capture_long_exposure
    return patch.object(live_verify, "meter_ev100", return_value=(ev100, 0.0))


def _plan(intent=TRAILS):
    return engine.intent_brain.preview(intent)


def test_a_light_trails_request_lengthens_the_shutter_past_one_second(rig, monkeypatch):
    monkeypatch.setenv("CAMERA_LONG_EXPOSURE", "1")
    with _long_body(rig):
        shot = rig.real.shoot(TRAILS, applied_plan=_plan())
    camera = rig.camera
    assert camera.capture_count == 1                       # one ordinary <= 1 s meter frame
    assert [c for c in camera.calls if c[0] == "capture_long_exposure"] == [
        ("capture_long_exposure", camera.long_captures[0]["shutter"])]
    fired = camera.long_captures[0]
    assert camera_caps._shutter_seconds(fired["shutter"]) > 1.0
    assert fired["iso"] == "100"
    assert fired["focus_mode"] == "Manual"
    rig.a7ii.assert_not_called()
    rig.a7iii.assert_not_called()
    assert shot.ok is True
    assert shot.evidence["source"] == "long_single_exposure_plan"
    assert shot.evidence["long_exposure"]["status"] == "ok"
    assert shot.evidence["long_exposure"]["shutter_label"] == fired["shutter"]
    assert shot.evidence["physical_exposure_count"] == 2
    assert shot.evidence["transport_receipt"]["route"] == "long_exposure"
    assert [card["axis"] for card in shot.cards] == ["SHUTTER", "APERTURE", "ISO"]
    assert "tripod" in str(shot.cards[0]).lower()
    assert (engine.SHOTS_DIR / shot.image).is_file()


def test_restoration_reconnects_before_it_writes_and_returns_the_body_to_its_settings(rig, monkeypatch):
    monkeypatch.setenv("CAMERA_LONG_EXPOSURE", "1")
    before = dict(rig.camera.settings)
    with _long_body(rig):
        shot = rig.real.shoot(TRAILS, applied_plan=_plan())
    calls = rig.camera.calls
    fired = calls.index(next(c for c in calls if c[0] == "capture_long_exposure"))
    after = calls[fired + 1:]
    assert after[0] == ("reconnect",)
    assert all(c[0] in ("set_setting", "set_exposure", "reconnect") for c in after)
    for key in ("iso", "aperture", "shutter", "focus_mode"):
        assert rig.camera.settings[key] == before[key]
    policy = shot.evidence["final_focus_policy"]
    assert policy["restore"] == "verified"
    assert policy["exposure_restore"] == "verified"


def test_without_the_gate_the_request_stays_on_the_capped_ordinary_route_and_says_so(rig, monkeypatch):
    monkeypatch.delenv("CAMERA_LONG_EXPOSURE", raising=False)
    with _long_body(rig):
        shot = rig.real.shoot(TRAILS, applied_plan=_plan())
    assert not any(c[0] == "capture_long_exposure" for c in rig.camera.calls)
    assert rig.camera.capture_count == 2
    assert "CAMERA_LONG_EXPOSURE" in shot.summary
    assert shot.evidence["long_exposure"]["status"] == "disabled"


def test_a_scene_too_bright_for_a_long_exposure_says_nd_and_fires_nothing_more(rig, monkeypatch):
    monkeypatch.setenv("CAMERA_LONG_EXPOSURE", "1")
    with _long_body(rig, ev100=13.0):
        shot = rig.real.shoot(TRAILS, applied_plan=_plan())
    assert not any(c[0] == "capture_long_exposure" for c in rig.camera.calls)
    assert rig.camera.capture_count == 1
    assert shot.ok is False
    assert "ND" in shot.summary
    assert shot.evidence["long_exposure"]["status"] == "needs_nd"
    assert shot.evidence["physical_exposure_count"] == 1
    assert shot.cards[0]["status"] == "limited"


def test_an_uncertain_long_exposure_is_never_retried_and_restoration_waits(rig, monkeypatch):
    monkeypatch.setenv("CAMERA_LONG_EXPOSURE", "1")
    with _long_body(rig):
        rig.camera.long_error = CaptureOutcomeUncertain("fixture timeout")
        with pytest.raises(CaptureOutcomeUncertain) as uncertain:
            rig.real.shoot(TRAILS, applied_plan=_plan())
    assert len([c for c in rig.camera.calls if c[0] == "capture_long_exposure"]) == 1
    assert rig.camera.capture_count == 1
    policy = uncertain.value.focus_policy
    assert policy["restore"] == "deferred_until_physical_idle_verified"
    fired = rig.camera.calls.index(next(c for c in rig.camera.calls if c[0] == "capture_long_exposure"))
    assert not any(c[0] in ("set_setting", "set_exposure") for c in rig.camera.calls[fired + 1:])


def test_ordinary_shoot_still_refuses_a_recommendation_longer_than_one_second(rig, monkeypatch):
    monkeypatch.setenv("CAMERA_LONG_EXPOSURE", "1")
    card = constraint_cards.ConstraintCard("SHUTTER", "2.0s", "fixture", [])
    rig.a7iii.return_value = constraint_cards.Recommendation(
        100, 8, 2.0, [card], iso_label="100", aperture_label="8", shutter_label="2")
    with _long_body(rig):
        rig.camera.model = "Sony Alpha-A7 III (PC Control)"
        rig.camera.lens_model = None
        with pytest.raises(CameraError, match="exceeds enabled Sony transport policy"):
            rig.real.shoot("a natural photo", applied_plan=_plan("a natural photo"))
    assert not any(c[0] == "capture_long_exposure" for c in rig.camera.calls)
    assert rig.camera.capture_count == 1


def test_a_long_exposure_refused_before_its_trigger_is_not_reported_as_may_have_fired(rig, monkeypatch):
    monkeypatch.setenv("CAMERA_LONG_EXPOSURE", "1")
    before = dict(rig.camera.settings)
    with _long_body(rig):
        rig.camera.long_refusal = "The camera had images pending in its memory; no shutter was issued"
        with pytest.raises(CameraError) as refused:
            rig.real.shoot(TRAILS, applied_plan=_plan())
    assert not isinstance(refused.value, CaptureOutcomeUncertain)
    assert getattr(refused.value, "retry_safe", True) is not False
    assert refused.value.capture_path.is_file()          # the meter frame stays reachable
    assert rig.camera.capture_count == 1
    # Nothing fired, so restoration runs at once: reconnect first, then every setting back.
    fired = rig.camera.calls.index(next(c for c in rig.camera.calls if c[0] == "capture_long_exposure"))
    assert rig.camera.calls[fired + 1] == ("reconnect",)
    for key in ("iso", "aperture", "shutter", "focus_mode"):
        assert rig.camera.settings[key] == before[key]


def test_refine_is_refused_for_a_long_exposure_before_any_capture(rig, monkeypatch):
    import product_capture
    monkeypatch.setenv("CAMERA_LONG_EXPOSURE", "1")
    with _long_body(rig):
        with pytest.raises(ValueError, match="Refine"):
            product_capture.shoot(rig.real, TRAILS, refine=True)
    assert rig.camera.capture_count == 0
    assert not any(c[0] == "capture_long_exposure" for c in rig.camera.calls)


def test_http_shoot_returns_the_structured_pre_trigger_refusal(product, monkeypatch, tmp_path):
    import product_capture
    meter = tmp_path / "meter.jpg"
    meter.write_bytes(b"fixture")
    refusal = CameraError("The camera had images pending in its memory; no shutter was issued")
    refusal.final_shutter_not_issued = True
    refusal.capture_path = meter
    refusal.focus_policy = {"restore": "verified", "exposure_restore": "verified"}
    refusal.transport_receipt = {"route": "long_exposure", "trigger_commands": 0}

    def refuse(*args, **kwargs):
        raise refusal
    monkeypatch.setattr(product_capture, "shoot", refuse)
    response = product.client.post("/shoot", headers=product.headers, json={"intent": TRAILS})
    assert response.status_code == 503
    body = response.json()
    assert body["may_have_fired"] is False and body["final_shutter_issued"] is False
    assert body["retry_capture"] is False       # review (PR #81): MCP had it, HTTP did not
    assert body["capture_path"] == str(meter)
    assert body["focus_policy"]["exposure_restore"] == "verified"
    assert body["transport_receipt"]["trigger_commands"] == 0


def test_an_nd_refusal_returns_the_body_to_the_settings_it_had_before_the_request(rig, monkeypatch):
    # Review (PR #81): the meter preflight fixed Auto ISO and capped a 30 s shutter, and the ND
    # early return left the body changed although no final photograph was taken.
    monkeypatch.setenv("CAMERA_LONG_EXPOSURE", "1")
    rig.camera.settings.update(iso="Auto ISO", shutter="30")
    before = dict(rig.camera.settings)
    with _long_body(rig, ev100=13.0):
        shot = rig.real.shoot(TRAILS, applied_plan=_plan())
    assert shot.ok is False and shot.evidence["long_exposure"]["status"] == "needs_nd"
    assert rig.camera.capture_count == 1
    assert rig.camera.exposure_writes[0] != {}              # the preflight did change the body
    for key in ("iso", "aperture", "shutter"):
        assert rig.camera.settings[key] == before[key]
    assert shot.evidence["exposure_restore"] == "verified"


def test_an_nd_refusal_stays_reportable_when_the_body_stops_answering(rig, monkeypatch):
    # Review (PR #81): a failed readback after the ND plan turned the refusal into a generic error
    # and left an unindexed gallery copy of the meter frame.
    import long_single_exposure
    monkeypatch.setenv("CAMERA_LONG_EXPOSURE", "1")
    real_plan = long_single_exposure.plan_long_single_exposure

    def plan_then_disconnect(*args, **kwargs):
        result = real_plan(*args, **kwargs)
        rig.camera.get_settings = lambda: (_ for _ in ()).throw(CameraError("disconnected"))
        return result
    monkeypatch.setattr(long_single_exposure, "plan_long_single_exposure", plan_then_disconnect)
    with _long_body(rig, ev100=13.0):
        rig.camera.settings.update(iso="Auto ISO")
        shot = rig.real.shoot(TRAILS, applied_plan=_plan())
    assert shot.ok is False and "ND" in shot.summary
    assert shot.evidence["exposure_restore"] == "failed"
    assert shot.evidence["settings_readback"]["source"] == "last_read_before_refusal"
    assert shot.settings["shutter"]                          # the last settings actually read
    assert [p.name for p in engine.SHOTS_DIR.iterdir()] == [shot.image]


def test_long_exposure_summaries_never_recommend_the_refused_refine(rig, monkeypatch):
    import remeter
    monkeypatch.setenv("CAMERA_LONG_EXPOSURE", "1")
    monkeypatch.setattr(live_verify, "meter_reliability",
                        lambda path: {"status": "low", "reason": "fixture near-black frame", "mean_luma": 4.0})
    monkeypatch.setattr(remeter, "corrected_exposure", lambda *a, **k: None)
    with _long_body(rig):
        rig.camera.read_focus_indication = lambda: "Unlock"
        rig.camera.autofocus_half_press = lambda hold: "Unlock"
        shot = rig.real.shoot(TRAILS, applied_plan=_plan())
    assert shot.ok is True and shot.evidence["long_exposure"]["status"] == "ok"
    assert shot.evidence["final_focus_policy"]["af_focus_lock"]["recommend_refine"] is True
    assert "Refine" not in shot.summary
    assert "ask again" in shot.summary.lower()


def test_a_body_without_long_shutters_gets_a_definite_refusal_not_may_have_fired(rig, monkeypatch):
    # Review (PR #81): the planner raised after the meter frame, so the engine reported the long
    # shutter as possibly fired and skipped restoration.
    monkeypatch.setenv("CAMERA_LONG_EXPOSURE", "1")
    with _long_body(rig):
        rig.camera.settings.update(iso="Auto ISO")
        before = dict(rig.camera.settings)
        rig.camera.list_options = (lambda base: lambda setting:
                                   ["1", "1/2", "1/60", "1/125"] if setting == "shutter" else base(setting))(
                                       rig.camera.list_options)
        shot = rig.real.shoot(TRAILS, applied_plan=_plan())
    assert shot.ok is False
    assert shot.evidence["long_exposure"]["status"] == "unplannable"
    assert "no live shutter above 1 s" in shot.summary.lower()
    assert shot.evidence["final_capture_attempted"] is False
    assert not any(c[0] == "capture_long_exposure" for c in rig.camera.calls)
    assert rig.camera.capture_count == 1
    for key in ("iso", "aperture", "shutter"):
        assert rig.camera.settings[key] == before[key]
    assert shot.evidence["exposure_restore"] == "verified"


@pytest.mark.parametrize("readback_shutter", [None, "1/60", "25"])
def test_an_unexpected_long_readback_is_a_clean_refusal_and_restores_the_body(rig, monkeypatch, readback_shutter):
    # Review (PR #81): the long settings were applied, then a missing or unexpected readback raised
    # before the restoration scope, left the body changed and was reported as "may have fired".
    monkeypatch.setenv("CAMERA_LONG_EXPOSURE", "1")
    with _long_body(rig):
        camera = rig.camera
        camera.settings.update(iso="Auto ISO")
        before = dict(camera.settings)
        applied = camera.set_exposure

        def set_exposure(**settings):
            applied(**settings)
            shutter = settings.get("shutter")
            if shutter and camera_caps._shutter_seconds(shutter) > 1:   # the body answers oddly
                camera.settings["shutter"] = readback_shutter
        camera.set_exposure = set_exposure
        with pytest.raises(CameraError) as refused:
            rig.real.shoot(TRAILS, applied_plan=_plan())
    assert not isinstance(refused.value, CaptureOutcomeUncertain)
    assert refused.value.final_shutter_not_issued is True
    assert refused.value.capture_path.is_file()                 # the meter frame stays reachable
    assert "no shutter was issued" in str(refused.value)
    assert not any(c[0] == "capture_long_exposure" for c in camera.calls)
    assert camera.capture_count == 1
    for key in ("iso", "aperture", "shutter"):
        assert camera.settings[key] == before[key]
    assert refused.value.focus_policy["exposure_restore"] == "verified"


def test_a_placeholder_readback_after_applying_the_long_settings_fires_nothing(rig, monkeypatch, module_time):
    # Merge of #79 (checked settings reads) with this route: a body answering Sony's placeholder
    # (ISO 0, 65535/65535) after the long settings are applied is #79's clean placeholder refusal;
    # the long route restores the body first, and neither shutter path fires.
    import sonyremote.camera as camera_mod
    from sonyremote import PlaceholderSettingsError
    module_time(camera_mod, sleep=lambda _s: None)
    monkeypatch.setenv("CAMERA_LONG_EXPOSURE", "1")
    with _long_body(rig):
        camera = rig.camera
        before = dict(camera.settings)
        applied = camera.set_exposure

        def set_exposure(**settings):
            applied(**settings)
            shutter = settings.get("shutter")
            if shutter and camera_caps._shutter_seconds(shutter) > 1:
                camera.settings.update(iso="0", shutter="65535/65535")
        camera.set_exposure = set_exposure
        with pytest.raises(PlaceholderSettingsError) as refused:
            rig.real.shoot(TRAILS, applied_plan=_plan())
    assert refused.value.final_shutter_not_issued is True
    assert refused.value.capture_path.is_file()
    assert not any(c[0] == "capture_long_exposure" for c in camera.calls)
    assert camera.capture_count == 1
    for key in ("iso", "aperture", "shutter"):
        assert camera.settings[key] == before[key]


def test_a_focus_preparation_failure_before_the_long_trigger_is_a_no_fire_refusal(rig, monkeypatch):
    # Review (PR #81): a failure switching focus to Manual (before capture_long_exposure is ever
    # called) was reported as "may have fired" because the meter frame had already been taken.
    monkeypatch.setenv("CAMERA_LONG_EXPOSURE", "1")
    with _long_body(rig):
        camera = rig.camera
        before = dict(camera.settings)
        write = camera.set_setting

        def set_setting(setting, value):
            if setting == "focus_mode" and value == "Manual":
                raise CameraError("fixture: body dropped off while switching focus")
            return write(setting, value)
        camera.set_setting = set_setting
        with pytest.raises(CameraError) as refused:
            rig.real.shoot(TRAILS, applied_plan=_plan())
    assert not isinstance(refused.value, CaptureOutcomeUncertain)
    assert refused.value.final_shutter_not_issued is True
    assert refused.value.capture_path.is_file()
    assert not any(c[0] == "capture_long_exposure" for c in camera.calls)
    assert camera.capture_count == 1
    for key in ("iso", "aperture", "shutter", "focus_mode"):
        assert camera.settings[key] == before[key]
    assert refused.value.focus_policy["exposure_restore"] == "verified"


def test_restoration_reconnects_before_its_first_read_when_the_handle_dropped(rig, monkeypatch):
    # Review (PR #81): the restore helper read settings before reconnect(), so a dropped handle on
    # a reconnectable body recorded "failed" and left the metering settings on it.
    import long_single_exposure
    monkeypatch.setenv("CAMERA_LONG_EXPOSURE", "1")
    real_plan = long_single_exposure.plan_long_single_exposure
    with _long_body(rig, ev100=13.0):
        camera = rig.camera
        camera.settings.update(iso="Auto ISO")
        before = dict(camera.settings)
        camera.dropped = False
        read = camera.get_settings
        camera.get_settings = lambda: (_ for _ in ()).throw(CameraError("handle dropped")) if camera.dropped else read()
        reconnect = camera.reconnect

        def reconnect_and_recover():
            camera.dropped = False
            return reconnect()
        camera.reconnect = reconnect_and_recover

        def plan_then_drop(*args, **kwargs):
            result = real_plan(*args, **kwargs)
            camera.dropped = True
            return result
        monkeypatch.setattr(long_single_exposure, "plan_long_single_exposure", plan_then_drop)
        shot = rig.real.shoot(TRAILS, applied_plan=_plan())
    assert shot.evidence["long_exposure"]["status"] == "needs_nd"
    assert shot.evidence["exposure_restore"] == "verified"
    for key in ("iso", "aperture", "shutter"):
        assert camera.settings[key] == before[key]


def test_an_unknown_focus_mode_refuses_before_the_long_settings_are_written(rig, monkeypatch):
    # Review (PR #81): a baseline focus_mode of None was rejected only after the long ISO, aperture
    # and shutter had been written, outside any restoration, and reported as "may have fired".
    monkeypatch.setenv("CAMERA_LONG_EXPOSURE", "1")
    with _long_body(rig):
        camera = rig.camera
        camera.settings.update(iso="Auto ISO", focus_mode=None)
        before = dict(camera.settings)
        with pytest.raises(CameraError, match="focus mode") as refused:
            rig.real.shoot(TRAILS, applied_plan=_plan())
    assert not isinstance(refused.value, CaptureOutcomeUncertain)
    assert refused.value.final_shutter_not_issued is True
    assert refused.value.capture_path.is_file()
    long_writes = [c for c in camera.calls if c[0] == "set_exposure"
                   and c[1].get("shutter") and camera_caps._shutter_seconds(c[1]["shutter"]) > 1]
    assert long_writes == []                                   # the long settings were never written
    assert not any(c[0] == "capture_long_exposure" for c in camera.calls)
    for key in ("iso", "aperture", "shutter"):
        assert camera.settings[key] == before[key]


def test_a_long_refusal_reports_its_restore_like_a_72_precondition_refusal(product, monkeypatch, tmp_path):
    # Merge of #72: one clean 503 no-fire response for both refusals, with meter_restore and the
    # restore warning in detail when putting the exposure back failed.
    import product_capture
    meter = tmp_path / "meter.jpg"
    meter.write_bytes(b"fixture")
    refusal = CameraError("The camera had images pending in its memory; no shutter was issued")
    refusal.final_shutter_not_issued = True
    refusal.capture_path = meter
    refusal.meter_restore = {"status": "failed", "reason": "readback differs"}
    refusal.focus_policy = {"exposure_restore": "failed"}
    refusal.transport_receipt = {"route": "long_exposure", "trigger_commands": 0}

    def refuse(*args, **kwargs):
        raise refusal
    monkeypatch.setattr(product_capture, "shoot", refuse)
    response = product.client.post("/shoot", headers=product.headers, json={"intent": TRAILS})
    assert response.status_code == 503
    body = response.json()
    assert body["may_have_fired"] is False and body["retry_capture"] is False
    assert body["final_shutter_issued"] is False and body["capture_path"] == str(meter)
    assert body["meter_restore"]["status"] == "failed"
    assert "Warning:" in body["detail"] and "readback differs" in body["warning"]


def test_engine_long_refusals_carry_a_72_style_meter_restore(rig, monkeypatch):
    monkeypatch.setenv("CAMERA_LONG_EXPOSURE", "1")
    with _long_body(rig):
        camera = rig.camera
        camera.settings.update(iso="Auto ISO", focus_mode=None)
        with pytest.raises(CameraError) as refused:
            rig.real.shoot(TRAILS, applied_plan=_plan())
    assert refused.value.final_shutter_not_issued is True
    assert refused.value.meter_restore == {"status": "completed"}


def test_the_long_route_never_runs_the_depth_rule_or_its_early_focus_settle(rig, monkeypatch):
    # Merge of #73: its depth-driven aperture plan settles focus before the final exposure on the
    # ordinary route only. The long route plans its own exposure and keeps its own restore scope.
    import aperture_optimizer
    monkeypatch.setenv("CAMERA_LONG_EXPOSURE", "1")
    monkeypatch.setattr(aperture_optimizer, "plan_is_driven", lambda plan: True)
    settles = []
    original = engine.RealEngine._confirm_focus_lock
    monkeypatch.setattr(engine.RealEngine, "_confirm_focus_lock",
                        lambda self, cam, policy, mode: settles.append(policy) or original(self, cam, policy, mode))
    with _long_body(rig):
        shot = rig.real.shoot(TRAILS, applied_plan=_plan())
    assert shot.ok is True and shot.evidence["source"] == "long_single_exposure_plan"
    rig.a7ii.assert_not_called()
    rig.a7iii.assert_not_called()
    assert len(settles) == 1                                  # only _capture_with_meter_focus's own
    assert settles[0] is shot.evidence["final_focus_policy"]
    assert shot.evidence["final_focus_policy"]["exposure_restore"] == "verified"


@pytest.mark.parametrize("ev100", [3.0, 13.0])   # would plan a long exposure / would refuse with ND
def test_fixed_stack_metering_stays_on_the_ordinary_capped_route(rig, monkeypatch, ev100):
    # Review (PR #81): meter_baseline (a #72 Fixed stack) reached the long branch through
    # _brain_shoot(meter_only=True): an ok plan handed the capped session a >1 s baseline, and an
    # ND/unplannable plan returned a Shot where a dict is expected.
    monkeypatch.setenv("CAMERA_LONG_EXPOSURE", "1")
    with _long_body(rig, ev100=ev100):
        metered = rig.real.meter_baseline(TRAILS, applied_plan=_plan())
    assert isinstance(metered, dict)
    assert camera_caps._shutter_seconds(metered["metered"]["shutter"]) <= 1.0
    assert metered["recommendation_source"] != "long_single_exposure_plan"
    assert not any(c[0] == "capture_long_exposure" for c in rig.camera.calls)
    assert rig.camera.capture_count == 1


def test_a_manual_lens_long_request_is_refused_not_silently_taken_short(rig, monkeypatch, module_time):
    # Review (PR #81): f/0 (manual/adapted lens) sent the request to the JPEG-feedback fallback before
    # the long branch, so "light trails" silently took an ordinary <= 1 s photo.
    import sonyremote.camera as camera_mod
    from sonyremote import CapturePreconditionError
    module_time(camera_mod, sleep=lambda _s: None)
    monkeypatch.setenv("CAMERA_LONG_EXPOSURE", "1")
    with _long_body(rig):
        rig.camera.settings["aperture"] = "f/0"
        with patch.object(rig.real, "_autoexpose_shoot", side_effect=AssertionError("capped fallback")):
            with pytest.raises(CapturePreconditionError, match="aperture") as refused:
                rig.real.shoot(TRAILS, applied_plan=_plan())
    assert refused.value.may_have_fired is False
    assert rig.camera.capture_count == 0
    assert not any(c[0] == "capture_long_exposure" for c in rig.camera.calls)


def test_an_identity_refusal_after_the_long_settings_carries_the_restore_state(rig, monkeypatch):
    # Review (PR #81): a CameraIdentityError after the long settings and Manual focus were written went
    # out through the identity serializers without focus_policy / meter_restore, so a failed restore
    # looked like a clean refusal.
    from sonyremote import CameraIdentityError
    monkeypatch.setenv("CAMERA_LONG_EXPOSURE", "1")
    with _long_body(rig):
        camera = rig.camera

        def refuse_before_trigger(shutter, dest_dir=None):
            camera.calls.append(("capture_long_exposure", shutter))
            camera.last_capture_receipt = {"route": "long_exposure", "trigger_commands": 0}
            raise CameraIdentityError("fixture: another body at the port", reason="serial_mismatch")
        camera.capture_long_exposure = refuse_before_trigger

        def reconnect():
            camera.calls.append(("reconnect",))
            raise CameraIdentityError("fixture: still another body", reason="serial_mismatch")
        camera.reconnect = reconnect
        with pytest.raises(CameraIdentityError) as refused:
            rig.real.shoot(TRAILS, applied_plan=_plan())
    exc = refused.value
    assert exc.final_shutter_not_issued is True
    assert exc.focus_policy["exposure_restore"] == "failed"
    assert exc.meter_restore["status"] == "failed"
    state = engine.long_refusal_restore_state(exc)
    assert "ISO, shutter and focus mode" in state["warning"]


def _identity_refusal_with_failed_restore(meter):
    from sonyremote import CameraIdentityError
    refusal = CameraIdentityError("fixture: another body at the port", reason="serial_mismatch")
    refusal.final_shutter_not_issued = True
    refusal.capture_path = meter
    refusal.focus_policy = {"restore": "failed", "exposure_restore": "failed"}
    refusal.meter_restore = {"status": "failed", "reason": "identity refused on reconnect"}
    return refusal


def test_http_identity_refusal_keeps_the_long_restore_state_and_warning(product, monkeypatch, tmp_path):
    import product_capture
    meter = tmp_path / "meter.jpg"
    meter.write_bytes(b"fixture")
    refusal = _identity_refusal_with_failed_restore(meter)

    def refuse(*args, **kwargs):
        raise refusal
    monkeypatch.setattr(product_capture, "shoot", refuse)
    response = product.client.post("/shoot", headers=product.headers, json={"intent": TRAILS})
    assert response.status_code == 409
    body = response.json()
    assert body["reason"] == "serial_mismatch" and body["may_have_fired"] is False
    assert body["focus_policy"]["restore"] == "failed"
    assert body["meter_restore"]["status"] == "failed"
    assert "ISO, shutter and focus mode" in body["warning"] and body["warning"] in body["detail"]


# Electronic stop-down (receipt light-trails/a7iii.json, 29 Sep): the A7 III with the unprofiled
# FE 24-105 F4 G OSS planned 2.5 s at f/4 for light trails (preferred 4 s) because nothing past the
# confirmed f/4 was usable. An electronic lens is now stopped down, confirmed by readback first.
A7III = "Sony Alpha-A7 III (PC Control)"


def _electronic_lens(camera, *, model=A7III, lens="FE 24-105mm F4 G OSS", spec=(24, 105, 4, 4), aperture="4"):
    camera.model = model
    camera.lens_model = lens
    camera.lens_specification = spec
    camera.settings["aperture"] = aperture


@pytest.mark.parametrize("model", [A7III, A7II])
def test_an_electronic_unprofiled_lens_stops_down_for_trails_after_readback(rig, monkeypatch, model):
    monkeypatch.setenv("CAMERA_LONG_EXPOSURE", "1")
    with _long_body(rig):   # EV 3: f/4 needs 2 s, f/8 needs 8 s
        _electronic_lens(rig.camera, model=model)
        before = dict(rig.camera.settings)
        shot = rig.real.shoot(TRAILS, applied_plan=_plan())
    fired = rig.camera.long_captures[0]
    assert fired["aperture"] == "8"
    assert camera_caps._shutter_seconds(fired["shutter"]) >= 4.0     # the preferred trail length
    long_plan = shot.evidence["long_exposure"]
    assert long_plan["stop_down_verified"] is True
    assert long_plan["stop_down"]["status"] == "verified_by_readback"
    assert long_plan["stop_down"]["basis"] == "electronic_lens_readback"
    assert long_plan["stop_down"]["readback_aperture"] == "8"
    assert shot.evidence["profile"]["lens_identity_verified"] is False   # still no lens profile
    writes = [c for c in rig.camera.calls if c[0] == "set_exposure"]
    assert writes[0][1]["aperture"] == "8"
    for key in ("iso", "aperture", "shutter"):
        assert rig.camera.settings[key] == before[key]                   # restored afterwards


@pytest.mark.parametrize("answer", ["refuses", "reads_back_other"])
def test_a_stop_down_that_does_not_read_back_falls_back_to_the_confirmed_aperture(rig, monkeypatch, answer):
    # "refuses" is the real Camera.set_setting contract: it re-reads and raises SettingNotApplied
    # (camera stayed at ...) after ISO was written and before the shutter is.
    from sonyremote import SettingNotApplied
    monkeypatch.setenv("CAMERA_LONG_EXPOSURE", "1")
    with _long_body(rig):
        camera = rig.camera
        _electronic_lens(camera)
        applied = camera.set_exposure

        def set_exposure(**settings):
            if answer == "refuses" and settings.get("aperture") not in (None, "4"):
                applied(iso=settings["iso"])
                camera.settings["aperture"] = "5.6"
                raise SettingNotApplied("Could not set aperture; camera stayed at '5.6'",
                                        key="aperture", requested=settings["aperture"], final="5.6")
            applied(**settings)
            if settings.get("aperture") not in (None, "4"):
                camera.settings["aperture"] = "5.6"     # the body answers with another aperture
        camera.set_exposure = set_exposure
        shot = rig.real.shoot(TRAILS, applied_plan=_plan())
    fired = camera.long_captures[0]
    assert fired["aperture"] == "4"
    assert fired["shutter"] == "2"                                   # today's confirmed-aperture plan
    long_plan = shot.evidence["long_exposure"]
    assert long_plan["stop_down_verified"] is False
    assert long_plan["stop_down"]["status"] == "readback_mismatch"
    assert long_plan["stop_down"]["readback_aperture"] == "5.6"
    assert any("did not confirm" in note for note in long_plan["notes"])
    assert len([c for c in camera.calls if c[0] == "capture_long_exposure"]) == 1


@pytest.mark.parametrize("readback", ["5.6", "nan", "f/nan", "inf", "0", None])
def test_a_fallback_whose_aperture_does_not_read_back_fires_nothing(rig, monkeypatch, readback):
    monkeypatch.setenv("CAMERA_LONG_EXPOSURE", "1")
    with _long_body(rig):
        camera = rig.camera
        _electronic_lens(camera)
        before = dict(camera.settings)
        applied = camera.set_exposure

        def set_exposure(**settings):
            applied(**settings)
            shutter = settings.get("shutter")
            if shutter and camera_caps._shutter_seconds(shutter) > 1:
                camera.settings["aperture"] = readback     # never the planned aperture
        camera.set_exposure = set_exposure
        with pytest.raises(CameraError, match="no shutter was issued") as refused:
            rig.real.shoot(TRAILS, applied_plan=_plan())
    assert refused.value.final_shutter_not_issued is True
    assert not any(c[0] == "capture_long_exposure" for c in camera.calls)
    for key in ("iso", "aperture", "shutter"):
        assert camera.settings[key] == before[key]


def test_a_lens_that_names_itself_nowhere_keeps_the_confirmed_aperture(rig, monkeypatch):
    monkeypatch.setenv("CAMERA_LONG_EXPOSURE", "1")
    with _long_body(rig):
        _electronic_lens(rig.camera, lens=None)
        shot = rig.real.shoot(TRAILS, applied_plan=_plan())
    fired = rig.camera.long_captures[0]
    assert (fired["aperture"], fired["shutter"]) == ("4", "2")
    long_plan = shot.evidence["long_exposure"]
    assert long_plan["stop_down_verified"] is False
    assert long_plan["stop_down"]["status"] == "not_shown"
    assert "names no lens" in long_plan["stop_down"]["reason"]


def test_the_profiled_a7ii_tamron_keeps_its_verified_profile_route(rig, monkeypatch):
    monkeypatch.setenv("CAMERA_LONG_EXPOSURE", "1")
    with _long_body(rig):
        shot = rig.real.shoot(TRAILS, applied_plan=_plan())
    long_plan = shot.evidence["long_exposure"]
    assert long_plan["stop_down_verified"] is True
    assert "stop_down" not in long_plan
    assert rig.camera.long_captures[0]["aperture"] == "8"


def test_a_dark_scene_needs_no_stop_down_and_says_so(rig, monkeypatch):
    monkeypatch.setenv("CAMERA_LONG_EXPOSURE", "1")
    with _long_body(rig, ev100=-1.0):   # f/4 needs 32 s: no aperture past f/4 is wanted
        _electronic_lens(rig.camera)
        shot = rig.real.shoot(TRAILS, applied_plan=_plan())
    long_plan = shot.evidence["long_exposure"]
    assert rig.camera.long_captures[0]["aperture"] == "4"
    assert long_plan["stop_down"]["status"] == "not_needed"
    assert long_plan["stop_down_verified"] is False


def test_a_failed_focus_mode_restore_after_a_delivered_long_exposure_is_warned(rig, monkeypatch):
    # F-81-focus-restore-warning: the body is left in Manual focus; the summary must say so, not
    # only the evidence.
    monkeypatch.setenv("CAMERA_LONG_EXPOSURE", "1")
    with _long_body(rig):
        camera = rig.camera
        original_mode = camera.settings["focus_mode"]
        write = camera.set_setting

        def refuse_the_focus_restore(setting, value):
            if setting == "focus_mode" and value == original_mode and camera.long_captures:
                raise CameraError("fixture focus-mode write refused")
            write(setting, value)
        camera.set_setting = refuse_the_focus_restore
        shot = rig.real.shoot(TRAILS, applied_plan=_plan())
    policy = shot.evidence["final_focus_policy"]
    assert shot.ok is True and policy["restore"] == "failed"
    assert policy["exposure_restore"] == "verified"
    assert camera.settings["focus_mode"] == "Manual"
    assert "focus mode" in shot.summary and original_mode in shot.summary
    assert "earlier exposure settings were not restored" not in shot.summary
