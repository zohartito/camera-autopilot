"""The HDR merge must hand back the camera's photograph with the blown window fixed.

Written 2026-09-20, third pass over the same rejection. With the deghost blotches gone and
the fusion pyramid carried to full depth, the merge was still rejected: "it's making yellow
brown". That was measurable and it was not taste. Exposure fusion re-renders the WHOLE frame
— well-exposedness pulls every pixel toward mid-grey, and blending gamma-encoded values from
different exposures shifts hue — so on the A7 II daylight room the fused render measured
L 136.4 against the metered bracket's 146.1 and b* 135.99 against 135.48. Darker and
yellower is brown.

Raising the tonality curve could buy the lightness back, but only by re-crunching local
detail (1.74x the bracket at strength 0.85), and it could not touch the hue at all because it
only modifies L. The knob was never the problem: re-rendering the whole photograph was.

So the merge now keeps the metered bracket and substitutes fused content only where that
bracket is losing information. These tests pin that, and the sharpest of them is that outside
the recovery region the output IS the bracket, not something close to it.
"""
import sys
from pathlib import Path

import cv2
import numpy as np
import pytest

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "backend"))
import stacking  # noqa: E402

_SESSIONS = REPO / "backend" / "shots" / "sessions"


def _bracket(tmp_path):
    """A room the camera cannot hold in one frame: a window that blows, interior that does not."""
    rng = np.random.default_rng(17)
    height, width = 704, 896
    rows, cols = np.mgrid[0:height, 0:width].astype(np.float32)
    base = (95 + 60 * (cols / width) + 25 * np.sin(rows / height * 2.0))[:, :, None].repeat(3, axis=2)
    base[:, :, 2] *= 1.18                                   # a warm interior, so a cast would show
    base[60:380, 800:] = 252.0                              # the window
    base[120:380, 120:660] += rng.normal(0, 13, (260, 540, 1))
    paths = []
    for index, gain in enumerate((0.4, 1.0, 2.2)):
        frame = np.clip(base * gain + rng.normal(0, 1.3, base.shape), 0, 255).astype(np.uint8)
        path = tmp_path / f"frame_{index}.jpg"
        cv2.imwrite(str(path), frame, [cv2.IMWRITE_JPEG_QUALITY, 98])
        paths.append(str(path))
    return paths


def _lab(image):
    return cv2.cvtColor(image, cv2.COLOR_BGR2LAB).astype(np.float32)


def test_away_from_the_highlights_the_output_is_the_metered_frame(tmp_path):
    """The invariant the whole finish rests on.

    Not "close to" the bracket: the bracket. If the merge is free to re-render a correctly
    exposed wall, it will drift in lightness and hue, which is what was rejected twice.
    """
    paths = _bracket(tmp_path)
    merged = stacking.hdr_merge(paths, deghost=False)
    anchor = cv2.imread(paths[1])
    interior = (slice(420, 690), slice(40, 700))    # well exposed, far from the window
    assert _lab(anchor)[interior][:, :, 0].max() < stacking.HDR_RECOVERY_HIGHLIGHT, \
        "this region must be outside the recovery mask or the test proves nothing"
    difference = np.abs(merged[interior].astype(np.int16) - anchor[interior].astype(np.int16))
    # Exactly the bracket. This allowed 3 code values until 2026-09-27, to cover a LAB round
    # trip the finish ran on every pixel; that round trip now runs only inside the mask.
    assert difference.max() == 0, f"the merge altered a correctly exposed region by {difference.max()}"


def _recovery_weight(anchor):
    """The finish's feathered blend weight, computed the way ``hdr_merge`` computes it."""
    lightness = cv2.cvtColor(anchor, cv2.COLOR_BGR2LAB)[:, :, 0].astype(np.float32)
    weight = np.clip((lightness - stacking.HDR_RECOVERY_HIGHLIGHT)
                     / (255. - stacking.HDR_RECOVERY_HIGHLIGHT), 0, 1)
    return cv2.GaussianBlur(weight, (0, 0), stacking.HDR_RECOVERY_FEATHER)


def test_wherever_the_feathered_mask_is_zero_the_pixels_are_the_brackets(tmp_path):
    """Byte-identical outside the mask, on a bracket larger than one tile, and still recovered
    inside it. Would catch any whole-frame pass (a colour-space round trip, a curve, a blend
    with a zero weight) touching pixels the recovery never meant to change."""
    paths = _bracket(tmp_path)
    anchor = cv2.imread(paths[1])
    assert anchor.shape[0] > stacking.STACK_TILE_SIZE and anchor.shape[1] > stacking.STACK_TILE_SIZE
    merged = stacking.hdr_merge(paths)
    outside = _recovery_weight(anchor) == 0
    assert 0.5 < outside.mean() < 0.99, "the fixture must have both a mask and a clean region"
    changed = np.any(merged != anchor, axis=2)
    assert not changed[outside].any(), \
        f"{changed[outside].mean() * 100:.3f} % of pixels outside the mask differ from the bracket"
    blown = cv2.cvtColor(anchor, cv2.COLOR_BGR2GRAY) >= 250
    assert blown.mean() > 0.02, "the window must blow in the metered frame"
    recovered = cv2.cvtColor(merged, cv2.COLOR_BGR2GRAY)[blown]
    assert (recovered >= 250).mean() < 0.1, "the blown window must come back inside the mask"


def test_the_colour_of_the_photograph_is_the_cameras(tmp_path):
    """The 'yellow brown' complaint, as a number. b* is the yellow-blue axis."""
    paths = _bracket(tmp_path)
    merged, anchor = _lab(stacking.hdr_merge(paths)), _lab(cv2.imread(paths[1]))
    assert abs(merged[:, :, 2].mean() - anchor[:, :, 2].mean()) < 1.0, "yellow-blue drift"
    assert abs(merged[:, :, 1].mean() - anchor[:, :, 1].mean()) < 1.0, "green-red drift"
    assert abs(merged[:, :, 0].mean() - anchor[:, :, 0].mean()) < 4.0, "lightness drift"


def test_the_window_comes_back(tmp_path):
    """Preserving the bracket is only worth anything if the merge still fixes what was wrong."""
    paths = _bracket(tmp_path)
    clipped = lambda image: float((cv2.cvtColor(image, cv2.COLOR_BGR2GRAY) >= 250).mean())
    assert clipped(stacking.hdr_merge(paths)) < clipped(cv2.imread(paths[1])) / 10


def test_the_fused_render_is_what_the_finish_replaces(tmp_path):
    """Pin the defect the finish exists for: the raw fused render is darker than the bracket.

    Only the lightness half is asserted here. The hue drift needs real mixed lighting — a
    smooth synthetic ramp reproduces it at b* 131.310 against 131.317, which is nothing — so
    it is pinned on the private daylight session at the bottom of this file instead. Asserting
    it here would be asserting something this fixture does not actually produce.
    """
    paths = _bracket(tmp_path)
    fused = _lab(stacking.hdr_merge(paths, recover=False, tonality=0.0))
    anchor = _lab(cv2.imread(paths[1]))
    assert fused[:, :, 0].mean() < anchor[:, :, 0].mean() - 4, "fusion should pull toward mid-grey"


def test_the_curve_rolls_off_instead_of_restoring_the_clipping():
    """Matching the anchor's full histogram would reproduce its clipped top and undo the work."""
    anchor = np.zeros(256, dtype=np.int64)
    anchor[60:200] = 1000                       # a well-exposed bracket, nothing above 200
    fused = np.zeros(256, dtype=np.int64)
    fused[40:250] = 1000                        # the fusion carries detail well above it
    lut = stacking._recovery_lut(fused, anchor, stacking.HDR_RECOVERY_CEILING)
    assert np.all(np.diff(lut.astype(np.int16)) >= 0), "the curve must stay monotone"
    assert lut[255] <= stacking.HDR_RECOVERY_CEILING, "highlights must land under the ceiling"
    assert lut[255] > lut[200], "the top must still carry separation, not flatten to one value"


def test_a_degenerate_histogram_falls_back_to_identity():
    identity = stacking._recovery_lut(np.zeros(256, dtype=np.int64),
                                      np.zeros(256, dtype=np.int64), 248.)
    assert np.array_equal(identity, np.arange(256, dtype=np.uint8))


@pytest.mark.skipif(not (_SESSIONS / "02290746a9614e1b9bf74040e9a417ce" / "frame_0001.jpg").is_file(),
                    reason="private daylight HDR session 02290746 is not on this machine")
def test_real_daylight_bracket_keeps_its_colour_and_loses_its_clipping():
    session = _SESSIONS / "02290746a9614e1b9bf74040e9a417ce"
    paths = [str(session / f"frame_{i:04d}.jpg") for i in range(3)]
    merged, anchor = _lab(stacking.hdr_merge(paths)), _lab(cv2.imread(paths[1]))
    # Measured 2026-09-20: b* 141.57 against the bracket's 141.80, L 145.3 against 145.9,
    # highlight clipping 3.32% -> 0.01%. The fused render sat at b* 142.6 and L 138.2.
    assert abs(merged[:, :, 2].mean() - anchor[:, :, 2].mean()) < 0.6
    assert abs(merged[:, :, 0].mean() - anchor[:, :, 0].mean()) < 2.0
    clipped = lambda l: float((l[:, :, 0] >= 250).mean())
    assert clipped(merged) < clipped(anchor) / 10


def test_no_step_where_the_feather_ends_on_a_saturated_sky(tmp_path):
    """Copying the bracket only where the weight is zero is not enough on its own: if the
    finish still pushed the anchor through an 8-bit LAB round trip wherever the weight is
    positive, a saturated blue would jump by up to ~25 code values at the edge of the feather's
    support, a ring 64 px out from every highlight. Blending in BGR keeps the edge continuous.
    """
    height, width = 704, 896
    base = np.full((height, width, 3), 120.0, dtype=np.float32)
    base[60:380, 400:] = 252.0                               # the window, across a tile seam
    base[380:700, :] = (250.0, 164.0, 1.0)                   # sky blue: worst 8-bit LAB round trip
    paths = []
    for index, gain in enumerate((0.4, 1.0, 2.2)):
        path = tmp_path / f"frame_{index}.png"
        cv2.imwrite(str(path), np.clip(base * gain, 0, 255).astype(np.uint8))
        paths.append(str(path))
    anchor = cv2.imread(paths[1])
    weight = _recovery_weight(anchor)
    rim = (weight > 0) & (weight < 0.01) & np.all(anchor == (250, 164, 1), axis=2)
    assert rim.sum() > 1000, "the sky must cross the edge of the feather's support"
    merged = stacking.hdr_merge(paths, deghost=False)
    delta = np.abs(merged.astype(np.int16) - anchor.astype(np.int16)).max(axis=2)
    assert delta[weight == 0].max() == 0
    # A weight under 1 % can move a pixel by at most 2.55 code values, plus truncation.
    assert delta[rim].max() <= 3, f"a step of {delta[rim].max()} code values where the feather ends"
