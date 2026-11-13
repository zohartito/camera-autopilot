"""Every uncertain capture the engine builds itself carries the transport receipt it has (diagnostics).

PR #111 attached `transport_receipt` to the Sony CLI path; review noted four engine constructions
that raised their own `CaptureOutcomeUncertain` without it: the meter and re-meter frames kept but
reconnect failed, and the JPEG-feedback fallback (reconnect failed, or processing stopped after a
capture). Nothing here changes when a capture is uncertain or whether anything is retried.
"""
from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import patch

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "backend"))

from sonyremote import CameraError, CaptureOutcomeUncertain
from test_final_wave_engine import rig  # noqa: F401 — the camera-free engine fixture


def _receipt_on(rig, capture_number, *, cli="fixture"):
    """The fake body records a receipt per capture; capture `capture_number`'s reconnect fails."""
    original = rig.camera.capture

    def capture(**kwargs):
        path = original(**kwargs)
        failed = rig.camera.capture_count == capture_number
        rig.camera.last_capture_receipt = {"reconnect_ok": not failed, "path": str(path),
                                           "cli": {"argv": [cli, str(rig.camera.capture_count)]}}
        return path
    rig.camera.capture = capture


def test_meter_reconnect_failure_carries_the_meter_receipt(rig):
    _receipt_on(rig, 1)
    with pytest.raises(CaptureOutcomeUncertain) as caught:
        rig.real.shoot(None, variant_id="natural")
    receipt = caught.value.transport_receipt
    assert receipt["reconnect_ok"] is False and receipt["cli"]["argv"][1] == "1"
    assert receipt["path"] == str(caught.value.capture_path)
    assert caught.value.may_have_fired is True and caught.value.retry_safe is False
    assert rig.camera.capture_count == 1                       # nothing more fired


def test_remeter_reconnect_failure_carries_the_remeter_receipt(rig):
    rig.camera.frame_values = [255, 112]                        # a saturated first meter frame
    _receipt_on(rig, 2)
    with pytest.raises(CaptureOutcomeUncertain, match="Re-metering") as caught:
        rig.real.shoot(None, variant_id="natural")
    receipt = caught.value.transport_receipt
    assert receipt["reconnect_ok"] is False and receipt["cli"]["argv"][1] == "2"
    assert receipt["path"] == str(caught.value.capture_path)
    assert rig.camera.capture_count == 2


def test_fallback_reconnect_failure_carries_its_receipt(rig):
    _receipt_on(rig, 1)
    with patch.object(rig.real, "_brain_shoot", side_effect=ImportError("model unavailable")):
        with pytest.raises(CaptureOutcomeUncertain, match="Fallback JPEG") as caught:
            rig.real.shoot(None, variant_id="natural")
    assert caught.value.transport_receipt["reconnect_ok"] is False
    assert caught.value.transport_receipt["path"] == str(caught.value.capture_path)
    assert rig.camera.capture_count == 1


def test_fallback_processing_failure_carries_the_last_capture_receipt(rig):
    _receipt_on(rig, None)
    with patch.object(rig.real, "_brain_shoot", side_effect=ImportError("model unavailable")), \
            patch("sonyremote.autoexpose.measure_jpeg", side_effect=ValueError("invalid pixels")):
        with pytest.raises(CaptureOutcomeUncertain, match="Fallback processing") as caught:
            rig.real.shoot(None, variant_id="natural")
    receipt = caught.value.transport_receipt
    assert receipt["reconnect_ok"] is True and receipt["cli"]["argv"][1] == "1"
    assert rig.camera.capture_count == 1


@pytest.mark.parametrize("records_a_receipt", [True, False])
def test_a_fallback_capture_call_that_raises_never_borrows_an_earlier_receipt(rig, records_a_receipt):
    stale = {"reconnect_ok": True, "path": "an earlier request"}
    rig.camera.last_capture_receipt = stale

    def capture(**kwargs):
        rig.camera.capture_count += 1
        if records_a_receipt:
            rig.camera.last_capture_receipt = {"cli": {"exit_code": 1}}
        raise CameraError("gphoto2 capture failed")
    rig.camera.capture = capture
    with patch.object(rig.real, "_brain_shoot", side_effect=ImportError("model unavailable")):
        with pytest.raises(CaptureOutcomeUncertain, match="Fallback processing") as caught:
            rig.real.shoot(None, variant_id="natural")
    expected = {"cli": {"exit_code": 1}} if records_a_receipt else None
    assert caught.value.transport_receipt == expected
    assert rig.camera.capture_count == 1                       # never retried
