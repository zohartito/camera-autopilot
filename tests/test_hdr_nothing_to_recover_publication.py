"""A Fixed HDR whose metered frame has nothing to recover must say so, and hand back that frame.

2026-09-29 A7 III sitting (session 109b04e8, 3 frames): the merge decided "nothing to recover"
(the anchor clipped 0.04 %), but the /shoot response and the published sidecar both said only
"hdr completed from 3 frames.", because the shot record /shoot writes replaces the publication
sidecar and carried neither the HDR summary nor its evidence. The published file was also a
re-encode of the anchor's pixels (mean abs diff 0.556, max 25) with the camera EXIF gone.

Fakes only: a session-capable fake body whose frames are synthetic JPEGs with EXIF.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pytest

ROOT = Path(__file__).resolve().parents[1]
for path in (ROOT, ROOT / "backend"):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))

import sonyremote  # noqa: E402
from camera_caps import _A7II_SHUTTER_LABELS  # noqa: E402

A7II = "Sony ILCE-7M2 (Control)"
SERIAL = "A7II00SERIAL"
# Brightness per shutter around a 1/60 baseline: the middle frame sits at mid-grey, unclipped.
GAIN = {"1/125": 0.5, "1/60": 1.0, "1/30": 1.9}


def _scene():
    rows, cols = np.mgrid[0:192, 0:256].astype(np.float32)
    base = 80 + 60 * (cols / 256) + 20 * np.sin(rows / 11.0) + 10 * np.cos(cols / 7.0)
    return np.dstack([base * 0.9, base, base * 1.1])


@pytest.fixture
def body(tmp_path, monkeypatch):
    import engine
    from PIL import Image
    monkeypatch.setattr(engine, "SHOTS_DIR", tmp_path / "shots")
    monkeypatch.setattr(engine, "_free_usb", lambda: None)
    rig = SimpleNamespace(live={"iso": "100", "aperture": "8", "shutter": "1/60", "focus_mode": "Automatic"},
                          captures=[])
    scene = _scene()

    class Camera:
        def __init__(self, **kwargs): pass
        def __enter__(self): return self
        def __exit__(self, *args): pass
        def info(self): return SimpleNamespace(model=A7II, port="usb:001,002")
        def wake(self): return True
        def shooting_mode(self): return "M"
        def get_settings(self): return dict(rig.live)
        def list_options(self, key):
            return {"iso": ["100", "200"], "aperture": ["8"], "shutter": _A7II_SHUTTER_LABELS + ["Bulb"]}[key]
        def set_setting(self, key, value): rig.live[key] = value
        def set_exposure(self, **kwargs): rig.live.update(kwargs)
        def capture(self, **kwargs):
            path = tmp_path / f"source_{len(rig.captures)}.jpg"
            pixels = np.clip(scene * GAIN[rig.live["shutter"]], 0, 255).astype(np.uint8)
            exif = Image.Exif()
            exif[0x010F] = "SONY"
            exif[0x0110] = "ILCE-7M2"
            exif[0x9003] = f"2026:09:29 15:40:{len(rig.captures):02d}"   # DateTimeOriginal
            Image.fromarray(pixels).save(path, quality=93, exif=exif.tobytes())
            rig.captures.append(dict(rig.live))
            return str(path)
    monkeypatch.setattr(sonyremote, "Camera", Camera)
    return rig


def test_nothing_to_recover_reaches_the_response_and_the_sidecar_and_publishes_the_anchor_file(body):
    """Would catch the /shoot summary or the sidecar dropping the verdict, or the published file
    being a re-encode of the anchor rather than the camera's own bytes."""
    import engine
    import product_capture
    import product_sessions
    from PIL import Image

    camera = SimpleNamespace(name="real", camera_model=A7II, camera_port="usb:001,002", camera_serial=SERIAL)
    shot = product_capture.shoot(camera, "natural",
                                 stack_policy={"mode": "fixed", "stack_mode": "hdr", "frames": 3, "baseline": "current"})
    assert shot.ok, shot.summary
    assert [capture["shutter"] for capture in body.captures] == ["1/125", "1/60", "1/30"]
    hdr = shot.evidence["hdr"]
    assert hdr["recovery"] == "nothing_to_recover" and hdr["anchor_index"] == 1
    assert hdr["output_source"] == "anchor_bracket_file"
    # The response the client renders (result card) says why the photograph is one frame.
    assert shot.summary.startswith("hdr completed from 3 frames.")
    assert hdr["summary"] in shot.summary and "nothing to recover" in shot.summary.lower()

    # The sidecar Review reads (the shot record replaces the publication sidecar) says it too.
    sidecar = json.loads((engine.SHOTS_DIR / f"{Path(shot.image).stem}.json").read_text())
    assert sidecar["summary"] == shot.summary
    assert sidecar["evidence"]["hdr"]["recovery"] == "nothing_to_recover"

    # The published photograph is the anchor bracket's own file: same bytes, camera EXIF intact.
    session_id = shot.evidence["session_id"]
    state = product_sessions.runner().read(session_id)
    anchor = next(frame for frame in state["frames"] if frame["index"] == hdr["anchor_index"])
    anchor_bytes = Path(anchor["destination"]).read_bytes()
    published = (engine.SHOTS_DIR / shot.image).read_bytes()
    assert published == anchor_bytes
    assert state["output"]["receipt"]["sha256"] == anchor["receipt"]["sha256"]
    assert state["publication"]["sha256"] == anchor["receipt"]["sha256"]
    with Image.open(engine.SHOTS_DIR / shot.image) as image:
        assert image.getexif().get(0x010F) == "SONY"


def test_a_recovered_hdr_still_publishes_the_merge(tmp_path):
    """The byte copy belongs to the nothing-to-recover verdict only."""
    import cv2
    from stack_session import merge_session_frames

    scene = _scene()
    scene[20:120, 150:250] = 240.0                     # a window that blows at 0 EV
    paths = []
    for index, gain in enumerate((0.45, 1.0, 2.1)):
        path = tmp_path / f"frame_{index}.jpg"
        cv2.imwrite(str(path), np.clip(scene * gain, 0, 255).astype(np.uint8), [cv2.IMWRITE_JPEG_QUALITY, 95])
        paths.append(str(path))
    details = merge_session_frames("hdr", paths, str(tmp_path / "merged.jpg"))
    assert details["hdr"]["recovery"] == "recovered"
    assert details["hdr"]["output_source"] == "merged"
    assert (tmp_path / "merged.jpg").read_bytes() != Path(paths[details["hdr"]["anchor_index"]]).read_bytes()


def _shifted_bracket(tmp_path, strip):
    """Three exposures; the anchor has a clipped strip `strip` columns wide on its right edge and
    the first frame is shifted 20 px right, so registration moves the strip out of view."""
    import cv2
    rows, cols = np.mgrid[0:384, 0:512].astype(np.float32)
    base = 90 + 40 * (cols / 512) + 15 * np.sin(rows / 13.0) + 10 * np.cos(cols / 9.0)
    paths = []
    for index, gain in enumerate((0.45, 1.0, 1.9)):
        frame = np.clip(base * gain, 0, 255)
        if index == 1:
            frame[:, -strip:] = 255.0
        frame = frame.astype(np.uint8)
        if index == 0:
            frame = cv2.warpAffine(frame, np.float32([[1, 0, 20], [0, 1, 0]]), (512, 384),
                                   borderMode=cv2.BORDER_REFLECT)
        path = tmp_path / f"frame_{index}.jpg"
        cv2.imwrite(str(path), cv2.cvtColor(frame, cv2.COLOR_GRAY2BGR), [cv2.IMWRITE_JPEG_QUALITY, 95])
        paths.append(str(path))
    return paths


def _clipped(path):
    import cv2
    import stacking
    return float((cv2.cvtColor(cv2.imread(str(path)), cv2.COLOR_BGR2LAB)[:, :, 0]
                  > stacking.HDR_RECOVERY_HIGHLIGHT).mean())


def test_the_original_is_published_only_if_its_own_pixels_have_nothing_to_recover(tmp_path):
    """Review finding: the verdict is measured on the anchor as registered to the first frame, and
    registration can shift a clipped edge out of view. Publishing the original then restored a
    clipped edge (0.78 %) under a 'nothing to recover' verdict. Keep the measured frame then."""
    import cv2
    from stack_session import merge_session_frames
    import stacking

    paths = _shifted_bracket(tmp_path, 4)
    original = cv2.imread(paths[1])
    assert (cv2.cvtColor(original, cv2.COLOR_BGR2LAB)[:, :, 0] > stacking.HDR_RECOVERY_HIGHLIGHT).mean() \
        >= stacking.HDR_NOTHING_TO_RECOVER_FRACTION, "the original anchor must clip past the threshold"
    details = merge_session_frames("hdr", paths, str(tmp_path / "merged.jpg"))
    assert details["hdr"]["recovery"] == "nothing_to_recover", "registration must hide the strip"
    assert details["hdr"]["output_source"] == "merged"
    assert (tmp_path / "merged.jpg").read_bytes() != Path(paths[1]).read_bytes()


def test_the_published_original_carries_its_own_clipped_share(tmp_path):
    """Review finding: with both shares under the threshold (registered 0 %, original ~0.4 %), the
    original is published, so the evidence and summary must state the original's share."""
    from stack_session import merge_session_frames

    paths = _shifted_bracket(tmp_path, 2)
    own = _clipped(paths[1])
    assert 0.003 < own < 0.005, "fixture drifted"
    details = merge_session_frames("hdr", paths, str(tmp_path / "merged.jpg"))
    hdr = details["hdr"]
    assert hdr["recovery"] == "nothing_to_recover" and hdr["output_source"] == "anchor_bracket_file"
    assert hdr["registered_clipped_fraction"] < own
    assert hdr["clipped_fraction"] == pytest.approx(own, abs=1e-6)
    assert f"clipped {own * 100:.2f} %" in hdr["summary"] and details["summary"] == hdr["summary"]


def test_an_anchor_replaced_during_the_merge_fails_the_merge(tmp_path, monkeypatch):
    """Review finding: the anchor's bytes are read again after the merge, so a frame replaced
    meanwhile (same size, still unclipped) would have been published and receipted as the output.
    The output must be exactly the accepted frame, or the merge fails."""
    import shutil
    import cv2
    import engine
    import product_sessions
    import stacking
    from stack_session import merge_session_frames

    scene = _scene()
    bracket = []
    for index, gain in enumerate((0.5, 1.0, 1.9)):
        path = tmp_path / f"bracket_{index}.jpg"
        cv2.imwrite(str(path), np.clip(scene * gain, 0, 255).astype(np.uint8), [cv2.IMWRITE_JPEG_QUALITY, 95])
        bracket.append(str(path))
    monkeypatch.setattr(engine, "SHOTS_DIR", tmp_path / "shots")
    real = stacking.hdr_merge_with_report

    def merge_then_replace(paths, **kwargs):
        result = real(paths, **kwargs)
        # Someone rewrites the accepted anchor while the merge runs: same size, nothing clipped.
        cv2.imwrite(paths[1], cv2.imread(paths[1]), [cv2.IMWRITE_JPEG_QUALITY, 80])
        return result
    monkeypatch.setattr(stacking, "hdr_merge_with_report", merge_then_replace)
    sessions = product_sessions.runner()
    state = sessions.create("hdr", 3)
    capture = lambda index, _spec, dest: shutil.copyfile(bracket[index], dest) and dest
    finished = sessions.run(state["id"], capture, lambda paths, dest: merge_session_frames("hdr", paths, dest))
    assert finished["status"] == "failed"
    assert "not the accepted frame" in finished["error"]
    assert not finished.get("output")


def test_the_offline_remerge_checks_the_anchor_against_the_accepted_receipt(tmp_path, monkeypatch):
    """Review finding: tools/remerge_session.py uses the same byte copy but never compared it with
    the manifest's accepted receipts, so a replaced anchor frame came back as a clean result."""
    import shutil
    import cv2
    import engine
    import product_sessions
    from stack_session import merge_session_frames
    sys.path.insert(0, str(ROOT / "tools"))
    import remerge_session

    scene = _scene()
    bracket = []
    for index, gain in enumerate((0.5, 1.0, 1.9)):
        path = tmp_path / f"bracket_{index}.jpg"
        cv2.imwrite(str(path), np.clip(scene * gain, 0, 255).astype(np.uint8), [cv2.IMWRITE_JPEG_QUALITY, 95])
        bracket.append(str(path))
    monkeypatch.setattr(engine, "SHOTS_DIR", tmp_path / "shots")
    sessions = product_sessions.runner()
    state = sessions.create("hdr", 3)
    capture = lambda index, _spec, dest: shutil.copyfile(bracket[index], dest) and dest
    finished = sessions.run(state["id"], capture, lambda paths, dest: merge_session_frames("hdr", paths, dest))
    assert finished["status"] == "completed", finished.get("error")
    session_dir = sessions._directory(state["id"])

    clean = remerge_session.remerge(session_dir, tmp_path / "out" / "clean.jpg", deghost=None, force=False)
    assert clean["merge"]["hdr"]["output_source"] == "anchor_bracket_file"
    anchor = session_dir / "frame_0001.jpg"
    cv2.imwrite(str(anchor), cv2.imread(str(anchor)), [cv2.IMWRITE_JPEG_QUALITY, 80])   # replaced since
    with pytest.raises(ValueError, match="not the accepted frame"):
        remerge_session.remerge(session_dir, tmp_path / "out" / "replaced.jpg", deghost=None, force=False)
    assert not (tmp_path / "out" / "replaced.jpg").exists()
