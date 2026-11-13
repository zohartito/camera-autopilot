"""A "light trails" request resolves to ONE long exposure chosen from the metered scene.

Live on 2026-09-22 "light trails of the room at night" on the A7 II shot 1/25 s at ISO 6400
(artifacts/wave0-receipts-20260922/subject_run_a7ii.txt): `motion_intent` "blur" only removed the
shutter floor and USB capture was capped at 1 s. These tests pin the camera-free half of the fix:
the intent resolves to the long single-exposure route, and the planner picks a shutter above 1 s
from the body's own ladder at base ISO, stopping the aperture down to keep the exposure right, or
says an ND filter is needed. No camera, network or model is used.
"""
from __future__ import annotations

import math
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "backend"))

import camera_caps
import intent
import intent_brain
import long_single_exposure as lse
from sonyremote.camera import SONY_LONG_EXPOSURE_LADDER_S, SONY_LONG_EXPOSURE_MAX_SHUTTER_S

A7II = "Sony ILCE-7M2 (Control)"
A7III = "Sony Alpha-A7 III (PC Control)"
# The A7 III driver writes long shutters as decimals, the A7 II as tenths (camera_caps).
A7III_SHUTTERS = ["30", "25", "20", "15", "13", "10", "8", "6", "5", "4", "3.2", "2.5", "2", "1.6",
                  "1.3", "1", "0.8", "0.6", "0.5", "0.4", "1/3", "1/4", "1/8", "1/15", "1/30", "1/60",
                  "1/125", "1/250", "1/500", "1/1000", "1/8000", "Bulb"]
TAMRON = [2.8, 3.2, 3.5, 4.0, 4.5, 5.0, 5.6, 6.3, 7.1, 8.0, 9.0, 10.0, 11.0, 13.0, 14.0, 16.0,
          18.0, 20.0, 22.0]


def _caps(model=A7II, apertures=TAMRON, shutters=None):
    shutters = shutters or camera_caps._A7II_SHUTTER_LABELS
    return camera_caps.from_live_choices(model, [str(v) for v in camera_caps._A7II_ISO],
                                         [f"{a:g}" for a in apertures], shutters)


def _exact_shutter(ev100, f_number, iso):
    return f_number * f_number * 100 / (2 ** ev100 * iso)


# --- intent ---------------------------------------------------------------------------------------

@pytest.mark.parametrize("phrase, subject", [
    ("light trails of the room at night", "light_trails"),
    ("silky water on the river", "silky_water"),
    ("a long exposure of the street", "long_exposure"),
    ("motion blur of moving lights", "motion_blur"),
])
def test_long_exposure_requests_resolve_to_the_long_single_exposure_route(phrase, subject):
    assert intent.long_exposure_subject_from_intent(phrase) == subject
    assert intent.motion_from_intent(phrase) == "blur"
    plan = intent_brain.preview(phrase)
    assert plan["exposure_route"] == "long_single_exposure"
    assert plan["long_exposure_subject"] == subject


def test_moving_lights_asking_for_blur_is_not_read_as_freeze():
    # Before: "moving" was checked first, so a blur request for moving lights got a 1/500 floor.
    assert intent.motion_from_intent("motion blur of moving lights") == "blur"


@pytest.mark.parametrize("phrase", ["freeze my kid running around", "a natural photo", None, ""])
def test_other_requests_stay_on_the_ordinary_route(phrase):
    assert intent.long_exposure_subject_from_intent(phrase) is None
    plan = intent_brain.preview(phrase)
    assert plan["exposure_route"] == "ordinary"
    assert plan["long_exposure_subject"] is None
    if phrase and "freeze" in phrase:
        assert plan["motion_intent"] == "freeze"


def test_light_in_light_trails_names_the_subject_not_a_bright_look():
    # Before: "light" matched the bright/airy keywords, aiming the night trails 0.7 EV brighter.
    assert intent.target_from_intent("light trails of the room at night") == intent.NEUTRAL_TARGET
    assert intent.target_from_intent("blur the lights on the street") == intent.NEUTRAL_TARGET
    assert intent.target_from_intent("light and airy") == 140.0
    assert intent.target_from_intent("moody light trails") == 90.0
    assert intent.feel_from_intent("light trails of the room at night") == "planned one long exposure for the movement"
    assert intent.feel_from_intent("light and airy") == "kept it bright and clean"


def test_a_variant_keeps_the_users_long_exposure_request():
    plan = intent_brain.interpret("light trails of the room at night", variant_id="natural")
    assert plan["exposure_route"] == "long_single_exposure"
    assert plan["long_exposure_subject"] == "light_trails"


# --- planner --------------------------------------------------------------------------------------

def test_light_trails_in_a_dark_room_pick_a_long_base_iso_exposure_at_f8():
    plan, cards = lse.plan_long_single_exposure(3.0, _caps(), subject="light_trails")
    assert plan["status"] == "ok"
    assert plan["shutter_s"] > 1.0 and plan["shutter_label"] == "8"
    assert plan["aperture"] == 8.0 and plan["iso"] == 100 and plan["iso_raised"] is False
    assert abs(plan["exposure_error_ev"]) <= 1 / 3
    assert [card.axis for card in cards] == ["SHUTTER", "APERTURE", "ISO"]
    shutter_card = cards[0].to_dict()
    assert "tripod" in " ".join(f["readout"] for f in shutter_card["sub_factors"]).lower()
    assert shutter_card["outcome_verified"] is False


def test_a_brighter_scene_stops_down_to_keep_the_trail_long_and_the_exposure_right():
    plan, cards = lse.plan_long_single_exposure(6.0, _caps(), subject="light_trails")
    assert plan["status"] == "ok"
    assert plan["aperture"] == 16.0 and plan["shutter_label"] == "4" and plan["iso"] == 100
    assert abs(math.log2(plan["shutter_s"] / _exact_shutter(6.0, 16.0, 100))) <= 1 / 3
    assert "f/16" in cards[1].reason


def test_every_plan_uses_a_ladder_shutter_above_one_second_and_at_most_the_ceiling():
    for ev in [x / 2 for x in range(-6, 17)]:
        plan, _ = lse.plan_long_single_exposure(ev, _caps(A7III, shutters=A7III_SHUTTERS), subject="light_trails")
        if plan["status"] != "ok":
            continue
        assert 1.0 < plan["shutter_s"] <= SONY_LONG_EXPOSURE_MAX_SHUTTER_S == 30.0
        assert any(abs(plan["shutter_s"] - stop) / stop < .02 for stop in SONY_LONG_EXPOSURE_LADDER_S)
        assert plan["shutter_label"] in A7III_SHUTTERS


def test_a_very_dark_scene_caps_at_30s_and_raises_iso_with_an_explanation():
    plan, cards = lse.plan_long_single_exposure(-3.0, _caps(), subject="light_trails")
    assert plan["status"] == "ok"
    assert plan["shutter_label"] == "30" and plan["aperture"] == 2.8
    assert plan["iso_raised"] is True and plan["iso"] == 200
    iso_card = cards[2].to_dict()
    assert iso_card["status"] == "warning" and "30" in iso_card["reason"]


def test_a_scene_darker_than_the_iso_ceiling_reports_the_underexposure():
    plan, cards = lse.plan_long_single_exposure(-12.0, _caps(), subject="light_trails", iso_ceiling=6400)
    assert plan["status"] == "ok" and plan["iso"] == 6400 and plan["shutter_label"] == "30"
    assert plan["underexposure_ev"] > 1
    assert any("underexposed" in note for note in plan["notes"])


def test_too_bright_even_at_the_smallest_aperture_says_nd_and_never_fakes_it():
    plan, cards = lse.plan_long_single_exposure(12.0, _caps(), subject="light_trails")
    assert plan["status"] == "needs_nd"
    assert plan["shutter_label"] is None and plan["aperture_label"] is None and plan["iso_label"] is None
    assert plan["nd"]["stops_needed"] > 5 and plan["nd"]["suggested_filter"].startswith("ND64")
    assert "ND" in plan["reason"] and "f/22" in plan["reason"]
    assert cards[0].to_dict()["status"] == "limited"


def test_an_unprofiled_lens_is_never_stopped_down_past_the_confirmed_aperture():
    # constrain_mounted_lens leaves f/2.8..f/5.6 when f/5.6 is the confirmed, unprofiled aperture.
    caps = _caps(A7III, apertures=[2.8, 4.0, 5.6], shutters=A7III_SHUTTERS)
    plan, _ = lse.plan_long_single_exposure(9.0, caps, subject="light_trails", stop_down_verified=False)
    assert plan["status"] == "needs_nd"
    assert "smaller aperture" in plan["reason"] and "f/5.6" in plan["reason"]


def test_live_caps_without_long_shutters_are_refused():
    caps = _caps(shutters=["1", "1/2", "1/60"])
    with pytest.raises(ValueError, match="no live shutter"):
        lse.plan_long_single_exposure(3.0, caps, subject="light_trails")


@pytest.mark.parametrize("bad", [float("nan"), float("inf"), 40.0])
def test_non_physical_scene_values_are_refused(bad):
    with pytest.raises(ValueError):
        lse.plan_long_single_exposure(bad, _caps(), subject="light_trails")


def test_a_plan_is_json_safe_and_builds_a_settable_recommendation():
    import json
    plan, cards = lse.plan_long_single_exposure(3.0, _caps(), subject="light_trails")
    json.dumps(plan, allow_nan=False)
    rec = lse.as_recommendation(plan, cards)
    assert (rec.iso_label, rec.aperture_label, rec.shutter_label) == ("100", "8", "8")


def test_a_sparse_ladder_never_returns_a_badly_exposed_ok_plan():
    # Codex: EV 3, only f/8 and 1.3"/30" used to pick 30 s at +1.91 EV with status "ok".
    caps = _caps(apertures=[8.0], shutters=["30", "13/10", "1/60"])
    plan, _ = lse.plan_long_single_exposure(3.0, caps, subject="light_trails")
    assert plan["status"] == "ok" and abs(plan["exposure_error_ev"]) <= 1 / 3   # 1.3 s at ISO 640
    plan, cards = lse.plan_long_single_exposure(3.0, caps, subject="light_trails", iso_ceiling=100)
    assert plan["status"] == "no_settable_exposure"
    assert plan["shutter_label"] is None and plan["iso_label"] is None
    assert cards[0].to_dict()["status"] == "limited"


def test_a_ladder_gap_may_be_closed_by_raising_iso_within_tolerance():
    # f/8 only, 30" or 1.3": at EV 1 the exact time is 32 s -> 30 s at base ISO (-0.09 EV) is fine;
    # at EV 0.5 (45 s) ISO 160 at 30 s lands within a third of a stop.
    caps = _caps(apertures=[8.0], shutters=["30", "13/10"])
    plan, _ = lse.plan_long_single_exposure(0.5, caps, subject="light_trails")
    assert plan["status"] == "ok" and plan["shutter_label"] == "30"
    assert abs(plan["exposure_error_ev"]) <= 1 / 3


SMOOTHED_WATER = ["make the water silky", "silky flowing water", "silky river", "silky water",
                  "a silky waterfall", "smooth water at the lake", "milky waves on the beach",
                  "smooth out the ocean", "a silky creek in the forest", "silky falls", "smooth the tide",
                  "make the water look soft and silky", "smooth out the choppy sea", "silky surf",
                  "milky rapids", "smooth ripples"]
NOT_WATER = ["silky skin", "silky hair", "a silky dress", "silky fabric", "silky portrait skin",
             "a silky smooth dress", "silky hair in the wind", "smooth skin by the lake",
             "silky hair at the sea", "a smooth jazz vibe", "silky skin by water", "silky hair in waves",
             "smooth fabric beside water", "make the water look like silky hair"]


@pytest.mark.parametrize("phrase, expected", [(p, "silky_water") for p in SMOOTHED_WATER]
                         + [(p, None) for p in NOT_WATER])
def test_silky_smooth_or_milky_selects_a_long_exposure_only_near_water(phrase, expected):
    # Review (PR #81): bare "silky" sent portraits to a long exposure; the first fix dropped
    # "make the water silky" too. Now a silky/smooth/milky word counts only within a few words of water.
    assert intent.long_exposure_subject_from_intent(phrase) == expected
    route = intent_brain.preview(phrase)["exposure_route"]
    assert route == ("long_single_exposure" if expected else "ordinary")


NEGATED = ["freeze action with no motion blur", "avoid a long exposure; keep it sharp",
           "no light trails please", "don't make the water silky", "without any motion blur",
           "not a long exposure", "I can't use a long exposure", "I cant use a long exposure",
           "cannot use a slow shutter", "we can not do light trails", "I can’t use a long exposure"]
AFFIRMED = [("not too bright, light trails please", "light_trails"),
            ("light trails, no people in the frame", "light_trails"),
            ("no flash, silky water", "silky_water"),
            ("a long exposure, not too dark", "long_exposure")]


@pytest.mark.parametrize("phrase", NEGATED)
def test_a_negated_long_exposure_phrase_never_selects_the_long_route(phrase):
    # Review (PR #81): "freeze action with no motion blur" selected a seconds-long exposure and
    # overrode the freeze request with blur.
    assert intent.long_exposure_subject_from_intent(phrase) is None
    assert intent_brain.preview(phrase)["exposure_route"] == "ordinary"
    assert intent.motion_from_intent(phrase) != "blur"
    if "freeze" in phrase:
        assert intent.motion_from_intent(phrase) == "freeze"


@pytest.mark.parametrize("phrase, subject", AFFIRMED)
def test_a_negation_in_another_clause_does_not_cancel_the_request(phrase, subject):
    assert intent.long_exposure_subject_from_intent(phrase) == subject


@pytest.mark.parametrize("phrase", ["a portrait in a light trailer", "flight trails at an airshow",
                                    "preserve the highlight streaks in her hair", "a long exposureless snap"])
def test_long_exposure_phrases_match_whole_words_only(phrase):
    # Review (PR #81): substring matching sent "flight trails" and "highlight streaks" to light_trails.
    assert intent.long_exposure_subject_from_intent(phrase) is None
    assert intent_brain.preview(phrase)["exposure_route"] == "ordinary"


@pytest.mark.parametrize("phrase, subject", [("light trails", "light_trails"), ("a light trail", "light_trails"),
                                             ("light-trails on the bridge", "light_trails"),
                                             ("two long exposures", "long_exposure")])
def test_whole_phrases_and_plurals_still_match(phrase, subject):
    assert intent.long_exposure_subject_from_intent(phrase) == subject


def test_the_brightness_map_keeps_highlight_when_it_is_not_a_subject_phrase():
    assert intent.target_from_intent("bright highlight streaks") == 140.0


TRAILING_NEGATED = ["light trails are not desired", "motion blur is not wanted", "smooth water without a long exposure",
                    "light trails aren't needed", "silky water, but no long exposure", "a long exposure is not required",
                    "light trails? not wanted", "light trails are never wanted"]
TRAILING_AFFIRMED = [("I want light trails, not a sharp car", "light_trails"),
                     ("light trails not headlight glare", "light_trails"),
                     ("motion blur is wanted", "motion_blur"),
                     ("light trails with no people", "light_trails"),
                     ("a long exposure not too dark", "long_exposure"),
                     ("no flash but light trails please", "light_trails")]


@pytest.mark.parametrize("phrase", TRAILING_NEGATED)
def test_a_trailing_or_declined_technique_never_selects_the_long_route(phrase):
    # Review (PR #81): negations after the phrase ("light trails are not desired") and a declined
    # technique ("smooth water without a long exposure") still selected a seconds-long exposure.
    assert intent.long_exposure_subject_from_intent(phrase) is None
    assert intent_brain.preview(phrase)["exposure_route"] == "ordinary"


@pytest.mark.parametrize("phrase, subject", TRAILING_AFFIRMED)
def test_a_trailing_not_about_something_else_keeps_the_request(phrase, subject):
    assert intent.long_exposure_subject_from_intent(phrase) == subject


SCOPED_NEGATED = ["smooth water is not wanted", "silky river? not wanted", "make the water silky, not wanted",
                  "light trails? not wanted", "light trails are not desired"]
SCOPED_AFFIRMED = [("light trails, portraits are not wanted", "light_trails"),
                   ("light trails. Glare is not desired", "light_trails"),
                   ("silky river, people are not wanted", "silky_water"),
                   ("smooth water. Crowds are not desired", "silky_water")]


@pytest.mark.parametrize("phrase", SCOPED_NEGATED)
def test_a_verdict_in_the_same_clause_or_opening_the_next_declines(phrase):
    # Review (PR #81): the silky-water fallback ignored trailing verdicts ("smooth water is not wanted").
    assert intent.long_exposure_subject_from_intent(phrase) is None


@pytest.mark.parametrize("phrase, subject", SCOPED_AFFIRMED)
def test_a_verdict_about_another_subject_in_a_later_clause_keeps_the_request(phrase, subject):
    # Review (PR #81): the verdict search ran into later clauses about something else.
    assert intent.long_exposure_subject_from_intent(phrase) == subject


REMOVAL_NEGATED = ["remove motion blur", "eliminate motion blur", "reduce motion blur", "minimize motion blur",
                   "minimise motion blur", "avoid motion blur", "get rid of motion blur", "no more motion blur",
                   "remove the light trails", "reduce light trails", "eliminate the long exposure look",
                   "avoid a slow shutter", "get rid of the slow shutter blur", "removing motion blur from the kids"]
REMOVAL_AFFIRMED = [("add motion blur", "motion_blur"),
                    ("I want motion blur on the waterfall", "motion_blur"),
                    ("light trails please, remove the people", "light_trails")]


@pytest.mark.parametrize("phrase", REMOVAL_NEGATED)
def test_asking_to_remove_or_reduce_blur_never_selects_the_long_route(phrase):
    # Review (PR #81): "remove motion blur" resolved to motion_blur and could fire a 1.3-30 s exposure,
    # the inverse of the request.
    assert intent.long_exposure_subject_from_intent(phrase) is None
    assert intent.motion_from_intent(phrase) != "blur"
    assert intent_brain.preview(phrase)["exposure_route"] == "ordinary"


@pytest.mark.parametrize("phrase, subject", REMOVAL_AFFIRMED)
def test_adding_or_wanting_blur_still_selects_it(phrase, subject):
    assert intent.long_exposure_subject_from_intent(phrase) == subject


def _raw_caps():
    return camera_caps.from_live_choices(
        "Sony Alpha-A7 III (PC Control)", ["100", "200"],
        ["f/2.8", "f/4", "f/5.6", "f/8", "f/11", "f/16", "f/22", "f/32"], ["30", "1/60"])


def test_electronic_stop_down_offers_the_listed_apertures_up_to_f16_pending_readback():
    raw = _raw_caps()
    _, limits = camera_caps.constrain_mounted_lens(raw, "f/4", lens_specification=(24, 105, 4, 4))
    caps, evidence = camera_caps.electronic_stop_down(raw, limits, lens_model="FE 24-105mm F4 G OSS")
    assert caps.apertures == [4.0, 5.6, 8.0, 11.0, 16.0]
    assert caps.aperture_labels == ["f/4", "f/5.6", "f/8", "f/11", "f/16"]
    assert evidence["status"] == "pending_readback"


@pytest.mark.parametrize("lens_model, spec, reason", [
    ("", (24, 105, 4, 4), "names no lens"),
    ("FE 24-105mm F4 G OSS", None, "metadata"),
    ("FE 24-105mm F4 G OSS", (24, 105, 5.6, 5.6), "metadata"),   # disagrees with the f/4 readback
])
def test_electronic_stop_down_is_not_planned_without_electronic_evidence(lens_model, spec, reason):
    raw = _raw_caps()
    _, limits = camera_caps.constrain_mounted_lens(raw, "f/4", lens_specification=spec)
    caps, evidence = camera_caps.electronic_stop_down(raw, limits, lens_model=lens_model)
    assert caps is None
    assert evidence["status"] == "not_shown"
    assert reason in evidence["reason"]


def test_electronic_stop_down_is_not_needed_under_a_verified_profile():
    raw = _raw_caps()
    _, limits = camera_caps.constrain_mounted_lens(raw, "f/4", lens_specification=(28, 75, 2.8, 2.8),
                                                   verified_profile=(2.8, 22.0))
    caps, evidence = camera_caps.electronic_stop_down(raw, limits, lens_model="Tamron 28-75 f/2.8")
    assert caps is None and evidence["status"] == "not_needed"


def test_a_pending_stop_down_nd_card_says_it_was_never_read_back():
    raw = _raw_caps()
    _, limits = camera_caps.constrain_mounted_lens(raw, "f/4", lens_specification=(24, 105, 4, 4))
    caps, evidence = camera_caps.electronic_stop_down(raw, limits, lens_model="FE 24-105mm F4 G OSS")
    plan, _ = lse.plan_long_single_exposure(14.0, caps, subject="light_trails", stop_down_verified=False,
                                            stop_down=evidence)
    assert plan["status"] == "needs_nd"
    assert plan["nd"]["at_aperture"] == 16.0
    assert plan["stop_down"]["status"] == "pending_readback"
    assert "never read back" in str(plan["cards"][1])
