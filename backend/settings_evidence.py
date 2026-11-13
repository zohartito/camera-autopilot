"""Aggregate latest route-review artifacts into setting-level evidence.

The registry says what we intend and what route owns a row. The review artifacts say what proof we
actually have today. This module keeps those separate but queryable from the API.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import settings_registry

REPO_ROOT = Path(__file__).resolve().parent.parent
# Default API state must be reproducible from a fresh clone.  Live-review
# artifacts are intentionally ignored/private, so they are never consulted
# unless an operator supplies an explicit evidence root for a local review.
ARTIFACT_ROOT = Path(os.environ.get("CAMERA_AUTOPILOT_EVIDENCE_ROOT", REPO_ROOT / "data" / "evidence_reviews"))
CANONICAL_REVIEW_NAME = "final-review-summary.json"
DISABLED_REVIEW_ROUTES = frozenset({"openmemories"})


@dataclass(frozen=True)
class ReviewSource:
    route: str
    label: str
    directory: Path
    patterns: tuple[str, ...]


REVIEW_SOURCES = (
    ReviewSource(
        route="smart_remote_wifi",
        label="Smart Remote API surface",
        directory=ARTIFACT_ROOT / "r1_api_surface_review",
        patterns=("*smart-remote-review.json",),
    ),
    ReviewSource(
        route="smart_remote_phone",
        label="Smart Remote phone preferences",
        directory=ARTIFACT_ROOT / "r1_phone_settings_api_surface_review",
        patterns=("*smart-remote-review.json", "*phone-preference-review.json"),
    ),
    ReviewSource(
        route="usb_gphoto",
        label="USB/gphoto artifacts",
        directory=ARTIFACT_ROOT / "r2_usb_gphoto_review",
        patterns=("*usb-gphoto-review.json",),
    ),
    ReviewSource(
        route="raw_ptp",
        label="raw PTP focus artifacts",
        directory=ARTIFACT_ROOT / "r2_raw_ptp_review",
        patterns=("*raw-ptp-review.json",),
    ),
    ReviewSource(
        route="camera_remote_command",
        label="Sony Camera Remote Command package",
        directory=ARTIFACT_ROOT / "r4_camera_remote_command",
        patterns=("*command-package-review.json",),
    ),
    ReviewSource(
        route="openmemories",
        label="OpenMemories Route 3 artifacts",
        directory=ARTIFACT_ROOT / "r3_openmemories_review",
        patterns=("*openmemories-review.json",),
    ),
    ReviewSource(
        route="guided_physical",
        label="Guided physical/admin gate artifacts",
        directory=ARTIFACT_ROOT / "manual_gates_review",
        patterns=("*physical-gate-review.json",),
    ),
)


def _repo_path(path: Path) -> str:
    try:
        return str(path.resolve().relative_to(REPO_ROOT))
    except ValueError:
        return str(path)


def latest_review_file(source: ReviewSource) -> Path | None:
    if source.route in DISABLED_REVIEW_ROUTES:
        return None
    canonical = source.directory / CANONICAL_REVIEW_NAME
    if canonical.is_file():
        return canonical

    candidates: list[Path] = []
    if source.directory.exists():
        for pattern in source.patterns:
            candidates.extend(path for path in source.directory.glob(pattern) if path.is_file())
    if not candidates:
        return None
    return max(candidates, key=lambda path: (path.stat().st_mtime, path.name))


def load_json(path: Path) -> dict[str, Any] | None:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return payload if isinstance(payload, dict) else None


def _review_source_status(source: ReviewSource, path: Path | None, report: dict[str, Any] | None) -> dict[str, Any]:
    if source.route in DISABLED_REVIEW_ROUTES:
        return {
            "route": source.route,
            "label": source.label,
            "artifact": None,
            "status": "route_unavailable",
            "summary": None,
        }
    if path is None:
        return {
            "route": source.route,
            "label": source.label,
            "artifact": None,
            "status": "missing_review_artifact",
            "summary": None,
        }
    if report is None:
        return {
            "route": source.route,
            "label": source.label,
            "artifact": _repo_path(path),
            "status": "unreadable_review_artifact",
            "summary": None,
        }
    return {
        "route": source.route,
        "label": source.label,
        "artifact": _repo_path(path),
        "status": "loaded",
        "summary": report.get("registry_mapping_summary"),
        "artifact_summary": report.get("artifact_summary"),
        "generated_at": report.get("generated_at"),
    }


def _compact_mapping_row(source: ReviewSource, artifact: Path, row: dict[str, Any]) -> dict[str, Any]:
    compact: dict[str, Any] = {
        "route": source.route,
        "review_label": source.label,
        "review_artifact": _repo_path(artifact),
        "verdict": row.get("verdict"),
        "evidence_count": row.get("evidence_count"),
        "registry_status": row.get("registry_status"),
        "route_status": row.get("route_status"),
        "next_action": row.get("next_action"),
    }
    for key in (
        "matched_methods",
        "matched_setters",
        "matched_getters",
        "node",
        "propcode",
        "matched_terms",
        "package_mentions_ilce_7m2",
        "observed_value",
        "phone_state",
        "control_surface",
        "api_exposure",
        "observed_behavior",
        "target_setup",
        "human_verdict",
        "behavior_type",
        "gate_class",
        "observed_state",
        "admin_guarded",
        "control_surface",
        "api_exposure",
    ):
        if key in row:
            compact[key] = row[key]
    if "manual_proof_artifacts" in row:
        compact["manual_proof_artifact_count"] = len(row.get("manual_proof_artifacts") or [])
    if "routes" in row:
        compact["routes"] = row["routes"]
    if "generated_endpoints" in row:
        compact["generated_endpoints"] = row["generated_endpoints"]
    if "endpoint_proof_count" in row:
        compact["endpoint_proof_count"] = row["endpoint_proof_count"]
    elif "endpoint_proofs" in row:
        compact["endpoint_proof_count"] = len(row.get("endpoint_proofs") or [])
    if "behavior_evidence" in row:
        behavior_items = row.get("behavior_evidence") or []
        compact["behavior_evidence_count"] = len(behavior_items)
        visual_items = [
            item
            for item in behavior_items
            if item.get("human_verdict") or item.get("observed_behavior")
        ]
        if visual_items:
            latest = visual_items[-1]
            compact["human_verdict"] = latest.get("human_verdict")
            compact["observed_behavior"] = latest.get("observed_behavior")
            compact["target_setup"] = latest.get("target_setup")
            compact["behavior_type"] = latest.get("behavior_type")
            compact["method"] = latest.get("method")
            compact["requested_value"] = latest.get("requested_value")
            compact["observed_before"] = latest.get("observed_before")
            compact["observed_after"] = latest.get("observed_after")
            compact["value_changed"] = latest.get("value_changed")
            compact["after_matches_request"] = latest.get("after_matches_request")
    if "rollback_port_evidence" in row:
        ports = row["rollback_port_evidence"] or {}
        compact["rollback_open_ports"] = len(ports.get("open_ports") or [])
        compact["rollback_closed_evidence"] = bool(ports.get("has_rollback_closed_evidence"))
    return compact


def load_review_index(
    *,
    sources: tuple[ReviewSource, ...] = REVIEW_SOURCES,
) -> dict[str, Any]:
    by_setting: dict[str, list[dict[str, Any]]] = {}
    source_statuses = []
    by_route_verdict: dict[str, dict[str, int]] = {}

    for source in sources:
        path = latest_review_file(source)
        report = load_json(path) if path is not None else None
        source_statuses.append(_review_source_status(source, path, report))
        if path is None or report is None:
            continue
        for row in report.get("registry_mapping", []):
            if not isinstance(row, dict) or not isinstance(row.get("setting_id"), str):
                continue
            compact = _compact_mapping_row(source, path, row)
            by_setting.setdefault(row["setting_id"], []).append(compact)
            verdict = str(compact.get("verdict") or "unknown")
            route_counts = by_route_verdict.setdefault(source.route, {})
            route_counts[verdict] = route_counts.get(verdict, 0) + 1

    return {
        "sources": source_statuses,
        "by_setting": by_setting,
        "by_route_verdict": {
            route: dict(sorted(counts.items()))
            for route, counts in sorted(by_route_verdict.items())
        },
    }


def evidence_for_setting(
    setting_id: str,
    *,
    registry: dict[str, Any] | None = None,
    review_index: dict[str, Any] | None = None,
) -> dict[str, Any]:
    registry = registry or settings_registry.load_registry()
    row = settings_registry.get_setting(setting_id, registry)
    review_index = review_index or load_review_index()
    route_names = sorted(row.get("routes", {}))
    manual_review_applies = row["status"] in {"manual_physical_gate", "state_gated", "dangerous_admin"}
    reviews = [
        review
        for review in review_index["by_setting"].get(setting_id, [])
        if review.get("route") in route_names
        or (review.get("route") == "guided_physical" and manual_review_applies)
    ]
    reviewed_routes = {str(review.get("route")) for review in reviews}
    missing_review_routes = [
        route
        for route in route_names
        if (
            route in {source.route for source in REVIEW_SOURCES}
            and route not in DISABLED_REVIEW_ROUTES
            and route not in reviewed_routes
        )
    ]
    return {
        "setting_id": setting_id,
        "label": row["label"],
        "status": row["status"],
        "route_names": route_names,
        "reviews": reviews,
        "reviewed_routes": sorted(reviewed_routes),
        "missing_review_routes": missing_review_routes,
        "proof_artifacts": row["proof_artifacts"],
        "next_action": row["next_action"],
    }


def build_evidence_report(
    *,
    setting_id: str | None = None,
    menu_group: str | None = None,
    route: str | None = None,
    registry: dict[str, Any] | None = None,
) -> dict[str, Any]:
    registry = registry or settings_registry.load_registry()
    review_index = load_review_index()
    if setting_id:
        rows = [settings_registry.get_setting(setting_id, registry)]
    else:
        rows = settings_registry.list_settings(menu_group=menu_group, registry=registry)
    if route:
        rows = [row for row in rows if route in row.get("routes", {})]

    settings = [
        evidence_for_setting(row["setting_id"], registry=registry, review_index=review_index)
        for row in rows
    ]
    settings_with_reviews = sum(1 for item in settings if item["reviews"])
    settings_missing_review_routes = sum(1 for item in settings if item["missing_review_routes"])
    return {
        "summary": {
            "settings": len(settings),
            "settings_with_reviews": settings_with_reviews,
            "settings_missing_review_routes": settings_missing_review_routes,
            "loaded_review_sources": sum(1 for item in review_index["sources"] if item["status"] == "loaded"),
            "missing_review_sources": [
                item["route"]
                for item in review_index["sources"]
                if item["status"] not in {"loaded", "route_unavailable"}
            ],
            "by_route_verdict": review_index["by_route_verdict"],
        },
        "sources": review_index["sources"],
        "settings": settings,
    }
