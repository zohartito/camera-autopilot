#!/usr/bin/env python3
"""Map USB/gphoto proof artifacts back to A7 II registry rows.

This reviews local artifacts from:

- `tools/a7ii_usb_state_matrix.py`
- `tools/a7ii_usb_set_read_revert.py`
- `tools/a7ii_usb_movie_record_proof.py`

It never talks to the camera and it never edits the registry. It produces a conservative evidence
report so rows can be promoted only after saved artifacts prove writability, readback, and revert.
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


def usb_rows(registry: dict[str, Any] | None = None) -> list[dict[str, Any]]:
    registry = registry or settings_registry.load_registry()
    return [
        row
        for row in registry["settings"]
        if row.get("routes", {}).get("usb_gphoto", {}).get("node")
    ]


def iter_json_artifacts(source_dirs: list[Path]) -> list[tuple[Path, dict[str, Any]]]:
    artifacts = []
    for source_dir in source_dirs:
        if not source_dir.exists():
            continue
        if source_dir.is_file():
            payload = read_json(source_dir)
            if payload is not None:
                artifacts.append((source_dir.resolve(), payload))
            continue
        for path in sorted(source_dir.rglob("*.json")):
            payload = read_json(path)
            if payload is not None:
                artifacts.append((path.resolve(), payload))
    return artifacts


def _result_evidence(result: dict[str, Any], artifact: Path, roots: list[Path]) -> dict[str, Any]:
    before = result.get("before") if isinstance(result.get("before"), dict) else {}
    after_set = result.get("after_set") if isinstance(result.get("after_set"), dict) else {}
    after_revert = result.get("after_revert") if isinstance(result.get("after_revert"), dict) else {}
    return {
        "artifact": relative_path(artifact, roots),
        "setting_id": result.get("setting_id"),
        "node": result.get("node"),
        "ok": result.get("ok"),
        "skipped": result.get("skipped"),
        "error": result.get("error"),
        "set_ok": result.get("set_ok"),
        "revert_ok": result.get("revert_ok"),
        "before_value": before.get("value"),
        "target": result.get("target"),
        "after_set_value": after_set.get("value"),
        "after_revert_value": after_revert.get("value"),
        "readonly": before.get("readonly"),
    }


def _state_evidence(
    state: str,
    node: str,
    node_result: dict[str, Any],
    artifact: Path,
    roots: list[Path],
) -> dict[str, Any]:
    return {
        "artifact": relative_path(artifact, roots),
        "state": state,
        "node": node,
        "presence": node_result.get("presence"),
        "writability": node_result.get("writability"),
        "value": node_result.get("value"),
    }


def _movie_record_action_evidence(payload: dict[str, Any], artifact: Path, roots: list[Path]) -> dict[str, Any]:
    return {
        "artifact": relative_path(artifact, roots),
        "setting_id": payload.get("setting_id"),
        "node": payload.get("node"),
        "operation": payload.get("operation"),
        "ok": payload.get("ok"),
        "verdict": payload.get("verdict"),
        "seconds_requested": payload.get("seconds_requested"),
        "result": payload.get("result"),
        "notes": payload.get("notes"),
    }


def collect_evidence(source_dirs: list[Path]) -> dict[str, Any]:
    roots = [source_dir.expanduser().resolve() for source_dir in source_dirs]
    artifacts = iter_json_artifacts(roots)
    by_setting: dict[str, list[dict[str, Any]]] = {}
    by_node: dict[str, list[dict[str, Any]]] = {}
    state_by_node: dict[str, list[dict[str, Any]]] = {}
    action_by_node: dict[str, list[dict[str, Any]]] = {}
    summaries = {
        "set_read_revert": 0,
        "state_matrix": 0,
        "movie_record_action": 0,
        "movie_record_action_proven": 0,
    }

    for path, payload in artifacts:
        is_movie_record_action = payload.get("operation") == "native_movie_record_start_stop"
        if is_movie_record_action:
            summaries["movie_record_action"] += 1
            if payload.get("ok") is True and payload.get("verdict") == "movie_record_action_proven":
                summaries["movie_record_action_proven"] += 1
            node = str(payload.get("node") or "")
            if node:
                action_by_node.setdefault(node, []).append(
                    _movie_record_action_evidence(payload, path, roots)
                )

        results = payload.get("results")
        if isinstance(results, list):
            summaries["set_read_revert"] += 1
            for item in results:
                if not isinstance(item, dict):
                    continue
                evidence = _result_evidence(item, path, roots)
                setting_id = str(evidence.get("setting_id") or "")
                node = str(evidence.get("node") or "")
                if setting_id:
                    by_setting.setdefault(setting_id, []).append(evidence)
                if node:
                    by_node.setdefault(node, []).append(evidence)

        if (
            not is_movie_record_action
            and isinstance(payload.get("setting_id"), str)
            and isinstance(payload.get("node"), str)
        ):
            evidence = _result_evidence(payload, path, roots)
            by_setting.setdefault(str(payload["setting_id"]), []).append(evidence)
            by_node.setdefault(str(payload["node"]), []).append(evidence)

        states = payload.get("states")
        if isinstance(states, dict):
            summaries["state_matrix"] += 1
            for state, state_result in states.items():
                if not isinstance(state_result, dict):
                    continue
                watch_nodes = state_result.get("watch_nodes")
                if not isinstance(watch_nodes, dict):
                    continue
                for node, node_result in watch_nodes.items():
                    if isinstance(node_result, dict):
                        state_by_node.setdefault(str(node), []).append(
                            _state_evidence(str(state), str(node), node_result, path, roots)
                        )

    return {
        "roots": [str(root) for root in roots],
        "json_artifacts": len(artifacts),
        "summary_counts": summaries,
        "set_read_revert_by_setting": by_setting,
        "set_read_revert_by_node": by_node,
        "state_by_node": state_by_node,
        "action_by_node": action_by_node,
    }


def _is_writable(writability: Any) -> bool:
    value = str(writability or "").strip().lower()
    if not value or "read-only" in value or "readonly" in value:
        return False
    return value in {"writable", "writeable", "readwrite", "read-write", "read/write", "rw"} or "writ" in value


def verdict_for_row(
    set_evidence: list[dict[str, Any]],
    state_evidence: list[dict[str, Any]],
    action_evidence: list[dict[str, Any]],
    *,
    saw_any_usb_artifact: bool,
) -> str:
    if any(
        item.get("ok") is True and item.get("verdict") == "movie_record_action_proven"
        for item in action_evidence
    ):
        return "movie_record_action_proven"
    if any(item.get("ok") is True and item.get("set_ok") is True and item.get("revert_ok") is True for item in set_evidence):
        return "proven_set_read_revert"
    if any(item.get("ok") is False and item.get("skipped") is not True for item in set_evidence):
        return "failed_set_read_revert"
    if any(item.get("skipped") is True and "read-only" in str(item.get("error", "")).lower() for item in set_evidence):
        return "present_read_only_state_gated"
    if any(item.get("skipped") is True and "no alternate choice" in str(item.get("error", "")).lower() for item in set_evidence):
        return "present_needs_explicit_target"
    if any(str(item.get("presence")) == "PRESENT" and _is_writable(item.get("writability")) for item in state_evidence):
        return "present_writable_needs_set_read_revert"
    if any(str(item.get("presence")) == "PRESENT" for item in state_evidence):
        return "present_read_only_or_unknown"
    if any(str(item.get("presence")) == "ABSENT" for item in state_evidence):
        return "absent_in_recorded_states"
    if saw_any_usb_artifact:
        return "node_not_seen_in_artifacts"
    return "needs_live_usb_artifact"


def map_registry(evidence: dict[str, Any], *, registry: dict[str, Any] | None = None) -> list[dict[str, Any]]:
    mapped = []
    saw_any_usb_artifact = bool(evidence["json_artifacts"])
    by_setting = evidence["set_read_revert_by_setting"]
    by_node = evidence["set_read_revert_by_node"]
    state_by_node = evidence["state_by_node"]
    action_by_node = evidence["action_by_node"]
    for row in usb_rows(registry):
        route = row["routes"]["usb_gphoto"]
        node = str(route["node"])
        set_evidence = list(by_setting.get(row["setting_id"], []))
        for item in by_node.get(node, []):
            if item not in set_evidence:
                set_evidence.append(item)
        states = state_by_node.get(node, [])
        actions = action_by_node.get(node, [])
        mapped.append(
            {
                "setting_id": row["setting_id"],
                "label": row["label"],
                "registry_status": row["status"],
                "route_status": route.get("status"),
                "node": node,
                "set_read_revert_evidence": set_evidence,
                "state_matrix_evidence": states,
                "action_evidence": actions,
                "action_evidence_count": len(actions),
                "evidence_count": len(set_evidence) + len(states),
                "verdict": verdict_for_row(
                    set_evidence,
                    states,
                    actions,
                    saw_any_usb_artifact=saw_any_usb_artifact,
                ),
                "next_action": row["next_action"],
            }
        )
    return mapped


def compact_review(report: dict[str, Any]) -> dict[str, Any]:
    """Return the deterministic, commit-safe subset consumed by the product audit."""
    mapping_keys = (
        "setting_id",
        "label",
        "registry_status",
        "route_status",
        "node",
        "action_evidence_count",
        "evidence_count",
        "verdict",
        "next_action",
    )
    return {
        "schema_version": 2,
        "route": "usb_gphoto",
        "generated_at": report["generated_at"],
        "source_group_count": len(report.get("source_dirs") or []),
        "artifact_summary": report["artifact_summary"],
        "registry_mapping_summary": report["registry_mapping_summary"],
        "registry_proven_set_without_set_read_revert": report[
            "registry_proven_set_without_set_read_revert"
        ],
        "registry_mapping": [
            {key: row[key] for key in mapping_keys if key in row}
            for row in report["registry_mapping"]
        ],
        "raw_review_commit_policy": (
            "Timestamped detail is excluded because it contains verbose device-session metadata; "
            "this sanitized setting-level summary is the canonical committed review."
        ),
    }


def build_report(
    source_dirs: list[Path],
    *,
    out_dir: Path | None = None,
    registry: dict[str, Any] | None = None,
) -> tuple[dict[str, Any], Path]:
    if not source_dirs:
        source_dirs = [
            REPO_ROOT / "artifacts" / "falsification" / "r2_usb_state_matrix",
            REPO_ROOT / "artifacts" / "falsification" / "r2_usb_set_read_revert",
            REPO_ROOT / "artifacts" / "falsification" / "r2_usb_movie_record",
        ]
    evidence = collect_evidence(source_dirs)
    mapping = map_registry(evidence, registry=registry)

    out_dir = out_dir or REPO_ROOT / "artifacts" / "falsification" / "r2_usb_gphoto_review"
    out_dir.mkdir(parents=True, exist_ok=True)
    stamp = time.strftime("%Y%m%d-%H%M%S")
    report_path = out_dir / f"{stamp}-usb-gphoto-review.json"

    by_verdict: dict[str, int] = {}
    for row in mapping:
        verdict = row["verdict"]
        by_verdict[verdict] = by_verdict.get(verdict, 0) + 1
    registry_proven_set_without_set_read_revert = [
        {
            "setting_id": row["setting_id"],
            "label": row["label"],
            "node": row["node"],
            "verdict": row["verdict"],
        }
        for row in mapping
        if row.get("route_status") == "proven_set" and row.get("verdict") != "proven_set_read_revert"
    ]

    report = {
        "generated_at": stamp,
        "source_dirs": evidence["roots"],
        "artifact_summary": {
            "json_artifacts": evidence["json_artifacts"],
            "set_read_revert_summaries": evidence["summary_counts"]["set_read_revert"],
            "state_matrix_summaries": evidence["summary_counts"]["state_matrix"],
            "movie_record_action_artifacts": evidence["summary_counts"]["movie_record_action"],
            "movie_record_action_proven": evidence["summary_counts"][
                "movie_record_action_proven"
            ],
        },
        "registry_mapping_summary": {
            "usb_rows": len(mapping),
            "proven_set_read_revert_rows": by_verdict.get("proven_set_read_revert", 0),
            "registry_proven_set_without_set_read_revert_rows": len(
                registry_proven_set_without_set_read_revert
            ),
            "present_writable_needs_set_read_revert_rows": by_verdict.get(
                "present_writable_needs_set_read_revert", 0
            ),
            "needs_live_usb_artifact_rows": by_verdict.get("needs_live_usb_artifact", 0),
            "by_verdict": dict(sorted(by_verdict.items())),
        },
        "registry_proven_set_without_set_read_revert": registry_proven_set_without_set_read_revert,
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
        help=(
            "artifact root(s) to review. Defaults to r2_usb_state_matrix and "
            "r2_usb_set_read_revert plus r2_usb_movie_record."
        ),
    )
    parser.add_argument(
        "--out-dir",
        default="artifacts/falsification/r2_usb_gphoto_review",
        help="directory for the registry-mapped USB review report",
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
