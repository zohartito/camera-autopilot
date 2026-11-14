# MCP tool surface and intern-test audit

Combined S10-04 / S05-04 implementation. This document also serves as
`MCP_TOOL_AUDIT_2026-10.md`; there is no separate audit to keep in sync.
The audit below records the **pre-change** description first lines and parameters.

## Decision and compatibility

Default agent tools, in registration order: **shoot, sun_times, detect_camera,
get_camera_settings** (4). `CAMERA_MCP_EXPERT=1` adds **set_exposure, set_control,
capture_photo, auto_expose, get_liveview, record** (10 total). Other flag values do
not enable expert tools. Restart the server after changing the flag.

`record` belongs in expert mode: physical movie-mode setup may be needed; native
commands cannot establish clip existence, playback or audio, and uncertain stop
must retain quarantine. Exposing it beside read-only planning would imply too much.

Before: detect_camera, get_camera_settings, list_setting_options, set_exposure,
set_control, capture_photo, auto_expose, get_liveview, record_video,
record_native_video, sun_times, shoot (12 tools, no mode split).

No README-documented tool was renamed or removed. `set_exposure`, `capture_photo`
and `auto_expose` remain registered in expert mode. `set_control` and `get_liveview`
are also hidden by default. Two merges remove three unregistered wrappers:

- `list_setting_options(setting)` → `get_camera_settings(setting=...)`.
- `record_video(seconds, serial)` / `record_native_video(seconds, serial)` →
  `record(duration_s, mode="liveview_clip" | "native_movie", serial)` (expert).

All twelve original functions remain importable. Direct legacy functions retain
their calling conventions; expert registration guards are not a general sandbox
for Python imports. No alias is needed for the three merged, undocumented names.
`set_control` is retained because body-specific controls and generic exposure
ladders are not interchangeable; its existing no-reconnect write policy is preserved.

## Original surface audit

| Original tool | Original docstring first line | Original params | Destination | Overlaps | Intern-test verdict / reason |
| --- | --- | --- | --- | --- | --- |
| `detect_camera` | List attached cameras with their USB serial. Call this first; pass a camera's `serial` to | `none` | agent | `get_camera_settings` | Clear after rewrite: discovery/serials, not exposure reads. |
| `get_camera_settings` | Read the camera's current exposure settings (iso, aperture, shutter, white balance, ...) | `none` | agent | `list_setting_options` | Merge optional setting choices into this read-only inspection tool. |
| `list_setting_options` | List the valid values for a setting (e.g. 'iso', 'aperture', 'shutter', 'white_balance') | `setting` | agent (merged; internal) | `get_camera_settings` | Ambiguous inspection split; use get_camera_settings(setting=...). Old function stays importable. |
| `set_exposure` | Set any subset of exposure controls. Values are strings matching the camera's own labels | `iso, aperture, shutter, white_balance, exposure_comp` | expert | `set_control, shoot` | Explicit numeric exposure. Bounded enums plus live validation; no visual-goal interpretation. |
| `set_control` | Set an allowlisted ordinary setting without reconnecting to retry a failed write. | `setting, value` | expert | `set_exposure` | Keep ordinary body-specific controls. Prefer set_exposure for its bounded numeric subset; no action nodes. |
| `capture_photo` | Capture once and preserve the JPEG even when readback or preview rendering fails. | `serial` | expert | `shoot, auto_expose` | Keep README name: capture current settings once; no automatic exposure goal. |
| `auto_expose` | Bounded JPEG exposure feedback: at most two captures; deliver the last saved frame. | `serial, target_brightness` | expert | `shoot, capture_photo` | Keep README name: numeric brightness feedback, at most two captures. |
| `get_liveview` | EXPERIMENTAL: grab a single live-view frame. Sony USB preview is unreliable and may fail | `none` | expert | `capture_photo, record_video` | Experimental single preview, no finished still or clip. |
| `record_video` | Record a short clip from the camera's live-view and save it as an MP4 to disk — for | `seconds, serial` | expert (merged; internal) | `record_native_video, get_liveview` | Use record(mode="liveview_clip"); best-effort preview MP4, not a native movie. |
| `record_native_video` | Issue bounded native movie start/stop commands and return action evidence only. | `seconds, serial` | expert (merged; internal) | `record_video` | Use record(mode="native_movie"); bounded commands, not proof of a clip. |
| `sun_times` | Golden hour, blue hour, sunrise/sunset for a location and date — for planning when to | `latitude, longitude, date, tz` | agent | `shoot` | Planning times/coordinates only; no capture or network. |
| `shoot` | Take the whole shot from one plain-language vibe — the autopilot does ALL the reasoning. | `intent, auto_correct, serial` | agent | `capture_photo, auto_expose, set_exposure` | Primary visual-goal workflow. Keep product_capture.shoot delegation unchanged. |

`record` is the sole new name. First line: “Use for a bounded video request; use
shoot for a still or get_liveview for a single preview.” Parameters: `duration_s`
(default 6), `mode` (default liveview_clip), `serial` (required for real recording).
The named modes remove the old ambiguity without claiming native-file verification.
Current tool descriptions and schemas are available through `scripts/mcp_list_tools.py --json`.

## Values and useful errors

`record.mode` is a Literal enum. `set_exposure.iso` and `set_exposure.shutter` are
string Literal enums derived from `camera_caps.A7II_CAPS`: the numeric A7 II
reference subset. These enums bound this expert workflow; they do **not** claim
that every Sony body/lens has the same capabilities. All supplied exposure values
must additionally match the connected body's live choices before any setting is
written. Aperture, white balance and exposure compensation remain strings because
they depend on the body/lens. Additional live ISO/shutter labels can use
`set_control` after inspection with `get_camera_settings(setting=...)`.

Schema enum failures and direct value validation list allowed values and include
`Example:`. A bad ISO such as `"150"` cannot cause a write. Validation errors also
carry camera state and guidance. Read-only inspection still uses the existing
placeholder-settings guard; shutter identity checks and non-retry policies stay intact.

## Response contract and mock safety

Agent tools and `record` return `camera_state` and a `next` list of `{tool, when}`.
Errors retain these fields, including schema errors before the function runs.
Existing image/content blocks, delivery evidence and no-retry fields are preserved.

- `camera_state.engine`: selected engine, normally `mock` or `real`.
- `serial`: verified cached identity or the last observed shot identity, never a
  reflection of an unverified requested serial. `null` when unknown or mock.
- `connected`: `false` for mock; `true` for a cached serial-verified USB session;
  otherwise `null` (not probed). Discovery alone does not prove an open connection.
- `last_settings`: last known settings, or `null`; `settings_source` distinguishes
  `unknown`, `last_read`, `last_write_readback`, `last_shot` and `simulated_shot`.
  A write readback may contain only the changed controls. Shot settings are historical
  and may differ from the body's restored settings. Request `get_camera_settings`
  for a fresh read. Formatting a response never opens USB or calls a model.

In mock mode `detect_camera` returns an empty physical-camera list without discovery;
`get_camera_settings` returns the last simulated settings or unknown, plus reference
ISO/shutter choices on request. Expert hardware tools return an error without opening
USB. `shoot` retains its simulated product path. `sun_times` is offline calculation.
Read-only status has no shutter, although real USB reads may wake a connected body.
Refusals name discovery/readback fixes; uncertain captures and recordings never suggest
an automatic retry. Inspect preserved files/card evidence first.

## Use cases and verified limits

The hardware scope is the USB-tethered Mac and two tested bodies (A7 II and A7 III)
described in README and the existing receipts; this change adds no hardware receipt.
The engine defaults to mock. `shoot` accepts a single visual goal such as “make it
moody”, “bright and airy”, or “freeze my kid running”. Settings inspection belongs
in `get_camera_settings`, light planning in `sun_times`.

`shoot` calls `product_capture.shoot` without a stack policy; `capture_policy` defaults
that policy to off. HDR bracket controls, timelapse and panorama are therefore **not**
exposed by this MCP tool. They remain separate backend/session or operator pipelines.
The public repo has no `CLAUDE.md` or `docs/FIELD_GUIDE.md`; README, receipts and the
actual product capture path supplied the evidence instead.

`auto_correct=True` permits up to two product capture attempts and adds latency and
shutters, including possible meter frames. On real hardware the critic uses local
vision or deterministic luma fallback; mock/no critique stops after one attempt.
Long-exposure/refinement refusals remain those of the product pipeline. Native video
returns bounded command evidence only; liveview clips remain best-effort.

## Offline verification, not LLM accuracy

`tests/mcp_tool_choice_cases.json` holds ten human-labelled requests with old and
new destinations and required mode. The contract test verifies those destinations
are registered in the intended mode, checks the 4/10 tool sets and registration
order, description word-Jaccard <= 0.6, enum schema/error examples, response guidance,
mock guards, recording delegation/uncertainty, and live-choice validation using fakes.
This is an availability/description contract, **not** measured LLM tool-choice accuracy
or a semantic classifier. No baseline or after-change LLM accuracy is claimed.

The existing MCP shoot smoke checks the unchanged mock capture path and response shape.
`tests/test_mcp_client.py` now runs under pytest with the active interpreter and mock
engine. `scripts/test_safe.sh` already collects `tests/`, including the new contract.

```bash
bash scripts/test_safe.sh
CAMERA_ENGINE=mock .venv/bin/python scripts/mcp_list_tools.py
CAMERA_ENGINE=mock CAMERA_MCP_EXPERT=1 .venv/bin/python scripts/mcp_list_tools.py
```

When reusing a test environment, set `CAMERA_TEST_PYTHON` to its interpreter and
`CAMERA_AUTOPILOT_VENV` to its venv directory for the launcher checks. Run the listing
script with the same interpreter. The default listing prints names from real MCP
`tools/list`; `--json` prints the full response including descriptions and schemas.
No camera tool is called by the listing script.
