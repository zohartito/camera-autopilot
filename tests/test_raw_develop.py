"""Camera-free RAW develop path: isolated decoder, camera colour, strength identity."""
from __future__ import annotations

import hashlib
import json
import struct
import sys
from pathlib import Path
from unittest.mock import Mock

import numpy as np
import pytest
from PIL import Image

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'backend'))
sys.path.insert(0, str(Path(__file__).resolve().parent))
import isolated_development as development
import isolated_raw
import linear_dng
from raw_colour import camera_rgb_to_linear_srgb, write_linear_tiff, XYZ_D65_TO_SRGB
from test_isolated_raw import LINUX_ONLY, write_arw


def _stub_linear(path, output, **kwargs):
    pixels = np.full((8, 12, 3), 20000, dtype=np.uint16)
    pixels[:, 6:, 0] = 40000
    write_linear_tiff(output, pixels)
    digest = hashlib.sha256(np.asarray(pixels, dtype='<u2').tobytes()).hexdigest()
    return isolated_raw.RawLinearResult(
        width=12, height=8, dtype='uint16', pixel_sha256=digest,
        camera_whitebalance=(1.8, 1.0, 1.4),
        rgb_xyz_matrix=((0.5, 0.3, 0.2), (0.2, 0.7, 0.1), (0.1, 0.2, 0.7)),
        colour_source='camera', decoder='stub', elapsed_seconds=0.01,
        address_space_limit=isolated_raw.ADDRESS_SPACE_BYTES,
        output_bytes=output.stat().st_size,
    )


def test_camera_rgb_uses_as_shot_wb_and_camera_matrix_not_identity():
    matrix = np.linalg.inv(np.asarray(XYZ_D65_TO_SRGB, dtype=np.float64))
    camera = np.full((2, 2, 3), round(0.25 * 65535), dtype=np.uint16)
    identity_wb = camera_rgb_to_linear_srgb(camera, [1, 1, 1], matrix.tolist())
    assert np.allclose(identity_wb.astype(np.float64) / 65535, 0.25, atol=2 / 65535)
    scaled = camera_rgb_to_linear_srgb(camera, [2, 1, 1], matrix.tolist())
    assert scaled[0, 0, 0] > scaled[0, 0, 1]
    with pytest.raises(ValueError, match='white balance'):
        camera_rgb_to_linear_srgb(camera, [0, 1, 1], matrix.tolist())
    with pytest.raises(ValueError, match='colour matrix'):
        camera_rgb_to_linear_srgb(camera, [1, 1, 1], [[0, 0, 0], [0, 0, 0], [0, 0, 0]])


@LINUX_ONLY
def test_arw_develop_routes_through_isolated_decoder(tmp_path, monkeypatch):
    path = write_arw(tmp_path)
    before = path.read_bytes()
    called = {}

    def stub(source, output, **kwargs):
        called['source'] = Path(source)
        return _stub_linear(source, output, **kwargs)

    monkeypatch.setattr(isolated_raw, 'render_arw_linear_isolated', stub)
    result = development.run_development(path, tmp_path / 'jobs')
    job = tmp_path / 'jobs' / result['job_id']
    assert path.read_bytes() == before
    assert called['source'].name.endswith('.arw')
    assert result['source_kind'] == 'raw'
    assert result['neural'] is False
    assert result['colour']['source'] == 'camera'
    assert result['colour']['whitebalance'] == [1.8, 1.0, 1.4]
    assert result['source_identity']['make'] == 'SONY'
    assert result['source_identity']['model'] == 'ILCE-7M2'
    assert not list(job.glob('camera_linear*'))
    with Image.open(job / 'linear.tiff') as image:
        assert image.size == (12, 8)
        assert tuple(image.tag_v2[258]) == (16, 16, 16)
        offset = image.tag_v2[273][0]
    with (job / 'linear.tiff').open('rb') as handle:
        handle.seek(offset)
        sample = struct.unpack('<HHH', handle.read(6))
    assert sample[0] == 20000 and sample[1] == 20000
    with Image.open(job / 'developed.jpg') as image:
        exif = image.getexif()
        assert exif.get(271) == 'SONY'
        assert exif.get(272) == 'ILCE-7M2'
        assert 34853 not in exif
    assert 'import rawpy' not in Path(development.__file__).with_name('development_worker.py').read_text()


@LINUX_ONLY
def test_zero_strength_is_faithful_camera_linear(tmp_path, monkeypatch):
    path = write_arw(tmp_path)
    monkeypatch.setattr(isolated_raw, 'render_arw_linear_isolated', _stub_linear)
    result = development.run_development(path, tmp_path / 'jobs')
    job = tmp_path / 'jobs' / result['job_id']
    with Image.open(job / 'linear.tiff') as image:
        offset = image.tag_v2[273][0]
    with (job / 'linear.tiff').open('rb') as handle:
        handle.seek(offset)
        pixels = np.frombuffer(handle.read(8 * 12 * 6), dtype='<u2').reshape(8, 12, 3)
    assert pixels[0, 0, 0] == 20000
    assert pixels[0, 8, 0] == 40000
    stronger = development.run_development(path, tmp_path / 'jobs2', {'tone_strength': 1})
    with Image.open(tmp_path / 'jobs2' / stronger['job_id'] / 'linear.tiff') as image:
        offset = image.tag_v2[273][0]
    with (tmp_path / 'jobs2' / stronger['job_id'] / 'linear.tiff').open('rb') as handle:
        handle.seek(offset)
        lifted = np.frombuffer(handle.read(8 * 12 * 6), dtype='<u2').reshape(8, 12, 3)
    assert lifted[0, 0, 0] != 20000


@LINUX_ONLY
def test_raw_development_receipt_is_accepted_for_dng(tmp_path, monkeypatch):
    path = write_arw(tmp_path)
    monkeypatch.setattr(isolated_raw, 'render_arw_linear_isolated', _stub_linear)
    result = development.run_development(path, tmp_path / 'jobs')
    job = tmp_path / 'jobs' / result['job_id']
    meta = linear_dng.validate_input(job / 'linear.tiff', job / 'receipt.json')
    assert meta['source_kind'] == 'raw'
    header = linear_dng._header(meta)
    assert b'RAW-derived' in header
    assert b'SONY' in header or b'ILCE-7M2' in header


@LINUX_ONLY
def test_arw_without_libraw_is_typed_error_and_preserves_source(tmp_path):
    path = write_arw(tmp_path)
    before = path.read_bytes()
    with pytest.raises(development.DevelopmentError) as error:
        development.run_development(path, tmp_path / 'jobs')
    assert error.value.reason in {'decoder_unavailable', 'corrupt'}
    assert path.read_bytes() == before
    assert error.value.retry_capture is False


def test_arw_develop_fails_closed_off_linux_before_isolated_render(tmp_path, monkeypatch):
    path = write_arw(tmp_path)
    before = path.read_bytes()
    render = Mock()
    monkeypatch.setattr(isolated_raw, 'render_arw_linear_isolated', render)
    monkeypatch.setattr(development.sys, 'platform', 'darwin')
    with pytest.raises(development.DevelopmentError) as error:
        development.run_development(path, tmp_path / 'jobs')
    assert error.value.reason == 'raw_unavailable'
    render.assert_not_called()
    assert path.read_bytes() == before
    assert error.value.retry_capture is False


def test_unsupported_raw_suffix_is_refused(tmp_path):
    path = tmp_path / 'scene.nef'
    path.write_bytes(b'NEF')
    with pytest.raises(development.DevelopmentError) as error:
        development.run_development(path, tmp_path / 'jobs')
    assert error.value.reason == 'raw_unavailable'
