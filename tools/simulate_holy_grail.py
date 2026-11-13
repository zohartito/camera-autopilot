"""Simulated Holy Grail runs: the real exposure cascade against a synthetic scene.

No camera, preview or model is used. Each frame is metered exactly as the live
timelapse path meters it (the preceding frame's mean linear luminance), the
cascade picks settings from a real Sony 1/3-stop ladder under the same 1 s USB
transport cap, and the simulated frame's luminance follows from the difference
between the chosen exposure and what the scene needs. A simulated run proves the
control law, not a sunset: it cannot show tone-curve, white-balance or noise
behaviour of a real body.

    .venv/bin/python tools/simulate_holy_grail.py --scene sunset --frames 300 --darken-ev 10
"""
from __future__ import annotations

import argparse
import json
import math
import random
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
for entry in (ROOT, ROOT / "backend"):
    if str(entry) not in sys.path:
        sys.path.insert(0, str(entry))

import camera_caps  # noqa: E402
from stack_planner import metered_timelapse_step, timelapse_config  # noqa: E402

TRANSPORT_MAX_SHUTTER_S = 1.0  # sonyremote.camera.SONY_ACTION_MAX_ENABLED_SHUTTER_S
JPEG_SATURATION = 0.9  # mean linear luminance of a nearly white frame
JPEG_FLOOR = 1e-6  # stack_planner.jpeg_luminance floor


def _third_stop_shutter_labels():
    whole = ["1/8000", "1/6400", "1/5000", "1/4000", "1/3200", "1/2500", "1/2000", "1/1600",
             "1/1250", "1/1000", "1/800", "1/640", "1/500", "1/400", "1/320", "1/250", "1/200",
             "1/160", "1/125", "1/100", "1/80", "1/60", "1/50", "1/40", "1/30", "1/25", "1/20",
             "1/15", "1/13", "1/10", "1/8", "1/6", "1/5", "1/4", "1/3", "0.4", "0.5", "0.6",
             "0.8", "1", "1.3", "1.6", "2", "2.5", "3.2", "4", "5", "6", "8", "10", "13", "15",
             "20", "25", "30"]
    return whole


ISO_LADDER = ["100", "125", "160", "200", "250", "320", "400", "500", "640", "800", "1000",
              "1250", "1600", "2000", "2500", "3200", "4000", "5000", "6400", "8000", "10000",
              "12800", "16000", "20000", "25600"]


def sony_caps(aperture="8"):
    """A7 III-style third-stop ladders, bounded by the live USB transport cap."""
    caps = camera_caps.from_live_choices("Sony ILCE-7M3 (Control)", ISO_LADDER, [aperture],
                                         _third_stop_shutter_labels())
    bounded, _ = camera_caps.constrain_capture_duration(caps, max_shutter_s=TRANSPORT_MAX_SHUTTER_S)
    return bounded


def scene_ev(kind, index, frames, darken_ev, rng, flicker_ev):
    """Extra exposure (EV) the scene needs relative to frame 0's exposure."""
    t = index / max(1, frames - 1)
    if kind == "sunset":
        # Logistic dusk: slow at first, fastest around civil twilight, then flat.
        base = darken_ev / (1 + math.exp(-10 * (t - 0.5)))
        base -= darken_ev / (1 + math.exp(5))
    elif kind == "step":
        base = darken_ev if index >= frames // 3 else 0.0
    elif kind == "flat":
        base = 0.0
    else:
        raise ValueError("scene must be sunset, step or flat")
    return base + (rng.gauss(0.0, flicker_ev) if flicker_ev else 0.0)


def simulate(*, scene="sunset", frames=300, darken_ev=10.0, mode="holy_grail", options=None,
             priority_switch=None, flicker_ev=0.0, seed=7, base_shutter="1/500", base_iso="100",
             aperture="8"):
    caps = sony_caps(aperture)
    rng = random.Random(seed)
    config = timelapse_config(mode, options or {})
    base_shutter_s = camera_caps._shutter_seconds(base_shutter)
    rows = []
    ramp = {"requested_ev": 0.0}
    luminance = None
    for index in range(frames):
        if priority_switch and index == priority_switch[0]:
            config = timelapse_config(mode, {**(options or {}), "priority": priority_switch[1]})
        meter_luminance = config["target_luminance"] if index == 0 else luminance
        step = metered_timelapse_step(config, current_ev=0 if index == 0 else ramp["requested_ev"],
                                      luminance=meter_luminance, base_shutter_s=base_shutter_s,
                                      base_iso=float(base_iso), caps=caps, base_aperture=aperture,
                                      measured_ev=None if index == 0 else ramp.get("actual_ev"),
                                      scene_estimate=ramp.get("scene_ev_estimate"))
        ramp.update(step)
        needed = scene_ev(scene, index, frames, darken_ev, rng, flicker_ev)
        linear = config["target_luminance"] * 2 ** (step["actual_ev"] - needed)
        luminance = min(JPEG_SATURATION, max(JPEG_FLOOR, linear))
        rows.append({"index": index, "scene_ev": needed, "requested_ev": step["requested_ev"],
                     "actual_ev": step["actual_ev"], "shutter": step["shutter"], "iso": step["iso"],
                     "luminance": luminance, "range_limited": step["range_limited"],
                     "smoothing_frames": step["smoothing_frames"]})
    return {"scene": scene, "frames": frames, "darken_ev": darken_ev, "mode": mode,
            "config": config, "rows": rows, "metrics": metrics(rows, config["target_luminance"])}


def deflickered_log_luminance(rows, window=7, max_gain_stops=1.0):
    """The render path's deflicker, applied to simulated frames.

    deflicker works on the encoded (gamma) mean and multiplies encoded pixels, so the
    series is encoded with a 2.2 gamma and the corrected result converted back to EV.
    """
    import deflicker
    encoded = [255 * row["luminance"] ** (1 / 2.2) for row in rows]
    gains = deflicker.deflicker_gains(encoded, window=window, max_gain_stops=max_gain_stops)
    return [2.2 * math.log2(value * gain / 255) for value, gain in zip(encoded, gains)]


def metrics(rows, target):
    raw = [math.log2(row["luminance"]) for row in rows]
    rendered = deflickered_log_luminance(rows)
    steps = [row["requested_ev"] - prev["requested_ev"] for prev, row in zip(rows, rows[1:])]
    signs = [math.copysign(1, s) for s in steps if abs(s) > 1e-9]
    reversals = sum(a != b for a, b in zip(signs, signs[1:]))
    def jumps(series):
        deltas = sorted(abs(b - a) for a, b in zip(series, series[1:]))
        return {"max": deltas[-1] if deltas else 0.0,
                "p95": deltas[int(0.95 * (len(deltas) - 1))] if deltas else 0.0}
    errors = sorted(abs(value - math.log2(target)) for value in raw)
    limited = [row["index"] for row in rows if row["range_limited"]]
    return {"frame_to_frame_ev_raw": jumps(raw), "frame_to_frame_ev_rendered": jumps(rendered),
            "requested_step_reversals": reversals,
            "exposure_error_ev": {"mean": sum(errors) / len(errors), "p95": errors[int(0.95 * (len(errors) - 1))],
                                  "max": errors[-1]},
            "range_limited_frames": len(limited), "first_range_limited": limited[0] if limited else None,
            "actual_ev_span": max(r["actual_ev"] for r in rows) - min(r["actual_ev"] for r in rows)}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--scene", default="sunset", choices=["sunset", "step", "flat"])
    parser.add_argument("--frames", type=int, default=300)
    parser.add_argument("--darken-ev", type=float, default=10.0)
    parser.add_argument("--mode", default="holy_grail", choices=["holy_grail", "auto_custom"])
    parser.add_argument("--option", action="append", default=[], metavar="KEY=VALUE",
                        help="timelapse option, e.g. max_total_ev=12 or priority=iso_then_shutter")
    parser.add_argument("--flicker-ev", type=float, default=0.0)
    parser.add_argument("--rows", action="store_true", help="include every frame in the report")
    args = parser.parse_args(argv)
    options = {}
    for item in args.option:
        key, _, value = item.partition("=")
        try:
            options[key] = json.loads(value)
        except json.JSONDecodeError:
            options[key] = value
    result = simulate(scene=args.scene, frames=args.frames, darken_ev=args.darken_ev, mode=args.mode,
                      options=options, flicker_ev=args.flicker_ev)
    if not args.rows:
        result.pop("rows")
    json.dump(result, sys.stdout, indent=2, default=str)
    print()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
