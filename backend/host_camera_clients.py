"""Read-only preflight: running macOS ImageCapture client apps that can take a camera's USB.

On 1 Oct 2026 a 200-frame Holy Grail session stopped at frame 14: Preview (an ImageCapture
client) relaunched ptpcamerad, which opened the body's USB pipes inside the gphoto2 CLI's
capture window ("Could not claim the USB device"). Releasing ptpcamerad before each claim
cannot stop a client app from relaunching it, so the owner is asked to quit the app.

One cheap process listing (`/bin/ps -axo comm=`): no AppleScript, no app activation. The
result is a warning only: it never refuses, blocks or changes an exposure count.
"""
from __future__ import annotations

import os
import subprocess
import sys

# ImageCapture client apps known to claim cameras, by app bundle name (= executable name).
IMAGE_CAPTURE_CLIENT_APPS = ("Preview", "Photos", "Image Capture")
PROCESS_LIST_ARGV = ("/bin/ps", "-axo", "comm=")
PROCESS_LIST_TIMEOUT_S = 2.0
PROCESS_LIST_MAX_CHARS = 1 << 20


def _list_processes() -> str:
    """The only place that executes ps (pytest replaces it in conftest.py, and it refuses to run
    in a pytest session, collection included, even after a module reload). Returns stdout, one
    executable path per line (bounded by the process table); a non-zero exit raises, and the
    timeout kills the child."""
    if os.environ.get("PYTEST_VERSION") or os.environ.get("PYTEST_CURRENT_TEST"):
        raise RuntimeError("the host's process list is never read in a pytest session")
    result = subprocess.run(list(PROCESS_LIST_ARGV), capture_output=True, text=True, errors="replace",
                            timeout=PROCESS_LIST_TIMEOUT_S, check=False)
    if result.returncode != 0:
        raise RuntimeError(f"ps exited {result.returncode}")
    return result.stdout or ""


def running_clients(*, lister=None, platform: str | None = None) -> dict:
    """`{"status": "checked", "clients": [...]}` naming each running app of
    IMAGE_CAPTURE_CLIENT_APPS (in that order); `{"status": "not_applicable"}` off macOS;
    `{"status": "unavailable", "reason": ...}` when the listing fails. Never raises."""
    try:
        if (platform if platform is not None else sys.platform) != "darwin":
            return {"status": "not_applicable"}
        output = (lister or _list_processes)()
        if not isinstance(output, str):
            return {"status": "unavailable", "reason": "the process listing returned no text"}
        executables = {line.strip() for line in output[:PROCESS_LIST_MAX_CHARS].splitlines()}
        clients = [app for app in IMAGE_CAPTURE_CLIENT_APPS
                   if any(path.endswith(f"/{app}.app/Contents/MacOS/{app}") for path in executables)]
        return {"status": "checked", "clients": clients}
    except subprocess.TimeoutExpired:
        return {"status": "unavailable", "reason": f"the process listing timed out after {PROCESS_LIST_TIMEOUT_S:g} s"}
    except Exception as exc:  # noqa: BLE001 — a warning never changes a capture
        return {"status": "unavailable", "reason": f"{type(exc).__name__}: {str(exc)[:200]}"}


def summary_line(evidence) -> str | None:
    """One plain-language line asking the owner to quit the running clients, or None."""
    clients = evidence.get("clients") if isinstance(evidence, dict) and evidence.get("status") == "checked" else None
    if not clients:
        return None
    names = clients[0] if len(clients) == 1 else f"{', '.join(clients[:-1])} and {clients[-1]}"
    subject = "it" if len(clients) == 1 else "they"
    return f"Quit {names} while the camera is in use: {subject} can take the USB connection mid-capture."
