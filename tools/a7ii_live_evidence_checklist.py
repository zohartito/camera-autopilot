#!/usr/bin/env python3
"""Write an operator checklist for the next A7 II live evidence sessions."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
BACKEND = REPO_ROOT / "backend"
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))

import settings_live_checklist
import settings_session_ledger


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--route", help="limit to one route, e.g. smart_remote_wifi")
    parser.add_argument("--package-path", help="local Sony Camera Remote Command package path for readiness")
    parser.add_argument(
        "--session-out-dir",
        help="directory containing audited live-session manifests; defaults to the canonical artifact directory",
    )
    parser.add_argument("--json", action="store_true")
    parser.add_argument("--out", help="write report to path; .json writes JSON, otherwise Markdown")
    parser.add_argument("--quiet", action="store_true", help="with --out, write the file without echoing the report")
    parser.add_argument(
        "--limit-direct-commands",
        type=int,
        default=3,
        help="limit direct evidence commands shown per route; 0 shows all",
    )
    args = parser.parse_args(argv)
    if args.quiet and not args.out:
        parser.error("--quiet requires --out")

    session_ledger = (
        settings_session_ledger.build_ledger(out_dir=Path(args.session_out_dir))
        if args.session_out_dir
        else None
    )
    report = settings_live_checklist.build_report(
        route=args.route,
        package_path=args.package_path,
        direct_command_limit=args.limit_direct_commands,
        session_ledger=session_ledger,
    )
    output = (
        json.dumps(report, indent=2, sort_keys=True)
        if args.json
        else settings_live_checklist.to_markdown(report)
    )
    if args.out:
        out = Path(args.out)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(
            json.dumps(report, indent=2, sort_keys=True)
            if out.suffix.lower() == ".json"
            else settings_live_checklist.to_markdown(report),
            encoding="utf-8",
        )
    if not args.quiet:
        print(output)
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
