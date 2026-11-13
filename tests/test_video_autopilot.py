"""Hardware-free contract for video autopilot recording.

Run standalone:
    python3 tests/test_video_autopilot.py
"""

from __future__ import annotations

import atexit
import shutil
import sys
import tempfile
import types
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from PIL import Image

try:
    import gphoto2  # noqa: F401 — prefer the installed binding
except ModuleNotFoundError:
    sys.modules.setdefault(
        "gphoto2",
        types.SimpleNamespace(GPhoto2Error=RuntimeError, GP_CAPTURE_IMAGE=0, GP_FILE_TYPE_NORMAL=0),
    )

from _runner import run_module_tests

from sonyremote import autoexpose

SHUTTERS = ["1/15", "1/30", "1/60", "1/125", "1/250"]
# One frame directory per process: a fixed name under gettempdir() is shared with every concurrent
# suite, whose fake frames (another brightness) would then feed this exposure loop.
_FRAME_DIR = Path(tempfile.mkdtemp(prefix="fake_video_frames_"))
atexit.register(shutil.rmtree, _FRAME_DIR, ignore_errors=True)


class FakeVideoCamera:
    """Small simulator: brighter frames appear as the shutter is slowed."""

    def __init__(self):
        self.recording = False
        self.started = 0
        self.stopped = 0
        self.shutter_index = SHUTTERS.index("1/125")
        self.frames = 0
        self.settings_seen: list[str] = []

    def start_movie(self) -> None:
        self.started += 1
        self.recording = True

    def stop_movie(self) -> None:
        self.stopped += 1
        self.recording = False

    def shooting_mode(self) -> str:
        return "M"

    def get_settings(self) -> dict:
        return {
            "iso": "100",
            "aperture": "f/2.8",
            "shutter": SHUTTERS[self.shutter_index],
            "white_balance": "Auto",
            "exposure_comp": "0",
        }

    def list_options(self, key: str) -> list[str]:
        if key == "shutter":
            return list(SHUTTERS)
        if key == "iso":
            return ["100", "200", "400", "800"]
        if key == "exposure_comp":
            return ["-3", "-2", "-1", "0", "+1", "+2", "+3"]
        return []

    def set_setting(self, key: str, value: str) -> str:
        if key == "shutter":
            self.shutter_index = SHUTTERS.index(value)
            self.settings_seen.append(value)
            return value
        return value

    def liveview_frame(self, dest: Path | None = None) -> Path:
        assert self.recording, "video autopilot should sample while native recording is active"
        values = [142, 112, 96, 84, 54]
        value = values[self.shutter_index]
        self.frames += 1
        path = _FRAME_DIR / f"fake_video_frame_{self.frames}.jpg"
        Image.new("RGB", (80, 60), (value, value, value)).save(path, "JPEG", quality=90)
        return path


def _record_auto_exposed_movie():
    func = getattr(autoexpose, "record_auto_exposed_movie", None)
    assert callable(func), "record_auto_exposed_movie should exist"
    return func


def test_video_autopilot_adjusts_exposure_while_native_recording_is_active() -> None:
    cam = FakeVideoCamera()

    report = _record_auto_exposed_movie()(
        cam,
        target_mean=112.0,
        seconds=0.1,
        sample_interval=0.0,
        tolerance=8.0,
        max_samples=4,
        sleep=lambda _: None,
    )

    assert cam.started == 1
    assert cam.stopped == 1
    assert cam.recording is False
    assert cam.settings_seen
    assert report["ok"] is True
    assert report["samples"][-1]["mean"] == 112.0
    assert report["final_settings"]["shutter"] == "1/30"
    assert report["poster_frame"].endswith(".jpg")


if __name__ == "__main__":
    raise SystemExit(run_module_tests(__name__))
