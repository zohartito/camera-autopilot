#!/usr/bin/env python3
"""Generate aggregate JSON and Markdown reports for the beginner UX validation study."""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
BACKEND = REPO_ROOT / "backend"
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))

import ux_validation


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Build an aggregate-only 'Is It Magic?' operator report."
    )
    parser.add_argument(
        "--ledger",
        type=Path,
        default=ux_validation.VALIDATION_FILE,
        help="private JSONL ledger (default: ~/.camera-autopilot/ux-validation-sessions.jsonl)",
    )
    parser.add_argument(
        "--study-id",
        default=ux_validation.DEFAULT_STUDY_ID,
        help="preregistered cohort identifier (default: magic-v0)",
    )
    parser.add_argument(
        "--out-dir",
        type=Path,
        default=REPO_ROOT / "artifacts" / "falsification" / "ux_validation",
        help="aggregate report output directory",
    )
    parser.add_argument("--stdout", action="store_true", help="also print the Markdown report")
    args = parser.parse_args(argv)

    report = ux_validation.build_report(path=args.ledger, study_id=args.study_id)
    markdown = ux_validation.report_markdown(report)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    args.out_dir.mkdir(parents=True, exist_ok=True)
    json_path = args.out_dir / f"{stamp}-{args.study_id}-ux-validation-report.json"
    markdown_path = args.out_dir / f"{stamp}-{args.study_id}-ux-validation-report.md"
    json_path.write_text(json.dumps(report, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    markdown_path.write_text(markdown, encoding="utf-8")

    gate = report["gate"]
    print(f"study: {report['study_id']}")
    print(f"gate: {gate['status']}")
    print(f"cohort: {gate['cohort_sessions']}/{gate['cohort_size_required']}")
    print(f"passes: {gate['passes']}/{gate['cohort_sessions']}")
    print(f"json: {json_path}")
    print(f"markdown: {markdown_path}")
    if args.stdout:
        print()
        print(markdown)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
