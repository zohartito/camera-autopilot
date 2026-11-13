"""Offline adapter tests: restoration identity, publication recovery and private artifacts."""
import hashlib
import json
from pathlib import Path
from types import SimpleNamespace
import pytest
from PIL import Image
import engine
import product_sessions
from stack_session import SessionRunner, frame_receipt


def _jpeg(path, shade=90):
    Image.new("RGB", (32, 24), (shade, 80, 100)).save(path)
    return str(path)


@pytest.fixture
def sessions(tmp_path, monkeypatch):
    monkeypatch.setattr(engine, "SHOTS_DIR", tmp_path / "shots")
    return product_sessions.runner()


def _completed(sessions, mode="hdr"):
    state = sessions.create(mode, 2)
    def capture(index, spec, dest):
        return _jpeg(dest, 80 + index)
    def merge(paths, dest):
        return _jpeg(dest)
    return sessions.run(state["id"], capture, None if mode == "timelapse" else merge)


def test_completed_unpublished_recovers_without_camera_and_is_idempotent(sessions, monkeypatch):
    import sonyremote
    state = _completed(sessions)
    monkeypatch.setattr(sonyremote, "Camera", lambda **kwargs: pytest.fail("publication must not open camera"))
    first = product_sessions.run(state["id"])
    second = product_sessions.run(state["id"])
    assert first["publication"]["status"] == "published"
    assert first["image"] == second["image"] == f"session_{state['id']}.jpg"
    assert len(list(engine.SHOTS_DIR.glob("*.jpg"))) == 1
    assert sessions.read(state["id"])["image"] == first["image"]


def test_publication_failure_can_retry_same_identity(sessions, monkeypatch):
    from photo_lifecycle import PhotoLifecycle
    state = _completed(sessions)
    original = PhotoLifecycle.enroll
    monkeypatch.setattr(PhotoLifecycle, "enroll", lambda *args, **kwargs: (_ for _ in ()).throw(OSError("simulated receipt failure")))
    failed = product_sessions.run(state["id"])
    assert failed["status"] == "completed"
    assert failed["publication"]["status"] == "failed"
    assert len(list(engine.SHOTS_DIR.glob("*.jpg"))) == 1
    monkeypatch.setattr(PhotoLifecycle, "enroll", original)
    recovered = product_sessions.run(state["id"])
    assert recovered["publication"]["status"] == "published"
    assert len(list(engine.SHOTS_DIR.glob("*.jpg"))) == 1


def test_timelapse_zip_receipt_download_and_lifecycle_enrollment(sessions):
    import zipfile
    state = _completed(sessions, "timelapse")
    result = product_sessions.run(state["id"])
    assert result["publication"]["status"] == "published"
    archive = sessions.artifact(state["id"], "sequence.zip")
    with zipfile.ZipFile(archive) as handle:
        assert handle.namelist() == ["frame_0000.jpg", "frame_0001.jpg", "sequence.json"]
        assert len(json.loads(handle.read("sequence.json"))["frames"]) == 2
    lifecycle = json.loads((engine.SHOTS_DIR / ".lifecycle" / f"{result['image']}.json").read_text())
    assert f"sessions/{state['id']}/sequence.zip" in lifecycle["owned_paths"]
    assert result["sequence"]["url"].endswith("/artifacts/sequence.zip")


def test_original_baseline_survives_restore_failure_and_recovery(sessions, tmp_path, monkeypatch):
    import sonyremote
    import camera_caps
    baseline = {"iso": "100", "aperture": "8", "shutter": "1/60", "focus_mode": "Automatic"}
    live = dict(baseline)
    calls = []
    class Camera:
        fail_restore = True
        shots = 0
        def __init__(self, **kwargs): pass
        def __enter__(self): return self
        def __exit__(self, *args): pass
        def info(self): return SimpleNamespace(model="Sony ILCE-7M2 (Control)", port="usb:001,002")
        def wake(self): return True
        def shooting_mode(self): return "M"
        def get_settings(self): return dict(live)
        def list_options(self, key): return {"iso": ["100", "200"], "aperture": ["8"], "shutter": ["1/60"]}[key]
        def set_exposure(self, **kwargs):
            calls.append(("exposure", dict(kwargs)))
            live.update(kwargs)
        def set_setting(self, key, value):
            calls.append((key, value))
            if key == "focus_mode" and value == baseline["focus_mode"] and self.fail_restore:
                raise RuntimeError("restore connection lost")
            live[key] = value
        def capture(self, **kwargs):
            type(self).shots += 1
            return _jpeg(tmp_path / f"capture{self.shots}.jpg")
    monkeypatch.setattr(sonyremote, "Camera", Camera)
    monkeypatch.setattr(engine, "_free_usb", lambda: None)
    monkeypatch.setattr(camera_caps, "from_live_choices", lambda *args: SimpleNamespace(snap_shutter=lambda value: (value, "1/60")))
    state = sessions.create("timelapse", 2, metadata={"camera_model": "Sony ILCE-7M2 (Control)", "camera_port": "usb:001,002", "camera_serial": "A7II00SERIAL"})
    failed = product_sessions.run(state["id"])
    assert failed["status"] == "restore_failed", failed
    assert failed["restore_baseline"] == baseline
    assert live["focus_mode"] == "Manual"
    count = Camera.shots
    Camera.fail_restore = False
    calls.clear()
    recovered = product_sessions.run(state["id"])
    assert recovered["status"] == "completed", recovered
    assert recovered["restore_baseline"] == baseline
    assert live == baseline
    assert Camera.shots == count == 2
    assert calls[0][0] == "exposure" and calls[1] == ("focus_mode", "Automatic")


def test_partial_delete_and_active_operation_conflict(sessions):
    state = sessions.create("hdr", 2)
    with sessions.operation(state["id"]):
        with pytest.raises(RuntimeError, match="active"):
            product_sessions.delete(state["id"])
    result = product_sessions.delete(state["id"])
    assert result["deleted_count"] >= 2
    assert not sessions._directory(state["id"]).exists()


def test_ambiguous_delete_requires_explicit_discard(sessions):
    state = sessions.create("hdr", 2)
    failed = sessions.run(state["id"], lambda *args: None)
    assert failed["status"] == "needs_reconciliation"
    with pytest.raises(RuntimeError, match="explicit discard"):
        product_sessions.delete(state["id"])
    assert product_sessions.delete(state["id"], discard_unresolved=True)["discarded_unresolved"]


def test_artifact_rejects_unknown_pending_changed_or_escaped_files(sessions, tmp_path):
    state = _completed(sessions)
    for name in ("../result.jpg", "manifest.json", "frame_9999.jpg"):
        with pytest.raises((FileNotFoundError, ValueError)):
            sessions.artifact(state["id"], name)
    accepted = sessions.artifact(state["id"], "frame_0000.jpg")
    _jpeg(accepted, 220)
    with pytest.raises(ValueError, match="changed"):
        sessions.artifact(state["id"], "frame_0000.jpg")
    directory = sessions._directory(state["id"])
    link = directory / "unexpected"
    link.symlink_to(tmp_path)
    with pytest.raises(ValueError, match="symlinks"):
        sessions.delete(state["id"])


def test_delete_published_sequence_cleans_gallery_and_artifacts(sessions):
    state = _completed(sessions, "timelapse")
    result = product_sessions.run(state["id"])
    receipt = product_sessions.delete(state["id"])
    assert receipt["gallery"]["deleted_count"] >= 2
    assert not (engine.SHOTS_DIR / result["image"]).exists()
    assert not sessions._directory(state["id"]).exists()


def test_target_pair_required_and_mismatch_rejected_before_wake(sessions, monkeypatch):
    import sonyremote
    monkeypatch.setattr(engine, "_free_usb", lambda: None)
    class Camera:
        def __init__(self, **kwargs): pass
        def __enter__(self): return self
        def __exit__(self, *args): pass
        def info(self): return SimpleNamespace(model="Sony Alpha-A7 III (PC Control)", port="usb:001,003")
        def wake(self): pytest.fail("wrong camera must not be woken")
    monkeypatch.setattr(sonyremote, "Camera", Camera)
    missing = sessions.create("hdr", 2)
    with pytest.raises(sonyremote.CameraError, match="pair"):
        product_sessions.run(missing["id"])
    state = sessions.create("hdr", 2, metadata={"camera_model": "Sony ILCE-7M2 (Control)", "camera_port": "usb:001,002", "camera_serial": "A7II00SERIAL"})
    with pytest.raises(sonyremote.CameraError, match="differs"):
        product_sessions.run(state["id"])


def test_corrupt_manifest_cannot_redirect_capture_destination(sessions, tmp_path):
    state = sessions.create("hdr", 2)
    state["frames"][0]["destination"] = str(tmp_path / "outside.jpg")
    sessions.save(state)
    with pytest.raises(ValueError, match="identity/path"):
        sessions.run(state["id"], lambda *args: pytest.fail("must not capture"))
    assert not (tmp_path / "outside.jpg").exists()


def test_cancel_marker_symlink_is_not_followed(sessions, tmp_path):
    state = sessions.create("hdr", 2)
    outside = tmp_path / "outside.txt"
    outside.write_text("preserve")
    (sessions._directory(state["id"]) / "cancel").symlink_to(outside)
    with pytest.raises(OSError):
        sessions.cancel(state["id"])
    assert outside.read_text() == "preserve"


def test_gallery_delete_wins_before_rerun_without_active_lock_or_resurrection(sessions, monkeypatch):
    """Barrier reproduces the old operation->lifecycle wait window deterministically."""
    import contextlib
    import threading
    from photo_lifecycle import PhotoLifecycle, _LOCK
    state = _completed(sessions)
    published = product_sessions.run(state["id"])
    entered_operation = threading.Event()
    requested_runner = threading.Event()
    original_operation = sessions.operation
    @contextlib.contextmanager
    def observed_operation(session_id):
        with original_operation(session_id):
            entered_operation.set()
            yield
    monkeypatch.setattr(sessions, "operation", observed_operation)
    def get_runner():
        requested_runner.set()
        return sessions
    monkeypatch.setattr(product_sessions, "runner", get_runner)
    outcomes = []
    def retry():
        try:
            outcomes.append(product_sessions.run(state["id"]))
        except Exception as exc:
            outcomes.append(exc)
    with _LOCK:
        worker = threading.Thread(target=retry)
        worker.start()
        assert requested_runner.wait(1)
        # Old code acquired operation here and then waited on the held lifecycle lock.
        assert not entered_operation.wait(.05)
        PhotoLifecycle(engine.SHOTS_DIR).delete(published["image"])
        assert not (sessions._directory(state["id"]) / "manifest.json").exists()
    worker.join(2)
    assert not worker.is_alive()
    assert len(outcomes) == 1 and isinstance(outcomes[0], FileNotFoundError)
    assert not entered_operation.is_set()
    assert not (sessions._directory(state["id"]) / "manifest.json").exists()
    assert not (sessions._directory(state["id"]) / "operation.lock").exists()


def test_product_delete_acquires_lifecycle_before_operation(sessions, monkeypatch):
    import contextlib
    import threading
    from photo_lifecycle import _LOCK
    state = sessions.create("hdr", 2)
    requested_runner, entered_operation = threading.Event(), threading.Event()
    original_operation = sessions.operation
    @contextlib.contextmanager
    def observed_operation(session_id):
        with original_operation(session_id):
            entered_operation.set()
            yield
    monkeypatch.setattr(sessions, "operation", observed_operation)
    def get_runner():
        requested_runner.set()
        return sessions
    monkeypatch.setattr(product_sessions, "runner", get_runner)
    results = []
    with _LOCK:
        worker = threading.Thread(target=lambda: results.append(product_sessions.delete(state["id"])))
        worker.start()
        assert requested_runner.wait(1)
        assert not entered_operation.wait(.05)
        assert (sessions._directory(state["id"]) / "manifest.json").exists()
    worker.join(2)
    assert not worker.is_alive()
    assert entered_operation.is_set() and results[0]["deleted_count"] > 0


# --- Holy-Grail timelapse deflicker integration ------------------------------

def _require_encoder():
    """These tests render H.264; without a working ffmpeg/ffprobe they cannot run (hosted CI installs it)."""
    from timelapse_video import probe_encoder
    if probe_encoder() is None:
        pytest.skip('ffmpeg/ffprobe did not execute (-version)')


def _flicker_timelapse(sessions, frames=4, *, deflicker_control=None, policy="fixed"):
    """A completed timelapse whose frames carry a per-frame +/- brightness flicker."""
    import timelapse_controls
    state = sessions.create("timelapse", frames, timelapse_mode=policy)
    if deflicker_control is not None:
        timelapse_controls.update_controls(sessions, state["id"], {"deflicker": deflicker_control})
    def capture(index, spec, dest):
        base = 70.0 * (2 ** (0.03 * index))
        value = round(max(1, min(255, base * (2 ** (0.3 * (-1) ** index)))))
        Image.new("RGB", (96, 64), (value, value, value)).save(dest, format="JPEG", quality=95)
        return str(dest)
    return sessions.run(state["id"], capture, None)


def test_deflicker_final_video_renders_from_corrected_copies_zip_keeps_originals(sessions):
    _require_encoder()
    import zipfile
    completed = _flicker_timelapse(sessions, 4, deflicker_control={"enabled": True, "window": 3, "max_gain_stops": 1.0})
    result = product_sessions.run(completed["id"])
    assert result["publication"]["status"] == "published"
    # The render recorded the gain series + per-frame receipts and used the deflicker dir.
    video = result["video"]
    assert video["status"] == "completed"
    assert "deflicker_error" not in video
    info = video["deflicker"]
    assert info["frame_count"] == 4 and len(info["gains"]) == 4
    assert len(info["frames"]) == 4 and all("source_sha256" in f and "out_sha256" in f for f in info["frames"])
    directory = sessions._directory(completed["id"])
    corrected = directory / "deflicker"
    assert corrected.is_dir()
    assert sorted(p.name for p in corrected.iterdir()) == [f"frame_{i:04d}.jpg" for i in range(4)]
    # At least one corrected copy actually differs from its accepted original.
    changed = any((corrected / f"frame_{i:04d}.jpg").read_bytes()
                  != (directory / f"frame_{i:04d}.jpg").read_bytes() for i in range(4))
    assert changed
    # The exact-input ZIP still holds the untouched originals, never the corrected copies.
    with zipfile.ZipFile(directory / "sequence.zip") as handle:
        for i, frame in enumerate(completed["frames"]):
            entry = handle.read(f"frame_{i:04d}.jpg")
            assert hashlib.sha256(entry).hexdigest() == frame["receipt"]["sha256"]


def test_deflicker_incremental_preview_uses_corrected_copies(sessions):
    _require_encoder()
    completed = _flicker_timelapse(sessions, 3, deflicker_control={"enabled": True, "window": 3, "max_gain_stops": 1.0})
    state = sessions.read(completed["id"])
    control = {"enabled": True, "window": 3, "max_gain_stops": 1.0}
    preview = product_sessions._incremental_video(sessions, state, 24, control)
    assert preview["status"] == "completed"
    assert preview["deflicker"]["frame_count"] == 3
    directory = sessions._directory(completed["id"])
    assert (directory / "deflicker" / "frame_0000.jpg").is_file()
    # Registered like accepted_boundary would, the preview mp4 is verified and downloadable.
    state["preview_video"] = preview
    sessions.save(state)
    assert sessions.artifact(completed["id"], preview["name"]).is_file()


def test_deflicker_disabled_is_byte_for_byte_the_old_path(sessions):
    _require_encoder()
    completed = _flicker_timelapse(sessions, 3)  # no deflicker control at all
    result = product_sessions.run(completed["id"])
    assert result["publication"]["status"] == "published"
    assert result["video"]["status"] == "completed"
    assert "deflicker" not in result["video"] and "deflicker_error" not in result["video"]
    assert not (sessions._directory(completed["id"]) / "deflicker").exists()


def test_a_holy_grail_timelapse_is_deflickered_by_default(sessions):
    # Owner decision 2026-09-27: no deflicker option on a Holy Grail session means the recommended one.
    _require_encoder()
    completed = _flicker_timelapse(sessions, 4, policy="holy_grail")
    result = product_sessions.run(completed["id"])
    assert result["publication"]["status"] == "published"
    info = result["video"]["deflicker"]
    assert (info["window"], info["max_gain_stops"], info["frame_count"]) == (7, 1.0, 4)
    assert (sessions._directory(completed["id"]) / "deflicker").is_dir()


def test_no_encoder_means_no_deflicker_copies_are_written(sessions, monkeypatch):
    # Without ffmpeg the movie is unavailable; decoding and writing corrected copies first is wasted work.
    import deflicker
    import timelapse_video
    monkeypatch.setattr(timelapse_video, "probe_encoder", lambda: None)
    monkeypatch.setattr(deflicker, "apply_gains_to_frames",
                        lambda *a, **k: pytest.fail("deflicker copies written without an encoder"))
    completed = _flicker_timelapse(sessions, 4, policy="holy_grail")
    result = product_sessions.run(completed["id"])
    assert result["video"]["status"] == "unavailable"
    assert not (sessions._directory(completed["id"]) / "deflicker").exists()
    assert result["sequence"]["format"] == "zip/jpeg_sequence"  # frames are still published


def test_deflicker_failure_falls_back_to_raw_frames_and_records_error(sessions, monkeypatch):
    _require_encoder()
    import deflicker
    completed = _flicker_timelapse(sessions, 3, deflicker_control={"enabled": True, "window": 3, "max_gain_stops": 1.0})
    monkeypatch.setattr(deflicker, "deflicker_gains", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("gain solver blew up")))
    result = product_sessions.run(completed["id"])
    # The video still renders (from the untouched originals) and the failure is recorded.
    assert result["publication"]["status"] == "published"
    assert result["video"]["status"] == "completed"
    assert "deflicker" not in result["video"]
    assert "gain solver blew up" in result["video"]["deflicker_error"]
