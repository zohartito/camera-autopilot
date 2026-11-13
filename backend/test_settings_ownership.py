"""Camera-free tests for the A7 II per-setting ownership audit."""

from __future__ import annotations

import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "backend"))
sys.path.insert(0, str(REPO_ROOT / "tools"))

import a7ii_settings_ownership_report
import settings_ownership


def test_ownership_report_has_no_unowned_or_commandless_gap_rows() -> None:
    report = settings_ownership.build_report()
    summary = report["summary"]

    assert summary["total_registry_settings"] >= 150
    assert summary["selected_settings"] == summary["total_registry_settings"]
    assert summary["unowned_settings"] == []
    assert summary["missing_exact_next_command_settings"] == []
    assert summary["settings_with_exact_next_commands"] == summary["selected_settings"]
    assert summary["ownership_states"]["remote_setter_owned"] >= 7
    assert summary["ownership_states"]["needs_live_evidence_with_command"] >= 50
    assert summary["ownership_states"]["physical_or_admin_gate_owned"] >= 30
    print("  settings ownership zero-unowned audit: ok")


def test_ownership_report_explains_remote_physical_and_package_rows() -> None:
    report = settings_ownership.build_report()
    rows = {item["setting_id"]: item for item in report["settings"]}

    iso = rows["camera.iso"]
    assert iso["ownership_state"] == "remote_setter_owned"
    assert iso["owned"] is True
    assert {route["route"] for route in iso["route_candidates"]} == {
        "smart_remote_wifi",
        "usb_gphoto",
    }
    assert iso["next_concrete_command"] is None
    assert iso["proof_artifacts"] == []

    aperture = rows["camera.aperture"]
    assert aperture["ownership_state"] == "remote_setter_owned"
    assert aperture["owned"] is True
    assert any(route["route"] == "smart_remote_wifi" for route in aperture["route_candidates"])

    panorama = rows["camera.panorama_size"]
    assert panorama["ownership_state"] == "physical_or_admin_gate_owned"
    assert "one_setting_and_observed_camera_state" in panorama["blockers"]
    assert panorama["next_concrete_command"]
    assert panorama["next_concrete_command"] == panorama["remaining_gate"]["next_concrete_command"]
    assert panorama["recommended_sequence_session_command"] == panorama["remaining_gate"][
        "recommended_sequence_session_command"
    ]
    assert "--route guided_physical --setting camera.panorama_size --recommended-sequence" in panorama[
        "recommended_sequence_session_command"
    ]
    assert panorama["first_proof_command"] == panorama["recommended_sequence_session_command"]
    assert any("a7ii_physical_gate_proof.py" in command["command"] for command in panorama["proof_commands"])

    raw_file_type = rows["camera.raw_file_type"]
    assert raw_file_type["next_concrete_command"]
    assert "--setting camera.raw_file_type" in raw_file_type["next_concrete_command"]
    assert "--all-route-gaps" not in raw_file_type["next_concrete_command"]
    assert "--route camera_remote_command --setting camera.raw_file_type --recommended-sequence" in raw_file_type[
        "recommended_sequence_session_command"
    ]

    command_rows = [
        item
        for item in report["settings"]
        if any(route["route"] == "camera_remote_command" for route in item["route_candidates"])
    ]
    assert command_rows
    assert any(
        "local_sony_package_path" in item["blockers"]
        for item in command_rows
        if item["proof_commands"]
    )
    print("  settings ownership representative rows: ok")


def test_ownership_cli_markdown_contains_commands() -> None:
    report = settings_ownership.build_report(setting_id="camera.panorama_size")
    markdown = a7ii_settings_ownership_report._markdown(report, command_limit=1)

    assert "# A7 II Settings Ownership" in markdown
    assert "camera.panorama_size" in markdown
    assert "a7ii_physical_gate_proof.py" in markdown
    assert "next_concrete_command" in markdown
    assert "recommended_sequence_session_command" in markdown
    assert "missing_exact_next_command_settings" in markdown
    print("  settings ownership CLI markdown: ok")


if __name__ == "__main__":
    test_ownership_report_has_no_unowned_or_commandless_gap_rows()
    test_ownership_report_explains_remote_physical_and_package_rows()
    test_ownership_cli_markdown_contains_commands()
    print("settings_ownership tests: PASS")
