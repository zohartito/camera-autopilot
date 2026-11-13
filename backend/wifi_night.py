"""M4 NIGHT / TIMELAPSE, wired live over WiFi — fire a timelapse interval on the A7 II, or stack many
short frames into a clean low-noise night image, all over the camera's own WiFi AP.

This is the M4 sibling of `wifi_stack.py` (M2). Same transport, same idioms: begin_session ->
shoot over the Camera Remote API -> pull the full-res frames off the card via Contents Transfer ->
merge with the tested stacking suite. Two capture flows live here:

    intervalometer  -> fire N shots spaced `interval_s` apart, wall-clock (timelapse).
    night_stack     -> fire N identical short frames on M, transfer + long_exposure-merge them.

KEY CONSTRAINT (verified live, 2026-06-22): the A7 II Camera Remote API caps a single exposure at
30s — there is NO bulb over WiFi. So a "night long exposure" is not one long shot; it is a STACK of
many <=30s frames averaged together (digital long exposure / noise reduction — average N frames of a
static scene and random thermal/read noise drops ~sqrt(N), the same trick stacking.long_exposure
already implements). night_exposure() picks (shutter<=30s, iso, n_frames) for that stack: push the
shutter toward 30s and the ISO toward base first, only raising ISO when 30s still underexposes, and
asking for more frames when ISO is forced up (more gain = more noise = needs more averaging).

    backend/.venv-ml/bin/python backend/wifi_night.py timelapse --frames 30 --interval 5
    backend/.venv-ml/bin/python backend/wifi_night.py nightstack --frames 8 --shutter 15
"""
from __future__ import annotations

import argparse
import sys
import time
import math
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import wifi_a7ii
import wifi_autopilot

# `stacking` (and its cv2/numpy deps) is imported lazily inside night_stack so the intervalometer +
# the pure night_exposure math stay importable without the OpenCV stack present (mirrors wifi_stack).

# The A7 II WiFi API caps a single exposure at 30s (no bulb) — every shutter is clamped to this.
MAX_SHUTTER_S = 30.0


def _shutter_label(shutter_s: float) -> str:
    """Format a seconds value as a Sony shutter label: 30 -> '30"', 15 -> '15"', 0.5 -> '1/2'.
    Sony's long-shutter labels (>=0.4s) are whole/decimal seconds with a trailing double-quote; sub-
    second values are reciprocals. _num() in wifi_autopilot round-trips the '"' form back to seconds."""
    if shutter_s >= 1.0:
        # 30 -> "30", 1.5 -> "1.5" (drop a trailing .0 so 15.0 -> "15")
        whole = int(round(shutter_s)) if abs(shutter_s - round(shutter_s)) < 1e-6 else shutter_s
        return f'{whole}"'
    return f"1/{int(round(1.0 / shutter_s))}"


def _clamp_shutter(shutter_s: float) -> float:
    """Clamp any exposure to the 30s WiFi ceiling, printing a warning when it bites. The API has no
    bulb, so a request for a longer single exposure is physically impossible — cap it loudly."""
    if not math.isfinite(shutter_s) or shutter_s <= 0:
        raise ValueError("shutter must be finite and positive")
    if shutter_s > MAX_SHUTTER_S:
        print(f"  WARN  requested {shutter_s:g}s exceeds the A7 II WiFi 30s cap (no bulb) "
              f"-> clamped to {MAX_SHUTTER_S:g}s; stack more frames for a longer effective exposure")
        return MAX_SHUTTER_S
    return shutter_s


def intervalometer(cam, frames: int, interval_s: float, full_res_dir: str | None = None) -> dict:
    """Fire `frames` shots spaced `interval_s` apart over WiFi — a timelapse interval. The spacing is
    WALL-CLOCK: each capture takes time (a slow shutter + write blocks actTakePicture), so we sleep
    `interval_s - elapsed` (floored at 0) rather than a flat `interval_s`, keeping the cadence honest.
    No per-shot focus/exposure change — a timelapse holds settings; set them once on the camera (or
    via night_stack) first. When `full_res_dir` is set, capture-linked JPEGs are
    downloaded into a durable session; dimensions are reported without assuming
    full resolution. The argument name is retained for CLI compatibility.

    Returns the per-frame timing (requested vs actual interval, capture duration) + the postviews."""
    from stack_session import validate_frames
    import wifi_stack
    validate_frames(frames, sequence=True)
    if not math.isfinite(interval_s) or not 0 <= interval_s <= 3600:
        raise ValueError("interval must be finite and between 0 and 3600 seconds")
    if full_res_dir is not None:
        from stack_session import SessionRunner
        runner = SessionRunner(full_res_dir)
        session = runner.create("timelapse", frames, interval_s=interval_s,
                                metadata={"route": "smart_remote", "resolution": "capture-linked JPEG"})
        cam.begin_session()
        postviews = []
        seen = set()
        def capture_frame(index, spec, destination):
            urls = wifi_stack._capture_urls(cam, timeout=75.0)
            if urls[0] in seen:
                raise RuntimeError("duplicate capture URL; refusing unverified frame")
            seen.add(urls[0])
            postviews.append(urls)
            cam.download(urls[0], destination)
            return destination
        result = runner.run(session["id"], capture_frame)
        return {"frames": frames, "interval_s": interval_s, "session": result,
                "postviews": postviews, "full_res": [[frame["destination"]] for frame in result["frames"]
                                                       if frame["status"] == "accepted"],
                "per_frame": [{"frame": frame["index"], "capture_s": frame.get("capture_s")}
                              for frame in result["frames"] if frame["status"] == "accepted"],
                "transfer_resolution": "capture-linked JPEG; inspect session dimensions"}
    cam.begin_session()
    per_frame: list[dict] = []
    postviews: list = []
    full_res: list = []
    for i in range(frames):
        start = time.monotonic()
        postview = wifi_stack._capture_urls(cam, timeout=75.0)
        postviews.append(postview)
        elapsed = time.monotonic() - start
        # sleep only the REMAINDER of the interval; if the capture already overran it, don't sleep.
        # The last frame needs no trailing wait (no shot follows it).
        wait = max(0.0, interval_s - elapsed) if i < frames - 1 else 0.0
        per_frame.append({"frame": i, "capture_s": round(elapsed, 3),
                          "slept_s": round(wait, 3),
                          "interval_s": round(elapsed + wait, 3)})
        if wait:
            time.sleep(wait)
    return {"frames": frames, "interval_s": interval_s, "per_frame": per_frame,
            "postviews": postviews, "full_res": full_res or None}


def night_exposure(scene_ev100: float, max_shutter_s: float = 30.0, base_iso: int = 100) -> dict:
    """Pick a clean night stack (shutter_s<=max, iso, n_frames) for a dark metered EV100.

    Pure function — no camera, easy to unit-test. The A7 II WiFi cap means a single exposure can't
    exceed 30s, so to gather the light a dark scene needs we: (1) open the shutter all the way to
    `max_shutter_s`; (2) only THEN raise ISO above `base_iso`, by exactly the residual underexposure
    after the shutter is maxed; (3) ask for more frames the more ISO we were forced to add, since
    higher gain = more noise = more averaging needed to clean it.

    EV math: at ISO 100 a scene of brightness `scene_ev100` correctly exposes (aperture-agnostic, in
    EV/shutter space) at shutter t where log2(1/t) == scene_ev100 - K for the working f-stop; we work
    purely in stops relative to the reference, so a 1s exposure sits at EV 0 and each halving of the
    scene's light (one stop darker, lower EV) wants one stop more exposure. We express the needed
    exposure as `2^(-scene_ev100)` seconds-equivalent and split it between shutter and ISO.

    Returns: {shutter_s, shutter_label, iso, n_frames, scene_ev100, iso_stops_pushed, notes}."""
    if not math.isfinite(scene_ev100) or not -30 <= scene_ev100 <= 30:
        raise ValueError("scene EV must be finite and between -30 and 30")
    if not math.isfinite(max_shutter_s) or max_shutter_s <= 0 or not 50 <= base_iso <= 51200:
        raise ValueError("invalid shutter ceiling or base ISO")
    max_shutter_s = min(float(max_shutter_s), MAX_SHUTTER_S)  # never promise more than the API allows
    # Needed exposure time (seconds-equivalent at base ISO) to lift `scene_ev100` to a 0-EV target.
    # Darker scene -> lower (more negative) EV -> larger needed time. A scene at EV 0 wants 1s.
    needed_s = 2.0 ** (-scene_ev100)

    if needed_s <= max_shutter_s:
        # The shutter alone can carry it (within the 30s cap) — keep ISO at base, no gain noise.
        shutter_s = max(needed_s, 1.0 / 4000.0)       # don't go below the body's fastest shutter
        iso = int(base_iso)
        iso_stops = 0.0
    else:
        # 30s still underexposes -> peg the shutter at the cap and make up the residual stops with ISO.
        from math import log2
        shutter_s = max_shutter_s
        iso_stops = log2(needed_s / max_shutter_s)    # how many stops the 30s shutter still misses
        iso = int(round(base_iso * (2.0 ** iso_stops)))

    iso = _snap_iso(iso, base_iso)
    iso_stops_actual = _iso_stops(iso, base_iso)
    # Frame count: a clean base-ISO stack only needs a few frames; every stop of ISO pushed roughly
    # doubles read/thermal noise, so add frames with the gain. Floor 4, +~2 frames per ISO stop,
    # capped at 16 (diminishing sqrt(N) returns + battery/time on the A7 II).
    n_frames = int(min(16, max(4, round(4 + 2 * iso_stops_actual))))
    return {
        "scene_ev100": round(float(scene_ev100), 2),
        "shutter_s": round(shutter_s, 4),
        "shutter_label": _shutter_label(shutter_s),
        "iso": iso,
        "iso_stops_pushed": round(iso_stops_actual, 2),
        "n_frames": n_frames,
        "notes": ("shutter maxed at cap; ISO raised for the residual" if iso_stops_actual > 0
                  else "shutter alone carries it at base ISO"),
    }


def _snap_iso(iso: int, base_iso: int) -> int:
    """Snap a desired ISO onto the A7 II's native ISO ladder (nearest value), never below base_iso."""
    import camera_caps
    ladder = [v for v in camera_caps._A7II_ISO if v >= base_iso] or camera_caps._A7II_ISO
    return min(ladder, key=lambda v: abs(v - iso))


def _iso_stops(iso: int, base_iso: int) -> float:
    """Stops of gain `iso` sits above `base_iso` (0 at base, 1 at 2x, ...)."""
    from math import log2
    return log2(iso / base_iso) if iso > base_iso else 0.0


def night_stack(cam, frames: int = 8, shutter: str = '15"', iso: str = "3200",
                out_path: str = "/tmp/wifi_night.png") -> dict:
    """Capture an identified JPEG night burst, average it and verify the saved output.

    Shutter is capped at 30s. ISO/shutter are restored with readback in finally.
    Returned capture-linked JPEG dimensions may be below full resolution.
    """
    import cv2
    import stacking
    import wifi_stack
    from stack_session import validate_frames

    validate_frames(frames)
    requested_s = wifi_autopilot._num(shutter)
    if requested_s is None or not math.isfinite(requested_s) or requested_s <= 0:
        raise ValueError("night shutter must be finite and positive")
    shutter_to_set = _shutter_label(_clamp_shutter(requested_s))
    cam.begin_session()
    original_shutter = wifi_stack._read_setting(cam, "getShutterSpeed")
    original_iso = wifi_stack._read_setting(cam, "getIsoSpeedRate")
    try:
        set_shutter = wifi_autopilot._snap_set(cam, shutter_to_set, "getAvailableShutterSpeed", cam.set_shutter)
        set_iso = wifi_autopilot._snap_set(cam, iso, "getAvailableIsoSpeedRate", cam.set_iso)
        if set_shutter is None or set_iso is None:
            raise RuntimeError("night exposure settings are unavailable")
        time.sleep(0.3)
        shot = wifi_stack.shoot_burst(cam, frames=frames)
        paths, receipts = wifi_stack.transfer_capture_jpegs(cam, shot["postviews"], str(Path(out_path).parent / "frames"))
        shot["frame_receipts"] = receipts
        merged = stacking.long_exposure(paths, deghost=True)
        Path(out_path).parent.mkdir(parents=True, exist_ok=True)
        if not cv2.imwrite(out_path, merged):
            return {"merged": False, "reason": f"cv2.imwrite failed for {out_path}",
                    "transferred": paths, "shot": shot}
        return {"merged": True, "out_path": out_path, "frames_merged": len(paths),
                "shutter_set": set_shutter, "iso_set": set_iso,
                "transferred": paths, "shot": shot, "transfer_resolution": "capture-linked JPEG"}
    finally:
        try:
            cam.set_shutter(original_shutter)
        finally:
            cam.set_iso(original_iso)
        if (wifi_stack._read_setting(cam, "getShutterSpeed") != original_shutter
                or wifi_stack._read_setting(cam, "getIsoSpeedRate") != original_iso):
            raise RuntimeError("night exposure restoration could not be verified")


def _parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="M4 night / timelapse on the A7 II over WiFi")
    sub = parser.add_subparsers(dest="mode", required=True)

    tl = sub.add_parser("timelapse", help="fire N shots spaced --interval seconds apart")
    tl.add_argument("--frames", type=int, default=30)
    tl.add_argument("--interval", type=float, default=5.0, help="wall-clock seconds between shots")
    tl.add_argument("--full-res-dir", default=None, help="pull each frame's full-res here (optional)")

    ns = sub.add_parser("nightstack", help="stack N short frames into a clean night image")
    ns.add_argument("--frames", type=int, default=8)
    ns.add_argument("--shutter", default="15", help="seconds or Sony label; clamped to <=30s")
    ns.add_argument("--iso", default="3200")
    ns.add_argument("--out", default="/tmp/wifi_night.png")
    return parser.parse_args(argv)


if __name__ == "__main__":
    args = _parse_args(sys.argv[1:])
    camera = wifi_a7ii.connect()
    if not camera:
        print("No camera. Launch Smart Remote Control on the A7 II + join its WiFi AP first.")
        raise SystemExit(1)
    import json
    if args.mode == "timelapse":
        result = intervalometer(camera, args.frames, args.interval, full_res_dir=args.full_res_dir)
    else:
        result = night_stack(camera, frames=args.frames, shutter=args.shutter, iso=args.iso,
                             out_path=args.out)
    print(json.dumps(result, indent=2, default=str))
