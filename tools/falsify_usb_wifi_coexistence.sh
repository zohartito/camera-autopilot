#!/usr/bin/env bash
set -euo pipefail

# Capture a timestamped USB + Smart Remote WiFi coexistence attempt.
#
# This script is deliberately conservative. It records what is reachable in the current camera state;
# it does not declare protocol support or impossibility by itself.
#
# Usage:
#   tools/falsify_usb_wifi_coexistence.sh
#   WIFI_BASE=http://<camera-lan-ip>:8080/sony tools/falsify_usb_wifi_coexistence.sh
#   RAW_PTP=1 PYTHON=/tmp/pmca-venv/bin/python tools/falsify_usb_wifi_coexistence.sh

out_root="${1:-artifacts/falsification/r1_usb_wifi_coexistence}"
wifi_base="${WIFI_BASE:-http://192.168.122.1:8080/sony}"
python_bin="${PYTHON:-python3}"
raw_ptp="${RAW_PTP:-0}"
raw_ptp_wall="${RAW_PTP_WALL:---w1}"
raw_ptp_args=()
if [[ -n "${RAW_PTP_ARGS:-}" ]]; then
  # shellcheck disable=SC2206
  raw_ptp_args=(${RAW_PTP_ARGS})
fi
stamp="$(date +%Y%m%d-%H%M%S)"
out_dir="$out_root/$stamp"
mkdir -p "$out_dir"

log="$out_dir/coexistence-summary.txt"

run_step() {
  local label="$1"
  shift
  local outfile="$out_dir/${label}.txt"
  local code
  {
    echo "== $label =="
    echo "time: $(date)"
    echo "command: $*"
    echo
    set +e
    "$@"
    code=$?
    set -e
    echo
    echo "exit_code=$code"
  } >"$outfile" 2>&1
  perl -0pi -e 's/[ \t]+$//mg; s/\n+\z/\n/' "$outfile"
  printf '%s -> %s\n' "$label" "$outfile" | tee -a "$log"
}

{
  echo "USB + Smart Remote WiFi coexistence falsifier"
  echo "time: $(date)"
  echo "wifi_base: $wifi_base"
  echo "python: $python_bin"
  echo "raw_ptp: $raw_ptp"
  echo "raw_ptp_wall: $raw_ptp_wall"
  echo "raw_ptp_args: ${RAW_PTP_ARGS:-<none>}"
  echo "out_dir: $out_dir"
  echo
} | tee "$log"
perl -0pi -e 's/[ \t]+$//mg; s/\n+\z/\n/' "$log"

run_step "network-ifconfig" ifconfig
run_step "gphoto-auto-detect" gphoto2 --auto-detect
run_step "gphoto-summary" gphoto2 --summary
run_step "smartremote-api-surface" python3 tools/falsify_smart_remote_api_surface.py \
  --base "$wifi_base" \
  --session-unlock \
  --timeout 0.75 \
  --out-dir "$out_dir/wifi-api-surface"

if [ "$raw_ptp" = "1" ]; then
  run_step "raw-ptp-focus" "$python_bin" tests/probe_raw_control.py "$raw_ptp_wall" "${raw_ptp_args[@]}"
else
  {
    echo "raw PTP probe skipped"
    echo "Set RAW_PTP=1 PYTHON=/tmp/pmca-venv/bin/python to run tests/probe_raw_control.py."
    echo "Optional: RAW_PTP_WALL=--w1 RAW_PTP_ARGS=--no-precondition."
  } >"$out_dir/raw-ptp-focus.txt"
  printf '%s -> %s\n' "raw-ptp-focus" "$out_dir/raw-ptp-focus.txt" | tee -a "$log"
fi

cat <<EOF | tee -a "$log"

Interpretation rules:
- PROVEN-YES requires USB property/blob evidence and Smart Remote JSON action evidence in this same
  timestamped folder.
- PROVEN-NO requires at least three folders showing the same activation-order failure after a passing
  baseline for each surface.
- A timeout or missing USB device in one state is NEEDS-HARDWARE/STATE, not protocol failure.
EOF

echo "$out_dir"
