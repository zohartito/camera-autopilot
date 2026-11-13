"""A fixed HDR/long-exposure/crowd/night stack checks its whole shutter ladder before the first shutter.

Fixed HDR brackets 1 EV around the body's current shutter. Each frame used to be validated only in
its own prepare(), so a ladder whose long end passed the 1 s USB cap (or whose short end passed the
body's fastest shutter) failed part-way, after earlier frames had fired. Fakes only.
"""
from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[1]
for path in (ROOT, ROOT / "backend"):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))

import sonyremote  # noqa: E402
from camera_caps import _A7II_SHUTTER_LABELS  # noqa: E402

A7II = "Sony ILCE-7M2 (Control)"
IDENTITY = {"camera_model": A7II, "camera_port": "usb:001,002", "camera_serial": "A7II00SERIAL"}
LADDER = _A7II_SHUTTER_LABELS + ["Bulb"]  # 30 s .. 1/8000 in thirds, as the driver lists it


@pytest.fixture
def body(tmp_path, monkeypatch):
    """A session-capable fake body; records every setting write and shutter."""
    import engine
    import product_sessions
    from PIL import Image
    monkeypatch.setattr(engine, "SHOTS_DIR", tmp_path / "shots")
    monkeypatch.setattr(engine, "_free_usb", lambda: None)

    def merge(mode, paths, out, **kwargs):  # the merge is not under test
        Path(out).write_bytes(Path(paths[0]).read_bytes())
        return {"out_path": out}
    monkeypatch.setattr(product_sessions, "merge_session_frames", merge)
    rig = SimpleNamespace(live={"iso": "100", "aperture": "8", "shutter": "1/60", "focus_mode": "Automatic"},
                          ladder=list(LADDER), writes=[], captures=[])

    class Camera:
        def __init__(self, **kwargs): pass
        def __enter__(self): return self
        def __exit__(self, *args): pass
        def info(self): return SimpleNamespace(model=A7II, port="usb:001,002")
        def wake(self): return True
        def shooting_mode(self): return "M"
        def get_settings(self): return dict(rig.live)
        def list_options(self, key): return {"iso": ["100", "200"], "aperture": ["8"], "shutter": rig.ladder}[key]
        def set_setting(self, key, value):
            rig.writes.append({key: value})
            rig.live[key] = value
        def set_exposure(self, **kwargs):
            rig.writes.append(kwargs)
            rig.live.update(kwargs)
        def capture(self, **kwargs):
            path = tmp_path / f"source_{len(rig.captures)}.jpg"
            Image.new("RGB", (64, 48), (90, 90, 90)).save(path)
            rig.captures.append(dict(rig.live))
            return str(path)
    monkeypatch.setattr(sonyremote, "Camera", Camera)
    return rig


def run_hdr(body, baseline, frames, *, mode="hdr", ev_step=1.0):
    import product_sessions
    body.live["shutter"] = baseline
    state = product_sessions.runner().create(mode, frames, ev_step=ev_step, metadata=dict(IDENTITY))
    return product_sessions.run(state["id"])


def assert_refused_before_any_shutter(body, result):
    assert result["status"] == "failed"
    assert all(frame["status"] == "pending" for frame in result["frames"])
    assert body.captures == [] and body.writes == []  # no shutter, no setting write
    refusal = result["ladder_refusal"]
    assert refusal["may_have_fired"] is False and refusal["retry_capture"] is False
    return refusal


def test_long_end_beyond_the_usb_cap_refuses_before_the_first_shutter(body):
    # 5 frames at 1 EV around 1/2 s: 1/8 .. 2 s. On main frames 0-2 fired, then frame 3 (1 s ok)
    # and frame 4 (2 s) failed part-way.
    refusal = assert_refused_before_any_shutter(body, run_hdr(body, "1/2", 5))
    assert refusal["reason"] == "exceeds_transport_cap"
    assert refusal["baseline_shutter"] == "1/2" and refusal["frame_count"] == 5
    assert refusal["allowed_baseline_range"]["min"] == "1/2000"
    assert refusal["allowed_baseline_range"]["max"] == "1/4"
    assert "baseline shutter 1/2" in refusal["detail"] and "between 1/2000 and 1/4" in refusal["detail"]
    assert [row["problem"] for row in refusal["frames"]] == [None, None, None, None, "exceeds_transport_cap"]


def test_short_end_beyond_the_fastest_shutter_refuses_before_the_first_shutter(body):
    # 5 frames around 1/4000 s: the fast end wants 1/16000, which this 1/8000 ladder cannot give.
    refusal = assert_refused_before_any_shutter(body, run_hdr(body, "1/4000", 5))
    assert refusal["reason"] == "beyond_body_fastest"
    assert refusal["frames"][0]["problem"] == "beyond_body_fastest"
    assert refusal["allowed_baseline_range"]["min"] == "1/2000"
    assert "faster than this body's fastest shutter" in refusal["detail"]


def test_driver_shutters_faster_than_1_8000_are_outside_the_transport_policy(body):
    # A driver can advertise 1/16000 and 1/32000; the bracket must not snap onto them.
    body.ladder = LADDER[:-1] + ["1/10000", "1/12800", "1/16000", "1/32000", "Bulb"]
    refusal = assert_refused_before_any_shutter(body, run_hdr(body, "1/4000", 5))
    assert refusal["frames"][0]["shutter"] == "1/16000"
    assert refusal["frames"][0]["problem"] == "below_transport_minimum"


@pytest.mark.parametrize("frames", range(2, 10))
def test_the_named_range_is_exactly_what_runs(body, frames, monkeypatch):
    """The allowed range reported for N frames runs in full at both ends, and one ladder step
    past either end is refused before any shutter."""
    import product_sessions
    body.live["shutter"] = "30"  # far outside every range: learn the range from the refusal
    refusal = run_hdr(body, "30", frames)["ladder_refusal"]
    allowed = refusal["allowed_baseline_range"]
    usable = [label for label in _A7II_SHUTTER_LABELS]
    for label in (allowed["min"], allowed["max"]):
        body.captures.clear(); body.writes.clear()
        result = run_hdr(body, label, frames)
        assert "ladder_refusal" not in result, (label, result.get("error"))
        assert len(body.captures) == frames
        assert result["status"] == "completed" and result["restore"] == "completed", result.get("error")
        assert body.live["shutter"] == label  # restored to the baseline
    slower = usable[usable.index(allowed["max"]) - 1]   # the ladder runs slow -> fast
    faster = usable[usable.index(allowed["min"]) + 1] if allowed["min"] != usable[-1] else None
    for label in filter(None, (slower, faster)):
        body.captures.clear(); body.writes.clear()
        assert_refused_before_any_shutter(body, run_hdr(body, label, frames))


def test_an_in_range_bracket_is_unchanged(body):
    result = run_hdr(body, "1/60", 3)
    assert "ladder_refusal" not in result
    assert [shot["shutter"] for shot in body.captures] == ["1/125", "1/60", "1/30"]


@pytest.mark.parametrize("mode", ["long_exposure", "crowd_remove"])
def test_equal_exposure_stacks_refuse_a_baseline_over_the_cap_with_a_structured_reason(body, mode):
    refusal = assert_refused_before_any_shutter(body, run_hdr(body, "2", 3, mode=mode))
    assert refusal["reason"] == "exceeds_transport_cap" and refusal["mode"] == mode
    assert refusal["allowed_baseline_range"]["max"] == "1"


def test_fixed_shoot_policy_reports_the_refusal_and_fired_nothing(body):
    import product_capture
    body.live["shutter"] = "1/2"
    engine = SimpleNamespace(name="real", camera_model=A7II, camera_port="usb:001,002",
                             camera_serial="A7II00SERIAL")
    shot = product_capture._session_capture(engine, {"mode": "hdr", "frames": 5, "reason": "fixed"},
                                            {"mode": "fixed", "stack_mode": "hdr", "frames": 5}, "natural")
    policy = shot.evidence["stack_policy"]
    assert shot.ok is False and body.captures == [] and body.writes == []
    assert policy["may_have_fired"] is False and shot.evidence["physical_exposure_count"] == 0
    assert policy["ladder_refusal"]["baseline_shutter"] == "1/2"
    assert policy["ladder_refusal"]["allowed_baseline_range"]["max"] == "1/4"


HALF_STOP = ["30", "20", "15", "10", "8", "6", "4", "3", "2", "15/10", "1", "7/10", "5/10", "1/3",
             "1/4", "1/6", "1/8", "1/10", "1/15", "1/20", "1/30", "1/45", "1/60", "1/90", "1/125",
             "1/180", "1/250", "1/350", "1/500", "1/750", "1/1000", "1/1500", "1/2000", "1/3000",
             "1/4000", "1/6000", "1/8000", "Bulb"]


def test_interior_rounding_on_a_half_stop_ladder_is_not_a_refusal(body):
    # 3 frames around 1/6 on a half-stop ladder want 1/12, which rounds to 1/10 (0.26 stop): that
    # is ordinary snapping, exactly what prepare() has always done, never a range refusal.
    body.ladder = HALF_STOP
    result = run_hdr(body, "1/6", 3)
    assert "ladder_refusal" not in result and result["status"] == "completed", result.get("error")
    assert [shot["shutter"] for shot in body.captures] == ["1/10", "1/6", "1/3"]


def test_a_body_ladder_that_ends_early_is_named_as_the_body_limit_not_the_usb_cap(body):
    import camera_caps
    body.ladder = [label for label in LADDER if label == "Bulb" or camera_caps._shutter_seconds(label) <= 0.25]
    refusal = assert_refused_before_any_shutter(body, run_hdr(body, "1/4", 3))
    assert refusal["reason"] == "beyond_body_slowest"
    assert "slower than this body's slowest shutter" in refusal["detail"]
    assert refusal["allowed_baseline_range"]["max"] == "1/8"


def test_a_ladder_refusal_is_final_even_if_the_live_ladder_changes(body, monkeypatch):
    import product_sessions
    body.ladder = [label for label in LADDER if label not in ("1/1250", "1/1600", "1/2000", "1/2500",
                                                              "1/3200", "1/4000", "1/5000", "1/6400", "1/8000")]
    body.live["shutter"] = "1/1000"
    state = product_sessions.runner().create("hdr", 3, ev_step=1.0, metadata=dict(IDENTITY))
    refused = product_sessions.run(state["id"])
    assert refused["ladder_refusal"]["reason"] == "beyond_body_fastest" and body.captures == []
    opened = []
    monkeypatch.setattr(sonyremote, "Camera", lambda **kw: opened.append(kw))
    with pytest.raises(product_sessions.LadderRefused, match="start a new capture"):
        product_sessions.run(state["id"])  # refused before any camera connection
    assert opened == [] and body.captures == []


def test_only_pending_frames_are_checked_on_resume():
    import camera_caps
    import product_sessions
    caps = camera_caps.from_live_choices(A7II, ["100"], ["8"], LADDER)
    frames = [{"index": i, "ev_offset": float(i - 1), "status": "pending"} for i in range(3)]
    state = {"mode": "hdr", "frames": frames, "metadata": {}}
    assert product_sessions.fixed_ladder_refusal(state, base_shutter="1", caps=caps)["reason"] == "exceeds_transport_cap"
    frames[2]["status"] = "accepted"  # the over-cap frame already exists; nothing pending exceeds
    assert product_sessions.fixed_ladder_refusal(state, base_shutter="1", caps=caps) is None
    for frame in frames:
        frame["status"] = "accepted"  # a merge-only retry checks nothing
    assert product_sessions.fixed_ladder_refusal(state, base_shutter="30", caps=caps) is None


def test_night_assist_plan_off_the_live_ladder_refuses_before_any_write(body):
    import product_sessions
    plan = {"shutter_label": "1/55", "iso": "100", "aperture_label": "8",
            "camera_serial": IDENTITY["camera_serial"]}  # 1/55 is 8 % from the nearest 1/60
    state = product_sessions.runner().create("night", 3, metadata={**IDENTITY, "night_assist": plan})
    refusal = assert_refused_before_any_shutter(body, product_sessions.run(state["id"]))
    assert refusal["reason"] == "night_plan_not_available"
    assert "new Night Assist plan" in refusal["detail"] and refusal["allowed_baseline_range"] is None



def _check(ladder, base, frames, ev_step=1.0):
    import camera_caps
    import product_sessions
    caps = camera_caps.from_live_choices(A7II, ["100"], ["8"], ladder)
    state = {"mode": "hdr", "metadata": {}, "frames": [
        {"index": i, "ev_offset": (i - (frames - 1) / 2) * ev_step, "status": "pending"} for i in range(frames)]}
    return product_sessions.fixed_ladder_refusal(state, base_shutter=base, caps=caps)


def test_a_bracket_clamped_at_1_8000_is_refused_not_silently_duplicated():
    # On main 3 frames around 1/8000 captured 1/8000, 1/8000, 1/4000: two identical exposures in
    # an "HDR" bracket. The precheck refuses it (a deliberate change: the short end is past the body).
    refusal = _check(LADDER, "1/8000", 3)
    assert refusal["reason"] == "beyond_body_fastest" and refusal["frames"][0]["shutter"] == "1/8000"


def test_duplicate_numeric_labels_do_not_collapse_the_end_step():
    # 0.1 stop past 1/8000 is ordinary rounding onto 1/8000; a zero-width "step" would refuse it.
    assert _check(["1/4000", "1/5000", "1/6400", "1/8000", "2/16000", "Bulb"], "1/8000", 3, ev_step=0.1) is None


def test_single_label_ladder_is_bounded():
    assert _check(["1/60"], "1/60", 3)["reason"] in ("beyond_body_fastest", "beyond_body_slowest")
    assert _check(["1/60"], "1/60", 3, ev_step=0.0) is None


def test_no_fitting_baseline_advises_fewer_frames_or_a_smaller_step():
    refusal = _check(["1/125", "1/60", "1/30"], "1/60", 5)
    assert refusal["allowed_baseline_range"] is None
    assert "fewer frames or a smaller EV step" in refusal["detail"] and "change the shutter" not in refusal["detail"]


def test_transport_refusal_names_the_snapped_shutter_not_a_legal_wanted_one():
    refusal = _check(["1/30", "1/2", "13/10"], "1/2", 3)
    assert refusal["reason"] == "exceeds_transport_cap"
    assert "nearest shutter 13/10 is longer than the 1 s USB capture limit" in refusal["detail"]


# --- a ladder refusal is final for that session: no Resume (review thread on #86) -------------

@pytest.mark.parametrize("entrypoint", ["resume", "run", "jobs"])
def test_every_session_entrypoint_refuses_a_ladder_refused_session(body, monkeypatch, entrypoint):
    import importlib
    from fastapi.testclient import TestClient
    import product_sessions
    app = importlib.import_module("app")
    refused = run_hdr(body, "1/2", 5)
    assert refused["ladder_refusal"]
    opened = []
    monkeypatch.setattr(sonyremote, "Camera", lambda **kw: opened.append(kw))
    monkeypatch.setattr(app, "API_TOKEN", "fixture-credential-1234567890")
    monkeypatch.setattr(app, "engine", SimpleNamespace(name="real"))
    with TestClient(app.app) as client:
        response = client.post(f"/sessions/{refused['id']}/{entrypoint}", json={} if entrypoint == "jobs" else None,
                               headers={"X-Camera-Autopilot-Token": "fixture-credential-1234567890"})
    assert response.status_code == 409, response.text
    assert "start a new capture" in response.json()["detail"]
    assert opened == [] and body.captures == []
    assert product_sessions.runner().read(refused["id"])["status"] == "failed"  # unchanged


def test_a_ladder_refused_shoot_says_start_a_new_capture(body):
    import product_capture
    body.live["shutter"] = "1/2"
    engine = SimpleNamespace(name="real", camera_model=A7II, camera_port="usb:001,002",
                             camera_serial="A7II00SERIAL")
    shot = product_capture._session_capture(engine, {"mode": "hdr", "frames": 5, "reason": "fixed"},
                                            {"mode": "fixed", "stack_mode": "hdr", "frames": 5}, "natural")
    assert "start a new capture" in shot.summary and "continue from its saved session" not in shot.summary
    assert "between 1/2000 and 1/4" in shot.why


def test_the_web_page_does_not_append_do_not_recapture_to_a_ladder_refusal():
    import subprocess
    text = (ROOT / "backend/web/index.html").read_text()
    import re
    source = re.search(r"  function noImageWhy\([^\n]*\) \{.*?\n  \}", text, re.S).group()
    subprocess.run(["node", "-e", "const assert=require('assert');\n" + source + """
      assert.match(noImageWhy({why:'Stack session needs review.',evidence:{}}),/Do not recapture/);
      const refused={why:'Set a shutter between 1/2000 and 1/4.',evidence:{stack_policy:{ladder_refusal:{reason:'exceeds_transport_cap'}}}};
      assert.doesNotMatch(noImageWhy(refused),/Do not recapture/);
      assert.match(noImageWhy(refused),/start a new capture/i);
    """], check=True)


def test_the_web_page_hides_resume_for_a_ladder_refusal():
    import re
    import subprocess
    text = (ROOT / "backend/web/index.html").read_text()
    source = re.search(r"  function sessionResumable\([^\n]*\) \{.*?\n  \}", text, re.S).group()
    subprocess.run(["node", "-e", "const assert=require('assert');\n" + source + """
      assert.equal(sessionResumable({status:'failed',frames:[]}),true);
      assert.equal(sessionResumable({status:'failed',frames:[],ladder_refusal:{reason:'exceeds_transport_cap'}}),false);
      assert.equal(sessionResumable({status:'failed',frames:[{status:'ambiguous'}]}),false);
      assert.equal(sessionResumable({status:'completed',frames:[]}),false);
    """], check=True)


# --- review round 3 (#86): the remedy follows the allowed range ---------------------------------

def test_no_fitting_baseline_says_fewer_frames_everywhere_not_change_the_shutter(body):
    import product_capture
    import product_sessions
    body.ladder = ["1/125", "1/60", "1/30", "Bulb"]
    body.live["shutter"] = "1/60"
    engine = SimpleNamespace(name="real", camera_model=A7II, camera_port="usb:001,002",
                             camera_serial="A7II00SERIAL")
    shot = product_capture._session_capture(engine, {"mode": "hdr", "frames": 5, "reason": "fixed"},
                                            {"mode": "fixed", "stack_mode": "hdr", "frames": 5}, "natural")
    refusal = shot.evidence["stack_policy"]["ladder_refusal"]
    assert refusal["allowed_baseline_range"] is None
    assert "fewer frames or a smaller EV step" in shot.summary and "change the shutter" not in shot.summary.lower()
    assert "fewer frames or a smaller EV step" in refusal["remedy"]
    with pytest.raises(product_sessions.LadderRefused) as caught:
        product_sessions.run(shot.evidence["stack_policy"]["session_id"])
    assert "fewer frames or a smaller EV step" in str(caught.value)


def test_a_fitting_range_names_it_in_the_summary(body):
    import product_capture
    body.live["shutter"] = "1/2"
    engine = SimpleNamespace(name="real", camera_model=A7II, camera_port="usb:001,002",
                             camera_serial="A7II00SERIAL")
    shot = product_capture._session_capture(engine, {"mode": "hdr", "frames": 5, "reason": "fixed"},
                                            {"mode": "fixed", "stack_mode": "hdr", "frames": 5}, "natural")
    assert "change the shutter to within 1/2000–1/4 and start a new capture" in shot.summary.lower()


def test_the_web_note_uses_the_refusal_remedy():
    import re
    import subprocess
    text = (ROOT / "backend/web/index.html").read_text()
    source = re.search(r"  function noImageWhy\([^\n]*\) \{.*?\n  \}", text, re.S).group()
    subprocess.run(["node", "-e", "const assert=require('assert');\n" + source + """
      const none={why:'No baseline fits.',evidence:{stack_policy:{ladder_refusal:{allowed_baseline_range:null,
        remedy:'Use fewer frames or a smaller EV step and start a new capture.'}}}};
      assert.match(noImageWhy(none),/fewer frames or a smaller EV step/);
      assert.doesNotMatch(noImageWhy(none),/[Cc]hange the shutter/);
    """], check=True)
