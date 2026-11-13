"""Focus-bracket planner and runner over the A7 III libgphoto2 manualfocus route.

The route (proved shutter-free on 2026-09-15, `artifacts/stage-wave-20260915/route-matrix/
focus-experiment-v3/`): with `focusmode=Manual`, each `manualfocus=<-7..7>` write is one RELATIVE
lens adjust whose widget reads back 0. Step response was asymmetric and clipped at the rail, so
positions here are *commands sent*, never calibrated distances. Preview sharpness is the centre
variance of the Laplacian on the small liveview JPEG; it is relative to the scene and lens.

`run_bracket` never fires the shutter itself: the caller passes the existing single-exposure
`capture` callable. A capture whose outcome is uncertain is re-raised, never retried, and
restoration of the original focus mode always runs in `finally`.

`plan_from_sweep` turns a `focus_sweep/1` centre curve, or a `focus_sweep/2` sweep with per-tile
curves (tools/focus_sweep.py, shutter-free), into an explicit plan whose per-frame relative moves may differ in size and sign. Every move is issued
as repeated commands of the sweep's own `step` magnitude: the live sweep of 2026-09-15
(`artifacts/stage-wave-20260915/focus-sweep-03/`) showed magnitude-3 commands reproduce the same
curve in both directions while a route made of +-7 chunks landed at 16 % of peak sharpness — the
manualfocus value is a step-size class with a non-linear, per-magnitude response, so a move is
only reproducible when it is made of the commands that measured it. Offsets are cumulative
commands from the sweep's start position (AF-locked after one half-press), never distances.
"""
from __future__ import annotations

import hashlib
import json
import math
import os
import time
from pathlib import Path
from typing import Callable

import cv2
import numpy as np
from image_safety import open_rgb

import camera_caps
import lens_optics
import night_assist
import rig_profile

MIN_FRAMES, MAX_FRAMES = 2, 7
MIN_STEP, MAX_STEP = 1, 7
DIRECTIONS = ("near", "far")
# libgphoto2 names the Sony property NearFar; negative moves toward near. Optical sign on the
# FE 24-105 has NOT been verified — the 2026-09-15 experiment only showed asymmetric motion.
DIRECTION_SIGN = {"near": -1, "far": +1}
CALIBRATION_NOTE = "uncalibrated relative steps; asymmetric rail behaviour observed 2026-09-15"
STEP_SETTLE_S = 0.5
MANIFEST_NAME = "bracket_manifest.json"
SHARPNESS_CENTER_FRACTION = 0.5
# focus_sweep/2 adds a per-tile sharpness grid to every sample (`tile_grid`, point `tiles`) and a
# bounded far-walk extension; focus_sweep/1 (centre curve only) is still planned the old way.
SWEEP_SCHEMA = "focus_sweep/2"
SWEEP_SCHEMAS = ("focus_sweep/1", SWEEP_SCHEMA)
TILE_ROWS, TILE_COLS = 3, 4
TILE_METRIC = "Laplacian variance per tile of the whole liveview preview; relative, uncalibrated"
# A tile shows a focus peak only when its best sample clearly stands above its most defocused one:
# at least TILE_PEAK_CONTRAST x its minimum and TILE_MIN_RANGE_FLOORS noise floors above it (the
# sweep's floor: liveview repeatability was ~1-2 % at rest, 2026-09-15). Flatter tiles (sky, a
# blank wall) carry no depth evidence and are ignored.
TILE_PEAK_CONTRAST = 1.5
TILE_NOISE_FRACTION, TILE_NOISE_ABS = 0.03, 0.5
TILE_MIN_RANGE_FLOORS = 4
SWEEP_PLAN_ASSUMPTION = ("the bracket starts from the sweep's start position: the same scene and lens, "
                         "the same AF-locked position after one AF half-press; every move is made of "
                         "commands of the sweep's own step magnitude because only those are known to "
                         "reproduce; command offsets are not distances")


class FocusBracketError(RuntimeError):
    """A refusal made before, or a failure detected during, a bracket (not a shutter uncertainty)."""


# --- aperture selection (Arsenal 2: shoot the bracket at the lens's sharpest aperture) --------
# The bracket must hold ONE aperture across every frame so focus is the only variable. We pick it
# from the existing optics model (lens_optics.LensOptics.optimal_aperture, the sweet spot balancing
# aberration against diffraction) and snap it onto the body's settable ladder. Changing the aperture
# changes the exposure, so the stop difference is paid back — shutter first (a focus stack is a
# tripod workflow), bounded by the enabled capture envelope, then ISO within the body's ladder and
# the rig's usable ceiling. If the metered exposure cannot be matched within
# EXPOSURE_MATCH_TOLERANCE_STOPS the aperture is not changed at all. With no lens profile we invent
# nothing: keep the current aperture and record why. Reasons are stable, machine-checkable strings.
APERTURE_REASON_SWEET_SPOT = "lens_sweet_spot"
APERTURE_REASON_UNKNOWN_LENS = "unknown_lens_kept_current"
APERTURE_REASON_NO_LENS = "no_lens_specified_kept_current"
APERTURE_REASON_NO_LADDER = "no_settable_aperture_kept_current"
APERTURE_REASON_EXPOSURE_INFEASIBLE = "exposure_infeasible_kept_current"
EXPOSURE_MATCH_TOLERANCE_STOPS = 1 / 3
HELD_KEYS = ("aperture", "iso", "shutter")

# Lens ids (rig_profile canonical names, normalized) that have a measured LensOptics profile.
_PROFILED_LENS_OPTICS = {
    rig_profile.normalize_lens("Tamron 28-75mm F2.8"): lens_optics.TAMRON_28_75_OPTICS,
}


def _resolve_named_rig(body_model, lens_model):
    """The rig for a lens the operator names. The body's generic EXIF LensModel ("E 28-75mm
    F2.8-2.8") spells out its own LensSpecification, which the engine requires before it accepts
    that form, so the named form carries that specification here."""
    implied_spec = (rig_profile.GENERIC_SONY_TAMRON_SPEC
                    if rig_profile.normalize_lens(lens_model) == rig_profile.GENERIC_SONY_TAMRON_ALIAS else None)
    return rig_profile.resolve_rig(body_model, lens_model=lens_model, lens_specification=implied_spec)


def _parse_f_number(value) -> float | None:
    """Parse an aperture label ('f/5.6', 'F5.6', '5.6') to a positive f-number, else None."""
    if value is None:
        return None
    text = str(value).strip().lower()
    if text.startswith("f"):
        text = text[1:]
    text = text.lstrip("/").strip()
    try:
        number = float(text)
    except ValueError:
        return None
    return number if number > 0 and math.isfinite(number) else None


def _snap_to_ladder(target_f: float, options) -> tuple[str, float] | tuple[None, None]:
    """Nearest settable aperture to `target_f` by stops (|log2|); ties favour the wider stop.

    Returns the camera's own label string (so a later write uses the exact value it accepts).
    """
    parsed = [(str(opt), _parse_f_number(opt)) for opt in (options or [])]
    parsed = [(label, number) for label, number in parsed if number is not None]
    if not parsed:
        return None, None
    return min(parsed, key=lambda pair: (abs(math.log2(pair[1] / target_f)), pair[1]))


def choose_bracket_aperture(*, body_model, lens_model, current_aperture, aperture_options) -> dict:
    """Deterministically choose the bracket's single aperture. Pure and camera-free.

    With a profiled lens: the optics model's sweet spot, snapped to the settable ladder
    (`lens_sweet_spot`). Otherwise the current aperture is kept and the reason recorded — an
    unknown lens (`unknown_lens_kept_current`), no lens given (`no_lens_specified_kept_current`),
    or no settable aperture ladder (`no_settable_aperture_kept_current`). `aperture` is the value
    to WRITE, or None to leave the body untouched.
    """
    current_f = _parse_f_number(current_aperture)
    decision = {
        "reason": None,
        "note": None,                        # plain-language explanation for the dry-run reader
        "aperture": None,                    # value to write; None => keep current, write nothing
        "shutter": None,                     # compensating shutter to write, set with the aperture
        "iso": None,                         # compensating ISO to write, set with the aperture
        "compensation": None,
        "f_number": current_f,
        "current_aperture": current_aperture,
        "sweet_spot_f": None,
        "lens_model": lens_model,
        "lens_id": None,
        "lens_profiled": False,
        "sensor": None,
        "settable_apertures": [str(opt) for opt in (aperture_options or [])],
    }
    kept = f"the current aperture ({current_aperture}) will be kept and no setting is changed"
    if not lens_model:
        decision["reason"] = APERTURE_REASON_NO_LENS
        decision["note"] = f"No --lens given, so no sharpness profile applies; {kept}."
        return decision
    rig = _resolve_named_rig(body_model, lens_model)
    decision["lens_id"] = rig.lens_id
    decision["lens_profiled"] = bool(rig.lens_profiled)
    optics = (_PROFILED_LENS_OPTICS.get(rig_profile.normalize_lens(rig.lens_id))
              if rig.lens_profiled else None)
    if optics is None or rig.sensor is None:
        decision["reason"] = APERTURE_REASON_UNKNOWN_LENS
        decision["note"] = (f"{rig.lens_id or lens_model} has no measured aperture-sharpness profile "
                            f"on this body; {kept}.")
        return decision
    sweet = optics.optimal_aperture(rig.sensor)
    decision["sweet_spot_f"] = sweet
    decision["sensor"] = rig.sensor.name
    label, number = _snap_to_ladder(sweet, aperture_options)
    if label is None:
        decision["reason"] = APERTURE_REASON_NO_LADDER
        decision["note"] = f"The body reports no settable aperture ladder; {kept}."
        return decision
    decision["aperture"] = label
    decision["f_number"] = number
    decision["reason"] = APERTURE_REASON_SWEET_SPOT
    return decision


def _nearest_in_stops(target: float, values) -> float:
    return min(values, key=lambda value: (abs(math.log2(value / target)), value))


def compensate_exposure(*, from_aperture, to_aperture, shutter, iso, caps,
                        iso_ceiling: int) -> dict:
    """Match the metered exposure after an aperture change. Pure and camera-free.

    `caps` is a camera_caps.CameraCapabilities already bounded to the enabled shutter envelope
    (camera_caps.constrain_capture_duration). Shutter absorbs the stop difference first, snapped to
    its ladder; ISO absorbs any remainder, snapped to its ladder at or under `iso_ceiling` (the
    current ISO stays allowed). `feasible` is False when the residual exceeds
    EXPOSURE_MATCH_TOLERANCE_STOPS or a current value cannot be read as a number.
    """
    from_f, to_f = _parse_f_number(from_aperture), _parse_f_number(to_aperture)
    result = {"feasible": False, "stops_needed": None, "residual_stops": None,
              "from": {"aperture": from_aperture, "shutter": shutter, "iso": iso},
              "to": None, "tolerance_stops": EXPOSURE_MATCH_TOLERANCE_STOPS,
              "max_shutter_s": max(caps.shutters), "iso_ceiling": iso_ceiling, "error": None}
    try:
        shutter_s = camera_caps._shutter_seconds(str(shutter).strip())
        iso_value = int(str(iso).strip())
    except (TypeError, ValueError, ZeroDivisionError):
        result["error"] = f"current shutter {shutter!r} / ISO {iso!r} is not a fixed numeric value"
        return result
    if from_f is None or to_f is None or not shutter_s > 0 or iso_value <= 0:
        result["error"] = f"cannot compare apertures {from_aperture!r} -> {to_aperture!r}"
        return result
    stops_needed = 2.0 * math.log2(to_f / from_f)  # positive: the new aperture admits less light
    new_shutter_s = _nearest_in_stops(shutter_s * 2 ** stops_needed, caps.shutters)
    remainder = stops_needed - math.log2(new_shutter_s / shutter_s)
    iso_ladder = sorted({value for value in caps.isos if value <= iso_ceiling} | {iso_value})
    new_iso = _nearest_in_stops(iso_value * 2 ** remainder, iso_ladder)
    residual = remainder - math.log2(new_iso / iso_value)
    result.update({
        "stops_needed": stops_needed, "residual_stops": residual,
        "feasible": abs(residual) <= EXPOSURE_MATCH_TOLERANCE_STOPS + 1e-9,
        "to": {"aperture": to_aperture,
               "shutter": caps.shutter_labels[caps.shutters.index(new_shutter_s)],
               "iso": str(new_iso)},
    })
    return result


def resolve_bracket_aperture(camera, *, body_model, lens_model=None,
                             max_shutter_s: float | None = None, baseline: dict | None = None) -> dict:
    """Read-only: read the current exposure and settable ladders, then choose (no writes).

    When the chosen aperture differs from the current one, compensating shutter/ISO values are
    planned with `compensate_exposure`; if the metered exposure cannot be matched the aperture is
    kept (`exposure_infeasible_kept_current`). Records the ISO/aperture/shutter found under `held`
    — `run_bracket` verifies every frame against what it wrote and restores these afterwards.

    `baseline` is a metered baseline (`metered_baseline`): the decision then starts from its
    `metered` exposure instead of the body's ("current" in the reasons means the metered value),
    always writes an aperture, shutter and ISO (the sweet spot with its compensation, else the
    metered exposure itself), and `held` — the restore target — is its `inherited` pre-meter exposure.
    """
    from sonyremote.camera import read_settings_checked
    # A placeholder read (f/0, ISO 0, 65535/65535) must never become the held/restore target.
    current, reread = read_settings_checked(camera, purpose="focus bracket held settings")
    if baseline is None:
        decision = _decide_bracket_exposure(camera, current, body_model=body_model, lens_model=lens_model,
                                            max_shutter_s=max_shutter_s)
        decision["held"] = {key: current.get(key) for key in HELD_KEYS}
    else:
        _check_metered_baseline(baseline)
        metered = {key: baseline["metered"][key] for key in HELD_KEYS}
        decision = _decide_bracket_exposure(camera, {**current, **metered}, body_model=body_model,
                                            lens_model=lens_model, max_shutter_s=max_shutter_s)
        decision["held"] = {key: baseline["inherited"][key] for key in HELD_KEYS}
        decision["baseline_source"] = "metered"
        if decision["reason"] != APERTURE_REASON_SWEET_SPOT:
            # Unknown lens, no lens, no ladder or an infeasible sweet spot: the metered exposure.
            decision.update(aperture=metered["aperture"], shutter=metered["shutter"], iso=metered["iso"],
                            f_number=_parse_f_number(metered["aperture"]),
                            note=f"{decision['note'].split('; ')[0]}; the bracket uses the metered "
                                 f"{metered['aperture']} with shutter {metered['shutter']} and ISO "
                                 f"{metered['iso']}, and restores the pre-meter exposure afterwards.")
    if reread:
        decision["held_read"] = reread
    return decision


def _check_metered_baseline(baseline: dict) -> None:
    if not isinstance(baseline, dict) or baseline.get("source") != "metered" or any(
            not (baseline.get(part) or {}).get(key) for part in ("metered", "inherited") for key in HELD_KEYS):
        raise ValueError("a metered bracket baseline must carry its metered and inherited ISO, aperture and shutter")


def metered_baseline(meter: dict) -> dict:
    """The bracket's baseline record from `engine.RealEngine.meter_baseline`, shaped like the
    /shoot Fixed stack's `evidence.stack_policy.baseline` (JSON-safe; paths become strings)."""
    record = {"source": "metered", "inherited": meter["inherited"], "metered": meter["metered"],
              "recommendation_source": meter.get("recommendation_source"),
              "meter_passes": meter.get("meter_passes"), "meter_reliability": meter.get("meter_reliability"),
              "meter_physical_exposures": meter["physical_exposure_count"],
              "meter_restore": meter.get("meter_restore")}
    return json.loads(json.dumps(record, default=str))


def _decide_bracket_exposure(camera, current: dict, *, body_model, lens_model,
                             max_shutter_s: float | None) -> dict:
    """The aperture decision and its shutter/ISO compensation, starting from `current`."""
    options = camera.list_options("aperture")
    decision = choose_bracket_aperture(
        body_model=body_model, lens_model=lens_model,
        current_aperture=current.get("aperture"), aperture_options=options)
    if decision["reason"] != APERTURE_REASON_SWEET_SPOT:
        return decision
    if decision["f_number"] == _parse_f_number(current.get("aperture")):
        decision["compensation"] = {"feasible": True, "stops_needed": 0.0, "residual_stops": 0.0,
                                    "note": "already at the chosen aperture; nothing to compensate"}
        decision["shutter"], decision["iso"] = current.get("shutter"), current.get("iso")
        return decision
    if max_shutter_s is None:
        from sonyremote.camera import SONY_ACTION_MAX_ENABLED_SHUTTER_S
        max_shutter_s = SONY_ACTION_MAX_ENABLED_SHUTTER_S
    rig = _resolve_named_rig(body_model, lens_model)
    iso_ceiling, ceiling_source = night_assist._iso_ceiling(rig)
    try:
        caps = camera_caps.from_live_choices(str(body_model), camera.list_options("iso"), options,
                                             camera.list_options("shutter"))
        caps, _limits = camera_caps.constrain_capture_duration(caps, max_shutter_s=max_shutter_s)
        compensation = compensate_exposure(
            from_aperture=current.get("aperture"), to_aperture=decision["aperture"],
            shutter=current.get("shutter"), iso=current.get("iso"), caps=caps, iso_ceiling=iso_ceiling)
    except ValueError as exc:
        compensation = {"feasible": False, "error": str(exc)}
    compensation["iso_ceiling_source"] = ceiling_source
    decision["compensation"] = compensation
    if not compensation["feasible"]:
        decision.update(reason=APERTURE_REASON_EXPOSURE_INFEASIBLE, aperture=None,
                        f_number=_parse_f_number(current.get("aperture")),
                        note=(f"The sweet spot f/{decision['sweet_spot_f']} could not keep the metered "
                              f"exposure within {EXPOSURE_MATCH_TOLERANCE_STOPS:.2f} stop using shutter "
                              f"<= {max_shutter_s:g} s and ISO <= {iso_ceiling}; the current aperture "
                              f"({current.get('aperture')}) will be kept and no setting is changed."))
        return decision
    decision["shutter"], decision["iso"] = compensation["to"]["shutter"], compensation["to"]["iso"]
    return decision


def preview_sharpness(path: str | Path, center_fraction: float = SHARPNESS_CENTER_FRACTION) -> float:
    """Variance of the Laplacian over the central crop of a preview, at native preview size.

    Relative only: it compares frames of the same scene/lens/exposure, matching the
    `lapvar_center` metric in the 2026-09-15 experiment receipt.
    """
    gray = np.asarray(open_rgb(path).convert("L"), dtype="float64")
    height, width = gray.shape
    if height < 8 or width < 8:
        return 0.0
    crop_h, crop_w = max(8, int(height * center_fraction)), max(8, int(width * center_fraction))
    y0, x0 = (height - crop_h) // 2, (width - crop_w) // 2
    center = gray[y0:y0 + crop_h, x0:x0 + crop_w]
    return float(cv2.Laplacian(center, cv2.CV_64F).var())


def preview_tile_sharpness(path: str | Path, rows: int = TILE_ROWS, cols: int = TILE_COLS) -> list[list[float]]:
    """Variance of the Laplacian per tile of a `rows` x `cols` grid over the whole preview.

    The centre metric only sees the middle of the frame; a scene whose middle is one near object
    needs the other tiles to show where the rest of the scene comes into focus. Relative only.
    """
    gray = np.asarray(open_rgb(path).convert("L"), dtype="float64")
    height, width = gray.shape
    if height < 2 * rows or width < 2 * cols:
        return [[0.0] * cols for _ in range(rows)]
    laplacian = cv2.Laplacian(gray, cv2.CV_64F)
    return [[float(laplacian[row * height // rows:(row + 1) * height // rows,
                             col * width // cols:(col + 1) * width // cols].var())
             for col in range(cols)] for row in range(rows)]


def tile_noise_floor(value: float) -> float:
    return max(TILE_NOISE_ABS, TILE_NOISE_FRACTION * abs(value))


def tile_has_signal(values) -> bool:
    """True when a tile's samples hold a clear focus change above the noise floor (see constants)."""
    values = list(values)
    if not values:
        return False
    low, high = min(values), max(values)
    return high >= TILE_PEAK_CONTRAST * low and high - low >= TILE_MIN_RANGE_FLOORS * tile_noise_floor(high)


def preview_mean_luma(path: str | Path) -> float:
    """Mean 8-bit luma of a preview; a saturated (or black) liveview makes sharpness meaningless."""
    gray = np.asarray(open_rgb(path).convert("L"), dtype="float64")
    return float(gray.mean()) if gray.size else 0.0


def step_commands(delta: int, step: int) -> list[int]:
    """A move of `delta` cumulative commands as repeated writes of `sign * step`, never other sizes.

    `delta` must be a multiple of `step`; a move that is not cannot be reproduced with the
    commands the sweep measured, so it is refused rather than approximated.
    """
    if isinstance(delta, bool) or not isinstance(delta, int):
        raise ValueError("focus move must be an int number of commands")
    if isinstance(step, bool) or not isinstance(step, int) or not MIN_STEP <= step <= MAX_STEP:
        raise ValueError(f"step must be an int in {MIN_STEP}..{MAX_STEP}")
    if delta % step:
        raise ValueError(f"move of {delta} commands is not a multiple of the sweep step {step}")
    sign = 1 if delta > 0 else -1
    return [sign * step] * (abs(delta) // step)


def plan_bracket(frames: int, step: int, direction: str) -> dict:
    """K frames; after each frame but the last, one `manualfocus=signed_step` command is sent."""
    if isinstance(frames, bool) or not isinstance(frames, int) or not MIN_FRAMES <= frames <= MAX_FRAMES:
        raise ValueError(f"frames must be an int in {MIN_FRAMES}..{MAX_FRAMES}")
    if isinstance(step, bool) or not isinstance(step, int) or not MIN_STEP <= step <= MAX_STEP:
        raise ValueError(f"step must be an int in {MIN_STEP}..{MAX_STEP}")
    if direction not in DIRECTIONS:
        raise ValueError(f"direction must be one of {DIRECTIONS}")
    signed = DIRECTION_SIGN[direction] * step
    positions = []
    for index in range(frames):
        positions.append({
            "index": index,
            "cumulative_offset": signed * index,
            "command_after": signed if index < frames - 1 else None,
        })
    return {
        "frames": frames, "step": step, "direction": direction, "signed_step": signed,
        "positions": positions,
        "commands": [signed] * (frames - 1),
        "calibration": CALIBRATION_NOTE,
        "sign_convention": "near=-, far=+ (libgphoto2 Sony NearFar); optical sign unverified on this lens",
    }


def _sweep_curve(sweep: dict) -> tuple[int, dict[int, float]]:
    """(step, cumulative offset from start -> best sharpness sampled there).

    Every sample counts (start, near walk, far walk); the far walk re-samples the near offsets
    and the better reading wins. Only offsets that are multiples of `step` are usable positions.
    """
    if not isinstance(sweep, dict) or sweep.get("schema") not in SWEEP_SCHEMAS:
        raise ValueError(f"sweep must be a {' or '.join(SWEEP_SCHEMAS)} document")
    step = sweep.get("step")
    if isinstance(step, bool) or not isinstance(step, int) or not MIN_STEP <= step <= MAX_STEP:
        raise ValueError(f"sweep lacks an int step in {MIN_STEP}..{MAX_STEP}")
    curve: dict[int, float] = {}
    for point in sweep.get("curve") or []:
        offset, sharpness = point.get("offset"), point.get("sharpness")
        if isinstance(offset, bool) or not isinstance(offset, int) or not isinstance(sharpness, (int, float)) \
                or not math.isfinite(sharpness):
            raise ValueError(f"sweep curve point {point!r} lacks an int offset and finite sharpness")
        if offset % step:
            raise ValueError(f"sweep sample at offset {offset} is not a multiple of step {step}")
        curve[offset] = max(curve.get(offset, float("-inf")), float(sharpness))
    if len(curve) < 2:
        raise ValueError("sweep holds fewer than two sampled offsets; nothing to plan across")
    return step, curve


def _sweep_tiles(sweep: dict, step: int) -> tuple[dict | None, dict[str, dict[int, float]]]:
    """(tile_grid, tile id 'rRcC' -> {offset -> best sharpness there}) of a focus_sweep/2 document.

    A focus_sweep/1 document has no tiles: (None, {}). A /2 document must carry a grid on every
    sample; a malformed grid is refused rather than planned around.
    """
    if sweep.get("schema") == "focus_sweep/1":
        return None, {}
    grid = sweep.get("tile_grid")
    rows, cols = (grid.get("rows"), grid.get("cols")) if isinstance(grid, dict) else (None, None)
    if any(isinstance(n, bool) or not isinstance(n, int) or not 1 <= n <= 16 for n in (rows, cols)):
        raise ValueError(f"{SWEEP_SCHEMA} sweep lacks a tile_grid with int rows/cols in 1..16")
    tiles: dict[str, dict[int, float]] = {f"r{row}c{col}": {} for row in range(rows) for col in range(cols)}
    for point in sweep.get("curve") or []:
        grid_values = point.get("tiles")
        if not isinstance(grid_values, list) or len(grid_values) != rows \
                or any(not isinstance(line, list) or len(line) != cols for line in grid_values):
            raise ValueError(f"sweep point at offset {point.get('offset')} lacks a {rows}x{cols} tile grid")
        for row, line in enumerate(grid_values):
            for col, value in enumerate(line):
                if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) \
                        or value < 0:
                    raise ValueError(f"sweep point at offset {point.get('offset')} has a non-finite or "
                                     "negative tile value (a variance is never negative)")
                curve = tiles[f"r{row}c{col}"]
                curve[point["offset"]] = max(curve.get(point["offset"], float("-inf")), float(value))
    return {"rows": rows, "cols": cols, "metric": grid.get("metric"),
            "tile_ids": "rRcC = row R from the top, column C from the left, both 0-based"}, tiles


def _normalised(curve: dict[int, float]) -> dict[int, float]:
    """Each sample as a fraction of the way from the curve's minimum to its peak (0..1)."""
    low, high = min(curve.values()), max(curve.values())
    if high <= low:
        return {offset: 1.0 for offset in curve}
    return {offset: (value - low) / (high - low) for offset, value in curve.items()}


def _band_around_peak(norm: dict[int, float], peak_offset: int, coverage: float, step: int) -> list[int]:
    """The contiguous run of sampled offsets around the peak whose normalised sharpness >= coverage."""
    low = high = peak_offset
    while low - step in norm and norm[low - step] >= coverage:
        low -= step
    while high + step in norm and norm[high + step] >= coverage:
        high += step
    return [low, high]


def _plan_across_tiles(curve: dict[int, float], tiles: dict[str, dict[int, float]], grid: dict,
                       frames: int, coverage: float, step: int, peak_offset: int) -> dict:
    """Place up to `frames` positions so every tile with a focus peak is covered, near to far.

    Targets are the centre curve plus every tile with a clear peak (`tile_has_signal`). Each target's
    samples are normalised from its own minimum (0) to its peak (1): a defocused tile keeps texture
    and noise energy, so a fraction of the peak alone would call almost any frame "covered". Frames
    are chosen greedily from sampled offsets: first the centre peak, then each next frame is the
    offset that brings the most targets to `coverage`, then (ties) most raises the worst-covered
    target, then the larger total, then the smaller move from the start. A frame made redundant by
    later ones is dropped and its slot re-spent, and a frame is moved when that strictly improves
    (targets covered, worst, total). This is a best-effort search, not a proof of optimal coverage.
    It stops early only when no remaining offset improves any target. A target is
    covered when some frame reaches `coverage` on its normalised curve; the plan lists the uncovered
    ones and the ones whose peak sits at an end of the sweep (their true peak may lie beyond it).
    """
    targets = {"centre": curve}
    ignored = []
    for tile_id, tile_curve in tiles.items():
        if tile_has_signal(tile_curve.values()):
            targets[tile_id] = tile_curve
        else:
            ignored.append(tile_id)
    norms = {target: _normalised(values) for target, values in targets.items()}
    sampled = sorted(curve)

    def reached(frames_at: list[int]) -> dict[str, float]:
        return {target: max(norms[target][offset] for offset in frames_at) for target in targets}

    def quality(frames_at: list[int]) -> tuple:
        values = list(reached(frames_at).values())
        return sum(value >= coverage for value in values), round(min(values), 9), round(sum(values), 9)

    chosen = [peak_offset]
    for _ in range(4 * len(sampled)):  # bounded; each pass below strictly improves or stops
        while len(chosen) < frames:
            best = reached(chosen)
            remaining = [offset for offset in sampled if offset not in chosen]
            if not remaining:
                break

            def score(offset: int) -> tuple:
                values = [max(best[target], norms[target][offset]) for target in targets]
                covered = sum(value >= coverage for value in values)
                return covered, min(values), sum(values), -abs(offset), -offset
            pick = max(remaining, key=score)
            if len(chosen) >= MIN_FRAMES and score(pick)[2] <= sum(best.values()) + 1e-12:
                break  # every target is already as sharp as any remaining offset can make it
            chosen.append(pick)
        # Greedy picks can settle on a compromise: move one frame (never the centre peak) to any
        # other sampled offset when that strictly improves (targets covered, worst, total).
        swapped = False
        for slot, offset in enumerate(chosen):
            if offset == peak_offset:
                continue
            current = quality(chosen)
            for candidate in sampled:
                if candidate in chosen:
                    continue
                trial = chosen[:slot] + [candidate] + chosen[slot + 1:]
                if quality(trial) > current:
                    chosen, swapped = trial, True
                    break
            if swapped:
                break
        if swapped:
            continue
        # A frame made redundant by later ones is dropped (never the centre peak, never below
        # MIN_FRAMES) and the loop spends the slot where it helps.
        full = reached(chosen)
        redundant = next((offset for offset in reversed(chosen) if offset != peak_offset
                          and len(chosen) > MIN_FRAMES
                          and reached([other for other in chosen if other != offset]) == full), None)
        if redundant is None:
            break
        chosen.remove(redundant)
    if len(chosen) < MIN_FRAMES:
        raise ValueError(f"sweep spans only {len(sampled)} step positions; cannot place {MIN_FRAMES} distinct frames")
    offsets = sorted(chosen)
    near_end, far_end = sampled[0], sampled[-1]
    report = []
    for target, values in targets.items():
        target_peak = max(sorted(values), key=lambda offset: values[offset])
        # An end sample within noise of the peak means the curve never measurably fell on that side.
        near_to_peak = values[target_peak] - tile_noise_floor(values[target_peak])
        edge = ("near" if values[near_end] >= near_to_peak else "far" if values[far_end] >= near_to_peak else None)
        best_offset = max(offsets, key=lambda offset: (norms[target][offset], -offset))
        report.append({
            "id": target, "peak_offset": target_peak, "peak_sharpness": values[target_peak],
            "min_sharpness": min(values.values()),
            "band": _band_around_peak(norms[target], target_peak, coverage, step),
            "best_offset": best_offset, "best_coverage": norms[target][best_offset],
            "covered": norms[target][best_offset] >= coverage,
            "peak_at_sweep_edge": edge,
        })
    covers = {offset: [entry["id"] for entry in report if entry["covered"] and entry["best_offset"] == offset]
              for offset in offsets}
    positions = []
    for index, offset in enumerate(offsets):
        command_after = offsets[index + 1] - offset if index < len(offsets) - 1 else None
        positions.append({"index": index, "cumulative_offset": offset, "command_after": command_after,
                          "predicted_sharpness": curve.get(offset), "covers": covers[offset]})
    centre_norm_floor = min(curve.values())
    return {
        "source": "sweep", "method": "tiles", "frames": len(offsets), "frames_requested": frames,
        "step": step, "coverage": coverage,
        "coverage_rule": ("a target is covered when a frame's sample reaches its minimum + coverage x "
                          "(peak - minimum) on that target's own curve"),
        "threshold": centre_norm_floor + coverage * (curve[peak_offset] - centre_norm_floor),
        "peak_offset": peak_offset, "peak_sharpness": curve[peak_offset],
        "band": [min(entry["band"][0] for entry in report), max(entry["band"][1] for entry in report)],
        "tile_grid": grid, "targets": report, "ignored_tiles": ignored,
        "uncovered": [entry["id"] for entry in report if not entry["covered"]],
        "unresolved": [entry["id"] for entry in report if entry["peak_at_sweep_edge"]],
        "initial_move": offsets[0],
        "positions": positions,
        "commands": [position["command_after"] for position in positions[:-1]],
    }


def _even_indices(low: int, high: int, count: int) -> list[int]:
    # floor(x + 0.5) rather than round(): banker's rounding can collide two spaced values.
    return [int(math.floor(low + index * (high - low) / (count - 1) + 0.5)) for index in range(count)]


def plan_from_sweep(sweep: dict, frames: int, *, coverage: float = 0.5) -> dict:
    """Plan up to `frames` positions from a sweep, near -> far, always including the centre peak.

    A focus_sweep/2 sweep whose tile grid shows focus peaks outside the centre is planned across
    the scene's depth (`_plan_across_tiles`, `method: "tiles"`): every tile with a clear peak is
    covered by some frame when `frames` allows, and the plan names any tile left uncovered or whose
    peak sits at an end of the sweep. `--frames` is then a maximum.

    A focus_sweep/1 sweep, or a /2 sweep with no tile carrying a clear peak, is planned on the centre
    curve alone (`method: "centre_band"`): `frames` positions across the offsets at >= `coverage` x
    peak sharpness, evenly spaced between the near-most and far-most qualifying samples; a band
    holding fewer than `frames` samples is widened one sample at a time within the sampled range.

    Positions are sampled offsets (multiples of the sweep `step`, cumulative from the start);
    `initial_move` is the move from the start (offset 0) to the first position and every move is a
    multiple of `step`, to be issued as repeated `step`-sized commands (see SWEEP_PLAN_ASSUMPTION).
    """
    if isinstance(frames, bool) or not isinstance(frames, int) or not MIN_FRAMES <= frames <= MAX_FRAMES:
        raise ValueError(f"frames must be an int in {MIN_FRAMES}..{MAX_FRAMES}")
    if isinstance(coverage, bool) or not isinstance(coverage, (int, float)) \
            or not math.isfinite(coverage) or not 0.0 < coverage <= 1.0:
        raise ValueError("coverage must be a number in (0, 1]")
    step, curve = _sweep_curve(sweep)
    peak_offset = max(sorted(curve), key=lambda offset: curve[offset])
    peak = curve[peak_offset]
    if peak <= 0:
        raise ValueError("sweep peak sharpness is not positive; the curve carries no focus evidence")
    grid, tiles = _sweep_tiles(sweep, step)
    common = {"calibration": sweep.get("calibration", CALIBRATION_NOTE), "assumption": SWEEP_PLAN_ASSUMPTION,
              "sign_convention": "near=-, far=+ (libgphoto2 Sony NearFar); optical sign unverified on this lens"}
    if any(tile_has_signal(tile_curve.values()) for tile_curve in tiles.values()):
        return {**_plan_across_tiles(curve, tiles, grid, frames, coverage, step, peak_offset), **common}
    threshold = coverage * peak
    sampled = sorted(offset // step for offset in curve)  # step indices; contiguous for a real sweep
    qualifying = [index for index in sampled if curve[index * step] >= threshold]
    low, high = min(qualifying), max(qualifying)
    near_end, far_end = sampled[0], sampled[-1]
    while high - low + 1 < frames:
        widened = False
        if low > near_end:
            low, widened = low - 1, True
        if high - low + 1 < frames and high < far_end:
            high, widened = high + 1, True
        if not widened:
            raise ValueError(f"sweep spans only {far_end - near_end + 1} step positions; "
                             f"cannot place {frames} distinct frames")
    indices = _even_indices(low, high, frames)
    peak_index = peak_offset // step
    if peak_index not in indices:
        nearest = min(range(frames), key=lambda slot: abs(indices[slot] - peak_index))
        indices[nearest] = peak_index
    offsets = sorted({index * step for index in indices})
    assert len(offsets) == frames, "even spacing over >= frames indices cannot collide"
    positions = []
    for index, offset in enumerate(offsets):
        command_after = offsets[index + 1] - offset if index < frames - 1 else None
        positions.append({"index": index, "cumulative_offset": offset, "command_after": command_after,
                          "predicted_sharpness": curve.get(offset)})
    plan = {
        "source": "sweep", "method": "centre_band", "frames": frames, "step": step, "coverage": coverage,
        "threshold": threshold, "peak_offset": peak_offset, "peak_sharpness": peak, "band": [low * step, high * step],
        "initial_move": offsets[0],
        "positions": positions,
        "commands": [position["command_after"] for position in positions[:-1]],
        **common,
    }
    if grid is not None:
        plan["tile_grid"] = grid
        plan["ignored_tiles"] = list(tiles)  # no tile showed a focus peak: the centre curve decides
    return plan


def validate_plan(plan: dict) -> None:
    """Reject a malformed explicit plan before any camera write; every move must be a step multiple."""
    positions = plan.get("positions") if isinstance(plan, dict) else None
    if not positions or not MIN_FRAMES <= len(positions) <= MAX_FRAMES:
        raise ValueError(f"plan must hold {MIN_FRAMES}..{MAX_FRAMES} positions")
    step = plan.get("step")
    if isinstance(step, bool) or not isinstance(step, int) or not MIN_STEP <= step <= MAX_STEP:
        raise ValueError(f"plan step must be an int in {MIN_STEP}..{MAX_STEP}")
    initial = plan.get("initial_move", 0)
    if isinstance(initial, bool) or not isinstance(initial, int) or initial % step:
        raise ValueError(f"plan initial_move must be an int multiple of step {step}")
    for index, position in enumerate(positions):
        if position.get("index") != index:
            raise ValueError("plan positions must be indexed 0..n-1 in order")
        command = position.get("command_after")
        last = index == len(positions) - 1
        if last and command is not None:
            raise ValueError("no command may follow the last frame")
        if not last and (isinstance(command, bool) or not isinstance(command, int) or command == 0
                         or command % step):
            raise ValueError(f"plan position {index} needs a non-zero command_after that is a multiple of {step}")


def _file_receipt(path: Path) -> dict:
    data = Path(path).read_bytes()
    return {"path": str(path), "sha256": hashlib.sha256(data).hexdigest(), "bytes": len(data)}


def _write_manifest(dest_dir: Path, manifest: dict) -> Path:
    """Atomic manifest write: a hard kill leaves either the previous or the new manifest."""
    target = dest_dir / MANIFEST_NAME
    pending = dest_dir / (MANIFEST_NAME + ".tmp")
    pending.write_text(json.dumps(manifest, indent=2, sort_keys=True), encoding="utf-8")
    os.replace(pending, target)
    return target


def _refuse_reused_output(dest_dir: Path) -> None:
    """A bracket directory is single-use: never fire a shutter into a partial earlier run."""
    if not dest_dir.exists():
        return
    stale = [dest_dir / MANIFEST_NAME, *dest_dir.glob("preview_*.jpg"), *dest_dir.glob("frame_*")]
    present = [str(path) for path in stale if path.exists()]
    if present:
        raise FocusBracketError(
            f"{dest_dir} already holds bracket output ({present[0]}); choose a fresh --out directory. "
            "No setting was written.")


class MeterRefused(FocusBracketError):
    """The meter frame fired but gave no usable exposure; no bracket frame follows."""

    def __init__(self, detail: str, *, baseline: dict):
        self.baseline = baseline
        super().__init__(detail)


def _metered_decision(baseline: dict | None, aperture: dict | None) -> bool:
    """True for a metered baseline, which must come with the aperture decision resolved from it."""
    if baseline is None or baseline.get("source") != "metered":
        return False
    _check_metered_baseline(baseline)
    if (aperture or {}).get("baseline_source") != "metered" or not all(aperture.get(key) for key in HELD_KEYS):
        raise ValueError("a metered baseline needs the aperture decision resolved from it")
    return True


def check_bracket_route(camera, dest_dir: str | Path) -> dict:
    """Refuse, writing nothing, a body without the manualfocus route or a used `dest_dir`.

    Returns `focus_capabilities()`. `run_bracket` runs it before any write or shutter.
    """
    capabilities = camera.focus_capabilities()
    body = f"{capabilities.get('model')} at {capabilities.get('port')}"
    if not capabilities.get("manual_focus_route"):
        raise FocusBracketError(
            f"{body} exposes no writable manualfocus node; optical focus bracketing is unavailable "
            "on this body (the A7 II driver descriptor has no such node). No setting was written.")
    for node in ("focusmode",):
        if not capabilities["nodes"].get(node):
            raise FocusBracketError(f"{body} exposes no '{node}' node; cannot enter Manual focus. "
                                    "No setting was written.")
    dest_dir = Path(dest_dir)
    if dest_dir.exists() and not dest_dir.is_dir():
        raise FocusBracketError(f"{dest_dir} exists and is not a directory; choose a fresh --out directory. "
                                "No setting was written.")
    _refuse_reused_output(dest_dir)
    return capabilities


def run_bracket(camera, *, frames: int | None = None, step: int | None = None,
                direction: str | None = None, dest_dir: str | Path,
                capture: Callable[[Path], Path | None], af_hold_s: float = 1.0,
                plan: dict | None = None, aperture: dict | None = None,
                baseline: dict | None = None,
                meter: Callable[[], tuple[dict, dict]] | None = None) -> dict:
    """Shoot a focus bracket with the shutter-owning `capture(dest_dir) -> Path` callable.

    Order per frame: preview -> sharpness -> capture -> (step, except after the last frame).
    Refuses before any write when the body lacks the manualfocus route. Restores the original
    focus mode in `finally`; if that mode was an AF mode, one half-press re-acquires focus and
    the reported indication is recorded. Writes `bracket_manifest.json` in `dest_dir`.

    Either `frames/step/direction` (uniform plan) or an explicit `plan` (from `plan_from_sweep`)
    is given. An explicit plan may open with `initial_move` (sent before frame 0) and its
    per-frame moves may be any multiple of the plan's `step`; each move is issued as repeated
    `step`-sized commands (never larger chunks), settled and drained once per move.

    `aperture` is an optional decision from `resolve_bracket_aperture`: when its reason is
    `lens_sweet_spot` the chosen aperture and its compensating shutter and ISO are written once
    before frame 0 and held, and the original aperture/shutter/ISO are restored in `finally`
    (`restoration.exposure`); otherwise nothing is written. Either way ISO, aperture and shutter are
    read from the body at every frame so the manifest proves they stayed as set
    (`aperture.held_constant`).

    `baseline` is recorded as the manifest's `baseline` (the /shoot `stack_policy.baseline` shape).
    A metered one (`metered_baseline`) requires an `aperture` decision resolved from it: its
    aperture, shutter and ISO are always written before frame 0 and the inherited pre-meter
    exposure is restored in `finally`.

    `meter` (instead of `aperture`/`baseline`) is called once after the body is in Manual focus
    and before any exposure write or frame, so the meter frame neither moves focus away from the
    bracket's start position nor escapes the focus-mode restore; it fires the meter frame and
    returns `(baseline, aperture)`. Anything it raises stops the bracket before frame 0; a
    `MeterRefused` carries its `baseline` into the manifest, and the manifest's `meter_failure`
    keeps the meter's own restore receipt and fired exposures.
    """
    if plan is None:
        plan = plan_bracket(frames, step, direction)
    else:
        if frames is not None or step is not None or direction is not None:
            raise ValueError("pass either frames/step/direction or an explicit plan, not both")
        validate_plan(plan)
    if meter is not None and (aperture is not None or baseline is not None):
        raise ValueError("pass either a meter or a decided aperture/baseline, not both")
    metered = _metered_decision(baseline, aperture)
    dest_dir = Path(dest_dir)
    capabilities = check_bracket_route(camera, dest_dir)
    dest_dir.mkdir(mode=0o700, parents=True, exist_ok=True)

    manifest: dict = {
        "schema": "focus_bracket_manifest/1",
        "body": {"model": capabilities.get("model"), "port": capabilities.get("port")},
        "plan": plan,
        "calibration": CALIBRATION_NOTE,
        "sharpness_metric": "center Laplacian variance of the liveview preview; relative, uncalibrated",
        "started": time.time(),
        "original_focus_mode": None,
        "manual_mode_readback": None,
        "aperture": None,
        "baseline": baseline,
        "frames": [],
        "steps_sent": [],
        "status": "started",
        "error": None,
        "restore_error": None,
        "restoration": {"attempted": False, "focus_mode_readback": None, "af_half_press": None,
                        "error": None},
    }
    # Restored afterwards: a placeholder focus mode ("Undefined" after auto power-off, or a value
    # outside the node's choices) is re-read and, if it persists, refused here before any write.
    from sonyremote.camera import read_focus_mode_checked
    original_mode, mode_reread = read_focus_mode_checked(
        camera, purpose="focus bracket original focus mode",
        choices=(capabilities["nodes"].get("focusmode") or {}).get("choices"))
    manifest["original_focus_mode"] = original_mode
    if mode_reread:
        manifest["original_focus_mode_read"] = mode_reread

    def move(commands: list[int], label: dict, drain_name: str) -> None:
        for command in commands:
            manifest["steps_sent"].append({**label, **camera.manual_focus_step(command)})
        # The liveview stream lags a focus step (2026-09-15: the preview after a +3 step
        # was unchanged while the full frame had moved); settle and drain one stale frame.
        time.sleep(STEP_SETTLE_S)
        try:
            drained = camera.capture_preview(dest_dir / drain_name)
            manifest["steps_sent"][-1]["drained_preview"] = str(drained)
        except Exception as exc:  # the drain is optional; the measured preview is not
            manifest["steps_sent"][-1]["drained_preview"] = f"unavailable: {exc}"

    exposure_written: list[str] = []  # keys written before frame 0, in order; restored in finally

    def record_held(frame_entry: dict | None):
        # Read ISO/aperture/shutter now and record them so the manifest proves the bracket held one
        # exposure across every frame; never averaged or inferred, just read back. Returns the
        # placeholder refusal (the caller records this frame, then stops the bracket) or None.
        if aperture is None:
            return None
        from sonyremote.camera import PlaceholderSettingsError, read_settings_checked
        refused = error = None
        try:
            settings, reread = read_settings_checked(camera, purpose="focus bracket held readback")
        except PlaceholderSettingsError as exc:
            # This frame already fired: keep its record with no values; no further frame is shot.
            settings, reread, refused, error = {}, None, exc.as_dict(), exc
        subset = {key: settings.get(key) for key in HELD_KEYS}
        manifest["aperture"]["frame_settings"].append(subset)
        if frame_entry is not None:
            frame_entry["held"] = subset
            if reread:
                frame_entry["held_read"] = reread
            if refused:
                frame_entry["held_read_refused"] = refused
        return error

    try:
        # Inside try/finally so a failed Manual switch still records and restores.
        manifest["manual_mode_readback"] = camera.set_focus_mode("Manual")
        if meter is not None:
            # Focus is already frozen: the meter frame fires at the bracket's start position.
            manifest["phase"] = "meter"
            _write_manifest(dest_dir, manifest)
            baseline, aperture = meter()
            manifest["baseline"] = baseline
            metered = _metered_decision(baseline, aperture)
            manifest["phase"] = "bracket"
        if aperture is not None:
            record = dict(aperture)
            record["frame_settings"] = []
            manifest["aperture"] = record
            if (aperture.get("reason") == APERTURE_REASON_SWEET_SPOT or metered) and aperture.get("aperture"):
                # Aperture plus its compensating shutter and ISO (or the metered baseline itself),
                # written once before any frame and never touched again, so focus is the only
                # variable and the exposure stays metered.
                readbacks = {}
                for key in ("aperture", "shutter", "iso"):
                    if aperture.get(key) is None:
                        continue
                    exposure_written.append(key)
                    readbacks[key] = camera.set_setting(key, aperture[key])
                record["set_readback"] = readbacks
                record["expected"] = {key: readbacks.get(key, (aperture.get("held") or {}).get(key))
                                      for key in HELD_KEYS}
            else:
                record["set_readback"] = None  # kept current: nothing written
                record["expected"] = aperture.get("held")
        _write_manifest(dest_dir, manifest)
        if plan.get("initial_move"):
            move(step_commands(plan["initial_move"], plan["step"]), {"before_frame": 0}, "preview_00_stale.jpg")
            _write_manifest(dest_dir, manifest)
        for position in plan["positions"]:
            index = position["index"]
            preview_path = camera.capture_preview(dest_dir / f"preview_{index:02d}.jpg")
            preview_receipt = _file_receipt(preview_path)
            preview_receipt["sharpness"] = preview_sharpness(preview_path)
            frame_dir = dest_dir / f"frame_{index:02d}"
            frame_dir.mkdir(mode=0o700, exist_ok=True)
            # A body answering placeholders (65535/65535 reads as 1 s) refuses here, before this
            # frame's shutter, with or without a managed aperture.
            from sonyremote.camera import read_settings_checked
            _, pre_capture_read = read_settings_checked(camera, purpose=f"before bracket frame {index}")
            captured = capture(frame_dir)  # NOT idempotent; an uncertain outcome propagates.
            if captured is None:
                raise FocusBracketError(f"capture for frame {index} returned no file")
            frame_entry = {
                "index": index, "cumulative_offset": position["cumulative_offset"],
                "preview": preview_receipt, "capture": _file_receipt(Path(captured)),
                "capture_receipt": getattr(camera, "last_capture_receipt", None),
            }
            if pre_capture_read:
                frame_entry["pre_capture_read"] = pre_capture_read
            held_refusal = record_held(frame_entry)  # prove aperture/ISO/shutter held for this frame
            manifest["frames"].append(frame_entry)
            _write_manifest(dest_dir, manifest)  # progress survives a hard kill mid-bracket
            if held_refusal is not None:
                raise held_refusal  # the body answers placeholders: stop before another shutter
            if position["command_after"] is not None:
                # Uniform plans send their single signed step; explicit plans repeat step-sized commands.
                commands = (step_commands(position["command_after"], plan["step"]) if plan.get("source") == "sweep"
                            else [position["command_after"]])
                move(commands, {"after_frame": index}, f"preview_{index + 1:02d}_stale.jpg")
        if aperture is not None:
            seen = manifest["aperture"]["frame_settings"]
            expected = manifest["aperture"].get("expected")
            identical = all(len({frame.get(key) for frame in seen}) <= 1 for key in HELD_KEYS)
            as_written = expected is None or all(frame == expected for frame in seen)
            manifest["aperture"]["held_constant"] = (identical and as_written) if seen else None
        manifest["status"] = "complete"
    except BaseException as exc:
        manifest["status"] = "failed"
        manifest["error"] = f"{type(exc).__name__}: {exc}"
        if manifest.get("phase") == "meter":
            # No bracket frame fired. What the meter fired and restored, for the receipt.
            # A failure after a completed meter (e.g. the exposure decision) carries its baseline.
            if getattr(exc, "baseline", None) is not None:
                manifest["baseline"] = exc.baseline
            metered_record = manifest.get("baseline") or {}
            fired = getattr(exc, "prior_exposures", None) or metered_record.get("meter_physical_exposures")
            manifest["meter_failure"] = json.loads(json.dumps({
                "meter_restore": getattr(exc, "meter_restore", None) or metered_record.get("meter_restore"),
                "meter_exposures_fired": fired,
                "may_have_fired": bool(getattr(exc, "may_have_fired", False) or fired)}, default=str))
        raise
    finally:
        restoration = manifest["restoration"]
        restoration["attempted"] = True
        # A Ctrl-C during one restore step is recorded like a failure; the remaining steps still
        # run and it is re-raised once the manifest is written.
        interrupted: list[BaseException] = []
        try:
            if getattr(camera, "_camera", object()) is None and hasattr(camera, "connect"):
                camera.connect()  # a capture reconnect may have failed; restoration reconnects first
            restoration["focus_mode_readback"] = camera.set_focus_mode(original_mode)
        except BaseException as exc:  # never mask the capture outcome with a restoration failure
            interrupted += [] if isinstance(exc, Exception) else [exc]
            restoration["error"] = f"{type(exc).__name__}: {exc}"
        if original_mode != "Manual" and capabilities["nodes"].get("autofocus") \
                and capabilities["nodes"].get("focusindication"):
            try:  # attempted even when the focus-mode write failed; each failure is recorded
                restoration["af_half_press"] = {"hold_s": af_hold_s,
                                                "indication": camera.autofocus_half_press(af_hold_s)}
            except BaseException as exc:
                interrupted += [] if isinstance(exc, Exception) else [exc]
                af_error = f"af_half_press: {type(exc).__name__}: {exc}"
                restoration["af_half_press"] = {"error": af_error}
                restoration["error"] = "; ".join(filter(None, (restoration["error"], af_error)))
        elif original_mode != "Manual":
            restoration["af_half_press"] = {"skipped": "autofocus/focusindication node absent"}
        if exposure_written:
            # Put back the metered aperture/shutter/ISO found before the bracket, in its own try so a
            # focus-mode failure never leaves the body at the bracket's aperture.
            original = (aperture or {}).get("held") or {}
            exposure = {"attempted": list(exposure_written), "readback": {}, "error": None}
            restoration["exposure"] = exposure
            failures = []
            for key in exposure_written:  # each key independently: one failure never skips the rest
                try:
                    if original.get(key) is None:
                        raise FocusBracketError(f"original {key} was not read; cannot restore it")
                    exposure["readback"][key] = camera.set_setting(key, original[key])
                except BaseException as exc:  # recorded and surfaced; never masks the capture outcome
                    interrupted += [] if isinstance(exc, Exception) else [exc]
                    failures.append(f"{key}: {type(exc).__name__}: {exc}")
            if failures:
                exposure["error"] = "; ".join(failures)
                restoration["error"] = "; ".join(filter(None, (restoration["error"],
                                                               f"exposure restore: {exposure['error']}")))
        if restoration["error"]:
            manifest["restore_error"] = restoration["error"]
            if manifest["status"] == "complete":
                manifest["status"] = "complete_restoration_failed"
        manifest["finished"] = time.time()
        manifest["manifest_path"] = str(_write_manifest(dest_dir, manifest))
        if interrupted:
            raise interrupted[0]
    return manifest
