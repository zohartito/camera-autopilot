"""Registry-derived proof commands for A7 II settings gaps.

The settings registry says what we know. This module turns the unknown or gated rows into exact
commands that produce artifacts. It does not claim hardware success; it makes the next proof step
repeatable and ties every gap back to one or more registry rows.
"""

from __future__ import annotations

import json
import shlex
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import settings_registry
import settings_smart_remote_values

NON_GAP_ROUTE_STATUSES = {"proven_set", "unsupported_on_a7ii"}
NON_GAP_ROW_STATUSES = {"proven_set", "unsupported_on_a7ii"}
NON_SETTER_ROUTE_STATUSES = NON_GAP_ROUTE_STATUSES | {
    "dangerous_admin",
    "manual_physical_gate",
    "state_gated",
}
CAMERA_LAN_HOST_SHELL = "${CAMERA_LAN_HOST}"
OPENMEMORIES_BASE_SHELL = f"http://{CAMERA_LAN_HOST_SHELL}:8080"
# USB setting writes name the selected body; session runs fill these from its model, port and serial.
CAMERA_MODEL_PLACEHOLDER = "<camera model>"
CAMERA_PORT_PLACEHOLDER = "<camera port>"
CAMERA_SERIAL_PLACEHOLDER = "<camera serial>"
USB_CAMERA_IDENTITY_ARGS = (
    f'--model "{CAMERA_MODEL_PLACEHOLDER}" --port "{CAMERA_PORT_PLACEHOLDER}" '
    f'--serial "{CAMERA_SERIAL_PLACEHOLDER}" '
)


@dataclass(frozen=True)
class ProbeCommand:
    probe_id: str
    route: str
    title: str
    command: str
    artifact_root: str
    setting_ids: tuple[str, ...]
    prerequisite: str
    proves: str
    risk: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "probe_id": self.probe_id,
            "route": self.route,
            "title": self.title,
            "command": self.command,
            "artifact_root": self.artifact_root,
            "setting_ids": list(self.setting_ids),
            "setting_count": len(self.setting_ids),
            "prerequisite": self.prerequisite,
            "proves": self.proves,
            "risk": self.risk,
        }


def row_needs_evidence(row: dict[str, Any], route: str | None = None) -> bool:
    """True when a registry row still has a control/proof gap."""
    if route is not None:
        if route in settings_registry.DISABLED_ROUTES:
            return False
        route_info = row.get("routes", {}).get(route)
        if not route_info:
            return False
        return route_info.get("status") not in NON_GAP_ROUTE_STATUSES
    return any(
        route.get("status") not in NON_GAP_ROUTE_STATUSES
        for name, route in row.get("routes", {}).items()
        if name not in settings_registry.DISABLED_ROUTES
    )


def _filter_rows(
    registry: dict[str, Any],
    *,
    setting_id: str | None = None,
    menu_group: str | None = None,
    status: str | None = None,
    route: str | None = None,
) -> list[dict[str, Any]]:
    rows = list(registry["settings"])
    if setting_id:
        rows = [settings_registry.get_setting(setting_id, registry)]
    if menu_group:
        rows = [row for row in rows if row["menu_group"] == menu_group]
    if status:
        rows = [row for row in rows if row["status"] == status]
    if route:
        rows = [row for row in rows if route in row.get("routes", {})]
    return [row for row in rows if row_needs_evidence(row, route)]


def _route_rows(rows: list[dict[str, Any]], route: str) -> tuple[str, ...]:
    ids = []
    for row in rows:
        route_info = row.get("routes", {}).get(route)
        if not route_info:
            continue
        if route_info.get("status") in NON_GAP_ROUTE_STATUSES:
            continue
        ids.append(row["setting_id"])
    return tuple(sorted(set(ids)))


def _route_rows_with_field(rows: list[dict[str, Any]], route: str, field: str) -> tuple[str, ...]:
    ids = []
    for row in rows:
        route_info = row.get("routes", {}).get(route)
        if not route_info or not route_info.get(field):
            continue
        if route_info.get("status") in NON_SETTER_ROUTE_STATUSES:
            continue
        ids.append(row["setting_id"])
    return tuple(sorted(set(ids)))


def _physical_rows(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    physical_statuses = {"manual_physical_gate", "dangerous_admin", "state_gated"}
    out = []
    for row in rows:
        routes = row.get("routes", {})
        if "guided_physical" in routes or row["status"] in physical_statuses:
            out.append(row)
    return sorted(out, key=lambda item: item["setting_id"])


def _admin_guarded(row: dict[str, Any]) -> bool:
    if row["status"] == "dangerous_admin":
        return True
    if row["setting_id"] in settings_registry.DESTRUCTIVE_SETTING_IDS:
        return True
    return any(route.get("status") == "dangerous_admin" for route in row.get("routes", {}).values())


def _physical_gate_command(row: dict[str, Any]) -> str:
    command = (
        "python3 tools/a7ii_physical_gate_proof.py "
        f"--setting {row['setting_id']} "
        "--observed-state \"<describe camera/menu state>\" "
    )
    if _admin_guarded(row):
        command += "--admin-observation-only "
    command += "--out-dir artifacts/falsification/manual_gates"
    return command


def _phone_preference_command(row: dict[str, Any]) -> str:
    return (
        "python3 tools/a7ii_phone_preference_proof.py "
        f"--setting {row['setting_id']} "
        "--observed-value \"<visible phone-app value>\" "
        "--phone-state \"<describe phone/app state>\" "
        "--out-dir artifacts/falsification/r1_phone_settings_manual"
    )


def _raw_ptp_visual_proof_command(row: dict[str, Any]) -> str:
    return (
        "python3 tools/a7ii_raw_ptp_visual_proof.py "
        f"--setting {row['setting_id']} "
        "--human-verdict proven "
        "--observed-behavior \"<describe near/far visual behavior>\" "
        "--target-setup \"<describe near/far target setup>\" "
        "--source-artifact artifacts/falsification/r2_usb_ptp "
        "--out-dir artifacts/falsification/r2_usb_ptp"
    )


def _usb_set_read_revert_command(row: dict[str, Any]) -> str:
    return (
        "python3 tools/a7ii_usb_set_read_revert.py "
        f"--setting {row['setting_id']} "
        "--execute "
        f"{USB_CAMERA_IDENTITY_ARGS}"
        "--out-dir artifacts/falsification/r2_usb_set_read_revert"
    )


def _usb_movie_record_command(row: dict[str, Any]) -> str:
    return (
        "python3 tools/a7ii_usb_movie_record_proof.py "
        f"--setting {row['setting_id']} "
        "--seconds 1.0 "
        "--execute --allow-camera-record "
        "--out-dir artifacts/falsification/r2_usb_movie_record"
    )


def _openmemories_setting_endpoint_command(row: dict[str, Any]) -> str:
    return (
        "python3 tools/prove_openmemories_endpoint_surface.py "
        f'--base "{OPENMEMORIES_BASE_SHELL}" '
        f"--setting-id {row['setting_id']} "
        "--out-dir artifacts/falsification/r3_openmemories"
    )


def _camera_remote_command_setting_review_command(row: dict[str, Any]) -> str:
    return (
        "python3 tools/review_camera_remote_command_package.py "
        "/path/to/CameraRemoteCommand-package.zip "
        f"--setting {row['setting_id']} "
        "--out-dir artifacts/falsification/r4_camera_remote_command"
    )


SMART_REMOTE_METHOD_HINTS = {
    "camera.image_size": "setStillSize",
    "camera.aspect_ratio": "setStillSize",
    "camera.file_format": "setMovieFileFormat",
    "camera.drive_mode": "setShootMode",
    "camera.flash_mode": "setFlashMode",
    "camera.flash_comp": "setFlashCompensation",
    "camera.focus_mode": "setFocusMode",
    "camera.focus_area": "setTouchAFPosition",
    "camera.exposure_comp": "setExposureCompensation",
    "camera.iso": "setIsoSpeedRate",
    "camera.metering_mode": "setMeteringMode",
    "camera.white_balance": "setWhiteBalance",
    "camera.dro_auto_hdr": "setDRO",
    "camera.creative_style": "setCreativeStyle",
    "camera.picture_effect": "setPictureEffect",
    "camera.zoom": "actZoom",
    "camera.focus_magnifier": "actZoom",
    "camera.smile_face_detect": "setFaceDetection",
}


def approved_smart_remote_method(setting_id: str, method: object) -> str | None:
    """Accept only the registry's exact method, never a review artifact's shell-like string."""
    expected = SMART_REMOTE_METHOD_HINTS.get(setting_id)
    return expected if isinstance(method, str) and method == expected else None


def _smart_remote_method_hint(row: dict[str, Any]) -> str:
    route = row.get("routes", {}).get("smart_remote_wifi", {})
    for key in ("method", "method_hint"):
        method = route.get(key)
        approved = approved_smart_remote_method(row["setting_id"], method)
        if approved:
            return approved
    return SMART_REMOTE_METHOD_HINTS.get(row["setting_id"], "<Smart Remote setter method>")


def _smart_remote_behavior_command(row: dict[str, Any]) -> str:
    method = _smart_remote_method_hint(row)
    if method == "<Smart Remote setter method>":
        raise ValueError(f"no approved Smart Remote setter for {row['setting_id']}")
    value_argument = settings_smart_remote_values.behavior_value_argument(row["setting_id"], method)
    return shlex.join([
        "python3", "tools/a7ii_smart_remote_set_observe.py", "--setting", row["setting_id"],
        *shlex.split(value_argument), "--method", method, "--execute", "--allow-camera-write",
        "--observed-before", "<observed before value>", "--observed-after", "<observed after value>",
        "--human-verdict", "proven", "--source-artifact", "artifacts/falsification/r1_api_surface",
        "--out-dir", "artifacts/falsification/r1_api_surface",
    ])


def _smart_remote_behavior_rows(rows: list[dict[str, Any]], setting_ids: tuple[str, ...]) -> list[dict[str, Any]]:
    selected = []
    selected_ids = set(setting_ids)
    for row in rows:
        if row["setting_id"] not in selected_ids:
            continue
        if row["status"] in {
            "dangerous_admin",
            "manual_physical_gate",
            "proven_read",
            "unsupported_on_a7ii",
        }:
            continue
        route = row.get("routes", {}).get("smart_remote_wifi", {})
        if route.get("status") in {"proven_set", "proven_read", "unsupported_on_a7ii"}:
            continue
        method = _smart_remote_method_hint(row)
        if method == "<Smart Remote setter method>":
            continue
        if method.startswith("act") and not settings_smart_remote_values.strategy_for(row["setting_id"], method):
            continue
        selected.append(row)
    return sorted(
        selected,
        key=lambda item: (
            _smart_remote_method_hint(item) == "<Smart Remote setter method>",
            item["setting_id"],
        ),
    )


def build_probe_commands(
    registry: dict[str, Any] | None = None,
    *,
    setting_id: str | None = None,
    menu_group: str | None = None,
    status: str | None = None,
    route: str | None = None,
) -> list[ProbeCommand]:
    """Return route-level proof commands for the selected registry rows."""
    registry = registry or settings_registry.load_registry()
    rows = _filter_rows(
        registry,
        setting_id=setting_id,
        menu_group=menu_group,
        status=status,
        route=route,
    )
    commands: list[ProbeCommand] = []

    def include(route_name: str) -> bool:
        return route_name not in settings_registry.DISABLED_ROUTES and (
            route is None or route == route_name
        )

    smart_rows = _route_rows(rows, "smart_remote_wifi")
    if smart_rows and include("smart_remote_wifi"):
        commands.append(
            ProbeCommand(
                probe_id="smart_remote.unlocked_api_surface",
                route="smart_remote_wifi",
                title="Smart Remote unlocked JSON-RPC API surface",
                command=(
                    "python3 tools/falsify_smart_remote_api_surface.py "
                    "--base http://192.168.122.1:8080/sony "
                    "--session-unlock --timeout 1.5 --repeat 3 "
                    "--out-dir artifacts/falsification/r1_api_surface"
                ),
                artifact_root="artifacts/falsification/r1_api_surface",
                setting_ids=smart_rows,
                prerequisite="Camera is running Smart Remote Control and this Mac is on the camera AP.",
                proves="Which Smart Remote getters/setters exist after startRecMode and startLiveview.",
                risk="safe_read",
            )
        )
        commands.append(
            ProbeCommand(
                probe_id="smart_remote.api_surface_review",
                route="smart_remote_wifi",
                title="Smart Remote API surface registry review",
                command=(
                    "python3 tools/review_smart_remote_api_surface.py "
                    "artifacts/falsification/r1_api_surface "
                    "--out-dir artifacts/falsification/r1_api_surface_review"
                ),
                artifact_root="artifacts/falsification/r1_api_surface_review",
                setting_ids=smart_rows,
                prerequisite=(
                    "Run smart_remote.unlocked_api_surface first, ideally from a live unlocked "
                    "Smart Remote session."
                ),
                proves="Which discovered Smart Remote methods map to each registry row.",
                risk="local_artifact_review",
            )
        )
        for row in _smart_remote_behavior_rows(rows, smart_rows):
            commands.append(
                ProbeCommand(
                    probe_id=f"smart_remote.behavior_proof.{row['setting_id']}",
                    route="smart_remote_wifi",
                    title=f"Smart Remote observed setter behavior proof: {row['label']}",
                    command=_smart_remote_behavior_command(row),
                    artifact_root="artifacts/falsification/r1_api_surface",
                    setting_ids=(row["setting_id"],),
                    prerequisite=(
                        "Run Smart Remote API-surface capture/review first, then perform one safe setter "
                        "attempt and record the observed before/after camera value for this row."
                    ),
                    proves=(
                        "That a Smart Remote setter changed this setting visibly enough to promote the "
                        "route beyond API-surface discovery."
                    ),
                    risk="smart_remote_behavior_review",
                )
            )

    phone_rows = _route_rows(rows, "smart_remote_phone")
    if phone_rows and include("smart_remote_phone"):
        commands.append(
            ProbeCommand(
                probe_id="smart_remote.phone_preferences_surface",
                route="smart_remote_phone",
                title="Smart Remote phone-preference API check",
                command=(
                    "python3 tools/falsify_smart_remote_api_surface.py "
                    "--base http://192.168.122.1:8080/sony "
                    "--session-unlock --timeout 1.5 --repeat 3 "
                    "--out-dir artifacts/falsification/r1_phone_settings_api_surface"
                ),
                artifact_root="artifacts/falsification/r1_phone_settings_api_surface",
                setting_ids=phone_rows,
                prerequisite="Phone app is connected to Smart Remote or the Mac is on the same camera AP.",
                proves="Whether phone UI preferences are backed by camera JSON-RPC methods.",
                risk="safe_read",
            )
        )
        commands.append(
            ProbeCommand(
                probe_id="smart_remote.phone_preferences_review",
                route="smart_remote_phone",
                title="Smart Remote phone-preference registry review",
                command=(
                    "python3 tools/review_smart_remote_api_surface.py "
                    "artifacts/falsification/r1_phone_settings_api_surface "
                    "--out-dir artifacts/falsification/r1_phone_settings_api_surface_review"
                ),
                artifact_root="artifacts/falsification/r1_phone_settings_api_surface_review",
                setting_ids=phone_rows,
                prerequisite=(
                    "Run smart_remote.phone_preferences_surface first, with the phone/app "
                    "preference context available when possible."
                ),
                proves="Whether phone preference rows have camera-side Smart Remote evidence.",
                risk="local_artifact_review",
            )
        )
        phone_rows_by_id = {row["setting_id"]: row for row in rows if row["setting_id"] in phone_rows}
        for setting_id in phone_rows:
            row = phone_rows_by_id[setting_id]
            commands.append(
                ProbeCommand(
                    probe_id=f"smart_remote_phone.manual_observation.{setting_id}",
                    route="smart_remote_phone",
                    title=f"Smart Remote phone-app observed preference: {row['label']}",
                    command=_phone_preference_command(row),
                    artifact_root="artifacts/falsification/r1_phone_settings_manual",
                    setting_ids=(setting_id,),
                    prerequisite=(
                        "Operator opens the Smart Remote phone app preference page and records the visible "
                        "value/context; add --photo for screenshots when available."
                    ),
                    proves=(
                        "The phone-app preference row has explicit observed evidence and remains "
                        "app-side/manual unless an API setter is separately proven."
                    ),
                    risk="phone_manual_observation",
                )
            )
        commands.append(
            ProbeCommand(
                probe_id="smart_remote.phone_preferences_manual_review",
                route="smart_remote_phone",
                title="Smart Remote phone-app observation registry review",
                command=(
                    "python3 tools/review_smart_remote_phone_preferences.py "
                    "artifacts/falsification/r1_phone_settings_manual "
                    "--out-dir artifacts/falsification/r1_phone_settings_api_surface_review"
                ),
                artifact_root="artifacts/falsification/r1_phone_settings_api_surface_review",
                setting_ids=phone_rows,
                prerequisite="Run the per-setting phone-app observation proof commands first.",
                proves="Which Smart Remote phone preference rows have operator/photo evidence.",
                risk="local_artifact_review",
            )
        )

    usb_rows = _route_rows(rows, "usb_gphoto")
    usb_node_rows = _route_rows_with_field(rows, "usb_gphoto", "node")
    usb_movie_record_rows = tuple(row_id for row_id in usb_node_rows if row_id == "camera.movie_mode")
    usb_set_read_revert_rows = tuple(row_id for row_id in usb_node_rows if row_id != "camera.movie_mode")
    if usb_rows and include("usb_gphoto"):
        commands.append(
            ProbeCommand(
                probe_id="usb_gphoto.state_matrix",
                route="usb_gphoto",
                title="USB/gphoto capability state matrix",
                command=(
                    "python3 tools/a7ii_usb_state_matrix.py "
                    "--state stills --state movie_capable --state manual_focus "
                    "--state autofocus --state smart_remote_exited "
                    "--out-dir artifacts/falsification/r2_usb_state_matrix"
                ),
                artifact_root="artifacts/falsification/r2_usb_state_matrix",
                setting_ids=usb_rows,
                prerequisite="USB Connection is PC Remote; operator changes the named camera state before each run.",
                proves="Which gphoto nodes are present, writable, read-only, or state-gated in each state.",
                risk="safe_read",
            )
        )
        if usb_set_read_revert_rows:
            commands.append(
                ProbeCommand(
                    probe_id="usb_gphoto.set_read_revert",
                    route="usb_gphoto",
                    title="USB/gphoto set-read-revert proof",
                    command=(
                        "python3 tools/a7ii_usb_set_read_revert.py "
                        "--include-proven --execute "
                        f"{USB_CAMERA_IDENTITY_ARGS}"
                        "--out-dir artifacts/falsification/r2_usb_set_read_revert"
                    ),
                    artifact_root="artifacts/falsification/r2_usb_set_read_revert",
                    setting_ids=usb_set_read_revert_rows,
                    prerequisite="Known-good USB PC Remote state; operator accepts temporary safe setting writes.",
                    proves="Which registered USB nodes can be written, read back, and reverted safely.",
                    risk="temporary_setting_write",
                )
            )
        usb_rows_by_id = {row["setting_id"]: row for row in rows if row["setting_id"] in usb_node_rows}
        for setting_id in usb_set_read_revert_rows:
            row = usb_rows_by_id[setting_id]
            commands.append(
                ProbeCommand(
                    probe_id=f"usb_gphoto.set_read_revert.{setting_id}",
                    route="usb_gphoto",
                    title=f"USB/gphoto one-setting set-read-revert proof: {row['label']}",
                    command=_usb_set_read_revert_command(row),
                    artifact_root="artifacts/falsification/r2_usb_set_read_revert",
                    setting_ids=(setting_id,),
                    prerequisite=(
                        "Known-good USB PC Remote state; operator accepts one temporary safe write "
                        "for this row. Pass --value setting_id=value if the automatic alternate "
                        "choice is not appropriate."
                    ),
                    proves=(
                        "That this registered USB node can be written, read back, and reverted "
                        "without running the full USB route sweep first."
                    ),
                    risk="temporary_setting_write",
                )
            )
        for setting_id in usb_movie_record_rows:
            row = usb_rows_by_id[setting_id]
            commands.append(
                ProbeCommand(
                    probe_id=f"usb_gphoto.movie_record.{setting_id}",
                    route="usb_gphoto",
                    title=f"USB/gphoto one-setting movie record proof: {row['label']}",
                    command=_usb_movie_record_command(row),
                    artifact_root="artifacts/falsification/r2_usb_movie_record",
                    setting_ids=(setting_id,),
                    prerequisite=(
                        "USB Connection is PC Remote, camera is in a movie-capable state, and the "
                        "operator accepts recording one short clip to the SD card."
                    ),
                    proves=(
                        "That the native USB/gphoto movie node can start and stop movie recording. "
                        "This does not prove software control of the physical Movie mode dial."
                    ),
                    risk="records_movie",
                )
            )
        commands.append(
            ProbeCommand(
                probe_id="usb_bridge.lan_smoke",
                route="usb_gphoto",
                title="USB-backed HTTP bridge LAN smoke",
                command="tools/falsify_usb_bridge_lan.sh artifacts/falsification/r2_usb_bridge",
                artifact_root="artifacts/falsification/r2_usb_bridge",
                setting_ids=usb_rows,
                prerequisite="Known-good USB PC Remote/stills state; ideally call from phone/LAN client.",
                proves="Whether USB settings/preview/focus/capture can be productized through a LAN API.",
                risk="captures_photo",
            )
        )
        commands.append(
            ProbeCommand(
                probe_id="usb_gphoto.artifact_review",
                route="usb_gphoto",
                title="USB/gphoto registry evidence review",
                command=(
                    "python3 tools/review_usb_gphoto_artifacts.py "
                    "artifacts/falsification/r2_usb_state_matrix "
                    "artifacts/falsification/r2_usb_set_read_revert "
                    "artifacts/falsification/r2_usb_movie_record "
                    "--out-dir artifacts/falsification/r2_usb_gphoto_review"
                ),
                artifact_root="artifacts/falsification/r2_usb_gphoto_review",
                setting_ids=usb_rows,
                prerequisite=(
                    "Run usb_gphoto.state_matrix and usb_gphoto.set_read_revert first, "
                    "with the camera in the required USB PC Remote states."
                ),
                proves="Which USB/gphoto registry nodes are proven, read-only, absent, skipped, or failed.",
                risk="local_artifact_review",
            )
        )

    raw_rows = _route_rows(rows, "raw_ptp")
    if raw_rows and include("raw_ptp"):
        commands.append(
            ProbeCommand(
                probe_id="raw_ptp.protocol_acceptance",
                route="raw_ptp",
                title="Raw Sony PTP protocol acceptance",
                command="tools/falsify_raw_ptp_focus.sh artifacts/falsification/r2_usb_ptp",
                artifact_root="artifacts/falsification/r2_usb_ptp",
                setting_ids=raw_rows,
                prerequisite="USB PC Remote state with pmca/pyusb environment available.",
                proves="Whether the hidden Sony PTP focus writes are accepted across three attempts.",
                risk="focus_may_move",
            )
        )
        commands.append(
            ProbeCommand(
                probe_id="raw_ptp.af_point_behavior",
                route="raw_ptp",
                title="Raw PTP AF-point visible behavior proof",
                command="tools/falsify_usb_af_point_behavior.sh artifacts/falsification/r2_usb_ptp",
                artifact_root="artifacts/falsification/r2_usb_ptp",
                setting_ids=raw_rows,
                prerequisite="Autofocus-capable lens, near/far target, USB PC Remote state.",
                proves="Whether different AF point writes produce visible focus-region differences.",
                risk="focus_may_move",
            )
        )
        commands.append(
            ProbeCommand(
                probe_id="raw_ptp.manual_focus_behavior",
                route="raw_ptp",
                title="Raw PTP manual-focus visible behavior proof",
                command="tools/falsify_usb_focus_behavior.sh artifacts/falsification/r2_usb_ptp",
                artifact_root="artifacts/falsification/r2_usb_ptp",
                setting_ids=raw_rows,
                prerequisite="Manual-focus-capable state, near/far target, USB PC Remote state.",
                proves="Whether manual focus step writes visibly move focus and can be reversed.",
                risk="focus_may_move",
            )
        )
        raw_rows_by_id = {row["setting_id"]: row for row in rows if row["setting_id"] in raw_rows}
        for setting_id in raw_rows:
            row = raw_rows_by_id[setting_id]
            commands.append(
                ProbeCommand(
                    probe_id=f"raw_ptp.visual_proof.{setting_id}",
                    route="raw_ptp",
                    title=f"Raw PTP visual behavior proof marker: {row['label']}",
                    command=_raw_ptp_visual_proof_command(row),
                    artifact_root="artifacts/falsification/r2_usb_ptp",
                    setting_ids=(setting_id,),
                    prerequisite=(
                        "Protocol and behavior probe artifacts exist; operator reviews near/far "
                        "target previews before marking behavior as proven."
                    ),
                    proves=(
                        "Human-visible focus behavior for this raw PTP row, with target setup "
                        "and source artifact evidence."
                    ),
                    risk="focus_visual_review",
                )
            )
        commands.append(
            ProbeCommand(
                probe_id="raw_ptp.artifact_review",
                route="raw_ptp",
                title="Raw PTP registry evidence review",
                command=(
                    "python3 tools/review_raw_ptp_artifacts.py "
                    "artifacts/falsification/r2_usb_ptp "
                    "--out-dir artifacts/falsification/r2_raw_ptp_review"
                ),
                artifact_root="artifacts/falsification/r2_raw_ptp_review",
                setting_ids=raw_rows,
                prerequisite=(
                    "Run raw_ptp.protocol_acceptance plus AF/manual behavior probes first, "
                    "then add a visual proof marker only after human confirmation."
                ),
                proves="Which raw PTP focus rows have protocol acceptance, behavior metrics, and visual proof.",
                risk="local_artifact_review",
            )
        )

    command_rows = _route_rows(rows, "camera_remote_command")
    if command_rows and include("camera_remote_command"):
        commands.append(
            ProbeCommand(
                probe_id="camera_remote_command.package_locator",
                route="camera_remote_command",
                title="Sony Camera Remote Command local package locator",
                command=(
                    "python3 tools/find_camera_remote_command_package.py "
                    "--include-common-search "
                    "--out-dir artifacts/falsification/r4_camera_remote_command"
                ),
                artifact_root="artifacts/falsification/r4_camera_remote_command",
                setting_ids=command_rows,
                prerequisite=(
                    "If the Sony package was obtained through the approved path, place it in a local "
                    "directory such as ~/Downloads, ~/Development, ~/Code, or pass --candidate to the locator."
                ),
                proves=(
                    "Whether a local Camera Remote Command package candidate is present, without "
                    "downloading, copying, or redistributing Sony files."
                ),
                risk="local_package_discovery",
            )
        )
        commands.append(
            ProbeCommand(
                probe_id="camera_remote_command.package_review",
                route="camera_remote_command",
                title="Sony Camera Remote Command package compatibility review",
                command=(
                    "python3 tools/review_camera_remote_command_package.py "
                    "/path/to/CameraRemoteCommand-package.zip "
                    "--out-dir artifacts/falsification/r4_camera_remote_command"
                ),
                artifact_root="artifacts/falsification/r4_camera_remote_command",
                setting_ids=command_rows,
                prerequisite="Sony package obtained through the approved corporate path and stored locally.",
                proves="Official ILCE-7M2 support rows and command availability without redistributing Sony files.",
                risk="local_package_read",
            )
        )
        command_rows_by_id = {row["setting_id"]: row for row in rows if row["setting_id"] in command_rows}
        for setting_id in command_rows:
            row = command_rows_by_id[setting_id]
            commands.append(
                ProbeCommand(
                    probe_id=f"camera_remote_command.package_review.{setting_id}",
                    route="camera_remote_command",
                    title=f"Sony Camera Remote Command one-setting package review: {row['label']}",
                    command=_camera_remote_command_setting_review_command(row),
                    artifact_root="artifacts/falsification/r4_camera_remote_command",
                    setting_ids=(setting_id,),
                    prerequisite=(
                        "Sony package obtained through the approved corporate path and stored locally; "
                        "this command filters the local package review to one registry row."
                    ),
                    proves=(
                        "Whether the official package contains ILCE-7M2-compatible command evidence "
                        "for this exact setting row, without redistributing Sony files."
                    ),
                    risk="local_package_read",
                )
            )

    open_rows = _route_rows(rows, "openmemories")
    # Public session/API surfaces intercept this permanently disabled route
    # before plan construction; retain historical parser support only here.
    if open_rows and include("openmemories"):
        commands.append(
            ProbeCommand(
                probe_id="openmemories.route3_preflight",
                route="openmemories",
                title="OpenMemories clean-room proof-app preflight",
                command=(
                    "python3 tools/falsify_openmemories_route3.py "
                    "--generate-app --try-build --out-dir artifacts/falsification/r3_openmemories"
                ),
                artifact_root="artifacts/falsification/r3_openmemories",
                setting_ids=open_rows,
                prerequisite="OpenMemories/PMCA references are local; no Sony APK patching or redistribution.",
                proves="Whether the Mac can build/install a reversible camera-hosted proof app route.",
                risk="tooling_and_admin_probe",
            )
        )
        commands.append(
            ProbeCommand(
                probe_id="openmemories.endpoint_surface",
                route="openmemories",
                title="OpenMemories proof-app safe HTTP endpoint proof",
                command=(
                    "python3 tools/prove_openmemories_endpoint_surface.py "
                    f'--base "{OPENMEMORIES_BASE_SHELL}" '
                    "--setting-id preview.running "
                    "--include-parameter-dump "
                    "--out-dir artifacts/falsification/r3_openmemories"
                ),
                artifact_root="artifacts/falsification/r3_openmemories",
                setting_ids=open_rows,
                prerequisite=(
                    "OpenMemories proof app is built, installed, launched, and reachable on the camera LAN IP."
                ),
                proves=(
                    "That the camera-hosted proof app answers /health, /settings, one safe setting "
                    "endpoint, and the read-only Camera.Parameters dump endpoints."
                ),
                risk="live_camera_read",
            )
        )
        open_rows_by_id = {row["setting_id"]: row for row in rows if row["setting_id"] in open_rows}
        for setting_id in open_rows:
            row = open_rows_by_id[setting_id]
            commands.append(
                ProbeCommand(
                    probe_id=f"openmemories.setting_endpoint.{setting_id}",
                    route="openmemories",
                    title=f"OpenMemories proof-app one-setting endpoint proof: {row['label']}",
                    command=_openmemories_setting_endpoint_command(row),
                    artifact_root="artifacts/falsification/r3_openmemories",
                    setting_ids=(setting_id,),
                    prerequisite=(
                        "OpenMemories proof app is built, installed, launched, and reachable on "
                        "the camera LAN IP; this safe read calls /health, /settings, and the "
                        "exact /settings/{id} shown in the command."
                    ),
                    proves=(
                        "That the camera-hosted proof app exposes this exact registry row through "
                        "its safe /settings/{id} route."
                    ),
                    risk="live_camera_read",
                )
            )
        commands.append(
            ProbeCommand(
                probe_id="openmemories.action_endpoint_surface",
                route="openmemories",
                title="OpenMemories proof-app preview/focus/capture endpoint proof",
                command=(
                    "python3 tools/prove_openmemories_endpoint_surface.py "
                    f'--base "{OPENMEMORIES_BASE_SHELL}" '
                    "--setting-id preview.running "
                    "--include-actions --allow-camera-actions "
                    "--out-dir artifacts/falsification/r3_openmemories"
                ),
                artifact_root="artifacts/falsification/r3_openmemories",
                setting_ids=open_rows,
                prerequisite=(
                    "Safe endpoint proof succeeded; operator accepts preview/focus/capture proof actions."
                ),
                proves=(
                    "That the camera-hosted proof app accepts preview, autofocus, and capture actions "
                    "for Route 3 behavior evidence."
                ),
                risk="live_camera_capture_focus",
            )
        )
        commands.append(
            ProbeCommand(
                probe_id="openmemories.rollback_port_scan",
                route="openmemories",
                title="OpenMemories rollback developer-port closure proof",
                command=(
                    "python3 tools/prove_openmemories_rollback_ports.py "
                    f'--host "{CAMERA_LAN_HOST_SHELL}" '
                    "--out-dir artifacts/falsification/r3_openmemories"
                ),
                artifact_root="artifacts/falsification/r3_openmemories",
                setting_ids=open_rows,
                prerequisite=(
                    "OpenMemories proof app/developer access has been stopped or rolled back; camera is "
                    "reachable on the LAN IP."
                ),
                proves="That Telnet, ADB, and proof-app HTTP ports no longer accept TCP connections.",
                risk="rollback_port_scan",
            )
        )
        commands.append(
            ProbeCommand(
                probe_id="openmemories.artifact_review",
                route="openmemories",
                title="OpenMemories Route 3 registry evidence review",
                command=(
                    "python3 tools/review_openmemories_artifacts.py "
                    "artifacts/falsification/r3_openmemories "
                    "--out-dir artifacts/falsification/r3_openmemories_review"
                ),
                artifact_root="artifacts/falsification/r3_openmemories_review",
                setting_ids=open_rows,
                prerequisite=(
                    "Run openmemories.route3_preflight first; add endpoint proof artifacts after "
                    "the app is built, installed, launched, and rollback ports are scanned."
                ),
                proves=(
                    "Which OpenMemories rows have tooling, generated app endpoints, HTTP proof, "
                    "and rollback/admin-port evidence."
                ),
                risk="local_artifact_review",
            )
        )

    if include("guided_physical"):
        for row in _physical_rows(rows):
            commands.append(
                ProbeCommand(
                    probe_id=f"guided_physical.{row['setting_id']}",
                    route="guided_physical",
                    title=f"Guided physical/state gate proof: {row['label']}",
                    command=_physical_gate_command(row),
                    artifact_root="artifacts/falsification/manual_gates",
                    setting_ids=(row["setting_id"],),
                    prerequisite=(
                        "Operator records the visible camera state and any photos/screenshots; "
                        "admin/destructive rows are observation-only and must not be executed."
                    ),
                    proves=(
                        "That this row is a physical/admin/state gate unless another route later proves "
                        "safe control; dangerous/admin rows prove observation only, not execution."
                    ),
                    risk="manual_admin_observation" if _admin_guarded(row) else "manual_observation",
                )
            )

    return commands


def build_probe_report(
    registry: dict[str, Any] | None = None,
    *,
    setting_id: str | None = None,
    menu_group: str | None = None,
    status: str | None = None,
    route: str | None = None,
) -> dict[str, Any]:
    registry = registry or settings_registry.load_registry()
    if route == "openmemories":
        return {"summary": {"selected_gap_rows": 0, "probe_commands": 0,
                            "covered_gap_rows": 0, "missing_gap_rows": [],
                            "status": "permanently_disabled"}, "commands": []}
    selected_rows = _filter_rows(
        registry,
        setting_id=setting_id,
        menu_group=menu_group,
        status=status,
        route=route,
    )
    if route is None:
        selected_rows = [
            row
            for row in selected_rows
            if any(name not in settings_registry.DISABLED_ROUTES for name in row.get("routes", {}))
        ]
    commands = build_probe_commands(
        registry,
        setting_id=setting_id,
        menu_group=menu_group,
        status=status,
        route=route,
    )
    covered = {setting_id for command in commands for setting_id in command.setting_ids}
    missing = sorted(row["setting_id"] for row in selected_rows if row["setting_id"] not in covered)
    return {
        "summary": {
            "selected_gap_rows": len(selected_rows),
            "probe_commands": len(commands),
            "covered_gap_rows": len(covered),
            "missing_gap_rows": missing,
        },
        "commands": [command.to_dict() for command in commands],
    }


def write_probe_report(path: str | Path, report: dict[str, Any]) -> None:
    Path(path).write_text(json.dumps(report, indent=2, sort_keys=True), encoding="utf-8")
