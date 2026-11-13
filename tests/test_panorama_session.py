"""Capture-time panorama sessions: bounds, locked exposure, incremental mosaic previews
and recoverable failure. No camera hardware: a fake capture_frame writes synthetic
overlapping tiles (generated like tests/test_panorama.py) so the real stitcher runs.
"""
from pathlib import Path
from types import SimpleNamespace
import sys

import cv2
import numpy as np
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))
import engine
import panorama
import product_sessions
import stack_session
from stack_session import SessionRunner, merge_session_frames

METADATA = {"camera_model": "Sony ILCE-7M2 (Control)", "camera_port": "usb:001,002", "camera_serial": "A7II00SERIAL"}


def _world():
    """A wide, textured world; horizontal crops of it share real, matchable overlap."""
    rng = np.random.default_rng(1701)
    world = rng.integers(0, 256, (440, 1400, 3), dtype=np.uint8)
    world = cv2.GaussianBlur(world, (3, 3), 0)
    for _ in range(160):
        x, y = rng.integers(10, 1390), rng.integers(10, 430)
        cv2.circle(world, (int(x), int(y)), int(rng.integers(4, 18)), (20, 240, 80), 2)
    cv2.line(world, (20, 210), (1380, 210), (250, 20, 20), 3)
    return world


WORLD = _world()


def _overlapping_tile(index):
    x = index * 250
    return WORLD[:, x:x + 640].copy()


def _disjoint_tile(index):
    return np.random.default_rng(90 + index).integers(0, 256, (240, 320, 3), dtype=np.uint8)


def _write(tile, destination):
    assert cv2.imwrite(str(destination), tile, [cv2.IMWRITE_JPEG_QUALITY, 96])
    return str(destination)


@pytest.fixture
def sessions(tmp_path, monkeypatch):
    monkeypatch.setattr(engine, "SHOTS_DIR", tmp_path / "shots")
    return product_sessions.runner()


def _panorama_merge(paths, out, options=None):
    return merge_session_frames("panorama", paths, out, panorama_options=options or {"projection": "cylindrical"})


# --- 1. Frame-count bounds match panorama.MAX_FRAMES (2..12) -----------------

@pytest.mark.parametrize("frames", [2, 12])
def test_panorama_accepts_two_to_twelve_frames(sessions, frames):
    state = sessions.create("panorama", frames, metadata=METADATA)
    assert state["frame_count"] == frames
    # No per-frame exposure ramp: every frame carries the same zero EV offset.
    assert all(frame["ev_offset"] == 0.0 for frame in state["frames"])


@pytest.mark.parametrize("frames", [1, 13, 16])
def test_panorama_rejects_out_of_range_frame_counts(sessions, frames):
    with pytest.raises(ValueError):
        sessions.create("panorama", frames, metadata=METADATA)


# --- 3/6. Final merge uses build_panorama; publishes; overlap failure is honest ---

def test_final_merge_publishes_mosaic_with_receipt_fields(sessions):
    state = sessions.create("panorama", 3, metadata=METADATA)
    def capture(index, spec, dest):
        return _write(_overlapping_tile(index), dest)
    result = sessions.run(state["id"], capture, _panorama_merge)
    assert result["status"] == "completed", result.get("error")
    receipt = result["output"]["details"]["panorama"]
    assert receipt["projection"] == "cylindrical"
    assert receipt["coverage"]["valid_fraction"] > 0
    assert receipt["algorithm"].startswith("sift-ransac")
    assert "horizon" in receipt
    # Overlap evidence per stitched frame is carried into the manifest.
    assert receipt["alignment"] and all(
        item["estimated_overlap_fraction"] > 0 for item in receipt["alignment"])

    published = product_sessions._publish(sessions, sessions.read(state["id"]))
    assert published["publication"]["status"] == "published"
    assert published["image"] == f"session_{state['id']}.jpg"
    assert (engine.SHOTS_DIR / published["image"]).is_file()
    import json
    sidecar = json.loads((engine.SHOTS_DIR / f"session_{state['id']}.json").read_text())
    assert sidecar["evidence"]["panorama"]["projection"] == "cylindrical"


def test_insufficient_overlap_final_fails_and_preserves_frames(sessions):
    state = sessions.create("panorama", 3, metadata=METADATA)
    def capture(index, spec, dest):
        return _write(_disjoint_tile(index), dest)
    result = sessions.run(state["id"], capture, _panorama_merge)  # no previews (after_frame unset)
    assert result["status"] == "failed"
    assert "overlap" in result["error"].lower()
    # Every accepted frame remains downloadable; no false mosaic was published.
    for index in range(3):
        assert sessions.artifact(state["id"], f"frame_{index:04d}.jpg").is_file()
    with pytest.raises(FileNotFoundError):
        sessions.artifact(state["id"], "result.jpg")


# --- 4. Incremental preview appears/updates from the accepted prefix ----------

def test_incremental_preview_updates_and_removes_prior_dir(sessions):
    state = sessions.create("panorama", 4, metadata=METADATA)
    directory = sessions._directory(state["id"])
    seen = []
    def boundary(live):
        product_sessions._panorama_boundary(sessions, state["id"], live,
            {"projection": "cylindrical"}, preview_every=1, timeout=30)
        seen.append((live.get("preview_panorama") or {}).get("frame_count"))
    def capture(index, spec, dest):
        return _write(_overlapping_tile(index), dest)
    result = sessions.run(state["id"], capture, _panorama_merge, after_frame=boundary)
    assert result["status"] == "completed", result.get("error")
    # Preview absent after frame 1, present at 2, updated to 3, then final merge (frame 4).
    assert seen == [None, 2, 3, 3]
    assert result["preview_panorama"]["frame_count"] == 3
    assert (directory / "pano_0003").is_dir()
    assert not (directory / "pano_0002").exists()  # prior preview dir removed


def test_preview_failure_preserves_frames_and_keeps_capturing(sessions):
    state = sessions.create("panorama", 3, metadata=METADATA)
    def boundary(live):
        product_sessions._panorama_boundary(sessions, state["id"], live,
            {"projection": "cylindrical"}, preview_every=1, timeout=30)
    def capture(index, spec, dest):
        return _write(_disjoint_tile(index), dest)  # cannot stitch -> preview fails
    result = sessions.run(state["id"], capture, None, after_frame=boundary)
    assert result["status"] == "completed"  # capture sequence finished despite preview failure
    assert result["progress"]["accepted"] == 3
    error = result["preview_panorama_error"]
    assert error["accepted_frames_preserved"] is True
    assert "preview_panorama" not in result
    for index in range(3):
        assert sessions.artifact(state["id"], f"frame_{index:04d}.jpg").is_file()


def test_busy_stitcher_lock_is_reported_without_blocking(sessions):
    state = sessions.create("panorama", 3, metadata=METADATA)
    def capture(index, spec, dest):
        if index == 2:
            _write(_overlapping_tile(index), dest)  # shutter fired, transport then lost
            raise TimeoutError("response lost")
        return _write(_overlapping_tile(index), dest)
    ambiguous = sessions.run(state["id"], capture, None)
    assert ambiguous["status"] == "needs_reconciliation"
    live = sessions.read(state["id"])  # two accepted frames, one ambiguous
    assert panorama._LOCK.acquire(blocking=False)
    try:
        product_sessions._panorama_boundary(sessions, state["id"], live,
            {"projection": "cylindrical"}, preview_every=1, timeout=5)
    finally:
        panorama._LOCK.release()
    assert live["preview_panorama_error"]["reason"] == "busy"
    assert live["preview_panorama_error"]["accepted_frames_preserved"] is True
    for index in range(2):
        assert sessions.artifact(state["id"], f"frame_{index:04d}.jpg").is_file()
    assert not (sessions._directory(state["id"]) / "pano_0002").exists()  # busy attempt cleaned



# --- A partial grid previews without the session's full rows×columns ---------

GRID_WORLD = WORLD[:, :940].copy()
GRID_WORLD = np.concatenate([GRID_WORLD, cv2.flip(GRID_WORLD, 0)[:180]], axis=0)  # 620 rows
GRID_CELLS = [(0, 0), (150, 0), (300, 0), (300, 200), (150, 200), (0, 200)]  # 2×3 serpentine


def _grid_tile(index):
    x, y = GRID_CELLS[index]
    return GRID_WORLD[y:y + 420, x:x + 640].copy()


def test_preview_layout_uses_completed_rows_or_no_layout():
    options = {"projection": "cylindrical", "layout": {"rows": 2, "columns": 3, "order": "serpentine"}}
    assert product_sessions._preview_layout(options, 6) == (options, "session_layout")
    rows, policy = product_sessions._preview_layout(options, 3)
    assert policy == "completed_rows" and rows["layout"] == {"rows": 1, "columns": 3}
    partial, policy = product_sessions._preview_layout(options, 5)
    assert policy == "partial_row_without_layout" and "layout" not in partial
    assert options["layout"]["rows"] == 2  # the session's own options are not mutated
    assert product_sessions._preview_layout({"projection": "cylindrical"}, 4)[1] == "no_layout"


def test_grid_session_previews_every_partial_prefix(sessions):
    layout = {"rows": 2, "columns": 3, "order": "serpentine"}
    options = {"projection": "cylindrical", "layout": layout}
    state = sessions.create("panorama", 6, metadata=METADATA)
    seen = []
    def boundary(live):
        product_sessions._panorama_boundary(sessions, state["id"], live, options, preview_every=1, timeout=30)
        assert "preview_panorama_error" not in live, live.get("preview_panorama_error")
        preview = live.get("preview_panorama") or {}
        seen.append((preview.get("frame_count"), preview.get("layout_policy"), preview.get("layout")))
    def capture(index, spec, dest):
        return _write(_grid_tile(index), dest)
    result = sessions.run(state["id"], capture, lambda paths, out: _panorama_merge(paths, out, options),
                          after_frame=boundary)
    assert result["status"] == "completed", result.get("error")
    assert seen[:5] == [(None, None, None),
                        (2, "partial_row_without_layout", None),
                        (3, "completed_rows", {"rows": 1, "columns": 3}),
                        (4, "partial_row_without_layout", None),
                        (5, "partial_row_without_layout", None)]
    assert result["output"]["details"]["panorama"]["layout"] == layout

# --- 1. Locked-base exposure and exposure_policy through the real run() path ---

def test_locked_exposure_and_policy_via_camera(sessions, tmp_path, monkeypatch):
    import sonyremote
    import camera_caps
    baseline = {"iso": "100", "aperture": "8", "shutter": "1/60", "focus_mode": "Manual"}
    live = dict(baseline)
    exposures = []
    tiles = [tmp_path / f"tile{i}.jpg" for i in range(3)]
    for i, path in enumerate(tiles):
        _write(_overlapping_tile(i), path)

    class Camera:
        shots = 0
        def __init__(self, **kwargs): pass
        def __enter__(self): return self
        def __exit__(self, *args): pass
        def info(self): return SimpleNamespace(model=baseline_model, port="usb:001,002")
        def wake(self): return True
        def shooting_mode(self): return "M"
        def get_settings(self): return dict(live)
        def list_options(self, key):
            return {"iso": ["100", "200"], "aperture": ["8"], "shutter": ["1/60"]}[key]
        def set_exposure(self, **kwargs):
            exposures.append(dict(kwargs))
            live.update(kwargs)
        def set_setting(self, key, value):
            live[key] = value
        def capture(self, **kwargs):
            path = tiles[type(self).shots]
            type(self).shots += 1
            return str(path)

    baseline_model = METADATA["camera_model"]
    monkeypatch.setattr(sonyremote, "Camera", Camera)
    monkeypatch.setattr(engine, "_free_usb", lambda: None)
    monkeypatch.setattr(camera_caps, "from_live_choices",
                        lambda *args: SimpleNamespace(snap_shutter=lambda value: (value, "1/60"), shutters=[]))
    state = sessions.create("panorama", 3, metadata=METADATA)
    result = product_sessions.run(state["id"])
    assert result["publication"]["status"] == "published", result.get("error") or result.get("publication_error")
    persisted = sessions.read(state["id"])
    assert persisted["exposure_policy"] == "locked_base"
    # Every capture requested the identical base exposure (no per-frame ramp).
    capture_exposures = exposures[:3]
    assert len(capture_exposures) == 3 and all(item == capture_exposures[0] for item in capture_exposures)
    import json
    sidecar = json.loads((engine.SHOTS_DIR / f"session_{state['id']}.json").read_text())
    assert sidecar["evidence"]["exposure_policy"] == "locked_base"
    assert sidecar["evidence"]["panorama"]["projection"] == "cylindrical"


def test_panorama_options_follow_a_multi_row_plan_and_reject_unknown(sessions):
    plan = panorama.guided_plan(2, 2)
    state = sessions.create("panorama", 4, metadata={**METADATA, "panorama": {
        "projection": "spherical", "level_horizon": True, "crop": [0.1, 0.1, 0.9, 0.9],
        "plan": plan}})
    options = product_sessions._panorama_options(state)
    assert options["layout"] == {"rows": 2, "columns": 2, "order": "serpentine"}
    assert options["crop"] == [0.1, 0.1, 0.9, 0.9]
    assert options["level_horizon"] is True
    mismatched = sessions.create("panorama", 3, metadata={**METADATA, "panorama": {"plan": plan}})
    with pytest.raises(ValueError, match="frame count"):
        product_sessions._panorama_options(mismatched)
    unknown = sessions.create("panorama", 3, metadata={**METADATA, "panorama": {"foo": True}})
    with pytest.raises(ValueError, match="Unknown"):
        product_sessions._panorama_options(unknown)
