"""Hardware-free camera swapping regressions; no USB claims or captures."""
from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "backend"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import engine
from sonyremote.camera import Camera as USBCamera, CameraError, gp
from _runner import run_module_tests


# Product capture names the body by its USB serial; the engine refuses without one.
SERIAL = "A7III0SERIAL"


class Camera:
    model = "Sony Alpha-A7 III (PC Control)"

    def __init__(self, model=None, port=None, serial=None):
        self.model = model or type(self).model
        self.port = port or "usb:001,001"
        self.settings = {"white_balance": "Automatic", "iso": "100", "shutter": "1/125"}

    def info(self):
        return SimpleNamespace(model=self.model, port=self.port)

    def shooting_mode(self):
        return "M"

    def list_options(self, key):
        return ["Automatic", "Daylight"] if key == "white_balance" else []

    def get_settings(self):
        return self.settings.copy()

    def set_setting(self, key, value):
        self.settings[key] = value
        return value

    def wake(self):
        return True

    def __enter__(self):
        return self

    def __exit__(self, *args):
        pass


def _rows(*models, method="autodetect", serial=SERIAL):
    return [{"model": m, "port": "usb:001,001", "serial": serial,
             "method": method, "os_source": "ioreg"} for m in models]


def test_transfer_mode_is_not_ready():
    for model in ("USB PTP Class Camera", "Sony Alpha-A7 III (MTP mode)"):
        with patch("sonyremote.detect_identified", return_value=_rows(model)):
            status = engine.RealEngine().status()
        assert status["ready"] is False
        assert "PC Remote" in status["hint"]


def test_control_mode_is_detected():
    with patch("sonyremote.detect_identified", return_value=_rows(Camera.model)):
        assert engine.RealEngine().status()["ready"] is True
    with patch("sonyremote.detect_identified", return_value=[]):
        assert engine.RealEngine().status()["ready"] is False
    with patch("sonyremote.detect_identified", return_value=_rows("Unconfigured camera")):
        assert engine.RealEngine().status()["ready"] is False


def test_status_surfaces_identification_method_and_serial():
    # A recovered A7 II (mislabelled by the driver) must be selectable and badged as recovered,
    # while its OS serial identifies which physical body it is; existing keys stay intact.
    rows = _rows("Sony ILCE-7M2 (Control)", method="recovered_probe", serial="A7II00SERIAL")
    with patch("sonyremote.detect_identified", return_value=rows):
        status = engine.RealEngine().status()
    assert status["ready"] is True
    camera = status["cameras"][0]
    assert camera["model"] == "Sony ILCE-7M2 (Control)"
    assert camera["supported"] is True
    assert camera["identification"] == "recovered_probe"
    assert camera["serial"] == "A7II00SERIAL"


def test_a7iii_uses_live_metering_and_honors_corrected_target():
    real = engine.RealEngine(camera_serial=SERIAL)
    plan = {"target_brightness": 112, "white_balance": "auto"}
    shot = engine.Shot(True, "fixture.jpg", "fixture", "fixture", {}, {})
    with patch("sonyremote.Camera", Camera), patch.object(engine, "_free_usb"), \
         patch.object(engine.intent_brain, "interpret", return_value=plan), \
         patch.object(real, "_brain_shoot", return_value=shot) as metered, \
         patch.object(real, "_autoexpose_shoot", side_effect=AssertionError("unexpected fallback")):
        assert real.shoot("moody", target_override=90) is shot
        assert metered.call_args.args[1]["target_brightness"] == 90
        assert metered.call_args.args[0].info().model == Camera.model
        assert shot.applied["white_balance"]["verified"] is True
        assert shot.evidence["camera"] == Camera.model


def test_a7ii_keeps_existing_brain_path():
    camera = Camera()
    camera.model = "Sony ILCE-7M2 (Control)"
    real = engine.RealEngine(camera_serial=SERIAL)
    shot = engine.Shot(True, "fixture.jpg", "fixture", "fixture", {}, {})
    with patch("sonyremote.Camera", return_value=camera), patch.object(engine, "_free_usb"), \
         patch.object(engine.intent_brain, "interpret", return_value={"target_brightness": 112}), \
         patch.object(real, "_brain_shoot", return_value=shot) as brain, \
         patch.object(real, "_autoexpose_shoot", side_effect=AssertionError("unexpected fallback")):
        assert real.shoot(None) is shot
        assert brain.call_args.args[0].info().model == "Sony ILCE-7M2 (Control)"


def test_engine_passes_explicit_camera_selection_and_refuses_unsupported_body():
    real = engine.RealEngine(camera_model="Unsupported body", camera_port="usb:001,009", camera_serial=SERIAL)
    camera = Camera(model="Unsupported body", port="usb:001,009")
    with patch("sonyremote.Camera", return_value=camera) as constructor, \
         patch.object(engine, "_free_usb"), \
         patch.object(engine.intent_brain, "interpret", return_value={"target_brightness": 112}), \
         patch.object(real, "_brain_shoot") as brain:
        try:
            real.shoot(None)
        except CameraError as exc:
            assert "not configured" in str(exc)
        else:
            raise AssertionError("unsupported camera was accepted")
        constructor.assert_called_once_with(model="Unsupported body", port="usb:001,009", serial=SERIAL)
        brain.assert_not_called()


def test_camera_error_never_falls_back_to_another_shutter():
    real = engine.RealEngine(camera_serial=SERIAL)
    with patch("sonyremote.Camera", Camera), patch.object(engine, "_free_usb"), \
         patch.object(engine.intent_brain, "interpret", return_value={"target_brightness": 112}), \
         patch.object(real, "_brain_shoot", side_effect=CameraError("uncertain capture")), \
         patch.object(real, "_autoexpose_shoot") as fallback:
        try:
            real.shoot(None)
        except CameraError:
            pass
        else:
            raise AssertionError("uncertain capture was accepted")
        fallback.assert_not_called()


def test_sleeping_camera_does_not_shoot():
    camera = Camera()
    camera.wake = lambda: False
    real = engine.RealEngine(camera_serial=SERIAL)
    with patch("sonyremote.Camera", return_value=camera), patch.object(engine, "_free_usb"), \
         patch.object(engine.intent_brain, "interpret", return_value={}), \
         patch.object(real, "_autoexpose_shoot", side_effect=AssertionError("shot while asleep")):
        try:
            real.shoot(None)
        except CameraError:
            pass
        else:
            raise AssertionError("sleeping camera was accepted")


def test_a7iii_ambiguous_capture_does_not_fire_twice():
    camera = USBCamera(allow_unverified_identity=True)
    low = SimpleNamespace(capture=lambda _: None)
    camera._camera = low
    # The generic path enforces the 1 s cap, so the body must report a readable shutter.
    with patch.object(camera, "_has_sony_action_capture", return_value=False), \
         patch.object(camera, "get_settings", return_value={"shutter": "1/60"}), \
         patch.object(camera, "info", return_value=Camera().info()), \
         patch.object(low, "capture", side_effect=gp.GPhoto2Error(-1)) as capture, \
         patch.object(camera, "set_setting", side_effect=AssertionError("changed focus after error")):
        try:
            camera.capture()
        except CameraError as exc:
            assert "not retrying" in str(exc)
        else:
            raise AssertionError("ambiguous capture was accepted")
        assert capture.call_count == 1


def test_describe_range_and_toggle_do_not_request_menu_choices():
    for kind, value in ((gp.GP_WIDGET_RANGE, 5500.0), (gp.GP_WIDGET_TOGGLE, 2)):
        widget = SimpleNamespace(get_type=lambda: kind, get_value=lambda: value,
                                 get_readonly=lambda: 0, get_range=lambda: (2500, 9900, 100))
        camera = USBCamera()
        camera._camera = SimpleNamespace(get_config=lambda: SimpleNamespace(
            get_child_by_name=lambda _: widget))
        description = camera.describe_setting("colortemperature" if kind == gp.GP_WIDGET_RANGE else "movie")
        assert description["value"] == value
        assert description["readonly"] is False
        assert "choices" not in description
        assert ("range" in description) == (kind == gp.GP_WIDGET_RANGE)


def test_a7iii_manual_lens_can_wake_with_valid_shutter_and_iso():
    camera = USBCamera()
    with patch.object(camera, "info", return_value=Camera().info()), \
         patch.object(camera, "get_settings", return_value={"aperture": "f/0", "iso": "100", "shutter": "1/60"}):
        assert camera.wake(timeout=0.1)


def test_a7iii_uninitialized_shutter_is_not_awake():
    camera = USBCamera()
    with patch.object(camera, "info", return_value=Camera().info()), \
         patch.object(camera, "get_settings", return_value={"aperture": "f/0", "iso": "100", "shutter": "65535/65535"}), \
         patch("sonyremote.camera.time.sleep"):
        assert camera.wake(timeout=0.01) is False


def test_a7iii_movie_control_is_not_assumed_verified():
    with patch("sonyremote.Camera", Camera), patch("sonyremote.assert_recording_allowed"), \
         patch.object(engine.intent_brain, "interpret", return_value={}):
        try:
            engine.RealEngine(camera_serial=SERIAL).record(None, seconds=1)
        except NotImplementedError as exc:
            assert "start/stop verification" in str(exc)
        else:
            raise AssertionError("unverified movie toggles were accepted")


if __name__ == "__main__":
    raise SystemExit(run_module_tests(__name__))
