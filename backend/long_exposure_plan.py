"""Duration-driven independent-frame averaging, not continuous ND/Bulb capture."""
from __future__ import annotations
import math

USB_TRANSFER_ALLOWANCE_S = 8.0


def validate_duration(duration_s, *, mode="long_exposure", interval_s=0.0):
    if duration_s is None:
        return None
    if mode != "long_exposure":
        raise ValueError("duration_s requires long_exposure mode")
    if type(duration_s) not in (int, float) or not math.isfinite(duration_s) or not 1 <= duration_s <= 3600:
        raise ValueError("duration_s must be finite and between 1 and 3600 seconds")
    if interval_s != 0:
        raise ValueError("duration_s chooses cadence; interval_s must be zero")
    return float(duration_s)


def plan_long_exposure(duration_s, shutter_s, *, frame_budget=16, transfer_allowance_s=USB_TRANSFER_ALLOWANCE_S):
    duration_s = validate_duration(duration_s)
    if duration_s is None:
        raise ValueError("duration_s is required")
    if type(shutter_s) not in (int, float) or not math.isfinite(shutter_s) or not 1/8000 <= shutter_s <= 30:
        raise ValueError("confirmed shutter must be between 1/8000 and 30 seconds; Bulb is unsupported")
    if type(frame_budget) is not int or not 2 <= frame_budget <= 16:
        raise ValueError("frame_budget must be an integer between 2 and 16")
    if type(transfer_allowance_s) not in (int, float) or not math.isfinite(transfer_allowance_s) or not 0 <= transfer_allowance_s <= 60:
        raise ValueError("transfer allowance must be finite and between zero and 60 seconds")
    minimum = 2 * shutter_s + transfer_allowance_s
    if duration_s < minimum - 1e-9:
        raise ValueError(f"Duration is too short: at least {minimum:.3f}s is required for two {shutter_s:g}s frames and the {transfer_allowance_s:g}s transfer allowance; no shutter was issued")
    cycle = shutter_s + transfer_allowance_s
    frames = min(frame_budget, int(math.floor((duration_s-shutter_s) / cycle + 1e-9)) + 1)
    interval = (duration_s-shutter_s)/(frames-1)
    return {"algorithm": "duration-window-average-v1", "duration_s": duration_s,
            "frame_budget": frame_budget, "frames": frames, "shutter_s": shutter_s,
            "interval_s": interval, "planned_integrated_shutter_s": frames*shutter_s,
            "usb_transfer_allowance_s": transfer_allowance_s,
            "allowance_source": "conservative_unverified_planning_allowance",
            "gaps_expected": True, "continuous_exposure": False,
            "note": "Independent JPEG frames are averaged. Transport and preparation can overrun the window; this is not continuous ND-filter or Bulb exposure."}



def apply_duration_plan(state, shutter_s):
    """Configure only the pending manifest before the first shutter, never reshoot."""
    if state.get("duration_s") is None:
        return None
    validate_duration(state["duration_s"], mode=state["mode"])
    if any(frame["status"] != "pending" for frame in state["frames"]):
        if all(frame["status"] == "accepted" for frame in state["frames"]) and state.get("long_exposure_plan"):
            return state["long_exposure_plan"]
        raise ValueError("An interrupted duration window cannot resume with new exposures; accepted frames remain preserved")
    budget = (state.get("long_exposure_plan") or {}).get("frame_budget", state["frame_count"])
    plan = plan_long_exposure(state["duration_s"], shutter_s, frame_budget=budget)
    state["long_exposure_plan"] = plan
    state["frame_count"] = plan["frames"]
    state["frames"] = state["frames"][:plan["frames"]]
    state["interval_s"] = plan["interval_s"]
    return plan


def timing_receipt(plan, frames):
    accepted = [frame for frame in frames if frame.get("status") == "accepted"]
    window = completion = None
    integrated = 0.0
    from camera_caps import _shutter_seconds
    for frame in accepted:
        try:
            applied = frame.get("applied") if isinstance(frame.get("applied"), dict) else {}
            shutter = _shutter_seconds(str(applied.get("shutter", "")))
        except (ValueError, TypeError, ZeroDivisionError):
            shutter = None
        if shutter is None or not math.isfinite(shutter) or shutter <= 0:
            integrated = None
            break
        integrated += shutter
    unresolved = any(frame.get("status") in ("capturing", "ambiguous") for frame in frames)
    if unresolved:
        integrated = None
    if accepted:
        intervals = [frame.get("actual_interval_s") for frame in accepted[1:]]
        monotonic = all(frame.get("actual_interval_source") == "monotonic" for frame in accepted[1:])
        if monotonic and all(type(value) in (int, float) and math.isfinite(value) and value >= 0 for value in intervals):
            start_window = sum(intervals)
            if integrated is not None:
                last_shutter = _shutter_seconds(str(accepted[-1]["applied"]["shutter"]))
                window = start_window + last_shutter
            last_pipeline = accepted[-1].get("capture_pipeline_s")
            if type(last_pipeline) in (int, float) and math.isfinite(last_pipeline) and last_pipeline >= 0:
                completion = start_window + last_pipeline
    requested = plan["duration_s"]
    tolerance = max(.5, requested * .05)
    incomplete = len(accepted) < plan["frames"]
    return {"requested_duration_s": requested, "accepted_frames": len(accepted),
            "planned_frames": plan["frames"], "actual_capture_window_s": window,
            "observed_capture_completion_s": completion, "integrated_shutter_s": integrated,
            "overrun_s": max(0., window-requested) if window is not None else None,
            "completion_overrun_s": max(0., completion-requested) if completion is not None else None,
            "underfill_s": max(0., requested-window) if window is not None else None,
            "window_within_tolerance": not incomplete and window is not None and abs(window-requested) <= tolerance,
            "tolerance_s": tolerance, "incomplete": incomplete, "unresolved_capture": unresolved,
            "evidence_source": "host_monotonic_capture_markers_plus_confirmed_shutter_settings",
            "continuous_exposure": False,
            "note": "Window is a host-timing estimate; integrated seconds sum camera shutter readbacks, not measured sensor-open time. Final transfer/validation completion is reported separately."}
