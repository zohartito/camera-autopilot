#!/usr/bin/env python3
"""Report registry promotion candidates from latest A7 II route-review artifacts."""

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


def _markdown(report: dict) -> str:
    summary = report["summary"]
    lines = [
        "# A7 II Registry Promotion Plan",
        "",
        "This report does not edit the registry. It shows which reviewed artifacts are strong enough "
        "to promote and which routes still need behavior proof.",
        "",
        "## Summary",
        "",
        f"- reviewed_candidates: `{summary['reviewed_candidates']}`",
        f"- unfiltered_reviewed_candidates: `{summary['unfiltered_reviewed_candidates']}`",
        f"- applicable_promotions: `{summary['applicable_promotions']}`",
        f"- guarded_candidates: `{summary['guarded_candidates']}`",
        f"- missing_review_sources: `{', '.join(summary['missing_review_sources']) or 'none'}`",
        "",
        "Actions:",
        "",
    ]
    for action, count in summary["actions"].items():
        lines.append(f"- `{action}`: {count}")
    lines.extend(["", "## Candidates", ""])
    for item in report["promotions"]:
        lines.append(f"### {item['setting_id']} via {item['route']}")
        lines.append("")
        lines.append(f"- verdict: `{item['verdict']}`")
        lines.append(f"- action: `{item['action']}`")
        lines.append(f"- can_apply: `{item['can_apply']}`")
        lines.append(f"- current: `{item['current_status']}` / `{item['current_route_status']}`")
        lines.append(f"- recommended: `{item['recommended_status']}` / `{item['recommended_route_status']}`")
        lines.append(f"- reason: {item['reason']}")
        if item.get("review_artifact"):
            lines.append(f"- review_artifact: `{item['review_artifact']}`")
        if item.get("registry_patch"):
            lines.append("")
            lines.append("Patch intent:")
            lines.append("")
            lines.append("```json")
            lines.append(json.dumps(item["registry_patch"], indent=2, sort_keys=True))
            lines.append("```")
        if item.get("next_commands"):
            lines.append("")
            lines.append("Next proof command:")
            lines.append("")
            lines.append("```bash")
            lines.append(item["next_commands"][0]["command"])
            lines.append("```")
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--route", help="limit to one route, e.g. usb_gphoto")
    parser.add_argument("--setting", help="limit to one setting_id")
    parser.add_argument("--action", help="limit to one planner action, e.g. promote_to_proven_set")
    parser.add_argument("--applicable-only", action="store_true", help="show only applicable promotions")
    parser.add_argument("--limit", type=int, help="limit displayed candidates after filters")
    parser.add_argument("--json", action="store_true", help="print machine-readable JSON")
    parser.add_argument("--out", help="write report to path; .json writes JSON, otherwise Markdown")
    args = parser.parse_args(argv)

    report = settings_promotions.build_promotion_plan(
        route=args.route,
        setting_id=args.setting,
        action=args.action,
        applicable_only=args.applicable_only,
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
    print(output)
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
