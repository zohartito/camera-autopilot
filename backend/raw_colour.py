"""Camera as-shot white balance and RGB→XYZ matrix to linear sRGB.

Exact colour math. Missing camera metadata is an error; this module does not
guess daylight multipliers or a generic matrix. The W3C XYZ→sRGB matrix only
encodes the output primaries after the camera matrix has been applied.
"""
from __future__ import annotations

import math
import os
import struct
from pathlib import Path

# W3C CSS Color 4, sRGB primaries / D65, XYZ -> linear sRGB.
XYZ_D65_TO_SRGB = (
    (12831 / 3959, -329 / 214, -1974 / 3959),
    (-851781 / 878810, 1648619 / 878810, 36519 / 878810),
    (705 / 12673, -2585 / 12673, 705 / 667),
)
TIFF_DESCRIPTION = b'Camera Autopilot linear sRGB primaries; 16-bit unsigned RGB; not DNG\x00'


def require_camera_whitebalance(values) -> list[float]:
    try:
        red, green, blue = float(values[0]), float(values[1]), float(values[2])
    except (TypeError, ValueError, IndexError) as exc:
        raise ValueError('Camera white balance is missing.') from exc
    if type(values[0]) is bool or type(values[1]) is bool or type(values[2]) is bool:
        raise ValueError('Camera white balance is missing.')
    if not all(math.isfinite(channel) and channel > 0 for channel in (red, green, blue)):
        raise ValueError('Camera white balance is missing.')
    return [red, green, blue]


def require_rgb_xyz_matrix(values) -> list[list[float]]:
    try:
        rows = [[float(values[i][0]), float(values[i][1]), float(values[i][2])] for i in range(3)]
    except (TypeError, ValueError, IndexError) as exc:
        raise ValueError('Camera colour matrix is missing.') from exc
    for row in rows:
        if any(type(value) is bool for value in row) or not all(math.isfinite(value) for value in row):
            raise ValueError('Camera colour matrix is missing.')
    det = (rows[0][0] * (rows[1][1] * rows[2][2] - rows[1][2] * rows[2][1])
           - rows[0][1] * (rows[1][0] * rows[2][2] - rows[1][2] * rows[2][0])
           + rows[0][2] * (rows[1][0] * rows[2][1] - rows[1][1] * rows[2][0]))
    if abs(det) < 1e-12:
        raise ValueError('Camera colour matrix is missing.')
    return rows


def camera_rgb_to_linear_srgb(camera_rgb, whitebalance, rgb_xyz_matrix):
    """Map demosaiced camera RGB (uint16) to encoded linear sRGB uint16.

    White balance is as-shot, green-normalized. ``rgb_xyz_matrix`` is camera RGB
    to XYZ (column vectors). Output primaries are W3C linear sRGB / D65.
    """
    import numpy as np
    wb = require_camera_whitebalance(whitebalance)
    camera_to_xyz = np.asarray(require_rgb_xyz_matrix(rgb_xyz_matrix), dtype=np.float64)
    combined = (np.asarray(XYZ_D65_TO_SRGB, dtype=np.float64) @ camera_to_xyz).astype(np.float32)
    rgb = np.asarray(camera_rgb)
    if rgb.ndim != 3 or rgb.shape[2] != 3:
        raise ValueError('Camera RGB must be HxWx3.')
    linear = rgb.astype(np.float32) * np.float32(1 / 65535)
    linear *= np.array([wb[0] / wb[1], 1.0, wb[2] / wb[1]], dtype=np.float32)
    linear = linear @ combined.T
    return np.rint(np.clip(linear, 0, 1) * 65535).astype('<u2')


def linear_tiff_pixel_offset() -> int:
    extras = 8 + 2 + 12 * 12 + 4
    return extras + 12 + len(TIFF_DESCRIPTION)


def write_tiff_header(handle, width, height):
    # Baseline uncompressed RGB unsigned 16-bit TIFF, explicitly linear-light.
    description = TIFF_DESCRIPTION
    entries_count = 12
    extras = 8 + 2 + 12 * entries_count + 4
    bits_offset, format_offset, description_offset = extras, extras + 6, extras + 12
    pixels_offset = description_offset + len(description)
    entries = [(256, 4, 1, width), (257, 4, 1, height), (258, 3, 3, bits_offset),
               (259, 3, 1, 1), (262, 3, 1, 2), (270, 2, len(description), description_offset),
               (273, 4, 1, pixels_offset), (274, 3, 1, 1), (277, 3, 1, 3),
               (278, 4, 1, height), (279, 4, 1, width * height * 6), (339, 3, 3, format_offset)]
    handle.write(b'II' + struct.pack('<HIH', 42, 8, entries_count))
    for entry in entries:
        handle.write(struct.pack('<HHII', *entry))
    handle.write(struct.pack('<I', 0) + struct.pack('<HHHHHH', 16, 16, 16, 1, 1, 1) + description)


def write_linear_tiff(path, pixels):
    import numpy as np
    samples = np.asarray(pixels, dtype='<u2')
    if samples.ndim != 3 or samples.shape[2] != 3 or min(samples.shape[0], samples.shape[1]) < 1:
        raise ValueError('Linear TIFF must be HxWx3 uint16.')
    height, width = samples.shape[:2]
    with os.fdopen(os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600), 'wb') as handle:
        write_tiff_header(handle, width, height)
        handle.write(samples.tobytes())
        handle.flush()
        os.fsync(handle.fileno())


def read_linear_rgb_tile(handle, width, top, bottom):
    import numpy as np
    rows = bottom - top
    handle.seek(linear_tiff_pixel_offset() + top * width * 6)
    data = handle.read(rows * width * 6)
    if len(data) != rows * width * 6:
        raise ValueError('Linear TIFF is truncated.')
    return np.frombuffer(data, dtype='<u2').reshape((rows, width, 3)).astype(np.float32) * np.float32(1 / 65535)


def linear_tiff_bytes_for(width: int, height: int) -> int:
    return linear_tiff_pixel_offset() + width * height * 6
