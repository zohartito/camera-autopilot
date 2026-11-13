from pathlib import Path
import hashlib
import json
import struct
import sys
import threading

import pytest
from PIL import Image

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'backend'))
import isolated_development as development


def source(tmp_path, color=(100, 100, 100), size=(80, 60)):
    path = tmp_path / 'source.jpg'
    exif = Image.Exif()
    exif[315] = 'private author'
    Image.new('RGB', size, color).save(path, quality=100, exif=exif)
    return path


def test_linear_pixels_tiff_depth_preview_identity_and_source_preserved(tmp_path):
    original = source(tmp_path)
    before = original.read_bytes()
    result = development.run_development(original, tmp_path / 'jobs', {'exposure_ev': 1})
    job = tmp_path / 'jobs' / result['job_id']
    assert original.read_bytes() == before
    assert result['source_sha256'] == hashlib.sha256(before).hexdigest()
    assert result['source_preserved'] and result['neural'] is False
    assert result['retry_capture'] is False
    with Image.open(job / 'developed.jpg') as image, Image.open(job / 'preview.jpg') as preview:
        assert image.size == (80, 60)
        assert 137 <= image.getpixel((20, 20))[0] <= 139
        assert not image.getexif()
        assert image.tobytes() == preview.tobytes()
    with Image.open(job / 'linear.tiff') as image:
        assert image.size == (80, 60)
        assert tuple(image.tag_v2[258]) == (16, 16, 16)
        offset = image.tag_v2[273][0]
        assert 'not DNG' in image.tag_v2[270]
        image.load()
    with (job / 'linear.tiff').open('rb') as handle:
        handle.seek(offset)
        channels = struct.unpack('<HHH', handle.read(6))
    expected = round((((100 / 255 + .055) / 1.055) ** 2.4) * 2 * 65535)
    assert abs(channels[0] - expected) <= 1 and len(set(channels)) == 1
    assert all(path.stat().st_mode & 0o777 == 0o600 for path in job.iterdir())
    assert job.stat().st_mode & 0o777 == 0o700
    assert not list(job.glob('source*'))


@pytest.mark.parametrize('options', [{'exposure_ev': 4}, {'wb_red': 0}, {'wb_blue': float('nan')}, {'tone_strength': True}, {'other': 1}])
def test_invalid_options(tmp_path, options):
    with pytest.raises(ValueError):
        development.run_development(source(tmp_path), tmp_path / 'jobs', options)


def test_invalid_decoder_preserves_original_and_cleans_outputs(tmp_path):
    original = tmp_path / 'invalid.jpg'
    original.write_bytes(b'not an image')
    with pytest.raises(development.DevelopmentError) as raised:
        development.run_development(original, tmp_path / 'jobs')
    assert original.read_bytes() == b'not an image'
    job = tmp_path / 'jobs' / raised.value.job_id
    assert sorted(p.name for p in job.iterdir()) == ['receipt.json', 'worker.log']
    assert json.loads((job / 'receipt.json').read_text())['retry_capture'] is False


def test_pre_cancellation(tmp_path):
    cancel = threading.Event()
    cancel.set()
    with pytest.raises(development.DevelopmentError, match='canceled') as raised:
        development.run_development(source(tmp_path), tmp_path / 'jobs', cancel_event=cancel)
    assert raised.value.reason == 'canceled'


def test_timeout_kills_child_without_source_loss(tmp_path, monkeypatch):
    original = source(tmp_path)
    monkeypatch.setattr(development, 'MAX_SECONDS', .001)
    with pytest.raises(development.DevelopmentError) as raised:
        development.run_development(original, tmp_path / 'jobs')
    assert raised.value.reason == 'timeout'
    assert original.exists()


def test_memory_watchdog_kills_child(tmp_path, monkeypatch):
    monkeypatch.setattr(development, '_rss_bytes', lambda pid: development.MAX_MEMORY_BYTES + 1)
    with pytest.raises(development.DevelopmentError) as raised:
        development.run_development(source(tmp_path), tmp_path / 'jobs')
    assert raised.value.reason == 'memory_limit'


def test_byte_and_pixel_limits(tmp_path, monkeypatch):
    original = source(tmp_path)
    monkeypatch.setattr(development, 'MAX_SOURCE_BYTES', 5)
    with pytest.raises(development.DevelopmentError, match='128 MiB'):
        development.run_development(original, tmp_path / 'jobs')
    monkeypatch.setattr(development, 'MAX_SOURCE_BYTES', 128 * 1024 * 1024)
    monkeypatch.setattr(development, 'MAX_PIXELS', 100)
    with pytest.raises(development.DevelopmentError) as raised:
        development.run_development(original, tmp_path / 'jobs')
    assert raised.value.reason == 'worker_failed'


def test_symlink_rejected(tmp_path):
    original = source(tmp_path)
    link = tmp_path / 'link.jpg'
    link.symlink_to(original)
    with pytest.raises(development.DevelopmentError):
        development.run_development(link, tmp_path / 'jobs')


def test_raw_fails_closed_without_hard_platform_bound(tmp_path, monkeypatch):
    original = tmp_path / 'source.arw'
    original.write_bytes(b'RAW')
    monkeypatch.setattr(development.sys, 'platform', 'darwin')
    with pytest.raises(development.DevelopmentError) as raised:
        development.run_development(original, tmp_path / 'jobs')
    assert raised.value.reason == 'raw_unavailable'
    assert original.read_bytes() == b'RAW'


def test_white_balance_and_highlight_options_have_declared_pixel_effect(tmp_path):
    original = source(tmp_path, (150, 150, 150))
    result = development.run_development(original, tmp_path / 'jobs', {'wb_red': 1.5, 'wb_blue': .7, 'highlight_compression': .8, 'tone_strength': .25})
    with Image.open(tmp_path / 'jobs' / result['job_id'] / result['image']) as output:
        r, g, b = output.getpixel((20, 20))
        assert r > g > b
    assert result['options']['highlight_compression'] == .8


def test_cancellation_while_child_running(tmp_path, monkeypatch):
    cancel = threading.Event()
    def cancel_on_sample(pid):
        cancel.set()
        return 1
    monkeypatch.setattr(development, '_rss_bytes', cancel_on_sample)
    with pytest.raises(development.DevelopmentError) as raised:
        development.run_development(source(tmp_path, size=(2000, 2000)), tmp_path / 'jobs', cancel_event=cancel)
    assert raised.value.reason == 'canceled'


def test_changed_source_withholds_render(tmp_path, monkeypatch):
    original = source(tmp_path, size=(2000, 2000))
    real_sample = development._rss_bytes
    changed = False
    def change_on_sample(pid):
        nonlocal changed
        if not changed:
            original.write_bytes(original.read_bytes() + b'changed')
            changed = True
        return real_sample(pid)
    monkeypatch.setattr(development, '_rss_bytes', change_on_sample)
    with pytest.raises(development.DevelopmentError) as raised:
        development.run_development(original, tmp_path / 'jobs')
    assert raised.value.reason == 'source_changed'
    assert original.read_bytes().endswith(b'changed')


def test_multi_picture_jpeg_primary_frame_only(tmp_path):
    original = tmp_path / 'sony.jpg'
    first = Image.new('RGB', (100, 80), (100, 100, 100))
    second = Image.new('RGB', (100, 80), (200, 0, 0))
    first.save(original, format='MPO', save_all=True, append_images=[second], quality=100)
    result = development.run_development(original, tmp_path / 'jobs')
    with Image.open(tmp_path / 'jobs' / result['job_id'] / result['image']) as output:
        assert output.size == (100, 80)
        assert output.getpixel((20, 20)) == (100, 100, 100)


def test_orientation_applied_and_private_metadata_removed(tmp_path):
    original = tmp_path / 'portrait.jpg'
    exif = Image.Exif()
    exif[274] = 6
    exif[315] = 'private author'
    Image.new('RGB', (100, 80), (90, 90, 90)).save(original, exif=exif)
    result = development.run_development(original, tmp_path / 'jobs')
    with Image.open(tmp_path / 'jobs' / result['job_id'] / result['image']) as output:
        assert output.size == (80, 100)
        assert not output.getexif()
    assert Image.open(original).getexif()[274] == 6


def test_fifo_source_rejected_before_blocking_copy(tmp_path):
    import os
    import subprocess
    os.mkfifo(tmp_path / 'source.jpg')
    script = '''from pathlib import Path
import sys
sys.path.insert(0, sys.argv[1])
from isolated_development import run_development, DevelopmentError
try:
    run_development(Path(sys.argv[2]), Path(sys.argv[3]))
except DevelopmentError:
    raise SystemExit(0)
raise SystemExit(2)
'''
    result = subprocess.run([sys.executable, '-c', script, str(Path(development.__file__).parent), str(tmp_path / 'source.jpg'), str(tmp_path / 'jobs')], timeout=3, capture_output=True)
    assert result.returncode == 0, result.stderr.decode()
