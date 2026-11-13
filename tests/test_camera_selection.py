"""No-hardware identity, capture, and bounded-exposure regressions."""
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch
import tempfile
import io
import json
import subprocess
import stat
from contextlib import redirect_stdout

import pytest

from sonyremote.camera import Camera, CameraError, CameraInfo, CaptureOutcomeUncertain, gp
from sonyremote.autoexpose import auto_expose, _apply_correction, _shift_in_list

A7II = "Sony ILCE-7M2 (Control)"
A7III = "Sony Alpha-A7 III (PC Control)"
BODIES = [(A7II, "usb:000,003"), (A7III, "usb:001,005")]


def test_ambiguous_selection_never_claims_camera():
    with patch.dict("os.environ", {}, clear=True), patch("sonyremote.camera.detect", return_value=BODIES), \
         patch.object(gp, "Camera") as low:
        with pytest.raises(CameraError, match="Multiple cameras"):
            Camera().connect()
        low.assert_not_called()


def test_wrong_model_port_pair_never_claims_camera():
    with patch("sonyremote.camera.detect", return_value=BODIES), patch.object(gp, "Camera") as low:
        with pytest.raises(CameraError, match="No camera matches"):
            Camera(model=A7II, port=BODIES[1][1]).connect()
        low.assert_not_called()


@pytest.mark.parametrize("explicit", [True, False])
def test_selection_binds_exact_abilities_and_port_and_stays_pinned(explicit):
    low = MagicMock()
    low.get_abilities.return_value.model = A7II
    low.get_port_info.return_value.get_path.return_value = BODIES[0][1]
    ports, abilities = MagicMock(), MagicMock()
    ports.lookup_path.return_value = 7
    abilities.lookup_model.return_value = 11
    with patch.dict("os.environ", {"CAMERA_MODEL": A7III, "CAMERA_PORT": BODIES[1][1]}), \
         patch("sonyremote.camera.detect", return_value=BODIES) as detected, \
         patch.object(gp, "Camera", return_value=low), \
         patch.object(gp, "PortInfoList", return_value=ports), \
         patch.object(gp, "CameraAbilitiesList", return_value=abilities):
        if explicit:
            camera = Camera(model=A7II, port=BODIES[0][1])
        else:
            with patch.dict("os.environ", {"CAMERA_MODEL": A7II, "CAMERA_PORT": BODIES[0][1]}):
                camera = Camera()
        assert camera.connect() == CameraInfo(*BODIES[0])
        ports.lookup_path.assert_called_once_with(BODIES[0][1])
        abilities.lookup_model.assert_called_once_with(A7II)
        low.set_port_info.assert_called_once_with(ports[7])
        low.set_abilities.assert_called_once_with(abilities[11])
        camera.disconnect()
        detected.return_value = [BODIES[1]]
        with pytest.raises(CameraError, match="No camera matches"):
            camera.connect()
        assert low.init.call_count == 1


def test_connect_rejects_identity_change_and_releases_claim():
    low = MagicMock()
    low.get_abilities.return_value.model = A7III
    low.get_port_info.return_value.get_path.return_value = BODIES[0][1]
    with patch("sonyremote.camera.detect", return_value=[BODIES[0]]), \
         patch.object(gp, "Camera", return_value=low), patch.object(gp, "PortInfoList"), \
         patch.object(gp, "CameraAbilitiesList"):
        camera = Camera(model=A7II, port=BODIES[0][1])
        with pytest.raises(CameraError, match="identity changed"):
            camera.connect()
        low.exit.assert_called_once()
        assert camera._camera is None


@pytest.mark.parametrize("model", [A7II, A7III])
def test_generic_uncertain_capture_never_retries_or_changes_focus(model):
    camera = Camera(model=model, allow_unverified_identity=True)
    camera._camera = MagicMock()
    camera._camera.capture.side_effect = gp.GPhoto2Error(-1)
    # The generic path enforces the 1 s cap, so the body must report a readable shutter.
    with patch.object(camera, "_has_sony_action_capture", return_value=False), \
         patch.object(camera, "get_settings", return_value={"shutter": "1/60"}), \
         patch.object(camera, "set_setting") as setting:
        with pytest.raises(CameraError, match="not retrying"):
            camera.capture()
        assert camera._camera.capture.call_count == 1
        setting.assert_not_called()


@pytest.mark.parametrize("error", [False, True])
def test_action_capture_pins_cli_and_does_not_retry_ambiguous_error(error):
    camera = Camera()
    result = SimpleNamespace(returncode=1 if error else 0, stdout="", stderr="Could not claim the USB device")
    with tempfile.TemporaryDirectory() as tmp, \
         patch.object(camera, "info", return_value=CameraInfo(*BODIES[0])), \
         patch.object(camera, "get_settings", return_value={"shutter": "1/60"}), \
         patch.object(camera, "disconnect"), patch.object(camera, "connect") as reconnect, \
         patch("sonyremote.camera.shutil.which", return_value="/fake/gphoto2"), \
         patch("sonyremote.camera.time.sleep"), \
         patch("sonyremote.camera.subprocess.run", return_value=result) as run:
        if error:
            with pytest.raises(CameraError, match="action capture failed"):
                camera._capture_sony_action(download=False, dest_dir=Path(tmp))
        else:
            assert camera._capture_sony_action(download=False, dest_dir=Path(tmp)) is None
        command = run.call_args.args[0]
        assert command[command.index("--camera") + 1] == A7II
        assert command[command.index("--port") + 1] == BODIES[0][1]
        assert camera._selected == BODIES[0]
        run.assert_called_once()
        reconnect.assert_called_once()


def test_a7iii_native_movie_is_blocked_before_toggle_or_quarantine():
    camera = Camera()
    camera._selected = BODIES[1]
    with patch("sonyremote.camera.assert_recording_allowed"), \
         patch.object(camera, "_press_movie_button") as press:
        assert camera.supports_native_movie() is False
        with pytest.raises(NotImplementedError, match="start/stop verification"):
            camera.start_movie()
        with pytest.raises(NotImplementedError, match="start/stop verification"):
            camera.record_native(1)
        with pytest.raises(NotImplementedError, match="start/stop verification"):
            camera.stop_movie()
        press.assert_not_called()
        assert not camera._recording_quarantined


class MeterCamera:
    def __init__(self):
        self.settings = {"iso": "Auto ISO", "shutter": "1/8"}
        self.writes = []
        self.captured = []

    def shooting_mode(self):
        return "M"

    def get_settings(self):
        return self.settings.copy()

    def list_options(self, key):
        return {"iso": ["Auto ISO", "400", "100", "200"],
                "shutter": ["65535/65535", "1/125", "1/8", "1/250", "Bulb"]}[key]

    def set_setting(self, key, value):
        self.writes.append((key, value))
        self.settings[key] = value

    def capture(self, download):
        self.captured.append(self.get_settings())
        return Path("fixture.jpg")


def test_motion_limit_is_applied_before_first_frame_and_iso_rises_at_limit():
    camera = MeterCamera()
    measurements = [{"mean": 40, "blown_pct": 0}, {"mean": 112, "blown_pct": 0}]
    with patch("sonyremote.autoexpose.measure_jpeg", side_effect=[*measurements, measurements[-1]]):
        result = auto_expose(camera, max_iter=2, max_shutter_s=1/125)
    assert camera.captured == [{"iso": "100", "shutter": "1/125"},
                               {"iso": "400", "shutter": "1/125"}]
    assert result["ok"] is True
    assert result["final_settings"] == camera.captured[-1]


def test_no_unphotographed_correction_after_last_frame():
    camera = MeterCamera()
    camera.settings["iso"] = "100"
    with patch("sonyremote.autoexpose.measure_jpeg", return_value={"mean": 20, "blown_pct": 0}):
        result = auto_expose(camera, max_iter=1)
    assert result["ok"] is False
    assert camera.writes == []
    assert result["final_settings"] == camera.captured[-1]


def test_option_order_and_sentinel_do_not_change_exposure_direction():
    assert _shift_in_list(["1/125", "65535/65535", "1/8", "1/250", "Bulb"],
                          "1/125", 1, True) == "1/8"


@pytest.mark.parametrize("limit", [0, -1, float("inf"), float("nan")])
def test_invalid_motion_limit_is_rejected_before_camera_access(limit):
    camera = MagicMock()
    with pytest.raises(ValueError, match="max_shutter_s"):
        auto_expose(camera, max_shutter_s=limit)
    camera.capture.assert_not_called()


def test_motion_limit_is_not_silently_ignored_in_auto_mode():
    camera = MagicMock()
    camera.shooting_mode.return_value = "Aperture Priority"
    with pytest.raises(CameraError, match="requires M mode"):
        auto_expose(camera, max_shutter_s=1/125)
    camera.capture.assert_not_called()


def test_inspect_list_never_opens_any_body():
    from tools.inspect_usb_camera import main
    output = io.StringIO()
    rows = [{"model": model, "port": port, "serial": None, "method": "autodetect"} for model, port in BODIES]
    with patch("tools.inspect_usb_camera.detect") as detect, \
         patch("tools.inspect_usb_camera.detect_identified", return_value=rows) as identified, \
         patch("tools.inspect_usb_camera.Camera") as camera, redirect_stdout(output):
        assert main(["--list"]) == 0
    camera.assert_not_called()
    # One discovery pass (its recovery probe may briefly claim a body): never detect() on top.
    identified.assert_called_once_with()
    detect.assert_not_called()
    assert json.loads(output.getvalue())["detected"] == [list(body) for body in BODIES]


def test_inspect_cli_passes_exact_selectors():
    from tools.inspect_usb_camera import main
    with patch("tools.inspect_usb_camera.detect", return_value=BODIES), \
         patch("tools.inspect_usb_camera.Camera") as camera, \
         patch("tools.inspect_usb_camera.inspect_camera", return_value={"awake": True}), \
         redirect_stdout(io.StringIO()):
        assert main(["--model", A7II, "--port", BODIES[0][1]]) == 0
    camera.assert_called_once_with(model=A7II, port=BODIES[0][1])


@pytest.mark.parametrize("applied", [True, False])
def test_kelvin_success_requires_actual_readback(applied):
    wb = MagicMock()
    wb.count_choices.return_value = 1
    wb.get_choice.return_value = "Color Temperature"
    wb.get_value.return_value = "Color Temperature"
    temperature = MagicMock()
    temperature.get_value.return_value = 7500 if applied else 5500
    config = MagicMock()
    config.get_child_by_name.side_effect = {"whitebalance": wb, "colortemperature": temperature}.__getitem__
    camera = Camera()
    camera._camera = MagicMock()
    camera._camera.get_config.return_value = config
    with patch("sonyremote.camera.time.sleep"):
        assert camera.set_color_temp(7500) is applied
    assert camera._camera.set_config.call_count == 2
    assert temperature.get_value.call_count == (1 if applied else 20)


def test_kelvin_waits_for_delayed_readback_without_repeating_actuation():
    wb = MagicMock()
    wb.count_choices.return_value = 1
    wb.get_choice.return_value = "Color Temperature"
    wb.get_value.return_value = "Color Temperature"
    temperature = MagicMock()
    temperature.get_value.side_effect = [5500.0, 5500.0, 7500.0]
    config = MagicMock()
    config.get_child_by_name.side_effect = {"whitebalance": wb, "colortemperature": temperature}.__getitem__
    camera = Camera()
    camera._camera = MagicMock()
    camera._camera.get_config.return_value = config
    with patch("sonyremote.camera.time.sleep") as sleep:
        assert camera.set_color_temp(7500) is True
        assert sleep.call_count == 3
        assert all(call.args[0] <= 0.1 for call in sleep.call_args_list)
    wb.set_value.assert_called_once_with("Color Temperature")
    temperature.set_value.assert_called_once_with(7500)
    assert camera._camera.set_config.call_count == 2


def test_successful_jpeg_receipt_survives_reconnect_failure():
    camera = Camera()

    def captured(_command, **kwargs):
        (kwargs["cwd"] / "DSC00042.JPG").write_bytes(b"verified fixture bytes")
        return SimpleNamespace(returncode=0, stderr="", stdout="captured")

    with tempfile.TemporaryDirectory() as tmp, \
         patch.object(camera, "info", return_value=CameraInfo(*BODIES[0])), \
         patch.object(camera, "get_settings", return_value={"shutter": "1/60"}), \
         patch.object(camera, "disconnect"), \
         patch.object(camera, "connect", side_effect=CameraError("USB disconnected")), \
         patch("sonyremote.camera.shutil.which", return_value="/fake/gphoto2"), \
         patch("sonyremote.camera.time.sleep"), \
         patch("sonyremote.camera.subprocess.run", side_effect=captured) as run:
        Path(tmp).chmod(0o755)
        path = camera._capture_sony_action(dest_dir=Path(tmp))
        assert path.read_bytes() == b"verified fixture bytes"
        assert stat.S_IMODE(path.stat().st_mode) == 0o600
        assert stat.S_IMODE(Path(tmp).stat().st_mode) == 0o755
        assert camera.last_capture_receipt["path"] == str(path)
        assert camera.last_capture_receipt["capture_confirmed"] is True
        assert camera.last_capture_receipt["reconnect_ok"] is False
        assert "USB disconnected" in camera.last_capture_receipt["warning"]
        run.assert_called_once()


@pytest.mark.parametrize("timeout", [True, False])
def test_uncertain_command_preserves_type_when_reconnect_also_fails(timeout):
    camera = Camera()
    failure = subprocess.TimeoutExpired("gphoto2", 18) if timeout else None
    result = SimpleNamespace(returncode=1, stderr="transport failed after shutter", stdout="")
    with tempfile.TemporaryDirectory() as tmp, \
         patch.object(camera, "info", return_value=CameraInfo(*BODIES[0])), \
         patch.object(camera, "get_settings", return_value={"shutter": "1/60"}), \
         patch.object(camera, "disconnect"), \
         patch.object(camera, "connect", side_effect=CameraError("USB disconnected")), \
         patch("sonyremote.camera.shutil.which", return_value="/fake/gphoto2"), \
         patch("sonyremote.camera.time.sleep"), \
         patch("sonyremote.camera.subprocess.run", side_effect=failure, return_value=result) as run:
        with pytest.raises(CaptureOutcomeUncertain) as error:
            camera._capture_sony_action(dest_dir=Path(tmp))
        assert error.value.may_have_fired is True
        assert error.value.retry_safe is False
        assert "no automatic retry" in str(error.value)
        assert camera.last_capture_receipt["reconnect_ok"] is False
        run.assert_called_once()


def test_autoexposure_retains_captured_image_and_stops_after_reconnect_failure():
    camera = MeterCamera()
    camera.settings["iso"] = "100"
    reads = []

    def settings():
        reads.append(True)
        if len(reads) > 2:
            raise AssertionError("settings read after known reconnect failure")
        return camera.settings.copy()

    def capture(download):
        camera.captured.append(camera.settings.copy())
        camera.last_capture_receipt = {"path": "fixture.jpg", "capture_confirmed": True,
                                       "reconnect_ok": False, "warning": "USB disconnected"}
        return Path("fixture.jpg")

    camera.get_settings = settings
    camera.capture = capture
    with patch("sonyremote.autoexpose.measure_jpeg", return_value={"mean": 40, "blown_pct": 0}):
        result = auto_expose(camera, max_iter=5)
    assert len(camera.captured) == 1
    assert result["final_image"] == "fixture.jpg"
    assert result["final_settings"] == camera.captured[0]
    assert result["capture_warning"] == "USB disconnected"
    assert result["ok"] is False
    assert camera.writes == []


@pytest.mark.parametrize("default_destination", [True, False])
def test_generic_captured_file_is_private_and_only_owned_directory_is_tightened(default_destination):
    camera = Camera(allow_unverified_identity=True)
    camera._camera = MagicMock()
    camera._camera.capture.return_value = SimpleNamespace(folder="/DCIM", name="DSC00055.JPG")

    def save(destination):
        path = Path(destination)
        assert stat.S_IMODE(path.stat().st_mode) == 0o600
        path.write_bytes(b"private image bytes")

    camera._camera.file_get.return_value.save.side_effect = save
    with tempfile.TemporaryDirectory() as temporary:
        directory = Path(temporary) / "photos"
        directory.mkdir(mode=0o755)
        directory.chmod(0o755)
        # The generic path enforces the 1 s cap, so the body must report a readable shutter.
        with patch("sonyremote.camera.CAPTURE_DIR", directory), \
             patch.object(camera, "get_settings", return_value={"shutter": "1/60"}), \
             patch.object(camera, "_has_sony_action_capture", return_value=False):
            image = camera.capture(dest_dir=None if default_destination else directory)
        assert stat.S_IMODE(image.stat().st_mode) == 0o600
        assert stat.S_IMODE(directory.stat().st_mode) == (0o700 if default_destination else 0o755)
        assert image.read_bytes() == b"private image bytes"
