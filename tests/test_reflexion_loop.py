"""Tests for the closed reflexion loop (backend/reflexion.py).

The loop must, on a mock camera + mock critique, prove four behaviors:
  (a) a 'good' first shot -> exactly ONE shot, loop exits immediately;
  (b) 'too dark' then corrected -> re-shoots, exits when the critique says good;
  (c) a never-satisfied critique -> stops at the cap (NO infinite loop);
  (d) vision unavailable / loop disabled -> a single shot, unchanged.

Pure unit tests: no real camera, no Ollama, no network. The camera is a fake ``shoot_fn(target)``
that records every target it was asked to shoot; the vision model is a fake ``critique_fn`` that
returns scripted critique dicts (the exact shape ``vision_critique.critique`` returns). Runs under
pytest OR standalone (``python3 tests/test_reflexion_loop.py``).
"""

from __future__ import annotations

import sys
from pathlib import Path

# Make backend/ and this tests/ dir importable whether run via pytest or as a script.
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "backend"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from reflexion import (
    DEFAULT_MAX_ATTEMPTS,
    TARGET_MAX,
    TARGET_MIN,
    corrected_target,
    critique_is_satisfied,
    reflexion_shoot,
)

# --- fakes ------------------------------------------------------------------------------------

class FakeCamera:
    """A mock engine: records every target it's told to shoot and returns a report-shaped dict
    matching auto_expose's output (``ok`` / ``final_image`` / ``final_settings``)."""

    def __init__(self):
        self.targets = []          # every target shoot_fn was called with, in order
        self.shots = 0

    def shoot(self, target):
        self.shots += 1
        self.targets.append(target)
        return {
            "ok": True,
            "final_image": f"/tmp/fake_shot_{self.shots}.jpg",
            "final_settings": {"iso": "400", "shutter": "1/60", "aperture": "f/2.8"},
            "final_mean": target,
            "note": "fake",
        }


def good_critique():
    return {"matches_intent": True, "exposure": "good", "brightness_delta": 0,
            "issues": [], "suggestion": "looks great"}


def dark_critique(delta=30):
    return {"matches_intent": False, "exposure": "too dark", "brightness_delta": delta,
            "issues": ["shadows crushed"], "suggestion": "brighten it up"}


def bright_critique(delta=-30):
    return {"matches_intent": False, "exposure": "too bright", "brightness_delta": delta,
            "issues": ["blown highlights"], "suggestion": "pull it down"}


def scripted_critique(*verdicts):
    """A critique_fn that returns each verdict in turn (the Nth shot gets the Nth verdict).
    Past the end, repeats the last verdict — handy for 'never satisfied'."""
    box = {"i": 0}

    def critique_fn(report):
        i = min(box["i"], len(verdicts) - 1)
        box["i"] += 1
        return verdicts[i]

    return critique_fn


# --- (a) good first shot -> exactly one shot --------------------------------------------------

def test_good_first_shot_takes_exactly_one_shot():
    """A 'good' critique on the first frame must exit the loop immediately: one shot only."""
    cam = FakeCamera()
    result = reflexion_shoot(cam.shoot, scripted_critique(good_critique()), base_target=112.0)

    assert cam.shots == 1, f"expected exactly 1 shot, got {cam.shots}"
    assert result.attempts == 1
    assert result.satisfied is True
    assert result.stopped_reason == "satisfied"
    assert cam.targets == [112.0], "the one shot must aim at the base target, unmodified"


# --- (b) too dark then corrected -> re-shoots, exits on good -----------------------------------

def test_too_dark_then_corrected_reshoots_and_exits():
    """First frame is too dark; after the corrective re-shoot the critique is good -> 2 shots,
    and the second shot must aim HIGHER (a dark frame raises the brightness target)."""
    cam = FakeCamera()
    critique_fn = scripted_critique(dark_critique(delta=30), good_critique())
    result = reflexion_shoot(cam.shoot, critique_fn, base_target=100.0)

    assert cam.shots == 2, f"expected a re-shoot (2 shots), got {cam.shots}"
    assert result.attempts == 2
    assert result.satisfied is True
    assert result.stopped_reason == "satisfied"
    # First shot at the base target, second raised by the +30 brightness_delta.
    assert cam.targets[0] == 100.0
    assert cam.targets[1] == 130.0, f"dark frame should raise target to 130, got {cam.targets[1]}"


def test_too_bright_lowers_target():
    """Symmetric check: a 'too bright' frame must LOWER the brightness target on the re-shoot."""
    cam = FakeCamera()
    critique_fn = scripted_critique(bright_critique(delta=-30), good_critique())
    result = reflexion_shoot(cam.shoot, critique_fn, base_target=140.0)

    assert cam.shots == 2
    assert cam.targets[0] == 140.0
    assert cam.targets[1] == 110.0, f"bright frame should lower target to 110, got {cam.targets[1]}"
    assert result.satisfied is True


# --- (c) never satisfied -> stops at the cap (no infinite loop) --------------------------------

def test_never_satisfied_stops_at_cap():
    """A critique that is ALWAYS 'too dark' must NOT loop forever — it stops at the max-attempts
    cap (default 3) and hands back the last (most-corrected) frame."""
    cam = FakeCamera()
    # Always too dark, but with a varying delta so the stall-guard's repeat detector can't fire
    # first — this isolates the hard max_attempts cap as the thing that stops the loop.
    critique_fn = scripted_critique(
        dark_critique(delta=20), dark_critique(delta=18), dark_critique(delta=16),
        dark_critique(delta=14), dark_critique(delta=12),
    )
    result = reflexion_shoot(cam.shoot, critique_fn, base_target=100.0)

    assert cam.shots == DEFAULT_MAX_ATTEMPTS, f"must cap at {DEFAULT_MAX_ATTEMPTS}, got {cam.shots}"
    assert result.attempts == DEFAULT_MAX_ATTEMPTS
    assert result.satisfied is False
    assert result.stopped_reason == "max_attempts"


def test_never_satisfied_respects_custom_cap():
    """The cap is configurable; with max_attempts=5 a never-good critique takes exactly 5 shots."""
    cam = FakeCamera()
    critique_fn = scripted_critique(*[dark_critique(delta=20 - i) for i in range(10)])
    result = reflexion_shoot(cam.shoot, critique_fn, base_target=100.0, max_attempts=5)

    assert cam.shots == 5
    assert result.stopped_reason == "max_attempts"
    assert result.satisfied is False


def test_oscillating_critique_stalls_not_caps():
    """A correction that oscillates between two targets must trip the STALL-GUARD before the cap,
    proving the guard caps re-shoots on top of max_attempts. With a high cap, the guard is the only
    thing that can stop it — and it must."""
    cam = FakeCamera()
    # Flip-flop: dark -> raise, bright -> lower, forever, between the same two targets.
    critique_fn = scripted_critique(
        dark_critique(delta=20), bright_critique(delta=-20),
        dark_critique(delta=20), bright_critique(delta=-20),
        dark_critique(delta=20), bright_critique(delta=-20),
        dark_critique(delta=20), bright_critique(delta=-20),
    )
    result = reflexion_shoot(cam.shoot, critique_fn, base_target=100.0, max_attempts=50)

    assert result.stopped_reason == "stalled", "oscillating correction must trip the stall-guard"
    assert cam.shots < 50, "the guard must bail early, not run to the cap"


# --- (d) vision unavailable / disabled -> single shot, unchanged -------------------------------

def test_vision_unavailable_first_shot_single_shot():
    """If the critique THROWS on the first frame (vision unreachable — its best-effort failure
    mode), the loop must degrade to exactly one shot, identical to today's behavior."""
    cam = FakeCamera()

    def critique_fn(report):
        raise RuntimeError("Ollama unreachable")

    result = reflexion_shoot(cam.shoot, critique_fn, base_target=112.0)

    assert cam.shots == 1, "vision down -> single shot, no re-shoots"
    assert result.attempts == 1
    assert result.satisfied is False
    assert result.stopped_reason == "no_critique"
    assert result.final_report is not None, "must still deliver the one frame it took"
    assert cam.targets == [112.0]


def test_vision_returns_none_first_shot_single_shot():
    """A critique_fn that returns None (no verdict) on the first frame also degrades to one shot."""
    cam = FakeCamera()
    result = reflexion_shoot(cam.shoot, lambda report: None, base_target=112.0)

    assert cam.shots == 1
    assert result.stopped_reason == "no_critique"


# --- helpers: target translation + satisfaction, in isolation ---------------------------------

def test_corrected_target_uses_brightness_delta():
    assert corrected_target(100.0, dark_critique(delta=25)) == 125.0
    assert corrected_target(100.0, bright_critique(delta=-25)) == 75.0
    assert corrected_target(100.0, good_critique()) == 100.0


def test_corrected_target_falls_back_to_exposure_sign_when_no_delta():
    """No numeric brightness_delta -> use the exposure verdict's sign with the default nudge."""
    dark_no_delta = {"exposure": "too dark", "issues": ["dim"], "suggestion": "brighter"}
    bright_no_delta = {"exposure": "too bright", "issues": ["hot"], "suggestion": "darker"}
    assert corrected_target(100.0, dark_no_delta) > 100.0
    assert corrected_target(100.0, bright_no_delta) < 100.0


def test_corrected_target_clamps_to_brain_range():
    """A correction can never push the target outside the intent brain's own [60, 180] range."""
    assert corrected_target(170.0, dark_critique(delta=60)) == TARGET_MAX
    assert corrected_target(70.0, bright_critique(delta=-60)) == TARGET_MIN


def test_critique_is_satisfied():
    assert critique_is_satisfied(good_critique()) is True
    assert critique_is_satisfied(dark_critique()) is False
    # 'good' exposure but lingering issues -> NOT satisfied (issues must clear too).
    assert critique_is_satisfied(
        {"exposure": "good", "issues": ["crooked horizon"]}
    ) is False
    # No critique at all -> nothing to act on -> treated as satisfied (loop won't re-shoot on None).
    assert critique_is_satisfied(None) is True


if __name__ == "__main__":
    from _runner import run_module_tests

    raise SystemExit(run_module_tests(__name__))
