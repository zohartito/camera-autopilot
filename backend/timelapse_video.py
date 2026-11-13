"""1080p H.264 for timelapse stills. Probe the encoder by running it; PATH is not enough."""
from __future__ import annotations

import json
import math
import shutil
import subprocess
from pathlib import Path

WIDTH = 1920
HEIGHT = 1080
# Cover 1080p, then centre-crop to it, so a 3:2 still fills the 16:9 frame instead of being
# pillarboxed with black side bars (owner's call, 2026-09-22). A 6000x4000 frame scales to
# 1920x1280 and loses 100 px top and bottom. Even dimensions for yuv420p. Originals stay JPEG.
SCALE_FILTER = (
    "scale=w=1920:h=1080:force_original_aspect_ratio=increase:force_divisible_by=2,"
    "crop=1920:1080"
)
ENCODER = "libx264"
PROBE_TIMEOUT_S = 8
_VERSION_MARKERS = {
    "ffmpeg": b"ffmpeg version",
    "ffprobe": b"ffprobe version",
}


class EncoderUnavailable(RuntimeError):
    """ffmpeg/ffprobe was missing, a stub, or did not actually execute."""


def playback_duration_s(frame_count, fps):
    """Playback length of N frames at fps. Exact; not the real-world capture span."""
    if type(frame_count) is not int or frame_count < 1:
        raise ValueError("frame_count must be a positive int")
    if type(fps) not in (int, float) or not math.isfinite(fps) or not 1 <= float(fps) <= 60:
        raise ValueError("fps must be a finite number 1..60")
    return frame_count / float(fps)


def _executes(path, marker):
    try:
        result = subprocess.run(
            [path, "-version"], capture_output=True, check=False, timeout=PROBE_TIMEOUT_S,
        )
    except (OSError, subprocess.TimeoutExpired):
        return False
    blob = result.stdout + result.stderr
    return result.returncode == 0 and marker in blob


def probe_encoder():
    """Return ffmpeg/ffprobe paths only if both run `-version` successfully.

    macOS (and some package stubs) can put a non-functional binary on PATH.
    `shutil.which` is not a probe.
    """
    ffmpeg = shutil.which("ffmpeg")
    ffprobe = shutil.which("ffprobe")
    if not ffmpeg or not ffprobe:
        return None
    if not _executes(ffmpeg, _VERSION_MARKERS["ffmpeg"]):
        return None
    if not _executes(ffprobe, _VERSION_MARKERS["ffprobe"]):
        return None
    return {"ffmpeg": ffmpeg, "ffprobe": ffprobe}


def verify_h264_probe(info, frame_count, fps):
    """Require 1920×1080 H.264, exact frame count, playback duration within one frame."""
    expected = playback_duration_s(frame_count, fps)
    duration_raw = info.get("duration")
    try:
        duration = float(duration_raw)
        width = int(info["width"])
        height = int(info["height"])
        frames = int(info["nb_read_frames"])
    except (TypeError, ValueError, KeyError) as exc:
        raise ValueError("video probe is missing duration, size or frame count") from exc
    if (info.get("codec_name") != "h264" or frames != frame_count
            or width != WIDTH or height != HEIGHT
            or not math.isfinite(duration)
            or abs(duration - expected) > (1.0 / float(fps)) + 1e-3):
        raise ValueError(
            f"rendered video is not 1080p H.264 with {frame_count} frames / {expected}s "
            f"(got {info.get('codec_name')} {width}x{height} frames={info.get('nb_read_frames')} "
            f"duration={duration_raw})"
        )
    return expected


def _probe_file(ffprobe, path, timeout_s=30):
    result = subprocess.run(
        [ffprobe, "-v", "error", "-select_streams", "v:0", "-count_frames",
         "-show_entries", "stream=codec_name,width,height,nb_read_frames,duration,r_frame_rate",
         "-show_entries", "format=duration", "-of", "json", str(path)],
        capture_output=True, check=True, timeout=timeout_s,
    )
    payload = json.loads(result.stdout)
    info = payload["streams"][0]
    if info.get("duration") in (None, "N/A", ""):
        info["duration"] = (payload.get("format") or {}).get("duration")
    return info


def render_h264(frames_dir, frame_count, fps, pending, *, preview=False, tools=None):
    """Encode `frame_%04d.jpg` from frames_dir into pending; fully decode; return probe."""
    tools = tools or probe_encoder()
    if not tools:
        raise EncoderUnavailable(
            "Local ffmpeg and ffprobe must execute (-version); JPEG sequence is preserved."
        )
    encode_timeout = 8 if preview else 180
    decode_timeout = 4 if preview else 60
    probe_timeout = 4 if preview else 30
    byte_limit = 16 * 1024 * 1024 if preview else 128 * 1024 * 1024
    command = [
        tools["ffmpeg"], "-nostdin", "-v", "error", "-y", "-threads", "1",
        "-framerate", str(fps), "-i", str(Path(frames_dir) / "frame_%04d.jpg"),
        "-frames:v", str(frame_count), "-r", str(fps), "-an",
        "-vf", SCALE_FILTER, "-c:v", ENCODER,
    ]
    if preview:
        command.extend(["-preset", "ultrafast"])
    command.extend([
        "-threads", "1", "-pix_fmt", "yuv420p", "-movflags", "+faststart",
        "-fs", str(byte_limit), str(pending),
    ])
    subprocess.run(command, capture_output=True, check=True, timeout=encode_timeout)
    info = _probe_file(tools["ffprobe"], pending, timeout_s=probe_timeout)
    expected = verify_h264_probe(info, frame_count, fps)
    subprocess.run(
        [tools["ffmpeg"], "-nostdin", "-v", "error", "-xerror", "-i", str(pending), "-f", "null", "-"],
        capture_output=True, check=True, timeout=decode_timeout,
    )
    info["playback_duration_s"] = expected
    return info


def decode_frames(video, out_dir, *, tools=None):
    """Decode every frame to PNG and return them in order. Camera-free proof of playability."""
    tools = tools or probe_encoder()
    if not tools:
        raise EncoderUnavailable(
            "Local ffmpeg and ffprobe must execute (-version); JPEG sequence is preserved."
        )
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    pattern = out_dir / "decoded_%04d.png"
    subprocess.run(
        [tools["ffmpeg"], "-nostdin", "-v", "error", "-xerror", "-i", str(video),
         str(pattern)],
        capture_output=True, check=True, timeout=60,
    )
    return sorted(out_dir.glob("decoded_*.png"))
