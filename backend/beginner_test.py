#!/usr/bin/env python3
"""beginner_test.py — the "is it magic?" test, one command per tester.

Run this once for each of your 10 beginners. It greets them, takes a spoken-aloud-then-typed
vibe (or nothing), runs the SAME loop shoot.py runs — words -> a local model picks an exposure
target -> the camera wakes and shoots itself -> the photo opens — then asks three quick questions
and logs an anonymous structured session to the private UX validation ledger.

    CAMERA_SERIAL=A7II00SERIAL python beginner_test.py   # facilitator runs this, hands over the keyboard

CAMERA_SERIAL names the body by its USB serial (ports re-enumerate); without it no photo is taken.

Standalone on purpose: it replicates shoot.py's tiny loop rather than importing it, so the test
harness can't break the production path and vice-versa. With no camera connected it still runs the
brain and prints the plan, so you can dry-run the script the night before. Reads the docs/TEST_PROTOCOL.md
facilitator script before your first session.

No API billing (local Ollama, with a keyword fallback), no server, no browser — just this file.
"""

from __future__ import annotations

import os
import subprocess
import sys
import time
from pathlib import Path

# Same path wiring shoot.py uses: the backend (intent_brain, UX validation) and the monorepo root
# (where the sonyremote package lives) both go on sys.path so this file runs from anywhere.
BACKEND = Path(__file__).resolve().parent
ENGINE_REPO = Path(__file__).resolve().parent.parent  # monorepo root
for path in (BACKEND, ENGINE_REPO):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))

import intent_brain
import ux_validation


def _open_photo(path: Path) -> None:
    """Pop the finished photo open in Preview so the tester sees it the instant it lands."""
    try:
        subprocess.run(["open", str(path)], check=False)
    except OSError:
        pass


def _ask(prompt: str) -> str:
    """One free-text question. Returns '' if the tester just hits enter (or aborts)."""
    try:
        return input(prompt).strip()
    except (EOFError, KeyboardInterrupt):
        print()
        return ""


def _ask_rating(prompt: str) -> int | None:
    """Magic rating 1-5. Re-asks on garbage; returns None if the tester gives up / aborts."""
    while True:
        raw = _ask(prompt)
        if not raw:
            return None
        try:
            value = int(raw)
        except ValueError:
            print("    (just a number 1 to 5, please)")
            continue
        if 1 <= value <= 5:
            return value
        print("    (1 = meh, 5 = actual magic — pick 1 to 5)")


def _ask_choice(prompt: str, allowed: set[str], aliases: dict[str, str] | None = None) -> str | None:
    """Ask for one normalized structured choice; blank means the participant stopped."""

    aliases = aliases or {}
    while True:
        raw = _ask(prompt).lower().replace("-", "_").replace(" ", "_")
        if not raw:
            return None
        value = aliases.get(raw, raw)
        if value in allowed:
            return value
        print(f"    (choose one of: {', '.join(sorted(allowed))})")


def ask_consent() -> bool:
    print(
        "  Before we start: this saves your answers and the photo filename under an\n"
        "  anonymous session ID. It does not save your name, IP address, device info,\n"
        "  or the exact vibe you type. No audio or video is recorded.\n"
    )
    answer = _ask_choice("  Okay to start? (yes / no): ", {"yes", "no"}, {"y": "yes", "n": "no"})
    return answer == "yes"


def greet() -> None:
    print("\n" + "=" * 64)
    print("  THE CAMERA THAT LISTENS")
    print("=" * 64)
    print(
        "\n  Hi! This is a tiny experiment. You don't need to know anything about\n"
        "  cameras — that's the whole point. You'll tell it the *vibe* you want\n"
        "  in plain words, and it sets itself up and takes the photo. Or say\n"
        "  nothing and let it read the room. There are no wrong answers here —\n"
        "  we're testing the camera, not you.\n"
    )


def take_the_shot(intent: str | None) -> tuple[str | None, bool]:
    """Run the exact shoot.py loop: brain -> wake -> auto_expose -> open the photo.

    Returns (image_name_or_path, took_a_real_photo). image is None if nothing was produced
    (e.g. camera asleep) — the questions still run so we capture the reaction to the *plan*.
    """
    # 1. The brain: words -> a plan. Runs today, with or without the camera.
    plan = intent_brain.interpret(intent)
    print(f"\n  you said : {intent or '(nothing — just read the room)'}")
    print(f"  the plan : {plan['feel']}")
    print(f"  because  : {plan['why']}")
    wb = plan["white_balance"] or "leave as-is"
    print(f"  aiming   : brightness {int(plan['target_brightness'])} · white balance {wb}\n")

    # 2. The camera. detect() lists bodies without claiming USB; if none, the plan still stands.
    from sonyremote import detect
    if not detect():
        print("  camera asleep — the plan is ready but no photo this round.")
        print("  (Connect the A7 II: USB > PC Remote, then rerun for a live shot.)\n")
        return None, False

    serial = os.getenv("CAMERA_SERIAL")
    if not serial:
        print("  CAMERA_SERIAL is not set — the plan is ready but no photo this round.")
        print("  (Set CAMERA_SERIAL to the body's USB serial so the shutter can only fire that body.)\n")
        return None, False
    from sonyremote import camera_for_serial
    from sonyremote.autoexpose import auto_expose
    print("  taking over the camera…")
    with camera_for_serial(serial) as cam:  # proves the serial before any write or shutter
        # The A7 II naps in PC Remote and reports junk settings until nudged. wake() polls until
        # the lens reports a real aperture (or times out) so auto_expose doesn't meter a sleeping
        # body. Best-effort: if it times out we still try — the worst case is one wasted frame.
        if not cam.wake():
            print("  (camera was slow to wake — trying the shot anyway)")
        report = auto_expose(cam, target_mean=plan["target_brightness"])

    image = report.get("final_image")
    if report.get("ok") and image:
        photo = Path(image)
        print(f"  got it → {photo}")
        print(f"  settings : {report.get('final_settings', {})}\n")
        _open_photo(photo)
        return photo.name, True

    print(f"  couldn't nail it: {report.get('note', 'check the camera and try again')}\n")
    return (Path(image).name if image else None), False


def ask_the_three_questions(took_photo: bool) -> tuple[int | None, str | None, str, str | None]:
    """Return the fixed rating, adoption choice, confusion area, and optional participant words."""

    print("  --- three quick questions ------------------------------------\n")
    subject = "that" if took_photo else "the idea of that"
    magic = _ask_rating(f"  1. How magic did {subject} feel? (1 = meh, 5 = actual magic): ")
    would_use = _ask_choice(
        "  2. Would you use this? (yes / no / maybe): ",
        ux_validation.WOULD_USE_VALUES,
        {"y": "yes", "n": "no", "m": "maybe", "not_sure": "unsure"},
    )
    confusion_area = _ask_choice(
        "  3. What got in the way? "
        "(none / setup / prompt / waiting / result / controls / review_share / other): ",
        ux_validation.CONFUSION_AREAS,
        {"wait": "waiting", "review": "review_share", "share": "review_share"},
    )
    if confusion_area is None:
        confusion_area = "other"
    confused = None
    if confusion_area != "none":
        confused = _ask(
            "     In your own words (optional; do not include names or contact details): "
        )
    print()
    return magic, would_use, confusion_area, confused


def run_session() -> int:
    greet()
    if not ask_consent():
        print("\n  No problem. Nothing was saved.\n")
        return 0

    started = time.monotonic()
    intent = _ask("  Say your vibe out loud, then type it here (or just press enter): ") or None

    try:
        image, took_photo = take_the_shot(intent)
    except Exception as exc:
        # A failed camera handoff is UX data. Do not leak the exception into the private ledger.
        print(f"  The camera step stopped early ({type(exc).__name__}).")
        print("  Please rate the experience you saw so far.\n")
        image, took_photo = None, False

    magic, would_use, confusion_area, confused = ask_the_three_questions(took_photo)
    completed = magic is not None and would_use is not None
    row = ux_validation.record_session(
        consent_given=True,
        magic_rating=magic,
        would_use=would_use or "unsure",
        confusion_area=confusion_area,
        confusion_note=confused,
        interface="cli",
        experience_mode="live_capture" if took_photo else "plan_only",
        capture_succeeded=took_photo,
        intent_provided=bool(intent),
        duration_seconds=time.monotonic() - started,
        photo_reference=image,
        completion_status="completed" if completed else "incomplete",
        study_id=os.getenv(
            "CAMERA_AUTOPILOT_UX_STUDY_ID", ux_validation.DEFAULT_STUDY_ID
        ),
    )
    report = ux_validation.build_report(study_id=row["study_id"])

    print(f"  Logged anonymous session {row['session_id']}.")
    print(
        f"  Study progress: {report['gate']['cohort_sessions']}/"
        f"{report['gate']['cohort_size_required']} eligible sessions."
    )
    print("  Keep the session ID if you want this response deleted.")
    print("  Thank you — that's genuinely useful.")
    print("  " + "=" * 62 + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(run_session())
