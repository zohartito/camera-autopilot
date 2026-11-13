#!/usr/bin/env bash
# Render/check by default. Installation requires --install and never starts the service.
set -euo pipefail
DEPLOY_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
exec "${CAMERA_PROVISION_PYTHON:-python3}" "$DEPLOY_DIR/provision.py" "$@"
