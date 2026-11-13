#!/usr/bin/env python3
"""Wi-Fi Smart Remote acceptance receipt (Sony A7 II).

Preconditions the owner must have met before running this:
  1. On the camera: MENU -> Application List -> "Smart Remote Control" (the FULL
     app, not "Smart Remote Embedded"), launched so the body becomes a Wi-Fi AP.
     The exposure setters only appear with the full app and the mode dial on M.
  2. On the Mac: join the camera's "DIRECT-xxxx:ILCE-7M2" Wi-Fi network.
  3. Start the server with the Wi-Fi engine on this base URL:
        CAMERA_ENGINE=wifi ./run.sh      (default base URL http://127.0.0.1:8002)

The tool: checks TCP reachability of the camera's Smart Remote ScalarWebAPI
endpoint, then POSTs one /shoot with intent "warm" and refine=false. It writes a
receipt asserting `applied.white_balance.verified` is true and that the brain's
`recommended` exposure matches what was `applied` over Wi-Fi -- or the
fail-closed report when the camera refused to verifiably apply a setting.

Exit 0 only on an observed pass.
"""
from __future__ import annotations

import argparse
import re
import socket
import sys
from pathlib import Path
from urllib.parse import urlparse

sys.path.insert(0, str(Path(__file__).resolve().parent))
import _common as common

# Mirrors backend/wifi_a7ii.py DEFAULT_CAMERA_ENDPOINT.
DEFAULT_CAMERA_ENDPOINT = "http://192.168.122.1:8080/sony/camera"

_REC_RE = re.compile(r"ISO\s+(?P<iso>\S+)\s+f/(?P<f>\S+)\s+(?P<shutter>\S+)")


def _endpoint_reachable(endpoint: str, timeout: float) -> tuple[bool, str]:
    parsed = urlparse(endpoint)
    host = parsed.hostname
    port = parsed.port or (443 if parsed.scheme == "https" else 80)
    if not host:
        return False, f"could not parse host from {endpoint!r}"
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True, f"TCP connect to {host}:{port} succeeded"
    except OSError as exc:
        return False, f"TCP connect to {host}:{port} failed ({type(exc).__name__}: {exc})"


def _parse_recommended(recommended: str | None) -> dict | None:
    if not isinstance(recommended, str):
        return None
    match = _REC_RE.search(recommended)
    if not match:
        return None
    return {"iso": match.group("iso"), "f": match.group("f"), "shutter": match.group("shutter")}


def _recommended_matches_applied(recommended: str | None, applied: dict | None) -> tuple[bool, dict]:
    """The brain's recommended exposure should equal what verifiably stuck over Wi-Fi."""
    parsed = _parse_recommended(recommended)
    detail = {"recommended_parsed": parsed, "applied_over_wifi": applied}
    if not parsed or not isinstance(applied, dict):
        return False, detail
    comparison = {
        "iso": (str(parsed["iso"]), str(applied.get("iso"))),
        "f": (str(parsed["f"]), str(applied.get("f"))),
        "shutter": (str(parsed["shutter"]), str(applied.get("shutter"))),
    }
    detail["comparison"] = comparison
    return all(want == got for want, got in comparison.values()), detail


def run(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--camera-endpoint", default=DEFAULT_CAMERA_ENDPOINT,
                        help=f"Smart Remote ScalarWebAPI endpoint (default {DEFAULT_CAMERA_ENDPOINT})")
    parser.add_argument("--reach-timeout", type=float, default=3.0,
                        help="seconds to wait for the endpoint TCP connect (default 3)")
    parser.add_argument("--skip-reachability", action="store_true",
                        help="skip the camera endpoint reachability precheck")
    common.add_common_args(parser, default_base_url="http://127.0.0.1:8002")
    args = parser.parse_args(argv)

    token = common.read_token(args.token_file)
    client = common.Client(args.base_url, token, timeout=90.0)

    reach_ok, reach_detail = (True, "skipped")
    if not args.skip_reachability:
        reach_ok, reach_detail = _endpoint_reachable(args.camera_endpoint, args.reach_timeout)
        print(f"Smart Remote reachability: {reach_detail}")
        if not reach_ok:
            return common.finish(
                what="wifi-session", passed=False, out_dir=args.out_dir,
                receipt_name="wifi-session",
                payload={"camera_endpoint": args.camera_endpoint,
                         "reachability": {"ok": reach_ok, "detail": reach_detail}},
                lines=["FAIL CAUSE: camera Smart Remote endpoint not reachable",
                       reach_detail,
                       "Launch Smart Remote Control on the A7 II and join its DIRECT- Wi-Fi."])

    status, resp = client.post("/shoot", {"intent": "warm", "refine": False})
    if not isinstance(resp, dict):
        resp = {"raw": resp}

    # Fail-closed path: the server refuses (409) or returns ok=False when the camera
    # could not verifiably apply a requested setting before the shutter.
    fail_closed = status == 409 or resp.get("ok") is False
    settings = resp.get("settings") if isinstance(resp.get("settings"), dict) else {}
    applied_block = resp.get("applied") if isinstance(resp.get("applied"), dict) else {}
    wb = applied_block.get("white_balance")
    wb_verified = isinstance(wb, dict) and wb.get("verified") is True

    recommended = settings.get("recommended")
    applied_over_wifi = settings.get("applied_over_wifi")
    match_ok, match_detail = _recommended_matches_applied(recommended, applied_over_wifi)

    # Full-res "escape the postview" criterion: the published image is the verified 24 MP JPEG AND
    # its identity was proved by content+time (not content alone, and never a newest-card guess).
    image_kind = settings.get("image_kind")
    image_dimensions = settings.get("image_dimensions")
    full_res_status = settings.get("full_res_status")
    full_res = settings.get("full_res") if isinstance(settings.get("full_res"), dict) else {}
    identity = full_res.get("identity") if isinstance(full_res.get("identity"), dict) else {}
    identity_basis = identity.get("identity_basis")
    full_res_ok = bool(image_kind == "full_res"
                       and isinstance(identity_basis, str) and "time" in identity_basis)

    passed = bool(status == 200 and not fail_closed and wb_verified and match_ok)

    lines = [
        f"endpoint: {args.camera_endpoint}",
        f"/shoot HTTP {status}  ok={resp.get('ok')}",
        f"white_balance.verified: {wb_verified}  (wb={wb})",
        f"recommended: {recommended!r}",
        f"applied_over_wifi: {applied_over_wifi!r}",
        f"recommended vs applied match: {match_ok}",
        f"image_kind: {image_kind}  image_dimensions: {image_dimensions}",
        f"full_res_status: {full_res_status}  identity_basis: {identity_basis}",
        f"full-res criterion (full_res image + content+time identity): {full_res_ok}",
    ]
    if fail_closed:
        lines.append("FAIL-CLOSED: camera refused to verifiably apply a setting; shutter not released.")
        lines.append(f"  detail: {resp.get('why') or resp.get('detail')}")
    if status == 200 and not fail_closed and not wb_verified:
        lines.append("FAIL CAUSE: white balance was not verified")
    if status == 200 and not fail_closed and not match_ok:
        lines.append("FAIL CAUSE: recommended exposure did not match what was applied")

    return common.finish(
        what="wifi-session", passed=passed, out_dir=args.out_dir,
        receipt_name="wifi-session",
        payload={
            "camera_endpoint": args.camera_endpoint,
            "reachability": {"ok": reach_ok, "detail": reach_detail},
            "shoot_http_status": status,
            "fail_closed": fail_closed,
            "white_balance": wb,
            "white_balance_verified": wb_verified,
            "recommended": recommended,
            "applied_over_wifi": applied_over_wifi,
            "recommended_vs_applied": match_detail,
            "image_kind": image_kind,
            "image_dimensions": image_dimensions,
            "full_res_status": full_res_status,
            "full_res_identity_basis": identity_basis,
            "full_res_criterion": full_res_ok,
            "shot_summary": resp.get("summary"),
            "shot_why": resp.get("why") or resp.get("detail"),
        },
        lines=lines)


if __name__ == "__main__":
    raise SystemExit(run())
