"""Held-out scoreboard of a developed JPEG against a camera JPEG.

Deterministic metrics live here: colour error (CIE76 ΔE), detail retention
(luma Laplacian energy ratio) and highlight handling (clip fractions). No
model is trained or invoked. Private photographs are never stored.
"""
from __future__ import annotations

from pathlib import Path

import numpy as np

from raw_colour import XYZ_D65_TO_SRGB

HIGHLIGHT_THRESHOLD = 250
ALGORITHM = 'develop-compare-v1'
_D65_WHITE = np.array([0.95047, 1.0, 1.08883], dtype=np.float32)
# Row tiles keep peak memory bounded on full-resolution (24-40 MP) twins: every
# intermediate is float32 and no full-frame Lab image is ever materialised.
_TILE_ROWS = 256


def _row_tiles(height: int):
    for top in range(0, height, _TILE_ROWS):
        yield top, min(top + _TILE_ROWS, height)


def _to_linear_srgb(encoded: np.ndarray) -> np.ndarray:
    x = encoded.astype(np.float32) / np.float32(255.0)
    return np.where(x <= np.float32(0.04045), x / np.float32(12.92),
                    ((x + np.float32(0.055)) / np.float32(1.055)) ** np.float32(2.4))


def _xyz_to_lab(xyz: np.ndarray) -> np.ndarray:
    t = xyz / _D65_WHITE
    delta = np.float32(6 / 29)
    f = np.where(t > delta ** 3, np.cbrt(t), t / (3 * delta ** 2) + np.float32(4 / 29))
    return np.stack((116 * f[..., 1] - 16, 500 * (f[..., 0] - f[..., 1]), 200 * (f[..., 1] - f[..., 2])), axis=-1)


def _lab_tile(rgb_rows: np.ndarray, srgb_to_xyz: np.ndarray) -> np.ndarray:
    return _xyz_to_lab(_to_linear_srgb(rgb_rows) @ srgb_to_xyz.T)


def colour_error_delta_e76(developed: np.ndarray, camera: np.ndarray) -> tuple[float, float]:
    developed = np.asarray(developed)
    camera = np.asarray(camera)
    srgb_to_xyz = np.linalg.inv(np.asarray(XYZ_D65_TO_SRGB, dtype=np.float64)).astype(np.float32)
    height, width = developed.shape[0], developed.shape[1]
    deltas = np.empty(height * width, dtype=np.float32)
    cursor = 0
    for top, bottom in _row_tiles(height):
        lab_developed = _lab_tile(developed[top:bottom], srgb_to_xyz)
        lab_camera = _lab_tile(camera[top:bottom], srgb_to_xyz)
        block = np.linalg.norm(lab_developed - lab_camera, axis=-1).reshape(-1)
        deltas[cursor:cursor + block.size] = block
        cursor += block.size
    return float(deltas.mean()), float(np.quantile(deltas, 0.95))


def _laplacian_variance(rgb: np.ndarray) -> float:
    height, width = rgb.shape[0], rgb.shape[1]
    total = height * width
    lap_sum = 0.0
    lap_sqsum = 0.0
    for top, bottom in _row_tiles(height):
        # The Laplacian at interior rows needs one halo row on each side; the
        # image border (row/column 0 and -1) stays zero, exactly as the full-frame
        # form did, and contributes nothing to either accumulator.
        low = max(top - 1, 0)
        high = min(bottom + 1, height)
        band = rgb[low:high]
        y = (0.2126 * band[..., 0] + 0.7152 * band[..., 1] + 0.0722 * band[..., 2]).astype(np.float64)
        first = top if top >= 1 else 1
        last = bottom if bottom <= height - 1 else height - 1
        if last <= first:
            continue
        begin, end = first - low, last - low
        lap = (4 * y[begin:end, 1:-1] - y[begin - 1:end - 1, 1:-1] - y[begin + 1:end + 1, 1:-1]
               - y[begin:end, :-2] - y[begin:end, 2:])
        lap_sum += float(lap.sum())
        lap_sqsum += float((lap * lap).sum())
    mean = lap_sum / total
    return lap_sqsum / total - mean * mean


def detail_retention(developed: np.ndarray, camera: np.ndarray) -> float:
    developed = np.asarray(developed)
    camera = np.asarray(camera)
    camera_energy = _laplacian_variance(camera)
    developed_energy = _laplacian_variance(developed)
    if camera_energy <= 1e-12:
        return 1.0 if developed_energy <= 1e-12 else float('inf')
    return developed_energy / camera_energy


def highlight_handling(developed: np.ndarray, camera: np.ndarray) -> dict:
    developed = np.asarray(developed)
    camera = np.asarray(camera)
    height, width = camera.shape[0], camera.shape[1]
    total = height * width
    camera_clip_count = 0
    developed_clip_count = 0
    held_count = 0
    developed_sum_at_clip = 0.0
    developed_count_at_clip = 0
    for top, bottom in _row_tiles(height):
        cam = camera[top:bottom]
        dev = developed[top:bottom]
        camera_clip = cam.max(axis=2) >= HIGHLIGHT_THRESHOLD
        developed_clip = dev.max(axis=2) >= HIGHLIGHT_THRESHOLD
        camera_clip_count += int(camera_clip.sum())
        developed_clip_count += int(developed_clip.sum())
        if camera_clip.any():
            developed_at_clip = dev[camera_clip]
            held_count += int((developed_at_clip.max(axis=1) < HIGHLIGHT_THRESHOLD).sum())
            developed_sum_at_clip += float(developed_at_clip.sum())
            developed_count_at_clip += int(developed_at_clip.size)
    result = {
        'camera_clip_fraction': camera_clip_count / total,
        'developed_clip_fraction': developed_clip_count / total,
        'developed_holds_where_camera_clips': 1.0,
        'developed_mean_where_camera_clips': None,
    }
    if camera_clip_count:
        result['developed_holds_where_camera_clips'] = held_count / camera_clip_count
        result['developed_mean_where_camera_clips'] = developed_sum_at_clip / developed_count_at_clip
    return result


def score_developed_against_camera(developed_rgb, camera_rgb) -> dict:
    developed = np.asarray(developed_rgb)
    camera = np.asarray(camera_rgb)
    if developed.shape != camera.shape or developed.ndim != 3 or developed.shape[2] != 3:
        raise ValueError('Developed and camera images must be the same RGB shape.')
    mean_e, p95_e = colour_error_delta_e76(developed, camera)
    return {
        'algorithm': ALGORITHM,
        'neural': False,
        'width': int(camera.shape[1]),
        'height': int(camera.shape[0]),
        'colour_error_delta_e76_mean': mean_e,
        'colour_error_delta_e76_p95': p95_e,
        'detail_retention': detail_retention(developed, camera),
        'highlight_handling': highlight_handling(developed, camera),
    }


def score_paths(developed_path: str | Path, camera_path: str | Path) -> dict:
    from image_safety import open_rgb_oriented
    developed = np.asarray(open_rgb_oriented(developed_path))
    camera = np.asarray(open_rgb_oriented(camera_path))
    return score_developed_against_camera(developed, camera)
