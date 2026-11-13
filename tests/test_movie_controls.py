"""Hardware-free checks for native movie recording control.

Run:
    ./.venv/bin/python tests/test_movie_controls.py
"""

from __future__ import annotations

import atexit
import shutil
import sys
import tempfile
import types
import importlib
import importlib.util
import io
from contextlib import contextmanager, redirect_stderr, redirect_stdout
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

# The contract uses a fake camera, so it must remain runnable without the optional
# physical-camera binding installed in the test interpreter.
try:
    import gphoto2  # noqa: F401 — prefer the installed binding
except ModuleNotFoundError:
    sys.modules.setdefault(
        "gphoto2",
        types.SimpleNamespace(
            GPhoto2Error=RuntimeError,
            GP_WIDGET_RANGE=1,
            GP_WIDGET_TOGGLE=2,
        ),
    )

from PIL import Image

from sonyremote.autoexpose import record_auto_exposed_movie
from sonyremote.camera import Camera, CameraError, RecordingStopUncertain
import sonyremote.camera as camera_module

SHUTTERS = ["1/15", "1/30"]
# One frame directory per process: a fixed name under gettempdir() is shared with every concurrent
# suite, whose fake frames (another brightness) would then feed this exposure loop.
_FRAME_DIR = Path(tempfile.mkdtemp(prefix="fake_video_frames_"))
atexit.register(shutil.rmtree, _FRAME_DIR, ignore_errors=True)


class FakeMovieWidget:
    def __init__(self):
        self.values: list[int] = []

    def set_value(self, value: int) -> None:
        self.values.append(value)


class FakeMovieConfig:
    def __init__(self, widget: FakeMovieWidget):
        self.widget = widget

    def get_child_by_name(self, name: str) -> FakeMovieWidget:
        assert name == "movie"
        return self.widget


class FakeGPhotoCamera:
    def __init__(self):
        self.widget = FakeMovieWidget()
        self.configs_set = 0

    def get_config(self) -> FakeMovieConfig:
        return FakeMovieConfig(self.widget)

    def set_config(self, config: FakeMovieConfig) -> None:
        self.configs_set += 1


class FakeVideoCamera:
    """Live-view samples respond to exposure changes while native movie is rolling."""

    def __init__(self):
        self.movie_started = False
        self.movie_stopped = False
        self.shutter = "1/30"
        self.frames = 0
        self.corrections: list[str] = []

    def shooting_mode(self) -> str:
        return "M"

    def start_movie(self) -> None:
        self.movie_started = True

    def stop_movie(self) -> None:
        self.movie_stopped = True

    def liveview_frame(self) -> Path:
        self.frames += 1
        value = 95 if self.shutter == "1/30" else 112
        path = _FRAME_DIR / f"fake_video_frame_{self.frames}.jpg"
        Image.new("RGB", (80, 60), (value, value, value)).save(path, "JPEG", quality=90)
        return path

    def get_settings(self) -> dict:
        return {
            "iso": "400",
            "aperture": "f/2.8",
            "shutter": self.shutter,
            "white_balance": "Auto",
            "exposure_comp": "0",
        }

    def list_options(self, key: str) -> list[str]:
        return {"shutter": list(SHUTTERS), "iso": ["400"], "exposure_comp": ["0"]}.get(key, [])

    def set_setting(self, key: str, value: str) -> str:
        if key == "shutter":
            self.shutter = value
            self.corrections.append(value)
        return value


class FakeNoLiveViewCamera(FakeVideoCamera):
    def liveview_frame(self) -> Path:
        raise CameraError("USB live view unavailable while recording")


class FakeStopFailureCamera(FakeVideoCamera):
    def __init__(self, *, retry_safe: bool = False):
        super().__init__()
        self.stop_attempts = 0
        self.stop_movie_retry_safe = retry_safe

    def stop_movie(self) -> None:
        self.stop_attempts += 1
        raise CameraError("simulated stop failure")


class FakeRetrySafeStopCamera(FakeVideoCamera):
    stop_movie_retry_safe = True

    def __init__(self):
        super().__init__()
        self.stop_attempts = 0

    def stop_movie(self) -> None:
        self.stop_attempts += 1
        if self.stop_attempts == 1:
            raise CameraError("transient stop failure")
        super().stop_movie()


@contextmanager
def _isolated_recording_quarantine():
    old_root = camera_module.RECORDING_QUARANTINE_ROOT
    old_process_state = camera_module._recording_quarantined_process
    with tempfile.TemporaryDirectory() as tmp:
        camera_module.RECORDING_QUARANTINE_ROOT = Path(tmp) / "recording-safety"
        camera_module._recording_quarantined_process = False
        try:
            yield camera_module.RECORDING_QUARANTINE_ROOT
        finally:
            try:
                camera_module.clear_recording_quarantine(verified_device_idle=True)
            except Exception:
                pass
            camera_module.RECORDING_QUARANTINE_ROOT = old_root
            camera_module._recording_quarantined_process = old_process_state


def test_native_movie_uses_sony_press_release_values() -> None:
    fake = FakeGPhotoCamera()
    camera = Camera(model="Sony ILCE-7M2 (Control)", allow_unverified_identity=True)
    camera._camera = fake  # type: ignore[attr-defined]  # hardware-free injection

    with _isolated_recording_quarantine():
        camera.start_movie()
        camera.stop_movie()

    assert fake.widget.values == [2, 1, 2, 1]
    assert fake.configs_set == 4


def test_native_start_uncertainty_does_not_send_a_compensating_toggle_and_quarantines() -> None:
    with _isolated_recording_quarantine() as root:
        camera = Camera(model="Sony ILCE-7M2 (Control)", allow_unverified_identity=True)
        attempts: list[str] = []

        def press() -> None:
            attempts.append("press")
            raise CameraError("start transport may have reached camera")

        camera._press_movie_button = press
        try:
            camera.record_native(0.5)
        except RecordingStopUncertain as exc:
            assert "recording_stop_uncertain" in str(exc)
        else:
            raise AssertionError("uncertain post-start stop was accepted")
        assert attempts == ["press"], "an uncertain start toggle must not receive a compensating toggle"
        assert camera._recording_quarantined is True
        assert (root / camera_module.RECORDING_QUARANTINE_NAME).is_file()


def test_native_recording_keeps_start_timestamp_and_elapsed_duration() -> None:
    camera = Camera(model="Sony ILCE-7M2 (Control)", allow_unverified_identity=True)
    calls: list[str] = []
    clock = {"value": 10.0}
    original_monotonic = camera_module.time.monotonic
    original_sleep = camera_module.time.sleep
    camera._press_movie_button = lambda: calls.append("press")
    camera_module.time.monotonic = lambda: clock["value"]
    camera_module.time.sleep = lambda seconds: clock.__setitem__("value", clock["value"] + seconds)
    try:
        with _isolated_recording_quarantine():
            report = camera.record_native(1.2)
    finally:
        camera_module.time.monotonic = original_monotonic
        camera_module.time.sleep = original_sleep
    assert calls == ["press", "press"]
    assert report["requested_seconds"] == 1.2
    assert report["recorded_seconds"] == 1.2


def test_record_auto_exposed_movie_adjusts_from_liveview_and_stops() -> None:
    camera = FakeVideoCamera()

    report = record_auto_exposed_movie(
        camera,
        target_mean=112.0,
        seconds=2.0,
        sample_interval=0.0,
        max_samples=2,
    )

    assert camera.movie_started is True
    assert camera.movie_stopped is True
    assert camera.corrections == ["1/15"]
    assert report["ok"] is True
    assert report["samples"][0]["correction"].startswith("shutter -> 1/15")
    assert report["poster_frame"]


def test_record_auto_exposed_movie_stops_when_liveview_is_unavailable() -> None:
    camera = FakeNoLiveViewCamera()

    report = record_auto_exposed_movie(
        camera,
        target_mean=112.0,
        seconds=2.0,
        sample_interval=0.0,
        max_samples=2,
    )

    assert camera.movie_started is True
    assert camera.movie_stopped is True
    assert report["ok"] is False
    assert "error" in report["samples"][0]
    assert report["note"] == "Native movie recorded, but USB live view was unavailable during recording."


def test_record_stop_failure_is_terminal_and_quarantines_future_recording() -> None:
    with _isolated_recording_quarantine():
        camera = FakeStopFailureCamera()
        try:
            record_auto_exposed_movie(camera, seconds=1.0, sample_interval=0.0, max_samples=1)
        except RecordingStopUncertain as exc:
            assert "recording_stop_uncertain" in str(exc)
        else:
            raise AssertionError("uncertain movie stop was reported as a completed recording")
        assert camera.stop_attempts == 1, "toggle transport must not retry an uncertain stop"
        assert camera._recording_quarantined is True
        started_before = camera.movie_started
        try:
            record_auto_exposed_movie(camera, seconds=1.0, sample_interval=0.0, max_samples=1)
        except RecordingStopUncertain:
            pass
        else:
            raise AssertionError("quarantined camera accepted another recording")
        assert camera.movie_started is started_before


def test_record_stop_retries_once_only_when_transport_declares_it_safe() -> None:
    camera = FakeRetrySafeStopCamera()
    report = record_auto_exposed_movie(camera, seconds=1.0, sample_interval=0.0, max_samples=1)
    assert report["recorded_seconds"] >= 0
    assert camera.stop_attempts == 2
    assert camera.movie_stopped is True
    assert not getattr(camera, "_recording_quarantined", False)


def test_recording_quarantine_survives_new_camera_api_and_process_reload_until_verified_clear() -> None:
    with _isolated_recording_quarantine() as root:
        failing = FakeStopFailureCamera()
        try:
            record_auto_exposed_movie(failing, seconds=1.0, sample_interval=0.0, max_samples=1)
        except RecordingStopUncertain:
            pass
        else:
            raise AssertionError("expected stop uncertainty")
        sentinel = root / camera_module.RECORDING_QUARANTINE_NAME
        assert sentinel.is_file()

        fresh = Camera(model="Sony ILCE-7M2 (Control)", allow_unverified_identity=True)
        fresh._press_movie_button = lambda: (_ for _ in ()).throw(AssertionError("new Camera reached device"))
        try:
            fresh.start_movie()
        except RecordingStopUncertain:
            pass
        else:
            raise AssertionError("new Camera bypassed durable quarantine")

        sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "backend"))
        import engine as backend_engine
        try:
            backend_engine.RealEngine().record("test", seconds=1.0)
        except RecordingStopUncertain:
            pass
        else:
            raise AssertionError("second RealEngine API request bypassed durable quarantine")

        camera_module._recording_quarantined_process = False
        assert camera_module.recording_quarantine_active() is True, "reload must rediscover sentinel"
        try:
            camera_module.clear_recording_quarantine(verified_device_idle=False)
        except ValueError:
            pass
        else:
            raise AssertionError("quarantine cleared without physical-idle affirmation")
        camera_module.clear_recording_quarantine(verified_device_idle=True)
        assert camera_module.recording_quarantine_active() is False
        started: list[bool] = []
        fresh._press_movie_button = lambda: started.append(True)
        fresh.start_movie()
        assert started == [True], "verified local clearance should restore recording only"


def test_mcp_cache_reset_does_not_clear_durable_recording_quarantine() -> None:
    with _isolated_recording_quarantine():
        failing = FakeStopFailureCamera()
        try:
            record_auto_exposed_movie(failing, seconds=1.0, sample_interval=0.0, max_samples=1)
        except RecordingStopUncertain:
            pass

        astral = types.ModuleType("astral")
        astral.Observer = object
        astral_sun = types.ModuleType("astral.sun")
        astral_sun.SunDirection = object
        astral_sun.blue_hour = astral_sun.golden_hour = astral_sun.sun = lambda *_args, **_kwargs: {}
        mcpserver = types.ModuleType("mcp.server.mcpserver")

        class FakeMCP:
            def __init__(self, *_args, **_kwargs):
                pass

            @staticmethod
            def tool():
                return lambda function: function

        mcpserver.MCPServer = FakeMCP
        mcpserver.Image = object
        mcp_types = types.ModuleType("mcp.types")
        mcp_types.CallToolResult = object
        mcp_types.TextContent = object
        with patch.dict(sys.modules, {
            "astral": astral,
            "astral.sun": astral_sun,
            "mcp": types.ModuleType("mcp"),
            "mcp.server": types.ModuleType("mcp.server"),
            "mcp.server.mcpserver": mcpserver,
            "mcp.types": mcp_types,
        }):
            sys.modules.pop("server", None)
            mcp_server = importlib.import_module("server")
            try:
                class CachedCamera:
                    def disconnect(self) -> None:
                        return None

                mcp_server._camera = CachedCamera()
                try:
                    mcp_server._once(lambda _camera: (_ for _ in ()).throw(RecordingStopUncertain("stop failed")))
                except RecordingStopUncertain:
                    pass
                else:
                    raise AssertionError("expected MCP action failure")
                assert mcp_server._camera is None
                assert camera_module.recording_quarantine_active() is True
                try:
                    mcp_server.record_native_video(1.0)
                except RecordingStopUncertain:
                    pass
                else:
                    raise AssertionError("MCP recording entry bypassed durable quarantine after cache reset")
            finally:
                mcp_server._stop_keepalive()  # the re-import started another keep-alive thread


def test_corrupt_or_symlinked_quarantine_sentinel_fails_closed() -> None:
    with _isolated_recording_quarantine() as root:
        root.mkdir(mode=0o700)
        sentinel = root / camera_module.RECORDING_QUARANTINE_NAME
        target = root / "target.json"
        target.write_text('{"state":"recording_stop_uncertain"}', encoding="utf-8")
        sentinel.symlink_to(target)
        try:
            camera_module.recording_quarantine_active()
        except RecordingStopUncertain:
            pass
        else:
            raise AssertionError("symlinked quarantine sentinel was accepted")
        sentinel.unlink()
        sentinel.write_text("not-json", encoding="utf-8")
        camera_module._recording_quarantined_process = False
        try:
            camera_module.recording_quarantine_active()
        except RecordingStopUncertain:
            pass
        else:
            raise AssertionError("corrupt quarantine sentinel was accepted")


def test_camera_stop_exception_persists_quarantine_before_returning() -> None:
    with _isolated_recording_quarantine() as root:
        camera = Camera(model="Sony ILCE-7M2 (Control)", allow_unverified_identity=True)
        camera._press_movie_button = lambda: None
        camera.start_movie()
        camera._press_movie_button = lambda: (_ for _ in ()).throw(RuntimeError("transport failed"))
        try:
            camera.stop_movie()
        except RecordingStopUncertain:
            pass
        else:
            raise AssertionError("Camera.stop_movie suppressed an uncertain stop")
        assert (root / camera_module.RECORDING_QUARANTINE_NAME).is_file()
        assert camera_module.recording_quarantine_active() is True


def test_verified_clear_then_requarantine_reuses_only_safe_root_and_survives_restart() -> None:
    with _isolated_recording_quarantine() as root:
        sentinel = root / camera_module.RECORDING_QUARANTINE_NAME
        camera_module.quarantine_recording_stop("first uncertain stop")
        assert sentinel.is_file()
        camera_module.clear_recording_quarantine(verified_device_idle=True)
        assert root.is_dir() and not sentinel.exists()

        # The verified clear deliberately retains the trusted root.  A later
        # uncertain stop must recreate a durable sentinel there rather than
        # treating FileExistsError from mkdir as an operational failure.
        camera_module.quarantine_recording_stop("second uncertain stop")
        assert sentinel.is_file()
        camera_module._recording_quarantined_process = False
        assert camera_module.recording_quarantine_active() is True


def test_requarantine_rejects_existing_permissive_root() -> None:
    with _isolated_recording_quarantine() as root:
        root.mkdir(mode=0o700)
        root.chmod(0o755)
        try:
            camera_module.quarantine_recording_stop("unsafe root")
        except RecordingStopUncertain as exc:
            assert "root is unsafe" in str(exc)
        else:
            raise AssertionError("quarantine reused a permissive existing root")


def test_quarantine_clear_cli_rejects_every_abbreviation() -> None:
    script = Path(__file__).resolve().parent.parent / "tools" / "clear_recording_quarantine.py"
    spec = importlib.util.spec_from_file_location("clear_recording_quarantine_test", script)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    cleared: list[bool] = []
    module.clear_recording_quarantine = lambda *, verified_device_idle: cleared.append(verified_device_idle)
    exact_flag = "--verified-device-idle"
    rejected = [exact_flag[:length] for length in range(3, len(exact_flag))] + [
        "-v",
        "--verified_device_idle",
        "--verified-device-idle=true",
    ]
    for abbreviation in rejected:
        with redirect_stderr(io.StringIO()):
            try:
                module.main([abbreviation])
            except SystemExit as exc:
                assert exc.code == 2, (abbreviation, exc.code)
            else:
                raise AssertionError(f"clear CLI accepted abbreviation: {abbreviation}")
    with redirect_stdout(io.StringIO()):
        assert module.main(["--verified-device-idle"]) == 0
    assert cleared == [True]


def main() -> int:
    tests = [
        test_native_movie_uses_sony_press_release_values,
        test_native_start_uncertainty_does_not_send_a_compensating_toggle_and_quarantines,
        test_native_recording_keeps_start_timestamp_and_elapsed_duration,
        test_record_auto_exposed_movie_adjusts_from_liveview_and_stops,
        test_record_auto_exposed_movie_stops_when_liveview_is_unavailable,
        test_record_stop_failure_is_terminal_and_quarantines_future_recording,
        test_record_stop_retries_once_only_when_transport_declares_it_safe,
        test_recording_quarantine_survives_new_camera_api_and_process_reload_until_verified_clear,
        test_mcp_cache_reset_does_not_clear_durable_recording_quarantine,
        test_corrupt_or_symlinked_quarantine_sentinel_fails_closed,
        test_camera_stop_exception_persists_quarantine_before_returning,
        test_verified_clear_then_requarantine_reuses_only_safe_root_and_survives_restart,
        test_requarantine_rejects_existing_permissive_root,
        test_quarantine_clear_cli_rejects_every_abbreviation,
    ]
    failures: list[str] = []
    for test in tests:
        try:
            test()
        except Exception as exc:
            failures.append(f"{test.__name__}: {type(exc).__name__}: {exc}")

    if failures:
        print("MOVIE CONTROL TESTS: FAIL")
        for failure in failures:
            print("  " + failure)
        return 1

    print(f"MOVIE CONTROL TESTS: PASS ({len(tests)}/{len(tests)})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
