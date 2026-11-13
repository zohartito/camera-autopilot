#!/usr/bin/env python3
"""Plan a Night Assist (A23) session from a single meter JPEG — print only by default.

The plan is chosen from the body's MEASURED noise table (not a fixed recipe): ISO under the
measured usable ceiling, shutter under the transport/motion cap, widest aperture, and a frame
count N so averaging brings sigma/sqrt(N) back near base-ISO noise. This tool never runs a
camera; with --submit it hands the plan to a running server's durable session runner (POST
/sessions), which is the only thing that operates the shutter.

    .venv/bin/python tools/plan_night_session.py --jpeg meter.jpg --model "Sony Alpha-A7 III (PC Control)"
    ... --submit --port usb:001,001 --token-file ~/.camera_token --base-url http://127.0.0.1:8001
"""
import argparse
import json
import sys
from dataclasses import asdict
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))

import night_assist  # noqa: E402


def _default_choices(jpeg: str):
    """The body's native Sony ladders as planning choices; apertures are clipped to the mounted
    lens's widest opening (from EXIF) so 'widest available' is realistic, not f/1.0."""
    import camera_caps
    iso_choices = [str(v) for v in camera_caps._A7II_ISO]
    shutter_choices = list(camera_caps._A7II_SHUTTER_LABELS)
    _, lens_specification, max_aperture_value, *_ = night_assist._frame_exposure_exif(jpeg)
    widest = None
    try:
        if lens_specification and len(lens_specification) == 4:
            widest = max(float(v) for v in lens_specification[2:])
    except (TypeError, ValueError):
        widest = None
    aperture_choices = [f"f/{v:g}" for v in camera_caps._A7II_APERTURE
                        if widest is None or v >= widest - 1e-6]
    if not aperture_choices:
        aperture_choices = [f"f/{v:g}" for v in camera_caps._A7II_APERTURE]
    return iso_choices, aperture_choices, shutter_choices


def _submit(body: dict, base_url: str, token: str) -> dict:
    import urllib.request
    request = urllib.request.Request(
        base_url.rstrip("/") + "/sessions",
        data=json.dumps(body).encode(),
        headers={"Content-Type": "application/json", "X-Camera-Autopilot-Token": token},
        method="POST")
    with urllib.request.urlopen(request, timeout=15) as response:  # noqa: S310 - operator-supplied URL
        return json.loads(response.read().decode())


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--jpeg", required=True, help="meter frame with EXIF exposure + lens")
    parser.add_argument("--model", required=True, help="exact camera model string")
    parser.add_argument("--max-shutter-s", type=float, default=1.0)
    parser.add_argument("--max-frames", type=int, default=night_assist.DEFAULT_MAX_FRAMES)
    parser.add_argument("--motion-floor-s", type=float, default=None)
    parser.add_argument("--target-snr-gain-db", type=float, default=None)
    parser.add_argument("--aperture", default=None, help="force an aperture label, e.g. f/2.8")
    parser.add_argument("--handheld", action="store_true", help="no tripod: refuse frames slower than the handheld floor")
    parser.add_argument("--submit", action="store_true", help="submit the plan to a running server")
    parser.add_argument("--port", help="USB port for --submit, e.g. usb:001,001")
    parser.add_argument("--token-file", type=Path, help="file holding the API token for --submit")
    parser.add_argument("--base-url", default="http://127.0.0.1:8001")
    args = parser.parse_args()

    iso_choices, aperture_choices, shutter_choices = _default_choices(args.jpeg)
    try:
        plan = night_assist.plan_from_meter_frame(
            args.jpeg, args.model, iso_choices, aperture_choices, shutter_choices,
            max_shutter_s=args.max_shutter_s, max_frames=args.max_frames,
            subject_motion_floor_s=args.motion_floor_s, target_snr_gain_db=args.target_snr_gain_db,
            aperture_label=args.aperture, tripod=not args.handheld)
    except (ValueError, OSError) as error:
        print(json.dumps({"status": "error", "message": str(error)}), file=sys.stderr)
        return 1

    output = {"plan": asdict(plan)}
    if args.submit:
        if plan.status != "ok":
            output["submit"] = {"status": "skipped", "reason": f"plan status {plan.status}"}
            print(json.dumps(output, indent=2))
            return 1
        if not args.port or not args.token_file:
            print(json.dumps({"status": "error",
                              "message": "--submit requires --port and --token-file"}), file=sys.stderr)
            return 1
        body = night_assist.to_session_request(plan, args.model, args.port)
        try:
            token = args.token_file.read_text().strip()
            output["submit"] = {"status": "created", "request": body,
                                "response": _submit(body, args.base_url, token)}
        except OSError as error:
            print(json.dumps({"status": "error", "message": f"submit failed: {error}"}),
                  file=sys.stderr)
            return 1

    print(json.dumps(output, indent=2, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
