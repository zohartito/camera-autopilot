"""Camera-free tests for the USB/PTP HTTP bridge proof server."""

from __future__ import annotations

import json
import sys
import tempfile
import types
from http import HTTPStatus
from io import BytesIO
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "tools"))

import usb_ptp_bridge_server


def _fake_handler(state: usb_ptp_bridge_server.BridgeState):
    handler = object.__new__(usb_ptp_bridge_server.Handler)
    handler.server = types.SimpleNamespace(state=state)
    responses = []

    def fake_json(status, payload):
        responses.append((status.value, json.loads(json.dumps(payload))))

    handler._json = fake_json
    return handler, responses


def _header_handler(state: usb_ptp_bridge_server.BridgeState):
    handler = object.__new__(usb_ptp_bridge_server.Handler)
    handler.server = types.SimpleNamespace(state=state)
    handler.headers = {}
    handler.wfile = BytesIO()
    responses = []
    headers = []
    ended = []

    def fake_send_response(status):
        responses.append(status)

    def fake_send_header(name, value):
        headers.append((name, value))

    def fake_end_headers():
        ended.append(True)

    handler.send_response = fake_send_response
    handler.send_header = fake_send_header
    handler.end_headers = fake_end_headers
    return handler, responses, headers, ended


def test_usb_bridge_allows_browser_phone_lan_clients() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        state = usb_ptp_bridge_server.BridgeState(
            Path(tmp),
            "python3",
            cors_origin="http://phone-client.local",
        )
        handler, responses, headers, ended = _header_handler(state)
        handler.headers = {
            "Origin": "http://phone-client.local",
            "Access-Control-Request-Method": "POST",
            "Access-Control-Request-Headers": "Authorization, Content-Type",
        }

        handler.do_OPTIONS()
        assert responses == [HTTPStatus.NO_CONTENT.value]
        assert ended == [True]
        assert ("Access-Control-Allow-Origin", "http://phone-client.local") in headers
        assert ("Access-Control-Allow-Methods", "GET, POST") in headers
        assert (
            "Access-Control-Allow-Headers",
            "Authorization, Content-Type, X-API-Key",
        ) in headers
        assert ("Access-Control-Expose-Headers", "X-Artifact-Dir") in headers

        handler, responses, headers, ended = _header_handler(state)
        handler.headers = {"Origin": "http://phone-client.local"}
        handler._json(HTTPStatus.OK, {"ok": True})
        assert responses == [HTTPStatus.OK.value]
        assert ended == [True]
        assert ("Access-Control-Allow-Origin", "http://phone-client.local") in headers
        assert json.loads(handler.wfile.getvalue().decode("utf-8")) == {"ok": True}
    print("  USB bridge browser/phone LAN CORS: ok")


def test_usb_bridge_serves_phone_control_page() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        state = usb_ptp_bridge_server.BridgeState(
            Path(tmp),
            "python3",
            cors_origin="http://phone-client.local",
        )
        handler, responses, headers, ended = _header_handler(state)
        handler.headers = {"Origin": "http://phone-client.local"}

        handler._handle_index()

        assert responses == [HTTPStatus.OK.value]
        assert ended == [True]
        assert ("Content-Type", "text/html; charset=utf-8") in headers
        assert ("Access-Control-Allow-Origin", "http://phone-client.local") in headers
        html = handler.wfile.getvalue().decode("utf-8")
        assert "A7 II USB Bridge" in html
        assert "USB bridge preview is unavailable" in html
        assert "/preview.jpg" not in html
        assert 'data-action="preview"' not in html
        assert "/ready" in html
        assert "/focus/af-point" in html
        assert "/capture" not in html
        assert "dry_run" in html
        assert "Bridge API token" in html
        assert "Authorization" in html
    print("  USB bridge phone control page: ok")


def test_usb_bridge_settings_routes_are_registry_backed_and_guarded() -> None:
    calls: list[list[str]] = []

    def fake_run(cmd: list[str], *, timeout: float = 30.0) -> dict[str, Any]:
        calls.append(cmd)
        stdout = ""
        if cmd[:2] == ["gphoto2", "--summary"]:
            stdout = "Camera summary\nModel: Sony ILCE-7M2\n"
        elif cmd[:3] == ["gphoto2", "--get-config", "focusmode"]:
            stdout = "Label: Focus Mode\nCurrent: Automatic\n"
        elif cmd[:3] == ["gphoto2", "--get-config", "iso"]:
            stdout = "Label: ISO\nCurrent: 800\nChoice: 0 100\nChoice: 1 800\n"
        elif cmd[:3] == ["gphoto2", "--get-config", "imagesize"]:
            stdout = "Label: Image Size\nCurrent: L:24M\nChoice: 0 L:24M\nChoice: 1 M:10M\n"
        elif cmd[:3] == ["gphoto2", "--get-config", "exposuremetermode"]:
            stdout = "Label: Metering Mode\nCurrent: Multi\nChoice: 0 Multi\nChoice: 1 Center Weighted\n"
        elif cmd[:2] == ["gphoto2", "--set-config"]:
            stdout = "OK\n"
        return {
            "cmd": cmd,
            "returncode": 0,
            "timed_out": False,
            "stdout": stdout,
            "stderr": "",
            "elapsed_s": 0.01,
        }

    original_run = usb_ptp_bridge_server._run
    usb_ptp_bridge_server._run = fake_run
    try:
        with tempfile.TemporaryDirectory() as tmp:
            state = usb_ptp_bridge_server.BridgeState(Path(tmp), "python3")
            handler, responses = _fake_handler(state)

            handler._handle_settings()
            status, settings = responses.pop()
            assert status == 200
            assert settings["registry_summary"]["total_settings"] >= 150
            iso_summary = next(item for item in settings["usb_settings"] if item["setting_id"] == "camera.iso")
            assert iso_summary["product_write_ready"] is True
            assert iso_summary["requires_behavior_proof"] is False
            image_summary = next(
                item for item in settings["usb_settings"] if item["setting_id"] == "camera.image_size"
            )
            assert image_summary["product_write_ready"] is True
            assert image_summary["requires_behavior_proof"] is False
            metering_summary = next(
                item for item in settings["usb_settings"] if item["setting_id"] == "camera.metering_mode"
            )
            assert metering_summary["product_write_ready"] is True
            assert metering_summary["requires_behavior_proof"] is False

            handler._handle_setting_read("camera.iso")
            status, read = responses.pop()
            assert status == 200
            assert read["current"]["value"] == "800"
            assert read["setting"]["node"] == "iso"

            handler._handle_setting_write("camera.iso", {"value": "100", "dry_run": True})
            status, dry = responses.pop()
            assert status == 200
            assert dry["dry_run"] is True
            assert dry["control_plan"]["route"] == "usb_gphoto"
            assert dry["control_plan"]["live_backend"] == "usb_gphoto"
            assert dry["control_plan"]["behavior_proven"] is True
            assert dry["control_plan"]["requires_behavior_proof"] is False
            assert dry["applied"] is None

            handler._handle_setting_write("camera.image_size", {"value": "M:10M", "dry_run": True})
            status, image_dry = responses.pop()
            assert status == 200
            assert image_dry["control_plan"]["requires_behavior_proof"] is False

            handler._handle_setting_write("camera.image_size", {"value": "M:10M", "dry_run": False})
            status, image_applied = responses.pop()
            assert status == 200
            assert image_applied["ok"] is True
            assert image_applied["control_plan"]["requires_behavior_proof"] is False
            assert image_applied["applied"]["node"] == "imagesize"

            handler._handle_setting_write(
                "camera.metering_mode",
                {"value": "Center Weighted", "dry_run": False},
            )
            status, metering_probe = responses.pop()
            assert status == 200
            assert metering_probe["allow_behavior_probe"] is False
            assert metering_probe["control_plan"]["requires_behavior_proof"] is False
            assert metering_probe["control_plan"]["allow_behavior_probe"] is False
            assert metering_probe["applied"]["node"] == "exposuremetermode"
            assert ["gphoto2", "--set-config", "imagesize=M:10M"] in calls
            assert ["gphoto2", "--set-config", "exposuremetermode=Center Weighted"] in calls

            handler._handle_setting_write("camera.iso", {"value": "100", "dry_run": False})
            status, applied = responses.pop()
            assert status == 200
            assert applied["applied"]["node"] == "iso"
            assert applied["applied"]["readback"]["value"] == "800"
            assert ["gphoto2", "--set-config", "iso=100"] in calls

            handler._handle_setting_write("setup.format", {"value": "confirm", "dry_run": False})
            status, guarded = responses.pop()
            assert status == 403
            assert guarded["ok"] is False
            assert "guarded" in guarded["error"]
    finally:
        usb_ptp_bridge_server._run = original_run
    print("  USB bridge registry-backed settings routes: ok")


def test_usb_bridge_capture_is_tombstoned_before_process_or_artifact_work() -> None:
    calls: list[list[str]] = []

    def fake_run(cmd: list[str], *, timeout: float = 30.0) -> dict[str, Any]:
        calls.append(cmd)
        return {
            "cmd": cmd,
            "returncode": 0,
            "timed_out": False,
            "stdout": "New file is in location /capt_DSC00001.JPG on the camera\n",
            "stderr": "",
            "elapsed_s": 0.01,
        }

    original_run = usb_ptp_bridge_server._run
    usb_ptp_bridge_server._run = fake_run
    try:
        with tempfile.TemporaryDirectory() as tmp:
            state = usb_ptp_bridge_server.BridgeState(Path(tmp), "python3")
            handler, responses = _fake_handler(state)

            handler._handle_capture({"download": True})
            status, payload = responses.pop()

        assert status == 410
        assert payload == {
            "ok": False,
            "error": "capture is disabled: gphoto2 %C cannot prove a JPEG-only artifact",
        }
        assert calls == []
    finally:
        usb_ptp_bridge_server._run = original_run
    print("  USB bridge capture is tombstoned before process or artifact work: ok")


if __name__ == "__main__":
    test_usb_bridge_allows_browser_phone_lan_clients()
    test_usb_bridge_serves_phone_control_page()
    test_usb_bridge_settings_routes_are_registry_backed_and_guarded()
    test_usb_bridge_capture_is_tombstoned_before_process_or_artifact_work()
    print("usb_ptp_bridge_server tests: PASS")
