"""Capture proves WHICH body it fires by USB serial, never by model + port alone.

USB ports re-enumerate; the serial is the only stable identity (docs/ROUTE_MATRIX_2026-09-15.md).
Two same-model bodies that exchange USB addresses between selection and capture must be refused before any setting write or shutter.

Camera-free: libgphoto2 (gp.Camera, port and abilities lists), gphoto2 autodetect, the OS USB
readers (ioreg/sysfs), the read-only probe and every subprocess are replaced by fakes in an autouse
fixture, so no test here can open a real camera even when run against older code.
"""
from __future__ import annotations

import json
import re
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest
from PIL import Image

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "backend"))

import engine
import product_sessions
import sonyremote.camera as camera_mod
from sonyremote.camera import Camera, CameraError
from test_final_wave_product_api import product  # noqa: F401 — the camera-free app fixture

A7III = "Sony Alpha-A7 III (PC Control)"
SERIAL_A = "A7III0SERIAL"   # the body the user selected
SERIAL_B = "AAAA1111BBBB"   # a second A7 III (fixture serial)
PORT_1, PORT_2 = "usb:000,003", "usb:000,004"
INDEX = (ROOT / "backend" / "web" / "index.html").read_text(encoding="utf-8")
NODE_RUN = subprocess.run  # captured before the autouse fixture replaces subprocess.run
REAL_CAPTURE = Camera.capture  # the real shutter path, for tests that must not use the stub
REAL_RECORD = Camera.record_native


def _ioreg(serial, address, product="ILCE-7M3"):
    """One macOS ioreg USB record; locationID 1245184 is bus 0, so the port is usb:000,<address>."""
    return {"USB Vendor Name": "Sony", "USB Product Name": product,
            "kUSBSerialNumberString": serial or "", "locationID": "1245184", "USB Address": str(address)}


SELECTED = [_ioreg(SERIAL_A, 3), _ioreg(SERIAL_B, 4)]   # as the user saw it: A at PORT_1
SWAPPED = [_ioreg(SERIAL_B, 3), _ioreg(SERIAL_A, 4)]    # after re-enumeration: B at PORT_1


class Bus:
    """What the fake USB world observed: claims, setting writes, shutters, CLI commands."""

    def __init__(self):
        self.views = [SELECTED]
        self.claims, self.writes, self.captures, self.commands = [], [], [], []

    def os_devices(self):
        # Each OS read consumes the next view; the last one persists.
        return list(self.views.pop(0) if len(self.views) > 1 else self.views[0])


class FakeGPCamera:
    """Stands in for gphoto2.Camera: records every claim, write and shutter."""

    def __init__(self, bus):
        self.bus, self.path, self.model = bus, None, None

    def set_port_info(self, path): self.path = path
    def set_abilities(self, model): self.model = model
    def init(self): self.bus.claims.append(self.path)
    def exit(self): pass
    def get_abilities(self): return SimpleNamespace(model=self.model)
    def get_port_info(self): return SimpleNamespace(get_path=lambda: self.path)
    def set_config(self, *args): self.bus.writes.append(("gp.set_config", args))
    def set_single_config(self, *args): self.bus.writes.append(("gp.set_single_config", args))
    def capture(self, *args): self.bus.captures.append(("gp.capture", args))
    def trigger_capture(self, *args): self.bus.captures.append(("gp.trigger_capture", args))


class _Lookup:
    def load(self): pass
    def lookup_path(self, path): return path
    def lookup_model(self, model): return model
    def __getitem__(self, key): return key


@pytest.fixture(autouse=True)
def bus(monkeypatch, tmp_path):
    """Replace every hardware and host-state entry point below sonyremote.Camera."""
    world = Bus()
    monkeypatch.setattr(camera_mod.gp, "Camera", lambda: FakeGPCamera(world))
    monkeypatch.setattr(camera_mod.gp, "PortInfoList", _Lookup)
    monkeypatch.setattr(camera_mod.gp, "CameraAbilitiesList", _Lookup)
    monkeypatch.setattr(camera_mod, "_autodetect_rows", lambda: [(A7III, PORT_1), (A7III, PORT_2)])
    monkeypatch.setattr(camera_mod, "_read_os_usb_devices", world.os_devices)
    monkeypatch.setattr(camera_mod, "_read_sysfs_usb_devices", lambda *a: None)
    monkeypatch.setattr(camera_mod, "_probe_camera_answers", lambda *a: pytest.fail("probe opened a camera"))
    monkeypatch.setattr(camera_mod, "_release_macos_camera_service", lambda: None)

    def run(command, **kwargs):
        world.commands.append(list(command))
        if "--capture-image-and-download" in command:
            world.captures.append(("gphoto2 CLI", command))
            (Path(kwargs["cwd"]) / "DSC00001.JPG").write_bytes(b"fixture JPEG")
        return SimpleNamespace(returncode=0, stdout="", stderr="")
    monkeypatch.setattr(camera_mod.subprocess, "run", run)
    # Everything the product engine does to a connected body after connect, recorded.
    image = tmp_path / "captured.jpg"
    Image.new("RGB", (16, 12), "gray").save(image)
    monkeypatch.setattr(Camera, "wake", lambda self, timeout=20.0: True)
    monkeypatch.setattr(Camera, "shooting_mode", lambda self: "M")
    monkeypatch.setattr(Camera, "list_options", lambda self, key: [])
    monkeypatch.setattr(Camera, "get_settings", lambda self: {"iso": "100", "aperture": "f/4", "shutter": "1/60",
                                                                "focus_mode": "Manual"})
    monkeypatch.setattr(Camera, "set_setting", lambda self, key, value, max_seconds=8.0:
                        world.writes.append(("set_setting", key, value)) or value)
    monkeypatch.setattr(Camera, "set_exposure", lambda self, **kw: world.writes.append(("set_exposure", kw)) or kw)
    monkeypatch.setattr(Camera, "set_color_temp", lambda self, kelvin: world.writes.append(("kelvin", kelvin)))
    monkeypatch.setattr(Camera, "record_native", lambda self, seconds=6.0:
                        world.captures.append(("movie", seconds)) or {"recorded_seconds": seconds})

    def capture(self, download=True, dest_dir=None):
        world.captures.append(("capture", self.info().port))
        self.last_capture_receipt = {"serial": getattr(self, "verified_serial", None),
                                     "serial_verification": getattr(self, "serial_verification", None)}
        return image
    monkeypatch.setattr(Camera, "capture", capture)
    monkeypatch.delenv("CAMERA_SERIAL", raising=False)
    monkeypatch.delenv("CAMERA_MODEL", raising=False)
    monkeypatch.delenv("CAMERA_PORT", raising=False)
    # Nothing a test downloads or quarantines may land in the repository tree.
    monkeypatch.setattr(camera_mod, "CAPTURE_DIR", tmp_path / "captures")
    monkeypatch.setenv("CAMERA_AUTOPILOT_SHOTS_DIR", str(tmp_path / "default-shots"))
    return world


def _fake_brain(monkeypatch):
    """The metered pipeline reduced to one write and one shutter on the connected body."""
    def brain(self, cam, plan):
        cam.set_setting("iso", "100")
        cam.capture(download=True)
        name = "shot_serial_fixture.jpg"
        Image.new("RGB", (16, 12), "gray").save(engine.SHOTS_DIR / name)
        return engine.Shot(True, name, "Photo captured.", "fixture", {}, {"iso": "100"}, evidence={})
    monkeypatch.setattr(engine.RealEngine, "_brain_shoot", brain)


def _selection(serial=SERIAL_A, port=PORT_1):
    return {"camera_model": A7III, "camera_port": port, **({"camera_serial": serial} if serial else {})}


def _real(product):
    product.fake.name = "real"
    _fake_brain(product.monkeypatch)


def _nothing_fired(bus):
    return bus.captures == [] and bus.writes == []


# --- 1. the same-model swap ------------------------------------------------------------------

def test_same_model_swap_is_refused_before_any_write_or_shutter(product, bus):
    """Two A7 IIIs exchange ports after the user selected A at PORT_1: /shoot must refuse."""
    _real(product)
    bus.views = [SWAPPED]
    response = product.client.post("/shoot", headers=product.headers, json={"intent": "x", **_selection()})
    assert response.status_code == 409, response.text
    body = response.json()
    assert body["reason"] == "serial_mismatch"
    assert (body["expected_serial"], body["observed_serial"]) == (SERIAL_A, SERIAL_B)
    assert body["may_have_fired"] is False and body["retry_capture"] is False
    assert bus.claims == [PORT_1]  # opened, proved wrong, released
    assert _nothing_fired(bus), (bus.captures, bus.writes)
    assert not bus.commands


def test_swap_between_connect_and_cli_shutter_is_refused_before_the_cli(bus, monkeypatch, tmp_path):
    """The CLI re-opens the port by address after Python releases it; re-prove the serial first."""
    camera = Camera(model=A7III, port=PORT_1, serial=SERIAL_A)
    camera.connect()
    monkeypatch.setattr(camera, "_has_sony_action_capture", lambda: True)
    monkeypatch.setattr(camera_mod.shutil, "which", lambda _name: "/fake/gphoto2")
    monkeypatch.setattr(camera_mod.time, "sleep", lambda _s: bus.views.__setitem__(0, SWAPPED))
    with pytest.raises(CameraError) as refused:
        camera._capture_sony_action(dest_dir=tmp_path)
    assert getattr(refused.value, "reason", None) == "serial_mismatch"
    assert not any("--capture-image-and-download" in command for command in bus.commands)


# --- 2. the matching serial shoots, and the receipt carries it ----------------------------------

def test_matching_serial_shoots_and_the_receipt_records_the_verified_serial(product, bus):
    _real(product)
    response = product.client.post("/shoot", headers=product.headers, json={"intent": "x", **_selection()})
    assert response.status_code == 200, response.text
    evidence = response.json()["evidence"]
    assert evidence["camera_serial"] == SERIAL_A
    verification = evidence["serial_verification"]
    assert verification["serial"] == SERIAL_A and verification["port"] == PORT_1
    assert verification["method"] == "os_usb_serial_at_connected_port"
    assert bus.captures == [("capture", PORT_1)]
    sidecar = json.loads((product.root / "shot_serial_fixture.json").read_text())
    assert sidecar["evidence"]["camera_serial"] == SERIAL_A


def test_cli_capture_receipt_carries_connect_and_pre_shutter_verification(bus, monkeypatch, tmp_path):
    camera = Camera(model=A7III, port=PORT_1, serial=SERIAL_A)
    camera.connect()
    monkeypatch.setattr(camera, "_has_sony_action_capture", lambda: True)
    monkeypatch.setattr(camera_mod.shutil, "which", lambda _name: "/fake/gphoto2")
    monkeypatch.setattr(camera_mod.time, "sleep", lambda _s: None)
    assert camera._capture_sony_action(dest_dir=tmp_path).is_file()
    receipt = camera.last_capture_receipt
    assert receipt["serial"] == SERIAL_A
    assert receipt["pre_shutter_serial_verification"]["serial"] == SERIAL_A
    assert receipt["reconnect_ok"] is True


# --- 3. no expected serial, or a body without a unique serial, never fires ---------------------

def test_shoot_without_an_expected_serial_is_refused_before_opening_a_body(product, bus):
    _real(product)
    response = product.client.post("/shoot", headers=product.headers, json=_selection(serial=None))
    assert response.status_code == 400, response.text
    assert response.json()["reason"] == "serial_required"
    assert bus.claims == [] and _nothing_fired(bus)


@pytest.mark.parametrize("view,reason", [
    ([_ioreg(None, 3), _ioreg(SERIAL_B, 4)], "serial_missing"),
    ([_ioreg(SERIAL_A, 3), _ioreg(SERIAL_A, 4)], "serial_duplicate"),
])
def test_a_body_without_a_unique_serial_is_refused(product, bus, view, reason):
    _real(product)
    bus.views = [view]
    response = product.client.post("/shoot", headers=product.headers, json=_selection())
    assert response.status_code == 409, response.text
    assert response.json()["reason"] == reason
    assert _nothing_fired(bus)


def test_record_without_an_expected_serial_is_refused(product, bus):
    _real(product)
    response = product.client.post("/record", headers=product.headers, json=_selection(serial=None))
    assert response.status_code == 400 and response.json()["reason"] == "serial_required"
    assert _nothing_fired(bus)


def test_session_create_requires_the_serial_and_stores_it(product, bus):
    product.fake.name = "real"
    missing = product.client.post("/sessions", headers=product.headers,
                                  json={"mode": "hdr", "frames": 3, **_selection(serial=None)})
    assert missing.status_code == 400 and missing.json()["reason"] == "serial_required"
    created = product.client.post("/sessions", headers=product.headers,
                                  json={"mode": "hdr", "frames": 3, **_selection()})
    assert created.status_code == 200, created.text
    assert created.json()["metadata"]["camera_serial"] == SERIAL_A


def test_session_run_after_a_same_model_swap_is_refused_before_any_frame(product, bus):
    product.fake.name = "real"
    created = product.client.post("/sessions", headers=product.headers,
                                  json={"mode": "hdr", "frames": 3, **_selection()}).json()
    bus.views = [SWAPPED]
    response = product.client.post(f"/sessions/{created['id']}/run", headers=product.headers)
    assert response.status_code == 409, response.text
    assert response.json()["reason"] == "serial_mismatch"
    assert _nothing_fired(bus)
    state = product_sessions.runner().read(created["id"])
    assert all(frame["status"] == "pending" for frame in state["frames"])


def test_handheld_requires_the_serial_and_refuses_a_swapped_body(product, bus):
    _real(product)
    missing = product.client.post("/handheld", headers=product.headers, json=_selection(serial=None))
    assert missing.status_code == 400 and missing.json()["reason"] == "serial_required"
    bus.views = [SWAPPED]
    swapped = product.client.post("/handheld", headers=product.headers, json=_selection())
    assert swapped.status_code == 409, swapped.text
    assert swapped.json()["reason"] == "serial_mismatch"
    assert not product.app._camera_lock.locked()
    assert _nothing_fired(bus)


# --- MCP ------------------------------------------------------------------------------------

@pytest.fixture
def mcp(monkeypatch):
    import server
    monkeypatch.setattr(server, "_camera", None)
    yield server
    server._stop_keepalive()  # importing server started its keep-alive thread


def test_mcp_detect_camera_reports_serial_and_serial_status(mcp, bus):
    bus.views = [[_ioreg(SERIAL_A, 3), _ioreg(None, 4, product="ILCE-7M3")]]
    report = mcp.detect_camera()
    rows = {row["port"]: row for row in report["cameras"]}
    assert (rows[PORT_1]["serial"], rows[PORT_1]["serial_status"]) == (SERIAL_A, "present")
    assert (rows[PORT_2]["serial"], rows[PORT_2]["serial_status"]) == (None, "missing")
    assert bus.claims == []


def test_mcp_capture_requires_a_serial_and_refuses_a_swap_after_discovery(mcp, bus):
    refused = mcp.capture_photo(None)
    assert refused.is_error and refused.structured_content["reason"] == "serial_required"
    bus.views = [SELECTED, SWAPPED]  # discovery sees A at PORT_1; by connect the ports exchanged
    swapped = mcp.capture_photo(SERIAL_A)
    assert swapped.is_error and swapped.structured_content["reason"] == "serial_mismatch"
    assert swapped.structured_content["may_have_fired"] is False
    assert _nothing_fired(bus)


def test_mcp_capture_with_the_matching_serial_fires_that_body(mcp, bus):
    result = mcp.capture_photo(SERIAL_A)
    assert not result.is_error, result.structured_content
    # The connection's own OS verification, never an echo of the requested serial.
    verification = result.structured_content["serial_verification"]
    assert verification["method"] == "os_usb_serial_at_connected_port"
    assert (verification["serial"], verification["port"], verification["os_source"]) == (SERIAL_A, PORT_1, "ioreg")
    assert result.structured_content["camera_serial"] == SERIAL_A
    assert bus.captures == [("capture", PORT_1)]


def test_mcp_autoexpose_shoot_and_movie_require_a_serial(mcp, bus, monkeypatch, tmp_path):
    assert mcp.auto_expose(None).structured_content["reason"] == "serial_required"
    monkeypatch.setattr(engine, "SHOTS_DIR", tmp_path)
    monkeypatch.setattr(engine, "get_engine", lambda: engine.RealEngine())
    _fake_brain(monkeypatch)
    shot = mcp.shoot("x")
    assert shot.is_error and shot.structured_content["reason"] == "serial_required"
    monkeypatch.setattr(mcp, "assert_recording_allowed", lambda: None)
    with pytest.raises(CameraError) as refused:
        mcp.record_native_video(1.0)
    assert getattr(refused.value, "reason", None) == "serial_required"
    assert bus.claims == [] and _nothing_fired(bus)


# --- 4. status ------------------------------------------------------------------------------

@pytest.mark.parametrize("row,serial_status", [
    ({"serial": None}, "missing"),
    ({"serial": None, "serial_status": "duplicate"}, "duplicate"),
])
def test_status_reports_a_body_without_a_unique_serial_as_not_ready(monkeypatch, row, serial_status):
    rows = [{"model": A7III, "port": PORT_1, "method": "autodetect", "os_source": "ioreg", **row}]
    monkeypatch.setattr("sonyremote.detect_identified", lambda: rows)
    status = engine.RealEngine().status()
    assert status["ready"] is False
    assert status["serial_status"] == serial_status
    assert status["cameras"][0]["serial_status"] == serial_status
    assert "serial" in status["hint"]


def test_status_is_ready_for_a_body_with_a_serial(monkeypatch):
    rows = [{"model": A7III, "port": PORT_1, "serial": SERIAL_A, "method": "autodetect", "os_source": "ioreg"}]
    monkeypatch.setattr("sonyremote.detect_identified", lambda: rows)
    status = engine.RealEngine().status()
    assert status["ready"] is True and status["serial"] == SERIAL_A and status["serial_status"] == "present"


# --- 5. the web client never picks a serial-less row for the user ---------------------------

def _auto_select(cameras: list[dict]) -> dict:
    """Run the page's own restore/auto-select lines (index.html) in node, nothing remembered."""
    block = re.search(r"try \{ savedPair = JSON\.parse\(savedCamera\); \} catch \(_\) \{\}\n"
                      r"(.*?\$\(\"camera-select\"\)\.value = \"0\";)\n", INDEX, re.S).group(1)
    script = ("const savedCamera = null; const savedPair = null; let cameraSelectionRequired;\n"
              f"const availableCameras = {json.dumps(cameras)};\n"
              "const select = {value: ''}, status = {textContent: ''};\n"
              "const $ = id => id === 'camera-select' ? select : status;\n"
              f"{block}\n"
              "process.stdout.write(JSON.stringify({value: select.value, required: cameraSelectionRequired}));")
    return json.loads(NODE_RUN(["node", "-e", script], check=True, capture_output=True, text=True).stdout)


@pytest.mark.parametrize("serial_status", ["missing", "duplicate"])
def test_a_sole_serial_less_camera_is_never_auto_selected(serial_status):
    result = _auto_select([{"model": A7III, "port": PORT_1, "serial": None, "serial_status": serial_status}])
    assert result == {"value": "", "required": True}
    # Control: the same sole row carrying its serial is still picked for the user.
    assert _auto_select([{"model": A7III, "port": PORT_1, "serial": SERIAL_A, "serial_status": "present"}]) == \
        {"value": "0", "required": False}


def test_take_photo_refuses_a_selected_serial_less_row_and_sends_the_serial():
    function = re.search(r"  async function takePhoto\(.*?\n  \}\n", INDEX, re.S).group(0)
    constant = re.search(r"const NO_SERIAL_MESSAGE = .*;", INDEX)
    script = ((constant.group(0) if constant else "const NO_SERIAL_MESSAGE = 'fixture';") + "\n" + function + """
      const sent = [];
      const captureRequest = async (path, body) => { sent.push(body); return body; };
      const values = {'camera-select': {value: '0'}, refine: {checked: false}, 'stack-policy': {value: 'off'},
                      'stack-mode': {value: 'night'}, 'stack-frames': {value: '3'}};
      const $ = id => values[id];
      let cameraSelectionRequired = false;
      let availableCameras = [{model: 'A7 III', port: 'usb:000,003', serial: null}];
      (async () => {
        let refused = null;
        try { await takePhoto('x'); } catch (e) { refused = e; }
        availableCameras = [{model: 'A7 III', port: 'usb:000,003', serial: 'A7III0SERIAL'}];
        await takePhoto('x');
        process.stdout.write(JSON.stringify({refused: Boolean(refused), sent}));
      })();
    """)
    result = json.loads(NODE_RUN(["node", "-e", script], check=True, capture_output=True, text=True).stdout)
    assert result["refused"] is True
    assert [body.get("camera_serial") for body in result["sent"]] == ["A7III0SERIAL"]


# --- 6. a session saved before serial binding never resumes by port ---------------------------

def _legacy_session():
    """A session as the previous release saved it: model and port, no serial."""
    return product_sessions.runner().create("hdr", 3, metadata={"camera_model": A7III, "camera_port": PORT_1})


def test_legacy_session_run_resume_and_recovery_require_reselection(product, bus, monkeypatch):
    product.fake.name = "real"
    opened = []
    monkeypatch.setattr("sonyremote.Camera", lambda **kw: opened.append(kw) or pytest.fail("legacy session opened a body"))
    legacy = _legacy_session()
    with pytest.raises(CameraError) as direct:
        product_sessions.run(legacy["id"])
    assert getattr(direct.value, "reason", None) == "reselection_required"
    for path in ("run", "resume", "jobs"):
        response = product.client.post(f"/sessions/{legacy['id']}/{path}", headers=product.headers, json={})
        assert response.status_code == 409, (path, response.text)
        assert response.json()["reason"] == "reselection_required"
    assert opened == [] and _nothing_fired(bus)


def test_explicit_reselection_binds_the_serial_and_the_next_run_proves_it(product, bus):
    product.fake.name = "real"
    legacy = _legacy_session()
    bus.views = [SWAPPED]  # the OS reports A at PORT_2 at bind time
    bound = product.client.post(f"/sessions/{legacy['id']}/camera", headers=product.headers,
                                json=_selection(port=PORT_2))
    assert bound.status_code == 200, bound.text
    assert bound.json()["metadata"]["camera_serial"] == SERIAL_A
    assert bound.json()["metadata"]["camera_port"] == PORT_2
    other = product.client.post(f"/sessions/{legacy['id']}/camera", headers=product.headers,
                                json=_selection(serial=SERIAL_B))
    assert other.status_code == 400  # a bound serial never changes
    bus.views = [SELECTED]  # A is at PORT_1, not PORT_2: the run must refuse, not fire B
    response = product.client.post(f"/sessions/{legacy['id']}/run", headers=product.headers)
    assert response.status_code == 409 and response.json()["reason"] == "serial_mismatch"
    assert _nothing_fired(bus)


# --- tools that fire the shutter ----------------------------------------------------------

def test_focus_bracket_tool_refuses_to_fire_without_a_serial(monkeypatch, capsys, tmp_path):
    sys.path.insert(0, str(ROOT / "tools"))
    import focus_bracket_capture as tool
    monkeypatch.setattr(tool, "Camera", lambda **kw: pytest.fail("camera must not be opened"))
    code = tool.main(["--model", A7III, "--port", PORT_1, "--direction", "far", "--out", str(tmp_path / "out")])
    assert code == 2 and "--serial" in json.loads(capsys.readouterr().out)["error"]


# --- round 2: refusals stay clean refusals; nothing retries them ---------------------------

LADDERS = {"iso": ["100", "200", "400"], "aperture": ["f/4", "f/5.6", "f/8"],
           "shutter": ["1/250", "1/125", "1/60", "1/30", "1/15"]}


def _real_shutter_path(monkeypatch, bus, *, swap_at=None, after=SWAPPED):
    """Use the real Camera.capture/CLI path; `swap_at` changes the OS view to `after` at the
    settle sleep ("settle": before the pre-shutter check) or inside the CLI ("cli": after it)."""
    monkeypatch.setattr(Camera, "capture", REAL_CAPTURE)
    monkeypatch.setattr(Camera, "_has_sony_action_capture", lambda self: True)
    monkeypatch.setattr(Camera, "list_options", lambda self, key: list(LADDERS.get(key, [])))
    monkeypatch.setattr(camera_mod.shutil, "which", lambda _name: "/fake/gphoto2")
    live = {"iso": "100", "aperture": "f/4", "shutter": "1/60", "focus_mode": "Manual"}
    monkeypatch.setattr(Camera, "get_settings", lambda self: dict(live))
    monkeypatch.setattr(Camera, "set_exposure", lambda self, **kw: live.update(
        {key: value for key, value in kw.items() if value is not None}) or dict(live))
    monkeypatch.setattr(Camera, "set_setting", lambda self, key, value, max_seconds=8.0: live.update({key: value}) or value)
    if swap_at == "settle":
        monkeypatch.setattr(camera_mod.time, "sleep", lambda _s: bus.views.__setitem__(0, after))
    else:
        monkeypatch.setattr(camera_mod.time, "sleep", lambda _s: None)
    if swap_at == "cli":
        cli = camera_mod.subprocess.run

        def run(command, **kwargs):
            if "--capture-image-and-download" in command:
                bus.views[0] = after  # the CLI opened the port after the pre-shutter check
            return cli(command, **kwargs)
        monkeypatch.setattr(camera_mod.subprocess, "run", run)


def test_refusal_before_the_first_meter_shutter_is_not_reported_as_may_have_fired(product, bus):
    """The real engine path (_brain_shoot + the CLI capture), refused at the pre-shutter check."""
    product.fake.name = "real"
    _real_shutter_path(product.monkeypatch, bus, swap_at="settle")
    response = product.client.post("/shoot", headers=product.headers, json={"intent": "x", **_selection()})
    assert response.status_code == 409, response.text
    body = response.json()
    assert body["may_have_fired"] is False and body["reason"] == "serial_mismatch"
    assert not any("--capture-image-and-download" in command for command in bus.commands)


def test_session_frame_refused_before_its_shutter_stays_pending(product, bus):
    """Real product_sessions.run + SessionRunner + Camera CLI path: frame 1 is refused."""
    product.fake.name = "real"
    created = product.client.post("/sessions", headers=product.headers,
                                  json={"mode": "hdr", "frames": 3, **_selection()}).json()
    _real_shutter_path(product.monkeypatch, bus, swap_at="settle")
    response = product.client.post(f"/sessions/{created['id']}/run", headers=product.headers)
    state = product_sessions.runner().read(created["id"])
    assert state["status"] == "identity_refused", (response.status_code, response.text, state["status"])
    assert [frame["status"] for frame in state["frames"]] == ["pending"] * 3
    refusal = state["identity_refusal"]
    assert refusal["reason"] == "serial_mismatch" and refusal["may_have_fired"] is False
    assert (refusal["expected_serial"], refusal["observed_serial"]) == (SERIAL_A, SERIAL_B)
    assert not any("--capture-image-and-download" in command for command in bus.commands)


def test_background_job_keeps_the_structured_refusal(product, bus):
    import time as clock
    product.fake.name = "real"
    created = product.client.post("/sessions", headers=product.headers,
                                  json={"mode": "hdr", "frames": 3, **_selection()}).json()
    bus.views = [SWAPPED]  # refused at connect
    path = f"/sessions/{created['id']}/jobs"
    assert product.client.post(path, headers=product.headers, json={}).status_code == 202
    deadline = clock.monotonic() + 10
    job = product.client.get(path, headers=product.headers).json()
    while job["status"] in ("queued", "running") and clock.monotonic() < deadline:
        clock.sleep(0.02)
        job = product.client.get(path, headers=product.headers).json()
    assert job["status"] == "identity_refused", job
    assert job["identity_refusal"]["reason"] == "serial_mismatch"
    assert job["identity_refusal"]["may_have_fired"] is False and job["retry_safe"] is True
    assert job["session"]["status"] == "identity_refused"
    assert _nothing_fired(bus)


def test_handheld_keeps_the_structured_refusal(bus, tmp_path):
    from handheld import HandheldSession
    camera = Camera(model=A7III, port=PORT_1, serial=SERIAL_A)
    camera.connect()

    def listen(*args, **kwargs):
        bus.views[0] = SWAPPED
        camera.disconnect()
        camera.connect()  # the body re-enumerated under us: the reconnect refuses it
    camera.listen_for_files = listen
    camera.drain_events = lambda max_ms=500: 0
    session = HandheldSession(lambda: camera, lambda *a: {}, max_frames=2, max_duration_s=5, out_dir=tmp_path)
    with pytest.raises(CameraError):
        session.run()
    state = session.state()
    assert state["status"] == "identity_refused"
    assert state["identity_refusal"]["reason"] == "serial_mismatch"
    assert state["identity_refusal"]["may_have_fired"] is False


def test_identity_refusals_are_terminal_in_every_retry_loop(bus, tmp_path):
    from handheld import HandheldSession
    refusal = camera_mod.CameraIdentityError("fixture", reason="serial_mismatch")
    # 1. Camera's post-capture reconnect with backoff.
    camera = Camera(model=A7III, port=PORT_1, serial=SERIAL_A)
    attempts = []
    camera.connect = lambda: attempts.append(1) or (_ for _ in ()).throw(refusal)
    with pytest.raises(CameraError) as raised:
        camera._reconnect_with_backoff()
    assert raised.value is refusal and attempts == [1]
    # 2. Session restoration's reconnect.
    connects = []
    dropped = SimpleNamespace(get_settings=lambda: (_ for _ in ()).throw(CameraError("Not connected")),
                              connect=lambda: connects.append(1) or (_ for _ in ()).throw(refusal))
    with pytest.raises(CameraError) as raised:
        product_sessions._ensure_connected(dropped, attempts=(0.0, 0.0, 0.0, 0.0))
    assert raised.value is refusal and connects == [1]
    # 3. The handheld exposure setter.
    writes = []
    body = SimpleNamespace(set_exposure=lambda **kw: writes.append(kw) or (_ for _ in ()).throw(refusal))
    session = HandheldSession(lambda: body, lambda *a: {}, max_frames=1, max_duration_s=5, out_dir=tmp_path)
    session.APPLY_SETTLE_DELAYS_S = (0.0, 0.0, 0.0)
    with pytest.raises(CameraError) as raised:
        session._apply_with_settle(body, {"iso": "100"})
    assert raised.value is refusal and len(writes) == 1


# --- round 2: nothing fires on an unverified connection -----------------------------------

def test_capture_and_movie_refuse_an_unverified_connection(bus, monkeypatch):
    monkeypatch.setattr(Camera, "capture", REAL_CAPTURE)
    unverified = Camera(model=A7III, port=PORT_1)
    unverified.connect()
    with pytest.raises(CameraError) as refused:
        unverified.capture(download=True)
    assert getattr(refused.value, "reason", None) == "serial_unverified"
    monkeypatch.setattr(camera_mod, "assert_recording_allowed", lambda: None)
    monkeypatch.setattr(camera_mod, "quarantine_recording_stop",
                        lambda *a, **k: pytest.fail("an identity refusal must not quarantine or toggle"))
    movie = Camera(model="Sony ILCE-7M2 (Control)")
    monkeypatch.setattr(movie, "_press_movie_button", lambda: pytest.fail("movie button pressed"), raising=False)
    for action in (movie.start_movie, lambda: REAL_RECORD(movie, 1.0)):
        with pytest.raises(CameraError) as refused:
            action()
        assert getattr(refused.value, "reason", None) == "serial_unverified"
    assert bus.captures == [] and not bus.commands


def test_dev_shutter_tools_require_a_serial(monkeypatch, tmp_path):
    import importlib.util
    sys.path.insert(0, str(ROOT / "tools"))

    def load(name, relative):
        spec = importlib.util.spec_from_file_location(name, ROOT / relative)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module
    smoke = load("smoke_test_fixture", "smoke_test.py")
    with pytest.raises(SystemExit):
        smoke.main([])  # --serial is required
    import m0_capture
    with pytest.raises((TypeError, CameraError)):
        m0_capture.FastCapture()
    probe = load("probe_fixture", "tools/probe_sony_long_capture.py")
    monkeypatch.setattr(probe.subprocess, "run", lambda *a, **k: pytest.fail("probe ran the CLI"))
    receipt, code = probe.run_probe(SimpleNamespace(model="Sony ILCE-7M2 (Control)", port="usb:000,003",
                                                    shutter="30", aperture="f/5.6", execute=True, out=str(tmp_path)))
    assert code == 2 and receipt["status"] == "serial_required_no_trigger"
    movie = load("movie_proof_fixture", "tools/a7ii_usb_movie_record_proof.py")
    monkeypatch.delenv("CAMERA_SERIAL", raising=False)
    payload = movie.build_payload(movie.parse_args(["--execute", "--allow-camera-record", "--out-dir", str(tmp_path)]))
    assert payload["verdict"] == "guarded_missing_serial"


# --- round 2: a verified rebind, an atomic rebind, and a post-shutter re-check -------------

def test_rebind_refuses_a_serial_the_os_does_not_report_at_that_port(product, bus):
    product.fake.name = "real"
    legacy = _legacy_session()
    for serial, port in (("NOT-ATTACHED", "usb:999,999"), (SERIAL_B, PORT_1)):
        response = product.client.post(f"/sessions/{legacy['id']}/camera", headers=product.headers,
                                       json={"camera_model": A7III, "camera_port": port, "camera_serial": serial})
        assert response.status_code == 409, response.text
        assert response.json()["reason"] in ("not_present", "serial_mismatch")
    # A failed bind changes nothing.
    assert product_sessions.runner().read(legacy["id"])["metadata"] == {"camera_model": A7III, "camera_port": PORT_1}


def test_rebind_checks_the_job_under_the_session_operation_lock(product, bus, monkeypatch):
    """A job submitted right after the job check (as SessionJobs._save does: its receipt is
    written under the session operation lock) must not slip in before the bind is saved."""
    product.fake.name = "real"
    legacy = _legacy_session()
    sessions = product_sessions.runner()
    real_check, submitted = product_sessions._job_active, []

    def check_then_concurrent_submit(runner, session_id):
        active = real_check(runner, session_id)
        try:
            with sessions.operation(session_id):
                (sessions._directory(session_id) / "job.json").write_text(
                    json.dumps({"id": session_id, "status": "queued"}))
                submitted.append(True)
        except RuntimeError:
            pass  # the rebind holds the lock: the submission is refused
        return active
    monkeypatch.setattr(product_sessions, "_job_active", check_then_concurrent_submit)
    try:
        product_sessions.reselect_camera(legacy["id"], A7III, PORT_1, SERIAL_A)
        bound = True
    except RuntimeError:
        bound = False
    assert not (bound and submitted), "a job was queued between the check and the bind"
    assert submitted == [] and bound


def test_a_serial_that_changes_around_the_cli_shutter_is_never_a_success(bus, monkeypatch, tmp_path):
    _real_shutter_path(monkeypatch, bus, swap_at="cli")
    camera = Camera(model=A7III, port=PORT_1, serial=SERIAL_A)
    camera.connect()
    with pytest.raises(camera_mod.CaptureOutcomeUncertain) as uncertain:
        camera.capture(dest_dir=tmp_path)
    assert uncertain.value.retry_safe is False
    receipt = camera.last_capture_receipt
    assert receipt["pre_shutter_serial_verification"]["serial"] == SERIAL_A
    assert receipt["post_shutter_identity_refusal"]["observed_serial"] == SERIAL_B
    assert receipt.get("path") is None and not any(tmp_path.glob("*.JPG"))


# --- round 3: an unproven-but-downloaded photograph is never deleted ------------------------

GONE = [_ioreg(SERIAL_B, 4)]  # body A dropped off USB after its shutter


def test_a_download_whose_body_cannot_be_proven_afterwards_is_quarantined_not_deleted(product, bus):
    product.fake.name = "real"
    _real_shutter_path(product.monkeypatch, bus, swap_at="cli", after=GONE)
    response = product.client.post("/shoot", headers=product.headers, json={"intent": "x", **_selection()})
    assert response.status_code == 409, response.text
    body = response.json()
    assert body["may_have_fired"] is True and body["retry_capture"] is False
    events = list((product.root / "quarantine").iterdir())
    assert len(events) == 1
    kept = events[0] / "DSC00001.JPG"
    assert kept.read_bytes() == b"fixture JPEG" and body["capture_path"] == str(kept)
    receipt = json.loads((events[0] / "receipt.json").read_text())
    assert receipt["status"] == "body_unproven" and receipt["published"] is False
    assert receipt["pre_shutter_serial"] == SERIAL_A
    assert receipt["post_shutter"]["reason"] == "not_present" and receipt["post_shutter"]["observed_serial"] is None
    assert oct((product.root / "quarantine").stat().st_mode & 0o777) == "0o700"
    assert oct(kept.stat().st_mode & 0o777) == "0o600"
    # Nothing is published: no gallery record, no top-level image.
    assert product.client.get("/gallery", headers=product.headers).json() == []
    assert not [name for name in product.root.iterdir() if name.suffix.lower() in (".jpg", ".json")]


def test_delete_all_and_retention_remove_quarantined_downloads(product, bus):
    from photo_lifecycle import PhotoLifecycle, DELETE_ALL_CONFIRMATION
    product.fake.name = "real"
    _real_shutter_path(product.monkeypatch, bus, swap_at="cli", after=GONE)
    for _ in range(2):
        bus.views = [SELECTED]
        product.client.post("/shoot", headers=product.headers, json={"intent": "x", **_selection()})
    events = sorted((product.root / "quarantine").iterdir())
    assert len(events) == 2
    lifecycle = PhotoLifecycle(product.root)
    # Retention follows the receipt's quarantined_at: only the older event expires.
    receipt = events[0] / "receipt.json"
    record = json.loads(receipt.read_text())
    record["quarantined_at"] -= 3 * 86400
    receipt.write_text(json.dumps(record))
    _age(events[0], 3)
    lifecycle.set_retention(2)
    lifecycle.enforce_retention()
    assert not events[0].exists() and (events[1] / "DSC00001.JPG").exists()
    lifecycle.delete_all(confirm=DELETE_ALL_CONFIRMATION)
    assert not events[1].exists() and not list((product.root).rglob("DSC00001.JPG"))


def test_a_refused_restore_after_an_accepted_frame_keeps_the_structured_refusal(tmp_path):
    from stack_session import SessionRunner
    runner = SessionRunner(tmp_path / "sessions")
    state = runner.create("timelapse", 2, metadata={"camera_model": A7III, "camera_port": PORT_1,
                                                    "camera_serial": SERIAL_A})

    def capture(index, spec, destination):
        Image.new("RGB", (8, 8), "gray").save(destination, format="JPEG")
        return destination

    def restore():
        raise camera_mod.CameraIdentityError("fixture", reason="serial_mismatch", expected_serial=SERIAL_A,
                                             observed_serial=SERIAL_B)
    result = runner.run(state["id"], capture, restore_settings=restore)
    assert [f["status"] for f in result["frames"]] == ["accepted"] * 2 and result["restore"] == "failed"
    refusal = result["restore_identity_refusal"]
    assert (refusal["reason"], refusal["observed_serial"], refusal["may_have_fired"]) == ("serial_mismatch", SERIAL_B, False)


# --- round 4: stage in the quarantine tree; never a temp dir, never a fallback -------------

import errno  # noqa: E402
import os  # noqa: E402
import time as _clock  # noqa: E402


class SimulatedCrash(Exception):
    """Stands in for a process dying at an exact point; no real process is killed."""


def _age(path: Path, days: float) -> None:
    stamp = _clock.time() - days * 86400
    os.utime(path, (stamp, stamp), follow_symlinks=False)


def _connected(bus, monkeypatch, store: Path, *, files=(("DSC00001.JPG", b"fixture JPEG"),), after=None,
               returncode=0):
    """A verified A7 III whose fake CLI downloads `files`; `after` is the OS view once it returns."""
    _real_shutter_path(monkeypatch, bus)
    cli = camera_mod.subprocess.run

    def run(command, **kwargs):
        if "--capture-image-and-download" not in command:
            return cli(command, **kwargs)
        bus.captures.append(("gphoto2 CLI", command))
        for name, data in files:
            (Path(kwargs["cwd"]) / name).write_bytes(data)
        if after is not None:
            bus.views[0] = after
        return SimpleNamespace(returncode=returncode, stdout="", stderr="fixture failure" if returncode else "")
    monkeypatch.setattr(camera_mod.subprocess, "run", run)
    camera = Camera(model=A7III, port=PORT_1, serial=SERIAL_A)
    camera.connect()
    camera.quarantine_dir = store / "quarantine"
    return camera


def _everything_under(root: Path) -> dict:
    return {str(p.relative_to(root)): p.read_bytes() for p in root.rglob("*") if p.is_file() and not p.is_symlink()}


def test_disk_full_while_quarantining_loses_no_bytes_and_writes_nothing_outside(bus, monkeypatch, tmp_path):
    store, captures = tmp_path / "store", tmp_path / "captures"
    camera = _connected(bus, monkeypatch, store, after=GONE,
                        files=(("A.ARW", b"raw bytes"), ("B.JPG", b"jpeg bytes")))
    real_move, real_open, moved = camera_mod.shutil.move, camera_mod.os.open, []

    def move(source, target):  # the disk fills after the first file (the old move-based quarantine)
        moved.append(source)
        if len(moved) > 1:
            raise OSError(errno.ENOSPC, "No space left on device")
        return real_move(source, target)

    def open_(path, flags, *args, **kwargs):  # ... and no receipt can be written either
        if str(path).endswith("receipt.json"):
            raise OSError(errno.ENOSPC, "No space left on device")
        return real_open(path, flags, *args, **kwargs)
    monkeypatch.setattr(camera_mod.shutil, "move", move)
    monkeypatch.setattr(camera_mod.os, "open", open_)
    with pytest.raises(camera_mod.CaptureOutcomeUncertain):
        camera.capture(dest_dir=captures)
    kept = _everything_under(store / "quarantine")
    assert sorted(kept.values()) == [b"jpeg bytes", b"raw bytes"], kept
    assert all(Path(name).parent.name.startswith("staging-") for name in kept)
    assert not any(p.is_file() for p in captures.rglob("*")), "a failure path wrote outside quarantine/"


@pytest.mark.parametrize("failure", ["cli_error", "no_jpeg"])
def test_other_failure_paths_keep_bytes_in_quarantine_only(bus, monkeypatch, tmp_path, failure):
    store, captures = tmp_path / "store", tmp_path / "captures"
    files = (("DSC00001.ARW", b"raw"),) if failure == "no_jpeg" else (("DSC00001.JPG", b"jpeg"),)
    camera = _connected(bus, monkeypatch, store, files=files, returncode=1 if failure == "cli_error" else 0)
    with pytest.raises(camera_mod.CaptureOutcomeUncertain):
        camera.capture(dest_dir=captures)
    assert sorted(_everything_under(store / "quarantine").values()) == [files[0][1]]
    assert not any(p.is_file() for p in captures.rglob("*"))


def test_nothing_under_quarantine_is_served(product, bus):
    product.fake.name = "real"
    _real_shutter_path(product.monkeypatch, bus, swap_at="cli", after=GONE)
    product.client.post("/shoot", headers=product.headers, json={"intent": "x", **_selection()})
    event = next((product.root / "quarantine").iterdir())
    assert (event / "DSC00001.JPG").is_file()
    for name in ("DSC00001.JPG", f"quarantine/{event.name}/DSC00001.JPG",
                 f"quarantine%2F{event.name}%2FDSC00001.JPG"):
        assert product.client.get(f"/shots/{name}", headers=product.headers).status_code != 200
        assert product.client.get(f"/thumbnails/{name}", headers=product.headers).status_code != 200
    assert product.client.get("/gallery", headers=product.headers).json() == []


def test_refusals_before_the_shutter_leave_no_staging_directory(product, bus):
    product.fake.name = "real"
    _real_shutter_path(product.monkeypatch, bus, swap_at="settle")
    for _ in range(3):
        bus.views = [SELECTED]
        response = product.client.post("/shoot", headers=product.headers, json={"intent": "x", **_selection()})
        assert response.status_code == 409 and response.json()["may_have_fired"] is False
    quarantine = product.root / "quarantine"
    assert not quarantine.exists() or not [p for p in quarantine.iterdir() if p.name.startswith("staging-")]


def test_staging_refuses_a_symlink_anywhere_from_the_store_root_down(bus, monkeypatch, tmp_path):
    outside = tmp_path / "outside"
    outside.mkdir()
    for layout in ("quarantine", "store"):
        store = tmp_path / f"store-{layout}"
        if layout == "quarantine":
            store.mkdir()
            (store / "quarantine").symlink_to(outside, target_is_directory=True)
        else:
            store.symlink_to(outside, target_is_directory=True)
        bus.captures.clear()
        camera = _connected(bus, monkeypatch, store)
        with pytest.raises(CameraError) as refused:
            camera.capture(dest_dir=tmp_path / "captures")
        assert not isinstance(refused.value, camera_mod.CaptureOutcomeUncertain)  # no shutter was issued
        assert bus.captures == [] and list(outside.iterdir()) == []


def test_a_proven_capture_moves_by_rename_and_leaves_no_staging(bus, monkeypatch, tmp_path):
    store = tmp_path / "store"
    camera = _connected(bus, monkeypatch, store)
    real_replace, replaced = camera_mod.os.replace, []
    monkeypatch.setattr(camera_mod.os, "replace", lambda source, target, *a, **k: (
        replaced.append((str(source), str(target))), real_replace(source, target, *a, **k))[1])
    delivered = camera.capture(dest_dir=store)
    assert [(Path(source).parent.name.startswith("staging-"), target) for source, target in replaced] == \
        [(True, str(delivered))], "the same-volume delivery must be one os.replace out of staging"
    assert delivered.read_bytes() == b"fixture JPEG" and oct(delivered.stat().st_mode & 0o777) == "0o600"
    assert list((store / "quarantine").iterdir()) == []


def _cross_volume(monkeypatch, events=None, *, fail=None):
    """os.replace out of staging into other-volume raises EXDEV. `events` records the delivery's
    fsync/replace/rename/unlink order; `fail` injects an error at one step."""
    import stat as stat_mod
    real = {name: getattr(camera_mod.os, name) for name in ("replace", "rename", "unlink", "fsync")}
    real_copy = camera_mod.shutil.copyfileobj

    def note(event):
        if events is not None:
            events.append(event)

    def replace(source, target, *args, **kwargs):
        if "staging-" in str(source) and "other-volume" in str(target):
            raise OSError(errno.EXDEV, "Invalid cross-device link")
        if str(source).endswith(".part"):
            note("rename")
            if fail == "rename":
                raise OSError(errno.EIO, "Input/output error")
        return real["replace"](source, target, *args, **kwargs)

    def rename(source, target, *args, **kwargs):
        note("rename")
        return real["rename"](source, target, *args, **kwargs)

    def unlink(target, *args, **kwargs):
        if "staging-" in str(target) and str(target).endswith(".JPG"):
            note("unlink source")
        return real["unlink"](target, *args, **kwargs)

    def fsync(descriptor):
        kind = "dir fsync" if stat_mod.S_ISDIR(os.fstat(descriptor).st_mode) else "file fsync"
        note(kind)
        if fail == kind:
            raise OSError(errno.EIO, "Input/output error")
        return real["fsync"](descriptor)

    def copy(source, target, *args, **kwargs):
        if fail == "copy":
            raise OSError(errno.ENOSPC, "No space left on device")
        return real_copy(source, target, *args, **kwargs)
    monkeypatch.setattr(camera_mod.os, "replace", replace)
    monkeypatch.setattr(camera_mod.os, "rename", rename)
    monkeypatch.setattr(camera_mod.os, "unlink", unlink)
    monkeypatch.setattr(camera_mod.os, "fsync", fsync)
    monkeypatch.setattr(camera_mod.shutil, "copyfileobj", copy)


def test_a_cross_volume_delivery_copies_durably_before_removing_the_source(bus, monkeypatch, tmp_path):
    store, captures = tmp_path / "store", tmp_path / "other-volume"
    camera = _connected(bus, monkeypatch, store)
    events = []
    _cross_volume(monkeypatch, events)
    delivered = camera.capture(dest_dir=captures)
    assert delivered.read_bytes() == b"fixture JPEG"
    assert events == ["file fsync", "rename", "dir fsync", "unlink source"], events
    assert list((store / "quarantine").iterdir()) == []
    assert not list(captures.glob(".*.part"))


def test_a_failed_directory_fsync_keeps_the_staged_jpeg(bus, monkeypatch, tmp_path):
    store, captures = tmp_path / "store", tmp_path / "other-volume"
    camera = _connected(bus, monkeypatch, store)
    _cross_volume(monkeypatch, fail="dir fsync")
    delivered = camera.capture(dest_dir=captures)
    assert delivered.read_bytes() == b"fixture JPEG"
    staged = list((store / "quarantine").glob("staging-*/DSC00001.JPG"))
    assert [p.read_bytes() for p in staged] == [b"fixture JPEG"], "the staged JPEG was removed"
    assert camera.last_capture_receipt["staged_duplicate"] == str(staged[0])
    assert camera.last_capture_receipt["delivery"]["source_removed"] is False


@pytest.mark.parametrize("fail", ["copy", "file fsync", "rename"])
def test_a_failed_delivery_leaves_nothing_at_the_destination(bus, monkeypatch, tmp_path, fail):
    store, captures = tmp_path / "store", tmp_path / "other-volume"
    camera = _connected(bus, monkeypatch, store)
    _cross_volume(monkeypatch, fail=fail)
    with pytest.raises(camera_mod.CaptureOutcomeUncertain):
        camera.capture(dest_dir=captures)
    assert [p.name for p in captures.iterdir()] == [], "an empty (or partial) file was left under the name"
    staged = list((store / "quarantine").glob("staging-*/DSC00001.JPG"))
    assert [p.read_bytes() for p in staged] == [b"fixture JPEG"]


def test_cli_capture_records_size_and_sha256_of_every_downloaded_file(bus, monkeypatch, tmp_path):
    """The board proof's RAW evidence (deploy/raw_proof.py): each file the camera delivered is
    hashed from the bytes as stored, before the ARW is discarded; only the JPEG is delivered."""
    import hashlib
    store, captures = tmp_path / "store", tmp_path / "captures"
    raw, jpeg = b"fixture ARW bytes" * 3, b"fixture JPEG"
    camera = _connected(bus, monkeypatch, store, files=(("DSC00001.ARW", raw), ("DSC00001.JPG", jpeg)))
    delivered = camera.capture(dest_dir=captures)
    assert delivered.read_bytes() == jpeg
    assert camera.last_capture_receipt["downloaded_files"] == [
        {"name": "DSC00001.ARW", "size": len(raw), "sha256": hashlib.sha256(raw).hexdigest(), "retained": False},
        {"name": "DSC00001.JPG", "size": len(jpeg), "sha256": hashlib.sha256(jpeg).hexdigest(), "retained": True}]
    assert camera.last_capture_receipt["timings"]["download_hash_s"] >= 0
    # Delivery is unchanged: the ARW is still discarded and nothing stays in quarantine.
    assert not [p for p in (store / "quarantine").rglob("*") if p.is_file()]
    assert [p.name for p in captures.iterdir()] == [delivered.name]


@pytest.mark.parametrize("stage", ["open", "read", "fstat"])
def test_a_hashing_failure_is_recorded_and_never_costs_the_photo(bus, monkeypatch, tmp_path, stage):
    """Evidence only: an unreadable ARW leaves no hash (the proof then fails RAW) but the one
    shutter's JPEG is delivered exactly as before and nothing is retried."""
    store, captures = tmp_path / "store", tmp_path / "captures"
    camera = _connected(bus, monkeypatch, store, files=(("DSC00001.ARW", b"raw"), ("DSC00001.JPG", b"fixture JPEG")))
    real_open, real_digest, real_fstat = camera_mod.os.open, camera_mod.hashlib.file_digest, camera_mod.os.fstat
    failing = {"fd": None}

    def open_(path, flags, *args, **kwargs):
        if str(path).endswith(".ARW") and stage == "open":
            raise OSError(errno.EIO, "fixture I/O error")
        fd = real_open(path, flags, *args, **kwargs)
        failing["fd"] = fd if str(path).endswith(".ARW") else None  # descriptor numbers are reused
        return fd

    def digest(stream, name):
        if stage == "read" and stream.fileno() == failing["fd"]:
            raise OSError(errno.EIO, "fixture read error")
        return real_digest(stream, name)

    def fstat(fd):
        if stage == "fstat" and fd == failing["fd"]:
            raise OSError(errno.EIO, "fixture fstat error")
        return real_fstat(fd)
    monkeypatch.setattr(camera_mod.os, "open", open_)
    monkeypatch.setattr(camera_mod.hashlib, "file_digest", digest)
    monkeypatch.setattr(camera_mod.os, "fstat", fstat)
    delivered = camera.capture(dest_dir=captures)
    assert delivered.read_bytes() == b"fixture JPEG"
    assert [c[0] for c in bus.captures] == ["gphoto2 CLI"]
    arw, jpg = camera.last_capture_receipt["downloaded_files"]
    assert arw["name"] == "DSC00001.ARW" and arw["sha256"] is None and arw["size"] is None
    assert arw["error"].startswith("OSError: ") and "fixture" in arw["error"]
    assert jpg["sha256"] is not None and jpg["retained"] is True


def test_nothing_is_hashed_when_the_body_cannot_be_proven_after_the_shutter(bus, monkeypatch, tmp_path):
    store = tmp_path / "store"
    camera = _connected(bus, monkeypatch, store, after=GONE,
                        files=(("DSC00001.ARW", b"raw"), ("DSC00001.JPG", b"fixture JPEG")))
    monkeypatch.setattr(camera_mod, "_download_record", lambda path: pytest.fail("hashed before identity"))
    with pytest.raises(camera_mod.CaptureFromUnprovenBody):
        camera.capture(dest_dir=tmp_path / "captures")
    assert "downloaded_files" not in camera.last_capture_receipt


def test_a_crash_after_a_same_volume_rename_leaves_at_most_an_empty_staging_dir(bus, monkeypatch, tmp_path):
    store = tmp_path / "store"
    camera = _connected(bus, monkeypatch, store, files=(("DSC00001.ARW", b"raw"), ("DSC00001.JPG", b"fixture JPEG")))
    monkeypatch.setattr(camera_mod, "_remove_empty_staging", lambda staging: (_ for _ in ()).throw(SimulatedCrash()))
    with pytest.raises(SimulatedCrash):
        camera.capture(dest_dir=store)
    assert [p.read_bytes() for p in store.glob("*DSC00001.JPG")] == [b"fixture JPEG"]
    leftovers = list((store / "quarantine").iterdir())
    assert len(leftovers) == 1 and leftovers[0].name.startswith("staging-") and list(leftovers[0].iterdir()) == []


def test_a_crash_between_cross_volume_copy_and_unlink_leaves_a_safe_duplicate(product, bus, monkeypatch, tmp_path):
    """Nothing is lost: the delivered photo is complete; the staged duplicate is never served,
    delete-all removes it and retention expires it."""
    from photo_lifecycle import PhotoLifecycle, DELETE_ALL_CONFIRMATION
    store, captures = product.root, tmp_path / "other-volume"
    real_replace, real_unlink = camera_mod.os.replace, camera_mod.os.unlink

    def replace(source, target, *args, **kwargs):
        if "staging-" in str(source) and "other-volume" in str(target):
            raise OSError(errno.EXDEV, "Invalid cross-device link")
        return real_replace(source, target, *args, **kwargs)

    def unlink(target, *args, **kwargs):
        if "staging-" in str(target) and str(target).endswith("DSC00001.JPG"):
            raise SimulatedCrash()  # after the durable rename, before the source is removed
        return real_unlink(target, *args, **kwargs)
    monkeypatch.setattr(camera_mod.os, "replace", replace)
    monkeypatch.setattr(camera_mod.os, "unlink", unlink)
    stagings = []
    for attempt in range(2):
        bus.views = [SELECTED]
        camera = _connected(bus, monkeypatch, store)
        with pytest.raises(SimulatedCrash):
            camera.capture(dest_dir=captures / str(attempt))
        assert [p.read_bytes() for p in (captures / str(attempt)).glob("*DSC00001.JPG")] == [b"fixture JPEG"]
        stagings = sorted((store / "quarantine").iterdir())
    monkeypatch.setattr(camera_mod.os, "unlink", real_unlink)
    assert len(stagings) == 2 and all((s / "DSC00001.JPG").read_bytes() == b"fixture JPEG" for s in stagings)
    for name in ("DSC00001.JPG", f"quarantine/{stagings[0].name}/DSC00001.JPG"):
        assert product.client.get(f"/shots/{name}", headers=product.headers).status_code != 200
        assert product.client.get(f"/thumbnails/{name}", headers=product.headers).status_code != 200
    lifecycle = PhotoLifecycle(store)
    lifecycle.set_retention(1)
    _age(stagings[0], 2)
    lifecycle.enforce_retention()
    assert not stagings[0].exists() and stagings[1].exists()
    lifecycle.delete_all(confirm=DELETE_ALL_CONFIRMATION)
    assert not stagings[1].exists()


# --- round 4: retention never aborts; containment never follows a link -------------------

def test_retention_survives_hostile_receipts_and_never_expires_a_new_capture(tmp_path):
    from photo_lifecycle import PhotoLifecycle
    root = tmp_path / "shots"
    lifecycle = PhotoLifecycle(root)
    quarantine = root / "quarantine"

    def event(name, receipt_text, *, age_days):
        directory = quarantine / name
        directory.mkdir(parents=True)
        (directory / "DSC00001.JPG").write_bytes(b"jpeg")
        (directory / "receipt.json").write_text(receipt_text)
        if age_days:
            _age(directory, age_days)
        return directory
    overflow = event("unproven_overflow", '{"quarantined_at": 1' + "0" * 1000 + "}", age_days=5)
    nested = event("unproven_nested", "[" * 2000 + "]" * 2000, age_days=5)
    fresh_zero = event("unproven_zero", '{"quarantined_at": 0}', age_days=0)
    old = event("unproven_old", json.dumps({"quarantined_at": _clock.time() - 5 * 86400}), age_days=5)
    lifecycle.set_retention(1)
    result = lifecycle.enforce_retention()  # must not raise
    records = {item["record"] for item in result["errors"]}
    assert f"quarantine/{overflow.name}/receipt.json" in records and f"quarantine/{nested.name}/receipt.json" in records
    assert fresh_zero.exists(), "quarantined_at=0 expired a brand-new capture"
    assert not old.exists()


def test_delete_all_and_retention_never_follow_links_out_of_the_quarantine(tmp_path):
    from photo_lifecycle import PhotoLifecycle, DELETE_ALL_CONFIRMATION
    outside = tmp_path / "outside"
    outside.mkdir()
    sentinel = outside / "sentinel.jpg"
    sentinel.write_bytes(b"keep me")
    for action in ("retention", "delete_all"):
        root = tmp_path / f"shots-{action}"
        lifecycle = PhotoLifecycle(root)
        event = root / "quarantine" / "unproven_links"
        event.mkdir(parents=True)
        (event / "receipt.json").write_text(json.dumps({"quarantined_at": _clock.time() - 5 * 86400}))
        (event / "symlink.jpg").symlink_to(sentinel)
        os.link(sentinel, event / "hardlink.jpg")
        (event / "linked-dir").symlink_to(outside, target_is_directory=True)
        (root / "quarantine" / "staging-link").symlink_to(outside, target_is_directory=True)
        for link in (event / "symlink.jpg", event / "linked-dir", root / "quarantine" / "staging-link", event):
            _age(link, 5)
        if action == "retention":
            lifecycle.set_retention(1)
            lifecycle.enforce_retention()
        else:
            lifecycle.delete_all(confirm=DELETE_ALL_CONFIRMATION)
        assert not event.exists() and not (root / "quarantine" / "staging-link").is_symlink(), action
        assert sentinel.read_bytes() == b"keep me" and sorted(p.name for p in outside.iterdir()) == ["sentinel.jpg"]


# --- round 5: serial follow-ups (same-serial port rebind, durability warning, 0600 before move) --

def _bound_session(product):
    """A session bound to SERIAL_A at PORT_1, created through the API."""
    created = product.client.post("/sessions", headers=product.headers,
                                  json={"mode": "hdr", "frames": 3, **_selection()})
    assert created.status_code == 200, created.text
    return created.json()


def _manifest(session_id):
    return (product_sessions.runner()._directory(session_id) / "manifest.json").read_bytes()


def test_a_bound_session_moves_to_the_port_its_serial_re_enumerated_to_and_resumes(product, bus):
    product.fake.name = "real"
    session = _bound_session(product)
    bus.views = [SWAPPED]  # body A re-enumerated to PORT_2
    stuck = product.client.post(f"/sessions/{session['id']}/run", headers=product.headers)
    assert stuck.status_code == 409 and stuck.json()["reason"] == "serial_mismatch"
    moved = product.client.post(f"/sessions/{session['id']}/camera", headers=product.headers,
                                json=_selection(port=PORT_2))
    assert moved.status_code == 200, moved.text
    metadata = moved.json()["metadata"]
    assert (metadata["camera_model"], metadata["camera_port"], metadata["camera_serial"]) == (A7III, PORT_2, SERIAL_A)
    assert moved.json()["camera_reselections"][-1]["previous_port"] == PORT_1
    resumed = product.client.post(f"/sessions/{session['id']}/resume", headers=product.headers)
    assert resumed.status_code == 200, resumed.text
    bus.claims.clear()
    _real_shutter_path(product.monkeypatch, bus)  # live ladders + readback; the fake CLI shutter
    product.client.post(f"/sessions/{session['id']}/run", headers=product.headers)
    assert bus.claims and set(bus.claims) == {PORT_2}, bus.claims
    shutters = [command for kind, command in bus.captures if kind == "gphoto2 CLI"]
    assert shutters and all(PORT_2 in command for command in shutters), bus.captures
    assert product_sessions.runner().read(session["id"])["status"] != "identity_refused"


def test_a_bound_session_never_changes_its_serial_or_model(product, bus):
    product.fake.name = "real"
    session = _bound_session(product)
    before = _manifest(session["id"])
    bus.views = [SWAPPED]  # B really is at PORT_1, so only the bound-serial rule can refuse it
    other = product.client.post(f"/sessions/{session['id']}/camera", headers=product.headers,
                                json=_selection(serial=SERIAL_B, port=PORT_1))
    assert other.status_code == 400, other.text
    model = product.client.post(f"/sessions/{session['id']}/camera", headers=product.headers,
                                json={**_selection(port=PORT_2), "camera_model": "Sony Alpha-A7 II (PC Control)"})
    assert model.status_code == 400, model.text
    assert _manifest(session["id"]) == before


def test_a_port_rebind_that_shows_another_serial_is_refused_and_changes_nothing(product, bus):
    product.fake.name = "real"
    session = _bound_session(product)
    before = _manifest(session["id"])
    bus.views = [SELECTED]  # PORT_2 holds B, not the bound A
    response = product.client.post(f"/sessions/{session['id']}/camera", headers=product.headers,
                                   json=_selection(port=PORT_2))
    assert response.status_code == 409, response.text
    body = response.json()
    assert body["reason"] == "serial_mismatch"
    assert (body["expected_serial"], body["observed_serial"]) == (SERIAL_A, SERIAL_B)
    assert _manifest(session["id"]) == before
    assert _nothing_fired(bus) and bus.claims == []


def _resume_click(metadata, camera):
    """Run the page's own Resume handler (index.html) in node against fakes; return its requests."""
    handler = re.search(r"  \$\('session-resume'\)\.onclick=async\(\)=>\{.*?\n  \};\n", INDEX, re.S).group(0)
    script = ("const sent=[]; let activeSession='fixture', busy=false;\n"
              f"const selectedSessionState={{publication:{{status:'pending'}},metadata:{json.dumps(metadata)}}};\n"
              f"const availableCameras=[{json.dumps(camera)}];\n"
              "const elements={'camera-select':{value:'0'},'session-resume':{}};\n"
              "const $=id=>elements[id] || (elements[id]={});\n"
              "const confirm=()=>true, sessionBusy=()=>{}, showToast=message=>sent.push({toast:message});\n"
              "const sessionRequest=async(path,body)=>{sent.push({path,body});return {};};\n"
              "const runSession=async id=>sent.push({run:id});\n"
              f"{handler}\n"
              "elements['session-resume'].onclick().then(()=>process.stdout.write(JSON.stringify(sent)));")
    return json.loads(NODE_RUN(["node", "-e", script], check=True, capture_output=True, text=True).stdout)


def test_resume_rebinds_the_port_when_the_same_serial_moved():
    bound = {"camera_model": A7III, "camera_port": PORT_1, "camera_serial": SERIAL_A}
    moved = _resume_click(bound, {"model": A7III, "port": PORT_2, "serial": SERIAL_A})
    assert moved == [
        {"path": "/sessions/fixture/camera",
         "body": {"camera_model": A7III, "camera_port": PORT_2, "camera_serial": SERIAL_A}},
        {"path": "/sessions/fixture/resume", "body": {}}, {"run": "fixture"}], moved
    # Same port: nothing to rebind. Another serial: never a rebind request from the page.
    for camera in ({"model": A7III, "port": PORT_1, "serial": SERIAL_A},
                   {"model": A7III, "port": PORT_2, "serial": SERIAL_B}):
        assert _resume_click(bound, camera) == [{"path": "/sessions/fixture/resume", "body": {}},
                                                {"run": "fixture"}], camera


def test_night_assist_client_sends_the_selected_serial():
    workflows = (ROOT / "backend" / "web" / "workflows.js").read_text(encoding="utf-8")
    call = re.search(r"AutopilotWorkflows\.nightAssistPlan\(apiFetch,(\{[^}]*\})\)", INDEX).group(1)
    script = ("const window={};\n" + workflows + "\n"
              "const sent=[];\n"
              "const apiFetch=async(path,options)=>{sent.push({path,body:options?.body});\n"
              "  return {ok:true,json:async()=>path==='/gallery'?[{image:'shot_1.jpg'}]:{status:'ok'}};};\n"
              f"const camera={{model:{json.dumps(A7III)},port:'{PORT_1}',serial:'{SERIAL_A}'}};\n"
              f"window.AutopilotWorkflows.nightAssistPlan(apiFetch,{call})\n"
              "  .then(()=>process.stdout.write(sent[1].body));")
    body = json.loads(NODE_RUN(["node", "-e", script], check=True, capture_output=True, text=True).stdout)
    assert body == {"image": "shot_1.jpg", "camera_model": A7III, "camera_port": PORT_1, "camera_serial": SERIAL_A}


def _failing_same_volume_dir_fsync(monkeypatch, delivered_dir: Path):
    """EIO on the directory fsync after a same-volume os.replace into `delivered_dir`."""
    real = camera_mod._fsync_directory

    def fsync_directory(path):
        if Path(path) == Path(delivered_dir):
            raise OSError(errno.EIO, "Input/output error")
        return real(path)
    monkeypatch.setattr(camera_mod, "_fsync_directory", fsync_directory)


def test_a_failed_same_volume_directory_fsync_keeps_the_photo_and_says_so(bus, monkeypatch, tmp_path):
    store = tmp_path / "store"
    camera = _connected(bus, monkeypatch, store)
    _failing_same_volume_dir_fsync(monkeypatch, store)
    delivered = camera.capture(dest_dir=store)
    assert delivered.read_bytes() == b"fixture JPEG"
    receipt = camera.last_capture_receipt
    assert receipt["delivery"]["source_removed"] is True
    assert receipt["delivery_durability"]["status"] == "unconfirmed"
    assert "Input/output error" in receipt["delivery_durability"]["reason"]


def test_shoot_surfaces_an_unconfirmed_delivery_in_evidence_and_summary(product, bus):
    """The real /shoot path (_brain_shoot + the CLI capture): the same-volume directory fsync
    fails after the final JPEG is renamed into place. The shot is returned, and says so."""
    import intent_brain
    product.fake.name = "real"
    product.monkeypatch.setattr(intent_brain, "interpret", lambda intent, **kw: {
        "target_brightness": 112, "white_balance": None, "why": "fixture plan", "mood": {}})
    _real_shutter_path(product.monkeypatch, bus)
    cli = camera_mod.subprocess.run

    def run(command, **kwargs):
        if "--capture-image-and-download" in command:
            bus.captures.append(("gphoto2 CLI", command))
            Image.new("RGB", (96, 64), (118, 118, 118)).save(Path(kwargs["cwd"]) / "DSC00001.JPG", quality=90)
            return SimpleNamespace(returncode=0, stdout="", stderr="")
        return cli(command, **kwargs)
    product.monkeypatch.setattr(camera_mod.subprocess, "run", run)
    _failing_same_volume_dir_fsync(product.monkeypatch, camera_mod.CAPTURE_DIR)
    response = product.client.post("/shoot", headers=product.headers, json={"intent": "x", **_selection()})
    assert response.status_code == 200, response.text
    shot = response.json()
    assert shot["ok"] is True and shot["image"]
    durability = shot["evidence"]["delivery_durability"]
    assert durability["status"] == "unconfirmed" and "Input/output error" in durability["reason"]
    assert "not confirmed durable" in shot["summary"]


def test_the_staged_jpeg_is_0600_before_the_same_volume_rename(bus, monkeypatch, tmp_path):
    store = tmp_path / "store"
    camera = _connected(bus, monkeypatch, store)
    real_replace, modes = camera_mod.os.replace, []

    def replace(source, target, *args, **kwargs):
        if "staging-" in str(source):
            modes.append(oct(os.lstat(source).st_mode & 0o777))
        return real_replace(source, target, *args, **kwargs)
    monkeypatch.setattr(camera_mod.os, "replace", replace)
    old_umask = os.umask(0o022)  # the CLI's file is created 0644, as on a default host
    try:
        delivered = camera.capture(dest_dir=store)
    finally:
        os.umask(old_umask)
    assert modes == ["0o600"], modes
    assert oct(delivered.stat().st_mode & 0o777) == "0o600"


def test_the_cross_volume_temporary_is_0600_before_its_rename(bus, monkeypatch, tmp_path):
    store, captures = tmp_path / "store", tmp_path / "other-volume"
    camera = _connected(bus, monkeypatch, store)
    _cross_volume(monkeypatch)
    crossed, real_open, modes = camera_mod.os.replace, camera_mod.os.open, []

    def open_(path, flags, *args, **kwargs):
        if not str(path).endswith(".part"):
            return real_open(path, flags, *args, **kwargs)
        old_umask = os.umask(0o277)  # a umask that strips the owner's write bit at creation
        try:
            return real_open(path, flags, *args, **kwargs)
        finally:
            os.umask(old_umask)

    def replace(source, target, *args, **kwargs):
        if str(source).endswith(".part"):
            modes.append(oct(os.lstat(source).st_mode & 0o777))
        return crossed(source, target, *args, **kwargs)
    monkeypatch.setattr(camera_mod.os, "open", open_)
    monkeypatch.setattr(camera_mod.os, "replace", replace)
    delivered = camera.capture(dest_dir=captures)
    assert modes == ["0o600"], modes
    assert oct(delivered.stat().st_mode & 0o777) == "0o600"


# --- round 6: a Night Assist plan runs only on the body it was made for ---------------------

NIGHT_PLAN = {"mode": "night", "frames": 4, "ev_step": 0.0, "interval_s": 0.0,
              "camera_model": A7III, "camera_port": PORT_1, "camera_serial": SERIAL_A,
              "night_assist": {"iso": "1600", "aperture_label": "f/4", "shutter_label": "1", "frames": 4,
                               "evidence": {"fixture": True}, "camera_serial": SERIAL_A}}


def _start_click(plan, camera):
    """Run the page's own Start handler (index.html) in node with a Night Assist plan pending."""
    handler = re.search(r"  \$\('session-start'\)\.onclick=async\(\)=>\{.*?\n  \};\n", INDEX, re.S).group(0)
    script = ("const sent=[]; let busy=false, sessionRunning=false, cameraSelectionRequired=false;\n"
              "const NO_SERIAL_MESSAGE='fixture: no serial';\n"
              f"let nightAssistRequest={json.dumps(plan)};\n"
              f"const availableCameras=[{json.dumps(camera)}];\n"
              "const values={'camera-select':'0','session-mode':'night','session-frames':'4','session-ev':'0',\n"
              "  'session-interval':'0','timelapse-policy':'fixed','timelapse-fps':'24','timelapse-min-iso':'100',\n"
              "  'timelapse-max-iso':'6400','timelapse-min-shutter':'0.001','timelapse-max-shutter':'1','timelapse-step':'0.3'};\n"
              "const elements={};\n"
              "const $=id=>elements[id] || (elements[id]={value:values[id],checked:false,textContent:''});\n"
              "const timelapseControls=()=>null, longExposureDuration=()=>undefined, deflickerOption=()=>({});\n"
              "const timelapseSessionFields=mode=>mode==='timelapse'?{timelapse_mode:'fixed'}:{};\n"
              "const sessionBusy=()=>{}, showSession=()=>{}, showToast=message=>sent.push({toast:message});\n"
              "const noteSessionCameraWarning=()=>{};\n"
              "const sessionRequest=async(path,body)=>{sent.push({path,body});return {id:'fixture'};};\n"
              "const runSession=async id=>sent.push({run:id});\n"
              f"{handler}\n"
              "elements['session-start'].onclick().then(()=>process.stdout.write(JSON.stringify(\n"
              "  {sent, status: elements['session-status']?.textContent || ''})));")
    return json.loads(NODE_RUN(["node", "-e", script], check=True, capture_output=True, text=True).stdout)


@pytest.mark.parametrize("selected", [
    {"model": A7III, "port": PORT_2, "serial": SERIAL_B},                           # another A7 III
    {"model": "Sony Alpha-A7 II (PC Control)", "port": PORT_1, "serial": SERIAL_A},  # another model
])
def test_start_refuses_a_night_plan_made_for_another_body(selected):
    result = _start_click(NIGHT_PLAN, selected)
    assert not [item for item in result["sent"] if item.get("path") == "/sessions"], result
    toasts = [item["toast"] for item in result["sent"] if "toast" in item]
    assert toasts == [f"This plan was made for {A7III}; re-plan for the selected camera."], result


def test_start_submits_the_plans_own_camera():
    for selected in ({"model": A7III, "port": PORT_1, "serial": SERIAL_A},
                     {"model": A7III, "port": PORT_2, "serial": SERIAL_A}):  # same body on a new port
        result = _start_click(NIGHT_PLAN, selected)
        posts = [item["body"] for item in result["sent"] if item.get("path") == "/sessions"]
        assert len(posts) == 1, result
        body = posts[0]
        assert (body["camera_model"], body["camera_port"], body["camera_serial"]) == (A7III, PORT_1, SERIAL_A), body
        assert body["night_assist"] == NIGHT_PLAN["night_assist"]
        assert {"run": "fixture"} in result["sent"], result  # the job was submitted, not a setup failure


def test_sessions_refuses_night_assist_metadata_recorded_for_another_serial(product, bus):
    product.fake.name = "real"
    foreign = {**NIGHT_PLAN, "camera_port": PORT_2, "camera_serial": SERIAL_B}
    refused = product.client.post("/sessions", headers=product.headers, json=foreign)
    assert refused.status_code == 400 and "re-plan" in refused.json()["detail"], refused.text
    own = product.client.post("/sessions", headers=product.headers, json=NIGHT_PLAN)
    assert own.status_code == 200, own.text
    # A plan that records no serial is not checked server side (the page still binds it).
    unrecorded = {**foreign, "night_assist": {**NIGHT_PLAN["night_assist"], "camera_serial": None}}
    assert product.client.post("/sessions", headers=product.headers, json=unrecorded).status_code == 200


# --- session frames carry an unconfirmed delivery (the #61 evidence) through to the record -----

def test_a_session_frame_with_an_unconfirmed_delivery_says_so_in_manifest_status_and_record(product, bus):
    """Real product_sessions.run + SessionRunner + the CLI capture: only frame 2's delivery into the
    download folder fails its directory fsync. That frame, the session rollup, the API state and
    the published record all carry it; the other frames do not."""
    product.fake.name = "real"
    created = product.client.post("/sessions", headers=product.headers,
                                  json={"mode": "hdr", "frames": 3, **_selection()}).json()
    _real_shutter_path(product.monkeypatch, bus)
    cli = camera_mod.subprocess.run

    def run(command, **kwargs):
        if "--capture-image-and-download" in command:
            bus.captures.append(("gphoto2 CLI", command))
            Image.new("RGB", (96, 64), (118, 118, 118)).save(Path(kwargs["cwd"]) / "DSC00001.JPG", quality=90)
            return SimpleNamespace(returncode=0, stdout="", stderr="")
        return cli(command, **kwargs)
    product.monkeypatch.setattr(camera_mod.subprocess, "run", run)
    real_fsync, deliveries = camera_mod._fsync_directory, []

    def fsync_directory(path):
        if Path(path) == Path(camera_mod.CAPTURE_DIR):
            deliveries.append(path)
            if len(deliveries) == 2:
                raise OSError(errno.EIO, "Input/output error")
        return real_fsync(path)
    product.monkeypatch.setattr(camera_mod, "_fsync_directory", fsync_directory)

    response = product.client.post(f"/sessions/{created['id']}/run", headers=product.headers)
    assert response.status_code == 200, response.text
    state = json.loads(_manifest(created["id"]))
    assert state["status"] == "completed", state.get("error")
    assert len(deliveries) == 3
    flagged = [frame["index"] for frame in state["frames"] if "delivery_durability" in frame]
    assert flagged == [1], [frame.get("transport_receipt") for frame in state["frames"]]
    frame = state["frames"][1]
    assert frame["delivery_durability"] == {"status": "unconfirmed", "reason_at": "transport_receipt.delivery_durability"}  # the reason stays in the receipt, not copied
    assert frame["delivery_durability"]["status"] == "unconfirmed"
    rollup = state["delivery_durability"]
    assert rollup["status"] == "unconfirmed" and rollup["frames"] == [1]
    assert "Input/output error" in rollup["reasons"][0]
    assert "frame 2 of 3" in rollup["warning"] and "not confirmed" in rollup["warning"]
    assert "Every frame the session accepted" in rollup["warning"]  # claims only accepted copies
    # The API state the Sessions panel reads, and the published Review record.
    api = product.client.get(f"/sessions/{created['id']}", headers=product.headers).json()
    assert api["delivery_durability"] == rollup
    sidecar = json.loads((engine.SHOTS_DIR / f"session_{created['id']}.json").read_text())
    assert rollup["warning"] in sidecar["summary"]
    assert sidecar["evidence"]["delivery_durability"] == rollup


def test_the_durability_rollup_follows_receipts_frame_count_and_older_manifests(product, bus):
    """Codex review: a manifest saved before this rollup existed is backfilled on read; a clean
    receipt on a recaptured frame clears its flag; the count follows frame_count (Smartlapse)."""
    import stack_session
    sessions = product_sessions.runner()
    state = sessions.create("hdr", 3, metadata={"camera_model": A7III, "camera_port": PORT_1,
                                                "camera_serial": SERIAL_A})
    unconfirmed = {"status": "unconfirmed", "reason": "the photo's folder could not be flushed to disk (x)"}
    state["frames"][0].update(status="ambiguous", transport_receipt={"delivery_durability": unconfirmed})  # it fired
    manifest = sessions._directory(state["id"]) / "manifest.json"
    stack_session._atomic_json(manifest, state)  # written the way an older build wrote it: no rollup
    assert "delivery_durability" not in json.loads(manifest.read_text())
    read = sessions.read(state["id"])
    assert read["frames"][0]["delivery_durability"] == {"status": "unconfirmed", "reason_at": "transport_receipt.delivery_durability"}
    assert read["delivery_durability"]["frames"] == [0] and "frame 1 of 3" in read["delivery_durability"]["warning"]
    api = product.client.get(f"/sessions/{state['id']}", headers=product.headers).json()
    assert api["delivery_durability"] == read["delivery_durability"]
    sessions.save(read)
    assert json.loads(manifest.read_text())["delivery_durability"] == read["delivery_durability"]

    rolled = {"frame_count": 5, "frames": [{"index": 0, "status": "accepted", "transport_receipt": {"delivery_durability": unconfirmed}},
                                           {"index": 1, "status": "accepted", "transport_receipt": {"delivery_durability": unconfirmed}}]}
    stack_session._record_delivery_durability(rolled)
    assert "frames 1, 2 of 5" in rolled["delivery_durability"]["warning"]
    rolled["frame_count"] = 3  # Smartlapse shortened the plan
    rolled["frames"][1]["transport_receipt"] = {"transport": "recaptured, clean"}
    stack_session._record_delivery_durability(rolled)
    assert rolled["delivery_durability"]["frames"] == [0] and "frame 1 of 3" in rolled["delivery_durability"]["warning"]
    assert "delivery_durability" not in rolled["frames"][1]
    rolled["frames"][0]["transport_receipt"] = {}
    stack_session._record_delivery_durability(rolled)
    assert "delivery_durability" not in rolled and "delivery_durability" not in rolled["frames"][0]


def test_gallery_shows_the_warning_for_a_session_published_before_the_rollup_existed(product, bus):
    """Review of #70: a session published by an older build has a sidecar without the warning.
    /gallery projects the rollup derived from its manifest into the returned record; the stored
    sidecar and the photo are not rewritten."""
    import stack_session
    from PIL import Image as _Image
    sessions = product_sessions.runner()
    state = sessions.create("hdr", 3, metadata={"camera_model": A7III, "camera_port": PORT_1,
                                                "camera_serial": SERIAL_A})
    unconfirmed = {"status": "unconfirmed", "reason": "the photo's folder could not be flushed to disk (x)"}
    state["frames"][2].update(status="ambiguous", transport_receipt={"delivery_durability": unconfirmed})  # it fired
    stack_session._atomic_json(sessions._directory(state["id"]) / "manifest.json", state)  # older build
    image = f"session_{state['id']}.jpg"
    _Image.new("RGB", (16, 12), "gray").save(engine.SHOTS_DIR / image)
    old_sidecar = {"image": image, "summary": "hdr completed from 3 frames.", "why": "fixture",
                   "settings": {}, "cards": [], "mood": {}, "applied": {}, "intent": None,
                   "evidence": {"source": "usb_session", "session_id": state["id"]}, "ts": 1.0}
    sidecar_path = engine.SHOTS_DIR / f"session_{state['id']}.json"
    sidecar_path.write_text(json.dumps(old_sidecar))
    before = (sidecar_path.read_bytes(), (engine.SHOTS_DIR / image).read_bytes())
    # A record whose session is gone, and one with a forged id, are listed unchanged.
    for name, session_id in (("session_gone", "e" * 32), ("session_forged", "../../etc")):
        (engine.SHOTS_DIR / f"{name}.json").write_text(json.dumps(
            {**old_sidecar, "image": f"{name}.jpg", "evidence": {"source": "usb_session", "session_id": session_id}}))
    rows = {row["image"]: row for row in product.client.get("/gallery", headers=product.headers).json()}
    row = rows[f"/shots/{image}"]
    rollup = row["evidence"]["delivery_durability"]
    assert rollup["frames"] == [2] and "frame 3 of 3" in rollup["warning"]
    assert row["summary"] == "hdr completed from 3 frames. " + rollup["warning"]
    for name in ("session_gone", "session_forged"):
        assert "delivery_durability" not in rows[f"/shots/{name}.jpg"]["evidence"]
        assert rows[f"/shots/{name}.jpg"]["summary"] == "hdr completed from 3 frames."
    assert (sidecar_path.read_bytes(), (engine.SHOTS_DIR / image).read_bytes()) == before


def test_gallery_reads_no_manifest_for_new_records_and_each_legacy_manifest_once(product, bus, monkeypatch):
    """Review of #70: new publications mark their sidecar as checked, so /gallery never rereads a clean
    session's manifest; a legacy record is read once per manifest version, not on every request."""
    import stack_session
    from PIL import Image as _Image
    product.fake.name = "real"
    sessions = product_sessions.runner()
    # A clean session published by this build (through the real publisher).
    clean = sessions.create("hdr", 3, metadata={"camera_model": A7III, "camera_port": PORT_1, "camera_serial": SERIAL_A})
    directory = sessions._directory(clean["id"])
    for frame in clean["frames"]:
        _Image.new("RGB", (32, 24), (90, 90, 90)).save(frame["destination"])
        frame.update(status="accepted", receipt=stack_session.frame_receipt(frame["destination"]),
                     transport_receipt={"delivery": {"directory_fsync": "ok"}})
    _Image.new("RGB", (32, 24), (90, 90, 90)).save(directory / "result.jpg")
    clean.update(status="completed", restore="completed", completed_at=2.0,
                 output={"receipt": stack_session.frame_receipt(directory / "result.jpg"), "details": {}})
    sessions.save(clean)
    published = product_sessions._publish(sessions, sessions.read(clean["id"]))
    assert published["publication"]["status"] == "published", published.get("publication_error")
    sidecar = json.loads((engine.SHOTS_DIR / f"session_{clean['id']}.json").read_text())
    assert sidecar["evidence"]["delivery_durability_checked"] is True
    assert "delivery_durability" not in sidecar["evidence"]
    # A legacy record: an older build's sidecar with no marker, and a manifest with an unconfirmed frame.
    legacy = sessions.create("hdr", 3, metadata={"camera_model": A7III, "camera_port": PORT_1, "camera_serial": SERIAL_A})
    legacy["frames"][0].update(status="ambiguous", transport_receipt={"delivery_durability": {"status": "unconfirmed", "reason": "x"}})
    stack_session._atomic_json(sessions._directory(legacy["id"]) / "manifest.json", legacy)
    image = f"session_{legacy['id']}.jpg"
    _Image.new("RGB", (16, 12), "gray").save(engine.SHOTS_DIR / image)
    (engine.SHOTS_DIR / f"session_{legacy['id']}.json").write_text(json.dumps(
        {"image": image, "summary": "hdr completed from 3 frames.", "ts": 1.0,
         "evidence": {"source": "usb_session", "session_id": legacy["id"]}}))
    reads = []
    real_read = stack_session.SessionRunner.read
    monkeypatch.setattr(stack_session.SessionRunner, "read",
                        lambda self, session_id: reads.append(session_id) or real_read(self, session_id))
    for _ in range(3):
        rows = {row["image"]: row for row in product.client.get("/gallery", headers=product.headers).json()}
        assert "delivery_durability" not in rows[f"/shots/session_{clean['id']}.jpg"]["evidence"]
        assert rows[f"/shots/{image}"]["evidence"]["delivery_durability"]["frames"] == [0]
    assert clean["id"] not in reads
    assert reads == [legacy["id"]]  # cached until the manifest changes
    stack_session._atomic_json(sessions._directory(legacy["id"]) / "manifest.json",
                               {**legacy, "frames": [{**f, "transport_receipt": {}} for f in legacy["frames"]]})
    rows = {row["image"]: row for row in product.client.get("/gallery", headers=product.headers).json()}
    assert "delivery_durability" not in rows[f"/shots/{image}"]["evidence"] and reads == [legacy["id"]] * 2


def test_a_frame_refused_before_its_shutter_never_inherits_the_previous_frames_save_warning(product, bus):
    """Review of #70: frame 1's delivery was unconfirmed; frame 2 is then refused by the identity gate
    before its shutter. Camera.capture raises before it starts a new receipt, so the adapter must clear
    the previous receipt first, and a frame that never fired is never flagged."""
    product.fake.name = "real"
    created = product.client.post("/sessions", headers=product.headers,
                                  json={"mode": "hdr", "frames": 3, **_selection()}).json()
    _real_shutter_path(product.monkeypatch, bus)
    cli = camera_mod.subprocess.run

    def run(command, **kwargs):
        if "--capture-image-and-download" in command:
            bus.captures.append(("gphoto2 CLI", command))
            Image.new("RGB", (96, 64), (118, 118, 118)).save(Path(kwargs["cwd"]) / "DSC00001.JPG", quality=90)
            return SimpleNamespace(returncode=0, stdout="", stderr="")
        return cli(command, **kwargs)
    product.monkeypatch.setattr(camera_mod.subprocess, "run", run)
    real_fsync, attempts = camera_mod._fsync_directory, []

    def fsync_directory(path):
        if Path(path) == Path(camera_mod.CAPTURE_DIR):
            raise OSError(errno.EIO, "Input/output error")  # frame 1's delivery is unconfirmed
        return real_fsync(path)
    product.monkeypatch.setattr(camera_mod, "_fsync_directory", fsync_directory)

    def capture(self, download=True, dest_dir=None):
        attempts.append(self.last_capture_receipt if hasattr(self, "last_capture_receipt") else None)
        if len(attempts) == 2:
            # The connection lost its proven serial: capture() refuses at its first line, before it
            # starts a new receipt, so last_capture_receipt still holds frame 1's.
            self.verified_serial = None
        return REAL_CAPTURE(self, download=download, dest_dir=dest_dir)
    product.monkeypatch.setattr(Camera, "capture", capture)
    product.client.post(f"/sessions/{created['id']}/run", headers=product.headers)
    assert len(attempts) == 2 and attempts[1] is None  # the adapter cleared frame 1's receipt first
    state = json.loads(_manifest(created["id"]))
    assert state["status"] == "identity_refused", state.get("error")
    assert [frame["status"] for frame in state["frames"]] == ["accepted", "pending", "pending"]
    shutters = [command for kind, command in bus.captures if kind == "gphoto2 CLI"]
    assert len(shutters) == 1
    assert state["frames"][0]["delivery_durability"]["status"] == "unconfirmed"
    refused = state["frames"][1]
    assert "delivery_durability" not in refused
    assert "delivery_durability" not in (refused.get("transport_receipt") or {}), refused.get("transport_receipt")
    assert state["delivery_durability"]["frames"] == [0] and "frame 1 of 3" in state["delivery_durability"]["warning"]


def test_the_rollup_ignores_frames_that_never_fired():
    import stack_session
    unconfirmed = {"status": "unconfirmed", "reason": "the photo's folder could not be flushed to disk (x)"}
    state = {"frame_count": 3, "frames": [
        {"index": 0, "status": "accepted", "transport_receipt": {"delivery_durability": unconfirmed}},
        {"index": 1, "status": "pending", "transport_receipt": {"delivery_durability": unconfirmed},
         "delivery_durability": unconfirmed},  # a stale copy on a frame that never fired
        {"index": 2, "status": "ambiguous", "transport_receipt": {"delivery_durability": unconfirmed}}]}
    stack_session._record_delivery_durability(state)
    assert state["delivery_durability"]["frames"] == [0, 2]
    assert "delivery_durability" not in state["frames"][1]
    state["frames"][0]["status"] = state["frames"][2]["status"] = "pending"
    stack_session._record_delivery_durability(state)
    assert "delivery_durability" not in state


def test_gallery_projects_the_linked_session_warning_onto_a_legacy_auto_fallback_record(product, bus, monkeypatch):
    """Review of #70: an older build's ordinary photo for an Auto stack that fell back links its session
    only through evidence.stack_policy.session_id. /gallery adds that session's warning to the row
    (read once per manifest version), keeps the ordinary photo's own #61 warning, and rewrites nothing.
    A fallback record written by this build is marked checked and never rereads the manifest."""
    import stack_session
    from PIL import Image as _Image
    sessions = product_sessions.runner()
    session = sessions.create("hdr", 3, metadata={"camera_model": A7III, "camera_port": PORT_1, "camera_serial": SERIAL_A})
    session["frames"][1].update(status="ambiguous",
                                transport_receipt={"delivery_durability": {"status": "unconfirmed", "reason": "x"}})
    stack_session._atomic_json(sessions._directory(session["id"]) / "manifest.json", session)
    own = {"status": "unconfirmed", "reason": "the ordinary photo's own folder flush failed"}
    own_sentence = f" Warning: saving was not confirmed durable — {own['reason']}."

    def legacy(name, evidence_extra, summary="Photo captured."):
        _Image.new("RGB", (16, 12), "gray").save(engine.SHOTS_DIR / f"{name}.jpg")
        path = engine.SHOTS_DIR / f"{name}.json"
        path.write_text(json.dumps({"image": f"{name}.jpg", "summary": summary, "ts": 1.0, "evidence": {
            "source": "usb", "stack_policy": {"session_id": session["id"], "status": "needs_reconciliation"},
            **evidence_extra}}))
        return path
    plain = legacy("shot_legacy_plain", {})
    with_own = legacy("shot_legacy_own", {"delivery_durability": own}, summary="Photo captured." + own_sentence)
    stored = {path: path.read_bytes() for path in (plain, with_own)}
    # This build's fallback record for a clean stack: marked inside stack_policy, never reread.
    clean = sessions.create("hdr", 3, metadata={"camera_model": A7III, "camera_port": PORT_1, "camera_serial": SERIAL_A})
    _Image.new("RGB", (16, 12), "gray").save(engine.SHOTS_DIR / "shot_new_clean.jpg")
    (engine.SHOTS_DIR / "shot_new_clean.json").write_text(json.dumps({"image": "shot_new_clean.jpg", "summary": "ok",
        "ts": 1.0, "evidence": {"source": "usb", "stack_policy": {"session_id": clean["id"],
                                                                   "delivery_durability_checked": True}}}))
    reads = []
    real_read = stack_session.SessionRunner.read
    monkeypatch.setattr(stack_session.SessionRunner, "read",
                        lambda self, session_id: reads.append(session_id) or real_read(self, session_id))
    for _ in range(2):
        rows = {row["image"]: row for row in product.client.get("/gallery", headers=product.headers).json()}
        rollup = rows["/shots/shot_legacy_plain.jpg"]["evidence"]["stack_policy"]["delivery_durability"]
        assert rollup["frames"] == [1]
        assert rows["/shots/shot_legacy_plain.jpg"]["evidence"]["delivery_durability"] == rollup
        assert rows["/shots/shot_legacy_plain.jpg"]["summary"] == "Photo captured. " + rollup["warning"]
        mixed = rows["/shots/shot_legacy_own.jpg"]
        assert mixed["evidence"]["delivery_durability"] == own  # the ordinary photo's own warning stays
        assert mixed["evidence"]["stack_policy"]["delivery_durability"] == rollup
        assert mixed["summary"] == "Photo captured." + own_sentence + " " + rollup["warning"]
        assert "delivery_durability" not in rows["/shots/shot_new_clean.jpg"]["evidence"]["stack_policy"]
    assert reads == [session["id"]]  # one read across rows and requests; the clean record never read
    assert {path: path.read_bytes() for path in (plain, with_own)} == stored


# The manifest stores absolute paths (each frame's destination and receipt.path, plus any a real receipt
# carries), so its raw size grows with wherever pytest puts tmp_path. Sizes count tmp_path as this many
# characters: more than a default macOS run's (116 plus the digits of its pytest-N, 118 at pytest-68), a
# Linux CI run's (62) or a short TMPDIR's, so no budget is looser than where it already passed, and a
# 171-character session directory is longer than a deployment's (98 for backend/shots/sessions/<id> in
# the Mac checkout).
REFERENCE_TMP_PATH_CHARS = 128


def _timelapse_with_receipts(tmp_path, frames, receipt, name):
    """Run a SessionRunner timelapse whose every frame returns a copy of `receipt`; the manifest size
    with tmp_path counted as REFERENCE_TMP_PATH_CHARS characters, so it does not depend on TMPDIR."""
    import io
    import stack_session
    jpeg = io.BytesIO()
    Image.new("RGB", (16, 12), (90, 90, 90)).save(jpeg, "JPEG")
    runner = stack_session.SessionRunner(tmp_path / name)
    state = runner.create("timelapse", frames, ev_step=0, interval_s=0)

    def capture(index, spec, destination):
        spec["transport_receipt"] = json.loads(json.dumps(receipt))
        Path(destination).write_bytes(jpeg.getvalue())
        return destination
    result = runner.run(state["id"], capture)
    assert result["status"] == "completed", result.get("error")
    raw = (runner._directory(state["id"]) / "manifest.json").read_bytes()
    root = json.dumps(str(tmp_path))[1:-1].encode()  # tmp_path as the manifest's JSON strings hold it
    assert raw.count(root) >= 2 * frames  # the paths being normalized are really there
    return result, len(raw.replace(root, b"x" * REFERENCE_TMP_PATH_CHARS))


def test_a_240_frame_timelapse_with_every_save_unconfirmed_adds_little_to_the_manifest(bus, monkeypatch, tmp_path):
    """Review of #70: a failing directory fsync on every frame of a 240-frame timelapse must not grow
    the manifest toward SessionRunner.read's 512 KiB limit. The frame references its receipt's reason
    instead of copying it, and the rollup keeps indices plus unique, capped reasons.
    (Pre-existing and out of scope here: full real transport receipts, ~3.9 KB each in the manifest,
    already exceed that limit at about 130 frames without any durability flag.)"""
    store = tmp_path / "store"
    camera = _connected(bus, monkeypatch, store)
    _failing_same_volume_dir_fsync(monkeypatch, store)
    camera.capture(dest_dir=store)
    receipt = camera.last_capture_receipt  # a real CLI receipt with an unconfirmed delivery
    flag = receipt["delivery_durability"]
    assert flag["status"] == "unconfirmed"
    # 240 frames whose receipts carry only the durability flag: what this PR itself stores.
    result, size = _timelapse_with_receipts(tmp_path, 240, {"delivery_durability": flag}, "flag-only")
    rollup = result["delivery_durability"]
    assert rollup["frames"] == list(range(240))
    assert rollup["reasons"] == [flag["reason"]] and rollup["reasons_omitted"] == 0
    assert "240 of 240 frames (1, 2, 3, 4, 5 and 235 more)" in rollup["warning"] and len(rollup["warning"]) < 400
    assert result["frames"][7]["delivery_durability"] == {"status": "unconfirmed",
                                                         "reason_at": "transport_receipt.delivery_durability"}
    _, unflagged_size = _timelapse_with_receipts(tmp_path, 240, {}, "unflagged")
    # The receipt's own flag, the per-frame reference and the rollup: a few hundred bytes per frame.
    assert size - unflagged_size < 240 * 450, (size, unflagged_size)
    assert size < 512 * 1024 * 0.7, size
    # With full real receipts, the warning's own cost per frame (beyond the receipt's own flag) is small.
    clean = {key: value for key, value in receipt.items() if key != "delivery_durability"}
    _, clean_size = _timelapse_with_receipts(tmp_path, 100, clean, "clean")
    _, flagged_size = _timelapse_with_receipts(tmp_path, 100, receipt, "flagged")
    assert flagged_size - clean_size < 100 * 450, (flagged_size, clean_size)


def test_the_rollup_keeps_unique_reasons_capped_with_a_count():
    import stack_session
    frames = [{"index": index, "status": "accepted", "transport_receipt": {"delivery_durability": {
        "status": "unconfirmed", "reason": f"reason {index % 9}"}}} for index in range(30)]
    state = {"frame_count": 30, "frames": frames}
    stack_session._record_delivery_durability(state)
    rollup = state["delivery_durability"]
    assert rollup["reasons"] == [f"reason {n}" for n in range(stack_session.MAX_ROLLUP_REASONS)]
    assert rollup["reasons_omitted"] == 9 - stack_session.MAX_ROLLUP_REASONS
    assert "30 of 30 frames" in rollup["warning"] and "and 25 more" in rollup["warning"]
