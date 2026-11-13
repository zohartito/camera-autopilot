"""A nearly black or white meter frame must be flagged, not trusted silently."""
import sys
from pathlib import Path

import numpy as np
from PIL import Image

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))
import live_verify  # noqa: E402
from test_final_wave_engine import rig  # noqa: E402,F401  (fake-camera engine fixture)


def _jpeg(path: Path, value: int) -> str:
    Image.fromarray(np.full((40, 60, 3), value, dtype=np.uint8)).save(path, quality=90)
    return str(path)


def test_black_and_white_meter_frames_are_low_reliability(tmp_path):
    black = live_verify.meter_reliability(_jpeg(tmp_path / "black.jpg", 1))
    white = live_verify.meter_reliability(_jpeg(tmp_path / "white.jpg", 254))
    mid = live_verify.meter_reliability(_jpeg(tmp_path / "mid.jpg", 118))
    assert black["status"] == "low" and "black" in black["reason"]
    assert white["status"] == "low" and "white" in white["reason"]
    assert mid["status"] == "ok" and mid["reason"] is None and 110 < mid["mean_luma"] < 126


def test_engine_records_meter_reliability_and_explains_low(rig, monkeypatch):
    shot = rig.real.shoot(None, variant_id="natural")
    assert shot.evidence["meter_reliability"]["status"] in ("ok", "low")
    monkeypatch.setattr(live_verify, "meter_reliability",
                        lambda path: {"status": "low", "mean_luma": 1.0,
                                      "reason": "meter frame is nearly black; the EV estimate is a noise-floor guess"})
    shot = rig.real.shoot(None, variant_id="natural")
    assert shot.evidence["meter_reliability"]["status"] == "low"
    assert "Refine" in shot.summary
