"""Camera-free bounded process, malformed-image, and zero-hidden-shutter regressions."""
import io
import json
import os
import signal
import subprocess
import sys
import time
from unittest.mock import Mock

import pytest
from PIL import Image
from sonyremote import isolated_preview as probe

MODEL = "Sony ILCE-7M2 (Control)"
PORT = "usb:000,003"


def jpeg():
    stream = io.BytesIO()
    Image.new("RGB", (20, 10), "red").save(stream, "JPEG")
    return stream.getvalue()


def wire(data=b"", **overrides):
    meta = {"ok": True, "model": MODEL, "port": PORT, "mode": "preview", **overrides}
    return json.dumps(meta).encode() + b"\n" + data


@pytest.fixture(autouse=True)
def no_quarantine(monkeypatch):
    monkeypatch.setattr("sonyremote.camera.assert_recording_allowed", lambda: None)


@pytest.mark.parametrize("model,port", [(None, PORT), (MODEL, None), (MODEL, "usb"), ("other", PORT), (MODEL, "usb:001,003\n")])
def test_exact_identity_before_spawn(monkeypatch, model, port):
    spawn = Mock()
    monkeypatch.setattr(probe, "_collect_worker", spawn)
    with pytest.raises(probe.IsolatedProbeError, match="exact supported"):
        probe.capture_preview_isolated(model, port)
    spawn.assert_not_called()


@pytest.mark.parametrize("timeout", [0, 31, float("nan"), True])
def test_deadline_is_bounded(timeout):
    with pytest.raises(probe.IsolatedProbeError):
        probe.capture_preview_isolated(MODEL, PORT, timeout_seconds=timeout)


def test_success_validates_jpeg_and_identity(monkeypatch):
    monkeypatch.setattr(probe, "_collect_worker", lambda *a, **k: wire(jpeg()))
    frame = probe.capture_preview_isolated(MODEL, PORT)
    assert (frame.width, frame.height) == (20, 10)
    assert len(frame.sha256) == 64


@pytest.mark.parametrize("raw,code", [
    (b"no header", "invalid_protocol"),
    (b"[]\n", "invalid_protocol"),
    (wire(jpeg(), port="usb:001,002"), "identity_mismatch"),
    (wire(b"not JPEG"), "invalid_image"),
    (wire(jpeg()[:100]), "invalid_image"),
    (wire(jpeg(), ok=False, code="unsupported_memory_limit", error="not opened"), "unsupported_memory_limit"),
])
def test_bad_worker_output(monkeypatch, raw, code):
    monkeypatch.setattr(probe, "_collect_worker", lambda *a, **k: raw)
    with pytest.raises(probe.IsolatedProbeError) as error:
        probe.capture_preview_isolated(MODEL, PORT)
    assert error.value.code == code


def test_oversized_image_dimensions_rejected():
    stream = io.BytesIO()
    Image.new("L", (4000, 3001)).save(stream, "JPEG")
    with pytest.raises(probe.IsolatedProbeError):
        probe._jpeg_dimensions(stream.getvalue(), probe.MAX_BYTES)


def test_child_timeout_kills_and_reaps(tmp_path):
    pidfile = tmp_path / "pid"
    command = [sys.executable, "-c", "import os,time,pathlib; pathlib.Path(" + repr(str(pidfile)) + ").write_text(str(os.getpid())); time.sleep(30)"]
    start = time.monotonic()
    with pytest.raises(probe.IsolatedProbeError) as error:
        probe._collect_worker(command, timeout_seconds=0.3, max_output=100)
    assert error.value.code == "timeout"
    assert time.monotonic() - start < 2
    with pytest.raises(ProcessLookupError):
        os.kill(int(pidfile.read_text()), 0)


def test_child_output_flood_is_bounded():
    command = [sys.executable, "-c", "import os; os.write(1,b'x'*1000000)"]
    with pytest.raises(probe.IsolatedProbeError) as error:
        probe._collect_worker(command, timeout_seconds=2, max_output=1024)
    assert error.value.code == "byte_limit"


def test_worker_crash_does_not_return_photo():
    with pytest.raises(probe.IsolatedProbeError) as error:
        probe._collect_worker([sys.executable, "-c", "raise SystemExit(42)"], timeout_seconds=2, max_output=1024)
    assert error.value.code == "worker_failed"


def test_native_preview_calls_only_preview(monkeypatch):
    camera = Mock()
    camera.__enter__ = Mock(return_value=camera)
    camera.__exit__ = Mock(return_value=False)
    native = Mock()
    native.capture_preview.return_value.get_data_and_size.return_value = jpeg()
    camera._require.return_value = native
    monkeypatch.setattr("sonyremote.camera.Camera", Mock(return_value=camera))
    meta, data = probe._native_probe("preview", MODEL, PORT, probe.MAX_BYTES)
    assert data == jpeg() and meta["still_exposures"] == 0
    assert native.method_calls == [(("capture_preview"), (), {})]
    camera.capture.assert_not_called()
    camera.set_setting.assert_not_called()
    camera.wake.assert_not_called()


def test_focus_descriptors_do_not_claim_motor_or_write(monkeypatch):
    camera = Mock()
    camera.__enter__ = Mock(return_value=camera)
    camera.__exit__ = Mock(return_value=False)
    camera.describe_setting.return_value = {"readonly": False, "value": 0}
    monkeypatch.setattr("sonyremote.camera.Camera", Mock(return_value=camera))
    meta, data = probe._native_probe("focus", MODEL, PORT, probe.MAX_BYTES)
    assert meta["manual_focus_motor_verified"] is False and not data
    assert camera.describe_setting.call_count == len(probe.FOCUS_NODES)
    camera.set_setting.assert_not_called()
    camera.capture.assert_not_called()


def test_quarantine_checked_before_spawn(monkeypatch):
    def blocked():
        raise RuntimeError("recording quarantined")
    monkeypatch.setattr("sonyremote.camera.assert_recording_allowed", blocked)
    spawn = Mock()
    monkeypatch.setattr(probe, "_collect_worker", spawn)
    with pytest.raises(RuntimeError, match="quarantined"):
        probe.capture_preview_isolated(MODEL, PORT)
    spawn.assert_not_called()


def test_unavailable_address_limit_fails_before_native(monkeypatch):
    native = Mock()
    monkeypatch.setattr(probe, "_native_probe", native)
    def denied(*args, **kwargs):
        raise ValueError("current limit exceeds maximum limit")
    monkeypatch.setattr("resource.setrlimit", denied)
    with pytest.raises(probe.IsolatedProbeError) as error:
        probe._set_worker_limits()
    assert error.value.code == "unsupported_memory_limit"
    native.assert_not_called()
