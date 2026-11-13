"""An uncertain capture keeps its transport evidence: the CLI argv, wall-clock timings, exit or
timeout, bounded output tails, the progress lines seen, the pre-shutter serial check and every
reconnect attempt. /shoot's 409 carries it and writes it to <store>/uncertain/ (0600). Camera-free:
the gphoto2 CLI and the USB bus are fakes; nothing is ever retried."""
from __future__ import annotations

import importlib
import json
import stat
import subprocess
import sys
import threading
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "backend"))

from sonyremote.camera import (Camera, CameraError, CameraInfo, CaptureOutcomeUncertain,  # noqa: E402
                               CLI_OUTPUT_TAIL_CHARS)
import uncertain_receipts  # noqa: E402

MODEL, PORT, SERIAL = "Sony Alpha-A7 III (PC Control)", "usb:000,004", "A7III0SERIAL"
VERIFIED = {"serial": SERIAL, "method": "os_usb_serial_at_connected_port", "os_source": "ioreg",
            "model": MODEL, "port": PORT}


def _capture(tmp_path, run, *, connect=None):
    """Drive _capture_sony_action once against a fake CLI; returns (camera, error or None, run mock)."""
    camera = Camera()
    camera._expected_serial = SERIAL
    camera.quarantine_dir = tmp_path / "store" / "quarantine"
    with patch.object(camera, "info", return_value=CameraInfo(MODEL, PORT)), \
         patch.object(camera, "get_settings", return_value={"shutter": "1/4"}), \
         patch.object(camera, "disconnect"), \
         patch.object(camera, "connect", side_effect=connect or CameraError("[-10] Timeout reading from or writing to the port")), \
         patch("sonyremote.camera.verify_serial_at_port", return_value=dict(VERIFIED)), \
         patch("sonyremote.camera.shutil.which", return_value="/fake/gphoto2"), \
         patch("sonyremote.camera.time.sleep"), \
         patch("sonyremote.camera.subprocess.run", side_effect=run) as fake_run:
        try:
            camera._capture_sony_action(dest_dir=tmp_path / "gallery")
        except CaptureOutcomeUncertain as error:
            return camera, error, fake_run
    return camera, None, fake_run


def test_timeout_carries_the_attempt_receipt_and_never_retries(tmp_path):
    def run(argv, **kwargs):
        # The camera reported a file and the CLI began saving it when the deadline hit.
        (kwargs["cwd"] / "capt0000.jpg").write_bytes(b"partial")
        raise subprocess.TimeoutExpired(argv, kwargs["timeout"],
                                        output=b"New file is in location /capt0000.jpg on the camera\n",
                                        stderr=b"")

    camera, error, fake_run = _capture(tmp_path, run)
    assert error is not None and "timed out waiting for the camera" in str(error)
    assert error.may_have_fired is True and error.retry_safe is False
    fake_run.assert_called_once()  # one shutter command, never a second
    receipt = error.transport_receipt
    assert receipt is camera.last_capture_receipt
    cli = receipt["cli"]
    assert cli["argv"] == ["/fake/gphoto2", "--camera", MODEL, "--port", PORT,
                           "--capture-image-and-download", "--keep"]
    assert cli["outcome"] == "timeout_killed" and cli["exit_code"] is None
    assert cli["timeout_s"] == pytest.approx(18.5)
    assert cli["started_at"].endswith("Z") and cli["ended_at"].endswith("Z")
    assert cli["event_lines"] == ["New file is in location /capt0000.jpg on the camera"]
    assert cli["staged_at_timeout"] == [{"name": "capt0000.jpg", "size": 7}]
    assert receipt["pre_shutter_serial_verification"] == VERIFIED
    assert receipt["capture_deadline"]["confirmed_shutter_setting"] == "1/4"
    timings = receipt["timings"]
    assert {"usb_release_s", "settle_s", "pre_trigger_checks_s", "trigger_and_download_s",
            "reconnect_s", "total_s"} <= set(timings)
    # The reconnect that follows is recorded attempt by attempt (a hung body answers none of them).
    attempts = receipt["reconnect_attempts"]
    assert [attempt["delay_s"] for attempt in attempts] == list(Camera.RECONNECT_ATTEMPT_DELAYS_S)
    assert all(attempt["ok"] is False and "[-10]" in attempt["error"] for attempt in attempts)
    assert receipt["reconnect_ok"] is False
    json.dumps(receipt)  # the 409 body must serialise


def test_timeout_with_no_output_says_the_camera_never_reported_a_file(tmp_path):
    def run(argv, **kwargs):
        raise subprocess.TimeoutExpired(argv, kwargs["timeout"])  # output None, as a silent CLI gives

    camera, error, _ = _capture(tmp_path, run, connect=[CameraError("busy"), CameraInfo(MODEL, PORT)])
    cli = error.transport_receipt["cli"]
    assert cli["event_lines"] == [] and cli["stdout_tail"] == "" and cli["staged_at_timeout"] == []
    attempts = error.transport_receipt["reconnect_attempts"]
    assert [attempt["ok"] for attempt in attempts] == [False, True]
    assert error.transport_receipt["reconnect_ok"] is True


def test_failed_exit_keeps_exit_code_and_bounded_tails(tmp_path):
    noise = "x" * (CLI_OUTPUT_TAIL_CHARS + 500)

    def run(argv, **kwargs):
        return SimpleNamespace(returncode=1, stdout=noise,
                               stderr="*** Error ***\nERROR: Could not capture image.\nERROR: Could not capture.\n")

    _, error, fake_run = _capture(tmp_path, run)
    fake_run.assert_called_once()
    cli = error.transport_receipt["cli"]
    assert cli["outcome"] == "exited" and cli["exit_code"] == 1
    assert len(cli["stdout_tail"]) == CLI_OUTPUT_TAIL_CHARS and cli["stdout_chars"] == len(noise)
    assert cli["event_lines"] == ["*** Error ***", "ERROR: Could not capture image.", "ERROR: Could not capture."]


def test_an_evidence_failure_never_replaces_the_timeout(tmp_path):
    def run(argv, **kwargs):
        (kwargs["cwd"] / "capt0000.jpg").write_bytes(b"partial")
        raise subprocess.TimeoutExpired(argv, kwargs["timeout"])

    with patch("sonyremote.camera._staged_sizes", side_effect=PermissionError("denied")):
        _, error, fake_run = _capture(tmp_path, run)
    fake_run.assert_called_once()
    assert "timed out waiting for the camera" in str(error)
    assert error.capture_path is not None and error.capture_path.name == "capt0000.jpg"
    assert error.transport_receipt["cli"]["evidence_error"] == "PermissionError"


def test_success_records_the_cli_without_changing_delivery(tmp_path):
    def run(argv, **kwargs):
        (kwargs["cwd"] / "DSC00042.JPG").write_bytes(b"jpeg")
        return SimpleNamespace(returncode=0, stderr="",
                               stdout="New file is in location /DSC00042.JPG on the camera\n"
                                      "Saving file as DSC00042.JPG\n")

    camera, error, _ = _capture(tmp_path, run, connect=[CameraInfo(MODEL, PORT)])
    assert error is None
    receipt = camera.last_capture_receipt
    assert receipt["capture_confirmed"] is True and Path(receipt["path"]).read_bytes() == b"jpeg"
    assert receipt["cli"]["exit_code"] == 0 and len(receipt["cli"]["event_lines"]) == 2


@pytest.fixture
def product(tmp_path, monkeypatch):
    monkeypatch.setenv("CAMERA_ENGINE", "mock")
    module = importlib.import_module("app")
    engine = importlib.import_module("engine")
    root = tmp_path / "shots"
    root.mkdir(mode=0o700)
    monkeypatch.setattr(module, "SHOTS_DIR", root)
    monkeypatch.setattr(engine, "SHOTS_DIR", root)
    monkeypatch.setattr(module, "API_TOKEN", "fixture-credential-1234567890")
    monkeypatch.setattr(module, "_camera_lock", threading.Lock())
    monkeypatch.setattr(module, "_capture_budget", module.RequestBudget(limit=100))
    fired = []

    class TimingOutEngine:
        name = "mock"

        def shoot(self, intent=None, **kwargs):
            fired.append(intent)
            failure = CaptureOutcomeUncertain("Sony action capture timed out waiting for the camera.")
            failure.transport_receipt = {"cli": {"argv": ["/fake/gphoto2", "--keep"], "outcome": "timeout_killed",
                                                 "exit_code": None, "event_lines": []},
                                         "capture_path": Path("/not/serialisable/by/default")}
            raise failure

    monkeypatch.setattr(module, "engine", TimingOutEngine())
    with TestClient(module.app) as client:
        yield SimpleNamespace(app=module, root=root, client=client, fired=fired,
                              headers={"X-Camera-Autopilot-Token": module.API_TOKEN})


def test_shoot_409_carries_the_receipt_and_keeps_it_beside_the_store(product):
    response = product.client.post("/shoot", headers=product.headers, json={"intent": "walking"})
    assert response.status_code == 409
    body = response.json()
    assert body["may_have_fired"] is True and body["retry_capture"] is False
    assert body["transport_receipt"]["cli"]["outcome"] == "timeout_killed"
    assert body["transport_receipt"]["capture_path"] == "/not/serialisable/by/default"
    assert product.fired == ["walking"]  # one request, one attempt
    kept = product.root / body["uncertain_receipt"]
    assert kept.parent == product.root / "uncertain"
    assert stat.S_IMODE(kept.stat().st_mode) == 0o600
    assert stat.S_IMODE(kept.parent.stat().st_mode) == 0o700
    record = json.loads(kept.read_text())
    assert record["route"] == "/shoot" and record["response"]["transport_receipt"] == body["transport_receipt"]
    assert "uncertain_receipt" not in record["response"]
    # Never a gallery entry; delete-all removes it.
    gallery = product.client.get("/gallery", headers=product.headers).json()
    assert all("uncertain" not in json.dumps(item) for item in gallery)
    deleted = product.client.request("DELETE", "/photos", headers=product.headers,
                                     json={"confirm": "delete-all-local-photos"})
    assert deleted.status_code == 200, deleted.text
    assert not (product.root / "uncertain").exists()


def test_shoot_409_survives_a_receipt_that_cannot_be_written(product):
    (product.root / "uncertain").symlink_to(product.root.parent, target_is_directory=True)
    response = product.client.post("/shoot", headers=product.headers, json={})
    assert response.status_code == 409
    body = response.json()
    assert body["may_have_fired"] is True and "uncertain_receipt" not in body
    assert "[Errno" in body["uncertain_receipt_error"]  # ELOOP or ENOTDIR: the link is never followed
    assert list(product.root.parent.glob("*.json")) == []  # nothing written through the link


def test_persist_tightens_an_existing_directory(tmp_path):
    (tmp_path / "uncertain").mkdir()
    (tmp_path / "uncertain").chmod(0o777)
    relative = uncertain_receipts.persist(tmp_path, {"response": {}})
    assert stat.S_IMODE((tmp_path / "uncertain").stat().st_mode) == 0o700
    assert stat.S_IMODE((tmp_path / relative).stat().st_mode) == 0o600


def test_json_safe_output_is_strict_utf8_json():
    class Unprintable:
        def __str__(self):
            raise RuntimeError("no")

    safe = uncertain_receipts.json_safe({"a": float("nan"), "b": float("inf"), (1, 2): "x", "p": Path("/p"),
                                         "name": "capt\udcff.jpg", "odd": Unprintable()})
    assert safe == {"a": None, "b": None, "p": "/p", "name": "capt?.jpg", "odd": "<unprintable Unprintable>"}
    json.dumps(safe, ensure_ascii=False, allow_nan=False).encode("utf-8")


def test_an_unencodable_receipt_still_answers_uncertain(product, monkeypatch):
    receipt = {"cli": {}}
    receipt["self"] = receipt  # circular: cannot be encoded

    def shoot(intent=None, **kwargs):
        failure = CaptureOutcomeUncertain("Sony action capture timed out waiting for the camera.")
        failure.transport_receipt = receipt
        raise failure

    monkeypatch.setattr(product.app.engine, "shoot", shoot)
    response = product.client.post("/shoot", headers=product.headers, json={})
    assert response.status_code == 409
    body = response.json()
    assert body["may_have_fired"] is True and body["retry_capture"] is False
    assert body["transport_receipt"] is None and body["evidence_error"] == "ValueError"
    assert (product.root / body["uncertain_receipt"]).is_file()


def test_conftest_redirects_only_the_repository_default_store(tmp_path):
    repository_store = ROOT / "artifacts" / "demo-shots"
    for spelling in (repository_store, ROOT / "artifacts" / ".." / "artifacts" / "demo-shots"):
        kept = uncertain_receipts.persist(spelling, {"response": {}})
        assert not (repository_store / kept).exists()
    chosen = tmp_path / "chosen"
    chosen.mkdir()
    assert (chosen / uncertain_receipts.persist(chosen, {"response": {}})).is_file()
    with pytest.raises(FileNotFoundError):  # a store the test named is never swapped
        uncertain_receipts.persist(ROOT / "artifacts" / "no-such-store", {"response": {}})


def test_an_unprintable_reconnect_error_still_retries():
    class Odd(CameraError):
        def __str__(self):
            raise ValueError("unprintable")

    camera = Camera()
    with patch.object(camera, "connect", side_effect=[Odd(), CameraInfo(MODEL, PORT)]), \
         patch("sonyremote.camera.time.sleep"):
        assert camera._reconnect_with_backoff() == CameraInfo(MODEL, PORT)
    assert [attempt["ok"] for attempt in camera.last_reconnect_attempts] == [False, True]
    assert camera.last_reconnect_attempts[0]["error"] == "Odd"


def test_an_identity_refusal_on_reconnect_keeps_its_type_even_if_the_clock_fails(module_time):
    import sonyremote.camera as camera_module
    from sonyremote.camera import CameraIdentityError

    def broken_clock():
        raise OSError("clock")

    module_time(camera_module, monotonic=broken_clock)
    camera = Camera()
    refusal = CameraIdentityError("wrong body", reason="serial_mismatch")
    with patch.object(camera, "connect", side_effect=refusal):
        with pytest.raises(CameraIdentityError):
            camera._reconnect_with_backoff()
    assert camera.last_reconnect_attempts[0]["ok"] is False


def test_a_receipt_write_error_with_an_unencodable_name_keeps_the_409(product, monkeypatch):
    def refuse(*_args, **_kwargs):
        raise OSError("bad filename: \udcff")

    monkeypatch.setattr(uncertain_receipts, "persist", refuse)
    response = product.client.post("/shoot", headers=product.headers, json={})
    assert response.status_code == 409
    assert response.json()["uncertain_receipt_error"] == "OSError: bad filename: ?"


def test_any_receipt_write_failure_keeps_the_409(product, monkeypatch):
    def overflow(*_args, **_kwargs):
        raise OverflowError("timestamp out of range")

    monkeypatch.setattr(uncertain_receipts, "persist", overflow)
    response = product.client.post("/shoot", headers=product.headers, json={})
    assert response.status_code == 409
    assert response.json()["uncertain_receipt_error"] == "OverflowError: timestamp out of range"
