#!/usr/bin/env python3
"""shoot.py — the whole autopilot in one command. Say a vibe, the camera takes the shot.

    python shoot.py "golden hour, warm and moody"
    python shoot.py "bright and airy for instagram"
    python shoot.py                      # no vibe = just read the scene and balance it

The loop: your words -> a LOCAL model picks an exposure target -> the camera sets itself and shoots
-> the photo opens. With no camera connected (battery asleep) it still runs the brain and prints the
plan, so you can feel the "vibe -> decision" magic today and it shoots the moment the camera wakes.

No API billing (local Ollama, with a keyword fallback), no server, no browser — just this file.
"""

import os
import subprocess
import sys
from pathlib import Path

BACKEND = Path(__file__).resolve().parent / "backend"
ENGINE_REPO = Path(__file__).resolve().parent  # monorepo root — the sonyremote package lives here
for path in (BACKEND, ENGINE_REPO):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))

import intent_brain


def _reflexion_enabled(argv: list[str]) -> bool:
    """Is the closed reflexion loop turned on? OFF by default — one bounded product capture.

    Engage only on an explicit opt-in: the ``--auto-correct`` CLI flag, or ``AUTOPILOT_REFLEXION=1``
    in the environment. Anything else (including the flag absent) leaves the bounded ordinary capture path.
    """
    if "--auto-correct" in argv:
        return True
    return os.getenv("AUTOPILOT_REFLEXION", "") == "1"


def _open_photo(path: Path) -> None:
    """Pop the finished photo open in Preview so you see it the instant it lands."""
    try:
        subprocess.run(["open", str(path)], check=False)
    except OSError:
        pass


def main() -> int:
    reflexion = _reflexion_enabled(sys.argv[1:])
    # Strip the control flag so it never leaks into the spoken intent.
    words = [arg for arg in sys.argv[1:] if arg != "--auto-correct"]
    intent = " ".join(words).strip() or None

    # 1. The brain: words -> a plan. Runs today, with or without the camera.
    plan = intent_brain.interpret(intent)
    print(f"\n  you said : {intent or '(nothing — just balance the scene)'}")
    print(f"  the plan : {plan['feel']}")
    print(f"  because  : {plan['why']}")
    wb = plan["white_balance"] or "leave as-is"
    print(f"  aiming   : brightness {int(plan['target_brightness'])} · white balance {wb}\n")

    # Use the same bounded capture, immutable plan and persistence path as the app.
    # RealEngine reads CAMERA_MODEL/CAMERA_PORT so two attached cameras require a choice.
    import engine
    import product_capture
    from sonyremote import CameraError, CaptureOutcomeUncertain

    camera = engine.RealEngine()
    status = camera.status()
    if not status.get("ready"):
        print(f"  {status.get('hint') or 'No supported camera is ready.'}")
        if status.get("selection_required"):
            print("  Select CAMERA_MODEL or CAMERA_PORT before taking a photograph.")
        for candidate in status.get("cameras", []):
            print(f"  {candidate.get('model')} at {candidate.get('port')}")
        print("  The plan is ready; no capture was attempted.\n")
        return 1 if status.get("cameras") else 0

    print("  Taking up to 4 exposures for bounded refinement." if reflexion
          else "  Taking up to 2 exposures: meter and final photograph.")
    try:
        shot = product_capture.shoot(camera, intent, refine=reflexion, applied_plan=plan)
    except CaptureOutcomeUncertain as exc:
        print(f"  {exc}")
        if exc.capture_path:
            print(f"  Preserved capture: {exc.capture_path}")
        print("  No automatic retry was attempted; inspect the preserved outcome first.\n")
        return 1
    except CameraError as exc:
        # A connection failure is reported once. Capture retries belong nowhere in
        # the CLI; the shared engine owns exact post-actuation outcome handling.
        print(f"  Camera stopped: {exc}. No automatic retry was attempted.\n")
        return 1

    photo = engine.SHOTS_DIR / shot.image if shot.image else None
    if photo is not None and photo.is_file():
        print(f"  Photograph preserved → {photo}")
        print(f"  settings : {shot.settings}")
        print(f"  result   : {shot.summary}")
        if shot.why:
            print(f"  details  : {shot.why}")
        if shot.evidence.get("persistence"):
            print(f"  storage  : {shot.evidence['persistence']}")
        if shot.evidence.get("refinement"):
            print(f"  refinement : {shot.evidence['refinement']['stopped_reason']}")
        _open_photo(photo)
        if not shot.ok:
            print("  Quality target was not met; review the saved photograph. No capture retry is required for delivery.")
        return 0

    print("  No verified photograph was returned; no retry was attempted.\n")
    return 1


def _run_reflexion(shoot_at, base_target, intent):
    """Drive the closed reflexion loop and narrate each attempt. Returns the delivered report.

    ``shoot_at(target)`` performs one camera capture; the critique looks at that shot's final image
    and is mapped to a corrected target by ``reflexion.reflexion_shoot``. The vision model only ever
    moves the TARGET — physics computes the real ISO/aperture/shutter — so the determinism boundary
    holds. If the first critique is unavailable the loop degrades to a single shot automatically."""
    import reflexion
    import vision_critique

    def critique_report(report):
        """Best-effort critique of a shot's final image. Returns None (no critique) if the shot
        failed or vision is unreachable, so the loop degrades safely to a single shot."""
        if not report or not report.get("final_image"):
            return None
        seen = vision_critique.critique(Path(report["final_image"]), intent)
        exposure = seen.get("exposure")
        print(f"  the AI sees : exposure {exposure} — {seen.get('suggestion', '')}")
        if seen.get("issues"):
            print(f"  it noticed  : {', '.join(seen['issues'])}")
        return seen

    def shoot_fn(target):
        if target != base_target:
            print(f"  re-shooting — nudging the exposure target to {int(round(target))}…")
        return shoot_at(target)

    result = reflexion.reflexion_shoot(shoot_fn, critique_report, base_target, max_attempts=2)

    if result.stopped_reason == "satisfied":
        print(f"  the AI's happy — nailed it in {result.attempts} shot(s).")
    elif result.stopped_reason == "no_critique":
        print("  (vision unavailable — kept the single shot.)")
    elif result.stopped_reason == "stalled":
        print(f"  stopping — the correction isn't converging (stall-guard, {result.attempts} shots).")
    elif result.stopped_reason == "max_attempts":
        print(f"  stopping at the {result.attempts}-shot cap — handing back the closest frame.")
    return result.final_report


if __name__ == "__main__":
    raise SystemExit(main())
