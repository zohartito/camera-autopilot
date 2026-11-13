"""Crowd Control live preview, Stop when clean and the automatic cleared-share stop (capability 15).

No camera hardware: fake capture adapters write synthetic crowd frames (a static textured
background, passers-by that cross it, one person who lingers for three frames then leaves, and a
spot that a different person covers in every frame). The real estimator, 25% rule and merge run.
"""
from pathlib import Path
from types import SimpleNamespace
import json
import sys

import cv2
import numpy as np
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))
import crowd_auto_mask
import engine
import product_sessions
import stack_session
from stack_session import merge_session_frames

METADATA = {"camera_model": "Sony ILCE-7M2 (Control)", "camera_port": "usb:001,002", "camera_serial": "A7II00SERIAL"}
HEIGHT, WIDTH = 240, 320
NEVER = (slice(150, 200), slice(240, 300))    # covered by someone in every frame
LINGER = (slice(150, 200), slice(40, 100))    # occupied for frames 0-2, then revealed
# Saturated colours whose per-channel median is none of them, so no occupant equals the median.
CYCLE = [(200, 40, 40), (40, 200, 40), (40, 40, 200)]


def _background():
    rng = np.random.default_rng(7)
    base = rng.integers(40, 216, (HEIGHT // 8, WIDTH // 8, 3), dtype=np.uint8)
    base = cv2.resize(base, (WIDTH, HEIGHT), interpolation=cv2.INTER_CUBIC)
    for _ in range(40):
        x, y = int(rng.integers(0, WIDTH)), int(rng.integers(0, HEIGHT))
        cv2.circle(base, (x, y), int(rng.integers(3, 12)), tuple(int(c) for c in rng.integers(0, 255, 3)), 2)
    return base


BACKGROUND = _background()


def crowd_frame(index):
    image = BACKGROUND.copy()
    x0 = 10 + 38 * index  # one passer-by walks right, another walks left; neither returns
    cv2.rectangle(image, (x0, 10), (x0 + 30, 60), CYCLE[index % 3], -1)
    x1 = 280 - 38 * index
    cv2.rectangle(image, (x1, 80), (x1 + 30, 130), CYCLE[(index + 1) % 3], -1)
    image[NEVER] = CYCLE[index % 3]
    if index < 3:
        image[LINGER] = CYCLE[(index + 2) % 3]
    return image


def static_frame(index):
    return BACKGROUND.copy()


def leaving_frame(index):
    """The crowd walks through frames 0-2 and has gone from frame 3, so every spot it covered is
    seen clear again (crowd_clear_regions) and an automatic stop may fire."""
    return crowd_frame(index) if index < 3 else BACKGROUND.copy()


def _writer(make_frame, calls=None, on_capture=None):
    def capture(index, spec, destination):
        if calls is not None:
            calls.append(index)
        if on_capture:
            on_capture(index)
        assert cv2.imwrite(destination, make_frame(index), [cv2.IMWRITE_JPEG_QUALITY, 95])
        return destination
    return capture


def _crowd_merge(paths, out):
    return merge_session_frames("crowd_remove", paths, out)


@pytest.fixture
def sessions(tmp_path, monkeypatch):
    monkeypatch.setattr(engine, "SHOTS_DIR", tmp_path / "shots")
    return product_sessions.runner()


def _boundary(sessions, session_id, options, seen):
    def after(live):
        product_sessions._crowd_boundary(sessions, session_id, live, options)
        seen.append(dict(live.get("preview_crowd") or {}))
    return after


def _mask(directory, preview):
    return cv2.imread(str(directory / preview["mask"]), cv2.IMREAD_GRAYSCALE)


# --- 1. The live preview: share rises as frames accrue; never-revealed stays flagged -----------

def test_preview_share_rises_as_frames_accrue(sessions):
    """Reverting the crowd boundary (or the preview) leaves no preview_crowd and fails here."""
    state = sessions.create("crowd_remove", 8, metadata=METADATA)
    directory = sessions._directory(state["id"])
    seen = []
    options = {"preview_every": 1, "stop_when_cleared": None}
    result = sessions.run(state["id"], _writer(crowd_frame), _crowd_merge,
                          after_frame=_boundary(sessions, state["id"], options, seen))
    assert result["status"] == "completed", result.get("error")
    shares = {item["accepted_frames"]: item["cleared_share"] for item in seen if item}
    # Previews exist from 2 accepted frames up to one below the requested count (the merge is last).
    assert sorted(shares) == [2, 3, 4, 5, 6, 7]
    # Passers-by clear once a third frame outvotes them; the lingering person clears once enough
    # later frames show the ground behind them.
    assert shares[3] > shares[2] + 0.05
    assert shares[5] > shares[4] + 0.01
    assert shares[7] > shares[2]
    for item in seen:
        if item:
            assert item["not_cleared_share"] == pytest.approx(1 - item["cleared_share"])
            assert item["visibility_floor_frames"] == crowd_auto_mask.min_frames_for_visibility(item["accepted_frames"])
    preview = result["preview_crowd"]
    assert preview["accepted_frames"] == 7 and preview["frame_count"] == 7
    # Only the newest preview and mask remain; earlier snapshots were removed after publication.
    assert sorted(p.name for p in (directory / "crowd_preview").glob("preview_*.jpg")) == ["preview_0007.jpg"]
    assert sorted(p.name for p in (directory / "crowd_preview").glob("not_cleared_*.png")) == ["not_cleared_0007.png"]
    assert (_mask(directory, preview)[NEVER] == 255).mean() > 0.99
    assert (_mask(directory, preview)[LINGER] == 255).mean() < 0.05


def test_never_revealed_region_stays_flagged_in_every_preview_and_the_result(sessions):
    """A spot a *different* person covers in every frame never counts as cleared, live or final.

    This does not cover a subject who stands still in every frame: that is indistinguishable
    from background (crowd_auto_mask.STATIC_SUBJECT_LIMIT) and is counted as cleared.
    """
    state = sessions.create("crowd_remove", 6, metadata=METADATA)
    directory = sessions._directory(state["id"])
    masks = {}

    def after(live):
        product_sessions._crowd_boundary(sessions, state["id"], live, {"preview_every": 1, "stop_when_cleared": None})
        if live.get("preview_crowd"):
            masks[live["preview_crowd"]["accepted_frames"]] = _mask(directory, live["preview_crowd"])

    result = sessions.run(state["id"], _writer(crowd_frame), _crowd_merge, after_frame=after)
    assert result["status"] == "completed", result.get("error")
    assert sorted(masks) == [2, 3, 4, 5]
    for mask in masks.values():
        assert (mask[NEVER] == 255).mean() > 0.99
    final = cv2.imread(str(directory / "crowd_auto" / "not_cleared.png"), cv2.IMREAD_GRAYSCALE)
    assert (final[NEVER] == 255).mean() > 0.99
    never_share = (NEVER[0].stop - NEVER[0].start) * (NEVER[1].stop - NEVER[1].start) / (HEIGHT * WIDTH)
    assert result["output"]["details"]["not_cleared_fraction"] >= never_share * 0.99


# --- 2. Stop when clean: a clean stop that merges exactly the accepted frames ------------------

def test_manual_stop_merges_exactly_the_accepted_frames(sessions, tmp_path):
    """Reverting the runner's stop handling captures all 8 frames; this test pins 4."""
    state = sessions.create("crowd_remove", 8, metadata=METADATA)
    directory = sessions._directory(state["id"])
    calls = []

    def press_stop(index):
        if index == 3:  # the user presses Stop while frame 3 is being exposed
            sessions.stop(state["id"])

    result = sessions.run(state["id"], _writer(crowd_frame, calls, press_stop), _crowd_merge)
    assert result["status"] == "completed", result.get("error")
    assert calls == [0, 1, 2, 3]  # the in-flight exposure completes; no further shutter
    assert result["frame_count"] == 4 and result["requested_frame_count"] == 8
    assert [frame["index"] for frame in result["frames"]] == [0, 1, 2, 3]
    assert all(frame["status"] == "accepted" for frame in result["frames"])
    assert result["stopped_early"]["reason"] == "user"
    assert result["stopped_early"]["accepted_frames"] == 4 and result["stopped_early"]["requested_frames"] == 8
    assert result["output"]["details"]["frames_merged"] == 4
    assert not (directory / "frame_0004.jpg").exists()
    assert not (directory / "stop").exists()
    # The published result is exactly the merge of the four accepted frames.
    reference = merge_session_frames("crowd_remove", [f["destination"] for f in result["frames"]],
                                     str(tmp_path / "reference.jpg"))
    assert np.array_equal(cv2.imread(str(directory / "result.jpg")), cv2.imread(reference["out_path"]))
    assert result["progress"] == {"accepted": 4, "total": 4, "fraction": 1.0}
    # The truncated manifest is still a valid sidecar-format manifest (#94).
    assert result["evidence_format"] == stack_session.EVIDENCE_FORMAT
    detail = sessions.read_detail(state["id"])
    assert len(detail["frames"]) == 4 and detail["requested_frame_count"] == 8
    # Publication states the stop plainly.
    published = product_sessions._publish(sessions, sessions.read(state["id"]))
    assert published["publication"]["status"] == "published"
    sidecar = json.loads((engine.SHOTS_DIR / f"session_{state['id']}.json").read_text())
    assert "Stopped when you pressed Stop after 4 of 8" in sidecar["summary"]
    assert sidecar["evidence"]["stopped_early"]["accepted_frames"] == 4


def test_stop_with_fewer_than_two_frames_is_a_resumable_cancel(sessions):
    state = sessions.create("crowd_remove", 3, metadata=METADATA)
    calls = []

    def press_stop(index):
        if index == 0:
            sessions.stop(state["id"])

    stopped = sessions.run(state["id"], _writer(crowd_frame, calls, press_stop), _crowd_merge)
    assert calls == [0]
    assert stopped["status"] == "cancelled"
    assert stopped["stop_outcome"]["outcome"] == "cancelled_too_few_frames"
    assert stopped["frame_count"] == 3 and "stopped_early" not in stopped
    assert stopped["output"] is None
    # An explicit resume continues the same session; the accepted frame is never re-exposed.
    sessions.resume(state["id"])
    finished = sessions.run(state["id"], _writer(crowd_frame, calls), _crowd_merge)
    assert finished["status"] == "completed", finished.get("error")
    assert calls == [0, 1, 2]


def test_cancel_takes_precedence_over_stop(sessions):
    state = sessions.create("crowd_remove", 4, metadata=METADATA)

    def both(index):
        if index == 2:
            sessions.stop(state["id"])
            sessions.cancel(state["id"])

    result = sessions.run(state["id"], _writer(crowd_frame, None, both), _crowd_merge)
    assert result["status"] == "cancelled" and result["output"] is None
    assert "stopped_early" not in result and result["frame_count"] == 4


def test_stop_during_frame_preparation_fires_no_shutter(sessions):
    state = sessions.create("crowd_remove", 6, metadata=METADATA)
    calls, prepared = [], []

    def prepare(index, spec):
        prepared.append(index)
        if index == 3:  # Stop lands after the boundary check, while settings are being applied
            sessions.stop(state["id"])
        return {}

    result = sessions.run(state["id"], _writer(crowd_frame, calls), _crowd_merge, prepare_frame=prepare)
    assert result["status"] == "completed", result.get("error")
    assert prepared == [0, 1, 2, 3] and calls == [0, 1, 2]
    assert result["frame_count"] == 3 and result["stopped_early"]["accepted_frames"] == 3


def test_stop_during_the_last_progress_callback_fires_no_shutter(sessions):
    """Stop acknowledged while the post-preparation progress callback runs (after every earlier
    check passed): the frame must not be marked capturing and no further shutter may fire."""
    state = sessions.create("crowd_remove", 6, metadata=METADATA)
    calls, prepared = [], []

    def prepare(index, spec):
        prepared.append(index)
        return {}

    def progress(live):
        # The post-preparation call for frame 3: preparation has run for it, capture has not.
        if prepared and prepared[-1] == 3 and len(calls) == 3 and live["status"] == "running":
            if not (sessions._directory(state["id"]) / "stop").exists():
                sessions.stop(state["id"])

    result = sessions.run(state["id"], _writer(crowd_frame, calls), _crowd_merge,
                          prepare_frame=prepare, progress=progress)
    assert result["status"] == "completed", result.get("error")
    assert calls == [0, 1, 2]  # zero shutters after the acknowledged Stop
    assert prepared[-1] == 3
    assert result["stopped_early"]["reason"] == "user" and result["stopped_early"]["accepted_frames"] == 3
    assert result["frame_count"] == 3
    assert all(frame["status"] == "accepted" for frame in result["frames"])


def test_stop_racing_the_capture_transition_waits_for_the_durable_intent(sessions, monkeypatch):
    """stop() takes the same lock as the final check + capturing save, so a Stop that races the
    transition is ordered after it: that exposure is already in progress and is kept, and the
    Stop is honoured at the next boundary. It can never land between the check and the shutter."""
    import threading
    state = sessions.create("crowd_remove", 6, metadata=METADATA)
    directory = sessions._directory(state["id"])
    calls, racers, frame3_checks = [], [], []
    real_check = stack_session.SessionRunner._stop_requested

    def racing_check(directory_, live):
        seen = real_check(directory_, live)
        pending = [f for f in live["frames"] if f["status"] == "pending"]
        if live["status"] == "running" and pending and pending[0]["index"] == 3 and len(calls) == 3:
            frame3_checks.append(1)
        # Frame 3's first check is the boundary check; the second is the one inside the lock.
        if not racers and len(frame3_checks) == 2:
            racer = threading.Thread(target=sessions.stop, args=(state["id"],))
            racer.start()
            racer.join(0.3)
            racers.append(racer.is_alive())  # blocked: the transition holds the lock
            racers.append(racer)
        return seen

    monkeypatch.setattr(stack_session.SessionRunner, "_stop_requested", staticmethod(racing_check))
    result = sessions.run(state["id"], _writer(crowd_frame, calls), _crowd_merge)
    racers[1].join(5)
    assert not racers[1].is_alive()
    assert racers[0] is True
    assert calls == [0, 1, 2, 3]  # the in-progress exposure completes; nothing after it
    assert result["status"] == "completed", result.get("error")
    assert result["stopped_early"]["reason"] == "user" and result["frame_count"] == 4
    assert not (directory / "stop").exists()


def test_stop_is_refused_unless_the_session_is_capturing(sessions):
    state = sessions.create("crowd_remove", 4, metadata=METADATA)
    with pytest.raises(ValueError, match="needs a capturing session; this one is ready"):
        sessions.stop(state["id"])
    finished = sessions.run(state["id"], _writer(crowd_frame), _crowd_merge)
    assert finished["status"] == "completed"
    with pytest.raises(ValueError, match="this one is completed"):
        sessions.stop(state["id"])
    assert not (sessions._directory(state["id"]) / "stop").exists()


def test_preview_writes_replace_symlinks_instead_of_following_them(tmp_path):
    outside = tmp_path / "outside.png"
    outside.write_bytes(b"keep")
    sources = []
    for index in range(3):
        path = tmp_path / f"f{index}.jpg"
        cv2.imwrite(str(path), crowd_frame(index), [cv2.IMWRITE_JPEG_QUALITY, 95])
        sources.append(str(path))
    folder = tmp_path / "crowd_preview"
    folder.mkdir()
    for name in ("preview_0003.jpg", "not_cleared_0003.png", "src_0000.png"):
        (folder / name).symlink_to(outside)
    stack_session.crowd_preview_source(sources[0], folder / "src_0000.png")
    stack_session.crowd_preview(sources, folder / "preview_0003.jpg", folder / "not_cleared_0003.png")
    assert outside.read_bytes() == b"keep"
    for name in ("preview_0003.jpg", "not_cleared_0003.png", "src_0000.png"):
        assert not (folder / name).is_symlink() and (folder / name).stat().st_size > 0


def test_live_preview_passes_the_crowd_auto_memory_guard_before_decoding(tmp_path, monkeypatch):
    """The preview runs the same estimator as the merge, so it is refused by #87's upfront
    crowd_auto budget check before any pixel is decoded, and nothing is published."""
    import image_safety
    import stacking
    sources = []
    for index in range(3):
        path = tmp_path / f"f{index}.jpg"
        cv2.imwrite(str(path), crowd_frame(index), [cv2.IMWRITE_JPEG_QUALITY, 95])
        sources.append(str(path))
    width, height = 7728, 5152
    monkeypatch.setattr(image_safety, "image_size", lambda path: (width, height))
    monkeypatch.setattr(stacking, "MAX_STACK_WORKING_BYTES",
                        stacking.estimate_merge_peak("crowd_auto", width, height, 3) - 1)
    monkeypatch.setattr(stacking, "_read_one", lambda *args: pytest.fail("decoded before the memory guard"))
    with pytest.raises(ValueError, match="aggregate working-memory budget"):
        stack_session.crowd_preview(sources, tmp_path / "preview_0003.jpg", tmp_path / "not_cleared_0003.png")
    assert not (tmp_path / "preview_0003.jpg").exists() and not (tmp_path / "not_cleared_0003.png").exists()


def test_stop_is_refused_for_modes_whose_prefix_is_not_a_result(sessions):
    state = sessions.create("hdr", 3, metadata=METADATA)
    with pytest.raises(ValueError, match="crowd removal"):
        sessions.stop(state["id"])
    assert not (sessions._directory(state["id"]) / "stop").exists()


# --- 3. Automatic stop at the requested cleared share ----------------------------------------

def test_auto_stop_fires_at_the_threshold(sessions):
    """Reverting the auto-stop keeps shooting to 8; the stop lands on the first eligible frame."""
    state = sessions.create("crowd_remove", 8, metadata=METADATA)
    seen, calls = [], []
    options = {"preview_every": 1, "stop_when_cleared": 0.9}
    result = sessions.run(state["id"], _writer(leaving_frame, calls), _crowd_merge,
                          after_frame=_boundary(sessions, state["id"], options, seen))
    assert result["status"] == "completed", result.get("error")
    previews = {item["accepted_frames"]: item for item in seen if item}
    expected = next(count for count in sorted(previews)
                    if count >= product_sessions.CROWD_AUTO_STOP_MIN_FRAMES
                    and previews[count]["cleared_share"] >= 0.9 and previews[count]["regions"]["all_seen_clear"])
    assert expected < 8
    assert calls == list(range(expected))
    stop = result["stopped_early"]
    assert stop["reason"] == "cleared_share_threshold" and stop["threshold"] == 0.9
    assert stop["accepted_frames"] == expected and stop["cleared_share"] >= 0.9
    assert result["output"]["details"]["frames_merged"] == expected


def test_a_spot_never_seen_clear_blocks_the_stop_though_the_share_passes(sessions):
    """A different person covers NEVER in every frame: the share alone passes 0.9, but that spot
    was never seen clear, so no automatic stop (it would publish a median of passers-by there)."""
    state = sessions.create("crowd_remove", 8, metadata=METADATA)
    seen, calls = [], []
    options = {"preview_every": 1, "stop_when_cleared": 0.9}
    result = sessions.run(state["id"], _writer(crowd_frame, calls), _crowd_merge,
                          after_frame=_boundary(sessions, state["id"], options, seen))
    assert result["status"] == "completed", result.get("error")
    assert any(item["accepted_frames"] >= product_sessions.CROWD_AUTO_STOP_MIN_FRAMES
               and item["cleared_share"] >= 0.9 for item in seen if item)
    assert calls == list(range(8)) and "stopped_early" not in result


class _ProcessDied(BaseException):
    """Stands in for a hard kill: not an Exception, so no product handler records a failure."""


def test_auto_stop_survives_a_crash_right_after_the_preview_save(sessions, monkeypatch):
    """The qualifying preview and its stop decision are one durable write: a process that dies
    right after it recovers into the stop, never into the next shutter."""
    state = sessions.create("crowd_remove", 8, metadata=METADATA)
    real_save = sessions.save

    def save_then_die(live):
        real_save(live)
        if (live.get("preview_crowd") or {}).get("accepted_frames") == 5:
            raise _ProcessDied()

    monkeypatch.setattr(sessions, "save", save_then_die)
    calls = []
    options = {"preview_every": 1, "stop_when_cleared": 0.5}
    with pytest.raises(_ProcessDied):
        sessions.run(state["id"], _writer(static_frame, calls), _crowd_merge,
                     after_frame=_boundary(sessions, state["id"], options, []))
    assert calls == [0, 1, 2, 3, 4]
    persisted = json.loads((sessions._directory(state["id"]) / "manifest.json").read_text())
    assert persisted["stop_request"]["reason"] == "cleared_share_threshold"
    monkeypatch.setattr(sessions, "save", real_save)
    # Explicit recovery (a resubmitted job runs the runner again): no further shutter.
    recovered = sessions.run(state["id"], _writer(static_frame, calls), _crowd_merge,
                             after_frame=_boundary(sessions, state["id"], options, []))
    assert calls == [0, 1, 2, 3, 4]
    assert recovered["status"] == "completed", recovered.get("error")
    assert recovered["stopped_early"]["reason"] == "cleared_share_threshold"
    assert recovered["frame_count"] == 5 and recovered["output"]["details"]["frames_merged"] == 5


def test_stop_racing_the_early_stop_is_ordered_not_misrecorded(sessions, monkeypatch):
    """A Stop posted between _stop_early's marker check and its marker removal must not be
    acknowledged and then recorded as automatic: it waits on the lock, then finds the session
    already merging and is refused, so the automatic record is the truth."""
    import pathlib
    import threading
    state = sessions.create("crowd_remove", 8, metadata=METADATA)
    outcome, real_remove = {}, pathlib.Path.unlink

    def racing_remove(self, *args, **kwargs):
        if self.name == "stop" and "racer" not in outcome:
            def press():
                try:
                    outcome["ack"] = sessions.stop(state["id"])
                except ValueError as exc:
                    outcome["refused"] = str(exc)
            racer = threading.Thread(target=press)
            outcome["racer"] = racer
            racer.start()
            racer.join(0.3)
            outcome["blocked"] = racer.is_alive()
        return real_remove(self, *args, **kwargs)

    monkeypatch.setattr(pathlib.Path, "unlink", racing_remove)
    calls = []
    options = {"preview_every": 1, "stop_when_cleared": 0.5}
    result = sessions.run(state["id"], _writer(static_frame, calls), _crowd_merge,
                          after_frame=_boundary(sessions, state["id"], options, []))
    outcome["racer"].join(5)
    assert outcome["blocked"] is True  # stop() waits for the outcome to be committed
    assert "ack" not in outcome and "merging" in outcome["refused"]
    assert calls == [0, 1, 2, 3, 4]
    assert result["status"] == "completed", result.get("error")
    assert result["stopped_early"]["reason"] == "cleared_share_threshold"


def test_cancel_landing_before_the_early_stop_commit_wins(sessions, monkeypatch):
    """A Cancel acknowledged after the runner's cancel check but before the early-stop outcome is
    committed must be honoured: the session is cancelled (no merge, no result), its accepted frames
    are kept and it resumes as any cancelled session does."""
    state = sessions.create("crowd_remove", 8, metadata=METADATA)
    real_check = stack_session.SessionRunner._stop_requested
    fired = []

    def check_then_cancel(directory_, live):
        seen = real_check(directory_, live)
        pending = [f for f in live["frames"] if f["status"] == "pending"]
        if seen and pending and pending[0]["index"] == 5:
            fired.append(1)
            # The first such call is the loop-top break; the second is the post-loop decision, which
            # runs after the cancel check and before the early-stop lock is taken.
            if len(fired) == 2:
                sessions.cancel(state["id"])
        return seen

    monkeypatch.setattr(stack_session.SessionRunner, "_stop_requested", staticmethod(check_then_cancel))
    calls = []
    options = {"preview_every": 1, "stop_when_cleared": 0.5}
    result = sessions.run(state["id"], _writer(static_frame, calls), _crowd_merge,
                          after_frame=_boundary(sessions, state["id"], options, []))
    assert len(fired) == 2
    assert calls == [0, 1, 2, 3, 4]
    assert result["status"] == "cancelled"
    assert result["output"] is None and "stopped_early" not in result
    assert not (sessions._directory(state["id"]) / "result.jpg").exists()
    assert result["frame_count"] == 8 and sum(f["status"] == "accepted" for f in result["frames"]) == 5
    monkeypatch.setattr(stack_session.SessionRunner, "_stop_requested", staticmethod(real_check))
    assert sessions.resume(state["id"])["status"] == "ready"


def test_split_control_role_computes_no_crowd_preview_and_refuses_auto_stop(sessions, monkeypatch):
    """In CAMERA_AUTOPILOT_RUNTIME=split the camera-control process never runs the clean-plate
    estimator (stacking / crowd_auto_mask stay out of it); without a live share an automatic stop
    cannot work, so it is refused before any shutter. Manual Stop still works."""
    import runtime_roles
    monkeypatch.setenv(runtime_roles.ROLE_ENV, "control")
    assert runtime_roles.heavy_jobs_deferred()
    with pytest.raises(ValueError, match="split runtime"):
        product_sessions._crowd_options({"frame_count": 8, "metadata": {"stop_when_cleared": 0.9}})
    assert product_sessions._crowd_options({"frame_count": 8, "metadata": {}})["stop_when_cleared"] is None
    monkeypatch.setattr(stack_session, "crowd_preview",
                        lambda *args, **kwargs: pytest.fail("clean-plate preview ran in the control role"))
    monkeypatch.setattr(stack_session, "crowd_preview_source",
                        lambda *args, **kwargs: pytest.fail("preview source decoded in the control role"))
    state = sessions.create("crowd_remove", 4, metadata=METADATA)
    options = {"preview_every": 1, "stop_when_cleared": None}
    result = sessions.run(state["id"], _writer(static_frame), None,
                          after_frame=_boundary(sessions, state["id"], options, []))
    assert result["status"] == "completed", result.get("error")
    assert "preview_crowd" not in result and "preview_crowd_error" not in result


def test_split_control_process_crowd_session_loads_no_merge_code():
    """#77's module-absence check on a real crowd session in a fresh control-role process: a
    4-frame session never loads stacking / crowd_auto_mask (no live preview there), and an
    automatic-stop request is refused before its first shutter."""
    from test_runtime_split import _run_child, MODEL, PORT, SERIAL
    code = r'''
import json, sys, tempfile
from pathlib import Path
from types import SimpleNamespace
sys.path.insert(0, str(Path.cwd().parent))
import role_control
role_control.prepare()
from PIL import Image
import engine, product_sessions, runtime_roles, sonyremote, camera_caps
root = Path(tempfile.mkdtemp())
engine.SHOTS_DIR = root / "shots"
engine._free_usb = lambda: None
live = {"iso": "100", "aperture": "8", "shutter": "1/60", "focus_mode": "Automatic"}
class Camera:
    shots = 0
    def __init__(self, **kwargs): pass
    def __enter__(self): return self
    def __exit__(self, *args): pass
    def info(self): return SimpleNamespace(model="%(model)s", port="%(port)s")
    def wake(self): return True
    def shooting_mode(self): return "M"
    def get_settings(self): return dict(live)
    def list_options(self, key): return {"iso": ["100", "200"], "aperture": ["8"], "shutter": ["1/60"]}[key]
    def set_exposure(self, **kwargs): live.update(kwargs)
    def set_setting(self, key, value): live[key] = value
    def capture(self, **kwargs):
        type(self).shots += 1
        path = root / f"capture{self.shots}.jpg"
        Image.new("RGB", (64, 48), (70 + 30 * self.shots, 90, 100)).save(path)
        return str(path)
sonyremote.Camera = Camera
camera_caps.from_live_choices = lambda *a: SimpleNamespace(snap_shutter=lambda v: (v, "1/60"),
                                                          shutters=[1 / 8000, 1.0], shutter_labels=["1/8000", "1"])
sessions = product_sessions.runner()
meta = {"camera_model": "%(model)s", "camera_port": "%(port)s", "camera_serial": "%(serial)s"}
state = sessions.create("crowd_remove", 4, metadata=meta)
result = product_sessions.run(state["id"])
loaded = sorted(m for m in sys.modules if m.split(".")[0] in runtime_roles.CONTROL_ABSENT_MODULES)
auto = sessions.create("crowd_remove", 8, metadata={**meta, "stop_when_cleared": 0.9})
before = Camera.shots
try:
    product_sessions.run(auto["id"])
    refused = None
except ValueError as exc:
    refused = str(exc)
print(json.dumps({"loaded": loaded, "status": result["status"], "shots": before,
                  "preview": "preview_crowd" in result, "refused": refused,
                  "auto_shots": Camera.shots - before}))
''' % {"model": MODEL, "port": PORT, "serial": SERIAL}
    report = json.loads(_run_child(code))
    assert report["loaded"] == [] and report["status"] == "completed" and report["shots"] == 4
    assert report["preview"] is False
    assert report["refused"] and "split runtime" in report["refused"] and report["auto_shots"] == 0


def test_auto_stop_that_is_never_reached_shoots_the_full_budget(sessions):
    state = sessions.create("crowd_remove", 7, metadata=METADATA)
    calls = []
    options = {"preview_every": 1, "stop_when_cleared": 0.99}  # the never-revealed spot caps it
    result = sessions.run(state["id"], _writer(crowd_frame, calls), _crowd_merge,
                          after_frame=_boundary(sessions, state["id"], options, []))
    assert result["status"] == "completed", result.get("error")
    assert calls == list(range(7)) and "stopped_early" not in result
    assert result["output"]["details"]["frames_merged"] == 7


def test_auto_stop_waits_until_the_floor_needs_two_agreeing_frames(sessions):
    """A clean scene is 100% cleared from frame 2, yet one agreeing frame is not evidence."""
    assert product_sessions.CROWD_AUTO_STOP_MIN_FRAMES == 5
    assert crowd_auto_mask.min_frames_for_visibility(product_sessions.CROWD_AUTO_STOP_MIN_FRAMES) == 2
    assert crowd_auto_mask.min_frames_for_visibility(product_sessions.CROWD_AUTO_STOP_MIN_FRAMES - 1) == 1
    state = sessions.create("crowd_remove", 8, metadata=METADATA)
    seen, calls = [], []
    options = {"preview_every": 1, "stop_when_cleared": 0.5}
    result = sessions.run(state["id"], _writer(static_frame, calls), _crowd_merge,
                          after_frame=_boundary(sessions, state["id"], options, seen))
    assert result["status"] == "completed", result.get("error")
    assert all(item["cleared_share"] > 0.99 for item in seen if item)
    assert calls == [0, 1, 2, 3, 4]
    assert result["stopped_early"]["accepted_frames"] == 5


def test_manual_stop_during_preview_is_recorded_as_manual(sessions, monkeypatch):
    """Stop pressed while the preview that also meets the threshold is being computed: the
    person pressed Stop, so the record and summary must say so, not 'automatically'."""
    state = sessions.create("crowd_remove", 8, metadata=METADATA)
    real = product_sessions._incremental_crowd

    def preview_while_user_presses_stop(sessions_, live):
        report = real(sessions_, live)
        if report["accepted_frames"] == 5:
            sessions.stop(state["id"])
        return report

    monkeypatch.setattr(product_sessions, "_incremental_crowd", preview_while_user_presses_stop)
    calls = []
    options = {"preview_every": 1, "stop_when_cleared": 0.5}
    result = sessions.run(state["id"], _writer(static_frame, calls), _crowd_merge,
                          after_frame=_boundary(sessions, state["id"], options, []))
    assert result["status"] == "completed", result.get("error")
    assert calls == [0, 1, 2, 3, 4]
    assert result["stopped_early"]["reason"] == "user"
    assert "cleared_share" not in result["stopped_early"]


def test_preview_failure_never_blocks_or_stops(sessions, monkeypatch):
    def broken(*args, **kwargs):
        raise RuntimeError("preview decoder unavailable")
    monkeypatch.setattr(stack_session, "crowd_preview", broken)
    state = sessions.create("crowd_remove", 6, metadata=METADATA)
    calls = []
    options = {"preview_every": 1, "stop_when_cleared": 0.5}
    result = sessions.run(state["id"], _writer(static_frame, calls), _crowd_merge,
                          after_frame=_boundary(sessions, state["id"], options, []))
    assert result["status"] == "completed", result.get("error")
    assert calls == list(range(6)) and "stopped_early" not in result
    assert result["preview_crowd_error"]["accepted_frames_preserved"] is True
    assert "preview_crowd" not in result


def test_crowd_options_are_validated_before_any_shutter(sessions):
    def options(frames, **metadata):
        return product_sessions._crowd_options({"frame_count": frames, "metadata": metadata})
    assert options(4) == {"preview_every": 1, "stop_when_cleared": None}
    assert options(8, stop_when_cleared=0.95, preview_every=2) == {"preview_every": 2, "stop_when_cleared": 0.95}
    for bad in ({"preview_every": 13}, {"preview_every": True}, {"stop_when_cleared": 0.4},
                {"stop_when_cleared": 1.2}, {"stop_when_cleared": True}, {"stop_when_cleared": "0.9"}):
        with pytest.raises(ValueError):
            options(8, **bad)
    with pytest.raises(ValueError, match="at least 6 frames"):
        options(product_sessions.CROWD_AUTO_STOP_MIN_FRAMES, stop_when_cleared=0.9)
    # Previews (and so the threshold check) stop at 8 frames and a merge of 9+ has no visibility
    # report, so a larger budget could never stop on the rule; it is refused, not silently capped.
    assert stack_session.CROWD_AUTO_MAX_FRAMES == 8
    assert options(8, stop_when_cleared=0.9)["stop_when_cleared"] == 0.9
    for frames in (9, 16):
        with pytest.raises(ValueError, match="at most 8 frames"):
            options(frames, stop_when_cleared=0.9)
    assert options(16) == {"preview_every": 1, "stop_when_cleared": None}  # manual Stop still fine


# --- 4. The real run() path with a fake camera -----------------------------------------------

def test_product_run_auto_stops_and_publishes(sessions, monkeypatch):
    import sonyremote
    import camera_caps
    baseline = {"iso": "100", "aperture": "8", "shutter": "1/60", "focus_mode": "Manual"}
    live = dict(baseline)
    shots = []
    staging = engine.SHOTS_DIR.parent / "camera"
    staging.mkdir(parents=True)

    class Camera:
        def __init__(self, **kwargs): pass
        def __enter__(self): return self
        def __exit__(self, *args): pass
        def info(self): return SimpleNamespace(model=METADATA["camera_model"], port=METADATA["camera_port"])
        def wake(self): return True
        def shooting_mode(self): return "M"
        def get_settings(self): return dict(live)
        def list_options(self, key):
            return {"iso": ["100", "200"], "aperture": ["8"], "shutter": ["1/60"]}[key]
        def set_exposure(self, **kwargs): live.update(kwargs)
        def set_setting(self, key, value): live[key] = value
        def capture(self, **kwargs):
            path = staging / f"DSC{len(shots):04d}.JPG"
            cv2.imwrite(str(path), leaving_frame(len(shots)), [cv2.IMWRITE_JPEG_QUALITY, 95])
            shots.append(path)
            return str(path)

    monkeypatch.setattr(sonyremote, "Camera", Camera)
    monkeypatch.setattr(engine, "_free_usb", lambda: None)
    monkeypatch.setattr(camera_caps, "from_live_choices",
                        lambda *args: SimpleNamespace(snap_shutter=lambda value: (value, "1/60"),
                                                      # the live ladder's ends, which #86's precheck reads
                                                      shutters=[1 / 8000, 1.0], shutter_labels=["1/8000", "1"]))
    state = sessions.create("crowd_remove", 8, metadata={**METADATA, "stop_when_cleared": 0.9})
    result = product_sessions.run(state["id"])
    assert result["publication"]["status"] == "published", result.get("error") or result.get("publication_error")
    stop = result["stopped_early"]
    assert stop["reason"] == "cleared_share_threshold"
    assert len(shots) == stop["accepted_frames"] == result["frame_count"] < 8
    assert result["restore"] == "completed" and live == baseline
    # #94 compaction: each kept frame's camera evidence lives in a verified sidecar, the truncated
    # manifest reads back in detail, and the stop record and live preview survive compaction.
    detail = sessions.read_detail(state["id"])
    assert len(detail["frames"]) == result["frame_count"]
    assert all(frame.get("evidence_sidecar") and frame.get("applied") for frame in detail["frames"])
    assert not any("evidence_error" in frame for frame in detail["frames"])
    assert detail["stopped_early"] == stop and detail["preview_crowd"]["accepted_frames"] < 8
    sidecar = json.loads((engine.SHOTS_DIR / f"session_{state['id']}.json").read_text())
    assert "Stopped automatically at the requested cleared share" in sidecar["summary"]


def test_failed_merge_after_auto_stop_recovers_without_new_frames(sessions, monkeypatch):
    """The truncated manifest (frame_count 5) still carries the 8-frame budget's threshold."""
    import sonyremote
    import camera_caps
    live = {"iso": "100", "aperture": "8", "shutter": "1/60", "focus_mode": "Manual"}
    shots = []
    staging = engine.SHOTS_DIR.parent / "camera"
    staging.mkdir(parents=True)

    class Camera:
        def __init__(self, **kwargs): pass
        def __enter__(self): return self
        def __exit__(self, *args): pass
        def info(self): return SimpleNamespace(model=METADATA["camera_model"], port=METADATA["camera_port"])
        def wake(self): return True
        def shooting_mode(self): return "M"
        def get_settings(self): return dict(live)
        def list_options(self, key):
            return {"iso": ["100", "200"], "aperture": ["8"], "shutter": ["1/60"]}[key]
        def set_exposure(self, **kwargs): live.update(kwargs)
        def set_setting(self, key, value): live[key] = value
        def capture(self, **kwargs):
            path = staging / f"DSC{len(shots):04d}.JPG"
            cv2.imwrite(str(path), static_frame(len(shots)), [cv2.IMWRITE_JPEG_QUALITY, 95])
            shots.append(path)
            return str(path)

    monkeypatch.setattr(sonyremote, "Camera", Camera)
    monkeypatch.setattr(engine, "_free_usb", lambda: None)
    monkeypatch.setattr(camera_caps, "from_live_choices",
                        lambda *args: SimpleNamespace(snap_shutter=lambda value: (value, "1/60"),
                                                      # the live ladder's ends, which #86's precheck reads
                                                      shutters=[1 / 8000, 1.0], shutter_labels=["1/8000", "1"]))
    real_merge = product_sessions.merge_session_frames
    failures = []

    def flaky_merge(*args, **kwargs):
        if not failures:
            failures.append(1)
            raise OSError("disk full while merging")
        return real_merge(*args, **kwargs)

    monkeypatch.setattr(product_sessions, "merge_session_frames", flaky_merge)
    state = sessions.create("crowd_remove", 8, metadata={**METADATA, "stop_when_cleared": 0.9})
    failed = product_sessions.run(state["id"])
    assert failed["status"] == "failed" and "disk full" in failed["error"]
    assert failed["frame_count"] == 5 and failed["requested_frame_count"] == 8 and len(shots) == 5
    sessions.resume(state["id"])
    recovered = product_sessions.run(state["id"])
    assert recovered["publication"]["status"] == "published", recovered.get("error")
    assert len(shots) == 5  # the recovery merged; it never fired again
    assert recovered["output"]["details"]["frames_merged"] == 5


def test_product_run_rejects_bad_crowd_options_before_the_shutter(sessions, monkeypatch):
    import sonyremote
    opened = []
    monkeypatch.setattr(sonyremote, "Camera", lambda **kwargs: opened.append(kwargs))
    monkeypatch.setattr(engine, "_free_usb", lambda: None)
    state = sessions.create("crowd_remove", 4, metadata={**METADATA, "stop_when_cleared": 0.9})
    with pytest.raises(ValueError, match="at least 6 frames"):
        product_sessions.run(state["id"])
    beyond = sessions.create("crowd_remove", 12, metadata={**METADATA, "stop_when_cleared": 0.9})
    with pytest.raises(ValueError, match="at most 8 frames"):
        product_sessions.run(beyond["id"])
    assert opened == []
