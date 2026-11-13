"""Camera-free work queue for the A7 II full settings-control project.

This report answers one narrow question: what can be improved or audited without a live camera
attached, and what still truly needs a live route proof.
"""

from __future__ import annotations

from typing import Any

import settings_closure
import settings_live_checklist
import settings_registry

NO_CAMERA_COMMANDS = (
    {
        "kind": "offline_report_refresh",
        "route": "all",
        "label": "Refresh this camera-free work report",
        "command": (
            "python3 tools/a7ii_offline_work_report.py "
            "--out artifacts/falsification/a7ii_offline_work.md --quiet"
        ),
    },
    {
        "kind": "acceptance_report_refresh",
        "route": "all",
        "label": "Refresh acceptance and next-action queue",
        "command": (
            "python3 tools/a7ii_acceptance_report.py "
            "--out artifacts/falsification/a7ii_acceptance.md --quiet --limit-direct-commands 2"
        ),
    },
    {
        "kind": "live_checklist_refresh",
        "route": "all",
        "label": "Refresh remaining live-tests-only checklist",
        "command": (
            "python3 tools/a7ii_live_evidence_checklist.py "
            "--out artifacts/falsification/a7ii_live_checklist.md --quiet --limit-direct-commands 1"
        ),
    },
    {
        "kind": "completion_audit_refresh",
        "route": "all",
        "label": "Refresh requirement-level completion audit",
        "command": (
            "python3 tools/a7ii_completion_audit.py "
            "--out artifacts/falsification/a7ii_completion_audit.md --quiet --limit-commands 2"
        ),
    },
    {
        "kind": "triage_report_refresh",
        "route": "all",
        "label": "Refresh unproven-closed settings triage",
        "command": (
            "python3 tools/a7ii_unproven_closed_triage.py "
            "--out artifacts/falsification/a7ii_unproven_closed_triage.md --quiet"
        ),
    },
)


ROUTE_OFFLINE_ACTIONS = {
    "smart_remote_wifi": (
        {
            "kind": "artifact_review",
            "label": "Review latest Smart Remote surface/behavior artifacts if present",
            "command": (
                "python3 tools/review_smart_remote_api_surface.py "
                "artifacts/falsification/r1_api_surface "
                "--out-dir artifacts/falsification/r1_api_surface_review"
            ),
        },
    ),
    "usb_gphoto": (
        {
            "kind": "artifact_review",
            "label": "Review latest USB/gphoto artifacts if present",
            "command": "python3 tools/review_usb_gphoto_artifacts.py",
        },
    ),
    "smart_remote_phone": (
        {
            "kind": "artifact_review",
            "label": "Review latest phone preference screenshots/artifacts if present",
            "command": (
                "python3 tools/review_smart_remote_phone_preferences.py "
                "artifacts/falsification/r1_phone_settings_manual "
                "--out-dir artifacts/falsification/r1_phone_settings_api_surface_review"
            ),
        },
    ),
    "camera_remote_command": (
        {
            "kind": "local_package_locator",
            "label": "Search local files for the official Sony Camera Remote Command package",
            "command": (
                "python3 tools/find_camera_remote_command_package.py --include-common-search "
                "--out-dir artifacts/falsification/r4_camera_remote_command"
            ),
        },
        {
            "kind": "local_package_review",
            "label": "Review a local package after it has been obtained through Sony's approved path",
            "command": (
                "python3 tools/review_camera_remote_command_package.py "
                "/path/to/CameraRemoteCommand-package.zip "
                "--out-dir artifacts/falsification/r4_camera_remote_command"
            ),
        },
    ),
    "openmemories": (),
    "guided_physical": (
        {
            "kind": "gate_report_refresh",
            "label": "Refresh physical/admin gate ownership report",
            "command": "python3 tools/a7ii_remaining_gates_report.py --out artifacts/falsification/a7ii_remaining_gates.md",
        },
    ),
}


def _offline_class(route_item: dict[str, Any]) -> str:
    status = route_item["status"]
    route = route_item["route"]
    if status == "no_action":
        return "no_action_or_stopped"
    if route == "camera_remote_command":
        return "waiting_external_package"
    if status == "ready_for_operator":
        return "needs_live_camera"
    if status == "missing_local_prerequisite":
        return "offline_prerequisite_first"
    return "needs_review"


def _route_offline_actions(route: str) -> list[dict[str, Any]]:
    return [
        {"route": route, **action}
        for action in ROUTE_OFFLINE_ACTIONS.get(route, ())
    ]


def _route_item(route_item: dict[str, Any]) -> dict[str, Any]:
    route = route_item["route"]
    offline_class = _offline_class(route_item)
    camera_required = offline_class == "needs_live_camera"
    return {
        "route": route,
        "label": route_item["label"],
        "offline_class": offline_class,
        "status": route_item["status"],
        "proof_goal": route_item["proof_goal"],
        "camera_required": camera_required,
        "selected_gap_rows": route_item["selected_gap_rows"],
        "closed_without_proof_count": route_item.get("closed_without_proof_count", 0),
        "direct_command_count": route_item["direct_command_count"],
        "first_one_row_setting_ids": route_item.get("first_one_row_setting_ids", []),
        "recommended_next_command": route_item.get("recommended_next_command"),
        "recommended_next_command_kind": route_item.get("recommended_next_command_kind"),
        "audited_session_command": route_item.get("audited_session_command"),
        "next_concrete_command": route_item.get("next_concrete_command"),
        "review_and_refresh_command": route_item.get("review_and_refresh_command"),
        "blocked_by": route_item.get("blocked_by", []),
        "risk_kinds": route_item.get("risk_kinds", {}),
        "missing_requirements": route_item.get("missing_requirements", []),
        "manual_requirements": route_item.get("manual_requirements", []),
        "offline_actions": _route_offline_actions(route),
        "live_action": route_item.get("recommended_next_command") if camera_required else None,
    }


def build_report(
    *,
    registry: dict[str, Any] | None = None,
    route: str | None = None,
    package_path: str | None = None,
) -> dict[str, Any]:
    registry = registry or settings_registry.load_registry()
    if route is not None and route not in settings_closure.ROUTE_ORDER:
        raise ValueError(f"unknown route: {route}")
    checklist = settings_live_checklist.build_report(
        registry=registry,
        route=route,
        package_path=package_path,
        direct_command_limit=1,
    )
    routes = [_route_item(item) for item in checklist["routes"]]
    class_counts: dict[str, int] = {}
    for item in routes:
        class_counts[item["offline_class"]] = class_counts.get(item["offline_class"], 0) + 1

    camera_free_route_actions = [
        action
        for item in routes
        for action in item["offline_actions"]
    ]
    camera_required_routes = [item for item in routes if item["camera_required"]]
    external_package_routes = [
        item for item in routes if item["offline_class"] == "waiting_external_package"
    ]
    no_action_routes = [
        item for item in routes if item["offline_class"] == "no_action_or_stopped"
    ]
    closed_without_proof_rows = checklist["summary"].get("closed_without_proof_rows", 0)
    if camera_required_routes:
        interpretation = (
            "Registry ownership is closed; final completion is gated by the remaining live route "
            "proofs and the local Sony package review; physical/admin gates are observation-owned."
        )
    else:
        interpretation = (
            "Registry ownership is closed; no live camera route is currently queued. Final completion "
            "is gated by the local Sony Camera Remote Command package review; physical/admin gates "
            "are observation-owned."
        )
    if closed_without_proof_rows:
        interpretation += (
            f" Caution: {closed_without_proof_rows} route gap row(s) were closed without behavior "
            "proof (candidate/failed/route-level evidence) and remain unproven."
        )

    return {
        "summary": {
            "completion_state": checklist["summary"]["completion_state"],
            "routes": len(routes),
            "offline_class_counts": dict(sorted(class_counts.items())),
            "camera_free_action_count": len(NO_CAMERA_COMMANDS) + len(camera_free_route_actions),
            "remaining_live_test_routes": len(camera_required_routes),
            "external_package_routes": len(external_package_routes),
            "no_action_or_stopped_routes": len(no_action_routes),
            "selected_gap_rows": checklist["summary"]["selected_gap_rows"],
            "closed_without_proof_rows": closed_without_proof_rows,
            "evidence_commands": checklist["summary"]["evidence_commands"],
            "missing_review_sources": checklist["summary"]["missing_review_sources"],
            "interpretation": interpretation,
        },
        "no_camera_global_commands": list(NO_CAMERA_COMMANDS),
        "no_camera_route_actions": camera_free_route_actions,
        "remaining_live_tests": camera_required_routes,
        "waiting_external_package": external_package_routes,
        "no_action_or_stopped": no_action_routes,
        "routes": routes,
    }


def to_markdown(report: dict[str, Any]) -> str:
    summary = report["summary"]
    lines = [
        "# A7 II Camera-Free Work Report",
        "",
        f"- completion_state: `{summary['completion_state']}`",
        f"- routes: `{summary['routes']}`",
        f"- remaining_live_test_routes: `{summary['remaining_live_test_routes']}`",
        f"- external_package_routes: `{summary['external_package_routes']}`",
        f"- no_action_or_stopped_routes: `{summary['no_action_or_stopped_routes']}`",
        f"- closed_without_proof_rows: `{summary['closed_without_proof_rows']}`",
        f"- camera_free_action_count: `{summary['camera_free_action_count']}`",
        f"- missing_review_sources: `{', '.join(summary['missing_review_sources']) or 'none'}`",
        f"- interpretation: {summary['interpretation']}",
        "",
        "## Offline Class Counts",
        "",
    ]
    for key, count in summary["offline_class_counts"].items():
        lines.append(f"- `{key}`: {count}")

    lines.extend(["", "## Can Run Without Camera", ""])
    for action in report["no_camera_global_commands"] + report["no_camera_route_actions"]:
        lines.append(f"### {action['label']}")
        lines.append("")
        lines.append(f"- kind: `{action['kind']}`")
        lines.append(f"- route: `{action['route']}`")
        lines.extend(["", "```bash", action["command"], "```", ""])

    lines.extend(["", "## Remaining Live Tests Only", ""])
    for route in report["remaining_live_tests"]:
        lines.append(f"### {route['label']}")
        lines.append("")
        lines.append(f"- route: `{route['route']}`")
        lines.append(f"- selected_gap_rows: `{route['selected_gap_rows']}`")
        lines.append(f"- first_one_row_setting_ids: `{', '.join(route['first_one_row_setting_ids']) or 'none'}`")
        if route["blocked_by"]:
            lines.append(f"- blocked_by: `{', '.join(route['blocked_by'])}`")
        if route["live_action"]:
            lines.extend(["", "```bash", route["live_action"], "```", ""])

    if report["waiting_external_package"]:
        lines.extend(["", "## Waiting External Package", ""])
        for route in report["waiting_external_package"]:
            lines.append(f"### {route['label']}")
            lines.append("")
            for action in route["offline_actions"]:
                lines.append(f"- {action['label']}")
                lines.extend(["", "```bash", action["command"], "```", ""])

    if report["no_action_or_stopped"]:
        lines.extend(["", "## No Action Or Stopped", ""])
        for route in report["no_action_or_stopped"]:
            if route["closed_without_proof_count"]:
                lines.append(
                    f"- `{route['route']}`: `{route['status']}` "
                    f"({route['closed_without_proof_count']} gap row(s) closed without behavior proof)"
                )
            else:
                lines.append(f"- `{route['route']}`: `{route['status']}`")
    return "\n".join(lines).rstrip() + "\n"
