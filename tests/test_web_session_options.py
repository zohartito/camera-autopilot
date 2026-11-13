"""The Sessions panel's Start builds a /sessions body the server accepts, for every mode.

Since 49c45fa1 the page sent `timelapse_mode` and a non-empty `timelapse_options` for EVERY mode,
and SessionRunner.create refuses timelapse configuration on any other mode ("timelapse
configuration requires timelapse mode"), so HDR, night (including Night Assist), crowd removal,
long exposure and panorama could not be started from the web page.

Camera-free: the page's own Start handler runs in node with stubbed DOM values and captures the
POST body; the same body is then sent to the app's POST /sessions (SessionRunner.create) with the
product fixture. Nothing opens a camera: creating a session does not connect to a body.
"""
from __future__ import annotations

import json
import re
import shutil
from pathlib import Path

import pytest

from test_capture_serial import A7III, NODE_RUN, PORT_1, SERIAL_A, bus  # noqa: F401 — autouse fake USB world
from test_final_wave_product_api import product  # noqa: F401 — the camera-free app fixture

ROOT = Path(__file__).resolve().parent.parent
INDEX = (ROOT / "backend" / "web" / "index.html").read_text(encoding="utf-8")
CAMERA = {"model": A7III, "port": PORT_1, "serial": SERIAL_A}
NIGHT_PLAN = {"mode": "night", "frames": 4, "ev_step": 0.0, "interval_s": 0.0,
              "camera_model": A7III, "camera_port": PORT_1, "camera_serial": SERIAL_A,
              "night_assist": {"iso": "1600", "aperture_label": "f/4", "shutter_label": "1", "frames": 4,
                               "evidence": {"fixture": True}, "camera_serial": SERIAL_A}}
FORM = {"camera-select": "0", "session-ev": "2", "session-interval": "0",
        "timelapse-policy": "holy_grail", "timelapse-fps": "24", "timelapse-min-iso": "100",
        "timelapse-max-iso": "6400", "timelapse-min-shutter": "0.001", "timelapse-max-shutter": "1",
        "timelapse-step": "0.3", "timelapse-deflicker-window": "5", "timelapse-deflicker-gain": "1",
        "long-exposure-duration": "", "panorama-projection": "cylindrical", "panorama-hfov": "65",
        "panorama-rotation": "0", "panorama-preview-every": "2",
        "session-baseline-source": "current"}  # the body fields are the subject; meter-first: test_session_meter_plan.py
MODES = [("hdr", 3), ("long_exposure", 3), ("crowd_remove", 3), ("night", 4), ("panorama", 3), ("timelapse", 10)]


def _page_function(name: str) -> str:
    """One top-level function of the page script, or '' when the page has none by that name."""
    found = re.search(r"  function " + name + r"\(.*?\n  }\n", INDEX, re.S)
    return found.group(0) if found else ""


def _node() -> str:
    path = shutil.which("node")
    if path is None:
        pytest.skip("node is not on PATH")
    if NODE_RUN([path, "--version"], check=False, capture_output=True).returncode != 0:
        pytest.skip("node --version failed")
    return path


def _start_body(mode: str, frames: int, *, deflicker: bool = True, night_plan: dict | None = None) -> dict:
    """Click the page's own Start handler for `mode` and return the POST /sessions body.
    The real deflickerOption and longExposureDuration run; every timelapse control is filled in
    and deflicker is ticked, exactly the state a user leaves behind after trying timelapse."""
    handler = re.search(r"  \$\('session-start'\)\.onclick=async\(\)=>\{.*?\n  \};\n", INDEX, re.S).group(0)
    values = {**FORM, "session-mode": mode, "session-frames": str(frames),
              "session-interval": "2" if mode == "timelapse" else "0"}
    script = ("const sent=[]; let busy=false, sessionRunning=false, cameraSelectionRequired=false;\n"
              "const NO_SERIAL_MESSAGE='fixture: no serial';\n"
              f"let nightAssistRequest={json.dumps(night_plan)};\n"
              f"const availableCameras=[{json.dumps(CAMERA)}];\n"
              f"const values={json.dumps(values)};\n"
              f"const checked={{'timelapse-deflicker':{json.dumps(deflicker)},'panorama-autocrop':true}};\n"
              "const elements={};\n"
              "const $=id=>elements[id] || (elements[id]={value:values[id],checked:!!checked[id],textContent:''});\n"
              "const timelapseControls=()=>null;\n"
              + _page_function("deflickerOption") + _page_function("longExposureDuration")
              + _page_function("timelapseSessionFields") +
              "const sessionBusy=()=>{}, showSession=()=>{}, showToast=message=>sent.push({toast:message});\n"
              "const noteSessionCameraWarning=(id,response)=>sent.push({warning:id});\n"
              "const sessionRequest=async(path,body)=>{sent.push({path,body});return {id:'fixture'};};\n"
              "const runSession=async id=>sent.push({run:id});\n"
              f"{handler}\n"
              "elements['session-start'].onclick().then(()=>process.stdout.write(JSON.stringify(\n"
              "  {sent, status: elements['session-status']?.textContent || ''})));")
    # The autouse fake USB world replaces subprocess.run; NODE_RUN is the real one, captured first.
    result = NODE_RUN([_node(), "-e", script], check=False, capture_output=True, text=True)
    assert result.returncode == 0, result.stderr[-1500:]
    out = json.loads(result.stdout)
    posts = [item["body"] for item in out["sent"] if item.get("path") == "/sessions"]
    assert len(posts) == 1, out
    # The handler went all the way through: the answer was noted and the job was submitted.
    assert {"warning": "fixture"} in out["sent"] and {"run": "fixture"} in out["sent"], out
    return posts[0]


@pytest.mark.parametrize("mode,frames", [m for m in MODES if m[0] != "timelapse"])
def test_start_sends_no_timelapse_configuration_for_other_modes(mode, frames):
    body = _start_body(mode, frames)
    assert body["mode"] == mode
    assert "timelapse_mode" not in body and "timelapse_options" not in body, body
    assert "deflicker" not in json.dumps(body)


def test_start_sends_timelapse_configuration_and_deflicker_for_timelapse():
    body = _start_body("timelapse", 10)
    assert body["timelapse_mode"] == "holy_grail"
    options = body["timelapse_options"]
    assert options["fps"] == 24 and options["max_shutter_s"] == 1 and options["max_step_ev"] == 0.3
    assert options["deflicker"] == {"enabled": True, "window": 5, "max_gain_stops": 1}
    # Unticked never asks for deflicker (PR #80 makes Holy Grail send an explicit {enabled: false}).
    unticked = _start_body("timelapse", 10, deflicker=False)["timelapse_options"]
    assert unticked.get("deflicker", {}).get("enabled") is not True


@pytest.mark.parametrize("mode,frames", MODES)
def test_the_exact_page_body_is_accepted_by_the_server(product, bus, mode, frames):  # noqa: F811
    """What the page sends, unmodified, through POST /sessions into SessionRunner.create."""
    product.fake.name = "real"
    body = _start_body(mode, frames)
    response = product.client.post("/sessions", headers=product.headers, json=body)
    assert response.status_code == 200, (mode, response.text)
    state = response.json()
    assert state["mode"] == mode and state["frame_count"] == frames
    assert state["metadata"]["camera_serial"] == SERIAL_A
    assert not bus.claims and not bus.captures  # creating a session never opens the camera


def test_night_assist_start_body_is_accepted_by_the_server(product, bus):  # noqa: F811
    product.fake.name = "real"
    body = _start_body("night", 4, night_plan=NIGHT_PLAN)
    assert body["night_assist"] == NIGHT_PLAN["night_assist"] and "timelapse_options" not in body
    response = product.client.post("/sessions", headers=product.headers, json=body)
    assert response.status_code == 200, response.text
    assert response.json()["metadata"]["night_assist"]["iso"] == "1600"
