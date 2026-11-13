"""F-75: Cancel is refused once a session is merging (CAP #15 follow-up).

On main, cancel() accepted a Cancel while the session was `merging`; run() never read the marker
again, so the merge still published and the session ended `completed`: the Cancel was silently
ignored. Now cancel() refuses while the session is merging (409, nothing changed), and run()
makes its last Cancel check and the durable `merging` status under the lock cancel() takes, so
a Cancel accepted while capturing still stops the session before the merge, as on main.
Camera-free: captures and merges are fakes, except the crowd early-stop case, which runs the
shipping crowd merge.
"""
from __future__ import annotations

import sys
import threading
from pathlib import Path

import cv2
import numpy as np
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))
import engine  # noqa: E402
import product_sessions  # noqa: E402
import stack_session  # noqa: E402
from stack_session import MERGE_RUNNING_DETAIL, MergeAlreadyRunning, merge_session_frames  # noqa: E402

from test_final_wave_product_api import product  # noqa: E402,F401  (pytest fixture import)

METADATA = {"camera_model": "Sony ILCE-7M2 (Control)", "camera_port": "usb:001,002", "camera_serial": "A7II00SERIAL"}
MERGING_MODES = [("hdr", 3), ("long_exposure", 3), ("night", 3), ("crowd_remove", 3), ("panorama", 3)]


@pytest.fixture
def sessions(tmp_path, monkeypatch):
    monkeypatch.setattr(engine, "SHOTS_DIR", tmp_path / "shots")
    return product_sessions.runner()


def _frame(index):
    image = np.full((120, 160, 3), 40 + 30 * index, np.uint8)
    cv2.circle(image, (30 + 20 * index, 60), 12, (200, 60, 60), -1)
    return image


def _capture(calls, on_capture=None):
    def capture(index, spec, destination):
        calls.append(index)
        if on_capture:
            on_capture(index)
        assert cv2.imwrite(destination, _frame(index), [cv2.IMWRITE_JPEG_QUALITY, 92])
        return destination
    return capture


def _merge(during=None, merges=None):
    """A merge that writes result.jpg; `during` runs while the session is merging."""
    def merge(paths, out):
        if merges is not None:
            merges.append(list(paths))
        if during:
            during()
        stacked = np.mean([cv2.imread(path).astype(np.float32) for path in paths], axis=0)
        assert cv2.imwrite(out, stacked.astype(np.uint8))
        return {"out_path": out, "frames_merged": len(paths)}
    return merge


@pytest.mark.parametrize("mode,frames", MERGING_MODES)
def test_cancel_while_merging_is_refused_and_the_merge_completes(sessions, mode, frames):
    state = sessions.create(mode, frames, metadata=METADATA)
    directory = sessions._directory(state["id"])
    refusals = []

    def cancel_while_merging():
        assert sessions.read(state["id"])["status"] == "merging"
        with pytest.raises(MergeAlreadyRunning) as refused:
            sessions.cancel(state["id"])
        refusals.append(str(refused.value))
        assert not (directory / "cancel").exists()   # nothing changed

    result = sessions.run(state["id"], _capture([]), _merge(cancel_while_merging))
    assert refusals == [MERGE_RUNNING_DETAIL]
    assert result["status"] == "completed", result.get("error")
    assert result["output"]["receipt"]["sha256"] == stack_session.frame_receipt(directory / "result.jpg")["sha256"]
    assert not (directory / "cancel").exists()


def test_cancel_is_refused_until_the_outcome_is_saved(sessions):
    """Restoration runs after the merge, before the outcome is saved: still no Cancel."""
    state = sessions.create("hdr", 3, metadata=METADATA)
    refused = []

    def cancel_during_restore():
        with pytest.raises(MergeAlreadyRunning):
            sessions.cancel(state["id"])
        refused.append(1)

    result = sessions.run(state["id"], _capture([]), _merge(), restore_settings=cancel_during_restore)
    assert refused == [1] and result["status"] == "completed"


def test_cancel_during_the_crowd_early_stop_merge_is_refused(sessions):
    """F-75 as found: Stop when clean, then Cancel while the accepted frames are merging."""
    state = sessions.create("crowd_remove", 8, metadata=METADATA)
    calls, refusals = [], []

    def press_stop(index):
        if index == 3:
            sessions.stop(state["id"])

    def crowd_merge(paths, out):
        with pytest.raises(MergeAlreadyRunning):
            sessions.cancel(state["id"])
        refusals.append(1)
        return merge_session_frames("crowd_remove", paths, out)

    result = sessions.run(state["id"], _capture(calls, press_stop), crowd_merge)
    assert refusals == [1] and calls == [0, 1, 2, 3]
    assert result["status"] == "completed", result.get("error")
    assert result["stopped_early"]["accepted_frames"] == 4 and result["output"]["details"]["frames_merged"] == 4


def test_cancel_while_capturing_is_unchanged(sessions):
    state = sessions.create("hdr", 3, metadata=METADATA)
    calls, merges, answers = [], [], []

    def cancel_at(index):
        if index == 1:
            answers.append(sessions.cancel(state["id"]))

    result = sessions.run(state["id"], _capture(calls, cancel_at), _merge(merges=merges))
    assert answers == [{"id": state["id"], "cancel_requested": True,
                        "note": "An exposure already in progress completes before cancellation."}]
    assert result["status"] == "cancelled" and calls == [0, 1] and merges == []
    assert result["output"] is None
    assert not (sessions._directory(state["id"]) / "result.jpg").exists()


def test_a_cancel_landing_just_before_the_merging_transition_stops_the_merge(sessions, monkeypatch):
    """Deterministic hook: the Cancel is accepted after the post-capture cancel check and before
    the session becomes merging. It must be honoured: no merge, the session ends cancelled."""
    state = sessions.create("hdr", 3, metadata=METADATA)
    real_check = stack_session.SessionRunner._stop_requested
    answers, merges = [], []

    def check_then_cancel(directory_, live):
        if not answers and all(frame["status"] == "accepted" for frame in live["frames"]):
            answers.append(sessions.cancel(state["id"]))  # after the loop's last cancel check
        return real_check(directory_, live)

    monkeypatch.setattr(stack_session.SessionRunner, "_stop_requested", staticmethod(check_then_cancel))
    result = sessions.run(state["id"], _capture([]), _merge(merges=merges))
    assert answers and answers[0]["cancel_requested"] is True
    assert result["status"] == "cancelled" and merges == [] and result["output"] is None
    assert not (sessions._directory(state["id"]) / "result.jpg").exists()
    assert all(frame["status"] == "accepted" for frame in result["frames"])


def test_a_cancel_racing_the_merging_transition_is_ordered_by_the_lock(sessions, monkeypatch):
    """Real threads: a Cancel issued while run() is writing the merging status must wait for that
    write and then be refused. If the check and the transition were not under cancel()'s lock, the
    Cancel would be accepted here and then ignored by the merge."""
    state = sessions.create("hdr", 3, metadata=METADATA)
    real_write = stack_session._write_manifest
    outcome, merges = {}, []
    racer = []

    def cancel_in_thread():
        try:
            outcome["answer"] = sessions.cancel(state["id"])
        except MergeAlreadyRunning as exc:
            outcome["refused"] = str(exc)

    def write_while_cancel_races(directory_, live):
        if live["status"] == "merging" and not racer:
            racer.append(threading.Thread(target=cancel_in_thread, name="test-cancel-racer"))
            racer[0].start()
            racer[0].join(timeout=0.5)  # with the lock held, the Cancel cannot finish meanwhile
            outcome["finished_before_write"] = not racer[0].is_alive()
        real_write(directory_, live)

    monkeypatch.setattr(stack_session, "_write_manifest", write_while_cancel_races)
    result = sessions.run(state["id"], _capture([]), _merge(merges=merges))
    racer[0].join(timeout=10)
    assert not racer[0].is_alive()
    assert outcome["finished_before_write"] is False
    assert outcome.get("refused") == MERGE_RUNNING_DETAIL and "answer" not in outcome
    assert result["status"] == "completed" and len(merges) == 1


def test_normal_completion_is_unchanged(sessions):
    state = sessions.create("night", 3, metadata=METADATA)
    directory = sessions._directory(state["id"])
    calls = []
    result = sessions.run(state["id"], _capture(calls), _merge())
    assert result["status"] == "completed" and calls == [0, 1, 2]
    assert result["output"]["details"] == {"out_path": str(directory / "result.jpg"), "frames_merged": 3}
    assert not (directory / "cancel").exists()


def test_api_answers_cancel_while_merging_with_409_and_the_result_publishes(product):  # noqa: F811
    from test_section1_api import _real_runner
    runner = _real_runner(product)
    state = runner.create("hdr", 3, metadata=METADATA)
    responses = []

    def cancel_over_http():
        responses.append(product.client.post(f"/sessions/{state['id']}/cancel", headers=product.headers))

    result = runner.run(state["id"], _capture([]), _merge(cancel_over_http))
    assert responses[0].status_code == 409, responses[0].text
    assert responses[0].json()["detail"] == MERGE_RUNNING_DETAIL
    assert result["status"] == "completed"
    published = product_sessions._publish(runner, runner.read(state["id"]))
    assert published["publication"]["status"] == "published", published.get("publication_error")


def test_scheduler_shutdown_tolerates_a_session_that_is_merging(sessions):
    import concurrent.futures
    from session_jobs import SessionJobs
    state = sessions.create("hdr", 3, metadata=METADATA)
    jobs = SessionJobs(lambda: sessions, lambda session_id: None)
    pending = concurrent.futures.Future()
    jobs._futures[state["id"]] = pending

    def shut_down_while_merging():
        jobs.close(wait=False)   # its Cancel is refused; the merge goes on

    result = sessions.run(state["id"], _capture([]), _merge(shut_down_while_merging))
    assert pending.cancelled() and result["status"] == "completed"
