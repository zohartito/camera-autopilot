"""Heavy-jobs role: merges and timelapse encodes queued by the camera-control role.

    backend/.venv-jobs/bin/python backend/role_jobs.py [--once] [--poll-s 2]

Scans the session store (the same ``CAMERA_ENGINE``/``CAMERA_AUTOPILOT_SHOTS_DIR`` as control), runs each
queued job from its hash-verified originals and exits cleanly between jobs on SIGTERM. Never opens a camera.
"""
from __future__ import annotations

import argparse
import json
import math
import os
import signal
import sys
import threading

import runtime_roles

EXIT_OVERDUE = 75


def _watchdog(sessions, stop, limit_s, heavy_jobs, every_s=5.0):
    """Heartbeat for control's /device-health, and a wall clock for in-process merges: a job past
    ``limit_s`` exits the process, leaving the job 'running' to be recomputed (bounded by MAX_ATTEMPTS)."""
    while not stop.wait(every_s):
        runtime_roles.touch_heartbeat(sessions.root / runtime_roles.JOBS_HEARTBEAT)
        session_id = heavy_jobs.overdue(limit_s)
        if session_id:
            print(f"jobs: {session_id} exceeded {limit_s:g} s; exiting for a restart", file=sys.stderr, flush=True)
            os._exit(EXIT_OVERDUE)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--once", action="store_true", help="one scan, print what ran, exit")
    parser.add_argument("--poll-s", type=float, default=2.0)
    parser.add_argument("--job-max-s", type=float,
                        default=runtime_roles.env_float("CAMERA_AUTOPILOT_JOB_MAX_S", 3600.0, 10.0, 86400.0))
    args = parser.parse_args(argv)
    if not math.isfinite(args.poll_s) or not 0.1 <= args.poll_s <= 300:
        parser.error("--poll-s must be 0.1..300")
    if not math.isfinite(args.job_max_s) or not 10 <= args.job_max_s <= 86400:
        parser.error("--job-max-s must be 10..86400")
    runtime_roles.set_role("jobs")
    runtime_roles.require_memory_cap("jobs")
    import heavy_jobs
    import product_sessions
    sessions = product_sessions.runner()
    if args.once:
        print(json.dumps({"role": "jobs", "processed": heavy_jobs.run_pending(sessions)}), flush=True)
        return 0
    stop = threading.Event()
    for signum in (signal.SIGTERM, signal.SIGINT):
        signal.signal(signum, lambda *_: stop.set())
    print(json.dumps({"role": "jobs", "store": str(sessions.root)}), flush=True)
    runtime_roles.touch_heartbeat(sessions.root / runtime_roles.JOBS_HEARTBEAT)
    threading.Thread(target=_watchdog, args=(sessions, stop, args.job_max_s, heavy_jobs), daemon=True).start()
    while not stop.is_set():
        try:
            processed = heavy_jobs.run_pending(sessions, stop=stop)
        except Exception as exc:  # noqa: BLE001 — a vanished store or a bad manifest never ends the worker
            print(f"jobs: scan failed ({type(exc).__name__}: {exc})", file=sys.stderr, flush=True)
            processed = []
        if processed:
            print(json.dumps({"role": "jobs", "processed": processed}), flush=True)
        stop.wait(args.poll_s)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
