"""Durable, bounded background sessions. Restart never repeats camera work."""
from __future__ import annotations
import contextlib
import concurrent.futures
import fcntl
import json
import math
import os
import threading
import time
import uuid
import runtime_roles
from stack_session import CancelRefused, _atomic_json, refuse_cancel, regular_bytes


class SessionJobs:
    def __init__(self, runner_factory, run_session, *, camera_exclusion=contextlib.nullcontext, max_jobs=8):
        if type(max_jobs) is not int or not 1 <= max_jobs <= 100:
            raise ValueError("max_jobs must be 1..100")
        self.runner_factory, self.run_session = runner_factory, run_session
        self.camera_exclusion, self.max_jobs = camera_exclusion, max_jobs
        self._lock = threading.RLock()
        self._futures = {}
        self._timers = {}
        self._submitted_monotonic = {}
        self._rejected_operations = set()
        # Receipts whose recovery waits for a session lock held by another process (the split
        # runtime's heavy-jobs worker); status() retries them. Startup never fails on one.
        self._recovery_deferred = set()
        self._closing = False
        self._executor = concurrent.futures.ThreadPoolExecutor(max_workers=1, thread_name_prefix="camera-session")
        root = runner_factory().root
        fd = os.open(root / ".jobs.lock", os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
        self._lease = os.fdopen(fd, "a")
        try:
            fcntl.flock(self._lease, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BaseException:
            self._lease.close()
            self._executor.shutdown(wait=False)
            raise RuntimeError("another session scheduler owns this store")
        self.recover()

    def _path(self, session_id):
        sessions = self.runner_factory()
        sessions.read(session_id)
        return sessions._directory(session_id) / "job.json"

    def status(self, session_id):
        with self._lock:
            path = self._path(session_id)
            if not path.exists():
                return {"id": session_id, "status": "not_submitted"}
            job = json.loads(regular_bytes(path, 65536))
            if job.get("id") != session_id:
                raise ValueError("job identity mismatch")
            if session_id in self._recovery_deferred:
                return self._recover_one(self.runner_factory().read(session_id), job)
            future = self._futures.get(session_id)
            if job["status"] in ("queued", "running") and future and future.done():
                # Repair only metadata. Accepted frames/session state remain authority;
                # a completed Future must never leave an eternal running receipt.
                state = self.runner_factory().read(session_id)
                ambiguous = any(f["status"] in ("capturing", "ambiguous") for f in state["frames"])
                terminal = state["status"] if state["status"] in (
                    "completed", "failed", "cancelled", "restore_failed") else "recovery_required"
                repaired = {**job, "status": "uncertain" if ambiguous else terminal,
                            "session_status": state["status"], "retry_safe": not ambiguous,
                            "metadata_reconciled": True,
                            "reconciliation_reason": "Worker ended before final job receipt was saved; no camera replay was performed."}
                try:
                    return self._save(repaired)
                except (OSError, RuntimeError) as exc:
                    return {**repaired, "status": "recovery_required", "metadata_status": "write_failed",
                            "error": f"Could not persist final job receipt: {type(exc).__name__}: {exc}",
                            "recovery_action": "retry_job_receipt_only"}
            return job

    def _save(self, job):
        job["updated_at"] = time.time()
        # Metadata for another queued session must not wait for the global
        # gallery lock held across a long capture. Per-session operation locking
        # protects direct deletion; lifecycle deletion checks active job markers.
        with self.runner_factory().operation(job["id"]):
            _atomic_json(self._path(job["id"]), job)
        return dict(job)

    def recover(self):
        """A process restart requires explicit operator retry, even for queued work."""
        recovered = []
        with self._lock:
            for state in self.runner_factory().list():
                job = self.status(state["id"])
                if job["status"] in ("queued", "running") and state["id"] not in self._futures:
                    recovered.append(self._recover_one(state, job))
        return recovered

    def _recover_one(self, state, job):
        """Mark a receipt from an ended process for explicit retry. If another process holds the
        session lock (a heavy job merging it), keep the receipt and retry on the next status()."""
        if job["status"] not in ("queued", "running") or state["id"] in self._futures:
            self._recovery_deferred.discard(state["id"])
            return job
        ambiguous = any(f["status"] in ("capturing", "ambiguous") for f in state["frames"])
        repaired = {**job, "status": "uncertain" if ambiguous else "recovery_required",
                    "error": "Previous process ended; automatic camera replay is disabled.",
                    "retry_safe": not ambiguous, "session_status": state["status"]}
        try:
            saved = self._save(repaired)
        except RuntimeError:
            self._recovery_deferred.add(state["id"])
            # Not "running": the capture is over; only this receipt waits for the other process.
            return {**job, "status": "recovery_pending", "recovery_pending": True, "previous_status": job["status"],
                    "note": "Another process holds this session; recovery completes when it finishes."}
        self._recovery_deferred.discard(state["id"])
        return saved

    def submit(self, session_id, *, delay_s=0):
        if type(delay_s) not in (int, float) or not math.isfinite(delay_s) or not (delay_s == 0 or 1 <= delay_s <= 36000):
            raise ValueError("delay_s must be zero or between 1 and 36000 seconds")
        with self._lock:
            if self._closing:
                raise RuntimeError("session scheduler is shutting down")
            state = self.runner_factory().read(session_id)
            metadata = state["metadata"]
            if not metadata.get("camera_model") or not metadata.get("camera_port"):
                raise ValueError("background session requires exact camera model and port")
            # A recovered or legacy session without a bound serial is never replayed by port.
            import product_sessions
            product_sessions.require_camera_serial(state)
            product_sessions.require_runnable_ladder(state)  # a refused fixed ladder never reruns
            current = self.status(session_id)
            if current.get("recovery_pending"):
                raise RuntimeError("another process holds this session; retry when it finishes")
            # Every frame is captured and only the merge or the publication failed: the retry re-runs
            # that step alone (product_sessions.run never opens the camera for a completed session).
            heavy = state.get("heavy_job") if isinstance(state.get("heavy_job"), dict) else {}
            processing_retry = (state["status"] == "completed"
                                and (heavy.get("status") == "failed"
                                     or (state.get("publication") or {}).get("status") == "failed"))
            if current.get("metadata_status") == "write_failed" and not processing_retry:
                raise RuntimeError("Could not persist recovered job receipt; camera replay is blocked")
            if (current.get("metadata_reconciled") and current["status"] in ("completed", "uncertain")
                    and not processing_retry):  # a failed merge may be re-queued; the camera is not used
                return current
            if current["status"] in ("queued", "running"):
                return current
            if any(f["status"] in ("capturing", "ambiguous") for f in state["frames"]):
                raise RuntimeError("ambiguous shutter blocks resubmission")
            if current["status"] == "cancelled" and state["status"] in ("ready", "cancelled", "failed", "restore_failed"):
                # A new explicit submission may resume a cancelled queued timer.
                self.runner_factory().resume(session_id)
            if sum(not f.done() for f in self._futures.values()) >= self.max_jobs:
                raise RuntimeError("session queue is full")
            # The persisted accepted frames remain authoritative when explicitly retried.
            job = self._save({"id": session_id, "operation_id": uuid.uuid4().hex,
                             "status": "queued", "submitted_at": time.time(),
                             "camera_model": metadata["camera_model"], "camera_port": metadata["camera_port"],
                             "camera_serial": metadata["camera_serial"],
                             "retry_safe": True, "delay_s": delay_s, "delay_seconds": delay_s,
                             "scheduled_at": time.time() + delay_s})
            self._submitted_monotonic[session_id] = time.monotonic()
            try:
                if delay_s:
                    # Delays reserve a bounded job slot, not the worker or camera.
                    self._futures[session_id] = concurrent.futures.Future()
                    timer = threading.Timer(delay_s, self._dispatch_delayed, args=(session_id, job["operation_id"]))
                    timer.daemon = True
                    timer.name = f"camera-session-timer-{session_id[:8]}"
                    self._timers[session_id] = timer
                    timer.start()
                else:
                    self._futures[session_id] = self._executor.submit(self._work, session_id, job["operation_id"])
            except Exception as exc:
                self._rejected_operations.add(job["operation_id"])
                timer = self._timers.pop(session_id, None)
                if timer:
                    timer.cancel()
                pending = self._futures.get(session_id)
                if pending and not pending.done():
                    pending.cancel()
                self._save({**job, "status": "failed", "retry_safe": True,
                            "error": f"Worker submission failed before camera access: {type(exc).__name__}: {exc}"})
                raise RuntimeError("session worker could not be submitted; no camera command was issued") from exc
            return job

    def _dispatch_delayed(self, session_id, operation_id):
        with self._lock:
            job = self.status(session_id)
            if job.get("operation_id") != operation_id:
                return
            self._timers.pop(session_id, None)
            pending = self._futures[session_id]
            if self._closing or pending.cancelled():
                return
            if job["status"] != "queued":
                pending.cancel()
                return
            try:
                self._futures[session_id] = self._executor.submit(self._work, session_id, job["operation_id"])
            except Exception as exc:
                self._rejected_operations.add(job["operation_id"])
                self._save({**job, "status": "failed", "retry_safe": True,
                            "error": f"Delayed worker submission failed before camera access: {type(exc).__name__}: {exc}"})
                pending.set_exception(exc)

    def _work(self, session_id, operation_id):
        with self._lock:
            job = self.status(session_id)
            if (job["status"] != "queued" or job.get("operation_id") != operation_id
                    or operation_id in self._rejected_operations or self._closing):
                return
            job.update(status="running", started_at=time.time(), retry_safe=False,
                       actual_delay_s=time.monotonic() - self._submitted_monotonic[session_id])
            self._save(job)
        try:
            with self.camera_exclusion():
                state = self.run_session(session_id)
            status = state["status"]
            ambiguous = any(f["status"] in ("capturing", "ambiguous") for f in state["frames"])
            job.update(status="uncertain" if ambiguous else status, session_status=status,
                       retry_safe=not ambiguous, finished_at=time.time(), error=state.get("error"))
            if isinstance(state.get("heavy_job"), dict):
                # Split runtime: capture is complete; the merge is the worker's (queued/running/failed).
                job["heavy_job"] = state["heavy_job"].get("status")
            if state.get("identity_refusal") and not ambiguous:
                job["identity_refusal"] = state["identity_refusal"]
        except Exception as exc:
            state = self.runner_factory().read(session_id)
            ambiguous = any(f["status"] in ("capturing", "ambiguous") for f in state["frames"])
            if getattr(exc, "identity_refused", False) is True and not ambiguous:
                # A camera identity refusal fired nothing: keep its structured fields.
                job.update(status="identity_refused", retry_safe=True, session_status=state["status"],
                           identity_refusal=exc.as_dict(), error=str(exc), finished_at=time.time())
            else:
                job.update(status="uncertain" if ambiguous else "failed", retry_safe=not ambiguous,
                           error=f"{type(exc).__name__}: {exc}", finished_at=time.time())
        with self._lock:
            try:
                self._save(job)
            except RuntimeError:
                # Another process (the split runtime's heavy-jobs worker) took the session lock
                # right after capture. The Future is done, so status() repairs the receipt from
                # the session manifest once the lock is free; the worker thread never raises.
                pass

    def cancel(self, session_id):
        with self._lock:
            if self.status(session_id).get("recovery_pending"):
                # The split runtime's worker holds the lock while it makes the result: say so first.
                refuse_cancel(self.runner_factory().read(session_id), split_control=runtime_roles.heavy_jobs_deferred())
                # Never persist the synthetic receipt; the real one is recovered when the lock frees.
                raise RuntimeError("another process holds this session; retry when it finishes")
            result = self.runner_factory().cancel(session_id, split_control=runtime_roles.heavy_jobs_deferred())
            job = self.status(session_id)
            if job["status"] == "queued":
                job.update(status="cancelled", finished_at=time.time())
                timer = self._timers.pop(session_id, None)
                if timer:
                    timer.cancel()
                future = self._futures.get(session_id)
                if future:
                    future.cancel()
            job["cancel_requested"] = True
            # The runner owns its operation lock during capture. Its cancel marker is
            # already durable; never wait for the full sequence to acknowledge it.
            saved = job if job["status"] == "running" else self._save(job)
            return {**saved, "note": result["note"]}

    def close(self, *, wait=True):
        """Stop intake and queued work; finish only the current physical exposure."""
        with self._lock:
            self._closing = True
            for session_id, future in self._futures.items():
                if not future.done():
                    try:
                        self.cancel(session_id)
                    except CancelRefused:
                        pass  # The capture is over (merging or queued); the result finishes on its own.
                    future.cancel()
        self._executor.shutdown(wait=wait, cancel_futures=True)
        # Keep the process lease until every worker is finished.
        if wait or all(f.done() for f in self._futures.values()):
            self._lease.close()
