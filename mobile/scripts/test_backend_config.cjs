#!/usr/bin/env node
"use strict";

const assert = require("node:assert/strict");
const fs = require("node:fs");
const os = require("node:os");
const path = require("node:path");
const { spawnSync } = require("node:child_process");
const {
  normalizeBackendUrl,
  resolveBackendUrl,
  serverConfigFor,
  setLocalBackendUrl,
  resetLocalBackendUrl,
  simulatorDefault,
} = require("./backend_config.cjs");

const tempDir = fs.mkdtempSync(path.join(os.tmpdir(), "autopilot-ios-config-"));
const tempFile = path.join(tempDir, ".backend-url");

try {
  assert.equal(normalizeBackendUrl(" https://example.com/ "), "https://example.com");
  assert.equal(normalizeBackendUrl("http://127.0.0.1:8000"), "http://127.0.0.1:8000");

  for (const invalid of [
    "",
    "camera.local:8000",
    "ftp://camera.local",
    "http://192.168.1.42:8000",
    "http://10.0.2.2:8000",
    "https://user:secret@example.com",
    "https://example.com/api",
    "https://example.com/?token=secret",
    "https://example.com/#token",
  ]) {
    assert.throws(() => normalizeBackendUrl(invalid), `expected rejection: ${invalid}`);
  }

  assert.deepEqual(serverConfigFor("https://api.example.com"), {
    url: "https://api.example.com",
    cleartext: false,
    allowNavigation: ["api.example.com"],
  });
  assert.deepEqual(serverConfigFor("http://127.0.0.1:8000"), {
    url: "http://127.0.0.1:8000",
    cleartext: true,
    allowNavigation: ["127.0.0.1"],
  });

  assert.deepEqual(resolveBackendUrl({ env: {}, filePath: tempFile }), {
    url: simulatorDefault,
    source: "simulator default",
  });

  setLocalBackendUrl("https://camera-mac.local", tempFile);
  assert.deepEqual(resolveBackendUrl({ env: {}, filePath: tempFile }), {
    url: "https://camera-mac.local",
    source: ".backend-url",
  });
  assert.equal(fs.statSync(tempFile).mode & 0o777, 0o600);

  assert.deepEqual(
    resolveBackendUrl({
      env: { AUTOPILOT_BACKEND_URL: "https://beta.example.com" },
      filePath: tempFile,
    }),
    { url: "https://beta.example.com", source: "AUTOPILOT_BACKEND_URL" },
  );

  const configProbe = spawnSync(
    process.execPath,
    [
      "-e",
      "process.stdout.write(JSON.stringify(require('./capacitor.config.js')))",
    ],
    {
      cwd: path.resolve(__dirname, ".."),
      encoding: "utf8",
      env: {
        ...process.env,
        AUTOPILOT_BACKEND_URL: "https://camera-mac.local",
      },
    },
  );
  assert.equal(configProbe.status, 0, configProbe.stderr);
  const capacitorConfig = JSON.parse(configProbe.stdout);
  assert.equal(capacitorConfig.server.url, "https://camera-mac.local");
  assert.equal(capacitorConfig.server.cleartext, false);
  assert.deepEqual(capacitorConfig.server.allowNavigation, ["camera-mac.local"]);
  assert.equal(capacitorConfig.ios.contentInset, "never");
  assert.equal(typeof capacitorConfig.android, "object");
  assert.equal(JSON.stringify(capacitorConfig).includes("SET-YOUR-BACKEND-HOST"), false);

  resetLocalBackendUrl(tempFile);
  assert.equal(fs.existsSync(tempFile), false);
  console.log(
    "PASS: iOS backend URL validation, precedence, generated config, and local-file permissions",
  );
} finally {
  fs.rmSync(tempDir, { recursive: true, force: true });
}
