"""Local and operator readiness checks for A7 II live proof sessions.

The acceptance report says what still needs proof. This module answers the next practical
question: whether a route session is locally ready to attempt, and what manual camera/package
state still has to be prepared before pressing `--execute`.
"""

from __future__ import annotations

import json
import shlex
import shutil
from collections.abc import Callable
from pathlib import Path
from typing import Any

import settings_closure
import settings_registry
import settings_sessions

ToolResolver = Callable[[str], str | None]
PathExists = Callable[[Path], bool]
OBSERVED_STATE_PLACEHOLDER = "<describe camera/menu state>"
OBSERVED_VALUE_PLACEHOLDER = "<visible phone-app value>"
PHONE_STATE_PLACEHOLDER = "<describe phone/app state>"
OBSERVED_BEHAVIOR_PLACEHOLDER = "<describe near/far visual behavior>"
TARGET_SETUP_PLACEHOLDER = "<describe near/far target setup>"
SMART_REMOTE_METHOD_PLACEHOLDER = "<Smart Remote setter method>"
SMART_REMOTE_REQUESTED_VALUE_PLACEHOLDER = "<value sent>"
SMART_REMOTE_OBSERVED_BEFORE_PLACEHOLDER = "<observed before value>"
SMART_REMOTE_OBSERVED_AFTER_PLACEHOLDER = "<observed after value>"
CAMERA_REMOTE_COMMAND_ARTIFACT_DIR = (
    settings_registry.REPO_ROOT / "artifacts" / "falsification" / "r4_camera_remote_command"
)
CAMERA_REMOTE_COMMAND_LOCATOR_SUMMARY = "final-package-locator-summary.json"
CAMERA_REMOTE_COMMAND_LOCATOR_COMMAND = (
    "python3 tools/find_camera_remote_command_package.py "
    "--include-common-search "
    "--out-dir artifacts/falsification/r4_camera_remote_command"
)
CAMERA_REMOTE_COMMAND_REVIEW_COMMAND = (
    "python3 tools/review_camera_remote_command_package.py "
    "/path/to/CameraRemoteCommand-package.zip "
    "--out-dir artifacts/falsification/r4_camera_remote_command"
)


ROUTE_TOOL_REQUIREMENTS: dict[str, tuple[dict[str, str], ...]] = {
    "smart_remote_wifi": (
        {"tool": "python3", "why": "Runs the Smart Remote JSON-RPC surface probe and review."},
    ),
    "smart_remote_phone": (
        {"tool": "python3", "why": "Runs the phone-preference Smart Remote surface probe and review."},
    ),
    "usb_gphoto": (
        {"tool": "python3", "why": "Runs registry-aware USB probe/review tools."},
        {"tool": "gphoto2", "why": "Talks to the A7 II over USB PC Remote."},
        {"tool": "curl", "why": "Exercises the USB-backed LAN bridge endpoints."},
        {"tool": "file", "why": "Classifies bridge proof artifacts."},
    ),
    "raw_ptp": (
        {"tool": "gphoto2", "why": "Sets camera focus preconditions and captures comparison previews."},
    ),
    "camera_remote_command": (
        {"tool": "python3", "why": "Reviews the local Sony Camera Remote Command package."},
    ),
    "openmemories": (
        {"tool": "python3", "why": "Runs the Route 3 clean-room OpenMemories preflight."},
    ),
    "guided_physical": (
        {"tool": "python3", "why": "Archives guided physical/admin gate proof artifacts."},
    ),
}


ROUTE_PATH_REQUIREMENTS: dict[str, tuple[dict[str, Any], ...]] = {
    "raw_ptp": (
        {
            "name": "raw PTP Python with pyusb",
            "any_of": (
                ".venv/bin/python",
                "/tmp/pmca-venv/bin/python",
            ),
            "why": "Runs raw PTP/pyusb focus behavior probes without recompiling libgphoto2.",
        },
        {
            "path": "tests/probe_raw_control.py",
            "why": "Sends the raw Sony PTP focus transactions used by behavior probes.",
        },
    ),
    "openmemories": (
        {
            "path": "tools/falsify_openmemories_route3.py",
            "why": "Generates and checks the reversible clean-room proof app scaffold.",
        },
    ),
}


ROUTE_OPERATOR_REQUIREMENTS: dict[str, tuple[str, ...]] = {
    "smart_remote_wifi": (
        "Launch Smart Remote Control on the A7 II and join the camera DIRECT Wi-Fi AP from this Mac.",
        "Keep the camera awake through startRecMode/startLiveview API discovery.",
    ),
    "smart_remote_phone": (
        "Connect the phone app preference context, or keep this Mac on the same Smart Remote camera AP.",
        "Record which phone-side settings are app/OS-only versus camera-side API controls.",
    ),
    "usb_gphoto": (
        "Set USB Connection to PC Remote and reconnect the camera before USB probes.",
        "Keep a safe stills state for preview/capture and accept temporary set/read/revert writes.",
        "For product proof, call the USB bridge from a separate LAN/phone client when possible.",
    ),
    "raw_ptp": (
        "Use USB PC Remote with an autofocus/manual-focus capable lens.",
        "Prepare controlled near/far targets and visual review notes before marking behavior proven.",
    ),
    "camera_remote_command": (
        "Obtain the official Sony Camera Remote Command package through the approved local path.",
        "Do not commit or redistribute Sony package contents.",
    ),
    "openmemories": (
        "Keep OpenMemories work reversible; start with /health before settings, preview, focus, or capture.",
        "After any developer-port test, capture rollback evidence that ADB/Telnet are off.",
    ),
    "guided_physical": (
        "Record the exact visible camera/menu state for each physical/admin/state gate.",
        "Do not perform format, delete, reset, cleaning, or network reset actions without new explicit approval.",
    ),
}


def _repo_path(path: str | Path) -> Path:
    candidate = Path(path)
    return candidate if candidate.is_absolute() else settings_registry.REPO_ROOT / candidate


def _command_file_requirements(playbook: dict[str, Any], path_exists: PathExists) -> list[dict[str, Any]]:
    requirements = []
    seen: set[str] = set()
    for command in playbook["ordered_commands"]:
        try:
            tokens = shlex.split(command["command"])
        except ValueError:
            continue
        for token in tokens:
            if not (token.startswith("tools/") or token.startswith("tests/")):
                continue
            if token in seen:
                continue
            seen.add(token)
            path = _repo_path(token)
            exists = path_exists(path)
            requirements.append(
                {
                    "kind": "repo_file",
                    "name": token,
                    "status": "ok" if exists else "missing",
                    "path": str(path),
                    "why": "Referenced by this route playbook.",
                }
            )
    return requirements


def _tool_requirements(route: str, resolver: ToolResolver) -> list[dict[str, Any]]:
    out = []
    for item in ROUTE_TOOL_REQUIREMENTS.get(route, ()):
        tool = item["tool"]
        path = resolver(tool)
        out.append(
            {
                "kind": "tool",
                "name": tool,
                "status": "ok" if path else "missing",
                "path": path,
                "why": item["why"],
            }
        )
    return out


def _path_requirements(route: str, path_exists: PathExists) -> list[dict[str, Any]]:
    out = []
    for item in ROUTE_PATH_REQUIREMENTS.get(route, ()):
        if "any_of" in item:
            candidates = [_repo_path(path) for path in item["any_of"]]
            chosen = next((path for path in candidates if path_exists(path)), None)
            out.append(
                {
                    "kind": "path",
                    "name": item.get("name") or " or ".join(item["any_of"]),
                    "status": "ok" if chosen else "missing",
                    "path": str(chosen) if chosen else None,
                    "candidates": [str(path) for path in candidates],
                    "why": item["why"],
                }
            )
            continue
        path = _repo_path(item["path"])
        exists = path_exists(path)
        out.append(
            {
                "kind": "path",
                "name": item["path"],
                "status": "ok" if exists else "missing",
                "path": str(path),
                "why": item["why"],
            }
        )
    return out


def _latest_locator_report(report_dir: Path = CAMERA_REMOTE_COMMAND_ARTIFACT_DIR) -> dict[str, Any] | None:
    canonical = report_dir / CAMERA_REMOTE_COMMAND_LOCATOR_SUMMARY
    candidates = [canonical] if canonical.is_file() else sorted(
        report_dir.glob("*command-package-locator.json") if report_dir.exists() else [],
        key=lambda path: (path.stat().st_mtime, path.name),
        reverse=True,
    )
    for path in candidates:
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if not isinstance(payload, dict):
            continue
        try:
            artifact = str(path.resolve().relative_to(settings_registry.REPO_ROOT))
        except ValueError:
            artifact = str(path)
        return {
            "artifact": artifact,
            "generated_at": payload.get("generated_at"),
            "summary": payload.get("summary") or {},
            "search": payload.get("search") or {},
        }
    return None


def _package_requirement(package_path: str | Path | None, path_exists: PathExists) -> dict[str, Any]:
    locator = _latest_locator_report()
    if package_path is None:
        requirement = {
            "kind": "package",
            "name": "CameraRemoteCommand package",
            "status": "missing",
            "path": None,
            "why": "Required to review official ILCE-7M2 command support.",
            "next_action": (
                "Locate the official Sony Camera Remote Command package locally, then rerun with "
                "--package-path /path/to/CameraRemoteCommand-package.zip."
            ),
            "next_commands": [
                CAMERA_REMOTE_COMMAND_LOCATOR_COMMAND,
                CAMERA_REMOTE_COMMAND_REVIEW_COMMAND,
            ],
        }
        if locator:
            requirement["last_locator_artifact"] = locator["artifact"]
            requirement["last_locator_summary"] = locator["summary"]
            if (locator["summary"].get("reviewable_candidates") or 0) == 0:
                root_count = locator["search"].get("search_root_count")
                scope = (
                    f" across {root_count} configured local search roots"
                    if root_count is not None
                    else ""
                )
                requirement["last_locator_result"] = (
                    f"No reviewable package candidates found{scope}."
                )
        return requirement
    path = Path(package_path).expanduser()
    exists = path_exists(path)
    return {
        "kind": "package",
        "name": "CameraRemoteCommand package",
        "status": "ok" if exists else "missing",
        "path": str(path),
        "why": "Required to review official ILCE-7M2 command support.",
        "next_action": "Use the approved local package path; do not commit Sony package contents.",
        "next_commands": [
            (
                "python3 tools/review_camera_remote_command_package.py "
                f"{shlex.quote(str(path))} "
                "--out-dir artifacts/falsification/r4_camera_remote_command"
            )
        ],
    }


def _operator_requirements(route: str) -> list[dict[str, Any]]:
    return [
        {
            "kind": "operator",
            "name": f"{settings_closure.ROUTE_LABELS[route]} prerequisite",
            "status": "manual",
            "why": item,
        }
        for item in ROUTE_OPERATOR_REQUIREMENTS.get(route, ())
    ]


def _session_commands(route: str, playbook: dict[str, Any], package_path: str | Path | None) -> dict[str, Any]:
    scoped_setting = f" --setting {playbook['setting_id']}" if playbook.get("setting_id") else ""
    evidence = (
        f"python3 tools/a7ii_live_session_runner.py --route {route}{scoped_setting} "
        "--phase evidence --execute"
    )
    if route == "camera_remote_command":
        evidence += f" --package-path {package_path or '/path/to/CameraRemoteCommand-package.zip'}"
    elif route == "smart_remote_wifi" and playbook.get("setting_id"):
        evidence += (
            " --allow-hardware "
            f'--smart-remote-method "{SMART_REMOTE_METHOD_PLACEHOLDER}" '
            f'--smart-remote-requested-value "{SMART_REMOTE_REQUESTED_VALUE_PLACEHOLDER}" '
            f'--smart-remote-observed-before "{SMART_REMOTE_OBSERVED_BEFORE_PLACEHOLDER}" '
            f'--smart-remote-observed-after "{SMART_REMOTE_OBSERVED_AFTER_PLACEHOLDER}"'
        )
    elif route == "raw_ptp" and playbook.get("setting_id"):
        evidence += (
            f' --observed-behavior "{OBSERVED_BEHAVIOR_PLACEHOLDER}" '
            f'--target-setup "{TARGET_SETUP_PLACEHOLDER}"'
        )
    elif route == "smart_remote_phone":
        setting_arg = f"--setting {playbook.get('setting_id')}" if playbook.get("setting_id") else "--setting <setting_id>"
        evidence = (
            "python3 tools/a7ii_live_session_runner.py --route smart_remote_phone "
            f"{setting_arg} --phase evidence --execute --allow-hardware "
            f'--observed-value "{OBSERVED_VALUE_PLACEHOLDER}" '
            f'--phone-state "{PHONE_STATE_PLACEHOLDER}"'
        )
    elif route == "guided_physical":
        setting_arg = f"--setting {playbook.get('setting_id')}" if playbook.get("setting_id") else "--setting <setting_id>"
        evidence = (
            "python3 tools/a7ii_live_session_runner.py --route guided_physical "
            f"{setting_arg} --phase evidence --execute --allow-hardware "
            f'--observed-state "{OBSERVED_STATE_PLACEHOLDER}"'
        )
    elif any(command["risk"] not in settings_sessions.LOCAL_REVIEW_RISKS | settings_sessions.LOCAL_EVIDENCE_RISKS for command in playbook["evidence_commands"]):
        evidence += " --allow-hardware"
    review = f"python3 tools/a7ii_live_session_runner.py --route {route} --phase review --phase refresh --execute"
    return {
        "recommended_sequence": playbook.get("recommended_sequence_session_command"),
        "evidence": evidence,
        "review_refresh": review,
        "next_concrete": playbook.get("next_concrete_command"),
        "first_evidence": playbook["evidence_commands"][0]["command"] if playbook["evidence_commands"] else None,
        "first_review": playbook["review_commands"][0]["command"] if playbook["review_commands"] else None,
    }


def build_route_readiness(
    route: str,
    *,
    registry: dict[str, Any] | None = None,
    setting_id: str | None = None,
    package_path: str | Path | None = None,
    tool_resolver: ToolResolver | None = None,
    path_exists: PathExists | None = None,
) -> dict[str, Any]:
    if route not in settings_closure.ROUTE_ORDER:
        raise ValueError(f"unknown route: {route}")
    registry = registry or settings_registry.load_registry()
    if route == "openmemories":
        return {
            "route": route,
            "label": settings_closure.ROUTE_LABELS[route],
            "summary": {
                "local_status": "unavailable",
                "live_status": "permanently_disabled",
                "ready_to_attempt": False,
                "requirements": 0,
                "missing_requirements": 0,
                "manual_requirements": 0,
                "evidence_commands": 0,
                "review_commands": 0,
                "refresh_commands": 0,
                "selected_gap_rows": 0,
                "missing_gap_rows": [],
            },
            "requirements": [],
            "missing_requirements": [],
            "manual_requirements": [],
            "session_commands": {
                "recommended_sequence": None,
                "evidence": None,
                "review_refresh": None,
                "next_concrete": None,
                "first_evidence": None,
                "first_review": None,
            },
            "next_concrete_command": None,
            "recommended_sequence_session_command": None,
            "next_commands": [],
            "recommended_next_action": None,
        }
    tool_resolver = tool_resolver or shutil.which
    path_exists = path_exists or (lambda path: path.exists())
    playbook = settings_sessions.build_route_playbook(route, registry=registry, setting_id=setting_id)
    requirements = [
        *_tool_requirements(route, tool_resolver),
        *_path_requirements(route, path_exists),
        *_command_file_requirements(playbook, path_exists),
        *_operator_requirements(route),
    ]
    if route == "camera_remote_command":
        requirements.append(_package_requirement(package_path, path_exists))

    missing = [item for item in requirements if item["status"] == "missing"]
    manual = [item for item in requirements if item["status"] == "manual"]
    local_ready = not missing
    session_commands = _session_commands(route, playbook, package_path)
    next_commands: list[str] = []
    if missing:
        next_commands.extend(missing[0].get("next_commands") or [])
        if not next_commands:
            next_commands.append(f"Install or restore `{missing[0]['name']}` before running route evidence.")
    else:
        next_commands.append(session_commands["recommended_sequence"])
    recommended_next = next_commands[0]

    return {
        "route": route,
        "label": settings_closure.ROUTE_LABELS[route],
        "summary": {
            "local_status": "ready" if local_ready else "missing_prerequisites",
            "live_status": "needs_operator_camera_state" if manual else "ready",
            "ready_to_attempt": local_ready,
            "requirements": len(requirements),
            "missing_requirements": len(missing),
            "manual_requirements": len(manual),
            "evidence_commands": len(playbook["evidence_commands"]),
            "review_commands": len(playbook["review_commands"]),
            "refresh_commands": len(playbook["refresh_commands"]),
            "selected_gap_rows": playbook["summary"]["selected_gap_rows"],
            "missing_gap_rows": playbook["summary"]["missing_gap_rows"],
        },
        "requirements": requirements,
        "missing_requirements": missing,
        "manual_requirements": manual,
        "session_commands": session_commands,
        "next_concrete_command": playbook.get("next_concrete_command"),
        "recommended_sequence_session_command": playbook.get("recommended_sequence_session_command"),
        "next_commands": next_commands,
        "recommended_next_action": recommended_next,
    }


def build_readiness_report(
    *,
    route: str | None = None,
    registry: dict[str, Any] | None = None,
    setting_id: str | None = None,
    package_path: str | Path | None = None,
    tool_resolver: ToolResolver | None = None,
    path_exists: PathExists | None = None,
) -> dict[str, Any]:
    registry = registry or settings_registry.load_registry()
    routes = [route] if route else list(settings_closure.ROUTE_ORDER)
    route_reports = [
        build_route_readiness(
            item,
            registry=registry,
            setting_id=setting_id,
            package_path=package_path,
            tool_resolver=tool_resolver,
            path_exists=path_exists,
        )
        for item in routes
    ]
    return {
        "summary": {
            "routes": len(route_reports),
            "ready_to_attempt": sum(1 for item in route_reports if item["summary"]["ready_to_attempt"]),
            "missing_requirements": sum(item["summary"]["missing_requirements"] for item in route_reports),
            "manual_requirements": sum(item["summary"]["manual_requirements"] for item in route_reports),
            "selected_gap_rows": sum(item["summary"]["selected_gap_rows"] for item in route_reports),
            "missing_gap_rows": sorted(
                {
                    setting_id
                    for item in route_reports
                    for setting_id in item["summary"]["missing_gap_rows"]
                }
            ),
        },
        "routes": route_reports,
    }
