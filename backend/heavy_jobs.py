"""Resumable heavy jobs for the split runtime (docs/RUNTIME_SPLIT_2026-09-28.md).

Control queues work in the session manifest (``heavy_job``) after capture and restoration; the heavy-jobs
role recomputes it from the hash-verified originals. The session's cross-process ``operation`` flock is
the lease: a job left ``running`` with a free lock means its worker died (an OOM kill, a reboot), and the
next scan recomputes it. The worker never opens a camera and never publishes to the gallery.
"""
from __future__ import annotations

import os
import re
import sys
import time
from pathlib import Path

from stack_session import frame_receipt

MAX_ATTEMPTS = 3
# The job this process is computing now; role_jobs' watchdog reads it (F11: no unbounded merge).
CURRENT = {"session_id": None, "started": None}


def overdue(limit_s: float) -> str | None:
    """The running session id when its job has exceeded ``limit_s`` of wall clock."""
    started = CURRENT["started"]
    if started is not None and time.monotonic() - started > limit_s:
        return CURRENT["session_id"]
    return None


def job_of(state: dict) -> dict:
    """The manifest's heavy_job, or {} when absent or not a mapping (a corrupt manifest never stops a sweep)."""
    job = state.get("heavy_job")
    return job if isinstance(job, dict) else {}


class EncodeFailed(RuntimeError):
    """The timelapse video encode failed; the JPEG sequence is saved."""


def pending_heavy_work(state: dict) -> bool:
    if state["mode"] == "timelapse":
        # "unavailable" (no ffmpeg) is a final answer. A failed encode is retried up to MAX_ATTEMPTS,
        # then the sequence is published with the video recorded as failed, as the single process does.
        job = job_of(state)
        video = (state.get("video") or {}).get("status")
        return not (state.get("sequence")
                    and (video in ("completed", "unavailable") or (video == "failed" and job.get("encode_gave_up"))))
    return not state.get("output")


def queue(state: dict) -> None:
    """Record a queued heavy job on ``state``; the caller saves it. ``SessionRunner.run`` calls it so a
    deferred session becomes completed and queued in one manifest write (Cancel refuses both)."""
    job = job_of(state)
    state["heavy_job"] = {
        "status": "queued",
        "kind": "timelapse_sequence_video" if state["mode"] == "timelapse" else "merge",
        "inputs": [frame["receipt"]["sha256"] for frame in state["frames"]],
        "queued_at": time.time(),
        "attempts": 0,
    }
    if job.get("status") == "failed":
        state["heavy_job"]["previous_error"] = job.get("error")


def enqueue(sessions, state: dict) -> dict:
    """Queue (or re-queue a failed) heavy job. The caller holds the session operation lock."""
    if job_of(state).get("status") in ("queued", "running"):
        return sessions.read(state["id"])
    queue(state)
    sessions.save(state)
    return sessions.read(state["id"])


def _verified_inputs(sessions, state: dict) -> list[str]:
    expected = state["heavy_job"]["inputs"]
    if len(expected) != len(state["frames"]):
        raise ValueError("queued inputs disagree with the session frames")
    paths = []
    for frame, sha256 in zip(state["frames"], expected):
        if frame["status"] != "accepted" or frame["receipt"]["sha256"] != sha256:
            raise ValueError("accepted frame changed after the job was queued")
        # artifact() re-hashes the file against the accepted receipt.
        paths.append(str(sessions.artifact(state["id"], f"frame_{frame['index']:04d}.jpg")))
    return paths


def _merge(sessions, state: dict) -> dict:
    import product_sessions
    from stack_session import merge_session_frames, verify_anchor_output
    paths = _verified_inputs(sessions, state)
    destination = sessions._directory(state["id"]) / "result.jpg"
    destination.unlink(missing_ok=True)  # an unaccepted partial output from a killed attempt
    if state["mode"] == "panorama":
        output = merge_session_frames("panorama", paths, str(destination),
                                      panorama_options=product_sessions._panorama_options(state))
    else:
        output = merge_session_frames(state["mode"], paths, str(destination))
    output_path = output.get("out_path") if isinstance(output, dict) else output
    if not output_path or Path(output_path).resolve() != destination.resolve():
        raise ValueError("merge did not produce the requested output")
    receipt = frame_receipt(output_path)
    verify_anchor_output(output, receipt, list(state["heavy_job"]["inputs"]))
    state["output"] = {"receipt": receipt, "details": output if isinstance(output, dict) else {}}
    return {"result.jpg": state["output"]["receipt"]["sha256"]}


def _timelapse(sessions, state: dict) -> dict:
    import product_sessions
    _verified_inputs(sessions, state)
    state = product_sessions._sequence(sessions, state)
    state = product_sessions._video(sessions, state)
    video = state.get("video") or {}
    if video.get("status") not in ("completed", "unavailable"):
        raise EncodeFailed(f"timelapse encode {video.get('status')}: {video.get('error')}")
    return {"sequence.zip": state["sequence"]["receipt"]["sha256"],
            "timelapse.mp4": (video.get("receipt") or {}).get("sha256")}


def process_locked(sessions, session_id: str) -> bool:
    """Run one queued job; the caller holds ``sessions.operation(session_id)``. True if it acted."""
    state = sessions.read(session_id)
    job = job_of(state)
    if state["status"] != "completed" or job.get("status") not in ("queued", "running"):
        return False
    if job["status"] == "running" and job.get("attempts", 0) >= MAX_ATTEMPTS:
        job.update(status="failed", finished_at=time.time(),
                   error=f"worker exited during the job {job['attempts']} times (possibly a memory kill); "
                         "originals are preserved and POST /sessions/{id}/run re-queues it")
        sessions.save(state)
        return True
    job.update(status="running", attempts=job.get("attempts", 0) + 1, started_at=time.time(),
               worker_pid=os.getpid())
    job.pop("error", None)
    sessions.save(state)
    CURRENT.update(session_id=session_id, started=time.monotonic())
    try:
        outputs = _timelapse(sessions, state) if state["mode"] == "timelapse" else _merge(sessions, state)
    except EncodeFailed as exc:
        state = sessions.read(session_id)
        job = state["heavy_job"]
        if job["attempts"] < MAX_ATTEMPTS:
            job.update(status="queued", last_error=str(exc))  # the next scan tries the encode again
        else:
            job.update(status="completed", finished_at=time.time(), encode_gave_up=True, last_error=str(exc),
                       outputs={"sequence.zip": state["sequence"]["receipt"]["sha256"], "timelapse.mp4": None},
                       note=f"The video encode failed {job['attempts']} times; the JPEG sequence is published "
                            "without a movie, as the single-process runtime does.")
        sessions.save(state)
        return True
    except Exception as exc:  # noqa: BLE001 — the job records every failure; originals stay untouched
        state = sessions.read(session_id)
        state["heavy_job"].update(status="failed", finished_at=time.time(),
                                  error=f"{type(exc).__name__}: {exc}")
        sessions.save(state)
        return True
    finally:
        CURRENT.update(session_id=None, started=None)
    state["heavy_job"].update(status="completed", finished_at=time.time(), outputs=outputs,
                              algorithm="stack_session.merge_session_frames"
                              if state["mode"] != "timelapse" else "product_sessions._sequence+_video")
    sessions.save(state)
    return True


def partial_pending(state: dict) -> bool:
    """A partial timelapse movie the owner requested on a blocked session (queued by control)."""
    record = state.get("partial_video")
    return (state["status"] == "needs_reconciliation" and isinstance(record, dict)
            and record.get("status") in ("queued", "running"))


def process_partial_locked(sessions, session_id: str) -> bool:
    """Encode one queued partial movie; the caller holds ``sessions.operation(session_id)``."""
    import product_sessions
    state = sessions.read(session_id)
    if not partial_pending(state):
        return False
    record = state["partial_video"]
    attempts = record.get("attempts", 0)
    if record["status"] == "running" and attempts >= MAX_ATTEMPTS:
        import timelapse_partial
        directory = sessions._directory(session_id)
        timelapse_partial.clean_stale(directory / timelapse_partial.WORK_DIR_NAME,
                                      directory / timelapse_partial.PENDING_NAME)
        state["partial_video"] = {"status": "failed", "frames_preserved": True, "retry_safe": True,
                                  "error": f"worker exited during the partial encode {attempts} times "
                                           "(possibly a memory kill); request it again to retry"}
        sessions.save(state)
        return True
    record.update(status="running", attempts=attempts + 1, started_at=time.time(), worker_pid=os.getpid())
    sessions.save(state)
    CURRENT.update(session_id=session_id, started=time.monotonic())
    try:
        product_sessions._encode_partial_locked(sessions, session_id)
    finally:
        CURRENT.update(session_id=None, started=None)
    return True


def run_pending(sessions, stop=None) -> list[str]:
    """One scan of the store. Busy sessions (operation lock held elsewhere) wait for the next scan;
    a set ``stop`` event ends the scan between jobs, never inside one."""
    processed = []
    for directory in sorted(sessions.root.iterdir()):
        if stop is not None and stop.is_set():
            break
        session_id = directory.name
        if not re.fullmatch(r"[0-9a-f]{32}", session_id) or not (directory / "manifest.json").is_file():
            continue
        try:
            state = sessions.read(session_id)
        except (OSError, ValueError):
            continue
        partial = partial_pending(state)
        if not partial and (state["status"] != "completed" or job_of(state).get("status") not in ("queued", "running")):
            continue
        try:
            with sessions.operation(session_id):
                if (process_partial_locked if partial else process_locked)(sessions, session_id):
                    processed.append(session_id)
        except RuntimeError:
            continue  # control is publishing, deleting or running this session
        except (OSError, ValueError) as exc:
            # Deleted while we looked, or a manifest write failed; the next scan retries.
            if not isinstance(exc, FileNotFoundError):
                print(f"jobs: {session_id}: {type(exc).__name__}: {exc}", file=sys.stderr, flush=True)
            continue
    return processed
