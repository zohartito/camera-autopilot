# Camera route matrix — current, September 15, 2026

This replaces the tombstoned `docs/falsification/capability_matrix.md` as the live statement of which
camera routes are supported, unsupported, or unproved on the two recorded rigs. Every "proven" row
points at a receipt produced on the named body; "unproved" means no receipt exists, not that the
route fails. Private receipts live under ignored `artifacts/` paths and the session directories.

## Recorded rigs and host

| Rig | Body | USB serial | Lens | Driver label | Host |
|---|---|---|---|---|---|
| A7 II | ILCE-7M2 | `A7II00SERIAL` | Tamron 28-75 (calibrated profile) | `Sony ILCE-7M2 (Control)` | macOS 27, libgphoto2 2.5.32 |
| A7 III | ILCE-7M3 | `A7III0SERIAL` | FE 24-105mm F4 G OSS | `Sony Alpha-A7 III (PC Control)` | same |

USB ports move across reconnects; identity is the serial, never the port or the gphoto label
(the A7 III can enumerate as an "ILCE-7M2" row while asleep). macOS `ptpcamerad` must be released
before every Python claim and after the settle gap before the single CLI capture
(`sonyremote/camera.py`, committed `7da5e908`).

## Routes

| Route | A7 II | A7 III | Evidence | Remaining gate |
|---|---|---|---|---|
| USB claim, settings read/write/revert (Python bindings) | proven | proven | read-only probes and restored settings 2026-09-13/15 (`artifacts/orchestrator-20260913/`, `artifacts/stage-wave-20260915/route-matrix/a7iii-inspect.json`) | driver menus advertise values the body/lens cannot reach; planners apply `constrain_mounted_lens` |
| USB single capture + download (gphoto2 CLI, one attempt) | proven | proven | A7 III meter+final pairs and 2-/4-frame stacks 2026-09-13 with hash-verified downloads; A7 II receipts in earlier waves | shutter envelope: `max_enabled_shutter_s = 1.0` (`provisional_short_exposure_policy`); ≥ 1 s retrieval unproved on either body |
| USB multi-frame stack (night / averaging / HDR) | **proven 2026-09-20** | proven | sessions `8f62ca95…` (N=2) and `a33ccd64…` (N=4) completed, restored, published | motion deghosting now built (`backend/stacking.py`, this wave); star/sky scenes unproved |
| USB liveview preview frame (`capture_preview`, no shutter) | unproved | proven | `route-matrix/focus-experiment-v3/` — repeatable 1024×680 JPEG frames, sharpness metric stable to ±1 % | product preview endpoint stays disabled on the Mac (decoder bound); a bounded Linux path is Stage F work |
| Manual focus actuation (`/main/actions/manualfocus`, range −7..7) | **unsupported** (node absent from descriptor; raw-PTP route disabled) | **proven optical** | center Laplacian variance 720 → 9.5 after two −7 steps, rail end at 9.3, partial return 22.5 after +7 steps, AF re-acquire 742; settings restored and read back | steps are relative and uncalibrated; asymmetric/clipped response observed; single lens only |
| AF half-press + `focusindication` readback | **proven, and slower than the A7 III** | proven | A7 II 2026-09-20: `Unlock` at a 1.0 s hold, `Focus Locked` at 2.0 s. A7 III: `Focus Locked` at 1.2 s. | no tap-to-focus coordinates over this route (`focus_coordinates: false`) |
| Focus bracket capture (K frames across focus planes) | unsupported | **proven, one live bracket** | `backend/focus_bracket.py`, `tools/focus_bracket_capture.py`; bracket 03: three confirmed exposures toward near, full-frame center sharpness 82 → 78 → 29, focus stack merged, focus mode restored (`artifacts/stage-wave-20260915/focus-bracket-03/`) | steps are uncalibrated and non-uniform; DoF/CoC slab planning needs a calibrated step size; brackets 01/02 stopped on the post-capture preview payload before the re-read was added |
| Handheld listen loop (body shutter, PC save destination) | proven | proven | Section 1 wave 2026-09-07 | libgphoto2 handle is single-threaded; nothing else may touch the body during a session |
| Wi-Fi Smart Remote capture | proven (historical) | n/a (not offered by body app) | earlier waves | body must be put in Smart Remote by hand; USB is off while it runs |
| Wi-Fi full-resolution file with identity proof (`CAMERA_WIFI_FULL_RES=1`) | unproved (current code) | n/a | historical full-res experiments predate `wifi_full_res.verify_identity` | owner puts the A7 II in Smart Remote and joins the DIRECT- network; then one identity-gated receipt |
| Native movie start/stop | historical (A7 II) | unproved | earlier waves | recording safety state must be inspected before promotion |
| Raw PTP focus/AF-area (`0xD2D1`, `0xD2DC`) | disabled | disabled | `docs/REVERSE_ENGINEERING.md`; A7 II firmware does not expose them | not a route; the A7 III driver node above supersedes the need |

## Safe shutter envelope

Ordinary USB capture is limited to `max_enabled_shutter_s = 1.0` until long-shutter retrieval is
physically validated. Night Assist therefore plans frame counts at ≤ 1 s and the measured A7 III
ISO ceiling of 3200 (`data/sensor_profiles/ilce-7m3.json`).

Added 2026-09-27: a separate long single-exposure route (`Camera.capture_long_exposure`, 1.3–30 s,
trigger plus a bounded event wait) serves light-trail requests only when the server runs with
`CAMERA_LONG_EXPOSURE=1`. Its only physical evidence is the 2026-09-05 A7 II 30 s probe; the A7 III
and shorter A7 II shutters await `tools/long_exposure_capture.py --run` receipts.

## How to repeat

```bash
.venv/bin/python tools/inspect_usb_camera.py --list
.venv/bin/python tools/inspect_usb_camera.py --model "Sony Alpha-A7 III (PC Control)" --port <port>
.venv/bin/python tools/focus_bracket_capture.py --model "Sony Alpha-A7 III (PC Control)" --port <port> --dry-run
```

The focus experiment itself is a short script kept with its receipt in
`artifacts/stage-wave-20260915/route-matrix/`; it fires no shutter and restores focus mode, ISO and
shutter before exiting.

## Addendum, 2026-09-20: identity, and the A7 II's first autopilot photograph

**Discovery no longer trusts the driver label.** With a real iPad also on USB, `gphoto2 --auto-detect`
labelled the A7 II's port `usb:000,004` as "Apple iPad Air", so the body was invisible to the picker,
could not be selected, and the identity gate refused. The body was alive throughout: addressed by
model it returned its serial and card contents. `detect_identified()` now takes identity from the
operating system plus a bounded read-only probe of OS-confirmed Sony ports only, and every row
carries its serial and how it was identified (commit `8aaa3757`). Verified live: A7 III
`usb:000,003` by autodetect serial `A7III0SERIAL`; A7 II `usb:000,004` by recovered probe serial
`A7II00SERIAL`; the real iPad listed unsupported and never probed. **Ports re-enumerate between
plug-ins. The serial is the only stable identifier.**

**The A7 II had never taken an autopilot photograph until today, and the first one was out of focus.**
`shot_1789933782863875000.jpg`, Tamron 28-75mm at 28 mm, ISO 400, f/5, 1/50, `existing_profile`.
Metering worked correctly, recovering from an unreadable first frame through one remeter pass, and
the exposure was reasonable at mean luma 182. Focus was not: the sharpness evidence reported
`verdict: soft`, 33 % of the frame's best-tile detail, and `focusindication` read `Unlock`.

The cause is body-specific timing, not a broken design. The `carry_meter_focus_position` policy lets
the meter frame's autofocus establish focus and then freezes the body into Manual so the final
exposure holds exactly that position. Correct. But it froze after `focus_lock_s: 0.9687`, and this
body needs about 2 s. It locked an unfocused position and shot it. The A7 III locks at about 1.2 s,
which is why a week of A7 III receipts never exposed it. **A capture path that assumes one body's
timing will quietly produce bad photographs on another.**

## Addendum, 2026-09-20 (later): the A7 II's first computational sessions

With focus and discovery fixed, the A7 II ran the modes it had never attempted. All settings restored.

| Mode | Session | Result |
|---|---|---|
| HDR, 3 frames at plus and minus 2 EV | `7c27b568` | merged detail 16.91 against 6.87 to 10.99 for its inputs, clipping 2.97 % against 20.45 % on the brightest frame. **This is what exposed the deghost blotch defect**, fixed in `7a2dedeb`. |
| Averaging, 4 frames | `51f8b5bd` | flat-wall noise 3.59 to 3.66 per frame, 2.74 merged. Moving fraction 10.4 %, the television being genuinely in motion. |
| Timelapse, 4 frames | `a2983bb6` | completed, video written, 4 of 4 accepted, real span 31.6 s against the 24 s the requested interval implied, reported honestly. |

**A tripod bump was caught in the act, and handled correctly.** The first averaging attempt
(`7ad0fbb3`) flagged 42.7 % of the frame as moving. Frames 0 to 2 held to 1.5 px while frame 3 sat
**1119 px** away: the camera was knocked between exposures. EXIF was identical across all four
frames (28 mm, f/6.3, 1/50, ISO 640), which independently confirms a Fixed stack locks exposure on
this body. Registration still produced an unghosted result, and the mask correctly declined to
average the regions that no longer corresponded. **The cost of that honesty is that those regions
get no noise benefit, which is the right trade.**

The A7 II's remaining gaps are unchanged: no dark-sky night scene, no real panorama sweep, no crowd,
and focus bracketing is permanently unavailable because its driver exposes no `manualfocus` node.

## Addendum, 2026-09-20 (third): the HDR "generated" look was pyramid depth

The A7 II bracket above was re-rejected on sight after the deghost fix: the blotches were
gone, but the photograph was still flat overall and crunchy up close. It was not taste. The
tiled Mertens fusion carried only `_STACK_PYRAMID_LEVELS = 4` levels, so on a 6000x4000 frame
the brackets were blended at scales up to **16 px out of 6000** and no further. Low frequencies
never moved between exposures (global tonality flattened) and every exposure decision was
forced into fine scales (halos and crunch). That combination is the tonemapped-HDR look.

Fusion now carries full depth inside the same memory bound: the tiles supply the fine levels,
and a whole-frame fusion of the 1/16 Gaussian level — assembled from the tiles' own pyramid
bases, with the normalised weights carried down alongside the pixels — supplies the rest. The
merge is now **bit-exact against a non-tiled full-depth reference implementation**
(`backend/test_hdr_fusion_scale.py`, max |delta| = 0 code values; the old path differs by a
mean of 30). `HDR_TONALITY_STRENGTH` drops 0.7 -> 0.3, because it had been compensating for
this and at 0.7 it re-crunched the result.

Measured over every bracket session on both bodies, against the frame the camera exposed best:

| Session | Body | detail, shallow | detail, fixed | saturation: bracket / shallow / fixed |
|---|---|---|---|---|
| `02290746` | A7 III | **3.58x** | 0.98x | 86.4 / 82.7 / 86.4 |
| `2255fd47` | A7 III | **2.35x** | 1.06x | 59.6 / 63.7 / 65.0 |
| `3d2cbfa3` | A7 III | 1.31x | 1.23x | 203.3 / **186.9** / 211.3 |
| `7c27b568` | A7 II | **3.44x** | 1.20x | 60.3 / 57.7 / 61.1 |

**The shallow merge scored better on highlight clipping** — 0.22 % against the bracket's
3.32 % on `02290746`, a 15x "win" — because flattening a photograph is one way to stop
clipping it. The honest recovery is 1.74 %. Two acceptance bars in
`backend/test_hdr_tonality.py` had been calibrated on that output and are re-based there with
the measurements. This is the second defect on this algorithm to have been propping up the
metric meant to police it.

Open, and an owner decision rather than a bug: exposure fusion compresses global tonality by
construction, so on a scene whose best bracket barely clips, merging can only make the
photograph worse. Explicit Fixed stacks are honoured as requested; nothing declines to merge.
