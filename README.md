# Camera Autopilot

Camera Autopilot takes over a Sony mirrorless camera tethered to a Mac by USB: you say what you
want ("make it moody", "freeze my kid running", or nothing at all), and it meters the scene, sets
ISO, aperture, shutter and white balance, fires, downloads the photo and tells you in one line why
it chose those settings. It runs as a local web app (usable from a phone on the same network) and
as an MCP server, so an AI agent such as Claude can drive the camera too.

**Status: a working prototype, tested on two camera bodies. Not a product.**

## How it was built

I (Zohar Tito) built this by directing AI coding agents. I wrote the specs, reviewed the changes,
and tested every live claim on real cameras in my own sittings; the agents wrote most of the code.
The receipts below are from those sittings.

## What works today

Tested on **two bodies only**, both over USB "PC Remote" from a Mac:

- **Sony A7 III** (ILCE-7M3) with an FE 24-105mm F4 G OSS (and a Tamron 28-75mm in one test)
- **Sony A7 II** (ILCE-7M2) with a Tamron 28-75mm F2.8

Across 44 live receipts from 29 September and 1 October 2026: **30 pass** (some with caveats),
**4 partial**, **7 fail**, **2 split** (part pass, part fail), **1 camera-free**. Every receipt,
including every failure, is written up in [`docs/RECEIPTS_2026-09-29.md`](docs/RECEIPTS_2026-09-29.md)
and [`docs/RECEIPTS_2026-10-01.md`](docs/RECEIPTS_2026-10-01.md). Earlier sittings are in
[`docs/RECEIPTS_2026-09-22.md`](docs/RECEIPTS_2026-09-22.md) and
[`docs/RECEIPTS_2026-09-27.md`](docs/RECEIPTS_2026-09-27.md). Highlights that passed:

| What | Receipt |
|---|---|
| One-request shot: meter, set, fire, download, explain (both bodies) | R-0929-1, R-0929-15, R-0929-16 |
| HDR bracket of 2 to 9 frames, merged; recovers a bright window | R-0929-3, R-0929-15 |
| HDR with a person walking through, no ghost in the merge | R-1001-2 |
| Refuses to shoot when the requested camera's USB serial doesn't match | R-0929-13, R-0929-16, R-0929-17 |
| Focus sweep returns to its peak and restores the body's settings | R-1001-3 |
| Timelapse of 60 frames with an exposure change mid-run, encoded to H.264 | R-0929-12 |
| Long exposures of 2, 5 and 15 s (A7 III) and on the A7 II | R-0929-10, R-0929-21 |
| A walking subject gets a faster shutter from the words and a subject check | R-0929-22 |
| MCP `shoot` driven live by an agent, A7 II | R-0929-20 |
| iPhone pairs with a 6-digit code and stays paired across a server restart | R-1001-6, R-1001-8 |
| Auto-save to Photos, share from the phone, change a setting from the phone | R-0929-26, R-0929-27, R-0929-28 |

## What does not work

- **USB-tethered Mac only.** No Wi-Fi control is verified. There is code for the A7 II's Wi-Fi
  "Smart Remote" mode from earlier experiments, but it was not re-tested in these sittings (the
  test Mac's only internet link was Wi-Fi, so joining the camera's network would cut it off).
- **Two bodies only.** Other Sony bodies may enumerate over gphoto2, but nothing else is tested.
  Other brands are not supported.
- **2 to 4 physical shots per request** (a meter frame, then the result; up to four with
  refinement). There is no live preview stream on the Mac.
- **Recorded failures** (from the receipts):
  - Holy Grail dusk timelapse stopped at frame 14 when macOS Preview grabbed the USB camera
    (R-1001-7). Quit Preview, Photos and Image Capture while the camera is in use.
  - A 2×3 grid panorama captured all six frames but did not stitch (R-0929-32).
  - Card browsing on the A7 II lists no files in PC Remote mode (R-0929-18).
  - A warm white-balance request on the A7 II produced a cooler photo (R-0929-19, warm half).
  - An HDR merge left a translucent ghost of a walking person (R-0929-6; passed after a fix in
    R-1001-2).
  - A walking subject was blurred before the subject fix (R-0929-8; passed in R-0929-22).
  - Two shutter timeouts left the outcome uncertain (R-0929-14 on the A7 III at 105 mm, the
    first attempt in R-0929-16 on the A7 II); the product reported them as uncertain and did not
    retry on its own.
  - A focus bracket fired at the night exposure left on the body instead of metering (R-1001-4).
- Some of these have code fixes merged since (panorama stitching, white-balance restore, focus
  bracket and sessions that meter first). Those fixes are covered by camera-free tests only; they
  were **not** re-shot on a camera.
- RAW decoding, motorised focus stacking on the A7 II and native movie control on the A7 III are
  not shipped.

## Quickstart (no camera needed)

The default engine is `mock`: it returns a stand-in photo, so you can try the whole flow without
hardware, Ollama or a GPU. Tested on macOS (Apple Silicon) with Python 3.14.

```bash
git clone https://github.com/zohartito/camera-autopilot.git
cd camera-autopilot
python3.14 -m venv .venv
.venv/bin/pip install -r requirements-test.lock   # exact pins, also installed by Linux CI
ln -s ../.venv backend/.venv                       # the launcher looks for backend/.venv
./run.sh
```

On Linux, install `libgphoto2-dev`, `libgl1` and `libglib2.0-0` first, then use the same lock.
`requirements-test.txt` lists direct dependencies; `requirements-test.lock` pins the complete
test environment used by both the quickstart and CI.

`run.sh` serves the app on `http://127.0.0.1:8000`. It creates one API token per user and keeps it
in a private file (on macOS `~/Library/Application Support/camera-autopilot/api-token`); paste it
into the web UI when asked. Every camera route needs the token, even on loopback. To set your own:
`CAMERA_AUTOPILOT_API_TOKEN=<at least 24 characters> ./run.sh`.

Check that the MCP entry point imports cleanly:

```bash
source .venv/bin/activate
python3 -c 'import server; print("server import: ok")'
```

### With a real camera

Install the **gphoto2 CLI**, which the USB capture path requires in addition to the Python
binding: `brew install gphoto2` on macOS, or `sudo apt-get install gphoto2` on Debian/Ubuntu.
`gphoto2 --version` should work before continuing.

Set the body to **MENU → Setup → USB Connection → PC Remote**, use a data cable, put the mode dial
on **M**, choose a quality that includes JPEG, and set the PC Remote save destination to the
computer. Quit Preview, Photos and Image Capture. Then:

```bash
.venv/bin/python tools/inspect_usb_camera.py   # read-only: lists the body, its serial and settings
CAMERA_ENGINE=real ./run.sh
```

Pick the body by its USB serial in the UI. The camera is identified by serial, never by the
gphoto2 model label, which can be wrong while the body is asleep.

### On a phone (Mac backend, same Wi-Fi)

The phone connects through an HTTPS proxy; `run.sh` keeps the backend on loopback.
Start with the mock web app, then select `CAMERA_ENGINE=real` when the camera is ready.

1. Install the proxy tools with `brew install caddy mkcert nss`. From the repository root,
   preview the configuration with `.venv/bin/python deploy/mac_lan_https.py --tls mkcert`.
   It prints the detected Mac IPv4 address, Caddyfile and trust steps without starting anything.
   If detection is ambiguous, pass `--ip <Mac-Wi-Fi-IPv4>`. Its printed launcher command uses
   `CAMERA_ENGINE=real`; use the mock command in step 3 for this first setup.
2. In that terminal, substitute the address from the preview below. Create the certificate
   and Caddyfile in a new local directory, then leave Caddy running:

   ```bash
   PHONE_IP="<Mac-Wi-Fi-IPv4>"
   mkdir -p artifacts/phone-https
   cd artifacts/phone-https
   mkcert -install
   mkcert "$PHONE_IP"
   ../../.venv/bin/python ../../deploy/mac_lan_https.py --ip "$PHONE_IP" --tls mkcert --write .
   caddy run --config Caddyfile
   ```

   `mkcert -install` installs the local CA on the Mac. The helper refuses to overwrite an
   existing Caddyfile; on later starts, just run Caddy from the same directory.
3. In a second terminal at the repository root, use the same address and start the backend:

   ```bash
   PHONE_ORIGIN="https://<Mac-Wi-Fi-IPv4>:8443"
   CAMERA_ENGINE=mock \
   CAMERA_AUTOPILOT_EXPOSURE=lan-proxy \
   CAMERA_AUTOPILOT_TLS_PROXY_CONFIRMED=1 \
   CAMERA_AUTOPILOT_PUBLIC_HTTPS_URL="$PHONE_ORIGIN" \
   CAMERA_AUTOPILOT_CORS_ORIGINS="$PHONE_ORIGIN" \
   ./run.sh
   ```

4. On the Mac, locate `rootCA.pem` in the directory printed by `mkcert -CAROOT`. AirDrop that
   certificate to the iPhone (never the private key). Install its profile under **Settings →
   General → VPN & Device Management**, then enable full trust under **General → About →
   Certificate Trust Settings**.
5. Open the HTTPS origin from step 3 in the phone's browser. Enter the server's six-digit code
   at the authentication prompt or **Device status → Pair with code**. It expires after ten
   minutes and works once; `.venv/bin/python tools/pair_code.py` issues a fresh code locally.
   The stored token keeps the phone paired across restarts; `./run.sh --rotate-token` resets it.

For the native iOS or Android shell, follow [`mobile/README.md`](mobile/README.md): install its
npm dependencies, set the same HTTPS origin with `npm run backend:set -- <origin>`, sync, and
build the existing native project. iOS signing and Android certificate trust are covered there.

## Tests

The camera-free suite runs the mock engine and points Ollama and the vision service at a dead
port, so nothing leaves the machine and no camera is touched:

```bash
bash scripts/test_safe.sh
```

That runs pytest over `backend/` and `tests/`, the physics self-checks, an MCP stdio smoke test,
the launcher and USB-bridge security scripts and a deterministic intent eval. CI
(`.github/workflows/ci.yml`) runs the same script plus a gitleaks scan.

## MCP server

`server.py` (server name `sony-camera`) leads with four agent tools:

| Tool | Use it for |
| --- | --- |
| `shoot(intent, auto_correct=False, serial=None)` | A photo from one plain-language goal |
| `sun_times` | Offline golden/blue-hour and sunrise/sunset planning |
| `detect_camera` | USB identity discovery before a real capture |
| `get_camera_settings(setting=None)` | Read exposure and, optionally, a setting's valid choices |

Set **`CAMERA_MCP_EXPERT=1`** and restart the server to add `set_exposure`, `set_control`,
`capture_photo`, `auto_expose`, `get_liveview` and `record` (10 tools total). Existing documented
names are preserved; explicit controls/capture are now expert-only. `list_setting_options`
is folded into `get_camera_settings(setting=...)`. `record_video` and `record_native_video`
are replaced on MCP by `record(duration_s, mode="liveview_clip" | "native_movie", serial=...)`.
All old functions remain importable in Python. Native recording returns command evidence,
not proof of a saved clip; inspect the SD card. Recording stays expert-only.

`shoot` uses the same intent, metering and capture path as the web app, returning a preview,
settings, reason and delivery/quality evidence. `auto_correct=True` allows up to two product
capture attempts (which may include meter frames). Real capture uses local vision or luma
fallback; mock/no critique stops after one attempt. HDR bracket, timelapse and panorama
controls are separate workflows, not options on this MCP tool.

The default engine is **mock**. Discovery/settings are camera-free in mock mode and expert
hardware operations refuse without accessing USB. Real shooting requires `CAMERA_ENGINE=real`
and a discovered serial (or the existing `CAMERA_SERIAL` fallback). Verified hardware scope
remains a USB-tethered Mac and the two bodies listed above. Agent responses and `record`
include `camera_state` and `next` guidance; unknown connection/settings state is explicit.
See [the surface audit](docs/MCP_TOOL_SURFACE.md) for the split, enum limits and compatibility.
To add the default surface to Claude Code:

```bash
claude mcp add sony-camera -e CAMERA_ENGINE=mock -- /ABS/PATH/camera-autopilot/.venv/bin/python /ABS/PATH/camera-autopilot/server.py
```

Inspect the actual registry without calling a camera tool:

```bash
CAMERA_ENGINE=mock .venv/bin/python scripts/mcp_list_tools.py
CAMERA_ENGINE=mock CAMERA_MCP_EXPERT=1 .venv/bin/python scripts/mcp_list_tools.py
```

Add `--json` for full descriptions and schemas. Ten offline tool-choice cases check expected
mode/registration; they do not measure LLM selection accuracy.

## Repository layout

- `backend/` — FastAPI app (`app.py`), the intent brain, metering physics, HDR/stack/timelapse
  sessions, and the web UI (`backend/web/`). Tests sit beside the modules (`backend/test_*.py`).
- `sonyremote/` — the USB camera engine on top of libgphoto2 (identity by serial, capture,
  settings read/write/revert).
- `server.py`, `shoot.py` — MCP server and command-line entry points.
- `tools/` — operator tools: camera inspection, focus sweep and bracket, long exposure, panorama,
  offline development, receipt helpers.
- `deploy/` — experimental Raspberry Pi / CM5 bring-up scripts. Not tested on a board.
- `mobile/` — Capacitor iOS/Android wrapper around the same web UI.
- `docs/` — receipts and a few technical notes; see [`docs/README.md`](docs/README.md).

The optional taste index (CLIP embeddings of public Wikimedia photo settings) is not included; see
[`docs/TASTE_INDEX_OPERATIONS.md`](docs/TASTE_INDEX_OPERATIONS.md) to build it. Without it the
autopilot falls back to its physics rules and says that taste was unavailable.

## Licence

[AGPL-3.0-or-later](LICENSE). Copyright (c) 2026 Zohar Tito. Third-party components keep their own
licences; see [`THIRD_PARTY_NOTICES.md`](THIRD_PARTY_NOTICES.md).
