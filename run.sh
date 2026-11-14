#!/usr/bin/env bash
#
# Camera Autopilot launcher (macOS).
#
# Starts the FastAPI autopilot on loopback. LAN access is supported only through
# an explicitly confirmed HTTPS reverse proxy that forwards to this loopback
# listener; the backend is never bound directly to a LAN interface.
#
# Usage:
#   ./run.sh
#   CAMERA_ENGINE=real ./run.sh
#   CAMERA_AUTOPILOT_EXPOSURE=lan-proxy \
#     CAMERA_AUTOPILOT_TLS_PROXY_CONFIRMED=1 \
#     CAMERA_AUTOPILOT_PUBLIC_HTTPS_URL=https://camera.example.test \
#     ./run.sh
#   ./run.sh --rotate-token        # replace the stored API token (every phone pairs again)
#
# Without CAMERA_AUTOPILOT_API_TOKEN the launcher reuses one per-user token kept in a private
# 0600 file (backend/api_token_store.py), so a paired phone keeps working across restarts.
#
set -euo pipefail

ROTATE_TOKEN="${CAMERA_AUTOPILOT_ROTATE_TOKEN:-0}"
for arg in "$@"; do
  case "$arg" in
    --rotate-token) ROTATE_TOKEN=1 ;;
    *)
      echo "Unknown argument: $arg (supported: --rotate-token)" >&2
      exit 1
      ;;
  esac
done
if [[ "$ROTATE_TOKEN" != "0" && "$ROTATE_TOKEN" != "1" ]]; then
  echo "CAMERA_AUTOPILOT_ROTATE_TOKEN must be 0 or 1." >&2
  exit 1
fi

# Resolve paths relative to this script so it works no matter where it's called from.
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
BACKEND_DIR="$SCRIPT_DIR/backend"
VENV_DIR="${CAMERA_AUTOPILOT_VENV:-$BACKEND_DIR/.venv}"
if [[ -z "${CAMERA_AUTOPILOT_VENV:-}" && -x "$BACKEND_DIR/.venv-ml/bin/python" ]]; then
  VENV_DIR="$BACKEND_DIR/.venv-ml"
fi
VENV_ACTIVATE="$VENV_DIR/bin/activate"

if [[ ! -f "$VENV_ACTIVATE" ]]; then
  echo "No virtualenv found at $VENV_ACTIVATE" >&2
  echo "Create it first with the complete README environment:" >&2
  printf '  python3.14 -m venv %q\n' "$VENV_DIR" >&2
  printf '  %q -m pip install -r %q\n' "$VENV_DIR/bin/python" "$SCRIPT_DIR/requirements-test.lock" >&2
  exit 1
fi

# Activate the backend venv (uvicorn, fastapi live here).
# shellcheck disable=SC1090
source "$VENV_ACTIVATE"
export VIRTUAL_ENV="$VENV_DIR"
export PATH="$VENV_DIR/bin:$PATH"

MIN_API_TOKEN_LENGTH=24

TOKEN_STORE_FILE=""
if [[ -z "${CAMERA_AUTOPILOT_API_TOKEN:-}" ]]; then
  STORE_FLAG="--load"
  if [[ "$ROTATE_TOKEN" == "1" ]]; then
    STORE_FLAG="--rotate"
  fi
  # stdout is "<state> <token>"; it stays in this shell and is never echoed.
  if ! STORE_OUTPUT="$(python "$BACKEND_DIR/api_token_store.py" "$STORE_FLAG")"; then
    echo "Could not load the stored API token (see the message above)." >&2
    exit 1
  fi
  TOKEN_STATE="${STORE_OUTPUT%% *}"
  CAMERA_AUTOPILOT_API_TOKEN="${STORE_OUTPUT#* }"
  unset STORE_OUTPUT
  TOKEN_STORE_FILE="$(python "$BACKEND_DIR/api_token_store.py" --print-path)"
  export CAMERA_AUTOPILOT_API_TOKEN
  case "$TOKEN_STATE" in
    created) TOKEN_SOURCE="stored per user, created now" ;;
    rotated) TOKEN_SOURCE="stored per user, rotated now: every phone must pair again" ;;
    *) TOKEN_SOURCE="stored per user, reused" ;;
  esac
else
  if [[ "$ROTATE_TOKEN" == "1" ]]; then
    echo "Token rotation replaces the stored token; unset CAMERA_AUTOPILOT_API_TOKEN to use it." >&2
    exit 1
  fi
  if (( ${#CAMERA_AUTOPILOT_API_TOKEN} < MIN_API_TOKEN_LENGTH )); then
    echo "CAMERA_AUTOPILOT_API_TOKEN must be at least $MIN_API_TOKEN_LENGTH characters." >&2
    exit 1
  fi
  if [[ "$CAMERA_AUTOPILOT_API_TOKEN" =~ [[:space:]] ]]; then
    echo "CAMERA_AUTOPILOT_API_TOKEN must not contain whitespace." >&2
    exit 1
  fi
  TOKEN_SOURCE="from CAMERA_AUTOPILOT_API_TOKEN"
fi

PORT="${CAMERA_AUTOPILOT_PORT:-8000}"
if ! [[ "$PORT" =~ ^[0-9]+$ ]] || (( PORT < 1 || PORT > 65535 )); then
  echo "CAMERA_AUTOPILOT_PORT must be an integer from 1 through 65535." >&2
  exit 1
fi

# CAMERA_AUTOPILOT_RUNTIME=split starts the camera-control role of the puck runtime
# (docs/RUNTIME_SPLIT_2026-09-28.md); the default keeps the single Mac process.
RUNTIME="${CAMERA_AUTOPILOT_RUNTIME:-monolith}"
if [[ "$RUNTIME" != "monolith" && "$RUNTIME" != "split" ]]; then
  echo "CAMERA_AUTOPILOT_RUNTIME must be 'monolith' or 'split'." >&2
  exit 1
fi

EXPOSURE="${CAMERA_AUTOPILOT_EXPOSURE:-local}"
BIND_HOST="127.0.0.1"
PUBLIC_URL="http://127.0.0.1:$PORT"
# One worker: the pairing code and its rate budget live in this one process.
UVICORN_ARGS=(app:app --host "$BIND_HOST" --port "$PORT" --workers 1)

case "$EXPOSURE" in
  local)
    ;;
  lan-proxy)
    if [[ "${CAMERA_AUTOPILOT_TLS_PROXY_CONFIRMED:-}" != "1" ]]; then
      echo "LAN mode requires CAMERA_AUTOPILOT_TLS_PROXY_CONFIRMED=1." >&2
      echo "Configure the HTTPS reverse proxy described in docs/SECURITY_PRIVACY_RELEASE.md." >&2
      exit 1
    fi
    PUBLIC_URL="${CAMERA_AUTOPILOT_PUBLIC_HTTPS_URL:-}"
    if [[ "$PUBLIC_URL" != https://* ]]; then
      echo "LAN mode requires CAMERA_AUTOPILOT_PUBLIC_HTTPS_URL beginning with https://." >&2
      exit 1
    fi
    case "$PUBLIC_URL" in
      https://localhost*|https://127.*|https://\[::1\]*)
        echo "LAN mode requires a non-loopback HTTPS URL reachable by the client." >&2
        exit 1
        ;;
    esac
    # Only a reverse proxy on this Mac may supply forwarded client/protocol headers.
    UVICORN_ARGS+=(--proxy-headers --forwarded-allow-ips "127.0.0.1,::1")
    ;;
  *)
    echo "CAMERA_AUTOPILOT_EXPOSURE must be 'local' or 'lan-proxy'." >&2
    exit 1
    ;;
esac

# Deliver the token through a private local file instead of terminal output or
# shell history. The stored per-user token already is that file; a token supplied
# through the environment gets a temporary file that is removed when this launcher exits.
TOKEN_FILE_IS_TEMP=0
TOKEN_FILE_IS_STORE=0
TOKEN_FILE="${CAMERA_AUTOPILOT_TOKEN_FILE:-}"
if [[ -z "$TOKEN_FILE" && -n "$TOKEN_STORE_FILE" ]]; then
  TOKEN_FILE="$TOKEN_STORE_FILE"
  TOKEN_FILE_IS_STORE=1
elif [[ -z "$TOKEN_FILE" ]]; then
  TOKEN_FILE="$(mktemp "${TMPDIR:-/tmp}/camera-autopilot-api-token.XXXXXX")"
  TOKEN_FILE_IS_TEMP=1
else
  TOKEN_PARENT="$(dirname "$TOKEN_FILE")"
  if [[ ! -d "$TOKEN_PARENT" ]]; then
    echo "Token-file directory does not exist: $TOKEN_PARENT" >&2
    exit 1
  fi
  if [[ -L "$TOKEN_FILE" ]]; then
    echo "Refusing to write the API token through a symbolic link: $TOKEN_FILE" >&2
    exit 1
  fi
fi

cleanup_token() {
  if [[ "$TOKEN_FILE_IS_TEMP" == "1" ]]; then
    rm -f -- "$TOKEN_FILE"
  fi
}
trap cleanup_token EXIT

if [[ "$TOKEN_FILE_IS_STORE" != "1" ]]; then
  (
    umask 077
    printf '%s\n' "$CAMERA_AUTOPILOT_API_TOKEN" > "$TOKEN_FILE"
  )
  chmod 600 "$TOKEN_FILE"
fi

# The app issues a one-time phone pairing code only in lan-proxy exposure.
export CAMERA_AUTOPILOT_EXPOSURE="$EXPOSURE"

echo ""
echo "  Camera Autopilot is starting (engine: ${CAMERA_ENGINE:-mock})"
echo "  -------------------------------------------------------------"
echo "  Exposure:        $EXPOSURE"
echo "  Backend bind:    http://$BIND_HOST:$PORT"
echo "  Client URL:      $PUBLIC_URL"
echo "  API token:       configured ($TOKEN_SOURCE)"
echo "  Token file:      $TOKEN_FILE (mode 0600)"
if [[ "$RUNTIME" == "split" ]]; then
  echo "  Runtime:         split (camera-control role)"
fi
echo "  -------------------------------------------------------------"
echo "  Read or copy the token locally; never put it in a URL."
if [[ "$TOKEN_FILE_IS_TEMP" == "1" ]]; then
  echo "  The token file will be removed when this launcher exits."
fi
if [[ -n "$TOKEN_STORE_FILE" ]]; then
  echo "  The same token is reused on every launch, so a paired phone stays paired."
  echo "  Replace it with ./run.sh --rotate-token (every phone must pair again)."
fi
if [[ "$EXPOSURE" == "lan-proxy" ]]; then
  echo "  Phone pairing:   the server prints a one-time 6-digit code below"
  echo "                   (10 minutes, single use: Device status > Pair with code)."
  echo "                   New code later: python tools/pair_code.py --port $PORT"
fi
echo ""

if [[ "${CAMERA_AUTOPILOT_LAUNCHER_DRY_RUN:-}" == "1" ]]; then
  echo "Launcher dry run complete; Uvicorn was not started."
  exit 0
fi

# Run from backend/ because the app uses flat imports (app, engine, intent, feedback).
cd "$BACKEND_DIR"

CHILD_PID=""
forward_signal() {
  if [[ -n "$CHILD_PID" ]]; then
    kill -TERM "$CHILD_PID" 2>/dev/null || true
  fi
}
trap forward_signal INT TERM HUP

if [[ "$RUNTIME" == "split" ]]; then
  python role_control.py "${UVICORN_ARGS[@]}" &
else
  uvicorn "${UVICORN_ARGS[@]}" &
fi
CHILD_PID=$!
STATUS=0
wait "$CHILD_PID" || STATUS=$?
exit "$STATUS"
