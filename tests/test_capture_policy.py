"""Camera-free ordinary-shot policy and durable session integration acceptance."""
import json
from pathlib import Path
from types import SimpleNamespace
import sys
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))

import cv2
import numpy as np
import pytest

import capture_policy
import engine
import remeter
import product_capture
import product_sessions
from stack_session import merge_session_frames


@pytest.fixture
def rig(tmp_path, monkeypatch):
    monkeypatch.setattr(engine, "SHOTS_DIR", tmp_path)
    monkeypatch.setattr(product_capture.intent_brain, "interpret", lambda *a, **kw: {"target_brightness": 112})
    calls, frames, receipts, failures, ordinary = [], [], {}, set(), {}
    motion = {"status": "unavailable", "outcome_verified": False}
    camera = SimpleNamespace(name="real", camera_model=sorted(engine.USB_CONTROL_MODELS)[0], camera_port="usb:001,009",
                             camera_serial="A7III0SERIAL")
    def shoot(*args, **kwargs):
        calls.append(kwargs)
        image = np.zeros((64, 64, 3), dtype=np.uint8)
        image[:32] = 255
        name = f"ordinary-{len(calls)}.jpg"
        assert cv2.imwrite(str(tmp_path / name), image)
        return engine.Shot(True, name, "Captured" + ordinary.get("summary_suffix", ""), "Review it.", {},
                           {"iso": "100", "aperture": "5.6", "shutter": "1/60"},
                           evidence={"physical_exposure_count": 2, "motion": dict(motion), **ordinary.get("evidence", {})})
    camera.shoot = shoot
    meters = []
    def meter_baseline(intent, **kwargs):
        # The Fixed stack's meter frame (engine.RealEngine.meter_baseline), as a receipt only.
        meters.append(kwargs)
        return {"metered": {"iso": "100", "aperture": "5.6", "shutter": "1/60"},
                "inherited": {"iso": "3200", "aperture": "5.6", "shutter": "1/15"},
                "restore_baseline": {"iso": "3200", "aperture": "5.6", "shutter": "1/15", "focus_mode": "Automatic"},
                "physical_exposure_count": 1, "meter_passes": [{"pass": 0}], "recommendation_source": "fixture"}
    camera.meter_baseline = meter_baseline
    def run(session_id):
        sessions = product_sessions.runner()
        state = sessions.read(session_id)
        state["settings"] = {"iso": "100", "aperture": "5.6", "shutter": "1/60", "focus_mode": "Manual"}
        sessions.save(state)
        def capture(index, spec, destination):
            frames.append((index, spec))
            if index in receipts:
                spec["transport_receipt"] = receipts[index]
            if index in failures:
                raise OSError("fixture: frame outcome unknown after delivery")
            rng = np.random.default_rng(index)
            assert cv2.imwrite(str(destination), rng.integers(20, 230, (64, 64, 3), dtype=np.uint8))
            return destination
        state = sessions.run(session_id, capture,
            lambda paths, out: merge_session_frames(state["mode"], paths, out),
            prepare_frame=lambda *args: {"iso": "100", "shutter": "1/60"}, restore_settings=lambda: None)
        return product_sessions._publish(sessions, state)
    monkeypatch.setattr(product_sessions, "run", run)
    return SimpleNamespace(camera=camera, calls=calls, frames=frames, motion=motion, root=tmp_path, receipts=receipts, failures=failures, ordinary=ordinary, meters=meters)


@pytest.mark.parametrize("policy", [{"mode": "surprise"}, {"mode": "fixed", "frames": 10},
    {"mode": "fixed", "frames": True}, {"mode": "fixed", "stack_mode": "focus"},
    {"mode": "auto", "undeclared": 1}, "auto"])
def test_invalid_policy_rejected_before_any_capture(rig, policy):
    with pytest.raises(ValueError):
        product_capture.shoot(rig.camera, stack_policy=policy)
    assert not rig.calls and not rig.frames


@pytest.mark.parametrize("mode", ["auto", "fixed"])
def test_refinement_and_stack_conflict_before_capture(rig, mode):
    with pytest.raises(ValueError, match="cannot be combined"):
        product_capture.shoot(rig.camera, refine=True, stack_policy={"mode": mode})
    assert not rig.calls and not rig.frames


def test_default_off_preserves_single_pipeline_and_logs_budget(rig):
    shot = product_capture.shoot(rig.camera)
    assert shot.ok and len(rig.calls) == 1 and not rig.frames
    assert shot.evidence["physical_exposure_count"] == 2
    assert shot.evidence["stack_policy"]["requested"]["mode"] == "off"
    assert shot.evidence["stack_policy"]["budget"]["max_physical_exposures"] == 2 + remeter.MAX_REMETER_PASSES
    assert shot.evidence["capture_timing"]["attempts"][0]["pipeline_s"] >= 0


@pytest.mark.parametrize("mode", capture_policy.STACK_MODES)
def test_fixed_session_uses_exact_body_and_one_meter_frame_without_ordinary_capture(rig, mode):
    shot = product_capture.shoot(rig.camera, stack_policy={"mode": "fixed", "stack_mode": mode, "frames": 3})
    assert shot.ok and (rig.root / shot.image).is_file()
    assert not rig.calls and len(rig.frames) == 3 and len(rig.meters) == 1
    assert shot.evidence["physical_exposure_count"] == 1 + 3
    receipt = shot.evidence["stack_policy"]
    state = product_sessions.runner().read(receipt["session_id"])
    assert state["metadata"]["camera_model"] == rig.camera.camera_model
    assert state["metadata"]["camera_port"] == rig.camera.camera_port
    assert state["metadata"]["camera_serial"] == rig.camera.camera_serial
    assert state["restore"] == "completed"
    assert state["restore_baseline"]["iso"] == "3200"            # the body's pre-meter exposure
    assert state["metadata"]["stack_baseline"]["source"] == "metered"
    assert receipt["baseline"]["source"] == "metered"
    assert receipt["budget"]["baseline_meter_exposures"] == 1
    assert receipt["budget"]["extra_meter_exposures"] == remeter.MAX_REMETER_PASSES
    assert receipt["budget"]["max_physical_exposures"] == 1 + remeter.MAX_REMETER_PASSES + 3


@pytest.mark.parametrize("mode", capture_policy.STACK_MODES)
def test_fixed_session_keeping_current_exposure_takes_no_meter_frame(rig, mode):
    shot = product_capture.shoot(rig.camera, stack_policy={
        "mode": "fixed", "stack_mode": mode, "frames": 3, "baseline": "current"})
    assert shot.ok and not rig.calls and not rig.meters and len(rig.frames) == 3
    assert shot.evidence["physical_exposure_count"] == 3
    receipt = shot.evidence["stack_policy"]
    assert receipt["baseline"]["source"] == "inherited" and receipt["baseline"]["metered"] is None
    assert receipt["budget"]["extra_meter_exposures"] == 0
    assert receipt["budget"]["max_physical_exposures"] == 3


def test_fixed_nine_frame_hdr_uses_bounded_one_ev_steps(rig):
    shot = product_capture.shoot(rig.camera, stack_policy={"mode": "fixed", "frames": 9})
    assert shot.ok and len(rig.frames) == 9 and not rig.calls
    assert [spec["ev_offset"] for _, spec in rig.frames] == list(range(-4, 5))


def test_fixed_stack_with_an_unconfirmed_frame_delivery_warns_in_the_shot_and_its_record(rig):
    """#61's delivery_durability on one session frame reaches the /shoot result and saved record."""
    unconfirmed = {"status": "unconfirmed", "reason": "the photo's folder could not be flushed to disk (failed: EIO)"}
    rig.receipts[2] = {"transport": "fixture", "delivery_durability": unconfirmed}
    shot = product_capture.shoot(rig.camera, stack_policy={"mode": "fixed", "stack_mode": "night", "frames": 3})
    assert shot.ok
    rollup = shot.evidence["delivery_durability"]
    assert rollup["frames"] == [2] and rollup["reasons"] == [unconfirmed["reason"]]
    assert "frame 3 of 3" in rollup["warning"] and rollup["warning"] in shot.summary
    state = product_sessions.runner().read(shot.evidence["stack_policy"]["session_id"])
    assert state["frames"][2]["delivery_durability"] == {"status": "unconfirmed", "reason_at": "transport_receipt.delivery_durability"}
    assert state["frames"][2]["transport_receipt"]["delivery_durability"] == unconfirmed
    assert [frame.get("delivery_durability") for frame in state["frames"][:2]] == [None, None]
    record = json.loads((rig.root / f"{Path(shot.image).stem}.json").read_text())
    assert rollup["warning"] in record["summary"]
    assert record["evidence"]["delivery_durability"] == rollup


def test_fixed_stack_with_confirmed_deliveries_carries_no_durability_warning(rig):
    shot = product_capture.shoot(rig.camera, stack_policy={"mode": "fixed", "stack_mode": "night", "frames": 3})
    assert shot.ok and "delivery_durability" not in shot.evidence and "not confirmed" not in shot.summary
    state = product_sessions.runner().read(shot.evidence["stack_policy"]["session_id"])
    assert "delivery_durability" not in state
    # The saved record says its durability was checked, so /gallery never rereads this manifest.
    record = json.loads((rig.root / f"{Path(shot.image).stem}.json").read_text())
    assert record["evidence"]["delivery_durability_checked"] is True


def test_auto_stack_that_falls_back_to_the_ordinary_photo_keeps_the_durability_warning(rig):
    """Review of #70: an Auto stack with an unconfirmed frame delivery that then fails (frame 3's
    outcome is unknown) keeps the ordinary photo; its response and saved record still say so."""
    rig.motion.update(status="measured", outcome_verified=True, motion_fraction=.01)
    unconfirmed = {"status": "unconfirmed", "reason": "the photo's folder could not be flushed to disk (failed: EIO)"}
    rig.receipts[1] = {"transport": "fixture", "delivery_durability": unconfirmed}
    rig.failures.add(2)
    shot = product_capture.shoot(rig.camera, stack_policy={"mode": "auto", "frames": 3})
    assert shot.ok and shot.image.startswith("ordinary-")
    receipt = shot.evidence["stack_policy"]
    state = product_sessions.runner().read(receipt["session_id"])
    assert state["status"] == "needs_reconciliation"
    rollup = state["delivery_durability"]
    assert rollup["frames"] == [1]
    assert shot.evidence["delivery_durability"] == rollup and receipt["delivery_durability"] == rollup
    assert shot.summary.count(rollup["warning"]) == 1
    record = json.loads((rig.root / f"{Path(shot.image).stem}.json").read_text())
    assert rollup["warning"] in record["summary"] and record["evidence"]["delivery_durability"] == rollup
    assert record["evidence"]["stack_policy"]["delivery_durability_checked"] is True  # /gallery never rereads it


@pytest.mark.parametrize("stack_unconfirmed", [False, True])
def test_successful_auto_stack_keeps_the_ordinary_photos_durability_warning(rig, stack_unconfirmed):
    """Review of #70: Auto's preliminary ordinary photo had an unconfirmed save (#61) and the stack then
    succeeded; the stacked result still says so, beside any distinct session warning."""
    rig.motion.update(status="measured", outcome_verified=True, motion_fraction=.01)
    ordinary = {"status": "unconfirmed", "reason": "the photo's folder could not be flushed to disk (failed: EIO)"}
    rig.ordinary.update(evidence={"delivery_durability": ordinary},
                        summary_suffix=f" Warning: saving was not confirmed durable — {ordinary['reason']}.")
    if stack_unconfirmed:
        rig.receipts[0] = {"transport": "fixture", "delivery_durability": dict(ordinary)}
    shot = product_capture.shoot(rig.camera, stack_policy={"mode": "auto", "frames": 3})
    assert shot.ok and shot.image.startswith("session_")
    receipt = shot.evidence["stack_policy"]
    assert receipt["ordinary_delivery_durability"] == ordinary
    assert f"({receipt['ordinary_image']})" in shot.summary and ordinary["reason"] in shot.summary
    session_warning = (shot.evidence.get("delivery_durability") or {}).get("warning")
    assert bool(session_warning) is stack_unconfirmed  # the session's own rollup keeps the top-level key
    if stack_unconfirmed:
        assert shot.summary.count(session_warning) == 1
    record = json.loads((rig.root / f"{Path(shot.image).stem}.json").read_text())
    assert record["evidence"]["stack_policy"]["ordinary_delivery_durability"] == ordinary
    assert ordinary["reason"] in record["summary"]


def test_fixed_mock_cannot_open_real_session(rig):
    rig.camera.name = "mock"
    with pytest.raises(NotImplementedError, match="exact Sony USB"):
        product_capture.shoot(rig.camera, stack_policy={"mode": "fixed"})
    assert not rig.calls and not rig.frames and not rig.meters
    assert not (rig.root / "sessions").exists()


def test_auto_missing_motion_reuses_ordinary_photo_and_adds_no_meter(rig):
    shot = product_capture.shoot(rig.camera, stack_policy={"mode": "auto", "frames": 9})
    assert len(rig.calls) == 1 and not rig.frames and not rig.meters
    decision = shot.evidence["stack_policy"]["decision"]
    assert decision["mode"] == "single" and "unmeasured" in decision["reason"]
    assert shot.evidence["stack_policy"]["budget"]["max_physical_exposures"] == 11 + remeter.MAX_REMETER_PASSES
    assert shot.evidence["physical_exposure_count"] == 2


def test_auto_proven_motion_and_clipping_trigger_declared_additional_session(rig):
    rig.motion.update(status="measured", outcome_verified=True, motion_fraction=.01)
    shot = product_capture.shoot(rig.camera, stack_policy={"mode": "auto", "frames": 3})
    assert len(rig.calls) == 1 and len(rig.frames) == 3 and shot.ok and not rig.meters
    assert shot.evidence["physical_exposure_count"] == 5
    receipt = shot.evidence["stack_policy"]
    assert receipt["decision"]["mode"] == "hdr"
    assert receipt["decision"]["evidence_source"] == "accepted_jpeg_and_verified_motion"
    assert receipt["baseline"]["source"] == "ordinary_capture"
    assert (rig.root / receipt["ordinary_image"]).is_file()


def test_auto_model_claim_without_verified_measurement_cannot_trigger_stack(rig):
    rig.motion.update(status="predicted", outcome_verified=False, motion_fraction=0)
    shot = product_capture.shoot(rig.camera, stack_policy={"mode": "auto"})
    assert shot.ok and not rig.frames
    assert shot.evidence["stack_policy"]["decision"]["mode"] == "single"


def test_auto_failed_stack_keeps_ordinary_photo_and_durable_session(rig, monkeypatch):
    rig.motion.update(status="measured", outcome_verified=True, motion_fraction=.01)
    def fail(session_id):
        raise RuntimeError("fixture setup failure")
    monkeypatch.setattr(product_sessions, "run", fail)
    shot = product_capture.shoot(rig.camera, stack_policy={"mode": "auto"})
    assert shot.ok and shot.image.startswith("ordinary-") and not rig.frames
    receipt = shot.evidence["stack_policy"]
    assert receipt["session_id"] and receipt["retry_capture"] is False
    state = product_sessions.runner().read(receipt["session_id"])
    assert "fixture setup failure" in state["product_error"]
    assert (rig.root / shot.image).is_file()


def test_fixed_failed_stack_has_no_fake_image_or_empty_sidecar(rig, monkeypatch):
    monkeypatch.setattr(product_sessions, "run", lambda _: (_ for _ in ()).throw(RuntimeError("fixture failure")))
    shot = product_capture.shoot(rig.camera, stack_policy={"mode": "fixed"})
    assert not shot.ok and shot.image == ""
    assert not (rig.root / ".json").exists()
    assert shot.evidence["stack_policy"]["retry_capture"] is False


def test_refinement_receipt_counts_both_engine_attempts(rig, monkeypatch):
    import vision_critique
    monkeypatch.setattr(vision_critique, "critique", lambda *args: {
        "exposure": "too dark", "brightness_delta": 15, "issues": ["dim"]})
    shot = product_capture.shoot(rig.camera, refine=True)
    assert len(rig.calls) == 2 and not rig.frames
    assert shot.evidence["physical_exposure_count"] == 4
    assert len(shot.evidence["capture_timing"]["attempts"]) == 2
    assert shot.evidence["stack_policy"]["budget"]["max_physical_exposures"] == 4 + 2 * remeter.MAX_REMETER_PASSES


def test_ambiguous_session_is_never_reported_as_zero_exposures(rig, monkeypatch):
    def ambiguous(session_id):
        sessions = product_sessions.runner()
        state = sessions.read(session_id)
        state["status"] = "failed"
        state["frames"][0]["status"] = "ambiguous"
        sessions.save(state)
        return state
    monkeypatch.setattr(product_sessions, "run", ambiguous)
    shot = product_capture.shoot(rig.camera, stack_policy={"mode": "fixed"})
    assert not shot.ok and not shot.image
    assert shot.evidence["physical_exposure_count"] is None
    assert shot.evidence["stack_policy"]["may_have_fired"] is True
    assert shot.evidence["stack_policy"]["retry_capture"] is False


def test_policy_cannot_bypass_session_capacity_limit(rig, monkeypatch):
    from stack_session import SessionRunner
    monkeypatch.setattr(SessionRunner, "list", lambda self: [{}] * 100)
    with pytest.raises(ValueError, match="limit 100"):
        product_capture.shoot(rig.camera, stack_policy={"mode": "fixed"})
    assert not rig.calls and not rig.frames and not rig.meters
