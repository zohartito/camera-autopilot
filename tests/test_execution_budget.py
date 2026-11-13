"""The session execution budget is per session, cumulative across restarts and resumes.

Camera-free: a fake USB body and a fake monotonic clock that advances only when the
fake shutter fires. A restart is simulated by resetting that clock, the way a new
server process starts a new monotonic epoch.
"""
import json
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[1]
for entry in (ROOT, ROOT / "backend"):
    sys.path.insert(0, str(entry))

from stack_planner import timelapse_config  # noqa: E402

FRAME_S = 1000.0  # every fake exposure costs 1000 s of execution time


class Clock:
    def __init__(self):
        self.now = 0.0

    def __call__(self):
        return self.now


def _harness(tmp_path, monkeypatch, *, cancel_after=None, settings_s=0.0, wake_s=0.0, awake=True,
             connect_s=0.0, baseline_s=0.0, discovery_s=0.0):
    from PIL import Image
    import engine
    import product_sessions
    import sonyremote
    monkeypatch.setattr(engine, "SHOTS_DIR", tmp_path / "shots")
    clock = Clock()
    monkeypatch.setattr(product_sessions, "_clock", clock)
    live = {"iso": "100", "shutter": "1/60", "aperture": "8", "focus_mode": "Automatic"}
    shots = []
    seen_on_disk = []
    seen_in_prepare = []
    seen_in_setup = {}

    class Camera:
        def __init__(self, **kwargs): pass
        def __enter__(self):
            clock.now += connect_s
            return self
        def __exit__(self, *args): pass
        def info(self): return SimpleNamespace(model="Sony ILCE-7M2 (Control)", port="usb:001,002")
        def on_disk(self):
            directory = product_sessions.runner()._directory(harness.session_id)
            return (json.loads((directory / "manifest.json").read_text()).get("execution_budget") or {}).get("used_s")
        def wake(self):
            # Each setup step records what a hard kill at that step would leave for the next run.
            seen_in_setup.setdefault("wake", self.on_disk())
            clock.now += wake_s
            return awake
        def shooting_mode(self): return "M"
        def get_settings(self):
            if "baseline" not in seen_in_setup:
                seen_in_setup["baseline"] = self.on_disk()
                clock.now += baseline_s
            return dict(live)
        def list_options(self, key):
            if key == "iso" and "discovery" not in seen_in_setup:
                seen_in_setup["discovery"] = self.on_disk()
                clock.now += discovery_s
            return {"iso": ["100", "125", "160", "200"], "aperture": ["8"], "shutter": ["1/60"]}[key]
        def set_setting(self, key, value): live[key] = value
        def set_exposure(self, **kwargs):
            directory = product_sessions.runner()._directory(harness.session_id)
            # What a hard kill during preparation's camera commands would leave behind.
            seen_in_prepare.append((json.loads((directory / "manifest.json").read_text())
                                    .get("execution_budget") or {}).get("used_s"))
            clock.now += settings_s
            live.update(kwargs)
        def capture(self, **kwargs):
            directory = product_sessions.runner()._directory(harness.session_id)
            # What a hard kill at this instant would leave for the next process to resume from.
            seen_on_disk.append((json.loads((directory / "manifest.json").read_text())
                                 .get("execution_budget") or {}).get("used_s"))
            shots.append(1)
            clock.now += FRAME_S
            if cancel_after is not None and len(shots) == cancel_after:
                (directory / "cancel").touch()
            path = tmp_path / f"source_{len(shots)}.jpg"
            Image.new("RGB", (64, 48), (90, 90, 90)).save(path)
            return str(path)

    monkeypatch.setattr(sonyremote, "Camera", Camera)
    monkeypatch.setattr(engine, "_free_usb", lambda: None)
    harness = SimpleNamespace(clock=clock, shots=shots, seen_on_disk=seen_on_disk, session_id=None,
                              seen_in_prepare=seen_in_prepare, seen_in_setup=seen_in_setup,
                              runner=product_sessions.runner(), run=product_sessions.run)

    def create(frames, mode="auto_custom", interval_s=0.0):
        state = harness.runner.create("timelapse", frames, timelapse_mode=mode, interval_s=interval_s,
                                      timelapse_options={"max_shutter_s": 1 / 60},
                                      metadata={"camera_model": "Sony ILCE-7M2 (Control)",
                                                "camera_port": "usb:001,002", "camera_serial": "A7II00SERIAL"})
        harness.session_id = state["id"]
        return state["id"]
    harness.create = create
    return harness


def test_a_run_stops_when_the_one_hour_budget_is_spent(tmp_path, monkeypatch):
    h = _harness(tmp_path, monkeypatch)
    result = h.run(h.create(6))
    assert result["status"] == "failed" and "1-hour session execution budget" in result["error"]
    assert len(h.shots) == 4  # prepared at 0, 1000, 2000, 3000 s; refused at 4000 s
    budget = result["execution_budget"]
    assert budget["budget_s"] == 3600 and budget["applies"] == "session"
    assert budget["used_s"] == 4000 and budget["remaining_s"] == 0 and budget["exhausted"] is True


def test_a_resumed_run_keeps_counting_the_time_already_spent(tmp_path, monkeypatch):
    # Reverting to a per-run budget lets the second run shoot all three remaining frames.
    h = _harness(tmp_path, monkeypatch, cancel_after=2)
    session_id = h.create(5)
    first = h.run(session_id)
    assert first["status"] == "cancelled" and len(h.shots) == 2
    assert first["execution_budget"]["used_s"] == 2000
    assert first["execution_budget"]["remaining_s"] == 1600
    h.clock.now = 5.0  # a restarted server: a new monotonic epoch
    h.runner.resume(session_id)
    second = h.run(session_id)
    assert second["status"] == "failed" and "1-hour session execution budget" in second["error"]
    assert len(h.shots) == 4  # frames 2 and 3 ran at 2000 and 3000 s used; frame 4 was refused
    assert [frame["status"] for frame in second["frames"]] == ["accepted"] * 4 + ["pending"]
    assert second["execution_budget"]["used_s"] == 4000


def test_the_budget_used_is_durable_before_each_shutter(tmp_path, monkeypatch):
    # A hard kill at any shutter must not hand the next process a fresh budget. Settings take
    # 10 s after the previous accepted boundary, so only a pre-shutter checkpoint shows them.
    h = _harness(tmp_path, monkeypatch, settings_s=10.0)
    h.run(h.create(3))
    assert h.seen_on_disk == [10, 1020, 2030]


def test_boundary_work_and_long_waits_are_checkpointed(tmp_path, monkeypatch):
    # Fake time for the runner's wait loop too, so the checkpoint cadence is exact, not scheduled.
    import stack_session
    import time
    import product_sessions
    import timelapse_controls
    h = _harness(tmp_path, monkeypatch)
    monkeypatch.setattr(stack_session, "time", SimpleNamespace(
        monotonic=h.clock, time=time.time, sleep=lambda seconds: setattr(h.clock, "now", h.clock.now + seconds)))
    writes = []
    original = stack_session._write_manifest  # every manifest write goes through it

    def recording(directory, value):
        if value.get("execution_budget"):
            writes.append(value["execution_budget"]["used_s"])
        return original(directory, value)
    monkeypatch.setattr(stack_session, "_write_manifest", recording)

    def slow_preview(*args, **kwargs):  # 20 s of boundary work (a movie snapshot) before the wait
        h.clock.now += 20
        raise RuntimeError("no encoder in this test")
    monkeypatch.setattr(product_sessions, "_incremental_video", slow_preview)
    session_id = h.create(3, interval_s=1200)
    timelapse_controls.update_controls(h.runner, session_id, {"preview_every": 2})
    h.run(session_id)
    # Frame 1 starts at 1200 s and its capture ends at 2200 s; the snapshot runs to 2220 s, then the
    # wait lasts until 2400 s. The first record of that wait includes the snapshot, and no stretch of
    # the wait goes longer than one checkpoint period unrecorded.
    wait = [value for value in writes if 2200 < value <= 2400]
    assert wait[0] == pytest.approx(2220)
    assert max(later - earlier for earlier, later in zip(wait, wait[1:])) <= stack_session.PROGRESS_CHECKPOINT_S + 0.2
    assert wait[-1] >= 2400 - stack_session.PROGRESS_CHECKPOINT_S


def test_time_before_preparation_is_durable_before_its_camera_commands(tmp_path, monkeypatch):
    # Frame 1 is prepared at 1100 s, after a wait whose last 30 s checkpoint fell near 1090 s.
    import stack_session
    import time
    h = _harness(tmp_path, monkeypatch)
    monkeypatch.setattr(stack_session, "time", SimpleNamespace(
        monotonic=h.clock, time=time.time, sleep=lambda seconds: setattr(h.clock, "now", h.clock.now + seconds)))
    h.run(h.create(2, interval_s=1100))
    assert h.seen_in_prepare[0] == 0
    assert h.seen_in_prepare[1] == pytest.approx(1100, abs=0.2)


def test_setup_time_is_durable_at_each_setup_boundary(tmp_path, monkeypatch):
    # Connection 50 s, wake 600 s, baseline read 30 s, capability discovery 20 s: at each next step,
    # the manifest a hard kill would leave already holds everything spent before it.
    h = _harness(tmp_path, monkeypatch, connect_s=50.0, wake_s=600.0, baseline_s=30.0, discovery_s=20.0)
    h.run(h.create(2))
    assert h.seen_in_setup == {"wake": 50, "baseline": 650, "discovery": 680}
    assert h.seen_in_prepare[0] == 700


def test_repeated_kills_during_setup_cannot_reset_the_budget(tmp_path, monkeypatch):
    # Each run spends 1000 s waking the body and is then killed in capability discovery. Nothing
    # unwinds after a hard kill, so the exit recorder is disabled while the kill is injected.
    import camera_caps
    import product_sessions

    class Killed(BaseException):
        pass

    def killed(*args, **kwargs):
        raise Killed()
    h = _harness(tmp_path, monkeypatch, wake_s=1000.0)
    session_id = h.create(3)
    with monkeypatch.context() as kill:
        kill.setattr(product_sessions, "_record_execution", killed)
        kill.setattr(camera_caps, "from_live_choices", killed)
        for _ in range(4):
            h.clock.now = 0.0  # every restart is a new monotonic epoch
            with pytest.raises(Killed):
                h.run(session_id)
    assert h.runner.read(session_id)["execution_budget"]["used_s"] == 4000
    h.clock.now = 0.0
    result = h.run(session_id)
    assert result["status"] == "failed" and "1-hour session execution budget reached" in result["error"]
    assert h.shots == []


def test_a_failed_setup_still_counts(tmp_path, monkeypatch):
    from sonyremote import CameraError
    h = _harness(tmp_path, monkeypatch, wake_s=600.0, awake=False)
    session_id = h.create(3)
    with pytest.raises(CameraError, match="Wake the camera"):
        h.run(session_id)
    assert h.runner.read(session_id)["execution_budget"]["used_s"] == 600


def test_a_new_session_gets_a_fresh_budget(tmp_path, monkeypatch):
    h = _harness(tmp_path, monkeypatch)
    spent = h.run(h.create(6))
    assert spent["execution_budget"]["exhausted"] is True
    h.clock.now = 0.0
    fresh = h.run(h.create(3))
    assert fresh["status"] == "completed", fresh
    assert fresh["execution_budget"]["used_s"] == 3000
    assert fresh["execution_budget"]["remaining_s"] == 600


def test_a_holy_grail_session_counts_against_three_hours_across_a_restart(tmp_path, monkeypatch):
    h = _harness(tmp_path, monkeypatch, cancel_after=6)
    session_id = h.create(12, mode="holy_grail")
    first = h.run(session_id)
    assert first["status"] == "cancelled" and first["execution_budget"]["used_s"] == 6000
    assert first["execution_budget"]["applies"] == "holy_grail_timelapse"
    assert first["execution_budget"]["budget_s"] == 3 * 3600
    h.clock.now = 0.0
    h.runner.resume(session_id)
    second = h.run(session_id)
    assert second["status"] == "failed" and "3-hour session execution budget" in second["error"]
    assert len(h.shots) == 11  # 0..10 000 s used; refused at 11 000 s


def test_a_legacy_smartlapse_elapsed_still_counts(tmp_path, monkeypatch):
    h = _harness(tmp_path, monkeypatch)
    session_id = h.create(3)
    stored = h.runner.read(session_id)
    stored["smartlapse_status"] = {"elapsed_s": 3000.0}
    h.runner.save(stored)
    result = h.run(session_id)
    assert len(h.shots) == 1 and result["execution_budget"]["used_s"] == 4000


@pytest.mark.parametrize("record", [{"used_s": -1.0}, {"used_s": float("nan")}, {"used_s": "3000"},
                                    {"used_s": True}, None, {}, {"budget_s": 3600}])
def test_an_unreadable_recorded_budget_refuses_before_any_shutter(tmp_path, monkeypatch, record):
    h = _harness(tmp_path, monkeypatch)
    session_id = h.create(3)
    stored = h.runner.read(session_id)
    stored["execution_budget"] = record
    # Written directly: the runner's own writer refuses NaN, but a manifest on disk may hold it.
    (h.runner._directory(session_id) / "manifest.json").write_text(json.dumps(stored))
    with pytest.raises(ValueError, match="execution time"):
        h.run(session_id)
    assert h.shots == []


def test_the_budget_status_names_the_rule_that_applies():
    from product_sessions import execution_budget_status
    holy = execution_budget_status("timelapse", timelapse_config("holy_grail"), 1800.0)
    assert holy == {"budget_s": 10800.0, "applies": "holy_grail_timelapse", "scope": "per_session",
                    "used_s": 1800.0, "remaining_s": 9000.0, "exhausted": False}
    other = execution_budget_status("night", None, 4000.0)
    assert other["applies"] == "session" and other["remaining_s"] == 0 and other["exhausted"] is True


def test_the_smartlapse_cap_follows_the_execution_budget():
    from timelapse_controls import smartlapse_settings
    holy = smartlapse_settings({"duration_s": 3 * 3600, "output_s": 10, "fps": 24}, timelapse_config("holy_grail"))
    assert holy["execution_budget_s"] == 10800 and holy["budget_applies"] == "holy_grail_timelapse"
    with pytest.raises(ValueError, match="3-hour Holy Grail"):
        smartlapse_settings({"duration_s": 3 * 3600 + 1, "output_s": 10, "fps": 24}, timelapse_config("holy_grail"))
    with pytest.raises(ValueError, match="1-hour session"):
        smartlapse_settings({"duration_s": 3601, "output_s": 10, "fps": 24}, timelapse_config("auto_custom"))
    with pytest.raises(ValueError, match="interval must be <=3600"):  # a session interval stays <= 1 h
        smartlapse_settings({"duration_s": 3 * 3600, "output_s": 2, "fps": 1}, timelapse_config("holy_grail"))
    assert smartlapse_settings({"duration_s": 3 * 3600, "output_s": 4, "fps": 1},
                               timelapse_config("holy_grail"))["interval_s"] == 3600
    plain = smartlapse_settings({"duration_s": 3600, "output_s": 10, "fps": 24})
    assert plain["execution_budget_s"] == 3600 and plain["budget_applies"] == "session"


def test_a_two_hour_smartlapse_plan_is_accepted_only_for_holy_grail(tmp_path):
    from stack_session import SessionRunner
    from timelapse_controls import update_controls
    sessions = SessionRunner(tmp_path)
    plan = {"duration_s": 7200, "output_s": 5, "fps": 24}
    auto = sessions.create("timelapse", 3, timelapse_mode="auto_custom")
    with pytest.raises(ValueError, match="1-hour session"):
        update_controls(sessions, auto["id"], {"smartlapse": plan})
    holy = sessions.create("timelapse", 3, timelapse_mode="holy_grail")
    controls = update_controls(sessions, holy["id"], {"smartlapse": plan})
    assert controls["smartlapse"]["execution_budget_s"] == 10800
    # Switching to Holy Grail in the same patch applies the Holy Grail budget.
    switched = update_controls(sessions, auto["id"], {"exposure": {"mode": "holy_grail"}, "smartlapse": plan})
    assert switched["smartlapse"]["budget_applies"] == "holy_grail_timelapse"


def test_a_plan_outside_the_current_budget_is_refused_before_the_first_shutter(tmp_path, monkeypatch):
    from timelapse_controls import update_controls
    h = _harness(tmp_path, monkeypatch)
    session_id = h.create(3, mode="holy_grail")
    update_controls(h.runner, session_id, {"smartlapse": {"duration_s": 7200, "output_s": 5, "fps": 24}})
    update_controls(h.runner, session_id, {"exposure": {"mode": "auto_custom"}})
    result = h.run(session_id)
    assert result["status"] == "failed" and "exceeds the 1-hour session" in result["error"], result
    assert h.shots == []


def test_a_switch_between_the_run_start_and_the_first_frame_is_still_refused(tmp_path, monkeypatch):
    import timelapse_controls
    h = _harness(tmp_path, monkeypatch)
    session_id = h.create(3, mode="holy_grail")
    timelapse_controls.update_controls(h.runner, session_id,
                                       {"smartlapse": {"duration_s": 7200, "output_s": 5, "fps": 24}})
    original, calls = timelapse_controls.read_controls, []

    def switched_after_the_first_read(sessions, sid):
        controls = original(sessions, sid)
        calls.append(1)
        if len(calls) > 1:  # the run's own start read sees Holy Grail; the first frame sees Auto
            controls["exposure"] = timelapse_config("auto_custom", {"max_shutter_s": 1 / 60})
        return controls
    monkeypatch.setattr(timelapse_controls, "read_controls", switched_after_the_first_read)
    result = h.run(session_id)
    assert result["status"] == "failed" and "exceeds the 1-hour session" in result["error"], result
    assert h.shots == []


def test_an_early_return_reports_the_recorded_time(tmp_path, monkeypatch):
    # An unresolved shutter blocks capture after setup; the returned state carries that setup time.
    h = _harness(tmp_path, monkeypatch, wake_s=18.0)
    session_id = h.create(3)
    stored = h.runner.read(session_id)
    stored["frames"][0]["status"] = "ambiguous"
    stored["restore_baseline"] = {"iso": "100", "shutter": "1/60", "aperture": "8", "focus_mode": "Automatic"}
    stored["restore"] = "completed"
    h.runner.save(stored)
    result = h.run(session_id)
    assert h.shots == [] and result["execution_budget"]["used_s"] == 18
