"""Body-shutter event listening (Camera.drain_events / Camera.listen_for_files) — no hardware.

A fake gphoto2 module and a fake gp.Camera drive the event channel deterministically. The host
must never issue a shutter here, so the fake camera's ``capture`` raises if it is ever touched.
"""
from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "backend"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import sonyremote.camera as camera_mod
from sonyremote.camera import Camera, CameraError
from _runner import run_module_tests

# Event-type constants mirror libgphoto2; values are arbitrary but must be distinct.
_TIMEOUT = 1
_FILE_ADDED = 2
_FILE_CHANGED = 5


class _FakeGPhoto2Error(Exception):
    pass


class _FakeCameraFile:
    def __init__(self, folder: str, name: str):
        self._folder = folder
        self._name = name

    def save(self, path: str) -> None:
        Path(path).write_bytes(f"bytes:{self._folder}/{self._name}".encode())


class _FakeGPCamera:
    """A gp.Camera stand-in: replays scripted events, downloads on request, never captures."""

    def __init__(self, events, *, fail_download_on=None):
        # events: list of (event_type, data). Exhausted -> perpetual TIMEOUT.
        self._events = list(events)
        self._index = 0
        self._fail_download_on = fail_download_on
        self.downloaded: list[tuple[str, str]] = []
        self.capture_called = False

    def wait_for_event(self, _ms):
        if self._index < len(self._events):
            event = self._events[self._index]
            self._index += 1
            return event
        return (_TIMEOUT, None)

    def file_get(self, folder, name, filetype):
        if self._fail_download_on is not None and (folder, name) == self._fail_download_on:
            raise _FakeGPhoto2Error("simulated download failure")
        self.downloaded.append((folder, name))
        return _FakeCameraFile(folder, name)

    def capture(self, *args, **kwargs):  # pragma: no cover - must never run
        self.capture_called = True
        raise AssertionError("listen_for_files must never trigger the shutter")


def _fake_gp():
    return SimpleNamespace(
        GP_EVENT_TIMEOUT=_TIMEOUT,
        GP_EVENT_FILE_ADDED=_FILE_ADDED,
        GP_EVENT_FILE_CHANGED=_FILE_CHANGED,
        GP_FILE_TYPE_NORMAL=1,
        GPhoto2Error=_FakeGPhoto2Error,
    )


def _file_event(folder: str, name: str):
    return (_FILE_ADDED, SimpleNamespace(folder=folder, name=name))


def _camera_with(events, *, fake_gp=None, **camera_kwargs):
    """A connected Camera whose underlying gp.Camera is the scripted fake."""
    cam = Camera()
    cam._camera = _FakeGPCamera(events, **camera_kwargs)
    return cam, fake_gp or _fake_gp()


# --- listen_for_files -------------------------------------------------------

def test_file_added_downloads_file(tmp_path=None):
    dest = _tmp("listen_one")
    cam, gp = _camera_with([_file_event("/store", "DSC0001.JPG")])
    with patch.object(camera_mod, "gp", gp, create=True):
        result = cam.listen_for_files(dest, timeout_s=0.5, quiet_window_s=0.05)
    assert len(result) == 1
    assert result[0].exists() and result[0].name.endswith("DSC0001.JPG")
    assert cam._camera.downloaded == [("/store", "DSC0001.JPG")]
    assert cam._camera.capture_called is False


def test_siblings_collected_within_quiet_window(tmp_path=None):
    dest = _tmp("listen_siblings")
    events = [_file_event("/store", "DSC0002.JPG"), _file_event("/store", "DSC0002.ARW")]
    cam, gp = _camera_with(events)
    with patch.object(camera_mod, "gp", gp, create=True):
        result = cam.listen_for_files(dest, timeout_s=0.5, quiet_window_s=0.05)
    names = sorted(p.name[-12:] for p in result)
    assert len(result) == 2, result
    assert any(n.endswith("DSC0002.JPG") for n in names)
    assert any(n.endswith("DSC0002.ARW") for n in names)
    assert cam._camera.capture_called is False


def test_timeout_returns_empty(tmp_path=None):
    dest = _tmp("listen_timeout")
    cam, gp = _camera_with([])  # only timeouts
    with patch.object(camera_mod, "gp", gp, create=True):
        result = cam.listen_for_files(dest, timeout_s=0.05, quiet_window_s=0.05)
    assert result == []
    assert cam._camera.downloaded == []
    assert cam._camera.capture_called is False


def test_non_image_event_ignored(tmp_path=None):
    dest = _tmp("listen_nonimage")
    events = [_file_event("/store", "CLIP0001.MP4"), _file_event("/store", "DSC0003.JPG")]
    cam, gp = _camera_with(events)
    with patch.object(camera_mod, "gp", gp, create=True):
        result = cam.listen_for_files(dest, timeout_s=0.5, quiet_window_s=0.05)
    assert len(result) == 1 and result[0].name.endswith("DSC0003.JPG")
    assert cam._camera.downloaded == [("/store", "DSC0003.JPG")]  # MP4 never downloaded


def test_file_changed_event_ignored(tmp_path=None):
    dest = _tmp("listen_changed")
    events = [(_FILE_CHANGED, SimpleNamespace(folder="/store", name="DSC0004.JPG"))]
    cam, gp = _camera_with(events)
    with patch.object(camera_mod, "gp", gp, create=True):
        result = cam.listen_for_files(dest, timeout_s=0.05, quiet_window_s=0.05)
    assert result == []
    assert cam._camera.downloaded == []


def test_download_error_raises_camera_error(tmp_path=None):
    dest = _tmp("listen_error")
    cam, gp = _camera_with([_file_event("/store", "DSC0005.JPG")],
                           fail_download_on=("/store", "DSC0005.JPG"))
    raised = False
    with patch.object(camera_mod, "gp", gp, create=True):
        try:
            cam.listen_for_files(dest, timeout_s=0.5, quiet_window_s=0.05)
        except CameraError:
            raised = True
    assert raised, "a transport error during download must surface as CameraError"
    assert cam._camera.capture_called is False


# --- drain_events -----------------------------------------------------------

def test_drain_counts_events(tmp_path=None):
    events = [_file_event("/s", "a.JPG"), _file_event("/s", "b.JPG"), _file_event("/s", "c.JPG")]
    cam, gp = _camera_with(events)
    with patch.object(camera_mod, "gp", gp, create=True):
        count = cam.drain_events(max_ms=500)
    assert count == 3
    assert cam._camera.capture_called is False


def test_drain_empty_queue_returns_zero(tmp_path=None):
    cam, gp = _camera_with([])
    with patch.object(camera_mod, "gp", gp, create=True):
        count = cam.drain_events(max_ms=200)
    assert count == 0


# --- helpers ----------------------------------------------------------------

_TMP_ROOT = None


def _tmp(name: str) -> Path:
    global _TMP_ROOT
    import tempfile
    if _TMP_ROOT is None:
        _TMP_ROOT = Path(tempfile.mkdtemp(prefix="handheld_listen_"))
    path = _TMP_ROOT / name
    path.mkdir(parents=True, exist_ok=True)
    return path


if __name__ == "__main__":
    raise SystemExit(run_module_tests(__name__))
