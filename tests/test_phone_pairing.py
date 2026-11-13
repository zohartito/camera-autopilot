"""One-time phone pairing: a 6-digit code from the Mac terminal buys the API token exactly once.

The phone reaches the loopback backend only through the confirmed HTTPS proxy, which Uvicorn sees
as scheme https with the phone's LAN address as the client. A plain-HTTP LAN request is refused
before the code is even looked at. Camera-free; no server is started.
"""
from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path

import pytest

os.environ["CAMERA_ENGINE"] = "mock"
os.environ.setdefault("CAMERA_AUTOPILOT_API_TOKEN", "test-token")

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))

import app as backend_app  # noqa: E402
import pairing  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402
from security_bounds import RequestBudget  # noqa: E402

PHONE = ("192.168.1.42", 51000)
JSON = {"Content-Type": "application/json"}


class FakeClock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


@pytest.fixture
def clock(monkeypatch) -> FakeClock:
    fake = FakeClock()
    monkeypatch.setenv("CAMERA_AUTOPILOT_EXPOSURE", "lan-proxy")
    monkeypatch.setattr(backend_app, "_pairing_codes", pairing.PairingCodes(clock=fake))
    monkeypatch.setattr(backend_app, "_pairing_budget", RequestBudget(limit=10, window_seconds=60))
    return fake


def phone() -> TestClient:
    """The phone as the backend sees it behind the TLS proxy."""
    return TestClient(backend_app.app, base_url="https://testserver", client=PHONE)


def _load_pair_code():
    """Load tools/pair_code.py by path: putting tools/ on sys.path would shadow backend modules."""
    import importlib.util

    spec = importlib.util.spec_from_file_location("pair_code_tool", ROOT / "tools" / "pair_code.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def pair(client: TestClient, code):
    return client.post("/api/pair", json={"code": code})


def test_code_is_single_use_and_returns_the_token(clock) -> None:
    code = backend_app._pairing_codes.issue()
    assert len(code) == 6 and code.isdigit()
    first = pair(phone(), code)
    assert first.status_code == 200, first.text
    assert first.json() == {"token": backend_app.API_TOKEN}
    assert first.headers["cache-control"] == "no-store"
    # The token works for the camera API from the phone afterwards.
    assert phone().get("/status", headers={"X-Camera-Autopilot-Token": first.json()["token"]}).status_code == 200
    reused = pair(phone(), code)
    assert reused.status_code == 401 and reused.json() == {"detail": "pairing code not accepted"}


def test_code_expires_after_ten_minutes(clock) -> None:
    code = backend_app._pairing_codes.issue()
    clock.now += pairing.CODE_TTL_SECONDS
    assert pair(phone(), code).status_code == 401
    assert not backend_app._pairing_codes.active()


def test_five_failures_invalidate_the_code(clock) -> None:
    code = backend_app._pairing_codes.issue()
    wrong = f"{(int(code) + 1) % 10**6:06d}"
    for _ in range(pairing.MAX_FAILURES):
        response = pair(phone(), wrong)
        assert response.status_code == 401
        assert response.json() == {"detail": "pairing code not accepted"}
    # The right code no longer works: a new one needs a restart or the loopback endpoint.
    assert pair(phone(), code).status_code == 401
    assert not backend_app._pairing_codes.active()


def test_malformed_attempts_count_and_leak_nothing(clock) -> None:
    code = backend_app._pairing_codes.issue()
    client = phone()
    attempts = [
        client.post("/api/pair", json={"code": int(code)}),
        client.post("/api/pair", json={"code": code + "0"}),
        client.post("/api/pair", json=[code]),
        client.post("/api/pair", content=b"not json", headers=JSON),
        client.post("/api/pair", content=b'{"code": "' + b"1" * 400 + b'"}', headers=JSON),
    ]
    for response in attempts:
        assert response.status_code == 401, response.text
        assert response.json() == {"detail": "pairing code not accepted"}
        assert backend_app.API_TOKEN not in response.text
    assert not backend_app._pairing_codes.active(), "five malformed attempts must use up the code"


def test_non_json_posts_are_refused_before_the_code_is_touched(clock) -> None:
    code = backend_app._pairing_codes.issue()
    # A cross-site form or text/plain post (no CORS preflight) cannot redeem or burn the code.
    for headers in ({"Content-Type": "text/plain"}, {"Content-Type": "application/x-www-form-urlencoded"}):
        response = phone().post("/api/pair", content=f'{{"code":"{code}"}}'.encode(), headers=headers)
        assert response.status_code == 415
    assert phone().get("/api/pair").status_code == 405
    assert pair(phone(), code).status_code == 200


def test_global_rate_limit_answers_429(clock) -> None:
    code = backend_app._pairing_codes.issue()
    wrong = f"{(int(code) + 1) % 10**6:06d}"
    statuses = [pair(phone(), wrong).status_code for _ in range(10)]
    assert set(statuses) == {401}
    limited = pair(phone(), wrong)
    assert limited.status_code == 429
    assert int(limited.headers["retry-after"]) >= 1
    # The limit is global: a different client address is limited too, and even the right code waits.
    code = backend_app._pairing_codes.issue()
    other = TestClient(backend_app.app, base_url="https://testserver", client=("192.168.1.77", 52000))
    assert pair(other, code).status_code == 429
    assert backend_app._pairing_codes.active(), "a rate-limited attempt must not consume the code"


def test_plain_http_from_the_lan_is_refused(clock) -> None:
    code = backend_app._pairing_codes.issue()
    plain = TestClient(backend_app.app, base_url="http://testserver", client=PHONE)
    response = pair(plain, code)
    assert response.status_code == 403
    assert backend_app.API_TOKEN not in response.text
    assert backend_app._pairing_codes.active(), "a refused transport must not consume the code"
    assert pair(phone(), code).status_code == 200


def test_pairing_is_off_outside_lan_proxy_exposure(clock, monkeypatch) -> None:
    code = backend_app._pairing_codes.issue()
    monkeypatch.setenv("CAMERA_AUTOPILOT_EXPOSURE", "local")
    assert pair(phone(), code).status_code == 401
    monkeypatch.delenv("CAMERA_AUTOPILOT_EXPOSURE")
    assert pair(phone(), code).status_code == 401


def test_pairing_fails_closed_without_a_configured_token(clock, monkeypatch) -> None:
    code = backend_app._pairing_codes.issue()
    monkeypatch.setattr(backend_app, "API_TOKEN", "")
    response = pair(phone(), code)
    assert response.status_code == 401 and "token" not in response.json()


def test_new_code_needs_the_token_and_this_computer(clock) -> None:
    auth = {"X-Camera-Autopilot-Token": backend_app.API_TOKEN, **JSON}
    local = TestClient(backend_app.app)  # "testclient" counts as loopback, like 127.0.0.1
    assert local.post("/api/pair/new", json={}).status_code == 401
    assert local.post("/api/pair/new", json={}, headers={"X-Camera-Autopilot-Token": "wrong"}).status_code == 401
    # A phone holding the token still cannot mint codes over the proxy.
    assert phone().post("/api/pair/new", json={}, headers=auth).status_code == 403
    loopback = TestClient(backend_app.app, client=("127.0.0.1", 50123))
    issued = loopback.post("/api/pair/new", json={}, headers=auth)
    assert issued.status_code == 200, issued.text
    body = issued.json()
    assert set(body) == {"code", "expires_in_seconds"} and body["expires_in_seconds"] == 600
    assert issued.headers["cache-control"] == "no-store"
    # A fresh code resets the failure count and replaces the previous code.
    assert pair(phone(), body["code"]).status_code == 200


def test_new_code_refused_outside_lan_proxy(clock, monkeypatch) -> None:
    monkeypatch.setenv("CAMERA_AUTOPILOT_EXPOSURE", "local")
    auth = {"X-Camera-Autopilot-Token": backend_app.API_TOKEN}
    assert TestClient(backend_app.app).post("/api/pair/new", json={}, headers=auth).status_code == 409


def test_cross_origin_preflight_is_not_allowed(clock) -> None:
    response = phone().options("/api/pair", headers={
        "Origin": "https://evil.example", "Access-Control-Request-Method": "POST",
        "Access-Control-Request-Headers": "content-type"})
    assert "access-control-allow-origin" not in response.headers


def test_startup_prints_the_code_to_stderr_only_in_lan_proxy(clock, monkeypatch, capsys, caplog) -> None:
    asyncio.run(backend_app.issue_startup_pairing_code())
    out, err = capsys.readouterr()
    assert out == ""
    printed = [line for line in err.splitlines() if "Phone pairing code:" in line]
    assert len(printed) == 1
    code = printed[0].split("Phone pairing code:")[1].split()[0]
    assert len(code) == 6 and code.isdigit()
    assert backend_app.API_TOKEN not in err
    assert code not in caplog.text, "the code goes to the terminal, never the logging system"
    assert pair(phone(), code).status_code == 200
    monkeypatch.setenv("CAMERA_AUTOPILOT_EXPOSURE", "local")
    asyncio.run(backend_app.issue_startup_pairing_code())
    assert "Phone pairing code" not in capsys.readouterr().err
    assert not backend_app._pairing_codes.active()


def test_codes_are_uniform_six_digit_strings() -> None:
    codes = pairing.PairingCodes()
    seen = {codes.issue() for _ in range(200)}
    assert all(len(code) == 6 and code.isdigit() for code in seen)
    assert len(seen) > 150


def test_pair_code_tool_sends_the_token_only_to_loopback_and_prints_only_the_code(monkeypatch, capsys) -> None:
    pair_code = _load_pair_code()

    sent = []

    class Opener:
        def open(self, request, timeout):
            sent.append((request.full_url, request.get_method(), request.get_header("X-camera-autopilot-token")))

            class Response:
                def __enter__(self):
                    return self

                def __exit__(self, *exc):
                    return False

                def read(self, limit):
                    return b'{"code": "042042", "expires_in_seconds": 600}'
            return Response()

    real_request_code = pair_code.request_code
    monkeypatch.setattr(pair_code, "request_code", lambda port, token: real_request_code(port, token, opener=Opener()))
    monkeypatch.setenv("CAMERA_AUTOPILOT_API_TOKEN", "t" * 64)
    assert pair_code.main(["--port", "8765"]) == 0
    out, err = capsys.readouterr()
    assert sent == [("http://127.0.0.1:8765/api/pair/new", "POST", "t" * 64)]
    assert "042042" in out and "t" * 64 not in out + err


def test_pair_code_tool_never_follows_a_redirect_or_a_proxy(monkeypatch) -> None:
    import urllib.request

    pair_code = _load_pair_code()

    built = []

    class Refuse:
        def open(self, request, timeout):
            raise OSError("stop before any network access")

    def build_opener(*handlers):
        built.extend(handlers)
        return Refuse()

    monkeypatch.setattr(urllib.request, "build_opener", build_opener)
    try:
        pair_code.request_code(8765, "t" * 64)
    except OSError:
        pass
    assert any(isinstance(handler, pair_code._NoRedirect) for handler in built)
    proxy = next(handler for handler in built if isinstance(handler, urllib.request.ProxyHandler))
    assert proxy.proxies == {}
    assert pair_code._NoRedirect().redirect_request(None, None, 302, "Found", {}, "http://192.168.1.9/") is None


def test_pair_code_tool_never_echoes_a_malformed_or_failing_token(monkeypatch, capsys) -> None:
    pair_code = _load_pair_code()

    secret = "s" * 64
    monkeypatch.setenv("CAMERA_AUTOPILOT_API_TOKEN", secret + "\nextra")
    monkeypatch.setattr(pair_code, "request_code", lambda port, token: pytest.fail("must refuse before sending"))
    assert pair_code.main(["--port", "8765"]) == 1
    out, err = capsys.readouterr()
    assert secret not in out + err and "not a valid header value" in err

    def quoting_failure(port, token):
        raise ValueError(f"Invalid header value {token!r}")

    monkeypatch.setenv("CAMERA_AUTOPILOT_API_TOKEN", secret)
    monkeypatch.setattr(pair_code, "request_code", quoting_failure)
    assert pair_code.main(["--port", "8765"]) == 1
    out, err = capsys.readouterr()
    assert secret not in out + err and "ValueError" in err
