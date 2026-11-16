"""Shared helpers for the human-gated acceptance receipts.

Every receipt tool talks ONLY to the running Camera Autopilot server over
``--base-url`` with the bearer token read from ``--token-file``. The token is
sent in the ``X-Camera-Autopilot-Token`` header and is NEVER placed in a URL,
printed, or written into a receipt. Receipts land as JSON under
``artifacts/falsification/human-gated-20260907/`` (gitignored) next to a printed
pass/fail summary.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

TOKEN_HEADER = "X-Camera-Autopilot-Token"
DEFAULT_OUT_DIR = (
    Path(__file__).resolve().parents[2]
    / "artifacts"
    / "falsification"
    / "human-gated-20260907"
)
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "backend"))
from api_token_store import default_store_path


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def read_token(token_file: str) -> str:
    """Read the private API token from a local file; fail loudly if it is missing."""
    path = Path(token_file).expanduser()
    try:
        token = path.read_text(encoding="utf-8").strip()
    except OSError as exc:
        raise SystemExit(
            f"Could not read the API token from {path}. Start the server first, "
            f"then pass --token-file <path>. ({type(exc).__name__})"
        ) from exc
    if not token:
        raise SystemExit(f"The token file {path} is empty; start the server to populate it.")
    return token


class Client:
    """Minimal JSON HTTP client. Sends the token only as a header, never in the URL."""

    def __init__(self, base_url: str, token: str, timeout: float = 30.0):
        self.base_url = base_url.rstrip("/")
        self._token = token
        self.timeout = timeout

    def _headers(self, extra: dict | None = None) -> dict:
        headers = {TOKEN_HEADER: self._token, "Accept": "application/json"}
        if extra:
            headers.update(extra)
        return headers

    def request(self, method: str, path: str, body: dict | None = None,
                timeout: float | None = None) -> tuple[int, object]:
        """Return (status_code, parsed_json_or_text). Never raises on HTTP errors."""
        if not path.startswith("/"):
            path = "/" + path
        url = self.base_url + path
        data = None
        headers = self._headers()
        if body is not None:
            data = json.dumps(body).encode("utf-8")
            headers["Content-Type"] = "application/json"
            headers["Content-Length"] = str(len(data))
        req = urllib.request.Request(url, data=data, headers=headers, method=method)
        try:
            with urllib.request.urlopen(req, timeout=timeout or self.timeout) as resp:
                raw = resp.read()
                return resp.status, _parse(raw)
        except urllib.error.HTTPError as exc:
            raw = exc.read()
            return exc.code, _parse(raw)
        except urllib.error.URLError as exc:
            # Reason may carry the base URL but never the token (token is header-only).
            raise SystemExit(
                f"Could not reach the server at {self.base_url}. Is it running? "
                f"({type(exc).__name__}: {exc.reason})"
            ) from exc

    def get(self, path: str, timeout: float | None = None) -> tuple[int, object]:
        return self.request("GET", path, timeout=timeout)

    def post(self, path: str, body: dict | None = None,
             timeout: float | None = None) -> tuple[int, object]:
        return self.request("POST", path, body=body, timeout=timeout)

    def get_bytes(self, path: str, timeout: float | None = None) -> tuple[int, bytes]:
        """Download raw bytes (used for hash-verifying a downloaded frame/artifact)."""
        if not path.startswith("/"):
            path = "/" + path
        url = self.base_url + path
        req = urllib.request.Request(url, headers=self._headers(), method="GET")
        try:
            with urllib.request.urlopen(req, timeout=timeout or self.timeout) as resp:
                return resp.status, resp.read()
        except urllib.error.HTTPError as exc:
            return exc.code, exc.read()
        except urllib.error.URLError as exc:
            raise SystemExit(
                f"Could not reach the server at {self.base_url}. Is it running? "
                f"({type(exc).__name__}: {exc.reason})"
            ) from exc


def _parse(raw: bytes):
    try:
        return json.loads(raw.decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        return raw.decode("utf-8", "replace")


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def add_common_args(parser: argparse.ArgumentParser) -> None:
    default_base_url = f"http://127.0.0.1:{os.environ.get('CAMERA_AUTOPILOT_PORT', '8000')}"
    token_file = os.environ.get("CAMERA_AUTOPILOT_TOKEN_FILE") or str(default_store_path())
    parser.add_argument("--base-url", default=default_base_url,
                        help=f"running server base URL (default {default_base_url})")
    parser.add_argument("--token-file", default=token_file,
                        help=f"file holding the API token (default {token_file})")
    parser.add_argument("--out-dir", default=str(DEFAULT_OUT_DIR),
                        help="directory for the JSON receipt (default: gitignored falsification dir)")


def write_receipt(out_dir: str, name: str, payload: dict) -> Path:
    """Write a timestamped JSON receipt and return its path. Never contains the token."""
    directory = Path(out_dir).expanduser()
    directory.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    path = directory / f"{name}-{stamp}.json"
    path.write_text(json.dumps(payload, indent=2, sort_keys=True, default=str), encoding="utf-8")
    return path


def print_summary(*, what: str, passed: bool, receipt_path: Path, lines: list[str]) -> None:
    verdict = "PASS" if passed else "FAIL"
    print("")
    print(f"===== {what}: {verdict} =====")
    for line in lines:
        print(f"  {line}")
    print(f"  receipt: {receipt_path}")
    print("=" * (14 + len(what) + len(verdict)))


def finish(*, what: str, passed: bool, out_dir: str, receipt_name: str,
           payload: dict, lines: list[str]) -> int:
    """Write the receipt, print the summary, and return the process exit code."""
    payload = {"what": what, "recorded_at": utc_now(), "passed": passed, **payload}
    receipt_path = write_receipt(out_dir, receipt_name, payload)
    print_summary(what=what, passed=passed, receipt_path=receipt_path, lines=lines)
    return 0 if passed else 1


def poll_until(fn, *, predicate, timeout_s: float, interval_s: float = 1.0):
    """Call fn() every interval until predicate(result) is true or timeout. Returns last result."""
    deadline = time.monotonic() + timeout_s
    result = fn()
    while not predicate(result):
        if time.monotonic() >= deadline:
            return result
        time.sleep(interval_s)
        result = fn()
    return result
