#!/usr/bin/env python3
"""Run and archive a Smart Remote setter attempt, then attach observed behavior proof.

This is the live counterpart to `a7ii_smart_remote_behavior_proof.py`: it is dry-run-first, requires
explicit camera-write approval, archives the exact JSON-RPC setter attempt, and can immediately
write the behavior-proof marker consumed by `review_smart_remote_api_surface.py`.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parent.parent
BACKEND = REPO_ROOT / "backend"
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))
if str(Path(__file__).resolve().parent) not in sys.path:
    sys.path.insert(0, str(Path(__file__).resolve().parent))

import a7ii_smart_remote_behavior_proof
import settings_registry
import settings_smart_remote_values

SETTER_PREFIXES = ("act", "set", "start", "stop")
VALUE_PLACEHOLDER = "<value sent>"


def _repo_path(path: Path) -> str:
    try:
        return str(path.resolve().relative_to(REPO_ROOT))
    except ValueError:
        return str(path)


def _parse_value(value: str) -> Any:
    raw = value.strip()
    if raw.startswith("{") or raw.startswith("[") or raw.startswith('"'):
        try:
            return json.loads(raw)
        except json.JSONDecodeError:
            return value
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError:
        return value
    if isinstance(parsed, (int, float, bool)) or parsed is None:
        return parsed
    return value


def _smart_remote_params(method: str, value: Any) -> list[Any]:
    if method == "setTouchAFPosition":
        if isinstance(value, dict):
            if "x" not in value or "y" not in value:
                raise ValueError("setTouchAFPosition expects JSON {'x': 0..100, 'y': 0..100}")
            return [value["x"], value["y"]]
        if isinstance(value, (list, tuple)) and len(value) == 2:
            return [value[0], value[1]]
        raise ValueError("setTouchAFPosition expects JSON {'x': 0..100, 'y': 0..100} or [x, y]")
    if method == "setIsoSpeedRate":
        return [str(value)]
    if method == "setWhiteBalance":
        if isinstance(value, dict):
            mode = value.get("whiteBalanceMode") or value.get("mode")
            color_temperature = value.get("colorTemperature", -1)
            if not mode:
                raise ValueError("setWhiteBalance expects whiteBalanceMode/mode in JSON object")
            return [mode, False, color_temperature]
        if isinstance(value, str):
            return [value, False, -1]
        if isinstance(value, (list, tuple)) and len(value) == 3:
            return list(value)
        raise ValueError("setWhiteBalance expects mode string, JSON object, or [mode, false, colorTemperature]")
    if isinstance(value, (list, tuple)):
        return list(value)
    return [value]


def _resolve_auto_value(
    *,
    remote: Any,
    row: dict[str, Any],
    method: str,
    mode: str,
    apis_seen: list[str],
) -> tuple[Any, dict[str, Any]]:
    if mode != settings_smart_remote_values.AUTO_VALUE_MODE:
        raise ValueError(f"unsupported --auto-value mode: {mode}")
    strategy = settings_smart_remote_values.strategy_for(row["setting_id"], method)
    if strategy is None:
        raise ValueError(f"{row['setting_id']} / {method} has no safe Smart Remote auto-value strategy")
    if apis_seen and strategy.available_getter not in apis_seen:
        raise settings_registry.SettingControlUnavailable(
            f"{strategy.available_getter} is not available after session unlock; cannot choose "
            f"a camera-advertised value for {row['setting_id']}"
        )
    if strategy.current_getter and apis_seen and strategy.current_getter not in apis_seen:
        raise settings_registry.SettingControlUnavailable(
            f"{strategy.current_getter} is not available after session unlock; cannot compare "
            f"current value for {row['setting_id']} before choosing an alternate"
        )
    available_response = remote.call(strategy.available_getter, [])
    current_response = remote.call(strategy.current_getter, []) if strategy.current_getter else None
    chosen = settings_smart_remote_values.choose_first_available_alternate(
        strategy,
        available_response,
        current_response,
    )
    return chosen, {
        "mode": mode,
        "strategy": {
            "setting_id": strategy.setting_id,
            "method": strategy.method,
            "available_getter": strategy.available_getter,
            "current_getter": strategy.current_getter,
            "notes": strategy.notes,
        },
        "available_response": available_response,
        "current_response": current_response,
    }


def _route_method(row: dict[str, Any]) -> str | None:
    route = row.get("routes", {}).get("smart_remote_wifi", {})
    method = route.get("method") or route.get("method_hint")
    return str(method) if method else None


def _validate_method(method: str, *, dry_run: bool) -> None:
    if not method or method == a7ii_smart_remote_behavior_proof.METHOD_PLACEHOLDER:
        if dry_run:
            return
        raise ValueError("--method is required when the registry row does not already name a setter")
    if not method.startswith(SETTER_PREFIXES):
        raise ValueError(f"Smart Remote method {method!r} is not a recognized setter/action prefix")


def _validate_behavior_observation_inputs(*, observed_before: str, observed_after: str) -> None:
    placeholders = {
        "",
        a7ii_smart_remote_behavior_proof.OBSERVED_BEFORE_PLACEHOLDER,
        a7ii_smart_remote_behavior_proof.OBSERVED_AFTER_PLACEHOLDER,
    }
    if observed_before.strip() in placeholders:
        raise ValueError("--observed-before must be supplied before live behavior proof execution")
    if observed_after.strip() in placeholders:
        raise ValueError("--observed-after must be supplied before live behavior proof execution")


def _connect_remote(factory: Callable[[], Any] | None) -> Any:
    if factory is not None:
        return factory()
    import wifi_a7ii

    remote = wifi_a7ii.connect()
    if remote is None:
        raise settings_registry.SettingControlUnavailable(
            "Smart Remote camera endpoint is not reachable. Launch Smart Remote on the A7 II "
            "and join its DIRECT Wi-Fi AP."
        )
    return remote


def _write_attempt(
    *,
    out_dir: Path,
    stamp: str,
    setting_id: str,
    payload: dict[str, Any],
) -> Path:
    attempt_dir = out_dir / f"{stamp}-{setting_id.replace('.', '_')}-smart-remote-set-observe"
    attempt_dir.mkdir(parents=True, exist_ok=True)
    attempt_path = attempt_dir / "smart-remote-setter-attempt.json"
    attempt_path.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")
    lines = [
        f"# Smart Remote Setter Attempt: {payload['label']}",
        "",
        f"- setting_id: `{payload['setting_id']}`",
        f"- method: `{payload['request']['method']}`",
        f"- ok: `{payload['ok']}`",
        f"- executed: `{payload['executed']}`",
        f"- allow_camera_write: `{payload['allow_camera_write']}`",
        f"- behavior_proof: `{payload.get('behavior_proof_artifact') or ''}`",
        "",
        "```json",
        json.dumps(payload.get("response") or payload.get("error") or {}, indent=2, sort_keys=True),
        "```",
        "",
    ]
    (attempt_dir / "summary.md").write_text("\n".join(lines), encoding="utf-8")
    return attempt_path


def build_report(
    *,
    setting_id: str,
    value: str | None = None,
    auto_value: str | None = None,
    method: str | None = None,
    execute: bool = False,
    allow_camera_write: bool = False,
    observed_before: str = "",
    observed_after: str = "",
    human_verdict: str | None = None,
    observed_behavior: str = "",
    source_artifacts: list[str] | None = None,
    photos: list[str] | None = None,
    reviewer: str = "operator",
    notes: list[str] | None = None,
    out_dir: Path | None = None,
    registry: dict[str, Any] | None = None,
    smart_remote_factory: Callable[[], Any] | None = None,
) -> tuple[dict[str, Any], Path]:
    if bool(value) == bool(auto_value):
        raise ValueError("supply exactly one of --value or --auto-value")
    registry = registry or settings_registry.load_registry()
    row = settings_registry.get_setting(setting_id, registry)
    if "smart_remote_wifi" not in row.get("routes", {}):
        raise ValueError(f"{setting_id} is not registered for Smart Remote Wi-Fi")
    planned_value = value if value is not None else f"auto:{auto_value}"
    planned = settings_registry.plan_setting_control(
        setting_id,
        planned_value,
        route="smart_remote_wifi",
        dry_run=True,
        registry=registry,
    ).to_dict()
    if planned.get("guarded"):
        raise settings_registry.SettingGuardError(
            f"{setting_id} is guarded as {row['status']}; Smart Remote writes are disabled."
        )

    route = row["routes"]["smart_remote_wifi"]
    selected_method = (method or _route_method(row) or "").strip()
    _validate_method(selected_method, dry_run=not execute)
    parsed_value: Any = _parse_value(value) if value is not None else None
    params = _smart_remote_params(selected_method, parsed_value) if selected_method and value is not None else []
    source_artifacts = source_artifacts or []
    photos = photos or []
    notes = notes or []
    out_dir = out_dir or REPO_ROOT / "artifacts" / "falsification" / "r1_api_surface"
    stamp = f"{time.strftime('%Y%m%d-%H%M%S')}-{time.time_ns() % 1_000_000_000:09d}"

    attempt = {
        "tool": "a7ii_smart_remote_set_observe",
        "timestamp": stamp,
        "setting_id": row["setting_id"],
        "label": row["label"],
        "route": "smart_remote_wifi",
        "status": row["status"],
        "route_status": route.get("status"),
        "control_plan": planned,
        "method_source": "argument" if method else "registry",
        "request": {
            "method": selected_method,
            "params": params,
            "value": parsed_value,
            "value_source": "argument" if value is not None else "auto",
            "auto_value": auto_value,
        },
        "execute": execute,
        "executed": False,
        "allow_camera_write": allow_camera_write,
        "ok": False,
        "apis_seen": [],
        "response": None,
        "error": None,
        "behavior_proof_artifact": None,
        "auto_value_evidence": None,
        "next_action": row["next_action"],
    }
    if not execute:
        attempt["ok"] = True
        attempt_path = out_dir / f"{stamp}-{setting_id.replace('.', '_')}-smart-remote-set-observe" / "smart-remote-setter-attempt.json"
        return attempt, attempt_path
    if not allow_camera_write:
        raise ValueError("--execute requires --allow-camera-write")
    if human_verdict:
        _validate_behavior_observation_inputs(
            observed_before=observed_before,
            observed_after=observed_after,
        )

    try:
        remote = _connect_remote(smart_remote_factory)
        apis = remote.begin_session()
        attempt["apis_seen"] = [str(api) for api in apis or []]
        if attempt["apis_seen"] and selected_method not in attempt["apis_seen"]:
            raise settings_registry.SettingControlUnavailable(
                f"Smart Remote method {selected_method} is not available after session unlock"
            )
        if auto_value:
            parsed_value, auto_evidence = _resolve_auto_value(
                remote=remote,
                row=row,
                method=selected_method,
                mode=auto_value,
                apis_seen=attempt["apis_seen"],
            )
            params = _smart_remote_params(selected_method, parsed_value)
            attempt["request"]["value"] = parsed_value
            attempt["request"]["params"] = params
            attempt["auto_value_evidence"] = auto_evidence
        response = remote.call(selected_method, params)
        attempt["executed"] = True
        attempt["response"] = response
        attempt["ok"] = isinstance(response, dict) and "error" not in response
    except Exception as exc:
        attempt["executed"] = True
        attempt["error"] = {
            "type": exc.__class__.__name__,
            "message": str(exc),
        }
        attempt["ok"] = False

    attempt_path = _write_attempt(
        out_dir=out_dir,
        stamp=stamp,
        setting_id=setting_id,
        payload=attempt,
    )
    if human_verdict and attempt["ok"]:
        proof_sources = [*source_artifacts, str(attempt_path)]
        proof_report, proof_path = a7ii_smart_remote_behavior_proof.build_report(
            setting_id=setting_id,
            method=selected_method,
            requested_value=str(parsed_value),
            observed_before=observed_before,
            observed_after=observed_after,
            observed_behavior=observed_behavior,
            human_verdict=human_verdict,
            source_artifacts=proof_sources,
            photos=photos,
            reviewer=reviewer,
            notes=notes,
            out_dir=out_dir,
        )
        attempt["behavior_proof_artifact"] = _repo_path(proof_path)
        attempt["behavior_proof"] = proof_report
        attempt_path.write_text(json.dumps(attempt, indent=2, sort_keys=True), encoding="utf-8")
    return attempt, attempt_path


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--setting", required=True)
    value_group = parser.add_mutually_exclusive_group(required=True)
    value_group.add_argument("--value", help="value to send; JSON objects/lists are accepted")
    value_group.add_argument(
        "--auto-value",
        choices=[settings_smart_remote_values.AUTO_VALUE_MODE],
        help=(
            "choose a camera-advertised alternate value after getAvailable*/current getter checks; "
            "the command fails before writing if the value cannot be resolved"
        ),
    )
    parser.add_argument("--method", help="Smart Remote setter/action method; required for unwrapped candidates")
    parser.add_argument("--execute", action="store_true", help="call the camera instead of only planning")
    parser.add_argument("--allow-camera-write", action="store_true", help="required with --execute")
    parser.add_argument("--observed-before", default="", help="camera/app/getter value before the setter")
    parser.add_argument("--observed-after", default="", help="camera/app/getter value after the setter")
    parser.add_argument(
        "--human-verdict",
        choices=["proven", "inconclusive", "rejected"],
        help="write a behavior-proof marker after a successful setter attempt",
    )
    parser.add_argument("--observed-behavior", default="")
    parser.add_argument("--source-artifact", action="append", default=[])
    parser.add_argument("--photo", action="append", default=[])
    parser.add_argument("--reviewer", default="operator")
    parser.add_argument("--note", action="append", default=[])
    parser.add_argument("--out-dir", default="artifacts/falsification/r1_api_surface")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)

    try:
        report, report_path = build_report(
            setting_id=args.setting,
            value=args.value,
            auto_value=args.auto_value,
            method=args.method,
            execute=args.execute,
            allow_camera_write=args.allow_camera_write,
            observed_before=args.observed_before,
            observed_after=args.observed_after,
            human_verdict=args.human_verdict,
            observed_behavior=args.observed_behavior,
            source_artifacts=args.source_artifact,
            photos=args.photo,
            reviewer=args.reviewer,
            notes=args.note,
            out_dir=REPO_ROOT / args.out_dir,
        )
    except (ValueError, settings_registry.RegistryError) as exc:
        parser.error(str(exc))
    if args.json or not args.execute:
        print(json.dumps({"report_path": str(report_path), "report": report}, indent=2, sort_keys=True))
    else:
        print(report_path.parent)
    return 0 if report.get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
