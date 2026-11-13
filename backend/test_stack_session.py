"""Offline proofs for capture provenance, crash recovery and bounded session behavior."""
from pathlib import Path
import json
import pytest
from PIL import Image
import stack_session
from stack_session import SessionRunner


def capture(index, spec, destination):
    Image.new("RGB", (48, 32), (40 + index, 60, 80)).save(destination)
    return destination


def test_checkpointed_sequence_and_idempotent_completed_run(tmp_path):
    runner = SessionRunner(tmp_path)
    session = runner.create("timelapse", 3)
    calls = []
    def shoot(index, spec, destination):
        state = runner.read(session["id"])
        assert state["frames"][index]["status"] == "capturing"
        calls.append(index)
        return capture(index, spec, destination)
    result = runner.run(session["id"], shoot)
    assert result["status"] == "completed"
    assert result["progress"]["accepted"] == 3
    assert len({f["receipt"]["path"] for f in result["frames"]}) == 3
    runner.run(session["id"], shoot)
    assert calls == [0, 1, 2]


def test_ambiguous_shutter_never_automatically_retries_and_restores(tmp_path):
    runner = SessionRunner(tmp_path)
    session = runner.create("hdr", 3)
    restored, calls = [], []
    def timeout(index, spec, destination):
        calls.append(index)
        capture(index, spec, destination)  # Shutter actually fired before transport timeout.
        raise TimeoutError("response lost")
    result = runner.run(session["id"], timeout, restore_settings=lambda: restored.append(True))
    assert result["status"] == "needs_reconciliation"
    assert result["frames"][0]["status"] == "ambiguous"
    assert restored == [True]
    runner.run(session["id"], timeout)
    assert calls == [0]


def test_merge_failure_resumes_without_duplicate_exposures(tmp_path):
    runner = SessionRunner(tmp_path)
    session = runner.create("hdr", 3)
    calls = []
    def shoot(index, spec, destination):
        calls.append(index)
        return capture(index, spec, destination)
    def bad_merge(paths, destination):
        raise OSError("disk write rejected")
    result = runner.run(session["id"], shoot, bad_merge)
    assert result["status"] == "failed"
    def merge(paths, destination):
        Image.open(paths[0]).save(destination)
        return destination
    result = runner.run(session["id"], shoot, merge)
    assert result["status"] == "completed"
    assert calls == [0, 1, 2]
    assert result["output"]["receipt"]["width"] == 48


def test_changed_accepted_frame_blocks_resume_before_camera(tmp_path):
    runner = SessionRunner(tmp_path)
    session = runner.create("hdr", 2)
    runner.run(session["id"], capture, lambda *args: (_ for _ in ()).throw(OSError("merge")))
    Image.new("RGB", (48, 32), "red").save(session["frames"][0]["destination"])
    result = runner.run(session["id"], lambda *args: pytest.fail("must not expose"))
    assert result["status"] == "failed"
    assert "changed" in result["error"]


def test_old_card_file_not_accepted_as_new_capture(tmp_path):
    old = tmp_path / "old.jpg"
    capture(0, {}, str(old))
    runner = SessionRunner(tmp_path / "sessions")
    session = runner.create("hdr", 2)
    result = runner.run(session["id"], lambda *args: str(old))
    assert result["status"] == "needs_reconciliation"
    assert result["progress"]["accepted"] == 0


def test_cancel_finishes_current_exposure_and_preserves_partial(tmp_path):
    runner = SessionRunner(tmp_path)
    session = runner.create("hdr", 3)
    restored = []
    def shoot(index, spec, destination):
        result = capture(index, spec, destination)
        runner.cancel(session["id"])
        return result
    result = runner.run(session["id"], shoot, restore_settings=lambda: restored.append(True))
    assert result["status"] == "cancelled"
    assert result["progress"]["accepted"] == 1
    assert restored == [True]


def test_restore_failure_is_not_reported_complete(tmp_path):
    runner = SessionRunner(tmp_path)
    session = runner.create("hdr", 2)
    def restore():
        raise RuntimeError("camera gone")
    result = runner.run(session["id"], capture, restore_settings=restore)
    assert result["status"] == "restore_failed"


@pytest.mark.parametrize("frames", [0, 1, 17, True, 2.2])
def test_stack_frame_bounds(tmp_path, frames):
    with pytest.raises(ValueError):
        SessionRunner(tmp_path).create("hdr", frames)


def test_raw_focus_and_overlarge_bracket_fail_before_capture(tmp_path):
    runner = SessionRunner(tmp_path)
    with pytest.raises(ValueError):
        runner.create("focus_stack", 3)
    with pytest.raises(ValueError):
        runner.create("hdr", 16, ev_step=3)
    raw = tmp_path / "raw.ARW"
    raw.write_bytes(b"raw")
    with pytest.raises(ValueError):
        stack_session.frame_receipt(raw)


def test_source_budget(tmp_path, monkeypatch):
    monkeypatch.setattr(stack_session, "MAX_SESSION_SOURCE_BYTES", 10)
    runner = SessionRunner(tmp_path)
    session = runner.create("hdr", 2)
    result = runner.run(session["id"], capture)
    assert result["status"] == "needs_reconciliation"
    assert "budget" in result["error"]


def test_planner_motion_gate_and_timelapse_primitives():
    from stack_planner import plan_stack, ramp_ev, smartlapse_plan, deflicker_gains
    assert plan_stack(highlight_fraction=.2, shadow_fraction=.3)["mode"] == "single"
    assert plan_stack(highlight_fraction=.2, shadow_fraction=.3, motion_fraction=.01)["mode"] == "hdr"
    assert plan_stack(highlight_fraction=.2, shadow_fraction=.3, motion_fraction=.2)["mode"] == "single"
    assert ramp_ev(0, 4) == .3
    assert ramp_ev(0, -4) == -.3
    assert smartlapse_plan(10, 2, 24)["frames"] == 48
    gains = deflicker_gains([100, 100, 200, 100, 100])
    assert gains[2] == .5
    assert gains[0] == 1


def test_stack_aggregate_budget_checked_before_native_decode(tmp_path, monkeypatch):
    import stacking
    paths = []
    for index in range(3):
        path = tmp_path / f"frame{index}.jpg"
        capture(index, {}, str(path))
        paths.append(str(path))
    monkeypatch.setattr(stacking, "MAX_STACK_WORKING_BYTES", 10)
    monkeypatch.setattr(stacking, "_read_one", lambda *args: pytest.fail("must reject before native decode"))
    with pytest.raises(ValueError, match="aggregate"):
        stacking.long_exposure(paths)


def test_empty_capture_and_duplicate_urls_stop_without_card_substitution(tmp_path):
    import wifi_stack
    class Camera:
        def take_picture(self, **kwargs):
            return []
        def download(self, *args):
            pytest.fail("no download without exact capture identities")
        def transfer_latest_full_res(self, *args, **kwargs):
            pytest.fail("never substitute latest card files")
    camera = Camera()
    with pytest.raises(RuntimeError, match="ambiguous"):
        wifi_stack._capture_urls(camera)
    with pytest.raises(RuntimeError, match="duplicate"):
        wifi_stack.transfer_capture_jpegs(camera, [["http://cam/a.jpg"], ["http://cam/a.jpg"]], str(tmp_path))
    with pytest.raises(NotImplementedError, match="actuator"):
        wifi_stack.live_stack(camera, "focus_stack", 3, 2, str(tmp_path / "out.jpg"))


def test_bracket_restores_original_shutter_after_ambiguous_capture(monkeypatch):
    import wifi_stack
    monkeypatch.setattr(wifi_stack.time, "sleep", lambda *_args: None)
    class Camera:
        shutter = "1/60"
        def begin_session(self):
            pass
        def call(self, method):
            return {"result": [{"getExposureMode": "Manual", "getIsoSpeedRate": "100",
                                "getFNumber": "8"}.get(method, self.shutter)]}
        def set_shutter(self, label):
            self.shutter = label
        def take_picture(self, **kwargs):
            return []
    camera = Camera()
    def set_offset(cam, base, offset):
        cam.set_shutter("1/250")
        return "1/250"
    monkeypatch.setattr(wifi_stack, "_shutter_at_ev_offset", set_offset)
    with pytest.raises(RuntimeError, match="ambiguous"):
        wifi_stack.shoot_bracket(camera, frames=3)
    assert camera.shutter == "1/60"


def test_night_failed_write_is_not_success_and_restores(tmp_path, monkeypatch):
    import cv2
    import numpy as np
    import wifi_stack
    import wifi_night
    import stacking
    monkeypatch.setattr(wifi_night.time, "sleep", lambda *_args: None)
    class Camera:
        shutter, iso = "1/60", "100"
        def begin_session(self):
            pass
        def call(self, method):
            return {"result": [self.shutter if method == "getShutterSpeed" else self.iso]}
        def set_shutter(self, value):
            self.shutter = value
        def set_iso(self, value):
            self.iso = value
    camera = Camera()
    def snap(cam, value, getter, setter):
        setter(value)
        return value
    monkeypatch.setattr(wifi_night.wifi_autopilot, "_snap_set", snap)
    monkeypatch.setattr(wifi_stack, "shoot_burst", lambda *args, **kwargs: {"postviews": [["a"], ["b"]]})
    monkeypatch.setattr(wifi_stack, "transfer_capture_jpegs", lambda *args: (["a", "b"], []))
    monkeypatch.setattr(stacking, "long_exposure", lambda *args, **kwargs: np.zeros((32, 32, 3), dtype=np.uint8))
    monkeypatch.setattr(cv2, "imwrite", lambda *args: False)
    result = wifi_night.night_stack(camera, frames=2, out_path=str(tmp_path / "night.jpg"))
    assert result["merged"] is False
    assert camera.shutter == "1/60" and camera.iso == "100"


def test_panorama_auto_settings_rejected_and_overlap_measured(tmp_path):
    import wifi_pano
    import numpy as np
    import cv2
    class Camera:
        def call(self, method):
            return {"result": ["Auto"]}
    with pytest.raises(RuntimeError, match="Manual"):
        wifi_pano.verify_panorama_locks(Camera())
    source = np.random.default_rng(13).integers(0, 256, (400, 1000, 3), dtype=np.uint8)
    left, right = tmp_path / "left.jpg", tmp_path / "right.jpg"
    cv2.imwrite(str(left), source[:, :600])
    cv2.imwrite(str(right), source[:, 360:960])
    result = wifi_pano.estimate_overlap(str(left), str(right))
    assert result["measured"], result
    assert abs(result["overlap_fraction"] - .4) < .05, result
    assert result["in_target_band"], result


def test_cancelled_session_explicitly_resumes_pending_only(tmp_path):
    runner = SessionRunner(tmp_path)
    session = runner.create("timelapse", 3)
    calls = []
    def shoot(index, spec, destination):
        calls.append(index)
        if index == 0:
            runner.cancel(session["id"])
        return capture(index, spec, destination)
    assert runner.run(session["id"], shoot)["status"] == "cancelled"
    assert runner.resume(session["id"])["status"] == "ready"
    assert runner.run(session["id"], shoot)["status"] == "completed"
    assert calls == [0, 1, 2]


def test_panorama_failed_write_does_not_claim_success(tmp_path, monkeypatch):
    import cv2
    import numpy as np
    import wifi_pano
    paths = []
    for index in range(2):
        path = tmp_path / f"input{index}.jpg"
        capture(index, {}, str(path))
        paths.append(str(path))
    monkeypatch.setattr(wifi_pano, "_try_stitch", lambda *args: (cv2.Stitcher_OK, np.zeros((40, 80, 3), dtype=np.uint8)))
    monkeypatch.setattr(cv2, "imwrite", lambda *args: False)
    result = wifi_pano.stitch(paths, str(tmp_path / "out.jpg"))
    assert result["stitched"] is False
    assert "imwrite failed" in result["hint"]


def test_downloadable_timelapse_uses_durable_exact_receipts(tmp_path):
    import wifi_night
    class Camera:
        shots = 0
        def begin_session(self):
            pass
        def take_picture(self, **kwargs):
            self.shots += 1
            return [f"http://camera/frame{self.shots}.jpg"]
        def download(self, url, destination):
            capture(self.shots, {}, destination)
        def transfer_latest_full_res(self, *args, **kwargs):
            pytest.fail("old card substitution forbidden")
    camera = Camera()
    result = wifi_night.intervalometer(camera, 3, 0, full_res_dir=str(tmp_path))
    assert result["session"]["status"] == "completed"
    assert camera.shots == 3
    assert len(result["full_res"]) == 3
    assert all(frame["receipt"]["sha256"] for frame in result["session"]["frames"])
