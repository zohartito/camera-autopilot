#!/usr/bin/env python3
"""Build an operator-ready closure report for A7 II settings-control gaps."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parent.parent
BACKEND = REPO_ROOT / "backend"
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))

import settings_closure

ROUTE_LABELS = settings_closure.ROUTE_LABELS
ROUTE_ORDER = settings_closure.ROUTE_ORDER
build_report = settings_closure.build_report


def _markdown_commands(commands: list[dict[str, Any]], limit: int) -> list[str]:
    lines = []
    visible = commands[:limit] if limit > 0 else commands
    for command in visible:
        lines.append(f"#### {command['probe_id']}")
        lines.append("")
        lines.append(f"- settings: `{command['setting_count']}`")
        lines.append(f"- artifacts: `{command['artifact_root']}`")
        lines.append(f"- risk: `{command['risk']}`")
        lines.append(f"- prerequisite: {command['prerequisite']}")
        lines.append("")
        lines.append("```bash")
        lines.append(command["command"])
        lines.append("```")
        lines.append("")
    hidden = len(commands) - len(visible)
    if hidden > 0:
        lines.append(f"_Plus {hidden} more command(s); rerun with a higher --limit-commands to list them._")
        lines.append("")
    return lines


def _markdown(report: dict[str, Any], *, command_limit: int) -> str:
    summary = report["summary"]
    lines = [
        "# A7 II Settings Evidence Closure",
        "",
        "This is the live-work closure map: every remaining gap has either a review artifact, "
        "a concrete command to generate one, or a physical/admin gate path.",
        "",
        "## Summary",
        "",
        f"- total_settings: `{summary['total_settings']}`",
        f"- selected_gap_rows: `{summary['selected_gap_rows']}`",
        f"- covered_gap_rows: `{summary['covered_gap_rows']}`",
        f"- proof_commands: `{summary['probe_commands']}`",
        f"- missing_probe_command_rows: `{len(summary['missing_probe_command_rows'])}`",
        f"- loaded_review_sources: `{summary['loaded_review_sources']}`",
        f"- missing_review_sources: `{', '.join(summary['missing_review_sources']) or 'none'}`",
        f"- greyed_settings: `{summary['greyed_settings']}`",
        f"- physical_only_settings: `{summary['physical_only_settings']}`",
        "",
        "## Blocking Notes",
        "",
    ]
    for note in report["blocking_notes"] or ["No blocking notes; run verification before claiming completion."]:
        lines.append(f"- {note}")
    lines.append("")

    lines.extend(["## Route Closure", ""])
    for section in report["routes"]:
        source = section["source_status"] or {
            "status": "not_applicable",
            "artifact": None,
            "label": section["label"],
        }
        lines.append(f"### {section['label']}")
        lines.append("")
        lines.append(f"- source_status: `{source['status']}`")
        if source.get("artifact"):
            lines.append(f"- latest_artifact: `{source['artifact']}`")
        lines.append(f"- reviewed_settings: `{section['reviewed_settings']}`")
        lines.append(f"- missing_review_settings: `{section['missing_review_settings']}`")
        lines.append(f"- gap_setting_count: `{section['gap_setting_count']}`")
        lines.append(f"- command_count: `{len(section['commands'])}`")
        if section["probe_summary"]["missing_gap_rows"]:
            lines.append(
                "- missing_command_rows: "
                + ", ".join(f"`{item}`" for item in section["probe_summary"]["missing_gap_rows"])
            )
        if section["verdict_counts"]:
            lines.append("- verdict_counts:")
            for verdict, count in section["verdict_counts"].items():
                lines.append(f"  - `{verdict}`: {count}")
        if section["gap_setting_sample"]:
            lines.append("- sample_gaps:")
            for row in section["gap_setting_sample"]:
                lines.append(f"  - `{row['setting_id']}` [{row['status']}] {row['label']}")
                lines.append(f"    next: {row['next_action']}")
        lines.append("")
        if section["commands"]:
            lines.append("Commands:")
            lines.append("")
            lines.extend(_markdown_commands(section["commands"], command_limit))

    lines.extend(["## Precondition Solver", ""])
    pre = report["precondition_summary"]
    lines.append(f"- gated_settings: `{pre['settings']}`")
    lines.append(f"- route_disruptive_settings: `{pre['route_disruptive_settings']}`")
    lines.append("- gate_counts:")
    for gate_type, count in pre["gate_counts"].items():
        lines.append(f"  - `{gate_type}`: {count}")
    lines.append("")
    lines.append("```bash")
    lines.append("python3 tools/a7ii_precondition_solver.py --gated-only")
    lines.append("python3 tools/a7ii_precondition_solver.py --greyed-only")
    lines.append("```")
    return "\n".join(lines).rstrip() + "\n"


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--json", action="store_true", help="print machine-readable JSON")
    parser.add_argument("--out", help="write report to path; .json writes JSON, otherwise Markdown")
    parser.add_argument("--limit-settings", type=int, default=8, help="sample gap settings per route")
    parser.add_argument("--limit-commands", type=int, default=4, help="commands to show per route in Markdown")
    args = parser.parse_args(argv)

    report = build_report(sample_limit=args.limit_settings)
    output = json.dumps(report, indent=2, sort_keys=True) if args.json else _markdown(report, command_limit=args.limit_commands)
    if args.out:
        out = Path(args.out)
        out.parent.mkdir(parents=True, exist_ok=True)
        if out.suffix.lower() == ".json":
            out.write_text(json.dumps(report, indent=2, sort_keys=True), encoding="utf-8")
        else:
            out.write_text(_markdown(report, command_limit=args.limit_commands), encoding="utf-8")
    print(output)
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
