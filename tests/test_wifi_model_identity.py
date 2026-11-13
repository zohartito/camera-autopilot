"""A7 II Smart Remote identity must be established before any camera mutation."""
from __future__ import annotations

import io
import json
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))
import wifi_a7ii as wifi

ENDPOINT = "http://192.168.122.1:8080/sony/camera"
DESCRIPTION = "http://192.168.122.1:64321/dd.xml"


def description(model="ILCE-7M2", udn="uuid:a7ii-test", endpoint="http://192.168.122.1:8080/sony"):
    return (f'<root xmlns="urn:schemas-upnp-org:device-1-0" xmlns:av="urn:schemas-sony-com:av">'
            f'<device><modelName>{model}</modelName><UDN>{udn}</UDN>'
            '<av:X_ScalarWebAPI_Service>'
            '<av:X_ScalarWebAPI_ServiceType>camera</av:X_ScalarWebAPI_ServiceType>'
            f'<av:X_ScalarWebAPI_ActionList_URL>{endpoint}</av:X_ScalarWebAPI_ActionList_URL>'
            '</av:X_ScalarWebAPI_Service></device></root>').encode()


@pytest.mark.parametrize("model,udn", [("ILCE-7M3", "uuid:a7iii"), ("", "uuid:unknown"), ("ILCE-7M2", "")])
@pytest.mark.parametrize("method", ["begin_session", "take_picture"])
def test_wrong_or_unknown_body_never_receives_start_or_shutter(model, udn, method):
    calls = []

    def open_(url, *, data=None, timeout=None):
        calls.append((url, data))
        assert data is None, "wrong-body endpoint received a camera command"
        return io.BytesIO(description(model, udn))

    with patch.dict(wifi._DISCOVERED_IDENTITIES, {}, clear=True), patch.object(wifi, "_camera_urlopen", open_):
        camera = wifi.CameraRemote(ENDPOINT, device_description_url=DESCRIPTION)
        with pytest.raises(wifi.CameraIdentityError, match="requires ILCE-7M2"):
            getattr(camera, method)()
        assert len(calls) == 1


def test_endpoint_lookup_carries_model_and_udn_then_rechecks_before_shutter():
    requests = []

    def open_(url, *, data=None, timeout=None):
        requests.append((url, data))
        if data is None:
            return io.BytesIO(description())
        return io.BytesIO(json.dumps({"result": [["http://192.168.122.1/new.JPG"]]}).encode())

    with patch.dict(wifi._DISCOVERED_IDENTITIES, {}, clear=True), patch.object(wifi, "_camera_urlopen", open_):
        endpoint = wifi.endpoint_from_dd(DESCRIPTION)
        camera = wifi.CameraRemote(endpoint)
        assert camera.identity.model_name == "ILCE-7M2"
        assert camera.identity.udn == "uuid:a7ii-test"
        assert camera.take_picture() == ["http://192.168.122.1/new.JPG"]
    assert sum(data is None for _, data in requests) == 2
    assert [json.loads(data)["method"] for _, data in requests if data] == ["actTakePicture"]


def test_explicit_endpoint_cannot_skip_identity_even_when_live_check_disabled():
    with patch.dict(wifi._DISCOVERED_IDENTITIES, {}, clear=True), \
         patch.object(wifi, "discover", return_value=None), \
         patch.object(wifi, "_camera_urlopen") as request:
        camera = wifi.connect(camera_endpoint=ENDPOINT, require_live=False)
        request.assert_not_called()
        with pytest.raises(wifi.CameraIdentityError, match="identity unknown"):
            camera.set_iso("100")
        request.assert_not_called()


def test_explicit_endpoint_live_connection_requires_same_host_description():
    with patch.dict(wifi._DISCOVERED_IDENTITIES, {}, clear=True), \
         patch.object(wifi, "discover", return_value="http://192.168.1.33/dd.xml"), \
         patch.object(wifi, "_camera_urlopen") as request:
        assert wifi.connect(camera_endpoint=ENDPOINT) is None
        request.assert_not_called()


def test_description_for_another_endpoint_is_rejected_before_mutation():
    with patch.dict(wifi._DISCOVERED_IDENTITIES, {}, clear=True), \
         patch.object(wifi, "_camera_urlopen", return_value=io.BytesIO(
             description(endpoint="http://192.168.122.1:9999/sony"))):
        camera = wifi.CameraRemote(ENDPOINT, device_description_url=DESCRIPTION)
        with pytest.raises(wifi.CameraIdentityError, match="does not identify this camera endpoint"):
            camera.begin_session()


def test_new_udn_at_same_address_stops_next_shutter():
    commands = []
    reads = []

    def open_(url, *, data=None, timeout=None):
        if data is None:
            reads.append(url)
            return io.BytesIO(description(udn="uuid:first" if len(reads) == 1 else "uuid:replacement"))
        commands.append(json.loads(data)["method"])
        return io.BytesIO(b'{"result":[0]}')

    with patch.dict(wifi._DISCOVERED_IDENTITIES, {}, clear=True), patch.object(wifi, "_camera_urlopen", open_):
        camera = wifi.CameraRemote(ENDPOINT, device_description_url=DESCRIPTION)
        camera.start_rec_mode()
        with pytest.raises(wifi.CameraIdentityError, match="UDN changed"):
            camera.take_picture()
    assert commands == ["startRecMode"]
