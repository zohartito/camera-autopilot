"""Latest audited live-session outcomes for A7 II route proof work."""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

import settings_closure
import settings_registry
import settings_session_runs
import settings_sessions

ACTIVE_ROUTE_ORDER = tuple(
    route for route in settings_closure.ROUTE_ORDER if route not in settings_registry.DISABLED_ROUTES
)


def _display_path(path: Path) -> str:
    resolved = path.expanduser().resolve(strict=False)
    try:
        return resolved.relative_to(settings_registry.REPO_ROOT.resolve()).as_posix()
    except ValueError:
        return resolved.as_posix()


def _manifest_paths(out_dir: Path) -> list[Path]:
    if not out_dir.exists():
        return []
    return sorted(out_dir.glob("*/session-manifest.json"))


def _legacy_aftermath(manifest: dict[str, Any]) -> dict[str, Any]:
    summary = manifest.get("summary") or {}
    if summary.get("planned") == summary.get("commands"):
        outcome = "planned_only"
        note = "Legacy manifest without structured aftermath; session was planned but not executed."
    elif summary.get("failed"):
        outcome = "command_failed"
        note = "Legacy manifest without structured aftermath; at least one command failed."
    elif summary.get("readiness_blocked"):
        outcome = "needs_local_readiness"
        note = "Legacy manifest without structured aftermath; local readiness blocked execution."
    elif summary.get("package_blocked"):
        outcome = "needs_package_path"
        note = "Legacy manifest without structured aftermath; package-path evidence was blocked."
    elif summary.get("hardware_blocked"):
        outcome = "needs_hardware_approval"
        note = "Legacy manifest without structured aftermath; hardware approval blocked execution."
    elif summary.get("observation_blocked"):
        outcome = "needs_operator_observation"
        note = "Legacy manifest without structured aftermath; operator observation blocked execution."
    elif summary.get("executed") == summary.get("commands") and not summary.get("blocked"):
        outcome = "session_complete"
        note = "Legacy manifest without structured aftermath; selected commands executed without blocks."
    else:
        outcome = "mixed_unresolved"
        note = "Legacy manifest without structured aftermath; inspect command statuses directly."
    return {
        "outcome": outcome,
        "note": note,
        "next_commands": [],
        "first_unresolved": None,
        "produced_artifact_roots": [],
        "pending_artifact_roots": [],
    }


def _load_session(path: Path) -> dict[str, Any]:
    manifest = json.loads(path.read_text(encoding="utf-8"))
    aftermath = manifest.get("aftermath") or _legacy_aftermath(manifest)
    summary = manifest.get("summary") or {}
    route = manifest.get("route") or "unknown"
    return {
        "timestamp": manifest.get("timestamp") or path.parent.name,
        "route": route,
        "setting_id": manifest.get("setting_id"),
        "session_dir": manifest.get("session_dir") or str(path.parent),
        "manifest_path": manifest.get("manifest_path") or str(path),
        "summary_path": manifest.get("summary_path"),
        "readiness_path": manifest.get("readiness_path"),
        "phases": manifest.get("phases") or [],
        "recommended_sequence": bool(manifest.get("recommended_sequence")),
        "execute": bool(manifest.get("execute")),
        "allow_hardware": bool(manifest.get("allow_hardware")),
        "outcome": aftermath.get("outcome"),
        "note": aftermath.get("note"),
        "next_commands": aftermath.get("next_commands") or [],
        "first_unresolved": aftermath.get("first_unresolved"),
        "produced_artifact_roots": aftermath.get("produced_artifact_roots") or [],
        "pending_artifact_roots": aftermath.get("pending_artifact_roots") or [],
        "summary": {
            "commands": summary.get("commands", 0),
            "planned": summary.get("planned", 0),
            "executed": summary.get("executed", 0),
            "blocked": summary.get("blocked", 0),
            "failed": summary.get("failed", 0),
            "statuses": summary.get("statuses") or {},
            "phases": summary.get("phases") or {},
        },
    }


def _route_next_command(
    route: str,
    *,
    registry: dict[str, Any],
    setting_id: str | None = None,
) -> str | None:
    try:
        playbook = settings_sessions.build_route_playbook(
            route,
            registry=registry,
            setting_id=setting_id,
        )
    except (ValueError, settings_registry.SettingNotFoundError):
        return None
    return playbook.get("recommended_sequence_session_command") or playbook.get("next_concrete_command")


def _latest_by_route(
    sessions: list[dict[str, Any]],
    *,
    registry: dict[str, Any],
    setting_id: str | None = None,
) -> list[dict[str, Any]]:
    latest: dict[str, dict[str, Any]] = {}
    for session in sessions:
        route = session["route"]
        if route not in latest or session["timestamp"] > latest[route]["timestamp"]:
            latest[route] = session
    rows = []
    for route in ACTIVE_ROUTE_ORDER:
        session = latest.get(route)
        route_command = _route_next_command(route, registry=registry, setting_id=setting_id)
        next_command = None
        if session and session.get("next_commands"):
            next_command = session["next_commands"][0]
        if not next_command:
            next_command = route_command
        rows.append(
            {
                "route": route,
                "has_session": route in latest,
                "latest_session": session,
                "next_command": next_command,
                "latest_session_next_command": (session.get("next_commands") or [None])[0] if session else None,
                "route_recommended_command": route_command,
            }
        )
    for route in sorted(set(latest) - set(ACTIVE_ROUTE_ORDER) - settings_registry.DISABLED_ROUTES):
        session = latest[route]
        rows.append(
            {
                "route": route,
                "has_session": True,
                "latest_session": session,
                "next_command": (session.get("next_commands") or [None])[0],
                "latest_session_next_command": (session.get("next_commands") or [None])[0],
                "route_recommended_command": None,
            }
        )
    return rows


def build_ledger(
    *,
    out_dir: Path | None = None,
    route: str | None = None,
    setting_id: str | None = None,
    limit_recent: int = 20,
) -> dict[str, Any]:
    if route is not None and route not in settings_closure.ROUTE_ORDER:
        raise ValueError(f"unknown route: {route}")
    if route in settings_registry.DISABLED_ROUTES:
        # This is a query result rather than an error so clients can render a
        # disabled filter without constructing a synthetic command or reading
        # any private session directory.
        return {
            "summary": {
                "out_dir": _display_path(out_dir) if out_dir is not None else None,
                "local_evidence_opted_in": out_dir is not None,
                "sessions": 0,
                "unreadable_manifests": 0,
                "routes_with_sessions": 0,
                "routes_without_sessions": [],
                "outcomes": {},
                "routes_with_next_command": 0,
                "availability": "permanently_disabled",
                "filters": {"route": route, "setting_id": setting_id, "limit_recent": limit_recent},
            },
            "latest_by_route": [],
            "recent_sessions": [],
            "unreadable_manifests": [],
        }
    # Ignored local session history is private operator evidence, never an API
    # default.  An owner must explicitly supply a directory (or opt in via a
    # dedicated configuration value) before it is read.
    configured = os.environ.get("CAMERA_AUTOPILOT_SESSION_LEDGER_DIR")
    root = out_dir or (Path(configured) if configured else None)
    registry = settings_registry.load_registry()
    sessions = []
    unreadable = []
    for path in _manifest_paths(root) if root is not None else []:
        try:
            session = _load_session(path)
        except (OSError, json.JSONDecodeError) as exc:
            unreadable.append({"path": str(path), "error": str(exc)})
            continue
        if route and session["route"] != route:
            continue
        if session["route"] in settings_registry.DISABLED_ROUTES:
            continue
        if setting_id and session.get("setting_id") != setting_id:
            continue
        sessions.append(session)
    sessions.sort(key=lambda item: (item["timestamp"], item["manifest_path"]), reverse=True)
    latest_routes = _latest_by_route(sessions, registry=registry, setting_id=setting_id)
    outcome_counts: dict[str, int] = {}
    for session in sessions:
        outcome = session.get("outcome") or "unknown"
        outcome_counts[outcome] = outcome_counts.get(outcome, 0) + 1
    routes_with_sessions = [row["route"] for row in latest_routes if row["has_session"]]
    return {
        "summary": {
            "out_dir": _display_path(root) if root is not None else None,
            "local_evidence_opted_in": root is not None,
            "sessions": len(sessions),
            "unreadable_manifests": len(unreadable),
            "routes_with_sessions": len(routes_with_sessions),
            "routes_without_sessions": [
                route_name
                for route_name in ACTIVE_ROUTE_ORDER
                if route_name not in routes_with_sessions
            ],
            "outcomes": dict(sorted(outcome_counts.items())),
            "routes_with_next_command": sum(1 for row in latest_routes if row.get("next_command")),
            "filters": {
                "route": route,
                "setting_id": setting_id,
                "limit_recent": limit_recent,
            },
        },
        "latest_by_route": latest_routes,
        "recent_sessions": sessions[:limit_recent],
        "unreadable_manifests": unreadable,
    }


def render_markdown(report: dict[str, Any]) -> str:
    summary = report["summary"]
    lines = [
        "# A7 II Live Session Ledger",
        "",
        f"- out_dir: `{summary['out_dir']}`",
        f"- sessions: `{summary['sessions']}`",
        f"- unreadable_manifests: `{summary['unreadable_manifests']}`",
        f"- routes_with_sessions: `{summary['routes_with_sessions']}`",
        f"- routes_without_sessions: `{', '.join(summary['routes_without_sessions']) or 'none'}`",
        "",
        "## Latest By Route",
        "",
    ]
    for row in report["latest_by_route"]:
        lines.append(f"### {row['route']}")
        lines.append("")
        if not row["has_session"]:
            lines.append("- latest_session: `none`")
            if row.get("next_command"):
                lines.append("- next_command:")
                lines.append("")
                lines.append("```bash")
                lines.append(row["next_command"])
                lines.append("```")
            lines.append("")
            continue
        session = row["latest_session"]
        lines.append(f"- outcome: `{session['outcome']}`")
        lines.append(f"- timestamp: `{session['timestamp']}`")
        lines.append(f"- setting_id: `{session.get('setting_id') or ''}`")
        lines.append(f"- manifest: `{session['manifest_path']}`")
        if session.get("summary_path"):
            lines.append(f"- summary: `{session['summary_path']}`")
        if row.get("next_command"):
            lines.append("- next_command:")
            lines.append("")
            lines.append("```bash")
            lines.append(row["next_command"])
            lines.append("```")
        if row.get("route_recommended_command") and row["route_recommended_command"] != row.get("next_command"):
            lines.append("- route_recommended_command:")
            lines.append("")
            lines.append("```bash")
            lines.append(row["route_recommended_command"])
            lines.append("```")
        lines.append("")
    lines.extend(["## Recent Sessions", ""])
    for session in report["recent_sessions"]:
        lines.append(
            f"- `{session['timestamp']}` `{session['route']}` `{session['outcome']}` "
            f"`{session['manifest_path']}`"
        )
    return "\n".join(lines).rstrip() + "\n"
