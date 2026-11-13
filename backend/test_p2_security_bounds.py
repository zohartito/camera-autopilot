"""Offline regression checks for P2 resource and input-boundary controls."""
from __future__ import annotations

import io
import os
import sys
import tempfile
from pathlib import Path

from PIL import Image

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT.parent / "tools"))

import image_safety  # noqa: E402
import security_bounds  # noqa: E402
import settings_probe_plan  # noqa: E402
import usb_ptp_bridge_server  # noqa: E402
import feedback  # noqa: E402


class _Response(io.BytesIO):
    def __init__(self, data: bytes, length: str | None = None):
        super().__init__(data)
        self.headers = {} if length is None else {"Content-Length": length}


def test_bounded_response_rejects_declared_and_actual_overflow() -> None:
    for response in (
        _Response(b"{}", str(security_bounds.MAX_HTTP_RESPONSE_BYTES + 1)),
        _Response(b"x" * 9),
    ):
        try:
            security_bounds.read_bounded(response, limit=8)
        except ValueError:
            pass
        else:
            raise AssertionError("oversized response was accepted")
    assert security_bounds.read_bounded(_Response(b"{}"), limit=8) == b"{}"


def test_predecode_image_budget_rejects_oversized_headers() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "oversized.png"
        Image.new("1", (8000, 6000)).save(path)
        try:
            image_safety.image_size(path)
        except ValueError as exc:
            assert "budget" in str(exc)
        else:
            raise AssertionError("oversized image was decoded")


def test_image_ingress_inventory_is_guarded_or_explicitly_tombstoned() -> None:
    """Keep every remaining image ingress on a bounded pre-decode path."""
    sources = {
        "embed_index.py": ("MAX_IMAGE_SOURCE_BYTES", "open_rgb_from_bytes(data)"),
        "wifi_pano.py": ("image_size_from_bytes(snapshot)", "source_bytes(path)"),
        "wifi_a7ii.py": ("image_size_from_bytes(buf)", "MAX_LIVEVIEW_FRAME_BYTES"),
        "stacking.py": ("image_size_from_bytes(snapshot)", "RAW stacking is disabled"),
        "wifi_demo.py": ("image_size(\"/tmp/demo_lv.jpg\")",),
        "camera.py": ("USB live view is disabled", "record_clip is disabled"),
    }
    for filename, required in sources.items():
        parent = ROOT.parent / ("sonyremote" if filename == "camera.py" else "backend")
        text = (parent / filename).read_text(encoding="utf-8")
        for marker in required:
            assert marker in text, (filename, marker)
    assert "RAW stacking is disabled" in (ROOT / "stacking.py").read_text(encoding="utf-8")
    stack_source = (ROOT / "wifi_stack.py").read_text(encoding="utf-8")
    assert "transfer_capture_jpegs" in stack_source
    assert "frame_receipt" in stack_source
    assert "transfer_latest_full_res" not in stack_source
    assert "RAW stacking is disabled" in stack_source
    isolated_raw_source = (ROOT / "isolated_raw.py").read_text(encoding="utf-8")
    for marker in ("resource.RLIMIT_AS", "start_new_session=True", "signal.SIGKILL",
                   "inspect_arw_header", "RAW requires hard address-space enforcement",
                   "render_arw_linear_isolated", "colour_missing", "use_auto_wb=False",
                   "user_wb=[1.0, 1.0, 1.0, 1.0]"):
        assert marker in isolated_raw_source, marker
    assert "Image.open(" not in isolated_raw_source
    assert "cv2.imread(" not in isolated_raw_source
    for extra in ("raw_colour.py", "develop_compare.py"):
        extra_source = (ROOT / extra).read_text(encoding="utf-8")
        assert "Image.open(" not in extra_source
        assert "cv2.imread(" not in extra_source
    for source in ROOT.glob("*.py"):
        if source.name.startswith("test_") or source.name == "image_safety.py":
            continue
        text = source.read_text(encoding="utf-8")
        # These reviewed disposable workers own their bounded native decoders.
        # Runtime regressions exercise dimension refusal, deadline/cancel, and
        # partial-output cleanup; they never decode in an HTTP handler.
        if source.name == 'development_worker.py':
            for marker in ('resource.RLIMIT_CPU', 'resource.RLIMIT_FSIZE',
                           "opened.width * opened.height > request['max_pixels']",
                           "range(0, height, 128)", "RAW requires hard address-space enforcement"):
                assert marker in text
            continue
        if source.name == 'panorama.py':
            for marker in ('def _worker(job)', 'subprocess.Popen', 'MAX_RSS_BYTES',
                           'width * height > MAX_SOURCE_PIXELS', 'MAX_OUTPUT_PIXELS',
                           'signal.SIGKILL'):
                assert marker in text
            continue
        assert "Image.open(" not in text, source
        assert "cv2.imread(" not in text, source


def test_promoted_method_is_exact_allowlist_and_command_is_argv_safe() -> None:
    assert settings_probe_plan.approved_smart_remote_method("camera.iso", "setIsoSpeedRate")
    assert settings_probe_plan.approved_smart_remote_method("camera.iso", 'setIsoSpeedRate";x') is None
    row = {"setting_id": "camera.iso", "routes": {"smart_remote_wifi": {"method": 'setIsoSpeedRate";x'}}}
    command = settings_probe_plan._smart_remote_behavior_command(row)
    assert "setIsoSpeedRate" in command
    assert '";x' not in command


def test_usb_bridge_rejects_oversized_body_and_unauthenticated_mode() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        state = usb_ptp_bridge_server.BridgeState(Path(tmp), "python3", api_token="a" * 24)
        assert state.admit_capture() is True
        assert state.admit_capture() is False
        try:
            usb_ptp_bridge_server.BridgeState(
                Path(tmp), "python3", api_token="a" * 24, allow_unauthenticated_lab=True
            )
        except ValueError:
            pass
        else:
            raise AssertionError("unauthenticated physical bridge was accepted")


def test_feedback_write_rate_is_bounded() -> None:
    previous = feedback._recent_writes.copy()
    try:
        feedback._recent_writes.clear()
        feedback._recent_writes.extend([0.0] * feedback.MAX_FEEDBACK_WRITES_PER_MINUTE)
        # Replace the monotonic window with current timestamps, then verify no append is attempted.
        now = __import__("time").monotonic()
        feedback._recent_writes.clear()
        feedback._recent_writes.extend([now] * feedback.MAX_FEEDBACK_WRITES_PER_MINUTE)
        with tempfile.TemporaryDirectory() as tmp:
            old_file = feedback.FEEDBACK_FILE
            feedback.FEEDBACK_FILE = Path(tmp) / "feedback.jsonl"
            try:
                feedback.log_feedback(None, "up", "shot.jpg")
            except ValueError as exc:
                assert "rate limit" in str(exc)
            else:
                raise AssertionError("feedback rate limit was not enforced")
            finally:
                feedback.FEEDBACK_FILE = old_file
    finally:
        feedback._recent_writes.clear()
        feedback._recent_writes.extend(previous)


def test_feedback_rejects_links_and_special_files_without_following_or_reading() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        target = root / "target.jsonl"
        target.write_text('{"image":"target.jpg","verdict":"up"}\n', encoding="utf-8")
        link = root / "feedback.jsonl"
        link.symlink_to(target)
        try:
            feedback._validated_rows(link)
        except ValueError as exc:
            assert "regular file" in str(exc)
        else:
            raise AssertionError("feedback symlink was followed")
        fifo = root / "feedback.fifo"
        os.mkfifo(fifo)
        try:
            feedback._validated_rows(fifo)
        except ValueError as exc:
            assert "regular file" in str(exc)
        else:
            raise AssertionError("feedback FIFO was opened")


def test_feedback_reads_a_single_bounded_snapshot() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "feedback.jsonl"
        path.write_text('{"image":"shot.jpg","verdict":"up"}\n', encoding="utf-8")
        rows = feedback._validated_rows(path)
        assert rows == [b'{"image":"shot.jpg","verdict":"up"}']


def test_fastapi_body_limit_middleware_returns_direct_responses() -> None:
    source = (ROOT / "app.py").read_text(encoding="utf-8")
    assert 'JSONResponse(status_code=411' in source
    assert 'JSONResponse(status_code=413' in source
    assert 'raise HTTPException(status_code=413' not in source


def test_root_server_import_and_harvesters_keep_bounded_guards() -> None:
    server_source = (ROOT.parent / "server.py").read_text(encoding="utf-8")
    assert server_source.index("_BACKEND =") < server_source.index("from image_safety import open_rgb")
    assert "python3 -c 'import server; print(\"server import: ok\")'" in (
        ROOT.parent / "README.md"
    ).read_text(encoding="utf-8")
    for filename in ("harvest_flickr.py", "harvest_wikimedia.py"):
        source = (ROOT / filename).read_text(encoding="utf-8")
        assert "from security_bounds import json_response" in source
        assert "return json_response(resp)" in source
    assert "CAMERA_AUTOPILOT_ALLOW_UNAUTH_LOCAL" not in (ROOT / "app.py").read_text(encoding="utf-8")
    app_source = (ROOT / "app.py").read_text(encoding="utf-8")
    assert 'request.url.path == "/status" or request.url.path.startswith("/api/camera/")' in app_source
    assert "async def enforce_camera_api_boundaries" in app_source
    simulator_smoke = (ROOT.parent / "mobile/scripts/run_ios_simulator_smoke.sh").read_text(encoding="utf-8")
    assert 'X-Camera-Autopilot-Token: ios-simulator-smoke-token' in simulator_smoke


if __name__ == "__main__":
    test_bounded_response_rejects_declared_and_actual_overflow()
    test_predecode_image_budget_rejects_oversized_headers()
    test_image_ingress_inventory_is_guarded_or_explicitly_tombstoned()
    test_promoted_method_is_exact_allowlist_and_command_is_argv_safe()
    test_usb_bridge_rejects_oversized_body_and_unauthenticated_mode()
    test_feedback_write_rate_is_bounded()
    test_feedback_rejects_links_and_special_files_without_following_or_reading()
    test_feedback_reads_a_single_bounded_snapshot()
    test_fastapi_body_limit_middleware_returns_direct_responses()
    test_root_server_import_and_harvesters_keep_bounded_guards()
    print("P2 security bounds tests: PASS")
