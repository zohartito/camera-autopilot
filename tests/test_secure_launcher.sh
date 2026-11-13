#!/usr/bin/env bash
set -euo pipefail
# Portable file mode: GNU stat (-c) first, since BSD `stat -f` "succeeds" on Linux by printing
# filesystem information instead of a mode.
file_mode() {
  stat -c '%a' "$1" 2>/dev/null || stat -f '%Lp' "$1"
}



ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
RUN="$ROOT/run.sh"
SECRET="launcher-test-secret-that-must-not-print"
TMP_BASE="${TMPDIR:-/tmp}"
TMP_ROOT="$(mktemp -d "${TMP_BASE%/}/camera-autopilot-launcher-test.XXXXXX")"
# The per-user token store lives under HOME: never touch the real one.
export HOME="$TMP_ROOT/home"
mkdir -p "$HOME"
unset XDG_CONFIG_HOME
RUNTIME_PID=""
RUNTIME_CHILD_PID=""

cleanup() {
  if [[ -n "$RUNTIME_CHILD_PID" ]] && kill -0 "$RUNTIME_CHILD_PID" 2>/dev/null; then
    kill -TERM "$RUNTIME_CHILD_PID" 2>/dev/null || true
  fi
  if [[ -n "$RUNTIME_PID" ]] && kill -0 "$RUNTIME_PID" 2>/dev/null; then
    kill -TERM "$RUNTIME_PID" 2>/dev/null || true
  fi
  if [[ -n "$RUNTIME_PID" ]]; then
    wait "$RUNTIME_PID" 2>/dev/null || true
  fi
  rm -rf -- "$TMP_ROOT"
}
trap cleanup EXIT

fail() {
  echo "FAIL: $*" >&2
  exit 1
}

run_ok() {
  local output_file="$1"
  shift
  env \
    CAMERA_AUTOPILOT_API_TOKEN="$SECRET" \
    CAMERA_AUTOPILOT_LAUNCHER_DRY_RUN=1 \
    "$@" \
    "$RUN" >"$output_file" 2>&1
}

run_fail() {
  local output_file="$1"
  shift
  if env \
    CAMERA_AUTOPILOT_API_TOKEN="$SECRET" \
    CAMERA_AUTOPILOT_LAUNCHER_DRY_RUN=1 \
    "$@" \
    "$RUN" >"$output_file" 2>&1; then
    fail "expected launcher failure"
  fi
}

run_generated_ok() {
  local output_file="$1"
  shift
  env -u CAMERA_AUTOPILOT_API_TOKEN \
    CAMERA_AUTOPILOT_LAUNCHER_DRY_RUN=1 \
    "$@" \
    "$RUN" >"$output_file" 2>&1
}

LOCAL_OUT="$TMP_ROOT/local.out"
LOCAL_TOKEN="$TMP_ROOT/local-token"
run_ok "$LOCAL_OUT" \
  CAMERA_AUTOPILOT_TOKEN_FILE="$LOCAL_TOKEN"

grep -Fq "Exposure:        local" "$LOCAL_OUT" || fail "local mode was not selected"
grep -Fq "Backend bind:    http://127.0.0.1:8000" "$LOCAL_OUT" || fail "local bind is not loopback"
grep -Fq "Client URL:      http://127.0.0.1:8000" "$LOCAL_OUT" || fail "local URL is wrong"
if grep -Fq "$SECRET" "$LOCAL_OUT"; then
  fail "launcher printed the API token"
fi
if grep -Fq "0.0.0.0" "$LOCAL_OUT"; then
  fail "launcher exposed a wildcard bind"
fi
[[ -f "$LOCAL_TOKEN" ]] || fail "explicit token file was not created"
[[ "$(cat "$LOCAL_TOKEN")" == "$SECRET" ]] || fail "token file content is wrong"
MODE="$(file_mode "$LOCAL_TOKEN")"
[[ "$MODE" == "600" ]] || fail "token file mode is $MODE instead of 600"

GENERATED_OUT="$TMP_ROOT/generated.out"
GENERATED_TOKEN_FILE="$TMP_ROOT/generated-token"
run_generated_ok "$GENERATED_OUT" \
  CAMERA_AUTOPILOT_TOKEN_FILE="$GENERATED_TOKEN_FILE"
grep -Fq "API token:       configured (stored per user, created now)" "$GENERATED_OUT" \
  || fail "missing token did not create the per-user stored token"
[[ -f "$GENERATED_TOKEN_FILE" ]] || fail "generated token file was not created"
GENERATED_TOKEN="$(cat "$GENERATED_TOKEN_FILE")"
[[ ${#GENERATED_TOKEN} -eq 64 ]] || fail "generated token is not 64 hex characters (32 random bytes)"
[[ "$GENERATED_TOKEN" =~ ^[0-9a-f]{64}$ ]] || fail "generated token is not random hex output"
GENERATED_MODE="$(file_mode "$GENERATED_TOKEN_FILE")"
[[ "$GENERATED_MODE" == "600" ]] || fail "generated token file mode is $GENERATED_MODE instead of 600"
if grep -Fq "$GENERATED_TOKEN" "$GENERATED_OUT"; then
  fail "launcher printed the generated API token"
fi

# Without a token file override, the per-user store itself is the token file and outlives the launcher.
STORE_OUT="$TMP_ROOT/store.out"
run_generated_ok "$STORE_OUT"
grep -Fq "API token:       configured (stored per user, reused)" "$STORE_OUT" \
  || fail "second launch did not reuse the stored token"
STORE_FILE="$(sed -n 's/^  Token file:      \(.*\) (mode 0600)$/\1/p' "$STORE_OUT")"
[[ -n "$STORE_FILE" ]] || fail "stored token path was not reported"
[[ "$STORE_FILE" == "$HOME/"* ]] || fail "stored token is not under the user's home: $STORE_FILE"
[[ "$STORE_FILE" == */camera-autopilot/api-token ]] || fail "stored token path is unexpected: $STORE_FILE"
[[ -f "$STORE_FILE" ]] || fail "stored token file did not survive launcher exit"
[[ "$(cat "$STORE_FILE")" == "$GENERATED_TOKEN" ]] || fail "second launch did not reuse the same token"
[[ "$(file_mode "$STORE_FILE")" == "600" ]] || fail "stored token file is not 0600"
[[ "$(file_mode "$(dirname "$STORE_FILE")")" == "700" ]] || fail "stored token directory is not 0700"
grep -Fq -- "--rotate-token" "$STORE_OUT" || fail "banner does not explain token rotation"
if grep -Fq "$GENERATED_TOKEN" "$STORE_OUT"; then
  fail "launcher printed the stored API token"
fi
if grep -Fq "will be removed when this launcher exits" "$STORE_OUT"; then
  fail "banner claims the persistent token file is temporary"
fi

ROTATE_OUT="$TMP_ROOT/rotate.out"
env -u CAMERA_AUTOPILOT_API_TOKEN CAMERA_AUTOPILOT_LAUNCHER_DRY_RUN=1 "$RUN" --rotate-token >"$ROTATE_OUT" 2>&1
grep -Fq "API token:       configured (stored per user, rotated now: every phone must pair again)" "$ROTATE_OUT" \
  || fail "--rotate-token did not report a rotation"
ROTATED_TOKEN="$(cat "$STORE_FILE")"
[[ "$ROTATED_TOKEN" =~ ^[0-9a-f]{64}$ ]] || fail "rotated token is not 64 hex characters"
[[ "$ROTATED_TOKEN" != "$GENERATED_TOKEN" ]] || fail "--rotate-token kept the old token"
[[ "$(file_mode "$STORE_FILE")" == "600" ]] || fail "rotated token file is not 0600"
if grep -Fq "$ROTATED_TOKEN" "$ROTATE_OUT"; then
  fail "rotation printed the new API token"
fi
ENV_ROTATE_OUT="$TMP_ROOT/env-rotate.out"
run_generated_ok "$ENV_ROTATE_OUT" CAMERA_AUTOPILOT_ROTATE_TOKEN=1
[[ "$(cat "$STORE_FILE")" != "$ROTATED_TOKEN" ]] || fail "CAMERA_AUTOPILOT_ROTATE_TOKEN=1 did not rotate"
ls -A "$(dirname "$STORE_FILE")" | grep -q '\.tmp$' && fail "token store left a temporary file behind"

ROTATE_ENV_TOKEN_OUT="$TMP_ROOT/rotate-env-token.out"
run_fail "$ROTATE_ENV_TOKEN_OUT" CAMERA_AUTOPILOT_ROTATE_TOKEN=1
grep -Fq "unset CAMERA_AUTOPILOT_API_TOKEN" "$ROTATE_ENV_TOKEN_OUT" || fail "rotation with an env token is not refused"
UNKNOWN_ARG_OUT="$TMP_ROOT/unknown-arg.out"
if env CAMERA_AUTOPILOT_LAUNCHER_DRY_RUN=1 "$RUN" --rotate >"$UNKNOWN_ARG_OUT" 2>&1; then
  fail "unknown launcher argument was accepted"
fi
grep -Fq "Unknown argument: --rotate" "$UNKNOWN_ARG_OUT" || fail "unknown-argument error is unclear"
if grep -Fq "Camera Autopilot is starting" "$UNKNOWN_ARG_OUT"; then
  fail "launcher started after an unknown argument"
fi

# A store that other users can read is refused, never silently reused or repaired.
chmod 640 "$STORE_FILE"
EXPOSED_OUT="$TMP_ROOT/exposed.out"
if env -u CAMERA_AUTOPILOT_API_TOKEN CAMERA_AUTOPILOT_LAUNCHER_DRY_RUN=1 "$RUN" >"$EXPOSED_OUT" 2>&1; then
  fail "group-readable token store was accepted"
fi
grep -Fq "rotate it" "$EXPOSED_OUT" || fail "exposed-store refusal does not say to rotate"
if grep -Fq "$(cat "$STORE_FILE")" "$EXPOSED_OUT"; then
  fail "exposed-store refusal printed the token"
fi
[[ "$(file_mode "$STORE_FILE")" == "640" ]] || fail "refusal silently changed the exposed store"
env -u CAMERA_AUTOPILOT_API_TOKEN CAMERA_AUTOPILOT_LAUNCHER_DRY_RUN=1 "$RUN" --rotate-token >"$EXPOSED_OUT" 2>&1 \
  || fail "rotation could not replace the exposed store"
[[ "$(file_mode "$STORE_FILE")" == "600" ]] || fail "rotation did not replace the exposed store with a 0600 file"

# A symbolic link in place of the store is refused.
STORE_TARGET="$TMP_ROOT/store-target"
printf '%064d\n' 0 > "$STORE_TARGET"
chmod 600 "$STORE_TARGET"
rm -f -- "$STORE_FILE"
ln -s "$STORE_TARGET" "$STORE_FILE"
LINKED_OUT="$TMP_ROOT/linked.out"
if env -u CAMERA_AUTOPILOT_API_TOKEN CAMERA_AUTOPILOT_LAUNCHER_DRY_RUN=1 "$RUN" >"$LINKED_OUT" 2>&1; then
  fail "symlinked token store was accepted"
fi
grep -Fq "symbolic link" "$LINKED_OUT" || fail "symlinked-store refusal is unclear"
rm -f -- "$STORE_FILE"

TEMP_OUT="$TMP_ROOT/temp.out"
run_ok "$TEMP_OUT"
TEMP_TOKEN="$(sed -n 's/^  Token file:      \(.*\) (mode 0600)$/\1/p' "$TEMP_OUT")"
[[ -n "$TEMP_TOKEN" ]] || fail "temporary token path was not reported"
[[ ! -e "$TEMP_TOKEN" ]] || fail "temporary token file survived launcher exit"
grep -Fq "API token:       configured (from CAMERA_AUTOPILOT_API_TOKEN)" "$TEMP_OUT" \
  || fail "an environment token did not take precedence over the store"

WEAK_TOKEN="too-short"
WEAK_TOKEN_FILE="$TMP_ROOT/weak-token"
WEAK_OUT="$TMP_ROOT/weak.out"
if env \
  CAMERA_AUTOPILOT_API_TOKEN="$WEAK_TOKEN" \
  CAMERA_AUTOPILOT_TOKEN_FILE="$WEAK_TOKEN_FILE" \
  CAMERA_AUTOPILOT_LAUNCHER_DRY_RUN=1 \
  "$RUN" >"$WEAK_OUT" 2>&1; then
  fail "weak caller-supplied token was accepted"
fi
grep -Fq "must be at least 24 characters" "$WEAK_OUT" || fail "weak-token error is unclear"
[[ ! -e "$WEAK_TOKEN_FILE" ]] || fail "weak token reached token-file creation"
if grep -Fq "$WEAK_TOKEN" "$WEAK_OUT"; then
  fail "weak-token rejection printed the supplied token"
fi
if grep -Fq "Camera Autopilot is starting" "$WEAK_OUT"; then
  fail "launcher began startup after rejecting a weak token"
fi

WHITESPACE_TOKEN="launcher token with enough length but spaces"
WHITESPACE_OUT="$TMP_ROOT/whitespace.out"
if env \
  CAMERA_AUTOPILOT_API_TOKEN="$WHITESPACE_TOKEN" \
  CAMERA_AUTOPILOT_LAUNCHER_DRY_RUN=1 \
  "$RUN" >"$WHITESPACE_OUT" 2>&1; then
  fail "whitespace-containing caller-supplied token was accepted"
fi
grep -Fq "must not contain whitespace" "$WHITESPACE_OUT" \
  || fail "whitespace-token error is unclear"
if grep -Fq "$WHITESPACE_TOKEN" "$WHITESPACE_OUT"; then
  fail "whitespace-token rejection printed the supplied token"
fi

SYMLINK_TARGET="$TMP_ROOT/symlink-target"
SYMLINK_TOKEN="$TMP_ROOT/symlink-token"
printf 'do-not-overwrite\n' > "$SYMLINK_TARGET"
ln -s "$SYMLINK_TARGET" "$SYMLINK_TOKEN"
SYMLINK_OUT="$TMP_ROOT/symlink.out"
run_fail "$SYMLINK_OUT" CAMERA_AUTOPILOT_TOKEN_FILE="$SYMLINK_TOKEN"
grep -Fq "symbolic link" "$SYMLINK_OUT" || fail "token-file symlink was not rejected"
[[ "$(cat "$SYMLINK_TARGET")" == "do-not-overwrite" ]] || fail "symlink target was overwritten"

BAD_MODE_OUT="$TMP_ROOT/bad-mode.out"
run_fail "$BAD_MODE_OUT" CAMERA_AUTOPILOT_EXPOSURE=lan
grep -Fq "must be 'local' or 'lan-proxy'" "$BAD_MODE_OUT" || fail "bad-mode error is unclear"

NO_CONFIRM_OUT="$TMP_ROOT/no-confirm.out"
run_fail "$NO_CONFIRM_OUT" \
  CAMERA_AUTOPILOT_EXPOSURE=lan-proxy \
  CAMERA_AUTOPILOT_PUBLIC_HTTPS_URL=https://camera.example.test
grep -Fq "TLS_PROXY_CONFIRMED=1" "$NO_CONFIRM_OUT" || fail "LAN confirmation is not enforced"

HTTP_OUT="$TMP_ROOT/http.out"
run_fail "$HTTP_OUT" \
  CAMERA_AUTOPILOT_EXPOSURE=lan-proxy \
  CAMERA_AUTOPILOT_TLS_PROXY_CONFIRMED=1 \
  CAMERA_AUTOPILOT_PUBLIC_HTTPS_URL=http://camera.example.test
grep -Fq "beginning with https://" "$HTTP_OUT" || fail "HTTPS URL is not enforced"

LOOPBACK_OUT="$TMP_ROOT/loopback.out"
run_fail "$LOOPBACK_OUT" \
  CAMERA_AUTOPILOT_EXPOSURE=lan-proxy \
  CAMERA_AUTOPILOT_TLS_PROXY_CONFIRMED=1 \
  CAMERA_AUTOPILOT_PUBLIC_HTTPS_URL=https://127.0.0.1
grep -Fq "non-loopback HTTPS URL" "$LOOPBACK_OUT" || fail "LAN URL may still be loopback"

LAN_OUT="$TMP_ROOT/lan.out"
LAN_TOKEN="$TMP_ROOT/lan-token"
run_ok "$LAN_OUT" \
  CAMERA_AUTOPILOT_EXPOSURE=lan-proxy \
  CAMERA_AUTOPILOT_TLS_PROXY_CONFIRMED=1 \
  CAMERA_AUTOPILOT_PUBLIC_HTTPS_URL=https://camera.example.test \
  CAMERA_AUTOPILOT_TOKEN_FILE="$LAN_TOKEN"
grep -Fq "Exposure:        lan-proxy" "$LAN_OUT" || fail "LAN proxy mode was not selected"
grep -Fq "Backend bind:    http://127.0.0.1:8000" "$LAN_OUT" || fail "LAN backend escaped loopback"
grep -Fq "Client URL:      https://camera.example.test" "$LAN_OUT" || fail "public HTTPS URL is wrong"
if grep -Fq "$SECRET" "$LAN_OUT"; then
  fail "LAN launcher printed the API token"
fi

BAD_PORT_OUT="$TMP_ROOT/bad-port.out"
run_fail "$BAD_PORT_OUT" CAMERA_AUTOPILOT_PORT=70000
grep -Fq "integer from 1 through 65535" "$BAD_PORT_OUT" || fail "port validation is missing"

# Launch the real loopback server once to prove a generated token remains out of
# both terminal output and the Uvicorn process argument vector.
RUNTIME_PORT="$(python3 - <<'PY'
import socket

sock = socket.socket()
sock.bind(("127.0.0.1", 0))
print(sock.getsockname()[1])
sock.close()
PY
)"
RUNTIME_OUT="$TMP_ROOT/runtime.out"
# lan-proxy exposure still binds loopback only; it adds the one-time phone pairing code.
env -u CAMERA_AUTOPILOT_API_TOKEN \
  CAMERA_AUTOPILOT_PORT="$RUNTIME_PORT" \
  CAMERA_AUTOPILOT_EXPOSURE=lan-proxy \
  CAMERA_AUTOPILOT_TLS_PROXY_CONFIRMED=1 \
  CAMERA_AUTOPILOT_PUBLIC_HTTPS_URL=https://camera.example.test \
  "$RUN" >"$RUNTIME_OUT" 2>&1 &
RUNTIME_PID=$!

RUNTIME_STARTED=0
for _ in {1..100}; do
  if grep -Fq "Uvicorn running" "$RUNTIME_OUT"; then
    RUNTIME_STARTED=1
    break
  fi
  if ! kill -0 "$RUNTIME_PID" 2>/dev/null; then
    break
  fi
  sleep 0.1
done
[[ "$RUNTIME_STARTED" == "1" ]] || {
  cat "$RUNTIME_OUT" >&2
  fail "runtime launcher did not start Uvicorn"
}

RUNTIME_TOKEN_FILE="$(sed -n 's/^  Token file:      \(.*\) (mode 0600)$/\1/p' "$RUNTIME_OUT")"
[[ -f "$RUNTIME_TOKEN_FILE" ]] || fail "runtime stored token file is missing"
RUNTIME_TOKEN="$(cat "$RUNTIME_TOKEN_FILE")"
[[ "$RUNTIME_TOKEN" =~ ^[0-9a-f]{64}$ ]] || fail "runtime generated token is not strong random hex"
grep -Fq "Backend bind:    http://127.0.0.1:$RUNTIME_PORT" "$RUNTIME_OUT" || fail "runtime backend escaped loopback"

# The server prints one pairing code on the terminal; it redeems exactly once for the same token.
PAIR_CODE="$(sed -n 's/^  Phone pairing code: \([0-9]\{6\}\) .*$/\1/p' "$RUNTIME_OUT" | head -n 1)"
[[ "$PAIR_CODE" =~ ^[0-9]{6}$ ]] || { cat "$RUNTIME_OUT" >&2; fail "lan-proxy server did not print a pairing code"; }
PAIR_RESULT="$(python3 - "$RUNTIME_PORT" "$PAIR_CODE" <<'PY'
import json, sys, urllib.error, urllib.request
port, code = sys.argv[1], sys.argv[2]
opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
def pair():
    request = urllib.request.Request(f"http://127.0.0.1:{port}/api/pair", method="POST",
                                     data=json.dumps({"code": code}).encode(),
                                     headers={"Content-Type": "application/json"})
    try:
        with opener.open(request, timeout=10) as response:
            return response.status, json.loads(response.read()).get("token", "")
    except urllib.error.HTTPError as exc:
        return exc.code, ""
first, token = pair()
second, _ = pair()
print(first, second, token)
PY
)"
PAIR_FIRST="${PAIR_RESULT%% *}"
PAIR_REST="${PAIR_RESULT#* }"
PAIR_SECOND="${PAIR_REST%% *}"
PAIR_TOKEN="${PAIR_REST#* }"
[[ "$PAIR_FIRST" == "200" ]] || fail "the printed pairing code was not accepted ($PAIR_FIRST)"
[[ "$PAIR_SECOND" == "401" ]] || fail "a used pairing code was accepted again ($PAIR_SECOND)"
[[ "$PAIR_TOKEN" == "$RUNTIME_TOKEN" ]] || fail "pairing returned a different token"
RUNTIME_MODE="$(file_mode "$RUNTIME_TOKEN_FILE")"
[[ "$RUNTIME_MODE" == "600" ]] || fail "runtime token file mode is $RUNTIME_MODE instead of 600"

RUNTIME_CHILD_PID="$(pgrep -P "$RUNTIME_PID" | tail -n 1 || true)"
[[ -n "$RUNTIME_CHILD_PID" ]] || fail "could not identify the Uvicorn child process"
RUNTIME_ARGV="$(ps -ww -o command= -p "$RUNTIME_CHILD_PID")"
if [[ "$RUNTIME_ARGV" == *"$RUNTIME_TOKEN"* ]]; then
  fail "generated API token leaked into the Uvicorn argument vector"
fi
if grep -Fq "$RUNTIME_TOKEN" "$RUNTIME_OUT"; then
  fail "generated API token leaked into launcher or Uvicorn output"
fi

kill -TERM "$RUNTIME_PID"
wait "$RUNTIME_PID" 2>/dev/null || true
RUNTIME_PID=""
RUNTIME_CHILD_PID=""
[[ -f "$RUNTIME_TOKEN_FILE" ]] || fail "runtime stored token file did not survive launcher exit"
[[ "$(cat "$RUNTIME_TOKEN_FILE")" == "$RUNTIME_TOKEN" ]] || fail "runtime stored token changed on exit"
if grep -Fq "$RUNTIME_TOKEN" "$RUNTIME_OUT"; then
  fail "stored API token leaked into launcher or Uvicorn output after pairing"
fi

echo "secure launcher tests passed"
