#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
SMOKE="$ROOT/tools/falsify_usb_bridge_lan.sh"
TMP_ROOT="$(mktemp -d "${TMPDIR:-/tmp}/camera-autopilot-bridge-smoke-test.XXXXXX")"
TOKEN="bridge-smoke-test-token-that-is-long-enough"

cleanup() {
  rm -rf -- "$TMP_ROOT"
}
trap cleanup EXIT

fail() {
  echo "FAIL: $*" >&2
  exit 1
}

bash -n "$SMOKE"

PLAINTEXT_OUT="$TMP_ROOT/plaintext.out"
if env \
  BRIDGE_API_TOKEN="$TOKEN" \
  BRIDGE_CLIENT_BASE="http://192.168.50.8:8766" \
  "$SMOKE" "$TMP_ROOT/plaintext-artifacts" >"$PLAINTEXT_OUT" 2>&1; then
  fail "plaintext LAN client URL was accepted without the trusted-lab override"
fi
grep -Fq "Refusing to send a bridge API token over plaintext LAN HTTP" "$PLAINTEXT_OUT" ||
  fail "plaintext LAN rejection was unclear"
[[ ! -e "$TMP_ROOT/plaintext-artifacts" ]] ||
  fail "plaintext LAN rejection created proof artifacts"

SHORT_TOKEN_OUT="$TMP_ROOT/short-token.out"
if env \
  BRIDGE_API_TOKEN="too-short" \
  "$SMOKE" "$TMP_ROOT/short-token-artifacts" >"$SHORT_TOKEN_OUT" 2>&1; then
  fail "short bridge token was accepted"
fi
grep -Fq "must be at least 24 characters" "$SHORT_TOKEN_OUT" ||
  fail "short-token rejection was unclear"

BAD_SCHEME_OUT="$TMP_ROOT/bad-scheme.out"
if env \
  BRIDGE_API_TOKEN="$TOKEN" \
  BRIDGE_CLIENT_BASE="ftp://camera.example.test" \
  "$SMOKE" "$TMP_ROOT/bad-scheme-artifacts" >"$BAD_SCHEME_OUT" 2>&1; then
  fail "unsupported bridge client URL scheme was accepted"
fi
grep -Fq "must begin with http:// or https://" "$BAD_SCHEME_OUT" ||
  fail "bad-scheme rejection was unclear"

grep -Fq 'bridge_host="${BRIDGE_HOST:-127.0.0.1}"' "$SMOKE" ||
  fail "secure loopback bind is not the default"
grep -Fq 'Authorization: Bearer ${api_token}' "$SMOKE" ||
  fail "smoke requests do not carry bearer authentication"
grep -Fq 'CAMERA_AUTOPILOT_USB_BRIDGE_TOKEN="$api_token"' "$SMOKE" ||
  fail "bridge token is not passed through the environment"
if grep -Fq -- '--api-token "$api_token"' "$SMOKE"; then
  fail "bridge token is exposed in process arguments"
fi

echo "USB bridge smoke security tests passed"
