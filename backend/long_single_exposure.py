"""ONE long exposure for light trails, silky water and motion blur, chosen from the metered scene.

Frame averaging (the long_exposure/night session modes) cannot draw a continuous light trail:
USB frames are seconds apart, so a moving light leaves dots. A trail needs the shutter itself
open for seconds. This planner picks that single exposure from the body's own live ladders:

- shutter: a Sony third-stop above 1 s, at most 30 s (sonyremote SONY_LONG_EXPOSURE_*), and at
  least the subject's preferred length when the scene allows it;
- ISO: base ISO, raised only when even the widest aperture at 30 s is too dark (and then said so);
- aperture: near f/8, stopped down (to f/16 before going further) to keep the exposure right.

A scene too bright for the subject's exposure even at the smallest usable aperture and base ISO
gets an ND filter suggestion and no settings: the planner never fakes a long exposure. Pure: no
camera, no model. The engine fires the result through the separately gated long route.
"""
from __future__ import annotations

import math

import constraint_cards
from sonyremote.camera import (SONY_ACTION_MAX_ENABLED_SHUTTER_S, SONY_LONG_EXPOSURE_LADDER_S,
                               SONY_LONG_EXPOSURE_MAX_SHUTTER_S)

ALGORITHM = "long-single-exposure-v1"
PREFERRED_APERTURE = 8.0            # a sharp middle aperture on full frame
DIFFRACTION_SOFT_APERTURE = 16.0    # stop down past this only to reach the subject's length
EXPOSURE_TOLERANCE_EV = 1 / 3       # one Sony third-stop
SUBJECTS = {
    "light_trails": {"preferred_min_s": 4.0, "look": "moving lights draw continuous trails"},
    "silky_water": {"preferred_min_s": 2.0, "look": "moving water smooths into silk"},
    "motion_blur": {"preferred_min_s": 1.3, "look": "moving subjects blur"},
    "long_exposure": {"preferred_min_s": 2.0, "look": "movement smooths across the frame"},
}
ND_FILTERS = ((3, "ND8 (3 stops)"), (6, "ND64 (6 stops)"), (10, "ND1000 (10 stops)"))

_OK, _WARNING, _LIMITED, _PENDING = (constraint_cards.OK, constraint_cards.WARNING,
                                     constraint_cards.LIMITED, constraint_cards.PENDING)


def long_shutter_choices(live_caps) -> list[tuple[float, str]]:
    """(seconds, exact label) the body lists above the ordinary 1 s cap, on Sony's ladder, <= 30 s."""
    choices = []
    for seconds, label in zip(live_caps.shutters, live_caps.shutter_labels):
        if (SONY_ACTION_MAX_ENABLED_SHUTTER_S < seconds <= SONY_LONG_EXPOSURE_MAX_SHUTTER_S
                and any(abs(seconds - stop) / stop < .02 for stop in SONY_LONG_EXPOSURE_LADDER_S)):
            choices.append((seconds, label))
    return sorted(choices)


def _seconds(value: float) -> str:
    return f"{value:g} s" if value >= 1 else f"1/{1 / value:.0f} s"


def _nd_suggestion(stops_needed: float) -> str:
    for stops, name in ND_FILTERS:
        if stops_needed <= stops + 1e-9:
            return name
    return "more than 10 stops (stack ND filters)"


def plan_long_single_exposure(scene_ev100, live_caps, *, subject: str | None = "long_exposure",
                              base_iso: int = 100, iso_ceiling: int = 6400,
                              stop_down_verified: bool = True, meter_note: str | None = None,
                              stop_down: dict | None = None):
    """Plan ONE long exposure. Returns (plan, cards): a JSON-safe plan and its constraint cards.

    `live_caps` must already carry the mounted-lens aperture limits (camera_caps.constrain_mounted_lens)
    and the body's raw shutter ladder. `stop_down_verified` says whether those limits come from a
    verified lens profile; without one the smallest usable aperture is the confirmed one, and the
    ND message says the user can set a smaller aperture themselves. `stop_down` is the evidence of
    camera_caps.electronic_stop_down when `live_caps` already carries its unread stop-down choices:
    the plan records it as pending, and the caller confirms it by readback before firing.
    """
    if (type(scene_ev100) not in (int, float) or not math.isfinite(scene_ev100)
            or not -20 <= scene_ev100 <= 30):
        raise ValueError("scene_ev100 must be finite and within the supported exposure range")
    subject = subject if subject in SUBJECTS else "long_exposure"
    look = SUBJECTS[subject]["look"]
    shutters = long_shutter_choices(live_caps)
    if not shutters:
        raise ValueError("The body lists no live shutter above 1 s on the Sony ladder; the long "
                         "exposure cannot be planned")
    labels = live_caps.aperture_labels or [str(value) for value in live_caps.apertures]
    apertures = sorted(zip(live_caps.apertures, labels))
    isos = sorted(iso for iso in live_caps.isos if base_iso <= iso <= iso_ceiling)
    if not apertures or not isos:
        raise ValueError("Live aperture and ISO choices within the policy are required")
    base = isos[0]
    min_t, max_t = shutters[0][0], shutters[-1][0]
    preferred_min = max(SUBJECTS[subject]["preferred_min_s"], min_t)
    widest, smallest = apertures[0], apertures[-1]

    def exact(f_number: float, iso: int) -> float:
        return f_number * f_number * 100 / (2 ** scene_ev100 * iso)

    notes = [f"Scene metered at EV {scene_ev100:.2f} (ISO 100) from an ordinary meter frame of at most 1 s."]
    if meter_note:
        notes.append(meter_note)
    plan = {"algorithm": ALGORITHM, "subject": subject, "look": look, "scene_ev100": scene_ev100,
            "preferred_min_shutter_s": preferred_min, "max_shutter_s": max_t, "base_iso": base,
            "stop_down_verified": bool(stop_down_verified), "nd": None, "iso_raised": False,
            **({"stop_down": dict(stop_down)} if stop_down is not None else {}),
            "underexposure_ev": 0.0, "outcome_verified": False,
            "long_exposure_nr": "unverified: the body may record an equal-length dark frame afterwards"}

    candidates = []
    for f_number, f_label in apertures:
        for t, t_label in shutters:
            error = math.log2(t / exact(f_number, base))
            if abs(error) <= EXPOSURE_TOLERANCE_EV + 1e-9:
                rank = (t < preferred_min - 1e-9, f_number > DIFFRACTION_SOFT_APERTURE + 1e-9,
                        abs(math.log2(f_number / PREFERRED_APERTURE)), abs(error), -t)
                candidates.append((rank, f_number, f_label, t, t_label, error))

    t_brightest = exact(smallest[0], base)   # the shortest correct time the lens allows at base ISO
    if not candidates and t_brightest < min_t * 2 ** -EXPOSURE_TOLERANCE_EV:
        stops = math.log2(preferred_min / t_brightest)
        suggestion = _nd_suggestion(stops)
        reason = (f"Too bright for a long exposure: even f/{smallest[0]:g} at ISO {base} needs only "
                  f"{_seconds(t_brightest)}, and {look} needs about {preferred_min:g} s. Add an ND "
                  f"filter of about {stops:.1f} stops ({suggestion})")
        if not stop_down_verified:
            reason += (f", or set a smaller aperture on the camera yourself (the autopilot does not stop "
                       f"an unprofiled lens down past f/{smallest[0]:g})")
        reason += ". No long exposure was faked."
        plan.update(status="needs_nd", reason=reason, shutter_s=None, shutter_label=None,
                    aperture=None, aperture_label=None, iso=None, iso_label=None, exposure_error_ev=None,
                    nd={"stops_needed": stops, "suggested_filter": suggestion,
                        "at_aperture": smallest[0], "at_iso": base, "target_shutter_s": preferred_min})
        factor, card = constraint_cards.SubFactor, constraint_cards.ConstraintCard
        cards = [
            card("SHUTTER", "not fired", reason,
                 [factor("ND Filter", f"add {suggestion}", _LIMITED),
                  factor("Vibrations", "tripod required for any long exposure", _WARNING)]),
            card("APERTURE", f"f/{smallest[0]:g}", "Smallest usable aperture for this lens",
                 [factor("Stop-down", "verified lens profile" if stop_down_verified
                         else f"electronic lens: f/{smallest[0]:g} planned, never read back (nothing fired)"
                         if (stop_down or {}).get("status") == "pending_readback"
                         else f"unprofiled lens: not stopped down past f/{smallest[0]:g}",
                         _OK if stop_down_verified else _PENDING)]),
            card("ISO", f"ISO {base}", "Base ISO: already the least sensitive setting", []),
        ]
        plan["notes"] = notes
        plan["cards"] = [item.to_dict() for item in cards]
        return plan, cards

    iso = base
    if candidates:
        _, f_number, f_label, t, t_label, error = min(candidates, key=lambda item: item[0])
    elif exact(widest[0], base) > max_t * 2 ** EXPOSURE_TOLERANCE_EV:
        # Too dark even wide open at the longest shutter: raise ISO, never exceed the ceiling.
        f_number, f_label = widest
        t, t_label = shutters[-1]
        needed = base * exact(widest[0], base) / max_t
        iso = (isos[-1] if needed >= isos[-1]
               else min(isos, key=lambda value: abs(math.log2(value / needed))))
        error = math.log2(t / exact(f_number, iso))
        plan["iso_raised"] = iso > base
        if -error > EXPOSURE_TOLERANCE_EV:
            plan["underexposure_ev"] = -error
            notes.append(f"Too dark for f/{f_number:g}, {max_t:g} s and ISO {iso}: this photo will be "
                         f"underexposed by about {-error:.1f} EV.")
    else:
        # A gap in an unusual ladder: a higher ISO may still land within a third of a stop.
        # Otherwise refuse: never fire a long exposure that is knowingly badly exposed.
        _, _, f_number, f_label, t, t_label, iso, error = min(
            ((abs(math.log2(t / exact(f, value))), value, f, fl, t, tl, value, math.log2(t / exact(f, value)))
             for f, fl in apertures for t, tl in shutters for value in isos), key=lambda item: item[:2])
        if abs(error) > EXPOSURE_TOLERANCE_EV + 1e-9:
            reason = (f"This body's live settings cannot expose the scene within a third of a stop "
                      f"for a long exposure (closest {error:+.2f} EV at f/{f_number:g}, {_seconds(t)}, ISO {iso}). "
                      "No long exposure was faked.")
            plan.update(status="no_settable_exposure", reason=reason, shutter_s=None, shutter_label=None,
                        aperture=None, aperture_label=None, iso=None, iso_label=None, exposure_error_ev=None)
            factor, card = constraint_cards.SubFactor, constraint_cards.ConstraintCard
            cards = [card("SHUTTER", "not fired", reason,
                          [factor("Settable Exposure", f"closest {error:+.2f} EV", _LIMITED)]),
                     card("APERTURE", "unchanged", "No settable combination", []),
                     card("ISO", "unchanged", "No settable combination", [])]
            plan["notes"] = notes
            plan["cards"] = [item.to_dict() for item in cards]
            return plan, cards
        plan["iso_raised"] = iso > base
    if t < preferred_min - 1e-9:
        notes.append(f"{_seconds(t)} is shorter than the {preferred_min:g} s this look prefers: the scene is "
                     f"too bright at f/{f_number:g}; an ND filter would allow a longer exposure.")

    factor, card = constraint_cards.SubFactor, constraint_cards.ConstraintCard
    if f_number > PREFERRED_APERTURE + 1e-9:
        aperture_reason = f"Stopped down to f/{f_number:g} so the {_seconds(t)} exposure stays right at ISO {iso}"
    elif f_number < PREFERRED_APERTURE - 1e-9:
        aperture_reason = (f"Opened to f/{f_number:g}: the scene is too dark for f/{PREFERRED_APERTURE:g} "
                           f"within {max_t:g} s")
    else:
        aperture_reason = f"f/{f_number:g}, a sharp middle aperture; the exposure is right at {_seconds(t)}"
    if plan["iso_raised"]:
        iso_reason = (f"Raised to ISO {iso}: even f/{widest[0]:g} at {max_t:g} s is too dark at ISO {base}")
    else:
        iso_reason = f"Base ISO {iso}: the cleanest long exposure"
    iso_factors = [factor("Long Exposure NR", "the body may record an equal-length dark frame afterwards; "
                          "wait for it", _PENDING)]
    if plan["iso_raised"]:
        iso_factors.append(factor("Noise", f"ISO {iso} is above base ISO {base}", _WARNING))
    if plan["underexposure_ev"]:
        iso_factors.append(factor("Exposure Target", f"{-plan['underexposure_ev']:+.2f} EV at the ISO "
                                  f"{iso_ceiling} ceiling", _LIMITED))
    cards = [
        card("SHUTTER", f"{t:g}s", f"{_seconds(t)} so {look}; chosen from the metered scene",
             [factor("Vibrations", f"tripod required: a {_seconds(t)} exposure cannot be hand-held", _WARNING),
              factor("Scene Movement", f"movement records for {_seconds(t)}",
                     _OK if t >= preferred_min - 1e-9 else _WARNING)]),
        card("APERTURE", f"f/{f_number:g}", aperture_reason,
             [factor("Exposure Balance", f"{error:+.2f} EV against the metered scene",
                     _OK if abs(error) <= EXPOSURE_TOLERANCE_EV + 1e-9 else _LIMITED),
              factor("Diffraction", f"past f/{DIFFRACTION_SOFT_APERTURE:g}: fine detail softens"
                     if f_number > DIFFRACTION_SOFT_APERTURE + 1e-9 else f"within f/{DIFFRACTION_SOFT_APERTURE:g}",
                     _WARNING if f_number > DIFFRACTION_SOFT_APERTURE + 1e-9 else _OK)]),
        card("ISO", f"ISO {iso}", iso_reason, iso_factors),
    ]
    plan.update(status="ok", reason=f"One {_seconds(t)} exposure at f/{f_number:g}, ISO {iso}, so {look}.",
                shutter_s=t, shutter_label=t_label, aperture=f_number, aperture_label=f_label,
                iso=iso, iso_label=str(iso), exposure_error_ev=error, notes=notes,
                cards=[item.to_dict() for item in cards])
    return plan, cards


def unplannable(reason: str):
    """A definite no-final-shutter refusal for a body or scene the planner cannot plan at all (for
    example no live shutter above 1 s). Returns (plan, cards) like plan_long_single_exposure."""
    text = f"No long exposure can be planned: {reason} No long exposure was faked."
    factor, card = constraint_cards.SubFactor, constraint_cards.ConstraintCard
    cards = [card("SHUTTER", "not fired", text, [factor("Long Shutter", reason, _LIMITED)]),
             card("APERTURE", "unchanged", "No long exposure planned", []),
             card("ISO", "unchanged", "No long exposure planned", [])]
    plan = {"algorithm": ALGORITHM, "status": "unplannable", "reason": text, "nd": None,
            "shutter_s": None, "shutter_label": None, "aperture": None, "aperture_label": None,
            "iso": None, "iso_label": None, "exposure_error_ev": None, "notes": [],
            "outcome_verified": False, "cards": [item.to_dict() for item in cards]}
    return plan, cards


def as_recommendation(plan: dict, cards) -> constraint_cards.Recommendation:
    """The settable recommendation for an `ok` plan (exact labels the body lists)."""
    if plan.get("status") != "ok":
        raise ValueError("Only an ok long-exposure plan has settings to apply")
    return constraint_cards.Recommendation(
        plan["iso"], plan["aperture"], plan["shutter_s"], list(cards), notes=list(plan["notes"]),
        iso_label=plan["iso_label"], aperture_label=plan["aperture_label"],
        shutter_label=plan["shutter_label"])
