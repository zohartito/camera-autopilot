#!/usr/bin/env python3
"""Summarize latest audited A7 II live-session outcomes by route."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
BACKEND = REPO_ROOT / "backend"
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))

import settings_session_ledger


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out-dir", default="artifacts/falsification/live_sessions")
    parser.add_argument("--route")
    parser.add_argument("--setting")
    parser.add_argument("--limit-recent", type=int, default=20)
    parser.add_argument("--json", action="store_true")
    parser.add_argument("--out", help="write report to this file")
    parser.add_argument("--quiet", action="store_true")
    args = parser.parse_args(argv)

    report = settings_session_ledger.build_ledger(
        out_dir=Path(args.out_dir),
        route=args.route,
        setting_id=args.setting,
        limit_recent=args.limit_recent,
    )
    text = (
        json.dumps(report, indent=2, sort_keys=True) + "\n"
        if args.json
        else settings_session_ledger.render_markdown(report)
    )
    if args.out:
        Path(args.out).parent.mkdir(parents=True, exist_ok=True)
        Path(args.out).write_text(text, encoding="utf-8")
    if not args.quiet:
        print(text, end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
