"""The crowd auto-stop's spatial signal: every once-occupied region must be seen clear again.

No camera hardware. Synthetic frames run through the real live-preview pipeline
(``stack_session.crowd_preview``: registration, estimator, 25% rule, plate) and the real session
runner. The global cleared share stays a display figure; these tests pin the cases it cannot
judge: someone who sits down, a close passer-by filling most of the frame, and a room that
relights between frames.
"""
from pathlib import Path
import sys

import cv2
import numpy as np
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))
import crowd_clear_regions
import engine
import product_sessions
import stack_session
from stack_session import merge_session_frames

METADATA = {"camera_model": "Sony ILCE-7M2 (Control)", "camera_port": "usb:001,002", "camera_serial": "A7II00SERIAL"}
HEIGHT, WIDTH = 240, 320
SEAT = (slice(120, 200), slice(150, 210))  # where the occupant sits down


def _background():
    rng = np.random.default_rng(11)
    base = rng.integers(40, 216, (HEIGHT // 8, WIDTH // 8, 3), dtype=np.uint8)
    base = cv2.resize(base, (WIDTH, HEIGHT), interpolation=cv2.INTER_CUBIC)
    for _ in range(40):
        x, y = int(rng.integers(0, WIDTH)), int(rng.integers(0, HEIGHT))
        cv2.circle(base, (x, y), int(rng.integers(3, 12)), tuple(int(c) for c in rng.integers(0, 255, 3)), 2)
    return base


BACKGROUND = _background()


def _person(shape, seed):
    """A textured occupant patch that is unrelated to the background behind it."""
    rng = np.random.default_rng(seed)
    coarse = rng.integers(20, 120, (max(1, shape[0] // 10), max(1, shape[1] // 10), 3), dtype=np.uint8)
    return cv2.resize(coarse, (shape[1], shape[0]), interpolation=cv2.INTER_CUBIC)


SITTER = _person((SEAT[0].stop - SEAT[0].start, SEAT[1].stop - SEAT[1].start), 5)


def _passers_by(image, index):
    """Two passers-by in different places in each frame; neither returns."""
    x0 = 10 + 70 * index
    image[10:70, x0:x0 + 34] = _person((60, 34), 100 + index)
    x1 = 270 - 70 * index
    image[150:220, max(0, x1):max(0, x1) + 34] = _person((70, 34), 200 + index)[:, :max(0, min(34, WIDTH - x1))]
    return image


def leaving_frame(index):
    """A crowd that walks through frames 0-2 and is gone from frame 3."""
    image = BACKGROUND.copy()
    return _passers_by(image, index) if index < 3 else image


def seated_frame(index, sits_at=4):
    """Passers-by in the first frames, then one person sits down and stays."""
    image = leaving_frame(index)
    if index >= sits_at:
        image[SEAT] = SITTER
    return image


def relit_frame(index):
    """An empty room whose light changes level and colour between frames."""
    gains = [(1.0, 1.0, 1.0), (0.82, 0.84, 0.86), (1.15, 1.1, 1.04), (0.9, 0.96, 1.05), (1.22, 1.2, 1.18),
             (0.86, 0.9, 0.93)]
    return np.clip(BACKGROUND.astype(np.float32) * np.array(gains[index], np.float32), 0, 255).astype(np.uint8)


def close_passer_frame(index, at):
    """An empty scene except frame ``at``, where someone walks right past the lens and fills ~85%."""
    image = BACKGROUND.copy()
    if index == at:
        cover = int(WIDTH * 0.85)
        shirt = np.empty((HEIGHT, cover, 3), np.uint8)
        shirt[:] = (96, 104, 112)
        noise = np.random.default_rng(3).normal(0, 3, shirt.shape)
        shirt = np.clip(shirt + noise + np.linspace(-12, 12, cover)[None, :, None], 0, 255).astype(np.uint8)
        image[:, :cover] = shirt
    return image


def _preview(tmp_path, make_frame, count, **kwargs):
    folder = tmp_path / f"prefix_{count}"
    folder.mkdir()
    paths = []
    for index in range(count):
        path = folder / f"src_{index}.png"
        assert cv2.imwrite(str(path), make_frame(index, **kwargs))
        paths.append(str(path))
    return stack_session.crowd_preview(paths, folder / "preview.jpg", folder / "not_cleared.png")


# --- 1. The signal on the real preview pipeline --------------------------------------------

@pytest.mark.parametrize("count", [5, 6, 7, 8])
def test_someone_who_sits_down_is_never_seen_clear(tmp_path, count):
    preview = _preview(tmp_path, seated_frame, count)
    regions = preview["regions"]
    assert regions["all_seen_clear"] is False and regions["pending_regions"] > 0
    # The seat is small, so the global share alone would have called the plate clean.
    assert preview["cleared_share"] > 0.8


@pytest.mark.parametrize("count", [6, 7, 8])
def test_a_sitter_who_outlasts_the_background_is_ambiguous_not_clear(tmp_path, count):
    """Seated from frame 2: by the end the plate itself may show them, so the newest frames agree
    with it. The earlier, stable empty seat is the other state pixels cannot rule out."""
    preview = _preview(tmp_path, seated_frame, count, sits_at=2)
    regions = preview["regions"]
    assert regions["all_seen_clear"] is False and regions["pending_regions"] > 0


def test_close_passer_by_in_the_newest_frame_blocks_the_stop(tmp_path):
    preview = _preview(tmp_path, close_passer_frame, 5, at=4)
    assert preview["regions"]["all_seen_clear"] is False
    # The frame cannot set a gain from the person (their tiles do not correlate with the plate),
    # so either no gain was measured or most of the frame is pending; nothing reads as clear.
    regions = preview["regions"]
    assert regions["frames_without_gain"] == [4] or regions["pending_share"] > 0.7


def test_close_passer_by_who_has_left_is_seen_clear_afterwards(tmp_path):
    preview = _preview(tmp_path, close_passer_frame, 6, at=1)
    assert preview["regions"]["all_seen_clear"] is True
    assert preview["regions"]["ever_occupied_share"] > 0.7


def test_relighting_a_clean_scene_marks_nothing_occupied(tmp_path):
    preview = _preview(tmp_path, lambda index: relit_frame(index), 6)
    regions = preview["regions"]
    assert regions["all_seen_clear"] is True
    assert regions["ever_occupied_share"] == 0.0 and regions["frames_without_gain"] == []


def clipped_relit_frame(index):
    """An empty room with a clipped window, brightening by a quarter in the last two frames."""
    image = BACKGROUND.copy()
    image[32:208, 32:256] = 255
    gain = 1.25 if index >= 3 else 1.0
    return np.clip(image.astype(np.float32) * gain, 0, 255).astype(np.uint8)


def test_relighting_with_a_clipped_highlight_marks_nothing_occupied(tmp_path):
    """A clipped value is a lower bound: scaling it by the inverse gain is not a departure."""
    regions = _preview(tmp_path, lambda index: clipped_relit_frame(index), 5)["regions"]
    assert regions["all_seen_clear"] is True and regions["pending_regions"] == 0, regions


def test_someone_dark_in_front_of_a_clipped_window_still_departs():
    window = clipped_relit_frame(0)
    visitor = window.copy()
    visitor[60:180, 100:160] = (60, 70, 80)
    report = crowd_clear_regions.seen_clear_regions([window, window, window, visitor, visitor], window)
    assert report["report"]["all_seen_clear"] is False and report["occupied"][3].any()


def _red_shirt(pattern):
    """A blown white window; S = someone in a red shirt (clipped red channel) sits in front of it."""
    window = BACKGROUND.copy()
    window[32:208, 32:256] = 255
    shirt = window.copy()
    shirt[80:180, 100:170] = (60, 80, 255)
    return [shirt.copy() if mark == "S" else window.copy() for mark in pattern]


@pytest.mark.parametrize("plate_index", [0, 2])
def test_a_red_shirt_against_a_blown_window_still_departs(plate_index):
    """One clipped channel is not a clipped pixel: the shirt's blue and green are measured."""
    frames = _red_shirt("EESSS")
    report = crowd_clear_regions.seen_clear_regions(frames, frames[plate_index])["report"]
    assert report["all_seen_clear"] is False and report["pending_regions"] > 0, report


def test_a_red_shirt_against_a_blown_window_never_auto_stops(sessions):
    frames = _red_shirt("EESSSSSS")
    result, seen, calls = _run(sessions, lambda index: frames[index], 0.5)
    assert calls == list(range(8)) and "stopped_early" not in result


def _pink_on_pale(pattern):
    """A warm pale wall (clipped red); S = someone in pale pink (clipped blue and red) in front of it."""
    image = BACKGROUND.copy()
    empty, sitter = image.copy(), image.copy()
    empty[80:180, 100:180] = (230, 240, 255)
    sitter[80:180, 100:180] = (255, 212, 255)
    return [sitter.copy() if mark == "S" else empty.copy() for mark in pattern]


@pytest.mark.parametrize("pattern", ["EESSSSSS", "EEESSSSS"])
def test_pale_pink_against_a_warm_pale_wall_never_auto_stops(sessions, pattern):
    """Chroma still counts near a clipped channel: the clipped values are read as their most
    agreeing bound, and the pink still departs from the wall."""
    frames = _pink_on_pale(pattern)
    result, seen, calls = _run(sessions, lambda index: frames[index], 0.5)
    assert calls == list(range(8)) and "stopped_early" not in result


def saturated_relit_frame(index):
    """An empty room with a near-saturated red cast; the red channel brightens 25% (and clips)
    in the last two frames, everywhere except a darker red patch that stays measurable."""
    image = BACKGROUND.copy()
    image[:, :, 2] = 245
    image[80:180, 100:180, 2] = 190
    if index >= 3:
        image[:, :, 2] = np.clip(image[:, :, 2].astype(np.float32) * 1.25, 0, 255).astype(np.uint8)
    return image


def test_a_relight_that_clips_one_channel_marks_nothing_occupied(tmp_path):
    """Clipped samples never set the gain, and a channel clipped by the relight is a bound."""
    regions = _preview(tmp_path, lambda index: saturated_relit_frame(index), 5)["regions"]
    assert regions["all_seen_clear"] is True and regions["pending_regions"] == 0, regions


def _thin_silhouette(pattern):
    """A blown window; S = a distant seated figure about 8 px wide, dark, inside the window."""
    window = BACKGROUND.copy()
    window[32:208, 32:256] = 255
    figure = window.copy()
    figure[90:150, 140:148] = 60
    return [figure.copy() if mark == "S" else window.copy() for mark in pattern]


def test_a_thin_figure_in_front_of_a_blown_window_still_departs():
    """Only the clipped samples themselves are bounds: the figure is measured where it stands."""
    frames = _thin_silhouette("EESSS")
    for plate in (frames[0], frames[2]):
        report = crowd_clear_regions.seen_clear_regions(frames, plate)["report"]
        assert report["all_seen_clear"] is False and report["ever_occupied_share"] > 0, report


def _bridged(pattern):
    """E, M, S: an empty seat, a blurred half-arrival and someone seated, as greys 120 / 145 / 170."""
    image = BACKGROUND.copy()
    states = {}
    for mark, grey in (("E", 120), ("M", 145), ("S", 170)):
        states[mark] = image.copy()
        states[mark][90:150, 120:200] = grey
    return [states[mark].copy() for mark in pattern]


def test_an_intermediate_frame_does_not_bridge_two_recurring_states():
    report = crowd_clear_regions.seen_clear_regions(_bridged("EEMSS"), _bridged("M")[0])["report"]
    assert report["all_seen_clear"] is False and report["ambiguous_regions"] > 0, report


@pytest.mark.parametrize("make", [_thin_silhouette, _bridged], ids=["thin_figure", "bridge"])
def test_thin_figure_and_bridged_states_never_auto_stop(sessions, make):
    frames = make("EESSSSSS") if make is _thin_silhouette else make("EEMSSSSS")
    result, seen, calls = _run(sessions, lambda index: frames[index], 0.5)
    assert calls == list(range(8)) and "stopped_early" not in result


def test_crowd_that_leaves_is_seen_clear_once_two_frames_show_it(tmp_path):
    assert crowd_clear_regions.RECENT_CLEAR_FRAMES == 2
    assert _preview(tmp_path, lambda index: leaving_frame(index), 4)["regions"]["all_seen_clear"] is False
    after = _preview(tmp_path, lambda index: leaving_frame(index), 5)["regions"]
    assert after["all_seen_clear"] is True and after["ever_occupied_share"] > 0.05


# --- 2. Unit behaviour of the tile rule --------------------------------------------------------

def test_frames_must_share_the_plate_grid():
    with pytest.raises(ValueError, match="registered pixel grid"):
        crowd_clear_regions.seen_clear_regions([BACKGROUND[:-1]], BACKGROUND)
    with pytest.raises(ValueError, match="at least one frame"):
        crowd_clear_regions.seen_clear_regions([], BACKGROUND)


def test_an_unreadable_frame_vouches_for_nothing():
    """A frame with no textured tile matching the plate has no gain: every tile is occupied."""
    flat = np.full_like(BACKGROUND, 128)
    result = crowd_clear_regions.seen_clear_regions([BACKGROUND, BACKGROUND, flat], BACKGROUND)
    assert result["report"]["frames_without_gain"] == [2]
    assert result["occupied"][2].all() and result["report"]["all_seen_clear"] is False


def _sat(pattern):
    """Frames from a pattern string: E = empty seat, S = the same person seated."""
    sat = BACKGROUND.copy()
    sat[SEAT] = SITTER
    return [sat.copy() if mark == "S" else BACKGROUND.copy() for mark in pattern]


@pytest.mark.parametrize("pattern", ["SEESS", "ESSEE", "SSEEE", "EESSS", "ESESS"])
def test_a_state_seen_in_two_frames_is_ambiguous_in_any_order(pattern):
    """Seated, away for two frames, back again (the plate shows them), and every other order:
    a second state seen in the 25% floor of the frames could be the background, so the seat
    stays pending whichever state the plate holds and whatever came first."""
    frames = _sat(pattern)
    for plate in (BACKGROUND, frames[pattern.index("S")]):
        report = crowd_clear_regions.seen_clear_regions(frames, plate)["report"]
        assert report["all_seen_clear"] is False and report["ambiguous_regions"] > 0, (pattern, report)
        assert report["alternative_floor_frames"] == 2


@pytest.mark.parametrize("before", ["E", "PQ"], ids=["empty_seat_once", "two_passers_by"])
def test_departures_seen_once_each_read_as_passers_by(before):
    """The documented limit: one empty-seat frame, or two different passers-by, before a steady
    sitter look exactly like passers-by before steady background (a crowd that left). The hidden
    background shows in at most one frame so far, so within the 8-frame automatic budget it can
    never become the per-pixel majority the merge needs."""
    passers = {"P": (200, 40, 40), "Q": (40, 200, 40)}  # two differently dressed passers-by
    frames = []
    for mark in before + "S" * (5 - len(before)):
        frame = BACKGROUND.copy()
        frame[SEAT] = SITTER if mark == "S" else passers.get(mark, BACKGROUND[SEAT])
        frames.append(frame)
    report = crowd_clear_regions.seen_clear_regions(frames, frames[-1])["report"]
    assert report["all_seen_clear"] is True and report["ever_occupied_share"] > 0
    hidden = 1 if before == "E" else 0
    assert hidden + (8 - len(frames)) <= 8 // 2  # the background cannot outvote them by frame 8


def test_a_gainless_frame_may_match_any_alternative():
    """A frame whose gain cannot be measured could hold the same content as a one-frame departure."""
    sat = _sat("S")[0]
    flat = np.full_like(BACKGROUND, 128)
    frames = [sat, flat, BACKGROUND, BACKGROUND, BACKGROUND]
    report = crowd_clear_regions.seen_clear_regions(frames, BACKGROUND)["report"]
    assert report["frames_without_gain"] == [1] and report["all_seen_clear"] is False


# --- 3. The session runner: the stop obeys the regions, not only the share ----------------------

@pytest.fixture
def sessions(tmp_path, monkeypatch):
    monkeypatch.setattr(engine, "SHOTS_DIR", tmp_path / "shots")
    return product_sessions.runner()


def _writer(make_frame, calls):
    def capture(index, spec, destination):
        calls.append(index)
        assert cv2.imwrite(destination, make_frame(index), [cv2.IMWRITE_JPEG_QUALITY, 95])
        return destination
    return capture


def _run(sessions, make_frame, threshold, budget=8):
    state = sessions.create("crowd_remove", budget, metadata=METADATA)
    seen, calls = [], []
    options = {"preview_every": 1, "stop_when_cleared": threshold}

    def after(live):
        product_sessions._crowd_boundary(sessions, state["id"], live, options)
        seen.append(dict(live.get("preview_crowd") or {}))

    result = sessions.run(state["id"], _writer(make_frame, calls),
                          lambda paths, out: merge_session_frames("crowd_remove", paths, out), after_frame=after)
    assert result["status"] == "completed", result.get("error")
    return result, [item for item in seen if item], calls


def test_seated_occupant_never_auto_stops_though_the_share_says_clean(sessions):
    """Reverting the region gate stops at frame 5 here: the share alone crosses the threshold."""
    result, seen, calls = _run(sessions, seated_frame, 0.8)
    assert any(item["accepted_frames"] >= product_sessions.CROWD_AUTO_STOP_MIN_FRAMES
               and item["cleared_share"] >= 0.8 for item in seen)
    assert calls == list(range(8)) and "stopped_early" not in result
    assert all(item["regions"]["all_seen_clear"] is False for item in seen if item["accepted_frames"] >= 5)


def test_crowd_that_leaves_auto_stops_once_seen_clear(sessions):
    result, seen, calls = _run(sessions, leaving_frame, 0.9)
    assert calls == [0, 1, 2, 3, 4]
    stop = result["stopped_early"]
    assert stop["reason"] == "cleared_share_threshold" and stop["accepted_frames"] == 5
    assert stop["all_seen_clear"] is True and stop["cleared_share"] >= 0.9
    assert seen[-1]["regions"]["all_seen_clear"] is True


@pytest.mark.parametrize("regions", [
    None, "yes", {}, {"all_seen_clear": "true", "pending_regions": 0, "pending_share": 0.0},
    {"all_seen_clear": True}, {"all_seen_clear": True, "pending_regions": 7, "pending_share": 0.2},
    {"all_seen_clear": True, "pending_regions": 0, "pending_share": 0.01},
    {"all_seen_clear": True, "pending_regions": False, "pending_share": 0.0},
    {"all_seen_clear": True, "pending_regions": 0.0, "pending_share": 0.0},
    {"all_seen_clear": True, "pending_regions": 0, "pending_share": 0},  # incomplete
    {"all_seen_clear": True, "pending_regions": 0, "pending_share": 0, "ambiguous_regions": 1,
     "recent_clear_frames": 2},
    {"all_seen_clear": True, "pending_regions": 0, "pending_share": 0, "ambiguous_regions": 0,
     "recent_clear_frames": 0},
])
def test_a_missing_or_inconsistent_regions_report_never_stops(sessions, monkeypatch, regions):
    """Missing or contradictory spatial evidence (an older preview record) is not a licence to stop."""
    real = stack_session.crowd_preview

    def replaced(*args, **kwargs):
        report = real(*args, **kwargs)
        report.pop("regions")
        if regions is not None:
            report["regions"] = regions
        return report

    monkeypatch.setattr(stack_session, "crowd_preview", replaced)
    result, seen, calls = _run(sessions, leaving_frame, 0.5, budget=6)
    assert calls == list(range(6)) and "stopped_early" not in result
