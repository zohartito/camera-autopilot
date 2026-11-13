"""Split runtime: Cancel once capture is over is refused, as #134 refuses it while merging (F-75 follow-up).

With CAMERA_AUTOPILOT_RUNTIME=split the control role captures with no merger and queues a heavy job;
on main the session read `completed` (or still `running` during restoration) while the heavy-jobs role
had not yet made the result, a Cancel there was accepted (`cancel_requested: true`) and the worker then
merged and control published anyway. Now the deferred session is `merging` from run()'s last Cancel
check (taken under the lock cancel() takes) until it becomes `completed` with its heavy job queued in
the same manifest write; Cancel refuses `merging` and a queued/running heavy job with HTTP 409 "The
merge is already running; …" and changes nothing, and the result publishes exactly once. A queued job
that has not started is refused the same way. Camera-free: fake bodies, real merges of tiny JPEGs.
"""
from __future__ import annotations

import fcntl
import sys
import threading
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))
import engine  # noqa: E402
import heavy_jobs  # noqa: E402
import product_sessions  # noqa: E402
import runtime_roles  # noqa: E402
import stack_session  # noqa: E402
from stack_session import (MERGE_RUNNING_DETAIL, NOTHING_TO_CANCEL_DETAIL, MergeAlreadyRunning,  # noqa: E402
                           NothingToCancel)

from test_final_wave_product_api import product  # noqa: E402,F401  (pytest fixture import)
from test_runtime_split import _create, store  # noqa: E402,F401  (fake camera store fixture)


def _cancel_in_thread(sessions, session_id):
    """Cancel as another request thread would; returns ("accepted", answer) or ("refused", exception)."""
    outcome = []

    def press():
        try:
            outcome.append(("accepted", sessions.cancel(session_id)))
        except stack_session.CancelRefused as exc:
            outcome.append(("refused", exc))
    thread = threading.Thread(target=press, name="test-cancel")
    thread.start()
    thread.join(timeout=10)
    assert not thread.is_alive()
    return outcome[0]


class _Racer:
    """A Cancel in another thread. start() returns once the Cancel has tried the control lock (its first
    lock) without blocking; ``blocked`` is True only if that try failed, which proves the caller held
    the lock at that moment, before the caller's write. The Cancel then waits for the lock as usual."""
    NAME = "test-cancel-racer"

    def __init__(self, monkeypatch, sessions, session_id):
        self.sessions, self.session_id = sessions, session_id
        self.at_lock, self.outcome, self.thread, self.blocked = threading.Event(), {}, None, None
        self.contended = None
        real_flock = fcntl.flock

        def flock(fd, flags):
            if threading.current_thread().name == self.NAME and flags == fcntl.LOCK_EX and not self.at_lock.is_set():
                try:  # The control lock (cancel()'s first lock): observe contention, then wait as usual.
                    real_flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    self.contended = False
                except BlockingIOError:
                    self.contended = True
                self.at_lock.set()
                if not self.contended:
                    return None
            return real_flock(fd, flags)
        monkeypatch.setattr(stack_session, "fcntl", SimpleNamespace(
            flock=flock, LOCK_EX=fcntl.LOCK_EX, LOCK_NB=fcntl.LOCK_NB, LOCK_UN=fcntl.LOCK_UN))

    def _press(self):
        try:
            self.outcome["answer"] = self.sessions.cancel(self.session_id)
        except stack_session.CancelRefused as exc:
            self.outcome["refused"] = str(exc)

    def start(self):
        self.thread = threading.Thread(target=self._press, name=self.NAME)
        self.thread.start()
        reached = self.at_lock.wait(timeout=10)
        self.blocked = reached and self.contended  # a non-blocking try failed: the caller holds the lock

    def finish(self):
        self.thread.join(timeout=10)
        assert not self.thread.is_alive()
        return self.outcome


def _record_writes(monkeypatch, session_id):
    """(status, heavy_job status) of every manifest write of this session, in order."""
    real_write = stack_session._write_manifest
    writes = []

    def recording(directory, state):
        if state.get("id") == session_id:
            writes.append((state["status"], (state.get("heavy_job") or {}).get("status")))
        real_write(directory, state)
    monkeypatch.setattr(stack_session, "_write_manifest", recording)
    return writes


def _merge_and_publish(store, monkeypatch, session_id):
    monkeypatch.setenv(runtime_roles.ROLE_ENV, "jobs")
    assert heavy_jobs.run_pending(store.sessions) == [session_id]
    monkeypatch.setenv(runtime_roles.ROLE_ENV, "control")
    assert product_sessions.publish_deferred() == [session_id]
    assert product_sessions.publish_deferred() == []  # published exactly once
    final = store.sessions.read(session_id)
    assert final["status"] == "completed" and final["publication"]["status"] == "published"
    assert (engine.SHOTS_DIR / f"session_{session_id}.jpg").is_file()
    assert not (store.sessions._directory(session_id) / "cancel").exists()
    return final


@pytest.mark.parametrize("mode,frames", [("long_exposure", 2), ("hdr", 3), ("timelapse", 2)])
def test_cancel_during_restoration_and_while_queued_is_refused_and_the_result_publishes(store, monkeypatch,
                                                                                        mode, frames):
    """On main: a Cancel during restoration found the session `running` and was accepted, then the
    session completed, queued and published anyway."""
    import timelapse_video
    monkeypatch.setattr(timelapse_video, "probe_encoder", lambda: None)  # no host encoder dependency
    monkeypatch.setenv(runtime_roles.ROLE_ENV, "control")
    state = _create(store.sessions, mode, frames)
    directory = store.sessions._directory(state["id"])
    real_restore = product_sessions._restore
    during_restore = []

    def restore_after_a_cancel(cam, baseline):
        during_restore.append((store.sessions.read(state["id"])["status"], _cancel_in_thread(store.sessions, state["id"])))
        return real_restore(cam, baseline)
    monkeypatch.setattr(product_sessions, "_restore", restore_after_a_cancel)
    queued = product_sessions.run(state["id"])

    status, (verdict, refusal) = during_restore[0]
    assert status == "merging" and verdict == "refused" and isinstance(refusal, MergeAlreadyRunning)
    assert str(refusal) == MERGE_RUNNING_DETAIL
    assert queued["status"] == "completed" and queued["heavy_job"]["status"] == "queued"
    assert queued["restore"] == "completed" and not (directory / "cancel").exists()

    # Queued and not yet started: refused the same way, nothing changes.
    verdict, refusal = _cancel_in_thread(store.sessions, state["id"])
    assert verdict == "refused" and str(refusal) == MERGE_RUNNING_DETAIL
    assert store.sessions.read(state["id"]) == queued
    _merge_and_publish(store, monkeypatch, state["id"])


def test_completion_and_the_queued_job_are_one_manifest_write(store, monkeypatch):
    """No write ever shows `completed` without a queued job (a Cancel there would be accepted)."""
    monkeypatch.setenv(runtime_roles.ROLE_ENV, "control")
    state = _create(store.sessions, "hdr", 3)
    writes = _record_writes(monkeypatch, state["id"])
    product_sessions.run(state["id"])
    statuses = [status for status, _ in writes]
    first_done = statuses.index("merging")
    assert all(status == "running" for status in statuses[statuses.index("running"):first_done])
    assert set(statuses[first_done:]) == {"merging", "completed"}
    assert all(job == "queued" for status, job in writes if status == "completed")
    assert all(job is None for status, job in writes if status != "completed")


def test_cancel_while_the_worker_merges_and_before_it_commits_is_refused(store, monkeypatch):
    """Cancel racing the worker's start and its commit: both refused, the result publishes once."""
    monkeypatch.setenv(runtime_roles.ROLE_ENV, "control")
    state = _create(store.sessions)
    product_sessions.run(state["id"])
    real_merge = heavy_jobs._merge
    seen = []

    def merge_between_cancels(sessions, live):
        seen.append((sessions.read(state["id"])["heavy_job"]["status"], _cancel_in_thread(store.sessions, state["id"])))
        outputs = real_merge(sessions, live)
        assert (sessions._directory(state["id"]) / "result.jpg").is_file()  # made, not yet committed
        seen.append((sessions.read(state["id"])["heavy_job"]["status"], _cancel_in_thread(store.sessions, state["id"])))
        return outputs
    monkeypatch.setattr(heavy_jobs, "_merge", merge_between_cancels)
    final = _merge_and_publish(store, monkeypatch, state["id"])
    assert [(job, verdict, str(exc)) for job, (verdict, exc) in seen] == [
        ("running", "refused", MERGE_RUNNING_DETAIL), ("running", "refused", MERGE_RUNNING_DETAIL)]
    assert final["heavy_job"]["attempts"] == 1


def test_a_cancel_racing_the_merging_transition_is_ordered_by_the_lock(store, monkeypatch):
    """Real threads: a Cancel issued while run() writes the deferred `merging` status reaches the
    control lock, stays blocked until that write is done, and is refused. Without cancel()'s lock
    around the last Cancel check and that write, the Cancel would be accepted and the queued merge
    would publish anyway."""
    monkeypatch.setenv(runtime_roles.ROLE_ENV, "control")
    state = _create(store.sessions, "hdr", 3)
    real_write = stack_session._write_manifest
    racer = _Racer(monkeypatch, store.sessions, state["id"])

    def write_while_cancel_races(directory, live):
        if live.get("id") == state["id"] and live["status"] == "merging" and racer.thread is None:
            racer.start()
        real_write(directory, live)
    monkeypatch.setattr(stack_session, "_write_manifest", write_while_cancel_races)
    queued = product_sessions.run(state["id"])
    assert racer.thread is not None, "the deferred session never entered merging"
    outcome = racer.finish()
    assert racer.blocked is True
    assert outcome == {"refused": MERGE_RUNNING_DETAIL}
    assert queued["status"] == "completed" and queued["heavy_job"]["status"] == "queued"
    monkeypatch.setattr(stack_session, "_write_manifest", real_write)
    _merge_and_publish(store, monkeypatch, state["id"])


def test_a_cancel_racing_resume_is_never_acknowledged_and_then_erased(store, monkeypatch):
    """Resume clears a Cancel under the same lock: a Cancel pressed while Resume is clearing waits,
    lands after it and is honoured. On main Resume read the state, the Cancel was acknowledged, and
    Resume then deleted the marker: the resumed run queued, merged and published."""
    monkeypatch.setenv(runtime_roles.ROLE_ENV, "control")
    state = _create(store.sessions, "hdr", 3)
    real_restore = product_sessions._restore

    def restore_fails(cam, baseline):
        raise RuntimeError("simulated restoration failure")
    monkeypatch.setattr(product_sessions, "_restore", restore_fails)
    failed = product_sessions.run(state["id"])
    assert failed["status"] == "restore_failed" and "heavy_job" not in failed  # nothing queued
    assert all(frame["status"] == "accepted" for frame in failed["frames"])
    monkeypatch.setattr(product_sessions, "_restore", real_restore)

    real_write = stack_session._write_manifest
    racer = _Racer(monkeypatch, store.sessions, state["id"])

    def write_while_cancel_races(directory, live):
        if live.get("id") == state["id"] and live["status"] == "ready" and racer.thread is None:
            racer.start()
        real_write(directory, live)
    monkeypatch.setattr(stack_session, "_write_manifest", write_while_cancel_races)
    assert store.sessions.resume(state["id"])["status"] == "ready"
    outcome = racer.finish()
    monkeypatch.setattr(stack_session, "_write_manifest", real_write)
    assert racer.blocked is True and outcome["answer"]["cancel_requested"] is True
    result = product_sessions.run(state["id"])
    assert result["status"] == "cancelled" and "heavy_job" not in result
    monkeypatch.setenv(runtime_roles.ROLE_ENV, "jobs")
    assert heavy_jobs.run_pending(store.sessions) == []
    monkeypatch.setenv(runtime_roles.ROLE_ENV, "control")
    assert product_sessions.publish_deferred() == []
    assert not (engine.SHOTS_DIR / f"session_{state['id']}.jpg").exists()


def test_a_completed_session_written_before_its_job_was_queued_refuses_cancel(store, monkeypatch):
    """A split manifest left `completed` with no job (written before this change, or by a process
    killed between the two writes) is queued by its next run and published: the control role
    refuses Cancel for it as for every completed session."""
    from session_jobs import SessionJobs
    monkeypatch.setenv(runtime_roles.ROLE_ENV, "control")
    state = _create(store.sessions)
    record = product_sessions.run(state["id"])
    del record["heavy_job"]
    store.sessions.save(record)
    jobs = SessionJobs(product_sessions.runner, lambda session_id: None)
    try:
        with pytest.raises(NothingToCancel):
            jobs.cancel(state["id"])
    finally:
        jobs.close()
    requeued = product_sessions.run(state["id"])
    assert requeued["heavy_job"]["status"] == "queued"
    _merge_and_publish(store, monkeypatch, state["id"])


def test_a_cancel_landing_just_before_the_transition_cancels_with_nothing_queued(store, monkeypatch):
    """Deterministic hook: a Cancel accepted after the loop's last unlocked check and before the
    locked transition is honoured: the session ends cancelled, no job is queued, nothing publishes.
    On main the split session completed and queued its merge."""
    monkeypatch.setenv(runtime_roles.ROLE_ENV, "control")
    state = _create(store.sessions, "hdr", 3)
    real_check = stack_session.SessionRunner._stop_requested
    answers = []

    def check_then_cancel(directory, live):
        if not answers and all(frame["status"] == "accepted" for frame in live["frames"]):
            answers.append(store.sessions.cancel(state["id"]))
        return real_check(directory, live)
    monkeypatch.setattr(stack_session.SessionRunner, "_stop_requested", staticmethod(check_then_cancel))
    result = product_sessions.run(state["id"])
    assert answers and answers[0]["cancel_requested"] is True
    assert result["status"] == "cancelled" and "heavy_job" not in result and result["output"] is None
    assert all(frame["status"] == "accepted" for frame in result["frames"])
    monkeypatch.setenv(runtime_roles.ROLE_ENV, "jobs")
    assert heavy_jobs.run_pending(store.sessions) == []
    monkeypatch.setenv(runtime_roles.ROLE_ENV, "control")
    assert product_sessions.publish_deferred() == []
    assert not (store.sessions._directory(state["id"]) / "result.jpg").exists()
    assert not (engine.SHOTS_DIR / f"session_{state['id']}.jpg").exists()


def test_cancel_while_capturing_is_unchanged(store, monkeypatch):
    monkeypatch.setenv(runtime_roles.ROLE_ENV, "control")
    state = _create(store.sessions, "hdr", 3)
    real_capture = store.camera.capture
    answers = []

    def capture_then_cancel(self, **kwargs):
        path = real_capture(self, **kwargs)
        if not answers:
            answers.append(store.sessions.cancel(state["id"]))
        return path
    monkeypatch.setattr(store.camera, "capture", capture_then_cancel)
    result = product_sessions.run(state["id"])
    assert answers[0]["cancel_requested"] is True
    assert result["status"] == "cancelled" and "heavy_job" not in result


def test_a_failed_job_refuses_cancel_and_its_retry_publishes(store, monkeypatch):
    """Nothing runs after a failed job, but its retry publishes: an accepted Cancel would be ignored."""
    monkeypatch.setenv(runtime_roles.ROLE_ENV, "control")
    state = _create(store.sessions)
    product_sessions.run(state["id"])
    record = store.sessions.read(state["id"])
    record["heavy_job"].update(status="failed", error="RuntimeError: simulated")
    store.sessions.save(record)
    with pytest.raises(NothingToCancel, match="nothing to cancel"):
        store.sessions.cancel(state["id"])
    assert product_sessions.run(state["id"])["heavy_job"]["status"] == "queued"  # the retry, no camera
    _merge_and_publish(store, monkeypatch, state["id"])
    with pytest.raises(NothingToCancel):
        store.sessions.cancel(state["id"])


def test_scheduler_cancel_says_merging_while_the_worker_holds_the_session(store):
    """After a control restart the worker's lock makes the receipt recovery_pending; Cancel still
    answers with the merge refusal, not "retry when it finishes"."""
    from session_jobs import SessionJobs
    from test_runtime_split import _session_with_job
    state = _session_with_job(store.sessions, "running")
    record = store.sessions.read(state["id"])
    record.update(status="completed", heavy_job={"status": "running", "kind": "merge", "attempts": 1})
    store.sessions.save(record)
    with store.sessions.operation(state["id"]):  # the worker's lease
        jobs = SessionJobs(product_sessions.runner, lambda session_id: None)
        assert jobs.status(state["id"])["status"] == "recovery_pending"
        with pytest.raises(MergeAlreadyRunning, match="already running"):
            jobs.cancel(state["id"])
    assert not (store.sessions._directory(state["id"]) / "cancel").exists()
    jobs.close()


def test_in_process_runs_are_unchanged(store, monkeypatch):
    """Single process: no queued job; a merger enters merging (#134); a timelapse (no merger) never does."""
    monkeypatch.delenv(runtime_roles.ROLE_ENV, raising=False)
    merged = _create(store.sessions, "hdr", 3)
    writes = _record_writes(monkeypatch, merged["id"])
    result = product_sessions.run(merged["id"])
    assert result["status"] == "completed" and "heavy_job" not in result
    assert result["publication"]["status"] == "published"
    assert "merging" in [status for status, _ in writes] and all(job is None for _, job in writes)

    sequence = _create(store.sessions, "timelapse", 2)
    writes = _record_writes(monkeypatch, sequence["id"])
    result = product_sessions.run(sequence["id"])
    assert result["status"] == "completed" and "heavy_job" not in result
    assert "merging" not in [status for status, _ in writes] and all(job is None for _, job in writes)


def test_api_answers_409_while_queued_and_after_the_merge(product, monkeypatch):  # noqa: F811
    from test_section1_api import _real_runner
    runner = _real_runner(product)
    monkeypatch.setenv(runtime_roles.ROLE_ENV, "control")
    state = runner.create("hdr", 3, metadata={"camera_model": "Sony ILCE-7M2 (Control)", "camera_port": "usb:001,002",
                                              "camera_serial": "A7II00SERIAL"})
    from test_cancel_during_merge import _capture

    def cancel_over_http():
        return product.client.post(f"/sessions/{state['id']}/cancel", headers=product.headers)
    during_restore = []
    queued = runner.run(state["id"], _capture([]), None, queue_deferred_merge=heavy_jobs.queue,
                        restore_settings=lambda: during_restore.append(cancel_over_http()))
    assert during_restore[0].status_code == 409 and during_restore[0].json()["detail"] == MERGE_RUNNING_DETAIL
    assert queued["status"] == "completed" and queued["heavy_job"]["status"] == "queued"
    response = cancel_over_http()
    assert response.status_code == 409 and response.json()["detail"] == MERGE_RUNNING_DETAIL
    monkeypatch.setenv(runtime_roles.ROLE_ENV, "jobs")
    assert heavy_jobs.run_pending(runner) == [state["id"]]
    response = cancel_over_http()
    assert response.status_code == 409 and response.json()["detail"] == NOTHING_TO_CANCEL_DETAIL
    monkeypatch.setenv(runtime_roles.ROLE_ENV, "control")
    assert product_sessions.publish_deferred() == [state["id"]]
    assert runner.read(state["id"])["publication"]["status"] == "published"
    assert not (runner._directory(state["id"]) / "cancel").exists()
