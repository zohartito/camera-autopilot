"""API-level contract for the A7 II settings registry/control surface.

Run standalone:
    python3 tests/test_a7ii_settings_api.py
"""

from __future__ import annotations

import contextlib
import shlex
import shutil
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(ROOT / "backend"))

import app as backend_app
import settings_readiness
import settings_registry
import settings_session_runs
from _runner import run_module_tests


@contextlib.contextmanager
def _pinned_local_toolchain(*, missing: tuple[str, ...] = ()):
    """Pin CLI discovery so route readiness does not depend on host-installed tools.

    Route readiness resolves tools through ``shutil.which``. The Mac dev box has
    gphoto2/curl/file on PATH; the ubuntu CI runner does not, which silently reclassifies
    live-camera routes and changes the offline/live counts and the recommended next action.
    Pinning the resolver keeps these endpoint contracts deterministic on both hosts without
    weakening the readiness logic under test.
    """
    original = settings_readiness.shutil.which

    def resolver(tool: str) -> str | None:
        return None if tool in missing else f"/usr/bin/{tool}"

    settings_readiness.shutil.which = resolver
    try:
        yield
    finally:
        settings_readiness.shutil.which = original


def test_settings_list_and_detail_include_current_value() -> None:
    listing = backend_app.camera_settings()
    assert listing["summary"]["total_settings"] >= 150
    assert listing["summary"]["selected_settings"] == len(listing["settings"])
    # Route 3 is permanently disabled, and rows without an active route are
    # deliberately unavailable rather than assigned a synthetic command.
    assert listing["summary"]["settings_with_remaining_gates"] >= 100
    assert listing["summary"]["settings_with_next_concrete_command"] >= 100
    assert listing["summary"]["settings_with_recommended_sequence_session_command"] >= 100
    assert any(row["setting_id"] == "camera.iso" for row in listing["settings"])
    rows = {row["setting_id"]: row for row in listing["settings"]}
    assert rows["setup.format"]["remaining_gate_category"] == "dangerous_admin_guarded"
    assert "--admin-observation-only" in rows["setup.format"]["next_concrete_command"]
    assert (
        "review_camera_remote_command_package.py /path/to/CameraRemoteCommand-package.zip "
        "--setting camera.raw_file_type"
        in rows["camera.raw_file_type"]["next_concrete_command"]
    )
    assert "--all-route-gaps" not in rows["camera.raw_file_type"]["next_concrete_command"]
    assert "--route camera_remote_command --setting camera.raw_file_type --recommended-sequence" in rows[
        "camera.raw_file_type"
    ]["recommended_sequence_session_command"]

    detail = backend_app.camera_setting("camera.iso")
    assert detail["status"] == "proven_set"
    assert detail["current"]["source"] == "registry_photo"
    assert detail["current_route"] == "usb_gphoto"
    assert detail["precondition_solution"]["setting_id"] == "camera.iso"
    assert isinstance(detail["evidence_reviews"], list)
    assert "remaining_gate" in detail
    assert "exact_next_commands" in detail
    assert "next_concrete_command" in detail
    assert "recommended_sequence_session_command" in detail

    format_detail = backend_app.camera_setting("setup.format")
    assert format_detail["remaining_gate"]["gate_category"] == "dangerous_admin_guarded"
    assert format_detail["remaining_gate"]["requires_admin_observation_only_ack"] is True
    assert "--admin-observation-only" in format_detail["remaining_gate"]["guided_physical_command"]
    assert format_detail["exact_next_commands"] == format_detail["remaining_gate"]["exact_next_commands"]
    assert format_detail["next_concrete_command"] == format_detail["remaining_gate"]["next_concrete_command"]
    assert format_detail["recommended_sequence_session_command"] == format_detail["remaining_gate"][
        "recommended_sequence_session_command"
    ]
    assert "--route guided_physical --setting setup.format --recommended-sequence" in format_detail[
        "recommended_sequence_session_command"
    ]

    raw_file_type = backend_app.camera_setting("camera.raw_file_type")
    assert (
        "review_camera_remote_command_package.py /path/to/CameraRemoteCommand-package.zip "
        "--setting camera.raw_file_type"
        in raw_file_type["next_concrete_command"]
    )
    assert "--all-route-gaps" not in raw_file_type["next_concrete_command"]
    assert "--route camera_remote_command --setting camera.raw_file_type --recommended-sequence" in raw_file_type[
        "recommended_sequence_session_command"
    ]


def test_settings_write_dry_run_and_dangerous_guardrail() -> None:
    iso = backend_app.plan_camera_setting_control(
        "camera.iso",
        backend_app.SettingControlRequest(value="800"),
    )
    assert iso["ok"] is True
    assert iso["control_plan"]["dry_run"] is True
    assert iso["control_plan"]["route"] == "smart_remote_wifi"
    assert iso["control_plan"]["live_backend"] == "smart_remote_wifi"
    assert iso["control_plan"]["requires_behavior_proof"] is False
    assert iso["control_plan"]["behavior_proven"] is True

    image_size = backend_app.plan_camera_setting_control(
        "camera.image_size",
        backend_app.SettingControlRequest(value="L:24M"),
    )
    assert image_size["control_plan"]["route"] == "usb_gphoto"
    assert image_size["control_plan"]["requires_behavior_proof"] is False
    assert image_size["control_plan"]["behavior_proven"] is True
    assert image_size["control_plan"]["allow_behavior_probe"] is False

    image_probe = backend_app.plan_camera_setting_control(
        "camera.image_size",
        backend_app.SettingControlRequest(value="L:24M", allow_behavior_probe=True),
    )
    assert image_probe["control_plan"]["allow_behavior_probe"] is True
    assert image_probe["control_plan"]["requires_behavior_proof"] is False

    smart_focus = backend_app.plan_camera_setting_control(
        "camera.focus_mode",
        backend_app.SettingControlRequest(value="AF-S", route="smart_remote_wifi"),
    )
    assert smart_focus["control_plan"]["live_backend"] == "smart_remote_wifi"
    assert smart_focus["control_plan"]["method"] == "setFocusMode"
    assert smart_focus["control_plan"]["requires_behavior_proof"] is False
    assert smart_focus["control_plan"]["behavior_proven"] is True

    raw_focus = backend_app.plan_camera_setting_control(
        "camera.focus_area",
        backend_app.SettingControlRequest(
            value={"x": 320, "y": 420, "focus_area": "0x102"},
            route="raw_ptp",
        ),
    )
    assert raw_focus["control_plan"]["route"] == "raw_ptp"
    assert raw_focus["control_plan"]["live_backend"] is None
    assert raw_focus["control_plan"]["can_attempt_live_write"] is False
    assert raw_focus["control_plan"]["requires_behavior_proof"] is False

    try:
        backend_app.plan_camera_setting_control(
            "camera.focus_area",
            backend_app.SettingControlRequest(
                value={"x": 320, "y": 420, "focus_area": "0x102"},
                route="raw_ptp",
                dry_run=False,
            ),
        )
    except backend_app.HTTPException as exc:
        assert exc.status_code == 501
        assert "no behavior-approved raw PTP route" in str(exc.detail)
    else:
        raise AssertionError("raw PTP focus writes must stay unavailable after behavior rejection")

    original_apply_setting = backend_app.settings_control.apply_setting
    calls = []

    def fake_apply_setting(*args, **kwargs):
        calls.append((args, kwargs))
        return {"ok": True, "control_plan": {"sentinel": True}, "applied": {"sentinel": True}}

    backend_app.settings_control.apply_setting = fake_apply_setting
    try:
        try:
            backend_app.plan_camera_setting_control(
                "camera.focus_area",
                backend_app.SettingControlRequest(
                    value={"x": 320, "y": 420, "focus_area": "0x102"},
                    route="raw_ptp",
                    dry_run=False,
                    allow_behavior_probe=True,
                ),
            )
        except backend_app.HTTPException as exc:
            assert exc.status_code == 403
            assert "settings-sessions/run" in str(exc.detail)
        else:
            raise AssertionError("normal setting writes must not run live behavior probes")
        assert calls == []
    finally:
        backend_app.settings_control.apply_setting = original_apply_setting

    try:
        backend_app.plan_camera_setting_control(
            "setup.format",
            backend_app.SettingControlRequest(value="confirm", dry_run=False),
        )
    except backend_app.HTTPException as exc:
        assert exc.status_code == 403
    else:
        raise AssertionError("format must be guarded")


def test_probe_and_precondition_endpoints_cover_greyed_rows() -> None:
    probes = backend_app.camera_setting_probes(setting_id="camera.raw_file_type")
    assert probes["summary"]["missing_gap_rows"] == []
    assert {"usb_gphoto", "camera_remote_command"} <= {command["route"] for command in probes["commands"]}

    preconditions = backend_app.camera_setting_preconditions(setting_id="camera.panorama_size")
    item = preconditions["settings"][0]
    assert item["greyed_in_photo"] is True
    assert item["physical_only"] is True
    assert any(gate["gate_type"] == "mode_dial" for gate in item["gates"])


def test_setting_evidence_endpoint_maps_latest_review_artifacts() -> None:
    evidence = backend_app.camera_setting_evidence(setting_id="camera.iso")
    assert evidence["summary"]["settings"] == 1
    assert evidence["settings"][0]["setting_id"] == "camera.iso"
    assert "sources" in evidence
    assert "missing_review_sources" in evidence["summary"]

    detail = backend_app.camera_setting_evidence_detail("camera.iso")
    assert detail["setting_id"] == "camera.iso"
    assert isinstance(detail["reviews"], list)
    assert isinstance(detail["missing_review_routes"], list)


def test_settings_closure_endpoint_reports_next_commands() -> None:
    closure = backend_app.camera_settings_closure(sample_limit=2)
    summary = closure["summary"]
    assert summary["total_settings"] >= 150
    assert summary["missing_probe_command_rows"] == []
    assert summary["probe_commands"] >= 60
    assert len(summary["missing_review_sources"]) == 6

    routes = {section["route"]: section for section in closure["routes"]}
    assert "smart_remote_wifi" in routes
    assert "usb_gphoto" in routes
    assert "openmemories" in routes
    assert any(
        "falsify_smart_remote_api_surface.py" in command["command"]
        for command in routes["smart_remote_wifi"]["commands"]
    )
    assert any(
        "review_usb_gphoto_artifacts.py" in command["command"]
        for command in routes["usb_gphoto"]["commands"]
    )
    assert any(
        "a7ii_usb_movie_record_proof.py --setting camera.movie_mode" in command["command"]
        for command in routes["usb_gphoto"]["commands"]
    )
    assert routes["openmemories"]["commands"] == []
    assert any(
        "review_camera_remote_command_package.py" in command["command"]
        for command in routes["camera_remote_command"]["commands"]
    )
    assert any(
        "review_camera_remote_command_package.py /path/to/CameraRemoteCommand-package.zip --setting camera.audio_out_timing"
        in command["command"]
        for command in routes["camera_remote_command"]["commands"]
    )


def test_settings_gates_endpoint_reports_structured_exact_commands() -> None:
    gates = backend_app.camera_settings_gates(limit=5)
    summary = gates["summary"]
    assert summary["total_settings"] >= 150
    assert summary["remaining_gate_rows"] >= 100
    assert summary["returned_gate_rows"] == 5
    assert len(gates["gate_rows"]) == 5
    assert all(row["exact_next_commands"] for row in gates["gate_rows"])
    assert all(row["next_concrete_command"] for row in gates["gate_rows"])
    assert all(row["recommended_sequence_session_command"] for row in gates["gate_rows"])

    setup_format = backend_app.camera_settings_gates(setting_id="setup.format")
    item = setup_format["gate_rows"][0]
    assert setup_format["summary"]["returned_gate_rows"] == 1
    assert setup_format["summary"]["returned_probe_commands"] == len(setup_format["route_commands"])
    assert item["setting_id"] == "setup.format"
    assert item["gate_category"] == "dangerous_admin_guarded"
    assert item["requires_admin_observation_only_ack"] is True
    assert "--admin-observation-only" in item["guided_physical_command"]
    assert item["next_concrete_command"] == item["guided_physical_command"]
    assert "--route guided_physical --setting setup.format --recommended-sequence" in item[
        "recommended_sequence_session_command"
    ]
    assert all(command["setting_ids"] == ["setup.format"] for command in setup_format["route_commands"])

    usb_connection = backend_app.camera_settings_gates(setting_id="setup.usb_connection")
    physical = usb_connection["gate_rows"][0]
    assert physical["gate_category"] == "physical_or_guided"
    assert physical["requires_admin_observation_only_ack"] is False
    assert "--admin-observation-only" not in physical["guided_physical_command"]
    assert physical["next_concrete_command"] == physical["guided_physical_command"]

    dangerous = backend_app.camera_settings_gates(gate_category="dangerous_admin_guarded", limit=3)
    assert dangerous["summary"]["returned_gate_rows"] == 3
    assert all(row["requires_admin_observation_only_ack"] for row in dangerous["gate_rows"])

    try:
        backend_app.camera_settings_gates(setting_id="missing.setting")
    except backend_app.HTTPException as exc:
        assert exc.status_code == 404
    else:
        raise AssertionError("unknown setting gate lookup should 404")


def test_settings_acceptance_endpoint_reports_current_completion_state() -> None:
    acceptance = backend_app.camera_settings_acceptance()
    summary = acceptance["summary"]
    assert summary["total_settings"] >= 150
    assert summary["completion_state"] == "needs_live_evidence"
    assert summary["missing_probe_command_rows"] == []
    assert len(summary["missing_review_sources"]) == 6
    assert summary["next_action_queue"] == len(acceptance["next_action_queue"])
    assert "ready_to_attempt_routes" in summary
    assert "missing_readiness_requirements" in summary
    assert "session_ledger_sessions" in summary
    assert "routes_with_session_history" in summary
    assert "routes_with_ledger_next_command" in summary
    assert "session_ledger_unreadable_manifests" in summary
    assert "session_ledger_summary" in acceptance
    assert "camera_remote_command" in {item["route"] for item in acceptance["next_action_queue"]}
    assert any(
        "local_sony_package_path" in item["blocked_by"]
        for item in acceptance["next_action_queue"]
        if item["route"] == "camera_remote_command"
    )
    camera_remote_queue = next(
        item for item in acceptance["next_action_queue"] if item["route"] == "camera_remote_command"
    )
    assert camera_remote_queue["readiness_next_action"].startswith(
        "python3 tools/find_camera_remote_command_package.py"
    )
    assert camera_remote_queue["next_concrete_command"].startswith(
        "python3 tools/find_camera_remote_command_package.py"
    )
    assert any(
        "review_camera_remote_command_package.py" in command
        for command in camera_remote_queue["readiness_next_commands"]
    )
    assert (
        "review_camera_remote_command_package.py /path/to/CameraRemoteCommand-package.zip --setting camera.audio_out_timing"
        in camera_remote_queue["first_one_row_direct_command"]
    )
    assert camera_remote_queue["recommended_next_command_kind"] == "readiness_prerequisite"
    assert camera_remote_queue["recommended_next_command"].startswith(
        "python3 tools/find_camera_remote_command_package.py"
    )
    assert [step["kind"] for step in camera_remote_queue["recommended_command_sequence"][:2]] == [
        "readiness_prerequisite",
        "readiness_prerequisite",
    ]
    assert "--setting camera.audio_out_timing" in camera_remote_queue["audited_session_command"]
    assert "--allow-hardware" not in camera_remote_queue["audited_session_command"]

    sessions = {item["route"]: item for item in acceptance["next_live_sessions"]}
    assert "--package-path /path/to/CameraRemoteCommand-package.zip" in sessions["camera_remote_command"][
        "next_evidence_session_command"
    ]
    assert "openmemories" not in sessions

    physical = backend_app.camera_settings_acceptance(route="guided_physical")
    assert physical["next_live_sessions"]


def test_settings_completion_audit_endpoint_reports_full_goal_state() -> None:
    audit = backend_app.camera_settings_completion_audit()
    summary = audit["summary"]
    items = {item["requirement"]: item for item in audit["items"]}

    assert summary["total_settings"] >= 150
    assert summary["goal_complete"] is False
    assert summary["completion_state"] == "needs_behavior_evidence"
    assert summary["inventory_complete"] is True
    assert summary["route_adjudication_complete"] is False
    assert summary["full_behavior_control_complete"] is False
    assert summary["closed_without_behavior_proof_route_rows"] == 0
    assert summary["incomplete_items"] == []
    assert "camera_remote_command" in summary["missing_review_sources"]
    assert items["canonical_settings_registry"]["status"] == "complete"
    assert items["photo_source_inventory"]["status"] == "complete"
    assert items["unified_settings_api"]["status"] == "complete"
    assert items["live_session_ledger"]["status"] == "complete"
    assert items["openmemories_reversible_route_proof"]["status"] == "unavailable"
    assert items["camera_remote_command_package_review"]["status"] == "waiting_external_package"
    assert any(
        "find_camera_remote_command_package.py" in command
        for command in items["camera_remote_command_package_review"]["exact_next_commands"]
    )
    assert any(
        "review_camera_remote_command_package.py /path/to/CameraRemoteCommand-package.zip --setting camera.audio_out_timing"
        in command
        for command in items["camera_remote_command_package_review"]["exact_next_commands"]
    )


def test_settings_photo_inventory_endpoint_accounts_for_all_source_photos() -> None:
    inventory = backend_app.camera_settings_photo_inventory()
    summary = inventory["summary"]

    assert summary["status"] == "source_dir_missing"
    assert summary["source_dir"] is None
    for key in ("unaccounted_source_files", "missing_registry_photo_files", "missing_non_registry_files", "inventory_overlaps_registry"):
        assert key not in summary and f"{key}_count" in summary
    assert inventory["records"]
    assert all("filename" not in item and item["source_id"] for item in inventory["records"])
    assert all(len(item["source_id"]) == 16 for item in inventory["records"])

    context_only = backend_app.camera_settings_photo_inventory(include_registry_sources=False)
    assert context_only["summary"]["non_registry_sources"] == len(context_only["records"])


def test_settings_live_checklist_endpoint_reports_operator_steps() -> None:
    checklist = backend_app.camera_settings_live_checklist(limit_direct_commands=2)
    summary = checklist["summary"]
    routes = checklist["routes"]

    assert summary["completion_state"] == "needs_live_evidence"
    assert summary["routes"] == 7
    assert summary["routes_with_ledger_next_command"] >= 1
    assert "routes_with_session_history" in summary
    assert "unreadable_session_manifests" in summary
    assert {item["route"] for item in routes} >= {"camera_remote_command", "smart_remote_wifi"}
    assert routes[0]["checklist_steps"]
    assert "ledger_next_command" in routes[0]
    assert "route_recommended_command" in routes[0]
    assert routes[0]["checklist_steps"][0]["kind"] in {"latest_session_outcome", "no_prior_session"}
    openmemories_route = {item["route"]: item for item in routes}["openmemories"]
    assert openmemories_route["status"] == "no_action"
    assert openmemories_route["next_concrete_command"] is None
    camera_remote = {item["route"]: item for item in routes}["camera_remote_command"]
    assert camera_remote["status"] == "waiting_external_package"
    assert any(item["kind"] == "package" for item in camera_remote["missing_requirements"])
    assert (
        "review_camera_remote_command_package.py /path/to/CameraRemoteCommand-package.zip --setting camera.audio_out_timing"
        in camera_remote["first_one_row_direct_command"]
    )
    assert "--setting camera.audio_out_timing" in camera_remote["audited_session_command"]

    raw = backend_app.camera_settings_live_checklist(route="raw_ptp", limit_direct_commands=0)
    assert raw["summary"]["routes"] == 1
    assert raw["routes"][0]["route"] == "raw_ptp"
    assert raw["routes"][0]["status"] == "no_action"
    assert raw["routes"][0]["selected_gap_rows"] == 0
    assert raw["routes"][0]["direct_evidence_commands"] == []


def test_settings_offline_work_endpoint_reports_camera_free_queue() -> None:
    with _pinned_local_toolchain():
        report = backend_app.camera_settings_offline_work()
        raw = backend_app.camera_settings_offline_work(route="raw_ptp")
    summary = report["summary"]
    routes = {item["route"]: item for item in report["routes"]}

    assert summary["completion_state"] == "needs_live_evidence"
    assert summary["remaining_live_test_routes"] == 4
    assert summary["external_package_routes"] == 1
    assert summary["no_action_or_stopped_routes"] == 2
    assert routes["camera_remote_command"]["offline_class"] == "waiting_external_package"
    assert routes["raw_ptp"]["offline_class"] == "no_action_or_stopped"
    assert routes["smart_remote_wifi"]["offline_class"] == "needs_live_camera"
    assert routes["usb_gphoto"]["offline_class"] == "needs_live_camera"
    assert routes["openmemories"]["offline_class"] == "no_action_or_stopped"
    assert any(
        action["kind"] == "local_package_locator"
        and "--include-common-search" in action["command"]
        for action in report["no_camera_route_actions"]
    )

    assert raw["summary"]["routes"] == 1
    assert raw["routes"][0]["offline_class"] == "no_action_or_stopped"


def test_settings_offline_work_endpoint_defers_live_route_missing_a_local_tool() -> None:
    # Regression for the ubuntu CI runner (no gphoto2 on PATH): the USB route moves
    # to offline-prerequisite-first and drops out of the live-test count, rather than
    # staying misclassified as camera-required.
    with _pinned_local_toolchain(missing=("gphoto2",)):
        report = backend_app.camera_settings_offline_work()
    routes = {item["route"]: item for item in report["routes"]}
    assert routes["usb_gphoto"]["offline_class"] == "offline_prerequisite_first"
    assert report["summary"]["remaining_live_test_routes"] == 3


def test_settings_ownership_endpoint_reports_zero_unowned_rows() -> None:
    ownership = backend_app.camera_settings_ownership()
    summary = ownership["summary"]
    assert summary["total_registry_settings"] >= 150
    assert summary["unowned_settings"] == []
    assert summary["missing_exact_next_command_settings"] == []
    assert summary["settings_with_exact_next_commands"] == summary["selected_settings"]

    panorama = backend_app.camera_settings_ownership(setting_id="camera.panorama_size")
    item = panorama["settings"][0]
    assert item["setting_id"] == "camera.panorama_size"
    assert item["ownership_state"] == "physical_or_admin_gate_owned"
    assert "one_setting_and_observed_camera_state" in item["blockers"]
    assert item["next_concrete_command"] == item["remaining_gate"]["next_concrete_command"]
    assert item["recommended_sequence_session_command"] == item["remaining_gate"][
        "recommended_sequence_session_command"
    ]
    assert "--recommended-sequence" in item["recommended_sequence_session_command"]
    assert any("a7ii_physical_gate_proof.py" in command["command"] for command in item["proof_commands"])


def test_setting_promotions_endpoint_is_read_only_policy() -> None:
    promotions = backend_app.camera_setting_promotions()
    assert "summary" in promotions
    assert "promotions" in promotions
    assert "missing_review_sources" in promotions["summary"]
    assert promotions["promotions"] == []  # missing reviews cannot propose a promotion

    filtered = backend_app.camera_setting_promotions(setting_id="camera.iso")
    assert all(item["setting_id"] == "camera.iso" for item in filtered["promotions"])


def test_setting_promotion_apply_endpoint_is_dry_run_first() -> None:
    dry_run = backend_app.apply_camera_setting_promotions(
        backend_app.PromotionApplyRequest(route="usb_gphoto")
    )
    assert dry_run["summary"]["dry_run"] is True
    assert dry_run["summary"]["written"] is False
    assert "changes" in dry_run

    try:
        backend_app.apply_camera_setting_promotions(
            backend_app.PromotionApplyRequest(
                route="usb_gphoto",
                dry_run=False,
                out_path="artifacts/falsification/promoted_registry.json",
            )
        )
    except backend_app.HTTPException as exc:
        assert exc.status_code == 403
        assert "allow_write" in str(exc.detail)
    else:
        raise AssertionError("registry writes must require allow_write=true")

    try:
        backend_app.apply_camera_setting_promotions(
            backend_app.PromotionApplyRequest(route="usb_gphoto", out_path="/tmp/a7ii-registry.json")
        )
    except backend_app.HTTPException as exc:
        assert exc.status_code == 403
        assert "inside this repository" in str(exc.detail)
    else:
        raise AssertionError("promotion out_path must be repo-bounded")


def test_settings_sessions_endpoint_returns_route_playbook() -> None:
    session = backend_app.camera_settings_sessions(route="usb_gphoto")
    assert session["summary"]["routes"] == 1
    route = session["routes"][0]
    assert route["route"] == "usb_gphoto"
    assert route["summary"]["missing_gap_rows"] == []
    assert any("a7ii_usb_state_matrix.py" in command["command"] for command in route["evidence_commands"])
    assert any(
        "a7ii_usb_movie_record_proof.py --setting camera.movie_mode" in command["command"]
        for command in route["evidence_commands"]
    )
    assert any(
        "review_usb_gphoto_artifacts.py" in command["command"]
        for command in route["review_commands"]
    )
    assert any(
        "a7ii_registry_promotion_plan.py --route usb_gphoto" in command["command"]
        for command in route["refresh_commands"]
    )
    assert any(
        "a7ii_apply_registry_promotions.py --route usb_gphoto" in command["command"]
        for command in route["refresh_commands"]
    )

    scoped = backend_app.camera_settings_sessions(
        route="usb_gphoto",
        setting_id="camera.raw_file_type",
    )
    scoped_route = scoped["routes"][0]
    assert scoped_route["setting_id"] == "camera.raw_file_type"
    assert "a7ii_usb_state_matrix.py" in scoped_route["next_concrete_command"]
    assert "a7ii_usb_set_read_revert.py --setting camera.raw_file_type" not in scoped_route[
        "next_concrete_command"
    ]
    assert "--all-route-gaps" not in scoped_route["next_concrete_command"]
    assert scoped_route["recommended_sequence_session_command"] == (
        "python3 tools/a7ii_live_session_runner.py --route usb_gphoto "
        "--setting camera.raw_file_type --recommended-sequence"
    )

    try:
        backend_app.camera_settings_sessions(route="openmemories")
    except backend_app.HTTPException as exc:
        assert exc.status_code == 400
        assert "permanently disabled" in str(exc.detail)
    else:
        raise AssertionError("disabled Route 3 session endpoint must reject")


def test_settings_readiness_endpoint_reports_local_and_operator_gates() -> None:
    with _pinned_local_toolchain():
        readiness = backend_app.camera_settings_readiness(route="usb_gphoto")
        scoped = backend_app.camera_settings_readiness(
            route="usb_gphoto",
            setting_id="camera.raw_file_type",
        )
        crc = backend_app.camera_settings_readiness(route="camera_remote_command")
    assert readiness["summary"]["routes"] == 1
    route = readiness["routes"][0]
    assert route["route"] == "usb_gphoto"
    assert "local_status" in route["summary"]
    assert route["summary"]["manual_requirements"] >= 1
    assert any(item["name"] == "gphoto2" for item in route["requirements"])
    assert route["session_commands"]["recommended_sequence"] == (
        "python3 tools/a7ii_live_session_runner.py --route usb_gphoto --recommended-sequence"
    )
    # All local tools pinned present -> the next action is the live sequence, not an install step.
    assert route["recommended_next_action"] == route["session_commands"]["recommended_sequence"]
    assert "--route usb_gphoto --phase evidence --execute --allow-hardware" in route["session_commands"]["evidence"]
    assert route["session_commands"]["next_concrete"] == route["next_concrete_command"]

    scoped_route = scoped["routes"][0]
    assert "a7ii_usb_state_matrix.py" in scoped_route["next_concrete_command"]
    assert "a7ii_usb_set_read_revert.py --setting camera.raw_file_type" not in scoped_route[
        "next_concrete_command"
    ]
    assert "--all-route-gaps" not in scoped_route["next_concrete_command"]
    assert scoped_route["session_commands"]["recommended_sequence"] == (
        "python3 tools/a7ii_live_session_runner.py --route usb_gphoto "
        "--setting camera.raw_file_type --recommended-sequence"
    )
    assert scoped_route["recommended_next_action"] == scoped_route["session_commands"]["recommended_sequence"]
    assert "--route usb_gphoto --setting camera.raw_file_type --phase evidence" in scoped_route["session_commands"]["evidence"]

    command_route = crc["routes"][0]
    assert command_route["summary"]["ready_to_attempt"] is False
    assert any(item["kind"] == "package" for item in command_route["missing_requirements"])
    assert command_route["session_commands"]["recommended_sequence"] == (
        "python3 tools/a7ii_live_session_runner.py --route camera_remote_command --recommended-sequence"
    )


def test_settings_readiness_endpoint_flags_a_missing_local_tool_as_next_action() -> None:
    # Regression for the ubuntu CI runner (no gphoto2 on PATH): readiness must report
    # the missing tool as the concrete next action and mark the route not-ready, instead
    # of pointing at the live sequence as if the box were provisioned.
    with _pinned_local_toolchain(missing=("gphoto2",)):
        readiness = backend_app.camera_settings_readiness(route="usb_gphoto")
    route = readiness["routes"][0]
    assert route["summary"]["ready_to_attempt"] is False
    assert route["recommended_next_action"] == "Install or restore `gphoto2` before running route evidence."
    assert route["recommended_next_action"] != route["session_commands"]["recommended_sequence"]
    assert any(
        item["name"] == "gphoto2" and item["status"] == "missing"
        for item in route["missing_requirements"]
    )


def test_settings_session_run_endpoint_is_audited_and_guarded() -> None:
    # The API gate must run before it resolves an output path, so a disabled
    # route cannot create a session directory through any request shape.
    original_path_guard = backend_app._repo_bounded_path
    backend_app._repo_bounded_path = lambda *_args, **_kwargs: (_ for _ in ()).throw(
        AssertionError("disabled route reached path handling")
    )
    try:
        try:
            backend_app.run_camera_settings_session(
                backend_app.SessionRunRequest(route="openmemories", out_dir="artifacts/falsification/blocked")
            )
        except backend_app.HTTPException as exc:
            assert exc.status_code == 400
            assert "permanently disabled" in str(exc.detail)
        else:
            raise AssertionError("disabled Route 3 session run endpoint must reject")
    finally:
        backend_app._repo_bounded_path = original_path_guard

    planned = backend_app.run_camera_settings_session(
        backend_app.SessionRunRequest(
            route="usb_gphoto",
            phases=["review"],
            out_dir="artifacts/falsification/api_session_tests",
        )
    )
    assert planned["execute"] is False
    assert planned["summary"]["planned"] == planned["summary"]["commands"]
    assert planned["summary"]["executed"] == 0
    assert planned["summary"]["blocked"] == 0
    assert all(entry["status"] == "planned" for entry in planned["commands"])
    assert all("review_usb_gphoto_artifacts.py" in entry["command"] for entry in planned["commands"])
    assert planned["aftermath"]["outcome"] == "planned_only"
    assert planned["aftermath"]["next_commands"] == [
        "python3 tools/a7ii_live_session_runner.py --route usb_gphoto --phase review --execute"
    ]

    blocked = backend_app.run_camera_settings_session(
        backend_app.SessionRunRequest(
            route="usb_gphoto",
            phases=["evidence"],
            execute=True,
            out_dir="artifacts/falsification/api_session_tests",
        )
    )
    assert blocked["summary"]["hardware_blocked"] == blocked["summary"]["commands"]
    assert blocked["summary"]["executed"] == 0
    assert all(entry["status"] == "blocked_requires_hardware_approval" for entry in blocked["commands"])
    assert blocked["aftermath"]["outcome"] == "needs_hardware_approval"
    assert blocked["aftermath"]["next_commands"] == [
        "python3 tools/a7ii_live_session_runner.py --route usb_gphoto --phase evidence --execute --allow-hardware"
    ]

    scoped = backend_app.run_camera_settings_session(
        backend_app.SessionRunRequest(
            route="usb_gphoto",
            setting_id="camera.raw_file_type",
            phases=["evidence"],
            out_dir="artifacts/falsification/api_session_tests",
        )
    )
    assert scoped["setting_id"] == "camera.raw_file_type"
    assert scoped["summary"]["commands"] == 1
    assert "a7ii_usb_state_matrix.py" in scoped["commands"][0]["command"]
    assert "--all-route-gaps" not in scoped["commands"][0]["command"]
    assert "a7ii_usb_set_read_revert.py --setting camera.raw_file_type" not in scoped["commands"][0][
        "command"
    ]

    sequence = backend_app.run_camera_settings_session(
        backend_app.SessionRunRequest(
            route="usb_gphoto",
            recommended_sequence=True,
            out_dir="artifacts/falsification/api_session_tests",
        )
    )
    assert sequence["recommended_sequence"] is True
    assert sequence["phases"] == ["recommended_sequence"]
    assert sequence["aftermath"]["outcome"] == "planned_only"
    assert sequence["aftermath"]["recommended_sequence_session_command"] == (
        "python3 tools/a7ii_live_session_runner.py --route usb_gphoto --recommended-sequence"
    )
    assert [entry["sequence_kind"] for entry in sequence["commands"][:2]] == [
        "route_prerequisite_probe",
        "one_row_direct_evidence",
    ]
    assert "a7ii_usb_state_matrix.py" in sequence["commands"][0]["command"]
    assert "a7ii_usb_movie_record_proof.py --setting camera.movie_mode" in sequence["commands"][1][
        "command"
    ]
    assert "--allow-camera-record" in sequence["commands"][1]["command"]

    physical_blocked = backend_app.run_camera_settings_session(
        backend_app.SessionRunRequest(
            route="guided_physical",
            setting_id="setup.usb_connection",
            phases=["evidence"],
            execute=True,
            allow_hardware=True,
            out_dir="artifacts/falsification/api_session_tests",
        )
    )
    assert physical_blocked["summary"]["observation_blocked"] == 1
    assert physical_blocked["summary"]["executed"] == 0
    assert physical_blocked["commands"][0]["status"] == "blocked_needs_operator_observation"
    assert physical_blocked["aftermath"]["outcome"] == "needs_operator_observation"
    assert "--observed-state" in physical_blocked["aftermath"]["next_commands"][0]

    smart_remote_planned = backend_app.run_camera_settings_session(
        backend_app.SessionRunRequest(
            route="smart_remote_wifi",
            setting_id="camera.aspect_ratio",
            phases=["evidence"],
            out_dir="artifacts/falsification/api_session_tests",
            smart_remote_method="setStillSize",
            smart_remote_requested_value="3:2",
            smart_remote_observed_before="16:9",
            smart_remote_observed_after="3:2",
            smart_remote_observed_behavior="getEvent reflected 3:2 after the setter.",
        )
    )
    assert smart_remote_planned["execute"] is False
    assert smart_remote_planned["smart_remote_method"] == "setStillSize"
    assert smart_remote_planned["smart_remote_requested_value"] == "3:2"
    assert smart_remote_planned["smart_remote_observed_before"] == "16:9"
    assert smart_remote_planned["smart_remote_observed_after"] == "3:2"
    assert smart_remote_planned["smart_remote_observed_behavior"] == "getEvent reflected 3:2 after the setter."
    assert smart_remote_planned["summary"]["planned"] == smart_remote_planned["summary"]["commands"]
    assert any("setStillSize" in entry["command"] for entry in smart_remote_planned["commands"])
    assert any("16:9" in entry["command"] and "3:2" in entry["command"] for entry in smart_remote_planned["commands"])
    assert all("<Smart Remote setter method>" not in entry["command"] for entry in smart_remote_planned["commands"])
    assert all("<observed before value>" not in entry["command"] for entry in smart_remote_planned["commands"])

    try:
        backend_app.run_camera_settings_session(
            backend_app.SessionRunRequest(
                route="camera_remote_command",
                phases=["evidence"],
                package_path="/tmp/CameraRemoteCommand-package.zip",
            )
        )
    except backend_app.HTTPException as exc:
        assert exc.status_code == 403
        assert "package_path" in str(exc.detail)
    else:
        raise AssertionError("session package_path must be repo-bounded through the API")

    shutil.rmtree(ROOT / "artifacts" / "falsification" / "api_session_tests", ignore_errors=True)


def test_settings_session_run_endpoint_requires_the_selected_body_serial_for_usb_writes() -> None:
    model, port, serial = "Sony ILCE-7M2 (Control)", "usb:001,004", "TESTSERIAL01"
    registry = settings_registry.load_registry()
    settings_registry.settings_by_id(registry)["camera.iso"]["routes"]["usb_gphoto"]["status"] = "available_not_wrapped"
    calls: list[str] = []
    out_dir = "artifacts/falsification/api_session_identity_tests"
    original_load, original_runner = settings_registry.load_registry, settings_session_runs.default_runner
    settings_registry.load_registry = lambda *_args, **_kwargs: registry
    # Never a real subprocess: a command that reached the runner is a test failure below.
    settings_session_runs.default_runner = lambda command, cwd: (
        calls.append(command) or settings_session_runs.CommandResult(0, "", "")
    )

    def request(**kwargs) -> backend_app.SessionRunRequest:
        return backend_app.SessionRunRequest(
            route="usb_gphoto", setting_id="camera.iso", phases=["evidence"], out_dir=out_dir, **kwargs)

    try:
        try:
            backend_app.run_camera_settings_session(
                request(execute=True, allow_hardware=True, camera_model=model, camera_port=port))
        except backend_app.HTTPException as exc:
            assert exc.status_code == 400
            assert "camera_serial" in str(exc.detail)
        else:
            raise AssertionError("a USB selection without its serial must be refused")

        blocked = backend_app.run_camera_settings_session(request(execute=True, allow_hardware=True))
        assert blocked["commands"][0]["status"] == "blocked_requires_camera_serial"
        assert blocked["aftermath"]["outcome"] == "needs_camera_serial"
        assert calls == []

        planned = backend_app.run_camera_settings_session(
            request(camera_model=model, camera_port=port, camera_serial=serial))
        assert planned["camera_serial"] == serial
        argv = shlex.split(planned["commands"][0]["command"])
        assert argv[argv.index("--model") + 1] == model
        assert argv[argv.index("--port") + 1] == port
        assert argv[argv.index("--serial") + 1] == serial
        assert calls == []
    finally:
        settings_registry.load_registry = original_load
        settings_session_runs.default_runner = original_runner
        shutil.rmtree(ROOT / out_dir, ignore_errors=True)


def test_settings_session_ledger_endpoint_reports_latest_outcomes() -> None:
    ledger_root_parent = ROOT / "artifacts" / "falsification"
    ledger_root_parent.mkdir(parents=True, exist_ok=True)
    out_dir = Path(tempfile.mkdtemp(prefix="api_session_ledger_", dir=ledger_root_parent))

    backend_app.run_camera_settings_session(
        backend_app.SessionRunRequest(
            route="usb_gphoto",
            phases=["review"],
            out_dir=str(out_dir),
        )
    )
    backend_app.run_camera_settings_session(
        backend_app.SessionRunRequest(
            route="usb_gphoto",
            phases=["evidence"],
            execute=True,
            out_dir=str(out_dir),
        )
    )

    ledger = backend_app.camera_settings_session_ledger(
        route="usb_gphoto",
        out_dir=str(out_dir),
        limit_recent=5,
    )
    assert ledger["summary"]["sessions"] == 2
    assert ledger["summary"]["routes_with_next_command"] >= 1
    assert ledger["summary"]["outcomes"]["planned_only"] == 1
    assert ledger["summary"]["outcomes"]["needs_hardware_approval"] == 1
    usb = next(row for row in ledger["latest_by_route"] if row["route"] == "usb_gphoto")
    assert usb["has_session"] is True
    assert usb["latest_session"]["outcome"] == "needs_hardware_approval"
    assert "--allow-hardware" in usb["latest_session"]["next_commands"][0]
    assert "--allow-hardware" in usb["next_command"]

    shutil.rmtree(out_dir, ignore_errors=True)


def test_settings_live_marathon_endpoint_is_audited_and_guarded() -> None:
    root_parent = ROOT / "artifacts" / "falsification"
    root_parent.mkdir(parents=True, exist_ok=True)
    out_dir = Path(tempfile.mkdtemp(prefix="api_marathon_", dir=root_parent))
    session_out_dir = Path(tempfile.mkdtemp(prefix="api_marathon_sessions_", dir=root_parent))

    planned = backend_app.run_camera_settings_live_marathon(
        backend_app.LiveMarathonRequest(
            routes=["openmemories"],
            out_dir=str(out_dir),
            session_out_dir=str(session_out_dir),
        )
    )
    assert planned["execute"] is False
    assert planned["summary"]["routes"] == 0
    assert planned["summary"]["planned"] == 0
    assert planned["summary"]["needs_attention"] == 0
    assert Path(planned["manifest_path"]).exists()
    assert planned["routes"] == []

    original_runner = backend_app.settings_session_runs.default_runner

    def fake_runner(command: str, cwd: Path) -> backend_app.settings_session_runs.CommandResult:
        return backend_app.settings_session_runs.CommandResult(0, "package locator test stub\n", "")

    backend_app.settings_session_runs.default_runner = fake_runner
    try:
        blocked = backend_app.run_camera_settings_live_marathon(
            backend_app.LiveMarathonRequest(
                execute=True,
                out_dir=str(out_dir),
                session_out_dir=str(session_out_dir),
            )
        )
    finally:
        backend_app.settings_session_runs.default_runner = original_runner
    assert blocked["execute"] is True
    assert blocked["summary"]["first_unresolved_route"] == "smart_remote_wifi"
    assert blocked["summary"]["needs_attention"] == 1
    assert blocked["summary"]["skipped"] >= 1
    assert blocked["routes"][0]["outcome"] == "needs_hardware_approval"

    openmemories_filtered = backend_app.run_camera_settings_live_marathon(
        backend_app.LiveMarathonRequest(
            routes=["openmemories"],
            execute=True,
            out_dir=str(out_dir),
            session_out_dir=str(session_out_dir),
        )
    )
    assert openmemories_filtered["execute"] is True
    assert openmemories_filtered["summary"]["routes"] == 0
    assert openmemories_filtered["summary"]["first_unresolved_route"] is None
    assert openmemories_filtered["routes"] == []

    try:
        backend_app.run_camera_settings_live_marathon(
            backend_app.LiveMarathonRequest(routes=["not_a_route"])
        )
    except backend_app.HTTPException as exc:
        assert exc.status_code == 400
        assert "unknown route" in str(exc.detail)
    else:
        raise AssertionError("unknown marathon route should 400")

    try:
        backend_app.run_camera_settings_live_marathon(
            backend_app.LiveMarathonRequest(
                routes=["camera_remote_command"],
                package_path="/tmp/CameraRemoteCommand-package.zip",
            )
        )
    except backend_app.HTTPException as exc:
        assert exc.status_code == 403
        assert "package_path" in str(exc.detail)
    else:
        raise AssertionError("marathon package_path must be repo-bounded through the API")

    shutil.rmtree(out_dir, ignore_errors=True)
    shutil.rmtree(session_out_dir, ignore_errors=True)


def test_setup_checklist_is_registry_backed() -> None:
    checklist = backend_app.camera_setup_checklist()
    assert checklist["summary"]["source"] == "settings_registry"
    assert checklist["summary"]["items"] >= 6
    labels = [item["label"] for item in checklist["items"]]
    assert labels[:3] == ["Focus Mode", "Long Exposure NR", "Smile/Face Detect."]
    assert all({"label", "where", "target", "why"} <= set(item) for item in checklist["items"])


if __name__ == "__main__":
    raise SystemExit(run_module_tests(__name__))
