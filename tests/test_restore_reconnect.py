"""Restoration after a slow USB re-enumeration must reconnect, not fail on a dropped handle."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import product_sessions  # noqa: E402
from sonyremote import CameraError  # noqa: E402
from sonyremote.camera import Camera  # noqa: E402


class _DroppedCamera:
    def __init__(self, fail_connects=1):
        self.connected = False
        self.fail_connects = fail_connects
        self.connect_calls = 0
        self.settings = {"iso": "6400", "aperture": "f/2.8", "shutter": "1/15", "focus_mode": "Manual"}

    def get_settings(self):
        if not self.connected:
            raise CameraError("Not connected. Call connect() first.")
        return dict(self.settings)

    def connect(self):
        self.connect_calls += 1
        if self.connect_calls <= self.fail_connects:
            raise CameraError("No camera matches; re-enumerating")
        self.connected = True

    def set_exposure(self, **kw):
        assert self.connected
        self.settings.update({k: v for k, v in kw.items() if v is not None})

    def set_setting(self, key, value):
        assert self.connected
        self.settings[key] = value


def test_restore_reconnects_a_dropped_handle_with_bounded_retries(monkeypatch):
    monkeypatch.setattr(product_sessions.time, "sleep", lambda s: None)
    cam = _DroppedCamera(fail_connects=1)
    baseline = {"iso": "2000", "aperture": "f/7.1", "shutter": "1/60", "focus_mode": "Automatic"}
    product_sessions._restore(cam, baseline)
    assert cam.connect_calls == 2 and cam.settings["iso"] == "2000" and cam.settings["focus_mode"] == "Automatic"


def test_restore_surfaces_a_body_that_never_returns(monkeypatch):
    monkeypatch.setattr(product_sessions.time, "sleep", lambda s: None)
    cam = _DroppedCamera(fail_connects=99)
    baseline = {"iso": "2000", "aperture": "f/7.1", "shutter": "1/60", "focus_mode": "Automatic"}
    try:
        product_sessions._restore(cam, baseline)
    except CameraError as exc:
        assert "reconnected" in str(exc)
    else:
        raise AssertionError("a permanently missing body must fail restoration explicitly")


def test_camera_reconnect_backoff_retries_then_succeeds(monkeypatch):
    cam = Camera.__new__(Camera)
    calls = {"n": 0}

    def fake_connect():
        calls["n"] += 1
        if calls["n"] < 3:
            raise CameraError("re-enumerating")
        return "info"

    monkeypatch.setattr(cam, "connect", fake_connect)
    monkeypatch.setattr("sonyremote.camera.time.sleep", lambda s: None)
    assert cam._reconnect_with_backoff() == "info" and calls["n"] == 3
