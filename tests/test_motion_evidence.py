"""Verified two-frame motion evidence: honest measurement that can satisfy the stack gate.

Camera-free. Synthetic frame pairs prove that `analyze_frame_pair` (a) measures the whole-frame
moving-pixel fraction, (b) certifies `outcome_verified`/`status="measured"` only when the flow is
trustworthy, and (c) that `motion_evidence_for_shot` produces exactly the dict the capture-policy
stack gate accepts, so Auto actually proceeds to `plan_stack`.
"""
from __future__ import annotations

import sys
from pathlib import Path

import cv2
import numpy as np
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))

import capture_analysis
import capture_policy
import motion_texture as mt

_RNG = np.random.default_rng(7)
_SIZE = 320
_DT_S = 0.05


def _texture(size: int = _SIZE) -> np.ndarray:
    noise = _RNG.integers(0, 256, (size, size), dtype=np.uint8)
    smoothed = cv2.GaussianBlur(noise, (0, 0), sigmaX=1.0)
    return cv2.cvtColor(smoothed, cv2.COLOR_GRAY2BGR)


def _shift(image: np.ndarray, dx: float, dy: float = 0.0) -> np.ndarray:
    matrix = np.float32([[1, 0, dx], [0, 1, dy]])
    height, width = image.shape[:2]
    return cv2.warpAffine(image, matrix, (width, height), borderMode=cv2.BORDER_REFLECT)


_PATCH_ORIGIN = 90
_PATCH_SIZE = 140


def _place_patch(frame: np.ndarray, patch: np.ndarray) -> np.ndarray:
    out = frame.copy()
    end = _PATCH_ORIGIN + _PATCH_SIZE
    out[_PATCH_ORIGIN:end, _PATCH_ORIGIN:end] = patch
    return out


def _noise_frame(size: int = _SIZE) -> np.ndarray:
    return cv2.cvtColor(_RNG.integers(0, 256, (size, size), dtype=np.uint8), cv2.COLOR_GRAY2BGR)


# --------------------------------------------------------------------------- #
# motion_fraction and verification semantics
# --------------------------------------------------------------------------- #
def test_static_textured_scene_is_verified_with_no_motion() -> None:
    base = _texture()
    result = mt.analyze_frame_pair(base, base.copy(), _DT_S)
    assert result["outcome_verified"] is True and result["status"] == "measured"
    assert result["motion_fraction"] == pytest.approx(0.0, abs=0.02)
    assert "reason" not in result


def test_moving_square_measures_its_area_fraction_and_verifies() -> None:
    background = _texture()
    patch = _texture(size=_PATCH_SIZE)
    frame_a = _place_patch(background, patch)
    frame_b = _place_patch(background, _shift(patch, 6.0))
    result = mt.analyze_frame_pair(frame_a, frame_b, _DT_S)
    expected = (_PATCH_SIZE ** 2) / (_SIZE ** 2)  # 0.1914
    assert result["outcome_verified"] is True
    assert result["motion_fraction"] == pytest.approx(expected, abs=0.03)


def test_whole_frame_pan_reads_as_no_subject_motion() -> None:
    base = _texture()
    result = mt.analyze_frame_pair(base, _shift(base, 9.0), _DT_S)
    # A uniform camera pan is removed by the robust median -> ~no moving pixels.
    assert result["outcome_verified"] is True
    assert result["motion_fraction"] == pytest.approx(0.0, abs=0.02)


def test_pure_noise_pair_is_unverified_with_reason() -> None:
    result = mt.analyze_frame_pair(_noise_frame(), _noise_frame(), _DT_S)
    assert result["outcome_verified"] is False and result["status"] == "estimated"
    assert "inconsistent" in result["reason"]
    assert result["flow_confidence"] < mt._FLOW_CONFIDENCE_FLOOR


def test_dt_over_two_seconds_still_raises() -> None:
    base = _texture()
    with pytest.raises(ValueError, match=r"\(0, 2\]"):
        mt.analyze_frame_pair(base, base.copy(), 2.5)


def test_guessed_interval_is_not_certified() -> None:
    base = _texture()
    result = mt.analyze_frame_pair(base, _shift(base, 6.0), _DT_S, dt_measured=False)
    assert result["outcome_verified"] is False
    assert "assumed" in result["reason"]


def test_mismatched_sizes_are_unverified_not_raised() -> None:
    result = mt.analyze_frame_pair(_texture(320), _texture(256), _DT_S)
    assert result["outcome_verified"] is False
    assert result["motion_fraction"] is None
    assert "size" in result["reason"]
    # Every existing key survives even in the degenerate branch.
    for key in ("status", "dt_s", "subject_motion_floor_s", "global_motion_floor_s",
                "texture_score", "output_scale"):
        assert key in result


# --------------------------------------------------------------------------- #
# capture_analysis wrappers
# --------------------------------------------------------------------------- #
def _write_pair(tmp_path: Path, frame_a: np.ndarray, frame_b: np.ndarray) -> tuple[str, str]:
    first, second = tmp_path / "a.jpg", tmp_path / "b.jpg"
    assert cv2.imwrite(str(first), frame_a) and cv2.imwrite(str(second), frame_b)
    return str(first), str(second)


def test_motion_between_files_passes_through_verified_fields(tmp_path) -> None:
    base = _texture()
    first, second = _write_pair(tmp_path, base, base.copy())
    motion = capture_analysis.motion_between_files(first, second, _DT_S)
    assert motion["outcome_verified"] is True and motion["status"] == "measured"
    assert isinstance(motion["motion_fraction"], float)
    assert motion["sample_interval_s"] == round(_DT_S, 4)


def test_inconsistent_timestamps_mark_unverified(tmp_path) -> None:
    base = _texture()
    first, second = _write_pair(tmp_path, base, _shift(base, 6.0))
    # Caller-supplied dt says 0.05s but the timestamps span 0.5s -> conflict.
    motion = capture_analysis.motion_between_files(first, second, 0.05,
                                                   frame_timestamps=(10.0, 10.5))
    assert motion["outcome_verified"] is False
    assert "disagrees" in motion["reason"]


def test_consistent_timestamps_stay_verified(tmp_path) -> None:
    base = _texture()
    first, second = _write_pair(tmp_path, base, base.copy())
    motion = capture_analysis.motion_between_files(first, second, 0.05,
                                                   frame_timestamps=(10.0, 10.05))
    assert motion["outcome_verified"] is True


def test_mismatched_files_report_unavailable_not_crash(tmp_path) -> None:
    first, second = _write_pair(tmp_path, _texture(320), _texture(256))
    motion = capture_analysis.motion_between_files(first, second, _DT_S)
    # analyze_frame_pair returns an unverified (size) record; the wrapper keeps it honest.
    assert motion["outcome_verified"] is False


def test_usb_motion_unavailable_shape() -> None:
    record = capture_analysis.usb_motion_unavailable("USB stills transfer too far apart")
    assert record == {"status": "unavailable", "subject_motion_floor_s": None,
                      "outcome_verified": False, "reason": "USB stills transfer too far apart"}


# --------------------------------------------------------------------------- #
# The projected evidence must actually pass the capture-policy stack gate.
# --------------------------------------------------------------------------- #
class _FakeShot:
    def __init__(self, motion_evidence: dict) -> None:
        self.ok = True
        self.evidence = {"motion": motion_evidence}


def _clipped_jpeg(tmp_path: Path) -> str:
    """A 64x64 frame that is half pure white, half pure black: highlight + shadow clipping."""
    image = np.zeros((64, 64, 3), dtype=np.uint8)
    image[:32] = 255
    path = tmp_path / "accepted.jpg"
    assert cv2.imwrite(str(path), image)
    return str(path)


def test_verified_low_motion_lets_auto_proceed_to_stack(tmp_path) -> None:
    base = _texture()
    analysis = mt.analyze_frame_pair(base, base.copy(), _DT_S)  # verified, fraction ~0 (<=0.08)
    evidence = capture_analysis.motion_evidence_for_shot(analysis)
    policy = capture_policy.validate_stack_policy({"mode": "auto", "frames": 3})
    decision = capture_policy.decide_stack_policy(policy, _FakeShot(evidence),
                                                  _clipped_jpeg(tmp_path))
    # The gate accepted the evidence: Auto reached plan_stack on the accepted JPEG...
    assert decision["evidence_source"] == "accepted_jpeg_and_verified_motion"
    # ...and low motion + dual clipping yields a real HDR stack, not a withheld single.
    assert decision["mode"] == "hdr" and decision["frames"] == 3


def test_verified_high_motion_withholds_stack_but_still_passes_gate(tmp_path) -> None:
    background = _texture()
    patch = _texture(size=_PATCH_SIZE)
    frame_b = _place_patch(background, _shift(patch, 6.0))
    analysis = mt.analyze_frame_pair(_place_patch(background, patch), frame_b, _DT_S)
    assert analysis["motion_fraction"] > 0.08  # a genuinely moving subject
    evidence = capture_analysis.motion_evidence_for_shot(analysis)
    policy = capture_policy.validate_stack_policy({"mode": "auto", "frames": 3})
    decision = capture_policy.decide_stack_policy(policy, _FakeShot(evidence),
                                                  _clipped_jpeg(tmp_path))
    # Gate passed (verified path), but plan_stack withholds stacking to avoid duplicate subjects.
    assert decision["evidence_source"] == "accepted_jpeg_and_verified_motion"
    assert decision["mode"] == "single" and "duplicate" in decision["reason"].lower()


def test_unverified_evidence_is_rejected_by_the_gate(tmp_path) -> None:
    analysis = mt.analyze_frame_pair(_noise_frame(), _noise_frame(), _DT_S)  # unverified
    evidence = capture_analysis.motion_evidence_for_shot(analysis)
    assert evidence["outcome_verified"] is False
    policy = capture_policy.validate_stack_policy({"mode": "auto", "frames": 3})
    decision = capture_policy.decide_stack_policy(policy, _FakeShot(evidence),
                                                  _clipped_jpeg(tmp_path))
    assert decision["evidence_source"] == "existing_shot_motion_unavailable"
    assert decision["mode"] == "single"
