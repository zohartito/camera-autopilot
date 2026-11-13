#!/usr/bin/env python3
"""Shutter-free focus sweep on the A7 III: measure a scene-relative sharpness curve over the
libgphoto2 manualfocus route so a bracket can be planned from measured positions.

    .venv/bin/python tools/focus_sweep.py --model "Sony Alpha-A7 III (PC Control)" \\
        --port "usb:000,004" --serial SERIAL --step 3 --out DIR [--preview-iso 400 --preview-shutter 1/60]

What it does (no shutter, ever): records focus mode / ISO / shutter; sets a mid-tone preview
exposure (shutter adjusted one stop at a time, bounded, until the liveview mean luma sits in
40..190); half-presses AF once when the body is in an AF mode so the start is the AF-locked
position; enters Manual focus; measures center Laplacian variance of the liveview at the start,
plus the same metric on every tile of a 3x4 grid over the whole liveview (so a bracket can cover
objects outside the centre), walks toward NEAR in `--step` commands until two consecutive changes
fall under the noise floor on a defocus plateau (stop reason `plateau`) or 12 commands (`cap`),
then walks toward FAR across the whole range with the same rule and a 24-command cap; if a tile
with a clear focus signal is still getting sharper where the far walk ended, the walk continues
far one step command at a time while it keeps rising; after a far walk that ended on its cap it
also crosses flat gaps until a tile has come into focus and passed its peak and three more samples
showed no other tile rising. Either extension
stops after 12 more commands, or when the whole image stops changing for two steps
(`far_extension`); then it returns to the sharpest centre sample by sending
the exact inverse of every command issued since it, in the same step-sized commands, and verifies
with a fresh measurement (a mismatch is recorded, not hidden); finally restores focus mode (AF
half-press if it was AF), ISO and shutter, each attempted even when another fails, and reads
everything back; a failed restoration makes the status `complete_restoration_failed` (with a
top-level `restore_error`) and the exit code 1. A focus mode read as "Undefined" or outside the
node's choices (a body just woken from auto power-off) is re-read like the f/0 / ISO 0 / 65535/65535
placeholders and refuses before any write if it persists. Writes `DIR/sweep.json`
(`focus_sweep/2`: `focus_sweep/1` plus `tile_grid`, per-sample `tiles` and `far_extension`) for
`tools/focus_bracket_capture.py --sweep`, which still reads `focus_sweep/1` files.

Offsets are cumulative commands from the start, never distances. Every move is made only of
`--step`-sized commands: the live sweep of 2026-09-15 (`focus-sweep-03`) reproduced the same
curve with magnitude-3 commands in both directions, while a route of +-7 chunks landed at 16 % of
peak — the manualfocus value is a step-size class with a non-linear per-magnitude response. A
walk that stops on a flat metric is a defocus plateau, not a mechanical stop; `from_near_rail`
on far-walk samples is informational only (commands since the near walk ended). The return to the
peak sends its inverse commands one at a time, measuring after each like the walks, and stops early
at a sample that matches the peak within the noise floor (`return_check.walk`, `stop_reason`): a
whole route sent back to back missed the peak on every live sweep. Requires
exclusive ownership of the body and its USB serial (`--serial`, from tools/inspect_usb_camera.py --list):
the sweep writes focus mode, ISO and shutter, so the body at the port is proven by serial before
any write, and a mismatch refuses with nothing written. There is no dry-run mode.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import sys
import time
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_REPO_ROOT / "backend"))
sys.path.insert(0, str(_REPO_ROOT))

from PIL import Image  # noqa: E402
from focus_bracket import (  # noqa: E402
    FocusBracketError, MAX_STEP, MIN_STEP, STEP_SETTLE_S as _BRACKET_SETTLE_S, SWEEP_SCHEMA, TILE_COLS,
    TILE_METRIC, TILE_ROWS, preview_mean_luma, preview_sharpness, preview_tile_sharpness, tile_has_signal,
    tile_noise_floor,
)
from sonyremote.camera import (  # noqa: E402
    Camera, CameraError, CameraIdentityError, read_focus_mode_checked, read_settings_checked,
)

SWEEP_NAME = "sweep.json"
STEP_SETTLE_S = _BRACKET_SETTLE_S  # tests set this to 0; the only sleep in this module
NEAR_CAP, FAR_CAP = 12, 24
# The far extension: at most this many more far commands, one step command at a time. After a far
# walk that ended on its command cap (the centre was still changing, so the lens was still moving)
# it explores: a far subject still fully defocused at the cap looks flat, so the walk crosses flat
# gaps until a tile has come into focus and passed its peak AND a further FAR_QUIET_STEPS samples
# showed no other tile rising (a second, farther subject may still be ahead), or the cap. After a far walk that
# ended on a plateau it only follows a tile that is still rising (a flat metric could be a rail).
# Either way it stops when the whole image stops changing for two steps (a rail absorbing commands).
FAR_EXTENSION_CAP = 12
FAR_QUIET_STEPS = 3
NOISE_FLOOR_FRACTION = 0.03  # liveview metric repeatability was ~1-2 % at rest (2026-09-15)
NOISE_FLOOR_ABS = 0.5
FLAT_STEPS_FOR_RAIL = 2
PLATEAU_FRACTION = 0.5  # a defocus plateau sits well below the sharpest point seen on the walk
RETURN_TOLERANCE_FRACTION = 0.10
LUMA_BAND = (40.0, 190.0)
MAX_EXPOSURE_TRIES = 4
MAX_PREVIEW_SHUTTER_S = 1.0
CALIBRATION_NOTE = "scene-relative sharpness curve; offsets are cumulative commands, not distances"
EXIF_LENS_MODEL = 0xA434


class FocusSweepError(FocusBracketError):
    """A refusal before, or a failure during, a sweep (never a shutter outcome)."""


def _settle() -> None:
    if STEP_SETTLE_S > 0:
        time.sleep(STEP_SETTLE_S)


def shutter_seconds(label: str) -> float | None:
    """'1/60' -> 1/60, '2' or '2"' -> 2.0; None for Bulb or anything unparsable."""
    text = str(label).strip().rstrip('"').replace("s", "")
    try:
        if "/" in text:
            numerator, denominator = text.split("/", 1)
            value = float(numerator) / float(denominator)
        else:
            value = float(text)
    except (ValueError, ZeroDivisionError):
        return None
    return value if math.isfinite(value) and value > 0 else None


def shutter_one_stop(options: list[str], current: str, *, brighter: bool) -> str | None:
    """The option nearest one stop from `current` in the requested direction, within the preview bound."""
    now = shutter_seconds(current)
    if now is None:
        return None
    target = now * 2.0 if brighter else now / 2.0
    candidates = []
    for option in options:
        seconds = shutter_seconds(option)
        if seconds is None or option == current or seconds > MAX_PREVIEW_SHUTTER_S:
            continue
        if (brighter and seconds <= now) or (not brighter and seconds >= now):
            continue
        candidates.append((abs(math.log2(seconds / target)), option))
    return min(candidates)[1] if candidates else None


def _file_receipt(path: Path) -> dict:
    data = Path(path).read_bytes()
    return {"path": str(path), "sha256": hashlib.sha256(data).hexdigest(), "bytes": len(data)}


def _write_sweep(dest_dir: Path, sweep: dict) -> Path:
    target = dest_dir / SWEEP_NAME
    pending = dest_dir / (SWEEP_NAME + ".tmp")
    pending.write_text(json.dumps(sweep, indent=2, sort_keys=True), encoding="utf-8")
    os.replace(pending, target)
    return target


def _refuse_reused_output(dest_dir: Path) -> None:
    if not dest_dir.exists():
        return
    stale = [dest_dir / SWEEP_NAME, *dest_dir.glob("preview_*.jpg")]
    present = [str(path) for path in stale if path.exists()]
    if present:
        raise FocusSweepError(f"{dest_dir} already holds sweep output ({present[0]}); choose a fresh "
                              "--out directory. No setting was written.")


def _lens_from_exif(path: Path) -> str | None:
    try:
        value = Image.open(path).getexif().get(EXIF_LENS_MODEL)
    except Exception:
        return None
    return str(value) if value else None


def _flat(previous: float, current: float) -> bool:
    floor = max(NOISE_FLOOR_ABS, NOISE_FLOOR_FRACTION * max(abs(previous), abs(current)))
    return abs(current - previous) < floor


def _beyond_noise(high: float, low: float) -> bool:
    """`high` exceeds `low` by more than the noise of both samples."""
    return high - low > tile_noise_floor(high) + tile_noise_floor(low)


def tile_is_rising(series: list[float]) -> bool:
    """A tile with a clear focus signal whose latest sample is its sharpest and still climbing.

    The climb is measured over FLAT_STEPS_FOR_RAIL steps and must exceed the noise of both compared
    samples, so jitter on a plateau (even after a real rise) does not count.
    """
    if len(series) <= FLAT_STEPS_FOR_RAIL or not tile_has_signal(series):
        return False
    last = series[-1]
    return last >= max(series) - tile_noise_floor(last) and _beyond_noise(last, series[-1 - FLAT_STEPS_FOR_RAIL])


def tile_passed_peak(series: list[float], since: int) -> bool:
    """Since sample `since`, the tile rose above its value just before it and then fell off again."""
    extension = series[since:]
    if not extension or since < 1 or not tile_has_signal(series):
        return False
    top = max(extension)
    return _beyond_noise(top, series[since - 1]) and _beyond_noise(top, extension[-1])


def run_sweep(camera, *, step: int, dest_dir: str | Path, preview_iso: str = "400",
              preview_shutter: str = "1/60", af_hold_s: float = 1.0) -> dict:
    """Measure the sharpness curve with a duck-typed `Camera`; never calls any capture method."""
    if isinstance(step, bool) or not isinstance(step, int) or not MIN_STEP <= step <= MAX_STEP:
        raise ValueError(f"step must be an int in {MIN_STEP}..{MAX_STEP}")
    dest_dir = Path(dest_dir)
    capabilities = camera.focus_capabilities()
    body = f"{capabilities.get('model')} at {capabilities.get('port')}"
    if not capabilities.get("manual_focus_route"):
        raise FocusSweepError(f"{body} exposes no writable manualfocus node; a focus sweep is unavailable "
                              "on this body. No setting was written.")
    if not capabilities["nodes"].get("focusmode"):
        raise FocusSweepError(f"{body} exposes no 'focusmode' node; cannot enter Manual focus. "
                              "No setting was written.")
    _refuse_reused_output(dest_dir)
    dest_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    af_available = bool(capabilities["nodes"].get("autofocus") and capabilities["nodes"].get("focusindication"))

    sweep: dict = {
        "schema": SWEEP_SCHEMA,
        "body": {"model": capabilities.get("model"), "port": capabilities.get("port")},
        "lens": None,
        "step": step,
        "shutter_fired": False,
        "started": time.time(),
        "original": {"focus_mode": None, "iso": None, "shutter": None},
        "start_af": None,
        "preview_exposure": {"iso": preview_iso, "shutter": preview_shutter, "tries": [],
                             "luma_band": list(LUMA_BAND), "mean_luma": None},
        "manual_mode_readback": None,
        "sharpness_metric": "center Laplacian variance of the liveview preview; relative, uncalibrated",
        "noise_floor": {"fraction": NOISE_FLOOR_FRACTION, "abs": NOISE_FLOOR_ABS,
                        "flat_steps_for_plateau": FLAT_STEPS_FOR_RAIL, "plateau_fraction": PLATEAU_FRACTION},
        "tile_grid": {"rows": TILE_ROWS, "cols": TILE_COLS, "metric": TILE_METRIC},
        "curve": [],
        "near_walk": None, "far_walk": None, "far_extension": None,
        "near_rail_offset": None, "far_rail_offset": None,
        "peak_offset": None, "peak_from_near_rail": None, "peak_sharpness": None,
        "return_check": None, "return_verified": None,
        "restoration": {"attempted": False, "focus_mode_readback": None, "af_half_press": None,
                        "iso_readback": None, "shutter_readback": None, "readback": None,
                        "matches_original": None, "failed": [], "error": None},
        "calibration": CALIBRATION_NOTE,
        "reference_note": ("near_rail_offset/far_rail_offset are where each walk stopped (plateau or cap), "
                           "not mechanical stops; from_near_rail is informational. Plans use cumulative "
                           "offsets from the start and step-sized commands only"),
        "status": "started", "error": None, "restore_error": None,
    }
    previews = 0
    offset = 0  # cumulative commands from the start position
    sent: list[int] = []  # every manualfocus command since the start, in order

    def preview(name: str) -> Path:
        nonlocal previews
        path = camera.capture_preview(dest_dir / f"preview_{previews:02d}_{name}.jpg")
        previews += 1
        return path

    def measure(label: str, walk: str | None) -> dict:
        path = preview(label)
        point = {"label": label, "walk": walk, "offset": offset, "from_near_rail": None,
                 "commands_before": len(sent), "sharpness": preview_sharpness(path),
                 "tiles": preview_tile_sharpness(path), "preview": _file_receipt(path)}
        if sweep["lens"] is None:
            sweep["lens"] = _lens_from_exif(path)
        return point

    def record(point: dict) -> None:
        sweep["curve"].append(point)
        _write_sweep(dest_dir, sweep)

    def move_then_measure(commands: list[int], label: str, walk: str) -> dict:
        """Send step-sized commands only (each is +-step), then settle, drain and measure."""
        nonlocal offset
        for command in commands:
            assert abs(command) == step, "sweep moves are made of step-sized commands only"
            camera.manual_focus_step(command)
            sent.append(command)
            offset += command
        _settle()
        try:
            preview("stale")  # the liveview lags a focus step: drain one frame before measuring
        except Exception as exc:  # the drain is optional; the measured preview is not
            sweep.setdefault("drain_failures", []).append(f"{label}: {exc}")
        return measure(label, walk)

    def walk(direction: int, cap: int, name: str) -> dict:
        """Step in one direction until flat on a plateau for FLAT_STEPS_FOR_RAIL steps or `cap`."""
        previous = sweep["curve"][-1]["sharpness"]  # the walk starts where the last measurement was
        best = previous
        flat_run, commands, reason = 0, 0, "cap"
        while commands < cap:
            point = move_then_measure([direction * step], f"{name}{commands + 1:02d}", name)
            commands += 1
            if name == "far":
                point["from_near_rail"] = offset - sweep["near_rail_offset"]
            record(point)
            current = point["sharpness"]
            best = max(best, current)
            flat_run = flat_run + 1 if _flat(previous, current) else 0
            previous = current
            if flat_run >= FLAT_STEPS_FOR_RAIL and current < PLATEAU_FRACTION * best:
                reason = "plateau"  # a flat metric, never a claim of a mechanical stop
                break
        return {"commands": commands, "step": direction * step, "stop_reason": reason, "end_offset": offset}

    def tile_series() -> dict[str, list[float]]:
        return {f"r{row}c{col}": [point["tiles"][row][col] for point in sweep["curve"]]
                for row in range(TILE_ROWS) for col in range(TILE_COLS)}

    def rising_tiles() -> list[str]:
        return [tile for tile, series in tile_series().items() if tile_is_rising(series)]

    def passed_peak(since: int) -> list[str]:
        return [tile for tile, series in tile_series().items() if tile_passed_peak(series, since)]

    def image_static(before: dict, after: dict) -> bool:
        """Centre and every tile unchanged within noise: the lens may be on a rail."""
        if not _flat(before["sharpness"], after["sharpness"]):
            return False
        return all(abs(a - b) < tile_noise_floor(max(abs(a), abs(b)))
                   for row_a, row_b in zip(before["tiles"], after["tiles"]) for a, b in zip(row_a, row_b))

    def extend_far(explore: bool) -> dict:
        """Walk far one step command at a time (see FAR_EXTENSION_CAP); bounded by the cap."""
        since = len(sweep["curve"])  # index of the first extension sample
        rising = rising_tiles()
        extension = {"commands": 0, "step": step, "cap": FAR_EXTENSION_CAP, "explore": explore,
                     "rising_at_far_walk_end": rising, "stop_reason": "cap", "passed_peak": [],
                     "rising_at_stop": [], "end_offset": offset}
        static_run = quiet = 0  # quiet: samples since a peak was passed in which no tile was rising
        while extension["commands"] < FAR_EXTENSION_CAP:
            passed = passed_peak(since)
            extension["passed_peak"] = passed
            if not rising and not explore:
                extension["stop_reason"] = "no_tile_rising"
                break
            # One passed peak is not the far end of the scene: stop only after a further quiet
            # stretch in which no other tile started to rise.
            if not rising and passed and quiet >= FAR_QUIET_STEPS:
                extension["stop_reason"] = "passed_peak"
                break
            before = sweep["curve"][-1]
            point = move_then_measure([step], f"farx{extension['commands'] + 1:02d}", "far_extension")
            extension["commands"] += 1
            point["from_near_rail"] = offset - sweep["near_rail_offset"]
            record(point)
            static_run = static_run + 1 if image_static(before, point) else 0
            rising = rising_tiles()
            # Counted only once some tile has passed its peak (the first falling sample counts as one),
            # so a broad, flat-topped peak cannot use up the quiet stretch before it is passed.
            quiet = 0 if rising or not passed_peak(since) else quiet + 1
            if static_run >= FLAT_STEPS_FOR_RAIL:
                extension["stop_reason"] = "image_static"  # possibly a rail; never a claim of one
                break
        extension["rising_at_stop"], extension["end_offset"] = rising, offset
        extension["passed_peak"] = passed_peak(since)
        return extension

    def return_to_peak(peak: dict) -> dict:
        """Walk back toward the peak by the exact inverse of every command sent since it, ONE
        step-sized command at a time with a settled, drained measurement after each (the cadence the
        curve was recorded with), and stop at the first sample whose sharpness matches the peak
        within the noise floor, or when the inverse route is used up (never more commands than it).

        Every live return of 2026-09-15 and 2026-09-29 sent the whole route back to back and missed
        (ratio 0.09-0.20): two of them landed exactly on the value measured 9 commands from the far
        end, so commands sent back to back did not all move the lens. Each sample is kept beside the
        outbound sample at the same offset, so a mismatch shows where the route stopped reproducing.
        Kept out of `curve`: the return verifies the walk-back, it is not a sweep sample.
        """
        route = [-command for command in reversed(sent[peak["commands_before"]:])]
        walk_back: list[dict] = []
        check, stop_reason = None, "at_peak"
        for index, command in enumerate(route):
            outbound = next((point["sharpness"] for point in reversed(sweep["curve"])
                             if point["offset"] == offset + command), None)
            check = move_then_measure([command], f"return{index + 1:02d}", "return")
            walk_back.append({"offset": offset, "sharpness": check["sharpness"], "outbound_sharpness": outbound,
                              "preview": check["preview"]})
            _write_sweep(dest_dir, sweep)
            stop_reason = "route_end"
            if not _beyond_noise(peak["sharpness"], check["sharpness"]):
                stop_reason = "matched_peak"
                break
        if check is None:
            check = measure("return", "return")
        ratio = check["sharpness"] / peak["sharpness"] if peak["sharpness"] > 0 else 0.0
        return {"method": ("exact inverse of the commands sent since the peak sample, one step command at a "
                           "time with a settled measurement after each; stops at the first sample matching "
                           "the peak within the noise floor"),
                "commands_planned": len(route), "commands_sent": route[:len(walk_back)],
                "stop_reason": stop_reason, "walk": walk_back, "offset": offset,
                "measured_sharpness": check["sharpness"], "peak_sharpness": peak["sharpness"],
                "ratio": ratio, "tolerance_fraction": RETURN_TOLERANCE_FRACTION, "preview": check["preview"]}

    # Restored afterwards: never keep a placeholder read (focus mode "Undefined" or outside the node's
    # choices, f/0, ISO 0, 65535/65535) as the original. Either refusal comes before any write.
    original_mode, mode_reread = read_focus_mode_checked(
        camera, purpose="focus sweep original focus mode",
        choices=(capabilities["nodes"].get("focusmode") or {}).get("choices"))
    before, before_reread = read_settings_checked(camera, purpose="focus sweep original settings")
    sweep["original"] = {"focus_mode": original_mode, "iso": before.get("iso"), "shutter": before.get("shutter")}
    if mode_reread:
        sweep["original_focus_mode_read"] = mode_reread
    if before_reread:
        sweep["original_read"] = before_reread
    try:
        exposure = sweep["preview_exposure"]
        exposure["iso_readback"] = camera.set_setting("iso", str(preview_iso))
        shutter = str(preview_shutter)
        exposure["shutter_readback"] = camera.set_setting("shutter", shutter)
        for attempt in range(MAX_EXPOSURE_TRIES):
            path = preview("exposure")
            luma = preview_mean_luma(path)
            exposure["tries"].append({"shutter": shutter, "mean_luma": luma, "preview": _file_receipt(path)})
            if LUMA_BAND[0] <= luma <= LUMA_BAND[1]:
                exposure["mean_luma"] = luma
                break
            if attempt + 1 == MAX_EXPOSURE_TRIES:
                raise FocusSweepError(f"liveview mean luma {luma:.1f} stayed outside {LUMA_BAND} after "
                                      f"{MAX_EXPOSURE_TRIES} shutter tries; sharpness would be meaningless")
            candidate = shutter_one_stop(camera.list_options("shutter"), shutter, brighter=luma < LUMA_BAND[0])
            if candidate is None:
                raise FocusSweepError(f"no shutter option one stop from {shutter} within {MAX_PREVIEW_SHUTTER_S}s; "
                                      f"liveview mean luma {luma:.1f} is outside {LUMA_BAND}")
            shutter = candidate
            exposure["shutter_readback"] = camera.set_setting("shutter", shutter)
        exposure["shutter"] = shutter
        _write_sweep(dest_dir, sweep)

        if original_mode != "Manual":
            sweep["start_af"] = ({"hold_s": af_hold_s, "indication": camera.autofocus_half_press(af_hold_s)}
                                 if af_available else {"skipped": "autofocus/focusindication node absent"})
        sweep["manual_mode_readback"] = camera.set_focus_mode("Manual")
        _settle()
        preview("stale")
        record(measure("start", None))

        sweep["near_walk"] = walk(-1, NEAR_CAP, "near")
        sweep["near_rail_offset"] = sweep["near_walk"]["end_offset"]
        sweep["curve"][-1]["from_near_rail"] = 0
        sweep["far_walk"] = walk(+1, FAR_CAP, "far")
        sweep["far_extension"] = extend_far(explore=sweep["far_walk"]["stop_reason"] == "cap")
        sweep["far_rail_offset"] = sweep["far_extension"]["end_offset"]

        peak = max(sweep["curve"], key=lambda point: point["sharpness"])  # first sample wins a tie
        sweep["peak_offset"], sweep["peak_sharpness"] = peak["offset"], peak["sharpness"]
        sweep["peak_from_near_rail"] = peak["from_near_rail"]
        sweep["return_check"] = return_to_peak(peak)
        # One-sided, like the walk-back's stop rule: a return sample sharper than the peak sample is
        # at least as well focused (the peak is one noisy reading), never a failed return.
        sweep["return_verified"] = sweep["return_check"]["ratio"] >= 1.0 - RETURN_TOLERANCE_FRACTION
        sweep["status"] = "complete"
    except BaseException as exc:
        sweep["status"] = "failed"
        sweep["error"] = f"{type(exc).__name__}: {exc}"
        raise
    finally:
        interrupted = _restore(camera, sweep, original_mode, before, af_available, af_hold_s)
        sweep["finished"] = time.time()
        sweep["sweep_path"] = str(_write_sweep(dest_dir, sweep))
        if interrupted is not None:
            raise interrupted  # a Ctrl-C during restoration, re-raised once every field was attempted
    return sweep


def _restore(camera, sweep: dict, original_mode: str, before: dict, af_available: bool,
             af_hold_s: float) -> BaseException | None:
    """Put back focus mode (AF half-press if it was AF), ISO and shutter, then read them back.

    Best effort per field: each is attempted even when an earlier one failed (2026-09-29: a failed
    focus-mode write used to skip the ISO and shutter restore), and every failure is recorded.
    Never raises, so the sweep outcome is never masked; a failure marks the sweep
    `complete_restoration_failed` (when it had completed) and sets the top-level `restore_error`.
    An interruption (Ctrl-C) during one field is recorded like a failure, the remaining fields are
    still attempted, and it is returned for the caller to re-raise after writing sweep.json.
    """
    restoration = sweep["restoration"]
    restoration["attempted"] = True
    failed: list[str] = restoration["failed"]
    errors: list[str] = []
    interrupted: list[BaseException] = []

    def attempt(field: str, action):
        try:
            return action()
        except BaseException as exc:  # recorded; the remaining fields are still attempted
            if not isinstance(exc, Exception):
                interrupted.append(exc)
            failed.append(field)
            errors.append(f"{field}: {type(exc).__name__}: {exc}")
            return None

    restoration["focus_mode_readback"] = attempt("focus_mode", lambda: camera.set_focus_mode(original_mode))
    if original_mode != "Manual":
        restoration["af_half_press"] = (
            attempt("af_half_press", lambda: {"hold_s": af_hold_s, "indication": camera.autofocus_half_press(af_hold_s)})
            if af_available else {"skipped": "autofocus/focusindication node absent"})
    if before.get("iso") is not None:
        restoration["iso_readback"] = attempt("iso", lambda: camera.set_setting("iso", str(before["iso"])))
    if before.get("shutter") is not None:
        restoration["shutter_readback"] = attempt("shutter", lambda: camera.set_setting("shutter", str(before["shutter"])))

    def read_back() -> dict:
        after, _ = read_settings_checked(camera, purpose="focus sweep restore readback")
        after["focus_mode"] = camera.get_focus_mode()
        return after

    after = attempt("readback", read_back)
    if after is not None:
        restoration["readback"] = after
        restoration["matches_original"] = all(
            str(after.get(key)) == str(sweep["original"][key]) for key in ("focus_mode", "iso", "shutter"))
    restoration["error"] = "; ".join(errors) or None
    if restoration["error"] or restoration["matches_original"] is False:
        sweep["restore_error"] = restoration["error"] or (
            f"readback {({key: restoration['readback'].get(key) for key in ('focus_mode', 'iso', 'shutter')})} "
            f"does not match the original {sweep['original']}")
        if sweep["status"] == "complete":
            sweep["status"] = "complete_restoration_failed"
    return interrupted[0] if interrupted else None


def _af_hold(text: str) -> float:
    value = float(text)
    if not 0 < value <= 3:
        raise argparse.ArgumentTypeError("--af-hold must be in (0, 3] seconds")
    return value


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--model", required=True, help="exact gphoto2 model, e.g. 'Sony Alpha-A7 III (PC Control)'")
    parser.add_argument("--port", required=True, help="exact USB port, e.g. usb:000,004 (re-detect first)")
    parser.add_argument("--serial", help="the body's USB serial (printed by tools/inspect_usb_camera.py --list); "
                                         "required, and proven at the port before any setting write")
    parser.add_argument("--step", type=int, default=3, help=f"manualfocus units per sweep step, {MIN_STEP}..{MAX_STEP}")
    parser.add_argument("--out", required=True, type=Path)
    parser.add_argument("--preview-iso", default="400", help="ISO for the liveview measurement (restored afterwards)")
    parser.add_argument("--preview-shutter", default="1/60", help="starting shutter for the liveview measurement")
    parser.add_argument("--af-hold", type=_af_hold, default=1.0, help="AF half-press hold in (0, 3] s")
    args = parser.parse_args(argv)
    if not MIN_STEP <= args.step <= MAX_STEP:
        print(json.dumps({"status": "invalid", "error": f"--step must be in {MIN_STEP}..{MAX_STEP}"}, indent=2))
        return 2
    if not args.serial:
        # The sweep writes focus mode, ISO and shutter; USB ports re-enumerate, so a same-model body
        # could hold this port. Write only to a body proven by its serial.
        print(json.dumps({"status": "invalid", "error": "--serial is required: the sweep writes focus mode, "
                                                        "ISO and shutter (the port alone is not an identity)"}, indent=2))
        return 2
    try:
        with Camera(model=args.model, port=args.port, serial=args.serial) as camera:
            sweep = run_sweep(camera, step=args.step, dest_dir=args.out, preview_iso=args.preview_iso,
                              preview_shutter=args.preview_shutter, af_hold_s=args.af_hold)
    except CameraIdentityError as exc:
        # Terminal: a connect to an unproven body refuses before any write; never retried.
        print(json.dumps({"status": "identity_refused", "identity_refusal": exc.as_dict(),
                          "sweep": str(args.out / SWEEP_NAME)}, indent=2))
        return 1
    except (CameraError, FocusBracketError) as exc:
        print(json.dumps({"status": "failed", "error": str(exc), "sweep": str(args.out / SWEEP_NAME)}, indent=2))
        return 1
    summary = {key: sweep[key] for key in ("status", "sweep_path", "near_rail_offset", "far_rail_offset",
                                           "peak_offset", "peak_from_near_rail", "peak_sharpness",
                                           "return_verified", "restoration", "restore_error")}
    summary["points"] = len(sweep["curve"])
    summary["walks"] = {"near": sweep["near_walk"], "far": sweep["far_walk"], "far_extension": sweep["far_extension"]}
    summary["preview_exposure"] = {key: sweep["preview_exposure"][key] for key in ("iso", "shutter", "mean_luma")}
    if sweep["restoration"].get("error") or sweep["restoration"].get("matches_original") is False:
        summary["status"] = "complete_restoration_failed"
    print(json.dumps(summary, indent=2))
    return 0 if summary["status"] == "complete" else 1


if __name__ == "__main__":
    raise SystemExit(main())
