"""Camera-free tests for A7 II settings acceptance and next live sessions."""

from __future__ import annotations

import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "backend"))
sys.path.insert(0, str(REPO_ROOT / "tools"))

import a7ii_acceptance_report
import settings_acceptance
import settings_closure


def _fake_session_ledger(latest_route: str = "usb_gphoto") -> dict:
    rows = []
    for route in settings_closure.ROUTE_ORDER:
        next_command = f"python3 tools/a7ii_live_session_runner.py --route {route} --recommended-sequence"
        latest_session = None
        latest_next = None
        if route == latest_route:
            latest_next = f"python3 tools/a7ii_live_session_runner.py --route {route} --phase review --phase refresh --execute"
            latest_session = {
                "timestamp": "20260624-test-ledger",
                "route": route,
                "setting_id": "camera.af_illuminator",
                "manifest_path": f"artifacts/falsification/live_sessions/20260624-test-{route}/session-manifest.json",
                "summary_path": f"artifacts/falsification/live_sessions/20260624-test-{route}/session-summary.md",
                "outcome": "planned_only",
                "note": "test ledger fixture",
                "next_commands": [latest_next],
            }
            next_command = latest_next
        rows.append(
            {
                "route": route,
                "has_session": latest_session is not None,
                "latest_session": latest_session,
                "latest_session_next_command": latest_next,
                "next_command": next_command,
                "route_recommended_command": (
                    f"python3 tools/a7ii_live_session_runner.py --route {route} --recommended-sequence"
                ),
            }
        )
    return {
        "summary": {
            "sessions": 1,
            "unreadable_manifests": 0,
            "routes_with_next_command": len(settings_closure.ROUTE_ORDER),
            "routes_without_sessions": [
                route for route in settings_closure.ROUTE_ORDER if route != latest_route
            ],
        },
        "latest_by_route": rows,
        "recent_sessions": [],
        "unreadable_manifests": [],
    }


def test_acceptance_report_summarizes_completion_and_live_sessions() -> None:
    report = settings_acceptance.build_report()
    summary = report["summary"]

    assert summary["total_settings"] >= 150
    assert summary["proof_commands"] >= 60
    assert summary["completion_state"] in {
        "needs_live_evidence",
        "registry_probe_coverage_incomplete",
        "ready_for_final_audit",
    }
    assert summary["missing_probe_command_rows"] == []
    # The default report deliberately never consumes ignored local artifacts.
    # A fresh clone therefore treats every review route as unreviewed.
    assert set(summary["missing_review_sources"]) == {
        "smart_remote_wifi", "smart_remote_phone", "usb_gphoto", "raw_ptp",
        "camera_remote_command", "guided_physical",
    }
    assert report["requirements"]
    queue = report["next_action_queue"]
    assert len(queue) == summary["next_action_queue"]
    assert "camera_remote_command" in {item["route"] for item in queue}
    queue_by_route = {item["route"]: item for item in queue}
    assert "local_sony_package_path" in queue_by_route["camera_remote_command"]["blocked_by"]
    assert queue_by_route["camera_remote_command"]["readiness_next_action"].startswith(
        "python3 tools/find_camera_remote_command_package.py"
    )
    assert any(
        "review_camera_remote_command_package.py" in command
        for command in queue_by_route["camera_remote_command"]["readiness_next_commands"]
    )
    assert queue_by_route["camera_remote_command"]["first_one_row_setting_ids"] == [
        "camera.audio_out_timing"
    ]
    assert (
        "review_camera_remote_command_package.py /path/to/CameraRemoteCommand-package.zip --setting camera.audio_out_timing"
        in queue_by_route["camera_remote_command"]["first_one_row_direct_command"]
    )
    assert queue_by_route["camera_remote_command"]["recommended_next_command_kind"] == "readiness_prerequisite"
    assert queue_by_route["camera_remote_command"]["recommended_next_command"].startswith(
        "python3 tools/find_camera_remote_command_package.py"
    )
    camera_remote_sequence = queue_by_route["camera_remote_command"]["recommended_command_sequence"]
    assert [step["kind"] for step in camera_remote_sequence[:2]] == [
        "readiness_prerequisite",
        "readiness_prerequisite",
    ]
    assert "find_camera_remote_command_package.py" in camera_remote_sequence[0]["command"]
    assert "review_camera_remote_command_package.py" in camera_remote_sequence[1]["command"]
    assert queue_by_route["camera_remote_command"]["next_one_row_concrete_command"] == queue_by_route[
        "camera_remote_command"
    ]["first_one_row_direct_command"]
    assert "--setting camera.audio_out_timing" in queue_by_route["camera_remote_command"][
        "audited_one_row_session_command"
    ]
    assert "--allow-hardware" not in queue_by_route["camera_remote_command"][
        "audited_one_row_session_command"
    ]
    assert "raw_ptp" not in queue_by_route
    assert "ready_to_attempt_routes" in summary
    assert "missing_readiness_requirements" in summary
    assert "session_ledger_sessions" in summary
    assert "routes_with_session_history" in summary
    assert "routes_with_ledger_next_command" in summary
    assert "session_ledger_unreadable_manifests" in summary
    assert all(item["why"] != "Next route evidence session from the current playbook." for item in queue)

    sessions = {item["route"]: item for item in report["next_live_sessions"]}
    assert "camera_remote_command" in sessions
    assert "usb_gphoto" in sessions
    assert "smart_remote_wifi" in sessions
    assert "openmemories" not in sessions
    assert "--package-path /path/to/CameraRemoteCommand-package.zip" in sessions["camera_remote_command"][
        "next_evidence_session_command"
    ]
    assert any(
        command["probe_id"] == "camera_remote_command.package_review.camera.audio_out_timing"
        and command["setting_ids"] == ["camera.audio_out_timing"]
        for command in sessions["camera_remote_command"]["direct_evidence_commands"]
    )
    assert "smart_remote_phone" in sessions
    assert "raw_ptp" in sessions
    assert "guided_physical" in sessions

    print("  settings acceptance report: ok")


def test_acceptance_report_embeds_live_session_ledger_context() -> None:
    report = settings_acceptance.build_report(session_ledger=_fake_session_ledger("usb_gphoto"))
    summary = report["summary"]

    assert summary["session_ledger_sessions"] == 1
    assert summary["routes_with_session_history"] == 1
    assert summary["routes_with_ledger_next_command"] == summary["routes"]
    assert summary["session_ledger_unreadable_manifests"] == 0

    queue_by_route = {item["route"]: item for item in report["next_action_queue"]}
    command = queue_by_route["camera_remote_command"]
    assert command["latest_session"] is None
    assert command["latest_session_outcome"] is None
    assert command["ledger_next_command"] == (
        "python3 tools/a7ii_live_session_runner.py --route camera_remote_command --recommended-sequence"
    )
    assert command["route_recommended_command"] == command["recommended_sequence_session_command"]

    sessions = {item["route"]: item for item in report["next_live_sessions"]}
    assert "openmemories" not in sessions

    markdown = a7ii_acceptance_report._markdown(report, direct_command_limit=1)
    assert "### 60. OpenMemories Route 3" not in markdown
    assert "session_ledger_sessions: `1`" in markdown
    print("  settings acceptance ledger context: ok")


def test_acceptance_markdown_puts_readiness_command_first_for_missing_package() -> None:
    report = settings_acceptance.build_report()
    markdown = a7ii_acceptance_report._markdown(report, direct_command_limit=1)
    camera_remote_section = markdown.split("### 50. Sony Camera Remote Command", maxsplit=1)[1]

    assert "Recommended readiness command:" in camera_remote_section
    assert "Evidence session after readiness:" in camera_remote_section
    readiness_index = camera_remote_section.index("Recommended readiness command:")
    evidence_index = camera_remote_section.index("Evidence session after readiness:")
    assert readiness_index < evidence_index
    readiness_block = camera_remote_section[readiness_index:evidence_index]
    evidence_block = camera_remote_section[evidence_index:]
    assert "python3 tools/find_camera_remote_command_package.py" in readiness_block
    assert "python3 tools/a7ii_live_session_runner.py --route camera_remote_command" in evidence_block
    print("  settings acceptance markdown readiness ordering: ok")


def test_acceptance_markdown_shows_concrete_and_one_row_commands() -> None:
    report = settings_acceptance.build_report()
    markdown = a7ii_acceptance_report._markdown(report, direct_command_limit=1)
    assert "### 60. OpenMemories Route 3" not in markdown
    camera_remote_section = markdown.split("### 50. Sony Camera Remote Command", maxsplit=1)[1]
    assert "Recommended readiness command:" in camera_remote_section
    assert "python3 tools/find_camera_remote_command_package.py" in camera_remote_section
    assert "### 70. Guided Physical/Admin Gates" in markdown
    assert "--setting application.introduction" in markdown
    print("  settings acceptance concrete command markdown: ok")


def test_acceptance_report_can_filter_one_route() -> None:
    report = settings_acceptance.build_report(route="raw_ptp")
    assert report["summary"]["routes"] == 1
    assert all(item["route"] == "raw_ptp" for item in report["next_live_sessions"])
    print("  settings acceptance route filter fails closed without a review: ok")


def test_acceptance_guided_physical_is_closed_by_observation_artifacts() -> None:
    report = settings_acceptance.build_report(route="guided_physical")
    closure = settings_closure.build_report(sample_limit=1)
    guided = next(item for item in closure["routes"] if item["route"] == "guided_physical")
    assert report["summary"]["routes"] == 1
    assert all(item["route"] == "guided_physical" for item in report["next_live_sessions"])
    assert guided["gap_setting_count"] > 0
    assert guided["reviewed_settings"] == 0
    print("  settings acceptance guided physical fails closed without a review: ok")


if __name__ == "__main__":
    test_acceptance_report_summarizes_completion_and_live_sessions()
    test_acceptance_report_embeds_live_session_ledger_context()
    test_acceptance_markdown_puts_readiness_command_first_for_missing_package()
    test_acceptance_markdown_shows_concrete_and_one_row_commands()
    test_acceptance_report_can_filter_one_route()
    test_acceptance_guided_physical_is_closed_by_observation_artifacts()
    print("settings_acceptance tests: PASS")
