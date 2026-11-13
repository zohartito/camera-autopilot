"""tools/night_receipt_compare.py — verify the sigma->N rule check against synthetic night sessions.

No camera, no model. Each test builds a fake session directory: flat mid-grey frames with a known
gaussian sigma, a synthetic averaged result (noise/sqrt(N)), and a manifest carrying a
night_assist plan. The comparison measures the frames with the calibrator's flat-patch residual
method and must recover the known sigma, the 10*log10(N) averaging gain, and the right verdict.
"""
from __future__ import annotations

import json
import math
import sys
from pathlib import Path

import numpy as np
from PIL import Image

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tools"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import night_receipt_compare as nrc
from _runner import run_module_tests

_SIZE = 512  # 8x8 = 64 CELL(64) cells -> plenty of mid-tone flat cells for the default 24


def _grey_noise(sigma: float, seed: int) -> np.ndarray:
    rng = np.random.default_rng(seed)
    return np.clip(128.0 + rng.normal(0.0, sigma, (_SIZE, _SIZE)), 0, 255).astype(np.uint8)


def _save(path: Path, array: np.ndarray) -> None:
    # quality=100, no chroma subsampling: keep the luma noise the test injected.
    Image.fromarray(array, "L").save(path, quality=100, subsampling=0)


def _build_session(directory: Path, *, sigma: float, frames: int, planned_frames: int | None = None,
                   result_sigma: float | None = None, accepted: int | None = None,
                   include_plan: bool = True, seed: int = 100,
                   sigma_base_iso: float | None = None) -> str:
    """Write frame_NNNN.jpg, result.jpg and manifest.json into `directory`; return the session id.

    result_sigma defaults to the ideal sigma/sqrt(frames). `accepted` (default all) marks the first
    `accepted` frames accepted and the rest rejected. planned_frames defaults to `frames`.
    """
    directory.mkdir(parents=True, exist_ok=True)
    planned_frames = planned_frames or frames
    accepted = frames if accepted is None else accepted
    manifest_frames = []
    for i in range(frames):
        _save(directory / f"frame_{i:04d}.jpg", _grey_noise(sigma, seed + i))
        manifest_frames.append({"index": i,
                                "status": "accepted" if i < accepted else "rejected"})
    res_sigma = sigma / math.sqrt(planned_frames) if result_sigma is None else result_sigma
    _save(directory / "result.jpg", _grey_noise(res_sigma, seed + 9000))

    session_id = directory.name
    ev = {"noise_source": "measured_jpeg_noise", "iso": 3200, "shutter_s": 1.0,
          "sigma_at_iso": round(sigma, 4),
          "sigma_after_average": round(sigma / math.sqrt(planned_frames), 4),
          "frames": planned_frames}
    if sigma_base_iso is not None:
        ev["sigma_base_iso"] = round(sigma_base_iso, 4)
    metadata = {"camera_model": "TEST", "camera_port": "usb:001,001"}
    if include_plan:
        metadata["night_assist"] = {"iso": "3200", "aperture_label": "f/2.8",
                                    "shutter_label": "1", "frames": planned_frames, "evidence": ev}
    manifest = {"id": session_id, "mode": "night", "status": "completed",
                "frame_count": frames, "frames": manifest_frames, "metadata": metadata}
    (directory / "manifest.json").write_text(json.dumps(manifest, indent=2))
    return session_id


# --- tests ------------------------------------------------------------------------------------ #
def test_measured_sigma_matches_known(tmp_path):
    """Per-frame flat-patch sigma recovers the injected gaussian sigma within 10%."""
    session_id = _build_session(tmp_path / "s1", sigma=12.0, frames=9)
    report = nrc.compare(tmp_path / "s1", session_id, cells_count=nrc.calib.DEFAULT_CELLS)
    measured = report["measured"]["per_frame_sigma_median"]
    assert abs(measured - 12.0) / 12.0 <= 0.10, report["measured"]


def test_measured_gain_matches_10logN_and_rule_holds(tmp_path):
    """An ideal noise/sqrt(N) result gives measured gain ~ 10*log10(N) (+/-0.5 dB), verdict rule_holds."""
    session_id = _build_session(tmp_path / "s2", sigma=12.0, frames=9)
    report = nrc.compare(tmp_path / "s2", session_id, cells_count=nrc.calib.DEFAULT_CELLS)
    planned_gain = 10.0 * math.log10(9)
    assert abs(report["measured"]["gain_db"] - planned_gain) <= 0.5, report
    assert abs(report["measured"]["gain_db"] - report["planned"]["gain_db"]) <= 0.5
    assert report["verdict"] == "rule_holds", report
    assert report["informational"]["needed_frames_for_ideal_relative_gain"] == 9


def test_result_sigma_near_planned_after_average(tmp_path):
    """Measured result sigma tracks the planned sigma_after_average (sigma/sqrt(N)) within 12%."""
    session_id = _build_session(tmp_path / "s3", sigma=12.0, frames=9)
    report = nrc.compare(tmp_path / "s3", session_id, cells_count=nrc.calib.DEFAULT_CELLS)
    planned_after = report["planned"]["sigma_after_average"]
    measured_after = report["measured"]["result_sigma"]
    assert abs(measured_after - planned_after) / planned_after <= 0.12, report


def test_half_gain_under_delivers_needs_more_frames(tmp_path):
    """A result with only ~half the expected dB gain -> under_delivers, needed_frames > planned."""
    # Ideal result would be sigma/4 (N=16); sigma/16**0.25 = sigma/2 delivers half the dB gain.
    session_id = _build_session(tmp_path / "s4", sigma=12.0, frames=16,
                                result_sigma=12.0 / (16 ** 0.25))
    report = nrc.compare(tmp_path / "s4", session_id, cells_count=nrc.calib.DEFAULT_CELLS)
    assert report["verdict"] == "under_delivers", report
    needed = report["informational"]["needed_frames_for_ideal_relative_gain"]
    assert needed is not None and needed > 16, report
    assert report["measured"]["gain_db"] < report["planned"]["gain_db"]


def test_over_delivers_needs_fewer_frames(tmp_path):
    """A result cleaner than sqrt(N) predicts -> over_delivers with a smaller sufficient N."""
    # sigma/8 from a planned N=4 stack is far beyond the planned sigma/2 (6 dB) reduction.
    session_id = _build_session(tmp_path / "s5", sigma=12.0, frames=4, result_sigma=12.0 / 8.0)
    report = nrc.compare(tmp_path / "s5", session_id, cells_count=nrc.calib.DEFAULT_CELLS)
    assert report["verdict"] == "over_delivers", report
    needed = report["informational"]["needed_frames_for_ideal_relative_gain"]
    assert needed is not None and needed < 4, report


def test_missing_plan_is_a_clear_error(tmp_path):
    """A night session with no night_assist plan raises a clear ValueError, not a crash."""
    session_id = _build_session(tmp_path / "s6", sigma=12.0, frames=9, include_plan=False)
    try:
        nrc.compare(tmp_path / "s6", session_id, cells_count=nrc.calib.DEFAULT_CELLS)
    except ValueError as exc:
        assert "no night_assist plan" in str(exc), exc
    else:
        raise AssertionError("expected a ValueError for the missing night_assist plan")


def test_only_accepted_frames_are_measured(tmp_path):
    """Rejected frames are ignored: accepted_frames reflects only the accepted subset."""
    session_id = _build_session(tmp_path / "s7", sigma=12.0, frames=9, accepted=5)
    report = nrc.compare(tmp_path / "s7", session_id, cells_count=nrc.calib.DEFAULT_CELLS)
    assert report["accepted_frames"] == 5, report
    assert len(report["measured"]["per_frame_sigmas"]) == 5, report


def test_deterministic_same_inputs_same_report(tmp_path):
    """The same session yields byte-identical reports across runs (fixed cells, fixed order)."""
    session_id = _build_session(tmp_path / "s8", sigma=12.0, frames=9)
    first = nrc.compare(tmp_path / "s8", session_id, cells_count=nrc.calib.DEFAULT_CELLS)
    second = nrc.compare(tmp_path / "s8", session_id, cells_count=nrc.calib.DEFAULT_CELLS)
    assert json.dumps(first, sort_keys=True) == json.dumps(second, sort_keys=True)


def test_run_writes_night_compare_json(tmp_path):
    """The CLI writes <session dir>/night_compare.json with the verdict, and returns 0."""
    sessions_dir = tmp_path / "sessions"
    session_id = _build_session(sessions_dir / "sess9", sigma=12.0, frames=9)
    code = nrc.run(["--session", session_id, "--sessions-dir", str(sessions_dir)])
    assert code == 0
    written = sessions_dir / "sess9" / "night_compare.json"
    assert written.is_file(), "night_compare.json was not written"
    payload = json.loads(written.read_text())
    assert payload["session_id"] == session_id
    assert payload["verdict"] == "rule_holds"
    assert payload["method"] == "jpeg_flat_patch_residual"


def test_run_from_receipt_resolves_session(tmp_path):
    """--receipt reads the session_id from a night_session.py-style receipt and compares it."""
    sessions_dir = tmp_path / "sessions"
    session_id = _build_session(sessions_dir / "sessA", sigma=12.0, frames=9)
    receipt = tmp_path / "night-session-20260908T000000Z.json"
    receipt.write_text(json.dumps({"what": "night-session", "passed": True,
                                   "session_id": session_id, "frames": 9}))
    code = nrc.run(["--receipt", str(receipt), "--sessions-dir", str(sessions_dir)])
    assert code == 0
    assert (sessions_dir / "sessA" / "night_compare.json").is_file()


if __name__ == "__main__":
    raise SystemExit(run_module_tests(__name__))


def test_frames_at_the_jpeg_noise_floor_are_not_blamed_on_the_plan(tmp_path, monkeypatch):
    """Base-ISO frames with sigma at the encoder floor: averaging cannot help, the verdict says so,
    and the needed-frames estimate never explodes into billions."""
    import night_receipt_compare as nrc
    session_id = _build_session(tmp_path / "s8", sigma=0.35, frames=2, result_sigma=0.34)
    # The synthetic generator's own residual floor is ~0.73 (8-bit quantization of the injected
    # noise), above the real-JPEG floor the tool uses; raise the threshold to exercise the branch.
    monkeypatch.setattr(nrc, "JPEG_NOISE_FLOOR_SIGMA", 0.8)
    report = nrc.compare(tmp_path / "s8", session_id, cells_count=nrc.calib.DEFAULT_CELLS)
    assert report["verdict"] == "at_noise_floor"
    assert report["informational"]["needed_frames_for_ideal_relative_gain"] == 2


def test_under_delivers_relative_but_meets_absolute_target(tmp_path):
    """The real A7 III case: relative averaging gain falls short of the ideal 10*log10(N), yet the
    result still lands under the calibrated absolute target. Overall verdict is meets_target, the
    relative under_delivers verdict is preserved, and the frame extrapolation stays informational."""
    # sigma 12, N=4: ideal gain 6.02 dB (result sigma/2 = 6). result_sigma 9 -> ~2.5 dB, under_delivers.
    # Absolute target = sigma_base_iso 8.0 * 1.25 = 10.0, comfortably above the measured ~9 result.
    session_id = _build_session(tmp_path / "t1", sigma=12.0, frames=4,
                                result_sigma=9.0, sigma_base_iso=8.0)
    report = nrc.compare(tmp_path / "t1", session_id, cells_count=nrc.calib.DEFAULT_CELLS)
    assert report["verdict"] == "under_delivers", report
    assert report["overall_verdict"] == "meets_target", report["final_noise_target"]
    assert report["final_noise_target"]["verdict"] == "meets_target", report["final_noise_target"]
    assert report["measured"]["result_sigma"] <= report["final_noise_target"]["target_sigma"], report
    # The extrapolation is present but explicitly demoted and did not flip the overall verdict.
    assert report["informational"]["one_point_extrapolation"] == "not a prescription"
    assert report["informational"]["needed_frames_for_ideal_relative_gain"] is not None


def test_misses_relative_and_absolute_target_stays_a_miss(tmp_path):
    """When the result misses both the relative gain and the absolute target, overall is misses_target."""
    # Same under-delivering result (~9), but target = sigma_base_iso 5.0 * 1.25 = 6.25, below ~9.
    session_id = _build_session(tmp_path / "t2", sigma=12.0, frames=4,
                                result_sigma=9.0, sigma_base_iso=5.0)
    report = nrc.compare(tmp_path / "t2", session_id, cells_count=nrc.calib.DEFAULT_CELLS)
    assert report["verdict"] == "under_delivers", report
    assert report["overall_verdict"] == "misses_target", report["final_noise_target"]
    assert report["final_noise_target"]["verdict"] == "misses_target", report["final_noise_target"]
    assert report["measured"]["result_sigma"] > report["final_noise_target"]["target_sigma"], report


def test_no_base_iso_sigma_yields_no_target(tmp_path):
    """A manifest without sigma_base_iso cannot yield an absolute target: overall is no_target."""
    session_id = _build_session(tmp_path / "t3", sigma=12.0, frames=9)  # no sigma_base_iso
    report = nrc.compare(tmp_path / "t3", session_id, cells_count=nrc.calib.DEFAULT_CELLS)
    assert report["overall_verdict"] == "no_target", report
    assert report["final_noise_target"]["verdict"] == "no_target", report
    assert report["final_noise_target"]["target_sigma"] is None, report
