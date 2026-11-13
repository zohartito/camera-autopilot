"""A saturated or black meter frame must be re-metered, not trusted.

Regression for the 2026-09-08 field defect: a body left at ISO 6400 / f/2.8 / 1/15 metered a
daylight room at 98.5% clipped, the pinned mean under-reported the error by ~6 stops, and the
recommendation blew the photograph out.
"""
import sys
from pathlib import Path

import numpy as np
import pytest
from PIL import Image

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))
import camera_caps  # noqa: E402
import capture_policy  # noqa: E402
import live_verify  # noqa: E402
import remeter  # noqa: E402


def _a7ii_caps():
    return camera_caps.from_live_choices(
        "Sony ILCE-7M2 (Control)",
        ["100", "200", "400", "800", "1600", "3200", "6400"],
        ["f/2.8", "f/4", "f/5.6", "f/8"],
        ["1/1000", "1/500", "1/250", "1/125", "1/60", "1/30", "1/15", "1/8"])


def test_pinned_frames_take_the_decisive_step_not_the_measured_one():
    # The field frame: mean 254.59. Measured difference is only ~1.2 stops; the real error was ~6.
    assert remeter.correction_stops(254.59) == -remeter.DECISIVE_STEP_STOPS
    assert remeter.correction_stops(1.0) == remeter.DECISIVE_STEP_STOPS
    assert remeter.correction_stops(0.0) == remeter.DECISIVE_STEP_STOPS


def test_readable_frames_take_the_measured_difference():
    assert remeter.correction_stops(56.0, target=112.0) == pytest.approx(1.0)
    assert remeter.correction_stops(224.0, target=112.0) == pytest.approx(-1.0)
    assert abs(remeter.correction_stops(120.0, target=112.0)) < 0.2


def test_correction_spends_iso_first_and_holds_the_aperture():
    caps = _a7ii_caps()
    plan = remeter.corrected_exposure(caps, iso=6400, f_number=2.8, shutter_s=1 / 15, stops=-4.0)
    assert plan is not None
    assert plan["iso"] == "400"                      # 4 stops of ISO alone covers the correction
    assert plan["shutter"] == "1/15"                 # shutter untouched
    assert plan["aperture_held"] == "f/2.8"
    assert plan["achieved_stops"] == pytest.approx(-4.0, abs=0.1)


def test_correction_spills_onto_the_shutter_when_iso_runs_out():
    caps = _a7ii_caps()
    plan = remeter.corrected_exposure(caps, iso=400, f_number=5.6, shutter_s=1 / 15, stops=-4.0)
    assert plan["iso"] == "100"                      # only 2 stops of ISO headroom left
    assert plan["shutter"] == "1/60"                 # the remaining 2 stops come from the shutter
    assert plan["achieved_stops"] == pytest.approx(-4.0, abs=0.1)


def test_no_correction_when_the_ladders_are_already_at_their_end():
    caps = _a7ii_caps()
    assert remeter.corrected_exposure(caps, iso=100, f_number=5.6, shutter_s=1 / 1000, stops=-4.0) is None
    assert remeter.corrected_exposure(caps, iso=800, f_number=5.6, shutter_s=1 / 60, stops=0.1) is None


def test_budget_declares_the_bounded_extra_meter_frames():
    budget = capture_policy.exposure_budget({"mode": "off", "frames": 1}, refine=False)
    assert budget["extra_meter_exposures"] == remeter.MAX_REMETER_PASSES
    assert budget["max_physical_exposures"] == 2 + remeter.MAX_REMETER_PASSES
    fixed = capture_policy.exposure_budget({"mode": "fixed", "frames": 3}, refine=False)
    assert fixed["extra_meter_exposures"] == remeter.MAX_REMETER_PASSES   # Fixed meters first
    kept = capture_policy.exposure_budget({"mode": "fixed", "frames": 3, "baseline": "current"})
    assert kept["extra_meter_exposures"] == 0    # keeping the body's exposure takes no meter frame


def _frame(path: Path, value: int) -> str:
    Image.fromarray(np.full((48, 64, 3), value, dtype=np.uint8)).save(path, quality=92)
    return str(path)


def test_reliability_matches_the_frames_the_loop_reacts_to(tmp_path):
    assert live_verify.meter_reliability(_frame(tmp_path / "white.jpg", 255))["status"] == "low"
    assert live_verify.meter_reliability(_frame(tmp_path / "black.jpg", 1))["status"] == "low"
    assert live_verify.meter_reliability(_frame(tmp_path / "mid.jpg", 118))["status"] == "ok"


from test_final_wave_engine import rig  # noqa: E402,F401  (fake-camera engine fixture)


def test_engine_remeters_a_saturated_frame_and_recovers(rig):
    """The field case: the first meter frame is pinned white, the corrected one is readable."""
    rig.camera.frame_values = [255, 112]
    shot = rig.real.shoot(None, variant_id="natural")
    passes = shot.evidence["meter_passes"]
    assert [p["pass"] for p in passes] == [0, 1]
    assert passes[0]["reliability"]["status"] == "low"
    assert passes[1]["reliability"]["status"] == "ok"
    assert passes[1]["correction"]["achieved_stops"] < 0        # corrected by exposing less
    assert shot.evidence["meter_reliability"]["status"] == "ok"
    assert shot.evidence["physical_exposure_count"] == 3        # two meter frames plus the photo
    assert "corrected metering pass" in shot.summary


def test_engine_does_not_remeter_a_readable_frame(rig):
    shot = rig.real.shoot(None, variant_id="natural")
    assert len(shot.evidence["meter_passes"]) == 1
    assert shot.evidence["physical_exposure_count"] == 2        # unchanged ordinary budget
    assert rig.camera.capture_count == 2


def test_engine_stops_when_the_ladders_cannot_correct_further(rig):
    """A body already at the end of its ladders must not burn shutters on identical frames."""
    rig.camera.frame_values = [255]                             # every frame stays pinned
    shot = rig.real.shoot(None, variant_id="natural")
    passes = shot.evidence["meter_passes"]
    assert len(passes) <= remeter.MAX_REMETER_PASSES + 1
    assert passes[-1].get("stopped") or len(passes) == remeter.MAX_REMETER_PASSES + 1
    assert shot.evidence["meter_reliability"]["status"] == "low"
    assert "Refine" in shot.summary                             # still honest about the limit
