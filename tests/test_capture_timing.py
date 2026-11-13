"""Transport phase timings are observed, not inferred shutter-open durations."""
import itertools
import math
from types import SimpleNamespace
from unittest.mock import MagicMock, patch
import pytest
from sonyremote.camera import Camera, CameraInfo, CameraError, CaptureOutcomeUncertain
from test_final_wave_engine import rig


def test_sony_combined_transport_timing_survives_reconnect_failure(tmp_path):
    camera = Camera(allow_unverified_identity=True)
    clock = itertools.count(10, .125)
    def capture(command, **kwargs):
        (kwargs["cwd"] / "DSC00042.JPG").write_bytes(b"fixture JPEG download")
        return SimpleNamespace(returncode=0, stderr="", stdout="captured")
    with patch.object(camera, "info", return_value=CameraInfo("Sony ILCE-7M2 (Control)", "usb:000,003")), \
         patch.object(camera, "get_settings", return_value={"shutter": "1/60"}), \
         patch.object(camera, "disconnect"), patch.object(camera, "connect", side_effect=CameraError("fixture reconnect failed")), \
         patch.object(camera, "_has_sony_action_capture", return_value=True), \
         patch("sonyremote.camera.shutil.which", return_value="/fake/gphoto2"), \
         patch("sonyremote.camera.time.sleep"), patch("sonyremote.camera.time.monotonic", side_effect=lambda: next(clock)), \
         patch("sonyremote.camera.subprocess.run", side_effect=capture) as run:
        image = camera.capture(dest_dir=tmp_path)
    assert image.is_file() and run.call_count == 1
    receipt = camera.last_capture_receipt
    assert receipt["reconnect_ok"] is False
    timings = receipt["timings"]
    assert timings["clock"] == "monotonic" and timings["sensor_exposure_s"] is None
    for key in ("total_s", "usb_release_s", "settle_s", "trigger_and_download_s", "local_copy_s", "reconnect_s"):
        assert timings[key] > 0
    assert "trigger_to_return_s" not in timings


def test_generic_capture_reports_trigger_return_and_download_separately(tmp_path):
    camera = Camera(allow_unverified_identity=True)
    camera._camera = MagicMock()
    camera._camera.capture.return_value = SimpleNamespace(folder="/DCIM", name="DSC00055.JPG")
    camera._camera.file_get.return_value.save.side_effect = lambda name: __import__("pathlib").Path(name).write_bytes(b"fixture")
    # The generic fallback now enforces the 1 s cap, so the body must report a readable shutter.
    with patch.object(camera, "_has_sony_action_capture", return_value=False), \
            patch.object(camera, "get_settings", return_value={"shutter": "1/60"}):
        camera.capture(dest_dir=tmp_path)
    timings = camera.last_capture_receipt["timings"]
    assert timings["trigger_to_return_s"] >= 0 and timings["resolve_and_download_s"] >= 0
    assert timings["sensor_exposure_s"] is None
    assert "trigger_and_download_s" not in timings
    camera._camera.capture.assert_called_once()


def test_engine_records_meter_final_analysis_and_save_phases_without_extra_exposures(rig):
    shot = rig.real.shoot("natural", variant_id="natural")
    timings = shot.evidence["timings"]
    assert timings["clock"] == "monotonic"
    for key in ("meter_capture_s", "meter_analysis_and_recommendation_s", "apply_and_readback_s",
                "final_capture_s", "final_validation_and_readback_s", "gallery_save_s", "engine_until_result_s"):
        assert math.isfinite(timings[key]) and timings[key] >= 0
    assert rig.camera.capture_count == 2
    assert "meter_transport_receipt" in shot.evidence
    assert shot.evidence["physical_exposure_count"] == 2
