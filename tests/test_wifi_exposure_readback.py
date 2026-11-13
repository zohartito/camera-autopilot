"""Sony exposure setters may report applied values only after exact current-value readback."""
from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import patch

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))
import wifi_a7ii
import wifi_autopilot

CONTROLS = [("set_iso", "setIsoSpeedRate", "getIsoSpeedRate", "400"),
            ("set_aperture", "setFNumber", "getFNumber", "8.0"),
            ("set_shutter", "setShutterSpeed", "getShutterSpeed", "1/125")]


@pytest.mark.parametrize("method,setter,getter,target", CONTROLS)
def test_exposure_setters_require_matching_current_value(method, setter, getter, target):
    camera = wifi_a7ii.CameraRemote("http://camera/sony/camera")
    with patch.object(camera, "call", side_effect=[{"result": [0]}, {"result": [target]}]) as call:
        assert getattr(camera, method)(target) == target
    assert [item.args[0] for item in call.call_args_list] == [setter, getter]


@pytest.mark.parametrize("method,setter,getter,target", CONTROLS)
@pytest.mark.parametrize("response", [{"error": [403, "Forbidden"]}, {"result": []},
                                       {"result": [False]}, {"result": [1]}])
def test_rejected_or_missing_ack_never_becomes_applied(method, setter, getter, target, response):
    camera = wifi_a7ii.CameraRemote("http://camera/sony/camera")
    with patch.object(camera, "call", return_value=response) as call:
        with pytest.raises(wifi_a7ii.CameraSettingError) as error:
            getattr(camera, method)(target)
        assert error.value.verified is False
        call.assert_called_once_with(setter, [target])


@pytest.mark.parametrize("response", [{"error": [12, "No such method"]}, {}, {"result": []},
                                       {"result": [None]}, {"result": ["400", ["100", "400"]]}])
def test_unsupported_or_malformed_getter_leaves_write_unverified(response):
    camera = wifi_a7ii.CameraRemote("http://camera/sony/camera")
    with patch.object(camera, "call", side_effect=[{"result": [0]}, response]) as call:
        with pytest.raises(wifi_a7ii.CameraSettingError, match="not verified"):
            camera.set_iso("400")
    assert call.call_count == 2


def test_delayed_readback_uses_read_only_polling_without_repeating_setter():
    camera = wifi_a7ii.CameraRemote("http://camera/sony/camera")
    responses = [{"result": [0]}, {"result": ["100"]}, {"result": ["200"]}, {"result": ["400"]}]
    with patch.object(camera, "call", side_effect=responses) as call, patch("wifi_a7ii.time.sleep"):
        assert camera.set_iso("400") == "400"
    assert [item.args[0] for item in call.call_args_list] == ["setIsoSpeedRate"] + ["getIsoSpeedRate"] * 3


def test_stalled_readback_is_bounded_and_never_reports_applied():
    camera = wifi_a7ii.CameraRemote("http://camera/sony/camera")

    def response(method, *args, **kwargs):
        return {"result": [0]} if method == "setIsoSpeedRate" else {"result": ["100"]}

    with patch.object(camera, "call", side_effect=response) as call, patch("wifi_a7ii.time.sleep"):
        with pytest.raises(wifi_a7ii.CameraSettingError, match="stayed at '100'"):
            camera.set_iso("400")
    methods = [item.args[0] for item in call.call_args_list]
    assert methods.count("setIsoSpeedRate") == 1
    assert methods.count("getIsoSpeedRate") == 20


def test_snap_set_does_not_claim_a_camera_rejected_setting():
    camera = wifi_a7ii.CameraRemote("http://camera/sony/camera")

    def response(method, *args, **kwargs):
        if method == "getAvailableIsoSpeedRate":
            return {"result": ["100", ["100", "200", "400"]]}
        if method == "setIsoSpeedRate":
            return {"error": [403, "Forbidden"]}
        raise AssertionError(f"unexpected call {method}")

    with patch.object(camera, "call", side_effect=response):
        assert wifi_autopilot._snap_set(camera, "400", "getAvailableIsoSpeedRate", camera.set_iso) is None
