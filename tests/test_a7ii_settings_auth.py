"""Auth contract for the full camera API namespace + /record engine-capability honesty.

Every /api/camera/* method—including metadata and non-live setting reads—must sit behind the
same API-token gate as /shoot: 401 on a missing/wrong token, 503 fail-closed when no token is
configured, and a valid token passes through to the normal (dry-run-guarded) handler. /record
uses the same fail-closed token and transport gate before it can inspect or call the active engine;
it must still refuse cleanly (501, not a 500 AttributeError) when that engine has no
record path — the Wi-Fi engine's case today.

Run standalone:
    python3 tests/test_a7ii_settings_auth.py
"""

from __future__ import annotations

import os
import sys
from pathlib import Path
from unittest.mock import patch

# A camera-free test must never touch real hardware, whatever the ambient env says.
os.environ["CAMERA_ENGINE"] = "mock"
os.environ["CAMERA_AUTOPILOT_API_TOKEN"] = "test-token"

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(ROOT / "backend"))

import app as backend_app
import engine as engine_module
import wifi_engine
from _runner import run_module_tests
from fastapi.testclient import TestClient

AUTH = {"X-Camera-Autopilot-Token": "test-token"}

# Minimal valid bodies, so a rejection can only come from the auth gate, never from validation.
WRITE_ENDPOINTS = [
    ("/api/camera/setting-promotions/apply", {}),
    ("/api/camera/settings-sessions/run", {"route": "usb_gphoto"}),
    ("/api/camera/settings-live-marathon/run", {}),
    ("/api/camera/settings/camera.iso", {"value": "800"}),
]
CAMERA_METADATA_ENDPOINTS = [
    "/api/camera/settings",
    "/api/camera/setting-probes",
    "/api/camera/settings-gates",
    "/api/camera/settings/camera.iso",
]


def test_status_uses_the_camera_token_gate() -> None:
    client = TestClient(backend_app.app)
    missing = client.get("/status")
    wrong = client.get("/status", headers={"X-Camera-Autopilot-Token": "wrong-token"})
    valid = client.get("/status", headers=AUTH)
    assert missing.status_code == 401, missing.text
    assert wrong.status_code == 401, wrong.text
    assert valid.status_code == 200, valid.text


def test_status_fails_closed_without_configured_token() -> None:
    client = TestClient(backend_app.app)
    original_token = backend_app.API_TOKEN
    backend_app.API_TOKEN = ""
    try:
        res = client.get("/status")
        assert res.status_code == 503, res.text
    finally:
        backend_app.API_TOKEN = original_token


def test_write_endpoints_reject_missing_token() -> None:
    client = TestClient(backend_app.app)
    for path, body in WRITE_ENDPOINTS:
        res = client.post(path, json=body)
        assert res.status_code == 401, f"{path}: expected 401 without token, got {res.status_code}"


def test_write_endpoints_reject_wrong_token() -> None:
    client = TestClient(backend_app.app)
    for path, body in WRITE_ENDPOINTS:
        res = client.post(path, json=body, headers={"X-Camera-Autopilot-Token": "wrong-token"})
        assert res.status_code == 401, f"{path}: expected 401 with wrong token, got {res.status_code}"


def test_write_endpoints_fail_closed_without_configured_token() -> None:
    """Same contract as /shoot: no configured token means the camera write API is off (503)."""
    client = TestClient(backend_app.app)
    original_token = backend_app.API_TOKEN
    backend_app.API_TOKEN = ""
    try:
        for path, body in WRITE_ENDPOINTS:
            res = client.post(path, json=body)
            assert res.status_code == 503, f"{path}: expected 503 fail-closed, got {res.status_code}"
    finally:
        backend_app.API_TOKEN = original_token


def test_valid_token_admits_dry_run_write() -> None:
    """The gate must let a valid token through to the handler; the default plan writes nothing."""
    client = TestClient(backend_app.app)
    res = client.post("/api/camera/setting-promotions/apply", json={}, headers=AUTH)
    assert res.status_code == 200, f"expected 200 with valid token, got {res.status_code}: {res.text}"
    summary = res.json()["summary"]
    assert summary["dry_run"] is True
    assert summary["written"] is False


def test_camera_metadata_rejects_missing_token_before_any_read() -> None:
    client = TestClient(backend_app.app)
    with patch.object(
        backend_app.settings_control,
        "read_setting",
        return_value={"current": "800", "route": "registry"},
    ) as read_setting:
        for path in CAMERA_METADATA_ENDPOINTS:
            res = client.get(path)
            assert res.status_code == 401, f"{path}: expected 401, got {res.status_code}: {res.text}"

    read_setting.assert_not_called()


def test_static_setting_metadata_accepts_valid_token() -> None:
    client = TestClient(backend_app.app)
    with patch.object(
        backend_app.settings_control,
        "read_setting",
        return_value={"current": "800", "route": "registry"},
    ) as read_setting:
        res = client.get("/api/camera/settings/camera.iso", headers=AUTH)

    assert res.status_code == 200, f"expected admitted metadata, got {res.status_code}: {res.text}"
    read_setting.assert_called_once()
    assert read_setting.call_args.kwargs["live"] is False


def test_live_setting_read_rejects_missing_token_before_hardware() -> None:
    client = TestClient(backend_app.app)
    with patch.object(backend_app.settings_control, "read_setting") as read_setting:
        res = client.get("/api/camera/settings/camera.iso?live=true")

    assert res.status_code == 401, f"expected 401 without token, got {res.status_code}: {res.text}"
    read_setting.assert_not_called()


def test_live_setting_read_rejects_wrong_token_before_hardware() -> None:
    client = TestClient(backend_app.app)
    with patch.object(backend_app.settings_control, "read_setting") as read_setting:
        res = client.get(
            "/api/camera/settings/camera.iso?live=true",
            headers={"X-Camera-Autopilot-Token": "wrong-token"},
        )

    assert res.status_code == 401, f"expected 401 with wrong token, got {res.status_code}: {res.text}"
    read_setting.assert_not_called()


def test_live_setting_read_accepts_valid_token() -> None:
    client = TestClient(backend_app.app)
    with patch.object(
        backend_app.settings_control,
        "read_setting",
        return_value={"current": "800", "route": "usb_gphoto"},
    ) as read_setting:
        res = client.get("/api/camera/settings/camera.iso?live=true", headers=AUTH)

    assert res.status_code == 200, f"expected 200 with valid token, got {res.status_code}: {res.text}"
    read_setting.assert_called_once()
    assert read_setting.call_args.kwargs["live"] is True


def test_record_returns_501_not_500_when_engine_cannot_record() -> None:
    """Auth runs before the unsupported-engine response and still returns an honest 501."""
    client = TestClient(backend_app.app)
    original_engine = backend_app.engine
    backend_app.engine = wifi_engine.WiFiEngine()
    try:
        denied = client.post("/record", json={"intent": "moody clip", "seconds": 2})
        assert denied.status_code == 401, f"expected auth before engine check, got {denied.status_code}"
        res = client.post("/record", json={"intent": "moody clip", "seconds": 2}, headers=AUTH)
        assert res.status_code == 501, f"expected 501, got {res.status_code}"
        assert "wifi" in res.json()["detail"]
    finally:
        backend_app.engine = original_engine


class _RecordingEngine:
    name = "test-recording"

    def __init__(self) -> None:
        self.calls = 0

    def record(self, intent: str | None, seconds: float) -> engine_module.Recording:
        self.calls += 1
        return engine_module.Recording(
            ok=True,
            image="shot_test.jpg",
            summary="Recorded a test clip.",
            why="Test-only recording result.",
            mood={"heard": intent or ""},
            settings={},
            recording={"recorded_seconds": seconds, "samples": []},
        )


def _record_client(*, base_url: str = "http://testserver", client_host: str = "testclient") -> TestClient:
    return TestClient(
        backend_app.app,
        base_url=base_url,
        client=(client_host, 50000),
    )


def test_record_rejects_missing_or_wrong_token_before_engine() -> None:
    fake_engine = _RecordingEngine()
    original_engine = backend_app.engine
    backend_app.engine = fake_engine
    try:
        client = _record_client()
        missing = client.post("/record", json={"intent": "quick clip", "seconds": 1})
        wrong = client.post(
            "/record",
            json={"intent": "quick clip", "seconds": 1},
            headers={"X-Camera-Autopilot-Token": "wrong-token"},
        )
        remote = _record_client(
            base_url="http://camera.example.test",
            client_host="192.0.2.10",
        )
        remote_missing = remote.post("/record", json={"intent": "quick clip", "seconds": 1})
        remote_wrong = remote.post(
            "/record",
            json={"intent": "quick clip", "seconds": 1},
            headers={"X-Camera-Autopilot-Token": "wrong-token"},
        )
        assert missing.status_code == 401
        assert wrong.status_code == 401
        assert remote_missing.status_code == 401
        assert remote_wrong.status_code == 401
        assert fake_engine.calls == 0
    finally:
        backend_app.engine = original_engine


def test_record_fails_closed_without_configured_token_even_on_loopback() -> None:
    fake_engine = _RecordingEngine()
    original_engine = backend_app.engine
    original_token = backend_app.API_TOKEN
    backend_app.engine = fake_engine
    backend_app.API_TOKEN = ""
    try:
        res = _record_client().post("/record", json={"intent": "quick clip", "seconds": 1})
        assert res.status_code == 503
        assert fake_engine.calls == 0
    finally:
        backend_app.engine = original_engine
        backend_app.API_TOKEN = original_token


def test_record_accepts_valid_loopback_token_and_calls_engine_once() -> None:
    fake_engine = _RecordingEngine()
    original_engine = backend_app.engine
    backend_app.engine = fake_engine
    try:
        res = _record_client().post(
            "/record",
            json={"intent": "quick clip", "seconds": 1},
            headers=AUTH,
        )
        assert res.status_code == 200, res.text
        assert res.json()["ok"] is True
        assert fake_engine.calls == 1
    finally:
        backend_app.engine = original_engine


def test_record_rejects_valid_token_over_nonloopback_plaintext_before_engine() -> None:
    fake_engine = _RecordingEngine()
    original_engine = backend_app.engine
    backend_app.engine = fake_engine
    try:
        res = _record_client(
            base_url="http://camera.example.test",
            client_host="192.0.2.10",
        ).post(
            "/record",
            json={"intent": "quick clip", "seconds": 1},
            headers=AUTH,
        )
        assert res.status_code == 403, res.text
        assert fake_engine.calls == 0
    finally:
        backend_app.engine = original_engine


def test_record_accepts_valid_token_over_https_before_engine() -> None:
    fake_engine = _RecordingEngine()
    original_engine = backend_app.engine
    backend_app.engine = fake_engine
    try:
        res = _record_client(
            base_url="https://camera.example.test",
            client_host="192.0.2.10",
        ).post(
            "/record",
            json={"intent": "quick clip", "seconds": 1},
            headers=AUTH,
        )
        assert res.status_code == 200, res.text
        assert fake_engine.calls == 1
    finally:
        backend_app.engine = original_engine


if __name__ == "__main__":
    raise SystemExit(run_module_tests(__name__))
