"""Structured remaining-gates report for A7 II full settings control.

This is the backend source of truth for the remaining hardware/state/admin gates. The CLI wrapper
and API both use this module so the human-readable Markdown and machine-readable `gate_rows` stay
in lockstep.
"""

from __future__ import annotations

from typing import Any

import settings_preconditions
import settings_probe_plan
import settings_registry

GATE_STATUSES = {
    "needs_probe",
    "available_not_wrapped",
    "protocol_ok_needs_behavior",
    "state_gated",
    "manual_physical_gate",
    "dangerous_admin",
    "proven_read",
}


def _gate_rows(registry: dict[str, Any]) -> list[dict[str, Any]]:
    rows = []
    for row in registry["settings"]:
        if row["status"] in GATE_STATUSES and settings_probe_plan.row_needs_evidence(row):
            rows.append(row)
            continue
        if any(
            route.get("status") not in {"proven_set", "unsupported_on_a7ii"}
            for name, route in row.get("routes", {}).items()
            if name not in settings_registry.DISABLED_ROUTES
        ):
            rows.append(row)
    return sorted(rows, key=lambda row: (row["status"], row["menu_group"], row["setting_id"]))


def _admin_guarded(row: dict[str, Any]) -> bool:
    if row["status"] == "dangerous_admin":
        return True
    if row["setting_id"] in settings_registry.DESTRUCTIVE_SETTING_IDS:
        return True
    return any(route.get("status") == "dangerous_admin" for route in row.get("routes", {}).values())


def physical_gate_command(row: dict[str, Any]) -> str:
    command = (
        "python3 tools/a7ii_physical_gate_proof.py "
        f"--setting {row['setting_id']} "
        "--observed-state \"<describe camera/menu state>\" "
    )
    if _admin_guarded(row):
        command += "--admin-observation-only "
    return command + "--out-dir artifacts/falsification/manual_gates"


def _gate_category(row: dict[str, Any]) -> str:
    if _admin_guarded(row):
        return "dangerous_admin_guarded"
    if row["status"] == "state_gated":
        return "state_gated"
    if row["status"] == "manual_physical_gate" or "guided_physical" in row.get("routes", {}):
        return "physical_or_guided"
    return "route_gap"


def _is_one_row_command(setting_id: str, command: dict[str, Any]) -> bool:
    text = command["command"]
    return (
        command.get("setting_count") == 1
        or f"--setting {setting_id}" in text
        or f"--setting-id {setting_id}" in text
        or f"/settings/{setting_id}" in text
    )


def _next_concrete_command(
    row: dict[str, Any],
    proof_commands: list[dict[str, Any]],
    guided_physical_command: str | None,
) -> str | None:
    if guided_physical_command and _gate_category(row) in {
        "dangerous_admin_guarded",
        "physical_or_guided",
        "state_gated",
    }:
        return guided_physical_command
    for command in proof_commands:
        if _is_one_row_command(row["setting_id"], command):
            return command["command"]
    if guided_physical_command:
        return guided_physical_command
    if proof_commands:
        return proof_commands[0]["command"]
    return None


def _recommended_sequence_route(
    row: dict[str, Any],
    proof_commands: list[dict[str, Any]],
    guided_physical_command: str | None,
) -> str | None:
    if guided_physical_command and _gate_category(row) in {
        "dangerous_admin_guarded",
        "physical_or_guided",
        "state_gated",
    }:
        return "guided_physical"
    for command in proof_commands:
        if _is_one_row_command(row["setting_id"], command):
            return command["route"]
    if proof_commands:
        return proof_commands[0]["route"]
    if guided_physical_command:
        return "guided_physical"
    return None


def _recommended_sequence_session_command(
    row: dict[str, Any],
    proof_commands: list[dict[str, Any]],
    guided_physical_command: str | None,
) -> str | None:
    route = _recommended_sequence_route(row, proof_commands, guided_physical_command)
    if not route:
        return None
    return (
        "python3 tools/a7ii_live_session_runner.py "
        f"--route {route} --setting {row['setting_id']} --recommended-sequence"
    )


def _commands_by_setting(commands: list[dict[str, Any]]) -> dict[str, list[dict[str, Any]]]:
    by_setting: dict[str, list[dict[str, Any]]] = {}
    for command in commands:
        slim = {
            "probe_id": command["probe_id"],
            "route": command["route"],
            "title": command["title"],
            "command": command["command"],
            "setting_count": command.get("setting_count", len(command["setting_ids"])),
            "artifact_root": command["artifact_root"],
            "risk": command["risk"],
            "prerequisite": command["prerequisite"],
            "proves": command["proves"],
        }
        for setting_id in command["setting_ids"]:
            by_setting.setdefault(setting_id, []).append(slim)
    return by_setting


def _structured_gate_rows(
    rows: list[dict[str, Any]],
    *,
    route_commands: list[dict[str, Any]],
    precondition_report: dict[str, Any],
) -> list[dict[str, Any]]:
    command_map = _commands_by_setting(route_commands)
    preconditions_by_id = {
        item["setting_id"]: item for item in precondition_report["settings"]
    }
    structured = []
    for row in rows:
        setting_id = row["setting_id"]
        precondition_solution = preconditions_by_id.get(setting_id)
        proof_commands = command_map.get(setting_id, [])
        guided_physical_command = (
            physical_gate_command(row)
            if row["status"] in {"manual_physical_gate", "dangerous_admin", "state_gated"}
            or "guided_physical" in row.get("routes", {})
            else None
        )
        recommended_sequence_session_command = _recommended_sequence_session_command(
            row,
            proof_commands,
            guided_physical_command,
        )
        structured.append(
            {
                "setting_id": setting_id,
                "label": row["label"],
                "status": row["status"],
                "gate_category": _gate_category(row),
                "menu_group": row["menu_group"],
                "menu_page": row["menu_page"],
                "current_photo_value": row["current_photo_value"],
                "greyed_in_photo": row["greyed_in_photo"],
                "admin_guarded": _admin_guarded(row),
                "requires_admin_observation_only_ack": _admin_guarded(row),
                "physical_or_guided": row["status"] == "manual_physical_gate"
                or "guided_physical" in row.get("routes", {}),
                "state_gated": row["status"] == "state_gated",
                "preconditions": row["preconditions"],
                "precondition_gates": precondition_solution["gates"] if precondition_solution else [],
                "physical_only": bool(precondition_solution and precondition_solution["physical_only"]),
                "route_disruptive": bool(precondition_solution and precondition_solution["route_disruptive"]),
                "routes": row["routes"],
                "proof_artifacts": row["proof_artifacts"],
                "next_action": row["next_action"],
                "proof_commands": proof_commands,
                "exact_next_commands": [
                    command
                    for command in [
                        recommended_sequence_session_command,
                        *[item["command"] for item in proof_commands],
                    ]
                    if command
                ],
                "guided_physical_command": guided_physical_command,
                "next_concrete_command": _next_concrete_command(
                    row,
                    proof_commands,
                    guided_physical_command,
                ),
                "recommended_sequence_session_command": recommended_sequence_session_command,
            }
        )
    return structured


def _filter_gate_rows(
    gate_rows: list[dict[str, Any]],
    *,
    setting_id: str | None = None,
    menu_group: str | None = None,
    status: str | None = None,
    gate_category: str | None = None,
    limit: int | None = None,
) -> list[dict[str, Any]]:
    rows = gate_rows
    if setting_id is not None:
        rows = [row for row in rows if row["setting_id"] == setting_id]
    if menu_group is not None:
        rows = [row for row in rows if row["menu_group"] == menu_group]
    if status is not None:
        rows = [row for row in rows if row["status"] == status]
    if gate_category is not None:
        rows = [row for row in rows if row["gate_category"] == gate_category]
    if limit is not None:
        rows = rows[: max(0, limit)]
    return rows


def _filter_route_commands(
    commands: list[dict[str, Any]],
    gate_rows: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    setting_ids = {row["setting_id"] for row in gate_rows}
    if not setting_ids:
        return []
    out = []
    for command in commands:
        matched = [setting_id for setting_id in command["setting_ids"] if setting_id in setting_ids]
        if not matched:
            continue
        narrowed = dict(command)
        narrowed["setting_ids"] = matched
        narrowed["setting_count"] = len(matched)
        out.append(narrowed)
    return out


def _counts_by_status(gate_rows: list[dict[str, Any]]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for row in gate_rows:
        counts[row["status"]] = counts.get(row["status"], 0) + 1
    return dict(sorted(counts.items()))


def _counts_by_gate_category(gate_rows: list[dict[str, Any]]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for row in gate_rows:
        counts[row["gate_category"]] = counts.get(row["gate_category"], 0) + 1
    return dict(sorted(counts.items()))


def build_report(
    registry: dict[str, Any] | None = None,
    *,
    setting_id: str | None = None,
    menu_group: str | None = None,
    status: str | None = None,
    gate_category: str | None = None,
    limit: int | None = None,
) -> dict[str, Any]:
    registry = registry or settings_registry.load_registry()
    if setting_id is not None:
        settings_registry.get_setting(setting_id, registry)

    rows = _gate_rows(registry)
    probe_report = settings_probe_plan.build_probe_report(registry)
    precondition_report = settings_preconditions.build_precondition_report(registry, gated_only=True)
    route_commands = probe_report["commands"]
    by_status: dict[str, int] = {}
    physical = []
    state_gated = []
    dangerous = []
    for row in rows:
        by_status[row["status"]] = by_status.get(row["status"], 0) + 1
        if row["status"] == "manual_physical_gate" or "guided_physical" in row.get("routes", {}):
            physical.append(row["setting_id"])
        if row["status"] == "state_gated":
            state_gated.append(row["setting_id"])
        if row["status"] == "dangerous_admin":
            dangerous.append(row["setting_id"])

    gate_rows = _structured_gate_rows(
        rows,
        route_commands=route_commands,
        precondition_report=precondition_report,
    )
    filtered_gate_rows = _filter_gate_rows(
        gate_rows,
        setting_id=setting_id,
        menu_group=menu_group,
        status=status,
        gate_category=gate_category,
        limit=limit,
    )
    filtered_route_commands = _filter_route_commands(route_commands, filtered_gate_rows)

    return {
        "summary": {
            "total_settings": len(registry["settings"]),
            "remaining_gate_rows": len(rows),
            "returned_gate_rows": len(filtered_gate_rows),
            "by_status": dict(sorted(by_status.items())),
            "returned_by_status": _counts_by_status(filtered_gate_rows),
            "returned_by_gate_category": _counts_by_gate_category(filtered_gate_rows),
            "physical_or_guided_rows": len(set(physical)),
            "state_gated_rows": len(state_gated),
            "dangerous_admin_rows": len(dangerous),
            "dangerous_admin_proofs_require_observation_only_ack": True,
            "probe_commands": probe_report["summary"]["probe_commands"],
            "returned_probe_commands": len(filtered_route_commands),
            "missing_probe_command_rows": probe_report["summary"]["missing_gap_rows"],
            "filters": {
                "setting_id": setting_id,
                "menu_group": menu_group,
                "status": status,
                "gate_category": gate_category,
                "limit": limit,
            },
        },
        "route_commands": filtered_route_commands,
        "precondition_summary": precondition_report["summary"],
        "physical_or_guided_setting_ids": sorted(set(physical)),
        "state_gated_setting_ids": sorted(state_gated),
        "dangerous_admin_setting_ids": sorted(dangerous),
        "gate_rows": filtered_gate_rows,
        "rows": rows,
    }


def to_markdown(report: dict[str, Any]) -> str:
    summary = report["summary"]
    lines = [
        "# A7 II Remaining Settings Gates",
        "",
        f"- total_settings: `{summary['total_settings']}`",
        f"- remaining_gate_rows: `{summary['remaining_gate_rows']}`",
        f"- returned_gate_rows: `{summary['returned_gate_rows']}`",
        f"- proof_commands: `{summary['probe_commands']}`",
        f"- returned_proof_commands: `{summary['returned_probe_commands']}`",
        f"- missing_probe_command_rows: `{len(summary['missing_probe_command_rows'])}`",
        "- dangerous_admin_proofs_require_observation_only_ack: "
        f"`{summary['dangerous_admin_proofs_require_observation_only_ack']}`",
        "",
        "## Status Counts",
        "",
    ]
    for status, count in summary["by_status"].items():
        lines.append(f"- `{status}`: {count}")
    if summary["returned_gate_rows"] != summary["remaining_gate_rows"]:
        lines.extend(["", "Returned status counts:", ""])
        for status, count in summary["returned_by_status"].items():
            lines.append(f"- `{status}`: {count}")
    lines.extend(["", "Returned gate categories:", ""])
    for gate_category, count in summary["returned_by_gate_category"].items():
        lines.append(f"- `{gate_category}`: {count}")

    lines.extend(["", "## Route Proof Commands", ""])
    if not report["route_commands"]:
        lines.append("No route proof commands match this filter.")
        lines.append("")
    for command in report["route_commands"]:
        lines.append(f"### {command['probe_id']}")
        lines.append("")
        lines.append(f"- route: `{command['route']}`")
        lines.append(f"- setting_count: `{command['setting_count']}`")
        lines.append(f"- artifacts: `{command['artifact_root']}`")
        lines.append(f"- risk: `{command['risk']}`")
        lines.append(f"- prerequisite: {command['prerequisite']}")
        lines.append("")
        lines.append("```bash")
        lines.append(command["command"])
        lines.append("```")
        lines.append("")

    lines.extend(["## Precondition Solver", ""])
    pre = report["precondition_summary"]
    lines.append(f"- gated_settings: `{pre['settings']}`")
    lines.append(f"- greyed_settings: `{pre['greyed_settings']}`")
    lines.append(f"- physical_only_settings: `{pre['physical_only_settings']}`")
    lines.append(f"- route_disruptive_settings: `{pre['route_disruptive_settings']}`")
    lines.append("")
    lines.append("```bash")
    lines.append("python3 tools/a7ii_precondition_solver.py --gated-only")
    lines.append("python3 tools/a7ii_precondition_solver.py --greyed-only")
    lines.append("python3 tools/a7ii_precondition_solver.py --setting camera.panorama_size")
    lines.append("```")
    lines.append("")
    lines.append("Gate counts:")
    lines.append("")
    for gate_type, count in pre["gate_counts"].items():
        lines.append(f"- `{gate_type}`: {count}")
    lines.append("")

    lines.extend(["## Per-Setting Gate Rows", ""])
    if not report["gate_rows"]:
        lines.append("No gate rows match this filter.")
        lines.append("")
    current_category = None
    for row in report["gate_rows"]:
        if row["gate_category"] != current_category:
            current_category = row["gate_category"]
            lines.append(f"### {current_category}")
            lines.append("")
        lines.append(f"- `{row['setting_id']}` [{row['status']}] {row['label']}")
        lines.append(f"  - menu: `{row['menu_group']} / {row['menu_page']}`")
        lines.append(f"  - next: {row['next_action']}")
        if row["recommended_sequence_session_command"]:
            lines.append(
                "  - recommended_sequence: "
                f"`{row['recommended_sequence_session_command']}`"
            )
        if row["next_concrete_command"]:
            lines.append(f"  - next_concrete: `{row['next_concrete_command']}`")
        if row["admin_guarded"]:
            lines.append(
                "  - admin_guardrail: observation-only; do not execute delete, format, reset, "
                "network reset, calibration, or media-management actions."
            )
        if row["guided_physical_command"]:
            lines.append(f"  - guided_physical: `{row['guided_physical_command']}`")
        for command in row["proof_commands"]:
            lines.append(f"  - proof[{command['route']}]: `{command['command']}`")
        lines.append("")

    return "\n".join(lines).rstrip() + "\n"
