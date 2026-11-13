"""Camera-free tests for the A7 II Smart Remote client."""

from __future__ import annotations

import json
import io
import sys
import urllib.request
from collections.abc import Callable
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "backend"))

import wifi_a7ii


class FakeResponse:
    def __init__(self, payload: dict[str, Any]):
        self.payload = json.dumps(payload).encode("utf-8")

    def read(self) -> bytes:
        return self.payload


def _with_fake_urlopen(handler: Callable[[str, dict[str, Any], float | None], dict[str, Any]], fn):
    original = urllib.request.urlopen
    original_camera_urlopen = wifi_a7ii._camera_urlopen
    original_discover = wifi_a7ii.discover
    calls = []

    def fake_urlopen(url, data=None, timeout=None):
        if data is None:
            return io.BytesIO(b'<root><device><modelName>ILCE-7M2</modelName>'
                              b'<UDN>uuid:test-camera</UDN><X_ScalarWebAPI_Service>'
                              b'<X_ScalarWebAPI_ServiceType>camera</X_ScalarWebAPI_ServiceType>'
                              b'<X_ScalarWebAPI_ActionList_URL>http://fake/sony</X_ScalarWebAPI_ActionList_URL>'
                              b'</X_ScalarWebAPI_Service></device></root>')
        payload = json.loads(data.decode("utf-8")) if data else {}
        calls.append((url, payload, timeout))
        return FakeResponse(handler(str(url), payload, timeout))

    urllib.request.urlopen = fake_urlopen
    wifi_a7ii._camera_urlopen = fake_urlopen
    wifi_a7ii.discover = lambda *args: "http://fake/dd.xml"
    try:
        fn(calls)
    finally:
        urllib.request.urlopen = original
        wifi_a7ii._camera_urlopen = original_camera_urlopen
        wifi_a7ii.discover = original_discover


def test_begin_session_and_discovered_method_helpers() -> None:
    api_list = [
        "getAvailableApiList",
        "getAvailableIsoSpeedRate",
        "getSupportedCameraFunction",
        "setIsoSpeedRate",
        "setTouchAFPosition",
        "actTakePicture",
    ]

    def handler(_url: str, payload: dict[str, Any], _timeout: float | None) -> dict[str, Any]:
        method = payload["method"]
        if method == "getAvailableApiList":
            return {"result": [api_list], "id": payload["id"]}
        if method == "startLiveview":
            return {"result": ["http://fake/liveview"], "id": payload["id"]}
        return {"result": [0], "id": payload["id"]}

    def run(calls):
        camera = wifi_a7ii.CameraRemote("http://fake/sony/camera")
        apis = camera.begin_session()
        assert apis == api_list
        assert [call[1]["method"] for call in calls[:3]] == [
            "startRecMode",
            "startLiveview",
            "getAvailableApiList",
        ]
        assert camera.safe_capability_methods() == [
            "getAvailableApiList",
            "getAvailableIsoSpeedRate",
            "getSupportedCameraFunction",
        ]
        assert camera.setter_candidates() == ["setIsoSpeedRate", "setTouchAFPosition"]

    _with_fake_urlopen(handler, run)
    print("  Smart Remote begin_session + discovered helpers: ok")


def test_connect_liveness() -> None:
    def ok_handler(_url: str, payload: dict[str, Any], _timeout: float | None) -> dict[str, Any]:
        assert payload["method"] == "getAvailableApiList"
        return {"result": [["getAvailableApiList"]], "id": payload["id"]}

    def run_ok(_calls):
        camera = wifi_a7ii.connect(camera_endpoint="http://fake/sony/camera")
        assert camera is not None
        assert camera.endpoint == "http://fake/sony/camera"

    _with_fake_urlopen(ok_handler, run_ok)

    original = urllib.request.urlopen
    original_camera_urlopen = wifi_a7ii._camera_urlopen

    def failing_urlopen(*_args, **_kwargs):
        raise OSError("not reachable")

    urllib.request.urlopen = failing_urlopen
    wifi_a7ii._camera_urlopen = failing_urlopen
    try:
        assert wifi_a7ii.connect(camera_endpoint="http://fake/sony/camera") is None
    finally:
        urllib.request.urlopen = original
        wifi_a7ii._camera_urlopen = original_camera_urlopen
    print("  Smart Remote connect liveness: ok")


if __name__ == "__main__":
    test_begin_session_and_discovered_method_helpers()
    test_connect_liveness()
    print("wifi_a7ii tests: PASS")
