"""Camera-free tests for the A7 II evidence closure report."""

from __future__ import annotations

import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "backend"))
sys.path.insert(0, str(REPO_ROOT / "tools"))

import a7ii_evidence_closure_report


def test_closure_report_covers_all_routes_and_next_commands() -> None:
    report = a7ii_evidence_closure_report.build_report(sample_limit=2)

    summary = report["summary"]
    assert summary["total_settings"] >= 150
    assert summary["probe_commands"] >= 60
    assert summary["missing_probe_command_rows"] == []
    assert report["blocking_notes"]

    routes = {section["route"]: section for section in report["routes"]}
    assert set(routes) == set(a7ii_evidence_closure_report.ROUTE_ORDER)

    smart_commands = [command["command"] for command in routes["smart_remote_wifi"]["commands"]]
    assert any("falsify_smart_remote_api_surface.py" in command for command in smart_commands)
    assert any("review_smart_remote_api_surface.py" in command for command in smart_commands)

    usb_commands = [command["command"] for command in routes["usb_gphoto"]["commands"]]
    assert any("a7ii_usb_state_matrix.py" in command for command in usb_commands)
    assert any("a7ii_usb_movie_record_proof.py --setting camera.movie_mode" in command for command in usb_commands)
    assert any("review_usb_gphoto_artifacts.py" in command for command in usb_commands)

    openmemories_commands = [command["command"] for command in routes["openmemories"]["commands"]]
    assert openmemories_commands == []
    assert routes["openmemories"]["availability"] == "permanently_disabled"

    camera_commands = [command["command"] for command in routes["camera_remote_command"]["commands"]]
    assert any("find_camera_remote_command_package.py" in command for command in camera_commands)
    assert any(
        "review_camera_remote_command_package.py /path/to/CameraRemoteCommand-package.zip" in command
        for command in camera_commands
    )
    assert all("$CAMERA_REMOTE_COMMAND_PACKAGE" not in command for command in camera_commands)

    markdown = a7ii_evidence_closure_report._markdown(report, command_limit=1)
    assert "# A7 II Settings Evidence Closure" in markdown
    assert "Smart Remote Wi-Fi" in markdown
    assert "Sony Camera Remote Command" in markdown
    print("  evidence closure report: ok")


if __name__ == "__main__":
    test_closure_report_covers_all_routes_and_next_commands()
    print("evidence_closure_report tests: PASS")
