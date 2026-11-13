#!/usr/bin/env python3
"""Map Sony Smart Remote API-surface artifacts back to A7 II registry rows.

This tool reviews artifacts produced by `falsify_smart_remote_api_surface.py`. It does not call the
camera and it does not promote rows to behavior-proven status. Its job is narrower: turn raw
ScalarWebAPI method dumps into per-setting evidence so the next hardware pass can target exact
set/read/revert or behavior-proof commands.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import time
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parent.parent
BACKEND = REPO_ROOT / "backend"
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))

import settings_registry

SMART_REMOTE_ROUTES = {"smart_remote_wifi", "smart_remote_phone"}
SETTER_PREFIXES = ("act", "set", "start", "stop")
GETTER_PREFIXES = ("get",)

GENERIC_WORDS = {
    "area",
    "camera",
    "display",
    "file",
    "format",
    "image",
    "info",
    "mode",
    "record",
    "remote",
    "setting",
    "settings",
    "smart",
    "still",
    "the",
    "view",
}

ROW_METHOD_ALIASES: dict[str, tuple[str, ...]] = {
    "camera.image_size": ("setStillSize", "getAvailableStillSize", "getStillSize"),
    "camera.aspect_ratio": ("setStillSize", "getAvailableStillSize", "getStillSize"),
    "phone.review_image_size": (
        "setPostviewImageSize",
        "getAvailablePostviewImageSize",
        "getPostviewImageSize",
    ),
    "camera.file_format": ("setMovieFileFormat", "getAvailableMovieFileFormat"),
    "camera.drive_mode": ("setShootMode", "getAvailableShootMode", "actSelfTimer"),
    "camera.flash_mode": ("setFlashMode", "getAvailableFlashMode", "getFlashMode"),
    "camera.flash_comp": (
        "setFlashCompensation",
        "getAvailableFlashCompensation",
        "getFlashCompensation",
    ),
    "camera.focus_mode": ("setFocusMode", "getAvailableFocusMode", "getFocusMode"),
    "camera.focus_area": (
        "setTouchAFPosition",
        "getTouchAFPosition",
        "cancelTouchAFPosition",
    ),
    "camera.exposure_comp": (
        "setExposureCompensation",
        "getAvailableExposureCompensation",
        "getExposureCompensation",
    ),
    "camera.iso": ("setIsoSpeedRate", "getAvailableIsoSpeedRate", "getIsoSpeedRate"),
    "camera.metering_mode": ("setMeteringMode", "getAvailableMeteringMode", "getMeteringMode"),
    "camera.white_balance": ("setWhiteBalance", "getAvailableWhiteBalance", "getWhiteBalance"),
    "camera.dro_auto_hdr": ("setDRO", "getAvailableDRO", "getDRO"),
    "camera.creative_style": (
        "setCreativeStyle",
        "getAvailableCreativeStyle",
        "getCreativeStyle",
    ),
    "camera.picture_effect": (
        "setPictureEffect",
        "getAvailablePictureEffect",
        "getPictureEffect",
    ),
    "camera.zoom": ("actZoom", "setZoomSetting", "getAvailableZoomSetting"),
    "camera.focus_magnifier": ("actZoom", "startLiveviewWithSize"),
    "camera.smile_face_detect": (
        "setFaceDetection",
        "getAvailableFaceDetection",
        "getFaceDetection",
    ),
    "phone.self_timer": ("setSelfTimer", "getAvailableSelfTimer", "getSelfTimer"),
    "phone.camera_information": ("getEvent", "getAvailableApiList"),
    "phone.save_options": (
        "setCameraFunction",
        "getAvailableCameraFunction",
        "getContentList",
        "getSchemeList",
        "getSourceList",
    ),
    "phone.location_information": ("setGeotag", "setGpsInfo"),
    "phone.grid_line": ("setLiveviewFrameInfo", "getLiveviewFrameInfo"),
    "phone.mirror_mode": ("setLiveviewOrientation", "getLiveviewOrientation"),
    "wireless.send_to_smartphone": (
        "getContentList",
        "getSchemeList",
        "getSourceList",
        "setStreamingContent",
    ),
    "playback.view_mode": ("getContentList", "getSchemeList", "getSourceList"),
    "playback.image_index": ("getContentList", "getContentCount"),
    "playback.enlarge_image": ("getContentList", "getContentInfo"),
    "playback.still_image_pb": ("getContentList", "getContentInfo"),
    "setup.display_media_info": ("getStorageInformation", "getContentCount"),
}


def clean_text(value: str) -> str:
    return re.sub(r"\s+", " ", value).strip()


def compact(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", "", value.lower())


def words(value: str) -> set[str]:
    return {
        word
        for word in re.split(r"[^a-z0-9]+", value.lower())
        if len(word) >= 3 and word not in GENERIC_WORDS
    }


def pascal_words(value: str) -> str:
    parts = [
        part
        for part in re.split(r"[^A-Za-z0-9]+", value.replace("/", " "))
        if part and part.lower() not in {"and", "or", "w", "wo"}
    ]
    return "".join(part[:1].upper() + part[1:] for part in parts)


def read_json(path: Path) -> dict[str, Any] | None:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return payload if isinstance(payload, dict) else None


def iter_json_artifacts(source_dir: Path) -> list[tuple[Path, dict[str, Any]]]:
    return [
        (path, payload)
        for path in sorted(source_dir.rglob("*.json"))
        if (payload := read_json(path)) is not None
    ]


def extract_api_list(payload: dict[str, Any]) -> list[str]:
    apis = payload.get("apis")
    if isinstance(apis, list):
        return [str(api) for api in apis]

    parsed = payload.get("json")
    if isinstance(parsed, dict):
        result = parsed.get("result")
        if isinstance(result, list) and result and isinstance(result[0], list):
            return [str(api) for api in result[0]]
    return []


def requested_method(payload: dict[str, Any]) -> str | None:
    request = payload.get("request")
    if isinstance(request, dict) and isinstance(request.get("method"), str):
        return request["method"]
    return None


def relative_path(path: Path, root: Path) -> str:
    try:
        return str(path.relative_to(root))
    except ValueError:
        return str(path)


def route_rows(registry: dict[str, Any] | None = None) -> list[dict[str, Any]]:
    registry = registry or settings_registry.load_registry()
    rows = []
    for row in registry["settings"]:
        routes = row.get("routes", {})
        if SMART_REMOTE_ROUTES & set(routes):
            rows.append(row)
    return rows


def row_candidate_methods(row: dict[str, Any]) -> list[str]:
    route_methods = []
    for route_name in SMART_REMOTE_ROUTES:
        route = row.get("routes", {}).get(route_name, {})
        for key in ("method", "method_hint"):
            method = route.get(key)
            if isinstance(method, str) and method:
                route_methods.append(method)

    stems = {
        pascal_words(row["label"]),
        pascal_words(row["setting_id"].split(".", 1)[1]),
    }
    generated = []
    for stem in sorted(stem for stem in stems if stem):
        generated.extend(
            [
                f"set{stem}",
                f"get{stem}",
                f"getAvailable{stem}",
                f"getSupported{stem}",
            ]
        )

    candidates = [*route_methods, *ROW_METHOD_ALIASES.get(row["setting_id"], ()), *generated]
    seen = set()
    out = []
    for method in candidates:
        key = compact(method)
        if not key or key in seen:
            continue
        seen.add(key)
        out.append(method)
    return out


def method_matches_row(method: str, row: dict[str, Any]) -> tuple[bool, list[str]]:
    compact_method = compact(method)
    matched = []
    for candidate in row_candidate_methods(row):
        candidate_compact = compact(candidate)
        if candidate == method or (
            len(candidate_compact) >= 6
            and (candidate_compact in compact_method or compact_method in candidate_compact)
        ):
            matched.append(candidate)
    if matched:
        return True, matched

    row_words = words(row["label"]) | words(row["setting_id"].split(".", 1)[1])
    method_words = words(method)
    overlap = sorted(row_words & method_words)
    if len(overlap) >= 2:
        return True, overlap
    return False, []


def _behavior_proof_item(path: Path, payload: dict[str, Any], source_dir: Path) -> dict[str, Any] | None:
    if payload.get("tool") != "a7ii_smart_remote_behavior_proof" and path.name != "smart-remote-behavior-proof.json":
        return None
    setting_id = payload.get("setting_id")
    method = payload.get("method")
    if not isinstance(setting_id, str) or not setting_id:
        return None
    if not isinstance(method, str) or not method:
        return None
    return {
        "artifact": relative_path(path, source_dir),
        "method": method,
        "requested_value": payload.get("requested_value"),
        "observed_before": payload.get("observed_before"),
        "observed_after": payload.get("observed_after"),
        "value_changed": payload.get("value_changed"),
        "after_matches_request": payload.get("after_matches_request"),
        "observed_behavior": payload.get("observed_behavior"),
        "human_verdict": payload.get("human_verdict"),
        "behavior_proven": payload.get("behavior_proven") is True,
        "source_artifacts": payload.get("source_artifacts"),
        "copied_evidence": payload.get("copied_evidence"),
        "reviewer": payload.get("reviewer"),
        "notes": payload.get("notes"),
    }


def _setter_attempt_item(path: Path, payload: dict[str, Any], source_dir: Path) -> dict[str, Any] | None:
    if payload.get("tool") != "a7ii_smart_remote_set_observe":
        return None
    setting_id = payload.get("setting_id")
    request = payload.get("request")
    if not isinstance(setting_id, str) or not setting_id:
        return None
    if not isinstance(request, dict):
        return None
    method = request.get("method")
    if not isinstance(method, str) or not method:
        return None
    error = payload.get("error")
    return {
        "artifact": relative_path(path, source_dir),
        "method": method,
        "params": request.get("params"),
        "requested_value": request.get("value"),
        "auto_value": request.get("auto_value"),
        "executed": payload.get("executed") is True,
        "ok": payload.get("ok") is True,
        "error_type": error.get("type") if isinstance(error, dict) else None,
        "error_message": error.get("message") if isinstance(error, dict) else None,
        "response": payload.get("response"),
        "apis_seen_count": len(payload.get("apis_seen") or []),
    }


def collect_surface(source_dir: Path) -> dict[str, Any]:
    source_dir = source_dir.expanduser().resolve()
    artifacts = iter_json_artifacts(source_dir)
    method_evidence: dict[str, list[dict[str, Any]]] = {}
    setter_candidates: set[str] = set()
    safe_getters: set[str] = set()
    behavior_evidence: dict[str, list[dict[str, Any]]] = {}
    setter_attempt_evidence: dict[str, list[dict[str, Any]]] = {}
    ok_artifacts = 0
    failed_artifacts = 0

    def add_method(method: str, path: Path, payload: dict[str, Any], source: str) -> None:
        evidence = {
            "path": relative_path(path, source_dir),
            "source": source,
            "ok": payload.get("ok"),
            "error_type": payload.get("error_type"),
        }
        if evidence not in method_evidence.setdefault(method, []):
            method_evidence[method].append(evidence)
        if method.startswith(SETTER_PREFIXES):
            setter_candidates.add(method)
        if method.startswith(GETTER_PREFIXES):
            safe_getters.add(method)

    for path, payload in artifacts:
        ok_artifacts += int(payload.get("ok") is True)
        failed_artifacts += int(payload.get("ok") is False)

        behavior_item = _behavior_proof_item(path, payload, source_dir)
        if behavior_item is not None:
            setting_id = str(payload["setting_id"])
            behavior_evidence.setdefault(setting_id, []).append(behavior_item)
            add_method(str(payload["method"]), path, payload, "behavior_proof")

        setter_attempt_item = _setter_attempt_item(path, payload, source_dir)
        if setter_attempt_item is not None:
            setting_id = str(payload["setting_id"])
            setter_attempt_evidence.setdefault(setting_id, []).append(setter_attempt_item)
            if setter_attempt_item["ok"]:
                add_method(str(setter_attempt_item["method"]), path, payload, "setter_attempt")

        for method in extract_api_list(payload):
            add_method(method, path, payload, "api_list")

        method = requested_method(payload)
        if method and payload.get("ok") is True:
            add_method(method, path, payload, "safe_request")

        methods = payload.get("methods")
        if isinstance(methods, list):
            for method in methods:
                add_method(str(method), path, payload, "setter_candidates_file")

        setter_block = payload.get("setter_candidates_after_unlock")
        if isinstance(setter_block, dict) and isinstance(setter_block.get("methods"), list):
            for method in setter_block["methods"]:
                add_method(str(method), path, payload, "summary_setter_candidates")

        safe_block = payload.get("safe_camera_methods_after_unlock")
        if isinstance(safe_block, dict):
            for method, info in safe_block.items():
                if isinstance(info, dict) and info.get("ok") is True:
                    add_method(str(method), path, payload, "summary_safe_getters")

    return {
        "source_dir": str(source_dir),
        "json_artifacts": len(artifacts),
        "ok_artifacts": ok_artifacts,
        "failed_artifacts": failed_artifacts,
        "method_evidence": method_evidence,
        "behavior_evidence": behavior_evidence,
        "setter_attempt_evidence": setter_attempt_evidence,
        "discovered_methods": sorted(method_evidence),
        "setter_candidates": sorted(setter_candidates),
        "safe_getters": sorted(safe_getters),
    }


def route_summaries(row: dict[str, Any]) -> list[dict[str, Any]]:
    summaries = []
    for route_name in sorted(SMART_REMOTE_ROUTES & set(row.get("routes", {}))):
        route = row["routes"][route_name]
        summaries.append(
            {
                "route": route_name,
                "status": route.get("status"),
                "method": route.get("method"),
                "method_hint": route.get("method_hint"),
                "visible_in": route.get("visible_in"),
                "service": route.get("service"),
                "relation": route.get("relation"),
                "reason": route.get("reason"),
            }
        )
    return summaries


def verdict_for_row(
    row: dict[str, Any],
    matched_setters: list[str],
    matched_getters: list[str],
    matched_other: list[str],
    behavior_items: list[dict[str, Any]],
    setter_attempt_items: list[dict[str, Any]],
    surface: dict[str, Any],
) -> str:
    if any(item.get("behavior_proven") is True for item in behavior_items):
        return "behavior_proven"
    if behavior_items:
        return "behavior_observed_not_proven"
    if setter_attempt_items and any(item.get("ok") is False for item in setter_attempt_items):
        return "setter_attempt_failed"
    if setter_attempt_items:
        return "setter_attempt_needs_observation"
    if not surface["discovered_methods"]:
        return "needs_live_probe_artifact"
    if row["status"] == "dangerous_admin" and (matched_setters or matched_other):
        return "candidate_admin_needs_approval"
    route_methods = {
        route.get(key)
        for route_name in SMART_REMOTE_ROUTES
        for route in [row.get("routes", {}).get(route_name, {})]
        for key in ("method", "method_hint")
        if isinstance(route.get(key), str)
    }
    if route_methods & set(matched_setters):
        return "route_setter_seen_in_artifact"
    if matched_setters:
        return "setter_candidate_seen_in_artifact"
    if route_methods & set(matched_getters):
        return "route_getter_seen_in_artifact"
    if matched_getters:
        return "getter_candidate_seen_in_artifact"
    if matched_other:
        return "service_method_candidate_seen_in_artifact"
    return "needs_manual_review"


def map_registry(surface: dict[str, Any], *, registry: dict[str, Any] | None = None) -> list[dict[str, Any]]:
    method_evidence = surface["method_evidence"]
    behavior_evidence = surface["behavior_evidence"]
    setter_attempt_evidence = surface["setter_attempt_evidence"]
    methods = list(method_evidence)
    mapped = []
    for row in route_rows(registry):
        matched_methods = []
        matched_terms: set[str] = set()
        for method in methods:
            ok, terms = method_matches_row(method, row)
            if ok:
                matched_methods.append(method)
                matched_terms.update(terms)

        setters = sorted(method for method in matched_methods if method.startswith(SETTER_PREFIXES))
        getters = sorted(method for method in matched_methods if method.startswith(GETTER_PREFIXES))
        other = sorted(set(matched_methods) - set(setters) - set(getters))
        behavior_items = behavior_evidence.get(row["setting_id"], [])
        setter_attempt_items = setter_attempt_evidence.get(row["setting_id"], [])
        evidence = [
            {
                "method": method,
                "artifacts": method_evidence.get(method, [])[:8],
            }
            for method in sorted(matched_methods)
        ]
        mapped.append(
            {
                "setting_id": row["setting_id"],
                "label": row["label"],
                "registry_status": row["status"],
                "routes": route_summaries(row),
                "candidate_methods": row_candidate_methods(row),
                "matched_methods": sorted(matched_methods),
                "matched_setters": setters,
                "matched_getters": getters,
                "matched_service_methods": other,
                "matched_terms": sorted(matched_terms),
                "evidence": evidence,
                "behavior_evidence": behavior_items,
                "setter_attempt_evidence": setter_attempt_items,
                "evidence_count": len(evidence) + len(behavior_items) + len(setter_attempt_items),
                "verdict": verdict_for_row(
                    row,
                    setters,
                    getters,
                    other,
                    behavior_items,
                    setter_attempt_items,
                    surface,
                ),
                "next_action": row["next_action"],
            }
        )
    return mapped


def build_report(
    source_dir: Path,
    *,
    out_dir: Path | None = None,
    registry: dict[str, Any] | None = None,
) -> tuple[dict[str, Any], Path]:
    source_dir = source_dir.expanduser().resolve()
    if not source_dir.exists():
        raise FileNotFoundError(f"Smart Remote artifact directory not found: {source_dir}")

    out_dir = out_dir or REPO_ROOT / "artifacts" / "falsification" / "r1_api_surface_review"
    out_dir.mkdir(parents=True, exist_ok=True)
    stamp = time.strftime("%Y%m%d-%H%M%S")
    report_path = out_dir / f"{stamp}-smart-remote-review.json"

    surface = collect_surface(source_dir)
    mapping = map_registry(surface, registry=registry)
    mapped_candidate_rows = [
        row for row in mapping if row["verdict"] not in {"needs_manual_review", "needs_live_probe_artifact"}
    ]
    setter_rows = [row for row in mapping if row["matched_setters"]]
    getter_rows = [row for row in mapping if row["matched_getters"]]
    behavior_rows = [row for row in mapping if row["behavior_evidence"]]
    behavior_proven_rows = [row for row in mapping if row["verdict"] == "behavior_proven"]
    setter_attempt_rows = [row for row in mapping if row["setter_attempt_evidence"]]
    setter_attempt_failed_rows = [row for row in mapping if row["verdict"] == "setter_attempt_failed"]

    report = {
        "generated_at": stamp,
        "source_dir": str(source_dir),
        "artifact_summary": {
            "json_artifacts": surface["json_artifacts"],
            "ok_artifacts": surface["ok_artifacts"],
            "failed_artifacts": surface["failed_artifacts"],
            "discovered_method_count": len(surface["discovered_methods"]),
            "setter_candidate_count": len(surface["setter_candidates"]),
            "safe_getter_count": len(surface["safe_getters"]),
            "behavior_proof_count": sum(len(items) for items in surface["behavior_evidence"].values()),
            "behavior_proven_count": sum(
                1
                for items in surface["behavior_evidence"].values()
                for item in items
                if item.get("behavior_proven") is True
            ),
            "setter_attempt_count": sum(
                len(items) for items in surface["setter_attempt_evidence"].values()
            ),
            "setter_attempt_failed_count": sum(
                1
                for items in surface["setter_attempt_evidence"].values()
                for item in items
                if item.get("ok") is False
            ),
        },
        "registry_mapping_summary": {
            "smart_remote_rows": len(mapping),
            "mapped_candidate_rows": len(mapped_candidate_rows),
            "setter_candidate_rows": len(setter_rows),
            "getter_candidate_rows": len(getter_rows),
            "behavior_observed_rows": len(behavior_rows),
            "behavior_proven_rows": len(behavior_proven_rows),
            "setter_attempt_rows": len(setter_attempt_rows),
            "setter_attempt_failed_rows": len(setter_attempt_failed_rows),
            "needs_manual_review_rows": sum(
                1 for row in mapping if row["verdict"] == "needs_manual_review"
            ),
            "needs_live_probe_artifact_rows": sum(
                1 for row in mapping if row["verdict"] == "needs_live_probe_artifact"
            ),
        },
        "discovered_methods": surface["discovered_methods"],
        "setter_candidates_after_unlock": surface["setter_candidates"],
        "safe_getter_methods": surface["safe_getters"],
        "registry_mapping": mapping,
    }
    report_path.write_text(json.dumps(report, indent=2, sort_keys=True), encoding="utf-8")
    return report, report_path


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "source_dir",
        nargs="?",
        default="artifacts/falsification/r1_api_surface",
        help="directory containing falsify_smart_remote_api_surface.py JSON artifacts",
    )
    parser.add_argument(
        "--out-dir",
        default="artifacts/falsification/r1_api_surface_review",
        help="directory for the registry-mapped review report",
    )
    args = parser.parse_args(argv)

    report, report_path = build_report(Path(args.source_dir), out_dir=Path(args.out_dir))
    print(json.dumps({
        "report": str(report_path),
        "artifact_summary": report["artifact_summary"],
        "registry_mapping_summary": report["registry_mapping_summary"],
    }, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
