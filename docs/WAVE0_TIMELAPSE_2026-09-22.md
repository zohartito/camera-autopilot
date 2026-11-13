# Wave 0 lane 0D: timelapse engine (Holy Grail, SmartLapse), 2026-09-22

Camera-free lane from `docs/SEQUENTIAL_AGENT_STACK_2026-09-20.md`. No camera was opened and no
shutter fired. Everything below is **software evidence from a simulator**; none of it is a sunset.
Rows touched: A31 SmartLapse, A32 Auto with custom intervals, A33 Holy Grail. **No row is promoted**:
each still needs a live receipt (indoor lamp-dimmer ramp on a real body, then a real sunset in Wave 3).

## What was wrong (measured before any change)

`tools/simulate_holy_grail.py` drives the real `metered_timelapse_step` over a Sony third-stop ladder
under the 1 s USB transport cap, against a synthetic scene. On the code at `accefc29`:

| Simulated scene (300 frames) | Exposure reversed direction | Mean / p95 error vs target | EV span reached | Frames flagged `range_limited` |
|---|---|---|---|---|
| Smooth 10-stop sunset | 86 times | 1.38 / 3.86 EV | 5.97 (capped) | **0** |
| Smooth 5-stop sunset | 157 times | 0.11 / 0.26 EV | 5.06 | 0 |
| Flat scene, 0.05 EV noise | 61 times | 0.06 / 0.24 EV | 0.68 | 0 |

Three defects:
1. **Meter anchored on the wrong exposure.** Each frame was metered against the smooth *requested* EV,
   but the frame was taken at the *quantized actual* EV, so the controller chased its own rounding and
   hunted on a perfectly smooth dusk.
2. **Holy Grail capped at 6 EV** from the first frame, so a day-to-night run stopped at 1/8 s, ISO 100
   with the camera's range unused and ended 3.9 EV underexposed.
3. **The receipt hid it**: every one of those frames reported `range_limited: false`.

## What changed (branch `wave0/timelapse`)

- The meter is anchored on the metered frame's actual EV (`measured_ev`).
- Holy Grail exponentially smooths the metered scene level over `smoothing_frames` (default 8), with a
  1.5 EV per-reading surprise limit; other modes keep frame-by-frame tracking.
- Requests are clamped to `reachable_ev`, the EVs the configured ladders can actually produce inside
  `max_total_ev` (anti-windup). Any shortfall at a hard range or total-EV edge is `range_limited`;
  ordinary third-stop rounding is not.
- Holy Grail's total range defaults to 20 EV (the ladders bound it); only Holy Grail may exceed 6.
- A mid-run mode switch applies the destination mode's defaults; sessions stored before this change
  keep frame-by-frame tracking on unrelated edits.
- SmartLapse accepts up to the 240-frame sequence limit (10 s at 24 fps; was 100).
- A Holy Grail session may execute for 3 hours instead of 1 (owner decision, below).

## After (same simulator, final law)

| Simulated scene | Reversals | Mean / p95 error | Span | Rendered frame-to-frame p95 (existing deflicker) |
|---|---|---|---|---|
| Smooth 10-stop sunset, 300 frames | 0 | 0.17 / 0.42 EV | 10.0 | 0.045 EV |
| Smooth 5-stop sunset | 0 | 0.11 / 0.26 EV | 5.1 | 0.041 EV |
| 5-stop sunset, 0.05 EV noise | 73 (frame-by-frame: 178) | 0.12 / 0.27 EV | 5.1 | 0.044 EV |
| Sudden +2 / +5 EV light change | 0 overshoot; lands in 11 / 20 frames | — | — | — |
| One black frame (meter floor) or white frame | peak 0.30 EV, above 0.2 EV for 3 frames | — | — | — |

## Lag at real cadences (the open tuning question)

Level-only smoothing trails a steady dusk by roughly 4.5 capture intervals. It matters most at slow
cadences (simulated 10-stop dusk, error against target):

| Cadence and dusk | smoothing 8 (default) | smoothing 4 |
|---|---|---|
| 1 h at 30 s, steep (peak ~25 EV/h) | 0.37 mean / 0.90 p95 EV | 0.22 / 0.50 EV |
| 2 h at 30 s (peak ~12 EV/h, closer to a real sunset) | 0.20 / 0.49 EV | 0.13 / 0.31 EV |
| 1 h at 10 s | 0.15 / 0.36 EV | 0.11 / 0.25 EV |

Recommendation until a real sunset says otherwise: `timelapse_options: {"smoothing_frames": 4}` at
intervals of 20 s or more. The default stays 8 because only a real sunset can say which way to move it.

## Reviews

- **Round 1** (Codex gpt-5.6-sol ultra, on the first commit): fix-first, 9 confirmed findings; all
  addressed in the second commit (see its message).
- **Round 2** (Codex gpt-6-astra high, per the owner's model choice, on the fix and budget commits):
  confirmed that ranges, the total-EV ceiling, non-finite input and the budget runner were resolved,
  and found five follow-ups, all fixed in the fourth commit with tests: a black *first* frame seeded
  the level unclipped (+8.4 EV, 57 frames); session creation still refused Holy Grail plans over one
  hour (`app.py`); a Fixed frame mid-run left a stale anchor and estimate; an explicit
  `smoothing_frames` on a legacy session was overwritten; a carried range the next mode cannot hold
  failed validation. Documented rather than changed: the first frame (or a mid-run range edit) may
  jump into the configured range, and mode switches treat a value equal to the old mode's default as
  inherited (repeat it in the patch to keep it).

## Rejected along the way (logged so it is not retried)

- **Plain proportional ramp over 20 frames**: lagged a 10-stop sunset by 0.65 EV mean, 1.5 EV p95.
- **Alpha-beta trend filter** (first commit): best sunset accuracy (0.13 EV mean) but, as the Codex
  review confirmed, underdamped (beta was twice the critical value): it overshot a 5 EV step by ~1 EV,
  and one black frame at the 1e-6 meter floor started a 24-frame excursion peaking at 1.39 EV. Capping
  or damping the trend traded the overshoot straight back for lag.

## What this does not prove

- The simulator's plant inverts the controller exactly away from clipping. Real JPEG tone curves,
  spatial clipping, content changes, noise and white balance are absent. Its deflicker model takes the
  gamma of the mean, which is not the mean of gamma-encoded pixels.
- No real sunset, no real body, no lamp ramp. The indoor live receipt is the coordinator's (serialized
  camera work); the sunset is Wave 3.

## Owner decisions this lane surfaced (not changed silently)

1. **Session execution budget: decided 2026-09-22.** The owner asked to raise it for Holy Grail only.
   A Holy Grail timelapse now gets 3 hours (`HOLY_GRAIL_EXECUTION_BUDGET_S`); every other session
   keeps 1 hour. The rule lives in `product_sessions.execution_budget_s` and is re-read every frame, so
   a mid-run switch into or out of Holy Grail changes the budget. A session past one hour keeps
   shooting only while it is a Holy Grail (tested with a stored 3700 s elapsed, and with a mid-run
   switch to Auto). Session creation applies the same rule, and the API now accepts up to the
   240-frame sequence limit (each mode's runner still enforces its own). Was open: a **SmartLapse**
   plan was capped at a 3600 s real duration (`smartlapse_settings`), and elapsed time survived a
   restart only through SmartLapse status, so the budget was per run rather than cumulative.
   Closed 2026-09-27 (`fix/smartlapse-per-run-budget`): the budget is per session. Each run's time
   (from camera connection on) is recorded as `execution_budget` (`budget_s`, `applies`, `used_s`,
   `remaining_s`, `exhausted`) after each setup step (connection, wake, baseline, restoration, capability
   discovery), at start, before each frame's preparation, before every shutter, after each accepted frame's boundary
   work (preview, SmartLapse), every 30 s of a between-frame wait, and on every exit; a resumed or
   restarted run starts from the recorded `used_s`, and only a new session starts at zero. A hard kill
   loses at most the time since the last checkpoint (one setup step, one frame's preparation, one capture plus its
   boundary work, or 30 s of a wait). A SmartLapse duration may use the budget of its
   exposure mode (3 h Holy Grail, 1 h otherwise; the plan names it) and is re-checked against the
   controls the first frame uses. The budget is checked when a frame is prepared, so a frame
   prepared just before the limit can fire a moment after it (unchanged). `tests/test_execution_budget.py`.
2. **Deflicker was off by default** (now on for Holy Grail, below). Without it, third-stop setting changes show as ~0.25 to 0.33 EV raw
   steps; with it, the simulated output is ~0.045 EV. Recommend on-by-default for Holy Grail.
   Decided 2026-09-27 (owner, 0.2 sweep): on by default for Holy Grail. A Holy Grail session created
   without a `deflicker` option gets window 7 / max 1 stop (the settings measured above,
   `timelapse_controls.HOLY_GRAIL_DEFLICKER_DEFAULT`); `{"enabled": false}` keeps it off; Fixed and
   Auto are unchanged. The default applies at creation only, not on a later switch into Holy Grail.
3. **Preview cap vs SmartLapse.** The incremental preview (#46) still stops at 100 frames; SmartLapse now
   plans up to 240. Frames are preserved either way; only the live preview stops refreshing.

## How to reproduce

```
.venv/bin/python tools/simulate_holy_grail.py --scene sunset --frames 300 --darken-ev 10
.venv/bin/python tools/simulate_holy_grail.py --scene step --frames 160 --darken-ev 5 --rows
.venv/bin/python -m pytest -q tests/test_holy_grail_transitions.py
```
