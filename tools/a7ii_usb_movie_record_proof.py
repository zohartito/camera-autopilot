#!/usr/bin/env python3
"""Prove the A7 II USB movie-record action with a saved artifact.

This is intentionally separate from the registry row named "Movie" because the
gphoto/libgphoto `movie` node behaves like pressing the physical MOVIE button:
it starts or stops recording. It does not turn the mode dial or prove the
camera's persistent MOVIE Button policy menu row.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from sonyremote import CameraError, camera_for_serial, detect


def _stamp() -> str:
    return time.strftime("%Y%m%d-%H%M%S")


def _base_payload(args: argparse.Namespace) -> dict[str, Any]:
    return {
        "generated_at": _stamp(),
        "route": "usb_gphoto",
        "setting_id": args.setting,
        "node": "movie",
        "operation": "native_movie_record_start_stop",
        "risk": "records_short_movie_to_sd_card",
        "dry_run": not args.execute,
        "allow_camera_record": bool(args.allow_camera_record),
        "seconds_requested": float(args.seconds),
        "notes": [
            "This proves the USB/gphoto movie record operation only.",
            "It does not prove software control of the physical Movie mode dial.",
            "It does not prove the persistent MOVIE Button policy menu setting.",
        ],
    }


def _artifact_path(out_dir: Path) -> Path:
    run_dir = out_dir / f"{_stamp()}-usb-movie-record"
    run_dir.mkdir(parents=True, exist_ok=True)
    return run_dir / "usb-movie-record-proof.json"


def write_artifact(payload: dict[str, Any], out_dir: Path) -> Path:
    path = _artifact_path(out_dir)
    payload["artifact"] = str(path)
    path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return path


def live_movie_record_payload(args: argparse.Namespace) -> dict[str, Any]:
    payload = _base_payload(args)
    found = detect()
    payload["detect"] = [{"model": model, "port": port} for model, port in found]
    if not found:
        payload.update(
            {
                "ok": False,
                "verdict": "no_camera_detected",
                "error": "No USB camera detected. Set USB Connection to PC Remote and reconnect.",
            }
        )
        return payload

    try:
        # The movie toggle only ever presses the body proven by this USB serial.
        with camera_for_serial(args.serial) as cam:
            info = cam.info()
            payload["camera"] = {"model": info.model, "port": info.port}
            supported = cam.supports_native_movie()
            payload["supports_native_movie"] = supported
            if not supported:
                payload.update(
                    {
                        "ok": False,
                        "verdict": "movie_node_absent",
                        "error": "The live camera does not expose the gphoto movie node.",
                    }
                )
                return payload
            result = cam.record_native(float(args.seconds))
            payload.update(
                {
                    "ok": True,
                    "verdict": "movie_record_action_proven",
                    "result": result,
                }
            )
            return payload
    except (CameraError, ValueError) as exc:
        payload.update(
            {
                "ok": False,
                "verdict": "movie_record_action_failed",
                "error": str(exc),
            }
        )
        return payload


def build_payload(args: argparse.Namespace) -> dict[str, Any]:
    payload = _base_payload(args)
    if not args.execute:
        payload.update(
            {
                "ok": False,
                "verdict": "dry_run_guarded",
                "error": "Pass --execute --allow-camera-record to record a short movie proof.",
            }
        )
        return payload
    if not args.allow_camera_record:
        payload.update(
            {
                "ok": False,
                "verdict": "guarded_missing_allow_camera_record",
                "error": "Recording is guarded; rerun with --allow-camera-record.",
            }
        )
        return payload
    if not getattr(args, "serial", None):
        payload.update(
            {
                "ok": False,
                "verdict": "guarded_missing_serial",
                "error": "Pass --serial <USB serial>: ports re-enumerate, the serial is the identity.",
            }
        )
        return payload
    return live_movie_record_payload(args)


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--seconds", type=float, default=3.0, help="short recording duration")
    parser.add_argument(
        "--setting",
        default="camera.movie_mode",
        choices=["camera.movie_mode"],
        help="registry row this action proof is tied to",
    )
    parser.add_argument("--execute", action="store_true", help="talk to the camera")
    parser.add_argument("--serial", default=os.environ.get("CAMERA_SERIAL") or None,
                        help="the body's USB serial (default: CAMERA_SERIAL); required with --execute")
    parser.add_argument(
        "--allow-camera-record",
        action="store_true",
        help="acknowledge that this records a short movie clip to the SD card",
    )
    parser.add_argument(
        "--out-dir",
        type=Path,
        default=REPO_ROOT / "artifacts" / "falsification" / "r2_usb_movie_record",
    )
    parser.add_argument("--json", action="store_true", help="print the payload as JSON")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    payload = build_payload(args)
    path = write_artifact(payload, Path(args.out_dir))
    if args.json:
        print(json.dumps(payload, indent=2, sort_keys=True))
    else:
        print(f"artifact: {path}")
        print(f"verdict: {payload['verdict']}")
        if payload.get("error"):
            print(f"error: {payload['error']}")
    if payload.get("ok") is True:
        return 0
    return 0 if payload["verdict"] == "dry_run_guarded" else 2


if __name__ == "__main__":
    raise SystemExit(main())
