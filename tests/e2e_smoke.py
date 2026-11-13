"""End-to-end field harness for sony-camera-mcp.

Run from the repo root with the project's venv:

    CAMERA_SERIAL=A7II00SERIAL ./.venv/bin/python tests/e2e_smoke.py

The camera stages fire only the body whose USB serial is CAMERA_SERIAL (ports re-enumerate);
without CAMERA_SERIAL they are SKIPPED.

WITH a camera connected (Sony A7 II in PC Remote mode), this drives the full pipeline:
detect -> connect -> read settings + shooting_mode -> capture -> measure_jpeg ->
auto_expose -> assert the final mean lands within tolerance of target -> sun_times for a
sample location. Each stage prints PASS / FAIL.

WITHOUT a camera, the camera stages are clearly marked SKIPPED and only the camera-free
parts run: sun_times for a sample location, and measure_jpeg on an existing captures/*.JPG.

Exit code is non-zero ONLY on a real failure. SKIPPED stages never fail the run, so this is
safe to run on a Pi/laptop with no camera attached as a sanity check of the analysis path.
"""

from __future__ import annotations

import os
import sys
from datetime import date as date_cls
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

# Make the harness runnable from anywhere: put the repo root on sys.path so `sonyremote`
# imports regardless of the current working directory.
REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from sonyremote import Camera, CameraError, camera_for_serial, detect
from sonyremote.autoexpose import auto_expose, measure_jpeg

# Auto-expose target/tolerance mirror the autoexpose defaults so the assertion matches the loop.
TARGET_MEAN = 112.0
TOLERANCE = 14.0

# Sample location for the planning stage: Lake Tahoe (matches the field guide example).
SAMPLE_LAT = 39.0968
SAMPLE_LON = -120.0324
SAMPLE_TZ = "America/Los_Angeles"

CAPTURE_DIR = REPO_ROOT / "captures"

# --- tiny PASS/FAIL/SKIP reporter --------------------------------------------
_results: list[tuple[str, str]] = []  # (stage, outcome) where outcome in {PASS, FAIL, SKIP}


def record(stage: str, outcome: str, detail: str = "") -> None:
    _results.append((stage, outcome))
    tail = f" — {detail}" if detail else ""
    print(f"[{outcome:4}] {stage}{tail}")


def sun_times(latitude: float, longitude: float, date: str = "today", tz: str | None = None) -> dict:
    """Standalone copy of the server's sun_times planning logic (no MCP/camera needed).

    Kept identical to server.sun_times so the harness exercises the same astral path without
    spinning up the FastMCP server just to call one pure function.
    """
    from astral import Observer
    from astral.sun import SunDirection, blue_hour, golden_hour, sun

    tzinfo = ZoneInfo(tz) if tz else datetime.now().astimezone().tzinfo
    day = datetime.now(tzinfo).date() if date in (None, "", "today") else date_cls.fromisoformat(date)
    observer = Observer(latitude=latitude, longitude=longitude)
    events = sun(observer, date=day, tzinfo=tzinfo)

    def window(direction: SunDirection, kind) -> list[str]:
        start, end = kind(observer, day, direction, tzinfo)
        return [start.isoformat(timespec="minutes"), end.isoformat(timespec="minutes")]

    fmt = lambda moment: moment.isoformat(timespec="minutes")
    return {
        "date": day.isoformat(),
        "timezone": str(tzinfo),
        "dawn": fmt(events["dawn"]),
        "sunrise": fmt(events["sunrise"]),
        "noon": fmt(events["noon"]),
        "sunset": fmt(events["sunset"]),
        "dusk": fmt(events["dusk"]),
        "golden_hour_morning": window(SunDirection.RISING, golden_hour),
        "golden_hour_evening": window(SunDirection.SETTING, golden_hour),
        "blue_hour_morning": window(SunDirection.RISING, blue_hour),
        "blue_hour_evening": window(SunDirection.SETTING, blue_hour),
    }


def newest_capture() -> Path | None:
    """Most recent existing JPEG in captures/, for the camera-free measure_jpeg check."""
    jpegs = sorted(
        list(CAPTURE_DIR.glob("*.JPG")) + list(CAPTURE_DIR.glob("*.jpg")),
        key=lambda p: p.stat().st_mtime,
    )
    return jpegs[-1] if jpegs else None


# --- camera-dependent pipeline -----------------------------------------------
def run_camera_pipeline(cam: Camera) -> bool:
    """Full live pipeline against a connected camera. Returns True if every stage passed."""
    ok = True

    info = cam.info()
    record("connect", "PASS", f"{info.model} over {info.port}")

    settings = cam.get_settings()
    mode = cam.shooting_mode()
    record("read settings + shooting_mode", "PASS", f"mode={mode} settings={settings}")

    photo = cam.capture(download=True)
    if photo is not None and photo.exists() and photo.stat().st_size > 0:
        record("capture", "PASS", f"{photo} ({photo.stat().st_size} bytes)")
    else:
        record("capture", "FAIL", "no file written")
        return False  # nothing downstream can run without a frame

    measured = measure_jpeg(photo)
    record(
        "measure_jpeg",
        "PASS",
        f"mean={measured['mean']:.1f} blown={measured['blown_pct']:.1f}% black={measured['black_pct']:.1f}%",
    )

    report = auto_expose(cam, target_mean=TARGET_MEAN, tolerance=TOLERANCE)
    final_mean = report["final_mean"]
    shots = len(report["iterations"])
    record(
        "auto_expose",
        "PASS" if report["ok"] else "FAIL",
        f"mode={report['mode']} shots={shots} final_mean={final_mean} note={report['note']}",
    )
    if not report["ok"]:
        ok = False

    # Independent assertion: the final frame's mean must land within tolerance of target.
    within = abs(final_mean - TARGET_MEAN) <= TOLERANCE
    record(
        "assert final mean within tolerance",
        "PASS" if within else "FAIL",
        f"|{final_mean} - {TARGET_MEAN}| {'<=' if within else '>'} {TOLERANCE}",
    )
    if not within:
        ok = False

    return ok


# --- camera-free analysis/planning checks ------------------------------------
def run_sun_times_stage() -> bool:
    """sun_times for the sample location — runs with or without a camera."""
    try:
        times = sun_times(SAMPLE_LAT, SAMPLE_LON, date="today", tz=SAMPLE_TZ)
    except Exception as exc:  # astral can raise on polar edge cases; surface it as a failure
        record("sun_times", "FAIL", str(exc))
        return False
    record(
        "sun_times",
        "PASS",
        f"Lake Tahoe {times['date']}: sunset {times['sunset']}, "
        f"evening golden {times['golden_hour_evening'][0]}–{times['golden_hour_evening'][1]}",
    )
    return True


def run_measure_existing_stage() -> tuple[str, bool]:
    """measure_jpeg on an existing capture — the camera-free half of the analysis path.

    Returns (outcome, counts_as_failure). A missing captures/ folder is reported SKIPPED, not
    FAILED, so a clean checkout with no sample frames still exits 0.
    """
    sample = newest_capture()
    if sample is None:
        record("measure_jpeg (existing capture)", "SKIP", f"no JPEGs in {CAPTURE_DIR}")
        return "SKIP", False
    try:
        measured = measure_jpeg(sample)
    except Exception as exc:
        record("measure_jpeg (existing capture)", "FAIL", str(exc))
        return "FAIL", True
    record(
        "measure_jpeg (existing capture)",
        "PASS",
        f"{sample.name}: mean={measured['mean']:.1f} blown={measured['blown_pct']:.1f}% "
        f"black={measured['black_pct']:.1f}% rgb={measured['rgb']}",
    )
    return "PASS", False


def main() -> int:
    print("=" * 72)
    print("sony-camera-mcp end-to-end field harness")
    print("=" * 72)

    real_failure = False
    camera_stages = (
        "connect",
        "read settings + shooting_mode",
        "capture",
        "measure_jpeg (live)",
        "auto_expose",
        "assert final mean within tolerance",
    )

    # 1) Detection decides which path we take. detect() never claims the camera.
    found = detect()
    print(f"\ndetect(): {found or 'NOTHING FOUND'}\n")

    # detect() can surface a generic 'USB PTP Class Camera' phantom (a USB host/hub entry) even
    # with no real body attached; connect() then fails to claim it. We try to connect, and only
    # if connect SUCCEEDS do we run the live pipeline. A connect failure is the field "no usable
    # camera" condition (phantom, not in PC Remote, cable issue) — SKIP, never a real failure.
    cam: Camera | None = None
    serial = os.getenv("CAMERA_SERIAL")
    if found and not serial:
        print("CAMERA_SERIAL is not set: the live pipeline fires only a body proven by its USB serial.\n")
    if found and serial:
        try:
            cam = camera_for_serial(serial)
            cam.connect()
        except CameraError as exc:
            cam = None
            print(f"Camera listed but could not be claimed: {exc}\n")

    if cam is not None:
        print("Camera connected — running the FULL pipeline.\n")
        try:
            if not run_camera_pipeline(cam):
                real_failure = True
        finally:
            cam.disconnect()
    else:
        reason = ("CAMERA_SERIAL not set" if found and not serial else
                  "no usable camera (listed but unclaimable)" if found else "no camera connected")
        print(f"Camera stages SKIPPED — {reason}; running camera-free path only.\n")
        for stage in camera_stages:
            record(stage, "SKIP", reason)

    # 2) Camera-free stages always run.
    print()
    if not run_sun_times_stage():
        real_failure = True
    _, measure_failed = run_measure_existing_stage()
    if measure_failed:
        real_failure = True

    # 3) Summary.
    passed = sum(1 for _, outcome in _results if outcome == "PASS")
    skipped = sum(1 for _, outcome in _results if outcome == "SKIP")
    failed = sum(1 for _, outcome in _results if outcome == "FAIL")
    print("\n" + "-" * 72)
    print(f"SUMMARY: {passed} passed, {skipped} skipped, {failed} failed")
    print("-" * 72)

    if real_failure:
        print("RESULT: FAIL (a real stage failed)")
        return 1
    print("RESULT: OK (no real failures; SKIPPED stages do not fail the run)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
