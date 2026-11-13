#!/usr/bin/env bash
# Eval-gate for the intent brain's roster/config surface.
#
# Blocks a change when the golden eval (tests/eval_intent.py over
# tests/eval_set.json) fails. The eval set IS the baseline: any deterministic
# failure, or any model-graded failure when a live Ollama is reachable, is a
# score drop and blocks. Skipped model-graded cases never block (never score
# what wasn't verified — same law as the eval itself).
#
# Guarded surface (the "roster/config" of the brain):
#   backend/intent_brain.py        the layer the camera trusts
#   backend/feedback_to_eval.py    writes learned eval cases
#   tests/eval_set.json            the golden set
#   tests/eval_set.learned.json    learned additions
#   data/a7ii_settings_registry.yaml   camera settings registry
#
# Usage:
#   scripts/eval_gate.sh <file...>   # gate if any listed file is guarded
#   scripts/eval_gate.sh --staged    # gate against git staged files (pre-commit)
#   scripts/eval_gate.sh --force     # run the eval regardless of files
cd "$(dirname "$0")/.." || exit 1

GUARDED=(
  "backend/intent_brain.py"
  "backend/feedback_to_eval.py"
  "tests/eval_set.json"
  "tests/eval_set.learned.json"
  "data/a7ii_settings_registry.yaml"
)

if [ "${1:-}" = "--staged" ]; then
  files=$(git diff --cached --name-only)
elif [ "${1:-}" = "--force" ]; then
  files="${GUARDED[0]}"
else
  files="$*"
fi

touched=""
for f in $files; do
  for g in "${GUARDED[@]}"; do
    [ "$f" = "$g" ] && touched="$touched $f"
  done
done

if [ -z "$touched" ]; then
  exit 0 # nothing guarded changed — gate stays open, no eval run
fi

echo "[eval-gate] guarded file(s) changed:$touched"
echo "[eval-gate] running golden eval (tests/eval_intent.py)…"
if python3 tests/eval_intent.py; then
  echo "[eval-gate] PASS — change allowed."
  exit 0
fi
echo
echo "[eval-gate] BLOCKED — the golden eval fails with this change."
echo "[eval-gate] Fix the regression (or fix the eval case if the contract"
echo "[eval-gate] deliberately moved) before committing. To see model-graded"
echo "[eval-gate] cases too, run on a box that can reach Ollama:"
echo "[eval-gate]   OLLAMA_HOST=http://localhost:11434 python3 tests/eval_intent.py"
exit 1
