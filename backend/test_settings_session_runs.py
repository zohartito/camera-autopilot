"""Camera-free tests for audited A7 II live session manifests."""

from __future__ import annotations

import shlex
import sys
import tempfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "backend"))

import settings_registry
import settings_session_runs


def _raw_ptp_probe_registry() -> dict:
    registry = settings_registry.load_registry()
    rows = settings_registry.settings_by_id(registry)
    rows["camera.focus_area"]["routes"]["raw_ptp"]["status"] = "protocol_ok_needs_behavior"
    rows["camera.focus_settings"]["routes"]["raw_ptp"]["status"] = "protocol_ok_needs_behavior"
    return registry


USB_MODEL = "Sony ILCE-7M2 (Control)"
USB_PORT = "usb:001,004"
USB_SERIAL = "TESTSERIAL01"


def _usb_set_read_revert_registry() -> dict:
    """A registry whose ISO row still needs its USB set/read/revert proof."""
    registry = settings_registry.load_registry()
    settings_registry.settings_by_id(registry)["camera.iso"]["routes"]["usb_gphoto"]["status"] = "available_not_wrapped"
    return registry


def _usb_set_read_revert_session(tmp: str, calls: list[str], **kwargs) -> dict:
    def fake_runner(command: str, cwd: Path) -> settings_session_runs.CommandResult:
        calls.append(command)
        return settings_session_runs.CommandResult(0, "", "")

    return settings_session_runs.run_session(
        route="usb_gphoto",
        setting_id="camera.iso",
        phases=["evidence"],
        execute=True,
        allow_hardware=True,
        out_dir=Path(tmp),
        registry=_usb_set_read_revert_registry(),
        command_runner=fake_runner,
        readiness_tool_resolver=lambda tool: f"/usr/bin/{tool}",
        **kwargs,
    )


def test_session_runner_dry_run_writes_manifest_without_executing() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        manifest = settings_session_runs.run_session(
            route="usb_gphoto",
            phases=["review", "refresh"],
            out_dir=Path(tmp),
        )
        assert Path(manifest["manifest_path"]).exists()
        assert Path(manifest["summary_path"]).exists()
        assert Path(manifest["readiness_path"]).exists()

    assert manifest["execute"] is False
    assert manifest["readiness_snapshot"]["routes"][0]["route"] == "usb_gphoto"
    assert manifest["readiness_snapshot"]["routes"][0]["summary"]["selected_gap_rows"] >= 1
    assert manifest["summary"]["planned"] == manifest["summary"]["commands"]
    assert manifest["summary"]["executed"] == 0
    assert manifest["summary"]["blocked"] == 0
    assert all(entry["status"] == "planned" for entry in manifest["commands"])
    assert manifest["aftermath"]["outcome"] == "planned_only"
    assert manifest["aftermath"]["next_commands"] == [
        "python3 tools/a7ii_live_session_runner.py --route usb_gphoto --phase review --phase refresh --execute"
    ]
    assert manifest["aftermath"]["first_unresolved"]["status"] == "planned"
    print("  session dry-run manifest: ok")


def test_session_runner_executes_local_commands_with_injected_runner() -> None:
    calls = []

    def fake_runner(command: str, cwd: Path) -> settings_session_runs.CommandResult:
        calls.append((command, cwd))
        return settings_session_runs.CommandResult(0, "stdout ok\n", "")

    with tempfile.TemporaryDirectory() as tmp:
        manifest = settings_session_runs.run_session(
            route="usb_gphoto",
            phases=["review", "refresh"],
            execute=True,
            out_dir=Path(tmp),
            command_runner=fake_runner,
        )
        assert all(Path(entry["stdout_path"]).exists() for entry in manifest["commands"])

    assert manifest["summary"]["executed"] == manifest["summary"]["commands"]
    assert manifest["summary"]["failed"] == 0
    assert len(calls) == manifest["summary"]["commands"]
    assert all(entry["status"] == "passed" for entry in manifest["commands"])
    assert manifest["aftermath"]["outcome"] == "session_complete"
    assert manifest["aftermath"]["next_commands"] == ["python3 tools/a7ii_completion_audit.py --json"]
    print("  session local execution manifest: ok")


def test_session_runner_blocks_hardware_without_approval() -> None:
    calls = []

    def fake_runner(command: str, cwd: Path) -> settings_session_runs.CommandResult:
        calls.append(command)
        return settings_session_runs.CommandResult(0, "", "")

    with tempfile.TemporaryDirectory() as tmp:
        manifest = settings_session_runs.run_session(
            route="usb_gphoto",
            phases=["evidence"],
            execute=True,
            allow_hardware=False,
            out_dir=Path(tmp),
            command_runner=fake_runner,
        )

    assert manifest["summary"]["blocked"] == manifest["summary"]["commands"]
    assert manifest["summary"]["executed"] == 0
    assert calls == []
    assert all(entry["status"] == "blocked_requires_hardware_approval" for entry in manifest["commands"])
    assert manifest["aftermath"]["outcome"] == "needs_hardware_approval"
    assert manifest["aftermath"]["blocked_commands"][0]["status"] == "blocked_requires_hardware_approval"
    assert manifest["aftermath"]["next_commands"] == [
        "python3 tools/a7ii_live_session_runner.py --route usb_gphoto --phase evidence --execute --allow-hardware"
    ]
    print("  session hardware block: ok")


def test_session_runner_scoped_evidence_uses_one_concrete_command() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        manifest = settings_session_runs.run_session(
            route="usb_gphoto",
            setting_id="camera.movie_mode",
            phases=["evidence"],
            out_dir=Path(tmp),
        )

    assert manifest["setting_id"] == "camera.movie_mode"
    assert manifest["playbook_next_concrete_command"]
    assert manifest["summary"]["commands"] == 1
    command = manifest["commands"][0]["command"]
    assert "a7ii_usb_movie_record_proof.py --setting camera.movie_mode" in command
    assert "--allow-camera-record" in command
    assert "a7ii_usb_state_matrix.py" not in command
    assert "--all-route-gaps" not in command
    assert manifest["commands"][0]["status"] == "planned"
    assert manifest["aftermath"]["outcome"] == "planned_only"
    assert manifest["aftermath"]["pending_artifact_roots"] == ["artifacts/falsification/r2_usb_movie_record"]
    print("  session scoped evidence concrete command: ok")


def test_session_runner_evidence_passed_points_to_review_refresh() -> None:
    calls = []

    def fake_tool(tool: str) -> str | None:
        return f"/usr/bin/{tool}"

    def fake_runner(command: str, cwd: Path) -> settings_session_runs.CommandResult:
        calls.append(command)
        return settings_session_runs.CommandResult(0, "evidence ok\n", "")

    with tempfile.TemporaryDirectory() as tmp:
        manifest = settings_session_runs.run_session(
            route="usb_gphoto",
            setting_id="camera.movie_mode",
            phases=["evidence"],
            execute=True,
            allow_hardware=True,
            out_dir=Path(tmp),
            command_runner=fake_runner,
            readiness_tool_resolver=fake_tool,
        )

    assert manifest["summary"]["executed"] == 1
    assert manifest["summary"]["blocked"] == 0
    assert len(calls) == 1
    assert manifest["aftermath"]["outcome"] == "evidence_captured_needs_review"
    assert manifest["aftermath"]["produced_artifact_roots"] == ["artifacts/falsification/r2_usb_movie_record"]
    assert manifest["aftermath"]["next_commands"] == [
        "python3 tools/a7ii_live_session_runner.py --route usb_gphoto --phase review --phase refresh --execute"
    ]
    print("  session evidence aftermath review pointer: ok")


def test_session_runner_recommended_sequence_uses_direct_proof_runbook() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        manifest = settings_session_runs.run_session(
            route="usb_gphoto",
            recommended_sequence=True,
            out_dir=Path(tmp),
        )

    assert manifest["recommended_sequence"] is True
    assert manifest["phases"] == ["recommended_sequence"]
    assert manifest["summary"]["planned"] == manifest["summary"]["commands"]
    assert [entry["sequence_kind"] for entry in manifest["commands"][:2]] == [
        "route_prerequisite_probe",
        "one_row_direct_evidence",
    ]
    assert "a7ii_usb_state_matrix.py" in manifest["commands"][0]["command"]
    assert "a7ii_usb_movie_record_proof.py --setting camera.movie_mode" in manifest["commands"][1][
        "command"
    ]
    assert any(entry["sequence_kind"] == "review_after_evidence" for entry in manifest["commands"])
    assert any(entry["sequence_kind"] == "refresh_after_evidence" for entry in manifest["commands"])
    print("  session recommended sequence direct runbook: ok")


def test_session_runner_scoped_recommended_sequence_keeps_route_probe_label() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        manifest = settings_session_runs.run_session(
            route="usb_gphoto",
            setting_id="camera.raw_file_type",
            recommended_sequence=True,
            out_dir=Path(tmp),
    )

    assert manifest["setting_id"] == "camera.raw_file_type"
    assert [entry["sequence_kind"] for entry in manifest["commands"][:2]] == [
        "route_prerequisite_probe",
        "review_after_evidence",
    ]
    assert "a7ii_usb_state_matrix.py" in manifest["commands"][0]["command"]
    assert "review_usb_gphoto_artifacts.py" in manifest["commands"][1]["command"]
    assert all(
        "a7ii_usb_set_read_revert.py --setting camera.raw_file_type" not in entry["command"]
        for entry in manifest["commands"]
    )
    print("  session scoped recommended sequence labels: ok")


def test_session_runner_recommended_sequence_labels_guided_physical_as_one_row() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        manifest = settings_session_runs.run_session(
            route="guided_physical",
            recommended_sequence=True,
            out_dir=Path(tmp),
        )

    assert manifest["summary"]["commands"] == 2
    assert manifest["commands"][0]["sequence_kind"] == "one_row_direct_evidence"
    assert "--setting application.introduction" in manifest["commands"][0]["command"]
    assert manifest["commands"][1]["sequence_kind"] == "refresh_after_evidence"
    print("  session recommended sequence guided physical label: ok")


def test_session_runner_recommended_sequence_blocks_after_prior_block() -> None:
    calls = []

    def fake_runner(command: str, cwd: Path) -> settings_session_runs.CommandResult:
        calls.append(command)
        return settings_session_runs.CommandResult(0, "", "")

    with tempfile.TemporaryDirectory() as tmp:
        manifest = settings_session_runs.run_session(
            route="usb_gphoto",
            recommended_sequence=True,
            execute=True,
            allow_hardware=False,
            out_dir=Path(tmp),
            command_runner=fake_runner,
        )

    assert manifest["summary"]["hardware_blocked"] == 1
    assert manifest["summary"]["prior_step_blocked"] == manifest["summary"]["commands"] - 1
    assert manifest["summary"]["executed"] == 0
    assert calls == []
    assert manifest["commands"][0]["status"] == "blocked_requires_hardware_approval"
    assert all(entry["status"] == "blocked_prior_sequence_step" for entry in manifest["commands"][1:])
    assert manifest["aftermath"]["outcome"] == "needs_hardware_approval"
    assert manifest["aftermath"]["next_commands"] == [
        "python3 tools/a7ii_live_session_runner.py --route usb_gphoto --recommended-sequence --execute --allow-hardware"
    ]
    print("  session recommended sequence prior block: ok")


def test_session_runner_blocks_evidence_when_local_readiness_is_missing() -> None:
    calls = []

    def missing_gphoto(tool: str) -> str | None:
        if tool == "gphoto2":
            return None
        return f"/usr/bin/{tool}"

    def fake_runner(command: str, cwd: Path) -> settings_session_runs.CommandResult:
        calls.append(command)
        return settings_session_runs.CommandResult(0, "", "")

    with tempfile.TemporaryDirectory() as tmp:
        manifest = settings_session_runs.run_session(
            route="usb_gphoto",
            phases=["evidence"],
            execute=True,
            allow_hardware=True,
            out_dir=Path(tmp),
            command_runner=fake_runner,
            readiness_tool_resolver=missing_gphoto,
        )

    assert manifest["summary"]["readiness_blocked"] == manifest["summary"]["commands"]
    assert manifest["summary"]["blocked"] == manifest["summary"]["commands"]
    assert manifest["summary"]["executed"] == 0
    assert calls == []
    assert all(entry["status"] == "blocked_missing_local_readiness" for entry in manifest["commands"])
    assert all(
        any(item["name"] == "gphoto2" for item in entry["missing_readiness_requirements"])
        for entry in manifest["commands"]
    )
    assert manifest["aftermath"]["outcome"] == "needs_local_readiness"
    assert manifest["aftermath"]["first_unresolved"]["status"] == "blocked_missing_local_readiness"
    print("  session local readiness block: ok")


def test_session_runner_treats_local_package_review_as_non_hardware() -> None:
    calls = []

    def fake_runner(command: str, cwd: Path) -> settings_session_runs.CommandResult:
        calls.append(command)
        return settings_session_runs.CommandResult(0, "package review ok\n", "")

    with tempfile.TemporaryDirectory() as tmp:
        package_path = Path(tmp) / "CameraRemoteCommand-package.zip"
        package_path.write_bytes(b"fake package for runner test")
        manifest = settings_session_runs.run_session(
            route="camera_remote_command",
            phases=["evidence"],
            execute=True,
            allow_hardware=False,
            out_dir=Path(tmp),
            package_path=package_path,
            command_runner=fake_runner,
        )

    assert manifest["summary"]["blocked"] == 0
    assert manifest["summary"]["executed"] == manifest["summary"]["commands"]
    assert len(calls) == manifest["summary"]["commands"]
    assert any("find_camera_remote_command_package.py" in command for command in calls)
    review_calls = [command for command in calls if "review_camera_remote_command_package.py" in command]
    assert len(review_calls) >= 2
    assert any("--setting camera.audio_out_timing" in command for command in review_calls)
    assert all("/path/to/CameraRemoteCommand-package.zip" not in command for command in review_calls)
    assert all("CameraRemoteCommand-package.zip" in command for command in review_calls)
    assert all(entry["requires_hardware"] is False for entry in manifest["commands"])
    assert sum(1 for entry in manifest["commands"] if entry["requires_package"]) == len(review_calls)
    assert all(entry["status"] == "passed" for entry in manifest["commands"])
    print("  session local package review: ok")


def test_session_runner_blocks_missing_local_package_without_running() -> None:
    calls = []

    def fake_runner(command: str, cwd: Path) -> settings_session_runs.CommandResult:
        calls.append(command)
        return settings_session_runs.CommandResult(0, "", "")

    with tempfile.TemporaryDirectory() as tmp:
        manifest = settings_session_runs.run_session(
            route="camera_remote_command",
            phases=["evidence"],
            execute=True,
            allow_hardware=False,
            out_dir=Path(tmp),
            package_path=Path(tmp) / "missing.zip",
            command_runner=fake_runner,
        )

    assert manifest["summary"]["package_blocked"] >= 1
    assert manifest["summary"]["blocked"] == manifest["summary"]["package_blocked"]
    assert manifest["summary"]["executed"] == 1
    assert len(calls) == 1
    assert "find_camera_remote_command_package.py" in calls[0]
    statuses = {entry["title"]: entry["status"] for entry in manifest["commands"]}
    assert statuses["Sony Camera Remote Command local package locator"] == "passed"
    assert statuses["Sony Camera Remote Command package compatibility review"] == "blocked_missing_local_package"
    assert statuses["Sony Camera Remote Command one-setting package review: Audio Out Timing"] == (
        "blocked_missing_local_package"
    )
    assert manifest["aftermath"]["outcome"] == "needs_package_path"
    assert any("--package-path /path/to/CameraRemoteCommand-package.zip" in command for command in manifest["aftermath"]["next_commands"])
    print("  session missing package block: ok")


def test_session_runner_blocks_guided_physical_without_observation() -> None:
    calls = []

    def fake_runner(command: str, cwd: Path) -> settings_session_runs.CommandResult:
        calls.append(command)
        return settings_session_runs.CommandResult(0, "", "")

    with tempfile.TemporaryDirectory() as tmp:
        manifest = settings_session_runs.run_session(
            route="guided_physical",
            setting_id="setup.usb_connection",
            phases=["evidence"],
            execute=True,
            allow_hardware=True,
            out_dir=Path(tmp),
            command_runner=fake_runner,
        )

    assert manifest["summary"]["observation_blocked"] == 1
    assert manifest["readiness_snapshot"]["routes"][0]["summary"]["local_status"] == "ready"
    assert manifest["summary"]["blocked"] == 1
    assert manifest["summary"]["executed"] == 0
    assert calls == []
    entry = manifest["commands"][0]
    assert entry["requires_observation"] is True
    assert entry["status"] == "blocked_needs_operator_observation"
    assert "<describe camera/menu state>" in entry["command"]
    assert manifest["aftermath"]["outcome"] == "needs_operator_observation"
    assert "--observed-state" in manifest["aftermath"]["next_commands"][0]
    print("  session guided physical observation block: ok")


def test_session_runner_can_execute_one_guided_physical_observation() -> None:
    calls = []

    def fake_runner(command: str, cwd: Path) -> settings_session_runs.CommandResult:
        calls.append(command)
        return settings_session_runs.CommandResult(0, "physical proof ok\n", "")

    observed_state = "Setup page 4 shows USB Connection set to PC Remote."
    with tempfile.TemporaryDirectory() as tmp:
        manifest = settings_session_runs.run_session(
            route="guided_physical",
            setting_id="setup.usb_connection",
            phases=["evidence"],
            execute=True,
            allow_hardware=True,
            observed_state=observed_state,
            out_dir=Path(tmp),
            command_runner=fake_runner,
        )

    assert manifest["summary"]["blocked"] == 0
    assert manifest["summary"]["executed"] == 1
    assert len(calls) == 1
    assert observed_state in calls[0]
    assert "<describe camera/menu state>" not in calls[0]
    entry = manifest["commands"][0]
    assert entry["status"] == "passed"
    assert entry["requires_observation"] is True
    assert observed_state in entry["command"]
    print("  session guided physical observation execution: ok")


def test_session_runner_blocks_admin_guided_physical_without_observation() -> None:
    calls = []

    def fake_runner(command: str, cwd: Path) -> settings_session_runs.CommandResult:
        calls.append(command)
        return settings_session_runs.CommandResult(0, "", "")

    with tempfile.TemporaryDirectory() as tmp:
        manifest = settings_session_runs.run_session(
            route="guided_physical",
            setting_id="setup.format",
            phases=["evidence"],
            execute=True,
            allow_hardware=True,
            out_dir=Path(tmp),
            command_runner=fake_runner,
        )

    assert manifest["summary"]["observation_blocked"] == 1
    assert manifest["summary"]["executed"] == 0
    assert calls == []
    entry = manifest["commands"][0]
    assert entry["risk"] == "manual_admin_observation"
    assert entry["requires_observation"] is True
    assert entry["status"] == "blocked_needs_operator_observation"
    assert "--admin-observation-only" in entry["command"]
    print("  session admin guided physical observation block: ok")


def test_session_runner_can_execute_one_admin_guided_physical_observation() -> None:
    calls = []

    def fake_runner(command: str, cwd: Path) -> settings_session_runs.CommandResult:
        calls.append(command)
        return settings_session_runs.CommandResult(0, "admin physical proof ok\n", "")

    observed_state = "Setup page 5 shows Format; operator did not enter or confirm it."
    with tempfile.TemporaryDirectory() as tmp:
        manifest = settings_session_runs.run_session(
            route="guided_physical",
            setting_id="setup.format",
            phases=["evidence"],
            execute=True,
            allow_hardware=True,
            observed_state=observed_state,
            out_dir=Path(tmp),
            command_runner=fake_runner,
        )

    assert manifest["summary"]["blocked"] == 0
    assert manifest["summary"]["executed"] == 1
    assert len(calls) == 1
    assert observed_state in calls[0]
    assert "--admin-observation-only" in calls[0]
    assert "<describe camera/menu state>" not in calls[0]
    entry = manifest["commands"][0]
    assert entry["status"] == "passed"
    assert entry["requires_observation"] is True
    assert entry["risk"] == "manual_admin_observation"
    print("  session admin guided physical observation execution: ok")


def test_session_runner_blocks_phone_preference_without_observation() -> None:
    calls = []

    def fake_runner(command: str, cwd: Path) -> settings_session_runs.CommandResult:
        calls.append(command)
        return settings_session_runs.CommandResult(0, "smart remote api proof ok\n", "")

    with tempfile.TemporaryDirectory() as tmp:
        manifest = settings_session_runs.run_session(
            route="smart_remote_phone",
            setting_id="phone.grid_line",
            phases=["evidence"],
            execute=True,
            allow_hardware=True,
            out_dir=Path(tmp),
            command_runner=fake_runner,
    )

    assert manifest["summary"]["observation_blocked"] == 1
    assert manifest["summary"]["commands"] == 1
    assert manifest["summary"]["executed"] == 0
    assert calls == []
    blocked = [entry for entry in manifest["commands"] if entry["status"] == "blocked_needs_operator_observation"]
    assert len(blocked) == 1
    assert blocked[0]["risk"] == "phone_manual_observation"
    assert blocked[0]["requires_observation"] is True
    assert "<visible phone-app value>" in blocked[0]["command"]
    print("  session phone preference observation block: ok")


def test_session_runner_can_execute_one_phone_preference_observation() -> None:
    calls = []

    def fake_runner(command: str, cwd: Path) -> settings_session_runs.CommandResult:
        calls.append(command)
        return settings_session_runs.CommandResult(0, "phone proof ok\n", "")

    with tempfile.TemporaryDirectory() as tmp:
        manifest = settings_session_runs.run_session(
            route="smart_remote_phone",
            setting_id="phone.grid_line",
            phases=["evidence"],
            execute=True,
            allow_hardware=True,
            observed_value="Rule of 3rds Grid",
            phone_state="Smart Remote phone settings screen is open.",
            out_dir=Path(tmp),
            command_runner=fake_runner,
        )

    assert manifest["summary"]["blocked"] == 0
    assert manifest["summary"]["executed"] == manifest["summary"]["commands"]
    assert manifest["summary"]["commands"] == 1
    assert len(calls) == manifest["summary"]["commands"]
    assert any("Rule of 3rds Grid" in command for command in calls)
    assert any("Smart Remote phone settings screen is open." in command for command in calls)
    assert all("<visible phone-app value>" not in command for command in calls)
    print("  session phone preference observation execution: ok")


def test_session_runner_blocks_smart_remote_behavior_without_observation() -> None:
    calls = []

    def fake_runner(command: str, cwd: Path) -> settings_session_runs.CommandResult:
        calls.append(command)
        return settings_session_runs.CommandResult(0, "smart remote command ok\n", "")

    with tempfile.TemporaryDirectory() as tmp:
        manifest = settings_session_runs.run_session(
            route="smart_remote_wifi",
            setting_id="camera.aspect_ratio",
            phases=["evidence"],
            execute=True,
            allow_hardware=True,
            out_dir=Path(tmp),
            command_runner=fake_runner,
        )

    assert manifest["summary"]["observation_blocked"] == 1
    assert manifest["summary"]["commands"] == 1
    assert manifest["summary"]["executed"] == 0
    blocked = [entry for entry in manifest["commands"] if entry["status"] == "blocked_needs_operator_observation"]
    assert len(blocked) == 1
    assert blocked[0]["risk"] == "smart_remote_behavior_review"
    assert "<observed before value>" in blocked[0]["command"]
    assert calls == []
    print("  session Smart Remote behavior observation block: ok")


def test_session_runner_can_execute_one_smart_remote_behavior_observation() -> None:
    calls = []

    def fake_runner(command: str, cwd: Path) -> settings_session_runs.CommandResult:
        calls.append(command)
        return settings_session_runs.CommandResult(0, "smart remote command ok\n", "")

    with tempfile.TemporaryDirectory() as tmp:
        manifest = settings_session_runs.run_session(
            route="smart_remote_wifi",
            setting_id="camera.aspect_ratio",
            phases=["evidence"],
            execute=True,
            allow_hardware=True,
            smart_remote_observed_before="3:2 / L:24M",
            smart_remote_observed_after="16:9 / L:20M",
            smart_remote_observed_behavior="getEvent reflected the alternate still size pair after the setter.",
            out_dir=Path(tmp),
            command_runner=fake_runner,
        )

    assert manifest["summary"]["blocked"] == 0
    assert manifest["summary"]["executed"] == manifest["summary"]["commands"]
    assert manifest["summary"]["commands"] == 1
    assert any("setStillSize" in command for command in calls)
    assert any("--auto-value first-available-alternate" in command for command in calls)
    assert any("3:2 / L:24M" in command and "16:9 / L:20M" in command for command in calls)
    assert all("<value sent>" not in command for command in calls)
    assert all("<observed before value>" not in command for command in calls)
    print("  session Smart Remote behavior observation execution: ok")


def test_session_runner_blocks_raw_ptp_visual_proof_without_observation() -> None:
    calls = []

    def fake_runner(command: str, cwd: Path) -> settings_session_runs.CommandResult:
        calls.append(command)
        return settings_session_runs.CommandResult(0, "raw ptp command ok\n", "")

    def fake_tool(tool: str) -> str | None:
        return f"/usr/bin/{tool}"

    with tempfile.TemporaryDirectory() as tmp:
        manifest = settings_session_runs.run_session(
            route="raw_ptp",
            setting_id="camera.focus_area",
            phases=["evidence"],
            execute=True,
            allow_hardware=True,
            out_dir=Path(tmp),
            command_runner=fake_runner,
            readiness_tool_resolver=fake_tool,
            readiness_path_exists=lambda path: True,
            registry=_raw_ptp_probe_registry(),
        )

    assert manifest["summary"]["observation_blocked"] == 1
    assert manifest["summary"]["commands"] == 1
    assert manifest["summary"]["executed"] == 0
    blocked = [entry for entry in manifest["commands"] if entry["status"] == "blocked_needs_operator_observation"]
    assert len(blocked) == 1
    assert blocked[0]["risk"] == "focus_visual_review"
    assert "<describe near/far visual behavior>" in blocked[0]["command"]
    assert calls == []
    print("  session raw PTP visual proof observation block: ok")


def test_session_runner_can_execute_one_raw_ptp_visual_proof_observation() -> None:
    calls = []

    def fake_runner(command: str, cwd: Path) -> settings_session_runs.CommandResult:
        calls.append(command)
        return settings_session_runs.CommandResult(0, "raw ptp command ok\n", "")

    def fake_tool(tool: str) -> str | None:
        return f"/usr/bin/{tool}"

    observed_behavior = "AF point A sharpened near target; point B sharpened far target."
    target_setup = "Near card at 0.5m and far card at 2m, fixed tripod."
    with tempfile.TemporaryDirectory() as tmp:
        manifest = settings_session_runs.run_session(
            route="raw_ptp",
            setting_id="camera.focus_area",
            phases=["evidence"],
            execute=True,
            allow_hardware=True,
            observed_behavior=observed_behavior,
            target_setup=target_setup,
            out_dir=Path(tmp),
            command_runner=fake_runner,
            readiness_tool_resolver=fake_tool,
            readiness_path_exists=lambda path: True,
            registry=_raw_ptp_probe_registry(),
        )

    assert manifest["summary"]["blocked"] == 0
    assert manifest["summary"]["executed"] == manifest["summary"]["commands"]
    assert manifest["summary"]["commands"] == 1
    assert any(observed_behavior in command for command in calls)
    assert any(target_setup in command for command in calls)
    assert all("<describe near/far visual behavior>" not in command for command in calls)
    print("  session raw PTP visual proof observation execution: ok")


def test_session_runner_rejects_broad_observed_state_reuse() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        try:
            settings_session_runs.run_session(
                route="guided_physical",
                phases=["evidence"],
                observed_state="One observation should not apply to every physical row.",
                out_dir=Path(tmp),
            )
        except ValueError as exc:
            assert "--setting" in str(exc)
        else:
            raise AssertionError("observed_state must be tied to one physical setting")
    print("  session guided physical broad observation guard: ok")


def test_session_runner_rejects_broad_phone_observation_reuse() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        try:
            settings_session_runs.run_session(
                route="smart_remote_phone",
                phases=["evidence"],
                observed_value="Rule of 3rds Grid",
                phone_state="Smart Remote phone settings screen is open.",
                out_dir=Path(tmp),
            )
        except ValueError as exc:
            assert "--setting" in str(exc)
        else:
            raise AssertionError("phone observations must be tied to one phone setting")
    print("  session phone broad observation guard: ok")


def test_session_runner_rejects_broad_raw_ptp_observation_reuse() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        try:
            settings_session_runs.run_session(
                route="raw_ptp",
                phases=["evidence"],
                observed_behavior="AF point visibly changed focus.",
                target_setup="Near/far target setup.",
                out_dir=Path(tmp),
            )
        except ValueError as exc:
            assert "--setting" in str(exc)
        else:
            raise AssertionError("raw PTP visual proof observations must be tied to one setting")
    print("  session raw PTP broad observation guard: ok")


def test_session_runner_rejects_broad_smart_remote_behavior_observation_reuse() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        try:
            settings_session_runs.run_session(
                route="smart_remote_wifi",
                phases=["evidence"],
                smart_remote_method="setFocusMode",
                smart_remote_requested_value="MF",
                smart_remote_observed_before="AF-S",
                smart_remote_observed_after="MF",
                out_dir=Path(tmp),
            )
        except ValueError as exc:
            assert "--setting" in str(exc)
        else:
            raise AssertionError("Smart Remote behavior proof observations must be tied to one setting")
    print("  session Smart Remote behavior broad observation guard: ok")


def test_session_runner_blocks_usb_setting_write_without_camera_serial() -> None:
    calls: list[str] = []
    with tempfile.TemporaryDirectory() as tmp:
        manifest = _usb_set_read_revert_session(tmp, calls)

    [entry] = manifest["commands"]
    assert "a7ii_usb_set_read_revert.py --setting camera.iso" in entry["command"]
    assert entry["requires_camera_identity"] is True
    assert entry["status"] == "blocked_requires_camera_serial"
    assert calls == []
    assert manifest["summary"]["identity_blocked"] == 1
    assert manifest["summary"]["blocked"] == 1
    assert manifest["aftermath"]["outcome"] == "needs_camera_serial"
    assert manifest["aftermath"]["next_commands"] == [
        "python3 tools/a7ii_live_session_runner.py --route usb_gphoto --setting camera.iso --phase evidence "
        "--execute --allow-hardware --camera-model '<camera model>' --camera-port '<camera port>' "
        "--camera-serial '<camera serial>'"
    ]
    assert manifest["aftermath"]["pending_artifact_roots"] == ["artifacts/falsification/r2_usb_set_read_revert"]
    print("  session USB setting write blocked without camera serial: ok")


def test_session_runner_supplies_selected_body_identity_to_usb_setting_write() -> None:
    calls: list[str] = []
    with tempfile.TemporaryDirectory() as tmp:
        manifest = _usb_set_read_revert_session(
            tmp, calls, camera_model=USB_MODEL, camera_port=USB_PORT, camera_serial=USB_SERIAL)

    [entry] = manifest["commands"]
    assert entry["status"] == "passed"
    assert calls == [entry["command"]]
    argv = shlex.split(calls[0])
    assert argv[argv.index("--model") + 1] == USB_MODEL
    assert argv[argv.index("--port") + 1] == USB_PORT
    assert argv[argv.index("--serial") + 1] == USB_SERIAL
    assert "<camera" not in calls[0]
    assert (manifest["camera_model"], manifest["camera_port"], manifest["camera_serial"]) == (
        USB_MODEL, USB_PORT, USB_SERIAL)
    print("  session USB setting write carries the selected body identity: ok")


def test_session_runner_substituted_values_cannot_add_arguments() -> None:
    # A value that contains another placeholder (here the package path) must stay one argv token.
    cases = (
        ({"camera_serial": "x /path/to/CameraRemoteCommand-package.zip"},
         Path("/repo/artifacts/p --serial TESTSERIAL01 --value camera.iso=12800")),
        ({"camera_model": "Sony \"ILCE\" 'M2' $HOME; --value camera.iso=12800"}, None),
    )
    for overrides, package_path in cases:
        identity = {"camera_model": USB_MODEL, "camera_port": USB_PORT, "camera_serial": USB_SERIAL, **overrides}
        calls: list[str] = []
        with tempfile.TemporaryDirectory() as tmp:
            _usb_set_read_revert_session(tmp, calls, package_path=package_path, **identity)

        argv = shlex.split(calls[0])
        assert argv.count("--model") == argv.count("--port") == argv.count("--serial") == 1
        assert "--value" not in argv
        assert argv[argv.index("--model") + 1] == identity["camera_model"]
        assert argv[argv.index("--serial") + 1] == identity["camera_serial"]
    print("  session substituted values stay single arguments: ok")


def test_session_runner_rejects_partial_or_placeholder_camera_identity() -> None:
    cases = (
        {"camera_model": USB_MODEL, "camera_port": USB_PORT},
        {"camera_serial": USB_SERIAL},
        {"camera_model": USB_MODEL, "camera_port": USB_PORT, "camera_serial": "<camera serial>"},
        {"camera_model": '"<camera serial>"', "camera_port": USB_PORT, "camera_serial": USB_SERIAL},
    )
    for identity in cases:
        calls: list[str] = []
        with tempfile.TemporaryDirectory() as tmp:
            try:
                _usb_set_read_revert_session(tmp, calls, **identity)
            except ValueError as exc:
                assert "camera_model, camera_port and camera_serial" in str(exc)
            else:
                raise AssertionError(f"partial or placeholder camera identity must be refused: {identity}")
            assert list(Path(tmp).iterdir()) == []
        assert calls == []
    print("  session partial camera identity guard: ok")


def test_session_runner_dry_run_keeps_identity_placeholders_for_the_operator() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        manifest = settings_session_runs.run_session(
            route="usb_gphoto",
            setting_id="camera.iso",
            phases=["evidence"],
            out_dir=Path(tmp),
            registry=_usb_set_read_revert_registry(),
        )

    [entry] = manifest["commands"]
    assert entry["status"] == "planned"
    assert '--serial "<camera serial>"' in entry["command"]
    assert manifest["aftermath"]["next_commands"][0].endswith(
        "--execute --allow-hardware --camera-model '<camera model>' --camera-port '<camera port>' "
        "--camera-serial '<camera serial>'"
    )
    print("  session dry-run USB identity placeholders: ok")


def _smart_remote_behavior_argv(setting_id: str, registry: dict | None = None, **observations) -> list[str]:
    calls: list[str] = []

    def fake_runner(command: str, cwd: Path) -> settings_session_runs.CommandResult:
        calls.append(command)
        return settings_session_runs.CommandResult(0, "", "")

    with tempfile.TemporaryDirectory() as tmp:
        settings_session_runs.run_session(
            route="smart_remote_wifi",
            setting_id=setting_id,
            phases=["evidence"],
            execute=True,
            allow_hardware=True,
            out_dir=Path(tmp),
            registry=registry,
            command_runner=fake_runner,
            readiness_tool_resolver=lambda tool: f"/usr/bin/{tool}",
            readiness_path_exists=lambda path: True,
            **observations,
        )
    [command] = [call for call in calls if "a7ii_smart_remote_set_observe.py" in call]
    return shlex.split(command)


def test_session_runner_single_quoted_placeholders_keep_values_whole() -> None:
    # Smart Remote templates come from shlex.join, which single-quotes each placeholder. Every value,
    # including one that contains another field's placeholder or its own, must stay one argv token.
    cases = (
        {"smart_remote_observed_before": "3:2 / L:24M", "smart_remote_observed_after": "16:9 --method actZoom"},
        {"smart_remote_observed_before": "prefix <observed after value>",
         "smart_remote_observed_after": "16:9 --method actZoom"},
        {"smart_remote_observed_before": "prefix <observed before value>", "smart_remote_observed_after": "16:9"},
        {"smart_remote_observed_before": "3:2", "smart_remote_observed_after": "prefix <describe camera-visible behavior>",
         "smart_remote_observed_behavior": "16:9 --method actZoom"},
    )
    for observations in cases:
        argv = _smart_remote_behavior_argv("camera.aspect_ratio", **observations)
        assert argv.count("--method") == 1, (observations, argv)
        assert argv[argv.index("--method") + 1] == "setStillSize"
        assert argv[argv.index("--observed-before") + 1] == observations["smart_remote_observed_before"]
        assert argv[argv.index("--observed-after") + 1] == observations["smart_remote_observed_after"]
    print("  session single-quoted placeholders keep values whole: ok")


def test_session_runner_single_quoted_requested_value_stays_one_argument() -> None:
    # Without a registered value strategy the behavior command asks for --value '<value sent>'.
    registry = settings_registry.load_registry()
    row = settings_registry.settings_by_id(registry)["camera.focus_area"]
    row["status"] = row["routes"]["smart_remote_wifi"]["status"] = "protocol_ok_needs_behavior"
    argv = _smart_remote_behavior_argv(
        "camera.focus_area",
        registry=registry,
        smart_remote_method="setTouchAFPosition",
        smart_remote_requested_value="1 --method actZoom",
        smart_remote_observed_before="3:2",
        smart_remote_observed_after="16:9",
    )
    assert argv.count("--method") == 1
    assert argv[argv.index("--method") + 1] == "setTouchAFPosition"
    assert argv[argv.index("--value") + 1] == "1 --method actZoom"
    print("  session single-quoted requested value stays one argument: ok")


def test_session_runner_uses_unique_session_directories() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        first = settings_session_runs.run_session(
            route="usb_gphoto",
            phases=["review"],
            out_dir=Path(tmp),
        )
        second = settings_session_runs.run_session(
            route="usb_gphoto",
            phases=["review"],
            out_dir=Path(tmp),
        )

    assert first["session_dir"] != second["session_dir"]
    assert first["manifest_path"] != second["manifest_path"]
    print("  session unique directories: ok")


def test_default_runner_rejects_invalid_or_empty_commands() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        cwd = Path(tmp)
        invalid = settings_session_runs.default_runner("python3 -c 'unterminated", cwd)
        empty = settings_session_runs.default_runner("   ", cwd)

    assert invalid.returncode == 2
    assert "invalid command quoting" in invalid.stderr
    assert empty.returncode == 2
    assert empty.stderr == "empty command"
    print("  session command parser fails closed: ok")


def test_default_runner_does_not_interpret_shell_metacharacters() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        cwd = Path(tmp)
        marker = cwd / "must-not-exist"
        command = f"""python3 -c "print('safe')" ; touch {marker}"""
        result = settings_session_runs.default_runner(command, cwd)

        assert result.returncode == 0
        assert result.stdout.strip() == "safe"
        assert not marker.exists()
    print("  session command runner bypasses shell metacharacters: ok")


if __name__ == "__main__":
    test_session_runner_dry_run_writes_manifest_without_executing()
    test_session_runner_executes_local_commands_with_injected_runner()
    test_session_runner_blocks_hardware_without_approval()
    test_session_runner_scoped_evidence_uses_one_concrete_command()
    test_session_runner_evidence_passed_points_to_review_refresh()
    test_session_runner_recommended_sequence_uses_direct_proof_runbook()
    test_session_runner_scoped_recommended_sequence_keeps_route_probe_label()
    test_session_runner_recommended_sequence_labels_guided_physical_as_one_row()
    test_session_runner_recommended_sequence_blocks_after_prior_block()
    test_session_runner_blocks_evidence_when_local_readiness_is_missing()
    test_session_runner_treats_local_package_review_as_non_hardware()
    test_session_runner_blocks_missing_local_package_without_running()
    test_session_runner_blocks_guided_physical_without_observation()
    test_session_runner_can_execute_one_guided_physical_observation()
    test_session_runner_blocks_admin_guided_physical_without_observation()
    test_session_runner_can_execute_one_admin_guided_physical_observation()
    test_session_runner_blocks_phone_preference_without_observation()
    test_session_runner_can_execute_one_phone_preference_observation()
    test_session_runner_blocks_smart_remote_behavior_without_observation()
    test_session_runner_can_execute_one_smart_remote_behavior_observation()
    test_session_runner_blocks_raw_ptp_visual_proof_without_observation()
    test_session_runner_can_execute_one_raw_ptp_visual_proof_observation()
    test_session_runner_rejects_broad_observed_state_reuse()
    test_session_runner_rejects_broad_phone_observation_reuse()
    test_session_runner_rejects_broad_raw_ptp_observation_reuse()
    test_session_runner_rejects_broad_smart_remote_behavior_observation_reuse()
    test_session_runner_blocks_usb_setting_write_without_camera_serial()
    test_session_runner_supplies_selected_body_identity_to_usb_setting_write()
    test_session_runner_substituted_values_cannot_add_arguments()
    test_session_runner_rejects_partial_or_placeholder_camera_identity()
    test_session_runner_dry_run_keeps_identity_placeholders_for_the_operator()
    test_session_runner_single_quoted_placeholders_keep_values_whole()
    test_session_runner_single_quoted_requested_value_stays_one_argument()
    test_session_runner_uses_unique_session_directories()
    test_default_runner_rejects_invalid_or_empty_commands()
    test_default_runner_does_not_interpret_shell_metacharacters()
    print("settings_session_runs tests: PASS")
