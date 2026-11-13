#!/usr/bin/env bash
# Reproducible unsigned Capacitor build. This never signs, archives, or uploads the app.
set -euo pipefail

cd "$(dirname "$0")/.."

# shellcheck source=ios_toolchain.sh
source scripts/ios_toolchain.sh
autopilot_use_xcode

if ! command -v pod >/dev/null 2>&1; then
  echo "CocoaPods is required; install it before running this build." >&2
  exit 1
fi

if [[ -n "${1:-}" ]]; then
  export AUTOPILOT_BACKEND_URL="$1"
fi

node scripts/backend_config.cjs show
echo "Xcode: $(xcodebuild -version | tr '\n' ' ')"
echo "Developer directory: $DEVELOPER_DIR"
echo "CocoaPods: $(pod --version)"

if [[ "${IOS_SKIP_NPM_CI:-0}" != "1" ]]; then
  npm ci
fi
npm run test:config
npx cap sync ios

destination="${IOS_SIMULATOR_DESTINATION:-generic/platform=iOS Simulator}"
derived_data="${IOS_DERIVED_DATA:-/tmp/camera-autopilot-ios-derived}"

xcodebuild \
  -quiet \
  -workspace ios/App/App.xcworkspace \
  -scheme App \
  -configuration Debug \
  -sdk iphonesimulator \
  -destination "$destination" \
  -derivedDataPath "$derived_data" \
  CODE_SIGNING_ALLOWED=NO \
  build

echo "Unsigned simulator app: $derived_data/Build/Products/Debug-iphonesimulator/App.app"
