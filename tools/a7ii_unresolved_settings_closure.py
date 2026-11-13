#!/usr/bin/env python3
"""Emit the remaining A7 II live evidence tests and external blockers.

This report is intentionally camera-free. It reads the registry, completion audit, and
deterministically selected route-review artifacts, then emits only:

1. live tests that require a specific camera state and have not already been exhausted; and
2. blockers that require an external input before repository work can continue.

Candidate sightings, rejected protocols, repeated failed tests, implementation decisions, and
documented deferrals are counted as data-quality caveats, not silently re-queued as live work.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Mapping

REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_REGISTRY = REPO_ROOT / "data" / "a7ii_settings_registry.yaml"
DEFAULT_COMPLETION_AUDIT = REPO_ROOT / "artifacts" / "falsification" / "a7ii_completion_audit.json"
DEFAULT_LEGACY_CLOSURE = REPO_ROOT / "artifacts" / "falsification" / "a7ii_evidence_closure.json"
DEFAULT_ARTIFACT_ROOT = REPO_ROOT / "artifacts" / "falsification"
CANONICAL_REVIEW_NAME = "final-review-summary.json"
CANONICAL_LOCATOR_NAME = "final-package-locator-summary.json"

NON_GAP_ROUTE_STATUSES = {"proven_set", "unsupported_on_a7ii"}
NON_GAP_ROW_STATUSES = {"proven_set", "unsupported_on_a7ii"}

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

CLOSING_VERDICTS_BY_ROUTE = {
    "smart_remote_wifi": {
        "behavior_observed_not_proven",
        "behavior_proven",
        "getter_candidate_seen_in_artifact",
        "service_method_candidate_seen_in_artifact",
        "setter_attempt_failed",
        "candidate_admin_needs_approval",
    },
    "smart_remote_phone": {"phone_app_manual_preference_observed"},
    "usb_gphoto": {
        "failed_set_read_revert",
        "proven_set_read_revert",
        "movie_record_action_proven",
        "present_read_only_state_gated",
    },
    "raw_ptp": {"behavior_rejected"},
    "openmemories": {
        "dangerous_admin_needs_explicit_approval",
        "proof_app_setting_write_proven",
        "proof_app_setting_endpoint_proven",
        "proof_app_setting_not_exposed",
        "rollback_ports_closed",
        "unsupported_on_a7ii",
    },
    "guided_physical": {
        "dangerous_admin_observed_no_action",
        "manual_gate_recorded_not_software_controlled",
    },
}


@dataclass(frozen=True)
class ReviewSourceSpec:
    route: str
    label: str
    directory: str
    patterns: tuple[str, ...]


REVIEW_SOURCES = (
    ReviewSourceSpec(
        "smart_remote_wifi",
        "Smart Remote API surface",
        "r1_api_surface_review",
        ("*smart-remote-review.json",),
    ),
    ReviewSourceSpec(
        "smart_remote_phone",
        "Smart Remote phone preferences",
        "r1_phone_settings_api_surface_review",
        ("*smart-remote-review.json", "*phone-preference-review.json"),
    ),
    ReviewSourceSpec(
        "usb_gphoto",
        "USB/gphoto artifacts",
        "r2_usb_gphoto_review",
        ("*usb-gphoto-review.json",),
    ),
    ReviewSourceSpec(
        "raw_ptp",
        "Raw PTP focus artifacts",
        "r2_raw_ptp_review",
        ("*raw-ptp-review.json",),
    ),
    ReviewSourceSpec(
        "camera_remote_command",
        "Sony Camera Remote Command package",
        "r4_camera_remote_command",
        ("*command-package-review.json",),
    ),
    ReviewSourceSpec(
        "openmemories",
        "OpenMemories Route 3 artifacts",
        "r3_openmemories_review",
        ("*openmemories-review.json",),
    ),
    ReviewSourceSpec(
        "guided_physical",
        "Guided physical/admin gate artifacts",
        "manual_gates_review",
        ("*physical-gate-review.json",),
    ),
)

LIVE_RETEST_POLICIES: dict[str, dict[str, Any]] = {
    "camera.flash_mode": {
        "enabled": True,
        "route": "usb_gphoto",
        "state_id": "flash_attached",
        "title": "USB flash-mode write with a compatible flash state",
        "why_live": (
            "The flashmode node was readable but refused every write without a compatible "
            "flash or trigger attached."
        ),
        "operator_steps": [
            "Put the mode dial in P, A, S, or M and leave the camera in still-photo mode.",
            "Attach and power on a compatible flash or flash trigger.",
            "Set USB Connection to PC Remote and connect USB to the Mac.",
        ],
        "command": (
            "python3 tools/a7ii_usb_set_read_revert.py "
            "--setting camera.flash_mode --execute "
            "--model \"<camera model>\" --port \"<camera port>\" --serial \"<camera serial>\" "
            "--out-dir artifacts/falsification/r2_usb_set_read_revert/closure-flash-attached"
        ),
        "success_criteria": [
            "The tool reads the original flashmode value.",
            "The alternate value is visible on readback.",
            "The original value is restored and verified.",
        ],
        "risk": "safe_reversible_setting_write",
    },
    "camera.picture_effect": {
        "enabled": False,
        "route": "usb_gphoto",
        "state_id": "jpeg_rendering_mode",
        "title": "USB picture-effect JPEG precondition",
        "exclusion_reason": (
            "Already exhausted: JPEG Fine was explicitly tested and pictureeffect still returned "
            "GPhoto2Error [-2] Bad parameters. The next work is an alternate route decision, not "
            "another identical live USB test."
        ),
    },
    "camera.zoom": {
        "enabled": True,
        "route": "usb_gphoto",
        "state_id": "power_zoom_or_digital_zoom",
        "title": "USB zoom write with a controllable zoom state",
        "why_live": (
            "The zoom node was readable but the current non-power-zoom lens did not move or "
            "change the value."
        ),
        "operator_steps": [
            "Attach a compatible power-zoom lens, or enable a camera state that exposes digital or clear-image zoom.",
            "Put the mode dial in P, A, S, or M and leave the camera in still-photo mode.",
            "Set USB Connection to PC Remote and connect USB to the Mac.",
        ],
        "command": (
            "python3 tools/a7ii_usb_set_read_revert.py "
            "--setting camera.zoom --execute "
            "--model \"<camera model>\" --port \"<camera port>\" --serial \"<camera serial>\" "
            "--out-dir artifacts/falsification/r2_usb_set_read_revert/closure-zoom-enabled"
        ),
        "success_criteria": [
            "The tool reads the original zoom value.",
            "A small alternate value produces a changed readback and visible zoom behavior.",
            "The original value is restored and verified.",
        ],
        "risk": "safe_reversible_setting_write",
    },
}


class ClosureDataError(RuntimeError):
    """Raised when an input cannot support a trustworthy closure report."""


def _load_structured(path: Path, *, required: bool) -> dict[str, Any] | None:
    if not path.is_file():
        if required:
            raise ClosureDataError(f"required input does not exist: {path}")
        return None
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        if required:
            raise ClosureDataError(f"could not read {path}: {exc}") from exc
        return None
    try:
        payload = json.loads(text)
    except json.JSONDecodeError:
        try:
            import yaml  # type: ignore

            payload = yaml.safe_load(text)
        except Exception as exc:
            if required:
                raise ClosureDataError(f"could not parse {path} as JSON or YAML: {exc}") from exc
            return None
    if not isinstance(payload, dict):
        if required:
            raise ClosureDataError(f"expected an object in {path}")
        return None
    return payload


def _display_path(path: Path) -> str:
    try:
        return path.resolve().relative_to(REPO_ROOT).as_posix()
    except ValueError:
        return path.resolve().as_posix()


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def deterministic_latest(directory: Path, patterns: Iterable[str]) -> Path | None:
    """Select the lexicographically latest artifact, independent of filesystem mtimes."""
    canonical = directory / CANONICAL_REVIEW_NAME
    if canonical.is_file():
        return canonical
    candidates: dict[str, Path] = {}
    if directory.is_dir():
        for pattern in patterns:
            for path in directory.glob(pattern):
                if path.is_file():
                    candidates[path.resolve().as_posix()] = path
    if not candidates:
        return None
    return candidates[sorted(candidates)[-1]]


def deterministic_best_locator(directory: Path) -> Path | None:
    """Prefer an actionable or complete package scan over a newer partial scan."""
    canonical = directory / CANONICAL_LOCATOR_NAME
    if canonical.is_file():
        return canonical
    candidates = sorted(
        path
        for path in directory.glob("*command-package-locator.json")
        if path.is_file()
    )
    ranked: list[tuple[tuple[int, int, int, str], Path]] = []
    for path in candidates:
        payload = _load_structured(path, required=False)
        if payload is None:
            continue
        summary = payload.get("summary") or {}
        search = payload.get("search") or {}
        candidate_rows = payload.get("candidates") or []
        current_schema = bool(candidate_rows) and all(
            isinstance(row, dict) and "local_project_material" in row
            for row in candidate_rows
        )
        reviewable = int(summary.get("reviewable_candidates") or 0) > 0
        complete = bool(search.get("complete_with_policy_exclusions"))
        ranked.append(
            (
                (
                    int(current_schema),
                    int(reviewable),
                    int(complete),
                    path.name,
                ),
                path,
            )
        )
    return max(ranked)[1] if ranked else None


def _source_manifest_entry(path: Path, role: str) -> dict[str, Any]:
    return {
        "role": role,
        "path": _display_path(path),
        "sha256": _sha256(path),
        "size_bytes": path.stat().st_size,
    }


def _load_review_index(
    artifact_root: Path,
) -> tuple[
    dict[tuple[str, str], list[dict[str, Any]]],
    list[dict[str, Any]],
    list[Path],
]:
    mappings: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
    statuses: list[dict[str, Any]] = []
    selected_paths: list[Path] = []

    for spec in REVIEW_SOURCES:
        path = deterministic_latest(artifact_root / spec.directory, spec.patterns)
        if path is None:
            statuses.append(
                {
                    "route": spec.route,
                    "label": spec.label,
                    "status": "missing_review_artifact",
                    "artifact": None,
                    "mapping_rows": 0,
                }
            )
            continue
        payload = _load_structured(path, required=False)
        if payload is None:
            statuses.append(
                {
                    "route": spec.route,
                    "label": spec.label,
                    "status": "unreadable_review_artifact",
                    "artifact": _display_path(path),
                    "mapping_rows": 0,
                }
            )
            selected_paths.append(path)
            continue

        mapping_rows = 0
        for row in payload.get("registry_mapping", []):
            if not isinstance(row, dict) or not isinstance(row.get("setting_id"), str):
                continue
            mappings[(row["setting_id"], spec.route)].append(row)
            mapping_rows += 1
        statuses.append(
            {
                "route": spec.route,
                "label": spec.label,
                "status": "loaded",
                "artifact": _display_path(path),
                "mapping_rows": mapping_rows,
            }
        )
        selected_paths.append(path)
    return mappings, statuses, selected_paths


def _route_is_gap(row: Mapping[str, Any], route: str) -> bool:
    route_info = row.get("routes", {}).get(route)
    if not isinstance(route_info, dict):
        return False
    if route_info.get("status") in NON_GAP_ROUTE_STATUSES:
        return False
    if route == "usb_gphoto" and not route_info.get("node"):
        return False
    return True


def _verdicts_for(
    review_index: Mapping[tuple[str, str], list[dict[str, Any]]],
    setting_id: str,
    route: str,
) -> set[str]:
    return {
        str(item["verdict"])
        for item in review_index.get((setting_id, route), [])
        if item.get("verdict")
    }


def _dedupe_items(items: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    by_key: dict[tuple[str, str, str], dict[str, Any]] = {}
    for item in items:
        key = (item["setting_id"], item["state_id"], item["route"])
        candidate = dict(item)
        candidate["dedupe_key"] = "|".join(key)
        by_key.setdefault(key, candidate)
    return [by_key[key] for key in sorted(by_key)]


def _analyze_evidence(
    registry: dict[str, Any],
    review_index: Mapping[tuple[str, str], list[dict[str, Any]]],
) -> dict[str, Any]:
    weak_route_rows: list[dict[str, Any]] = []
    open_gap_rows: list[dict[str, Any]] = []
    proof_ids: set[str] = set()
    rows_by_id = {row["setting_id"]: row for row in registry["settings"]}

    for (setting_id, route), mapping_rows in review_index.items():
        row = rows_by_id.get(setting_id)
        if row is None:
            continue
        route_applies = route in row.get("routes", {}) or (
            route == "guided_physical"
            and row["status"] in {"manual_physical_gate", "state_gated", "dangerous_admin"}
        )
        if not route_applies:
            continue
        if any(str(item.get("verdict")) in BEHAVIOR_PROOF_VERDICTS for item in mapping_rows):
            proof_ids.add(setting_id)

    for row in registry["settings"]:
        setting_id = row["setting_id"]
        for route in sorted(row.get("routes", {})):
            if not _route_is_gap(row, route):
                continue
            verdicts = _verdicts_for(review_index, setting_id, route)
            closing = CLOSING_VERDICTS_BY_ROUTE.get(route, set())
            if verdicts & closing:
                if not verdicts & BEHAVIOR_PROOF_VERDICTS:
                    weak_route_rows.append(
                        {
                            "setting_id": setting_id,
                            "route": route,
                            "verdicts": sorted(verdicts),
                        }
                    )
            else:
                open_gap_rows.append({"setting_id": setting_id, "route": route})

    open_gap_ids = {item["setting_id"] for item in open_gap_rows}
    weak_ids = {item["setting_id"] for item in weak_route_rows}
    row_status = {row["setting_id"]: row["status"] for row in registry["settings"]}
    unproven_closed_ids = sorted(
        setting_id
        for setting_id in weak_ids
        if setting_id not in open_gap_ids
        and setting_id not in proof_ids
        and row_status.get(setting_id) not in NON_GAP_ROW_STATUSES
    )
    return {
        "weak_route_rows": weak_route_rows,
        "open_gap_rows": open_gap_rows,
        "proof_ids": sorted(proof_ids),
        "unproven_closed_ids": unproven_closed_ids,
    }


def _build_live_groups(
    registry: dict[str, Any],
    evidence_analysis: dict[str, Any],
    source_by_route: Mapping[str, dict[str, Any]],
    policies: Mapping[str, Mapping[str, Any]],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[str]]:
    rows_by_id = {row["setting_id"]: row for row in registry["settings"]}
    weak_pairs = {
        (item["setting_id"], item["route"])
        for item in evidence_analysis["weak_route_rows"]
        if item["setting_id"] in evidence_analysis["unproven_closed_ids"]
    }
    candidates: list[dict[str, Any]] = []
    exclusions: list[dict[str, Any]] = []
    uncovered: list[str] = []

    state_gated_pairs = sorted(
        (setting_id, route)
        for setting_id, route in weak_pairs
        if rows_by_id[setting_id].get("routes", {}).get(route, {}).get("status") == "state_gated"
    )
    for setting_id, route in state_gated_pairs:
        policy = policies.get(setting_id)
        if not policy or policy.get("route") != route:
            uncovered.append(f"{setting_id}|{route}")
            continue
        if not policy.get("enabled"):
            exclusions.append(
                {
                    "setting_id": setting_id,
                    "route": route,
                    "state_id": policy["state_id"],
                    "reason": policy["exclusion_reason"],
                }
            )
            continue
        row = rows_by_id[setting_id]
        source = source_by_route.get(route, {})
        candidates.append(
            {
                "setting_id": setting_id,
                "label": row["label"],
                "route": route,
                "state_id": policy["state_id"],
                "why_live": policy["why_live"],
                "preconditions": list(row.get("preconditions") or []),
                "source_artifact": source.get("artifact"),
            }
        )

    items = _dedupe_items(candidates)
    grouped: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
    for item in items:
        grouped[(item["route"], item["state_id"])].append(item)

    groups = []
    for route, state_id in sorted(grouped):
        group_items = grouped[(route, state_id)]
        policy = policies[group_items[0]["setting_id"]]
        groups.append(
            {
                "group_id": f"{route}.{state_id}",
                "route": route,
                "state_id": state_id,
                "title": policy["title"],
                "setting_ids": [item["setting_id"] for item in group_items],
                "items": group_items,
                "operator_steps": list(policy["operator_steps"]),
                "command": policy["command"],
                "success_criteria": list(policy["success_criteria"]),
                "risk": policy["risk"],
            }
        )
    return groups, exclusions, uncovered


def _camera_remote_blockers(
    registry: dict[str, Any],
    source_by_route: Mapping[str, dict[str, Any]],
    artifact_root: Path,
) -> tuple[list[dict[str, Any]], Path | None, dict[str, Any] | None]:
    source = source_by_route["camera_remote_command"]
    if source["status"] == "loaded":
        return [], None, None

    rows = sorted(
        (
            row
            for row in registry["settings"]
            if _route_is_gap(row, "camera_remote_command")
        ),
        key=lambda row: row["setting_id"],
    )
    if not rows:
        return [], None, None

    locator = deterministic_best_locator(
        artifact_root / "r4_camera_remote_command",
    )
    locator_payload = _load_structured(locator, required=False) if locator else None
    locator_summary = (locator_payload or {}).get("summary") or {}
    items = _dedupe_items(
        {
            "setting_id": row["setting_id"],
            "label": row["label"],
            "route": "camera_remote_command",
            "state_id": "official_package_review",
            "global_status": row["status"],
            "route_status": row["routes"]["camera_remote_command"]["status"],
        }
        for row in rows
    )
    unresolved = [
        item
        for item in items
        if item["global_status"] not in NON_GAP_ROW_STATUSES
    ]
    redundant = [
        item
        for item in items
        if item["global_status"] in NON_GAP_ROW_STATUSES
    ]
    group = {
        "blocker_id": "sony_camera_remote_command_official_package",
        "route": "camera_remote_command",
        "state_id": "official_package_review",
        "title": "Obtain and review Sony's official Camera Remote Command package",
        "reason": (
            locator_summary.get("external_blocker")
            or "No readable official Camera Remote Command package-review artifact exists."
        ),
        "setting_count": len(items),
        "globally_unresolved_setting_count": len(unresolved),
        "route_redundant_setting_count": len(redundant),
        "globally_unresolved_setting_ids": [item["setting_id"] for item in unresolved],
        "route_redundant_setting_ids": [item["setting_id"] for item in redundant],
        "items": items,
        "latest_locator_artifact": _display_path(locator) if locator else None,
        "locator_result": locator_summary.get("result"),
        "acquisition_action": (
            locator_summary.get("next_action")
            or "Obtain the official package through Sony's eligible corporate-customer path."
        ),
        "local_search_command": (
            "python3 tools/find_camera_remote_command_package.py "
            "--include-common-search "
            "--out-dir artifacts/falsification/r4_camera_remote_command"
        ),
        "review_command": (
            "python3 tools/review_camera_remote_command_package.py "
            "/path/to/CameraRemoteCommand-package.zip "
            "--out-dir artifacts/falsification/r4_camera_remote_command"
        ),
    }
    return [group], locator, locator_payload


def _disposition_counts(
    registry: dict[str, Any],
    unproven_closed_ids: Iterable[str],
    live_setting_ids: set[str],
    excluded_setting_ids: set[str],
) -> dict[str, int]:
    rows_by_id = {row["setting_id"]: row for row in registry["settings"]}
    counts: dict[str, int] = defaultdict(int)
    for setting_id in unproven_closed_ids:
        row = rows_by_id[setting_id]
        next_action = str(row.get("next_action") or "").lower()
        if setting_id in live_setting_ids:
            bucket = "active_live_retest"
        elif setting_id in excluded_setting_ids:
            bucket = "route_implementation_or_decision"
        elif "only if" in next_action or "if needed" in next_action:
            bucket = "deliberate_deferral_documented"
        elif row["status"] == "proven_read":
            bucket = "read_only_product_decision"
        else:
            bucket = "route_implementation_or_decision"
        counts[bucket] += 1
    return dict(sorted(counts.items()))


def _tool_exists(command: str) -> bool:
    parts = command.split()
    try:
        index = parts.index("python3")
        tool = parts[index + 1]
    except (ValueError, IndexError):
        return True
    return (REPO_ROOT / tool).is_file()


def _fingerprint(source_manifest: Iterable[dict[str, Any]]) -> str:
    canonical = "\n".join(
        f"{item['role']}|{item['path']}|{item['sha256']}"
        for item in sorted(source_manifest, key=lambda item: (item["role"], item["path"]))
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def validate_report(report: Mapping[str, Any]) -> list[str]:
    errors: list[str] = []
    all_items = [
        item
        for group in report["live_test_groups"]
        for item in group["items"]
    ] + [
        item
        for group in report["external_blocker_groups"]
        for item in group["items"]
    ]
    keys = [item["dedupe_key"] for item in all_items]
    if len(keys) != len(set(keys)):
        errors.append("duplicate setting/state/route dedupe keys")
    if report["data_quality"]["uncovered_state_gated_pairs"]:
        errors.append(
            "state-gated weak-closure rows lack an explicit live/exhausted policy: "
            + ", ".join(report["data_quality"]["uncovered_state_gated_pairs"])
        )
    for group in report["live_test_groups"]:
        if not _tool_exists(group["command"]):
            errors.append(f"live command references a missing tool: {group['command']}")
    for group in report["external_blocker_groups"]:
        for field in ("local_search_command", "review_command"):
            if not _tool_exists(group[field]):
                errors.append(f"blocker command references a missing tool: {group[field]}")
    return errors


def build_report(
    *,
    registry_path: Path = DEFAULT_REGISTRY,
    completion_audit_path: Path = DEFAULT_COMPLETION_AUDIT,
    legacy_closure_path: Path = DEFAULT_LEGACY_CLOSURE,
    artifact_root: Path = DEFAULT_ARTIFACT_ROOT,
    live_policies: Mapping[str, Mapping[str, Any]] = LIVE_RETEST_POLICIES,
) -> dict[str, Any]:
    registry = _load_structured(registry_path, required=True)
    assert registry is not None
    settings = registry.get("settings")
    if not isinstance(settings, list) or not all(
        isinstance(row, dict) and isinstance(row.get("setting_id"), str)
        for row in settings
    ):
        raise ClosureDataError("registry must contain a settings array with string setting_id values")
    setting_ids = [row["setting_id"] for row in settings]
    if len(setting_ids) != len(set(setting_ids)):
        raise ClosureDataError("registry contains duplicate setting_id values")

    completion = _load_structured(completion_audit_path, required=False)
    legacy_closure = _load_structured(legacy_closure_path, required=False)
    review_index, source_statuses, review_paths = _load_review_index(artifact_root)
    source_by_route = {item["route"]: item for item in source_statuses}
    evidence = _analyze_evidence(registry, review_index)
    live_groups, exhausted, uncovered = _build_live_groups(
        registry,
        evidence,
        source_by_route,
        live_policies,
    )
    blockers, locator_path, _locator_payload = _camera_remote_blockers(
        registry,
        source_by_route,
        artifact_root,
    )

    source_manifest = [_source_manifest_entry(registry_path, "registry")]
    for path, role in (
        (completion_audit_path, "completion_audit"),
        (legacy_closure_path, "legacy_closure_snapshot"),
    ):
        if path.is_file():
            source_manifest.append(_source_manifest_entry(path, role))
    for path in review_paths:
        source_manifest.append(_source_manifest_entry(path, "route_review"))
    if locator_path is not None:
        source_manifest.append(_source_manifest_entry(locator_path, "command_package_locator"))

    live_ids = {
        item["setting_id"]
        for group in live_groups
        for item in group["items"]
    }
    excluded_ids = {item["setting_id"] for item in exhausted}
    dispositions = _disposition_counts(
        registry,
        evidence["unproven_closed_ids"],
        live_ids,
        excluded_ids,
    )
    blocker_items = [item for group in blockers for item in group["items"]]
    unresolved_blocker_items = [
        item
        for item in blocker_items
        if item["global_status"] not in NON_GAP_ROW_STATUSES
    ]
    redundant_blocker_items = [
        item
        for item in blocker_items
        if item["global_status"] in NON_GAP_ROW_STATUSES
    ]

    caveats: list[dict[str, Any]] = [
        {
            "code": "weak_closure_is_not_behavior_proof",
            "detail": (
                f"{len(evidence['weak_route_rows'])} route rows were closed by candidate, failed, "
                "or not-exposed verdicts rather than setting-level behavior proof."
            ),
        },
        {
            "code": "non_live_backlog_not_requeued",
            "detail": (
                f"{len(evidence['unproven_closed_ids']) - len(live_ids)} unproven-closed settings "
                "are implementation/product decisions, read-only decisions, exhausted attempts, "
                "or deliberate deferrals; they are not repeatable live tests."
            ),
        },
        {
            "code": "raw_ptp_focus_rejected",
            "detail": (
                "Raw PTP focus/focus-area protocol acceptance lacked visible behavior and is "
                "recorded as rejected/unsupported, so it is not re-emitted as a live test."
            ),
        },
        {
            "code": "legacy_closure_snapshot_not_authoritative",
            "detail": (
                "The legacy evidence-closure artifact is audited as an input snapshot but is not "
                "used for queue selection; this report recomputes from deterministic route reviews "
                "and prefers current-schema complete package scans over shallow locator runs."
            ),
        },
    ]
    for item in exhausted:
        caveats.append(
            {
                "code": "exhausted_repeat_test",
                "setting_id": item["setting_id"],
                "route": item["route"],
                "detail": item["reason"],
            }
        )
    if blockers:
        caveats.append(
            {
                "code": "official_command_package_missing",
                "detail": (
                    "Camera Remote Command capabilities cannot be mapped until Sony's official "
                    "package is obtained and a command-package-review artifact is generated."
                ),
            }
        )

    completion_summary = (completion or {}).get("summary") or {}
    completion_total = completion_summary.get("total_settings")
    registry_status_counts: dict[str, int] = defaultdict(int)
    for row in settings:
        registry_status_counts[str(row["status"])] += 1
    route_adjudication_complete = (
        not evidence["open_gap_rows"]
        and all(item["status"] == "loaded" for item in source_statuses)
    )
    full_behavior_control_complete = (
        route_adjudication_complete
        and not evidence["unproven_closed_ids"]
        and registry_status_counts["needs_probe"] == 0
    )
    consistency_checks = {
        "registry_setting_count": len(settings),
        "completion_audit_loaded": completion is not None,
        "completion_audit_setting_count": completion_total,
        "registry_matches_completion_audit": (
            completion_total == len(settings) if completion_total is not None else None
        ),
        "legacy_closure_loaded": legacy_closure is not None,
        "missing_review_routes": [
            item["route"] for item in source_statuses if item["status"] != "loaded"
        ],
    }

    report: dict[str, Any] = {
        "schema_version": 1,
        "scope": {
            "camera_free": True,
            "actionable_sections": ["live_test_groups", "external_blocker_groups"],
            "dedupe_key_fields": ["setting_id", "state_id", "route"],
            "selection_rule": (
                "Lexicographically latest artifact path per route; filesystem mtimes are ignored."
            ),
        },
        "summary": {
            "registry_settings": len(settings),
            "completion_state": completion_summary.get("completion_state"),
            "inventory_complete": len(settings) == 162,
            "route_adjudication_complete": route_adjudication_complete,
            "full_behavior_control_complete": full_behavior_control_complete,
            "registry_status_counts": dict(sorted(registry_status_counts.items())),
            "proven_set_settings": registry_status_counts["proven_set"],
            "proven_read_settings": registry_status_counts["proven_read"],
            "needs_probe_settings": registry_status_counts["needs_probe"],
            "live_test_groups": len(live_groups),
            "live_test_items": sum(len(group["items"]) for group in live_groups),
            "external_blocker_groups": len(blockers),
            "external_blocker_items": len(blocker_items),
            "external_blocker_globally_unresolved_items": len(unresolved_blocker_items),
            "external_blocker_route_redundant_items": len(redundant_blocker_items),
            "closed_without_behavior_proof_route_rows": len(evidence["weak_route_rows"]),
            "unique_unproven_closed_settings": len(evidence["unproven_closed_ids"]),
            "unproven_closed_not_live_ready": (
                len(evidence["unproven_closed_ids"]) - len(live_ids)
            ),
        },
        "live_test_groups": live_groups,
        "external_blocker_groups": blockers,
        "sources": {
            "input_fingerprint_sha256": _fingerprint(source_manifest),
            "files": sorted(source_manifest, key=lambda item: (item["role"], item["path"])),
            "route_reviews": source_statuses,
        },
        "data_quality": {
            "disposition_counts": dispositions,
            "exhausted_repeat_tests": exhausted,
            "uncovered_state_gated_pairs": uncovered,
            "consistency_checks": consistency_checks,
            "caveats": caveats,
        },
    }
    report["validation_errors"] = validate_report(report)
    return report


def to_markdown(report: Mapping[str, Any]) -> str:
    summary = report["summary"]
    lines = [
        "# A7 II Unresolved Settings Closure",
        "",
        "This is a deterministic, camera-free report. Its actionable output is limited to "
        "unresolved live tests and external blockers.",
        "Inventory coverage and route adjudication are reported separately from behavior-proven "
        "control; this report does not claim full control of all settings.",
        "",
        "## Summary",
        "",
        f"- Registry settings: `{summary['registry_settings']}`",
        f"- Inventory complete: `{summary['inventory_complete']}`",
        f"- Route adjudication complete: `{summary['route_adjudication_complete']}`",
        f"- Full behavior control complete: `{summary['full_behavior_control_complete']}`",
        f"- Registry `proven_set`: `{summary['proven_set_settings']}`",
        f"- Registry `proven_read`: `{summary['proven_read_settings']}`",
        f"- Registry `needs_probe`: `{summary['needs_probe_settings']}`",
        (
            "- Closed without behavior proof route rows: "
            f"`{summary['closed_without_behavior_proof_route_rows']}`"
        ),
        (
            "- Unique unproven-closed settings: "
            f"`{summary['unique_unproven_closed_settings']}`"
        ),
        f"- Remaining live test groups: `{summary['live_test_groups']}`",
        f"- Remaining live test items: `{summary['live_test_items']}`",
        f"- External blocker groups: `{summary['external_blocker_groups']}`",
        (
            "- External blocker route rows: "
            f"`{summary['external_blocker_items']}` "
            f"(`{summary['external_blocker_globally_unresolved_items']}` globally unresolved, "
            f"`{summary['external_blocker_route_redundant_items']}` route-redundant)"
        ),
        (
            "- Unproven-closed settings not live-ready: "
            f"`{summary['unproven_closed_not_live_ready']}`"
        ),
        f"- Input fingerprint: `{report['sources']['input_fingerprint_sha256']}`",
        "",
        "## Remaining Live Tests",
        "",
    ]
    if not report["live_test_groups"]:
        lines.append("None.")
    for group in report["live_test_groups"]:
        lines.extend(
            [
                f"### {group['group_id']}",
                "",
                f"- Title: {group['title']}",
                f"- Settings: `{', '.join(group['setting_ids'])}`",
                f"- Risk: `{group['risk']}`",
                "- Camera preparation:",
            ]
        )
        lines.extend(f"  {index}. {step}" for index, step in enumerate(group["operator_steps"], 1))
        lines.extend(
            [
                "- Command:",
                "",
                "```bash",
                group["command"],
                "```",
                "",
                "- Success criteria:",
            ]
        )
        lines.extend(f"  - {criterion}" for criterion in group["success_criteria"])
        lines.append("")

    lines.extend(["## External Blockers", ""])
    if not report["external_blocker_groups"]:
        lines.append("None.")
    for group in report["external_blocker_groups"]:
        lines.extend(
            [
                f"### {group['blocker_id']}",
                "",
                f"- Reason: {group['reason']}",
                f"- Route rows blocked: `{group['setting_count']}`",
                f"- Globally unresolved: `{group['globally_unresolved_setting_count']}`",
                f"- Route-redundant: `{group['route_redundant_setting_count']}`",
                f"- Latest locator: `{group['latest_locator_artifact']}`",
                f"- Locator result: `{group['locator_result']}`",
                f"- Acquisition action: {group['acquisition_action']}",
                "- Globally unresolved setting IDs:",
                "",
                "```text",
                "\n".join(group["globally_unresolved_setting_ids"]),
                "```",
                "",
                "- Route-redundant setting IDs:",
                "",
                "```text",
                "\n".join(group["route_redundant_setting_ids"]),
                "```",
                "",
                "- Search again:",
                "",
                "```bash",
                group["local_search_command"],
                "```",
                "",
                "- Review after acquisition:",
                "",
                "```bash",
                group["review_command"],
                "```",
                "",
            ]
        )

    lines.extend(["## Data-Quality Caveats", ""])
    for caveat in report["data_quality"]["caveats"]:
        setting = f" (`{caveat['setting_id']}`)" if caveat.get("setting_id") else ""
        lines.append(f"- `{caveat['code']}`{setting}: {caveat['detail']}")
    lines.extend(["", "### Dispositions", ""])
    for name, count in report["data_quality"]["disposition_counts"].items():
        lines.append(f"- `{name}`: {count}")
    lines.extend(["", "## Selected Evidence Sources", ""])
    for source in report["sources"]["route_reviews"]:
        lines.append(
            f"- `{source['route']}`: `{source['status']}`"
            + (f" via `{source['artifact']}`" if source["artifact"] else "")
        )
    if report["validation_errors"]:
        lines.extend(["", "## Validation Errors", ""])
        lines.extend(f"- {error}" for error in report["validation_errors"])
    return "\n".join(lines).rstrip() + "\n"


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--registry", type=Path, default=DEFAULT_REGISTRY)
    parser.add_argument("--completion-audit", type=Path, default=DEFAULT_COMPLETION_AUDIT)
    parser.add_argument("--legacy-closure", type=Path, default=DEFAULT_LEGACY_CLOSURE)
    parser.add_argument("--artifact-root", type=Path, default=DEFAULT_ARTIFACT_ROOT)
    parser.add_argument("--json", action="store_true", help="print JSON instead of Markdown")
    parser.add_argument("--out", type=Path, help="write JSON for .json, otherwise Markdown")
    parser.add_argument("--quiet", action="store_true", help="with --out, do not echo the report")
    parser.add_argument(
        "--strict",
        action="store_true",
        help="exit 2 if report validation finds uncovered state policies or missing tools",
    )
    args = parser.parse_args(argv)
    if args.quiet and not args.out:
        parser.error("--quiet requires --out")

    try:
        report = build_report(
            registry_path=args.registry,
            completion_audit_path=args.completion_audit,
            legacy_closure_path=args.legacy_closure,
            artifact_root=args.artifact_root,
        )
    except ClosureDataError as exc:
        parser.exit(2, f"closure reporter: {exc}\n")

    output = json.dumps(report, indent=2, sort_keys=True) if args.json else to_markdown(report)
    if args.out:
        args.out.parent.mkdir(parents=True, exist_ok=True)
        rendered = (
            json.dumps(report, indent=2, sort_keys=True) + "\n"
            if args.out.suffix.lower() == ".json"
            else to_markdown(report)
        )
        args.out.write_text(rendered, encoding="utf-8")
    if not args.quiet:
        print(output, end="" if output.endswith("\n") else "\n")
    if args.strict and report["validation_errors"]:
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
