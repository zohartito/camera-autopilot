#!/usr/bin/env python3
"""Clear the local recording-stop quarantine after physically verifying the camera is idle."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from sonyremote import clear_recording_quarantine


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, allow_abbrev=False)
    parser.add_argument(
        "--verified-device-idle",
        action="store_true",
        help="affirm that you physically confirmed the camera is not recording",
    )
    args = parser.parse_args(argv)
    if not args.verified_device_idle:
        parser.error(
            "first physically verify that recording has stopped on the camera, then pass --verified-device-idle"
        )
    clear_recording_quarantine(verified_device_idle=True)
    print("Recording quarantine cleared after operator-verified device-idle confirmation.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
