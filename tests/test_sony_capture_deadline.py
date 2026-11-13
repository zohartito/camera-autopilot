"""Confirmed long shutter settings extend only the bounded CLI deadline."""
from pathlib import Path
import sys
from types import SimpleNamespace
from unittest.mock import patch
import pytest
from sonyremote.camera import Camera, CameraInfo, CameraError, sony_capture_deadline
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))
from long_exposure_plan import plan_long_exposure


@pytest.mark.parametrize("setting,expected", [("30", 78.0), ("1/60", 18 + 2/60), ("8/10", 19.6)])
def test_deadline_tracks_confirmed_shutter_plus_fixed_transfer_allowance(setting, expected):
    result = sony_capture_deadline(setting)
    assert result["subprocess_timeout_s"] == pytest.approx(expected)
    assert result["subprocess_timeout_s"] <= 78
    assert result["transfer_allowance_s"] == 18
    assert result["sensor_exposure_measured"] is False
    assert result["possible_dark_frame_s"] == result["configured_shutter_s"]
    assert "unverified" in result["dark_frame_state"]


@pytest.mark.parametrize("setting", [None, "Bulb", "", "unknown", "1/0", "NaN", "inf", "31", "0", "-1", "1/100000", "30", "2"])
def test_unbounded_or_invalid_shutter_never_disconnects_or_fires(tmp_path, setting):
    camera = Camera()
    with patch.object(camera, "info", return_value=CameraInfo("Sony ILCE-7M2 (Control)", "usb:000,003")), \
         patch.object(camera, "get_settings", return_value={"shutter": setting}), \
         patch.object(camera, "disconnect") as disconnect, patch.object(camera, "connect") as connect, \
         patch("sonyremote.camera.shutil.which", return_value="/fake/gphoto2"), \
         patch("sonyremote.camera.subprocess.run") as command:
        with pytest.raises(CameraError, match="no shutter was issued"):
            camera._capture_sony_action(dest_dir=tmp_path)
    command.assert_not_called()
    disconnect.assert_not_called()
    connect.assert_not_called()


@pytest.mark.parametrize("shutter,deadline", [("1",20.0), ("1/60",18+2/60)])
def test_action_uses_readback_deadline_and_records_configuration_not_physical_duration(tmp_path, shutter, deadline):
    camera = Camera()
    events = []
    def settings():
        events.append("readback")
        return {"shutter": shutter}
    def command(*args, **kwargs):
        events.append("trigger")
        assert kwargs["timeout"] == pytest.approx(deadline)
        (kwargs["cwd"] / "DSC00042.JPG").write_bytes(b"fixture")
        return SimpleNamespace(returncode=0, stderr="", stdout="captured")
    with patch.object(camera, "info", return_value=CameraInfo("Sony ILCE-7M2 (Control)", "usb:000,003")), \
         patch.object(camera, "get_settings", side_effect=settings), \
         patch.object(camera, "disconnect", side_effect=lambda: events.append("disconnect")), \
         patch.object(camera, "connect"), patch("sonyremote.camera.time.sleep"), \
         patch("sonyremote.camera.shutil.which", return_value="/fake/gphoto2"), \
         patch("sonyremote.camera.subprocess.run", side_effect=command) as run:
        assert camera._capture_sony_action(dest_dir=tmp_path).is_file()
    assert events == ["readback", "disconnect", "trigger"]
    run.assert_called_once()
    receipt = camera.last_capture_receipt
    assert receipt["capture_deadline"]["confirmed_shutter_setting"] == shutter
    assert receipt["timings"]["capture_timeout_s"] == pytest.approx(deadline)
    assert receipt["timings"]["sensor_exposure_s"] is None


def test_twenty_second_window_cannot_fit_two_thirty_second_exposures():
    with pytest.raises(ValueError, match="at least 68.000s"):
        plan_long_exposure(20,30,frame_budget=2)
    feasible = plan_long_exposure(68,30,frame_budget=2)
    assert feasible["frames"] == 2
    assert feasible["interval_s"] == 38
    assert feasible["planned_integrated_shutter_s"] == 60
    assert feasible["usb_transfer_allowance_s"] == 8
