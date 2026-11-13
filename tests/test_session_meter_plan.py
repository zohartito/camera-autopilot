"""Session meter-first: POST /sessions/meter-plan, then POST /sessions `start_settings`.

Field defect (docs/RECEIPTS_2026-10-01.md R-1001-7): a Holy Grail started at the body's inherited
ISO 640 · 1/125, about 5.1 EV under its target, and spent 13 frames at the 0.3 EV/frame cap catching
up. The fix keeps metering OUT of the session runner: POST /sessions/meter-plan takes one meter frame
(plus the bounded re-meter) through the very /shoot Fixed-stack metering path (#72,
`RealEngine.meter_baseline`, which puts the inherited exposure back), saves no session and recommends
a start; POST /sessions `start_settings` (validated before the session exists) becomes frame 0's
exposure, and the session restores at the end as every session does. Fake cameras only.
"""
from __future__ import annotations

import importlib
import json
from pathlib import Path
import sys

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tests"))
sys.path.insert(0, str(ROOT / "backend"))

import engine  # noqa: E402
import product_sessions  # noqa: E402
import remeter  # noqa: E402
import sonyremote  # noqa: E402
from test_final_wave_engine import rig  # noqa: E402,F401  (fake-camera engine fixture)
from test_final_wave_product_api import product  # noqa: E402,F401  (camera-free app fixture)
from test_fixed_stack_meter_first import MODEL, NIGHT, PORT, SERIAL, _recommend, stack  # noqa: E402,F401

METERED = {"iso": "100", "aperture": "8", "shutter": "1/500"}   # the stack fixture's daylight meter
CAMERA = {"camera_model": MODEL, "camera_port": PORT, "camera_serial": SERIAL}
WEB = ROOT / "backend" / "web"


@pytest.fixture
def api(product, stack, monkeypatch):  # noqa: F811
    """The app on the real-engine code path, with the stack fixture's fake body behind sonyremote."""
    product.fake.name = "real"
    monkeypatch.setattr(importlib.import_module("app"), "_host_camera_preflight", lambda: None)
    return product


def _plan(api, mode="hdr", frames=3, ev_step=2.0, session=None, **body):
    """POST /sessions/meter-plan; the session body is required (by default the plain session of this shape)."""
    session = session if session is not None else {**CAMERA, "mode": mode, "frames": frames, "ev_step": ev_step}
    return api.client.post("/sessions/meter-plan", headers=api.headers, json={**CAMERA, "session": session, **body})


def _create(api, **body):
    return api.client.post("/sessions", headers=api.headers, json={**CAMERA, **body})


def _held(camera):
    return {key: camera.settings[key] for key in NIGHT}


def _exposures(state):
    return [{key: frame["applied"][key] for key in METERED} for frame in state["frames"]
            if frame["status"] == "accepted"]


# --- POST /sessions/meter-plan ----------------------------------------------------------------------

def test_meter_plan_meters_once_restores_and_recommends_a_start(api, stack):
    response = _plan(api, mode="timelapse", frames=10)
    assert response.status_code == 200, response.text
    plan = response.json()
    assert stack.camera.capture_count == 1
    assert plan["recommended_start"] == METERED and plan["metered"] == METERED
    assert plan["inherited"] == NIGHT and len(plan["meter_passes"]) == 1
    assert (plan["camera_model"], plan["camera_port"], plan["camera_serial"]) == (MODEL, PORT, SERIAL)
    assert plan["exposure_budget"] == {"meter_exposures": 1, "extra_meter_exposures": remeter.MAX_REMETER_PASSES,
                                       "session_frames": 10, "max_physical_exposures": 13}
    assert plan["session_created"] is False and product_sessions.runner().list() == []
    assert _held(stack.camera) == NIGHT                     # the meter put the inherited exposure back


def test_meter_plan_reserves_hdr_headroom_like_a_fixed_stack(api, stack):
    _recommend(stack, "400", "1", 1.0)                      # +1 EV would exceed the 1 s USB cap
    plan = _plan(api, mode="hdr", frames=3, ev_step=1.0).json()
    assert plan["recommended_start"] == {"iso": "800", "aperture": "8", "shutter": "1/2"}
    assert plan["bracket_headroom"]["adjusted"] == plan["recommended_start"]


def test_an_uncertain_meter_is_a_409_with_its_receipt_and_nothing_else_fires(api, stack):
    stack.camera.missing_capture = 1                        # the shutter fired, no JPEG came back
    response = _plan(api, mode="timelapse", frames=10)
    assert response.status_code == 409, response.text
    body = response.json()
    assert body["may_have_fired"] is True and body["retry_capture"] is False
    receipt = json.loads((api.app.SHOTS_DIR / body["uncertain_receipt"]).read_text())  # store-relative
    assert receipt["route"] == "/sessions/meter-plan"
    assert stack.camera.capture_count == 1 and product_sessions.runner().list() == []


def test_a_meter_black_or_white_after_the_re_meter_recommends_nothing(api, stack):
    stack.camera.frame_values = [255]
    response = _plan(api, mode="hdr", frames=3)
    assert response.status_code == 409
    body = response.json()
    assert body["error"] == "meter_unreliable" and body["recommended_start"] is None
    assert stack.camera.capture_count == 1 + remeter.MAX_REMETER_PASSES
    assert _held(stack.camera) == NIGHT and product_sessions.runner().list() == []


def test_meter_plan_refuses_a_missing_serial_and_a_swapped_body_before_any_write(api, stack, monkeypatch):
    missing = api.client.post("/sessions/meter-plan", headers=api.headers, json={
        "camera_model": MODEL, "camera_port": PORT,
        "session": {"camera_model": MODEL, "camera_port": PORT, "mode": "hdr", "frames": 3}})
    assert missing.status_code == 400 and missing.json()["reason"] == "serial_required"

    def swapped(**kwargs):
        raise sonyremote.CameraIdentityError("fixture swapped body", reason="serial_mismatch")
    monkeypatch.setattr(sonyremote, "Camera", swapped)
    refused = _plan(api, mode="hdr", frames=3)
    assert refused.status_code == 409 and refused.json()["reason"] == "serial_mismatch"
    assert stack.camera.capture_count == 0
    assert stack.camera.exposure_writes == [] and stack.camera.setting_writes == []


# --- POST /sessions start_settings --------------------------------------------------------------------

def test_holy_grail_frame_0_fires_exactly_at_the_metered_start(api, stack):
    plan = _plan(api, mode="timelapse", frames=3).json()
    created = _create(api, mode="timelapse", frames=3, timelapse_mode="holy_grail",
                      start_settings=plan["recommended_start"], meter_plan=plan)
    assert created.status_code == 200, created.text
    state = created.json()
    assert state["settings"] == METERED
    baseline = state["metadata"]["baseline"]
    assert baseline["source"] == "meter_plan" and baseline["start_settings"] == METERED
    assert baseline["inherited"] == NIGHT
    stack.a7iii.reset_mock()
    result = product_sessions.run(state["id"])
    assert result["status"] == "completed", result.get("error")
    stack.a7iii.assert_not_called()                          # no metering inside the session runner
    assert stack.camera.capture_count == 1 + 3
    assert _exposures(result)[0] == METERED
    meter = product_sessions.runner().frame_evidence(result, 0)["applied"]["timelapse_meter"]
    assert meter["requested_ev"] == 0
    assert {key: result["restore_baseline"][key] for key in NIGHT} == NIGHT
    assert result["restore"] == "completed" and _held(stack.camera) == NIGHT


def test_hdr_brackets_around_the_start_settings(api, stack):
    plan = _plan(api, mode="hdr", frames=3, ev_step=1.0).json()
    state = _create(api, mode="hdr", frames=3, ev_step=1.0, start_settings=plan["recommended_start"],
                    meter_plan=plan).json()
    result = product_sessions.run(state["id"])
    assert result["status"] == "completed", result.get("error")
    assert [frame["shutter"] for frame in _exposures(result)] == ["1/1000", "1/500", "1/250"]
    assert {frame["iso"] for frame in _exposures(result)} == {"100"}
    assert stack.camera.capture_count == 1 + 3 and _held(stack.camera) == NIGHT


def test_an_unlocked_aperture_holy_grail_starts_at_the_start_aperture(api, stack):
    # The body holds f/4; the start is f/8. Frame 0's lens bound must come from the start aperture.
    stack.camera.settings["aperture"] = "4"
    stack.camera.lens_model = "Tamron 28-75 f/2.8"
    state = _create(api, mode="timelapse", frames=3, timelapse_mode="holy_grail",
                    timelapse_options={"aperture_locked": False}, start_settings=METERED).json()
    result = product_sessions.run(state["id"])
    assert result["status"] == "completed", result.get("error")
    assert _exposures(result)[0] == METERED and stack.camera.settings["aperture"] == "4"


@pytest.mark.parametrize("body,why", [
    ({"mode": "hdr", "frames": 3, "start_settings": {"iso": "100", "aperture": "8", "shutter": "2"}}, "shutter"),
    ({"mode": "hdr", "frames": 3, "ev_step": 2.0, "start_settings": {"iso": "100", "aperture": "8", "shutter": "1/2"}},
     "bracket frame"),
    ({"mode": "hdr", "frames": 3, "start_settings": {"iso": "Auto ISO", "aperture": "8", "shutter": "1/60"}}, "ISO"),
    ({"mode": "hdr", "frames": 3, "start_settings": {"iso": "100", "shutter": "1/60"}}, "exactly"),
    ({"mode": "timelapse", "frames": 3, "timelapse_mode": "holy_grail",
      "timelapse_options": {"min_shutter_s": 1 / 125}, "start_settings": METERED}, "timelapse"),
    ({"mode": "hdr", "frames": 3, "meter_plan": {"recommended_start": METERED}}, "meter_plan"),
])
def test_out_of_range_start_settings_are_refused_before_the_session_exists(api, stack, body, why):
    response = _create(api, **body)
    assert response.status_code == 400 and why in response.text, response.text
    assert product_sessions.runner().list() == []
    assert stack.camera.capture_count == 0 and stack.camera.exposure_writes == []


def test_a_plan_for_another_camera_or_outside_the_rig_iso_range_is_refused(api, stack):
    plan = _plan(api, mode="hdr", frames=3).json()
    other = _create(api, mode="hdr", frames=3, start_settings=METERED,
                    meter_plan={**plan, "camera_serial": "A7II00SERIAL"})
    assert other.status_code == 400 and "another camera" in other.text
    edited = _create(api, mode="hdr", frames=3, start_settings={**METERED, "iso": "200"}, meter_plan=plan)
    assert edited.status_code == 400 and "recommended_start" in edited.text
    narrow = {**plan, "usable_iso_range": [200, 6400]}
    iso = _create(api, mode="hdr", frames=3, start_settings=METERED, meter_plan=narrow)
    assert iso.status_code == 400 and "usable ISO range" in iso.text
    night = _create(api, mode="night", frames=4, start_settings=METERED, night_assist={
        "iso": "1600", "aperture_label": "8", "shutter_label": "1", "frames": 4, "evidence": {}, "camera_serial": SERIAL})
    assert night.status_code == 400 and "Night Assist" in night.text
    assert product_sessions.runner().list() == []


def test_resume_never_meters_and_keeps_the_start_settings(api, stack):
    state = _create(api, mode="timelapse", frames=3, timelapse_mode="holy_grail", start_settings=METERED).json()
    sessions = product_sessions.runner()
    original_capture = stack.camera.capture

    def cancel_after_frame_0(download=True):
        path = original_capture(download)
        (sessions._directory(state["id"]) / "cancel").touch()
        return path
    stack.camera.capture = cancel_after_frame_0
    first = product_sessions.run(state["id"])
    assert first["status"] == "cancelled" and len(_exposures(first)) == 1
    stack.camera.capture = original_capture
    stack.camera.settings.update({"iso": "800", "shutter": "1/60"})   # someone touched the body
    sessions.resume(state["id"])
    resumed = product_sessions.run(state["id"])
    assert resumed["status"] == "completed", resumed.get("error")
    stack.a7iii.assert_not_called()                                  # nothing ever metered
    assert stack.camera.capture_count == 3 and resumed["settings"] == METERED
    assert _exposures(resumed)[0] == METERED


def test_without_start_settings_a_session_is_exactly_mains(api, stack):
    state = _create(api, mode="hdr", frames=3, ev_step=1.0).json()
    assert state["settings"] == {} and "baseline" not in state["metadata"]
    result = product_sessions.run(state["id"])
    assert result["status"] == "completed", result.get("error")
    stack.a7iii.assert_not_called()
    assert stack.camera.capture_count == 3
    assert [frame["shutter"] for frame in _exposures(result)] == ["1/30", "1/15", "1/8"]
    assert {frame["iso"] for frame in _exposures(result)} == {"3200"}


def test_a_duration_session_confirms_its_start_settings(api, stack):
    # Duration averaging checks the baseline it was planned from: the start settings, not the body's.
    state = _create(api, mode="long_exposure", frames=2, duration_s=8.5, start_settings=METERED).json()
    (product_sessions.runner()._directory(state["id"]) / "cancel").touch()   # stop before frame 0
    result = product_sessions.run(state["id"])
    assert result["status"] == "cancelled", result.get("error")
    assert result["settings"] == METERED and result["long_exposure_plan"]["frames"] >= 2
    assert stack.camera.capture_count == 0
    assert _held(stack.camera) == NIGHT


def test_a_serial_refusal_on_run_writes_and_fires_nothing(api, stack, monkeypatch):
    state = _create(api, mode="hdr", frames=3, start_settings=METERED).json()

    def swapped(**kwargs):
        raise sonyremote.CameraIdentityError("fixture swapped body", reason="serial_mismatch")
    monkeypatch.setattr(sonyremote, "Camera", swapped)
    with pytest.raises(sonyremote.CameraIdentityError):
        product_sessions.run(state["id"])
    assert product_sessions.runner().read(state["id"])["status"] == "identity_refused"
    assert stack.camera.capture_count == 0 and stack.camera.exposure_writes == []


# --- the Sessions panel ---------------------------------------------------------------------------

def _workflows(code: str) -> str:
    import shutil
    import subprocess
    node = shutil.which("node")
    if node is None:
        pytest.skip("node is not on PATH")
    script = "const window={};\n" + (WEB / "workflows.js").read_text() + "\n" + code
    return subprocess.run([node, "-e", script], check=True, capture_output=True, text=True).stdout


def test_the_panel_counts_the_meter_photo_and_offers_keep_current() -> None:
    html = (WEB / "index.html").read_text()
    block = html[html.index('id="sessions-panel"'):html.index('id="session-start"')]
    assert 'id="session-baseline-source"' in block and 'id="session-budget"' in block
    assert block.index('<option value="metered">') < block.index("<option value=\"current\">Keep the camera's current exposure")
    out = _workflows("const w=window.AutopilotWorkflows; process.stdout.write(JSON.stringify(["
                     "w.sessionBudgetText(10,'metered',false), w.sessionBudgetText(10,'current',false),"
                     "w.sessionBudgetText(4,'metered',true), w.sessionBaselineText(null,'metered',false),"
                     "w.sessionStartText({source:'meter_plan',start_settings:{iso:'100',aperture:'8',shutter:'1/500'},"
                     "inherited:{iso:'640',aperture:'4',shutter:'1/125'}})]));")
    metered, current, night, lead, started = json.loads(out)
    assert metered.startswith("Session: up to 13 physical photos") and "1 meter photo" in metered
    assert current.startswith("Session: 10 physical photos") and "No meter photo" in current
    assert night.startswith("Session: 4 physical photos at the Night Assist exposure")
    assert lead.startswith("Start meters first")
    assert started == ("Started from the metered exposure ISO 100 · 8 · 1/500 "
                       "(the body held ISO 640 · 4 · 1/125 before metering).")


def _start(values: dict, *, plan: dict | None, night_plan: dict | None = None):
    """Click the page's own Start handler in node; meter-plan answers `plan`."""
    import test_web_session_options as page
    import re
    import shutil
    import subprocess
    node = shutil.which("node")
    if node is None:
        pytest.skip("node is not on PATH")
    html = (WEB / "index.html").read_text()
    handler = re.search(r"  \$\('session-start'\)\.onclick=async\(\)=>\{.*?\n  \};\n", html, re.S).group(0)
    form = {**page.FORM, "session-interval": "0", "session-baseline-source": "metered", **values}
    camera = {"model": MODEL, "port": PORT, "serial": SERIAL}
    script = ("const sent=[]; let busy=false, sessionRunning=false, cameraSelectionRequired=false;\n"
              "const NO_SERIAL_MESSAGE='fixture: no serial';\n"
              f"let nightAssistRequest={json.dumps(night_plan)};\n"
              f"const availableCameras=[{json.dumps(camera)}];\n"
              f"const values={json.dumps(form)};\n"
              "const elements={};\n"
              "const $=id=>elements[id] || (elements[id]={value:values[id],checked:false,textContent:''});\n"
              "const timelapseControls=()=>null, longExposureDuration=()=>undefined, deflickerOption=()=>({});\n"
              "const timelapseSessionFields=mode=>mode==='timelapse'?{timelapse_mode:'holy_grail'}:{};\n"
              "const sessionBusy=()=>{}, showSession=()=>{}, showToast=m=>sent.push({toast:m}), noteSessionCameraWarning=()=>{};\n"
              f"const meterPlan={json.dumps(plan)};\n"
              "const sessionRequest=async(path,body)=>{sent.push({path,body});"
              "if(path==='/sessions/meter-plan'){if(!meterPlan)throw new Error('fixture: uncertain meter');return meterPlan;}"
              "return {id:'fixture'};};\n"
              "const runSession=async id=>sent.push({run:id});\n"
              f"{handler}\n"
              "elements['session-start'].onclick().then(()=>process.stdout.write(JSON.stringify("
              "{sent,status:elements['session-status']?.textContent||''})));")
    result = subprocess.run([node, "-e", script], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr[-1500:]
    return json.loads(result.stdout)


def test_start_meters_first_then_submits_the_recommended_start():
    plan = {"recommended_start": METERED, "camera_serial": SERIAL, "detail": None}
    out = _start({"session-mode": "hdr", "session-frames": "3"}, plan=plan)
    paths = [item.get("path") for item in out["sent"] if "path" in item]
    assert paths == ["/sessions/meter-plan", "/sessions"]
    meter_body = out["sent"][0]["body"]
    assert meter_body["camera_serial"] == SERIAL and meter_body["session"]["mode"] == "hdr"
    assert meter_body["session"]["frames"] == 3
    create = out["sent"][1]["body"]
    assert create["start_settings"] == METERED and create["meter_plan"] == plan


def test_keep_current_and_night_assist_send_no_start_settings():
    kept = _start({"session-mode": "hdr", "session-frames": "3", "session-baseline-source": "current"}, plan=None)
    assert [item.get("path") for item in kept["sent"] if "path" in item] == ["/sessions"]
    assert "start_settings" not in kept["sent"][0]["body"]
    import test_web_session_options as page
    night = _start({"session-mode": "night", "session-frames": "4"}, plan=None, night_plan=page.NIGHT_PLAN)
    assert [item.get("path") for item in night["sent"] if "path" in item] == ["/sessions"]
    assert "start_settings" not in night["sent"][0]["body"]


def test_a_failed_meter_creates_no_session():
    out = _start({"session-mode": "hdr", "session-frames": "3"}, plan=None)
    assert [item.get("path") for item in out["sent"] if "path" in item] == ["/sessions/meter-plan"]
    assert "no capture job was submitted" in out["status"]


def test_a_meter_whose_restore_failed_recommends_nothing(api, stack, monkeypatch):
    # The body may still hold the meter's exposure; a session would read it as the exposure to restore.
    monkeypatch.setattr(engine, "_restore_exposure", lambda cam, target: {"status": "failed", "reason": "fixture"})
    response = _plan(api, mode="hdr", frames=3)
    assert response.status_code == 409
    body = response.json()
    assert body["error"] == "meter_restore_failed" and body["recommended_start"] is None
    assert "check the camera" in body["detail"] and product_sessions.runner().list() == []


# --- codex review of b73ee3ea --------------------------------------------------------------------------

def test_a_start_shutter_off_the_live_ladder_is_refused_before_any_write(api, stack):
    # 1/700 would have been rounded to 1/500 by every frame while the session said 1/700.
    state = _create(api, mode="night", frames=2, start_settings={"iso": "100", "aperture": "8", "shutter": "1/700"})
    assert state.status_code == 200, state.text
    result = product_sessions.run(state.json()["id"])
    assert result["status"] == "failed" and "live ladder" in result["error"]
    assert result["start_settings_refusal"]["may_have_fired"] is False
    assert stack.camera.capture_count == 0
    assert stack.camera.exposure_writes == [] and stack.camera.setting_writes == []


def test_bare_start_values_run_as_the_body_s_own_labels(api, stack, monkeypatch):
    # The Sony driver reads an aperture back as "f/8": a bare "8" must become that label before frame 0.
    camera = stack.camera
    options = type(camera).list_options

    def labelled(self, setting):
        values = options(self, setting)
        return [f"f/{value}" for value in values] if setting == "aperture" else values

    def normalising(**settings):
        if "aperture" in settings and not str(settings["aperture"]).startswith("f/"):
            settings["aperture"] = f"f/{settings['aperture']}"
        camera.exposure_writes.append(settings)
        camera.settings.update(settings)
    monkeypatch.setattr(type(camera), "list_options", labelled)
    monkeypatch.setattr(camera, "set_exposure", normalising)
    camera.settings["aperture"] = "f/8"                     # the body reads its aperture back labelled
    state = _create(api, mode="hdr", frames=3, ev_step=1.0, start_settings=METERED).json()
    result = product_sessions.run(state["id"])
    assert result["status"] == "completed", result.get("error")
    assert result["settings"]["aperture"] == "f/8"
    assert {frame["aperture"] for frame in _exposures(result)} == {"f/8"}


@pytest.mark.parametrize("usable", [[None, 6400], ["low", 6400], [6400, 100], [100], {"200": 0, "6400": 0},
                                    {"100": 0, "6400": 0}, "100-6400", [True, 6400], [100, 6400, 12800],
                                    [100, 10 ** 400]])
def test_a_malformed_usable_iso_range_is_a_400(api, stack, usable):
    plan = _plan(api, mode="hdr", frames=3).json()
    response = _create(api, mode="hdr", frames=3, start_settings=METERED, meter_plan={**plan, "usable_iso_range": usable})
    assert response.status_code == 400 and "usable_iso_range" in response.text, response.text
    assert product_sessions.runner().list() == []


# --- PR threads on b6e56b5a (chatgpt-codex-connector) -----------------------------------------------

def test_a_delayed_start_never_meters_now():
    # P1: the meter photo describes the light when Start is pressed, not hours later when the job runs.
    out = _start({"session-mode": "timelapse", "session-frames": "10", "session-delay": "3600"},
                 plan={"recommended_start": METERED})
    assert [item.get("path") for item in out["sent"] if "path" in item] == []
    assert "Start delay" in out["status"] and "No photo was taken" in out["status"]
    kept = _start({"session-mode": "timelapse", "session-frames": "10", "session-delay": "3600",
                   "session-baseline-source": "current"}, plan=None)
    assert [item.get("path") for item in kept["sent"] if "path" in item] == ["/sessions"]
    assert "start_settings" not in kept["sent"][0]["body"]


def test_the_budget_sentence_says_a_delayed_session_cannot_meter_first():
    out = _workflows("const w=window.AutopilotWorkflows; process.stdout.write(JSON.stringify(["
                     "w.sessionBudgetText(10,'metered',false,3600), w.sessionBudgetText(10,'metered',false,0),"
                     "w.sessionBudgetText(10,'current',false,3600)]));")
    delayed, now, kept = json.loads(out)
    assert "Meter first needs Start delay 0" in delayed
    assert "Start delay" not in now and "Start delay" not in kept


def test_an_invalid_form_fires_no_meter_photo():
    # P2: the crowd auto-stop needs 6-8 frames; with 3 the form is refused before the meter request.
    out = _start({"session-mode": "crowd_remove", "session-frames": "3", "crowd-stop-at": "0.9"},
                 plan={"recommended_start": METERED})
    assert [item.get("path") for item in out["sent"] if "path" in item] == []
    assert "6 to 8 frames" in out["status"]


def test_the_meter_request_carries_the_whole_session_for_the_server_to_check():
    out = _start({"session-mode": "hdr", "session-frames": "3"}, plan={"recommended_start": METERED})
    meter, create = out["sent"][0]["body"], out["sent"][1]["body"]
    assert meter["session"] == {key: value for key, value in create.items()
                                if key not in ("start_settings", "meter_plan")}


@pytest.mark.parametrize("session,why", [
    ({"mode": "crowd_remove", "frames": 3, "stop_when_cleared": 0.9}, "6"),
    ({"mode": "timelapse", "frames": 10, "interval_s": 3600}, "hour"),
    ({"mode": "panorama", "frames": 3, "panorama": {"projection": "fisheye-ish"}}, "projection"),
    ({"mode": "hdr", "frames": 3, "timelapse_mode": "holy_grail"}, "timelapse"),
])
def test_meter_plan_refuses_an_invalid_session_before_its_meter_photo(api, stack, session, why):
    body = {**CAMERA, **session}
    response = _plan(api, mode=session["mode"], frames=session["frames"], ev_step=2.0, session=body)
    assert response.status_code == 400 and why in response.text, response.text
    assert stack.camera.capture_count == 0 and stack.camera.exposure_writes == []
    assert product_sessions.runner().list() == []


def test_meter_plan_refuses_a_session_for_another_camera_or_shape(api, stack):
    other = _plan(api, mode="hdr", frames=3, session={**CAMERA, "camera_serial": "A7II00SERIAL", "mode": "hdr", "frames": 3})
    assert other.status_code == 400 and "another camera" in other.text
    assert stack.camera.capture_count == 0


def test_a_valid_session_is_checked_but_not_saved_by_the_meter_plan(api, stack):
    response = _plan(api, mode="hdr", frames=3, ev_step=2.0, session={**CAMERA, "mode": "hdr", "frames": 3})
    assert response.status_code == 200, response.text
    assert stack.camera.capture_count == 1 and product_sessions.runner().list() == []


# --- codex review round 4 (full PR at 461b22e6) ----------------------------------------------------

HOLY_GRAIL = {**CAMERA, "mode": "timelapse", "frames": 10, "interval_s": 2, "timelapse_mode": "holy_grail"}


def test_invalid_timelapse_controls_cost_no_meter_photo(api, stack):
    # The panel applies its exposure controls after creating the session; meter-plan checks them first.
    controls = {"exposure": {"mode": "holy_grail", "aperture_locked": False, "min_aperture": 8, "max_aperture": 4}}
    response = _plan(api, mode="timelapse", frames=10, session=HOLY_GRAIL, controls=controls)
    assert response.status_code == 400 and "aperture" in response.text, response.text
    assert stack.camera.capture_count == 0 and product_sessions.runner().list() == []


def test_a_recommendation_outside_the_session_bounds_is_refused_not_returned(api, stack):
    # Holy Grail bounds from the session body and from the controls: a plan /sessions would refuse is
    # never handed back as a recommendation.
    _recommend(stack, "100", "1/2000", 1 / 2000)
    body = {**HOLY_GRAIL, "timelapse_options": {"min_shutter_s": 0.001}}
    response = _plan(api, mode="timelapse", frames=10, session=body)
    assert response.status_code == 409, response.text
    plan = response.json()
    assert plan["error"] == "recommendation_outside_session" and plan["recommended_start"] is None
    assert "shutter bounds" in plan["detail"] and stack.camera.capture_count == 1
    _recommend(stack, "100", "1/500", 1 / 500)
    controlled = _plan(api, mode="timelapse", frames=10, session=HOLY_GRAIL,
                       controls={"exposure": {"mode": "holy_grail", "min_shutter_s": 0.01}})
    assert controlled.status_code == 409 and controlled.json()["error"] == "recommendation_outside_session"
    assert product_sessions.runner().list() == []


def test_valid_controls_pass_and_the_page_sends_them_with_the_meter_request(api, stack):
    controls = {"exposure": {"mode": "holy_grail", "aperture_locked": True}, "preview_every": 2}
    response = _plan(api, mode="timelapse", frames=10, session=HOLY_GRAIL, controls=controls)
    assert response.status_code == 200, response.text
    assert response.json()["recommended_start"] == METERED and product_sessions.runner().list() == []
    html = (WEB / "index.html").read_text()
    assert "session:body,...(controls?{controls}:{})});" in html


# --- codex review round 5 (full PR at b897cf21) ----------------------------------------------------

def test_a_recommendation_is_checked_against_the_session_and_its_controls(api, stack):
    # Controls that widen the bounds do not let through a start POST /sessions would refuse.
    body = {**HOLY_GRAIL, "timelapse_options": {"min_shutter_s": 0.01}}
    response = _plan(api, mode="timelapse", frames=10, session=body,
                     controls={"exposure": {"mode": "holy_grail", "min_shutter_s": 1 / 8000}})
    assert response.status_code == 409 and response.json()["error"] == "recommendation_outside_session"
    assert product_sessions.runner().list() == []


def test_an_impossible_duration_window_costs_no_meter_photo(api, stack):
    session = {**CAMERA, "mode": "long_exposure", "frames": 2, "duration_s": 1}
    response = _plan(api, mode="long_exposure", frames=2, session=session)
    assert response.status_code == 400 and "too short" in response.text, response.text
    assert stack.camera.capture_count == 0 and product_sessions.runner().list() == []


def test_a_start_shutter_too_long_for_its_duration_window_is_refused(api, stack):
    # 8.5 s fits two 1/500 frames but not two 1 s frames (plus the 8 s transfer allowance).
    _recommend(stack, "400", "1", 1.0)
    session = {**CAMERA, "mode": "long_exposure", "frames": 2, "duration_s": 8.5}
    planned = _plan(api, mode="long_exposure", frames=2, session=session)
    assert planned.status_code == 409 and planned.json()["error"] == "recommendation_outside_session"
    created = _create(api, mode="long_exposure", frames=2, duration_s=8.5,
                      start_settings={"iso": "400", "aperture": "8", "shutter": "1"})
    assert created.status_code == 400 and "too short" in created.text
    assert product_sessions.runner().list() == []


# --- coordinator review of b9ce7c01 ------------------------------------------------------------------

def test_meter_plan_requires_the_session_and_refuses_an_invalid_shape_before_any_shutter(api, stack):
    # P1: a standalone shape skipped validation and fired for HDR with 1 frame, which POST /sessions refuses.
    bare = api.client.post("/sessions/meter-plan", headers=api.headers, json={**CAMERA, "mode": "hdr", "frames": 1})
    assert bare.status_code == 422                                   # the session body is required
    one = _plan(api, mode="hdr", frames=1)
    assert one.status_code == 400 and "frames" in one.text, one.text
    assert stack.camera.capture_count == 0 and product_sessions.runner().list() == []


def test_a_failed_restore_after_metering_is_reported_on_every_exit(api, stack, monkeypatch):
    # P1: Auto ISO + 2 s; metering writes ISO 100, the shutter ladder lookup fails and restoring Auto ISO
    # fails too. The answer must say the exposure was not put back, and recommend nothing.
    stack.camera.settings.update(iso="Auto ISO", shutter="2")
    original_exposure, original_options = stack.camera.set_exposure, stack.camera.list_options

    def exposure(**settings):
        if settings.get("iso") == "Auto ISO":
            raise sonyremote.CameraError("restore write unavailable")
        original_exposure(**settings)

    def options(setting):
        if setting == "shutter":
            raise sonyremote.CameraError("live shutter choices unavailable")
        return original_options(setting)
    monkeypatch.setattr(stack.camera, "set_exposure", exposure)
    monkeypatch.setattr(stack.camera, "list_options", options)
    response = _plan(api, mode="night", frames=2)
    assert response.status_code == 503, response.text
    body = response.json()
    assert body["meter_restore"]["status"] == "failed" and "not restored" in body["detail"]
    assert body["may_have_fired"] is False and "recommended_start" not in body
    assert product_sessions.runner().list() == []


def test_a_fixed_timelapse_keeps_start_settings_inside_its_bounds(api, stack):
    # P2: Fixed timelapses ignored min_iso / min_shutter_s for start settings.
    response = _create(api, mode="timelapse", frames=2, timelapse_mode="fixed",
                       timelapse_options={"min_iso": 200, "min_shutter_s": 0.01}, start_settings=METERED)
    assert response.status_code == 400 and "bounds" in response.text, response.text
    assert product_sessions.runner().list() == [] and stack.camera.capture_count == 0


def test_the_form_is_frozen_while_metering_and_the_frozen_delay_is_submitted():
    # P2: Start with delay 0, the delay changes to 3600 while metering -> a delayed job with a meter from now.
    import re
    import shutil
    import subprocess
    import test_web_session_options as page
    node = shutil.which("node")
    if node is None:
        pytest.skip("node is not on PATH")
    html = (WEB / "index.html").read_text()
    handler = re.search(r"  \$\('session-start'\)\.onclick=async\(\)=>\{.*?\n  \};\n", html, re.S).group(0)
    runner = re.search(r"  async function runSession\(id, delaySeconds\) \{.*?\n  \}\n", html, re.S).group(0)
    form = {**page.FORM, "session-mode": "timelapse", "session-frames": "10", "session-interval": "0",
            "session-delay": "0", "session-baseline-source": "metered"}
    camera = {"model": MODEL, "port": PORT, "serial": SERIAL}
    script = ("const sent=[]; let busy=false, sessionRunning=false, cameraSelectionRequired=false, nightAssistRequest=null;\n"
              "const NO_SERIAL_MESSAGE='serial';\n"
              f"const availableCameras=[{json.dumps(camera)}]; const values={json.dumps(form)};\n"
              "const elements={}; const $=id=>elements[id] || (elements[id]={value:values[id],checked:false,textContent:''});\n"
              "const timelapseControls=()=>null,longExposureDuration=()=>undefined,deflickerOption=()=>({});\n"
              "const timelapseSessionFields=()=>({timelapse_mode:'holy_grail'});\n"
              "const sessionBusy=()=>{},showSession=()=>{},showToast=()=>{},noteSessionCameraWarning=()=>{},watchSessionJob=()=>{};\n"
              "let disabledWhileMetering=null;\n"
              "const sessionRequest=async(path,body)=>{sent.push({path,body});if(path==='/sessions/meter-plan'){"
              "disabledWhileMetering=elements['session-delay'].disabled; $('session-delay').value='3600';"
              f"return {{recommended_start:{json.dumps(METERED)}}};}}return {{id:'fixture'}};}};\n"
              + runner + handler +
              "\n$('session-start').onclick().then(()=>process.stdout.write(JSON.stringify("
              "{sent,disabledWhileMetering,disabledAfter:!!elements['session-delay'].disabled})));\n")
    result = subprocess.run([node, "-e", script], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr[-1500:]
    out = json.loads(result.stdout)
    jobs = [item for item in out["sent"] if item["path"].endswith("/jobs")]
    assert jobs and jobs[-1]["body"]["delay_seconds"] == 0           # the delay frozen at Start
    assert out["disabledWhileMetering"] is True and out["disabledAfter"] is False



@pytest.mark.parametrize("restore_works", [False, True])
def test_shoot_reports_the_meter_restore_like_meter_plan(api, stack, monkeypatch, restore_works):
    # Found during #135: /shoot's generic 503 answered only detail and dropped meter_restore, so a
    # failed restore of Auto ISO never told the owner the body was left at the meter's ISO 100.
    stack.camera.settings.update(iso="Auto ISO", shutter="2")
    original_exposure, original_options = stack.camera.set_exposure, stack.camera.list_options

    def exposure(**settings):
        if settings.get("iso") == "Auto ISO" and not restore_works:
            raise sonyremote.CameraError("restore write unavailable")
        original_exposure(**settings)

    def options(setting):
        if setting == "shutter":
            raise sonyremote.CameraError("live shutter choices unavailable")
        return original_options(setting)
    monkeypatch.setattr(stack.camera, "set_exposure", exposure)
    monkeypatch.setattr(stack.camera, "list_options", options)
    response = api.client.post("/shoot", headers=api.headers, json={
        **CAMERA, "stack_policy": {"mode": "fixed", "stack_mode": "night", "frames": 2}})
    assert response.status_code == 503, response.text
    body = response.json()
    assert stack.camera.capture_count == 0
    assert body["may_have_fired"] is False and body["retry_capture"] is False
    assert "live shutter choices unavailable" in body["detail"]
    if restore_works:
        assert body["meter_restore"]["status"] == "completed" and "warning" not in body
        assert stack.camera.settings["iso"] == "Auto ISO"
    else:
        assert body["meter_restore"]["status"] == "failed"
        assert "restore write unavailable" in body["meter_restore"]["reason"]
        assert "not restored" in body["warning"] and body["warning"] in body["detail"]
        assert stack.camera.settings["iso"] == "100"           # left at the meter's ISO
