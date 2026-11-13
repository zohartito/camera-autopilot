"""Contract for Sony native movie PTP values.

Run standalone:
    python3 tests/test_movie_control_values.py
"""

from __future__ import annotations

import sys
import types
import tempfile
from unittest.mock import patch
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
try:
    import gphoto2  # noqa: F401 — prefer the installed binding
except ModuleNotFoundError:
    sys.modules.setdefault(
        "gphoto2",
        types.SimpleNamespace(GPhoto2Error=RuntimeError, GP_CAPTURE_IMAGE=0, GP_FILE_TYPE_NORMAL=0),
    )

from _runner import run_module_tests

from sonyremote.camera import Camera


class FakeWidget:
    def __init__(self):
        self.values: list[int] = []

    def set_value(self, value: int) -> None:
        self.values.append(value)


class FakeConfig:
    def __init__(self, widget: FakeWidget):
        self.widget = widget

    def get_child_by_name(self, name: str) -> FakeWidget:
        assert name == "movie"
        return self.widget


class FakeGPhotoCamera:
    def __init__(self):
        self.widget = FakeWidget()
        self.config = FakeConfig(self.widget)
        self.sets = 0

    def get_config(self) -> FakeConfig:
        return self.config

    def set_config(self, config: FakeConfig) -> None:
        assert config is self.config
        self.sets += 1


def test_native_movie_start_and_stop_use_sony_press_release_values() -> None:
    fake = FakeGPhotoCamera()
    cam = Camera(model="Sony ILCE-7M2 (Control)", allow_unverified_identity=True)
    cam._camera = fake

    with tempfile.TemporaryDirectory() as temporary, \
         patch("sonyremote.camera.RECORDING_QUARANTINE_ROOT", Path(temporary) / "recording"), \
         patch("sonyremote.camera._recording_quarantined_process", False):
        cam.start_movie()
        cam.stop_movie()

    assert fake.widget.values == [2, 1, 2, 1]
    assert fake.sets == 4


if __name__ == "__main__":
    raise SystemExit(run_module_tests(__name__))
