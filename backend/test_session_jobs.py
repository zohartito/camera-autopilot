"""Background execution and restart proof without camera access."""
import contextlib
import json
import subprocess
import sys
import threading
import time
from pathlib import Path
import pytest
from PIL import Image
from stack_session import SessionRunner, _atomic_json
from session_jobs import SessionJobs

META = {"camera_model": "Sony ILCE-7M2 (Control)", "camera_port": "usb:001,002", "camera_serial": "A7II00SERIAL"}


def test_submit_returns_before_capture_and_survives_request_scope(tmp_path):
    runner = SessionRunner(tmp_path)
    state = runner.create("timelapse", 2, metadata=META)
    entered, release = threading.Event(), threading.Event()
    exclusions = []
    @contextlib.contextmanager
    def exclusion():
        exclusions.append("enter")
        yield
        exclusions.append("exit")
    def execute(sid):
        entered.set()
        assert release.wait(3)
        def capture(index, spec, destination):
            Image.new("RGB", (16, 16), (index, 2, 3)).save(destination)
            return destination
        return runner.run(sid, capture)
    jobs = SessionJobs(lambda: runner, execute, camera_exclusion=exclusion)
    try:
        before = time.monotonic()
        assert jobs.submit(state["id"])["status"] == "queued"
        assert time.monotonic() - before < .5
        assert entered.wait(1)
        assert jobs.submit(state["id"])["status"] == "running"
        # Original caller has returned; independent worker continues.
        release.set()
        jobs._futures[state["id"]].result(timeout=3)
        jobs.close()
        assert jobs.status(state["id"])["status"] == "completed"
        assert runner.read(state["id"])["progress"]["accepted"] == 2
        assert exclusions == ["enter", "exit"]
    finally:
        release.set()
        jobs.close()


def test_process_crash_recovers_uncertain_without_automatic_replay(tmp_path):
    runner = SessionRunner(tmp_path)
    state = runner.create("timelapse", 2, metadata=META)
    code = '''import os,sys
from stack_session import SessionRunner,_atomic_json
from session_jobs import SessionJobs
r=SessionRunner(sys.argv[1]); sid=sys.argv[2]
j=SessionJobs(lambda:r,lambda _:None)
s=r.read(sid); s['status']='running'; s['frames'][0]['status']='capturing'; r.save(s)
_atomic_json(r._directory(sid)/'job.json',{'id':sid,'status':'running'})
os._exit(19)
'''
    result = subprocess.run([sys.executable, "-c", code, str(tmp_path), state["id"]],
                            cwd=Path(__file__).parent, timeout=10)
    assert result.returncode == 19
    calls = []
    with pytest.raises(RuntimeError):
        # Explicit submit still cannot repeat an uncertain shutter after restart.
        jobs = SessionJobs(lambda: runner, lambda sid: calls.append(sid))
        try:
            assert jobs.status(state["id"])["status"] == "uncertain"
            jobs.submit(state["id"])
        finally:
            jobs.close()
    assert calls == []


def test_queued_restart_requires_explicit_retry(tmp_path):
    runner = SessionRunner(tmp_path)
    state = runner.create("timelapse", 2, metadata=META)
    _atomic_json(runner._directory(state["id"]) / "job.json", {"id": state["id"], "status": "queued"})
    calls = []
    jobs = SessionJobs(lambda: runner, lambda sid: calls.append(sid))
    assert jobs.status(state["id"])["status"] == "recovery_required"
    jobs.close()
    assert not calls


def test_queue_bound_and_queued_cancel(tmp_path):
    runner = SessionRunner(tmp_path)
    states = [runner.create("timelapse", 2, metadata=META) for _ in range(3)]
    entered, release = threading.Event(), threading.Event()
    calls = []
    def execute(sid):
        calls.append(sid)
        entered.set()
        release.wait(3)
        state = runner.read(sid)
        state["status"] = "cancelled"
        return state
    jobs = SessionJobs(lambda: runner, execute, max_jobs=2)
    try:
        jobs.submit(states[0]["id"])
        assert entered.wait(1)
        jobs.submit(states[1]["id"])
        with pytest.raises(RuntimeError, match="full"):
            jobs.submit(states[2]["id"])
        assert jobs.cancel(states[1]["id"])["status"] == "cancelled"
    finally:
        release.set()
        jobs.close()
    assert calls == [states[0]["id"]]


def test_scheduler_exclusive_lease(tmp_path):
    runner = SessionRunner(tmp_path)
    jobs = SessionJobs(lambda: runner, lambda _: None)
    try:
        with pytest.raises(RuntimeError, match="owns"):
            SessionJobs(lambda: runner, lambda _: None)
    finally:
        jobs.close()


def test_active_job_blocks_direct_session_deletion(tmp_path):
    runner = SessionRunner(tmp_path)
    state = runner.create("timelapse", 2, metadata=META)
    _atomic_json(runner._directory(state["id"]) / "job.json", {"id": state["id"], "status": "queued"})
    with pytest.raises(RuntimeError, match="active background"):
        runner.delete(state["id"])
    assert runner.read(state["id"])["id"] == state["id"]


def test_submit_second_and_cancel_first_do_not_wait_for_capture_lifecycle_lock(tmp_path):
    from photo_lifecycle import _LOCK
    runner = SessionRunner(tmp_path)
    first = runner.create("timelapse", 2, metadata=META)
    second = runner.create("timelapse", 2, metadata=META)
    entered, release = threading.Event(), threading.Event()
    def execute(sid):
        with _LOCK:
            entered.set()
            assert release.wait(3)
            state = runner.read(sid)
            state["status"] = "cancelled"
            return state
    jobs = SessionJobs(lambda: runner, execute)
    try:
        jobs.submit(first["id"])
        assert entered.wait(1)
        before = time.monotonic()
        assert jobs.submit(second["id"])["status"] == "queued"
        assert time.monotonic() - before < .5
        before = time.monotonic()
        assert jobs.cancel(first["id"])["cancel_requested"]
        assert time.monotonic() - before < .5
        jobs.cancel(second["id"])
    finally:
        release.set()
        jobs.close()


def test_shutdown_cancels_queue_and_running_sequence_before_next_shutter(tmp_path):
    runner = SessionRunner(tmp_path)
    first = runner.create("timelapse", 3, metadata=META)
    second = runner.create("timelapse", 3, metadata=META)
    entered, release, closed = threading.Event(), threading.Event(), threading.Event()
    captures = []
    def execute(sid):
        def capture(index, spec, destination):
            captures.append((sid, index))
            entered.set()
            assert release.wait(3)
            Image.new("RGB", (16, 16), (index, 2, 3)).save(destination)
            return destination
        return runner.run(sid, capture)
    jobs = SessionJobs(lambda: runner, execute)
    jobs.submit(first["id"])
    assert entered.wait(1)
    jobs.submit(second["id"])
    shutdown = threading.Thread(target=lambda: (jobs.close(), closed.set()))
    shutdown.start()
    try:
        deadline = time.monotonic() + 1
        while not (runner._directory(first["id"]) / "cancel").exists() and time.monotonic() < deadline:
            time.sleep(.01)
        assert (runner._directory(first["id"]) / "cancel").exists()
        with pytest.raises(RuntimeError, match="shutting down"):
            jobs.submit(second["id"])
    finally:
        release.set()
        shutdown.join(3)
    assert closed.is_set()
    assert captures == [(first["id"], 0)]
    assert jobs.status(first["id"])["status"] == "cancelled"
    assert jobs.status(second["id"])["status"] == "cancelled"


def test_executor_submission_failure_is_durable_and_does_not_leave_queued_job(tmp_path, monkeypatch):
    runner = SessionRunner(tmp_path)
    state = runner.create("timelapse", 2, metadata=META)
    calls = []
    jobs = SessionJobs(lambda: runner, lambda sid: calls.append(sid))
    def fail(*args, **kwargs):
        raise RuntimeError("simulated executor submission failure")
    monkeypatch.setattr(jobs._executor, "submit", fail)
    try:
        with pytest.raises(RuntimeError, match="no camera command"):
            jobs.submit(state["id"])
        job = jobs.status(state["id"])
        assert job["status"] == "failed" and job["retry_safe"]
        assert calls == []
    finally:
        jobs.close()


def test_delayed_job_does_not_occupy_camera_worker_and_can_cancel(tmp_path):
    runner = SessionRunner(tmp_path)
    delayed = runner.create("timelapse", 2, metadata=META)
    immediate = runner.create("timelapse", 2, metadata=META)
    calls = []
    done = threading.Event()
    def execute(sid):
        calls.append(sid)
        state = runner.read(sid)
        state["status"] = "completed"
        done.set()
        return state
    jobs = SessionJobs(lambda: runner, execute)
    try:
        job = jobs.submit(delayed["id"], delay_s=36000)
        assert job["scheduled_at"] - job["submitted_at"] >= 35999
        jobs.submit(immediate["id"])
        assert done.wait(1)
        jobs._futures[immediate["id"]].result(timeout=1)
        assert calls == [immediate["id"]]
        assert jobs.cancel(delayed["id"])["status"] == "cancelled"
    finally:
        jobs.close()
    assert not jobs._timers


def test_one_second_delay_dispatches_once_and_persists_schedule(tmp_path):
    runner = SessionRunner(tmp_path)
    state = runner.create("timelapse", 2, metadata=META)
    fired = threading.Event()
    calls = []
    started = time.monotonic()
    def execute(sid):
        calls.append(time.monotonic() - started)
        result = runner.read(sid)
        result["status"] = "completed"
        fired.set()
        return result
    jobs = SessionJobs(lambda: runner, execute)
    try:
        jobs.submit(state["id"], delay_s=1)
        assert not fired.wait(.1)
        assert fired.wait(2)
        jobs._futures[state["id"]].result(timeout=1)
        assert len(calls) == 1 and calls[0] >= .95
        assert jobs.status(state["id"])["delay_s"] == 1
        assert jobs.status(state["id"])["actual_delay_s"] >= .95
    finally:
        jobs.close()


@pytest.mark.parametrize("delay", [True, -1, .5, 36001, float("nan")])
def test_invalid_delays_never_enqueue(tmp_path, delay):
    runner = SessionRunner(tmp_path)
    state = runner.create("timelapse", 2, metadata=META)
    jobs = SessionJobs(lambda: runner, lambda _: pytest.fail("must not execute"))
    try:
        with pytest.raises(ValueError):
            jobs.submit(state["id"], delay_s=delay)
        assert jobs.status(state["id"])["status"] == "not_submitted"
    finally:
        jobs.close()


def test_live_delay_uses_monotonic_wait_despite_wall_clock_rollback(tmp_path, monkeypatch):
    import session_jobs
    runner = SessionRunner(tmp_path)
    state = runner.create("timelapse", 2, metadata=META)
    fired = threading.Event()
    def execute(sid):
        result = runner.read(sid)
        result["status"] = "completed"
        fired.set()
        return result
    jobs = SessionJobs(lambda: runner, execute)
    try:
        job = jobs.submit(state["id"], delay_s=1)
        original = time.time
        monkeypatch.setattr(session_jobs.time, "time", lambda: original() - 86400)
        assert fired.wait(2)
        jobs._futures[state["id"]].result(timeout=1)
        assert jobs.status(state["id"])["scheduled_at"] == job["scheduled_at"]
    finally:
        jobs.close()


def test_delayed_queued_restart_does_not_dispatch_even_when_overdue(tmp_path):
    runner = SessionRunner(tmp_path)
    state = runner.create("timelapse", 2, metadata=META)
    _atomic_json(runner._directory(state["id"]) / "job.json", {
        "id": state["id"], "status": "queued", "delay_seconds": 36000,
        "scheduled_at": time.time() - 1})
    jobs = SessionJobs(lambda: runner, lambda _: pytest.fail("restart may not dispatch overdue work"))
    try:
        assert jobs.status(state["id"])["status"] == "recovery_required"
        assert jobs._timers == {} and jobs._futures == {}
    finally:
        jobs.close()


def test_explicit_resubmit_after_delayed_cancel_clears_cancel_marker(tmp_path):
    runner = SessionRunner(tmp_path)
    state = runner.create("timelapse", 2, metadata=META)
    def execute(sid):
        def capture(index, spec, destination):
            Image.new("RGB", (16, 16), (index, 2, 3)).save(destination)
            return destination
        return runner.run(sid, capture)
    jobs = SessionJobs(lambda: runner, execute)
    try:
        jobs.submit(state["id"], delay_s=36000)
        jobs.cancel(state["id"])
        jobs.submit(state["id"])
        jobs._futures[state["id"]].result(timeout=2)
        assert jobs.status(state["id"])["status"] == "completed"
        assert runner.read(state["id"])["progress"]["accepted"] == 2
    finally:
        jobs.close()


def test_stale_timer_and_worker_cannot_execute_a_later_submission(tmp_path):
    runner = SessionRunner(tmp_path)
    state = runner.create("timelapse", 2, metadata=META)
    calls = []
    jobs = SessionJobs(lambda: runner, lambda sid: calls.append(sid))
    try:
        old = jobs.submit(state["id"], delay_s=36000)
        jobs.cancel(state["id"])
        current = jobs.submit(state["id"], delay_s=36000)
        jobs._dispatch_delayed(state["id"], old["operation_id"])
        jobs._work(state["id"], old["operation_id"])
        assert jobs.status(state["id"])["operation_id"] == current["operation_id"]
        assert jobs.status(state["id"])["status"] == "queued"
        assert calls == []
    finally:
        jobs.close()


def test_submit_failure_after_enqueue_cannot_execute_camera_work(tmp_path, monkeypatch):
    runner = SessionRunner(tmp_path)
    state = runner.create("timelapse", 2, metadata=META)
    calls, enqueued = [], []
    jobs = SessionJobs(lambda: runner, lambda sid: calls.append(sid))
    def fail_after_enqueue(fn, *args):
        enqueued.append((fn, args))
        raise RuntimeError("thread creation failed after internal enqueue")
    monkeypatch.setattr(jobs._executor, "submit", fail_after_enqueue)
    try:
        with pytest.raises(RuntimeError):
            jobs.submit(state["id"])
        fn, args = enqueued[0]
        fn(*args)
        assert calls == []
        assert jobs.status(state["id"])["status"] == "failed"
    finally:
        jobs.close()


@pytest.mark.parametrize("persistent", [False, True])
def test_final_job_receipt_failure_reconciles_metadata_without_camera_replay(tmp_path, monkeypatch, persistent):
    runner = SessionRunner(tmp_path)
    state = runner.create("timelapse", 2, metadata=META)
    calls = []
    def execute(sid):
        calls.append(sid)
        def capture(index, spec, destination):
            Image.new("RGB", (16, 16), (index, 2, 3)).save(destination)
            return destination
        return runner.run(sid, capture)
    jobs = SessionJobs(lambda: runner, execute)
    original = jobs._save
    failures = []
    def save(job):
        if job["status"] == "completed" and (persistent or not failures):
            failures.append(1)
            raise OSError("simulated full disk for final job metadata")
        return original(job)
    monkeypatch.setattr(jobs, "_save", save)
    try:
        jobs.submit(state["id"])
        with pytest.raises(OSError):
            jobs._futures[state["id"]].result(timeout=2)
        result = jobs.status(state["id"])
        if persistent:
            assert result["status"] == "recovery_required"
            assert result["metadata_status"] == "write_failed"
            with pytest.raises(RuntimeError, match="camera replay is blocked"):
                jobs.submit(state["id"])
        else:
            assert result["status"] == "completed" and result["metadata_reconciled"]
            assert jobs.submit(state["id"])["status"] == "completed"
        assert runner.read(state["id"])["progress"]["accepted"] == 2
        assert calls == [state["id"]]
    finally:
        monkeypatch.setattr(jobs, "_save", original)
        jobs.close()
