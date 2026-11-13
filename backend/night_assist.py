"""Night Assist (Arsenal A23) — plan a bounded night exposure from the body's *measured*
noise, not a fixed recipe.

The transport caps an ordinary single exposure at 1 s
(``sonyremote.camera.SONY_ACTION_MAX_ENABLED_SHUTTER_S``), so night work here is multi-frame
*averaging*: stack N short exposures and the random noise falls as sigma/sqrt(N). This module
picks ISO / shutter / aperture and the frame count N so that averaging brings the measured
noise at the chosen ISO back down to near the base-ISO noise, and it refuses to invent a plan
when the scene is simply too dark for the shutter/ISO ceiling.

Pure and deterministic and camera-free: it reasons over a resolved ``rig_profile.RigProfile``
(which carries the measured noise table) and the body's live ``camera_caps.CameraCapabilities``
ladders. ``plan_from_meter_frame`` adds the EXIF/metering front-end so a single JPEG becomes a
plan; ``to_session_request`` turns a plan into the exact ``POST /sessions`` body the durable
session runner already executes (mode "night").

    python3 backend/night_assist.py   # self-test on a synthetic measured table
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field

import physics

# Averaging target: N is chosen so the measured sigma at the working ISO, divided by sqrt(N),
# lands at or below the base-ISO sigma times this factor. 1.25 = "within a quarter-stop of
# base-ISO noise" — clean enough for night without demanding an unreachable frame count.
NOISE_TARGET_RATIO = 1.25

# Night averaging is a stack: at least two frames, never more than the session runner's cap.
MIN_FRAMES = 2
DEFAULT_MAX_FRAMES = 16

_EPS = 1e-9


@dataclass(frozen=True)
class NightPlan:
    status: str                       # "ok" | "insufficient_light" | "meter_unreliable"
    mode: str = "night"
    iso: int | None = None
    aperture: float | None = None
    aperture_label: str | None = None
    shutter_s: float | None = None
    shutter_label: str | None = None
    frames: int = 0
    noise_reduction_db: float | None = None
    total_time_s: float | None = None
    scene_ev100: float | None = None
    shortfall_ev: float | None = None
    why: list[str] = field(default_factory=list)
    evidence: dict = field(default_factory=dict)


# --- noise helpers ----------------------------------------------------------------------- #
def _base_iso(rig) -> int:
    noise = getattr(rig, "noise", None)
    if noise is not None and hasattr(noise, "base_iso"):
        return int(noise.base_iso)
    sensor = getattr(rig, "sensor", None)
    if sensor is not None and getattr(sensor, "base_iso", None):
        return int(sensor.base_iso)
    return 100


def _iso_ceiling(rig) -> tuple[int, str]:
    """(usable-ISO ceiling, noise_source) for the resolved rig.

    A measured JPEG-noise table or the A7 II electron model each yield a real ceiling; a body
    with no noise model falls back to a conservative fixed ISO 6400 (documented, not measured).
    """
    noise = getattr(rig, "noise", None)
    if noise is None:
        return 6400, "uncalibrated_conservative"
    source = ("measured_jpeg_noise" if hasattr(noise, "sigma_luma_8bit")
              else "a7ii_electron_noise_model")
    return int(noise.usable_iso_ceiling()), source


def _sigma_ratio(rig, iso: int, base_iso: int, exposure_s: float) -> tuple[float, str]:
    """Return (sigma_at_iso / sigma_at_base_iso, noise_source).

    Measured JPEG table: the ratio of measured 8-bit luma sigmas. A7 II electron model: the
    inverse SNR ratio at a fixed 18% patch and the *same* exposure time, so the ratio isolates
    the gain (ISO) effect. No model: a conservative shot-noise approximation sqrt(iso/base)."""
    noise = getattr(rig, "noise", None)
    if noise is not None and hasattr(noise, "sigma_luma_8bit"):
        base = noise.sigma_luma_8bit(base_iso)
        return noise.sigma_luma_8bit(iso) / max(base, _EPS), "measured_jpeg_noise"
    if noise is not None and hasattr(noise, "snr"):
        snr_base = noise.snr(0.18, base_iso, exposure_s=exposure_s, base_iso=base_iso)
        snr_iso = noise.snr(0.18, iso, exposure_s=exposure_s, base_iso=base_iso)
        return snr_base / max(snr_iso, _EPS), "a7ii_electron_noise_model"
    return math.sqrt(max(iso, base_iso) / max(base_iso, 1)), "uncalibrated_shot_noise_approx"


def _sigma_at(rig, iso: int, exposure_s: float) -> float | None:
    """Measured 8-bit luma sigma at an ISO, when the rig has a JPEG-noise table (else None)."""
    noise = getattr(rig, "noise", None)
    if noise is not None and hasattr(noise, "sigma_luma_8bit"):
        return float(noise.sigma_luma_8bit(iso))
    return None


def _frames_for_ratio(ratio: float, *, target_ratio: float, max_frames: int,
                      min_snr_gain_db: float | None) -> tuple[int, bool]:
    """Smallest N in [MIN_FRAMES, max_frames] with ratio/sqrt(N) <= target_ratio, also honoring
    an optional minimum averaging gain (dB). Returns (N, clamped) where clamped is True when the
    noise target could not be met within max_frames."""
    need = 1.0
    if ratio > target_ratio:
        need = (ratio / target_ratio) ** 2  # ratio / sqrt(N) <= target  ->  N >= (ratio/target)^2
    frames = max(MIN_FRAMES, math.ceil(need - _EPS))
    if min_snr_gain_db is not None:
        frames = max(frames, math.ceil(10 ** (min_snr_gain_db / 10.0) - _EPS))
    clamped = frames > max_frames
    return min(frames, max_frames), clamped


# --- shutter / aperture selection -------------------------------------------------------- #
def _handheld_floor_s(focal_mm: float | None) -> float:
    """Slowest safe *handheld* shutter for a night frame: 1/(2*focal) when focal is known,
    else a conservative 1/60 s."""
    if focal_mm and focal_mm > 0:
        return 1.0 / (2.0 * float(focal_mm))
    return 1.0 / 60.0


def _pick_shutter(caps, cap_s: float) -> tuple[float, str] | None:
    """Longest settable shutter at or below cap_s (most light without exceeding the cap)."""
    best = None
    for value, label in zip(caps.shutters, caps.shutter_labels):
        if value <= cap_s + _EPS and (best is None or value > best[0]):
            best = (value, label)
    return best


def _pick_aperture(caps, aperture_label: str | None) -> tuple[float, str]:
    """Widest available aperture (smallest f-number), or the requested one snapped to the ladder."""
    if aperture_label is not None:
        f = float(str(aperture_label).lower().removeprefix("f/"))
        return caps.snap_aperture(f)
    value = min(caps.apertures)
    if caps.aperture_labels:
        label = caps.aperture_labels[caps.apertures.index(value)]
    else:
        label = f"f/{value:g}"
    return value, label


# --- the planner ------------------------------------------------------------------------- #
def plan_night(scene_ev100: float, rig, caps, *, max_shutter_s: float = 1.0,
               max_frames: int = DEFAULT_MAX_FRAMES, subject_motion_floor_s: float | None = None,
               target_snr_gain_db: float | None = None, aperture_label: str | None = None,
               tripod: bool = True, focal_mm: float | None = None,
               reliability: dict | None = None,
               ev_reference: str = physics.EV_THROUGH_THIS_LENS) -> NightPlan:
    """Plan a bounded night averaging session from measured noise.

    ISO <= the rig's measured usable ceiling (conservative 6400 when unmeasured); shutter <=
    min(max_shutter_s, subject motion floor, and — when tripod=False — the handheld floor);
    aperture = widest available unless given; frames N in [2, max_frames] so measured
    sigma/sqrt(N) <= base-ISO sigma * 1.25. Too dark for the ceiling -> status
    "insufficient_light" with the EV shortfall and no frames. A through-this-lens `scene_ev100`
    (every meter frame) already contains the lens's light loss; a scene luminance is dimmed by the
    rig's published lens transmission before ISO and the shortfall are solved (unknown: unchanged).
    """
    transmission = getattr(rig, "lens_transmission", None)
    offset = transmission.t_stop_offset_stops if transmission is not None else None
    exposure_ev100 = physics.lens_adjusted_ev100(scene_ev100, ev_reference=ev_reference,
                                                 t_stop_offset_stops=offset)
    exposure_reference = {"ev_reference": ev_reference, "exposure_ev100": round(exposure_ev100, 4),
                          "applied_offset_stops": round(scene_ev100 - exposure_ev100, 4),
                          "lens_transmission": (transmission.to_dict() if transmission is not None
                                                else {"status": "unknown"})}
    base_iso = _base_iso(rig)
    ceiling, ceiling_source = _iso_ceiling(rig)
    max_frames = max(MIN_FRAMES, int(max_frames))

    aperture, ap_label = _pick_aperture(caps, aperture_label)

    # Shutter cap: transport/user limit, subject motion floor, and (handheld) the shake floor.
    cap_s = float(max_shutter_s)
    why: list[str] = []
    if subject_motion_floor_s is not None and subject_motion_floor_s > 0:
        cap_s = min(cap_s, float(subject_motion_floor_s))
        why.append(f"Shutter capped to the subject-motion floor {subject_motion_floor_s:.4g}s.")
    handheld_capped = False
    if not tripod:
        floor = _handheld_floor_s(focal_mm)
        if floor < cap_s:
            handheld_capped = True
            cap_s = floor
        focal_note = f"1/(2*{focal_mm:.0f}mm)" if focal_mm else "1/60s default"
        why.append(f"tripod=False: frames longer than the handheld floor ({focal_note}) refused.")

    picked = _pick_shutter(caps, cap_s)
    if picked is None:
        return NightPlan(status="insufficient_light", scene_ev100=round(scene_ev100, 3),
                         why=["No settable shutter at or below the required cap."],
                         evidence={"noise_source": ceiling_source, "ceiling": ceiling,
                                   "reliability": reliability})
    shutter_s, shutter_label = picked

    # Highest settable ISO still under the noise ceiling sets the darkest exposable scene.
    # Never below the rig's base ISO: the driver menu advertises extended-low values that cost
    # dynamic range, and never above the measured ceiling.
    usable_isos = sorted(i for i in caps.isos if base_iso <= i <= ceiling)
    if not usable_isos:
        raise ValueError(f"no ISO between base {base_iso} and ceiling {ceiling} on this body's ladder")
    iso_cap = usable_isos[-1]
    # Scene brightness the rig can correctly expose at (iso_cap, widest f, longest allowed shutter).
    min_scene_ev100 = physics.ev100(aperture, shutter_s) - math.log2(iso_cap / 100.0)
    if exposure_ev100 < min_scene_ev100 - 1e-6:
        shortfall = min_scene_ev100 - exposure_ev100
        adjusted = ("" if exposure_ev100 == scene_ev100 else
                    f" after {scene_ev100 - exposure_ev100:.2f} EV of lens transmission loss "
                    f"(scene EV100 {scene_ev100:.2f})")
        why.append(f"Exposure EV100 {exposure_ev100:.2f}{adjusted} is {shortfall:.2f} EV below the "
                   f"darkest the rig can expose (ISO {iso_cap}, {shutter_label}s, f/{aperture:g}).")
        return NightPlan(status="insufficient_light", scene_ev100=round(scene_ev100, 3),
                         shortfall_ev=round(shortfall, 3), aperture=aperture, aperture_label=ap_label,
                         shutter_s=shutter_s, shutter_label=shutter_label, why=why,
                         evidence={"noise_source": ceiling_source, "ceiling": ceiling,
                                   "sigma_at_iso": None, "sigma_after_average": None, "frames": 0,
                                   "shutter_s": shutter_s, "iso": iso_cap,
                                   "ev100": round(scene_ev100, 3), "reliability": reliability,
                                   "exposure_reference": exposure_reference})

    # ISO for correct exposure at the chosen shutter/aperture, snapped up to the ladder, capped.
    needed_iso = 100.0 * 2 ** (physics.ev100(aperture, shutter_s) - exposure_ev100)
    iso = next((i for i in usable_isos if i >= needed_iso - _EPS), iso_cap)

    ratio, noise_source = _sigma_ratio(rig, iso, base_iso, shutter_s)
    frames, clamped = _frames_for_ratio(ratio, target_ratio=NOISE_TARGET_RATIO,
                                        max_frames=max_frames, min_snr_gain_db=target_snr_gain_db)
    sigma_at = _sigma_at(rig, iso, shutter_s)
    sigma_base = _sigma_at(rig, base_iso, shutter_s)
    sigma_after = sigma_at / math.sqrt(frames) if sigma_at is not None else None
    noise_reduction_db = 10.0 * math.log10(frames)
    total_time_s = frames * shutter_s

    why.append(f"Measured noise ({noise_source}); usable-ISO ceiling {ceiling}.")
    why.append(f"ISO {iso} (needed {needed_iso:.0f}) at {shutter_label}s f/{aperture:g}; "
               f"noise x{ratio:.2f} vs base ISO {base_iso}.")
    why.append(f"N={frames}: sigma/sqrt(N) -> x{ratio / math.sqrt(frames):.2f} of base "
               f"(target x{NOISE_TARGET_RATIO}); {noise_reduction_db:.1f} dB reduction, "
               f"{total_time_s:.1f}s total.")
    if clamped:
        why.append(f"Frame count clamped to {max_frames}; residual noise stays x"
                   f"{ratio / math.sqrt(frames):.2f} of base (target x{NOISE_TARGET_RATIO} not met).")

    return NightPlan(
        status="ok", iso=int(iso), aperture=aperture, aperture_label=ap_label,
        shutter_s=shutter_s, shutter_label=shutter_label, frames=int(frames),
        noise_reduction_db=round(noise_reduction_db, 2), total_time_s=round(total_time_s, 3),
        scene_ev100=round(scene_ev100, 3), why=why,
        evidence={"noise_source": noise_source, "ceiling": ceiling,
                  "sigma_at_iso": round(sigma_at, 4) if sigma_at is not None else None,
                  "sigma_after_average": round(sigma_after, 4) if sigma_after is not None else None,
                  "frames": int(frames), "shutter_s": shutter_s, "iso": int(iso),
                  "ev100": round(scene_ev100, 3),
                  "noise_ratio_to_base": round(ratio, 4),
                  "sigma_base_iso": round(sigma_base, 4) if sigma_base is not None else None,
                  "handheld_capped": handheld_capped, "reliability": reliability,
                  "exposure_reference": exposure_reference})


# --- EXIF/metering front-end ------------------------------------------------------------- #
def _frame_exposure_exif(jpeg_path: str):
    """(lens_model, lens_specification, max_aperture_value, iso, f_number, shutter_s) from EXIF,
    read the same way handheld_recommend does (Exif sub-IFD, falling back to the top level)."""
    from image_safety import open_image
    with open_image(jpeg_path) as image:
        exif = image.getexif()
        ifd = exif.get_ifd(34665) if 34665 in exif else exif
        lens_model = str(ifd.get(42036, exif.get(42036, ""))).strip()
        lens_specification = ifd.get(42034, exif.get(42034))
        max_aperture_value = ifd.get(37381, exif.get(37381))
        iso = ifd.get(34855, exif.get(34855))
        f_number = ifd.get(33437, exif.get(33437))
        shutter = ifd.get(33434, exif.get(33434))
    to_float = lambda value: float(value) if value not in (None, "") else None  # noqa: E731
    return lens_model, lens_specification, max_aperture_value, to_float(iso), to_float(f_number), to_float(shutter)


def plan_from_meter_frame(jpeg_path: str, model: str, iso_choices: list[str],
                          aperture_choices: list[str], shutter_choices: list[str], **kw) -> NightPlan:
    """Meter one JPEG (its own EXIF exposure), resolve the mounted rig, and plan a night session.

    Deterministic. A nearly black/white meter frame (``live_verify.meter_reliability`` "low") is
    reflected in ``status`` ("meter_unreliable") and the ``why`` lines rather than trusted.
    """
    import camera_caps
    import live_verify
    import rig_profile

    lens_model, lens_specification, max_aperture_value, iso, f_number, shutter_s = _frame_exposure_exif(jpeg_path)
    if iso is None or f_number is None or shutter_s is None or shutter_s <= 0:
        raise ValueError("Frame EXIF lacks ISO/aperture/shutter; the frame cannot be metered")
    rig = rig_profile.resolve_rig(model, lens_model=lens_model, lens_specification=lens_specification,
                                  max_aperture_value=max_aperture_value)
    scene_ev100, clipped = live_verify.meter_ev100(jpeg_path, iso, f_number, shutter_s)
    reliability = live_verify.meter_reliability(jpeg_path)
    focal = live_verify.focal_from_exif(jpeg_path, default=None)
    caps = camera_caps.from_live_choices(model, iso_choices, aperture_choices, shutter_choices)
    # The body's generic menus advertise apertures the mounted lens cannot reach and shutters the
    # transport cannot wait for; bound the ladders exactly like an ordinary capture does.
    from sonyremote.camera import SONY_ACTION_MAX_ENABLED_SHUTTER_S
    caps, transport_limits = camera_caps.constrain_capture_duration(
        caps, max_shutter_s=min(float(kw.get("max_shutter_s", SONY_ACTION_MAX_ENABLED_SHUTTER_S)),
                                SONY_ACTION_MAX_ENABLED_SHUTTER_S))
    caps, mounted_lens_limits = camera_caps.constrain_mounted_lens(
        caps, f"f/{f_number:g}", lens_specification=lens_specification,
        max_aperture_value=max_aperture_value,
        verified_profile=(2.8, 22.0) if rig.lens_profiled else None)

    plan = plan_night(scene_ev100, rig, caps, focal_mm=focal, reliability=reliability, **kw)
    plan.evidence.update({"mounted_lens_limits": mounted_lens_limits,
                          "capture_transport_limits": transport_limits})

    why = [f"Metered scene EV100 {scene_ev100:.2f} from the frame's own exposure "
           f"(clipped {clipped:.0%}); rig calibration: {rig.calibration_status}."]
    why.extend(plan.why)
    if reliability.get("status") == "low":
        why.insert(0, f"Meter frame unreliable: {reliability.get('reason')}; EV is a bound, not a "
                      "measurement — refine before trusting this plan.")
        from dataclasses import replace
        return replace(plan, status="meter_unreliable", why=why)
    from dataclasses import replace
    return replace(plan, why=why)


# --- session request --------------------------------------------------------------------- #
def to_session_request(plan: NightPlan, camera_model: str, camera_port: str,
                       camera_serial: str | None = None) -> dict:
    """Exact ``POST /sessions`` body for the durable runner: mode "night", frames from the plan,
    ev_step 0 and interval 0 (a plain average stack, no bracket, back-to-back). ``/sessions``
    refuses a request without the body's USB serial, so the planner carries the selected one."""
    if plan.status != "ok":
        raise ValueError(f"cannot submit a {plan.status} night plan")
    if not MIN_FRAMES <= plan.frames <= DEFAULT_MAX_FRAMES:
        raise ValueError(f"night frame count {plan.frames} is out of the 2..{DEFAULT_MAX_FRAMES} range")
    return {"mode": "night", "frames": int(plan.frames), "ev_step": 0.0, "interval_s": 0.0,
            "camera_model": camera_model, "camera_port": camera_port, "camera_serial": camera_serial}


# --- self-test (run: python backend/night_assist.py) ------------------------------------- #
def _selftest() -> None:
    import sensor_noise
    from dataclasses import dataclass as _dc

    @_dc
    class _Rig:
        noise: object
        sensor: object

    measured = sensor_noise.load_measured_noise({
        "body": "TEST", "base_iso": 100, "ceiling_threshold_ratio": 2.5,
        "noise_table": [{"iso": 100, "sigma_luma_8bit": 2.0},
                        {"iso": 400, "sigma_luma_8bit": 3.0},
                        {"iso": 1600, "sigma_luma_8bit": 4.5},
                        {"iso": 3200, "sigma_luma_8bit": 5.0}]})
    caps = physics  # placeholder; real caps below
    import camera_caps
    caps = camera_caps.from_live_choices(
        "TEST", ["100", "400", "1600", "3200", "6400"],
        ["f/1.8", "f/2.8", "f/8"], ["1", "1/2", "1/4", "1/60", "1/2000"])
    rig = _Rig(noise=measured, sensor=physics.A7III)

    plan = plan_night(2.0, rig, caps)
    assert plan.status == "ok", plan
    assert plan.iso <= 1600, plan.iso  # ceiling: base sigma 2.0, 2.5x bar -> ISO 1600
    print(f"self-test plan: ISO {plan.iso} {plan.shutter_label}s f/{plan.aperture:g} x{plan.frames} "
          f"-> {plan.noise_reduction_db} dB, {plan.total_time_s}s")
    assert to_session_request(plan, "TEST", "usb:001,001")["frames"] == plan.frames

    dark = plan_night(-20.0, rig, caps)
    assert dark.status == "insufficient_light" and dark.shortfall_ev > 0, dark
    print("night-assist self-test: PASS")


if __name__ == "__main__":
    _selftest()
