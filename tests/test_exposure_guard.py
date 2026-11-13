"""Tests for the outer-loop stall-guard (backend/exposure_guard.py).

Two things must hold:
  1. The guard FIRES (bails to the deterministic fallback) on a stuck or oscillating exposure loop.
  2. The guard is SILENT on a loop that is genuinely converging toward the target.

These are pure unit tests: no camera, no Ollama, no network. The capture/adjust loop is modelled
by a fake ``step`` callable, so we control exactly what each "capture" measures and what settings
it used. Runs under pytest OR standalone (`python3 tests/test_exposure_guard.py`).
"""

from __future__ import annotations

import sys
from pathlib import Path

# Make backend/ and this tests/ dir importable whether run via pytest or as a script.
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "backend"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from exposure_guard import (
    ExposureStallGuard,
    StallReason,
    guarded_auto_expose,
)

# --- helpers ----------------------------------------------------------------------------------

def converging_step(start_mean, target, gain=0.6, settings_seq=None):
    """A fake capture loop that genuinely homes in on `target`: each step closes a fraction of the
    remaining gap, like a well-behaved exposure servo. Returns a step(index) callable."""
    state = {"mean": float(start_mean)}

    def step(index):
        # Move a fraction of the way to the target each capture.
        state["mean"] += (target - state["mean"]) * gain
        settings = settings_seq[index] if settings_seq else {"iso": str(100 + index * 50)}
        return state["mean"], settings, f"frame_{index}"

    return step


def stuck_step(measured_mean, settings):
    """A fake loop that always measures the same (wrong) brightness with the same settings —
    the body has nowhere left to go, so it repeats one config forever."""
    def step(index):
        return measured_mean, settings, f"frame_{index}"

    return step


def oscillating_step(mean_a, mean_b, settings_a, settings_b):
    """A fake loop that flip-flops between two settings/brightnesses, never landing on target."""
    def step(index):
        if index % 2 == 0:
            return mean_a, settings_a, f"frame_{index}_a"
        return mean_b, settings_b, f"frame_{index}_b"

    return step


# --- the guard fires on a stuck loop ----------------------------------------------------------

def test_guard_fires_on_stuck_settings():
    """Same settings + same wrong brightness every capture -> guard bails via REPEATED_SETTINGS,
    well before the hard iteration cap, and hands back a best frame."""
    target = 112.0
    step = stuck_step(measured_mean=60.0, settings={"iso": "800", "shutter": "1/60"})
    result = guarded_auto_expose(step, target_mean=target, tolerance=6.0, max_iterations=12)

    assert result.stalled is True, "stuck loop should trip the stall-guard"
    assert result.ok is False
    assert result.reason == StallReason.REPEATED_SETTINGS
    # repeat_limit defaults to 2, so the 3rd identical capture trips it: iterations < max.
    assert result.iterations < 12, "guard must bail early, not run to the cap"
    assert result.frame is not None, "must still return the best frame seen"
    assert abs(result.best_error - abs(target - 60.0)) < 1e-9


def test_guard_fires_on_no_improvement():
    """Error that never shrinks (but settings vary, so repeat-detection can't catch it) -> the
    no-improvement path must fire after `patience` stale steps."""
    target = 112.0

    def step(index):
        # Always 40 below target, but with a fresh setting each time so REPEATED can't trigger.
        return 72.0, {"iso": str(100 + index * 25)}, f"frame_{index}"

    result = guarded_auto_expose(
        step, target_mean=target, tolerance=6.0, max_iterations=20, patience=3
    )

    assert result.stalled is True
    assert result.reason == StallReason.NO_IMPROVEMENT
    # First observe latches best (stale=0), then 3 stale steps -> bail on the 4th capture.
    assert result.iterations == 4, f"expected bail on 4th capture, got {result.iterations}"


def test_guard_fires_on_oscillation():
    """Two-state oscillation that never converges -> guard bails (settings repeat past the limit)."""
    target = 112.0
    step = oscillating_step(
        mean_a=80.0, mean_b=150.0,
        settings_a={"iso": "800"}, settings_b={"iso": "200"},
    )
    result = guarded_auto_expose(step, target_mean=target, tolerance=6.0, max_iterations=30)

    assert result.stalled is True, "oscillating loop should trip the stall-guard"
    # A 2-state oscillation is caught by EITHER signal — whichever crosses its threshold first.
    # Here the no-improvement counter (patience=3) trips on the 4th capture, just before the same
    # settings recur a 3rd time. Both reasons are legitimate "this loop is going nowhere".
    assert result.reason in (StallReason.NO_IMPROVEMENT, StallReason.REPEATED_SETTINGS)
    assert result.iterations < 30, "guard must bail early, not run to the cap"
    # Best frame is the one closest to target (150 is 38 off, 80 is 32 off -> the '80' frame).
    assert result.best_error == abs(target - 80.0)


def test_oscillation_caught_by_repeat_when_patience_high():
    """With patience raised so the no-improvement path can't fire first, the SAME oscillation is
    still caught — this time explicitly via REPEATED_SETTINGS — proving repeat-detection works."""
    target = 112.0
    step = oscillating_step(
        mean_a=80.0, mean_b=150.0,
        settings_a={"iso": "800"}, settings_b={"iso": "200"},
    )
    result = guarded_auto_expose(
        step, target_mean=target, tolerance=6.0, max_iterations=30, patience=99
    )
    assert result.stalled is True
    assert result.reason == StallReason.REPEATED_SETTINGS
    assert result.iterations < 30


# --- the guard is silent on a converging loop -------------------------------------------------

def test_guard_silent_on_converging_loop():
    """A loop that homes in on the target must converge (ok=True) and NEVER stall."""
    target = 112.0
    step = converging_step(start_mean=40.0, target=target, gain=0.6)
    result = guarded_auto_expose(step, target_mean=target, tolerance=6.0, max_iterations=20)

    assert result.ok is True, "converging loop must reach the target"
    assert result.stalled is False, "converging loop must NOT trip the guard"
    assert result.reason == StallReason.NONE
    assert abs(result.best_error) <= 6.0


def test_guard_silent_on_fast_converge():
    """A loop that nails the target on the very first capture stops immediately, no stall."""
    target = 112.0

    def step(index):
        return 112.0, {"iso": "400"}, "perfect_frame"

    result = guarded_auto_expose(step, target_mean=target, tolerance=6.0, max_iterations=12)
    assert result.ok is True
    assert result.stalled is False
    assert result.iterations == 1
    assert result.frame == "perfect_frame"


def test_slow_but_steady_progress_not_stalled():
    """Each step improves by MORE than min_improvement, so even a slow march must NOT be flagged
    as stalled before it converges."""
    target = 130.0
    # Means: 60, 75, 90, 105, 120, 128, 131 -> each step strictly closes the gap by > 1.0.
    means = [60.0, 75.0, 90.0, 105.0, 120.0, 128.0, 131.0]

    def step(index):
        return means[index], {"iso": str(100 + index * 10)}, f"frame_{index}"

    result = guarded_auto_expose(
        step, target_mean=target, tolerance=6.0, max_iterations=len(means),
        patience=3, min_improvement=1.0,
    )
    assert result.ok is True, "steady progress should converge, not stall"
    assert result.stalled is False


# --- guard internals (ExposureStallGuard.observe) ---------------------------------------------

def test_observe_resets_patience_on_improvement():
    """A genuine improvement must reset the stale-step counter, so progress + a stale step + more
    progress never accumulates to a false stall."""
    guard = ExposureStallGuard(patience=2, min_improvement=1.0, repeat_limit=99)
    # error 40 (best), 39.5 (sub-threshold -> stale=1), 30 (improves -> stale reset to 0),
    # 29.8 (stale=1) -> still under patience=2, so no stall.
    assert guard.observe(40.0, {"iso": "100"}) == StallReason.NONE
    assert guard.observe(39.5, {"iso": "200"}) == StallReason.NONE  # stale 1
    assert guard.observe(30.0, {"iso": "300"}) == StallReason.NONE  # improvement -> reset
    assert guard.observe(29.8, {"iso": "400"}) == StallReason.NONE  # stale 1, < patience
    assert guard.state.stale_steps == 1


def test_observe_settings_key_normalises_dict_order():
    """The same settings in a different dict order must count as the SAME settings for repeat
    detection (otherwise an oscillation could hide behind key ordering)."""
    guard = ExposureStallGuard(repeat_limit=2)
    a = {"iso": "800", "shutter": "1/60"}
    b = {"shutter": "1/60", "iso": "800"}  # same settings, reordered
    assert guard.observe(50.0, a) == StallReason.NONE
    assert guard.observe(50.0, b) == StallReason.NONE
    # Third identical capture (regardless of key order) trips the repeat limit.
    assert guard.observe(50.0, a) == StallReason.REPEATED_SETTINGS


def test_subthreshold_creep_still_converges():
    """Patience is measured against the last MEANINGFUL-progress baseline, not the running best, so
    many small sub-`min_improvement` gains that ACCUMULATE to real progress must NOT be flagged as a
    stall. A slow-but-steady servo (0.4 brightness/step, below the 1.0 default threshold) still
    converges — this is the false-positive Codex caught, now fixed."""
    target = 112.0
    means = [90.0 + 0.4 * i for i in range(80)]  # +0.4 per step, below default min_improvement=1.0

    def step(index):
        return means[index], {"iso": str(100 + index)}, f"frame_{index}"

    result = guarded_auto_expose(step, target_mean=target, tolerance=6.0, max_iterations=80)
    assert result.stalled is False, "steady creep accumulates progress; must not stall"
    assert result.ok is True, "the creep reaches the target eventually"


def test_true_plateau_after_partial_progress_stalls():
    """The flip side: a loop that makes some progress then goes genuinely FLAT (gains stop
    accumulating toward `min_improvement`) must still stall. Errors 22,16,10 (good), then stuck near
    9.x forever -> the plateau accumulates stale steps against the fixed baseline and bails."""
    target = 112.0
    # mean climbs 90->96->102 (real progress), then parks at ~102.8 (gain < 1.0, never accumulates).
    means = [90.0, 96.0, 102.0] + [102.8 + 0.05 * i for i in range(20)]

    def step(index):
        return means[index], {"iso": str(100 + index)}, f"frame_{index}"

    result = guarded_auto_expose(
        step, target_mean=target, tolerance=2.0, max_iterations=len(means), patience=3
    )
    assert result.stalled is True, "a true plateau must stall even after early progress"
    assert result.reason == StallReason.NO_IMPROVEMENT


def test_guard_constructor_validates_args():
    for bad in ({"patience": 0}, {"min_improvement": -1.0}, {"repeat_limit": 0}):
        try:
            ExposureStallGuard(**bad)
        except ValueError:
            pass
        else:
            raise AssertionError(f"ExposureStallGuard({bad}) should have raised ValueError")


if __name__ == "__main__":
    from _runner import run_module_tests

    raise SystemExit(run_module_tests(__name__))
