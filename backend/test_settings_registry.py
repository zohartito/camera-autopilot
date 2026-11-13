"""Tests for the A7 II full settings registry.

These are camera-free: the registry must be useful before any live route is attached.
"""

from __future__ import annotations

import shlex
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "backend"))

import settings_control
import settings_preconditions
import settings_probe_plan
import settings_registry
import settings_smart_remote_values


def test_registry_loads_and_covers_photo_surface() -> None:
    registry = settings_registry.load_registry()
    summary = settings_registry.registry_summary(registry)
    assert summary["camera"] == "Sony A7 II ILCE-7M2"
    assert summary["total_settings"] >= 120, summary
    assert summary["by_group"]["camera_settings"] >= 40, summary["by_group"]
    assert summary["by_group"]["custom_settings"] >= 35, summary["by_group"]
    assert summary["by_group"]["setup"] >= 30, summary["by_group"]

    rows = settings_registry.settings_by_id(registry)
    for setting_id in (
        "camera.focus_area",
        "camera.aperture",
        "camera.shutter_speed",
        "camera.raw_file_type",
        "custom.af_system",
        "setup.format",
        "openmem.enable_adb",
        "phone.save_options",
    ):
        assert setting_id in rows, f"{setting_id} missing from registry"

    # A needs_probe row without a concrete probe command is how gaps rot.
    missing_probe_actions = [
        row["setting_id"]
        for row in rows.values()
        if (
            row["status"] == "needs_probe"
            and "probe" not in row["next_action"].lower()
            and "permanently disabled" not in row["next_action"].lower()
        )
    ]
    assert missing_probe_actions == []
    stale_proven_actions = [
        row["setting_id"]
        for row in rows.values()
        if row["status"] == "proven_set"
        and settings_registry.proven_set_next_action_is_stale(row["next_action"])
    ]
    assert stale_proven_actions == []
    print(f"  registry coverage: ok ({summary['total_settings']} settings)")


def test_registry_internal_proof_artifacts_exist() -> None:
    registry = settings_registry.load_registry()
    missing = []
    for row in registry["settings"]:
        for artifact in row.get("proof_artifacts") or []:
            if artifact.startswith("artifacts/") and not (REPO_ROOT / artifact).exists():
                missing.append((row["setting_id"], artifact))

    assert missing == []
    print("  registry internal proof artifacts exist: ok")


def test_route_planning_and_guardrails() -> None:
    registry = settings_registry.load_registry()

    iso = settings_registry.plan_setting_control("camera.iso", "800", registry=registry)
    assert iso.route == "smart_remote_wifi", iso
    assert iso.status == "proven_set"
    assert iso.can_attempt_live_write is True
    assert "setIsoSpeedRate" in (iso.command_hint or "")

    aperture = settings_registry.plan_setting_control("camera.aperture", "f/5.6", registry=registry)
    assert aperture.route == "smart_remote_wifi", aperture
    assert aperture.status == "proven_set"
    assert aperture.can_attempt_live_write is True
    assert "setFNumber" in (aperture.command_hint or "")

    shutter = settings_registry.plan_setting_control("camera.shutter_speed", "1/30", registry=registry)
    assert shutter.route == "smart_remote_wifi", shutter
    assert shutter.status == "proven_set"
    assert shutter.can_attempt_live_write is True
    assert "setShutterSpeed" in (shutter.command_hint or "")

    raw = settings_registry.plan_setting_control("camera.raw_file_type", "Uncompressed", registry=registry)
    assert raw.status == "needs_probe"
    assert raw.route == "usb_gphoto"
    assert raw.can_attempt_live_write is False
    try:
        settings_registry.plan_setting_control(
            "camera.raw_file_type", "Uncompressed", dry_run=False, registry=registry
        )
    except settings_registry.SettingControlUnavailable as exc:
        assert "needs_probe" in str(exc)
    else:
        raise AssertionError("non-dry-run write must stay unavailable until the route backend opts in")

    try:
        settings_registry.plan_setting_control(
            "camera.raw_file_type", "Uncompressed", route="openmemories", registry=registry
        )
    except settings_registry.SettingControlUnavailable as exc:
        assert "permanently disabled" in str(exc)
    else:
        raise AssertionError("Route 3 must never be selected explicitly")

    destructive = settings_registry.plan_setting_control("setup.format", "confirm", registry=registry)
    assert destructive.guarded is True
    try:
        settings_registry.plan_setting_control("setup.format", "confirm", dry_run=False, registry=registry)
    except settings_registry.SettingGuardError as exc:
        assert "disabled" in str(exc)
    else:
        raise AssertionError("non-dry-run format must be blocked")
    print("  route planning + guardrails: ok")


def test_live_write_auto_route_selection_matches_registry_priority() -> None:
    iso = settings_control.apply_setting("camera.iso", "800", dry_run=True)
    iso_plan = iso["control_plan"]
    assert iso_plan["route"] == "smart_remote_wifi"
    assert iso_plan["live_backend"] == "smart_remote_wifi"
    assert iso_plan["can_attempt_live_write"] is True
    assert iso_plan["method"] == "setIsoSpeedRate"
    assert iso_plan["behavior_proven"] is True
    assert iso_plan["requires_behavior_proof"] is False

    image_size = settings_control.apply_setting("camera.image_size", "L:24M", dry_run=True)
    image_plan = image_size["control_plan"]
    assert image_plan["route"] == "usb_gphoto"
    assert image_plan["live_backend"] == "usb_gphoto"
    assert image_plan["can_attempt_live_write"] is True
    assert image_plan["behavior_proven"] is True
    assert image_plan["requires_behavior_proof"] is False

    focus_mode = settings_control.apply_setting("camera.focus_mode", "AF-S", dry_run=True)
    assert focus_mode["control_plan"]["route"] == "smart_remote_wifi"
    assert focus_mode["control_plan"]["method"] == "setFocusMode"
    assert focus_mode["control_plan"]["requires_behavior_proof"] is False

    aperture = settings_control.apply_setting("camera.aperture", "f/5.6", dry_run=True)
    aperture_plan = aperture["control_plan"]
    assert aperture_plan["route"] == "smart_remote_wifi"
    assert aperture_plan["method"] == "setFNumber"
    assert aperture_plan["behavior_proven"] is True
    assert aperture_plan["requires_behavior_proof"] is False

    shutter = settings_control.apply_setting("camera.shutter_speed", "1/30", dry_run=True)
    shutter_plan = shutter["control_plan"]
    assert shutter_plan["route"] == "smart_remote_wifi"
    assert shutter_plan["method"] == "setShutterSpeed"
    assert shutter_plan["behavior_proven"] is True
    assert shutter_plan["requires_behavior_proof"] is False
    print("  live write auto route selection: ok")


def test_usb_live_dispatch_with_fake_camera() -> None:
    class FakeCamera:
        def __init__(self):
            self.calls = []

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return None

        def set_setting(self, node: str, value: str) -> str:
            self.calls.append((node, value))
            return value

    fake = FakeCamera()
    result = settings_control.apply_setting(
        "camera.iso",
        "800",
        route="usb_gphoto",
        dry_run=False,
        camera_factory=lambda: fake,
    )
    assert result["ok"] is True
    assert result["control_plan"]["requires_behavior_proof"] is False
    assert result["applied"] == {"route": "usb_gphoto", "node": "iso", "value": "800"}
    assert fake.calls == [("iso", "800")]

    proof = settings_control.apply_setting(
        "camera.image_size",
        "M:10M",
        route="usb_gphoto",
        dry_run=False,
        camera_factory=lambda: fake,
    )
    assert proof["control_plan"]["requires_behavior_proof"] is False
    assert proof["applied"] == {"route": "usb_gphoto", "node": "imagesize", "value": "M:10M"}
    print("  fake USB live dispatch: ok")


def test_smart_remote_live_dispatch_with_fake_camera() -> None:
    class FakeRemote:
        def __init__(self):
            self.calls = []

        def begin_session(self):
            self.calls.append(("begin_session", []))
            return ["setIsoSpeedRate", "setFocusMode", "setTouchAFPosition"]

        def call(self, method: str, params: list):
            self.calls.append((method, params))
            return {"result": [0, {"AFResult": True}], "id": 1}

    fake = FakeRemote()
    iso = settings_control.apply_setting(
        "camera.iso",
        "800",
        route="smart_remote_wifi",
        dry_run=False,
        smart_remote_factory=lambda: fake,
    )
    assert iso["control_plan"]["requires_behavior_proof"] is False
    assert iso["applied"]["route"] == "smart_remote_wifi"
    assert iso["applied"]["method"] == "setIsoSpeedRate"
    assert iso["applied"]["params"] == ["800"]

    focus = settings_control.apply_setting(
        "camera.focus_area",
        {"x": 25, "y": 45},
        route="smart_remote_wifi",
        dry_run=False,
        smart_remote_factory=lambda: fake,
    )
    assert focus["applied"]["method"] == "setTouchAFPosition"
    assert focus["applied"]["params"] == [25, 45]
    assert ("setTouchAFPosition", [25, 45]) in fake.calls

    dry_focus_mode = settings_control.apply_setting(
        "camera.focus_mode",
        "AF-S",
        route="smart_remote_wifi",
        dry_run=True,
    )
    assert dry_focus_mode["control_plan"]["live_backend"] == "smart_remote_wifi"
    assert dry_focus_mode["control_plan"]["method"] == "setFocusMode"
    assert dry_focus_mode["control_plan"]["requires_behavior_proof"] is False

    focus_mode = settings_control.apply_setting(
        "camera.focus_mode",
        "AF-S",
        route="smart_remote_wifi",
        dry_run=False,
        smart_remote_factory=lambda: fake,
    )
    assert focus_mode["control_plan"]["requires_behavior_proof"] is False
    assert focus_mode["applied"]["method"] == "setFocusMode"
    assert focus_mode["applied"]["params"] == ["AF-S"]
    assert ("setFocusMode", ["AF-S"]) in fake.calls
    print("  fake Smart Remote live dispatch: ok")


def test_raw_ptp_behavior_probe_dispatch_with_fake_runner() -> None:
    calls = []
    registry = settings_registry.load_registry()
    rows = settings_registry.settings_by_id(registry)
    rows["camera.focus_area"]["routes"]["raw_ptp"]["status"] = "protocol_ok_needs_behavior"
    rows["camera.focus_settings"]["routes"]["raw_ptp"]["status"] = "protocol_ok_needs_behavior"

    def fake_runner(command: list[str]) -> dict:
        calls.append(command)
        return {
            "returncode": 0,
            "timed_out": False,
            "stdout": "=== SUMMARY ===\n  W1: ResponseCode 0x2001 (PTP_RC_OK) -> HONORED\n",
            "stderr": "",
            "artifact": "artifacts/falsification/r2_usb_ptp/setting_api/fake.json",
        }

    dry = settings_control.apply_setting(
        "camera.focus_area",
        {"x": 320, "y": 420, "focus_area": "0x102"},
        route="raw_ptp",
        dry_run=True,
        registry=registry,
    )
    dry_plan = dry["control_plan"]
    assert dry_plan["route"] == "raw_ptp"
    assert dry_plan["live_backend"] == "raw_ptp"
    assert dry_plan["operation"] == "af_point"
    assert dry_plan["propcode"] == "0xD22C/0xD2DC"
    assert dry_plan["requires_behavior_proof"] is True

    try:
        settings_control.apply_setting(
            "camera.focus_area",
            {"x": 320, "y": 420, "focus_area": "0x102"},
            route="raw_ptp",
            dry_run=False,
            registry=registry,
            raw_ptp_runner=fake_runner,
        )
    except settings_registry.SettingBehaviorProofRequired as exc:
        assert "allow_behavior_probe=true" in str(exc)
    else:
        raise AssertionError("unproven raw PTP write must require explicit behavior-probe approval")

    proof = settings_control.apply_setting(
        "camera.focus_area",
        {"x": 320, "y": 420, "focus_area": "0x102"},
        route="raw_ptp",
        dry_run=False,
        allow_behavior_probe=True,
        registry=registry,
        raw_ptp_runner=fake_runner,
    )
    assert proof["ok"] is True
    assert proof["applied"]["operation"] == "af_point"
    assert proof["applied"]["params"] == {"x": 320, "y": 420, "focus_area": "0x102"}
    assert "tests/probe_raw_control.py" in proof["control_plan"]["raw_ptp_command"]
    assert calls[-1][2:] == [
        "--no-precondition",
        "--w1",
        "--af-x",
        "320",
        "--af-y",
        "420",
        "--focus-area",
        "0x102",
    ]

    manual = settings_control.apply_setting(
        "camera.focus_settings",
        {"step": 40, "return_after": False},
        route="raw_ptp",
        dry_run=False,
        allow_behavior_probe=True,
        registry=registry,
        raw_ptp_runner=fake_runner,
    )
    assert manual["applied"]["operation"] == "manual_focus"
    assert manual["applied"]["params"] == {"step": 40, "return_after": False}
    assert "--no-return" in calls[-1]
    print("  fake raw PTP behavior-probe dispatch: ok")


def test_read_setting_default_and_live_usb_with_fake_camera() -> None:
    photo = settings_control.read_setting("camera.iso")
    assert photo["current"]["source"] == "registry_photo"
    assert "ISO" in str(photo["current"]["value"])

    class FakeCamera:
        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return None

        def read_setting(self, node: str) -> str:
            assert node == "iso"
            return "800"

    live = settings_control.read_setting(
        "camera.iso",
        live=True,
        route="usb_gphoto",
        camera_factory=FakeCamera,
    )
    assert live["current"] == {
        "value": "800",
        "source": "usb_gphoto",
        "node": "iso",
        "greyed_in_photo": False,
    }
    print("  setting read path: ok")


def test_registry_backed_setup_checklist() -> None:
    checklist = settings_registry.registry_setup_checklist()
    labels = [row[0] for row in checklist]
    assert labels == [
        "Focus Mode",
        "Long Exposure NR",
        "Smile/Face Detect.",
        "High ISO NR",
        "SteadyShot",
        "Color Space",
    ]
    assert all(len(row) == 4 for row in checklist)
    print("  setup checklist: ok")


def test_probe_plan_covers_every_actionable_gap() -> None:
    registry = settings_registry.load_registry()
    report = settings_probe_plan.build_probe_report(registry)
    assert report["summary"]["selected_gap_rows"] >= 100
    assert report["summary"]["missing_gap_rows"] == []
    probe_ids = {command["probe_id"] for command in report["commands"]}
    assert "smart_remote.unlocked_api_surface" in probe_ids
    assert "smart_remote.api_surface_review" in probe_ids
    assert "smart_remote.behavior_proof.camera.aspect_ratio" in probe_ids
    assert "smart_remote.behavior_proof.camera.aperture" not in probe_ids
    assert "smart_remote.behavior_proof.phone.camera_information" not in probe_ids
    assert "smart_remote.behavior_proof.application.smart_remote_exit" not in probe_ids
    assert "smart_remote.phone_preferences_review" in probe_ids
    assert "smart_remote.phone_preferences_manual_review" in probe_ids
    assert "usb_gphoto.state_matrix" in probe_ids
    assert "usb_gphoto.set_read_revert" not in probe_ids
    assert "usb_gphoto.set_read_revert.camera.exposure_comp" not in probe_ids
    assert "usb_gphoto.set_read_revert.camera.flash_mode" not in probe_ids
    assert "usb_gphoto.set_read_revert.camera.picture_effect" not in probe_ids
    assert "usb_gphoto.set_read_revert.camera.zoom" not in probe_ids
    assert "usb_gphoto.set_read_revert.custom.aps_c_size_capture" not in probe_ids
    assert "usb_gphoto.movie_record.camera.movie_mode" in probe_ids
    assert "usb_gphoto.set_read_revert.camera.af_illuminator" not in probe_ids
    assert "usb_gphoto.artifact_review" in probe_ids
    assert "raw_ptp.af_point_behavior" not in probe_ids
    assert "raw_ptp.visual_proof.camera.focus_area" not in probe_ids
    assert "raw_ptp.artifact_review" not in probe_ids
    assert "camera_remote_command.package_locator" in probe_ids
    assert "camera_remote_command.package_review" in probe_ids
    assert "camera_remote_command.package_review.camera.audio_out_timing" in probe_ids
    assert not any(probe_id.startswith("openmemories.") for probe_id in probe_ids)
    assert any("review_smart_remote_api_surface.py" in command["command"] for command in report["commands"])
    smart_behavior_commands = [
        command
        for command in report["commands"]
        if command["probe_id"].startswith("smart_remote.behavior_proof.")
    ]
    assert smart_behavior_commands
    assert all("<Smart Remote setter method>" not in command["command"] for command in smart_behavior_commands)
    assert all("<value sent>" not in command["command"] for command in smart_behavior_commands)
    assert all("actZoom" not in command["command"] for command in smart_behavior_commands)
    assert any("--auto-value first-available-alternate" in command["command"] for command in smart_behavior_commands)
    assert all(command["setting_ids"] != ["phone.camera_information"] for command in smart_behavior_commands)
    assert any("a7ii_phone_preference_proof.py" in command["command"] for command in report["commands"])
    assert any("review_smart_remote_phone_preferences.py" in command["command"] for command in report["commands"])
    assert any("review_usb_gphoto_artifacts.py" in command["command"] for command in report["commands"])
    assert any(
        "a7ii_usb_movie_record_proof.py --setting camera.movie_mode" in command["command"]
        and "--allow-camera-record" in command["command"]
        and command["setting_ids"] == ["camera.movie_mode"]
        for command in report["commands"]
    )
    assert not any("review_raw_ptp_artifacts.py" in command["command"] for command in report["commands"])
    assert not any("a7ii_raw_ptp_visual_proof.py" in command["command"] for command in report["commands"])
    assert any("find_camera_remote_command_package.py" in command["command"] for command in report["commands"])
    assert any(
        "review_camera_remote_command_package.py /path/to/CameraRemoteCommand-package.zip --setting camera.audio_out_timing"
        in command["command"]
        and command["setting_ids"] == ["camera.audio_out_timing"]
        for command in report["commands"]
    )
    assert not any("openmemories" in command["route"] for command in report["commands"])
    print("  proof command coverage: ok")


def test_smart_remote_auto_value_picker_handles_still_size_pairs() -> None:
    strategy = settings_smart_remote_values.strategy_for("camera.aspect_ratio", "setStillSize")
    assert strategy is not None
    chosen = settings_smart_remote_values.choose_first_available_alternate(
        strategy,
        {
            "result": [
                {"aspect": "3:2", "size": "L:24M"},
                {"aspect": "16:9", "size": "L:20M"},
            ]
        },
        {"result": [{"aspect": "3:2", "size": "L:24M"}]},
    )
    assert chosen == ["16:9", "L:20M"]
    print("  Smart Remote auto-value still-size picker: ok")


def test_smart_remote_auto_value_picker_handles_white_balance_params() -> None:
    strategy = settings_smart_remote_values.strategy_for("camera.white_balance", "setWhiteBalance")
    assert strategy is not None
    chosen = settings_smart_remote_values.choose_first_available_alternate(
        strategy,
        {
            "result": [
                {"colorTemperature": -1, "whiteBalanceMode": "Auto WB"},
                [
                    {"colorTemperatureRange": [], "whiteBalanceMode": "Auto WB"},
                    {"colorTemperatureRange": [], "whiteBalanceMode": "Daylight"},
                ],
            ]
        },
        {"result": [{"colorTemperature": -1, "whiteBalanceMode": "Auto WB"}]},
    )
    assert chosen == ["Daylight", False, -1]
    print("  Smart Remote auto-value white-balance picker: ok")


def test_probe_commands_reference_existing_local_tools() -> None:
    registry = settings_registry.load_registry()
    report = settings_probe_plan.build_probe_report(registry)
    missing = []
    for command in report["commands"]:
        for token in shlex.split(command["command"]):
            if not token.startswith("tools/"):
                continue
            if token.startswith("tools/") and Path(token).suffix not in {".py", ".sh"}:
                continue
            path = REPO_ROOT / token
            if not path.exists():
                missing.append((command["probe_id"], token))
    assert missing == []
    print("  proof command tool targets: ok")


def test_probe_plan_filters_and_physical_gate_command() -> None:
    registry = settings_registry.load_registry()
    raw_report = settings_probe_plan.build_probe_report(registry, setting_id="camera.raw_file_type")
    raw_routes = {command["route"] for command in raw_report["commands"]}
    assert {"usb_gphoto", "camera_remote_command"} <= raw_routes
    assert any("a7ii_usb_state_matrix.py" in command["command"] for command in raw_report["commands"])
    assert not any(
        "a7ii_usb_set_read_revert.py --setting camera.raw_file_type" in command["command"]
        for command in raw_report["commands"]
    )
    # The retired Route 3 path is never a generated probe command.
    assert not any(
        "prove_openmemories_endpoint_surface.py" in command["command"]
        and "--setting-id camera.raw_file_type" in command["command"]
        for command in raw_report["commands"]
    )
    assert any(
        "review_camera_remote_command_package.py /path/to/CameraRemoteCommand-package.zip --setting camera.raw_file_type"
        in command["command"]
        for command in raw_report["commands"]
    )

    physical = settings_probe_plan.build_probe_report(registry, setting_id="setup.usb_connection")
    assert physical["summary"]["missing_gap_rows"] == []
    assert physical["commands"][0]["route"] == "guided_physical"
    assert "a7ii_physical_gate_proof.py --setting setup.usb_connection" in physical["commands"][0]["command"]
    assert "--observed-state" in physical["commands"][0]["command"]
    assert "--admin-observation-only" not in physical["commands"][0]["command"]

    admin = settings_probe_plan.build_probe_report(registry, setting_id="setup.format")
    assert admin["commands"][0]["route"] == "guided_physical"
    assert "--setting setup.format" in admin["commands"][0]["command"]
    assert "--admin-observation-only" in admin["commands"][0]["command"]
    assert admin["commands"][0]["risk"] == "manual_admin_observation"
    print("  proof command filters: ok")


def test_precondition_solver_maps_greyed_and_physical_gates() -> None:
    registry = settings_registry.load_registry()
    report = settings_preconditions.build_precondition_report(registry, greyed_only=True)
    assert report["summary"]["settings"] >= 10
    assert report["summary"]["gate_counts"]["mode_dial"] >= 5
    assert all(item["gates"] for item in report["settings"])

    pano = settings_preconditions.solve_setting("camera.panorama_size", registry)
    assert pano["greyed_in_photo"] is True
    assert pano["physical_only"] is True
    assert any(gate["gate_type"] == "mode_dial" for gate in pano["gates"])
    assert any("a7ii_physical_gate_proof.py --setting camera.panorama_size" in command for command in pano["proof_commands"])

    zoom = settings_preconditions.solve_setting("camera.zoom", registry)
    assert any(gate["gate_type"] == "lens_or_adapter" for gate in zoom["gates"])
    assert any("a7ii_usb_state_matrix.py" in command for command in zoom["proof_commands"])

    format_row = settings_preconditions.solve_setting("setup.format", registry)
    assert any("--admin-observation-only" in command for command in format_row["proof_commands"])
    print("  precondition solver: ok")


if __name__ == "__main__":
    test_registry_loads_and_covers_photo_surface()
    test_registry_internal_proof_artifacts_exist()
    test_route_planning_and_guardrails()
    test_live_write_auto_route_selection_matches_registry_priority()
    test_usb_live_dispatch_with_fake_camera()
    test_smart_remote_live_dispatch_with_fake_camera()
    test_raw_ptp_behavior_probe_dispatch_with_fake_runner()
    test_read_setting_default_and_live_usb_with_fake_camera()
    test_registry_backed_setup_checklist()
    test_probe_plan_covers_every_actionable_gap()
    test_smart_remote_auto_value_picker_handles_still_size_pairs()
    test_smart_remote_auto_value_picker_handles_white_balance_params()
    test_probe_commands_reference_existing_local_tools()
    test_probe_plan_filters_and_physical_gate_command()
    test_precondition_solver_maps_greyed_and_physical_gates()
    print("settings_registry tests: PASS")
