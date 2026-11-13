"""Bound camera-image bytes, headers, pixels, and decoded RGB before use."""
from __future__ import annotations

from io import BytesIO
from pathlib import Path

from PIL import Image

MAX_IMAGE_SOURCE_BYTES = 64 * 1024 * 1024
MAX_IMAGE_PIXELS = 40_000_000
MAX_DECODED_RGB_BYTES = 120_000_000


def _decoded_byte_estimate(image: Image.Image) -> int:
    """Conservative in-memory size for the source mode before any conversion.

    Pillow opens lazily, so this is deliberately evaluated after header parsing
    and before ``load()``, ``copy()``, or ``convert()``.  Unknown modes fail
    closed rather than guessing that their decoder is cheap.
    """
    bytes_per_pixel = {
        "1": 1,
        "L": 1,
        "P": 1,
        "LA": 2,
        "PA": 2,
        "RGB": 3,
        "YCbCr": 3,
        "RGBA": 4,
        "RGBX": 4,
        "CMYK": 4,
        "I;16": 2,
        "I;16B": 2,
        "I;16L": 2,
        "I": 4,
        "F": 4,
    }.get(image.mode)
    if bytes_per_pixel is None:
        raise ValueError(f"unsupported camera image mode: {image.mode}")
    return image.width * image.height * bytes_per_pixel


def source_bytes(path: str | Path) -> bytes:
    path = Path(path)
    size = path.stat().st_size
    if size < 1 or size > MAX_IMAGE_SOURCE_BYTES:
        raise ValueError("camera image source exceeds byte budget")
    with path.open("rb") as handle:
        data = handle.read(size + 1)
    if len(data) != size:
        raise ValueError("camera image source changed during read")
    return data


def _validate(image: Image.Image) -> tuple[int, int]:
    width, height = image.size
    if width <= 0 or height <= 0 or width * height > MAX_IMAGE_PIXELS:
        raise ValueError("camera image exceeds pixel budget")
    if _decoded_byte_estimate(image) > MAX_DECODED_RGB_BYTES:
        raise ValueError("camera image exceeds decoded-byte budget")
    return width, height


def image_size(path: str | Path) -> tuple[int, int]:
    with Image.open(BytesIO(source_bytes(path))) as image:
        return _validate(image)


def image_size_from_bytes(data: bytes) -> tuple[int, int]:
    if not data or len(data) > MAX_IMAGE_SOURCE_BYTES:
        raise ValueError("camera image source exceeds byte budget")
    with Image.open(BytesIO(data)) as image:
        return _validate(image)


def open_rgb_from_bytes(data: bytes) -> Image.Image:
    """Decode bounded bytes as RGB after source-mode and target-size validation."""
    if not data or len(data) > MAX_IMAGE_SOURCE_BYTES:
        raise ValueError("camera image source exceeds byte budget")
    with Image.open(BytesIO(data)) as image:
        _validate(image)
        if image.width * image.height * 3 > MAX_DECODED_RGB_BYTES:
            raise ValueError("camera image exceeds RGB-byte budget")
        return image.convert("RGB")


def open_rgb(path: str | Path) -> Image.Image:
    return open_rgb_from_bytes(source_bytes(path))


def open_image(path: str | Path) -> Image.Image:
    with Image.open(BytesIO(source_bytes(path))) as image:
        _validate(image)
        return image.copy()


def open_rgb_oriented(path: str | Path) -> Image.Image:
    """Decode bounded bytes as RGB with EXIF orientation applied.

    For comparing a developed frame (already physically oriented) against a
    camera JPEG that stores landscape pixels plus an EXIF orientation tag.
    ``exif_transpose`` runs before ``convert`` so the pixel grids align.
    """
    from PIL import ImageOps

    data = source_bytes(path)
    with Image.open(BytesIO(data)) as image:
        _validate(image)
        if image.width * image.height * 3 > MAX_DECODED_RGB_BYTES:
            raise ValueError("camera image exceeds RGB-byte budget")
        return ImageOps.exif_transpose(image).convert("RGB")


def image_orientation_from_bytes(data: bytes) -> int:
    """Read validated EXIF orientation without decoding or transforming pixels.

    Callers that align independent masks can reject implicit decoder transforms.
    Missing orientation means the encoded pixel grid is authoritative (identity).
    """
    if not data or len(data) > MAX_IMAGE_SOURCE_BYTES:
        raise ValueError("camera image source exceeds byte budget")
    with Image.open(BytesIO(data)) as image:
        _validate(image)
        orientation = image.getexif().get(274, 1)
        if type(orientation) is not int or not 1 <= orientation <= 8:
            raise ValueError("invalid EXIF orientation")
        return orientation
