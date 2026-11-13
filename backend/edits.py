"""Bounded JPEG editing. Save a new image and leave the source untouched."""
from __future__ import annotations

import math
import os
from pathlib import Path
import uuid

from PIL import ImageOps
from image_safety import open_image, source_bytes

MAX_IMAGE_BYTES = 32 * 1024 * 1024
MAX_IMAGE_PIXELS = 40_000_000


def edit_image(shots_dir: Path, image: str, exposure_ev: float = 0.0,
               crop: list[float] | None = None) -> dict:
    """Return new image metadata. Crop is normalized [left, top, right, bottom]."""
    root = Path(shots_dir).resolve(strict=True)
    if not image or Path(image).name != image or Path(image).suffix.lower() not in {'.jpg', '.jpeg'}:
        raise ValueError('Select a saved JPEG image.')
    source = root / image
    if source.is_symlink() or not source.is_file() or source.resolve().parent != root:
        raise ValueError('Saved image not found.')
    if source.stat().st_size > MAX_IMAGE_BYTES:
        raise ValueError('Image exceeds the 32 MiB editing limit.')
    if isinstance(exposure_ev, bool) or not math.isfinite(exposure_ev) or not -3 <= exposure_ev <= 3:
        raise ValueError('Exposure must be between -3 and +3 EV.')
    if crop is not None:
        if len(crop) != 4 or any(isinstance(v, bool) or not math.isfinite(v) for v in crop):
            raise ValueError('Crop needs four finite normalized coordinates.')
        left, top, right, bottom = crop
        if not (0 <= left < right <= 1 and 0 <= top < bottom <= 1):
            raise ValueError('Crop must be inside the image with positive area.')
    if not source_bytes(source).startswith(b'\xff\xd8\xff'):
        raise ValueError('Editing supports JPEG images only.')
    with open_image(source) as opened:
        result = ImageOps.exif_transpose(opened).convert('RGB')
    if crop is not None:
        bounds = tuple(round(v * (result.width if i % 2 == 0 else result.height)) for i, v in enumerate(crop))
        if bounds[2] - bounds[0] < 8 or bounds[3] - bounds[1] < 8:
            raise ValueError('Crop must retain at least 8 × 8 pixels.')
        result = result.crop(bounds)
    # Convert sRGB channel values to linear light before exposure adjustment.
    if exposure_ev:
        def channel(value):
            encoded = value / 255
            linear = encoded / 12.92 if encoded <= .04045 else ((encoded + .055) / 1.055) ** 2.4
            linear = min(1.0, linear * 2 ** exposure_ev)
            encoded = 12.92 * linear if linear <= .0031308 else 1.055 * linear ** (1 / 2.4) - .055
            return round(encoded * 255)
        result = result.point([channel(v) for v in range(256)] * 3)
    filename = f'edit_{uuid.uuid4().hex}.jpg'
    temporary = root / (filename + '.tmp')
    destination = root / filename
    try:
        result.save(temporary, format='JPEG', quality=95, optimize=True)
        os.chmod(temporary, 0o600)
        temporary.replace(destination)
    finally:
        temporary.unlink(missing_ok=True)
    return {'image': filename, 'source_image': image, 'exposure_ev': exposure_ev,
            'crop': crop, 'width': result.width, 'height': result.height,
            'summary': 'Edited copy saved', 'why': 'Exposure/crop edit; original preserved. JPEG edits cannot recover clipped highlights.'}
