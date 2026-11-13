#!/usr/bin/env bash
set -euo pipefail

# Archive a behavior-level USB focus probe:
#   preview -> raw manual focus nudge -> preview -> inverse nudge -> preview.
#
# The raw PTP command can return OK even when the lens/camera state ignores it, so
# this script pairs command logs with preview images and image-difference metrics.

out_root="${1:-artifacts/falsification/r2_usb_ptp}"
repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
default_python="$repo_root/.venv/bin/python"
if [[ ! -x "$default_python" ]]; then
  default_python="/tmp/pmca-venv/bin/python"
fi
python_bin="${PYTHON:-$default_python}"
step="${RAW_PTP_MF_STEP:-40}"
stamp="$(date +%Y%m%d-%H%M%S)"
out_dir="$out_root/${stamp}-focus-behavior"
mkdir -p "$out_dir"

summary="$out_dir/summary.txt"

capture_preview() {
  local label="$1"
  local requested="$out_dir/${label}.jpg"
  local saved="$requested"
  gphoto2 --capture-preview --filename "$requested" >"$out_dir/capture-${label}.txt" 2>&1
  if [[ ! -f "$saved" ]]; then
    local thumb="$out_dir/thumb_${label}.jpg"
    if [[ -f "$thumb" ]]; then
      saved="$thumb"
    fi
  fi
  if [[ ! -f "$saved" ]]; then
    echo "capture-preview did not create $requested or thumb_${label}.jpg" >&2
    return 1
  fi
  printf '%s\n' "$saved"
}

run_raw_step() {
  local label="$1"
  local signed_step="$2"
  "$python_bin" tests/probe_raw_control.py \
    --w2 \
    --mf-step "$signed_step" \
    --no-return \
    --no-precondition >"$out_dir/raw-${label}.txt" 2>&1
}

{
  echo "USB focus behavior falsifier"
  echo "time: $(date)"
  echo "python: $python_bin"
  echo "manual_focus_step: $step"
  echo "out_dir: $out_dir"
  echo
  echo "precondition: camera should be visible over USB PC Remote and focusmode should be Manual."
  echo "proof bar: preview-after-step should differ from preview-before more than preview-after-return."
} | tee "$summary"

gphoto2 --auto-detect >"$out_dir/gphoto-auto-detect.txt" 2>&1 || true
gphoto2 --get-config focusmode >"$out_dir/gphoto-focusmode.txt" 2>&1 || true

before="$(capture_preview before)"
run_raw_step forward "$step"
after="$(capture_preview after-step)"
run_raw_step return "-$step"
returned="$(capture_preview after-return)"

python3 - "$before" "$after" "$returned" >"$out_dir/image-metrics.json" <<'PY'
import json
import math
import sys
from pathlib import Path

from PIL import Image, ImageChops, ImageStat


def load(path):
    return Image.open(path).convert("L")


def mean_abs_diff(a, b):
    diff = ImageChops.difference(a, b)
    return ImageStat.Stat(diff).mean[0]


def rms_diff(a, b):
    diff = ImageChops.difference(a, b)
    values = ImageStat.Stat(diff).sum2[0] / (diff.width * diff.height)
    return math.sqrt(values)


def gradient_energy(img):
    # Simple sharpness proxy without cv2: average squared adjacent-pixel gradient.
    pixels = img.load()
    total = 0
    count = 0
    for y in range(img.height - 1):
        for x in range(img.width - 1):
            dx = int(pixels[x + 1, y]) - int(pixels[x, y])
            dy = int(pixels[x, y + 1]) - int(pixels[x, y])
            total += dx * dx + dy * dy
            count += 2
    return total / max(count, 1)


before_path, after_path, returned_path = map(Path, sys.argv[1:4])
before = load(before_path)
after = load(after_path)
returned = load(returned_path)

metrics = {
    "files": {
        "before": str(before_path),
        "after_step": str(after_path),
        "after_return": str(returned_path),
    },
    "size": {"width": before.width, "height": before.height},
    "diff": {
        "after_vs_before_mean_abs": mean_abs_diff(after, before),
        "after_return_vs_before_mean_abs": mean_abs_diff(returned, before),
        "after_vs_before_rms": rms_diff(after, before),
        "after_return_vs_before_rms": rms_diff(returned, before),
    },
    "sharpness_gradient_energy": {
        "before": gradient_energy(before),
        "after_step": gradient_energy(after),
        "after_return": gradient_energy(returned),
    },
}

metrics["interpretation"] = (
    "Behavior evidence is stronger when after_vs_before is materially larger "
    "than after_return_vs_before and sharpness changes in the expected direction. "
    "This metric alone is not a full visual proof; inspect the images."
)

print(json.dumps(metrics, indent=2, sort_keys=True))
PY

{
  echo
  echo "preview_before: $before"
  echo "preview_after_step: $after"
  echo "preview_after_return: $returned"
  echo "raw_forward_log: $out_dir/raw-forward.txt"
  echo "raw_return_log: $out_dir/raw-return.txt"
  echo "metrics: $out_dir/image-metrics.json"
} | tee -a "$summary"

perl -0pi -e 's/[ \t]+$//mg; s/\n+\z/\n/' "$out_dir"/*.txt "$out_dir"/*.json
echo "$out_dir"
