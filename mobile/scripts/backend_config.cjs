#!/usr/bin/env node
"use strict";

const fs = require("node:fs");
const path = require("node:path");

const mobileRoot = path.resolve(__dirname, "..");
const localConfigPath = path.join(mobileRoot, ".backend-url");
const simulatorDefault = "http://127.0.0.1:8000";

function isLoopbackSimulator(parsed) {
  return parsed.protocol === "http:" && ["127.0.0.1", "localhost", "::1", "[::1]"].includes(parsed.hostname);
}

function normalizeBackendUrl(raw) {
  const value = String(raw || "").trim();
  if (!value) {
    throw new Error("Backend URL is empty.");
  }

  let parsed;
  try {
    parsed = new URL(value);
  } catch {
    throw new Error(`Invalid backend URL: ${value}`);
  }

  if (parsed.protocol !== "https:" && !isLoopbackSimulator(parsed)) {
    throw new Error("Backend URL must use HTTPS, except for an exact loopback simulator origin.");
  }
  if (!parsed.hostname) {
    throw new Error("Backend URL must include a hostname.");
  }
  if (parsed.username || parsed.password) {
    throw new Error("Do not put credentials or API tokens in the backend URL.");
  }
  if (parsed.search || parsed.hash) {
    throw new Error("Backend URL cannot include a query string or fragment.");
  }
  if (parsed.pathname && parsed.pathname !== "/") {
    throw new Error("Backend URL must point at the origin, without a path.");
  }

  return parsed.origin;
}

function readLocalBackendUrl(filePath = localConfigPath) {
  if (!fs.existsSync(filePath)) return null;
  return normalizeBackendUrl(fs.readFileSync(filePath, "utf8"));
}

function resolveBackendUrl({
  env = process.env,
  filePath = localConfigPath,
  fallback = simulatorDefault,
} = {}) {
  if (env.AUTOPILOT_BACKEND_URL) {
    return {
      url: normalizeBackendUrl(env.AUTOPILOT_BACKEND_URL),
      source: "AUTOPILOT_BACKEND_URL",
    };
  }

  const local = readLocalBackendUrl(filePath);
  if (local) return { url: local, source: path.basename(filePath) };

  return { url: normalizeBackendUrl(fallback), source: "simulator default" };
}

function serverConfigFor(url) {
  const parsed = new URL(normalizeBackendUrl(url));
  return {
    url: parsed.origin,
    cleartext: isLoopbackSimulator(parsed),
    allowNavigation: [parsed.hostname],
  };
}

function setLocalBackendUrl(raw, filePath = localConfigPath) {
  const url = normalizeBackendUrl(raw);
  fs.writeFileSync(filePath, `${url}\n`, { encoding: "utf8", mode: 0o600 });
  fs.chmodSync(filePath, 0o600);
  return url;
}

function resetLocalBackendUrl(filePath = localConfigPath) {
  if (fs.existsSync(filePath)) fs.unlinkSync(filePath);
}

function usage() {
  return [
    "Usage:",
    "  npm run backend:show",
    "  npm run backend:set -- https://backend.example.com",
    "  npm run backend:reset",
    "",
    "AUTOPILOT_BACKEND_URL overrides the saved local value for one command.",
  ].join("\n");
}

function runCli(argv = process.argv.slice(2)) {
  const command = argv[0] || "show";

  if (command === "show") {
    const backend = resolveBackendUrl();
    const server = serverConfigFor(backend.url);
    console.log(`Backend: ${backend.url}`);
    console.log(`Source: ${backend.source}`);
    console.log(`Transport: ${server.cleartext ? "local HTTP" : "HTTPS"}`);
    return;
  }

  if (command === "set") {
    if (!argv[1]) throw new Error(`${usage()}\n\nMissing backend URL.`);
    const url = setLocalBackendUrl(argv[1]);
    console.log(`Saved backend: ${url}`);
    console.log("Run `npm run sync -- ios` or `npm run sync -- android` before launching the app.");
    return;
  }

  if (command === "reset") {
    resetLocalBackendUrl();
    console.log(`Reset backend to simulator default: ${simulatorDefault}`);
    return;
  }

  throw new Error(`${usage()}\n\nUnknown command: ${command}`);
}

if (require.main === module) {
  try {
    runCli();
  } catch (error) {
    console.error(error.message);
    process.exitCode = 1;
  }
}

module.exports = {
  localConfigPath,
  simulatorDefault,
  normalizeBackendUrl,
  readLocalBackendUrl,
  resolveBackendUrl,
  serverConfigFor,
  setLocalBackendUrl,
  resetLocalBackendUrl,
  runCli,
};
