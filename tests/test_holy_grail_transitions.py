"""Holy Grail control law: meter anchoring, smoothed scene level, anti-windup.

Camera-free. The simulated runs drive the real cascade over a Sony third-stop
ladder under the 1 s USB transport cap; they prove the control law, not a sunset.
"""
import math
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
for entry in (ROOT, ROOT / "backend", ROOT / "tools"):
    sys.path.insert(0, str(entry))

from camera_caps import A7II_CAPS  # noqa: E402
from simulate_holy_grail import simulate  # noqa: E402
from stack_planner import metered_timelapse_step, timelapse_config  # noqa: E402

STEP = dict(base_shutter_s=1 / 60, base_iso=100, caps=A7II_CAPS)


def test_holy_grail_defaults_to_smoothed_level_and_day_to_night_range():
    config = timelapse_config("holy_grail")
    assert config["smoothing_frames"] == 8 and config["max_total_ev"] == 20
    assert timelapse_config("auto_custom")["smoothing_frames"] == 1
    assert timelapse_config("fixed")["max_total_ev"] == 6


@pytest.mark.parametrize("options", [{"smoothing_frames": 0}, {"smoothing_frames": 61},
                                     {"smoothing_frames": 2.5}, {"smoothing_frames": True},
                                     {"max_total_ev": 21}])
def test_transition_and_total_range_options_are_bounded(options):
    with pytest.raises(ValueError):
        timelapse_config("holy_grail", options)


def test_meter_is_anchored_on_the_exposure_the_frame_actually_used():
    config = timelapse_config("auto_custom")
    # The frame was exposed 0.3 EV brighter than the smooth ramp asked for and metered
    # exactly on target, so the scene needs +0.3 EV, not the ramp's 0.
    anchored = metered_timelapse_step(config, current_ev=0, luminance=.18, measured_ev=.3, **STEP)
    assert anchored["target_ev"] == pytest.approx(.3)
    legacy = metered_timelapse_step(config, current_ev=0, luminance=.18, **STEP)
    assert legacy["target_ev"] == pytest.approx(0)


def test_filter_state_round_trips_and_every_step_respects_the_cap():
    config = timelapse_config("holy_grail")
    ramp = {"requested_ev": 0.0, "actual_ev": 0.0}
    for _ in range(40):
        step = metered_timelapse_step(config, current_ev=ramp["requested_ev"], luminance=.02,
                                      measured_ev=ramp["actual_ev"],
                                      scene_estimate=ramp.get("scene_ev_estimate"), **STEP)
        assert abs(step["requested_step_ev"]) <= config["max_step_ev"] + 1e-9
        assert math.isfinite(step["scene_ev_estimate"])
        ramp.update(step)


def test_range_limit_holds_the_ramp_at_the_achieved_exposure():
    config = timelapse_config("holy_grail", {"max_shutter_s": 1 / 60, "max_iso": 200})
    ramp = {"requested_ev": 0.0, "actual_ev": 0.0}
    for _ in range(30):
        step = metered_timelapse_step(config, current_ev=ramp["requested_ev"], luminance=.001,
                                      measured_ev=ramp["actual_ev"], **STEP)
        ramp.update(step)
    assert step["range_limited"] and step["requested_ev"] == pytest.approx(step["actual_ev"])
    assert step["actual_ev"] == pytest.approx(1)  # ISO 200 at 1/60 is all the range allows


def test_dusk_beyond_the_ranges_is_reported_and_the_ramp_waits_at_the_edge():
    run = simulate(scene="sunset", frames=300, darken_ev=18)
    last = run["rows"][-1]
    assert run["metrics"]["range_limited_frames"] > 0
    assert (last["shutter"], last["iso"]) == ("1", "6400")  # 1 s USB cap, default ISO ceiling
    assert last["requested_ev"] == pytest.approx(last["actual_ev"])


def test_product_session_carries_the_meter_anchor_and_filter_between_frames(tmp_path, monkeypatch):
    from types import SimpleNamespace
    from PIL import Image
    import engine
    import product_sessions
    import sonyremote
    monkeypatch.setattr(engine, "SHOTS_DIR", tmp_path / "shots")
    live = {"iso": "100", "shutter": "1/60", "aperture": "8", "focus_mode": "Automatic"}

    class Camera:
        def __init__(self, **kwargs): pass
        def __enter__(self): return self
        def __exit__(self, *args): pass
        def info(self): return SimpleNamespace(model="Sony ILCE-7M2 (Control)", port="usb:001,002")
        def wake(self): return True
        def shooting_mode(self): return "M"
        def get_settings(self): return dict(live)
        def list_options(self, key):
            return {"iso": ["100", "125", "160", "200", "250"], "aperture": ["8"], "shutter": ["1/60"]}[key]
        def set_setting(self, key, value): live[key] = value
        def set_exposure(self, **kwargs): live.update(kwargs)
        def capture(self, **kwargs):
            path = tmp_path / f"source_{len(list(tmp_path.glob('source_*')))}.jpg"
            Image.new("RGB", (64, 48), (30, 30, 30)).save(path)
            return str(path)

    monkeypatch.setattr(sonyremote, "Camera", Camera)
    monkeypatch.setattr(engine, "_free_usb", lambda: None)
    runner = product_sessions.runner()
    state = runner.create("timelapse", 4, timelapse_mode="holy_grail", timelapse_options={"max_shutter_s": 1 / 60},
                          metadata={"camera_model": "Sony ILCE-7M2 (Control)", "camera_port": "usb:001,002", "camera_serial": "A7II00SERIAL"})
    result = product_sessions.run(state["id"])
    assert result["status"] == "completed", result
    meters = [frame["applied"]["timelapse_meter"] for frame in result["frames"]]
    for previous, current in zip(meters[1:], meters[2:]):
        assert current["measured_ev"] == pytest.approx(previous["actual_ev"])
        assert "scene_ev_estimate" in previous
    assert meters[1]["measured_ev"] == pytest.approx(meters[0]["actual_ev"])


def test_total_ev_cap_is_reported_instead_of_hidden():
    capped = simulate(scene="sunset", frames=300, darken_ev=10, options={"max_total_ev": 6})
    assert capped["metrics"]["range_limited_frames"] > 0
    assert capped["metrics"]["actual_ev_span"] <= 6 + 1e-9


def test_simulated_ten_stop_sunset_is_tracked_smoothly():
    run = simulate(scene="sunset", frames=300, darken_ev=10)
    metrics = run["metrics"]
    assert metrics["requested_step_reversals"] == 0          # no hunting on a smooth dusk
    assert metrics["actual_ev_span"] >= 9.5                   # reaches night, not a 6 EV wall
    assert metrics["range_limited_frames"] == 0
    assert metrics["exposure_error_ev"]["mean"] < .2
    assert metrics["exposure_error_ev"]["p95"] < .5
    assert metrics["frame_to_frame_ev_rendered"]["p95"] < .06  # after the existing deflicker
    steps = [b["requested_ev"] - a["requested_ev"] for a, b in zip(run["rows"], run["rows"][1:])]
    assert max(abs(s) for s in steps) <= .3 + 1e-9


@pytest.mark.parametrize("change_ev", [2, 5])
def test_a_sudden_light_change_is_followed_without_overshoot(change_ev):
    rows = simulate(scene="step", frames=160, darken_ev=change_ev)["rows"][160 // 3:]
    assert max(row["requested_ev"] for row in rows) <= change_ev + .17
    landed = next(i for i, row in enumerate(rows) if abs(row["requested_ev"] - change_ev) < .17)
    assert landed <= 20  # Arsenal documents transitions of about 20 frames


def test_smoothed_level_hunts_less_than_frame_by_frame_tracking_in_noisy_light():
    noisy = dict(scene="sunset", frames=300, darken_ev=5, flicker_ev=.05)
    fast = simulate(options={"smoothing_frames": 1}, **noisy)["metrics"]
    smooth = simulate(**noisy)["metrics"]
    assert smooth["requested_step_reversals"] < fast["requested_step_reversals"] / 2
    assert smooth["frame_to_frame_ev_raw"]["p95"] < fast["frame_to_frame_ev_raw"]["p95"]


@pytest.mark.parametrize("glitch_ev", [20, -8])  # a frame at the meter floor, a white frame
def test_a_single_glitched_meter_frame_does_not_start_a_long_excursion(monkeypatch, glitch_ev):
    import simulate_holy_grail as sim
    original = sim.scene_ev
    monkeypatch.setattr(sim, "scene_ev", lambda kind, index, *args:
                        original(kind, index, *args) + (glitch_ev if index == 100 else 0))
    rows = sim.simulate(scene="flat", frames=160, darken_ev=0)["rows"][101:]
    assert max(abs(row["requested_ev"]) for row in rows) <= .31  # one max_step, as unsmoothed
    assert sum(abs(row["requested_ev"]) > .2 for row in rows) <= 3


def test_priority_change_mid_run_keeps_the_ramp_continuous():
    run = simulate(scene="sunset", frames=300, darken_ev=5, priority_switch=(150, "iso_then_shutter"))
    rows = run["rows"]
    assert rows[-1]["shutter"] == "1/500" and int(rows[-1]["iso"]) > 1000
    assert abs(rows[150]["requested_ev"] - rows[149]["requested_ev"]) <= .3 + 1e-9
    assert run["metrics"]["frame_to_frame_ev_rendered"]["max"] < .1


def test_smartlapse_accepts_a_ten_second_movie_up_to_the_sequence_limit():
    from stack_session import MAX_SEQUENCE_FRAMES
    from timelapse_controls import smartlapse_settings
    plan = smartlapse_settings({"duration_s": 3600, "output_s": 10, "fps": 24})
    assert plan["requested_frames"] == MAX_SEQUENCE_FRAMES == 240
    assert plan["interval_s"] == pytest.approx(3600 / 239)
    with pytest.raises(ValueError):
        smartlapse_settings({"duration_s": 3600, "output_s": 10.1, "fps": 24})


def test_mode_switch_applies_the_destination_modes_defaults(tmp_path):
    from stack_session import SessionRunner
    from timelapse_controls import update_controls
    sessions = SessionRunner(tmp_path)
    state = sessions.create("timelapse", 3)  # fixed
    holy = update_controls(sessions, state["id"], {"exposure": {"mode": "holy_grail"}})["exposure"]
    assert (holy["smoothing_frames"], holy["max_total_ev"], holy["max_step_ev"]) == (8, 20, .3)
    auto = update_controls(sessions, state["id"], {"exposure": {"mode": "auto_custom"}})["exposure"]
    assert (auto["smoothing_frames"], auto["max_total_ev"], auto["max_step_ev"]) == (1, 6, 1.0)
    chosen = update_controls(sessions, state["id"], {"exposure": {"mode": "holy_grail", "smoothing_frames": 12}})
    assert chosen["exposure"]["smoothing_frames"] == 12 and chosen["exposure"]["max_step_ev"] == .3


def test_a_session_stored_before_smoothing_keeps_its_original_law(tmp_path):
    from stack_session import SessionRunner
    from timelapse_controls import update_controls
    sessions = SessionRunner(tmp_path)
    # Deflicker off: a session from before smoothing had no controls file (the Holy Grail
    # deflicker default writes one at creation).
    state = sessions.create("timelapse", 3, timelapse_mode="holy_grail",
                            timelapse_options={"deflicker": {"enabled": False}})
    stored = sessions.read(state["id"])
    del stored["timelapse"]["smoothing_frames"]  # as written before this change
    sessions.save(stored)
    edited = update_controls(sessions, state["id"], {"exposure": {"priority": "iso_then_shutter"}})
    assert edited["exposure"]["smoothing_frames"] == 1


def test_restart_rebuilds_the_same_ramp_from_the_persisted_frame_record():
    import json
    config = timelapse_config("holy_grail")
    def run(frames, restart_at=None):
        ramp, out = {"requested_ev": 0.0, "actual_ev": 0.0}, []
        for index in range(frames):
            if index == restart_at:  # only what the manifest keeps survives
                ramp = json.loads(json.dumps(out[-1]))
            luminance = .18 * 2 ** (ramp["actual_ev"] - (3 if index > 5 else 0))
            step = metered_timelapse_step(config, current_ev=ramp["requested_ev"], luminance=luminance,
                                          measured_ev=ramp["actual_ev"],
                                          scene_estimate=ramp.get("scene_ev_estimate"), **STEP)
            ramp.pop("scene_ev_estimate", None)
            ramp.update(step)
            out.append(step)
        return [step["requested_ev"] for step in out]
    assert run(30, restart_at=12) == pytest.approx(run(30))


def test_the_incoming_scene_estimate_is_what_gets_smoothed():
    config = timelapse_config("holy_grail")
    reading = dict(current_ev=0, luminance=.18 / 8, measured_ev=0, **STEP)  # the scene needs +3 EV
    fresh = metered_timelapse_step(config, **reading)  # starts from the metered frame's EV (0)
    carried = metered_timelapse_step(config, scene_estimate=2.0, **reading)
    assert fresh["scene_ev_estimate"] == pytest.approx(2 / 9 * 1.5)      # alpha * clipped surprise
    assert carried["scene_ev_estimate"] == pytest.approx(2 + 2 / 9 * 1.0)  # surprise 3 - 2


def test_a_hard_range_edge_is_flagged_however_small_the_shortfall():
    config = timelapse_config("auto_custom", {"min_shutter_s": 1 / 60, "max_shutter_s": 1 / 60,
                                              "min_iso": 100, "max_iso": 100})
    step = metered_timelapse_step(config, current_ev=0, luminance=.18 / 2 ** .2, measured_ev=0, **STEP)
    assert step["reachable_ev"] == [0, 0] and step["actual_ev"] == 0 and step["range_limited"]


def test_a_total_cap_below_one_ladder_step_reaches_only_real_settings():
    config = timelapse_config("auto_custom", {"max_total_ev": .1, "min_iso": 100, "max_iso": 100})
    step = metered_timelapse_step(config, current_ev=0, luminance=.18 / 2, measured_ev=0, **STEP)
    assert step["reachable_ev"] == [0, 0] and step["requested_ev"] == 0 and step["range_limited"]


@pytest.mark.parametrize("mode", ["fixed", "auto_custom"])
def test_only_holy_grail_accepts_a_range_beyond_six_ev(mode):
    with pytest.raises(ValueError):
        timelapse_config(mode, {"max_total_ev": 7})


def test_a_meter_reading_that_overflows_is_refused():
    with pytest.raises(ValueError, match="representable"):
        metered_timelapse_step(timelapse_config("holy_grail"), current_ev=0, luminance=5e-324,
                               measured_ev=0, **STEP)


def test_only_holy_grail_gets_the_three_hour_execution_budget():
    from product_sessions import execution_budget_s
    assert execution_budget_s("timelapse", timelapse_config("holy_grail")) == 3 * 3600
    assert execution_budget_s("timelapse", timelapse_config("auto_custom")) == 3600
    assert execution_budget_s("timelapse", timelapse_config("fixed")) == 3600
    assert execution_budget_s("night", {"mode": "holy_grail"}) == 3600  # only a timelapse qualifies
    assert execution_budget_s("timelapse", None) == 3600


@pytest.mark.parametrize("mode, completes", [("holy_grail", True), ("auto_custom", False)])
def test_a_session_past_one_hour_continues_only_for_holy_grail(tmp_path, monkeypatch, mode, completes):
    from types import SimpleNamespace
    from PIL import Image
    import engine
    import product_sessions
    import sonyremote
    monkeypatch.setattr(engine, "SHOTS_DIR", tmp_path / "shots")
    live = {"iso": "100", "shutter": "1/60", "aperture": "8", "focus_mode": "Automatic"}
    shots = []

    class Camera:
        def __init__(self, **kwargs): pass
        def __enter__(self): return self
        def __exit__(self, *args): pass
        def info(self): return SimpleNamespace(model="Sony ILCE-7M2 (Control)", port="usb:001,002")
        def wake(self): return True
        def shooting_mode(self): return "M"
        def get_settings(self): return dict(live)
        def list_options(self, key):
            return {"iso": ["100", "125", "160", "200"], "aperture": ["8"], "shutter": ["1/60"]}[key]
        def set_setting(self, key, value): live[key] = value
        def set_exposure(self, **kwargs): live.update(kwargs)
        def capture(self, **kwargs):
            shots.append(1)
            path = tmp_path / f"source_{len(shots)}.jpg"
            Image.new("RGB", (64, 48), (90, 90, 90)).save(path)
            return str(path)

    monkeypatch.setattr(sonyremote, "Camera", Camera)
    monkeypatch.setattr(engine, "_free_usb", lambda: None)
    runner = product_sessions.runner()
    state = runner.create("timelapse", 2, timelapse_mode=mode, timelapse_options={"max_shutter_s": 1 / 60},
                          metadata={"camera_model": "Sony ILCE-7M2 (Control)", "camera_port": "usb:001,002", "camera_serial": "A7II00SERIAL"})
    stored = runner.read(state["id"])
    stored["smartlapse_status"] = {"elapsed_s": 3700.0}  # already executed for more than an hour
    runner.save(stored)
    if completes:
        assert product_sessions.run(state["id"])["status"] == "completed"
        assert len(shots) == 2
    else:
        result = product_sessions.run(state["id"])
        assert result["status"] == "failed" and "1-hour session execution budget" in result["error"]
        assert shots == [] and all(frame["status"] == "pending" for frame in result["frames"])


def test_a_black_first_frame_is_clipped_like_any_other(monkeypatch):
    import simulate_holy_grail as sim
    original = sim.scene_ev
    monkeypatch.setattr(sim, "scene_ev", lambda kind, index, *args:
                        original(kind, index, *args) + (20 if index == 0 else 0))
    rows = sim.simulate(scene="flat", frames=60, darken_ev=0)["rows"][1:]
    assert max(abs(row["requested_ev"]) for row in rows) <= .31
    assert sum(abs(row["requested_ev"]) > .2 for row in rows) <= 3


def test_an_explicit_smoothing_value_on_a_legacy_session_is_kept(tmp_path):
    from stack_session import SessionRunner
    from timelapse_controls import update_controls
    sessions = SessionRunner(tmp_path)
    state = sessions.create("timelapse", 3, timelapse_mode="holy_grail",
                            timelapse_options={"deflicker": {"enabled": False}})  # legacy: no controls file
    stored = sessions.read(state["id"])
    del stored["timelapse"]["smoothing_frames"]
    sessions.save(stored)
    assert update_controls(sessions, state["id"], {"exposure": {"smoothing_frames": 8}})["exposure"]["smoothing_frames"] == 8


def test_a_range_the_next_mode_cannot_hold_falls_back_to_its_default(tmp_path):
    from stack_session import SessionRunner
    from timelapse_controls import update_controls
    sessions = SessionRunner(tmp_path)
    state = sessions.create("timelapse", 3, timelapse_mode="holy_grail", timelapse_options={"max_total_ev": 10})
    fixed = update_controls(sessions, state["id"], {"exposure": {"mode": "fixed"}})["exposure"]
    assert fixed["max_total_ev"] == 6


def _switching_session(tmp_path, monkeypatch, modes, *, elapsed_s=0.0, grey=30):
    """Run a real product session whose live exposure mode follows `modes[accepted_count]`."""
    from types import SimpleNamespace
    from PIL import Image
    import engine
    import product_sessions
    import sonyremote
    import timelapse_controls
    monkeypatch.setattr(engine, "SHOTS_DIR", tmp_path / "shots")
    live = {"iso": "100", "shutter": "1/60", "aperture": "8", "focus_mode": "Automatic"}
    shots = []

    class Camera:
        def __init__(self, **kwargs): pass
        def __enter__(self): return self
        def __exit__(self, *args): pass
        def info(self): return SimpleNamespace(model="Sony ILCE-7M2 (Control)", port="usb:001,002")
        def wake(self): return True
        def shooting_mode(self): return "M"
        def get_settings(self): return dict(live)
        def list_options(self, key):
            return {"iso": ["100", "125", "160", "200", "250", "320", "400"], "aperture": ["8"],
                    "shutter": ["1/60"]}[key]
        def set_setting(self, key, value): live[key] = value
        def set_exposure(self, **kwargs): live.update(kwargs)
        def capture(self, **kwargs):
            shots.append(dict(live))
            path = tmp_path / f"source_{len(shots)}.jpg"
            Image.new("RGB", (64, 48), (grey, grey, grey)).save(path)
            return str(path)

    monkeypatch.setattr(sonyremote, "Camera", Camera)
    monkeypatch.setattr(engine, "_free_usb", lambda: None)
    runner = product_sessions.runner()
    state = runner.create("timelapse", len(modes), timelapse_mode=modes[0],
                          timelapse_options={"max_shutter_s": 1 / 60},
                          metadata={"camera_model": "Sony ILCE-7M2 (Control)", "camera_port": "usb:001,002", "camera_serial": "A7II00SERIAL"})
    if elapsed_s:
        stored = runner.read(state["id"])
        stored["smartlapse_status"] = {"elapsed_s": elapsed_s}
        runner.save(stored)
    original = timelapse_controls.read_controls

    def scheduled(sessions, session_id):
        controls = original(sessions, session_id)
        accepted = sum(frame["status"] == "accepted" for frame in sessions.read(session_id)["frames"])
        mode = modes[min(accepted, len(modes) - 1)]
        options = {"max_shutter_s": 1 / 60}
        controls["exposure"] = timelapse_config(mode, options)
        return controls
    monkeypatch.setattr(timelapse_controls, "read_controls", scheduled)
    return product_sessions.run(state["id"]), shots


def test_a_fixed_frame_mid_run_leaves_no_stale_anchor_or_estimate(tmp_path, monkeypatch):
    result, shots = _switching_session(tmp_path, monkeypatch,
                                       ["holy_grail", "holy_grail", "holy_grail", "fixed", "holy_grail"])
    assert result["status"] == "completed", result
    after_fixed = result["frames"][4]["applied"]["timelapse_meter"]
    assert after_fixed["measured_ev"] is None           # re-anchored like a restart would be
    assert "scene_ev_estimate" not in after_fixed         # no estimate carried across the Fixed frame
    assert shots[3]["iso"] == "100"                       # the Fixed frame used the baseline


def test_the_budget_follows_a_mid_run_switch_out_of_holy_grail(tmp_path, monkeypatch):
    result, shots = _switching_session(tmp_path, monkeypatch, ["holy_grail", "auto_custom"], elapsed_s=3700.0)
    assert result["status"] == "failed" and "1-hour session execution budget" in result["error"]
    assert len(shots) == 1  # the Holy Grail frame ran; the Auto frame past one hour did not
