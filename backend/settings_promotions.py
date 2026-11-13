"""Plan safe registry promotions from reviewed route evidence.

Review artifacts are evidence, not registry truth. This module turns the latest route reviews into
explicit promotion candidates so live hardware proof can be converted into registry updates without
over-promoting surface-only or admin-gated rows.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import settings_evidence
import settings_probe_plan
import settings_registry

PROVEN_SET_VERDICTS = {
    ("smart_remote_wifi", "behavior_proven"),
    ("usb_gphoto", "proven_set_read_revert"),
    ("raw_ptp", "behavior_proven"),
}

READ_ONLY_VERDICTS = {
    ("usb_gphoto", "present_read_only_or_unknown"),
    ("usb_gphoto", "present_read_only_state_gated"),
    ("smart_remote_wifi", "route_getter_seen_in_artifact"),
    ("smart_remote_wifi", "getter_candidate_seen_in_artifact"),
    ("smart_remote_phone", "route_getter_seen_in_artifact"),
    ("smart_remote_phone", "getter_candidate_seen_in_artifact"),
}

SURFACE_ONLY_VERDICTS = {
    ("smart_remote_wifi", "route_setter_seen_in_artifact"),
    ("smart_remote_wifi", "setter_candidate_seen_in_artifact"),
    ("smart_remote_wifi", "service_method_candidate_seen_in_artifact"),
    ("smart_remote_phone", "route_setter_seen_in_artifact"),
    ("smart_remote_phone", "setter_candidate_seen_in_artifact"),
    ("smart_remote_phone", "service_method_candidate_seen_in_artifact"),
    ("camera_remote_command", "candidate_package_and_model_evidence"),
    ("camera_remote_command", "candidate_package_evidence_model_unscoped"),
}

PROTOCOL_ONLY_VERDICTS = {
    ("raw_ptp", "protocol_accepted_needs_behavior"),
    ("raw_ptp", "behavior_metrics_need_visual_review"),
}

BEHAVIOR_REJECTED_VERDICTS = {
    ("raw_ptp", "behavior_rejected"),
    ("smart_remote_wifi", "setter_attempt_failed"),
    ("usb_gphoto", "failed_set_read_revert"),
}

OBSERVATION_ONLY_VERDICTS = {
    ("smart_remote_phone", "phone_app_manual_preference_observed"),
}

ACTION_ONLY_VERDICTS = {
    ("usb_gphoto", "movie_record_action_proven"),
}

NO_PROMOTION_VERDICTS = {
    "needs_live_probe_artifact",
    "needs_live_usb_artifact",
    "needs_live_raw_ptp_artifact",
    "needs_phone_app_observation",
    "needs_route3_preflight",
    "proof_app_scaffolded_needs_tooling",
    "proof_app_scaffolded_ready_for_build",
    "present_writable_needs_set_read_revert",
    "present_needs_explicit_target",
    "node_not_seen_in_artifacts",
    "absent_in_recorded_states",
    "needs_manual_review",
    "needs_tooling",
    "needs_scaffold_or_endpoint_proof",
    "needs_rollback_port_scan",
    "admin_port_open_needs_rollback",
    "dangerous_admin_needs_explicit_approval",
    "behavior_observed_not_proven",
    "proof_app_setting_not_exposed",
    "unsupported_on_a7ii",
}


def _guarded(row: dict[str, Any]) -> bool:
    return (
        row["setting_id"] in settings_registry.DESTRUCTIVE_SETTING_IDS
        or row["status"] == "dangerous_admin"
    )


def _route_commands(
    row: dict[str, Any],
    route: str,
    registry: dict[str, Any],
) -> list[dict[str, Any]]:
    try:
        report = settings_probe_plan.build_probe_report(
            registry,
            setting_id=row["setting_id"],
            route=route,
        )
    except settings_registry.SettingNotFoundError:
        return []
    return report["commands"]


def _target_setting_status(row: dict[str, Any], route_status: str) -> str:
    if row["status"] == "unsupported_on_a7ii":
        return "unsupported_on_a7ii"
    if route_status == "proven_set":
        return "proven_set"
    if route_status == "proven_read" and row["status"] not in {"proven_set", "dangerous_admin"}:
        return "proven_read"
    return row["status"]


def _first_review_setter(row: dict[str, Any], review: dict[str, Any]) -> str | None:
    methods = [review.get("method")]
    methods.extend(evidence.get("method") for evidence in review.get("behavior_evidence") or [])
    methods.extend(review.get("matched_setters") or [])
    methods.extend(review.get("candidate_methods") or [])
    for method in methods:
        approved = settings_probe_plan.approved_smart_remote_method(row["setting_id"], method)
        if approved:
            return approved
    return None


def _route_metadata_patch(row: dict[str, Any], route: str, review: dict[str, Any]) -> dict[str, Any]:
    route_info = row.get("routes", {}).get(route, {})
    if route == "smart_remote_wifi" and not (
        route_info.get("method") or route_info.get("method_hint")
    ):
        method = _first_review_setter(row, review)
        if method:
            return {"method": method}
    return {}


def _no_change_action(
    row: dict[str, Any],
    route: str,
    recommended_route_status: str | None,
    route_metadata_patch: dict[str, Any] | None = None,
) -> bool:
    route_status = row.get("routes", {}).get(route, {}).get("status")
    if recommended_route_status is None:
        return False
    if route_metadata_patch:
        return False
    target_setting_status = _target_setting_status(row, recommended_route_status)
    return route_status == recommended_route_status and row["status"] == target_setting_status


def classify_review(
    row: dict[str, Any],
    review: dict[str, Any],
    *,
    registry: dict[str, Any],
) -> dict[str, Any]:
    route = str(review.get("route") or "")
    verdict = str(review.get("verdict") or "unknown")
    key = (route, verdict)
    route_info = row.get("routes", {}).get(route, {})
    route_metadata_patch = _route_metadata_patch(row, route, review)
    guarded = _guarded(row)

    recommended_route_status: str | None = None
    reason: str
    action: str
    can_apply = False

    # Disabled Route 3 and imported review JSON are never promotion authority.
    # A future owner-approved attestation flow must update this policy before
    # either can select or rewrite a durable control claim.
    if route == "openmemories":
        action = "route_permanently_disabled"
        reason = "OpenMemories Route 3 is permanently disabled and cannot promote registry state."
    # Raw USB proof files are intentionally untrusted imported evidence.  A
    # local JSON artifact cannot establish the authority needed to rewrite the
    # durable registry; an operator must attest it in a new, separately
    # approved hardware workflow before any promotion is possible.
    elif route == "usb_gphoto":
        action = "usb_evidence_requires_operator_attestation"
        reason = "USB proof artifacts are untrusted and cannot auto-promote the settings registry."
    elif row["status"] == "unsupported_on_a7ii" or verdict == "unsupported_on_a7ii":
        action = "keep_unsupported"
        reason = "Registry or review marks this row unsupported on the A7 II."
    elif guarded:
        action = "guarded_no_auto_promotion"
        reason = "Admin/destructive rows require explicit approval even if a route appears available."
    elif key in PROVEN_SET_VERDICTS:
        recommended_route_status = "proven_set"
        if _no_change_action(row, route, recommended_route_status, route_metadata_patch):
            action = "already_promoted"
            reason = "The registry already reflects this behavior-proven setter."
        else:
            action = "promote_to_proven_set"
            reason = "Imported artifacts are not an owner attestation and cannot auto-promote the registry."
            action = "evidence_requires_operator_attestation"
    elif key == ("usb_gphoto", "present_read_only_state_gated") and route_info.get("status") == "state_gated":
        recommended_route_status = "state_gated"
        action = "already_promoted"
        reason = "The registry already preserves this USB route as readable but state-gated."
    elif key in READ_ONLY_VERDICTS:
        recommended_route_status = "proven_read"
        if row["status"] == "proven_set":
            action = "keep_setter_preferred"
            reason = "Read evidence exists, but the setting already has a stronger proven_set status."
        elif _no_change_action(row, route, recommended_route_status):
            action = "already_promoted"
            reason = "The registry already reflects this read evidence."
        else:
            action = "promote_to_proven_read"
            reason = "Imported artifacts are not an owner attestation and cannot auto-promote the registry."
            action = "evidence_requires_operator_attestation"
    elif key in PROTOCOL_ONLY_VERDICTS:
        recommended_route_status = "protocol_ok_needs_behavior"
        action = "needs_behavior_proof"
        reason = "Protocol acceptance is useful but not enough; visual/behavior proof is still required."
    elif key in BEHAVIOR_REJECTED_VERDICTS:
        action = "route_behavior_rejected"
        reason = (
            "Saved artifacts show this route failed behavior or setter validation for this setting; "
            "keep the registry on a different active route or a physical fallback."
        )
    elif key in OBSERVATION_ONLY_VERDICTS:
        action = "observation_recorded_no_api_promotion"
        reason = (
            "Phone-app preference evidence proves the visible app-side state, but it does not prove "
            "a camera API setter or camera-side behavior."
        )
    elif key in ACTION_ONLY_VERDICTS:
        action = "action_recorded_no_setting_promotion"
        reason = (
            "USB evidence proves this camera action, but it does not prove software control of the "
            "corresponding physical or persistent menu setting."
        )
    elif key in SURFACE_ONLY_VERDICTS:
        action = "needs_behavior_proof"
        reason = "Surface/package/app evidence found a candidate, but behavior proof is still required before promotion."
    elif verdict in NO_PROMOTION_VERDICTS:
        action = "needs_more_evidence"
        reason = "The latest review does not contain enough proof for registry promotion."
    else:
        action = "manual_review_required"
        reason = "Unknown review verdict; keep out of automated promotion until classified."

    target_setting_status = (
        _target_setting_status(row, recommended_route_status)
        if recommended_route_status is not None
        else row["status"]
    )
    registry_patch = None
    if can_apply:
        registry_patch = {
            "setting_id": row["setting_id"],
            "route": route,
            "set_status": target_setting_status,
            "set_route_status": recommended_route_status,
            "append_proof_artifact": review.get("review_artifact"),
        }
        if route_metadata_patch:
            registry_patch["set_route_metadata"] = route_metadata_patch

    return {
        "setting_id": row["setting_id"],
        "label": row["label"],
        "route": route,
        "verdict": verdict,
        "review_artifact": review.get("review_artifact"),
        "current_status": row["status"],
        "current_route_status": route_info.get("status"),
        "recommended_status": target_setting_status,
        "recommended_route_status": recommended_route_status,
        "action": action,
        "can_apply": can_apply,
        "guarded": guarded,
        "reason": reason,
        "registry_patch": registry_patch,
        "next_commands": _route_commands(row, route, registry),
        "review": review,
    }


def build_promotion_plan(
    *,
    registry: dict[str, Any] | None = None,
    review_index: dict[str, Any] | None = None,
    route: str | None = None,
    setting_id: str | None = None,
    action: str | None = None,
    applicable_only: bool = False,
    limit: int | None = None,
) -> dict[str, Any]:
    registry = registry or settings_registry.load_registry()
    review_index = review_index or settings_evidence.load_review_index()
    rows = (
        [settings_registry.get_setting(setting_id, registry)]
        if setting_id
        else list(registry["settings"])
    )
    candidates = []
    for row in rows:
        for review in review_index["by_setting"].get(row["setting_id"], []):
            if review.get("route") not in row.get("routes", {}):
                continue
            if route is not None and review.get("route") != route:
                continue
            candidates.append(classify_review(row, review, registry=registry))
    unfiltered_count = len(candidates)
    if action is not None:
        candidates = [item for item in candidates if item["action"] == action]
    if applicable_only:
        candidates = [item for item in candidates if item["can_apply"]]
    if limit is not None and limit >= 0:
        candidates = candidates[:limit]

    action_counts: dict[str, int] = {}
    route_counts: dict[str, int] = {}
    for item in candidates:
        action_counts[item["action"]] = action_counts.get(item["action"], 0) + 1
        route_counts[item["route"]] = route_counts.get(item["route"], 0) + 1

    return {
        "summary": {
            "reviewed_candidates": len(candidates),
            "unfiltered_reviewed_candidates": unfiltered_count,
            "applicable_promotions": sum(1 for item in candidates if item["can_apply"]),
            "guarded_candidates": sum(1 for item in candidates if item["guarded"]),
            "actions": dict(sorted(action_counts.items())),
            "routes": dict(sorted(route_counts.items())),
            "missing_review_sources": [
                item["route"]
                for item in review_index["sources"]
                if item["status"] not in {"loaded", "route_unavailable"}
            ],
        },
        "sources": review_index["sources"],
        "promotions": candidates,
    }


def _rows_by_id_mutable(registry: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {row["setting_id"]: row for row in registry["settings"]}


def apply_promotion_plan(
    *,
    registry_path: str | Path | None = None,
    out_path: str | Path | None = None,
    review_index: dict[str, Any] | None = None,
    route: str | None = None,
    setting_id: str | None = None,
    action: str | None = None,
    dry_run: bool = True,
) -> dict[str, Any]:
    """Apply behavior-proven promotion candidates to a registry file.

    The default is a dry run. Only candidates where `can_apply` is true are selected, guarded rows
    are excluded by the planner, and existing proof artifacts are not duplicated.
    """
    source_path = Path(registry_path) if registry_path is not None else settings_registry.DEFAULT_REGISTRY_PATH
    target_path = Path(out_path) if out_path is not None else source_path
    registry = settings_registry.load_registry(source_path)
    plan = build_promotion_plan(
        registry=registry,
        review_index=review_index,
        route=route,
        setting_id=setting_id,
        action=action,
        applicable_only=True,
    )
    rows = _rows_by_id_mutable(registry)
    changes = []

    for item in plan["promotions"]:
        patch = item.get("registry_patch") or {}
        if patch.get("route") == "openmemories" or patch.get("set_route_status") == "proven_set" and not item.get("can_apply"):
            continue
        row = rows[patch["setting_id"]]
        route_name = patch["route"]
        route_info = row["routes"][route_name]
        artifact = patch.get("append_proof_artifact")
        before = {
            "status": row["status"],
            "route_status": route_info.get("status"),
            "route_metadata": dict(route_info),
            "proof_artifacts": list(row["proof_artifacts"]),
        }
        after_artifacts = list(row["proof_artifacts"])
        if artifact and artifact not in after_artifacts:
            after_artifacts.append(artifact)
        after = {
            "status": patch["set_status"],
            "route_status": patch["set_route_status"],
            "route_metadata": {
                **route_info,
                "status": patch["set_route_status"],
                **patch.get("set_route_metadata", {}),
            },
            "proof_artifacts": after_artifacts,
        }
        if not dry_run:
            row["status"] = after["status"]
            route_info["status"] = after["route_status"]
            route_info.update(patch.get("set_route_metadata", {}))
            row["proof_artifacts"] = after_artifacts
        changes.append(
            {
                "setting_id": row["setting_id"],
                "label": row["label"],
                "route": route_name,
                "action": item["action"],
                "verdict": item["verdict"],
                "review_artifact": item.get("review_artifact"),
                "before": before,
                "after": after,
            }
        )

    written = False
    if not dry_run and changes:
        settings_registry.validate_registry(registry)
        target_path.parent.mkdir(parents=True, exist_ok=True)
        target_path.write_text(json.dumps(registry, indent=2, sort_keys=False) + "\n", encoding="utf-8")
        written = True

    return {
        "summary": {
            "dry_run": dry_run,
            "registry_path": str(source_path),
            "target_path": str(target_path),
            "selected_promotions": len(changes),
            "written": written,
            "missing_review_sources": plan["summary"]["missing_review_sources"],
        },
        "changes": changes,
        "promotion_plan_summary": plan["summary"],
    }
