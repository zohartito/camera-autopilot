#!/usr/bin/env bash
# release_checklist.sh — reproducible release-candidate gate for a fresh clone.
#
# A reviewer who is NOT this machine's owner runs this from the repo root:
#     bash scripts/release_checklist.sh
#
# It is idempotent (reuses an existing .venv, re-creates nothing it can reuse),
# non-destructive (never deletes tracked or untracked data, no recursive force
# delete), and
# touches the network only through `pip install` (into .venv) and a single
# `git fetch`. It NEVER opens a camera and never prints the API token.
#
# Gates that must pass for a PASS verdict:
#   - Python interpreter is >= 3.14
#   - .venv exists and requirements-test.lock installs
#   - the camera-free gate scripts/test_safe.sh passes
#   - git working tree is clean and HEAD is pushed to its upstream
#   - no capture / token / model-weight files are tracked
# Read-only inspectors (deployment_preflight, host_readiness) are reported for
# information; in a development checkout they legitimately report "not release
# ready" and do NOT by themselves fail this checklist.
set -uo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO_ROOT"

VENV="$REPO_ROOT/.venv"
PY="$VENV/bin/python"
REQUIRED_MAJOR=3
REQUIRED_MINOR=14

PASS=0
FAIL=0
declare -a RESULTS
note() { RESULTS+=("$1"); }
ok()   { PASS=$((PASS+1)); note "PASS  $1"; printf '  \033[32mPASS\033[0m  %s\n' "$1"; }
bad()  { FAIL=$((FAIL+1)); note "FAIL  $1"; printf '  \033[31mFAIL\033[0m  %s\n' "$1"; }
info() { note "INFO  $1"; printf '  INFO  %s\n' "$1"; }

echo "== camera-autopilot release checklist =="
echo "repo:   $REPO_ROOT"
echo "commit: $(git rev-parse HEAD 2>/dev/null || echo unknown)"
echo

# 1) Python interpreter >= 3.14 -----------------------------------------------
echo "[1/8] Python interpreter"
PY_BOOTSTRAP=""
for cand in python3.14 python3 python; do
  if command -v "$cand" >/dev/null 2>&1; then
    ver="$("$cand" -c 'import sys;print("%d %d"%sys.version_info[:2])' 2>/dev/null)" || continue
    maj="${ver% *}"; min="${ver#* }"
    if [[ "$maj" -gt "$REQUIRED_MAJOR" || ( "$maj" -eq "$REQUIRED_MAJOR" && "$min" -ge "$REQUIRED_MINOR" ) ]]; then
      PY_BOOTSTRAP="$cand"; break
    fi
  fi
done
if [[ -n "$PY_BOOTSTRAP" ]]; then
  ok "interpreter $("$PY_BOOTSTRAP" --version 2>&1) satisfies >= ${REQUIRED_MAJOR}.${REQUIRED_MINOR}"
else
  bad "no python >= ${REQUIRED_MAJOR}.${REQUIRED_MINOR} on PATH (install python${REQUIRED_MAJOR}.${REQUIRED_MINOR})"
fi

# 2) Virtualenv ---------------------------------------------------------------
echo "[2/8] Virtualenv (.venv)"
if [[ ! -x "$PY" ]]; then
  if [[ -n "$PY_BOOTSTRAP" ]]; then
    "$PY_BOOTSTRAP" -m venv "$VENV" && ok "created .venv" || bad "could not create .venv"
  else
    bad ".venv missing and no suitable python to create it"
  fi
else
  ok ".venv already present ($("$PY" --version 2>&1))"
fi

# 3) Install the verified lock ------------------------------------------------
echo "[3/8] pip install -r requirements-test.lock"
if [[ -x "$PY" ]]; then
  if "$PY" -m pip install -q --disable-pip-version-check -r requirements-test.lock; then
    ok "requirements-test.lock installed"
  else
    bad "requirements-test.lock install failed"
  fi
else
  bad "skipped install: no .venv interpreter"
fi

# 3b) Backend venv for the launcher subtest -----------------------------------
# scripts/test_safe.sh -> tests/test_secure_launcher.sh launches run.sh, which
# requires a backend virtualenv (backend/.venv). The root lock already provides
# fastapi+uvicorn, so on a single-venv clone we point backend/.venv at it. This
# path is gitignored, so creating it does not dirty the tree.
if [[ ! -e "$REPO_ROOT/backend/.venv" && -x "$PY" ]]; then
  ln -s "$VENV" "$REPO_ROOT/backend/.venv" && info "linked backend/.venv -> .venv (for run.sh launcher test)"
fi

# 4) Camera-free gate ---------------------------------------------------------
echo "[4/8] scripts/test_safe.sh (camera-free gate)"
GATE_LOG="$(mktemp -t rc-test-safe.XXXXXX)"
PYTEST_COUNTS="unknown"
if [[ -x "$PY" ]]; then
  if bash scripts/test_safe.sh >"$GATE_LOG" 2>&1; then
    PYTEST_COUNTS="$(grep -Eo '[0-9]+ passed[,0-9 a-z]*' "$GATE_LOG" | tail -1)"
    ok "test_safe.sh passed (${PYTEST_COUNTS:-counts unavailable})"
  else
    PYTEST_COUNTS="$(grep -Eo '[0-9]+ (passed|failed)[,0-9 a-z]*' "$GATE_LOG" | tail -1)"
    bad "test_safe.sh FAILED (${PYTEST_COUNTS:-see $GATE_LOG}); tail:"
    tail -15 "$GATE_LOG" | sed 's/^/      /'
  fi
else
  bad "skipped gate: no .venv interpreter"
fi

# 5) Read-only inspectors (informational) -------------------------------------
echo "[5/8] Read-only inspectors"
if [[ -x "$PY" ]]; then
  if "$PY" tools/deployment_preflight.py >/dev/null 2>&1; then
    info "deployment_preflight.py ran (configuration/file-presence only; not a release approval)"
  else
    info "deployment_preflight.py returned non-zero (dev checkout: expected while unconfigured)"
  fi
  RDY_DIR="$(mktemp -d -t rc-readiness.XXXXXX)"
  head -c 40 /dev/zero | tr '\0' 'x' > "$RDY_DIR/token"; chmod 600 "$RDY_DIR/token"
  printf '{"artifacts": []}' > "$RDY_DIR/manifest.json"
  mkdir -p "$RDY_DIR/storage" "$RDY_DIR/artifacts"
  if "$PY" -m deploy.host_readiness --storage "$RDY_DIR/storage" --token-file "$RDY_DIR/token" \
       --manifest "$RDY_DIR/manifest.json" --artifact-root "$RDY_DIR/artifacts" >/dev/null 2>&1; then
    info "host_readiness.py verdict: ready (unexpected in a dev checkout)"
  else
    info "host_readiness.py verdict: not ready (expected: no target board / offline package)"
  fi
else
  info "skipped inspectors: no .venv interpreter"
fi

# 6) Clean tree + HEAD pushed --------------------------------------------------
echo "[6/8] git fetch + working tree / upstream"
git fetch --quiet 2>/dev/null || info "git fetch failed (offline?); upstream comparison uses last-known ref"
if [[ -z "$(git status --porcelain 2>/dev/null)" ]]; then
  ok "working tree clean"
else
  bad "working tree has uncommitted changes"
fi
UPSTREAM="$(git rev-parse --abbrev-ref --symbolic-full-name @{u} 2>/dev/null || true)"
if [[ -n "$UPSTREAM" ]]; then
  if [[ "$(git rev-parse HEAD)" == "$(git rev-parse @{u})" ]]; then
    ok "HEAD is pushed (== $UPSTREAM)"
  else
    bad "HEAD differs from upstream $UPSTREAM (push before release)"
  fi
else
  bad "no upstream configured for the current branch"
fi

# 7) No capture / token / model files tracked ---------------------------------
echo "[7/8] No sensitive artifacts tracked"
# Literal release pattern from AGENTS/release scope. Reviewed text proofs
# (*.md/*.txt, e.g. mobile/artifacts/ios-simulator/PROOF.md) are allow-listed;
# any binary capture, token file, or model weight (.pt/.onnx) is a hard fail.
SENSITIVE="$(git ls-files | grep -E 'captures/|artifacts/|(^|/)token[^/]*$|\.pt$|\.onnx$' | grep -vE '\.(md|txt)$' || true)"
if [[ -z "$SENSITIVE" ]]; then
  ok "no tracked captures/tokens/model weights"
else
  bad "sensitive files are tracked:"
  echo "$SENSITIVE" | sed 's/^/      /'
fi

# 8) Summary ------------------------------------------------------------------
echo "[8/8] Summary"
echo "----------------------------------------"
printf '  pytest gate:  %s\n' "${PYTEST_COUNTS:-unknown}"
printf '  checks:       %d passed, %d failed\n' "$PASS" "$FAIL"
echo "----------------------------------------"
if [[ "$FAIL" -eq 0 ]]; then
  echo "RELEASE CHECKLIST: PASS"
  exit 0
else
  echo "RELEASE CHECKLIST: FAIL"
  exit 1
fi
