"""Camera-free scoreboard: colour error, detail retention, highlight handling."""
from __future__ import annotations

import os
import sys
from pathlib import Path

import numpy as np
import pytest
from PIL import Image

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'backend'))
from develop_compare import HIGHLIGHT_THRESHOLD, score_developed_against_camera, score_paths


def test_identical_images_score_zero_colour_error_and_unit_detail():
    rgb = np.zeros((32, 48, 3), np.uint8)
    rgb[:, :24] = (40, 80, 120)
    rgb[:, 24:] = (200, 180, 60)
    rgb[16:20, 10:14] = (8, 8, 8)
    score = score_developed_against_camera(rgb, rgb)
    assert score['neural'] is False
    assert score['algorithm'] == 'develop-compare-v1'
    assert score['colour_error_delta_e76_mean'] == 0
    assert score['colour_error_delta_e76_p95'] == 0
    assert score['detail_retention'] == pytest.approx(1.0)
    assert score['highlight_handling']['camera_clip_fraction'] == 0
    assert score['highlight_handling']['developed_holds_where_camera_clips'] == 1.0


def test_hue_shift_increases_colour_error_without_claiming_a_model():
    camera = np.full((16, 16, 3), 120, np.uint8)
    developed = camera.copy()
    developed[..., 0] = 200
    developed[..., 2] = 40
    score = score_developed_against_camera(developed, camera)
    assert score['colour_error_delta_e76_mean'] > 10
    assert score['neural'] is False


def test_blur_lowers_detail_retention():
    camera = np.zeros((64, 64, 3), np.uint8)
    camera[:, 30:34] = 255
    developed = camera.copy()
    developed[:, 1:] = (developed[:, 1:].astype(np.uint16) + developed[:, :-1].astype(np.uint16)) // 2
    score = score_developed_against_camera(developed, camera)
    assert score['detail_retention'] < 1.0


def test_highlight_handling_counts_held_pixels_where_camera_clips():
    camera = np.full((20, 20, 3), 100, np.uint8)
    camera[:10] = 255
    developed = camera.copy()
    developed[:10] = 220
    score = score_developed_against_camera(developed, camera)
    highlights = score['highlight_handling']
    assert highlights['camera_clip_fraction'] == pytest.approx(0.5)
    assert highlights['developed_clip_fraction'] == 0
    assert highlights['developed_holds_where_camera_clips'] == 1.0
    assert highlights['developed_mean_where_camera_clips'] < HIGHLIGHT_THRESHOLD


def test_score_paths_uses_bounded_jpeg_open(tmp_path):
    camera = tmp_path / 'camera.jpg'
    developed = tmp_path / 'developed.jpg'
    Image.fromarray(np.full((24, 32, 3), 90, np.uint8)).save(camera, quality=100)
    Image.fromarray(np.full((24, 32, 3), 90, np.uint8)).save(developed, quality=100)
    score = score_paths(developed, camera)
    assert score['width'] == 32 and score['height'] == 24
    assert score['colour_error_delta_e76_mean'] < 1


def _reference_scores(developed, camera):
    """Independent full-frame float64 reference for the tiled metrics."""
    xyz = np.array(np.linalg.inv(np.asarray(
        __import__('develop_compare').XYZ_D65_TO_SRGB, dtype=np.float64)))
    d65 = np.array([0.95047, 1.0, 1.08883])

    def to_linear(encoded):
        x = encoded.astype(np.float64) / 255.0
        return np.where(x <= 0.04045, x / 12.92, ((x + 0.055) / 1.055) ** 2.4)

    def to_lab(v):
        t = (to_linear(v) @ xyz.T) / d65
        delta = 6 / 29
        f = np.where(t > delta ** 3, np.cbrt(t), t / (3 * delta ** 2) + 4 / 29)
        return np.stack((116 * f[..., 1] - 16, 500 * (f[..., 0] - f[..., 1]),
                         200 * (f[..., 1] - f[..., 2])), axis=-1)

    delta_e = np.linalg.norm(to_lab(developed) - to_lab(camera), axis=-1)

    def energy(rgb):
        y = (0.2126 * rgb[..., 0] + 0.7152 * rgb[..., 1] + 0.0722 * rgb[..., 2]).astype(np.float64)
        lap = np.zeros_like(y)
        lap[1:-1, 1:-1] = (4 * y[1:-1, 1:-1] - y[:-2, 1:-1] - y[2:, 1:-1]
                           - y[1:-1, :-2] - y[1:-1, 2:])
        return float(lap.var())

    return float(delta_e.mean()), float(np.quantile(delta_e, 0.95)), energy(developed) / energy(camera)


def test_tiled_metrics_match_full_frame_reference_within_tolerance():
    rng = np.random.default_rng(7)
    developed = rng.integers(0, 256, (307, 411, 3), np.uint8)
    camera = rng.integers(0, 256, (307, 411, 3), np.uint8)
    camera[50:80, 60:90] = 255
    ref_mean, ref_p95, ref_detail = _reference_scores(developed, camera)
    score = score_developed_against_camera(developed, camera)
    assert score['colour_error_delta_e76_mean'] == pytest.approx(ref_mean, abs=1e-3)
    assert score['colour_error_delta_e76_p95'] == pytest.approx(ref_p95, abs=1e-3)
    assert score['detail_retention'] == pytest.approx(ref_detail, rel=1e-6)


def test_scores_are_invariant_to_tile_size():
    import develop_compare as dc
    rng = np.random.default_rng(11)
    developed = rng.integers(0, 256, (200, 133, 3), np.uint8)
    camera = rng.integers(0, 256, (200, 133, 3), np.uint8)
    camera[10:40, 5:25] = 255
    original = dc._TILE_ROWS
    try:
        dc._TILE_ROWS = 7
        small = dc.score_developed_against_camera(developed, camera)
        dc._TILE_ROWS = 100000
        whole = dc.score_developed_against_camera(developed, camera)
    finally:
        dc._TILE_ROWS = original
    for key in ('colour_error_delta_e76_mean', 'colour_error_delta_e76_p95', 'detail_retention'):
        assert small[key] == pytest.approx(whole[key], abs=1e-9, rel=0)
    assert (small['highlight_handling']['developed_mean_where_camera_clips']
            == pytest.approx(whole['highlight_handling']['developed_mean_where_camera_clips'], abs=1e-9))


def test_score_paths_applies_exif_orientation_to_portrait_twin(tmp_path):
    # Camera JPEG stores a landscape grid (100w x 60h) plus EXIF Orientation=6,
    # which displays as portrait (60w x 100h); the developed JPEG is already
    # physically portrait. Without exif_transpose the shapes mismatch and the
    # scoreboard would raise instead of scoring the twin.
    camera_grid = np.zeros((60, 100, 3), np.uint8)
    camera_grid[:, :50] = (40, 90, 130)
    camera_grid[:, 50:] = (180, 160, 70)
    camera_image = Image.fromarray(camera_grid)
    exif = camera_image.getexif()
    exif[274] = 6
    camera_path = tmp_path / 'camera.jpg'
    camera_image.save(camera_path, quality=100, exif=exif)

    developed_grid = np.zeros((100, 60, 3), np.uint8)
    developed_grid[:50, :] = (40, 90, 130)
    developed_grid[50:, :] = (180, 160, 70)
    developed_path = tmp_path / 'developed.jpg'
    Image.fromarray(developed_grid).save(developed_path, quality=100)

    score = score_paths(developed_path, camera_path)
    assert score['width'] == 60 and score['height'] == 100


def test_oriented_open_rejects_an_oversized_image_before_decoding_or_transposing(tmp_path, monkeypatch):
    # The orientation entry point must hit the same pixel budget as open_rgb from the
    # header alone: nothing is decoded or transposed for an image over MAX_IMAGE_PIXELS.
    import image_safety
    from PIL import ImageOps
    oversized = tmp_path / 'oversized.png'
    Image.new('1', (8000, 6000)).save(oversized)  # 48 MP, tiny on disk
    assert oversized.stat().st_size < image_safety.MAX_IMAGE_SOURCE_BYTES
    calls = []
    monkeypatch.setattr(ImageOps, 'exif_transpose', lambda image, **kw: calls.append(image) or image)
    with pytest.raises(ValueError, match='pixel budget'):
        image_safety.open_rgb_oriented(oversized)
    with pytest.raises(ValueError, match='pixel budget'):
        image_safety.open_rgb(oversized)
    assert calls == []


def test_full_resolution_scoreboard_peak_memory_is_bounded(tmp_path):
    # A 24 MP twin must not materialise multi-GiB float64 Lab intermediates.
    program = (
        'import resource, platform\n'
        'import numpy as np, sys\n'
        'sys.path.insert(0, %r)\n'
        'import develop_compare as dc\n'
        'rng = np.random.default_rng(0)\n'
        'a = rng.integers(0, 256, (4000, 6000, 3), np.uint8)\n'
        'b = rng.integers(0, 256, (4000, 6000, 3), np.uint8)\n'
        'dc.score_developed_against_camera(a, b)\n'
        'peak = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss\n'
        'unit = 1 if platform.system() == "Darwin" else 1024\n'
        'print(peak * unit)\n'
    ) % str(Path(__file__).resolve().parents[1] / 'backend')
    import subprocess
    out = subprocess.run([sys.executable, '-c', program], capture_output=True, text=True, timeout=120)
    assert out.returncode == 0, out.stderr
    peak_bytes = int(out.stdout.strip())
    # The full-frame float64 version peaked near 4.8 GB; the tiled path stays well
    # under 1.5 GB (measured ~0.6 GB), so this margin cleanly guards a regression.
    assert peak_bytes < 1_500_000_000, f'peak RSS {peak_bytes / 1e9:.2f} GB exceeds bound'


@pytest.mark.skipif(os.environ.get('CAMERA_REAL_DATA_TESTS') != '1',
                    reason='private handheld ARW twins absent from this machine; real-file develop is unproven')
@pytest.mark.skipif(not sys.platform.startswith('linux'),
                    reason='the bounded worker path is Linux-only; macOS fails closed first')
def test_real_handheld_arw_develop_is_unproven_without_private_twins(tmp_path):
    root = Path(__file__).resolve().parents[1] / 'backend' / 'shots' / 'handheld'
    pairs = []
    if root.is_dir():
        for arw in root.rglob('*.ARW'):
            for suffix in ('.JPG', '.jpg', '.JPEG'):
                twin = arw.with_suffix(suffix)
                if twin.is_file():
                    pairs.append((arw, twin))
                    break
    if not pairs:
        pytest.skip('No private handheld ARW twins on this machine; real-file develop is unproven.')
    from isolated_development import run_development
    arw, jpeg = pairs[0]
    result = run_development(arw, tmp_path / 'jobs')
    score = score_paths(tmp_path / 'jobs' / result['job_id'] / 'developed.jpg', jpeg)
    assert result['colour']['source'] == 'camera'
    assert score['neural'] is False
    assert 'colour_error_delta_e76_mean' in score
