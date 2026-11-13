"""B07 sharpness-evidence tests for focus_verify.sharpness_evidence / compare_sharpness.

The verdict is deliberately RELATIVE (subject vs the frame's own most-detailed region), so the
synthetic frames here exercise the honest cases: sharp everywhere, a blurred subject inside an
otherwise-sharp frame, a flat frame with no reference detail, correct box scaling after downscale,
determinism, band-size invariance, comparison ordering, and use of the guarded opener.
"""
from pathlib import Path
import sys

import cv2
import numpy as np
from PIL import Image

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))

import focus_verify

_RNG_SEED = 7


def _textured_frame(width: int = 2400, height: int = 1600) -> np.ndarray:
    """A frame with real high-frequency detail everywhere (deterministic RNG)."""
    return np.random.default_rng(_RNG_SEED).integers(0, 256, (height, width, 3), dtype="uint8")


def _save(path: Path, rgb: np.ndarray) -> str:
    Image.fromarray(rgb).save(path)
    return str(path)


def test_sharp_everywhere_is_sharp(tmp_path) -> None:
    src = _save(tmp_path / "sharp.png", _textured_frame())
    ev = focus_verify.sharpness_evidence(src)
    assert ev["verdict"] == "sharp", ev
    assert ev["calibration"] == "uncalibrated_relative"
    assert ev["laplacian_var"] > 0 and ev["tenengrad"] > 0
    assert ev["subject_laplacian_var"] is None and ev["subject_box_used"] is None
    print(f"  sharp-everywhere: {ev['verdict']} ({ev['reason']})")


def test_blurred_subject_is_soft_and_scores_lower(tmp_path) -> None:
    frame = _textured_frame()
    box = (900, 500, 700, 600)  # x, y, w, h in original pixels
    x, y, w, h = box
    sharp_src = _save(tmp_path / "frame.png", frame)
    blurred = frame.copy()
    patch = cv2.GaussianBlur(blurred[y : y + h, x : x + w], (0, 0), 8)
    blurred[y : y + h, x : x + w] = patch
    blurred_src = _save(tmp_path / "frame_blur.png", blurred)

    sharp_ev = focus_verify.sharpness_evidence(sharp_src, subject_box=box)
    soft_ev = focus_verify.sharpness_evidence(blurred_src, subject_box=box)
    assert sharp_ev["verdict"] == "sharp", sharp_ev
    assert soft_ev["verdict"] == "soft", soft_ev
    assert soft_ev["subject_laplacian_var"] < sharp_ev["subject_laplacian_var"]
    assert soft_ev["subject_tenengrad"] < sharp_ev["subject_tenengrad"]
    print(
        f"  blurred subject: soft={soft_ev['subject_laplacian_var']:.0f} "
        f"< sharp={sharp_ev['subject_laplacian_var']:.0f}"
    )


def test_flat_frame_is_undetermined(tmp_path) -> None:
    flat = np.full((1600, 2400, 3), 128, dtype="uint8")
    src = _save(tmp_path / "flat.png", flat)
    ev = focus_verify.sharpness_evidence(src)
    assert ev["verdict"] == "undetermined", ev
    assert "floor" in ev["reason"]
    print(f"  flat frame: {ev['verdict']} ({ev['reason']})")


def test_box_scaling_maps_to_the_right_region(tmp_path) -> None:
    """Texture only inside the box; the scaled crop must land on it, so the subject score dwarfs the
    (mostly-flat) whole-frame score even though the box is given in original coordinates."""
    height, width = 1600, 2400
    frame = np.full((height, width, 3), 128, dtype="uint8")
    box = (1500, 200, 700, 700)
    x, y, w, h = box
    noise = np.random.default_rng(_RNG_SEED).integers(0, 256, (h, w, 3), dtype="uint8")
    frame[y : y + h, x : x + w] = noise
    src = _save(tmp_path / "boxed.png", frame)

    ev = focus_verify.sharpness_evidence(src, subject_box=box, working_width=1600)
    assert ev["subject_box_used"] is not None
    # subject crop is full of detail; a WRONG box mapping would land on flat grey (~0), so a large
    # subject score that also dwarfs the whole-frame average proves the box scaled onto the texture.
    assert ev["subject_laplacian_var"] > 3000, ev
    assert ev["subject_laplacian_var"] > ev["laplacian_var"] * 4, ev
    # scaled box origin should sit in the right half (x ~ 1500/2400 of the working width).
    sx0 = ev["subject_box_used"][0]
    assert sx0 > ev["working_width"] * 0.5, ev
    print(
        f"  box scaling: subject {ev['subject_laplacian_var']:.0f} >> frame "
        f"{ev['laplacian_var']:.0f}; scaled x0={sx0}"
    )


def test_deterministic_across_runs(tmp_path) -> None:
    src = _save(tmp_path / "det.png", _textured_frame())
    box = (900, 500, 700, 600)
    first = focus_verify.sharpness_evidence(src, subject_box=box)
    second = focus_verify.sharpness_evidence(src, subject_box=box)
    assert first == second, (first, second)
    print("  determinism: two runs identical")


def test_band_size_invariance_keeps_verdict(tmp_path) -> None:
    frame = _textured_frame()
    box = (900, 500, 700, 600)
    x, y, w, h = box
    blurred = frame.copy()
    blurred[y : y + h, x : x + w] = cv2.GaussianBlur(frame[y : y + h, x : x + w], (0, 0), 8)
    sharp_src = _save(tmp_path / "bi_sharp.png", frame)
    soft_src = _save(tmp_path / "bi_soft.png", blurred)

    for width in (1200, 1600):
        sharp_ev = focus_verify.sharpness_evidence(sharp_src, subject_box=box, working_width=width)
        soft_ev = focus_verify.sharpness_evidence(soft_src, subject_box=box, working_width=width)
        assert sharp_ev["verdict"] == "sharp", (width, sharp_ev)
        assert soft_ev["verdict"] == "soft", (width, soft_ev)
    print("  band-size invariance: verdicts stable at 1200 and 1600")


def test_compare_sharpness_ordering(tmp_path) -> None:
    frame = _textured_frame()
    box = (900, 500, 700, 600)
    x, y, w, h = box
    blurred = frame.copy()
    blurred[y : y + h, x : x + w] = cv2.GaussianBlur(frame[y : y + h, x : x + w], (0, 0), 8)
    sharp_ev = focus_verify.sharpness_evidence(_save(tmp_path / "c_sharp.png", frame), subject_box=box)
    soft_ev = focus_verify.sharpness_evidence(_save(tmp_path / "c_soft.png", blurred), subject_box=box)

    final_vs_meter = focus_verify.compare_sharpness(sharp_ev, soft_ev)
    assert final_vs_meter["sharper"] == "a" and final_vs_meter["ratio"] > 1.0, final_vs_meter
    reversed_cmp = focus_verify.compare_sharpness(soft_ev, sharp_ev)
    assert reversed_cmp["sharper"] == "b" and reversed_cmp["ratio"] < 1.0, reversed_cmp
    assert focus_verify.compare_sharpness(sharp_ev, sharp_ev)["sharper"] == "equal"
    print(f"  compare ordering: ratio final/meter={final_vs_meter['ratio']:.2f}")


def test_uses_guarded_opener_not_pil_open() -> None:
    """The module must decode through image_safety.open_rgb, never a bare Image.open."""
    source = Path(focus_verify.__file__).read_text(encoding="utf-8")
    assert "Image.open(" not in source, "focus_verify must use the guarded open_rgb, not Image.open"
    assert "open_rgb" in source
    print("  guarded opener: no Image.open( in focus_verify.py")


if __name__ == "__main__":
    import tempfile

    with tempfile.TemporaryDirectory() as d:
        base = Path(d)
        test_sharp_everywhere_is_sharp(base)
        test_blurred_subject_is_soft_and_scores_lower(base)
        test_flat_frame_is_undetermined(base)
        test_box_scaling_maps_to_the_right_region(base)
        test_deterministic_across_runs(base)
        test_band_size_invariance_keeps_verdict(base)
        test_compare_sharpness_ordering(base)
        test_uses_guarded_opener_not_pil_open()
    print("focus_verify evidence tests: PASS")


from test_final_wave_engine import rig  # noqa: E402,F401


def test_engine_attaches_sharpness_evidence_to_every_shot(rig):
    shot = rig.real.shoot(None, variant_id="natural")
    sharpness = shot.evidence["sharpness"]
    assert sharpness["calibration"] == "uncalibrated_relative"
    assert sharpness["verdict"] in ("sharp", "soft", "undetermined")
    assert "Sharpness:" in shot.mood["camera"]
