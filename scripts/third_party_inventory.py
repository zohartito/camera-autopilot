#!/usr/bin/env python3
"""Print notice tables from the installed README and mobile environments."""
from __future__ import annotations

from importlib import metadata
import json
from pathlib import Path

from packaging.requirements import Requirement
from packaging.utils import canonicalize_name

ROOT = Path(__file__).resolve().parents[1]


def inventory() -> str:
    pins = {}
    for line in (ROOT / "requirements-test.lock").read_text().splitlines():
        if line.strip() and not line.startswith("#"):
            requirement = Requirement(line)
            pins[canonicalize_name(requirement.name)] = requirement
    installed = {canonicalize_name(d.metadata["Name"]): d for d in metadata.distributions()
                 if canonicalize_name(d.metadata["Name"]) != "pip"}
    if installed.keys() != pins.keys():
        raise ValueError("Use a clean environment containing only requirements-test.lock")
    lines = [f"## Installed Python packages ({len(installed)})", "",
             "| Name | Version | License metadata |", "|---|---|---|"]
    for name, dist in sorted(installed.items()):
        if dist.version not in pins[name].specifier:
            raise ValueError(f"Installed {name} does not match requirements-test.lock")
        license_name = dist.metadata.get("License-Expression")
        if not license_name:
            license_name = "; ".join(
                value.split(" :: ")[-1] for value in dist.metadata.get_all("Classifier", [])
                if value.startswith("License ::") and value.split(" :: ")[-1] != "OSI Approved"
            ) or dist.metadata.get("License")
        if not license_name or "\n" in license_name:
            raise ValueError(f"Review license metadata for {name}")
        lines.append(f"| {dist.metadata['Name']} | {dist.version} | {license_name} |")

    packages = json.loads((ROOT / "mobile/package-lock.json").read_text())["packages"]
    rows = {}
    for location, locked in packages.items():
        if not location:
            continue
        package = json.loads((ROOT / "mobile" / location / "package.json").read_text())
        if package["version"] != locked["version"]:
            raise ValueError(f"Installed {location} does not match package-lock.json")
        license_name = package.get("license")
        if not isinstance(license_name, str) or not license_name:
            raise ValueError(f"Review license metadata for {location}")
        key = (package["name"], package["version"], license_name)
        rows.setdefault(key, set()).add("development" if locked.get("dev") else "runtime")
    lines += ["", f"## Installed npm packages ({len(rows)} name/version entries)", "",
              f"{len(packages) - 1} installed package locations, including CLI dependencies. "
              "Repeated copies of the same name/version are listed once.", "",
              "| Name | Version | License metadata | Use |", "|---|---|---|---|"]
    for (name, version, license_name), uses in sorted(rows.items()):
        lines.append(f"| {name} | {version} | {license_name} | {', '.join(sorted(uses))} |")
    return "\n".join(lines) + "\n"


if __name__ == "__main__":
    print(inventory(), end="")
