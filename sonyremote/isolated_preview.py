"""Bounded native preview/read-only focus probes in a disposable process.

Callers must retain their camera ownership lock until this call returns. A preview
uses libgphoto2 capture_preview only: it never falls back to a still exposure.
Legacy Camera.live_view/record_clip remain disabled. No worker output is trusted.
"""
from __future__ import annotations

import hashlib
import io
import json
import math
import os
import selectors
import signal
import subprocess
import sys
import time
from pathlib import Path
from dataclasses import dataclass

SUPPORTED_MODELS = {"Sony ILCE-7M2 (Control)", "Sony Alpha-A7 III (PC Control)"}
MAX_BYTES = 8 * 1024 * 1024
MAX_METADATA_BYTES = 64 * 1024
MAX_PIXELS = 12_000_000
ADDRESS_SPACE_BYTES = 2 * 1024 * 1024 * 1024
FOCUS_NODES = ("focusmode", "focusarea", "manualfocus", "manualfocusdrive",
               "autofocusdrive", "autofocus", "focuslock", "spotfocusarea",
               "focusmagnifier", "focusmagnification", "focusdistance", "focalposition")


class IsolatedProbeError(RuntimeError):
    def __init__(self, code: str, detail: str):
        self.code = code
        super().__init__(detail)


@dataclass(frozen=True)
class PreviewFrame:
    jpeg: bytes
    model: str
    port: str
    width: int
    height: int
    elapsed_seconds: float
    sha256: str


def _validate_identity(model: str, port: str) -> None:
    import re
    if model not in SUPPORTED_MODELS or not isinstance(port, str) or not re.fullmatch(r"usb:\d{3},\d{3}", port):
        raise IsolatedProbeError("invalid_identity", "An exact supported model and USB port are required")


def _jpeg_dimensions(data: bytes, max_bytes: int) -> tuple[int, int]:
    from PIL import Image
    if not data or len(data) > max_bytes:
        raise IsolatedProbeError("byte_limit", "Preview is empty or exceeds the encoded byte limit")
    try:
        with Image.open(io.BytesIO(data)) as image:
            width, height = image.size
            if image.format != "JPEG" or width < 1 or height < 1 or width * height > MAX_PIXELS or max(width, height) > 8192:
                raise IsolatedProbeError("invalid_image", "Preview must be a bounded JPEG")
            image.load()
            return width, height
    except IsolatedProbeError:
        raise
    except Exception as exc:
        raise IsolatedProbeError("invalid_image", "Preview JPEG did not fully decode") from exc


def _collect_worker(command: list[str], *, timeout_seconds: float, max_output: int) -> bytes:
    """Incremental bounded pipe reads; kill the worker group and reap on every failure."""
    deadline = time.monotonic() + timeout_seconds
    process = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                               start_new_session=True, close_fds=True,
                               cwd=Path(__file__).resolve().parents[1])
    chunks = bytearray()
    try:
        with selectors.DefaultSelector() as selector:
            selector.register(process.stdout, selectors.EVENT_READ)
            while selector.get_map():
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise IsolatedProbeError("timeout", "Isolated camera probe exceeded its hard deadline; worker terminated")
                for key, _ in selector.select(min(remaining, 0.1)):
                    chunk = os.read(key.fileobj.fileno(), min(65536, max_output + 1 - len(chunks)))
                    if not chunk:
                        selector.unregister(key.fileobj)
                    else:
                        chunks.extend(chunk)
                        if len(chunks) > max_output:
                            raise IsolatedProbeError("byte_limit", "Worker exceeded its output byte limit")
            remaining = deadline - time.monotonic()
            try:
                code = process.wait(timeout=max(0.001, remaining))
            except subprocess.TimeoutExpired as exc:
                raise IsolatedProbeError("timeout", "Isolated camera probe exceeded its hard deadline") from exc
            if code:
                raise IsolatedProbeError("worker_failed", f"Isolated probe exited with status {code}; no preview claimed")
        return bytes(chunks)
    finally:
        # Kill even after leader exit: a descendant must not retain USB or pipes.
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        process.wait()
        process.stdout.close()


def _run_probe(model: str, port: str, mode: str, timeout_seconds: float, max_bytes: int) -> tuple[dict, bytes, float]:
    from .camera import assert_recording_allowed
    _validate_identity(model, port)
    if isinstance(timeout_seconds, bool) or not math.isfinite(timeout_seconds) or not 0.1 <= timeout_seconds <= 30:
        raise IsolatedProbeError("invalid_limit", "Timeout must be between 0.1 and 30 seconds")
    if isinstance(max_bytes, bool) or not isinstance(max_bytes, int) or not 1024 <= max_bytes <= MAX_BYTES:
        raise IsolatedProbeError("invalid_limit", "Encoded byte limit must be between 1024 and 8388608")
    assert_recording_allowed()
    started = time.monotonic()
    command = [sys.executable, "-m", "sonyremote.isolated_preview", "--worker", mode, model, port, str(max_bytes)]
    raw = _collect_worker(command, timeout_seconds=timeout_seconds, max_output=max_bytes + MAX_METADATA_BYTES)
    header, separator, data = raw.partition(b"\n")
    if not separator or len(header) > MAX_METADATA_BYTES:
        raise IsolatedProbeError("invalid_protocol", "Worker metadata is missing or too large")
    try:
        metadata = json.loads(header)
    except (ValueError, UnicodeDecodeError) as exc:
        raise IsolatedProbeError("invalid_protocol", "Worker metadata is invalid") from exc
    if not isinstance(metadata, dict):
        raise IsolatedProbeError("invalid_protocol", "Worker metadata is not an object")
    if metadata.get("ok") is not True:
        raise IsolatedProbeError(str(metadata.get("code", "probe_failed"))[:64], str(metadata.get("error", "Native probe failed"))[:2000])
    if metadata.get("model") != model or metadata.get("port") != port or metadata.get("mode") != mode:
        raise IsolatedProbeError("identity_mismatch", "Worker result did not match the requested camera and operation")
    if mode == "focus" and data:
        raise IsolatedProbeError("invalid_protocol", "Read-only descriptor probe returned unexpected image bytes")
    return metadata, data, time.monotonic() - started


def capture_preview_isolated(model: str, port: str, *, timeout_seconds: float = 8.0, max_bytes: int = MAX_BYTES) -> PreviewFrame:
    metadata, data, elapsed = _run_probe(model, port, "preview", timeout_seconds, max_bytes)
    width, height = _jpeg_dimensions(data, max_bytes)
    return PreviewFrame(data, model, port, width, height, elapsed, hashlib.sha256(data).hexdigest())


def probe_focus_isolated(model: str, port: str, *, timeout_seconds: float = 8.0) -> dict:
    metadata, _, elapsed = _run_probe(model, port, "focus", timeout_seconds, MAX_BYTES)
    return {**metadata, "elapsed_seconds": elapsed}


def _set_worker_limits(*, require_address_space: bool = True) -> None:
    import resource
    # Fail closed if the OS cannot install the allocation limit. RLIMIT_AS bounds
    # virtual address space; this is separate from the parent's encoded IPC cap.
    if require_address_space:
        try:
            resource.setrlimit(resource.RLIMIT_AS, (ADDRESS_SPACE_BYTES, ADDRESS_SPACE_BYTES))
        except (OSError, ValueError) as exc:
            raise IsolatedProbeError("unsupported_memory_limit",
                "This host cannot enforce the native preview address-space limit; camera was not opened") from exc
    resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
    resource.setrlimit(resource.RLIMIT_CPU, (30, 30))
    resource.setrlimit(resource.RLIMIT_FSIZE, (MAX_BYTES + MAX_METADATA_BYTES, MAX_BYTES + MAX_METADATA_BYTES))


def _native_probe(mode: str, model: str, port: str, max_bytes: int) -> tuple[dict, bytes]:
    from .camera import Camera, CameraError, assert_recording_allowed
    _validate_identity(model, port)
    assert_recording_allowed()
    metadata = {"ok": True, "mode": mode, "model": model, "port": port,
                "address_space_limit": ADDRESS_SPACE_BYTES if mode == "preview" else None,
                "still_exposures": 0, "setting_writes": 0}
    with Camera(model=model, port=port) as camera:
        if mode == "preview":
            # Native API is intentionally confined to this resource-limited child.
            camera_file = camera._require().capture_preview()
            blob = camera_file.get_data_and_size()
            if len(blob) > max_bytes:
                raise IsolatedProbeError("byte_limit", "Native preview exceeds encoded byte limit")
            data = bytes(blob)
            width, height = _jpeg_dimensions(data, max_bytes)
            metadata.update(width=width, height=height)
        elif mode == "focus":
            data = b""
            controls = {}
            for node in FOCUS_NODES:
                try:
                    controls[node] = camera.describe_setting(node)
                except CameraError as exc:
                    controls[node] = {"unavailable": str(exc)[:500]}
            metadata.update(controls=controls, manual_focus_motor_verified=False,
                            evidence="Read-only driver descriptors. Advertised nodes do not prove working motor control or focus bracketing.")
        else:
            raise IsolatedProbeError("invalid_mode", "Unknown isolated probe mode")
    return metadata, data


def _worker_main(argv: list[str]) -> int:
    try:
        mode, model, port, encoded_limit = argv
        _set_worker_limits(require_address_space=mode == "preview")
        metadata, data = _native_probe(mode, model, port, int(encoded_limit))
        header = json.dumps(metadata, separators=(",", ":")).encode()
        if len(header) > MAX_METADATA_BYTES:
            raise IsolatedProbeError("byte_limit", "Descriptor metadata exceeds its bound")
    except Exception as exc:
        header = json.dumps({"ok": False, "code": getattr(exc, "code", "probe_failed"), "error": str(exc)[:2000]}).encode()
        data = b""
    sys.stdout.buffer.write(header + b"\n")
    sys.stdout.buffer.write(data)
    sys.stdout.buffer.flush()
    return 0


if __name__ == "__main__":
    if sys.argv[1:2] != ["--worker"]:
        raise SystemExit("Use tools/probe_isolated_camera.py; caller must own the camera")
    raise SystemExit(_worker_main(sys.argv[2:]))
