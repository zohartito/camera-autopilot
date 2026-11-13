#!/usr/bin/env python3
"""Dry-run or apply behavior-proven A7 II registry promotions."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
BACKEND = REPO_ROOT / "backend"
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))

import settings_promotions
import settings_registry


def _markdown(report: dict) -> str:
    summary = report["summary"]
    lines = [
        "# A7 II Registry Promotion Apply Plan",
        "",
        "Only behavior-proven, unguarded promotion candidates are selected. Dry-run is the default.",
        "",
        "## Summary",
        "",
        f"- dry_run: `{summary['dry_run']}`",
        f"- registry_path: `{summary['registry_path']}`",
        f"- target_path: `{summary['target_path']}`",
        f"- selected_promotions: `{summary['selected_promotions']}`",
        f"- written: `{summary['written']}`",
        f"- missing_review_sources: `{', '.join(summary['missing_review_sources']) or 'none'}`",
        "",
        "## Changes",
        "",
    ]
    if not report["changes"]:
        lines.append("No applicable promotions found.")
    for change in report["changes"]:
        lines.append(f"### {change['setting_id']} via {change['route']}")
        lines.append("")
        lines.append(f"- label: {change['label']}")
        lines.append(f"- action: `{change['action']}`")
        lines.append(f"- verdict: `{change['verdict']}`")
        lines.append(f"- review_artifact: `{change['review_artifact']}`")
        lines.append(
            f"- status: `{change['before']['status']}` -> `{change['after']['status']}`"
        )
        lines.append(
            "- route_status: "
            f"`{change['before']['route_status']}` -> `{change['after']['route_status']}`"
        )
        new_artifacts = [
            artifact
            for artifact in change["after"]["proof_artifacts"]
            if artifact not in change["before"]["proof_artifacts"]
        ]
        if new_artifacts:
            lines.append(f"- append_proof_artifact: `{new_artifacts[0]}`")
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--registry",
        default=str(settings_registry.DEFAULT_REGISTRY_PATH),
        help="registry file to read; defaults to data/a7ii_settings_registry.yaml",
    )
    parser.add_argument("--out", help="with --apply, write a promoted registry copy to this path")
    parser.add_argument(
        "--in-place",
        action="store_true",
        help="with --apply, update the registry file itself instead of writing a copy",
    )
    parser.add_argument("--route", help="limit to one route, e.g. usb_gphoto")
    parser.add_argument("--setting", help="limit to one setting_id")
    parser.add_argument("--action", help="limit to one planner action, e.g. promote_to_proven_set")
    parser.add_argument("--apply", action="store_true", help="write registry updates; otherwise dry-run")
    parser.add_argument("--json", action="store_true", help="print machine-readable JSON")
    args = parser.parse_args(argv)

    if args.in_place and not args.apply:
        parser.error("--in-place is only valid with --apply")
    if args.out and args.in_place:
        parser.error("--out and --in-place are mutually exclusive")
    if args.apply and not args.out and not args.in_place:
        parser.error("--apply requires either --out PATH for copy-write or --in-place for registry write")

    report = settings_promotions.apply_promotion_plan(
        registry_path=Path(args.registry),
        out_path=Path(args.out) if args.out else None,
        route=args.route,
        setting_id=args.setting,
        action=args.action,
        dry_run=not args.apply,
    )
    print(json.dumps(report, indent=2, sort_keys=True) if args.json else _markdown(report))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
