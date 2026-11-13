"""A subject walking past a blown window must not be pasted into the recovered highlights.

Ledger F01, 2026-09-27 A7 III receipt (9 frames, 1/1000..1/4 s, 111 s): the owner walked in
front of the window in the two darkest brackets, the ones the recovery draws the window from,
and the merge put a translucent silhouette of him into the recovered pane. The existing deghost
could not see it: the anchor is blown there, the brighter brackets are clipped, and its
reference was a bracket that held the subject.

These tests pin the fix on a synthetic bracket shaped like that receipt: the subject is a dark
blob inside the blown window, in the two darkest brackets only, at different places.
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
_GAINS = (0.1, 0.14, 0.2, 0.28, 1.0, 1.6)          # frame 4 is the metered anchor
_WINDOW = (slice(60, 420), slice(420, 860))
_BLOBS = {0: (slice(140, 300), slice(480, 600)),     # the subject, darkest bracket
          1: (slice(180, 340), slice(640, 760))}     # and the next one, moved on


def _scene():
    rng = np.random.default_rng(23)
    rows, cols = np.mgrid[0:_HEIGHT, 0:_WIDTH].astype(np.float32)
    base = (100 + 40 * (cols / _WIDTH) + 12 * np.sin(rows / 60.0))[:, :, None].repeat(3, axis=2)
    base[:, :, 2] *= 1.08
    base[100:620, 40:400] += rng.normal(0, 9, (520, 360, 1))
    window = base[_WINDOW]
    window[:] = (1000.0, 960.0, 900.0)                        # ~3 stops over the room
    window += rng.normal(0, 25, window.shape[:2] + (1,))      # texture the fusion must keep
    return base


def _bracket(tmp_path, moving):
    base = _scene()
    rng = np.random.default_rng(5)
    paths = []
    for index, gain in enumerate(_GAINS):
        scene = base.copy()
        if moving and index in _BLOBS:
            scene[_BLOBS[index]] = (60.0, 55.0, 50.0)         # a dark head: near black here
        frame = np.clip(scene * gain + rng.normal(0, 1.2, scene.shape), 0, 255).astype(np.uint8)
        path = tmp_path / f"frame_{index}.png"
        cv2.imwrite(str(path), frame)
        paths.append(str(path))
    return paths


def _without_motion_check(monkeypatch):
    empty = {"motion_reference_index": None, "motion_checked_fraction": 0.0,
             "motion_rejected_fraction": 0.0, "motion_rejected_frames": {}}
    monkeypatch.setattr(stacking, "_repair_recovery_motion", lambda *args, **kwargs: dict(empty))


def test_the_fixture_is_the_receipt(tmp_path):
    """Guard: the anchor is frame 4, the window blows in it, only the three darkest brackets
    hold it; the subject brackets are the two darkest (the receipt)."""
    paths = _bracket(tmp_path, moving=False)
    frames = [cv2.imread(path) for path in paths]
    means = [float(cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY).mean()) for frame in frames]
    assert min(range(len(means)), key=lambda i: abs(means[i] - 127.5)) == 4
    lightness = cv2.cvtColor(frames[4], cv2.COLOR_BGR2LAB)[:, :, 0]
    assert (lightness[_WINDOW] > stacking.HDR_RECOVERY_HIGHLIGHT).mean() > 0.95
    for index in range(4):
        window = cv2.cvtColor(frames[index], cv2.COLOR_BGR2GRAY)[_WINDOW]
        unclipped = ((window > 8) & (window < 247)).mean()
        assert (unclipped > 0.95) if index < 3 else (unclipped < 0.5), f"bracket {index}: {unclipped}"


def test_a_subject_in_the_darkest_brackets_is_not_recovered_into_the_window(tmp_path, monkeypatch):
    """Would catch the recovery fusing a bracket whose content disagrees with the others."""
    (tmp_path / "static").mkdir()
    static = stacking.hdr_merge(_bracket(tmp_path / "static", moving=False))
    moving_paths = _bracket(tmp_path, moving=True)
    report = {}
    merged = stacking.hdr_merge(moving_paths, report=report)
    for blob in _BLOBS.values():
        residual = np.abs(merged[blob].astype(np.int16) - static[blob].astype(np.int16))
        assert residual.mean() < 3.0, f"the subject shows in the recovered window: {residual.mean():.1f}"
    assert report["motion_reference_index"] == 2, "a bracket holding the subject became the reference"
    assert set(report["motion_rejected_frames"]) >= {"0", "1"}
    assert report["motion_rejected_fraction"] > 0.01
    # The control: without the check the same bracket does ghost.
    _without_motion_check(monkeypatch)
    ghosted = stacking.hdr_merge(moving_paths)
    ghost = max(np.abs(ghosted[blob].astype(np.int16) - static[blob].astype(np.int16)).mean()
                for blob in _BLOBS.values())
    assert ghost > 10.0, "the fixture no longer ghosts, so the test above proves nothing"


def test_a_static_bracket_is_unchanged(tmp_path, monkeypatch):
    """Nothing moved: nothing is rejected and the output is byte-identical to the merge
    without the check. Would catch false rejections from exposure normalisation."""
    paths = _bracket(tmp_path, moving=False)
    report = {}
    checked = stacking.hdr_merge(paths, report=report)
    assert report["motion_rejected_fraction"] == 0.0 and report["motion_rejected_frames"] == {}
    assert report["motion_checked_fraction"] > 0.9
    _without_motion_check(monkeypatch)
    assert np.array_equal(checked, stacking.hdr_merge(paths))


def test_outside_the_recovery_mask_the_moving_bracket_still_gives_the_anchor(tmp_path):
    paths = _bracket(tmp_path, moving=True)
    anchor = cv2.imread(paths[4])
    lightness = cv2.cvtColor(anchor, cv2.COLOR_BGR2LAB)[:, :, 0].astype(np.float32)
    weight = cv2.GaussianBlur(np.clip((lightness - stacking.HDR_RECOVERY_HIGHLIGHT)
                                      / (255. - stacking.HDR_RECOVERY_HIGHLIGHT), 0, 1),
                              (0, 0), stacking.HDR_RECOVERY_FEATHER)
    merged = stacking.hdr_merge(paths)
    assert np.array_equal(merged[weight == 0], anchor[weight == 0])


def test_a_black_subject_in_a_dark_bracket_is_a_disagreement():
    """Clipped values are intervals: black against a lit window disagrees, black against a
    nearly black shadow does not. Would catch dropping clipped pixels from the comparison,
    which left the receipt's hair and shirt (black in the 1/1000 s frame) in the window."""
    joint = np.zeros((256, 256), dtype=np.int64)
    levels = np.arange(20, 240)
    joint[levels, levels // 2] = 1000                       # darker bracket = brighter / 2
    table = stacking._pair_difference_table(joint, first_brighter=True)
    assert table[200, 3] > stacking.HDR_MOTION_SEED         # lit in one, black in the other
    assert table[20, 3] == 0.0                               # shadow: below the signal floor
    assert table[255, 120] == 0.0                            # clipped high, bright in the other
    assert table[255, 20] > stacking.HDR_MOTION_SEED         # clipped high, dark in the other
    assert table[100, 50] == 0.0                             # exposure explains it


def test_the_session_records_motion_in_evidence(tmp_path, monkeypatch):
    import engine
    import product_sessions
    from stack_session import merge_session_frames

    bracket = _bracket(tmp_path, moving=True)
    jpegs = []
    for index, path in enumerate(bracket):
        target = tmp_path / f"jpeg_{index}.jpg"
        cv2.imwrite(str(target), cv2.imread(path), [cv2.IMWRITE_JPEG_QUALITY, 97])
        jpegs.append(str(target))
    monkeypatch.setattr(engine, "SHOTS_DIR", tmp_path / "shots")
    sessions = product_sessions.runner()
    state = sessions.create("hdr", len(jpegs))
    capture = lambda index, _spec, dest: shutil.copyfile(jpegs[index], dest) and dest
    merge = lambda paths, dest: merge_session_frames("hdr", paths, dest)
    completed = sessions.run(state["id"], capture, merge)
    assert completed["status"] == "completed", completed.get("error")
    assert completed["output"]["details"]["hdr_motion"]["motion_rejected_fraction"] > 0.01
    published = product_sessions.run(state["id"])
    assert published["publication"]["status"] == "published"
    sidecar = json.loads((engine.SHOTS_DIR / f"session_{state['id']}.json").read_text())
    assert sidecar["evidence"]["hdr_motion"]["motion_rejected_fraction"] > 0.01


def test_a_pair_without_a_measurable_exposure_relation_is_not_compared():
    """Codex counterexample: 26 samples on each level is a perfectly consistent pair, but no
    level is populated enough to estimate the curve; an identity fallback called it ~75 %
    disagreement and could rewrite a static region."""
    joint = np.zeros((256, 256), dtype=np.int64)
    levels = np.arange(80, 241)
    joint[levels, levels // 4] = 26
    assert joint.sum() >= 4096
    assert stacking._pair_difference_table(joint, first_brighter=True) is None


def test_a_clipped_value_is_bounded_at_the_clip_level():
    """Clipped high means at least 247, not 255: 247 against a mapped 100 is 147/247 = 0.595,
    under the seed, where treating it as 255 gave 0.608, over it."""
    joint = np.zeros((256, 256), dtype=np.int64)
    levels = np.arange(20, 241)
    joint[levels // 2, levels] = 1000                        # rows darker, columns brighter
    table = stacking._pair_difference_table(joint, first_brighter=False)
    assert table[255, 200] == pytest.approx(147 / 247, abs=1e-6)
    assert table[255, 200] < stacking.HDR_MOTION_SEED


def test_one_sparse_outlier_level_does_not_lift_the_exposure_curve():
    """A running maximum let 64 dark pixels of a bright bracket over a lit window push the
    whole curve to 186 on a real receipt, so every level looked like motion."""
    joint = np.zeros((256, 256), dtype=np.int64)
    levels = np.arange(40, 240)
    joint[levels, levels // 4] = 5000
    joint[20, 186] = 64                                      # the outlier level
    lower, upper = stacking._exposure_lut(joint)
    assert lower[200] == upper[200] == pytest.approx(50, abs=2)
    assert np.all(np.diff(lower) >= 0) and np.all(np.diff(upper) >= 0)


def test_levels_beyond_the_measured_relation_are_bounds_not_guesses():
    """Codex reproducer: one dense level (200 -> 100) and sparse consistent levels below it.
    A constant extension mapped every level under 200 to 100 and flagged them all; below the
    lowest supported level only 'at most 100' is known."""
    joint = np.zeros((256, 256), dtype=np.int64)
    joint[200, 100] = 10000
    levels = np.arange(20, 80)
    joint[levels, levels // 2] = 63
    table = stacking._pair_difference_table(joint, first_brighter=True)
    assert float(table[levels, levels // 2].max()) == 0.0
    assert table[240, 20] > stacking.HDR_MOTION_SEED       # above it, 'at least 100' still holds


def test_no_repair_when_deghost_is_off(tmp_path, monkeypatch):
    """``deghost=False`` asks for no motion handling at all."""
    paths = _bracket(tmp_path, moving=True)
    monkeypatch.setattr(stacking, "_repair_recovery_motion",
                        lambda *args, **kwargs: pytest.fail("motion repair ran with deghost off"))
    stacking.hdr_merge(paths, deghost=False)


def test_evidence_counts_the_pixels_actually_rewritten(tmp_path):
    """Review finding: the evidence counted only the detected core, while the staged bracket is
    rewritten across the grown, feathered mask. Every changed pixel must be inside what the
    evidence reports, and the rewritten share exceeds the detected one."""
    paths = _bracket(tmp_path, moving=True)
    with stacking._staged_stack(paths, hdr=True) as (files, shape, histograms):
        means = [float(np.dot(h, np.arange(256)) / h.sum()) for h in histograms]
        tone = min(range(len(means)), key=lambda i: (abs(means[i] - 127.5), i))
        before = [np.array(np.memmap(f, dtype=np.uint8, mode="r", shape=shape)) for f in files]
        report = stacking._repair_recovery_motion(files, shape, tone, means)
        after = [np.array(np.memmap(f, dtype=np.uint8, mode="r", shape=shape)) for f in files]
        region = np.zeros(shape[:2], dtype=bool)
        for bounds, dest, interior in stacking._tiles(shape):
            region[dest] = stacking._recovery_weight(
                stacking._tile_from_file(files[tone], shape, bounds))[interior] > 0
    total = region.sum()
    for index in (0, 1):
        changed = np.any(before[index] != after[index], axis=2)
        assert changed.any()
        assert not changed[~region].any(), "a bracket was rewritten outside the recovery region"
        assert changed.sum() <= report["motion_rejected_frames"][str(index)] * total
    assert report["motion_rejected_fraction"] > report["motion_detected_fraction"] > 0


def test_a_reference_nobody_corroborates_repairs_nothing(tmp_path, monkeypatch):
    """Review finding (P1): with three brackets there are only two non-anchor brackets, so the
    reference is whichever wins a tie. Here the subject is in the bracket that wins it (closer
    to the anchor's exposure), and without a third bracket to say which one is clean the repair
    copied the subject into the clean bracket. Repair nothing, and say so in evidence."""
    base = _scene()
    # Static window bars at the subject's brightness, so the exposure relation at that level
    # is measured on static pixels and not learned from the subject itself.
    base[60:420, 700:760] = (220.0, 200.0, 180.0)
    base[380:420, 420:860] = (220.0, 200.0, 180.0)
    rng = np.random.default_rng(11)
    paths = []
    for index, gain in enumerate((0.1, 0.14, 1.0)):     # clean dark, dark with subject, anchor
        scene = base.copy()
        if index == 1:
            scene[_BLOBS[0]] = (220.0, 200.0, 180.0)         # a subject both dark brackets resolve
        frame = np.clip(scene * gain + rng.normal(0, 1.2, scene.shape), 0, 255).astype(np.uint8)
        path = tmp_path / f"frame_{index}.png"
        cv2.imwrite(str(path), frame)
        paths.append(str(path))
    report = {}
    checked = stacking.hdr_merge(paths, report=report)
    assert report["motion_reference_index"] == 1, "the fixture must make the subject bracket win"
    assert report["motion_rejected_frames"] == {} and report["motion_rejected_fraction"] == 0.0
    assert report.get("motion_uncorroborated_frames") == [0]
    _without_motion_check(monkeypatch)
    assert np.array_equal(checked, stacking.hdr_merge(paths))


# Equal grey level to the window (0.114 B + 0.587 G + 0.299 R): an orange jacket against pale sky.
_COLOUR_SUBJECT = (250.0, 800.0, 1500.0)


def _colour_bracket(tmp_path, moving):
    rng = np.random.default_rng(5)
    base = _scene()
    paths = []
    for index, gain in enumerate(_GAINS):
        scene = base.copy()
        if moving and index in _BLOBS:
            scene[_BLOBS[index]] = _COLOUR_SUBJECT
        frame = np.clip(scene * gain + rng.normal(0, 1.2, scene.shape), 0, 255).astype(np.uint8)
        path = tmp_path / f"frame_{index}.png"
        cv2.imwrite(str(path), frame)
        paths.append(str(path))
    return paths


def test_a_subject_that_differs_only_in_colour_is_repaired(tmp_path, monkeypatch):
    """Review finding: the luma table cannot see a subject at the window's grey level; the
    deghost then pastes its own reference, which can hold the subject, over the region."""
    (tmp_path / "static").mkdir()
    static = stacking.hdr_merge(_colour_bracket(tmp_path / "static", moving=False))
    paths = _colour_bracket(tmp_path, moving=True)
    frames = [cv2.imread(path) for path in paths]
    gray = cv2.cvtColor(frames[0], cv2.COLOR_BGR2GRAY).astype(np.float32)
    subject, window = gray[_BLOBS[0]].mean(), gray[60:120, 800:860].mean()
    assert abs(subject - window) / window < 0.1, "the subject must match the window's grey level"
    report = {}
    merged = stacking.hdr_merge(paths, report=report)
    assert set(report["motion_rejected_frames"]) >= {"0", "1"}
    for blob in _BLOBS.values():
        residual = np.abs(merged[blob].astype(np.int16) - static[blob].astype(np.int16))
        assert residual.mean() < 3.0, f"the colour subject shows in the window: {residual.mean():.1f}"
    _without_motion_check(monkeypatch)
    ghosted = stacking.hdr_merge(paths)
    ghost = max(np.abs(ghosted[blob].astype(np.int16) - static[blob].astype(np.int16)).mean()
                for blob in _BLOBS.values())
    assert ghost > 5.0, "the fixture no longer ghosts, so the test above proves nothing"


def test_a_static_coloured_scene_is_not_flagged(tmp_path):
    """Saturated static colours across every exposure: the chroma test must not read the
    camera's exposure-dependent saturation as motion."""
    rng = np.random.default_rng(9)
    base = _scene()
    for row, colour in enumerate([(1400.0, 700.0, 500.0), (400.0, 900.0, 500.0),
                                  (400.0, 600.0, 1300.0), (1200.0, 1100.0, 300.0)]):
        base[80 + row * 80: 140 + row * 80, 440:840] = colour     # coloured glass in the window
        base[450 + row * 40: 480 + row * 40, 60:380] = [c / 8 for c in colour]   # and in the room
    paths = []
    for index, gain in enumerate(_GAINS):
        frame = np.clip(base * gain + rng.normal(0, 1.2, base.shape), 0, 255).astype(np.uint8)
        path = tmp_path / f"frame_{index}.png"
        cv2.imwrite(str(path), frame)
        paths.append(str(path))
    report = {}
    stacking.hdr_merge(paths, report=report)
    assert report["motion_checked_fraction"] > 0.5
    assert report["motion_rejected_frames"] == {}, report


def test_chroma_is_compared_only_inside_the_blown_core():
    """Outside the core the brighter brackets' saturation drifts with exposure (static p99.9
    up to 62 on real receipts); inside it, only signal clear of the shoulder counts."""
    gray = np.array([[100, 100, 20, 240]], dtype=np.uint8)
    reference = np.array([[120, 120, 120, 120]], dtype=np.uint8)
    core = np.array([[True, False, True, True]])
    assert stacking._chroma_usable(gray, reference, core).tolist() == [[True, False, False, False]]


def test_a_bracket_holding_a_colour_subject_is_not_chosen_as_the_reference(tmp_path):
    """Review finding (P1): the reference was chosen on luma alone, so a subject that differs
    only in colour could sit in the reference itself; every clean bracket then disagreed with it
    and was repaired by copying the subject in. The reference choice (and corroboration) now
    weighs chroma disagreement too: the subject bracket is the odd one out and is repaired."""
    rng = np.random.default_rng(5)
    base = _scene()
    paths = []
    for index, gain in enumerate(_GAINS):
        scene = base.copy()
        if index == 2:                                        # the bracket luma alone picks
            scene[_BLOBS[0]] = _COLOUR_SUBJECT
        frame = np.clip(scene * gain + rng.normal(0, 1.2, scene.shape), 0, 255).astype(np.uint8)
        path = tmp_path / f"frame_{index}.png"
        cv2.imwrite(str(path), frame)
        paths.append(str(path))
    (tmp_path / "static").mkdir()
    static = stacking.hdr_merge(_colour_bracket(tmp_path / "static", moving=False))
    report = {}
    merged = stacking.hdr_merge(paths, report=report)
    assert report["motion_reference_index"] != 2, "the colour-subject bracket became the reference"
    assert set(report["motion_rejected_frames"]) == {"2"}, report["motion_rejected_frames"]
    residual = np.abs(merged[_BLOBS[0]].astype(np.int16) - static[_BLOBS[0]].astype(np.int16))
    assert residual.mean() < 3.0, f"the colour subject shows in the window: {residual.mean():.1f}"


def test_a_dirty_reference_is_never_copied_even_if_chosen(tmp_path, monkeypatch):
    """Review finding (P1, reference choice): whatever picks the reference, a repair needs a
    third bracket that is valid over the detected component and agrees with the reference
    there. With the subject in the reference, every clean bracket disagrees with it at the
    subject, so no component is corroborated and nothing is repaired (the merge without the
    check, #89's behaviour). Chroma evidence is switched off here to force the dirty choice."""
    rng = np.random.default_rng(5)
    base = _scene()
    paths = []
    for index, gain in enumerate(_GAINS):
        scene = base.copy()
        if index == 2:
            scene[_BLOBS[0]] = _COLOUR_SUBJECT
        frame = np.clip(scene * gain + rng.normal(0, 1.2, scene.shape), 0, 255).astype(np.uint8)
        path = tmp_path / f"frame_{index}.png"
        cv2.imwrite(str(path), frame)
        paths.append(str(path))
    monkeypatch.setattr(stacking, "_core_chroma_disagreements", lambda *args, **kwargs: {})
    report = {}
    checked = stacking.hdr_merge(paths, report=report)
    assert report["motion_reference_index"] == 2, "the fixture must force the dirty reference"
    assert report["motion_rejected_frames"] == {}, "a subject was copied from the reference"
    assert report["motion_uncorroborated_fraction"] > 0.01
    _without_motion_check(monkeypatch)
    assert np.array_equal(checked, stacking.hdr_merge(paths))


def test_a_third_bracket_clipped_over_the_subject_does_not_corroborate(tmp_path, monkeypatch):
    """Review finding (P1, corroboration): a brighter bracket clipped over the subject agrees
    with the reference only on unrelated background, so it says nothing about the component.
    Abstain for it."""
    base = _scene()
    rng = np.random.default_rng(13)
    paths = []
    for index, gain in enumerate((0.1, 0.14, 0.5, 1.0)):   # subject, clean, clipped over window, anchor
        scene = base.copy()
        if index == 0:
            scene[_BLOBS[0]] = (60.0, 55.0, 50.0)
        frame = np.clip(scene * gain + rng.normal(0, 1.2, scene.shape), 0, 255).astype(np.uint8)
        path = tmp_path / f"frame_{index}.png"
        cv2.imwrite(str(path), frame)
        paths.append(str(path))
    window = cv2.cvtColor(cv2.imread(paths[2]), cv2.COLOR_BGR2GRAY)[_BLOBS[0]]
    assert (window >= 247).mean() > 0.95, "the third bracket must be clipped over the subject"
    report = {}
    checked = stacking.hdr_merge(paths, report=report)
    assert report["motion_reference_index"] == 1
    assert "2" not in report["motion_uncorroborated_frames"] and 2 not in report["motion_uncorroborated_frames"]
    assert report["motion_rejected_frames"] == {}
    assert report["motion_uncorroborated_fraction"] > 0.01
    _without_motion_check(monkeypatch)
    assert np.array_equal(checked, stacking.hdr_merge(paths))


# 2026-09-29 A7 III receipt: the subject is in EVERY bracket but the anchor, at a different place in
# each, so no two brackets agree and the reference check abstains. Gains are about one stop apart
# in encoded values (x0.73), as a camera JPEG is; only the darkest bracket holds the window.
_WALK_GAINS = (0.73, 1.0, 1.37, 1.88)                 # frame 1 is the metered anchor
_WALK_BLOBS = {0: (slice(120, 300), slice(470, 590)),   # darkest bracket: in front of the window
               2: (slice(160, 360), slice(620, 720)),
               3: (slice(100, 260), slice(730, 830))}


def _walk_bracket(tmp_path, moving, subject=(70.0, 62.0, 55.0), glass=(300.0, 290.0, 280.0)):
    rng = np.random.default_rng(29)
    rows, cols = np.mgrid[0:_HEIGHT, 0:_WIDTH].astype(np.float32)
    base = (72 + 30 * (cols / _WIDTH) + 10 * np.sin(rows / 60.0))[:, :, None].repeat(3, axis=2)
    base[100:620, 40:400] += rng.normal(0, 9, (520, 360, 1))
    window = base[_WINDOW]
    window[:] = glass                                          # blown at the anchor, 1 stop over
    # A real window falls off into the reveal and the wall around it, so the anchor holds every
    # level up to its clip level just outside the blown glass.
    base = cv2.GaussianBlur(base, (0, 0), 10)
    window = base[_WINDOW]
    window += rng.normal(0, 12, window.shape[:2] + (1,))
    paths = []
    for index, gain in enumerate(_WALK_GAINS):
        scene = base.copy()
        if moving and index in _WALK_BLOBS:
            scene[_WALK_BLOBS[index]] = subject                # the owner, dark against the glass
        frame = np.clip(scene * gain + rng.normal(0, 1.2, scene.shape), 0, 255).astype(np.uint8)
        path = tmp_path / f"frame_{index}.png"
        cv2.imwrite(str(path), frame)
        paths.append(str(path))
    return paths


def _without_anchor_check(monkeypatch):
    empty = {"motion_anchor_checked_frames": [], "motion_anchor_unchecked_frames": [],
             "motion_anchor_detected_fraction": 0.0, "motion_anchor_rejected_fraction": 0.0,
             "motion_anchor_rejected_frames": {}}
    monkeypatch.setattr(stacking, "_hold_brackets_to_anchor", lambda *args, **kwargs: dict(empty))


def _gray(image):
    return cv2.cvtColor(image, cv2.COLOR_BGR2GRAY).astype(np.float32)


def test_the_walk_fixture_is_the_receipt(tmp_path):
    """Guard: frame 1 anchors and blows the window; only frame 0 holds it; the reference check
    abstains on the moving bracket (no corroborated reference), as it did on the receipt."""
    paths = _walk_bracket(tmp_path, moving=True)
    frames = [cv2.imread(path) for path in paths]
    means = [float(_gray(frame).mean()) for frame in frames]
    assert min(range(len(means)), key=lambda i: abs(means[i] - 127.5)) == 1
    assert (cv2.cvtColor(frames[1], cv2.COLOR_BGR2LAB)[:, :, 0][_WINDOW] > stacking.HDR_RECOVERY_HIGHLIGHT).mean() > 0.95
    window = [_gray(frame)[_WINDOW] for frame in frames]
    assert ((window[0] > 8) & (window[0] < 247)).mean() > 0.9
    assert all((window[index] >= 247).mean() > 0.6 for index in (1, 2, 3))
    with stacking._staged_stack(paths, hdr=True) as (files, shape, histograms):
        tone_means = [float(np.dot(h, np.arange(256)) / h.sum()) for h in histograms]
        report = stacking._repair_recovery_motion(files, shape, 1, tone_means)
    assert report["motion_rejected_frames"] == {}, "the reference check must abstain here"


@pytest.mark.parametrize("subject, glass", [
    ((70.0, 62.0, 55.0), (300.0, 290.0, 280.0)),     # dark clothes
    ((55.0, 62.0, 280.0), (300.0, 290.0, 280.0)),    # a red shirt: one channel as bright as the glass
    ((55.0, 62.0, 280.0), (230.0, 240.0, 280.0)),    # and against bluish glass the anchor clips in red only
    ((55.0, 62.0, 350.0), (300.0, 290.0, 280.0)),    # a red shirt clipped in red in the darker bracket too
])
def test_a_subject_in_every_bracket_is_not_recovered_into_the_window(tmp_path, monkeypatch, subject, glass):
    """Would catch recovery pasting a bracket's content that contradicts what the anchor shows
    when no two brackets corroborate a reference (the 2026-09-29 translucent silhouette). The red
    shirt is the review finding where requiring all three channels let the red one veto it."""
    (tmp_path / "static").mkdir()
    static = stacking.hdr_merge(_walk_bracket(tmp_path / "static", moving=False, glass=glass))
    paths = _walk_bracket(tmp_path, moving=True, subject=subject, glass=glass)
    report = {}
    merged = stacking.hdr_merge(paths, report=report)
    for blob in _WALK_BLOBS.values():
        darker = float((_gray(static[blob]) - _gray(merged[blob])).mean())
        assert darker < 3.0, f"the subject shows in the recovered window: {darker:.1f} darker"
    assert set(report["motion_anchor_rejected_frames"]) == {"0", "2", "3"}
    assert report["motion_anchor_checked_frames"] == [0, 2, 3]
    assert report["motion_anchor_detected_fraction"] > 0.01
    # Away from the subject the window is still recovered from the darkest bracket.
    clear = (slice(330, 400), slice(440, 600))
    anchor = cv2.imread(paths[1])
    assert _gray(merged[clear]).mean() < _gray(anchor[clear]).mean() - 3, "the window was not recovered"
    assert np.abs(_gray(merged[clear]) - _gray(static[clear])).mean() < 2.0
    # The control: without the anchor check the same bracket ghosts.
    _without_anchor_check(monkeypatch)
    ghosted = stacking.hdr_merge(paths)
    ghost = max(float((_gray(static[blob]) - _gray(ghosted[blob])).mean()) for blob in _WALK_BLOBS.values())
    assert ghost > 10.0, "the fixture no longer ghosts, so the test above proves nothing"


def test_a_static_walk_scene_is_unchanged_by_the_anchor_check(tmp_path, monkeypatch):
    """Nothing moved: the anchor check rewrites nothing, and the output is byte-identical to the
    merge without it. Would catch the clip-level lower bound flagging static pixels."""
    paths = _walk_bracket(tmp_path, moving=False)
    report = {}
    checked = stacking.hdr_merge(paths, report=report)
    assert report["motion_anchor_checked_frames"] == [0, 2, 3]
    assert report["motion_anchor_rejected_frames"] == {} and report["motion_anchor_detected_fraction"] == 0.0
    _without_anchor_check(monkeypatch)
    assert np.array_equal(checked, stacking.hdr_merge(paths))


def test_the_anchor_check_writes_only_inside_the_region_and_never_the_anchor(tmp_path):
    """Every rewritten pixel is in the recovery region, the anchor's staged copy is untouched,
    and the evidence bounds what was written."""
    paths = _walk_bracket(tmp_path, moving=True)
    with stacking._staged_stack(paths, hdr=True) as (files, shape, histograms):
        means = [float(np.dot(h, np.arange(256)) / h.sum()) for h in histograms]
        before = [np.array(np.memmap(f, dtype=np.uint8, mode="r", shape=shape)) for f in files]
        report = stacking._hold_brackets_to_anchor(files, shape, 1, means)
        after = [np.array(np.memmap(f, dtype=np.uint8, mode="r", shape=shape)) for f in files]
        region = np.zeros(shape[:2], dtype=bool)
        for bounds, dest, interior in stacking._tiles(shape):
            region[dest] = stacking._recovery_weight(
                stacking._tile_from_file(files[1], shape, bounds))[interior] > 0
    assert np.array_equal(before[1], after[1]), "the anchor was rewritten"
    total = region.sum()
    for index in (0, 2, 3):
        changed = np.any(before[index] != after[index], axis=2)
        assert changed.any()
        assert not changed[~region].any(), "a bracket was rewritten outside the recovery region"
        assert changed.sum() <= report["motion_anchor_rejected_frames"][str(index)] * total
    assert report["motion_anchor_rejected_fraction"] >= report["motion_anchor_detected_fraction"] > 0


@pytest.mark.parametrize("dim_window, colour", [
    (False, (100.0, 100.0, 2000.0)),     # a red lamp inside the blown window
    (True, (100.0, 2000.0, 100.0)),      # a green one in a window the anchor barely blows
])
def test_a_static_saturated_colour_is_not_read_as_motion(tmp_path, monkeypatch, dim_window, colour):
    """Review findings (P1, twice): gray alone called a pixel with one clipped channel an exact
    value. A saturated red whose red channel clips in the anchor but not in the darker brackets
    read as ~7 % of the region moving and was erased; and such pixels, admitted into the exposure
    fit, bent the curve until 79 % of a static region read as moving (neutral glass went green).
    A partly clipped pixel is a bound in the comparison and no sample in the fit."""
    rng = np.random.default_rng(5)
    base = _scene()
    if dim_window:
        base[_WINDOW] *= 0.28
    base[150:230, 550:630] = colour
    paths = []
    for index, gain in enumerate(_GAINS):
        frame = np.clip(base * gain + rng.normal(0, 1.2, base.shape), 0, 255).astype(np.uint8)
        path = tmp_path / f"frame_{index}.png"
        cv2.imwrite(str(path), frame)
        paths.append(str(path))
    report = {}
    checked = stacking.hdr_merge(paths, report=report)
    assert report["motion_anchor_rejected_frames"] == {}, report["motion_anchor_rejected_frames"]
    _without_anchor_check(monkeypatch)
    assert np.array_equal(checked, stacking.hdr_merge(paths))


def test_a_static_colour_through_a_per_channel_tone_curve_is_not_read_as_motion(tmp_path, monkeypatch):
    """Review finding (P2): a camera renders each channel through its own nonlinear curve, so a
    static saturated colour follows its channels' exposure relations but not the neutral gray one
    (anchor BGR 76,76,246 against 10,10,191 in a darker bracket read as motion). Every
    channel must disagree, the same way, for a disagreement to count."""
    rng = np.random.default_rng(31)
    rows, cols = np.mgrid[0:_HEIGHT, 0:_WIDTH].astype(np.float32)
    radiance = (30 + 170 * (cols / _WIDTH) + 20 * np.sin(rows / 50.0))[:, :, None].repeat(3, axis=2)
    radiance[_WINDOW] = 5000.0
    radiance = cv2.GaussianBlur(radiance, (0, 0), 10)
    radiance[150:230, 550:630] = (42.5, 42.5, 2400.0)        # a static red lamp, unclipped at the anchor
    paths = []
    for index, gain in enumerate((0.1, 1.0, 1.6)):
        exposed = radiance * gain
        frame = np.clip(255 * exposed / (100 + exposed) + rng.normal(0, 0.5, exposed.shape), 0, 255).astype(np.uint8)
        path = tmp_path / f"frame_{index}.png"
        cv2.imwrite(str(path), frame)
        paths.append(str(path))
    frames = [cv2.imread(path) for path in paths]
    assert frames[1][190, 590].tolist() == pytest.approx([76, 76, 245], abs=2) and frames[1][150:230, 550:630].max() < 247
    assert frames[0][190, 590].tolist() == pytest.approx([10, 10, 180], abs=4)
    report = {}
    checked = stacking.hdr_merge(paths, report=report)
    assert report["anchor_index"] == 1
    assert report["motion_anchor_checked_frames"] == [0, 2]
    assert report["motion_anchor_rejected_frames"] == {}, report["motion_anchor_rejected_frames"]
    _without_anchor_check(monkeypatch)
    assert np.array_equal(checked, stacking.hdr_merge(paths))


def test_the_fill_behind_a_blown_anchor_is_never_below_the_glass(tmp_path):
    """Review finding: the value written where the anchor is blown came from pixels a coloured
    subject contaminated, and fell below the anchor's lower bound in two channels (219,73,73
    against a bound of 179). Written into the darker bracket over its red-shirt subject, it must
    be at least what that bracket shows of the static glass, channel by channel."""
    paths = _walk_bracket(tmp_path, moving=True, subject=(55.0, 62.0, 280.0))
    (tmp_path / "static").mkdir()
    glass = cv2.imread(_walk_bracket(tmp_path / "static", moving=False)[0])[_WALK_BLOBS[0]]
    with stacking._staged_stack(paths, hdr=True) as (files, shape, histograms):
        means = [float(np.dot(h, np.arange(256)) / h.sum()) for h in histograms]
        report = stacking._hold_brackets_to_anchor(files, shape, 1, means)
        repaired = np.array(np.memmap(files[0], dtype=np.uint8, mode="r", shape=shape))
    assert "0" in report["motion_anchor_rejected_frames"]
    core = repaired[_WALK_BLOBS[0]][20:-20, 20:-20].reshape(-1, 3).astype(np.float32)
    floor = np.percentile(glass.reshape(-1, 3), 5, axis=0)
    assert np.all(core.mean(axis=0) >= floor), (core.mean(axis=0), floor)


@pytest.mark.parametrize("lamp", [(20.0, 3000.0, 3000.0),     # yellow: blown core at the anchor (L > 225)
                                  (3000.0, 20.0, 3000.0)])    # magenta: L 127, outside the core
def test_a_colour_clipped_then_rerendered_below_255_is_not_read_as_motion(tmp_path, monkeypatch, lamp):
    """Review findings (P2, twice): one global render for every frame, clip then a saturation step.
    A static lamp clipped in every frame comes out below 255 (hidden clipping), dominated the sparse
    levels of the exposure fit and made 52-60 % of the region read as moving (the magenta one cast
    the window magenta). The fit takes near-neutral pixels only, outside the anchor's blown core."""
    rng = np.random.default_rng(31)
    rows, cols = np.mgrid[0:_HEIGHT, 0:_WIDTH].astype(np.float32)
    scene = (72 + 30 * cols / _WIDTH + 10 * np.sin(rows / 60))[:, :, None].repeat(3, axis=2)
    scene[100:620, 40:400] += rng.normal(0, 9, (520, 360, 1))
    scene[_WINDOW] = (300.0, 290.0, 280.0)
    scene = cv2.GaussianBlur(scene, (0, 0), 10)
    scene[_WINDOW] += rng.normal(0, 12, scene[_WINDOW].shape[:2] + (1,))
    scene[150:230, 550:630] = lamp                          # a static lamp
    paths = []
    for index, gain in enumerate((0.1, 1.0, 1.8)):
        exposed = np.clip(scene * gain, 0, 255)
        gray = cv2.cvtColor(exposed, cv2.COLOR_BGR2GRAY)[:, :, None]
        frame = np.clip(gray + 0.5 * (exposed - gray), 0, 255).astype(np.uint8)
        path = tmp_path / f"frame_{index}.png"
        cv2.imwrite(str(path), frame)
        paths.append(str(path))
    assert cv2.imread(paths[1])[190, 590].max() < 247, "the lamp's clipping must be hidden"
    report = {}
    checked = stacking.hdr_merge(paths, report=report)
    assert report["motion_anchor_rejected_frames"] == {}, report["motion_anchor_rejected_frames"]
    _without_anchor_check(monkeypatch)
    assert np.array_equal(checked, stacking.hdr_merge(paths))


@pytest.mark.parametrize("lamp, saturation", [
    ((slice(150, 230), slice(550, 630)), 0.2),       # round 7: a darker bracket reading brighter
    ((slice(120, 320), slice(500, 700)), 0.1),       # round 8: large enough to own its fit levels
])
def test_a_muted_srgb_render_of_a_clipped_lamp_is_not_read_as_motion(tmp_path, monkeypatch, lamp, saturation):
    """Review finding (P2, round 7): linear exposure, channel clipping, sRGB, then a strong global
    desaturation (x0.2) and JPEG. The clipped lamp comes out near neutral (anchor 172,136,172;
    darker bracket 145,97,145), so it looked like an exact value, and the darker bracket reading
    brighter than its exposure allows was taken for motion. A darker bracket can read brighter
    only where the anchor clipped, so for it only reading darker is evidence. Round 8: the same
    lamp, large and clipped in every frame, reads nearly the same in all of them and owned its
    levels of the exposure fit; a fit sample must show an exposure step between the two frames."""
    rng = np.random.default_rng(31)
    rows, cols = np.mgrid[0:_HEIGHT, 0:_WIDTH].astype(np.float32)
    scene = (20 + 5 * cols / _WIDTH + 3 * np.sin(rows / 60))[:, :, None].repeat(3, axis=2)
    scene[100:620, 40:400] += rng.normal(0, 2, (520, 360, 1))
    scene[_WINDOW] = (300.0, 290.0, 280.0)
    scene = cv2.GaussianBlur(scene, (0, 0), 10)
    scene[_WINDOW] += rng.normal(0, 12, scene[_WINDOW].shape[:2] + (1,))
    scene[lamp] = (3000.0, 20.0, 3000.0) if saturation == 0.2 else (3000.0, 0.2, 3000.0)
    paths = []
    for index, gain in enumerate((0.1, 1.0, 1.8)):
        linear = np.clip(scene * gain / 255, 0, 1)
        rgb = 255 * np.where(linear <= 0.0031308, 12.92 * linear, 1.055 * linear ** (1 / 2.4) - 0.055)
        gray = cv2.cvtColor(rgb.astype(np.float32), cv2.COLOR_BGR2GRAY)[:, :, None]
        frame = np.clip(gray + saturation * (rgb - gray), 0, 255).astype(np.uint8)
        path = tmp_path / f"frame_{index}.jpg"
        cv2.imwrite(str(path), frame, [cv2.IMWRITE_JPEG_QUALITY, 97])
        paths.append(str(path))
    report = {}
    checked = stacking.hdr_merge(paths, report=report)
    assert report["anchor_index"] == 1
    assert report["motion_anchor_rejected_frames"] == {}, report["motion_anchor_rejected_frames"]
    _without_anchor_check(monkeypatch)
    assert np.array_equal(checked, stacking.hdr_merge(paths))


@pytest.mark.parametrize("lamp", [(100.0, 100.0, 3000.0),      # round 9: red, saturated at the anchor
                                  (180.0, 3000.0, 180.0)])     # round 10: green, one channel clipped, near neutral
def test_a_saturation_boosted_clipped_colour_is_not_read_as_motion(tmp_path, monkeypatch, lamp):
    """Review finding (round 9, P2): clip, then a global saturation boost (x1.6). A static red lamp
    (anchor 153,152,255; darker bracket 19,20,254) has its weaker channels pushed below the
    neutral relation, and two channels voted motion. At a saturated anchor colour the anchor
    check gives no evidence; it speaks where the anchor's colour is near neutral. Round 10: a
    green lamp clipped in one channel only (anchor 204,255,205) passes as near neutral; where the
    anchor clipped some channels but not all and the darker bracket clips one of those too: a
    coloured light source, where a darker bracket gives no evidence."""
    rng = np.random.default_rng(31)
    rows, cols = np.mgrid[0:_HEIGHT, 0:_WIDTH].astype(np.float32)
    scene = (20 + 5 * cols / _WIDTH + 3 * np.sin(rows / 60))[:, :, None].repeat(3, axis=2)
    scene[100:620, 40:400] += rng.normal(0, 2, (520, 360, 1))
    scene[_WINDOW] = (300.0, 290.0, 280.0)
    scene = cv2.GaussianBlur(scene, (0, 0), 10)
    scene[_WINDOW] += rng.normal(0, 12, scene[_WINDOW].shape[:2] + (1,))
    scene[150:230, 550:630] = lamp
    paths = []
    for index, gain in enumerate((0.1, 1.0, 1.8)):
        linear = np.clip(scene * gain / 255, 0, 1)
        rgb = 255 * np.where(linear <= 0.0031308, 12.92 * linear, 1.055 * linear ** (1 / 2.4) - 0.055)
        gray = cv2.cvtColor(rgb.astype(np.float32), cv2.COLOR_BGR2GRAY)[:, :, None]
        frame = np.clip(gray + 1.6 * (rgb - gray), 0, 255).astype(np.uint8)
        path = tmp_path / f"frame_{index}.jpg"
        cv2.imwrite(str(path), frame, [cv2.IMWRITE_JPEG_QUALITY, 97])
        paths.append(str(path))
    report = {}
    checked = stacking.hdr_merge(paths, report=report)
    assert report["anchor_index"] == 1
    assert report["motion_anchor_rejected_frames"] == {}, report["motion_anchor_rejected_frames"]
    _without_anchor_check(monkeypatch)
    assert np.array_equal(checked, stacking.hdr_merge(paths))


def _sony_exif_jpeg(pixels, path, *, style=None, effect=None):
    """Write ``pixels`` as a JPEG carrying a minimal Sony MakerNote (Creative Style 0xB020, Picture
    Effect 0x200E), laid out as the camera does: a style longer than 4 bytes sits at an offset
    from the TIFF start. Layout: header 8 | IFD0 18 | Exif IFD 18 | "SONY DSC" 12 | IFD | strings."""
    import struct
    exif_at, maker_at = 8 + 18, 8 + 18 + 18
    count = (style is not None) + (effect is not None)
    strings_at = maker_at + 12 + 2 + 12 * count + 4
    entries, tail = [], b""
    if style is not None:
        text = style.encode() + b"\x00"
        if len(text) <= 4:
            value = text.ljust(4, b"\x00")
        else:
            value, tail = struct.pack("<I", strings_at), text
        entries.append(struct.pack("<HHI", 0xB020, 2, len(text)) + value)
    if effect is not None:
        entries.append(struct.pack("<HHI", 0x200E, 3, 1) + struct.pack("<H", effect) + b"\x00\x00")
    maker = b"SONY DSC \x00\x00\x00" + struct.pack("<H", count) + b"".join(entries) + b"\x00" * 4 + tail
    ifd0 = struct.pack("<H", 1) + struct.pack("<HHII", 0x8769, 4, 1, exif_at) + b"\x00" * 4
    exif_ifd = struct.pack("<H", 1) + struct.pack("<HHII", 0x927C, 7, len(maker), maker_at) + b"\x00" * 4
    tiff = b"II*\x00" + struct.pack("<I", 8) + ifd0 + exif_ifd + maker
    assert tiff.index(b"SONY DSC") == maker_at
    segment = b"Exif\x00\x00" + tiff
    ok, encoded = cv2.imencode(".jpg", pixels, [cv2.IMWRITE_JPEG_QUALITY, 97])
    assert ok
    data = encoded.tobytes()
    Path(path).write_bytes(data[:2] + b"\xff\xe1" + struct.pack(">H", len(segment) + 2) + segment + data[2:])
    return str(path)


def test_the_picture_style_is_read_from_the_anchor(tmp_path):
    pixels = np.full((32, 32, 3), 128, np.uint8)
    read = lambda **style: stacking._anchor_picture_style(_sony_exif_jpeg(pixels, tmp_path / "f.jpg", **style))
    assert read(style="BW") == {"name": "BW", "monochrome": True}
    assert read(style="Standard") == {"name": "Standard", "monochrome": False}
    assert read(style="Sepia")["monochrome"] is True
    assert read(style="Standard", effect=13) == {"name": "High Contrast Monochrome", "monochrome": True}
    assert read(style="Vivid", effect=0) == {"name": "Vivid", "monochrome": False}
    cv2.imwrite(str(tmp_path / "plain.jpg"), pixels)
    assert stacking._anchor_picture_style(tmp_path / "plain.jpg") is None       # unknown: check as today
    cv2.imwrite(str(tmp_path / "plain.png"), pixels)
    assert stacking._anchor_picture_style(tmp_path / "plain.png") is None
    (tmp_path / "torn.jpg").write_bytes(b"\xff\xd8\xff\xe1\x00\x40Exif\x00\x00II*\x00\xff\xff")
    assert stacking._anchor_picture_style(tmp_path / "torn.jpg") is None


def test_a_monochrome_capture_abstains_from_the_anchor_check(tmp_path, monkeypatch):
    """Owner decision (2026-09-29): in a monochrome picture style the anchor check abstains and
    says so; an unknown style keeps it. The same moving bracket is repaired when the style is
    unknown, so the abstention is what changed the outcome."""
    frames = [cv2.imread(path) for path in _walk_bracket(tmp_path, moving=True)]
    (tmp_path / "mono").mkdir()
    (tmp_path / "plain").mkdir()
    mono = [_sony_exif_jpeg(frame, tmp_path / "mono" / f"frame_{index}.jpg", style="BW")
            for index, frame in enumerate(frames)]
    plain = []
    for index, frame in enumerate(frames):
        path = tmp_path / "plain" / f"frame_{index}.jpg"
        cv2.imwrite(str(path), frame, [cv2.IMWRITE_JPEG_QUALITY, 97])
        plain.append(str(path))
    report = {}
    abstained = stacking.hdr_merge(mono, report=report)
    assert report["motion_anchor_status"] == "abstained_monochrome"
    assert report["motion_anchor_picture_style"] == "BW"
    assert report["motion_anchor_rejected_frames"] == {} and report["motion_anchor_checked_frames"] == []
    control = {}
    stacking.hdr_merge(plain, report=control)
    assert control["motion_anchor_status"] == "checked" and control["motion_anchor_picture_style"] is None
    assert control["motion_anchor_rejected_frames"], "without the style the check must act on this bracket"
    _without_anchor_check(monkeypatch)
    assert np.array_equal(abstained, stacking.hdr_merge(mono))


def test_the_picture_style_reader_refuses_what_it_cannot_prove(tmp_path):
    """Review findings on the reader: a value or segment declared longer than the bytes present,
    and a Picture Effect whose count makes its field an offset, must not read as monochrome; a RAW
    anchor is read through its JPEG twin, as staging does."""
    import struct
    pixels = np.full((32, 32, 3), 128, np.uint8)
    good = Path(_sony_exif_jpeg(pixels, tmp_path / "good.jpg", style="BW"))
    data = good.read_bytes()
    # The style entry declares 0xffffffff bytes; only "BW\0" (inline) is there.
    at = data.index(struct.pack("<HHI", 0xB020, 2, 3))
    (tmp_path / "long.jpg").write_bytes(data[:at] + struct.pack("<HHI", 0xB020, 2, 0xFFFFFFFF) + data[at + 8:])
    assert stacking._anchor_picture_style(tmp_path / "long.jpg") is None
    # The APP1 segment declares more bytes than the file holds.
    (tmp_path / "app1.jpg").write_bytes(data[:4] + b"\xff\xff" + data[6:])
    assert stacking._anchor_picture_style(tmp_path / "app1.jpg") is None
    # A Picture Effect with count 3 holds an offset, not the value 13.
    effect = Path(_sony_exif_jpeg(pixels, tmp_path / "effect.jpg", style="Standard", effect=13)).read_bytes()
    at = effect.index(struct.pack("<HHI", 0x200E, 3, 1))
    (tmp_path / "count.jpg").write_bytes(effect[:at] + struct.pack("<HHI", 0x200E, 3, 3) + effect[at + 8:])
    assert stacking._anchor_picture_style(tmp_path / "count.jpg") == {"name": "Standard", "monochrome": False}
    # A MakerNote whose declared length cannot hold its directory, or runs past the segment.
    at = data.index(struct.pack("<HH", 0x927C, 7))
    for length in (12, 0xFFFFFFFF):
        (tmp_path / "maker.jpg").write_bytes(data[:at + 4] + struct.pack("<I", length) + data[at + 8:])
        assert stacking._anchor_picture_style(tmp_path / "maker.jpg") is None, length
    # A style string placed past the MakerNote's declared end.
    sepia = Path(_sony_exif_jpeg(pixels, tmp_path / "sepia.jpg", style="Sepia")).read_bytes()
    at = sepia.index(struct.pack("<HH", 0x927C, 7))
    declared = struct.unpack("<I", sepia[at + 4:at + 8])[0]
    (tmp_path / "short.jpg").write_bytes(sepia[:at + 4] + struct.pack("<I", declared - 6) + sepia[at + 8:])
    assert stacking._anchor_picture_style(tmp_path / "sepia.jpg")["monochrome"] is True
    assert stacking._anchor_picture_style(tmp_path / "short.jpg") is None
    # An Exif IFD pointer that is not one LONG, and a header without the TIFF magic number.
    at = data.index(struct.pack("<HHI", 0x8769, 4, 1))
    for kind, number in ((4, 0), (4, 2), (3, 1)):
        (tmp_path / "pointer.jpg").write_bytes(data[:at + 2] + struct.pack("<HI", kind, number) + data[at + 8:])
        assert stacking._anchor_picture_style(tmp_path / "pointer.jpg") is None, (kind, number)
    at = data.index(b"II*\x00")
    (tmp_path / "magic.jpg").write_bytes(data[:at + 2] + b"\x00\x00" + data[at + 4:])
    assert stacking._anchor_picture_style(tmp_path / "magic.jpg") is None
    # A MakerNote declared without room for its next-directory field, an ASCII style without its
    # NUL, and a header that is not a known Sony MakerNote header.
    at = data.index(struct.pack("<HH", 0x927C, 7))
    declared = struct.unpack("<I", data[at + 4:at + 8])[0]
    (tmp_path / "nonext.jpg").write_bytes(data[:at + 4] + struct.pack("<I", declared - 4) + data[at + 8:])
    assert stacking._anchor_picture_style(tmp_path / "nonext.jpg") is None
    at = data.index(struct.pack("<HHI", 0xB020, 2, 3))
    (tmp_path / "nonul.jpg").write_bytes(data[:at] + struct.pack("<HHI", 0xB020, 2, 2) + data[at + 8:])
    assert stacking._anchor_picture_style(tmp_path / "nonul.jpg") is None
    (tmp_path / "header.jpg").write_bytes(data.replace(b"SONY DSC \x00\x00\x00", b"SONYgarbage!", 1))
    assert stacking._anchor_picture_style(tmp_path / "header.jpg") is None
    # A directory inside the TIFF header, bytes hidden after an embedded NUL, and a tag twice.
    at = data.index(b"II*\x00")
    (tmp_path / "overlap.jpg").write_bytes(data[:at + 4] + struct.pack("<I", 4) + data[at + 8:])
    assert stacking._anchor_picture_style(tmp_path / "overlap.jpg") is None
    at = data.index(struct.pack("<HHI", 0xB020, 2, 3))
    (tmp_path / "embedded.jpg").write_bytes(data[:at] + struct.pack("<HHI", 0xB020, 2, 4) + b"B\x00W\x00" + data[at + 12:])
    assert stacking._anchor_picture_style(tmp_path / "embedded.jpg") is None
    twice = Path(_sony_exif_jpeg(pixels, tmp_path / "twice.jpg", style="BW", effect=0)).read_bytes()
    at = twice.index(struct.pack("<HHI", 0x200E, 3, 1))
    (tmp_path / "dup.jpg").write_bytes(twice[:at] + struct.pack("<HHI", 0xB020, 2, 3) + b"BW\x00\x00" + twice[at + 12:])
    assert stacking._anchor_picture_style(tmp_path / "dup.jpg") is None
    # A RAW path reads its JPEG twin.
    (tmp_path / "anchor.ARW").write_bytes(b"not decoded")
    (tmp_path / "anchor.JPG").write_bytes(data)
    assert stacking._anchor_picture_style(tmp_path / "anchor.ARW") == {"name": "BW", "monochrome": True}
    (tmp_path / "lonely.ARW").write_bytes(b"not decoded")
    assert stacking._anchor_picture_style(tmp_path / "lonely.ARW") is None
    # Two different twins: which one staging decoded is not provable here, so the check runs.
    (tmp_path / "pair.ARW").write_bytes(b"not decoded")
    (tmp_path / "pair.JPG").write_bytes(data)
    (tmp_path / "pair.jpeg").write_bytes(Path(_sony_exif_jpeg(pixels, tmp_path / "std.jpg", style="Standard")).read_bytes())
    assert stacking._anchor_picture_style(tmp_path / "pair.ARW") is None


def _jpeg_with_tiff(pixels, path, tiff):
    """Write ``pixels`` as a JPEG whose APP1 Exif segment carries exactly ``tiff``."""
    import struct
    segment = b"Exif\x00\x00" + tiff
    ok, encoded = cv2.imencode(".jpg", pixels, [cv2.IMWRITE_JPEG_QUALITY, 97])
    assert ok
    data = encoded.tobytes()
    Path(path).write_bytes(data[:2] + b"\xff\xe1" + struct.pack(">H", len(segment) + 2) + segment + data[2:])
    return path


def _inline_maker(tag, kind, value):
    """A Sony MakerNote with one entry whose value fits inline (4 bytes)."""
    import struct
    return (b"SONY DSC \x00\x00\x00" + struct.pack("<H", 1)
            + struct.pack("<HHI", tag, kind, 3 if kind == 2 else 1) + value.ljust(4, b"\x00") + b"\x00" * 4)



def _forged_tiff(case):
    """Little-endian TIFFs around one BW MakerNote: ``single`` is well formed; the others duplicate
    the MakerNote or the Exif-pointer tag, or point the Exif IFD back at (or into) IFD0."""
    import struct
    black_white = _inline_maker(0xB020, 2, b"BW\x00")
    header = b"II*\x00" + struct.pack("<I", 8)
    if case == "single":
        exif_at = 8 + 18
        return (header + struct.pack("<H", 1) + struct.pack("<HHII", 0x8769, 4, 1, exif_at) + b"\x00" * 4
                + struct.pack("<H", 1) + struct.pack("<HHII", 0x927C, 7, len(black_white), exif_at + 18)
                + b"\x00" * 4 + black_white)
    if case == "two_makers":                    # the first says BW, the second says no effect
        no_effect = _inline_maker(0x200E, 3, struct.pack("<H", 0))
        exif_at = 8 + 18
        first_at = exif_at + 2 + 24 + 4
        return (header + struct.pack("<H", 1) + struct.pack("<HHII", 0x8769, 4, 1, exif_at) + b"\x00" * 4
                + struct.pack("<H", 2) + struct.pack("<HHII", 0x927C, 7, len(black_white), first_at)
                + struct.pack("<HHII", 0x927C, 7, len(no_effect), first_at + len(black_white)) + b"\x00" * 4
                + black_white + no_effect)
    if case == "two_pointers":
        exif_at = 8 + 2 + 24 + 4
        return (header + struct.pack("<H", 2) + struct.pack("<HHII", 0x8769, 4, 1, exif_at)
                + struct.pack("<HHII", 0x8769, 4, 1, exif_at) + b"\x00" * 4
                + struct.pack("<H", 1) + struct.pack("<HHII", 0x927C, 7, len(black_white), exif_at + 18)
                + b"\x00" * 4 + black_white)
    if case == "overlapping":                   # the Exif IFD [22, 40) starts inside IFD0 [8, 26)
        # IFD0's next-directory field doubles as the Exif IFD's count (1) and its entry's tag.
        return (header + struct.pack("<H", 1) + struct.pack("<HHII", 0x8769, 4, 1, 22)
                + struct.pack("<HH", 1, 0x927C) + struct.pack("<HII", 7, len(black_white), 40)
                + b"\x00" * 4 + black_white)
    assert case == "aliased"                    # IFD0 is its own Exif IFD and holds the MakerNote
    return (header + struct.pack("<H", 2) + struct.pack("<HHII", 0x8769, 4, 1, 8)
            + struct.pack("<HHII", 0x927C, 7, len(black_white), 8 + 2 + 24 + 4) + b"\x00" * 4
            + black_white)


@pytest.mark.parametrize("case", ["two_makers", "two_pointers", "aliased", "overlapping"])
def test_duplicate_or_aliased_exif_directories_leave_the_style_unknown(tmp_path, case):
    """PR #113 review follow-up: a duplicated MakerNote or Exif-pointer tag, or an Exif pointer
    whose directory overlaps IFD0, is not provably the camera's style. The reader treats each as
    unknown (None), so the anchor check keeps running instead of abstaining as monochrome."""
    pixels = np.full((32, 32, 3), 128, np.uint8)
    control = _jpeg_with_tiff(pixels, tmp_path / "single.jpg", _forged_tiff("single"))
    assert stacking._anchor_picture_style(control) == {"name": "BW", "monochrome": True}
    forged = _jpeg_with_tiff(pixels, tmp_path / f"{case}.jpg", _forged_tiff(case))
    assert stacking._anchor_picture_style(forged) is None
