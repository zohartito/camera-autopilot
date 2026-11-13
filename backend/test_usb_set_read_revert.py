"""Camera-free tests for USB set/read/revert proof planning."""

from __future__ import annotations

import contextlib
import io
import json
import sys
import tempfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "tools"))

import a7ii_usb_set_read_revert
import sonyremote

MODEL = "Sony ILCE-7M2 (Control)"
PORT = "usb:001,004"
SERIAL = "TESTSERIAL01"


class _FakeConfig:
    def __init__(self, camera: "_FakeUsbCamera") -> None:
        self._camera = camera

    def get_child_by_name(self, node: str) -> "_FakeConfig":
        return self

    def get_value(self) -> str:
        return self._camera.value

    def count_choices(self) -> int:
        return len(self._camera.choices)

    def get_choice(self, index: int) -> str:
        return self._camera.choices[index]

    def get_readonly(self) -> int:
        return 0


class _FakeUsbCamera:
    """Stands in for sonyremote.Camera: records construction and writes, never opens USB."""

    constructed: list[dict] = []
    writes: list[tuple[str, str]] = []
    refuse_on_connect: str | None = None
    refuse_on_write: str | None = None

    def __init__(self, model=None, port=None, serial=None, **kwargs) -> None:
        type(self).constructed.append({"model": model, "port": port, "serial": serial, **kwargs})
        self._serial = serial
        self.verified_serial = None
        self.value = "800"
        self.choices = ["640", "800", "1000"]

    def __enter__(self) -> "_FakeUsbCamera":
        if type(self).refuse_on_connect:
            raise sonyremote.CameraIdentityError(
                "fake body at the port reports another serial", reason=type(self).refuse_on_connect,
                expected_serial=self._serial, observed_serial="OTHERBODY", model=MODEL, port=PORT)
        self.verified_serial = self._serial  # the real Camera proves this on connect
        return self

    def __exit__(self, *exc) -> None:
        return None

    def _require(self) -> "_FakeUsbCamera":
        return self

    def get_config(self) -> _FakeConfig:
        return _FakeConfig(self)

    def set_setting(self, node: str, value: str) -> None:
        type(self).writes.append((node, value))
        if type(self).refuse_on_write:
            raise sonyremote.CameraIdentityError(
                "fake reconnect proved another body", reason=type(self).refuse_on_write,
                expected_serial=self._serial, model=MODEL, port=PORT)
        self.value = value


@contextlib.contextmanager
def _fake_camera(*, refuse_on_connect: str | None = None, refuse_on_write: str | None = None):
    _FakeUsbCamera.constructed = []
    _FakeUsbCamera.writes = []
    _FakeUsbCamera.refuse_on_connect = refuse_on_connect
    _FakeUsbCamera.refuse_on_write = refuse_on_write
    original = sonyremote.Camera
    sonyremote.Camera = _FakeUsbCamera
    try:
        yield _FakeUsbCamera
    finally:
        sonyremote.Camera = original


def _run(argv: list[str]) -> tuple[int, str]:
    stdout = io.StringIO()
    with contextlib.redirect_stdout(stdout):
        code = a7ii_usb_set_read_revert.main(argv)
    return code, stdout.getvalue()


def _summary(out_root: Path) -> dict:
    [summary_path] = out_root.glob("*-usb-set-read-revert/summary.json")
    return json.loads(summary_path.read_text(encoding="utf-8"))


def test_auto_target_prefers_nearby_choice_over_extreme_first_choice() -> None:
    assert (
        a7ii_usb_set_read_revert._auto_target(
            {
                "value": "f/2.8",
                "choices": ["f/1", "f/1.4", "f/2", "f/2.8", "f/3.2", "f/3.5"],
            }
        )
        == "f/3.2"
    )
    assert (
        a7ii_usb_set_read_revert._auto_target(
            {
                "value": "800",
                "choices": ["Auto ISO", "400", "640", "800", "1000", "1250"],
            }
        )
        == "1000"
    )
    assert (
        a7ii_usb_set_read_revert._auto_target(
            {
                "value": "1/800",
                "choices": ["1/500", "1/640", "1/800", "1/1000", "1/1250"],
            }
        )
        == "1/1000"
    )
    print("  USB set/read/revert nearby auto-target: ok")


def test_auto_targets_keep_fallback_choices_after_nearby_candidate() -> None:
    assert a7ii_usb_set_read_revert._auto_targets(
        {
            "value": "Average",
            "choices": ["Center Spot", "Average", "Center"],
        }
    ) == ["Center", "Center Spot"]
    print("  USB set/read/revert fallback target list: ok")


def test_values_match_accepts_nearly_equal_range_readback() -> None:
    assert a7ii_usb_set_read_revert._values_match(
        "35.220001220703125",
        "35.22",
        choices=[],
    )
    assert not a7ii_usb_set_read_revert._values_match(
        "Center Weighted",
        "Center",
        choices=["Center Spot", "Average", "Center"],
    )
    print("  USB set/read/revert numeric readback matching: ok")


def test_write_then_snapshot_accepts_delayed_success_after_set_exception() -> None:
    class FakeCamera:
        value = "Single Shot"

        def set_setting(self, node: str, value: str) -> None:
            self.value = value
            raise RuntimeError("immediate verifier read stale value")

    original_snapshot = a7ii_usb_set_read_revert._widget_snapshot

    def fake_snapshot(camera: FakeCamera, node: str) -> dict:
        return {
            "node": node,
            "value": camera.value,
            "readonly": False,
            "choices": ["Single Shot", "Burst"],
        }

    try:
        a7ii_usb_set_read_revert._widget_snapshot = fake_snapshot
        snapshot, error = a7ii_usb_set_read_revert._write_then_snapshot(
            FakeCamera(),
            "capturemode",
            "Burst",
            settle_seconds=0,
        )
    finally:
        a7ii_usb_set_read_revert._widget_snapshot = original_snapshot

    assert snapshot is not None
    assert snapshot["value"] == "Burst"
    assert error is not None
    assert "immediate verifier read stale value" in error
    print("  USB set/read/revert delayed write snapshot: ok")


def test_execute_without_serial_constructs_no_camera() -> None:
    with tempfile.TemporaryDirectory() as tmp, _fake_camera() as camera_class:
        code, stdout = _run(["--setting", "camera.iso", "--execute", "--model", MODEL, "--port", PORT,
                             "--settle-seconds", "0", "--out-dir", tmp])
        assert list(Path(tmp).iterdir()) == []  # no run directory, plan or summary

    assert code == 2
    assert camera_class.constructed == []
    assert camera_class.writes == []
    refusal = json.loads(stdout)
    assert refusal["status"] == "identity_refused"
    assert refusal["setting_writes"] == 0
    assert refusal["identity"]["reason"] == "serial_required"
    assert refusal["identity"]["camera_port"] == PORT
    print("  USB set/read/revert refuses --execute without --serial: ok")


def test_execute_without_model_or_port_constructs_no_camera() -> None:
    for identity_args in (["--port", PORT, "--serial", SERIAL], ["--model", MODEL, "--serial", SERIAL]):
        with tempfile.TemporaryDirectory() as tmp, _fake_camera() as camera_class:
            code, stdout = _run(["--setting", "camera.iso", "--execute", *identity_args,
                                 "--settle-seconds", "0", "--out-dir", tmp])
            assert list(Path(tmp).iterdir()) == []
        assert code == 2
        assert camera_class.constructed == []
        assert json.loads(stdout)["status"] == "invalid"
    print("  USB set/read/revert requires exact --model and --port: ok")


def test_dry_run_needs_no_identity_and_constructs_no_camera() -> None:
    with tempfile.TemporaryDirectory() as tmp, _fake_camera() as camera_class:
        code, stdout = _run(["--setting", "camera.iso", "--json", "--out-dir", tmp])
        assert list(Path(tmp).iterdir()) == []
    assert code == 0
    assert camera_class.constructed == []
    assert json.loads(stdout)["execute"] is False
    print("  USB set/read/revert dry-run stays camera-free: ok")


def test_execute_passes_exact_identity_to_camera() -> None:
    with tempfile.TemporaryDirectory() as tmp, _fake_camera() as camera_class:
        code, _ = _run(["--setting", "camera.iso", "--execute", "--model", MODEL, "--port", PORT,
                        "--serial", SERIAL, "--settle-seconds", "0", "--out-dir", tmp])
        summary = _summary(Path(tmp))

    assert code == 0
    assert camera_class.constructed == [{"model": MODEL, "port": PORT, "serial": SERIAL}]
    assert camera_class.writes == [("iso", "1000"), ("iso", "800")]  # set, then revert
    assert summary["status"] == "complete"
    assert summary["verified_serial"] == SERIAL
    assert summary["ok"] == 1
    print("  USB set/read/revert passes model/port/serial to Camera: ok")


def test_identity_refusal_at_connect_is_terminal_and_writes_nothing() -> None:
    with tempfile.TemporaryDirectory() as tmp, _fake_camera(refuse_on_connect="serial_mismatch") as camera_class:
        code, stdout = _run(["--setting", "camera.iso", "--setting", "camera.white_balance", "--execute",
                             "--model", MODEL, "--port", PORT, "--serial", SERIAL, "--settle-seconds", "0",
                             "--json", "--out-dir", tmp])
        summary = _summary(Path(tmp))
        row_artifacts = sorted(path.name for path in Path(tmp).glob("*/camera.*.json"))

    assert code == 2
    assert len(camera_class.constructed) == 1
    assert camera_class.writes == []
    assert row_artifacts == []
    assert summary["status"] == "identity_refused"
    assert summary["identity"]["reason"] == "serial_mismatch"
    assert summary["identity"]["may_have_fired"] is False
    assert summary["results"] == []
    assert json.loads(stdout)["status"] == "identity_refused"
    print("  USB set/read/revert connect-time identity refusal: ok")


def test_identity_refusal_during_a_write_stops_every_remaining_row() -> None:
    with tempfile.TemporaryDirectory() as tmp, _fake_camera(refuse_on_write="not_present") as camera_class:
        code, _ = _run(["--setting", "camera.iso", "--setting", "camera.white_balance", "--execute",
                        "--model", MODEL, "--port", PORT, "--serial", SERIAL, "--settle-seconds", "0",
                        "--out-dir", tmp])
        summary = _summary(Path(tmp))
        row_artifacts = sorted(path.name for path in Path(tmp).glob("*/camera.*.json"))

    assert code == 2
    assert camera_class.writes == [("iso", "1000")]  # no revert retry, no second target, no second row
    assert row_artifacts == ["camera.iso.json"]
    assert summary["status"] == "identity_refused"
    assert summary["identity"]["reason"] == "not_present"
    print("  USB set/read/revert identity refusal is terminal mid-run: ok")


if __name__ == "__main__":
    test_auto_target_prefers_nearby_choice_over_extreme_first_choice()
    test_auto_targets_keep_fallback_choices_after_nearby_candidate()
    test_values_match_accepts_nearly_equal_range_readback()
    test_write_then_snapshot_accepts_delayed_success_after_set_exception()
    test_execute_without_serial_constructs_no_camera()
    test_execute_without_model_or_port_constructs_no_camera()
    test_dry_run_needs_no_identity_and_constructs_no_camera()
    test_execute_passes_exact_identity_to_camera()
    test_identity_refusal_at_connect_is_terminal_and_writes_nothing()
    test_identity_refusal_during_a_write_stops_every_remaining_row()
    print("usb_set_read_revert tests: PASS")
