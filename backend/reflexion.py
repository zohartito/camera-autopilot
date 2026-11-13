"""Closed reflexion loop — act on the vision critique, don't just print it.

What this is
------------
``shoot.py`` today is an OPEN loop: it shoots, asks ``vision_critique.critique`` whether the
frame is good, PRINTS the verdict, and stops. This module closes that loop. When the critique
says the shot is OFF, we translate the critique into a corrected exposure **target** and re-shoot,
repeating until the critique is satisfied, a max-attempts cap is hit, or the stall-guard says the
loop is going nowhere.

The determinism boundary is sacred
----------------------------------
The vision model NEVER invents raw camera settings. It only ever moves a single number: the
mean-brightness **target** handed to the existing exposure solver (``auto_expose`` /
``physics.solve_exposure``). "too dark" raises the target, "too bright" lowers it; the magnitude
comes from the critique's ``brightness_delta`` (or a conservative default keyed off ``exposure``).
The camera's actual ISO / aperture / shutter are still computed by the deterministic physics layer
inside each shot — exactly as before.

Conservative by construction
-----------------------------
* **Opt-in only.** Nothing here runs unless the caller routes a shot through ``reflexion_shoot``.
  ``shoot.py``'s default path (single shot) is untouched and byte-identical.
* **Re-uses the existing stall-guard.** ``ExposureStallGuard`` caps oscillation / no-improvement
  across re-shoots, on top of the hard ``max_attempts`` cap. The loop can only ever stop *earlier*.
* **Hardware-free.** This module takes two callables (shoot + critique) and a base target. It never
  imports a camera or a network library, so it drives the real engine, the mock engine, or a fake
  in a test through the same code path.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from exposure_guard import ExposureStallGuard

# Match the intent brain's target range (intent_brain._coerce clamps to 60..180). A corrected
# target is never pushed outside the range the brain itself would ever produce.
TARGET_MIN = 60.0
TARGET_MAX = 180.0

# Default attempts: the FIRST shot plus up to (DEFAULT_MAX_ATTEMPTS - 1) re-shoots. Kept small so a
# real camera never hunts: 3 means at most one initial + two corrective frames.
DEFAULT_MAX_ATTEMPTS = 3

# Fallback brightness shift (target units, 0-255 scale) when the critique flags a direction but
# gives no usable numeric ``brightness_delta``. Deliberately gentle — one conservative nudge.
DEFAULT_NUDGE = 25.0


def critique_is_satisfied(critique: dict | None) -> bool:
    """True when the critique says the shot is GOOD: exposure == 'good' and no issues flagged.

    A missing/empty critique is treated as 'not satisfied is unknowable' -> we do NOT re-shoot on
    nothing; the caller gates on critique availability separately. Here, ``None`` -> True (no
    critique means no actionable complaint, so the loop has nothing to act on)."""
    if not critique:
        return True
    if critique.get("exposure", "good") != "good":
        return False
    if critique.get("issues"):
        return False
    return True


def corrected_target(base_target: float, critique: dict | None) -> float:
    """Translate a critique into a corrected mean-brightness TARGET (never raw camera settings).

    Priority: use the critique's signed ``brightness_delta`` ("how far to shift the exposure target
    to fix it") directly. If that's missing/unusable, fall back to the ``exposure`` verdict's sign
    with a gentle default nudge. The result is clamped to the intent brain's own [60, 180] range so
    a correction can never drive the target somewhere the brain would never pick.

    Sign convention (matches vision_critique's prompt): a positive delta brightens, so it RAISES
    the target; 'too dark' -> raise, 'too bright' -> lower.
    """
    if not critique:
        return _clamp(base_target)

    delta = _coerce_delta(critique.get("brightness_delta"))
    if delta is None:
        exposure = critique.get("exposure", "good")
        if exposure == "too dark":
            delta = DEFAULT_NUDGE
        elif exposure == "too bright":
            delta = -DEFAULT_NUDGE
        else:
            delta = 0.0

    return _clamp(base_target + delta)


def _coerce_delta(raw) -> float | None:
    """Parse ``brightness_delta`` to a float; None if absent or non-numeric. 0 is a valid delta."""
    if raw is None:
        return None
    try:
        return float(raw)
    except (TypeError, ValueError):
        return None


def _clamp(target: float) -> float:
    return max(TARGET_MIN, min(TARGET_MAX, float(target)))


def _correction_reason(critique: dict | None) -> str:
    """Plain-language why for a correction, from the critique's verdict and delta."""
    if not critique:
        return "no critique"
    exposure = critique.get("exposure", "good")
    delta = _coerce_delta(critique.get("brightness_delta"))
    if delta is not None:
        return f"critique '{exposure}', brightness_delta {delta:+g}"
    return f"critique '{exposure}', default nudge"


@dataclass
class Attempt:
    """One shot + its critique inside the reflexion loop."""

    index: int               # 0-based attempt number (0 == the initial shot)
    target: float            # the mean-brightness target this shot aimed for
    report: object           # whatever shoot_fn returned for this shot (report dict / Shot / ...)
    critique: dict | None    # the critique of this shot's frame, or None if vision was unavailable
    satisfied: bool          # did the critique say this frame is good?
    # The correction the loop APPLIED after this shot to aim the NEXT shot: a dict of
    # {"delta": signed target shift, "reason": why, "next_target": the aimed-at target}. Set only on
    # an attempt that was actually followed by a re-shoot; None on the delivered/last attempt (and
    # whenever the loop stopped rather than re-shooting, e.g. satisfied / capped / stalled).
    correction: dict | None = None


@dataclass
class ReflexionResult:
    """Outcome of the closed reflexion loop."""

    satisfied: bool          # True if a frame's critique was satisfied (the happy exit)
    stopped_reason: str      # 'satisfied' | 'max_attempts' | 'stalled' | 'no_critique'
    attempts: int            # how many shots were actually taken (>= 1)
    final_report: object     # the report for the frame we're delivering (best/last)
    final_critique: dict | None  # the critique for the delivered frame
    final_target: float      # the target the delivered frame aimed for
    history: list = field(default_factory=list)  # list[Attempt], one per shot


def reflexion_shoot(
    shoot_fn,
    critique_fn,
    base_target: float,
    *,
    max_attempts: int = DEFAULT_MAX_ATTEMPTS,
    patience: int = None,
    repeat_limit: int = None,
):
    """Shoot, critique, and RE-SHOOT on a corrected target until the critique is satisfied.

    Parameters
    ----------
    shoot_fn : callable(target: float) -> report
        Takes ONE mean-brightness target and performs one full capture (the existing converge
        loop). Returns whatever the caller wants to deliver (a report dict, a ``Shot``, a path).
        This is the ONLY thing that touches the camera; everything else here is pure orchestration.
    critique_fn : callable(report) -> dict | None
        Looks at the just-captured frame and returns the vision critique dict, or ``None`` /
        raises if vision is unavailable. A raised exception is caught and treated as "no critique
        for this frame" — the loop degrades to a single shot, never crashing the autopilot.
    base_target : float
        The intent brain's original target for the first shot. Re-used unchanged for shot 0.
    max_attempts : int
        Hard cap on total shots (initial + re-shoots). Default 3. Must be >= 1.

    Returns
    -------
    ReflexionResult
        Carries the delivered frame's report + critique, why the loop stopped, and the full
        per-shot history. On a stall or the cap, it delivers the LAST shot taken (the loop already
        moved the target toward the fix; the last frame is the most-corrected one).

    Safety
    ------
    * The loop runs at most ``max_attempts`` times AND is independently capped by the stall-guard
      (oscillating / no-improvement targets bail early). It can never re-shoot forever.
    * If the very first critique is unavailable, the loop stops after one shot with
      ``stopped_reason='no_critique'`` — identical observable behavior to today's single shot.
    """
    if max_attempts < 1:
        raise ValueError("max_attempts must be >= 1")

    guard_kwargs = {}
    if patience is not None:
        guard_kwargs["patience"] = patience
    if repeat_limit is not None:
        guard_kwargs["repeat_limit"] = repeat_limit
    # The guard caps re-shoots on top of max_attempts: if the corrected target oscillates or stops
    # moving the frame closer, bail to what we have. min_improvement is in target-units.
    guard = ExposureStallGuard(**guard_kwargs)

    history: list[Attempt] = []
    target = _clamp(base_target)

    for index in range(max_attempts):
        report = shoot_fn(target)

        # Critique is best-effort: a throw (vision unreachable) degrades to "no critique", never a
        # crash — exactly the safe default the open loop already relies on.
        try:
            critique = critique_fn(report)
        except Exception:
            critique = None

        satisfied = critique_is_satisfied(critique) if critique is not None else False
        history.append(Attempt(index=index, target=target, report=report,
                               critique=critique, satisfied=satisfied))

        # No critique at all on the FIRST shot -> behave exactly like today: one shot, stop.
        if critique is None:
            return ReflexionResult(
                satisfied=False,
                stopped_reason="no_critique",
                attempts=index + 1,
                final_report=report,
                final_critique=None,
                final_target=target,
                history=history,
            )

        # The critique is happy -> deliver this frame, we're done.
        if satisfied:
            return ReflexionResult(
                satisfied=True,
                stopped_reason="satisfied",
                attempts=index + 1,
                final_report=report,
                final_critique=critique,
                final_target=target,
                history=history,
            )

        # Not satisfied. If this was the last allowed attempt, stop at the cap.
        if index + 1 >= max_attempts:
            return ReflexionResult(
                satisfied=False,
                stopped_reason="max_attempts",
                attempts=index + 1,
                final_report=report,
                final_critique=critique,
                final_target=target,
                history=history,
            )

        # Translate the critique into the next target, and ask the stall-guard whether moving there
        # is still making progress. The guard's "error" is the absolute brightness_delta the model
        # still wants (how far from good we are); the "settings" key is the rounded target, so a
        # target that oscillates or stops moving trips the guard.
        next_target = corrected_target(target, critique)
        residual = abs(_coerce_delta(critique.get("brightness_delta")) or DEFAULT_NUDGE)
        reason = guard.observe(residual, {"target": round(next_target, 1)})
        if reason:
            return ReflexionResult(
                satisfied=False,
                stopped_reason="stalled",
                attempts=index + 1,
                final_report=report,
                final_critique=critique,
                final_target=target,
                history=history,
            )

        # We're re-shooting: record the correction this attempt applied to reach the next target.
        history[-1].correction = {
            "delta": round(next_target - target, 2),
            "reason": _correction_reason(critique),
            "next_target": round(next_target, 2),
        }
        target = next_target

    # Unreachable in practice (the cap-check above returns first), but keep a defined exit.
    last = history[-1]
    return ReflexionResult(
        satisfied=last.satisfied,
        stopped_reason="max_attempts",
        attempts=len(history),
        final_report=last.report,
        final_critique=last.critique,
        final_target=last.target,
        history=history,
    )
