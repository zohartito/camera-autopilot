"""Truthful same-scene mood previews derived from the axes capture ACTUALLY applies.

Why this exists
---------------
The mood variants (``intent.MOOD_VARIANTS``) drive only two real capture axes: a mean-brightness
**target** (turned into an EV bias — ``engine.py`` subtracts ``2.2 * log2(target/112)`` stops at
metering time) and a **white-balance** direction (``warm`` -> 7500K, ``cool`` -> 4000K via
``cam.set_color_temp``). The web UI, however, previews variants with a CSS ``filter`` string that has
nothing to do with those numbers, so the little swatch a user picks from can look nothing like the
frame the camera will produce.

This module renders a preview JPEG from the SAME two axes the engine applies, so "what you see" and
"what gets captured" cannot silently diverge:

* **Brightness** — scale the source pixels so the mean luma moves from the source's own mean toward
  the variant's ``target_brightness``. The engine's neutral metering pivot is 112 (0-255 mean-luma
  scale; ``engine.py`` line ~570); the variant target is a value on that same scale. We move the
  source's mean onto that target with ``gain = (target / source_mean) ** BRIGHTNESS_K`` — the log2/
  exponent form of the engine's EV bias, referenced to the source's own mean so the preview is
  faithful to THIS scene rather than assuming the source was already neutral. With ``BRIGHTNESS_K``
  == 1.0 the preview's mean lands exactly on the target — the quantity the engine's exposure loop
  converges toward. (When a source already sits at the neutral 112, this reduces to the engine's own
  ``(target / 112)`` gain.)
* **White balance** — a small, stated per-channel tint approximating the kelvin change: a warm
  (7500K) request renders warmer (more red, less blue -> a higher R/B ratio); a cool (4000K) request
  renders cooler (more blue, less red); neutral/auto leaves color untouched.

Everything here is deterministic, bounded in memory (ingress goes through ``image_safety``'s guarded
pre-decode budget, then ``thumbnail`` before any pixel work), strips EXIF, and never touches a camera
or the network.
"""

from __future__ import annotations

from io import BytesIO
from math import isfinite
from pathlib import Path

# The engine's neutral metering pivot on the 0-255 mean-luma scale (engine.py ~line 570:
# ``scene_ev100 -= 2.2 * log2(target/112)``). Documented here so the preview's brightness model and
# the engine's exposure model name the same neutral.
LUMA_NEUTRAL = 112.0

# Brightness easing exponent. 1.0 lands the preview's mean luma exactly on the variant target (the
# value the engine's exposure loop drives toward). A smaller k would render a partial nudge toward
# the target instead of the full move; we use the full move so the preview does not understate the
# variant.
BRIGHTNESS_K = 1.0

# Clamp the brightness gain so a very dark or very bright source can't blow the preview out. Bounds
# a ~2-stop move in either direction.
GAIN_MIN = 0.25
GAIN_MAX = 4.0

# White-balance tints as (red, green, blue) channel multipliers. Warm renders warmer (R up, B down)
# -> raises the R/B ratio; cool is the mirror. Deliberately gentle and luma-near-neutral so the WB
# tint doesn't fight the brightness axis. Keyed by the variant's ``white_balance`` field.
WB_TINTS = {
    "warm": (1.12, 1.0, 0.88),
    "cool": (0.88, 1.0, 1.12),
    None: (1.0, 1.0, 1.0),
}

# Preview encode quality. Fixed so repeated renders of the same source produce byte-identical output.
_JPEG_QUALITY = 90


def variant_recipe_from_values(variant: dict) -> str:
    """Generate the human recipe string FROM a variant's numeric fields.

    Display and application share one source of truth: the recipe a user reads is computed from the
    exact ``target_brightness`` and ``white_balance`` the engine applies, so the two cannot drift.
    Reproduces the format ``brightness target <int> / <wb> WB`` (auto when no WB direction is set).
    """
    target = int(round(float(variant["target_brightness"])))
    white_balance = variant.get("white_balance") or "auto"
    return f"brightness target {target} / {white_balance} WB"


def render_variant_preview(source_jpeg_path, variant: dict, *, max_width: int = 640) -> bytes:
    """Render one variant's truthful preview from an existing JPEG, as JPEG bytes.

    Applies the variant's real brightness target and white-balance direction (see module docstring)
    to a memory-bounded copy of the source. EXIF is stripped; output is deterministic for a given
    (source, variant, max_width).
    """
    base = _load_bounded(source_jpeg_path, max_width)
    base_mean = _mean_luma(base)
    return _encode(_apply(base, base_mean, variant))


def render_all_previews(source_jpeg_path, variants, *, max_width: int = 640) -> dict:
    """Render every variant from ONE bounded load of the source. Returns ``{variant_id: jpeg bytes}``.

    Loading the source once (and never mutating it) keeps the render both cheaper and byte-identical
    to calling :func:`render_variant_preview` per variant.
    """
    base = _load_bounded(source_jpeg_path, max_width)
    base_mean = _mean_luma(base)
    return {variant["id"]: _encode(_apply(base, base_mean, variant)) for variant in variants}


# --- internals ------------------------------------------------------------------------------------


def _load_bounded(source_jpeg_path, max_width: int):
    """Decode the source into a bounded RGB image via the guarded ingress path, then thumbnail.

    ``image_safety.open_rgb`` is the repo's single guarded image-ingress helper: it enforces the
    source-byte cap, refuses a symlink/oversized file, and rejects an image whose decoded RGB size
    would blow the budget — all before returning a validated RGB copy. ``thumbnail`` then bounds both
    dimensions to ``max_width`` (without upscaling) so all subsequent pixel work is small.
    """
    from image_safety import open_rgb

    if not isinstance(max_width, int) or max_width < 1:
        raise ValueError("max_width must be a positive integer")

    image = open_rgb(Path(source_jpeg_path))
    image.thumbnail((max_width, max_width))
    return image


def _mean_luma(image) -> float:
    """Mean luma (0-255) of an RGB image via the standard L conversion. Floored at 1.0 for safe division."""
    from PIL import ImageStat

    return max(1.0, ImageStat.Stat(image.convert("L")).mean[0])


def _brightness_gain(target_brightness: float, source_mean: float) -> float:
    """Gain that moves ``source_mean`` toward ``target_brightness`` on the engine's mean-luma scale.

    ``gain = (target / source_mean) ** BRIGHTNESS_K`` — the exponent form of the engine's EV bias,
    referenced to the source's own mean so the preview is faithful to this scene. Clamped to keep a
    dark/bright source from blowing out.
    """
    target = float(target_brightness)
    if not isfinite(target) or target <= 0:
        return 1.0
    gain = (target / source_mean) ** BRIGHTNESS_K
    return max(GAIN_MIN, min(GAIN_MAX, gain))


def _apply(base, base_mean: float, variant: dict):
    """Return a NEW image with the variant's brightness gain and WB tint applied. ``base`` is untouched."""
    from PIL import Image

    gain = _brightness_gain(variant["target_brightness"], base_mean)
    red_mult, green_mult, blue_mult = WB_TINTS.get(variant.get("white_balance"), WB_TINTS[None])

    channels = base.split()  # R, G, B (base stays unmodified)
    tinted = [
        channel.point(_channel_lut(gain * mult))
        for channel, mult in zip(channels, (red_mult, green_mult, blue_mult))
    ]
    return Image.merge("RGB", tinted)


def _channel_lut(scale: float):
    """A 256-entry lookup mapping v -> clamp(round(v * scale)). Deterministic and pass-through fast."""
    return [min(255, max(0, round(value * scale))) for value in range(256)]


def _encode(image) -> bytes:
    """Encode to JPEG bytes with EXIF stripped and a fixed quality (deterministic output)."""
    buffer = BytesIO()
    image.save(buffer, format="JPEG", quality=_JPEG_QUALITY)
    return buffer.getvalue()
