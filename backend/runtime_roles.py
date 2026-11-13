"""Which runtime role this process plays (docs/RUNTIME_SPLIT_2026-09-28.md).

The Mac product is one process (``monolith``, the default). On the puck, ``CAMERA_AUTOPILOT_RUNTIME=split``
makes ``run.sh`` start ``role_control.py``; ``role_perception.py`` and ``role_jobs.py`` are separate
services. Each entry point sets ``CAMERA_AUTOPILOT_ROLE`` for its own process; it is not a user setting.
"""
from __future__ import annotations

import importlib.abc
import os
import platform
import sys
import time
from pathlib import Path

ROLE_ENV = "CAMERA_AUTOPILOT_ROLE"
PERCEPTION_SOCKET_ENV = "CAMERA_AUTOPILOT_PERCEPTION_SOCKET"
ROLES = ("monolith", "control", "perception", "jobs")

# Refused by the import guard in the control process: the CLIP stack (~1.5 GB resident).
CONTROL_BLOCKED_IMPORTS = frozenset({"torch", "torchvision", "open_clip"})
# Absent after the control entry point and its API modules load. Merges, encoders and RAW decode
# run in the heavy-jobs role; CLIP retrieval in the perception role.
CONTROL_ABSENT_MODULES = CONTROL_BLOCKED_IMPORTS | {
    "embed_index", "compact_taste_index", "stacking", "crowd_auto_mask", "crowd_recovery",
    "deflicker", "timelapse_video", "isolated_raw", "development_worker", "rawpy",
}


def role() -> str:
    value = os.environ.get(ROLE_ENV, "monolith")
    return value if value in ROLES else "monolith"


def set_role(name: str) -> None:
    if name not in ROLES:
        raise ValueError(f"unknown runtime role {name!r}")
    os.environ[ROLE_ENV] = name


def heavy_jobs_deferred() -> bool:
    """Control in the split runtime queues merges/encodes for the heavy-jobs worker."""
    return role() == "control"


class _BlockedImports(importlib.abc.MetaPathFinder):
    def __init__(self, names):
        self.names = frozenset(names)

    def find_spec(self, fullname, path=None, target=None):
        if fullname.split(".", 1)[0] in self.names:
            raise ModuleNotFoundError(
                f"{fullname} is not available in the camera-control role; it runs in the perception role",
                name=fullname)
        return None


def install_control_import_guard() -> None:
    """Refuse the CLIP stack in this process, even when the venv happens to contain it."""
    loaded = sorted(name for name in sys.modules if name.split(".", 1)[0] in CONTROL_BLOCKED_IMPORTS)
    if loaded:
        raise RuntimeError(f"control role started after {loaded[0]} was imported")
    if not any(isinstance(finder, _BlockedImports) for finder in sys.meta_path):
        sys.meta_path.insert(0, _BlockedImports(CONTROL_BLOCKED_IMPORTS))


# --- memory caps (docs/RUNTIME_SPLIT_2026-09-28.md) -------------------------------------------------
CGROUP_ROOT = Path("/sys/fs/cgroup")
PROC_SELF_CGROUP = Path("/proc/self/cgroup")
EXIT_UNCAPPED = 78  # EX_CONFIG; the units list it in RestartPreventExitStatus


def memory_cap_status(platform_name: str | None = None) -> dict:
    """Whether this process runs under an enforced cgroup v2 memory.max."""
    if (platform_name or platform.system()) != "Linux":
        return {"enforced": False, "reason": "not Linux"}
    try:
        controllers = (CGROUP_ROOT / "cgroup.controllers").read_text().split()
        if "memory" not in controllers:
            return {"enforced": False, "reason": "memory cgroup disabled (add cgroup_enable=memory to cmdline.txt)"}
        group = next(line.split("::", 1)[1].strip() for line in PROC_SELF_CGROUP.read_text().splitlines()
                     if line.startswith("0::"))
        value = (CGROUP_ROOT / group.lstrip("/") / "memory.max").read_text().strip()
    except (OSError, StopIteration) as exc:
        return {"enforced": False, "reason": f"memory.max unreadable ({type(exc).__name__}); controller not delegated?"}
    if not value.isdigit():
        return {"enforced": False, "memory_max": value, "reason": "memory.max is unlimited"}
    return {"enforced": True, "memory_max": int(value)}


ENFORCE_CAP_ENV = "CAMERA_AUTOPILOT_ENFORCE_MEMORY_CAP"  # set only by the worker unit templates


def require_memory_cap(role_name: str, platform_name: str | None = None) -> dict:
    """A split-runtime worker started by its own unit (``ENFORCE_CAP_ENV=1``, never an inherited
    INVOCATION_ID) refuses to run uncapped unless CAMERA_AUTOPILOT_ALLOW_UNCAPPED=1. The verdict is
    printed either way."""
    status = memory_cap_status(platform_name)
    if status["enforced"]:
        print(f"{role_name}: memory cap enforced (memory.max={status['memory_max']})", file=sys.stderr, flush=True)
        return status
    message = f"{role_name}: memory cap not enforced: {status['reason']}"
    if os.environ.get(ENFORCE_CAP_ENV) != "1":
        print(message + " (not started by its unit; continuing)", file=sys.stderr, flush=True)
        return status
    if os.environ.get("CAMERA_AUTOPILOT_ALLOW_UNCAPPED") == "1":
        print(message + " (continuing: CAMERA_AUTOPILOT_ALLOW_UNCAPPED=1)", file=sys.stderr, flush=True)
        return status
    print(message + "; refusing to start (set CAMERA_AUTOPILOT_ALLOW_UNCAPPED=1 to override)",
          file=sys.stderr, flush=True)
    raise SystemExit(EXIT_UNCAPPED)


def env_float(name: str, default: float, low: float, high: float) -> float:
    """A float setting; a malformed or out-of-range value falls back to the default with a warning
    instead of crash-looping a service."""
    raw = os.environ.get(name)
    if raw is None:
        return default
    try:
        value = float(raw)
    except ValueError:
        value = float("nan")
    if value == value and low <= value <= high:
        return value
    print(f"{name}={raw!r} is not a number in {low:g}..{high:g}; using {default:g}", file=sys.stderr, flush=True)
    return default


# --- liveness ------------------------------------------------------------------------------------
JOBS_HEARTBEAT = ".jobs-worker.alive"
HEARTBEAT_STALE_S = 30.0


def touch_heartbeat(path: Path) -> None:
    try:
        Path(path).touch(mode=0o600)
        os.utime(path)
    except OSError:
        pass  # liveness is advisory; never let it stop the work


def _beat(path: Path) -> dict:
    try:
        age = max(0.0, time.time() - Path(path).stat().st_mtime)
    except OSError:
        return {"alive": False, "heartbeat_age_s": None}
    return {"alive": age < HEARTBEAT_STALE_S, "heartbeat_age_s": round(age, 1)}


def liveness(store_root: Path, perception_socket: str | None) -> dict:
    """Control's view of the other two roles, from their heartbeat files."""
    return {"role": role(),
            "jobs_worker": _beat(Path(store_root) / JOBS_HEARTBEAT),
            "perception": (_beat(Path(perception_socket + ".alive")) if perception_socket
                           else {"alive": False, "heartbeat_age_s": None, "reason": "socket not configured"})}
