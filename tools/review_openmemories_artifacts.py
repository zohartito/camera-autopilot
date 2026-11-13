#!/usr/bin/env python3
"""Map OpenMemories Route 3 artifacts back to A7 II registry rows.

This reviews local artifacts from `tools/falsify_openmemories_route3.py` plus optional endpoint
proof JSON files captured later from the camera-hosted proof app. It never talks to the camera and
does not edit the registry. The route stays conservative: generated app scaffolding is not proof of
camera control, and admin/developer rows require rollback port evidence.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parent.parent
BACKEND = REPO_ROOT / "backend"
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))

import settings_registry

CANONICAL_SUMMARY_NAME = "final-review-summary.json"

REQUIRED_ENDPOINTS = {
    "GET /health",
    "GET /settings",
    "GET /settings/{id}",
    "POST /preview",
    "POST /focus",
    "POST /capture",
}
ERGONOMIC_ENDPOINTS = {
    "POST /exit",
    "POST /shutdown",
    "GET /rollback",
}
DEVELOPER_PORTS = {23: "telnet", 5555: "adb", 8080: "proof_http"}
REQUIRED_DEVELOPER_PORTS = frozenset(DEVELOPER_PORTS)
OPENMEMORIES_SETTING_ALIASES = {
    "param.self_timer": "phone.self_timer",
    "param.iso_sensitivity": "camera.iso",
    "param.image_aspect": "camera.aspect_ratio",
    "param.image_size": "camera.image_size",
    "param.metering_mode": "camera.metering_mode",
    "param.dro_mode": "camera.dro_auto_hdr",
    "param.color_mode": "camera.creative_style",
    "param.picture_effect": "camera.picture_effect",
    "param.face_detection_mode": "camera.smile_face_detect",
    "param.flash_mode": "camera.flash_mode",
    "param.red_eye_reduction_mode": "camera.red_eye_reduction",
    "param.color_space": "camera.color_space",
    "param.apsc_mode": "custom.aps_c_size_capture",
    "param.raw_rec_mode": "camera.raw_file_type",
    "param.drive_mode": "camera.drive_mode",
    "param.soft_skin_effect": "camera.soft_skin_effect",
    "param.focus_mode": "camera.focus_mode",
}
REGISTRY_TO_PARAM_IDS: dict[str, set[str]] = {}
for _param_id, _registry_id in OPENMEMORIES_SETTING_ALIASES.items():
    REGISTRY_TO_PARAM_IDS.setdefault(_registry_id, set()).add(_param_id)


def read_json(path: Path) -> dict[str, Any] | None:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return payload if isinstance(payload, dict) else None


def relative_path(path: Path, roots: list[Path]) -> str:
    for root in roots:
        try:
            return str(path.relative_to(root))
        except ValueError:
            continue
    return str(path)


def iter_files(source_dirs: list[Path]) -> list[Path]:
    files: list[Path] = []
    for source_dir in source_dirs:
        source_dir = source_dir.expanduser().resolve()
        if not source_dir.exists():
            continue
        if source_dir.is_file():
            files.append(source_dir)
            continue
        files.extend(sorted(path for path in source_dir.rglob("*") if path.is_file()))
    return files


def openmemories_rows(registry: dict[str, Any] | None = None) -> list[dict[str, Any]]:
    registry = registry or settings_registry.load_registry()
    return [row for row in registry["settings"] if "openmemories" in row.get("routes", {})]


def endpoint_key(payload: dict[str, Any]) -> str | None:
    value = payload.get("endpoint") or payload.get("path") or payload.get("route")
    if not isinstance(value, str) or not value:
        return None
    method = payload.get("method")
    if isinstance(method, str) and method:
        return f"{method.upper()} {value}"
    if value.startswith("/"):
        return value
    return value


def endpoint_ok(payload: dict[str, Any]) -> bool:
    key = endpoint_key(payload)
    response_json = payload.get("response_json") if isinstance(payload.get("response_json"), dict) else {}
    payloads = [payload, response_json]
    if key in {"POST /focus", "POST /capture", "/focus", "/capture"}:
        return False
    if key in {"GET /settings/capture.count", "/settings/capture.count"}:
        return any(_positive_value(item, "value") for item in payloads)
    if key in {"GET /settings/capture.bytes", "/settings/capture.bytes"}:
        return any(_positive_value(item, "value") for item in payloads)
    if key in {"GET /media/latest", "/media/latest"}:
        return any(_positive_value(item, "latestCaptureBytes") for item in payloads)
    if payload.get("ok") is True:
        return True
    if payload.get("cameraOpen") is True:
        if str(payload.get("endpoint") or payload.get("path") or "") == "/media/latest":
            try:
                return float(payload.get("latestCaptureBytes") or 0) > 0
            except (TypeError, ValueError):
                return False
        return True
    return False


def _positive_value(payload: dict[str, Any], key: str) -> bool:
    try:
        return float(payload.get(key) or 0) > 0
    except (TypeError, ValueError):
        return False


def collect_artifacts(source_dirs: list[Path]) -> dict[str, Any]:
    roots = [source_dir.expanduser().resolve() for source_dir in source_dirs]
    files = iter_files(roots)
    preflights = []
    port_scans = []
    endpoint_proofs = []
    focus_observations = []
    setting_write_proofs = []
    for path in files:
        if path.suffix.lower() != ".json":
            continue
        payload = read_json(path)
        if payload is None:
            continue
        if path.name == "route3-preflight.json":
            generated = payload.get("generated_app") if isinstance(payload.get("generated_app"), dict) else {}
            preflights.append(
                {
                    "artifact": relative_path(path, roots),
                    "status": payload.get("status"),
                    "build_blockers": payload.get("build_blockers") or [],
                    "generated_app_path": generated.get("path"),
                    "generated_endpoints": generated.get("endpoints") or [],
                    "build_ok": isinstance(payload.get("build"), dict) and payload["build"].get("ok") is True,
                    "ports": payload.get("ports") or [],
                    "adb_probed": payload.get("adb") is not None,
                }
            )
            continue
        if (
            path.name == "openmemories-rollback-ports.json"
            or payload.get("tool") == "prove_openmemories_rollback_ports"
        ):
            port_scans.append(
                {
                    "artifact": relative_path(path, roots),
                    "host": payload.get("host"),
                    "network_context_proven": payload.get("network_context_proven") is True,
                    "ports": payload.get("ports") or [],
                    "rollback_closed_evidence": payload.get("rollback_closed_evidence") is True,
                    "policy": payload.get("policy") or {},
                }
            )
            continue
        if payload.get("tool") == "prove_openmemories_focus_observation":
            focus_observations.append(
                {
                    "artifact": relative_path(path, roots),
                    "endpoint": endpoint_key(payload),
                    "ok": payload.get("ok") is True,
                    "observed_before": payload.get("observed_before"),
                    "observed_after": payload.get("observed_after"),
                    "observation_note": payload.get("observation_note"),
                    "proof_artifact": payload.get("proof_artifact"),
                    "proof_artifact_exists": payload.get("proof_artifact_exists") is True,
                }
            )
            continue
        if payload.get("artifact") in {
            "openmemories_param_self_timer_set_read_revert",
            "openmemories_param_set_read_revert",
        }:
            raw_setting_id = str(payload.get("setting_id") or "")
            registry_setting_id = payload.get("registry_setting_id")
            if not isinstance(registry_setting_id, str) or not registry_setting_id:
                registry_setting_id = OPENMEMORIES_SETTING_ALIASES.get(raw_setting_id, raw_setting_id)
            generic_write = payload.get("artifact") == "openmemories_param_set_read_revert"
            ok = payload.get("ok") is True and (
                not generic_write or payload.get("executed") is True
            )
            setting_write_proofs.append(
                {
                    "artifact": relative_path(path, roots),
                    "setting_id": registry_setting_id,
                    "endpoint_setting_id": raw_setting_id,
                    "requested": payload.get("requested"),
                    "route": payload.get("route"),
                    "ok": ok,
                    "dry_run_ok": payload.get("dry_run_ok"),
                    "executed": payload.get("executed"),
                    "changed": payload.get("changed"),
                    "before": payload.get("before"),
                    "after_set": payload.get("after_set"),
                    "after_revert": payload.get("after_revert"),
                    "step_count": len(payload.get("steps") or []),
                }
            )
            continue

        key = endpoint_key(payload)
        if key is not None:
            endpoint_proofs.append(
                {
                    "artifact": relative_path(path, roots),
                    "endpoint": key,
                    "ok": endpoint_ok(payload),
                    "status_code": payload.get("status_code") or payload.get("http_status"),
                    "payload_keys": sorted(payload.keys()),
                }
            )
    return {
        "roots": [str(root) for root in roots],
        "files": len(files),
        "preflights": preflights,
        "port_scans": port_scans,
        "endpoint_proofs": endpoint_proofs,
        "focus_observations": focus_observations,
        "setting_write_proofs": setting_write_proofs,
    }


def latest_preflight(preflights: list[dict[str, Any]]) -> dict[str, Any] | None:
    if not preflights:
        return None
    return sorted(preflights, key=lambda item: item["artifact"])[-1]


def latest_port_scan(port_scans: list[dict[str, Any]] | None) -> dict[str, Any] | None:
    if not port_scans:
        return None
    return sorted(port_scans, key=lambda item: item["artifact"])[-1]


def generated_endpoint_set(preflights: list[dict[str, Any]]) -> set[str]:
    endpoints: set[str] = set()
    for preflight in preflights:
        endpoints.update(str(item) for item in preflight.get("generated_endpoints") or [])
    return endpoints


def endpoint_proof_set(endpoint_proofs: list[dict[str, Any]]) -> set[str]:
    proven = set()
    for proof in endpoint_proofs:
        if proof.get("ok") is True:
            proven.add(str(proof["endpoint"]))
    return proven


def setting_endpoint_proven(setting_id: str, proven_endpoints: set[str]) -> bool:
    endpoint_ids = {setting_id} | REGISTRY_TO_PARAM_IDS.get(setting_id, set())
    return any(
        endpoint in proven_endpoints
        for endpoint_id in endpoint_ids
        for endpoint in (
            f"GET /settings/{endpoint_id}",
            f"/settings/{endpoint_id}",
        )
    )


def setting_write_proof_set(setting_write_proofs: list[dict[str, Any]]) -> set[str]:
    return {
        str(proof["setting_id"])
        for proof in setting_write_proofs
        if proof.get("ok") is True and proof.get("setting_id")
    }


def port_summary(preflights: list[dict[str, Any]], port_scans: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    observed = []
    historical_open_ports = []
    historical_closed_ports = []
    rollback_open_ports = []
    rollback_closed_ports = []
    current_open_ports = []
    current_closed_ports = []
    current_unknown_ports = []
    latest_scan = latest_port_scan(port_scans)
    sources = [
        {"artifact": preflight["artifact"], "ports": preflight.get("ports") or [], "kind": "preflight"}
        for preflight in preflights
    ]
    sources.extend(
        {"artifact": scan["artifact"], "ports": scan.get("ports") or [], "kind": "rollback_scan"}
        for scan in (port_scans or [])
    )
    for source in sources:
        for item in source["ports"]:
            if not isinstance(item, dict):
                continue
            port = item.get("port")
            if port not in DEVELOPER_PORTS:
                continue
            evidence = {
                "artifact": source["artifact"],
                "host": item.get("host"),
                "port": port,
                "name": DEVELOPER_PORTS[port],
                "open": item.get("open") is True,
                "observed_state": (
                    "open"
                    if item.get("open") is True
                    else "closed"
                    if item.get("open") is False
                    else "unknown"
                ),
                "error": item.get("error"),
                "source_kind": source["kind"],
            }
            observed.append(evidence)
            if evidence["observed_state"] == "open":
                historical_open_ports.append(evidence)
                if source["kind"] == "rollback_scan":
                    rollback_open_ports.append(evidence)
            elif evidence["observed_state"] == "closed":
                historical_closed_ports.append(evidence)
                if source["kind"] == "rollback_scan":
                    rollback_closed_ports.append(evidence)
    current_source = None
    if latest_scan is not None:
        current_source = {
            "artifact": latest_scan["artifact"],
            "ports": latest_scan.get("ports") or [],
            "kind": "rollback_scan",
        }
    elif preflights:
        current_preflight = latest_preflight(preflights)
        current_source = {
            "artifact": current_preflight["artifact"],
            "ports": current_preflight.get("ports") or [],
            "kind": "preflight",
        }
    if current_source is not None:
        for item in current_source["ports"]:
            if not isinstance(item, dict):
                continue
            port = item.get("port")
            if port not in DEVELOPER_PORTS:
                continue
            evidence = {
                "artifact": current_source["artifact"],
                "host": item.get("host"),
                "port": port,
                "name": DEVELOPER_PORTS[port],
                "open": item.get("open") is True,
                "observed_state": (
                    "open"
                    if item.get("open") is True
                    else "closed"
                    if item.get("open") is False
                    else "unknown"
                ),
                "error": item.get("error"),
                "source_kind": current_source["kind"],
            }
            if evidence["observed_state"] == "open":
                current_open_ports.append(evidence)
            elif evidence["observed_state"] == "closed":
                current_closed_ports.append(evidence)
            else:
                current_unknown_ports.append(evidence)
    latest_items = latest_scan.get("ports") or [] if latest_scan is not None else []
    latest_required_items = [
        item
        for item in latest_items
        if isinstance(item, dict) and item.get("port") in REQUIRED_DEVELOPER_PORTS
    ]
    latest_scanned_ports = {
        item.get("port")
        for item in latest_items
        if isinstance(item, dict) and isinstance(item.get("port"), int)
    }
    latest_complete_required_scan = (
        len(latest_items) == len(REQUIRED_DEVELOPER_PORTS)
        and len(latest_required_items) == len(REQUIRED_DEVELOPER_PORTS)
        and latest_scanned_ports == REQUIRED_DEVELOPER_PORTS
    )
    latest_all_required_closed = (
        latest_complete_required_scan
        and all(item.get("open") is False for item in latest_required_items)
    )
    latest_rollback_closed = (
        latest_scan is not None
        and latest_scan.get("rollback_closed_evidence") is True
        and latest_scan.get("network_context_proven") is True
        and latest_complete_required_scan
        and latest_all_required_closed
    )
    return {
        "observed": observed,
        "open_ports": current_open_ports,
        "closed_ports": current_closed_ports,
        "unknown_ports": current_unknown_ports,
        "historical_open_ports": historical_open_ports,
        "historical_closed_ports": historical_closed_ports,
        "rollback_open_ports": rollback_open_ports,
        "rollback_closed_ports": rollback_closed_ports,
        "latest_rollback_scan": latest_scan,
        "required_ports": sorted(REQUIRED_DEVELOPER_PORTS),
        "latest_scanned_ports": sorted(latest_scanned_ports),
        "latest_complete_required_scan": latest_complete_required_scan,
        "latest_all_required_closed": latest_all_required_closed,
        "has_rollback_closed_evidence": (
            latest_rollback_closed
            and len(current_closed_ports) == len(REQUIRED_DEVELOPER_PORTS)
            and not current_open_ports
            and not current_unknown_ports
        ),
    }


def base_route_verdict(
    *,
    preflight: dict[str, Any] | None,
    generated_endpoints: set[str],
    proven_endpoints: set[str],
) -> str:
    if not preflight:
        return "needs_route3_preflight"
    if "GET /health" in proven_endpoints or "/health" in proven_endpoints:
        if action_completion_proven(proven_endpoints):
            return "proof_app_action_endpoint_proven"
        return "proof_app_health_proven"
    if REQUIRED_ENDPOINTS <= generated_endpoints:
        if preflight.get("build_ok"):
            return "app_built_needs_install_and_http_proof"
        if preflight.get("build_blockers"):
            return "proof_app_scaffolded_needs_tooling"
        return "proof_app_scaffolded_ready_for_build"
    if preflight.get("status") == "NEEDS-TOOLING":
        return "needs_tooling"
    return "needs_scaffold_or_endpoint_proof"


def action_completion_proven(proven_endpoints: set[str]) -> bool:
    media_proven = any(endpoint in proven_endpoints for endpoint in ("GET /media/latest", "/media/latest"))
    count_proven = any(
        endpoint in proven_endpoints
        for endpoint in ("GET /settings/capture.count", "/settings/capture.count")
    )
    bytes_proven = any(
        endpoint in proven_endpoints
        for endpoint in ("GET /settings/capture.bytes", "/settings/capture.bytes")
    )
    return media_proven or (count_proven and bytes_proven)


def row_verdict(
    row: dict[str, Any],
    *,
    base_verdict: str,
    ports: dict[str, Any],
    proven_endpoints: set[str],
    proven_setting_writes: set[str],
    exposed_setting_ids: set[str],
) -> str:
    setting_id = row["setting_id"]
    route = row["routes"]["openmemories"]
    if row["status"] == "unsupported_on_a7ii" or route.get("status") == "unsupported_on_a7ii":
        return "unsupported_on_a7ii"
    if setting_id in {"openmem.enable_adb", "openmem.enable_telnet"}:
        if ports["open_ports"]:
            return "admin_port_open_needs_rollback"
        if ports["has_rollback_closed_evidence"]:
            return "rollback_ports_closed"
        return "needs_rollback_port_scan"
    if row["status"] == "dangerous_admin":
        return "dangerous_admin_needs_explicit_approval"
    if setting_id in proven_setting_writes:
        return "proof_app_setting_write_proven"
    if setting_endpoint_proven(setting_id, proven_endpoints):
        return "proof_app_setting_endpoint_proven"
    if base_verdict in {"proof_app_action_endpoint_proven", "proof_app_health_proven"}:
        # The live app is proven at route level, but that is not per-setting evidence.
        if setting_id in exposed_setting_ids:
            return "proof_app_endpoint_available_needs_live_proof"
        return "proof_app_setting_not_exposed"
    return base_verdict


def _row_endpoint_ids(setting_id: str) -> set[str]:
    return {setting_id} | REGISTRY_TO_PARAM_IDS.get(setting_id, set())


def _row_endpoint_proofs(setting_id: str, endpoint_proofs: list[dict[str, Any]]) -> list[dict[str, Any]]:
    endpoint_ids = _row_endpoint_ids(setting_id)
    matched = []
    for proof in endpoint_proofs:
        endpoint = str(proof.get("endpoint") or "")
        if any(f"/settings/{endpoint_id}" in endpoint for endpoint_id in endpoint_ids):
            matched.append(proof)
    return matched


def _row_write_proofs(setting_id: str, setting_write_proofs: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [proof for proof in setting_write_proofs if proof.get("setting_id") == setting_id]


def _behavior_evidence(write_proofs: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Turn saved set/read/revert cycles into promotion-grade behavior evidence rows."""
    evidence = []
    for proof in write_proofs:
        if proof.get("ok") is not True:
            continue
        before = proof.get("before")
        after_set = proof.get("after_set")
        requested = proof.get("requested")
        value_changed = proof.get("changed")
        if value_changed is None and before is not None and after_set is not None:
            value_changed = before != after_set
        evidence.append(
            {
                "behavior_type": "set_read_revert",
                "method": f"POST /settings/{proof.get('endpoint_setting_id') or proof.get('setting_id')}",
                "requested_value": requested,
                "observed_before": before,
                "observed_after": after_set,
                "observed_after_revert": proof.get("after_revert"),
                "value_changed": bool(value_changed),
                "after_matches_request": (
                    str(after_set) == str(requested) if requested is not None and after_set is not None else None
                ),
                "artifact": proof.get("artifact"),
            }
        )
    return evidence


def map_registry(artifacts: dict[str, Any], *, registry: dict[str, Any] | None = None) -> list[dict[str, Any]]:
    preflight = latest_preflight(artifacts["preflights"])
    endpoints = generated_endpoint_set(artifacts["preflights"])
    proven = endpoint_proof_set(artifacts["endpoint_proofs"])
    all_write_proofs = artifacts.get("setting_write_proofs") or []
    proven_writes = setting_write_proof_set(all_write_proofs)
    ports = port_summary(artifacts["preflights"], artifacts.get("port_scans") or [])
    base_verdict = base_route_verdict(
        preflight=preflight,
        generated_endpoints=endpoints,
        proven_endpoints=proven,
    )
    exposed_setting_ids = set(OPENMEMORIES_SETTING_ALIASES.values()) | proven_writes
    rows = []
    for row in openmemories_rows(registry):
        route = row["routes"]["openmemories"]
        setting_id = row["setting_id"]
        row_write_proofs = _row_write_proofs(setting_id, all_write_proofs)
        rows.append(
            {
                "setting_id": setting_id,
                "label": row["label"],
                "registry_status": row["status"],
                "route_status": route.get("status"),
                "visible_in": route.get("visible_in"),
                "preflight_evidence": preflight,
                "generated_endpoints": sorted(endpoints),
                "endpoint_proofs": _row_endpoint_proofs(setting_id, artifacts["endpoint_proofs"]),
                "setting_write_proofs": row_write_proofs,
                "behavior_evidence": _behavior_evidence(row_write_proofs),
                "rollback_port_evidence": ports,
                "verdict": row_verdict(
                    row,
                    base_verdict=base_verdict,
                    ports=ports,
                    proven_endpoints=proven,
                    proven_setting_writes=proven_writes,
                    exposed_setting_ids=exposed_setting_ids,
                ),
                "next_action": row["next_action"],
            }
        )
    return rows


def _compact_behavior_evidence(row: dict[str, Any]) -> list[dict[str, Any]]:
    fields = (
        "human_verdict",
        "observed_behavior",
        "target_setup",
        "behavior_type",
        "method",
        "requested_value",
        "observed_before",
        "observed_after",
        "value_changed",
        "after_matches_request",
    )
    items = row.get("behavior_evidence") or []
    if not items:
        return []
    latest = items[-1]
    return [{key: latest[key] for key in fields if key in latest}]


def compact_review(report: dict[str, Any]) -> dict[str, Any]:
    """Return the commit-safe setting evidence without LAN or session metadata."""
    mapping = []
    for row in report["registry_mapping"]:
        compact = {
            key: row[key]
            for key in (
                "setting_id",
                "label",
                "registry_status",
                "route_status",
                "visible_in",
                "generated_endpoints",
                "verdict",
                "next_action",
            )
            if key in row
        }
        compact["endpoint_proof_count"] = len(row.get("endpoint_proofs") or [])
        behavior = _compact_behavior_evidence(row)
        compact["behavior_evidence"] = behavior
        ports = row.get("rollback_port_evidence") or {}
        compact["rollback_port_evidence"] = {
            "open_ports": [
                {"port": item.get("port"), "name": item.get("name")}
                for item in ports.get("open_ports") or []
            ],
            "has_rollback_closed_evidence": bool(
                ports.get("has_rollback_closed_evidence")
            ),
        }
        mapping.append(compact)

    return {
        "schema_version": 2,
        "route": "openmemories_route3",
        "generated_at": report["generated_at"],
        "source_group_count": len(report.get("source_dirs") or []),
        "artifact_summary": report["artifact_summary"],
        "registry_mapping_summary": report["registry_mapping_summary"],
        "registry_mapping": mapping,
        "raw_review_commit_policy": (
            "Timestamped detail is excluded because it contains verbose network and session "
            "metadata; this sanitized setting-level summary is the canonical committed review."
        ),
    }


def build_report(
    source_dirs: list[Path],
    *,
    out_dir: Path | None = None,
    registry: dict[str, Any] | None = None,
) -> tuple[dict[str, Any], Path]:
    if not source_dirs:
        source_dirs = [REPO_ROOT / "artifacts" / "falsification" / "r3_openmemories"]
    artifacts = collect_artifacts(source_dirs)
    mapping = map_registry(artifacts, registry=registry)
    by_verdict: dict[str, int] = {}
    for row in mapping:
        verdict = row["verdict"]
        by_verdict[verdict] = by_verdict.get(verdict, 0) + 1

    out_dir = out_dir or REPO_ROOT / "artifacts" / "falsification" / "r3_openmemories_review"
    out_dir.mkdir(parents=True, exist_ok=True)
    stamp = time.strftime("%Y%m%d-%H%M%S")
    report_path = out_dir / f"{stamp}-openmemories-review.json"
    ports = port_summary(artifacts["preflights"], artifacts.get("port_scans") or [])
    report = {
        "generated_at": stamp,
        "source_dirs": artifacts["roots"],
        "artifact_summary": {
            "files": artifacts["files"],
            "preflight_count": len(artifacts["preflights"]),
            "rollback_port_scan_count": len(artifacts["port_scans"]),
            "endpoint_proof_count": len(artifacts["endpoint_proofs"]),
            "setting_write_proof_count": len(artifacts["setting_write_proofs"]),
            "generated_endpoints": sorted(generated_endpoint_set(artifacts["preflights"])),
            "generated_ergonomic_endpoints": sorted(
                generated_endpoint_set(artifacts["preflights"]) & ERGONOMIC_ENDPOINTS
            ),
            "missing_ergonomic_endpoints": sorted(
                ERGONOMIC_ENDPOINTS - generated_endpoint_set(artifacts["preflights"])
            ),
            "proven_endpoints": sorted(endpoint_proof_set(artifacts["endpoint_proofs"])),
            "proven_setting_writes": sorted(
                setting_write_proof_set(artifacts["setting_write_proofs"])
            ),
            "open_developer_ports": ports["open_ports"],
            "rollback_closed_evidence": ports["has_rollback_closed_evidence"],
            "focus_observation_count": len(artifacts["focus_observations"]),
            "focus_observation_proven": any(
                observation.get("ok") is True for observation in artifacts["focus_observations"]
            ),
        },
        "registry_mapping_summary": {
            "openmemories_rows": len(mapping),
            "by_verdict": dict(sorted(by_verdict.items())),
            "route_level_verdict": base_route_verdict(
                preflight=latest_preflight(artifacts["preflights"]),
                generated_endpoints=generated_endpoint_set(artifacts["preflights"]),
                proven_endpoints=endpoint_proof_set(artifacts["endpoint_proofs"]),
            ),
            "proof_app_health_proven_rows": by_verdict.get("proof_app_health_proven", 0),
            "proof_app_action_endpoint_proven_rows": by_verdict.get(
                "proof_app_action_endpoint_proven", 0
            ),
            "proof_app_setting_endpoint_proven_rows": by_verdict.get(
                "proof_app_setting_endpoint_proven", 0
            ),
            "proof_app_setting_write_proven_rows": by_verdict.get(
                "proof_app_setting_write_proven", 0
            ),
            "proof_app_endpoint_available_needs_live_proof_rows": by_verdict.get(
                "proof_app_endpoint_available_needs_live_proof", 0
            ),
            "proof_app_setting_not_exposed_rows": by_verdict.get(
                "proof_app_setting_not_exposed", 0
            ),
            "needs_route3_preflight_rows": by_verdict.get("needs_route3_preflight", 0),
        },
        "registry_mapping": mapping,
    }
    report_path.write_text(json.dumps(report, indent=2, sort_keys=True), encoding="utf-8")
    canonical_path = out_dir / CANONICAL_SUMMARY_NAME
    canonical_path.write_text(
        json.dumps(compact_review(report), indent=2, sort_keys=True),
        encoding="utf-8",
    )
    return report, report_path


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "source_dir",
        nargs="*",
        help="OpenMemories artifact root(s). Defaults to artifacts/falsification/r3_openmemories.",
    )
    parser.add_argument(
        "--out-dir",
        default="artifacts/falsification/r3_openmemories_review",
        help="directory for the registry-mapped OpenMemories review report",
    )
    args = parser.parse_args(argv)
    report, report_path = build_report(
        [Path(item) for item in args.source_dir],
        out_dir=Path(args.out_dir),
    )
    print(
        json.dumps(
            {
                "report": str(report_path),
                "artifact_summary": report["artifact_summary"],
                "registry_mapping_summary": report["registry_mapping_summary"],
            },
            indent=2,
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
