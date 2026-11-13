"""A7 III placeholder settings reads (f/0, ISO 0, 65535/65535) never become a baseline or a plan.

The A7 III answered the first settings read after idle with placeholder exposure values on
2026-09-22; a second read was normal. Every read that is recorded as a held/baseline/restore
snapshot or planned from goes through `read_settings_checked`, which re-reads a bounded number of
times and refuses before any write or shutter when the placeholder persists. Fakes only.
"""
from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest

ROOT = Path(__file__).resolve().parents[1]
for path in (ROOT, ROOT / "backend", ROOT / "tools", ROOT / "tests"):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))

import sonyremote  # noqa: E402
import sonyremote.camera as camera_module  # noqa: E402
from sonyremote.camera import (CameraError, PlaceholderSettingsError, placeholder_fields,  # noqa: E402
                               read_focus_mode_checked, read_settings_checked)
from test_final_wave_engine import rig  # noqa: E402,F401  (fixture)
from test_final_wave_product_api import product  # noqa: E402,F401  (fixture)

PLACEHOLDER = {"aperture": "f/0", "iso": "0", "shutter": "65535/65535", "focus_mode": "Automatic"}
A7II = "Sony ILCE-7M2 (Control)"
A7III = "Sony Alpha-A7 III (PC Control)"


@pytest.fixture(autouse=True)
def no_settle(monkeypatch):
    """Re-reads settle with time.sleep; record the pauses instead of waiting."""
    pauses = []
    monkeypatch.setattr(camera_module.time, "sleep", lambda seconds: pauses.append(seconds))
    return pauses


class ScriptedReads:
    """get_settings answers the placeholder `placeholders` times, then the live settings."""

    def __init__(self, live, placeholders, placeholder=None):
        self.live = live
        self.remaining = placeholders
        self.placeholder = dict(placeholder or PLACEHOLDER)
        self.reads = 0

    def __call__(self):
        self.reads += 1
        if self.remaining == "always" or (isinstance(self.remaining, int) and self.remaining > 0):
            if self.remaining != "always":
                self.remaining -= 1
            return {**self.live, **self.placeholder}
        return dict(self.live)


# --- the definition ---------------------------------------------------------------------------

@pytest.mark.parametrize("settings, expected", [
    (PLACEHOLDER, ["aperture", "iso", "shutter"]),
    ({"aperture": "0", "iso": "0.0", "shutter": "4294967295"}, ["aperture", "iso", "shutter"]),
    ({"aperture": "f/655.35", "iso": "100", "shutter": "1/60"}, ["aperture"]),
    ({"aperture": "f/8", "iso": "100", "shutter": "65535/1"}, ["shutter"]),
    ({"aperture": "f/8", "iso": "100", "shutter": "0"}, ["shutter"]),
    ({"aperture": "f/8", "iso": "100", "shutter": "1/0"}, ["shutter"]),
    ({"aperture": "f/8", "iso": "0", "shutter": "1/60"}, ["iso"]),
    # Focus-mode word 0 (2026-09-29, first read after auto power-off, beside the exposure placeholders).
    ({**PLACEHOLDER, "focus_mode": "Undefined"}, ["aperture", "focus_mode", "iso", "shutter"]),
    ({"aperture": "f/8", "iso": "100", "shutter": "1/60", "focus_mode": "Undefined"}, ["focus_mode"]),
    ({"aperture": "f/8", "iso": "100", "shutter": "1/60", "focus_mode": "Manual"}, []),
    ({"aperture": "f/8", "iso": "100", "shutter": "1/60", "focus_mode": "DMF"}, []),
    # Real values, a real dial position and unreported nodes are never placeholders.
    ({"aperture": "f/8", "iso": "100", "shutter": "1/60"}, []),
    ({"aperture": "f/2.8", "iso": "Auto ISO", "shutter": "Bulb"}, []),
    ({"aperture": "8", "iso": "25600", "shutter": "30"}, []),
    ({"aperture": "f/5.6", "iso": "6400", "shutter": "1.3"}, []),
    ({"aperture": None, "iso": None, "shutter": None}, []),
    ({}, []),
])
def test_placeholder_fields_names_exactly_the_sentinel_values(settings, expected):
    assert placeholder_fields(settings) == expected


# --- the guarded read -------------------------------------------------------------------------

def test_placeholder_first_read_is_reread_and_the_real_read_is_returned(no_settle):
    live = {"aperture": "f/8", "iso": "200", "shutter": "1/125", "focus_mode": "Automatic"}
    camera = SimpleNamespace(get_settings=ScriptedReads(live, 1))
    settings, receipt = read_settings_checked(camera, purpose="baseline")
    assert settings == live
    assert receipt["reads"] == 2 and receipt["purpose"] == "baseline"
    assert receipt["placeholder_reads"] == [{"read": 1, "fields": ["aperture", "iso", "shutter"],
                                             "values": {"aperture": "f/0", "iso": "0",
                                                        "shutter": "65535/65535"}}]
    assert receipt["aperture_unreported"] is False
    assert no_settle == [camera_module.PLACEHOLDER_SETTLE_S]


def test_an_undefined_focus_mode_in_a_settings_read_is_reread_then_refused(no_settle):
    camera = SimpleNamespace(get_settings=ScriptedReads({"aperture": "f/8", "iso": "200", "shutter": "1/125"}, 0))
    real = camera.get_settings
    camera.get_settings = lambda: {**real(), "focus_mode": "Undefined"}
    with pytest.raises(PlaceholderSettingsError) as caught:
        read_settings_checked(camera, purpose="stack restore baseline")
    assert caught.value.reads[-1] == {"read": camera_module.PLACEHOLDER_REREADS + 1, "fields": ["focus_mode"],
                                      "values": {"focus_mode": "Undefined"}}
    assert "placeholder values" in str(caught.value)


class ModeReads:
    def __init__(self, *modes):
        self.modes, self.reads = list(modes), 0

    def __call__(self):
        self.reads += 1
        return self.modes.pop(0) if len(self.modes) > 1 else self.modes[0]


CHOICES = ["Manual", "Automatic", "AF-C", "AF-A", "DMF", "Preset Focus"]


def test_read_focus_mode_checked_rereads_a_placeholder_and_returns_the_real_mode(no_settle):
    camera = SimpleNamespace(get_focus_mode=ModeReads("Undefined", "AF-C"))
    mode, receipt = read_focus_mode_checked(camera, purpose="sweep original", choices=CHOICES)
    assert mode == "AF-C" and receipt["reads"] == 2 and no_settle == [camera_module.PLACEHOLDER_SETTLE_S]
    assert receipt["placeholder_reads"] == [{"read": 1, "fields": ["focus_mode"], "values": {"focus_mode": "Undefined"}}]
    clean = SimpleNamespace(get_focus_mode=ModeReads("Manual"))
    assert read_focus_mode_checked(clean, purpose="x", choices=CHOICES) == ("Manual", None)
    # Without the node's choices only the "Undefined" label is a placeholder.
    assert read_focus_mode_checked(SimpleNamespace(get_focus_mode=ModeReads("Odd")), purpose="x") == ("Odd", None)


@pytest.mark.parametrize("stuck", ["Undefined", "Unknown value 0000"])
def test_read_focus_mode_checked_refuses_a_persistent_placeholder(no_settle, stuck):
    camera = SimpleNamespace(get_focus_mode=ModeReads(stuck))
    with pytest.raises(PlaceholderSettingsError) as caught:
        read_focus_mode_checked(camera, purpose="bracket original", choices=CHOICES)
    assert camera.get_focus_mode.reads == camera_module.PLACEHOLDER_REREADS + 1
    refusal = caught.value.as_dict()
    assert refusal["purpose"] == "bracket original" and refusal["may_have_fired"] is False
    assert refusal["reads"][-1]["values"] == {"focus_mode": stuck} and "choices" in refusal["detail"]


def test_clean_first_read_has_no_receipt_and_no_pause(no_settle):
    live = {"aperture": "f/8", "iso": "200", "shutter": "1/125"}
    camera = SimpleNamespace(get_settings=ScriptedReads(live, 0))
    assert read_settings_checked(camera, purpose="baseline") == (live, None)
    assert camera.get_settings.reads == 1 and no_settle == []


def test_persistent_placeholder_refuses_after_a_bounded_number_of_reads(no_settle):
    camera = SimpleNamespace(get_settings=ScriptedReads({"focus_mode": "Automatic"}, "always"))
    with pytest.raises(PlaceholderSettingsError) as caught:
        read_settings_checked(camera, purpose="session baseline")
    assert isinstance(caught.value, CameraError)
    assert camera.get_settings.reads == camera_module.PLACEHOLDER_REREADS + 1
    assert len(no_settle) == camera_module.PLACEHOLDER_REREADS
    refusal = caught.value.as_dict()
    assert refusal["error"] == "placeholder_settings" and refusal["may_have_fired"] is False
    assert refusal["purpose"] == "session baseline" and len(refusal["reads"]) == 4
    assert "issued no further setting write or shutter" in str(caught.value)


def test_iso_placeholder_alone_also_refuses():
    camera = SimpleNamespace(get_settings=ScriptedReads(
        {"aperture": "f/8", "shutter": "1/60"}, "always", placeholder={"iso": "0"}))
    with pytest.raises(PlaceholderSettingsError):
        read_settings_checked(camera, purpose="plan")


def test_persistent_lone_f0_is_a_manual_lens_and_is_reported():
    # Camera.wake accepts a lone f/0 with real ISO and shutter (manual/adapted lens); the guard
    # still re-reads it, then keeps it and says so rather than refusing the body.
    live = {"aperture": "f/0", "iso": "100", "shutter": "1/60"}
    camera = SimpleNamespace(get_settings=ScriptedReads(live, 0))
    settings, receipt = read_settings_checked(camera, purpose="baseline")
    assert settings == live
    assert receipt["aperture_unreported"] is True
    assert receipt["reads"] == camera_module.PLACEHOLDER_REREADS + 1


def test_transient_lone_f0_is_reread_to_the_real_aperture():
    live = {"aperture": "f/4", "iso": "100", "shutter": "1/60"}
    camera = SimpleNamespace(get_settings=ScriptedReads(live, 1, placeholder={"aperture": "f/0"}))
    settings, receipt = read_settings_checked(camera, purpose="baseline")
    assert settings["aperture"] == "f/4" and receipt["aperture_unreported"] is False


def test_lone_f0_needs_real_iso_and_shutter_on_the_last_two_reads():
    # Three full placeholders then one lone f/0 is a body still waking, not a manual lens.
    live = {"aperture": "f/0", "iso": "100", "shutter": "1/60"}
    camera = SimpleNamespace(get_settings=ScriptedReads(live, camera_module.PLACEHOLDER_REREADS))
    with pytest.raises(PlaceholderSettingsError):
        read_settings_checked(camera, purpose="baseline")
    # Two trailing lone f/0 reads beside real ISO/shutter are a manual lens.
    camera = SimpleNamespace(get_settings=ScriptedReads(live, camera_module.PLACEHOLDER_REREADS - 1))
    assert read_settings_checked(camera, purpose="baseline")[1]["aperture_unreported"] is True


@pytest.mark.parametrize("live", [
    {"aperture": "f/655.35", "iso": "100", "shutter": "1/60"},   # raw 0xFFFF is never a lens
    {"aperture": "f/0", "iso": None, "shutter": "1/60"},          # no ISO reported
    {"aperture": "f/0", "shutter": "1/60"},                       # ISO node missing
    {"aperture": "f/0", "iso": "100", "shutter": None},           # no shutter reported
])
def test_persistent_aperture_placeholder_without_manual_lens_evidence_refuses(live):
    camera = SimpleNamespace(get_settings=ScriptedReads(live, 0))
    with pytest.raises(PlaceholderSettingsError):
        read_settings_checked(camera, purpose="baseline")


# --- focus bracket: held / restore snapshot -------------------------------------------------

def _aperture_camera(placeholders):
    from test_focus_bracket import ApertureFakeCamera
    camera = ApertureFakeCamera(settings={"aperture": "f/4", "iso": "100", "shutter": "1/125"})
    reads = ScriptedReads(camera.settings, placeholders)
    camera.get_settings = reads
    return camera


def test_focus_bracket_held_snapshot_rereads_past_a_placeholder():
    import focus_bracket
    camera = _aperture_camera(1)
    decision = focus_bracket.resolve_bracket_aperture(camera, body_model=A7III, lens_model="Tamron 28-75 f/2.8")
    assert decision["held"] == {"aperture": "f/4", "iso": "100", "shutter": "1/125"}
    assert decision["held_read"]["reads"] == 2
    assert decision["compensation"]["feasible"] is True  # planned from the real exposure


def test_focus_bracket_persistent_placeholder_refuses_before_any_write():
    import focus_bracket
    camera = _aperture_camera("always")
    with pytest.raises(PlaceholderSettingsError):
        focus_bracket.resolve_bracket_aperture(camera, body_model=A7III, lens_model="Tamron 28-75 f/2.8")
    assert camera.set_calls == []


# --- product sessions: restore baseline and timelapse before-settings ------------------------

def _session_camera(tmp_path, live, reads, captures):
    from PIL import Image

    class Camera:
        def __init__(self, **kwargs): pass
        def __enter__(self): return self
        def __exit__(self, *args): pass
        def info(self): return SimpleNamespace(model=A7II, port="usb:001,002")
        def wake(self): return True
        def shooting_mode(self): return "M"
        def get_settings(self): return reads()
        def list_options(self, key):
            return {"iso": ["100", "125", "160", "200", "250"], "aperture": ["8"], "shutter": ["1/60"]}[key]
        def set_setting(self, key, value): live[key] = value
        def set_exposure(self, **kwargs): live.update(kwargs)
        def capture(self, **kwargs):
            path = tmp_path / f"source_{len(captures)}.jpg"
            Image.new("RGB", (64, 48), (30, 30, 30)).save(path)
            captures.append(path)
            reads.remaining = 1  # the body answers its next read with the placeholder
            return str(path)
    return Camera


def _session(tmp_path, monkeypatch, *, mode, placeholders, frames=2, expect_refusal=False, **create):
    import engine
    import product_sessions
    monkeypatch.setattr(engine, "SHOTS_DIR", tmp_path / "shots")
    monkeypatch.setattr(engine, "_free_usb", lambda: None)
    live = {"iso": "100", "shutter": "1/60", "aperture": "8", "focus_mode": "Automatic"}
    reads = ScriptedReads(live, placeholders)
    captures = []
    writes = []
    Camera = _session_camera(tmp_path, live, reads, captures)
    original_set_exposure = Camera.set_exposure
    Camera.set_exposure = lambda self, **kwargs: (writes.append(kwargs), original_set_exposure(self, **kwargs))[1]
    monkeypatch.setattr(sonyremote, "Camera", Camera)
    state = product_sessions.runner().create(mode, frames, metadata={
        "camera_model": A7II, "camera_port": "usb:001,002", "camera_serial": "A7II00SERIAL"}, **create)
    if expect_refusal:
        # Like "set the mode dial to M", the refusal surfaces before the session starts shooting.
        with pytest.raises(PlaceholderSettingsError):
            product_sessions.run(state["id"])
        return product_sessions.runner().read(state["id"]), live, captures, writes
    return product_sessions.run(state["id"]), live, captures, writes


def test_session_baseline_rereads_past_a_placeholder_and_records_it(tmp_path, monkeypatch):
    result, live, captures, _ = _session(tmp_path, monkeypatch, mode="timelapse", placeholders=1)
    assert result["status"] == "completed", result
    assert result["restore_baseline"]["iso"] == "100" and result["restore_baseline"]["shutter"] == "1/60"
    assert result["restore_baseline_read"]["reads"] == 2
    assert live["iso"] == "100" and live["shutter"] == "1/60"


def test_session_persistent_placeholder_baseline_fires_nothing_and_writes_nothing(tmp_path, monkeypatch):
    result, _, captures, writes = _session(tmp_path, monkeypatch, mode="timelapse", placeholders="always",
                                           expect_refusal=True)
    assert result["status"] not in ("completed", "needs_reconciliation", "restore_failed")
    assert not result.get("restore_baseline")  # a later run takes a real baseline
    assert captures == [] and writes == []
    assert all(frame["status"] == "pending" for frame in result["frames"])


def test_timelapse_before_settings_never_record_the_placeholder(tmp_path, monkeypatch):
    # Every capture leaves the body answering its next read with the placeholder: the next frame's
    # before-settings (which also feed the mounted-lens plan) must be the re-read real values.
    result, _, captures, _ = _session(tmp_path, monkeypatch, mode="timelapse", placeholders=0, frames=3,
                                      timelapse_mode="holy_grail", timelapse_options={"max_shutter_s": 1 / 60})
    assert result["status"] == "completed", result
    assert len(captures) == 3
    # The manifest keeps each frame's full evidence in its sidecar (#94); read the complete frames.
    import product_sessions
    frames = product_sessions.runner().read_detail(result["id"])["frames"]
    for frame in frames:
        before = frame["applied"]["exposure_transition"]["before"]
        assert placeholder_fields(before) == [], before
    rereads = [frame["applied"].get("settings_rereads") for frame in frames[1:]]
    assert all(reread and reread[0]["reads"] == 2 for reread in rereads), rereads


# --- engine: ordinary /shoot meter baseline -------------------------------------------------

def test_engine_shoot_refuses_a_persistent_placeholder_before_the_meter_shutter(rig):
    rig.camera.get_settings = ScriptedReads(rig.camera.settings, "always")
    with pytest.raises(PlaceholderSettingsError):
        rig.real.shoot("natural", variant_id="natural")
    assert rig.camera.capture_count == 0
    assert rig.camera.exposure_writes == [] and rig.camera.setting_writes == []  # not even white balance


def test_engine_refusal_after_the_meter_frame_is_clean_not_capture_uncertain(rig):
    # The meter frame fired; the body then answers placeholders. The final shutter is refused
    # cleanly (the meter frame is named as preserved), never reported as "may have fired".
    reads = ScriptedReads(rig.camera.settings, 0)
    rig.camera.get_settings = reads
    original_capture = rig.camera.capture

    def capture(*args, **kwargs):
        path = original_capture(*args, **kwargs)
        reads.remaining = "always"
        return path
    rig.camera.capture = capture
    with pytest.raises(PlaceholderSettingsError) as caught:
        rig.real.shoot("natural", variant_id="natural")
    assert rig.camera.capture_count == 1
    assert not isinstance(caught.value, sonyremote.CaptureOutcomeUncertain)
    assert caught.value.as_dict()["earlier_capture_path"]


def test_engine_shoot_records_rereads_and_plans_from_real_values(rig):
    reads = ScriptedReads(rig.camera.settings, 0)
    rig.camera.get_settings = reads
    original_capture = rig.camera.capture

    def capture(*args, **kwargs):
        path = original_capture(*args, **kwargs)
        reads.remaining = 1  # the first read after each capture answers the placeholder
        return path
    rig.camera.capture = capture
    shot = rig.real.shoot("natural", variant_id="natural")
    assert shot.ok and rig.camera.capture_count == 2
    assert all(placeholder_fields(p) == [] for p in shot.evidence["meter_passes"])
    assert placeholder_fields(shot.settings) == []
    assert shot.evidence["settings_rereads"], shot.evidence


# --- handheld: base settings restored at the end ---------------------------------------------

def test_handheld_refuses_a_placeholder_base_and_never_restores_it(tmp_path):
    from handheld import HandheldSession
    from test_handheld import FakeCamera
    camera = FakeCamera([])
    camera.get_settings = ScriptedReads(dict(camera._settings), "always")
    session = HandheldSession(lambda: camera, lambda *a: {}, max_frames=1, max_duration_s=5, out_dir=tmp_path)
    with pytest.raises(PlaceholderSettingsError):
        session.run()
    assert camera.set_exposure_calls == []
    assert camera.capture_called is False


# --- focus sweep tool: the original ISO/shutter it restores -----------------------------------

def test_focus_sweep_original_is_the_reread_real_exposure(tmp_path, monkeypatch):
    import focus_sweep as sweep_tool
    from test_focus_sweep import FakeLensCamera
    camera = FakeLensCamera()
    reads = ScriptedReads(camera.settings, 1)
    camera.get_settings = reads
    monkeypatch.setattr(sweep_tool, "STEP_SETTLE_S", 0)
    monkeypatch.setattr(sweep_tool, "preview_sharpness", lambda p: camera.sharpness_by_path[str(p)])
    monkeypatch.setattr(sweep_tool, "preview_mean_luma", lambda p: camera.luma_by_path[str(p)])
    sweep = sweep_tool.run_sweep(camera, step=3, dest_dir=tmp_path / "sweep")
    assert sweep["original"]["iso"] == "3200" and sweep["original"]["shutter"] == "1/8"
    assert sweep["original_read"]["reads"] == 2
    assert camera.settings["iso"] == "3200" and camera.settings["shutter"] == "1/8"  # restored


def test_focus_sweep_persistent_placeholder_refuses_before_any_write(tmp_path, monkeypatch):
    import focus_sweep as sweep_tool
    from test_focus_sweep import FakeLensCamera
    camera = FakeLensCamera()
    camera.get_settings = ScriptedReads(camera.settings, "always")
    monkeypatch.setattr(sweep_tool, "STEP_SETTLE_S", 0)
    with pytest.raises(PlaceholderSettingsError):
        sweep_tool.run_sweep(camera, step=3, dest_dir=tmp_path / "sweep")
    assert not any(isinstance(event, tuple) and event[0] == "set" for event in camera.events)


# --- MCP auto_expose: plans its starting shutter from the read -------------------------------

def test_auto_expose_never_plans_from_a_placeholder_read(tmp_path):
    from PIL import Image
    from sonyremote.autoexpose import auto_expose
    live = {"aperture": "f/8", "iso": "100", "shutter": "1/60", "exposure_comp": "0"}
    reads = ScriptedReads(live, "always")
    writes, shots = [], []

    class Body:
        def shooting_mode(self): return "M"
        def get_settings(self): return reads()
        def list_options(self, key): return {"shutter": ["1/250", "1/60", "1/15"], "iso": ["100", "200"]}[key]
        def set_setting(self, key, value): writes.append((key, value)); live[key] = value
        def capture(self, download=True):
            shots.append(1)
            path = tmp_path / f"shot{len(shots)}.jpg"
            Image.new("RGB", (32, 32), (110, 110, 110)).save(path)
            return path

    with pytest.raises(PlaceholderSettingsError):
        auto_expose(Body(), target_mean=112, max_iter=2, max_shutter_s=1 / 30)
    assert writes == [] and shots == []
    reads.remaining = 1
    report = auto_expose(Body(), target_mean=112, max_iter=2)
    assert report["iterations"][0]["settings_rereads"][0]["reads"] == 2
    assert all(placeholder_fields(entry["settings"]) == [] for entry in report["iterations"])


def test_auto_expose_keeps_the_first_frame_when_the_second_read_is_refused(tmp_path):
    from PIL import Image
    from sonyremote.autoexpose import auto_expose
    live = {"aperture": "f/8", "iso": "100", "shutter": "1/60"}
    reads = ScriptedReads(live, 0)
    shots = []

    class Body:
        def shooting_mode(self): return "M"
        def get_settings(self): return reads()
        def list_options(self, key): return {"shutter": ["1/250", "1/60", "1/15"], "iso": ["100", "200"]}[key]
        def set_setting(self, key, value):
            live[key] = value
            reads.remaining = "always"  # after the correction the body answers placeholders
        def capture(self, download=True):
            shots.append(1)
            path = tmp_path / f"shot{len(shots)}.jpg"
            Image.new("RGB", (32, 32), (20, 20, 20)).save(path)  # dark: a correction follows
            return path

    report = auto_expose(Body(), target_mean=112, max_iter=2)
    assert shots == [1] and report["final_image"].endswith("shot1.jpg")
    assert report["settings_read_refused"]["error"] == "placeholder_settings"
    assert report["iterations"][0]["settings"]["iso"] == "100"


# --- handheld: a refused current read skips the adjustment but keeps the frame ---------------

def test_handheld_placeholder_current_read_skips_the_adjustment(tmp_path):
    from handheld import HandheldSession
    from test_handheld import FakeCamera, _frames
    frames_dir = tmp_path / "frames"
    frames_dir.mkdir()
    camera = FakeCamera(_frames(frames_dir, 1))
    reads = ScriptedReads(camera._settings, 0)
    camera.get_settings = reads
    original_listen = camera.listen_for_files

    def listen(*args, **kwargs):
        files = original_listen(*args, **kwargs)
        reads.remaining = "always" if files else 0  # the body answers placeholders after a push
        return files
    camera.listen_for_files = listen
    recommended = []
    session = HandheldSession(lambda: camera, lambda *a: recommended.append(a) or {"iso_label": "800"},
                              max_frames=1, max_duration_s=5, out_dir=tmp_path / "out")
    state = session.run()
    frame = state["frames"][0]
    assert frame["receipt"] and frame["apply_status"] == "skipped"
    assert frame["settings_read_refused"]["error"] == "placeholder_settings"
    assert recommended == [] and not any(call.get("iso") == "800" for call in camera.set_exposure_calls)


# --- MCP capture_photo: a pre-capture placeholder refuses before the shutter ----------------

@pytest.fixture
def mcp_server():
    """Importing server starts its keep-alive thread; stop and join it after the test."""
    import server
    yield server
    server._stop_keepalive()


def test_mcp_capture_photo_refuses_a_placeholder_before_the_shutter(monkeypatch, tmp_path, mcp_server):
    server = mcp_server
    camera = SimpleNamespace(captures=0, last_capture_receipt=None,
                             get_settings=ScriptedReads({"focus_mode": "Automatic"}, "always"))

    def capture(download=True):
        camera.captures += 1
        return tmp_path / "never.jpg"
    camera.capture = capture
    monkeypatch.setattr(server, "_ensure_connected", lambda serial=None: camera)
    monkeypatch.setattr(server, "_reset_camera", lambda: None)
    result = server.capture_photo("A7III0SERIAL")
    assert camera.captures == 0
    # Review round 3: the MCP client gets the structured no-shutter refusal, not a bare exception.
    assert result.is_error is True
    assert result.structured_content["reason"] == "placeholder_settings"
    assert result.structured_content["may_have_fired"] is False and result.structured_content["ok"] is False


# --- second review pass ------------------------------------------------------------------------

def test_focus_bracket_stops_after_a_refused_held_readback_and_keeps_that_frame(tmp_path):
    import focus_bracket
    from test_focus_bracket import ShutterCounter
    camera = _aperture_camera(0)
    decision = focus_bracket.resolve_bracket_aperture(camera, body_model=A7III, lens_model="Tamron 28-75 f/2.8")
    counter = ShutterCounter(camera)

    def capture(frame_dir):
        path = counter(frame_dir)
        camera.get_settings.remaining = "always"  # after frame 0 the body answers placeholders
        return path
    with pytest.raises(PlaceholderSettingsError):
        focus_bracket.run_bracket(camera, frames=3, step=2, direction="far", dest_dir=tmp_path,
                                  capture=capture, aperture=decision)
    import json
    manifest = json.loads((tmp_path / "bracket_manifest.json").read_text())
    assert counter.calls == 1  # no shutter after the refused readback
    assert len(manifest["frames"]) == 1 and manifest["frames"][0]["held_read_refused"]
    assert manifest["status"] == "failed"
    assert manifest["restoration"]["exposure"]["attempted"]  # the original exposure is still restored


def test_engine_remeter_capture_is_preceded_by_a_checked_read(rig):
    # A black meter frame forces a re-meter; after the correction write the body answers
    # placeholders. The re-meter shutter is refused cleanly, the meter frame named as preserved.
    rig.camera.frame_values = [0, 0, 0]
    reads = ScriptedReads(rig.camera.settings, 0)
    rig.camera.get_settings = reads
    original_set_exposure = rig.camera.set_exposure
    captures_before_write = []

    def set_exposure(**settings):
        original_set_exposure(**settings)
        if rig.camera.capture_count == 1:
            captures_before_write.append(rig.camera.capture_count)
            reads.remaining = "always"
    rig.camera.set_exposure = set_exposure
    with pytest.raises(PlaceholderSettingsError) as caught:
        rig.real.shoot("natural", variant_id="natural")
    assert captures_before_write and rig.camera.capture_count == 1
    assert caught.value.purpose == "re-meter preflight readback"
    assert caught.value.as_dict()["earlier_capture_path"]


def test_engine_manual_lens_uses_jpeg_feedback_before_any_meter_shutter(rig):
    rig.camera.settings["aperture"] = "f/0"  # a manual lens: f/0 beside a real ISO and shutter
    shot = rig.real.shoot("natural", variant_id="natural")
    assert shot.evidence["source"] == "jpeg_feedback_fallback"
    assert "reports no aperture" in shot.evidence["fallback_reason"]


def test_session_with_a_legacy_placeholder_baseline_refuses_before_restoring_it(tmp_path, monkeypatch):
    import engine
    import product_sessions
    monkeypatch.setattr(engine, "SHOTS_DIR", tmp_path / "shots")
    monkeypatch.setattr(engine, "_free_usb", lambda: None)
    live = {"iso": "100", "shutter": "1/60", "aperture": "8", "focus_mode": "Automatic"}
    writes, captures = [], []
    Camera = _session_camera(tmp_path, live, ScriptedReads(live, 0), captures)
    Camera.set_exposure = lambda self, **kwargs: writes.append(kwargs)
    Camera.set_setting = lambda self, key, value: writes.append({key: value})
    monkeypatch.setattr(sonyremote, "Camera", Camera)
    runner = product_sessions.runner()
    state = runner.create("hdr", 3, ev_step=1.0, metadata={
        "camera_model": A7II, "camera_port": "usb:001,002", "camera_serial": "A7II00SERIAL"})
    state["restore_baseline"] = {**PLACEHOLDER, "focus_mode": "Automatic"}  # saved before this guard
    state["settings"] = dict(state["restore_baseline"])
    state["restore"] = "failed"
    runner.save(state)
    with pytest.raises(CameraError, match="placeholder camera values"):
        product_sessions.run(state["id"])
    assert writes == [] and captures == []


def test_api_shoot_returns_the_structured_refusal_with_the_earlier_frame(product):
    def refused_shoot(intent=None, **kwargs):
        refusal = PlaceholderSettingsError("Last read: {}.", purpose="before final capture", reads=[{"read": 1}])
        refusal.capture_path = Path("/private/meter.jpg")
        raise refusal
    product.fake.shoot = refused_shoot
    response = product.client.post("/shoot", json={"intent": "natural"}, headers=product.headers)
    assert response.status_code == 409, response.text
    body = response.json()
    assert body["reason"] == "placeholder_settings" and body["may_have_fired"] is False
    assert body["earlier_capture_path"] == "/private/meter.jpg" and body["retry_capture"] is False


def test_auto_expose_refusal_raised_by_a_capture_wrapper_keeps_history(tmp_path):
    from PIL import Image
    from sonyremote.autoexpose import auto_expose
    live = {"aperture": "f/8", "iso": "100", "shutter": "1/60"}
    shots = []

    class Body:
        def shooting_mode(self): return "M"
        def get_settings(self): return dict(live)
        def list_options(self, key): return {"shutter": ["1/250", "1/60", "1/15"], "iso": ["100", "200"]}[key]
        def set_setting(self, key, value): live[key] = value
        def capture(self, download=True):
            if shots:  # e.g. the MCP wrapper's own checked read refuses before the second shutter
                raise PlaceholderSettingsError("wrapper", purpose="before capture", reads=[{"read": 1}])
            shots.append(1)
            path = tmp_path / "shot1.jpg"
            Image.new("RGB", (32, 32), (20, 20, 20)).save(path)
            return path

    report = auto_expose(Body(), target_mean=112, max_iter=2)
    assert shots == [1] and report["final_image"].endswith("shot1.jpg")
    assert report["settings_read_refused"]["purpose"] == "before capture"
    assert len(report["iterations"]) == 1


def test_auto_expose_post_capture_refusal_keeps_its_structured_evidence(tmp_path):
    # Review thread on #79: a persistent placeholder right after a saved frame must be reported as
    # the structured refusal, not flattened into a generic capture warning.
    from PIL import Image
    from sonyremote.autoexpose import auto_expose
    live = {"aperture": "f/8", "iso": "100", "shutter": "1/60"}
    reads = ScriptedReads(live, 0)

    class Body:
        def shooting_mode(self): return "M"
        def get_settings(self): return reads()
        def list_options(self, key): return {"shutter": ["1/250", "1/60", "1/15"], "iso": ["100", "200"]}[key]
        def set_setting(self, key, value): live[key] = value
        def capture(self, download=True):
            path = tmp_path / "shot1.jpg"
            Image.new("RGB", (32, 32), (20, 20, 20)).save(path)
            reads.remaining = "always"  # the post-capture read answers placeholders
            return path

    report = auto_expose(Body(), target_mean=112, max_iter=2)
    assert report["final_image"].endswith("shot1.jpg")
    refused = report["settings_read_refused"]
    assert refused["error"] == "placeholder_settings" and refused["purpose"] == "after capture"
    assert len(refused["reads"]) == 4
    assert report["iterations"][0]["settings"]["iso"] == "100"  # the checked pre-capture read


def test_settings_route_refuses_a_placeholder_before_read_with_the_structured_409(product, monkeypatch):
    # #88's serial-proven settings route and this guard together: the body is proven, then its
    # "before" read keeps answering placeholders; nothing is written and the refusal is structured.
    writes = []

    class Camera:
        def __init__(self, **kwargs): self.serial = kwargs.get("serial")
        def __enter__(self): return self
        def __exit__(self, *args): pass
        def info(self): return SimpleNamespace(model=A7II, port="usb:000,003")
        def get_settings(self): return dict(PLACEHOLDER)
        def set_setting(self, node, value): writes.append((node, value))
    monkeypatch.setattr(sonyremote, "Camera", Camera)
    monkeypatch.setattr(product.engine, "_free_usb", lambda: None)
    product.fake.name = "real"
    response = product.client.post("/api/camera/settings/camera.image_size", headers=product.headers, json={
        "value": "L:24M", "route": "usb_gphoto", "dry_run": False, "camera_model": A7II,
        "camera_port": "usb:000,003", "camera_serial": "A7II00SERIAL"})
    assert response.status_code == 409, response.text
    body = response.json()
    assert body["reason"] == "placeholder_settings" and body["may_have_fired"] is False
    assert writes == []


# --- review round 2 (#79) ----------------------------------------------------------------------

@pytest.mark.parametrize("with_lens", [False, True])
def test_focus_bracket_checks_settings_before_the_first_shutter(tmp_path, with_lens):
    # Without --lens there is no aperture record at all; the bracket must still refuse a
    # placeholder body before frame 0 instead of letting the CLI read 65535/65535 as 1 s.
    import focus_bracket
    from test_focus_bracket import ShutterCounter
    camera = _aperture_camera(0)
    decision = (focus_bracket.resolve_bracket_aperture(camera, body_model=A7III, lens_model="Tamron 28-75 f/2.8")
                if with_lens else None)
    camera.get_settings.remaining = "always"
    counter = ShutterCounter(camera)
    with pytest.raises(PlaceholderSettingsError) as caught:
        focus_bracket.run_bracket(camera, frames=3, step=2, direction="far", dest_dir=tmp_path,
                                  capture=counter, aperture=decision)
    assert counter.calls == 0
    assert caught.value.purpose == "before bracket frame 0"


def test_focus_bracket_checks_settings_before_every_later_shutter(tmp_path):
    import json
    import focus_bracket
    from test_focus_bracket import ShutterCounter
    camera = _aperture_camera(0)
    counter = ShutterCounter(camera)
    reads = camera.get_settings
    original_step = camera.manual_focus_step

    def step(value):
        reads.remaining = "always"  # after frame 0's move the body answers placeholders
        return original_step(value)
    camera.manual_focus_step = step
    with pytest.raises(PlaceholderSettingsError) as caught:
        focus_bracket.run_bracket(camera, frames=3, step=2, direction="far", dest_dir=tmp_path,
                                  capture=counter, aperture=None)
    assert counter.calls == 1 and caught.value.purpose == "before bracket frame 1"
    manifest = json.loads((tmp_path / "bracket_manifest.json").read_text())
    assert len(manifest["frames"]) == 1 and manifest["status"] == "failed"


def test_auto_expose_correction_read_refusal_keeps_the_frame_and_its_evidence(tmp_path):
    from PIL import Image
    from sonyremote.autoexpose import auto_expose
    live = {"aperture": "f/8", "iso": "100", "shutter": "1/60"}
    reads = ScriptedReads(live, 0)
    calls = {"reads": 0}

    def get_settings():
        calls["reads"] += 1
        if calls["reads"] >= 4:  # start, before, after capture are clean; the correction read is not
            reads.remaining = "always"
        return reads()

    class Body:
        def shooting_mode(self): return "M"
        def list_options(self, key): return {"shutter": ["1/250", "1/60", "1/15"], "iso": ["100", "200"]}[key]
        def set_setting(self, key, value): live[key] = value
        def capture(self, download=True):
            path = tmp_path / "shot1.jpg"
            Image.new("RGB", (32, 32), (20, 20, 20)).save(path)
            return path
    body = Body()
    body.get_settings = get_settings
    report = auto_expose(body, target_mean=112, max_iter=2)
    assert report["final_image"].endswith("shot1.jpg")
    assert report["settings_read_refused"]["purpose"] == "auto-expose correction"


def test_engine_final_read_refusal_is_structured_evidence_on_the_kept_photo(rig):
    reads = ScriptedReads(rig.camera.settings, 0)
    rig.camera.get_settings = reads
    original_capture = rig.camera.capture

    def capture(*args, **kwargs):
        path = original_capture(*args, **kwargs)
        if rig.camera.capture_count == 2:
            reads.remaining = "always"  # only after the final photograph
        return path
    rig.camera.capture = capture
    shot = rig.real.shoot("natural", variant_id="natural")
    assert shot.ok and rig.camera.capture_count == 2
    refused = shot.evidence["settings_read_refused"]
    assert refused["purpose"] == "after final capture" and len(refused["reads"]) == 4
    assert shot.evidence["settings_readback"]["source"] == "before_final"


def test_mcp_capture_photo_final_read_refusal_is_structured(monkeypatch, tmp_path, mcp_server):
    from PIL import Image
    server = mcp_server
    image = tmp_path / "captured.jpg"
    Image.new("RGB", (12, 8), "gray").save(image)
    reads = ScriptedReads({"aperture": "f/8", "iso": "100", "shutter": "1/125"}, 0)
    camera = SimpleNamespace(last_capture_receipt=None, get_settings=reads)

    def capture(download=True):
        reads.remaining = "always"
        return image
    camera.capture = capture
    monkeypatch.setattr(server, "_ensure_connected", lambda serial=None: camera)
    monkeypatch.setattr(server, "_reset_camera", lambda: None)
    result = server.capture_photo("A7III0SERIAL")
    assert result.is_error is False
    structured = result.structured_content
    assert structured["settings_read_refused"]["purpose"] == "after capture"
    assert structured["settings"]["iso"] == "100"  # the checked pre-capture read


# --- review round 3 (#79) ----------------------------------------------------------------------

def test_mcp_auto_expose_refuses_a_placeholder_before_the_first_shutter_with_structure(monkeypatch, mcp_server):
    server = mcp_server
    camera = SimpleNamespace(last_capture_receipt=None, verified_serial=None, serial_verification=None,
                             get_settings=ScriptedReads({"focus_mode": "Automatic"}, "always"),
                             shooting_mode=lambda: "M", list_options=lambda key: ["100", "200"])
    shots = []
    camera.capture = lambda *a, **k: shots.append(1)
    monkeypatch.setattr(server, "_ensure_connected", lambda serial=None: camera)
    monkeypatch.setattr(server, "_reset_camera", lambda: None)
    result = server.auto_expose("A7III0SERIAL")
    assert shots == []
    assert result.is_error is True
    assert result.structured_content["reason"] == "placeholder_settings"
    assert result.structured_content["may_have_fired"] is False


def test_engine_movie_keeps_the_recording_and_the_final_read_refusal(tmp_path, monkeypatch):
    import engine
    reads = ScriptedReads({"aperture": "f/4", "iso": "100", "shutter": "1/60"}, 0)

    class Body:
        def __enter__(self): return self
        def __exit__(self, *args): return False
        def info(self): return SimpleNamespace(model=A7II, port="usb:001,002")
        def get_settings(self): return reads()
        def record_native(self, seconds):
            reads.remaining = "always"  # the body answers placeholders after the movie
            return {"recorded_seconds": seconds}
    real = engine.RealEngine(camera_serial="A7II00SERIAL")
    monkeypatch.setattr(real, "_camera", lambda: Body())
    monkeypatch.setattr(engine, "SHOTS_DIR", tmp_path / "shots")
    monkeypatch.setattr(engine, "serial_evidence", lambda cam: {"camera_serial": "A7II00SERIAL"})
    monkeypatch.setattr(engine.intent_brain, "interpret", lambda intent, **kw: {"mood": {}})
    monkeypatch.setattr(sonyremote, "assert_recording_allowed", lambda: None)
    recording = real.record(None, seconds=1)
    assert recording.ok is True and recording.settings["iso"] == "100"  # the checked before-read
    refused = recording.recording["settings_read_refused"]
    assert refused["purpose"] == "after recording" and len(refused["reads"]) == 4


def test_engine_jpeg_feedback_fallback_keeps_the_auto_expose_refusal(rig):
    # Review thread on #79: a manual lens (f/0) takes the JPEG-feedback fallback; when its
    # auto_expose caught a placeholder after the first frame, the saved Shot keeps the refusal.
    rig.camera.settings["aperture"] = "f/0"
    reads = ScriptedReads(rig.camera.settings, 0)
    rig.camera.get_settings = reads
    original_capture = rig.camera.capture

    def capture(*args, **kwargs):
        path = original_capture(*args, **kwargs)
        reads.remaining = "always"  # the post-capture read answers placeholders
        return path
    rig.camera.capture = capture
    shot = rig.real.shoot("natural", variant_id="natural")
    assert shot.evidence["source"] == "jpeg_feedback_fallback" and rig.camera.capture_count == 1
    assert shot.evidence["settings_read_refused"]["purpose"] == "after capture"
