# Lessons — mistakes we made, and the guards that stop us repeating them

A running record so the same bug never lands twice. Every entry: what went wrong, the fix, and
the **guard** (a test or rule) that now catches it. **Run `bash backend/run_tests.sh` before
committing** — it runs every self-test + the regression suite.

## Correctness bugs (caught by Codex review, now regression-tested)
- **Metering underread blown scenes** — mean luminance on a gamma-encoded JPEG caps a fully-blown
  frame at +1.19 EV no matter how overexposed. → linear-light median + a clipping flag (EV is a
  *floor* when clipping). **Guard:** `test_regression.py::test_metering_handles_clipping`.
- **Inverted parameter (`stability`)** — steadier gave a *faster* shutter floor. →
  `stability / (focal * crop)`. **Guard:** assertion in `physics.py` self-test.
- **Flag from a non-final value** — `diffraction_limited` was set before the dark path could
  reopen the aperture, and rounding then dropped it under the cap. → compute it from the final,
  pre-rounding aperture. **Guard:** the capped/dim cases in `physics.py` self-test.
- **Untameable bright scene was a note, not a flag.** → an `overexposed` flag + ND-filter note.
  **Guard:** assertion in `physics.py` self-test.

## Integration / architecture mistakes
- **Brain emitted un-settable values** (ISO 297, f/21.38) — it solved in continuous space and
  never snapped to the camera's real ladders. → `camera_caps.py` snaps to live-read A7 II ladders.
  **Guard:** `camera_caps.py` self-test (ISO 297→320, f/21.38→f/22, 1/29→1/30).
- **The validated brain wasn't in the production path** — `live_verify` used it; `/shoot`
  (`engine.py`) used a dumb mean-target loop. → `RealEngine._brain_shoot`, with the auto-expose
  loop kept as a never-lose-the-shot fallback.
- **T-stop vs f-stop** — exposure used the f-number for light, but light is governed by the T-stop.
  → solve for a target dimmed by lens transmission; f-stop still drives DoF/diffraction.

## Hardware / environment
- **macOS `ptpcamerad` steals the USB** → intermittent PTP `-52`. SIP blocks disabling it, so kill
  it right before each capture (`killall -9 ptpcamerad`; it respawns harmlessly, an open PTP
  session survives the kill). **Guard:** `_free_usb()` in the MCP capture path *and* `engine.py`.
  Do **not** also kill `mscamerad`/`launchctl bootout` — that broke the live connection.
- **Wikimedia rate-limits concurrent thumbnail fetches** (HTTP 429). → serial fetches at the
  *cached* 320px size, respecting `Retry-After`. Don't hammer it; concurrency made it worse.

## Process rules (so the above don't recur)
1. **`bash backend/run_tests.sh` before every commit.** It runs every self-test + the regressions.
2. **Codex-review correctness-critical math** before committing — it caught 4 real bugs here.
3. **Verify `main` has it.** We commit on a branch and `git push origin HEAD:main`; check
   `HEAD == origin/main` so nothing is stranded on a local branch.
4. **Snap to real device values** before handing settings to hardware — never trust continuous output.
5. **The meter is a floor when clipping** — trust the camera meter / RAW histogram on heavy clipping.
6. **DoF targets must scale with distance.** A fixed-meters depth-of-field target over-stops close
   subjects into diffraction — live verify on a 0.6 m subject demanded f/11; making the target a
   fraction of the subject distance landed it at f/5.6 (the sweet spot). Depth of field shrinks as
   you get closer, so the target has to as well.
7. **The A7 II cannot be focus-driven over USB — a firmware wall, not an IP choice.** The body
   doesn't expose the AF-area (`0xD2DC`) or manual-focus (`0xD2D1`) PTP properties; a raw-PTP probe
   confirmed they're absent from its property table (added only in A7 III+). Corrected an earlier
   wrong belief that the "red wall" was a deliberate IP choice. On this body, focus = compose the
   subject under the center AF zone + trigger AF + verify with the sharpness gate; true AI-focus-
   point selection needs an A7 III+ / the Sony Camera Remote SDK, or on-camera OpenMemories.
