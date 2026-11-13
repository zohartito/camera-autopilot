"""DNG contract and independent decoder verification; no camera/RAW input."""
import hashlib
import importlib.util
import json
from pathlib import Path
import shutil
import struct
import subprocess
import sys

import numpy as np
from PIL import Image
import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'backend'))
import linear_dng
from isolated_development import run_development


def verifier_python():
    if importlib.util.find_spec('rawpy'):
        return Path(sys.executable)
    candidate = ROOT / 'backend/.venv-ml/bin/python'
    if candidate.is_file():
        return candidate
    pytest.skip('Independent LibRaw runtime unavailable; DNG decoder acceptance is not verified.')


@pytest.fixture
def developed(tmp_path):
    source = tmp_path / 'source.jpg'
    rgb = np.zeros((48, 64, 3), np.uint8)
    rgb[..., 0] = np.arange(64, dtype=np.uint8) * 4
    rgb[..., 1] = np.arange(48, dtype=np.uint8)[:, None] * 5
    rgb[..., 2] = 130
    Image.fromarray(rgb).save(source, quality=98, subsampling=0)
    result = run_development(source, tmp_path / 'development')
    job = tmp_path / 'development' / result['job_id']
    return job / 'linear.tiff', job / 'receipt.json'


def tags(path):
    # Test-side independent structural reader; decoder tests below use LibRaw.
    with path.open('rb') as stream:
        header = stream.read(8)
        assert header[:4] == b'II*\0'
        stream.seek(struct.unpack('<I', header[4:])[0])
        count = struct.unpack('<H', stream.read(2))[0]
        result = {}
        for _ in range(count):
            tag, kind, length, slot = struct.unpack('<HHI4s', stream.read(12))
            size = {1: 1, 2: 1, 3: 2, 4: 4, 5: 8, 10: 8, 11: 4}[kind] * length
            position = stream.tell()
            if size > 4:
                stream.seek(struct.unpack('<I', slot)[0])
                payload = stream.read(size)
                stream.seek(position)
            else:
                payload = slot[:size]
            result[tag] = (kind, length, payload)
        return result


def test_real_developer_input_and_independent_pixel_identity(developed, tmp_path):
    tiff, receipt = developed
    before = hashlib.sha256(tiff.read_bytes()).hexdigest()
    result = linear_dng.export_linear_dng(tiff, receipt, tmp_path / 'dng', verification_python=verifier_python())
    output = tmp_path / 'dng' / result['job_id'] / 'output.dng'
    assert result['status'] == 'completed'
    assert result['sensor_raw'] is False and result['source_precision_restored'] is False
    assert result['verification']['sample_equality'] == 'all RGB uint16 samples exact'
    assert result['verification']['pixel_sha256'] == result['pixel_sha256']
    assert result['verification']['shape'] == [48, 64, 3]
    assert hashlib.sha256(tiff.read_bytes()).hexdigest() == before
    directory = tags(output)
    assert directory[50706][2] == bytes([1, 2, 0, 0])
    assert struct.unpack('<H', directory[262][2])[0] == 34892
    assert struct.unpack('<H', directory[50879][2])[0] == 1
    assert directory[258][2] == struct.pack('<HHH', 16, 16, 16)
    assert b'JPEG-derived' in directory[270][2]
    assert b'not sensor RAW' in directory[270][2]
    assert 33421 not in directory and 33422 not in directory  # No invented CFA pattern.
    assert output.stat().st_mode & 0o777 == 0o600


def test_exiftool_independently_validates_tags(developed, tmp_path):
    exiftool = shutil.which('exiftool')
    if not exiftool:
        pytest.skip('ExifTool unavailable; independent metadata validation not verified.')
    result = linear_dng.export_linear_dng(*developed, tmp_path / 'dng', verification_python=verifier_python())
    path = tmp_path / 'dng' / result['job_id'] / 'output.dng'
    check = subprocess.run([exiftool, '-j', '-validate', '-warning', '-error', '-ColorimetricReference', '-PhotometricInterpretation', str(path)], capture_output=True, text=True, check=True, timeout=10)
    data = json.loads(check.stdout)[0]
    assert data['Validate'] == 'OK'
    assert 'Warning' not in data and 'Error' not in data
    assert data['PhotometricInterpretation'] == 'Linear Raw'
    assert data['ColorimetricReference'].startswith('Output-referred')


def test_color_matrix_maps_d65_white_without_sensor_calibration():
    matrix = np.asarray(linear_dng.XYZ_D65_TO_SRGB)
    white = np.array([.3127 / .3290, 1., (1 - .3127 - .3290) / .3290])
    assert np.allclose(matrix @ white, np.ones(3), atol=1e-10)
    # Positive RGB primaries and exact invertibility, rather than identity XYZ misuse.
    xyz = np.linalg.inv(matrix)
    assert np.all(xyz >= 0)
    assert not np.allclose(matrix, np.eye(3))
    import raw_colour
    assert raw_colour.XYZ_D65_TO_SRGB == linear_dng.XYZ_D65_TO_SRGB


def test_changed_tiff_cannot_be_exported(developed, tmp_path):
    tiff, receipt = developed
    contents = bytearray(tiff.read_bytes()); contents[-1] ^= 1; tiff.write_bytes(contents)
    with pytest.raises(linear_dng.DNGError) as error:
        linear_dng.export_linear_dng(tiff, receipt, tmp_path / 'dng')
    assert error.value.reason == 'hash_mismatch'
    assert not (tmp_path / 'dng').exists()


def test_neural_provenance_cannot_export(developed, tmp_path):
    tiff, receipt = developed
    data = json.loads(receipt.read_text()); data['neural'] = True; receipt.write_text(json.dumps(data))
    with pytest.raises(linear_dng.DNGError, match='non-neural'):
        linear_dng.export_linear_dng(tiff, receipt, tmp_path / 'dng')


def test_raw_receipt_writes_raw_derived_dng_tags_not_jpeg(developed):
    tiff, receipt = developed
    data = json.loads(receipt.read_text())
    data['source_kind'] = 'raw'
    data['source_identity'] = {'make': 'SONY', 'model': 'ILCE-7M2'}
    receipt.write_text(json.dumps(data))
    meta = linear_dng.validate_input(tiff, receipt)
    assert meta['source_kind'] == 'raw'
    header = linear_dng._header(meta)
    assert b'RAW-derived' in header
    assert b'JPEG-derived' not in header
    assert b'ILCE-7M2' in header
    assert b'GPS' not in header
    assert b'camera colour' in header


def test_decoder_failure_removes_unverified_candidate(developed, tmp_path):
    result_root = tmp_path / 'dng'
    with pytest.raises(linear_dng.DNGError) as failed:
        linear_dng.export_linear_dng(*developed, result_root, verification_python='/usr/bin/false')
    assert failed.value.reason == 'decoder_verification_failed'
    job = result_root / failed.value.job_id
    assert not list(job.glob('*.dng'))
    assert json.loads((job / 'receipt.json').read_text())['status'] == 'failed'


def test_symlink_source_is_rejected(developed, tmp_path):
    link = tmp_path / 'link.tiff'; link.symlink_to(developed[0])
    with pytest.raises(OSError):
        linear_dng.validate_input(link, developed[1])


def test_unsupported_tiff_layout_is_rejected_before_native_decode(developed):
    tiff, _ = developed
    with tiff.open('r+b') as stream:
        stream.seek(8); stream.write(struct.pack('<H', 65535))
    with tiff.open('rb') as stream, pytest.raises(linear_dng.DNGError, match='directory'):
        linear_dng.inspect_development_tiff(stream)


def test_concurrent_export_is_explicitly_busy(developed, tmp_path):
    assert linear_dng._EXPORT_LOCK.acquire(blocking=False)
    try:
        with pytest.raises(linear_dng.DNGError) as failed:
            linear_dng.export_linear_dng(*developed, tmp_path / 'dng')
        assert failed.value.reason == 'busy'
    finally:
        linear_dng._EXPORT_LOCK.release()


def test_hash_reader_does_not_follow_unbounded_file_growth(monkeypatch):
    import io
    monkeypatch.setattr(linear_dng, 'MAX_TIFF_BYTES', 16)
    with pytest.raises(linear_dng.DNGError, match='byte budget'):
        linear_dng._sha_stream(io.BytesIO(b'x' * 17))
