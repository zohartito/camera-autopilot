"""A respawned PTP service must not take the gap between Python and CLI claims."""
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

import sonyremote.camera as module
from sonyremote.camera import Camera, CameraInfo, CaptureOutcomeUncertain
from sonyremote.camera import _release_macos_camera_service as release_service


@pytest.mark.parametrize("platform,expected", [("darwin", 1), ("linux", 0)])
def test_service_release_is_bounded_and_macos_only(monkeypatch, platform, expected):
    command = MagicMock()
    monkeypatch.setattr(module.sys, "platform", platform)
    monkeypatch.setattr(module.subprocess, "run", command)
    release_service()
    assert command.call_count == expected
    if expected:
        command.assert_called_once_with(
            ["killall", "-9", "ptpcamerad"], capture_output=True, timeout=2)


def test_reconnection_releases_service_before_usb_init(monkeypatch):
    identity = ("Sony Alpha-A7 III (PC Control)", "usb:000,004")
    events = []
    low = MagicMock()
    low.get_abilities.return_value.model = identity[0]
    low.get_port_info.return_value.get_path.return_value = identity[1]
    low.init.side_effect = lambda: events.append("claim")
    monkeypatch.setattr(module, "detect", lambda: [identity])
    monkeypatch.setattr(module.gp, "Camera", lambda: low)
    monkeypatch.setattr(module.gp, "PortInfoList", MagicMock())
    monkeypatch.setattr(module.gp, "CameraAbilitiesList", MagicMock())
    monkeypatch.setattr(module, "_release_macos_camera_service", lambda: events.append("release"))
    cam = Camera(model=identity[0], port=identity[1])
    with cam:
        assert cam.info() == CameraInfo(*identity)
    with cam:
        assert cam.info() == CameraInfo(*identity)
    assert events == ["release", "claim", "release", "claim"]


@pytest.mark.parametrize("uncertain", [False, True])
def test_cli_releases_reclaimed_service_after_settle_without_replaying(monkeypatch, tmp_path, uncertain):
    cam = Camera()
    events = []
    ownership = {"blocked": False}

    def settle(_):
        ownership["blocked"] = True  # macOS reclaims the released camera during this gap
        events.append("settle")

    def release():
        ownership["blocked"] = False
        events.append("release")

    def capture(command, **kwargs):
        assert not ownership["blocked"]
        assert "--capture-image-and-download" in command
        events.append("shutter")
        if uncertain:
            return SimpleNamespace(returncode=1, stderr="uncertain transfer", stdout="")
        (kwargs["cwd"] / "DSC00001.JPG").write_bytes(b"fixture JPEG")
        return SimpleNamespace(returncode=0, stderr="", stdout="captured")

    monkeypatch.setattr(cam, "info", lambda: CameraInfo("Sony Alpha-A7 III (PC Control)", "usb:000,004"))
    monkeypatch.setattr(cam, "get_settings", lambda: {"shutter": "1"})
    monkeypatch.setattr(cam, "disconnect", lambda: events.append("disconnect"))
    monkeypatch.setattr(cam, "connect", lambda: events.append("reconnect"))
    monkeypatch.setattr(module.shutil, "which", lambda _: "/fake/gphoto2")
    monkeypatch.setattr(module.time, "sleep", settle)
    monkeypatch.setattr(module, "_release_macos_camera_service", release)
    monkeypatch.setattr(module.subprocess, "run", capture)
    if uncertain:
        with pytest.raises(CaptureOutcomeUncertain) as error:
            cam._capture_sony_action(dest_dir=tmp_path)
        assert error.value.retry_safe is False
    else:
        assert cam._capture_sony_action(dest_dir=tmp_path).is_file()
    assert events == ["disconnect", "settle", "release", "shutter", "reconnect"]
