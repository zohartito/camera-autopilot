"""Verified-setting panorama capture and bounded preview stitching.

A sweep requires Manual exposure, fixed ISO/WB and manual focus before it starts.
Exact capture-linked JPEGs are merged; old card files cannot stand in for missing
captures. The stitcher downsizes inputs to 1600px; this is a preview-resolution
single-row implementation, not a full-resolution multi-row product claim.
"""
from __future__ import annotations

import argparse
import contextlib
import ctypes
import ctypes.util
import os
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

# Handle to libc's fflush, used to DRAIN the C stdio buffers into /dev/null before we restore the
# real fds during stitch (see _suppress_c_output). Resolved once, best-effort: None on any platform
# where it can't be loaded, in which case the redirect still runs without the extra drain.
try:
    _LIBC = ctypes.CDLL(ctypes.util.find_library("c") or "libc.so.6")
except OSError:
    _LIBC = None


def _flush_c_stdio() -> None:
    """fflush(NULL) — flush ALL C stdio output streams. No-op if libc couldn't be resolved."""
    if _LIBC is not None:
        try:
            _LIBC.fflush(None)
        except OSError:
            pass

import wifi_a7ii
import wifi_autopilot
from image_safety import MAX_DECODED_RGB_BYTES, image_size_from_bytes, source_bytes

# `cv2` (the OpenCV stack) is imported lazily inside stitch()/live_pano so the sweep-capture logic
# stays importable without OpenCV present — exactly like wifi_stack defers its `stacking` import.


def shoot_sweep(cam, frames: int = 6, settle_s: float = 2.5,
                frame_prefix: str = "/tmp/wifi_pano", focus: bool = True) -> dict:
    """Capture a bounded horizontal sweep after verifying fixed exposure, WB and MF.

    The camera must already be configured for locked capture. Each frame checks
    the lock readbacks again; no undocumented AF or lock capability is assumed.
    """
    from stack_session import validate_frames
    import wifi_stack
    import math
    validate_frames(frames)
    if not math.isfinite(settle_s) or not 0 <= settle_s <= 60:
        raise ValueError("settle_s must be finite and between 0 and 60")
    cam.begin_session()
    locks = verify_panorama_locks(cam)
    iso = wifi_autopilot._num(locks["iso"])
    f_number = wifi_autopilot._num(locks["aperture"])
    shutter_s = wifi_autopilot._num(locks["shutter"])
    af_locked = None
    point = None
    postviews: list = []
    for index in range(frames):
        if index > 0:
            # prompt the user to pan, then give them time to settle the camera before firing. Aim for
            # ~1/3 of the frame -> ~40% overlap, the overlap the stitcher is happiest with.
            print(f"▶ pan ~60% of the frame (~40% overlap) and hold… (frame {index + 1}/{frames})")
            time.sleep(settle_s)
        if verify_panorama_locks(cam) != locks:
            raise RuntimeError("panorama locks changed; refusing inconsistent sweep")
        postviews.append(wifi_stack._capture_urls(cam, timeout=max(40.0, (shutter_s or 0) * 2 + 15)))
        time.sleep(0.4)                         # let the write to card finish before the next shot
    return {
        "frames": frames,
        "locks_verified": locks,
        "settle_s": settle_s,
        "locked_iso": iso,
        "locked_f": f_number,
        "locked_shutter_s": shutter_s,
        "af_locked": af_locked,
        "focus_point_pct": point,
        "postviews": postviews,
    }


def verify_panorama_locks(cam) -> dict:
    """Require fixed camera settings; never describe AF-S or Auto ISO as locked.

    Set Manual exposure, fixed ISO/WB and manual focus on the camera before a
    panorama. This preserves settings and refuses automatic drift rather than
    inventing a portable focus-lock command across unsupported bodies.
    """
    from wifi_stack import _read_setting
    settings = {key: _read_setting(cam, method) for key, method in (
        ("mode", "getExposureMode"), ("iso", "getIsoSpeedRate"),
        ("aperture", "getFNumber"), ("shutter", "getShutterSpeed"),
        ("focus", "getFocusMode"), ("white_balance", "getWhiteBalance"))}
    if settings["mode"] not in ("Manual", "M"):
        raise RuntimeError("panorama requires Manual exposure mode")
    if settings["focus"] not in ("MF", "Manual"):
        raise RuntimeError("panorama requires manual focus to prevent refocusing between frames")
    if "auto" in str(settings["iso"]).lower() or "auto" in str(settings["white_balance"]).lower():
        raise RuntimeError("panorama requires fixed ISO and white balance")
    if any(not wifi_autopilot._num(settings[key]) for key in ("iso", "aperture", "shutter")):
        raise RuntimeError("panorama exposure readback is invalid")
    return settings


def estimate_overlap(previous: str, current: str) -> dict:
    """Measure pairwise overlap from robust ORB correspondences, or report unknown."""
    import cv2
    import numpy as np
    from stacking import _read_one
    left, right = (_downscale(_read_one(path), 1000) for path in (previous, current))
    orb = cv2.ORB_create(nfeatures=2500)
    keys_a, desc_a = orb.detectAndCompute(left, None)
    keys_b, desc_b = orb.detectAndCompute(right, None)
    unknown = {"measured": False, "overlap_fraction": None, "in_target_band": False}
    if desc_a is None or desc_b is None:
        return {**unknown, "reason": "Not enough texture to measure overlap."}
    pairs = cv2.BFMatcher(cv2.NORM_HAMMING).knnMatch(desc_b, desc_a, k=2)
    good = [pair[0] for pair in pairs if len(pair) == 2 and pair[0].distance < .7 * pair[1].distance]
    if len(good) < 12:
        return {**unknown, "reason": "Not enough matching features."}
    src = np.float32([keys_b[m.queryIdx].pt for m in good]).reshape(-1, 1, 2)
    dst = np.float32([keys_a[m.trainIdx].pt for m in good]).reshape(-1, 1, 2)
    transform, mask = cv2.findHomography(src, dst, cv2.RANSAC, 3.0)
    if transform is None or mask is None or int(mask.sum()) < 12 or not np.isfinite(transform).all():
        return {**unknown, "reason": "No reliable geometric overlap."}
    height_b, width_b = right.shape[:2]
    height_a, width_a = left.shape[:2]
    boundary_b = np.float32([[0, 0], [width_b, 0], [width_b, height_b], [0, height_b]])
    warped = cv2.perspectiveTransform(boundary_b.reshape(-1, 1, 2), transform).reshape(-1, 2)
    boundary_a = np.float32([[0, 0], [width_a, 0], [width_a, height_a], [0, height_a]])
    if not np.isfinite(warped).all() or not cv2.isContourConvex(warped):
        return {**unknown, "reason": "Unstable camera geometry."}
    area, _polygon = cv2.intersectConvexConvex(boundary_a, warped)
    overlap = max(0.0, min(1.0, float(area) / (width_a * height_a)))
    return {"measured": True, "overlap_fraction": round(overlap, 3),
            "in_target_band": .2 <= overlap <= .5, "inlier_matches": int(mask.sum())}


# cv2-decodable suffixes — the transfer can return RAW (.ARW) siblings cv2 can't read, so the
# stitch is fed only decodable frames (same filter wifi_stack.live_stack applies).
_CV2_OK = (".jpg", ".jpeg", ".png", ".tif", ".tiff")

# Stitcher works MUCH better on small frames. 24MP (6000x4000) frames destabilize the bundle
# adjuster (it floods stderr with LAPACK "DLASCL ... illegal value" warnings and degenerates);
# downscaling each frame to ~1600px wide first is necessary (though not sufficient on its own) for a
# stable solve. The output is correspondingly ~1600px-class, which is plenty for a preview pano.
_STITCH_MAX_W = 1600
# weaker-but-valid feature matches should still be accepted — the default 1.0 pano confidence is too
# strict for low-parallax handheld sweeps; 0.3-0.5 lets borderline overlaps through.
_PANO_CONF_THRESH = 0.3
# the single hint that actually guides capture: both extremes fail, aim for the middle.
_OVERLAP_HINT = "aim for ~40% overlap between frames — both too-wide and too-narrow pans fail."


@contextlib.contextmanager
def _suppress_c_output():
    """Silence C-level stdout AND stderr (fds 1 and 2) for the duration of the block, then restore
    them — fully draining the C stdio buffers first so nothing leaks on restore.

    cv2's bundle adjuster emits LAPACK "** On entry to DLASCL, parameter number 4/5 had an illegal
    value" lines straight from native code (here, macOS Accelerate/vecLib via GCD worker threads).
    Python-level redirects can't catch them, so we dup the real fds onto /dev/null. Two subtleties
    this handles that a naive redirect does NOT:
      * The flood lands on fd 1 (STDOUT), not fd 2 — so BOTH fds are redirected; covering only stderr
        would leave the terminal spammed.
      * The LAPACK printf goes through C stdio's BUFFERED `stdout` stream. Those bytes sit in libc's
        buffer and would flush to the *restored* (real) fd 1 the instant we put it back — leaking the
        flood after the block. So we fflush(NULL) while the fds still point at /dev/null, draining the
        buffer into the void, THEN restore.
    Best-effort: a fd that can't be duped (already closed) is left as-is rather than crashing."""
    saved = {}
    for fd in (1, 2):
        try:
            saved[fd] = os.dup(fd)
        except OSError:
            pass
    devnull_fd = os.open(os.devnull, os.O_WRONLY)
    try:
        sys.stdout.flush()
        sys.stderr.flush()
        _flush_c_stdio()
        for fd in saved:
            os.dup2(devnull_fd, fd)
        yield
    finally:
        sys.stdout.flush()
        sys.stderr.flush()
        _flush_c_stdio()                  # drain buffered LAPACK noise into /dev/null BEFORE restore
        for fd, original in saved.items():
            os.dup2(original, fd)
            os.close(original)
        os.close(devnull_fd)


def _downscale(img, max_w: int = _STITCH_MAX_W):
    """Return `img` scaled down so its width is <= max_w (area interpolation for clean shrink). Frames
    already <= max_w are returned unchanged — we never upscale."""
    import cv2

    height, width = img.shape[:2]
    if max(width, height) <= max_w:
        return img
    scale = max_w / float(max(width, height))
    new_size = (max(1, int(round(width * scale))), max(1, int(round(height * scale))))
    return cv2.resize(img, new_size, interpolation=cv2.INTER_AREA)


def _make_stitcher(mode):
    """Build a cv2.Stitcher in `mode`, probing both Stitcher_create signatures seen across OpenCV
    builds: most expose `Stitcher_create(mode)`, a few only the no-arg `Stitcher_create()` + a
    `setMode` setter (and very old builds spell it `createStitcher`). Returns the configured stitcher
    or None if this build can't make one. The pano confidence threshold is lowered so weaker valid
    matches are accepted, and GPU is off (try_use_gpu=False) for determinism."""
    import cv2

    factory = getattr(cv2, "Stitcher_create", None) or getattr(cv2, "createStitcher", None)
    stitcher = None
    if factory is not None:
        try:
            stitcher = factory(mode)                      # Stitcher_create(mode) — the common path
        except TypeError:
            try:
                stitcher = factory(False)                 # createStitcher(try_use_gpu=False)
                if hasattr(stitcher, "setMode"):
                    stitcher.setMode(mode)
            except TypeError:
                stitcher = factory()                      # bare no-arg form
                if hasattr(stitcher, "setMode"):
                    stitcher.setMode(mode)
    if stitcher is None:
        return None
    if hasattr(stitcher, "setPanoConfidenceThresh"):
        stitcher.setPanoConfidenceThresh(_PANO_CONF_THRESH)
    return stitcher


def _try_stitch(images, mode):
    """Run one stitch pass in `mode` with C-level stdout+stderr suppressed (so the LAPACK flood can't
    reach the user) and exceptions contained. Returns (status, pano) — status is cv2.Stitcher_OK only
    on a clean stitch; on a failed build, missing mode, or any cv2 exception returns a non-OK status
    with pano=None."""
    import cv2

    stitcher = _make_stitcher(mode)
    if stitcher is None:
        return cv2.Stitcher_ERR_NEED_MORE_IMGS, None
    with _suppress_c_output():
        try:
            status, pano = stitcher.stitch(images)
        except cv2.error:
            return cv2.Stitcher_ERR_NEED_MORE_IMGS, None
    return status, pano


def stitch(paths: list[str], out_path: str) -> dict:
    """Stitch a list of overlapping frames into one panorama, ROBUSTLY. HANDLES status codes instead
    of raising and keeps the C-level LAPACK/DLASCL flood off the user's terminal.

    Pipeline: read + drop undecodable frames -> DOWNSCALE each to ~1600px wide (24MP destabilizes the
    bundle adjuster) -> try PANORAMA mode (spherical, lowered pano-confidence) -> on any non-OK
    status FALL BACK to SCANS mode (affine — far more stable for low-parallax handheld sweeps). Both
    passes run with fd 2 redirected to /dev/null so the DLASCL warnings can't spam stdout/stderr.

    Returns a clear dict every time:
        {"stitched": bool, "status": int|None, "mode_used": "panorama"|"scans"|None,
         "size": (w, h)|None, "hint": str, "out_path"?: str}
    On success writes the pano to out_path and includes "out_path"; on failure writes nothing."""
    import cv2
    import numpy as np

    def decode_bgr(path: str):
        # One bounded snapshot supplies both header validation and native decode.
        snapshot = source_bytes(path)
        image_size_from_bytes(snapshot)
        image = cv2.imdecode(np.frombuffer(snapshot, dtype=np.uint8), cv2.IMREAD_COLOR)
        if image is None or image.nbytes > MAX_DECODED_RGB_BYTES:
            return None
        return image

    from stack_session import validate_frames
    if len(paths) < 2:
        return {"stitched": False, "status": None, "mode_used": None, "size": None,
                "hint": f"need >=2 readable frames to stitch — {_OVERLAP_HINT}"}
    validate_frames(len(paths))
    images = []
    for path in paths:
        try:
            image = decode_bgr(path)
        except (OSError, ValueError, cv2.error):
            image = None
        if image is None:
            return {"stitched": False, "status": None, "mode_used": None, "size": None,
                    "hint": "An identified panorama frame is unreadable; refusing a substituted or incomplete sweep."}
        images.append(_downscale(image))
    if len(images) < 2:
        return {"stitched": False, "status": None, "mode_used": None, "size": None,
                "hint": f"need >=2 readable frames to stitch — {_OVERLAP_HINT}"}

    images = [_downscale(img) for img in images]          # 24MP -> ~1600px so the solve stays stable

    # try the full spherical PANORAMA solve first; fall back to the affine SCANS solve, which holds
    # up far better on flat, low-parallax handheld sweeps where PANORAMA's warp degenerates.
    for mode_name, mode in (("panorama", cv2.Stitcher_PANORAMA), ("scans", cv2.Stitcher_SCANS)):
        status, pano = _try_stitch(images, mode)
        if status == cv2.Stitcher_OK and pano is not None:
            Path(out_path).parent.mkdir(parents=True, exist_ok=True)
            if pano.nbytes > MAX_DECODED_RGB_BYTES:
                return {"stitched": False, "status": int(status), "mode_used": mode_name,
                        "size": None, "hint": "panorama output exceeds memory budget"}
            if not cv2.imwrite(out_path, pano):
                return {"stitched": False, "status": int(status), "mode_used": mode_name,
                        "size": None, "hint": f"cv2.imwrite failed for {out_path}"}
            height, width = pano.shape[:2]
            return {"stitched": True, "status": int(status), "mode_used": mode_name,
                    "size": (width, height), "out_path": out_path, "hint": _OVERLAP_HINT}
        last_status = status

    return {"stitched": False, "status": int(last_status), "mode_used": None, "size": None,
            "hint": _OVERLAP_HINT}


def live_pano(cam, frames: int, out_path: str, save_dir: str = "/tmp/wifi_pano_frames") -> dict:
    """End-to-end live panorama: sweep the frames, transfer the full-res stills off the card, filter
    to cv2-decodable suffixes, and stitch them to `out_path`. Returns a summary dict. Falls back
    gracefully when the transfer returns fewer decodable frames than requested (stitch reports a
    clean error dict rather than raising)."""
    shot = shoot_sweep(cam, frames=frames)
    import wifi_stack
    paths, receipts = wifi_stack.transfer_capture_jpegs(cam, shot["postviews"], save_dir)
    shot["frame_receipts"] = receipts
    shot["transfer_resolution"] = "capture-linked JPEG; inspect receipt dimensions"
    if len(paths) < 2:
        return {"stitched": False, "reason": "need >=2 transferred frames",
                "transferred": paths, "shot": shot}
    result = stitch(paths, out_path)
    return {**result, "transferred": paths, "shot": shot}


def _parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Live WiFi panorama on the A7 II")
    parser.add_argument("--frames", type=int, default=6)
    parser.add_argument("--out", default="/tmp/pano.jpg")
    return parser.parse_args(argv)


if __name__ == "__main__":
    args = _parse_args(sys.argv[1:])
    camera = wifi_a7ii.connect()
    if not camera:
        print("No camera. Launch Smart Remote Control on the A7 II + join its WiFi AP first.")
        raise SystemExit(1)
    import json
    result = live_pano(camera, args.frames, args.out)
    print(json.dumps(result, indent=2, default=str))
