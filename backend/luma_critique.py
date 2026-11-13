"""Deterministic exposure critique from the captured JPEG itself.

Why this exists
---------------
Refine (``reflexion_shoot``) leans on ``vision_critique.critique`` to judge whether the delivered
frame is well exposed. When the vision model service (``VISION_HOST`` / Ollama) is unreachable the
critique closure raised, the loop caught the throw, degraded to "no critique", and a badly exposed
photo was returned as-is (``stop_reason: no_critique``) — Refine did nothing. Field finding
2026-09-08.

This module gives Refine a critic that needs no network: it measures the mean luma of the actual
JPEG and judges it against the intent's target brightness. It is a fallback, never silent — the
critique it returns is tagged ``source: "luma_fallback"`` so the critique trail and Why card show
which critic judged the frame.

Contract
--------
``critique_from_image`` returns the SAME keys ``vision_critique.critique`` returns
(``matches_intent`` / ``exposure`` / ``brightness_delta`` / ``issues`` / ``suggestion``), so
``reflexion.critique_is_satisfied`` and ``reflexion.corrected_target`` consume it unchanged. It adds
``mean_brightness``, ``stop_error`` and ``source`` for the trail. ``brightness_delta`` is the
MEASURED target shift (target minus mean), not a fixed nudge, so ``corrected_target`` moves the next
target by the amount the frame is actually off — the correction converges instead of guessing.
"""

from __future__ import annotations

import math
from pathlib import Path

from PIL import ImageStat

from image_safety import open_rgb

# Mean luma is judged in stops. A frame within this many stops of the target is "good"; the same
# 0.35-stop band both accepts a frame and defines the boundary between "too bright" and "too dark".
DEFAULT_TOLERANCE_STOPS = 0.35

# Downscale the (up to 24MP) JPEG before measuring — a few hundred pixels give a stable mean and
# histogram, and keep the fallback cheap. Deterministic: same bytes in, same numbers out.
_MEASURE_MAX = (256, 256)

# Clipped highlights: pixels at or above this 8-bit luma are effectively blown.
_CLIP_HIGH = 250
# Crushed shadows: pixels at or below this are effectively black.
_CLIP_LOW = 5
# Only surface a clipping/crushing issue once it covers a meaningful share of the frame.
_CLIP_ISSUE_FRACTION = 0.02


def _measure(image_path: str | Path) -> tuple[float, float, float]:
    """Return (mean_luma, clipped_fraction, crushed_fraction) from a downscaled, guarded decode."""
    image = open_rgb(image_path)  # bounded, guarded decode — never a raw Image.open
    image.thumbnail(_MEASURE_MAX)
    gray = image.convert("L")
    mean = float(ImageStat.Stat(gray).mean[0])

    histogram = gray.histogram()  # 256 luma bins
    total = float(sum(histogram)) or 1.0
    clipped = sum(histogram[_CLIP_HIGH:]) / total
    crushed = sum(histogram[: _CLIP_LOW + 1]) / total
    return mean, clipped, crushed


def critique_from_image(
    image_path: str | Path,
    target_brightness: float,
    *,
    tolerance_stops: float = DEFAULT_TOLERANCE_STOPS,
) -> dict:
    """Judge a captured JPEG's exposure against the intent's target brightness, deterministically.

    Parameters
    ----------
    image_path : path to the delivered JPEG.
    target_brightness : the intent brain's mean-brightness goal for this photo (0-255 scale).
    tolerance_stops : half-width of the "good" band, in stops.

    Returns a critique dict shaped like ``vision_critique.critique`` plus ``mean_brightness``,
    ``stop_error`` and ``source: "luma_fallback"``.
    """
    mean, clipped, crushed = _measure(image_path)
    target = max(1.0, float(target_brightness))
    # Guard the log against a fully black frame; a hair above zero keeps the stop error finite.
    stop_error = math.log2(max(mean, 0.5) / target)

    if abs(stop_error) <= tolerance_stops:
        exposure = "good"
        matches_intent = True
        suggestion = "Exposure is on target; no change needed."
    elif stop_error > 0:
        exposure = "too bright"
        matches_intent = False
        suggestion = f"Frame is {stop_error:+.2f} stops bright; lower the exposure target."
    else:
        exposure = "too dark"
        matches_intent = False
        suggestion = f"Frame is {stop_error:+.2f} stops dark; raise the exposure target."

    observations: list[str] = []
    if clipped > _CLIP_ISSUE_FRACTION:
        observations.append(f"clipped highlights ({clipped * 100:.0f}% of pixels blown)")
    if crushed > _CLIP_ISSUE_FRACTION:
        observations.append(f"crushed shadows ({crushed * 100:.0f}% of pixels black)")
    # A reshoot at the same target cannot fix clipping when the mean is already on target, so
    # clipping is an issue only when it agrees with the exposure verdict; otherwise it is reported
    # without forcing another shutter.
    issues = [] if exposure == "good" else list(observations)

    # The measured correction: shift the target toward the goal by exactly how far the frame missed.
    # Positive brightens (raise target), matching vision_critique's sign convention; corrected_target
    # clamps the resulting target to the brain's own range.
    brightness_delta = round(target - mean)

    return {
        "matches_intent": matches_intent,
        "exposure": exposure,
        "brightness_delta": brightness_delta,
        "issues": issues,
        "observations": observations,
        "suggestion": suggestion,
        "mean_brightness": round(mean, 2),
        "stop_error": round(stop_error, 3),
        "source": "luma_fallback",
    }


def critique_with_fallback(image_path: str | Path, intent: str | None, target_brightness: float) -> dict | None:
    """Vision critic first; the deterministic luma critic when vision is unreachable.

    Tries ``vision_critique.critique``. If it raises (host down / HTTP error) or returns an empty
    critique, fall back to ``critique_from_image`` so Refine still corrects exposure offline. The
    returned critique always carries a ``source`` so the fallback is never silent: ``"vision"`` when
    the model judged the frame, ``"luma_fallback"`` when this module did.
    """
    try:
        from vision_critique import critique as assess

        result = assess(image_path, intent)
    except Exception:
        result = None
    if result:
        result.setdefault("source", "vision")
        return result
    return critique_from_image(image_path, target_brightness)
