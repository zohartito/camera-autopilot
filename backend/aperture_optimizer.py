"""M1 aperture optimizer (audit M9) — choose the aperture once the brain knows the subject distance.

Physics stays AUTHORITATIVE. Depth-of-field coverage, the diffraction cap, and exposure feasibility
are HARD GATES; among the apertures that pass all three we pick the one closest to the retrieval
"taste", breaking ties toward the sharper aperture (the lens sweet spot). Lexicographic-with-gates,
so a nice-to-have (the sweet spot) can never override a must (cover the subject / avoid diffraction
mush). Without a subject distance (no Vision Banana), the brain skips this and uses its taste-clamped
baseline as before — this only engages when depth is known.

B04 `plan_aperture` states the depth requirement from the subject distance and scene class:
a landscape needs the subject AND infinity sharp (focused on the subject, the subject must lie at or
beyond the hyperfocal distance); a closeup/portrait/generic subject needs a depth of field of a fixed
fraction of its distance. The distance comes from the frame's EXIF SubjectDistance (the lens's own
focus distance) when the body writes it, else the Vision Banana depth estimate; with neither, the
aperture keeps today's taste/current preference and the plan says the distance is unknown.
"""
from __future__ import annotations

from math import inf, isfinite, log2

import lens_optics
import physics


def choose_aperture(taste_f_number: float, subject_distance_m: float, dof_depth_m: float, *,
                    focal_mm: float, sensor: physics.SensorProfile,
                    lens_opt: lens_optics.LensOptics, apertures,
                    exposure_feasible=None) -> tuple[float, str]:
    """Return (chosen_f_number, status). `apertures` is the camera's settable f-number ladder;
    `exposure_feasible(N) -> bool` (optional) rejects apertures the exposure solver can't satisfy.

    Hard gates: cover the subject depth (exact DoF model), stay at/under the diffraction cutoff
    (N <= hard), be exposure-feasible. Soft pick among survivors: closest to taste in stops, then
    the sharper (higher MTF) aperture. If nothing covers the depth short of diffraction, return the
    deepest non-mush aperture and flag `dof_exceeds_diffraction`."""
    def covers_dof(n: float) -> bool:
        # Gate on the project's EXACT depth-of-field model, not a symmetric approximation: the gate
        # must never accept an aperture that comes up a hair short of covering the subject.
        near, far = physics.depth_of_field_m(subject_distance_m, focal_mm, n, sensor.coc_mm)
        return far == float("inf") or (far - near) >= dof_depth_m - 1e-9

    return _choose(taste_f_number, covers_dof, sensor=sensor, lens_opt=lens_opt,
                   apertures=apertures, exposure_feasible=exposure_feasible)


def _choose(taste_f_number: float, covers, *, sensor, lens_opt, apertures,
            exposure_feasible=None) -> tuple[float, str]:
    """Hard gates (covers, diffraction, exposure), then closest to taste, then the sharper aperture
    when a lens optics profile exists, then the wider aperture. `lens_opt` may be None (an
    unprofiled lens): no sharpness claim is made and ties go to the wider aperture."""
    hard_diffraction = sensor.diffraction_hard_aperture
    feasible = [n for n in apertures
                if covers(n) and n <= hard_diffraction + 1e-6
                and (exposure_feasible is None or exposure_feasible(n))]

    if not feasible:
        # Nothing covers the depth: the deepest aperture that still passes the other hard gates
        # (diffraction, then exposure when the caller gives one). If every aperture is past the
        # diffraction cap there is no safe choice; the widest does the least harm.
        non_mush = [n for n in apertures if n <= hard_diffraction + 1e-6]
        exposable = [n for n in non_mush if exposure_feasible is None or exposure_feasible(n)]
        pool = exposable or non_mush
        chosen = max(pool) if pool else min(apertures)
        return chosen, "dof_exceeds_diffraction"

    def cost(n: float) -> tuple[float, float, float]:
        sharpness = lens_opt.sharpness(n, sensor) if lens_opt is not None else 0.0
        return (abs(log2(n / taste_f_number)), -sharpness, n)

    return min(feasible, key=cost), "ok"


# --- B04: the depth requirement from subject distance + scene class --------------------------------
# Per-scene depth-of-field target as a FRACTION of the subject distance. Depth of field scales with
# distance, so a fixed-meters target over-stops close subjects into diffraction (validated live: a
# 0.6m subject demanded f/11 under a fixed 0.25m target). A closeup isolates a thin slice, a portrait
# covers the subject, generic is more generous. A landscape uses the hyperfocal rule instead.
DOF_FRACTION_BY_SCENE = {"closeup": 0.10, "portrait": 0.20, "generic": 0.40}
LANDSCAPE = "landscape"

DISTANCE_EXIF = "exif_subject_distance"
DISTANCE_VISION = "vision_depth_estimate"
_EXIF_INFINITY = 0xFFFFFFFF
_MAX_FINITE_DISTANCE_M = 100_000.0

STATUS_OK = "ok"
STATUS_DOF_EXCEEDS = "dof_exceeds_diffraction"
STATUS_DISTANCE_UNKNOWN = "distance_unknown"
STATUS_NO_REQUIREMENT = "no_depth_requirement"
STATUS_FOCAL_UNKNOWN = "focal_unknown"
STATUS_FOCUS_UNKNOWN = "focus_unknown"

# The ONE rule: depth drives the aperture and is certified only with a focal length measured for
# this shot AND an EXIF-proven focus distance. Anything else is pending, with the missing datum named.
REASON_SUBJECT_UNKNOWN = "subject distance unknown"
REASON_FOCAL_UNKNOWN = "focal length unavailable"
REASON_FOCUS_UNKNOWN = "focus distance unknown (no EXIF focus distance)"
REASON_NO_REQUIREMENT = "no depth requirement (no subject or landscape established)"
REASON_FOCUS_NOT_VERIFIED = "focus not verified at the subject"
REASON_FINAL_FOCAL_UNKNOWN = "focal length unavailable for the final frame"
REASON_APERTURE_NOT_VERIFIED = "captured aperture not verified"
REASON_SENSOR_UNKNOWN = "sensor profile unavailable"
_PENDING_REASONS = {STATUS_DISTANCE_UNKNOWN: REASON_SUBJECT_UNKNOWN,
                    STATUS_FOCAL_UNKNOWN: REASON_FOCAL_UNKNOWN,
                    STATUS_FOCUS_UNKNOWN: REASON_FOCUS_UNKNOWN,
                    STATUS_NO_REQUIREMENT: REASON_NO_REQUIREMENT}


def plan_is_driven(plan: dict | None) -> bool:
    """True when depth chose the aperture (measured focal + EXIF focus + a requirement)."""
    return bool(plan) and plan.get("status") in (STATUS_OK, STATUS_DOF_EXCEEDS)


def plan_has_zone(plan: dict | None) -> bool:
    """True when a sharp zone can honestly be drawn on the card: only for a driven plan."""
    return plan_is_driven(plan)


def depth_pending_reason(plan: dict | None) -> str | None:
    """Why depth is not certified, from the CURRENT state (final focus first), or None."""
    if not plan:
        return REASON_SUBJECT_UNKNOWN
    if plan.get("final_focus_checked"):
        if plan.get("final_meets_requirement") is None:
            return plan.get("final_pending_reason") or REASON_FOCUS_NOT_VERIFIED
        return None
    return _PENDING_REASONS.get(plan.get("status"))


def required_dof_depth_m(scene_class, subject_distance_m) -> float | None:
    """How much subject depth (m) to keep sharp: the scene's fraction of the distance, or None."""
    fraction = DOF_FRACTION_BY_SCENE.get(scene_class)
    if fraction is None or subject_distance_m is None or not isfinite(subject_distance_m):
        return None
    return fraction * subject_distance_m


def subject_distance_from_exif(value) -> float | None:
    """EXIF SubjectDistance (tag 0x9206, RATIONAL metres) -> metres, inf, or None.

    Per the EXIF standard a numerator of 0xFFFFFFFF means infinity and 0 means unknown. Accepts the
    (numerator, denominator) tuple, a PIL IFDRational, or a plain number; anything else is None."""
    if value is None or isinstance(value, bool):
        return None
    try:
        if isinstance(value, tuple):
            if len(value) != 2:
                return None
            numerator, denominator = float(value[0]), float(value[1])
        elif hasattr(value, "numerator") and hasattr(value, "denominator"):
            # PIL's IFDRational may carry float components (IFDRational(1.5)); keep them exact.
            numerator, denominator = float(value.numerator), float(value.denominator)
        else:
            numerator, denominator = float(value), 1.0
        if numerator == _EXIF_INFINITY:
            return inf
        if not denominator:
            return None
        metres = numerator / denominator
    except (TypeError, ValueError, OverflowError, ZeroDivisionError):
        return None
    if not isfinite(metres) or metres <= 0 or metres > _MAX_FINITE_DISTANCE_M:
        return None
    return metres


def _usable_distance(value) -> bool:
    return value is not None and (value == inf or (isfinite(value) and value > 0))


def resolve_subject_distance(exif_distance_m, vision_distance_m) -> tuple[float | None, str | None]:
    """The SUBJECT's distance, the thing depth of field must protect: the vision model's detected
    subject range first; the lens's EXIF focus distance stands in only when vision has none. EXIF
    remains the focus plane (plan_aperture's `focus_distance_m`) the subject is judged against.
    (None, None) when neither is usable."""
    if vision_distance_m is not None and isfinite(vision_distance_m) and vision_distance_m > 0:
        return vision_distance_m, DISTANCE_VISION
    if _usable_distance(exif_distance_m):
        return exif_distance_m, DISTANCE_EXIF
    return None, None


def plan_aperture(taste_f_number: float, *, subject_distance_m, scene_class, focal_mm: float | None,
                  sensor: physics.SensorProfile, lens_opt, apertures, dof_depth_m=None,
                  distance_source=None, exposure_feasible=None, focus_distance_m=None) -> dict:
    """Choose the aperture from the subject distance and the depth the scene needs (B04).

    Returns a JSON-safe plan: `f_number` (a ladder value, or the unchanged taste when nothing is
    planned), `status` (ok / dof_exceeds_diffraction / distance_unknown / no_depth_requirement),
    `rule` (subject_depth / hyperfocal / focused_at_infinity / None), the requirement, the sharp
    zone at the chosen aperture and a plain `note`. An explicit `dof_depth_m` overrides the scene
    fraction. The diffraction cap and lens sweet-spot tie-break are those of choose_aperture.

    `focus_distance_m` is the lens's own focus plane (EXIF). When it differs from the subject, the
    subject's interval must fall inside the sharp zone around that plane; otherwise the plane is
    assumed to be on the subject."""
    plan = {"f_number": taste_f_number, "status": STATUS_DISTANCE_UNKNOWN, "rule": None,
            "subject_distance_m": None, "distance_source": None, "scene_class": scene_class,
            "required_depth_m": None, "near_m": None, "far_m": None, "hyperfocal_m": None,
            "taste_f_number": taste_f_number,
            "sweet_spot_f": lens_opt.optimal_aperture(sensor) if lens_opt is not None else None,
            "diffraction_cap_f": round(sensor.diffraction_hard_aperture, 2), "note": ""}
    distance = subject_distance_m
    if distance is None or not (distance == inf or (isfinite(distance) and distance > 0)):
        plan["note"] = ("Subject distance unknown: the aperture keeps the taste/current preference "
                        "and depth of field is not planned.")
        return plan
    plan["subject_distance_m"] = distance if isfinite(distance) else "inf"
    plan["distance_source"] = distance_source
    # The REQUIREMENT depends only on the subject and scene. It is kept even when depth cannot choose
    # the aperture, so the captured aperture can still be assessed against the final frame's proofs.
    if distance == inf and scene_class != LANDSCAPE:
        plan.update(status=STATUS_NO_REQUIREMENT,
                    note="The subject distance is infinity but no landscape is established, so no "
                         "depth requirement is set and the taste/current aperture is kept.")
        return plan
    if distance == inf:
        rule, depth, interval = "focused_at_infinity", None, None
    else:
        if dof_depth_m is not None and not (isfinite(dof_depth_m) and dof_depth_m > 0):
            raise ValueError(f"dof_depth_m must be a finite positive depth, got {dof_depth_m!r}")
        depth = dof_depth_m if dof_depth_m is not None else required_dof_depth_m(scene_class, distance)
        if depth is not None:
            rule, interval = "subject_depth", [distance - depth / 2.0, distance + depth / 2.0]
        elif scene_class == LANDSCAPE:
            rule, interval = "hyperfocal", [distance, "inf"]
        else:
            plan.update(status=STATUS_NO_REQUIREMENT,
                        note=f"Subject at {distance:.2f} m but the scene class is unknown, so no "
                             "depth requirement is set and the taste/current aperture is kept.")
            return plan
    plan.update(rule=rule, required_depth_m=depth, subject_interval_m=interval)

    if focal_mm is None or not (isfinite(focal_mm) and focal_mm > 0):
        # Depth of field scales with focal length squared: a fallback focal would plan a
        # different aperture (50 mm vs 105 mm is ~f/4 vs ~f/13), so nothing is planned.
        plan.update(status=STATUS_FOCAL_UNKNOWN,
                    note="Focal length unavailable: depth of field is not planned and the "
                         "taste/current aperture is kept.")
        return plan
    if not _usable_distance(focus_distance_m):
        # A vision range is the subject to protect, never proof of where the lens is focused.
        plan.update(status=STATUS_FOCUS_UNKNOWN,
                    note="Focus distance unknown (no EXIF focus distance): depth of field is not "
                         "planned and the taste/current aperture is kept.")
        return plan
    focus = focus_distance_m
    plan["focus_distance_m"] = focus if isfinite(focus) else "inf"
    plan["focus_distance_source"] = DISTANCE_EXIF
    separate = focus != distance
    if rule == "focused_at_infinity":
        # Depth is no constraint, but the ladder, diffraction cap and exposure gates still are.
        chosen, status = _choose(taste_f_number, lambda n: True, sensor=sensor, lens_opt=lens_opt,
                                 apertures=apertures, exposure_feasible=exposure_feasible)
        plan.update(f_number=chosen, status=status, far_m="inf",
                    note=f"Focused at infinity: everything from the hyperfocal distance on is sharp "
                         f"at any aperture, so f/{chosen:g} follows the taste preference.")
        return plan

    if separate:
        # The lens is focused elsewhere: the subject's own interval must sit inside that zone.
        def covers(n: float) -> bool:
            return _zone_contains(interval, focus, n, focal_mm, sensor)
    elif rule == "subject_depth":
        def covers(n: float) -> bool:
            near, far = physics.depth_of_field_m(distance, focal_mm, n, sensor.coc_mm)
            return far == inf or (far - near) >= depth - 1e-9
    else:
        def covers(n: float) -> bool:
            # Focused on the subject, infinity stays sharp only if the subject is at or past the
            # hyperfocal distance for this aperture.
            return physics.depth_of_field_m(distance, focal_mm, n, sensor.coc_mm)[1] == inf

    chosen, status = _choose(taste_f_number, covers, sensor=sensor, lens_opt=lens_opt,
                             apertures=apertures, exposure_feasible=exposure_feasible)
    near, far = sharp_zone_m(focus, focal_mm, chosen, sensor)
    hyperfocal_m = physics.hyperfocal_mm(focal_mm, chosen, sensor.coc_mm) / 1000.0
    plan.update(f_number=chosen, status=status, rule=rule, required_depth_m=depth,
                near_m=round(near, 4), far_m="inf" if far == inf else round(far, 4),
                hyperfocal_m=round(hyperfocal_m, 4),
                subject_interval_m=(interval if separate
                                    else _subject_interval(rule, distance, depth, near, far)))
    far_text = "infinity" if far == inf else f"{far:.2f} m"
    if separate:
        focus_text = "infinity" if focus == inf else f"{focus:.2f} m"
        plan["note"] = (f"The lens is focused at {focus_text} and the subject is at {distance:.2f} m: "
                        + (f"f/{chosen:g} keeps the subject inside the sharp zone ({near:.2f} m to "
                           f"{far_text})." if status == STATUS_OK else
                           f"no diffraction-safe aperture keeps the subject sharp; f/{chosen:g} is "
                           "the deepest."))
    elif rule == "hyperfocal":
        if status == STATUS_OK:
            plan["note"] = (f"Landscape: f/{chosen:g} puts the hyperfocal distance at "
                            f"{hyperfocal_m:.2f} m, inside the {distance:.2f} m subject, so the "
                            f"subject and infinity are sharp ({near:.2f} m to {far_text}).")
        else:
            plan["note"] = (f"Landscape: the {distance:.2f} m subject is closer than the hyperfocal "
                            f"distance at every diffraction-safe aperture; f/{chosen:g} is the "
                            f"deepest (hyperfocal {hyperfocal_m:.2f} m), so infinity will be soft.")
    elif status == STATUS_OK:
        plan["note"] = (f"f/{chosen:g} keeps {depth:.2f} m of depth sharp around the "
                        f"{distance:.2f} m subject ({near:.2f} m to {far_text}).")
    else:
        plan["note"] = (f"Subject depth ~{depth:.2f} m at {distance:.2f} m exceeds the "
                        f"diffraction-safe depth of field; f/{chosen:g} is the deepest safe aperture.")
    return plan


def _subject_interval(rule: str, distance: float, depth, near: float, far: float) -> list:
    """The stretch of the scene the plan protects, in metres: a landscape keeps [subject, inf]; a
    subject keeps a slab of the required depth around it, centred when that fits inside the planned
    sharp zone and otherwise slid to fit (depth of field extends further behind than in front).
    When no aperture covers the depth, the centred slab is recorded and cannot be met."""
    if rule == "hyperfocal":
        return [distance, "inf"]
    low = distance - depth / 2.0
    if far == inf or far - near >= depth - 1e-9:
        low = max(low, near)
        if far != inf:
            low = min(low, far - depth)
    return [low, low + depth]


def sharp_zone_m(focus_distance_m: float, focal_mm: float, f_number: float, sensor) -> tuple[float, float]:
    """(near, far) sharp limits for any focus distance, including infinity: focused at infinity the
    zone runs from f^2/(N*c) (the hyperfocal distance less one focal length) to infinity."""
    if focus_distance_m == inf:
        return (physics.hyperfocal_mm(focal_mm, f_number, sensor.coc_mm) - focal_mm) / 1000.0, inf
    return physics.depth_of_field_m(focus_distance_m, focal_mm, f_number, sensor.coc_mm)


def _zone_contains(interval, focus_distance_m: float, f_number: float, focal_mm: float,
                   sensor) -> bool:
    low, high = interval
    high = inf if high == "inf" else high
    near, far = sharp_zone_m(focus_distance_m, focal_mm, f_number, sensor)
    return near <= low + 1e-6 and (far == inf or (high != inf and far >= high - 1e-6))


def _interval_covered(plan: dict, focus_distance_m: float, f_number: float, focal_mm: float,
                      sensor) -> bool:
    return _zone_contains(plan["subject_interval_m"], focus_distance_m, f_number, focal_mm, sensor)


def plan_focus_distance(plan: dict):
    """The focus plane the plan was judged at (the lens's EXIF plane, else the subject); inf-safe."""
    focus = plan.get("focus_distance_m", plan.get("subject_distance_m"))
    return inf if focus == "inf" else focus


def requirement_met(plan: dict | None, f_number: float, focal_mm: float, sensor) -> bool | None:
    """Does `f_number` (the FINAL aperture, which an exposure solve may have reopened) still keep the
    plan's protected subject interval sharp at the planned focus distance? None when the plan set
    no requirement."""
    if not plan_is_driven(plan) or plan.get("rule") not in ("hyperfocal", "subject_depth"):
        return None
    return _interval_covered(plan, plan_focus_distance(plan), f_number, focal_mm, sensor)


def reconcile_plan(plan: dict, final_f_number: float, focal_mm: float, sensor) -> str | None:
    """Record the final aperture on the plan; return a note when the final aperture differs from
    the planned one and no longer meets the requirement (so the plan's note is not read as a
    promise the final settings break)."""
    met = requirement_met(plan, final_f_number, focal_mm, sensor)
    plan["final_f_number"] = final_f_number
    plan["final_meets_requirement"] = met
    if met is False and plan["status"] == STATUS_OK:
        return (f"Exposure moved the aperture to f/{final_f_number:g}, so the planned depth "
                f"(f/{plan['f_number']:g}) is not covered in the final settings.")
    return None


FOCUS_BASIS_METER = "meter_frame_focus"
FOCUS_BASIS_AF_AFTER_PLAN = "autofocus_searched_after_plan"
FOCUS_BASIS_MANUAL = "manual_focus"
FOCUS_BASIS_UNCONFIRMED = "meter_focus_unconfirmed"


def reconcile_final_focus(plan: dict, *, af_search, final_distance_m, final_f_number: float | None,
                          focal_mm: float | None, sensor) -> str | None:
    """ASSESS the captured aperture against the final frame's proofs (never changes a setting).

    The final focus distance is proven by the final frame's EXIF, or, for a driven plan whose focus
    provably did not move since the meter frame (the AF lock held, or manual focus), by the meter
    frame's EXIF focus distance. `focal_mm` must be measured for the final frame. Any plan with a
    depth requirement is assessed, including one that did not choose the aperture. `final_f_number`
    is None when the captured aperture could not be verified. Without the proofs a driven plan
    becomes pending, naming each missing datum (final focus, final focal length, captured aperture),
    and an undriven one keeps its planning reason. Returns a plain note when the result is pending, not covered, or
    reverses the planning state; else None."""
    searched = af_search == "driven_half_press"
    manual = af_search == "none_manual_focus"
    held = af_search == "none_already_locked"
    plan["focus_basis"] = (FOCUS_BASIS_AF_AFTER_PLAN if searched else FOCUS_BASIS_MANUAL if manual
                           else FOCUS_BASIS_METER if held else FOCUS_BASIS_UNCONFIRMED)
    if plan.get("rule") not in ("hyperfocal", "subject_depth", "focused_at_infinity"):
        return None
    driven = plan_is_driven(plan)
    if _usable_distance(final_distance_m):
        final = final_distance_m
    elif driven and (held or manual) and plan.get("focus_distance_source") == DISTANCE_EXIF:
        final = plan_focus_distance(plan)          # focus did not move since the EXIF-proven meter frame
    else:
        final = None
    measured = focal_mm is not None and isfinite(focal_mm) and focal_mm > 0
    missing = [reason for reason, absent in ((REASON_FOCUS_NOT_VERIFIED, final is None),
                                             (REASON_FINAL_FOCAL_UNKNOWN, not measured),
                                             (REASON_APERTURE_NOT_VERIFIED, final_f_number is None),
                                             (REASON_SENSOR_UNKNOWN, sensor is None)) if absent]
    if missing:
        if not driven:
            return None                            # nothing new is proven; the planning reason stands
        plan["final_focus_checked"] = True
        plan["final_meets_requirement"] = None
        plan["final_pending_reason"] = "; ".join(missing)
        detail = "; ".join(f"{reason} (no EXIF focus distance for the final frame)"
                           if reason == REASON_FOCUS_NOT_VERIFIED else reason for reason in missing)
        return f"{detail[0].upper()}{detail[1:]}, so the planned depth of field is unverified."
    plan["final_focus_checked"] = True
    plan["final_focus_distance_m"] = "inf" if final == inf else round(final, 4)
    if plan["rule"] == "focused_at_infinity":
        # "Sharp from the hyperfocal distance to infinity" holds only if the final plane still
        # reaches infinity (at infinity, or at/beyond the hyperfocal).
        met = sharp_zone_m(final, focal_mm, final_f_number, sensor)[1] == inf
    else:
        # The ORIGINAL subject interval must sit inside the final zone.
        met = _interval_covered(plan, final, final_f_number, focal_mm, sensor)
    plan["final_meets_requirement"] = met
    final_near, final_far = sharp_zone_m(final, focal_mm, final_f_number, sensor)
    plan["final_near_m"] = round(final_near, 4)
    plan["final_far_m"] = "inf" if final_far == inf else round(final_far, 4)
    if met:
        if plan.get("status") == STATUS_OK:
            return None
        return (f"The final focus at {final:.2f} m covers the planned subject at "
                f"f/{final_f_number:g}; the earlier depth warning does not apply.")
    return (f"The final focus is at {final:.2f} m; at f/{final_f_number:g} that sharp zone does not "
            "cover the planned subject.")
