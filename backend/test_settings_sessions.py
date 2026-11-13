"""Camera-free tests for route-centric A7 II live proof playbooks."""

from __future__ import annotations

import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "backend"))

import settings_closure
import settings_sessions


def test_usb_route_playbook_splits_evidence_review_and_refresh() -> None:
    playbook = settings_sessions.build_route_playbook("usb_gphoto")

    assert playbook["route"] == "usb_gphoto"
    assert playbook["setting_id"] is None
    assert playbook["summary"]["selected_gap_rows"] >= 15
    assert playbook["summary"]["missing_gap_rows"] == []
    assert playbook["summary"]["evidence_commands"] >= 3
    assert playbook["summary"]["review_commands"] >= 1
    assert playbook["summary"]["refresh_commands"] == 4
    assert playbook["recommended_sequence_session_command"] == (
        "python3 tools/a7ii_live_session_runner.py --route usb_gphoto --recommended-sequence"
    )

    evidence_commands = [command["command"] for command in playbook["evidence_commands"]]
    review_commands = [command["command"] for command in playbook["review_commands"]]
    refresh_commands = [command["command"] for command in playbook["refresh_commands"]]
    assert any("a7ii_usb_state_matrix.py" in command for command in evidence_commands)
    assert any("a7ii_usb_movie_record_proof.py --setting camera.movie_mode" in command for command in evidence_commands)
    assert any("review_usb_gphoto_artifacts.py" in command for command in review_commands)
    assert any("r2_usb_movie_record" in command for command in review_commands)
    assert any("a7ii_evidence_closure_report.py" in command for command in refresh_commands)
    assert any("--route usb_gphoto --applicable-only" in command for command in refresh_commands)
    assert any("a7ii_apply_registry_promotions.py --route usb_gphoto" in command for command in refresh_commands)
    print("  USB route playbook: ok")


def test_route_playbook_preserves_selected_setting() -> None:
    playbook = settings_sessions.build_route_playbook(
        "guided_physical",
        setting_id="setup.usb_connection",
    )

    assert playbook["route"] == "guided_physical"
    assert playbook["setting_id"] == "setup.usb_connection"
    assert playbook["summary"]["selected_gap_rows"] == 1
    assert len(playbook["evidence_commands"]) == 1
    assert playbook["next_concrete_command"] == playbook["evidence_commands"][0]["command"]
    assert playbook["recommended_sequence_session_command"] == (
        "python3 tools/a7ii_live_session_runner.py --route guided_physical "
        "--setting setup.usb_connection --recommended-sequence"
    )
    print("  per-setting route playbook: ok")


def test_scoped_route_playbook_prefers_setting_specific_evidence_command() -> None:
    playbook = settings_sessions.build_route_playbook(
        "usb_gphoto",
        setting_id="camera.movie_mode",
    )

    assert playbook["route"] == "usb_gphoto"
    assert playbook["setting_id"] == "camera.movie_mode"
    assert playbook["summary"]["selected_gap_rows"] == 1
    assert "a7ii_usb_movie_record_proof.py --setting camera.movie_mode" in playbook["next_concrete_command"]
    assert "--allow-camera-record" in playbook["next_concrete_command"]
    assert "--all-route-gaps" not in playbook["next_concrete_command"]
    assert playbook["recommended_sequence_session_command"] == (
        "python3 tools/a7ii_live_session_runner.py --route usb_gphoto "
        "--setting camera.movie_mode --recommended-sequence"
    )
    assert playbook["evidence_commands"][0]["probe_id"] == "usb_gphoto.state_matrix"
    print("  scoped route playbook concrete command: ok")


def test_scoped_usb_no_node_row_falls_back_to_state_matrix() -> None:
    playbook = settings_sessions.build_route_playbook(
        "usb_gphoto",
        setting_id="camera.raw_file_type",
    )

    assert playbook["route"] == "usb_gphoto"
    assert playbook["setting_id"] == "camera.raw_file_type"
    assert playbook["summary"]["selected_gap_rows"] == 1
    assert "a7ii_usb_state_matrix.py" in playbook["next_concrete_command"]
    assert "a7ii_usb_set_read_revert.py --setting camera.raw_file_type" not in playbook["next_concrete_command"]
    print("  scoped USB no-node fallback: ok")


def test_openmemories_route_playbook_is_permanently_disabled() -> None:
    playbook = settings_sessions.build_route_playbook("openmemories")

    assert playbook["route"] == "openmemories"
    assert playbook["summary"]["status"] == "permanently_disabled"
    assert playbook["summary"]["selected_gap_rows"] == 0
    assert playbook["summary"]["missing_gap_rows"] == []
    assert playbook["evidence_commands"] == []
    assert playbook["ordered_commands"] == []
    assert playbook["recommended_sequence_session_command"] is None
    print("  OpenMemories route permanently disabled: ok")


def test_camera_remote_command_playbook_has_per_setting_package_reviews() -> None:
    playbook = settings_sessions.build_route_playbook("camera_remote_command")

    assert playbook["route"] == "camera_remote_command"
    assert playbook["summary"]["selected_gap_rows"] >= 30
    assert playbook["summary"]["missing_gap_rows"] == []
    evidence_commands = [command["command"] for command in playbook["evidence_commands"]]
    assert any("find_camera_remote_command_package.py" in command for command in evidence_commands)
    assert any(
        "review_camera_remote_command_package.py /path/to/CameraRemoteCommand-package.zip --setting camera.audio_out_timing"
        in command
        for command in evidence_commands
    )
    assert any(
        "review_camera_remote_command_package.py /path/to/CameraRemoteCommand-package.zip --setting camera.file_format"
        in command
        for command in evidence_commands
    )
    print("  Camera Remote Command playbook one-row package reviews: ok")


def test_all_route_playbook_covers_every_route() -> None:
    report = settings_sessions.build_session_playbook()

    routes = {item["route"] for item in report["routes"]}
    assert routes == set(settings_closure.ROUTE_ORDER)
    assert report["summary"]["routes"] == len(settings_closure.ROUTE_ORDER)
    assert report["summary"]["missing_gap_rows"] == []
    assert report["summary"]["ordered_commands"] >= report["summary"]["evidence_commands"]
    assert report["summary"]["refresh_commands"] >= len(settings_closure.ROUTE_ORDER)
    print("  all-route playbook: ok")


if __name__ == "__main__":
    test_usb_route_playbook_splits_evidence_review_and_refresh()
    test_route_playbook_preserves_selected_setting()
    test_scoped_route_playbook_prefers_setting_specific_evidence_command()
    test_scoped_usb_no_node_row_falls_back_to_state_matrix()
    test_openmemories_route_playbook_is_permanently_disabled()
    test_camera_remote_command_playbook_has_per_setting_package_reviews()
    test_all_route_playbook_covers_every_route()
    print("settings_sessions tests: PASS")
