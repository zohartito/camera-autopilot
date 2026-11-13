#!/usr/bin/env python3
"""Shoot a bounded focus bracket on the A7 III over the libgphoto2 manualfocus route and merge it.

    .venv/bin/python tools/focus_bracket_capture.py --model "Sony Alpha-A7 III (PC Control)" \
        --port "usb:000,004" --frames 3 --step 3 --direction far --out DIR [--dry-run]

With `--sweep DIR/sweep.json` (from `tools/focus_sweep.py`) the plan comes from
`backend.focus_bracket.plan_from_sweep`, always including the centre peak; `--step`/`--direction`
are not used. A `focus_sweep/2` sweep whose tile grid shows focus peaks gets up to `--frames`
positions chosen (best effort) to bring each such tile within `--coverage` of its own sampled peak,
near to far; the plan (and `--dry-run`) lists each tile's peak, the frame that covers it, and any
tile left uncovered or peaking at an end of the sweep. One tile can hold several depths; a tile
curve is evidence about the tile, not proof that every object in it is sharp. A `/2` sweep with no
tile showing a peak is planned like `/1`. A `focus_sweep/1` sweep (centre curve only)
gets `--frames` positions across the offsets whose sharpness is >= `--coverage` x peak. The
bracket then assumes the body is at the sweep's start position (same scene, AF-locked).

Exposure (`--baseline metered`, the default): the body may still hold whatever exposure the last
capture left (1 Oct 2026: a night ISO 2500 · 1/30 · f/4 made a daylight bracket ~4.6 stops over).
So after the route and `--out` checks and with focus already frozen in Manual (the meter frame
cannot refocus away from the bracket's start position), ONE meter frame goes through the /shoot
Fixed-stack metering path (`engine.RealEngine.meter_baseline` on this connection, bounded re-meter of
a black or saturated frame included; up to 1 + `remeter.MAX_REMETER_PASSES` meter exposures), the
bracket fires at the metered ISO/aperture/shutter, and the pre-meter exposure (and focus mode) is
restored afterwards. An uncertain meter frame is never retried and no bracket frame follows; a meter that stays
unreliable after the re-meter is refused before any bracket frame. The meter needs the mode dial
at M. `--baseline current` keeps the old behaviour: no meter frame, the body's exposure as found.
Either way the manifest's `baseline` records the inherited and metered exposure.

With `--lens` (the mounted lens), a lens that has a measured sharpness profile gets the bracket's
aperture at its sweet spot, with shutter (<= the enabled 1.0 s envelope) then ISO compensating so
the metered exposure is kept; all three are restored afterwards. A lens without a profile, or an
exposure that cannot be matched within 1/3 stop, keeps the metered aperture (with `--baseline
current`, the current one, and writes nothing).

`--dry-run` connects read-only, prints `focus_capabilities()`, the inherited exposure, whether a
meter frame would fire and the exposure budget, the aperture decision (with a plain
`aperture_note`) and the plan, and exits without writing any setting or firing the shutter. Without it the bracket runs (`backend/focus_bracket.run_bracket`, using the
existing `Camera.capture` single-exposure path for every frame), then the accepted frames are
merged with `backend.stacking.focus_stack` into `DIR/focus_stack.jpg` beside `DIR/focus_stack.json`.

Limits: A7 III only (the A7 II driver has no manualfocus node and the tool refuses before any
write); steps are uncalibrated relative lens adjusts with asymmetric rail behaviour; preview
sharpness is relative to this scene and lens. Requires exclusive ownership of the body: stop the
server and any handheld listener first. USB ports move across reconnects; re-detect before use.
"""
from __future__ import annotations

import argparse
import contextlib
import hashlib
import json
import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_REPO_ROOT / "backend"))
sys.path.insert(0, str(_REPO_ROOT))

import cv2  # noqa: E402
import remeter  # noqa: E402
from focus_bracket import (  # noqa: E402
    CALIBRATION_NOTE, DIRECTIONS, HELD_KEYS, FocusBracketError, MAX_FRAMES, MAX_STEP, MIN_FRAMES,
    MIN_STEP, MeterRefused, metered_baseline, plan_bracket, plan_from_sweep, resolve_bracket_aperture,
    run_bracket,
)
from stacking import focus_stack  # noqa: E402
import rig_profile  # noqa: E402
from sonyremote.camera import (  # noqa: E402
    Camera, CameraError, CaptureOutcomeUncertain, read_settings_checked,
)

BASELINES = ("metered", "current")

STACK_NAME = "focus_stack.jpg"
STACK_RECEIPT_NAME = "focus_stack.json"


def _sha256(path: Path) -> dict:
    data = path.read_bytes()
    return {"path": str(path), "sha256": hashlib.sha256(data).hexdigest(), "bytes": len(data)}


def _merge(manifest: dict, out_dir: Path) -> dict:
    inputs = [{**frame["capture"], "preview_sharpness": frame["preview"]["sharpness"],
               "cumulative_offset": frame["cumulative_offset"]} for frame in manifest["frames"]]
    merged = focus_stack([entry["path"] for entry in inputs])
    output = out_dir / STACK_NAME
    if not cv2.imwrite(str(output), merged, [cv2.IMWRITE_JPEG_QUALITY, 95]):
        raise FocusBracketError(f"could not write {output}")
    receipt = {
        "schema": "focus_stack_receipt/1",
        "method": "backend.stacking.focus_stack (bounded sharpest-tile selection)",
        "inputs": inputs, "output": _sha256(output),
        "bracket_manifest": manifest.get("manifest_path"),
        "calibration": manifest.get("plan", {}).get("calibration", CALIBRATION_NOTE),
        "evidence": "merged detail is not proof of distinct optical planes; compare frames by eye",
    }
    (out_dir / STACK_RECEIPT_NAME).write_text(json.dumps(receipt, indent=2, sort_keys=True), encoding="utf-8")
    return receipt


def _plan_note(plan: dict) -> str | None:
    """One plain sentence on what a tiled sweep plan covers; None for other plans."""
    if plan.get("method") != "tiles":
        return None
    offsets = ", ".join(str(position["cumulative_offset"]) for position in plan["positions"])
    note = [f"{plan['frames']} frames at offsets {offsets} (of up to {plan['frames_requested']})."]
    focusing = len(plan["targets"]) - 1
    note.append(f"The centre and {focusing} focusing tile(s) were targeted; {len(plan['ignored_tiles'])} flat "
                "tile(s) carry no focus evidence and were ignored.")
    note.append(f"Not covered: {', '.join(plan['uncovered'])}." if plan["uncovered"]
                else "Every target reaches the coverage threshold in some frame.")
    if plan["unresolved"]:
        note.append(f"Peaking at an end of the sweep (true peak may lie beyond it): {', '.join(plan['unresolved'])}.")
    return " ".join(note)


def _af_hold(text: str) -> float:
    value = float(text)
    if not 0 < value <= 3:
        raise argparse.ArgumentTypeError("--af-hold must be in (0, 3] seconds")
    return value


def exposure_budget(bracket_frames: int, baseline: str) -> dict:
    """Most physical exposures this run may make (same keys as capture_policy.exposure_budget)."""
    meter = 1 if baseline == "metered" else 0
    extra = remeter.MAX_REMETER_PASSES if meter else 0
    return {"baseline_meter_exposures": meter, "extra_meter_exposures": extra,
            "bracket_frames": bracket_frames, "max_physical_exposures": meter + extra + bracket_frames}


def _meter(camera, args) -> dict:
    """One meter frame through the /shoot Fixed-stack metering path, on this proven connection.

    `engine.RealEngine.meter_baseline` restores the pre-meter exposure before it returns and on
    every failure it can still write to; an uncertain outcome is raised, never retried."""
    import engine

    class _ThisConnection(engine.RealEngine):
        def _camera(self):
            return contextlib.nullcontext(camera)  # the tool owns (and later closes) the connection

    return _ThisConnection(args.model, args.port, args.serial).meter_baseline(None)


def _meter_failure(exc: Exception) -> dict:
    """What a failed, refused or uncertain meter left behind; no bracket frame was issued."""
    return {"phase": "meter", "bracket_frames_fired": 0, "retry_safe": False,
            "meter_restore": getattr(exc, "meter_restore", None),
            "meter_exposures_fired": getattr(exc, "prior_exposures", None),
            "baseline": getattr(exc, "baseline", None),
            **({"refusal": exc.as_dict()} if callable(getattr(exc, "as_dict", None)) else {})}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--model", required=True, help="exact gphoto2 model, e.g. 'Sony Alpha-A7 III (PC Control)'")
    parser.add_argument("--port", required=True, help="exact USB port, e.g. usb:000,004 (re-detect first)")
    parser.add_argument("--serial", help="the body's USB serial (tools/inspect_usb_camera.py); required unless "
                                         "--dry-run, and proven at the port before any write or shutter")
    parser.add_argument("--frames", type=int, default=3,
                        help=f"{MIN_FRAMES}..{MAX_FRAMES}; with a tiled --sweep, the most frames the plan may use")
    parser.add_argument("--step", type=int, default=3, help=f"manualfocus units per frame, {MIN_STEP}..{MAX_STEP}")
    parser.add_argument("--direction", choices=DIRECTIONS, help="required without --sweep")
    parser.add_argument("--sweep", type=Path,
                        help="plan from a focus_sweep/1 or /2 sweep.json instead of step/direction")
    parser.add_argument("--coverage", type=float, default=0.5,
                        help="with --sweep, (0, 1]: /1 spans offsets whose sharpness is >= coverage x peak; "
                             "/2 needs each focusing tile to reach coverage of the way from its minimum to its peak")
    parser.add_argument("--lens", help="mounted lens; a profiled lens gets the bracket's aperture at its "
                                       "sweet spot. Accepted (case-insensitive; the aperture as F2.8, f2.8, "
                                       "f/2.8 or F/2.8; 'mm' optional): "
                                       + "; ".join(f"'{name}'" for name in rig_profile.PROFILED_TAMRON_SPELLINGS)
                                       + ". Any other lens keeps the metered aperture (with --baseline current, "
                                         "the current one)")
    parser.add_argument("--baseline", choices=BASELINES, default="metered",
                        help="metered (default): one meter frame sets the bracket's exposure and the pre-meter "
                             "exposure is restored afterwards; current: no meter frame, the body's exposure as found")
    parser.add_argument("--out", required=True, type=Path)
    parser.add_argument("--af-hold", type=_af_hold, default=1.0, help="half-press hold when restoring an AF mode (0 < s <= 3)")
    parser.add_argument("--dry-run", action="store_true", help="read capabilities and print the plan; write nothing to the camera")
    args = parser.parse_args(argv)

    try:
        if args.sweep is not None:
            sweep = json.loads(args.sweep.read_text(encoding="utf-8"))
            plan = plan_from_sweep(sweep, args.frames, coverage=args.coverage)
            plan["sweep_path"] = str(args.sweep)
            run_args = {"plan": plan}
        else:
            if args.direction is None:
                raise ValueError("--direction is required without --sweep")
            plan = plan_bracket(args.frames, args.step, args.direction)
            run_args = {"frames": args.frames, "step": args.step, "direction": args.direction}
        if not args.dry_run and not args.serial:
            # USB ports re-enumerate; a same-model body could hold this port. Fire only a proven body.
            raise ValueError("--serial is required to fire the shutter (the port alone is not an identity)")
    except (ValueError, OSError) as exc:
        print(json.dumps({"status": "invalid", "error": str(exc)}, indent=2))
        return 2
    out_dir: Path = args.out
    metered = args.baseline == "metered"
    budget = exposure_budget(len(plan["positions"]), args.baseline)

    if args.dry_run:
        try:
            with Camera(model=args.model, port=args.port) as camera:
                capabilities = camera.focus_capabilities()
                inherited, _ = read_settings_checked(camera, purpose="focus bracket dry-run inherited exposure")
                shooting_mode = camera.shooting_mode() if callable(getattr(camera, "shooting_mode", None)) else None
                aperture = (resolve_bracket_aperture(camera, body_model=args.model, lens_model=args.lens)
                            if args.lens else None)
        except CameraError as exc:
            print(json.dumps({"status": "failed", "dry_run": True, "error": str(exc)}, indent=2))
            return 1
        if metered:
            sweet = aperture is not None and aperture.get("reason") == "lens_sweet_spot"
            aperture_note = (
                "The bracket's exposure comes from the meter frame, so it is not known before it fires: "
                + (f"it will use {aperture['aperture']} (the lens's sweet spot) with shutter and ISO compensated "
                   "from the metered exposure if that match is within 1/3 stop, else the metered aperture, "
                   if sweet else "it will use the metered aperture, ")
                + "shutter and ISO, hold them, and restore the pre-meter exposure afterwards. Any values in "
                  "`aperture` were computed from the body's current exposure, which the meter replaces.")
            meter_note = ("One meter frame fires first, with focus already held in Manual, through the "
                          f"/shoot metering path (up to "
                          f"{budget['extra_meter_exposures']} more if it is black or saturated), then "
                          f"{budget['bracket_frames']} bracket frames: at most {budget['max_physical_exposures']} "
                          "exposures. A meter that stays unreliable refuses before any bracket frame.")
            if shooting_mode not in (None, "M"):
                meter_note += f" The mode dial reads {shooting_mode!r}: the meter refuses unless it is at M."
        else:
            aperture_note = (aperture["note"] if aperture and aperture.get("note") else
                             "No --lens given; the bracket does not manage aperture, shutter or ISO."
                             if aperture is None else
                             f"The bracket will set {aperture['aperture']} with shutter {aperture['shutter']} "
                             f"and ISO {aperture['iso']}, hold them, and restore the originals afterwards.")
            meter_note = (f"No meter frame (--baseline current): {budget['bracket_frames']} bracket frames at "
                          "the exposure the body holds now.")
        print(json.dumps({"status": "dry_run", "dry_run": True, "setting_writes": 0, "shutter_fired": 0,
                          "capabilities": capabilities, "baseline": args.baseline, "meter_frame": metered,
                          "meter_note": meter_note, "exposure_budget": budget,
                          "inherited": {key: inherited.get(key) for key in HELD_KEYS},
                          "shooting_mode": shooting_mode, "aperture_note": aperture_note,
                          "aperture": aperture, "plan_note": _plan_note(plan), "plan": plan,
                          "out": str(out_dir)}, indent=2))
        return 0 if capabilities.get("manual_focus_route") else 3

    state = {"phase": "setup", "baseline": None}

    def meter_then_decide(camera):
        # run_bracket calls this with the body already in Manual focus, before any exposure write.
        def meter_step():
            state["phase"] = "meter"
            pre_meter, _ = read_settings_checked(camera, purpose="focus bracket pre-meter exposure")
            try:
                meter = _meter(camera, args)
            except BaseException as exc:
                if not isinstance(exc, Exception):
                    # meter_baseline restores on every Exception; an interrupt (Ctrl-C mid re-meter)
                    # would leave its exposure behind. Put the pre-meter exposure back, never a shutter.
                    import engine
                    exc.meter_restore = engine._restore_exposure(camera, {key: pre_meter.get(key) for key in HELD_KEYS})
                    exc.may_have_fired = True
                if getattr(exc, "baseline", None) is None:
                    # The manifest keeps what the body held even when no metered exposure exists.
                    exc.baseline = json.loads(json.dumps({
                        "source": "metered", "metered": None, "meter_failed": True,
                        "inherited": {key: pre_meter.get(key) for key in HELD_KEYS},
                        "meter_restore": getattr(exc, "meter_restore", None),
                        "meter_physical_exposures": getattr(exc, "prior_exposures", None)}, default=str))
                    state["baseline"] = exc.baseline
                raise
            baseline = state["baseline"] = metered_baseline(meter)
            reliability = meter.get("meter_reliability") or {}
            if reliability.get("status") == "low":
                # Black or saturated beyond the bounded re-meter: its exposure is only a bound.
                restore = baseline.get("meter_restore") or {}
                restored = ("the pre-meter exposure was restored"
                            if restore.get("status") in ("completed", "unchanged") else
                            f"the pre-meter exposure could not be restored ({restore.get('reason')})")
                raise MeterRefused(f"The meter frame stayed unreliable ({reliability.get('reason')}) after "
                                   f"{len(baseline.get('meter_passes') or []) - 1} re-meter pass(es); no bracket "
                                   f"frame was fired and {restored}.", baseline=baseline)
            state["phase"] = "bracket"
            try:
                aperture = resolve_bracket_aperture(camera, body_model=args.model, lens_model=args.lens,
                                                    baseline=baseline)
            except BaseException as exc:  # an interrupt too: the meter already fired
                exc.baseline = baseline  # its evidence travels with the failure into the manifest
                raise
            return baseline, aperture
        return meter_step

    manifest_path = str(out_dir / "bracket_manifest.json")
    try:
        with Camera(model=args.model, port=args.port, serial=args.serial) as camera:
            if metered:
                # run_bracket refuses a body without the manualfocus route or a used --out before
                # any write, then freezes focus in Manual, then calls the meter.
                exposure_args = {"meter": meter_then_decide(camera)}
            else:
                inherited, _ = read_settings_checked(camera, purpose="focus bracket inherited exposure")
                state["baseline"] = {"source": "inherited", "metered": None,
                                     "inherited": {key: inherited.get(key) for key in HELD_KEYS}}
                exposure_args = {"baseline": state["baseline"], "aperture": (
                    resolve_bracket_aperture(camera, body_model=args.model, lens_model=args.lens)
                    if args.lens else None)}
                state["phase"] = "bracket"
            manifest = run_bracket(
                camera, **run_args, dest_dir=out_dir, af_hold_s=args.af_hold, **exposure_args,
                capture=lambda frame_dir: camera.capture(dest_dir=frame_dir))
    except MeterRefused as exc:
        print(json.dumps({"status": "meter_refused", "error": str(exc), "manifest": manifest_path,
                          "baseline": exc.baseline, "exposure_budget": budget, **_meter_failure(exc)}, indent=2))
        return 1
    except CaptureOutcomeUncertain as exc:
        # The shutter may or may not have fired; the partial manifest is preserved, nothing is replayed.
        if state["phase"] == "meter":
            print(json.dumps({"status": "uncertain", "error": str(exc), "manifest": manifest_path,
                              **_meter_failure(exc)}, indent=2))
            return 2
        print(json.dumps({"status": "uncertain", "error": str(exc), "retry_safe": False,
                          "manifest": manifest_path, "baseline": state["baseline"]}, indent=2))
        return 2
    except (CameraError, FocusBracketError, ValueError, OSError) as exc:
        if state["phase"] == "meter":
            print(json.dumps({"status": "failed", "error": str(exc), "manifest": manifest_path,
                              **_meter_failure(exc)}, indent=2))
            return 1
        # A metered baseline says whether the pre-meter exposure was restored before this failure.
        print(json.dumps({"status": "failed", "error": str(exc), "phase": state["phase"],
                          "manifest": manifest_path, "baseline": state["baseline"]}, indent=2))
        return 1

    baseline = manifest["baseline"]
    result = {"status": "complete", "manifest": manifest["manifest_path"],
              "restoration": manifest["restoration"], "frames": len(manifest["frames"]),
              "baseline": baseline, "exposure_budget": budget,
              "physical_exposure_count": (baseline.get("meter_physical_exposures") or 0) + len(manifest["frames"])}
    if manifest["restoration"].get("error"):
        result["status"] = "complete_restoration_failed"
    try:
        result["stack"] = _merge(manifest, out_dir)
    except (ValueError, FileNotFoundError, FocusBracketError) as exc:
        result["stack"] = {"status": "failed", "error": str(exc)}
    print(json.dumps(result, indent=2))
    return 0 if result["status"] == "complete" and "error" not in result["stack"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
