#!/usr/bin/env python3
"""Archive a guided proof artifact for physical, admin, or state-gated A7 II settings.

This does not automate the camera. It records the exact registry row, preconditions, operator
observation, and optional screenshot/photo paths so physical-only gates are explicit rather than
hand-waved.
"""

from __future__ import annotations

import argparse
import json
import re
import shutil
import sys
import time
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
BACKEND = REPO_ROOT / "backend"
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))

import settings_registry

OBSERVED_STATE_PLACEHOLDER = "<describe camera/menu state>"
ADMIN_OBSERVATION_ONLY_HELP = (
    "required for dangerous/admin rows; confirms this proof records only visible state "
    "and did not perform delete, format, reset, network reset, calibration, or other admin action"
)


def _slug(value: str) -> str:
    return re.sub(r"[^A-Za-z0-9_.-]+", "_", value).strip("_") or "setting"


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
        copied.append(str(target))
    return copied


def _is_admin_guarded(row: dict) -> bool:
    if row["status"] == "dangerous_admin":
        return True
    if row["setting_id"] in settings_registry.DESTRUCTIVE_SETTING_IDS:
        return True
    return any(route.get("status") == "dangerous_admin" for route in row.get("routes", {}).values())


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--setting", required=True, help="registry setting_id")
    parser.add_argument("--out-dir", default="artifacts/falsification/manual_gates")
    parser.add_argument("--observed-state", default="", help="what the camera/menu showed")
    parser.add_argument("--evidence-note", action="append", default=[], help="operator note; repeatable")
    parser.add_argument("--photo", action="append", default=[], help="optional screenshot/photo to copy")
    parser.add_argument("--admin-observation-only", action="store_true", help=ADMIN_OBSERVATION_ONLY_HELP)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args(argv)

    registry = settings_registry.load_registry()
    row = settings_registry.get_setting(args.setting, registry)
    admin_guarded = _is_admin_guarded(row)
    has_guided = "guided_physical" in row.get("routes", {})
    if not has_guided and row["status"] not in {"manual_physical_gate", "dangerous_admin", "state_gated"}:
        parser.error(f"{args.setting} is not registered as a physical/admin/state gate")
    observed_state = args.observed_state.strip()
    if not args.dry_run and (
        not observed_state or observed_state == OBSERVED_STATE_PLACEHOLDER
    ):
        parser.error(
            "--observed-state must describe the visible camera/menu state for a non-dry-run proof artifact"
        )
    if admin_guarded and not args.dry_run and not args.admin_observation_only:
        parser.error(
            "--admin-observation-only is required for dangerous/admin rows; this proof must record "
            "menu state only and must not perform delete, format, reset, network reset, calibration, "
            "or other admin actions"
        )

    stamp = time.strftime("%Y%m%d-%H%M%S")
    out_dir = REPO_ROOT / args.out_dir / f"{stamp}-{_slug(args.setting)}"
    gate_class = "dangerous_admin" if admin_guarded else row["status"]

    report = {
        "timestamp": stamp,
        "setting_id": row["setting_id"],
        "label": row["label"],
        "status": row["status"],
        "gate_class": gate_class,
        "admin_guarded": admin_guarded,
        "admin_observation_only": bool(args.admin_observation_only),
        "destructive_action_performed": False,
        "menu_group": row["menu_group"],
        "menu_page": row["menu_page"],
        "current_photo_value": row["current_photo_value"],
        "greyed_in_photo": row["greyed_in_photo"],
        "preconditions": row["preconditions"],
        "routes": row["routes"],
        "next_action": row["next_action"],
        "observed_state": observed_state,
        "evidence_notes": args.evidence_note,
        "copied_evidence": [],
        "evidence_quality": {
            "has_observed_state": bool(observed_state),
            "photo_count": len(args.photo),
            "note_count": len(args.evidence_note),
            "admin_observation_only_ack": bool(args.admin_observation_only) if admin_guarded else None,
        },
        "verdict": (
            "dangerous_admin_observed_no_action"
            if admin_guarded
            else "manual_gate_recorded_not_software_controlled"
        ),
    }

    if args.dry_run:
        print(json.dumps({"out_dir": str(out_dir), "report": report}, indent=2, sort_keys=True))
        return 0

    out_dir.mkdir(parents=True, exist_ok=True)
    report["copied_evidence"] = _copy_evidence(args.photo, out_dir)
    (out_dir / "physical-gate.json").write_text(json.dumps(report, indent=2, sort_keys=True), encoding="utf-8")

    lines = [
        f"# Physical Gate Proof: {row['label']}",
        "",
        f"- setting_id: `{row['setting_id']}`",
        f"- status: `{row['status']}`",
        f"- gate_class: `{gate_class}`",
        f"- admin_guarded: `{admin_guarded}`",
        f"- menu: `{row['menu_group']} / {row['menu_page']}`",
        f"- greyed_in_photo: `{row['greyed_in_photo']}`",
        f"- current_photo_value: `{row['current_photo_value']}`",
        f"- observed_state: `{observed_state}`",
        "",
        "## Preconditions",
        "",
    ]
    for item in row["preconditions"]:
        lines.append(f"- {item}")
    if admin_guarded:
        lines.extend(
            [
                "",
                "## Admin Guardrail",
                "",
                "- This artifact records visible camera/menu state only.",
                "- No destructive/admin action was performed by this proof command.",
                "- Any future write path requires a new explicit approval and rollback plan.",
            ]
        )
    lines.extend(["", "## Next Action", "", row["next_action"], "", "## Evidence Notes", ""])
    for note in args.evidence_note or ["No operator note provided."]:
        lines.append(f"- {note}")
    lines.extend(["", "## Copied Evidence", ""])
    for path in report["copied_evidence"] or ["None"]:
        lines.append(f"- `{path}`")
    (out_dir / "summary.md").write_text("\n".join(lines).rstrip() + "\n", encoding="utf-8")
    print(out_dir)
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
