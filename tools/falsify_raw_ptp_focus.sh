#!/usr/bin/env bash
set -euo pipefail

out_dir="${1:-artifacts/falsification/r2_usb_ptp}"
repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
default_python="$repo_root/.venv/bin/python"
if [[ ! -x "$default_python" ]]; then
  default_python="python3"
fi
python_bin="${PYTHON:-$default_python}"
# These env vars are operator-facing command fragments, so split them the same
# way the existing live-session tooling presents them.
# shellcheck disable=SC2206
wall_args=(${RAW_PTP_WALL:---w1})
extra_args=()
if [[ -n "${RAW_PTP_ARGS:-}" ]]; then
  # shellcheck disable=SC2206
  extra_args=(${RAW_PTP_ARGS})
fi
mkdir -p "$out_dir"

stamp="$(date +%Y%m%d-%H%M%S)"
summary="$out_dir/${stamp}-raw-ptp-focus-summary.txt"

{
  echo "Raw PTP focus falsifier"
  echo "time: $(date)"
  echo "script: tests/probe_raw_control.py"
  echo "python: $python_bin"
  echo "wall_args: ${RAW_PTP_WALL:---w1}"
  echo "extra_args: ${RAW_PTP_ARGS:-<none>}"
  echo "goal: archive three independent raw Sony PTP attempts"
  echo
} | tee "$summary"

for attempt in 1 2 3; do
  log="$out_dir/${stamp}-attempt-${attempt}.log"
  echo "== attempt ${attempt} -> ${log} ==" | tee -a "$summary"
  set +e
  "$python_bin" tests/probe_raw_control.py "${wall_args[@]}" "${extra_args[@]+"${extra_args[@]}"}" >"$log" 2>&1
  code=$?
  set -e
  echo "exit_code=${code}" | tee -a "$summary"
  tail -40 "$log" | sed 's/^/  /' | tee -a "$summary"
  echo | tee -a "$summary"
done

echo "summary: $summary"
