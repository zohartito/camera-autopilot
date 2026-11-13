#!/usr/bin/env bash
# Build, install, launch, and screenshot the current Autopilot UI in an iOS simulator.
set -euo pipefail

cd "$(dirname "$0")/.."

# shellcheck source=ios_toolchain.sh
source scripts/ios_toolchain.sh
autopilot_use_xcode

backend_url="${AUTOPILOT_BACKEND_URL:-http://127.0.0.1:8000}"
derived_data="${IOS_DERIVED_DATA:-/tmp/camera-autopilot-ios-derived}"
artifact_dir="${IOS_ARTIFACT_DIR:-$PWD/artifacts/ios-simulator}"
backend_log="$artifact_dir/backend.log"
screenshot="$artifact_dir/home.png"
proof_json="$artifact_dir/proof.json"
repo_root="$(cd .. && pwd)"
# Demo captures from the mock backend stay out of the real gallery (backend/shots/); the
# repo-root artifacts/ tree is gitignored and may contain imagery.
demo_shots_dir="${IOS_SMOKE_SHOTS_DIR:-$repo_root/artifacts/ios-simulator/demo-shots}"

mkdir -p "$artifact_dir"

cleanup() {
  if [[ -n "${backend_pid:-}" ]]; then
    kill "$backend_pid" >/dev/null 2>&1 || true
    wait "$backend_pid" 2>/dev/null || true
  fi
}
trap cleanup EXIT

AUTOPILOT_BACKEND_URL="$backend_url" \
  IOS_DERIVED_DATA="$derived_data" \
  bash scripts/build_ios_simulator.sh

python_bin="${IOS_SMOKE_PYTHON:-$repo_root/.venv/bin/python}"
if [[ ! -x "$python_bin" ]]; then
  python_bin="$(command -v python3)"
fi

: >"$backend_log"
(
  cd "$repo_root/backend"
  CAMERA_ENGINE=mock \
    CAMERA_AUTOPILOT_SHOTS_DIR="$demo_shots_dir" \
    CAMERA_AUTOPILOT_API_TOKEN=ios-simulator-smoke-token \
    "$python_bin" -m uvicorn app:app --host 127.0.0.1 --port 8000
) >"$backend_log" 2>&1 &
backend_pid=$!

for _ in $(seq 1 40); do
  if curl --fail --silent -H 'X-Camera-Autopilot-Token: ios-simulator-smoke-token' "$backend_url/status" >/dev/null; then
    break
  fi
  sleep 0.25
done
curl --fail --silent -H 'X-Camera-Autopilot-Token: ios-simulator-smoke-token' "$backend_url/status" >/dev/null

device_udid="${IOS_SIMULATOR_UDID:-}"
if [[ -z "$device_udid" ]]; then
  device_udid="$(
    xcrun simctl list devices available -j | node -e '
      let body = "";
      process.stdin.on("data", chunk => body += chunk);
      process.stdin.on("end", () => {
        const runtimes = JSON.parse(body).devices || {};
        const devices = Object.entries(runtimes)
          .sort(([a], [b]) => b.localeCompare(a))
          .flatMap(([, rows]) => rows)
          .filter(device => device.isAvailable && device.name.includes("iPhone"));
        const preferred = devices.find(device => device.name === "Autopilot iPhone 17 Pro")
          || devices.find(device => device.name.includes("iPhone 17 Pro"))
          || devices[0];
        if (preferred) process.stdout.write(preferred.udid);
      });
    '
  )"
fi

if [[ -z "$device_udid" ]]; then
  echo "No available iPhone simulator was found." >&2
  exit 1
fi

xcrun simctl boot "$device_udid" >/dev/null 2>&1 || true
xcrun simctl bootstatus "$device_udid" -b

app_path="$derived_data/Build/Products/Debug-iphonesimulator/App.app"
bundle_config="$app_path/capacitor.config.json"
if [[ ! -f "$bundle_config" ]]; then
  echo "Built app is missing capacitor.config.json." >&2
  exit 1
fi
node - "$bundle_config" "$backend_url" <<'NODE'
const fs = require("node:fs");
const [configPath, expectedUrl] = process.argv.slice(2);
const config = JSON.parse(fs.readFileSync(configPath, "utf8"));
if (config.server?.url !== expectedUrl) {
  throw new Error(`Built app backend is ${config.server?.url}; expected ${expectedUrl}`);
}
if (JSON.stringify(config).includes("SET-YOUR-BACKEND-HOST")) {
  throw new Error("Built app still contains the retired placeholder backend host.");
}
NODE

xcrun simctl terminate "$device_udid" com.zohar.autopilot >/dev/null 2>&1 || true
xcrun simctl uninstall "$device_udid" com.zohar.autopilot >/dev/null 2>&1 || true
xcrun simctl install "$device_udid" "$app_path"
launch_output="$(xcrun simctl launch "$device_udid" com.zohar.autopilot)"
sleep "${IOS_SMOKE_SETTLE_SECONDS:-7}"
xcrun simctl io "$device_udid" screenshot "$screenshot"

if ! grep -Eq '"GET / HTTP/1\.[01]" 200' "$backend_log"; then
  echo "The app launched, but the backend did not observe a successful WebView root request." >&2
  echo "See $backend_log" >&2
  exit 1
fi

case "$screenshot" in
  "$PWD"/*) proof_screenshot="${screenshot#"$PWD"/}" ;;
  *) proof_screenshot="$(basename "$screenshot")" ;;
esac

node - "$proof_json" "$backend_url" "$proof_screenshot" <<'NODE'
const fs = require("node:fs");
const [proofPath, backendUrl, screenshot] = process.argv.slice(2);
const proof = {
  ok: true,
  tested_at: new Date().toISOString(),
  backend_url: backendUrl,
  simulator: "local iOS simulator",
  app_path: "<derived-data>/Build/Products/Debug-iphonesimulator/App.app",
  screenshot,
  bundle_id: "com.zohar.autopilot",
  checks: [
    "backend mock status reachable",
    "unsigned simulator build succeeded",
    "built app contains the requested backend URL and no placeholder host",
    "app installed",
    "app launched",
    "WebView loaded GET / with HTTP 200",
    "simulator screenshot captured",
  ],
};
fs.writeFileSync(proofPath, JSON.stringify(proof, null, 2) + "\n");
NODE

echo "Simulator smoke proof: $proof_json"
echo "Screenshot: $screenshot"
