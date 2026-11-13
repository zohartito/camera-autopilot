"""Batch runner for remaining A7 II live evidence route sessions.

The per-route session runner creates audited manifests for one route. This module adds the outer
operator loop: run every remaining route in priority order, dry-run-first, and stop on the first
unresolved live/package gate unless explicitly told to continue.
"""

from __future__ import annotations

import json
import shlex
import time
from pathlib import Path
from typing import Any

import settings_acceptance
import settings_closure
import settings_registry
import settings_session_runs

REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_OUT_DIR = REPO_ROOT / "artifacts" / "falsification" / "live_marathon"
TERMINAL_OUTCOMES = {"session_complete"}


def _slug(value: str) -> str:
    return "".join(ch if ch.isalnum() or ch in "._-" else "_" for ch in value).strip("_") or "batch"


def _batch_command(
    *,
    routes: list[str] | None,
    execute: bool,
    allow_hardware: bool,
    continue_on_block: bool,
    out_dir: Path | None,
    session_out_dir: Path | None,
    package_path: Path | None,
) -> str:
    parts = ["python3", "tools/a7ii_live_evidence_marathon.py"]
    for route in routes or []:
        parts.extend(["--route", route])
    if execute:
        parts.append("--execute")
    if allow_hardware:
        parts.append("--allow-hardware")
    if continue_on_block:
        parts.append("--continue-on-block")
    if out_dir:
        parts.extend(["--out-dir", str(out_dir)])
    if session_out_dir:
        parts.extend(["--session-out-dir", str(session_out_dir)])
    if package_path:
        parts.extend(["--package-path", str(package_path)])
    return " ".join(shlex.quote(part) for part in parts)


def _session_command(route: str, *, execute: bool, allow_hardware: bool, package_path: Path | None) -> str:
    parts = [
        "python3",
        "tools/a7ii_live_session_runner.py",
        "--route",
        route,
        "--recommended-sequence",
    ]
    if execute:
        parts.append("--execute")
    if allow_hardware:
        parts.append("--allow-hardware")
    if route == "camera_remote_command":
        parts.extend(["--package-path", str(package_path or "/path/to/CameraRemoteCommand-package.zip")])
    return " ".join(shlex.quote(part) for part in parts)


def _route_queue(
    *,
    registry: dict[str, Any],
    routes: list[str] | None,
    package_path: Path | None,
) -> list[dict[str, Any]]:
    acceptance = settings_acceptance.build_report(registry=registry, package_path=str(package_path) if package_path else None)
    queue = acceptance["next_action_queue"]
    if not routes:
        return queue
    wanted = set(routes)
    return [item for item in queue if item["route"] in wanted]


def _validate_routes(routes: list[str] | None) -> None:
    if not routes:
        return
    unknown = sorted(set(routes) - set(settings_closure.ROUTE_ORDER))
    if unknown:
        raise ValueError(f"unknown route(s): {', '.join(unknown)}")


def _summarize_route_entries(entries: list[dict[str, Any]]) -> dict[str, Any]:
    outcomes: dict[str, int] = {}
    statuses: dict[str, int] = {}
    for entry in entries:
        status = entry["status"]
        statuses[status] = statuses.get(status, 0) + 1
        outcome = entry.get("outcome") or status
        outcomes[outcome] = outcomes.get(outcome, 0) + 1
    unresolved = [
        entry
        for entry in entries
        if entry["status"] in {"needs_attention", "skipped_after_prior_unresolved"}
    ]
    next_commands: list[str] = []
    for entry in entries:
        for command in entry.get("next_commands") or []:
            if command not in next_commands:
                next_commands.append(command)
    first_unresolved = unresolved[0] if unresolved else None
    return {
        "routes": len(entries),
        "sessions_created": sum(1 for entry in entries if entry.get("manifest_path")),
        "statuses": dict(sorted(statuses.items())),
        "outcomes": dict(sorted(outcomes.items())),
        "complete": statuses.get("complete", 0),
        "planned": statuses.get("planned", 0),
        "needs_attention": statuses.get("needs_attention", 0),
        "skipped": statuses.get("skipped_after_prior_unresolved", 0),
        "first_unresolved_route": first_unresolved.get("route") if first_unresolved else None,
        "first_unresolved_outcome": first_unresolved.get("outcome") if first_unresolved else None,
        "next_commands": next_commands,
    }


def _markdown(report: dict[str, Any]) -> str:
    summary = report["summary"]
    lines = [
        "# A7 II Live Evidence Marathon",
        "",
        f"- execute: `{report['execute']}`",
        f"- allow_hardware: `{report['allow_hardware']}`",
        f"- continue_on_block: `{report['continue_on_block']}`",
        f"- manifest: `{report['manifest_path']}`",
        f"- session_out_dir: `{report['session_out_dir']}`",
        f"- routes: `{summary['routes']}`",
        f"- sessions_created: `{summary['sessions_created']}`",
        f"- complete: `{summary['complete']}`",
        f"- planned: `{summary['planned']}`",
        f"- needs_attention: `{summary['needs_attention']}`",
        f"- skipped: `{summary['skipped']}`",
        f"- first_unresolved_route: `{summary.get('first_unresolved_route') or ''}`",
        "",
    ]
    if summary.get("next_commands"):
        lines.extend(["## Next Commands", ""])
        for command in summary["next_commands"]:
            lines.extend(["```bash", command, "```", ""])
    lines.extend(["## Routes", ""])
    for entry in report["routes"]:
        lines.append(f"### {entry['rank']}. {entry['label']}")
        lines.append("")
        lines.append(f"- route: `{entry['route']}`")
        lines.append(f"- status: `{entry['status']}`")
        lines.append(f"- outcome: `{entry.get('outcome') or ''}`")
        if entry.get("manifest_path"):
            lines.append(f"- manifest: `{entry['manifest_path']}`")
        if entry.get("summary_path"):
            lines.append(f"- summary: `{entry['summary_path']}`")
        if entry.get("note"):
            lines.append(f"- note: {entry['note']}")
        if entry.get("command"):
            lines.extend(["", "Session command:", "", "```bash", entry["command"], "```"])
        if entry.get("next_commands"):
            lines.extend(["", "Next:", ""])
            for command in entry["next_commands"]:
                lines.extend(["```bash", command, "```", ""])
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def run_marathon(
    *,
    routes: list[str] | None = None,
    execute: bool = False,
    allow_hardware: bool = False,
    continue_on_block: bool = False,
    out_dir: Path | None = None,
    session_out_dir: Path | None = None,
    package_path: Path | None = None,
    registry: dict[str, Any] | None = None,
    command_runner: settings_session_runs.CommandRunner | None = None,
    readiness_tool_resolver: settings_session_runs.ToolResolver | None = None,
    readiness_path_exists: settings_session_runs.PathExists | None = None,
) -> dict[str, Any]:
    _validate_routes(routes)
    registry = registry or settings_registry.load_registry()
    queue = _route_queue(registry=registry, routes=routes, package_path=package_path)
    requested_routes = [item["route"] for item in queue]
    stamp = time.strftime("%Y%m%d-%H%M%S") + f"-{time.time_ns() % 1_000_000_000:09d}"
    batch_dir = (out_dir or DEFAULT_OUT_DIR) / f"{stamp}-{'_'.join(_slug(route) for route in requested_routes) or 'no_routes'}"
    session_root = session_out_dir or batch_dir / "sessions"
    route_entries: list[dict[str, Any]] = []
    stopped_after: str | None = None

    for item in queue:
        route = item["route"]
        command = _session_command(
            route,
            execute=execute,
            allow_hardware=allow_hardware,
            package_path=package_path,
        )
        if stopped_after and execute and not continue_on_block:
            route_entries.append(
                {
                    "rank": item["rank"],
                    "route": route,
                    "label": item["label"],
                    "status": "skipped_after_prior_unresolved",
                    "outcome": None,
                    "note": f"Skipped because {stopped_after} needs attention.",
                    "command": command,
                    "manifest_path": None,
                    "summary_path": None,
                    "next_commands": [command],
                }
            )
            continue
        manifest = settings_session_runs.run_session(
            route=route,
            recommended_sequence=True,
            execute=execute,
            allow_hardware=allow_hardware,
            out_dir=session_root,
            package_path=package_path,
            registry=registry,
            command_runner=command_runner,
            readiness_tool_resolver=readiness_tool_resolver,
            readiness_path_exists=readiness_path_exists,
        )
        aftermath = manifest.get("aftermath") or {}
        outcome = aftermath.get("outcome")
        if not execute:
            status = "planned"
        elif outcome in TERMINAL_OUTCOMES:
            status = "complete"
        else:
            status = "needs_attention"
            stopped_after = route
        route_entries.append(
            {
                "rank": item["rank"],
                "route": route,
                "label": item["label"],
                "status": status,
                "outcome": outcome,
                "note": aftermath.get("note"),
                "command": command,
                "manifest_path": manifest.get("manifest_path"),
                "summary_path": manifest.get("summary_path"),
                "session_dir": manifest.get("session_dir"),
                "summary": manifest.get("summary"),
                "next_commands": aftermath.get("next_commands") or [],
            }
        )

    summary = _summarize_route_entries(route_entries)
    report = {
        "timestamp": stamp,
        "execute": execute,
        "allow_hardware": allow_hardware,
        "continue_on_block": continue_on_block,
        "routes_requested": requested_routes,
        "command": _batch_command(
            routes=routes,
            execute=execute,
            allow_hardware=allow_hardware,
            continue_on_block=continue_on_block,
            out_dir=out_dir,
            session_out_dir=session_out_dir,
            package_path=package_path,
        ),
        "batch_dir": str(batch_dir),
        "session_out_dir": str(session_root),
        "manifest_path": str(batch_dir / "marathon-manifest.json"),
        "summary_path": str(batch_dir / "marathon-summary.md"),
        "summary": summary,
        "routes": route_entries,
    }
    batch_dir.mkdir(parents=True, exist_ok=True)
    Path(report["manifest_path"]).write_text(json.dumps(report, indent=2, sort_keys=True), encoding="utf-8")
    Path(report["summary_path"]).write_text(_markdown(report), encoding="utf-8")
    return report


def render_markdown(report: dict[str, Any]) -> str:
    return _markdown(report)
