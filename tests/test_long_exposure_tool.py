"""tools/long_exposure_capture.py: plan-only by default; with --run one shutter each, never retried.

Camera-free: `sonyremote.camera_for_serial` is replaced by a fake body.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tools"))

import long_exposure_capture as tool
import sonyremote
from sonyremote import CaptureOutcomeUncertain


class FakeBody:
    def __init__(self, out, outcomes):
        self.settings = {"iso": "800", "aperture": "f/4", "shutter": "1/60", "focus_mode": "Automatic"}
        self.placeholder_after_long_write = False   # the A7 III answering placeholders after a write
        self.calls, self.outcomes, self.out = [], list(outcomes), out
        self.serial_verification = {"serial": "A7II00SERIAL"}
        self.last_capture_receipt = None

    def __enter__(self): return self
    def __exit__(self, *exc): return False
    def info(self): return SimpleNamespace(model="Sony ILCE-7M2 (Control)", port="usb:000,004")
    def shooting_mode(self): return "M"
    def get_settings(self): return dict(self.settings)

    def set_exposure(self, **kw):
        self.calls.append(("set_exposure", kw))
        self.settings.update(kw)
        if self.placeholder_after_long_write and kw.get("shutter") not in (None, "1/60"):
            self.settings.update(iso="0", aperture="f/0", shutter="65535/65535")

    def set_setting(self, key, value):
        self.calls.append(("set_setting", key, value))
        self.settings[key] = value

    def reconnect(self):
        self.calls.append(("reconnect",))

    def capture_long_exposure(self, shutter, dest_dir=None):
        self.calls.append(("capture_long_exposure", shutter))
        self.last_capture_receipt = {"route": "long_exposure"}
        outcome = self.outcomes.pop(0)
        if isinstance(outcome, dict):        # the transport receipt this call leaves behind
            self.last_capture_receipt = outcome["receipt"]
            outcome = outcome["raise"]
        if isinstance(outcome, BaseException):
            raise outcome
        path = Path(dest_dir) / f"DSC-{shutter}.JPG"
        path.write_bytes(b"fixture")
        return path


def test_plan_only_never_opens_a_camera(monkeypatch, capsys, tmp_path):
    monkeypatch.setattr(sonyremote, "camera_for_serial", lambda *a, **k: pytest.fail("opened a camera"))
    code = tool.main(["--shutter", "2", "--shutter", "5", "--shutter", "15", "--out", str(tmp_path / "r")])
    assert code == 0
    printed = json.loads(capsys.readouterr().out)
    assert printed["status"] == "plan_only"
    assert [row["event_wait_s"] for row in printed["shots"]] == [19, 25, 45]
    assert [row["process_timeout_s"] for row in printed["shots"]] == [34, 40, 60]
    assert not (tmp_path / "r").exists()


@pytest.mark.parametrize("label", ["1", "1/60", "60", "Bulb", "3", "7", "65535/65535"])  # 3 and 7: off the Sony ladder
def test_a_shutter_the_long_route_cannot_take_is_refused_before_anything(capsys, tmp_path, label):
    assert tool.main(["--shutter", label, "--out", str(tmp_path / "r")]) == 2
    assert json.loads(capsys.readouterr().out)["status"] == "refused"


def test_run_requires_the_serial(capsys, tmp_path):
    assert tool.main(["--shutter", "2", "--out", str(tmp_path / "r"), "--run"]) == 2
    assert json.loads(capsys.readouterr().out)["status"] == "serial_required"


def test_each_shutter_fires_once_and_restoration_reconnects_first(monkeypatch, tmp_path):
    out = tmp_path / "r"
    body = FakeBody(out, [None, None])
    monkeypatch.setattr(sonyremote, "camera_for_serial", lambda serial: body)
    code = tool.main(["--serial", "A7II00SERIAL", "--shutter", "2", "--shutter", "5", "--out", str(out), "--run"])
    assert code == 0
    assert [c for c in body.calls if c[0] == "capture_long_exposure"] == [
        ("capture_long_exposure", "2"), ("capture_long_exposure", "5")]
    fired = body.calls.index(("capture_long_exposure", "2"))
    assert body.calls[fired + 1] == ("reconnect",)
    assert body.settings == {"iso": "800", "aperture": "f/4", "shutter": "1/60", "focus_mode": "Automatic"}
    receipt = json.loads((out / "receipt.json").read_text())
    assert receipt["status"] == "completed"
    assert [s["restore"] for s in receipt["shots"]] == ["verified", "verified"]
    assert all(len(s["sha256"]) == 64 for s in receipt["shots"])


def test_an_uncertain_exposure_stops_the_run_and_defers_restoration(monkeypatch, tmp_path):
    out = tmp_path / "r"
    body = FakeBody(out, [CaptureOutcomeUncertain("fixture timeout")])
    monkeypatch.setattr(sonyremote, "camera_for_serial", lambda serial: body)
    code = tool.main(["--serial", "A7II00SERIAL", "--shutter", "15", "--shutter", "2", "--out", str(out), "--run"])
    assert code == 2
    assert [c for c in body.calls if c[0] == "capture_long_exposure"] == [("capture_long_exposure", "15")]
    fired = body.calls.index(("capture_long_exposure", "15"))
    assert body.calls[fired + 1:] == []
    receipt = json.loads((out / "receipt.json").read_text())
    assert receipt["status"] == "stopped"
    assert receipt["shots"][0]["status"] == "uncertain_not_retried"
    assert receipt["shots"][0]["restore"] == "deferred_until_physical_idle_verified"
    assert receipt["shots"][0]["baseline_to_restore_by_hand"]["shutter"] == "1/60"


def test_out_inside_the_gallery_is_refused(capsys):
    code = tool.main(["--serial", "X", "--shutter", "2", "--out", str(ROOT / "backend" / "shots" / "x"), "--run"])
    assert code == 2
    assert "backend/shots" in json.loads(capsys.readouterr().out)["error"]


def test_settling_is_judged_from_the_trigger_not_from_the_whole_call(monkeypatch, tmp_path, module_time):
    # 65 s of call time can include the pre-drain; only post-trigger time counts toward settling.
    out = tmp_path / "r"
    ticks = iter([0.0, 0.0, 65.0, 65.0, 65.0])
    module_time(tool, monotonic=lambda: next(ticks, 65.0))
    body = FakeBody(out, [{"receipt": {"trigger_commands": 1, "timings": {"trigger_and_download_s": 20.0}},
                           "raise": CaptureOutcomeUncertain("fixture")}])
    monkeypatch.setattr(sonyremote, "camera_for_serial", lambda serial: body)
    tool.main(["--serial", "S", "--shutter", "30", "--out", str(out), "--run"])
    receipt = json.loads((out / "receipt.json").read_text())
    assert receipt["shots"][0]["restore"] == "deferred_until_physical_idle_verified"
    fired = body.calls.index(("capture_long_exposure", "30"))
    assert body.calls[fired + 1:] == []


def test_an_interrupt_during_the_exposure_defers_restoration(monkeypatch, tmp_path):
    out = tmp_path / "r"
    body = FakeBody(out, [{"receipt": {"trigger_commands": 1, "timings": {"trigger_and_download_s": 1.0}},
                           "raise": KeyboardInterrupt()}])
    monkeypatch.setattr(sonyremote, "camera_for_serial", lambda serial: body)
    with pytest.raises(KeyboardInterrupt):
        tool.main(["--serial", "S", "--shutter", "15", "--out", str(out), "--run"])
    receipt = json.loads((out / "receipt.json").read_text())
    assert receipt["shots"][0]["restore"] == "deferred_until_physical_idle_verified"
    fired = body.calls.index(("capture_long_exposure", "15"))
    assert body.calls[fired + 1:] == []


def test_a_refusal_before_the_trigger_restores_at_once(monkeypatch, tmp_path):
    from sonyremote import CameraError
    out = tmp_path / "r"
    body = FakeBody(out, [{"receipt": {"trigger_commands": 0},
                           "raise": CameraError("pending objects; no shutter was issued")}])
    monkeypatch.setattr(sonyremote, "camera_for_serial", lambda serial: body)
    assert tool.main(["--serial", "S", "--shutter", "15", "--out", str(out), "--run"]) == 2
    receipt = json.loads((out / "receipt.json").read_text())
    assert receipt["shots"][0]["status"] == "refused_no_shutter"
    assert receipt["shots"][0]["restore"] == "verified"


@pytest.mark.parametrize("engine_name", ["mock", "real"])
def test_out_inside_the_configured_gallery_root_is_refused(monkeypatch, capsys, tmp_path, engine_name):
    # Review (PR #81): the guard hard-coded backend/shots, so with CAMERA_AUTOPILOT_SHOTS_DIR set an
    # --out inside the live gallery was accepted (a later delete-all would remove the receipts).
    gallery = tmp_path / "gallery"
    monkeypatch.setenv("CAMERA_AUTOPILOT_SHOTS_DIR", str(gallery))
    monkeypatch.setenv("CAMERA_ENGINE", engine_name)
    monkeypatch.setattr(sonyremote, "camera_for_serial", lambda *a, **k: pytest.fail("opened a camera"))
    for out in (gallery, gallery / "receipts" / "a7ii"):
        assert tool.main(["--serial", "S", "--shutter", "2", "--out", str(out), "--run"]) == 2
        assert "gallery" in json.loads(capsys.readouterr().out)["error"]
    assert not gallery.exists()


PLACEHOLDERS = {"iso": "0", "aperture": "f/0", "shutter": "65535/65535"}


def test_a_placeholder_baseline_refuses_before_any_write_or_shutter(monkeypatch, tmp_path, module_time):
    # Review (PR #81): the raw baseline could record A7 III placeholders and write them back.
    import sonyremote.camera as camera_mod
    module_time(camera_mod, sleep=lambda _s: None)
    out = tmp_path / "r"
    body = FakeBody(out, [None])
    body.settings.update(PLACEHOLDERS)
    monkeypatch.setattr(sonyremote, "camera_for_serial", lambda serial: body)
    assert tool.main(["--serial", "S", "--shutter", "2", "--out", str(out), "--run"]) == 2
    assert body.calls == []                                    # no write, no shutter
    receipt = json.loads((out / "receipt.json").read_text())
    assert receipt["status"] == "failed" and "placeholder" in receipt["error"].lower()


def test_a_placeholder_readback_before_the_trigger_refuses_and_restores_the_real_baseline(monkeypatch, tmp_path, module_time):
    import sonyremote.camera as camera_mod
    module_time(camera_mod, sleep=lambda _s: None)
    out = tmp_path / "r"
    body = FakeBody(out, [None])
    body.placeholder_after_long_write = True
    monkeypatch.setattr(sonyremote, "camera_for_serial", lambda serial: body)
    assert tool.main(["--serial", "S", "--shutter", "2", "--out", str(out), "--run"]) == 2
    assert not any(c[0] == "capture_long_exposure" for c in body.calls)
    receipt = json.loads((out / "receipt.json").read_text())
    shot = receipt["shots"][0]
    assert shot["status"] == "refused_no_shutter" and "placeholder" in shot["error"].lower()
    assert shot["restore"] == "verified"
    assert body.settings == {"iso": "800", "aperture": "f/4", "shutter": "1/60", "focus_mode": "Automatic"}
    for call in body.calls:                                    # a placeholder is never written back
        if call[0] == "set_exposure":
            assert not set(call[1].values()) & set(PLACEHOLDERS.values())


@pytest.mark.parametrize("focus", [None, "", "  "])
def test_a_baseline_without_a_focus_mode_refuses_before_any_write(monkeypatch, tmp_path, focus):
    # Review (PR #81): with no focus restore target the tool switched to Manual, fired, and reported
    # restore "verified" while leaving the body in Manual.
    out = tmp_path / "r"
    body = FakeBody(out, [None])
    body.settings["focus_mode"] = focus
    monkeypatch.setattr(sonyremote, "camera_for_serial", lambda serial: body)
    assert tool.main(["--serial", "S", "--shutter", "2", "--out", str(out), "--run"]) == 2
    assert body.calls == []                                    # no write, no shutter
    receipt = json.loads((out / "receipt.json").read_text())
    assert receipt["status"] == "refused_focus_mode_unknown"
