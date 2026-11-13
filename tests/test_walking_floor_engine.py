"""The product engine turns "my friend walking" and a person in part of the frame into shutter floors.

Live on 2026-09-29 (A7 III, 24 mm, daylight room) "a photo of my friend walking across the room"
shot 1/25 s at ISO 160; evidence.subject read static_object 0.51 (no floor) and the walker blurred.
Uses the camera-free `rig` (tests/test_final_wave_engine.py): a fake body, patched metering and no
image intelligence; CLIP is replaced by deterministic stubs, never loaded.
"""
from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "backend"))

import pytest

import brain
import camera_caps
import embed_index
import engine
import live_verify
import subject_recognition
from test_final_wave_engine import rig  # noqa: F401 — the camera-free engine fixture

WALKING = "a photo of my friend walking across the room"
LIVING_ROOM = "a natural photo of my living room"
REAL_RECOMMEND_LIVE = brain.recommend_live  # captured before the rig patches it


def _plan(text):
    """The deterministic intent plan (no Ollama), as the product's applied plan."""
    return engine.intent_brain.preview(text)


def _whole(top_class, confidence=0.99):
    others = [name for name in subject_recognition.SUBJECT_PROMPTS if name != top_class]
    probabilities = {name: (1 - confidence) / len(others) for name in others}
    probabilities[top_class] = confidence
    return lambda _path: probabilities


def _tiles(person_tile=None):
    tiles = []
    for index in range(7):
        if index == person_tile:
            probabilities = {"person": 0.9, "person_in_motion": 0.05, "static_object": 0.04, "artwork": 0.01}
        else:
            probabilities = {"static_object": 0.97, "person": 0.01, "artwork": 0.02}
        tiles.append({"box": [0.0, 0.0, 0.4, 0.6], "probabilities": probabilities})
    return lambda _path: tiles


def _clip(monkeypatch, whole, tiles):
    monkeypatch.setattr(subject_recognition, "_clip_classify", whole)
    monkeypatch.setattr(subject_recognition, "_clip_classify_tiles", tiles)


def test_a_walking_request_sends_a_1_250_floor_to_the_brain(rig, monkeypatch):
    _clip(monkeypatch, _whole("static_object", 0.51), _tiles())
    shot = rig.real.shoot(WALKING, applied_plan=_plan(WALKING))
    assert rig.a7iii.call_args.kwargs["subject_motion_floor_s"] <= 1 / 250
    assert shot.evidence["subject"]["status"] == "no_floor"


def test_a_walking_request_shoots_at_or_faster_than_1_250_when_light_allows(rig, monkeypatch):
    # The real A7 III brain on the rig's body (ISO 100-400, f/4-f/8) in a bright room: the floor
    # holds, so the photograph is 1/250 s or faster rather than the live 1/25 s.
    _clip(monkeypatch, _whole("static_object", 0.51), _tiles())

    def no_taste(*_args, **_kwargs):
        raise RuntimeError("taste retrieval is not part of this test")
    monkeypatch.setattr(embed_index, "recommend", no_taste)          # the brain's labelled fallback
    monkeypatch.setattr(embed_index, "_get_model", lambda: pytest.fail("CLIP must not load"))
    rig.a7iii.side_effect = REAL_RECOMMEND_LIVE
    with patch.object(live_verify, "meter_ev100", return_value=(12.0, 0.0)):
        shot = rig.real.shoot(WALKING, applied_plan=_plan(WALKING))
    fired = rig.camera.exposure_writes[-1]
    assert camera_caps._shutter_seconds(fired["shutter"]) <= 1 / 250, fired
    assert camera_caps._shutter_seconds(shot.settings["shutter"]) <= 1 / 250


def test_the_same_request_without_walking_words_keeps_no_floor_in_an_empty_room(rig, monkeypatch):
    _clip(monkeypatch, _whole("static_object", 0.99), _tiles())
    shot = rig.real.shoot(LIVING_ROOM, applied_plan=_plan(LIVING_ROOM))
    assert rig.a7iii.call_args.kwargs["subject_motion_floor_s"] is None
    subject = shot.evidence["subject"]
    assert (subject["status"], subject["floor_s"], subject["decided_by"]) == ("no_floor", None, "whole_frame")
    assert subject["tiles"]["status"] == "ran" and len(subject["tiles"]["scores"]) == 7


def test_a_person_found_in_one_tile_sets_the_person_floor(rig, monkeypatch):
    _clip(monkeypatch, _whole("static_object", 0.51), _tiles(person_tile=0))
    shot = rig.real.shoot(LIVING_ROOM, applied_plan=_plan(LIVING_ROOM))
    assert rig.a7iii.call_args.kwargs["subject_motion_floor_s"] == 1 / 125
    subject = shot.evidence["subject"]
    assert (subject["status"], subject["class"], subject["decided_by"]) == ("applied", "person", "tile_0")
    assert subject["whole_frame"]["class"] == "static_object"


def test_walking_words_and_a_person_tile_bind_the_faster_floor(rig, monkeypatch):
    _clip(monkeypatch, _whole("static_object", 0.51), _tiles(person_tile=3))
    shot = rig.real.shoot(WALKING, applied_plan=_plan(WALKING))
    assert rig.a7iii.call_args.kwargs["subject_motion_floor_s"] == 1 / 250
    assert shot.evidence["subject"]["floor_s"] == 1 / 125


def test_a_blur_request_suppresses_the_tile_person_floor(rig, monkeypatch):
    def must_not_run(_path):
        raise AssertionError("tile pass ran for a blur request")
    _clip(monkeypatch, _whole("static_object", 0.51), must_not_run)
    shot = rig.real.shoot("motion blur of people walking", applied_plan=_plan("motion blur of people walking"))
    assert rig.a7iii.call_args.kwargs["subject_motion_floor_s"] is None
    assert shot.evidence["subject"]["status"] == "suppressed"
