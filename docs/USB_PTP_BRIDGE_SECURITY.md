# USB/PTP Bridge Security

`tools/usb_ptp_bridge_server.py` is a host-side HTTP bridge to a camera attached
over USB. The static control page (`/`) and process-liveness probe (`/health`)
are public. `/health` does not run `gphoto2`, inspect camera state, or create
request artifacts. Camera readiness is reported by the authenticated `/ready`
endpoint. Every endpoint that reads camera state or can trigger camera behavior
requires authentication.

`POST /capture` is intentionally tombstoned: gphoto2's `%C` filename expansion
cannot prove that a downloaded artifact is JPEG rather than RAW. The bridge
therefore returns `410 Gone` before creating artifacts or invoking a process.

## Recording-stop quarantine

If a native-movie stop command cannot be confirmed, recording is quarantined
process-wide and in the local `.recording-safety/recording-stop-uncertain.json`
sentinel. New Camera objects, FastAPI real-engine requests, and MCP recording
tools all refuse before connecting or starting another recording. No HTTP or
MCP endpoint can clear this state.

First physically confirm on the camera that recording has stopped. Only then run
this local, affirmative command:

```bash
python3 tools/clear_recording_quarantine.py --verified-device-idle
```

The flag is an operator attestation, not a device check. Do not clear the
sentinel merely to retry a command. Clearance leaves the `0700`, current-user,
non-symlink quarantine root in place; a later uncertain stop reuses only that
validated root, fsyncs the recreated sentinel and root, and remains quarantined
after process restart.

## Authentication

Generate a fresh token for each test session and keep it in the environment:

```bash
export CAMERA_AUTOPILOT_USB_BRIDGE_TOKEN="$(
  python3 -c 'import secrets; print(secrets.token_urlsafe(32))'
)"
```

Clients may send either header:

```text
Authorization: Bearer <session token>
X-API-Key: <session token>
```

Prefer the bearer header. Do not put the token in a URL, source file, shell
history, screenshot, artifact, or commit. The server does not print the token.
Unset it when the session ends:

```bash
unset CAMERA_AUTOPILOT_USB_BRIDGE_TOKEN
```

## Loopback Use

Loopback is the default bind address. CORS is disabled unless an origin is
explicitly configured:

```bash
python3 tools/usb_ptp_bridge_server.py
```

The local control page is then available at `http://127.0.0.1:8765/`. Enter the
session token in its password field and choose **Use Token** before invoking a
camera operation. The server refuses to start without a token of at least 24
characters, including on loopback.

## Trusted LAN Use

Do not expose the bridge's plaintext listener directly to a phone or LAN.
Keep the bridge bound to loopback and place it behind a trusted HTTPS reverse
proxy as described in `docs/SECURITY_PRIVACY_RELEASE.md`. The client-facing URL
must be HTTPS, and the proxy must not log the `Authorization` header.

Allow only the exact HTTPS browser origin that will call the bridge:

```bash
python3 tools/usb_ptp_bridge_server.py \
  --host 127.0.0.1 \
  --port 8765 \
  --cors-origin 'https://camera.example.test'
```

Repeat `--cors-origin` to allow another exact origin. Wildcard origins,
credential-bearing origins, paths, query strings, and fragments are rejected.
CORS is a browser boundary, not authentication; non-browser clients must still
provide the API token.

`tools/falsify_usb_bridge_lan.sh` follows the same policy. It generates a
session token, passes it through the environment, authenticates every protected
request, redacts it from artifacts, binds to loopback by default, and refuses a
plaintext non-loopback client URL. There is no plaintext-LAN diagnostic override.

Example public liveness, authenticated readiness, and settings calls:

```bash
curl --fail http://127.0.0.1:8765/health
curl --fail \
  -H "Authorization: Bearer ${CAMERA_AUTOPILOT_USB_BRIDGE_TOKEN}" \
  http://127.0.0.1:8765/ready
curl --fail \
  -H "Authorization: Bearer ${CAMERA_AUTOPILOT_USB_BRIDGE_TOKEN}" \
  http://127.0.0.1:8765/settings/camera.iso
```

## No Authentication Bypass

`--lab-allow-unauthenticated` remains a rejected legacy command-line option so
older automation fails clearly. It never starts the bridge. Every camera route
requires a valid token, and no unauthenticated lab mode exists.

## Artifact Retention

Retention removes only request directories positively marked by this bridge: a
bridge-specific prefix and UUID must agree with a small ownership marker inside
the direct child of the configured artifact root. Unrelated directories,
symlinks, and lookalike names are preserved. This deliberately favors bounded
bridge-owned cleanup over trying to clean every entry an operator may place in
the artifact root.

## Verification

The camera-free security contract covers side-effect-free public liveness,
authenticated camera readiness, missing and invalid credentials, bearer and
API-key success paths, settings/focus/preview handling through mocks, the
JPEG-unprovable capture tombstone, exact-origin CORS, loopback defaults, and
lab-bypass restrictions:

```bash
python3 backend/test_usb_ptp_bridge_security.py
bash tests/test_usb_bridge_smoke_security.sh
```

The tests do not connect to the camera or perform camera operations.
