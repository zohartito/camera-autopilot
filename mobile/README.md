# Autopilot iOS and Android

This directory contains the Capacitor iOS and Android shells for the existing
Camera Autopilot web application. Each shell loads the FastAPI-served UI and
keeps relative API requests on the same backend origin. It does not duplicate
the product UI.

## Backend host

There is no checked-in placeholder host. Resolution order is:

1. `AUTOPILOT_BACKEND_URL` for one command.
2. `mobile/.backend-url`, managed by the commands below.
3. `http://127.0.0.1:8000`, the simulator default.

```bash
cd /path/to/camera-autopilot/mobile

npm run backend:show
npm run backend:set -- https://camera.example.test
npm run backend:reset
```

Only an `https://` origin is supported for a physical phone; loopback HTTP is
reserved for the simulator. Paths, query strings, fragments, and credentials
are rejected. The saved host is local-only, gitignored, and written with mode
`0600`.

For a one-off build:

```bash
AUTOPILOT_BACKEND_URL=https://camera.example.com npm run build:ios:sim
```

After changing the saved host, run `npm run sync -- ios` or `npm run sync -- android`
before opening the existing native project. Never run `cap add ios` or
`cap add android` on this checkout; both native projects are committed.

## Simulator

The automated smoke flow discovers the full Xcode installation, installs
dependencies, validates configuration, syncs Capacitor, builds without signing,
starts the backend in mock mode, boots an available iPhone simulator, installs
and launches Autopilot, verifies that the WebView requested `/`, and captures a
screenshot.

```bash
cd /path/to/camera-autopilot/mobile
npm test
npm run smoke:ios:sim
```

Proof is written under `mobile/artifacts/ios-simulator/`. Logs, screenshots, and
JSON are intentionally untracked and regenerated on each run; `PROOF.md`
records the latest verified baseline.

## Physical iPhone

The phone and backend must be able to reach each other. Use the launcher's
HTTPS reverse-proxy mode for any phone or LAN client; do not expose Uvicorn's
plaintext listener directly. From the repository root:

```bash
CAMERA_ENGINE=mock \
CAMERA_AUTOPILOT_EXPOSURE=lan-proxy \
CAMERA_AUTOPILOT_TLS_PROXY_CONFIRMED=1 \
CAMERA_AUTOPILOT_PUBLIC_HTTPS_URL=https://camera.example.test \
./run.sh
```

Then point the mobile shell at the proxy origin and sync the existing project:

```bash
cd mobile
npm run backend:set -- https://camera.example.test
npm run sync -- ios
npm run open:ios
```

The launcher keeps one per-user API token in a mode-`0600` file without printing
it, so a paired phone stays paired across restarts (`./run.sh --rotate-token`
replaces it). To pair, enter the 6-digit code the server prints on the Mac
terminal (10 minutes, single use) in the 401 prompt or under Device status >
Pair with code; `python tools/pair_code.py --port <port>` issues a new one.
Pasting the token still works. Video recording always requires
it, including on loopback; the proxy must terminate trusted TLS before
forwarding to `http://127.0.0.1:8000`.

In Xcode, select the `App` target, choose an Apple Developer Team under
Signing & Capabilities, choose the attached iPhone, and press Run. The bundle
identifier is `com.zohar.autopilot`; the deployment floor is iOS 15.

## Product UI

The app uses `backend/web/index.html` as its source and the running FastAPI
backend as its runtime origin. Current simulator proof covers the actual
Autopilot interface: backend status, review access, mood and photo/video
controls, voice affordance, and shutter. Re-run `npm run sync -- ios` whenever
that web UI changes.

## Android

The committed project is `android/`, Capacitor **8.6.2**, minSdk **29**. Custom
`AutopilotMedia` (`savePhoto` / `sharePhoto` / `safeArea`) matches the iOS
plugin: MediaStore insert of the **original JPEG bytes**, `ACTION_SEND` +
FileProvider, `WindowInsetsCompat`. Do not add `@capacitor/share` or
`@capacitor-community/media`. Never re-run `cap add android`.

```bash
cd mobile
npm test
npm run sync -- android
```

**Emulator (loopback HTTP, not a physical receipt).** Keep
`http://127.0.0.1:8000` and reverse the backend port; do not switch the origin
to `http://10.0.2.2`:

```bash
adb reverse tcp:8000 tcp:8000
```

An unsigned debug APK is the iOS-simulator analogue. It is not Play Store
acceptance and it is not a physical-phone receipt.

**Physical phone.** Use the same `deploy/mac_lan_https.py` HTTPS origin as iOS.
A **debug** build opts into user-installed CAs so mkcert can work. A **release**
build trusts the system store only and will not blindly trust a user CA.
Physical Android (install, Gallery byte identity, share-sheet cancel, WebView
token/cache, live capture) is unproven. The owner's daily phone is an iPhone, so
that receipt cannot be taken here.

A29 (Arsenal Android panorama) is a different row and stays blocked. Scope:
[docs/ANDROID_PARITY_SCOPE_2026-09-20.md](../docs/ANDROID_PARITY_SCOPE_2026-09-20.md).
