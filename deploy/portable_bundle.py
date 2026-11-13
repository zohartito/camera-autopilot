"""Render private, reviewable Linux host configuration. Never installs or starts it."""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import re
import secrets
import sys
import uuid

# Run either as a module (`python -m deploy.portable_bundle`, repo root already on
# sys.path) or by path (`python deploy/portable_bundle.py`, what a person on a
# fresh board types). The path form starts with `deploy/` on sys.path, not the
# repo root, so bootstrap the root before importing the backend package.
ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from deploy.provision import render_unit
from backend.device_health import RADIO_CONFIGURATIONS, radio_configuration

PROFILES = {
    "pi5-hybrid": {"architecture": "aarch64", "memory_minimum_gib": 8,
                   "heavy_rendering": "phone", "shipping_board_verified": False},
    "cm5-hybrid": {"architecture": "aarch64", "memory_minimum_gib": 8,
                   "heavy_rendering": "phone", "shipping_board_verified": False},
    "jetson-hybrid": {"architecture": "aarch64", "memory_minimum_gib": 8,
                      "heavy_rendering": "phone", "shipping_board_verified": False},
}


def render_bundle(profile="pi5-hybrid", interface="wlan0", country="US",
                  repo=Path("/opt/camera-autopilot"), config=Path("/etc/camera-autopilot"),
                  band="2.4ghz", channel=None):
    if profile not in PROFILES:
        raise ValueError("Unknown host profile")
    if not re.fullmatch(r"[a-zA-Z0-9_.-]{1,15}", interface):
        raise ValueError("Invalid interface")
    if not re.fullmatch(r"[A-Z]{2}", country):
        raise ValueError("Country must be a two-letter regulatory code")
    if not repo.is_absolute() or not config.is_absolute():
        raise ValueError("Deployment paths must be absolute")
    radio = radio_configuration(band, channel)
    band_config = RADIO_CONFIGURATIONS[band]
    api_token, wifi_passphrase = secrets.token_urlsafe(36), secrets.token_hex(16)
    identity = str(uuid.uuid4())
    files = {
        "api-token": api_token + "\n",
        "runtime.env": ("CAMERA_ENGINE=real\nCAMERA_AUTOPILOT_EXPOSURE=local\n"
                        "CAMERA_MODEL=Sony ILCE-7M2 (Control)\n"
                        f"CAMERA_AUTOPILOT_API_TOKEN={api_token}\n"
                        f"CAMERA_AUTOPILOT_DEVICE_ID={identity}\n"
                        f"CAMERA_AUTOPILOT_PORTABLE_PROFILE={config / 'profile.json'}\n"
                        "CAMERA_INTENT_PROFILE=compact\nCAMERA_AUTOPILOT_TASTE_STORAGE=auto\n"
                        "HF_HUB_OFFLINE=1\nTRANSFORMERS_OFFLINE=1\n"
                        "HF_DATASETS_OFFLINE=1\nVISION_HOST=http://127.0.0.1:11434\n"),
        "camera-autopilot.service": render_unit(repo, config / "runtime.env"),
        "camera-puck.nmconnection": f"""[connection]
id=camera-puck
uuid={identity}
type=wifi
interface-name={interface}
autoconnect=false

[wifi]
mode=ap
ssid=Camera-Puck
band={band_config["networkmanager_band"]}
channel={radio["channel"]}

[wifi-security]
key-mgmt=wpa-psk
psk={wifi_passphrase}

[ipv4]
method=shared
address1=192.168.77.1/24
never-default=true

[ipv6]
method=disabled
""",
        "hostapd.conf": f"""# Alternative to NetworkManager. Do not run both on one interface.
interface={interface}
driver=nl80211
ssid=Camera-Puck
country_code={country}
ieee80211d=1
hw_mode={band_config["hostapd_mode"]}
channel={radio["channel"]}
wpa=2
wpa_key_mgmt=WPA-PSK
rsn_pairwise=CCMP
wpa_passphrase={wifi_passphrase}
""",
        "dnsmasq.conf": f"""# Only for hostapd alternative; assign 192.168.77.1/24 first.
interface={interface}
listen-address=192.168.77.1
bind-interfaces
port=0
dhcp-range=192.168.77.10,192.168.77.100,255.255.255.0,12h
dhcp-option=3
dhcp-option=6
""",
        "Caddyfile": """# Install trusted CA on the companion phone before entering credentials.
https://192.168.77.1 {
    bind 192.168.77.1
    tls internal
    reverse_proxy 127.0.0.1:8000
}
""",
        "offline-artifacts.json": json.dumps({"schema": "camera-offline-artifacts-v1",
            "artifacts": [], "instructions": "Populate exact local runtime/model/index paths, byte sizes and sha256; empty fails readiness."}, indent=2) + "\n",
        "profile.json": json.dumps({"schema": "camera-portable-host-v1", "profile": profile,
            **PROFILES[profile], "interface": interface, "country": country,
            "ap_method": "choose-networkmanager-or-hostapd",
            "api": "https://192.168.77.1", "ble_enabled": False,
            "device_id": identity, "radio": radio,
            "camera_selector_requires_live_confirmation": True,
            "power_thermal_endurance_measured": False}, indent=2) + "\n",
    }
    return files


def write_bundle(output: Path, files: dict[str, str]):
    # Refuse replacement so repeated rendering cannot silently rotate credentials.
    output.mkdir(mode=0o700, parents=True, exist_ok=False)
    for name, text in files.items():
        destination = output / name
        descriptor = os.open(destination, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(descriptor, "w") as stream:
            stream.write(text)
    # Hash only public configuration names; credentials never appear in report.
    return {"directory": str(output), "files": sorted(files), "installed": False,
            "network_changed": False, "services_started": False,
            "credentials_printed": False}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--profile", choices=sorted(PROFILES), default="pi5-hybrid")
    parser.add_argument("--interface", default="wlan0")
    parser.add_argument("--country", default="US")
    parser.add_argument("--band", choices=sorted(RADIO_CONFIGURATIONS), default="2.4ghz")
    parser.add_argument("--channel", type=int)
    parser.add_argument("--repo", type=Path, default=Path("/opt/camera-autopilot"))
    parser.add_argument("--config", type=Path, default=Path("/etc/camera-autopilot"))
    parser.add_argument("--output", type=Path, help="Create a NEW private review directory")
    args = parser.parse_args(argv)
    files = render_bundle(args.profile, args.interface, args.country, args.repo, args.config, args.band, args.channel)
    report = write_bundle(args.output, files) if args.output else {
        "dry_run": True, "profile": args.profile, "files": sorted(files),
        "radio": radio_configuration(args.band, args.channel),
        "installed": False, "network_changed": False, "services_started": False,
        "credentials_printed": False}
    print(json.dumps(report, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
