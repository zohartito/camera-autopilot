"""Precondition solver for greyed and state-gated A7 II settings.

The registry records the photographed menu state. This module turns each row's free-text
preconditions into a structured unlock plan: gate type, operator action, whether it is physical-only,
and the exact proof commands to run after changing state.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import settings_probe_plan
import settings_registry


@dataclass(frozen=True)
class GateRule:
    gate_type: str
    keywords: tuple[str, ...]
    action: str
    physical_only: bool = False
    route_disruptive: bool = False


RULES = (
    GateRule(
        "unsupported_model",
        ("rx100m4 only", "not a7 ii"),
        "Do not pursue on the A7 II; keep the row as a photo/compatibility record only.",
        physical_only=True,
    ),
    GateRule(
        "mode_dial",
        ("physical mode dial", "mode/dial", "superior auto", "memory recall mode"),
        "Move the physical mode dial or app shooting mode to the named position, then re-open this menu.",
        physical_only=True,
    ),
    GateRule(
        "movie_state",
        ("movie", "timecode", "audio-level", "audio monitoring", "audio recording"),
        "Enter a movie-capable state and rerun Smart Remote, USB, Command, and OpenMemories probes.",
    ),
    GateRule(
        "still_state",
        ("still-image", "still shutter", "still/movie"),
        "Use a still-image-capable shooting state such as M/A/S/P unless the row specifically asks for movie.",
    ),
    GateRule(
        "focus_state",
        ("af mode", "autofocus", "manual focus", "focus area", "focus magnifier", "continuous af", "phase detect"),
        "Set the body/lens focus mode and focus-area context named by the row, then rerun focus probes.",
    ),
    GateRule(
        "lens_or_adapter",
        ("lens", "adapter", "power zoom", "zoom-capable", "stabilized"),
        "Attach the required lens/adapter or enable the named lens/body feature before probing.",
        physical_only=True,
    ),
    GateRule(
        "rendering_format",
        ("jpeg", "raw", "file format", "movie file format", "profile-capable", "metadata"),
        "Switch file format/quality/rendering mode to the named JPEG/RAW/movie state, then probe again.",
    ),
    GateRule(
        "flash_state",
        ("flash", "red eye", "low-light af"),
        "Test in a flash-compatible still state, with flash hardware attached if the row requires it.",
        physical_only=True,
    ),
    GateRule(
        "storage_card",
        ("memory card", "card content", "folder", "file numbering", "file naming"),
        "Insert a card with suitable content/folders and rerun read/write/media probes.",
        physical_only=True,
    ),
    GateRule(
        "playback_state",
        ("playback", "selected image", "dpof"),
        "Enter playback mode, select a representative image, then rerun avContent/playback probes.",
    ),
    GateRule(
        "wireless_network",
        ("wireless", "network", "dlna", "tv", "access point", "wps"),
        "Enter the relevant wireless/network configuration state and avoid changing known-good routes until logged.",
    ),
    GateRule(
        "phone_app",
        ("phone app", "smart remote", "permissions/gps", "phone app ui", "phone app overlay"),
        "Connect the phone/Smart Remote app and grant the named phone-side permission before probing.",
    ),
    GateRule(
        "openmemories_app",
        ("openmemories", "tweak", "protection tab", "developer tab", "video tab", "region tab"),
        "Open the relevant OpenMemories/Tweak page, keep changes reversible, and record rollback state.",
    ),
    GateRule(
        "usb_mode",
        ("usb", "pc remote"),
        "Set USB Connection to PC Remote for USB probes; expect route drops when changing USB mode.",
        physical_only=True,
        route_disruptive=True,
    ),
    GateRule(
        "hdmi_state",
        ("hdmi",),
        "Attach/configure HDMI output only for an HDMI-specific proof run.",
        physical_only=True,
    ),
    GateRule(
        "display_state",
        ("display", "viewfinder", "finder", "monitor", "overlay", "peaking", "zebra", "marker", "guide"),
        "Enable the named display/overlay support state, then rerun menu and OpenMemories probes.",
    ),
    GateRule(
        "admin_or_destructive",
        ("delete", "reset", "cleaning", "format", "recover", "save to memory slot", "micro-adjust"),
        "Keep this guarded; record manual proof only unless the user gives explicit approval for the admin action.",
        physical_only=True,
    ),
    GateRule(
        "operator_ui",
        (
            "operator ui",
            "customization",
            "custom key",
            "dial setup",
            "dial exposure",
            "button",
            "menu display",
            "language",
            "clock",
            "region",
        ),
        "Treat as guided setup unless a reversible settings-storage route is proven.",
        physical_only=True,
    ),
    GateRule(
        "camera_app_menu",
        ("application menu", "app"),
        "Open the camera application menu/app state and record whether any remote route can observe it.",
    ),
    GateRule(
        "nfc_hardware",
        ("nfc",),
        "Record as a physical NFC/app-state gate; no remote setter is expected.",
        physical_only=True,
    ),
    GateRule(
        "ir_remote",
        ("ir remote",),
        "Enable/disable only as a guided setup item unless a reversible settings route is proven.",
        physical_only=True,
    ),
    GateRule(
        "sensor_cleaning",
        ("sensor cleaning",),
        "Do not trigger remotely; record manual-only proof if needed.",
        physical_only=True,
    ),
)


def _matching_rules(precondition: str) -> list[GateRule]:
    text = precondition.lower()
    matches = [rule for rule in RULES if any(keyword in text for keyword in rule.keywords)]
    if matches:
        return matches
    return [
        GateRule(
            "generic_camera_state",
            (),
            "Record the exact visible camera state, then rerun the route-specific probe for this setting.",
        )
    ]


def _admin_guarded(row: dict[str, Any]) -> bool:
    if row["status"] == "dangerous_admin":
        return True
    if row["setting_id"] in settings_registry.DESTRUCTIVE_SETTING_IDS:
        return True
    return any(route.get("status") == "dangerous_admin" for route in row.get("routes", {}).values())


def _physical_gate_command(row: dict[str, Any]) -> str:
    command = (
        "python3 tools/a7ii_physical_gate_proof.py "
        f"--setting {row['setting_id']} "
        "--observed-state \"<describe camera/menu state>\" "
    )
    if _admin_guarded(row):
        command += "--admin-observation-only "
    return command + "--out-dir artifacts/falsification/manual_gates"


def _proof_commands(row: dict[str, Any]) -> list[str]:
    setting_id = row["setting_id"]
    commands = [f"python3 tools/a7ii_settings_probe_plan.py --setting {setting_id}"]
    if row["greyed_in_photo"] or row["status"] in {
        "state_gated",
        "manual_physical_gate",
        "dangerous_admin",
    } or "guided_physical" in row.get("routes", {}):
        commands.append(_physical_gate_command(row))
    if "usb_gphoto" in row.get("routes", {}):
        commands.append(
            "python3 tools/a7ii_usb_state_matrix.py "
            "--state stills --state movie_capable --state manual_focus --state autofocus "
            "--state smart_remote_exited --out-dir artifacts/falsification/r2_usb_state_matrix"
        )
    if "raw_ptp" in row.get("routes", {}):
        commands.append("python3 tools/a7ii_settings_probe_plan.py --setting " f"{setting_id} --route raw_ptp")
    return list(dict.fromkeys(commands))


def solve_setting(setting_id: str, registry: dict[str, Any] | None = None) -> dict[str, Any]:
    registry = registry or settings_registry.load_registry()
    row = settings_registry.get_setting(setting_id, registry)
    gates = []
    seen = set()
    for precondition in row["preconditions"]:
        for rule in _matching_rules(precondition):
            key = (precondition, rule.gate_type)
            if key in seen:
                continue
            seen.add(key)
            gates.append(
                {
                    "precondition": precondition,
                    "gate_type": rule.gate_type,
                    "action": rule.action,
                    "physical_only": rule.physical_only,
                    "route_disruptive": rule.route_disruptive,
                }
            )

    route_report = settings_probe_plan.build_probe_report(registry, setting_id=setting_id)
    physical_only = bool(gates) and all(gate["physical_only"] for gate in gates)
    return {
        "setting_id": row["setting_id"],
        "label": row["label"],
        "status": row["status"],
        "menu_group": row["menu_group"],
        "menu_page": row["menu_page"],
        "greyed_in_photo": row["greyed_in_photo"],
        "preconditions": row["preconditions"],
        "gates": gates,
        "physical_only": physical_only,
        "route_disruptive": any(gate["route_disruptive"] for gate in gates),
        "proof_commands": _proof_commands(row),
        "route_commands": route_report["commands"],
        "next_action": row["next_action"],
    }


def build_precondition_report(
    registry: dict[str, Any] | None = None,
    *,
    status: str | None = None,
    greyed_only: bool = False,
    gated_only: bool = False,
) -> dict[str, Any]:
    registry = registry or settings_registry.load_registry()
    rows = list(registry["settings"])
    if status:
        rows = [row for row in rows if row["status"] == status]
    if greyed_only:
        rows = [row for row in rows if row["greyed_in_photo"]]
    if gated_only:
        rows = [
            row
            for row in rows
            if row["greyed_in_photo"]
            or row["status"] in {"state_gated", "manual_physical_gate", "dangerous_admin"}
            or "guided_physical" in row.get("routes", {})
        ]

    solved = [solve_setting(row["setting_id"], registry) for row in rows]
    gate_counts: dict[str, int] = {}
    for item in solved:
        for gate in item["gates"]:
            gate_counts[gate["gate_type"]] = gate_counts.get(gate["gate_type"], 0) + 1

    return {
        "summary": {
            "settings": len(solved),
            "greyed_settings": sum(1 for item in solved if item["greyed_in_photo"]),
            "physical_only_settings": sum(1 for item in solved if item["physical_only"]),
            "route_disruptive_settings": sum(1 for item in solved if item["route_disruptive"]),
            "gate_counts": dict(sorted(gate_counts.items())),
        },
        "settings": solved,
    }
