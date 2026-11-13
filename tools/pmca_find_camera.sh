#!/usr/bin/env bash
set -euo pipefail

# Lightweight PMCA/OpenMemories network finder.
#
# Usage:
#   tools/pmca_find_camera.sh <lan-cidr> 192.168.122.1
#
# This does not prove camera control. It only records which likely PMCA/Smart Remote ports answer:
#   23   OpenMemories Tweak telnetd
#   5555 OpenMemories Tweak adbd
#   8080 Smart Remote / proof-app HTTP candidates

targets=("$@")
if [ "${#targets[@]}" -eq 0 ]; then
  targets=("192.168.122.1")
fi

probe_host() {
  local host="$1"
  local any=0
  for port in 23 5555 8080; do
    if nc -G 1 -z "$host" "$port" >/dev/null 2>&1; then
      printf '%s port %s open\n' "$host" "$port"
      any=1
    fi
  done
  return "$any"
}

expand_target() {
  local target="$1"
  if [[ "$target" =~ ^([0-9]+\.[0-9]+\.[0-9]+)\.0/24$ ]]; then
    local prefix="${BASH_REMATCH[1]}"
    local i
    for i in $(seq 1 254); do
      echo "${prefix}.${i}"
    done
  else
    echo "$target"
  fi
}

echo "PMCA/OpenMemories candidate scan"
echo "time: $(date)"
echo "targets: ${targets[*]}"
echo

for target in "${targets[@]}"; do
  while IFS= read -r host; do
    probe_host "$host" || true
  done < <(expand_target "$target")
done
