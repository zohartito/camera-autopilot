#!/usr/bin/env python3
"""Independently confirm every pin in the board locks has a wheel the CM5 can install.

    python3 deploy/locks/check_wheels.py [--json]

Target: CPython 3.11, glibc 2.36 (Raspberry Pi OS Bookworm), aarch64. Reads the PyPI JSON API and the
PyTorch CPU simple index (for ``+cpu`` pins); read-only, installs nothing. A pin with no compatible wheel
would build from source on the board and is reported as ``sdist_only`` or ``missing``.
"""
from __future__ import annotations

import argparse
import html.parser
import json
import re
import sys
import urllib.request
from pathlib import Path

LOCKS = Path(__file__).resolve().parent
GLIBC_MINOR = 36
PY_MINOR = 11
TORCH_CPU_INDEX = "https://download.pytorch.org/whl/cpu"


def pins(lock: Path) -> dict[str, str]:
    result = {}
    for line in lock.read_text().splitlines():
        line = line.split("#", 1)[0].strip()
        if line and not line.startswith("-"):
            name, _, version = line.partition("==")
            result[name.strip()] = version.strip()
    return result


def _platform_ok(tag: str) -> bool:
    if tag == "any":
        return True
    if tag == "manylinux2014_aarch64":
        return True
    match = re.fullmatch(r"manylinux_2_(\d+)_aarch64", tag)
    return bool(match) and int(match.group(1)) <= GLIBC_MINOR


def _python_ok(python: str, abi: str) -> bool:
    if python in ("py3", f"py3{PY_MINOR}") and abi == "none":
        return True
    if python == f"cp3{PY_MINOR}" and abi in (f"cp3{PY_MINOR}", "none", "abi3"):
        return True
    match = re.fullmatch(r"cp3(\d+)", python)
    return bool(match) and abi == "abi3" and int(match.group(1)) <= PY_MINOR


def wheel_ok(filename: str) -> bool:
    parts = filename[:-4].split("-")
    if not filename.endswith(".whl") or len(parts) < 5:
        return False
    python_tags, abi_tags, platform_tags = parts[-3].split("."), parts[-2].split("."), parts[-1].split(".")
    # Expand the compressed tag set into complete (python, abi, platform) triples; each must hold as a whole.
    return any(_python_ok(py, abi) and _platform_ok(plat) and (plat != "any" or abi == "none")
               for py in python_tags for abi in abi_tags for plat in platform_tags)


class _Links(html.parser.HTMLParser):
    def __init__(self):
        super().__init__()
        self.files = []

    def handle_starttag(self, tag, attrs):
        if tag == "a":
            href = dict(attrs).get("href") or ""
            self.files.append(href.split("#", 1)[0].rsplit("/", 1)[-1].replace("%2B", "+"))


def distribution_files(name: str, version: str) -> list[str]:
    if "+cpu" in version:
        parser = _Links()
        with urllib.request.urlopen(f"{TORCH_CPU_INDEX}/{name}/", timeout=60) as response:
            parser.feed(response.read().decode())
        prefix = f"{name.replace('-', '_')}-{version}-"
        return [f for f in parser.files if f.startswith(prefix)]
    with urllib.request.urlopen(f"https://pypi.org/pypi/{name}/{version}/json", timeout=60) as response:
        return [item["filename"] for item in json.load(response)["urls"]]


def check(lock: Path) -> list[dict]:
    rows = []
    for name, version in sorted(pins(lock).items()):
        try:
            files = distribution_files(name, version)
        except OSError as exc:
            rows.append({"package": name, "version": version, "status": "unchecked", "error": str(exc)})
            continue
        compatible = sorted(f for f in files if wheel_ok(f))
        status = ("wheel" if compatible else "sdist_only" if any(f.endswith((".tar.gz", ".zip")) for f in files)
                  else "missing")
        rows.append({"package": name, "version": version, "status": status,
                     "wheel": compatible[0] if compatible else None})
    return rows


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)
    report = {lock.name: check(lock) for lock in sorted(LOCKS.glob("requirements-*.linux-aarch64.lock"))}
    problems = [(lock, row) for lock, rows in report.items() for row in rows if row["status"] != "wheel"]
    if args.json:
        print(json.dumps(report, indent=2))
    else:
        for lock, rows in report.items():
            print(f"{lock}: {sum(row['status'] == 'wheel' for row in rows)}/{len(rows)} pins have a cp311 aarch64 wheel")
        for lock, row in problems:
            print(f"  {lock}: {row['package']}=={row['version']} -> {row['status']}")
    return 1 if problems else 0


if __name__ == "__main__":
    raise SystemExit(main())
