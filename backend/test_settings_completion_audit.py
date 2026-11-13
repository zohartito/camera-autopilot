"""Camera-free tests for the A7 II full settings completion audit."""

from __future__ import annotations

import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "backend"))

import settings_completion_audit


def test_completion_audit_summarizes_full_goal_state() -> None:
    report = settings_completion_audit.build_report()
    summary = report["summary"]
    items = {item["requirement"]: item for item in report["items"]}

    assert summary["total_settings"] >= 150
    assert summary["goal_complete"] is False
    assert summary["completion_state"] == "needs_behavior_evidence"
    assert summary["inventory_complete"] is True
    assert summary["route_adjudication_complete"] is False
    assert summary["full_behavior_control_complete"] is False
    assert summary["proven_set_settings"] == 16
    assert summary["proven_read_settings"] == 10
    assert summary["needs_probe_settings"] == 89
    assert summary["closed_without_behavior_proof_route_rows"] == 0
    assert summary["unique_unproven_closed_settings"] == 0
    assert summary["incomplete_items"] == []
    assert report["acceptance_summary"]["completion_state"] == summary["completion_state"]
    assert (
        report["acceptance_summary"]["legacy_queue_completion_state"]
        == "needs_live_evidence"
    )
    assert len(summary["missing_review_sources"]) == 6
    assert summary["status_counts"]["complete"] >= 9

    assert items["canonical_settings_registry"]["status"] == "complete"
    assert items["registry_required_fields"]["status"] == "complete"
    assert items["photo_source_inventory"]["status"] == "complete"
    assert "private source inventory intentionally unconfigured" in items["photo_source_inventory"]["evidence"]
    assert items["unified_settings_api"]["status"] == "complete"
    assert items["route_probe_hooks_and_playbooks"]["status"] == "complete"
    assert "tools/a7ii_live_evidence_marathon.py" in items["route_probe_hooks_and_playbooks"]["artifacts"]
    assert "python3 tools/a7ii_live_evidence_marathon.py" in items["route_probe_hooks_and_playbooks"][
        "exact_next_commands"
    ]
    assert items["remaining_gate_report"]["status"] == "complete"
    assert items["registry_promotion_pipeline"]["status"] == "waiting_for_evidence"
    assert items["live_session_ledger"]["status"] == "complete"
    assert "routes with next commands" in items["live_session_ledger"]["evidence"]
    assert items["live_session_ledger"]["exact_next_commands"] == [
        "python3 tools/a7ii_live_session_ledger.py --out artifacts/falsification/a7ii_live_session_ledger.md"
    ]
    assert items["acceptance_ledger_context"]["status"] == "complete"
    assert "queue routes with ledger next commands" in items["acceptance_ledger_context"]["evidence"]
    assert "artifacts/falsification/a7ii_acceptance.md" in items["acceptance_ledger_context"]["artifacts"]
    assert items["acceptance_ledger_context"]["exact_next_commands"] == [
        "python3 tools/a7ii_acceptance_report.py --out artifacts/falsification/a7ii_acceptance.md --quiet"
    ]
    smart_remote = items["smart_remote_wifi_behavior_evidence"]
    assert smart_remote["status"] == "needs_route_adjudication"
    usb = items["usb_gphoto_live_set_read_revert"]
    assert usb["status"] == "needs_route_adjudication"
    raw_ptp = items["raw_ptp_visible_focus_behavior"]
    assert raw_ptp["status"] == "needs_route_adjudication"
    openmemories_item = items["openmemories_reversible_route_proof"]
    assert openmemories_item["status"] == "unavailable"
    guided = items["guided_physical_admin_observations"]
    assert guided["status"] == "needs_route_adjudication"
    phone = items["smart_remote_phone_preference_evidence"]
    assert phone["status"] == "needs_route_adjudication"
    assert items["camera_remote_command_package_review"]["status"] == "waiting_external_package"
    camera_remote_commands = items["camera_remote_command_package_review"]["exact_next_commands"]
    assert camera_remote_commands
    assert any("find_camera_remote_command_package.py" in command for command in camera_remote_commands)
    assert any("review_camera_remote_command_package.py" in command for command in camera_remote_commands)
    assert any(
        "review_camera_remote_command_package.py /path/to/CameraRemoteCommand-package.zip --setting camera.audio_out_timing"
        in command
        for command in camera_remote_commands
    )
    assert not any("a7ii_live_session_runner.py" in command for command in camera_remote_commands)
    assert all(not command.startswith("Run ") for command in camera_remote_commands)
    assert all("`" not in command for command in camera_remote_commands)
    print("  completion audit goal-state summary: ok")


def test_completion_audit_markdown_includes_next_commands() -> None:
    report = settings_completion_audit.build_report()
    markdown = settings_completion_audit.to_markdown(report, limit_commands=1)

    assert "# A7 II Full Settings Control Completion Audit" in markdown
    assert "- goal_complete: `False`" in markdown
    assert "- full_behavior_control_complete: `False`" in markdown
    assert "- proven_set settings: `16`" in markdown
    assert "- closed_without_behavior_proof route rows: `0`" in markdown
    assert "photo_source_inventory" in markdown
    assert "camera_remote_command_package_review" in markdown
    assert "tools/find_camera_remote_command_package.py" in markdown
    print("  completion audit markdown: ok")


if __name__ == "__main__":
    test_completion_audit_summarizes_full_goal_state()
    test_completion_audit_markdown_includes_next_commands()
    print("settings_completion_audit tests: PASS")
