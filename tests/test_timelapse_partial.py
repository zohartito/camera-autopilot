"""Partial timelapse movie for a session blocked by an ambiguous frame (owner decision 2026-10-02).

Camera-free: synthetic JPEGs, a fake encoder for the bookkeeping, the real ffmpeg (skipped when it
does not execute) for the movie itself. Every test arms a camera stand-in that fails on any use.
"""
from __future__ import annotations

import hashlib
import json
import subprocess
import sys
from pathlib import Path

import pytest
from PIL import Image

ROOT = Path(__file__).resolve().parents[1]
for entry in (ROOT, ROOT / "backend"):
    sys.path.insert(0, str(entry))

import engine  # noqa: E402
import heavy_jobs  # noqa: E402
import product_sessions  # noqa: E402
import stack_session  # noqa: E402
import timelapse_partial  # noqa: E402
from timelapse_partial import PartialRefused  # noqa: E402

SERIAL = "PARTIAL-TEST-SERIAL"
# The real capture loop builds the blocked sessions; the guards below make every later call fail.
_REAL_RUN = stack_session.SessionRunner.run


def _sha(path) -> str:
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


class _Exploding:
    def __init__(self, *args, **kwargs):
        raise AssertionError("a partial encode must never open a camera")


@pytest.fixture
def sessions(tmp_path, monkeypatch):
    import sonyremote

    monkeypatch.setattr(engine, "SHOTS_DIR", tmp_path / "shots")
    # Any camera, capture or session-run path explodes: the partial encode uses none of them.
    monkeypatch.setattr(sonyremote, "Camera", _Exploding)
    monkeypatch.setattr(engine, "_free_usb", lambda: pytest.fail("USB touched"))
    monkeypatch.setattr(product_sessions, "run", lambda *a, **k: pytest.fail("session run called"))
    monkeypatch.setattr(stack_session.SessionRunner, "run", lambda *a, **k: pytest.fail("capture loop called"))
    monkeypatch.setattr(stack_session.SessionRunner, "resume", lambda *a, **k: pytest.fail("resume called"))
    return product_sessions.runner()


def _shade(index: int) -> int:
    return 40 + 25 * index


def _blocked(tmp_path, frames=6, ambiguous_at=4, policy="fixed", mode="timelapse", controls=None):
    """A session left in needs_reconciliation at ``ambiguous_at`` by the real capture loop."""
    runner = stack_session.SessionRunner(tmp_path / "shots" / "sessions")
    kwargs = {"timelapse_mode": policy} if mode == "timelapse" else {}
    state = runner.create(mode, frames, metadata={"camera_serial": SERIAL}, **kwargs)
    if controls is not None:
        import timelapse_controls
        timelapse_controls.update_controls(runner, state["id"], controls)

    def capture(index, spec, dest):
        Image.new("RGB", (96, 64), (_shade(index),) * 3).save(dest, format="JPEG", quality=95)
        if index == ambiguous_at:
            return None  # the bytes may have arrived, but the outcome is uncertain
        return str(dest)

    _REAL_RUN(runner, state["id"], capture, None if mode == "timelapse" else (lambda paths, dest: None))
    blocked = runner.read(state["id"])
    assert blocked["status"] == "needs_reconciliation"
    assert blocked["frames"][ambiguous_at]["status"] == "ambiguous"
    return blocked


@pytest.fixture
def blocked_session(tmp_path):
    return _blocked(tmp_path)


def _fake_encoder(monkeypatch, calls):
    """ffmpeg stand-in that records the exact bytes it was handed, frame by frame."""
    import timelapse_video

    def render(frames_dir, frame_count, fps, pending, *, preview=False, tools=None):
        inputs = [Path(frames_dir) / f"frame_{position:04d}.jpg" for position in range(frame_count)]
        calls.append({"hashes": [_sha(path) for path in inputs], "fps": fps,
                      "listed": sorted(path.name for path in Path(frames_dir).iterdir())})
        Path(pending).write_bytes(b"\x00\x00\x00\x18ftypmp42" + bytes([frame_count]) * 64)
        return {"codec_name": "h264", "width": 1920, "height": 1080, "nb_read_frames": str(frame_count),
                "playback_duration_s": frame_count / fps}

    monkeypatch.setattr(timelapse_video, "probe_encoder", lambda: {"ffmpeg": "ffmpeg", "ffprobe": "ffprobe"})
    monkeypatch.setattr(timelapse_video, "render_h264", render)


def test_encodes_exactly_the_accepted_frames_and_the_session_stays_blocked(sessions, blocked_session, monkeypatch):
    calls = []
    _fake_encoder(monkeypatch, calls)
    session_id = blocked_session["id"]
    directory = sessions._directory(session_id)
    frames_before = json.loads((directory / "manifest.json").read_text())["frames"]
    accepted = [frame["receipt"]["sha256"] for frame in blocked_session["frames"][:4]]
    ambiguous_bytes = _sha(directory / "frame_0004.jpg")

    result = product_sessions.encode_partial(session_id)

    assert len(calls) == 1
    assert calls[0]["hashes"] == accepted  # exactly frames 0-3, in index order
    assert ambiguous_bytes not in calls[0]["hashes"]
    assert calls[0]["listed"] == [f"frame_{i:04d}.jpg" for i in range(4)]
    assert calls[0]["fps"] == blocked_session["timelapse"]["fps"]
    assert result["status"] == "needs_reconciliation"
    assert [frame["status"] for frame in result["frames"]] == ["accepted"] * 4 + ["ambiguous", "pending"]
    assert json.loads((directory / "manifest.json").read_text())["frames"] == frames_before
    assert result.get("video") is None and result.get("sequence") is None and not result.get("publication")

    record = result["partial_video"]
    assert record["status"] == "completed" and record["partial"] is True
    assert record["frame_indices"] == [0, 1, 2, 3] and record["frame_count"] == 4
    assert record["ambiguous_indices"] == [4]
    assert record["url"] == f"/sessions/{session_id}/artifacts/timelapse_partial.mp4"
    movie = directory / "timelapse_partial.mp4"
    receipt_file = directory / "timelapse_partial.json"
    assert record["receipt"]["sha256"] == _sha(movie)
    assert record["receipt_file"]["receipt"]["sha256"] == _sha(receipt_file)
    document = json.loads(receipt_file.read_text())
    assert document["frame_indices"] == [0, 1, 2, 3] and document["ambiguous_indices"] == [4]
    assert [item["sha256"] for item in document["inputs"]] == accepted
    assert all(_sha(directory / item["file"]) == item["sha256"] for item in document["inputs"])
    assert document["output"]["sha256"] == _sha(movie)
    assert document["camera_opened"] is False and document["shutter_fired"] is False
    assert document["pending_frames_not_shot"] == 1
    # Both files are hash-verified session artifacts; the render folder and pending file are gone.
    assert sessions.artifact(session_id, "timelapse_partial.mp4") == movie
    assert sessions.artifact(session_id, "timelapse_partial.json") == receipt_file
    assert not (directory / "partial_frames").exists()
    assert not (directory / "timelapse_partial.pending.mp4").exists()

    # A second request re-verifies and returns; it never encodes again.
    again = product_sessions.encode_partial(session_id)
    assert len(calls) == 1 and again["partial_video"] == record


def test_a_changed_partial_movie_is_no_longer_served(sessions, blocked_session, monkeypatch):
    _fake_encoder(monkeypatch, [])
    session_id = blocked_session["id"]
    product_sessions.encode_partial(session_id)
    (sessions._directory(session_id) / "timelapse_partial.mp4").write_bytes(b"replaced")
    with pytest.raises(ValueError, match="changed after acceptance"):
        sessions.artifact(session_id, "timelapse_partial.mp4")


def test_real_encoder_partial_movie_holy_grail_deflickered(sessions, tmp_path, monkeypatch):
    from timelapse_video import probe_encoder
    tools = probe_encoder()
    if tools is None:
        pytest.skip("ffmpeg/ffprobe did not execute (-version); not scored here")
    blocked = _blocked(tmp_path, frames=5, ambiguous_at=3, policy="holy_grail")
    result = product_sessions.encode_partial(blocked["id"])
    record = result["partial_video"]
    assert record["status"] == "completed", record
    assert result["status"] == "needs_reconciliation"
    # Holy Grail deflickers by default, exactly as the completed render would.
    assert record["deflicker"] == {"window": 7, "max_gain_stops": 1.0, "frame_count": 3}
    movie = sessions._directory(blocked["id"]) / "timelapse_partial.mp4"
    probe = subprocess.run(
        [tools["ffprobe"], "-v", "error", "-select_streams", "v:0", "-count_frames", "-show_entries",
         "stream=codec_name,width,height,nb_read_frames", "-of", "json", str(movie)],
        capture_output=True, check=True, text=True)
    stream = json.loads(probe.stdout)["streams"][0]
    assert (stream["codec_name"], stream["width"], stream["height"], stream["nb_read_frames"]) == ("h264", 1920, 1080, "3")
    document = json.loads((movie.parent / "timelapse_partial.json").read_text())
    assert [row["source_sha256"] for row in document["deflicker"]["frames"]] == [
        frame["receipt"]["sha256"] for frame in blocked["frames"][:3]]


def test_missing_ffmpeg_is_unavailable_and_writes_nothing(sessions, blocked_session, tmp_path, monkeypatch):
    import deflicker
    empty = tmp_path / "empty-path"
    empty.mkdir()
    monkeypatch.setenv("PATH", str(empty))
    monkeypatch.setattr(deflicker, "apply_gains_to_frames",
                        lambda *a, **k: pytest.fail("deflicker copies written without an encoder"))
    result = product_sessions.encode_partial(blocked_session["id"])
    assert result["partial_video"]["status"] == "unavailable"
    assert result["partial_video"]["frames_preserved"] is True
    assert result["status"] == "needs_reconciliation"
    directory = sessions._directory(blocked_session["id"])
    assert not (directory / "timelapse_partial.mp4").exists()
    assert not (directory / "partial_frames").exists()
    for frame in result["frames"][:4]:  # the accepted JPEGs remain, verified
        assert sessions.artifact(blocked_session["id"], f"frame_{frame['index']:04d}.jpg").is_file()
    with pytest.raises(FileNotFoundError):
        sessions.artifact(blocked_session["id"], "timelapse_partial.mp4")


@pytest.mark.parametrize("status", ["completed", "running", "identity_refused", "failed", "cancelled",
                                    "ready", "restore_failed", "precondition_refused"])
def test_refuses_every_other_status(sessions, blocked_session, monkeypatch, status):
    calls = []
    _fake_encoder(monkeypatch, calls)
    state = sessions.read(blocked_session["id"])
    state["status"] = status
    sessions.save(state)
    with pytest.raises(PartialRefused) as refused:
        product_sessions.encode_partial(blocked_session["id"])
    assert refused.value.reason == "status"
    assert calls == [] and "partial_video" not in sessions.read(blocked_session["id"])


def test_refuses_a_truly_completed_timelapse(tmp_path, sessions, monkeypatch):
    calls = []
    _fake_encoder(monkeypatch, calls)
    runner = stack_session.SessionRunner(tmp_path / "shots" / "sessions")
    state = runner.create("timelapse", 3)

    def capture(index, spec, dest):
        Image.new("RGB", (96, 64), (_shade(index),) * 3).save(dest, format="JPEG")
        return str(dest)

    completed = _REAL_RUN(runner, state["id"], capture, None)
    assert completed["status"] == "completed"
    with pytest.raises(PartialRefused, match="needs_reconciliation"):
        product_sessions.encode_partial(state["id"])
    assert calls == []


@pytest.mark.parametrize("mode", ["hdr", "night", "crowd_remove"])
def test_refuses_non_timelapse_modes(sessions, tmp_path, monkeypatch, mode):
    calls = []
    _fake_encoder(monkeypatch, calls)
    blocked = _blocked(tmp_path, frames=3, ambiguous_at=2, mode=mode)
    with pytest.raises(PartialRefused) as refused:
        product_sessions.encode_partial(blocked["id"])
    assert refused.value.reason == "not_timelapse" and calls == []


@pytest.mark.parametrize("ambiguous_at", [0, 1])
def test_refuses_fewer_than_two_accepted_frames(sessions, tmp_path, monkeypatch, ambiguous_at):
    calls = []
    _fake_encoder(monkeypatch, calls)
    blocked = _blocked(tmp_path, frames=4, ambiguous_at=ambiguous_at)
    with pytest.raises(PartialRefused) as refused:
        product_sessions.encode_partial(blocked["id"])
    assert refused.value.reason == "too_few_frames" and calls == []


SMARTLAPSE = {"smartlapse": {"duration_s": 0.25, "output_s": 0.2, "fps": 30}}  # 6 frames at 30 fps, 50 ms apart


@pytest.mark.parametrize("policy, controls, fps", [("fixed", None, 24), ("auto_custom", None, 24),
                                                   ("holy_grail", None, 24), ("fixed", SMARTLAPSE, 30)])
def test_every_timelapse_policy_is_encodable_at_its_own_fps(sessions, tmp_path, monkeypatch, policy, controls, fps):
    calls = []
    _fake_encoder(monkeypatch, calls)
    blocked = _blocked(tmp_path, frames=5, ambiguous_at=3, policy=policy, controls=controls)
    result = product_sessions.encode_partial(blocked["id"])
    assert result["partial_video"]["status"] == "completed"
    assert result["partial_video"]["frame_indices"] == [0, 1, 2]
    assert result["partial_video"]["fps"] == fps and calls[0]["fps"] == fps
    assert result["status"] == "needs_reconciliation"


def test_a_busy_session_is_refused(sessions, blocked_session, monkeypatch):
    _fake_encoder(monkeypatch, [])
    with sessions.operation(blocked_session["id"]):
        with pytest.raises(RuntimeError, match="active operation"):
            product_sessions.encode_partial(blocked_session["id"])


def test_split_runtime_control_queues_and_the_jobs_role_encodes(sessions, blocked_session, monkeypatch):
    import runtime_roles
    calls = []
    _fake_encoder(monkeypatch, calls)
    encode = timelapse_partial.encode
    monkeypatch.setenv(runtime_roles.ROLE_ENV, "control")
    monkeypatch.setattr(timelapse_partial, "encode", lambda *a, **k: pytest.fail("control role encoded"))
    queued = product_sessions.encode_partial(blocked_session["id"])
    assert queued["partial_video"]["status"] == "queued" and queued["status"] == "needs_reconciliation"
    assert product_sessions.encode_partial(blocked_session["id"])["partial_video"]["status"] == "queued"

    monkeypatch.setattr(timelapse_partial, "encode", encode)
    monkeypatch.setenv(runtime_roles.ROLE_ENV, "jobs")
    assert heavy_jobs.run_pending(sessions) == [blocked_session["id"]]
    done = sessions.read(blocked_session["id"])
    assert done["partial_video"]["status"] == "completed" and done["status"] == "needs_reconciliation"
    assert len(calls) == 1
    assert heavy_jobs.run_pending(sessions) == []  # nothing left to do


def test_manifest_refuses_a_partial_record_pointing_elsewhere(sessions, blocked_session, monkeypatch):
    _fake_encoder(monkeypatch, [])
    session_id = blocked_session["id"]
    state = product_sessions.encode_partial(session_id)
    state["partial_video"]["receipt"]["path"] = str(sessions._directory(session_id) / "frame_0000.jpg")
    stack_session._write_manifest(sessions._directory(session_id), state)
    with pytest.raises(ValueError, match="partial movie receipt path"):
        sessions.read(session_id)


def test_route_encodes_serves_and_refuses(sessions, blocked_session, tmp_path, monkeypatch):
    import app
    from fastapi.testclient import TestClient
    calls = []
    _fake_encoder(monkeypatch, calls)
    monkeypatch.setattr(app, "SHOTS_DIR", tmp_path / "shots")
    monkeypatch.setattr(app, "API_TOKEN", "fixture-credential-1234567890")
    monkeypatch.setattr(app, "_exclusive_camera", lambda: pytest.fail("camera lock taken"))
    headers = {"X-Camera-Autopilot-Token": app.API_TOKEN}
    client = TestClient(app.app)
    session_id = blocked_session["id"]

    assert client.post(f"/sessions/{session_id}/encode-partial").status_code in (401, 403)
    assert calls == []
    response = client.post(f"/sessions/{session_id}/encode-partial", headers=headers)
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["status"] == "needs_reconciliation" and body["partial_video"]["status"] == "completed"
    movie = client.get(body["partial_video"]["url"], headers=headers)
    assert movie.status_code == 200
    assert hashlib.sha256(movie.content).hexdigest() == body["partial_video"]["receipt"]["sha256"]
    receipt = client.get(body["partial_video"]["receipt_file"]["url"], headers=headers)
    assert receipt.status_code == 200 and receipt.json()["frame_indices"] == [0, 1, 2, 3]

    state = sessions.read(session_id)
    state["status"] = "identity_refused"
    state.pop("partial_video")
    sessions.save(state)
    refused = client.post(f"/sessions/{session_id}/encode-partial", headers=headers)
    assert refused.status_code == 409
    assert refused.json()["detail"]["reason"] == "status"
    assert client.post(f"/sessions/{'0' * 32}/encode-partial", headers=headers).status_code == 404
    assert len(calls) == 1


def _tree(directory: Path) -> dict:
    return {path.relative_to(directory).as_posix(): _sha(path) for path in sorted(directory.rglob("*"))
            if path.is_file()}


def test_offline_partial_reads_the_session_and_writes_only_its_output(sessions, blocked_session, tmp_path, monkeypatch):
    sys.path.insert(0, str(ROOT / "tools"))
    import remerge_session
    calls = []
    _fake_encoder(monkeypatch, calls)
    directory = sessions._directory(blocked_session["id"])
    before = _tree(directory)
    with pytest.raises(ValueError, match="refusing"):
        remerge_session.encode_partial(directory, directory / "partial.mp4", force=True)
    out = tmp_path / "out" / "partial.mp4"
    assert remerge_session.main([str(directory), "--partial", "--out", str(out)]) == 0
    assert _tree(directory) == before  # the session is only read
    assert sorted(path.name for path in out.parent.iterdir()) == ["partial.mp4", "partial.mp4.json"]
    report = json.loads(out.with_name("partial.mp4.json").read_text())
    accepted = [frame["receipt"]["sha256"] for frame in blocked_session["frames"][:4]]
    assert calls[0]["hashes"] == accepted
    assert [item["sha256"] for item in report["inputs"]] == accepted
    assert report["frame_indices"] == [0, 1, 2, 3] and report["ambiguous_indices"] == [4]
    assert report["output"]["sha256"] == _sha(out) and report["offline"] is True
    assert remerge_session.main([str(directory), "--partial", "--out", str(out)]) == 1  # exists, no --force
    # The same refusals as the route: a completed timelapse has no partial movie.
    state = sessions.read(blocked_session["id"])
    state["status"] = "completed"
    sessions.save(state)
    assert remerge_session.main([str(directory), "--partial", "--out", str(tmp_path / "x.mp4")]) == 1
    assert not (tmp_path / "x.mp4").exists()


def test_offline_partial_without_ffmpeg_is_unavailable(sessions, blocked_session, tmp_path, monkeypatch):
    sys.path.insert(0, str(ROOT / "tools"))
    import remerge_session
    empty = tmp_path / "empty-path"
    empty.mkdir()
    monkeypatch.setenv("PATH", str(empty))
    out = tmp_path / "out" / "partial.mp4"
    directory = sessions._directory(blocked_session["id"])
    assert remerge_session.main([str(directory), "--partial", "--out", str(out)]) == 1
    assert not out.exists() and not out.with_name("partial.mp4.json").exists()


def test_offline_receipt_never_writes_through_a_symlink_into_the_session(sessions, blocked_session, tmp_path, monkeypatch):
    sys.path.insert(0, str(ROOT / "tools"))
    import remerge_session
    calls = []
    _fake_encoder(monkeypatch, calls)
    directory = sessions._directory(blocked_session["id"])
    before = _tree(directory)
    out = tmp_path / "out" / "partial.mp4"
    out.parent.mkdir()
    out.with_name("partial.mp4.json").symlink_to(directory / "manifest.json")
    assert remerge_session.main([str(directory), "--partial", "--force", "--out", str(out)]) == 1
    assert _tree(directory) == before and calls == [] and not out.exists()
    # A symlink planted after the check is replaced by the rename, never followed.
    out.with_name("partial.mp4.json").unlink()
    real_check = remerge_session.encode_partial
    import timelapse_partial as partial_module
    real_encode = partial_module.encode

    def plant_then_encode(*args, **kwargs):
        result = real_encode(*args, **kwargs)
        out.with_name("partial.mp4.json").symlink_to(directory / "manifest.json")
        return result

    monkeypatch.setattr(partial_module, "encode", plant_then_encode)
    assert real_check(directory, out, force=True)["status"] == "ok"
    assert _tree(directory) == before
    assert not out.with_name("partial.mp4.json").is_symlink()
    assert json.loads(out.with_name("partial.mp4.json").read_text())["frame_indices"] == [0, 1, 2, 3]


def _plant_stale_render(directory: Path) -> None:
    (directory / "partial_frames" / "frames").mkdir(parents=True)
    (directory / "partial_frames" / "frames" / "frame_0000.jpg").write_bytes(b"left by a killed worker")
    (directory / "timelapse_partial.pending.mp4").write_bytes(b"unfinished")


def test_a_killed_encode_leaves_nothing_once_recovery_ends(sessions, blocked_session, tmp_path, monkeypatch):
    directory = sessions._directory(blocked_session["id"])
    # Recovery without an encoder still removes the earlier attempt's folder and movie.
    _plant_stale_render(directory)
    empty = tmp_path / "empty-path"
    empty.mkdir()
    monkeypatch.setenv("PATH", str(empty))
    assert product_sessions.encode_partial(blocked_session["id"])["partial_video"]["status"] == "unavailable"
    assert not (directory / "partial_frames").exists()
    assert not (directory / "timelapse_partial.pending.mp4").exists()
    # The jobs role gives up after MAX_ATTEMPTS worker exits and cleans up the same way.
    _plant_stale_render(directory)
    state = sessions.read(blocked_session["id"])
    state["partial_video"] = {"status": "running", "attempts": heavy_jobs.MAX_ATTEMPTS}
    sessions.save(state)
    assert heavy_jobs.run_pending(sessions) == [blocked_session["id"]]
    assert sessions.read(blocked_session["id"])["partial_video"]["status"] == "failed"
    assert not (directory / "partial_frames").exists()
    assert not (directory / "timelapse_partial.pending.mp4").exists()


@pytest.mark.parametrize("damage", ["changed_frame", "corrupt_controls"])
def test_a_retry_that_fails_its_checks_still_removes_the_stale_render(sessions, blocked_session, tmp_path,
                                                                       monkeypatch, damage):
    sys.path.insert(0, str(ROOT / "tools"))
    import remerge_session
    calls = []
    _fake_encoder(monkeypatch, calls)
    directory = sessions._directory(blocked_session["id"])
    _plant_stale_render(directory)
    out = tmp_path / "out" / "partial.mp4"
    (out.parent / ".partial.mp4.frames").mkdir(parents=True)
    (out.parent / ".partial.pending.mp4").write_bytes(b"unfinished")
    if damage == "changed_frame":
        (directory / "frame_0001.jpg").write_bytes(b"not the accepted bytes")
    else:
        (directory / "controls.json").write_text("{not json")
    record = product_sessions.encode_partial(blocked_session["id"])["partial_video"]
    assert record["status"] == "failed" and calls == []
    assert not (directory / "partial_frames").exists()
    assert not (directory / "timelapse_partial.pending.mp4").exists()
    assert remerge_session.main([str(directory), "--partial", "--out", str(out)]) == 1
    assert sorted(path.name for path in out.parent.iterdir()) == []


def test_offline_cleanup_never_removes_a_session_inside_its_temporary_path(tmp_path, monkeypatch):
    sys.path.insert(0, str(ROOT / "tools"))
    import remerge_session
    calls = []
    _fake_encoder(monkeypatch, calls)
    # The session lives under the folder the tool would use as its render scratch beside --out.
    shots = tmp_path / "export" / ".partial.mp4.frames"
    blocked = _blocked(shots.parent, frames=4, ambiguous_at=3)  # creates <shots.parent>/shots/sessions
    directory = shots.parent / "shots" / "sessions" / blocked["id"]
    moved = shots / "sessions" / blocked["id"]
    moved.parent.mkdir(parents=True)
    directory.rename(moved)
    before = _tree(moved)
    out = tmp_path / "export" / "partial.mp4"
    with pytest.raises(ValueError, match="holds the session"):
        remerge_session.encode_partial(moved, out, force=True)
    assert _tree(moved) == before and calls == []
