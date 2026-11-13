"""Acceptance and next-live-run report for A7 II settings control.

This is the compact operator view over the registry, closure report, promotion plan, and route
playbooks. It intentionally does not create new truth; it packages the existing evidence into a
clear "complete vs. live-gated" state with exact next commands.
"""

from __future__ import annotations

import shlex
from typing import Any

import settings_closure
import settings_promotions
import settings_readiness
import settings_registry
import settings_session_ledger
import settings_sessions
import settings_smart_remote_values

RISK_KIND = {
    "local_artifact_review": "local_review",
    "local_package_discovery": "local_package",
    "local_package_read": "local_package",
    "safe_read": "live_camera_read",
    "temporary_setting_write": "live_camera_write",
    "captures_photo": "live_camera_capture",
    "records_movie": "live_camera_record",
    "focus_may_move": "live_camera_focus",
    "focus_visual_review": "raw_ptp_visual_review",
    "live_camera_read": "live_camera_read",
    "live_camera_capture_focus": "live_camera_capture_focus",
    "rollback_port_scan": "rollback_proof",
    "tooling_and_admin_probe": "admin_tooling",
    "manual_observation": "physical_manual",
    "manual_admin_observation": "physical_manual",
    "phone_manual_observation": "phone_manual",
    "smart_remote_behavior_review": "smart_remote_behavior_review",
}


NEXT_ACTION_ROUTE_ORDER = {
    "smart_remote_wifi": 10,
    "usb_gphoto": 20,
    "smart_remote_phone": 30,
    "raw_ptp": 40,
    "camera_remote_command": 50,
    "openmemories": 60,
    "guided_physical": 70,
}


def _runner_command(route: str, *, phase: str, package_path: bool = False) -> str:
    command = f"python3 tools/a7ii_live_session_runner.py --route {route} --phase {phase} --execute"
    if route == "camera_remote_command" and package_path:
        command += " --package-path /path/to/CameraRemoteCommand-package.zip"
    elif route == "smart_remote_phone" and phase == "evidence":
        command = (
            "python3 tools/a7ii_live_session_runner.py --route smart_remote_phone "
            "--setting <setting_id> --phase evidence --execute --allow-hardware "
            '--observed-value "<visible phone-app value>" '
            '--phone-state "<describe phone/app state>"'
        )
    elif route == "smart_remote_wifi" and phase == "evidence":
        command = (
            "python3 tools/a7ii_live_session_runner.py --route smart_remote_wifi "
            "--setting <setting_id> --phase evidence --execute --allow-hardware "
            '--smart-remote-method "<Smart Remote setter method>" '
            '--smart-remote-requested-value "<value sent>" '
            '--smart-remote-observed-before "<observed before value>" '
            '--smart-remote-observed-after "<observed after value>"'
        )
    elif route == "raw_ptp" and phase == "evidence":
        command = (
            "python3 tools/a7ii_live_session_runner.py --route raw_ptp "
            "--setting <setting_id> --phase evidence --execute --allow-hardware "
            '--observed-behavior "<describe near/far visual behavior>" '
            '--target-setup "<describe near/far target setup>"'
        )
    elif route == "guided_physical" and phase == "evidence":
        command = (
            "python3 tools/a7ii_live_session_runner.py --route guided_physical "
            "--setting <setting_id> --phase evidence --execute --allow-hardware "
            '--observed-state "<describe camera/menu state>"'
        )
    elif phase == "evidence":
        command += " --allow-hardware"
    return command


def _recommended_sequence_session_command(route: str) -> str:
    return f"python3 tools/a7ii_live_session_runner.py --route {route} --recommended-sequence"


def _command_queue(commands: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [
        {
            "probe_id": command.get("probe_id"),
            "title": command.get("title"),
            "setting_ids": command.get("setting_ids", []),
            "setting_count": command.get("setting_count", len(command.get("setting_ids", []))),
            "risk": command.get("risk"),
            "risk_kind": RISK_KIND.get(command.get("risk"), "unknown"),
            "artifact_root": command.get("artifact_root"),
            "prerequisite": command.get("prerequisite"),
            "proves": command.get("proves"),
            "command": command["command"],
        }
        for command in commands
    ]


def _first_command(commands: list[dict[str, Any]]) -> dict[str, Any] | None:
    return commands[0] if commands else None


def _is_scoped_to_setting(command: dict[str, Any], setting_id: str) -> bool:
    text = command["command"]
    return (
        f"--setting {setting_id}" in text
        or f"--setting-id {setting_id}" in text
        or f"/settings/{setting_id}" in text
    )


def _first_one_row_command(commands: list[dict[str, Any]]) -> dict[str, Any] | None:
    for command in commands:
        setting_ids = command.get("setting_ids", [])
        if (
            command.get("setting_count", len(setting_ids)) == 1
            and len(setting_ids) == 1
            and _is_scoped_to_setting(command, str(setting_ids[0]))
        ):
            return command
    return None


def _recommended_next_command(
    *,
    readiness_next_commands: list[str],
    audited_one_row_session: str | None,
    first_one_row: dict[str, Any] | None,
    first_direct: dict[str, Any] | None,
    evidence_command: str,
) -> tuple[str, str]:
    if readiness_next_commands:
        return readiness_next_commands[0], "readiness_prerequisite"
    if audited_one_row_session:
        return audited_one_row_session, "audited_one_row_evidence_session"
    if first_one_row:
        return first_one_row["command"], "one_row_direct_evidence"
    if first_direct:
        return first_direct["command"], "route_direct_evidence"
    return evidence_command, "route_evidence_session"


def _append_sequence_step(
    steps: list[dict[str, Any]],
    *,
    kind: str,
    command: str | None,
    label: str,
) -> None:
    if not command:
        return
    if any(step["command"] == command for step in steps):
        return
    steps.append(
        {
            "rank": len(steps) + 1,
            "kind": kind,
            "label": label,
            "command": command,
        }
    )


def _recommended_command_sequence(
    *,
    readiness_next_commands: list[str],
    route_prerequisite_command: str | None,
    recommended_next_command: str,
    recommended_next_command_kind: str,
    audited_one_row_session: str | None,
    first_one_row: dict[str, Any] | None,
    first_direct: dict[str, Any] | None,
    review_and_refresh_command: str | None,
) -> list[dict[str, Any]]:
    steps: list[dict[str, Any]] = []
    if readiness_next_commands:
        for command in readiness_next_commands:
            _append_sequence_step(
                steps,
                kind="readiness_prerequisite",
                command=command,
                label="Resolve local/package readiness before evidence",
            )
    else:
        _append_sequence_step(
            steps,
            kind="route_prerequisite_probe",
            command=route_prerequisite_command,
            label="Capture or refresh route-level probe evidence",
        )
    _append_sequence_step(
        steps,
        kind=recommended_next_command_kind,
        command=recommended_next_command,
        label="Run the recommended next evidence action",
    )
    if audited_one_row_session:
        _append_sequence_step(
            steps,
            kind="audited_one_row_evidence_session",
            command=audited_one_row_session,
            label="Create an audited one-row evidence session manifest",
        )
    elif first_one_row:
        _append_sequence_step(
            steps,
            kind="one_row_direct_evidence",
            command=first_one_row["command"],
            label="Capture direct one-row proof evidence",
        )
    elif first_direct:
        _append_sequence_step(
            steps,
            kind="route_direct_evidence",
            command=first_direct["command"],
            label="Capture direct route proof evidence",
        )
    _append_sequence_step(
        steps,
        kind="review_refresh_after_evidence",
        command=review_and_refresh_command,
        label="Review artifacts and refresh closure/promotion reports",
    )
    return steps


def _flag_value(command: str, flag: str) -> str | None:
    try:
        tokens = shlex.split(command)
    except ValueError:
        return None
    try:
        index = tokens.index(flag)
    except ValueError:
        return None
    if index + 1 >= len(tokens):
        return None
    return tokens[index + 1]


def _shell_arg(value: str) -> str:
    safe = set("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789._-/:")
    if value and all(char in safe for char in value):
        return value
    return f'"{value}"'


def _scoped_runner_command(route: str, first_one_row: dict[str, Any] | None) -> str | None:
    if not first_one_row:
        return None
    setting_ids = first_one_row.get("setting_ids") or []
    if not setting_ids:
        return None
    setting_id = setting_ids[0]
    base = (
        f"python3 tools/a7ii_live_session_runner.py --route {route} "
        f"--setting {setting_id} --phase evidence --execute --allow-hardware"
    )
    if route == "smart_remote_wifi":
        method = _flag_value(first_one_row["command"], "--method") or "<Smart Remote setter method>"
        requested_value = settings_smart_remote_values.behavior_requested_value_for_session(setting_id, method)
        if requested_value is None:
            return (
                f"{base} "
                '--smart-remote-observed-before "<observed before value>" '
                '--smart-remote-observed-after "<observed after value>"'
            )
        return (
            f"{base} "
            f"--smart-remote-method {_shell_arg(method)} "
            f"--smart-remote-requested-value {_shell_arg(requested_value)} "
            '--smart-remote-observed-before "<observed before value>" '
            '--smart-remote-observed-after "<observed after value>"'
        )
    if route == "smart_remote_phone":
        return (
            f"{base} "
            '--observed-value "<visible phone-app value>" '
            '--phone-state "<describe phone/app state>"'
        )
    if route == "raw_ptp":
        return (
            f"{base} "
            '--observed-behavior "<describe near/far visual behavior>" '
            '--target-setup "<describe near/far target setup>"'
        )
    if route == "camera_remote_command":
        local_base = base.replace(" --allow-hardware", "")
        return f"{local_base} --package-path /path/to/CameraRemoteCommand-package.zip"
    if route == "guided_physical":
        return f'{base} --observed-state "<describe camera/menu state>"'
    if route == "usb_gphoto":
        return base
    if route == "openmemories":
        return base
    return None


def _risk_kind_counts(commands: list[dict[str, Any]]) -> dict[str, int]:
    risk_counts: dict[str, int] = {}
    for command in commands:
        kind = RISK_KIND.get(command.get("risk"), "unknown")
        risk_counts[kind] = risk_counts.get(kind, 0) + 1
    return dict(sorted(risk_counts.items()))


def _route_next_session(playbook: dict[str, Any], ledger_item: dict[str, Any] | None = None) -> dict[str, Any]:
    route = playbook["route"]
    evidence = playbook["evidence_commands"]
    review = playbook["review_commands"]
    refresh = playbook["refresh_commands"]

    evidence_command = None
    if evidence:
        evidence_command = _runner_command(
            route,
            phase="evidence",
            package_path=route == "camera_remote_command",
        )
    review_refresh_command = None
    if review or refresh:
        review_refresh_command = (
            f"python3 tools/a7ii_live_session_runner.py --route {route} "
            "--phase review --phase refresh --execute"
        )
    latest_session = ledger_item.get("latest_session") if ledger_item else None

    return {
        "route": route,
        "label": playbook["label"],
        "selected_gap_rows": playbook["summary"]["selected_gap_rows"],
        "covered_gap_rows": playbook["summary"]["covered_gap_rows"],
        "missing_gap_rows": playbook["summary"]["missing_gap_rows"],
        "evidence_commands": len(evidence),
        "review_commands": len(review),
        "refresh_commands": len(refresh),
        "risk_kinds": _risk_kind_counts(evidence),
        "prerequisites": playbook["prerequisites"],
        "next_evidence_session_command": evidence_command,
        "review_and_refresh_command": review_refresh_command,
        "first_evidence_command": evidence[0]["command"] if evidence else None,
        "first_review_command": review[0]["command"] if review else None,
        "direct_evidence_commands": _command_queue(evidence),
        "direct_review_commands": _command_queue(review),
        "latest_session": latest_session,
        "latest_session_outcome": latest_session.get("outcome") if latest_session else None,
        "latest_session_timestamp": latest_session.get("timestamp") if latest_session else None,
        "latest_session_manifest_path": latest_session.get("manifest_path") if latest_session else None,
        "latest_session_next_command": ledger_item.get("latest_session_next_command") if ledger_item else None,
        "ledger_next_command": ledger_item.get("next_command") if ledger_item else None,
        "route_recommended_command": ledger_item.get("route_recommended_command") if ledger_item else None,
    }


def _readiness_snapshot(route: str, readiness_by_route: dict[str, dict[str, Any]]) -> dict[str, Any] | None:
    return readiness_by_route.get(route)


def _readiness_missing_names(readiness: dict[str, Any] | None) -> list[str]:
    if not readiness:
        return []
    return [
        item["name"]
        for item in readiness.get("missing_requirements", [])
        if item.get("kind") != "package"
    ]


def _blocked_by(
    session: dict[str, Any],
    missing_reviews: list[str],
    readiness: dict[str, Any] | None,
) -> list[str]:
    route = session["route"]
    blockers = []
    risk_kinds = session["risk_kinds"]
    missing_local = _readiness_missing_names(readiness)
    if missing_local:
        blockers.append("missing_local_readiness")
    if route in missing_reviews:
        blockers.append("missing_latest_review_artifact")
    if route == "camera_remote_command":
        blockers.append("local_sony_package_path")
    if route == "guided_physical":
        blockers.append("one_setting_and_observed_camera_state")
    if route == "smart_remote_phone" and risk_kinds.get("phone_manual"):
        blockers.append("one_phone_setting_and_observed_value")
    if route == "smart_remote_wifi" and risk_kinds.get("smart_remote_behavior_review"):
        blockers.append("one_smart_remote_setting_and_observed_behavior")
    if route == "raw_ptp" and risk_kinds.get("raw_ptp_visual_review"):
        blockers.append("one_raw_ptp_setting_and_visual_behavior")
    if any(kind.startswith("live_camera") for kind in risk_kinds):
        blockers.append("camera_route_state_and_hardware_approval")
    if route == "openmemories":
        blockers.append("reversible_admin_tooling_and_rollback_proof")
    return blockers


def _queue_reason(session: dict[str, Any], missing_reviews: list[str]) -> str:
    route = session["route"]
    if route in missing_reviews:
        return "First produce the missing review source so registry promotion has current evidence."
    if route == "smart_remote_wifi":
        return "Highest-priority cable-free route for capture/focus-safe camera control."
    if route == "usb_gphoto":
        return "Broad exposed PTP/gphoto route with set/read/revert and LAN bridge proof hooks."
    if route == "smart_remote_phone":
        return "Separates phone-app UI preferences from camera-side API control with explicit observations."
    if route == "raw_ptp":
        return "Small but important focus-control route; needs visible near/far behavior proof."
    if route == "camera_remote_command":
        return "Official package review can convert speculation into Sony-documented support rows."
    if route == "openmemories":
        return "Deep reversible camera-hosted route for rows not reachable over normal remote APIs."
    if route == "guided_physical":
        return "Explicit fallback for physical/admin/state gates, one observed setting at a time."
    return "Next route evidence session from the current playbook."


def _next_action_queue(
    next_sessions: list[dict[str, Any]],
    missing_reviews: list[str],
    readiness_by_route: dict[str, dict[str, Any]],
    ledger_by_route: dict[str, dict[str, Any]],
    gap_ids_by_route: dict[str, set[str]],
) -> list[dict[str, Any]]:
    queue = []
    for session in next_sessions:
        evidence_command = session.get("next_evidence_session_command")
        if not evidence_command:
            continue
        direct_commands = _commands_for_gap_ids(
            session.get("direct_evidence_commands") or [],
            gap_ids_by_route.get(session["route"], set()),
        )
        first_direct = _first_command(direct_commands)
        first_one_row = _first_one_row_command(direct_commands)
        route_readiness = _readiness_snapshot(session["route"], readiness_by_route)
        readiness_summary = route_readiness.get("summary", {}) if route_readiness else {}
        missing_requirements = route_readiness.get("missing_requirements", []) if route_readiness else []
        readiness_next_commands = []
        if route_readiness and missing_requirements:
            readiness_next_commands = route_readiness.get("next_commands", [])
        blockers = _blocked_by(session, missing_reviews, route_readiness)
        route_prerequisite_command = (
            readiness_next_commands[0]
            if readiness_next_commands
            else first_direct["command"]
            if first_direct
            else evidence_command
        )
        audited_one_row_session = _scoped_runner_command(session["route"], first_one_row)
        audited_session = audited_one_row_session or evidence_command
        recommended_next_command, recommended_next_command_kind = _recommended_next_command(
            readiness_next_commands=readiness_next_commands,
            audited_one_row_session=audited_one_row_session,
            first_one_row=first_one_row,
            first_direct=first_direct,
            evidence_command=evidence_command,
        )
        next_one_row_concrete_command = first_one_row["command"] if first_one_row else None
        review_and_refresh_command = session.get("review_and_refresh_command")
        ledger_item = ledger_by_route.get(session["route"], {})
        latest_session = ledger_item.get("latest_session")
        recommended_sequence = _recommended_command_sequence(
            readiness_next_commands=readiness_next_commands,
            route_prerequisite_command=route_prerequisite_command,
            recommended_next_command=recommended_next_command,
            recommended_next_command_kind=recommended_next_command_kind,
            audited_one_row_session=audited_one_row_session,
            first_one_row=first_one_row,
            first_direct=first_direct,
            review_and_refresh_command=review_and_refresh_command,
        )
        queue.append(
            {
                "rank": NEXT_ACTION_ROUTE_ORDER.get(session["route"], 999),
                "route": session["route"],
                "label": session["label"],
                "selected_gap_rows": session["selected_gap_rows"],
                "risk_kinds": session["risk_kinds"],
                "local_status": readiness_summary.get("local_status", "unknown"),
                "live_status": readiness_summary.get("live_status", "unknown"),
                "ready_to_attempt": readiness_summary.get("ready_to_attempt", False),
                "missing_requirements": [
                    {
                        "kind": item.get("kind"),
                        "name": item.get("name"),
                        "status": item.get("status"),
                        "path": item.get("path"),
                    }
                    for item in missing_requirements
                ],
                "manual_requirements": readiness_summary.get("manual_requirements", 0),
                "blocked_by": blockers,
                "why": _queue_reason(session, missing_reviews),
                "next_command": evidence_command,
                "recommended_sequence_session_command": _recommended_sequence_session_command(session["route"]),
                "audited_session_command": audited_session,
                "audited_one_row_session_command": audited_one_row_session,
                "next_one_row_audited_session_command": audited_one_row_session,
                "readiness_next_commands": readiness_next_commands,
                "readiness_next_action": route_prerequisite_command,
                "direct_command_count": len(session.get("direct_evidence_commands") or []),
                "next_concrete_command": route_prerequisite_command,
                "route_prerequisite_command": route_prerequisite_command,
                "recommended_next_command": recommended_next_command,
                "recommended_next_command_kind": recommended_next_command_kind,
                "recommended_command_sequence": recommended_sequence,
                "first_direct_command": first_direct["command"] if first_direct else None,
                "first_direct_probe_id": first_direct.get("probe_id") if first_direct else None,
                "first_one_row_direct_command": first_one_row["command"] if first_one_row else None,
                "next_one_row_concrete_command": next_one_row_concrete_command,
                "first_one_row_probe_id": first_one_row.get("probe_id") if first_one_row else None,
                "first_one_row_setting_ids": first_one_row.get("setting_ids", []) if first_one_row else [],
                "review_and_refresh_command": review_and_refresh_command,
                "latest_session": latest_session,
                "latest_session_outcome": latest_session.get("outcome") if latest_session else None,
                "latest_session_timestamp": latest_session.get("timestamp") if latest_session else None,
                "latest_session_manifest_path": latest_session.get("manifest_path") if latest_session else None,
                "latest_session_setting_id": latest_session.get("setting_id") if latest_session else None,
                "latest_session_next_command": ledger_item.get("latest_session_next_command"),
                "ledger_next_command": ledger_item.get("next_command"),
                "route_recommended_command": ledger_item.get("route_recommended_command"),
            }
        )
    return sorted(queue, key=lambda item: (item["rank"], -item["selected_gap_rows"], item["route"]))


def _ledger_by_route(ledger_report: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {item["route"]: item for item in ledger_report.get("latest_by_route", [])}


def _commands_for_gap_ids(commands: list[dict[str, Any]], gap_ids: set[str]) -> list[dict[str, Any]]:
    if not gap_ids:
        return commands
    out = []
    for command in commands:
        setting_ids = set(command.get("setting_ids") or [])
        if setting_ids and not setting_ids & gap_ids:
            continue
        out.append(command)
    return out


def _ledger_summary_for_routes(
    ledger_report: dict[str, Any],
    routes: list[str],
    ledger_by_route: dict[str, dict[str, Any]],
) -> dict[str, Any]:
    selected_rows = [ledger_by_route.get(route, {}) for route in routes]
    return {
        "session_ledger_sessions": ledger_report.get("summary", {}).get("sessions", 0),
        "session_ledger_unreadable_manifests": ledger_report.get("summary", {}).get(
            "unreadable_manifests", 0
        ),
        "routes_with_session_history": sum(1 for item in selected_rows if item.get("has_session")),
        "routes_with_ledger_next_command": sum(1 for item in selected_rows if item.get("next_command")),
    }


def _active_sessions_from_closure(
    next_sessions: list[dict[str, Any]],
    closure: dict[str, Any],
    missing_reviews: list[str],
) -> list[dict[str, Any]]:
    gap_counts = {
        section["route"]: section["gap_setting_count"]
        for section in closure.get("routes", [])
    }
    gap_ids_by_route = _gap_ids_by_route(closure)
    out = []
    for session in next_sessions:
        route = session["route"]
        gap_count = gap_counts.get(route, session["selected_gap_rows"])
        if route in missing_reviews or gap_count > 0:
            filtered_evidence = _commands_for_gap_ids(
                session.get("direct_evidence_commands") or [],
                gap_ids_by_route.get(route, set()),
            )
            active_session = dict(session)
            active_session["selected_gap_rows"] = gap_count
            active_session["direct_evidence_commands"] = filtered_evidence
            active_session["evidence_commands"] = len(filtered_evidence)
            active_session["first_evidence_command"] = (
                filtered_evidence[0]["command"] if filtered_evidence else None
            )
            active_session["risk_kinds"] = _risk_kind_counts(filtered_evidence)
            out.append(active_session)
    return out


def _gap_ids_by_route(closure: dict[str, Any]) -> dict[str, set[str]]:
    return {
        section["route"]: set(section.get("gap_setting_ids") or [])
        for section in closure.get("routes", [])
    }


def _session_totals(sessions: list[dict[str, Any]]) -> dict[str, int]:
    return {
        "routes": len(sessions),
        "ordered_session_commands": sum(
            item["evidence_commands"] + item["review_commands"] + item.get("refresh_commands", 0)
            for item in sessions
        ),
        "direct_evidence_commands": sum(item["evidence_commands"] for item in sessions),
    }


def _requirements(
    closure: dict[str, Any],
    promotions: dict[str, Any],
    session_playbook: dict[str, Any],
) -> list[dict[str, Any]]:
    summary = closure["summary"]
    missing_probe_rows = summary["missing_probe_command_rows"]
    missing_reviews = summary["missing_review_sources"]
    missing_session_rows = session_playbook["summary"]["missing_gap_rows"]
    selected_gaps = session_playbook["summary"]["selected_gap_rows"]
    applicable_promotions = promotions["summary"]["applicable_promotions"]
    return [
        {
            "requirement": "canonical_settings_registry",
            "status": "complete" if summary["total_settings"] >= 150 else "incomplete",
            "evidence": f"{summary['total_settings']} settings loaded from registry",
        },
        {
            "requirement": "all_gap_rows_have_probe_commands",
            "status": "complete" if not missing_probe_rows else "incomplete",
            "evidence": f"{summary['probe_commands']} proof commands; missing rows: {len(missing_probe_rows)}",
            "missing_rows": missing_probe_rows,
        },
        {
            "requirement": "route_session_playbooks_cover_gap_rows",
            "status": "complete" if not missing_session_rows else "incomplete",
            "evidence": f"{session_playbook['summary']['covered_gap_rows']}/{selected_gaps} route-session gap rows covered",
            "missing_rows": missing_session_rows,
        },
        {
            "requirement": "live_route_review_artifacts_loaded",
            "status": "needs_live_evidence" if missing_reviews else "complete",
            "evidence": f"missing review sources: {', '.join(missing_reviews) or 'none'}",
            "missing_review_sources": missing_reviews,
        },
        {
            "requirement": "behavior_proven_promotions_available",
            "status": "waiting_for_evidence" if applicable_promotions == 0 and selected_gaps else "ready",
            "evidence": f"{applicable_promotions} applicable registry promotions from current artifacts",
        },
    ]


def build_report(
    *,
    registry: dict[str, Any] | None = None,
    route: str | None = None,
    package_path: str | None = None,
    session_ledger: dict[str, Any] | None = None,
    tool_resolver: settings_readiness.ToolResolver | None = None,
    path_exists: settings_readiness.PathExists | None = None,
) -> dict[str, Any]:
    registry = registry or settings_registry.load_registry()
    closure = settings_closure.build_report(registry=registry, sample_limit=3)
    promotions = settings_promotions.build_promotion_plan(registry=registry, applicable_only=True)
    session_playbook = settings_sessions.build_session_playbook(registry=registry, route=route)
    ledger_report = session_ledger or settings_session_ledger.build_ledger(route=route)
    ledger_by_route = _ledger_by_route(ledger_report)
    next_sessions = [
        _route_next_session(item, ledger_item=ledger_by_route.get(item["route"]))
        for item in session_playbook["routes"]
    ]
    missing_reviews = closure["summary"]["missing_review_sources"]
    active_next_sessions = _active_sessions_from_closure(next_sessions, closure, missing_reviews)
    readiness_report = settings_readiness.build_readiness_report(
        registry=registry,
        route=route,
        package_path=package_path,
        tool_resolver=tool_resolver,
        path_exists=path_exists,
    )
    readiness_by_route = {item["route"]: item for item in readiness_report["routes"]}
    next_action_queue = _next_action_queue(
        active_next_sessions,
        missing_reviews,
        readiness_by_route,
        ledger_by_route,
        _gap_ids_by_route(closure),
    )
    selected_gaps = closure["summary"]["selected_gap_rows"]
    missing_session_rows = session_playbook["summary"]["missing_gap_rows"]
    route_names = [item["route"] for item in active_next_sessions] or [
        item["route"] for item in session_playbook["routes"]
    ]
    ledger_summary = _ledger_summary_for_routes(ledger_report, route_names, ledger_by_route)
    active_totals = _session_totals(active_next_sessions)
    completion_state = "ready_for_final_audit"
    if closure["summary"]["missing_probe_command_rows"] or missing_session_rows:
        completion_state = "registry_probe_coverage_incomplete"
    elif missing_reviews or selected_gaps:
        completion_state = "needs_live_evidence"

    return {
        "summary": {
            "completion_state": completion_state,
            "total_settings": closure["summary"]["total_settings"],
            "selected_gap_rows": selected_gaps,
            "covered_gap_rows": session_playbook["summary"]["covered_gap_rows"],
            "missing_probe_command_rows": closure["summary"]["missing_probe_command_rows"],
            "proof_commands": closure["summary"]["probe_commands"],
            "missing_review_sources": missing_reviews,
            "applicable_promotions": promotions["summary"]["applicable_promotions"],
            "routes": active_totals["routes"],
            "ordered_session_commands": active_totals["ordered_session_commands"],
            "direct_evidence_commands": active_totals["direct_evidence_commands"],
            "next_action_queue": len(next_action_queue),
            "ready_to_attempt_routes": readiness_report["summary"]["ready_to_attempt"],
            "missing_readiness_requirements": readiness_report["summary"]["missing_requirements"],
            **ledger_summary,
        },
        "requirements": _requirements(closure, promotions, session_playbook),
        "readiness_summary": readiness_report["summary"],
        "session_ledger_summary": ledger_report.get("summary", {}),
        "next_action_queue": next_action_queue,
        "next_live_sessions": active_next_sessions,
        "blocking_notes": closure["blocking_notes"],
    }
