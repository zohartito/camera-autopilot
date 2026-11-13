"""A person moving at walking pace asks for a 1/250 s shutter floor.

Live on 2026-09-29 (A7 III, 24 mm, daylight room) "a photo of my friend walking across the room"
shot 1/25 s at ISO 160 and the walker came out blurred: the intent words only reacted to
freeze/action/running/moving/sports/fast. Camera-free; the engine half is
tests/test_walking_floor_engine.py.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))

import intent
import intent_brain


@pytest.mark.parametrize("phrase", [
    "a photo of my friend walking across the room",
    "people walking",
    "People walking down the street",
    "my friend walks to the door",
    "she walked in",
    "a couple strolling along the beach",
    "my daughter dancing in the kitchen",
    "dance with me",
    "kids playing in the yard",
    "children are playing outside",
    "the kid plays with a ball",
    "walking",
    # A negated blur is about the blur, not the walking: the walker must still be sharp.
    "avoid motion blur of my friend walking across the room",
    "no blur while my friend walks across the room",
    "don't want it blurry, my friend is walking",
    "no flash while my friend is walking across the room",
    "my friend walks trails in the mountains",
    "a photo of my friend walking trails in the mountains",
    "a photo of people walking paths in the park",
    "capture her walking trails with her dog",
    "my child is playing in the garden",
    "the toddler was playing",
    "my boy is playing in the garden",
    "my girl is playing in the garden",
    "my friend walking. Floor to ceiling windows behind him",
    "remove the glare, people walking by the window",
])
def test_walking_pace_words_ask_for_the_walk_floor(phrase):
    assert intent.motion_from_intent(phrase) == "walk"
    assert intent.motion_intent_floor_s(intent.motion_from_intent(phrase)) == 1 / 250
    assert intent_brain.preview(phrase)["motion_intent"] == "walk"


@pytest.mark.parametrize("phrase", [
    "the walkway at dusk",
    "my walk-in closet",
    "a walk-through of the house",
    "sidewalk chalk art",
    "the boardwalk at sunset",
    "a cafe within walking distance",
    "grandpa's walking stick",
    "a photo of a red walking stick",
    "a photo of a beautiful dance floor",
    "new dance shoes",
    "an empty dance floor",
    "nobody walking in the frame",
    "the room with no one walking through",
    "don't walk sign",
    "people aren't walking",
    "my friend wasn't walking",
    "none of the people are walking",
    "no one in the blurry background is walking",
    "nobody's walking in the frame",
    "my friend didn't walk",
    "people haven't walked through yet",
    "she hasn\u2019t walked in",
    "nobody\u2019s dancing",
    "no one's walking by",
    "nobody in the blurry hallway walks",
    "no kids playing today",
    "the street without anyone walking",
    "a play of light on the wall",
    "the playground",
    "kids' toys on the floor",
    "a natural photo of my living room",
    None,
    "",
])
def test_unrelated_uses_set_no_walk_floor(phrase):
    motion = intent.motion_from_intent(phrase)
    assert motion != "walk", phrase
    assert intent.motion_intent_floor_s(motion) is None, (phrase, motion)


@pytest.mark.parametrize("phrase", ["my friend jogging in the park", "a jogger on the bridge",
                                    "kids running around", "freeze my friend walking fast"])
def test_faster_movement_keeps_the_faster_freeze_floor(phrase):
    assert intent.motion_from_intent(phrase) == "freeze"
    assert intent.motion_intent_floor_s("freeze") == 1 / 500


@pytest.mark.parametrize("phrase", [
    "motion blur of people walking",
    "a long exposure of people walking through the station",
    "light trails and people walking",
    "silky water with kids playing by the river",
    "slow shutter of my friend dancing",
])
def test_a_blur_request_still_wins_over_walking_words(phrase):
    assert intent.motion_from_intent(phrase) == "blur"
    assert intent.motion_intent_floor_s("blur") is None


def test_the_floor_table():
    assert intent.MOTION_INTENT_FLOORS_S == {"freeze": 1 / 500, "walk": 1 / 250}
    assert intent.motion_intent_floor_s("unspecified") is None
    assert intent.motion_intent_floor_s(None) is None
