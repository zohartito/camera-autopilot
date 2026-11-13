#!/usr/bin/env python3
"""Write a requirement-level completion audit for A7 II full settings control."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
BACKEND = REPO_ROOT / "backend"
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))

import settings_completion_audit


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--package-path", help="local Sony Camera Remote Command package path for readiness")
    parser.add_argument("--json", action="store_true")
    parser.add_argument("--out", help="write report to path; .json writes JSON, otherwise Markdown")
    parser.add_argument("--quiet", action="store_true", help="with --out, write the file without echoing the report")
    parser.add_argument(
        "--limit-commands",
        type=int,
        default=3,
        help="limit next commands shown per requirement in Markdown; 0 shows all",
    )
    args = parser.parse_args(argv)
    if args.quiet and not args.out:
        parser.error("--quiet requires --out")

    report = settings_completion_audit.build_report(package_path=args.package_path)
    output = (
        json.dumps(report, indent=2, sort_keys=True)
        if args.json
        else settings_completion_audit.to_markdown(report, limit_commands=args.limit_commands)
    )
    if args.out:
        out = Path(args.out)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(
            json.dumps(report, indent=2, sort_keys=True)
            if out.suffix.lower() == ".json"
            else settings_completion_audit.to_markdown(report, limit_commands=args.limit_commands),
            encoding="utf-8",
        )
    if not args.quiet:
        print(output)
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
