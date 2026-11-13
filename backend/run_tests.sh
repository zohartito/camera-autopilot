#!/usr/bin/env bash
# Compatibility entry point for the complete hardware-free regression gate.
set -euo pipefail
exec bash "$(dirname "$0")/../scripts/test_safe.sh" "$@"
