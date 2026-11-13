#!/usr/bin/env python3
"""Archive operator proof for Smart Remote phone-app preference rows.

This is deliberately not a camera-control script. It records what the phone app showed, which
registry row it belongs to, and optional screenshots/photos. That gives phone-side settings an
owned evidence route without pretending they are camera JSON-RPC setters.
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

OBSERVED_VALUE_PLACEHOLDER = "<visible phone-app value>"
PHONE_STATE_PLACEHOLDER = "<describe phone/app state>"


def _slug(value: str) -> str:
    return re.sub(r"[^A-Za-z0-9_.-]+", "_", value).strip("_") or "phone-setting"


def _repo_path(path: Path) -> str:
    try:
        return str(path.resolve().relative_to(REPO_ROOT))
    except ValueError:
        return str(path)


def _copy_evidence(paths: list[str], out_dir: Path) -> list[str]:
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


def _is_phone_preference_row(row: dict[str, Any]) -> bool:
    return row.get("menu_group") == "smart_remote_phone" or "smart_remote_phone" in row.get("routes", {})


def build_report(
    *,
    setting_id: str,
    observed_value: str,
    phone_state: str,
    evidence_notes: list[str] | None = None,
    photo_paths: list[str] | None = None,
    out_dir: Path | None = None,
    dry_run: bool = False,
    registry: dict[str, Any] | None = None,
) -> tuple[dict[str, Any], Path]:
    registry = registry or settings_registry.load_registry()
    row = settings_registry.get_setting(setting_id, registry)
    if not _is_phone_preference_row(row):
        raise ValueError(f"{setting_id} is not registered as a Smart Remote phone preference row")

    observed_value = observed_value.strip()
    phone_state = phone_state.strip()
    if not dry_run and (not observed_value or observed_value == OBSERVED_VALUE_PLACEHOLDER):
        raise ValueError("--observed-value must record the visible phone-app value")
    if not dry_run and (not phone_state or phone_state == PHONE_STATE_PLACEHOLDER):
        raise ValueError("--phone-state must describe the visible phone/app context")

    stamp = f"{time.strftime('%Y%m%d-%H%M%S')}-{time.time_ns() % 1_000_000_000:09d}"
    root = (out_dir or REPO_ROOT / "artifacts" / "falsification" / "r1_phone_settings_manual")
    proof_dir = root / f"{stamp}-{_slug(setting_id)}"
    report_path = proof_dir / "phone-preference-proof.json"

    report = {
        "tool": "a7ii_phone_preference_proof",
        "timestamp": stamp,
        "setting_id": row["setting_id"],
        "label": row["label"],
        "menu_group": row["menu_group"],
        "menu_page": row["menu_page"],
        "registry_status": row["status"],
        "current_photo_value": row["current_photo_value"],
        "greyed_in_photo": row["greyed_in_photo"],
        "preconditions": row["preconditions"],
        "routes": row["routes"],
        "next_action": row["next_action"],
        "control_surface": "smart_remote_phone_app",
        "api_exposure": "not_proven_by_this_artifact",
        "observed_value": observed_value,
        "phone_state": phone_state,
        "evidence_notes": evidence_notes or [],
        "copied_evidence": [],
        "evidence_quality": {
            "has_observed_value": bool(observed_value and observed_value != OBSERVED_VALUE_PLACEHOLDER),
            "has_phone_state": bool(phone_state and phone_state != PHONE_STATE_PLACEHOLDER),
            "photo_count": len(photo_paths or []),
            "note_count": len(evidence_notes or []),
        },
        "verdict": "phone_app_preference_observed_manual_route",
    }
    if dry_run:
        return report, report_path

    proof_dir.mkdir(parents=True, exist_ok=True)
    report["copied_evidence"] = _copy_evidence(photo_paths or [], proof_dir)
    report_path.write_text(json.dumps(report, indent=2, sort_keys=True), encoding="utf-8")

    lines = [
        f"# Smart Remote Phone Preference Proof: {row['label']}",
        "",
        f"- setting_id: `{row['setting_id']}`",
        f"- menu: `{row['menu_group']} / {row['menu_page']}`",
        f"- registry_status: `{row['status']}`",
        f"- current_photo_value: `{row['current_photo_value']}`",
        f"- observed_value: `{observed_value}`",
        f"- phone_state: `{phone_state}`",
        f"- api_exposure: `{report['api_exposure']}`",
        "",
        "## Notes",
        "",
    ]
    for note in evidence_notes or ["No operator note provided."]:
        lines.append(f"- {note}")
    lines.extend(["", "## Copied Evidence", ""])
    for path in report["copied_evidence"] or ["None"]:
        lines.append(f"- `{path}`")
    lines.extend(["", "## Next Action", "", row["next_action"]])
    (proof_dir / "summary.md").write_text("\n".join(lines).rstrip() + "\n", encoding="utf-8")
    return report, report_path


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--setting", required=True, help="registry setting_id")
    parser.add_argument("--observed-value", default="", help="visible value shown in the phone app")
    parser.add_argument("--phone-state", default="", help="visible phone/app context")
    parser.add_argument("--evidence-note", action="append", default=[], help="operator note; repeatable")
    parser.add_argument("--photo", action="append", default=[], help="optional phone screenshot/photo to copy")
    parser.add_argument("--out-dir", default="artifacts/falsification/r1_phone_settings_manual")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args(argv)

    try:
        report, report_path = build_report(
            setting_id=args.setting,
            observed_value=args.observed_value,
            phone_state=args.phone_state,
            evidence_notes=args.evidence_note,
            photo_paths=args.photo,
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
