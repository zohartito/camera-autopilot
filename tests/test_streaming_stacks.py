"""Geometry, halo continuity and bounded-resource regressions for JPEG stacks."""
from __future__ import annotations

import gc
from pathlib import Path
import weakref
import sys
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))

import cv2
import numpy as np
import pytest
import stacking


def write_frames(tmp_path, images):
    paths = []
    for index, image in enumerate(images):
        path = tmp_path / f"source-{index}.png"
        assert cv2.imwrite(str(path), image)
        paths.append(path)
    return paths


@pytest.mark.parametrize("operation", ["hdr_merge", "long_exposure", "crowd_remove", "focus_stack"])
def test_tile_boundaries_do_not_change_pixels(tmp_path, monkeypatch, operation):
    rng = np.random.default_rng(198)
    base = rng.integers(45, 205, (289, 337, 3), dtype=np.uint8)
    frames = [base, np.clip(base.astype(np.int16)+25, 0, 255).astype(np.uint8),
              np.clip(base.astype(np.int16)-30, 0, 255).astype(np.uint8)]
    frames[1][100:150, 100:150] = (50, 170, 110)
    paths = write_frames(tmp_path, frames)
    # Isolate tile support from the independently tested registration algorithm.
    monkeypatch.setattr(stacking, "_registration", lambda *args, **kwargs: np.eye(3))
    # This bracket clips nothing, so HDR would hand back the metered frame untouched and the
    # comparison would be vacuous. Run the tiled merge regardless: that is what is under test.
    monkeypatch.setattr(stacking, "HDR_NOTHING_TO_RECOVER_FRACTION", 0.0)
    monkeypatch.setattr(stacking, "STACK_TILE_SIZE", 512)
    full = getattr(stacking, operation)(paths)
    monkeypatch.setattr(stacking, "STACK_TILE_SIZE", 128)
    tiled = getattr(stacking, operation)(paths)
    residual = np.abs(tiled.astype(np.int16) - full.astype(np.int16))
    assert residual.max() <= 1
    assert residual.mean() < .025
    # A seam must not create a stripe even where the moving subject crosses it.
    assert residual[:, 127:130].mean() < .025
    assert residual[127:130].mean() < .025


@pytest.mark.parametrize("hdr", [False, True])
def test_registration_recovers_translation_with_exposure_offset(hdr):
    rng = np.random.default_rng(55)
    reference = cv2.GaussianBlur(rng.integers(30, 180, (512, 768), dtype=np.uint8), (3, 3), 0)
    transform = np.array([[1, 0, 9], [0, 1, -6]], dtype=np.float32)
    target = cv2.warpAffine(reference, transform, (768, 512), borderMode=cv2.BORDER_REFLECT)
    if hdr:
        target = np.clip(target.astype(np.int16) + 35, 0, 255).astype(np.uint8)
    registration = stacking._registration(reference, target, hdr=hdr)
    assert np.allclose(registration[:2, 2], [-9, 6], atol=.4)
    aligned = cv2.warpPerspective(target, registration, (768, 512))
    expected = reference.astype(np.int16) + (35 if hdr else 0)
    assert np.abs(aligned[20:-20, 20:-20].astype(np.int16) - expected[20:-20, 20:-20]).mean() < .3


def test_textureless_hdr_registration_does_not_invent_a_shift():
    reference = np.full((128, 128), 80, dtype=np.uint8)
    target = np.full_like(reference, 160)
    assert np.array_equal(stacking._registration(reference, target, hdr=True), np.eye(3))


def test_full_resolution_frames_are_decoded_one_at_a_time(tmp_path, monkeypatch):
    paths = write_frames(tmp_path, [np.full((80, 96, 3), i+80, np.uint8) for i in range(9)])
    references = []
    decode = stacking._read_one
    def tracked(path):
        gc.collect()
        assert all(ref() is None for ref in references), "a previous full decoded frame is retained"
        image = decode(path)
        references.append(weakref.ref(image))
        return image
    monkeypatch.setattr(stacking, "_read_one", tracked)
    merged = stacking.hdr_merge(paths)
    assert merged.shape == (80, 96, 3)
    assert len(references) == 9


def test_failed_decode_cleans_private_staging_and_preserves_inputs(tmp_path, monkeypatch):
    paths = write_frames(tmp_path, [np.full((32, 48, 3), 80, np.uint8)] * 3)
    originals = [path.read_bytes() for path in paths]
    decode = stacking._read_one
    observed = []
    import tempfile
    real_temporary = tempfile.TemporaryDirectory
    def temporary(*args, **kwargs):
        result = real_temporary(*args, **kwargs)
        observed.append(Path(result.name))
        assert Path(result.name).stat().st_mode & 0o777 == 0o700
        return result
    def fail_second(path):
        if path == paths[1]:
            assert all(p.stat().st_mode & 0o777 == 0o600 for p in observed[-1].iterdir())
            raise ValueError("injected corrupt second frame")
        return decode(path)
    monkeypatch.setattr(stacking.tempfile, "TemporaryDirectory", temporary)
    monkeypatch.setattr(stacking, "_read_one", fail_second)
    with pytest.raises(ValueError, match="injected"):
        stacking.long_exposure(paths)
    assert observed and not any(path.exists() for path in observed)
    assert [path.read_bytes() for path in paths] == originals


@pytest.mark.parametrize("budget", ["MAX_STACK_WORKING_BYTES", "MAX_STACK_TEMP_BYTES"])
def test_aggregate_budgets_reject_before_pixel_decode(tmp_path, monkeypatch, budget):
    paths = write_frames(tmp_path, [np.full((32, 48, 3), 80, np.uint8)] * 2)
    monkeypatch.setattr(stacking, budget, 1)
    monkeypatch.setattr(stacking, "_read_one", lambda path: pytest.fail("decoded before guard"))
    with pytest.raises(ValueError, match="aggregate"):
        stacking.hdr_merge(paths)


def test_aggregate_encoded_budget_rejects_before_pixel_decode(tmp_path, monkeypatch):
    import stack_session
    paths = write_frames(tmp_path, [np.full((32, 48, 3), 80, np.uint8)] * 2)
    monkeypatch.setattr(stack_session, "MAX_SESSION_SOURCE_BYTES", 1)
    monkeypatch.setattr(stacking, "_read_one", lambda path: pytest.fail("decoded before guard"))
    with pytest.raises(ValueError, match="aggregate source-byte"):
        stacking.hdr_merge(paths)


def test_nine_24mp_headers_fit_current_bounds_without_decoding(tmp_path, monkeypatch):
    import image_safety
    paths = write_frames(tmp_path, [np.full((32, 48, 3), 80, np.uint8)] * 9)
    monkeypatch.setattr(image_safety, "image_size", lambda path: (6000, 4000))
    assert stacking._validate_sources(paths) == (6000, 4000)


def test_changed_dimensions_fail_before_staging_write(tmp_path, monkeypatch):
    paths = write_frames(tmp_path, [np.full((32, 48, 3), 80, np.uint8)] * 2)
    monkeypatch.setattr(stacking, "_read_one", lambda path: np.zeros((16, 48, 3), np.uint8))
    with pytest.raises(ValueError, match="dimensions changed"):
        stacking.long_exposure(paths)


def test_plain_hdr_multiband_halo_matches_whole_image(tmp_path, monkeypatch):
    rng = np.random.default_rng(98)
    ramp = np.linspace(10, 210, 537, dtype=np.float32)[None, :, None]
    texture = rng.normal(0, 16, (311, 537, 3))
    base = ramp + texture
    frames = [np.clip(base * scale, 0, 255).astype(np.uint8) for scale in [.5, 1, 2]]
    paths = write_frames(tmp_path, frames)
    monkeypatch.setattr(stacking, "_registration", lambda *args, **kwargs: np.eye(3))
    # The anchor clips ~0.52 %, just above the nothing-to-recover threshold; pin the merge on so
    # a fixture tweak cannot turn this halo comparison into anchor-vs-anchor.
    monkeypatch.setattr(stacking, "HDR_NOTHING_TO_RECOVER_FRACTION", 0.0)
    monkeypatch.setattr(stacking, "STACK_TILE_SIZE", 1024)
    whole = stacking.hdr_merge(paths, deghost=False)
    monkeypatch.setattr(stacking, "STACK_TILE_SIZE", 128)
    tiled = stacking.hdr_merge(paths, deghost=False)
    residual = np.abs(tiled.astype(np.int16) - whole.astype(np.int16))
    assert residual.max() <= 1
    assert residual.mean() < .025


def test_missing_raw_twin_is_refused_before_header_read(tmp_path, monkeypatch):
    import image_safety
    raw = tmp_path / "source.ARW"
    raw.write_bytes(b"native RAW must never be parsed")
    monkeypatch.setattr(image_safety, "image_size", lambda path: pytest.fail("RAW header reader entered"))
    with pytest.raises(FileNotFoundError, match="disabled and no JPEG twin"):
        stacking.hdr_merge([raw, raw])
