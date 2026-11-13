#!/usr/bin/env python3
"""Offline merge benchmark: explicit JPEGs, fresh processes, no camera/model access.

Example (use your own existing files):
  .venv/bin/python tools/puck_workload_bench.py frame0.jpg frame1.jpg frame2.jpg \
    --mode hdr --mode night --mode crowd --output /tmp/puck-benchmark.json

Night/crowd on an exposure bracket measure compute only, not suitable scene quality.
The existing merger owns input bounds and an estimated working-set guard,
which is not a measured process-RSS ceiling.
"""
from __future__ import annotations

import argparse
import hashlib
import importlib.metadata
import json
import os
from pathlib import Path
import platform
import subprocess
import sys
import tempfile
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))
# "night" measures the CURRENT production default: motion-aware deghosting, which
# allocates moving-pixel masks the plain average never did. This is the exact call
# backend/stack_session._merge_night makes (long_exposure_with_report(deghost=True)),
# minus the tiny single-channel motion_mask.png write. The old plain-average
# stacking.long_exposure (deghost=False) is NOT what the shipping night path runs.
# "long" is the plain trails average (deghost=False); "focus" is the sharpest-region stack.
# "crowd" is the bare temporal median; "crowd_auto" is the shipping crowd session merge
# (backend/stack_session._merge_crowd_remove: median fallback, automatic masks, recovery).
MODES = {"hdr": "hdr_merge", "night": "long_exposure_with_report", "crowd": "crowd_remove",
         "long": "long_exposure", "focus": "focus_stack", "crowd_auto": "_merge_crowd_remove"}
EXCLUDED = ["camera control and transfer", "model inference", "power and battery life",
            "thermal steady state", "image quality or photographic success", "hardware ranking"]


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def inputs(paths: list[str]) -> list[dict]:
    from stack_session import frame_receipt, validate_frames
    validate_frames(len(paths))
    receipts = [frame_receipt(Path(path)) for path in paths]
    if len({item["path"] for item in receipts}) != len(receipts):
        raise ValueError("provide distinct existing JPEG paths; duplicates are not extra captured frames")
    if len({(item["width"], item["height"]) for item in receipts}) != 1:
        raise ValueError("all JPEG dimensions must match")
    return receipts


def peak_rss_bytes() -> int | None:
    # getrusage reports bytes on macOS and KiB on Linux; don't guess elsewhere.
    if sys.platform not in ("darwin", "linux"):
        return None
    import resource
    peak = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    return int(peak * (1024 if sys.platform == "linux" else 1))


def crowd_auto_worker(paths: list[str], result: dict):
    """Run the shipping crowd session merge; return its image and decode-and-merge seconds.

    Unlike the array-returning merges, the session merge encodes files itself: the registered
    JPEGs its estimator reads, the mask PNGs, the maps and the result JPEG. Every cv2.imwrite
    during the call is timed and subtracted, so ``merge_wall_s`` stays decode and merge only
    (decoding those staged files back is part of the merge), and the result is read back for
    the receipt hash after the clock stops. The whole call is ``session_merge_wall_s``.
    """
    import cv2
    import stack_session
    encode = {"wall_s": 0.0, "files": 0}
    original_imwrite = cv2.imwrite

    def timed_imwrite(*args, **kwargs):
        began = time.perf_counter()
        try:
            return original_imwrite(*args, **kwargs)
        finally:
            encode["wall_s"] += time.perf_counter() - began
            encode["files"] += 1

    with tempfile.TemporaryDirectory(prefix="camera-puck-bench-crowd-") as work:
        cv2.imwrite = timed_imwrite
        try:
            began = time.perf_counter()
            summary = stack_session._merge_crowd_remove(paths, str(Path(work) / "result.jpg"))
            session_s = time.perf_counter() - began
        finally:
            cv2.imwrite = original_imwrite
        merged = cv2.imread(summary["out_path"])
    result["crowd_auto"] = {"background_estimate": summary.get("background_estimate"),
                            "session_merge_wall_s": session_s,
                            "file_encode_wall_s": encode["wall_s"],
                            "files_encoded": encode["files"]}
    return merged, session_s - encode["wall_s"]


def worker(mode: str, paths: list[str]) -> dict:
    import stacking
    import cv2
    started = time.perf_counter()
    result = {"mode": mode, "operation": MODES[mode], "pid": os.getpid()}
    try:
        if mode == "night":
            # Production default (deghost=True): the memory-heavier path with mask
            # allocation, exactly as backend/stack_session._merge_night invokes it.
            merged, report = stacking.long_exposure_with_report(paths, deghost=True)
            result["night"] = {"deghost": report["deghost"],
                               "reference_index": report["reference_index"],
                               "moving_fraction": report["moving_fraction"]}
        elif mode == "crowd_auto":
            merged, merge_s = crowd_auto_worker(paths, result)
        else:
            merged = getattr(stacking, MODES[mode])(paths)
        if mode != "crowd_auto":
            merge_s = time.perf_counter() - started
        with tempfile.TemporaryDirectory(prefix="camera-puck-bench-") as directory:
            destination = Path(directory) / "merged.jpg"
            if not cv2.imwrite(str(destination), merged):
                raise RuntimeError("temporary merged JPEG encoding failed")
            result.update(status="completed", merge_wall_s=merge_s,
                          output={"width": int(merged.shape[1]), "height": int(merged.shape[0]),
                                  "bytes": destination.stat().st_size, "sha256": sha256(destination),
                                  "retained": False})
    except Exception as exc:
        result.update(status="rejected" if isinstance(exc, ValueError) else "error",
                      error_type=type(exc).__name__, error=str(exc))
    result.update(workload_wall_s=time.perf_counter() - started, peak_rss_bytes=peak_rss_bytes(),
                  opencv_threads=cv2.getNumThreads(), opencv_version=cv2.__version__)
    return result


def run_benchmark(paths: list[str], modes: list[str], *, timeout_s: float = 180,
                  repeats: int = 1) -> dict:
    if not modes or len(modes) > len(MODES) or len(set(modes)) != len(modes) or any(mode not in MODES for mode in modes):
        raise ValueError(f"select distinct modes from: {', '.join(MODES)}")
    if not 1 <= repeats <= 3 or not 1 <= timeout_s <= 600:
        raise ValueError("repeats must be 1..3 and timeout must be 1..600 seconds")
    receipts = inputs(paths)
    canonical = [item["path"] for item in receipts]
    runs = []
    for mode in modes:
        for repeat in range(repeats):
            started = time.perf_counter()
            try:
                completed = subprocess.run(
                    [sys.executable, str(Path(__file__).resolve()), "--worker", mode, *canonical],
                    capture_output=True, text=True, timeout=timeout_s, check=False)
                if completed.returncode:
                    run = {"mode": mode, "status": "process_error", "returncode": completed.returncode,
                           "error": completed.stderr[-2000:]}
                else:
                    run = json.loads(completed.stdout)
            except subprocess.TimeoutExpired:
                run = {"mode": mode, "status": "timeout", "timeout_s": timeout_s}
            run.update(repeat=repeat + 1, process_wall_s=time.perf_counter() - started)
            runs.append(run)
    import cv2
    versions = {"cv2": cv2.__version__}
    for name in ("numpy", "opencv-python-headless", "opencv-python",
                 "opencv-contrib-python", "opencv-contrib-python-headless", "Pillow"):
        try:
            versions[name] = importlib.metadata.version(name)
        except importlib.metadata.PackageNotFoundError:
            pass
    # Every backend module a mode runs: crowd_auto adds the estimator and the recovery.
    source_files = [Path(__file__).resolve(), ROOT / "backend/stacking.py",
                    ROOT / "backend/image_safety.py", ROOT / "backend/stack_session.py",
                    ROOT / "backend/crowd_auto_mask.py", ROOT / "backend/crowd_recovery.py"]
    # Reuse the bounded regular-file reader with O_NOFOLLOW/O_NONBLOCK. A source
    # replaced with a symlink or FIFO must never escape the child timeout by
    # blocking this parent-side integrity check.
    from stack_session import frame_receipt
    integrity_errors = []
    unchanged = True
    for item in receipts:
        try:
            current = frame_receipt(Path(item["path"]))
            if current["sha256"] != item["sha256"]:
                unchanged = False
                integrity_errors.append({"path": item["path"], "error": "source hash changed"})
        except (ValueError, OSError) as exc:
            unchanged = False
            integrity_errors.append({"path": item["path"], "error": str(exc)})
    return {"schema": 1, "benchmark": "offline-jpeg-stacking", "created_at_unix": time.time(),
            "platform": {"system": platform.system(), "release": platform.release(),
                         "machine": platform.machine(), "processor": platform.processor(),
                         "python": sys.version, "executable": sys.executable},
            "versions": versions,
            "opencv_build": {"module_path": cv2.__file__,
                             "build_info_sha256": hashlib.sha256(cv2.getBuildInformation().encode()).hexdigest()},
            "source_hashes": {str(path.relative_to(ROOT)): sha256(path) for path in source_files},
            "scope": {"included": "existing JPEG decode, alignment, merge and temporary JPEG encode",
                      "merge_wall_s": "decode and merge only; imports and output encode excluded. crowd_auto also excludes the file encodes the session merge performs itself (registered JPEGs, mask PNGs, maps, result), recorded as crowd_auto.file_encode_wall_s; crowd_auto.session_merge_wall_s is the whole call",
                      "process_wall_s": "fresh interpreter startup through process exit",
                      "peak_rss_bytes": "fresh process lifetime high-water mark, includes imports and encode; not incremental RAM",
                      "excluded": EXCLUDED, "cache": "OS file caches are not flushed; no cold-storage claim",
                      "exposure": "HDR is fixed 4-level Mertens-family Laplacian JPEG exposure fusion with contrast/saturation weights, no calibrated EV/response input; input scene suitability not inferred",
                      "night": "night runs stacking.long_exposure_with_report(deghost=True), the shipping default that backend/stack_session._merge_night invokes; it allocates moving-pixel masks the old plain average did not, so this figure supersedes any pre-deghosting night number. The tiny single-channel motion_mask.png write is excluded",
                      "threads": "OpenCV host default; recorded per run"},
            "inputs": receipts, "originals_unchanged": unchanged,
            "original_integrity_errors": integrity_errors, "runs": runs}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("paths", nargs="+", help="2..16 distinct existing JPEG files")
    parser.add_argument("--mode", action="append", choices=MODES)
    parser.add_argument("--output", type=Path, help="new JSON receipt path; never overwritten")
    parser.add_argument("--timeout", type=float, default=180)
    parser.add_argument("--repeats", type=int, default=1)
    parser.add_argument("--worker", choices=MODES, help=argparse.SUPPRESS)
    args = parser.parse_args(argv)
    if args.worker:
        print(json.dumps(worker(args.worker, args.paths), allow_nan=False))
        return 0
    try:
        if args.output and (args.output.exists() or args.output.is_symlink()):
            raise ValueError("output receipt already exists; choose a new path")
        report = run_benchmark(args.paths, args.mode or list(MODES), timeout_s=args.timeout,
                               repeats=args.repeats)
        encoded = json.dumps(report, indent=2, allow_nan=False) + "\n"
        if args.output:
            descriptor = os.open(args.output, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(descriptor, "w") as stream:
                stream.write(encoded)
        else:
            print(encoded, end="")
        return 0 if report["originals_unchanged"] and all(run["status"] == "completed" for run in report["runs"]) else 2
    except (ValueError, OSError) as exc:
        parser.error(str(exc))


if __name__ == "__main__":
    raise SystemExit(main())
