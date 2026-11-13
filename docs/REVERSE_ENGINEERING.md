# Sony A7 II — how it's built, and which "hard limits" are real

Findings from a 5-agent deep dive of libgphoto2 (`camlibs/ptp2`), ma1co/OpenMemories,
frank26080115/alpha-fairy, pixeltris/SonyAlphaUSB, LexOptical/E-Mount, and — most importantly — the
**real ILCE-7M2 fw4.0 property dump** in [libgphoto2 #762](https://github.com/gphoto/libgphoto2/issues/762).
Opcode/property hex is quoted from source. Anything marked "verify" must be confirmed on the powered
camera with `tests/probe_capabilities.py`.

## The camera is three concentric control surfaces

1. **PTP / "PC Remote" over USB** — what we drive today. The whole surface is ~6 Sony opcodes:
   `0x9201` Connect, `0x9202` GetExtDeviceInfo (what this body advertises), `0x9205` SetControlDeviceA
   (absolute set), `0x9207` SetControlDeviceB (the workhorse — **relative** for steppable props,
   momentary for buttons), `0x9209` GetAllExtDevicePropInfo (one ~4KB blob of every prop), `0x1008/0x1009`
   image fetch. Everything is "set property `0xD2xx`" or "press button `0xD2Cx`". The A7 II is a Sony
   **"mode 2"** body (handshake returns protocol 2.00 / `0xc8`) — that is *why* settings step one notch
   at a time instead of jumping to a value.

2. **On-camera Linux + `Backup.bin` + Sony's private Java APIs** — the A7 II is a 4×ARM Cortex-A5
   (Sony **CXD90014 "BIONZ X"**) running Linux + Android 2.3.7. Its firmware is **UNSIGNED** (the lucky
   pre-A7III generation), so ma1co's OpenMemories can install a custom app, open a **telnet root shell +
   adb over Wi-Fi**, edit the ~15,000-entry `Backup.bin` settings store (clear the read-only bit on *any*
   setting, then restore it), and call the firmware's own `CameraEx` (94 methods — `setForceExposureLevel`,
   `adjustAperture`, `startTrackingFocus`, `shiftFocusPosition`…) and `MediaRecorder` (the real movie
   encoder). This is a far wider surface than PTP and sidesteps most limits below.

3. **E-mount lens bus** — a 750k→1.5Mbaud UART, `0xF0`/`0x55` framing + chip-select/vertical-sync lines,
   10 pins. Aperture = two bytes in a type-`0x05` message; focus arrives in type-`0x03`. People have
   emulated a whole lens with a Teensy (LexOptical). Hardware-only; documents how body↔lens talk.

## Ground truth: the real ILCE-7M2 fw4.0 dump

**READWRITE on the A7 II:** F-Number `0x5007`, Exposure Bias `0x5010`, Shutter `0xD20D`, ISO `0xD21E`,
**Color temp `0xD20F`**, **Sensor Crop `0xD219`**, Autofocus `0xD2C1`, Capture `0xD2C2`, Still `0xD2C7`,
**Movie `0xD2C8` (Enum[1,2], press=2/release=1)**.

**ABSENT — the genuine firmware wall:** AFAreaPosition `0xD2DC`, FocusArea `0xD22C`, RemoteTouch `0xD2E4`
(remote AF-point selection was added in gen-3 / A7 III+).

This machine runs libgphoto2 **2.5.34** (recent), so missing writability is most likely a *camera-state
precondition*, not a stale library.

## The 11 limits, reclassified

| # | Limit | Verdict | How to beat |
|---|-------|---------|-------------|
| 1 | USB live view flaky | **MEDIUM — fixable** | Not a wall (Capture One Pro 8.1 streamed A7 II liveview). Keep one PTP session open, tight `GetObject` loop on handle `0xffffc002`, apply the #981 "ILCE-7" event-vs-poll fix. For smooth HQ: clean 1080p60 **HDMI → UVC dongle**, USB stays for control. |
| 2 | AF fails on low contrast (-1) | **PARTIAL** | The -1 is `FocusFound 0xD213` timing out. You **cannot rack focus over USB** — `0xD2D1`/`0xD2D2` are **absent** on the A7 II (confirmed wall, resolves the earlier contradiction). Mitigation is limited to changing focus *mode* (`focusmode` → AF-C/DMF) over USB, or focusing on the body. |
| 3 | EV comp over USB | **NEEDS PROBE** | Property is `0x5010` (not `0xD224`), advertised readwrite. The "physical dial blocks it" theory was **unfounded** — no source supports it. Real risk: firmware silently reverts the set to 0 (libgphoto2 #468). Read-after-write on the body settles it. |
| 4 | Shutter/aperture locked in A/P/Auto | **HARDWARE** | The top mode dial is mechanical; the A7 II has no `expprogram`-over-PTP (newer bodies do). Set the dial to **M** → both become writable. |
| 5 | Settings step slowly | **MEDIUM** | `0x9207` is relative (+1/−1); never instant. A custom setter that skips gphoto2's full-blob re-poll per step (read one prop via `0x9203`) removes most of the latency. |
| 6 | sensorcrop / colortemp read-only | **EASY — precondition** | Real dump = both readwrite. colortemp `0xD20F` unlocks only when WB is set to the Kelvin/Color-Temp mode; sensorcrop `0xD219` is gated by lens + photo/movie mode. Set the gate, re-read, then write. |
| 7 | RAW+JPEG returns the .ARW | **EASY** | `trigger_capture` + two `wait_for_event` loops (#156) to collect both objects; or set JPEG-only via FileFormat `0x5004`; or pick the JPEG object by ObjectFormat, not arrival order. |
| 8 | Auto-power-off | **SOLVED** | No PTP property exists; the keep-alive read loop is correct. Cleaner: OpenMemories-Tweak disables APO persistently in `Backup.bin`. |
| 9 | Native movie over USB | **WORKS — verify** | `0xD2C8` is readwrite in the real dump (press=2/release=1). Caveat: you must **physically turn the mode dial to Movie** — the A7 II can't switch to movie mode over USB (its `expprogram` enum has no movie modes). Then send via `0x9207`, watch `MovieRecordingState 0xD21D`. Fallback: clean HDMI capture. |
| 10 | Move AF point remotely | **HARD — PTP wall** | `0xD2DC`/`0xD22C` are ABSENT on the A7 II. Only paths: OpenMemories on-camera (`startTrackingFocus`/`getFocusAreaInfos`), or in-body AF-area + half-press. |
| 11 | Wi-Fi / SDK dead | **SIDESTEP** | Permanent vendor cutoff; PlayMemories service ended 2025-08-31. USB PC-Remote is strictly more capable. Wireless = Pi/USB-over-IP bridge or an on-camera HTTP server. |

Net (after adversarial verification): genuine USB walls are **#4 (P/A/S/M mode), #10 (AF point), and
remote manual focus** — all absent/mechanical on the A7 II, beatable only by going on-camera (OpenMemories).
**#6 colortemp/sensorcrop and #9 movie are confirmed readwrite in the real dump** — doable. #3 EV-comp is
unsettled (the dial theory was wrong; real risk is a silent revert). Every "verify" is settled by one run of
`tests/probe_capabilities.py` on the powered body.

## Live confirmation — raw USB cannot break the focus walls (2026-06-16)

Powered-camera test (`tests/probe_raw_control.py`, raw pyusb PTP, bypassing gphoto2's mode gate):
the A7 II firmware does **not** expose AFAreaPosition `0xD2DC` or ManualFocusAdjust `0xD2D1` — both
are ABSENT from the body's advertised property table, and writing them via `SDIO_ControlDevice`
(`0x9207`) is a no-op. **Gotcha that nearly fooled us:** this body returns `PTP_RC_OK` (`0x2001`) to
*every* control write, including bogus propcodes (`0xDEAD`) — so the response code is meaningless;
the only true test is whether the property blob actually changes (a real ISO write moves it, these
don't). Conclusion: W1 (AF point) and W2 (manual focus) are **firmware-deep, not a gphoto2 gate** —
unbreakable over USB. W3 (P/A/S/M) likewise. The sole remaining route for all three is **on-camera
(OpenMemories)** — brick-risk, human-gated.

## Attack order (safe → deep)

1. **Run `tests/probe_capabilities.py`** on the powered body. Dumps the advertised prop list and the
   read-only/absent verdict for every limit-deciding node — turns each "verify" into a definite EASY or
   IMPOSSIBLE for *our* exact camera. (Camera battery dead → waiting on the NP-FW50 USB-PD dummy.)
2. **Raw PTP without recompiling:** gphoto2 exposes `/main/actions/opcode` ("PTP Opcode") to send
   arbitrary `0x9207`/`0x9205` calls by hand.
3. **Reliable video/preview:** clean 1080p60 HDMI → cheap UVC capture stick; keep USB for control.
4. **Deepest — OpenMemories** (Sony-PMCA-RE + OpenMemories-Tweak): root shell, `Backup.bin`, on-camera
   `CameraEx`/`MediaRecorder`. Brick risk — last resort, after exhausting non-destructive raw PTP.

## Key repos

- `gphoto/libgphoto2` → `camlibs/ptp2/{ptp.h,config.c,library.c}` — the reference Sony PC-Remote impl.
- `ma1co/Sony-PMCA-RE`, `OpenMemories-Tweak`, `OpenMemories-Framework`, `fwtool.py` — the on-camera layer.
- `frank26080115/alpha-fairy` — plain-English RE notes (AF/MF lock gotcha, RAW+JPEG single notification).
- `pixeltris/SonyAlphaUSB` — clean opcode/property lookup table.
- `LexOptical/E-Mount` — the E-mount lens serial protocol.
