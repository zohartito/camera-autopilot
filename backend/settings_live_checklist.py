"""Operator checklist for live A7 II settings evidence sessions.

This report turns the acceptance queue into a route-by-route worksheet: what the route is supposed
to prove, whether local prerequisites are ready, which manual camera state is needed, and the exact
commands to run for evidence and review.
"""

from __future__ import annotations

from typing import Any

import settings_acceptance
import settings_closure
import settings_readiness
import settings_registry
import settings_session_ledger
import settings_sessions

ROUTE_PROOF_GOALS = {
    "smart_remote_wifi": "Prove cable-free Smart Remote getters/setters with observed before/after behavior.",
    "usb_gphoto": "Prove exposed USB/gphoto nodes with state matrix plus safe set/read/revert.",
    "smart_remote_phone": "Prove phone-app preference rows separately from camera-side API control.",
    "raw_ptp": "Prove raw Sony PTP focus operations with visible near/far behavior.",
    "camera_remote_command": "Review the official Sony Camera Remote Command package for ILCE-7M2 support.",
    "openmemories": "Route 3 is permanently disabled; historical private evidence is unavailable and unverified.",
    "guided_physical": "Record physical/state/admin gates with observation-only proof where required.",
}


def _queue_by_route(acceptance: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {item["route"]: item for item in acceptance["next_action_queue"]}


def _readiness_by_route(readiness: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {item["route"]: item for item in readiness["routes"]}


def _playbook_by_route(playbook: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {item["route"]: item for item in playbook["routes"]}


def _acceptance_session_by_route(acceptance: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {item["route"]: item for item in acceptance.get("next_live_sessions", [])}


def _ledger_by_route(ledger: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {item["route"]: item for item in ledger["latest_by_route"]}


def _status_from_queue(queue_item: dict[str, Any] | None, readiness_summary: dict[str, Any] | None = None) -> str:
    if queue_item is None:
        return "no_action"
    readiness_ready = (
        bool(readiness_summary.get("ready_to_attempt"))
        if readiness_summary is not None
        else bool(queue_item["ready_to_attempt"])
    )
    if queue_item["route"] == "camera_remote_command" and not readiness_ready:
        return "waiting_external_package"
    if readiness_ready:
        return "ready_for_operator"
    return "missing_local_prerequisite"


def _is_template_command(command: str | None) -> bool:
    return bool(command and "<" in command and ">" in command)


def _checklist_steps(
    *,
    route: str,
    queue_item: dict[str, Any] | None,
    readiness_item: dict[str, Any] | None,
    playbook: dict[str, Any],
    ledger_item: dict[str, Any] | None = None,
) -> list[dict[str, Any]]:
    steps: list[dict[str, Any]] = []
    rank = 1
    if ledger_item:
        latest = ledger_item.get("latest_session")
        if latest:
            steps.append(
                {
                    "rank": rank,
                    "kind": "latest_session_outcome",
                    "text": (
                        f"{latest.get('outcome') or 'unknown'} at {latest.get('timestamp')}; "
                        f"manifest {latest.get('manifest_path')}"
                    ),
                }
            )
        elif ledger_item.get("next_command"):
            steps.append(
                {
                    "rank": rank,
                    "kind": "no_prior_session",
                    "text": f"No prior audited session for this route; start with {ledger_item['next_command']}",
                }
            )
        rank += 1
    for note in playbook.get("notes", []):
        steps.append({"rank": rank, "kind": "prepare", "text": note})
        rank += 1
    if readiness_item:
        for item in readiness_item.get("manual_requirements", []):
            steps.append({"rank": rank, "kind": "manual_requirement", "text": item["why"]})
            rank += 1
        for item in readiness_item.get("missing_requirements", []):
            steps.append(
                {
                    "rank": rank,
                    "kind": "missing_requirement",
                    "text": item.get("next_action") or f"Resolve missing {item['kind']}: {item['name']}",
                }
            )
            rank += 1
    if queue_item:
        audited_session_command = queue_item.get("audited_session_command") or queue_item.get("next_command")
        seen_commands = set()
        sequence_session_command = queue_item.get("recommended_sequence_session_command")
        route_prerequisite_command = queue_item.get("route_prerequisite_command") or queue_item.get(
            "next_concrete_command"
        )
        recommended_command = queue_item.get("recommended_next_command")
        if sequence_session_command:
            steps.append(
                {
                    "rank": rank,
                    "kind": "recommended_sequence_manifest",
                    "text": sequence_session_command,
                    "label": "Create the dry-run audited proof-sequence manifest",
                }
            )
            seen_commands.add(sequence_session_command)
            rank += 1
        for sequence_step in queue_item.get("recommended_command_sequence", []):
            command = sequence_step.get("command")
            if not command or command in seen_commands:
                continue
            steps.append(
                {
                    "rank": rank,
                    "kind": sequence_step.get("kind") or "recommended_sequence",
                    "text": command,
                    "label": sequence_step.get("label"),
                }
            )
            seen_commands.add(command)
            rank += 1
        if (
            route_prerequisite_command
            and route_prerequisite_command != queue_item.get("next_command")
            and route_prerequisite_command not in seen_commands
        ):
            steps.append(
                {
                    "rank": rank,
                    "kind": "route_prerequisite_command",
                    "text": route_prerequisite_command,
                }
            )
            seen_commands.add(route_prerequisite_command)
            rank += 1
        if recommended_command and recommended_command not in seen_commands:
            steps.append(
                {
                    "rank": rank,
                    "kind": queue_item.get("recommended_next_command_kind") or "recommended_next_command",
                    "text": recommended_command,
                }
            )
            seen_commands.add(recommended_command)
            rank += 1
        if (
            queue_item.get("next_concrete_command")
            and queue_item["next_concrete_command"] != queue_item.get("next_command")
            and queue_item["next_concrete_command"] not in seen_commands
        ):
            steps.append({"rank": rank, "kind": "next_concrete_command", "text": queue_item["next_concrete_command"]})
            seen_commands.add(queue_item["next_concrete_command"])
            rank += 1
        if (
            queue_item.get("first_one_row_direct_command")
            and queue_item["first_one_row_direct_command"] != queue_item.get("next_command")
            and queue_item["first_one_row_direct_command"] != queue_item.get("next_concrete_command")
            and queue_item["first_one_row_direct_command"] not in seen_commands
        ):
            steps.append(
                {
                    "rank": rank,
                    "kind": "first_one_row_proof",
                    "text": queue_item["first_one_row_direct_command"],
                }
            )
            seen_commands.add(queue_item["first_one_row_direct_command"])
            rank += 1
        if audited_session_command and audited_session_command not in seen_commands:
            steps.append({"rank": rank, "kind": "evidence_session", "text": audited_session_command})
            seen_commands.add(audited_session_command)
            rank += 1
        if queue_item.get("next_command") and queue_item["next_command"] != audited_session_command:
            steps.append(
                {
                    "rank": rank,
                    "kind": "broad_route_template" if _is_template_command(queue_item["next_command"]) else "broad_route_session",
                    "text": queue_item["next_command"],
                }
            )
            rank += 1
        if queue_item.get("first_direct_command") and queue_item["first_direct_command"] not in {
            queue_item.get("next_command"),
            queue_item.get("next_concrete_command"),
            queue_item.get("first_one_row_direct_command"),
            audited_session_command,
        }:
            steps.append({"rank": rank, "kind": "first_direct_command", "text": queue_item["first_direct_command"]})
            rank += 1
        if queue_item.get("review_and_refresh_command"):
            steps.append({"rank": rank, "kind": "review_refresh", "text": queue_item["review_and_refresh_command"]})
            rank += 1
    return steps


def _compact_command(command: dict[str, Any]) -> dict[str, Any]:
    return {
        "probe_id": command.get("probe_id"),
        "title": command.get("title"),
        "route": command.get("route"),
        "setting_ids": command.get("setting_ids", []),
        "setting_count": command.get("setting_count", len(command.get("setting_ids", []))),
        "risk": command.get("risk"),
        "risk_kind": settings_acceptance.RISK_KIND.get(command.get("risk"), "unknown"),
        "artifact_root": command.get("artifact_root"),
        "prerequisite": command.get("prerequisite"),
        "proves": command.get("proves"),
        "command": command["command"],
    }


def _route_item(
    route: str,
    *,
    queue_item: dict[str, Any] | None,
    acceptance_session: dict[str, Any] | None,
    closure_item: dict[str, Any] | None,
    readiness_item: dict[str, Any] | None,
    playbook: dict[str, Any],
    ledger_item: dict[str, Any] | None,
    direct_command_limit: int,
) -> dict[str, Any]:
    active_commands = (
        acceptance_session.get("direct_evidence_commands") if acceptance_session else []
    ) or []
    direct_commands = [_compact_command(command) for command in active_commands]
    if direct_command_limit > 0:
        visible_direct = direct_commands[:direct_command_limit]
    else:
        visible_direct = direct_commands
    readiness_summary = readiness_item.get("summary", {}) if readiness_item else {}
    missing_requirements = readiness_item.get("missing_requirements", []) if readiness_item else []
    manual_requirements = readiness_item.get("manual_requirements", []) if readiness_item else []
    latest_session = ledger_item.get("latest_session") if ledger_item else None
    status = _status_from_queue(queue_item, readiness_summary)
    has_active_action = status != "no_action"
    selected_gap_rows = (
        acceptance_session.get("selected_gap_rows")
        if acceptance_session
        else (closure_item or {}).get("gap_setting_count", 0)
    )
    closed_without_proof_count = (closure_item or {}).get("closed_without_proof_count", 0)
    if closed_without_proof_count:
        no_action_text = (
            f"No current live action is queued for this route; {closed_without_proof_count} gap row(s) "
            "were closed without behavior proof (candidate/failed/route-level evidence) and remain "
            "unproven pending an alternate route."
        )
    else:
        no_action_text = (
            "No current live action is queued for this route; existing evidence closes the route gaps."
        )
    return {
        "route": route,
        "label": settings_closure.ROUTE_LABELS[route],
        "priority": queue_item["rank"] if queue_item else None,
        "status": status,
        "proof_goal": ROUTE_PROOF_GOALS[route],
        "selected_gap_rows": selected_gap_rows,
        "closed_without_proof_count": closed_without_proof_count,
        "evidence_command_count": len(active_commands),
        "direct_command_count": len(direct_commands),
        "direct_commands_hidden": max(0, len(direct_commands) - len(visible_direct)),
        "review_command_count": len(playbook.get("review_commands") or []),
        "refresh_command_count": len(playbook.get("refresh_commands") or []),
        "risk_kinds": queue_item.get("risk_kinds", {}) if queue_item else {},
        "blocked_by": queue_item.get("blocked_by", []) if queue_item else [],
        "local_status": readiness_summary.get("local_status", "unknown"),
        "live_status": readiness_summary.get("live_status", "unknown"),
        "ready_to_attempt": readiness_summary.get("ready_to_attempt", False),
        "latest_session": latest_session,
        "latest_session_outcome": latest_session.get("outcome") if latest_session else None,
        "latest_session_timestamp": latest_session.get("timestamp") if latest_session else None,
        "latest_session_manifest_path": latest_session.get("manifest_path") if latest_session else None,
        "latest_session_next_command": (
            ledger_item.get("latest_session_next_command") if ledger_item and has_active_action else None
        ),
        "ledger_next_command": ledger_item.get("next_command") if ledger_item and has_active_action else None,
        "route_recommended_command": (
            ledger_item.get("route_recommended_command") if ledger_item and has_active_action else None
        ),
        "missing_requirements": missing_requirements,
        "manual_requirements": manual_requirements,
        "next_command": queue_item.get("next_command") if queue_item else None,
        "recommended_sequence_session_command": queue_item.get("recommended_sequence_session_command")
        if queue_item
        else None,
        "audited_session_command": queue_item.get("audited_session_command") if queue_item else None,
        "audited_one_row_session_command": queue_item.get("audited_one_row_session_command") if queue_item else None,
        "next_one_row_audited_session_command": queue_item.get("next_one_row_audited_session_command")
        if queue_item
        else None,
        "next_concrete_command": queue_item.get("next_concrete_command") if queue_item else None,
        "route_prerequisite_command": queue_item.get("route_prerequisite_command") if queue_item else None,
        "recommended_next_command": queue_item.get("recommended_next_command") if queue_item else None,
        "recommended_next_command_kind": queue_item.get("recommended_next_command_kind") if queue_item else None,
        "recommended_command_sequence": queue_item.get("recommended_command_sequence", []) if queue_item else [],
        "first_one_row_direct_command": queue_item.get("first_one_row_direct_command") if queue_item else None,
        "next_one_row_concrete_command": queue_item.get("next_one_row_concrete_command") if queue_item else None,
        "first_one_row_setting_ids": queue_item.get("first_one_row_setting_ids", []) if queue_item else [],
        "readiness_next_action": queue_item.get("readiness_next_action") if queue_item else None,
        "review_and_refresh_command": queue_item.get("review_and_refresh_command") if queue_item else None,
        "direct_evidence_commands": visible_direct,
        "checklist_steps": (
            _checklist_steps(
                route=route,
                queue_item=queue_item,
                readiness_item=readiness_item,
                playbook=playbook,
                ledger_item=ledger_item,
            )
            if has_active_action
            else [
                {
                    "rank": 1,
                    "kind": "no_action",
                    "text": no_action_text,
                }
            ]
        ),
    }


def build_report(
    *,
    registry: dict[str, Any] | None = None,
    route: str | None = None,
    package_path: str | None = None,
    direct_command_limit: int = 3,
    session_ledger: dict[str, Any] | None = None,
    tool_resolver: settings_readiness.ToolResolver | None = None,
    path_exists: settings_readiness.PathExists | None = None,
) -> dict[str, Any]:
    registry = registry or settings_registry.load_registry()
    if route is not None and route not in settings_closure.ROUTE_ORDER:
        raise ValueError(f"unknown route: {route}")
    acceptance = settings_acceptance.build_report(
        registry=registry,
        route=route,
        package_path=package_path,
        tool_resolver=tool_resolver,
        path_exists=path_exists,
    )
    closure = settings_closure.build_report(registry=registry, sample_limit=3)
    readiness = settings_readiness.build_readiness_report(
        registry=registry,
        route=route,
        package_path=package_path,
        tool_resolver=tool_resolver,
        path_exists=path_exists,
    )
    playbook = settings_sessions.build_session_playbook(registry=registry, route=route)
    ledger = session_ledger or settings_session_ledger.build_ledger(route=route)
    queue = _queue_by_route(acceptance)
    acceptance_sessions = _acceptance_session_by_route(acceptance)
    closure_items = {item["route"]: item for item in closure["routes"]}
    readiness_items = _readiness_by_route(readiness)
    playbooks = _playbook_by_route(playbook)
    ledger_items = _ledger_by_route(ledger)
    route_names = (
        [route]
        if route
        else sorted(
            settings_closure.ROUTE_ORDER,
            key=lambda name: (
                queue.get(name, {}).get("rank", 999),
                settings_closure.ROUTE_ORDER.index(name),
            ),
        )
    )
    routes = [
        _route_item(
            name,
            queue_item=queue.get(name),
            acceptance_session=acceptance_sessions.get(name),
            closure_item=closure_items.get(name),
            readiness_item=readiness_items.get(name),
            playbook=playbooks[name],
            ledger_item=ledger_items.get(name),
            direct_command_limit=direct_command_limit,
        )
        for name in route_names
    ]
    status_counts: dict[str, int] = {}
    for item in routes:
        status_counts[item["status"]] = status_counts.get(item["status"], 0) + 1
    active_routes = [item for item in routes if item["status"] != "no_action"]
    return {
        "summary": {
            "completion_state": acceptance["summary"]["completion_state"],
            "routes": len(routes),
            "status_counts": dict(sorted(status_counts.items())),
            "ready_to_attempt_routes": readiness["summary"]["ready_to_attempt"],
            "missing_readiness_requirements": readiness["summary"]["missing_requirements"],
            "missing_review_sources": acceptance["summary"]["missing_review_sources"],
            "selected_gap_rows": acceptance["summary"]["selected_gap_rows"],
            "closed_without_proof_rows": sum(item["closed_without_proof_count"] for item in routes),
            "evidence_commands": acceptance["summary"]["direct_evidence_commands"],
            "review_commands": sum(item["review_command_count"] for item in active_routes),
            "refresh_commands": sum(item["refresh_command_count"] for item in active_routes),
            "routes_with_session_history": sum(1 for item in routes if item["latest_session"] is not None),
            "routes_with_ledger_next_command": sum(1 for item in routes if item["ledger_next_command"]),
            "unreadable_session_manifests": ledger["summary"]["unreadable_manifests"],
            "direct_command_limit": direct_command_limit,
        },
        "routes": routes,
    }


def to_markdown(report: dict[str, Any]) -> str:
    summary = report["summary"]
    lines = [
        "# A7 II Live Evidence Checklist",
        "",
        f"- completion_state: `{summary['completion_state']}`",
        f"- routes: `{summary['routes']}`",
        f"- selected_gap_rows: `{summary['selected_gap_rows']}`",
        f"- closed_without_proof_rows: `{summary['closed_without_proof_rows']}`",
        f"- evidence_commands: `{summary['evidence_commands']}`",
        f"- review_commands: `{summary['review_commands']}`",
        f"- refresh_commands: `{summary['refresh_commands']}`",
        f"- ready_to_attempt_routes: `{summary['ready_to_attempt_routes']}`",
        f"- missing_readiness_requirements: `{summary['missing_readiness_requirements']}`",
        f"- missing_review_sources: `{', '.join(summary['missing_review_sources']) or 'none'}`",
        f"- routes_with_session_history: `{summary['routes_with_session_history']}`",
        f"- routes_with_ledger_next_command: `{summary['routes_with_ledger_next_command']}`",
        f"- unreadable_session_manifests: `{summary['unreadable_session_manifests']}`",
        "",
        "## Route Status",
        "",
    ]
    for status, count in summary["status_counts"].items():
        lines.append(f"- `{status}`: {count}")
    lines.extend(["", "## Routes", ""])
    for route in report["routes"]:
        title = f"{route['priority']}. {route['label']}" if route["priority"] is not None else route["label"]
        lines.append(f"### {title}")
        lines.append("")
        lines.append(f"- route: `{route['route']}`")
        lines.append(f"- status: `{route['status']}`")
        lines.append(f"- proof_goal: {route['proof_goal']}")
        lines.append(f"- selected_gap_rows: `{route['selected_gap_rows']}`")
        if route["closed_without_proof_count"]:
            lines.append(f"- closed_without_proof_rows: `{route['closed_without_proof_count']}`")
        lines.append(f"- evidence_commands: `{route['evidence_command_count']}`")
        lines.append(f"- direct_evidence_commands_shown: `{len(route['direct_evidence_commands'])}`")
        lines.append(f"- direct_evidence_commands_hidden: `{route['direct_commands_hidden']}`")
        lines.append(f"- local_status: `{route['local_status']}`")
        lines.append(f"- live_status: `{route['live_status']}`")
        lines.append(f"- ready_to_attempt: `{route['ready_to_attempt']}`")
        if route["latest_session"]:
            lines.append(f"- latest_session_outcome: `{route['latest_session_outcome']}`")
            lines.append(f"- latest_session_timestamp: `{route['latest_session_timestamp']}`")
            lines.append(f"- latest_session_manifest: `{route['latest_session_manifest_path']}`")
        else:
            lines.append("- latest_session_outcome: `none`")
        if route["ledger_next_command"]:
            lines.append(f"- ledger_next_command: `{route['ledger_next_command']}`")
        if route["route_recommended_command"] and route["route_recommended_command"] != route["ledger_next_command"]:
            lines.append(f"- route_recommended_command: `{route['route_recommended_command']}`")
        if route["blocked_by"]:
            lines.append(f"- blocked_by: `{', '.join(route['blocked_by'])}`")
        if route["risk_kinds"]:
            risks = ", ".join(f"{name}={count}" for name, count in route["risk_kinds"].items())
            lines.append(f"- risk_kinds: `{risks}`")
        if route["missing_requirements"]:
            missing = ", ".join(f"{item['kind']}:{item['name']}" for item in route["missing_requirements"])
            lines.append(f"- missing_requirements: `{missing}`")
        if route["recommended_sequence_session_command"]:
            lines.extend(
                [
                    "",
                    "Audited proof-sequence manifest:",
                    "",
                    "```bash",
                    route["recommended_sequence_session_command"],
                    "```",
                ]
            )
        lines.extend(["", "Checklist:", ""])
        for step in route["checklist_steps"]:
            label = f" - {step['label']}" if step.get("label") else ""
            lines.append(f"{step['rank']}. `{step['kind']}`{label}: {step['text']}")
        if route["route_prerequisite_command"] and route["route_prerequisite_command"] != route["next_command"]:
            lines.extend(
                [
                    "",
                    "Route prerequisite/probe command:",
                    "",
                    "```bash",
                    route["route_prerequisite_command"],
                    "```",
                ]
            )
        if route["recommended_next_command"]:
            kind = route.get("recommended_next_command_kind") or "recommended_next_command"
            label = {
                "readiness_prerequisite": "Recommended readiness command",
                "audited_one_row_evidence_session": "Recommended audited one-row command",
                "one_row_direct_evidence": "Recommended one-row proof command",
                "route_direct_evidence": "Recommended route evidence command",
                "route_evidence_session": "Recommended route session",
            }.get(kind, "Recommended next command")
            lines.extend(
                [
                    "",
                    f"{label}:",
                    "",
                    "```bash",
                    route["recommended_next_command"],
                    "```",
                ]
            )
        if (
            route["first_one_row_direct_command"]
            and route["first_one_row_direct_command"] != route["next_command"]
            and route["first_one_row_direct_command"] != route["route_prerequisite_command"]
            and route["first_one_row_direct_command"] != route["recommended_next_command"]
        ):
            lines.extend(
                [
                    "",
                    "Direct one-row proof command:",
                    "",
                    "```bash",
                    route["first_one_row_direct_command"],
                    "```",
                ]
            )
        audited_session_command = route["audited_session_command"] or route["next_command"]
        if audited_session_command:
            label = "Audited one-row evidence session" if route["audited_one_row_session_command"] else "Evidence session"
            lines.extend(["", f"{label}:", "", "```bash", audited_session_command, "```"])
        if route["next_command"] and route["next_command"] != audited_session_command:
            label = (
                "Broad route template, not a concrete row command"
                if _is_template_command(route["next_command"])
                else "Broad route evidence session"
            )
            lines.extend(["", f"{label}:", "", "```bash", route["next_command"], "```"])
        if route["review_and_refresh_command"]:
            lines.extend(["", "Review and refresh:", "", "```bash", route["review_and_refresh_command"], "```"])
        if route["direct_evidence_commands"]:
            lines.extend(["", "Direct evidence commands:", ""])
            for command in route["direct_evidence_commands"]:
                lines.append(f"#### {command.get('probe_id') or command.get('title')}")
                lines.append("")
                lines.append(f"- risk: `{command.get('risk')}`")
                lines.append(f"- risk_kind: `{command.get('risk_kind')}`")
                lines.append(f"- settings: `{command.get('setting_count')}`")
                if command.get("setting_ids"):
                    lines.append(f"- setting_ids: `{', '.join(command['setting_ids'])}`")
                lines.extend(["", "```bash", command["command"], "```", ""])
            if route["direct_commands_hidden"] > 0:
                lines.append(
                    f"_Plus {route['direct_commands_hidden']} more direct evidence command(s); "
                    "rerun with --limit-direct-commands 0 for all._"
                )
                lines.append("")
    return "\n".join(lines).rstrip() + "\n"
