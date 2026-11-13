"""Exact USB sessions, immutable restoration and recoverable artifact publication."""
from __future__ import annotations
import hashlib
import copy
import contextlib
import json
import logging
import math
import os
import re
import shutil
from pathlib import Path
import time
import zipfile

import engine
import heavy_jobs
import runtime_roles
from stack_session import (SessionRunner, merge_session_frames, frame_receipt,
                           binary_receipt, regular_bytes, panorama_receipt_summary,
                           timelapse_cadence, MAX_SESSION_SOURCE_BYTES,
                           SESSION_EXECUTION_BUDGET_S, HOLY_GRAIL_EXECUTION_BUDGET_S,  # noqa: F401 (re-exported)
                           execution_budget_s, execution_budget_status)
from stack_session import MODES as SESSION_MODES  # noqa: F401 (POST /sessions/meter-plan validates modes)

_LOG = logging.getLogger("camera_autopilot.runtime")
RESTORE_KEYS = ("iso", "aperture", "shutter", "focus_mode")
# Incremental panorama previews are best-effort; keep any single rebuild well under
# the supervised engine's hard ceiling so it never stalls the next accepted frame.
PANORAMA_PREVIEW_TIMEOUT_S = 30.0
# Execution time counted against the session budget; tests substitute a fake clock.
_clock = time.monotonic
# Fixed stacks whose whole shutter ladder is known before the first shutter.
FIXED_LADDER_MODES = ("hdr", "long_exposure", "crowd_remove", "night")


def planned_frame_shutter(spec, *, base_seconds, caps):
    """(wanted_s, snapped_s, label) that prepare() requests for this frame (non-night modes)."""
    wanted = base_seconds * 2 ** spec["ev_offset"]
    snapped, label = caps.snap_shutter(wanted)
    return wanted, snapped, label


def night_plan_exposure(night_plan, caps, iso_options, aperture_options):
    """(shutter label, iso, aperture) of a Night Assist plan on this body's live ladders, re-snapped
    so a stale plan cannot request a value the body does not offer now; ValueError otherwise."""
    import camera_caps
    wanted_s = camera_caps._shutter_seconds(str(night_plan["shutter_label"]))
    snapped_s, label = caps.snap_shutter(wanted_s)
    if abs(snapped_s - wanted_s) > 0.05 * wanted_s:
        raise ValueError("Night Assist plan names a shutter this body's ladder does not offer now")
    iso = str(int(float(night_plan["iso"])))
    aperture = str(night_plan["aperture_label"])
    if iso not in iso_options or aperture not in aperture_options:
        raise ValueError("Night Assist plan names an ISO/aperture this body does not offer now")
    return label, iso, aperture


def _frame_ladder_problem(wanted, snapped, label, caps):
    """Why this frame's shutter cannot be captured as planned, or None.

    Interior rounding to the nearest live label is what prepare() has always done and is fine.
    A frame fails when its snapped label is outside the Sony USB transport policy (1/8000 s to the
    1 s cap), or when the wanted value lies past an end of the body's live ladder by more than
    half that end's own step (a continued ladder would have rounded it to another label)."""
    import math
    from sonyremote import CameraError
    from sonyremote.camera import SONY_ACTION_MAX_ENABLED_SHUTTER_S, validate_sony_capture_shutter
    try:
        validate_sony_capture_shutter(label)
    except CameraError:
        return "exceeds_transport_cap" if snapped > SONY_ACTION_MAX_ENABLED_SHUTTER_S else "below_transport_minimum"
    values = sorted({round(value, 12) for value in caps.shutters})  # distinct: "1/8000" == "2/16000"
    # A one-label ladder has no step of its own; use half a third-stop, Sony's finest step.
    fast_half_step = math.log2(values[1] / values[0]) / 2 if len(values) > 1 else 1 / 6
    slow_half_step = math.log2(values[-1] / values[-2]) / 2 if len(values) > 1 else 1 / 6
    if wanted < values[0] and math.log2(values[0] / wanted) > fast_half_step + 1e-9:
        return "beyond_body_fastest"
    if wanted > values[-1] and math.log2(wanted / values[-1]) > slow_half_step + 1e-9:
        return "beyond_body_slowest"
    return None


_REASON_TEXT = {"exceeds_transport_cap": "is longer than the 1 s USB capture limit",
                "below_transport_minimum": "is faster than the 1/8000 s USB capture minimum",
                "beyond_body_fastest": "is faster than this body's fastest shutter",
                "beyond_body_slowest": "is slower than this body's slowest shutter"}


def _ladder_rows(frames, *, base_seconds, caps):
    rows = []
    for spec in frames:
        wanted, snapped, label = planned_frame_shutter(spec, base_seconds=base_seconds, caps=caps)
        rows.append({"index": spec["index"], "ev_offset": spec["ev_offset"], "wanted_s": wanted,
                     "shutter": label, "shutter_s": snapped, "status": spec.get("status"),
                     "problem": _frame_ladder_problem(wanted, snapped, label, caps)})
    return rows


def ladder_remedy(refusal) -> str:
    """What can actually fix this refusal: a new Night Assist plan, a shutter inside the allowed
    range, or (when no baseline on the live ladder fits) fewer frames / a smaller EV step."""
    if refusal.get("night_plan_shutter"):
        return "Make a new Night Assist plan for this body and start a new capture."
    allowed = refusal.get("allowed_baseline_range")
    if not allowed:
        return "Use fewer frames or a smaller EV step and start a new capture."
    return f"Change the shutter to within {allowed['min']}–{allowed['max']} and start a new capture."


class LadderRefused(RuntimeError):
    """A session whose fixed ladder was refused never runs again: its saved baseline decides the
    whole bracket, so every run, resume or job would refuse the same way. Raised before any camera
    connection; the API answers 409."""

    ladder_refused = True

    def __init__(self, refusal):
        super().__init__("This session's fixed bracket cannot be captured from its saved baseline. "
                         + ladder_remedy(refusal))


# --- Session meter-first: POST /sessions/meter-plan, then POST /sessions `start_settings` ----------

_EXPOSURE_KEYS = ("iso", "aperture", "shutter")


def bracket_offsets(mode, frames, ev_step) -> list[float]:
    """The EV offsets SessionRunner.create assigns (HDR only; every other mode shoots at 0)."""
    if mode != "hdr" or not frames:
        return []
    return [(index - (frames - 1) / 2) * ev_step for index in range(frames)]


def meter_plan_exposure_budget(frames) -> dict:
    """The photos a metered session may take: 1 meter frame, the bounded re-meter, then its frames."""
    import remeter
    session = frames if type(frames) is int else None
    return {"meter_exposures": 1, "extra_meter_exposures": remeter.MAX_REMETER_PASSES, "session_frames": session,
            "max_physical_exposures": None if session is None else 1 + remeter.MAX_REMETER_PASSES + session}


def meter_plan_response(meter: dict, *, camera, frames=None, mode=None, ev_step=None) -> dict:
    """What POST /sessions/meter-plan returns from `RealEngine.meter_baseline` (which already put the
    inherited exposure back). A meter still black or white after the bounded re-meter recommends nothing."""
    model, port, serial = camera
    reliability = meter.get("meter_reliability") or {}
    metered = {key: str(meter["metered"][key]) for key in _EXPOSURE_KEYS}
    plan = {"camera_model": model, "camera_port": port, "camera_serial": serial,
            "verified_serial": meter.get("camera_serial"), "mode": mode, "frames": frames, "ev_step": ev_step,
            "inherited": meter.get("inherited"), "metered": metered, "meter_passes": meter.get("meter_passes"),
            "meter_reliability": reliability, "meter_restore": meter.get("meter_restore"),
            "recommendation_source": meter.get("recommendation_source"),
            "bracket_headroom": meter.get("bracket_headroom"), "usable_iso_range": meter.get("usable_iso_range"),
            "meter_physical_exposures": meter.get("physical_exposure_count"),
            "recommended_start": dict(metered), "exposure_budget": meter_plan_exposure_budget(frames),
            "planned_at": time.time(), "session_created": False}
    if reliability.get("status") == "low":
        passes = len(meter.get("meter_passes") or []) - 1
        plan.update(error="meter_unreliable", recommended_start=None, retry_capture=False, may_have_fired=False,
                    detail=(f"The meter frame stayed unreliable ({reliability.get('reason')}) after {passes} "
                            "re-meter pass(es), so no starting exposure is recommended. Change the light or the "
                            "framing and meter again, or keep the camera's current exposure."))
    elif (meter.get("meter_restore") or {}).get("status") == "failed":
        # The body may still hold the meter's exposure: a session would read it as the exposure to restore.
        plan.update(error="meter_restore_failed", recommended_start=None, retry_capture=False, may_have_fired=False,
                    detail=("The exposure changed for metering could not be put back "
                            f"({meter['meter_restore'].get('reason')}); check the camera's ISO and shutter, then "
                            "meter again. No starting exposure is recommended."))
    return json.loads(json.dumps(plan, default=str))


def _f_number(value) -> float:
    return float(str(value).strip().lower().removeprefix("f/"))


def validate_start_settings(start, *, mode, frames, ev_step, timelapse_mode, timelapse_options, meter_plan, camera,
                            duration_s=None):
    """Check a session's `start_settings` before the session exists (no camera is opened): the Sony USB
    shutter range (1 s cap), every bracket frame inside it, the timelapse bounds, the rig's usable ISO
    range from the meter plan, and the plan's own camera. Returns (settings, manifest baseline record);
    ValueError otherwise. The live ladder precheck (#86) still runs before frame 0."""
    import camera_caps
    from sonyremote import CameraError
    from sonyremote.camera import SONY_ACTION_MAX_ENABLED_SHUTTER_S, validate_sony_capture_shutter
    if not isinstance(start, dict) or set(start) != set(_EXPOSURE_KEYS):
        raise ValueError("start_settings must give exactly iso, aperture and shutter")
    settings = {key: str(start[key]).strip() for key in _EXPOSURE_KEYS}
    if not settings["iso"].isdigit() or not 50 <= int(settings["iso"]) <= 409600:
        raise ValueError("start_settings iso must be a fixed numeric ISO")
    try:
        f_number = _f_number(settings["aperture"])
        seconds = camera_caps._shutter_seconds(settings["shutter"])
    except (TypeError, ValueError, ZeroDivisionError) as exc:
        raise ValueError("start_settings aperture and shutter must be camera values") from exc
    if not (math.isfinite(f_number) and f_number > 0 and math.isfinite(seconds) and seconds > 0):
        raise ValueError("start_settings aperture and shutter must be positive")
    try:
        validate_sony_capture_shutter(settings["shutter"])
    except CameraError as exc:
        raise ValueError(f"start_settings shutter {settings['shutter']}: {exc}") from exc
    # Every bracket frame must stay inside the USB transport range (a frame past either end by more
    # than half a third-stop would be clamped onto another frame's label).
    tolerance = 2 ** (1 / 6)
    for offset in bracket_offsets(mode, frames, ev_step):
        wanted = seconds * 2 ** offset
        if wanted > SONY_ACTION_MAX_ENABLED_SHUTTER_S * tolerance or wanted < (1 / 8000) / tolerance:
            raise ValueError(f"start_settings shutter {settings['shutter']} puts the {offset:+g} EV bracket frame at "
                             f"{wanted:.6g} s, outside the 1/8000 s to {SONY_ACTION_MAX_ENABLED_SHUTTER_S:g} s USB range")
    if duration_s is not None:
        # A duration blend must fit two frames of this shutter (plus the USB transfer allowance).
        from long_exposure_plan import plan_long_exposure
        plan_long_exposure(duration_s, seconds, frame_budget=frames)
    if mode == "timelapse":
        # Every timelapse policy, Fixed included, keeps its frames inside the configured bounds.
        from stack_planner import timelapse_config
        options = {key: value for key, value in (timelapse_options or {}).items() if key != "deflicker"}
        config = timelapse_config(timelapse_mode, options)
        if not config["min_shutter_s"] * (1 - 1e-6) <= seconds <= config["max_shutter_s"] * (1 + 1e-6):
            raise ValueError(f"start_settings shutter {settings['shutter']} is outside this timelapse's shutter bounds")
        if not config["min_iso"] <= int(settings["iso"]) <= config["max_iso"]:
            raise ValueError(f"start_settings ISO {settings['iso']} is outside this timelapse's ISO bounds")
        if not config["aperture_locked"] and not config["min_aperture"] <= f_number <= config["max_aperture"]:
            raise ValueError(f"start_settings aperture {settings['aperture']} is outside this timelapse's aperture bounds")
    record = {"source": "start_settings", "inherited": None, "start_settings": dict(settings)}
    if meter_plan is not None:
        if not isinstance(meter_plan, dict):
            raise ValueError("meter_plan must be the object POST /sessions/meter-plan returned")
        if (meter_plan.get("camera_model"), meter_plan.get("camera_port"), meter_plan.get("camera_serial")) != tuple(camera):
            raise ValueError("This meter plan was made for another camera; meter again with the selected camera")
        recommended = meter_plan.get("recommended_start")
        if not isinstance(recommended, dict) or {key: str(recommended.get(key)) for key in _EXPOSURE_KEYS} != settings:
            raise ValueError("start_settings must be the meter plan's recommended_start")
        usable = meter_plan.get("usable_iso_range")
        if usable is not None:
            if not isinstance(usable, list) or len(usable) != 2 or any(type(value) not in (int, float) for value in usable):
                raise ValueError("meter_plan usable_iso_range must be a list of two numbers")
            try:
                low, high = float(usable[0]), float(usable[1])
            except OverflowError as exc:
                raise ValueError("meter_plan usable_iso_range must be two finite numbers") from exc
            if not (math.isfinite(low) and math.isfinite(high) and low <= high):
                raise ValueError("meter_plan usable_iso_range must be two ordered finite numbers")
            if not low <= int(settings["iso"]) <= high:
                raise ValueError(f"start_settings ISO {settings['iso']} is outside this rig's usable ISO range "
                                 f"{low:g}-{high:g}")
        record.update(source="meter_plan", inherited=meter_plan.get("inherited"), meter_plan={
            key: meter_plan.get(key) for key in ("metered", "meter_passes", "meter_reliability", "meter_restore",
                                                  "recommendation_source", "bracket_headroom", "usable_iso_range",
                                                  "meter_physical_exposures", "planned_at")})
    return settings, json.loads(json.dumps(record, default=str))


def live_start_settings(settings, iso_options, aperture_options, shutter_options):
    """The start settings as this body's own live labels (e.g. "8" -> "f/8"), or (None, problem) when
    one is not on its live ladder: a value the camera would round to another is refused, never sent."""
    import camera_caps

    def match(options, wanted, value_of):
        for option in options or ():
            try:
                if math.isclose(value_of(option), wanted, rel_tol=1e-6):
                    return str(option)
            except (TypeError, ValueError, ZeroDivisionError):
                continue
        return None
    try:
        wanted = {"iso": float(settings["iso"]), "aperture": _f_number(settings["aperture"]),
                  "shutter": camera_caps._shutter_seconds(str(settings["shutter"]))}
    except (KeyError, TypeError, ValueError, ZeroDivisionError):
        return None, "the start settings are not camera values"
    live = {"iso": match(iso_options, wanted["iso"], lambda option: float(str(option))),
            "aperture": match(aperture_options, wanted["aperture"], _f_number),
            "shutter": match(shutter_options, wanted["shutter"], lambda option: camera_caps._shutter_seconds(str(option)))}
    missing = [key for key, label in live.items() if label is None]
    if missing:
        return None, ("this body's live ladder does not offer the start " + " or ".join(missing)
                      + " (" + " · ".join(str(settings[key]) for key in missing) + ")")
    return live, None


def check_timelapse_controls(create_kwargs: dict, controls: dict) -> dict:
    """Apply a timelapse's controls (exposure bounds, aperture, priority, Smartlapse, preview) to a
    scratch copy of the session it is for, exactly as POST /sessions/{id}/controls would, and return
    the resulting exposure configuration. Nothing is saved in the shot store. ValueError if refused."""
    import tempfile
    from timelapse_controls import read_controls, update_controls
    with tempfile.TemporaryDirectory(prefix="meter-plan-controls-") as root:
        scratch = SessionRunner(Path(root) / "sessions")
        state = scratch.create(**create_kwargs)
        update_controls(scratch, state["id"], controls)
        return read_controls(scratch, state["id"])["exposure"]


def _start_settings(state) -> dict | None:
    """The validated frame-0 exposure a session was created with, or None (main's behaviour)."""
    record = (state.get("metadata") or {}).get("baseline")
    return record.get("start_settings") if isinstance(record, dict) else None


def _same_exposure(chosen, start) -> bool:
    """The control law's frame-0 choice is exactly the start exposure (shutter, ISO, aperture)."""
    import camera_caps
    try:
        return (math.isclose(camera_caps._shutter_seconds(str(chosen["shutter"])),
                             camera_caps._shutter_seconds(str(start["shutter"])), rel_tol=1e-6)
                and math.isclose(float(chosen["iso"]), float(start["iso"]))
                and math.isclose(_f_number(chosen["aperture"]), _f_number(start["aperture"])))
    except (KeyError, TypeError, ValueError, ZeroDivisionError):
        return False


def require_runnable_ladder(state) -> None:
    """Shared by run(), SessionJobs.submit and /resume: refuse a ladder-refused session."""
    if state.get("ladder_refusal"):
        raise LadderRefused(state["ladder_refusal"])


def fixed_ladder_refusal(state, *, base_shutter, caps, iso_options=(), aperture_options=(),
                         recovery_restored=False):
    """Check every pending frame of a fixed hdr/long_exposure/crowd_remove/night stack before the
    first shutter of this attempt, with the same shutter computation prepare() uses. Returns None,
    or a structured refusal naming the baseline shutter and the baselines on this body's live
    ladder that would run the whole N-frame bracket."""
    import camera_caps
    from sonyremote.camera import SONY_ACTION_MAX_ENABLED_SHUTTER_S
    if state["mode"] not in FIXED_LADDER_MODES:
        return None
    pending = [frame for frame in state["frames"] if frame["status"] == "pending"]
    if not pending:
        return None  # nothing left to expose (e.g. a merge-only retry)
    accepted = sum(frame["status"] == "accepted" for frame in state["frames"])
    scope = (" This attempt fired no shutter" + (f"; {accepted} earlier accepted frame(s) are kept" if accepted else "")
             + ("; the saved original settings were restored first." if recovery_restored else "."))
    common = {"mode": state["mode"], "frame_count": len(state["frames"]), "baseline_shutter": str(base_shutter),
              "max_enabled_shutter_s": SONY_ACTION_MAX_ENABLED_SHUTTER_S, "accepted_frames": accepted,
              "restored_before_refusal": recovery_restored, "may_have_fired": False, "retry_capture": False}
    night_plan = (state.get("metadata") or {}).get("night_assist") if state["mode"] == "night" else None
    if night_plan:
        # A Night Assist plan owns its exposure; the baseline cannot fix it, a new plan can.
        try:
            label = night_plan_exposure(night_plan, caps, iso_options, aperture_options)[0]
            snapped = camera_caps._shutter_seconds(label)
            problem = _frame_ladder_problem(snapped, snapped, label, caps)
            why = _REASON_TEXT.get(problem)
        except ValueError as exc:
            problem, why = "night_plan_not_available", str(exc)
        if not problem:
            return None
        return {"error": "fixed_ladder_out_of_range", "reason": problem, **common,
                "night_plan_shutter": night_plan["shutter_label"], "allowed_baseline_range": None,
                "remedy": ladder_remedy({"night_plan_shutter": night_plan["shutter_label"]}),
                "detail": (f"The Night Assist plan's shutter {night_plan['shutter_label']} cannot be captured: "
                           f"{why}. Make a new Night Assist plan for this body." + scope)}
    base_seconds = camera_caps._shutter_seconds(str(base_shutter))
    rows = _ladder_rows(state["frames"], base_seconds=base_seconds, caps=caps)
    failing = [row for row in rows if row["problem"] and row["status"] == "pending"]
    if not failing:
        return None
    # The range that runs the whole N-frame bracket from this body's live ladder, found by running
    # the same check for every label (not a formula, so it can never disagree with the check).
    allowed = [label for value, label in sorted(zip(caps.shutters, caps.shutter_labels))
               if not any(row["problem"] for row in _ladder_rows(state["frames"], base_seconds=value, caps=caps))]
    first, count = failing[0], len(rows)
    span = f"{rows[0]['shutter']} to {rows[-1]['shutter']}" if count > 1 else rows[0]["shutter"]
    transport = first["problem"] in ("exceeds_transport_cap", "below_transport_minimum")
    subject = (f"its nearest shutter {first['shutter']}" if transport else
               f"{first['wanted_s']:.6g} s")
    detail = (f"Fixed {state['mode']} with {count} frames brackets {span} around the baseline shutter "
              f"{base_shutter}; frame {first['index']} wants {first['wanted_s']:.6g} s (nearest on this body: "
              f"{first['shutter']}), and {subject} {_REASON_TEXT[first['problem']]}. "
              + (f"With {count} frames the baseline shutter must be between {allowed[0]} and {allowed[-1]} "
                 "on this body; change the shutter and start a new capture." if allowed else
                 f"No baseline shutter on this body's ladder fits {count} frames at this spacing; "
                 "use fewer frames or a smaller EV step.")
              + scope)
    allowed_range = {"min": allowed[0], "max": allowed[-1], "labels": allowed} if allowed else None
    return {"error": "fixed_ladder_out_of_range", "reason": first["problem"], **common, "detail": detail,
            "night_plan_shutter": None, "allowed_baseline_range": allowed_range,
            "remedy": ladder_remedy({"allowed_baseline_range": allowed_range}), "frames": rows}


def _recorded_execution_s(state):
    """Execution time earlier runs of this session already spent (0 for a new session).

    Sessions recorded before the explicit budget carry it only in SmartLapse status; the
    larger of the two counts. An unreadable value refuses the run rather than resetting it.
    """
    used = 0.0
    for key, field in (("execution_budget", "used_s"), ("smartlapse_status", "elapsed_s")):
        if key not in state:
            continue
        record = state[key]
        value = record.get(field) if isinstance(record, dict) else None
        if type(value) not in (int, float) or not math.isfinite(value) or value < 0:
            raise ValueError("Recorded session execution time is unreadable; refusing to reset the budget")
        used = max(used, float(value))
    return used


def _record_execution(sessions, session_id, elapsed):
    """Persist the time this run has spent and return the session as now stored."""
    from timelapse_controls import read_controls
    current = sessions.read(session_id)
    exposure = read_controls(sessions, session_id)["exposure"] if current["mode"] == "timelapse" else None
    current["execution_budget"] = execution_budget_status(current["mode"], exposure, elapsed())
    sessions.save(current)
    return sessions.read(session_id)


@contextlib.contextmanager
def _execution_recorded(sessions, session_id, elapsed):
    """On every exit (return, failure, refusal) persist the time this run spent.

    Recording never replaces the run's own outcome; if it fails, the last in-run
    checkpoint (before each shutter, after each accepted frame, every 30 s of a wait) stands.
    """
    try:
        yield
    finally:
        with contextlib.suppress(Exception):
            _record_execution(sessions, session_id, elapsed)


def runner():
    return SessionRunner(engine.SHOTS_DIR / "sessions")


def _panorama_options(state):
    """Validate the session's panorama projection options up front (before capture).

    Mirrors /panoramas bounds so a bad option fails fast instead of after shooting.
    """
    from panorama import PROJECTIONS, _layout, _normalize_crop, _number
    raw = (state.get("metadata") or {}).get("panorama") or {}
    allowed = {"projection", "horizontal_fov_deg", "rotation_deg", "autocrop",
               "level_horizon", "crop", "plan", "layout"}
    if not isinstance(raw, dict) or set(raw) - allowed:
        raise ValueError("Unknown or invalid panorama option")
    options = {"projection": raw.get("projection", "cylindrical")}
    if options["projection"] not in PROJECTIONS:
        raise ValueError("Unknown panorama projection.")
    if "horizontal_fov_deg" in raw:
        options["horizontal_fov_deg"] = _number(raw["horizontal_fov_deg"], 20, 140, "horizontal_fov_deg")
    if "rotation_deg" in raw:
        options["rotation_deg"] = _number(raw["rotation_deg"], -45, 45, "rotation_deg")
    if "autocrop" in raw:
        if type(raw["autocrop"]) is not bool:
            raise ValueError("autocrop must be a boolean.")
        options["autocrop"] = raw["autocrop"]
    if "level_horizon" in raw:
        if type(raw["level_horizon"]) is not bool:
            raise ValueError("level_horizon must be a boolean.")
        options["level_horizon"] = raw["level_horizon"]
    if "crop" in raw:
        options["crop"] = _normalize_crop(raw["crop"])
    layout = raw.get("layout")
    plan = raw.get("plan")
    if plan is not None:
        if not isinstance(plan, dict) or not isinstance(plan.get("frames"), list):
            raise ValueError("panorama plan must include a frames list")
        n_plan = len(plan["frames"])
        if n_plan != state["frame_count"]:
            raise ValueError("panorama plan frame count must match the session")
        if layout is None:
            rows = plan.get("rows")
            columns = plan.get("columns")
            if type(rows) is int and type(columns) is int:
                layout = {"rows": rows, "columns": columns}
            else:
                layout = {"rows": 1 + max(frame["row"] for frame in plan["frames"]),
                          "columns": 1 + max(frame["column"] for frame in plan["frames"])}
    if layout is not None:
        options["layout"] = _layout(layout, n_sources=state["frame_count"])
    return options


def _panorama_preview_every(state):
    """Cadence knob: rebuild the mosaic every N accepted frames (0 disables previews)."""
    value = (state.get("metadata") or {}).get("preview_every", 1)
    if isinstance(value, bool) or type(value) is not int or not 0 <= value <= 12:
        raise ValueError("panorama preview_every must be an integer between 0 and 12")
    return value


def _remove_preview_dir(path):
    """Bounded removal of one nested panorama preview tree; never follow a symlink."""
    import os as _os
    path = Path(path)
    if path.is_symlink() or not path.is_dir():
        return
    count = 0
    for root, dirnames, filenames in _os.walk(path, topdown=False, followlinks=False):
        root_path = Path(root)
        for name in filenames:
            count += 1
            if count > 512:
                raise ValueError("panorama preview exceeds removal budget")
            (root_path / name).unlink(missing_ok=True)
        for name in dirnames:
            entry = root_path / name
            entry.unlink(missing_ok=True) if entry.is_symlink() else entry.rmdir()
    path.rmdir()


def _preview_layout(options, count):
    """A partial grid cannot carry the session's full rows×columns layout.

    Serpentine order fills whole rows first, so a prefix of completed rows keeps the
    grid (rows = count // columns); a prefix ending mid-row is stitched without a layout.
    """
    layout = options.get("layout")
    if not layout or layout["rows"] * layout["columns"] == count:
        return options, "session_layout" if layout else "no_layout"
    options = {key: value for key, value in options.items() if key != "layout"}
    if count % layout["columns"] == 0:
        options["layout"] = {"rows": count // layout["columns"], "columns": layout["columns"]}
        return options, "completed_rows"
    return options, "partial_row_without_layout"


def _incremental_panorama(sessions, state, options, *, timeout):
    """Rebuild the mosaic from the verified accepted prefix into pano_NNNN/ under the session."""
    from panorama import build_panorama, MAX_FRAMES
    accepted = [frame for frame in state["frames"] if frame["status"] == "accepted"]
    count = len(accepted)
    if count < 2 or count > MAX_FRAMES or any(frame["index"] != i for i, frame in enumerate(accepted)):
        raise ValueError(f"panorama preview requires an accepted prefix of 2..{MAX_FRAMES} frames")
    directory = sessions._directory(state["id"])
    # artifact() re-verifies each accepted frame's sha before it feeds the stitcher.
    sources = [str(sessions.artifact(state["id"], f"frame_{frame['index']:04d}.jpg")) for frame in accepted]
    preview_root = directory / f"pano_{count:04d}"
    options, layout_policy = _preview_layout(options, count)
    receipt = build_panorama(sources, preview_root, timeout_seconds=timeout, **options)
    produced = preview_root / receipt["job_id"] / receipt["output"]["filename"]
    import hashlib
    with open(produced, "rb") as handle:
        digest = hashlib.file_digest(handle, "sha256").hexdigest()
    return {"status": "completed", "dir": f"pano_{count:04d}", "frame_count": count,
            "layout": options.get("layout"), "layout_policy": layout_policy,
            "job_id": receipt["job_id"], "path": str(produced.resolve()),
            "bytes": produced.stat().st_size, "sha256": digest,
            "url": f"/sessions/{state['id']}/artifacts/pano_{count:04d}/{receipt['job_id']}/panorama.jpg",
            "receipt": panorama_receipt_summary(receipt)}


def _panorama_boundary(sessions, session_id, live_state, options, *, preview_every, timeout):
    """After an accepted frame, refresh the incremental mosaic preview; failures never block."""
    directory = sessions._directory(session_id)
    count = sum(frame["status"] == "accepted" for frame in live_state["frames"])
    # The final complete mosaic is published as result.jpg, so previews stop below it.
    if not preview_every or count < 2 or count >= live_state["frame_count"] or count % preview_every != 0:
        return
    if (directory / "cancel").exists():
        return
    previous = live_state.get("preview_panorama") or {}
    preview_dir = directory / f"pano_{count:04d}"
    try:
        preview = _incremental_panorama(sessions, live_state, options, timeout=timeout)
        live_state["preview_panorama"] = preview
        live_state.pop("preview_panorama_error", None)
        sessions.save(live_state)  # Publish verified identity before removing the prior snapshot.
        prior = previous.get("dir")
        if prior and prior != preview["dir"]:
            _remove_preview_dir(directory / prior)
    except Exception as exc:
        _remove_preview_dir(preview_dir)
        live_state["preview_panorama_error"] = {"message": str(exc), "frame_count": count,
            "reason": getattr(exc, "reason", None), "accepted_frames_preserved": True,
            "layout_policy": _preview_layout(options, count)[1]}



# Automatic stop-when-clean waits for the first frame count whose 25% floor needs two agreeing
# frames (crowd_auto_mask.min_frames_for_visibility(5) == 2). Below it one frame that happens to
# equal the per-pixel median counts as "cleared", which is not evidence of background. A manual
# Stop may still merge from 2 frames: the person sees the preview and decides.
CROWD_AUTO_STOP_MIN_FRAMES = 5


def _crowd_options(state):
    """Validate the crowd preview cadence and optional auto-stop threshold before any shutter."""
    metadata = state.get("metadata") or {}
    every = metadata.get("preview_every", 1)
    if isinstance(every, bool) or type(every) is not int or not 0 <= every <= 12:
        raise ValueError("crowd preview_every must be an integer between 0 and 12")
    threshold = metadata.get("stop_when_cleared")
    if threshold is not None and (isinstance(threshold, bool) or type(threshold) not in (int, float)
                                  or not 0.5 <= threshold <= 1.0):
        raise ValueError("stop_when_cleared must be a share between 0.5 and 1.0")
    budget = state.get("requested_frame_count", state["frame_count"])
    if threshold is not None and budget <= CROWD_AUTO_STOP_MIN_FRAMES:
        raise ValueError(f"stop_when_cleared needs a budget of at least {CROWD_AUTO_STOP_MIN_FRAMES + 1} "
                         f"frames: it can stop no earlier than frame {CROWD_AUTO_STOP_MIN_FRAMES}")
    from stack_session import CROWD_AUTO_MAX_FRAMES
    if threshold is not None and budget > CROWD_AUTO_MAX_FRAMES:
        # Previews, and so the threshold check, end at the automatic-labeling limit, and a larger
        # merge is a plain median with no visibility report: refuse rather than silently shoot on.
        raise ValueError(f"stop_when_cleared needs a budget of at most {CROWD_AUTO_MAX_FRAMES} frames: the "
                         "cleared share is only measured up to that many; use Stop when clean for more")
    if threshold is not None and runtime_roles.heavy_jobs_deferred():
        # The split runtime keeps the estimator out of the camera-control process, so no live share
        # exists to stop on; refuse before any shutter rather than shoot the whole budget.
        raise ValueError("stop_when_cleared is unavailable in the split runtime: the live crowd preview "
                         "is not computed in the camera-control process; use Stop when clean")
    return {"preview_every": every, "stop_when_cleared": None if threshold is None else float(threshold)}


def _incremental_crowd(sessions, state):
    """Clean-plate preview of the verified accepted prefix, from cached downscaled copies."""
    from stack_session import crowd_preview, crowd_preview_source
    accepted = [frame for frame in state["frames"] if frame["status"] == "accepted"]
    if any(frame["index"] != index for index, frame in enumerate(accepted)):
        raise ValueError("crowd preview requires an accepted prefix")
    directory = sessions._directory(state["id"])
    folder = directory / "crowd_preview"
    if folder.is_symlink():
        raise ValueError("crowd preview directory cannot be a symlink")
    folder.mkdir(mode=0o700, exist_ok=True)
    sources = []
    for frame in accepted:
        # Keyed by the accepted receipt, so a cached copy always belongs to that exact frame.
        small = folder / f"src_{frame['index']:04d}_{frame['receipt']['sha256'][:16]}.png"
        if small.is_symlink():
            raise ValueError("crowd preview source cannot be a symlink")
        if not small.is_file():
            # artifact() re-verifies the accepted frame's sha before it is read.
            crowd_preview_source(sessions.artifact(state["id"], f"frame_{frame['index']:04d}.jpg"), small)
        sources.append(str(small))
    name, mask_name = f"preview_{len(accepted):04d}.jpg", f"not_cleared_{len(accepted):04d}.png"
    produced = folder / name
    report = crowd_preview(sources, produced, folder / mask_name)
    with open(produced, "rb") as handle:
        digest = hashlib.file_digest(handle, "sha256").hexdigest()
    return {"status": "completed", **report, "frame_count": len(accepted), "name": f"crowd_preview/{name}",
            "mask": f"crowd_preview/{mask_name}",
            "path": str(produced.resolve()), "bytes": produced.stat().st_size, "sha256": digest,
            "url": f"/sessions/{state['id']}/crowd-preview"}


def _regions_seen_clear(regions):
    """True only for a complete, self-consistent report that no once-occupied region is pending:
    nothing pending or ambiguous, judged over at least the two newest frames
    (crowd_clear_regions.RECENT_CLEAR_FRAMES; not imported, the control role loads no estimator)."""
    if not isinstance(regions, dict) or regions.get("all_seen_clear") is not True:
        return False
    pending, ambiguous, recent = (regions.get(key) for key in
                                  ("pending_regions", "ambiguous_regions", "recent_clear_frames"))
    share = regions.get("pending_share")
    return (type(pending) is int and pending == 0 and type(ambiguous) is int and ambiguous == 0
            and type(recent) is int and recent >= 2 and type(share) in (int, float) and share == 0)


def _crowd_boundary(sessions, session_id, live_state, options):
    """After an accepted frame: refresh the clean-plate preview and, when the request set a
    threshold, ask the runner to stop cleanly once the cleared share reaches it and every
    region that was ever occupied has been seen clear again. A failed
    preview never blocks capture and never stops the session."""
    from stack_session import CROWD_AUTO_MAX_FRAMES
    directory = sessions._directory(session_id)
    count = sum(frame["status"] == "accepted" for frame in live_state["frames"])
    # The final merge reports the last frame; beyond the automatic-labeling budget the merge is a
    # plain median with no visibility report, so the preview (and auto-stop) stop there too.
    if count < 2 or count >= live_state["frame_count"] or count > CROWD_AUTO_MAX_FRAMES:
        return
    if runtime_roles.heavy_jobs_deferred():
        return  # Split runtime: the estimator never runs in the camera-control process.
    if (directory / "cancel").exists() or (directory / "stop").exists() or live_state.get("stop_request"):
        return
    threshold, every = options["stop_when_cleared"], options["preview_every"]
    if threshold is None and (not every or count % every):
        return
    previous = live_state.get("preview_crowd") or {}
    try:
        preview = _incremental_crowd(sessions, live_state)
        live_state["preview_crowd"] = preview
        live_state.pop("preview_crowd_error", None)
        # A qualifying preview and its stop decision are committed in the same write, so a process
        # that dies after it recovers into the stop, not the next shutter. A manual Stop pressed
        # while this preview was computing wins: the person stopped it.
        # The global share cannot tell a clean plate from one that still holds someone who sat
        # down, so the stop also needs every region that was ever occupied to have been seen
        # clear again in the newest frames (crowd_clear_regions). Missing evidence never stops.
        regions = preview.get("regions")
        if (threshold is not None and count >= CROWD_AUTO_STOP_MIN_FRAMES
                and preview["cleared_share"] >= threshold and _regions_seen_clear(regions)
                and not (directory / "stop").exists()):
            live_state["stop_request"] = {"reason": "cleared_share_threshold", "threshold": threshold,
                                          "cleared_share": preview["cleared_share"], "accepted_frames": count,
                                          "all_seen_clear": True,
                                          "ever_occupied_share": regions.get("ever_occupied_share"),
                                          "requested_at": time.time()}
        sessions.save(live_state)  # Publish verified identity before removing the prior snapshot.
        for key, pattern in (("name", r"crowd_preview/preview_[0-9]{4}\.jpg"),
                             ("mask", r"crowd_preview/not_cleared_[0-9]{4}\.png")):
            prior = previous.get(key)
            if prior and prior != preview[key] and re.fullmatch(pattern, prior):
                (directory / prior).unlink(missing_ok=True)
    except Exception as exc:
        live_state["preview_crowd_error"] = {"message": str(exc), "frame_count": count,
                                             "accepted_frames_preserved": True}
        return


@contextlib.contextmanager
def _transaction(sessions, session_id):
    """Global order is gallery lifecycle, then session operation, then runner lock.

    The lifecycle lock also covers the hardware phase: cancellation and manifest
    reads remain independent, while gallery deletion/retention cannot unlink an
    enrolled session between capture and publication. Never reverse this order.
    """
    from photo_lifecycle import _LOCK
    with _LOCK:
        sessions.read(session_id)  # A gallery deletion may have completed while waiting.
        with sessions.operation(session_id):
            yield

@contextlib.contextmanager
def _identity_refusal_recorded(sessions, session_id):
    """A refused camera identity leaves every frame as it was and marks the session with a
    distinct, resumable status carrying the structured refusal (never needs_reconciliation)."""
    try:
        yield
    except Exception as exc:
        if getattr(exc, "identity_refused", False) is True:
            current = sessions.read(session_id)
            current.update(status="identity_refused", identity_refusal=exc.as_dict(), error=str(exc))
            sessions.save(current)
        raise


def _ensure_connected(cam, *, attempts=(0.0, 1.5, 3.0, 5.0)) -> None:
    """A capture reconnect can fail once after a long exposure; restoration must not run on a
    dropped handle. Bounded retries, then the original error surfaces."""
    from sonyremote import CameraError, CameraIdentityError
    last = None
    for delay in attempts:
        try:
            cam.get_settings()
            return
        except CameraError as exc:
            last = exc
            if "Not connected" not in str(exc):
                raise
        if delay:
            time.sleep(delay)
        try:
            cam.connect()
            return
        except CameraIdentityError:
            raise  # terminal: never restore settings onto a body whose serial was refused
        except Exception as exc:  # noqa: BLE001 — retried within the bounded window
            last = exc
    raise CameraError(f"Camera could not be reconnected for restoration: {last}")


def _refuse_placeholder_snapshot(state, snapshot, name):
    """Refuse a persisted snapshot holding Sony placeholders (f/0, ISO 0, 65535/65535) before it
    is restored or planned from. A lone f/0 recorded as a manual lens at baseline time is kept."""
    from sonyremote import CameraError
    from sonyremote.camera import placeholder_fields
    fields = placeholder_fields(snapshot or {})
    if fields == ["aperture"] and (state.get("restore_baseline_read") or {}).get("aperture_unreported"):
        return
    if fields:
        raise CameraError(f"Session {name} holds placeholder camera values ({', '.join(fields)}); "
                          "nothing was written. Set the camera's exposure by hand and start a new session.")


def _restore(cam, baseline):
    from sonyremote import CameraError
    _ensure_connected(cam)
    cam.set_exposure(aperture=baseline["aperture"], shutter=baseline["shutter"], iso=baseline["iso"])
    cam.set_setting("focus_mode", baseline["focus_mode"])
    from sonyremote.camera import read_settings_checked
    actual, _ = read_settings_checked(cam, purpose="session restore readback")
    for key in RESTORE_KEYS:
        if str(actual.get(key)) != str(baseline[key]):
            raise CameraError(f"Could not verify restored {key}")


def _sequence(sessions, state, *, verified=False):
    """Build deterministic, bounded downloadable JPEG sequence and portable receipts.

    ``verified``: the caller already re-hashed the existing ZIP (outside the lifecycle lock)."""
    directory = sessions._directory(state["id"])
    destination = directory / "sequence.zip"
    if state.get("sequence"):
        if not verified:
            sessions.artifact(state["id"], "sequence.zip")
        return state
    temporary = directory / "sequence.zip.pending"
    temporary.unlink(missing_ok=True)
    portable = []
    total = 0
    from image_safety import MAX_IMAGE_SOURCE_BYTES
    with zipfile.ZipFile(temporary, "x", compression=zipfile.ZIP_STORED) as archive:
        for frame in state["frames"]:
            name = f"frame_{frame['index']:04d}.jpg"
            path = sessions.artifact(state["id"], name)
            data = regular_bytes(path, MAX_IMAGE_SOURCE_BYTES)
            if hashlib.sha256(data).hexdigest() != frame["receipt"]["sha256"]:
                raise ValueError("accepted sequence frame changed during publication")
            total += len(data)
            if total > MAX_SESSION_SOURCE_BYTES:
                raise ValueError("sequence archive exceeds source-byte budget")
            archive.writestr(zipfile.ZipInfo(name, (1980, 1, 1, 0, 0, 0)), data)
            portable.append({"file": name, "index": frame["index"], "sha256": frame["receipt"]["sha256"],
                             "width": frame["receipt"]["width"], "height": frame["receipt"]["height"],
                             "capture_started_at": frame.get("capture_started_at"),
                             "actual_interval_s": frame.get("actual_interval_s"),
                             "applied": sessions.frame_evidence(state, frame["index"]).get("applied", {})})
        archive.writestr(zipfile.ZipInfo("sequence.json", (1980, 1, 1, 0, 0, 0)),
                         json.dumps({"mode": "timelapse", "interval_s": state["interval_s"],
                                     "requested_interval_s": state["interval_s"],
                                     "timelapse": state.get("timelapse", {"mode": "fixed"}),
                                     "frames": portable}, indent=2))
    os.chmod(temporary, 0o600)
    with temporary.open("rb") as stream:
        os.fsync(stream.fileno())
    os.replace(temporary, destination)
    state["sequence"] = {"receipt": binary_receipt(destination), "format": "zip/jpeg_sequence",
                         "url": f"/sessions/{state['id']}/artifacts/sequence.zip"}
    sessions.save(state)
    return state


def _publish(sessions, state, *, sequence_verified=False):
    from photo_lifecycle import _LOCK
    with _LOCK:
        return _publish_locked(sessions, state, sequence_verified=sequence_verified)


def _publish_locked(sessions, state, *, sequence_verified=False):
    """Complete/repair publication without opening the camera or creating duplicates."""
    from photo_lifecycle import PhotoLifecycle
    from image_safety import MAX_IMAGE_SOURCE_BYTES
    directory = sessions._directory(state["id"])
    try:
        if state["mode"] == "timelapse":
            state = _sequence(sessions, state, verified=sequence_verified)
            if heavy_jobs.job_of(state).get("status") != "completed":
                state = _video(sessions, state)  # else the heavy-jobs role already rendered or recorded it
            source = sessions.artifact(state["id"], "frame_0000.jpg")
        else:
            source = sessions.artifact(state["id"], "result.jpg")
        receipt = frame_receipt(source)
        image = f"session_{state['id']}.jpg"
        publication = state.get("publication") or {}
        if publication.get("image", image) != image or publication.get("sha256", receipt["sha256"]) != receipt["sha256"]:
            raise ValueError("publication identity disagrees with the accepted output")
        # Durable intent and deterministic identity make every subsequent step replayable.
        state["publication"] = {"status": "pending", "image": image, "sha256": receipt["sha256"],
                                "source_receipt": receipt}
        sessions.save(state)
        lifecycle = PhotoLifecycle(engine.SHOTS_DIR)
        if lifecycle._regular(image):
            existing = frame_receipt(engine.SHOTS_DIR / image)
            if existing["sha256"] != receipt["sha256"]:
                raise ValueError("existing published image differs from accepted output")
        else:
            data = regular_bytes(source, MAX_IMAGE_SOURCE_BYTES)
            if hashlib.sha256(data).hexdigest() != receipt["sha256"]:
                raise ValueError("accepted output changed during publication")
            lifecycle._write(image, data)
        summary = (f"Timelapse sequence completed with {state['frame_count']} downloadable JPEG frames."
                   if state["mode"] == "timelapse" else
                   f"{state['mode']} completed from {state['frame_count']} frames.")
        stopped = state.get("stopped_early")
        if stopped:
            how = ("automatically at the requested cleared share"
                   if stopped.get("reason") == "cleared_share_threshold" else "when you pressed Stop")
            summary += (f" Stopped {how} after {stopped['accepted_frames']} of "
                        f"{stopped['requested_frames']} requested frames; only those frames were merged.")
        hdr_report = ((state.get("output") or {}).get("details") or {}).get("hdr") if state["mode"] == "hdr" else None
        if hdr_report:
            summary = f"{summary} {hdr_report['summary']}"
        sidecar = {"image": image, "summary": summary,
                   "why": "Exact frame receipts and verified settings restoration are recorded in the session.",
                   "settings": state["settings"], "cards": [], "mood": {}, "applied": {}, "intent": None,
                   "evidence": {"source": "usb_session", "session_id": state["id"]},
                   "ts": state.get("completed_at", state["updated_at"])}
        existing_sidecar = lifecycle._json(f"session_{state['id']}.json")
        if existing_sidecar:
            if (existing_sidecar.get("image") != image
                    or (existing_sidecar.get("evidence") or {}).get("session_id") != state["id"]):
                raise ValueError("existing sidecar belongs to a different session")
            sidecar = {**sidecar, **existing_sidecar}
        sidecar["evidence"] = {**sidecar.get("evidence", {}), "source": "usb_session", "session_id": state["id"]}
        if state["metadata"].get("capture_policy") is not None:
            sidecar["evidence"]["capture_policy"] = copy.deepcopy(state["metadata"]["capture_policy"])
        if state["metadata"].get("intent") is not None:
            sidecar["intent"] = state["metadata"]["intent"]
        if state["metadata"].get("stack_baseline") is not None:
            sidecar["evidence"]["stack_baseline"] = copy.deepcopy(state["metadata"]["stack_baseline"])
        if state.get("long_exposure_timing"):
            sidecar["evidence"]["long_exposure_timing"] = state["long_exposure_timing"]
        if state.get("exposure_policy"):
            sidecar["evidence"]["exposure_policy"] = state["exposure_policy"]
        # This record's durability was decided here, so /gallery never rereads the manifest for it.
        sidecar["evidence"]["delivery_durability_checked"] = True
        if state.get("delivery_durability"):
            # Frames are kept (never a reshoot); the warning must not stay buried in frame receipts.
            sidecar["evidence"]["delivery_durability"] = state["delivery_durability"]
            if state["delivery_durability"]["warning"] not in sidecar["summary"]:
                sidecar["summary"] += " " + state["delivery_durability"]["warning"]
        if stopped:
            sidecar["evidence"]["stopped_early"] = copy.deepcopy(stopped)
        if state["mode"] == "panorama":
            details = (state.get("output") or {}).get("details") or {}
            if details.get("panorama"):
                sidecar["evidence"]["panorama"] = details["panorama"]
        if hdr_report:
            sidecar["evidence"]["hdr"] = hdr_report
            details = (state.get("output") or {}).get("details") or {}
            if details.get("hdr_motion"):
                sidecar["evidence"]["hdr_motion"] = details["hdr_motion"]
        if state.get("sequence"):
            sidecar["sequence"] = state["sequence"]
        if state.get("video"):
            sidecar["video"] = state["video"]
        lifecycle._write_json(f"session_{state['id']}.json", sidecar)
        owned = [str(path.relative_to(engine.SHOTS_DIR)) for path in directory.iterdir()
                 if path.is_file() and not path.is_symlink()]
        lifecycle.enroll(image, owned_paths=owned)
        state.update(image=image, publication={"status": "published", "image": image,
                     "sha256": receipt["sha256"], "source_receipt": receipt,
                     "published_at": publication.get("published_at", time.time())})
        state.pop("publication_error", None)
        sessions.save(state)
    except Exception as exc:
        # Physical capture truth stays completed. Retry resumes publication, never shutter.
        state["publication"] = {**state.get("publication", {}), "status": "failed"}
        state["publication_error"] = f"{type(exc).__name__}: {exc}"
        sessions.save(state)
    return sessions.read(state["id"])


_LEGACY_ROLLUPS: dict[str, tuple[tuple[int, int], dict | None]] = {}


def _legacy_rollup(sessions, session_id: str) -> dict | None:
    """The manifest-derived rollup of one session, read once per manifest version (mtime, size).
    Raises OSError/ValueError for a deleted, unreadable or malformed session."""
    status = (sessions._directory(session_id) / "manifest.json").stat()
    version = (status.st_mtime_ns, status.st_size)
    cached = _LEGACY_ROLLUPS.get(session_id)
    if cached and cached[0] == version:
        return cached[1]
    rollup = sessions.read(session_id).get("delivery_durability")
    _LEGACY_ROLLUPS[session_id] = (version, rollup)
    return rollup


def _with_warning(row: dict, rollup: dict) -> None:
    if rollup["warning"] not in str(row.get("summary") or ""):
        row["summary"] = f"{row.get('summary') or ''} {rollup['warning']}".strip()


def project_delivery_durability(records: list[dict]) -> list[dict]:
    """Gallery rows written before the durability rollup existed carry no warning in their stored
    sidecar. Derive it from the session manifest (SessionRunner.read backfills it) and add it to the
    returned row only; the sidecar and the photo are never rewritten here. Two legacy shapes:
    a published session record (source usb_session, evidence.session_id), and an Auto stack's
    fallback ordinary photo (evidence.stack_policy.session_id), which gets the same fields this
    build writes for a fallback: stack_policy.delivery_durability, the top-level key only if the
    ordinary photo has no warning of its own, and the summary sentence.
    Records written since carry `delivery_durability_checked` (top level, or in stack_policy) and
    are never reread; a legacy manifest is read once per version for the life of the server."""
    session_id_pattern = re.compile(r"[0-9a-f]{32}")
    sessions = None
    for row in records:
        evidence = row.get("evidence")
        if not isinstance(evidence, dict):
            continue
        if evidence.get("source") == "usb_session":
            session_id = evidence.get("session_id")
            if (evidence.get("delivery_durability") or evidence.get("delivery_durability_checked")
                    or not isinstance(session_id, str) or not session_id_pattern.fullmatch(session_id)):
                continue
            sessions = sessions or runner()
            try:
                rollup = _legacy_rollup(sessions, session_id)
            except (OSError, ValueError, TypeError, KeyError):
                continue  # a deleted or unreadable session leaves the row as stored
            if rollup:
                row["evidence"] = {**evidence, "delivery_durability": rollup}
                _with_warning(row, rollup)
            continue
        policy = evidence.get("stack_policy")
        session_id = policy.get("session_id") if isinstance(policy, dict) else None
        if (not isinstance(session_id, str) or not session_id_pattern.fullmatch(session_id)
                or policy.get("delivery_durability") or policy.get("delivery_durability_checked")):
            continue
        sessions = sessions or runner()
        try:
            rollup = _legacy_rollup(sessions, session_id)
        except (OSError, ValueError, TypeError, KeyError):
            continue
        if rollup:
            row["evidence"] = {**evidence, "stack_policy": {**policy, "delivery_durability": rollup}}
            if not evidence.get("delivery_durability"):
                row["evidence"]["delivery_durability"] = rollup
            _with_warning(row, rollup)
    return records


def require_camera_serial(state) -> str:
    """The session's bound USB serial. A session saved before serial binding has none; it must
    never resume or recover by port alone, so it needs an explicit re-selection first."""
    serial = (state.get("metadata") or {}).get("camera_serial")
    if not serial:
        from sonyremote import CameraIdentityError
        metadata = state.get("metadata") or {}
        raise CameraIdentityError(
            f"Session {state.get('id')} was created before cameras were bound by USB serial. Select its "
            f"camera again (POST /sessions/{state.get('id')}/camera with camera_model, camera_port and "
            "camera_serial) before running, resuming or recovering it.",
            reason="reselection_required", model=metadata.get("camera_model"), port=metadata.get("camera_port"))
    return serial


def _job_active(sessions, session_id: str) -> bool:
    """The session's job receipt says queued or running (read raw; the caller holds the lock)."""
    job_path = sessions._directory(session_id) / "job.json"
    return job_path.exists() and json.loads(regular_bytes(job_path, 65536)).get("status") in ("queued", "running")


def reselect_camera(session_id: str, model: str, port: str, serial: str) -> dict:
    """Bind a session to an explicitly re-selected body. The model never changes; a serial, once
    bound, never changes either (only the port may, because ports re-enumerate). The OS must
    report that serial at that port now; a failed bind changes nothing."""
    from sonyremote.camera import verify_serial_at_port
    if not serial:
        raise ValueError("camera_serial is required")
    if not isinstance(port, str) or not re.fullmatch(r"usb:[0-9]+,[0-9]+", port):
        raise ValueError("Select an exact USB port")
    sessions = runner()
    # The operation lock covers the job check and the save: a job is submitted (its receipt is
    # written) and a session runs only under this same lock, so neither can slip in between.
    with sessions.operation(session_id):
        state = sessions.read(session_id)
        if _job_active(sessions, session_id):
            raise RuntimeError("Wait for the active session job before re-selecting its camera")
        metadata = state["metadata"]
        if model != metadata.get("camera_model"):
            raise ValueError("A session keeps its camera model; create a new session for another body")
        bound = metadata.get("camera_serial")
        if bound and bound != serial:
            raise ValueError(f"This session is bound to USB serial {bound}; select that body")
        verification = verify_serial_at_port(model, port, serial)  # OS only; opens no camera
        state.setdefault("camera_reselections", []).append(
            {"at": time.time(), "previous_port": metadata.get("camera_port"), "port": port,
             "previous_serial": bound, "serial": serial, "verification": verification})
        metadata.update(camera_port=port, camera_serial=serial)
        sessions.save(state)
        return sessions.read(session_id)


def run(session_id: str):
    sessions = runner()
    with _transaction(sessions, session_id):
        state = sessions.read(session_id)
        deferred = runtime_roles.heavy_jobs_deferred()
        if state["status"] == "completed":
            if deferred and heavy_jobs.pending_heavy_work(state):
                return heavy_jobs.enqueue(sessions, state)  # never publish before the worker's output
            if heavy_jobs.job_of(state) and heavy_jobs.pending_heavy_work(state):
                # Queued by the split runtime, now opened by the single process: finish it here.
                heavy_jobs.enqueue(sessions, state)
                heavy_jobs.process_locked(sessions, session_id)
                state = sessions.read(session_id)
                if state["heavy_job"]["status"] != "completed":
                    return state
            return _publish(sessions, state)
        require_runnable_ladder(state)  # before any camera connection
        # The budget is per session: earlier runs' time counts, a restart does not reset it.
        prior_elapsed = _recorded_execution_s(state)  # Refuses before any camera command.
        from sonyremote import Camera, CameraError
        import camera_caps
        metadata = state["metadata"]
        pano_options = pano_preview_every = crowd_options = None
        if state["mode"] == "crowd_remove":
            crowd_options = _crowd_options(state)  # Preview cadence and auto-stop before any shutter.
        if state["mode"] == "panorama":
            # Validate projection options and the preview cadence before any shutter.
            pano_options = _panorama_options(state)
            pano_preview_every = _panorama_preview_every(state)
            if state.get("exposure_policy") != "locked_base":
                state["exposure_policy"] = "locked_base"
                sessions.save(state)
        model, port = metadata.get("camera_model"), metadata.get("camera_port")
        if model not in engine.USB_CONTROL_MODELS or not isinstance(port, str) or not re.fullmatch(r"usb:[0-9]+,[0-9]+", port):
            raise CameraError("Session requires an exact supported camera model and USB port pair")
        serial = require_camera_serial(state)
        kwargs = {key: metadata[value] for key, value in (("model", "camera_model"), ("port", "camera_port"))
                  if metadata.get(value)}
        run_started = _clock()  # Connection, wake and restoration count too.

        def elapsed():
            return prior_elapsed + _clock() - run_started

        def checkpoint():
            # Setup boundaries are durable too: a hard kill cannot hand the next run the time spent so far.
            state["execution_budget"] = execution_budget_status(state["mode"], state.get("timelapse"), elapsed())
            sessions.save(state)
        engine._free_usb()
        # Every run, resume and recovery re-proves the body by serial before any write or shutter.
        with (_execution_recorded(sessions, session_id, elapsed), _identity_refusal_recorded(sessions, session_id),
              Camera(**kwargs, serial=serial) as cam):
            cam.quarantine_dir = engine.SHOTS_DIR / "quarantine"  # unproven downloads kept privately
            actual_camera = cam.info()
            if actual_camera.model != model or actual_camera.port != port:
                raise CameraError("Connected camera model/port differs from the session target")
            state["camera_serial_verification"] = engine.serial_evidence(cam)["serial_verification"]
            checkpoint()  # After connection.
            if not cam.wake() or cam.shooting_mode() != "M":
                raise CameraError("Wake the camera and set the physical mode dial to M")
            checkpoint()  # After wake.
            if not state.get("restore_baseline"):
                if any(frame["status"] != "pending" for frame in state["frames"]):
                    raise CameraError("Session predates immutable restoration; reconcile its original settings before retry")
                from sonyremote.camera import read_settings_checked
                # Never keep a placeholder read (f/0, ISO 0, 65535/65535) as the restore target.
                original, baseline_read = read_settings_checked(cam, purpose="session restore baseline")
                if any(key not in original for key in RESTORE_KEYS):
                    raise CameraError("Cannot establish complete original camera settings")
                if baseline_read:
                    state["restore_baseline_read"] = baseline_read
                state["restore_baseline"] = dict(original)
                state["baseline_saved_at"] = time.time()
                if not state["settings"]:
                    state["settings"] = dict(original)
                checkpoint()  # Baseline survives failed restoration and all retries.
            baseline = state["restore_baseline"]
            # A session saved before placeholder reads were refused may hold one: never restore it.
            _refuse_placeholder_snapshot(state, baseline, "restore baseline")
            _refuse_placeholder_snapshot(state, state["settings"], "base settings")
            recovery_restored = False
            if state.get("restore") in ("failed", "pending"):
                try:
                    _restore(cam, baseline)
                    recovery_restored = True
                    state["restore"] = "completed"
                    state.pop("restore_error", None)
                    checkpoint()
                except Exception as exc:
                    state.update(status="restore_failed", restore="failed", restore_error=str(exc))
                    if getattr(exc, "identity_refused", False) is True:
                        state["restore_identity_refusal"] = exc.as_dict()
                    sessions.save(state)
                    return _record_execution(sessions, session_id, elapsed)
            # A recovery may restore settings while capture ambiguity still blocks re-exposure.
            if any(frame["status"] in ("capturing", "ambiguous") for frame in state["frames"]):
                return _record_execution(sessions, session_id, elapsed)
            if _start_settings(state) is not None:
                # Start settings run exactly as given, in this body's own labels, or not at all: a value
                # off the live ladder is refused here, before any setting write or shutter.
                live, problem = live_start_settings(state["settings"], cam.list_options("iso"),
                                                    cam.list_options("aperture"), cam.list_options("shutter"))
                if problem:
                    state.update(status="failed", error=f"Start settings refused: {problem}; nothing was written "
                                 "or fired. Meter again or choose values this camera offers.",
                                 start_settings_refusal={"detail": problem, "may_have_fired": False,
                                                         "retry_capture": False, "settings": dict(state["settings"])})
                    sessions.save(state)
                    return sessions.read(session_id)
                if live != {key: state["settings"].get(key) for key in _EXPOSURE_KEYS}:
                    state["settings"] = {**state["settings"], **live}
                    sessions.save(state)
            base = state["settings"]
            choices = cam.list_options("iso")
            numeric = [int(value) for value in choices if str(value).isdigit() and int(value) >= 100]
            if "auto" in str(base["iso"]).lower() and not numeric:
                raise CameraError("No fixed ISO available for session")
            base_iso = str(min(numeric)) if "auto" in str(base["iso"]).lower() else base["iso"]
            caps = camera_caps.from_live_choices(cam.info().model, choices, cam.list_options("aperture"), cam.list_options("shutter"))
            base_seconds = camera_caps._shutter_seconds(base["shutter"])
            checkpoint()  # After capability discovery.
            from long_exposure_plan import apply_duration_plan
            if state.get("duration_s") is not None:
                confirmed = base if _start_settings(state) is not None else baseline
                if any(str(base.get(key)) != str(confirmed.get(key)) for key in ("iso", "aperture", "shutter")):
                    raise CameraError("Duration averaging must preserve the confirmed baseline exposure")
                if "auto" in str(base["iso"]).lower():
                    raise CameraError("Duration averaging requires a fixed baseline ISO before capture")
                if not any(abs(value-base_seconds) < 1e-9 for value in caps.shutters):
                    raise CameraError("Baseline shutter is not a confirmed supported camera choice")
            duration_plan = apply_duration_plan(state, base_seconds)
            if duration_plan:
                sessions.save(state)  # Complete plan and frame budget precede every shutter.
            # The whole fixed ladder is checked before the first shutter, so a bracket whose long or
            # short end the transport or body cannot take never leaves a partial session behind.
            night_mode = state["mode"] == "night" and (state.get("metadata") or {}).get("night_assist")
            ladder_refusal = fixed_ladder_refusal(
                state, base_shutter=base["shutter"], caps=caps, recovery_restored=recovery_restored,
                iso_options=cam.list_options("iso") if night_mode else (),
                aperture_options=cam.list_options("aperture") if night_mode else ())
            if ladder_refusal:
                state.update(status="failed", error=ladder_refusal["detail"], ladder_refusal=ladder_refusal)
                sessions.save(state)
                return sessions.read(session_id)
            duration_started = None
            from stack_planner import timelapse_config, jpeg_luminance, metered_timelapse_step
            config = state.get("timelapse") or timelapse_config()
            from timelapse_controls import read_controls, adapt_pending
            controls = read_controls(sessions, session_id) if state["mode"] == "timelapse" else None
            if controls and controls.get("smartlapse") and not any(f["status"] == "accepted" for f in state["frames"]):
                if state["frame_count"] != controls["smartlapse"]["requested_frames"]:
                    raise CameraError("Smartlapse controls and frame manifest differ; reapply controls before capture")
            state["execution_budget"] = execution_budget_status(state["mode"], (controls or {}).get("exposure", config), elapsed())
            sessions.save(state)  # The runner reads this record; a new session starts at zero.
            ramp_state = {"requested_ev": 0.0}
            accepted = [frame for frame in state["frames"] if frame["status"] == "accepted"]
            if accepted:
                ramp_state.update(accepted[-1].get("applied", {}).get("timelapse_meter", {}))

            def prepare(index, spec):
                nonlocal config, controls
                if duration_plan and duration_started is not None:
                    if time.monotonic()-duration_started+base_seconds > duration_plan["duration_s"] + max(.5, duration_plan["duration_s"]*.05):
                        raise CameraError("Requested duration window elapsed; remaining frames were not captured")
                if state["mode"] == "timelapse":
                    controls = read_controls(sessions, session_id)
                    config = controls["exposure"]
                budget_s = execution_budget_s(state["mode"], config)
                if elapsed() >= budget_s:
                    raise CameraError(f"{budget_s / 3600:g}-hour session execution budget reached; "
                                      "accepted frames are preserved")
                if index == 0 and controls and controls.get("smartlapse") and controls["smartlapse"]["duration_s"] > budget_s:
                    # Checked against the controls this first frame uses; a later switch only shortens the run.
                    raise CameraError(f"Smartlapse duration exceeds the {budget_s / 3600:g}-hour session "
                                      "execution budget for this exposure mode; reapply controls")
                _, _, label = planned_frame_shutter(spec, base_seconds=base_seconds, caps=caps)
                iso = str(base_iso)
                aperture = base["aperture"]
                night_plan = (state.get("metadata") or {}).get("night_assist") if state["mode"] == "night" else None
                if night_plan:
                    # Night Assist: the planner's measured-table exposure replaces the base exposure
                    # for every frame; the ladders are re-snapped so a stale plan cannot request a
                    # value this body does not offer right now.
                    label, iso, aperture = night_plan_exposure(night_plan, caps, cam.list_options("iso"),
                                                               cam.list_options("aperture"))
                    state.setdefault("exposure_policy", "night_assist_plan")
                from sonyremote.camera import read_settings_checked
                rereads = []
                if state["mode"] == "timelapse":
                    before_settings, reread = read_settings_checked(cam, purpose="timelapse before settings")
                    rereads += [reread] if reread else []
                else:
                    before_settings = dict(base)
                meter = None
                if state["mode"] == "timelapse" and config["mode"] != "fixed":
                    from timelapse_controls import mounted_lens_for_frame
                    from sonyremote.camera import SONY_ACTION_MAX_ENABLED_SHUTTER_S
                    bounded_caps, transport_limits = camera_caps.constrain_capture_duration(
                        caps, max_shutter_s=SONY_ACTION_MAX_ENABLED_SHUTTER_S)
                    previous = sessions.artifact(session_id, f"frame_{index-1:04d}.jpg") if index > 0 else None
                    start_frame_0 = index == 0 and _start_settings(state) is not None
                    lens_caps, lens_evidence = mounted_lens_for_frame(
                        bounded_caps, base["aperture"] if start_frame_0 else before_settings["aperture"], previous)
                    # The preceding accepted JPEG supplies the next exposure's feedback.
                    if index > 0:
                        previous = sessions.artifact(session_id, f"frame_{index - 1:04d}.jpg")
                        measurement = jpeg_luminance(previous)
                        meter = metered_timelapse_step(config, current_ev=ramp_state["requested_ev"],
                            luminance=measurement["linear_luminance"], base_shutter_s=base_seconds,
                            base_iso=float(base_iso), caps=lens_caps, base_aperture=base["aperture"],
                            mounted_lens_evidence=lens_evidence, measured_ev=ramp_state.get("actual_ev"),
                            scene_estimate=ramp_state.get("scene_ev_estimate"))
                        meter["source_sha256"] = frame_receipt(previous)["sha256"]
                        # Carry exactly this frame's record, as a restart rebuilds it; a mode
                        # without smoothing must not leave a stale estimate for a later switch.
                        ramp_state.clear()
                        ramp_state.update(meter)
                        label, iso, aperture = meter["shutter"], meter["iso"], meter["aperture"]
                        meter["transport_limits"] = transport_limits
                    else:
                        # Reject unusable ranges before the first physical exposure.
                        initial = metered_timelapse_step(config, current_ev=0,
                            luminance=config["target_luminance"], base_shutter_s=base_seconds,
                            base_iso=float(base_iso), caps=lens_caps, base_aperture=base["aperture"],
                            mounted_lens_evidence=lens_evidence)
                        if start_frame_0 and not _same_exposure(initial, base):
                            raise CameraError(
                                f"Frame 0 would be ISO {initial['iso']} · {initial['aperture']} · {initial['shutter']}, "
                                f"not the start settings ISO {base['iso']} · {base['aperture']} · {base['shutter']}; "
                                "no timelapse frame was fired.")
                        label, iso, aperture = initial["shutter"], initial["iso"], initial["aperture"]
                        initial["transport_limits"] = transport_limits
                        ramp_state.clear()
                        ramp_state.update(initial)  # Frame 1 meters against frame 0's actual EV.
                        meter = initial
                elif state["mode"] == "timelapse":
                    # A Fixed frame carries no meter record, so a restart would resume with no
                    # anchor or estimate; the running ramp must forget them the same way.
                    ramp_state.clear()
                    ramp_state["requested_ev"] = 0.0
                from sonyremote.camera import validate_sony_capture_shutter
                validate_sony_capture_shutter(label)  # Before any shutter intent is persisted.
                cam.set_setting("focus_mode", "Manual")
                cam.set_exposure(iso=iso, aperture=aperture, shutter=label)
                actual, reread = read_settings_checked(cam, purpose="session frame readback")
                rereads += [reread] if reread else []
                validate_sony_capture_shutter(actual.get("shutter"))
                expected = {"iso": iso, "aperture": aperture, "shutter": label, "focus_mode": "Manual"}
                if any(str(actual.get(key)) != str(value) for key, value in expected.items()):
                    raise CameraError("Session settings were not confirmed by readback")
                if meter:
                    actual["timelapse_meter"] = meter
                if rereads:
                    actual["settings_rereads"] = rereads
                if controls:
                    actual["timelapse_control_revision"] = controls["revision"]
                    actual["timelapse_priority"] = config.get("priority", "shutter_then_iso")
                    actual["exposure_transition"] = {"before": before_settings,
                        "requested": expected, "after": {key: actual.get(key) for key in expected},
                        "control_revision": controls["revision"], "readback_verified": True}
                return actual

            def capture(index, spec, destination):
                nonlocal duration_started
                engine._free_usb()
                if duration_plan and duration_started is None:
                    duration_started = time.monotonic()
                # A capture refused before it starts its own receipt (identity) must not leave the
                # previous frame's receipt to be copied onto this frame.
                cam.last_capture_receipt = None
                try:
                    source = cam.capture(download=True)
                finally:
                    spec["transport_receipt"] = copy.deepcopy(getattr(cam, "last_capture_receipt", None) or {})
                if not source:
                    raise CameraError("No new camera file returned; do not automatically retry")
                from image_safety import MAX_IMAGE_SOURCE_BYTES
                data = regular_bytes(Path(source), MAX_IMAGE_SOURCE_BYTES)
                descriptor = os.open(destination, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
                with os.fdopen(descriptor, "wb") as stream:
                    stream.write(data)
                    stream.flush()
                    os.fsync(stream.fileno())
                return destination

            def accepted_boundary(live_state):
                if live_state["mode"] == "crowd_remove":
                    _crowd_boundary(sessions, session_id, live_state, crowd_options)
                    return
                if live_state["mode"] == "panorama":
                    _panorama_boundary(sessions, session_id, live_state, pano_options,
                                       preview_every=pano_preview_every,
                                       timeout=PANORAMA_PREVIEW_TIMEOUT_S)
                    return
                if live_state["mode"] != "timelapse":
                    return
                current = read_controls(sessions, session_id)
                count = sum(f["status"] == "accepted" for f in live_state["frames"])
                every = current["preview_every"]
                before_preview = time.monotonic()
                if (every and count >= 2 and not (sessions._directory(session_id) / "cancel").exists()
                        and (count % every == 0 or count == live_state["frame_count"])):
                    try:
                        previous = live_state.get("preview_video") or {}
                        live_state["preview_video"] = _incremental_video(sessions, live_state, current["exposure"]["fps"], current.get("deflicker"))
                        live_state.pop("preview_video_error", None)
                        sessions.save(live_state)  # Publish verified identity before removing prior snapshot.
                        if previous.get("name") and previous["name"] != live_state["preview_video"]["name"]:
                            (sessions._directory(session_id) / previous["name"]).unlink(missing_ok=True)
                    except Exception as exc:
                        live_state["preview_video_error"] = {"message": str(exc), "accepted_frames_preserved": True}
                if current.get("smartlapse"):
                    # Rendering cost contributes to the observed next-frame budget.
                    cost = max(f.get("capture_s", 0) for f in live_state["frames"][:count][-3:])
                    cost += time.monotonic() - before_preview
                    adapt_pending(live_state, current["smartlapse"],
                                  elapsed_s=elapsed(),
                                  observed_frame_s=cost)
                live_state["controls_revision"] = current["revision"]

            if state["mode"] == "timelapse" or deferred:
                merge = None  # split runtime: the heavy-jobs role merges after restoration
            elif state["mode"] == "panorama":
                merge = lambda paths, out: merge_session_frames("panorama", paths, out, panorama_options=pano_options)
            else:
                merge = lambda paths, out: merge_session_frames(state["mode"], paths, out)
            def progress(live_state):
                live_state["execution_budget"] = execution_budget_status(live_state["mode"], config, elapsed())
            result = sessions.run(session_id, capture, merge, prepare_frame=prepare,
                                  after_frame=accepted_boundary, progress=progress,
                                  restore_settings=lambda: _restore(cam, baseline),
                                  queue_deferred_merge=heavy_jobs.queue if deferred else None)
        result = sessions.read(session_id)  # With this run's recorded execution time.
        if result["status"] == "completed":
            if deferred:
                return heavy_jobs.enqueue(sessions, result)
            return _publish(sessions, result)
        return result


def publish_deferred() -> list[str]:
    """Control role: publish sessions whose heavy job completed. The scan reads manifests without
    the lifecycle lock; the lock is taken non-blocking per session, only around its publication, so a
    shutter press never waits behind the sweep. A busy session waits for the next sweep; a failed
    publication waits for an explicit ``POST /sessions/{id}/run``."""
    from photo_lifecycle import _LOCK
    sessions = runner()
    published = []
    for directory in sorted(sessions.root.iterdir()):
        session_id = directory.name
        if not re.fullmatch(r"[0-9a-f]{32}", session_id) or not (directory / "manifest.json").is_file():
            continue
        try:
            state = sessions.read(session_id)
            heavy = heavy_jobs.job_of(state)
            if (state["status"] != "completed" or heavy.get("status") != "completed"
                    or (state.get("publication") or {}).get("status") in ("published", "failed")):
                continue
            if state["mode"] == "timelapse":
                # Re-hash the (up to 512 MB) ZIP before taking the lock a shutter press also waits on.
                sessions.artifact(session_id, "sequence.zip")
        except (OSError, ValueError) as exc:
            if not isinstance(exc, FileNotFoundError):
                _LOG.warning("deferred publication of %s skipped: %s: %s", session_id, type(exc).__name__, exc)
            continue
        if not _LOCK.acquire(blocking=False):
            break  # capture or a gallery operation owns the lifecycle; the next sweep retries
        try:
            with _transaction(sessions, session_id):
                current = sessions.read(session_id)
                unchanged = (current.get("sequence") or {}).get("receipt") == (state.get("sequence") or {}).get("receipt")
                result = _publish(sessions, current, sequence_verified=unchanged and current["mode"] == "timelapse")
        except RuntimeError:
            continue  # the session is busy (worker, deletion or a run in progress)
        except (OSError, ValueError) as exc:
            _LOG.warning("deferred publication of %s failed: %s: %s", session_id, type(exc).__name__, exc)
            continue
        finally:
            _LOCK.release()
        if (result.get("publication") or {}).get("status") == "published":
            published.append(session_id)
        else:
            _LOG.warning("deferred publication of %s failed: %s", session_id, result.get("publication_error"))
    return published


def delete(session_id: str, *, discard_unresolved: bool = False):
    from photo_lifecycle import PhotoLifecycle
    sessions = runner()
    with _transaction(sessions, session_id):
        state = sessions.read(session_id)
        publication = state.get("publication") or {}
        image = publication.get("image")
        if image and image != f"session_{session_id}.jpg":
            raise ValueError("publication identity disagrees with session")
        lifecycle = PhotoLifecycle(engine.SHOTS_DIR)
        if image:
            lifecycle._deletion_paths(image)  # Preflight before deleting any session evidence.
        result = sessions._delete_locked(session_id, discard_unresolved=discard_unresolved)
        if image and lifecycle._regular(image):
            result["gallery"] = lifecycle.delete(image)
        return result


def _deflicker_frames(sessions, state, frames, control):
    """Write gain-corrected copies of `frames` under sessions/<id>/deflicker/.

    Only hash-verified accepted originals are read (via ``sessions.artifact``); the
    corrected copies live in a private subdirectory so the accepted originals and the
    exact-input ZIP are never touched. Raises on any failure so the caller can fall back
    to rendering the raw frames. Returns (frames_dir, manifest_info).
    """
    import deflicker
    directory = sessions._directory(state["id"])
    out_dir = directory / "deflicker"
    if out_dir.is_symlink():
        raise ValueError("deflicker directory cannot be a symlink")
    sources = [sessions.artifact(state["id"], f"frame_{frame['index']:04d}.jpg") for frame in frames]
    luma = deflicker.luminance_series(sources)
    gains = deflicker.deflicker_gains(luma, window=control["window"], max_gain_stops=control["max_gain_stops"])
    produced = deflicker.apply_gains_to_frames(sources, gains, out_dir)
    info = {"window": control["window"], "max_gain_stops": control["max_gain_stops"],
            "gains": gains, "frame_count": len(gains),
            "frames": deflicker.frame_receipts(sources, produced, gains)}
    return out_dir, info


DEFLICKER_PREVIEW_MAX_FRAMES = 24
# The live preview shows at most this many frames. Longer runs (SmartLapse and Holy Grail go to
# 240) are sampled evenly across the whole accepted run, so the preview keeps updating to the latest
# frame instead of freezing at frame 100 (owner's call, 2026-09-22). The final video uses every frame.
PREVIEW_MAX_FRAMES = 100


def _even_sample(count, limit):
    """Indices into `count` items: all of them, or `limit` spread evenly keeping both ends.

    Integer arithmetic, so it's exact and strictly increasing (the step is at least 1 when
    count > limit), and the last index is always count - 1: the preview ends on the newest frame.
    """
    if count <= limit:
        return list(range(count))
    return [(i * (count - 1)) // (limit - 1) for i in range(limit)]


def _link_preview_frames(sessions, state, shown, directory):
    """Renumber the sampled frames frame_0000.. in a private folder for ffmpeg's image2 input.

    Hard links, not copies (24 MP JPEGs) and not symlinks; falls back to a copy only where the
    filesystem refuses a link. The caller removes the folder.
    """
    folder = directory / "preview_frames"
    _remove_preview_dir(folder)
    folder.mkdir(mode=0o700)
    for position, frame in enumerate(shown):
        source = sessions.artifact(state["id"], f"frame_{frame['index']:04d}.jpg")
        target = folder / f"frame_{position:04d}.jpg"
        try:
            os.link(source, target)
        except OSError:
            shutil.copyfile(source, target)
    return folder


def _deflicker_render_dir(sessions, state, frames, control, default_dir, *, incremental=False):
    """Resolve the ffmpeg input directory, applying deflicker when enabled.

    Returns (frames_dir, meta) where meta carries either the deflicker manifest under
    ``deflicker`` or ``deflicker_error`` after a fallback to the untouched originals.
    """
    if not control:
        return default_dir, {}
    if incremental and len(frames) > DEFLICKER_PREVIEW_MAX_FRAMES:
        # Re-deflickering the whole accepted prefix at every boundary is quadratic in decodes;
        # long sequences keep raw previews and get the corrected frames in the final render.
        return default_dir, {"deflicker_deferred_to_final": True, "frame_count": len(frames)}
    try:
        frames_dir, info = _deflicker_frames(sessions, state, frames, control)
        return frames_dir, {"deflicker": info}
    except Exception as exc:
        return default_dir, {"deflicker_error": f"{type(exc).__name__}: {exc}"}


def _video(sessions, state):
    """Render accepted JPEGs to 1920×1080 H.264; decode every output frame."""
    from timelapse_controls import read_controls
    from timelapse_video import EncoderUnavailable, probe_encoder, render_h264
    if (state.get("video") or {}).get("status") == "completed":
        sessions.artifact(state["id"], "timelapse.mp4")
        return state
    directory = sessions._directory(state["id"])
    destination = directory / "timelapse.mp4"
    pending = directory / "timelapse.pending.mp4"
    fps = (state.get("timelapse") or {}).get("fps", 24)
    tools = probe_encoder()
    if not tools:
        # Probe first: without an encoder the deflicker copies (up to 240 full JPEGs) would be wasted.
        state["video"] = {"status": "unavailable",
                          "reason": "Local ffmpeg and ffprobe must execute (-version); JPEG sequence is preserved."}
        sessions.save(state)
        return state
    control = (read_controls(sessions, state["id"]) or {}).get("deflicker")
    frames_dir, deflicker_meta = _deflicker_render_dir(sessions, state, state["frames"], control, directory)
    try:
        inputs = []
        for frame in state["frames"]:
            path = sessions.artifact(state["id"], f"frame_{frame['index']:04d}.jpg")
            inputs.append({"file": path.name, "sha256": frame["receipt"]["sha256"]})
        from stack_session import _atomic_json
        _atomic_json(directory / "video-inputs.json", {"fps": fps, "inputs": inputs, "width": 1920,
                                                      "height": 1080, "frame_count": len(inputs)})
        pending.unlink(missing_ok=True)
        # Exclusive private file prevents a partial output from becoming a public artifact.
        fd = os.open(pending, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        os.close(fd)
        info = render_h264(frames_dir, len(inputs), fps, pending, tools=tools)
        os.replace(pending, destination)
        # A finished timelapse plays back its frames at `fps`, but each frame stands for one
        # real-world interval, so the honest span the video represents is the achieved cadence,
        # never fps * frame_count. Surface it (and the achieved cadence it was built from) beside
        # the probe's playback duration so the receipt cannot imply the requested-interval span.
        cadence = timelapse_cadence(state)
        state["video"] = {"status": "completed", "receipt": binary_receipt(destination, 128 * 1024 * 1024),
                          "url": f"/sessions/{state['id']}/artifacts/timelapse.mp4", "fps": fps,
                          "width": 1920, "height": 1080,
                          "playback_duration_s": info["playback_duration_s"],
                          "probe": info, "full_decode_verified": True,
                          "real_time_span_s": cadence["video_real_time_span_s"],
                          "requested_real_time_span_s": cadence["video_requested_real_time_span_s"],
                          "requested_interval_s": cadence["requested_interval_s"],
                          "achieved_interval_median_s": cadence["achieved_interval_median_s"],
                          "cadence_overrun": cadence["cadence_overrun"],
                          "frames": cadence["frames"],
                          "input_manifest": "video-inputs.json", **deflicker_meta}
    except EncoderUnavailable as exc:
        pending.unlink(missing_ok=True)
        state["video"] = {"status": "unavailable", "reason": str(exc), **deflicker_meta}
    except Exception as exc:
        pending.unlink(missing_ok=True)
        state["video"] = {"status": "failed", "error": f"{type(exc).__name__}: {exc}",
                          "frames_preserved": True, "retry_render_safe": True, **deflicker_meta}
    sessions.save(state)
    return state


def encode_partial(session_id: str) -> dict:
    """Owner-triggered (2026-10-02): encode the accepted frames of a timelapse blocked by an ambiguous
    frame into ``timelapse_partial.mp4`` plus ``timelapse_partial.json``. Opens no camera, fires
    nothing and never changes the session status, so it never completes, resumes or re-fires.
    Refuses (``timelapse_partial.PartialRefused``) any other mode or status and fewer than 2
    accepted frames; a busy session raises RuntimeError. The split runtime's control role queues the
    encode for the heavy-jobs role, which never loads the encoder here."""
    import timelapse_partial
    sessions = runner()
    with sessions.operation(session_id):
        state = sessions.read(session_id)
        timelapse_partial.plan(state)
        existing = state.get("partial_video") or {}
        if existing.get("status") == "completed":
            # Already made: re-verify both files and return; a second request never re-encodes.
            sessions.artifact(session_id, timelapse_partial.PARTIAL_NAME)
            sessions.artifact(session_id, timelapse_partial.RECEIPT_NAME)
            return state
        if runtime_roles.heavy_jobs_deferred():
            if existing.get("status") not in ("queued", "running"):
                state["partial_video"] = {"status": "queued", "requested_at": time.time(), "attempts": 0}
                sessions.save(state)
            return sessions.read(session_id)
        return _encode_partial_locked(sessions, session_id)


def _encode_partial_locked(sessions, session_id: str) -> dict:
    """Make the partial movie; the caller holds ``sessions.operation(session_id)``."""
    import timelapse_partial
    from stack_session import _atomic_json
    from timelapse_controls import read_controls
    from timelapse_video import EncoderUnavailable
    state = sessions.read(session_id)
    partial_plan = timelapse_partial.plan(state)
    directory = sessions._directory(session_id)
    try:
        # First, before any check can fail: a killed earlier attempt's folder and unfinished movie.
        timelapse_partial.clean_stale(directory / timelapse_partial.WORK_DIR_NAME,
                                      directory / timelapse_partial.PENDING_NAME)
        control = (read_controls(sessions, session_id) or {}).get("deflicker")
        # artifact() re-hashes every accepted original against its receipt before the encode.
        sources = [{"index": frame["index"], "sha256": frame["receipt"]["sha256"],
                    "path": sessions.artifact(session_id, f"frame_{frame['index']:04d}.jpg")}
                   for frame in partial_plan["frames"]]
        encoded = timelapse_partial.encode(
            sources, fps=partial_plan["fps"], deflicker_control=control,
            work_dir=directory / timelapse_partial.WORK_DIR_NAME,
            pending=directory / timelapse_partial.PENDING_NAME,
            destination=directory / timelapse_partial.PARTIAL_NAME)
        video_receipt = binary_receipt(directory / timelapse_partial.PARTIAL_NAME, timelapse_partial.MAX_VIDEO_BYTES)
        document = timelapse_partial.receipt_document(session_id=session_id, partial_plan=partial_plan,
                                                      sources=sources, encoded=encoded, output=video_receipt)
        _atomic_json(directory / timelapse_partial.RECEIPT_NAME, document)
        record = timelapse_partial.manifest_record(
            session_id=session_id, document=document, video_receipt=video_receipt,
            document_receipt=binary_receipt(directory / timelapse_partial.RECEIPT_NAME))
    except EncoderUnavailable as exc:
        record = {"status": "unavailable", "reason": str(exc), "frames_preserved": True}
    except Exception as exc:  # noqa: BLE001 — recorded; the accepted originals are untouched
        record = {"status": "failed", "error": f"{type(exc).__name__}: {exc}",
                  "frames_preserved": True, "retry_safe": True}
    current = sessions.read(session_id)
    current["partial_video"] = record
    sessions.save(current)
    return sessions.read(session_id)


def _incremental_video(sessions, state, fps, deflicker=None):
    """Publish one 1920×1080, fully decoded preview from the accepted prefix only."""
    from timelapse_video import EncoderUnavailable, probe_encoder, render_h264
    tools = probe_encoder()
    if not tools:
        raise EncoderUnavailable('Preview requires ffmpeg and ffprobe that execute (-version); frames remain available')
    accepted = [frame for frame in state['frames'] if frame['status'] == 'accepted']
    if len(accepted) < 2 or any(frame['index'] != i for i, frame in enumerate(accepted)):
        raise ValueError('Preview requires an accepted prefix of at least 2 frames')
    directory = sessions._directory(state['id'])
    shown = [accepted[i] for i in _even_sample(len(accepted), PREVIEW_MAX_FRAMES)]
    sampling = 'even' if len(shown) < len(accepted) else 'all'
    frames_dir, deflicker_meta = _deflicker_render_dir(sessions, state, accepted, deflicker, directory, incremental=True)
    name = f'preview_{len(accepted):04d}.mp4'
    destination = directory / name
    pending = directory / 'preview.pending.mp4'
    inputs = []
    for frame in shown:
        sessions.artifact(state['id'], f"frame_{frame['index']:04d}.jpg")
        inputs.append({'index': frame['index'], 'sha256': frame['receipt']['sha256']})
    from stack_session import _atomic_json
    # The manifest lists exactly the frames in the preview, never the whole run it was sampled from.
    _atomic_json(directory / 'preview-inputs.json', {'frames': inputs, 'fps': fps, 'width': 1920, 'height': 1080,
                                                     'sampling': sampling, 'accepted_frames': len(accepted)})
    pending.unlink(missing_ok=True)
    fd = os.open(pending, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    os.close(fd)
    linked = None
    try:
        if sampling == 'even':
            # PREVIEW_MAX_FRAMES > DEFLICKER_PREVIEW_MAX_FRAMES, so deflicker is already deferred to
            # the final render here and frames_dir is the session's own sequence; renumber the sample.
            frames_dir = linked = _link_preview_frames(sessions, state, shown, directory)
        info = render_h264(frames_dir, len(shown), fps, pending, preview=True, tools=tools)
        os.replace(pending, destination)
        return {'status': 'completed', 'name': name, 'frame_count': len(shown),
                'accepted_frames': len(accepted), 'sampling': sampling,
                'receipt': binary_receipt(destination, 16 * 1024 * 1024), 'fps': fps,
                'width': 1920, 'height': 1080,
                'playback_duration_s': info['playback_duration_s'],
                'url': f"/sessions/{state['id']}/artifacts/{name}",
                'full_decode_verified': True, 'inputs': inputs, 'probe': info, **deflicker_meta}
    finally:
        pending.unlink(missing_ok=True)
        if linked is not None:
            _remove_preview_dir(linked)
