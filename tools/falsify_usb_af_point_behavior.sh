#!/usr/bin/env bash
set -euo pipefail

# Archive a behavior-level USB AF-point probe:
#   set AF mode -> send raw AF point A -> autofocus -> preview
#               -> send raw AF point B -> autofocus -> preview.
#
# This tests whether SDIO_ControlDevice(AFAreaPosition 0xD2DC) does more than
# return OK. It is still evidence, not a product API, until the previews show
# the selected region's focus behavior changed.

out_root="${1:-artifacts/falsification/r2_usb_ptp}"
repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
default_python="$repo_root/.venv/bin/python"
if [[ ! -x "$default_python" ]]; then
  default_python="/tmp/pmca-venv/bin/python"
fi
python_bin="${PYTHON:-$default_python}"
point_a="${RAW_PTP_AF_A:-320,420}"
point_b="${RAW_PTP_AF_B:-520,180}"
focus_area="${RAW_PTP_FOCUS_AREA:-0x102}"
stamp="$(date +%Y%m%d-%H%M%S)"
out_dir="$out_root/${stamp}-af-point-behavior"
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

send_af_point() {
  local label="$1"
  local point="$2"
  local x="${point%,*}"
  local y="${point#*,}"
  "$python_bin" tests/probe_raw_control.py \
    --focus-area "$focus_area" \
    --w1 \
    --af-x "$x" \
    --af-y "$y" \
    --no-precondition >"$out_dir/raw-af-${label}.txt" 2>&1
}

trigger_af() {
  local label="$1"
  gphoto2 --set-config autofocus=1 >"$out_dir/autofocus-${label}.txt" 2>&1 || true
  sleep 0.8
  gphoto2 --get-config focusindication >"$out_dir/focusindication-${label}.txt" 2>&1 || true
}

{
  echo "USB AF-point behavior falsifier"
  echo "time: $(date)"
  echo "python: $python_bin"
  echo "point_a: $point_a"
  echo "point_b: $point_b"
  echo "focus_area: $focus_area"
  echo "out_dir: $out_dir"
  echo
  echo "precondition: camera should be visible over USB PC Remote and focusmode should allow AF."
  echo "proof bar: previews should show focus/region sharpness changes after different AF points."
} | tee "$summary"

gphoto2 --auto-detect >"$out_dir/gphoto-auto-detect.txt" 2>&1 || true
gphoto2 --set-config focusmode=Automatic >"$out_dir/set-focusmode-automatic.txt" 2>&1 || true
gphoto2 --get-config focusmode >"$out_dir/gphoto-focusmode.txt" 2>&1 || true

before="$(capture_preview before)"
send_af_point a "$point_a"
trigger_af a
after_a="$(capture_preview after-point-a)"
send_af_point b "$point_b"
trigger_af b
after_b="$(capture_preview after-point-b)"

python3 - "$before" "$after_a" "$after_b" >"$out_dir/af-point-metrics.json" <<'PY'
import json
import math
import sys
from pathlib import Path

from PIL import Image, ImageChops, ImageStat


ROIS = {
    "foreground_table": (0.22, 0.58, 0.78, 0.94),
    "background_window": (0.56, 0.12, 0.96, 0.46),
    "center_door_tv": (0.34, 0.14, 0.66, 0.55),
}


def load(path):
    return Image.open(path).convert("L")


def crop_frac(img, box):
    w, h = img.size
    left, top, right, bottom = box
    return img.crop((int(left * w), int(top * h), int(right * w), int(bottom * h)))


def gradient_energy(img):
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


def mean_abs_diff(a, b):
    return ImageStat.Stat(ImageChops.difference(a, b)).mean[0]


before_path, a_path, b_path = map(Path, sys.argv[1:4])
frames = {
    "before": load(before_path),
    "after_point_a": load(a_path),
    "after_point_b": load(b_path),
}

roi_metrics = {}
for roi_name, box in ROIS.items():
    roi_metrics[roi_name] = {
        frame_name: gradient_energy(crop_frac(frame, box))
        for frame_name, frame in frames.items()
    }

metrics = {
    "files": {
        "before": str(before_path),
        "after_point_a": str(a_path),
        "after_point_b": str(b_path),
    },
    "size": {"width": frames["before"].width, "height": frames["before"].height},
    "full_frame_mean_abs_diff": {
        "point_a_vs_before": mean_abs_diff(frames["after_point_a"], frames["before"]),
        "point_b_vs_point_a": mean_abs_diff(frames["after_point_b"], frames["after_point_a"]),
        "point_b_vs_before": mean_abs_diff(frames["after_point_b"], frames["before"]),
    },
    "roi_sharpness_gradient_energy": roi_metrics,
    "interpretation": (
        "Behavior evidence is stronger if the ROI near point A sharpens after point A "
        "and the ROI near point B sharpens after point B. Inspect the images and logs; "
        "metrics alone are not enough for product truth."
    ),
}

print(json.dumps(metrics, indent=2, sort_keys=True))
PY

{
  echo
  echo "preview_before: $before"
  echo "preview_after_point_a: $after_a"
  echo "preview_after_point_b: $after_b"
  echo "raw_a_log: $out_dir/raw-af-a.txt"
  echo "raw_b_log: $out_dir/raw-af-b.txt"
  echo "metrics: $out_dir/af-point-metrics.json"
} | tee -a "$summary"

perl -0pi -e 's/[ \t]+$//mg; s/\n+\z/\n/' "$out_dir"/*.txt "$out_dir"/*.json
echo "$out_dir"
