#!/usr/bin/env python3
"""Plan or run every remaining A7 II live evidence route in priority order."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
BACKEND = REPO_ROOT / "backend"
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))

import settings_closure
import settings_live_marathon


def _print_text(report: dict) -> None:
    summary = report["summary"]
    print(
        "A7 II live evidence marathon: "
        f"{summary['routes']} routes, "
        f"{summary['sessions_created']} sessions, "
        f"{summary['planned']} planned, "
        f"{summary['complete']} complete, "
        f"{summary['needs_attention']} need attention, "
        f"{summary['skipped']} skipped"
    )
    print(f"manifest: {report['manifest_path']}")
    print(f"summary:  {report['summary_path']}")
    print(f"sessions: {report['session_out_dir']}")
    if summary.get("first_unresolved_route"):
        print(f"first unresolved: {summary['first_unresolved_route']} ({summary['first_unresolved_outcome']})")
    for command in summary.get("next_commands") or []:
        print(f"next command: {command}")
    for entry in report["routes"]:
        print(f"- {entry['rank']:02d} {entry['route']} {entry['status']} {entry.get('outcome') or ''}".rstrip())
        if entry.get("manifest_path"):
            print(f"  manifest: {entry['manifest_path']}")
        print(f"  command: {entry['command']}")


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--route", action="append", choices=list(settings_closure.ROUTE_ORDER))
    parser.add_argument("--execute", action="store_true", help="run route sessions instead of only planning")
    parser.add_argument(
        "--allow-hardware",
        action="store_true",
        help="allow camera-touching evidence commands inside executed route sessions",
    )
    parser.add_argument(
        "--continue-on-block",
        action="store_true",
        help="keep attempting later routes after an executed route needs attention",
    )
    parser.add_argument(
        "--out-dir",
        default="artifacts/falsification/live_marathon",
        help="directory for marathon manifests",
    )
    parser.add_argument(
        "--session-out-dir",
        default="artifacts/falsification/live_sessions",
        help="directory for per-route live-session manifests created by the marathon",
    )
    parser.add_argument(
        "--package-path",
        help="local Sony Camera Remote Command package path for Camera Remote Command route sessions",
    )
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)

    report = settings_live_marathon.run_marathon(
        routes=args.route,
        execute=args.execute,
        allow_hardware=args.allow_hardware,
        continue_on_block=args.continue_on_block,
        out_dir=Path(args.out_dir),
        session_out_dir=Path(args.session_out_dir),
        package_path=Path(args.package_path) if args.package_path else None,
    )
    if args.json:
        print(json.dumps(report, indent=2, sort_keys=True))
    else:
        _print_text(report)
    return 1 if report["summary"]["needs_attention"] and args.execute else 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
