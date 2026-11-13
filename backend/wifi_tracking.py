"""Stage 2 — real-time vision -> focus tracking loop (~5 fps) over WiFi.

Unblocked now that AF reliability (Stage 1a) is done. The loop keeps native AF locked on the
auto-chosen subject as it (or the camera) moves:

    [grab liveview frame] -> [vision picks the subject] -> [smooth the point] -> [touch-AF it]
    -> repeat at a capped rate.

Patent-clean: the subject is chosen AUTOMATICALLY each frame (saliency/depth), never voice-named,
then handed to the camera's native AF — the same design-around as the single-shot autopilot, just
run continuously.

Design choices that make it actually track instead of chatter:
  - FOLLOW A MOVING SUBJECT, don't lock the dominant static object. Live-validated failure: feeding
    vision_banana's `saliency_centroid` straight to AF pinned focus on the most salient STATIC object
    (a wall 3.8m away) while the person walked left<->right in front of it — saliency is appearance-
    dominant, not motion-aware. The subject POINT is now chosen by `_select_subject_point`:
      1. an appearance tracker (cv2 TrackerMIL) seeded from vision_banana's `subject_box` and updated
         on every liveview frame — local + fast, no depth-server round-trip — so it follows the subject
         whether it moves or briefly stops. Re-seeded from a fresh detection when its box degenerates /
         leaves the frame or every N frames, so it can't drift forever or get stuck on the wrong thing.
      2. when no tracker box exists yet, frame-to-frame motion: if the inter-frame difference clears a
         floor, the moving region's (diff-weighted) centroid is the subject.
      3. else `saliency_centroid` — a genuinely still scene still focuses the real subject.
    NOTE this build's OpenCV (4.13, base `opencv-python`) ships NO CSRT/KCF (those need
    `opencv-contrib-python`'s `cv2.legacy.*`); we probe for CSRT/KCF/MIL in that order and use whatever
    is present, falling back to MIL which is always available. The whole selector is wrapped so the
    never-raise contract holds — any tracker/CV failure degrades to saliency.
  - RE-DETECT every frame but SMOOTH the target with an EMA (exponential moving average) so a noisy
    per-frame centroid doesn't make AF hunt. A large jump (subject switch / fast motion) snaps
    instead of easing, so we don't lag a genuine cut.
  - DEADBAND: skip the touch-AF call when the smoothed point barely moved (< a few %). The
    setTouchAFPosition round-trip is the expensive part; not re-issuing it when nothing changed is
    what buys the frame rate.
  - RATE GOVERNOR: sleep to hold a target FPS (the A7 II liveview + a vision round-trip realistically
    sustains ~4-6 fps over WiFi; 5 is the design point).
  - FAIL-SAFE: vision returns None on any error -> we hold the last good point rather than dropping
    focus. The loop never raises into the capture path.

    backend/.venv-ml/bin/python backend/wifi_tracking.py --fps 5 --seconds 30
"""
from __future__ import annotations

import argparse
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import vision_banana
import wifi_a7ii
from image_safety import open_image, open_rgb

try:  # cv2 lives in .venv-ml; under the plain interpreter the selector degrades to saliency-only.
    import cv2  # type: ignore
    import numpy as np  # type: ignore
except Exception:  # noqa: BLE001 - optional; absence must not break the import or the loop
    cv2 = None  # type: ignore
    np = None  # type: ignore

# EMA smoothing factor for the target point: new = alpha*measured + (1-alpha)*previous. Higher =
# snappier/noisier, lower = smoother/laggier. 0.5 tracks a walking subject without visible chatter.
_EMA_ALPHA = 0.5
# A measured jump larger than this (fraction of frame width) is treated as a real subject switch /
# fast move -> snap to it instead of easing, so we don't lag a cut.
_SNAP_JUMP_FRAC = 0.25
# Don't re-issue touch-AF if the smoothed point moved less than this (fraction of frame) — the AF is
# already on it; re-commanding only wastes a round-trip and risks a refocus hunt.
_DEADBAND_FRAC = 0.03

# --- subject-point selection (the part being fixed: motion/appearance-aware, not static-saliency) ---
# Re-seed the appearance tracker from a FRESH vision_banana detection at least this often, so it can
# never drift forever or stay stuck on the wrong object even if its own confidence stays (falsely)
# high. ~2.4 fps over WiFi -> 24 frames is a re-detect roughly every 10 s.
_RESEED_EVERY_FRAMES = 24
# Mean absolute inter-frame pixel difference (0..255) above which the scene is "moving" and we trust
# the motion centroid. Below it the scene is effectively static -> fall back to saliency. 4.0 clears
# liveview sensor noise / JPEG shimmer while a walking subject reads well above it (live: ~10).
_MOTION_DIFF_FLOOR = 4.0
# When building the motion centroid, keep only diff pixels brighter than this fraction of the frame's
# peak difference, so the centroid sits on the actual moving object, not faint compression noise.
_MOTION_PEAK_FRAC = 0.5


def _make_tracker():
    """Construct the best available single-object tracker, or None if cv2 has none we can use.

    OpenCV moved trackers between namespaces across versions/builds: contrib builds expose
    `cv2.legacy.TrackerCSRT_create` (and older ones `cv2.TrackerCSRT_create`); the base `opencv-python`
    wheel ships none of CSRT/KCF and only the always-present MIL. Probe CSRT -> KCF -> MIL across both
    the modern factory (`cv2.TrackerX.create`), the flat `cv2.TrackerX_create`, and `cv2.legacy.*`, and
    return the first that constructs. Returns None (never raises) so the caller degrades to saliency."""
    if cv2 is None:
        return None
    legacy = getattr(cv2, "legacy", None)
    for name in ("TrackerCSRT", "TrackerKCF", "TrackerMIL"):
        for factory in (
            getattr(getattr(cv2, name, None), "create", None),   # cv2.TrackerCSRT.create()
            getattr(cv2, f"{name}_create", None),                # cv2.TrackerCSRT_create()
            getattr(getattr(legacy, name, None), "create", None) if legacy else None,
            getattr(legacy, f"{name}_create", None) if legacy else None,
        ):
            if factory is None:
                continue
            try:
                return factory()
            except Exception:  # noqa: BLE001 - try the next factory/tracker
                continue
    return None


def _box_center_ok(box, width: int, height: int) -> tuple[float, float] | None:
    """Center (x,y) of a tracker box=(x,y,w,h), or None if the box is degenerate or off-frame.

    MIL (and KCF) keep reporting success even after they have lost the target, so we can't trust the
    tracker's own ok flag alone — this geometric sanity check is what actually triggers a re-seed when
    the box has collapsed to nothing or wandered out of the frame. A malformed (non-4-tuple / non-
    numeric) box also returns None rather than raising, so the never-raise contract holds upstream."""
    try:
        x, y, w, h = (float(v) for v in box)
    except (TypeError, ValueError):
        return None
    if w <= 1 or h <= 1:
        return None
    cx, cy = x + w / 2.0, y + h / 2.0
    if cx < 0 or cy < 0 or cx > width or cy > height:
        return None
    return (cx, cy)


def _motion_centroid(prev_gray, cur_gray) -> tuple[float, float] | None:
    """Diff-weighted centroid of the moving region between two grayscale frames, or None if the scene
    is effectively static (mean diff below the floor). Used as the fallback subject point before the
    appearance tracker has a box — so a person walking in front of a static backdrop is selected by
    their motion, not by the backdrop's saliency."""
    if cv2 is None or np is None:
        return None
    diff = np.abs(cur_gray.astype(np.float32) - prev_gray.astype(np.float32))
    if float(diff.mean()) < _MOTION_DIFF_FLOOR:
        return None  # static scene -> let the caller fall back to saliency
    peak = float(diff.max())
    if peak <= 0:
        return None
    ys, xs = np.nonzero(diff > _MOTION_PEAK_FRAC * peak)
    if xs.size == 0:
        return None
    weights = diff[ys, xs]
    total = float(weights.sum())
    if total <= 0:
        return None
    return (float((xs * weights).sum() / total), float((ys * weights).sum() / total))


def _read_gray(frame_path: str):
    """Grayscale ndarray for the frame, or None if cv2/numpy absent or the read failed. PIL-decoded
    (the camera writes JPEG via grab_liveview_frame) so a truncated/short frame returns None instead
    of raising — keeping the never-raise contract."""
    if cv2 is None or np is None:
        return None
    try:
        arr = np.asarray(open_rgb(frame_path).convert("L"))
        return arr if arr.size else None
    except Exception:  # noqa: BLE001 - a dropped/short/corrupt frame must not raise
        return None


def _reseed_tracker_on_motion(state: TrackState, gray, center, width: int, height: int) -> None:
    """Seed/refresh the appearance tracker on a box around the MOTION centroid — NOT the static salient
    subject_box (that was the bug: CSRT then locked the backdrop). Seeded on the mover, the tracker
    bridges a brief PAUSE in motion instead of snapping back to the salient object. Throttled — CSRT
    init is costly, so only re-seed when there's no tracker or the lock is stale."""
    if cv2 is None:
        return
    state.tracker_age += 1
    if state.tracker is not None and state.tracker_age < _RESEED_EVERY_FRAMES:
        return  # keep the current lock; don't re-init every frame
    tracker = _make_tracker()
    if tracker is None:
        return
    box_w = max(1, int(0.22 * width))            # a roughly person-sized box around the mover
    box_h = max(1, int(0.45 * height))
    x = max(0, min(int(center[0] - box_w / 2), width - 1))
    y = max(0, min(int(center[1] - box_h / 2), height - 1))
    box_w = min(box_w, width - x)
    box_h = min(box_h, height - y)
    try:
        tracker.init(_as_bgr(gray), (x, y, box_w, box_h))
        state.tracker = tracker
        state.tracker_age = 0
        state.reseeds += 1
    except Exception:  # noqa: BLE001 - init failed -> no tracker, motion still drives selection
        state.tracker = None


def _select_subject_point(state: TrackState, analysis: dict | None, frame_path: str,
                          width: int, height: int) -> tuple[float, float] | None:
    """Pick the subject point for THIS frame, MOTION-FIRST. Validated live: the frame-diff motion
    centroid swept x 13%->71% across a walk while the saliency/subject_box-seeded tracker sat at ~50%
    on the static backdrop. Order:

      1. frame-to-frame MOTION centroid — when the scene moves, THAT is the subject; (re)seed the
         appearance tracker on the moving region so a brief pause keeps the lock;
      2. the appearance tracker (seeded from motion) — bridges short pauses when motion drops;
      3. vision_banana `saliency_centroid` — a genuinely still scene focuses the salient subject.

    NEVER raises — any CV/tracker failure falls through to saliency. The returned point feeds the
    EXISTING EMA-smooth + deadband + touch-AF path unchanged."""
    gray = _read_gray(frame_path)
    saliency = analysis.get("saliency_centroid") if analysis else None

    # 1. MOTION FIRST: a moving subject in front of a static backdrop is selected by its motion, not by
    #    the backdrop's saliency/depth (which dominate vision_banana's subject_box — the old bug).
    if gray is not None and state.prev_gray is not None:
        moving = _motion_centroid(state.prev_gray, gray)
        if moving is not None:
            state.motion_picks += 1
            _reseed_tracker_on_motion(state, gray, moving, width, height)
            return moving

    # 2. No motion this frame: hold the subject via the appearance tracker (seeded on the mover).
    if state.tracker is not None and gray is not None and cv2 is not None:
        try:
            ok, raw_box = state.tracker.update(_as_bgr(gray))
        except Exception:  # noqa: BLE001 - tracker blew up -> drop it, fall through to saliency
            ok, raw_box, state.tracker = False, None, None
        if ok and raw_box is not None:
            center = _box_center_ok(raw_box, width, height)
            if center is not None:
                state.tracker_age += 1
                return center
        state.tracker = None

    # 3. Genuinely still scene (or no CV at all): the real subject is the salient one.
    if saliency and len(saliency) >= 2:
        return (float(saliency[0]), float(saliency[1]))
    return state.point_xy   # hold the last good point


def _as_bgr(gray):
    """Trackers want a 3-channel image; promote a grayscale ndarray to BGR (cheap, no color anyway)."""
    return cv2.cvtColor(gray, cv2.COLOR_GRAY2BGR)


@dataclass
class TrackState:
    """Carried across frames so the loop can smooth + deadband + run the appearance tracker."""
    point_xy: tuple[float, float] | None = None   # smoothed subject center, pixels
    last_commanded_xy: tuple[float, float] | None = None  # point of the LAST touch-AF command issued
    frames: int = 0
    af_commands: int = 0
    af_locks: int = 0
    misses: int = 0                                # frames where vision returned nothing
    fps_samples: list[float] = field(default_factory=list)
    error: str | None = None                       # set if loop setup failed before the loop ran
    # --- subject-point selection state (motion/appearance-aware) ---
    tracker: object | None = None                  # live cv2 single-object tracker (None until seeded)
    tracker_age: int = 0                           # frames since the tracker was last (re)seeded
    prev_gray: object | None = None                # previous frame grayscale ndarray, for motion diff
    reseeds: int = 0                               # times the appearance tracker was (re)seeded
    motion_picks: int = 0                          # frames the point came from the motion centroid

    @property
    def mean_fps(self) -> float:
        return (len(self.fps_samples) / sum(self.fps_samples)) if self.fps_samples else 0.0


def _smooth(prev: tuple[float, float] | None, measured: tuple[float, float],
            frame_w: int) -> tuple[float, float]:
    """EMA toward `measured`, except snap on a large jump (subject switch / fast motion)."""
    if prev is None:
        return measured
    dx = measured[0] - prev[0]
    dy = measured[1] - prev[1]
    if (dx * dx + dy * dy) ** 0.5 > _SNAP_JUMP_FRAC * frame_w:
        return measured  # snap — a genuine cut, don't ease into it
    return (prev[0] + _EMA_ALPHA * dx, prev[1] + _EMA_ALPHA * dy)


def _moved_enough(prev: tuple[float, float] | None, new: tuple[float, float], frame_w: int) -> bool:
    """True if `new` differs from `prev` by more than the deadband (so re-commanding AF is worth it)."""
    if prev is None:
        return True
    dist = ((new[0] - prev[0]) ** 2 + (new[1] - prev[1]) ** 2) ** 0.5
    return dist > _DEADBAND_FRAC * frame_w


def track_step(cam, state: TrackState, frame_path: str) -> TrackState:
    """One tracking iteration: grab a liveview frame, pick the subject, smooth, and (if it moved
    past the deadband) touch-AF it. Mutates + returns `state`. NEVER raises — ANY failure (a dropped
    liveview grab, a corrupt frame, the vision server timing out, a missing/short saliency centroid,
    a dropped AF call) is swallowed and we hold the last good point so the loop keeps running."""
    state.frames += 1
    try:
        cam.grab_liveview_frame(save_path=frame_path)
        with open_image(frame_path) as img:   # already bounded before decode
            width, height = img.size
        analysis = vision_banana.analyze(frame_path)  # vision_banana is already fail-safe -> None
        # Motion/appearance-aware subject point (appearance tracker -> motion centroid -> saliency),
        # NOT the bare static saliency centroid that locked the dominant still object live. This is
        # itself never-raise; it returns None when nothing usable is available this frame.
        measured = _select_subject_point(state, analysis, frame_path, width, height)
        # Advance the motion-diff reference AFTER selection used the old prev_gray (so this frame's
        # grayscale becomes next frame's "previous"). Held regardless of whether a point was found.
        state.prev_gray = _read_gray(frame_path)
        if measured is None:
            state.misses += 1
            return state  # hold the last good point; AF stays where it was
    except Exception:  # noqa: BLE001 - a dropped frame / decode error must not kill the loop
        state.misses += 1
        return state

    previous = state.point_xy
    smoothed = _smooth(previous, measured, width)
    state.point_xy = smoothed

    # Deadband against the LAST COMMANDED point, not the previous smoothed point: comparing to the
    # previous frame let slow drift slip under the deadband every frame and never accumulate, so AF
    # was never re-commanded. Measuring drift since the last actual command lets it cross eventually.
    if _moved_enough(state.last_commanded_xy, smoothed, width):
        if isinstance(cam, wifi_a7ii.CameraRemote) and not wifi_a7ii.touch_af_enabled():
            return state
        x_pct = max(0.0, min(100.0, 100.0 * smoothed[0] / width))
        y_pct = max(0.0, min(100.0, 100.0 * smoothed[1] / height))
        try:
            locked = cam.touch_af(x_pct, y_pct)
            state.af_commands += 1
            state.last_commanded_xy = smoothed  # only update when a command was actually issued
            if locked:
                state.af_locks += 1
        except Exception:  # noqa: BLE001 - a dropped AF call must not kill the loop
            pass
    return state


def track(cam, fps: float = 5.0, seconds: float = 30.0,
          frame_path: str = "/tmp/track_lv.jpg") -> TrackState:
    """Run the tracking loop for `seconds` at a capped `fps`. Returns the final TrackState with the
    achieved frame rate and AF lock stats. Continuous focus mode (AF-C) keeps native AF re-acquiring
    between our touch-AF commands."""
    state = TrackState()
    # Setup (begin_session / set_focus_mode) runs BEFORE the never-raise loop and can itself raise
    # (no camera, dropped link). Wrap it so a setup failure returns the state with an error note
    # instead of propagating into the caller's capture path.
    try:
        cam.begin_session()
        try:
            cam.set_focus_mode("AF-C")  # continuous AF rides our moving touch-AF point
        except Exception:  # noqa: BLE001 - some firmwares only expose AF-S; fall back
            try:
                cam.set_focus_mode("AF-S")
            except Exception:  # noqa: BLE001
                pass
    except Exception as exc:  # noqa: BLE001 - setup failed; don't propagate into the capture path
        state.error = f"track setup failed: {exc}"
        return state

    frame_budget = 1.0 / fps if fps > 0 else 0.0
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        start = time.monotonic()
        track_step(cam, state, frame_path)
        processing = time.monotonic() - start
        # rate governor: hold the target fps (sleep only if we ran faster than the budget)
        if frame_budget > processing:
            time.sleep(frame_budget - processing)
        # record the WHOLE-LOOP period (processing + governor sleep) so mean_fps is the ACHIEVED
        # loop rate, not the raw processing rate — that's what the --fps target is judged against.
        state.fps_samples.append(time.monotonic() - start)
    return state


def _parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Stage-2 real-time WiFi focus tracking")
    parser.add_argument("--fps", type=float, default=5.0)
    parser.add_argument("--seconds", type=float, default=30.0)
    return parser.parse_args(argv)


if __name__ == "__main__":
    args = _parse_args(sys.argv[1:])
    camera = wifi_a7ii.connect()
    if not camera:
        print("No camera. Launch Smart Remote Control on the A7 II + join its WiFi AP first.")
        raise SystemExit(1)
    final = track(camera, fps=args.fps, seconds=args.seconds)
    print(f"tracked {final.frames} frames @ {final.mean_fps:.1f} fps  "
          f"(AF commands {final.af_commands}, locks {final.af_locks}, vision misses {final.misses})")
