"""Subject-SELECTION tests for the Stage-2 tracking loop — the fix for the live failure where focus
locked the dominant STATIC object (saliency) instead of following a MOVING subject.

Camera-free + deterministic: a fake camera renders a synthetic liveview frame on each grab — a bright
blob translating across a dark canvas — and a stubbed `vision_banana` feeds a subject_box / saliency
centroid. We then assert the point the loop actually commands to AF TRACKS THE BLOB'S MOTION, that a
genuinely static scene falls back to saliency, and that a dropped/short frame never raises.

    backend/.venv-ml/bin/python backend/test_wifi_tracking_subject.py   (needs cv2 + numpy + PIL)
"""
from __future__ import annotations

import sys
import tempfile
import types
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import wifi_tracking

try:
    import numpy as np
    from PIL import Image
    _HAVE_CV = wifi_tracking.cv2 is not None
except Exception:  # noqa: BLE001
    _HAVE_CV = False


# --- a fake camera that renders a moving (or static) bright blob as the liveview frame ------------
class BlobCamera:
    """Writes a synthetic JPEG on each grab_liveview_frame and records every touch_af (x%,y%) call.

    `positions` is the list of blob CENTER x-pixels, one per frame; the blob holds `blob_y` in y. When
    the list runs out the last position repeats (a subject that stopped). `static` renders the SAME
    frame every grab (no inter-frame motion at all)."""

    def __init__(self, positions, width=640, height=480, radius=24, blob_y=None, static=False):
        self.positions = list(positions)
        self.width = width
        self.height = height
        self.radius = radius
        self.blob_y = height // 2 if blob_y is None else blob_y
        self.static = static
        self.frame_idx = 0
        self.touch_calls: list[tuple[float, float]] = []

    def _render(self, cx):
        canvas = np.zeros((self.height, self.width, 3), dtype=np.uint8)
        # faint texture so the JPEG isn't a degenerate solid (keeps the diff honest), well below floor
        canvas[::8, ::8] = 12
        r = self.radius
        x0, x1 = max(0, cx - r), min(self.width, cx + r)
        y0, y1 = max(0, self.blob_y - r), min(self.height, self.blob_y + r)
        canvas[y0:y1, x0:x1] = 255  # the bright subject
        return canvas

    def grab_liveview_frame(self, stream_url=None, save_path=None):
        idx = 0 if self.static else min(self.frame_idx, len(self.positions) - 1)
        cx = self.positions[idx]
        Image.fromarray(self._render(cx)).save(save_path, format="JPEG", quality=92)
        self.frame_idx += 1
        return b""

    # AF-C setup the loop calls; harmless no-ops for the selection test
    def begin_session(self):
        return []

    def set_focus_mode(self, mode):
        pass

    def touch_af(self, x_pct, y_pct):
        self.touch_calls.append((x_pct, y_pct))
        return True


def _install_vision_stub(box_for, saliency=None):
    """Swap vision_banana for a stub. `box_for(frame_idx) -> subject_box|None` decides when a fresh
    detection is available (so we can seed the tracker then withhold detections to prove the tracker /
    motion path keeps following on its own). Returns a restore() callable."""
    calls = {"n": 0}
    stub = types.ModuleType("vision_banana")

    def analyze(_path, *_a, **_k):
        idx = calls["n"]
        calls["n"] += 1
        box = box_for(idx)
        if box is None and saliency is None:
            return None
        return {"subject_box": box, "saliency_centroid": saliency,
                "subject_distance_m": 3.0}

    def metering_region(analysis):
        if not analysis:
            return None
        box = analysis.get("subject_box")
        return tuple(int(v) for v in box) if box and len(box) == 4 else None

    stub.analyze = analyze
    stub.metering_region = metering_region
    orig = sys.modules.get("vision_banana")
    sys.modules["vision_banana"] = stub
    real, wifi_tracking.vision_banana = wifi_tracking.vision_banana, stub

    def restore():
        if orig is not None:
            sys.modules["vision_banana"] = orig
        wifi_tracking.vision_banana = real

    return restore, calls


def _x_pixels(cam):
    """Convert recorded touch_af x-percentages back to pixels for readable assertions."""
    return [pct[0] / 100.0 * cam.width for pct in cam.touch_calls]


# --- tests ---------------------------------------------------------------------------------------
def test_point_tracks_moving_blob_over_static_saliency_decoy():
    """The exact live failure, reproduced: a STATIC saliency competitor sits at the left edge the whole
    time (mimicking the salient wall 3.8m away), while the real subject (the blob) walks left->right.
    The commanded AF point must FOLLOW THE BLOB, not lock onto the static saliency decoy."""
    width = 640
    # A person-sized blob walks x=120->520 in ~40px steps. Selection is now motion-FIRST, so the mover
    # must produce enough inter-frame motion to clear the real motion-diff floor the way a walking
    # person does (validated live: a real walk swept the motion centroid 14-70%). A 24px blob @ 20px
    # steps is BELOW the floor and would correctly read as "no motion" -> hence the larger radius/steps.
    positions = list(range(120, 521, 40))
    cam = BlobCamera(positions, width=width, radius=70)
    # The decoy: saliency_centroid is pinned at a fixed point (~9% width) on EVERY frame — this is the
    # dominant-static-object signal that wrongly won live. If selection still used bare saliency, the
    # point would stay glued to ~60px instead of tracking the blob out to ~520.
    static_decoy = [int(0.09 * width), cam.blob_y]
    # vision detects the subject_box only on the FIRST 2 frames (seed the tracker), then goes silent —
    # so anything that keeps following afterwards is the appearance tracker / motion, NOT re-detection,
    # and definitely not the (still-present) saliency decoy.
    def box_for(idx):
        if idx < 2:
            cx = positions[min(idx, len(positions) - 1)]
            return (cx - 24, cam.blob_y - 24, 48, 48)
        return None
    restore, _ = _install_vision_stub(box_for, saliency=static_decoy)
    # A private frame path: a fixed /tmp name is shared with every concurrent run of this test (another
    # worktree's suite), whose saves then land between this loop's write and reads, so the loop diffs
    # the other run's frames or loses its motion reference to a truncated file (seen: AF x 379 -> 137).
    try:
        with tempfile.TemporaryDirectory() as tmp:
            state = wifi_tracking.TrackState()
            frame = str(Path(tmp) / "frame.jpg")
            for _ in positions:
                wifi_tracking.track_step(cam, state, frame)
    finally:
        restore()

    assert state.point_xy is not None, "loop never produced a subject point"
    xs = _x_pixels(cam)
    assert len(xs) >= 3, f"expected several AF commands as the blob moved, got {cam.touch_calls}"
    # FOLLOWS the motion: the last commanded x is far to the RIGHT of the first (the blob's travel),
    # and it ended near the blob's final position (520) — NOT pinned at the ~60px static decoy.
    assert xs[-1] - xs[0] > 0.4 * width, f"point did not track the blob's travel: {[round(x) for x in xs]}"
    assert xs[-1] > 400, f"point lagged far behind the moving blob (final x={xs[-1]:.0f}, blob~520)"
    # the final smoothed point is on the blob, PROVABLY not on the static saliency decoy (~60px).
    assert state.point_xy[0] > 380, f"smoothed point stuck behind blob: {state.point_xy}"
    assert abs(state.point_xy[0] - static_decoy[0]) > 0.4 * width, (
        f"point collapsed onto the static saliency decoy at {static_decoy[0]} — the live bug: {state.point_xy}")
    print(f"  ok  point tracks moving blob over static saliency decoy "
          f"(AF x {round(xs[0])} -> {round(xs[-1])}, decoy pinned at {static_decoy[0]}, blob 120 -> 520)")


def test_static_scene_falls_back_to_saliency():
    """A genuinely STILL scene has no motion to chase -> the point must come from saliency_centroid,
    landing on the real subject (here the saliency marker), not drifting on noise."""
    width, height = 640, 480
    sal = [int(0.7 * width), int(0.5 * height)]   # saliency marks a subject at 70% width
    cam = BlobCamera([sal[0]], width=width, height=height, static=True)
    # NO subject_box ever (so the tracker is never seeded), only a saliency centroid.
    restore, _ = _install_vision_stub(lambda idx: None, saliency=sal)
    try:
        with tempfile.TemporaryDirectory() as tmp:
            state = wifi_tracking.TrackState()
            frame = str(Path(tmp) / "frame.jpg")
            for _ in range(6):
                wifi_tracking.track_step(cam, state, frame)
    finally:
        restore()

    assert state.tracker is None, "no subject_box was offered -> tracker must never have seeded"
    assert state.motion_picks == 0, "a static scene must not register motion picks"
    assert cam.touch_calls, "static scene still has a real subject -> AF should be commanded"
    # the commanded point sits on the saliency marker (within the deadband), not wandering
    assert abs(state.point_xy[0] - sal[0]) < 0.05 * width, (state.point_xy, sal)
    print(f"  ok  static scene falls back to saliency (point x={round(state.point_xy[0])} ~ {sal[0]})")


def test_dropped_and_short_frame_never_raise():
    """A camera that drops the grab, and a zero-length/corrupt frame on disk, must both be swallowed —
    the never-raise contract still holds with the new selection path."""
    # (a) grab raises
    class BoomCam:
        def grab_liveview_frame(self, stream_url=None, save_path=None):
            raise OSError("liveview dropped")
    restore, _ = _install_vision_stub(lambda idx: None, saliency=[100, 100])
    try:
        with tempfile.TemporaryDirectory() as tmp:
            state = wifi_tracking.TrackState()
            out = wifi_tracking.track_step(BoomCam(), state, str(Path(tmp) / "nonexistent.jpg"))
            assert out.frames == 1 and out.misses == 1 and out.point_xy is None, vars(out)

            # (b) a SHORT/corrupt frame: grab "succeeds" but writes a truncated file PIL can't open
            class ShortFrameCam:
                def grab_liveview_frame(self, stream_url=None, save_path=None):
                    Path(save_path).write_bytes(b"\xff\xd8")  # JPEG SOI only — truncated, undecodable
                def touch_af(self, *_):  # selection must yield nothing -> AF never reached
                    raise AssertionError("touch_af must NOT fire on an undecodable frame")
            state2 = wifi_tracking.TrackState()
            out2 = wifi_tracking.track_step(ShortFrameCam(), state2, str(Path(tmp) / "short.jpg"))
            assert out2.frames == 1 and out2.misses == 1 and out2.af_commands == 0, vars(out2)
    finally:
        restore()
    print("  ok  dropped + short frame never raise (selection path stays fail-safe)")


def test_make_tracker_is_available():
    """The fix relies on SOME tracker existing in this OpenCV build — prove the probe finds one (MIL
    is always present even on the base opencv-python wheel that lacks CSRT/KCF)."""
    tracker = wifi_tracking._make_tracker()
    assert tracker is not None, "no usable cv2 tracker found — _make_tracker probe failed"
    assert hasattr(tracker, "init") and hasattr(tracker, "update"), tracker
    print("  ok  _make_tracker finds a usable tracker (CSRT/KCF/MIL probe)")


if __name__ == "__main__":
    print("wifi tracking subject-selection tests:")
    if not _HAVE_CV:
        print("  skip ALL (cv2/numpy/PIL absent — run under backend/.venv-ml)")
        raise SystemExit(0)
    test_make_tracker_is_available()
    test_point_tracks_moving_blob_over_static_saliency_decoy()
    test_static_scene_falls_back_to_saliency()
    test_dropped_and_short_frame_never_raise()
    print("ALL wifi tracking subject-selection tests PASS")
