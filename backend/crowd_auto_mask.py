"""Automatic per-pixel background estimation that feeds explicit-mask recovery.

`crowd_recovery.recover_background` consumes caller-asserted binary masks: white
means "this pixel is verified static background". This module replaces the human
assertion with a temporal estimator and writes the same kind of binary masks, so
the recovery code (and all of its provenance/hash guarantees) runs unchanged.

The estimator is honest about what it cannot decide. It computes a per-pixel
temporal median of the registered frames and labels a frame's pixel as background
when its exposure-matched residual to that median is small (reusing the luma and
LAB-chroma thresholds and semantics of `stacking._moving_mask`). But a median is
only a trustworthy background where the background is actually a majority: when a
crowd occupies most of the frames, the median is the crowd, and every crowd frame
"agrees" with it. To catch that, a pixel's background labels are discarded unless
the frames that agree with the median also agree with EACH OTHER -- a static
background is temporally stable, whereas a contaminated median is the coincidental
middle of several different moving colors. Pixels that fail this test are reported
as never-observed rather than silently returned as recovered background.

No clip suppression is applied (unlike the HDR path): the reference here is the
same-exposure median, so a bright foreground object is genuine content, not a
bracket-clipping artifact. Processing is memory-bounded (disk-staged frames read
back in row bands) and deterministic; row-band size never changes the result.
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import math
import os
import shutil

import cv2
import numpy as np

import stacking

PROVENANCE = "automatic_temporal_outlier_vs_median"

# Residual thresholds: reuse the exact motion-detection semantics of
# stacking._moving_mask (exposure-matched luma residual + LAB a/b chroma distance).
DEFAULT_LUMA_THRESHOLD = stacking._DEGHOST_DIFF_THRESHOLD      # 0.12, normalized 0..1
DEFAULT_CHROMA_THRESHOLD = stacking._DEGHOST_CHROMA_THRESHOLD  # 25, LAB a/b units

# Temporal-stability limits for the frames that agree with the median. A static
# background is near-identical frame to frame (only sensor noise), so genuine
# background support clusters tightly; a crowd-contaminated median is the middle
# of several different colors and its "supporters" spread far apart. These bounds
# tolerate mild noise (a few code values) but flag foreground-level disagreement.
STABILITY_LUMA_SPREAD = 0.06     # peak-to-peak luma among supporters, normalized 0..1
STABILITY_CHROMA_SPREAD = 8.0    # peak-to-peak LAB a/b distance among supporters

# Arsenal Crowd Control's honest limit: "the background must be visible in at least 25% of the
# photos; if someone never moves, Crowd Control cannot remove them." A pixel is only clearable
# when background was judged visible in at least this fraction of the frames; below it the median
# there is the occupant, not the scene, so the region is reported as never sufficiently revealed.
MIN_BACKGROUND_VISIBLE_FRACTION = 0.25
# The estimator cannot tell a motionless person from the background: identical pixels in every
# frame ARE the temporal median. The not-cleared share never counts them, so every user-facing
# summary states this limit rather than letting "0% could not be cleared" imply they were removed.
STATIC_SUBJECT_LIMIT = ("Anyone who stayed still in every frame can't be told apart from the "
                        "background and will remain.")


def min_frames_for_visibility(n_frames: int) -> int:
    """Fewest frames that must show background for a pixel to be clearable (>= 25% of frames).

    Integer floor of the Arsenal rule: e.g. 1 of 4, 2 of 8, 3 of 10. Used both to fill the merge
    (recovery ``minimum_observations``) and to classify the revealed/not-cleared report, so the
    reported "could not be cleared" share matches exactly the region the output keeps as fallback.
    """
    if isinstance(n_frames, bool) or not isinstance(n_frames, int) or n_frames < 1:
        raise ValueError("n_frames must be a positive integer")
    return max(1, math.ceil(MIN_BACKGROUND_VISIBLE_FRACTION * n_frames))


@dataclass
class AutoMaskResult:
    mask_paths: list[Path]              # one binary grayscale PNG per source, in order
    support_count: np.ndarray           # uint16 H×W: frames labeling each pixel background
    fraction_pixels_with_majority_support: float
    fraction_never_observed: float
    provenance: str
    shape: tuple[int, int]              # (height, width)
    n_frames: int
    majority_threshold: int             # ceil(N/2)
    luma_threshold: float
    chroma_threshold: float


def _row_bands(height: int, band_rows: int):
    for y0 in range(0, height, band_rows):
        yield y0, min(y0 + band_rows, height)


def _global_luma_luts(files, shape, frame_histograms, median_u8):
    """Per-frame grayscale LUT mapping each frame's tones onto the median's.

    Mirrors stacking._exposure_match_gray (searchsorted of the source CDF into the
    reference CDF) but is computed once from whole-frame histograms so it is
    independent of how the frames are later split into row bands.
    """
    # Accumulated in row bands: a whole-frame np.bincount first converts every pixel to int64
    # (8 bytes per pixel). Integer counts are exact, so the histogram is identical.
    median_hist = np.zeros(256, dtype=np.int64)
    for y0, y1 in _row_bands(median_u8.shape[0], stacking.STACK_TILE_SIZE):
        median_hist += np.bincount(cv2.cvtColor(median_u8[y0:y1], cv2.COLOR_BGR2GRAY).ravel(), minlength=256)
    median_hist = median_hist.astype(np.float64)
    median_cdf = np.cumsum(median_hist) / max(median_u8.shape[0] * median_u8.shape[1], 1)
    luts = []
    for hist in frame_histograms:
        src_cdf = np.cumsum(hist) / max(float(np.sum(hist)), 1.0)
        luts.append(np.searchsorted(median_cdf, src_cdf).clip(0, 255).astype(np.uint8))
    return luts


def estimate_background_masks(paths, out_dir, *, registered=True,
                              luma_threshold: float = DEFAULT_LUMA_THRESHOLD,
                              chroma_threshold: float = DEFAULT_CHROMA_THRESHOLD,
                              min_support=None, tile_rows: int = stacking.STACK_BAND_ROWS) -> AutoMaskResult:
    """Estimate one binary background mask per source and write them to out_dir.

    Sources must already share a registered pixel grid (registration is never
    re-estimated here; that would invalidate the pixel correspondence the masks
    depend on). Returns mask paths plus per-pixel support and honesty statistics.
    """
    if registered is not True:
        raise ValueError("explicit registered=True is required: sources must already share a registered pixel grid")
    if not isinstance(paths, (list, tuple)) or len(paths) == 0:
        raise ValueError("provide a list of registered source frames")
    if tile_rows < 1:
        raise ValueError("tile_rows must be a positive number of rows")

    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)

    with stacking._staged_stack(list(paths), align=False, mode="crowd_auto",
                                band_rows=tile_rows) as (files, shape, histograms):
        height, width, _ = shape
        n_frames = len(files)
        majority = math.ceil(n_frames / 2)
        floor = None if min_support is None else int(min_support)
        if floor is not None and not 1 <= floor <= n_frames:
            raise ValueError("min_support must be between 1 and the number of source frames")

        # Pass 1: per-pixel temporal median reference, held once (one image, not N).
        median_u8 = np.empty(shape, np.uint8)
        for y0, y1 in _row_bands(height, tile_rows):
            bounds = (y0, y1, 0, width)
            band = np.stack([stacking._tile_from_file(path, shape, bounds) for path in files])
            median = np.median(band, axis=0)
            median_u8[y0:y1] = np.clip(median, 0, 255).astype(np.uint8)

        luts = _global_luma_luts(files, shape, histograms, median_u8)

        # Pass 2: label each frame vs the median, then discard labels whose
        # supporting frames disagree with each other (contaminated median).
        # The masks stream band by band into private raw files in the stack's staging
        # directory instead of living as N full-resolution arrays for the whole pass; each is
        # read back and PNG-encoded one at a time below. Bands are whole rows, so appending
        # them in order reproduces the row-major frame exactly.
        staging = files[0].parent
        if shutil.disk_usage(staging).free < height * width * n_frames + 64 * 1024 * 1024:
            raise ValueError("insufficient temporary storage for background masks")
        raw_masks = []
        for index in range(n_frames):
            raw = staging / f"{index:02d}.automask"
            os.close(os.open(raw, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600))
            raw_masks.append(raw)
        support_count = np.zeros((height, width), np.uint16)
        luma_limit = STABILITY_LUMA_SPREAD * 255.0
        for y0, y1 in _row_bands(height, tile_rows):
            bounds = (y0, y1, 0, width)
            med_band = median_u8[y0:y1]
            med_gray = cv2.cvtColor(med_band, cv2.COLOR_BGR2GRAY).astype(np.float32) / 255.0
            med_ab = cv2.cvtColor(med_band, cv2.COLOR_BGR2LAB)[:, :, 1:].astype(np.float32)

            labels = []
            gmin = np.full((y1 - y0, width), np.inf, np.float32)
            gmax = np.full((y1 - y0, width), -np.inf, np.float32)
            amin = np.full((y1 - y0, width), np.inf, np.float32)
            amax = np.full((y1 - y0, width), -np.inf, np.float32)
            bmin = np.full((y1 - y0, width), np.inf, np.float32)
            bmax = np.full((y1 - y0, width), -np.inf, np.float32)
            for index, path in enumerate(files):
                tile = stacking._tile_from_file(path, shape, bounds)
                gray = cv2.cvtColor(tile, cv2.COLOR_BGR2GRAY)
                matched = luts[index][gray].astype(np.float32) / 255.0
                luma_res = np.abs(matched - med_gray)
                frame_ab = cv2.cvtColor(tile, cv2.COLOR_BGR2LAB)[:, :, 1:].astype(np.float32)
                chroma_res = np.sqrt(((frame_ab - med_ab) ** 2).sum(axis=2))
                label = (luma_res < luma_threshold) & (chroma_res < chroma_threshold)
                labels.append(label)
                grayf = gray.astype(np.float32)
                gmin = np.where(label, np.minimum(gmin, grayf), gmin)
                gmax = np.where(label, np.maximum(gmax, grayf), gmax)
                af, bf = frame_ab[:, :, 0], frame_ab[:, :, 1]
                amin = np.where(label, np.minimum(amin, af), amin)
                amax = np.where(label, np.maximum(amax, af), amax)
                bmin = np.where(label, np.minimum(bmin, bf), bmin)
                bmax = np.where(label, np.maximum(bmax, bf), bmax)

            support = np.zeros((y1 - y0, width), np.uint16)
            for label in labels:
                support += label.astype(np.uint16)
            luma_spread = np.where(support >= 1, gmax - gmin, 0.0)
            chroma_spread = np.where(support >= 1,
                                     np.sqrt((amax - amin) ** 2 + (bmax - bmin) ** 2), 0.0)
            unstable = (support >= 1) & ((luma_spread > luma_limit) | (chroma_spread > STABILITY_CHROMA_SPREAD))
            discard = unstable
            if floor is not None:
                discard = discard | (support < floor)
            for index, label in enumerate(labels):
                kept = label & ~discard
                descriptor = os.open(raw_masks[index], os.O_WRONLY | os.O_APPEND | os.O_NOFOLLOW)
                with os.fdopen(descriptor, "ab") as handle:
                    (kept.astype(np.uint8) * 255).tofile(handle)
                support_count[y0:y1] += kept.astype(np.uint16)

        mask_paths = []
        for index in range(n_frames):
            path = out / f"auto_mask_{index:02d}.png"
            with os.fdopen(os.open(raw_masks[index], os.O_RDONLY | os.O_NOFOLLOW), "rb") as handle:
                mask = np.fromfile(handle, dtype=np.uint8)
            if mask.size != height * width:
                raise ValueError("background mask staging is incomplete")
            if not cv2.imwrite(str(path), mask.reshape(height, width)):
                raise OSError(f"failed to write background mask {path}")
            mask_paths.append(path)

    total = height * width
    return AutoMaskResult(
        mask_paths=mask_paths,
        support_count=support_count,
        fraction_pixels_with_majority_support=float(np.count_nonzero(support_count >= majority) / total),
        fraction_never_observed=float(np.count_nonzero(support_count == 0) / total),
        provenance=PROVENANCE,
        shape=(height, width),
        n_frames=n_frames,
        majority_threshold=majority,
        luma_threshold=float(luma_threshold),
        chroma_threshold=float(chroma_threshold),
    )


def recover_background_auto(paths, out_dir, *, minimum_observations=None,
                            luma_threshold: float = DEFAULT_LUMA_THRESHOLD,
                            chroma_threshold: float = DEFAULT_CHROMA_THRESHOLD,
                            tile_rows: int = stacking.STACK_BAND_ROWS) -> dict:
    """Estimate masks automatically, then run the explicit-mask recovery on them.

    Returns a dict wrapping a `GuidedCrowdResult` (never mutating crowd_recovery's
    own evidence strings) plus an honesty verdict. Pixels whose support is below a
    majority (ceil(N/2)) are counted in `low_confidence_fraction`; when that
    exceeds 0.5 the recovered maps are still returned but the status is
    "insufficient_background_visibility" -- the background was not visible often
    enough to trust the per-pixel median as a static background.
    """
    from crowd_recovery import recover_background, GuidedCrowdResult

    out = Path(out_dir)
    masks_dir = out / "auto_masks"
    estimate = estimate_background_masks(
        paths, masks_dir, registered=True,
        luma_threshold=luma_threshold, chroma_threshold=chroma_threshold, tile_rows=tile_rows)

    n_frames = estimate.n_frames
    majority = estimate.majority_threshold
    if minimum_observations is None:
        minimum_observations = majority
    result = recover_background([str(p) for p in paths], [str(p) for p in estimate.mask_paths],
                                registered=True, minimum_observations=minimum_observations)

    # Compared as returned (uint8 counts <= 16); an int64 copy cost 8 bytes per pixel.
    support = result.support_count
    low_confidence_fraction = float(np.count_nonzero(support < majority) / support.size)
    status = "insufficient_background_visibility" if low_confidence_fraction > 0.5 else "ok"

    # Arsenal honesty: a pixel is clearable only where background was judged visible in at least
    # MIN_BACKGROUND_VISIBLE_FRACTION of the frames. Below it the person could not be removed.
    visibility_floor = min_frames_for_visibility(n_frames)
    revealed = support >= visibility_floor
    fraction_revealed = float(np.count_nonzero(revealed) / support.size)
    fraction_not_cleared = float(np.count_nonzero(~revealed) / support.size)

    estimator_stats = {
        "provenance": PROVENANCE,
        "n_frames": n_frames,
        "majority_threshold": majority,
        "min_background_visible_fraction": MIN_BACKGROUND_VISIBLE_FRACTION,
        "visibility_floor_frames": visibility_floor,
        "fraction_revealed": fraction_revealed,
        "fraction_not_cleared": fraction_not_cleared,
        "luma_threshold": estimate.luma_threshold,
        "chroma_threshold": estimate.chroma_threshold,
        "fraction_pixels_with_majority_support": estimate.fraction_pixels_with_majority_support,
        "fraction_never_observed": estimate.fraction_never_observed,
        "mask_paths": [str(p) for p in estimate.mask_paths],
    }
    # Wrap, do not mutate: keep crowd_recovery's caller-asserted string intact and
    # override the label for this automatic path in a fresh evidence copy.
    evidence = dict(result.evidence)
    evidence["recovery_background_labels"] = result.evidence["background_labels"]
    evidence["background_labels"] = PROVENANCE
    evidence["estimator"] = estimator_stats
    evidence["status"] = status
    evidence["low_confidence_fraction"] = low_confidence_fraction
    evidence["low_confidence_semantics"] = "fraction of pixels with support < ceil(N/2); the median there is not a verified majority background"
    evidence["fraction_revealed"] = fraction_revealed
    evidence["fraction_not_cleared"] = fraction_not_cleared
    evidence["revealed_semantics"] = (
        f"fraction of pixels where background was judged visible in >= {MIN_BACKGROUND_VISIBLE_FRACTION:.0%} "
        "of frames; the rest could not be cleared (a subject that never moved is never revealed)")

    annotated = GuidedCrowdResult(result.image, result.support_count, result.confidence_map,
                                  result.valid_mask, result.never_observed_mask, evidence)
    return {
        "result": annotated,
        "status": status,
        "low_confidence_fraction": low_confidence_fraction,
        "fraction_revealed": fraction_revealed,
        "fraction_not_cleared": fraction_not_cleared,
        "minimum_observations": minimum_observations,
        "estimator": estimator_stats,
        "mask_paths": [str(p) for p in estimate.mask_paths],
        "evidence": evidence,
    }
