#!/usr/bin/env python3
"""Report which camera-settings photos are registry rows vs route/context evidence."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "backend"))

import settings_photo_inventory


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-dir", help="override the configured source photo directory")
    parser.add_argument("--inventory", help="alternate inventory JSON path")
    parser.add_argument("--json", action="store_true", help="emit JSON instead of Markdown")
    parser.add_argument("--out", help="write report to this path")
    parser.add_argument("--quiet", action="store_true", help="do not print when --out is provided")
    parser.add_argument("--limit", type=int, default=80, help="record limit for Markdown; 0 means all")
    args = parser.parse_args()

    inventory = settings_photo_inventory.load_inventory(args.inventory) if args.inventory else None
    report = settings_photo_inventory.build_report(
        inventory=inventory,
        source_dir=args.source_dir,
    )
    if args.json:
        rendered = json.dumps(report, indent=2, sort_keys=True) + "\n"
    else:
        rendered = settings_photo_inventory.to_markdown(report, limit_records=args.limit)

    if args.out:
        out = Path(args.out)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(rendered, encoding="utf-8")
    if not args.quiet or not args.out:
        print(rendered, end="")
    # A checked-in inventory deliberately has no local photo directory.  This
    # is a successful privacy posture, not an incomplete scan; an operator can
    # supply an explicit untracked directory when they need reconciliation.
    acceptable = report["summary"]["coverage_complete"] or (
        report["summary"]["status"] == "source_dir_missing"
        and report["summary"]["non_registry_sources"] == 0
    )
    return 0 if acceptable else 1


if __name__ == "__main__":
    raise SystemExit(main())
