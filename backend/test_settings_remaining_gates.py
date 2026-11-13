"""Camera-free tests for the structured remaining-gates report."""

from __future__ import annotations

import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "backend"))

import settings_remaining_gates


def test_remaining_gates_report_has_exact_commands_for_every_gate_row() -> None:
    report = settings_remaining_gates.build_report()
    summary = report["summary"]

    assert summary["total_settings"] >= 150
    assert summary["remaining_gate_rows"] == len(report["gate_rows"])
    assert summary["returned_gate_rows"] == summary["remaining_gate_rows"]
    assert summary["missing_probe_command_rows"] == []
    assert all(row["exact_next_commands"] for row in report["gate_rows"])
    assert all(row["next_concrete_command"] for row in report["gate_rows"])
    assert all(row["recommended_sequence_session_command"] for row in report["gate_rows"])
    print("  remaining gates exact command coverage: ok")


def test_remaining_gates_report_marks_admin_and_physical_rows() -> None:
    rows = {row["setting_id"]: row for row in settings_remaining_gates.build_report()["gate_rows"]}

    setup_format = rows["setup.format"]
    assert setup_format["gate_category"] == "dangerous_admin_guarded"
    assert setup_format["requires_admin_observation_only_ack"] is True
    assert "--admin-observation-only" in setup_format["guided_physical_command"]
    assert setup_format["next_concrete_command"] == setup_format["guided_physical_command"]
    assert setup_format["recommended_sequence_session_command"] == (
        "python3 tools/a7ii_live_session_runner.py --route guided_physical "
        "--setting setup.format --recommended-sequence"
    )
    assert any(command["risk"] == "manual_admin_observation" for command in setup_format["proof_commands"])

    usb_connection = rows["setup.usb_connection"]
    assert usb_connection["gate_category"] == "physical_or_guided"
    assert usb_connection["requires_admin_observation_only_ack"] is False
    assert "--admin-observation-only" not in usb_connection["guided_physical_command"]
    assert usb_connection["next_concrete_command"] == usb_connection["guided_physical_command"]
    print("  remaining gates admin/physical classification: ok")


def test_remaining_gates_best_next_command_prefers_specific_proofs() -> None:
    rows = {row["setting_id"]: row for row in settings_remaining_gates.build_report()["gate_rows"]}

    raw_file_type = rows["camera.raw_file_type"]
    assert raw_file_type["gate_category"] == "route_gap"
    assert "--setting camera.raw_file_type" in raw_file_type["next_concrete_command"]
    assert "--all-route-gaps" not in raw_file_type["next_concrete_command"]
    assert raw_file_type["recommended_sequence_session_command"] == (
        "python3 tools/a7ii_live_session_runner.py --route camera_remote_command "
        "--setting camera.raw_file_type --recommended-sequence"
    )
    assert raw_file_type["exact_next_commands"][0] == raw_file_type[
        "recommended_sequence_session_command"
    ]

    assert "custom.movie_button" not in rows

    audio_rec_level = rows["camera.audio_rec_level"]
    assert audio_rec_level["gate_category"] == "state_gated"
    assert audio_rec_level["next_concrete_command"] == audio_rec_level["guided_physical_command"]
    assert "--setting camera.audio_rec_level" in audio_rec_level["next_concrete_command"]
    assert audio_rec_level["recommended_sequence_session_command"] == (
        "python3 tools/a7ii_live_session_runner.py --route guided_physical "
        "--setting camera.audio_rec_level --recommended-sequence"
    )
    print("  remaining gates best next concrete command: ok")


def test_remaining_gates_report_filters_for_api_use() -> None:
    by_setting = settings_remaining_gates.build_report(setting_id="setup.format")
    assert by_setting["summary"]["returned_gate_rows"] == 1
    assert by_setting["summary"]["returned_probe_commands"] == len(by_setting["route_commands"])
    assert by_setting["summary"]["returned_by_gate_category"] == {"dangerous_admin_guarded": 1}
    assert by_setting["gate_rows"][0]["setting_id"] == "setup.format"
    assert all(
        command["setting_ids"] == ["setup.format"]
        for command in by_setting["route_commands"]
    )

    dangerous = settings_remaining_gates.build_report(
        gate_category="dangerous_admin_guarded",
        limit=2,
    )
    assert dangerous["summary"]["returned_gate_rows"] == 2
    assert all(row["gate_category"] == "dangerous_admin_guarded" for row in dangerous["gate_rows"])

    markdown = settings_remaining_gates.to_markdown(by_setting)
    assert "returned_gate_rows: `1`" in markdown
    assert "### dangerous_admin_guarded" in markdown
    assert "recommended_sequence:" in markdown
    assert "next_concrete:" in markdown
    assert "setup.format" in markdown
    assert "setup.setting_reset" not in markdown
    print("  remaining gates filters: ok")


if __name__ == "__main__":
    test_remaining_gates_report_has_exact_commands_for_every_gate_row()
    test_remaining_gates_report_marks_admin_and_physical_rows()
    test_remaining_gates_best_next_command_prefers_specific_proofs()
    test_remaining_gates_report_filters_for_api_use()
    print("settings_remaining_gates tests: PASS")
