const { resolveBackendUrl, serverConfigFor } = require("./scripts/backend_config.cjs");

const backend = resolveBackendUrl();

/** @type {import('@capacitor/cli').CapacitorConfig} */
const config = {
  appId: "com.zohar.autopilot",
  appName: "Autopilot",
  webDir: "../backend/web",
  server: serverConfigFor(backend.url),
  ios: {
    contentInset: "never",
  },
  android: {
    // AutopilotMedia.safeArea reports system bars. Emulator HTTP is adb reverse to loopback.
  },
};

module.exports = config;
