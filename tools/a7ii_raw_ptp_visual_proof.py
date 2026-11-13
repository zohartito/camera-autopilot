#!/usr/bin/env python3
"""Archive visual proof for raw PTP focus behavior.

The raw PTP probes can show protocol acceptance and image metrics, but product truth still needs a
human-visible near/far behavior judgment. This tool writes the `human-proof.json` artifact consumed
by `review_raw_ptp_artifacts.py` and keeps proven/inconclusive/rejected outcomes explicit.
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

OBSERVED_BEHAVIOR_PLACEHOLDER = "<describe near/far visual behavior>"
TARGET_SETUP_PLACEHOLDER = "<describe near/far target setup>"
BEHAVIOR_TYPES = {
    "camera.focus_area": "af_point",
    "camera.focus_settings": "manual_focus",
}


def _slug(value: str) -> str:
    return re.sub(r"[^A-Za-z0-9_.-]+", "_", value).strip("_") or "raw-ptp-proof"


def _repo_path(path: Path) -> str:
    try:
        return str(path.resolve().relative_to(REPO_ROOT))
    except ValueError:
        return str(path)


def _copy_photos(paths: list[str], out_dir: Path) -> list[str]:
    copied = []
    evidence_dir = out_dir / "evidence"
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
    if setting_id not in BEHAVIOR_TYPES or "raw_ptp" not in row.get("routes", {}):
        raise ValueError(f"{setting_id} is not a raw PTP focus behavior row")
    return row


def _validate_required_text(name: str, value: str, placeholder: str, *, dry_run: bool) -> None:
    if dry_run:
        return
    if not value.strip() or value.strip() == placeholder:
        raise ValueError(f"{name} must be a real observation, not the placeholder")


def build_report(
    *,
    setting_id: str,
    human_verdict: str,
    observed_behavior: str,
    target_setup: str,
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
    source_artifacts = source_artifacts or []
    photos = photos or []
    notes = notes or []

    _validate_required_text(
        "--observed-behavior",
        observed_behavior,
        OBSERVED_BEHAVIOR_PLACEHOLDER,
        dry_run=dry_run,
    )
    _validate_required_text(
        "--target-setup",
        target_setup,
        TARGET_SETUP_PLACEHOLDER,
        dry_run=dry_run,
    )
    if not dry_run and human_verdict == "proven" and not (source_artifacts or photos):
        raise ValueError("--human-verdict proven requires at least one --source-artifact or --photo")

    stamp = f"{time.strftime('%Y%m%d-%H%M%S')}-{time.time_ns() % 1_000_000_000:09d}"
    root = out_dir or REPO_ROOT / "artifacts" / "falsification" / "r2_usb_ptp"
    proof_dir = root / f"{stamp}-{_slug(setting_id)}-visual-proof"
    report_path = proof_dir / "human-proof.json"

    report = {
        "tool": "a7ii_raw_ptp_visual_proof",
        "timestamp": stamp,
        "setting_id": row["setting_id"],
        "label": row["label"],
        "behavior_type": BEHAVIOR_TYPES[setting_id],
        "human_verdict": human_verdict,
        "behavior_proven": human_verdict == "proven",
        "observed_behavior": observed_behavior.strip(),
        "target_setup": target_setup.strip(),
        "reviewer": reviewer,
        "notes": notes,
        "source_artifacts": _source_artifacts(source_artifacts),
        "copied_evidence": [],
        "registry_status": row["status"],
        "route_status": row["routes"]["raw_ptp"].get("status"),
        "propcode": row["routes"]["raw_ptp"].get("propcode"),
        "next_action": row["next_action"],
        "evidence_quality": {
            "has_observed_behavior": bool(
                observed_behavior.strip() and observed_behavior.strip() != OBSERVED_BEHAVIOR_PLACEHOLDER
            ),
            "has_target_setup": bool(
                target_setup.strip() and target_setup.strip() != TARGET_SETUP_PLACEHOLDER
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
        f"# Raw PTP Visual Proof: {row['label']}",
        "",
        f"- setting_id: `{row['setting_id']}`",
        f"- behavior_type: `{report['behavior_type']}`",
        f"- human_verdict: `{human_verdict}`",
        f"- behavior_proven: `{report['behavior_proven']}`",
        f"- target_setup: `{report['target_setup']}`",
        f"- observed_behavior: `{report['observed_behavior']}`",
        "",
        "## Source Artifacts",
        "",
    ]
    for artifact in report["source_artifacts"] or [{"path": "None", "exists": False, "kind": "none"}]:
        lines.append(f"- `{artifact['path']}` ({artifact['kind']}, exists={artifact['exists']})")
    lines.extend(["", "## Notes", ""])
    for note in notes or ["No operator note provided."]:
        lines.append(f"- {note}")
    lines.extend(["", "## Copied Evidence", ""])
    for path in report["copied_evidence"] or ["None"]:
        lines.append(f"- `{path}`")
    (proof_dir / "summary.md").write_text("\n".join(lines).rstrip() + "\n", encoding="utf-8")
    return report, report_path


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--setting", required=True, choices=sorted(BEHAVIOR_TYPES))
    parser.add_argument(
        "--human-verdict",
        required=True,
        choices=["proven", "inconclusive", "rejected"],
        help="visual review outcome; only 'proven' can satisfy behavior proof",
    )
    parser.add_argument("--observed-behavior", default="", help="what changed visually in the previews")
    parser.add_argument("--target-setup", default="", help="near/far target and focus setup used for review")
    parser.add_argument("--source-artifact", action="append", default=[], help="raw PTP artifact file/dir reviewed")
    parser.add_argument("--photo", action="append", default=[], help="optional proof screenshot/photo to copy")
    parser.add_argument("--reviewer", default="operator")
    parser.add_argument("--note", action="append", default=[])
    parser.add_argument("--out-dir", default="artifacts/falsification/r2_usb_ptp")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args(argv)

    try:
        report, report_path = build_report(
            setting_id=args.setting,
            human_verdict=args.human_verdict,
            observed_behavior=args.observed_behavior,
            target_setup=args.target_setup,
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
