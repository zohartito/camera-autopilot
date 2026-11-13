"""The Sessions panel shows the server's host-camera-client warning (#129) for sessions.

`POST /sessions` and `POST /sessions/{id}/jobs` carry `host_camera_warning` while Preview,
Photos or Image Capture runs. The page shows that sentence verbatim, as text, bound to the
session it was issued for, and nothing when it is absent. Camera-free: the page's own helper and
render functions run in node against a fake element; the wiring is pinned at source level.
"""
from __future__ import annotations

import json
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "backend"))

import host_camera_clients  # noqa: E402

WEB = ROOT / "backend" / "web"
INDEX = (WEB / "index.html").read_text(encoding="utf-8")
WORKFLOWS = (WEB / "workflows.js").read_text(encoding="utf-8")
REVIEW = (WEB / "review.html").read_text(encoding="utf-8")

# Exactly the sentence the server builds (backend/host_camera_clients.summary_line).
PREVIEW_LINE = host_camera_clients.summary_line({"status": "checked", "clients": ["Preview"]})
# A sentence that would become markup if it were ever assigned as HTML.
MARKUP_LINE = 'Quit <img src=x onerror="alert(1)"> & <b>Photos</b> while the camera is in use.'
ID_A, ID_B = "a" * 32, "b" * 32


def _node() -> str:
    path = shutil.which("node")
    if path is None:
        pytest.skip("node is not on PATH")
    return path


def _page_functions() -> str:
    """The page's own warning state and render functions, cut from index.html."""
    start = INDEX.index("  let sessionCameraWarning=null;")
    end = INDEX.index("  function sessionBusy(value) {")
    return INDEX[start:end]


def _run(steps: str) -> dict:
    """Run the page functions with a fake `$` whose element records text and HTML writes."""
    script = ("const window = {};\n" + WORKFLOWS + "\nconst AutopilotWorkflows = window.AutopilotWorkflows;\n"
              "const element = {hidden: true, _text: '', htmlWrites: 0,\n"
              "  set textContent(v) { this._text = String(v); }, get textContent() { return this._text; },\n"
              "  set innerHTML(v) { this.htmlWrites += 1; this._text = String(v); }, get innerHTML() { return this._text; }};\n"
              "const $ = id => { if (id !== 'session-camera-warning') throw new Error('unexpected id ' + id); return element; };\n"
              "let activeSession = null;\n"
              + _page_functions() + "\nconst seen = [];\n"
              "const look = () => seen.push({text: element.textContent, hidden: element.hidden, htmlWrites: element.htmlWrites});\n"
              + steps + "\nprocess.stdout.write(JSON.stringify(seen));")
    out = subprocess.run([_node(), "-e", script], check=True, capture_output=True, text=True).stdout
    return json.loads(out)


def test_helper_returns_the_server_sentence_verbatim_or_nothing():
    responses = [{"host_camera_warning": PREVIEW_LINE}, {"host_camera_warning": MARKUP_LINE},
                 {"id": ID_A, "host_camera_clients": {"status": "checked", "clients": []}},
                 {"host_camera_warning": ""}, {"host_camera_warning": "   "}, {"host_camera_warning": 5},
                 {"host_camera_warning": None}, {}, None]
    script = ("const window = {};\n" + WORKFLOWS + "\n"
              f"const responses = {json.dumps(responses)};\n"
              "process.stdout.write(JSON.stringify(responses.map(r => window.AutopilotWorkflows.cameraClientWarning(r))));")
    shown = json.loads(subprocess.run([_node(), "-e", script], check=True, capture_output=True, text=True).stdout)
    assert shown == [PREVIEW_LINE, MARKUP_LINE, "", "", "", "", "", "", ""]


def test_warning_shown_as_text_for_its_session_and_absent_otherwise():
    seen = _run(f"""
      activeSession = '{ID_A}';
      noteSessionCameraWarning('{ID_A}', {{id: '{ID_A}', host_camera_clients: {{status: 'checked', clients: []}}}}); look();
      noteSessionCameraWarning('{ID_A}', {{id: '{ID_A}', host_camera_warning: {json.dumps(MARKUP_LINE)}}}); look();
      activeSession = '{ID_B}'; renderSessionCameraWarning(); look();   // another session selected
      activeSession = '{ID_A}'; renderSessionCameraWarning(); look();   // back to the warned session
      noteSessionCameraWarning('{ID_A}', {{status: 'queued', host_camera_clients: {{status: 'checked', clients: []}}}}); look();
      noteSessionCameraWarning('{ID_A}', {{status: 'queued', host_camera_warning: {json.dumps(PREVIEW_LINE)}}}); look();
      activeSession = null; renderSessionCameraWarning(); look();       // session deleted
    """)
    hidden = {"text": "", "hidden": True, "htmlWrites": 0}
    assert seen == [hidden,
                    {"text": MARKUP_LINE, "hidden": False, "htmlWrites": 0},
                    hidden,
                    {"text": MARKUP_LINE, "hidden": False, "htmlWrites": 0},
                    hidden,   # the newer job answer found no client: the old warning goes
                    {"text": PREVIEW_LINE, "hidden": False, "htmlWrites": 0},
                    hidden]


def test_forget_device_clears_the_warning():
    """Forget clears the active session, so its warning goes too (and does not come back)."""
    forget = INDEX[INDEX.index("  $('forget-device').onclick"):INDEX.index("  $('device-refresh').onclick")]
    script = ("const window = {};\n" + WORKFLOWS + "\nconst AutopilotWorkflows = window.AutopilotWorkflows;\n"
              "const elements = {};\n"
              "const $ = id => elements[id] || (elements[id] = {hidden: true, textContent: '', value: '', checked: true});\n"
              f"let activeSession = '{ID_A}', selectedSessionState = {{}}, apiToken = 't', bodyBaseline = {{}};\n"
              "let cameraSelectionRequired = false; const availableCameras = [];\n"
              "const Autopilot = {deviceStore: {forget() {}}}; const confirm = () => true;\n"
              "const renderBodyBaseline = () => {}, syncCaptureControls = () => {}, syncAutoSaveControl = () => {};\n"
              + _page_functions() + forget +
              f"noteSessionCameraWarning('{ID_A}', {{host_camera_warning: {json.dumps(PREVIEW_LINE)}}});\n"
              "const before = {...elements['session-camera-warning']};\n"
              "$('forget-device').onclick();\n"
              "const after = {...elements['session-camera-warning']};\n"
              f"activeSession = '{ID_A}'; renderSessionCameraWarning();\n"
              "process.stdout.write(JSON.stringify([before, after, {...elements['session-camera-warning']}]));")
    out = subprocess.run([_node(), "-e", script], check=True, capture_output=True, text=True).stdout
    before, after, reselected = json.loads(out)
    assert (before["textContent"], before["hidden"]) == (PREVIEW_LINE, False)
    assert (after["textContent"], after["hidden"]) == ("", True)
    assert (reselected["textContent"], reselected["hidden"]) == ("", True)


def test_page_wires_both_responses_and_versions_the_script():
    assert '<p id="session-camera-warning" class="voice-status" role="note" hidden></p>' in INDEX
    panel = INDEX.split('<p id="session-status"', 1)[1].split("</details>", 1)[0]
    assert 'id="session-camera-warning"' in panel
    block = _page_functions()
    assert "innerHTML" not in block and "$('session-camera-warning').textContent=shown;" in block
    # POST /sessions: noted before the panel renders that session.
    start = INDEX.split("let state=await sessionRequest('/sessions',", 1)[1].split("showSession(state);", 1)[0]
    assert start.rstrip().endswith("noteSessionCameraWarning(state.id,state);")
    # POST /sessions/{id}/jobs: the answer is kept and noted; a lost answer keeps the earlier one.
    run = INDEX.split("async function runSession(id, delaySeconds) {", 1)[1].split("\n  }\n", 1)[0]
    assert ("const job=await sessionRequest('/sessions/'+id+'/jobs',{delay_seconds:delaySeconds ?? "
            "Number($('session-delay').value || 0)});\n      noteSessionCameraWarning(id,job);") in run
    show = INDEX.split("function showSession(state) {", 1)[1].split("\n  }\n", 1)[0]
    assert "renderSessionCameraWarning();" in show
    assert "'session-partial-movie','session-camera-warning'].forEach(id=>$(id).hidden=true);" in INDEX
    assert INDEX.count("noteSessionCameraWarning(") == 3   # definition + two responses
    # workflows.js changed: every page that loads it asks for the new version (2026-09-17 lesson).
    for page in (INDEX, REVIEW):
        assert '<script src="/workflows.js?v=20261003c"></script>' in page
        assert "workflows.js?v=20261002a" not in page
