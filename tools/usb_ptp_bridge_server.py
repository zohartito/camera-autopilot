#!/usr/bin/env python3
"""Tiny USB/PTP-to-HTTP bridge proof for the Sony A7 II.

This is not the production server. It is a falsification artifact for the
"USB is only a transport bridge" claim: can a host expose a WiFi/LAN-friendly
API while driving the camera over USB?

Endpoints:
  GET  /health
  GET  /ready                 (authenticated)
  GET  /settings              (authenticated)
  GET  /settings/{setting_id} (authenticated)
  POST /settings/{setting_id} (authenticated)
  GET  /preview.jpg           (authenticated; disabled, returns 410)
  POST /focus/af-point        (authenticated)

All camera operations are serialized because libgphoto2/PyUSB require exclusive
access to the PTP interface. Authentication accepts either
``Authorization: Bearer <token>`` or ``X-API-Key: <token>``. The static control
page and health probe are public; every camera read/write route is fail-closed.
The public health probe reports process liveness only. Camera readiness is
checked exclusively by the authenticated ``/ready`` endpoint.
"""

from __future__ import annotations

import argparse
import hmac
import ipaddress
import json
import os
import re
import selectors
import shutil
import stat
import subprocess
import sys
import threading
import time
import uuid
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urlsplit

REPO_ROOT = Path(__file__).resolve().parent.parent
BACKEND = REPO_ROOT / "backend"
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))

import settings_control
import settings_registry

DEFAULT_ARTIFACT_ROOT = REPO_ROOT / "artifacts" / "falsification" / "r2_usb_bridge"
DEFAULT_RAW_PTP_PYTHON = sys.executable
DEFAULT_API_TOKEN_ENV = "CAMERA_AUTOPILOT_USB_BRIDGE_TOKEN"
MIN_API_TOKEN_LENGTH = 24
MAX_REQUEST_BODY_BYTES = 32 * 1024
MAX_CONCURRENT_REQUESTS = 8
MAX_ARTIFACT_DIRECTORIES = 100
REQUEST_DIR_PREFIX = "bridge-request-"
REQUEST_DIR_MARKER = ".camera-autopilot-bridge-request.json"
REQUEST_DIR_OWNER = "camera-autopilot-usb-bridge"
MAX_REQUEST_DIR_MARKER_BYTES = 512
CAPTURE_MIN_INTERVAL_SECONDS = 10.0
MAX_SUBPROCESS_OUTPUT_BYTES = 1 * 1024 * 1024
MAX_JSON_RESPONSE_BYTES = 2 * 1024 * 1024
USB_BRIDGE_WRITABLE_ROUTE_STATUSES = {"proven_set", "available_not_wrapped", "protocol_ok_needs_behavior"}
PUBLIC_PATHS = {"/", "/health"}
ALLOWED_CORS_METHODS = {"GET", "POST"}
ALLOWED_CORS_HEADERS = {"authorization", "content-type", "x-api-key"}

_camera_lock = threading.RLock()


def _timestamp() -> str:
    return time.strftime("%Y%m%d-%H%M%S")


def _run(cmd: list[str], *, timeout: float = 30.0) -> dict[str, Any]:
    started = time.time()
    stdout = bytearray()
    stderr = bytearray()
    overflow = False
    timed_out = False
    proc: subprocess.Popen[bytes] | None = None
    selector: selectors.BaseSelector | None = None
    run_error: str | None = None
    try:
        proc = subprocess.Popen(
            cmd,
            cwd=REPO_ROOT,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            start_new_session=True,
        )
        assert proc.stdout and proc.stderr
        selector = selectors.DefaultSelector()
        selector.register(proc.stdout, selectors.EVENT_READ, stdout)
        selector.register(proc.stderr, selectors.EVENT_READ, stderr)
        deadline = time.monotonic() + timeout
        while selector.get_map():
            if time.monotonic() >= deadline:
                timed_out = True
                break
            for key, _ in selector.select(max(0.0, deadline - time.monotonic())):
                chunk = os.read(key.fileobj.fileno(), 64 * 1024)
                if not chunk:
                    selector.unregister(key.fileobj)
                    continue
                target = key.data
                if len(target) + len(chunk) > MAX_SUBPROCESS_OUTPUT_BYTES:
                    overflow = True
                    break
                target.extend(chunk)
            if overflow:
                break
    except OSError as exc:
        run_error = str(exc)
    finally:
        if selector is not None:
            try:
                selector.close()
            except OSError:
                pass
        if proc is not None:
            # This cleanup intentionally covers selector/register/read failures after Popen.
            # The child owns a new session, so descendants receive the same signal.
            if timed_out or overflow or run_error is not None:
                try:
                    os.killpg(proc.pid, 15)
                except (ProcessLookupError, OSError):
                    pass
            try:
                proc.wait(timeout=2)
            except subprocess.TimeoutExpired:
                try:
                    os.killpg(proc.pid, 9)
                except (ProcessLookupError, OSError):
                    pass
                try:
                    proc.wait(timeout=2)
                except subprocess.TimeoutExpired:
                    run_error = run_error or "subprocess did not exit after SIGKILL"
    if run_error is not None:
        return {
            "cmd": cmd,
            "returncode": None,
            "timed_out": False,
            "output_overflow": False,
            "stdout": stdout.decode("utf-8", "replace"),
            "stderr": run_error,
            "elapsed_s": round(time.time() - started, 3),
        }
    return {
        "cmd": cmd,
        "returncode": None if timed_out or overflow else proc.returncode,
        "timed_out": timed_out,
        "output_overflow": overflow,
        "stdout": stdout.decode("utf-8", "replace"),
        "stderr": stderr.decode("utf-8", "replace") or ("output exceeded byte limit" if overflow else ""),
        "elapsed_s": round(time.time() - started, 3),
    }


def _sanitize_name(name: str) -> str:
    return re.sub(r"[^A-Za-z0-9_.-]+", "_", name).strip("_") or "request"


def _current_from_gphoto(stdout: str) -> str | None:
    for line in stdout.splitlines():
        match = re.match(r"\s*Current:\s*(.+?)\s*$", line)
        if match:
            return match.group(1)
    return None


def _is_loopback_host(host: str) -> bool:
    normalized = host.strip().strip("[]")
    if normalized.lower() == "localhost":
        return True
    try:
        return ipaddress.ip_address(normalized).is_loopback
    except ValueError:
        return False


def _normalize_cors_origins(origins: list[str] | tuple[str, ...] | None) -> tuple[str, ...]:
    normalized: list[str] = []
    for raw_origin in origins or ():
        origin = raw_origin.strip().rstrip("/")
        if not origin:
            continue
        if origin == "*":
            raise ValueError("wildcard CORS is disabled; configure each trusted origin explicitly")
        parsed = urlsplit(origin)
        if (
            parsed.scheme not in {"http", "https"}
            or not parsed.netloc
            or parsed.username is not None
            or parsed.password is not None
            or parsed.path
            or parsed.query
            or parsed.fragment
        ):
            raise ValueError(f"CORS origin must be an exact http(s) origin: {raw_origin!r}")
        if origin not in normalized:
            normalized.append(origin)
    return tuple(normalized)


def _row_bridge_summary(row: dict[str, Any]) -> dict[str, Any]:
    usb = row.get("routes", {}).get("usb_gphoto", {})
    route_status = usb.get("status")
    guarded = row["status"] == "dangerous_admin" or row["setting_id"] in settings_registry.DESTRUCTIVE_SETTING_IDS
    can_live_write = bool(usb.get("node")) and route_status in USB_BRIDGE_WRITABLE_ROUTE_STATUSES and not guarded
    behavior_proven = route_status == "proven_set"
    return {
        "setting_id": row["setting_id"],
        "label": row["label"],
        "status": row["status"],
        "current_photo_value": row["current_photo_value"],
        "greyed_in_photo": row["greyed_in_photo"],
        "node": usb.get("node"),
        "route_status": route_status,
        "can_live_read": bool(usb.get("node")),
        "can_live_write": can_live_write,
        "product_write_ready": can_live_write and behavior_proven,
        "requires_behavior_proof": can_live_write and not behavior_proven,
        "behavior_proven": behavior_proven,
        "guarded": guarded,
        "next_action": row["next_action"],
    }


def _bounded_json(payload: dict[str, Any], limit: int = MAX_JSON_RESPONSE_BYTES) -> bytes | None:
    """Serialize a response incrementally so multiple bounded command outputs cannot combine unboundedly."""
    encoded = bytearray()
    for part in json.JSONEncoder(indent=2, sort_keys=True).iterencode(payload):
        chunk = part.encode("utf-8")
        if len(encoded) + len(chunk) > limit:
            return None
        encoded.extend(chunk)
    return bytes(encoded)


class BridgeState:
    def __init__(
        self,
        artifact_root: Path,
        raw_ptp_python: str,
        cors_origin: str | None = None,
        *,
        cors_origins: list[str] | tuple[str, ...] | None = None,
        api_token: str | None = None,
        allow_unauthenticated_lab: bool = False,
    ):
        if not artifact_root.is_absolute():
            artifact_root = REPO_ROOT / artifact_root
        if cors_origin is not None and cors_origins is not None:
            raise ValueError("pass cors_origin or cors_origins, not both")
        configured_origins = cors_origins if cors_origins is not None else ([cors_origin] if cors_origin else [])
        self.artifact_root = artifact_root.resolve()
        self.raw_ptp_python = raw_ptp_python
        self.cors_origins = _normalize_cors_origins(configured_origins)
        self.api_token = api_token.strip() if api_token and api_token.strip() else None
        if allow_unauthenticated_lab:
            raise ValueError("unauthenticated lab mode is disabled for physical camera operations")
        self.allow_unauthenticated_lab = False
        self.request_slots = threading.BoundedSemaphore(MAX_CONCURRENT_REQUESTS)
        self.capture_lock = threading.Lock()
        self.retention_lock = threading.Lock()
        self._request_counter = 0
        self.last_capture_at = 0.0
        self.artifact_root.mkdir(parents=True, exist_ok=True)

    def request_dir(self, label: str) -> Path:
        with self.retention_lock:
            owned_directories = sorted(
                (path for path in self.artifact_root.iterdir() if self._is_owned_request_dir(path)),
                key=self._request_dir_created_ns,
            )
            for old in owned_directories[: max(0, len(owned_directories) - MAX_ARTIFACT_DIRECTORIES + 1)]:
                # Recheck immediately before recursive removal in case an artifact was replaced.
                if self._is_owned_request_dir(old):
                    shutil.rmtree(old)
            self._request_counter += 1
            request_id = uuid.uuid4()
            out = self.artifact_root / (
                f"{REQUEST_DIR_PREFIX}{request_id}-{self._request_counter}-{_sanitize_name(label)}"
            )
            out.mkdir(mode=0o700)
            marker = {
                "created_ns": time.time_ns(),
                "owner": REQUEST_DIR_OWNER,
                "request_id": str(request_id),
            }
            (out / REQUEST_DIR_MARKER).write_text(json.dumps(marker, sort_keys=True), encoding="utf-8")
            return out

    @staticmethod
    def _request_dir_created_ns(path: Path) -> int:
        try:
            marker = json.loads((path / REQUEST_DIR_MARKER).read_text(encoding="utf-8"))
            created_ns = marker.get("created_ns") if isinstance(marker, dict) else None
            return created_ns if isinstance(created_ns, int) and created_ns > 0 else 0
        except (OSError, ValueError, json.JSONDecodeError):
            return 0

    def _is_owned_request_dir(self, path: Path) -> bool:
        """Recognize only directories made by this bridge; retention never touches anything else."""
        try:
            root = self.artifact_root.resolve(strict=True)
            entry = path.lstat()
            if not stat.S_ISDIR(entry.st_mode) or stat.S_ISLNK(entry.st_mode):
                return False
            if path.parent.resolve(strict=True) != root:
                return False
            match = re.fullmatch(
                rf"{re.escape(REQUEST_DIR_PREFIX)}"
                r"([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})"
                r"-[0-9]+-[A-Za-z0-9._-]+",
                path.name,
            )
            if not match:
                return False
            marker_path = path / REQUEST_DIR_MARKER
            marker_stat = marker_path.lstat()
            if not stat.S_ISREG(marker_stat.st_mode) or stat.S_ISLNK(marker_stat.st_mode):
                return False
            if marker_stat.st_size > MAX_REQUEST_DIR_MARKER_BYTES:
                return False
            marker = json.loads(marker_path.read_text(encoding="utf-8"))
            return (
                isinstance(marker, dict)
                and set(marker) == {"created_ns", "owner", "request_id"}
                and marker["owner"] == REQUEST_DIR_OWNER
                and marker["request_id"] == match.group(1)
                and isinstance(marker["created_ns"], int)
                and marker["created_ns"] > 0
            )
        except (OSError, ValueError, json.JSONDecodeError):
            return False

    def admit_capture(self) -> bool:
        with self.capture_lock:
            now = time.monotonic()
            if now - self.last_capture_at < CAPTURE_MIN_INTERVAL_SECONDS:
                return False
            self.last_capture_at = now
            return True

    @staticmethod
    def label(path: Path) -> str:
        path = path.resolve()
        try:
            return str(path.relative_to(REPO_ROOT))
        except ValueError:
            return str(path)


class Handler(BaseHTTPRequestHandler):
    server_version = "USBPTPBridge/0.3"

    @property
    def state(self) -> BridgeState:
        return self.server.state  # type: ignore[attr-defined]

    def log_message(self, fmt: str, *args: Any) -> None:
        # Keep stdout concise; request artifacts carry command detail.
        print(f"{self.address_string()} - {fmt % args}")

    def handle_one_request(self) -> None:
        if not self.state.request_slots.acquire(blocking=False):
            # Do not create an unbounded thread backlog for a physical-device bridge.
            self.request.close()
            return
        try:
            super().handle_one_request()
        finally:
            self.state.request_slots.release()

    def _json(self, status: HTTPStatus, payload: dict[str, Any]) -> None:
        data = _bounded_json(payload)
        if data is None:
            status = HTTPStatus.INTERNAL_SERVER_ERROR
            data = _bounded_json(
                {
                    "ok": False,
                    "error": f"response exceeds {MAX_JSON_RESPONSE_BYTES} byte limit",
                },
            )
            assert data is not None
        self.send_response(status.value)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        if status == HTTPStatus.UNAUTHORIZED:
            self.send_header("WWW-Authenticate", 'Bearer realm="camera-autopilot-usb-bridge"')
        self._cors_headers()
        self.end_headers()
        self.wfile.write(data)

    def _html(self, status: HTTPStatus, body: str) -> None:
        data = body.encode("utf-8")
        self.send_response(status.value)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self._cors_headers()
        self.end_headers()
        self.wfile.write(data)

    def _request_origin(self) -> str | None:
        headers = getattr(self, "headers", {})
        origin = headers.get("Origin")
        return origin.strip().rstrip("/") if origin else None

    def _allowed_request_origin(self) -> str | None:
        origin = self._request_origin()
        if origin and origin in self.state.cors_origins:
            return origin
        return None

    def _cors_headers(self) -> None:
        origin = self._allowed_request_origin()
        if not origin:
            return
        self.send_header("Access-Control-Allow-Origin", origin)
        self.send_header("Vary", "Origin")
        self.send_header("Access-Control-Allow-Methods", "GET, POST")
        self.send_header("Access-Control-Allow-Headers", "Authorization, Content-Type, X-API-Key")
        self.send_header("Access-Control-Expose-Headers", "X-Artifact-Dir")

    def do_OPTIONS(self) -> None:
        origin = self._request_origin()
        if not origin or origin not in self.state.cors_origins:
            self._json(HTTPStatus.FORBIDDEN, {"ok": False, "error": "CORS origin is not allowed"})
            return
        requested_method = self.headers.get("Access-Control-Request-Method", "").strip().upper()
        if requested_method and requested_method not in ALLOWED_CORS_METHODS:
            self._json(HTTPStatus.METHOD_NOT_ALLOWED, {"ok": False, "error": "CORS method is not allowed"})
            return
        requested_headers = {
            header.strip().lower()
            for header in self.headers.get("Access-Control-Request-Headers", "").split(",")
            if header.strip()
        }
        if requested_headers - ALLOWED_CORS_HEADERS:
            self._json(HTTPStatus.FORBIDDEN, {"ok": False, "error": "CORS request header is not allowed"})
            return
        self.send_response(HTTPStatus.NO_CONTENT.value)
        self._cors_headers()
        self.end_headers()

    def _presented_api_tokens(self) -> list[str]:
        tokens: list[str] = []
        headers = getattr(self, "headers", {})
        authorization = headers.get("Authorization", "")
        scheme, separator, credential = authorization.partition(" ")
        if separator and scheme.lower() == "bearer" and credential.strip():
            tokens.append(credential.strip())
        api_key = headers.get("X-API-Key")
        if api_key and api_key.strip():
            tokens.append(api_key.strip())
        return tokens

    def _is_authenticated(self) -> bool:
        if self.state.allow_unauthenticated_lab:
            return True
        expected = self.state.api_token
        if not expected:
            return False
        expected_bytes = expected.encode("utf-8")
        return any(
            hmac.compare_digest(candidate.encode("utf-8"), expected_bytes)
            for candidate in self._presented_api_tokens()
        )

    def _require_auth(self) -> bool:
        if self._is_authenticated():
            return True
        self._json(
            HTTPStatus.UNAUTHORIZED,
            {"ok": False, "error": "authentication required"},
        )
        return False

    def _read_json_body(self) -> dict[str, Any]:
        raw_length = self.headers.get("Content-Length", "0") or "0"
        try:
            length = int(raw_length)
        except ValueError as exc:
            raise ValueError("invalid Content-Length") from exc
        if length <= 0:
            return {}
        if length > MAX_REQUEST_BODY_BYTES:
            raise ValueError(f"request body exceeds {MAX_REQUEST_BODY_BYTES} byte limit")
        raw = self.rfile.read(length)
        if len(raw) != length:
            raise ValueError("incomplete request body")
        try:
            body = json.loads(raw.decode("utf-8"))
        except json.JSONDecodeError as exc:
            raise ValueError(f"invalid JSON body: {exc}") from exc
        if not isinstance(body, dict):
            raise ValueError("JSON body must be an object")
        return body

    def do_GET(self) -> None:
        path = urlsplit(self.path).path
        try:
            if path not in PUBLIC_PATHS and not self._require_auth():
                return
            if path == "/":
                self._handle_index()
            elif path == "/health":
                self._handle_health()
            elif path == "/ready":
                self._handle_readiness()
            elif path == "/settings":
                self._handle_settings()
            elif path.startswith("/settings/"):
                self._handle_setting_read(unquote(path[len("/settings/") :]))
            elif path == "/preview.jpg":
                self._handle_preview()
            else:
                self._json(HTTPStatus.NOT_FOUND, {"ok": False, "error": "not found"})
        except Exception as exc:  # proof server: fail-safe and log error to client
            self._json(HTTPStatus.INTERNAL_SERVER_ERROR, {"ok": False, "error": str(exc)})

    def _handle_index(self) -> None:
        self._html(
            HTTPStatus.OK,
            """<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>A7 II USB Bridge</title>
  <style>
    :root {
      color-scheme: dark;
      --bg: #101113;
      --panel: #1b1d20;
      --text: #f3f4f6;
      --muted: #aeb4bd;
      --line: #343840;
      --accent: #43d17a;
      --warn: #f1b84b;
    }
    * { box-sizing: border-box; }
    body {
      margin: 0;
      background: var(--bg);
      color: var(--text);
      font: 15px/1.35 system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
    }
    main { max-width: 900px; margin: 0 auto; padding: 16px; }
    header { display: flex; align-items: baseline; justify-content: space-between; gap: 12px; }
    h1 { font-size: 22px; margin: 0 0 12px; font-weight: 720; }
    .status { color: var(--muted); font-size: 13px; }
    .preview {
      width: 100%;
      aspect-ratio: 3 / 2;
      background: #050608;
      border: 1px solid var(--line);
      object-fit: contain;
      display: block;
    }
    .toolbar {
      display: grid;
      grid-template-columns: repeat(auto-fit, minmax(116px, 1fr));
      gap: 8px;
      margin: 12px 0;
    }
    button, input {
      min-height: 42px;
      border: 1px solid var(--line);
      border-radius: 6px;
      background: var(--panel);
      color: var(--text);
      font: inherit;
      padding: 8px 10px;
    }
    button { cursor: pointer; font-weight: 680; }
    button.primary { border-color: #2f8f56; background: #163721; }
    button.warn { border-color: #8a6424; background: #302515; }
    .setting {
      display: grid;
      grid-template-columns: 1fr 96px 120px;
      gap: 8px;
      align-items: stretch;
      margin: 0 0 12px;
    }
    pre {
      min-height: 150px;
      max-height: 42vh;
      overflow: auto;
      padding: 12px;
      border: 1px solid var(--line);
      border-radius: 6px;
      background: var(--panel);
      color: var(--muted);
      white-space: pre-wrap;
      word-break: break-word;
      margin: 0;
    }
    @media (max-width: 560px) {
      main { padding: 12px; }
      .setting { grid-template-columns: 1fr; }
    }
  </style>
</head>
<body>
  <main>
    <header>
      <h1>A7 II USB Bridge</h1>
      <div class="status" id="status">idle</div>
    </header>
    <p>USB bridge preview is unavailable.</p>
    <div class="toolbar">
      <button data-action="health">Service Health</button>
      <button data-action="ready">Camera Ready</button>
      <button data-action="settings">Settings</button>
      <button data-action="readIso">Read ISO</button>
      <button data-action="focus">Focus</button>
    </div>
    <div class="setting">
      <input id="apiToken" type="password" autocomplete="off" placeholder="Bridge API token"
             aria-label="bridge API token">
      <button data-action="rememberToken">Use Token</button>
    </div>
    <div class="setting">
      <input id="settingId" value="camera.iso" aria-label="setting id">
      <input id="settingValue" value="800" aria-label="setting value">
      <button data-action="dryRun">Dry Run</button>
    </div>
    <pre id="log"></pre>
  </main>
  <script>
    const statusEl = document.getElementById("status");
    const logEl = document.getElementById("log");
    const apiTokenEl = document.getElementById("apiToken");
    const settingIdEl = document.getElementById("settingId");
    const settingValueEl = document.getElementById("settingValue");
    let apiToken = "";

    function setStatus(text) { statusEl.textContent = text; }
    function show(value) {
      logEl.textContent = typeof value === "string" ? value : JSON.stringify(value, null, 2);
    }
    function authHeaders(extra = {}) {
      if (!apiToken) return extra;
      return {...extra, "Authorization": "Bearer " + apiToken};
    }
    async function getJson(path) {
      setStatus("loading " + path);
      const response = await fetch(path, {headers: authHeaders()});
      const data = await response.json();
      setStatus(response.ok ? "ok" : "error");
      show(data);
    }
    async function postJson(path, body) {
      setStatus("posting " + path);
      const response = await fetch(path, {
        method: "POST",
        headers: authHeaders({"Content-Type": "application/json"}),
        body: JSON.stringify(body)
      });
      const data = await response.json();
      setStatus(response.ok ? "ok" : "error");
      show(data);
    }
    document.addEventListener("click", (event) => {
      const button = event.target.closest("button[data-action]");
      if (!button) return;
      const action = button.dataset.action;
      if (action === "rememberToken") {
        apiToken = apiTokenEl.value;
        apiTokenEl.value = "";
        setStatus(apiToken ? "token loaded" : "token cleared");
      }
      if (action === "health") getJson("/health");
      if (action === "ready") getJson("/ready");
      if (action === "settings") getJson("/settings");
      if (action === "readIso") getJson("/settings/camera.iso");
      if (action === "focus") postJson("/focus/af-point", {x: 320, y: 240, focus_area: "0x102"});
      if (action === "dryRun") {
        postJson("/settings/" + encodeURIComponent(settingIdEl.value), {
          value: settingValueEl.value,
          dry_run: true
        });
      }
    });
  </script>
</body>
</html>
""",
        )

    def do_POST(self) -> None:
        path = urlsplit(self.path).path
        try:
            if not self._require_auth():
                return
            if path == "/capture":
                # Tombstone before parsing caller-controlled content or allocating an artifact.
                self._handle_capture({})
                return
            body = self._read_json_body()
            if path == "/focus/af-point":
                self._handle_af_point(body)
            elif path.startswith("/settings/"):
                self._handle_setting_write(unquote(path[len("/settings/") :]), body)
            else:
                self._json(HTTPStatus.NOT_FOUND, {"ok": False, "error": "not found"})
        except ValueError as exc:
            self._json(HTTPStatus.BAD_REQUEST, {"ok": False, "error": str(exc)})
        except Exception as exc:
            self._json(HTTPStatus.INTERNAL_SERVER_ERROR, {"ok": False, "error": str(exc)})

    def _handle_health(self) -> None:
        self._json(
            HTTPStatus.OK,
            {
                "ok": True,
                "service": "usb-ptp-bridge",
                "status": "alive",
            },
        )

    def _handle_readiness(self) -> None:
        out = self.state.request_dir("readiness")
        with _camera_lock:
            auto = _run(["gphoto2", "--auto-detect"], timeout=10)
        (out / "gphoto-auto-detect.json").write_text(json.dumps(auto, indent=2), encoding="utf-8")
        detected = "Sony ILCE-7M2" in auto["stdout"]
        self._json(
            HTTPStatus.OK if detected else HTTPStatus.SERVICE_UNAVAILABLE,
            {
                "ok": detected,
                "camera_detected": detected,
                "artifact_dir": self.state.label(out),
                "stdout": auto["stdout"],
            },
        )

    def _handle_settings(self) -> None:
        out = self.state.request_dir("settings")
        registry = settings_registry.load_registry()
        usb_rows = [
            _row_bridge_summary(row)
            for row in registry["settings"]
            if "usb_gphoto" in row.get("routes", {})
        ]
        with _camera_lock:
            summary = _run(["gphoto2", "--summary"], timeout=20)
            focusmode = _run(["gphoto2", "--get-config", "focusmode"], timeout=10)
        (out / "gphoto-summary.json").write_text(json.dumps(summary, indent=2), encoding="utf-8")
        (out / "focusmode.json").write_text(json.dumps(focusmode, indent=2), encoding="utf-8")
        (out / "registry-usb-settings.json").write_text(
            json.dumps(usb_rows, indent=2, sort_keys=True),
            encoding="utf-8",
        )
        ok = summary["returncode"] == 0
        self._json(
            HTTPStatus.OK if ok else HTTPStatus.BAD_GATEWAY,
            {
                "ok": ok,
                "artifact_dir": self.state.label(out),
                "registry_summary": settings_registry.registry_summary(registry),
                "usb_settings": usb_rows,
                "summary": summary["stdout"],
                "focusmode": focusmode["stdout"],
                "errors": summary["stderr"] + focusmode["stderr"],
            },
        )

    def _handle_setting_read(self, setting_id: str) -> None:
        out = self.state.request_dir(f"settings-read-{setting_id}")
        try:
            row = settings_registry.get_setting(setting_id)
        except settings_registry.SettingNotFoundError as exc:
            self._json(HTTPStatus.NOT_FOUND, {"ok": False, "error": str(exc)})
            return
        usb = row.get("routes", {}).get("usb_gphoto", {})
        node = usb.get("node")
        if not node:
            self._json(
                HTTPStatus.NOT_IMPLEMENTED,
                {
                    "ok": False,
                    "setting": _row_bridge_summary(row),
                    "error": "setting has no registered USB/gphoto node",
                    "next_action": row["next_action"],
                },
            )
            return

        with _camera_lock:
            result = _run(["gphoto2", "--get-config", str(node)], timeout=10)
        (out / "gphoto-get-config.json").write_text(json.dumps(result, indent=2), encoding="utf-8")
        ok = result["returncode"] == 0
        self._json(
            HTTPStatus.OK if ok else HTTPStatus.BAD_GATEWAY,
            {
                "ok": ok,
                "artifact_dir": self.state.label(out),
                "setting": _row_bridge_summary(row),
                "current": {
                    "value": _current_from_gphoto(result["stdout"]),
                    "source": "usb_gphoto",
                    "node": node,
                    "raw": result["stdout"],
                },
                "stderr": result["stderr"],
            },
        )

    def _handle_setting_write(self, setting_id: str, body: dict[str, Any]) -> None:
        out = self.state.request_dir(f"settings-write-{setting_id}")
        value = body.get("value")
        dry_run = bool(body.get("dry_run", True))
        allow_behavior_probe = bool(body.get("allow_behavior_probe", False))
        try:
            row = settings_registry.get_setting(setting_id)
        except settings_registry.SettingNotFoundError as exc:
            self._json(HTTPStatus.NOT_FOUND, {"ok": False, "error": str(exc)})
            return

        guarded = row["status"] == "dangerous_admin" or setting_id in settings_registry.DESTRUCTIVE_SETTING_IDS
        if not dry_run and guarded:
            self._json(
                HTTPStatus.FORBIDDEN,
                {
                    "ok": False,
                    "artifact_dir": self.state.label(out),
                    "dry_run": dry_run,
                    "setting": _row_bridge_summary(row),
                    "control_plan": None,
                    "applied": None,
                    "error": "destructive/admin setting is guarded",
                },
            )
            return

        try:
            control_preview = settings_control.apply_setting(
                setting_id,
                value,
                route="usb_gphoto",
                dry_run=True,
                allow_behavior_probe=allow_behavior_probe,
            )
        except settings_registry.RegistryError as exc:
            self._json(HTTPStatus.BAD_REQUEST, {"ok": False, "error": str(exc)})
            return
        control_plan = control_preview["control_plan"]

        payload = {
            "ok": True,
            "artifact_dir": self.state.label(out),
            "dry_run": dry_run,
            "allow_behavior_probe": allow_behavior_probe,
            "setting": _row_bridge_summary(row),
            "control_plan": control_plan,
            "applied": None,
        }
        (out / "plan.json").write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")
        if dry_run:
            self._json(HTTPStatus.OK, payload)
            return

        if control_plan["guarded"]:
            payload["ok"] = False
            payload["error"] = "destructive/admin setting is guarded"
            self._json(HTTPStatus.FORBIDDEN, payload)
            return
        if value is None:
            payload["ok"] = False
            payload["error"] = "non-dry-run setting writes require a JSON 'value'"
            self._json(HTTPStatus.BAD_REQUEST, payload)
            return
        usb = row.get("routes", {}).get("usb_gphoto", {})
        node = usb.get("node")
        route_status = usb.get("status")
        if not node or route_status not in USB_BRIDGE_WRITABLE_ROUTE_STATUSES:
            payload["ok"] = False
            payload["error"] = "setting has no bridge-approved USB/gphoto setter"
            payload["next_action"] = row["next_action"]
            self._json(HTTPStatus.NOT_IMPLEMENTED, payload)
            return
        if control_plan.get("requires_behavior_proof") and not allow_behavior_probe:
            payload["ok"] = False
            payload["error"] = "non-proven USB/gphoto writes require allow_behavior_probe=true"
            payload["next_action"] = row["next_action"]
            self._json(HTTPStatus.CONFLICT, payload)
            return

        with _camera_lock:
            write_result = _run(["gphoto2", "--set-config", f"{node}={value}"], timeout=12)
            readback = _run(["gphoto2", "--get-config", str(node)], timeout=10)
        (out / "gphoto-set-config.json").write_text(json.dumps(write_result, indent=2), encoding="utf-8")
        (out / "gphoto-readback.json").write_text(json.dumps(readback, indent=2), encoding="utf-8")
        ok = write_result["returncode"] == 0
        payload["ok"] = ok
        payload["applied"] = {
            "route": "usb_gphoto",
            "node": node,
            "value": str(value),
            "write": write_result,
            "readback": {
                "value": _current_from_gphoto(readback["stdout"]),
                "raw": readback["stdout"],
                "stderr": readback["stderr"],
            },
        }
        self._json(HTTPStatus.OK if ok else HTTPStatus.BAD_GATEWAY, payload)

    def _handle_preview(self) -> None:
        # The legacy CLI writes an unbounded file before returning. Do not invoke
        # it or create an artifact directory until a pre-write bound is proven.
        self._json(HTTPStatus.GONE, {
            "ok": False,
            "error": "USB bridge preview is disabled: bounded pre-write capture is unverified",
        })

    def _handle_af_point(self, body: dict[str, Any]) -> None:
        out = self.state.request_dir("focus-af-point")
        x = int(body.get("x", 320))
        y = int(body.get("y", 240))
        focus_area = str(body.get("focus_area", "0x102"))
        with _camera_lock:
            set_af = _run(["gphoto2", "--set-config", "focusmode=Automatic"], timeout=10)
            raw = _run(
                [
                    self.state.raw_ptp_python,
                    "tests/probe_raw_control.py",
                    "--focus-area",
                    focus_area,
                    "--w1",
                    "--af-x",
                    str(x),
                    "--af-y",
                    str(y),
                    "--no-precondition",
                ],
                timeout=20,
            )
            autofocus = _run(["gphoto2", "--set-config", "autofocus=1"], timeout=10)
            indication = _run(["gphoto2", "--get-config", "focusindication"], timeout=10)
        for name, result in {
            "set-focusmode.json": set_af,
            "raw-af-point.json": raw,
            "autofocus.json": autofocus,
            "focusindication.json": indication,
        }.items():
            (out / name).write_text(json.dumps(result, indent=2), encoding="utf-8")
        ok = all(
            result["returncode"] == 0
            for result in (set_af, raw, autofocus, indication)
        )
        self._json(
            HTTPStatus.OK if ok else HTTPStatus.BAD_GATEWAY,
            {
                "ok": ok,
                "artifact_dir": self.state.label(out),
                "x": x,
                "y": y,
                "focus_area": focus_area,
                "raw_stdout": raw["stdout"],
                "focusindication": indication["stdout"],
                "errors": set_af["stderr"] + raw["stderr"] + autofocus["stderr"] + indication["stderr"],
            },
        )

    def _handle_capture(self, body: dict[str, Any]) -> None:
        self._json(
            HTTPStatus.GONE,
            {
                "ok": False,
                "error": "capture is disabled: gphoto2 %C cannot prove a JPEG-only artifact",
            },
        )


class BridgeServer(ThreadingHTTPServer):
    def __init__(self, server_address: tuple[str, int], handler_cls: type[Handler], state: BridgeState):
        host, _port = server_address
        if not _is_loopback_host(host):
            raise ValueError("USB/PTP bridge is permanently loopback-only; use an authenticated HTTPS proxy")
        if not state.api_token:
            raise ValueError("API token is required")
        if len(state.api_token) < MIN_API_TOKEN_LENGTH:
            raise ValueError(f"API token must be at least {MIN_API_TOKEN_LENGTH} characters")
        super().__init__(server_address, handler_cls)
        self.state = state

    def get_request(self):
        request, address = super().get_request()
        request.settimeout(15.0)
        return request, address


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--artifact-root", type=Path, default=DEFAULT_ARTIFACT_ROOT)
    parser.add_argument("--raw-ptp-python", default=os.environ.get("PYTHON", DEFAULT_RAW_PTP_PYTHON))
    parser.add_argument(
        "--cors-origin",
        action="append",
        default=[],
        help="Exact trusted browser origin; repeat for multiple origins. CORS is disabled by default.",
    )
    parser.add_argument(
        "--api-token",
        default=os.environ.get(DEFAULT_API_TOKEN_ENV),
        help=f"API token (prefer the {DEFAULT_API_TOKEN_ENV} environment variable)",
    )
    parser.add_argument(
        "--lab-allow-unauthenticated",
        action="store_true",
        help="disabled legacy option; physical camera routes always require a token",
    )
    args = parser.parse_args()

    if not _is_loopback_host(args.host):
        parser.error("--host must be loopback; expose only through an authenticated HTTPS proxy")
    if args.lab_allow_unauthenticated:
        parser.error("--lab-allow-unauthenticated is disabled; physical camera routes always require a token")
    api_token = args.api_token.strip() if args.api_token else None
    if not api_token:
        parser.error(
            f"set {DEFAULT_API_TOKEN_ENV} (at least {MIN_API_TOKEN_LENGTH} characters)"
        )
    if api_token and len(api_token) < MIN_API_TOKEN_LENGTH:
        parser.error(f"API token must be at least {MIN_API_TOKEN_LENGTH} characters")
    try:
        state = BridgeState(
            args.artifact_root,
            args.raw_ptp_python,
            cors_origins=args.cors_origin,
            api_token=api_token,
            allow_unauthenticated_lab=False,
        )
    except ValueError as exc:
        parser.error(str(exc))
    server = BridgeServer((args.host, args.port), Handler, state)
    print(f"USB/PTP bridge listening on http://{args.host}:{args.port}")
    print(f"Artifacts: {state.artifact_root}")
    print(
        "Authentication: "
        + "required"
    )
    print("CORS origins: " + (", ".join(state.cors_origins) if state.cors_origins else "disabled"))
    server.serve_forever()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
