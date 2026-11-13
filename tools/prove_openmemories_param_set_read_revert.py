#!/usr/bin/env python3
"""Prove one OpenMemories Route 3 parameter setter with set/read/revert artifacts.

The proof app exposes safe, allowlisted `param.*` writes as HTTP endpoints. This runner keeps the
workflow repeatable:

1. Read the current value.
2. Dry-run the requested write.
3. Save the pre-write recovery token in a private mode-0600 file.
4. Optionally execute the write with the one-time guard token.
5. Read after write.
6. Revert with the recovery token even if commit/readback transport failed.
7. Read after revert and remove the private recovery file only after verification.
8. Confirm both consumed tokens are rejected if reused.

Only an executed write that changes the value and returns to the original/revert value is marked as
`ok=true`. Route3 vNext's guarded protocol is required for new proof. Executable mode fails closed:
it never commits without a valid baseline plus guarded dry-run, and it never falls back to an
unguarded write. Tokens are redacted from repo artifacts; the recovery token is temporarily retained
outside the repository with mode 0600 until restoration is verified.
"""

# The CLI is tombstoned. Historical helpers below are retained for offline fixture tests.

from __future__ import annotations

import argparse
import json
import os
import re
import time
import urllib.error
import urllib.parse
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
DEFAULT_RECOVERY_DIR = Path.home() / ".camera-autopilot" / "recovery"
Opener = Callable[..., Any]
REDACTED = "<redacted>"
SENSITIVE_FIELD_NAMES = frozenset(
    {
        "guardToken",
        "revertToken",
        "recoveryToken",
        "guard_token",
        "revert_token",
        "recovery_token",
    }
)
TOKEN_QUERY_RE = re.compile(
    r"([?&](?:guard_token|revert_token|recovery_token)=)([^&#\s\"]+)"
)
TOKEN_JSON_RE = re.compile(
    r'("(?:guardToken|revertToken|recoveryToken|guard_token|revert_token|recovery_token)"\s*:\s*")([^"]*)(")'
)
EXPECTED_REJECTION_STATUSES = frozenset({200, 400, 409})
EXPECTED_REUSE_REJECTIONS = {
    "guard": ("invalid_write_guard", "commit"),
    "revert": ("invalid_revert_guard", "revert"),
}


def _stamp() -> str:
    return f"{time.strftime('%Y%m%d-%H%M%S')}-{time.time_ns()}"


def _normalize_base(base: str) -> str:
    return base.rstrip("/")


def _safe_slug(value: str) -> str:
    slug = re.sub(r"[^A-Za-z0-9_.-]+", "-", str(value)).strip("-")
    return slug or "blank"


def _artifact_label(path: Path) -> str:
    try:
        return str(path.relative_to(REPO_ROOT))
    except ValueError:
        return str(path)


def _response_status(response: Any) -> int | None:
    status = getattr(response, "status", None)
    if status is None and hasattr(response, "getcode"):
        status = response.getcode()
    try:
        return int(status)
    except (TypeError, ValueError):
        return None


def _parse_response(raw: bytes) -> tuple[dict[str, Any] | None, str]:
    text = raw.decode("utf-8", errors="replace")
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError:
        parsed = None
    return parsed if isinstance(parsed, dict) else None, text[:2000]


def _value_from(result: dict[str, Any], key: str = "value") -> Any:
    response_json = result.get("response_json")
    if isinstance(response_json, dict) and key in response_json:
        return response_json[key]
    return result.get(key)


def _registry_setting_id(*results: dict[str, Any]) -> str | None:
    for result in results:
        response_json = result.get("response_json")
        if isinstance(response_json, dict):
            value = response_json.get("registrySettingId") or response_json.get("registry_setting_id")
            if isinstance(value, str) and value:
                return value
        value = result.get("registrySettingId") or result.get("registry_setting_id")
        if isinstance(value, str) and value:
            return value
    return None


def _same_value(left: Any, right: Any) -> bool:
    return str(left) == str(right)


def _redact_string(value: str, secrets: tuple[str, ...]) -> str:
    redacted = value
    for secret in secrets:
        if secret:
            redacted = redacted.replace(secret, REDACTED)
    redacted = TOKEN_QUERY_RE.sub(rf"\1{REDACTED}", redacted)
    return TOKEN_JSON_RE.sub(rf"\1{REDACTED}\3", redacted)


def _redact_sensitive(value: Any, *, secrets: tuple[str, ...] = ()) -> Any:
    if isinstance(value, dict):
        return {
            key: (
                REDACTED
                if key in SENSITIVE_FIELD_NAMES and item not in (None, "")
                else _redact_sensitive(item, secrets=secrets)
            )
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [_redact_sensitive(item, secrets=secrets) for item in value]
    if isinstance(value, tuple):
        return tuple(_redact_sensitive(item, secrets=secrets) for item in value)
    if isinstance(value, str):
        return _redact_string(value, secrets)
    return value


def call_endpoint(
    base: str,
    method: str,
    endpoint: str,
    *,
    timeout: float = 5.0,
    opener: Opener = urllib.request.urlopen,
) -> dict[str, Any]:
    url = _normalize_base(base) + endpoint
    request = urllib.request.Request(
        url,
        method=method,
        headers={"Accept": "application/json"},
    )
    started = time.time()
    error = None
    response_received = False
    try:
        with opener(request, timeout=timeout) as response:
            response_received = True
            raw = response.read()
            response_json, response_text = _parse_response(raw)
            status_code = _response_status(response)
            transport_ok = status_code is None or 200 <= status_code < 300
    except urllib.error.HTTPError as exc:
        response_received = True
        raw = exc.read()
        response_json, response_text = _parse_response(raw)
        status_code = exc.code
        transport_ok = False
        error = str(exc)
    except (OSError, TimeoutError, urllib.error.URLError) as exc:
        response_json = None
        response_text = ""
        status_code = None
        transport_ok = False
        error = str(exc)

    app_ok = isinstance(response_json, dict) and response_json.get("ok") is True
    result = {
        "method": method,
        "endpoint": endpoint,
        "url": url,
        "status_code": status_code,
        "response_received": response_received,
        "transport_ok": transport_ok,
        "app_ok": app_ok,
        "ok": bool(transport_ok and app_ok),
        "elapsed_s": round(time.time() - started, 3),
        "response_json": response_json,
        "response_text": response_text,
    }
    if error:
        result["error"] = error
    return result


def _write_step(
    path: Path,
    result: dict[str, Any],
    *,
    name: str,
    secrets: tuple[str, ...] = (),
) -> dict[str, Any]:
    persisted = _redact_sensitive(result, secrets=secrets)
    path.write_text(json.dumps(persisted, indent=2, sort_keys=True), encoding="utf-8")
    step = dict(persisted)
    step["name"] = name
    step["artifact"] = _artifact_label(path)
    return step


def _write_emergency_recovery_record(
    recovery_dir: Path,
    *,
    base: str,
    setting_id: str,
    before: Any,
    requested: str,
    recovery_token: str,
) -> Path:
    recovery_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    os.chmod(recovery_dir, 0o700)
    path = recovery_dir / f"route3-{_safe_slug(setting_id)}.json"
    payload = {
        "base": _normalize_base(base),
        "setting_id": setting_id,
        "before": before,
        "requested": requested,
        "recovery_token": recovery_token,
        "created_at": int(time.time()),
        "note": "Sensitive emergency recovery token. Delete only after verified restoration.",
    }
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, indent=2, sort_keys=True)
            handle.write("\n")
    except BaseException:
        try:
            os.close(descriptor)
        except OSError:
            pass
        raise
    os.chmod(path, 0o600)
    return path


def _clear_emergency_recovery_record(path: Path | None) -> bool:
    if path is None:
        return True
    try:
        path.unlink()
    except FileNotFoundError:
        return True
    return not path.exists()


def _response_field(result: dict[str, Any], key: str) -> Any:
    response_json = result.get("response_json")
    if isinstance(response_json, dict):
        return response_json.get(key)
    return None


def _setting_endpoint(
    setting_id: str,
    *,
    value: str | None = None,
    dry_run: bool = False,
    guard_token: str | None = None,
    revert_token: str | None = None,
    revert: bool = False,
) -> str:
    setting_path = urllib.parse.quote(setting_id, safe="")
    endpoint = f"/settings/{setting_path}"
    if revert:
        endpoint += "/revert"
    query: dict[str, str] = {}
    if value is not None:
        query["value"] = value
    if dry_run:
        query["dry_run"] = "true"
    if guard_token:
        query["guard_token"] = guard_token
    if revert_token:
        query["revert_token"] = revert_token
    if query:
        endpoint += "?" + urllib.parse.urlencode(query)
    return endpoint


def _valid_baseline(result: dict[str, Any], value: Any) -> bool:
    return bool(result.get("ok") is True and value is not None)


def _valid_guarded_dry_run(
    result: dict[str, Any],
    *,
    before: Any,
    requested: str,
    guard_token: Any,
    recovery_token: Any,
) -> bool:
    response = result.get("response_json")
    return bool(
        result.get("ok") is True
        and isinstance(response, dict)
        and response.get("dry_run") is True
        and response.get("wouldWrite") is True
        and response.get("writeGuardRequired") is True
        and _same_value(response.get("before"), before)
        and _same_value(response.get("requested"), requested)
        and isinstance(guard_token, str)
        and bool(guard_token)
        and isinstance(recovery_token, str)
        and bool(recovery_token)
    )


def _expected_token_reuse_rejection(
    result: dict[str, Any] | None,
    *,
    token_kind: str,
) -> bool:
    if result is None or result.get("response_received") is not True:
        return False
    if result.get("status_code") not in EXPECTED_REJECTION_STATUSES:
        return False
    response = result.get("response_json")
    if not isinstance(response, dict) or response.get("ok") is not False:
        return False
    expected_error, expected_phase = EXPECTED_REUSE_REJECTIONS[token_kind]
    return bool(
        response.get("error") == expected_error
        and response.get("phase") == expected_phase
        and response.get("writeGuardRequired") is True
    )


def run_proof(
    *,
    base: str,
    setting_id: str,
    value: str,
    revert_value: str | None = None,
    execute: bool = False,
    out_dir: Path = DEFAULT_OUT_DIR,
    recovery_dir: Path = DEFAULT_RECOVERY_DIR,
    timeout: float = 5.0,
    opener: Opener = urllib.request.urlopen,
) -> tuple[dict[str, Any], Path]:
    proof_dir = out_dir / f"{_stamp()}-{_safe_slug(setting_id)}-set-read-revert"
    proof_dir.mkdir(parents=True, exist_ok=True)

    steps: list[dict[str, Any]] = []
    before_result = call_endpoint(
        base,
        "GET",
        _setting_endpoint(setting_id),
        timeout=timeout,
        opener=opener,
    )
    steps.append(_write_step(proof_dir / "before.json", before_result, name="before"))
    before = _value_from(before_result)
    baseline_ok = _valid_baseline(before_result, before)
    final_revert_value = (
        str(revert_value)
        if revert_value is not None
        else (str(before) if baseline_ok else None)
    )

    dry_run_result: dict[str, Any] | None = None
    guard_token: str | None = None
    recovery_token: str | None = None
    dry_run_ok = False
    guarded_protocol = False
    guarded_dry_run_ok = False
    if baseline_ok:
        dry_run_result = call_endpoint(
            base,
            "POST",
            _setting_endpoint(setting_id, value=value, dry_run=True),
            timeout=timeout,
            opener=opener,
        )
        guard_token_value = _response_field(dry_run_result, "guardToken")
        guard_token = (
            guard_token_value
            if isinstance(guard_token_value, str) and guard_token_value
            else None
        )
        recovery_token_value = _response_field(dry_run_result, "recoveryToken")
        recovery_token = (
            recovery_token_value
            if isinstance(recovery_token_value, str) and recovery_token_value
            else None
        )
        dry_run_ok = dry_run_result.get("ok") is True
        guarded_protocol = guard_token is not None and recovery_token is not None
        guarded_dry_run_ok = _valid_guarded_dry_run(
            dry_run_result,
            before=before,
            requested=value,
            guard_token=guard_token,
            recovery_token=recovery_token,
        )
        steps.append(
            _write_step(
                proof_dir / f"dry_run_set_{_safe_slug(value)}.json",
                dry_run_result,
                name=f"dry_run_set_{value}",
                secrets=tuple(
                    token for token in (guard_token, recovery_token) if token
                ),
            )
        )

    set_result: dict[str, Any] | None = None
    after_set_result: dict[str, Any] | None = None
    guard_reuse_result: dict[str, Any] | None = None
    revert_result: dict[str, Any] | None = None
    after_revert_result: dict[str, Any] | None = None
    revert_reuse_result: dict[str, Any] | None = None
    revert_token: str | None = None
    commit_revert_token: str | None = None
    recovery_token_consistent = True
    recovery_record_path: Path | None = None
    recovery_record_cleared = False
    after_set = None
    after_revert = None
    requested_differs = bool(baseline_ok and not _same_value(before, value))

    if execute and guarded_dry_run_ok and requested_differs:
        recovery_record_path = _write_emergency_recovery_record(
            recovery_dir,
            base=base,
            setting_id=setting_id,
            before=before,
            requested=value,
            recovery_token=recovery_token or "",
        )
        set_result = call_endpoint(
            base,
            "POST",
            _setting_endpoint(
                setting_id,
                value=value,
                guard_token=guard_token,
            ),
            timeout=timeout,
            opener=opener,
        )
        revert_token_value = _response_field(set_result, "revertToken")
        commit_revert_token = (
            revert_token_value
            if isinstance(revert_token_value, str) and revert_token_value
            else None
        )
        recovery_token_consistent = (
            commit_revert_token is None or commit_revert_token == recovery_token
        )
        revert_token = recovery_token
        secrets = tuple(
            token
            for token in (guard_token, recovery_token, commit_revert_token)
            if token
        )
        steps.append(
            _write_step(
                proof_dir / f"set_{_safe_slug(value)}.json",
                set_result,
                name=f"set_{value}",
                secrets=secrets,
            )
        )
        after_set_result = call_endpoint(
            base,
            "GET",
            _setting_endpoint(setting_id),
            timeout=timeout,
            opener=opener,
        )
        steps.append(_write_step(proof_dir / "after_set.json", after_set_result, name="after_set"))
        after_set = _value_from(after_set_result)
        set_ok_now = set_result.get("ok") is True
        after_set_ok_now = (
            after_set_result.get("ok") is True
            and after_set is not None
            and _same_value(after_set, value)
        )

        if set_ok_now and after_set_ok_now and recovery_token_consistent:
            guard_reuse_result = call_endpoint(
                base,
                "POST",
                _setting_endpoint(setting_id, value=value, guard_token=guard_token),
                timeout=timeout,
                opener=opener,
            )
            steps.append(
                _write_step(
                    proof_dir / "reused_guard_token.json",
                    guard_reuse_result,
                    name="reused_guard_token",
                    secrets=secrets,
                )
            )

    elif recovery_token:
        revert_token = recovery_token

    if revert_token:
        secrets = tuple(
            token
            for token in (guard_token, recovery_token, commit_revert_token)
            if token
        )
        revert_result = call_endpoint(
            base,
            "POST",
            _setting_endpoint(
                setting_id,
                revert=True,
                revert_token=revert_token,
            ),
            timeout=timeout,
            opener=opener,
        )
        steps.append(
            _write_step(
                proof_dir / f"revert_{_safe_slug(final_revert_value)}.json",
                revert_result,
                name=f"revert_{final_revert_value}",
                secrets=secrets,
            )
        )
        after_revert_result = call_endpoint(
            base,
            "GET",
            _setting_endpoint(setting_id),
            timeout=timeout,
            opener=opener,
        )
        steps.append(
            _write_step(
                proof_dir / "after_revert.json",
                after_revert_result,
                name="after_revert",
            )
        )
        after_revert = _value_from(after_revert_result)

        revert_ok_now = revert_result.get("ok") is True
        after_revert_ok_now = (
            after_revert_result.get("ok") is True
            and after_revert is not None
            and final_revert_value is not None
            and _same_value(after_revert, final_revert_value)
        )
        if recovery_record_path is not None and revert_ok_now and after_revert_ok_now:
            recovery_record_cleared = _clear_emergency_recovery_record(
                recovery_record_path
            )
        if (
            execute
            and set_result is not None
            and revert_ok_now
            and after_revert_ok_now
        ):
            revert_reuse_result = call_endpoint(
                base,
                "POST",
                _setting_endpoint(
                    setting_id,
                    revert=True,
                    revert_token=revert_token,
                ),
                timeout=timeout,
                opener=opener,
            )
            steps.append(
                _write_step(
                    proof_dir / "reused_revert_token.json",
                    revert_reuse_result,
                    name="reused_revert_token",
                    secrets=secrets,
                )
            )

    changed = bool(
        execute
        and set_result is not None
        and set_result.get("ok") is True
        and before is not None
        and after_set is not None
        and not _same_value(before, after_set)
    )
    set_ok = execute and set_result is not None and set_result.get("ok") is True
    revert_ok = revert_result is not None and revert_result.get("ok") is True
    after_set_ok = bool(
        execute
        and after_set_result is not None
        and after_set_result.get("ok") is True
        and after_set is not None
        and _same_value(after_set, value)
    )
    after_revert_ok = bool(
        after_revert_result is not None
        and after_revert_result.get("ok") is True
        and after_revert is not None
        and final_revert_value is not None
        and _same_value(after_revert, final_revert_value)
    )
    guard_reuse_rejected = _expected_token_reuse_rejection(
        guard_reuse_result,
        token_kind="guard",
    )
    revert_reuse_rejected = _expected_token_reuse_rejection(
        revert_reuse_result,
        token_kind="revert",
    )
    ok = bool(
        baseline_ok
        and guarded_dry_run_ok
        and requested_differs
        and changed
        and set_ok
        and after_set_ok
        and recovery_token_consistent
        and revert_token
        and revert_ok
        and after_revert_ok
        and recovery_record_cleared
        and guard_reuse_rejected
        and revert_reuse_rejected
    )

    failure_reason = None
    if not baseline_ok:
        failure_reason = (
            "baseline_value_missing"
            if before_result.get("ok") is True
            else "baseline_read_failed"
        )
    elif not dry_run_ok:
        failure_reason = "dry_run_failed"
    elif not guard_token:
        failure_reason = "guard_token_missing"
    elif not recovery_token:
        failure_reason = "recovery_token_missing"
    elif not guarded_dry_run_ok:
        failure_reason = "guarded_dry_run_invalid"
    elif not execute:
        failure_reason = (
            "dry_run_only"
            if revert_ok and after_revert_ok
            else "dry_run_recovery_cleanup_failed"
        )
    elif not requested_differs:
        failure_reason = "requested_equals_before"
    elif set_result is None:
        failure_reason = "execution_blocked"
    elif not set_ok:
        failure_reason = "set_failed"
    elif not recovery_token_consistent:
        failure_reason = "commit_recovery_token_mismatch"
    elif not after_set_ok:
        failure_reason = "after_set_mismatch"
    elif not changed:
        failure_reason = "setting_did_not_change"
    elif not revert_token:
        failure_reason = "revert_token_missing"
    elif not revert_ok:
        failure_reason = "revert_failed"
    elif not after_revert_ok:
        failure_reason = "after_revert_mismatch"
    elif not guard_reuse_rejected:
        failure_reason = "guard_token_reuse_not_rejected"
    elif not revert_reuse_rejected:
        failure_reason = "revert_token_reuse_not_rejected"

    registry_setting_id = _registry_setting_id(
        before_result,
        dry_run_result or {},
        set_result or {},
        after_set_result or {},
        revert_result or {},
        after_revert_result or {},
    )
    summary = {
        "artifact": "openmemories_param_set_read_revert",
        "route": "openmemories_route3_proof_app",
        "base": _normalize_base(base),
        "setting_id": setting_id,
        "registry_setting_id": registry_setting_id,
        "before": before,
        "baseline_ok": baseline_ok,
        "requested": value,
        "requested_differs": requested_differs,
        "revert_value": final_revert_value,
        "after_set": after_set,
        "after_revert": after_revert,
        "dry_run_ok": dry_run_ok,
        "guarded_protocol": guarded_protocol,
        "guarded_dry_run_ok": guarded_dry_run_ok,
        "guard_token_received": bool(guard_token),
        "recovery_token_received": bool(recovery_token),
        "recovery_token_consistent": recovery_token_consistent,
        "revert_token_received": bool(revert_token),
        "recovery_cleanup_ok": bool(revert_ok and after_revert_ok),
        "emergency_recovery_record_created": recovery_record_path is not None,
        "emergency_recovery_record_cleared": recovery_record_cleared,
        "emergency_recovery_record_retained": bool(
            recovery_record_path is not None and not recovery_record_cleared
        ),
        "guard_reuse_rejected": guard_reuse_rejected,
        "revert_reuse_rejected": revert_reuse_rejected,
        "executed": execute,
        "execution_blocked": bool(execute and set_result is None),
        "changed": changed,
        "ok": ok,
        "failure_reason": failure_reason,
        "steps": steps,
    }
    secrets = tuple(
        token
        for token in (guard_token, recovery_token, commit_revert_token, revert_token)
        if token
    )
    summary = _redact_sensitive(summary, secrets=secrets)
    summary_path = proof_dir / "openmemories-param-set-read-revert.json"
    summary_path.write_text(json.dumps(summary, indent=2, sort_keys=True), encoding="utf-8")
    return summary, summary_path


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--base",
        default=DEFAULT_BASE,
        help="proof app base URL; defaults to CAMERA_LAN_BASE or CAMERA_LAN_HOST on port 8080",
    )
    parser.add_argument("--setting-id", required=True, help="OpenMemories proof app setting id, e.g. param.iso_sensitivity")
    parser.add_argument("--value", required=True, help="Requested camera parameter value")
    parser.add_argument("--revert-value", help="Value to restore after write; defaults to the first read value")
    parser.add_argument("--execute", action="store_true", help="Actually write and revert after the dry-run succeeds")
    parser.add_argument("--timeout", type=float, default=5.0)
    parser.add_argument("--out-dir", type=Path, default=DEFAULT_OUT_DIR)
    parser.add_argument(
        "--recovery-dir",
        type=Path,
        default=DEFAULT_RECOVERY_DIR,
        help="Private mode-0700 directory for an emergency recovery token",
    )
    args = parser.parse_args(argv)
    if not args.base:
        parser.error(
            "--base is required unless CAMERA_LAN_BASE or CAMERA_LAN_HOST is set"
        )
    return args


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
    raise SystemExit(main())
