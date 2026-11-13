"""Operator-ready closure report for A7 II settings-control gaps.

This is the shared implementation behind the FastAPI closure endpoint and the CLI report. It keeps
the registry, route-review artifacts, precondition solver, and probe planner tied to one source of
truth.
"""

from __future__ import annotations

from typing import Any

import settings_evidence
import settings_preconditions
import settings_probe_plan
import settings_registry

ROUTE_LABELS = {
    "smart_remote_wifi": "Smart Remote Wi-Fi",
    "smart_remote_phone": "Smart Remote Phone Preferences",
    "usb_gphoto": "USB/gphoto",
    "raw_ptp": "Raw Sony PTP",
    "camera_remote_command": "Sony Camera Remote Command",
    "openmemories": "OpenMemories Route 3",
    "guided_physical": "Guided Physical/Admin Gates",
}

ROUTE_ORDER = tuple(ROUTE_LABELS)

# Closing verdicts that carry behavior proof or an observed terminal camera constraint.
# Any other closing verdict (candidate sightings, failed attempts, route-level app proof)
# removes the row from the route queue without proving the setting; those rows are
# reported separately as closed_without_proof so they cannot silently disappear.
BEHAVIOR_PROOF_VERDICTS = {
    "behavior_proven",
    "proven_set_read_revert",
    "movie_record_action_proven",
    "present_read_only_state_gated",
    "proof_app_setting_write_proven",
    "proof_app_setting_endpoint_proven",
    "phone_app_manual_preference_observed",
    "manual_gate_recorded_not_software_controlled",
    "dangerous_admin_observed_no_action",
    "dangerous_admin_needs_explicit_approval",
    "unsupported_on_a7ii",
    "rollback_ports_closed",
}


def _route_source_status(evidence_report: dict[str, Any], route: str) -> dict[str, Any] | None:
    for source in evidence_report["sources"]:
        if source["route"] == route:
            return source
    return None


def _route_review_closes_gap(evidence_report: dict[str, Any], setting_id: str, route: str) -> bool:
    closing_verdicts_by_route = {
        "guided_physical": {
            "dangerous_admin_observed_no_action",
            "manual_gate_recorded_not_software_controlled",
        },
        "smart_remote_phone": {
            "phone_app_manual_preference_observed",
        },
        "smart_remote_wifi": {
            "behavior_observed_not_proven",
            "behavior_proven",
            "getter_candidate_seen_in_artifact",
            "service_method_candidate_seen_in_artifact",
            "setter_attempt_failed",
            "candidate_admin_needs_approval",
        },
        "usb_gphoto": {
            "failed_set_read_revert",
            "proven_set_read_revert",
            "movie_record_action_proven",
            "present_read_only_state_gated",
        },
        "raw_ptp": {
            "behavior_rejected",
        },
    }
    closing_verdicts = closing_verdicts_by_route.get(route)
    if not closing_verdicts:
        return False
    for item in evidence_report["settings"]:
        if item["setting_id"] != setting_id:
            continue
        return any(
            review.get("route") == route and review.get("verdict") in closing_verdicts
            for review in item["reviews"]
        )
    return False


def _rows_for_route(registry: dict[str, Any], route: str, evidence_report: dict[str, Any]) -> list[dict[str, Any]]:
    return [
        row
        for row in registry["settings"]
        if route in row.get("routes", {}) and settings_probe_plan.row_needs_evidence(row, route)
        and not (route == "usb_gphoto" and not row.get("routes", {}).get(route, {}).get("node"))
        and not _route_review_closes_gap(evidence_report, row["setting_id"], route)
    ]


def _closed_without_proof_rows(
    registry: dict[str, Any], route: str, evidence_report: dict[str, Any]
) -> list[dict[str, Any]]:
    """Rows removed from the route gap list by a review whose verdicts carry no behavior proof."""
    reviews_by_id = {item["setting_id"]: item["reviews"] for item in evidence_report["settings"]}
    rows = []
    for row in registry["settings"]:
        if route not in row.get("routes", {}) or not settings_probe_plan.row_needs_evidence(row, route):
            continue
        if route == "usb_gphoto" and not row.get("routes", {}).get(route, {}).get("node"):
            continue
        if not _route_review_closes_gap(evidence_report, row["setting_id"], route):
            continue
        verdicts = {
            str(review.get("verdict"))
            for review in reviews_by_id.get(row["setting_id"], [])
            if review.get("route") == route and review.get("verdict")
        }
        if verdicts & BEHAVIOR_PROOF_VERDICTS:
            continue
        rows.append({"setting_id": row["setting_id"], "verdicts": sorted(verdicts)})
    return rows


def _sample_rows(rows: list[dict[str, Any]], limit: int) -> list[dict[str, Any]]:
    return [
        {
            "setting_id": row["setting_id"],
            "label": row["label"],
            "status": row["status"],
            "next_action": row["next_action"],
        }
        for row in sorted(rows, key=lambda item: item["setting_id"])[:limit]
    ]


def _commands_for_route(registry: dict[str, Any], route: str) -> dict[str, Any]:
    report = settings_probe_plan.build_probe_report(registry, route=route)
    return {
        "summary": report["summary"],
        "commands": report["commands"],
    }


def _reviewed_route_counts(evidence_report: dict[str, Any], route: str) -> dict[str, int]:
    reviewed = 0
    missing = 0
    for item in evidence_report["settings"]:
        if route not in item["route_names"]:
            continue
        if any(review.get("route") == route for review in item["reviews"]):
            reviewed += 1
        elif route in item["missing_review_routes"]:
            missing += 1
    return {"reviewed_settings": reviewed, "missing_review_settings": missing}


def _build_route_section(
    registry: dict[str, Any],
    evidence_report: dict[str, Any],
    route: str,
    *,
    sample_limit: int,
) -> dict[str, Any]:
    source = _route_source_status(evidence_report, route)
    if route == "openmemories":
        return {
            "route": route,
            "label": ROUTE_LABELS[route],
            "source_status": source,
            "verdict_counts": {},
            "reviewed_settings": 0,
            "missing_review_settings": 0,
            "gap_setting_count": 0,
            "gap_setting_ids": [],
            "gap_setting_sample": [],
            "closed_without_proof_count": 0,
            "closed_without_proof_setting_ids": [],
            "closed_without_proof_verdicts": {},
            "probe_summary": {"selected_gap_rows": 0, "covered_gap_rows": 0, "missing_gap_rows": []},
            "commands": [],
            "availability": "permanently_disabled",
        }
    command_report = _commands_for_route(registry, route)
    gap_rows = _rows_for_route(registry, route, evidence_report)
    review_counts = _reviewed_route_counts(evidence_report, route)
    verdict_counts = evidence_report["summary"]["by_route_verdict"].get(route, {})
    closed_without_proof = _closed_without_proof_rows(registry, route, evidence_report)
    closed_without_proof_verdicts: dict[str, int] = {}
    for item in closed_without_proof:
        for verdict in item["verdicts"]:
            closed_without_proof_verdicts[verdict] = closed_without_proof_verdicts.get(verdict, 0) + 1
    return {
        "route": route,
        "label": ROUTE_LABELS[route],
        "source_status": source,
        "verdict_counts": verdict_counts,
        "reviewed_settings": review_counts["reviewed_settings"],
        "missing_review_settings": review_counts["missing_review_settings"],
        "gap_setting_count": len(gap_rows),
        "gap_setting_ids": [row["setting_id"] for row in sorted(gap_rows, key=lambda item: item["setting_id"])],
        "gap_setting_sample": _sample_rows(gap_rows, sample_limit),
        "closed_without_proof_count": len(closed_without_proof),
        "closed_without_proof_setting_ids": sorted(item["setting_id"] for item in closed_without_proof),
        "closed_without_proof_verdicts": dict(sorted(closed_without_proof_verdicts.items())),
        "probe_summary": command_report["summary"],
        "commands": command_report["commands"],
    }


def _blocking_notes(sections: list[dict[str, Any]]) -> list[str]:
    notes = []
    for section in sections:
        route = section["route"]
        label = section["label"]
        source = section["source_status"]
        if route in settings_registry.DISABLED_ROUTES:
            notes.append(f"{label}: permanently disabled; historical evidence is unavailable and unverified.")
            continue
        if source and source["status"] != "loaded":
            if route == "camera_remote_command":
                notes.append(
                    f"{label}: missing official package review artifact; obtain the package locally, "
                    "then run the package-review command."
                )
            else:
                notes.append(f"{label}: missing latest review artifact; run the route probe and review command.")
            continue
        if section["probe_summary"]["missing_gap_rows"]:
            notes.append(f"{label}: has registry gap rows without a generated proof command.")
            continue
        if section["gap_setting_count"]:
            notes.append(
                f"{label}: {section['gap_setting_count']} row(s) still need live proof, "
                "behavior confirmation, wrapping, or physical/admin evidence."
            )
    return notes


def build_report(
    *,
    registry: dict[str, Any] | None = None,
    sample_limit: int = 8,
) -> dict[str, Any]:
    registry = registry or settings_registry.load_registry()
    evidence_report = settings_evidence.build_evidence_report(registry=registry)
    probe_report = settings_probe_plan.build_probe_report(registry)
    precondition_report = settings_preconditions.build_precondition_report(registry, gated_only=True)
    route_sections = [
        _build_route_section(registry, evidence_report, route, sample_limit=sample_limit)
        for route in ROUTE_ORDER
    ]
    registry_status_counts = settings_registry.registry_summary(registry)["by_status"]

    missing_probe_rows = probe_report["summary"]["missing_gap_rows"]
    open_gap_ids = {sid for section in route_sections for sid in section["gap_setting_ids"]}
    proof_verdict_ids = {
        item["setting_id"]
        for item in evidence_report["settings"]
        if any(str(review.get("verdict")) in BEHAVIOR_PROOF_VERDICTS for review in item["reviews"])
    }
    row_status_by_id = {row["setting_id"]: row["status"] for row in registry["settings"]}
    weak_closed_ids = {
        sid for section in route_sections for sid in section["closed_without_proof_setting_ids"]
    }
    unproven_closed_setting_ids = sorted(
        sid
        for sid in weak_closed_ids
        if sid not in open_gap_ids
        and sid not in proof_verdict_ids
        and row_status_by_id.get(sid) not in ("proven_set", "unsupported_on_a7ii")
    )
    closed_without_proof_notes = [
        (
            f"{section['label']}: {section['closed_without_proof_count']} gap row(s) closed without "
            f"behavior proof ({', '.join(f'{name}={count}' for name, count in section['closed_without_proof_verdicts'].items())})."
        )
        for section in route_sections
        if section["closed_without_proof_count"]
    ]
    route_adjudication_complete = (
        not evidence_report["summary"]["missing_review_sources"]
        and all(
            section["gap_setting_count"] == 0
            and section["missing_review_settings"] == 0
            for section in route_sections
        )
    )
    full_behavior_control_complete = (
        route_adjudication_complete
        and not unproven_closed_setting_ids
        and registry_status_counts["needs_probe"] == 0
    )
    return {
        "summary": {
            "total_settings": len(registry["settings"]),
            "inventory_complete": True,
            "route_adjudication_complete": route_adjudication_complete,
            "full_behavior_control_complete": full_behavior_control_complete,
            "registry_status_counts": registry_status_counts,
            "proven_set_settings": registry_status_counts["proven_set"],
            "proven_read_settings": registry_status_counts["proven_read"],
            "needs_probe_settings": registry_status_counts["needs_probe"],
            "evidence_settings": evidence_report["summary"]["settings"],
            "settings_with_reviews": evidence_report["summary"]["settings_with_reviews"],
            "settings_missing_review_routes": evidence_report["summary"]["settings_missing_review_routes"],
            "loaded_review_sources": evidence_report["summary"]["loaded_review_sources"],
            "missing_review_sources": evidence_report["summary"]["missing_review_sources"],
            "probe_commands": probe_report["summary"]["probe_commands"],
            "covered_gap_rows": probe_report["summary"]["covered_gap_rows"],
            "selected_gap_rows": probe_report["summary"]["selected_gap_rows"],
            "missing_probe_command_rows": missing_probe_rows,
            "gated_settings": precondition_report["summary"]["settings"],
            "greyed_settings": precondition_report["summary"]["greyed_settings"],
            "physical_only_settings": precondition_report["summary"]["physical_only_settings"],
            "route_disruptive_settings": precondition_report["summary"]["route_disruptive_settings"],
            "by_route_verdict": evidence_report["summary"]["by_route_verdict"],
            "closed_without_proof_rows": sum(
                section["closed_without_proof_count"] for section in route_sections
            ),
            "unproven_closed_setting_count": len(unproven_closed_setting_ids),
            "unproven_closed_setting_ids": unproven_closed_setting_ids,
        },
        "blocking_notes": _blocking_notes(route_sections),
        "closed_without_proof_notes": closed_without_proof_notes,
        "sources": evidence_report["sources"],
        "routes": route_sections,
        "precondition_summary": precondition_report["summary"],
    }
