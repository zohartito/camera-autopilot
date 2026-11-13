"""Hardware-free checks for gphoto widget value coercion in Camera.set_setting."""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from _runner import run_module_tests

from sonyremote.camera import Camera, gp


class FakeWidget:
    def __init__(self, widget_type: int, initial):
        self.widget_type = widget_type
        self.value = initial
        self.values: list[object] = []

    def get_type(self) -> int:
        return self.widget_type

    def get_value(self):
        return self.value

    def set_value(self, value) -> None:
        self.values.append(value)
        self.value = value


class FakeRoundedRangeWidget(FakeWidget):
    def set_value(self, value) -> None:
        self.values.append(value)
        self.value = value + 0.000001220703125


class FakeConfig:
    def __init__(self, widget: FakeWidget):
        self.widget = widget

    def get_child_by_name(self, name: str) -> FakeWidget:
        return self.widget


class FakeGPhotoCamera:
    def __init__(self, widget: FakeWidget):
        self.widget = widget
        self.configs_set = 0

    def get_config(self) -> FakeConfig:
        return FakeConfig(self.widget)

    def set_config(self, config: FakeConfig) -> None:
        self.configs_set += 1


def test_set_setting_sends_float_to_range_widgets() -> None:
    widget = FakeWidget(gp.GP_WIDGET_RANGE, 34.22003173828125)
    camera = Camera()
    camera._camera = FakeGPhotoCamera(widget)  # type: ignore[attr-defined]

    camera.set_setting("zoom", "35.22")

    assert widget.values == [35.22]
    assert isinstance(widget.values[0], float)


def test_set_setting_accepts_nearly_equal_range_readback() -> None:
    widget = FakeRoundedRangeWidget(gp.GP_WIDGET_RANGE, 34.22003173828125)
    camera = Camera()
    camera._camera = FakeGPhotoCamera(widget)  # type: ignore[attr-defined]

    camera.set_setting("zoom", "35.22")

    assert widget.values == [35.22]
    assert camera._camera.configs_set == 1  # type: ignore[union-attr]


def test_set_setting_sends_int_to_toggle_widgets() -> None:
    widget = FakeWidget(gp.GP_WIDGET_TOGGLE, 2)
    camera = Camera()
    camera._camera = FakeGPhotoCamera(widget)  # type: ignore[attr-defined]

    camera.set_setting("movie", "1")

    assert widget.values == [1]
    assert isinstance(widget.values[0], int)


class FakeStuckRadioWidget(FakeWidget):
    """A lens that ignores the write: the body keeps reading its old aperture."""

    def set_value(self, value) -> None:
        self.values.append(value)

    def count_choices(self) -> int:
        return 2

    def get_choice(self, index: int) -> str:
        return ["f/4", "f/8"][index]


def test_a_value_the_body_does_not_take_raises_setting_not_applied() -> None:
    from sonyremote import CameraError, SettingNotApplied
    widget = FakeStuckRadioWidget(gp.GP_WIDGET_RADIO, "f/4")
    camera = Camera()
    camera._camera = FakeGPhotoCamera(widget)  # type: ignore[attr-defined]

    try:
        camera.set_setting("aperture", "8", max_seconds=0.0)
    except SettingNotApplied as exc:
        assert isinstance(exc, CameraError)          # every existing CameraError handler still applies
        assert (exc.key, exc.requested, exc.final) == ("aperture", "8", "f/4")
        assert "camera stayed at 'f/4'" in str(exc)
    else:
        raise AssertionError("a value the body did not take must raise")


if __name__ == "__main__":
    raise SystemExit(run_module_tests(__name__))
