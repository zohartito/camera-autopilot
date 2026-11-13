"""When the metered frame is barely clipped, HDR hands back the metered frame.

Owner decision (0.2 sweep, 2026-09-27): below about 0.5 % clipped pixels there is nothing
worth recovering, so running the merge can only cost the photograph something. HDR returns
the metered frame, says "nothing to recover" in the summary and evidence, and records the
measured clipped share. "Clipped" is exactly what the recovery finish treats as blown: LAB L
above ``HDR_RECOVERY_HIGHLIGHT`` in the recovery anchor, the bracket whose mean luma sits
closest to mid-grey, which the recovery finish already keeps as the photograph. In these
fixtures that is the 0 EV frame.
"""
import json
import shutil
import sys
from pathlib import Path

import cv2
import numpy as np
import pytest

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "backend"))
import stacking  # noqa: E402

_HEIGHT, _WIDTH = 704, 896


def _bracket(tmp_path, clipped_share):
    """Three exposures of a room whose metered (middle) frame blows `clipped_share` of pixels."""
    rng = np.random.default_rng(41)
    rows, cols = np.mgrid[0:_HEIGHT, 0:_WIDTH].astype(np.float32)
    base = (85 + 55 * (cols / _WIDTH) + 15 * np.sin(rows / 70.0))[:, :, None].repeat(3, axis=2)
    base[:, :, 2] *= 1.1
    base[150:550, 100:700] += rng.normal(0, 10, (400, 600, 1))
    side = int(round(np.sqrt(clipped_share * _HEIGHT * _WIDTH)))
    base[60:60 + side, 740 - side:740] = 252.0                  # a lamp / window that blows at 0 EV
    paths = []
    for index, gain in enumerate((0.45, 1.0, 2.1)):
        frame = np.clip(base * gain + rng.normal(0, 1.2, base.shape), 0, 255).astype(np.uint8)
        path = tmp_path / f"frame_{index}.jpg"
        cv2.imwrite(str(path), frame, [cv2.IMWRITE_JPEG_QUALITY, 97])
        paths.append(str(path))
    return paths


def _clipped_share(image):
    return float((cv2.cvtColor(image, cv2.COLOR_BGR2LAB)[:, :, 0] > stacking.HDR_RECOVERY_HIGHLIGHT).mean())


def test_the_threshold_is_the_owner_decision():
    assert stacking.HDR_NOTHING_TO_RECOVER_FRACTION == 0.005


def test_a_barely_clipped_metered_frame_is_the_photograph(tmp_path):
    """0.2 % clipped: the output IS the metered frame, and the evidence says why.
    Would catch the merge running anyway, or a different frame being handed back."""
    paths = _bracket(tmp_path, 0.002)
    metered = cv2.imread(paths[1])
    assert 0.001 < _clipped_share(metered) < 0.003, "fixture drifted from 0.2 %"
    image, report = stacking.hdr_merge_with_report(paths)
    assert np.array_equal(image, metered)
    assert report["recovery"] == "nothing_to_recover"
    assert report["anchor_index"] == 1
    assert report["clipped_fraction"] == pytest.approx(_clipped_share(metered), abs=1e-6)
    assert report["clipped_threshold"] == stacking.HDR_NOTHING_TO_RECOVER_FRACTION
    assert report["clip_level"] == stacking.HDR_RECOVERY_HIGHLIGHT
    assert "nothing to recover" in report["summary"].lower()
    assert np.array_equal(stacking.hdr_merge(paths), metered), "hdr_merge must agree with the report"


def test_the_shortcut_does_no_fusion_work(tmp_path, monkeypatch):
    """Would catch the merge being computed and then discarded. The recovery-region motion
    repair (#92, F01) must not run either: with nothing to recover it has nothing to protect,
    so once both land the early return has to come first (``raising=False`` until then)."""
    paths = _bracket(tmp_path, 0.002)
    refuse = lambda *args, **kwargs: pytest.fail("fusion ran although there was nothing to recover")
    monkeypatch.setattr(stacking, "_coarse_frames", refuse)
    monkeypatch.setattr(stacking, "_fuse_tile", refuse)
    monkeypatch.setattr(stacking, "_repair_recovery_motion", refuse, raising=False)
    assert stacking.hdr_merge_with_report(paths)[1]["recovery"] == "nothing_to_recover"


def test_a_clipped_metered_frame_still_gets_its_highlights_back(tmp_path):
    """2 % clipped: unchanged behaviour. Would catch the shortcut firing above the threshold."""
    paths = _bracket(tmp_path, 0.02)
    metered = cv2.imread(paths[1])
    assert 0.015 < _clipped_share(metered) < 0.025, "fixture drifted from 2 %"
    image, report = stacking.hdr_merge_with_report(paths)
    assert report["recovery"] == "recovered"
    assert report["clipped_fraction"] == pytest.approx(_clipped_share(metered), abs=1e-6)
    assert "nothing to recover" not in report["summary"].lower()
    assert not np.array_equal(image, metered)
    assert _clipped_share(image) < _clipped_share(metered) / 2, "the window came back"
    assert np.array_equal(image, stacking.hdr_merge(paths)), "hdr_merge must agree with the report"


def test_the_fused_render_is_not_short_circuited(tmp_path):
    """recover=False asks for the whole-frame fused render; the shortcut belongs to recovery."""
    paths = _bracket(tmp_path, 0.002)
    image, report = stacking.hdr_merge_with_report(paths, recover=False)
    assert report["recovery"] == "off"
    assert not np.array_equal(image, cv2.imread(paths[1]))


def test_the_session_merge_and_the_gallery_say_nothing_to_recover(tmp_path, monkeypatch):
    """End to end without a camera: the session output details, the published sidecar's
    evidence and its summary all carry the verdict and the measured share."""
    import engine
    import product_sessions
    from stack_session import merge_session_frames

    bracket = _bracket(tmp_path, 0.002)
    details = merge_session_frames("hdr", bracket, str(tmp_path / "merged.jpg"))
    assert details["hdr"]["recovery"] == "nothing_to_recover"
    assert "nothing to recover" in details["summary"].lower()

    monkeypatch.setattr(engine, "SHOTS_DIR", tmp_path / "shots")
    sessions = product_sessions.runner()
    state = sessions.create("hdr", 3)
    capture = lambda index, _spec, dest: shutil.copyfile(bracket[index], dest) and dest
    merge = lambda paths, dest: merge_session_frames("hdr", paths, dest)
    completed = sessions.run(state["id"], capture, merge)
    assert completed["status"] == "completed", completed.get("error")
    published = product_sessions.run(state["id"])
    assert published["publication"]["status"] == "published"
    sidecar = json.loads((engine.SHOTS_DIR / f"session_{state['id']}.json").read_text())
    assert sidecar["evidence"]["hdr"]["recovery"] == "nothing_to_recover"
    assert sidecar["evidence"]["hdr"]["clipped_fraction"] < stacking.HDR_NOTHING_TO_RECOVER_FRACTION
    assert "nothing to recover" in sidecar["summary"].lower()


def test_the_wifi_stack_result_carries_the_verdict(tmp_path, monkeypatch):
    """The Wi-Fi HDR path reports the same evidence; the camera side is faked entirely."""
    import wifi_stack

    bracket = _bracket(tmp_path, 0.002)
    monkeypatch.setattr(wifi_stack, "shoot_bracket", lambda cam, frames, ev_step: {"postviews": []})
    monkeypatch.setattr(wifi_stack, "transfer_capture_jpegs", lambda cam, postviews, save_dir: (bracket, []))
    result = wifi_stack.live_stack(object(), "hdr", 3, 1.0, str(tmp_path / "wifi.jpg"),
                                   save_dir=str(tmp_path / "wifi"))
    assert result["merged"] is True
    assert result["hdr"]["recovery"] == "nothing_to_recover"
