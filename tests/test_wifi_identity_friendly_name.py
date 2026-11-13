"""Sony device descriptions name the body in friendlyName; modelName is the generic
"SonyImagingDevice". The identity gate must accept the real A7 II and still reject other bodies."""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))
import wifi_a7ii  # noqa: E402

DD = """<?xml version="1.0"?>
<root xmlns="urn:schemas-upnp-org:device-1-0" xmlns:av="urn:schemas-sony-com:av">
 <device>
  <friendlyName>{friendly}</friendlyName>
  <manufacturer>Sony Corporation</manufacturer>
  <modelName>SonyImagingDevice</modelName>
  <UDN>uuid:00000000-0005-0010-8000-aabbccddeeff</UDN>
  <av:X_ScalarWebAPI_DeviceInfo>
   <av:X_ScalarWebAPI_ServiceList>
    <av:X_ScalarWebAPI_Service>
     <av:X_ScalarWebAPI_ServiceType>camera</av:X_ScalarWebAPI_ServiceType>
     <av:X_ScalarWebAPI_ActionList_URL>http://192.168.122.1:8080/sony</av:X_ScalarWebAPI_ActionList_URL>
    </av:X_ScalarWebAPI_Service>
   </av:X_ScalarWebAPI_ServiceList>
  </av:X_ScalarWebAPI_DeviceInfo>
 </device>
</root>
"""


class _Resp:
    def __init__(self, data): self.data = data
    def read(self, n=-1): out, self.data = self.data, b""; return out
    def __enter__(self): return self
    def __exit__(self, *a): return False


@pytest.mark.parametrize("friendly,accepted", [("ILCE-7M2", True), ("ILCE-7M3", False), ("DSC-RX100M3", False)])
def test_identity_uses_friendly_name_when_model_name_is_generic(monkeypatch, friendly, accepted):
    xml = DD.format(friendly=friendly).encode()
    monkeypatch.setattr(wifi_a7ii, "_camera_urlopen", lambda url, timeout=5: _Resp(xml))
    identity = wifi_a7ii.identity_from_dd("http://192.168.122.1:64321/dd.xml")
    assert identity is not None and identity.model_name == friendly
    assert identity.endpoint == "http://192.168.122.1:8080/sony/camera"
    cam = wifi_a7ii.CameraRemote.__new__(wifi_a7ii.CameraRemote)
    cam.endpoint = identity.endpoint
    cam.device_description_url = "http://192.168.122.1:64321/dd.xml"
    cam.identity = None
    cam._identity_verified = False
    if accepted:
        assert cam.verify_identity().model_name == "ILCE-7M2"
    else:
        with pytest.raises(wifi_a7ii.CameraIdentityError):
            cam.verify_identity()


def test_wifi_engine_puts_the_engine_repo_on_the_path_for_sonyremote(monkeypatch):
    """The server runs from backend/; the Wi-Fi engine must reach the repo-root sonyremote
    package exactly like the USB engine does, or shoot() dies with ModuleNotFoundError."""
    import sys as _sys
    import engine as engine_module
    import wifi_engine
    repo = str(engine_module.ENGINE_REPO)
    monkeypatch.setattr(_sys, "path", [p for p in _sys.path if p != repo])
    wifi_engine.WiFiEngine()
    assert repo in _sys.path
    import importlib
    assert importlib.import_module("sonyremote").CaptureOutcomeUncertain is not None
