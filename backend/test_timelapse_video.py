"""Camera-free 1080p H.264 timelapse tests. Probe the encoder by running it."""
from __future__ import annotations

import stat

import pytest
from PIL import Image, ImageDraw

from timelapse_video import (
    HEIGHT,
    WIDTH,
    decode_frames,
    playback_duration_s,
    probe_encoder,
    render_h264,
    verify_h264_probe,
)


def _require_encoder():
    tools = probe_encoder()
    if tools is None:
        pytest.skip("ffmpeg/ffprobe did not execute (-version); not scored here")
    return tools


def _numbered_jpeg(path, index, size=(320, 240), color=(220, 40, 40)):
    image = Image.new("RGB", size, color)
    ImageDraw.Draw(image).text((12, 12), str(index), fill=(255, 255, 255))
    image.save(path, format="JPEG", quality=95)


def _stub(directory, name, script):
    path = directory / name
    path.write_text(script)
    path.chmod(path.stat().st_mode | stat.S_IEXEC)
    return path


def test_playback_duration_is_frames_over_fps():
    assert playback_duration_s(24, 24) == 1.0
    assert playback_duration_s(3, 24) == pytest.approx(0.125)
    with pytest.raises(ValueError):
        playback_duration_s(0, 24)


def test_verify_rejects_non_1080p_or_wrong_count():
    with pytest.raises(ValueError):
        verify_h264_probe(
            {"codec_name": "h264", "width": 64, "height": 48, "nb_read_frames": "3", "duration": "0.125"},
            3, 24,
        )
    with pytest.raises(ValueError):
        verify_h264_probe(
            {"codec_name": "h264", "width": 1920, "height": 1080, "nb_read_frames": "2", "duration": "0.125"},
            3, 24,
        )


def test_path_stub_that_fails_version_is_not_an_encoder(tmp_path, monkeypatch):
    _stub(tmp_path, "ffmpeg", "#!/bin/sh\necho 'Unable to locate ffmpeg runtime' >&2\nexit 1\n")
    _stub(tmp_path, "ffprobe", "#!/bin/sh\necho 'Unable to locate ffprobe runtime' >&2\nexit 1\n")
    monkeypatch.setenv("PATH", str(tmp_path))
    import shutil
    assert shutil.which("ffmpeg") is not None
    assert probe_encoder() is None


def test_missing_binaries_are_unavailable(tmp_path, monkeypatch):
    monkeypatch.setenv("PATH", str(tmp_path))
    assert probe_encoder() is None


def test_render_is_1080p_h264_with_matching_frames_and_duration(tmp_path):
    tools = _require_encoder()
    frames = tmp_path / "frames"
    frames.mkdir()
    colors = [(200, 30, 30), (30, 180, 40), (40, 40, 200)]
    for index, color in enumerate(colors):
        _numbered_jpeg(frames / f"frame_{index:04d}.jpg", index, size=(480, 320), color=color)
    pending = tmp_path / "out.mp4"
    pending.touch()
    info = render_h264(frames, 3, 24, pending, tools=tools)
    assert int(info["width"]) == WIDTH and int(info["height"]) == HEIGHT
    assert info["codec_name"] == "h264"
    assert int(info["nb_read_frames"]) == 3
    assert info["playback_duration_s"] == pytest.approx(3 / 24)
    decoded = decode_frames(pending, tmp_path / "decoded", tools=tools)
    assert len(decoded) == 3
    first = Image.open(decoded[0]).convert("RGB")
    last = Image.open(decoded[2]).convert("RGB")
    assert first.size == (WIDTH, HEIGHT)
    # 3:2 content fills the frame edge to edge (centre-cropped, not pillarboxed).
    assert first.getpixel((WIDTH // 2, HEIGHT // 2))[0] > 150
    assert first.getpixel((8, HEIGHT // 2))[0] > 150, "black side bars: pillarboxed, not cropped"
    assert last.getpixel((WIDTH // 2, HEIGHT // 2))[2] > 150


def test_three_by_two_stills_are_cropped_not_stretched(tmp_path):
    """Filling 16:9 from 3:2 must trim top and bottom. A stretch would also fill the edges."""
    tools = _require_encoder()
    frames = tmp_path / "frames"
    frames.mkdir()
    for index in range(2):
        still = Image.new("RGB", (600, 400), (200, 30, 30))
        # A band in the top 5% of the still. Covering 1080p scales 400 -> 1280 rows and crops
        # 100 from each end (~7.8%), so the band must be gone; a stretch would keep it.
        still.paste((30, 30, 200), (0, 0, 600, 20))
        still.save(frames / f"frame_{index:04d}.jpg", quality=95)
    pending = tmp_path / "out.mp4"
    pending.touch()
    render_h264(frames, 2, 12, pending, tools=tools)
    first = Image.open(decode_frames(pending, tmp_path / "decoded", tools=tools)[0]).convert("RGB")
    assert first.size == (WIDTH, HEIGHT)
    top = first.getpixel((WIDTH // 2, 4))
    assert top[0] > 150 and top[2] < 90, f"top band survived, so the frame was stretched: {top}"


def test_preview_render_matches_two_frame_duration(tmp_path):
    tools = _require_encoder()
    frames = tmp_path / "frames"
    frames.mkdir()
    for index in range(2):
        _numbered_jpeg(frames / f"frame_{index:04d}.jpg", index, size=(1920, 1080), color=(20, 90, 20))
    pending = tmp_path / "preview.mp4"
    pending.touch()
    info = render_h264(frames, 2, 12, pending, preview=True, tools=tools)
    assert int(info["nb_read_frames"]) == 2
    assert info["playback_duration_s"] == pytest.approx(2 / 12)
    assert len(decode_frames(pending, tmp_path / "decoded", tools=tools)) == 2
