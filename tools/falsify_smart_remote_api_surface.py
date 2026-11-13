#!/usr/bin/env python3
"""Dump Sony Smart Remote service API surfaces with exact request/response artifacts.

This is a falsification harness, not a success claim. It records what each service answers so
capability-matrix rows can be flipped only from saved evidence.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

SERVICES = ("camera", "system", "avContent", "accessControl")
SAFE_CAMERA_METHODS = (
    "getAvailableApiList",
    "getEvent",
)


def _slug(value: str) -> str:
    return re.sub(r"[^A-Za-z0-9_.-]+", "-", value).strip("-") or "endpoint"


def _write_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")


def _run_local(cmd: list[str], timeout: float = 3.0) -> dict:
    started = time.time()
    try:
        proc = subprocess.run(
            cmd,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            timeout=timeout,
            check=False,
        )
        return {
            "cmd": cmd,
            "ok": proc.returncode == 0,
            "returncode": proc.returncode,
            "duration_s": round(time.time() - started, 3),
            "output": proc.stdout[-6000:],
        }
    except FileNotFoundError as exc:
        return {
            "cmd": cmd,
            "ok": False,
            "returncode": None,
            "duration_s": round(time.time() - started, 3),
            "error_type": exc.__class__.__name__,
            "output": str(exc),
        }
    except subprocess.TimeoutExpired as exc:
        output = exc.stdout if isinstance(exc.stdout, str) else ""
        return {
            "cmd": cmd,
            "ok": False,
            "returncode": None,
            "duration_s": round(time.time() - started, 3),
            "error_type": exc.__class__.__name__,
            "output": output[-6000:],
        }


def _network_snapshot(base: str, timeout: float) -> dict:
    parsed = urllib.parse.urlparse(base)
    host = parsed.hostname or ""
    snapshot = {
        "base": base,
        "host": host,
        "local_hostname": socket.gethostname(),
        "commands": {},
    }
    commands = {
        "wifi_en0": ["networksetup", "-getairportnetwork", "en0"],
        "ifconfig_en0": ["ifconfig", "en0"],
        "scutil_nwi": ["scutil", "--nwi"],
        "route_to_host": ["route", "-n", "get", host] if host else [],
        "arp": ["arp", "-an"],
        "ping_host": ["ping", "-c", "1", "-W", str(max(1000, int(timeout * 1000))), host] if host else [],
    }
    for name, cmd in commands.items():
        if cmd:
            snapshot["commands"][name] = _run_local(cmd, timeout=max(1.0, timeout + 1.0))
    return snapshot


def _request_payload(method: str, params: list | None = None) -> dict:
    return {
        "method": method,
        "params": params or [],
        "id": 1,
        "version": "1.0",
    }


def _parse_body(raw: str) -> dict | None:
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError:
        return None
    return parsed if isinstance(parsed, dict) else None


def _call_urllib(endpoint: str, payload: dict, timeout: float) -> dict:
    body = json.dumps(payload).encode()
    try:
        with urllib.request.urlopen(endpoint, data=body, timeout=timeout) as resp:
            raw = resp.read().decode("utf-8", errors="replace")
        return {
            "ok": True,
            "endpoint": endpoint,
            "request": payload,
            "http_body": raw,
            "json": _parse_body(raw),
            "transport": "urllib",
        }
    except (OSError, urllib.error.URLError, TimeoutError) as exc:
        return {
            "ok": False,
            "endpoint": endpoint,
            "request": payload,
            "error_type": exc.__class__.__name__,
            "error": str(exc),
            "transport": "urllib",
        }


def _call_curl(endpoint: str, payload: dict, timeout: float) -> dict:
    curl = shutil.which("curl")
    if not curl:
        return {
            "ok": False,
            "endpoint": endpoint,
            "request": payload,
            "error_type": "FileNotFoundError",
            "error": "curl not found",
            "transport": "curl",
        }

    started = time.time()
    try:
        proc = subprocess.run(
            [
                curl,
                "-sS",
                "--connect-timeout",
                str(timeout),
                "--max-time",
                str(max(timeout + 1.0, 3.0)),
                "-H",
                "Content-Type: application/json",
                "-d",
                json.dumps(payload),
                endpoint,
            ],
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=max(timeout + 2.0, 5.0),
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        output = exc.stdout if isinstance(exc.stdout, str) else ""
        error = exc.stderr if isinstance(exc.stderr, str) else str(exc)
        return {
            "ok": False,
            "endpoint": endpoint,
            "request": payload,
            "http_body": output,
            "error_type": exc.__class__.__name__,
            "error": error[-6000:],
            "duration_s": round(time.time() - started, 3),
            "transport": "curl",
        }

    raw = proc.stdout
    parsed = _parse_body(raw)
    if proc.returncode == 0:
        return {
            "ok": True,
            "endpoint": endpoint,
            "request": payload,
            "http_body": raw,
            "json": parsed,
            "duration_s": round(time.time() - started, 3),
            "transport": "curl",
        }
    return {
        "ok": False,
        "endpoint": endpoint,
        "request": payload,
        "http_body": raw,
        "json": parsed,
        "returncode": proc.returncode,
        "error_type": "curl",
        "error": proc.stderr.strip() or raw[-6000:],
        "duration_s": round(time.time() - started, 3),
        "transport": "curl",
    }


def _call(
    endpoint: str,
    method: str,
    params: list | None = None,
    timeout: float = 1.5,
    transport: str = "urllib",
) -> dict:
    payload = _request_payload(method, params)
    if transport == "curl":
        return _call_curl(endpoint, payload, timeout)
    return _call_urllib(endpoint, payload, timeout)


def _extract_api_list(result: dict) -> list[str]:
    if not result.get("ok") or not isinstance(result.get("json"), dict):
        return []
    candidate = result["json"].get("result", [[]])
    if candidate and isinstance(candidate[0], list):
        return [str(api) for api in candidate[0]]
    if isinstance(candidate, list):
        for item in candidate:
            if isinstance(item, dict) and item.get("type") == "availableApiList":
                names = item.get("names")
                if isinstance(names, list):
                    return [str(api) for api in names]
    return []


def _safe_getter_methods(apis: list[str]) -> list[str]:
    discovered = [
        api
        for api in apis
        if api.startswith("getAvailable") or api.startswith("getSupported")
    ]
    return sorted(set(discovered) | set(SAFE_CAMERA_METHODS))


def _setter_candidates(apis: list[str]) -> list[str]:
    return sorted(api for api in apis if api.startswith("set"))


def _probe_once(args: argparse.Namespace, out_dir: Path, services: list[str], base: str, stamp: str) -> dict:
    summary = {
        "timestamp": stamp,
        "base": base,
        "services": {},
        "network_snapshot": None,
    }

    if args.network_snapshot:
        snapshot = _network_snapshot(base, args.timeout)
        snapshot_file = out_dir / f"{stamp}-network-snapshot.json"
        _write_json(snapshot_file, snapshot)
        summary["network_snapshot"] = str(snapshot_file)

    for service in services:
        endpoint = f"{base}/{service}"
        result = _call(
            endpoint,
            "getAvailableApiList",
            timeout=args.timeout,
            transport=args.transport,
        )
        apis = _extract_api_list(result)
        result["api_count"] = len(apis)
        result["apis"] = apis
        out_file = out_dir / f"{stamp}-{service}-getAvailableApiList.json"
        _write_json(out_file, result)
        summary["services"][service] = {
            "ok": result.get("ok", False),
            "api_count": len(apis),
            "artifact": str(out_file),
            "error_type": result.get("error_type"),
            "error": result.get("error"),
        }

    if args.session_unlock:
        camera_endpoint = f"{base}/camera"
        stages = []
        stages.append(("camera-before-startRecMode",
                       _call(
                           camera_endpoint,
                           "getAvailableApiList",
                           timeout=args.timeout,
                           transport=args.transport,
                       )))
        stages.append(("camera-startRecMode",
                       _call(
                           camera_endpoint,
                           "startRecMode",
                           timeout=args.timeout,
                           transport=args.transport,
                       )))
        time.sleep(args.settle)
        stages.append(("camera-after-startRecMode",
                       _call(
                           camera_endpoint,
                           "getAvailableApiList",
                           timeout=args.timeout,
                           transport=args.transport,
                       )))
        stages.append(("camera-after-startRecMode-getEvent",
                       _call(
                           camera_endpoint,
                           "getEvent",
                           params=[False],
                           timeout=args.timeout,
                           transport=args.transport,
                       )))
        stages.append(("camera-startLiveview",
                       _call(
                           camera_endpoint,
                           "startLiveview",
                           timeout=args.timeout,
                           transport=args.transport,
                       )))
        time.sleep(args.settle)
        stages.append(("camera-after-startLiveview",
                       _call(
                           camera_endpoint,
                           "getAvailableApiList",
                           timeout=args.timeout,
                           transport=args.transport,
                       )))
        stages.append(("camera-after-startLiveview-getEvent",
                       _call(
                           camera_endpoint,
                           "getEvent",
                           params=[False],
                           timeout=args.timeout,
                           transport=args.transport,
                       )))
        unlocked_apis = []
        for label, result in stages:
            apis = _extract_api_list(result)
            result["api_count"] = len(apis)
            result["apis"] = apis
            out_file = out_dir / f"{stamp}-{label}.json"
            _write_json(out_file, result)
            if label.endswith("getAvailableApiList") or "after" in label or "before" in label:
                if len(apis) > len(unlocked_apis):
                    unlocked_apis = apis
                summary["services"][label] = {
                    "ok": result.get("ok", False),
                    "api_count": len(apis),
                    "artifact": str(out_file),
                    "error_type": result.get("error_type"),
                    "error": result.get("error"),
                }

        safe_results = {}
        for method in _safe_getter_methods(unlocked_apis):
            params = [False] if method == "getEvent" else []
            result = _call(
                camera_endpoint,
                method,
                params=params,
                timeout=args.timeout,
                transport=args.transport,
            )
            apis = _extract_api_list(result)
            result["api_count"] = len(apis)
            result["apis"] = apis
            out_file = out_dir / f"{stamp}-safe-{method}.json"
            _write_json(out_file, result)
            safe_results[method] = {
                "ok": result.get("ok", False),
                "artifact": str(out_file),
                "error_type": result.get("error_type"),
                "error": result.get("error"),
            }
        summary["safe_camera_methods_after_unlock"] = safe_results
        summary["setter_candidates_after_unlock"] = {
            "count": len(_setter_candidates(unlocked_apis)),
            "methods": _setter_candidates(unlocked_apis),
        }
        _write_json(
            out_dir / f"{stamp}-setter-candidates-after-unlock.json",
            summary["setter_candidates_after_unlock"],
        )

    summary_file = out_dir / f"{stamp}-summary.json"
    _write_json(summary_file, summary)
    return summary


def _signature(value: dict) -> str:
    return json.dumps(
        {
            "ok": value.get("ok"),
            "api_count": value.get("api_count"),
            "error_type": value.get("error_type"),
            "error": value.get("error"),
        },
        sort_keys=True,
    )


def _repeat_summary(attempts: list[dict]) -> dict:
    by_probe: dict[str, dict[str, int]] = {}
    for attempt in attempts:
        for label, value in attempt.get("services", {}).items():
            bucket = by_probe.setdefault(label, {})
            sig = _signature(value)
            bucket[sig] = bucket.get(sig, 0) + 1
        for label, value in (attempt.get("safe_camera_methods_after_unlock") or {}).items():
            bucket = by_probe.setdefault(f"safe:{label}", {})
            sig = _signature(value)
            bucket[sig] = bucket.get(sig, 0) + 1
    repeated = {
        label: [
            {"signature": json.loads(signature), "count": count}
            for signature, count in sorted(signatures.items())
            if count >= 3
        ]
        for label, signatures in sorted(by_probe.items())
    }
    repeated = {label: values for label, values in repeated.items() if values}
    return {
        "repeat_count": len(attempts),
        "attempt_timestamps": [attempt["timestamp"] for attempt in attempts],
        "repeated_3x_signatures": repeated,
        "note": (
            "A repeated failure signature is not automatically PROVEN-NO; the camera/operator state "
            "must also match the capability-matrix proof bar."
        ),
    }


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base", default="http://192.168.122.1:8080/sony",
                        help="base ScalarWebAPI URL, without trailing service name")
    parser.add_argument("--out-dir", default="artifacts/falsification/r1_api_surface")
    parser.add_argument("--service", action="append", choices=SERVICES,
                        help="service to probe; repeatable. Defaults to all known services.")
    parser.add_argument("--session-unlock", action="store_true",
                        help="also call camera startRecMode/startLiveview and dump unlocked APIs")
    parser.add_argument("--timeout", type=float, default=1.5,
                        help="per-request timeout in seconds")
    parser.add_argument("--transport", choices=("urllib", "curl"), default="urllib",
                        help="HTTP transport. Use curl when Python sockets are sandbox-blocked.")
    parser.add_argument("--settle", type=float, default=1.2,
                        help="seconds to wait after startRecMode/startLiveview state transitions")
    parser.add_argument("--repeat", type=int, default=1,
                        help="repeat the full probe N times, preserving exact artifacts for each attempt")
    parser.add_argument("--no-network-snapshot", dest="network_snapshot", action="store_false",
                        help="skip local SSID/IP/route/ARP/ping snapshot artifacts")
    parser.set_defaults(network_snapshot=True)
    args = parser.parse_args(argv)

    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    services = args.service or list(SERVICES)
    base = args.base.rstrip("/")
    repeat = max(1, args.repeat)
    attempts = []
    for attempt in range(1, repeat + 1):
        suffix = f"-try{attempt:02d}of{repeat:02d}" if repeat > 1 else ""
        stamp = f"{time.strftime('%Y%m%d-%H%M%S')}-{os.getpid()}{suffix}-{_slug(base)}"
        attempts.append(_probe_once(args, out_dir, services, base, stamp))

    if repeat > 1:
        aggregate = _repeat_summary(attempts)
        aggregate["base"] = base
        aggregate["services"] = services
        aggregate["session_unlock"] = args.session_unlock
        aggregate_file = out_dir / f"{attempts[0]['timestamp']}-repeat-summary.json"
        _write_json(aggregate_file, aggregate)
        print(json.dumps(aggregate, indent=2, sort_keys=True))
    else:
        summary = attempts[0]
        print(json.dumps(summary, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
