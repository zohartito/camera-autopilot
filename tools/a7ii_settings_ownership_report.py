#!/usr/bin/env python3
"""Report per-setting ownership, blockers, and exact next proof commands."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
BACKEND = REPO_ROOT / "backend"
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))

import settings_ownership
import settings_registry


def _visible_commands(commands: list[dict], limit: int) -> list[dict]:
    return commands if limit <= 0 else commands[:limit]


def _markdown(report: dict, *, command_limit: int = 2) -> str:
    summary = report["summary"]
    lines = [
        "# A7 II Settings Ownership",
        "",
        f"- total_registry_settings: `{summary['total_registry_settings']}`",
        f"- selected_settings: `{summary['selected_settings']}`",
        f"- settings_needing_evidence: `{summary['settings_needing_evidence']}`",
        f"- settings_with_exact_next_commands: `{summary['settings_with_exact_next_commands']}`",
        f"- unowned_settings: `{len(summary['unowned_settings'])}`",
        f"- missing_exact_next_command_settings: `{len(summary['missing_exact_next_command_settings'])}`",
        f"- greyed_settings: `{summary['greyed_settings']}`",
        "",
        "## Ownership States",
        "",
    ]
    for state, count in summary["ownership_states"].items():
        lines.append(f"- `{state}`: `{count}`")

    lines.extend(["", "## Settings", ""])
    for item in report["settings"]:
        lines.append(f"### {item['setting_id']} - {item['label']}")
        lines.append("")
        lines.append(f"- status: `{item['status']}`")
        lines.append(f"- ownership_state: `{item['ownership_state']}`")
        lines.append(f"- menu: `{item['menu_group']} / {item['menu_page']}`")
        lines.append(f"- current_photo_value: `{item['current_photo_value']}`")
        lines.append(f"- greyed_in_photo: `{item['greyed_in_photo']}`")
        lines.append(f"- needs_evidence: `{item['needs_evidence']}`")
        lines.append(f"- has_exact_next_command: `{item['has_exact_next_command']}`")
        if item["blockers"]:
            lines.append(f"- blockers: `{', '.join(item['blockers'])}`")
        if item["missing_review_routes"]:
            lines.append(f"- missing_review_routes: `{', '.join(item['missing_review_routes'])}`")
        if item["proof_artifacts"]:
            lines.append(f"- proof_artifacts: `{', '.join(item['proof_artifacts'])}`")
        lines.append(f"- next_action: {item['next_action']}")
        if item.get("recommended_sequence_session_command"):
            lines.append(
                "- recommended_sequence_session_command: "
                f"`{item['recommended_sequence_session_command']}`"
            )
        if item.get("next_concrete_command"):
            lines.append(f"- next_concrete_command: `{item['next_concrete_command']}`")

        if item["route_candidates"]:
            routes = ", ".join(
                f"{route['route']}={route['status']}" for route in item["route_candidates"]
            )
            lines.append(f"- routes: `{routes}`")
        gates = item["precondition_solution"]["gates"]
        if gates:
            gates_text = ", ".join(f"{gate['gate_type']}" for gate in gates)
            lines.append(f"- gates: `{gates_text}`")

        commands = item.get("proof_commands") or []
        if commands:
            lines.extend(["", "Proof commands:", ""])
            visible = _visible_commands(commands, command_limit)
            for command in visible:
                lines.append(f"#### {command['probe_id']}")
                lines.append("")
                lines.append(f"- route: `{command['route']}`")
                lines.append(f"- risk: `{command['risk']}`")
                lines.append(f"- artifacts: `{command['artifact_root']}`")
                lines.extend(["", "```bash", command["command"], "```", ""])
            hidden = len(commands) - len(visible)
            if hidden > 0:
                lines.append(f"_Plus {hidden} more proof command(s); increase --limit-commands to show them._")
                lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--setting-id")
    parser.add_argument("--menu-group")
    parser.add_argument("--status", choices=sorted(settings_registry.STATUSES))
    parser.add_argument("--route")
    parser.add_argument("--greyed-only", action="store_true")
    parser.add_argument("--limit", type=int, default=0)
    parser.add_argument("--limit-commands", type=int, default=2)
    parser.add_argument("--json", action="store_true")
    parser.add_argument("--out", help="write report to path; .json writes JSON, otherwise Markdown")
    parser.add_argument("--quiet", action="store_true", help="with --out, write the file without echoing the report")
    args = parser.parse_args(argv)
    if args.quiet and not args.out:
        parser.error("--quiet requires --out")

    report = settings_ownership.build_report(
        setting_id=args.setting_id,
        menu_group=args.menu_group,
        status=args.status,
        route=args.route,
        greyed_only=args.greyed_only,
        limit=args.limit,
    )
    output = (
        json.dumps(report, indent=2, sort_keys=True)
        if args.json
        else _markdown(report, command_limit=args.limit_commands)
    )
    if args.out:
        out = Path(args.out)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(
            json.dumps(report, indent=2, sort_keys=True)
            if out.suffix.lower() == ".json"
            else _markdown(report, command_limit=args.limit_commands),
            encoding="utf-8",
        )
    if not args.quiet:
        print(output)
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
