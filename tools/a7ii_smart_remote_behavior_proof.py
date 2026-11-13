#!/usr/bin/env python3
"""Archive observed Smart Remote setter behavior proof.

Smart Remote API-surface artifacts can show that a setter exists, but they cannot prove the camera
actually changed state. This tool writes a per-setting `smart-remote-behavior-proof.json` marker
after an operator reviews a live setter attempt and records the before/after camera value.
"""

from __future__ import annotations

import argparse
import json
import re
import shutil
import sys
import time
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parent.parent
BACKEND = REPO_ROOT / "backend"
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))

import settings_registry

METHOD_PLACEHOLDER = "<Smart Remote setter method>"
REQUESTED_VALUE_PLACEHOLDER = "<value sent>"
OBSERVED_BEFORE_PLACEHOLDER = "<observed before value>"
OBSERVED_AFTER_PLACEHOLDER = "<observed after value>"
OBSERVED_BEHAVIOR_PLACEHOLDER = "<describe camera-visible behavior>"


def _slug(value: str) -> str:
    return re.sub(r"[^A-Za-z0-9_.-]+", "_", value).strip("_") or "smart-remote-proof"


def _repo_path(path: Path) -> str:
    try:
        return str(path.resolve().relative_to(REPO_ROOT))
    except ValueError:
        return str(path)


def _copy_photos(paths: list[str], proof_dir: Path) -> list[str]:
    copied = []
    evidence_dir = proof_dir / "evidence"
    for raw in paths:
        path = Path(raw).expanduser()
        if not path.exists():
            copied.append(f"MISSING:{path}")
            continue
        evidence_dir.mkdir(parents=True, exist_ok=True)
        target = evidence_dir / path.name
        if path.resolve() != target.resolve():
            shutil.copy2(path, target)
        copied.append(_repo_path(target))
    return copied


def _source_artifacts(paths: list[str]) -> list[dict[str, Any]]:
    artifacts = []
    for raw in paths:
        path = Path(raw).expanduser()
        artifacts.append(
            {
                "path": _repo_path(path) if path.exists() else str(path),
                "exists": path.exists(),
                "kind": "directory" if path.is_dir() else "file" if path.is_file() else "missing",
            }
        )
    return artifacts


def _validate_row(setting_id: str, registry: dict[str, Any]) -> dict[str, Any]:
    row = settings_registry.get_setting(setting_id, registry)
    if "smart_remote_wifi" not in row.get("routes", {}):
        raise ValueError(f"{setting_id} is not registered for the Smart Remote Wi-Fi route")
    return row


def _validate_required_text(name: str, value: str, placeholder: str, *, dry_run: bool) -> None:
    if dry_run:
        return
    if not value.strip() or value.strip() == placeholder:
        raise ValueError(f"{name} must be a real observation, not the placeholder")


def _registered_methods(route: dict[str, Any]) -> list[str]:
    out = []
    for key in ("method", "method_hint"):
        method = route.get(key)
        if isinstance(method, str) and method and method not in out:
            out.append(method)
    return out


def build_report(
    *,
    setting_id: str,
    method: str,
    requested_value: str,
    observed_before: str,
    observed_after: str,
    human_verdict: str,
    observed_behavior: str = "",
    source_artifacts: list[str] | None = None,
    photos: list[str] | None = None,
    reviewer: str = "operator",
    notes: list[str] | None = None,
    out_dir: Path | None = None,
    dry_run: bool = False,
    registry: dict[str, Any] | None = None,
) -> tuple[dict[str, Any], Path]:
    registry = registry or settings_registry.load_registry()
    row = _validate_row(setting_id, registry)
    route = row["routes"]["smart_remote_wifi"]
    source_artifacts = source_artifacts or []
    photos = photos or []
    notes = notes or []

    _validate_required_text("--method", method, METHOD_PLACEHOLDER, dry_run=dry_run)
    _validate_required_text(
        "--requested-value",
        requested_value,
        REQUESTED_VALUE_PLACEHOLDER,
        dry_run=dry_run,
    )
    _validate_required_text(
        "--observed-before",
        observed_before,
        OBSERVED_BEFORE_PLACEHOLDER,
        dry_run=dry_run,
    )
    _validate_required_text(
        "--observed-after",
        observed_after,
        OBSERVED_AFTER_PLACEHOLDER,
        dry_run=dry_run,
    )
    if not dry_run and human_verdict == "proven" and not (source_artifacts or photos):
        raise ValueError("--human-verdict proven requires at least one --source-artifact or --photo")

    method = method.strip()
    requested_value = requested_value.strip()
    observed_before = observed_before.strip()
    observed_after = observed_after.strip()
    observed_behavior = observed_behavior.strip()
    registered_methods = _registered_methods(route)
    stamp = f"{time.strftime('%Y%m%d-%H%M%S')}-{time.time_ns() % 1_000_000_000:09d}"
    root = out_dir or REPO_ROOT / "artifacts" / "falsification" / "r1_api_surface"
    proof_dir = root / f"{stamp}-{_slug(setting_id)}-smart-remote-behavior"
    report_path = proof_dir / "smart-remote-behavior-proof.json"
    value_changed = observed_before != observed_after
    after_matches_request = observed_after.lower() == requested_value.lower()

    report = {
        "tool": "a7ii_smart_remote_behavior_proof",
        "timestamp": stamp,
        "setting_id": row["setting_id"],
        "label": row["label"],
        "route": "smart_remote_wifi",
        "method": method,
        "registered_methods": registered_methods,
        "method_matches_registered_route": not registered_methods or method in registered_methods,
        "requested_value": requested_value,
        "observed_before": observed_before,
        "observed_after": observed_after,
        "value_changed": value_changed,
        "after_matches_request": after_matches_request,
        "observed_behavior": observed_behavior,
        "human_verdict": human_verdict,
        "behavior_proven": human_verdict == "proven",
        "reviewer": reviewer,
        "notes": notes,
        "source_artifacts": _source_artifacts(source_artifacts),
        "copied_evidence": [],
        "registry_status": row["status"],
        "route_status": route.get("status"),
        "next_action": row["next_action"],
        "evidence_quality": {
            "has_requested_value": bool(requested_value and requested_value != REQUESTED_VALUE_PLACEHOLDER),
            "has_observed_before": bool(observed_before and observed_before != OBSERVED_BEFORE_PLACEHOLDER),
            "has_observed_after": bool(observed_after and observed_after != OBSERVED_AFTER_PLACEHOLDER),
            "has_observed_behavior": bool(
                observed_behavior and observed_behavior != OBSERVED_BEHAVIOR_PLACEHOLDER
            ),
            "source_artifact_count": len(source_artifacts),
            "photo_count": len(photos),
            "proven_requires_artifact_or_photo": human_verdict != "proven" or bool(source_artifacts or photos),
        },
    }
    if dry_run:
        return report, report_path

    proof_dir.mkdir(parents=True, exist_ok=True)
    report["copied_evidence"] = _copy_photos(photos, proof_dir)
    report_path.write_text(json.dumps(report, indent=2, sort_keys=True), encoding="utf-8")

    lines = [
        f"# Smart Remote Behavior Proof: {row['label']}",
        "",
        f"- setting_id: `{row['setting_id']}`",
        f"- method: `{method}`",
        f"- human_verdict: `{human_verdict}`",
        f"- behavior_proven: `{report['behavior_proven']}`",
        f"- requested_value: `{requested_value}`",
        f"- observed_before: `{observed_before}`",
        f"- observed_after: `{observed_after}`",
        f"- value_changed: `{value_changed}`",
        f"- after_matches_request: `{after_matches_request}`",
        "",
        "## Source Artifacts",
        "",
    ]
    for artifact in report["source_artifacts"] or [{"path": "None", "exists": False, "kind": "none"}]:
        lines.append(f"- `{artifact['path']}` ({artifact['kind']}, exists={artifact['exists']})")
    lines.extend(["", "## Notes", ""])
    for note in notes or ["No operator note provided."]:
        lines.append(f"- {note}")
    if observed_behavior:
        lines.extend(["", "## Observed Behavior", "", observed_behavior])
    lines.extend(["", "## Copied Evidence", ""])
    for path in report["copied_evidence"] or ["None"]:
        lines.append(f"- `{path}`")
    (proof_dir / "summary.md").write_text("\n".join(lines).rstrip() + "\n", encoding="utf-8")
    return report, report_path


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--setting", required=True)
    parser.add_argument("--method", required=True, help="Smart Remote setter called, e.g. setIsoSpeedRate")
    parser.add_argument("--requested-value", required=True, help="value passed to the setter")
    parser.add_argument("--observed-before", required=True, help="camera/app/getter value before the setter")
    parser.add_argument("--observed-after", required=True, help="camera/app/getter value after the setter")
    parser.add_argument(
        "--human-verdict",
        required=True,
        choices=["proven", "inconclusive", "rejected"],
        help="only 'proven' can satisfy Smart Remote behavior proof",
    )
    parser.add_argument(
        "--observed-behavior",
        default="",
        help="optional visible behavior description, such as getter/event or camera menu change",
    )
    parser.add_argument("--source-artifact", action="append", default=[], help="API surface/session artifact reviewed")
    parser.add_argument("--photo", action="append", default=[], help="optional proof screenshot/photo to copy")
    parser.add_argument("--reviewer", default="operator")
    parser.add_argument("--note", action="append", default=[])
    parser.add_argument("--out-dir", default="artifacts/falsification/r1_api_surface")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args(argv)

    try:
        report, report_path = build_report(
            setting_id=args.setting,
            method=args.method,
            requested_value=args.requested_value,
            observed_before=args.observed_before,
            observed_after=args.observed_after,
            human_verdict=args.human_verdict,
            observed_behavior=args.observed_behavior,
            source_artifacts=args.source_artifact,
            photos=args.photo,
            reviewer=args.reviewer,
            notes=args.note,
            out_dir=REPO_ROOT / args.out_dir,
            dry_run=args.dry_run,
        )
    except ValueError as exc:
        parser.error(str(exc))

    if args.dry_run:
        print(json.dumps({"report_path": str(report_path), "report": report}, indent=2, sort_keys=True))
    else:
        print(report_path.parent)
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
