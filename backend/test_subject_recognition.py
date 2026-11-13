"""Tests for subject recognition -> shutter floor (Arsenal parity item 2).

Camera-free and ML-free: the CLIP encoder is never loaded. A stub classifier stands in for the
model, so these run identically on macOS and on the Ubuntu CI runner (neither ships open_clip).
The determinism boundary is what is under test: the class -> floor mapping, the focal rule, the
confidence gate, graceful degradation, and that the floor survives the exposure solver.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import pytest

import physics
import subject_recognition as sr

ORIGINAL_CLASSIFY_TILES = sr._clip_classify_tiles


@pytest.fixture(autouse=True)
def _no_real_tile_pass(monkeypatch):
    """The tile pass must never load CLIP here: a test that does not pass `tile_classifier` sees
    an empty room (every tile a still scene)."""
    monkeypatch.setattr(sr, "_clip_classify_tiles", lambda _path: _tiles())


def _tiles(person_tile: int | None = None, person: float = 0.9, person_in_motion: float = 0.0,
           artwork: float = 0.0, count: int = 7) -> list[dict]:
    """Deterministic stubbed tile scores: every tile a still scene except `person_tile`."""
    tiles = []
    for index in range(count):
        if index == person_tile:
            rest = 1.0 - person - person_in_motion - artwork
            probabilities = {"person": person, "person_in_motion": person_in_motion,
                             "artwork": artwork, "static_object": rest}
        else:
            probabilities = {"static_object": 0.97, "person": 0.01, "artwork": 0.02}
        tiles.append({"box": [0.0, 0.0, 0.4, 0.6], "probabilities": probabilities})
    return tiles


def _stub(top_class: str, confidence: float = 0.9):
    """A classifier stub: puts `confidence` on `top_class`, spreads the rest evenly."""
    others = [c for c in sr.SUBJECT_PROMPTS if c != top_class]
    rest = (1.0 - confidence) / len(others)
    probs = {c: rest for c in others}
    probs[top_class] = confidence
    return lambda _image_path: probs


# --- the floor is applied for fast-moving subjects ------------------------
def test_bird_sets_a_fast_floor() -> None:
    ev = sr.subject_evidence("meter.jpg", focal_mm=50, classifier=_stub("bird"))
    assert ev["status"] == "applied" and ev["class"] == "bird"
    assert ev["floor_s"] == 1 / 2000, ev
    print(f"  bird -> floor {ev['floor_s']} ({ev['reason']})")


def test_vehicle_and_person_in_motion_set_floors() -> None:
    veh = sr.subject_evidence("m.jpg", focal_mm=50, classifier=_stub("vehicle"))
    moving = sr.subject_evidence("m.jpg", focal_mm=50, classifier=_stub("person_in_motion"))
    assert veh["status"] == "applied" and veh["floor_s"] == 1 / 1000, veh
    assert moving["status"] == "applied" and moving["floor_s"] == 1 / 1000, moving


# --- no floor for water / landscape / static -------------------------------
def test_landscape_and_water_impose_no_floor() -> None:
    for cls in ("sky_or_landscape", "flowing_water", "static_object"):
        ev = sr.subject_evidence("m.jpg", focal_mm=200, classifier=_stub(cls))
        assert ev["status"] == "no_floor", (cls, ev)
        assert ev["floor_s"] is None, (cls, ev)
    print("  landscape / water / static object -> no floor (even at 200mm)")


# --- low confidence: no floor, reason recorded -----------------------------
def test_low_confidence_applies_no_floor() -> None:
    # A bird guess just under the threshold must not constrain the camera.
    conf = sr.CONFIDENCE_THRESHOLD - 0.05
    ev = sr.subject_evidence("m.jpg", focal_mm=50, classifier=_stub("bird", confidence=conf))
    assert ev["status"] == "low_confidence" and ev["floor_s"] is None, ev
    assert "threshold" in ev["reason"], ev


# --- CLIP unavailable: changes nothing, records `unavailable` --------------
def test_clip_unavailable_degrades_gracefully() -> None:
    def broken(_image_path):
        raise RuntimeError("CLIP embedding requires the ML environment")

    ev = sr.subject_evidence("m.jpg", focal_mm=50, classifier=broken)
    assert ev["status"] == "unavailable" and ev["floor_s"] is None, ev
    assert ev["class"] is None and "unavailable" in ev["reason"], ev
    print(f"  CLIP down -> {ev['status']} (no floor, shutter untouched)")


def test_empty_scores_are_unavailable() -> None:
    ev = sr.subject_evidence("m.jpg", classifier=lambda _p: {})
    assert ev["status"] == "unavailable" and ev["floor_s"] is None, ev


# --- focal length: a longer lens needs a faster floor ----------------------
def test_longer_lens_tightens_the_floor() -> None:
    # Person floor is 1/125; at 300mm the reciprocal rule tightens it to 1/300.
    short, _ = sr.subject_floor("person", focal_mm=50)
    long, reason = sr.subject_floor("person", focal_mm=300)
    assert short == 1 / 125, short
    assert long == 1 / 300 and long < short, (long, reason)
    assert "300mm" in reason, reason
    # A bird's 1/2000 is already faster than 1/400, so a 400mm lens does not loosen it.
    bird_floor, _ = sr.subject_floor("bird", focal_mm=400)
    assert bird_floor == 1 / 2000, bird_floor


def test_no_floor_class_ignores_focal_length() -> None:
    floor, _ = sr.subject_floor("sky_or_landscape", focal_mm=600)
    assert floor is None, floor


# --- the floor survives the exposure solver (ISO rises, not the floor) -----
def test_floor_survives_the_exposure_solver() -> None:
    floor, _ = sr.subject_floor("bird", focal_mm=400)  # 1/2000
    # A dim scene (EV100 = 8): at base ISO the correct shutter is far slower than 1/2000, so the
    # solver must raise ISO to hold the floor rather than drop below it.
    sol = physics.solve_exposure(8.0, baseline_aperture=2.8, shutter_floor_s=floor,
                                 lens_min_aperture=2.8, lens_max_aperture=22, max_iso=12800)
    assert not sol.below_shutter_floor, sol
    assert sol.iso > 100, "ISO should rise to hold the floor"
    assert abs(sol.shutter_s - floor) <= floor * 0.05, (sol.shutter_s, floor)
    print(f"  bird floor 1/2000 held -> ISO {sol.iso}, f/{sol.aperture}, 1/{1/sol.shutter_s:.0f}s")


def test_floor_breaks_only_after_iso_is_exhausted() -> None:
    floor, _ = sr.subject_floor("bird", focal_mm=400)  # 1/2000
    # Near-dark: even the ISO ceiling and a wide-open lens cannot hold 1/2000 -> floor gives way,
    # and the solver flags it (whichever gave way is recorded).
    sol = physics.solve_exposure(2.0, baseline_aperture=2.8, shutter_floor_s=floor,
                                 lens_min_aperture=2.8, lens_max_aperture=22, max_iso=6400)
    assert sol.hit_iso_ceiling and sol.below_shutter_floor, sol


# --- the user's stated intent outranks the classifier ---------------------
def test_blur_intent_suppresses_the_subject_floor() -> None:
    # "light trails of traffic" -> vehicle, but the user asked for blur: no floor.
    ev = sr.subject_evidence("m.jpg", focal_mm=50, motion_intent="blur",
                             classifier=_stub("vehicle"))
    assert ev["status"] == "suppressed" and ev["floor_s"] is None, ev
    assert ev["class"] == "vehicle", ev  # still classified + recorded
    assert "user_requested_motion_blur" in ev["reason"], ev
    print(f"  blur intent + vehicle -> no floor ({ev['reason']})")


def test_freeze_intent_takes_the_faster_of_the_two_floors() -> None:
    # Freeze intent sets a 1/500 floor in the engine; a bird adds 1/2000. The engine folds them
    # with min(), so the faster (1/2000) binds.
    ev = sr.subject_evidence("m.jpg", focal_mm=50, motion_intent="freeze",
                             classifier=_stub("bird"))
    assert ev["status"] == "applied" and ev["floor_s"] == 1 / 2000, ev
    freeze_floor = 1 / 500
    bound = min(freeze_floor, ev["floor_s"])  # the engine's fold
    assert bound == 1 / 2000, bound


def test_unspecified_intent_applies_the_subject_floor() -> None:
    ev = sr.subject_evidence("m.jpg", focal_mm=50, motion_intent=None,
                             classifier=_stub("vehicle"))
    assert ev["status"] == "applied" and ev["floor_s"] == 1 / 1000, ev


# --- evidence contract shape ----------------------------------------------
def test_evidence_contract_keys() -> None:
    ev = sr.subject_evidence("m.jpg", focal_mm=50, classifier=_stub("bird"))
    # The original five keys stay; the tile pass only adds keys.
    assert {"status", "class", "confidence", "floor_s", "reason"} <= set(ev), ev
    assert set(ev) - {"status", "class", "confidence", "floor_s", "reason"} == {
        "decided_by", "whole_frame", "tiles"}, ev
    assert isinstance(ev["confidence"], float)
    assert ev["decided_by"] == "whole_frame"


# --- the tile pass: a person in part of a wide frame -----------------------
def test_tile_boxes_are_fixed_overlapping_squares_inside_the_frame() -> None:
    boxes = sr.tile_boxes(6000, 4000)
    assert boxes == [(0, 0, 2400, 2400), (1800, 0, 4200, 2400), (3600, 0, 6000, 2400),
                     (0, 1600, 2400, 4000), (1800, 1600, 4200, 4000), (3600, 1600, 6000, 4000),
                     (1800, 800, 4200, 3200)]
    portrait = sr.tile_boxes(4000, 6000)
    assert len(portrait) == 7 and portrait[:2] == [(0, 0, 2400, 2400), (1600, 0, 4000, 2400)]
    assert portrait[5] == (1600, 3600, 4000, 6000)
    for width, height in ((6000, 4000), (4000, 6000), (640, 427), (3, 2)):
        for left, top, right, bottom in sr.tile_boxes(width, height):
            assert 0 <= left < right <= width and 0 <= top < bottom <= height
            assert right - left == bottom - top
    assert sr.tile_boxes(6000, 4000) == sr.tile_boxes(6000, 4000)
    with pytest.raises(ValueError):
        sr.tile_boxes(0, 10)


def test_person_tile_needs_both_the_threshold_and_the_margin() -> None:
    assert sr.person_tile(_tiles()) is None
    index, person, margin = sr.person_tile(_tiles(person_tile=3, person=0.55, person_in_motion=0.30))
    assert index == 3 and person == pytest.approx(0.85)  # a walker splits between the two person prompts
    assert margin == pytest.approx(0.85 - 0.15)
    below = sr.TILE_PERSON_THRESHOLD - 0.01
    assert sr.person_tile(_tiles(person_tile=0, person=below)) is None
    # Over the threshold but the poster/painting distractor is too close: no floor.
    assert sr.person_tile(_tiles(person_tile=0, person=0.62, artwork=0.36)) is None
    assert sr.person_tile(_tiles(person_tile=0, person=0.62, artwork=0.33)) is None   # margin 0.29
    assert sr.person_tile(_tiles(person_tile=0, person=0.62, artwork=0.30)) is not None  # margin 0.32


def test_a_person_in_one_tile_sets_the_person_floor_the_whole_frame_missed() -> None:
    # Live 2026-09-29 (A7 III, 24 mm): a friend walking across a room; the whole frame read
    # static_object 0.51, so no floor and a 1/25 s shot. The tile that holds him sets 1/125 s.
    calls = []
    ev = sr.subject_evidence("m.jpg", focal_mm=24, classifier=_stub("static_object", 0.51),
                             tile_classifier=lambda path: calls.append(path) or _tiles(person_tile=3))
    assert calls == ["m.jpg"]
    assert (ev["status"], ev["class"], ev["floor_s"]) == ("applied", "person", 1 / 125), ev
    assert ev["decided_by"] == "tile_3"
    assert ev["whole_frame"] == {"class": "static_object", "confidence": 0.51, "status": "no_floor"}
    assert ev["tiles"]["status"] == "ran" and ev["tiles"]["person_tile"] == 3
    assert [t["tile"] for t in ev["tiles"]["scores"]] == list(range(7))
    assert ev["tiles"]["scores"][3]["person"] == 0.9 and ev["tiles"]["scores"][3]["top"] == "person"
    assert "tile 3" in ev["reason"] and "static_object" in ev["reason"]


def test_the_same_room_without_a_person_stays_no_floor() -> None:
    ev = sr.subject_evidence("m.jpg", focal_mm=24, classifier=_stub("static_object", 0.99),
                             tile_classifier=lambda _p: _tiles())
    assert (ev["status"], ev["floor_s"], ev["decided_by"]) == ("no_floor", None, "whole_frame"), ev
    assert ev["tiles"]["status"] == "ran" and "person_tile" not in ev["tiles"]


def test_a_poster_face_that_reads_as_artwork_sets_no_floor() -> None:
    ev = sr.subject_evidence("m.jpg", focal_mm=24, classifier=_stub("static_object", 0.99),
                             tile_classifier=lambda _p: _tiles(person_tile=2, person=0.45, artwork=0.50))
    assert ev["status"] == "no_floor" and ev["floor_s"] is None, ev
    assert ev["tiles"]["scores"][2]["top"] == "artwork"


def test_a_tile_person_floor_tightens_with_focal_length() -> None:
    ev = sr.subject_evidence("m.jpg", focal_mm=300, classifier=_stub("sky_or_landscape"),
                             tile_classifier=lambda _p: _tiles(person_tile=0))
    assert ev["status"] == "applied" and ev["floor_s"] == 1 / 300, ev


def test_low_confidence_whole_frame_still_runs_the_tile_pass() -> None:
    ev = sr.subject_evidence("m.jpg", focal_mm=24, classifier=_stub("bird", sr.CONFIDENCE_THRESHOLD - 0.05),
                             tile_classifier=lambda _p: _tiles(person_tile=6))
    assert (ev["status"], ev["class"], ev["decided_by"]) == ("applied", "person", "tile_6"), ev
    assert ev["whole_frame"]["status"] == "low_confidence"


def test_a_failing_tile_pass_keeps_the_whole_frame_result() -> None:
    def broken(_path):
        raise RuntimeError("encoder gone")
    ev = sr.subject_evidence("m.jpg", focal_mm=24, classifier=_stub("static_object"),
                             tile_classifier=broken)
    assert (ev["status"], ev["decided_by"]) == ("no_floor", "whole_frame"), ev
    assert ev["tiles"]["status"] == "unavailable" and "encoder gone" in ev["tiles"]["reason"]
    malformed = sr.subject_evidence("m.jpg", focal_mm=24, classifier=_stub("static_object"),
                                    tile_classifier=lambda _p: [{"box": [0, 0, 1, 1], "probabilities": {}}])
    assert malformed["status"] == "no_floor" and malformed["tiles"]["status"] == "unavailable"


def test_the_production_tile_classifier_crops_scores_and_maps_boxes(monkeypatch, tmp_path) -> None:
    """`_clip_classify_tiles` itself, with the encoder replaced by one-hot features (CLIP never loads)."""
    import numpy as np
    from PIL import Image

    import embed_index
    import perception_client
    monkeypatch.setattr(sr, "_clip_classify_tiles", ORIGINAL_CLASSIFY_TILES)
    classes = list(sr.SUBJECT_PROMPTS) + list(sr.TILE_DISTRACTOR_PROMPTS)
    text = np.eye(len(classes), dtype="float32")
    crops = []

    def embed_batch(_model, _preprocess, _device, images):
        crops.extend(images)
        rows = [text[classes.index("person" if index == 3 else "static_object")] for index in range(len(images))]
        return np.stack(rows)
    monkeypatch.setattr(perception_client, "remote", lambda: False)
    monkeypatch.setattr(embed_index, "_get_model", lambda: ("model", "preprocess", "cpu"))
    monkeypatch.setattr(embed_index, "_embed_batch", embed_batch)
    monkeypatch.setattr(sr, "_tile_text_features", lambda _model, _device: (text, classes))
    frame = tmp_path / "meter.jpg"
    Image.new("RGB", (3000, 2000), (90, 90, 90)).save(frame)
    tiles = sr._clip_classify_tiles(str(frame))
    # 0.6 x 2000 = 1200 px tiles, reduced by 2 so each crop is 600 px (>= the 448 px working side).
    assert len(tiles) == len(crops) == 7 and all(crop.size == (600, 600) for crop in crops)
    assert [tile["box"] for tile in tiles] == [
        [box[0] / 3000, box[1] / 2000, box[2] / 3000, box[3] / 2000] for box in sr.tile_boxes(3000, 2000)]
    for tile in tiles:
        assert set(tile["probabilities"]) == set(classes)
        assert sum(tile["probabilities"].values()) == pytest.approx(1.0)
    assert sr.person_tile(tiles)[0] == 3
    ev = sr.subject_evidence(str(frame), focal_mm=24, classifier=_stub("static_object"))
    assert (ev["status"], ev["decided_by"], ev["floor_s"]) == ("applied", "tile_3", 1 / 125), ev


def test_the_tile_pass_is_skipped_when_it_cannot_change_the_floor() -> None:
    def must_not_run(_path):
        raise AssertionError("tile pass ran")
    bird = sr.subject_evidence("m.jpg", focal_mm=24, classifier=_stub("bird"), tile_classifier=must_not_run)
    assert bird["floor_s"] == 1 / 2000 and bird["tiles"]["status"] == "skipped"
    person = sr.subject_evidence("m.jpg", focal_mm=24, classifier=_stub("person"), tile_classifier=must_not_run)
    assert person["floor_s"] == 1 / 125 and person["tiles"]["status"] == "skipped"
    blur = sr.subject_evidence("m.jpg", focal_mm=24, motion_intent="blur",
                               classifier=_stub("static_object"), tile_classifier=must_not_run)
    assert blur["status"] == "suppressed" and blur["floor_s"] is None and blur["tiles"]["status"] == "skipped"
    unavailable = sr.subject_evidence("m.jpg", classifier=lambda _p: {}, tile_classifier=must_not_run)
    assert unavailable["status"] == "unavailable" and unavailable["tiles"]["status"] == "skipped"
    assert unavailable["decided_by"] is None and unavailable["whole_frame"] is None


def test_prompts_and_floors_cover_the_same_classes() -> None:
    assert set(sr.SUBJECT_PROMPTS) == set(sr.SUBJECT_SHUTTER_FLOORS)


if __name__ == "__main__":
    import pytest
    raise SystemExit(pytest.main([__file__, "-v"]))
