"""M4 night / timelapse tests — camera-FREE and deterministic. No live camera: a fake camera stub
replays canned Sony responses and (for the intervalometer) sleeps a known amount in take_picture so
the wall-clock spacing math is checkable. Mirrors the FakeCamera pattern in test_wifi_lastwave.py.

    backend/.venv-ml/bin/python backend/test_wifi_night.py
    python3 backend/test_wifi_night.py          (night_exposure + clamp + timing run plain too)
"""
from __future__ import annotations

import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

# Import the actual modules: process-wide dependency stubs contaminate aggregate tests.

import wifi_night


# --- a fake camera: scripted responses + a take_picture that sleeps a known duration --------------
class FakeNightCamera:
    """Minimal stand-in for CameraRemote. take_picture sleeps `capture_s` so the intervalometer's
    wall-clock spacing (sleep interval - elapsed) is deterministically testable; begin_session and the
    avail/setters are no-ops that just record calls."""

    def __init__(self, capture_s: float = 0.0):
        self.capture_s = capture_s
        self.begin_calls = 0
        self.shots = 0

    def begin_session(self):
        self.begin_calls += 1
        return []

    def take_picture(self, timeout: float = 40.0):
        self.shots += 1
        if self.capture_s:
            time.sleep(self.capture_s)
        return [f"http://fake/postview_{self.shots}.jpg"]


# --- night_exposure: <=30s, sane iso/frames across EVs --------------------------------------------
def test_night_exposure_picks_sane_settings():
    # VERY dark scene (deeply negative EV100): 30s alone can't carry it -> shutter pegged at 30s,
    # ISO pushed well above base, MORE frames for the noisier stack.
    very_dark = wifi_night.night_exposure(-12.0, max_shutter_s=30.0, base_iso=100)
    assert very_dark["shutter_s"] <= 30.0, very_dark
    assert very_dark["iso"] > 100, very_dark           # ISO forced up
    assert very_dark["iso_stops_pushed"] > 0, very_dark
    assert very_dark["n_frames"] > 4, very_dark        # more averaging for the gain noise

    # MODERATELY dark: ~1s of exposure (EV100 ~ 0) is plenty within the 30s cap -> shutter carries it
    # at BASE ISO, minimum frame count.
    moderate = wifi_night.night_exposure(0.0, max_shutter_s=30.0, base_iso=100)
    assert moderate["iso"] == 100, moderate            # no gain needed
    assert moderate["iso_stops_pushed"] == 0.0, moderate
    assert moderate["shutter_s"] <= 30.0, moderate
    assert moderate["n_frames"] == 4, moderate         # clean -> floor

    # A scene needing exactly ~30s (EV100 ~ -4.9 -> 2^4.9 ~= 30s) still sits at base ISO (shutter just
    # reaches the cap) — the boundary should NOT spuriously push ISO.
    import math
    at_cap = wifi_night.night_exposure(-math.log2(30.0), max_shutter_s=30.0, base_iso=100)
    assert at_cap["shutter_s"] <= 30.0 and at_cap["iso"] == 100, at_cap

    # darker scene -> at least as much ISO as a less-dark one (monotonic gain). And the very-dark case
    # must out-ISO the moderate one.
    assert very_dark["iso"] >= moderate["iso"], (very_dark, moderate)
    # shutter is ALWAYS clamped to the WiFi ceiling regardless of how dark it is
    for ev in (-20.0, -12.0, -5.0, 0.0, 3.0):
        rec = wifi_night.night_exposure(ev)
        assert rec["shutter_s"] <= 30.0, (ev, rec)
        assert 100 <= rec["iso"] <= 51200, (ev, rec)
        assert 4 <= rec["n_frames"] <= 16, (ev, rec)
    print("  ok  night_exposure (<=30s, base ISO when shutter suffices, more ISO+frames in the dark)")


# --- the >30s clamp (both night_exposure's cap and _clamp_shutter's loud clamp) -------------------
def test_shutter_clamped_to_30s():
    # night_exposure never returns more than 30s even when asked for a bigger max (the API has no bulb)
    rec = wifi_night.night_exposure(-15.0, max_shutter_s=120.0, base_iso=100)
    assert rec["shutter_s"] <= 30.0, rec

    # _clamp_shutter caps >30s down to 30s and leaves <=30s untouched
    assert wifi_night._clamp_shutter(45.0) == 30.0
    assert wifi_night._clamp_shutter(60.0) == 30.0
    assert wifi_night._clamp_shutter(30.0) == 30.0
    assert wifi_night._clamp_shutter(15.0) == 15.0
    assert wifi_night._clamp_shutter(0.5) == 0.5

    # label formatting round-trips through wifi_autopilot._num back to seconds
    import wifi_autopilot
    assert wifi_night._shutter_label(30.0) == '30"'
    assert wifi_night._shutter_label(15.0) == '15"'
    assert wifi_autopilot._num(wifi_night._shutter_label(30.0)) == 30.0
    assert wifi_autopilot._num(wifi_night._shutter_label(15.0)) == 15.0
    print("  ok  shutter clamp (>30s -> 30s, label round-trips to seconds)")


# --- intervalometer timing: spacing ~= interval, accounting for capture duration ------------------
def test_intervalometer_timing_math():
    # capture takes 0.20s; interval 0.50s -> each gap should be ~0.50s wall-clock (sleep the remainder,
    # NOT a flat 0.50 on top of the 0.20 capture). Assert the cadence, not just the sleeps.
    cam = FakeNightCamera(capture_s=0.20)
    frames, interval = 4, 0.50
    t0 = time.monotonic()
    result = wifi_night.intervalometer(cam, frames=frames, interval_s=interval)
    total = time.monotonic() - t0

    assert cam.shots == frames, cam.shots
    assert len(result["postviews"]) == frames
    per = result["per_frame"]
    # non-final frames: realized interval ~= the requested interval (within timing slop)
    for entry in per[:-1]:
        assert abs(entry["interval_s"] - interval) < 0.12, entry
        assert entry["capture_s"] >= 0.20 - 0.05, entry           # capture really took ~0.20s
        assert entry["slept_s"] == round(interval - entry["capture_s"], 3) or entry["slept_s"] >= 0
    # the LAST frame has no trailing wait (no shot follows it)
    assert per[-1]["slept_s"] == 0.0, per[-1]
    # total wall time ~= (frames-1) intervals + one final capture (the last frame still shoots, but
    # doesn't sleep). ~= 3*0.50 + 0.20 = ~1.70s. Generous upper bound for CI jitter.
    assert (frames - 1) * interval <= total < (frames - 1) * interval + 0.20 + 0.6, total

    # capture LONGER than the interval -> no negative sleep, gap floored at the capture time (cadence
    # can't beat how long a shot takes). capture 0.30s, interval 0.10s -> slept 0 every frame.
    cam2 = FakeNightCamera(capture_s=0.30)
    res2 = wifi_night.intervalometer(cam2, frames=3, interval_s=0.10)
    for entry in res2["per_frame"]:
        assert entry["slept_s"] == 0.0, entry                     # never sleeps a negative remainder
    print("  ok  intervalometer timing (wall-clock spacing accounts for capture, no negative sleep)")


def test_intervalometer_no_full_res_no_resession():
    # without full_res_dir, the session is begun ONCE up front (no per-frame transfer/re-session).
    cam = FakeNightCamera(capture_s=0.0)
    wifi_night.intervalometer(cam, frames=5, interval_s=0.0)
    assert cam.begin_calls == 1, cam.begin_calls
    assert cam.shots == 5, cam.shots
    print("  ok  intervalometer (single begin_session when not pulling full-res)")


if __name__ == "__main__":
    print("night / timelapse tests:")
    test_night_exposure_picks_sane_settings()
    test_shutter_clamped_to_30s()
    test_intervalometer_timing_math()
    test_intervalometer_no_full_res_no_resession()
    print("ALL night / timelapse tests PASS")
