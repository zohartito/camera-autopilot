#!/usr/bin/env python3
"""Capture HTTP proof from the clean-room OpenMemories Route 3 proof app.

The generated camera-hosted app is only useful once we can prove it answers over LAN. This tool
records that proof as small JSON artifacts consumed by `review_openmemories_artifacts.py`.

Default mode is safe/read-oriented:
  - GET /health
  - GET /settings
  - GET /settings/{setting-id}

The optional parameter dump is also read-only:
  - GET /parameters
  - GET /settings/param.dump

Preview/focus/capture endpoints are camera actions and require both --include-actions and
--allow-camera-actions.
"""

# The CLI is tombstoned. Historical helpers below are retained for offline fixture tests.

from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.request
from collections.abc import Callable
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_BASE = os.environ.get("CAMERA_LAN_BASE", "").strip()
if not DEFAULT_BASE:
    _camera_lan_host = os.environ.get("CAMERA_LAN_HOST", "").strip()
    DEFAULT_BASE = f"http://{_camera_lan_host}:8080" if _camera_lan_host else ""
DEFAULT_OUT_DIR = REPO_ROOT / "artifacts" / "falsification" / "r3_openmemories"
SAFE_ENDPOINTS = (
    ("GET", "/health", None, "safe_read"),
    ("GET", "/settings", None, "safe_read"),
)
PARAMETER_DUMP_ENDPOINTS = (
    ("GET", "/parameters", None, "safe_parameter_dump"),
    ("GET", "/settings/param.dump", None, "safe_parameter_dump"),
)
ACTION_ENDPOINTS = (
    ("POST", "/preview", {}, "camera_preview_action"),
    ("POST", "/focus", {}, "camera_focus_action"),
    ("POST", "/capture", {}, "camera_capture_action"),
    ("GET", "/settings/capture.count", None, "safe_read_after_action"),
    ("GET", "/settings/capture.bytes", None, "safe_read_after_action"),
    ("GET", "/media/latest", None, "safe_read_after_action"),
)
Opener = Callable[..., Any]


def _stamp() -> str:
    return f"{time.strftime('%Y%m%d-%H%M%S')}-{time.time_ns()}"


def _normalize_base(base: str) -> str:
    return base.rstrip("/")


def _endpoint_filename(method: str, endpoint: str) -> str:
    slug = endpoint.strip("/").replace("/", "-") or "root"
    return f"{method.lower()}-{slug}.json"


def _artifact_label(path: Path) -> str:
    try:
        return str(path.relative_to(REPO_ROOT))
    except ValueError:
        return str(path)


def _focus_observation_ok(
    *,
    before: str | None,
    after: str | None,
    note: str | None,
    artifact: Path | None,
) -> bool:
    return bool(
        before
        and after
        and before.strip() != after.strip()
        and note
        and note.strip()
        and artifact is not None
        and artifact.exists()
    )


def _response_status(response: Any) -> int | None:
    status = getattr(response, "status", None)
    if status is None and hasattr(response, "getcode"):
        status = response.getcode()
    try:
        return int(status)
    except (TypeError, ValueError):
        return None


def _response_header(response: Any, name: str) -> str | None:
    headers = getattr(response, "headers", None)
    if headers is not None and hasattr(headers, "get"):
        value = headers.get(name)
        return str(value) if value is not None else None
    if hasattr(response, "getheader"):
        value = response.getheader(name)
        return str(value) if value is not None else None
    return None


def _parse_response(raw: bytes) -> tuple[dict[str, Any] | None, str]:
    text = raw.decode("utf-8", errors="replace")
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError:
        parsed = None
    return parsed if isinstance(parsed, dict) else None, text[:2000]


def _numeric_value(payload: dict[str, Any], key: str) -> float | None:
    value = payload.get(key)
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _setting_value_number(payload: dict[str, Any]) -> float | None:
    return _numeric_value(payload, "value")


def _capture_complete(payload: dict[str, Any] | None, expected_min_count: float | None = None) -> bool:
    if not isinstance(payload, dict):
        return False
    if payload.get("latestError"):
        return False
    if payload.get("captureInFlight") is True:
        return False
    if expected_min_count is not None:
        count = _numeric_value(payload, "captureCount")
        if count is None or count < expected_min_count:
            return False
    return (_numeric_value(payload, "latestCaptureBytes") or 0) > 0


def _app_success(endpoint: str, payload: dict[str, Any] | None) -> bool:
    if not isinstance(payload, dict):
        return False
    if endpoint in {"/focus", "/capture"}:
        return False
    if endpoint == "/settings/capture.count":
        value = _setting_value_number(payload)
        return value is not None and value > 0
    if endpoint == "/settings/capture.bytes":
        value = _setting_value_number(payload)
        return value is not None and value > 0
    if payload.get("ok") is True:
        return True
    if endpoint == "/media/latest":
        return payload.get("cameraOpen") is True and (_numeric_value(payload, "latestCaptureBytes") or 0) > 0
    if payload.get("cameraOpen") is True:
        return True
    return False


def wait_for_capture_completion(
    base: str,
    *,
    timeout: float,
    expected_min_count: float | None = None,
    opener: Opener = urllib.request.urlopen,
) -> dict[str, Any]:
    deadline = time.time() + timeout
    attempts = []
    last_result: dict[str, Any] | None = None
    while True:
        last_result = call_endpoint(
            base,
            "GET",
            "/media/latest",
            risk="safe_read_after_capture_wait",
            timeout=timeout,
            opener=opener,
        )
        payload = last_result.get("response_json")
        attempts.append(
            {
                "elapsed_s": last_result.get("elapsed_s"),
                "status_code": last_result.get("status_code"),
                "captureInFlight": payload.get("captureInFlight") if isinstance(payload, dict) else None,
                "latestCaptureBytes": payload.get("latestCaptureBytes") if isinstance(payload, dict) else None,
                "latestError": payload.get("latestError") if isinstance(payload, dict) else None,
            }
        )
        if _capture_complete(payload, expected_min_count=expected_min_count):
            break
        if isinstance(payload, dict) and payload.get("latestError"):
            break
        if time.time() >= deadline:
            break
        time.sleep(0.25)

    payload = last_result.get("response_json") if last_result else None
    ok = _capture_complete(payload, expected_min_count=expected_min_count)
    result = dict(last_result or {})
    result.update(
        {
            "tool": "prove_openmemories_endpoint_surface",
            "method": "GET",
            "endpoint": "/media/latest#capture-completion",
            "risk": "safe_read_after_capture_wait",
            "app_ok": ok,
            "ok": bool((last_result or {}).get("transport_ok") and ok),
            "expected_min_count": expected_min_count,
            "attempt_count": len(attempts),
            "attempts": attempts,
        }
    )
    return result


def call_endpoint(
    base: str,
    method: str,
    endpoint: str,
    *,
    body: dict[str, Any] | None = None,
    risk: str,
    timeout: float = 5.0,
    opener: Opener = urllib.request.urlopen,
) -> dict[str, Any]:
    url = _normalize_base(base) + endpoint
    data = None if body is None else json.dumps(body).encode("utf-8")
    request = urllib.request.Request(
        url,
        data=data,
        method=method,
        headers={"Accept": "application/json", "Content-Type": "application/json"},
    )
    started = time.time()
    try:
        response = opener(request, timeout=timeout)
        raw = response.read()
        status_code = _response_status(response)
        content_type = _response_header(response, "Content-Type")
        parsed, text_excerpt = _parse_response(raw)
        transport_ok = status_code is not None and 200 <= status_code < 300
        app_ok = _app_success(endpoint, parsed)
        ok = bool(transport_ok and app_ok)
        accepted = bool(isinstance(parsed, dict) and parsed.get("accepted") is True)
        return {
            "tool": "prove_openmemories_endpoint_surface",
            "method": method,
            "endpoint": endpoint,
            "url": url,
            "risk": risk,
            "status_code": status_code,
            "content_type": content_type,
            "transport_ok": transport_ok,
            "app_ok": app_ok,
            "ok": ok,
            "accepted": accepted,
            "response_json": parsed,
            "response_text_excerpt": text_excerpt,
            "elapsed_s": round(time.time() - started, 3),
        }
    except urllib.error.HTTPError as exc:
        raw = exc.read()
        parsed, text_excerpt = _parse_response(raw)
        return {
            "tool": "prove_openmemories_endpoint_surface",
            "method": method,
            "endpoint": endpoint,
            "url": url,
            "risk": risk,
            "status_code": exc.code,
            "content_type": exc.headers.get("Content-Type") if exc.headers else None,
            "transport_ok": False,
            "app_ok": False,
            "ok": False,
            "accepted": False,
            "response_json": parsed,
            "response_text_excerpt": text_excerpt,
            "error_type": exc.__class__.__name__,
            "error": str(exc),
            "elapsed_s": round(time.time() - started, 3),
        }
    except Exception as exc:
        return {
            "tool": "prove_openmemories_endpoint_surface",
            "method": method,
            "endpoint": endpoint,
            "url": url,
            "risk": risk,
            "status_code": None,
            "transport_ok": False,
            "app_ok": False,
            "ok": False,
            "accepted": False,
            "response_json": None,
            "response_text_excerpt": "",
            "error_type": exc.__class__.__name__,
            "error": str(exc),
            "elapsed_s": round(time.time() - started, 3),
        }


def capture_surface(
    *,
    base: str = DEFAULT_BASE,
    setting_id: str = "preview.running",
    include_parameter_dump: bool = False,
    include_actions: bool = False,
    allow_camera_actions: bool = False,
    out_dir: Path = DEFAULT_OUT_DIR,
    timeout: float = 5.0,
    opener: Opener = urllib.request.urlopen,
    focus_observed_before: str | None = None,
    focus_observed_after: str | None = None,
    focus_observation_note: str | None = None,
    focus_proof_artifact: Path | None = None,
) -> tuple[dict[str, Any], Path]:
    if not base:
        raise ValueError("proof app base URL is required")
    if include_actions and not allow_camera_actions:
        raise ValueError("--include-actions requires --allow-camera-actions")
    if focus_proof_artifact is not None:
        focus_proof_artifact = focus_proof_artifact.expanduser().resolve()
        if not focus_proof_artifact.exists():
            raise ValueError(f"--focus-proof-artifact does not exist: {focus_proof_artifact}")

    run_dir = out_dir / f"{_stamp()}-openmemories-endpoint-proof"
    run_dir.mkdir(parents=True, exist_ok=True)
    endpoint_specs = [*SAFE_ENDPOINTS, ("GET", f"/settings/{setting_id}", None, "safe_read")]
    if include_parameter_dump:
        endpoint_specs.extend(PARAMETER_DUMP_ENDPOINTS)
    if include_actions:
        endpoint_specs.extend(ACTION_ENDPOINTS)

    results = []
    for method, endpoint, body, risk in endpoint_specs:
        result = call_endpoint(
            base,
            method,
            endpoint,
            body=body,
            risk=risk,
            timeout=timeout,
            opener=opener,
        )
        path = run_dir / _endpoint_filename(method, endpoint)
        path.write_text(json.dumps(result, indent=2, sort_keys=True), encoding="utf-8")
        result_summary = {
            "method": method,
            "endpoint": endpoint,
            "risk": risk,
            "ok": result["ok"],
            "transport_ok": result.get("transport_ok") is True,
            "app_ok": result.get("app_ok") is True,
            "accepted": result.get("accepted") is True,
            "status_code": result.get("status_code"),
            "artifact": _artifact_label(path),
        }
        results.append(result_summary)
        if endpoint == "/capture" and result.get("accepted") is True:
            response_json = result.get("response_json")
            count_before = None
            if isinstance(response_json, dict):
                count_before = _numeric_value(response_json, "captureCountBefore")
            wait_result = wait_for_capture_completion(
                base,
                timeout=timeout,
                expected_min_count=count_before + 1 if count_before is not None else None,
                opener=opener,
            )
            wait_path = run_dir / "wait-media-latest-after-capture.json"
            wait_path.write_text(json.dumps(wait_result, indent=2, sort_keys=True), encoding="utf-8")
            results.append(
                {
                    "method": "GET",
                    "endpoint": "/media/latest#capture-completion",
                    "risk": "safe_read_after_capture_wait",
                    "ok": wait_result["ok"],
                    "transport_ok": wait_result.get("transport_ok") is True,
                    "app_ok": wait_result.get("app_ok") is True,
                    "accepted": False,
                    "status_code": wait_result.get("status_code"),
                    "artifact": _artifact_label(wait_path),
                }
            )

    focus_observation = None
    if any(
        value is not None
        for value in (
            focus_observed_before,
            focus_observed_after,
            focus_observation_note,
            str(focus_proof_artifact) if focus_proof_artifact is not None else None,
        )
    ):
        if not include_actions:
            raise ValueError("focus observation requires --include-actions and --allow-camera-actions")
        focus_observation = {
            "tool": "prove_openmemories_focus_observation",
            "kind": "manual_visual_focus_completion",
            "endpoint": "/focus",
            "method": "POST",
            "observed_before": focus_observed_before,
            "observed_after": focus_observed_after,
            "observation_note": focus_observation_note,
            "proof_artifact": _artifact_label(focus_proof_artifact) if focus_proof_artifact else None,
            "proof_artifact_exists": bool(focus_proof_artifact and focus_proof_artifact.exists()),
            "ok": _focus_observation_ok(
                before=focus_observed_before,
                after=focus_observed_after,
                note=focus_observation_note,
                artifact=focus_proof_artifact,
            ),
        }
        focus_path = run_dir / "focus-observation.json"
        focus_path.write_text(json.dumps(focus_observation, indent=2, sort_keys=True), encoding="utf-8")
        focus_observation["artifact"] = _artifact_label(focus_path)

    summary = {
        "tool": "prove_openmemories_endpoint_surface",
        "generated_at": time.strftime("%Y%m%d-%H%M%S"),
        "base": base,
        "setting_id": setting_id,
        "include_parameter_dump": include_parameter_dump,
        "include_actions": include_actions,
        "allow_camera_actions": allow_camera_actions,
        "results": results,
        "ok_count": sum(1 for item in results if item["ok"]),
        "result_count": len(results),
        "focus_observation": focus_observation,
        "focus_observation_ok": bool(focus_observation and focus_observation["ok"]),
    }
    summary_path = run_dir / "summary.json"
    summary_path.write_text(json.dumps(summary, indent=2, sort_keys=True), encoding="utf-8")
    return summary, summary_path


def main(argv: list[str] | None = None) -> int:
    """Reject every legacy CLI invocation before parsing or contacting a camera."""
    print(json.dumps({
        "ok": False,
        "executed": False,
        "error": "OpenMemories Route 3 is permanently disabled; this CLI cannot contact the camera.",
        "offline_only": "Historical protocol helpers remain for injected-fixture regression tests.",
    }, indent=2))
    return 2


if __name__ == "__main__":
    raise SystemExit(main(__import__("sys").argv[1:]))
