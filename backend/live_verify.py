"""One-shot live verify: meter a captured frame, then run the brain on it.

Bridges the sony-camera MCP (which hands us a JPEG + the settings it was shot at) to the M1
brain: estimate the scene EV100 from the frame's brightness, pull focal length from EXIF, and
ask recommend_settings for the three constraint cards.

    backend/.venv-ml/bin/python backend/live_verify.py <frame.jpg> <iso> <f_number> <shutter_s>
"""
from __future__ import annotations

import math
import sys
from pathlib import Path

import numpy as np
from PIL.ExifTags import TAGS

sys.path.insert(0, str(Path(__file__).resolve().parent))

import physics
from image_safety import open_image, open_rgb

# `brain` (and its heavy CLIP taste-index deps) is imported lazily in __main__ only, so the metering
# functions here stay importable from the WiFi loop / tracking / tests without the ML stack present.


_REC709 = np.array([0.2126, 0.7152, 0.0722], dtype="float32")  # luminance weights for sRGB content
_MIDDLE_GREY_LINEAR = 0.18  # 18% reflectance = middle grey (~118 in 8-bit sRGB, NOT 128)

# LIVEVIEW-vs-STILL OFFSET (the A7 II WiFi calibration). HYPOTHESIS: the 2MP liveview JPEG is gained-up in
# dim scenes, reading BRIGHTER than the matching still, so a meter that trusts it over-reads EV and
# under-exposes. MEASURED LIVE 2026-06-22 at two points: liveview vs still is -0.04 stops @ EV 1.7 (a dim
# room) and only +0.21 stops at absolute black (lens capped, EV -7.6). So the effect is NEGLIGIBLE across
# the whole range on this body — the original underexposure was ISO stuck on AUTO (fixed by the dial-on-M
# change), NOT a liveview gain. _LV_OFFSET_MAX_EV is therefore 0 (no spurious correction). The ramp is kept
# so a future body that DOES gain its liveview can be calibrated by setting MAX from a grey-card A/B.
_LV_OFFSET_MAX_EV = 0.0   # MEASURED ~0 (-0.04 @ EV1.7, +0.21 @ black) -> the meter is accurate; no correction
_LV_OFFSET_KNEE_EV = 6.0  # scene EV at/above which the preview matches the still (offset -> 0)
_LV_OFFSET_FLOOR_EV = 0.0  # scene EV at/below which the full correction applies


def liveview_still_offset_ev(raw_liveview_ev100: float) -> float:
    """Stops to SUBTRACT from a liveview-metered EV so it predicts the STILL's exposure. Linear
    ramp from the full correction at/below _LV_OFFSET_FLOOR_EV up to zero at/above _LV_OFFSET_KNEE_EV.
    Calibrated against the field note that dim liveview drove ISO to 6400 (over-bright preview ->
    EV over-read -> under-exposed still). Tune _LV_OFFSET_MAX_EV from a grey-card A/B if needed."""
    if raw_liveview_ev100 >= _LV_OFFSET_KNEE_EV:
        return 0.0
    if raw_liveview_ev100 <= _LV_OFFSET_FLOOR_EV:
        return _LV_OFFSET_MAX_EV
    span = _LV_OFFSET_KNEE_EV - _LV_OFFSET_FLOOR_EV
    frac = (_LV_OFFSET_KNEE_EV - raw_liveview_ev100) / span  # 0 at knee, 1 at floor
    return _LV_OFFSET_MAX_EV * frac


def _srgb_to_linear(c: np.ndarray) -> np.ndarray:
    """Exact sRGB inverse EOTF (gamma-decode) so we average in scene-LINEAR light, not gamma —
    a plain ^2.2 over-weights the brights. (deep-dive research, Topic 2)."""
    return np.where(c <= 0.04045, c / 12.92, ((c + 0.055) / 1.055) ** 2.4)


def _crop_region(rgb: np.ndarray, region: tuple[float, float, float, float]) -> np.ndarray:
    """Crop to a normalized (x, y, w, h) box in 0-1 fractions; an empty box -> the whole frame."""
    h, w = rgb.shape[:2]
    x, y, rw, rh = region
    x0, y0, x1, y1 = int(x * w), int(y * h), int((x + rw) * w), int((y + rh) * h)
    crop = rgb[max(0, y0):min(h, y1), max(0, x0):min(w, x1)]
    return crop if crop.size else rgb


METER_LOW_LUMA = 6.0     # 8-bit mean below this: the JPEG is essentially black; EV is a noise-floor guess
METER_HIGH_LUMA = 249.0  # 8-bit mean above this: nearly everything clipped; EV is only a floor


def meter_reliability(image_path: str) -> dict:
    """Report whether one JPEG meter frame can support a trustworthy EV estimate.

    JPEG metering works over a few stops around a usable exposure; a nearly black or nearly
    white frame produces an EV that is a bound, not a measurement. The engine records this so
    the result explains itself and Refine can be recommended instead of trusting one frame.
    """
    with open_rgb(image_path) as image:
        image.draft("RGB", (600, 400))
        rgb = np.asarray(image.convert("RGB"), dtype=np.float32)
    mean = float((0.2126 * rgb[..., 0] + 0.7152 * rgb[..., 1] + 0.0722 * rgb[..., 2]).mean())
    if mean < METER_LOW_LUMA:
        return {"status": "low", "mean_luma": round(mean, 2),
                "reason": "meter frame is nearly black; the EV estimate is a noise-floor guess"}
    if mean > METER_HIGH_LUMA:
        return {"status": "low", "mean_luma": round(mean, 2),
                "reason": "meter frame is nearly white; the EV estimate is only a lower bound"}
    return {"status": "ok", "mean_luma": round(mean, 2), "reason": None}


def meter_ev100(image_path: str, iso: float, f_number: float, shutter_s: float,
                region: tuple[float, float, float, float] | None = None,
                from_liveview: bool = False) -> tuple[float, float]:
    """Estimate scene EV100 from a frame + the exposure it was shot at, the way real meters do
    (deep-dive research, Topic 2): linearize sRGB exactly, take a robust log-average key (the
    Reinhard scene-key estimator), anchor to 18% middle grey, and PROTECT HIGHLIGHTS — a clipped
    frame underreads, so bias the estimate up as clipping grows (the Google HDR+ principle: meter
    so highlights don't blow, recover shadows later). `region` (SALIENCY: a normalized subject box)
    meters for the subject only — expose the subject right, let the background fall where it may.
    `from_liveview=True` applies the liveview-vs-still EV offset: the 2MP preview is gained-up for
    visibility and reads brighter than the matching still in dim light, so we subtract the
    calibrated correction to expose for the STILL, not the preview (else the brain under-exposes).
    Returns (scene_ev100, clipped_fraction); the EV is a *floor* when clipping — on heavy clipping
    prefer the camera meter / a RAW histogram."""
    rgb = np.asarray(open_rgb(image_path), dtype="float32") / 255.0
    if region is not None:
        rgb = _crop_region(rgb, region)                          # SALIENCY: meter the subject region
    lum = (_srgb_to_linear(rgb) * _REC709).sum(axis=-1)           # linear-light luminance
    clipped = float((rgb.max(axis=-1) >= 0.988).mean())          # any channel ~at the 255 ceiling
    key = math.exp(float(np.mean(np.log(lum + 1e-4))))           # log-average (geometric) midtone
    shot_ev100 = physics.ev100(f_number, shutter_s) - math.log2(iso / 100.0)
    scene_ev100 = shot_ev100 + math.log2(max(key, 1e-4) / _MIDDLE_GREY_LINEAR)
    scene_ev100 += min(1.5, clipped * 3.0)  # highlight protection: clipped frames read brighter
    if from_liveview:
        # correct the preview's low-light over-brightness so we expose for the captured still
        scene_ev100 -= liveview_still_offset_ev(scene_ev100)
    return scene_ev100, clipped


_MIN_EXIF_FOCAL_MM = 1.0       # shorter than any photographic lens; a smaller FocalLength is corrupt
_MAX_EXIF_FOCAL_MM = 10_000.0  # longer than any photographic lens; a larger FocalLength is corrupt


def focal_from_exif(image_path: str, default: float = 50.0) -> float:
    """Real focal length (mm) from a transferred still's EXIF. Handles the EXIF rational form
    (an (numerator, denominator) tuple or a PIL IFDRational); falls back to `default` when the
    frame has no EXIF (e.g. the WiFi liveview JPEG, which carries none) or no FocalLength tag."""
    # open_image returns a validated Image copy, which has no JPEG-only _getexif(); read the public
    # getexif() and its Exif sub-IFD (where camera files keep FocalLength).
    try:
        exif = open_image(image_path).getexif()
        tags = {**dict(exif), **dict(exif.get_ifd(0x8769))}
    except Exception:  # noqa: BLE001 - no/corrupt EXIF -> default
        return default
    for tag, val in tags.items():
        if TAGS.get(tag) == "FocalLength":
            try:
                if isinstance(val, tuple) and len(val) == 2:
                    focal = float(val[0]) / float(val[1])  # EXIF rational (num, den)
                else:
                    focal = float(val)                     # IFDRational / plain number
            except (TypeError, ValueError, ArithmeticError):
                return default
            # A zero, negative, NaN (0/0 rational) or absurd value is a corrupt tag, not a lens; the
            # brain rejects such a focal, so fall back exactly as for a missing tag.
            plausible = math.isfinite(focal) and _MIN_EXIF_FOCAL_MM <= focal <= _MAX_EXIF_FOCAL_MM
            return focal if plausible else default
    return default


if __name__ == "__main__":
    import brain  # heavy CLIP deps — only needed for the standalone recommend, not for metering
    path, iso, f_number, shutter_s = sys.argv[1], float(sys.argv[2]), float(sys.argv[3]), float(sys.argv[4])
    ev100, clipped = meter_ev100(path, iso, f_number, shutter_s)
    focal = focal_from_exif(path)
    warn = "  [!] heavy clipping — EV is a floor; meter off the camera/RAW" if clipped > 0.10 else ""
    print(f"metered: scene EV100 ~ {ev100:.2f}  (clipped {clipped:.0%}, focal {focal:.0f}mm){warn}")
    brain.recommend_settings(path, ev100, focal_mm=focal)
