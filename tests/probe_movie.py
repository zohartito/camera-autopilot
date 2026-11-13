"""Probe: can THIS Sony body record native video over USB? Run with the camera powered + PC Remote.

    CAMERA_SERIAL=A7II00SERIAL ./.venv/bin/python tests/probe_movie.py

The movie toggle only presses the body proven by CAMERA_SERIAL (its USB serial).

Checks for gphoto2's Sony 'movie' control node, then attempts a short REAL recording. This is the
only definitive test of whether the A7 II honors the Sony movie control (PTP 0xD2C8).
"""

import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sonyremote import CameraError, camera_for_serial, detect


def main() -> int:
    found = detect()
    print("detect():", found or "NOTHING")
    if not found:
        print("FAIL: no camera. Power it on, set USB Connection -> PC Remote, reconnect.")
        return 1
    if not os.getenv("CAMERA_SERIAL"):
        print("FAIL: set CAMERA_SERIAL to the body's USB serial; ports re-enumerate.")
        return 1
    try:
        with camera_for_serial(os.environ["CAMERA_SERIAL"]) as cam:
            print("connected:", cam.info().model)
            supported = cam.supports_native_movie()
            print(f"'movie' control node present: {supported}")
            if not supported:
                print("VERDICT: this body does NOT expose native movie record over USB. "
                      "Use record_clip() (live-view burst) or HDMI capture instead.")
                return 0
            print("Attempting a 3s native recording...")
            report = cam.record_native(3.0)
            print("RESULT:", report)
            print("VERDICT: native movie record WORKS on this body — check the SD card for the clip.")
            return 0
    except CameraError as exc:
        print(f"native record raised: {exc}")
        print("VERDICT: the 'movie' control exists but the body refused it — likely unsupported on "
              "the A7 II, or it needs the mode dial on Movie / a specific live-view state.")
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
