"""live_verify.focal_from_exif must read the lens's real focal length (camera-free, synthetic JPEGs).

It never did: image_safety.open_image returns a validated Image copy, which has no JPEG-only
_getexif(), so every call fell into the except branch and returned the default. On real gear the
handheld shutter floor therefore always used the 50 mm fallback, whatever the zoom setting.
"""
from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import patch

import pytest
from PIL import Image
from PIL.TiffImagePlugin import IFDRational

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))

import brain
import camera_caps
import live_verify


def _camera_style_jpeg(path: Path, model: str, focal_mm: float) -> Path:
    """Laid out like a Sony body's JPEG: Make/Model in IFD0, FocalLength (0x920A) as a RATIONAL in
    the Exif sub-IFD (0x8769) next to the exposure tags."""
    exif = Image.Exif()
    exif[271] = "SONY"
    exif[272] = model
    exif[34665] = {37386: IFDRational(int(focal_mm * 10), 10), 33437: IFDRational(40, 10),
                   34855: 200}
    Image.new("RGB", (64, 48), "gray").save(path, exif=exif)
    return path


@pytest.mark.parametrize("model,focal", [("ILCE-7M2", 28.0), ("ILCE-7M3", 24.0)])
def test_focal_is_read_from_the_exif_sub_ifd(tmp_path, model, focal):
    path = _camera_style_jpeg(tmp_path / f"{model}.jpg", model, focal)
    assert live_verify.focal_from_exif(str(path), default=None) == pytest.approx(focal)


def test_focal_in_ifd0_and_missing_or_unreadable_files(tmp_path):
    exif = Image.Exif()
    exif[37386] = 85.0
    top = tmp_path / "ifd0.jpg"
    Image.new("RGB", (48, 32), "gray").save(top, exif=exif)
    assert live_verify.focal_from_exif(str(top), default=None) == pytest.approx(85.0)
    bare = tmp_path / "bare.jpg"
    Image.new("RGB", (48, 32), "gray").save(bare)
    assert live_verify.focal_from_exif(str(bare), default=None) is None
    assert live_verify.focal_from_exif(str(tmp_path / "missing.jpg"), default=50.0) == 50.0


@pytest.mark.parametrize("value", [IFDRational(50, 0), IFDRational(0, 1), -35.0, 1e308, 1e-320, 0.5,
                                   "fifty", (1, 2, 3)])
def test_corrupt_focal_tag_falls_back_to_the_default(tmp_path, value):
    """A 0/0 rational (NaN), zero, negative, sub-millimetre, absurd or non-numeric FocalLength is
    not a lens: the default comes back (the brain would reject such a focal), and nothing raises."""
    exif = Image.Exif()
    exif[34665] = {37386: value}
    path = tmp_path / "corrupt.jpg"
    Image.new("RGB", (16, 16), "gray").save(path, exif=exif)
    assert live_verify.focal_from_exif(str(path), default=None) is None
    assert live_verify.focal_from_exif(str(path)) == 50.0


def test_handheld_floor_follows_a_105mm_frame(tmp_path):
    """The floor the brain models is 1/focal: a 105 mm frame must give 1/105 s, not the 1/50 s the
    50 mm fallback produced."""
    path = _camera_style_jpeg(tmp_path / "tele.jpg", "ILCE-7M3", 105.0)
    focal = live_verify.focal_from_exif(str(path), default=None)
    caps = camera_caps.from_live_choices(
        "Sony Alpha-A7 III (PC Control)", ["100", "200", "400", "800", "1600", "3200", "6400"],
        ["4", "5.6", "8", "11"], ["1/1000", "1/500", "1/250", "1/125", "1/60", "1/30"])
    with patch.object(brain.embed_index, "recommend", return_value={"f_number": 4}):
        rec = brain.recommend_live(str(path), 10.0, caps.name, caps, 4, focal)
    vibrations = rec.card("SHUTTER").sub_factors[0]
    assert vibrations.name == "Vibrations" and vibrations.readout.startswith("1/105s")
    assert rec.shutter_s <= 1 / 105 + 1e-9
