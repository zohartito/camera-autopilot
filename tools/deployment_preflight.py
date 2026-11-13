#!/usr/bin/env python3
"""Read-only local deployment preflight. Does not open cameras or print secrets."""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
from urllib.parse import urlparse

ROOT = Path(__file__).resolve().parents[1]


def report(root: Path = ROOT, environ=None) -> dict:
    env = os.environ if environ is None else environ
    token = env.get("CAMERA_AUTOPILOT_API_TOKEN", "")
    exposure = env.get("CAMERA_AUTOPILOT_EXPOSURE", "local")
    url = urlparse(env.get("CAMERA_AUTOPILOT_PUBLIC_HTTPS_URL", ""))
    index = root / "data/taste_index.npz"
    package = next((env.get(key) for key in (
        "CAMERA_REMOTE_COMMAND_PACKAGE", "SONY_CAMERA_REMOTE_COMMAND_PACKAGE",
        "SONY_CAMERA_REMOTE_COMMAND_PATH") if env.get(key)), None)
    checks = {
        "configured_token_strong": len(token) >= 24 and not any(c.isspace() for c in token),
        "launcher_may_generate_token": not bool(token),
        "taste_index_exists": index.is_file(),
        "taste_manifest_exists": (root / "data/taste_index.MANIFEST.json").is_file(),
        "lan_proxy_declared": exposure == "lan-proxy",
        "lan_https_configuration_declared": (exposure == "lan-proxy"
            and env.get("CAMERA_AUTOPILOT_TLS_PROXY_CONFIRMED") == "1"
            and url.scheme == "https" and bool(url.hostname)
            and not url.username and not url.password),
        "sony_command_package_configured_and_exists": bool(package and Path(package).expanduser().exists()),
        "project_license_present": any((root / name).is_file() for name in ("LICENSE", "LICENSE.md", "LICENSE.txt")),
        "third_party_notices_present": (root / "THIRD_PARTY_NOTICES.md").is_file(),
    }
    return {"checks": checks, "public_release_ready": False,
            "not_established_by_this_preflight": [
                "certificate trust from physical phone", "camera behavior", "taste index integrity",
                "Sony package model support or redistribution terms", "Apple signing and physical device acceptance",
                "beginner acceptance", "legal review", "support and update ownership"],
            "interpretation": "Configuration and file presence only; no secrets, hardware actions, or release approval."}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    result = report()
    serialized = json.dumps(result, indent=2)
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(serialized + "\n")
    print(serialized)


if __name__ == "__main__":
    main()
