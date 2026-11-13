"""Fail-soft contract for optional Kelvin white balance.

Run standalone:
    python3 tests/test_camera_color_temp.py
"""

from __future__ import annotations

import sys
import types
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


class FakeWhiteBalanceWidget:
    def count_choices(self) -> int:
        return 1

    def get_choice(self, index: int) -> str:
        assert index == 0
        return "Color Temperature"

    def set_value(self, value) -> None:
        assert value == "Color Temperature"


class FakeColorTemperatureWidget:
    def set_value(self, value) -> None:
        raise TypeError("CameraWidget_set_value rejected this binding value")


class FakeConfig:
    def get_child_by_name(self, name: str):
        if name == "whitebalance":
            return FakeWhiteBalanceWidget()
        if name == "colortemperature":
            return FakeColorTemperatureWidget()
        raise AssertionError(f"unexpected node {name!r}")


class FakeGPhotoCamera:
    def get_config(self) -> FakeConfig:
        return FakeConfig()

    def set_config(self, config: FakeConfig) -> None:
        assert isinstance(config, FakeConfig)


def test_color_temperature_type_error_is_non_blocking() -> None:
    cam = Camera()
    cam._camera = FakeGPhotoCamera()

    assert cam.set_color_temp(7500) is False


if __name__ == "__main__":
    raise SystemExit(run_module_tests(__name__))
