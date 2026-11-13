"""Regressions for the independent review of the section 1 wave."""
import sys
import threading
from pathlib import Path

import numpy as np
import pytest
from PIL import Image

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))
import rig_profile  # noqa: E402
import sensor_noise  # noqa: E402
import stack_session  # noqa: E402
import wifi_autopilot  # noqa: E402
from handheld import HandheldSession  # noqa: E402


def test_generic_sony_tamron_alias_needs_a_corroborating_spec(tmp_path):
    body = "Sony ILCE-7M2 (Control)"
    bare = rig_profile.resolve_rig(body, lens_model="E 28-75mm F2.8-2.8", profiles_dir=tmp_path)
    assert bare.calibration_status == "uncalibrated" and not bare.lens_profiled
    corroborated = rig_profile.resolve_rig(body, lens_model="E 28-75mm F2.8-2.8",
                                           lens_specification=(28.0, 75.0, 2.8, 2.8), profiles_dir=tmp_path)
    assert corroborated.calibration_status == "existing_profile"
    other = rig_profile.resolve_rig(body, lens_model="E 28-75mm F2.8-2.8",
                                    lens_specification=(24.0, 70.0, 2.8, 2.8), profiles_dir=tmp_path)
    assert other.calibration_status == "uncalibrated"


def test_handheld_out_dir_failure_is_an_error_not_a_stuck_created(tmp_path):
    blocker = tmp_path / "handheld"
    blocker.write_bytes(b"not a directory")
    session = HandheldSession(lambda: None, lambda *a: {}, max_frames=1, max_duration_s=5,
                              out_dir=blocker / "session")
    with pytest.raises(OSError):
        session.run(threading.Event())
    assert session.state()["status"] == "error"


class _StepCamera:
    def __init__(self, step):
        self.step = step
        self.calls = []
    def exposure_compensation_step(self):
        return self.step
    def set_exposure_compensation(self, index):
        self.calls.append(index)
        return index


def test_exposure_bias_index_respects_half_ev_bodies():
    assert wifi_autopilot._bias_index(_StepCamera(1), "over") == 3
    assert wifi_autopilot._bias_index(_StepCamera(2), "over") == 2
    assert wifi_autopilot._bias_index(_StepCamera(2), "under") == -2
    assert wifi_autopilot._bias_index(_StepCamera(1), "neutral") == 0
    assert wifi_autopilot._bias_index(_StepCamera(1), "weird") is None


def test_monotone_iso_ceiling_stops_at_the_first_exceedance():
    isos = [100, 200, 400, 800, 1600]
    sigma = [1.0, 1.2, 3.0, 1.5, 1.6]  # 400 exceeds 2.5x, 800 dips back under
    assert sensor_noise.max_usable_iso_from_table(isos, sigma) == 200


def test_crowd_merge_respects_frame_budget(tmp_path, monkeypatch):
    frames = []
    for index in range(stack_session.CROWD_AUTO_MAX_FRAMES + 1):
        path = tmp_path / f"f{index}.jpg"
        Image.fromarray(np.full((16, 24, 3), 90 + index, np.uint8)).save(path, quality=95)
        frames.append(str(path))
    out = tmp_path / "result.jpg"
    receipt = stack_session._merge_crowd_remove(frames, str(out))
    assert receipt["background_estimate"] == "auto_mask_skipped_frame_budget" and out.is_file()
    assert not (tmp_path / "crowd_auto").exists()


def test_crowd_merge_writes_support_and_never_maps(tmp_path):
    rng = np.random.default_rng(7)
    background = rng.integers(40, 200, (32, 48, 3), dtype=np.uint8)
    frames = []
    for index in range(4):
        frame = background.copy()
        frame[4:12, 6 + index * 8: 14 + index * 8] = 255  # a white block moving across
        path = tmp_path / f"f{index}.jpg"
        Image.fromarray(frame).save(path, quality=95)
        frames.append(str(path))
    out = tmp_path / "result.jpg"
    receipt = stack_session._merge_crowd_remove(frames, str(out))
    maps = tmp_path / "crowd_auto"
    assert (maps / "support_count.png").is_file() and (maps / "never_observed.png").is_file()
    assert (maps / "not_cleared.png").is_file()
    assert receipt["maps"] == ["support_count.png", "never_observed.png", "not_cleared.png"]
    # The honest revealed/not-cleared shares are reported, are a valid partition, and the summary
    # states the uncleared share for the user.
    assert 0.0 <= receipt["not_cleared_fraction"] <= 1.0
    assert abs(receipt["revealed_fraction"] + receipt["not_cleared_fraction"] - 1.0) < 1e-9
    assert receipt["min_background_visible_fraction"] == 0.25
    assert "could not be cleared" in receipt["summary"]
    assert out.is_file()


def test_session_create_accepts_deflicker_inside_timelapse_options(tmp_path):
    runner = stack_session.SessionRunner(tmp_path)
    created = runner.create("timelapse", 4, ev_step=0, interval_s=1, timelapse_mode="holy_grail",
                            timelapse_options={"deflicker": {"enabled": True, "window": 5, "max_gain_stops": 1.0}})
    from timelapse_controls import read_controls
    controls = read_controls(runner, created["id"])
    assert controls["deflicker"] == {"enabled": True, "window": 5, "max_gain_stops": 1.0}
    assert "deflicker" not in created["timelapse"]
    with pytest.raises(ValueError):
        runner.create("hdr", 3, timelapse_options={"deflicker": {"enabled": True}})


def test_night_planner_refuses_when_no_iso_in_base_to_ceiling_range():
    import camera_caps
    caps = camera_caps.from_live_choices("Sony Alpha-A7 III (PC Control)", ["25", "50"], ["f/4"], ["1", "1/2"])
    rig = rig_profile.resolve_rig("Sony Alpha-A7 III (PC Control)", lens_model="FE 24-105mm F4 G OSS",
                                  lens_specification=(24.0, 105.0, 4.0, 4.0))
    import night_assist
    with pytest.raises(ValueError):
        night_assist.plan_night(2.0, rig, caps)


def test_web_night_assist_carries_the_plan_into_the_session_request():
    html = Path(__file__).resolve().parents[1].joinpath("backend/web/index.html").read_text()
    assert "nightAssistRequest=plan.session_request" in html
    # Start submits the plan it was metered for (its night_assist and camera fields); the
    # behaviour, including refusing another body, is exercised in tests/test_capture_serial.py.
    assert "night_assist:nightPlan.night_assist" in html
    assert "plan.status!=='ok' || !plan.session_request" in html


def test_pi_bringup_never_runs_sudo():
    script = Path(__file__).resolve().parents[1].joinpath("deploy/pi_bringup.sh").read_text()
    import re
    executable_sudo = [line for line in script.splitlines() if re.match(r"^\s*sudo\b", line)]
    assert executable_sudo == []  # sudo appears only inside printed operator commands and comments
    assert "operator_step=true" in script
