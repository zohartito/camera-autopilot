#!/usr/bin/env python3
"""Report A7 II settings-control acceptance state and next live sessions."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
BACKEND = REPO_ROOT / "backend"
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))

import settings_acceptance


def _visible_commands(commands: list[dict], limit: int) -> list[dict]:
    return commands if limit <= 0 else commands[:limit]


def _is_template_command(command: str | None) -> bool:
    return bool(command and "<" in command and ">" in command)


def _direct_command_block(command: dict) -> list[str]:
    setting_ids = ", ".join(command.get("setting_ids") or [])
    lines = [
        f"#### {command.get('probe_id') or command.get('title')}",
        "",
        f"- risk: `{command.get('risk')}`",
        f"- risk_kind: `{command.get('risk_kind')}`",
        f"- settings: `{command.get('setting_count')}`",
    ]
    if setting_ids:
        lines.append(f"- setting_ids: `{setting_ids}`")
    if command.get("artifact_root"):
        lines.append(f"- artifacts: `{command['artifact_root']}`")
    if command.get("prerequisite"):
        lines.append(f"- prerequisite: {command['prerequisite']}")
    lines.extend(["", "```bash", command["command"], "```", ""])
    return lines


def _markdown(report: dict, *, direct_command_limit: int = 0) -> str:
    summary = report["summary"]
    lines = [
        "# A7 II Settings Acceptance",
        "",
        f"- completion_state: `{summary['completion_state']}`",
        f"- total_settings: `{summary['total_settings']}`",
        f"- selected_gap_rows: `{summary['selected_gap_rows']}`",
        f"- covered_gap_rows: `{summary['covered_gap_rows']}`",
        f"- proof_commands: `{summary['proof_commands']}`",
        f"- next_action_queue: `{summary['next_action_queue']}`",
        f"- ready_to_attempt_routes: `{summary['ready_to_attempt_routes']}`",
        f"- missing_readiness_requirements: `{summary['missing_readiness_requirements']}`",
        f"- session_ledger_sessions: `{summary['session_ledger_sessions']}`",
        f"- routes_with_session_history: `{summary['routes_with_session_history']}`",
        f"- routes_with_ledger_next_command: `{summary['routes_with_ledger_next_command']}`",
        f"- session_ledger_unreadable_manifests: `{summary['session_ledger_unreadable_manifests']}`",
        f"- missing_review_sources: `{', '.join(summary['missing_review_sources']) or 'none'}`",
        f"- applicable_promotions: `{summary['applicable_promotions']}`",
        "",
        "## Requirements",
        "",
    ]
    for item in report["requirements"]:
        lines.append(f"- `{item['requirement']}`: `{item['status']}` - {item['evidence']}")
    if report.get("next_action_queue"):
        lines.extend(["", "## Priority Queue", ""])
        for item in report["next_action_queue"]:
            lines.append(f"### {item['rank']}. {item['label']}")
            lines.append("")
            lines.append(f"- route: `{item['route']}`")
            lines.append(f"- selected_gap_rows: `{item['selected_gap_rows']}`")
            lines.append(f"- direct_evidence_commands: `{item['direct_command_count']}`")
            lines.append(f"- local_status: `{item['local_status']}`")
            lines.append(f"- live_status: `{item['live_status']}`")
            lines.append(f"- ready_to_attempt: `{item['ready_to_attempt']}`")
            lines.append(f"- manual_requirements: `{item['manual_requirements']}`")
            if item["risk_kinds"]:
                risks = ", ".join(f"{name}={count}" for name, count in item["risk_kinds"].items())
                lines.append(f"- risk_kinds: `{risks}`")
            if item["blocked_by"]:
                lines.append(f"- blocked_by: `{', '.join(item['blocked_by'])}`")
            if item["missing_requirements"]:
                missing = ", ".join(
                    f"{requirement['kind']}:{requirement['name']}"
                    for requirement in item["missing_requirements"]
                )
                lines.append(f"- missing_requirements: `{missing}`")
            lines.append(f"- why: {item['why']}")
            if item.get("latest_session"):
                lines.append(f"- latest_session_outcome: `{item['latest_session_outcome']}`")
                lines.append(f"- latest_session_timestamp: `{item['latest_session_timestamp']}`")
                lines.append(f"- latest_session_manifest: `{item['latest_session_manifest_path']}`")
                if item.get("latest_session_setting_id"):
                    lines.append(f"- latest_session_setting_id: `{item['latest_session_setting_id']}`")
            elif item.get("ledger_next_command"):
                lines.append("- latest_session_outcome: `none`")
            if item.get("latest_session_next_command"):
                lines.extend(
                    [
                        "",
                        "Latest session next command:",
                        "",
                        "```bash",
                        item["latest_session_next_command"],
                        "```",
                    ]
                )
            if (
                item.get("ledger_next_command")
                and item["ledger_next_command"]
                not in {
                    item.get("latest_session_next_command"),
                    item.get("recommended_sequence_session_command"),
                }
            ):
                lines.extend(
                    [
                        "",
                        "Ledger-selected next command:",
                        "",
                        "```bash",
                        item["ledger_next_command"],
                        "```",
                    ]
                )
            if (
                item.get("route_recommended_command")
                and item["route_recommended_command"] != item.get("recommended_sequence_session_command")
            ):
                lines.extend(
                    [
                        "",
                        "Route recommended command from ledger:",
                        "",
                        "```bash",
                        item["route_recommended_command"],
                        "```",
                    ]
                )
            if item.get("recommended_command_sequence"):
                lines.extend(["", "Recommended proof sequence:", ""])
                for sequence_step in item["recommended_command_sequence"]:
                    lines.append(
                        f"{sequence_step['rank']}. `{sequence_step['kind']}` - {sequence_step['label']}"
                    )
                    lines.extend(["", "```bash", sequence_step["command"], "```", ""])
            if item.get("recommended_sequence_session_command"):
                lines.extend(
                    [
                        "",
                        "Audited proof-sequence manifest:",
                        "",
                        "```bash",
                        item["recommended_sequence_session_command"],
                        "```",
                    ]
                )
            primary_command = item.get("recommended_next_command") or item.get("readiness_next_action") or item[
                "next_command"
            ]
            primary_kind = item.get("recommended_next_command_kind")
            primary_label = {
                "readiness_prerequisite": "Recommended readiness command",
                "audited_one_row_evidence_session": "Recommended audited one-row command",
                "one_row_direct_evidence": "Recommended one-row proof command",
                "route_direct_evidence": "Recommended route evidence command",
                "route_evidence_session": "Recommended route session",
            }.get(primary_kind, "Recommended next command")
            lines.extend(["", f"{primary_label}:", "", "```bash", primary_command, "```"])
            if item.get("route_prerequisite_command") and item["route_prerequisite_command"] != primary_command:
                lines.extend(
                    [
                        "",
                        "Route prerequisite/probe command:",
                        "",
                        "```bash",
                        item["route_prerequisite_command"],
                        "```",
                    ]
                )
            audited_session = item.get("audited_session_command") or item["next_command"]
            if primary_command != audited_session:
                lines.extend(
                    [
                        "",
                        (
                            "Evidence session after readiness:"
                            if item.get("missing_requirements")
                            else (
                                "Audited one-row session wrapper:"
                                if item.get("audited_one_row_session_command")
                                else "Audited evidence session wrapper:"
                            )
                        ),
                        "",
                        "```bash",
                        audited_session,
                        "```",
                    ]
                )
            if (
                item.get("audited_one_row_session_command")
                and item["next_command"] not in {primary_command, audited_session}
            ):
                label = (
                    "Broad audited route template, not a concrete row command"
                    if _is_template_command(item["next_command"])
                    else "Broad audited route wrapper"
                )
                lines.extend(
                    [
                        "",
                        f"{label}:",
                        "",
                        "```bash",
                        item["next_command"],
                        "```",
                    ]
                )
            if (
                item.get("first_one_row_direct_command")
                and item["first_one_row_direct_command"] != primary_command
                and item["first_one_row_direct_command"] != item.get("route_prerequisite_command")
            ):
                lines.extend(
                    [
                        "",
                        "Direct one-row proof command:",
                        "",
                        "```bash",
                        item["first_one_row_direct_command"],
                        "```",
                    ]
                )
            if item.get("first_direct_command") and item["first_direct_command"] not in {
                primary_command,
                item.get("next_concrete_command"),
                item.get("first_one_row_direct_command"),
            }:
                lines.extend(
                    [
                        "",
                        "First direct command:",
                        "",
                        "```bash",
                        item["first_direct_command"],
                        "```",
                    ]
                )
            if item.get("review_and_refresh_command"):
                lines.extend(
                    [
                        "",
                        "Review/refresh after evidence:",
                        "",
                        "```bash",
                        item["review_and_refresh_command"],
                        "```",
                    ]
                )
            lines.append("")
    lines.extend(["", "## Next Live Sessions", ""])
    for item in report["next_live_sessions"]:
        lines.append(f"### {item['label']}")
        lines.append("")
        lines.append(f"- route: `{item['route']}`")
        lines.append(f"- selected_gap_rows: `{item['selected_gap_rows']}`")
        lines.append(f"- evidence_commands: `{item['evidence_commands']}`")
        lines.append(f"- review_commands: `{item['review_commands']}`")
        lines.append(f"- refresh_commands: `{item['refresh_commands']}`")
        if item.get("latest_session"):
            lines.append(f"- latest_session_outcome: `{item['latest_session_outcome']}`")
            lines.append(f"- latest_session_timestamp: `{item['latest_session_timestamp']}`")
            lines.append(f"- latest_session_manifest: `{item['latest_session_manifest_path']}`")
        elif item.get("ledger_next_command"):
            lines.append("- latest_session_outcome: `none`")
        if item["risk_kinds"]:
            risks = ", ".join(f"{name}={count}" for name, count in item["risk_kinds"].items())
            lines.append(f"- risk_kinds: `{risks}`")
        if item.get("latest_session_next_command"):
            lines.append("")
            lines.append("Latest session next command:")
            lines.append("")
            lines.append("```bash")
            lines.append(item["latest_session_next_command"])
            lines.append("```")
        if item["next_evidence_session_command"]:
            lines.append("")
            lines.append(
                "Route evidence template, not a concrete row command:"
                if _is_template_command(item["next_evidence_session_command"])
                else "Evidence session:"
            )
            lines.append("")
            lines.append("```bash")
            lines.append(item["next_evidence_session_command"])
            lines.append("```")
        if item["review_and_refresh_command"]:
            lines.append("")
            lines.append("Review and refresh:")
            lines.append("")
            lines.append("```bash")
            lines.append(item["review_and_refresh_command"])
            lines.append("```")
        direct_commands = item.get("direct_evidence_commands") or []
        if direct_commands:
            lines.append("")
            lines.append("Direct evidence commands:")
            lines.append("")
            visible = _visible_commands(direct_commands, direct_command_limit)
            for command in visible:
                lines.extend(_direct_command_block(command))
            hidden = len(direct_commands) - len(visible)
            if hidden > 0:
                lines.append(
                    f"_Plus {hidden} more direct evidence command(s); rerun with "
                    "--limit-direct-commands 0 for all._"
                )
        lines.append("")
    if report["blocking_notes"]:
        lines.extend(["## Blocking Notes", ""])
        for note in report["blocking_notes"]:
            lines.append(f"- {note}")
    return "\n".join(lines).rstrip() + "\n"


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--route", help="limit to one route, e.g. usb_gphoto")
    parser.add_argument("--package-path", help="local Sony Camera Remote Command package path for readiness")
    parser.add_argument("--json", action="store_true")
    parser.add_argument("--out", help="write report to path; .json writes JSON, otherwise Markdown")
    parser.add_argument("--quiet", action="store_true", help="with --out, write the file without echoing the report")
    parser.add_argument(
        "--limit-direct-commands",
        type=int,
        default=0,
        help="limit direct evidence commands shown in Markdown; 0 shows all",
    )
    args = parser.parse_args(argv)
    if args.quiet and not args.out:
        parser.error("--quiet requires --out")

    report = settings_acceptance.build_report(route=args.route, package_path=args.package_path)
    output = (
        json.dumps(report, indent=2, sort_keys=True)
        if args.json
        else _markdown(report, direct_command_limit=args.limit_direct_commands)
    )
    if args.out:
        out = Path(args.out)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(
            json.dumps(report, indent=2, sort_keys=True)
            if out.suffix.lower() == ".json"
            else _markdown(report, direct_command_limit=args.limit_direct_commands),
            encoding="utf-8",
        )
    if not args.quiet:
        print(output)
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
