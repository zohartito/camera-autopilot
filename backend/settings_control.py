"""Route-aware setting control.

This is the thin execution layer above `settings_registry`. It keeps dry-run planning cheap and
camera-free, and only touches hardware for a live route that has an implemented backend.
"""

from __future__ import annotations

import json
import os
import shlex
import subprocess
import time
from collections.abc import Callable
from typing import Any

import settings_registry

USB_WRITABLE_ROUTE_STATUSES = {"proven_set", "available_not_wrapped", "protocol_ok_needs_behavior"}
USB_READABLE_ROUTE_STATUSES = {"proven_set", "proven_read", "available_not_wrapped", "protocol_ok_needs_behavior"}
SMART_REMOTE_WRITABLE_ROUTE_STATUSES = {"proven_set", "available_not_wrapped"}
RAW_PTP_WRITABLE_ROUTE_STATUSES = {"proven_set", "protocol_ok_needs_behavior"}
RAW_PTP_SETTING_OPERATIONS = {
    "camera.focus_area": "af_point",
    "camera.focus_settings": "manual_focus",
}
DEFAULT_RAW_PTP_PYTHON = "/tmp/pmca-venv/bin/python"
DEFAULT_RAW_PTP_ARTIFACT_ROOT = (
    settings_registry.REPO_ROOT / "artifacts" / "falsification" / "r2_usb_ptp" / "setting_api"
)


def _smart_remote_method(route_info: dict[str, Any]) -> str | None:
    method = route_info.get("method") or route_info.get("method_hint")
    return str(method) if method else None


def _write_backend(row: dict[str, Any], route: str, *, allow_unproven: bool = False) -> dict[str, Any] | None:
    route_info = row.get("routes", {}).get(route)
    if not route_info:
        return None
    route_status = route_info.get("status")
    if route == "smart_remote_wifi":
        method = _smart_remote_method(route_info)
        route_is_allowed = route_status == "proven_set" or (
            allow_unproven and route_status in SMART_REMOTE_WRITABLE_ROUTE_STATUSES
        )
        if method and route_is_allowed:
            return {
                "backend": "smart_remote_wifi",
                "route_status": route_status,
                "method": method,
                "behavior_proven": route_status == "proven_set",
            }
    if route == "usb_gphoto":
        node = route_info.get("node")
        route_is_allowed = route_status == "proven_set" or (
            allow_unproven and route_status in USB_WRITABLE_ROUTE_STATUSES
        )
        if node and route_is_allowed:
            return {
                "backend": "usb_gphoto",
                "route_status": route_status,
                "node": node,
                "behavior_proven": route_status == "proven_set",
            }
    if route == "raw_ptp":
        operation = RAW_PTP_SETTING_OPERATIONS.get(row["setting_id"])
        route_is_allowed = route_status == "proven_set" or (
            allow_unproven and route_status in RAW_PTP_WRITABLE_ROUTE_STATUSES
        )
        if operation and route_is_allowed:
            return {
                "backend": "raw_ptp",
                "route_status": route_status,
                "operation": operation,
                "propcode": route_info.get("propcode"),
                "behavior_proven": route_status == "proven_set",
            }
    return None


def _select_live_route(row: dict[str, Any], requested_route: str, registry: dict[str, Any]) -> str:
    if requested_route != "auto":
        if requested_route not in row["routes"]:
            raise settings_registry.SettingControlUnavailable(
                f"{row['setting_id']} has no registered route {requested_route!r}"
            )
        return requested_route
    for route in settings_registry.route_priority(registry):
        if _write_backend(row, route):
            return route
    for route in settings_registry.route_priority(registry):
        if _write_backend(row, route, allow_unproven=True):
            return route
    return settings_registry.choose_route(row, "auto", registry)


def _annotate_live_write_plan(
    control_plan: dict[str, Any],
    row: dict[str, Any],
    route: str | None,
    *,
    allow_unproven_backend: bool = False,
) -> dict[str, Any]:
    out = dict(control_plan)
    backend = _write_backend(row, route, allow_unproven=allow_unproven_backend) if route else None
    out["live_backend"] = backend["backend"] if backend else None
    out["route_status"] = backend["route_status"] if backend else None
    if backend and "node" in backend:
        out["node"] = backend["node"]
    if backend and "method" in backend:
        out["method"] = backend["method"]
    if backend and "operation" in backend:
        out["operation"] = backend["operation"]
    if backend and backend.get("propcode"):
        out["propcode"] = backend["propcode"]
    out["behavior_proven"] = bool(backend and backend["behavior_proven"])
    out["requires_behavior_proof"] = bool(backend and not backend["behavior_proven"])
    out["can_attempt_live_write"] = bool(backend and not out["guarded"])
    return out


def _select_read_route(row: dict[str, Any], requested_route: str, registry: dict[str, Any]) -> str | None:
    if requested_route != "auto":
        if requested_route not in row["routes"]:
            raise settings_registry.SettingControlUnavailable(
                f"{row['setting_id']} has no registered route {requested_route!r}"
            )
        return requested_route
    for route in settings_registry.route_priority(registry):
        route_info = row["routes"].get(route)
        if route == "usb_gphoto" and route_info and route_info.get("node"):
            if route_info.get("status") in USB_READABLE_ROUTE_STATUSES:
                return route
    return settings_registry.choose_route(row, "auto", registry)


def _read_usb_node(camera: Any, node: str) -> str | None:
    if hasattr(camera, "read_setting"):
        value = camera.read_setting(node)
        return None if value is None else str(value)
    gp_camera = camera._require()
    widget = gp_camera.get_config().get_child_by_name(node)
    value = widget.get_value()
    return None if value is None else str(value)


def read_setting(
    setting_id: str,
    *,
    route: str = "auto",
    live: bool = False,
    registry: dict[str, Any] | None = None,
    camera_factory: Callable[[], Any] | None = None,
) -> dict[str, Any]:
    """Read a setting value.

    By default this is camera-free and returns the photographed registry value. A live read must be
    explicitly requested and is currently implemented only for USB/gphoto rows with concrete nodes.
    """
    registry = registry or settings_registry.load_registry()
    row = settings_registry.get_setting(setting_id, registry)
    selected_route = _select_read_route(row, route, registry)
    base = {
        "ok": True,
        "setting_id": setting_id,
        "label": row["label"],
        "route": selected_route,
        "live": live,
        "status": row["status"],
        "current": {
            "value": row["current_photo_value"],
            "source": "registry_photo",
            "photo_sources": row["photo_sources"],
            "greyed_in_photo": row["greyed_in_photo"],
        },
        "next_action": row["next_action"],
    }
    if not live:
        return base
    if selected_route != "usb_gphoto":
        raise settings_registry.SettingControlUnavailable(
            f"{setting_id} route {selected_route} has no live read backend yet. "
            f"Next action: {row['next_action']}"
        )
    route_info = row["routes"].get("usb_gphoto", {})
    node = route_info.get("node")
    route_status = route_info.get("status")
    if not node or route_status not in USB_READABLE_ROUTE_STATUSES:
        raise settings_registry.SettingControlUnavailable(
            f"{setting_id} has no live-readable USB/gphoto node. Next action: {row['next_action']}"
        )
    if camera_factory is None:
        from sonyremote import Camera  # lazy import for camera-free tests

        camera_factory = Camera
    with camera_factory() as camera:
        value = _read_usb_node(camera, str(node))
    base["current"] = {
        "value": value,
        "source": "usb_gphoto",
        "node": node,
        "greyed_in_photo": row["greyed_in_photo"],
    }
    return base


def apply_setting(
    setting_id: str,
    value: Any,
    *,
    route: str = "auto",
    dry_run: bool = True,
    allow_behavior_probe: bool = False,
    registry: dict[str, Any] | None = None,
    camera_factory: Callable[[], Any] | None = None,
    smart_remote_factory: Callable[[], Any] | None = None,
    raw_ptp_runner: Callable[[list[str]], dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Plan or apply a setting write.

    Live dispatch supports behavior-proven Smart Remote setters and USB/gphoto rows with concrete
    nodes. Other routes still return explicit unavailable errors until their behavior-proof gates
    are complete.
    """
    registry = registry or settings_registry.load_registry()
    row = settings_registry.get_setting(setting_id, registry)
    selected_route = _select_live_route(row, route, registry)
    plan = settings_registry.plan_setting_control(
        setting_id,
        value,
        route=selected_route,
        dry_run=True,
        registry=registry,
    )
    explicit_route = route != "auto"
    control_plan = _annotate_live_write_plan(
        plan.to_dict(),
        row,
        selected_route,
        allow_unproven_backend=True,
    )
    control_plan["allow_behavior_probe"] = allow_behavior_probe
    if dry_run:
        return {"ok": True, "control_plan": control_plan, "applied": None}
    if plan.guarded:
        raise settings_registry.SettingGuardError(
            f"{setting_id} is guarded as {row['status']}; non-dry-run writes are disabled. "
            f"Next action: {row['next_action']}"
        )
    if control_plan.get("requires_behavior_proof") and not allow_behavior_probe:
        raise settings_registry.SettingBehaviorProofRequired(
            f"{setting_id} route {selected_route} is not behavior-proven yet; "
            "rerun as an explicit behavior probe with allow_behavior_probe=true. "
            f"Next action: {row['next_action']}"
        )
    if selected_route == "smart_remote_wifi":
        return _apply_smart_remote(
            setting_id,
            value,
            row,
            control_plan,
            smart_remote_factory=smart_remote_factory,
        )
    if selected_route == "raw_ptp":
        return _apply_raw_ptp(
            setting_id,
            value,
            row,
            control_plan,
            raw_ptp_runner=raw_ptp_runner,
        )
    if selected_route != "usb_gphoto":
        raise settings_registry.SettingControlUnavailable(
            f"{setting_id} route {selected_route} has no live backend yet. "
            f"Next action: {row['next_action']}"
        )

    route_info = row["routes"].get("usb_gphoto", {})
    node = route_info.get("node")
    route_status = route_info.get("status")
    if not node or route_status not in USB_WRITABLE_ROUTE_STATUSES:
        raise settings_registry.SettingControlUnavailable(
            f"{setting_id} has no behavior-approved USB/gphoto setter. "
            f"Next action: {row['next_action']}"
        )

    if camera_factory is None:
        from sonyremote import Camera  # lazy import: tests can stay camera-free

        camera_factory = Camera

    with camera_factory() as camera:
        applied = camera.set_setting(str(node), str(value))
    control_plan["can_attempt_live_write"] = True
    return {
        "ok": True,
        "control_plan": control_plan,
        "applied": {
            "route": "usb_gphoto",
            "node": node,
            "value": applied,
        },
    }


def _smart_remote_params(method: str, value: Any) -> list[Any]:
    if method == "setTouchAFPosition":
        if isinstance(value, dict):
            if "x" not in value or "y" not in value:
                raise settings_registry.SettingControlUnavailable(
                    "setTouchAFPosition expects {'x': 0..100, 'y': 0..100}"
                )
            return [value["x"], value["y"]]
        if isinstance(value, (list, tuple)) and len(value) == 2:
            return [value[0], value[1]]
        raise settings_registry.SettingControlUnavailable(
            "setTouchAFPosition expects {'x': 0..100, 'y': 0..100} or [x, y]"
        )
    if method == "setIsoSpeedRate":
        return [str(value)]
    if method == "setWhiteBalance":
        if isinstance(value, dict):
            mode = value.get("whiteBalanceMode") or value.get("mode")
            color_temperature = value.get("colorTemperature", -1)
            if not mode:
                raise settings_registry.SettingControlUnavailable(
                    "setWhiteBalance expects whiteBalanceMode/mode in JSON object"
                )
            return [mode, False, color_temperature]
        if isinstance(value, str):
            return [value, False, -1]
        if isinstance(value, (list, tuple)) and len(value) == 3:
            return list(value)
        raise settings_registry.SettingControlUnavailable(
            "setWhiteBalance expects mode string, JSON object, or [mode, false, colorTemperature]"
        )
    if isinstance(value, (list, tuple)):
        return list(value)
    return [value]


def _apply_smart_remote(
    setting_id: str,
    value: Any,
    row: dict[str, Any],
    control_plan: dict[str, Any],
    *,
    smart_remote_factory: Callable[[], Any] | None = None,
) -> dict[str, Any]:
    route_info = row["routes"].get("smart_remote_wifi", {})
    method = _smart_remote_method(route_info)
    route_status = route_info.get("status")
    if not method or route_status not in SMART_REMOTE_WRITABLE_ROUTE_STATUSES:
        raise settings_registry.SettingControlUnavailable(
            f"{setting_id} has no behavior-approved Smart Remote setter. "
            f"Next action: {row['next_action']}"
        )

    if smart_remote_factory is None:
        import wifi_a7ii  # lazy import: keeps camera-free tests cheap

        def smart_remote_factory() -> Any:
            remote = wifi_a7ii.connect()
            if remote is None:
                raise settings_registry.SettingControlUnavailable(
                    "Smart Remote camera endpoint is not reachable. "
                    "Launch Smart Remote on the A7 II and join its DIRECT Wi-Fi AP."
                )
            return remote

    remote = smart_remote_factory()
    apis = remote.begin_session()
    if apis and method not in apis:
        raise settings_registry.SettingControlUnavailable(
            f"Smart Remote method {method} is not available after session unlock. "
            f"Next action: {row['next_action']}"
        )
    params = _smart_remote_params(str(method), value)
    result = remote.call(str(method), params)
    live_plan = dict(control_plan)
    live_plan["can_attempt_live_write"] = True
    return {
        "ok": True,
        "control_plan": live_plan,
        "applied": {
            "route": "smart_remote_wifi",
            "method": method,
            "params": params,
            "result": result,
        },
    }


def _raw_ptp_python() -> str:
    return os.getenv("RAW_PTP_PYTHON", DEFAULT_RAW_PTP_PYTHON)


def _int_value(value: Any, *, name: str) -> int:
    try:
        return int(value, 0) if isinstance(value, str) else int(value)
    except (TypeError, ValueError) as exc:
        raise settings_registry.SettingControlUnavailable(f"raw PTP {name} must be an integer") from exc


def _raw_ptp_command(setting_id: str, value: Any) -> tuple[str, list[str], dict[str, Any]]:
    python_bin = _raw_ptp_python()
    base = [python_bin, "tests/probe_raw_control.py", "--no-precondition"]
    if setting_id == "camera.focus_area":
        if isinstance(value, dict):
            x = _int_value(value.get("x", 320), name="x")
            y = _int_value(value.get("y", 240), name="y")
            focus_area = value.get("focus_area", "0x102")
        elif isinstance(value, (list, tuple)) and len(value) == 2:
            x = _int_value(value[0], name="x")
            y = _int_value(value[1], name="y")
            focus_area = "0x102"
        else:
            raise settings_registry.SettingControlUnavailable(
                "raw PTP focus area expects {'x': int, 'y': int, 'focus_area': '0x102'} or [x, y]"
            )
        command = base + ["--w1", "--af-x", str(x), "--af-y", str(y), "--focus-area", str(focus_area)]
        return "af_point", command, {"x": x, "y": y, "focus_area": str(focus_area)}

    if setting_id == "camera.focus_settings":
        if isinstance(value, dict):
            step = _int_value(value.get("step", 3), name="step")
            return_after = bool(value.get("return_after", True))
        else:
            step = _int_value(value, name="step")
            return_after = True
        command = base + ["--w2", "--mf-step", str(step)]
        if not return_after:
            command.append("--no-return")
        return "manual_focus", command, {"step": step, "return_after": return_after}

    raise settings_registry.SettingControlUnavailable(
        f"{setting_id} has no raw PTP setting-level behavior probe. "
        "Use the route playbook for broader raw PTP evidence."
    )


def _default_raw_ptp_runner(command: list[str]) -> dict[str, Any]:
    DEFAULT_RAW_PTP_ARTIFACT_ROOT.mkdir(parents=True, exist_ok=True)
    stamp = f"{time.strftime('%Y%m%d-%H%M%S')}-{time.time_ns()}"
    artifact_path = DEFAULT_RAW_PTP_ARTIFACT_ROOT / f"{stamp}-raw-ptp-setting-probe.json"
    started = time.time()
    try:
        proc = subprocess.run(
            command,
            cwd=settings_registry.REPO_ROOT,
            text=True,
            capture_output=True,
            timeout=60,
            check=False,
        )
        payload = {
            "command": command,
            "returncode": proc.returncode,
            "timed_out": False,
            "stdout": proc.stdout,
            "stderr": proc.stderr,
            "elapsed_s": round(time.time() - started, 3),
        }
    except subprocess.TimeoutExpired as exc:
        payload = {
            "command": command,
            "returncode": None,
            "timed_out": True,
            "stdout": exc.stdout or "",
            "stderr": exc.stderr or "raw PTP setting probe timed out after 60 seconds",
            "elapsed_s": round(time.time() - started, 3),
        }
    artifact_path.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")
    payload["artifact"] = str(artifact_path.relative_to(settings_registry.REPO_ROOT))
    return payload


def _apply_raw_ptp(
    setting_id: str,
    value: Any,
    row: dict[str, Any],
    control_plan: dict[str, Any],
    *,
    raw_ptp_runner: Callable[[list[str]], dict[str, Any]] | None = None,
) -> dict[str, Any]:
    route_info = row["routes"].get("raw_ptp", {})
    route_status = route_info.get("status")
    if route_status not in RAW_PTP_WRITABLE_ROUTE_STATUSES:
        raise settings_registry.SettingControlUnavailable(
            f"{setting_id} has no behavior-approved raw PTP route. "
            f"Next action: {row['next_action']}"
        )
    operation, command, params = _raw_ptp_command(setting_id, value)
    runner = raw_ptp_runner or _default_raw_ptp_runner
    result = runner(command)
    live_plan = dict(control_plan)
    live_plan["can_attempt_live_write"] = True
    live_plan["raw_ptp_command"] = " ".join(shlex.quote(part) for part in command)
    return {
        "ok": result.get("returncode") == 0,
        "control_plan": live_plan,
        "applied": {
            "route": "raw_ptp",
            "operation": operation,
            "params": params,
            "command": command,
            "result": result,
        },
    }
