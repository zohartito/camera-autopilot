"""Camera-free tests for A7 II live-session readiness reports."""

from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "backend"))
sys.path.insert(0, str(REPO_ROOT / "tools"))

import a7ii_live_readiness
import settings_readiness


def _fake_tool_resolver(tool: str) -> str | None:
    return f"/usr/bin/{tool}"


def _all_paths_exist(path: Path) -> bool:
    return True


def test_usb_readiness_reports_local_tools_and_operator_state() -> None:
    report = settings_readiness.build_readiness_report(
        route="usb_gphoto",
        tool_resolver=_fake_tool_resolver,
        path_exists=_all_paths_exist,
    )
    assert report["summary"]["routes"] == 1
    route = report["routes"][0]
    assert route["route"] == "usb_gphoto"
    assert route["summary"]["ready_to_attempt"] is True
    assert route["summary"]["manual_requirements"] >= 1
    names = {item["name"] for item in route["requirements"]}
    assert {"gphoto2", "curl", "tools/falsify_usb_bridge_lan.sh"} <= names
    assert route["session_commands"]["recommended_sequence"] == (
        "python3 tools/a7ii_live_session_runner.py --route usb_gphoto --recommended-sequence"
    )
    assert route["recommended_next_action"] == route["session_commands"]["recommended_sequence"]
    assert "--route usb_gphoto --phase evidence --execute --allow-hardware" in route["session_commands"]["evidence"]
    assert route["next_concrete_command"]
    assert route["session_commands"]["next_concrete"] == route["next_concrete_command"]
    print("  USB readiness report: ok")


def test_usb_readiness_surfaces_scoped_concrete_command() -> None:
    report = settings_readiness.build_readiness_report(
        route="usb_gphoto",
        setting_id="camera.movie_mode",
        tool_resolver=_fake_tool_resolver,
        path_exists=_all_paths_exist,
    )
    route = report["routes"][0]
    assert route["summary"]["ready_to_attempt"] is True
    assert "--setting camera.movie_mode" in route["next_concrete_command"]
    assert "--all-route-gaps" not in route["next_concrete_command"]
    assert route["session_commands"]["next_concrete"] == route["next_concrete_command"]
    assert route["session_commands"]["recommended_sequence"] == (
        "python3 tools/a7ii_live_session_runner.py --route usb_gphoto "
        "--setting camera.movie_mode --recommended-sequence"
    )
    assert route["recommended_next_action"] == route["session_commands"]["recommended_sequence"]
    assert "--route usb_gphoto --setting camera.movie_mode --phase evidence" in route["session_commands"]["evidence"]
    markdown = a7ii_live_readiness._markdown(report)
    assert "Audited proof-sequence manifest:" in markdown
    assert "--setting camera.movie_mode --recommended-sequence" in markdown
    assert "Next concrete evidence command:" in markdown
    assert "a7ii_usb_movie_record_proof.py --setting camera.movie_mode" in markdown
    assert "--allow-camera-record" in markdown
    print("  USB readiness scoped concrete command: ok")


def test_usb_readiness_no_node_rows_fall_back_to_state_matrix() -> None:
    report = settings_readiness.build_readiness_report(
        route="usb_gphoto",
        setting_id="camera.raw_file_type",
        tool_resolver=_fake_tool_resolver,
        path_exists=_all_paths_exist,
    )
    route = report["routes"][0]
    assert route["summary"]["ready_to_attempt"] is True
    assert "a7ii_usb_state_matrix.py" in route["next_concrete_command"]
    assert "a7ii_usb_set_read_revert.py --setting camera.raw_file_type" not in route["next_concrete_command"]
    print("  USB readiness no-node fallback: ok")


def test_camera_remote_command_requires_local_package_path() -> None:
    missing = settings_readiness.build_readiness_report(
        route="camera_remote_command",
        tool_resolver=_fake_tool_resolver,
        path_exists=_all_paths_exist,
    )
    route = missing["routes"][0]
    assert route["summary"]["ready_to_attempt"] is False
    assert route["missing_requirements"][0]["kind"] == "package"
    assert route["recommended_next_action"].startswith("python3 tools/find_camera_remote_command_package.py")
    assert "find_camera_remote_command_package.py" in route["recommended_next_action"]
    assert route["next_commands"][0].startswith("python3 tools/find_camera_remote_command_package.py")
    assert "review_camera_remote_command_package.py" in route["next_commands"][1]
    assert "Run `" not in route["recommended_next_action"]
    assert "Run `" not in route["missing_requirements"][0]["next_action"]
    assert route["session_commands"]["recommended_sequence"] == (
        "python3 tools/a7ii_live_session_runner.py --route camera_remote_command --recommended-sequence"
    )
    if route["missing_requirements"][0].get("last_locator_summary"):
        assert "reviewable_candidates" in route["missing_requirements"][0]["last_locator_summary"]
    assert "--package-path /path/to/CameraRemoteCommand-package.zip" in route["session_commands"]["evidence"]
    names = {item["name"] for item in route["requirements"]}
    assert "tools/find_camera_remote_command_package.py" in names

    ready = settings_readiness.build_readiness_report(
        route="camera_remote_command",
        package_path="/approved/CameraRemoteCommand-package.zip",
        tool_resolver=_fake_tool_resolver,
        path_exists=_all_paths_exist,
    )
    route = ready["routes"][0]
    assert route["summary"]["ready_to_attempt"] is True
    assert route["missing_requirements"] == []
    assert route["recommended_next_action"] == route["session_commands"]["recommended_sequence"]
    assert "--package-path /approved/CameraRemoteCommand-package.zip" in route["session_commands"]["evidence"]
    print("  Camera Remote Command package readiness: ok")


def test_camera_remote_command_readiness_prefers_path_free_canonical_locator() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        report_dir = Path(tmp)
        private_path = report_dir / "20991231-command-package-locator.json"
        private_path.write_text(
            json.dumps(
                {
                    "generated_at": "2099-12-31T00:00:00Z",
                    "search": {"search_dirs": ["/private/operator/path"]},
                    "summary": {"reviewable_candidates": 1},
                }
            ),
            encoding="utf-8",
        )
        canonical_path = report_dir / settings_readiness.CAMERA_REMOTE_COMMAND_LOCATOR_SUMMARY
        canonical_path.write_text(
            json.dumps(
                {
                    "generated_at": "2026-07-27T00:00:00Z",
                    "search": {"search_root_count": 7},
                    "summary": {
                        "result": "official_package_not_found_locally",
                        "reviewable_candidates": 0,
                    },
                }
            ),
            encoding="utf-8",
        )

        locator = settings_readiness._latest_locator_report(report_dir)

    assert locator is not None
    assert locator["artifact"].endswith(
        settings_readiness.CAMERA_REMOTE_COMMAND_LOCATOR_SUMMARY
    )
    assert locator["summary"]["reviewable_candidates"] == 0
    assert locator["search"]["search_root_count"] == 7
    assert "/private/operator/path" not in json.dumps(locator)
    print("  Camera Remote Command canonical locator precedence: ok")


def test_readiness_surfaces_missing_local_tool() -> None:
    def missing_gphoto(tool: str) -> str | None:
        if tool == "gphoto2":
            return None
        return f"/usr/bin/{tool}"

    report = settings_readiness.build_readiness_report(
        route="usb_gphoto",
        tool_resolver=missing_gphoto,
        path_exists=_all_paths_exist,
    )
    route = report["routes"][0]
    assert route["summary"]["ready_to_attempt"] is False
    assert any(item["name"] == "gphoto2" for item in route["missing_requirements"])
    assert "gphoto2" in route["recommended_next_action"]
    print("  missing local tool readiness: ok")


def test_guided_physical_readiness_uses_per_setting_observation_command() -> None:
    broad_report = settings_readiness.build_readiness_report(
        route="guided_physical",
        tool_resolver=_fake_tool_resolver,
        path_exists=_all_paths_exist,
    )
    broad_route = broad_report["routes"][0]
    broad_command = broad_route["session_commands"]["evidence"]
    assert "--route guided_physical" in broad_command
    assert "--setting <setting_id>" in broad_command
    assert "--observed-state \"<describe camera/menu state>\"" in broad_command
    assert broad_route["summary"]["manual_requirements"] >= 1

    scoped_report = settings_readiness.build_readiness_report(
        route="guided_physical",
        setting_id="setup.usb_connection",
        tool_resolver=_fake_tool_resolver,
        path_exists=_all_paths_exist,
    )
    scoped_route = scoped_report["routes"][0]
    scoped_command = scoped_route["session_commands"]["evidence"]
    assert "--setting setup.usb_connection" in scoped_command
    assert "<setting_id>" not in scoped_command
    print("  guided physical readiness command: ok")


def test_phone_readiness_uses_per_setting_observation_command() -> None:
    broad_report = settings_readiness.build_readiness_report(
        route="smart_remote_phone",
        tool_resolver=_fake_tool_resolver,
        path_exists=_all_paths_exist,
    )
    broad_route = broad_report["routes"][0]
    broad_command = broad_route["session_commands"]["evidence"]
    assert "--route smart_remote_phone" in broad_command
    assert "--setting <setting_id>" in broad_command
    assert "--observed-value \"<visible phone-app value>\"" in broad_command
    assert "--phone-state \"<describe phone/app state>\"" in broad_command

    scoped_report = settings_readiness.build_readiness_report(
        route="smart_remote_phone",
        setting_id="phone.grid_line",
        tool_resolver=_fake_tool_resolver,
        path_exists=_all_paths_exist,
    )
    scoped_command = scoped_report["routes"][0]["session_commands"]["evidence"]
    assert "--setting phone.grid_line" in scoped_command
    assert "<setting_id>" not in scoped_command
    print("  phone preference readiness command: ok")


def test_raw_ptp_readiness_uses_scoped_visual_proof_command() -> None:
    scoped_report = settings_readiness.build_readiness_report(
        route="raw_ptp",
        setting_id="camera.focus_area",
        tool_resolver=_fake_tool_resolver,
        path_exists=_all_paths_exist,
    )
    scoped_command = scoped_report["routes"][0]["session_commands"]["evidence"]
    assert "--route raw_ptp" in scoped_command
    assert '--observed-behavior "<describe near/far visual behavior>"' in scoped_command
    assert '--target-setup "<describe near/far target setup>"' in scoped_command
    print("  raw PTP readiness visual proof command: ok")


def test_raw_ptp_readiness_accepts_repo_venv_when_pmca_venv_is_missing() -> None:
    def repo_venv_only(path: Path) -> bool:
        if str(path) == "/tmp/pmca-venv/bin/python":
            return False
        return True

    report = settings_readiness.build_readiness_report(
        route="raw_ptp",
        tool_resolver=_fake_tool_resolver,
        path_exists=repo_venv_only,
    )
    route = report["routes"][0]
    assert route["summary"]["ready_to_attempt"] is True
    requirement = next(item for item in route["requirements"] if item["name"] == "raw PTP Python with pyusb")
    assert requirement["status"] == "ok"
    assert requirement["path"].endswith("/.venv/bin/python")
    assert "/tmp/pmca-venv/bin/python" in requirement["candidates"]
    print("  raw PTP readiness repo venv fallback: ok")


if __name__ == "__main__":
    test_usb_readiness_reports_local_tools_and_operator_state()
    test_usb_readiness_surfaces_scoped_concrete_command()
    test_usb_readiness_no_node_rows_fall_back_to_state_matrix()
    test_camera_remote_command_requires_local_package_path()
    test_camera_remote_command_readiness_prefers_path_free_canonical_locator()
    test_readiness_surfaces_missing_local_tool()
    test_guided_physical_readiness_uses_per_setting_observation_command()
    test_phone_readiness_uses_per_setting_observation_command()
    test_raw_ptp_readiness_uses_scoped_visual_proof_command()
    test_raw_ptp_readiness_accepts_repo_venv_when_pmca_venv_is_missing()
    print("settings_readiness tests: PASS")
