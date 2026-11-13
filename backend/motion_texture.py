"""Computer-vision factors that bend the capture brain's shutter floor to the *scene*.

`physics.py` knows the deterministic constraints (correct exposure, the vibration floor,
the diffraction cap). What it can't know from numbers alone is how fast the world in front
of the lens is actually moving — that takes pixels. This module supplies the three CV
"factors" the brain feeds into its shutter-floor logic:

  1. OPTICAL FLOW  — dense Farneback flow between two live-view frames, turned into the
                     slowest shutter that still freezes the fastest motion anywhere.
  2. SUBJECT MOTION— the same speed measured *inside the subject box only*, so motion the
                     photographer may want blurred (a flowing river, a passing crowd) does
                     not drag the shutter faster than the subject needs. This is the value
                     handed to `brain.recommend_settings(subject_motion_floor_s=...)`, where
                     it competes with the vibration floor via `min(...)` — the faster demand binds.
  3. TEXTURE       — a 0..1 detail/sharpness score (variance-of-Laplacian), high for crisp
                     detail and low for flat or already-blurred regions.

Everything here is a pure function on numpy ndarrays — give it BGR or grayscale frames and
it converts internally — so it is unit-testable on synthetic frames with no camera attached
(run `backend/.venv-ml/bin/python backend/test_motion_texture.py`).

Why the shutter conversion targets the *slowest* safe shutter, not the fastest:
a faster shutter than "freeze the fastest mover under max_blur_px" buys no sharpness the
subject needs — it only steals light, forcing the exposure solver to a higher ISO (more
noise) or a wider aperture (less depth of field). So the factor reports the slowest shutter
that still holds motion blur under the budget; the brain is then free to go faster only if
the exposure math wants to, never slower than this without flagging blur.
"""
from __future__ import annotations

import cv2
import numpy as np

# Farneback dense-flow parameters. These are the well-worn OpenCV defaults: a 0.5 image
# pyramid over 3 levels, a 15 px window (smooths noise without erasing real motion), 3
# iterations per level, and a degree-5 / sigma-1.2 polynomial expansion.
_FARNEBACK = dict(
    pyr_scale=0.5,
    levels=3,
    winsize=15,
    iterations=3,
    poly_n=5,
    poly_sigma=1.2,
    flags=0,
)

# We read the fastest motion at a high percentile rather than the raw max so a handful of
# noisy flow pixels (specular glints, occlusion edges) can't dictate the whole shutter.
_SPEED_PERCENTILE = 99.0

# "No motion" sentinel: when the percentile speed is ~zero we report this shutter rather than
# a literal +inf. It is far slower than any handheld vibration floor (1/focal is well under a
# second), so it reads as "effectively no motion floor" yet stays a finite number the brain
# can safely combine — `max(vib_floor, this)` or `min(...)` both leave the vibration floor in
# charge instead of being poisoned by inf. Real motion always returns far less than this.
_NO_MOTION_SHUTTER_S = 1.0

# Texture normalization. variance-of-Laplacian on an 8-bit image is unbounded (flat ~0,
# busy detail in the hundreds–thousands), so we squash it with var/(var+k): a saturating
# curve that maps 0 -> 0 and reaches ~1 for very busy detail. k is the half-saturation
# point — at var == k the score is 0.5. Tuned so a hard checkerboard reads near 1 while a
# uniform field reads ~0.
_TEXTURE_HALF_SATURATION = 300.0

# --- Verified-motion evidence thresholds ------------------------------------------------- #
# motion_fraction counts pixels that move *relative to the camera*: subtract the robust median
# flow (the dominant global pan/translation of the whole frame) from every pixel's flow, then
# count the pixels whose residual displacement between the two frames is at least this many
# pixels, measured at the working (preview) width the caller hands us. 1.5 px sits well above
# Farneback's sub-pixel noise (a static textured pair reads ~0) yet low enough to catch a
# subject genuinely moving against its background. This is a frame-to-frame displacement, NOT a
# per-second velocity — dt_s only decides whether the sample interval is usable at all.
_MOTION_RESIDUAL_PX = 1.5

# flow_confidence is the fraction of pixels where the forward (A->B) and backward (B->A) flow
# cancel to within this tolerance. Real motion and real stasis both cancel (fwd == -bwd, or both
# ~0); two frames with no true correspondence (independent noise, a blank wall) do not. A pair
# must clear _FLOW_CONFIDENCE_FLOOR before we are willing to certify its motion as measured.
_FB_CONSISTENCY_PX = 0.75
_FLOW_CONFIDENCE_FLOOR = 0.60

# A pair with almost no detail cannot yield trustworthy flow, so require the measured frame's
# texture score to clear this floor before certifying motion. (Independent pure noise is caught
# by flow_confidence instead — noise has plenty of texture but no coherent correspondence.)
_TEXTURE_VERIFY_FLOOR = 0.05


def _to_gray(frame: np.ndarray) -> np.ndarray:
    """Return an 8-bit single-channel view of a frame given as BGR or grayscale."""
    if frame.ndim == 2:
        gray = frame
    elif frame.ndim == 3 and frame.shape[2] == 3:
        gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
    else:
        raise ValueError(f"expected BGR (H,W,3) or grayscale (H,W) frame, got shape {frame.shape}")
    if gray.dtype != np.uint8:
        # Float frames in the conventional 0..1 range are scaled up; anything already in 0..255
        # (or beyond) is clipped. Without the scale, a 0..1 image would clip to near-black.
        gray = np.asarray(gray, dtype=np.float64)
        if gray.max() <= 1.0:
            gray = gray * 255.0
        gray = np.clip(gray, 0, 255).astype(np.uint8)
    return gray


def _crop(array: np.ndarray, region: tuple[int, int, int, int] | None) -> np.ndarray:
    """Crop an (H,W,...) array to a region=(x, y, w, h), clamped to the array bounds.

    Returns the whole array when region is None. Raises if the clamped box is empty.
    """
    if region is None:
        return array
    x, y, w, h = (int(v) for v in region)
    height, width = array.shape[:2]
    x0 = max(0, min(x, width))
    y0 = max(0, min(y, height))
    x1 = max(x0, min(x + w, width))
    y1 = max(y0, min(y + h, height))
    if x1 <= x0 or y1 <= y0:
        raise ValueError(f"region {region} is empty within a {width}x{height} frame")
    return array[y0:y1, x0:x1]


def optical_flow_field(frame_a: np.ndarray, frame_b: np.ndarray) -> np.ndarray:
    """Dense optical flow from frame_a to frame_b via Farneback.

    Accepts BGR or grayscale frames (converted internally). Returns the H x W x 2 flow
    field where flow[y, x] = (dx, dy) is the per-pixel displacement *in pixels* that maps a
    point in frame_a to its position in frame_b. The two frames must share the same shape.
    """
    gray_a = _to_gray(frame_a)
    gray_b = _to_gray(frame_b)
    if gray_a.shape != gray_b.shape:
        raise ValueError(f"frames differ in shape: {gray_a.shape} vs {gray_b.shape}")
    return cv2.calcOpticalFlowFarneback(gray_a, gray_b, None, **_FARNEBACK)


def _percentile_speed_px(flow: np.ndarray, region: tuple[int, int, int, int] | None,
                         percentile: float) -> float:
    """High-percentile per-pixel displacement magnitude (pixels) over an optional region."""
    field = _crop(flow, region)
    magnitude = np.linalg.norm(field, axis=2)  # sqrt(dx^2 + dy^2) per pixel
    return float(np.percentile(magnitude, percentile))


def freeze_shutter_s(flow: np.ndarray, dt_s: float, max_blur_px: float = 1.0,
                     region: tuple[int, int, int, int] | None = None) -> float:
    """OPTICAL-FLOW factor: the slowest shutter (seconds) that still freezes the fastest motion.

    From the flow field take a high percentile (~99th) of the per-pixel displacement — the
    speed of the fastest-moving things between the two frames, robust to a few outlier
    pixels. Convert that frame-to-frame displacement to a velocity in pixels/second by
    dividing by `dt_s` (the time elapsed between frame_a and frame_b). The shutter that
    keeps a moving point's smear under `max_blur_px` during the exposure is then

        shutter_s = max_blur_px / pixels_per_second.

    This is the *slowest* shutter that still freezes (see the module docstring): a faster
    one only costs light. `region`=(x, y, w, h) restricts the speed measurement to a box.
    Returns `inf` when nothing is moving — a static scene imposes no motion floor at all.
    """
    if dt_s <= 0:
        raise ValueError(f"dt_s must be positive, got {dt_s}")
    displacement_px = _percentile_speed_px(flow, region, _SPEED_PERCENTILE)
    pixels_per_second = displacement_px / dt_s
    if pixels_per_second <= 0:
        return _NO_MOTION_SHUTTER_S  # nothing moving -> effectively no motion floor (see const)
    return min(max_blur_px / pixels_per_second, _NO_MOTION_SHUTTER_S)


def subject_motion_floor_s(flow: np.ndarray, dt_s: float,
                           subject_box: tuple[int, int, int, int] | None = None,
                           max_blur_px: float = 1.0) -> float:
    """SUBJECT-MOTION factor: freeze_shutter_s measured *inside the subject box only*.

    Identical math to `freeze_shutter_s`, but the high-percentile speed is taken only within
    `subject_box`=(x, y, w, h). Background motion the photographer may *want* blurred (a
    river, a passing crowd) is therefore ignored — only the subject's own motion sets the
    floor. With `subject_box=None` it measures the whole frame.

    The returned value is what gets passed to
    `brain.recommend_settings(subject_motion_floor_s=...)`, where the brain takes
    `min(vibration_floor, this)` as the binding shutter floor (the faster demand wins). `inf` when the subject is
    still, so the brain is left free to choose any (slower) shutter the exposure wants.
    """
    return freeze_shutter_s(flow, dt_s, max_blur_px=max_blur_px, region=subject_box)


def texture_score(frame: np.ndarray, region: tuple[int, int, int, int] | None = None) -> float:
    """TEXTURE factor: a 0..1 detail/sharpness score (variance-of-Laplacian).

    The Laplacian is the second spatial derivative — large where intensity changes sharply
    (edges, fine detail), ~zero across flat areas. Its variance over the (optionally cropped)
    frame is the classic focus/detail measure: high for crisp detail, low for flat or
    already-blurred regions. We squash the unbounded variance into 0..1 with a saturating
    curve so a hard checkerboard reads near 1 and a uniform field reads ~0; a softened
    (blurred) checkerboard lands in between.
    """
    gray = _to_gray(_crop(frame, region))
    laplacian = cv2.Laplacian(gray, cv2.CV_64F)
    variance = float(laplacian.var())
    return variance / (variance + _TEXTURE_HALF_SATURATION)


def moving_pixel_fraction(flow: np.ndarray) -> float:
    """Fraction of pixels that move *relative to the camera* between the two frames.

    Subtract the robust per-axis median flow (the dominant global camera translation/pan) from
    every pixel, then count the pixels whose residual displacement is at least
    `_MOTION_RESIDUAL_PX`. A uniform whole-frame pan therefore reads ~0, while a subject moving
    against a static (or globally panning) background reads ~its image-area fraction. Measured
    over the WHOLE frame — this is what `plan_stack` reads to judge duplicate-subject/ghosting
    risk across a stack, which any moving region can cause. Returns a float in [0, 1].
    """
    median = np.median(flow.reshape(-1, 2), axis=0)  # robust global (camera) translation
    residual = np.linalg.norm(flow - median, axis=2)
    return float(np.mean(residual >= _MOTION_RESIDUAL_PX))


def flow_confidence(frame_a: np.ndarray, frame_b: np.ndarray,
                    flow: np.ndarray | None = None) -> float:
    """Forward/backward consistency: fraction of pixels where the A->B and B->A flow cancel.

    Genuine motion and genuine stasis both cancel (fwd == -bwd, or both ~0); two frames with no
    trackable correspondence (independent noise, a blank wall) do not. A low value means the flow
    field itself is untrustworthy, so we refuse to certify motion from it. Same-grid comparison
    (no warp) is exact for uniform translation and cheap. Returns a float in [0, 1].
    """
    if flow is None:
        flow = optical_flow_field(frame_a, frame_b)
    backward = optical_flow_field(frame_b, frame_a)
    residual = np.linalg.norm(flow + backward, axis=2)
    return float(np.mean(residual <= _FB_CONSISTENCY_PX))


def _unmeasured_pair(frame_b: np.ndarray, dt_s: float,
                     subject_box: tuple[int, int, int, int] | None,
                     output_width_px: int | None, *, reason: str) -> dict:
    """Full-shaped, honestly-unverified result for a pair whose flow cannot be computed."""
    width = frame_b.shape[1]
    scale = max(1.0, float(output_width_px or width) / width)
    return {
        "status": "estimated", "evidence": "two_frame_optical_flow",
        "outcome_verified": False, "dt_s": float(dt_s), "reason": reason,
        "motion_fraction": None, "flow_confidence": None,
        "subject_motion_floor_s": None, "global_motion_floor_s": None,
        "texture_score": texture_score(frame_b),
        "subject_region": list(subject_box) if subject_box is not None else None,
        "output_scale": scale,
        "limitations": ["Flow includes camera movement and depends on visible texture.",
                        "Two frames estimate motion; final capture sharpness is unverified."],
    }


def analyze_frame_pair(frame_a: np.ndarray, frame_b: np.ndarray, dt_s: float, *,
                       subject_box: tuple[int, int, int, int] | None = None,
                       output_width_px: int | None = None,
                       dt_measured: bool = True) -> dict:
    """One shared, pure capture-engine entry point for the motion/texture factors and evidence.

    dt_s is the measured interval between frame acquisition timestamps (not an assumed requested
    sleep); `dt_measured=False` records that the caller only guessed it. If full-resolution output
    width is supplied, scale the one-pixel blur budget into preview pixels. A subject box is
    (x,y,w,h) in preview pixels.

    Emits `status="measured"` / `outcome_verified=True` — the shape the capture-policy stack gate
    trusts — ONLY when every honesty condition holds: dt_s is valid AND measured (not guessed),
    both frames share a size, the measured frame clears `_TEXTURE_VERIFY_FLOOR`, and forward/
    backward `flow_confidence` clears `_FLOW_CONFIDENCE_FLOOR`. Otherwise `status="estimated"`,
    `outcome_verified=False`, with a `reason`. `motion_fraction` (see `moving_pixel_fraction`) is
    the whole-frame fraction moving relative to the camera. This estimates a floor and certifies
    the *measurement*; it does not certify sharpness of the subsequent exposure.
    """
    if not np.isfinite(dt_s) or not 0 < dt_s <= 2.0:
        raise ValueError("Motion frames require a finite interval in (0, 2] seconds")
    if output_width_px is not None and output_width_px <= 0:
        raise ValueError("output_width_px must be positive")
    if frame_a.shape[:2] != frame_b.shape[:2]:
        return _unmeasured_pair(frame_b, dt_s, subject_box, output_width_px,
                                reason="Motion frames differ in size; optical flow is undefined.")
    width = frame_a.shape[1]
    scale = max(1.0, float(output_width_px or width) / width)
    budget = 1.0 / scale
    flow = optical_flow_field(frame_a, frame_b)
    fraction = moving_pixel_fraction(flow)
    confidence = flow_confidence(frame_a, frame_b, flow)
    texture = texture_score(frame_b, subject_box)
    reasons = []
    if not dt_measured:
        reasons.append("sampling interval was assumed by the caller, not measured")
    if texture < _TEXTURE_VERIFY_FLOOR:
        reasons.append("scene has too little texture to track optical flow")
    if confidence < _FLOW_CONFIDENCE_FLOOR:
        reasons.append("forward/backward optical flow is inconsistent")
    verified = not reasons
    result = {
        "status": "measured" if verified else "estimated",
        "evidence": "two_frame_optical_flow",
        "outcome_verified": verified, "dt_s": float(dt_s),
        "motion_fraction": fraction, "flow_confidence": confidence,
        "subject_motion_floor_s": subject_motion_floor_s(
            flow, dt_s, subject_box, max_blur_px=budget),
        "global_motion_floor_s": freeze_shutter_s(flow, dt_s, max_blur_px=budget),
        "texture_score": texture,
        "subject_region": list(subject_box) if subject_box is not None else None,
        "output_scale": scale,
        "limitations": ["Flow includes camera movement and depends on visible texture.",
                        "Two frames estimate motion; final capture sharpness is unverified."],
    }
    if not verified:
        result["reason"] = "; ".join(reasons)
    return result
