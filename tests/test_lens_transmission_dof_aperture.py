"""Lens transmission (CAP #4) and the distance/depth aperture plan (B04). Camera-free: synthetic
lenses, synthetic distances and the shared fake-camera engine fixture; nothing opens USB."""
from __future__ import annotations

import math
import sys
from dataclasses import dataclass
from pathlib import Path
from unittest.mock import patch

import pytest
from PIL import Image

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "backend"))

import aperture_optimizer
import brain
import camera_caps
import constraint_cards
import lens_optics
import night_assist
import physics
import rig_profile
import sensor_noise
import vision_banana
from test_final_wave_engine import rig  # noqa: F401  shared fake-camera engine fixture

SCENE = physics.EV_SCENE_LUMINANCE
TTL = physics.EV_THROUGH_THIS_LENS
TAMRON = lens_optics.TAMRON_28_75_OPTICS
A7II_APERTURES = camera_caps.A7II_CAPS.apertures


def synthetic_transmission(offset_stops: float, f_number: float = 4.0) -> lens_optics.LensTransmission:
    """A made-up lens whose glass loses exactly `offset_stops`: T = f * 2**(offset/2)."""
    return lens_optics.LensTransmission(
        reported_t_stop=f_number * 2 ** (offset_stops / 2), reported_f_number=f_number,
        source_url="https://example.test/synthetic-lens", source_date="2026-09-27",
        measured_on="synthetic")


def _a7ii_rec(scene_ev: float, *, taste_f: float = 4.0, **kwargs):
    # Depth math tests supply the EXIF focus proof (focus on the subject) unless they test its absence.
    if kwargs.get("subject_distance_m") is not None:
        kwargs.setdefault("focus_distance_m", kwargs["subject_distance_m"])
    with patch.object(brain.embed_index, "recommend", return_value={"f_number": taste_f}):
        return brain.recommend_settings("unused.jpg", scene_ev, focal_mm=50, **kwargs)


# EV at which f/4, ISO 100, 1/250 s is exactly correct: log2(16 * 250).
EV_F4_ISO100_1_250 = math.log2(4.0 * 4.0 * 250)


# --- lens transmission --------------------------------------------------------------------------
def test_scene_luminance_exposure_moves_by_exactly_the_published_offset():
    """Reverting lens_adjusted_ev100 (or its use in recommend_settings) leaves the shutter at 1/250."""
    assert physics.lens_adjusted_ev100(10.0, ev_reference=SCENE, t_stop_offset_stops=0.5) == 9.5
    one_stop = synthetic_transmission(1.0)
    third = synthetic_transmission(1 / 3)
    assert one_stop.t_stop(4.0) == pytest.approx(4.0 * math.sqrt(2))
    base = _a7ii_rec(EV_F4_ISO100_1_250, ev_reference=SCENE)          # Tamron: unknown
    dimmer = _a7ii_rec(EV_F4_ISO100_1_250, ev_reference=SCENE, transmission=one_stop)
    slight = _a7ii_rec(EV_F4_ISO100_1_250, ev_reference=SCENE, transmission=third)
    assert (base.iso, base.aperture, base.shutter_s) == (100, 4.0, pytest.approx(1 / 250))
    assert (dimmer.iso, dimmer.aperture, dimmer.shutter_s) == (100, 4.0, pytest.approx(1 / 125))
    assert (slight.iso, slight.aperture) == (100, 4.0) and slight.shutter_label == "1/200"
    assert dimmer.exposure_reference["target_ev100"] == pytest.approx(EV_F4_ISO100_1_250 - 1.0, abs=1e-4)
    assert dimmer.exposure_reference["applied_offset_stops"] == pytest.approx(1.0, abs=1e-4)
    assert slight.exposure_reference["applied_offset_stops"] == pytest.approx(1 / 3, abs=1e-4)


def test_unknown_transmission_changes_nothing_and_says_so():
    """Catches an invented default transmittance (the old 0.85) creeping back in."""
    unknown = _a7ii_rec(EV_F4_ISO100_1_250, ev_reference=SCENE,
                        transmission=lens_optics.UNKNOWN_TRANSMISSION)
    zero = _a7ii_rec(EV_F4_ISO100_1_250, ev_reference=SCENE, transmission=synthetic_transmission(0.0))
    assert (unknown.iso, unknown.aperture, unknown.shutter_s) == (zero.iso, zero.aperture, zero.shutter_s)
    assert unknown.exposure_reference["applied_offset_stops"] == 0
    assert unknown.exposure_reference["lens_transmission"]["status"] == "unknown"
    assert any("transmission is unmeasured" in note for note in unknown.notes)
    assert physics.lens_adjusted_ev100(7.0, ev_reference=SCENE, t_stop_offset_stops=None) == 7.0


def test_through_the_lens_meter_cancels_the_lens_transmission():
    """The meter frame already passed through the glass. Reverting to the old
    `scene_ev100 + log2(transmission)` double count brightens the A7 II final (fails here)."""
    known = _a7ii_rec(EV_F4_ISO100_1_250, transmission=synthetic_transmission(1.0))
    default = _a7ii_rec(EV_F4_ISO100_1_250)
    assert (known.iso, known.aperture, known.shutter_s) == (default.iso, default.aperture,
                                                            default.shutter_s)
    assert default.shutter_s == pytest.approx(1 / 250)
    assert known.exposure_reference["ev_reference"] == TTL
    assert known.exposure_reference["applied_offset_stops"] == 0
    with pytest.raises(ValueError):
        physics.lens_adjusted_ev100(7.0, ev_reference="guess", t_stop_offset_stops=1.0)


def test_published_fe_24_105_measurement_is_cited_and_the_tamron_stays_unknown():
    """Catches an uncited or invented T-stop on either lens."""
    fe = rig_profile.resolve_rig("Sony Alpha-A7 III (PC Control)", lens_model="FE 24-105mm F4 G OSS",
                                 lens_specification=(24, 105, 4, 4))
    assert fe.lens_transmission.known and fe.lens_transmission.status == "published_measurement"
    assert fe.lens_transmission.t_stop(4.0) == pytest.approx(4.4)
    assert fe.lens_transmission.t_stop_offset_stops == pytest.approx(2 * math.log2(1.1))
    assert "dxomark.com" in fe.lens_transmission.source_url
    assert fe.lens_transmission.source_date == "2021-01-19"
    tamron = rig_profile.resolve_rig("Sony ILCE-7M2 (Control)", lens_model="E 28-75mm F2.8-2.8",
                                     lens_specification=(28, 75, 2.8, 2.8))
    assert tamron.calibration_status == "existing_profile"
    assert not tamron.lens_transmission.known and TAMRON.t_stop(2.8) is None
    assert not rig_profile.resolve_rig("Nikon Z6", lens_model="Z 24-70").lens_transmission.known


def test_a_transmission_measurement_must_be_cited_and_physical():
    with pytest.raises(ValueError):
        lens_optics.LensTransmission(reported_t_stop=4.4, reported_f_number=4.0)
    with pytest.raises(ValueError):
        lens_optics.LensTransmission(reported_t_stop=3.5, reported_f_number=4.0,
                                     source_url="https://example.test", source_date="2026-09-27")
    with pytest.raises(ValueError):
        lens_optics.LensTransmission(reported_t_stop=4.4)


def test_aperture_card_shows_lens_transmission_as_a_sub_factor():
    """Catches the sub-factor being dropped or an unknown lens reading as OK."""
    exposure = physics.ExposureSolution(iso=100, aperture=4.0, shutter_s=1 / 250)
    fe = lens_optics.FE_24_105_F4_G_TRANSMISSION
    known = constraint_cards.build_cards(exposure, focal_mm=50, shutter_floor_s=1 / 50,
                                         transmission=fe)[1]
    factor = next(f for f in known.sub_factors if f.name == "Lens Transmission")
    assert factor.status == constraint_cards.OK and "T/4.4" in factor.readout
    assert "already in the through-lens meter" in factor.readout
    scene = constraint_cards.build_cards(exposure, focal_mm=50, shutter_floor_s=1 / 50,
                                         transmission=fe, ev_reference=SCENE)[1]
    assert "exposure raised 0.28 EV" in next(
        f for f in scene.sub_factors if f.name == "Lens Transmission").readout
    unknown = constraint_cards.build_cards(exposure, focal_mm=50, shutter_floor_s=1 / 50)[1]
    factor = next(f for f in unknown.sub_factors if f.name == "Lens Transmission")
    assert factor.status == constraint_cards.PENDING and "unknown" in factor.readout
    assert "T/" not in next(f for f in unknown.sub_factors if f.name == "Lens Sharpness Profile").readout


def _a7iii_caps():
    return camera_caps.from_live_choices(
        "Sony Alpha-A7 III (PC Control)",
        ["100", "200", "400", "800", "1600", "3200", "6400"],
        ["4", "5.6", "8", "11", "16", "22"],
        ["1/8000", "1/2000", "1/1000", "1/500", "1/250", "1/125", "1/60", "1/30"])


def _a7iii_rig(transmission=lens_optics.UNKNOWN_TRANSMISSION):
    base = rig_profile.resolve_rig("Sony Alpha-A7 III (PC Control)", lens_model="FE 24-105mm F4 G OSS",
                                   lens_specification=(24, 105, 4, 4), profiles_dir=Path("/nonexistent"))
    from dataclasses import replace
    return replace(base, lens_transmission=transmission)


def test_recommend_live_applies_the_rig_transmission_only_to_scene_luminance():
    caps = _a7iii_caps()
    with patch.object(brain.embed_index, "recommend", return_value={"f_number": 4}):
        ttl = brain.recommend_live("unused.jpg", EV_F4_ISO100_1_250, caps.name, caps, 4, 50,
                                   rig=_a7iii_rig(synthetic_transmission(1.0)))
        scene = brain.recommend_live("unused.jpg", EV_F4_ISO100_1_250, caps.name, caps, 4, 50,
                                     rig=_a7iii_rig(synthetic_transmission(1.0)), ev_reference=SCENE)
        unknown = brain.recommend_live("unused.jpg", EV_F4_ISO100_1_250, caps.name, caps, 4, 50,
                                       rig=_a7iii_rig(), ev_reference=SCENE)
    assert (ttl.iso, ttl.aperture, ttl.shutter_s) == (100, 4.0, pytest.approx(1 / 250))
    assert (scene.iso, scene.aperture, scene.shutter_s) == (100, 4.0, pytest.approx(1 / 125))
    assert (unknown.iso, unknown.aperture, unknown.shutter_s) == (100, 4.0, pytest.approx(1 / 250))
    names = [f.name for f in scene.card("APERTURE").sub_factors]
    assert names[:2] == ["Taste", "Lens Sharpness Profile"] and "Lens Transmission" in names


def test_night_plan_raises_iso_by_the_offset_for_a_scene_luminance():
    @dataclass
    class _Rig:
        noise: object
        sensor: object
        lens_transmission: object

    noise = sensor_noise.load_measured_noise({
        "body": "TEST", "base_iso": 100, "ceiling_threshold_ratio": 2.5,
        "noise_table": [{"iso": 100, "sigma_luma_8bit": 2.0}, {"iso": 400, "sigma_luma_8bit": 3.0},
                        {"iso": 1600, "sigma_luma_8bit": 4.5}, {"iso": 6400, "sigma_luma_8bit": 9.0}]})
    caps = camera_caps.from_live_choices("TEST", ["100", "200", "400", "800", "1600", "3200"],
                                         ["4"], ["1"])
    ev = math.log2(16 / 1) - math.log2(400 / 100)   # f/4, 1 s needs exactly ISO 400
    known = _Rig(noise, physics.A7III, synthetic_transmission(1.0))
    unknown = _Rig(noise, physics.A7III, lens_optics.UNKNOWN_TRANSMISSION)
    assert night_assist.plan_night(ev, known, caps).iso == 400                       # through-lens
    assert night_assist.plan_night(ev, unknown, caps, ev_reference=SCENE).iso == 400
    scene = night_assist.plan_night(ev, known, caps, ev_reference=SCENE)
    assert scene.iso == 800
    assert scene.evidence["exposure_reference"]["applied_offset_stops"] == pytest.approx(1.0)


# --- B04: aperture from subject distance and required depth --------------------------------------
def _plan(taste, distance, scene_class, focal=50.0, **kwargs):
    kwargs.setdefault("focus_distance_m", distance)     # EXIF focus proof unless a test removes it
    return aperture_optimizer.plan_aperture(
        taste, subject_distance_m=distance, scene_class=scene_class, focal_mm=focal,
        sensor=physics.A7II, lens_opt=kwargs.pop("lens_opt", TAMRON),
        apertures=kwargs.pop("apertures", A7II_APERTURES), **kwargs)


def _wider(aperture, ladder):
    return max(n for n in ladder if n < aperture)


def test_landscape_far_subject_uses_the_hyperfocal_rule():
    """Without the landscape rule the plan keeps f/2.8 and infinity is soft (fails here)."""
    plan = _plan(2.8, 30.0, "landscape", focal=75.0)
    chosen = plan["f_number"]
    assert plan["status"] == "ok" and plan["rule"] == "hyperfocal" and plan["far_m"] == "inf"
    assert physics.hyperfocal_mm(75, chosen, physics.A7II.coc_mm) / 1000 <= 30.0
    assert physics.depth_of_field_m(30.0, 75, _wider(chosen, A7II_APERTURES),
                                    physics.A7II.coc_mm)[1] != math.inf
    assert chosen < physics.A7II.diffraction_hard_aperture and "hyperfocal" in plan["note"]


def test_landscape_subject_already_past_the_hyperfocal_keeps_the_taste_aperture():
    plan = _plan(4.0, 200.0, "landscape", focal=28.0)
    assert plan["status"] == "ok" and plan["f_number"] == 4.0 and plan["far_m"] == "inf"


def test_close_subject_gets_enough_depth_for_the_subject():
    """A 0.5 m portrait needs 0.10 m of depth: f/2.8 gives ~0.016 m, so the plan must stop down
    exactly as far as needed and no further."""
    plan = _plan(2.8, 0.5, "portrait")
    chosen = plan["f_number"]
    near, far = physics.depth_of_field_m(0.5, 50, chosen, physics.A7II.coc_mm)
    assert plan["status"] == "ok" and plan["rule"] == "subject_depth"
    assert plan["required_depth_m"] == pytest.approx(0.10)
    assert far - near >= 0.10 - 1e-9
    wn, wf = physics.depth_of_field_m(0.5, 50, _wider(chosen, A7II_APERTURES), physics.A7II.coc_mm)
    assert wf - wn < 0.10
    closeup = _plan(2.8, 0.5, "closeup")
    assert closeup["f_number"] < chosen and closeup["status"] == "ok"


def test_depth_beyond_the_diffraction_cap_is_flagged_not_forced():
    plan = _plan(2.8, 2.0, "landscape", focal=75.0)
    assert plan["status"] == "dof_exceeds_diffraction"
    assert plan["f_number"] == max(n for n in A7II_APERTURES
                                   if n <= physics.A7II.diffraction_hard_aperture + 1e-6)
    assert "infinity will be soft" in plan["note"]


def test_unknown_distance_keeps_todays_behaviour_and_says_so():
    """Unknown distance must not move the aperture; the recommendation must say why."""
    plan = _plan(5.6, None, "landscape")
    assert plan["status"] == "distance_unknown" and plan["f_number"] == 5.6
    assert "Subject distance unknown" in plan["note"]
    assert _plan(5.6, 3.0, None)["status"] == "no_depth_requirement"
    assert _plan(5.6, 3.0, None)["f_number"] == 5.6
    rec = _a7ii_rec(12.0, taste_f=5.6)
    assert rec.aperture == 5.6 and rec.aperture_plan["status"] == "distance_unknown"
    assert any("Subject distance unknown" in note for note in rec.notes)
    infinite = _plan(5.6, math.inf, "landscape")
    assert infinite["status"] == "ok" and infinite["rule"] == "focused_at_infinity"
    assert infinite["f_number"] == 5.6


def test_unprofiled_lens_ties_go_to_the_wider_aperture():
    plan = _plan(4.0, 200.0, "landscape", focal=28.0, lens_opt=None)
    assert plan["f_number"] == 4.0 and plan["sweet_spot_f"] is None


def test_recommend_settings_plans_a_landscape_from_distance_and_class():
    """The brain feeds the landscape class to the plan: without B04 it keeps f/2.8 (fails here)."""
    rec = _a7ii_rec(15.0, taste_f=2.8, subject_distance_m=30.0, scene_class="landscape",
                    distance_source=aperture_optimizer.DISTANCE_VISION)
    assert rec.aperture_plan["rule"] == "hyperfocal" and rec.aperture > 2.8
    dof = rec.card("APERTURE").sub_factors[0]
    assert dof.status == constraint_cards.OK and "hyperfocal" in dof.readout
    assert "vision_depth_estimate" in dof.readout


def test_recommend_live_prefers_the_depth_plan_over_taste():
    caps = _a7iii_caps()
    with patch.object(brain.embed_index, "recommend", return_value={"f_number": 4}):
        plain = brain.recommend_live("unused.jpg", 14.0, caps.name, caps, 4, 105, rig=_a7iii_rig())
        planned = brain.recommend_live("unused.jpg", 14.0, caps.name, caps, 4, 105, rig=_a7iii_rig(),
                                       subject_distance_m=40.0, scene_class="landscape",
                                       distance_source=aperture_optimizer.DISTANCE_EXIF,
                                       focus_distance_m=40.0)
    assert plain.aperture == 4.0 and plain.aperture_plan["status"] == "distance_unknown"
    hyperfocal = physics.hyperfocal_mm(105, planned.aperture, physics.A7III.coc_mm) / 1000
    assert planned.aperture > 4.0 and hyperfocal <= 40.0
    dof = next(f for f in planned.card("APERTURE").sub_factors if f.name == "Depth of Field")
    assert dof.status == constraint_cards.OK and "exif_subject_distance" in dof.readout


def test_exif_subject_distance_parsing_and_source_priority():
    parse = aperture_optimizer.subject_distance_from_exif
    assert parse((3, 2)) == 1.5 and parse(2.5) == 2.5
    assert parse((0, 1)) is None and parse(0) is None and parse(None) is None
    assert parse((0xFFFFFFFF, 1)) == math.inf and parse(-1.0) is None
    assert parse(float("nan")) is None and parse((1, 0)) is None and parse(True) is None
    resolve = aperture_optimizer.resolve_subject_distance
    # The detected subject is what depth of field must protect (review 4118663092); EXIF is the
    # lens's focus plane and stands in for the subject only when vision has no range.
    assert resolve(4.0, 9.0) == (9.0, aperture_optimizer.DISTANCE_VISION)
    assert resolve(None, 9.0) == (9.0, aperture_optimizer.DISTANCE_VISION)
    assert resolve(math.inf, None) == (math.inf, aperture_optimizer.DISTANCE_EXIF)
    assert resolve(4.0, None) == (4.0, aperture_optimizer.DISTANCE_EXIF)
    assert resolve(None, None) == (None, None)


def test_vision_depth_fraction_now_comes_from_the_optimizer_table():
    analysis = {"subject_distance_m": 2.0, "scene_class": "portrait"}
    assert vision_banana.dof_depth_m(analysis) == pytest.approx(0.4)
    assert vision_banana.dof_depth_m({**analysis, "scene_class": "landscape"}) is None


def _with_subject_distance(camera, value):
    original = camera.capture

    def capture(download=True):
        path = original(download)
        with Image.open(path) as image:
            exif = image.getexif()
            pixels = image.copy()
        exif[37382] = value
        pixels.save(path, exif=exif)
        return path
    camera.capture = capture


def test_engine_passes_the_vision_subject_and_the_exif_focus_plane(rig):  # noqa: F811
    _with_subject_distance(rig.camera, 7.0)
    with patch.object(vision_banana, "scene_class", return_value="landscape"), \
            patch.object(vision_banana, "subject_distance_m", return_value=3.0):
        shot = rig.real.shoot(None, variant_id="natural")
    kwargs = rig.a7iii.call_args.kwargs
    assert kwargs["subject_distance_m"] == pytest.approx(3.0)
    assert kwargs["distance_source"] == aperture_optimizer.DISTANCE_VISION
    assert kwargs["focus_distance_m"] == pytest.approx(7.0)
    assert kwargs["scene_class"] == "landscape"
    assert "aperture_plan" in shot.evidence and "exposure_reference" in shot.evidence


def test_engine_falls_back_to_the_vision_distance(rig):  # noqa: F811
    with patch.object(vision_banana, "subject_distance_m", return_value=3.0):
        rig.real.shoot(None, variant_id="natural")
    kwargs = rig.a7iii.call_args.kwargs
    assert kwargs["subject_distance_m"] == 3.0
    assert kwargs["distance_source"] == aperture_optimizer.DISTANCE_VISION


# --- Codex review findings (2026-09-27) ----------------------------------------------------------
def test_exif_ifdrational_with_float_components_is_not_truncated():
    """Catches int() truncation: IFDRational(1.5) must read 1.5 m, not 1 m; 0.5 m must not vanish."""
    from PIL.TiffImagePlugin import IFDRational
    parse = aperture_optimizer.subject_distance_from_exif
    assert parse(IFDRational(1.5)) == pytest.approx(1.5)
    assert parse(IFDRational(0.5)) == pytest.approx(0.5)
    assert parse(IFDRational(7, 2)) == pytest.approx(3.5)
    assert parse(IFDRational(0xFFFFFFFF, 1)) == math.inf
    assert parse((10 ** 400, 1)) is None     # an oversized component cannot raise


def test_infinity_focus_still_honours_the_ladder_and_diffraction_cap():
    """Catches the infinity branch returning a raw taste of f/25 past the ~f/21 hard cap."""
    plan = _plan(25.0, math.inf, "landscape")
    assert plan["f_number"] in A7II_APERTURES
    assert plan["f_number"] <= physics.A7II.diffraction_hard_aperture


def test_fallback_keeps_the_exposure_gate_when_depth_cannot_be_covered():
    """Catches the fallback returning f/20 when the exposure solver can only reach f/5.6."""
    plan = _plan(2.8, 2.0, "landscape", focal=75.0, exposure_feasible=lambda n: n <= 5.6)
    assert plan["status"] == "dof_exceeds_diffraction" and plan["f_number"] == 5.6


def test_reopened_final_aperture_is_reconciled_with_the_plan():
    """Catches a plan note that promises depth the final (reopened) aperture no longer gives."""
    rec = _a7ii_rec(1.0, taste_f=2.8, subject_distance_m=30.0, scene_class="landscape")
    plan = rec.aperture_plan
    assert plan["status"] == "ok" and plan["f_number"] > rec.aperture   # dark: exposure reopened
    assert plan["final_f_number"] == rec.aperture and plan["final_meets_requirement"] is False
    assert any("not covered in the final settings" in note for note in rec.notes)
    dof = rec.card("APERTURE").sub_factors[0]
    assert dof.status == constraint_cards.LIMITED


def test_plan_and_exposure_evidence_are_strict_json():
    import json
    for distance, scene in ((30.0, "landscape"), (math.inf, "portrait"), (None, None), (0.5, "portrait")):
        rec = _a7ii_rec(12.0, taste_f=2.8, subject_distance_m=distance, scene_class=scene,
                        transmission=lens_optics.FE_24_105_F4_G_TRANSMISSION, ev_reference=SCENE)
        json.dumps({"plan": rec.aperture_plan, "ref": rec.exposure_reference,
                    "rec": rec.to_dict()}, allow_nan=False)
    with pytest.raises(ValueError):
        _plan(2.8, 2.0, "portrait", dof_depth_m=math.inf)
    with pytest.raises(ValueError):
        physics.lens_adjusted_ev100(math.nan, ev_reference=TTL, t_stop_offset_stops=None)


def test_engine_reads_the_distance_from_the_accepted_remeter_frame(rig):  # noqa: F811
    """Catches a stale first-frame EXIF distance surviving a re-meter pass."""
    distances = iter([2.0, 9.0, 9.0, 9.0])
    original = rig.camera.capture

    def capture(download=True):
        path = original(download)
        with Image.open(path) as image:
            exif = image.getexif()
            pixels = image.copy()
        exif[37382] = next(distances)
        pixels.save(path, exif=exif)
        return path
    rig.camera.capture = capture
    rig.camera.frame_values = [255, 112, 112]   # first meter frame saturated -> one re-meter
    with patch.object(vision_banana, "scene_class", return_value="landscape"):
        shot = rig.real.shoot(None, variant_id="natural")
    assert len(shot.evidence["meter_passes"]) == 2
    assert rig.a7iii.call_args.kwargs["subject_distance_m"] == pytest.approx(9.0)


# --- PR #73 review thread: the final autofocus can move the focus plane after planning ------------
_REAL_RECOMMEND_LIVE = brain.recommend_live  # the engine fixture patches brain.recommend_live
_REAL_RECOMMEND_SETTINGS = brain.recommend_settings


def _planned_live_rec(rig, distance_source=aperture_optimizer.DISTANCE_VISION,  # noqa: F811
                      focus_distance_m=3.0):
    # The fake body has no lens EXIF, so the engine only permits its current aperture (f/8).
    caps = camera_caps.from_live_choices(rig.camera.model, ["100", "200", "400"], ["8"],
                                         ["1/500", "1/250", "1/125", "1/60"])
    with patch.object(brain.embed_index, "recommend", return_value={"f_number": 8}):
        return _REAL_RECOMMEND_LIVE("unused.jpg", 13.0, caps.name, caps, 8, 50, rig=_a7iii_rig(),
                                    subject_distance_m=3.0, scene_class="portrait",
                                    distance_source=distance_source,
                                    focus_distance_m=focus_distance_m)


def _wire_focus(camera, indication, half_press="Focus Locked"):
    camera.read_focus_indication = lambda: indication
    camera.autofocus_half_press = lambda hold_s: half_press


def _dof(shot):
    aperture = next(card for card in shot.cards if card["axis"] == "APERTURE")
    return next(f for f in aperture["sub_factors"] if f["name"] == "Depth of Field")


def test_af_search_after_planning_marks_the_depth_plan_unverified(rig):  # noqa: F811
    """Without reconciliation the shot claims an OK depth plan for a focus plane that moved."""
    rig.a7iii.return_value = _planned_live_rec(rig)
    _wire_focus(rig.camera, "Unlock")                       # meter lock lapsed -> one half-press
    shot = rig.real.shoot(None, variant_id="natural")
    plan = shot.evidence["aperture_plan"]
    assert shot.evidence["final_focus_policy"]["af_focus_lock"]["af_search"] == "driven_half_press"
    assert plan["focus_basis"] == "autofocus_searched_after_plan"
    assert plan["final_meets_requirement"] is None
    assert _dof(shot)["status"] == constraint_cards.PENDING
    assert "focus not verified at the subject" in _dof(shot)["readout"]
    assert "depth of field is unverified" in shot.summary


def test_meter_focus_held_keeps_the_depth_plan(rig):  # noqa: F811
    rig.a7iii.return_value = _planned_live_rec(rig)
    _wire_focus(rig.camera, "Focus Locked")                 # lock persists -> no new AF search
    shot = rig.real.shoot(None, variant_id="natural")
    assert shot.evidence["aperture_plan"]["focus_basis"] == "meter_frame_focus"
    assert _dof(shot)["status"] == constraint_cards.OK
    assert "unverified" not in shot.summary


@pytest.mark.parametrize("final_distance", [0.5, 30.0])
def test_af_search_rechecks_depth_at_the_final_frames_focus_distance(rig, final_distance):  # noqa: F811
    """Planned: portrait at 3 m, f/8. AF then lands at 0.5 m or 30 m; neither sharp zone contains
    the original subject (at 30 m the near limit is ~8 m even though the zone is wide)."""
    rig.a7iii.return_value = _planned_live_rec(rig)
    _wire_focus(rig.camera, "Unlock")
    distances = iter([None, final_distance])                # meter frame: none; final frame: moved
    original = rig.camera.capture

    def capture(download=True):
        path = original(download)
        value = next(distances)
        if value is not None:
            with Image.open(path) as image:
                exif = image.getexif()
                pixels = image.copy()
            exif[37382] = value
            pixels.save(path, exif=exif)
        return path
    rig.camera.capture = capture
    shot = rig.real.shoot(None, variant_id="natural")
    plan = shot.evidence["aperture_plan"]
    assert plan["final_focus_distance_m"] == pytest.approx(final_distance)
    assert plan["final_meets_requirement"] is False
    assert _dof(shot)["status"] == constraint_cards.LIMITED
    assert "does not cover" in shot.summary


def _reconcile(plan, final_distance, f_number, focal=50.0):
    note = aperture_optimizer.reconcile_final_focus(
        plan, af_search="driven_half_press", final_distance_m=final_distance,
        final_f_number=f_number, focal_mm=focal, sensor=physics.A7II)
    return note, plan["final_meets_requirement"]


def test_refocus_far_away_does_not_cover_the_original_subject():
    """Review 4118051434: 3 m -> 30 m at f/8 is a wide zone starting ~8 m, so the 3 m subject is soft."""
    plan = _plan(8.0, 3.0, "portrait")
    assert plan["f_number"] == 8.0 and plan["status"] == "ok"
    lo, hi = plan["subject_interval_m"]
    assert lo < 3.0 < hi and hi - lo == pytest.approx(0.6)
    note, met = _reconcile(plan, 30.0, 8.0)
    assert met is False and "does not cover" in note


def test_small_af_move_that_still_covers_the_subject_is_ok():
    plan = _plan(8.0, 3.0, "portrait")
    note, met = _reconcile(plan, 3.2, 8.0)
    assert met is True and note is None


def test_infinity_focus_with_a_near_subject_is_not_covered():
    plan = _plan(8.0, 3.0, "portrait")
    note, met = _reconcile(plan, math.inf, 8.0)
    assert met is False and "does not cover" in note


def test_infinity_focus_for_a_landscape_plan_is_covered():
    plan = _plan(2.8, 30.0, "landscape", focal=75.0)
    assert plan["subject_interval_m"] == [30.0, "inf"]
    note, met = _reconcile(plan, math.inf, plan["f_number"], focal=75.0)
    assert met is True and note is None
    tight = _plan(2.8, 30.0, "landscape", focal=75.0)
    _, met_wide_open = _reconcile(tight, math.inf, 2.8, focal=75.0)   # near limit ~70 m > 30 m
    assert met_wide_open is False



# --- PR #73 review 4118303319: in manual focus a vision range is not the lens's focus plane -------
def test_manual_focus_without_exif_distance_leaves_the_depth_plan_unverified(rig):  # noqa: F811
    """MF runs no AF, so nothing ties the vision-estimated range to where the lens is focused."""
    rig.a7iii.return_value = _planned_live_rec(rig, focus_distance_m=None)   # vision only
    rig.camera.settings["focus_mode"] = "Manual"
    shot = rig.real.shoot(None, variant_id="natural")
    plan = shot.evidence["aperture_plan"]
    assert shot.evidence["final_focus_policy"]["af_focus_lock"]["af_search"] == "none_manual_focus"
    assert plan["focus_basis"] == "manual_focus" and plan["status"] == "focus_unknown"
    assert _dof(shot)["status"] == constraint_cards.PENDING
    assert "focus distance unknown" in _dof(shot)["readout"]


def test_manual_focus_with_exif_distance_is_judged(rig):  # noqa: F811
    """With the lens's own focus distance (EXIF) the MF depth plan is judged, not assumed."""
    rig.a7iii.return_value = _planned_live_rec(rig, aperture_optimizer.DISTANCE_EXIF)
    rig.camera.settings["focus_mode"] = "Manual"
    shot = rig.real.shoot(None, variant_id="natural")
    plan = shot.evidence["aperture_plan"]
    assert plan["focus_basis"] == "manual_focus" and plan["final_meets_requirement"] is True
    assert _dof(shot)["status"] == constraint_cards.OK
    assert "unverified" not in shot.summary


def test_manual_focus_pure_judgement_uses_the_final_exif_distance():
    plan = _plan(8.0, 3.0, "portrait", distance_source=aperture_optimizer.DISTANCE_EXIF)
    note = aperture_optimizer.reconcile_final_focus(
        plan, af_search="none_manual_focus", final_distance_m=30.0, final_f_number=8.0,
        focal_mm=50, sensor=physics.A7II)
    assert plan["final_meets_requirement"] is False and "does not cover" in note
    vision = _plan(8.0, 3.0, "landscape", distance_source=aperture_optimizer.DISTANCE_VISION,
                   focus_distance_m=None)
    assert vision["status"] == "focus_unknown" and vision["f_number"] == 8.0
    note = aperture_optimizer.reconcile_final_focus(
        vision, af_search="none_manual_focus", final_distance_m=None, final_f_number=8.0,
        focal_mm=50, sensor=physics.A7II)
    assert note is None and "focus distance unknown" in aperture_optimizer.depth_pending_reason(vision)


# --- PR #73 review 4118464435: an infinity plan after a new AF search -----------------------------
def _infinity_plan():
    plan = _plan(5.6, math.inf, "landscape", distance_source=aperture_optimizer.DISTANCE_EXIF)
    assert plan["rule"] == "focused_at_infinity" and plan["status"] == "ok"
    return plan


def test_infinity_plan_after_af_moved_to_a_near_plane_is_not_covered():
    plan = _infinity_plan()
    note, met = _reconcile(plan, 3.0, plan["f_number"])
    assert met is False and "does not cover" in note


def test_infinity_plan_holds_when_af_stays_at_or_beyond_the_hyperfocal():
    for final in (math.inf, 100.0):
        plan = _infinity_plan()
        note, met = _reconcile(plan, final, plan["f_number"])
        assert met is True and note is None, final


def test_infinity_plan_after_af_without_a_final_distance_is_unverified():
    plan = _infinity_plan()
    note, met = _reconcile(plan, None, plan["f_number"])
    assert met is None and "unverified" in note


def test_engine_flags_an_infinity_plan_when_af_moves_focus_close(rig):  # noqa: F811
    caps = camera_caps.from_live_choices(rig.camera.model, ["100", "200", "400"], ["8"],
                                         ["1/500", "1/250", "1/125", "1/60"])
    with patch.object(brain.embed_index, "recommend", return_value={"f_number": 8}):
        rig.a7iii.return_value = _REAL_RECOMMEND_LIVE(
            "unused.jpg", 13.0, caps.name, caps, 8, 50, rig=_a7iii_rig(), subject_distance_m=math.inf,
            scene_class="landscape", distance_source=aperture_optimizer.DISTANCE_EXIF,
            focus_distance_m=math.inf)
    _wire_focus(rig.camera, "Unlock")
    distances = iter([None, 3.0])
    original = rig.camera.capture

    def capture(download=True):
        path = original(download)
        value = next(distances)
        if value is not None:
            with Image.open(path) as image:
                exif = image.getexif()
                pixels = image.copy()
            exif[37382] = value
            pixels.save(path, exif=exif)
        return path
    rig.camera.capture = capture
    shot = rig.real.shoot(None, variant_id="natural")
    assert shot.evidence["aperture_plan"]["final_meets_requirement"] is False
    assert _dof(shot)["status"] == constraint_cards.LIMITED


# --- PR #73 review 4118464442: no measured focal length, no depth plan ----------------------------
def test_recommend_settings_does_not_plan_depth_on_the_fallback_focal():
    with patch.object(brain.embed_index, "recommend", return_value={"f_number": 4.0}):
        rec = brain.recommend_settings("unused.jpg", 15.0, focal_mm=None, subject_distance_m=30.0,
                                       scene_class="landscape")
    assert rec.aperture_plan["status"] == "focal_unknown" and rec.aperture == 4.0
    assert rec.card("APERTURE").sub_factors[0].status == constraint_cards.PENDING
    assert any("Focal length unavailable" in note for note in rec.notes)


def test_recommend_live_does_not_plan_depth_on_the_fallback_focal():
    caps = _a7iii_caps()
    with patch.object(brain.embed_index, "recommend", return_value={"f_number": 4}):
        rec = brain.recommend_live("unused.jpg", 14.0, caps.name, caps, 4, None, rig=_a7iii_rig(),
                                   subject_distance_m=40.0, scene_class="landscape",
                                   distance_source=aperture_optimizer.DISTANCE_VISION)
    assert rec.aperture_plan["status"] == "focal_unknown" and rec.aperture == 4.0
    dof = next(f for f in rec.card("APERTURE").sub_factors if f.name == "Depth of Field")
    assert dof.status == constraint_cards.PENDING



# --- PR #73 review 4118663092: EXIF is the focus plane, vision is the subject --------------------
def test_subject_outside_the_exif_focus_zone_is_not_reported_covered():
    """AF on the background at infinity, subject at 3 m: no safe aperture reaches 3 m from infinity."""
    plan = _plan(2.8, 3.0, "portrait", focus_distance_m=math.inf,
                 distance_source=aperture_optimizer.DISTANCE_VISION)
    assert plan["focus_distance_m"] == "inf"
    assert plan["focus_distance_source"] == aperture_optimizer.DISTANCE_EXIF
    assert plan["status"] == "dof_exceeds_diffraction"
    assert aperture_optimizer.requirement_met(plan, plan["f_number"], 50.0, physics.A7II) is False
    dof = constraint_cards.depth_of_field_subfactor(plan["f_number"], 50.0, physics.A7II, 3.0,
                                                    True, plan)
    assert dof.status == constraint_cards.LIMITED and "focus inf" in dof.readout


def test_exif_focus_near_the_subject_still_plans_normally():
    plan = _plan(8.0, 3.0, "portrait", focus_distance_m=3.1,
                 distance_source=aperture_optimizer.DISTANCE_VISION)
    assert plan["status"] == "ok"
    low, high = plan["subject_interval_m"]
    assert (low, high) == (pytest.approx(2.7), pytest.approx(3.3))
    near, far = physics.depth_of_field_m(3.1, 50, plan["f_number"], physics.A7II.coc_mm)
    assert near <= 2.7 and far >= 3.3


# --- PR #73 review 4118663102: no depth requirement -> Depth of Field pending ---------------------
def test_no_depth_requirement_keeps_the_depth_factor_pending():
    rec = _a7ii_rec(12.0, taste_f=5.6, subject_distance_m=3.0, scene_class=None,
                    distance_source=aperture_optimizer.DISTANCE_EXIF)
    assert rec.aperture_plan["status"] == "no_depth_requirement"
    dof = rec.card("APERTURE").sub_factors[0]
    assert dof.status == constraint_cards.PENDING and "no depth requirement" in dof.readout


# --- PR #73 review 4118663107: the Wi-Fi route focuses after planning -----------------------------
def _wifi_rec():
    with patch.object(brain.embed_index, "recommend", return_value={"f_number": 2.8}):
        return brain.recommend_settings("unused.jpg", 15.0, focal_mm=75, subject_distance_m=30.0,
                                        scene_class="landscape",
                                        distance_source=aperture_optimizer.DISTANCE_VISION)


@pytest.mark.parametrize("focus_ran,final_distance", [(True, None), (True, 2.0), (False, None)])
def test_wifi_plan_without_an_exif_focus_stays_pending(focus_ran, final_distance):
    """4118663107 under the one rule: the Wi-Fi liveview has no EXIF focus distance, so the plan never
    drives the aperture and Depth of Field stays pending, whatever focus_subject later does."""
    import wifi_autopilot
    rec = _wifi_rec()
    assert rec.aperture_plan["status"] == "focus_unknown" and rec.aperture == 2.8
    wifi_autopilot._reconcile_depth_after_focus(rec, focus_ran=focus_ran,
                                                final_distance_m=final_distance, focal_mm=75)
    dof = rec.card("APERTURE").sub_factors[0]
    assert rec.aperture == 2.8                                  # assessment never changes a setting
    if final_distance is None:
        assert dof.status == constraint_cards.PENDING and "focus distance unknown" in dof.readout
    else:
        # astra P2: the captured f/2.8 is assessed against the final frame's proofs (2 m, 75 mm).
        assert dof.status == constraint_cards.LIMITED and "final focus 2.00m" in dof.readout
    wifi_autopilot._reconcile_depth_after_focus(object(), focus_ran=True, final_distance_m=None,
                                                focal_mm=75)   # a plan-less double is left alone


# --- Wi-Fi first shot: no measured focal length, no depth plan (same defect as 4118464442) ---------
def test_wifi_first_shot_without_a_measured_focal_does_not_plan_depth(tmp_path, monkeypatch):
    """Before the fix the cold-start 50 mm default reached the planner: a 30 m landscape planned
    f/3.2 (50 mm hyperfocal ~31 m) instead of keeping the current f/2.8."""
    import types
    import test_wifi_intent_propagation as wifi_harness
    import wifi_autopilot
    import wifi_engine
    cam = wifi_harness.FakeCamera()
    wifi_harness._install_stubs(monkeypatch, tmp_path, cam)
    monkeypatch.setattr(wifi_autopilot, "_LAST_FOCAL_MM_ACTUAL", None)
    monkeypatch.setattr(wifi_autopilot, "vision_banana", types.SimpleNamespace(
        analyze=lambda *a, **k: None, metering_region=lambda *a, **k: None,
        subject_distance_m=lambda *a, **k: 30.0, dof_depth_m=lambda *a, **k: None,
        scene_class=lambda *a, **k: "landscape"))
    seen = {}

    def real_brain(*args, **kwargs):
        with patch.object(brain.embed_index, "recommend", return_value={"f_number": 2.8}):
            seen["kwargs"] = kwargs
            seen["rec"] = _REAL_RECOMMEND_SETTINGS(*args, **kwargs)
            return seen["rec"]
    monkeypatch.setattr(wifi_autopilot.brain, "recommend_settings", real_brain)
    shot = wifi_engine.WiFiEngine().shoot(None, applied_plan=wifi_harness._plan())
    assert shot.ok, shot
    assert seen["kwargs"]["focal_is_current"] is False            # no fake focal reaches the planner
    rec = seen["rec"]
    assert rec.aperture_plan["status"] == "focal_unknown" and rec.aperture == 2.8
    assert rec.card("APERTURE").sub_factors[0].status == constraint_cards.PENDING
    assert rec.card("SHUTTER").sub_factors[0].readout.startswith("1/50s")   # floor keeps 50 mm


# --- PR #73 review 4119106835: a refocus that satisfies the plan clears the stale failure ---------
def test_refocus_onto_the_subject_clears_the_initial_depth_failure(rig):  # noqa: F811
    """Meter EXIF focus at infinity, subject at 3 m: the plan starts as dof_exceeds (LIMITED). AF then
    lands at 3 m (final EXIF) and covers the subject, so the delivered card must say so."""
    caps = camera_caps.from_live_choices(rig.camera.model, ["100", "200", "400"], ["8"],
                                         ["1/500", "1/250", "1/125", "1/60"])
    with patch.object(brain.embed_index, "recommend", return_value={"f_number": 8}):
        rec = _REAL_RECOMMEND_LIVE(
            "unused.jpg", 13.0, caps.name, caps, 8, 50, rig=_a7iii_rig(), subject_distance_m=3.0,
            scene_class="portrait", distance_source=aperture_optimizer.DISTANCE_VISION,
            focus_distance_m=math.inf)
    assert rec.aperture_plan["status"] == "dof_exceeds_diffraction"
    rig.a7iii.return_value = rec
    _wire_focus(rig.camera, "Unlock")
    distances = iter([None, 3.0])
    original = rig.camera.capture

    def capture(download=True):
        path = original(download)
        value = next(distances)
        if value is not None:
            with Image.open(path) as image:
                exif = image.getexif()
                pixels = image.copy()
            exif[37382] = value
            pixels.save(path, exif=exif)
        return path
    rig.camera.capture = capture
    shot = rig.real.shoot(None, variant_id="natural")
    assert shot.evidence["aperture_plan"]["final_meets_requirement"] is True
    assert _dof(shot)["status"] == constraint_cards.OK and "covers the subject" in _dof(shot)["readout"]
    assert "can't cover" not in _dof(shot)["readout"]            # rebuilt, not appended (4119218091)
    assert "final focus 3.00m" in _dof(shot)["readout"]
    assert "covers the planned subject" in shot.summary


def test_wifi_refocus_that_covers_the_subject_marks_the_card_ok():
    import wifi_autopilot
    with patch.object(brain.embed_index, "recommend", return_value={"f_number": 8.0}):
        rec = brain.recommend_settings("unused.jpg", 12.0, focal_mm=50, subject_distance_m=3.0,
                                       scene_class="portrait", focus_distance_m=math.inf,
                                       distance_source=aperture_optimizer.DISTANCE_VISION)
    assert rec.card("APERTURE").sub_factors[0].status == constraint_cards.LIMITED
    wifi_autopilot._reconcile_depth_after_focus(rec, focus_ran=True, final_distance_m=3.0, focal_mm=50)
    assert rec.aperture_plan["final_meets_requirement"] is True
    assert rec.card("APERTURE").sub_factors[0].status == constraint_cards.OK
    assert any("covers the planned subject" in note for note in rec.notes)



# --- PR #73 review 4119179172: EXIF infinity alone is not a depth requirement ---------------------
@pytest.mark.parametrize("scene_class", [None, "portrait"])
def test_exif_only_infinity_without_a_landscape_stays_pending(scene_class):
    """The lens focus plane at infinity says nothing about where the subject is."""
    rec = _a7ii_rec(12.0, taste_f=5.6, subject_distance_m=math.inf, scene_class=scene_class,
                    distance_source=aperture_optimizer.DISTANCE_EXIF, focus_distance_m=math.inf)
    assert rec.aperture_plan["status"] == "no_depth_requirement" and rec.aperture == 5.6
    dof = rec.card("APERTURE").sub_factors[0]
    assert dof.status == constraint_cards.PENDING and "no depth requirement" in dof.readout


def test_landscape_at_infinity_keeps_the_infinity_rule():
    rec = _a7ii_rec(12.0, taste_f=5.6, subject_distance_m=math.inf, scene_class="landscape",
                    distance_source=aperture_optimizer.DISTANCE_EXIF, focus_distance_m=math.inf)
    assert rec.aperture_plan["rule"] == "focused_at_infinity"
    assert rec.card("APERTURE").sub_factors[0].status == constraint_cards.OK


# --- PR #73 review 4119179175: the shortfall message uses the adjusted EV -------------------------
def test_night_shortfall_message_reports_the_transmission_adjusted_ev():
    @dataclass
    class _Rig:
        noise: object
        sensor: object
        lens_transmission: object

    noise = sensor_noise.load_measured_noise({
        "body": "TEST", "base_iso": 100, "ceiling_threshold_ratio": 2.5,
        "noise_table": [{"iso": 100, "sigma_luma_8bit": 2.0}, {"iso": 400, "sigma_luma_8bit": 3.0},
                        {"iso": 1600, "sigma_luma_8bit": 4.5}, {"iso": 6400, "sigma_luma_8bit": 9.0}]})
    caps = camera_caps.from_live_choices("TEST", ["100", "200", "400"], ["4"], ["1"])
    limit = math.log2(16 / 1) - math.log2(400 / 100)          # darkest exposable: f/4, 1 s, ISO 400
    rig = _Rig(noise, physics.A7III, synthetic_transmission(1.0))
    plan = night_assist.plan_night(limit + 0.5, rig, caps, ev_reference=SCENE)   # 0.5 above, then -1
    assert plan.status == "insufficient_light" and plan.shortfall_ev == pytest.approx(0.5, abs=1e-3)
    message = " ".join(plan.why)
    assert f"{limit - 0.5:.2f}" in message and "lens transmission" in message



# --- PR #73 review 4119218087: a cached postview focal is not a current measurement ---------------
def test_wifi_cached_focal_feeds_the_floor_but_not_the_depth_plan(tmp_path, monkeypatch):
    import types
    import test_wifi_intent_propagation as wifi_harness
    import wifi_autopilot
    import wifi_engine
    cam = wifi_harness.FakeCamera()
    wifi_harness._install_stubs(monkeypatch, tmp_path, cam)
    monkeypatch.setattr(wifi_autopilot, "_LAST_FOCAL_MM_ACTUAL", 24.0)   # previous shot's postview
    monkeypatch.setattr(wifi_autopilot, "vision_banana", types.SimpleNamespace(
        analyze=lambda *a, **k: None, metering_region=lambda *a, **k: None,
        subject_distance_m=lambda *a, **k: 30.0, dof_depth_m=lambda *a, **k: None,
        scene_class=lambda *a, **k: "landscape"))
    seen = {}

    def real_brain(*args, **kwargs):
        with patch.object(brain.embed_index, "recommend", return_value={"f_number": 2.8}):
            seen["kwargs"] = kwargs
            seen["rec"] = _REAL_RECOMMEND_SETTINGS(*args, **kwargs)
            return seen["rec"]
    monkeypatch.setattr(wifi_autopilot.brain, "recommend_settings", real_brain)
    shot = wifi_engine.WiFiEngine().shoot(None, applied_plan=wifi_harness._plan())
    assert shot.ok, shot
    assert seen["kwargs"]["focal_mm"] == 24.0 and seen["kwargs"]["focal_is_current"] is False
    rec = seen["rec"]
    assert rec.aperture_plan["status"] == "focal_unknown" and rec.aperture == 2.8
    assert rec.card("SHUTTER").sub_factors[0].readout.startswith("1/24s")   # floor keeps the cache


# --- PR #73 review 4119218095: a missing focal is not a missing subject ---------------------------
def test_focal_unknown_card_names_the_missing_focal_not_the_subject():
    with patch.object(brain.embed_index, "recommend", return_value={"f_number": 4.0}):
        rec = brain.recommend_settings("unused.jpg", 15.0, focal_mm=None, subject_distance_m=30.0,
                                       scene_class="landscape")
    dof = rec.card("APERTURE").sub_factors[0]
    assert dof.status == constraint_cards.PENDING
    assert "focal length unavailable" in dof.readout and "subject 30.00m" in dof.readout
    assert "subject distance pending" not in dof.readout and "hyperfocal" not in dof.readout


# --- ONE rule for certifying depth (coordinator, closes 4119218087/091/095 and 4119321252) --------
# Depth drives the aperture and is certified (OK / limited) only with BOTH a focal length measured
# for this shot AND an EXIF-proven focus distance; the vision range is the subject to protect,
# never proof of where the lens is focused. Otherwise the aperture is taste/current and Depth of
# Field is PENDING with a reason naming what is missing, rebuilt from the current state.
_FOCALS = {"measured": dict(focal_mm=75.0), "cached": dict(focal_mm=24.0, focal_is_current=False),
           "missing": dict(focal_mm=None)}
_FOCUS = {"exif": dict(subject_distance_m=30.0, focus_distance_m=30.0,
                       distance_source=aperture_optimizer.DISTANCE_VISION),
          "vision": dict(subject_distance_m=30.0, distance_source=aperture_optimizer.DISTANCE_VISION),
          "none": dict(subject_distance_m=None)}


def _expected(focal, focus, af):
    if focus == "none":
        return "pending", "subject distance unknown", False
    if focal != "measured":
        return "pending", "focal length unavailable", False
    if focus == "vision":
        return "pending", "focus distance unknown", False
    if af == "driven_half_press":
        return "pending", "focus not verified at the subject", True
    return "ok", "covers the subject", True


@pytest.mark.parametrize("af", ["driven_half_press", "none_already_locked", "none_manual_focus"])
@pytest.mark.parametrize("focus", sorted(_FOCUS))
@pytest.mark.parametrize("focal", sorted(_FOCALS))
def test_depth_is_certified_only_with_a_measured_focal_and_an_exif_focus(focal, focus, af):
    with patch.object(brain.embed_index, "recommend", return_value={"f_number": 2.8}):
        rec = _REAL_RECOMMEND_SETTINGS("unused.jpg", 15.0, scene_class="landscape",
                                       **_FOCALS[focal], **_FOCUS[focus])
    constraint_cards.settle_depth_after_focus(
        rec, af_search=af, final_distance_m=None,
        focal_mm=_FOCALS[focal]["focal_mm"] if focal == "measured" else None, sensor=physics.A7II)
    status, reason, driven = _expected(focal, focus, af)
    dof = rec.card("APERTURE").sub_factors[0]
    assert dof.status == status and reason in dof.readout, (focal, focus, af, dof)
    assert (rec.aperture > 2.8) is driven, (focal, focus, af, rec.aperture)
    assert "—  —" not in dof.readout and dof.readout.count(" — ") <= 1   # rebuilt, never appended


def test_held_meter_lock_without_exif_does_not_certify_the_vision_subject(rig):  # noqa: F811
    """4119321252: an AF lock chosen before vision ran proves nothing about the vision subject."""
    rig.a7iii.return_value = _planned_live_rec(rig, focus_distance_m=None)   # vision only, no EXIF
    _wire_focus(rig.camera, "Focus Locked")                     # meter lock held, no new AF
    shot = rig.real.shoot(None, variant_id="natural")
    assert shot.evidence["aperture_plan"]["status"] == "focus_unknown"
    assert _dof(shot)["status"] == constraint_cards.PENDING
    assert "focus distance unknown" in _dof(shot)["readout"]


def test_held_meter_lock_with_exif_focus_is_certified(rig):  # noqa: F811
    caps = camera_caps.from_live_choices(rig.camera.model, ["100", "200", "400"], ["8"],
                                         ["1/500", "1/250", "1/125", "1/60"])
    with patch.object(brain.embed_index, "recommend", return_value={"f_number": 8}):
        rig.a7iii.return_value = _REAL_RECOMMEND_LIVE(
            "unused.jpg", 13.0, caps.name, caps, 8, 50, rig=_a7iii_rig(), subject_distance_m=3.0,
            scene_class="portrait", distance_source=aperture_optimizer.DISTANCE_VISION,
            focus_distance_m=3.0)
    _wire_focus(rig.camera, "Focus Locked")
    shot = rig.real.shoot(None, variant_id="natural")
    assert shot.evidence["aperture_plan"]["final_meets_requirement"] is True
    assert _dof(shot)["status"] == constraint_cards.OK and "covers the subject" in _dof(shot)["readout"]



# --- astra review of 69dc3ff6 --------------------------------------------------------------------
# [P1] USB applied the depth-driven exposure before learning whether the meter's EXIF focus proof
# would hold for the final frame. The focus policy is now settled BEFORE the exposure is applied: if
# focus will move (or its state is unconfirmed), the taste/current exposure is solved and applied.
def _usb_depth_rig(rig, monkeypatch, af_case):  # noqa: F811
    # The engine fixture already patches focal_from_exif (restored on its exit); set its value only,
    # so no second patch layer can leak the fake past this test.
    live_verify_module().focal_from_exif.return_value = 75.0
    monkeypatch.setattr(vision_banana, "subject_distance_m", lambda *a: 30.0)
    monkeypatch.setattr(vision_banana, "scene_class", lambda *a: "landscape")
    rig.camera.read_focus_indication = lambda: "Focus Locked" if af_case == "held" else "Unlock"
    rig.camera.autofocus_half_press = lambda hold_s: "Focus Locked"

    def af_error(*args, **kwargs):
        raise RuntimeError("synthetic AF error")
    if af_case == "read_error":
        rig.camera.read_focus_indication = af_error
    elif af_case == "drive_error":
        rig.camera.autofocus_half_press = af_error
    original = rig.camera.capture

    def frame(download=True):
        path = original(download=download)
        if rig.camera.capture_count == 1:                     # meter frame: EXIF focus at 30 m
            with Image.open(path) as image:
                exif = image.getexif()
                pixels = image.copy()
            exif[34665] = {37382: 30.0, 42034: (28.0, 75.0, 2.8, 2.8)}
            pixels.save(path, exif=exif)
        return path
    rig.camera.capture = frame
    seen = {"calls": []}

    def recommend(*args, **kwargs):
        with patch.object(brain.embed_index, "recommend", return_value={"f_number": 4.0}):
            rec = _REAL_RECOMMEND_LIVE(*args, **kwargs)
            baseline = _REAL_RECOMMEND_LIVE(*args, **{**kwargs, "focus_distance_m": None})
        seen["calls"].append(kwargs.get("focus_distance_m"))
        seen["driven"] = seen.get("driven") or rec
        seen["baseline"] = baseline
        return rec
    rig.a7iii.side_effect = recommend
    return seen


def live_verify_module():
    import live_verify
    return live_verify


def _applied(rig):  # noqa: F811
    writes = [w for w in rig.camera.exposure_writes if {"iso", "aperture", "shutter"} <= set(w)]
    return writes[-1]


@pytest.mark.parametrize("af_case", ["searched", "read_error", "drive_error"])
def test_usb_applies_the_taste_exposure_when_the_meter_focus_proof_will_not_hold(
        rig, monkeypatch, af_case):  # noqa: F811
    seen = _usb_depth_rig(rig, monkeypatch, af_case)
    shot = rig.real.shoot(None, variant_id="natural")
    assert shot.ok
    assert seen["driven"].aperture != seen["baseline"].aperture       # depth would have changed it
    applied = _applied(rig)
    baseline = seen["baseline"]
    assert (applied["iso"], applied["aperture"], applied["shutter"]) == (
        baseline.iso_label, baseline.aperture_label, baseline.shutter_label)
    assert seen["calls"] == [30.0, None]                                # re-solved without the proof
    assert shot.evidence["aperture_plan"]["status"] == "focus_unknown"
    assert _dof(shot)["status"] == constraint_cards.PENDING


def test_usb_keeps_the_depth_exposure_when_the_meter_focus_holds(rig, monkeypatch):  # noqa: F811
    seen = _usb_depth_rig(rig, monkeypatch, "held")
    shot = rig.real.shoot(None, variant_id="natural")
    assert shot.ok and seen["calls"] == [30.0]
    applied = _applied(rig)
    assert applied["aperture"] == seen["driven"].aperture_label != seen["baseline"].aperture_label
    assert shot.evidence["final_focus_policy"]["af_focus_lock"]["af_search"] == "none_already_locked"
    assert _dof(shot)["status"] in (constraint_cards.OK, constraint_cards.LIMITED)


# [P2] The depth requirement is kept even when it did not choose the aperture, and after capture the
# CAPTURED aperture is assessed against the final frame's own proofs (assessment only).
def test_an_undriven_plan_is_assessed_from_the_final_frames_proofs():
    plan = _plan(2.8, 30.0, "landscape", focal=None, focus_distance_m=None)
    assert plan["status"] == "focal_unknown" and plan["rule"] == "hyperfocal"
    note = aperture_optimizer.reconcile_final_focus(
        plan, af_search="driven_half_press", final_distance_m=100.0, final_f_number=2.8,
        focal_mm=50.0, sensor=physics.A7II)
    assert plan["final_meets_requirement"] is True and plan["f_number"] == 2.8
    assert "covers" in note
    assert aperture_optimizer.depth_pending_reason(plan) is None


@pytest.mark.parametrize("event_focal", [0.0, 50.0])
def test_wifi_postview_focal_and_distance_assess_the_captured_aperture(tmp_path, monkeypatch,
                                                                         event_focal):
    import contextlib
    import io
    import types
    import test_wifi_intent_propagation as wifi_harness
    import wifi_autopilot
    import wifi_engine
    cam = wifi_harness.FakeCamera()
    wifi_harness._install_stubs(monkeypatch, tmp_path, cam)
    monkeypatch.setattr(cam, "focal_length_mm", lambda **k: event_focal)
    monkeypatch.setattr(wifi_autopilot, "_LAST_FOCAL_MM_ACTUAL", None)
    monkeypatch.setattr(wifi_autopilot, "vision_banana", types.SimpleNamespace(
        analyze=lambda *a: None, metering_region=lambda *a: None,
        subject_distance_m=lambda *a: 30.0, dof_depth_m=lambda *a: None,
        scene_class=lambda *a: "landscape"))
    # The postview's measured focal is stubbed: reading it from EXIF is a separate fix
    # (fix/exif-focal-length), and this test must not depend on it.
    monkeypatch.setattr(wifi_autopilot.live_verify, "focal_from_exif",
                        lambda path, default=None: 50.0)

    def download(url, dest):
        exif = Image.Exif()
        exif[37386] = 50.0          # FocalLength of the captured frame
        exif[37382] = 30.0          # SubjectDistance of the captured frame
        Image.new("RGB", (48, 32), "red").save(dest, exif=exif)
        return dest
    cam.download = download
    seen = {}

    def recommend(*args, **kwargs):
        with patch.object(brain.embed_index, "recommend", return_value={"f_number": 2.8}), \
                contextlib.redirect_stdout(io.StringIO()):
            seen["rec"] = _REAL_RECOMMEND_SETTINGS(*args, **kwargs)
            return seen["rec"]
    monkeypatch.setattr(wifi_autopilot.brain, "recommend_settings", recommend)
    shot = wifi_engine.WiFiEngine().shoot(None, applied_plan=wifi_harness._plan())
    assert shot.ok
    rec = seen["rec"]
    assert rec.aperture == 2.8                                   # never changed by the assessment
    dof = rec.card("APERTURE").sub_factors[0]
    assert dof.status in (constraint_cards.OK, constraint_cards.LIMITED)
    assert "final focus 30.00m" in dof.readout


def test_missing_planning_proofs_leave_other_outputs_exactly_unchanged():
    """From the astra probes: without the proofs, depth changes nothing but its own factor."""
    import contextlib
    import io
    import json

    def stable(rec):
        return json.dumps({"settings": [rec.iso, rec.aperture, rec.shutter_s, rec.iso_label,
                                        rec.aperture_label, rec.shutter_label],
                           "factors": [(c.axis, [vars(f) for f in c.sub_factors
                                                 if f.name != "Depth of Field"]) for c in rec.cards],
                           "exposure_reference": rec.exposure_reference}, sort_keys=True)
    with patch.object(brain.embed_index, "recommend", return_value={"f_number": 4.0}), \
            contextlib.redirect_stdout(io.StringIO()):
        for ev in (-2.0, 5.0, 10.0, 15.0):
            for focal, current, focus in ((None, True, 30.0), (24.0, False, 30.0), (75.0, True, None)):
                kwargs = dict(focal_mm=focal, focal_is_current=current, focus_distance_m=focus,
                              scene_class="landscape")
                with_subject = _REAL_RECOMMEND_SETTINGS("unused", ev, subject_distance_m=30.0, **kwargs)
                without = _REAL_RECOMMEND_SETTINGS("unused", ev, subject_distance_m=None, **kwargs)
                assert stable(with_subject) == stable(without)
                before = stable(with_subject)
                constraint_cards.settle_depth_after_focus(
                    with_subject, af_search="driven_half_press", final_distance_m=None,
                    focal_mm=focal if current else None, sensor=physics.A7II)
                assert stable(with_subject) == before




# --- astra re-check of 27a436c4 + 8a86000d -------------------------------------------------------
def test_usb_fallback_keeps_the_first_taste_when_retrieval_changes(rig, monkeypatch):  # noqa: F811
    """[P1] The no-proof fallback re-runs only the deterministic solve with the FIRST taste: a second
    retrieval that fails (or differs) must not move the applied exposure."""
    _usb_depth_rig(rig, monkeypatch, "searched")
    calls = {"retrieval": 0, "args": None}

    def flaky(*args, **kwargs):
        calls["retrieval"] += 1
        if calls["retrieval"] > 1:
            raise RuntimeError("retrieval down on the second call")
        return {"f_number": 4.0}

    def recommend(*args, **kwargs):
        calls["args"] = calls["args"] or (args, dict(kwargs))
        with patch.object(brain.embed_index, "recommend", side_effect=flaky):
            return _REAL_RECOMMEND_LIVE(*args, **kwargs)
    rig.a7iii.side_effect = recommend
    shot = rig.real.shoot(None, variant_id="natural")
    assert shot.ok and calls["retrieval"] == 1                     # taste retrieved exactly once
    args, kwargs = calls["args"]
    with patch.object(brain.embed_index, "recommend", return_value={"f_number": 4.0}):
        expected = _REAL_RECOMMEND_LIVE(*args, **{**kwargs, "focus_distance_m": None})
    applied = _applied(rig)
    assert (applied["iso"], applied["aperture"], applied["shutter"]) == (
        expected.iso_label, expected.aperture_label, expected.shutter_label)
    assert expected.aperture_label == "4"


def _wifi_final_frame(tmp_path, monkeypatch, *, event_focal, taste_f, reader_focal):
    """A Wi-Fi landscape shot at 30 m whose postview EXIF says 30 m; the postview focal reader returns
    `reader_focal` (None = unreadable, as with the current reader)."""
    import contextlib
    import io
    import types
    import test_wifi_intent_propagation as wifi_harness
    import wifi_autopilot
    import wifi_engine
    cam = wifi_harness.FakeCamera()
    wifi_harness._install_stubs(monkeypatch, tmp_path, cam)
    monkeypatch.setattr(cam, "focal_length_mm", lambda **k: event_focal)
    monkeypatch.setattr(wifi_autopilot, "_LAST_FOCAL_MM_ACTUAL", None)
    monkeypatch.setattr(wifi_autopilot, "vision_banana", types.SimpleNamespace(
        analyze=lambda *a: None, metering_region=lambda *a: None,
        subject_distance_m=lambda *a: 30.0, dof_depth_m=lambda *a: None,
        scene_class=lambda *a: "landscape"))
    monkeypatch.setattr(wifi_autopilot.live_verify, "focal_from_exif",
                        lambda path, default=None: reader_focal)

    def download(url, dest):
        exif = Image.Exif()
        exif[37382] = 30.0
        Image.new("RGB", (48, 32), "red").save(dest, exif=exif)
        return dest
    cam.download = download
    seen = {}

    def recommend(*args, **kwargs):
        with patch.object(brain.embed_index, "recommend", return_value={"f_number": taste_f}), \
                contextlib.redirect_stdout(io.StringIO()):
            seen["rec"] = _REAL_RECOMMEND_SETTINGS(*args, **kwargs)
            return seen["rec"]
    monkeypatch.setattr(wifi_autopilot.brain, "recommend_settings", recommend)
    shot = wifi_engine.WiFiEngine().shoot(None, applied_plan=wifi_harness._plan())
    assert shot.ok, shot
    return seen["rec"], shot


def test_wifi_never_assesses_with_the_pre_shot_focal(tmp_path, monkeypatch):
    """[P2] The frame was 75 mm but its focal is unreadable: the pre-shot 24 mm must not be used, so
    the assessment stays pending instead of certifying."""
    rec, _ = _wifi_final_frame(tmp_path, monkeypatch, event_focal=24.0, taste_f=2.8, reader_focal=None)
    dof = rec.card("APERTURE").sub_factors[0]
    assert dof.status == constraint_cards.PENDING, dof
    assert rec.aperture_plan.get("final_meets_requirement") is None


def test_wifi_assesses_the_verified_captured_aperture_not_the_recommended_one(tmp_path, monkeypatch):
    """[P2] Recommended f/8 but the body's ladder stops at f/5.6, which is verified and captured. At
    75 mm and 30 m, f/8 would cover the landscape (hyperfocal ~24.5 m) but f/5.6 does not (~35 m)."""
    rec, shot = _wifi_final_frame(tmp_path, monkeypatch, event_focal=0.0, taste_f=8.0,
                                  reader_focal=75.0)
    assert rec.aperture == 8.0                                   # the recommendation is unchanged
    dof = rec.card("APERTURE").sub_factors[0]
    assert dof.status == constraint_cards.LIMITED, dof
    assert rec.aperture_plan["final_f_number_assessed"] == 5.6


# --- F-73-pending-reason: a pending final assessment names the datum that is actually missing ------
@pytest.mark.parametrize("missing, kwargs, reason", [
    ("final focal", dict(focal_mm=None), "focal length unavailable for the final frame"),
    ("captured aperture", dict(focal_mm=75.0, captured_f_number=None), "captured aperture not verified"),
    ("final focus", dict(focal_mm=75.0, final_distance_m=None), "focus not verified at the subject"),
])
def test_a_pending_final_assessment_names_the_missing_datum(missing, kwargs, reason):
    """The final frame proved its focus distance, so a missing focal length or an unverified
    aperture must not be reported as an unverified focus (and the reverse)."""
    with patch.object(brain.embed_index, "recommend", return_value={"f_number": 2.8}):
        rec = _REAL_RECOMMEND_SETTINGS("unused.jpg", 15.0, scene_class="landscape",
                                       **_FOCALS["measured"], **_FOCUS["exif"])
    assert aperture_optimizer.plan_is_driven(rec.aperture_plan)
    note = constraint_cards.settle_depth_after_focus(
        rec, af_search="driven_half_press", sensor=physics.A7II,
        **{"final_distance_m": 30.0, **kwargs})
    dof = rec.card("APERTURE").sub_factors[0]
    assert dof.status == constraint_cards.PENDING, (missing, dof)
    assert reason in dof.readout and reason in note.lower(), (missing, dof.readout, note)
    others = {"focal length unavailable", "captured aperture not verified",
              "focus not verified"} - {reason.split(" for ")[0].replace(" at the subject", "")}
    assert not any(other in dof.readout for other in others), (missing, dof.readout)
    assert rec.aperture_plan["final_meets_requirement"] is None
