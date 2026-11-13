#!/usr/bin/env bash
# Turnkey overnight GPU pipeline for the PC (RTX 4070 Ti SUPER, WSL2).
#
#   1. set up once (see the chat for the exact venv/install commands), then
#   2. from the repo root:   bash backend/pc_overnight.sh
#      For a local WSL terminal, detach with:
#         nohup bash backend/pc_overnight.sh > pipeline.log 2>&1 &
#      From Windows OpenSSH, prefer a lingering user service; a detached child
#      of wsl.exe can be reaped when the Windows-side process exits:
#         systemd-run --user --unit=camera-autopilot-index --working-directory="$PWD" \
#           --setenv=CAM_VENV="$HOME/cam-venv" bash backend/pc_overnight.sh
#
# The default source is the recursive broad-Commons harvester. Set
# HARVEST_SOURCE=qi to resume the completed flat Quality Images lane. Harvest
# and embedding are resumable and checkpointed. Safe to interrupt and re-run.
set -uo pipefail

VENV="${CAM_VENV:-$HOME/cam-venv}"
PY="$VENV/bin/python"
HARVEST_SOURCE="${HARVEST_SOURCE:-broad}"
HARVEST_PAGES="${HARVEST_PAGES:-100000}"
CATCHUP_SLEEP_SECONDS="${CATCHUP_SLEEP_SECONDS:-90}"
MAX_EMBED_FAILURES="${MAX_EMBED_FAILURES:-3}"
PIPELINE_AUDIT_REPORT="${PIPELINE_AUDIT_REPORT:-}"
cd "$(dirname "$0")/.." || exit 1

case "$HARVEST_SOURCE" in
  broad)
    HARVEST_OUT="data/wikimedia_broad.csv"
    HARVEST_LOG="harvest-broad.log"
    HARVEST_CMD=("$PY" backend/harvest_wikimedia_broad.py "$HARVEST_PAGES")
    ;;
  qi)
    HARVEST_OUT="data/wikimedia_qi.csv"
    HARVEST_LOG="harvest-qi.log"
    HARVEST_CMD=("$PY" backend/harvest_wikimedia.py "$HARVEST_PAGES" "$HARVEST_OUT")
    ;;
  *)
    echo "HARVEST_SOURCE must be 'broad' or 'qi', got: $HARVEST_SOURCE"
    exit 2
    ;;
esac

if [ "${PIPELINE_DRY_RUN:-0}" = "1" ]; then
  echo "[pipeline] dry run"
  echo "[pipeline] source=$HARVEST_SOURCE pages=$HARVEST_PAGES out=$HARVEST_OUT"
  printf '[pipeline] harvest command:'
  printf ' %q' "${HARVEST_CMD[@]}"
  printf '\n'
  echo "[pipeline] embed command: $PY backend/embed_index.py build"
  echo "[pipeline] final audit: $PY backend/check_taste_index.py data/taste_index.npz --no-manifest"
  exit 0
fi

# --- preflight -------------------------------------------------------------
if [ ! -x "$PY" ]; then
  echo "venv not found at $VENV — run the one-time setup first (see chat)."
  exit 1
fi
if ! "$PY" -c "import torch,open_clip,PIL,numpy; assert torch.cuda.is_available()" 2>/dev/null; then
  echo "torch+CUDA not ready in $VENV. Verify with:"
  echo "  $PY -c \"import torch; print(torch.cuda.is_available())\""
  exit 1
fi
echo "[pipeline] CUDA OK -> $("$PY" -c 'import torch; print(torch.cuda.get_device_name(0))')"

# --- 1. harvest settings in the background ---------------------------------
echo "[pipeline] harvesting $HARVEST_SOURCE settings -> $HARVEST_OUT (log: $HARVEST_LOG)"
nohup "${HARVEST_CMD[@]}" > "$HARVEST_LOG" 2>&1 &
HARVEST_PID=$!

cleanup() {
  if kill -0 "$HARVEST_PID" 2>/dev/null; then
    kill "$HARVEST_PID" 2>/dev/null || true
    wait "$HARVEST_PID" 2>/dev/null || true
  fi
}
trap cleanup INT TERM

# A header-only corpus makes the embedder's final np.vstack undefined.
while [ ! -s "$HARVEST_OUT" ] || [ "$(wc -l < "$HARVEST_OUT")" -le 1 ]; do
  if ! kill -0 "$HARVEST_PID" 2>/dev/null; then
    wait "$HARVEST_PID"
    status=$?
    echo "[pipeline] harvester stopped before producing a usable row (exit $status)"
    exit 1
  fi
  sleep 3
done

# --- 2. embed on the GPU, looping to catch up to the growing corpus --------
echo "[pipeline] embedding on the GPU (loops until the harvest is done + caught up)..."
idle=0
embed_failures=0
harvest_finished=0
harvest_status=0
while true; do
  if "$PY" backend/embed_index.py build; then
    embed_failures=0
  else
    embed_failures=$((embed_failures + 1))
    echo "[pipeline] embed attempt failed ($embed_failures/$MAX_EMBED_FAILURES)"
    if [ "$embed_failures" -ge "$MAX_EMBED_FAILURES" ]; then
      echo "[pipeline] stopping after repeated embed failures; checkpoints are safe to resume"
      cleanup
      exit 1
    fi
  fi

  if kill -0 "$HARVEST_PID" 2>/dev/null; then
    idle=0
  else
    if [ "$harvest_finished" -eq 0 ]; then
      wait "$HARVEST_PID" || harvest_status=$?
      harvest_finished=1
      echo "[pipeline] harvester exited with status $harvest_status"
    fi
    idle=$((idle + 1))
    [ "$idle" -ge 2 ] && break
  fi
  sleep "$CATCHUP_SLEEP_SECONDS"
done

echo "[pipeline] DONE."
AUDIT_ARGS=(data/taste_index.npz --no-manifest)
if [ -n "$PIPELINE_AUDIT_REPORT" ]; then
  AUDIT_ARGS+=(--output "$PIPELINE_AUDIT_REPORT")
fi
if ! "$PY" backend/check_taste_index.py "${AUDIT_ARGS[@]}"; then
  echo "[pipeline] final taste-index integrity audit failed"
  exit 1
fi
exit "$harvest_status"
