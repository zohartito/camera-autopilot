"""Bounded JPEG computational stacks with private disk-backed source staging.

All public merges preserve full input dimensions and return BGR uint8 pixels.
Decode and registration are sequential; fusion, averaging, median and sharpest
region selection use bounded halo tiles. HDR uses a fixed four-level Laplacian
exposure fusion, not radiometric HDR or RAW development. Alignment is estimated
on at most 1024px thumbnails (MTB for brackets, phase-initialized ECC then ORB
for equal-exposure sequences). Unrecoverable registration falls back to identity;
parallax, persistent occlusion and physically unrecorded detail remain limits.

The allocation estimate is not a measured process RSS guarantee. A private
2GiB staging budget and checked free disk space bound temporary source pixels.
"""

from __future__ import annotations

from pathlib import Path
from contextlib import contextmanager
import os
import shutil
import tempfile

import cv2
import numpy as np

# ECC alignment tuning. Euclidean handles handheld translation + small rotation.
_ECC_MOTION_MODEL = cv2.MOTION_EUCLIDEAN
_ECC_ITERATIONS = 200
_ECC_EPS = 1e-6

# ORB fallback tuning (used only when ECC fails to converge).
_ORB_FEATURES = 2000
_ORB_GOOD_MATCH_RATIO = 0.75
_ORB_MIN_MATCHES = 12

# Region size (odd) for smoothing the focus-stack sharpness maps so the source
# is chosen per region rather than per noisy single pixel.
# Wide enough that the sharpest-frame choice is regional, not decided per pixel by noise.
_FOCUS_BLUR_KERNEL = 21
# Below this sharpness spread no frame is meaningfully sharper, so average instead.
_FOCUS_DECISION_FLOOR = 0.6

# Camera RAW suffixes are recognized only to provide a safe JPEG-twin fallback.
# Their native decoder is deliberately disabled until it can prove a bounded
# decoded allocation. ".arw" is the Sony A7 II RAW; the rest cover other bodies.
# Per-merge memory budget: the peak one merge may add to its process's resident memory.
# On the 8 GB CM5 the OS, the control server and perception share the rest (taste retrieval
# alone measured a 2.1 GiB process peak), so a merge gets 2 GiB. A job whose estimated peak
# (``estimate_merge_peak``) exceeds it is refused before any pixel is decoded, with a reason,
# rather than being killed by the OOM killer halfway through.
MAX_STACK_WORKING_BYTES = 2 * 1024 * 1024 * 1024
# The estimate is not an OS/process RSS guarantee. Sources are decoded one at a
# time into private temporary uint8 files. Only halo tiles are materialized
# together, even for the 16-frame maximum.
STACK_ALGORITHM_VERSION = "disk-tiled-laplacian-v4"
STACK_TILE_SIZE = 512
# Full-width row bands for passes that stream whole rows (the automatic crowd estimator). Its
# per-band working set is tens of bytes per pixel for every frame, so 128 rows keeps it near
# 100 MB at 40 MP; the result does not depend on the band height.
STACK_BAND_ROWS = 128
_STACK_HALO = 64
_STACK_PYRAMID_LEVELS = 4
_STACK_ALIGNMENT_EDGE = 1024
MAX_STACK_TEMP_BYTES = 2 * 1024 * 1024 * 1024

_RAW_SUFFIXES = (".arw", ".raw", ".dng", ".nef", ".cr2", ".cr3", ".rw2", ".orf", ".raf")


def _decode_raw(path: str | Path) -> np.ndarray:
    """Refuse RAW decoder entry until it supports a trustworthy pre-decode bound.

    The prior native decoder exposed no portable header-only decoded-buffer
    estimate. Bounding compressed input does not bound demosaic allocation, so
    this path is intentionally tombstoned instead of accepting an unbounded decode.
    """
    raise ImportError("RAW stacking is disabled pending a bounded RAW header decoder")


def _read_one(path: str | Path) -> np.ndarray:
    """Read a single frame as BGR uint8, using JPEG-twin fallback for RAW.

    RAW frames are not decoded directly. A sibling JPEG (the in-camera twin
    Sony writes alongside each RAW) is tried instead, so stacking retains a
    bounded path without entering an unbounded native demosaic decoder.
    """
    p = Path(path)
    from image_safety import MAX_DECODED_RGB_BYTES, image_size_from_bytes, source_bytes

    def decode_bgr(candidate: Path) -> np.ndarray | None:
        # One bounded snapshot supplies both header validation and OpenCV decode.
        snapshot = source_bytes(candidate)
        image_size_from_bytes(snapshot)
        encoded = np.frombuffer(snapshot, dtype=np.uint8)
        decoded = cv2.imdecode(encoded, cv2.IMREAD_COLOR)
        if decoded is None or decoded.nbytes > MAX_DECODED_RGB_BYTES:
            return None
        return decoded

    if p.suffix.lower() in _RAW_SUFFIXES:
        try:
            return _decode_raw(p)
        except ImportError:
            # Direct RAW is disabled: fall back to the JPEG twin the camera wrote beside it.
            for jpeg in (p.with_suffix(".JPG"), p.with_suffix(".jpg"),
                         p.with_suffix(".JPEG"), p.with_suffix(".jpeg")):
                if jpeg.exists():
                    image = decode_bgr(jpeg)
                    if image is not None:
                        return image
            raise FileNotFoundError(
                f"cannot read RAW {p}: direct RAW decode is disabled and no JPEG twin was found"
            )
    image = decode_bgr(p)
    if image is None:
        raise FileNotFoundError(f"could not read image: {p}")
    return image


# Peak-estimate coefficients per merge mode. ``full`` is bytes per source pixel for the
# full-resolution arrays alive at the mode's peak; ``tile`` is bytes per halo-tile pixel per
# frame; ``band`` is bytes per row-band pixel per frame (the crowd estimator streams
# STACK_BAND_ROWS-row full-width bands). Accounted from the code, then checked against
# fresh-process peaks measured on 12, 24 and 40 MP frames (tools/puck_workload_bench.py,
# Apple M4 Max, 2026-09-27; tests/test_merge_memory_budget.py pins the estimate >= 1.2x each).
# The measured growth of the peak per source pixel (12 -> 40 MP, or 24 -> 40 MP):
#   staging, every mode: decoded + warped source, gray view, encoded snapshot  (~7.5 B/px)
#   long 9.3, crowd 12.2, focus 8.8 B/px: staging, the uint8 output, per-tile work
#   hdr 17.8 B/px: output 3, deghost binaries 2, component labels 4 and the feathered
#       float mask 3 x 4 B/px on top of the tiled fusion; the recovery motion repair (F01)
#       adds its region/rewritten/detected maps, one bracket's binaries and component labels
#       and the pair histograms before fusion, and on macOS the freed pages stay in the
#       high-water mark: 40 MP x 16 with repairs measured 1.43 GiB, so hdr is charged 34
#   night 18.9 B/px: the same mask pipeline plus the returned uint8 motion mask
#   crowd_auto 25.5 B/px at 8 frames: temporal median, support counts, recovery image and
#       maps, fallback median, staging and the per-frame row bands; the masks stream to disk
# The estimate sits at least 1.35x above every measured peak, and the largest accepted camera
# frame (40 MP at 3:2, 4:3, 16:9 or 1:1; 16 frames, 8 for crowd_auto) is estimated at 1.0-1.7
# GiB. The row-band term grows with WIDTH, so an extreme strip (e.g. 40000x1000) can exceed
# the budget; such a job is refused by the guard, which is its purpose.
_MERGE_COEFFICIENTS = {
    "long": {"full": 16, "tile": 16, "band": 0},
    "crowd": {"full": 16, "tile": 32, "band": 0},
    "focus": {"full": 16, "tile": 72, "band": 0},
    "hdr": {"full": 34, "tile": 40, "band": 0},
    "night": {"full": 30, "tile": 16, "band": 0},
    "crowd_auto": {"full": 24, "tile": 32, "band": 48},
}
# Native scratch that does not scale with the frame: OpenCV thread buffers, the JPEG codec,
# allocator slack.
_MERGE_FIXED_BYTES = 256 * 1024 * 1024


def estimate_merge_peak(mode: str, width: int, height: int, count: int, *,
                        band_rows: int | None = None) -> int:
    """Estimated peak bytes one merge adds to its process, from dimensions and count alone.

    Monotone in width, height and count, so a job bounds every smaller one of the same shape.
    ``band_rows`` is the row-band height a streaming pass will use (default STACK_BAND_ROWS).
    It is an estimate calibrated on measured peaks, not a guarantee; re-measure on the
    target board (tools/puck_workload_bench.py) before trusting it there.
    """
    coefficients = _MERGE_COEFFICIENTS.get(mode)
    if coefficients is None:
        raise ValueError(f"unknown merge mode {mode!r}")
    pixels = width * height
    tile_pixels = min(width, STACK_TILE_SIZE + 2 * _STACK_HALO) * min(height, STACK_TILE_SIZE + 2 * _STACK_HALO)
    band_pixels = min(height, band_rows or STACK_BAND_ROWS) * width
    # Plus the 1/2**levels float copy of every frame (and its weights) that carries the coarse
    # fusion base; charged to every mode so the estimate never depends on which merge runs.
    coarse_pixels = pixels // (1 << (2 * _STACK_PYRAMID_LEVELS))
    return (coefficients["full"] * pixels
            + count * (coefficients["tile"] * tile_pixels + coefficients["band"] * band_pixels
                       + coarse_pixels * 16)
            + _MERGE_FIXED_BYTES)


def _working_estimate(width: int, height: int, count: int, *, mask_output: bool = False,
                      mode: str | None = None, band_rows: int | None = None) -> int:
    """Estimated merge peak; ``mask_output`` selects the deghosted night average, which also
    returns a full-resolution uint8 motion mask beside its image."""
    return estimate_merge_peak(mode or ("night" if mask_output else "long"), width, height, count,
                               band_rows=band_rows)


def _validate_sources(paths: list[str | Path], *, mask_output: bool = False,
                      mode: str | None = None, band_rows: int | None = None) -> tuple[int, int]:
    """Reject count, dimensions, aggregate encoded bytes and disk/RAM estimates
    before any native pixel decode. RAW inputs resolve only to their JPEG twin.
    """
    from stack_session import validate_frames, MAX_SESSION_SOURCE_BYTES
    from image_safety import image_size, source_bytes
    if not paths:
        raise ValueError("paths is empty; need at least one image")
    validate_frames(len(paths))
    shape = None
    encoded_bytes = 0
    for path in paths:
        candidate = Path(path)
        if candidate.suffix.lower() in _RAW_SUFFIXES:
            twins = [candidate.with_suffix(suffix) for suffix in (".JPG", ".jpg", ".JPEG", ".jpeg")]
            candidate = next((twin for twin in twins if twin.is_file()), None)
            if candidate is None:
                raise FileNotFoundError(f"cannot read RAW {path}: direct RAW decode is disabled and no JPEG twin was found")
        current = image_size(candidate)
        encoded_bytes += len(source_bytes(candidate))
        if shape is None:
            shape = current
        elif current != shape:
            raise ValueError(f"image {path} has shape {current}, expected {shape}")
    width, height = shape
    if encoded_bytes > MAX_SESSION_SOURCE_BYTES:
        raise ValueError("stack exceeds aggregate source-byte budget")
    disk_bytes = width * height * 3 * len(paths)
    if disk_bytes > MAX_STACK_TEMP_BYTES:
        raise ValueError("stack exceeds aggregate temporary-storage budget")
    mode = mode or ("night" if mask_output else "long")
    estimate = _working_estimate(width, height, len(paths), mode=mode, band_rows=band_rows)
    if estimate > MAX_STACK_WORKING_BYTES:
        raise ValueError(
            f"stack exceeds aggregate working-memory budget: a {mode} merge of {len(paths)} "
            f"{width}x{height} frames needs an estimated {estimate / 2**30:.2f} GiB against the "
            f"{MAX_STACK_WORKING_BYTES / 2**30:.2f} GiB per-merge budget; use fewer or smaller frames")
    return width, height


def _thumbnail(image: np.ndarray) -> np.ndarray:
    height, width = image.shape[:2]
    scale = min(1., _STACK_ALIGNMENT_EDGE / max(height, width))
    return cv2.resize(image, (max(1, round(width * scale)), max(1, round(height * scale))),
                      interpolation=cv2.INTER_AREA)


def _registration(reference: np.ndarray, target: np.ndarray, *, hdr: bool) -> np.ndarray:
    """Return target-to-reference homogeneous transform in thumbnail coordinates.

    HDR uses exposure-invariant MTB translation; equal-exposure sequences use
    ECC then ORB. A failed estimate explicitly falls back to identity. This is
    bounded registration, not a guarantee against parallax or subject motion.
    """
    identity = np.eye(3, dtype=np.float64)
    if hdr:
        shift = cv2.createAlignMTB(max_bits=5).calculateShift(reference, target)
        # Textureless MTB can produce the maximum search shift with no evidence.
        # Accept only an actual improvement in median-threshold disagreement.
        def score(dx, dy):
            height, width = reference.shape
            x0, x1 = max(0, dx), min(width, width + dx)
            y0, y1 = max(0, dy), min(height, height + dy)
            if x1 <= x0 or y1 <= y0:
                return 1.
            ref = reference[y0:y1, x0:x1]
            tgt = target[y0-dy:y1-dy, x0-dx:x1-dx]
            rm, tm = np.median(reference), np.median(target)
            valid = (np.abs(ref.astype(np.float32) - rm) > 4) & (np.abs(tgt.astype(np.float32) - tm) > 4)
            if np.count_nonzero(valid) < .05 * reference.size:
                return 1.
            return float(np.mean((ref[valid] > rm) != (tgt[valid] > tm)))
        if score(*shift) + .005 < score(0, 0):
            identity[:2, 2] = shift
        return identity
    try:
        affine = _align_ecc(reference, target)
        inverse = cv2.invertAffineTransform(affine)
        identity[:2] = inverse
        return identity
    except cv2.error:
        homography = _align_orb(reference, target)
        return homography if homography is not None else identity


def _gray_hist(image: np.ndarray) -> np.ndarray:
    gray = cv2.cvtColor(image, cv2.COLOR_BGR2GRAY)
    # calcHist avoids np.bincount's full-frame int64 input conversion.
    return cv2.calcHist([gray], [0], None, [256], [0, 256]).ravel().astype(np.float64)


@contextmanager
def _staged_stack(paths: list[str | Path], *, hdr: bool = False, align: bool = True,
                  mask_output: bool = False, mode: str | None = None, band_rows: int | None = None):
    """Private disk-backed registered uint8 frames, never all decoded in RAM.

    Input snapshots are decoded once. Temporary raw pixels are removed on success
    and exceptions. Up to 16 x 40MP inputs fit the 2GiB temporary-storage policy;
    actual disk free space and the separate working estimate may reject a job.
    """
    width, height = _validate_sources(paths, mask_output=mask_output,
                                      mode=mode or ("hdr" if hdr else None), band_rows=band_rows)
    with tempfile.TemporaryDirectory(prefix="camera-stack-") as directory:
        if shutil.disk_usage(directory).free < width * height * 3 * len(paths) + 64 * 1024 * 1024:
            raise ValueError("insufficient temporary storage for registered stack")
        files, histograms = [], []
        reference = None
        for index, path in enumerate(paths):
            image = _read_one(path)
            if image.shape != (height, width, 3):
                raise ValueError("stack source dimensions changed after validation")
            small = cv2.cvtColor(_thumbnail(image), cv2.COLOR_BGR2GRAY)
            if reference is None:
                reference = small
            elif align:
                transform = _registration(reference, small, hdr=hdr)
                scaling = np.diag([width / small.shape[1], height / small.shape[0], 1.])
                transform = scaling @ transform @ np.linalg.inv(scaling)
                if not np.allclose(transform, np.eye(3), atol=1e-4):
                    image = cv2.warpPerspective(image, transform, (width, height),
                                                flags=cv2.INTER_LINEAR, borderMode=cv2.BORDER_REFLECT)
            histograms.append(_gray_hist(image))
            target = Path(directory) / f"{index:02d}.bgr"
            descriptor = os.open(target, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
            with os.fdopen(descriptor, "wb") as handle:
                image.tofile(handle)
            files.append(target)
            del image
        yield files, (height, width, 3), histograms


def _tile_from_file(path: Path, shape: tuple[int, int, int], bounds) -> np.ndarray:
    # Close each mapping immediately after copying its tile. Keeping full-file
    # mappings alive would let sequential scans accumulate resident source pages.
    mapped = np.memmap(path, dtype=np.uint8, mode="r", shape=shape)
    y0, y1, x0, x1 = bounds
    try:
        return np.array(mapped[y0:y1, x0:x1])
    finally:
        mapped._mmap.close()


def _tiles(shape):
    height, width = shape[:2]
    for y in range(0, height, STACK_TILE_SIZE):
        for x in range(0, width, STACK_TILE_SIZE):
            y1, x1 = min(y + STACK_TILE_SIZE, height), min(x + STACK_TILE_SIZE, width)
            bounds = (max(0, y-_STACK_HALO), min(height, y1+_STACK_HALO),
                      max(0, x-_STACK_HALO), min(width, x1+_STACK_HALO))
            by, _, bx, _ = bounds
            yield bounds, (slice(y, y1), slice(x, x1)), (slice(y-by, y1-by), slice(x-bx, x1-bx))


def _gaussian_pyramid(image, levels):
    result = [image]
    for _ in range(levels):
        result.append(cv2.pyrDown(result[-1]))
    return result


# Mertens well-exposedness: the standard Gaussian width about mid-grey.
_FUSE_EXPOSURE_SIGMA = 0.2
# Floors so a flat, neutral region is decided by well-exposedness rather than by noise.
_FUSE_WEIGHT_FLOOR = 1e-2
# Blur applied to the selection map so the blend does not switch bracket pixel to pixel.
_FUSE_WEIGHT_SMOOTH = 3.0


def _unit(image: np.ndarray) -> np.ndarray:
    """View an image as float32 in [0,1], whether it arrived as uint8 or already scaled."""
    return image.astype(np.float32) / 255. if image.dtype == np.uint8 else image.astype(np.float32)


def _fusion_weights(images: list[np.ndarray]) -> list[np.ndarray]:
    """Normalised Mertens selection maps: contrast x saturation x well-exposedness."""
    weights = []
    for image in images:
        rgb = _unit(image)
        gray = cv2.cvtColor(rgb, cv2.COLOR_BGR2GRAY)
        contrast = np.abs(cv2.Laplacian(gray, cv2.CV_32F))
        saturation = np.std(rgb, axis=2)
        # Mertens weights are contrast x saturation x WELL-EXPOSEDNESS. The third term was
        # missing, and without it a flat neutral area (a beige wall) has contrast and
        # saturation both near zero in every bracket, so the normalised weights there were
        # decided by sensor noise. That produced low-frequency blotches on walls, ceilings and
        # upholstery that exist in none of the inputs (measured 2026-09-17: wall mottling 8.4
        # in the merge against 4.0-6.8 in the brackets). The floors keep those regions decided
        # by the smooth well-exposedness term instead of by noise.
        well_exposed = np.prod(np.exp(-((rgb - 0.5) ** 2) / (2. * _FUSE_EXPOSURE_SIGMA ** 2)), axis=2)
        weight = (contrast + _FUSE_WEIGHT_FLOOR) * (saturation + _FUSE_WEIGHT_FLOOR) * well_exposed
        # Smooth the selection map. Per-pixel weights make the blend switch bracket from one
        # pixel to the next in flat areas, which reads as grain that no single frame has.
        # The support is far smaller than the tile halo, so interiors stay boundary-independent.
        weights.append(cv2.GaussianBlur(weight, (0, 0), _FUSE_WEIGHT_SMOOTH) + 1e-12)
    denominator = np.sum(weights, axis=0)
    return [weight / denominator for weight in weights]


def _fuse_pyramid(images: list[np.ndarray], weights: list[np.ndarray], levels: int,
                  base: np.ndarray | None = None) -> np.ndarray:
    """Blend images through a Laplacian pyramid using precomputed normalised weights.

    ``base`` replaces the coarsest level with one computed elsewhere, which is how the
    scales a tile cannot reach get blended; see ``_coarse_frames``.
    """
    combined = None
    for image, weight in zip(images, weights):
        pyramid = _gaussian_pyramid(_unit(image), levels)
        wp = _gaussian_pyramid(weight, levels)
        if combined is None:
            combined = [np.zeros_like(level) for level in pyramid]
        for level in range(levels):
            up = cv2.pyrUp(pyramid[level+1], dstsize=(pyramid[level].shape[1], pyramid[level].shape[0]))
            combined[level] += (pyramid[level] - up) * wp[level][:, :, None]
        combined[-1] += pyramid[-1] * wp[-1][:, :, None]
    result = combined[-1]
    if base is not None:
        if base.shape != combined[-1].shape:
            raise ValueError(f"fusion base {base.shape} does not match pyramid base {combined[-1].shape}")
        result = base.astype(np.float32)
    for level in range(levels-1, -1, -1):
        result = cv2.pyrUp(result, dstsize=(combined[level].shape[1], combined[level].shape[0])) + combined[level]
    return result


def _fuse_tile(images: list[np.ndarray], levels: int,
               base: np.ndarray | None = None) -> np.ndarray:
    """Mertens-family exposure fusion over a bounded Laplacian pyramid.

    A 64-pixel halo makes interior tile results independent of tile boundaries (unlike
    arbitrary local tonemaps). A tile can only carry ``levels`` of pyramid, so on its own
    it blends the brackets at scales up to 2**levels pixels and no further; ``base``
    supplies the rest.
    """
    return _fuse_pyramid(images, _fusion_weights(images), levels, base)


def _coarse_frames(files: list[Path], shape: tuple[int, int, int], levels: int
                   ) -> tuple[list[np.ndarray], list[np.ndarray]] | None:
    """Every frame AND its selection map at 1/2**levels, from the tiles' own pyramid.

    Exposure fusion has to blend the brackets at EVERY scale up to the whole frame. A tile
    can only carry a few pyramid levels, so tiled fusion alone blends up to 2**levels pixels
    and stops: on a 6000x4000 room that is 16 px out of 6000. The low frequencies then never
    move between brackets, which flattens global tonality, and every exposure decision is
    forced into fine scales, which reads as halos and crunch. Measured on the A7 II daylight
    bracket (2026-09-20), fusing the same frames at 4 levels gave std 38.4 and Laplacian
    detail 913 against the best bracket's 59.1/798; at full depth, 53.1/851. **The shallow
    pyramid also scored BETTER on highlight clipping (1.78 % against 4.40 %), because
    flattening a photograph is one way to stop clipping it.**

    Both the pixels and the NORMALISED weights are carried down, because Mertens computes the
    selection maps at full resolution and downsamples those. Recomputing weights on an already
    downsampled frame is a different (and wrong) quantity: |Laplacian| of a downsampled image
    is not the downsampled |Laplacian|. Carrying them makes the composite exactly the
    full-depth whole-image fusion rather than merely a closer approximation to it.

    The tile halo makes each tile's base level interior-exact, so stitching those interiors
    reproduces the whole-image Gaussian level without ever materialising a full-resolution
    float frame. Returns ``None`` when the geometry does not divide evenly, in which case the
    caller keeps the shallow (worse, but correct) result.
    """
    scale = 1 << levels
    if levels <= 0 or shape[0] % scale or shape[1] % scale:
        return None
    coarse_shape = (shape[0] // scale, shape[1] // scale)
    images = [np.zeros(coarse_shape + (shape[2],), dtype=np.float32) for _ in files]
    weights = [np.zeros(coarse_shape, dtype=np.float32) for _ in files]
    for bounds, dest, interior in _tiles(shape):
        if bounds[0] % scale or bounds[2] % scale:
            return None
        rows = slice(dest[0].start // scale, dest[0].stop // scale)
        cols = slice(dest[1].start // scale, dest[1].stop // scale)
        inner = (slice(interior[0].start // scale, interior[0].stop // scale),
                 slice(interior[1].start // scale, interior[1].stop // scale))
        tiles = [_tile_from_file(path, shape, bounds) for path in files]
        for index, (tile, weight) in enumerate(zip(tiles, _fusion_weights(tiles))):
            small = _gaussian_pyramid(_unit(tile), levels)[-1][inner]
            small_weight = _gaussian_pyramid(weight, levels)[-1][inner]
            if small.shape[:2] != (rows.stop - rows.start, cols.stop - cols.start):
                return None
            images[index][rows, cols] = small
            weights[index][rows, cols] = small_weight
    return images, weights


def _align_ecc(reference_gray: np.ndarray, target_gray: np.ndarray) -> np.ndarray:
    """Estimate a 2x3 Euclidean warp mapping target onto reference via ECC.

    Raises cv2.error if ECC fails to converge.
    """
    warp_matrix = np.eye(2, 3, dtype=np.float32)
    shift, response = cv2.phaseCorrelate(reference_gray.astype(np.float32),
                                        target_gray.astype(np.float32))
    if response > .2 and abs(shift[0]) < reference_gray.shape[1] / 4 and abs(shift[1]) < reference_gray.shape[0] / 4:
        warp_matrix[:, 2] = shift
    criteria = (
        cv2.TERM_CRITERIA_EPS | cv2.TERM_CRITERIA_COUNT,
        _ECC_ITERATIONS,
        _ECC_EPS,
    )
    _, warp_matrix = cv2.findTransformECC(
        reference_gray, target_gray, warp_matrix, _ECC_MOTION_MODEL, criteria
    )
    return warp_matrix


def _align_orb(
    reference_gray: np.ndarray, target_gray: np.ndarray
) -> np.ndarray | None:
    """Estimate a 3x3 homography mapping target onto reference via ORB matches.

    Returns None when too few good matches are found to trust the estimate.
    """
    orb = cv2.ORB_create(_ORB_FEATURES)
    ref_keypoints, ref_descriptors = orb.detectAndCompute(reference_gray, None)
    tgt_keypoints, tgt_descriptors = orb.detectAndCompute(target_gray, None)
    if ref_descriptors is None or tgt_descriptors is None:
        return None

    matcher = cv2.BFMatcher(cv2.NORM_HAMMING)
    raw_matches = matcher.knnMatch(tgt_descriptors, ref_descriptors, k=2)

    good_matches = [
        m
        for pair in raw_matches
        if len(pair) == 2
        for m, n in [pair]
        if m.distance < _ORB_GOOD_MATCH_RATIO * n.distance
    ]
    if len(good_matches) < _ORB_MIN_MATCHES:
        return None

    target_points = np.float32(
        [tgt_keypoints[m.queryIdx].pt for m in good_matches]
    ).reshape(-1, 1, 2)
    reference_points = np.float32(
        [ref_keypoints[m.trainIdx].pt for m in good_matches]
    ).reshape(-1, 1, 2)

    homography, _ = cv2.findHomography(
        target_points, reference_points, cv2.RANSAC, 5.0
    )
    return homography


# --- HDR deghosting -------------------------------------------------------- #
# A bracket of a scene with a MOVING subject ghosts under plain Mertens fusion:
# the subject appears at several positions at once because every frame contributes
# its own copy. The fix below picks ONE reference frame and, in the regions where
# the other frames disagree with it on *content* (after compensating for the
# exposure difference), takes the result from the reference only — so the mover
# appears exactly once. Static regions still get the full multi-frame fusion that
# recovers shadow + highlight detail.

# Reference-vs-frame content disagreement above this (normalized 0..1, after
# exposure matching) marks a pixel as "moving" -> deghost it to the reference.
# 0.12 clears residual exposure-match + alignment error while a real moving
# subject (a person against a different backdrop) reads well above it.
_DEGHOST_DIFF_THRESHOLD = 0.12
# A pixel at/near the 8-bit floor or ceiling in EITHER bracket carries no recoverable
# content there: exposure-matching a clipped level can't reproduce the other bracket's
# detail, so the residual is a CLIPPING artifact, not subject motion. Marking such a
# pixel "moving" would paste the single reference back over a STATIC high-contrast
# region and discard the multi-frame HDR recovery. We suppress the mask wherever either
# frame is within this margin of pure black / pure white.
_DEGHOST_CLIP_LOW = 8
_DEGHOST_CLIP_HIGH = 247
# A subject can differ from the background mostly in COLOR at a similar brightness
# (a red coat over green grass), which the grayscale residual alone misses, leaving a
# chroma ghost. The LAB a/b chroma channels barely move with exposure (luma absorbs the
# bracket step), so a large chroma distance between frame and reference is genuine
# content disagreement. This is the a/b Euclidean distance (8-bit units) above which a
# pixel also counts as moving.
_DEGHOST_CHROMA_THRESHOLD = 25
# Grow + smooth the moving mask so the deghosted region has a soft, seam-free
# border instead of a hard cut against the fused background.
_DEGHOST_DILATE = 9
_DEGHOST_FEATHER = 21
# The pointwise disagreement test above marks a pixel "moving" whenever the matched-luma
# residual OR the LAB chroma distance clears a low detection floor. On a heavily clipped
# bracket (the A7 II blows 20% to white against the dark reference's 1.6%) the global
# histogram match is poor, so ordinary texture and JPEG chroma noise clear that floor and
# the raw mask paints scattered speckle AND soft, just-over-threshold blobs across a static
# scene; deghosting them pastes the dark reference over midtone upholstery -> dark blotches
# that exist in no input frame (A7 II session 7c27b568, 2026-09-20). Real subject motion is
# BOTH spatially contiguous AND an unambiguous content change; matching error and noise are
# not. Two guards, applied to the binary before it is grown and feathered, restore that:
#   1. drop connected components smaller than this fraction of the FRAME area (kills isolated
#      speckle), and
#   2. keep a component only if it holds a "seed" pixel whose disagreement is well clear of
#      the detection floor in luma OR chroma (kills weak blobs that never rise above it).
# The seed is hysteresis, not a raised detection floor: the low floor still fixes a real
# mover's FULL extent (its soft edges are not clipped away), while the seed only asks that
# somewhere inside that region the evidence is decisive. Measured 2026-09-20 the false blob
# carried matched-luma residual ~0.006 and chroma ~25 (right at the 25 floor) with no seed;
# the real A7 III night mover carried luma ~0.21 (p90 0.38) and chroma p90 ~62 densely across
# its body. The seed floors sit in the wide gap between them. The min-area is a FRACTION of
# frame area (not a raw pixel count) so it means the same thing at any sensor resolution:
# ~240 px at 24 MP, scaling up on larger frames where noise blobs are also larger in pixels.
_DEGHOST_MIN_AREA_FRACTION = 1e-5
_DEGHOST_SEED_DIFF = 0.06
_DEGHOST_SEED_CHROMA = 55.0


def _exposure_match_gray(source: np.ndarray, reference: np.ndarray) -> np.ndarray:
    """Scale `source` grayscale to the reference's brightness via histogram matching.

    Brackets differ in exposure EVERYWHERE, so a raw grayscale diff would flag the
    whole frame as "moving". Matching the source's tone curve to the reference makes
    the comparison exposure-invariant: only genuine CONTENT differences (the moving
    subject) survive, not the global brightness step between brackets.
    """
    src = cv2.cvtColor(source, cv2.COLOR_BGR2GRAY)
    ref = cv2.cvtColor(reference, cv2.COLOR_BGR2GRAY)
    src_hist = np.bincount(src.ravel(), minlength=256).astype(np.float64)
    ref_hist = np.bincount(ref.ravel(), minlength=256).astype(np.float64)
    src_cdf = np.cumsum(src_hist) / max(src.size, 1)
    ref_cdf = np.cumsum(ref_hist) / max(ref.size, 1)
    # For each source level, the reference level with the closest cumulative mass.
    lut = np.searchsorted(ref_cdf, src_cdf).clip(0, 255).astype(np.uint8)
    return lut[src]


def _moving_disagreement(frame: np.ndarray, reference: np.ndarray, lut: np.ndarray | None = None,
                         *, suppress_clipped: bool = True) -> tuple[np.ndarray, np.ndarray]:
    """Pointwise ``(weak, seed)`` content-disagreement binaries for one frame vs reference.

    After exposure-matching the frame to the reference, a large residual means the two
    frames disagree on what is there. ``weak`` fires at the low detection floor (matched
    luma residual OR LAB a/b chroma distance) and defines a mover's full extent; ``seed``
    fires only where that disagreement is decisive (`_DEGHOST_SEED_*`). Both are pure
    per-pixel tests, so a pixel's value is independent of any tiling around it — the
    spatial reasoning that follows is what must be done coherently, not this.

    Pixels where EITHER bracket is near-clipped (pure black / pure white) are cleared from
    both when ``suppress_clipped`` is set: there a residual reflects unrecoverable clipping,
    not motion, and deghosting it would paste the reference over a static high-contrast
    region and discard the HDR detail the multi-frame fuse recovered. ``suppress_clipped=
    False`` keeps them: an equal-exposure night sequence has no bracket step, so black in
    one frame and lit in another IS content change.
    """
    frame_gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
    ref_gray_u8 = cv2.cvtColor(reference, cv2.COLOR_BGR2GRAY)
    matched = (_exposure_match_gray(frame, reference) if lut is None else lut[frame_gray]).astype(np.float32) / 255.0
    ref_gray = ref_gray_u8.astype(np.float32) / 255.0
    diff = np.abs(matched - ref_gray)
    # LAB a/b are near exposure-invariant, so a large a/b distance is real content change
    # even where luma matches (a chroma ghost, e.g. a red coat over green grass).
    frame_ab = cv2.cvtColor(frame, cv2.COLOR_BGR2LAB)[:, :, 1:].astype(np.float32)
    ref_ab = cv2.cvtColor(reference, cv2.COLOR_BGR2LAB)[:, :, 1:].astype(np.float32)
    chroma_dist = np.sqrt(((frame_ab - ref_ab) ** 2).sum(axis=2))
    weak = ((diff > _DEGHOST_DIFF_THRESHOLD) | (chroma_dist > _DEGHOST_CHROMA_THRESHOLD)).astype(np.uint8)
    seed = ((diff > _DEGHOST_SEED_DIFF) | (chroma_dist > _DEGHOST_SEED_CHROMA)).astype(np.uint8)
    if suppress_clipped:
        clipped = ((frame_gray <= _DEGHOST_CLIP_LOW) | (frame_gray >= _DEGHOST_CLIP_HIGH)
                   | (ref_gray_u8 <= _DEGHOST_CLIP_LOW) | (ref_gray_u8 >= _DEGHOST_CLIP_HIGH))
        weak[clipped] = 0
        seed[clipped] = 0
    return weak, seed


def _coherent_binary(weak: np.ndarray, seed: np.ndarray) -> np.ndarray:
    """Keep only moving regions that are spatially coherent AND signal-seeded.

    This is meant to run once on the WHOLE-FRAME binary, never per tile: a region that
    would straddle a tile boundary is a single connected component here, so its keep/drop
    decision is one decision applied to both sides and no tile-edge discontinuity can
    arise. (A per-tile component-area test is NOT safe — a mover cropped by a boundary
    could fall under the threshold in one tile's view but not the other, cutting it at the
    seam — which is why the callers accumulate the pointwise binaries across tiles and call
    this once on the assembled frame.) Drops any 8-connected component smaller than
    ``_DEGHOST_MIN_AREA_FRACTION`` of the frame (isolated speckle) or without a seed pixel
    (a weak blob that never rises above the seed floor, e.g. chroma noise in a soft shadow).
    The result carries exactly the input weak pixels of the surviving components; growth
    and feathering happen afterwards in ``_grow_and_feather``.
    """
    if not weak.any():
        return weak
    count, labels, stats, _ = cv2.connectedComponentsWithStats(weak, connectivity=8)
    min_area = max(1, round(_DEGHOST_MIN_AREA_FRACTION * weak.shape[0] * weak.shape[1]))
    seeded = np.zeros(count, dtype=bool)
    seed_labels = labels[seed.astype(bool)]
    if seed_labels.size:
        seeded[seed_labels] = True
    keep = (stats[:, cv2.CC_STAT_AREA] >= min_area) & seeded
    keep[0] = False  # never keep the background label
    return keep[labels].astype(np.uint8)


def _grow_and_feather(binary: np.ndarray) -> np.ndarray:
    """Dilate (to cover the whole mover, not just its edges) then feather the binary into a
    soft 0..1 mask so the deghost composite has no hard seam. The support is far smaller
    than the callers' tile halo, so applied per tile it stays boundary-independent."""
    grown = cv2.dilate(binary, np.ones((_DEGHOST_DILATE, _DEGHOST_DILATE), np.uint8))
    feathered = cv2.GaussianBlur(grown.astype(np.float32), (_DEGHOST_FEATHER, _DEGHOST_FEATHER), 0)
    return np.clip(feathered, 0.0, 1.0)


def _moving_mask(frame: np.ndarray, reference: np.ndarray, lut: np.ndarray | None = None, *,
                 suppress_clipped: bool = True) -> np.ndarray:
    """Soft 0..1 mask of where `frame` shows different CONTENT than `reference`.

    Whole-frame convenience wrapper: pointwise disagreement, then the spatial-coherence +
    seed filter, then grow/feather. The tiled merges below do not call this — they build
    the pointwise binaries across tiles and run ``_coherent_binary`` once on the full frame
    so its connectivity is global; this wrapper is the equivalent for a single whole array.
    """
    weak, seed = _moving_disagreement(frame, reference, lut, suppress_clipped=suppress_clipped)
    return _grow_and_feather(_coherent_binary(weak, seed))


# A mild finish, not a rescue. It matches the fused luma part of the way back toward the
# bracket the camera itself exposed best. Being global and monotone, it cannot invent local
# contrast the way a local tonemap does, and it is tile-independent because the histogram is
# accumulated over the whole image first.
#
# It was 0.7 while the pyramid was too shallow to blend low frequencies (see `_coarse_frames`).
# That flattened every merge, and this curve was doing the work of hiding it: pushed that hard
# it also multiplied the fusion's already-exaggerated fine detail. With the depth fixed, 0.7
# measured 1.48x the best bracket's Laplacian detail on the A7 II room while 0.3 measured
# 1.17x, so the strength comes down to where the curve corrects tonality without re-crunching.
# Measured over four bracket sessions on both bodies (2026-09-20), 0.3 holds detail at
# 0.98-1.23x the best bracket and saturation at or above it.
HDR_TONALITY_STRENGTH = 0.3
_TONALITY_BAND_ROWS = 512


def _tonality_lut(fused_hist: np.ndarray, reference_hist: np.ndarray, strength: float) -> np.ndarray:
    """Monotone luma curve pulling the fused tonality `strength` of the way to the reference."""
    fused_cdf = np.cumsum(fused_hist) / max(float(np.sum(fused_hist)), 1.0)
    reference_cdf = np.cumsum(reference_hist) / max(float(np.sum(reference_hist)), 1.0)
    matched = np.searchsorted(reference_cdf, fused_cdf).clip(0, 255).astype(np.float64)
    identity = np.arange(256, dtype=np.float64)
    return np.clip(identity * (1.0 - strength) + matched * strength, 0, 255).astype(np.uint8)


def _apply_tonality(output: np.ndarray, lut: np.ndarray) -> None:
    """Apply the curve to luminance in row bands so peak memory stays bounded."""
    for start in range(0, output.shape[0], _TONALITY_BAND_ROWS):
        band = output[start:start + _TONALITY_BAND_ROWS]
        lab = cv2.cvtColor(band, cv2.COLOR_BGR2LAB)
        lab[:, :, 0] = cv2.LUT(lab[:, :, 0], lut)
        band[:] = cv2.cvtColor(lab, cv2.COLOR_LAB2BGR)


# Recovery finish. Exposure fusion re-renders the WHOLE photograph: well-exposedness pulls
# every pixel toward mid-grey, so a room that meters above mid-grey comes back darker, and
# blending gamma-encoded values from different exposures shifts hue. On the A7 II daylight
# room that measured L 136.4 against the bracket's 146.1 and b* 135.99 against 135.48 —
# darker and yellower, which reads as brown. It was rejected on sight for exactly that.
#
# The camera's own frame is right nearly everywhere; what it got wrong is the blown window.
# So the merge keeps the metered bracket as the photograph and substitutes fused content only
# where that bracket is losing information, matched into its tonality first. Everywhere else
# the output IS the camera's frame, so lightness, colour and local contrast are correct by
# construction rather than by correction. Measured on the same room: L 145.2, b* 135.48
# (exactly the bracket's), detail 1.04x, highlight clipping 4.83 % -> 0.05 %.
#
# This recovers highlights, not shadows. Lifting shadows is what carried the warm cast, and
# the metered bracket crushes far less than it blows (0.45 % against 4.98 % here).
HDR_RECOVERY_HIGHLIGHT = 225.0
HDR_RECOVERY_CEILING = 248.0
# OpenCV's float Gaussian reaches 4 sigma (64 px here), which must stay inside the tile halo
# or the feather would depend on tile boundaries.
HDR_RECOVERY_FEATHER = 16.0
# Owner decision (0.2 sweep, 2026-09-27): when less than this share of the metered frame is
# clipped there is nothing worth recovering, and running the merge can only cost the
# photograph something, so HDR hands back that frame itself. "Metered frame" here is the
# recovery anchor (the bracket the finish keeps), and "clipped" is exactly what the finish
# treats as blown: LAB L above HDR_RECOVERY_HIGHLIGHT in that frame.
HDR_NOTHING_TO_RECOVER_FRACTION = 0.005


def _recovery_lut(fused_hist: np.ndarray, anchor_hist: np.ndarray, ceiling: float) -> np.ndarray:
    """Monotone curve carrying fused luma onto the anchor's, fitted where the anchor is good.

    Fitting only on the anchor's well-exposed pixels is the point: the curve then agrees with
    the anchor through the midtones, and the fused frame's highlights — which have no
    counterpart in a clipped anchor — land above the fitted range, where the curve rolls off
    to ``ceiling`` instead of being pushed back into clipping. Matching the full histogram
    would reproduce the anchor's clipped top exactly and give the recovery back.
    """
    if anchor_hist.sum() <= 0 or fused_hist.sum() <= 0:
        return np.arange(256, dtype=np.uint8)
    anchor_cdf = np.cumsum(anchor_hist) / anchor_hist.sum()
    fused_cdf = np.cumsum(fused_hist) / fused_hist.sum()
    lut = np.maximum.accumulate(
        np.searchsorted(anchor_cdf, fused_cdf).clip(0, 255).astype(np.float32))
    top = int(np.searchsorted(fused_cdf, 0.999))
    top = min(max(top, 1), 254)
    lut[top:] = np.interp(np.arange(top, 256, dtype=np.float32), [top, 255], [lut[top], ceiling])
    return np.maximum.accumulate(lut).clip(0, 255).astype(np.uint8)


# Motion inside the recovery region (ledger F01, 2026-09-27 A7 III receipt). The recovery finish
# takes its highlight content from the darker brackets, and the existing deghost does not
# protect it: the anchor is blown there, the brighter brackets are clipped, the deghost skips
# pixels that are near-black in either bracket (a subject against a window, in a fast frame),
# and its single reference can itself hold the subject. The 9-frame receipt pasted the owner,
# translucent, into a recovered window from the two darkest brackets (frames 0 and 1).
#
# So before fusing, every bracket is checked against one reference bracket R inside the
# recovery region, after exposure-normalising the pair on the pixels where both are unclipped
# (``_exposure_lut``: the brighter one is mapped down into the darker one's scale, where its
# noise shrinks). The test is relative, gap / max, so it means the same at every exposure, it
# treats a clipped value as an interval (``_pair_difference_table``), and it is only taken
# where the pair carries signal. R is chosen once, on the whole region, from the two brackets
# that agree with each other best (``_reference_scores``): two separate exposures agreeing is
# strong evidence that neither holds a passing subject. (An absolute threshold in the darker
# scale picked the darkest bracket, which held the subject: compressing a scale hides
# disagreement.) A disagreement becomes a rejection only as a spatially coherent, seeded
# component of the whole frame (``_coherent_binary``), and a rejected bracket's pixels there
# are replaced, in its private staged copy, by R's pixels mapped into its exposure. Fusion,
# deghost and recovery then run unchanged on the repaired brackets, so a subject cannot leak
# back through the pyramid's coarse levels the way a zeroed fusion weight would let it. The
# anchor is never repaired or compared; outside the recovery region nothing is repaired.
HDR_MOTION_DIFF = 0.35
HDR_MOTION_SEED = 0.6
HDR_MOTION_SIGNAL_FLOOR = 48.0
# A rejected region is grown this far before it is feathered: a subject's soft edge (hair,
# motion blur) falls under the thresholds but still reads as an outline if it is left in.
# Replacing a static pixel with the reference's is harmless, so the margin can be generous.
HDR_MOTION_GROW = 31
# R must be unclipped over at least this share of the anchor's blown core to be a reference.
HDR_MOTION_REFERENCE_COVERAGE = 0.5
# Chroma disagreement (LAB a/b), for a subject at the background's grey level that the luma
# table cannot see. It is taken only inside the anchor's blown core, where the recovery takes
# its content and the brackets being compared are the darker ones, close in exposure; the
# brighter brackets' saturation drifts too far (static p99.9 up to 62 over the whole region).
# Each bracket's a/b are shifted by its median offset from the reference where luma agrees,
# and only pixels with signal and clear of the highlight shoulder in both count (near black
# a/b are noise, near clipping the camera desaturates). Static core residuals on real
# receipts: p99 9-17, p99.9 16-34; an orange subject at a pale window's grey level: 53-78.
HDR_MOTION_CHROMA = 30.0
HDR_MOTION_CHROMA_SEED = 50.0
_MOTION_CHROMA_TOP = 230
_MOTION_CHROMA_STRIDE = 7
# A bracket is repaired only if some third bracket (neither it nor R) agrees with R, on at most
# this share of their common pixels. Otherwise nothing independent says R is the clean one: with
# three brackets the two non-anchor ones tie, and repairing from a reference that holds the
# subject copies the subject into the clean bracket. Real receipts: 0.0001-0.029.
HDR_MOTION_CORROBORATION = 0.05
# ...and a detected component is repaired only if, over at least this share of its pixels, some
# other bracket carries a value (unclipped, above the signal floor) and agrees with the reference
# there; otherwise the repair abstains for that component.
HDR_MOTION_WITNESS_SHARE = 0.5


def _recovery_weight(anchor_bgr: np.ndarray) -> np.ndarray:
    """The recovery finish's feathered blend weight for a halo tile of the anchor."""
    weight = np.clip(
        (cv2.cvtColor(anchor_bgr, cv2.COLOR_BGR2LAB)[:, :, 0].astype(np.float32)
         - HDR_RECOVERY_HIGHLIGHT) / (255. - HDR_RECOVERY_HIGHLIGHT), 0, 1)
    return cv2.GaussianBlur(weight, (0, 0), HDR_RECOVERY_FEATHER)


def _unclipped(gray: np.ndarray) -> np.ndarray:
    return (gray > _DEGHOST_CLIP_LOW) & (gray < _DEGHOST_CLIP_HIGH)


def _cdf_lut(source_hist: np.ndarray, target_hist: np.ndarray) -> np.ndarray:
    """Monotone curve carrying the source histogram onto the target (same pixels in both)."""
    if source_hist.sum() <= 0 or target_hist.sum() <= 0:
        return np.arange(256, dtype=np.uint8)
    source_cdf = np.cumsum(source_hist) / source_hist.sum()
    target_cdf = np.cumsum(target_hist) / target_hist.sum()
    return np.searchsorted(target_cdf, source_cdf).clip(0, 255).astype(np.uint8)


# Levels of the brighter bracket with fewer common pixels than this are interpolated.
_MOTION_LUT_MIN_SAMPLES = 64
# A bracket sharing fewer unclipped pixels than this with the reference cannot be exposure-
# normalised against it, so it is not checked (a bracket clipped across the whole region).
_MOTION_MIN_OVERLAP = 4096


def _exposure_lut(joint: np.ndarray) -> tuple[np.ndarray, np.ndarray] | None:
    """Brighter bracket (rows) to darker bracket (columns): the median darker value at each
    brighter level, made monotone by ``_isotonic`` and interpolated where a level is sparse. A conditional
    median ignores the minority of pixels a subject or a misregistered edge contributes, where
    matching the two histograms lets them bend the ends of the curve (four stops apart on a
    sparse overlap, that sent a clipped bracket's top to 202 instead of ~60). ``None`` when
    too few pixels sit on well-populated levels to estimate the exposure relation at all.

    Returns (lower, upper) bounds per level. Between supported levels they coincide; beyond
    them only monotonicity is known, so a level below the lowest supported one maps to
    [0, curve there] and one above the highest to [curve there, 255]. A constant extension
    would claim a mapping the data never showed and flag static pixels as motion."""
    counts = joint.sum(axis=1)
    known = np.flatnonzero(counts >= _MOTION_LUT_MIN_SAMPLES)
    if counts[known].sum() < _MOTION_MIN_OVERLAP:
        return None
    cumulative = np.cumsum(joint[known], axis=1)
    medians = np.argmax(cumulative >= (counts[known] / 2.)[:, None], axis=1).astype(np.float64)
    fitted = _isotonic(medians, counts[known].astype(np.float64))
    levels = np.arange(256)
    curve = np.interp(levels, known, fitted).astype(np.float32)
    lower = np.where(levels < known[0], 0., curve).astype(np.float32)
    upper = np.where(levels > known[-1], 255., curve).astype(np.float32)
    return lower, upper


def _isotonic(values: np.ndarray, weights: np.ndarray) -> np.ndarray:
    """Weighted non-decreasing least-squares fit (pool adjacent violators). A running maximum
    lets one sparse outlier level (a few dark pixels of a bright bracket over a lit window)
    lift the whole curve; pooling weighs it by how many pixels it holds."""
    blocks: list[list[float]] = []            # [weighted mean, weight, length]
    for value, weight in zip(values, weights):
        blocks.append([value, weight, 1])
        while len(blocks) > 1 and blocks[-2][0] > blocks[-1][0]:
            mean_b, weight_b, length_b = blocks.pop()
            mean_a, weight_a, length_a = blocks.pop()
            total = weight_a + weight_b
            blocks.append([(mean_a * weight_a + mean_b * weight_b) / total, total, length_a + length_b])
    return np.concatenate([np.full(length, mean) for mean, _weight, length in blocks])


def _pair_difference_table(joint: np.ndarray, first_brighter: bool, *, signed: bool = False) -> np.ndarray:
    """Relative disagreement for every (first, second) gray pair, in the darker bracket's scale
    (``None`` when the pair's exposure relation cannot be estimated).

    A clipped value is an interval, not a number: at or below ``_DEGHOST_CLIP_LOW`` the scene
    is somewhere in [0, low], at or above ``_DEGHOST_CLIP_HIGH`` somewhere in [high, 255]. The
    pair disagrees by the gap between the two intervals once the brighter one is mapped into
    the darker scale, so a black subject in the darker bracket against a lit window in the
    other is a disagreement, while a shadow that is black in one and nearly black in the other
    is not. The gap is divided by the larger of the two nearest points and only counted where
    that point reaches ``HDR_MOTION_SIGNAL_FLOOR``. With ``signed`` the value is negative where
    the first's interval lies above the second's (positive: the first is the darker one).
    """
    levels = np.arange(256, dtype=np.float32)
    clipped_low, clipped_high = levels <= _DEGHOST_CLIP_LOW, levels >= _DEGHOST_CLIP_HIGH
    mapping = _exposure_lut(joint if first_brighter else joint.T)
    if mapping is None:
        return None

    def interval(low, high):
        # Clipped high means "at least the clip level", clipped low "at most it", in the
        # bracket's own scale and, mapped, in the darker one's.
        lower = np.where(clipped_high, low[_DEGHOST_CLIP_HIGH], np.where(clipped_low, 0., low))
        upper = np.where(clipped_low, high[_DEGHOST_CLIP_LOW], np.where(clipped_high, 255., high))
        return lower, upper

    if first_brighter:
        first = tuple(bound[:, None] for bound in interval(*mapping))
        second = tuple(bound[None, :] for bound in interval(levels, levels))
    else:
        first = tuple(bound[:, None] for bound in interval(levels, levels))
        second = tuple(bound[None, :] for bound in interval(*mapping))
    above = second[0] - first[1]          # second's interval lies above first's
    below = first[0] - second[1]          # first's interval lies above second's
    gap = np.maximum(np.maximum(above, below), 0.)
    top = np.where(above > 0, second[0], np.where(below > 0, first[0], 0.))
    table = np.where(top >= HDR_MOTION_SIGNAL_FLOOR, gap / np.maximum(top, 1.), 0.)
    return np.where(below > 0, -table, table) if signed else table


def _recovery_region_statistics(files: list[Path], shape: tuple[int, int, int], tone_idx: int,
                                 others: list[int]):
    """Pass 1: the recovery region, each bracket's unclipped share of the anchor's blown core,
    and the joint gray histogram of every pair of brackets where both are unclipped in it."""
    pairs = [(a, b) for position, a in enumerate(others) for b in others[position + 1:]]
    joints = {pair: np.zeros(65536, dtype=np.int64) for pair in pairs}
    region = np.zeros(shape[:2], dtype=bool)
    core_total, core_valid = 0, np.zeros(len(files), dtype=np.int64)
    for bounds, dest, interior in _tiles(shape):
        anchor = _tile_from_file(files[tone_idx], shape, bounds)
        inside = _recovery_weight(anchor)[interior] > 0
        if not inside.any():
            continue
        region[dest] = inside
        core = cv2.cvtColor(anchor[interior], cv2.COLOR_BGR2LAB)[:, :, 0] > HDR_RECOVERY_HIGHLIGHT
        core_total += int(np.count_nonzero(core))
        grays, valid = {}, {}
        for index in others:
            grays[index] = cv2.cvtColor(_tile_from_file(files[index], shape, bounds)[interior],
                                        cv2.COLOR_BGR2GRAY)
            valid[index] = _unclipped(grays[index]) & inside
            core_valid[index] += int(np.count_nonzero(valid[index] & core))
        for a, b in pairs:
            both = valid[a] & valid[b]
            joints[(a, b)] += np.bincount(grays[a][both].astype(np.int64) * 256 + grays[b][both],
                                          minlength=65536)
    return region, core_total, core_valid, {pair: joint.reshape(256, 256) for pair, joint in joints.items()}


def _joint_of(joints: dict, first: int, second: int) -> np.ndarray:
    """Joint histogram with ``first``'s gray levels on the rows."""
    return joints[(first, second)] if (first, second) in joints else joints[(second, first)].T


def _pair_disagreement(joints: dict, first: int, second: int, means: list[float],
                       chroma: dict | None = None) -> float:
    """Share of the pair's common unclipped pixels where it disagrees beyond exposure, plus, when
    ``chroma`` has it, the share of its blown-core colour samples that disagree (a subject can
    differ only in colour, and the luma share alone would call such a pair consistent)."""
    joint = _joint_of(joints, first, second)
    total = int(joint.sum())
    if total < _MOTION_MIN_OVERLAP:
        return float("inf")
    table = _pair_difference_table(joint, means[first] >= means[second])
    if table is None:
        return float("inf")
    colour = (chroma or {}).get((min(first, second), max(first, second))) or 0.0
    return int(joint[table > HDR_MOTION_DIFF].sum()) / total + colour


def _reference_scores(joints: dict, others: list[int], candidates: list[int],
                      means: list[float], chroma: dict | None = None) -> dict:
    """Each candidate's disagreements with every other bracket, smallest first; the smallest
    tuple wins. The first entry is its best-agreeing partner; the next ones break the tie
    between the two members of that pair, so the other brackets arbitrate (with two subjects
    in two brackets and one clean bracket, each subject bracket agrees best with the clean one,
    but the clean one agrees better with the rest). A lone candidate still gets a tuple, and
    its first entry is reported, so a reference without a partner is visible in evidence."""
    return {candidate: tuple(sorted(_pair_disagreement(joints, candidate, other, means, chroma)
                                    for other in others if other != candidate))
            for candidate in candidates}


# Colour samples for the reference choice are capped so memory stays flat at any frame size.
_MOTION_CHROMA_SAMPLES = 200_000


def _core_chroma_disagreements(files: list[Path], shape: tuple[int, int, int], tone_idx: int,
                               others: list[int], core_total: int) -> dict:
    """Per bracket pair, the share of blown-core colour samples that disagree beyond the pair's
    median a/b offset, on samples where both carry chroma (``_chroma_usable``); ``None`` for a pair
    with too few such samples. One strided sample of the core, the same pixels for every bracket."""
    stride = max(_MOTION_CHROMA_STRIDE, core_total // _MOTION_CHROMA_SAMPLES + 1)
    colours = {index: [] for index in others}
    grays = {index: [] for index in others}
    seen = 0
    for bounds, _dest, interior in _tiles(shape):
        core = _core_of(_tile_from_file(files[tone_idx], shape, bounds)[interior]).ravel()
        positions = np.flatnonzero(core)
        if not len(positions):
            continue
        # A global stride across tiles, so the sample does not depend on tile boundaries.
        picked = positions[(seen + np.arange(len(positions))) % stride == 0]
        seen += len(positions)
        if not len(picked):
            continue
        for index in others:
            tile = _tile_from_file(files[index], shape, bounds)[interior]
            colours[index].append(_chroma_ab(tile)[picked])
            grays[index].append(cv2.cvtColor(tile, cv2.COLOR_BGR2GRAY).ravel()[picked])
    colours = {index: np.concatenate(parts) if parts else np.zeros((0, 2), np.float32)
               for index, parts in colours.items()}
    grays = {index: np.concatenate(parts) if parts else np.zeros(0, np.uint8)
             for index, parts in grays.items()}
    result = {}
    for position, first in enumerate(others):
        for second in others[position + 1:]:
            both = _chroma_usable(grays[first], grays[second], np.ones(len(grays[first]), bool))
            if both.sum() * stride < _MOTION_MIN_OVERLAP:
                result[(first, second)] = None
                continue
            difference = colours[second][both] - colours[first][both]
            offset = np.median(difference, axis=0)
            result[(first, second)] = float(np.mean(
                np.linalg.norm(difference - offset, axis=1) > HDR_MOTION_CHROMA))
    return result


def _chroma_ab(tile: np.ndarray) -> np.ndarray:
    """LAB a/b of a tile, centred on neutral, as float32 (n, 2)."""
    return cv2.cvtColor(tile, cv2.COLOR_BGR2LAB).reshape(-1, 3)[:, 1:].astype(np.float32) - 128.


def _chroma_usable(gray: np.ndarray, reference_gray: np.ndarray, core: np.ndarray) -> np.ndarray:
    """Where a chroma comparison means something: inside the anchor's blown core (the pixels the
    recovery actually takes from these brackets), with signal and clear of the shoulder in both."""
    return (core & (gray >= HDR_MOTION_SIGNAL_FLOOR) & (gray < _MOTION_CHROMA_TOP)
            & (reference_gray >= HDR_MOTION_SIGNAL_FLOOR) & (reference_gray < _MOTION_CHROMA_TOP))


def _core_of(anchor_tile: np.ndarray) -> np.ndarray:
    return cv2.cvtColor(anchor_tile, cv2.COLOR_BGR2LAB)[:, :, 0] > HDR_RECOVERY_HIGHLIGHT


def _fit_chroma_offset(files: list[Path], shape: tuple[int, int, int], region: np.ndarray,
                       tone_idx: int, index: int, reference: int, table: np.ndarray):
    """Median a/b offset from a bracket onto the reference, over a sample of the blown core where
    both carry chroma and luma agrees; a median, so a coloured subject does not move it. ``None``
    when too few pixels qualify for the comparison to mean anything."""
    samples = []
    for bounds, dest, interior in _tiles(shape):
        inside = region[dest]
        if not inside.any():
            continue
        tile = _tile_from_file(files[index], shape, bounds)[interior]
        reference_tile = _tile_from_file(files[reference], shape, bounds)[interior]
        gray = cv2.cvtColor(tile, cv2.COLOR_BGR2GRAY)
        reference_gray = cv2.cvtColor(reference_tile, cv2.COLOR_BGR2GRAY)
        core = _core_of(_tile_from_file(files[tone_idx], shape, bounds)[interior])
        usable = (inside & _chroma_usable(gray, reference_gray, core)
                  & (table[gray, reference_gray] <= HDR_MOTION_DIFF)).ravel()
        picked = np.flatnonzero(usable)[::_MOTION_CHROMA_STRIDE]
        if len(picked):
            samples.append(_chroma_ab(reference_tile)[picked] - _chroma_ab(tile)[picked])
    if not samples or sum(len(item) for item in samples) * _MOTION_CHROMA_STRIDE < _MOTION_MIN_OVERLAP:
        return None
    return np.median(np.concatenate(samples), axis=0).astype(np.float32)


def _repair_recovery_motion(files: list[Path], shape: tuple[int, int, int], tone_idx: int,
                            means: list[float]) -> dict:
    """Find brackets that disagree with the reference inside the recovery region and repair
    them in their staged copies. Returns the motion evidence; see the comment above."""
    report = {"motion_reference_index": None, "motion_reference_partner_disagreement": None,
              "motion_checked_fraction": 0.0, "motion_detected_fraction": 0.0,
              "motion_rejected_fraction": 0.0, "motion_rejected_frames": {},
              "motion_uncorroborated_frames": [], "motion_uncorroborated_fraction": 0.0}
    others = [index for index in range(len(files)) if index != tone_idx]
    if len(others) < 2 or len(files) > 16:
        return report
    region, core_total, core_valid, joints = _recovery_region_statistics(files, shape, tone_idx, others)
    region_total = int(np.count_nonzero(region))
    if region_total == 0 or core_total == 0:
        return report
    candidates = [index for index in others
                  if core_valid[index] >= HDR_MOTION_REFERENCE_COVERAGE * core_total]
    if not candidates:
        return report
    chroma = _core_chroma_disagreements(files, shape, tone_idx, others, core_total)
    scores = _reference_scores(joints, others, candidates, means, chroma)
    # Of the best-agreeing pair, the one that covers more of the blown core.
    reference = min(candidates, key=lambda index: (
        scores[index], -core_valid[index], abs(means[index] - means[tone_idx]), index))
    report["motion_reference_index"] = reference
    partner = scores[reference][0] if scores[reference] else float("inf")
    report["motion_reference_partner_disagreement"] = None if partner == float("inf") else partner
    tables = {index: _pair_difference_table(_joint_of(joints, index, reference), means[index] >= means[reference])
              for index in others if index != reference
              and _joint_of(joints, index, reference).sum() >= _MOTION_MIN_OVERLAP}
    tables = {index: table for index, table in tables.items() if table is not None}
    agrees = {other for other in others if other != reference
              and _pair_disagreement(joints, reference, other, means, chroma) <= HDR_MOTION_CORROBORATION}
    for index in list(tables):
        if not agrees - {index}:
            report["motion_uncorroborated_frames"].append(index)
            del tables[index]
    del joints
    if not tables:
        return report
    checked = 0
    for bounds, dest, interior in _tiles(shape):
        if region[dest].any():
            gray = cv2.cvtColor(_tile_from_file(files[reference], shape, bounds)[interior], cv2.COLOR_BGR2GRAY)
            checked += int(np.count_nonzero(_unclipped(gray) & region[dest]))
    report["motion_checked_fraction"] = checked / region_total
    rejected = np.zeros(shape[:2], dtype=bool)     # pixels rewritten in any bracket
    detected = np.zeros(shape[:2], dtype=bool)     # the detected cores, before growth
    # For each pixel, how many compared brackets carry a value there (unclipped, above the
    # signal floor) and agree with the reference: the per-pixel witnesses a component needs.
    witnesses = np.zeros(shape[:2], dtype=np.uint8)
    decisions = {}
    # One bracket at a time, reusing one pair of whole-frame binaries.
    weak_full = np.zeros(shape[:2], dtype=np.uint8)
    seed_full = np.zeros(shape[:2], dtype=np.uint8)
    for index, table in tables.items():
        # Pass 2: pointwise disagreement with the reference, plus the per-channel histograms
        # that map the reference into this bracket's exposure.
        weak_full.fill(0)
        seed_full.fill(0)
        channel_hists = np.zeros((2, 3, 256), dtype=np.int64)
        chroma_offset = _fit_chroma_offset(files, shape, region, tone_idx, index, reference, table)
        for bounds, dest, interior in _tiles(shape):
            inside = region[dest]
            if not inside.any():
                continue
            reference_tile = _tile_from_file(files[reference], shape, bounds)[interior]
            reference_gray = cv2.cvtColor(reference_tile, cv2.COLOR_BGR2GRAY)
            reference_valid = _unclipped(reference_gray) & inside
            tile = _tile_from_file(files[index], shape, bounds)[interior]
            gray = cv2.cvtColor(tile, cv2.COLOR_BGR2GRAY)
            # The bracket may be clipped here (a black subject in a dark bracket is exactly
            # the case to catch); only the reference has to carry a value.
            difference = table[gray, reference_gray]
            weak = reference_valid & (difference > HDR_MOTION_DIFF)
            seed = reference_valid & (difference > HDR_MOTION_SEED)
            if chroma_offset is not None:
                core = _core_of(_tile_from_file(files[tone_idx], shape, bounds)[interior])
                usable = reference_valid & _chroma_usable(gray, reference_gray, core)
                if usable.any():
                    distance = np.linalg.norm(_chroma_ab(tile) + chroma_offset - _chroma_ab(reference_tile),
                                              axis=1).reshape(gray.shape)
                    weak |= usable & (distance > HDR_MOTION_CHROMA)
                    seed |= usable & (distance > HDR_MOTION_CHROMA_SEED)
            weak_full[dest] = weak
            seed_full[dest] = seed
            witness = reference_valid & _unclipped(gray) & (gray >= HDR_MOTION_SIGNAL_FLOOR) & ~weak
            np.add(witnesses[dest], witness, out=witnesses[dest], casting="unsafe")
            # A colour subject at the background's grey level must not bend the colour maps the
            # repair uses, so chroma disagreement is excluded from their samples too.
            steady = reference_valid & _unclipped(gray) & ~weak
            for channel in range(3):
                channel_hists[0, channel] += np.bincount(tile[:, :, channel][steady], minlength=256)
                channel_hists[1, channel] += np.bincount(reference_tile[:, :, channel][steady], minlength=256)
        # Decide once on the whole frame; repairs wait until every bracket has been compared.
        moving = _coherent_binary(weak_full, seed_full)
        if not moving.any():
            continue
        rows, cols = np.nonzero(moving)
        box = (rows.min(), rows.max() + 1, cols.min(), cols.max() + 1)
        decisions[index] = (box, np.packbits(moving[box[0]:box[1], box[2]:box[3]], axis=None),
                            [_cdf_lut(channel_hists[1, channel], channel_hists[0, channel]) for channel in range(3)])
    del weak_full, seed_full
    # Component-level corroboration. A detected component is repaired only if, over most of it,
    # some OTHER bracket carries a value and agrees with the reference. Agreement elsewhere in the
    # region says nothing about the component (a brighter bracket clipped over the subject), and
    # when the reference itself holds the subject every clean bracket disagrees with it there, so
    # no component is corroborated and nothing is copied from it. Abstaining keeps the unrepaired
    # merge (#89's behaviour).
    uncorroborated = np.zeros(shape[:2], dtype=bool)
    # Pass 3, per bracket: repair the staged copy. The feather is applied per halo tile (its
    # support is far inside the halo), so no full-resolution float mask is ever held.
    for index, (box, packed, luts) in decisions.items():
        height, width = box[1] - box[0], box[3] - box[2]
        crop = np.unpackbits(packed, count=height * width).reshape(height, width)
        count, labels = cv2.connectedComponents(crop, connectivity=8)
        # The bracket's own pixels in a component are its disagreements, never its witnesses.
        supported = np.bincount(labels[witnesses[box[0]:box[1], box[2]:box[3]] > 0], minlength=count)
        sizes = np.bincount(labels.ravel(), minlength=count)
        keep = supported >= HDR_MOTION_WITNESS_SHARE * np.maximum(sizes, 1)
        keep[0] = False
        uncorroborated[box[0]:box[1], box[2]:box[3]] |= (~keep[labels]) & (labels > 0)
        moving = np.zeros(shape[:2], dtype=np.uint8)
        moving[box[0]:box[1], box[2]:box[3]] = keep[labels]
        del labels, crop
        if not moving.any():
            continue
        detected |= moving.astype(bool)
        grown = cv2.dilate(moving, np.ones((HDR_MOTION_GROW, HDR_MOTION_GROW), np.uint8))
        del moving
        repaired = _rewrite_from(files, shape, index, reference, grown, region, luts, rejected)
        del grown
        report["motion_rejected_frames"][str(index)] = repaired / region_total
    report["motion_uncorroborated_fraction"] = float(np.count_nonzero(uncorroborated & region)) / region_total
    report["motion_rejected_fraction"] = float(np.count_nonzero(rejected)) / region_total
    report["motion_detected_fraction"] = float(np.count_nonzero(detected & region)) / region_total
    return report


def _rewrite_from(files: list[Path], shape: tuple[int, int, int], index: int, source_index: int,
                  grown: np.ndarray, region: np.ndarray, luts: list[np.ndarray],
                  rejected: np.ndarray) -> int:
    """Blend ``source_index``'s pixels, mapped into bracket ``index``'s exposure by ``luts``, into
    that bracket's staged copy under the feathered ``grown`` mask, inside ``region`` only. Marks
    the written pixels in ``rejected`` and returns how many were written. The feather is applied
    per halo tile (its support is far inside the halo), so no full-resolution float mask is held."""
    written_total = 0
    for bounds, dest, interior in _tiles(shape):
        y0, y1, x0, x1 = bounds
        if not grown[y0:y1, x0:x1].any():
            continue
        # Inside the region only. The grown margin may cross pixels where the source is
        # clipped; those are still written (as the mapped clip level), because stopping there
        # left the subject showing through as dark specks along bright sills on the receipt.
        weight = _grow_and_feather(grown[y0:y1, x0:x1])[interior] * region[dest]
        if not weight.any():
            continue
        # Evidence counts what is actually rewritten (the grown, feathered mask), not only
        # the detected core, which a small subject's 31 px margin can outgrow many times.
        written = weight > 0
        written_total += int(np.count_nonzero(written))
        rejected[dest] |= written
        source = _tile_from_file(files[source_index], shape, bounds)[interior]
        mapped = np.dstack([cv2.LUT(source[:, :, channel], luts[channel])
                            for channel in range(3)]).astype(np.float32)
        staged = np.memmap(files[index], dtype=np.uint8, mode="r+", shape=shape)
        try:
            # One tile mapped at a time, so dirty pages never accumulate across the frame.
            current = np.array(staged[dest]).astype(np.float32)
            weight = weight[:, :, None]
            staged[dest] = np.clip(current + (mapped - current) * weight, 0, 255).astype(np.uint8)
            staged.flush()
        finally:
            staged._mmap.close()
    return written_total


# Held to the anchor (2026-09-29 A7 III receipt, five brackets 1/100..1/6 s). The owner walked
# through the room in EVERY bracket, so no two brackets agreed and the reference check above
# abstained (partner disagreement 0.52, frames 2-4 uncorroborated), and the darkest bracket, the
# only one holding the window, pasted a translucent silhouette of him into the recovered window.
#
# The anchor is the photograph the finish keeps, so whatever it shows is by definition the scene,
# and it says something even where it is blown: a clipped pixel means "at least this bright".
# Each bracket is therefore also compared with the anchor across the recovery region, with the
# same interval table as above (``_pair_difference_table``), once per colour channel: where the
# anchor is unclipped (the shoulder around a blown area) the comparison is two-sided, and where
# it is clipped a bracket that maps to something darker than the anchor's clip level disagrees
# with it (a person in front of a window the anchor shows blown). A disagreement counts where at
# least two channels make it in the same direction (the median of the three): a static saturated
# colour follows each channel's own tone curve but not the gray one, and gray alone erased static
# lamps as motion, while a single agreeing channel must not veto a red shirt. A spatially coherent, seeded disagreement is written
# over, in that bracket's staged copy, with the anchor mapped into the bracket's exposure. There
# is nothing else to trust: the anchor needs no corroboration, and where it is blown the result
# is the anchor's clip level, so that part of the region stays as blown as the photograph rather
# than recovered from a bracket that shows something else. This runs after the reference repair
# (which, when corroborated, restores real detail) and on every bracket, the reference included.
# What it cannot see: a subject at least as bright as the anchor's clip level inside the blown
# core (the anchor says nothing there beyond the lower bound), and a colour-only subject there.
# Disagreement narrower than this is dropped before the coherence test: on a static 2026-09-29
# receipt the only detections were 1-4 px lines along shelf and window-frame edges (whole-pixel
# registration residual), each long enough to pass as a component.
HDR_ANCHOR_MIN_WIDTH = 7
_ANCHOR_MIN_WIDTH_KERNEL = np.ones((HDR_ANCHOR_MIN_WIDTH, HDR_ANCHOR_MIN_WIDTH), np.uint8)


def _channels_unclipped(tile: np.ndarray) -> np.ndarray:
    """Pixels whose every channel is clear of both clip levels."""
    return (tile.max(axis=2) < _DEGHOST_CLIP_HIGH) & (tile.min(axis=2) > _DEGHOST_CLIP_LOW)


# A pixel is near neutral when its channel spread is at most this share of its brightest channel
# (with a floor for dark pixels, where the spread is mostly noise).
_NEUTRAL_SPREAD = 0.25
_NEUTRAL_FLOOR = 16


# A fit sample must differ between the two frames by at least this (floor + 1/share of the
# brighter value, about 3 %): a 1/3-stop step is about 7 % in encoded values.
_FIT_STEP_FLOOR = 2
_FIT_STEP_SHARE = 32


def _near_neutral(tile: np.ndarray) -> np.ndarray:
    brightest = tile.max(axis=2).astype(np.int16)
    spread = brightest - tile.min(axis=2)
    return spread <= np.maximum(_NEUTRAL_FLOOR, (_NEUTRAL_SPREAD * brightest).astype(np.int16))


def _anchor_region_joints(files: list[Path], shape: tuple[int, int, int], tone_idx: int,
                          others: list[int], means: list[float]) -> tuple[np.ndarray, dict]:
    """The recovery region, and for each bracket one joint histogram per channel with the anchor
    (bracket on the rows), where that channel is unclipped in both inside the region.

    Per channel, not gray: a camera renders each channel through its own tone curve, so a static
    saturated colour follows its channels' exposure relations but not the neutral gray one, and a
    channel clipped in one frame is just an interval in that channel's comparison."""
    joints = {index: np.zeros((3, 65536), dtype=np.int64) for index in others}
    region = np.zeros(shape[:2], dtype=bool)
    for bounds, dest, interior in _tiles(shape):
        anchor = _tile_from_file(files[tone_idx], shape, bounds)
        inside = _recovery_weight(anchor)[interior] > 0
        if not inside.any():
            continue
        region[dest] = inside
        anchor = anchor[interior]
        # The fit takes near-neutral pixels only, outside the anchor's blown core (which the finish
        # treats as unknown). Saturated colours are where a camera's per-channel curves, colour
        # matrix and saturation step depart from the exposure relation, and where clipping hides
        # (clipped, then re-rendered below 255); one large lamp at a sparse level bent the whole
        # relation and made a static window read as moving.
        exact = inside & ~_core_of(anchor) & _near_neutral(anchor)
        for index in others:
            tile = _tile_from_file(files[index], shape, bounds)[interior]
            sampled = exact & _near_neutral(tile)
            darker = means[index] < means[tone_idx]
            for channel in range(3):
                values, anchor_values = tile[:, :, channel], anchor[:, :, channel]
                # A static unclipped pixel reads darker in the darker frame; one that reads the
                # same in both is clipped in both (a patch clipped in every frame and re-rendered
                # below 255, as a monochrome or muted style does) and says nothing about exposure.
                low, high = (values, anchor_values) if darker else (anchor_values, values)
                high = high.astype(np.int16)
                stepped = low.astype(np.int16) <= high - (_FIT_STEP_FLOOR + high // _FIT_STEP_SHARE)
                both = sampled & stepped & _unclipped(values) & _unclipped(anchor_values)
                joints[index][channel] += np.bincount(values[both].astype(np.int64) * 256 + anchor_values[both],
                                                      minlength=65536)
    return region, {index: joint.reshape(3, 256, 256) for index, joint in joints.items()}


# Sony MakerNote picture styles that render monochrome: Creative Style (tag 0xB020, ASCII) "BW" and
# "Sepia", and Picture Effect (tag 0x200E) 4 "Posterization B/W" and 13 "High Contrast
# Monochrome" (ExifTool's Sony tables). The EXIF segment sits at the start of a camera JPEG.
_MONOCHROME_STYLES = {"BW", "Sepia"}
_MONOCHROME_EFFECTS = {4: "Posterization B/W", 13: "High Contrast Monochrome"}
_EXIF_READ_BYTES = 256 * 1024
# The Sony MakerNote headers whose directory follows at +12 (ExifTool MakerNotes.pm).
_SONY_MAKERNOTE_HEADERS = (b"SONY DSC \x00\x00\x00", b"SONY CAM \x00\x00\x00")


def _anchor_picture_style(path: str | Path) -> dict | None:
    """The anchor JPEG's Sony picture style as ``{"name", "monochrome"}``; ``None`` when it cannot
    be read (not a JPEG, no Sony MakerNote, a malformed segment), which keeps today's behaviour."""
    import struct
    path = Path(path)
    if path.suffix.lower() in _RAW_SUFFIXES:
        # Staging reads a RAW's JPEG twin. Only a single twin is unambiguously the file it read.
        twins = []
        for suffix in (".JPG", ".jpg", ".JPEG", ".jpeg"):
            twin = path.with_suffix(suffix)
            if twin.is_file() and not any(twin.samefile(seen) for seen in twins):
                twins.append(twin)
        if len(twins) != 1:
            return None
        path = twins[0]
    try:
        with open(path, "rb") as stream:
            data = stream.read(_EXIF_READ_BYTES)
        if data[:2] != b"\xff\xd8":
            return None
        position = 2
        tiff = None
        while position + 4 <= len(data) and data[position] == 0xFF:
            marker, length = data[position + 1], struct.unpack(">H", data[position + 2:position + 4])[0]
            if length < 2 or position + 2 + length > len(data):
                return None                    # a segment declared longer than what was read
            segment = data[position + 4:position + 2 + length]
            if marker == 0xE1 and segment[:6] == b"Exif\x00\x00":
                tiff = segment[6:]
                break
            if marker == 0xDA:
                break
            position += 2 + length
        if tiff is None or tiff[:2] not in (b"II", b"MM"):
            return None
        order = "<" if tiff[:2] == b"II" else ">"
        if struct.unpack(order + "H", tiff[2:4])[0] != 42:
            return None                        # not a TIFF header

        def entries(offset, end=None):
            if offset < 8:
                raise ValueError("directory inside the TIFF header")
            count = struct.unpack(order + "H", tiff[offset:offset + 2])[0]
            # A directory is its count, its entries and the 4-byte next-directory offset.
            if offset + 2 + 12 * count + 4 > (len(tiff) if end is None else end):
                raise ValueError("IFD runs past its container")
            for index in range(count):
                start = offset + 2 + 12 * index
                tag, kind, number = struct.unpack(order + "HHI", tiff[start:start + 8])
                yield tag, kind, number, tiff[start + 8:start + 12]

        def pointer(raw):
            return struct.unpack(order + "I", raw)[0]

        # The Exif IFD pointer must be one LONG (or IFD) value, as the MakerNote must be one blob.
        # Each tag must appear once, and the Exif IFD must not overlap IFD0: otherwise which
        # directory the camera wrote is not provable, so the style is unknown.
        def directory_end(offset):
            return offset + 2 + 12 * struct.unpack(order + "H", tiff[offset:offset + 2])[0] + 4

        ifd0 = pointer(tiff[4:8])
        pointers = [(kind, number, pointer(raw)) for tag, kind, number, raw in entries(ifd0)
                    if tag == 0x8769]
        if len(pointers) != 1 or pointers[0][:2] not in ((4, 1), (13, 1)):
            return None
        exif_ifd = pointers[0][2]
        makers = [(kind, number, pointer(raw)) for tag, kind, number, raw in entries(exif_ifd)
                  if tag == 0x927C]
        if len(makers) != 1 or (exif_ifd < directory_end(ifd0) and ifd0 < directory_end(exif_ifd)):
            return None
        kind, length, maker = makers[0]
        # The MakerNote must be an UNDEFINED blob that fits the segment, and its directory must
        # fit the MakerNote's own declared length.
        if (kind != 7 or length < 14 or maker + length > len(tiff)
                or tiff[maker:maker + 12] not in _SONY_MAKERNOTE_HEADERS):
            return None
        style = effect = None
        seen = set()
        for tag, kind, number, raw in entries(maker + 12, end=maker + length):
            if tag in (0xB020, 0x200E):
                if tag in seen:
                    raise ValueError("a picture-style tag appears twice")
                seen.add(tag)
            if tag == 0xB020 and kind == 2 and 0 < number <= 64:
                start = None if number <= 4 else pointer(raw)
                if start is not None and not (maker <= start and start + number <= maker + length):
                    raise ValueError("style value lies outside the MakerNote")
                value = raw[:number] if start is None else tiff[start:start + number]
                text = value.rstrip(b"\x00")          # NUL padding after the string is allowed
                if value[-1:] != b"\x00" or not text or not all(32 <= byte < 127 for byte in text):
                    raise ValueError("style is not one NUL-terminated printable ASCII string")
                style = text.decode("ascii")
            elif tag == 0x200E and kind == 3 and number == 1:
                effect = struct.unpack(order + "H", raw[:2])[0]
        if style is None and effect is None:
            return None
        if effect in _MONOCHROME_EFFECTS:
            return {"name": _MONOCHROME_EFFECTS[effect], "monochrome": True}
        return {"name": style, "monochrome": style in _MONOCHROME_STYLES}
    except (OSError, struct.error, IndexError, ValueError, UnicodeDecodeError):
        return None


def _anchor_check_abstained() -> dict:
    return {"motion_anchor_checked_frames": [], "motion_anchor_unchecked_frames": [],
            "motion_anchor_detected_fraction": 0.0, "motion_anchor_rejected_fraction": 0.0,
            "motion_anchor_rejected_frames": {}}


def _hold_brackets_to_anchor(files: list[Path], shape: tuple[int, int, int], tone_idx: int,
                             means: list[float]) -> dict:
    """Rewrite, in each bracket's staged copy, what contradicts the anchor inside the recovery
    region. Returns the evidence; see the comment above."""
    report = _anchor_check_abstained()
    others = [index for index in range(len(files)) if index != tone_idx]
    if not others or len(files) > 16:
        return report
    region, joints = _anchor_region_joints(files, shape, tone_idx, others, means)
    region_total = int(np.count_nonzero(region))
    if region_total == 0:
        return report
    tables, clip_bounds = {}, {}
    for index in others:
        brighter = means[index] >= means[tone_idx]
        table = [_pair_difference_table(joints[index][channel], brighter, signed=True)
                 if joints[index][channel].sum() >= _MOTION_MIN_OVERLAP else None for channel in range(3)]
        if any(channel_table is None for channel_table in table):
            report["motion_anchor_unchecked_frames"].append(index)
            continue
        report["motion_anchor_checked_frames"].append(index)
        tables[index] = table
        if not brighter:
            # Per channel, the least this darker bracket shows where the anchor is clipped: the
            # fitted relation at the anchor's clip level (anchor on the rows as the brighter one).
            clip_bounds[index] = [int(np.ceil(_exposure_lut(joints[index][channel].T)[0][_DEGHOST_CLIP_HIGH]))
                                  for channel in range(3)]
    del joints
    rejected = np.zeros(shape[:2], dtype=bool)
    detected = np.zeros(shape[:2], dtype=bool)
    weak_full = np.zeros(shape[:2], dtype=np.uint8)
    seed_full = np.zeros(shape[:2], dtype=np.uint8)
    for index, table in tables.items():
        darker = means[index] < means[tone_idx]
        weak_full.fill(0)
        seed_full.fill(0)
        channel_hists = np.zeros((2, 3, 256), dtype=np.int64)
        # This bracket's values wherever the anchor clips a channel and the two agree: what the
        # bracket shows behind a blown anchor when nothing is in the way.
        behind_clip = np.zeros((3, 256), dtype=np.int64)
        for bounds, dest, interior in _tiles(shape):
            inside = region[dest]
            if not inside.any():
                continue
            anchor_tile = _tile_from_file(files[tone_idx], shape, bounds)[interior]
            tile = _tile_from_file(files[index], shape, bounds)[interior]
            # Clipped values on either side are intervals in each channel's table, so no pixel of
            # the region is excluded: a blown anchor is exactly where the recovery takes content.
            # The disagreement is the median of the three channels' signed disagreements: it
            # counts where at least two channels disagree, the same way, by at least that much. A
            # static colour agrees in each channel's own relation (and a clipped channel agrees as
            # an interval), whatever gray makes of it; a subject in front of a blown window is
            # darker in at least two channels even in a saturated shirt.
            signed = np.stack([table[channel][tile[:, :, channel], anchor_tile[:, :, channel]]
                               for channel in range(3)])
            # In the anchor's blown core its value is only a lower bound (the finish replaces it),
            # so there only a bracket reading darker than it disagrees (positive is darker). A
            # darker bracket reads brighter than its exposure allows only where the anchor clipped,
            # shown or hidden, so for a darker bracket that holds everywhere.
            if darker:
                np.maximum(signed, 0., out=signed)
            else:
                np.maximum(signed, 0., out=signed, where=_core_of(anchor_tile)[None, :, :])
            # At a saturated anchor colour the camera's colour processing (matrix, saturation,
            # clipping hidden under it), not exposure, decides how the channels relate, so the
            # anchor gives no evidence there; it speaks where its colour is near neutral. So it is
            # at a coloured highlight: the anchor clips some channels but not all, and a darker
            # bracket clips one of those too. A colour that saturates even with less light is a
            # light source whose other channels were rendered after the clip (a green lamp,
            # 204,255,205 against 10,255,11). Against glass the anchor shows fully blown, a
            # subject's clipped channel says nothing, and the median of the three decides.
            difference = np.abs(np.median(signed, axis=0))
            silent = ~_near_neutral(anchor_tile)
            if darker:
                anchor_clipped = anchor_tile >= _DEGHOST_CLIP_HIGH
                partly = np.any(anchor_clipped, axis=2) & ~np.all(anchor_clipped, axis=2)
                silent |= partly & np.any((tile >= _DEGHOST_CLIP_HIGH) & anchor_clipped, axis=2)
            difference[silent] = 0.
            weak = inside & (difference > HDR_MOTION_DIFF)
            weak_full[dest] = weak
            seed_full[dest] = inside & (difference > HDR_MOTION_SEED)
            steady = inside & _channels_unclipped(anchor_tile) & _channels_unclipped(tile) & ~weak
            for channel in range(3):
                channel_hists[0, channel] += np.bincount(tile[:, :, channel][steady], minlength=256)
                channel_hists[1, channel] += np.bincount(anchor_tile[:, :, channel][steady], minlength=256)
                # Only where this channel itself agrees: a coloured subject can disagree in one
                # channel without reaching the median.
                behind_clip[channel] += np.bincount(
                    tile[:, :, channel][inside & ~weak & (np.abs(signed[channel]) <= HDR_MOTION_DIFF)
                                        & (anchor_tile[:, :, channel] >= _DEGHOST_CLIP_HIGH)],
                    minlength=256)
        # Registration is a whole-pixel translation, so a static high-contrast edge can disagree
        # along a line a few pixels wide; a subject is far wider than that.
        cv2.morphologyEx(weak_full, cv2.MORPH_OPEN, _ANCHOR_MIN_WIDTH_KERNEL, dst=weak_full)
        np.bitwise_and(seed_full, weak_full, out=seed_full)
        moving = _coherent_binary(weak_full, seed_full)
        if not moving.any():
            continue
        detected |= moving.astype(bool)
        grown = cv2.dilate(moving, np.ones((HDR_MOTION_GROW, HDR_MOTION_GROW), np.uint8))
        del moving
        luts = [_cdf_lut(channel_hists[1, channel], channel_hists[0, channel]) for channel in range(3)]
        for channel, lut in enumerate(luts):
            # The histogram map is fitted where both are unclipped, so it sends a clipped anchor
            # value to the bracket's top steady level: a grey patch in a brighter bracket (which
            # is clipped there itself) and the bare lower bound in a darker one. Use what this
            # bracket shows behind the anchor's clip elsewhere, never below that bound; a
            # brighter bracket is clipped wherever the anchor is.
            if means[index] >= means[tone_idx]:
                lut[_DEGHOST_CLIP_HIGH:] = 255
                continue
            fill = max(int(lut[_DEGHOST_CLIP_HIGH]), clip_bounds[index][channel])
            if behind_clip[channel].sum() >= _MOTION_LUT_MIN_SAMPLES:
                cumulative = np.cumsum(behind_clip[channel])
                fill = max(fill, int(np.searchsorted(cumulative, cumulative[-1] / 2.)))
            lut[_DEGHOST_CLIP_HIGH:] = min(fill, 255)
        written = _rewrite_from(files, shape, index, tone_idx, grown, region, luts, rejected)
        del grown
        report["motion_anchor_rejected_frames"][str(index)] = written / region_total
    report["motion_anchor_detected_fraction"] = float(np.count_nonzero(detected & region)) / region_total
    report["motion_anchor_rejected_fraction"] = float(np.count_nonzero(rejected)) / region_total
    return report


def hdr_merge(paths: list[str | Path], deghost: bool = True,
              tonality: float = HDR_TONALITY_STRENGTH,
              recover: bool = True, report: dict | None = None) -> np.ndarray:
    """The HDR photograph alone; see ``hdr_merge_with_report`` for the algorithm and verdict.
    A ``report`` dict, when given, receives that verdict and the motion evidence."""
    image, verdict = hdr_merge_with_report(paths, deghost=deghost, tonality=tonality, recover=recover)
    if report is not None:
        report.update(verdict)
    return image


def _clipped_fraction(path: Path, shape: tuple[int, int, int]) -> float:
    """Share of a staged frame the recovery finish would treat as blown (LAB L above
    ``HDR_RECOVERY_HIGHLIGHT``), counted tile by tile so memory stays bounded."""
    clipped = 0
    for bounds, _dest, interior in _tiles(shape):
        lightness = cv2.cvtColor(_tile_from_file(path, shape, bounds)[interior], cv2.COLOR_BGR2LAB)[:, :, 0]
        clipped += int(np.count_nonzero(lightness > HDR_RECOVERY_HIGHLIGHT))
    return clipped / float(shape[0] * shape[1])


def hdr_merge_with_report(paths: list[str | Path], deghost: bool = True,
                          tonality: float = HDR_TONALITY_STRENGTH,
                          recover: bool = True) -> tuple[np.ndarray, dict]:
    """Bounded, full-resolution JPEG exposure fusion with optional motion masking.

    Algorithm ``disk-tiled-laplacian-v4``: thumbnail MTB translation, Mertens-family
    fusion carried to full pyramid depth (tiles supply the fine levels, a whole-frame
    coarse fusion supplies the levels a tile cannot reach; see ``_coarse_frames``),
    global histogram-matched deghosting, then by default a recovery finish that keeps
    the metered bracket as the photograph and substitutes fused content only where that
    bracket is blown (`recover`). With ``recover=False`` the whole frame is the fused
    render, corrected by a global monotone tonality curve (`tonality`, 0 disables it). No response curve, calibrated EV or RAW radiance recovery is
    inferred. Geometry follows the first input; least-clipped exposure anchors
    moving subjects. Originals are never written. Output is deterministically
    clipped/truncated to uint8. On one machine and library build, the same inputs in the same
    order give byte-identical output in every process, because brackets are accumulated in a
    fixed order here; other CPUs or OpenCV builds may round differently. Do not hand the blend to
    ``cv2.createMergeMertens``; it sums per-image pyramids from parallel workers, which made
    the pre-v4 merge vary run to run (``test_hdr_determinism.py``).

    With recovery and deghost on and three or more brackets, brackets that show a moving subject inside
    the recovery region are repaired before fusing (``_repair_recovery_motion``). A ``report``
    dict, when given, receives that check's evidence: the reference bracket, the share of the
    recovery region it could check, the share detected as moving and the share rewritten (the
    grown, feathered repair), in total and per bracket. Then every bracket is held to the anchor
    itself (``_hold_brackets_to_anchor``), which needs no corroboration: what contradicts the
    photograph inside the region is rewritten from it (``motion_anchor_*`` evidence).

    With recovery on, when the recovery anchor (the bracket exposed closest to mid-grey, which
    the finish keeps as the photograph) clips less than ``HDR_NOTHING_TO_RECOVER_FRACTION`` of
    its pixels, that bracket is returned unmerged ("nothing to recover"). The anchor is chosen
    from the pixels, not from EV metadata: in a dark scene it can be a +EV bracket rather than
    the 0 EV one. The report records the verdict (``recovery``: ``nothing_to_recover`` /
    ``recovered`` / ``off``), ``anchor_index``, its measured clipped share, and a ``summary``.
    The nothing-to-recover return comes first: with nothing to recover there is nothing for the
    motion repair to protect, so it never runs then.
    """
    with _staged_stack(paths, hdr=True) as (files, shape, histograms):
        levels = min(_STACK_PYRAMID_LEVELS, max(0, int(np.log2(min(shape[:2]))) - 1))
        deghost = bool(deghost) and len(files) > 1
        # The tonality/recovery anchor is NOT the deghost anchor. `ref_idx` is the
        # least-clipped bracket, which for a daylight room is the darkest one; anchoring on it
        # darkens rather than restores. Anchor on the bracket the camera exposed best, i.e.
        # whose mean luma sits closest to mid-grey. That is the photograph the camera would
        # have handed back, and the recovery finish keeps it.
        means = [float(np.dot(hist, np.arange(256)) / max(np.sum(hist), 1)) for hist in histograms]
        tone_idx = min(range(len(histograms)), key=lambda i: (abs(means[i] - 127.5), i))
        recover = bool(recover) and len(files) > 1
        clipped_fraction = _clipped_fraction(files[tone_idx], shape)
        report = {"recovery": "off", "anchor_index": tone_idx,
                  "clipped_fraction": clipped_fraction,
                  "clipped_threshold": HDR_NOTHING_TO_RECOVER_FRACTION,
                  "clip_level": HDR_RECOVERY_HIGHLIGHT,
                  "summary": "Recovery finish off: the whole frame is the fused render."}
        clipped_text = (f"the anchor bracket (frame {tone_idx + 1} of {len(files)}, exposed closest to "
                        f"mid-grey) clipped {clipped_fraction * 100:.2f} % of its pixels "
                        f"(threshold {HDR_NOTHING_TO_RECOVER_FRACTION * 100:g} %)")
        if recover and clipped_fraction < HDR_NOTHING_TO_RECOVER_FRACTION:
            # The camera's own frame: the staged snapshot that was measured, so the input is
            # decoded once and it is registered to the first input like every HDR output. It
            # is exactly the frame the recovery finish keeps outside the blown region (#89).
            report.update(recovery="nothing_to_recover",
                          summary=f"Nothing to recover: {clipped_text}, so it is the photograph.")
            return np.array(np.memmap(files[tone_idx], dtype=np.uint8, mode="r", shape=shape)), report
        if recover:
            report.update(recovery="recovered",
                          summary=f"Applied highlight recovery: {clipped_text}.")
        if bool(recover) and bool(deghost) and len(files) > 2:
            motion = _repair_recovery_motion(files, shape, tone_idx, means)
            # Owner decision (2026-09-29): in a monochrome picture style the colour evidence the
            # anchor check relies on is gone (a clipped colour renders as an ordinary grey), so it
            # abstains there. An unreadable or non-Sony style keeps the check.
            style = _anchor_picture_style(paths[tone_idx])
            if style is not None and style["monochrome"]:
                motion.update(_anchor_check_abstained(), motion_anchor_status="abstained_monochrome",
                              motion_anchor_picture_style=style["name"])
            else:
                motion.update(_hold_brackets_to_anchor(files, shape, tone_idx, means),
                              motion_anchor_status="checked",
                              motion_anchor_picture_style=None if style is None else style["name"])
            report.update(motion)
            # The deghost below matches every bracket to its reference through histograms; a
            # repaired bracket's must be its repaired pixels, or the stale curve reads the
            # whole region as moving and pastes the reference over it.
            for index in sorted(set(map(int, motion["motion_rejected_frames"]))
                                | set(map(int, motion["motion_anchor_rejected_frames"]))):
                histograms[index] = np.zeros(256, dtype=np.float64)
                for _bounds, dest, _interior in _tiles(shape):
                    histograms[index] += _gray_hist(_tile_from_file(files[index], shape, (
                        dest[0].start, dest[0].stop, dest[1].start, dest[1].stop)))
        ref_idx = min(range(len(histograms)), key=lambda index: (
            histograms[index][0] + histograms[index][255],
            abs(float(np.dot(histograms[index], np.arange(256)) / np.sum(histograms[index])) - 127.5),
            index))
        cdfs = [np.cumsum(hist) / max(np.sum(hist), 1) for hist in histograms]
        luts = [np.searchsorted(cdfs[ref_idx], cdf).clip(0, 255).astype(np.uint8) for cdf in cdfs]
        # Allocated after the motion repair so the two full-frame working sets never overlap.
        output = np.empty(shape, dtype=np.uint8)
        # Blend the brackets at the scales a tile cannot reach: fuse the whole frame at
        # 1/2**levels with a pyramid deep enough to reach it, and hand each tile that
        # result as its pyramid base. The composite is equivalent to a full-depth
        # whole-image fusion while still never holding a full-resolution float frame.
        scale = 1 << levels
        coarse_base = None
        if len(files) > 1:
            coarse = _coarse_frames(files, shape, levels)
            if coarse is not None:
                coarse_images, coarse_weights = coarse
                coarse_base = _fuse_pyramid(
                    coarse_images, coarse_weights,
                    max(0, int(np.log2(min(coarse_images[0].shape[:2]))) - 1))
        # Pass 1: fuse each tile and accumulate the pointwise disagreement binaries. The
        # coherence/seed decision is deferred to a single whole-frame pass so its connectivity
        # is global and no moving region can be cut at a tile boundary.
        weak_full = np.zeros(shape[:2], dtype=np.uint8) if deghost else None
        seed_full = np.zeros(shape[:2], dtype=np.uint8) if deghost else None
        for bounds, dest, interior in _tiles(shape):
            tiles = [_tile_from_file(path, shape, bounds) for path in files]
            base = None
            if coarse_base is not None:
                by, _, bx, _ = bounds
                height_t, width_t = tiles[0].shape[:2]
                base = coarse_base[by // scale: by // scale + height_t // scale,
                                   bx // scale: bx // scale + width_t // scale]
            fused = _fuse_tile(tiles, levels, base=base)
            output[dest] = np.clip(fused[interior] * 255., 0, 255).astype(np.uint8)
            if deghost:
                reference = tiles[ref_idx]
                for index, tile in enumerate(tiles):
                    if index != ref_idx:
                        weak, seed = _moving_disagreement(tile, reference, luts[index])
                        np.maximum(weak_full[dest], weak[interior], out=weak_full[dest])
                        np.maximum(seed_full[dest], seed[interior], out=seed_full[dest])
        if deghost:
            # Pass 2: global mask (spatial coherence + seed), then paste the reference over the
            # fused output only where the mask fires. Re-reads just the reference frame's tiles.
            mask_full = _grow_and_feather(_coherent_binary(weak_full, seed_full))
            for bounds, dest, interior in _tiles(shape):
                reference = _tile_from_file(files[ref_idx], shape, bounds)
                weight = mask_full[dest][:, :, None]
                blended = (output[dest].astype(np.float32) * (1. - weight)
                           + reference[interior].astype(np.float32) * weight)
                output[dest] = np.clip(blended, 0, 255).astype(np.uint8)
        if recover:
            # Two bounded passes. First the paired histograms, over the SAME pixels: the
            # anchor's own values and the fused values wherever the anchor is well exposed.
            anchor_hist = np.zeros(256, dtype=np.int64)
            fused_hist = np.zeros(256, dtype=np.int64)
            for bounds, dest, interior in _tiles(shape):
                anchor = cv2.cvtColor(
                    _tile_from_file(files[tone_idx], shape, bounds)[interior],
                    cv2.COLOR_BGR2LAB)[:, :, 0]
                fused = cv2.cvtColor(output[dest], cv2.COLOR_BGR2LAB)[:, :, 0]
                good = (anchor < HDR_RECOVERY_HIGHLIGHT) & (anchor > 15)
                anchor_hist += np.bincount(anchor[good], minlength=256)
                fused_hist += np.bincount(fused[good], minlength=256)
            lut = _recovery_lut(fused_hist, anchor_hist, HDR_RECOVERY_CEILING)
            # Then the blend. The weight is computed on the halo tile and feathered there, so
            # a tile interior does not depend on where the tile boundary fell.
            # The anchor enters the blend as its own BGR pixels, never through LAB: an 8-bit LAB
            # round trip changes 88 % of colours (up to 27 code values), and running it on the
            # anchor re-rendered every pixel the finish exists to keep (2026-09-27). Blending in
            # BGR makes a zero weight give the anchor exactly and a full weight give the curved
            # fused pixel exactly, with no step beyond truncation where the feather's support ends.
            for bounds, dest, interior in _tiles(shape):
                anchor_bgr = _tile_from_file(files[tone_idx], shape, bounds)
                weight = _recovery_weight(anchor_bgr)[interior][:, :, None]
                anchor = anchor_bgr[interior].astype(np.float32)
                fused = cv2.cvtColor(output[dest], cv2.COLOR_BGR2LAB)
                fused[:, :, 0] = cv2.LUT(fused[:, :, 0], lut)
                fused = cv2.cvtColor(fused, cv2.COLOR_LAB2BGR).astype(np.float32)
                blended = anchor + (fused - anchor) * weight
                output[dest] = np.clip(blended, 0, 255).astype(np.uint8)
        elif tonality > 0 and len(files) > 1:
            # Histogram of the FINAL output, accumulated over the whole image so the curve
            # below is tile-independent (and reflects any pasted reference pixels).
            fused_hist = np.zeros(256, dtype=np.int64)
            for _bounds, dest, _interior in _tiles(shape):
                fused_hist += np.bincount(
                    cv2.cvtColor(output[dest], cv2.COLOR_BGR2GRAY).ravel(), minlength=256)
            _apply_tonality(output, _tonality_lut(fused_hist, histograms[tone_idx], tonality))
        return output, report


def _deghost_reference_index(count: int) -> int:
    """Reference frame for the deghosted night average: the temporal middle.

    Every registered frame shares frame 0's geometry, so the reference only decides
    WHICH single frame a moving subject is taken from. The middle frame minimises the
    worst-case time gap to any other frame, so the subject's reference position is
    its median position and the union of disagreements (the area that forgoes the
    N-frame noise gain) is smallest on average. A "sharpest frame" rule was rejected:
    at night ISO a Laplacian measure is dominated by noise, and frames 1..N-1 are
    resampled by registration while frame 0 is not, which would bias the choice
    toward frame 0 regardless of the subject. Even counts take the earlier of the
    two middle frames, so N=2 keeps the unresampled anchor.
    """
    return (count - 1) // 2


def long_exposure_with_report(paths: list[str | Path], *, deghost: bool = True) -> tuple[np.ndarray, dict]:
    """Bounded aligned JPEG code-value average plus a motion report.

    ``deghost=False`` is the plain average ``long_exposure`` returns. With
    ``deghost=True`` the HDR moving-pixel mask (matched-luma residual OR chroma
    distance, grown and feathered; clip suppression off because the frames share
    one exposure) is evaluated against the middle frame, and moving pixels take the
    reference frame's value instead of the average, so a subject that moved between
    frames appears once. Those pixels keep single-frame noise; static pixels keep
    the full N-frame gain. Camera-scene gaps between frames stay gaps.

    Returns ``(image, report)``; the report carries ``deghost``, ``reference_index``,
    ``moving_fraction`` (mean feathered mask weight, i.e. the effective fraction of the
    image taken from the reference) and ``mask`` (full-resolution uint8, 255 = fully
    deghosted; ``None`` without deghost). Global histogram matching to the reference
    is a safety net against small exposure drift, not the motion test itself.
    """
    with _staged_stack(paths, mask_output=deghost) as (files, shape, histograms):
        output = np.empty(shape, dtype=np.uint8)
        deghost = bool(deghost) and len(files) > 1
        ref_idx = _deghost_reference_index(len(files)) if deghost else None
        luts = None
        # Pass 1: write the plain N-frame average and accumulate the pointwise disagreement
        # binaries per tile. The coherence/seed decision is deferred to one whole-frame pass
        # (below) so its connectivity is global and the mover cannot be cut at a tile seam.
        weak_full = np.zeros(shape[:2], dtype=np.uint8) if deghost else None
        seed_full = np.zeros(shape[:2], dtype=np.uint8) if deghost else None
        if deghost:
            cdfs = [np.cumsum(hist) / max(np.sum(hist), 1) for hist in histograms]
            luts = [np.searchsorted(cdfs[ref_idx], cdf).clip(0, 255).astype(np.uint8) for cdf in cdfs]
        for bounds, dest, interior in _tiles(shape):
            total = None
            # Only one halo tile per source is materialized at a time (plus the reference
            # tile), as before.
            reference = _tile_from_file(files[ref_idx], shape, bounds) if deghost else None
            for index, path in enumerate(files):
                tile = reference if index == ref_idx else _tile_from_file(path, shape, bounds)
                if total is None:
                    total = tile.astype(np.float32)
                else:
                    total += tile
                if deghost and index != ref_idx:
                    weak, seed = _moving_disagreement(tile, reference, luts[index],
                                                      suppress_clipped=False)
                    np.maximum(weak_full[dest], weak[interior], out=weak_full[dest])
                    np.maximum(seed_full[dest], seed[interior], out=seed_full[dest])
            output[dest] = np.clip(total[interior] / len(files), 0, 255).astype(np.uint8)
        if not deghost:
            return output, {"deghost": False, "reference_index": None,
                            "moving_fraction": 0.0, "mask": None}
        # Pass 2: global mask (spatial coherence + seed), then take moving pixels from the
        # reference instead of the average. Re-reads only the reference frame's tiles.
        mask_full = _grow_and_feather(_coherent_binary(weak_full, seed_full))
        mask_out = np.zeros(shape[:2], dtype=np.uint8)
        for bounds, dest, interior in _tiles(shape):
            reference = _tile_from_file(files[ref_idx], shape, bounds)
            weight = mask_full[dest][:, :, None]
            blended = (output[dest].astype(np.float32) * (1. - weight)
                       + reference[interior].astype(np.float32) * weight)
            output[dest] = np.clip(blended, 0, 255).astype(np.uint8)
            mask_out[dest] = np.clip(mask_full[dest] * 255. + .5, 0, 255).astype(np.uint8)
        report = {"deghost": True, "reference_index": ref_idx,
                  "moving_fraction": float(mask_full.mean()), "mask": mask_out}
        return output, report


def long_exposure(paths: list[str | Path], *, deghost: bool = False) -> np.ndarray:
    """Bounded aligned JPEG code-value average (not a linear RAW long exposure).

    Static random noise is averaged; motion becomes trails and camera transport
    gaps remain gaps. Night sessions share this implementation and add
    ``deghost=True`` (see ``long_exposure_with_report``); the default keeps
    the trails a long-exposure look wants.
    """
    return long_exposure_with_report(paths, deghost=deghost)[0]


def _sharpness_map(image: np.ndarray) -> np.ndarray:
    """Per-pixel sharpness as the smoothed magnitude of the Laplacian.

    The Laplacian responds to local contrast (edges/detail); blurring its
    magnitude turns it into a stable per-region focus measure.
    """
    gray = cv2.cvtColor(image, cv2.COLOR_BGR2GRAY)
    laplacian = cv2.Laplacian(gray, cv2.CV_32F)
    sharpness = np.abs(laplacian)
    return cv2.blur(sharpness, (_FOCUS_BLUR_KERNEL, _FOCUS_BLUR_KERNEL))


def focus_stack(paths: list[str | Path]) -> np.ndarray:
    """Bounded sharpest-region selection; this does not actuate camera focus.

    Selection is per pixel, so on a surface that is equally (un)sharp in every frame the
    winner is decided by sensor noise, and a hard argmax then takes the pixelwise maximum of
    that noise: grain appears on flat surfaces that no input contains (measured 2026-09-17,
    +30-38% on a flat cabinet face). The same failure family as the HDR fusion weights. Two
    guards: the sharpness map is smoothed so the choice is regional rather than per pixel, and
    where no frame is meaningfully sharper the frames are averaged instead of selected, which
    lowers noise rather than amplifying it.
    """
    with _staged_stack(paths, mode="focus") as (files, shape, _):
        output = np.empty(shape, dtype=np.uint8)
        for bounds, dest, interior in _tiles(shape):
            tiles = [_tile_from_file(path, shape, bounds) for path in files]
            maps = [_sharpness_map(tile) for tile in tiles]
            stacked = np.stack(maps, axis=0)
            best = stacked.max(axis=0)
            spread = best - stacked.min(axis=0)
            # Confident only where one frame is clearly sharper than the flattest one.
            confident = (spread > _FOCUS_DECISION_FLOOR)[:, :, None]
            selected = np.zeros(tiles[0].shape, dtype=np.float32)
            for tile, sharpness in zip(tiles, maps):
                selected += np.where(sharpness[:, :, None] >= best[:, :, None],
                                     tile.astype(np.float32), 0.0)
                # Ties would double-count, so normalise by how many frames hit the maximum.
            winners = (stacked >= best[None, :, :]).sum(axis=0)[:, :, None].astype(np.float32)
            selected /= np.maximum(winners, 1.0)
            averaged = np.mean([tile.astype(np.float32) for tile in tiles], axis=0)
            merged = np.where(confident, selected, averaged)
            output[dest] = np.clip(merged, 0, 255).astype(np.uint8)[interior]
        return output


def crowd_remove(paths: list[str | Path]) -> np.ndarray:
    """Bounded aligned temporal median; background must appear in >50% of frames."""
    with _staged_stack(paths, mode="crowd") as (files, shape, _):
        output = np.empty(shape, dtype=np.uint8)
        for bounds, dest, interior in _tiles(shape):
            tiles = np.stack([_tile_from_file(path, shape, bounds) for path in files])
            median = np.median(tiles, axis=0, overwrite_input=True)
            output[dest] = np.clip(median[interior], 0, 255).astype(np.uint8)
        return output


def crowd_remove_guided(paths: list[str | Path], background_masks: list[str | Path], *,
                        registered: bool = False, minimum_observations: int = 1):
    """Explicit-mask alternative returning image plus mandatory uncertainty maps.

    The default crowd_remove median remains unchanged. This path can use minority
    background observations only when caller-provided masks identify them. Inputs
    must already be registered; never-observed pixels are not reconstructed.
    """
    from crowd_recovery import recover_background
    return recover_background(paths, background_masks, registered=registered,
                              minimum_observations=minimum_observations)
