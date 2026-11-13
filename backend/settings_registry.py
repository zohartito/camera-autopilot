"""A7 II settings registry and route planner.

The registry is the source of truth for every photographed setting. It is intentionally
data-first: hardware-facing code can attach real setters route by route, while product UI and
tests can already reason about ownership, preconditions, guardrails, and proof artifacts.

The data file is JSON-compatible YAML, parsed with the stdlib `json` module so this layer does not
add a dependency just to validate the registry.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_REGISTRY_PATH = REPO_ROOT / "data" / "a7ii_settings_registry.yaml"

STATUSES = {
    "proven_set",
    "proven_read",
    "available_not_wrapped",
    "protocol_ok_needs_behavior",
    "state_gated",
    "manual_physical_gate",
    "dangerous_admin",
    "unsupported_on_a7ii",
    "needs_probe",
}

REQUIRED_ROW_FIELDS = {
    "setting_id",
    "label",
    "menu_group",
    "menu_page",
    "photo_sources",
    "current_photo_value",
    "greyed_in_photo",
    "preconditions",
    "routes",
    "proof_artifacts",
    "next_action",
    "status",
}

DESTRUCTIVE_SETTING_IDS = {
    "setup.format",
    "setup.setting_reset",
    "playback.delete",
    "playback.rotate",
    "playback.protect",
    "playback.specify_printing",
    "setup.recover_image_db",
    "setup.select_rec_folder",
    "setup.new_folder",
    "wireless.ssid_pw_reset",
    "wireless.reset_network_set",
}

WRITE_READY_STATUSES = {"proven_set"}
READ_READY_STATUSES = {"proven_set", "proven_read", "available_not_wrapped"}
# Route 3 was deliberately retired.  Keep its historical rows for ownership and
# UI context, but never choose it as a control transport or treat an old proof
# as current authority.
DISABLED_ROUTES = frozenset({"openmemories"})

STALE_PROVEN_SET_ACTION_PREFIXES = (
    "expose ",
    "implement ",
    "probe ",
    "set ",
)
STALE_PROVEN_SET_ACTION_PHRASES = (
    "before product write readiness",
    "before promoting",
    "lacks current set/read/revert proof",
    "lacks set/read/revert proof",
    "needs a fresh observed before/after behavior artifact",
)


class RegistryError(RuntimeError):
    """Base error for registry loading, validation, and control planning."""


class RegistryValidationError(RegistryError):
    """Raised when the registry file is internally inconsistent."""


class SettingNotFoundError(RegistryError):
    """Raised when a requested setting id is not in the registry."""


class SettingGuardError(RegistryError):
    """Raised when a request would perform a destructive/admin action."""


class SettingControlUnavailable(RegistryError):
    """Raised when the registry has a route hypothesis but no live setter yet."""


class SettingBehaviorProofRequired(SettingControlUnavailable):
    """Raised when a live write is possible only as an explicit behavior-proof probe."""


def proven_set_next_action_is_stale(next_action: str) -> bool:
    """Return whether a proven setter still asks agents to repeat its accepted proof."""
    normalized = str(next_action).strip().lower()
    return normalized.startswith(STALE_PROVEN_SET_ACTION_PREFIXES) or any(
        phrase in normalized for phrase in STALE_PROVEN_SET_ACTION_PHRASES
    )


@dataclass(frozen=True)
class ControlPlan:
    setting_id: str
    label: str
    value: Any
    route: str | None
    dry_run: bool
    status: str
    can_attempt_live_write: bool
    guarded: bool
    command_hint: str | None
    next_action: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "setting_id": self.setting_id,
            "label": self.label,
            "value": self.value,
            "route": self.route,
            "dry_run": self.dry_run,
            "status": self.status,
            "can_attempt_live_write": self.can_attempt_live_write,
            "guarded": self.guarded,
            "command_hint": self.command_hint,
            "next_action": self.next_action,
        }


def load_registry(path: str | Path | None = None) -> dict[str, Any]:
    """Load and validate the settings registry."""
    registry_path = Path(path) if path is not None else DEFAULT_REGISTRY_PATH
    raw = json.loads(registry_path.read_text(encoding="utf-8"))
    validate_registry(raw)
    return raw


def validate_registry(registry: dict[str, Any]) -> None:
    """Validate registry shape and invariants that keep gap tracking honest."""
    if not isinstance(registry, dict):
        raise RegistryValidationError("registry root must be an object")
    settings = registry.get("settings")
    if not isinstance(settings, list) or not settings:
        raise RegistryValidationError("registry.settings must be a non-empty list")
    priority = registry.get("route_priority")
    if not isinstance(priority, list) or not all(isinstance(route, str) for route in priority):
        raise RegistryValidationError("registry.route_priority must be a list of route names")

    seen: set[str] = set()
    errors: list[str] = []
    for index, row in enumerate(settings):
        if not isinstance(row, dict):
            errors.append(f"settings[{index}] must be an object")
            continue
        missing = sorted(REQUIRED_ROW_FIELDS - set(row))
        if missing:
            errors.append(f"{row.get('setting_id', f'settings[{index}]')} missing fields: {missing}")
            continue
        setting_id = row["setting_id"]
        if not isinstance(setting_id, str) or "." not in setting_id:
            errors.append(f"{setting_id!r} must be a namespaced string like group.name")
        if setting_id in seen:
            errors.append(f"duplicate setting_id: {setting_id}")
        seen.add(setting_id)

        status = row["status"]
        if status not in STATUSES:
            errors.append(f"{setting_id} has invalid status {status!r}")
        if row["greyed_in_photo"] is not True and row["greyed_in_photo"] is not False:
            errors.append(f"{setting_id} greyed_in_photo must be boolean")
        for field in ("photo_sources", "preconditions", "proof_artifacts"):
            if not isinstance(row[field], list):
                errors.append(f"{setting_id} {field} must be a list")
        if not isinstance(row["routes"], dict) or not row["routes"]:
            errors.append(f"{setting_id} routes must be a non-empty object")
        if not str(row["next_action"]).strip():
            errors.append(f"{setting_id} next_action must be concrete")
        disabled_route_notice = "permanently disabled" in row["next_action"].lower()
        if status == "needs_probe" and "probe" not in row["next_action"].lower() and not disabled_route_notice:
            errors.append(f"{setting_id} needs_probe rows must name a probe in next_action")
        if status == "proven_set" and proven_set_next_action_is_stale(row["next_action"]):
            errors.append(
                f"{setting_id} is proven_set but next_action asks to repeat accepted proof"
            )
        for route_name, route in row["routes"].items():
            if not isinstance(route, dict):
                errors.append(f"{setting_id}.{route_name} route must be an object")
                continue
            route_status = route.get("status")
            if route_status not in STATUSES:
                errors.append(f"{setting_id}.{route_name} has invalid route status {route_status!r}")
            if route_name in DISABLED_ROUTES and route_status in {"proven_set", "proven_read"}:
                errors.append(f"{setting_id}.{route_name} is disabled and cannot be {route_status}")

    if errors:
        raise RegistryValidationError("\n".join(errors))


def settings_by_id(registry: dict[str, Any] | None = None) -> dict[str, dict[str, Any]]:
    registry = registry or load_registry()
    return {row["setting_id"]: row for row in registry["settings"]}


def get_setting(setting_id: str, registry: dict[str, Any] | None = None) -> dict[str, Any]:
    rows = settings_by_id(registry)
    try:
        return rows[setting_id]
    except KeyError as exc:
        raise SettingNotFoundError(f"unknown setting_id: {setting_id}") from exc


def list_settings(
    *,
    menu_group: str | None = None,
    status: str | None = None,
    registry: dict[str, Any] | None = None,
) -> list[dict[str, Any]]:
    registry = registry or load_registry()
    rows = registry["settings"]
    if menu_group is not None:
        rows = [row for row in rows if row["menu_group"] == menu_group]
    if status is not None:
        rows = [row for row in rows if row["status"] == status]
    return rows


def route_priority(registry: dict[str, Any] | None = None) -> list[str]:
    registry = registry or load_registry()
    return list(registry["route_priority"])


def choose_route(
    row: dict[str, Any],
    requested_route: str = "auto",
    registry: dict[str, Any] | None = None,
) -> str | None:
    """Choose a route for a setting without touching hardware."""
    routes = row["routes"]
    if requested_route != "auto":
        if requested_route in DISABLED_ROUTES:
            raise SettingControlUnavailable(
                f"{requested_route} is permanently disabled and cannot be selected for control"
            )
        if requested_route not in routes:
            raise SettingControlUnavailable(
                f"{row['setting_id']} has no registered route {requested_route!r}"
            )
        return requested_route

    preferred_statuses: set[str] = set()
    if row["status"] == "proven_set":
        preferred_statuses = {"proven_set"}
    elif row["status"] == "proven_read":
        preferred_statuses = {"proven_set", "proven_read"}
    elif row["status"] == "available_not_wrapped":
        preferred_statuses = {"proven_set", "proven_read", "available_not_wrapped"}
    if preferred_statuses:
        for route in route_priority(registry):
            if route in DISABLED_ROUTES:
                continue
            if routes.get(route, {}).get("status") in preferred_statuses:
                return route

    for route in route_priority(registry):
        if route in DISABLED_ROUTES:
            continue
        if routes.get(route, {}).get("status") not in (None, "needs_probe"):
            return route

    for route in route_priority(registry):
        if route in DISABLED_ROUTES:
            continue
        if route in routes:
            return route
    return next((route for route in routes if route not in DISABLED_ROUTES), None)


def _command_hint(row: dict[str, Any], route_name: str | None, value: Any) -> str | None:
    if route_name is None:
        return None
    route = row["routes"].get(route_name, {})
    node = route.get("node")
    if route_name == "usb_gphoto" and node:
        return f"gphoto2 --set-config {node}={value}"
    method = route.get("method") or route.get("method_hint")
    if route_name == "smart_remote_wifi" and method:
        return f"Smart Remote JSON-RPC method {method} with value {value!r}"
    propcode = route.get("propcode")
    if route_name == "raw_ptp" and propcode:
        return f"raw PTP control write for {propcode} with behavior proof required"
    if route_name == "guided_physical":
        return "guided camera-state/physical-control route"
    return None


def plan_setting_control(
    setting_id: str,
    value: Any,
    *,
    route: str = "auto",
    dry_run: bool = True,
    registry: dict[str, Any] | None = None,
) -> ControlPlan:
    """Return the exact control plan for a setting write.

    This function is deliberately conservative: it plans and guards writes, but live hardware
    dispatch must be attached route-by-route after each setter is behavior-proven.
    """
    registry = registry or load_registry()
    row = get_setting(setting_id, registry)
    selected_route = choose_route(row, route, registry)
    guarded = row["status"] == "dangerous_admin" or setting_id in DESTRUCTIVE_SETTING_IDS
    can_attempt = row["status"] in WRITE_READY_STATUSES and not guarded
    plan = ControlPlan(
        setting_id=setting_id,
        label=row["label"],
        value=value,
        route=selected_route,
        dry_run=dry_run,
        status=row["status"],
        can_attempt_live_write=can_attempt,
        guarded=guarded,
        command_hint=_command_hint(row, selected_route, value),
        next_action=row["next_action"],
    )
    if dry_run:
        return plan
    if guarded:
        raise SettingGuardError(
            f"{setting_id} is guarded as {row['status']}; non-dry-run writes are disabled. "
            f"Next action: {row['next_action']}"
        )
    if not can_attempt:
        raise SettingControlUnavailable(
            f"{setting_id} is {row['status']} on route {selected_route}; live setter is not wired. "
            f"Next action: {row['next_action']}"
        )
    raise SettingControlUnavailable(
        f"{setting_id} has a proven route, but live route dispatch is intentionally not connected "
        "until the route backend opts in."
    )


def registry_summary(registry: dict[str, Any] | None = None) -> dict[str, Any]:
    registry = registry or load_registry()
    by_status = {status: 0 for status in sorted(STATUSES)}
    by_group: dict[str, int] = {}
    greyed = 0
    for row in registry["settings"]:
        by_status[row["status"]] += 1
        by_group[row["menu_group"]] = by_group.get(row["menu_group"], 0) + 1
        greyed += int(row["greyed_in_photo"])
    return {
        "schema_version": registry["schema_version"],
        "camera": registry["camera"],
        "total_settings": len(registry["settings"]),
        "greyed_in_photos": greyed,
        "by_status": by_status,
        "by_group": dict(sorted(by_group.items())),
        "route_priority": list(registry["route_priority"]),
    }


def registry_setup_checklist(registry: dict[str, Any] | None = None) -> list[tuple[str, str, str, str]]:
    """Return a camera_config-compatible checklist from registry rows."""
    registry = registry or load_registry()
    targets = {
        "camera.focus_mode": "AF-S",
        "camera.long_exposure_nr": "Off",
        "camera.smile_face_detect": "On",
        "camera.high_iso_nr": "Low",
        "camera.steadyshot": "Off on tripod",
        "camera.color_space": "sRGB",
    }
    rows = settings_by_id(registry)
    checklist: list[tuple[str, str, str, str]] = []
    for setting_id, target in targets.items():
        row = rows[setting_id]
        where = row["menu_page"].replace("_", " ")
        why = row["next_action"]
        checklist.append((row["label"], where, target, why))
    return checklist


if __name__ == "__main__":
    print(json.dumps(registry_summary(), indent=2, sort_keys=True))
