import json
from pathlib import Path
import pytest
from PIL import Image
import engine
import product_sessions
from camera_caps import A7II_CAPS
from stack_planner import timelapse_config, jpeg_luminance, metered_timelapse_step
from stack_session import SessionRunner


def test_meter_linearizes_saved_jpeg_and_ramps_shutter_then_iso(tmp_path):
    image = tmp_path / "meter.jpg"
    Image.new("RGB", (128, 96), (128, 128, 128)).save(image)
    measured = jpeg_luminance(image)
    assert .21 < measured["linear_luminance"] < .22
    config = timelapse_config("holy_grail", {"max_shutter_s": 1 / 60, "min_iso": 100, "max_iso": 400})
    step = metered_timelapse_step(config, current_ev=0, luminance=.01,
                                  base_shutter_s=1 / 60, base_iso=100, caps=A7II_CAPS)
    assert step["requested_ev"] == .3
    assert step["shutter"] == "1/60"
    assert step["iso"] == "125"
    assert step["cascade"] == "shutter_then_iso"
    state = 0
    for _ in range(80):
        step = metered_timelapse_step(config, current_ev=state, luminance=.0001,
                                     base_shutter_s=1 / 60, base_iso=100, caps=A7II_CAPS)
        state = step["requested_ev"]
        assert state <= 6 and int(step["iso"]) <= 400
    assert step["range_limited"] is True


@pytest.mark.parametrize("options", [{"max_step_ev": .4}, {"fps": True}, {"max_iso": 50}, {"unknown": 1}])
def test_config_rejects_unsafe_options(options):
    with pytest.raises(ValueError):
        timelapse_config("holy_grail", options)


def _complete(tmp_path, monkeypatch):
    monkeypatch.setattr(engine, "SHOTS_DIR", tmp_path / "shots")
    runner = product_sessions.runner()
    state = runner.create("timelapse", 3, timelapse_mode="holy_grail")
    def capture(index, spec, destination):
        Image.new("RGB", (64, 48), (index * 30, 70, 90)).save(destination)
        return destination
    return runner, runner.run(state["id"], capture)


def test_real_ffmpeg_video_full_decode_and_exact_input_manifest(tmp_path, monkeypatch):
    from timelapse_video import decode_frames, probe_encoder
    if probe_encoder() is None:
        pytest.skip("ffmpeg/ffprobe did not execute (-version)")
    runner, state = _complete(tmp_path, monkeypatch)
    result = product_sessions.run(state["id"])
    video = result["video"]
    assert video["status"] == "completed", video
    assert video["full_decode_verified"]
    assert video["width"] == 1920 and video["height"] == 1080
    assert video["playback_duration_s"] == pytest.approx(3 / 24)
    assert int(video["probe"]["nb_read_frames"]) == 3
    assert int(video["probe"]["width"]) == 1920 and int(video["probe"]["height"]) == 1080
    mp4 = runner.artifact(state["id"], "timelapse.mp4")
    assert mp4.stat().st_mode & 0o777 == 0o600
    decoded = decode_frames(mp4, tmp_path / "decoded")
    assert len(decoded) == 3
    inputs = json.loads((runner._directory(state["id"]) / "video-inputs.json").read_text())
    assert [f["sha256"] for f in inputs["inputs"]] == [f["receipt"]["sha256"] for f in result["frames"]]
    assert result["frames"][1]["actual_interval_s"] > 0
    assert product_sessions.run(state["id"])["video"]["receipt"] == video["receipt"]


def test_renderer_failure_keeps_frames_zip_and_gallery(tmp_path, monkeypatch):
    import subprocess
    import timelapse_video
    runner, state = _complete(tmp_path, monkeypatch)
    monkeypatch.setattr(timelapse_video, "probe_encoder", lambda: {"ffmpeg": "/x", "ffprobe": "/x"})
    def fail(*args, **kwargs):
        raise subprocess.CalledProcessError(1, ["ffmpeg"])
    monkeypatch.setattr(timelapse_video, "render_h264", fail)
    result = product_sessions.run(state["id"])
    assert result["status"] == "completed"
    assert result["video"]["status"] == "failed"
    assert result["publication"]["status"] == "published"
    assert runner.artifact(state["id"], "sequence.zip").exists()
    assert all(Path(f["destination"]).exists() for f in result["frames"])


def test_encoder_that_does_not_execute_keeps_frames_unavailable(tmp_path, monkeypatch):
    import timelapse_video
    runner, state = _complete(tmp_path, monkeypatch)
    monkeypatch.setattr(timelapse_video, "probe_encoder", lambda: None)
    result = product_sessions.run(state["id"])
    assert result["status"] == "completed"
    assert result["video"]["status"] == "unavailable"
    assert "execute" in result["video"]["reason"]
    assert result["publication"]["status"] == "published"
    assert runner.artifact(state["id"], "sequence.zip").exists()
    assert all(Path(f["destination"]).exists() for f in result["frames"])


def test_product_holy_grail_uses_previous_accepted_jpeg_and_restores(tmp_path, monkeypatch):
    from types import SimpleNamespace
    import sonyremote
    monkeypatch.setattr(engine, "SHOTS_DIR", tmp_path / "shots")
    baseline = {"iso": "100", "shutter": "1/60", "aperture": "8", "focus_mode": "Automatic"}
    live = dict(baseline)
    shots = []
    class Camera:
        def __init__(self, **kwargs): pass
        def __enter__(self): return self
        def __exit__(self, *args): pass
        def info(self): return SimpleNamespace(model="Sony ILCE-7M2 (Control)", port="usb:001,002")
        def wake(self): return True
        def shooting_mode(self): return "M"
        def get_settings(self): return dict(live)
        def list_options(self, key):
            return {"iso": ["100", "125", "160", "200"], "aperture": ["8"], "shutter": ["1/60"]}[key]
        def set_setting(self, key, value): live[key] = value
        def set_exposure(self, **kwargs): live.update(kwargs)
        def capture(self, **kwargs):
            shots.append(dict(live))
            path = tmp_path / f"source_{len(shots)}.jpg"
            Image.new("RGB", (64, 48), (30, 30, 30)).save(path)
            return str(path)
    monkeypatch.setattr(sonyremote, "Camera", Camera)
    monkeypatch.setattr(engine, "_free_usb", lambda: None)
    runner = product_sessions.runner()
    state = runner.create("timelapse", 3, timelapse_mode="holy_grail",
        timelapse_options={"max_shutter_s": 1/60},
        metadata={"camera_model": "Sony ILCE-7M2 (Control)", "camera_port": "usb:001,002", "camera_serial": "A7II00SERIAL"})
    result = product_sessions.run(state["id"])
    assert result["status"] == "completed", result
    assert len(shots) == 3
    assert [shot["iso"] for shot in shots] == ["100", "125", "160"]
    # The full meter record lives in the frame's evidence sidecar; the manifest keeps its summary.
    meter = runner.frame_evidence(result, 1)["applied"]["timelapse_meter"]
    assert meter["source_sha256"] == result["frames"][0]["receipt"]["sha256"]
    assert result["restore"] == "completed" and live == baseline


def test_auto_custom_responds_faster_than_smooth_holy_grail():
    kwargs = dict(current_ev=0, luminance=.01, base_shutter_s=1/60, base_iso=100, caps=A7II_CAPS)
    auto = metered_timelapse_step(timelapse_config("auto_custom"), **kwargs)
    holy = metered_timelapse_step(timelapse_config("holy_grail"), **kwargs)
    assert auto["requested_ev"] == 1.0
    assert holy["requested_ev"] == .3


def test_camera_quantization_stays_inside_total_ev_limit():
    config = timelapse_config("holy_grail", {"max_total_ev": .3})
    result = metered_timelapse_step(config, current_ev=0, luminance=.01,
                                   base_shutter_s=1/60, base_iso=100, caps=A7II_CAPS)
    assert abs(result["actual_ev"]) <= .3


def test_actual_capture_interval_is_monotonic_when_wall_clock_changes(tmp_path, monkeypatch):
    import itertools
    import stack_session
    runner = SessionRunner(tmp_path)
    state = runner.create("timelapse", 3)
    backward = itertools.count(1000000, -100)
    monkeypatch.setattr(stack_session.time, "time", lambda: next(backward))
    def capture(index, spec, destination):
        Image.new("RGB", (16, 16), (index, 2, 3)).save(destination)
        return destination
    result = runner.run(state["id"], capture)
    assert result["status"] == "completed"
    assert all(f["actual_interval_source"] == "monotonic" and f["actual_interval_s"] > 0
               for f in result["frames"][1:])
