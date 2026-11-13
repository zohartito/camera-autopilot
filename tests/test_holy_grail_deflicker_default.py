"""Deflicker is on by default for Holy Grail timelapses (owner decision, 2026-09-27).

Camera-free. The default is the setting the Wave 0 simulation measured (window 7,
max 1 stop); an explicit request, including an explicit off, always wins, and every
other timelapse policy stays off unless asked.
"""
import inspect
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
for entry in (ROOT, ROOT / "backend", ROOT / "tools"):
    sys.path.insert(0, str(entry))

from stack_session import SessionRunner  # noqa: E402
from timelapse_controls import HOLY_GRAIL_DEFLICKER_DEFAULT, read_controls  # noqa: E402

RECOMMENDED = {"enabled": True, "window": 7, "max_gain_stops": 1.0}


def test_a_holy_grail_session_gets_the_recommended_deflicker_by_default(tmp_path):
    sessions = SessionRunner(tmp_path)
    state = sessions.create("timelapse", 3, timelapse_mode="holy_grail", timelapse_options={"max_total_ev": 10})
    assert read_controls(sessions, state["id"])["deflicker"] == RECOMMENDED
    assert "deflicker" not in state["timelapse"]  # a render control, not an exposure option


def test_an_explicit_off_keeps_holy_grail_deflicker_off(tmp_path):
    sessions = SessionRunner(tmp_path)
    state = sessions.create("timelapse", 3, timelapse_mode="holy_grail",
                            timelapse_options={"deflicker": {"enabled": False}})
    assert read_controls(sessions, state["id"])["deflicker"] is None


def test_explicit_holy_grail_deflicker_settings_are_kept(tmp_path):
    sessions = SessionRunner(tmp_path)
    chosen = {"enabled": True, "window": 5, "max_gain_stops": 0.5}
    state = sessions.create("timelapse", 3, timelapse_mode="holy_grail", timelapse_options={"deflicker": chosen})
    assert read_controls(sessions, state["id"])["deflicker"] == chosen


@pytest.mark.parametrize("policy", ["fixed", "auto_custom"])
def test_other_policies_stay_off_unless_asked(tmp_path, policy):
    sessions = SessionRunner(tmp_path)
    state = sessions.create("timelapse", 3, timelapse_mode=policy)
    assert read_controls(sessions, state["id"])["deflicker"] is None
    assert not (sessions._directory(state["id"]) / "controls.json").exists()


def test_other_session_modes_are_unchanged(tmp_path):
    sessions = SessionRunner(tmp_path)
    state = sessions.create("hdr", 3)
    assert not (sessions._directory(state["id"]) / "controls.json").exists()
    with pytest.raises(ValueError, match="requires timelapse mode"):
        sessions.create("night", 3, timelapse_mode="holy_grail")


def test_the_default_is_the_setting_the_wave0_simulation_measured():
    # docs/WAVE0_TIMELAPSE_2026-09-22.md: ~0.045 EV rendered steps with the render path's
    # deflicker at its defaults; the simulator and the gain function share them.
    import deflicker
    import simulate_holy_grail
    assert HOLY_GRAIL_DEFLICKER_DEFAULT == RECOMMENDED
    for function in (deflicker.deflicker_gains, simulate_holy_grail.deflickered_log_luminance):
        parameters = inspect.signature(function).parameters
        assert parameters["window"].default == RECOMMENDED["window"]
        assert parameters["max_gain_stops"].default == RECOMMENDED["max_gain_stops"]

