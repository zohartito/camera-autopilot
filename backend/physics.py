"""M1 physics layer — deterministic photographic constraints.

The capture brain has two halves (modelled on how Arsenal's public blog describes its own):
  1. retrieval + clustering over an (image -> settings) database -> a *style* baseline
     (e.g. "portraits like this tend to be shot near f/2.8"), and
  2. **this module** — a deterministic constraint layer that bends that baseline to the
     real scene: hit the correct exposure, never go slower than the blur-safe shutter,
     never exceed the usable-ISO ceiling, and know the depth of field a choice implies.

It is pure math — no camera, no dataset, no ML — so it is fully unit-testable on its own
(run `python backend/physics.py`). The brain layer snaps these continuous results to the
camera's actually-enumerated ISO / aperture / shutter options.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from math import ceil, hypot, isfinite, log2, sqrt

FULL_FRAME_DIAGONAL_MM = 43.27  # 36 x 24 mm reference, for crop factor
GREEN_WAVELENGTH_MM = 0.00055   # 550 nm, mid-visible — for the Airy-disk diffraction math


@dataclass(frozen=True)
class SensorProfile:
    """Just enough sensor geometry to do the physics. `max_usable_iso` is the practical
    noise ceiling (a stand-in for Arsenal's per-sensor noise/DR profile — refine with data).
    `width_px` (long-edge pixels) gives the pixel pitch for the diffraction limit."""
    name: str
    width_mm: float
    height_mm: float
    base_iso: int = 100
    max_usable_iso: int = 6400
    width_px: int = 6000

    @property
    def diagonal_mm(self) -> float:
        return hypot(self.width_mm, self.height_mm)

    @property
    def crop_factor(self) -> float:
        return FULL_FRAME_DIAGONAL_MM / self.diagonal_mm

    @property
    def coc_mm(self) -> float:
        """Circle of confusion via the standard d/1500 rule (~0.029 mm full-frame)."""
        return self.diagonal_mm / 1500.0

    @property
    def pixel_pitch_mm(self) -> float:
        return self.width_mm / self.width_px

    @property
    def diffraction_soft_aperture(self) -> float:
        """The lens's diffraction-limited aperture: the f-number where the Airy disk
        (2.44·λ·N) grows past ~2 pixels and diffraction starts visibly softening fine
        detail. ~f/9 on a 24 MP full-frame. Sharp wide open; trade-off begins here."""
        return (2.0 * self.pixel_pitch_mm) / (2.44 * GREEN_WAVELENGTH_MM)

    @property
    def diffraction_hard_aperture(self) -> float:
        """Where the Airy disk exceeds the circle of confusion — diffraction destroys even
        print-scale sharpness (~f/21 full-frame). Physics refuses to stop down past here:
        this is what kept Arsenal from its f/25 mush."""
        return self.coc_mm / (2.44 * GREEN_WAVELENGTH_MM)


@dataclass(frozen=True)
class LensProfile:
    name: str
    min_focal_mm: float
    max_focal_mm: float
    min_aperture: float  # widest, e.g. 2.8 (smallest f-number)
    max_aperture: float  # narrowest, e.g. 22


# The dev rig.
A7II = SensorProfile("Sony A7 II (ILCE-7M2)", 35.8, 23.9, base_iso=100, max_usable_iso=6400,
                     width_px=6000)  # 24 MP, 6000 x 4000
# A7 III geometry from Sony's published spec (35.6 x 23.8 mm BSI, 24 MP, 6000 x 4000, base ISO
# 100). This is spec-derived, NOT measured: `max_usable_iso` here is a conservative policy
# placeholder, not a noise/DR measurement. A measured noise table (data/sensor_profiles) supplies
# the real ceiling; without one the rig is labelled geometry_only.
A7III = SensorProfile("Sony A7 III (ILCE-7M3) — spec geometry, not measured", 35.6, 23.8,
                      base_iso=100, max_usable_iso=6400, width_px=6000)
TAMRON_28_75 = LensProfile("Tamron 28-75 f/2.8", 28.0, 75.0, min_aperture=2.8, max_aperture=22.0)


# --- exposure value -------------------------------------------------------
def ev100(f_number: float, shutter_s: float) -> float:
    """Exposure value at ISO 100 for an aperture/shutter pair. EV = log2(N^2 / t)."""
    return log2(f_number * f_number / shutter_s)


# Where a scene EV100 came from decides whether the lens's light loss still has to be applied.
# THROUGH_THIS_LENS: metered from a frame shot through the lens that will take the photograph (every
# meter frame in this product) — the glass's loss is already in the reading, and a constant
# transmittance cancels between the meter and the final frame at any aperture.
# SCENE_LUMINANCE: a lens-independent brightness (an incident/spot meter, an EV table) — settings for
# this lens must supply the extra light its glass absorbs.
EV_THROUGH_THIS_LENS = "through_this_lens"
EV_SCENE_LUMINANCE = "scene_luminance"


def lens_adjusted_ev100(scene_ev100: float, *, ev_reference: str,
                        t_stop_offset_stops: float | None) -> float:
    """The EV100 the settings must reproduce on this lens. `t_stop_offset_stops` is 2*log2(T/f)
    (>= 0), or None when unmeasured, in which case nothing is changed. A through-this-lens reading
    is returned unchanged; a scene luminance is dimmed by exactly the offset (one stop of loss
    needs one stop more exposure)."""
    if not isfinite(scene_ev100):
        raise ValueError(f"scene EV must be finite, got {scene_ev100!r}")
    if t_stop_offset_stops is not None and not (isfinite(t_stop_offset_stops)
                                                and t_stop_offset_stops >= 0):
        raise ValueError(f"T-stop offset must be finite and >= 0, got {t_stop_offset_stops!r}")
    if ev_reference == EV_THROUGH_THIS_LENS:
        return scene_ev100
    if ev_reference != EV_SCENE_LUMINANCE:
        raise ValueError(f"unknown EV reference {ev_reference!r}")
    return scene_ev100 - (t_stop_offset_stops or 0.0)


def needed_shutter_s(target_ev100: float, f_number: float, iso: int, base_iso: int = 100) -> float:
    """Shutter (seconds) that correctly exposes a scene of brightness `target_ev100`
    (an EV measured at ISO 100) given an aperture and ISO. Raising ISO shortens it."""
    return f_number * f_number * base_iso / ((2.0 ** target_ev100) * iso)


# --- the vibration factor -------------------------------------------------
def handheld_shutter_floor(focal_length_mm: float, crop_factor: float = 1.0,
                           stability: float = 1.0) -> float:
    """Slowest blur-safe shutter (seconds): the "1/focal-length" rule, corrected for crop.
    `stability` >1 is steadier (IBIS/tripod) and allows a *slower* floor; <1 is shakier (wind)
    and forces a faster one. Arsenal feeds a high-pass-filtered accelerometer here; until the IMU
    lands, `stability` is the knob (tripod ~ large, braced ~2, handheld ~1)."""
    return stability / (focal_length_mm * crop_factor)


# --- depth of field -------------------------------------------------------
def hyperfocal_mm(focal_length_mm: float, f_number: float, coc_mm: float) -> float:
    """Hyperfocal distance (mm): focus here and everything from H/2 to infinity is sharp."""
    return focal_length_mm * focal_length_mm / (f_number * coc_mm) + focal_length_mm


def depth_of_field_m(focus_distance_m: float, focal_length_mm: float, f_number: float,
                     coc_mm: float) -> tuple[float, float]:
    """(near, far) sharp limits in meters. `far` is inf once focus reaches the hyperfocal."""
    h = hyperfocal_mm(focal_length_mm, f_number, coc_mm)
    s = focus_distance_m * 1000.0
    near = s * (h - focal_length_mm) / (h + s - 2.0 * focal_length_mm)
    far = float("inf") if s >= h else s * (h - focal_length_mm) / (h - s)
    return near / 1000.0, (far if far == float("inf") else far / 1000.0)


# --- the integrator: bend a style baseline to the real scene --------------
@dataclass
class ExposureSolution:
    iso: int
    aperture: float
    shutter_s: float
    hit_iso_ceiling: bool = False
    below_shutter_floor: bool = False
    diffraction_limited: bool = False
    overexposed: bool = False
    notes: list[str] = field(default_factory=list)

    @property
    def ev100_achieved(self) -> float:
        return ev100(self.aperture, self.shutter_s) - log2(self.iso / 100.0)


def solve_exposure(target_ev100: float, *, baseline_aperture: float, shutter_floor_s: float,
                   lens_min_aperture: float, lens_max_aperture: float, base_iso: int = 100,
                   max_iso: int = 6400, shutter_fastest_s: float = 1.0 / 8000,
                   diffraction_max_aperture: float = float("inf")) -> ExposureSolution:
    """Given the scene brightness (`target_ev100`) and a *style* baseline aperture, find the
    (iso, aperture, shutter) that exposes correctly while honoring the blur-safe shutter floor,
    the ISO ceiling, and the diffraction cap. Priority mirrors Arsenal's: hold the chosen
    aperture, raise ISO, then open aperture, and only then accept a shutter below the floor.

    `diffraction_max_aperture` makes physics *authoritative* over taste: it never holds or stops
    down past the f-number where diffraction destroys detail (that's an ND-filter job, not f/25).
    """
    # Authoritative standalone: clamp taste to the lens, then provisionally to the diffraction
    # cap. (The dark path below may still reopen wider than the cap to hold the shutter floor.)
    lens_clamped = min(max(baseline_aperture, lens_min_aperture), lens_max_aperture)
    taste_over_cap = lens_clamped > diffraction_max_aperture
    aperture = min(lens_clamped, diffraction_max_aperture)
    iso = base_iso
    notes: list[str] = []
    hit_ceiling = False
    below_floor = False
    overexposed = False

    shutter = needed_shutter_s(target_ev100, aperture, iso, base_iso)

    if shutter > shutter_floor_s:
        # Too dark -> shutter would be slower than blur-safe. Raise ISO to reach the floor.
        iso_for_floor = aperture * aperture * base_iso / ((2.0 ** target_ev100) * shutter_floor_s)
        if iso_for_floor <= max_iso:
            iso = max(base_iso, ceil(iso_for_floor))  # ceil: never settle a hair below the floor
            shutter = needed_shutter_s(target_ev100, aperture, iso, base_iso)
            notes.append(f"raised ISO to {iso} to hold the {1/shutter_floor_s:.0f}s shutter floor")
        else:
            # ISO ceiling not enough -> open the aperture as far as the lens allows.
            iso = max_iso
            hit_ceiling = True
            aperture_for_floor = sqrt((2.0 ** target_ev100) * shutter_floor_s * iso / base_iso)
            if aperture_for_floor >= lens_min_aperture:
                aperture = aperture_for_floor
                shutter = needed_shutter_s(target_ev100, aperture, iso, base_iso)
                notes.append(f"opened to f/{aperture:.1f} at ISO {iso} (ceiling) to hold the floor")
            else:
                aperture = lens_min_aperture
                shutter = needed_shutter_s(target_ev100, aperture, iso, base_iso)
                below_floor = True
                notes.append(f"wide open at f/{aperture:.1f}, ISO {iso}: shutter {1/shutter:.0f}s "
                             f"is below the blur-safe floor — expect motion blur")

    elif shutter < shutter_fastest_s:
        # Too bright -> shutter faster than the camera's max. Stop down to compensate, but never
        # past the diffraction cap — that's an ND-filter job, not a grind-detail-into-mush job.
        shutter = shutter_fastest_s
        aperture_for_fast = sqrt((2.0 ** target_ev100) * shutter * iso / base_iso)
        stop_limit = min(lens_max_aperture, diffraction_max_aperture)
        if aperture_for_fast <= stop_limit:
            aperture = aperture_for_fast
            notes.append(f"stopped down to f/{aperture:.1f} at the {1/shutter:.0f}s shutter cap")
        else:
            aperture = stop_limit
            overexposed = True
            notes.append(f"can't tame the light at f/{aperture:.1f}, {1/shutter:.0f}s — "
                         f"overexposed; add an ND filter")

    # Diffraction verdict from the FINAL aperture, BEFORE rounding (the dark path may have
    # reopened below the cap). Round only for display so it can't drop just under the cap.
    diffraction_limited = aperture >= diffraction_max_aperture - 1e-6
    aperture = round(aperture, 2)
    if diffraction_limited:
        if taste_over_cap:
            notes.append(f"capped at the f/{aperture:.1f} diffraction limit — taste wanted "
                         f"f/{baseline_aperture:.0f}, which would be mush")
        else:
            notes.append(f"held at the f/{aperture:.1f} diffraction limit — don't stop down "
                         f"further (use an ND filter for very bright scenes)")

    return ExposureSolution(iso=iso, aperture=aperture, shutter_s=shutter,
                            hit_iso_ceiling=hit_ceiling, below_shutter_floor=below_floor,
                            diffraction_limited=diffraction_limited, overexposed=overexposed,
                            notes=notes)


# --- diffraction classifier (constraint-card reasons) ---------------------
def diffraction_status(aperture: float, sensor: SensorProfile) -> tuple[str, str] | None:
    """Classify how badly diffraction softens the image at this aperture, for the constraint
    card. None = sharp; ('soft', msg) past the diffraction-limited aperture; ('hard', msg)
    past the circle-of-confusion cutoff where fine detail is destroyed."""
    soft = sensor.diffraction_soft_aperture
    hard = sensor.diffraction_hard_aperture
    if aperture >= hard - 1e-6:
        return "hard", (f"f/{aperture:.1f} past the f/{hard:.0f} diffraction cutoff — "
                        "even print-scale detail is gone")
    if aperture >= soft - 1e-6:
        return "soft", (f"f/{aperture:.1f} past the f/{soft:.0f} diffraction-limited aperture "
                        "— fine detail softening")
    return None


# --- self-test (run: python backend/physics.py) ---------------------------
def _selftest() -> None:
    def close(a: float, b: float, tol: float = 0.02) -> bool:
        return abs(a - b) <= tol * max(1.0, abs(b))

    # EV math against known photographic values.
    assert close(ev100(2.8, 1 / 30), 7.88, 0.01), ev100(2.8, 1 / 30)
    assert close(ev100(8, 1 / 250), 13.97, 0.01), ev100(8, 1 / 250)

    # Crop factor + circle of confusion for the A7 II (full frame).
    assert close(A7II.crop_factor, 1.0, 0.02), A7II.crop_factor
    assert close(A7II.coc_mm, 0.0287, 0.02), A7II.coc_mm

    # A7 III spec geometry is also full-frame (different exact mm, same class).
    assert close(A7III.crop_factor, 1.0, 0.02), A7III.crop_factor
    assert A7III.base_iso == 100 and A7III.width_px == 6000, A7III

    # The vibration factor: 1/focal-length, full frame.
    assert close(handheld_shutter_floor(50), 1 / 50), handheld_shutter_floor(50)
    assert close(handheld_shutter_floor(28), 1 / 28), handheld_shutter_floor(28)

    # Hyperfocal ~10.9 m at 50 mm f/8, and DoF near limit = H/2 when focused there.
    h_mm = hyperfocal_mm(50, 8, A7II.coc_mm)
    assert close(h_mm / 1000, 10.9, 0.03), h_mm / 1000
    near, far = depth_of_field_m(h_mm / 1000, 50, 8, A7II.coc_mm)
    assert close(near, h_mm / 2000, 0.02) and far == float("inf"), (near, far)

    floor50 = handheld_shutter_floor(50)  # 1/50 s

    # Dim room: should raise ISO to ~1225 and hold f/2.8 at 1/50.
    dim = solve_exposure(5.0, baseline_aperture=2.8, shutter_floor_s=floor50,
                         lens_min_aperture=2.8, lens_max_aperture=22, max_iso=6400)
    assert dim.aperture == 2.8 and 1100 < dim.iso < 1350, dim
    assert close(dim.shutter_s, 1 / 50, 0.05) and not dim.below_shutter_floor, dim

    # Near dark: ISO pins at the 6400 ceiling, can't open past f/2.8 -> below the floor.
    dark = solve_exposure(2.0, baseline_aperture=2.8, shutter_floor_s=floor50,
                          lens_min_aperture=2.8, lens_max_aperture=22, max_iso=6400)
    assert dark.iso == 6400 and dark.hit_iso_ceiling and dark.below_shutter_floor, dark
    assert dark.shutter_s > floor50, dark

    # Bright daylight: base ISO, hold f/2.8, fast shutter (well under the floor).
    bright = solve_exposure(13.0, baseline_aperture=2.8, shutter_floor_s=floor50,
                            lens_min_aperture=2.8, lens_max_aperture=22, max_iso=6400)
    assert bright.iso == 100 and bright.aperture == 2.8, bright
    assert bright.shutter_s < floor50 and close(bright.shutter_s, 1 / 1045, 0.05), bright

    # The fully-satisfiable solutions must actually hit their target exposure.
    assert close(dim.ev100_achieved, 5.0, 0.05), dim.ev100_achieved
    assert close(bright.ev100_achieved, 13.0, 0.05), bright.ev100_achieved

    # --- diffraction: physics is authoritative over taste on aperture too ---
    # The A7 II (24 MP full frame): diffraction-limited ~f/9, detail destroyed ~f/21.
    assert 8.0 < A7II.diffraction_soft_aperture < 10.0, A7II.diffraction_soft_aperture
    assert 20.0 < A7II.diffraction_hard_aperture < 23.0, A7II.diffraction_hard_aperture

    # The constraint-card classifier: sharp / softening / destroyed.
    assert diffraction_status(5.6, A7II) is None, "f/5.6 should read sharp"
    assert diffraction_status(13.0, A7II)[0] == "soft", "f/13 should read softening"
    assert diffraction_status(22.0, A7II)[0] == "hard", "f/22 should read destroyed"

    hard = A7II.diffraction_hard_aperture
    # Taste says f/25 (Arsenal's mush) in bright light -> physics caps at the hard limit + flags it.
    capped = solve_exposure(15.0, baseline_aperture=25.0, shutter_floor_s=floor50,
                            lens_min_aperture=2.8, lens_max_aperture=22, max_iso=6400,
                            diffraction_max_aperture=hard)
    assert capped.diffraction_limited and abs(capped.aperture - hard) < 0.1, capped
    # A legitimate deep-DoF f/16 is below the cutoff -> allowed, taste wins when physics agrees.
    ok = solve_exposure(11.0, baseline_aperture=16.0, shutter_floor_s=floor50,
                        lens_min_aperture=2.8, lens_max_aperture=22, max_iso=6400,
                        diffraction_max_aperture=hard)
    assert not ok.diffraction_limited and ok.aperture == 16.0, ok
    # Regression: the old unclamped calls (no diffraction arg) are untouched.
    assert not dim.diffraction_limited and not bright.diffraction_limited

    # Regression (audit H3): steadier must allow a SLOWER floor — the bug had it inverted.
    assert handheld_shutter_floor(50, stability=2.0) > handheld_shutter_floor(50, stability=1.0), \
        "stability inverted: steadier should allow a slower shutter, not a faster one"
    # Regression (audit M10): an untameable bright scene flags `overexposed` (not just a note).
    glare = solve_exposure(25.0, baseline_aperture=2.8, shutter_floor_s=floor50,
                           lens_min_aperture=2.8, lens_max_aperture=22, max_iso=6400)
    assert glare.overexposed and glare.aperture == 22.0, glare

    print("physics self-test: PASS")
    print(f"  dim room   -> ISO {dim.iso}, f/{dim.aperture}, 1/{1/dim.shutter_s:.0f}s")
    print(f"  near dark  -> ISO {dark.iso}, f/{dark.aperture}, 1/{1/dark.shutter_s:.0f}s  "
          f"(below floor: {dark.below_shutter_floor})")
    print(f"  daylight   -> ISO {bright.iso}, f/{bright.aperture}, 1/{1/bright.shutter_s:.0f}s")
    print(f"  diffraction-> soft f/{A7II.diffraction_soft_aperture:.1f} / hard "
          f"f/{A7II.diffraction_hard_aperture:.1f}; taste f/25 capped to f/{capped.aperture}")


if __name__ == "__main__":
    _selftest()
