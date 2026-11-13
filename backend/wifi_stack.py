"""Bounded JPEG computational photography over Sony Smart Remote Wi-Fi.

Capture receipts identify every input; an empty/ambiguous shutter response stops
capture, and newest card files are never substituted. Returned postview JPEG
sizes vary by camera and are recorded rather than called full resolution.
HDR restores and verifies the original shutter even on failure. Real stepped
focus capture stays unavailable until a supported actuator is verified.
"""
from __future__ import annotations

import argparse
import sys
import time
import math
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import wifi_a7ii
import wifi_autopilot
from stack_session import validate_frames, frame_receipt, MAX_SESSION_SOURCE_BYTES

# `stacking` (and its cv2/numpy deps) is imported lazily inside live_stack so the bracket/burst
# capture logic + the EV ladder stay importable without the OpenCV stack present.


def _shutter_at_ev_offset(cam, base_shutter_s: float, ev_offset: float) -> str | None:
    """Snap (base_shutter * 2^ev_offset) onto the camera's nearest available shutter label and set
    it. +EV = brighter = SLOWER shutter (more time). Returns the label set, or None."""
    target_s = base_shutter_s * (2.0 ** ev_offset)
    # pass the NUMERIC seconds straight through (_snap_set's _num handles floats). Hand-building a
    # "1/x" string rounded a sub-second-but-slow target like 0.8s to "1/1", corrupting the bracket.
    label = wifi_autopilot._snap_set(cam, target_s,
                                     "getAvailableShutterSpeed", cam.set_shutter)
    return label


def _ev_offsets(frames: int, ev_step: float) -> list[float]:
    """Symmetric bracket offsets centered on 0, e.g. frames=3,step=2 -> [-2, 0, +2]."""
    half = (frames - 1) / 2.0
    return [round((i - half) * ev_step, 2) for i in range(frames)]


def shoot_bracket(cam, frames: int = 3, ev_step: float = 2.0, frame_prefix: str = "/tmp/wifi_lv",
                  focus: bool = True) -> dict:
    """Capture a verified shutter bracket while preserving the original shutter.

    Manual exposure and fixed ISO/aperture must already be selected. Focus is not
    changed: the compatibility ``focus`` argument does not establish a focus lock.
    Empty capture receipts halt the bracket without re-firing the shutter.
    """
    validate_frames(frames)
    if not math.isfinite(ev_step) or not 0 <= ev_step <= 3 or (frames - 1) * ev_step / 2 > 6:
        raise ValueError("bracket must be within +/-6 EV, step 0..3")
    cam.begin_session()
    fixed_exposure = _verify_fixed_exposure(cam)
    base_label = _read_setting(cam, "getShutterSpeed")
    base_shutter_s = wifi_autopilot._num(base_label)
    if not base_shutter_s or base_shutter_s <= 0:
        raise ValueError("cannot bracket without a verified base shutter")
    offsets = _ev_offsets(frames, ev_step)
    set_labels, postviews = [], []
    try:
        for offset in offsets:
            label = _shutter_at_ev_offset(cam, base_shutter_s, offset)
            if label is None:
                raise RuntimeError("requested bracket shutter is unavailable")
            time.sleep(0.3)
            if _read_setting(cam, "getShutterSpeed") != label:
                raise RuntimeError("bracket shutter write was not confirmed by readback")
            set_labels.append(label)
            shutter_s = wifi_autopilot._num(label) or base_shutter_s
            if _verify_fixed_exposure(cam) != fixed_exposure:
                raise RuntimeError("ISO/aperture changed during bracket")
            postviews.append(_capture_urls(cam, timeout=max(40.0, shutter_s * 2 + 15)))
            time.sleep(0.4)
    finally:
        cam.set_shutter(base_label)
        if _read_setting(cam, "getShutterSpeed") != base_label:
            raise RuntimeError("base shutter restoration could not be verified")
    return {"frames": frames, "ev_step": ev_step, "ev_ladder": offsets,
            "shutter_labels": set_labels, "base_shutter_s": base_shutter_s,
            "af_locked": None, "focus_point_pct": None,
            "focus_note": "Focus held at existing setting; no focus-lock claim.",
            "postviews": postviews, "shutter_restored": True}


def _read_setting(cam, method: str):
    result = cam.call(method)
    if result.get("error") or not result.get("result"):
        raise RuntimeError(f"cannot read {method}; refusing unverified camera changes")
    return result["result"][0]



def _verify_fixed_exposure(cam) -> dict:
    settings = {key: _read_setting(cam, method) for key, method in (
        ("mode", "getExposureMode"), ("iso", "getIsoSpeedRate"), ("aperture", "getFNumber"))}
    if settings["mode"] not in ("Manual", "M") or "auto" in str(settings["iso"]).lower():
        raise RuntimeError("stacking requires Manual exposure and fixed ISO")
    if any(not wifi_autopilot._num(settings[key]) for key in ("iso", "aperture")):
        raise RuntimeError("stacking exposure readback is invalid")
    return settings

def _capture_urls(cam, timeout: float = 40.0) -> list[str]:
    urls = cam.take_picture(timeout=timeout)
    if not isinstance(urls, list) or not urls or not all(isinstance(url, str) and url for url in urls):
        raise RuntimeError("capture returned no exact image identity; shutter result is ambiguous, do not retry")
    return urls


def transfer_capture_jpegs(cam, postviews: list, save_dir: str) -> tuple[list[str], list[dict]]:
    """Download only URLs returned by these captures; never substitute newest card files.

    These are capture-linked JPEGs. Their dimensions are recorded because Sony
    postviews may be smaller than the full-resolution originals.
    """
    validate_frames(len(postviews), sequence=True)
    urls = []
    for capture in postviews:
        if not isinstance(capture, list) or not capture or not isinstance(capture[0], str):
            raise RuntimeError("capture lacks exact JPEG identity")
        urls.append(capture[0])
    if len(set(urls)) != len(urls):
        raise RuntimeError("duplicate image URL; cannot prove distinct capture identities")
    directory = Path(save_dir) / uuid.uuid4().hex
    directory.mkdir(parents=True, exist_ok=False)
    paths, receipts = [], []
    total = 0
    for index, url in enumerate(urls):
        path = directory / f"frame_{index:04d}.jpg"
        cam.download(url, str(path))
        receipt = frame_receipt(path)
        total += receipt["bytes"]
        if total > MAX_SESSION_SOURCE_BYTES:
            raise ValueError("session exceeds source-byte budget")
        receipt["capture_url"] = url
        paths.append(str(path))
        receipts.append(receipt)
    return paths, receipts


def shoot_burst(cam, frames: int = 8, frame_prefix: str = "/tmp/wifi_lv", focus: bool = True) -> dict:
    """Bounded same-settings burst; preserves existing focus mode and verifies every receipt."""
    validate_frames(frames)
    cam.begin_session()
    fixed_exposure = _verify_fixed_exposure(cam)
    shutter_s = wifi_autopilot._num(_read_setting(cam, "getShutterSpeed"))
    if not shutter_s or shutter_s <= 0:
        raise ValueError("cannot size capture timeout without a valid shutter")
    postviews = []
    for _ in range(frames):
        if _verify_fixed_exposure(cam) != fixed_exposure:
            raise RuntimeError("ISO/aperture changed during burst")
        postviews.append(_capture_urls(cam, timeout=max(40.0, shutter_s * 2 + 15)))
        time.sleep(0.4)
    return {"frames": frames, "af_locked": None,
            "focus_note": "Focus held at existing setting; no focus-lock claim.", "postviews": postviews}


MODES = ("hdr", "long_exposure", "focus_stack", "crowd_remove")


def live_stack(cam, mode: str, frames: int, ev_step: float, out_path: str,
               save_dir: str = "/tmp/wifi_stack", prefer_raw: bool | None = None) -> dict:
    """Capture and merge bounded, exactly identified JPEG frames.

    RAW decoding and unverified focus bracketing are unavailable. A capture URL
    identifies its returned JPEG but does not guarantee full-resolution output.
    """
    import cv2
    import stacking  # lazy: cv2/numpy only needed at merge time
    merge_fns = {"hdr": stacking.hdr_merge, "long_exposure": stacking.long_exposure,
                 "focus_stack": stacking.focus_stack, "crowd_remove": stacking.crowd_remove}
    if mode not in merge_fns:
        raise ValueError(f"unknown mode {mode!r}; choose from {sorted(merge_fns)}")
    if prefer_raw:
        raise ValueError("RAW stacking is disabled; request bounded JPEG transfer instead")
    if mode == "focus_stack":
        raise NotImplementedError("Live focus stacking requires a verified focus actuator; same-focus bursts do not extend depth of field")
    validate_frames(frames)
    prefer_raw = False
    shot = (shoot_bracket(cam, frames=frames, ev_step=ev_step) if mode == "hdr"
            else shoot_burst(cam, frames=frames))
    paths, receipts = transfer_capture_jpegs(cam, shot.get("postviews", []), save_dir)
    shot["frame_receipts"] = receipts
    shot["transfer_resolution"] = "exact capture-linked JPEG; inspect receipt dimensions"
    if len(paths) < 2:
        return {"mode": mode, "merged": False, "reason": "need >=2 transferred frames",
                "transferred": paths, "shot": shot, "prefer_raw": prefer_raw}
    hdr_report = None
    if mode == "hdr":
        merged, hdr_report = stacking.hdr_merge_with_report(paths)
    else:
        merged = merge_fns[mode](paths)
    Path(out_path).parent.mkdir(parents=True, exist_ok=True)
    # cv2.imwrite returns False (no exception) on a failed encode/write — e.g. an
    # unwritable path or unknown extension. Don't report merged=True on a write that
    # never landed; surface the failure with the path that could not be written.
    if not cv2.imwrite(out_path, merged):
        return {"mode": mode, "merged": False, "reason": f"cv2.imwrite failed for {out_path}",
                "out_path": out_path, "transferred": paths, "shot": shot, "prefer_raw": prefer_raw}
    result = {"mode": mode, "merged": True, "out_path": out_path,
              "transferred": paths, "shot": shot, "prefer_raw": prefer_raw}
    if hdr_report is not None:
        result["hdr"] = hdr_report
    return result


def _parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Live WiFi stacking on the A7 II")
    parser.add_argument("mode", choices=sorted(MODES), help="stack mode")
    parser.add_argument("--frames", type=int, default=3)
    parser.add_argument("--ev-step", type=float, default=2.0, help="EV between bracket frames (hdr)")
    parser.add_argument("--out", default="/tmp/wifi_stack/merged.jpg")
    return parser.parse_args(argv)


if __name__ == "__main__":
    args = _parse_args(sys.argv[1:])
    camera = wifi_a7ii.connect()
    if not camera:
        print("No camera. Launch Smart Remote Control on the A7 II + join its WiFi AP first.")
        raise SystemExit(1)
    import json
    result = live_stack(camera, args.mode, args.frames, args.ev_step, args.out)
    print(json.dumps(result, indent=2, default=str))
