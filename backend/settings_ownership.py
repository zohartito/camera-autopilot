"""Per-setting ownership audit for A7 II full settings control.

The acceptance report answers "which route should we run next?" This module answers the row-level
question: "for this photographed setting, who owns it, what blocks it, and what exact command proves
the next claim?"
"""

from __future__ import annotations

from typing import Any

import settings_closure
import settings_evidence
import settings_preconditions
import settings_probe_plan
import settings_registry
import settings_remaining_gates

CONTROLLED_STATUSES = {"proven_set"}
READABLE_STATUSES = {"proven_read"}
MANUAL_STATUSES = {"manual_physical_gate", "state_gated", "dangerous_admin"}


def _ordered_routes(row: dict[str, Any], registry: dict[str, Any]) -> list[str]:
    priority = list(registry.get("route_priority") or settings_closure.ROUTE_ORDER)
    routes = list(row.get("routes", {}))
    ordered = [route for route in priority if route in routes]
    ordered.extend(sorted(route for route in routes if route not in ordered))
    return ordered


def _route_candidates(
    row: dict[str, Any],
    registry: dict[str, Any],
    evidence: dict[str, Any],
) -> list[dict[str, Any]]:
    reviews_by_route = {review["route"]: review for review in evidence.get("reviews", [])}
    candidates = []
    for route in _ordered_routes(row, registry):
        info = dict(row.get("routes", {}).get(route) or {})
        review = reviews_by_route.get(route)
        candidate = {
            "route": route,
            "label": settings_closure.ROUTE_LABELS.get(route, route),
            "status": info.pop("status", "needs_probe"),
            "details": info,
            "review_verdict": review.get("verdict") if review else None,
            "review_artifact": review.get("review_artifact") if review else None,
            "evidence_count": review.get("evidence_count") if review else None,
            "missing_review": route in evidence.get("missing_review_routes", []),
        }
        candidates.append(candidate)
    return candidates


def _ownership_state(row: dict[str, Any], commands: list[dict[str, Any]]) -> str:
    status = row["status"]
    if (
        status not in CONTROLLED_STATUSES | READABLE_STATUSES
        and not settings_probe_plan.row_needs_evidence(row)
    ):
        return "unavailable_route_owned"
    if status in CONTROLLED_STATUSES:
        return "remote_setter_owned"
    if status in READABLE_STATUSES:
        return "remote_read_owned"
    if status == "available_not_wrapped":
        return "available_needs_wrapper_or_behavior_proof"
    if status == "protocol_ok_needs_behavior":
        return "protocol_ok_needs_visible_behavior"
    if status == "unsupported_on_a7ii":
        return "unsupported_model_owned"
    if status in MANUAL_STATUSES:
        return "physical_or_admin_gate_owned"
    if commands:
        return "needs_live_evidence_with_command"
    return "unowned_missing_command"


def _command_blockers(row: dict[str, Any], commands: list[dict[str, Any]], evidence: dict[str, Any]) -> list[str]:
    blockers: list[str] = []

    def add(name: str) -> None:
        if name not in blockers:
            blockers.append(name)

    if row["status"] == "dangerous_admin":
        add("destructive_or_admin_guardrail")
    for route in evidence.get("missing_review_routes", []):
        add(f"missing_review_artifact:{route}")
    for command in commands:
        risk = command.get("risk")
        if risk in {
            "safe_read",
            "temporary_setting_write",
            "captures_photo",
            "focus_may_move",
            "live_camera_read",
            "live_camera_capture_focus",
        }:
            add("camera_route_state_and_hardware_approval")
        elif risk == "focus_visual_review":
            add("one_raw_ptp_setting_and_visual_behavior")
        elif risk == "local_package_read":
            add("local_sony_package_path")
        elif risk == "tooling_and_admin_probe" or risk == "rollback_port_scan":
            add("reversible_admin_tooling_and_rollback_proof")
        elif risk == "manual_observation":
            add("one_setting_and_observed_camera_state")
    return blockers


def _proof_commands(row: dict[str, Any], registry: dict[str, Any]) -> list[dict[str, Any]]:
    report = settings_probe_plan.build_probe_report(registry, setting_id=row["setting_id"])
    return report["commands"]


def _setting_card(
    row: dict[str, Any],
    registry: dict[str, Any],
    review_index: dict[str, Any],
    gate_index: dict[str, dict[str, Any]],
) -> dict[str, Any]:
    evidence = settings_evidence.evidence_for_setting(
        row["setting_id"],
        registry=registry,
        review_index=review_index,
    )
    commands = _proof_commands(row, registry)
    preconditions = settings_preconditions.solve_setting(row["setting_id"], registry)
    needs_evidence = settings_probe_plan.row_needs_evidence(row)
    state = _ownership_state(row, commands)
    remaining_gate = gate_index.get(row["setting_id"])
    next_concrete_command = remaining_gate.get("next_concrete_command") if remaining_gate else None
    recommended_sequence_session_command = (
        remaining_gate.get("recommended_sequence_session_command") if remaining_gate else None
    )
    return {
        "setting_id": row["setting_id"],
        "label": row["label"],
        "menu_group": row["menu_group"],
        "menu_page": row["menu_page"],
        "photo_sources": row["photo_sources"],
        "current_photo_value": row["current_photo_value"],
        "greyed_in_photo": row["greyed_in_photo"],
        "status": row["status"],
        "ownership_state": state,
        "owned": state != "unowned_missing_command",
        "needs_evidence": needs_evidence,
        "has_exact_next_command": bool(commands) or bool(next_concrete_command) or not needs_evidence,
        "next_action": row["next_action"],
        "next_concrete_command": next_concrete_command,
        "recommended_sequence_session_command": recommended_sequence_session_command,
        "remaining_gate": remaining_gate,
        "route_candidates": _route_candidates(row, registry, evidence),
        "proof_artifacts": row["proof_artifacts"],
        "proof_commands": commands,
        "first_proof_command": (
            recommended_sequence_session_command
            or next_concrete_command
            or (commands[0]["command"] if commands else None)
        ),
        "missing_review_routes": evidence["missing_review_routes"],
        "evidence_reviews": evidence["reviews"],
        "precondition_solution": {
            "gates": preconditions["gates"],
            "physical_only": preconditions["physical_only"],
            "route_disruptive": preconditions["route_disruptive"],
        },
        "blockers": _command_blockers(row, commands, evidence),
    }


def _filter_rows(
    registry: dict[str, Any],
    *,
    setting_id: str | None = None,
    menu_group: str | None = None,
    status: str | None = None,
    route: str | None = None,
    greyed_only: bool = False,
) -> list[dict[str, Any]]:
    if setting_id:
        rows = [settings_registry.get_setting(setting_id, registry)]
    else:
        rows = settings_registry.list_settings(menu_group=menu_group, status=status, registry=registry)
    if route:
        rows = [row for row in rows if route in row.get("routes", {})]
    if greyed_only:
        rows = [row for row in rows if row["greyed_in_photo"]]
    return sorted(rows, key=lambda row: (row["menu_group"], row["menu_page"], row["setting_id"]))


def build_report(
    *,
    registry: dict[str, Any] | None = None,
    setting_id: str | None = None,
    menu_group: str | None = None,
    status: str | None = None,
    route: str | None = None,
    greyed_only: bool = False,
    limit: int | None = None,
) -> dict[str, Any]:
    registry = registry or settings_registry.load_registry()
    rows = _filter_rows(
        registry,
        setting_id=setting_id,
        menu_group=menu_group,
        status=status,
        route=route,
        greyed_only=greyed_only,
    )
    if limit is not None and limit > 0:
        rows = rows[:limit]
    review_index = settings_evidence.load_review_index()
    gate_report = settings_remaining_gates.build_report(registry=registry)
    gate_index = {item["setting_id"]: item for item in gate_report["gate_rows"]}
    settings = [_setting_card(row, registry, review_index, gate_index) for row in rows]
    unowned = [item["setting_id"] for item in settings if not item["owned"]]
    missing_commands = [
        item["setting_id"]
        for item in settings
        if item["needs_evidence"] and not item["has_exact_next_command"]
    ]
    ownership_counts: dict[str, int] = {}
    for item in settings:
        state = item["ownership_state"]
        ownership_counts[state] = ownership_counts.get(state, 0) + 1
    return {
        "summary": {
            "total_registry_settings": len(registry["settings"]),
            "selected_settings": len(settings),
            "unowned_settings": unowned,
            "missing_exact_next_command_settings": missing_commands,
            "settings_needing_evidence": sum(1 for item in settings if item["needs_evidence"]),
            "settings_with_exact_next_commands": sum(
                1 for item in settings if item["has_exact_next_command"]
            ),
            "greyed_settings": sum(1 for item in settings if item["greyed_in_photo"]),
            "ownership_states": dict(sorted(ownership_counts.items())),
        },
        "settings": settings,
    }
