#!/usr/bin/env python3
"""Read a connected camera's settings and choices without shooting or writing settings."""
from __future__ import annotations

import json
import argparse
import sys
from dataclasses import asdict
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "backend"))

from sonyremote import Camera, CameraError, detect, detect_identified


def inspect_camera(camera) -> dict:
    awake = camera.wake()
    report = {
        "awake": awake,
        "camera": asdict(camera.info()),
        "capabilities": camera.capabilities(),
        "shooting_mode": camera.shooting_mode(),
        "settings": camera.get_settings(),
        "controls": {},
        "evidence": "read-only; driver choices can exceed body/lens limits and are not verified writes or captures",
    }
    for node in ("iso", "aperture", "shutter", "focusmode", "focusarea", "imagequality", "capturemode",
                 "whitebalance", "colortemperature", "movie", "moviequality",
                 "moviesettings", "manualfocus"):
        try:
            report["controls"][node] = camera.describe_setting(node)
        except CameraError as exc:
            report["controls"][node] = {"unavailable": str(exc)}
    return report


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--model", help="Exact model from --list (or CAMERA_MODEL)")
    parser.add_argument("--port", help="Exact USB port from --list (or CAMERA_PORT)")
    parser.add_argument("--list", action="store_true",
                        help="List bodies with their OS USB serial (the --serial other tools require) "
                             "without claiming a camera")
    args = parser.parse_args(argv)
    detected = []
    try:
        if args.list:
            # One discovery pass (its recovery probe can briefly claim a body): both views come
            # from the same rows. The OS USB serial is the only stable identity, since ports
            # re-enumerate; serial_status says when a body has no usable one (missing/duplicate).
            rows = detect_identified()
            detected = [(row.get("model"), row.get("port")) for row in rows]
            identified = [{"model": row.get("model"), "port": row.get("port"), "serial": row.get("serial"),
                           "serial_status": row.get("serial_status") or ("present" if row.get("serial") else "missing")}
                          for row in rows]
            print(json.dumps({"ok": True, "detected": detected, "identified": identified}, indent=2))
            return 0
        detected = detect()
        with Camera(model=args.model, port=args.port) as camera:
            report = inspect_camera(camera)
    except (CameraError, ValueError) as exc:
        print(json.dumps({"ok": False, "detected": detected, "error": str(exc)}, indent=2))
        return 1
    print(json.dumps({"ok": True, **report}, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
