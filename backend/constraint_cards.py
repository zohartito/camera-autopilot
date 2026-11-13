"""M1 constraint cards — the brain's *explainable* output, mirroring Arsenal's Smart-Mode UI.

Arsenal surfaces each capture decision as three axis cards, each with two named sub-factors
(the on-screen sliders, [docs/video-research/01,03,18]):

    SHUTTER  -> Vibrations  · Scene Movement
    APERTURE -> Depth of Field · Lens Sharpness Profile  (+ Lens Transmission, one of the 22 factors)
    ISO      -> Dynamic Range · Color Range

and a live readout under each — the *computed constraint* (e.g. "Vibrations  1/30", "Dynamic
Range  64-2000" = the allowable ISO span). `build_cards` turns a physics ExposureSolution plus
the factor models into those three cards, so recommend_settings can return *why* each axis
landed where it did instead of a flat (iso, f, shutter) triple.

Pure (physics + factor models, no ML, no camera): `python backend/constraint_cards.py`.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, field

import aperture_optimizer
import color_accuracy
import lens_optics
import physics
import sensor_noise

OK, WARNING, LIMITED, PENDING = "ok", "warning", "limited", "pending"
_RANK = {OK: 0, PENDING: 1, WARNING: 2, LIMITED: 3}  # the card inherits its worst sub-factor


@dataclass
class SubFactor:
    name: str       # the Arsenal slider name, e.g. "Vibrations"
    readout: str    # the computed constraint, e.g. "1/50s floor"
    status: str     # ok | warning | limited | pending


@dataclass
class ConstraintCard:
    axis: str                                       # SHUTTER | APERTURE | ISO
    value: str                                      # the chosen setting, e.g. "1/50s"
    reason: str                                     # the dominant why
    sub_factors: list[SubFactor] = field(default_factory=list)

    @property
    def status(self) -> str:
        # the card reflects its worst *assessed* factor; pending factors (awaiting a live frame
        # or an unbuilt model) are shown but don't drag the card down to "pending".
        assessed = [s.status for s in self.sub_factors if s.status != PENDING]
        return max(assessed, key=lambda st: _RANK[st], default=PENDING)

    def to_dict(self) -> dict:
        return {**asdict(self), "status": self.status, "evidence": "computed",
                "outcome_verified": False}


@dataclass
class Recommendation:
    """What recommend_settings returns: the chosen exposure + the three explainable cards.
    The `*_label` fields are the exact strings the camera's `set_exposure` expects."""
    iso: int
    aperture: float
    shutter_s: float
    cards: list[ConstraintCard]
    notes: list[str] = field(default_factory=list)
    iso_label: str = ""
    aperture_label: str = ""
    shutter_label: str = ""
    aperture_plan: dict | None = None     # B04: aperture_optimizer.plan_aperture's decision
    exposure_reference: dict | None = None  # the EV reference and lens transmission applied
    taste_preference: dict | None = None  # the taste/current aperture preference this solve used

    def to_dict(self) -> dict:
        return {**asdict(self), "cards": [card.to_dict() for card in self.cards],
                "status": max((card.status for card in self.cards),
                              key=lambda status: _RANK[status], default=PENDING),
                "schema_version": 1, "outcome_verified": False}

    def card(self, axis: str) -> ConstraintCard:
        return next(c for c in self.cards if c.axis == axis)


def _fmt_shutter(shutter_s: float) -> str:
    return f"1/{1 / shutter_s:.0f}s" if shutter_s < 1 else f"{shutter_s:.1f}s"


def _shutter_card(exposure, focal_mm: float, shutter_floor_s: float, stability: float,
                  vib_floor_s: float, subject_motion_floor_s: float | None) -> ConstraintCard:
    is_note = f", {stability:.0f}x IS" if stability != 1.0 else ""
    vibrations = SubFactor("Vibrations", f"{_fmt_shutter(vib_floor_s)} floor ({focal_mm:.0f}mm{is_note})",
                           LIMITED if exposure.shutter_s > vib_floor_s + 1e-9 else OK)
    if subject_motion_floor_s is None:
        scene_movement = SubFactor("Scene Movement", "pending live frame (optical flow)", PENDING)
    else:
        froze = exposure.shutter_s <= subject_motion_floor_s + 1e-9
        scene_movement = SubFactor(
            "Scene Movement",
            f"{_fmt_shutter(subject_motion_floor_s)} to freeze" + ("" if froze else " — NOT met"),
            OK if froze else LIMITED)
    subject_binds = subject_motion_floor_s is not None and subject_motion_floor_s < vib_floor_s
    if exposure.below_shutter_floor:
        reason = "below the blur-safe floor — motion blur likely"
    elif subject_binds:
        reason = f"set to {_fmt_shutter(exposure.shutter_s)} to limit predicted subject motion blur"
    else:
        reason = f"meets the modeled {_fmt_shutter(vib_floor_s)} vibration floor"
    return ConstraintCard("SHUTTER", _fmt_shutter(exposure.shutter_s), reason,
                          [vibrations, scene_movement])


def transmission_subfactor(transmission, aperture: float, ev_reference: str) -> SubFactor:
    """Lens Transmission (T-stop vs f-stop). A published measurement reads as the T-stop at this
    aperture with its source; an unmeasured lens reads `unknown` (pending) and changes nothing. With a
    through-this-lens meter the loss is already in the reading, and the readout says so."""
    ttl = ev_reference == physics.EV_THROUGH_THIS_LENS
    if not transmission.known:
        effect = ("metered through this lens, so its loss is already included" if ttl
                  else "scene-luminance exposure not corrected")
        return SubFactor("Lens Transmission", f"T-stop unknown (no published measurement); {effect}",
                         PENDING)
    offset = transmission.t_stop_offset_stops
    effect = ("already in the through-lens meter reading" if ttl
              else f"exposure raised {offset:.2f} EV for it")
    return SubFactor("Lens Transmission",
                     f"T/{transmission.t_stop(aperture):.1f} at f/{aperture:g} "
                     f"(-{offset:.2f} EV, {transmission.measured_on}, {transmission.source_date}); "
                     f"{effect}", OK)


def _metres(value) -> str:
    return "inf" if value == "inf" or value == float("inf") else f"{float(value):.2f}m"


def depth_of_field_subfactor(aperture: float, focal_mm, sensor,
                             subject_distance_m: float | None = None, dof_exceeds: bool = False,
                             aperture_plan: dict | None = None) -> SubFactor:
    """DEPTH MAP, always rebuilt from the plan's CURRENT state (the one rule in aperture_optimizer).

    Certified (OK / LIMITED) only for a driven plan (a focal measured for this shot and an EXIF
    focus distance): after the final focus is checked, the final plane's zone; otherwise the zone at
    the EXIF focus plane and this (final) aperture. Everything else is PENDING and names what is
    missing. Without a plan (legacy callers) the old hyperfocal/zone readout is kept."""
    plan = aperture_plan
    if plan is None:
        if subject_distance_m is None:
            hyperfocal_m = physics.hyperfocal_mm(focal_mm, aperture, sensor.coc_mm) / 1000.0
            return SubFactor("Depth of Field",
                             f"hyperfocal {hyperfocal_m:.1f}m, CoC {sensor.coc_mm * 1000:.0f}um "
                             f"(subject distance pending)", PENDING)
        near, far = aperture_optimizer.sharp_zone_m(subject_distance_m, focal_mm, aperture, sensor)
        readout = f"subject {_metres(subject_distance_m)}, sharp {near:.2f}-{_metres(far)}"
        if dof_exceeds:
            return SubFactor("Depth of Field",
                             readout + " — can't cover full depth short of diffraction", LIMITED)
        return SubFactor("Depth of Field", readout, OK)

    known = plan.get("subject_distance_m")
    subject = f"subject {_metres(known)}" if known is not None else "subject unknown"
    reason = aperture_optimizer.depth_pending_reason(plan)
    if reason:
        return SubFactor("Depth of Field", f"{subject} — {reason}", PENDING)
    source = f" [{plan['distance_source']}]" if plan.get("distance_source") else ""
    if plan.get("final_focus_checked"):
        verdict = plan["final_meets_requirement"]
        readout = (f"{subject}, final focus {_metres(plan['final_focus_distance_m'])}, sharp "
                   f"{float(plan['final_near_m']):.2f}-{_metres(plan['final_far_m'])} — final focus "
                   f"plane {'covers' if verdict else 'does not cover'} the subject{source}")
        return SubFactor("Depth of Field", readout, OK if verdict else LIMITED)
    focus = aperture_optimizer.plan_focus_distance(plan)
    near, far = aperture_optimizer.sharp_zone_m(focus, focal_mm, aperture, sensor)
    readout = f"{subject}, focus {_metres(focus)}, sharp {near:.2f}-{_metres(far)}"
    if plan.get("rule") == "hyperfocal":
        hyperfocal_m = physics.hyperfocal_mm(focal_mm, aperture, sensor.coc_mm) / 1000.0
        readout += f", hyperfocal {hyperfocal_m:.1f}m (landscape)"
    covered = aperture_optimizer.requirement_met(plan, aperture, focal_mm, sensor) is not False
    if dof_exceeds:
        readout += " — can't cover full depth short of diffraction"
    elif not covered:
        readout += " — exposure reopened the aperture; the planned depth is not covered"
    return SubFactor("Depth of Field", readout + source,
                     LIMITED if dof_exceeds or not covered else OK)


_RECOMMENDED = object()


def settle_depth_after_focus(rec, *, af_search, final_distance_m, focal_mm, sensor,
                             captured_f_number=_RECOMMENDED) -> str | None:
    """The shared final step for the USB and Wi-Fi routes: ASSESS the captured aperture against the
    final frame's proofs (aperture_optimizer.reconcile_final_focus), append its note, and REBUILD
    the Depth of Field factor from the current state. Never changes a setting. A recommendation
    without a plan is left alone.

    `focal_mm` must be the FINAL frame's own measured focal (None when unreadable: never a pre-shot
    or cached value). `captured_f_number` is the verified applied aperture; the routes always pass it
    (None when it could not be verified, which keeps the assessment pending). Pure callers that
    omit it assess the recommended aperture."""
    plan = getattr(rec, "aperture_plan", None)
    if not isinstance(plan, dict):
        return None
    aperture = float(rec.aperture) if captured_f_number is _RECOMMENDED else captured_f_number
    plan["final_f_number_assessed"] = aperture
    note = aperture_optimizer.reconcile_final_focus(
        plan, af_search=af_search, final_distance_m=final_distance_m,
        final_f_number=aperture, focal_mm=focal_mm, sensor=sensor)
    if note:
        rec.notes.append(note)
    for card in rec.cards:
        for index, factor in enumerate(card.sub_factors if card.axis == "APERTURE" else []):
            if factor.name == "Depth of Field":
                card.sub_factors[index] = depth_of_field_subfactor(
                    aperture if aperture is not None else float(rec.aperture), focal_mm, sensor, None,
                    plan.get("status") == aperture_optimizer.STATUS_DOF_EXCEEDS, plan)
    return note


def _aperture_card(exposure, focal_mm: float, sensor, lens,
                   subject_distance_m: float | None = None, is_night: bool = False,
                   dof_exceeds: bool = False, transmission=None,
                   ev_reference: str = physics.EV_THROUGH_THIS_LENS,
                   aperture_plan: dict | None = None) -> ConstraintCard:
    dof = depth_of_field_subfactor(exposure.aperture, focal_mm, sensor, subject_distance_m,
                                   dof_exceeds, aperture_plan)

    sweet = lens.optimal_aperture(sensor)
    diff = physics.diffraction_status(exposure.aperture, sensor)
    coma = lens.coma_severity(exposure.aperture)
    bits = [f"sweet f/{sweet}"]
    # M11: coma only matters for night/astro point sources — only flag it when the scene is dark.
    if is_night and coma > 0.3:
        bits.append(f"coma {coma:.0%} (stop down for astro)")
    if diff:
        bits.append(diff[1])
    sharp_status = LIMITED if exposure.diffraction_limited else (WARNING if diff else OK)
    sharpness = SubFactor("Lens Sharpness Profile", ", ".join(bits), sharp_status)

    if exposure.overexposed:
        reason = f"stopped fully to f/{exposure.aperture} but the scene still overexposes — add an ND filter"
    elif exposure.diffraction_limited:
        reason = "capped at the diffraction limit — physics overruled a softer pick"
    elif diff:
        reason = "past the diffraction-limited aperture — softening fine detail"
    else:
        reason = f"in the sharp range (sweet spot f/{sweet})"
    lens_transmission = transmission if transmission is not None else lens.transmission
    return ConstraintCard("APERTURE", f"f/{exposure.aperture}", reason,
                          [dof, sharpness,
                           transmission_subfactor(lens_transmission, exposure.aperture, ev_reference)])


def _iso_card(exposure, noise, base_iso: int) -> ConstraintCard:
    ceiling = noise.usable_iso_ceiling(base_iso=base_iso)
    dr_here = noise.dynamic_range_stops(exposure.iso, base_iso)
    dr_status = (LIMITED if exposure.hit_iso_ceiling
                 else WARNING if exposure.iso >= ceiling / 2 else OK)
    dynamic_range = SubFactor("Dynamic Range",
                              f"{base_iso}-{ceiling} ISO span, {dr_here:.1f} stops DR here", dr_status)
    # Color Range: real color-accuracy model (chroma SNR -> distinguishable-color volume), no longer
    # a placeholder. Maps the model's status onto the card vocabulary (ok/warning/limited).
    color_readout, color_status = color_accuracy.color_range_readout(exposure.iso, noise, base_iso)
    color_range = SubFactor("Color Range", color_readout, color_status)
    reason = ("at the usable-ISO ceiling — noise floor reached" if exposure.hit_iso_ceiling
              else f"clean — {dr_here:.1f} stops of dynamic range")
    return ConstraintCard("ISO", f"ISO {exposure.iso}", reason, [dynamic_range, color_range])


def build_cards(exposure, *, focal_mm: float, shutter_floor_s: float,
                sensor=physics.A7II, lens=lens_optics.TAMRON_28_75_OPTICS,
                noise=sensor_noise.A7II_NOISE, stability: float = 1.0,
                base_iso: int = 100, vib_floor_s: float | None = None,
                subject_motion_floor_s: float | None = None,
                subject_distance_m: float | None = None,
                is_night: bool = False, dof_exceeds: bool = False, transmission=None,
                ev_reference: str = physics.EV_THROUGH_THIS_LENS,
                aperture_plan: dict | None = None) -> list[ConstraintCard]:
    """Three explainable cards (SHUTTER, APERTURE, ISO) from a physics ExposureSolution + the
    factor models. Pure — no ML, no camera. `vib_floor_s` / `subject_motion_floor_s` let the SHUTTER
    card attribute which floor bound; `subject_distance_m` / `is_night` feed the APERTURE card's DoF
    readout + the night-gated coma warning; `dof_exceeds` marks the DoF sub-factor LIMITED when no
    aperture covers the subject short of diffraction (defaults: vib = combined floor, depth = pending)."""
    if vib_floor_s is None:
        vib_floor_s = shutter_floor_s
    return [
        _shutter_card(exposure, focal_mm, shutter_floor_s, stability, vib_floor_s, subject_motion_floor_s),
        _aperture_card(exposure, focal_mm, sensor, lens, subject_distance_m, is_night, dof_exceeds,
                       transmission, ev_reference, aperture_plan),
        _iso_card(exposure, noise, base_iso),
    ]


# --- self-test (run: python backend/constraint_cards.py) ------------------
def _selftest() -> None:
    Exp = physics.ExposureSolution

    # A clean dim-room solve: ISO 1225, f/2.8, 1/50s, nothing limited.
    dim = Exp(iso=1225, aperture=2.8, shutter_s=1 / 50)
    cards = build_cards(dim, focal_mm=50, shutter_floor_s=1 / 50)
    assert [c.axis for c in cards] == ["SHUTTER", "APERTURE", "ISO"], cards
    for c in cards:
        assert c.value and c.reason and len(c.sub_factors) == (3 if c.axis == "APERTURE" else 2), c
    sh, ap, iso = cards
    assert sh.value == "1/50s" and sh.status == OK, sh
    assert sh.sub_factors[0].name == "Vibrations" and sh.sub_factors[1].name == "Scene Movement"
    assert ap.sub_factors[0].name == "Depth of Field"
    assert iso.value == "ISO 1225" and iso.sub_factors[0].name == "Dynamic Range"
    assert "100-6400" in iso.sub_factors[0].readout, iso.sub_factors[0].readout  # the noise-model span

    # A diffraction-capped solve -> APERTURE card reads limited.
    capped = Exp(iso=100, aperture=21.4, shutter_s=1 / 72, diffraction_limited=True)
    assert build_cards(capped, focal_mm=50, shutter_floor_s=1 / 50)[1].status == LIMITED

    # A below-floor solve -> SHUTTER card reads limited.
    dark = Exp(iso=6400, aperture=2.8, shutter_s=1 / 33, below_shutter_floor=True)
    assert build_cards(dark, focal_mm=50, shutter_floor_s=1 / 50)[0].status == LIMITED

    # An ISO-ceiling solve -> ISO card reads limited.
    pinned = Exp(iso=6400, aperture=2.8, shutter_s=1 / 33, hit_iso_ceiling=True)
    assert build_cards(pinned, focal_mm=50, shutter_floor_s=1 / 50)[2].status == LIMITED

    # Subject motion binds: the SHUTTER card credits it (not "pending") + the reason says so.
    fast = Exp(iso=2000, aperture=2.8, shutter_s=1 / 500)
    sh = build_cards(fast, focal_mm=50, shutter_floor_s=1 / 500, vib_floor_s=1 / 50,
                     subject_motion_floor_s=1 / 500)[0]
    assert sh.sub_factors[1].name == "Scene Movement" and sh.sub_factors[1].status == OK, sh
    assert "freeze" in sh.sub_factors[1].readout and "subject motion" in sh.reason, sh

    # DEPTH MAP: a known subject distance shows the sharp zone (not "pending"). M11: the coma warning
    # is night-gated — wide-open in daylight stays quiet; the same aperture at night flags coma.
    lit = build_cards(dim, focal_mm=50, shutter_floor_s=1 / 50, subject_distance_m=2.0)[1]
    assert "subject 2.00m" in lit.sub_factors[0].readout, lit.sub_factors[0].readout
    assert "coma" not in lit.sub_factors[1].readout, lit.sub_factors[1].readout
    night = build_cards(dim, focal_mm=50, shutter_floor_s=1 / 50, subject_distance_m=2.0,
                        is_night=True)[1]
    assert "coma" in night.sub_factors[1].readout, night.sub_factors[1].readout

    # M9 honesty: when no aperture covers the subject short of diffraction, the DoF sub-factor reads
    # LIMITED (not a false OK) so the card never claims a sharpness it can't deliver.
    exceeded = build_cards(dim, focal_mm=50, shutter_floor_s=1 / 50, subject_distance_m=0.5,
                           dof_exceeds=True)[1]
    assert exceeded.sub_factors[0].status == LIMITED, exceeded.sub_factors[0]
    assert "can't cover" in exceeded.sub_factors[0].readout, exceeded.sub_factors[0].readout

    print("constraint-cards self-test: PASS")
    for c in cards:
        print(f"  [{c.axis:8}] {c.value:8} {c.status:8} — {c.reason}")
        for s in c.sub_factors:
            print(f"      - {s.name}: {s.readout}  [{s.status}]")


if __name__ == "__main__":
    _selftest()
