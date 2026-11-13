"""Outer-loop STALL-GUARD for the autopilot's capture/adjust exposure loop.

Why this exists
---------------
The real exposure loop (``sonyremote.autoexpose.auto_expose``, in the sibling
``sony-camera-mcp`` repo) hunts for a setting that lands the frame's mean brightness on the
target the intent brain picked. On most scenes it converges in a few captures. But on some
scenes it can't get there — the only ISO/aperture/shutter steps the body offers overshoot the
target, so the loop *oscillates* between two settings, or it gets *stuck* repeating one setting,
or the error simply *stops shrinking*. Left alone, that burns shutter actuations and battery
hunting forever for a target it will never hit.

This module is a small, dependency-free **stall detector** plus a thin **driver** that wraps any
step-by-step capture/adjust loop with it. When the loop stalls, the guard bails out and hands
back the BEST frame seen so far (the deterministic fallback: "we couldn't nail the exact target,
here's the closest we got"), instead of looping until a hard iteration cap.

Design notes
------------
* **Purely additive.** Nothing in the existing runtime imports this module. ``engine.py`` /
  ``shoot.py`` keep calling ``auto_expose`` exactly as before; the default runtime path is
  byte-for-byte unchanged. A caller opts in by routing its loop through ``guarded_auto_expose``.
* **Hardware-free and model-free.** The guard only sees numbers (a brightness error) and a
  hashable settings snapshot per iteration. It works against the real camera loop, a Wi-Fi loop,
  or a fake step-function in a test — it never imports a camera or a network library.
* **Conservative by construction.** The guard only ever *stops early*; it can never push the loop
  past the caller's own iteration cap, and it never fabricates a frame — on a stall it returns the
  best-error frame the loop already produced.
"""

from __future__ import annotations

from dataclasses import dataclass, field

# Defaults are deliberately patient: a healthy loop converges well inside these, and we only want
# to bail when the loop is genuinely going nowhere.
DEFAULT_PATIENCE = 3          # consecutive no-progress iterations tolerated before bailing
DEFAULT_MIN_IMPROVEMENT = 1.0 # a step must cut the brightness error by at least this to count as progress
DEFAULT_REPEAT_LIMIT = 2      # how many times the SAME settings may recur before we call it stuck


class StallReason:
    """Why the guard decided the loop was stalled (string constants, no enum dependency)."""

    NO_IMPROVEMENT = "no_improvement"   # error stopped shrinking for `patience` iterations
    REPEATED_SETTINGS = "repeated"      # the same settings recurred too many times (stuck/oscillating)
    NONE = ""                            # not stalled


@dataclass
class GuardState:
    """Running record of the loop's progress, owned by ExposureStallGuard."""

    best_error: float | None = None          # smallest |error| seen so far (for "best frame" handback)
    best_index: int = -1                     # iteration that produced best_error
    # The error at the last point we credited as MEANINGFUL progress. Patience is measured against
    # this baseline, NOT against best_error — so many small sub-threshold gains that ADD UP to a
    # meaningful improvement still reset patience (a slow-but-steady servo is not a stall).
    progress_baseline: float | None = None
    stale_steps: int = 0                     # consecutive iterations with no meaningful improvement
    seen_settings: dict = field(default_factory=dict)  # settings-key -> times seen
    errors: list = field(default_factory=list)         # full |error| history (for introspection/tests)
    settings_log: list = field(default_factory=list)   # full settings-key history


class ExposureStallGuard:
    """Tracks no-progress across iterations and decides when to bail to the deterministic fallback.

    Feed it one ``(error, settings)`` observation per capture via :meth:`observe`. It returns the
    reason the loop should stop early (``StallReason``), or ``StallReason.NONE`` to keep going.

    * ``error`` is the brightness error for the just-captured frame — typically
      ``target_mean - measured_mean`` (sign doesn't matter; the guard uses ``abs``).
    * ``settings`` is any hashable-able snapshot of the settings used for that capture
      (a dict, tuple, or string). Dicts are normalised to a stable key internally.

    A step counts as *progress* only if it cuts the best error so far by at least
    ``min_improvement``. ``patience`` consecutive non-progress steps -> stall. Independently, if
    one settings snapshot recurs more than ``repeat_limit`` times, the loop is stuck/oscillating
    and we stall immediately (this catches a two-state oscillation that never improves).
    """

    def __init__(
        self,
        patience: int = DEFAULT_PATIENCE,
        min_improvement: float = DEFAULT_MIN_IMPROVEMENT,
        repeat_limit: int = DEFAULT_REPEAT_LIMIT,
    ):
        if patience < 1:
            raise ValueError("patience must be >= 1")
        if min_improvement < 0:
            raise ValueError("min_improvement must be >= 0")
        if repeat_limit < 1:
            raise ValueError("repeat_limit must be >= 1")
        self.patience = patience
        self.min_improvement = min_improvement
        self.repeat_limit = repeat_limit
        self.state = GuardState()

    @staticmethod
    def _settings_key(settings) -> str:
        """Normalise a settings snapshot to a stable, hashable string key.

        Dicts are sorted by key so {'iso':'400','shutter':'1/60'} and the same dict in another
        order collapse to one key. Everything else is stringified.
        """
        if isinstance(settings, dict):
            return "|".join(f"{k}={settings[k]}" for k in sorted(settings))
        return str(settings)

    def observe(self, error: float, settings) -> str:
        """Record one capture's outcome and return a StallReason (NONE = keep looping).

        Call this once per capture, AFTER measuring the frame. The guard updates its best-so-far
        bookkeeping and the caller should stop looping as soon as a non-empty reason comes back.
        """
        magnitude = abs(float(error))
        key = self._settings_key(settings)
        index = len(self.state.errors)
        self.state.errors.append(magnitude)
        self.state.settings_log.append(key)

        # Track the closest frame regardless of progress, so the handback is always the best one.
        if self.state.best_error is None or magnitude < self.state.best_error:
            self.state.best_error = magnitude
            self.state.best_index = index

        # --- progress accounting -------------------------------------------------
        # Patience is measured against `progress_baseline` (the last MEANINGFUL-progress point),
        # not against the running best. This is the key: a slow servo that closes the gap by less
        # than `min_improvement` each step still resets patience once the ACCUMULATED gain since the
        # baseline crosses the threshold — so steady-but-slow convergence is not a false stall,
        # while a loop that is genuinely flat (or creeping by a rounding-error amount) still stalls.
        if self.state.progress_baseline is None:
            # First observation: establish the baseline, nothing to compare against yet.
            self.state.progress_baseline = magnitude
            self.state.stale_steps = 0
        elif magnitude <= self.state.progress_baseline - self.min_improvement:
            # Accumulated improvement since the baseline is meaningful -> credit progress, re-baseline.
            self.state.progress_baseline = magnitude
            self.state.stale_steps = 0
        else:
            # No meaningful ACCUMULATED progress since the baseline yet -> a stale step. The baseline
            # is deliberately LEFT WHERE IT IS (not pulled down to this step's value): that's what
            # lets many small sub-threshold gains accumulate against a fixed bar and eventually
            # cross `min_improvement`, resetting patience. A loop that never accumulates a full
            # `min_improvement` of gain (a true plateau, or rounding-error creep) keeps piling up
            # stale steps until it stalls.
            self.state.stale_steps += 1

        # --- repeated-settings (stuck / oscillating) -----------------------------
        self.state.seen_settings[key] = self.state.seen_settings.get(key, 0) + 1
        if self.state.seen_settings[key] > self.repeat_limit:
            return StallReason.REPEATED_SETTINGS

        # --- no-improvement stall ------------------------------------------------
        if self.state.stale_steps >= self.patience:
            return StallReason.NO_IMPROVEMENT

        return StallReason.NONE


@dataclass
class GuardedResult:
    """Outcome of a guarded exposure run."""

    ok: bool                 # True if the loop converged within tolerance; False if it stalled out
    stalled: bool            # True if the stall-guard bailed the loop early
    reason: str              # StallReason describing the bail (empty when ok / converged)
    iterations: int          # how many captures were taken
    best_index: int          # which capture had the smallest brightness error
    best_error: float | None # the smallest |error| reached
    frame: object            # the best frame the loop produced (whatever the step-fn returns), or None
    history: list            # list of (error, settings, frame) tuples, one per capture


def guarded_auto_expose(
    step,
    target_mean: float,
    *,
    tolerance: float = 6.0,
    max_iterations: int = 12,
    patience: int = DEFAULT_PATIENCE,
    min_improvement: float = DEFAULT_MIN_IMPROVEMENT,
    repeat_limit: int = DEFAULT_REPEAT_LIMIT,
) -> GuardedResult:
    """Drive a capture/adjust loop with the stall-guard, bailing to the best frame on no-progress.

    This is a thin, hardware-free driver. The caller supplies ``step``, a callable that performs
    ONE capture+adjust cycle and returns ``(measured_mean, settings, frame)``:

        * ``measured_mean`` — the mean brightness of the frame it just captured (float).
        * ``settings``      — the settings snapshot used (dict / tuple / str; used for repeat
                              detection).
        * ``frame``         — whatever the caller wants back for the chosen capture (a path, bytes,
                              a report dict, ...); the guard treats it as opaque.

    ``step`` is called with one argument, the iteration index (0-based), so it can decide how to
    nudge the exposure. The loop stops when:

        * the frame lands within ``tolerance`` of ``target_mean``  -> ``ok=True`` (converged), or
        * the stall-guard fires (no improvement for ``patience`` steps, or settings repeat past
          ``repeat_limit``)                                        -> ``ok=False, stalled=True``, or
        * ``max_iterations`` is reached                            -> ``ok=False`` (hard cap).

    On a stall (or the hard cap) the result still carries the BEST frame seen, so the caller can
    deliver "couldn't hit the exact target, here's the closest" rather than nothing — the
    deterministic fallback the spec asks for.

    The default runtime never calls this; it's opt-in. ``step`` is the only thing that touches a
    camera, so this driver is fully unit-testable with a fake step-function.
    """
    guard = ExposureStallGuard(
        patience=patience, min_improvement=min_improvement, repeat_limit=repeat_limit
    )
    history: list = []
    best_frame = None
    best_index = -1
    best_error: float | None = None

    for index in range(max_iterations):
        measured_mean, settings, frame = step(index)
        error = float(target_mean) - float(measured_mean)
        history.append((error, settings, frame))

        # Track the best frame independently of the guard so we always have one to hand back.
        if best_error is None or abs(error) < best_error:
            best_error = abs(error)
            best_index = index
            best_frame = frame

        # Converged: the frame is close enough to the target — done, the happy path.
        if abs(error) <= tolerance:
            return GuardedResult(
                ok=True,
                stalled=False,
                reason=StallReason.NONE,
                iterations=index + 1,
                best_index=index,
                best_error=abs(error),
                frame=frame,
                history=history,
            )

        # Not there yet — ask the guard whether the loop is going nowhere.
        reason = guard.observe(error, settings)
        if reason:
            return GuardedResult(
                ok=False,
                stalled=True,
                reason=reason,
                iterations=index + 1,
                best_index=best_index,
                best_error=best_error,
                frame=best_frame,
                history=history,
            )

    # Hit the hard iteration cap without converging or stalling — hand back the best frame.
    return GuardedResult(
        ok=False,
        stalled=False,
        reason=StallReason.NONE,
        iterations=max_iterations,
        best_index=best_index,
        best_error=best_error,
        frame=best_frame,
        history=history,
    )
