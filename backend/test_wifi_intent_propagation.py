"""Camera-free proof that intent (exposure bias + white balance) reaches the A7 II Smart Remote
verified setters, and that a write the camera cannot confirm fails closed instead of firing the
shutter on stale settings.

The FakeCamera is modelled on backend/test_wifi_lastwave.py's: it subclasses the real CameraRemote
but fully overrides ``call`` (so identity verification never runs) and records every method it is
asked to issue. It is *stateful* — a setter stores the value and the matching getter echoes it —
so the REAL verified-setter logic in wifi_a7ii runs against it. The heavy per-frame collaborators
(vision, metering, the physics brain, motion) are stubbed so the test needs no ML deps or images
beyond a couple of tiny PIL JPEGs.

    ../.venv/bin/python -m pytest -q backend/test_wifi_intent_propagation.py
"""
from __future__ import annotations

import sys
import types
from pathlib import Path

import pytest
from PIL import Image

sys.path.insert(0, str(Path(__file__).resolve().parent))

import capture_analysis
import engine
import wifi_a7ii
import wifi_autopilot
import wifi_engine


# --- a stateful fake camera that runs the real verified setters --------------------------------
class FakeCamera(wifi_a7ii.CameraRemote):
    def __init__(self, *, exposure_comp_available=True, shutter_readback_wrong=False):
        super().__init__("http://cam/sony/camera")
        self.calls: list[tuple[str, object]] = []
        self._shutter_readback_wrong = shutter_readback_wrong
        self._exposure_comp_available = exposure_comp_available
        # Current values a getter echoes; seeded so the pre-metering reads return something sane.
        self._state = {"IsoSpeedRate": "800", "FNumber": "2.8", "ShutterSpeed": "1/60"}
        self._wb = {"whiteBalanceMode": "Auto WB", "colorTemperature": -1}
        self._exp_comp = 0

    # begin_session/grab/download are overridden so nothing touches the network.
    def begin_session(self):
        self.calls.append(("begin_session", None))
        return []

    def grab_liveview_frame(self, stream_url=None, save_path=None):
        if save_path:
            Image.new("RGB", (64, 48), "gray").save(save_path)
        return b""

    def download(self, url, save_path):
        self.calls.append(("download", url))
        Path(save_path).parent.mkdir(parents=True, exist_ok=True)
        Image.new("RGB", (48, 32), "red").save(save_path)
        return save_path

    def call(self, method, params=None, version="1.0", timeout=10.0):
        self.calls.append((method, params))
        # --- available-value ladders (shape: [current, [candidates...]]) ---
        if method == "getAvailableIsoSpeedRate":
            return {"result": ["800", ["800", "400", "200"]]}
        if method == "getAvailableFNumber":
            return {"result": ["2.8", ["2.8", "4.0", "5.6"]]}
        if method == "getAvailableShutterSpeed":
            return {"result": ["1/60", ["1/60", "1/125", "1/250"]]}
        if method == "getAvailableWhiteBalance":
            return {"result": [{"currentWhiteBalanceMode": "Auto WB"},
                               [{"whiteBalanceMode": "Auto WB"},
                                {"whiteBalanceMode": "Color Temperature"},
                                {"whiteBalanceMode": "Daylight"}]]}
        if method == "getAvailableExposureCompensation":
            # Sony flat shape [current, max, min, step]; empty == unsupported on this route.
            return {"result": [0, 9, -9, 1]} if self._exposure_comp_available else {"result": []}
        # --- setters (ack with [0]) + their echoing getters ---
        if method == "setIsoSpeedRate":
            self._state["IsoSpeedRate"] = str(params[0]); return {"result": [0]}
        if method == "getIsoSpeedRate":
            return {"result": [self._state["IsoSpeedRate"]]}
        if method == "setFNumber":
            self._state["FNumber"] = str(params[0]); return {"result": [0]}
        if method == "getFNumber":
            return {"result": [self._state["FNumber"]]}
        if method == "setShutterSpeed":
            self._state["ShutterSpeed"] = str(params[0]); return {"result": [0]}
        if method == "getShutterSpeed":
            if self._shutter_readback_wrong:
                return {"result": ["1/4000"]}   # never agrees -> real setter raises CameraSettingError
            return {"result": [self._state["ShutterSpeed"]]}
        if method == "setWhiteBalance":
            mode, _temp_enabled, color_temp = params
            self._wb = {"whiteBalanceMode": mode, "colorTemperature": color_temp}
            return {"result": [0]}
        if method == "getWhiteBalance":
            return {"result": [self._wb]}
        if method == "setExposureCompensation":
            self._exp_comp = int(params[0]); return {"result": [0]}
        if method == "getExposureCompensation":
            return {"result": [self._exp_comp]}
        if method == "actTakePicture":
            return {"result": [["http://cam/DSC00001.JPG"]]}
        return {"result": [], "id": 1}


def _methods(cam: FakeCamera) -> list[str]:
    return [method for method, _ in cam.calls]


def _install_stubs(monkeypatch, tmp_path, cam):
    """Wire a WiFiEngine to `cam` and stub the heavy per-frame collaborators (camera-free)."""
    monkeypatch.setattr(engine, "SHOTS_DIR", tmp_path / "shots")
    monkeypatch.setattr(wifi_engine.WiFiEngine, "_smart_remote_camera", lambda self: cam)
    monkeypatch.setattr(wifi_a7ii.time, "sleep", lambda *_a, **_k: None)
    monkeypatch.setattr(capture_analysis, "motion_between_files",
                        lambda *a, **k: {"subject_motion_floor_s": None})
    vision_stub = types.SimpleNamespace(
        analyze=lambda *a, **k: None,
        metering_region=lambda *a, **k: None,
        subject_distance_m=lambda *a, **k: None,
        dof_depth_m=lambda *a, **k: None,
        scene_class=lambda *a, **k: "neutral",
    )
    monkeypatch.setattr(wifi_autopilot, "vision_banana", vision_stub)
    monkeypatch.setattr(wifi_autopilot.live_verify, "meter_ev100", lambda *a, **k: (10.0, False))
    fake_rec = types.SimpleNamespace(iso_label="800", aperture_label="2.8",
                                     shutter_label="1/60", cards=[])
    monkeypatch.setattr(wifi_autopilot.brain, "recommend_settings", lambda *a, **k: fake_rec)


def _plan(target=112.0, white_balance=None, exposure_bias=None, motion_intent=None):
    mood = {"settings": {}}
    if exposure_bias is not None:
        mood["settings"]["exposure_bias"] = exposure_bias
    return {"target_brightness": target, "white_balance": white_balance,
            "motion_intent": motion_intent, "mood": mood}


# --- tests -------------------------------------------------------------------------------------
def test_full_intent_applies_wb_and_exposure_comp_before_capture(tmp_path, monkeypatch):
    """(a) warm WB + over bias -> ISO/aperture/shutter/WB(7500)/exp-comp(+3) all verified-applied
    BEFORE take_picture."""
    cam = FakeCamera()
    _install_stubs(monkeypatch, tmp_path, cam)
    plan = _plan(target=140.0, white_balance="warm", exposure_bias="over")

    shot = wifi_engine.WiFiEngine().shoot("warm and bright", applied_plan=plan)
    assert shot.ok, shot

    methods = _methods(cam)
    shutter_idx = methods.index("actTakePicture")
    for setter in ("setIsoSpeedRate", "setFNumber", "setShutterSpeed",
                   "setWhiteBalance", "setExposureCompensation"):
        assert setter in methods, methods
        assert methods.index(setter) < shutter_idx, (setter, methods)
    # WB written as Color Temperature 7500 K; exposure comp clamped-and-set to +3 (1/3 EV steps).
    wb_params = next(p for m, p in cam.calls if m == "setWhiteBalance")
    assert wb_params == ["Color Temperature", True, 7500], wb_params
    exp_params = next(p for m, p in cam.calls if m == "setExposureCompensation")
    assert exp_params == [3], exp_params


def test_white_balance_none_issues_no_set_white_balance(tmp_path, monkeypatch):
    """(b) WB None -> setWhiteBalance is never called at all."""
    cam = FakeCamera()
    _install_stubs(monkeypatch, tmp_path, cam)
    plan = _plan(target=112.0, white_balance=None, exposure_bias=None)

    shot = wifi_engine.WiFiEngine().shoot(None, applied_plan=plan)
    assert shot.ok, shot
    assert "setWhiteBalance" not in _methods(cam), cam.calls


def test_unverifiable_shutter_fails_closed_before_capture(tmp_path, monkeypatch):
    """(c) set_shutter cannot be verified -> take_picture NOT called, Shot.ok False, the report
    names the failed setter."""
    cam = FakeCamera(shutter_readback_wrong=True)
    _install_stubs(monkeypatch, tmp_path, cam)
    plan = _plan(target=100.0, white_balance="warm", exposure_bias="over")

    shot = wifi_engine.WiFiEngine().shoot("warm", applied_plan=plan)
    assert shot.ok is False, shot
    assert "actTakePicture" not in _methods(cam), cam.calls
    assert shot.settings["failed_setter"] == "setShutterSpeed", shot.settings
    assert shot.settings["failed_field"] == "shutter", shot.settings
    # recommended (asked-for) and applied (what stuck) are distinct and truthful.
    assert shot.settings["recommended"] == "ISO 800  f/2.8  1/60", shot.settings
    assert "shutter" not in (shot.settings["applied"] or {}), shot.settings
    # WB/exposure-comp come after shutter, so they were never reached on the wire.
    assert "setWhiteBalance" not in _methods(cam), cam.calls


def test_exposure_comp_unsupported_is_skipped_and_capture_proceeds(tmp_path, monkeypatch):
    """(d) exposure compensation advertised as unavailable (empty list) -> capture proceeds and the
    report marks exposure_comp skipped with a reason (intent NOT applied for that field)."""
    cam = FakeCamera(exposure_comp_available=False)
    _install_stubs(monkeypatch, tmp_path, cam)
    plan = _plan(target=112.0, white_balance=None, exposure_bias="over")

    shot = wifi_engine.WiFiEngine().shoot(None, applied_plan=plan)
    assert shot.ok, shot
    assert "setExposureCompensation" not in _methods(cam), cam.calls
    skipped = (shot.settings["applied_over_wifi"] or {}).get("exposure_comp")
    assert isinstance(skipped, dict) and "skipped" in skipped, shot.settings
    assert skipped["skipped"], skipped


def test_wifi_engine_forwards_exposure_bias_from_plan(tmp_path, monkeypatch):
    """(e) wifi_engine pulls exposure_bias from plan['mood']['settings'] and forwards it (and a
    None white balance) to wifi_autopilot.shoot without coercion."""
    cam = FakeCamera()
    _install_stubs(monkeypatch, tmp_path, cam)
    captured: dict = {}
    real_shoot = wifi_autopilot.shoot

    def spy(camera, **kwargs):
        captured.update(kwargs)
        return real_shoot(camera, **kwargs)

    monkeypatch.setattr(wifi_autopilot, "shoot", spy)
    plan = _plan(target=90.0, white_balance=None, exposure_bias="under")

    shot = wifi_engine.WiFiEngine().shoot(None, applied_plan=plan)
    assert shot.ok, shot
    assert captured["exposure_bias"] == "under", captured
    assert captured["white_balance"] is None, captured   # no coercion to "auto"


if __name__ == "__main__":
    import pytest as _pytest
    raise SystemExit(_pytest.main([__file__, "-q"]))
