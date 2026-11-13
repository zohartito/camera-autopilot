"""Prepare a Linux systemd user service; no package/network/camera operations."""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import platform
import shutil
import stat
import subprocess

ROOT = Path(__file__).resolve().parents[1]


def unit_quote(path: Path) -> str:
    value = str(path)
    if any(character in value for character in "\n\r\0"):
        raise ValueError("Paths cannot contain control characters")
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"').replace("%", "%%") + '"'


# Split runtime (docs/RUNTIME_SPLIT_2026-09-28.md): control keeps run.sh; the workers run their entry points.
ROLES = ("control", "perception", "jobs")


def unit_name(role: str = "control") -> str:
    return "camera-autopilot.service" if role == "control" else f"camera-autopilot-{role}.service"


def role_venv(repo: Path, role: str = "control") -> Path:
    return repo / "backend" / (".venv" if role == "control" else f".venv-{role}")


def render_unit(repo: Path, environment: Path, role: str = "control") -> str:
    if role not in ROLES:
        raise ValueError(f"unknown role {role!r}")
    unit = (Path(__file__).parent / f"{unit_name(role)}.in").read_text()
    for key, value in {"BACKEND": repo / "backend", "ENVIRONMENT": environment,
                       "LAUNCHER": repo / "run.sh", "PYTHON": role_venv(repo, role) / "bin/python",
                       "ENTRY": repo / "backend" / f"role_{role}.py"}.items():
        unit = unit.replace(f"@{key}@", unit_quote(value))
    return unit


def memory_cgroup_blockers(root: Path = Path("/sys/fs/cgroup"), uid: int | None = None) -> list[str]:
    """The split-runtime workers' MemoryMax only works with cgroup v2 memory enabled and delegated."""
    uid = os.getuid() if uid is None else uid
    try:
        if "memory" not in (root / "cgroup.controllers").read_text().split():
            return ["Memory cgroup is disabled: add cgroup_enable=memory to /boot/firmware/cmdline.txt and reboot"]
        manager = root / f"user.slice/user-{uid}.slice/user@{uid}.service/cgroup.controllers"
        if "memory" not in manager.read_text().split():
            return ["The user manager does not delegate the memory controller: add Delegate=memory "
                    "(systemctl edit user@.service) so MemoryMax applies to user units"]
    except OSError:
        return ["Cannot read cgroup v2 controllers; MemoryMax would not be enforced"]
    return []


def check_environment(path: Path) -> list[str]:
    errors = []
    try:
        info = path.lstat()
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) != 0o600:
            return ["Environment file must be an owned regular file with mode 0600"]
        if info.st_size > 16384:
            return ["Environment file exceeds 16 KiB"]
        values = {}
        for line in path.read_text().splitlines():
            if not line.strip() or line.lstrip().startswith("#"):
                continue
            key, separator, value = line.partition("=")
            if not separator:
                errors.append("Environment file contains a line without KEY=value")
                continue
            values[key.strip()] = value.strip().strip('"').strip("'")
        token = values.get("CAMERA_AUTOPILOT_API_TOKEN", "")
        if len(token) < 24 or any(character.isspace() for character in token) or token.startswith("REPLACE"):
            errors.append("Configure a private API token of at least 24 characters")
        if values.get("CAMERA_ENGINE") != "real":
            errors.append("Portable camera service requires CAMERA_ENGINE=real")
        if values.get("CAMERA_MODEL") not in {"Sony ILCE-7M2 (Control)", "Sony Alpha-A7 III (PC Control)"}:
            errors.append("Choose an exact supported CAMERA_MODEL")
        if values.get("CAMERA_AUTOPILOT_EXPOSURE", "local") != "local":
            errors.append("This service template accepts loopback-only local exposure; review a TLS proxy separately")
    except OSError:
        errors.append("Private environment file is missing or unreadable")
    return errors


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", type=Path, default=ROOT)
    parser.add_argument("--env-file", type=Path,
                        default=Path.home() / ".config/camera-autopilot/runtime.env")
    parser.add_argument("--output", type=Path, help="Write a reviewable unit without installing")
    parser.add_argument("--install", action="store_true", help="Install user unit on Linux; does not start it")
    parser.add_argument("--role", choices=ROLES, default="control",
                        help="Split-runtime role unit (control is the existing camera service)")
    args = parser.parse_args(argv)
    repo, environment = args.repo.resolve(), args.env_file.absolute()
    unit = render_unit(repo, environment, args.role)
    errors = check_environment(environment)
    if platform.system() != "Linux":
        errors.append("Linux is required for systemd deployment")
    if not shutil.which("systemctl"):
        errors.append("systemctl is not installed")
    venv = role_venv(repo, args.role)
    if not (venv / "bin/python").is_file():
        errors.append(f"An approved Linux runtime is required at {venv.relative_to(repo)}")
    if args.role != "control" and platform.system() == "Linux":
        errors.extend(memory_cgroup_blockers())
    if args.role == "control" and not shutil.which("gphoto2"):
        errors.append("The Linux gphoto2 CLI is not installed")
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(unit)
    installed = False
    if args.install and not errors:
        target = Path.home() / ".config/systemd/user" / unit_name(args.role)
        target.parent.mkdir(parents=True, exist_ok=True)
        if target.is_symlink() or target.exists():
            raise SystemExit("Existing service preserved; inspect and update it manually")
        with target.open("x") as stream:
            stream.write(unit)
        target.chmod(0o600)
        subprocess.run(["systemctl", "--user", "daemon-reload"], check=True, timeout=15)
        installed = True
    print(json.dumps({"ready_to_install": not errors, "installed": installed,
                      "started": False, "camera_accessed": False,
                      "blockers": errors, "rendered_unit": str(args.output) if args.output else None}, indent=2))
    return 1 if args.install and errors else 0


if __name__ == "__main__":
    raise SystemExit(main())
