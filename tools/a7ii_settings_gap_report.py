#!/usr/bin/env python3
"""Print the A7 II settings gap report from the canonical registry.

Examples:
  python3 tools/a7ii_settings_gap_report.py
  python3 tools/a7ii_settings_gap_report.py --status needs_probe
  python3 tools/a7ii_settings_gap_report.py --menu-group camera_settings --limit 20
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
BACKEND = REPO_ROOT / "backend"
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))

import settings_probe_plan
import settings_registry


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--status", choices=sorted(settings_registry.STATUSES))
    parser.add_argument("--menu-group")
    parser.add_argument("--limit", type=int, default=0)
    parser.add_argument("--commands", action="store_true", help="append exact probe commands")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)

    registry = settings_registry.load_registry()
    rows = settings_registry.list_settings(
        menu_group=args.menu_group,
        status=args.status,
        registry=registry,
    )
    rows = sorted(rows, key=lambda row: (row["status"], row["menu_group"], row["setting_id"]))
    if args.limit > 0:
        rows = rows[: args.limit]

    command_report = settings_probe_plan.build_probe_report(
        registry,
        menu_group=args.menu_group,
        status=args.status,
    )

    if args.json:
        payload = {"summary": settings_registry.registry_summary(registry), "settings": rows}
        if args.commands:
            payload["probe_plan"] = command_report
        print(json.dumps(payload, indent=2))
        return 0

    summary = settings_registry.registry_summary(registry)
    print(f"A7 II settings registry: {summary['total_settings']} settings")
    print("Status counts:")
    for status, count in summary["by_status"].items():
        if count:
            print(f"  {status}: {count}")
    print()
    print("Rows:")
    for row in rows:
        grey = " greyed" if row["greyed_in_photo"] else ""
        print(f"- {row['setting_id']} [{row['status']}{grey}] {row['label']}")
        print(f"  page: {row['menu_page']}  value: {row['current_photo_value']!r}")
        print(f"  next: {row['next_action']}")
    if args.commands:
        print()
        print("Probe commands:")
        for command in command_report["commands"]:
            print(f"- {command['probe_id']} [{command['route']}]")
            print(f"  covers: {command['setting_count']} settings  artifacts: {command['artifact_root']}")
            print(f"  command: {command['command']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
