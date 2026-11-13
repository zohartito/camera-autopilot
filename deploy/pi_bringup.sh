#!/usr/bin/env bash
#
# camera-autopilot — Raspberry Pi 5 / CM5 bring-up (stage F preparation).
#
# ONE script that takes a fresh Raspberry Pi OS (64-bit, Bookworm or newer)
# board from bare OS to a running camera-autopilot service with a 20-shot
# no-drop capture proof. Every phase is idempotent and dry-run by default. A
# dry run WRITES NOTHING: it prints each receipt and staged file it would write.
# --apply writes a JSON receipt per phase under --receipt-dir, which must lie
# outside the repo and outside system directories (deploy/safe_paths.py).
# A CM5 is assumed to sit on the Waveshare CM5-NANO-A (deploy/CM5_BRINGUP.md);
# the Pi 5 is the named fallback.
#
# With --apply this script never modifies a file outside the receipt directory,
# the runtime virtualenv, or what deploy/provision.py writes. Privileged host
# changes (apt, udev rules, config.txt, the hotspot profile) are printed as exact
# operator commands and staged into the receipt directory; it never runs sudo.
#
# Usage:
#   bash deploy/pi_bringup.sh --dry-run                 # inspect, run nothing
#   bash deploy/pi_bringup.sh --apply                   # execute host-side work
#   bash deploy/pi_bringup.sh --phase host-facts --dry-run
#   bash deploy/pi_bringup.sh --apply --phase proof \
#        --model "Sony Alpha-A7 III (PC Control)" --port usb:001,014 --serial A7III0SERIAL \
#        --token-file ~/.config/camera-autopilot/api-token
#
set -euo pipefail
# A dry run writes nothing, including Python bytecode caches under deploy/__pycache__.
export PYTHONDONTWRITEBYTECODE=1

# ---- constants ----------------------------------------------------------------
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO="$(cd "$SCRIPT_DIR/.." && pwd)"
PHASES=(host-facts board-config apt udev venv readiness service proof soak)

# apt package -> reason (names for Debian Bookworm/Trixie arm64):
#   gphoto2            provision.py requires the gphoto2 CLI on PATH
#   libgphoto2-6       runtime lib behind the pinned gphoto2 wheel
#   libgl1 libglib2.0-0  runtime libs behind the opencv wheel used by tools
#   ffmpeg             native video muxing for /record and tools
#   libimage-exiftool-perl  exiftool, used by tools/develop_photo & card_history
#   python3-venv python3-pip  create/populate the runtime virtualenv
APT_PACKAGES=(gphoto2 libgphoto2-6 libgl1 libglib2.0-0 ffmpeg libimage-exiftool-perl python3-venv python3-pip)
# caddy is only added when --with-ap requests the AP/local-HTTPS lane.
SUPPORTED_MODELS=("Sony ILCE-7M2 (Control)" "Sony Alpha-A7 III (PC Control)")

# ---- defaults -----------------------------------------------------------------
MODE="dry-run"
ONLY_PHASE=""
RECEIPT_DIR="${HOME}/camera-autopilot-receipts"
MODEL=""
PORT=""
SERIAL=""
HOST="127.0.0.1"
HTTP_PORT="${CAMERA_AUTOPILOT_PORT:-8000}"
TOKEN_FILE="${CAMERA_AUTOPILOT_TOKEN_FILE:-}"
SHOTS=20
ENV_FILE="${HOME}/.config/camera-autopilot/runtime.env"
MANIFEST=""
ARTIFACT_ROOT=""
SERVICE_PID=""
WITH_AP=0
RUNTIME_VENV="$REPO/backend/.venv"
CARRIER="auto"
ALLOW_SYSTEM_DIR=0
# Device-tree model string; overridable only so camera-free tests can present a CM5 / Pi 5.
MODEL_FILE="${CAMERA_AUTOPILOT_MODEL_FILE:-/proc/device-tree/model}"

usage() {
  sed -n '2,26p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'
  cat <<'OPTS'

Options:
  --dry-run            Inspect only; execute nothing side-effecting (default).
  --apply              Execute host-side work for the selected phase(s).
  --phase NAME         Run one phase: host-facts board-config apt udev venv readiness service proof soak
  --receipt-dir DIR    Where JSON receipts are written (default ~/camera-autopilot-receipts).
  --model NAME         Exact Sony body for the proof (see registry).
  --port usb:BBB,DDD   Exact USB port for the proof.
  --serial SERIAL      USB serial of that body; /shoot refuses a capture without it.
  --host ADDR          Service host for the proof (default 127.0.0.1).
  --http-port N        Service HTTP port for the proof (default 8000).
  --token-file PATH    File holding the API token for the proof (mode 0600).
  --shots N            Number of proof captures (default 20).
  --env-file PATH      Private systemd EnvironmentFile for provision.py.
  --manifest PATH      Offline-artifact manifest for the readiness gate.
  --artifact-root DIR  Prepared-package root for the readiness gate.
  --service-pid PID    Service PID for VmHWM sampling (else auto-discovered).
  --with-ap            Include caddy in the apt phase (AP/local-HTTPS lane).
  --carrier NAME       Carrier assumption: auto (default; CM5 => cm5-nano-a), cm5-nano-a, other, none.
  --allow-system-dir   With --apply only: permit a --receipt-dir inside /etc, /boot, /usr, ...
  -h, --help           Show this help and exit.
OPTS
}

# ---- arg parsing --------------------------------------------------------------
while [[ $# -gt 0 ]]; do
  case "$1" in
    --dry-run) MODE="dry-run"; shift ;;
    --apply) MODE="apply"; shift ;;
    --phase) ONLY_PHASE="${2:-}"; shift 2 ;;
    --receipt-dir) RECEIPT_DIR="${2:?}"; shift 2 ;;
    --model) MODEL="${2:-}"; shift 2 ;;
    --port) PORT="${2:-}"; shift 2 ;;
    --serial) SERIAL="${2:-}"; shift 2 ;;
    --host) HOST="${2:?}"; shift 2 ;;
    --http-port) HTTP_PORT="${2:?}"; shift 2 ;;
    --token-file) TOKEN_FILE="${2:?}"; shift 2 ;;
    --shots) SHOTS="${2:?}"; shift 2 ;;
    --env-file) ENV_FILE="${2:?}"; shift 2 ;;
    --manifest) MANIFEST="${2:?}"; shift 2 ;;
    --artifact-root) ARTIFACT_ROOT="${2:?}"; shift 2 ;;
    --service-pid) SERVICE_PID="${2:?}"; shift 2 ;;
    --with-ap) WITH_AP=1; shift ;;
    --carrier) CARRIER="${2:?}"; shift 2 ;;
    --allow-system-dir) ALLOW_SYSTEM_DIR=1; shift ;;
    -h|--help) usage; exit 0 ;;
    *) echo "Unknown argument: $1" >&2; usage >&2; exit 2 ;;
  esac
done

if [[ -n "$ONLY_PHASE" ]]; then
  found=0; for p in "${PHASES[@]}"; do [[ "$p" == "$ONLY_PHASE" ]] && found=1; done
  [[ "$found" == 1 ]] || { echo "Unknown --phase: $ONLY_PHASE" >&2; exit 2; }
fi
if [[ "$WITH_AP" == 1 ]]; then APT_PACKAGES+=(caddy); fi
case "$CARRIER" in auto|cm5-nano-a|other|none) ;; *) echo "Unknown --carrier: $CARRIER" >&2; exit 2 ;; esac

[[ "$SHOTS" =~ ^[1-9][0-9]*$ ]] || { echo "--shots must be a positive integer" >&2; exit 2; }
# A control character (newline included) in any path is refused: "/boot<newline>" would be
# validated as one path and, once command substitution strips the newline, written as another.
for path_value in "$RECEIPT_DIR" "$TOKEN_FILE" "$ENV_FILE" "$MANIFEST" "$ARTIFACT_ROOT" "$MODEL_FILE"; do
  if [[ "$path_value" =~ [[:cntrl:]] ]]; then
    echo "refusing a path argument containing a control character" >&2; exit 2
  fi
done
# The serial keeps its exact value (Linux discovery preserves the sysfs bytes and identity compares
# them exactly); the /shoot body is built by a JSON encoder, so only control characters are refused.
if [[ -n "$SERIAL" ]] && ! python3 -c 'import sys
sys.exit(1 if any(ord(c) < 0x20 or ord(c) == 0x7F for c in sys.argv[1]) else 0)' "$SERIAL"; then
  echo "--serial must not contain control characters" >&2; exit 2
fi
if [[ "$ALLOW_SYSTEM_DIR" == 1 && "$MODE" != "apply" ]]; then
  echo "--allow-system-dir needs --apply" >&2; exit 2
fi
# Refuse a receipt dir inside the repo (staged credentials could be committed) or inside a
# system directory, in BOTH modes so a dry run warns before an apply would write there.
# The checker prints the resolved path (~ expanded, realpath); that exact path is used for every
# write below, so a quoted "~/x" can never be checked as $HOME/x and written as ./~/x.
SAFE_ARGS=(--repo "$REPO" --print-resolved); [[ "$ALLOW_SYSTEM_DIR" == 1 ]] && SAFE_ARGS+=(--allow-system-dir)
RECEIPT_DIR="$(python3 "$REPO/deploy/safe_paths.py" "${SAFE_ARGS[@]}" "$RECEIPT_DIR")" || exit 2

if [[ "$MODE" == "apply" ]]; then
  # Mode 0700 only on a directory this run creates; never chmod an existing one.
  [[ -d "$RECEIPT_DIR" ]] || mkdir -p -m 700 "$RECEIPT_DIR"
  # Receipts are replaced by rename and never written through a link, but the shell redirects
  # below (udev rule, proof log) would follow one: refuse any symlink planted in the receipt dir.
  planted="$(find "$RECEIPT_DIR" -maxdepth 2 -type l 2>/dev/null | head -5 || true)"
  if [[ -n "$planted" ]]; then
    echo "refusing $RECEIPT_DIR: it contains symlinks, which a write could follow:" >&2
    printf '  %s\n' $planted >&2
    exit 2
  fi
fi

# ---- helpers ------------------------------------------------------------------
log() { printf '  %s\n' "$*"; }
phase_banner() { printf '\n=== phase: %-11s [%s] ===\n' "$1" "$MODE"; }

# Emit a receipt from simple key=value pairs. Each value is parsed as JSON when
# possible (numbers/bools/null/arrays/objects), otherwise kept as a string.
# Dry run: print "WOULD-WRITE <path>" and one "RECEIPT <phase> <json>" line; write nothing.
emit_receipt() {
  local phase="$1"; shift
  local out="$RECEIPT_DIR/$phase.json"
  RECEIPT_PHASE="$phase" RECEIPT_MODE="$MODE" python3 - "$out" "$@" <<'PY'
import json, os, sys, time
out = sys.argv[1]
data = {"phase": os.environ["RECEIPT_PHASE"], "mode": os.environ["RECEIPT_MODE"]}
for pair in sys.argv[2:]:
    key, _, value = pair.partition("=")
    try:
        data[key] = json.loads(value)
    except Exception:
        data[key] = value
data["written_at_unix"] = time.time()
if data["mode"] == "dry-run":
    print("  WOULD-WRITE:", out)
    print("  RECEIPT", data["phase"], json.dumps(data, sort_keys=True))
else:
    import tempfile
    if os.path.islink(out):
        sys.exit(f"refusing to write {out}: it is a symlink")
    fd, tmp = tempfile.mkstemp(prefix="." + os.path.basename(out) + ".", dir=os.path.dirname(out))
    with os.fdopen(fd, "w") as fh:
        fh.write(json.dumps(data, indent=2) + "\n")
    os.replace(tmp, out)  # replaces the entry; never writes through a link or a hard link
    print("  receipt:", out)
PY
}

read_thermal_json() {
  # Print a JSON array of thermal zones from /sys; empty array off-Linux.
  python3 - <<'PY'
import glob, json, os
zones = []
for temp in sorted(glob.glob("/sys/class/thermal/thermal_zone*/temp")):
    zone = os.path.dirname(temp)
    try:
        milli = int(open(temp).read().strip())
    except Exception:
        continue
    try:
        kind = open(os.path.join(zone, "type")).read().strip()
    except Exception:
        kind = os.path.basename(zone)
    zones.append({"name": kind, "celsius": round(milli / 1000.0, 2)})
print(json.dumps(zones))
PY
}

vmhwm_kib() {
  # Peak resident set (VmHWM) in KiB for a pid, or null.
  local pid="$1"
  if [[ -n "$pid" && -r "/proc/$pid/status" ]]; then
    awk '/^VmHWM:/{print $2}' "/proc/$pid/status" 2>/dev/null || echo ""
  else
    echo ""
  fi
}

discover_service_pid() {
  [[ -n "$SERVICE_PID" ]] && { echo "$SERVICE_PID"; return; }
  local pid=""
  if command -v ss >/dev/null 2>&1; then
    pid="$(ss -ltnpH "sport = :$HTTP_PORT" 2>/dev/null | grep -oE 'pid=[0-9]+' | head -1 | cut -d= -f2)"
  fi
  if [[ -z "$pid" ]] && command -v pgrep >/dev/null 2>&1; then
    pid="$(pgrep -f 'uvicorn app:app' 2>/dev/null | head -1 || true)"
  fi
  echo "$pid"
}

# ---- phase 1: host facts (read-only; runs even in dry-run) ---------------------
phase_host_facts() {
  phase_banner host-facts
  local model os_pretty kernel arch memtotal_kib free_kib throttled thermal
  model="$( { cat "$MODEL_FILE" 2>/dev/null | tr -d '\0'; } || true)"
  os_pretty="$( . /etc/os-release 2>/dev/null; echo "${PRETTY_NAME:-unknown}")"
  kernel="$(uname -srm 2>/dev/null || echo unknown)"
  arch="$(uname -m 2>/dev/null || echo unknown)"
  memtotal_kib="$(awk '/^MemTotal:/{print $2}' /proc/meminfo 2>/dev/null || echo "")"
  free_kib="$(df -Pk "$REPO" 2>/dev/null | awk 'NR==2{print $4}' || echo "")"
  throttled="$(command -v vcgencmd >/dev/null 2>&1 && vcgencmd get_throttled 2>/dev/null | cut -d= -f2 || echo "")"
  thermal="$(read_thermal_json)"
  # Board class + carrier assumption (CM5 => Waveshare CM5-NANO-A). Read-only, stdlib python.
  local board_json usb_max_current root_device
  board_json="$(python3 "$REPO/deploy/cm5_nano.py" facts --model-text "$model" --carrier "$CARRIER" 2>/dev/null || echo '{}')"
  # Read-only firmware query; board-config deliberately does not set it.
  usb_max_current="$(command -v vcgencmd >/dev/null 2>&1 && vcgencmd get_config usb_max_current_enable 2>/dev/null | cut -d= -f2 || echo "")"
  root_device="$(command -v findmnt >/dev/null 2>&1 && findmnt -no SOURCE / 2>/dev/null || echo "")"

  local is_arm64=false; [[ "$arch" == "aarch64" ]] && is_arm64=true
  local is_pi=false; [[ "$model" == *"Raspberry Pi"* ]] && is_pi=true
  # Bookworm=12, Trixie=13 or newer.
  local ver_id supported_os=false
  ver_id="$( . /etc/os-release 2>/dev/null; echo "${VERSION_ID:-0}")"
  if [[ "$os_pretty" == *"Raspberry Pi OS"* || "$os_pretty" == *"Debian"* ]]; then
    [[ "${ver_id%%.*}" =~ ^[0-9]+$ ]] && (( ${ver_id%%.*} >= 12 )) && supported_os=true
  fi

  log "model:    ${model:-unknown}"
  log "board:    $(printf '%s' "$board_json" | python3 -c 'import json,sys;d=json.load(sys.stdin);print(d.get("board","?"), "carrier(assumed)=" + str(d.get("carrier","?")))' 2>/dev/null || echo unknown)"
  log "os:       $os_pretty"
  log "kernel:   $kernel  (arch $arch, arm64=$is_arm64)"
  log "memory:   ${memtotal_kib:-?} KiB total"
  log "free:     ${free_kib:-?} KiB on $REPO"
  [[ -n "$throttled" ]] && log "throttled: $throttled (0x0 = no under-voltage/throttle flags)"

  RECEIPT_PHASE=host-facts RECEIPT_MODE="$MODE" \
  F_MODEL="$model" F_OS="$os_pretty" F_KERNEL="$kernel" F_ARCH="$arch" \
  F_MEM="$memtotal_kib" F_FREE="$free_kib" F_THROTTLED="$throttled" \
  F_ARM64="$is_arm64" F_PI="$is_pi" F_OSOK="$supported_os" F_THERMAL="$thermal" \
  F_REPO="$REPO" F_BOARD="$board_json" F_USBMAX="$usb_max_current" F_ROOTDEV="$root_device" \
  python3 - "$RECEIPT_DIR/host-facts.json" <<'PY'
import json, os, sys, time
env = os.environ
def num(key):
    v = env.get(key, "")
    return int(v) if v.strip().isdigit() else None
data = {
    "phase": "host-facts", "mode": env["RECEIPT_MODE"], "executed": True,
    "model": env.get("F_MODEL") or None,
    "os_pretty_name": env.get("F_OS") or None,
    "kernel": env.get("F_KERNEL") or None,
    "arch": env.get("F_ARCH") or None,
    "is_arm64": env.get("F_ARM64") == "true",
    "is_raspberry_pi": env.get("F_PI") == "true",
    "os_supported": env.get("F_OSOK") == "true",
    "mem_total_kib": num("F_MEM"),
    "free_disk_kib": num("F_FREE"),
    "repo": env.get("F_REPO"),
    "power_throttled_raw": env.get("F_THROTTLED") or None,
    "power_warning": bool(env.get("F_THROTTLED")) and env.get("F_THROTTLED") not in ("0x0", "0"),
    "thermal_zones": json.loads(env.get("F_THERMAL") or "[]"),
    "board": json.loads(env.get("F_BOARD") or "{}"),
    "usb_max_current_enable_raw": env.get("F_USBMAX") or None,
    "root_device": env.get("F_ROOTDEV") or None,
    "warnings": [],
    "written_at_unix": time.time(),
}
if not data["is_arm64"]:
    data["warnings"].append("architecture is not aarch64")
if not data["is_raspberry_pi"]:
    data["warnings"].append("model is not a Raspberry Pi (host-facts still recorded)")
if not data["os_supported"]:
    data["warnings"].append("OS is not confirmed Bookworm(12)+ / Raspberry Pi OS")
if data["power_warning"]:
    data["warnings"].append("vcgencmd reports throttle/under-voltage flags set")
if data["board"].get("carrier") == "cm5-nano-a":
    data["warnings"].append("assumed CM5-NANO-A carrier: its single USB-A port (2 A) belongs to the camera; "
                            "wlan0 is the only network link")
if data["mode"] == "dry-run":
    print("  WOULD-WRITE:", sys.argv[1])
    print("  RECEIPT host-facts", json.dumps(data, sort_keys=True))
else:
    import tempfile
    out = sys.argv[1]
    if os.path.islink(out):
        sys.exit(f"refusing to write {out}: it is a symlink")
    fd, tmp = tempfile.mkstemp(prefix="." + os.path.basename(out) + ".", dir=os.path.dirname(out))
    with os.fdopen(fd, "w") as fh:
        fh.write(json.dumps(data, indent=2) + "\n")
    os.replace(tmp, out)  # replaces the entry; never writes through a link or a hard link
    print("  receipt:", out)
PY
}

# ---- phase 1b: CM5-NANO-A config.txt fragment + onboard-Wi-Fi hotspot (staged) --
phase_board_config() {
  phase_banner board-config
  # Never touches /boot or /etc and never brings a connection up. A dry run prints the
  # fragment and writes nothing (no passphrase is generated to disk). --apply stages both files,
  # mode 0600, in the receipt dir (already refused if inside the repo). An existing staged dir is
  # kept so a re-run never rotates the hotspot passphrase; delete it to regenerate.
  local staged="$RECEIPT_DIR/board-config" model board
  model="$( { cat "$MODEL_FILE" 2>/dev/null | tr -d '\0'; } || true)"
  board="$(python3 "$REPO/deploy/cm5_nano.py" facts --model-text "$model" --carrier "$CARRIER" 2>/dev/null \
           | python3 -c 'import json,sys;print(json.load(sys.stdin).get("board","unknown"))' 2>/dev/null || echo unknown)"
  local applies=false; [[ "$board" == "cm5" ]] && applies=true
  local created=false
  if [[ "$MODE" == "dry-run" ]]; then
    log "WOULD-WRITE: $staged/config.txt.cm5-nano-a (below) and $staged/camera-puck.nmconnection (new passphrase, 0600)"
    python3 "$REPO/deploy/cm5_nano.py" render | sed 's/^/    | /'
    local preview_rc="${PIPESTATUS[0]}"
    if [[ "$preview_rc" != 0 ]]; then
      log "ERROR: rendering the board config preview failed (exit $preview_rc)"
      emit_receipt board-config executed=false staged_dir="$staged" created_now=false \
        board="$board" render_exit_code="$preview_rc" error="config render failed"
      return 1
    fi
    emit_receipt board-config executed=false staged_dir="$staged" created_now=false \
      board="$board" applies_to_board="$applies" requires_operator=true applied_by_script=false \
      files='["config.txt.cm5-nano-a","camera-puck.nmconnection"]' credentials_printed=false
    return
  fi
  if [[ -e "$staged" || -L "$staged" ]]; then
    # Only a complete staging (STAGED.json manifest, every hash matching) counts; a partial or
    # altered one is reported, never passed off as staged, and never silently replaced.
    local verify_err
    if ! verify_err="$(python3 "$REPO/deploy/cm5_nano.py" verify --dir "$staged" 2>&1)"; then
      log "ERROR: $verify_err"
      log "       delete $staged and re-run this phase to stage it again"
      emit_receipt board-config executed=false staged_dir="$staged" created_now=false \
        board="$board" applies_to_board="$applies" error="existing staged dir is incomplete" \
        credentials_printed=false
      return 1
    fi
    log "kept existing $staged (complete per its manifest; the passphrase is not rotated)"
  else
    local render_args=(render --apply --out "$staged")
    [[ "$ALLOW_SYSTEM_DIR" == 1 ]] && render_args+=(--allow-system-dir)
    local render_rc=0
    python3 "$REPO/deploy/cm5_nano.py" "${render_args[@]}" >/dev/null || render_rc=$?
    if [[ "$render_rc" != 0 ]]; then
      log "ERROR: rendering the board config failed (exit $render_rc); nothing staged"
      emit_receipt board-config executed=false staged_dir="$staged" created_now=false \
        board="$board" applies_to_board="$applies" render_exit_code="$render_rc" \
        error="config render failed" credentials_printed=false
      return 1
    fi
    created=true
  fi
  log "staged: $staged/config.txt.cm5-nano-a   (I2C on, Waveshare fan curve; [cm5]-scoped)"
  log "staged: $staged/camera-puck.nmconnection (2.4 GHz ch 6, WPA2-PSK, wlan0 only; mode 0600)"
  [[ "$applies" == true ]] || log "NOTE: this board is '$board', not a CM5; the files are for review only"
  log "operator step 1: sudo cp /boot/firmware/config.txt /boot/firmware/config.txt.bak"
  log "                 cat \"$staged/config.txt.cm5-nano-a\" | sudo tee -a /boot/firmware/config.txt >/dev/null && sudo reboot"
  log "operator step 2: sudo install -m 600 \"$staged/camera-puck.nmconnection\" /etc/NetworkManager/system-connections/"
  log "                 sudo nmcli connection reload   # loads it; autoconnect=false keeps client Wi-Fi up"
  log "WARNING: bringing camera-puck up drops the client Wi-Fi that carries SSH (the Nano-A has no"
  log "         Ethernet). Run deploy/day_one.py ap-soak detached; see deploy/CM5_BRINGUP.md."
  emit_receipt board-config executed=false staged_dir="$staged" created_now="$created" \
    board="$board" applies_to_board="$applies" requires_operator=true applied_by_script=false \
    files='["config.txt.cm5-nano-a","camera-puck.nmconnection"]' credentials_printed=false
}

# ---- phase 2: apt packages -----------------------------------------------------
phase_apt() {
  phase_banner apt
  local cmd="sudo apt-get update && sudo apt-get install -y ${APT_PACKAGES[*]}"
  # Read-only presence check (dpkg-query is not apt-get; safe in dry-run).
  local present=() missing=()
  if command -v dpkg-query >/dev/null 2>&1; then
    for pkg in "${APT_PACKAGES[@]}"; do
      if dpkg-query -W -f='${Status}' "$pkg" 2>/dev/null | grep -q "install ok installed"; then
        present+=("$pkg"); else missing+=("$pkg"); fi
    done
  fi
  local pkgs_json; pkgs_json="$(printf '%s\n' "${APT_PACKAGES[@]}" | python3 -c 'import json,sys;print(json.dumps([l.strip() for l in sys.stdin if l.strip()]))')"
  local present_json missing_json
  present_json="$(printf '%s\n' "${present[@]:-}" | python3 -c 'import json,sys;print(json.dumps([l.strip() for l in sys.stdin if l.strip()]))')"
  missing_json="$(printf '%s\n' "${missing[@]:-}" | python3 -c 'import json,sys;print(json.dumps([l.strip() for l in sys.stdin if l.strip()]))')"

  if [[ "$MODE" == "dry-run" ]]; then
    log "WOULD-RUN: $cmd"
    emit_receipt apt executed=false command="$cmd" packages="$pkgs_json" \
      already_installed="$present_json" missing="$missing_json"
    return
  fi
  # System packages need root. This script never runs sudo: it prints the exact command and
  # records the gap, exactly like the udev phase, so the operator stays in control of /.
  log "OPERATOR STEP (run yourself, then re-run this phase): $cmd"
  emit_receipt apt executed=false operator_step=true command="$cmd" packages="$pkgs_json" \
    already_installed="$present_json" missing="$missing_json"
  if [[ -n "${missing[*]:-}" ]]; then
    log "apt: ${#missing[@]} package(s) still missing; later phases may fail until installed"
  fi
}

# ---- phase 3: udev / USB ownership (advisory; never edits /etc) -----------------
phase_udev() {
  phase_banner udev
  # Sony vendor id 054c. On Raspberry Pi OS Desktop, gvfs-gphoto2 auto-mounts the
  # camera and steals the PTP device from gphoto2 (README: "use its normal USB
  # permissions; do not run as root; only one camera-owning process at a time").
  # We STAGE a proposed rule and print operator commands; we do not write /etc.
  local rule_file="$RECEIPT_DIR/99-camera-autopilot-sony.rules"
  local user; user="$(id -un)"
  local rule
  rule="$(cat <<RULE
# camera-autopilot: give the service user access to Sony PTP bodies (idVendor 054c).
# Install with: sudo cp "$rule_file" /etc/udev/rules.d/ && sudo udevadm control --reload
SUBSYSTEM=="usb", ATTR{idVendor}=="054c", MODE="0660", GROUP="plugdev", TAG+="uaccess"
RULE
)"
  if [[ "$MODE" == "dry-run" ]]; then
    log "WOULD-WRITE: $rule_file"
    printf '%s\n' "$rule" | sed 's/^/    | /'
  else
    ( umask 077; printf '%s\n' "$rule" > "$rule_file" )
  fi

  local gvfs_present=false
  command -v gvfs-mount >/dev/null 2>&1 && gvfs_present=true
  [[ -e /usr/lib/gvfs/gvfsd-gphoto2 || -e /usr/libexec/gvfsd-gphoto2 ]] && gvfs_present=true

  log "staged udev rule: $rule_file"
  log "operator step 1: sudo usermod -aG plugdev $user   # then re-login"
  log "operator step 2: sudo cp \"$rule_file\" /etc/udev/rules.d/ && sudo udevadm control --reload && sudo udevadm trigger"
  if [[ "$gvfs_present" == true ]]; then
    log "operator step 3: mask the auto-mounter so gphoto2 keeps exclusive ownership:"
    log "                 systemctl --user mask gvfs-gphoto2-volume-monitor.service  (or remove gvfs-backends)"
  fi
  # This phase is advisory in BOTH modes: it never runs sudo/systemctl itself.
  emit_receipt udev executed=false staged_rule="$rule_file" \
    service_user="$user" gvfs_automount_present="$gvfs_present" \
    requires_operator=true applied_by_script=false
}

# ---- phase 4: runtime virtualenv + taste validation ----------------------------
phase_venv() {
  phase_banner venv
  local py="$RUNTIME_VENV/bin/python"
  local create_cmd="python3 -m venv $RUNTIME_VENV"
  local pip_cmd="$py -m pip install -r $REPO/requirements.txt"
  local taste_cmd="$py $REPO/tools/prepare_taste_runtime.py --offline"

  if [[ "$MODE" == "dry-run" ]]; then
    log "WOULD-RUN: $create_cmd"
    log "WOULD-RUN: $pip_cmd"
    log "WOULD-RUN: $taste_cmd   # validates pinned model bytes, never downloads"
    emit_receipt venv executed=false venv="$RUNTIME_VENV" \
      create_command="$create_cmd" pip_command="$pip_cmd" taste_command="$taste_cmd"
    return
  fi
  if [[ ! -x "$py" ]]; then
    log "RUN: $create_cmd"
    python3 -m venv "$RUNTIME_VENV"
  else
    log "venv already present at $RUNTIME_VENV (idempotent)"
  fi
  log "RUN: $pip_cmd"
  "$py" -m pip install --upgrade pip >/dev/null
  "$py" -m pip install -r "$REPO/requirements.txt"
  # Taste validation is advisory: physics fallback exists when CLIP bytes are absent.
  local taste_ok=false taste_note=""
  if "$py" "$REPO/tools/prepare_taste_runtime.py" --offline >/dev/null 2>&1; then
    taste_ok=true
  else
    taste_note="pinned CLIP weights not validated offline; physics fallback remains (non-blocking)"
    log "NOTE: $taste_note"
  fi
  emit_receipt venv executed=true venv="$RUNTIME_VENV" \
    pip_command="$pip_cmd" taste_offline_ok="$taste_ok" taste_note="$taste_note"
}

# ---- phase 5: host readiness gate ----------------------------------------------
phase_readiness() {
  phase_banner readiness
  local py="$RUNTIME_VENV/bin/python"; [[ -x "$py" ]] || py="python3"
  local storage="$REPO/backend/shots"
  local ready_cmd="$py -m deploy.host_readiness --storage $storage --token-file ${TOKEN_FILE:-<token-file>} --manifest ${MANIFEST:-<manifest>} --artifact-root ${ARTIFACT_ROOT:-<artifact-root>}"
  local preflight_cmd="$py tools/deployment_preflight.py"

  if [[ "$MODE" == "dry-run" ]]; then
    log "WOULD-RUN: $preflight_cmd   # read-only config/file presence"
    log "WOULD-RUN: $ready_cmd       # offline-artifact + free-space + token gate"
    emit_receipt readiness executed=false host_readiness_command="$ready_cmd" \
      preflight_command="$preflight_cmd"
    return
  fi
  # deployment_preflight is always runnable (read-only, no offline package needed).
  log "RUN: $preflight_cmd"
  ( cd "$REPO" && "$py" tools/deployment_preflight.py --output "$RECEIPT_DIR/preflight.json" ) || true
  local gate_ran=false gate_ok=false blocker=""
  if [[ -n "$MANIFEST" && -n "$ARTIFACT_ROOT" && -n "$TOKEN_FILE" ]]; then
    log "RUN: $ready_cmd"
    if ( cd "$REPO" && "$py" -m deploy.host_readiness --storage "$storage" \
          --token-file "$TOKEN_FILE" --manifest "$MANIFEST" --artifact-root "$ARTIFACT_ROOT" \
          > "$RECEIPT_DIR/host-readiness-raw.json" ); then
      gate_ran=true; gate_ok=true
    else
      gate_ran=true; gate_ok=false
    fi
  else
    blocker="host_readiness gate skipped: provide --manifest, --artifact-root and --token-file (needs the prepared offline package)"
    log "SKIP: $blocker"
  fi
  emit_receipt readiness executed=true preflight="$RECEIPT_DIR/preflight.json" \
    host_readiness_ran="$gate_ran" host_readiness_ok="$gate_ok" blocker="$blocker"
}

# ---- phase 6: systemd unit via provision.py ------------------------------------
phase_service() {
  phase_banner service
  local rendered="$RECEIPT_DIR/camera-autopilot.service"
  local render_cmd="python3 deploy/provision.py --repo $REPO --env-file $ENV_FILE --output $rendered"
  local install_cmd="python3 deploy/provision.py --repo $REPO --env-file $ENV_FILE --install"

  if [[ "$MODE" == "dry-run" ]]; then
    log "WOULD-RUN: $render_cmd     # renders a reviewable unit, installs nothing"
    log "WOULD-RUN: $install_cmd    # provision.py --install (still does not start)"
    emit_receipt service executed=false render_command="$render_cmd" install_command="$install_cmd"
    return
  fi
  # Render first (read-only), then install per provision.py's own gated contract.
  log "RUN: $render_cmd"
  ( cd "$REPO" && python3 deploy/provision.py --repo "$REPO" --env-file "$ENV_FILE" --output "$rendered" > "$RECEIPT_DIR/provision-render.json" ) || true
  local target="$HOME/.config/systemd/user/camera-autopilot.service"
  local installed=false note=""
  if [[ -e "$target" ]]; then
    installed=true; note="unit already installed; provision.py preserves it (idempotent)"
    log "NOTE: $note"
  else
    log "RUN: $install_cmd"
    if ( cd "$REPO" && python3 deploy/provision.py --repo "$REPO" --env-file "$ENV_FILE" --install > "$RECEIPT_DIR/provision-install.json" ); then
      installed=true
    else
      note="provision.py reported blockers; see provision-install.json (env file / gphoto2 / venv)"
      log "NOTE: $note"
    fi
  fi
  log "Start it yourself once camera ownership is clear: systemctl --user start camera-autopilot"
  emit_receipt service executed=true rendered_unit="$rendered" \
    install_target="$target" installed="$installed" started=false note="$note"
}

# ---- phase 7: 20-shot no-drop proof --------------------------------------------
phase_proof() {
  phase_banner proof
  # /shoot refuses a capture whose request names a model and port but no serial (serial_required).
  # A JSON encoder builds the body, so no model, port or serial value can change its structure.
  local body; body="$(python3 -c 'import json, sys
print(json.dumps({"camera_model": sys.argv[1], "camera_port": sys.argv[2], "camera_serial": sys.argv[3]},
                 separators=(",", ":")))' "$MODEL" "$PORT" "$SERIAL")"
  local quoted_body; quoted_body="$(python3 -c 'import shlex, sys; print(shlex.quote(sys.argv[1]))' "$body")"
  local url="http://$HOST:$HTTP_PORT/shoot"
  local curl_cmd="curl -sS -m 30 -H 'X-Camera-Autopilot-Token: <token>' -H 'Content-Type: application/json' -X POST '$url' --data $quoted_body"

  if [[ "$MODE" == "dry-run" ]]; then
    log "WOULD-RUN: loop $SHOTS x  $curl_cmd"
    log "           pass only if $SHOTS/$SHOTS return 200 with settings_readback.after_final=verified"
    log "           and RAW transfer: $(PYTHONDONTWRITEBYTECODE=1 python3 "$REPO/deploy/raw_proof.py" criterion "$MODEL")"
    emit_receipt proof executed=false shots="$SHOTS" url="$url" \
      request_body="$body" curl_command="$curl_cmd" \
      model="${MODEL:-<unset>}" port="${PORT:-<unset>}" serial="${SERIAL:-<unset>}"
    return
  fi

  # ---- apply: run the real loop ----
  [[ -n "$MODEL" && -n "$PORT" && -n "$SERIAL" ]] || { echo "proof --apply needs --model, --port and --serial" >&2; exit 2; }
  [[ -n "$TOKEN_FILE" && -r "$TOKEN_FILE" ]] || { echo "proof --apply needs a readable --token-file" >&2; exit 2; }
  local ok_model=false; for m in "${SUPPORTED_MODELS[@]}"; do [[ "$m" == "$MODEL" ]] && ok_model=true; done
  [[ "$ok_model" == true ]] || log "WARNING: --model is not in the supported registry; proceeding as requested"
  local token; token="$(tr -d '\n' < "$TOKEN_FILE")"
  local pid; pid="$(discover_service_pid)"
  local thermal_before; thermal_before="$(read_thermal_json)"
  local throttled_before; throttled_before="$(command -v vcgencmd >/dev/null 2>&1 && vcgencmd get_throttled 2>/dev/null | cut -d= -f2 || echo "")"

  local jsonl="$RECEIPT_DIR/proof-shots.jsonl"; : > "$jsonl"
  local peak_vmhwm=""
  local i
  for (( i=1; i<=SHOTS; i++ )); do
    local rbody; rbody="$(mktemp "${TMPDIR:-/tmp}/pi-proof.XXXXXX")"
    local meta
    meta="$(curl -sS -m 30 -o "$rbody" -w '%{http_code} %{time_total}' \
      -H "X-Camera-Autopilot-Token: $token" -H "Content-Type: application/json" \
      -X POST "$url" --data "$body" 2>/dev/null || echo '000 0')"
    local code="${meta%% *}"; local elapsed="${meta##* }"
    local sample; sample="$(vmhwm_kib "$pid")"
    if [[ -n "$sample" ]]; then
      if [[ -z "$peak_vmhwm" ]] || (( sample > peak_vmhwm )); then peak_vmhwm="$sample"; fi
    fi
    # Parse the response for image + settings_readback.after_final (searched recursively).
    IDX="$i" CODE="$code" ELAPSED="$elapsed" VMHWM="${sample:-}" DEPLOY_DIR="$REPO/deploy" PYTHONDONTWRITEBYTECODE=1 \
    python3 - "$rbody" >> "$jsonl" <<'PY'
import json, os, sys
env = os.environ
sys.path.insert(0, env["DEPLOY_DIR"])
import raw_proof
row = {"index": int(env["IDX"]), "http_status": int(env["CODE"]),
       "elapsed_s": float(env["ELAPSED"]), "vmhwm_kib": int(env["VMHWM"]) if env.get("VMHWM") else None,
       "image": "", "after_final": None, "retry_capture": None, "may_have_fired": None,
       "camera_serial": None, "files": None}
try:
    payload = json.load(open(sys.argv[1]))
except Exception:
    payload = None
def find_readback(node):
    if isinstance(node, dict):
        if "settings_readback" in node and isinstance(node["settings_readback"], dict):
            return node["settings_readback"]
        for v in node.values():
            r = find_readback(v)
            if r is not None:
                return r
    elif isinstance(node, list):
        for v in node:
            r = find_readback(v)
            if r is not None:
                return r
    return None
if isinstance(payload, dict):
    row["image"] = payload.get("image", "") or ""
    row["retry_capture"] = payload.get("retry_capture")
    row["may_have_fired"] = payload.get("may_have_fired")
    # The body that answered: the serial the shot's evidence verified at the port, read from that one
    # place so no echoed request field can stand in for it.
    evidence = payload.get("evidence")
    serial = evidence.get("camera_serial") if isinstance(evidence, dict) else None
    row["camera_serial"] = serial if isinstance(serial, str) else None
    # Name, size and SHA-256 of each file the camera delivered for the final capture, hashed by the
    # service on this machine from the bytes as stored (deploy/raw_proof.py).
    row["files"] = raw_proof.shot_files(payload)
    rb = find_readback(payload)
    if isinstance(rb, dict):
        row["after_final"] = rb.get("after_final")
print(json.dumps(row))
PY
    rm -f "$rbody"
    local last; last="$(tail -1 "$jsonl")"
    log "shot $i/$SHOTS: $(echo "$last" | PYTHONDONTWRITEBYTECODE=1 python3 "$REPO/deploy/raw_proof.py" shot-line)"
  done

  local thermal_after; thermal_after="$(read_thermal_json)"
  local throttled_after; throttled_after="$(command -v vcgencmd >/dev/null 2>&1 && vcgencmd get_throttled 2>/dev/null | cut -d= -f2 || echo "")"

  # Summarize + verdict.
  SHOTS="$SHOTS" PID="${pid:-}" PEAK="${peak_vmhwm:-}" URL="$url" MODEL="$MODEL" PORT="$PORT" SERIAL="$SERIAL" \
  T_BEFORE="$thermal_before" T_AFTER="$thermal_after" \
  TH_BEFORE="$throttled_before" TH_AFTER="$throttled_after" DEPLOY_DIR="$REPO/deploy" PYTHONDONTWRITEBYTECODE=1 \
  python3 - "$jsonl" "$RECEIPT_DIR/proof.json" <<'PY'
import json, os, sys, time
env = os.environ
sys.path.insert(0, env["DEPLOY_DIR"])
import raw_proof
rows = [json.loads(l) for l in open(sys.argv[1]) if l.strip()]
expected = int(env["SHOTS"])
ok_200 = sum(1 for r in rows if r["http_status"] == 200)
ok_readback = sum(1 for r in rows if r["http_status"] == 200 and r["after_final"] == "verified" and r["image"])
reenum_failures = [r["index"] for r in rows
                   if r["http_status"] in (409, 503) or r.get("retry_capture") is False or r.get("may_have_fired")]
# Ports re-enumerate: every accepted shot must name the requested body's serial, or the receipt
# cannot say which camera produced it.
serial_mismatches = [r["index"] for r in rows
                     if r["http_status"] == 200 and r.get("camera_serial") != env["SERIAL"]]
elapsed = [r["elapsed_s"] for r in rows if r["elapsed_s"] > 0]
# 0/0 is never a pass: at least one shot must have been requested AND recorded.
no_drop = (expected >= 1 and len(rows) == expected and ok_200 == expected
           and ok_readback == expected and not reenum_failures and not serial_mismatches)
# The kill criterion also needs RAW transfer: every accepted shot's ARW present, in band and hashed.
raw = raw_proof.raw_transfer_verdict(rows, env["MODEL"])
passed = no_drop and raw["passed"]
data = {
    "phase": "proof", "mode": env["RECEIPT_MODE"] if "RECEIPT_MODE" in env else "apply",
    "executed": True, "url": env["URL"], "model": env["MODEL"], "port": env["PORT"],
    "serial": env["SERIAL"], "serial_mismatches": serial_mismatches,
    "requested_shots": expected, "recorded_shots": len(rows),
    "http_200": ok_200, "verified_readback": ok_readback,
    "reenumeration_failures": reenum_failures,
    "elapsed_s": {"min": min(elapsed) if elapsed else None,
                  "max": max(elapsed) if elapsed else None,
                  "mean": round(sum(elapsed) / len(elapsed), 3) if elapsed else None},
    "service_pid": int(env["PID"]) if env.get("PID") else None,
    "peak_vmhwm_kib": int(env["PEAK"]) if env.get("PEAK") else None,
    "thermal_before": json.loads(env["T_BEFORE"] or "[]"),
    "thermal_after": json.loads(env["T_AFTER"] or "[]"),
    "throttled_before": env.get("TH_BEFORE") or None,
    "throttled_after": env.get("TH_AFTER") or None,
    "no_drop_pass": no_drop, "raw_transfer": raw,
    "pass": passed,
    "shots": rows,
    "written_at_unix": time.time(),
}
import tempfile
out = sys.argv[2]
if os.path.islink(out):
    sys.exit(f"refusing to write {out}: it is a symlink")
fd, tmp = tempfile.mkstemp(prefix="." + os.path.basename(out) + ".", dir=os.path.dirname(out))
with os.fdopen(fd, "w") as fh:
    fh.write(json.dumps(data, indent=2) + "\n")
os.replace(tmp, out)  # replaces the entry; never writes through a link or a hard link
bounds = raw["arw_size_bounds"]
sizes = f"{raw['raw_size_min']}-{raw['raw_size_max']} B" if raw["raw_size_min"] is not None else "none"
print(f"  PROOF: {ok_readback}/{expected} verified 200s; RAW {raw['verdict']} "
      f"({raw['shots_with_raw']}/{raw['accepted_shots']} ARW, bounds "
      f"{'-'.join(map(str, bounds)) if bounds else 'unknown'} B, sizes {sizes}); pass={passed}")
sys.exit(0 if passed else 1)
PY
  local rc=$?
  log "receipt: $RECEIPT_DIR/proof.json"
  return $rc
}

# ---- phase 8: power / thermal soak summary -------------------------------------
phase_soak() {
  phase_banner soak
  if [[ "$MODE" == "dry-run" ]]; then
    log "WOULD-RUN: summarize proof.json + host-facts.json into board-decision inputs"
    emit_receipt soak executed=false \
      note="summarizes time, peak RAM, thermal delta and throttle flags for the board decision"
    return
  fi
  python3 - "$RECEIPT_DIR" <<'PY'
import json, os, sys, time
rd = sys.argv[1]
def load(name):
    try:
        return json.load(open(os.path.join(rd, name)))
    except Exception:
        return None
host = load("host-facts.json"); proof = load("proof.json")
def peak_temp(zones):
    temps = [z["celsius"] for z in (zones or []) if isinstance(z, dict) and "celsius" in z]
    return max(temps) if temps else None
summary = {
    "phase": "soak", "mode": "apply", "executed": True,
    "board_model": (host or {}).get("model"),
    "mem_total_kib": (host or {}).get("mem_total_kib"),
    "proof_pass": (proof or {}).get("pass"),
    "peak_vmhwm_kib": (proof or {}).get("peak_vmhwm_kib"),
    "elapsed_s": (proof or {}).get("elapsed_s"),
    "peak_temp_before_c": peak_temp((proof or {}).get("thermal_before")),
    "peak_temp_after_c": peak_temp((proof or {}).get("thermal_after")),
    "throttled_before": (proof or {}).get("throttled_before"),
    "throttled_after": (proof or {}).get("throttled_after"),
    "board_decision_inputs": [
        "time per shot (elapsed_s)", "peak RAM (peak_vmhwm_kib)",
        "heat (peak_temp_before/after_c)", "power/throttle flags (throttled_after)",
    ],
    "note": "Software-side field evidence only; whole-device power, weight and battery endurance are not measured here.",
    "written_at_unix": time.time(),
}
import tempfile
out = os.path.join(rd, "soak.json")
if os.path.islink(out):
    sys.exit(f"refusing to write {out}: it is a symlink")
fd, tmp = tempfile.mkstemp(prefix=".soak.json.", dir=rd)
with os.fdopen(fd, "w") as fh:
    fh.write(json.dumps(summary, indent=2) + "\n")
os.replace(tmp, out)  # replaces the entry; never writes through a link or a hard link
print("  receipt:", os.path.join(rd, "soak.json"))
PY
}

# ---- driver -------------------------------------------------------------------
run_phase() {
  case "$1" in
    host-facts) phase_host_facts ;;
    board-config) phase_board_config ;;
    apt) phase_apt ;;
    udev) phase_udev ;;
    venv) phase_venv ;;
    readiness) phase_readiness ;;
    service) phase_service ;;
    proof) phase_proof ;;
    soak) phase_soak ;;
  esac
}

printf 'camera-autopilot Pi bring-up — mode=%s receipts=%s\n' "$MODE" "$RECEIPT_DIR"
overall_rc=0
if [[ -n "$ONLY_PHASE" ]]; then
  run_phase "$ONLY_PHASE" || overall_rc=$?
else
  for phase in "${PHASES[@]}"; do
    run_phase "$phase" || overall_rc=$?
  done
fi
printf '\nDone (mode=%s). Receipts in %s\n' "$MODE" "$RECEIPT_DIR"
exit "$overall_rc"
