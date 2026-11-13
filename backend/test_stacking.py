"""Synthetic-stack tests for the M2 stacking suite.

Each test generates a burst on disk that exercises one function's core
guarantee, then asserts the merged output beats the inputs on the relevant
metric. Run with:

    /tmp/m2-build/venv/bin/python -m pytest test_stacking.py -v
    # or simply: /tmp/m2-build/venv/bin/python test_stacking.py
"""

from __future__ import annotations

import tempfile
from pathlib import Path

import cv2
import numpy as np
import stacking

_RNG = np.random.default_rng(42)
_IMAGE_SIZE = 256


def _write_stack(images: list[np.ndarray], directory: Path) -> list[str]:
    """Write images to PNG (lossless) and return their paths in order."""
    paths: list[str] = []
    for index, image in enumerate(images):
        path = directory / f"frame_{index:02d}.png"
        if not cv2.imwrite(str(path), image):
            raise RuntimeError(f"failed to write {path}")
        paths.append(str(path))
    return paths


def _clipped_pixel_count(image: np.ndarray) -> int:
    """Count pixels that are fully clipped (pure black or pure white)."""
    gray = cv2.cvtColor(image, cv2.COLOR_BGR2GRAY)
    return int(np.count_nonzero((gray == 0) | (gray == 255)))


def _noise_variance(image: np.ndarray, clean: np.ndarray) -> float:
    """Variance of the residual between an image and the clean reference."""
    residual = image.astype(np.float64) - clean.astype(np.float64)
    return float(residual.var())


def _laplacian_variance(image: np.ndarray) -> float:
    """Global focus measure: variance of the Laplacian (higher = sharper)."""
    gray = cv2.cvtColor(image, cv2.COLOR_BGR2GRAY)
    return float(cv2.Laplacian(gray, cv2.CV_64F).var())


# --------------------------------------------------------------------------- #
# HDR: complementary clipped highlights/shadows -> fewer clipped pixels out.
# --------------------------------------------------------------------------- #
def test_hdr_merge_reduces_clipping() -> None:
    # A high-dynamic-range scene: a bright "sky" band over a dark "foreground"
    # band, each with texture that a correct exposure would preserve.
    half = _IMAGE_SIZE // 2
    scene = np.empty((_IMAGE_SIZE, _IMAGE_SIZE, 3), dtype=np.int16)
    scene[:half] = _RNG.integers(180, 240, (half, _IMAGE_SIZE, 3))  # bright sky
    scene[half:] = _RNG.integers(20, 70, (half, _IMAGE_SIZE, 3))  # dark ground

    # A four-shot bracket. Two underexposed frames protect the bright sky but
    # crush the dark ground to black; two overexposed frames lift the ground
    # but blow the sky to white. Every frame clips one band -> this honours the
    # intel's "more frames over bigger EV steps" while keeping the comparison
    # unambiguous (each single input clips a large region).
    brackets = [
        np.clip(scene - 90, 0, 255).astype(np.uint8),  # strong under
        np.clip(scene - 55, 0, 255).astype(np.uint8),  # mild under
        np.clip(scene + 55, 0, 255).astype(np.uint8),  # mild over
        np.clip(scene + 90, 0, 255).astype(np.uint8),  # strong over
    ]

    with tempfile.TemporaryDirectory() as tmp:
        paths = _write_stack(brackets, Path(tmp))
        fused = stacking.hdr_merge(paths)

    fused_clipped = _clipped_pixel_count(fused)
    # The fusion should clip fewer pixels than the least-clipped single input.
    inputs_min_clipped = min(
        _clipped_pixel_count(frame) for frame in brackets
    )
    assert fused_clipped < inputs_min_clipped, (
        f"fused clips {fused_clipped} px, "
        f"not fewer than best input {inputs_min_clipped} px"
    )


# --------------------------------------------------------------------------- #
# Long exposure: identical base + per-frame noise -> lower output variance.
# --------------------------------------------------------------------------- #
def test_long_exposure_reduces_noise() -> None:
    clean = _RNG.integers(
        60, 196, (_IMAGE_SIZE, _IMAGE_SIZE, 3), dtype=np.uint8
    )
    frame_count = 12
    noisy_frames = []
    for _ in range(frame_count):
        noise = _RNG.normal(0.0, 18.0, clean.shape)
        noisy = np.clip(clean.astype(np.float64) + noise, 0, 255).astype(
            np.uint8
        )
        noisy_frames.append(noisy)

    with tempfile.TemporaryDirectory() as tmp:
        paths = _write_stack(noisy_frames, Path(tmp))
        averaged = stacking.long_exposure(paths)

    single_frame_variance = _noise_variance(noisy_frames[0], clean)
    averaged_variance = _noise_variance(averaged, clean)
    assert averaged_variance < single_frame_variance, (
        f"averaged variance {averaged_variance:.2f} not below "
        f"single-frame {single_frame_variance:.2f}"
    )


# --------------------------------------------------------------------------- #
# Focus stack: each frame sharp in a different half -> sharper combined output.
# --------------------------------------------------------------------------- #
def test_focus_stack_extends_sharpness() -> None:
    # High-frequency texture so blur measurably lowers the Laplacian variance.
    texture = _RNG.integers(
        0, 256, (_IMAGE_SIZE, _IMAGE_SIZE, 3), dtype=np.uint8
    )
    half = _IMAGE_SIZE // 2

    # Frame A: left half sharp, right half blurred.
    frame_a = texture.copy()
    frame_a[:, half:] = cv2.GaussianBlur(texture[:, half:], (0, 0), 6)
    # Frame B: right half sharp, left half blurred.
    frame_b = texture.copy()
    frame_b[:, :half] = cv2.GaussianBlur(texture[:, :half], (0, 0), 6)

    with tempfile.TemporaryDirectory() as tmp:
        paths = _write_stack([frame_a, frame_b], Path(tmp))
        stacked = stacking.focus_stack(paths)

    stacked_focus = _laplacian_variance(stacked)
    input_focus = [_laplacian_variance(frame_a), _laplacian_variance(frame_b)]
    assert all(stacked_focus >= value for value in input_focus), (
        f"stacked focus {stacked_focus:.1f} not >= every input "
        f"{[round(v, 1) for v in input_focus]}"
    )


# --------------------------------------------------------------------------- #
# Crowd remove: static background + a roaming bright box -> output ~= clean bg.
# --------------------------------------------------------------------------- #
def test_crowd_remove_recovers_background() -> None:
    background = _RNG.integers(
        70, 186, (_IMAGE_SIZE, _IMAGE_SIZE, 3), dtype=np.uint8
    )
    box = 48
    # Place the bright "person" at a different location in each frame so that
    # every pixel sees the true background in the large majority of frames.
    positions = [
        (10, 10),
        (10, 150),
        (150, 10),
        (150, 150),
        (80, 80),
        (40, 190),
        (190, 40),
    ]
    frames = []
    for top, left in positions:
        frame = background.copy()
        frame[top:top + box, left:left + box] = 255
        frames.append(frame)

    with tempfile.TemporaryDirectory() as tmp:
        paths = _write_stack(frames, Path(tmp))
        cleaned = stacking.crowd_remove(paths)

    mean_abs_diff = float(
        np.abs(cleaned.astype(np.float64) - background.astype(np.float64)).mean()
    )
    assert mean_abs_diff < 1.0, (
        f"recovered background differs from clean by {mean_abs_diff:.3f} "
        "mean abs (expected < 1.0)"
    )


# --------------------------------------------------------------------------- #
# HDR deghosting: a subject that MOVES between brackets must appear ONCE in the
# merge (at the reference position), not ghosted across every frame's position.
# --------------------------------------------------------------------------- #
def _subject_present(image: np.ndarray, top: int, left: int, box: int,
                     bg_value: int) -> float:
    """Mean absolute deviation of a box region from the flat background value.

    Near 0 -> the region looks like background (no subject there). Large -> the
    moving subject is sitting in that region. This is how we detect a ghost: a
    copy of the subject left behind at a NON-reference position.
    """
    region = image[top:top + box, left:left + box].astype(np.float64)
    return float(np.abs(region - bg_value).mean())


def test_hdr_merge_deghosts_moving_subject() -> None:
    # An HDR scene (bright sky over dark ground) so Mertens fusion is genuinely
    # exercised, with a MID-TONE subject (well-exposed in every frame, so plain
    # Mertens keeps all of its copies -> unmistakable ghosting) that occupies a
    # DIFFERENT location in each of three brackets — a person walking across the
    # frame between shots.
    half = _IMAGE_SIZE // 2
    base = np.empty((_IMAGE_SIZE, _IMAGE_SIZE, 3), dtype=np.uint8)
    base[:half] = 205   # bright sky band (flat -> deterministic)
    base[half:] = 70    # dark ground band

    box = 36
    subject_value = 150               # mid-tone: not clipped at any exposure
    subject_row = half + 10           # subject sits on the dark ground band
    # Frame 1 is the best-exposed (EV 0) -> the deghost REFERENCE. It holds the
    # subject on the LEFT. The under/over frames hold it RIGHT and CENTER. Plain
    # Mertens shows all three (ghosts); deghost must keep only the reference (LEFT).
    left_left, left_center, left_right = 30, 90, 150
    positions = [(subject_row, left_right),   # frame 0 (under):     subject RIGHT
                 (subject_row, left_left),    # frame 1 (reference): subject LEFT
                 (subject_row, left_center)]  # frame 2 (over):      subject CENTER
    ev_shift = [-60, 0, 60]
    frames = []
    for (top, left), shift in zip(positions, ev_shift):
        frame = np.clip(base.astype(np.int16) + shift, 0, 255).astype(np.uint8)
        frame[top:top + box, left:left + box] = subject_value  # the moving subject
        frames.append(frame)

    with tempfile.TemporaryDirectory() as tmp:
        paths = _write_stack(frames, Path(tmp))
        # Ghosting is a property of BLENDING several frames, so it is exercised on the fused
        # render. Since 2026-09-20 the default merge keeps the metered bracket outside blown
        # highlights, which is a stronger guarantee and is asserted separately at the end.
        deghosted = stacking.hdr_merge(paths, deghost=True, recover=False)
        plain = stacking.hdr_merge(paths, deghost=False, recover=False)
        # This scene clips nothing, so force the recovery finish to run rather than take the
        # nothing-to-recover shortcut (which would trivially return one bracket).
        threshold = stacking.HDR_NOTHING_TO_RECOVER_FRACTION
        stacking.HDR_NOTHING_TO_RECOVER_FRACTION = 0.0
        try:
            recovered = stacking.hdr_merge(paths, deghost=False)
        finally:
            stacking.HDR_NOTHING_TO_RECOVER_FRACTION = threshold

    # Measure each region's subject content as its deviation from the clean ground
    # band in the SAME image — a flat full-width strip BELOW the subject row that no
    # subject ever covers, so the deghost-vs-plain comparison is not biased by a
    # global brightness change. Textureless MTB estimates are now rejected,
    # preserving the intended coordinates instead of its previous edge artifacts.
    def clean_ground(image: np.ndarray) -> float:
        strip = image[half + 70:half + 100, :]
        return float(strip.reshape(-1, 3).mean(axis=1).mean())

    bg_deghost = clean_ground(deghosted)
    bg_plain = clean_ground(plain)
    ref_left = _subject_present(deghosted, subject_row, left_left, box, bg_deghost)
    ghost_center = _subject_present(deghosted, subject_row, left_center, box, bg_deghost)
    ghost_right = _subject_present(deghosted, subject_row, left_right, box, bg_deghost)
    plain_left = _subject_present(plain, subject_row, left_left, box, bg_plain)
    plain_right = _subject_present(plain, subject_row, left_right, box, bg_plain)

    # 1. The plain blend really DOES ghost: the under-exposed frame's subject copy
    #    (RIGHT) shows up strongly. (Guards against a scene that never ghosts.)
    assert plain_right > 40.0, (
        f"plain fusion did not ghost as expected (right {plain_right:.1f}) "
        "— test scene invalid"
    )
    # 2. The reference subject (LEFT) survives deghosting at full strength (it is in
    #    fact strengthened, since deghost replaces the blended copy with the pure
    #    reference frame there).
    assert ref_left >= plain_left and ref_left > 40.0, (
        f"reference subject not preserved after deghost "
        f"(deghost {ref_left:.1f} vs plain {plain_left:.1f})"
    )
    # 3. BOTH moved copies are cleaned back to near-background: the strong RIGHT
    #    ghost is cut to under a quarter of the plain blend, and neither non-
    #    reference position shows a subject (deviation < 15) any longer.
    assert ghost_right < 0.25 * plain_right, (
        f"deghost did not strongly suppress the right ghost "
        f"({ghost_right:.1f} vs plain {plain_right:.1f})"
    )
    # 5. The recovery finish removes the opportunity entirely for a mid-tone subject: the
    #    output there IS one bracket, so there is nothing to ghost even with deghost off.
    bg_recovered = clean_ground(recovered)
    assert _subject_present(recovered, subject_row, left_center, box, bg_recovered) < 5.0
    assert _subject_present(recovered, subject_row, left_right, box, bg_recovered) < 5.0
    assert _subject_present(recovered, subject_row, left_left, box, bg_recovered) > 40.0, (
        "the metered bracket's own subject must survive")

    assert ghost_center < 15.0 and ghost_right < 15.0, (
        f"ghosts remain at non-reference positions "
        f"(center {ghost_center:.1f}, right {ghost_right:.1f}; want < 15)"
    )


# --------------------------------------------------------------------------- #
# RAW (.ARW) decode is deliberately tombstoned until a decoder can pre-bound its
# native allocation. A JPEG twin remains the bounded fallback.
# --------------------------------------------------------------------------- #
def test_raw_decode_is_explicitly_disabled() -> None:
    try:
        stacking._decode_raw("anything.ARW")
    except ImportError as exc:
        assert "disabled" in str(exc)
    else:
        raise AssertionError("RAW decoder must remain disabled until it has a pre-decode bound")


def test_raw_falls_back_to_jpeg_twin_when_direct_decode_is_disabled() -> None:
    # Camera-free: RAW is always diverted to the JPEG twin Sony writes beside it.
    twin = _RNG.integers(0, 256, (64, 64, 3), dtype=np.uint8)
    with tempfile.TemporaryDirectory() as tmp:
        arw = Path(tmp) / "DSC00001.ARW"
        jpg = Path(tmp) / "DSC00001.JPG"
        arw.write_bytes(b"\x00\x01\x02not-a-real-raw")   # undecodable RAW bytes
        if not cv2.imwrite(str(jpg), twin):
            raise RuntimeError("failed to write JPEG twin")
        decoded = stacking._read_one(str(arw))

    # Falls back to the JPEG twin -> a valid 3-channel image of the twin's shape.
    assert decoded.shape == twin.shape, (
        f"JPEG-twin fallback shape {decoded.shape} != twin {twin.shape}"
    )


def test_raw_suffix_is_checked_before_any_raw_file_read() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        arw = Path(tmp) / "DSC00002.ARW"
        jpg = Path(tmp) / "DSC00002.JPG"
        arw.write_bytes(b"raw bytes must not be opened")
        if not cv2.imwrite(str(jpg), _RNG.integers(0, 256, (32, 32, 3), dtype=np.uint8)):
            raise RuntimeError("failed to write JPEG twin")
        original_source_bytes = __import__("image_safety").source_bytes
        reads: list[Path] = []

        def guarded_source_bytes(path):
            reads.append(Path(path))
            if Path(path).suffix.lower() == ".arw":
                raise AssertionError("RAW path was read before suffix refusal/fallback")
            return original_source_bytes(path)

        import image_safety
        image_safety.source_bytes = guarded_source_bytes
        try:
            decoded = stacking._read_one(arw)
        finally:
            image_safety.source_bytes = original_source_bytes

    assert decoded.shape == (32, 32, 3)
    assert reads == [jpg]


# --------------------------------------------------------------------------- #
# HDR deghost must NOT flag a STATIC near-clipped region as motion. A bright
# highlight that one bracket blows to white while another holds detail disagrees
# on content there only because of CLIPPING, not subject motion — the moving mask
# must stay near zero so the multi-frame fuse (and its recovered detail) is kept
# rather than overwritten by the single reference frame. Targets _moving_mask
# directly so MTB-shift artifacts don't confound the clip-suppression assertion.
# --------------------------------------------------------------------------- #
def test_deghost_mask_ignores_static_clipped_region() -> None:
    size = 128
    # Reference holds the highlight as recoverable detail (values just under clipping).
    reference = np.full((size, size, 3), 60, dtype=np.uint8)   # dark, well-exposed surround
    patch_top, patch_left, patch = 30, 30, 50
    detail = _RNG.integers(200, 246, (patch, patch, 3), dtype=np.uint8)  # textured, NOT clipped
    reference[patch_top:patch_top + patch, patch_left:patch_left + patch] = detail

    # A brighter bracket of the SAME static scene: the highlight patch clips to pure white
    # (255 >= 247) while the surround stays unclipped. Nothing moved — the only place the
    # frame disagrees with the reference on content is the clipped patch.
    frame = reference.copy()
    frame[patch_top:patch_top + patch, patch_left:patch_left + patch] = 255

    mask = stacking._moving_mask(frame, reference)

    patch_mask = mask[patch_top:patch_top + patch, patch_left:patch_left + patch]
    # The clipped patch must be (almost) fully suppressed — pre-fix, exposure-matching a
    # clipped level can't reproduce the reference detail, so the whole patch read as motion.
    assert float(patch_mask.mean()) < 0.05, (
        f"static near-clipped region was flagged as motion (mask mean {patch_mask.mean():.3f}); "
        "deghost would erase the recovered HDR detail there"
    )


# --------------------------------------------------------------------------- #
# And the converse guard: a GENUINE moving subject (unclipped content that differs)
# must still be flagged, so the clip suppression didn't blind the deghost to motion.
# --------------------------------------------------------------------------- #
def test_deghost_mask_still_catches_unclipped_motion() -> None:
    size = 128
    # Textured mid-tone background (so exposure-matching has a real tone curve to map).
    reference = _RNG.integers(90, 150, (size, size, 3), dtype=np.uint8)
    frame = reference.copy()
    # A dark subject (unclipped: well above the floor) sitting where the reference is plain
    # background — genuine content disagreement that must survive the clip suppression.
    frame[40:80, 40:80] = _RNG.integers(20, 55, (40, 40, 3), dtype=np.uint8)
    mask = stacking._moving_mask(frame, reference)
    assert float(mask[50:70, 50:70].mean()) > 0.5, (
        "an unclipped moving subject must still be flagged as motion"
    )


# --------------------------------------------------------------------------- #
# RAW decoding remains unavailable until it can pre-bound native allocations.
# --------------------------------------------------------------------------- #
def _run_all() -> int:
    """Run every test directly (no pytest) and report pass/fail per test."""
    tests = [
        test_hdr_merge_reduces_clipping,
        test_hdr_merge_deghosts_moving_subject,
        test_deghost_mask_ignores_static_clipped_region,
        test_deghost_mask_still_catches_unclipped_motion,
        test_long_exposure_reduces_noise,
        test_focus_stack_extends_sharpness,
        test_crowd_remove_recovers_background,
        test_raw_decode_is_explicitly_disabled,
        test_raw_falls_back_to_jpeg_twin_when_direct_decode_is_disabled,
        test_raw_suffix_is_checked_before_any_raw_file_read,
    ]
    failures = 0
    for test in tests:
        try:
            test()
            print(f"PASS  {test.__name__}")
        except AssertionError as error:
            failures += 1
            print(f"FAIL  {test.__name__}: {error}")
    print(f"\n{len(tests) - failures}/{len(tests)} tests passed")
    return failures


if __name__ == "__main__":
    raise SystemExit(_run_all())
