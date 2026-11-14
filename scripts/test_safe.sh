#!/usr/bin/env bash
# Hardware-free regression gate. Never invokes smoke_test.py or camera probe CLIs.
set -euo pipefail
cd "$(dirname "$0")/.."
PYTHON="${CAMERA_TEST_PYTHON:-.venv/bin/python}"
if [[ ! -x "$PYTHON" ]]; then
  echo "Missing test interpreter: $PYTHON. Create .venv and install requirements-test.lock." >&2
  exit 2
fi
export CAMERA_ENGINE=mock
export OLLAMA_HOST=http://127.0.0.1:1
export VISION_HOST=http://127.0.0.1:1
export VISION_BANANA_HOST=http://127.0.0.1:1
"$PYTHON" -m pytest -q backend tests "$@"
for module in physics sensor_noise lens_optics camera_caps color_accuracy constraint_cards; do
  "$PYTHON" "backend/$module.py"
done
"$PYTHON" tests/test_mcp_shoot.py
bash tests/test_secure_launcher.sh
bash tests/test_usb_bridge_smoke_security.sh
# Deterministic golden cases are required. Unavailable model grading stays explicit.
"$PYTHON" tests/eval_intent.py
