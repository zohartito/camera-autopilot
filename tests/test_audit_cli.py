"""CAM-01: the CLI default and explicit mock mode never enumerate USB."""
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT), str(ROOT / "backend")]
import engine
import intent_brain
import shoot
import sonyremote


@pytest.mark.parametrize("mode", [None, "mock"])
def test_cli_mock_never_constructs_real_engine_or_discovers_usb(mode, tmp_path, monkeypatch):
    if mode is None:
        monkeypatch.delenv("CAMERA_ENGINE", raising=False)
    else:
        monkeypatch.setenv("CAMERA_ENGINE", mode)
    monkeypatch.delenv("AUTOPILOT_REFLEXION", raising=False)
    monkeypatch.setattr(sys, "argv", ["shoot.py", "warm"])
    monkeypatch.setattr(engine, "SHOTS_DIR", tmp_path)
    monkeypatch.setattr(intent_brain, "interpret", intent_brain.preview)
    opened = []
    monkeypatch.setattr(shoot, "_open_photo", opened.append)

    def forbidden(*args, **kwargs):
        raise AssertionError("mock CLI reached real-engine construction or USB discovery")

    monkeypatch.setattr(engine, "RealEngine", forbidden)
    monkeypatch.setattr(sonyremote, "detect_identified", forbidden)
    assert shoot.main() == 0
    assert len(opened) == 1 and opened[0].is_file()
    assert list(tmp_path.glob("*.json")), "mock shot must be persisted"
