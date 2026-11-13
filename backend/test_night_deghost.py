"""Motion-aware deghosting for night stacks (stacking.long_exposure_with_report).

Synthetic truth: N=4 frames of one static texture plus Gaussian noise and a bright square
that sits at a different place in every frame. The deghosted average must show the square
only where the reference (middle) frame had it, keep the N-frame noise gain in static
regions, and leave the plain ``deghost=False`` average byte-identical to a numpy mean.

The last test re-merges a real A7 III session when its private frames are present on this
machine; it is skipped everywhere else and never copies the frames.
"""

from __future__ import annotations

import hashlib
import os
import json
import sys
from pathlib import Path

import cv2
import numpy as np
import pytest

import stacking
import stack_session

_SIZE = 256
_SQUARE = 32
_NOISE_SIGMA = 4.0
# Square top-left corner per frame; the reference for N=4 is index 1 (middle rule).
_SQUARE_POSITIONS = [(24, 24), (24, 150), (150, 24), (150, 150)]
_REAL_SESSION = Path(__file__).resolve().parent / "shots" / "sessions" / "a33ccd6461e54cebaf1c9bf1e03f028d"
# Static wall patch above the window in the real session (full-resolution y:x slice). Its
# motion mask is empty, so it measures the averaging gain, not the deghosted subject.
_REAL_STATIC_PATCH = (slice(600, 856), slice(2500, 2756))
# Bounding box of the person who moved on the couch during that session (y:x, full res).
_REAL_MOVER_BOX = (slice(2150, 2800), slice(4800, 5950))


def _base_texture(rng: np.random.Generator) -> np.ndarray:
    coarse = rng.integers(70, 150, (_SIZE // 8, _SIZE // 8, 3), dtype=np.uint8)
    return cv2.resize(coarse, (_SIZE, _SIZE), interpolation=cv2.INTER_CUBIC)


def _frames(rng: np.random.Generator, *, moving: bool) -> tuple[np.ndarray, list[np.ndarray]]:
    base = _base_texture(rng)
    frames = []
    for index in range(4):
        clean = base.copy()
        if moving:
            y, x = _SQUARE_POSITIONS[index]
            clean[y:y + _SQUARE, x:x + _SQUARE] = 245
        noisy = clean.astype(np.float64) + rng.normal(0.0, _NOISE_SIGMA, clean.shape)
        frames.append(np.clip(noisy, 0, 255).astype(np.uint8))
    return base, frames


def _write(frames: list[np.ndarray], directory: Path) -> list[str]:
    directory.mkdir(parents=True, exist_ok=True)
    paths = []
    for index, frame in enumerate(frames):
        path = directory / f"frame_{index:04d}.png"
        assert cv2.imwrite(str(path), frame)
        paths.append(str(path))
    return paths


def _plain_mean(frames: list[np.ndarray]) -> np.ndarray:
    total = frames[0].astype(np.float32)
    for frame in frames[1:]:
        total += frame
    return np.clip(total / len(frames), 0, 255).astype(np.uint8)


def _residual_sigma(gray: np.ndarray) -> float:
    """Noise estimate as in tools/calibrate_sensor_noise: std after removing a 5x5 median."""
    median = cv2.medianBlur(gray.astype(np.uint8), 5).astype(np.float64)
    return float((gray.astype(np.float64) - median).std())


@pytest.fixture
def identity_registration(monkeypatch):
    # The synthetic frames share one grid; pin registration so these tests exercise the
    # deghost composite rather than ECC's sub-pixel estimate on a tiny texture.
    monkeypatch.setattr(stacking, "_registration", lambda *args, **kwargs: np.eye(3))


def test_moving_square_appears_once(tmp_path, identity_registration):
    rng = np.random.default_rng(7)
    base, frames = _frames(rng, moving=True)
    image, report = stacking.long_exposure_with_report(_write(frames, tmp_path), deghost=True)
    assert report["deghost"] is True and report["reference_index"] == 1
    reference = frames[1].astype(np.float64)
    result = image.astype(np.float64)
    for index, (y, x) in enumerate(_SQUARE_POSITIONS):
        region = (slice(y + 4, y + _SQUARE - 4), slice(x + 4, x + _SQUARE - 4))
        error = np.abs(result[region] - reference[region]).mean()
        if index == 1:
            assert result[region].mean() > 230, "reference square must survive"
        else:
            # A plain mean would leave a ~45-level bright ghost here; deghosted pixels come
            # from the reference, so only the reference's own noise remains.
            assert error < 3.0, f"stale square {index} still ghosts: mean error {error:.2f}"
            assert result[region].mean() < base[region].mean() + 12
    static = (slice(200, 256), slice(0, 256))
    single = np.abs(frames[0][static].astype(np.float64) - base[static]).std()
    averaged = np.abs(result[static] - base[static]).std()
    assert averaged < single, (averaged, single)
    assert report["mask"].dtype == np.uint8 and report["mask"].shape == image.shape[:2]
    assert report["mask"][static].max() == 0
    assert 0.04 <= report["moving_fraction"] <= 0.20, report["moving_fraction"]


def test_plain_average_is_unchanged_without_deghost(tmp_path, identity_registration):
    rng = np.random.default_rng(11)
    _, frames = _frames(rng, moving=True)
    paths = _write(frames, tmp_path)
    expected = _plain_mean(frames)
    assert np.array_equal(stacking.long_exposure(paths), expected)
    image, report = stacking.long_exposure_with_report(paths, deghost=False)
    assert np.array_equal(image, expected)
    assert report == {"deghost": False, "reference_index": None, "moving_fraction": 0.0, "mask": None}
    # The trails a long-exposure look wants are kept: every square position stays visible.
    for y, x in _SQUARE_POSITIONS:
        assert expected[y + 4:y + _SQUARE - 4, x + 4:x + _SQUARE - 4].mean() > 130


def test_static_stack_keeps_full_average(tmp_path, identity_registration):
    rng = np.random.default_rng(3)
    _, frames = _frames(rng, moving=False)
    paths = _write(frames, tmp_path)
    image, report = stacking.long_exposure_with_report(paths, deghost=True)
    assert report["moving_fraction"] < 0.005, report["moving_fraction"]
    expected = _plain_mean(frames)
    untouched = report["mask"] == 0
    assert untouched.mean() > 0.99
    assert np.array_equal(image[untouched], expected[untouched])


def test_deghost_mask_counts_toward_working_budget(tmp_path, monkeypatch):
    _, frames = _frames(np.random.default_rng(5), moving=False)
    paths = _write(frames, tmp_path)
    plain_estimate = stacking._working_estimate(_SIZE, _SIZE, len(paths))
    # The deghosted night average carries the mask pipeline and returns a full-resolution
    # uint8 mask, so it is estimated at least one byte per pixel above the plain average.
    assert stacking._working_estimate(_SIZE, _SIZE, len(paths), mask_output=True) >= plain_estimate + _SIZE * _SIZE
    monkeypatch.setattr(stacking, "MAX_STACK_WORKING_BYTES", plain_estimate)
    assert stacking._validate_sources(paths) == (_SIZE, _SIZE)
    monkeypatch.setattr(stacking, "_read_one", lambda *args: pytest.fail("must reject before native decode"))
    with pytest.raises(ValueError, match="aggregate"):
        stacking.long_exposure(paths, deghost=True)


def test_single_frame_stack_is_rejected_before_decode(tmp_path, monkeypatch):
    _, frames = _frames(np.random.default_rng(5), moving=False)
    paths = _write(frames[:1], tmp_path)
    monkeypatch.setattr(stacking, "_read_one", lambda *args: pytest.fail("must reject before native decode"))
    with pytest.raises(ValueError, match="frames"):
        stacking.long_exposure_with_report(paths, deghost=True)


def test_night_merge_writes_motion_mask(tmp_path, identity_registration):
    _, frames = _frames(np.random.default_rng(9), moving=True)
    paths = _write(frames, tmp_path / "src")
    out = tmp_path / "result.jpg"
    summary = stack_session.merge_session_frames("night", paths, str(out))
    assert out.is_file()
    assert summary["deghost"] is True and summary["reference_index"] == 1
    assert 0.04 <= summary["moving_fraction"] <= 0.20
    assert summary["maps_dir"] == str(tmp_path) and summary["maps"] == ["motion_mask.png"]
    mask = cv2.imread(str(tmp_path / "motion_mask.png"), cv2.IMREAD_UNCHANGED)
    assert mask.dtype == np.uint8 and mask.shape == (_SIZE, _SIZE) and mask.max() == 255
    y, x = _SQUARE_POSITIONS[0]
    assert mask[y + 8:y + _SQUARE - 8, x + 8:x + _SQUARE - 8].min() == 255

    plain = stack_session.merge_session_frames("night", paths, str(tmp_path / "plain.jpg"),
                                               night_options={"deghost": False})
    assert plain["deghost"] is False and plain["moving_fraction"] == 0.0
    assert "maps" not in plain and plain["reference_index"] is None


def test_remerge_tool_never_touches_session_outputs(tmp_path, identity_registration):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools"))
    import remerge_session
    _, frames = _frames(np.random.default_rng(13), moving=True)
    session = tmp_path / "session"
    session.mkdir()
    for index, frame in enumerate(frames):
        assert cv2.imwrite(str(session / f"frame_{index:04d}.jpg"), frame, [cv2.IMWRITE_JPEG_QUALITY, 100])
    # A rejected frame must be ignored even though its file exists.
    assert cv2.imwrite(str(session / "frame_0004.jpg"), frames[0])
    assert cv2.imwrite(str(session / "result.jpg"), _plain_mean(frames))
    manifest = {"id": "synthetic", "mode": "night",
                "frames": [{"index": i, "status": "accepted"} for i in range(4)]
                + [{"index": 4, "status": "rejected"}]}
    (session / "manifest.json").write_text(json.dumps(manifest))
    before = {name: hashlib.sha256((session / name).read_bytes()).hexdigest()
              for name in ("result.jpg", "manifest.json")}

    with pytest.raises(ValueError, match="refusing"):
        remerge_session.remerge(session, session / "result.jpg", deghost=None, force=True)
    out = tmp_path / "out" / "remerged.jpg"
    assert remerge_session.main([str(session), "--out", str(out)]) == 0
    report = json.loads(out.with_name(out.name + ".json").read_text())
    assert [Path(item["path"]).name for item in report["inputs"]] == [f"frame_{i:04d}.jpg" for i in range(4)]
    assert report["output"]["sha256"] == hashlib.sha256(out.read_bytes()).hexdigest()
    assert report["merge"]["deghost"] is True and report["merge"]["frames_merged"] == 4
    assert report["merge"]["maps"] == ["remerged_motion_mask.png"]
    assert (out.parent / "remerged_motion_mask.png").is_file()
    assert "sigma_comparison" not in report  # no night_compare.json beside this session
    assert remerge_session.main([str(session), "--out", str(out)]) == 1  # exists, no --force
    after = {name: hashlib.sha256((session / name).read_bytes()).hexdigest()
             for name in ("result.jpg", "manifest.json")}
    assert before == after


@pytest.mark.skipif(not (_REAL_SESSION / "frame_0000.jpg").is_file()
                    or os.environ.get("CAMERA_REAL_DATA_TESTS") != "1",
                    reason="private A7 III night session a33ccd64 absent or CAMERA_REAL_DATA_TESTS!=1")
def test_real_a7iii_session_deghosts_the_mover():
    paths = [str(_REAL_SESSION / f"frame_{index:04d}.jpg") for index in range(4)]
    assert all(Path(path).is_file() for path in paths)
    image, report = stacking.long_exposure_with_report(paths, deghost=True)
    assert report["reference_index"] == 1
    assert 0.005 < report["moving_fraction"] < 0.2, report["moving_fraction"]
    mask = report["mask"].astype(np.float64) / 255.0
    assert mask[_REAL_MOVER_BOX].mean() > 0.15, "the person on the couch was not detected as moving"
    assert mask[_REAL_STATIC_PATCH].max() == 0, "static wall patch must stay averaged"
    reference = cv2.cvtColor(cv2.imread(paths[1]), cv2.COLOR_BGR2GRAY)
    result = cv2.cvtColor(image, cv2.COLOR_BGR2GRAY)
    single = _residual_sigma(reference[_REAL_STATIC_PATCH])
    stacked = _residual_sigma(result[_REAL_STATIC_PATCH])
    assert stacked < single, (stacked, single)
