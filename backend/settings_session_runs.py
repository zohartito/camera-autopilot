"""Audited execution manifests for A7 II route playbooks.

This layer is deliberately dry-run-first. It can run local review/refresh commands when asked, but
hardware-touching evidence commands require a separate explicit approval flag so a session manifest
cannot accidentally move focus, capture, or write camera settings.
"""

from __future__ import annotations

import json
import re
import shlex
import subprocess
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import settings_probe_plan
import settings_readiness
import settings_registry
import settings_sessions

REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_OUT_DIR = REPO_ROOT / "artifacts" / "falsification" / "live_sessions"
PHASE_TO_KEY = {
    "evidence": "evidence_commands",
    "review": "review_commands",
    "refresh": "refresh_commands",
}
LOCAL_RUN_RISKS = {"local_artifact_review", "local_package_discovery", "local_package_read"}
OBSERVATION_RISKS = {
    "manual_observation",
    "manual_admin_observation",
    "phone_manual_observation",
    "focus_visual_review",
    "smart_remote_behavior_review",
}
OBSERVED_STATE_PLACEHOLDER = "<describe camera/menu state>"
OBSERVED_VALUE_PLACEHOLDER = "<visible phone-app value>"
PHONE_STATE_PLACEHOLDER = "<describe phone/app state>"
OBSERVED_BEHAVIOR_PLACEHOLDER = "<describe near/far visual behavior>"
TARGET_SETUP_PLACEHOLDER = "<describe near/far target setup>"
SMART_REMOTE_METHOD_PLACEHOLDER = "<Smart Remote setter method>"
SMART_REMOTE_REQUESTED_VALUE_PLACEHOLDER = "<value sent>"
SMART_REMOTE_OBSERVED_BEFORE_PLACEHOLDER = "<observed before value>"
SMART_REMOTE_OBSERVED_AFTER_PLACEHOLDER = "<observed after value>"
SMART_REMOTE_OBSERVED_BEHAVIOR_PLACEHOLDER = "<describe camera-visible behavior>"
CAMERA_MODEL_PLACEHOLDER = settings_probe_plan.CAMERA_MODEL_PLACEHOLDER
CAMERA_PORT_PLACEHOLDER = settings_probe_plan.CAMERA_PORT_PLACEHOLDER
CAMERA_SERIAL_PLACEHOLDER = settings_probe_plan.CAMERA_SERIAL_PLACEHOLDER
CAMERA_IDENTITY_PLACEHOLDERS = (CAMERA_MODEL_PLACEHOLDER, CAMERA_PORT_PLACEHOLDER, CAMERA_SERIAL_PLACEHOLDER)


@dataclass(frozen=True)
class CommandResult:
    returncode: int
    stdout: str = ""
    stderr: str = ""


CommandRunner = Callable[[str, Path], CommandResult]
ToolResolver = Callable[[str], str | None]
PathExists = Callable[[Path], bool]


def command_requires_hardware(command: dict[str, Any]) -> bool:
    return command.get("risk") not in LOCAL_RUN_RISKS


def command_requires_package(command: dict[str, Any]) -> bool:
    return command.get("risk") == "local_package_read"


def command_requires_camera_identity(command: dict[str, Any]) -> bool:
    """USB setting writes run only against the selected body, proven by its USB serial."""
    text = command.get("command", "")
    return any(placeholder in text for placeholder in CAMERA_IDENTITY_PLACEHOLDERS)


def command_requires_observation(command: dict[str, Any]) -> bool:
    text = command.get("command", "")
    return command.get("risk") in OBSERVATION_RISKS and any(
        placeholder in text
        for placeholder in (OBSERVED_STATE_PLACEHOLDER, OBSERVED_VALUE_PLACEHOLDER, PHONE_STATE_PLACEHOLDER)
        + (OBSERVED_BEHAVIOR_PLACEHOLDER, TARGET_SETUP_PLACEHOLDER)
        + (
            SMART_REMOTE_METHOD_PLACEHOLDER,
            SMART_REMOTE_REQUESTED_VALUE_PLACEHOLDER,
            SMART_REMOTE_OBSERVED_BEFORE_PLACEHOLDER,
            SMART_REMOTE_OBSERVED_AFTER_PLACEHOLDER,
            SMART_REMOTE_OBSERVED_BEHAVIOR_PLACEHOLDER,
        )
    )


def normalize_phases(phases: list[str] | tuple[str, ...] | None) -> list[str]:
    if not phases or "all" in phases:
        return ["evidence", "review", "refresh"]
    out = []
    for phase in phases:
        if phase not in PHASE_TO_KEY:
            raise ValueError(f"unknown session phase: {phase}")
        if phase not in out:
            out.append(phase)
    return out


def default_runner(command: str, cwd: Path) -> CommandResult:
    try:
        argv = shlex.split(command)
    except ValueError as exc:
        return CommandResult(2, "", f"invalid command quoting: {exc}")
    if not argv:
        return CommandResult(2, "", "empty command")
    proc = subprocess.run(
        argv,
        cwd=cwd,
        check=False,
        capture_output=True,
        text=True,
    )
    return CommandResult(proc.returncode, proc.stdout, proc.stderr)


def _slug(value: str) -> str:
    return "".join(ch if ch.isalnum() or ch in "._-" else "_" for ch in value).strip("_") or "session"


def _scoped_evidence_command(playbook: dict[str, Any]) -> dict[str, Any] | None:
    next_concrete = playbook.get("next_concrete_command")
    if not playbook.get("setting_id") or not next_concrete:
        return None
    for command in playbook["evidence_commands"]:
        if command["command"] == next_concrete:
            return command
    return None


def _commands_for_phases(playbook: dict[str, Any], phases: list[str]) -> list[tuple[str, dict[str, Any]]]:
    commands = []
    scoped_evidence = _scoped_evidence_command(playbook)
    for phase in phases:
        if phase == "evidence" and scoped_evidence:
            commands.append((phase, scoped_evidence))
            continue
        for command in playbook[PHASE_TO_KEY[phase]]:
            commands.append((phase, command))
    return commands


def _sequence_command(command: dict[str, Any], kind: str) -> dict[str, Any]:
    out = dict(command)
    out["sequence_kind"] = kind
    return out


def _is_scoped_to_setting(command: dict[str, Any], setting_id: str) -> bool:
    text = command["command"]
    return (
        f"--setting {setting_id}" in text
        or f"--setting-id {setting_id}" in text
        or f"/settings/{setting_id}" in text
    )


def _is_one_row_evidence_command(playbook: dict[str, Any], command: dict[str, Any] | None) -> bool:
    if not command:
        return False
    setting_id = playbook.get("setting_id")
    if command.get("risk") in OBSERVATION_RISKS:
        return True
    if setting_id and _is_scoped_to_setting(command, str(setting_id)):
        return True
    setting_ids = command.get("setting_ids", [])
    if not setting_id and command.get("setting_count", len(setting_ids)) == 1 and len(setting_ids) == 1:
        return _is_scoped_to_setting(command, str(setting_ids[0]))
    return False


def _commands_for_recommended_sequence(playbook: dict[str, Any]) -> list[tuple[str, dict[str, Any]]]:
    commands: list[tuple[str, dict[str, Any]]] = []
    seen: set[str] = set()

    def append(phase: str, command: dict[str, Any] | None, kind: str) -> None:
        if not command or command["command"] in seen:
            return
        seen.add(command["command"])
        commands.append((phase, _sequence_command(command, kind)))

    evidence_commands = playbook.get("evidence_commands") or []
    first_evidence = evidence_commands[0] if evidence_commands else None
    append(
        "evidence",
        first_evidence,
        "one_row_direct_evidence"
        if _is_one_row_evidence_command(playbook, first_evidence)
        else "route_prerequisite_probe",
    )
    append("evidence", _scoped_evidence_command(playbook), "one_row_direct_evidence")
    if len(commands) == 1 and evidence_commands:
        for command in evidence_commands:
            if _is_one_row_evidence_command(playbook, command):
                append("evidence", command, "one_row_direct_evidence")
                break
    for command in playbook.get("review_commands") or []:
        append("review", command, "review_after_evidence")
    for command in playbook.get("refresh_commands") or []:
        append("refresh", command, "refresh_after_evidence")
    return commands


def _write_text(path: Path, text: str) -> str:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return str(path)


def _summarize(entries: list[dict[str, Any]]) -> dict[str, Any]:
    statuses: dict[str, int] = {}
    phases: dict[str, int] = {}
    for entry in entries:
        statuses[entry["status"]] = statuses.get(entry["status"], 0) + 1
        phases[entry["phase"]] = phases.get(entry["phase"], 0) + 1
    return {
        "commands": len(entries),
        "statuses": dict(sorted(statuses.items())),
        "phases": dict(sorted(phases.items())),
        "failed": statuses.get("failed", 0),
        "hardware_blocked": statuses.get("blocked_requires_hardware_approval", 0),
        "identity_blocked": statuses.get("blocked_requires_camera_serial", 0),
        "readiness_blocked": statuses.get("blocked_missing_local_readiness", 0),
        "package_blocked": statuses.get("blocked_missing_local_package", 0),
        "observation_blocked": statuses.get("blocked_needs_operator_observation", 0),
        "prior_step_blocked": statuses.get("blocked_prior_sequence_step", 0),
        "blocked": statuses.get("blocked_requires_hardware_approval", 0)
        + statuses.get("blocked_requires_camera_serial", 0)
        + statuses.get("blocked_missing_local_readiness", 0)
        + statuses.get("blocked_missing_local_package", 0)
        + statuses.get("blocked_needs_operator_observation", 0)
        + statuses.get("blocked_prior_sequence_step", 0),
        "executed": statuses.get("passed", 0) + statuses.get("failed", 0),
        "planned": statuses.get("planned", 0),
    }


def _session_command(
    *,
    route: str,
    setting_id: str | None,
    phases: list[str] | None = None,
    recommended_sequence: bool = False,
    execute: bool = False,
    allow_hardware: bool = False,
    package_path: Path | str | None = None,
    observation_placeholders_for: str | None = None,
    camera_identity_placeholders: bool = False,
) -> str:
    parts = ["python3", "tools/a7ii_live_session_runner.py", "--route", route]
    if setting_id:
        parts.extend(["--setting", setting_id])
    if recommended_sequence:
        parts.append("--recommended-sequence")
    else:
        for phase in phases or []:
            parts.extend(["--phase", phase])
    if execute:
        parts.append("--execute")
    if allow_hardware:
        parts.append("--allow-hardware")
    if camera_identity_placeholders:
        parts.extend(["--camera-model", CAMERA_MODEL_PLACEHOLDER])
        parts.extend(["--camera-port", CAMERA_PORT_PLACEHOLDER])
        parts.extend(["--camera-serial", CAMERA_SERIAL_PLACEHOLDER])
    if route == "camera_remote_command":
        parts.extend(["--package-path", str(package_path or "/path/to/CameraRemoteCommand-package.zip")])
    if observation_placeholders_for == "guided_physical":
        parts.extend(["--observed-state", OBSERVED_STATE_PLACEHOLDER])
    elif observation_placeholders_for == "smart_remote_phone":
        parts.extend(["--observed-value", OBSERVED_VALUE_PLACEHOLDER])
        parts.extend(["--phone-state", PHONE_STATE_PLACEHOLDER])
    elif observation_placeholders_for == "raw_ptp":
        parts.extend(["--observed-behavior", OBSERVED_BEHAVIOR_PLACEHOLDER])
        parts.extend(["--target-setup", TARGET_SETUP_PLACEHOLDER])
    elif observation_placeholders_for == "smart_remote_wifi":
        parts.extend(["--smart-remote-observed-before", SMART_REMOTE_OBSERVED_BEFORE_PLACEHOLDER])
        parts.extend(["--smart-remote-observed-after", SMART_REMOTE_OBSERVED_AFTER_PLACEHOLDER])
        parts.extend(["--smart-remote-observed-behavior", SMART_REMOTE_OBSERVED_BEHAVIOR_PLACEHOLDER])
    return " ".join(shlex.quote(part) for part in parts)


def _status_entries(entries: list[dict[str, Any]], statuses: set[str]) -> list[dict[str, Any]]:
    return [
        {
            "index": entry["index"],
            "phase": entry["phase"],
            "title": entry["title"],
            "status": entry["status"],
            "command": entry["command"],
            "sequence_kind": entry.get("sequence_kind"),
        }
        for entry in entries
        if entry["status"] in statuses
    ]


def _missing_requirement_commands(readiness_snapshot: dict[str, Any]) -> list[str]:
    route_report = readiness_snapshot["routes"][0]
    commands: list[str] = []
    seen: set[str] = set()
    for requirement in route_report["missing_requirements"]:
        for command in requirement.get("next_commands") or []:
            if command and command not in seen:
                seen.add(command)
                commands.append(command)
        next_action = requirement.get("next_action")
        if next_action and next_action.startswith("python3 ") and next_action not in seen:
            seen.add(next_action)
            commands.append(next_action)
    return commands


def _artifact_roots_for(entries: list[dict[str, Any]], statuses: set[str]) -> list[str]:
    roots = {
        entry["artifact_root"]
        for entry in entries
        if entry["status"] in statuses and entry.get("artifact_root")
    }
    return sorted(roots)


def _aftermath(
    *,
    route: str,
    setting_id: str | None,
    normalized_phases: list[str],
    recommended_sequence: bool,
    execute: bool,
    allow_hardware: bool,
    package_path: Path | None,
    readiness_snapshot: dict[str, Any],
    entries: list[dict[str, Any]],
) -> dict[str, Any]:
    summary = _summarize(entries)
    route_report = readiness_snapshot["routes"][0]
    session_commands = route_report.get("session_commands") or {}
    blocked_statuses = {
        "blocked_requires_hardware_approval",
        "blocked_requires_camera_serial",
        "blocked_missing_local_readiness",
        "blocked_missing_local_package",
        "blocked_needs_operator_observation",
        "blocked_prior_sequence_step",
    }
    unresolved = [entry for entry in entries if entry["status"] != "passed"]
    blocked = [entry for entry in entries if entry["status"] in blocked_statuses]
    failed = [entry for entry in entries if entry["status"] == "failed"]
    planned = [entry for entry in entries if entry["status"] == "planned"]
    needs_camera_identity = any(entry["requires_camera_identity"] for entry in entries)
    next_commands: list[str] = []
    note = ""

    def add(command: str | None) -> None:
        if command and command not in next_commands:
            next_commands.append(command)

    if planned and len(planned) == len(entries):
        outcome = "planned_only"
        note = "Dry-run manifest only; rerun the same session with execution enabled when the camera state is ready."
        add(
            _session_command(
                route=route,
                setting_id=setting_id,
                phases=None if recommended_sequence else normalized_phases,
                recommended_sequence=recommended_sequence,
                execute=True,
                allow_hardware=any(entry["requires_hardware"] for entry in entries) or allow_hardware,
                package_path=package_path,
                camera_identity_placeholders=needs_camera_identity,
            )
        )
    elif failed:
        outcome = "command_failed"
        note = "A command returned non-zero; inspect its stdout/stderr artifacts before rerunning."
        add(failed[0]["command"])
    elif summary["readiness_blocked"]:
        outcome = "needs_local_readiness"
        note = "Local route prerequisites are missing; satisfy the readiness command before touching the camera."
        for command in _missing_requirement_commands(readiness_snapshot):
            add(command)
    elif summary["package_blocked"]:
        outcome = "needs_package_path"
        note = "Camera Remote Command package review needs a real local Sony package path."
        for command in _missing_requirement_commands(readiness_snapshot):
            add(command)
        add(
            _session_command(
                route=route,
                setting_id=setting_id,
                phases=normalized_phases if not recommended_sequence else None,
                recommended_sequence=recommended_sequence,
                execute=True,
                allow_hardware=allow_hardware,
                package_path="/path/to/CameraRemoteCommand-package.zip",
            )
        )
    elif summary["hardware_blocked"]:
        outcome = "needs_hardware_approval"
        note = "The session includes camera-touching commands and was executed without explicit hardware approval."
        add(
            _session_command(
                route=route,
                setting_id=setting_id,
                phases=None if recommended_sequence else normalized_phases,
                recommended_sequence=recommended_sequence,
                execute=True,
                allow_hardware=True,
                package_path=package_path,
                camera_identity_placeholders=needs_camera_identity,
            )
        )
    elif summary["identity_blocked"]:
        outcome = "needs_camera_serial"
        note = (
            "USB setting writes run only on the selected body: re-detect the camera and rerun with its "
            "exact model, USB port and USB serial."
        )
        add(
            _session_command(
                route=route,
                setting_id=setting_id,
                phases=None if recommended_sequence else normalized_phases,
                recommended_sequence=recommended_sequence,
                execute=True,
                allow_hardware=True,
                package_path=package_path,
                camera_identity_placeholders=True,
            )
        )
    elif summary["observation_blocked"]:
        outcome = "needs_operator_observation"
        note = "One-row human observation fields are required before the proof command can run."
        add(
            _session_command(
                route=route,
                setting_id=setting_id,
                phases=None if recommended_sequence else normalized_phases,
                recommended_sequence=recommended_sequence,
                execute=True,
                allow_hardware=allow_hardware or any(entry["requires_hardware"] for entry in entries),
                package_path=package_path,
                observation_placeholders_for=route,
                camera_identity_placeholders=needs_camera_identity,
            )
        )
    elif summary["prior_step_blocked"]:
        outcome = "blocked_after_prior_sequence_step"
        note = "A recommended sequence stopped after an earlier step; rerun from the first blocked command."
        first_blocked = blocked[0] if blocked else None
        add(first_blocked["command"] if first_blocked else None)
    elif summary["executed"] == summary["commands"] and summary["blocked"] == 0 and summary["failed"] == 0:
        evidence_passed = any(entry["phase"] == "evidence" for entry in entries)
        review_or_refresh_passed = any(entry["phase"] in {"review", "refresh"} for entry in entries)
        if evidence_passed and not review_or_refresh_passed:
            outcome = "evidence_captured_needs_review"
            note = "Evidence command passed; review and refresh the registry-facing reports next."
            add(session_commands.get("review_refresh"))
        else:
            outcome = "session_complete"
            note = "All selected session commands passed; refresh the completion audit to see remaining route gates."
            add("python3 tools/a7ii_completion_audit.py --json")
    else:
        outcome = "mixed_unresolved"
        note = "Session has mixed unresolved states; inspect the first unresolved command."
        first_unresolved = unresolved[0] if unresolved else None
        add(first_unresolved["command"] if first_unresolved else None)

    first_unresolved = unresolved[0] if unresolved else None
    return {
        "outcome": outcome,
        "note": note,
        "first_unresolved": _status_entries([first_unresolved], {first_unresolved["status"]})[0]
        if first_unresolved
        else None,
        "blocked_commands": _status_entries(entries, blocked_statuses),
        "failed_commands": _status_entries(entries, {"failed"}),
        "planned_artifact_roots": _artifact_roots_for(entries, {"planned"}),
        "produced_artifact_roots": _artifact_roots_for(entries, {"passed"}),
        "pending_artifact_roots": _artifact_roots_for(
            entries,
            {
                "planned",
                "failed",
                "blocked_requires_hardware_approval",
                "blocked_requires_camera_serial",
                "blocked_missing_local_readiness",
                "blocked_missing_local_package",
                "blocked_needs_operator_observation",
                "blocked_prior_sequence_step",
            },
        ),
        "next_commands": next_commands,
        "review_refresh_command": session_commands.get("review_refresh"),
        "recommended_sequence_session_command": route_report.get("recommended_sequence_session_command"),
        "ready_to_attempt": route_report["summary"]["ready_to_attempt"],
        "local_status": route_report["summary"]["local_status"],
        "live_status": route_report["summary"]["live_status"],
    }


def _resolve_command(
    command: str,
    package_path: Path | None,
    observed_state: str | None,
    observed_value: str | None,
    phone_state: str | None,
    observed_behavior: str | None,
    target_setup: str | None,
    smart_remote_method: str | None,
    smart_remote_requested_value: str | None,
    smart_remote_observed_before: str | None,
    smart_remote_observed_after: str | None,
    smart_remote_observed_behavior: str | None,
    camera_model: str | None,
    camera_port: str | None,
    camera_serial: str | None,
) -> str:
    # One pass over the template: an inserted value is never searched again, so no value can be matched
    # by another placeholder (or its own) and split into extra arguments. A quoted placeholder is
    # replaced together with its quotes (shlex.join templates single-quote them), so a value is never
    # spliced inside another quoted string.
    supplied = {
        placeholder: value
        for placeholder, value in (
            ("/path/to/CameraRemoteCommand-package.zip", str(package_path) if package_path is not None else None),
            (OBSERVED_STATE_PLACEHOLDER, observed_state),
            (OBSERVED_VALUE_PLACEHOLDER, observed_value),
            (PHONE_STATE_PLACEHOLDER, phone_state),
            (OBSERVED_BEHAVIOR_PLACEHOLDER, observed_behavior),
            (TARGET_SETUP_PLACEHOLDER, target_setup),
            (SMART_REMOTE_METHOD_PLACEHOLDER, smart_remote_method),
            (SMART_REMOTE_REQUESTED_VALUE_PLACEHOLDER, smart_remote_requested_value),
            (SMART_REMOTE_OBSERVED_BEFORE_PLACEHOLDER, smart_remote_observed_before),
            (SMART_REMOTE_OBSERVED_AFTER_PLACEHOLDER, smart_remote_observed_after),
            (SMART_REMOTE_OBSERVED_BEHAVIOR_PLACEHOLDER, smart_remote_observed_behavior),
            (CAMERA_MODEL_PLACEHOLDER, camera_model),
            (CAMERA_PORT_PLACEHOLDER, camera_port),
            (CAMERA_SERIAL_PLACEHOLDER, camera_serial),
        )
        if value
    }
    if not supplied:
        return command
    alternatives = "|".join(re.escape(placeholder) for placeholder in sorted(supplied, key=len, reverse=True))
    pattern = re.compile(f'"({alternatives})"|\'({alternatives})\'|({alternatives})')
    return pattern.sub(lambda match: shlex.quote(supplied[next(filter(None, match.groups()))]), command)


def _has_required_observation(
    command: dict[str, Any],
    *,
    route: str,
    observed_state: str | None,
    observed_value: str | None,
    phone_state: str | None,
    observed_behavior: str | None,
    target_setup: str | None,
    smart_remote_method: str | None,
    smart_remote_requested_value: str | None,
    smart_remote_observed_before: str | None,
    smart_remote_observed_after: str | None,
    smart_remote_observed_behavior: str | None,
) -> bool:
    if not command_requires_observation(command):
        return True
    text = command.get("command", "")
    if route == "guided_physical":
        return bool(observed_state)
    if route == "smart_remote_phone":
        return bool(observed_value and phone_state)
    if route == "raw_ptp":
        return bool(observed_behavior and target_setup)
    if route == "smart_remote_wifi":
        method_ok = bool(smart_remote_method) or SMART_REMOTE_METHOD_PLACEHOLDER not in text
        value_ok = bool(smart_remote_requested_value) or SMART_REMOTE_REQUESTED_VALUE_PLACEHOLDER not in text
        return bool(method_ok and value_ok and smart_remote_observed_before and smart_remote_observed_after)
    return False


def _missing_non_package_requirements(readiness_snapshot: dict[str, Any]) -> list[dict[str, Any]]:
    route_report = readiness_snapshot["routes"][0]
    return [
        item
        for item in route_report["missing_requirements"]
        if item.get("kind") != "package"
    ]


def _markdown(manifest: dict[str, Any]) -> str:
    lines = [
        f"# A7 II Live Session Run: {manifest['route']}",
        "",
        f"- execute: `{manifest['execute']}`",
        f"- recommended_sequence: `{manifest.get('recommended_sequence', False)}`",
        f"- allow_hardware: `{manifest['allow_hardware']}`",
        f"- observed_state: `{manifest.get('observed_state') or ''}`",
        f"- observed_value: `{manifest.get('observed_value') or ''}`",
        f"- phone_state: `{manifest.get('phone_state') or ''}`",
        f"- observed_behavior: `{manifest.get('observed_behavior') or ''}`",
        f"- target_setup: `{manifest.get('target_setup') or ''}`",
        f"- smart_remote_method: `{manifest.get('smart_remote_method') or ''}`",
        f"- smart_remote_requested_value: `{manifest.get('smart_remote_requested_value') or ''}`",
        f"- smart_remote_observed_before: `{manifest.get('smart_remote_observed_before') or ''}`",
        f"- smart_remote_observed_after: `{manifest.get('smart_remote_observed_after') or ''}`",
        f"- smart_remote_observed_behavior: `{manifest.get('smart_remote_observed_behavior') or ''}`",
        f"- camera_model: `{manifest.get('camera_model') or ''}`",
        f"- camera_port: `{manifest.get('camera_port') or ''}`",
        f"- camera_serial: `{manifest.get('camera_serial') or ''}`",
        f"- phases: `{', '.join(manifest['phases'])}`",
        f"- playbook_next_concrete_command: `{manifest.get('playbook_next_concrete_command') or ''}`",
        f"- manifest: `{manifest['manifest_path']}`",
        f"- readiness: `{manifest['readiness_path']}`",
        "",
        "## Summary",
        "",
    ]
    for key, value in manifest["summary"].items():
        lines.append(f"- {key}: `{value}`")
    aftermath = manifest.get("aftermath") or {}
    if aftermath:
        lines.extend(["", "## Aftermath", ""])
        lines.append(f"- outcome: `{aftermath['outcome']}`")
        lines.append(f"- note: {aftermath['note']}")
        lines.append(f"- local_status: `{aftermath['local_status']}`")
        lines.append(f"- live_status: `{aftermath['live_status']}`")
        if aftermath.get("produced_artifact_roots"):
            lines.append(
                "- produced_artifact_roots: "
                + ", ".join(f"`{root}`" for root in aftermath["produced_artifact_roots"])
            )
        if aftermath.get("pending_artifact_roots"):
            lines.append(
                "- pending_artifact_roots: "
                + ", ".join(f"`{root}`" for root in aftermath["pending_artifact_roots"])
            )
        if aftermath.get("first_unresolved"):
            first = aftermath["first_unresolved"]
            lines.append(
                "- first_unresolved: "
                f"`{first['index']}` `{first['status']}` `{first['title']}`"
            )
        if aftermath.get("next_commands"):
            lines.extend(["", "Next commands:", ""])
            for command in aftermath["next_commands"]:
                lines.append("```bash")
                lines.append(command)
                lines.append("```")
                lines.append("")
    readiness_summary = manifest["readiness_snapshot"]["routes"][0]["summary"]
    lines.extend(["", "## Readiness Snapshot", ""])
    for key in (
        "local_status",
        "live_status",
        "ready_to_attempt",
        "missing_requirements",
        "manual_requirements",
        "selected_gap_rows",
    ):
        lines.append(f"- {key}: `{readiness_summary[key]}`")
    lines.extend(["", "## Commands", ""])
    for entry in manifest["commands"]:
        lines.append(f"### {entry['index']}. {entry['title']}")
        lines.append("")
        lines.append(f"- phase: `{entry['phase']}`")
        lines.append(f"- risk: `{entry['risk']}`")
        lines.append(f"- requires_observation: `{entry.get('requires_observation', False)}`")
        lines.append(f"- status: `{entry['status']}`")
        if entry.get("returncode") is not None:
            lines.append(f"- returncode: `{entry['returncode']}`")
        if entry.get("sequence_kind"):
            lines.append(f"- sequence_kind: `{entry['sequence_kind']}`")
        if entry.get("stdout_path"):
            lines.append(f"- stdout: `{entry['stdout_path']}`")
        if entry.get("stderr_path"):
            lines.append(f"- stderr: `{entry['stderr_path']}`")
        lines.append("")
        lines.append("```bash")
        lines.append(entry["command"])
        lines.append("```")
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def run_session(
    *,
    route: str,
    setting_id: str | None = None,
    phases: list[str] | tuple[str, ...] | None = None,
    recommended_sequence: bool = False,
    execute: bool = False,
    allow_hardware: bool = False,
    out_dir: Path | None = None,
    package_path: Path | None = None,
    observed_state: str | None = None,
    observed_value: str | None = None,
    phone_state: str | None = None,
    observed_behavior: str | None = None,
    target_setup: str | None = None,
    smart_remote_method: str | None = None,
    smart_remote_requested_value: str | None = None,
    smart_remote_observed_before: str | None = None,
    smart_remote_observed_after: str | None = None,
    smart_remote_observed_behavior: str | None = None,
    camera_model: str | None = None,
    camera_port: str | None = None,
    camera_serial: str | None = None,
    command_runner: CommandRunner | None = None,
    readiness_tool_resolver: ToolResolver | None = None,
    readiness_path_exists: PathExists | None = None,
    registry: dict[str, Any] | None = None,
) -> dict[str, Any]:
    # Reject before resolving the registry, planning commands, or creating an
    # output directory.  Route 3 has no permissible session mode.
    if route in settings_registry.DISABLED_ROUTES:
        raise ValueError(f"route permanently disabled: {route}")
    registry = registry or settings_registry.load_registry()
    if recommended_sequence and phases:
        raise ValueError("recommended_sequence cannot be combined with explicit phases")
    normalized_phases = ["recommended_sequence"] if recommended_sequence else normalize_phases(phases)
    observed_state = observed_state.strip() if observed_state else None
    observed_value = observed_value.strip() if observed_value else None
    phone_state = phone_state.strip() if phone_state else None
    observed_behavior = observed_behavior.strip() if observed_behavior else None
    target_setup = target_setup.strip() if target_setup else None
    smart_remote_method = smart_remote_method.strip() if smart_remote_method else None
    smart_remote_requested_value = smart_remote_requested_value.strip() if smart_remote_requested_value else None
    smart_remote_observed_before = smart_remote_observed_before.strip() if smart_remote_observed_before else None
    smart_remote_observed_after = smart_remote_observed_after.strip() if smart_remote_observed_after else None
    smart_remote_observed_behavior = smart_remote_observed_behavior.strip() if smart_remote_observed_behavior else None
    if observed_state == OBSERVED_STATE_PLACEHOLDER:
        observed_state = None
    if observed_value == OBSERVED_VALUE_PLACEHOLDER:
        observed_value = None
    if phone_state == PHONE_STATE_PLACEHOLDER:
        phone_state = None
    if observed_behavior == OBSERVED_BEHAVIOR_PLACEHOLDER:
        observed_behavior = None
    if target_setup == TARGET_SETUP_PLACEHOLDER:
        target_setup = None
    if smart_remote_method == SMART_REMOTE_METHOD_PLACEHOLDER:
        smart_remote_method = None
    if smart_remote_requested_value == SMART_REMOTE_REQUESTED_VALUE_PLACEHOLDER:
        smart_remote_requested_value = None
    if smart_remote_observed_before == SMART_REMOTE_OBSERVED_BEFORE_PLACEHOLDER:
        smart_remote_observed_before = None
    if smart_remote_observed_after == SMART_REMOTE_OBSERVED_AFTER_PLACEHOLDER:
        smart_remote_observed_after = None
    if smart_remote_observed_behavior == SMART_REMOTE_OBSERVED_BEHAVIOR_PLACEHOLDER:
        smart_remote_observed_behavior = None
    # Exact values, never stripped: the serial is compared byte for byte with the one the OS reports.
    camera_model, camera_port, camera_serial = (
        None if not value or value in CAMERA_IDENTITY_PLACEHOLDERS else value
        for value in (camera_model, camera_port, camera_serial)
    )
    if any((camera_model, camera_port, camera_serial)) and not all((camera_model, camera_port, camera_serial)):
        # USB ports re-enumerate; the serial is the body's identity, so a selection always carries it.
        raise ValueError("camera_model, camera_port and camera_serial must be supplied together")
    if any(char in (camera_model or "") + (camera_port or "") + (camera_serial or "") for char in "<>"):
        # Placeholder syntax; a value that looks like one would read as unresolved identity.
        raise ValueError("camera_model, camera_port and camera_serial must not contain '<' or '>'")
    if observed_state and route != "guided_physical":
        raise ValueError("--observed-state is only valid for guided_physical sessions")
    if observed_state and route == "guided_physical" and not setting_id:
        raise ValueError("--observed-state requires --setting so one observation is tied to one row")
    if (observed_value or phone_state) and route != "smart_remote_phone":
        raise ValueError("--observed-value/--phone-state are only valid for smart_remote_phone sessions")
    if (observed_value or phone_state) and not setting_id:
        raise ValueError("--observed-value/--phone-state require --setting so one observation is tied to one row")
    if bool(observed_value) != bool(phone_state):
        raise ValueError("--observed-value and --phone-state must be supplied together")
    if (observed_behavior or target_setup) and route != "raw_ptp":
        raise ValueError("--observed-behavior/--target-setup are only valid for raw_ptp sessions")
    if (observed_behavior or target_setup) and not setting_id:
        raise ValueError("--observed-behavior/--target-setup require --setting so one observation is tied to one row")
    if bool(observed_behavior) != bool(target_setup):
        raise ValueError("--observed-behavior and --target-setup must be supplied together")
    smart_remote_values = (
        smart_remote_method,
        smart_remote_requested_value,
        smart_remote_observed_before,
        smart_remote_observed_after,
    )
    if any(smart_remote_values) and route != "smart_remote_wifi":
        raise ValueError("Smart Remote observed setter fields are only valid for smart_remote_wifi sessions")
    if any(smart_remote_values) and not setting_id:
        raise ValueError("Smart Remote observed setter fields require --setting so one proof is tied to one row")
    if bool(smart_remote_observed_before) != bool(smart_remote_observed_after):
        raise ValueError(
            "--smart-remote-observed-before and --smart-remote-observed-after must be supplied together"
        )
    if bool(smart_remote_method) != bool(smart_remote_requested_value):
        raise ValueError(
            "--smart-remote-method and --smart-remote-requested-value must be supplied together "
            "when replacing a placeholder method/value"
        )
    playbook = settings_sessions.build_route_playbook(
        route,
        registry=registry,
        setting_id=setting_id,
    )
    readiness_snapshot = settings_readiness.build_readiness_report(
        registry=registry,
        route=route,
        setting_id=setting_id,
        package_path=package_path,
        tool_resolver=readiness_tool_resolver,
        path_exists=readiness_path_exists,
    )
    missing_local_requirements = _missing_non_package_requirements(readiness_snapshot)
    stamp = f"{time.strftime('%Y%m%d-%H%M%S')}-{time.time_ns() % 1_000_000_000:09d}"
    session_dir = (out_dir or DEFAULT_OUT_DIR) / f"{stamp}-{_slug(route)}"
    logs_dir = session_dir / "logs"
    entries = []
    runner = command_runner or default_runner
    command_plan = (
        _commands_for_recommended_sequence(playbook)
        if recommended_sequence
        else _commands_for_phases(playbook, normalized_phases)
    )
    prior_sequence_blocked = False

    for index, (phase, command) in enumerate(command_plan, start=1):
        requires_hardware = command_requires_hardware(command)
        requires_package = command_requires_package(command)
        requires_observation = command_requires_observation(command)
        requires_camera_identity = command_requires_camera_identity(command)
        resolved_command = _resolve_command(
            command["command"],
            package_path,
            observed_state,
            observed_value,
            phone_state,
            observed_behavior,
            target_setup,
            smart_remote_method,
            smart_remote_requested_value,
            smart_remote_observed_before,
            smart_remote_observed_after,
            smart_remote_observed_behavior,
            camera_model,
            camera_port,
            camera_serial,
        )
        entry = {
            "index": index,
            "phase": phase,
            "route": route,
            "title": command.get("title") or command.get("probe_id") or f"{route} command",
            "command": resolved_command,
            "risk": command["risk"],
            "sequence_kind": command.get("sequence_kind"),
            "artifact_root": command["artifact_root"],
            "requires_hardware": requires_hardware,
            "requires_package": requires_package,
            "requires_observation": requires_observation,
            "requires_camera_identity": requires_camera_identity,
            "status": "planned",
            "returncode": None,
            "stdout_path": None,
            "stderr_path": None,
        }
        if execute:
            if recommended_sequence and prior_sequence_blocked:
                entry["status"] = "blocked_prior_sequence_step"
            elif requires_hardware and not allow_hardware:
                entry["status"] = "blocked_requires_hardware_approval"
            elif requires_camera_identity and (
                not camera_serial or command_requires_camera_identity({"command": resolved_command})
            ):
                # Never run a USB setting write without the selected body's serial for it to prove.
                entry["status"] = "blocked_requires_camera_serial"
            elif phase == "evidence" and missing_local_requirements:
                entry["status"] = "blocked_missing_local_readiness"
                entry["missing_readiness_requirements"] = missing_local_requirements
            elif requires_package and (package_path is None or not package_path.exists()):
                entry["status"] = "blocked_missing_local_package"
            elif requires_observation and not _has_required_observation(
                command,
                route=route,
                observed_state=observed_state,
                observed_value=observed_value,
                phone_state=phone_state,
                observed_behavior=observed_behavior,
                target_setup=target_setup,
                smart_remote_method=smart_remote_method,
                smart_remote_requested_value=smart_remote_requested_value,
                smart_remote_observed_before=smart_remote_observed_before,
                smart_remote_observed_after=smart_remote_observed_after,
                smart_remote_observed_behavior=smart_remote_observed_behavior,
            ):
                entry["status"] = "blocked_needs_operator_observation"
            else:
                result = runner(resolved_command, REPO_ROOT)
                entry["returncode"] = result.returncode
                entry["status"] = "passed" if result.returncode == 0 else "failed"
                entry["stdout_path"] = _write_text(
                    logs_dir / f"{index:02d}-{phase}-stdout.txt",
                    result.stdout,
                )
                entry["stderr_path"] = _write_text(
                    logs_dir / f"{index:02d}-{phase}-stderr.txt",
                    result.stderr,
                )
            if recommended_sequence and entry["status"] not in {"passed"}:
                prior_sequence_blocked = True
        entries.append(entry)

    session_dir.mkdir(parents=True, exist_ok=True)
    manifest_path = session_dir / "session-manifest.json"
    readiness_path = session_dir / "session-readiness.json"
    readiness_path.write_text(json.dumps(readiness_snapshot, indent=2, sort_keys=True), encoding="utf-8")
    summary = _summarize(entries)
    aftermath = _aftermath(
        route=route,
        setting_id=setting_id,
        normalized_phases=normalized_phases,
        recommended_sequence=recommended_sequence,
        execute=execute,
        allow_hardware=allow_hardware,
        package_path=package_path,
        readiness_snapshot=readiness_snapshot,
        entries=entries,
    )
    manifest = {
        "timestamp": stamp,
        "route": route,
        "setting_id": setting_id,
        "phases": normalized_phases,
        "recommended_sequence": recommended_sequence,
        "execute": execute,
        "allow_hardware": allow_hardware,
        "package_path": str(package_path) if package_path else None,
        "observed_state": observed_state,
        "observed_value": observed_value,
        "phone_state": phone_state,
        "observed_behavior": observed_behavior,
        "target_setup": target_setup,
        "smart_remote_method": smart_remote_method,
        "smart_remote_requested_value": smart_remote_requested_value,
        "smart_remote_observed_before": smart_remote_observed_before,
        "smart_remote_observed_after": smart_remote_observed_after,
        "smart_remote_observed_behavior": smart_remote_observed_behavior,
        "camera_model": camera_model,
        "camera_port": camera_port,
        "camera_serial": camera_serial,
        "session_dir": str(session_dir),
        "manifest_path": str(manifest_path),
        "readiness_path": str(readiness_path),
        "readiness_snapshot": readiness_snapshot,
        "summary": summary,
        "aftermath": aftermath,
        "playbook_summary": playbook["summary"],
        "playbook_next_concrete_command": playbook.get("next_concrete_command"),
        "commands": entries,
    }
    manifest_path.write_text(json.dumps(manifest, indent=2, sort_keys=True), encoding="utf-8")
    summary_path = session_dir / "session-summary.md"
    summary_path.write_text(_markdown(manifest), encoding="utf-8")
    manifest["summary_path"] = str(summary_path)
    manifest_path.write_text(json.dumps(manifest, indent=2, sort_keys=True), encoding="utf-8")
    return manifest
