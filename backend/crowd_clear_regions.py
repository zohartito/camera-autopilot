"""Per-region "seen clear" signal for the crowd auto-stop (live preview only).

The global cleared share answers "how much of the plate is backed by enough frames", which
cannot tell a clean plate from one that still shows someone who sat down: a seated person is a
small share of the frame, and once relighting is normalised the rest of the room scores clear.
This module answers a different, spatial question on the downscaled preview frames: has every
region that was ever occupied by something moving been SEEN CLEAR again, recently?

The frame is cut into square tiles. For each accepted frame a tile is *occupied* when enough of
its pixels differ from the clean-plate estimate after a per-frame, per-channel gain. The gain is
measured only on textured tiles whose structure matches the plate (zero-mean normalised
cross-correlation), so a room that brightens or warms between frames keeps its static walls
clear, while a close passer-by who fills most of the frame cannot set the gain: their tiles do
not correlate with the plate and are left out. A frame with too few matching tiles has no gain
and every tile of it counts as occupied (it can never vouch for a region).

A tile that was occupied in any frame is *pending* until:

1. it agrees with the plate in each of the last ``RECENT_CLEAR_FRAMES`` frames, and
2. it has no alternative state: content that differs from the plate but is the same in at
   least the 25% visibility floor of the frames (never fewer than two; frames without a gain
   count as possibly matching), and no two distinct states that each recur in that floor of
   frames not shared with the other (a median plate halfway between them can agree with both). Such a state could itself be the background
   (someone sat down, left and came back, or sat early and stays), and pixels alone cannot say
   which state is the person, so the tile stays pending for the rest of the session whatever
   the order of frames.

Clipped channels are lower bounds: they never set the gain, and in a comparison they take the
value consistent with their bound that is closest to the other side.

The crowd auto-stop may fire only when no tile is pending. A tile that never differs from the
plate is not pending: a subject who never moved is indistinguishable from background
(``crowd_auto_mask.STATIC_SUBJECT_LIMIT``). The merge and the 25% visibility rule are unchanged;
this is a stop signal only.

Known limits. A departed state seen in fewer frames than the floor is, to the pixels, a passer-by
over background, so someone who sits down after the first frame, or after one or more different
people crossed that spot, and stays reads as a subject who never moved ([E,S,S,S,S] and
[P1,P2,S,S,S] look exactly like a crowd that left: [P1,P2,B,B,B]). The background they hide shows
in at most one of the frames so far, so within the 8-frame automatic budget it can never become
the per-pixel majority the merge would need to replace them. Flicker that never settles (a screen, leaves),
a texture that copies the background, and frames too dark or flat to measure a gain (no
textured tile correlates) keep their regions pending, so such scenes shoot the whole budget.
"""
from __future__ import annotations

import cv2
import numpy as np

import crowd_auto_mask

# Both images are smoothed before comparison so JPEG chroma subsampling around thin saturated
# edges and sub-pixel registration residue never read as an occupant (people span many pixels).
SMOOTH_KERNEL, SMOOTH_SIGMA = (9, 9), 2.0
# Square tile edge in preview pixels (previews are at most 640 px on the long edge).
TILE_EDGE = 16
# A region must agree with the plate in each of this many newest frames to count as seen clear.
RECENT_CLEAR_FRAMES = 2
# Per-pixel residual limits, the estimator's own "agrees with the median" semantics
# (crowd_auto_mask.DEFAULT_LUMA_THRESHOLD / DEFAULT_CHROMA_THRESHOLD).
LUMA_THRESHOLD = 0.12
CHROMA_THRESHOLD = 25.0
# A tile is occupied in a frame when more than this share of its pixels differ from the plate.
OCCUPIED_PIXEL_FRACTION = 0.04
# Two occupied frames show "the same content" at a tile when, among the tile's pixels that differ
# from the plate in either frame, at most this share also differ between the two frames. Loose on
# purpose: a seated person shifts a little, and "the same" only ever makes a tile more pending.
SAME_CONTENT_PIXEL_FRACTION = 0.25
# A channel at or above this level is a clipped highlight: its value is only a lower bound, so a
# gain cannot be read there and a scaled copy of it is not a measurement.
CLIPPED_LUMA = 250
# Gain support: a plate tile needs this much luma texture (std, 0..255) and the frame tile must
# correlate with it at least this well to be trusted as unchanged background.
GAIN_MIN_TEXTURE = 6.0
GAIN_MIN_CORRELATION = 0.9
GAIN_MIN_TILES = 12
GAIN_MIN_TILE_FRACTION = 0.03
# A tile supports the gain only if at least this share of its pixels is unclipped.
GAIN_MIN_USABLE_FRACTION = 0.5
GAIN_RANGE = (0.5, 2.0)


def _tile_grid(height: int, width: int, edge: int):
    rows, cols = -(-height // edge), -(-width // edge)
    return rows, cols


def _tile_sum(values: np.ndarray, edge: int, rows: int, cols: int) -> np.ndarray:
    """Sum a H×W(×C) array over each tile (partial edge tiles included)."""
    height, width = values.shape[:2]
    padded_shape = (rows * edge, cols * edge) + values.shape[2:]
    padded = np.zeros(padded_shape, np.float64)
    padded[:height, :width] = values
    return padded.reshape(rows, edge, cols, edge, *values.shape[2:]).sum(axis=(1, 3))


def _tile_counts(height: int, width: int, edge: int, rows: int, cols: int) -> np.ndarray:
    return _tile_sum(np.ones((height, width), np.float64), edge, rows, cols)


def _frame_gain(frame: np.ndarray, plate: np.ndarray, frame_clipped: np.ndarray,
                plate_clipped: np.ndarray, edge: int, rows: int, cols: int):
    """Per-channel gain mapping ``frame`` onto ``plate``, from tiles whose structure matches.

    Support tiles are textured and correlate with the plate (luma, on pixels with no clipped
    channel on either side). Each channel's gain is the median plate/frame ratio over the support
    tiles, using only pixels where that channel is unclipped on both sides: a clipped sample is
    not a measurement. Returns None when a channel has too few support tiles to measure it.
    """
    height, width = plate.shape[:2]
    counts = _tile_counts(height, width, edge, rows, cols)
    minimum = max(GAIN_MIN_TILES, GAIN_MIN_TILE_FRACTION * rows * cols)

    def tiles(values, weight):
        return _tile_sum(values * weight, edge, rows, cols)

    plate_gray = cv2.cvtColor(plate, cv2.COLOR_BGR2GRAY).astype(np.float64)
    frame_gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY).astype(np.float64)
    weight = (~(frame_clipped | plate_clipped).any(axis=2)).astype(np.float64)
    n = _tile_sum(weight, edge, rows, cols)
    safe_n = np.maximum(n, 1.0)
    p_mean, f_mean = tiles(plate_gray, weight) / safe_n, tiles(frame_gray, weight) / safe_n
    p_var = tiles(plate_gray ** 2, weight) / safe_n - p_mean ** 2
    f_var = tiles(frame_gray ** 2, weight) / safe_n - f_mean ** 2
    cov = tiles(plate_gray * frame_gray, weight) / safe_n - p_mean * f_mean
    p_std, f_std = np.sqrt(np.maximum(p_var, 0.0)), np.sqrt(np.maximum(f_var, 0.0))
    correlation = cov / np.maximum(p_std * f_std, 1e-6)
    support = ((n >= GAIN_MIN_USABLE_FRACTION * counts) & (p_std >= GAIN_MIN_TEXTURE)
               & (f_std >= GAIN_MIN_TEXTURE) & (correlation >= GAIN_MIN_CORRELATION)
               & (p_mean >= 20) & (f_mean >= 20))
    gain = np.empty(3)
    for channel in range(3):
        channel_weight = (~(frame_clipped[:, :, channel] | plate_clipped[:, :, channel])).astype(np.float64)
        usable = support & (_tile_sum(channel_weight, edge, rows, cols) >= GAIN_MIN_USABLE_FRACTION * counts)
        if np.count_nonzero(usable) < minimum:
            return None
        p_sum = tiles(plate[:, :, channel].astype(np.float64), channel_weight)[usable]
        f_sum = tiles(frame[:, :, channel].astype(np.float64), channel_weight)[usable]
        gain[channel] = np.median(p_sum / np.maximum(f_sum, 1.0))
    if not np.all((gain >= GAIN_RANGE[0]) & (gain <= GAIN_RANGE[1])):
        return None
    return gain


def _apply_gain(frame: np.ndarray, gain: np.ndarray) -> np.ndarray:
    return np.clip(frame.astype(np.float32) * gain.astype(np.float32), 0, 255).astype(np.uint8)


def _clipped(image: np.ndarray, *, near: bool) -> np.ndarray:
    """Per channel (H×W×3): clipped in the unsmoothed source, or with ``near`` also within the blur
    radius of a clipped value. The gain avoids every sample a clip may have contaminated; a
    comparison treats only the clipped samples themselves as bounds, so a thin subject in front
    of a blown window is still measured where it is."""
    clipped = image >= CLIPPED_LUMA
    if not near:
        return clipped
    kernel = np.ones(SMOOTH_KERNEL, np.uint8)
    return np.stack([cv2.dilate(clipped[:, :, channel].astype(np.uint8), kernel)
                     for channel in range(3)], axis=2).astype(bool)


def _differs(image: np.ndarray, reference: np.ndarray, image_clipped: np.ndarray,
             reference_clipped: np.ndarray) -> np.ndarray:
    """Pixels whose luma or LAB chroma residual exceeds the estimator's agreement limits.

    A clipped channel is only a lower bound (after any gain). Before the comparison each clipped
    channel takes the value, consistent with that bound, closest to the other side (the larger of
    the two), so a pixel departs only when even the most agreeing reading of its clipped channels
    departs: a blown window under a relight agrees with itself, while a red or pink shirt in front
    of it still departs on its measured channels and its chroma.
    """
    if image_clipped.any() or reference_clipped.any():
        image, reference = (np.where(image_clipped, np.maximum(image, reference), image),
                            np.where(reference_clipped, np.maximum(reference, image), reference))
    gray = cv2.cvtColor(image, cv2.COLOR_BGR2GRAY).astype(np.float32)
    ref_gray = cv2.cvtColor(reference, cv2.COLOR_BGR2GRAY).astype(np.float32)
    ab = cv2.cvtColor(image, cv2.COLOR_BGR2LAB)[:, :, 1:].astype(np.float32)
    ref_ab = cv2.cvtColor(reference, cv2.COLOR_BGR2LAB)[:, :, 1:].astype(np.float32)
    luma = np.abs(gray - ref_gray) / 255.0
    chroma = np.sqrt(((ab - ref_ab) ** 2).sum(axis=2))
    return (luma > LUMA_THRESHOLD) | (chroma > CHROMA_THRESHOLD)


def seen_clear_regions(frames: list[np.ndarray], plate: np.ndarray, *, edge: int = TILE_EDGE,
                       recent: int = RECENT_CLEAR_FRAMES) -> dict:
    """Classify every tile of the registered ``frames`` against the clean-plate ``plate``.

    Returns ``pending`` (bool rows×cols: ever occupied and not yet seen clear), the per-frame
    ``occupied`` tiles, and a JSON-ready ``report``.
    """
    if not frames:
        raise ValueError("seen_clear_regions needs at least one frame")
    height, width = plate.shape[:2]
    if any(frame.shape != plate.shape for frame in frames):
        raise ValueError("frames and plate must share one registered pixel grid")
    rows, cols = _tile_grid(height, width, edge)
    counts = _tile_counts(height, width, edge, rows, cols)
    smooth_plate = cv2.GaussianBlur(plate, SMOOTH_KERNEL, SMOOTH_SIGMA)
    plate_clipped, plate_near = _clipped(plate, near=False), _clipped(plate, near=True)
    occupied, corrected, departed, gains = [], [], [], []
    clipped = [_clipped(frame, near=False) for frame in frames]
    for frame, frame_clipped in zip(frames, clipped):
        smooth = cv2.GaussianBlur(frame, SMOOTH_KERNEL, SMOOTH_SIGMA)
        gain = _frame_gain(smooth, smooth_plate, _clipped(frame, near=True), plate_near, edge, rows, cols)
        gains.append(None if gain is None else [round(float(value), 4) for value in gain])
        if gain is None:
            corrected.append(None)
            departed.append(None)
            occupied.append(np.ones((rows, cols), bool))  # Unreadable frame: vouches for nothing.
            continue
        matched = _apply_gain(smooth, gain)
        corrected.append(matched)
        differs = _differs(matched, smooth_plate, frame_clipped, plate_clipped)  # departs from the plate
        departed.append(differs)
        occupied.append(_tile_sum(differs.astype(np.float64), edge, rows, cols) / counts > OCCUPIED_PIXEL_FRACTION)
    occupied = np.stack(occupied)                     # frames × rows × cols
    clear = ~occupied
    window = min(recent, len(frames))
    recent_clear = clear[-window:].all(axis=0)

    # States: every pair of frames is compared, in any order. Two frames are *distinct* at a tile
    # when more than OCCUPIED_PIXEL_FRACTION of its pixels differ between them, and the *same*
    # when they are not distinct and on the same side of the plate test (both occupied or both
    # clear), or both departed and their departures mostly coincide. A state seen
    # in at least the visibility floor of the frames could be the background, so a tile is
    # ambiguous when a departed state, or two distinct recurring states, reach the floor; a median plate
    # between two such states can agree with both. Gainless frames may hold any content.
    count = len(frames)
    floor = max(2, crowd_auto_mask.min_frames_for_visibility(count))
    unknown = sum(item is None for item in departed)
    same = np.zeros((count, count, rows, cols), bool)
    distinct = np.zeros((count, count, rows, cols), bool)
    for first in range(count):
        if departed[first] is None:
            continue
        same[first, first] = True
        for second in range(first + 1, count):
            if departed[second] is None:
                continue
            differs = _differs(corrected[second], corrected[first], clipped[second], clipped[first])
            apart = _tile_sum(differs.astype(np.float64), edge, rows, cols) / counts > OCCUPIED_PIXEL_FRACTION
            union = departed[first] | departed[second]
            coincide = (occupied[first] & occupied[second]
                        & (_tile_sum((differs & union).astype(np.float64), edge, rows, cols)
                           <= SAME_CONTENT_PIXEL_FRACTION * _tile_sum(union.astype(np.float64), edge, rows, cols)))
            same[first, second] = same[second, first] = (
                (~apart & (occupied[first] == occupied[second])) | coincide)
            distinct[first, second] = distinct[second, first] = apart
    known = np.array([item is not None for item in departed])
    stable = known[:, None, None] & (same.sum(axis=1) + unknown >= floor)   # frames × rows × cols
    # Two distinct frames are two states when each is the same as at least the floor of frames
    # that are not the same as the other. Frames the same as both (an intermediate exposure, or a
    # shared background) neither bridge two recurring states nor split one lightly varying one.
    exclusive = (same[:, None] & ~same[None, :]).sum(axis=2) + unknown   # frames × frames × rows × cols
    two_states = (distinct & (exclusive >= floor) & (exclusive.swapaxes(0, 1) >= floor)).any(axis=(0, 1))
    ambiguous = (stable & occupied).any(axis=0) | two_states | (unknown >= floor)
    ever_occupied = occupied.any(axis=0) | distinct.any(axis=(0, 1))
    ambiguous &= ever_occupied
    pending = ever_occupied & (~recent_clear | ambiguous)

    def pixel_share(tiles):
        return float(counts[tiles].sum() / counts.sum())

    report = {
        "tile_px": edge, "grid": [rows, cols], "recent_clear_frames": window,
        "pending_regions": int(np.count_nonzero(pending)),
        "pending_share": pixel_share(pending),
        "ever_occupied_share": pixel_share(ever_occupied),
        "ambiguous_regions": int(np.count_nonzero(ambiguous)), "alternative_floor_frames": floor,
        "frames_without_gain": [index for index, gain in enumerate(gains) if gain is None],
        "all_seen_clear": not bool(pending.any()),
    }
    return {"pending": pending, "occupied": occupied, "report": report, "gains": gains}


def pending_mask(pending: np.ndarray, height: int, width: int, edge: int = TILE_EDGE) -> np.ndarray:
    """Expand the tile verdict to a H×W bool mask (True = not yet seen clear)."""
    return np.repeat(np.repeat(pending, edge, axis=0), edge, axis=1)[:height, :width]
