"""M1 brain — fuse the taste layer (retrieval) with the physics layer (exposure).

Given a scene image and how bright the scene is (its EV at ISO 100), recommend the
final ISO / aperture / shutter:
  - retrieval (embed_index) says what *look* great photographers chose for a scene
    like this — the aperture/focal taste,
  - physics (physics.py) makes that look correctly exposed and blur-safe for the real
    scene (raise ISO, then open aperture, then accept a shutter below the blur floor).

    backend/.venv-ml/bin/python backend/brain.py <scene_image> <scene_EV100> [focal_mm]
"""
from __future__ import annotations

import math
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import aperture_optimizer
import camera_caps
import constraint_cards
import embed_index
import lens_optics
import physics
import sensor_noise


LIVE_POLICY_ISO_CEILING = 6400  # conservative application policy for rigs without a noise table
# recommend_live's geometry when no rig profile supplies one.
UNCALIBRATED_GEOMETRY = physics.SensorProfile("Uncalibrated full-frame geometry assumption", 36, 24,
                                              width_px=6000, max_usable_iso=LIVE_POLICY_ISO_CEILING)


def usable_iso_range(rig=None, *, existing_profile: bool = False) -> tuple[int, int]:
    """(base ISO, usable-ISO ceiling) this rig's recommendation applies: the A7 II's base ISO and
    DR-derived ceiling (recommend_settings' defaults) for the existing profile; otherwise the rig's
    (or the uncalibrated) sensor base ISO and a measured JPEG-noise table's own ceiling or the ISO6400
    policy. One source for anything that must stay inside the same quality gate (e.g. the ISO a
    Fixed HDR bracket moves to for shutter headroom); extended low ISOs sit below it."""
    if existing_profile:
        base = physics.A7II.base_iso
        return base, int(sensor_noise.A7II_NOISE.usable_iso_ceiling(base_iso=base))
    geometry = rig.sensor if rig is not None and getattr(rig, "sensor", None) is not None else UNCALIBRATED_GEOMETRY
    if rig is not None and getattr(rig, "sensor", None) is not None and rig.noise is not None:
        return geometry.base_iso, int(rig.noise.usable_iso_ceiling(base_iso=geometry.base_iso))
    return geometry.base_iso, LIVE_POLICY_ISO_CEILING


def usable_iso_ceiling(rig=None, *, existing_profile: bool = False) -> int:
    """The usable-ISO ceiling of usable_iso_range."""
    return usable_iso_range(rig, existing_profile=existing_profile)[1]


def taste_fell_back(rec) -> bool:
    """True when recommend_settings could not reach taste retrieval (the labelled fallback)."""
    return any(str(note).startswith("Taste unavailable") for note in rec.notes)


def _fmt_shutter(shutter_s: float) -> str:
    return f"1/{1 / shutter_s:.0f}s" if shutter_s < 1 else f"{shutter_s:.1f}s"


def recommend_settings(image_path: str, scene_ev100: float, focal_mm: float | None = None,
                       lens: physics.LensProfile = physics.TAMRON_28_75,
                       sensor: physics.SensorProfile = physics.A7II,
                       lens_opt: lens_optics.LensOptics = lens_optics.TAMRON_28_75_OPTICS,
                       noise: sensor_noise.NoiseModel = sensor_noise.A7II_NOISE,
                       caps: camera_caps.CameraCapabilities = camera_caps.A7II_CAPS,
                       stability: float = 1.0,
                       subject_motion_floor_s: float | None = None,
                       subject_distance_m: float | None = None,
                       dof_depth_m: float | None = None,
                       scene_class: str | None = None,
                       distance_source: str | None = None,
                       ev_reference: str = physics.EV_THROUGH_THIS_LENS,
                       transmission: lens_optics.LensTransmission | None = None,
                       fallback_aperture: float | None = None,
                       focus_distance_m: float | None = None,
                       focal_is_current: bool = True,
                       taste_preference: dict | None = None,
                       ) -> constraint_cards.Recommendation:
    """The full autopilot recommendation for one scene — three explainable cards (SHUTTER /
    APERTURE / ISO) plus the *settable* (snapped) ISO / aperture / shutter the camera can take.

    `scene_ev100` is a through-this-lens meter reading unless `ev_reference` says it is a
    lens-independent scene luminance; only then does the lens `transmission` (default: the optics
    profile's, which may be unknown) move the exposure. The aperture is planned from the subject
    distance and scene class (aperture_optimizer.plan_aperture); an explicit `dof_depth_m` overrides
    the scene's depth fraction."""
    taste_note = None
    if taste_preference is not None:
        # A re-solve of the same shot reuses the first solve's preference: no second retrieval.
        taste = {"f_number": float(taste_preference["f_number"])}
        taste_note = taste_preference.get("note")
    else:
        try:
            taste = embed_index.recommend(image_path)  # {f_number, iso, focal_mm} from similar excellent photos
        except Exception as exc:  # noqa: BLE001 — CLIP down/warming must never abort a shot after the meter frame
            # Same labelled fallback as recommend_live: keep the current aperture as the preference.
            preference = fallback_aperture if fallback_aperture and math.isfinite(fallback_aperture) else 8.0
            taste = {"f_number": preference}
            taste_note = (f"Taste unavailable ({type(exc).__name__}); retained f/{preference:g} as the aperture "
                          "preference. Physics constraints only.")
    # Physics needs the CAMERA-reported focal; the retrieved focal is taste metadata only (the
    # index mixes phone / crop / full-frame focals, so it's unsafe to drive the shutter floor).
    focal = focal_mm if focal_mm is not None else 50.0
    # Both shake AND subject motion need the shutter fast enough, so the binding floor is the
    # STRICTER (smaller-seconds) of the two. None => no subject-motion reading => shake floor only.
    vib_floor = physics.handheld_shutter_floor(focal, sensor.crop_factor, stability)
    floor = vib_floor if subject_motion_floor_s is None else min(vib_floor, subject_motion_floor_s)
    # T-stop (not f-stop) governs light, but a through-this-lens meter reading already contains the
    # glass's loss; only a lens-independent scene luminance is dimmed by the measured offset.
    transmission = transmission if transmission is not None else lens_opt.transmission
    target_ev = physics.lens_adjusted_ev100(scene_ev100, ev_reference=ev_reference,
                                            t_stop_offset_stops=transmission.t_stop_offset_stops)
    max_iso = noise.usable_iso_ceiling(base_iso=sensor.base_iso)  # derived from DR, not hand-set
    # Aperture baseline (B04): with a subject distance and a depth requirement (scene fraction, or the
    # hyperfocal rule for a landscape) the optimizer picks the aperture that COVERS it
    # (physics-authoritative); otherwise keep the taste-clamped pick and say the distance is unknown.
    # The diffraction cap + exposure solve still override downstream.
    taste_aperture = min(max(taste["f_number"], lens.min_aperture), lens.max_aperture)
    aperture_plan = aperture_optimizer.plan_aperture(
        taste_aperture, subject_distance_m=subject_distance_m, scene_class=scene_class,
        # Only a focal measured for THIS frame plans depth; a cached/default one sets the floor only.
        dof_depth_m=dof_depth_m, focal_mm=focal_mm if focal_is_current else None, sensor=sensor,
        lens_opt=lens_opt,
        apertures=caps.apertures, distance_source=distance_source, focus_distance_m=focus_distance_m)
    baseline_aperture = aperture_plan["f_number"]
    dof_exceeds = aperture_plan["status"] == aperture_optimizer.STATUS_DOF_EXCEEDS
    exposure = physics.solve_exposure(
        target_ev,
        baseline_aperture=baseline_aperture,
        shutter_floor_s=floor,
        lens_min_aperture=lens.min_aperture,
        lens_max_aperture=lens.max_aperture,
        base_iso=sensor.base_iso,
        max_iso=max_iso,
        diffraction_max_aperture=sensor.diffraction_hard_aperture,
    )
    # Snap the continuous solve onto real, settable camera values, then re-describe from those.
    iso_v, iso_label = caps.snap_iso(exposure.iso)
    f_v, f_label = caps.snap_aperture(exposure.aperture)
    shutter_v, shutter_label = caps.snap_shutter(exposure.shutter_s)
    snapped = physics.ExposureSolution(
        iso=iso_v, aperture=f_v, shutter_s=shutter_v,
        hit_iso_ceiling=exposure.hit_iso_ceiling, below_shutter_floor=exposure.below_shutter_floor,
        diffraction_limited=exposure.diffraction_limited, overexposed=exposure.overexposed,
        notes=list(exposure.notes))
    if subject_motion_floor_s is not None and subject_motion_floor_s < vib_floor:
        snapped.notes.append(f"shutter floor tightened to 1/{1/floor:.0f}s to freeze subject motion")
    ev_error = snapped.ev100_achieved - target_ev
    if abs(ev_error) > 0.4:
        snapped.notes.append(f"snapped settings expose {ev_error:+.1f} EV from ideal")
    snapped.notes.append(aperture_plan["note"])
    reopened = aperture_optimizer.reconcile_plan(aperture_plan, f_v, focal, sensor)
    if reopened:
        snapped.notes.append(reopened)
    exposure_reference = _exposure_reference(ev_reference, transmission, scene_ev100, target_ev)
    if not transmission.known and ev_reference == physics.EV_SCENE_LUMINANCE:
        snapped.notes.append("Lens transmission is unmeasured, so a scene-luminance exposure is not "
                             "corrected for the light the glass absorbs.")
    cards = constraint_cards.build_cards(
        snapped, focal_mm=focal, shutter_floor_s=floor, sensor=sensor,
        lens=lens_opt, noise=noise, stability=stability, base_iso=sensor.base_iso,
        vib_floor_s=vib_floor, subject_motion_floor_s=subject_motion_floor_s,
        subject_distance_m=(subject_distance_m if aperture_optimizer.plan_has_zone(aperture_plan)
                            else None),
        is_night=scene_ev100 < 3.0, dof_exceeds=dof_exceeds, transmission=transmission,
        ev_reference=ev_reference, aperture_plan=aperture_plan)
    if taste_note:
        snapped.notes.append(taste_note)
    rec = constraint_cards.Recommendation(
        iso=iso_v, aperture=f_v, shutter_s=shutter_v, cards=cards, notes=snapped.notes,
        iso_label=iso_label, aperture_label=f_label, shutter_label=shutter_label,
        aperture_plan=aperture_plan, exposure_reference=exposure_reference,
        taste_preference={"f_number": float(taste["f_number"]), "note": taste_note})
    _print_recommendation(rec, scene_ev100, focal, taste)
    return rec


def _exposure_reference(ev_reference: str, transmission, scene_ev100: float,
                        target_ev: float) -> dict:
    """Evidence of how the lens transmission entered the exposure target (JSON-safe)."""
    return {"ev_reference": ev_reference, "scene_ev100": round(scene_ev100, 4),
            "target_ev100": round(target_ev, 4),
            "applied_offset_stops": round(scene_ev100 - target_ev, 4),
            "lens_transmission": transmission.to_dict()}


def _print_recommendation(rec, scene_ev100: float, focal: float, taste: dict) -> None:
    print("\n=== AUTOPILOT RECOMMENDATION ===")
    print(f"  scene: EV{scene_ev100:.1f} at {focal:.0f}mm   (taste ~ f/{taste['f_number']})")
    print(f"  >>> ISO {rec.iso_label}   f/{rec.aperture_label}   {rec.shutter_label}  (settable)")
    for c in rec.cards:
        print(f"  [{c.axis:8}] {c.value:8} [{c.status:7}] {c.reason}")
        for s in c.sub_factors:
            print(f"        - {s.name}: {s.readout}  [{s.status}]")


if __name__ == "__main__":
    if len(sys.argv) < 3:
        print(f"usage: {sys.argv[0]} <scene_image> <scene_EV100> [focal_mm]")
        raise SystemExit(1)
    recommend_settings(sys.argv[1], float(sys.argv[2]),
                       float(sys.argv[3]) if len(sys.argv) > 3 else None)


def recommend_live(image_path: str, scene_ev100: float, model: str,
                   live_caps: camera_caps.CameraCapabilities, current_aperture: float,
                   focal_mm: float | None,
                   subject_motion_floor_s: float | None = None,
                   *, rig=None,
                   subject_distance_m: float | None = None,
                   scene_class: str | None = None,
                   distance_source: str | None = None,
                   ev_reference: str = physics.EV_THROUGH_THIS_LENS,
                   focus_distance_m: float | None = None,
                   taste_preference: dict | None = None,
                   ) -> constraint_cards.Recommendation:
    """Conservative retrieval + exposure recommendation using this body's live ladders.

    When a `rig` (rig_profile.RigProfile) is supplied it drives the sensor geometry, the ISO
    ceiling, and the honest sub-factor/notes labels: a measured JPEG-noise table caps ISO at its
    measured ceiling, while a geometry_only rig keeps the conservative ISO6400 policy. Dynamic
    Range stays PENDING regardless (JPEG-domain noise is not a DR measurement), and Lens Sharpness
    Profile stays PENDING unless a lens optics profile exists for the mounted lens.

    With no `rig`, the original blanket behaviour is preserved: a 36x24mm, 6000px approximation and
    an ISO6400 policy ceiling. No A7 II noise/transmission/MTF calibration is ever transferred.
    Exposure is still verified by the caller's JPEG feedback. Settings here are computed, never
    applied.

    `scene_ev100` is a through-this-lens reading unless `ev_reference` says it is a scene
    luminance; then the rig's published lens transmission (if any) raises the exposure. With a
    subject distance and scene class, the aperture preference is the depth plan (B04) instead of
    taste; with neither, taste/current is kept and the plan says the distance is unknown.
    """
    profile = camera_caps.model_profile(model)
    if not profile["supported_body"] or model != live_caps.name:
        raise ValueError("A supported matching body and its own live capability ladder are required")
    if not math.isfinite(scene_ev100) or not -20 <= scene_ev100 <= 30:
        raise ValueError("scene_ev100 must be finite and within the supported exposure range")
    if not math.isfinite(current_aperture) or current_aperture <= 0:
        raise ValueError("current_aperture must be a positive measured f-number")
    if focal_mm is not None and (not math.isfinite(focal_mm) or focal_mm <= 0):
        raise ValueError("focal_mm must be positive when supplied")
    if subject_motion_floor_s is not None and (
            not math.isfinite(subject_motion_floor_s) or subject_motion_floor_s <= 0):
        raise ValueError("subject_motion_floor_s must be positive when supplied")
    measured_noise = False
    iso_floor, iso_ceiling = usable_iso_range(rig)
    lens_profiled = False
    if rig is not None and getattr(rig, "sensor", None) is not None:
        geometry = rig.sensor
        measured_noise = rig.noise is not None
        lens_profiled = bool(getattr(rig, "lens_profiled", False))
        notes = list(getattr(rig, "notes", []))
        if measured_noise:
            notes.append(f"Measured JPEG-domain noise table: usable-ISO ceiling {iso_ceiling}. "
                         "Dynamic range and lens MTF remain unmeasured (JPEG-domain noise only).")
        else:
            notes.append("Sensor noise is uncalibrated; the ISO6400 ceiling is a conservative "
                         "application policy, not a measured sensor limit.")
        if getattr(rig, "ibis", False):
            notes.append("Body has in-body stabilization, but its stabilization stops are "
                         "unmeasured, so no shutter-floor credit is taken (stability=1).")
    else:
        geometry = UNCALIBRATED_GEOMETRY
        notes = ["Uncalibrated: no measured sensor noise, lens transmission or lens MTF model.",
                 "Geometry assumption: 36x24mm, 6000px; no stabilization credit (stability=1).",
                 "ISO6400 is a conservative application policy, not a measured sensor limit."]
    transmission = getattr(rig, "lens_transmission", None) or lens_optics.UNKNOWN_TRANSMISSION
    target_ev = physics.lens_adjusted_ev100(scene_ev100, ev_reference=ev_reference,
                                            t_stop_offset_stops=transmission.t_stop_offset_stops)
    if not transmission.known and ev_reference == physics.EV_SCENE_LUMINANCE:
        notes.append("Lens transmission is unmeasured, so a scene-luminance exposure is not "
                     "corrected for the light the glass absorbs.")
    focal = focal_mm if focal_mm is not None else 50.0
    vibration_floor = physics.handheld_shutter_floor(focal, geometry.crop_factor, stability=1)
    floor = min(vibration_floor, subject_motion_floor_s or vibration_floor)
    if focal_mm is None:
        notes.append("Focal length unavailable: assumed 50mm for the handheld shutter floor.")
    taste_aperture = current_aperture
    taste_status = constraint_cards.PENDING
    if taste_preference is not None:
        # A re-solve of the same shot reuses the first solve's preference: no second retrieval.
        taste_aperture = float(taste_preference["f_number"])
        taste_status = taste_preference.get("status", constraint_cards.PENDING)
        taste_note = taste_preference.get("note")
    else:
        try:
            value = float(embed_index.recommend(image_path)["f_number"])
            if not math.isfinite(value) or value <= 0:
                raise ValueError("invalid retrieved aperture")
            taste_aperture = value
            taste_status = constraint_cards.OK
            taste_note = f"Retrieval suggests f/{value:g}; live exposure constraints take precedence."
        except Exception as exc:
            taste_note = f"Taste unavailable ({type(exc).__name__}); retained current aperture as preference."
    if taste_note:
        notes.append(taste_note)
    # Filter hard gates before ranking. Numeric live settings are permission to choose a
    # value, not proof of a sensor's usable quality ceiling or the mounted lens's MTF.
    isos = [iso for iso in live_caps.isos if iso_floor <= iso <= iso_ceiling]
    apertures = [f for f in live_caps.apertures if 0 < f <= geometry.diffraction_hard_aperture]
    shutters = [t for t in live_caps.shutters if math.isfinite(t) and 0 < t <= 30]
    if not isos or not apertures or not shutters:
        raise ValueError("Live choices provide no exposure within the conservative policy")
    # B04: the depth plan replaces taste as the aperture preference when it has a requirement. The
    # Tamron 28-75 is the only profiled lens (rig.lens_profiled), so only it earns a sweet-spot
    # tie-break; any other lens ties toward the wider aperture.
    aperture_plan = aperture_optimizer.plan_aperture(
        taste_aperture, subject_distance_m=subject_distance_m, scene_class=scene_class,
        focal_mm=focal_mm, sensor=geometry,
        lens_opt=lens_optics.TAMRON_28_75_OPTICS if lens_profiled else None,
        apertures=apertures, distance_source=distance_source, focus_distance_m=focus_distance_m)
    planned = aperture_plan["status"] in (aperture_optimizer.STATUS_OK,
                                          aperture_optimizer.STATUS_DOF_EXCEEDS)
    preferred_aperture = aperture_plan["f_number"] if planned else taste_aperture
    notes.append(aperture_plan["note"])
    candidates = []
    for aperture in apertures:
        aperture_cost = abs(math.log2(aperture / preferred_aperture))
        for iso in isos:
            for shutter in shutters:
                achieved_ev = physics.ev100(aperture, shutter) - math.log2(iso / 100)
                error = abs(achieved_ev - target_ev)
                below_floor = shutter > floor + 1e-9
                # First require achievable exposure, then respect motion where feasible,
                # then preserve the taste aperture and minimize ISO. Outside the exposure
                # tolerance report the closest achievable exposure explicitly.
                motion_violation = max(0.0, math.log2(shutter / floor))
                rank = (error > .34, error if error > .34 else 0,
                        below_floor, motion_violation, aperture_cost, iso, error, shutter)
                candidates.append((rank, iso, aperture, shutter, achieved_ev - target_ev))
    _, iso, aperture, shutter, ev_error = min(candidates, key=lambda item: item[0])
    below_floor = shutter > floor + 1e-9
    if below_floor:
        notes.append("Available exposure could not meet the modeled motion floor; the closest achievable shutter was prioritized before aperture preference and lower ISO. Blur is possible.")
    if abs(ev_error) > .34:
        notes.append(f"Available settings differ from the target by {ev_error:+.2f} EV.")
    reopened = aperture_optimizer.reconcile_plan(aperture_plan, aperture, focal, geometry)
    if reopened:
        notes.append(reopened)
    if subject_motion_floor_s is None:
        notes.append("Subject motion was not measured; the vibration floor alone is modeled.")
    factor = constraint_cards.SubFactor
    card = constraint_cards.ConstraintCard
    cards = [
        card("SHUTTER", _fmt_shutter(shutter),
             "Modeled motion floor not met" if below_floor else "Meets the modeled motion floor",
             [factor("Vibrations", f"1/{1/vibration_floor:.0f}s modeled handheld floor",
                     constraint_cards.LIMITED if shutter > vibration_floor else constraint_cards.OK),
              factor("Scene Movement", (f"1/{1/subject_motion_floor_s:.0f}s estimated floor"
                     if subject_motion_floor_s is not None else "Motion sample unavailable"),
                     (constraint_cards.LIMITED if shutter > subject_motion_floor_s
                      else constraint_cards.OK) if subject_motion_floor_s is not None
                     else constraint_cards.PENDING)]),
        card("APERTURE", f"f/{aperture:g}", "Live aperture selected within exposure and diffraction limits",
             [factor("Taste", f"Preferred f/{taste_aperture:g}", taste_status),
              factor("Lens Sharpness Profile",
                     "Profiled lens optics (MTF) available" if lens_profiled
                     else "Mounted lens MTF uncalibrated",
                     constraint_cards.OK if lens_profiled else constraint_cards.PENDING),
              constraint_cards.depth_of_field_subfactor(
                  aperture, focal, geometry,
                  subject_distance_m if aperture_optimizer.plan_has_zone(aperture_plan) else None,
                  aperture_plan["status"] == aperture_optimizer.STATUS_DOF_EXCEEDS, aperture_plan),
              constraint_cards.transmission_subfactor(transmission, aperture, ev_reference)]),
        card("ISO", f"ISO {iso}",
             "Measured JPEG-noise ceiling applied" if measured_noise
             else "Conservative policy applied; sensor quality is uncalibrated",
             [factor("Dynamic Range",
                     "Dynamic range not measured (JPEG-domain noise only)" if measured_noise
                     else "Sensor noise/DR calibration unavailable", constraint_cards.PENDING),
              factor("Noise Ceiling" if measured_noise else "Policy Limit",
                     f"Measured JPEG-noise ceiling ISO {iso_ceiling}" if measured_noise
                     else "ISO6400 maximum, restricted to live choices",
                     constraint_cards.WARNING if iso >= iso_ceiling else constraint_cards.OK)]),
    ]
    if abs(ev_error) > .34:
        cards[2].sub_factors.append(factor("Exposure Target", f"{ev_error:+.2f} EV mismatch",
                                          constraint_cards.LIMITED))
    return constraint_cards.Recommendation(
        iso, aperture, shutter, cards, notes=notes, iso_label=live_caps.snap_iso(iso)[1],
        aperture_label=live_caps.snap_aperture(aperture)[1],
        shutter_label=live_caps.snap_shutter(shutter)[1], aperture_plan=aperture_plan,
        exposure_reference=_exposure_reference(ev_reference, transmission, scene_ev100, target_ev),
        taste_preference={"f_number": taste_aperture, "status": taste_status, "note": taste_note})
