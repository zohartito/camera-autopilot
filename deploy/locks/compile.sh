#!/usr/bin/env bash
# Regenerate the per-role Linux arm64 locks for the puck (docs/RUNTIME_SPLIT_2026-09-28.md).
# Target: Raspberry Pi OS Bookworm = Debian 12, Python 3.11, glibc 2.36 (manylinux_2_36), aarch64.
# Needs network access to PyPI (and download.pytorch.org for the CPU torch build); installs nothing.
# --only-binary :all: makes the resolution fail if any pinned package lacks a compatible wheel.
# Bump EXCLUDE_NEWER deliberately; it is what makes a re-run reproduce the same pins.
set -euo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")"
EXCLUDE_NEWER="2026-09-27T00:00:00Z"
common=(-c mac-tested.constraints.txt --python-platform aarch64-manylinux_2_36 --python-version 3.11
        --exclude-newer "$EXCLUDE_NEWER" --only-binary ':all:' -q)
uv pip compile requirements-control.in "${common[@]}" -o requirements-control.linux-aarch64.lock
uv pip compile requirements-jobs.in "${common[@]}" -o requirements-jobs.linux-aarch64.lock
# CPU torch/torchvision come from download.pytorch.org/whl/cpu; PyPI's aarch64 torch pulls CUDA wheels.
uv pip compile requirements-perception.in "${common[@]}" --torch-backend cpu \
  -o requirements-perception.linux-aarch64.lock
