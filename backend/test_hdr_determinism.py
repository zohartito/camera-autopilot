"""The same bracket must give the same HDR file, byte for byte, in every process.

Ledger X-HDRDET (2026-09-05 puck benchmark): three fresh-process HDR runs on one saved A7 III
bracket produced two different output SHA-256 values while night and crowd were identical.
Reproduced on 2026-09-27 with that exact code (stacking.py sha256 d0ea000c...): six fresh
processes gave five different pixel hashes. The varying stage was OpenCV's
``cv2.createMergeMertens().process``; decode, MTB alignment and every primitive the current
merge uses (pyrDown/pyrUp, GaussianBlur, Laplacian, resize, cvtColor) were stable, and with
``cv2.setNumThreads(1)`` the old merge was stable too. MergeMertens accumulates its per-image
pyramids from parallel workers, so float addition order followed thread scheduling.

The tiled ``disk-tiled-laplacian-v4`` merge no longer calls MergeMertens: it accumulates the
brackets in a fixed order in Python. These tests pin that on one machine and library build.
They run in separate interpreters, where a scheduling race shows most reliably, and hash both
the array ``hdr_merge`` returns and the file the product path (``merge_session_frames`` ->
``cv2.imwrite``) writes. The bracket is larger than one 512 px tile and wider than the
1024 px alignment thumbnail, has a blown window, a moving subject and a handheld shift, so the
thumbnail, registration warp, tiled, coarse-base, deghost and recovery passes all run.
"""
import hashlib
import json
import subprocess
import sys
from pathlib import Path

import cv2
import numpy as np
import pytest

REPO = Path(__file__).resolve().parent.parent
BACKEND = REPO / "backend"
sys.path.insert(0, str(BACKEND))
import stacking  # noqa: E402

_CHILD = """
import hashlib, json, sys
sys.path.insert(0, sys.argv[1])
import cv2
if sys.argv[2] != "default":
    cv2.setNumThreads(int(sys.argv[2]))
def _refuse(*args, **kwargs):
    raise AssertionError("cv2.createMergeMertens called: its parallel accumulation is run-order dependent")
cv2.createMergeMertens = _refuse
import stack_session
import stacking
array = stacking.hdr_merge(sys.argv[4:])
stack_session.merge_session_frames("hdr", sys.argv[4:], sys.argv[3])
print(json.dumps({
    "array": hashlib.sha256(array.tobytes()).hexdigest(),
    "file": hashlib.sha256(open(sys.argv[3], "rb").read()).hexdigest(),
}))
"""

_SHIFT = 6  # px: the brightest frame is handheld-shifted, so registration has to warp it


def _bracket(tmp_path, height=704, width=1152):
    """Three exposures larger than one tile, with a window that blows and a subject that moves."""
    rng = np.random.default_rng(29)
    rows, cols = np.mgrid[0:height, 0:width].astype(np.float32)
    base = (80 + 70 * (cols / width) + 20 * np.sin(rows / 90.0))[:, :, None].repeat(3, axis=2)
    base[:, :, 2] *= 1.12
    base[40:420, 900:] = 250.0                                   # clips in the brighter frames
    base[100:600, 60:840] += rng.normal(0, 12, (500, 780, 1))    # texture across tile seams
    paths = []
    for index, gain in enumerate((0.45, 1.0, 2.1)):
        scene = base.copy()
        x0 = 150 + 110 * index                                   # a subject that moves
        scene[450:620, x0:x0 + 120] = (40.0, 60.0, 170.0)
        if index == 2:
            scene = np.roll(scene, _SHIFT, axis=1)
        frame = np.clip(scene * gain + rng.normal(0, 1.5, scene.shape), 0, 255).astype(np.uint8)
        path = tmp_path / f"frame_{index}.jpg"
        cv2.imwrite(str(path), frame, [cv2.IMWRITE_JPEG_QUALITY, 97])
        paths.append(str(path))
    return paths


def _merge_in_fresh_process(tmp_path, paths, name, threads="default"):
    out = tmp_path / name
    completed = subprocess.run(
        [sys.executable, "-c", _CHILD, str(BACKEND), threads, str(out), *paths],
        capture_output=True, text=True, timeout=300, check=False)
    assert completed.returncode == 0, completed.stderr[-2000:]
    return json.loads(completed.stdout.strip().splitlines()[-1])


def test_the_fixture_reaches_every_pass_of_the_tiled_merge(tmp_path):
    """Guard the fixture, so the equality below cannot pass by skipping the parallel-prone work."""
    paths = _bracket(tmp_path)
    frames = [cv2.imread(path) for path in paths]
    height, width = frames[0].shape[:2]
    assert height > stacking.STACK_TILE_SIZE and width > stacking.STACK_TILE_SIZE
    assert width > stacking._STACK_ALIGNMENT_EDGE, "the alignment thumbnail would not downscale"
    assert height % 16 == 0 and width % 16 == 0, "the coarse full-depth base needs even geometry"
    reference, shifted = (cv2.cvtColor(stacking._thumbnail(frames[i]), cv2.COLOR_BGR2GRAY) for i in (0, 2))
    assert not np.allclose(stacking._registration(reference, shifted, hdr=True), np.eye(3)), \
        "registration would not warp the shifted frame"
    with stacking._staged_stack(paths, hdr=True) as (files, shape, histograms):
        assert stacking._coarse_frames(files, shape, stacking._STACK_PYRAMID_LEVELS) is not None
        # The recovery anchor, chosen exactly as hdr_merge chooses it, must actually clip.
        means = [float(np.dot(hist, np.arange(256)) / np.sum(hist)) for hist in histograms]
        anchor_idx = min(range(len(histograms)), key=lambda i: (abs(means[i] - 127.5), i))
        anchor = np.array(np.memmap(files[anchor_idx], dtype=np.uint8, mode="r", shape=shape))
    luma = cv2.cvtColor(anchor, cv2.COLOR_BGR2LAB)[:, :, 0]
    assert (luma > stacking.HDR_RECOVERY_HIGHLIGHT).mean() > 0.05, "recovery would not run"
    assert not np.array_equal(stacking.hdr_merge(paths, deghost=True),
                              stacking.hdr_merge(paths, deghost=False)), "deghost never fired"


def test_separate_processes_give_byte_identical_hdr_output(tmp_path, monkeypatch):
    """Two guards. Deterministically: every merge here runs with ``cv2.createMergeMertens``
    replaced by a function that raises, so reintroducing it fails on any runner, even one with
    a single worker where its race cannot show. Empirically: the hashes must match across
    processes, which catches any other thread-order-dependent accumulation (a parallel tile
    loop, an unordered reduction) or anything run-varying in the written JPEG. A race is only
    probable between two default runs, so the third runs OpenCV on one thread, where a result
    that depends on how work is split across threads is far more likely to differ."""
    monkeypatch.setattr(cv2, "createMergeMertens", lambda *args, **kwargs: pytest.fail(
        "cv2.createMergeMertens called: its parallel accumulation is run-order dependent"))
    paths = _bracket(tmp_path)
    runs = [_merge_in_fresh_process(tmp_path, paths, f"result_{index}.jpg") for index in range(2)]
    runs.append(_merge_in_fresh_process(tmp_path, paths, "result_single_thread.jpg", threads="1"))
    assert len({run["array"] for run in runs}) == 1, f"merged arrays differ between processes: {runs}"
    assert len({run["file"] for run in runs}) == 1, f"file bytes differ between processes: {runs}"
    assert hashlib.sha256(stacking.hdr_merge(paths).tobytes()).hexdigest() == runs[0]["array"]
