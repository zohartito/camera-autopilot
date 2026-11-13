"""Probe: what does THIS A7 II actually expose over USB? Run with the camera powered + PC Remote.

    ./.venv/bin/python tests/probe_capabilities.py

Dumps every gphoto2 config node (name / read-only / value) and then checks the specific Sony
properties that decide our "hard limits" — so we KNOW, for our exact body + firmware, which limits
are real walls and which were just a camera-state precondition or a missing config node.

Grounded in the deep-dive: the real ILCE-7M2 fw4.0 dump (libgphoto2 #762) shows movie 0xD2C8,
colortemperature 0xD20F and sensorcrop 0xD219 as READWRITE, but AFAreaPosition 0xD2DC / FocusArea
0xD22C ABSENT. This probe confirms that on the camera in front of us instead of trusting a stranger's
dump. Read-only and absent are different verdicts: read-only often unlocks once a precondition is met
(e.g. colortemperature is writable only when white balance is set to the Kelvin/Color-Temp mode);
absent means the firmware genuinely lacks the control.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import gphoto2 as gp

from sonyremote import Camera, CameraError, detect

# friendly gphoto2 node name -> (what it controls, which hard-limit it settles)
WATCH = {
    "movie": "native video record (0xD2C8) -> limit #9",
    "colortemperature": "WB Kelvin (0xD20F) -> limit #6",
    "sensorcrop": "APS-C crop (0xD219) -> limit #6",
    "exposurecompensation": "EV comp (0xD224) -> limit #3",
    "manualfocus": "MF step (0xD2D1) -> limit #2",
    "spotfocusarea": "AF point X/Y (0xD2DC) -> limit #10",
    "focusarea": "AF area mode (0xD22C) -> limit #10",
    "expprogram": "P/A/S/M over USB (0x500E) -> limit #4",
    "shutterspeed": "shutter (0xD20D)",
    "iso": "ISO (0xD21E)",
    "f-number": "aperture (0x5007)",
    "focusmode": "AF/MF mode",
    "capturemode": "drive mode",
    "imagequality": "RAW/JPEG (0x5004) -> limit #7",
}

LEAF_TYPES = {gp.GP_WIDGET_TEXT, gp.GP_WIDGET_RANGE, gp.GP_WIDGET_TOGGLE,
              gp.GP_WIDGET_RADIO, gp.GP_WIDGET_MENU, gp.GP_WIDGET_DATE}


def walk(widget, found):
    """Recurse the config tree, recording every leaf node by name."""
    try:
        wtype = widget.get_type()
    except gp.GPhoto2Error:
        return
    if wtype in LEAF_TYPES:
        name = widget.get_name()
        try:
            readonly = bool(widget.get_readonly())
        except gp.GPhoto2Error:
            readonly = None
        try:
            value = widget.get_value()
        except gp.GPhoto2Error:
            value = "?"
        found[name] = {"readonly": readonly, "value": value}
    for child in widget.get_children():
        walk(child, found)


def main() -> int:
    if not detect():
        print("FAIL: no camera. Power it on, set USB Connection -> PC Remote, reconnect.")
        return 1
    print(f"libgphoto2: {gp.gp_library_version(gp.GP_VERSION_SHORT)[0]}")
    try:
        with Camera() as cam:
            gp_cam = cam._require()
            print("connected :", cam.info().model)
            found = {}
            walk(gp_cam.get_config(), found)
            print(f"\n{len(found)} controllable nodes total.\n")
            print("=== nodes that decide our hard limits ===")
            for name, meaning in WATCH.items():
                if name in found:
                    ro = found[name]["readonly"]
                    flag = "READ-ONLY" if ro else "writable" if ro is False else "?"
                    print(f"  [PRESENT] {name:22} {flag:10} = {found[name]['value']!r:>16}  ({meaning})")
                else:
                    print(f"  [ABSENT ] {name:22} {'—':10} {'':>16}  ({meaning}) -> firmware wall")
            print("\n=== full node dump (rw = writable, ro = read-only) ===")
            for name in sorted(found):
                ro = found[name]["readonly"]
                flag = "ro" if ro else "rw" if ro is False else " ?"
                print(f"  {flag}  {name:28} = {found[name]['value']!r}")
            return 0
    except CameraError as exc:
        print(f"probe failed: {exc}")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
