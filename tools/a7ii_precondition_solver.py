#!/usr/bin/env python3
"""Explain how to unlock greyed/state-gated A7 II settings from the registry."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
BACKEND = REPO_ROOT / "backend"
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))

import settings_preconditions
import settings_registry


def _print_text(report: dict) -> None:
    summary = report["summary"]
    print(
        "A7 II precondition solver: "
        f"{summary['settings']} settings, "
        f"{summary['greyed_settings']} greyed, "
        f"{summary['physical_only_settings']} physical-only"
    )
    print("Gate counts:")
    for gate_type, count in summary["gate_counts"].items():
        print(f"  {gate_type}: {count}")
    print()
    for item in report["settings"]:
        print(f"- {item['setting_id']} [{item['status']}] {item['label']}")
        print(f"  greyed: {item['greyed_in_photo']}  physical_only: {item['physical_only']}")
        for gate in item["gates"]:
            print(f"  gate: {gate['gate_type']} from {gate['precondition']!r}")
            print(f"    action: {gate['action']}")
        print("  proof:")
        for command in item["proof_commands"]:
            print(f"    {command}")


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--setting", help="single setting_id")
    parser.add_argument("--status", choices=sorted(settings_registry.STATUSES))
    parser.add_argument("--greyed-only", action="store_true")
    parser.add_argument("--gated-only", action="store_true")
    parser.add_argument("--json", action="store_true")
    parser.add_argument("--out", help="write JSON report to path")
    parser.add_argument("--limit", type=int, default=0)
    args = parser.parse_args(argv)

    registry = settings_registry.load_registry()
    if args.setting:
        report = {
            "summary": {
                "settings": 1,
                "greyed_settings": 0,
                "physical_only_settings": 0,
                "route_disruptive_settings": 0,
                "gate_counts": {},
            },
            "settings": [settings_preconditions.solve_setting(args.setting, registry)],
        }
        item = report["settings"][0]
        report["summary"]["greyed_settings"] = int(item["greyed_in_photo"])
        report["summary"]["physical_only_settings"] = int(item["physical_only"])
        report["summary"]["route_disruptive_settings"] = int(item["route_disruptive"])
        for gate in item["gates"]:
            counts = report["summary"]["gate_counts"]
            counts[gate["gate_type"]] = counts.get(gate["gate_type"], 0) + 1
    else:
        report = settings_preconditions.build_precondition_report(
            registry,
            status=args.status,
            greyed_only=args.greyed_only,
            gated_only=args.gated_only,
        )
    if args.limit > 0:
        report["settings"] = report["settings"][: args.limit]
        report["summary"]["displayed_settings"] = len(report["settings"])

    if args.out:
        Path(args.out).write_text(json.dumps(report, indent=2, sort_keys=True), encoding="utf-8")
    if args.json:
        print(json.dumps(report, indent=2, sort_keys=True))
    else:
        _print_text(report)
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
