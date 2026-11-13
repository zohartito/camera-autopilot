"""Explicit exposure budgets and evidence-gated ordinary-capture stack selection.

No camera access lives here. Auto may use a photograph already captured by the
ordinary product flow; it never asks for an extra metering exposure or invents a
motion measurement. Fixed selection is a user-requested session, not an AI claim.
A Fixed stack meters first (one meter frame plus the bounded re-meter) unless the
owner explicitly keeps the body's current exposure with ``baseline: "current"``.
"""
from __future__ import annotations

import math

STACK_MODES = ("hdr", "night", "long_exposure", "crowd_remove")
# "metered": one meter frame sets a Fixed stack's exposure. "current": keep the body's exposure.
BASELINES = ("metered", "current")


def validate_stack_policy(value=None, *, refine=False):
    if value is None:
        value = {"mode": "off"}
    if not isinstance(value, dict) or set(value) - {"mode", "stack_mode", "frames", "baseline"}:
        raise ValueError("stack_policy must contain only mode, stack_mode, frames and baseline")
    mode = value.get("mode", "off")
    stack_mode = value.get("stack_mode", "hdr")
    frames = value.get("frames", 3)
    baseline = value.get("baseline", "metered")
    if mode not in ("off", "auto", "fixed"):
        raise ValueError("stack_policy mode must be off, auto or fixed")
    if stack_mode not in STACK_MODES:
        raise ValueError("stack_mode must be hdr, night, long_exposure or crowd_remove")
    if type(frames) is not int or not 2 <= frames <= 9:
        raise ValueError("stack_policy frames must be an integer between 2 and 9")
    if baseline not in BASELINES:
        raise ValueError("stack_policy baseline must be metered or current")
    if baseline == "current" and mode != "fixed":
        raise ValueError("stack_policy baseline current applies only to a Fixed stack")
    if mode != "off" and refine:
        raise ValueError("stack policy and refinement cannot be combined")
    policy = {"mode": mode, "stack_mode": stack_mode, "frames": frames}
    if mode == "fixed":
        policy["baseline"] = baseline
    return policy


def exposure_budget(policy, *, refine=False):
    import remeter
    mode, frames = policy["mode"], policy["frames"]
    inherited = mode == "fixed" and policy.get("baseline", "metered") == "current"
    # A metered Fixed stack takes one meter frame, and no final ordinary photograph.
    meter = 1 if mode == "fixed" and not inherited else 0
    # Re-metering is bounded per engine attempt; Refine may run two attempts.
    extra_meter = (0 if inherited else remeter.MAX_REMETER_PASSES if mode == "fixed"
                   else remeter.MAX_REMETER_PASSES * (2 if refine else 1))
    return {"max_physical_exposures": (meter + extra_meter + frames if mode == "fixed"
                                       else (4 if refine else 2) + extra_meter + (frames if mode == "auto" else 0)),
            "ordinary_max": 0 if mode == "fixed" else 4 if refine else 2,
            "additional_stack_max": frames if mode != "off" else 0,
            "baseline_meter_exposures": meter,
            # A saturated or black meter frame is re-metered at a corrected exposure before the
            # recommendation; bounded, and never an extra *final* photograph.
            "extra_meter_exposures": extra_meter,
            "fixed_uses_current_camera_settings": inherited}


def decide_stack_policy(policy, shot, image_path, *, intent=""):
    from stack_planner import plan_stack
    if policy["mode"] == "off":
        return {"mode": "single", "frames": 1, "reason": "Stacking is off."}
    if policy["mode"] == "fixed":
        return plan_stack(override=policy["stack_mode"], frames=policy["frames"])
    if not shot.ok or shot.evidence.get("persistence", {}).get("status") == "failed":
        return {"mode": "single", "frames": 1,
                "reason": "The ordinary capture needs review or persistence recovery; no extra exposures were authorized by the policy."}
    motion = shot.evidence.get("motion") or {}
    fraction = motion.get("motion_fraction") if isinstance(motion, dict) else None
    verified = (isinstance(motion, dict) and motion.get("outcome_verified") is True
                and motion.get("status") in ("measured", "verified")
                and type(fraction) in (int, float) and math.isfinite(fraction) and 0 <= fraction <= 1)
    if not verified:
        result = plan_stack(motion_fraction=None, intent=intent or "", frames=policy["frames"])
        result["evidence_source"] = "existing_shot_motion_unavailable"
        return result
    # Reuse the accepted JPEG; a single image supplies clipping, never temporal motion.
    from image_safety import open_image
    import numpy as np
    try:
        with open_image(image_path) as image:
            image.thumbnail((320, 240))
            pixels = np.asarray(image.convert("RGB"), dtype=np.float32)
        gray = pixels @ np.array([.2126, .7152, .0722], dtype=np.float32)
        highlight = float(np.mean(gray >= 247))
        shadow = float(np.mean(gray <= 8))
    except (OSError, ValueError) as exc:
        return {"mode": "single", "frames": 1,
                "reason": f"Existing photograph could not supply bounded clipping evidence ({type(exc).__name__}); no extra exposures."}
    result = plan_stack(highlight_fraction=highlight, shadow_fraction=shadow,
                        motion_fraction=fraction, intent=intent or "", frames=policy["frames"])
    if result["frames"] > policy["frames"] or result["mode"] not in (*STACK_MODES, "single"):
        return {"mode": "single", "frames": 1,
                "reason": "The suggested stack exceeds the accepted mode or frame budget; no extra exposures."}
    result.update(evidence_source="accepted_jpeg_and_verified_motion", highlight_fraction=highlight,
                  shadow_fraction=shadow, motion_fraction=fraction)
    return result
