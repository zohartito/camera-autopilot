#!/usr/bin/env python3
"""Report remaining A7 II hardware, state, admin, and physical gates with exact commands."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
BACKEND = REPO_ROOT / "backend"
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))

import settings_remaining_gates

build_report = settings_remaining_gates.build_report
_markdown = settings_remaining_gates.to_markdown


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--json", action="store_true")
    parser.add_argument("--out", help="write report to path; .json gets JSON, otherwise Markdown")
    parser.add_argument("--quiet", action="store_true", help="write --out without printing the full report")
    parser.add_argument("--setting", dest="setting_id", help="limit to one registry setting_id")
    parser.add_argument("--menu-group", help="limit to one registry menu_group")
    parser.add_argument("--status", help="limit to one registry status")
    parser.add_argument(
        "--gate-category",
        choices=["dangerous_admin_guarded", "physical_or_guided", "route_gap", "state_gated"],
        help="limit to one remaining-gate category",
    )
    parser.add_argument("--limit", type=int, help="limit returned gate rows after filtering")
    args = parser.parse_args(argv)

    report = build_report(
        setting_id=args.setting_id,
        menu_group=args.menu_group,
        status=args.status,
        gate_category=args.gate_category,
        limit=args.limit,
    )
    output = json.dumps(report, indent=2, sort_keys=True) if args.json else _markdown(report)
    if args.out:
        out = Path(args.out)
        out.parent.mkdir(parents=True, exist_ok=True)
        if out.suffix.lower() == ".json":
            out.write_text(json.dumps(report, indent=2, sort_keys=True), encoding="utf-8")
        else:
            out.write_text(_markdown(report), encoding="utf-8")
    if not args.quiet:
        print(output)
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
