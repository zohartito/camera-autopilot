"""Camera-free tests for the A7 II live evidence checklist."""

from __future__ import annotations

import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "backend"))

import settings_closure
import settings_live_checklist


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
                "setting_id": None,
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
                "route_recommended_command": f"python3 tools/a7ii_live_session_runner.py --route {route} --recommended-sequence",
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


def _fake_tool_resolver(tool: str) -> str:
    return f"/fake/bin/{tool}"


def _fake_path_exists(_path: Path) -> bool:
    return True


def test_live_checklist_orders_routes_by_acceptance_priority() -> None:
    report = settings_live_checklist.build_report(
        direct_command_limit=2,
        session_ledger=_fake_session_ledger("usb_gphoto"),
        tool_resolver=_fake_tool_resolver,
        path_exists=_fake_path_exists,
    )
    summary = report["summary"]
    routes = report["routes"]

    assert summary["completion_state"] == "needs_live_evidence"
    assert summary["routes"] == 7
    assert summary["ready_to_attempt_routes"] == 5
    assert summary["status_counts"]["waiting_external_package"] == 1
    assert summary["status_counts"]["no_action"] == 2
    assert summary["routes_with_session_history"] == 1
    assert summary["routes_with_ledger_next_command"] == 5
    assert summary["unreadable_session_manifests"] == 0
    assert {item["route"] for item in routes} == set(settings_closure.ROUTE_ORDER)
    routes_by_name = {item["route"]: item for item in routes}
    smart = routes_by_name["smart_remote_wifi"]
    assert smart["proof_goal"].startswith("Prove cable-free")
    assert smart["status"] == "ready_for_operator"
    assert smart["next_command"] is not None
    assert smart["closed_without_proof_count"] == 0
    assert len(smart["checklist_steps"]) >= 1
    smart_step = smart["checklist_steps"][0]
    assert smart_step["rank"] == 1
    assert smart_step["kind"] != "no_action"
    guided = routes_by_name["guided_physical"]
    assert guided["status"] == "ready_for_operator"
    usb = routes_by_name["usb_gphoto"]
    assert usb["route"] == "usb_gphoto"
    assert usb["latest_session_outcome"] == "planned_only"
    assert usb["latest_session_timestamp"] == "20260624-test-ledger"
    assert usb["checklist_steps"][0]["kind"] != "no_action"
    camera_remote = routes_by_name["camera_remote_command"]
    assert camera_remote["status"] == "waiting_external_package"
    assert any(item["kind"] == "package" for item in camera_remote["missing_requirements"])
    assert camera_remote["next_concrete_command"].startswith(
        "python3 tools/find_camera_remote_command_package.py"
    )
    assert camera_remote["recommended_next_command_kind"] == "readiness_prerequisite"
    assert [step["kind"] for step in camera_remote["recommended_command_sequence"][:2]] == [
        "readiness_prerequisite",
        "readiness_prerequisite",
    ]
    assert "review_camera_remote_command_package.py /path/to/CameraRemoteCommand-package.zip --setting camera.audio_out_timing" in camera_remote[
        "first_one_row_direct_command"
    ]
    assert "--setting camera.audio_out_timing" in camera_remote["audited_session_command"]
    assert "--allow-hardware" not in camera_remote["audited_session_command"]
    assert any(
        step["kind"] == "first_one_row_proof" and "--setting camera.audio_out_timing" in step["text"]
        for step in camera_remote["checklist_steps"]
    )
    openmemories = {item["route"]: item for item in routes}["openmemories"]
    assert openmemories["status"] == "no_action"
    assert openmemories["next_concrete_command"] is None
    assert openmemories["recommended_next_command"] is None
    assert openmemories["audited_session_command"] is None
    assert openmemories["first_one_row_direct_command"] is None
    assert openmemories["checklist_steps"][0]["kind"] == "no_action"
    print("  live evidence checklist route ordering: ok")


def test_live_checklist_can_filter_one_route() -> None:
    report = settings_live_checklist.build_report(
        route="raw_ptp",
        direct_command_limit=0,
        session_ledger=_fake_session_ledger("raw_ptp"),
        tool_resolver=_fake_tool_resolver,
        path_exists=_fake_path_exists,
    )
    assert report["summary"]["routes"] == 1
    assert report["summary"]["routes_with_session_history"] == 1
    item = report["routes"][0]
    assert item["route"] == "raw_ptp"
    assert item["status"] == "no_action"
    assert item["selected_gap_rows"] == 0
    assert item["direct_evidence_commands"] == []
    assert item["next_concrete_command"] is None
    assert item["recommended_next_command"] is None
    assert item["audited_session_command"] is None
    assert item["first_one_row_direct_command"] is None
    assert item["recommended_command_sequence"] == []
    markdown = settings_live_checklist.to_markdown(report)
    assert "# A7 II Live Evidence Checklist" in markdown
    assert "raw_ptp" in markdown
    assert "Route prerequisite/probe command:" not in markdown
    assert "Audited proof-sequence manifest:" not in markdown
    assert "Recommended audited one-row command:" not in markdown
    assert "Direct one-row proof command:" not in markdown
    assert "`review_refresh_after_evidence`" not in markdown
    assert "Audited one-row evidence session:" not in markdown
    assert "latest_session_outcome: `planned_only`" in markdown
    assert "route_recommended_command:" not in markdown
    print("  live evidence checklist route filter: ok")


if __name__ == "__main__":
    test_live_checklist_orders_routes_by_acceptance_priority()
    test_live_checklist_can_filter_one_route()
    print("settings_live_checklist tests: PASS")
