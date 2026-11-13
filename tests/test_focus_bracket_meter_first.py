"""The focus bracket meters first; it never fires at whatever exposure the last capture left behind.

Field defect (receipt R-1001, 1 Oct 2026): the A7 III still held a night exposure (ISO 2500 · 1/30 ·
f/4, left by a meter-first /shoot restoring its pre-meter exposure) and a 3-frame daylight focus
bracket came out ~4.6 stops over. Fixed stacks on /shoot have metered first since #72; now the
bracket takes ONE meter frame through the same path (`engine.RealEngine.meter_baseline`, bounded
re-meter included), fires at the metered exposure and restores the pre-meter exposure.
`--baseline current` keeps the old behaviour. Fake cameras only; nothing opens USB.
"""
from __future__ import annotations

import io
import json
from pathlib import Path
from types import SimpleNamespace
import sys

import numpy as np
import pytest
from PIL import Image

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tests"))
sys.path.insert(0, str(ROOT / "backend"))
sys.path.insert(0, str(ROOT / "tools"))
sys.path.insert(0, str(ROOT))

import constraint_cards  # noqa: E402
import focus_bracket  # noqa: E402
import focus_bracket_capture as tool  # noqa: E402
import remeter  # noqa: E402
from sonyremote.camera import CameraIdentityError, CaptureOutcomeUncertain  # noqa: E402
from test_final_wave_engine import FakeCamera, rig  # noqa: E402,F401  (fake-camera engine fixture)

MODEL = "Sony Alpha-A7 III (PC Control)"
PORT = "usb:001,009"
SERIAL = "A7III0SERIAL"
TAMRON = "Tamron 28-75 f/2.8"
NIGHT = {"iso": "2500", "aperture": "4", "shutter": "1/30"}      # what the body held on 1 Oct
DAYLIGHT = {"iso": "100", "aperture": "4", "shutter": "1/1000"}  # what the meter recommends
EXPOSURE = ("iso", "aperture", "shutter")


def _jpeg(seed: int) -> bytes:
    pixels = np.random.default_rng(seed).integers(0, 256, (48, 64, 3), dtype="uint8")
    stream = io.BytesIO()
    Image.fromarray(pixels).save(stream, "JPEG", quality=90)
    return stream.getvalue()


class BracketBody(FakeCamera):
    """The engine's metering fake plus the A7 III focus adapters the bracket drives.

    `shots` records every shutter with the exposure the body held when it fired."""

    def __init__(self, root, *, manual_node=True):
        super().__init__(root, MODEL)
        self.settings.update(NIGHT)
        self.manual_node = manual_node
        self.shots: list[tuple[str, dict]] = []
        self.focus_mode_writes: list[str] = []
        self.timeline: list[str] = []    # focus-mode writes, AF half-presses and shutters, in order
        self.steps: list[int] = []
        self.previews = 0
        self.uncertain_at = None        # shutter index whose outcome is uncertain
        self.identity_refused_at = None  # shutter index refused by the serial check (nothing fires)
        self.last_capture_receipt = None

    def info(self):
        return SimpleNamespace(model=self.model, port=PORT)

    def list_options(self, setting):
        if setting == "iso":
            return ["100", "200", "400", "800", "1600", "2500", "3200"]
        if setting == "aperture":
            return ["4", "5.6", "8", "11"]
        if setting == "shutter":
            return ["1/2000", "1/1000", "1/500", "1/250", "1/125", "1/60", "1/30", "1/15", "1/8",
                    "1/4", "1/2", "1"]
        return super().list_options(setting)

    def focus_capabilities(self):
        nodes = {"focusmode": {"value": self.settings["focus_mode"], "readonly": False, "type": 5},
                 "manualfocus": ({"value": "0", "readonly": False, "type": 3, "range": [-7.0, 7.0, 1.0]}
                                 if self.manual_node else None),
                 "autofocus": {"value": "2", "readonly": False, "type": 4},
                 "focusindication": {"value": "Unlock", "readonly": True, "type": 5}}
        return {"model": self.model, "port": PORT, "nodes": nodes, "manual_focus_route": self.manual_node}

    def set_setting(self, setting, value):
        super().set_setting(setting, value)
        return value  # the readback, as Camera.set_setting returns it

    def get_focus_mode(self):
        return self.settings["focus_mode"]

    def set_focus_mode(self, name):
        self.focus_mode_writes.append(name)
        self.timeline.append(f"focus_mode={name}")
        self.settings["focus_mode"] = name
        return name

    def capture_preview(self, dest: Path):
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(_jpeg(self.previews))
        self.previews += 1
        return dest

    def manual_focus_step(self, value):
        self.steps.append(value)
        return {"node": "manualfocus", "sent": value, "readback": "0", "elapsed_s": 0.0}

    def autofocus_half_press(self, hold_s):
        self.timeline.append("af_half_press")
        return "Focus Locked"

    def capture(self, download=True, dest_dir=None):
        index = len(self.shots)
        if index == self.identity_refused_at:
            raise CameraIdentityError("fixture", reason="serial_mismatch", expected_serial=SERIAL,
                                      observed_serial="OTHER", model=MODEL, port=PORT)
        exposure = {key: self.settings[key] for key in EXPOSURE}
        self.timeline.append(f"shutter@{self.settings['focus_mode']}")
        if index == self.uncertain_at:
            self.shots.append(("uncertain", exposure))
            raise CaptureOutcomeUncertain("fixture: the CLI timed out after the trigger")
        if dest_dir is None:  # the engine's meter frame: an 8-bit grey level per frame_values
            self.shots.append(("meter", exposure))
            return super().capture(download)
        self.shots.append(("bracket", exposure))
        path = Path(dest_dir) / f"DSC{index:05d}.JPG"
        path.write_bytes(_jpeg(100 + index))
        self.last_capture_receipt = {"transport": "fixture", "path": str(path)}
        return path


@pytest.fixture
def body(rig, monkeypatch, module_time):
    camera = BracketBody(rig.camera.root)
    opened = []

    def factory(model=None, port=None, serial=None):
        opened.append({"model": model, "port": port, "serial": serial})
        return camera
    monkeypatch.setattr(tool, "Camera", factory)
    monkeypatch.setattr(tool, "focus_stack", lambda paths: np.full((16, 24, 3), 128, dtype=np.uint8))
    module_time(focus_bracket, sleep=lambda _s: None)
    # Without a lens profile the meter never stops down past the confirmed aperture (f/4 here).
    card = constraint_cards.ConstraintCard("SHUTTER", "1/1000s", "Daylight test scene", [
        constraint_cards.SubFactor("Scene Movement", "unavailable", "pending")])
    rig.a7iii.return_value = constraint_cards.Recommendation(
        100, 4, 1 / 1000, [card], iso_label="100", aperture_label="4", shutter_label="1/1000")
    return SimpleNamespace(camera=camera, opened=opened, a7iii=rig.a7iii)


def _run(capsys, out, *extra, serial=True):
    argv = ["--model", MODEL, "--port", PORT, "--frames", "3", "--step", "3", "--direction", "far",
            "--out", str(out), *(["--serial", SERIAL] if serial else []), *extra]
    code = tool.main(argv)
    return code, json.loads(capsys.readouterr().out)


def _held(camera):
    return {key: camera.settings[key] for key in EXPOSURE}


def test_default_meters_once_then_brackets_at_the_metered_exposure(body, capsys, tmp_path):
    out = tmp_path / "bracket"
    code, payload = _run(capsys, out)
    assert code == 0 and payload["status"] == "complete", payload
    # One meter frame at the inherited night exposure, then every bracket frame at the meter's.
    assert body.camera.shots == [("meter", NIGHT)] + [("bracket", DAYLIGHT)] * 3
    body.a7iii.assert_called_once()
    assert payload["physical_exposure_count"] == 4
    assert payload["exposure_budget"] == {"baseline_meter_exposures": 1,
                                          "extra_meter_exposures": remeter.MAX_REMETER_PASSES,
                                          "bracket_frames": 3,
                                          "max_physical_exposures": 4 + remeter.MAX_REMETER_PASSES}
    manifest = json.loads((out / "bracket_manifest.json").read_text())
    baseline = manifest["baseline"]
    assert baseline["source"] == "metered"
    assert baseline["inherited"] == NIGHT and baseline["metered"] == DAYLIGHT
    assert baseline["meter_physical_exposures"] == 1 and len(baseline["meter_passes"]) == 1
    assert baseline["meter_restore"]["status"] == "unchanged"   # the meter frame needed no write
    assert payload["baseline"] == baseline
    # Every frame read back the metered exposure, and the body is left as it was found.
    assert manifest["aperture"]["held_constant"] is True
    assert manifest["aperture"]["held"] == NIGHT
    assert set(manifest["restoration"]["exposure"]["attempted"]) == set(EXPOSURE)
    assert manifest["restoration"]["exposure"]["error"] is None and manifest["restore_error"] is None
    assert _held(body.camera) == NIGHT
    assert body.camera.settings["focus_mode"] == "Automatic"
    assert body.opened == [{"model": MODEL, "port": PORT, "serial": SERIAL}]  # one proven connection


def test_baseline_current_fires_no_meter_frame_and_keeps_the_old_behaviour(body, capsys, tmp_path):
    out = tmp_path / "bracket"
    code, payload = _run(capsys, out, "--baseline", "current")
    assert code == 0 and payload["status"] == "complete", payload
    assert body.camera.shots == [("bracket", NIGHT)] * 3
    body.a7iii.assert_not_called()
    assert body.camera.exposure_writes == [] and body.camera.setting_writes == []
    assert payload["physical_exposure_count"] == 3
    assert payload["exposure_budget"]["max_physical_exposures"] == 3
    manifest = json.loads((out / "bracket_manifest.json").read_text())
    assert manifest["baseline"] == {"source": "inherited", "inherited": NIGHT, "metered": None}
    assert manifest["aperture"] is None and "exposure" not in manifest["restoration"]


def test_an_uncertain_meter_frame_stops_before_any_bracket_frame_and_is_never_retried(body, capsys, tmp_path):
    body.camera.uncertain_at = 0
    out = tmp_path / "bracket"
    code, payload = _run(capsys, out)
    assert code == 2 and payload["status"] == "uncertain", payload
    assert payload["phase"] == "meter" and payload["retry_safe"] is False
    assert payload["bracket_frames_fired"] == 0
    assert body.camera.shots == [("uncertain", NIGHT)]                 # one shutter, no retry
    assert body.camera.steps == [] and body.camera.exposure_writes == [] and body.camera.setting_writes == []
    assert body.camera.focus_mode_writes == ["Manual", "Automatic"]   # frozen for the meter, restored
    manifest = json.loads((out / "bracket_manifest.json").read_text())
    assert manifest["status"] == "failed" and manifest["phase"] == "meter" and manifest["frames"] == []
    assert manifest["meter_failure"]["may_have_fired"] is True
    assert manifest["baseline"]["inherited"] == NIGHT and manifest["baseline"]["metered"] is None
    assert payload["meter_restore"]["status"] in ("completed", "unchanged")
    assert _held(body.camera) == NIGHT


def test_a_meter_that_stays_saturated_is_refused_before_any_bracket_frame_and_restored(body, capsys, tmp_path):
    body.camera.frame_values = [255]        # every meter pass is blown, even after the re-meter
    out = tmp_path / "bracket"
    code, payload = _run(capsys, out)
    assert code == 1 and payload["status"] == "meter_refused", payload
    assert payload["bracket_frames_fired"] == 0 and payload["retry_safe"] is False
    kinds = [kind for kind, _ in body.camera.shots]
    assert kinds and set(kinds) == {"meter"} and len(kinds) <= 1 + remeter.MAX_REMETER_PASSES
    assert payload["baseline"]["meter_reliability"]["status"] == "low"
    assert payload["baseline"]["meter_restore"]["status"] == "completed"
    assert "no bracket frame was fired" in payload["error"]
    assert body.camera.steps == [] and body.camera.focus_mode_writes == ["Manual", "Automatic"]
    manifest = json.loads((out / "bracket_manifest.json").read_text())
    assert manifest["status"] == "failed" and manifest["frames"] == []
    assert manifest["baseline"]["meter_reliability"]["status"] == "low"
    assert "exposure" not in manifest["restoration"]   # the bracket wrote no exposure
    assert _held(body.camera) == NIGHT


def test_a_restore_failure_after_the_bracket_is_reported_and_every_field_still_attempted(body, capsys, tmp_path):
    camera = body.camera
    original = camera.set_setting
    calls = []

    def set_setting(key, value):
        calls.append((key, value))
        if key == "aperture" and value == NIGHT["aperture"] and len(calls) > 3:
            raise RuntimeError("aperture dial locked")
        return original(key, value)
    camera.set_setting = set_setting
    out = tmp_path / "bracket"
    code, payload = _run(capsys, out)
    assert code == 1 and payload["status"] == "complete_restoration_failed", payload
    assert [kind for kind, _ in camera.shots] == ["meter", "bracket", "bracket", "bracket"]
    manifest = json.loads((out / "bracket_manifest.json").read_text())
    exposure = manifest["restoration"]["exposure"]
    assert "aperture" in exposure["error"]
    assert exposure["readback"] == {"iso": NIGHT["iso"], "shutter": NIGHT["shutter"]}
    assert camera.settings["iso"] == NIGHT["iso"] and camera.settings["shutter"] == NIGHT["shutter"]


def test_lens_sweet_spot_is_applied_to_the_metered_exposure_with_compensation(body, capsys, tmp_path):
    out = tmp_path / "bracket"
    code, payload = _run(capsys, out, "--lens", TAMRON)
    assert code == 0 and payload["status"] == "complete", payload
    manifest = json.loads((out / "bracket_manifest.json").read_text())
    decision = manifest["aperture"]
    assert decision["reason"] == "lens_sweet_spot" and decision["baseline_source"] == "metered"
    # Compensated from the metered f/4 · 1/1000 · ISO 100, not from the night exposure on the body.
    assert decision["compensation"]["from"] == {"aperture": "4", "shutter": "1/1000", "iso": "100"}
    fired = {key: decision[key] for key in EXPOSURE}
    assert fired["aperture"] != DAYLIGHT["aperture"]
    assert body.camera.shots == [("meter", NIGHT)] + [("bracket", fired)] * 3
    assert abs(decision["compensation"]["residual_stops"]) <= focus_bracket.EXPOSURE_MATCH_TOLERANCE_STOPS + 1e-9
    assert _held(body.camera) == NIGHT


def test_an_unprofiled_lens_brackets_at_the_metered_aperture(body, capsys, tmp_path):
    out = tmp_path / "bracket"
    code, payload = _run(capsys, out, "--lens", "FE 24-105mm F4 G OSS")
    assert code == 0, payload
    decision = json.loads((out / "bracket_manifest.json").read_text())["aperture"]
    assert decision["reason"] == "unknown_lens_kept_current"
    assert "metered 4 with shutter 1/1000" in decision["note"]
    assert body.camera.shots == [("meter", NIGHT)] + [("bracket", DAYLIGHT)] * 3


def test_dry_run_writes_nothing_fires_nothing_and_states_the_budget(body, capsys, tmp_path):
    out = tmp_path / "bracket"
    code, payload = _run(capsys, out, "--dry-run", serial=False)
    assert code == 0 and payload["status"] == "dry_run"
    assert payload["meter_frame"] is True and payload["baseline"] == "metered"
    assert payload["exposure_budget"]["max_physical_exposures"] == 1 + remeter.MAX_REMETER_PASSES + 3
    assert "One meter frame fires first" in payload["meter_note"]
    assert payload["inherited"] == NIGHT and payload["shooting_mode"] == "M"
    assert payload["setting_writes"] == 0 and payload["shutter_fired"] == 0
    assert body.camera.shots == [] and body.camera.exposure_writes == [] and body.camera.setting_writes == []
    assert body.camera.focus_mode_writes == [] and body.camera.steps == []
    body.a7iii.assert_not_called()
    assert not out.exists()
    code, payload = _run(capsys, out, "--dry-run", "--baseline", "current", serial=False)
    assert code == 0 and payload["meter_frame"] is False
    assert payload["exposure_budget"] == {"baseline_meter_exposures": 0, "extra_meter_exposures": 0,
                                          "bracket_frames": 3, "max_physical_exposures": 3}
    assert body.camera.shots == [] and not out.exists()


def test_dry_run_warns_the_meter_needs_the_mode_dial_at_m(body, capsys, tmp_path):
    body.camera.shooting_mode = lambda: "A"
    _, payload = _run(capsys, tmp_path / "bracket", "--dry-run", serial=False)
    assert "refuses unless it is at M" in payload["meter_note"] and body.camera.shots == []


def test_no_serial_is_refused_before_the_camera_is_opened(body, capsys, tmp_path):
    code, payload = _run(capsys, tmp_path / "bracket", serial=False)
    assert code == 2 and "--serial" in payload["error"]
    assert body.opened == [] and body.camera.shots == []


def test_a_serial_refused_at_the_meter_shutter_fires_nothing_and_writes_no_exposure(body, capsys, tmp_path):
    body.camera.identity_refused_at = 0
    out = tmp_path / "bracket"
    code, payload = _run(capsys, out)
    assert code == 1 and payload["status"] == "failed" and payload["phase"] == "meter", payload
    assert payload["refusal"]["reason"] == "serial_mismatch"
    assert payload["bracket_frames_fired"] == 0
    assert body.camera.shots == [] and body.camera.exposure_writes == [] and body.camera.setting_writes == []
    assert body.camera.steps == []
    assert json.loads((out / "bracket_manifest.json").read_text())["status"] == "failed"


def test_a_serial_refused_at_connect_opens_nothing_further(body, capsys, tmp_path, monkeypatch):
    def refuse(model=None, port=None, serial=None):
        raise CameraIdentityError("fixture", reason="serial_mismatch", expected_serial=serial,
                                  observed_serial="OTHER", model=model, port=port)
    monkeypatch.setattr(tool, "Camera", refuse)
    code, payload = _run(capsys, tmp_path / "bracket")
    assert code == 1 and payload["status"] == "failed" and "serial_mismatch" in payload["error"]
    assert body.camera.shots == [] and body.camera.exposure_writes == []


def test_a_body_without_the_manualfocus_route_is_refused_before_the_meter_frame(body, capsys, tmp_path):
    body.camera.manual_node = False
    code, payload = _run(capsys, tmp_path / "bracket")
    assert code == 1 and "manualfocus" in payload["error"] and payload["phase"] == "setup"
    assert body.camera.focus_mode_writes == []
    assert body.camera.shots == [] and body.camera.exposure_writes == []
    body.a7iii.assert_not_called()


def test_the_meter_frame_fires_with_focus_frozen_at_the_brackets_start_position(body, capsys, tmp_path):
    # A meter capture in an AF mode would refocus, and a --sweep plan's offsets assume the start
    # position: focus is switched to Manual before the meter shutter and AF runs only in the restore.
    code, _ = _run(capsys, tmp_path / "bracket")
    assert code == 0
    timeline = body.camera.timeline
    assert timeline[0] == "focus_mode=Manual"
    assert [event for event in timeline if event.startswith("shutter")] == ["shutter@Manual"] * 4
    assert timeline[-2:] == ["focus_mode=Automatic", "af_half_press"]
    assert timeline.count("af_half_press") == 1


def test_an_interrupt_during_the_re_meter_restores_the_pre_meter_exposure(body, capsys, tmp_path):
    camera = body.camera
    camera.frame_values = [255]                      # the first meter frame is blown: a re-meter follows
    original = camera.capture

    def interrupted(download=True, dest_dir=None):
        if len(camera.shots) == 1:
            camera.shots.append(("interrupted", {key: camera.settings[key] for key in EXPOSURE}))
            raise KeyboardInterrupt
        return original(download=download, dest_dir=dest_dir)
    camera.capture = interrupted
    out = tmp_path / "bracket"
    with pytest.raises(KeyboardInterrupt):
        tool.main(["--model", MODEL, "--port", PORT, "--serial", SERIAL, "--frames", "3", "--step", "3",
                   "--direction", "far", "--out", str(out)])
    assert [kind for kind, _ in camera.shots] == ["meter", "interrupted"]
    assert camera.shots[1][1] != NIGHT              # the re-meter had moved the exposure
    assert _held(camera) == NIGHT and camera.focus_mode_writes == ["Manual", "Automatic"]
    manifest = json.loads((out / "bracket_manifest.json").read_text())
    assert manifest["frames"] == [] and manifest["phase"] == "meter"
    assert manifest["meter_failure"]["meter_restore"]["status"] == "completed"
    assert manifest["meter_failure"]["may_have_fired"] is True
    assert manifest["baseline"]["inherited"] == NIGHT and manifest["baseline"]["metered"] is None


def test_a_failure_after_a_completed_meter_keeps_the_meter_evidence_in_the_manifest(body, capsys, tmp_path, monkeypatch):
    from sonyremote.camera import CameraError

    def unreadable(*args, **kwargs):
        raise CameraError("settings read failed after the meter")
    monkeypatch.setattr(tool, "resolve_bracket_aperture", unreadable)
    out = tmp_path / "bracket"
    code, payload = _run(capsys, out)
    assert code == 1 and payload["status"] == "failed" and payload["phase"] == "bracket", payload
    assert body.camera.shots == [("meter", NIGHT)]
    assert body.camera.focus_mode_writes == ["Manual", "Automatic"] and _held(body.camera) == NIGHT
    manifest = json.loads((out / "bracket_manifest.json").read_text())
    assert manifest["frames"] == [] and manifest["baseline"]["metered"] == DAYLIGHT
    assert manifest["baseline"] == payload["baseline"]
    assert manifest["meter_failure"]["meter_exposures_fired"] == 1
    assert manifest["meter_failure"]["may_have_fired"] is True
    assert manifest["meter_failure"]["meter_restore"]["status"] == "unchanged"


def test_an_interrupt_after_a_completed_meter_keeps_the_meter_evidence_in_the_manifest(body, tmp_path, monkeypatch):
    def interrupted(*args, **kwargs):
        raise KeyboardInterrupt
    monkeypatch.setattr(tool, "resolve_bracket_aperture", interrupted)
    out = tmp_path / "bracket"
    with pytest.raises(KeyboardInterrupt):
        tool.main(["--model", MODEL, "--port", PORT, "--serial", SERIAL, "--frames", "3", "--step", "3",
                   "--direction", "far", "--out", str(out)])
    assert body.camera.shots == [("meter", NIGHT)] and _held(body.camera) == NIGHT
    manifest = json.loads((out / "bracket_manifest.json").read_text())
    assert manifest["frames"] == [] and manifest["baseline"]["metered"] == DAYLIGHT
    assert manifest["meter_failure"]["meter_exposures_fired"] == 1
    assert manifest["meter_failure"]["may_have_fired"] is True


def test_an_output_path_that_is_a_file_is_refused_before_the_meter_frame(body, capsys, tmp_path):
    out = tmp_path / "bracket"
    out.write_text("not a directory")
    code, payload = _run(capsys, out)
    assert code == 1 and payload["status"] == "failed" and "not a directory" in payload["error"]
    assert body.camera.shots == [] and body.camera.focus_mode_writes == [] and body.camera.exposure_writes == []


def test_a_used_output_directory_is_refused_before_the_meter_frame(body, capsys, tmp_path):
    out = tmp_path / "bracket"
    out.mkdir()
    (out / "bracket_manifest.json").write_text("{}")
    code, payload = _run(capsys, out)
    assert code == 1 and "fresh --out" in payload["error"]
    assert body.camera.shots == [] and body.camera.exposure_writes == []


def test_run_bracket_refuses_a_metered_baseline_without_its_decision(tmp_path):
    camera = BracketBody(tmp_path)
    baseline = {"source": "metered", "inherited": NIGHT, "metered": DAYLIGHT}
    with pytest.raises(ValueError, match="aperture decision"):
        focus_bracket.run_bracket(camera, frames=2, step=1, direction="far", dest_dir=tmp_path / "out",
                                  capture=lambda _d: pytest.fail("no shutter"), baseline=baseline)
    assert camera.focus_mode_writes == [] and not (tmp_path / "out").exists()
