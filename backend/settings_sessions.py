"""Route-centric live proof playbooks for A7 II settings work.

The probe planner knows individual commands. This module groups those commands into operator-facing
sessions: prepare the camera route, capture artifacts, review them locally, then refresh closure and
promotion reports.
"""

from __future__ import annotations

from typing import Any

import settings_closure
import settings_probe_plan
import settings_registry

LOCAL_REVIEW_RISKS = {"local_artifact_review"}
LOCAL_EVIDENCE_RISKS = {"local_package_discovery", "local_package_read"}

ROUTE_NOTES = {
    "smart_remote_wifi": [
        "Launch Smart Remote Control on the A7 II.",
        "Join the camera DIRECT Wi-Fi network from this Mac.",
        "Keep USB connected only if you are intentionally testing USB/Wi-Fi coexistence.",
    ],
    "smart_remote_phone": [
        "Connect the phone app preference context when possible.",
        "Treat phone overlay preferences separately from camera-side settings until API evidence says otherwise.",
    ],
    "usb_gphoto": [
        "Set USB Connection to PC Remote before starting.",
        "Move through stills, movie-capable, manual-focus, autofocus, and Smart Remote-exited states as prompted.",
        "Use set/read/revert only on safe rows and keep destructive/admin rows guarded.",
    ],
    "raw_ptp": [
        "Use USB PC Remote state with the pmca/pyusb environment available.",
        "Prepare a controlled near/far target so focus behavior can be visually judged.",
        "Protocol acceptance alone is not enough; behavior proof is required before promotion.",
    ],
    "camera_remote_command": [
        "Obtain the official Sony Camera Remote Command package through the approved path.",
        "Do not commit or redistribute Sony package contents.",
        "Package evidence identifies candidates; behavior proof is still required before registry promotion.",
    ],
    "guided_physical": [
        "Record visible camera state and photos/screenshots for physical/admin/state gates.",
        "Do not perform destructive actions such as format, delete, reset, or network reset.",
    ],
}


def _split_commands(commands: list[dict[str, Any]]) -> dict[str, list[dict[str, Any]]]:
    evidence = []
    review = []
    for command in commands:
        if command["risk"] in LOCAL_REVIEW_RISKS:
            review.append(command)
        else:
            evidence.append(command)
    return {"evidence": evidence, "review": review}


def _unique_prerequisites(commands: list[dict[str, Any]]) -> list[str]:
    out = []
    seen = set()
    for command in commands:
        prerequisite = str(command.get("prerequisite") or "").strip()
        if not prerequisite or prerequisite in seen:
            continue
        seen.add(prerequisite)
        out.append(prerequisite)
    return out


def _is_scoped_to_setting(command: dict[str, Any], setting_id: str) -> bool:
    text = command["command"]
    return (
        f"--setting {setting_id}" in text
        or f"--setting-id {setting_id}" in text
        or f"/settings/{setting_id}" in text
    )


def _is_one_row_command(command: dict[str, Any]) -> bool:
    setting_ids = command.get("setting_ids", [])
    if command.get("setting_count", len(setting_ids)) != 1 or len(setting_ids) != 1:
        return False
    return _is_scoped_to_setting(command, str(setting_ids[0]))


def _next_concrete_command(
    evidence_commands: list[dict[str, Any]],
    *,
    setting_id: str | None,
) -> str | None:
    if setting_id:
        for command in evidence_commands:
            if _is_scoped_to_setting(command, setting_id):
                return command["command"]
    for command in evidence_commands:
        if _is_one_row_command(command):
            return command["command"]
    return evidence_commands[0]["command"] if evidence_commands else None


def recommended_sequence_session_command(route: str, setting_id: str | None = None) -> str:
    scoped_setting = f" --setting {setting_id}" if setting_id else ""
    return f"python3 tools/a7ii_live_session_runner.py --route {route}{scoped_setting} --recommended-sequence"


def _refresh_commands(route: str) -> list[dict[str, Any]]:
    commands = [
        {
            "title": "Refresh route closure report",
            "command": (
                "python3 tools/a7ii_evidence_closure_report.py "
                "--out artifacts/falsification/a7ii_evidence_closure.md"
            ),
            "risk": "local_artifact_review",
            "proves": "Updated route closure counts after the session artifacts are reviewed.",
            "artifact_root": "artifacts/falsification/a7ii_evidence_closure.md",
        },
    ]
    if route != "guided_physical":
        commands.append(
            {
                "title": "Refresh route promotion plan",
                "command": (
                    "python3 tools/a7ii_registry_promotion_plan.py "
                    f"--route {route} "
                    "--out artifacts/falsification/a7ii_registry_promotion_plan.md"
                ),
                "risk": "local_artifact_review",
                "proves": "Which reviewed rows can safely be promoted after this route session.",
                "artifact_root": "artifacts/falsification/a7ii_registry_promotion_plan.md",
            }
        )
        commands.append(
            {
                "title": "Show applicable route promotions only",
                "command": f"python3 tools/a7ii_registry_promotion_plan.py --route {route} --applicable-only",
                "risk": "local_artifact_review",
                "proves": "Only behavior-proven registry promotions, if any.",
                "artifact_root": "stdout",
            }
        )
        commands.append(
            {
                "title": "Dry-run applicable registry promotions",
                "command": f"python3 tools/a7ii_apply_registry_promotions.py --route {route}",
                "risk": "local_artifact_review",
                "proves": "Exact registry changes that would be applied after behavior proof, without writing by default.",
                "artifact_root": "stdout",
            }
        )
    return commands


def build_route_playbook(
    route: str,
    *,
    registry: dict[str, Any] | None = None,
    setting_id: str | None = None,
) -> dict[str, Any]:
    registry = registry or settings_registry.load_registry()
    if route == "openmemories":
        return {
            "route": route, "setting_id": setting_id,
            "label": settings_closure.ROUTE_LABELS.get(route, route),
            "notes": ["Permanently disabled: no Route 3 app, camera, LAN, or rollback command is available."],
            "summary": {"selected_gap_rows": 0, "covered_gap_rows": 0, "missing_gap_rows": [],
                        "evidence_commands": 0, "review_commands": 0, "refresh_commands": 0, "ordered_commands": 0,
                        "status": "permanently_disabled"},
            "prerequisites": [], "next_concrete_command": None,
            "recommended_sequence_session_command": None,
            "evidence_commands": [], "review_commands": [], "refresh_commands": [], "ordered_commands": [],
        }
    report = settings_probe_plan.build_probe_report(
        registry,
        route=route,
        setting_id=setting_id,
    )
    split = _split_commands(report["commands"])
    ordered = [*split["evidence"], *split["review"], *_refresh_commands(route)]
    next_concrete = _next_concrete_command(split["evidence"], setting_id=setting_id)
    return {
        "route": route,
        "setting_id": setting_id,
        "label": settings_closure.ROUTE_LABELS.get(route, route),
        "notes": ROUTE_NOTES.get(route, []),
        "summary": {
            **report["summary"],
            "evidence_commands": len(split["evidence"]),
            "review_commands": len(split["review"]),
            "refresh_commands": len(_refresh_commands(route)),
            "ordered_commands": len(ordered),
        },
        "prerequisites": _unique_prerequisites(split["evidence"]),
        "next_concrete_command": next_concrete,
        "recommended_sequence_session_command": recommended_sequence_session_command(route, setting_id),
        "evidence_commands": split["evidence"],
        "review_commands": split["review"],
        "refresh_commands": _refresh_commands(route),
        "ordered_commands": ordered,
    }


def build_session_playbook(
    *,
    registry: dict[str, Any] | None = None,
    route: str | None = None,
    setting_id: str | None = None,
) -> dict[str, Any]:
    registry = registry or settings_registry.load_registry()
    routes = [route] if route else list(settings_closure.ROUTE_ORDER)
    playbooks = [
        build_route_playbook(item, registry=registry, setting_id=setting_id)
        for item in routes
    ]
    return {
        "summary": {
            "routes": len(playbooks),
            "selected_gap_rows": sum(item["summary"]["selected_gap_rows"] for item in playbooks),
            "covered_gap_rows": sum(item["summary"]["covered_gap_rows"] for item in playbooks),
            "missing_gap_rows": sorted(
                {
                    setting_id
                    for item in playbooks
                    for setting_id in item["summary"]["missing_gap_rows"]
                }
            ),
            "evidence_commands": sum(item["summary"]["evidence_commands"] for item in playbooks),
            "review_commands": sum(item["summary"]["review_commands"] for item in playbooks),
            "refresh_commands": sum(item["summary"]["refresh_commands"] for item in playbooks),
            "ordered_commands": sum(item["summary"]["ordered_commands"] for item in playbooks),
        },
        "routes": playbooks,
    }
