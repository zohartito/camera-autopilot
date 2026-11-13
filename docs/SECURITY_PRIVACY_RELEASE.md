# Security, Privacy, and Release Gates

This document is the release boundary for Camera Autopilot. It does not make the
current development build a production service.

## Current Decision

| Use | Decision | Conditions |
| --- | --- | --- |
| Local development on one Mac | Conditional go | Run `./run.sh`; backend remains on loopback. |
| Trusted hardware testing | Conditional go (non-Route-3 only) | Use an isolated network and a unique token; OpenMemories Route 3 is permanently disabled. |
| Phone or LAN testing | No direct-HTTP path | Use the documented HTTPS reverse-proxy mode only. |
| Public, App Store, or commercial release | No-go | Every unchecked release gate below must be closed. |

The USB bridge now has fail-closed token authentication, camera-route
authorization, restrictive exact-origin CORS, and a loopback default. It is
approved for local development on loopback. It is not approved for direct LAN
or public exposure because its HTTP listener does not terminate TLS; put it
behind a trusted HTTPS reverse proxy before any non-loopback client uses it.

## Launcher and Network Policy

`./run.sh` binds Uvicorn to `127.0.0.1` by default. It never prints the API token.
When no token is supplied, the launcher reuses one per-user token (64 hex
characters, 32 random bytes) from a private file it creates once:
`~/Library/Application Support/camera-autopilot/api-token` on macOS,
`${XDG_CONFIG_HOME:-~/.config}/camera-autopilot/api-token` elsewhere. The
directory is 0700 and the file 0600. Directories below HOME (or
`XDG_CONFIG_HOME`) are walked without following symlinks and must be owned by
the user and not group/other-writable; a file that is a symlink, not a regular
file, owned by someone else or readable by group/other is refused, never
repaired (`backend/api_token_store.py`). Uvicorn runs one worker, so one
pairing code and one rate budget exist. `./run.sh --rotate-token` (or
`CAMERA_AUTOPILOT_ROTATE_TOKEN=1`) atomically replaces it; every paired phone
must then pair again. A token supplied through the environment is written to a
mode-`0600` temporary file that is deleted when the launcher exits. A caller-supplied
`CAMERA_AUTOPILOT_API_TOKEN` must contain at least 24 characters and no
whitespace; weaker values are rejected before token-file creation or server
startup.

Local launch:

```bash
./run.sh
```

`POST /record` always requires the API token, even when the browser is on the
same Mac. A missing or wrong token is rejected before the camera engine is
inspected; an unconfigured token disables the route with `503`. For a
non-loopback client, a valid recording token is accepted only over HTTPS.

Copy the token on macOS without displaying it:

```bash
pbcopy < /path/printed/by/the/launcher
```

Phone pairing (lan-proxy only): the server prints a one-time 6-digit code on
the Mac terminal at startup (stderr, never the logging system). The phone
trades it once for the token with `POST /api/pair {"code": "123456"}` (JSON
only, HTTPS through the proxy or loopback; plain LAN HTTP gets `403`). A code
is valid 10 minutes, is cleared on success, and is cleared after 5 wrong or
malformed attempts (chance of a guess 5 in 10^6 per code); all attempts share a
global budget of 10 per minute (`429`). Every refusal is the same `401`. A new
code needs a restart or `python tools/pair_code.py --port <port>`, which calls
the loopback-only, token-authenticated `POST /api/pair/new`.

LAN mode is an explicit statement that a trusted HTTPS reverse proxy is already
configured on the same Mac. Uvicorn still binds only to loopback:

```bash
CAMERA_AUTOPILOT_EXPOSURE=lan-proxy \
CAMERA_AUTOPILOT_TLS_PROXY_CONFIRMED=1 \
CAMERA_AUTOPILOT_PUBLIC_HTTPS_URL=https://camera.example.test \
./run.sh
```

The proxy must:

1. Terminate TLS with a certificate trusted by every client.
2. Listen on the LAN-facing address.
3. Forward only to `http://127.0.0.1:8000`.
4. Preserve `Host`, `X-Forwarded-For`, and `X-Forwarded-Proto`.
5. Add its own access controls if the network is not a dedicated test network.
6. Never log the `Authorization` header or API token.

Example Caddy configuration for a hostname with a trusted certificate:

```caddyfile
camera.example.test {
    reverse_proxy 127.0.0.1:8000
}
```

A locally issued certificate is acceptable only after its issuing CA is
intentionally installed and trusted on each test device. Plain HTTP, a browser
warning bypass, and direct wildcard Uvicorn binding are not approved LAN paths.

## Capture Retention and Deletion

Captured images may reveal faces, homes, screens, documents, location clues, and
EXIF metadata. Text sidecars may contain the user's intent, settings, critique,
and timestamps.

Current development policy:

- `backend/shots/` and `captures/` are local working data. Delete them within
  seven days after a test session unless the owner explicitly promotes a file to
  evidence.
- `artifacts/falsification/`, `docs/progress/`, and `output/playwright/` are
  permanently ignored local evidence locations. They must not be committed,
  even after redaction; record only a minimal non-sensitive attestation when a
  future approved workflow needs durable evidence.
- Do not commit faces, addresses, screens, serial numbers, Wi-Fi credentials,
  API tokens, or precise location metadata.
- Deleting an image also requires deleting its JSON/EXIF sidecar and any copied
  preview, RAW, or artifact version.
- Backups and synced folders are separate copies. The operator must delete those
  copies or record why an evidence copy remains.

### Managed gallery lifecycle (2026-09-05)

`backend/photo_lifecycle.py` now implements bounded, authenticated-API-ready
per-photo deletion, explicitly confirmed delete-all, generated thumbnails, saved
explanation access, and configurable retention. The HTTP integration and user
interface are verified separately from the service's isolated fixture tests.

- Deletion includes the gallery image, exact-stem JPEG/RAW/JSON/XMP siblings,
  generated thumbnail, lifecycle marker, and explicitly enrolled owned files
  beneath the same trusted gallery root (including session frames/temp files).
- No ownership is inferred from arbitrary paths inside capture sidecars. Path
  traversal and symlinks are rejected; descriptor-relative access prevents
  symlinked parent directories from escaping the gallery.
- `manage_shot(root, image)` enrolls only a newly created successful capture or
  edit. Existing gallery photos and private proof directories are never enrolled
  automatically. Re-enrollment never resets a photo's original retention age.
- Retention defaults to **keep** (`days: null`). The owner may choose 1–3650 days.
  Automatic cleanup acts only on enrolled gallery files. The application must run
  enforcement at startup, periodically, and after successful capture/edit; the
  helper itself does not create an independent background process.
- Delete-all requires the exact confirmation `delete-all-local-photos`; callers
  can preview deletion with `dry_run=True`. Fixture tests never touch real photos.
- Camera SD cards, external `captures/` source copies, arbitrary `/tmp` files,
  private proof artifacts, feedback/diagnostic logs, exported/shared copies, and
  backups remain outside gallery deletion. The interface must make this scope
  visible. Do not describe gallery deletion as erasing every copy everywhere.

The earlier blanket seven-day cleanup command is withdrawn: it could erase
historical evidence or unowned files. Existing evidence remains owner-managed.
Public-release acceptance still requires verified route/UI behavior and a
complete lifecycle for any additional data stores included in the release.

## Historical OpenMemories Debug Rollback

OpenMemories Route3, ADB, and Telnet were development-only surfaces. This historical
rollback checklist is retained for incident interpretation only: Route 3 is permanently
disabled, its local proof artifacts are unavailable, and no Route 3 operation is authorized.

1. Save the exact Route3 APK hash and the pre-test port scan.
2. Run only the approved, non-destructive Route3 endpoints.
3. Call `/shutdown` or use the app's Exit action.
4. Disable ADB and Telnet in OpenMemories Tweak.
5. Exit the app and power-cycle the camera.
6. From the same LAN context, verify ports `23`, `5555`, and `8080` are closed.
7. Uninstall the Route3 proof app unless the next test explicitly needs it.
8. Save the post-test scan and rollback result with the session artifact.
9. If any port remains open, isolate or power off the camera and mark the session
   failed. Do not continue to Smart Remote or LAN testing.

The Route3 HTTP server has no production authentication contract. It must not be
left running on a shared network.

## P3 Tombstones and Offline Evidence Policy

The historical OpenMemories proof-app generator/build, mutable developer
toolchain bootstrap, and GitHub Actions workflows are tombstoned. They are not
approved to download packages, resolve branch heads or SNAPSHOT dependencies,
run with repository credentials, install onto a camera, or contact hardware.
No replacement is enabled until an owner supplies an offline-verifiable,
immutable dependency manifest, artifact hashes, least-privilege CI design, and
a separately approved hardware test plan.

USB/gphoto JSON reports are untrusted imported evidence. They may inform an
operator checklist but cannot auto-promote settings in the durable registry.
An owner must independently attest new hardware evidence in an approved session
before any registry update. The taste-index builder accepts only HTTPS Wikimedia
Commons `File:` records, rejects redirects and third-party dataset URLs, and
enforces an 8 MiB source-image budget. Raw PTP parsing also has per-container,
per-transaction, and container-count budgets.

## Release Gate Checklist

### Code and transport

- [ ] A clean checkout has an independently reproducible, fully passing safe-suite attestation.
- [x] USB bridge has authentication, authorization, least-privilege routes, and restrictive CORS.
- [ ] Every LAN path uses trusted TLS; no service binds plaintext control APIs directly to the LAN.
- [x] Development tokens are not printed, placed in URLs, checked in, or retained in proof logs.
- [ ] Rate limits, request-size limits, and abuse tests cover capture and setting writes.
- [ ] OpenMemories rollback evidence is historical and unavailable; Route 3 is permanently disabled, so no rollback operation is currently authorized.

### Privacy and data lifecycle

- [ ] User-facing privacy disclosure names captures, RAW files, EXIF, intent text, feedback, and diagnostics.
- [ ] Per-item deletion and delete-all behavior remove images, sidecars, previews, and caches.
- [ ] Retention is configurable and automatically enforced.
- [x] Committed logging and evidence summaries redact secrets, Wi-Fi credentials, serials, and personal media.
- [ ] Ten consenting beginner sessions meet the recorded UX acceptance threshold.

### Distribution, intellectual property, and operations

- [ ] The repository owner chooses and adds a project license. No license is selected by this document.
- [ ] `THIRD_PARTY_NOTICES.md` is regenerated from exact production lockfiles and packaged binaries.
- [ ] Required third-party license texts and attributions are shipped in the app and distribution.
- [ ] Sony Camera Remote Command package access and redistribution terms are reviewed in writing.
- [ ] PMCA/OpenMemories use and Sony proprietary API implications receive legal review.
- [ ] Patent/freedom-to-operate review is completed for the intended commercial feature set.
- [ ] Apple signing, privacy declarations, export compliance, and App Store metadata are complete.
- [ ] Incident response, support ownership, update signing, and rollback procedures are assigned.

No public-release claim is allowed while any applicable box remains unchecked.

## Clean-Checkout Verification

The checked regression gate above was exercised from a detached worktree at
commit `ee0273c68a50378eb22b4fba084072e42e3db9b6` on 2026-07-27. The replay ran:

- `bash scripts/run_a7ii_settings_tests.sh`;
- `bash backend/run_tests.sh`;
- `cd mobile && npm test`;
- `cd mobile && npm audit --audit-level=high`;
- `cd mobile && npm run smoke:ios:sim`;
- the full taste-index integrity and retrieval benchmark.

All commands passed. The simulator smoke produced an unsigned app and screenshot;
the taste index validated 233,785 aligned rows; the dependency audit found zero
vulnerabilities. `git status --short --untracked-files=no` was empty after the
replay, proving that the tests did not alter tracked source or canonical hardware
evidence. The machine-readable result is
the current camera-free checks; historical local scan artifacts are deleted and
must not be treated as a release attestation.

## Read-only deployment preflight

Run `python tools/deployment_preflight.py` for non-sensitive configuration and
file-presence checks. It never opens a camera or prints the token. A configured
HTTPS URL is not proof of certificate trust on a physical phone; package presence
is not review of Sony model support or redistribution permission. A local package
locator run on 2026-09-05 is recorded privately under
`artifacts/falsification/final-wave-20260905/lifecycle/sony-package/`. Its findings
do not grant package access or resolve distribution terms.

## Mac LAN HTTPS bootstrap helper

`deploy/mac_lan_https.py` renders the phone-facing LAN HTTPS configuration for a
Mac on the same Wi-Fi. It is dry-run by default and changes no system state: it
detects the Mac's LAN IPv4 (en0/en1, refusing when none is found or the choice is
ambiguous unless `--ip` is given), reports whether `caddy`/`mkcert` are installed
(printing `brew install caddy mkcert nss` without running it), and prints a
Caddyfile that terminates TLS on `https://<ip>:8443` and reverse-proxies the
loopback app on `127.0.0.1:8000` (`--backend-port`/`--https-port` override both).
TLS is a real mkcert certificate when mkcert is present, otherwise `tls internal`;
either CA must be intentionally trusted on the phone first, exactly as the proxy
requirements above demand. The tool also prints the exact run.sh environment line
(`CAMERA_AUTOPILOT_EXPOSURE=lan-proxy CAMERA_AUTOPILOT_TLS_PROXY_CONFIRMED=1
CAMERA_AUTOPILOT_PUBLIC_HTTPS_URL=https://<ip>:8443
CAMERA_AUTOPILOT_CORS_ORIGINS=https://<ip>:8443 CAMERA_ENGINE=real ./run.sh`) and
the iOS root-CA trust steps.

It only writes the Caddyfile with `--write <dir>` (never to a system location,
and never overwriting an existing file), and only starts `caddy run` with
`--run`. Rendering a config proves nothing about the phone actually connecting,
trusting the CA, or reconnect behavior.

## Local-gallery directory retention

`backend/photo_lifecycle.py` may enroll an owned *directory* (`owned_dirs`) in
addition to individual owned files, so files added under a job directory (for
example `development/<job_id>/`) after enrollment are still removed on deletion.
Enrolled directory trees are walked without following symlinks and bounded to
depth 4; a symlink inside such a tree is unlinked as a link, never followed to its
target. After a successful deletion, directory shells that were enrolled or that
became empty as a result are pruned, while the gallery root, `.thumbnails`, and
`.lifecycle` are always preserved. `delete_all` additionally removes orphan
`.lifecycle/<image>.json` records whose image is already gone, along with any
still-present owned paths and directories they referenced. The automatic retention
sweep interval is `CAMERA_AUTOPILOT_RETENTION_SWEEP_SECONDS` (default 60, clamped
to 10..3600).
