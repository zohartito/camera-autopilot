# iOS build and beta readiness

The iOS app is a committed Capacitor project at `mobile/ios/App`. It renders the
current Camera Autopilot web UI against a configured FastAPI backend. There is
no hardcoded placeholder host and no separate native copy of the UI.

## Verified local toolchain

Verified on July 27, 2026:

| Component | Verified state |
|---|---|
| Node | v26.5.0 |
| Capacitor | 8.4.2 (`cap doctor`: iOS looking great) |
| Full Xcode | 27.0 beta (`~/Downloads/Xcode-beta.app`) |
| CocoaPods | 1.17.0 |
| Deployment target | iOS 15.0 |
| Unsigned simulator build | Passing |
| Simulator install and launch | Passing |
| WebView backend load | Passing (`GET /` returned 200) |
| npm dependency audit | Passing (0 vulnerabilities) |

The scripts discover Xcode in `/Applications`, `~/Applications`, or
`~/Downloads`, so the system-wide `xcode-select` value does not need to change.

## Backend configuration

From `mobile/`:

```bash
npm run backend:show
npm run backend:set -- https://camera.example.com
npm run backend:reset
```

`AUTOPILOT_BACKEND_URL` overrides the saved value for one command:

```bash
AUTOPILOT_BACKEND_URL=https://camera.example.com npm run sync -- ios
```

The resolver accepts only a clean HTTP(S) origin and derives
`server.allowNavigation` and cleartext behavior. It rejects credentials, paths,
query strings, and fragments. The ignored `.backend-url` file is mode `0600`.

The default is `http://127.0.0.1:8000`, which is correct only for the simulator.
A physical iPhone must use an authenticated HTTPS reverse proxy; direct LAN HTTP
is intentionally unsupported.

## Reproducible simulator proof

```bash
cd /path/to/camera-autopilot/mobile
npm test
npm run build:ios:sim
npm run smoke:ios:sim
```

The smoke command:

1. Validates backend URL normalization, precedence, config generation, and file
   permissions.
2. Runs `cap sync ios`.
3. Builds `App.app` for the iOS simulator with signing disabled.
4. Verifies the requested backend URL is embedded in the app and no retired
   placeholder remains.
5. Starts the existing backend with the mock camera engine and verifies authenticated `/status`.
6. Boots, installs, and launches on an available iPhone simulator.
7. Requires a successful WebView `GET /` in the backend log.
8. Captures `mobile/artifacts/ios-simulator/home.png` and writes `proof.json`.

The latest human-readable proof is
`mobile/artifacts/ios-simulator/PROOF.md`.

## Physical-device test

Start the loopback backend through an authenticated HTTPS reverse proxy, using the
repository launcher (do not bind Uvicorn directly to a LAN interface):

```bash
cd /path/to/camera-autopilot
CAMERA_ENGINE=mock \
CAMERA_AUTOPILOT_EXPOSURE=lan-proxy \
CAMERA_AUTOPILOT_TLS_PROXY_CONFIRMED=1 \
CAMERA_AUTOPILOT_PUBLIC_HTTPS_URL=https://camera.example.test \
./run.sh
```

Then configure and sync:

```bash
cd /path/to/camera-autopilot/mobile
npm run backend:set -- https://camera.example.test
npm run backend:show
npm run sync -- ios
npm run open:ios
```

In Xcode:

1. Select the `App` target.
2. Open Signing & Capabilities.
3. Enable automatic signing and choose your Apple Developer Team.
4. Confirm bundle identifier `com.zohar.autopilot`.
5. Select the attached iPhone and press Run.
6. Allow Local Network, Microphone, and Speech Recognition when prompted.
7. Confirm the status indicator becomes ready.
8. Enter a mood, switch Photo/Video, press the shutter, open Review, and submit
   one feedback action.

Reset the saved development host afterward with `npm run backend:reset`.

## TestFlight blockers

Simulator readiness does not require signing. A physical-device or TestFlight
build still requires external Apple state:

- Developer Mode enabled on the paired iPhone under Settings > Privacy &
  Security.
- An Apple Developer Team available in Xcode.
- A valid development/distribution signing identity and provisioning profile.
- The paired physical iPhone registered or trusted for local testing.
- An App Store Connect record whose bundle ID is `com.zohar.autopilot`.
- A stable phone-reachable HTTPS backend for testers.
- Product metadata, privacy answers, screenshots, and beta-review details.

Once those exist: choose the Team, set version/build numbers, select
`Any iOS Device (arm64)`, use Product > Archive, and upload from Organizer.

Current command-line proof stops exactly at those boundaries: the paired physical
iPhone is detected but currently unavailable; the Xcode project has no selected
development team; and the keychain reports zero valid code-signing identities.
The exact cause of the unavailable physical-device state must be confirmed on the
phone/Xcode before recording Developer Mode as the blocker.

## Guardrails

- Do not commit `.backend-url`, tokens, credentials, or a private LAN address.
- Do not place API tokens in the backend URL.
- Do not run `cap add ios`; it can overwrite the committed native project.
- Re-run `npm run sync -- ios` after web UI, plugin, or host changes.
- The backend and bridge require a token on every camera route, including
  loopback. There is no unauthenticated development override.
