"""Friend-facing LIVE demo of the autopilot — the laptop narrates what the camera is doing so the
automation is *visible*. Point the camera at a person or a scene and run it; it talks through seeing the
subject, reading the light, choosing every setting (in plain English), focusing, and firing — hands-off.

    backend/.venv-ml/bin/python backend/wifi_demo.py            # one narrated auto shot
    backend/.venv-ml/bin/python backend/wifi_demo.py --track    # "watch it follow me" (10s tracking)

Pure showmanship over the real pipeline — every decision is the actual brain's, just spoken slowly. The
brain's own debug output is muted (via _quiet) so only the friendly narration shows.
"""
from __future__ import annotations

import contextlib
import io
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import brain
import live_verify
import vision_banana
import wifi_a7ii
import wifi_autopilot
from image_safety import image_size


@contextlib.contextmanager
def _quiet():
    """Mute a noisy call's stdout (the brain prints its constraint cards) so only narration shows."""
    with contextlib.redirect_stdout(io.StringIO()):
        yield


def say(text: str, pause: float = 1.3) -> None:
    print(text, flush=True)
    time.sleep(pause)


def _aperture_english(label) -> str:
    f = wifi_autopilot._num(label) or 5.6
    if f <= 2.8:
        return f"f/{label} — wide open, creamy blurred background"
    if f <= 5.6:
        return f"f/{label} — soft background, the subject pops"
    return f"f/{label} — stopped down, sharp front-to-back"


def _shutter_english(label) -> str:
    s = wifi_autopilot._num(label) or 0.0
    if s and s <= 1 / 250:
        return f"{label}s — freezes fast motion"
    if s and s <= 1 / 60:
        return f"{label}s — freezes normal movement"
    if s and s < 1:
        return f"{label}s — steady hands / a hint of motion"
    return f"{label}s — a long exposure"


def _iso_english(label) -> str:
    i = wifi_autopilot._num(label) or 100
    if i <= 200:
        return f"ISO {label} — cleanest, richest color"
    if i <= 1600:
        return f"ISO {label} — a touch more sensitivity"
    return f"ISO {label} — boosting for low light"


def narrated_shot(cam) -> None:
    say("\n🎥  CAMERA AUTOPILOT — watch it do everything itself.\n", 1.6)
    cam.begin_session()
    cam.set_focus_mode("AF-S")
    say("👀  Looking at the scene…", 1.6)
    cam.grab_liveview_frame(save_path="/tmp/demo_lv.jpg")
    width, height = image_size("/tmp/demo_lv.jpg")
    with _quiet():
        analysis = vision_banana.analyze("/tmp/demo_lv.jpg")
    scene = vision_banana.scene_class(analysis)
    dist = vision_banana.subject_distance_m(analysis)
    if dist:
        say(f"📍  Found the subject — about {dist:.1f} m away ({scene}).", 1.6)
    else:
        say("📍  Scanning the scene…", 1.2)

    iso0 = wifi_autopilot._num(cam.call("getIsoSpeedRate").get("result", ["100"])[0]) or 100
    f0 = wifi_autopilot._num(cam.call("getFNumber").get("result", ["5.6"])[0]) or 5.6
    s0 = wifi_autopilot._num(cam.call("getShutterSpeed").get("result", ["1/60"])[0]) or (1 / 60)
    box = vision_banana.metering_region(analysis)
    region = (box[0] / width, box[1] / height, box[2] / width, box[3] / height) if box else None
    ev100, _ = live_verify.meter_ev100("/tmp/demo_lv.jpg", iso0, f0, s0, region=region, from_liveview=True)
    light = "bright" if ev100 > 9 else "indoor light" if ev100 > 5 else "dim" if ev100 > 1 else "very dark"
    say(f"💡  Reading the light like a meter… it's {light}.", 1.7)

    with _quiet():
        rec = brain.recommend_settings(
            "/tmp/demo_lv.jpg", ev100,
            focal_mm=wifi_autopilot._LAST_FOCAL_MM_ACTUAL,  # None -> brain: 50mm floor, no depth plan
            subject_distance_m=dist, dof_depth_m=vision_banana.dof_depth_m(analysis), scene_class=scene)
    say("🧠  Choosing every setting myself:", 1.0)
    say("      • " + _aperture_english(rec.aperture_label), 1.4)
    say("      • " + _shutter_english(rec.shutter_label), 1.4)
    say("      • " + _iso_english(rec.iso_label), 1.6)

    say("🎯  Telling the camera where to focus — on the subject…", 1.6)
    if analysis:
        with _quiet():
            wifi_autopilot.focus_subject(cam, analysis, width, height)
    for target, method, setter in (
            (rec.iso_label, "getAvailableIsoSpeedRate", cam.set_iso),
            (rec.aperture_label, "getAvailableFNumber", cam.set_aperture),
            (rec.shutter_label, "getAvailableShutterSpeed", cam.set_shutter)):
        wifi_autopilot._snap_set(cam, target, method, setter)

    say("📸  3…  2…  1…", 1.6)
    postview = cam.take_picture()
    say("✅  SHOT! — every setting and the focus point, picked by the software. You didn't touch a dial.\n", 1.0)
    print(f"     What it chose:  ISO {rec.iso_label}  ·  f/{rec.aperture_label}  ·  {rec.shutter_label}s")
    if postview:
        print(f"     The photo:  {postview[0]}")


def narrated_track(cam, seconds: float = 10.0) -> None:
    say("\n🎥  Now watch it FOLLOW a moving subject — walk around.\n", 1.6)
    import wifi_tracking
    with _quiet():
        state = wifi_tracking.track(cam, fps=5, seconds=seconds)
    print(f"\n✅  Tracked you for {seconds:.0f}s — the focus point chased the movement on its own "
          f"({state.af_commands} re-focuses, {state.af_locks} locks).")


if __name__ == "__main__":
    camera = wifi_a7ii.connect()
    if not camera:
        print("Camera not found — open Smart Remote Control on the A7 II and join its WiFi AP first.")
        raise SystemExit(1)
    if "--track" in sys.argv:
        narrated_track(camera)
    else:
        narrated_shot(camera)
