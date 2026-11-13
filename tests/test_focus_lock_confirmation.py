"""Confirm — or actively acquire — the AF lock before freezing focus into Manual.

On the A7 II `focusindication` reads "Unlock" once AF is released, so a passive read alone
can only time out; the engine drives ONE bounded half-press when the meter frame is not
already locked. Every case must attempt exactly ONE final exposure (capture #2) and never
retry. Confirming an AF lock is not proof of optical sharpness.
"""
import pytest

from test_final_wave_engine import rig  # noqa: F401 (fixture; also puts backend/ on sys.path)
import engine


def _wire(camera, reads, half_press="Focus Locked"):
    """Attach a focusindication reader that walks `reads` and a counted half-press stub.

    `half_press` may be a value the driver returns, or an Exception instance to raise.
    Returns a state dict with the read index (`n`) and the half-press call count (`af`).
    """
    state = {"n": 0, "af": 0, "af_hold": None}

    def read():
        value = reads[min(state["n"], len(reads) - 1)]
        state["n"] += 1
        return value

    def drive(hold_s):
        state["af"] += 1
        state["af_hold"] = hold_s
        if isinstance(half_press, BaseException):
            raise half_press
        return half_press

    camera.read_focus_indication = read
    camera.autofocus_half_press = drive
    return state


def _af_lock(shot):
    return shot.evidence["final_focus_policy"]["af_focus_lock"]


def test_already_locked_freezes_without_driving_af(rig):
    state = _wire(rig.camera, ["Focus Locked"])
    shot = rig.real.shoot(None, variant_id="natural")
    lock = _af_lock(shot)
    assert lock["confirmed"] is True and lock["basis"] == "confirmed"
    assert lock["af_search"] == "none_already_locked"
    assert state["af"] == 0  # A7 III fast path: no half-press
    assert lock["recommend_refine"] is False
    assert shot.evidence["final_focus_policy"]["sharpness_verified"] is False
    assert "already reported AF locked" in shot.evidence["final_focus_policy"]["reason"]
    assert rig.camera.capture_count == 2
    assert shot.ok is True


def test_not_locked_drives_exactly_one_half_press(rig):
    state = _wire(rig.camera, ["Unlock"], half_press="Focus Locked")
    shot = rig.real.shoot(None, variant_id="natural")
    lock = _af_lock(shot)
    assert lock["confirmed"] is True and lock["basis"] == "confirmed"
    assert lock["af_search"] == "driven_half_press"
    assert state["af"] == 1  # exactly one AF search, never a retry loop
    assert state["af_hold"] == engine.FOCUS_AF_DRIVE_HOLD_S
    assert lock["indication_during_half_press"] == "Focus Locked"
    assert "one bounded autofocus half-press was driven" in shot.evidence[
        "final_focus_policy"]["reason"].lower()
    assert rig.camera.capture_count == 2
    assert shot.ok is True


def test_post_release_read_is_consulted_when_held_read_is_unlocked(rig):
    # Held read says Unlock; the body reports Focus Locked only after release.
    state = _wire(rig.camera, ["Unlock", "Focus Locked"], half_press="Unlock")
    shot = rig.real.shoot(None, variant_id="natural")
    lock = _af_lock(shot)
    assert lock["confirmed"] is True and lock["basis"] == "confirmed"
    assert lock["indication_during_half_press"] == "Unlock"
    assert lock["indication_after_release"] == "Focus Locked"
    assert state["af"] == 1 and state["n"] == 2  # one passive read + one post-release read
    assert rig.camera.capture_count == 2
    assert shot.ok is True


def test_never_locks_after_drive_proceeds_and_summary_says_so(rig):
    state = _wire(rig.camera, ["Unlock"], half_press="Unlock")
    shot = rig.real.shoot(None, variant_id="natural")
    lock = _af_lock(shot)
    assert lock["confirmed"] is False and lock["basis"] == "drove_af_no_lock"
    assert lock["af_search"] == "driven_half_press"
    assert state["af"] == 1
    assert lock["recommend_refine"] is True
    # Proceeded exactly as before: one meter + one final, no retry, photo kept.
    assert rig.camera.capture_count == 2
    assert shot.ok is True
    assert "did not confirm autofocus lock" in shot.summary
    assert "Enable Refine" in shot.summary


def test_low_contrast_after_drive_is_a_definitive_negative(rig):
    _wire(rig.camera, ["Unlock", "No Focus - Low Contrast"], half_press="No Focus - Low Contrast")
    shot = rig.real.shoot(None, variant_id="natural")
    lock = _af_lock(shot)
    assert lock["confirmed"] is False and lock["basis"] == "negative_no_lock"
    assert lock["recommend_refine"] is True
    assert rig.camera.capture_count == 2
    assert "did not confirm autofocus lock" in shot.summary


def test_half_press_error_is_swallowed_and_capture_proceeds(rig):
    state = _wire(rig.camera, ["Unlock"], half_press=RuntimeError("AF drive glitch"))
    shot = rig.real.shoot(None, variant_id="natural")
    lock = _af_lock(shot)
    assert lock["confirmed"] is False and lock["basis"] == "confirmation_unavailable"
    assert "AF drive glitch" in lock["read_error"]
    assert state["af"] == 1  # attempted once, error swallowed
    assert lock["recommend_refine"] is False  # unavailable, not a claimed failure to lock
    assert rig.camera.capture_count == 2
    assert shot.ok is True


def test_absent_indication_node_drives_no_af_and_behaves_as_before(rig):
    state = _wire(rig.camera, [None])
    shot = rig.real.shoot(None, variant_id="natural")
    lock = _af_lock(shot)
    assert lock["confirmed"] is False and lock["basis"] == "node_absent"
    assert state["af"] == 0  # nothing to acquire; do not drive AF
    assert lock["recommend_refine"] is False
    assert rig.camera.capture_count == 2
    assert "did not confirm autofocus lock" not in shot.summary


def test_body_without_the_read_helper_records_unavailable(rig):
    # The default FakeCamera has neither helper; behave exactly as today.
    assert not hasattr(rig.camera, "read_focus_indication")
    shot = rig.real.shoot(None, variant_id="natural")
    lock = _af_lock(shot)
    assert lock["confirmed"] is False and lock["basis"] == "confirmation_unavailable"
    assert lock["recommend_refine"] is False
    assert rig.camera.capture_count == 2
    assert "did not confirm autofocus lock" not in shot.summary


def test_passive_read_error_never_fails_the_capture(rig):
    def boom():
        raise RuntimeError("USB read glitch")
    rig.camera.read_focus_indication = boom
    rig.camera.autofocus_half_press = lambda hold_s: pytest.fail("must not drive AF after read error")
    shot = rig.real.shoot(None, variant_id="natural")
    lock = _af_lock(shot)
    assert lock["confirmed"] is False and lock["basis"] == "confirmation_unavailable"
    assert "USB read glitch" in lock["read_error"]
    assert rig.camera.capture_count == 2
    assert shot.ok is True


def test_manual_metering_focus_neither_reads_nor_drives(rig):
    rig.camera.settings["focus_mode"] = "Manual"
    called = {"read": 0, "af": 0}
    rig.camera.read_focus_indication = lambda: called.__setitem__("read", called["read"] + 1) or "Unlock"
    rig.camera.autofocus_half_press = lambda hold_s: called.__setitem__("af", called["af"] + 1) or "Focus Locked"
    shot = rig.real.shoot(None, variant_id="natural")
    lock = _af_lock(shot)
    assert lock["basis"] == "manual_focus_no_af_search" and lock["af_search"] == "none_manual_focus"
    assert lock["confirmed"] is False and lock["recommend_refine"] is False
    assert called["read"] == 0 and called["af"] == 0  # no AF ran, nothing to confirm
    assert rig.camera.capture_count == 2
    assert "did not confirm autofocus lock" not in shot.summary
