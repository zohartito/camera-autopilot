#!/usr/bin/env python3
"""Ask the running server (on this Mac, loopback only) for a fresh one-time phone pairing code.

The API token comes from CAMERA_AUTOPILOT_API_TOKEN or the per-user token file run.sh keeps; it
travels only in the request header to 127.0.0.1 and is never printed. The server issues codes only
in lan-proxy exposure; a new code replaces the previous one.

    python tools/pair_code.py --port 8123
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))

import api_token_store  # noqa: E402

TOKEN_HEADER_VALUE = re.compile(r"[\x21-\x7e]+")


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """The token header must never follow a redirect away from 127.0.0.1."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def request_code(port: int, token: str, *, opener=None) -> dict:
    opener = opener or urllib.request.build_opener(urllib.request.ProxyHandler({}), _NoRedirect())
    request = urllib.request.Request(
        f"http://127.0.0.1:{port}/api/pair/new", data=b"{}", method="POST",
        headers={"Content-Type": "application/json", "X-Camera-Autopilot-Token": token})
    with opener.open(request, timeout=10) as response:
        return json.loads(response.read(4096))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--port", type=int, default=int(os.environ.get("CAMERA_AUTOPILOT_PORT", "8000")))
    args = parser.parse_args(argv)
    token = os.environ.get("CAMERA_AUTOPILOT_API_TOKEN", "").strip()
    if not token:
        try:
            token = api_token_store.read_existing()
        except FileNotFoundError:
            print("No stored API token; start the server with ./run.sh first.", file=sys.stderr)
            return 1
        except (api_token_store.TokenStoreError, OSError) as exc:
            print(f"API token store: {exc}", file=sys.stderr)
            return 1
    # A header-invalid token would be echoed inside urllib's own error text: refuse it here, unseen.
    if not TOKEN_HEADER_VALUE.fullmatch(token):
        print("CAMERA_AUTOPILOT_API_TOKEN is not a valid header value (spaces or control characters).",
              file=sys.stderr)
        return 1
    try:
        result = request_code(args.port, token)
        code = result["code"]
        minutes = int(result.get("expires_in_seconds", 600)) // 60
    except urllib.error.HTTPError as exc:
        print(f"The server refused a new pairing code (HTTP {exc.code}).", file=sys.stderr)
        return 1
    except Exception as exc:  # noqa: BLE001 — never echo exception text: it may quote the header
        print(f"Could not get a pairing code from 127.0.0.1:{args.port} ({type(exc).__name__}).", file=sys.stderr)
        return 1
    print(f"Phone pairing code: {code}  (valid {minutes} minutes, single use)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
