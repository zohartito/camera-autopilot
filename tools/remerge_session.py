#!/usr/bin/env python3
"""Re-merge a saved computational session offline, never touching the camera or the session.

Reads ``<session_dir>/manifest.json``, takes the accepted ``frame_NNNN.jpg`` files in index
order and re-runs ``stack_session.merge_session_frames`` for the manifest's mode into ``--out``.
The session's ``result.jpg`` and ``manifest.json`` are never overwritten. ``<out>.json`` records
the merge summary, SHA-256 of every input and output, and, when ``night_compare.json`` exists
beside the session, the flat-patch luma sigma of the old and new results measured the same way
``tools/night_receipt_compare.py`` measures its receipt (calibrator cells chosen on frame 0).

    .venv/bin/python tools/remerge_session.py backend/shots/sessions/<id> --out artifacts/x/out.jpg

``--partial`` (owner decision 2026-10-02) instead encodes the accepted frames of a timelapse left in
``needs_reconciliation`` by an ambiguous frame into a 1080p H.264 ``--out`` movie, with the session's
own fps and deflicker setting, as ``POST /sessions/{id}/encode-partial`` does. The session directory is
only read (every frame re-hashed against its receipt); the movie, its receipt ``<out>.json`` and a
temporary render folder beside ``--out`` are the only writes. The ambiguous frame is never used.

    .venv/bin/python tools/remerge_session.py backend/shots/sessions/<id> --partial --out artifacts/x/partial.mp4
"""
from __future__ import annotations

import argparse
import hashlib
import json
import statistics
import sys
from pathlib import Path

_TOOLS_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(_TOOLS_DIR.parent / "backend"))
sys.path.insert(0, str(_TOOLS_DIR))

# The receipt comparator's metric lives in the calibrator (select_flat_cells + measure_frame);
# ``night_receipt_compare._measure`` is private, so its two calls are mirrored here verbatim.
import calibrate_sensor_noise as calib  # noqa: E402
from stack_session import merge_session_frames, verify_anchor_output  # noqa: E402


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def accepted_frame_paths(manifest: dict, session_dir: Path) -> list[Path]:
    """Accepted frames as ``frame_NNNN.jpg`` in the session dir, in index order."""
    frames = [f for f in (manifest.get("frames") or []) if f.get("status") == "accepted"]
    frames.sort(key=lambda f: int(f["index"]))
    paths = []
    for frame in frames:
        path = session_dir / f"frame_{int(frame['index']):04d}.jpg"
        if not path.is_file():
            raise FileNotFoundError(f"accepted frame {path.name} is missing from {session_dir}")
        paths.append(path)
    if len(paths) < 2:
        raise ValueError(f"need at least 2 accepted frames to merge; found {len(paths)}")
    return paths


def sigma_comparison(frame_paths: list[Path], old_result: Path, new_result: Path,
                     cells_count: int) -> dict:
    """Flat-patch luma sigma of old vs new result, cells chosen once on frame 0 as the
    receipt comparator does. A frame too dark for the calibrator reports the error instead."""
    try:
        cells = calib.select_flat_cells(frame_paths[0], cells_count)
    except ValueError as exc:
        return {"method": "jpeg_flat_patch_residual", "error": str(exc)}
    per_frame = [calib.measure_frame(path, cells)[0] for path in frame_paths]
    old_sigma = calib.measure_frame(old_result, cells)[0] if old_result.is_file() else None
    new_sigma = calib.measure_frame(new_result, cells)[0]
    return {"method": "jpeg_flat_patch_residual", "cells": cells_count,
            "per_frame_sigmas": [round(value, 4) for value in per_frame],
            "per_frame_sigma_median": round(float(statistics.median(per_frame)), 4),
            "old_result_sigma": None if old_sigma is None else round(old_sigma, 4),
            "new_result_sigma": round(new_sigma, 4)}


def remerge(session_dir: Path, out: Path, *, deghost: bool | None, force: bool) -> dict:
    manifest_path = session_dir / "manifest.json"
    with open(manifest_path, encoding="utf-8") as handle:
        manifest = json.load(handle)
    mode = manifest.get("mode")
    if not mode:
        raise ValueError(f"{manifest_path} has no mode")
    out = out.resolve()
    session_root = session_dir.resolve()
    if out == session_root or session_root in out.parents:
        # The night merge writes motion_mask.png beside its output; any output inside the
        # session directory could clobber the session's own frames, result or mask.
        raise ValueError("refusing to write inside the session directory; choose --out elsewhere")
    if out.exists() and not force:
        raise FileExistsError(f"{out} exists; pass --force to replace it")
    frame_paths = accepted_frame_paths(manifest, session_dir)
    accepted = sorted((f for f in manifest.get("frames") or [] if f.get("status") == "accepted"),
                      key=lambda f: int(f["index"]))
    out.parent.mkdir(parents=True, exist_ok=True)

    options = {}
    if mode == "night" and deghost is not None:
        options["night_options"] = {"deghost": deghost}
    summary = merge_session_frames(mode, [str(path) for path in frame_paths], str(out), **options)
    try:
        # A nothing-to-recover HDR output is the anchor frame's own file, read again after the
        # merge: it must be the frame the session accepted, not whatever is on disk now.
        verify_anchor_output(summary, {"sha256": _sha256(out)},
                             [(frame.get("receipt") or {}).get("sha256") for frame in accepted])
    except ValueError:
        out.unlink(missing_ok=True)
        raise

    # The night merge names its mask ``motion_mask.png`` beside the output; give it the
    # output's stem so several sessions can be re-merged into one directory.
    maps = []
    for name in summary.get("maps") or []:
        source = Path(summary["maps_dir"]) / name
        target = out.with_name(f"{out.stem}_{name}")
        source.replace(target)
        maps.append(str(target))
    if maps:
        summary["maps_dir"] = str(out.parent)
        summary["maps"] = [Path(item).name for item in maps]

    report = {
        "session_id": manifest.get("id"), "session_dir": str(session_dir), "mode": mode,
        "inputs": [{"path": str(path), "sha256": _sha256(path)} for path in frame_paths],
        "output": {"path": str(out), "sha256": _sha256(out)},
        "maps": [{"path": item, "sha256": _sha256(Path(item))} for item in maps],
        "merge": summary,
    }
    compare_path = session_dir / "night_compare.json"
    old_result = session_dir / "result.jpg"
    if compare_path.is_file():
        with open(compare_path, encoding="utf-8") as handle:
            night_compare = json.load(handle)
        report["old_result"] = {"path": str(old_result),
                                "sha256": _sha256(old_result) if old_result.is_file() else None}
        report["sigma_comparison"] = sigma_comparison(
            frame_paths, old_result, out, int(night_compare.get("cells") or calib.DEFAULT_CELLS))
        report["sigma_comparison"]["night_compare_result_sigma"] = (
            night_compare.get("measured") or {}).get("result_sigma")
    return report


def encode_partial(session_dir: Path, out: Path, *, force: bool) -> dict:
    """The accepted frames of a blocked timelapse as a partial movie at ``out`` (session read-only)."""
    import timelapse_partial
    from stack_session import SessionRunner, _atomic_json, binary_receipt
    from timelapse_controls import read_controls
    session_root = session_dir.resolve()
    out = out.resolve()
    if out == session_root or session_root in out.parents:
        raise ValueError("refusing to write inside the session directory; choose --out elsewhere")
    if out.suffix != ".mp4":
        raise ValueError("--partial writes an .mp4 movie; give --out an .mp4 name")
    report_path = out.with_name(out.name + ".json")
    if report_path.is_symlink() or session_root in report_path.resolve().parents:
        raise ValueError(f"refusing to write the receipt through a symlink or into the session: {report_path}")
    if (out.exists() or report_path.exists()) and not force:
        raise FileExistsError(f"{out} exists; pass --force to replace it")
    # SessionRunner only reads here: read() validates the manifest, artifact() re-hashes each frame.
    sessions = SessionRunner(session_root.parent)
    session_id = session_root.name
    work_dir, pending = out.with_name(f".{out.name}.frames"), out.with_name(f".{out.stem}.pending.mp4")
    for temporary in (work_dir, pending):
        resolved = temporary.resolve()
        if resolved == session_root or resolved in session_root.parents:
            raise ValueError(f"refusing: the temporary render path {temporary} holds the session; choose --out elsewhere")
    timelapse_partial.clean_stale(work_dir, pending)  # an interrupted earlier run, before any check can fail
    state = sessions.read(session_id)
    partial_plan = timelapse_partial.plan(state)
    control = (read_controls(sessions, session_id) or {}).get("deflicker")
    sources = [{"index": frame["index"], "sha256": frame["receipt"]["sha256"],
                "path": sessions.artifact(session_id, f"frame_{frame['index']:04d}.jpg")}
               for frame in partial_plan["frames"]]
    out.parent.mkdir(parents=True, exist_ok=True)
    encoded = timelapse_partial.encode(
        sources, fps=partial_plan["fps"], deflicker_control=control,
        work_dir=work_dir, pending=pending, destination=out)
    output = binary_receipt(out, timelapse_partial.MAX_VIDEO_BYTES)
    document = timelapse_partial.receipt_document(session_id=session_id, partial_plan=partial_plan,
                                                  sources=sources, encoded=encoded, output=output)
    document.update(offline=True, session_dir=str(session_root))
    document["output"]["path"] = str(out)
    # Temp file + rename: a symlink planted at the receipt path after the check is replaced, not followed.
    _atomic_json(report_path, document)
    return {"status": "ok", "report": str(report_path), "output": document["output"],
            "frame_indices": document["frame_indices"], "ambiguous_indices": document["ambiguous_indices"]}


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("session_dir", type=Path)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--no-deghost", action="store_true",
                        help="night mode: plain average without motion deghosting")
    parser.add_argument("--partial", action="store_true",
                        help="timelapse blocked by an ambiguous frame: encode its accepted frames as a partial movie")
    parser.add_argument("--force", action="store_true", help="replace an existing --out")
    args = parser.parse_args(argv)
    if args.partial:
        from timelapse_video import EncoderUnavailable
        try:
            print(json.dumps(encode_partial(args.session_dir, args.out, force=args.force), indent=2))
        except EncoderUnavailable as error:
            print(json.dumps({"status": "unavailable", "message": str(error)}), file=sys.stderr)
            return 1
        except (OSError, ValueError, KeyError) as error:
            print(json.dumps({"status": "failed", "message": str(error)}), file=sys.stderr)
            return 1
        return 0
    try:
        report = remerge(args.session_dir, args.out,
                         deghost=False if args.no_deghost else None, force=args.force)
    except (OSError, ValueError, KeyError) as error:
        print(json.dumps({"status": "failed", "message": str(error)}), file=sys.stderr)
        return 1
    report_path = args.out.resolve().with_name(args.out.name + ".json")
    with open(report_path, "w", encoding="utf-8") as handle:
        json.dump(report, handle, indent=2)
    print(json.dumps({"status": "ok", "report": str(report_path), "merge": report["merge"],
                      "sigma_comparison": report.get("sigma_comparison")}, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
