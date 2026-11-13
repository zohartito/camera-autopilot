#!/usr/bin/env python3
"""Build route-centric live proof playbooks for A7 II settings gaps."""

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
import settings_sessions


def _command_block(command: dict) -> list[str]:
    lines = [
        f"#### {command.get('probe_id') or command['title']}",
        "",
        f"- risk: `{command['risk']}`",
        f"- artifacts: `{command['artifact_root']}`",
    ]
    if command.get("setting_count") is not None:
        lines.append(f"- settings: `{command['setting_count']}`")
    if command.get("prerequisite"):
        lines.append(f"- prerequisite: {command['prerequisite']}")
    if command.get("proves"):
        lines.append(f"- proves: {command['proves']}")
    lines.extend(["", "```bash", command["command"], "```", ""])
    return lines


def _markdown(report: dict, *, command_limit: int) -> str:
    summary = report["summary"]
    lines = [
        "# A7 II Live Route Playbook",
        "",
        "Run a route playbook when the camera is in the matching state. Hardware commands create "
        "artifacts; review commands turn those artifacts into registry evidence; refresh commands "
        "update closure and promotion reports.",
        "",
        "## Summary",
        "",
        f"- routes: `{summary['routes']}`",
        f"- selected_gap_rows: `{summary['selected_gap_rows']}`",
        f"- covered_gap_rows: `{summary['covered_gap_rows']}`",
        f"- missing_gap_rows: `{len(summary['missing_gap_rows'])}`",
        f"- evidence_commands: `{summary['evidence_commands']}`",
        f"- review_commands: `{summary['review_commands']}`",
        f"- refresh_commands: `{summary['refresh_commands']}`",
        f"- ordered_commands: `{summary['ordered_commands']}`",
        "",
    ]
    for route in report["routes"]:
        lines.extend(
            [
                f"## {route['label']}",
                "",
                f"- route: `{route['route']}`",
                f"- selected_gap_rows: `{route['summary']['selected_gap_rows']}`",
                f"- covered_gap_rows: `{route['summary']['covered_gap_rows']}`",
                f"- missing_gap_rows: `{len(route['summary']['missing_gap_rows'])}`",
                f"- ordered_commands: `{route['summary']['ordered_commands']}`",
                "",
            ]
        )
        if route.get("next_concrete_command"):
            lines.extend(
                [
                    "Audited proof-sequence manifest:",
                    "",
                    "```bash",
                    route["recommended_sequence_session_command"],
                    "```",
                    "",
                    "Next concrete evidence command:",
                    "",
                    "```bash",
                    route["next_concrete_command"],
                    "```",
                    "",
                ]
            )
        if route["notes"]:
            lines.append("Notes:")
            lines.append("")
            for note in route["notes"]:
                lines.append(f"- {note}")
            lines.append("")
        if route["prerequisites"]:
            lines.append("Prerequisites:")
            lines.append("")
            for prerequisite in route["prerequisites"]:
                lines.append(f"- {prerequisite}")
            lines.append("")

        for title, key in (
            ("Evidence Commands", "evidence_commands"),
            ("Review Commands", "review_commands"),
            ("Refresh Commands", "refresh_commands"),
        ):
            commands = route[key]
            lines.extend([f"### {title}", ""])
            visible = commands[:command_limit] if command_limit > 0 else commands
            for command in visible:
                lines.extend(_command_block(command))
            hidden = len(commands) - len(visible)
            if hidden > 0:
                lines.append(f"_Plus {hidden} more command(s); rerun with a higher --limit-commands._")
                lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--route", choices=list(settings_closure.ROUTE_ORDER))
    parser.add_argument("--setting", help="limit a route playbook to one setting_id")
    parser.add_argument("--json", action="store_true")
    parser.add_argument("--out", help="write report to path; .json writes JSON, otherwise Markdown")
    parser.add_argument("--limit-commands", type=int, default=4)
    args = parser.parse_args(argv)

    report = settings_sessions.build_session_playbook(
        route=args.route,
        setting_id=args.setting,
    )
    output = json.dumps(report, indent=2, sort_keys=True) if args.json else _markdown(
        report,
        command_limit=args.limit_commands,
    )
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
