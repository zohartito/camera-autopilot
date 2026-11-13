#!/usr/bin/env python3
"""Print or run registry-derived proof commands for A7 II settings gaps.

Default mode is dry and camera-safe: it only prints the commands. Use --execute only when the camera
is in the named prerequisite state and you want to produce hardware artifacts.
"""

from __future__ import annotations

import argparse
import json
import shlex
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
BACKEND = REPO_ROOT / "backend"
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))

import settings_probe_plan
import settings_registry


def _print_text(report: dict) -> None:
    summary = report["summary"]
    print(
        "A7 II proof plan: "
        f"{summary['probe_commands']} commands cover "
        f"{summary['covered_gap_rows']}/{summary['selected_gap_rows']} selected gap rows"
    )
    if summary.get("truncated_probe_commands"):
        print(
            f"Showing {summary['displayed_probe_commands']} commands "
            f"({summary['truncated_probe_commands']} hidden by --limit)."
        )
    if summary["missing_gap_rows"]:
        print("Missing command coverage:")
        for setting_id in summary["missing_gap_rows"]:
            print(f"  - {setting_id}")
    print()
    for command in report["commands"]:
        print(f"- {command['probe_id']} [{command['route']}] {command['title']}")
        print(f"  settings: {command['setting_count']}  artifacts: {command['artifact_root']}")
        print(f"  prerequisite: {command['prerequisite']}")
        print(f"  proves: {command['proves']}")
        print(f"  risk: {command['risk']}")
        print(f"  command: {command['command']}")


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--setting", help="single setting_id to plan, e.g. camera.raw_file_type")
    parser.add_argument("--menu-group")
    parser.add_argument("--status", choices=sorted(settings_registry.STATUSES))
    parser.add_argument(
        "--route",
        choices=[
            "smart_remote_wifi",
            "smart_remote_phone",
            "usb_gphoto",
            "raw_ptp",
            "camera_remote_command",
            "openmemories",
            "guided_physical",
        ],
    )
    parser.add_argument("--json", action="store_true", help="print machine-readable JSON")
    parser.add_argument("--out", help="also write the JSON report to this path")
    parser.add_argument("--execute", action="store_true", help="run the selected commands")
    parser.add_argument("--limit", type=int, default=0, help="limit commands after filtering")
    args = parser.parse_args(argv)

    registry = settings_registry.load_registry()
    report = settings_probe_plan.build_probe_report(
        registry,
        setting_id=args.setting,
        menu_group=args.menu_group,
        status=args.status,
        route=args.route,
    )
    if args.limit > 0:
        original_count = len(report["commands"])
        report["commands"] = report["commands"][: args.limit]
        report["summary"]["displayed_probe_commands"] = len(report["commands"])
        report["summary"]["truncated_probe_commands"] = max(0, original_count - len(report["commands"]))

    if args.out:
        settings_probe_plan.write_probe_report(args.out, report)
    if args.json:
        print(json.dumps(report, indent=2, sort_keys=True))
    else:
        _print_text(report)

    if not args.execute:
        return 0

    failed = 0
    for command in report["commands"]:
        print(f"\n== {command['probe_id']} ==")
        try:
            argv = shlex.split(command["command"])
        except ValueError as exc:
            print(f"FAILED {command['probe_id']} invalid quoting: {exc}")
            failed = 1
            continue
        if not argv:
            print(f"FAILED {command['probe_id']} empty command")
            failed = 1
            continue
        proc = subprocess.run(argv, cwd=REPO_ROOT, check=False)
        if proc.returncode != 0:
            print(f"FAILED {command['probe_id']} exit={proc.returncode}")
            failed = 1
    return failed


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
