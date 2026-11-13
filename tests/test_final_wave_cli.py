"""CLI routes through the same bounded product flow without hardware or retries."""
from pathlib import Path
from unittest.mock import patch
from types import SimpleNamespace
import sys

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "backend"))
import engine
import intent_brain
import product_capture
import shoot
from sonyremote import CameraError, CaptureOutcomeUncertain


@pytest.fixture
def cli(tmp_path, monkeypatch):
    monkeypatch.setattr(sys, "argv", ["shoot.py", "cool blue"])
    monkeypatch.setenv("CAMERA_MODEL", "Sony ILCE-7M2 (Control)")
    monkeypatch.setenv("CAMERA_PORT", "usb:fixture")
    monkeypatch.delenv("AUTOPILOT_REFLEXION", raising=False)
    monkeypatch.setattr(engine, "SHOTS_DIR", tmp_path)
    image = tmp_path / "actual.jpg"
    image.write_bytes(b"fixture photo")
    plan = intent_brain.preview("cool blue")
    result = engine.Shot(True, image.name, "Captured", "computed", plan["mood"], {}, evidence={})
    with patch.object(engine.RealEngine, "status", return_value={"ready": True}), \
            patch.object(shoot, "_open_photo") as opened, \
            patch.object(intent_brain, "interpret", return_value=plan) as interpreted:
        yield SimpleNamespace(image=image, plan=plan, result=result, opened=opened, interpreted=interpreted)


def test_cli_passes_one_immutable_plan_and_exact_camera_selection_to_shared_product(cli):
    with patch.object(product_capture, "shoot", return_value=cli.result) as capture:
        assert shoot.main() == 0
    capture.assert_called_once()
    camera = capture.call_args.args[0]
    assert camera.camera_model == "Sony ILCE-7M2 (Control)"
    assert camera.camera_port == "usb:fixture"
    assert capture.call_args.kwargs["applied_plan"] is cli.plan
    assert capture.call_args.kwargs["refine"] is False
    cli.interpreted.assert_called_once_with("cool blue")
    cli.opened.assert_called_once_with(cli.image)


def test_cli_refinement_uses_shared_cap_and_does_not_run_legacy_loop(cli, monkeypatch):
    monkeypatch.setattr(sys, "argv", ["shoot.py", "--auto-correct", "cool blue"])
    with patch.object(product_capture, "shoot", return_value=cli.result) as capture, \
            patch.object(shoot, "_run_reflexion", side_effect=AssertionError("duplicate loop")):
        assert shoot.main() == 0
    assert capture.call_args.kwargs["refine"] is True
    capture.assert_called_once()


@pytest.mark.parametrize("error", [CameraError("transport stopped"),
    CaptureOutcomeUncertain("download stopped", capture_path=Path("/private/preserved.jpg"))])
def test_cli_never_retries_camera_failures_or_uncertain_capture(cli, error, capsys):
    with patch.object(product_capture, "shoot", side_effect=error) as capture:
        assert shoot.main() == 1
    capture.assert_called_once()
    assert "No automatic retry" in capsys.readouterr().out
    cli.opened.assert_not_called()


def test_cli_requires_selection_when_both_cameras_match(cli):
    with patch.object(engine.RealEngine, "status", return_value={
            "ready": False, "selection_required": True,
            "cameras": [{"model": "A7II", "port": "usb:a"}, {"model": "A7III", "port": "usb:b"}]}), \
            patch.object(product_capture, "shoot", side_effect=AssertionError("ambiguous camera")) as capture:
        assert shoot.main() == 1
    capture.assert_not_called()


def test_shared_product_reuses_cli_plan_without_second_interpretation(cli):
    def capture(self, intent, target_override=None, variant_id=None, applied_plan=None):
        assert applied_plan == cli.plan
        assert applied_plan is not cli.plan
        return cli.result
    with patch.object(engine.RealEngine, "shoot", capture), patch.object(engine, "save_shot_record"):
        assert shoot.main() == 0
    cli.interpreted.assert_called_once()


def test_legacy_reflexion_helper_remains_available_but_caps_at_two_calls(cli):
    import vision_critique
    calls = []
    def capture(target):
        calls.append(target)
        return {"ok": True, "final_image": str(cli.image)}
    with patch.object(vision_critique, "critique", return_value={
            "exposure": "too dark", "brightness_delta": 10, "issues": ["dim"]}):
        report = shoot._run_reflexion(capture, 112, "cool blue")
    assert len(calls) == 2
    assert report["final_image"] == str(cli.image)


def test_cli_delivers_saved_nonconverged_photo_with_zero_exit_to_avoid_shell_retry(cli, capsys):
    cli.result.ok = False
    with patch.object(product_capture, "shoot", return_value=cli.result) as capture:
        assert shoot.main() == 0
    capture.assert_called_once()
    cli.opened.assert_called_once_with(cli.image)
    assert "Quality target was not met" in capsys.readouterr().out
