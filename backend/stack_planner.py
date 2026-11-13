"""Deterministic, conservative stack suggestions and timelapse planning math.

Suggestions do not trigger a shutter. The caller must select a supported mode and
own the capture session; focus actuation and Holy Grail metering are not implied.
"""
from __future__ import annotations
import math


def plan_stack(*, highlight_fraction: float = 0.0, shadow_fraction: float = 0.0,
               motion_fraction: float | None = None, intent: str = "",
               override: str = "auto", frames: int = 3) -> dict:
    from stack_session import validate_frames
    validate_frames(frames)
    for value in (highlight_fraction, shadow_fraction):
        if not math.isfinite(value) or not 0 <= value <= 1:
            raise ValueError("clipping fractions must be finite values between 0 and 1")
    if motion_fraction is not None and (not math.isfinite(motion_fraction) or not 0 <= motion_fraction <= 1):
        raise ValueError("motion fraction must be between 0 and 1")
    modes = ("auto", "off", "hdr", "long_exposure", "crowd_remove", "night")
    if override not in modes:
        raise ValueError(f"override must be one of {modes}")
    if override == "off":
        return {"mode": "single", "frames": 1, "reason": "Stacking is off."}
    if override != "auto":
        return {"mode": override, "frames": frames, "reason": "Explicit mode selection."}
    if motion_fraction is None:
        return {"mode": "single", "frames": 1, "reason": "Motion is unmeasured; automatic stacking is withheld."}
    if motion_fraction > 0.08:
        return {"mode": "single", "frames": 1, "reason": "Motion risks duplicate subjects across frames."}
    if "long exposure" in intent.lower():
        return {"mode": "long_exposure", "frames": min(8, max(3, frames)),
                "reason": "Requested long-exposure blending with low measured motion."}
    if highlight_fraction >= 0.02 and shadow_fraction >= 0.10:
        return {"mode": "hdr", "frames": frames, "ev_step": 1.0,
                "reason": "Both highlight and shadow clipping are present with low measured motion."}
    return {"mode": "single", "frames": 1, "reason": "No measured benefit justifies extra captures."}


def ramp_ev(current_ev: float, target_ev: float, *, max_step_ev: float = 0.3) -> float:
    """One smooth EV step; this is ramp math, not a scene meter or actuator."""
    if not all(math.isfinite(v) for v in (current_ev, target_ev, max_step_ev)) or not 0 < max_step_ev <= 0.3:
        raise ValueError("EV values must be finite and max_step_ev in (0, 0.3]")
    return current_ev + max(-max_step_ev, min(max_step_ev, target_ev - current_ev))


def smartlapse_plan(duration_s: float, output_s: float, fps: float = 24.0) -> dict:
    from stack_session import validate_frames
    if not all(math.isfinite(v) and v > 0 for v in (duration_s, output_s, fps)):
        raise ValueError("durations and frame rate must be finite and positive")
    frames = max(2, round(output_s * fps))
    validate_frames(frames, sequence=True)
    return {"frames": frames, "interval_s": duration_s / (frames - 1),
            "output_s": frames / fps, "fps": fps,
            "note": "Capture/transfer overruns extend duration; camera motion and slider sync are unsupported."}


def deflicker_gains(luminance: list[float], radius: int = 2, max_gain_ev: float = 1.0) -> list[float]:
    """Bounded log-luminance median correction; preserves gradual scene trends.

    Returns linear image gains, without silently rewriting originals. This is an
    optional post-processing primitive; rapid intentional changes may also smooth.
    """
    import statistics
    if not luminance or len(luminance) > 240:
        raise ValueError("need 1..240 luminance measurements")
    if isinstance(radius, bool) or not isinstance(radius, int) or not 1 <= radius <= 10:
        raise ValueError("radius must be an integer between 1 and 10")
    if not math.isfinite(max_gain_ev) or not 0 <= max_gain_ev <= 2:
        raise ValueError("max_gain_ev must be between 0 and 2")
    if not all(math.isfinite(v) and v > 0 for v in luminance):
        raise ValueError("luminance must be finite and positive")
    logs = [math.log2(v) for v in luminance]
    return [2 ** max(-max_gain_ev, min(max_gain_ev,
                statistics.median(logs[max(0, i - radius):i + radius + 1]) - value))
            for i, value in enumerate(logs)]


TIMELAPSE_DEFAULTS = {"target_luminance": 0.18, "max_step_ev": 0.3, "max_total_ev": 6.0,
                      "min_shutter_s": 1 / 8000, "max_shutter_s": 30.0,
                      "min_iso": 100, "max_iso": 6400, "fps": 24.0, "priority": "shutter_then_iso",
                      "aperture_locked": True, "min_aperture": 0.5, "max_aperture": 128.0,
                      "smoothing_frames": 1}
# Holy Grail smooths the metered scene level over ~8 frames: in simulation a 2 EV
# change lands in ~10 frames and 5 EV in ~17 (Arsenal documents transitions of
# "about 20"), with no overshoot. A trend-following filter lagged a sunset less
# but overshot a 5 EV step by 1 EV. Day to night needs more than 6 EV, so the
# default range is the shutter/ISO ladders themselves, bounded at 20 EV.
HOLY_GRAIL_DEFAULTS = {"smoothing_frames": 8, "max_total_ev": 20.0}
MODE_DEFAULTS = {"auto_custom": {"max_step_ev": 1.0}, "holy_grail": HOLY_GRAIL_DEFAULTS}
# One meter reading may move the smoothed scene level by at most this much surprise.
# In simulation a single black or white frame (transfer glitch, hand over the lens)
# then peaks at 0.30 EV for 3 frames, and a genuine 5 EV change still lands in 20.
RESIDUAL_CLIP_EV = 1.5


def timelapse_config(mode="fixed", options=None):
    if mode not in ("fixed", "auto_custom", "holy_grail"):
        raise ValueError("timelapse_mode must be fixed, auto_custom or holy_grail")
    if options is not None and not isinstance(options, dict):
        raise ValueError("timelapse_options must be an object")
    if set(options or {}) - set(TIMELAPSE_DEFAULTS):
        raise ValueError("unknown timelapse option")
    result = {**TIMELAPSE_DEFAULTS, **MODE_DEFAULTS.get(mode, {}), **(options or {})}
    bounds = {"target_luminance": (0.01, 0.8), "max_step_ev": (0.01, 1.0 if mode == "auto_custom" else 0.3),
              "max_total_ev": (0, 20 if mode == "holy_grail" else 6), "min_shutter_s": (1 / 8000, 30),
              "max_shutter_s": (1 / 8000, 30), "min_iso": (50, 409600),
              "max_iso": (50, 409600), "fps": (1, 60),
              "min_aperture": (.5, 128), "max_aperture": (.5, 128), "smoothing_frames": (1, 60)}
    if type(result["smoothing_frames"]) is not int:
        raise ValueError("smoothing_frames must be an integer")
    import itertools
    priorities = {"shutter_then_iso", "iso_then_shutter"} | {
        "_then_".join(order) for order in itertools.permutations(("shutter", "iso", "aperture"))}
    if not isinstance(result["priority"], str) or result["priority"] not in priorities:
        raise ValueError("invalid timelapse priority")
    if type(result["aperture_locked"]) is not bool:
        raise ValueError("aperture_locked must be a boolean")
    for name, value in result.items():
        if name in ("priority", "aperture_locked"):
            continue
        if type(value) not in (int, float) or not math.isfinite(value) or not bounds[name][0] <= value <= bounds[name][1]:
            raise ValueError(f"invalid timelapse {name}")
    if result["min_aperture"] > result["max_aperture"]:
        raise ValueError("aperture range minimum exceeds maximum")
    if result["min_iso"] > result["max_iso"] or result["min_shutter_s"] > result["max_shutter_s"]:
        raise ValueError("timelapse range minimum exceeds maximum")
    return {"mode": mode, **result}


def jpeg_luminance(path):
    """Bounded saved-image meter; no preview, model, or extra exposure is used."""
    import numpy as np
    from image_safety import open_rgb
    with open_rgb(path) as image:
        image.thumbnail((320, 240))
        pixels = np.asarray(image.convert("RGB"), dtype=float) / 255
    linear = np.where(pixels <= 0.04045, pixels / 12.92, ((pixels + 0.055) / 1.055) ** 2.4)
    y = linear @ np.array([0.2126, 0.7152, 0.0722])
    return {"linear_luminance": max(1e-6, float(np.mean(y))),
            "highlight_fraction": float(np.mean(y >= 0.98)), "source": "accepted_jpeg"}


def metered_timelapse_step(config, *, current_ev, luminance, base_shutter_s, base_iso, caps,
                          base_aperture=None, mounted_lens_evidence=None, measured_ev=None,
                          scene_estimate=None):
    """Ordered, bounded exposure cascade; aperture remains locked unless explicit.

    Aperture movement requires a caller-supplied mounted-lens intersection. Raw
    advertised driver apertures alone cannot enable optical movement.

    ``measured_ev`` is the quantized EV the metered frame was actually exposed at;
    the meter reading describes that exposure, not the smooth requested ramp. With
    ``smoothing_frames`` > 1 the metered scene level is exponentially smoothed over
    about that many frames (the returned ``scene_ev_estimate`` is passed back next
    frame), so meter noise does not make the exposure hunt. Between captured frames
    each request stays within ``max_step_ev`` of the previous one; the first frame,
    or a range edited mid-run, may jump to the nearest EV the ranges allow. Every
    request stays inside ``reachable_ev`` (the EVs the configured shutter/ISO/aperture
    ladders can actually produce within ``max_total_ev``), so the ramp never runs
    away from the camera; a target outside that span is reported as ``range_limited``.
    """
    if not all(math.isfinite(v) and v > 0 for v in (luminance, base_shutter_s, base_iso)):
        raise ValueError("meter and baseline exposure must be finite and positive")
    if not math.isfinite(current_ev):
        raise ValueError("current EV must be finite")
    if any(v is not None and not math.isfinite(v) for v in (measured_ev, scene_estimate)):
        raise ValueError("measured EV and scene estimate must be finite")
    reference_ev = current_ev if measured_ev is None else measured_ev
    unbounded = reference_ev + math.log2(config["target_luminance"] / luminance)
    if not math.isfinite(unbounded):
        raise ValueError("meter luminance is outside the representable range")
    desired = max(-config["max_total_ev"], min(config["max_total_ev"], unbounded))
    smoothing = config.get("smoothing_frames", 1)
    estimate = None
    if smoothing > 1 and measured_ev is not None:
        # With no estimate yet, start from the exposure the metered frame used, so the
        # first reading is clipped like any other: a black first frame must not seed
        # the level 17 EV away.
        previous = reference_ev if scene_estimate is None else scene_estimate
        surprise = max(-RESIDUAL_CLIP_EV, min(RESIDUAL_CLIP_EV, unbounded - previous))
        estimate = previous + 2 / (smoothing + 1) * surprise
        aim = max(-config["max_total_ev"], min(config["max_total_ev"], estimate))
    else:
        aim = desired
    requested = current_ev + max(-config["max_step_ev"], min(config["max_step_ev"], aim - current_ev))
    shutters = [(s, label) for s, label in zip(caps.shutters, caps.shutter_labels)
                if config["min_shutter_s"] <= s <= config["max_shutter_s"]]
    isos = [i for i in caps.isos if config["min_iso"] <= i <= config["max_iso"]]
    if not shutters or not isos:
        raise ValueError("timelapse ranges contain no live camera choices")
    unlocked = not config.get("aperture_locked", True)
    base_f = 1.0 if base_aperture is None else float(str(base_aperture).lower().removeprefix("f/"))
    if not math.isfinite(base_f) or base_f <= 0:
        raise ValueError("baseline aperture must be finite and positive")
    aperture_labels = caps.aperture_labels or [str(v) for v in caps.apertures]
    if unlocked:
        if base_aperture is None or not isinstance(mounted_lens_evidence, dict):
            raise ValueError("Aperture movement requires confirmed mounted-lens bounds")
        allowed = mounted_lens_evidence.get("usable_apertures", [])
        apertures = [(f, label) for f, label in zip(caps.apertures, aperture_labels)
                     if any(abs(f-v) < 1e-6 for v in allowed)
                     and config.get("min_aperture", .5) <= f <= config.get("max_aperture", 128)]
        if not apertures:
            raise ValueError("Requested aperture range contains no verified mounted-lens choices")
    else:
        # Exact baseline label remains authoritative for the default locked mode.
        apertures = [(base_f, str(base_aperture))]
    selected = {"shutter": min(shutters, key=lambda p: abs(math.log2(p[0]/base_shutter_s))),
                "iso": min(isos, key=lambda v: abs(math.log2(v/base_iso))),
                "aperture": min(apertures, key=lambda p: abs(math.log2(p[0]/base_f)))}
    baseline_product = base_shutter_s * base_iso / base_f ** 2
    # The EVs the ladders can actually produce inside the total cap, not a continuous
    # span: a cap smaller than one ladder step must not invent a reachable EV.
    achievable = [ev for ev in (math.log2(s * i / f ** 2 / baseline_product)
                                for s, _ in shutters for i in isos for f, _ in apertures)
                  if abs(ev) <= config["max_total_ev"] + 1e-9]
    reachable = (min(achievable), max(achievable)) if achievable else None
    if reachable:
        # Anti-windup: never ramp past what the ranges can deliver, so the ramp is
        # already at the edge when the light turns back.
        requested = max(reachable[0], min(reachable[1], requested))
    target_product = baseline_product * 2 ** requested
    order = config.get("priority", "shutter_then_iso").split("_then_")
    if unlocked and "aperture" not in order:
        order.append("aperture")
    if not unlocked:
        order = [axis for axis in order if axis != "aperture"]
    def exposure(selection):
        return selection["shutter"][0] * selection["iso"] / selection["aperture"][0] ** 2
    ladders = {"shutter": shutters, "iso": isos, "aperture": apertures}
    for axis in order:
        selected[axis] = min(ladders[axis], key=lambda candidate:
            abs(math.log2(exposure({**selected, axis: candidate}) / target_product)))
    actual_ev = math.log2(exposure(selected) / baseline_product)
    if abs(actual_ev) > config["max_total_ev"] + 1e-9:
        bounded = ({"shutter": shutter, "iso": iso, "aperture": aperture}
                   for shutter in shutters for iso in isos for aperture in apertures)
        bounded = [candidate for candidate in bounded
                   if abs(math.log2(exposure(candidate)/baseline_product)) <= config["max_total_ev"] + 1e-9]
        if not bounded:
            raise ValueError("live choices cannot satisfy total EV and exposure ranges")
        selected = min(bounded, key=lambda candidate: abs(math.log2(exposure(candidate)/target_product)))
        actual_ev = math.log2(exposure(selected) / baseline_product)
    # Ordinary third-stop rounding (< .34 EV) is not a limit; a hard range or total-EV
    # edge is one however small the shortfall.
    total_ev_limited = abs(unbounded-desired) > 1e-6
    beyond_ranges = bool(reachable) and (aim > reachable[1] + 1e-6 or aim < reachable[0] - 1e-6)
    result = {"requested_ev": requested, "actual_ev": actual_ev,
        "target_ev": desired, "shutter": selected["shutter"][1], "iso": str(selected["iso"]),
        "range_limited": abs(actual_ev-requested) > .34 or beyond_ranges or total_ev_limited,
        "total_ev_limited": total_ev_limited, "reachable_ev": list(reachable) if reachable else None,
        "meter_luminance": luminance, "measured_ev": measured_ev,
        "cascade": "_then_".join(order), "aperture_locked": not unlocked,
        "effective_aperture_locked": len(apertures) == 1,
        "quantization_error_ev": actual_ev-requested,
        "requested_step_ev": requested-current_ev,
        "smoothing_frames": config.get("smoothing_frames", 1),
        "aperture_choices": [f for f, _ in apertures]}
    if estimate is not None:
        result["scene_ev_estimate"] = estimate
    if base_aperture is not None:
        result["aperture"] = selected["aperture"][1]
    if mounted_lens_evidence is not None:
        result["mounted_lens_limits"] = mounted_lens_evidence
    return result
