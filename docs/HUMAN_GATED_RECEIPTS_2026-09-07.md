# Human-gated acceptance receipts (2026-09-07)

Every acceptance step that still needs a person becomes one copy-paste command
that writes a JSON receipt and prints a `PASS`/`FAIL` summary. The tools live in
`tools/receipts/`. Each one talks ONLY to the running server at `--base-url`
using the private token read from `--token-file` (sent in the
`X-Camera-Autopilot-Token` header — never in a URL, a log line, or the receipt).

- **Where receipts land:** `artifacts/falsification/human-gated-20260907/`
  (gitignored via `/artifacts/`). Each file is
  `<what>-<UTC timestamp>.json`.
- **Token file:** the launcher writes it to `/tmp/camera-section1/token`
  (the default). Copy it locally; never paste it into a URL or a message.
- **Exit code:** every tool exits `0` only on an observed pass, `1` otherwise.
- **Roll-up:** after any run, `tools/receipts/summary.py` prints one markdown
  table of every receipt in the directory.

All blocks below run from the repository root with `.venv/bin/python`.

The two supported bodies (exact model strings, used verbatim):

| Body | `--model` string | Engine / port |
|------|------------------|---------------|
| Sony A7 III | `Sony Alpha-A7 III (PC Control)` | USB, `CAMERA_ENGINE=real`, port like `usb:001,004` |
| Sony A7 II  | `Sony ILCE-7M2 (Control)`        | USB, `CAMERA_ENGINE=real`, port like `usb:001,005` |

Find the exact port and USB serial for each attached body first (read-only, no capture):

```bash
.venv/bin/python tools/inspect_usb_camera.py --list
```

---

## 1. Body-shutter press (run once per body)

**What it proves:** the host meters each frame the camera pushes, applies the
next exposure, and restores the camera afterward — without ever firing the
shutter itself.

**Preconditions**
- Server running with the real USB engine on `http://127.0.0.1:8001`
  (`CAMERA_INTENT_PROFILE=compact CAMERA_ENGINE=real ./run.sh`).
- The body's mode dial is physically on **M**; the body is awake.
- Only this one body is being driven (handheld sessions take exclusive
  ownership of the camera).

**A7 III**

```bash
.venv/bin/python tools/receipts/body_press.py \
  --model "Sony Alpha-A7 III (PC Control)" --port usb:001,004 --serial "<discovered-USB-serial>" \
  --presses 3 --timeout 600
```

**A7 II**

```bash
.venv/bin/python tools/receipts/body_press.py \
  --model "Sony ILCE-7M2 (Control)" --port usb:001,005 --serial "<discovered-USB-serial>" \
  --presses 3 --timeout 600
```

The tool prints `>>> PRESS THE CAMERA SHUTTER NOW (n of 3) <<<`. Press the
physical shutter each time it asks; after the last press it cancels the session
and restores the body.

**What "pass" looks like:** `body-press: PASS`, with every frame's EXIF and
applied settings recorded, at least one applied change, the camera restored, and
every downloaded frame hash-verified against its receipt. Fails if no frame
arrived (shutter not pressed / transport dead), if the applied settings never
changed, if restoration was not reported, or if a downloaded frame did not match
its receipt.

**Receipt:** `artifacts/falsification/human-gated-20260907/body-press-<model>-<ts>.json`

---

## 2. Wi-Fi Smart Remote session (A7 II)

**What it proves:** the A7 II Smart Remote path applies and verifies white
balance, and the brain's recommended exposure matches what actually stuck over
Wi-Fi (or fails closed honestly when it doesn't).

**Preconditions**
- **On the camera:** `MENU -> Application List -> "Smart Remote Control"` — the
  **full** app, not "Smart Remote Embedded" (the embedded one exposes only
  EV/Self-Timer/Review). Launch it so the body becomes a Wi-Fi access point. The
  mode dial must be on **M** for ISO/aperture/shutter to be settable.
- **On the Mac:** open Wi-Fi and join the camera's network, named
  `DIRECT-xxxx:ILCE-7M2` (password shown on the camera screen the first time).
- Start the server with the Wi-Fi engine on `http://127.0.0.1:8002`:
  `CAMERA_ENGINE=wifi ./run.sh`. (If the camera's endpoint is not the default
  `http://192.168.122.1:8080/sony/camera`, pass `--camera-endpoint`, or set
  `SONY_CAMERA_REMOTE_DD_URL` before starting the server so it SSDP-discovers.)

```bash
.venv/bin/python tools/receipts/wifi_session.py \
  --base-url http://127.0.0.1:8002
```

The tool first TCP-checks the camera's Smart Remote endpoint, then POSTs one
`/shoot` with intent `"warm"` and `refine=false`.

**What "pass" looks like:** `wifi-session: PASS` — HTTP 200,
`applied.white_balance.verified` is true, and the recommended ISO/f/shutter
match `applied_over_wifi`. A refusal (HTTP 409 / `ok:false`) is recorded as the
fail-closed report, which is the correct, honest outcome when a setting could not
be verifiably applied.

**Receipt:** `artifacts/falsification/human-gated-20260907/wifi-session-<ts>.json`

---

## 3. Night stack session (after sunset, tripod)

**What it proves:** a `night` computational session runs as a durable background
job, publishes a stacked image, and that image is downloadable and
hash-verified against the session's own output receipt.

**Preconditions**
- After sunset, low light. Camera on a **tripod** (frames are stacked/averaged;
  any motion smears the result).
- Point at a **static, textured scene** with a few bright points — a lit
  building facade, a street with parked cars and a couple of streetlights, or a
  night skyline. Avoid moving cars/people in frame.
- Mode dial on **M**; body awake. Server on `http://127.0.0.1:8001`
  (real engine). The server must stay on for the whole job.

```bash
.venv/bin/python tools/receipts/night_session.py \
  --model "Sony Alpha-A7 III (PC Control)" --port usb:001,004 --serial "<discovered-USB-serial>" \
  --frames 8
```

To let the body's measured noise table choose the frame count, first take one
ordinary shot to meter from, then pass its gallery name — the tool calls
`/night-assist/plan` and creates the session from the plan (frame count capped by
`--frames`, max 16):

```bash
.venv/bin/python tools/receipts/night_session.py \
  --model "Sony Alpha-A7 III (PC Control)" --port usb:001,004 --serial "<discovered-USB-serial>" \
  --frames 16 --meter-image <shot_name>.jpg
```

A too-dark or unreliable meter fails the receipt honestly instead of guessing.

**What "pass" looks like:** `night-session: PASS` — the job status is
`completed`, `publication.status` is `published`, and `result.jpg` downloaded
through the session artifacts route hash-verifies against the output receipt. The
session sidecar evidence (publication, output receipt, restore state) is written
into the receipt.

**Receipt:** `artifacts/falsification/human-gated-20260907/night-session-<ts>.json`

### 3a. Verify the sigma->N rule against the real-sky frames

**What it proves:** that Night Assist's *prediction* held on a real night sky.
The plan picks N so averaging brings the measured per-frame sigma at the working
ISO down to `sigma_at_iso / sqrt(N)` — a `10*log10(N)` dB reduction — but it
predicts that from a calibration-scene noise table. Once the receipt above has
produced a completed session, this is a one-command check that measures the
*actual* flat-patch luma sigma on the shot frames (using the calibrator's own
residual method, so the numbers are directly comparable) and compares it to the
plan.

```bash
.venv/bin/python tools/night_receipt_compare.py --session <session_id>
# or, straight from the night-session receipt:
.venv/bin/python tools/night_receipt_compare.py \
  --receipt artifacts/falsification/human-gated-20260907/night-session-<ts>.json
```

Read-only: it never modifies the frames. It prints planned vs measured per-frame
sigma, planned vs measured result sigma, and the implied averaging gain in dB vs
the planned `10*log10(N)`, then writes `<session dir>/night_compare.json`.

**What the verdict means:**
- `rule_holds` — measured gain within 1.5 dB of the plan; keep the sigma->N rule.
- `under_delivers` / `over_delivers` — otherwise, with the N the measured data
  would have needed to reach the planned gain (fitted from the observed point).

Adjust the sigma-to-N rule in `backend/night_assist.py` **only if** a real-sky
session verdict says so — a daylight bench frame at base ISO sits on the JPEG
noise floor (not photon noise) and under-delivers by construction, which is not
evidence about the rule.

---

## 4. Beginner study (10 participants)

**What it proves:** the anonymous UX-validation contract works end to end — one
consented, completed session per participant, and the earliest-10 cohort gate
report is reachable.

**Preconditions**
- Server on `http://127.0.0.1:8001`.
- The participant used the app and answered on the `/validation` screen. The
  consent line they must tick reads, verbatim (from `backend/web/validation.html`):
  *"Save my anonymous answers and the photo filename for this study."* Nothing is
  stored without it. No direct identifier is ever sent; the server assigns an
  anonymous `ux_<hex>` id. The pseudonym (P01…P10) is for your receipt file only.

Run once per participant, passing that participant's real answers. `--consent`
is mandatory (only pass it after they ticked the box); a completed session needs
`--magic 1-5`:

```bash
.venv/bin/python tools/receipts/beginner_session.py \
  --participant P01 --consent \
  --magic 4 --would-use yes --confusion-area none \
  --experience-mode live_capture --completion-status completed
```

The tool prints the exact in-app steps the participant follows, records the
session, and fetches the cohort report. Repeat for `P02` … `P10`. After the
tenth, the report's gate flips from `not_enough_sessions` to `pass`/`fail`.

**What "pass" looks like:** `beginner-session: PASS` — the session was saved
(anonymous id shown) and the report was fetched. The receipt carries the saved
session summary and the aggregate gate status (never the free-text note or any
photo reference).

**Receipt:** `artifacts/falsification/human-gated-20260907/beginner-P0N-<ts>.json`

---

## 5. Phone: Developer Mode + signing + LAN HTTPS join

This is a manual Xcode/Wi-Fi step, not a receipt tool — the App Store / Apple ID
signing cannot be scripted here. It is the precondition for a phone to reach the
backend over trusted HTTPS (never expose Uvicorn's plaintext port to the LAN).

**Preconditions**
- `caddy` and `mkcert` installed: `brew install caddy mkcert nss`.
- Mac and iPhone on the **same Wi-Fi**.
- An Apple ID added to Xcode (Settings → Accounts) for a free personal team.

**Steps**

1. Render (and inspect) the LAN HTTPS reverse-proxy config — nothing is written
   or started without `--write`/`--run`:

   ```bash
   .venv/bin/python deploy/mac_lan_https.py --backend-port 8001
   .venv/bin/python deploy/mac_lan_https.py --backend-port 8001 --write /tmp/camera-lan-https
   .venv/bin/python deploy/mac_lan_https.py --backend-port 8001 --write /tmp/camera-lan-https --run
   ```

2. Start the backend in LAN-proxy mode (token file written mode-`0600`, not
   printed):

   ```bash
   CAMERA_ENGINE=real \
   CAMERA_AUTOPILOT_EXPOSURE=lan-proxy \
   CAMERA_AUTOPILOT_TLS_PROXY_CONFIRMED=1 \
   CAMERA_AUTOPILOT_PUBLIC_HTTPS_URL=https://camera.example.test \
   ./run.sh
   ```

3. Point the Capacitor shell at the proxy origin and sync the existing project
   (never `cap add ios` on this checkout):

   ```bash
   cd mobile
   npm run backend:set -- https://camera.example.test
   npm run sync -- ios
   npm run open:ios
   ```

4. On the iPhone: **Settings → Privacy & Security → Developer Mode → On**, then
   restart the phone and confirm.
5. In Xcode: select the `App` target → **Signing & Capabilities** → choose your
   Apple Developer **Team** → select the attached iPhone → **Run**. Bundle id
   `com.zohar.autopilot`; deployment floor iOS 15. First launch: on the phone,
   **Settings → General → VPN & Device Management** → trust the developer app.
6. Trust the proxy's local CA on the phone (the `mkcert`/`tls internal` root)
   before entering the token, then paste the token from
   `/tmp/camera-section1/token` into the app when prompted.

**What "pass" looks like:** the app loads over `https://…`, backend status is
green, and a capture/review round-trip works from the phone. There is no JSON
receipt for this step; record it by noting the working phone session in the
beginner-study receipts (interface `web`/`ios`).

References: `deploy/mac_lan_https.py`, `mobile/README.md`.

---

## 6. Pi arrival (portable Linux host)

When an owned Raspberry Pi 5 / CM5 arrives, its bring-up already has a dedicated,
receipt-writing runbook — do not re-invent it here.

**Preconditions**
- A fresh Raspberry Pi OS (64-bit, Bookworm or newer) on the Pi.
- The reviewed repo and a **Linux-native** `backend/.venv` copied to the Pi (a
  macOS virtualenv cannot be copied). `gphoto2`/`libgphoto2` installed with
  normal USB permissions; never run the camera backend as root.

**Steps (dry-run first, then apply):**

```bash
bash deploy/pi_bringup.sh --dry-run
bash deploy/pi_bringup.sh --apply
bash deploy/pi_bringup.sh --apply --phase proof \
  --model "Sony Alpha-A7 III (PC Control)" --port usb:001,014 \
  --token-file ~/.config/camera-autopilot/api-token
```

`pi_bringup.sh` is idempotent, runs no `sudo` itself (it prints the exact apt /
udev commands), and writes a JSON receipt per phase under `--receipt-dir`
(default `~/camera-autopilot-receipts`). The proof phase is a **20-shot no-drop
capture** on the selected body. Once the service is up on the Pi, the body-press
and night receipts above run against it the same way (point `--base-url` at the
Pi's loopback proxy).

**What "pass" looks like:** a green 20-shot proof receipt. Note that this is
software-side field evidence for one board only; whole-device power, mass,
thermal-under-enclosure, and battery endurance remain separate physical gates.

References: `deploy/pi_bringup.sh`, `deploy/PI_BRINGUP.md`, `deploy/README.md`.

---

## Roll-up

After running any receipts, print the combined table:

```bash
.venv/bin/python tools/receipts/summary.py
```

It reads every JSON receipt in
`artifacts/falsification/human-gated-20260907/` and prints one markdown table
(what, when, pass/fail, image/session ids). It never touches the network or the
token.
