#!/usr/bin/env bash
set -euo pipefail

# Archive a repeatable tooling/source inventory for the A7 II route falsification work.
#
# This does not install tools or contact Sony. It records local availability of the
# dev tools named in the falsification brief, plus reference-source directories if
# they have already been cloned locally.

out_root="${1:-artifacts/falsification/tooling}"
stamp="$(date +%Y%m%d-%H%M%S)"
out_dir="$out_root/$stamp"
mkdir -p "$out_dir"

summary="$out_dir/summary.md"
jsonl="$out_dir/tools.jsonl"
: >"$jsonl"

json_escape() {
  python3 -c 'import json,sys; print(json.dumps(sys.stdin.read()))'
}

record() {
  local name="$1"
  local status="$2"
  local evidence="$3"
  local path="${4:-}"
  local version="${5:-}"
  printf '{"name":%s,"status":%s,"evidence":%s,"path":%s,"version":%s}\n' \
    "$(printf '%s' "$name" | json_escape)" \
    "$(printf '%s' "$status" | json_escape)" \
    "$(printf '%s' "$evidence" | json_escape)" \
    "$(printf '%s' "$path" | json_escape)" \
    "$(printf '%s' "$version" | json_escape)" >>"$jsonl"
}

command_version() {
  local cmd="$1"
  shift || true
  if command -v "$cmd" >/dev/null 2>&1; then
    local path
    local version
    local code
    path="$(command -v "$cmd")"
    set +e
    version="$("$cmd" "$@" 2>&1 | head -5)"
    code=$?
    set -e
    if [[ "$code" == "0" ]]; then
      record "$cmd" "PRESENT" "command exists on PATH" "$path" "$version"
    else
      record "$cmd" "ERROR" "command exists but version/probe exited $code" "$path" "$version"
    fi
  else
    record "$cmd" "MISSING" "command not found on PATH" "" ""
  fi
}

python_import() {
  local label="$1"
  local python_bin="$2"
  local module="$3"
  if [[ ! -x "$python_bin" ]]; then
    record "$label" "MISSING" "python binary not executable" "$python_bin" ""
    return
  fi
  local version
  local code
  set +e
  version="$("$python_bin" - "$module" <<'PY' 2>&1
import importlib
import json
import sys

module = sys.argv[1]
mod = importlib.import_module(module)
print(json.dumps({"module": module, "version": getattr(mod, "__version__", None)}))
PY
)"
  code=$?
  set -e
  if [[ "$code" == "0" ]]; then
    record "$label" "PRESENT" "python import succeeded" "$python_bin" "$version"
  else
    record "$label" "MISSING" "python import failed: $version" "$python_bin" ""
  fi
}

path_status() {
  local label="$1"
  local path="$2"
  if [[ -e "$path" ]]; then
    local kind="file"
    local version=""
    [[ -d "$path" ]] && kind="directory"
    if [[ -d "$path/.git" ]]; then
      version="$(git -C "$path" log --oneline -1 2>/dev/null || true)"
    fi
    record "$label" "PRESENT" "$kind exists" "$path" "$version"
  else
    record "$label" "MISSING" "path not found" "$path" ""
  fi
}

command_version adb version
command_version gphoto2 --version
command_version tcpdump --version
command_version tshark --version
command_version dumpcap --version
command_version wireshark --version
command_version jadx --version
command_version java -version
if command -v brew >/dev/null 2>&1 && brew --prefix openjdk >/dev/null 2>&1; then
  command_version "$(brew --prefix openjdk)/bin/java" -version
else
  record "homebrew openjdk java" "MISSING" "brew openjdk prefix not found" "" ""
fi
command_version gradle --version

python_import "pyusb system python" "$(command -v python3)" "usb"
python_import "pyusb pmca venv" "/tmp/pmca-venv/bin/python" "usb"
python_import "python-gphoto2 backend venv" "$PWD/backend/.venv/bin/python" "gphoto2"
python_import "python-gphoto2 ml venv" "$PWD/backend/.venv-ml/bin/python" "gphoto2"

path_status "pmca-console.py" "/tmp/pmca-spike/pmca-console.py"
path_status "OpenMemories-Framework source" "/tmp/OpenMemories-Framework"
path_status "PMCADemo source" "/tmp/PMCADemo"
path_status "alpha-fairy source" "/tmp/alpha-fairy"
path_status "Sony Camera Remote Command package placeholder" "/tmp/CameraRemoteCommand"

if [[ -x "/tmp/pmca-venv/bin/python" && -f "/tmp/pmca-spike/pmca-console.py" ]]; then
  set +e
  /tmp/pmca-venv/bin/python /tmp/pmca-spike/pmca-console.py --help >"$out_dir/pmca-help.txt" 2>&1
  pmca_code=$?
  set -e
  record "sony-pmca-re help" "$([[ "$pmca_code" == "0" ]] && echo PRESENT || echo ERROR)" "pmca-console.py --help exit_code=$pmca_code" "/tmp/pmca-spike/pmca-console.py" ""
else
  record "sony-pmca-re help" "MISSING" "pmca venv or console script missing" "/tmp/pmca-spike/pmca-console.py" ""
fi

{
  echo "# Tooling Inventory"
  echo
  echo "Date: $(date)"
  echo
  echo "This artifact records local tooling availability for the A7 II route falsification goal."
  echo
  echo "| Tool / Source | Status | Evidence | Path | Version |"
  echo "|---|---:|---|---|---|"
  python3 - "$jsonl" <<'PY'
import json
import sys
from pathlib import Path

for line in Path(sys.argv[1]).read_text(encoding="utf-8").splitlines():
    row = json.loads(line)
    def cell(value):
        text = str(value or "").replace("\n", "<br>").replace("|", "\\|")
        return text
    print(
        f"| {cell(row['name'])} | {cell(row['status'])} | {cell(row['evidence'])} | "
        f"{cell(row['path'])} | {cell(row['version'])} |"
    )
PY
} >"$summary"

perl -0pi -e 's/\r\n/\n/g; s/[ \t]+$//mg; s/\n+\z/\n/' "$summary" "$jsonl" "$out_dir"/*.txt 2>/dev/null || true
echo "$out_dir"
