#!/usr/bin/env python3
"""Compare a Night Assist plan's predicted noise against the measured noise on the frames it shot.

Night Assist (``backend/night_assist.py``) picks a frame count N so that averaging N short
exposures brings the measured per-frame sigma at the working ISO down to near base-ISO noise:
sigma_after_average = sigma_at_iso / sqrt(N), a 10*log10(N) dB reduction. That prediction is made
from a *calibration-scene* noise table. This tool checks whether it actually held on a *real night
sky*, using the exact flat-patch residual-sigma method the calibrator uses
(``tools/calibrate_sensor_noise.select_flat_cells`` / ``measure_frame``), so the numbers are
directly comparable to the plan's ``evidence.sigma_at_iso`` / ``sigma_after_average``.

Given a completed ``night`` session (or a ``tools/receipts/night_session.py`` receipt), it:
  - loads the manifest and its ``metadata.night_assist`` plan,
  - measures the flat-patch luma sigma on every ACCEPTED frame (cells chosen once on frame 0,
    the same cells reused across all frames and the result),
  - measures the same cells on the published ``result.jpg``,
  - reports planned vs measured per-frame sigma, planned vs measured result sigma, and two
    independent judgements that must not be conflated:

    1. ``overall_verdict`` / ``final_noise_target`` — the ABSOLUTE calibrated target the shot had
       to meet: base-ISO sigma times ``night_assist.NOISE_TARGET_RATIO``. This is authoritative.
         * ``meets_target``  — measured result sigma <= the calibrated target,
         * ``misses_target`` — measured result sigma > the calibrated target,
         * ``no_target``     — the manifest carries no base-ISO sigma to compute a target from.

    2. ``verdict`` — the RELATIVE averaging-gain diagnostic, in dB vs the planned 10*log10(N)
       (unchanged, kept for backward compatibility of saved ``night_compare.json`` files):
         * ``rule_holds``     — measured gain within 1.5 dB of planned,
         * ``at_noise_floor`` — single frames already at the JPEG quantization floor,
         * ``under_delivers`` — measured gain short of planned,
         * ``over_delivers``  — measured gain beyond planned.

  The one-point frame extrapolation (how many frames the single (N, gain) point would imply to
  match the ideal relative gain) is confined to the ``informational`` block, explicitly labelled
  ``one_point_extrapolation: not a prescription``, and never changes ``overall_verdict``. A stack
  can under-deliver on relative gain yet still meet its absolute target — that is ``meets_target``.

Read-only: it never touches the frames or the result. It prints a table and writes
``<session dir>/night_compare.json`` (or, for a receipt whose session dir is not reachable,
``night_compare.json`` beside the receipt). Deterministic: same inputs -> identical output.

Usage:
  .venv/bin/python tools/night_receipt_compare.py --session <session_id>
  .venv/bin/python tools/night_receipt_compare.py --receipt path/to/night-session-<ts>.json
"""
from __future__ import annotations

import argparse
import json
import math
import statistics
import sys
from pathlib import Path

# Reuse the calibrator's flat-patch residual method rather than duplicating the cell selection
# and residual math. The tool lives beside this one under tools/.
sys.path.insert(0, str(Path(__file__).resolve().parent))
import calibrate_sensor_noise as calib  # noqa: E402

# Gain within this many dB of the plan's 10*log10(N) counts as the rule holding.
GAIN_TOLERANCE_DB = 1.5
JPEG_NOISE_FLOOR_SIGMA = 0.6   # 8-bit luma sigma below which JPEG quantization, not the sensor, sets the noise
MAX_SENSIBLE_FRAMES = 4096    # beyond this a 'needed N' is not a plan, only a sign the model does not apply

_REPO_ROOT = Path(__file__).resolve().parents[1]
_DEFAULT_SESSIONS_DIR = _REPO_ROOT / "backend" / "shots" / "sessions"


def _load_json(path: Path) -> dict:
    with open(path, encoding="utf-8") as handle:
        return json.load(handle)


def _resolve_session_dir(session_id: str, sessions_dir: Path) -> Path:
    directory = sessions_dir / session_id
    if not (directory / "manifest.json").is_file():
        raise FileNotFoundError(
            f"no session manifest at {directory / 'manifest.json'} "
            f"(is --sessions-dir {sessions_dir} correct for this machine?)")
    return directory


def _night_plan(manifest: dict, session_id: str) -> dict:
    """The plan Night Assist actually shot: manifest metadata.night_assist. Clear error if absent."""
    if manifest.get("mode") != "night":
        raise ValueError(
            f"session {session_id} is mode {manifest.get('mode')!r}, not 'night'; "
            "night_receipt_compare only compares Night Assist sessions")
    plan = (manifest.get("metadata") or {}).get("night_assist")
    if not isinstance(plan, dict) or not plan:
        raise ValueError(
            f"session {session_id} has no night_assist plan in its manifest metadata; "
            "the sigma->N rule can only be checked on a session planned by Night Assist")
    return plan


def _accepted_frame_paths(manifest: dict, session_dir: Path) -> list[Path]:
    """Reconstruct the accepted frames' paths from the session dir (frame_NNNN.jpg), in index order.

    Reconstructing from the session dir (not the manifest's absolute ``destination``) keeps the
    comparison working when the receipt/session was produced on another machine.
    """
    frames = [f for f in (manifest.get("frames") or []) if f.get("status") == "accepted"]
    frames.sort(key=lambda f: f["index"])
    paths = []
    for frame in frames:
        path = session_dir / f"frame_{int(frame['index']):04d}.jpg"
        if not path.is_file():
            raise FileNotFoundError(f"accepted frame {path.name} is missing from {session_dir}")
        paths.append(path)
    if len(paths) < 2:
        raise ValueError(
            f"need at least 2 accepted frames to measure averaging; found {len(paths)}")
    return paths


def _measure(frame_paths: list[Path], result_path: Path, cells_count: int):
    """Median per-frame luma sigma and the result luma sigma, using cells chosen once on frame 0.

    Returns (per_frame_sigmas, per_frame_median, result_sigma, cells). Raises a clear error if the
    frame is too dark/uniform for the calibrator to find enough mid-tone flat cells.
    """
    try:
        cells = calib.select_flat_cells(frame_paths[0], cells_count)
    except ValueError as exc:
        raise ValueError(
            f"{frame_paths[0].name}: {exc} A real night frame may be too dark for the JPEG "
            "flat-patch residual method; point the receipt shot at a scene with mid-tone texture "
            "or lower --cells.") from exc
    per_frame = [calib.measure_frame(path, cells)[0] for path in frame_paths]
    result_sigma = calib.measure_frame(result_path, cells)[0]
    return per_frame, float(statistics.median(per_frame)), result_sigma, cells


def _needed_frames(planned_gain_db: float, measured_gain_db: float) -> int | None:
    """Frames the measured data would have needed to reach the PLANNED gain.

    Fits a power law gain(N) = k * 10*log10(N) to the one observed point (the planned N produced
    measured_gain_db, so k = measured/planned), then solves gain(N) = planned_gain_db:
        N = 10 ** (planned_gain_db**2 / (measured_gain_db * 10)).
    Under-delivery (k<1) yields N>planned; over-delivery (k>1) yields N<planned. Returns None when
    the measured frames showed no usable reduction (gain <= 0), i.e. no finite N would reach target.

    This is a ONE-POINT extrapolation, not a validated prescription: it assumes the sigma->N power
    law holds unchanged out to the extrapolated N even though only a single (N, gain) point was
    observed, and it ignores whatever floor (motion ghosting, JPEG quantization) actually capped the
    measured gain. Its value belongs in the report's ``informational`` block, never in the overall
    verdict.
    """
    if measured_gain_db <= 0 or planned_gain_db <= 0:
        return None
    exponent = planned_gain_db ** 2 / (measured_gain_db * 10.0)
    return int(math.ceil(10 ** exponent))


def _noise_target_ratio() -> float:
    """The base-ISO multiplier Night Assist plans against (``night_assist.NOISE_TARGET_RATIO``).

    Imported from the planner so the tool and the planner can never drift; falls back to the
    documented 1.25 ("within a quarter-stop of base-ISO noise") only if the backend package is not
    importable from this tool's environment.
    """
    try:
        sys.path.insert(0, str(_REPO_ROOT / "backend"))
        import night_assist  # noqa: E402
        return float(night_assist.NOISE_TARGET_RATIO)
    except ImportError:
        return 1.25


def _noise_target_ratio_source() -> str:
    try:
        sys.path.insert(0, str(_REPO_ROOT / "backend"))
        import night_assist  # noqa: E402,F401
        return "night_assist.NOISE_TARGET_RATIO"
    except ImportError:
        return "fallback_constant"


def _final_noise_target(evidence: dict) -> tuple[float | None, float | None]:
    """The absolute calibrated final-noise target the plan aimed to reach.

    Night Assist picks N so the averaged sigma lands at or below ``sigma_base_iso * NOISE_TARGET_RATIO``
    (see ``night_assist._frames_for_ratio``). That absolute target is what the shot must actually meet;
    the relative 10*log10(N) averaging gain is a separate diagnostic. Returns ``(target_sigma, ratio)``,
    or ``(None, None)`` when the manifest carries no base-ISO sigma to compute it from.
    """
    sigma_base = evidence.get("sigma_base_iso")
    if not isinstance(sigma_base, (int, float)) or sigma_base <= 0:
        return None, None
    ratio = _noise_target_ratio()
    return float(sigma_base) * ratio, ratio


def compare(session_dir: Path, session_id: str, *, cells_count: int) -> dict:
    """Build the full comparison report for one night session directory."""
    manifest = _load_json(session_dir / "manifest.json")
    plan = _night_plan(manifest, session_id)
    evidence = plan.get("evidence") or {}

    result_path = session_dir / "result.jpg"
    if not result_path.is_file():
        raise FileNotFoundError(f"session {session_id} has no published result.jpg to measure")

    frame_paths = _accepted_frame_paths(manifest, session_dir)
    per_frame, per_frame_median, result_sigma, cells = _measure(
        frame_paths, result_path, cells_count)

    planned_frames = int(plan.get("frames") or 0)
    if planned_frames < 2:
        raise ValueError(f"night plan frame count {planned_frames} is not a valid stack (>=2)")
    planned_gain_db = 10.0 * math.log10(planned_frames)

    if result_sigma <= 0:
        raise ValueError("measured result sigma is zero; the result frame carries no measurable noise")
    measured_gain_db = 20.0 * math.log10(per_frame_median / result_sigma)
    delta_gain_db = measured_gain_db - planned_gain_db

    if per_frame_median <= JPEG_NOISE_FLOOR_SIGMA:
        # The single frames are already at the JPEG quantization floor (base ISO, well exposed):
        # averaging cannot reduce what the encoder has already rounded away, and fitting N to it
        # only produces absurd counts. The plan's N was not wrong; there was nothing left to gain.
        verdict = "at_noise_floor"
        needed = planned_frames
    elif abs(delta_gain_db) <= GAIN_TOLERANCE_DB:
        verdict = "rule_holds"
        needed = planned_frames
    else:
        verdict = "under_delivers" if delta_gain_db < 0 else "over_delivers"
        needed = _needed_frames(planned_gain_db, measured_gain_db)
        if needed is not None and needed > MAX_SENSIBLE_FRAMES:
            needed = None  # a stack that large is not a plan; report it as unbounded

    planned_sigma_at_iso = evidence.get("sigma_at_iso")
    planned_sigma_after = evidence.get("sigma_after_average")

    # Absolute calibrated target: what the averaged result must actually reach. This is the
    # authoritative pass/fail — separate from the relative averaging-gain verdict above.
    target_sigma, target_ratio = _final_noise_target(evidence)
    if target_sigma is None:
        target_verdict = "no_target"
        target_margin = None
    else:
        target_verdict = "meets_target" if result_sigma <= target_sigma else "misses_target"
        target_margin = round(target_sigma - result_sigma, 4)

    # The overall verdict is the absolute target when the manifest lets us compute one; the
    # one-point frame extrapolation never changes it (see the informational block below).
    overall_verdict = target_verdict

    return {
        "session_id": session_id,
        "session_dir": str(session_dir),
        "mode": "night",
        "method": "jpeg_flat_patch_residual",
        "cells": len(cells),
        "noise_source": evidence.get("noise_source"),
        "planned_frames": planned_frames,
        "accepted_frames": len(frame_paths),
        "iso": evidence.get("iso") or plan.get("iso"),
        "shutter_s": evidence.get("shutter_s"),
        "planned": {
            "sigma_at_iso": planned_sigma_at_iso,
            "sigma_after_average": planned_sigma_after,
            "gain_db": round(planned_gain_db, 4),
        },
        "measured": {
            "per_frame_sigma_median": round(per_frame_median, 4),
            "per_frame_sigmas": [round(s, 4) for s in per_frame],
            "result_sigma": round(result_sigma, 4),
            "gain_db": round(measured_gain_db, 4),
        },
        "delta_gain_db": round(delta_gain_db, 4),
        "tolerance_db": GAIN_TOLERANCE_DB,
        # Relative averaging-gain verdict, unchanged for backward compatibility of saved
        # night_compare.json files: rule_holds / at_noise_floor / under_delivers / over_delivers.
        "verdict": verdict,
        # Absolute calibrated final-noise target: the authoritative pass/fail.
        "final_noise_target": {
            "sigma_base_iso": evidence.get("sigma_base_iso"),
            "target_ratio_to_base": target_ratio,
            "target_ratio_source": _noise_target_ratio_source(),
            "target_sigma": round(target_sigma, 4) if target_sigma is not None else None,
            "result_sigma": round(result_sigma, 4),
            "margin": target_margin,
            "verdict": target_verdict,
        },
        "overall_verdict": overall_verdict,
        # The frame extrapolation is informational only: it fits N to a single (N, gain) point and
        # must never be read as a prescription or allowed to override the absolute target above.
        "informational": {
            "one_point_extrapolation": "not a prescription",
            "relative_gain_verdict": verdict,
            "needed_frames_for_ideal_relative_gain": needed,
            "note": ("Extrapolated from one observed (frames, gain) point assuming the sigma->N "
                     "power law holds unchanged; ignores the floor (motion, JPEG) that capped the "
                     "measured gain. Not a validated frame count."),
        },
    }


def _fmt(value, spec="8.4f") -> str:
    return format(value, spec) if isinstance(value, (int, float)) else f"{'n/a':>8}"


def print_report(report: dict) -> None:
    planned, measured = report["planned"], report["measured"]
    print(f"Night sigma->N check  session {report['session_id']}")
    print(f"  noise source {report['noise_source']}  planned N {report['planned_frames']}  "
          f"accepted frames {report['accepted_frames']}  cells {report['cells']}")
    print(f"  ISO {report['iso']}  shutter {report['shutter_s']}s  method {report['method']}")
    print("  quantity                    planned   measured")
    print(f"  per-frame sigma (ISO)      {_fmt(planned['sigma_at_iso'])}   "
          f"{_fmt(measured['per_frame_sigma_median'])}")
    print(f"  result sigma (averaged)    {_fmt(planned['sigma_after_average'])}   "
          f"{_fmt(measured['result_sigma'])}")
    print(f"  averaging gain (dB)        {_fmt(planned['gain_db'])}   {_fmt(measured['gain_db'])}"
          f"   (delta {report['delta_gain_db']:+.3f} dB, tol +/-{report['tolerance_db']})")

    # Absolute calibrated target — the authoritative pass/fail.
    target = report["final_noise_target"]
    print(f"  final-noise target (abs)   {_fmt(target['target_sigma'])}   {_fmt(target['result_sigma'])}")
    overall = report["overall_verdict"]
    if overall == "meets_target":
        print(f"  OVERALL: meets_target — result sigma {target['result_sigma']} <= calibrated target "
              f"{target['target_sigma']} (base-ISO x{target['target_ratio_to_base']}); the shot met its goal.")
    elif overall == "misses_target":
        print(f"  OVERALL: misses_target — result sigma {target['result_sigma']} > calibrated target "
              f"{target['target_sigma']} (base-ISO x{target['target_ratio_to_base']}).")
    else:
        print("  OVERALL: no_target — manifest carries no base-ISO sigma; no absolute target to check.")

    # Relative averaging-gain diagnostic (backward-compatible verdict).
    verdict = report["verdict"]
    if verdict == "rule_holds":
        print(f"  relative gain: rule_holds — measured gain within {report['tolerance_db']} dB of plan.")
    elif verdict == "at_noise_floor":
        print(f"  relative gain: at_noise_floor — single frames already sit at the JPEG quantization floor "
              f"(sigma <= {JPEG_NOISE_FLOOR_SIGMA}); averaging cannot help.")
    else:
        print(f"  relative gain: {verdict} — measured averaging gain {'short of' if verdict == 'under_delivers' else 'beyond'} plan.")

    # One-point extrapolation: informational only, never a prescription.
    needed = report["informational"]["needed_frames_for_ideal_relative_gain"]
    needed_txt = f"{needed} frames" if needed is not None else "an unbounded N (no finite stack)"
    print(f"  informational (one_point_extrapolation: not a prescription): to match the ideal "
          f"relative gain the single data point implies {needed_txt} (planned {report['planned_frames']}); "
          "do not treat as a validated frame count.")


def _output_path(report: dict, receipt_path: Path | None) -> Path:
    session_dir = Path(report["session_dir"])
    if session_dir.is_dir():
        return session_dir / "night_compare.json"
    if receipt_path is not None:
        return receipt_path.parent / "night_compare.json"
    return session_dir / "night_compare.json"


def run(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--session", help="night session id under the sessions dir")
    source.add_argument("--receipt", type=Path,
                        help="tools/receipts night-session JSON (its session_id is resolved)")
    parser.add_argument("--sessions-dir", type=Path, default=_DEFAULT_SESSIONS_DIR,
                        help=f"session store (default {_DEFAULT_SESSIONS_DIR})")
    parser.add_argument("--cells", type=int, default=calib.DEFAULT_CELLS,
                        help=f"flat mid-tone cells to measure over (default {calib.DEFAULT_CELLS})")
    parser.add_argument("--report-only", action="store_true", default=True,
                        help="read-only comparison (the only mode); no frames are ever modified")
    args = parser.parse_args(argv)

    receipt_path = None
    if args.receipt is not None:
        receipt_path = args.receipt.expanduser()
        receipt = _load_json(receipt_path)
        session_id = receipt.get("session_id")
        if not session_id:
            parser.error(f"receipt {receipt_path} has no session_id")
    else:
        session_id = args.session

    session_dir = _resolve_session_dir(session_id, args.sessions_dir.expanduser())
    report = compare(session_dir, session_id, cells_count=args.cells)

    print_report(report)
    out_path = _output_path(report, receipt_path)
    out_path.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(f"  wrote {out_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(run())
