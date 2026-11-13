"""User-visible critique trail for the talk -> shoot -> see -> critique -> adjust -> reshoot loop.

product_capture attaches a JSON-serializable ``critique_trail`` and a ``refinement`` summary to the
delivered Shot's evidence, for BOTH the refined and unrefined paths. These tests drive that with a
fake capture + scripted critiques (no camera, no vision model) and assert:

  * refine unsatisfied -> satisfied yields a 2-attempt trail with the correction deltas and a final
    stop_reason of 'satisfied';
  * an oscillating critique stalls, and the trail records 'stalled';
  * the unrefined path records enabled=False, a single attempt, and no critique;
  * the whole trail is JSON-serializable and leaks no absolute paths.

Run standalone:
    python3 tests/test_critique_trail.py
"""

from __future__ import annotations

import json
import sys
import tempfile
from dataclasses import dataclass, field
from pathlib import Path

from PIL import Image

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "backend"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import luma_critique
import vision_critique
from product_capture import _attach_refinement_trail
from reflexion import reflexion_shoot

LEAK_PATH = "/Users/secret/private/absolute/DSC0001.JPG"


@dataclass
class FakeShot:
    """Minimal Shot stand-in: what the trail builder reads off a delivered frame."""

    image: str
    settings: dict = field(default_factory=dict)
    applied: dict = field(default_factory=dict)
    evidence: dict = field(default_factory=dict)


class FakeCamera:
    def __init__(self):
        self.shots = 0

    def shoot(self, target):
        self.shots += 1
        return FakeShot(
            image=f"shot_{self.shots}.jpg",
            settings={"iso": "400", "shutter": "1/60", "aperture": "f/2.8"},
            applied={"target_brightness": target},
            # An absolute path lives in evidence; the trail must never surface it.
            evidence={"final_mean": target, "capture_files": [LEAK_PATH]},
        )


def good():
    return {"matches_intent": True, "exposure": "good", "brightness_delta": 0,
            "issues": [], "suggestion": "great"}


def dark(delta=30):
    return {"matches_intent": False, "exposure": "too dark", "brightness_delta": delta,
            "issues": ["shadows crushed"], "suggestion": "brighten"}


def bright(delta=-20):
    return {"matches_intent": False, "exposure": "too bright", "brightness_delta": delta,
            "issues": ["blown highlights"], "suggestion": "darken"}


def scripted(*verdicts):
    box = {"i": 0}

    def critique_fn(_report):
        i = min(box["i"], len(verdicts) - 1)
        box["i"] += 1
        return verdicts[i]

    return critique_fn


# --- refine: unsatisfied -> satisfied ------------------------------------------------------------

def test_refine_two_attempt_trail_records_correction_and_satisfied():
    cam = FakeCamera()
    result = reflexion_shoot(cam.shoot, scripted(dark(delta=30), good()), base_target=100.0, max_attempts=2)
    shot = result.final_report
    _attach_refinement_trail(shot, 100.0, result)

    refinement = shot.evidence["refinement"]
    assert refinement["enabled"] is True
    assert refinement["attempts"] == 2
    assert refinement["stop_reason"] == "satisfied"
    assert refinement["final_attempt_index"] == 1
    # Historic contract key kept alongside the trail's naming.
    assert refinement["stopped_reason"] == "satisfied"

    trail = shot.evidence["critique_trail"]
    assert len(trail) == 2, trail
    # First attempt: too dark, a +30 correction applied to reach 130, no terminal stop_reason yet.
    assert trail[0]["attempt_index"] == 0
    assert trail[0]["target_brightness_requested"] == 100.0
    assert trail[0]["critique"]["verdict"] == "too dark"
    assert trail[0]["critique"]["issues"] == ["shadows crushed"]
    assert trail[0]["critique"]["mean_brightness"] == 100.0
    assert trail[0]["correction_applied"]["delta"] == 30.0
    assert trail[0]["correction_applied"]["next_target"] == 130.0
    assert "too dark" in trail[0]["correction_applied"]["reason"]
    assert trail[0]["stop_reason"] is None
    # Second (delivered) attempt: good, no further correction, carries the loop's stop reason.
    assert trail[1]["attempt_index"] == 1
    assert trail[1]["target_brightness_requested"] == 130.0
    assert trail[1]["critique"]["verdict"] == "good"
    assert trail[1]["correction_applied"] is None
    assert trail[1]["stop_reason"] == "satisfied"
    # Every attempt names its (gallery-relative) frame.
    assert trail[0]["shot"]["image"] == "shot_1.jpg"
    assert trail[1]["shot"]["image"] == "shot_2.jpg"


# --- refine: oscillating -> stalled --------------------------------------------------------------

def test_refine_oscillating_records_stalled():
    cam = FakeCamera()
    critique_fn = scripted(dark(delta=20), bright(delta=-20), dark(delta=20), bright(delta=-20),
                           dark(delta=20), bright(delta=-20), dark(delta=20), bright(delta=-20))
    result = reflexion_shoot(cam.shoot, critique_fn, base_target=100.0, max_attempts=50)
    shot = result.final_report
    _attach_refinement_trail(shot, 100.0, result)

    assert shot.evidence["refinement"]["stop_reason"] == "stalled"
    assert shot.evidence["refinement"]["enabled"] is True
    trail = shot.evidence["critique_trail"]
    assert trail[-1]["stop_reason"] == "stalled"
    # The stalled (delivered) frame applied no correction; an earlier one did.
    assert trail[-1]["correction_applied"] is None
    assert any(entry["correction_applied"] for entry in trail[:-1])


# --- no refine -----------------------------------------------------------------------------------

def test_unrefined_path_records_single_disabled_attempt():
    shot = FakeShot(image="shot_1.jpg", settings={"iso": "400"},
                    evidence={"capture_files": [LEAK_PATH]})
    _attach_refinement_trail(shot, 112.0, None)

    refinement = shot.evidence["refinement"]
    assert refinement["enabled"] is False
    assert refinement["attempts"] == 1
    assert refinement["stop_reason"] == "refinement_disabled"
    assert refinement["stopped_reason"] == "refinement_disabled"
    assert refinement["final_attempt_index"] == 0
    trail = shot.evidence["critique_trail"]
    assert len(trail) == 1
    assert trail[0]["critique"] is None
    assert trail[0]["correction_applied"] is None
    assert trail[0]["target_brightness_requested"] == 112.0
    assert trail[0]["shot"]["image"] == "shot_1.jpg"
    assert trail[0]["stop_reason"] == "refinement_disabled"


# --- serialization / no path leak ----------------------------------------------------------------

def test_trail_is_json_serializable_and_leaks_no_absolute_paths():
    cam = FakeCamera()
    result = reflexion_shoot(cam.shoot, scripted(dark(delta=30), good()), base_target=100.0, max_attempts=2)
    shot = result.final_report
    _attach_refinement_trail(shot, 100.0, result)

    encoded = json.dumps(shot.evidence["critique_trail"])  # must not raise
    assert LEAK_PATH not in encoded, "the trail must not carry absolute private capture paths"
    # Round-trips cleanly.
    assert json.loads(encoded) == shot.evidence["critique_trail"]


# --- vision unavailable -> deterministic luma fallback still refines -----------------------------

# Fixed intent goal used by the fallback cases; the luma critic judges each frame against it.
_GOAL = 110.0
# Additive disturbance the fake pipeline adds on top of the aimed target: the first shot lands bright
# (150 vs a 110 goal, ~+0.45 stops), and the loop's corrected target (70) then lands at the goal.
_DISTURBANCE = 40


class _JpegCamera:
    """Fake capture that writes a real flat-gray JPEG whose mean luma is target + disturbance."""

    def __init__(self, directory: Path):
        self.directory = directory
        self.shots = 0

    def shoot(self, target):
        self.shots += 1
        value = max(0, min(255, round(float(target)) + _DISTURBANCE))
        name = f"shot_{self.shots}.jpg"
        Image.new("RGB", (96, 96), (value, value, value)).save(
            self.directory / name, "JPEG", quality=100, subsampling=0)
        return FakeShot(image=name, settings={"iso": "400"},
                        applied={"target_brightness": target},
                        evidence={"final_mean": value})


def _luma_critique_fn(directory: Path):
    def critique_fn(report):
        return luma_critique.critique_with_fallback(directory / report.image, "a clean photo", _GOAL)

    return critique_fn


def test_vision_unavailable_falls_back_to_luma_and_refines(monkeypatch=None):
    """With vision unreachable, Refine corrects exposure from the JPEG and the trail names the critic."""
    def _unreachable(*_args, **_kwargs):
        raise ConnectionRefusedError("vision host down")

    original = vision_critique.critique
    vision_critique.critique = _unreachable
    try:
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)
            cam = _JpegCamera(directory)
            result = reflexion_shoot(cam.shoot, _luma_critique_fn(directory),
                                     base_target=_GOAL, max_attempts=2)
            shot = result.final_report
            _attach_refinement_trail(shot, _GOAL, result)
    finally:
        vision_critique.critique = original

    assert result.satisfied is True
    assert result.stopped_reason == "satisfied"
    trail = shot.evidence["critique_trail"]
    assert len(trail) == 2, trail
    # The FIRST frame was judged by the deterministic luma critic, and the trail says so.
    assert trail[0]["critique"]["source"] == "luma_fallback"
    assert trail[0]["critique"]["verdict"] == "too bright"
    assert trail[0]["correction_applied"] is not None
    assert trail[-1]["stop_reason"] == "satisfied"


def test_vision_available_is_used_and_source_says_so(monkeypatch=None):
    """When vision answers, its critique is used and the trail's source is 'vision', not the fallback."""
    def _vision_good(*_args, **_kwargs):
        return {"matches_intent": True, "exposure": "good", "brightness_delta": 0,
                "issues": [], "suggestion": "looks great"}

    original = vision_critique.critique
    vision_critique.critique = _vision_good
    try:
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)
            cam = _JpegCamera(directory)
            result = reflexion_shoot(cam.shoot, _luma_critique_fn(directory),
                                     base_target=_GOAL, max_attempts=2)
            shot = result.final_report
            _attach_refinement_trail(shot, _GOAL, result)
    finally:
        vision_critique.critique = original

    assert result.satisfied is True
    trail = shot.evidence["critique_trail"]
    assert len(trail) == 1, trail  # vision said good on the first frame -> one shot
    assert trail[0]["critique"]["source"] == "vision"
    assert trail[0]["critique"]["verdict"] == "good"


def main() -> int:
    tests = [
        test_refine_two_attempt_trail_records_correction_and_satisfied,
        test_refine_oscillating_records_stalled,
        test_unrefined_path_records_single_disabled_attempt,
        test_trail_is_json_serializable_and_leaks_no_absolute_paths,
        test_vision_unavailable_falls_back_to_luma_and_refines,
        test_vision_available_is_used_and_source_says_so,
    ]
    failures: list[str] = []
    for test in tests:
        try:
            test()
        except AssertionError as exc:
            failures.append(f"{test.__name__}: {exc}")
    if failures:
        print("CRITIQUE TRAIL TESTS: FAIL")
        for failure in failures:
            print("  " + failure)
        return 1
    print(f"CRITIQUE TRAIL TESTS: PASS ({len(tests)}/{len(tests)})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
