"""CAM-02: receipt CLIs start sessions through the actual API, with USB replaced."""
import importlib
from pathlib import Path
import sys

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools" / "receipts"))
import _common as common
import body_press
import night_session
from test_final_wave_product_api import product  # noqa: F401
from test_section1_api import _patch_real_handheld, _real_runner, _night_meter_jpeg, _WideLadderCamera


class Started(Exception):
    """Stop the operator CLI after session creation, before any job submission."""


@pytest.mark.parametrize("tool,planned", [(body_press, False), (night_session, False), (night_session, True)])
def test_receipt_serial_reaches_actual_api(tool, planned, product, tmp_path, monkeypatch):
    _patch_real_handheld(product)
    runner = _real_runner(product)
    monkeypatch.setattr(importlib.import_module("sonyremote.camera"), "Camera", _WideLadderCamera)
    token = tmp_path / "token"
    token.write_text(product.app.API_TOKEN)
    serial = "fixture-serial"
    requests = []

    class APIClient(common.Client):
        def post(self, path, body=None, timeout=None):
            response = product.client.post(path, json=body, headers=self._headers())
            requests.append((path, body))
            expected = 202 if path == "/handheld" else 200
            assert response.status_code == expected, response.text
            assert body["camera_serial"] == serial
            result = response.json()
            if path == "/night-assist/plan":
                assert result["status"] == "ok"
                assert result["session_request"]["camera_serial"] == serial
                assert result["session_request"]["night_assist"]["camera_serial"] == serial
                return response.status_code, result
            if path == "/sessions":
                assert runner.read(result["id"])["metadata"]["camera_serial"] == serial
            raise Started

    monkeypatch.setattr(common, "Client", APIClient)
    args = ["--model", "Sony Alpha-A7 III (PC Control)", "--port", "usb:001,004",
            "--serial", serial, "--token-file", str(token), "--out-dir", str(tmp_path / "receipts")]
    if planned:
        args += ["--meter-image", _night_meter_jpeg(product)]
    try:
        with pytest.raises(Started):
            tool.run(args)
    finally:
        for entry in product.app._handheld_sessions.values():
            entry["cancel"].set()
            entry["thread"].join(5)
    assert [path for path, _ in requests] == (["/night-assist/plan", "/sessions"] if planned
                                             else ["/handheld" if tool is body_press else "/sessions"])


@pytest.mark.parametrize("tool", [body_press, night_session])
def test_receipt_requires_serial_before_reading_token(tool, monkeypatch):
    def forbidden(*args):
        raise AssertionError("missing serial reached token access")
    monkeypatch.setattr(common, "read_token", forbidden)
    with pytest.raises(SystemExit) as error:
        tool.run(["--model", "Sony Alpha-A7 III (PC Control)", "--port", "usb:001,004"])
    assert error.value.code == 2
