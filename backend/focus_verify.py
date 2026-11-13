"""M1 focus verification — *is the subject actually sharp?* (the buildable half of the focus story).

Earlier USB work treated the Sony A7 II as focus-verification-only, because libgphoto2 does not expose
`AFAreaPosition 0xD2DC`, `FocusArea 0xD22C`, or `ManualFocusAdjust 0xD2D1` in this body's normal config
tree. That is no longer the whole story: raw Sony PTP probes in `artifacts/falsification/r2_usb_ptp/`
show the camera accepts those vendor control writes and USB autofocus can report `Focus Locked`.
What is still not product-proven is point-specific behavior parity with Smart Remote: the accepted raw
coordinates have not yet shown a clean A/B refocus onto two different scene regions.

What we CAN do on ANY body is VERIFY focus: crop the subject region the vision model found and score
its sharpness (variance of the Laplacian — the classic focus measure). That lets the autopilot tell
whether the subject came out sharp, pick the best of several frames, or decide to re-trigger AF. On a
body/path where AF-area behavior is proven settable (Smart Remote today; maybe raw USB/PTP or Sony
Camera Remote Command after the remaining proof) this exact score closes the loop into a real
focus-on-subject driver.
"""
from __future__ import annotations

import cv2
import numpy as np
from image_safety import open_rgb

# Variance of the Laplacian is resolution-dependent, so always score at one fixed size to keep
# scores comparable across frames (the autoexpose downscale discipline, applied to focus).
_SCORE_LONG_EDGE = 1024


def sharpness(image_path: str, region: tuple[int, int, int, int] | None = None) -> float:
    """Sharpness of `region` (pixel x, y, w, h — the vision subject box) or the whole frame, scored
    at a fixed resolution so values compare across frames. Higher = sharper (better focus)."""
    img = open_rgb(image_path).convert("L")
    if region:
        x, y, w, h = region
        img = img.crop((x, y, x + w, y + h))
    if img.width < 8 or img.height < 8:
        return 0.0
    long_edge = max(img.width, img.height)
    if long_edge > _SCORE_LONG_EDGE:
        scale = _SCORE_LONG_EDGE / long_edge
        img = img.resize((max(1, round(img.width * scale)), max(1, round(img.height * scale))))
    gray = np.asarray(img, dtype="float64")
    return float(cv2.Laplacian(gray, cv2.CV_64F).var())


def sharpest(scores: list[float]) -> int:
    """Index of the sharpest frame (best-of-N selection); -1 for an empty list."""
    return max(range(len(scores)), key=lambda i: scores[i]) if scores else -1


def in_focus(score: float, best_seen: float, drop_frac: float = 0.6) -> bool:
    """Relative acceptance gate: a frame is 'in focus' if its score is within `drop_frac` of the best
    score seen for this subject. Relative (not absolute) because the metric scales with subject
    texture and light — calibrate against the running max, not a fixed threshold."""
    return best_seen > 0 and score >= drop_frac * best_seen


# --- B07: sharpness evidence for the "Review sharpness" line in the Why card ------------------------
#
# What this asserts: *does the frame contain a region as crisp as a focused frame of this scene would?*
# NOT "is the whole frame as sharp as its own sharpest tile". That earlier question (subject/frame
# ratio of Laplacian means) turned out to measure how much smooth area a scene contains, not focus.
# Calibrated 2026-09-20 against real captures on both bodies, that ratio labelled 7 of 8 good photos
# "soft" (33-52% of best-tile for genuinely sharp interiors, versus 33% for a truly defocused frame)
# because walls, ceilings and sofas drag the frame mean far below its sharpest tile even at perfect
# focus — the ratio barely separated the classes.
#
# The honest, separating signal is the ABSOLUTE detail of the frame's most-detailed region: the
# 90th-percentile tile of a 12x8 grid of local Laplacian variance, scored at a fixed working width so
# frames compare. A focused frame has at least one genuinely crisp region; a defocused frame has none
# anywhere. On the calibration set that best-tile value was 66 for the defocused frame and 160-1309 for
# every sharp one — a clean split the frame-mean ratio (22 vs 314-593 in raw variance) also shows. So:
#   * best-tile below the texture floor   -> too flat to tell focus from a low-detail scene (undetermined);
#   * best-tile at/above the detail floor  -> a crisply focused region is present (sharp);
#   * in between                           -> texture exists but nothing is crisp (soft).
# When the caller passes a subject box we anchor on the SUBJECT's own best tile instead, using the whole
# frame's best tile as context: a scene with a crisp region proves focus was achievable in this light,
# so a subject that never gets crisp there is soft. The detail floor is a coarse heuristic at the
# working width, NOT a per-body optical calibration, so the tag stays "uncalibrated_relative" and the
# threshold leans high — the false-negative direction (calling a blurry frame sharp) is the one to avoid.

_TILE_COLS = 12
_TILE_ROWS = 8
# The frame's "most-detailed region": the 90th-percentile tile, robust to a single glint tile while
# still requiring that a meaningful patch (>10% of the frame) reached that detail.
_REFERENCE_PERCENTILE = 90
# Absolute detail floor: variance of the Laplacian (8-bit grey) that the sharpest tile must reach for a
# crisply focused region to count as present. Calibrated 2026-09-20 at the 1600px working width — the
# one defocused real frame peaked at 66, every sharp real frame at >= 160; 120 splits them with margin
# on both sides and leans high so a soft frame is never mislabelled sharp. Tied to the working width.
_SHARP_DETAIL = 120.0
# Below this the frame's own sharpest tile carries too little contrast to serve as a reference at all,
# so focus cannot be told apart from a genuinely flat (but possibly in-focus) scene: the verdict is
# "undetermined", never "soft".
_TEXTURE_FLOOR = 20.0
_MIN_TILE_PX = 8


def _laplacian_var(gray: "np.ndarray") -> float:
    return float(cv2.Laplacian(gray, cv2.CV_64F).var())


def _tenengrad(gray: "np.ndarray") -> float:
    """Sobel gradient energy — mean of (Gx^2 + Gy^2). A second, edge-based focus measure."""
    gx = cv2.Sobel(gray, cv2.CV_64F, 1, 0, ksize=3)
    gy = cv2.Sobel(gray, cv2.CV_64F, 0, 1, ksize=3)
    return float(np.mean(gx * gx + gy * gy))


def _reference_detail(gray: "np.ndarray") -> float:
    """90th-percentile local Laplacian variance over a 12x8 tile grid: the frame's own most-detailed
    region, used as the honest 'as sharp as this scene gets' reference."""
    height, width = gray.shape
    if height < _TILE_ROWS or width < _TILE_COLS:
        return _laplacian_var(gray)
    lap = cv2.Laplacian(gray, cv2.CV_64F)
    tile_vars: list[float] = []
    for row in range(_TILE_ROWS):
        y0 = row * height // _TILE_ROWS
        y1 = (row + 1) * height // _TILE_ROWS
        for col in range(_TILE_COLS):
            x0 = col * width // _TILE_COLS
            x1 = (col + 1) * width // _TILE_COLS
            tile = lap[y0:y1, x0:x1]
            if tile.size:
                tile_vars.append(float(tile.var()))
    return float(np.percentile(tile_vars, _REFERENCE_PERCENTILE)) if tile_vars else _laplacian_var(gray)


def _working_gray(image_path: str, working_width: int) -> tuple["np.ndarray", float, int]:
    """Guarded decode -> grayscale float64 downscaled to `working_width`. Returns (gray, scale,
    working_width_used). Never Image.open — always the bounded `open_rgb`."""
    rgb = open_rgb(image_path).convert("L")
    original_width = rgb.width
    scale = working_width / original_width if original_width > working_width else 1.0
    if scale < 1.0:
        rgb = rgb.resize((max(1, round(rgb.width * scale)), max(1, round(rgb.height * scale))))
    return np.asarray(rgb, dtype="float64"), scale, rgb.width


def sharpness_evidence(
    image_path: str,
    *,
    subject_box: tuple[int, int, int, int] | None = None,
    working_width: int = 1600,
) -> dict:
    """Deterministic sharpness measurement for one accepted frame.

    `subject_box` is (x, y, w, h) in the ORIGINAL image's pixel coordinates (the engine's metering
    box); it is scaled to the working image before cropping. Returns whole-frame and subject
    variance-of-Laplacian and Tenengrad scores plus a verdict driven by the ABSOLUTE detail of the
    most-detailed region (subject's when a box is given, else the frame's), gated by a texture floor.
    The detail floor is a coarse heuristic at the working width, not a per-body optical calibration.
    """
    gray, scale, working_used = _working_gray(image_path, working_width)
    frame_lap = _laplacian_var(gray)
    frame_ten = _tenengrad(gray)
    reference = _reference_detail(gray)

    subject_lap: float | None = None
    subject_ten: float | None = None
    subject_reference: float | None = None
    subject_box_used: list[int] | None = None
    if subject_box is not None:
        x, y, w, h = subject_box
        height, width = gray.shape
        sx0 = max(0, min(width, int(round(x * scale))))
        sy0 = max(0, min(height, int(round(y * scale))))
        sx1 = max(0, min(width, int(round((x + w) * scale))))
        sy1 = max(0, min(height, int(round((y + h) * scale))))
        if sx1 - sx0 >= _MIN_TILE_PX and sy1 - sy0 >= _MIN_TILE_PX:
            crop = gray[sy0:sy1, sx0:sx1]
            subject_lap = _laplacian_var(crop)
            subject_ten = _tenengrad(crop)
            subject_reference = _reference_detail(crop)
            subject_box_used = [sx0, sy0, sx1 - sx0, sy1 - sy0]

    # Anchor on the most-detailed region of whatever we are judging: the subject's own best tile when a
    # box was measurable, else the whole frame's. `reference` (the frame's best tile) is always the
    # context that says whether the scene produced any crisp detail at all.
    anchor = subject_reference if subject_reference is not None else reference
    where = "subject" if subject_reference is not None else "frame"
    if reference < _TEXTURE_FLOOR:
        verdict = "undetermined"
        reason = (
            f"the scene's most-detailed region (best-tile Laplacian var {reference:.1f}) is below the "
            f"{_TEXTURE_FLOOR:.0f} texture floor — too flat to tell focus from a low-detail scene"
        )
    elif anchor >= _SHARP_DETAIL:
        verdict = "sharp"
        reason = (
            f"the {where}'s sharpest region reaches Laplacian var {anchor:.0f} "
            f"(>= {_SHARP_DETAIL:.0f} detail floor at {working_used}px): a crisply focused region is present"
        )
    else:
        verdict = "soft"
        context = "" if where == "frame" else f" while the scene's best region reaches {reference:.0f}"
        reason = (
            f"the {where}'s sharpest region only reaches Laplacian var {anchor:.0f}{context} "
            f"(< {_SHARP_DETAIL:.0f} detail floor at {working_used}px): no region is as crisp as a "
            f"focused frame of this scene would be"
        )

    return {
        "laplacian_var": frame_lap,
        "tenengrad": frame_ten,
        "subject_laplacian_var": subject_lap,
        "subject_tenengrad": subject_ten,
        "best_tile_detail": reference,
        "subject_best_tile_detail": subject_reference,
        "anchor_detail": anchor,
        "sharp_detail_floor": _SHARP_DETAIL,
        "texture_floor": _TEXTURE_FLOOR,
        "working_width": working_used,
        "subject_box_used": subject_box_used,
        "verdict": verdict,
        "reason": reason,
        "calibration": "uncalibrated_relative",
    }


def _evidence_score(evidence: dict) -> float:
    """The score that represents a frame: the subject region when measured, else the whole frame."""
    subject = evidence.get("subject_laplacian_var")
    return float(subject) if subject is not None else float(evidence.get("laplacian_var", 0.0))


def compare_sharpness(a: dict, b: dict) -> dict:
    """Compare two `sharpness_evidence` results (e.g. meter frame vs final). Deterministic; returns
    the ratio a/b and which frame is sharper. Uses the subject score when both carry one, else the
    whole-frame score."""
    a_score = _evidence_score(a)
    b_score = _evidence_score(b)
    ratio = a_score / b_score if b_score > 0 else float("inf") if a_score > 0 else 1.0
    if a_score > b_score:
        sharper = "a"
    elif b_score > a_score:
        sharper = "b"
    else:
        sharper = "equal"
    return {"a_score": a_score, "b_score": b_score, "ratio": ratio, "sharper": sharper}
