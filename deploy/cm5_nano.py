"""Raspberry Pi CM5 on a Waveshare CM5-NANO-A: board facts and reviewable config. Installs nothing.

Three things, all read-only or render-only:

* ``facts``  — classify ``/proc/device-tree/model`` (CM5, Pi 5 fallback, other) and record the
  carrier assumption. Software cannot identify the carrier, so it is an assumption, stated.
* ``render`` — print a ``config.txt`` fragment for the Nano-A and name the NetworkManager
  hotspot profile for the onboard ``wlan0``. Only ``--apply --out DIR`` writes them, into a NEW
  private directory outside the repository (deploy/safe_paths.py); the passphrase is never
  printed. Nothing is copied into ``/boot`` or ``/etc``; the runbook (deploy/CM5_BRINGUP.md)
  gives the operator commands.

Every generated line cites its source. Where no source supported a setting, it is left out and the
fragment says so. None of this has run on hardware yet: the board ships 2026-10-05.
"""
from __future__ import annotations

import sys

sys.dont_write_bytecode = True  # "writes nothing" includes __pycache__

import argparse  # noqa: E402
import hashlib  # noqa: E402
import json  # noqa: E402
import os  # noqa: E402
from pathlib import Path  # noqa: E402
import re  # noqa: E402
import secrets  # noqa: E402
import shutil  # noqa: E402
import tempfile  # noqa: E402
import uuid  # noqa: E402

# Run by path (`python3 deploy/cm5_nano.py`) or as a module; either way import from the repo root.
ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from deploy.safe_paths import path_arg, refusal, resolve_output  # noqa: E402

STAGED_MANIFEST = "STAGED.json"  # written last: a staged dir without it is incomplete
STAGED_SCHEMA = "camera-cm5-staged-v1"
STAGED_FILES = frozenset({"config.txt.cm5-nano-a", "camera-puck.nmconnection"})

MODEL_PATH = Path("/proc/device-tree/model")
WAVESHARE_WIKI = "https://www.waveshare.com/wiki/CM5-NANO-A"
OVERLAYS_README = "https://github.com/raspberrypi/linux/blob/rpi-6.12.y/arch/arm/boot/dts/overlays/README"
CONDITIONAL_DOC = "https://www.raspberrypi.com/documentation/computers/config_txt.html#conditional-filters"

CARRIERS = {
    "cm5-nano-a": {
        "name": "Waveshare CM5-NANO-A",
        "source": WAVESHARE_WIKI,
        "usb_host_ports": 1,
        "usb_a_current_limit_a": 2.0,
        "has_hdmi": False,
        "has_ethernet": False,
        "tf_slot_usable_with_emmc": False,
        "notes": [
            "One USB 3.2 Gen1 Type-A port, limited to 2 A: the camera takes it, so nothing else "
            "(ALFA adapter, keyboard, USB stick) can be attached without a hub.",
            "No HDMI and no Ethernet: wlan0 is the only link, so Wi-Fi and SSH must be baked into the image.",
            "USB-C is both the 5 V input and the rpiboot flashing port (BOOT held at power-on).",
            "The TF slot is disabled on eMMC modules; storage is the 16 GB eMMC only.",
        ],
    },
    "other": {"name": "unknown carrier", "source": None, "notes": [
        "Carrier not assumed; check its USB ports, power budget and network before any day-one test."]},
}


def read_model(path: Path = MODEL_PATH) -> str | None:
    """Return the device-tree model string, or None off a Raspberry Pi (e.g. the Mac)."""
    try:
        return path.read_bytes().replace(b"\x00", b"").decode("utf-8", "replace").strip() or None
    except OSError:
        return None


def classify_model(model: str | None) -> str:
    text = (model or "").strip()
    if "Raspberry Pi Compute Module 5" in text:
        return "cm5"
    # "Raspberry Pi 500" also starts with "Raspberry Pi 5": require a word boundary.
    if re.match(r"^Raspberry Pi 5( |$)", text):
        return "pi5"
    if text.startswith("Raspberry Pi"):
        return "raspberry-pi-other"
    return "unknown"


def board_facts(model: str | None, carrier: str = "auto") -> dict:
    """Board class plus the carrier ASSUMPTION. Never claims the carrier was detected."""
    board = classify_model(model)
    if carrier == "auto":
        carrier = "cm5-nano-a" if board == "cm5" else ("none" if board == "pi5" else "other")
    facts = {"model": model, "board": board, "is_cm5": board == "cm5", "is_pi5": board == "pi5",
             "carrier": carrier, "carrier_detected": False,
             "carrier_basis": "assumption: a CM5 here is the purchased CM5108016 on a Waveshare "
                              "CM5-NANO-A; carriers are not identifiable from software"}
    if carrier in CARRIERS:
        facts["carrier_facts"] = CARRIERS[carrier]
    elif carrier == "none":
        facts["carrier_basis"] = "Raspberry Pi 5 board: the named fallback, no carrier"
        facts["carrier_facts"] = {"name": "Raspberry Pi 5 (no carrier)", "notes": [
            "Named fallback board; the Pi 5 bring-up in deploy/PI_BRINGUP.md applies unchanged."]}
    else:
        raise ValueError("carrier must be auto, cm5-nano-a, other or none")
    facts["camera_usb_port"] = ("the single USB-A port" if carrier == "cm5-nano-a"
                                else "any USB-A port")
    return facts


def render_config_fragment() -> str:
    """config.txt lines for the Nano-A, each with its source. Scoped to [cm5] so a Pi 5 ignores it."""
    return f"""# camera-autopilot: Raspberry Pi CM5 on Waveshare CM5-NANO-A — config.txt fragment.
# GENERATED FOR REVIEW; nothing installs it. Append it to the END of /boot/firmware/config.txt
# by hand, then reboot. Unverified on hardware: the board has not arrived.
#
# [cm5] limits every line below to a Compute Module 5, and [all] resets the filter afterwards,
# so the Pi 5 fallback board ignores this block.
# Source: {CONDITIONAL_DOC}
[cm5]

# I2C on the 40-pin header, for the future IMU. The base device tree defaults it to "off".
# Source: {OVERLAYS_README} (base DTB params: i2c_arm).
dtparam=i2c_arm=on

# Fan curve for the carrier's 4-pin PWM fan header, copied verbatim from the Waveshare wiki
# "Fan Control" section (temperatures in millidegrees C, speed 0-255). Without these lines the
# firmware starts a fan at 50 C. The overlay README lists fan_temp0..3 as "Pi5 only"; Waveshare
# documents them for this CM5 carrier. Inert with the passive cooler that was bought: they only
# matter once a fan is plugged into the header. Unverified on the board.
# Source: {WAVESHARE_WIKI} and {OVERLAYS_README} (fan_temp0..3, _hyst, _speed).
dtparam=fan_temp0=36000,fan_temp0_hyst=2000,fan_temp0_speed=90
dtparam=fan_temp1=40000,fan_temp1_hyst=3000,fan_temp1_speed=150
dtparam=fan_temp2=52000,fan_temp2_hyst=4000,fan_temp2_speed=200
dtparam=fan_temp3=58000,fan_temp3_hyst=5000,fan_temp3_speed=255

# USB: deliberately nothing. The Nano-A's USB-A port is the CM5's own USB 3 controller and no
# source found says it needs a config.txt line. The dwc2 host-mode fix on Waveshare's flashing
# page is for the CM4, and whatever [cm5] lines the Raspberry Pi OS image already carries are left
# alone. usb_max_current_enable is NOT set: no source says how the CM5 firmware limit interacts
# with this carrier's 2 A limiter. host-facts records `vcgencmd get_config usb_max_current_enable`
# so the day-one receipts show the real value.
#
# Wi-Fi antenna: nothing set. ant1 (the module's on-board antenna) is the default per the overlay
# README; ant2 would need an external antenna, which was not bought.
[all]
"""


def render_hotspot_profile(passphrase: str, connection_uuid: str, interface: str = "wlan0",
                           channel: int = 6) -> str:
    """NetworkManager keyfile for an access point on the ONBOARD radio, chosen to avoid the
    brcmfmac AP-mode failures in deploy/CM5_BRINGUP.md, section 5."""
    if not re.fullmatch(r"[a-zA-Z0-9_.-]{1,15}", interface):
        raise ValueError("Invalid interface")
    if channel not in (1, 6, 11):
        raise ValueError("Channel must be 1, 6 or 11 (2.4 GHz, non-overlapping, never DFS)")
    if not re.fullmatch(r"[0-9a-f]{32,63}", passphrase):
        raise ValueError("Passphrase must be 32-63 lowercase hex characters")
    return f"""# camera-autopilot: onboard-Wi-Fi access point for the CM5 on the CM5-NANO-A.
# GENERATED FOR REVIEW; nothing installs it. NetworkManager keyfile, mode 0600.
# Install: sudo install -m 600 camera-puck.nmconnection /etc/NetworkManager/system-connections/
#          sudo nmcli connection reload
# Each setting below says which brcmfmac AP pitfall it avoids (deploy/CM5_BRINGUP.md, section 5).
[connection]
id=camera-puck
uuid={connection_uuid}
type=wifi
# The profile owns the physical wlan0 itself. No virtual AP interface (uap0) is ever created,
# and a NetworkManager device runs one connection at a time, so bringing this up replaces the
# client connection instead of running AP and client together.
# Avoids: firmware crash on a virtual-AP interface (raspberrypi/firmware#1463) and the
# "HT Avail timeout" wedge after AP+STA or repeated reloads (openwrt/openwrt#23069).
interface-name={interface}
# Never starts on its own: the board boots into the Imager-seeded client Wi-Fi so SSH stays
# reachable, and the AP comes up only when an operator or the soak test asks.
# Avoids: repeated automatic AP up/down cycles, the other trigger of the HT Avail wedge.
autoconnect=false

[wifi]
mode=ap
ssid=Camera-Puck
# 2.4 GHz only. DFS (radar detection) exists only on 5 GHz channels, so a 2.4 GHz AP cannot hit it.
# Avoids: the firmware stuck in DFS-UNSET, where hostapd reports AP-ENABLED but no beacons are
# sent (forums.raspberrypi.com t=392203). Channel 1, 6 or 11 (default 6); the regulatory domain
# comes from the Wi-Fi country set in Raspberry Pi Imager.
band=bg
channel={channel}

[wifi-security]
# WPA2-Personal (PSK) only. SAE, WPA3 and WPA2/WPA3 transition mode are deliberately absent.
# Avoids: the firmware crash with WPA3/SAE (raspberrypi/linux#7528).
key-mgmt=wpa-psk
# proto=rsn restricts the AP to RSN (WPA2) and excludes legacy WPA (WPA1).
# pairwise/group are NOT forced to ccmp: in the forum thread this profile follows
# (forums.raspberrypi.com t=357998) the poster set proto=rsn, group=ccmp and pairwise=ccmp together,
# clients failed, and removing all three fixed it. The thread never isolated which setting broke
# them, so proto=rsn alone is a deliberate choice, and itself untested: if a phone refuses the
# correct password, drop proto first. Client negotiation with this profile is UNTESTED until the
# day-one AP soak.
proto=rsn
psk={passphrase}

[ipv4]
# NetworkManager's own DHCP/NAT for the phone; same address as deploy/portable_bundle.py and its
# Caddyfile, so https://192.168.77.1 serves the app.
method=shared
address1=192.168.77.1/24
never-default=true

[ipv6]
method=disabled
"""


def render_files(channel: int = 6, interface: str = "wlan0") -> dict[str, str]:
    """The files to stage. The hotspot passphrase is new each render and never printed."""
    return {
        "config.txt.cm5-nano-a": render_config_fragment(),
        "camera-puck.nmconnection": render_hotspot_profile(
            secrets.token_hex(16), str(uuid.uuid4()), interface=interface, channel=channel),
    }


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _write_new(path: Path, text: str) -> None:
    """Create a NEW private file (never through a symlink, never over a file) and fsync it."""
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0), 0o600)
    with os.fdopen(descriptor, "w") as stream:
        stream.write(text)
        stream.flush()
        os.fsync(stream.fileno())


def _fsync_dir(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def write_files(output: Path, files: dict[str, str], allow_system: bool = False) -> dict:
    """Stage into a NEW private directory outside the repo, atomically.

    The path is resolved once and that same path is checked and written. Files go into a temporary
    sibling directory, each fsynced, then the STAGED.json manifest (hashes) last, then one rename
    puts the directory in place. A failure part-way (ENOSPC, a crash) leaves no directory at the
    final path, and a directory without a valid manifest never counts as staged (verify_staged).
    Refusing an existing target never rotates a passphrase."""
    output = resolve_output(output)
    reason = refusal(output, ROOT, allow_system)
    if reason:
        raise ValueError(reason)
    if os.path.lexists(output):
        raise FileExistsError(f"{output} already exists")
    output.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    temp = Path(tempfile.mkdtemp(prefix=f".{output.name}.partial-", dir=output.parent))
    try:
        for name, text in files.items():
            _write_new(temp / name, text)
        # Hash the exact bytes written (text mode does no newline translation on POSIX).
        manifest = {"schema": STAGED_SCHEMA,
                    "files": {name: _sha256(text.encode()) for name, text in files.items()}}
        _write_new(temp / STAGED_MANIFEST, json.dumps(manifest, indent=2) + "\n")
        _fsync_dir(temp)
        if os.path.lexists(output):
            raise FileExistsError(f"{output} appeared while staging")
        os.rename(temp, output)
        _fsync_dir(output.parent)
    except BaseException:
        shutil.rmtree(temp, ignore_errors=True)
        raise
    return {"directory": str(output), "files": sorted(files), "installed": False,
            "network_changed": False, "credentials_printed": False}


def verify_staged(directory: Path) -> str | None:
    """None when ``directory`` is a complete staging (manifest present, every hash matches)."""
    directory = Path(directory)
    if directory.is_symlink() or not directory.is_dir():
        return f"{directory} is not a real directory"
    try:
        manifest = json.loads((directory / STAGED_MANIFEST).read_bytes())
        expected = manifest["files"]
        schema = manifest["schema"]
    except (OSError, ValueError, KeyError, TypeError):
        return f"{directory} has no valid {STAGED_MANIFEST}: an incomplete or foreign staging"
    if schema != STAGED_SCHEMA:
        return f"{directory}/{STAGED_MANIFEST} has schema {schema!r}, not {STAGED_SCHEMA!r}"
    if not isinstance(expected, dict) or set(expected) != STAGED_FILES:
        return (f"{directory}/{STAGED_MANIFEST} must list exactly {sorted(STAGED_FILES)}; "
                f"it lists {sorted(expected) if isinstance(expected, dict) else expected!r}")
    for name in sorted(STAGED_FILES):
        path = directory / name
        if path.is_symlink() or not path.is_file():
            return f"{path} is missing or not a regular file"
        if _sha256(path.read_bytes()) != expected[name]:  # bytes: a CRLF rewrite must not pass
            return f"{path} does not match its manifest hash"
    return None


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = parser.add_subparsers(dest="command", required=True)
    facts = sub.add_parser("facts", help="print board facts as JSON (read-only)")
    facts.add_argument("--model-file", type=path_arg, default=MODEL_PATH)
    facts.add_argument("--model-text", help="classify this string instead of reading --model-file")
    facts.add_argument("--carrier", choices=["auto", "cm5-nano-a", "other", "none"], default="auto")
    render = sub.add_parser("render", help="print the config fragment; --apply --out writes a new private dir")
    render.add_argument("--out", type=path_arg, help="NEW private directory (outside the repo) for the staged files")
    render.add_argument("--apply", action="store_true", help="actually write --out (credentials only with this)")
    render.add_argument("--allow-system-dir", action="store_true",
                        help="with --apply, permit --out inside a system directory such as /etc")
    render.add_argument("--channel", type=int, choices=[1, 6, 11], default=6)
    render.add_argument("--interface", default="wlan0")
    verify = sub.add_parser("verify", help="exit 0 only if --dir is a complete staging (read-only)")
    verify.add_argument("--dir", type=path_arg, required=True)
    args = parser.parse_args(argv)
    if args.command == "verify":
        problem = verify_staged(args.dir)
        if problem:
            print(problem, file=sys.stderr)
            return 1
        return 0
    if args.command == "facts":
        model = args.model_text if args.model_text is not None else read_model(args.model_file)
        print(json.dumps(board_facts(model, args.carrier), indent=2))
        return 0
    if args.allow_system_dir and not args.apply:
        parser.error("--allow-system-dir needs --apply")
    if args.out:
        args.out = resolve_output(args.out)  # resolved once: this exact path is checked and written
        reason = refusal(args.out, ROOT, args.allow_system_dir)
        if reason:
            print(reason, file=sys.stderr)
            return 2
    files = render_files(args.channel, args.interface)
    if args.out and args.apply:
        try:
            print(json.dumps(write_files(args.out, files, args.allow_system_dir), indent=2))
        except (OSError, ValueError) as exc:
            print(f"render failed: {exc}", file=sys.stderr)
            return 1
    else:
        # Dry run: show the fragment and name the profile; the passphrase is never printed.
        print(files["config.txt.cm5-nano-a"], end="")
        print(json.dumps({"dry_run": True, "would_write": str(args.out) if args.out else None,
                          "files": sorted(files), "written": False, "installed": False,
                          "credentials_printed": False}, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
