#!/usr/bin/env python3
"""Map raw Sony PTP focus artifacts back to A7 II registry rows.

This reviews local artifacts produced by:

- `tools/falsify_raw_ptp_focus.sh`
- `tools/falsify_usb_af_point_behavior.sh`
- `tools/falsify_usb_focus_behavior.sh`

It does not talk to the camera and it does not edit the registry. Raw PTP protocol acceptance is
not enough for product truth; behavior artifacts and a visual proof marker are required before a
row can be considered behavior-proven.
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

WALL_TO_SETTING = {
    "W1": "camera.focus_area",
    "W2": "camera.focus_settings",
}
SETTING_TO_BEHAVIOR_DIR_TOKEN = {
    "camera.focus_area": "af-point-behavior",
    "camera.focus_settings": "focus-behavior",
}
SUMMARY_RE = re.compile(
    r"\b(?P<wall>W[12])\s*:\s*ResponseCode\s+0x(?P<code>[0-9a-fA-F]{4}).*?->\s*(?P<verdict>[A-Z_]+)"
)


def raw_rows(registry: dict[str, Any] | None = None) -> list[dict[str, Any]]:
    registry = registry or settings_registry.load_registry()
    return [row for row in registry["settings"] if "raw_ptp" in row.get("routes", {})]


def read_text(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""


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


def collect_protocol_evidence(files: list[Path], roots: list[Path]) -> dict[str, list[dict[str, Any]]]:
    evidence: dict[str, list[dict[str, Any]]] = {}
    for path in files:
        if path.suffix.lower() not in {".log", ".txt"}:
            continue
        text = read_text(path)
        if "ResponseCode" not in text and "SELF-CHECK" not in text:
            continue
        self_check_pass = "[SELF-CHECK] PASS" in text
        for match in SUMMARY_RE.finditer(text):
            wall = match.group("wall")
            setting_id = WALL_TO_SETTING.get(wall)
            if not setting_id:
                continue
            item = {
                "artifact": relative_path(path, roots),
                "wall": wall,
                "response_code": f"0x{match.group('code').lower()}",
                "protocol_verdict": match.group("verdict"),
                "self_check_pass": self_check_pass,
            }
            if item not in evidence.setdefault(setting_id, []):
                evidence[setting_id].append(item)
    return evidence


def _image_exists(path_value: str | None) -> bool:
    if not path_value:
        return False
    return Path(path_value).exists()


def collect_behavior_evidence(files: list[Path], roots: list[Path]) -> dict[str, list[dict[str, Any]]]:
    evidence: dict[str, list[dict[str, Any]]] = {}
    for path in files:
        name = path.name
        if name == "af-point-metrics.json":
            payload = read_json(path) or {}
            files_block = payload.get("files") if isinstance(payload.get("files"), dict) else {}
            item = {
                "artifact": relative_path(path, roots),
                "behavior_type": "af_point",
                "metrics_present": True,
                "preview_files": files_block,
                "preview_files_exist": all(_image_exists(str(value)) for value in files_block.values()),
                "full_frame_mean_abs_diff": payload.get("full_frame_mean_abs_diff"),
                "roi_sharpness_gradient_energy": payload.get("roi_sharpness_gradient_energy"),
                "interpretation": payload.get("interpretation"),
            }
            evidence.setdefault("camera.focus_area", []).append(item)
        elif name == "image-metrics.json":
            payload = read_json(path) or {}
            files_block = payload.get("files") if isinstance(payload.get("files"), dict) else {}
            item = {
                "artifact": relative_path(path, roots),
                "behavior_type": "manual_focus",
                "metrics_present": True,
                "preview_files": files_block,
                "preview_files_exist": all(_image_exists(str(value)) for value in files_block.values()),
                "diff": payload.get("diff"),
                "sharpness_gradient_energy": payload.get("sharpness_gradient_energy"),
                "interpretation": payload.get("interpretation"),
            }
            evidence.setdefault("camera.focus_settings", []).append(item)
        elif name in {"behavior-proof.json", "visual-proof.json", "human-proof.json"}:
            payload = read_json(path) or {}
            setting_id = str(payload.get("setting_id") or "")
            if setting_id in WALL_TO_SETTING.values():
                item = {
                    "artifact": relative_path(path, roots),
                    "behavior_type": payload.get("behavior_type"),
                    "human_verdict": payload.get("human_verdict"),
                    "behavior_proven": payload.get("behavior_proven") is True,
                    "observed_behavior": payload.get("observed_behavior"),
                    "target_setup": payload.get("target_setup"),
                    "source_artifacts": payload.get("source_artifacts"),
                    "copied_evidence": payload.get("copied_evidence"),
                    "notes": payload.get("notes"),
                    "reviewer": payload.get("reviewer"),
                }
                evidence.setdefault(setting_id, []).append(item)
    return evidence


def collect_evidence(source_dirs: list[Path]) -> dict[str, Any]:
    roots = [source_dir.expanduser().resolve() for source_dir in source_dirs]
    files = iter_files(roots)
    return {
        "roots": [str(root) for root in roots],
        "files": len(files),
        "protocol": collect_protocol_evidence(files, roots),
        "behavior": collect_behavior_evidence(files, roots),
    }


def protocol_accepted(items: list[dict[str, Any]]) -> bool:
    return any(
        item.get("protocol_verdict") == "HONORED" and item.get("self_check_pass") is True
        for item in items
    )


def behavior_metrics_present(items: list[dict[str, Any]]) -> bool:
    return any(item.get("metrics_present") is True for item in items)


def behavior_proven(items: list[dict[str, Any]]) -> bool:
    return any(
        item.get("behavior_proven") is True
        or str(item.get("human_verdict", "")).lower() in {"proven", "pass", "accepted"}
        for item in items
    )


def behavior_rejected(items: list[dict[str, Any]]) -> bool:
    return any(
        str(item.get("human_verdict", "")).lower() in {"rejected", "reject", "failed", "fail", "no-op", "noop"}
        for item in items
    )


def behavior_inconclusive(items: list[dict[str, Any]]) -> bool:
    return any(
        str(item.get("human_verdict", "")).lower() in {"inconclusive", "unclear", "needs_rerun"}
        for item in items
    )


def verdict_for_row(protocol_items: list[dict[str, Any]], behavior_items: list[dict[str, Any]], saw_files: bool) -> str:
    if protocol_accepted(protocol_items) and behavior_proven(behavior_items):
        return "behavior_proven"
    if protocol_accepted(protocol_items) and behavior_rejected(behavior_items):
        return "behavior_rejected"
    if protocol_accepted(protocol_items) and behavior_inconclusive(behavior_items):
        return "behavior_inconclusive"
    if protocol_accepted(protocol_items) and behavior_metrics_present(behavior_items):
        return "behavior_metrics_need_visual_review"
    if protocol_accepted(protocol_items):
        return "protocol_accepted_needs_behavior"
    if protocol_items:
        return "protocol_not_accepted_or_selfcheck_failed"
    if behavior_rejected(behavior_items):
        return "behavior_rejected_without_protocol_acceptance"
    if behavior_inconclusive(behavior_items):
        return "behavior_inconclusive_without_protocol_acceptance"
    if behavior_metrics_present(behavior_items):
        return "behavior_metrics_without_protocol_acceptance"
    if saw_files:
        return "raw_ptp_artifacts_do_not_cover_row"
    return "needs_live_raw_ptp_artifact"


def map_registry(evidence: dict[str, Any], *, registry: dict[str, Any] | None = None) -> list[dict[str, Any]]:
    mapped = []
    for row in raw_rows(registry):
        setting_id = row["setting_id"]
        route = row["routes"]["raw_ptp"]
        protocol_items = evidence["protocol"].get(setting_id, [])
        behavior_items = evidence["behavior"].get(setting_id, [])
        mapped.append(
            {
                "setting_id": setting_id,
                "label": row["label"],
                "registry_status": row["status"],
                "route_status": route.get("status"),
                "propcode": route.get("propcode"),
                "expected_behavior_artifact": SETTING_TO_BEHAVIOR_DIR_TOKEN.get(setting_id),
                "protocol_evidence": protocol_items,
                "behavior_evidence": behavior_items,
                "evidence_count": len(protocol_items) + len(behavior_items),
                "verdict": verdict_for_row(protocol_items, behavior_items, saw_files=bool(evidence["files"])),
                "next_action": row["next_action"],
            }
        )
    return mapped


def build_report(
    source_dirs: list[Path],
    *,
    out_dir: Path | None = None,
    registry: dict[str, Any] | None = None,
) -> tuple[dict[str, Any], Path]:
    if not source_dirs:
        source_dirs = [REPO_ROOT / "artifacts" / "falsification" / "r2_usb_ptp"]
    evidence = collect_evidence(source_dirs)
    mapping = map_registry(evidence, registry=registry)
    by_verdict: dict[str, int] = {}
    for row in mapping:
        verdict = row["verdict"]
        by_verdict[verdict] = by_verdict.get(verdict, 0) + 1

    out_dir = out_dir or REPO_ROOT / "artifacts" / "falsification" / "r2_raw_ptp_review"
    out_dir.mkdir(parents=True, exist_ok=True)
    stamp = time.strftime("%Y%m%d-%H%M%S")
    report_path = out_dir / f"{stamp}-raw-ptp-review.json"
    report = {
        "generated_at": stamp,
        "source_dirs": evidence["roots"],
        "artifact_summary": {
            "files": evidence["files"],
            "protocol_settings_with_evidence": len(evidence["protocol"]),
            "behavior_settings_with_evidence": len(evidence["behavior"]),
        },
        "registry_mapping_summary": {
            "raw_ptp_rows": len(mapping),
            "behavior_proven_rows": by_verdict.get("behavior_proven", 0),
            "protocol_accepted_needs_behavior_rows": by_verdict.get(
                "protocol_accepted_needs_behavior", 0
            ),
            "needs_live_raw_ptp_artifact_rows": by_verdict.get("needs_live_raw_ptp_artifact", 0),
            "by_verdict": dict(sorted(by_verdict.items())),
        },
        "registry_mapping": mapping,
    }
    report_path.write_text(json.dumps(report, indent=2, sort_keys=True), encoding="utf-8")
    return report, report_path


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "source_dir",
        nargs="*",
        help="raw PTP artifact root(s). Defaults to artifacts/falsification/r2_usb_ptp.",
    )
    parser.add_argument(
        "--out-dir",
        default="artifacts/falsification/r2_raw_ptp_review",
        help="directory for the registry-mapped raw PTP review report",
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
