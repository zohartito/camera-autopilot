#!/usr/bin/env python3
"""Create or execute an audited A7 II live route-session manifest."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
BACKEND = REPO_ROOT / "backend"
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))

import settings_closure
import settings_session_runs


def _print_text(manifest: dict) -> None:
    summary = manifest["summary"]
    print(
        "A7 II live session manifest: "
        f"{summary['commands']} commands, "
        f"{summary['planned']} planned, "
        f"{summary['executed']} executed, "
        f"{summary['blocked']} blocked, "
        f"{summary['failed']} failed"
    )
    print(f"manifest: {manifest['manifest_path']}")
    print(f"readiness: {manifest['readiness_path']}")
    print(f"summary:  {manifest['summary_path']}")
    if manifest.get("recommended_sequence"):
        print("sequence: recommended proof sequence")
    aftermath = manifest.get("aftermath") or {}
    if aftermath:
        print(f"outcome: {aftermath['outcome']}")
        print(f"next: {aftermath['note']}")
        for command in aftermath.get("next_commands") or []:
            print(f"next command: {command}")
    for entry in manifest["commands"]:
        print(f"- {entry['index']:02d} [{entry['phase']}] {entry['status']} {entry['title']}")
        print(
            f"  risk: {entry['risk']}  "
            f"hardware: {entry['requires_hardware']}  "
            f"observation: {entry.get('requires_observation', False)}"
        )
        if entry.get("sequence_kind"):
            print(f"  sequence: {entry['sequence_kind']}")
        print(f"  command: {entry['command']}")


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--route", required=True, choices=list(settings_closure.ROUTE_ORDER))
    parser.add_argument("--setting", help="limit route session to one setting_id")
    parser.add_argument(
        "--phase",
        action="append",
        choices=["all", "evidence", "review", "refresh"],
        help="phase to include; repeatable; default all",
    )
    parser.add_argument(
        "--recommended-sequence",
        action="store_true",
        help="plan or execute the route's recommended proof sequence instead of explicit phases",
    )
    parser.add_argument("--execute", action="store_true", help="run commands instead of only planning")
    parser.add_argument(
        "--allow-hardware",
        action="store_true",
        help="allow evidence commands that may touch the camera; only meaningful with --execute",
    )
    parser.add_argument(
        "--out-dir",
        default="artifacts/falsification/live_sessions",
        help="directory for session manifest folders",
    )
    parser.add_argument(
        "--package-path",
        help="local Sony Camera Remote Command package path for camera_remote_command evidence sessions",
    )
    parser.add_argument(
        "--observed-state",
        help="visible camera/menu state for one guided_physical --setting evidence session",
    )
    parser.add_argument(
        "--observed-value",
        help="visible phone-app value for one smart_remote_phone --setting evidence session",
    )
    parser.add_argument(
        "--phone-state",
        help="visible phone/app context for one smart_remote_phone --setting evidence session",
    )
    parser.add_argument(
        "--observed-behavior",
        help="visible near/far behavior for one raw_ptp --setting visual proof session",
    )
    parser.add_argument(
        "--target-setup",
        help="near/far target setup for one raw_ptp --setting visual proof session",
    )
    parser.add_argument(
        "--smart-remote-method",
        help="Smart Remote setter method for one smart_remote_wifi --setting behavior proof",
    )
    parser.add_argument(
        "--smart-remote-requested-value",
        help="value sent to the Smart Remote setter for one smart_remote_wifi behavior proof",
    )
    parser.add_argument(
        "--smart-remote-observed-before",
        help="camera/app/getter value before one smart_remote_wifi behavior proof",
    )
    parser.add_argument(
        "--smart-remote-observed-after",
        help="camera/app/getter value after one smart_remote_wifi behavior proof",
    )
    parser.add_argument(
        "--smart-remote-observed-behavior",
        help="optional visible behavior description for one smart_remote_wifi behavior proof",
    )
    parser.add_argument("--camera-model", help="exact gphoto2 model of the selected body (USB setting writes)")
    parser.add_argument("--camera-port", help="exact USB port of the selected body, e.g. usb:000,004 (re-detect first)")
    parser.add_argument(
        "--camera-serial",
        help="the selected body's USB serial (tools/inspect_usb_camera.py); USB setting writes are blocked without it",
    )
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)

    manifest = settings_session_runs.run_session(
        route=args.route,
        setting_id=args.setting,
        phases=args.phase,
        recommended_sequence=args.recommended_sequence,
        execute=args.execute,
        allow_hardware=args.allow_hardware,
        out_dir=Path(args.out_dir),
        package_path=Path(args.package_path) if args.package_path else None,
        observed_state=args.observed_state,
        observed_value=args.observed_value,
        phone_state=args.phone_state,
        observed_behavior=args.observed_behavior,
        target_setup=args.target_setup,
        smart_remote_method=args.smart_remote_method,
        smart_remote_requested_value=args.smart_remote_requested_value,
        smart_remote_observed_before=args.smart_remote_observed_before,
        smart_remote_observed_after=args.smart_remote_observed_after,
        smart_remote_observed_behavior=args.smart_remote_observed_behavior,
        camera_model=args.camera_model,
        camera_port=args.camera_port,
        camera_serial=args.camera_serial,
    )
    if args.json:
        print(json.dumps(manifest, indent=2, sort_keys=True))
    else:
        _print_text(manifest)
    return 1 if manifest["summary"]["failed"] else 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
