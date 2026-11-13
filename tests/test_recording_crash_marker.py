"""No camera calls: native recording intent must survive process death before stop."""
import json
from pathlib import Path
import subprocess
import sys
import tempfile
from unittest.mock import patch

import pytest

import sonyremote.camera as module


@pytest.mark.parametrize("method", ["start_movie", "record_native"])
def test_fresh_requested_a7iii_is_rejected_before_marker_connection_or_toggle(method):
    with tempfile.TemporaryDirectory() as temporary, \
         patch.object(module, "RECORDING_QUARANTINE_ROOT", Path(temporary) / "recording"), \
         patch.object(module, "_recording_quarantined_process", False):
        camera = module.Camera(model="Sony Alpha-A7 III (PC Control)", allow_unverified_identity=True)
        with patch.object(camera, "connect") as connect, patch.object(camera, "_press_movie_button") as toggle:
            with pytest.raises(NotImplementedError, match="start/stop verification"):
                getattr(camera, method)()
            connect.assert_not_called()
            toggle.assert_not_called()
        assert not module.RECORDING_QUARANTINE_ROOT.exists()
        assert module.recording_quarantine_active() is False


def test_unknown_unconnected_model_is_rejected_before_marker_or_toggle():
    with tempfile.TemporaryDirectory() as temporary, \
         patch.object(module, "RECORDING_QUARANTINE_ROOT", Path(temporary) / "recording"), \
         patch.object(module, "_recording_quarantined_process", False), \
         patch.dict("os.environ", {}, clear=True):
        camera = module.Camera(allow_unverified_identity=True)
        with patch.object(camera, "_press_movie_button") as toggle:
            with pytest.raises(module.CameraError, match="Not connected"):
                camera.start_movie()
            toggle.assert_not_called()
        assert not module.RECORDING_QUARANTINE_ROOT.exists()


def test_marker_is_durable_before_first_toggle_and_owned_stop_clears_it():
    with tempfile.TemporaryDirectory() as temporary, \
         patch.object(module, "RECORDING_QUARANTINE_ROOT", Path(temporary) / "recording"), \
         patch.object(module, "_recording_quarantined_process", False):
        camera = module.Camera(model="Sony ILCE-7M2 (Control)", allow_unverified_identity=True)
        sentinel = module.RECORDING_QUARANTINE_ROOT / module.RECORDING_QUARANTINE_NAME
        actions = []

        def toggle():
            assert json.loads(sentinel.read_text())["state"] == "recording_stop_uncertain"
            actions.append("toggle")

        camera._press_movie_button = toggle
        camera.start_movie()
        fresh = module.Camera(model="Sony ILCE-7M2 (Control)", allow_unverified_identity=True)
        fresh._press_movie_button = lambda: pytest.fail("fresh instance sent a toggle")
        with pytest.raises(module.RecordingStopUncertain):
            fresh.start_movie()
        with pytest.raises(module.RecordingStopUncertain):
            fresh.stop_movie()
        with pytest.raises(module.RecordingStopUncertain):
            camera.start_movie()
        camera.stop_movie()
        assert actions == ["toggle", "toggle"]
        assert not sentinel.exists()
        assert module.recording_quarantine_active() is False


@pytest.mark.parametrize("fail_at", [1, 2, 3])
def test_marker_persistence_failure_stops_before_first_toggle(fail_at):
    real_fsync = module.os.fsync
    calls = []

    def fsync(fd):
        calls.append(fd)
        if len(calls) == fail_at:
            raise OSError("storage unavailable")
        return real_fsync(fd)

    with tempfile.TemporaryDirectory() as temporary, \
         patch.object(module, "RECORDING_QUARANTINE_ROOT", Path(temporary) / "recording"), \
         patch.object(module, "_recording_quarantined_process", False), \
         patch.object(module.os, "fsync", side_effect=fsync):
        camera = module.Camera(model="Sony ILCE-7M2 (Control)", allow_unverified_identity=True)
        with patch.object(camera, "_press_movie_button") as toggle:
            with pytest.raises(module.RecordingStopUncertain):
                camera.record_native(1)
            toggle.assert_not_called()


def test_replaced_operation_in_same_marker_file_cannot_be_stopped_or_cleared():
    with tempfile.TemporaryDirectory() as temporary, \
         patch.object(module, "RECORDING_QUARANTINE_ROOT", Path(temporary) / "recording"), \
         patch.object(module, "_recording_quarantined_process", False):
        camera = module.Camera(model="Sony ILCE-7M2 (Control)", allow_unverified_identity=True)
        with patch.object(camera, "_press_movie_button"):
            camera.start_movie()
        sentinel = module.RECORDING_QUARANTINE_ROOT / module.RECORDING_QUARANTINE_NAME
        payload = json.loads(sentinel.read_text())
        payload["operation_id"] = "another-operation"
        sentinel.write_text(json.dumps(payload))
        with patch.object(camera, "_press_movie_button") as toggle:
            with pytest.raises(module.RecordingStopUncertain):
                camera.stop_movie()
            toggle.assert_not_called()
        assert sentinel.exists()


def test_killed_recording_process_leaves_fresh_process_blocked():
    killed_script = '''
import os, signal, sys
from pathlib import Path
import sonyremote.camera as c
c.RECORDING_QUARANTINE_ROOT = Path(sys.argv[1])
cam = c.Camera(model="Sony ILCE-7M2 (Control)", allow_unverified_identity=True)
def first_toggle():
    assert (c.RECORDING_QUARANTINE_ROOT / c.RECORDING_QUARANTINE_NAME).is_file()
    os.kill(os.getpid(), signal.SIGKILL)
cam._press_movie_button = first_toggle
cam.record_native(30)
raise AssertionError("unexpected return")
'''
    fresh_script = '''
import sys
from pathlib import Path
import sonyremote.camera as c
c.RECORDING_QUARANTINE_ROOT = Path(sys.argv[1])
cam = c.Camera(model="Sony ILCE-7M2 (Control)", allow_unverified_identity=True)
cam._press_movie_button = lambda: (_ for _ in ()).throw(AssertionError("unexpected camera action"))
try:
    cam.start_movie()
except c.RecordingStopUncertain:
    print("fresh process blocked by durable marker")
else:
    raise AssertionError("fresh process bypassed marker")
'''
    with tempfile.TemporaryDirectory() as temporary:
        root = Path(temporary) / "recording"
        cwd = Path(__file__).resolve().parents[1]
        killed = subprocess.run([sys.executable, "-c", killed_script, str(root)], cwd=cwd,
                                capture_output=True, text=True, timeout=10)
        assert killed.returncode < 0, killed.stderr
        sentinel = root / module.RECORDING_QUARANTINE_NAME
        assert json.loads(sentinel.read_text())["state"] == "recording_stop_uncertain"
        fresh = subprocess.run([sys.executable, "-c", fresh_script, str(root)], cwd=cwd,
                               capture_output=True, text=True, timeout=10)
        assert fresh.returncode == 0, fresh.stderr
        assert "fresh process blocked" in fresh.stdout
        assert sentinel.exists()


def test_failed_stop_retains_marker_and_cannot_toggle_again():
    with tempfile.TemporaryDirectory() as temporary, \
         patch.object(module, "RECORDING_QUARANTINE_ROOT", Path(temporary) / "recording"), \
         patch.object(module, "_recording_quarantined_process", False):
        camera = module.Camera(model="Sony ILCE-7M2 (Control)", allow_unverified_identity=True)
        with patch.object(camera, "_press_movie_button"):
            camera.start_movie()
        with patch.object(camera, "_press_movie_button", side_effect=module.CameraError("stop failed")) as toggle:
            with pytest.raises(module.RecordingStopUncertain):
                camera.stop_movie()
            with pytest.raises(module.RecordingStopUncertain):
                camera.stop_movie()
            assert toggle.call_count == 1
        assert module.recording_quarantine_active() is True
