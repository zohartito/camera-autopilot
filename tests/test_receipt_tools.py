"""Tests for the human-gated receipt tools (tools/receipts/*).

Hardware-free and network-free beyond a local scripted http.server fixture. No
real camera or server is touched. Verifies: each tool's --help exits 0; body_press
passes on a scripted press sequence and fails when no frame arrives; summary
renders; and no token string ever appears in captured stdout.
"""
from __future__ import annotations

import hashlib
import io
import json
import sys
import threading
from contextlib import redirect_stdout
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
RECEIPTS = ROOT / "tools" / "receipts"
sys.path.insert(0, str(RECEIPTS))

import body_press  # noqa: E402
import wifi_session  # noqa: E402
import night_session  # noqa: E402
import beginner_session  # noqa: E402
import summary as summary_tool  # noqa: E402

TOKEN = "SECRET-TOKEN-do-not-log-abc123"


# --------------------------------------------------------------------------- #
# Scripted fake server
# --------------------------------------------------------------------------- #
def _frame(index: int) -> dict:
    data = f"jpeg-bytes-frame-{index}".encode()
    return {
        "data": data,
        "record": {
            "index": index,
            "jpeg": f"/tmp/handheld/frame_{index:04d}.jpg",
            "apply_status": "applied",
            "applied": {"iso": "400", "aperture": "2.8", "shutter": "1/125"},
            "settings_for_next_frame": {"iso": "400", "aperture": "2.8", "shutter": "1/200"},
            "why": ["metered from the pushed JPEG"],
            "receipt": {
                "path": f"/tmp/handheld/frame_{index:04d}.jpg",
                "bytes": len(data),
                "sha256": hashlib.sha256(data).hexdigest(),
                "exif": {"iso": 400, "aperture": 2.8, "shutter": 0.008,
                         "camera_model": "Sony Alpha-A7 III (PC Control)"},
            },
        },
    }


class _Scenario:
    def __init__(self, *, n_frames: int):
        self.frames = [_frame(i) for i in range(n_frames)]
        self.cancelled = False


def _make_handler(scenario: _Scenario):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):  # silence
            pass

        def _send(self, code, payload):
            body = json.dumps(payload).encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _send_bytes(self, code, data):
            self.send_response(code)
            self.send_header("Content-Type", "image/jpeg")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def _state(self):
            frames = [f["record"] for f in scenario.frames]
            return {
                "id": "sess1",
                "status": "stopped" if scenario.cancelled else "running",
                "frames": frames,
                "applied_count": sum(1 for f in frames if f["apply_status"] == "applied"),
                "restored": True if scenario.cancelled else None,
                "stop_reason": "cancelled" if scenario.cancelled else None,
                "base_settings": {"iso": "100", "aperture": "4", "shutter": "1/60"},
                "summary": {"frames": len(frames)} if scenario.cancelled else None,
                "camera_model": "Sony Alpha-A7 III (PC Control)",
                "camera_port": "usb:001,004",
                "intent": None, "intent_why": None,
            }

        def do_POST(self):
            length = int(self.headers.get("Content-Length", 0))
            self.rfile.read(length)
            if self.path == "/handheld":
                self._send(202, {"id": "sess1", "status": "started"})
            elif self.path == "/handheld/sess1/cancel":
                scenario.cancelled = True
                self._send(200, {"id": "sess1", "cancel_requested": True, "status": "stopped"})
            else:
                self._send(404, {"detail": "not found"})

        def do_GET(self):
            if self.path == "/handheld/sess1":
                self._send(200, self._state())
            elif self.path.startswith("/handheld/sess1/frames/"):
                name = self.path.rsplit("/", 1)[-1]
                for f in scenario.frames:
                    if Path(f["record"]["receipt"]["path"]).name == name:
                        self._send_bytes(200, f["data"])
                        return
                self._send(404, {"detail": "frame not found"})
            else:
                self._send(404, {"detail": "not found"})

    return Handler


@pytest.fixture
def token_file(tmp_path) -> str:
    path = tmp_path / "token"
    path.write_text(TOKEN, encoding="utf-8")
    return str(path)


def _serve(scenario: _Scenario):
    server = ThreadingHTTPServer(("127.0.0.1", 0), _make_handler(scenario))
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server, f"http://127.0.0.1:{server.server_address[1]}"


# --------------------------------------------------------------------------- #
# --help exits 0 for every tool
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("module", [body_press, wifi_session, night_session,
                                    beginner_session, summary_tool])
def test_help_exits_zero(module):
    with redirect_stdout(io.StringIO()):
        with pytest.raises(SystemExit) as exc:
            module.run(["--help"])
    assert exc.value.code == 0


# --------------------------------------------------------------------------- #
# body_press: passes on a scripted press sequence
# --------------------------------------------------------------------------- #
def test_body_press_passes_on_scripted_frames(token_file, tmp_path):
    scenario = _Scenario(n_frames=3)
    server, base_url = _serve(scenario)
    try:
        buf = io.StringIO()
        with redirect_stdout(buf):
            code = body_press.run([
                "--model", "Sony Alpha-A7 III (PC Control)", "--port", "usb:001,004", "--serial", "fixture-serial",
                "--presses", "3", "--timeout", "10", "--poll-interval", "0.05",
                "--base-url", base_url, "--token-file", token_file,
                "--out-dir", str(tmp_path / "receipts"),
            ])
    finally:
        server.shutdown()
    out = buf.getvalue()
    assert code == 0, out
    assert "body-press: PASS" in out
    receipts = list((tmp_path / "receipts").glob("*.json"))
    assert receipts, "a receipt file should have been written"
    payload = json.loads(receipts[0].read_text())
    assert payload["passed"] is True
    assert payload["frames_metered"] == 3
    assert all(f["download"]["hash_verified"] for f in payload["frames"])


def test_body_press_fails_when_no_frame_arrives(token_file, tmp_path):
    scenario = _Scenario(n_frames=0)
    server, base_url = _serve(scenario)
    try:
        buf = io.StringIO()
        with redirect_stdout(buf):
            code = body_press.run([
                "--model", "Sony Alpha-A7 III (PC Control)", "--port", "usb:001,004", "--serial", "fixture-serial",
                "--presses", "3", "--timeout", "1", "--poll-interval", "0.05",
                "--base-url", base_url, "--token-file", token_file,
                "--out-dir", str(tmp_path / "receipts"),
            ])
    finally:
        server.shutdown()
    out = buf.getvalue()
    assert code == 1, out
    assert "body-press: FAIL" in out
    assert "no frame arrived" in out


# --------------------------------------------------------------------------- #
# summary renders a markdown table from receipts
# --------------------------------------------------------------------------- #
def test_summary_renders_table(tmp_path):
    directory = tmp_path / "receipts"
    directory.mkdir()
    (directory / "body-press-20260907T000000Z.json").write_text(json.dumps({
        "what": "body-press", "recorded_at": "2026-09-07T00:00:00+00:00",
        "passed": True, "session_id": "sess1",
        "frames": [{"download": {"name": "frame_0000.jpg"}}],
    }))
    (directory / "night-session-20260907T010000Z.json").write_text(json.dumps({
        "what": "night-session", "recorded_at": "2026-09-07T01:00:00+00:00",
        "passed": False, "published_image": "night_0001.jpg",
    }))
    buf = io.StringIO()
    with redirect_stdout(buf):
        code = summary_tool.run(["--out-dir", str(directory)])
    out = buf.getvalue()
    assert code == 0
    assert "| What | When | Result |" in out
    assert "body-press" in out and "PASS" in out
    assert "night-session" in out and "FAIL" in out
    assert "1/2 receipts passing" in out


# --------------------------------------------------------------------------- #
# no token string ever appears in captured stdout
# --------------------------------------------------------------------------- #
def test_no_token_in_stdout(token_file, tmp_path):
    scenario = _Scenario(n_frames=2)
    server, base_url = _serve(scenario)
    try:
        buf = io.StringIO()
        with redirect_stdout(buf):
            body_press.run([
                "--model", "Sony Alpha-A7 III (PC Control)", "--port", "usb:001,004", "--serial", "fixture-serial",
                "--presses", "2", "--timeout", "10", "--poll-interval", "0.05",
                "--base-url", base_url, "--token-file", token_file,
                "--out-dir", str(tmp_path / "receipts"),
            ])
    finally:
        server.shutdown()
    out = buf.getvalue()
    assert TOKEN not in out
    # And the token must not leak into the written receipt either.
    for receipt in (tmp_path / "receipts").glob("*.json"):
        assert TOKEN not in receipt.read_text()


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q"]))


@pytest.mark.parametrize("module", [body_press, wifi_session, night_session, beginner_session])
@pytest.mark.parametrize("overrides", [False, True])
def test_receipt_help_defaults_match_launcher(module, overrides, tmp_path, monkeypatch, capsys):
    """CAM-06: resolving defaults never reads the owner's token store."""
    from backend import api_token_store
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)
    monkeypatch.delenv("CAMERA_AUTOPILOT_TOKEN_FILE", raising=False)
    monkeypatch.delenv("CAMERA_AUTOPILOT_PORT", raising=False)
    token_path = str(api_token_store.default_store_path())
    port = "8000"
    if overrides:
        token_path = str(tmp_path / "explicit-token")
        port = "8765"
        monkeypatch.setenv("CAMERA_AUTOPILOT_TOKEN_FILE", token_path)
        monkeypatch.setenv("CAMERA_AUTOPILOT_PORT", port)
    with pytest.raises(SystemExit) as result:
        module.run(["--help"])
    assert result.value.code == 0
    # argparse wraps long paths: compare after collapsing whitespace only.
    out = "".join(capsys.readouterr().out.split())
    assert "".join(token_path.split()) in out
    assert f"http://127.0.0.1:{port}" in out
