"""Camera-free HTTP security contract tests for the USB/PTP bridge."""

from __future__ import annotations

import contextlib
import http.client
import io
import json
import sys
import tempfile
import threading
from collections.abc import Iterator
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "tools"))

import usb_ptp_bridge_server

TEST_TOKEN = "camera-autopilot-test-token-0001"
TRUSTED_ORIGIN = "http://phone-client.local:4173"


def _fake_run_factory(calls: list[list[str]]):
    def fake_run(cmd: list[str], *, timeout: float = 30.0) -> dict[str, Any]:
        calls.append(cmd)
        stdout = "OK\n"
        if cmd[:2] == ["gphoto2", "--auto-detect"]:
            stdout = "Sony ILCE-7M2 (Control)        usb:001,001\n"
        elif cmd[:2] == ["gphoto2", "--summary"]:
            stdout = "Camera summary\nModel: Sony ILCE-7M2\n"
        elif cmd[:3] == ["gphoto2", "--get-config", "focusmode"]:
            stdout = "Label: Focus Mode\nCurrent: Automatic\n"
        elif cmd[:3] == ["gphoto2", "--get-config", "iso"]:
            stdout = "Label: ISO\nCurrent: 800\nChoice: 0 100\nChoice: 1 800\n"
        elif cmd[:2] == ["gphoto2", "--capture-preview"]:
            filename = Path(cmd[cmd.index("--filename") + 1])
            filename.write_bytes(b"\xff\xd8camera-free-preview\xff\xd9")
            stdout = f"Saving file as {filename}\n"
        elif "--capture-image" in cmd[1] or "--capture-image-and-download" in cmd[1]:
            stdout = "New file is in location /capt_DSC00001.JPG on the camera\n"
        return {
            "cmd": cmd,
            "returncode": 0,
            "timed_out": False,
            "stdout": stdout,
            "stderr": "",
            "elapsed_s": 0.01,
        }

    return fake_run


@contextlib.contextmanager
def _running_bridge(
    *,
    api_token: str | None = TEST_TOKEN,
    cors_origins: tuple[str, ...] = (),
    allow_unauthenticated_lab: bool = False,
) -> Iterator[tuple[int, list[list[str]], usb_ptp_bridge_server.BridgeState]]:
    calls: list[list[str]] = []
    original_run = usb_ptp_bridge_server._run
    usb_ptp_bridge_server._run = _fake_run_factory(calls)
    with tempfile.TemporaryDirectory() as tmp:
        state = usb_ptp_bridge_server.BridgeState(
            Path(tmp),
            "python3",
            cors_origins=cors_origins,
            api_token=api_token,
            allow_unauthenticated_lab=allow_unauthenticated_lab,
        )
        server = usb_ptp_bridge_server.BridgeServer(
            ("127.0.0.1", 0),
            usb_ptp_bridge_server.Handler,
            state,
        )
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            yield server.server_address[1], calls, state
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=5)
            usb_ptp_bridge_server._run = original_run


def _request(
    port: int,
    method: str,
    path: str,
    *,
    headers: dict[str, str] | None = None,
    body: dict[str, Any] | None = None,
) -> tuple[int, dict[str, str], bytes]:
    request_headers = dict(headers or {})
    encoded_body = None
    if body is not None:
        encoded_body = json.dumps(body).encode("utf-8")
        request_headers.setdefault("Content-Type", "application/json")
    connection = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
    try:
        connection.request(method, path, body=encoded_body, headers=request_headers)
        response = connection.getresponse()
        payload = response.read()
        return response.status, dict(response.getheaders()), payload
    finally:
        connection.close()


def _json_payload(raw: bytes) -> dict[str, Any]:
    payload = json.loads(raw.decode("utf-8"))
    assert isinstance(payload, dict)
    return payload


def test_every_camera_operation_fails_closed_without_valid_authentication() -> None:
    protected_requests = (
        ("GET", "/ready", None),
        ("GET", "/settings", None),
        ("GET", "/settings/camera.iso", None),
        ("GET", "/preview.jpg", None),
        ("POST", "/settings/camera.iso", {"value": "800", "dry_run": False}),
        ("POST", "/focus/af-point", {"x": 320, "y": 240}),
        ("POST", "/capture", {"download": True}),
    )
    invalid_auth_headers = (
        {},
        {"Authorization": "Bearer wrong-token"},
        {"X-API-Key": "wrong-token"},
    )

    with _running_bridge() as (port, calls, _):
        for method, path, body in protected_requests:
            for headers in invalid_auth_headers:
                status, response_headers, raw = _request(
                    port,
                    method,
                    path,
                    headers=headers,
                    body=body,
                )
                assert status == 401, (method, path, headers, status, raw)
                assert response_headers["WWW-Authenticate"].startswith("Bearer ")
                assert response_headers["Cache-Control"] == "no-store"
                assert _json_payload(raw) == {
                    "error": "authentication required",
                    "ok": False,
                }
        assert calls == [], "unauthenticated requests must not reach camera command handlers"


def test_public_health_is_process_only_and_static_control_page_remains_public() -> None:
    with _running_bridge() as (port, calls, state):
        artifact_entries_before = tuple(state.artifact_root.iterdir())
        health_status, _, health_raw = _request(port, "GET", "/health")
        index_status, _, index_raw = _request(port, "GET", "/")
        artifact_entries_after = tuple(state.artifact_root.iterdir())

    assert health_status == 200
    assert _json_payload(health_raw) == {
        "ok": True,
        "service": "usb-ptp-bridge",
        "status": "alive",
    }
    assert index_status == 200
    assert b"A7 II USB Bridge" in index_raw
    assert calls == [], "public health must not invoke the camera command runner"
    assert artifact_entries_after == artifact_entries_before == (), (
        "public health and the static page must not create request artifacts"
    )


def test_authenticated_readiness_checks_camera_and_records_artifact() -> None:
    bearer = {"Authorization": f"Bearer {TEST_TOKEN}"}

    with _running_bridge() as (port, calls, state):
        readiness_status, _, readiness_raw = _request(
            port,
            "GET",
            "/ready",
            headers=bearer,
        )
        artifact_entries = tuple(state.artifact_root.iterdir())

        assert readiness_status == 200
        readiness = _json_payload(readiness_raw)
        assert readiness["ok"] is True
        assert readiness["camera_detected"] is True
        assert readiness["artifact_dir"]
        assert calls == [["gphoto2", "--auto-detect"]]
        assert len(artifact_entries) == 1
        assert artifact_entries[0].is_dir()
        assert (artifact_entries[0] / "gphoto-auto-detect.json").is_file()


def test_authenticated_camera_requests_preserve_handler_behavior_with_mocks() -> None:
    bearer = {"Authorization": f"Bearer {TEST_TOKEN}"}
    api_key = {"X-API-Key": TEST_TOKEN}

    with _running_bridge() as (port, calls, _):
        setting_status, _, setting_raw = _request(
            port,
            "POST",
            "/settings/camera.iso",
            headers=bearer,
            body={"value": "800", "dry_run": False},
        )
        focus_status, _, focus_raw = _request(
            port,
            "POST",
            "/focus/af-point",
            headers=api_key,
            body={"x": 123, "y": 234, "focus_area": "0x102"},
        )
        preview_status, preview_headers, preview_raw = _request(
            port,
            "GET",
            "/preview.jpg",
            headers=bearer,
        )
        capture_status, _, capture_raw = _request(
            port,
            "POST",
            "/capture",
            headers=api_key,
            body={"download": True},
        )

    setting = _json_payload(setting_raw)
    focus = _json_payload(focus_raw)
    capture = _json_payload(capture_raw)
    assert setting_status == 200
    assert setting["ok"] is True
    assert setting["applied"]["node"] == "iso"
    assert focus_status == 200
    assert focus["ok"] is True
    assert (focus["x"], focus["y"]) == (123, 234)
    assert preview_status == 410
    assert preview_headers["Content-Type"] == "application/json; charset=utf-8"
    assert _json_payload(preview_raw)["ok"] is False
    assert "preview is disabled" in _json_payload(preview_raw)["error"]
    assert capture_status == 410
    assert capture == {
        "ok": False,
        "error": "capture is disabled: gphoto2 %C cannot prove a JPEG-only artifact",
    }
    assert ["gphoto2", "--set-config", "iso=800"] in calls
    assert not any("--capture-preview" in cmd for cmd in calls)
    assert not any("--capture-image-and-download" in cmd for cmd in calls)
    assert any("tests/probe_raw_control.py" in cmd for cmd in calls)


def test_cors_is_disabled_by_default_and_exact_origin_when_configured() -> None:
    with _running_bridge(cors_origins=(TRUSTED_ORIGIN,)) as (port, _, _):
        allowed_status, allowed_headers, _ = _request(
            port,
            "GET",
            "/settings/camera.iso",
            headers={
                "Authorization": f"Bearer {TEST_TOKEN}",
                "Origin": TRUSTED_ORIGIN,
            },
        )
        denied_status, denied_headers, _ = _request(
            port,
            "GET",
            "/settings/camera.iso",
            headers={
                "Authorization": f"Bearer {TEST_TOKEN}",
                "Origin": "http://untrusted.example",
            },
        )
        preflight_status, preflight_headers, _ = _request(
            port,
            "OPTIONS",
            "/capture",
            headers={
                "Origin": TRUSTED_ORIGIN,
                "Access-Control-Request-Method": "POST",
                "Access-Control-Request-Headers": "Authorization, Content-Type",
            },
        )
        bad_method_status, _, _ = _request(
            port,
            "OPTIONS",
            "/capture",
            headers={
                "Origin": TRUSTED_ORIGIN,
                "Access-Control-Request-Method": "DELETE",
            },
        )
        bad_header_status, _, _ = _request(
            port,
            "OPTIONS",
            "/capture",
            headers={
                "Origin": TRUSTED_ORIGIN,
                "Access-Control-Request-Method": "POST",
                "Access-Control-Request-Headers": "X-Untrusted-Header",
            },
        )
        bad_origin_status, _, _ = _request(
            port,
            "OPTIONS",
            "/capture",
            headers={
                "Origin": "http://untrusted.example",
                "Access-Control-Request-Method": "POST",
            },
        )

    assert allowed_status == 200
    assert allowed_headers["Access-Control-Allow-Origin"] == TRUSTED_ORIGIN
    assert allowed_headers["Vary"] == "Origin"
    assert denied_status == 200
    assert "Access-Control-Allow-Origin" not in denied_headers
    assert preflight_status == 204
    assert preflight_headers["Access-Control-Allow-Origin"] == TRUSTED_ORIGIN
    assert preflight_headers["Access-Control-Allow-Methods"] == "GET, POST"
    assert preflight_headers["Access-Control-Allow-Headers"] == (
        "Authorization, Content-Type, X-API-Key"
    )
    assert bad_method_status == 405
    assert bad_header_status == 403
    assert bad_origin_status == 403

    with _running_bridge() as (port, _, _):
        status, headers, _ = _request(
            port,
            "GET",
            "/settings/camera.iso",
            headers={
                "Authorization": f"Bearer {TEST_TOKEN}",
                "Origin": TRUSTED_ORIGIN,
            },
        )
    assert status == 200
    assert "Access-Control-Allow-Origin" not in headers


def test_cors_rejects_wildcards_and_malformed_origins() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        for origins in (
            ("*",),
            ("phone-client.local",),
            ("http://user:password@phone-client.local",),
            ("http://phone-client.local/path",),
        ):
            try:
                usb_ptp_bridge_server.BridgeState(
                    Path(tmp),
                    "python3",
                    cors_origins=origins,
                    api_token=TEST_TOKEN,
                )
            except ValueError:
                continue
            raise AssertionError(f"unsafe CORS origin was accepted: {origins!r}")


def _legacy_test_default_cli_is_loopback_authenticated_and_lab_bypass_is_explicit() -> None:
    captured: list[tuple[tuple[str, int], usb_ptp_bridge_server.BridgeState]] = []

    class FakeServer:
        def __init__(self, address, _handler_cls, state):
            captured.append((address, state))

        def serve_forever(self):
            return None

    original_server = usb_ptp_bridge_server.BridgeServer
    original_argv = sys.argv
    usb_ptp_bridge_server.BridgeServer = FakeServer
    try:
        with tempfile.TemporaryDirectory() as tmp:
            sys.argv = [
                "usb_ptp_bridge_server.py",
                "--artifact-root",
                tmp,
                "--api-token",
                TEST_TOKEN,
            ]
            assert usb_ptp_bridge_server.main() == 0
            address, state = captured.pop()
            assert address == ("127.0.0.1", 8765)
            assert state.api_token == TEST_TOKEN
            assert state.allow_unauthenticated_lab is False
            assert state.cors_origins == ()

            sys.argv = [
                "usb_ptp_bridge_server.py",
                "--artifact-root",
                tmp,
                "--lab-allow-unauthenticated",
            ]
            assert usb_ptp_bridge_server.main() == 0
            address, state = captured.pop()
            assert address == ("127.0.0.1", 8765)
            assert state.api_token is None
            assert state.allow_unauthenticated_lab is True

            sys.argv = [
                "usb_ptp_bridge_server.py",
                "--host",
                "0.0.0.0",
                "--artifact-root",
                tmp,
                "--lab-allow-unauthenticated",
            ]
            with contextlib.redirect_stderr(io.StringIO()):
                try:
                    usb_ptp_bridge_server.main()
                except SystemExit as exc:
                    assert exc.code == 2
                else:
                    raise AssertionError("LAN lab bypass must fail closed")
    finally:
        usb_ptp_bridge_server.BridgeServer = original_server
        sys.argv = original_argv


def _legacy_test_lab_bypass_is_off_by_default_and_only_works_when_explicitly_enabled() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        for token in (None, "too-short"):
            state = usb_ptp_bridge_server.BridgeState(
                Path(tmp),
                "python3",
                api_token=token,
            )
            try:
                usb_ptp_bridge_server.BridgeServer(
                    ("127.0.0.1", 0),
                    usb_ptp_bridge_server.Handler,
                    state,
                )
            except ValueError as exc:
                assert "token" in str(exc).lower()
            else:
                raise AssertionError("BridgeServer must reject a missing or short token")

    with _running_bridge(
        api_token=None,
        allow_unauthenticated_lab=True,
    ) as (port, calls, _):
        status, _, raw = _request(port, "GET", "/settings")
    assert status == 200
    assert _json_payload(raw)["ok"] is True
    assert calls != []


def test_default_cli_is_loopback_authenticated_and_lab_bypass_is_explicit() -> None:
    """The command-line rejection occurs before a bridge server can be created."""
    original_argv = sys.argv
    try:
        for argv in (
            ["usb_ptp_bridge_server.py", "--api-token", TEST_TOKEN, "--host", "0.0.0.0"],
            ["usb_ptp_bridge_server.py", "--api-token", TEST_TOKEN, "--lab-allow-unauthenticated"],
        ):
            sys.argv = argv
            with contextlib.redirect_stderr(io.StringIO()):
                try:
                    usb_ptp_bridge_server.main()
                except SystemExit as exc:
                    assert exc.code == 2
                else:
                    raise AssertionError(f"unsafe bridge CLI was accepted: {argv}")
    finally:
        sys.argv = original_argv


def test_runner_reaps_process_group_after_post_spawn_oserror() -> None:
    """A selector failure after Popen cannot leave a gphoto child running."""
    class FakePipe:
        pass

    class FakeProcess:
        pid = 4242
        stdout = FakePipe()
        stderr = FakePipe()

        def __init__(self):
            self.waits = 0

        def wait(self, timeout=None):
            self.waits += 1
            return 0

    class BrokenSelector:
        def register(self, *_args):
            return None

        def get_map(self):
            return {1: object()}

        def select(self, _timeout):
            raise OSError("injected selector failure")

        def close(self):
            closed.append(True)

    original_popen = usb_ptp_bridge_server.subprocess.Popen
    original_selector = usb_ptp_bridge_server.selectors.DefaultSelector
    original_killpg = usb_ptp_bridge_server.os.killpg
    process = FakeProcess()
    killed: list[tuple[int, int]] = []
    closed: list[bool] = []
    try:
        usb_ptp_bridge_server.subprocess.Popen = lambda *_args, **_kwargs: process
        usb_ptp_bridge_server.selectors.DefaultSelector = BrokenSelector
        usb_ptp_bridge_server.os.killpg = lambda pid, signal: killed.append((pid, signal))
        result = usb_ptp_bridge_server._run(["fake-command"], timeout=0.1)
    finally:
        usb_ptp_bridge_server.subprocess.Popen = original_popen
        usb_ptp_bridge_server.selectors.DefaultSelector = original_selector
        usb_ptp_bridge_server.os.killpg = original_killpg

    assert result["returncode"] is None
    assert "injected selector failure" in result["stderr"]
    assert killed == [(4242, 15)]
    assert process.waits == 1
    assert closed == [True]


def test_json_responses_have_a_combined_byte_cap() -> None:
    handler = object.__new__(usb_ptp_bridge_server.Handler)
    handler.wfile = io.BytesIO()
    statuses: list[int] = []
    headers: dict[str, str] = {}
    handler.send_response = lambda status: statuses.append(status)
    handler.send_header = lambda key, value: headers.__setitem__(key, value)
    handler.end_headers = lambda: None
    handler._cors_headers = lambda: None

    usb_ptp_bridge_server.Handler._json(
        handler,
        usb_ptp_bridge_server.HTTPStatus.OK,
        {"ok": True, "unbounded_candidate": "x" * (usb_ptp_bridge_server.MAX_JSON_RESPONSE_BYTES + 1)},
    )

    payload = handler.wfile.getvalue()
    assert statuses == [500]
    assert len(payload) <= usb_ptp_bridge_server.MAX_JSON_RESPONSE_BYTES
    assert _json_payload(payload)["ok"] is False


def test_retention_deletes_only_marked_bridge_request_directories() -> None:
    with tempfile.TemporaryDirectory() as tmp, tempfile.TemporaryDirectory() as outside_tmp:
        root = Path(tmp)
        outside = Path(outside_tmp) / "outside"
        outside.mkdir()
        sentinel = outside / "keep.txt"
        sentinel.write_text("do not delete", encoding="utf-8")
        unrelated = root / "unrelated"
        unrelated.mkdir()
        link = root / "external-link"
        link.symlink_to(outside, target_is_directory=True)
        forged = root / "bridge-request-00000000-0000-0000-0000-000000000000-1-forged"
        forged.mkdir()

        old_limit = usb_ptp_bridge_server.MAX_ARTIFACT_DIRECTORIES
        usb_ptp_bridge_server.MAX_ARTIFACT_DIRECTORIES = 2
        try:
            state = usb_ptp_bridge_server.BridgeState(root, "python3", api_token=TEST_TOKEN)
            first = state.request_dir("first")
            second = state.request_dir("second")
            third = state.request_dir("third")
        finally:
            usb_ptp_bridge_server.MAX_ARTIFACT_DIRECTORIES = old_limit

        assert not first.exists(), "old marked request directory should be retained away"
        assert second.is_dir() and third.is_dir()
        assert unrelated.is_dir()
        assert forged.is_dir(), "name alone is not bridge ownership"
        assert link.is_symlink()
        assert sentinel.read_text(encoding="utf-8") == "do not delete"


def test_lab_bypass_is_off_by_default_and_only_works_when_explicitly_enabled() -> None:
    test_default_cli_is_loopback_authenticated_and_lab_bypass_is_explicit()
    test_runner_reaps_process_group_after_post_spawn_oserror()
    test_json_responses_have_a_combined_byte_cap()


if __name__ == "__main__":
    test_every_camera_operation_fails_closed_without_valid_authentication()
    test_public_health_is_process_only_and_static_control_page_remains_public()
    test_authenticated_readiness_checks_camera_and_records_artifact()
    test_authenticated_camera_requests_preserve_handler_behavior_with_mocks()
    test_cors_is_disabled_by_default_and_exact_origin_when_configured()
    test_cors_rejects_wildcards_and_malformed_origins()
    test_default_cli_is_loopback_authenticated_and_lab_bypass_is_explicit()
    test_lab_bypass_is_off_by_default_and_only_works_when_explicitly_enabled()
    test_retention_deletes_only_marked_bridge_request_directories()
    print("usb_ptp_bridge_security tests: PASS")
