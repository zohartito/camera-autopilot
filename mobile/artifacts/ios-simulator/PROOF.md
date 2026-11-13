# iOS simulator proof

Verified July 27, 2026 in the current architecture.

## Result

- Xcode 27.0 beta discovered at `~/Downloads/Xcode-beta.app`.
- CocoaPods 1.17.0 completed the Capacitor sync.
- Capacitor 8.4.2 passed `cap doctor`; `npm audit` reported 0 vulnerabilities.
- The iOS 15+ app built for the simulator with signing disabled.
- The built app contained `http://127.0.0.1:8000`, not a placeholder host.
- Bundle `com.zohar.autopilot` installed and launched on
  `Autopilot iPhone 17 Pro` (iOS 26.5).
- The mock FastAPI backend observed a successful WebView `GET /`.
- The live Autopilot UI rendered full-bleed with backend status, Review, mood,
  Photo/Video, voice, and shutter controls visible.

## Reproduce

```bash
cd /path/to/camera-autopilot/mobile
npm run smoke:ios:sim
```

The command regenerates:

- `home.png`: simulator screenshot.
- `proof.json`: machine-readable build/install/launch assertions.
- `backend.log`: backend request evidence.

These generated files remain local and are intentionally ignored by Git.

## Physical-device boundary

An iPhone 15 Pro Max is paired and visible to Xcode. A device
build is not yet possible because:

1. Developer Mode is disabled on the iPhone.
2. The Xcode target has no Apple Developer Team selected.
3. The Mac keychain reports zero valid code-signing identities.

The simulator result above is unaffected by those external signing requirements.
