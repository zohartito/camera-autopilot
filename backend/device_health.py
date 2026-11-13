"""Read-only host telemetry and offline readiness; unknown values remain null."""
from __future__ import annotations

import hashlib
import json
import math
import os
from pathlib import Path
import platform
import shutil
import stat
import time
import uuid


def _text(path: Path) -> str | None:
    try:
        with path.open() as stream:
            value = stream.read(4097)
        return value.strip() if len(value) <= 4096 else None
    except (OSError, UnicodeError):
        return None


def _number(path: Path, low: float, high: float) -> float | None:
    try:
        value = float(_text(path))
        return value if math.isfinite(value) and low <= value <= high else None
    except (TypeError, ValueError):
        return None


RADIO_CONFIGURATIONS = {
    "2.4ghz": {"label": "2.4 GHz", "networkmanager_band": "bg", "hostapd_mode": "g",
                "candidate_channels": [1, 6, 11], "default_channel": 6},
    "5ghz": {"label": "5 GHz", "networkmanager_band": "a", "hostapd_mode": "a",
             "candidate_channels": [36, 40, 44, 48], "default_channel": 36},
}
_PROCESS_INSTANCE_ID = str(uuid.uuid4())


def radio_configuration(band="2.4ghz", channel=None):
    """Renderer choices, never a statement of hardware/regulatory support."""
    if band not in RADIO_CONFIGURATIONS:
        raise ValueError("Band must be 2.4ghz or 5ghz")
    profile = RADIO_CONFIGURATIONS[band]
    channel = profile["default_channel"] if channel is None else channel
    if type(channel) is not int or channel not in profile["candidate_channels"]:
        raise ValueError("Channel must be a candidate for the selected band")
    return {"band": band, "channel": channel, "status": "configured_only",
            "hardware_support_verified": False, "regulatory_support_verified": False,
            "runtime_access_point_verified": False}


def radio_status(sys_root="/sys", profile_path=None):
    """Read configured intent and sysfs presence separately; never start a radio.

    An interface being up is not AP mode or a measured band. Actual channel,
    regulatory/AP capability and RF range need target-host radio validation.
    """
    configured, configuration_error = None, None
    if profile_path is not None:
        try:
            descriptor = os.open(profile_path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
            with os.fdopen(descriptor, "rb") as stream:
                info = os.fstat(stream.fileno())
                if not stat.S_ISREG(info.st_mode) or info.st_size > 65536:
                    raise ValueError("Invalid portable profile")
                raw = stream.read(65537)
                if len(raw) > 65536:
                    raise ValueError("Invalid portable profile")
            value = json.loads(raw)
            if value.get("schema") != "camera-portable-host-v1":
                raise ValueError("Invalid portable profile")
            radio = value["radio"]
            configured = radio_configuration(radio["band"], radio["channel"])
        except (OSError, ValueError, KeyError, TypeError, AttributeError, UnicodeError):
            configuration_error = "Configured portable profile is invalid or unavailable"
    interfaces = []
    for path in sorted((Path(sys_root) / "class/net").glob("*"))[:64]:
        if (path / "wireless").is_dir() or (path / "phy80211").exists():
            interfaces.append({"name": path.name, "operstate": _text(path / "operstate")})
    return {"schema": "camera-radio-state-v1", "configuration": configured,
            "configuration_error": configuration_error,
            "renderable_band_choices": [{"id": name, "label": info["label"],
                "candidate_channels": list(info["candidate_channels"])} for name, info in RADIO_CONFIGURATIONS.items()],
            "probe": {"source": "linux_sysfs_interface_presence", "interfaces": interfaces,
                      "supported_bands": None, "ap_mode_supported": None,
                      "regulatory_channels": None},
            "runtime": {"active_band": None, "active_channel": None,
                        "access_point_verified": False},
            "selection_action": "render_configuration_only",
            "note": "Choose a candidate configuration for later target validation. No host setting is changed; renderer choices are not detected radio capabilities."}


def known_server_identity():
    """Identity for an authenticated request to an already known server URL.

    No network scans, mDNS, BLE, arbitrary-URL fetches, or camera operations.
    A configured device UUID persists with the reviewed deployment environment;
    otherwise only this backend process has an identity, explicitly labeled.
    """
    configured = os.getenv("CAMERA_AUTOPILOT_DEVICE_ID")
    device_id, invalid = None, False
    if configured:
        try:
            device_id = str(uuid.UUID(configured))
        except (ValueError, AttributeError):
            invalid = True
    name = os.getenv("CAMERA_AUTOPILOT_DEVICE_NAME", "Camera Autopilot")
    if not name or len(name) > 64 or any(ord(c) < 32 for c in name):
        name = "Camera Autopilot"
    return {"schema": "camera-known-server-v1", "service": "camera-autopilot",
            "protocol_version": 1, "display_name": name, "device_id": device_id,
            "process_instance_id": _PROCESS_INSTANCE_ID,
            "identity_scope": "configured_device" if device_id else "backend_process",
            "configuration_error": "Invalid configured device UUID" if invalid else None,
            "discovery_mode": "known_server_url", "automatic_network_discovery": False,
            "ble_discovery_available": False, "control_requires_authentication": True,
            "remote_transport": "trusted_https_required"}


def device_health(storage: Path | str, sys_root: Path | str = "/sys") -> dict:
    """No sudo, subprocesses, network requests, or fabricated battery estimates."""
    sys_root = Path(sys_root)
    batteries = []
    for supply in sorted((sys_root / "class/power_supply").glob("*"))[:32]:
        if _text(supply / "type") != "Battery":
            continue
        power = _number(supply / "power_now", 0, 1e9)
        batteries.append({"name": supply.name,
                          "percent": _number(supply / "capacity", 0, 100),
                          "status": _text(supply / "status"),
                          "power_watts": power / 1e6 if power is not None else None})
    temperatures = []
    for zone in sorted((sys_root / "class/thermal").glob("thermal_zone*"))[:64]:
        temperature = _number(zone / "temp", -100000, 250000)
        if temperature is not None:
            temperatures.append({"name": _text(zone / "type") or zone.name,
                                 "celsius": temperature / 1000})
    try:
        disk = shutil.disk_usage(storage)
        free_bytes, total_bytes = disk.free, disk.total
    except OSError:
        free_bytes = total_bytes = None
    return {"schema": "camera-device-health-v1", "observed_at_unix": time.time(),
            "platform": platform.system(), "battery": batteries or None,
            "temperature": temperatures or None, "free_bytes": free_bytes,
            "total_bytes": total_bytes, "power_telemetry_available": any(
                item["power_watts"] is not None for item in batteries),
            "physical_endurance_verified": False,
            "radio": radio_status(sys_root, os.getenv("CAMERA_AUTOPILOT_PORTABLE_PROFILE"))}


def validate_offline_manifest(manifest: Path | str, root: Path | str) -> dict:
    """Verify declared local bytes. Manifest never causes a download or execution."""
    root = Path(root).resolve()
    errors, checked = [], []
    try:
        path = Path(manifest)
        if path.stat().st_size > 1024 * 1024:
            raise ValueError("manifest exceeds 1 MiB")
        payload = json.loads(path.read_text())
        if payload.get("schema") != "camera-offline-artifacts-v1":
            raise ValueError("unsupported manifest schema")
        artifacts = payload["artifacts"]
        if not isinstance(artifacts, list) or not artifacts or len(artifacts) > 1024:
            raise ValueError("manifest needs 1..1024 artifacts")
        seen = set()
        for entry in artifacts:
            relative = entry["path"]
            if not isinstance(relative, str) or relative in seen:
                raise ValueError("artifact path missing or duplicated")
            seen.add(relative)
            candidate = root / relative
            if Path(relative).is_absolute() or ".." in Path(relative).parts or candidate.is_symlink():
                raise ValueError("artifact path must be a local regular file")
            candidate.resolve().relative_to(root)
            expected_size = entry["size_bytes"]
            expected_hash = entry["sha256"]
            if (type(expected_size) is not int or expected_size < 0 or
                    not isinstance(expected_hash, str) or len(expected_hash) != 64 or
                    any(c not in "0123456789abcdef" for c in expected_hash)):
                raise ValueError("artifact size/hash invalid")
            try:
                info = candidate.stat()
                if not stat.S_ISREG(info.st_mode) or info.st_size != expected_size:
                    raise ValueError("file missing, not regular, or size differs")
                digest = hashlib.sha256()
                with candidate.open("rb") as stream:
                    for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                        digest.update(chunk)
                if digest.hexdigest() != expected_hash:
                    raise ValueError("sha256 differs")
                checked.append(relative)
            except (OSError, ValueError) as exc:
                errors.append({"path": relative, "reason": str(exc)})
    except (OSError, ValueError, KeyError, TypeError, AttributeError) as exc:
        errors.append({"path": None, "reason": f"Invalid or unavailable offline manifest: {exc}"})
    return {"ready": not errors, "verified_artifacts": checked, "errors": errors,
            "downloads_attempted": False}


def portable_readiness(storage: Path | str, token_file: Path | str | None = None,
                       manifest: Path | str | None = None, artifact_root: Path | str = ".",
                       minimum_free_bytes: int = 2 * 1024**3,
                       sys_root: Path | str = "/sys") -> dict:
    if type(minimum_free_bytes) is not int or minimum_free_bytes < 0:
        raise ValueError("minimum_free_bytes must be nonnegative")
    health = device_health(storage, sys_root)
    blockers, unknowns = [], []
    if health["free_bytes"] is None or health["free_bytes"] < minimum_free_bytes:
        blockers.append("Insufficient or unreadable free storage")
    try:
        path = Path(token_file) if token_file else None
        if path is None:
            raise ValueError("missing token")
        info = path.lstat()
        if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or
                stat.S_IMODE(info.st_mode) != 0o600 or not 24 <= info.st_size <= 512):
            raise ValueError("invalid token file")
        token = path.read_text().strip()
        if len(token) < 24 or any(c.isspace() for c in token):
            raise ValueError("invalid token")
    except (OSError, ValueError, UnicodeError):
        blockers.append("Private owned 0600 token file with at least 24 characters is required")
    offline = validate_offline_manifest(manifest, artifact_root) if manifest else {
        "ready": False, "errors": [{"reason": "Offline artifact manifest required"}],
        "verified_artifacts": [], "downloads_attempted": False}
    if not offline["ready"]:
        blockers.append("Offline artifacts are not verified")
    if health["battery"] is None:
        unknowns.append("Battery level unavailable")
    if not health["power_telemetry_available"]:
        unknowns.append("Power draw and endurance unavailable")
    if health["temperature"] is None:
        unknowns.append("Temperature unavailable")
    return {"ready": not blockers, "field_ready": False, "blockers": blockers,
            "unknowns": unknowns, "health": health, "offline_artifacts": offline}
