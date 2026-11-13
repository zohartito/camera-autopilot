#!/usr/bin/env bash
set -euo pipefail

# Archive one USB/PTP bridge LAN smoke attempt.
#
# This starts tools/usb_ptp_bridge_server.py, calls the authenticated HTTP
# endpoints, and saves both client responses and bridge-side command artifacts.
# The bridge is permanently loopback-only. A real LAN client must use an HTTPS
# reverse proxy; plaintext LAN testing is intentionally unsupported.
#
# Usage:
#   tools/falsify_usb_bridge_lan.sh
#   BRIDGE_PORT=8767 tools/falsify_usb_bridge_lan.sh
#   BRIDGE_CLIENT_BASE=https://camera.example.test tools/falsify_usb_bridge_lan.sh
#   BRIDGE_SKIP_CAPTURE=1 tools/falsify_usb_bridge_lan.sh
#   BRIDGE_SETTING_ID=camera.iso BRIDGE_SETTING_VALUE=800 tools/falsify_usb_bridge_lan.sh

out_root="${1:-artifacts/falsification/r2_usb_bridge}"
python_bin="${PYTHON:-python3}"
bridge_host="${BRIDGE_HOST:-127.0.0.1}"
bridge_port="${BRIDGE_PORT:-8766}"
raw_ptp_python="${RAW_PTP_PYTHON:-$python_bin}"
preflight_origin="${BRIDGE_PREFLIGHT_ORIGIN:-http://127.0.0.1:${bridge_port}}"
cors_origin="${BRIDGE_CORS_ORIGIN:-$preflight_origin}"
curl_max_time="${BRIDGE_CURL_MAX_TIME:-75}"
skip_capture="${BRIDGE_SKIP_CAPTURE:-0}"
setting_id="${BRIDGE_SETTING_ID:-camera.iso}"
setting_value="${BRIDGE_SETTING_VALUE:-800}"
execute_setting_write="${BRIDGE_EXECUTE_SETTING_WRITE:-0}"
allow_behavior_probe="${BRIDGE_ALLOW_BEHAVIOR_PROBE:-0}"
api_token="${BRIDGE_API_TOKEN:-}"

if [[ -z "$api_token" ]]; then
  api_token="$("$python_bin" -c 'import secrets; print(secrets.token_urlsafe(32))')"
fi

if (( ${#api_token} < 24 )); then
  echo "BRIDGE_API_TOKEN must be at least 24 characters." >&2
  exit 2
fi

client_base="${BRIDGE_CLIENT_BASE:-http://127.0.0.1:${bridge_port}}"
case "$client_base" in
  http://127.0.0.1:*|http://localhost:*|http://\[::1\]:*|https://*) ;;
  http://*)
    echo "Refusing to send a bridge API token over plaintext LAN HTTP." >&2
    echo "Use an authenticated HTTPS reverse proxy." >&2
    exit 2
    ;;
  *)
    echo "BRIDGE_CLIENT_BASE must begin with http:// or https://." >&2
    exit 2
    ;;
esac

stamp="$(date +%Y%m%d-%H%M%S)"
run_dir="$out_root/${stamp}-lan-bridge-run"
client_dir="$run_dir/client"
server_dir="$run_dir/server"
mkdir -p "$client_dir" "$server_dir"

server_log="$run_dir/bridge-server.log"
summary="$run_dir/summary.md"
http_codes="$client_dir/http-codes.txt"

server_pid=""
cleanup() {
  if [[ -n "$server_pid" ]] && kill -0 "$server_pid" 2>/dev/null; then
    kill "$server_pid" 2>/dev/null || true
    wait "$server_pid" 2>/dev/null || true
  fi
}
trap cleanup EXIT

redact_artifacts() {
  local serial_product
  local serial_short
  local serial_full
  serial_product="$(printf '%s%s' '3282' '7800')"
  serial_short="$(printf '%s%s' '0304' '7364')"
  serial_full="$(printf '0000000000000000%s%s' "$serial_product" "$serial_short")"
  find "$run_dir" -type f \( -name '*.txt' -o -name '*.json' -o -name '*.md' -o -name '*.headers' -o -name '*.log' \) -print0 |
    CAMERA_SERIAL_FULL="$serial_full" \
    CAMERA_SERIAL_SHORT="$serial_short" \
    CAMERA_PRODUCT_CODE="$serial_product" \
    BRIDGE_REDACT_TOKEN="$api_token" \
    xargs -0 perl -0pi -e 's/\r\n/\n/g; s/\Q$ENV{CAMERA_SERIAL_FULL}\E/<REDACTED_CAMERA_SERIAL>/g; s/\Q$ENV{CAMERA_SERIAL_SHORT}\E/<REDACTED_CAMERA_SERIAL_SHORT>/g; s/\Q$ENV{CAMERA_PRODUCT_CODE}\E/<REDACTED_CAMERA_PRODUCT_CODE>/g; s/[ \t]+$//mg; s/\n+\z/\n/' 2>/dev/null || true
  find "$run_dir" -type f \( -name '*.txt' -o -name '*.json' -o -name '*.md' -o -name '*.headers' -o -name '*.log' \) -print0 |
    BRIDGE_REDACT_TOKEN="$api_token" \
    xargs -0 perl -0pi -e 's/\Q$ENV{BRIDGE_REDACT_TOKEN}\E/<REDACTED_BRIDGE_TOKEN>/g' 2>/dev/null || true
}

curl_step() {
  local label="$1"
  local method="$2"
  local path="$3"
  local output="$4"
  local body="${5:-}"
  local url="${client_base}${path}"
  local headers="$client_dir/${label}.headers"
  local meta="$client_dir/${label}.curl-meta.txt"
  local code

  set +e
  if [[ "$method" == "POST" ]]; then
    curl -sS --max-time "$curl_max_time" \
      -H 'Content-Type: application/json' \
      -H "Authorization: Bearer ${api_token}" \
      -D "$headers" \
      -o "$output" \
      -w 'http_code=%{http_code} time_total=%{time_total}\n' \
      -d "$body" \
      "$url" >"$meta" 2>"$client_dir/${label}.curl-stderr.txt"
  elif [[ "$method" == "OPTIONS" ]]; then
    curl -sS --max-time "$curl_max_time" \
      -X OPTIONS \
      -H "Origin: ${preflight_origin}" \
      -H 'Access-Control-Request-Method: POST' \
      -H 'Access-Control-Request-Headers: Authorization, Content-Type' \
      -D "$headers" \
      -o "$output" \
      -w 'http_code=%{http_code} time_total=%{time_total}\n' \
      "$url" >"$meta" 2>"$client_dir/${label}.curl-stderr.txt"
  else
    curl -sS --max-time "$curl_max_time" \
      -H "Authorization: Bearer ${api_token}" \
      -D "$headers" \
      -o "$output" \
      -w 'http_code=%{http_code} time_total=%{time_total}\n' \
      "$url" >"$meta" 2>"$client_dir/${label}.curl-stderr.txt"
  fi
  code=$?
  set -e

  printf '%s curl_exit=%s %s\n' "$label" "$code" "$(tr '\n' ' ' <"$meta")" | tee -a "$http_codes"
}

{
  echo "# USB Bridge LAN Run"
  echo
  echo "Date: $(date)"
  echo
  echo "Configuration:"
  echo
  echo "- bridge_host: \`$bridge_host\`"
  echo "- bridge_port: \`$bridge_port\`"
  echo "- client_base: \`$client_base\`"
  echo "- python: \`$python_bin\`"
  echo "- raw_ptp_python: \`$raw_ptp_python\`"
  echo "- cors_origin: \`$cors_origin\`"
  echo "- preflight_origin: \`$preflight_origin\`"
  echo "- curl_max_time: \`$curl_max_time\`"
  echo "- skip_capture: \`$skip_capture\`"
  echo "- setting_id: \`$setting_id\`"
  echo "- setting_value: \`$setting_value\`"
  echo "- execute_setting_write: \`$execute_setting_write\`"
  echo "- allow_behavior_probe: \`$allow_behavior_probe\`"
  echo "- authentication: \`bearer token (redacted)\`"
  echo "- run_dir: \`$run_dir\`"
  echo
} | tee "$summary"

CAMERA_AUTOPILOT_USB_BRIDGE_TOKEN="$api_token" \
"$python_bin" tools/usb_ptp_bridge_server.py \
  --host "$bridge_host" \
  --port "$bridge_port" \
  --artifact-root "$server_dir" \
  --raw-ptp-python "$raw_ptp_python" \
  --cors-origin "$cors_origin" >"$server_log" 2>&1 &
server_pid="$!"

sleep 1
if ! kill -0 "$server_pid" 2>/dev/null; then
  {
    echo "Bridge server exited before probes."
    echo
    echo "Server log:"
    sed -n '1,160p' "$server_log"
  } | tee -a "$summary"
  redact_artifacts
  exit 1
fi

{
  echo "USB Bridge LAN HTTP codes"
  echo "time=$(date)"
  echo "client_base=$client_base"
  echo
} | tee "$http_codes"

curl_step "preflight" "OPTIONS" "/settings/${setting_id}" "$client_dir/preflight.txt"
curl_step "health" "GET" "/health" "$client_dir/health.json"
curl_step "readiness" "GET" "/ready" "$client_dir/readiness.json"
curl_step "settings" "GET" "/settings" "$client_dir/settings.json"
curl_step "setting-read" "GET" "/settings/${setting_id}" "$client_dir/setting-read.json"
curl_step "setting-dry-run" "POST" "/settings/${setting_id}" "$client_dir/setting-dry-run.json" "{\"value\":\"${setting_value}\",\"dry_run\":true}"
if [[ "$execute_setting_write" == "1" ]]; then
  curl_step "setting-write" "POST" "/settings/${setting_id}" "$client_dir/setting-write.json" "{\"value\":\"${setting_value}\",\"dry_run\":false,\"allow_behavior_probe\":$([[ "$allow_behavior_probe" == "1" ]] && echo true || echo false)}"
else
  {
    echo "setting write skipped"
    echo "Set BRIDGE_EXECUTE_SETTING_WRITE=1 to include POST /settings/${setting_id} with dry_run=false."
    echo "Set BRIDGE_ALLOW_BEHAVIOR_PROBE=1 when intentionally writing an unproven available_not_wrapped node."
  } >"$client_dir/setting-write.skipped.txt"
fi
curl_step "preview" "GET" "/preview.jpg" "$client_dir/preview.jpg"
curl_step "focus" "POST" "/focus/af-point" "$client_dir/focus.json" '{"x":320,"y":420,"focus_area":"0x102"}'

if [[ "$skip_capture" == "1" ]]; then
  {
    echo "capture skipped"
    echo "Set BRIDGE_SKIP_CAPTURE=0 to include POST /capture."
  } >"$client_dir/capture.skipped.txt"
else
  curl_step "capture" "POST" "/capture" "$client_dir/capture.json" '{"download":true}'
fi

file "$client_dir"/* >"$client_dir/file-types.txt" 2>&1 || true
redact_artifacts

{
  echo
  echo "## Result Summary"
  echo
  sed -n '1,80p' "$http_codes"
  echo
  echo "## Interpretation Rules"
  echo
  echo "- PROVEN-YES for robust phone/LAN bridge requires these same endpoints to pass from a separate LAN client or phone after a known-good USB baseline."
  echo "- A same-host LAN address run proves the server is not localhost-only, but does not prove phone ergonomics."
  echo "- HTTP 502/500 plus saved gphoto stderr is state/reliability evidence, not PROVEN-NO unless reproduced across controlled camera states."
  echo
  echo "Artifacts:"
  echo
  echo "- client: \`$client_dir\`"
  echo "- server: \`$server_dir\`"
  echo "- server_log: \`$server_log\`"
} | tee -a "$summary"

echo "$run_dir"
