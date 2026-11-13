"""Cross-boundary intent, evidence and live-capability checks (no camera/network)."""
from pathlib import Path
import json
import sys
from unittest.mock import patch

import numpy as np
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))
import camera_caps
import constraint_cards
import intent_brain
import motion_texture


def test_explicit_variant_matches_the_advertised_recipe_even_with_conflicting_text():
    with patch.object(intent_brain, "_ask_ollama", side_effect=AssertionError("must not call model")):
        for advertised in intent_brain.variants("freeze a runner")["variants"]:
            plan = intent_brain.interpret("bright warm photo", advertised["id"])
            assert plan["target_brightness"] == advertised["settings"]["brightness_target"]
            assert (plan["white_balance"] or "auto") == advertised["settings"]["white_balance"]
            assert plan["variant_id"] == advertised["id"]
            assert plan["source"] == "variant"
            assert plan["outcome_verified"] is False


def test_unknown_variant_does_not_silently_change_the_requested_look():
    with pytest.raises(ValueError, match="Unknown mood variant"):
        intent_brain.interpret("portrait", "not_a_recipe")


def test_motion_and_bokeh_are_requests_not_verified_results():
    freeze = intent_brain.preview("freeze my kid running")
    blur = intent_brain.preview("blur the background")
    assert freeze["motion_intent"] == "freeze"
    assert "froze" not in freeze["feel"]
    assert "blurred" not in blur["feel"]
    generated = intent_brain._coerce({"feel": "froze the motion", "why": "blurred the background"}, "freeze a runner")
    assert "froze" not in generated["feel"]
    assert "blurred" not in generated["why"]
    assert generated["outcome_verified"] is False


@pytest.mark.parametrize("target", [float("nan"), float("inf"), float("-inf")])
def test_nonfinite_model_target_falls_back_to_a_finite_exposure(target):
    plan = intent_brain._coerce({"target_brightness": target}, "cool blue")
    assert plan["target_brightness"] == 108
    json.dumps(plan, allow_nan=False)


def test_card_wire_schema_preserves_computed_status_and_unknown_evidence():
    card = constraint_cards.ConstraintCard("ISO", "100", "unknown", [
        constraint_cards.SubFactor("Noise", "uncalibrated", "pending")])
    rec = constraint_cards.Recommendation(100, 8, .01, [card])
    encoded = json.loads(json.dumps(rec.to_dict()))
    assert encoded["cards"][0]["status"] == "pending"
    assert encoded["cards"][0]["outcome_verified"] is False
    assert encoded["status"] == "pending"
    card.sub_factors.append(constraint_cards.SubFactor("Limit", "above ceiling", "limited"))
    assert rec.to_dict()["status"] == "limited"


def test_live_camera_ladders_are_model_specific_and_preserve_labels():
    caps = camera_caps.from_live_choices("Sony Alpha-A7 III (PC Control)",
        ["Auto ISO", "100", "200"], ["f/4", "f/8", "unknown"],
        ["1/100", "1/50", "65535/65535", "Bulb"])
    assert caps.isos == [100, 200]
    assert caps.snap_aperture(7.9) == (8, "f/8")
    assert caps.snap_shutter(.011) == (.01, "1/100")
    assert caps.snap_iso(3200) == (200, "200")
    profile = camera_caps.model_profile(caps.name)
    assert profile["calibration_status"] == "uncalibrated"
    assert profile["noise_model"] is None
    with pytest.raises(ValueError, match="Live numeric"):
        camera_caps.from_live_choices(caps.name, ["100"], ["unknown"], ["1/100"])


def test_motion_helper_uses_elapsed_time_subject_region_and_output_scale():
    frames = np.zeros((32, 32), dtype=np.uint8)
    flow = np.zeros((32, 32, 2), dtype=np.float32)
    flow[:, :, 0] = 8
    flow[8:24, 8:24, 0] = 2
    with patch.object(motion_texture, "optical_flow_field", return_value=flow):
        result = motion_texture.analyze_frame_pair(frames, frames, .1,
            subject_box=(8, 8, 16, 16), output_width_px=320)
    assert result["subject_motion_floor_s"] == pytest.approx(.005)
    assert result["global_motion_floor_s"] == pytest.approx(.00125)
    assert result["output_scale"] == 10
    assert result["outcome_verified"] is False
    json.dumps(result, allow_nan=False)


@pytest.mark.parametrize("interval", [0, -1, 3, float("nan"), float("inf")])
def test_stale_or_invalid_motion_samples_are_rejected(interval):
    frame = np.zeros((32, 32), dtype=np.uint8)
    with pytest.raises(ValueError, match="interval"):
        motion_texture.analyze_frame_pair(frame, frame, interval)


def _live_a7iii_caps():
    return camera_caps.from_live_choices("Sony Alpha-A7 III (PC Control)",
        ["100", "200", "400", "800", "1600", "3200", "6400", "12800"],
        ["2.8", "4", "5.6", "8", "11", "16", "22"],
        ["1/8000", "1/2000", "1/1000", "1/500", "1/250", "1/125", "1/60",
         "1/30", "1/15", "1/8", "1/4", "1/2", "1", "2", "4", "8", "15", "30"])


def test_live_recommendation_respects_body_ladders_policy_and_measured_motion():
    import brain
    import physics
    caps = _live_a7iii_caps()
    with patch.object(brain.embed_index, "recommend", return_value={"f_number": 8}):
        rec = brain.recommend_live("unused.jpg", 10, caps.name, caps, 4, 50, 1/500)
    assert rec.iso in caps.isos and rec.iso <= 6400
    assert rec.aperture in caps.apertures and rec.aperture <= 21.5
    assert rec.shutter_s in caps.shutters and rec.shutter_s <= 1/500
    ev = physics.ev100(rec.aperture, rec.shutter_s) - np.log2(rec.iso / 100)
    assert abs(ev - 10) <= .34
    assert any("uncalibrated" in note.lower() for note in rec.notes)
    assert rec.card("ISO").sub_factors[0].status == "pending"
    assert rec.to_dict()["outcome_verified"] is False


def test_live_recommendation_missing_taste_is_explicit_and_keeps_current_preference():
    import brain
    caps = _live_a7iii_caps()
    with patch.object(brain.embed_index, "recommend", side_effect=FileNotFoundError):
        rec = brain.recommend_live("unused.jpg", 13, caps.name, caps, 8, None)
    assert rec.aperture == 8
    assert rec.card("APERTURE").sub_factors[0].status == "pending"
    assert any("assumed 50mm" in note for note in rec.notes)
    assert any("Taste unavailable" in note for note in rec.notes)


def test_live_recommendation_flags_unachievable_dark_scene_and_rejects_wrong_body():
    import brain
    caps = _live_a7iii_caps()
    with patch.object(brain.embed_index, "recommend", return_value={"f_number": 8}):
        rec = brain.recommend_live("unused.jpg", -15, caps.name, caps, 8, 50, 1/500)
    assert rec.iso <= 6400
    assert rec.card("SHUTTER").status == "limited"
    assert rec.card("ISO").status == "limited"
    with pytest.raises(ValueError, match="matching body"):
        brain.recommend_live("unused.jpg", 10, "different camera", caps, 8, 50)
