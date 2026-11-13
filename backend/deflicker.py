"""Holy-Grail timelapse deflicker: bounded, deterministic per-frame luminance gains.

Ramping the exposure between timelapse frames (day-to-night "Holy Grail") produces
brightness flicker because Sony's shutter/ISO/aperture move in quantised steps. This
module measures each frame's mean luminance cheaply (reduced-scale JPEG decode, never a
full decode), fits a smooth trend through the log-luminance series, and derives a single
multiplicative gain per frame that pushes it back onto that trend. The gain is clamped so
correction can never invent more than ``max_gain_stops`` of exposure, endpoints are handled
without an edge-replication bias, and a series that is already smooth yields gains of 1.0.

No camera operations, no originals are modified: :func:`apply_gains_to_frames` writes fresh
JPEG copies one frame at a time and returns a receipt for each so the correction is auditable.
"""
from __future__ import annotations

import hashlib
import math
from pathlib import Path

from PIL import Image, ImageStat

# Downscale target for the luminance measurement. Full-resolution pixels are not needed
# for a mean, so each decoded frame is thumbnailed before measurement.
_LUMA_DECODE_BOX = (64, 64)
_LOG_LUMA_FLOOR = 1.0 / 255.0  # keep log2 finite for a (near) black frame


def luminance_series(paths) -> list[float]:
    """Mean luminance (0..255) per frame, decoded once on the guarded, bounded path.

    Deterministic: the same file always yields the same value. Decoding goes through
    ``image_safety`` so the shared pre-decode budgets (source bytes, pixel count, decoded
    bytes) are enforced; the frame is then thumbnailed to a small box so only the mean of
    a downscaled copy is measured, not the full-resolution image.
    """
    from image_safety import open_rgb  # bounded, validated decode (no raw Image.open here)

    values: list[float] = []
    for path in paths:
        frame = open_rgb(path).convert("L")
        frame.thumbnail(_LUMA_DECODE_BOX)
        values.append(float(ImageStat.Stat(frame).mean[0]))
    return values


def _smooth_log_luma(log_luma: list[float], window: int) -> list[float]:
    """Centered, symmetric, half-weighted moving average of the log-luminance series.

    A symmetric kernel reproduces a linear ramp exactly, and half-weighting the two ends
    nulls the per-frame (Nyquist) alternation that is the flicker itself. The window is
    truncated symmetrically toward each edge so no direction is favored; the two extreme
    samples, where a symmetric window collapses, average with their single neighbor (an
    even, two-sample mean cancels a +/- alternation with the least possible ramp bias)
    instead of replicating the edge value.
    """
    n = len(log_luma)
    half = window // 2
    smooth: list[float] = []
    for index in range(n):
        reach = min(half, index, n - 1 - index)
        if reach == 0:
            if n >= 2:
                pair = (log_luma[0], log_luma[1]) if index == 0 else (log_luma[-2], log_luma[-1])
                smooth.append((pair[0] + pair[1]) / 2.0)
            else:
                smooth.append(log_luma[index])
            continue
        total = 0.5 * log_luma[index - reach] + 0.5 * log_luma[index + reach]
        for offset in range(index - reach + 1, index + reach):
            total += log_luma[offset]
        smooth.append(total / (2 * reach))
    return smooth


def deflicker_gains(luma, *, window: int = 7, max_gain_stops: float = 1.0) -> list[float]:
    """Per-frame multiplicative gains that pull each frame's luminance onto a smooth trend.

    ``gain[i] = 2 ** clamp(smooth_logluma[i] - logluma[i], +/- max_gain_stops)``. An
    already-smooth series has ``smooth == logluma`` and therefore gains of 1.0.
    """
    if not isinstance(window, int) or isinstance(window, bool) or window < 1 or window % 2 == 0:
        raise ValueError("window must be a positive odd integer")
    if not isinstance(max_gain_stops, (int, float)) or isinstance(max_gain_stops, bool):
        raise ValueError("max_gain_stops must be a positive finite number")
    if not math.isfinite(max_gain_stops) or max_gain_stops <= 0:
        raise ValueError("max_gain_stops must be a positive finite number")
    values = [float(v) for v in luma]
    if not values:
        return []
    log_luma = [math.log2(max(v, _LOG_LUMA_FLOOR)) for v in values]
    smooth = _smooth_log_luma(log_luma, window)
    gains: list[float] = []
    for reference, target in zip(log_luma, smooth):
        delta = max(-max_gain_stops, min(max_gain_stops, target - reference))
        gains.append(2.0 ** delta)
    return gains


def _apply_gain(image: Image.Image, gain: float) -> Image.Image:
    """Multiply every channel by ``gain`` through a single clamped 8-bit lookup table."""
    lookup = [min(255, max(0, round(value * gain))) for value in range(256)]
    bands = len(image.getbands())
    return image.point(lookup * bands)


def apply_gains_to_frames(paths, gains, out_dir, *, quality: int = 95) -> list[Path]:
    """Write gain-corrected JPEG copies (one frame in memory at a time); never touch originals.

    Each output keeps the source file's name under ``out_dir``. EXIF is dropped, which is
    fine for a render input; callers that need provenance pair this with :func:`frame_receipts`.
    """
    from image_safety import open_rgb  # bounded, validated RGB decode

    sources = [Path(p) for p in paths]
    gains = list(gains)
    if len(sources) != len(gains):
        raise ValueError("gains must align one-to-one with frames")
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    if out_dir.is_symlink():
        raise ValueError("deflicker output directory cannot be a symlink")
    written: list[Path] = []
    for source, gain in zip(sources, gains):
        if not math.isfinite(gain) or gain <= 0:
            raise ValueError("deflicker gain must be positive and finite")
        destination = out_dir / source.name
        corrected = _apply_gain(open_rgb(source), float(gain))
        corrected.save(destination, format="JPEG", quality=quality)
        written.append(destination)
    return written


def frame_receipts(paths, out_paths, gains) -> list[dict]:
    """Auditable {source sha256, gain, out sha256} per corrected frame."""
    receipts: list[dict] = []
    for source, produced, gain in zip(paths, out_paths, gains):
        source_data = Path(source).read_bytes()
        out_data = Path(produced).read_bytes()
        receipts.append({
            "file": Path(produced).name,
            "gain": float(gain),
            "source_sha256": hashlib.sha256(source_data).hexdigest(),
            "out_sha256": hashlib.sha256(out_data).hexdigest(),
        })
    return receipts
