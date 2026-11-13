"""Regression tests for the subtler audit bugs — run by `backend/run_tests.sh` so they can't
silently come back. (The pure-physics regressions live inline in `physics.py`'s self-test.)
"""
import sys
import tempfile
from pathlib import Path

import numpy as np
from PIL import Image

sys.path.insert(0, str(Path(__file__).resolve().parent))

import brain
import live_verify
import physics


def _solid(value: int, path: Path) -> None:
    Image.fromarray(np.full((64, 64, 3), value, dtype="uint8")).save(path)


def test_metering_handles_clipping() -> None:
    """Audit C2: mean-luminance metering underread blown scenes — a fully-white frame could only
    ever add +1.19 EV no matter how overexposed. The clipping-aware meter must read a blown frame
    much brighter than a midtone one AND flag the clipping."""
    with tempfile.TemporaryDirectory() as d:
        white, gray = Path(d) / "w.jpg", Path(d) / "g.jpg"
        _solid(255, white)   # fully blown
        _solid(118, gray)    # ~middle gray
        ev_white, clip_white = live_verify.meter_ev100(str(white), 100, 5.6, 1 / 100)
        ev_gray, clip_gray = live_verify.meter_ev100(str(gray), 100, 5.6, 1 / 100)
    assert clip_white > 0.9, f"blown frame should read ~100% clipped, got {clip_white:.2f}"
    assert clip_gray < 0.05, f"midtone frame shouldn't clip, got {clip_gray:.2f}"
    assert ev_white > ev_gray + 1.5, (
        f"blown frame must meter much brighter than midtone ({ev_white:.1f} vs {ev_gray:.1f}) — "
        "the old mean meter capped that gap at ~1.2 EV")
    print(f"  metering clipping-aware: ok (white EV{ev_white:.1f}/{clip_white:.0%} clip, "
          f"gray EV{ev_gray:.1f})")


def test_subject_motion_drives_faster_shutter() -> None:
    """Audit: the shutter floor must compose with min(), not max(). A subject that needs a fast
    shutter (small subject_motion_floor_s) is the STRICTER demand and must bind BELOW the
    camera-shake floor. The old max() let the slower shake floor win and blurred the subject."""
    import embed_index  # heavy (torch); imported lazily and stubbed so no CLIP index is needed
    real_recommend = embed_index.recommend
    embed_index.recommend = lambda image_path: {"f_number": 2.8, "iso": 100, "focal_mm": 50}
    try:
        focal = 50.0
        vib_floor = physics.handheld_shutter_floor(focal, physics.A7II.crop_factor, 1.0)  # ~1/50s
        scene_ev = 8.0  # floor binds in both cases; ISO to hold 1/500 stays under the ceiling
        baseline = brain.recommend_settings("stub.jpg", scene_ev, focal_mm=focal)
        fast = brain.recommend_settings("stub.jpg", scene_ev, focal_mm=focal,
                                        subject_motion_floor_s=1 / 500)
    finally:
        embed_index.recommend = real_recommend

    assert fast.shutter_s < baseline.shutter_s, (
        f"fast subject must drive a faster shutter than the shake floor alone: "
        f"subject {fast.shutter_s:.4f}s vs baseline {baseline.shutter_s:.4f}s (max() ties them)")
    assert fast.shutter_s < vib_floor, (
        f"subject floor (1/500s) must bind below the 1/{1/vib_floor:.0f}s shake floor, "
        f"got 1/{1/fast.shutter_s:.0f}s")
    assert 40 <= 1 / baseline.shutter_s <= 60, (
        f"no subject motion -> stay at the ~1/50s shake floor (None must not force it faster), "
        f"got 1/{1/baseline.shutter_s:.0f}s")
    assert any("subject motion" in note for note in fast.notes), fast.notes
    assert not any("subject motion" in note for note in baseline.notes), baseline.notes
    print(f"  subject-motion floor binds with min(): ok "
          f"(baseline 1/{1/baseline.shutter_s:.0f}s -> subject 1/{1/fast.shutter_s:.0f}s)")


def test_subject_distance_drives_aperture() -> None:
    """M9: when the depth model gives a subject distance + DoF target, the aperture must stop down
    to COVER the subject — physics over taste. Wide taste (f/2.8) + a deep subject -> stopped down,
    and the APERTURE card reports the real subject distance instead of 'pending'."""
    import embed_index  # heavy (torch); stubbed so no CLIP index is needed
    real_recommend = embed_index.recommend
    embed_index.recommend = lambda image_path: {"f_number": 2.8, "iso": 100, "focal_mm": 50}
    try:
        ev = 13.0  # bright daylight: aperture isn't forced open by exposure, so DoF can drive it
        baseline = brain.recommend_settings("stub.jpg", ev, focal_mm=50)
        # The EXIF focus distance is the proof of focus the depth rule requires (PR #73).
        deep = brain.recommend_settings("stub.jpg", ev, focal_mm=50, subject_distance_m=2.0,
                                        dof_depth_m=1.0, scene_class="generic", focus_distance_m=2.0)
    finally:
        embed_index.recommend = real_recommend

    assert baseline.aperture <= 3.2, f"no subject -> keep the wide taste, got f/{baseline.aperture}"
    assert deep.aperture >= 8.0, (
        f"a deep subject must stop the aperture down to cover it, got f/{deep.aperture} (taste f/2.8)")
    assert "subject 2.00m" in deep.card("APERTURE").sub_factors[0].readout, deep.card("APERTURE")
    print(f"  subject distance drives aperture: ok "
          f"(taste f/2.8 -> baseline f/{baseline.aperture}, deep subject f/{deep.aperture})")


if __name__ == "__main__":
    test_metering_handles_clipping()
    test_subject_motion_drives_faster_shutter()
    test_subject_distance_drives_aperture()
    print("regression tests: PASS")
