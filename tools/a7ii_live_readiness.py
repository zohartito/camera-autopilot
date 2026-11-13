#!/usr/bin/env python3
"""Report local/operator readiness for A7 II live route proof sessions."""

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
import settings_readiness


def _markdown(report: dict) -> str:
    summary = report["summary"]
    lines = [
        "# A7 II Live Readiness",
        "",
        f"- routes: `{summary['routes']}`",
        f"- ready_to_attempt: `{summary['ready_to_attempt']}`",
        f"- missing_requirements: `{summary['missing_requirements']}`",
        f"- manual_requirements: `{summary['manual_requirements']}`",
        f"- selected_gap_rows: `{summary['selected_gap_rows']}`",
        f"- missing_gap_rows: `{len(summary['missing_gap_rows'])}`",
        "",
    ]
    for route in report["routes"]:
        route_summary = route["summary"]
        lines.extend(
            [
                f"## {route['label']}",
                "",
                f"- route: `{route['route']}`",
                f"- local_status: `{route_summary['local_status']}`",
                f"- live_status: `{route_summary['live_status']}`",
                f"- ready_to_attempt: `{route_summary['ready_to_attempt']}`",
                f"- missing_requirements: `{route_summary['missing_requirements']}`",
                f"- manual_requirements: `{route_summary['manual_requirements']}`",
                "",
            ]
        )
        if route["missing_requirements"]:
            lines.append("Missing local/package requirements:")
            lines.append("")
            for item in route["missing_requirements"]:
                lines.append(f"- `{item['name']}` ({item['kind']}): {item['why']}")
                if item.get("last_locator_result"):
                    lines.append(f"  last locator: {item['last_locator_result']}")
                if item.get("last_locator_artifact"):
                    lines.append(f"  artifact: `{item['last_locator_artifact']}`")
                if item.get("next_action"):
                    lines.append(f"  next: {item['next_action']}")
            lines.append("")
        if route["manual_requirements"]:
            lines.append("Operator/camera requirements:")
            lines.append("")
            for item in route["manual_requirements"]:
                lines.append(f"- {item['why']}")
            lines.append("")
        lines.append("Next command:")
        lines.append("")
        lines.append("```bash")
        lines.append(route["recommended_next_action"])
        lines.append("```")
        lines.append("")
        if route.get("recommended_sequence_session_command"):
            lines.append("Audited proof-sequence manifest:")
            lines.append("")
            lines.append("```bash")
            lines.append(route["recommended_sequence_session_command"])
            lines.append("```")
            lines.append("")
        if route.get("next_concrete_command") and route["next_concrete_command"] != route["recommended_next_action"]:
            lines.append("Next concrete evidence command:")
            lines.append("")
            lines.append("```bash")
            lines.append(route["next_concrete_command"])
            lines.append("```")
            lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--route", choices=list(settings_closure.ROUTE_ORDER))
    parser.add_argument("--setting", help="limit readiness to one setting_id")
    parser.add_argument("--package-path", help="local Camera Remote Command package path")
    parser.add_argument("--json", action="store_true")
    parser.add_argument("--out", help="write readiness report to path; .json writes JSON")
    args = parser.parse_args(argv)

    report = settings_readiness.build_readiness_report(
        route=args.route,
        setting_id=args.setting,
        package_path=args.package_path,
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
