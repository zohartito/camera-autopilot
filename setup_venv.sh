#!/usr/bin/env bash
# One-time GPU env setup on the PC (WSL2). Installs the CUDA torch stack.
set -e
python3 -m venv ~/cam-venv
~/cam-venv/bin/pip install --upgrade pip
~/cam-venv/bin/pip install torch open_clip_torch pillow numpy
~/cam-venv/bin/python - <<'PY'
import torch
print("CUDA", torch.cuda.is_available(),
      torch.cuda.get_device_name(0) if torch.cuda.is_available() else "cpu")
PY
echo "VENV_SETUP_DONE"
