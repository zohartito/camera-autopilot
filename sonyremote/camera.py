"""sonyremote — a thin control layer ("mini-SDK") over python-gphoto2 for the Sony A7 II.

The A7 II (ILCE-7M2) is too old for Sony's official Camera Remote SDK, but it is fully
controllable over USB (PTP) by libgphoto2. This module hides the gphoto2/PTP quirks behind a
clean, CRSDK-shaped API: read/set exposure, trigger the shutter, download the JPEG, and a
best-effort live-view frame. Transport is USB today; the API is kept transport-agnostic so a
Wi-Fi or Raspberry-Pi backend can slot underneath later without touching callers.

Every gphoto2 failure is surfaced as CameraError so callers (e.g. the MCP server) have a single
exception type to catch for reconnect/retry — raw GPhoto2Error never escapes the public methods.
"""

from __future__ import annotations

import errno
import hashlib
import json
import math
import os
import re
import shutil
import stat
import subprocess
import sys
import time
import threading
import uuid
from collections import Counter
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

import gphoto2 as gp


def _release_macos_camera_service() -> None:
    """Release Apple's auto-respawning PTP owner before claiming USB; never trigger a camera."""
    if sys.platform != "darwin":
        return
    try:
        subprocess.run(["killall", "-9", "ptpcamerad"], capture_output=True, timeout=2)
    except (OSError, subprocess.TimeoutExpired):
        # The actual USB claim remains authoritative if the service cannot be released.
        pass

# Default download location for captured frames.
CAPTURE_DIR = Path(__file__).resolve().parent.parent / "captures"


def _default_quarantine_dir() -> Path:
    """Where identity-unproven downloads are kept: the engine's private shot store
    (CAMERA_AUTOPILOT_SHOTS_DIR, else backend/shots) under quarantine/, never the gallery listing.
    Product paths set Camera.quarantine_dir to engine.SHOTS_DIR / "quarantine" explicitly."""
    root = os.environ.get("CAMERA_AUTOPILOT_SHOTS_DIR")
    return (Path(root) if root else Path(__file__).resolve().parent.parent / "backend" / "shots") / "quarantine"


_DIR_FLAGS = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW


@contextmanager
def _quarantine_fd(quarantine_root: Path, *, create: bool = False):
    """A descriptor for quarantine/, reached from the shot store root with O_NOFOLLOW at every
    component (the store root itself and quarantine/), as the gallery lifecycle's deletion does."""
    store = quarantine_root.parent
    if store.is_symlink():
        raise OSError(f"shot store root must not be a symlink: {store}")
    if create:
        store.mkdir(mode=0o700, parents=True, exist_ok=True)
    store_fd = os.open(store, _DIR_FLAGS)
    try:
        if create:
            try:
                os.mkdir(quarantine_root.name, 0o700, dir_fd=store_fd)
            except FileExistsError:
                pass
        quarantine = os.open(quarantine_root.name, _DIR_FLAGS, dir_fd=store_fd)
        try:
            if create:
                os.fchmod(quarantine, 0o700)
            yield quarantine
        finally:
            os.close(quarantine)
    finally:
        os.close(store_fd)


def _make_staging_dir(quarantine_root: Path) -> Path:
    """A fresh private directory the CLI downloads into: <store>/quarantine/staging-<id>/ (0700,
    created exclusively, no symlink from the store root down). Downloaded bytes never touch a
    system temporary directory, so no cleanup can ever delete them."""
    name = f"staging-{uuid.uuid4().hex}"
    with _quarantine_fd(quarantine_root, create=True) as quarantine:
        os.mkdir(name, 0o700, dir_fd=quarantine)
        staged = os.open(name, _DIR_FLAGS, dir_fd=quarantine)
        try:
            os.fchmod(staged, 0o700)
        finally:
            os.close(staged)
    # The no-follow chain ends here: the returned path is the CLI's cwd. That relies on the shot
    # store being the trust boundary (stable directories no other user can swap; photo_lifecycle).
    return quarantine_root / name


def _staged_files(staging: Path) -> list[Path]:
    """Regular files directly in a staging directory (never following a symlink)."""
    try:
        entries = sorted(staging.iterdir())
    except FileNotFoundError:
        return []
    return [entry for entry in entries if entry.is_file() and not entry.is_symlink()]


def _remove_empty_staging(staging: Path) -> None:
    """rmdir only: a staging directory that still holds bytes is kept for retention."""
    try:
        os.rmdir(staging)
    except OSError:
        pass


def _fsync_directory(path: Path) -> None:
    directory = os.open(path, os.O_RDONLY)
    try:
        os.fsync(directory)
    finally:
        os.close(directory)


def _move_durably(source: Path, destination: Path) -> dict:
    """Deliver a staged file. Same filesystem: one atomic rename, then the directory is fsynced.
    Another volume (EXDEV): copy to a temporary name beside the destination, fsync, rename into
    place, fsync the directory, and only then remove the staged source. If that directory fsync
    fails, the source stays staged as a duplicate: it is never removed before the destination
    is durable."""
    try:
        os.replace(source, destination)
    except OSError as exc:
        if exc.errno != errno.EXDEV:
            raise
    else:
        try:
            _fsync_directory(destination.parent)
        except OSError as exc:  # the rename already moved the only copy; report, nothing to keep
            return {"source_removed": True, "directory_fsync": f"failed: {exc}"}
        return {"source_removed": True, "directory_fsync": "ok"}
    temporary = destination.with_name(f".{destination.name}.{uuid.uuid4().hex[:8]}.part")
    descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    try:
        os.fchmod(descriptor, 0o600)  # exactly 0600 whatever the umask, before any byte lands
        with os.fdopen(descriptor, "wb") as target, open(source, "rb") as origin:
            shutil.copyfileobj(origin, target)
            target.flush()
            os.fsync(target.fileno())
        os.replace(temporary, destination)
    except BaseException:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass
        raise
    try:
        _fsync_directory(destination.parent)
    except OSError as exc:
        return {"source_removed": False, "directory_fsync": f"failed: {exc}"}
    os.unlink(source)
    return {"source_removed": True, "directory_fsync": "ok"}


def _make_private(path: Path) -> None:
    """chmod 0600 through a no-follow descriptor, so the inode is private before it is moved."""
    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    try:
        os.fchmod(descriptor, 0o600)
    finally:
        os.close(descriptor)


def _delivery_durability(delivery: dict) -> dict | None:
    """None when the delivery's directory entry was flushed; otherwise why it is unconfirmed."""
    if delivery.get("directory_fsync") == "ok":
        return None
    kept = ("the staged copy is kept in quarantine" if not delivery.get("source_removed")
            else "no second copy exists")
    return {"status": "unconfirmed",
            "reason": f"the photo's folder could not be flushed to disk ({delivery.get('directory_fsync')}); "
                      f"a power loss could still lose it; {kept}"}


def _seal_unproven(staging: Path, receipt: dict) -> dict:
    """Write receipt.json into the staging directory and rename it unproven_<time>_<id>. If the
    receipt cannot be written (disk full), the caller leaves staging-<id> with its bytes intact."""
    quarantine_root = staging.parent
    with _quarantine_fd(quarantine_root) as quarantine:
        staged = os.open(staging.name, _DIR_FLAGS, dir_fd=quarantine)
        try:
            saved = []
            for name in sorted(os.listdir(staged)):
                if not stat.S_ISREG(os.stat(name, dir_fd=staged, follow_symlinks=False).st_mode):
                    continue
                handle = os.open(name, os.O_RDONLY | os.O_NOFOLLOW, dir_fd=staged)
                with os.fdopen(handle, "rb") as stream:
                    os.fchmod(stream.fileno(), 0o600)
                    digest = hashlib.file_digest(stream, "sha256").hexdigest()
                    size = os.fstat(stream.fileno()).st_size
                saved.append({"name": name, "bytes": size, "sha256": digest})
            record = {**receipt, "files": saved, "quarantined_at": time.time()}
            descriptor = os.open("receipt.json", os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                                 0o600, dir_fd=staged)
            with os.fdopen(descriptor, "w") as handle:
                json.dump(record, handle, indent=2)
                handle.flush()
                os.fsync(handle.fileno())
        finally:
            os.close(staged)
        sealed = f"unproven_{time.strftime('%Y%m%d-%H%M%S')}_{staging.name.removeprefix('staging-')}"
        os.rename(staging.name, sealed, src_dir_fd=quarantine, dst_dir_fd=quarantine)
    event = quarantine_root / sealed
    return {"directory": str(event), "receipt": str(event / "receipt.json"),
            "files": [str(event / item["name"]) for item in saved]}


MIN_RECORD_SECONDS = 0.5
DEFAULT_MAX_NATIVE_RECORD_SECONDS = 300.0
RECORDING_QUARANTINE_ROOT = Path(__file__).resolve().parent.parent / ".recording-safety"
RECORDING_QUARANTINE_NAME = "recording-stop-uncertain.json"
MAX_RECORDING_QUARANTINE_BYTES = 4 * 1024
# Provisional product policy after actual 30-second capture failures in libgphoto2
# 2.5.34's fixed 35-second Sony image wait; this is not a camera sensor limit.
SONY_ACTION_MAX_ENABLED_SHUTTER_S = 1.0

# Long single exposures (light trails, silky water) use a SEPARATE route, Camera.capture_long_exposure,
# which only the product's gated long-exposure path and an explicit owner tool call. Every other
# path keeps SONY_ACTION_MAX_ENABLED_SHUTTER_S. Why the ceiling can be 30 s there: the route never
# uses --capture-image-and-download (whose fixed 35 s image wait failed at 30 s). It fires with
# --trigger-capture and collects the files in a --wait-event-and-download window of twice the
# shutter (exposure plus a possible equal-length Long Exposure NR dark frame) plus an allowance.
# At 30 s that is exactly the 2026-09-05 A7 II probe (75 s wait, 90 s timeout), which returned the
# JPEG+ARW with 30 s EXIF and restored settings (tools/probe_sony_long_capture.py,
# private/unpublished implementation receipt). What remains UNPROVEN: the A7 III on this route at any
# shutter; the A7 II at 1.3-25 s; how long either body takes to re-enumerate afterwards; and
# attribution beyond an empty pre-drain, the OS USB serial before and after, and the JPEG's own
# EXIF (a delayed stale object with the same model and exposure time cannot be excluded).
SONY_LONG_EXPOSURE_MAX_SHUTTER_S = 30.0
# Sony's third-stop ladder above one second; both bodies top out at 30".
SONY_LONG_EXPOSURE_LADDER_S = (1.3, 1.6, 2.0, 2.5, 3.2, 4.0, 5.0, 6.0, 8.0, 10.0, 13.0, 15.0,
                               20.0, 25.0, 30.0)
LONG_EXPOSURE_WAIT_ALLOWANCE_S = 15.0    # transfer/driver work after 2x shutter (the probe's 75 s at 30 s)
LONG_EXPOSURE_TIMEOUT_MARGIN_S = 15.0    # the CLI process outlives its own event wait by this much
LONG_EXPOSURE_PRE_DRAIN_WAIT_S = 5       # read-only drain of objects already pending in camera RAM
LONG_EXPOSURE_PRE_DRAIN_TIMEOUT_S = 20.0
LONG_EXPOSURE_RECONNECT_DELAYS_S = (0.0, 1.5, 3.0, 5.0, 8.0, 12.0)  # bounded: ~30 s in total
LONG_EXPOSURE_POST_CHECK_DELAYS_S = (0.0, 1.5, 3.0)  # only while the OS briefly lists no body
LONG_EXPOSURE_EXIF_TOLERANCE = 0.02      # JPEG ExposureTime vs the configured shutter
MAX_ATTRIBUTION_JPEG_BYTES = 64 * 1024 * 1024
_EXIF_MODEL_FOR_USB_MODEL = {"Sony ILCE-7M2 (Control)": "ILCE-7M2",
                             "Sony Alpha-A7 III (PC Control)": "ILCE-7M3"}
_CLI_ERROR = re.compile(r"(?im)^\s*(?:ERROR:|\*\*\* Error)")

# Optical-focus driver route (observed on the A7 III, libgphoto2 2.5.32, 2026-09-15). The A7 II
# descriptor has none of the writable nodes; presence is always read from the connected body.
FOCUS_ROUTE_NODES = ("manualfocus", "autofocus", "focusindication", "focusmode", "focusarea",
                     "focusmagnifier")
MANUAL_FOCUS_STEP_MIN, MANUAL_FOCUS_STEP_MAX = -7, 7
AUTOFOCUS_MAX_HOLD_S = 3.0
PREVIEW_HEADER_BYTES = 32  # the headered liveview frame overwrites exactly its first 32 bytes
PREVIEW_HEADER_MAGIC = b"\x01\x00\x00\x00"
PREVIEW_READ_ATTEMPTS = 12  # liveview alternates JPEG / 32-byte-headered payload on roughly every other read
PREVIEW_RETRY_DELAY_S = 0.15
MAX_PREVIEW_BYTES = 4 * 1024 * 1024
_recording_quarantine_lock = threading.Lock()
_recording_quarantined_process = False

# Friendly key -> gphoto2 leaf config-node name (the names libgphoto2 exposes for Sony bodies).
SETTING_NODES = {
    "iso": "iso",
    "aperture": "f-number",
    "shutter": "shutterspeed",
    "white_balance": "whitebalance",
    "exposure_comp": "exposurecompensation",
    "focus_mode": "focusmode",
    "image_quality": "imagequality",
}

# gphoto2 nodes that report the shooting (PASM) mode, in order of preference.
_MODE_NODES = ("expprogram", "autoexposuremode", "capturemode")

# Still-image file types the body-shutter listener will download. Anything else the camera
# pushes over the event channel (movie clips, unknown types) is treated as non-image and
# ignored — the listener never guesses a shutter to "recover" a missing frame.
_LISTEN_IMAGE_SUFFIXES = {
    ".jpg", ".jpeg", ".arw", ".raw", ".dng", ".tif", ".tiff", ".heif", ".heic",
}


def _pick_downloaded_jpeg(files: list[Path]) -> Path | None:
    """Return the JPEG from a Sony RAW+JPEG download batch, preferring the newest shot."""
    # "Newest" is the lexicographically last name (DSC09999 over DSC00001); that this is the
    # capture order is an unproven assumption (e.g. across the camera's file-number rollover).
    jpegs = [path for path in files if path.suffix.lower() in {".jpg", ".jpeg"}]
    return jpegs[-1] if jpegs else None


def _long_exposure_attribution(jpeg: Path, model: str, shutter_s: float) -> dict:
    """Whether a downloaded JPEG's own EXIF names this body model and this exposure time.

    Reads only the header (pixels are never decoded). A mismatch or an unreadable header is not
    this capture as far as the route can prove, so the caller quarantines it instead of delivering.
    """
    expected_model = _EXIF_MODEL_FOR_USB_MODEL.get(model)
    evidence = {"method": "jpeg_exif_model_and_exposure_time", "expected_exif_model": expected_model,
                "expected_exposure_s": shutter_s, "tolerance": LONG_EXPOSURE_EXIF_TOLERANCE,
                "exif_model": None, "exif_exposure_s": None, "matches": False}
    try:
        if jpeg.stat().st_size > MAX_ATTRIBUTION_JPEG_BYTES:
            evidence["reason"] = "JPEG exceeds the attribution read budget"
            return evidence
        from PIL import Image
        with Image.open(jpeg) as image:
            exif = image.getexif()
            detail = exif.get_ifd(0x8769)
            exif_model = str(exif.get(272, "")).strip()
            exposure = detail.get(33434)
        exposure_s = float(exposure) if exposure is not None else None
    except (OSError, ValueError, TypeError, ZeroDivisionError, SyntaxError) as exc:
        evidence["reason"] = f"EXIF unreadable ({type(exc).__name__})"
        return evidence
    evidence.update(exif_model=exif_model or None,
                    exif_exposure_s=exposure_s if exposure_s is not None and math.isfinite(exposure_s) else None)
    if exif_model != expected_model:
        evidence["reason"] = f"EXIF model {exif_model or 'missing'} is not {expected_model}"
    elif evidence["exif_exposure_s"] is None:
        evidence["reason"] = "EXIF exposure time missing"
    elif abs(evidence["exif_exposure_s"] - shutter_s) > LONG_EXPOSURE_EXIF_TOLERANCE * shutter_s:
        evidence["reason"] = f"EXIF exposure {evidence['exif_exposure_s']:g} s is not the configured {shutter_s:g} s"
    else:
        evidence["matches"] = True
    return evidence


def _download_record(path: Path) -> dict:
    """Name, size and SHA-256 of one downloaded file, read through a no-follow descriptor so the
    size and the hash describe the same stored bytes. Evidence only: a read failure is recorded,
    never raised, because the shutter has already fired."""
    try:
        handle = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
        with os.fdopen(handle, "rb") as stream:
            digest = hashlib.file_digest(stream, "sha256").hexdigest()
            size = os.fstat(stream.fileno()).st_size
    except OSError as exc:
        return {"name": path.name, "size": None, "sha256": None, "error": f"{type(exc).__name__}: {exc}"}
    return {"name": path.name, "size": size, "sha256": digest}


CLI_OUTPUT_TAIL_CHARS = 4000
CLI_EVENT_LINES_MAX = 40
# What gphoto2 prints while a capture progresses: the camera reported a new object, the download
# was written, or an error. A timeout with none of these lines never saw the camera report a file.
_CLI_EVENT_PREFIXES = ("New file is in location", "Saving file as", "Deleting file", "Keeping file",
                       "ERROR", "*** Error", "An error occurred")


def _cli_text(output) -> str:
    """CLI output as text: subprocess hands a timeout's partial output back as bytes."""
    if output is None:
        return ""
    if isinstance(output, bytes):
        return output.decode("utf-8", errors="replace")
    return str(output)


def _cli_output_evidence(stdout, stderr) -> dict:
    """Bounded tails of both streams plus the progress/error lines seen. Evidence only."""
    out, err = _cli_text(stdout), _cli_text(stderr)
    events = [line.strip() for line in (out + "\n" + err).splitlines()
              if line.strip().startswith(_CLI_EVENT_PREFIXES)]
    return {"stdout_tail": out[-CLI_OUTPUT_TAIL_CHARS:], "stderr_tail": err[-CLI_OUTPUT_TAIL_CHARS:],
            "stdout_chars": len(out), "stderr_chars": len(err),
            "event_lines": events[:CLI_EVENT_LINES_MAX], "event_line_count": len(events)}


def _staged_sizes(staging: Path) -> list[dict]:
    """Name and size of each file already staged; a file that cannot be read is named with its error."""
    records = []
    for path in _staged_files(staging):
        try:
            records.append({"name": path.name, "size": path.stat(follow_symlinks=False).st_size})
        except OSError as exc:
            records.append({"name": path.name, "size": None, "error": type(exc).__name__})
    return records


def _wall_clock() -> str | None:
    """UTC wall time with milliseconds, to line a receipt up with the macOS unified log."""
    try:
        now = time.time()
        return time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(now)) + f".{int(now % 1 * 1000):03d}Z"
    except Exception:  # noqa: BLE001 — evidence never changes a capture outcome
        return None


def _describe(exc: BaseException) -> str:
    """`Type: message` (bounded) for a receipt; never raises."""
    try:
        return f"{type(exc).__name__}: {str(exc)[:300]}"
    except Exception:  # noqa: BLE001 — evidence never changes a capture outcome
        return type(exc).__name__


def _note_elapsed(record: dict, started: float | None = None, **fields) -> float | None:
    """Best-effort reconnect note: with `started`, add `duration_s` and `fields` to `record`.
    Returns the current monotonic time (None if unreadable); never raises."""
    try:
        now = time.monotonic()
        if started is not None:
            record["duration_s"] = now - started
        record.update(fields)
        return now
    except Exception:  # noqa: BLE001 — evidence never changes a capture outcome
        record.update(fields)
        return None


def _record_cli_end(cli: dict, outcome: str, exit_code, stdout, stderr, staging: Path | None = None) -> None:
    """Fill the CLI receipt after the command ended. Best effort: any failure here is recorded
    as `evidence_error` and never raised, so it cannot replace the capture's own outcome."""
    try:
        cli.update(ended_at=_wall_clock(), outcome=outcome, exit_code=exit_code,
                   **_cli_output_evidence(stdout, stderr))
        if staging is not None:
            cli["staged_at_timeout"] = _staged_sizes(staging)
    except Exception as exc:  # noqa: BLE001 — evidence never changes a capture outcome
        cli["evidence_error"] = type(exc).__name__


# Which ImageCapture clients asked ptpcamerad to start sessions around an uncertain CLI capture.
# On 1 Oct 2026 Preview relaunched ptpcamerad 0.15 s into the CLI's window and the CLI failed with
# "Could not claim the USB device"; the unified log names the client (`requestStart | Process: X`).
HOST_LOG_BINARY = "/usr/bin/log"   # absolute: zsh shadows `log` with a builtin
HOST_LOG_TIMEOUT_S = 3.0
HOST_LOG_WINDOW_BEFORE_S = 5.0
HOST_LOG_WINDOW_AFTER_S = 1.0
HOST_LOG_MAX_OUTPUT_BYTES = 64 * 1024
HOST_LOG_MAX_STDERR_BYTES = 2000
HOST_LOG_MAX_CLIENTS = 16
HOST_LOG_PREDICATE = 'process == "ptpcamerad" AND eventMessage CONTAINS "requestStart"'
_HOST_LOG_CLIENT = re.compile(r"requestStart \| Process: ([^,\r\n]{1,128})")


def _under_pytest() -> bool:
    """True for the whole pytest session, collection included (pytest sets PYTEST_VERSION at
    startup and PYTEST_CURRENT_TEST per test), so even a reloaded module cannot reach the host."""
    return bool(os.environ.get("PYTEST_VERSION") or os.environ.get("PYTEST_CURRENT_TEST"))


def _run_host_log(argv: list[str], timeout_s: float) -> tuple[str, bool]:
    """The only place that executes /usr/bin/log (pytest replaces it in conftest.py, and it
    refuses to run in a pytest session even after a module reload).

    Bounded in time and memory: stdout is read up to HOST_LOG_MAX_OUTPUT_BYTES and stderr up to
    HOST_LOG_MAX_STDERR_BYTES; the child is killed and reaped on overflow or when `timeout_s` of
    wall clock has passed (subprocess.TimeoutExpired). Returns (stdout text, truncated). A
    non-zero exit without truncation raises RuntimeError."""
    if _under_pytest():
        raise RuntimeError("the host's unified log is never read in a pytest session")
    import selectors
    deadline = time.monotonic() + timeout_s
    caps = {"stdout": HOST_LOG_MAX_OUTPUT_BYTES, "stderr": HOST_LOG_MAX_STDERR_BYTES}
    buffers = {"stdout": bytearray(), "stderr": bytearray()}
    process = subprocess.Popen(argv, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    truncated = False
    try:
        with selectors.DefaultSelector() as selector:
            selector.register(process.stdout, selectors.EVENT_READ, "stdout")
            selector.register(process.stderr, selectors.EVENT_READ, "stderr")
            while selector.get_map() and not truncated:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise subprocess.TimeoutExpired(argv, timeout_s)
                for key, _ in selector.select(remaining):
                    data = os.read(key.fd, 65536)
                    if not data:
                        selector.unregister(key.fileobj)
                        continue
                    buffer = buffers[key.data]
                    room = max(0, caps[key.data] - len(buffer))
                    buffer += data[:room]
                    if key.data == "stdout" and len(data) > room:
                        truncated = True  # the cap is full and more arrived; the rest is never read
        if not truncated:
            try:
                process.wait(max(0.0, deadline - time.monotonic()))
            except subprocess.TimeoutExpired:
                raise subprocess.TimeoutExpired(argv, timeout_s) from None
    finally:
        if process.poll() is None:
            process.kill()
            process.wait()
        process.stdout.close()
        process.stderr.close()
    stdout = buffers["stdout"].decode("utf-8", errors="replace")
    if not truncated and process.returncode != 0:
        detail = buffers["stderr"].decode("utf-8", errors="replace").strip()[-200:]
        raise RuntimeError(f"log show exited {process.returncode}: {detail}")
    return stdout, truncated


def _epoch_from_wall_clock(value) -> float:
    """Seconds since the epoch for a `_wall_clock()` string (`YYYY-MM-DDTHH:MM:SS.mmmZ`)."""
    import calendar
    whole, _, fraction = str(value).rstrip("Z").partition(".")
    seconds = calendar.timegm(time.strptime(whole, "%Y-%m-%dT%H:%M:%S"))
    return seconds + (int(fraction[:3].ljust(3, "0")) / 1000 if fraction else 0.0)


def host_camera_clients(cli: dict, *, runner=None, platform: str | None = None) -> dict:
    """Distinct `Process:` names of ptpcamerad `requestStart` lines in the unified log between
    `cli.started_at - 5 s` and `cli.ended_at + 1 s` (rounded outward to whole seconds, the
    resolution of `log show --start/--end`). Diagnostics only and never raises: off macOS
    `{"status": "not_applicable"}`; any failure `{"status": "unavailable", "reason": ...}`.
    One bounded query (HOST_LOG_TIMEOUT_S), argv only, output read up to HOST_LOG_MAX_OUTPUT_BYTES;
    `runner(argv, timeout_s)` returns (text, truncated)."""
    try:
        if (platform if platform is not None else sys.platform) != "darwin":
            return {"status": "not_applicable"}
        if not isinstance(cli, dict) or not cli.get("started_at") or not cli.get("ended_at"):
            return {"status": "unavailable", "reason": "the CLI start or end time was not recorded"}
        start = math.floor(_epoch_from_wall_clock(cli["started_at"]) - HOST_LOG_WINDOW_BEFORE_S)
        end = math.ceil(_epoch_from_wall_clock(cli["ended_at"]) + HOST_LOG_WINDOW_AFTER_S)
        window = {"start": time.strftime("%Y-%m-%d %H:%M:%S+0000", time.gmtime(start)),
                  "end": time.strftime("%Y-%m-%d %H:%M:%S+0000", time.gmtime(end))}
        argv = [HOST_LOG_BINARY, "show", "--style", "compact", "--start", window["start"],
                "--end", window["end"], "--predicate", HOST_LOG_PREDICATE]
        output, truncated = (runner or _run_host_log)(argv, HOST_LOG_TIMEOUT_S)
        if not isinstance(output, str):
            return {"status": "unavailable", "reason": "the log query returned no text", "window": window}
        clients: list[str] = []
        request_starts = 0
        for line in output[:HOST_LOG_MAX_OUTPUT_BYTES].splitlines():
            match = _HOST_LOG_CLIENT.search(line)
            if not match:
                continue
            request_starts += 1
            name = match.group(1).strip()
            if name and name not in clients and len(clients) < HOST_LOG_MAX_CLIENTS:
                clients.append(name)
        return {"status": "ok", "clients": clients, "request_starts": request_starts,
                "window": window, "output_truncated": bool(truncated)}
    except subprocess.TimeoutExpired:
        return {"status": "unavailable", "reason": f"the log query timed out after {HOST_LOG_TIMEOUT_S:g} s"}
    except Exception as exc:  # noqa: BLE001 — evidence never changes a capture outcome
        return {"status": "unavailable", "reason": _describe(exc)}
    except BaseException as exc:  # noqa: BLE001 — called from a capture's finally: an interrupt
        # here must not replace the outcome propagating through it
        return {"status": "unavailable", "reason": f"interrupted: {type(exc).__name__}"}


def _capture_directory(destination: Path | None) -> Path:
    """Keep app-owned captures private without changing an explicit user directory."""
    path = Path(destination) if destination is not None else CAPTURE_DIR
    if destination is None and path.is_symlink():
        raise CameraError("Default capture directory must not be a symbolic link")
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    if destination is None:
        path.chmod(0o700)
    return path


def _reserve_private_capture(path: Path) -> tuple[int, int]:
    """Create the destination privately before a camera/library writes image bytes."""
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    try:
        info = os.fstat(descriptor)
    finally:
        os.close(descriptor)
    return info.st_dev, info.st_ino


def _release_reservation(path: Path, reservation: tuple[int, int]) -> None:
    """After a failed delivery, remove the reserved destination only if it is still the same
    empty file we created; anything else (a delivered or replaced file) is never touched."""
    try:
        info = os.lstat(path)
    except FileNotFoundError:
        return
    if stat.S_ISREG(info.st_mode) and info.st_size == 0 and (info.st_dev, info.st_ino) == reservation:
        os.unlink(path)


class CameraError(RuntimeError):
    """Raised for any camera connection or control failure, with a human-readable hint."""


class SettingNotApplied(CameraError):
    """`set_setting` wrote a value and the body read back another one (`final`). Nothing fired."""

    def __init__(self, message: str, *, key: str, requested: str, final: str):
        self.key, self.requested, self.final = key, requested, final
        super().__init__(message)


class CaptureOutcomeUncertain(CameraError):
    """The shutter may have fired; callers must never automatically retry this operation."""

    may_have_fired = True
    retry_safe = False

    def __init__(self, detail: str, *, capture_path: Path | None = None):
        self.capture_path = capture_path
        super().__init__(f"Capture outcome uncertain: shutter may have fired; not retrying "
                         f"(no automatic retry). {detail}")


class CapturePreconditionError(CameraError):
    """A capture refused by code that runs before any trigger call: nothing fired.

    Raised only where the shutter provably was not triggered (for example the gphoto2 CLI is
    missing, or the shutter cannot be bounded before USB is released). Any other capture error keeps
    its uncertain meaning.
    """

    may_have_fired = False
    retry_safe = True
    precondition_refused = True  # duck-typed by camera-agnostic callers (sessions)


class CameraIdentityError(CameraError):
    """The body at the selected port could not be proven to be the expected one (by USB serial).

    Raised before any setting write or shutter, so nothing fired and nothing changed. `reason` is
    one of: "serial_required" (no expected serial was given), "serial_mismatch", "serial_missing",
    "serial_duplicate", "not_present", "model_mismatch", "serial_unavailable" (no OS source), or
    "reselection_required" (a session saved before serial binding).
    """

    may_have_fired = False
    retry_safe = True
    identity_refused = True  # duck-typed by camera-agnostic callers (sessions, jobs, handheld)

    def __init__(self, detail: str, *, reason: str, expected_serial: str | None = None,
                 observed_serial: str | None = None, model: str | None = None, port: str | None = None):
        self.reason = reason
        self.expected_serial = expected_serial
        self.observed_serial = observed_serial
        self.model = model
        self.port = port
        # An earlier exposure of the same request (e.g. the meter frame) that stays preserved.
        self.capture_path: Path | None = None
        super().__init__(f"Camera identity not verified ({reason}); the refused operation wrote no "
                         f"setting and issued no shutter. {detail}")

    def as_dict(self) -> dict:
        """The structured refusal every API, job, session and handheld result carries."""
        return {"error": "camera_identity", "reason": self.reason, "detail": str(self),
                "expected_serial": self.expected_serial, "observed_serial": self.observed_serial,
                "camera_model": self.model, "camera_port": self.port,
                "may_have_fired": False, "retry_capture": False, "reselect_camera": True,
                **({"earlier_capture_path": str(self.capture_path)} if self.capture_path else {}),
                # Exposures earlier steps of the same request fired (e.g. a first Refine attempt).
                **({"prior_exposures": self.prior_exposures}
                   if getattr(self, "prior_exposures", None) is not None else {})}


class CaptureFromUnprovenBody(CaptureOutcomeUncertain):
    """The CLI shutter ran, but the port's serial could not be proven after it: the frame may be
    from another body, or the right body dropped off USB. Never a success, never retried, never
    published. The downloaded bytes are preserved in quarantine (capture_path, quarantine)."""

    def __init__(self, detail: str, *, refusal: CameraIdentityError,
                 capture_path: Path | None = None, quarantine: dict | None = None):
        self.identity = refusal.as_dict() | {"may_have_fired": True}
        self.quarantine = quarantine
        super().__init__(f"wrong_body_possible: {detail}", capture_path=capture_path)


class RecordingStopUncertain(CameraError):
    """Recording may still be active; the camera is quarantined from further record attempts."""


class PlaceholderSettingsError(CameraError):
    """Every bounded re-read still returned Sony's 'not yet reported' exposure placeholders.

    Raised by `read_settings_checked` before the read is recorded, planned from or kept as a
    restore target, and so before the write or shutter that would have used it. It is a clean
    refusal of the next step, never a capture-uncertain outcome; writes or frames the caller
    completed before this read are the caller's to report."""

    may_have_fired = False
    retry_safe = True
    reason = "placeholder_settings"

    def __init__(self, detail: str, *, purpose: str, reads: list[dict]):
        self.purpose = purpose
        self.reads = reads
        # An earlier exposure of the same request (e.g. the meter frame) that stays preserved.
        self.capture_path: Path | None = None
        super().__init__(f"Camera settings read ({purpose}) returned placeholder values on "
                         f"{len(reads)} consecutive reads; the operation stopped at this read and "
                         f"issued no further setting write or shutter. Wake the camera (half-press the "
                         f"shutter), exit menus, and retry. {detail}")

    def as_dict(self) -> dict:
        """`may_have_fired` is about the refused next step; an earlier frame may exist (capture_path)."""
        return {"error": "placeholder_settings", "reason": self.reason, "purpose": self.purpose,
                "reads": self.reads, "detail": str(self), "may_have_fired": False,
                **({"earlier_capture_path": str(self.capture_path)} if self.capture_path else {}),
                # Exposures earlier steps of the same request fired (e.g. the meter frame).
                **({"prior_exposures": self.prior_exposures}
                   if getattr(self, "prior_exposures", None) is not None else {})}


def _quarantine_error(detail: str) -> RecordingStopUncertain:
    return RecordingStopUncertain(f"recording_stop_uncertain: {detail}")


def _quarantine_root_fd(*, create: bool) -> int:
    root = RECORDING_QUARANTINE_ROOT
    if create:
        try:
            root.mkdir(mode=0o700)
            # Make the newly created marker-directory entry durable as well as its
            # later contents; syncing only the new directory does not persist its parent.
            parent_fd = os.open(root.parent, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
            try:
                os.fsync(parent_fd)
            finally:
                os.close(parent_fd)
        except FileExistsError:
            # Reusing a root is safe only after the same validation required for
            # every later open.  In particular, do not treat a symlink, another
            # user's directory, or a permissive directory as an idempotent root.
            pass
        except OSError as exc:
            raise _quarantine_error("cannot create local quarantine root") from exc
    try:
        root_stat = root.lstat()
    except FileNotFoundError:
        raise
    except OSError as exc:
        raise _quarantine_error("cannot inspect local quarantine root") from exc
    if (
        stat.S_ISLNK(root_stat.st_mode)
        or not stat.S_ISDIR(root_stat.st_mode)
        or root_stat.st_uid != os.geteuid()
        or stat.S_IMODE(root_stat.st_mode) != 0o700
    ):
        raise _quarantine_error("local quarantine root is unsafe")
    try:
        root_fd = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    except OSError as exc:
        raise _quarantine_error("cannot open local quarantine root") from exc
    try:
        opened = os.fstat(root_fd)
        if (
            opened.st_dev != root_stat.st_dev
            or opened.st_ino != root_stat.st_ino
            or not stat.S_ISDIR(opened.st_mode)
            or opened.st_uid != os.geteuid()
            or stat.S_IMODE(opened.st_mode) != 0o700
        ):
            raise _quarantine_error("local quarantine root changed while opening")
        return root_fd
    except Exception:
        os.close(root_fd)
        raise


def _owned_quarantine_stat(root_fd: int) -> os.stat_result | None:
    try:
        file_stat = os.stat(RECORDING_QUARANTINE_NAME, dir_fd=root_fd, follow_symlinks=False)
    except FileNotFoundError:
        return None
    except OSError as exc:
        raise _quarantine_error("cannot inspect local quarantine sentinel") from exc
    if (
        stat.S_ISLNK(file_stat.st_mode)
        or not stat.S_ISREG(file_stat.st_mode)
        or file_stat.st_uid != os.geteuid()
        or file_stat.st_nlink != 1
        or file_stat.st_size > MAX_RECORDING_QUARANTINE_BYTES
    ):
        raise _quarantine_error("local quarantine sentinel is unsafe")
    return file_stat


def _read_quarantine_sentinel(root_fd: int, *, operation_id: str | None = None) -> bool:
    expected = _owned_quarantine_stat(root_fd)
    if expected is None:
        return False
    try:
        fd = os.open(RECORDING_QUARANTINE_NAME, os.O_RDONLY | os.O_NOFOLLOW, dir_fd=root_fd)
    except OSError as exc:
        raise _quarantine_error("cannot open local quarantine sentinel") from exc
    try:
        opened = os.fstat(fd)
        if opened.st_dev != expected.st_dev or opened.st_ino != expected.st_ino:
            raise _quarantine_error("local quarantine sentinel changed while opening")
        data = bytearray()
        remaining = MAX_RECORDING_QUARANTINE_BYTES + 1
        while remaining:
            chunk = os.read(fd, min(1024, remaining))
            if not chunk:
                break
            data.extend(chunk)
            remaining -= len(chunk)
        after = os.fstat(fd)
        if (
            after.st_dev != opened.st_dev
            or after.st_ino != opened.st_ino
            or after.st_size != opened.st_size
            or len(data) != opened.st_size
            or len(data) > MAX_RECORDING_QUARANTINE_BYTES
        ):
            raise _quarantine_error("local quarantine sentinel changed while reading")
    finally:
        os.close(fd)
    try:
        payload = json.loads(bytes(data).decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise _quarantine_error("local quarantine sentinel is corrupt") from exc
    if not isinstance(payload, dict) or payload.get("state") != "recording_stop_uncertain":
        raise _quarantine_error("local quarantine sentinel is corrupt")
    if operation_id is not None and payload.get("operation_id") != operation_id:
        raise _quarantine_error("active recording marker operation changed")
    return True


def recording_quarantine_active() -> bool:
    """Return durable quarantine state; unreadable local state fails closed."""
    global _recording_quarantined_process
    with _recording_quarantine_lock:
        if _recording_quarantined_process:
            return True
        try:
            root_fd = _quarantine_root_fd(create=False)
        except FileNotFoundError:
            return False
        try:
            active = _read_quarantine_sentinel(root_fd)
        finally:
            os.close(root_fd)
        if active:
            _recording_quarantined_process = True
        return active


def assert_recording_allowed() -> None:
    if recording_quarantine_active():
        raise _quarantine_error(
            "recording is quarantined; physically verify the device is idle, then run the local "
            "clear command with --verified-device-idle"
        )


def quarantine_recording_stop(reason: str, *, _exclusive: bool = False) -> tuple[int, int, str] | None:
    """Persist a fail-closed stop uncertainty before a caller can return an outcome."""
    global _recording_quarantined_process
    with _recording_quarantine_lock:
        if _exclusive and _recording_quarantined_process:
            raise _quarantine_error("another recording operation or stale marker is active")
        _recording_quarantined_process = True
        root_fd = _quarantine_root_fd(create=True)
        try:
            if _read_quarantine_sentinel(root_fd):
                if _exclusive:
                    raise _quarantine_error("another recording operation or stale marker is active")
                return
            operation_id = os.urandom(16).hex()
            payload = json.dumps(
                {"reason": str(reason)[:512], "state": "recording_stop_uncertain", "version": 1,
                 "operation_id": operation_id},
                sort_keys=True,
            ).encode("utf-8")
            try:
                fd = os.open(
                    RECORDING_QUARANTINE_NAME,
                    os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                    0o600,
                    dir_fd=root_fd,
                )
            except FileExistsError:
                _read_quarantine_sentinel(root_fd)
                if _exclusive:
                    raise _quarantine_error("another recording operation created the marker")
                return
            try:
                created = os.fstat(fd)
                if (
                    not stat.S_ISREG(created.st_mode)
                    or created.st_uid != os.geteuid()
                    or created.st_nlink != 1
                ):
                    raise _quarantine_error("new local quarantine sentinel is unsafe")
                written = 0
                while written < len(payload):
                    count = os.write(fd, payload[written:])
                    if count <= 0:
                        raise _quarantine_error("cannot persist local quarantine sentinel")
                    written += count
                os.fsync(fd)
                # The content fsync alone does not guarantee that a recreated
                # directory entry survives restart after an earlier verified
                # clear.  Sync the trusted directory before returning success.
                os.fsync(root_fd)
                return created.st_dev, created.st_ino, operation_id
            finally:
                os.close(fd)
        except RecordingStopUncertain:
            raise
        except OSError as exc:
            raise _quarantine_error("cannot persist local quarantine sentinel") from exc
        finally:
            os.close(root_fd)


def _owned_recording_marker(token: tuple[int, int, str], *, clear: bool = False) -> None:
    """Validate/clear only the durable marker created by this live operation."""
    global _recording_quarantined_process
    with _recording_quarantine_lock:
        try:
            root_fd = _quarantine_root_fd(create=False)
        except FileNotFoundError as exc:
            raise _quarantine_error("active recording marker disappeared") from exc
        try:
            expected = _owned_quarantine_stat(root_fd)
            if expected is None or (expected.st_dev, expected.st_ino) != token[:2]:
                raise _quarantine_error("active recording marker ownership changed")
            _read_quarantine_sentinel(root_fd, operation_id=token[2])
            if clear:
                os.unlink(RECORDING_QUARANTINE_NAME, dir_fd=root_fd)
                os.fsync(root_fd)
                _recording_quarantined_process = False
        except OSError as exc:
            raise _quarantine_error("cannot validate or clear active recording marker") from exc
        finally:
            os.close(root_fd)


def clear_recording_quarantine(*, verified_device_idle: bool) -> None:
    """Local-only clearance after physical confirmation that the camera is no longer recording."""
    global _recording_quarantined_process
    if not verified_device_idle:
        raise ValueError("refusing to clear recording quarantine without verified_device_idle=True")
    with _recording_quarantine_lock:
        try:
            root_fd = _quarantine_root_fd(create=False)
        except FileNotFoundError:
            _recording_quarantined_process = False
            return
        try:
            expected = _owned_quarantine_stat(root_fd)
            if expected is not None:
                current = os.stat(RECORDING_QUARANTINE_NAME, dir_fd=root_fd, follow_symlinks=False)
                if current.st_dev != expected.st_dev or current.st_ino != expected.st_ino:
                    raise _quarantine_error("local quarantine sentinel changed before clearance")
                os.unlink(RECORDING_QUARANTINE_NAME, dir_fd=root_fd)
                os.fsync(root_fd)
            _recording_quarantined_process = False
        except OSError as exc:
            raise _quarantine_error("cannot safely remove local quarantine sentinel") from exc
        finally:
            os.close(root_fd)


def _max_native_record_seconds() -> float:
    raw = os.getenv("CAMERA_AUTOPILOT_MAX_NATIVE_RECORD_SECONDS", "").strip()
    if not raw:
        return DEFAULT_MAX_NATIVE_RECORD_SECONDS
    try:
        seconds = float(raw)
    except ValueError:
        return DEFAULT_MAX_NATIVE_RECORD_SECONDS
    if not math.isfinite(seconds) or seconds < MIN_RECORD_SECONDS:
        return DEFAULT_MAX_NATIVE_RECORD_SECONDS
    return seconds


def bounded_record_seconds(seconds: float) -> tuple[float, float]:
    try:
        requested = float(seconds)
    except (TypeError, ValueError) as exc:
        raise CameraError("native video duration must be a finite number of seconds") from exc
    if not math.isfinite(requested) or requested <= 0:
        raise CameraError("native video duration must be a finite positive number of seconds")
    max_seconds = _max_native_record_seconds()
    return min(max(MIN_RECORD_SECONDS, requested), max_seconds), max_seconds


@contextmanager
def _wrap_gphoto(action: str):
    """Translate a raw gphoto2 failure into a CameraError so callers have one type to catch."""
    try:
        yield
    except gp.GPhoto2Error as exc:
        raise CameraError(f"{action} failed ({exc}).") from exc


def _coerce_widget_target(widget, target: str):
    """Match python-gphoto2's expected Python value type for non-menu widgets."""
    try:
        widget_type = widget.get_type()
    except Exception:
        return target
    if widget_type == gp.GP_WIDGET_RANGE:
        return float(target)
    if widget_type == gp.GP_WIDGET_TOGGLE:
        lowered = target.strip().lower()
        if lowered in {"on", "true"}:
            return 1
        if lowered in {"off", "false"}:
            return 0
        return int(float(target))
    return target


def _widget_value_matches(widget, current: str, target: str) -> bool:
    try:
        widget_type = widget.get_type()
    except Exception:
        widget_type = None
    if widget_type == gp.GP_WIDGET_RANGE:
        try:
            return abs(float(current) - float(target)) <= 0.01
        except ValueError:
            return False
    return current == target


@dataclass
class CameraInfo:
    model: str
    port: str


# Discovery establishes identity instead of trusting the driver's label:
# the gphoto2 label and the port both lie — a real 2026-09-20 diagnosis had
# gphoto2 --auto-detect listing the A7 II as "Apple iPad Air" while a real iPad was also attached.
SUPPORTED_USB_MODELS = ("Sony ILCE-7M2 (Control)", "Sony Alpha-A7 III (PC Control)")
# ioreg "USB Product Name" / "kUSBProductString" -> the exact libgphoto2 driver model string.
_IOREG_PRODUCT_TO_MODEL = {
    "ILCE-7M2": "Sony ILCE-7M2 (Control)",
    "ILCE-7M3": "Sony Alpha-A7 III (PC Control)",
}
_SONY_USB_VENDOR_ID = 1356  # 0x054C
MAX_RECOVERY_PROBES = 4  # bounded read-only recovery probes per discovery pass
_SERIAL_NUMBER_NODE = "serialnumber"  # /main/status/serialnumber — a harmless read


def _autodetect_rows() -> list[tuple[str, str]]:
    """Raw gphoto2 autodetect rows [(model, port), ...] — the driver's own (fallible) view."""
    with _wrap_gphoto("detect cameras"):
        return [(name, addr) for name, addr in gp.Camera.autodetect()]


def _parse_ioreg_usb(text: str) -> list[dict[str, str]]:
    """Group `ioreg -p IOUSB -l` output into one property dict per USB node.

    Each node is introduced by a `+-o` header; the `"key" = value` lines that follow it belong
    to that node (children arrive under their own later header), so properties attach to the most
    recent header. String values are unquoted; other scalars are kept verbatim.
    """
    devices: list[dict[str, str]] = []
    current: dict[str, str] | None = None
    for line in text.splitlines():
        if "+-o " in line:
            if current is not None:
                devices.append(current)
            current = {}
            continue
        if current is None:
            continue
        # ioreg -l indents each property under a tree of "  | | " prefixes, so match the
        # "key" = value pair anywhere on the line rather than anchoring at the start.
        match = re.search(r'"([^"]+)"\s*=\s*(.*)', line)
        if not match:
            continue
        key, raw = match.group(1), match.group(2).strip()
        if len(raw) >= 2 and raw[0] == '"' and raw[-1] == '"':
            raw = raw[1:-1]
        current[key] = raw
    if current is not None:
        devices.append(current)
    return devices


def _read_os_usb_devices() -> list[dict[str, str]] | None:
    """The OS view of attached USB devices, or None when no OS source of truth is available.

    macOS reads `ioreg`; every other platform (a Linux puck is the future target) returns None so
    the caller falls back to plain autodetect and says so, rather than hard-failing there.
    """
    if sys.platform != "darwin":
        return None
    try:
        result = subprocess.run(["ioreg", "-p", "IOUSB", "-l"],
                                capture_output=True, text=True, timeout=5)
    except (OSError, subprocess.TimeoutExpired):
        return None
    if result.returncode != 0 or not result.stdout:
        return None
    return _parse_ioreg_usb(result.stdout)


def _is_sony(device: dict[str, str]) -> bool:
    if device.get("USB Vendor Name", "").strip().lower() == "sony":
        return True
    try:
        return int(device.get("idVendor", "")) == _SONY_USB_VENDOR_ID
    except ValueError:
        return False


def _gphoto_port_for(device: dict[str, str]) -> str | None:
    """Derive libgphoto2's `usb:BBB,DDD` port from a macOS ioreg USB record.

    On macOS libusb takes the bus from the top byte of the IOUSB locationID and the device from
    `USB Address` (verified 2026-09-20 against both bodies: A7 III locationID 1245184 / addr 3 ->
    usb:000,003, A7 II 1310720 / addr 4 -> usb:000,004, a real iPad 34799616 / addr 4 ->
    usb:002,004). A wrong derivation only costs a failed probe; it never mislabels a body.
    """
    try:
        location = int(device["locationID"])
        address = int(device["USB Address"])
    except (KeyError, ValueError):
        return None
    return f"usb:{(location >> 24) & 0xFF:03d},{address:03d}"


def _os_supported_bodies(devices: list[dict[str, str]]) -> list[dict]:
    """Supported Sony bodies the OS reports as physically present, with their gphoto ports."""
    bodies: list[dict] = []
    for device in devices:
        if not _is_sony(device):
            continue
        product = (device.get("USB Product Name") or device.get("kUSBProductString") or "").strip()
        model = _IOREG_PRODUCT_TO_MODEL.get(product)
        if model is None:
            continue
        port = _gphoto_port_for(device)
        if port is None:
            continue
        serial = (device.get("kUSBSerialNumberString") or "").strip() or None
        bodies.append({"model": model, "port": port, "serial": serial})
    return bodies


# Linux: the same identity data from sysfs, no subprocess. Tests point SYSFS_ROOT at a fake tree.
SYSFS_ROOT = Path("/sys")
_SONY_USB_VENDOR_HEX = "054c"  # sysfs idVendor is lower-case hex without 0x


def _read_sysfs_attr(path: Path, exact: bool = False) -> str | None:
    """One sysfs attribute; None when missing, unreadable, non-ASCII, empty or all whitespace.

    exact=True (the serial) removes only the one b"\\n" the kernel appends, so " S " and "S", or
    "S\\r" and "S", stay different identities; otherwise surrounding whitespace is stripped. Bytes are
    decoded directly because read_text() would turn "\\r\\n" into "\\n" first.
    """
    try:
        text = path.read_bytes().decode("ascii")
    except (OSError, UnicodeDecodeError):
        return None
    if not text.strip():
        return None
    if not exact:
        return text.strip()
    return text[:-1] if text.endswith("\n") else text


def _read_sysfs_usb_devices(sys_root: Path | None = None) -> list[dict[str, str | None]] | None:
    """Sony USB devices as Linux sysfs reports them, or None when sysfs cannot be listed.

    Every attribute of a record is read from the SAME device directory, so a serial is only ever
    paired with that node's own busnum/devnum — never by list position. Interface nodes
    ("1-1:1.0") are skipped, and a non-Sony device is dropped after reading only its idVendor, so
    its serial is never collected.
    """
    base = (sys_root if sys_root is not None else SYSFS_ROOT) / "bus" / "usb" / "devices"
    try:
        entries = sorted(base.iterdir())
    except OSError:
        return None
    devices: list[dict[str, str | None]] = []
    for entry in entries:
        if ":" in entry.name:
            continue
        vendor = _read_sysfs_attr(entry / "idVendor")
        if (vendor or "").lower() != _SONY_USB_VENDOR_HEX:
            continue
        # Attributes are separate reads, so the node can be replaced between them (unplug and
        # replug on the same path). busnum/devnum are read before and after the serial, and the
        # vendor is re-read inside that window; if any changed, the serial may belong to another
        # enumeration (or a non-Sony device) and the row is dropped.
        busnum, devnum = _read_sysfs_attr(entry / "busnum"), _read_sysfs_attr(entry / "devnum")
        record = {"sysfs_name": entry.name, "idVendor": vendor,
                  "idProduct": _read_sysfs_attr(entry / "idProduct"),
                  "product": _read_sysfs_attr(entry / "product"),
                  "serial": _read_sysfs_attr(entry / "serial", exact=True),
                  "busnum": busnum, "devnum": devnum}
        after = (_read_sysfs_attr(entry / "idVendor"), _read_sysfs_attr(entry / "busnum"),
                 _read_sysfs_attr(entry / "devnum"))
        if after != (vendor, busnum, devnum):
            continue
        devices.append(record)
    return devices


def _sysfs_gphoto_port(device: dict[str, str | None]) -> str | None:
    """libgphoto2's `usb:BBB,DDD` port for one sysfs node, from that node's busnum/devnum.

    libgphoto2 (libgphoto2_port/libusb1/libusb1.c) formats the port as "usb:%03d,%03d" from
    libusb_get_bus_number()/libusb_get_device_address(), which libusb's Linux backend
    (libusb/os/linux_usbfs.c) reads from these same sysfs "busnum"/"devnum" attributes.
    """
    busnum, devnum = device.get("busnum"), device.get("devnum")
    if not (busnum and devnum and busnum.isdigit() and devnum.isdigit()):
        return None
    return f"usb:{int(busnum):03d},{int(devnum):03d}"


def _sysfs_supported_bodies(devices: list[dict[str, str | None]]) -> list[dict]:
    """Supported Sony bodies sysfs reports as present, in the shape _os_supported_bodies returns.

    The sysfs "product" attribute is the same USB iProduct descriptor ioreg shows as
    "USB Product Name". A missing or empty serial stays None: it is unavailable, never guessed.
    """
    bodies: list[dict] = []
    for device in devices:
        if (device.get("idVendor") or "").lower() != _SONY_USB_VENDOR_HEX:
            continue
        model = _IOREG_PRODUCT_TO_MODEL.get((device.get("product") or "").strip())
        if model is None:
            continue
        port = _sysfs_gphoto_port(device)
        if port is None:
            continue
        bodies.append({"model": model, "port": port, "serial": device.get("serial")})
    return bodies


def _probe_camera_answers(model: str, port: str) -> bool:
    """Read-only identity probe: does `model` at `port` answer a harmless config read?

    Claims the body only long enough to read /main/status/serialnumber, then releases it. NEVER
    triggers the shutter and NEVER writes a setting. Releases Apple's auto-respawning PTP owner
    first (macOS) so the claim can succeed. Any gphoto2 failure means "did not answer" -> False.
    """
    _release_macos_camera_service()
    camera = gp.Camera()
    try:
        ports = gp.PortInfoList()
        ports.load()
        camera.set_port_info(ports[ports.lookup_path(port)])
        abilities = gp.CameraAbilitiesList()
        abilities.load()
        camera.set_abilities(abilities[abilities.lookup_model(model)])
        camera.init()
    except gp.GPhoto2Error:
        try:
            camera.exit()
        except gp.GPhoto2Error:
            pass
        return False
    try:
        config = camera.get_config()
        try:
            config.get_child_by_name(_SERIAL_NUMBER_NODE).get_value()
        except gp.GPhoto2Error:
            pass  # init + get_config already proves the body answers; the node is a bonus
        return True
    except gp.GPhoto2Error:
        return False
    finally:
        try:
            camera.exit()
        except gp.GPhoto2Error:
            pass


def _os_identity() -> tuple[str, list[dict] | None]:
    """(os_source, supported Sony bodies the OS reports) — bodies is None without an OS source."""
    devices = _read_os_usb_devices()
    if devices is not None:
        return "ioreg", _os_supported_bodies(devices)
    if sys.platform.startswith("linux") and (sysfs_devices := _read_sysfs_usb_devices()) is not None:
        return "sysfs", _sysfs_supported_bodies(sysfs_devices)
    if sys.platform.startswith("linux"):
        return "sysfs_unavailable", None
    return ("non_darwin" if sys.platform != "darwin" else "ioreg_unavailable"), None


def _duplicate_serials(os_bodies: list[dict]) -> set[str]:
    """A serial reported by more than one node identifies neither: no probe, no identity.
    Distinct (port, serial) pairs: the same device recorded twice is not two devices."""
    serial_counts = Counter(serial for _port, serial in
                            {(body["port"], body["serial"]) for body in os_bodies if body["serial"]})
    return {serial for serial, count in serial_counts.items() if count > 1}


# How a connection proves which body it holds. The OS USB descriptor serial is what discovery,
# the web picker and the route matrix identify bodies by (A7 II A7II00SERIAL, A7 III A7III0SERIAL).
# The PTP DeviceInfo serial (/main/status/serialnumber) is NOT used: no receipt in this repository
# shows its value on either body, so its equality with the USB serial is unproven.
SERIAL_VERIFICATION_METHOD = "os_usb_serial_at_connected_port"


def verify_serial_at_port(model: str, port: str, expected_serial: str | None) -> dict:
    """Prove that the supported body the OS reports at `port` carries `expected_serial`.

    Reads only the OS (macOS ioreg, Linux sysfs); never opens the camera. Returns the verification
    receipt, or raises CameraIdentityError when the serial is absent, duplicated, different, or
    cannot be read. Callers run it while holding the USB claim (so the port cannot be re-assigned)
    and again immediately before a CLI shutter that re-opens the port.
    """
    def refuse(reason: str, detail: str, observed: str | None = None):
        raise CameraIdentityError(detail, reason=reason, expected_serial=expected_serial,
                                  observed_serial=observed, model=model, port=port)

    if not expected_serial:
        refuse("serial_required", "Select the camera again: its USB serial is required before capture.")
    # A reader that raises or returns something malformed proves nothing: that is an identity
    # refusal (before a shutter: nothing fires; after one: the download is quarantined unproven).
    try:
        os_source, os_bodies = _os_identity()
    except Exception as exc:  # noqa: BLE001 — any reader failure is the same missing proof
        refuse("serial_unavailable", f"The OS USB reader failed ({type(exc).__name__}: {exc}); "
               f"nothing can confirm which body is at {port}.")
    if os_bodies is None:
        refuse("serial_unavailable", f"No OS USB source ({os_source}) can confirm which body is at {port}.")
    if not isinstance(os_bodies, list) or not all(
            isinstance(body, dict) and {"model", "port", "serial"} <= set(body) for body in os_bodies):
        refuse("serial_unavailable", f"The OS USB reader returned an unreadable listing; nothing can "
               f"confirm which body is at {port}.")
    at_port = [body for body in os_bodies if body["port"] == port]
    if not at_port:
        refuse("not_present", f"The OS reports no supported Sony body at {port}.")
    if len({(body["model"], body["serial"]) for body in at_port}) > 1:
        refuse("serial_duplicate", f"The OS reports more than one body at {port}.")
    body = at_port[0]
    if body["model"] != model:
        refuse("model_mismatch", f"The OS reports {body['model']} at {port}, not {model}.")
    if not body["serial"]:
        refuse("serial_missing", f"The body at {port} reports no USB serial.")
    if body["serial"] in _duplicate_serials(os_bodies):
        refuse("serial_duplicate", f"USB serial {body['serial']} is reported by more than one device.",
               body["serial"])
    if body["serial"] != expected_serial:
        refuse("serial_mismatch", f"Expected USB serial {expected_serial} at {port}, found "
               f"{body['serial']}. USB ports re-enumerate; re-detect and select the camera again.",
               body["serial"])
    return {"serial": body["serial"], "method": SERIAL_VERIFICATION_METHOD, "os_source": os_source,
            "model": model, "port": port}


def detect_identified() -> list[dict]:
    """Identity-first discovery: establish which bodies are present from the OS, then trust or
    recover each gphoto row instead of believing its label.

    Returns rows of {"model", "port", "serial", "method", "os_source"} where:
      * method "autodetect"      — gphoto's own row (a supported body it named correctly, or any
                                    non-supported/unknown row, passed through untouched);
      * method "recovered_probe" — the OS saw a supported Sony body that autodetect missed or
                                    mislabelled, and a read-only probe confirmed it answers there.
    "serial" is the OS (USB descriptor) serial when known, else None. "os_source" is "ioreg"
    (macOS), "sysfs" (Linux), "ioreg_unavailable", "sysfs_unavailable", or "non_darwin" (any other
    platform). On sysfs a Sony body without a readable serial is never recovered by probe: with
    no serial there is no identity to attach, so its row (if gphoto lists it) keeps serial None.
    A serial reported by more than one OS node is no identity either: those bodies are never
    probed, and their rows carry serial None plus "serial_status": "duplicate" (whatever gphoto
    labelled them), as do rows at a port the OS lists with two different bodies.
    detect() is the compatible [(model, port), ...] view.
    """
    rows = _autodetect_rows()
    os_source, os_bodies = _os_identity()
    if os_bodies is None:
        return [{"model": name, "port": port, "serial": None,
                 "method": "autodetect", "os_source": os_source} for name, port in rows]

    os_by_port = {body["port"]: body for body in os_bodies}
    # One body the OS records twice (same port, model and serial) is one body, not two.
    distinct_bodies = list({(body["port"], body["model"], body["serial"]): body
                            for body in os_bodies}.values())
    present_by_model = Counter(body["model"] for body in distinct_bodies)
    duplicate_serials = _duplicate_serials(os_bodies)
    # A port the OS lists with two different bodies names neither (verify_serial_at_port refuses it
    # too): never probed, and no row there gets a serial.
    bodies_per_port = Counter(body["port"] for body in distinct_bodies)
    conflicting_ports = {port for port, count in bodies_per_port.items() if count > 1}
    # A supported label is trusted only where the OS does not name another body at that port:
    # gphoto has listed the A7 III as "Sony ILCE-7M2 (Control)", and that row must not block the
    # A7 III's read-only recovery (and with it the serial every capture tool now requires).
    supported_ports = {port for name, port in rows if name in SUPPORTED_USB_MODELS
                       and os_by_port.get(port, {}).get("model", name) == name}

    # Recover supported bodies the OS sees that no supported autodetect row already claims.
    recovered: dict[str, dict] = {}
    probes_used = 0
    for body in distinct_bodies:
        if body["port"] in supported_ports:
            continue  # a trusted autodetect row already names this body
        if body["port"] in conflicting_ports:
            continue  # the OS lists two bodies at this port -> no identity to recover; fail closed
        if present_by_model[body["model"]] > 1:
            continue  # two identical models: a probe can't attribute a serial -> fail closed
        if body["serial"] in duplicate_serials:
            continue  # shared serial -> ambiguous identity; fail closed
        if os_source == "sysfs" and not body["serial"]:
            continue  # no serial -> no identity to recover; never identify by port or label
        if probes_used >= MAX_RECOVERY_PROBES:
            break
        probes_used += 1
        if _probe_camera_answers(body["model"], body["port"]):
            recovered[body["port"]] = {"model": body["model"], "port": body["port"],
                                       "serial": body["serial"], "method": "recovered_probe",
                                       "os_source": os_source}

    identified: list[dict] = []
    for name, port in rows:
        if port in recovered:
            continue  # a mislabelled row is superseded by the recovered identity at this port
        os_match = os_by_port.get(port)
        serial = os_match["serial"] if (os_match and os_match["model"] == name) else None
        row = {"model": name, "port": port, "serial": serial,
               "method": "autodetect", "os_source": os_source}
        # Duplicate status follows the OS record at the port, whatever gphoto labelled it.
        if port in conflicting_ports or (os_match and os_match["serial"] in duplicate_serials):
            row.update(serial=None, serial_status="duplicate")
        identified.append(row)
    identified.extend(recovered.values())
    return identified


def detect() -> list[tuple[str, str]]:
    """List cameras without claiming them, as [(model, port), ...].

    Compatible view over detect_identified(), so a body the driver mislabels (or drops) still
    appears once the OS confirms it is present. See detect_identified() for the identification
    method and OS serial behind each row.
    """
    return [(row["model"], row["port"]) for row in detect_identified()]


def camera_for_serial(serial: str | None, **kwargs) -> "Camera":
    """A Camera for the one attached body that reports `serial`, at today's port (ports
    re-enumerate). Discovery only; the returned Camera proves the serial again on connect."""
    if not serial:
        raise CameraIdentityError("A USB serial is required to choose a camera.", reason="serial_required")
    rows = [row for row in detect_identified() if row.get("serial") == serial]
    if len(rows) != 1:
        raise CameraIdentityError(
            f"USB serial {serial} is {'not attached' if not rows else 'reported by more than one row'}; "
            "re-detect and pass the serial discovery reports.",
            reason="not_present" if not rows else "serial_duplicate", expected_serial=serial)
    return Camera(model=rows[0]["model"], port=rows[0]["port"], serial=serial, **kwargs)



def sony_capture_deadline(shutter_setting) -> dict:
    """Bound the combined CLI operation using confirmed shutter configuration.

    Allow an equal-duration dark frame because long-exposure noise-reduction
    state is unverified, then add 18 seconds for transfer/driver work. These are
    bounded allowances, never a claim about measured sensor-open time.
    """
    if not isinstance(shutter_setting, str):
        raise CameraError("Cannot bound Sony capture: confirmed shutter setting is missing; no shutter was issued")
    label = shutter_setting.strip()
    if label == "65535/65535":  # Sony's "not yet reported" shutter word (as autoexpose._exposure_value)
        raise CameraError("Cannot bound Sony capture: the body reported its shutter placeholder 65535/65535 "
                          "(value not yet read); no shutter was issued")
    try:
        if "/" in label:
            numerator, denominator = label.split("/")
            seconds = float(numerator) / float(denominator)
        else:
            seconds = float(label)
    except (ValueError, ZeroDivisionError, OverflowError) as exc:
        raise CameraError("Cannot bound Sony capture: Bulb or unparseable shutter is unsupported; no shutter was issued") from exc
    if not math.isfinite(seconds) or not 1 / 8000 <= seconds <= 30:
        raise CameraError("Cannot bound Sony capture: shutter must be between 1/8000 and 30 seconds; no shutter was issued")
    return {"confirmed_shutter_setting": label, "configured_shutter_s": seconds,
            "possible_dark_frame_s": seconds,
            "dark_frame_state": "unverified; possible equal-duration noise-reduction dark frame",
            "transfer_allowance_s": 18.0, "subprocess_timeout_s": min(78.0, 2 * seconds + 18.0),
            "sensor_exposure_measured": False,
            "policy": "confirmed configured shutter plus possible equal-duration dark frame and bounded driver/transfer allowance"}


def validate_sony_capture_shutter(shutter_setting) -> dict:
    """Preflight only: no camera command; rejects unverified long-shutter transport."""
    deadline = sony_capture_deadline(shutter_setting)
    if deadline["configured_shutter_s"] > SONY_ACTION_MAX_ENABLED_SHUTTER_S:
        raise CameraError("Sony USB captures longer than 1 second are disabled pending transport verification; no shutter was issued")
    return deadline


def sony_long_exposure_deadline(shutter_setting) -> dict:
    """Bound the long route's trigger/wait CLI from the confirmed shutter.

    The event wait covers the exposure, a possible equal-length Long Exposure NR dark frame and a
    transfer allowance; the process timeout adds a margin over that wait. Both scale with the
    exposure and are bounded allowances, never a measurement of sensor-open time.
    """
    base = sony_capture_deadline(shutter_setting)
    seconds = base["configured_shutter_s"]
    wait_s = math.ceil(2 * seconds + LONG_EXPOSURE_WAIT_ALLOWANCE_S - 1e-9)
    return {**base, "event_wait_s": wait_s,
            "subprocess_timeout_s": wait_s + LONG_EXPOSURE_TIMEOUT_MARGIN_S,
            "transfer_allowance_s": LONG_EXPOSURE_WAIT_ALLOWANCE_S,
            "minimum_settle_s": 2 * seconds + 5,
            "policy": "trigger once, then wait 2x shutter plus a transfer allowance for the camera's files"}


def validate_sony_long_exposure_shutter(shutter_setting, live_shutter_options) -> dict:
    """Preflight only (no camera command): the long route accepts only a shutter above the ordinary
    1 s cap, at most SONY_LONG_EXPOSURE_MAX_SHUTTER_S, on Sony's third-stop ladder, and spelled
    exactly as this body lists it (the A7 II writes 3.2 s as "32/10"; never another body's label)."""
    deadline = sony_long_exposure_deadline(shutter_setting)
    seconds = deadline["configured_shutter_s"]
    label = deadline["confirmed_shutter_setting"]
    if seconds <= SONY_ACTION_MAX_ENABLED_SHUTTER_S:
        raise CameraError(f"{label} s belongs to the ordinary capture route, not the long-exposure route; "
                          "no shutter was issued")
    if seconds > SONY_LONG_EXPOSURE_MAX_SHUTTER_S:
        raise CameraError(f"Long exposures are limited to {SONY_LONG_EXPOSURE_MAX_SHUTTER_S:g} s; "
                          "no shutter was issued")
    if not any(abs(seconds - stop) / stop < 0.02 for stop in SONY_LONG_EXPOSURE_LADDER_S):
        raise CameraError(f"{label} s is not on the Sony shutter ladder; no shutter was issued")
    if not live_shutter_options or label not in {str(option).strip() for option in live_shutter_options}:
        raise CameraError(f"This body does not list the shutter {label!r}; no shutter was issued")
    return deadline


# Sony's "not yet reported" exposure values. The A7 III answered the first settings read after
# idle with f/0, ISO 0 and shutter 65535/65535 (live, 2026-09-22); a second read was normal.
# libgphoto2 renders an all-ones Sony shutter word as "65535/65535", an ISO word of 0 as "0" and an
# F-number word of 0 (or all-ones 0xFFFF / 100) as "f/0" ("f/655.35"). Bulb is a real dial position.
PLACEHOLDER_REREADS = 3      # extra reads after a placeholder read (4 reads in total)
PLACEHOLDER_SETTLE_S = 0.5   # pause before each re-read
_PLACEHOLDER_F_NUMBERS = (0.0, 655.35)
_PLACEHOLDER_SHUTTER_WORDS = (65535, 0xFFFFFFFF)
# libgphoto2's label for a Sony focus-mode word of 0. The A7 III answered it on the first read after
# waking from auto power-off (live, 2026-09-29, beside ISO 0 and 65535/65535); it is no choice of
# the focusmode node, so restoring it fails ("Valid options: Manual, Automatic, AF-C, ...").
_PLACEHOLDER_FOCUS_MODES = ("Undefined",)


def placeholder_fields(settings: dict) -> list[str]:
    """Which of aperture/focus_mode/iso/shutter hold a Sony placeholder (sorted; empty when none do).

    - iso: a numeric value of 0 ("0").
    - shutter: "65535/65535" (either half 65535), the raw all-ones word 4294967295, or a numeric
      label that is not a positive finite duration ("0", "1/0"). "Bulb" and missing values are not.
    - aperture: an F-number of 0 ("f/0", "0") or 655.35 (raw 0xFFFF). A manual or adapted lens also
      reports f/0 legitimately, so `read_settings_checked` treats a lone aperture differently.
    - focus_mode: "Undefined" (a focus-mode word of 0; see `read_focus_mode_checked` for a read
      checked against the node's choices).
    Missing (None) or non-numeric labels such as "Auto ISO" are not placeholders.
    """
    fields = []
    aperture = settings.get("aperture")
    if aperture is not None:
        try:
            f_number = float(str(aperture).strip().lower().removeprefix("f/"))
            if any(abs(f_number - value) < 1e-9 for value in _PLACEHOLDER_F_NUMBERS):
                fields.append("aperture")
        except ValueError:
            pass
    iso = settings.get("iso")
    if iso is not None:
        try:
            if float(str(iso).strip()) == 0:
                fields.append("iso")
        except ValueError:
            pass
    shutter = settings.get("shutter")
    label = str(shutter).strip() if shutter is not None else ""
    if re.fullmatch(r"\d+(\.\d+)?(/\d+(\.\d+)?)?", label):
        parts = [float(part) for part in label.split("/")]
        seconds = parts[0] / parts[1] if len(parts) == 2 and parts[1] else (parts[0] if len(parts) == 1 else 0.0)
        if any(part in _PLACEHOLDER_SHUTTER_WORDS for part in parts) or not (math.isfinite(seconds) and seconds > 0):
            fields.append("shutter")
    if str(settings.get("focus_mode")).strip() in _PLACEHOLDER_FOCUS_MODES:
        fields.append("focus_mode")
    return sorted(fields)


def read_settings_checked(camera, *, purpose: str, rereads: int = PLACEHOLDER_REREADS,
                          settle_s: float = PLACEHOLDER_SETTLE_S) -> tuple[dict, dict | None]:
    """Read settings that will be recorded, planned from or restored, refusing placeholder reads.

    Re-reads up to `rereads` times, `settle_s` apart, while the read holds a placeholder. A
    placeholder that survives every read raises PlaceholderSettingsError before the caller uses it.
    One exception: exactly f/0 on at least the last two reads, each beside a reported real ISO and
    shutter, is kept, because a manual or adapted lens reports it (Camera.wake accepts it for the
    same reason); the receipt says `aperture_unreported`. Returns (settings, receipt); receipt is None when the first
    read was clean, otherwise it records every placeholder read for the caller's evidence.
    """
    history: list[dict] = []
    lone_f0_reads = 0  # trailing consecutive reads showing exactly f/0 beside a real ISO and shutter
    for read in range(1, rereads + 2):
        if read > 1:
            time.sleep(settle_s)
        settings = dict(camera.get_settings())
        fields = placeholder_fields(settings)
        if not fields:
            break
        history.append({"read": read, "fields": fields,
                        "values": {key: settings.get(key) for key in fields}})
        lone_f0_reads = lone_f0_reads + 1 if _is_lone_f0(settings, fields) else 0
    else:
        # A manual/adapted lens: exactly f/0 on at least the last two reads, each beside a reported,
        # non-placeholder ISO and shutter. Anything else (655.35, a missing ISO/shutter, one late
        # lone f/0 after full placeholders) refuses.
        if lone_f0_reads < 2:
            raise PlaceholderSettingsError(
                f"Last read: {history[-1]['values']}.", purpose=purpose, reads=history)
    if not history:
        return settings, None
    return settings, {"purpose": purpose, "reads": read, "placeholder_reads": history,
                      "aperture_unreported": bool(fields),
                      "policy": f"re-read up to {rereads} times, {settle_s:g} s apart; ISO/shutter "
                                "placeholders refuse; f/0 on the last two reads beside a real ISO and "
                                "shutter is a manual lens"}


def read_focus_mode_checked(camera, *, purpose: str, choices=None, rereads: int = PLACEHOLDER_REREADS,
                            settle_s: float = PLACEHOLDER_SETTLE_S) -> tuple[str, dict | None]:
    """Read the focus mode that will be recorded or restored, refusing a placeholder read.

    A placeholder is "Undefined", or (when `choices` is given, e.g. the focusmode node's choices
    from `focus_capabilities`) any value that is not one of them: restoring it could only fail.
    Re-reads up to `rereads` times, `settle_s` apart, like `read_settings_checked`; a placeholder
    that survives every read raises PlaceholderSettingsError before the caller writes anything.
    Returns (mode, receipt); receipt is None when the first read was clean.
    """
    valid = [str(choice) for choice in choices] if choices else None
    history: list[dict] = []
    for read in range(1, rereads + 2):
        if read > 1:
            time.sleep(settle_s)
        mode = str(camera.get_focus_mode())
        if mode.strip() not in _PLACEHOLDER_FOCUS_MODES and (valid is None or mode in valid):
            break
        history.append({"read": read, "fields": ["focus_mode"], "values": {"focus_mode": mode}})
    else:
        raise PlaceholderSettingsError(
            f"Last read: {history[-1]['values']}" + (f"; the node's choices are {valid}." if valid else "."),
            purpose=purpose, reads=history)
    if not history:
        return mode, None
    return mode, {"purpose": purpose, "reads": read, "placeholder_reads": history,
                  "policy": f"re-read up to {rereads} times, {settle_s:g} s apart; a focus mode that is "
                            "'Undefined' or not one of the node's choices refuses"}


def placeholder_refusal(exc: BaseException) -> dict | None:
    """The structured refusal to carry beside a kept photo when a read after it was refused."""
    return exc.as_dict() if isinstance(exc, PlaceholderSettingsError) else None


def _is_lone_f0(settings: dict, fields: list[str]) -> bool:
    if fields != ["aperture"] or settings.get("iso") is None or settings.get("shutter") is None:
        return False
    return float(str(settings["aperture"]).strip().lower().removeprefix("f/")) == 0.0

class Camera:
    """A connected Sony camera. Use as a context manager or call connect()/disconnect().

    Not thread-safe: libgphoto2 holds an exclusive USB claim, so callers must serialize access
    to a single Camera instance (the MCP server does this with a lock).
    """

    def __init__(self, model: str | None = None, port: str | None = None, serial: str | None = None,
                 *, allow_unverified_identity: bool = False):
        # Exact gphoto identities; never silently pick a different body when two are attached.
        self._requested_model = model if model is not None else os.environ.get("CAMERA_MODEL") or None
        self._requested_port = port if port is not None else os.environ.get("CAMERA_PORT") or None
        # With an expected USB serial, every connect (and reconnect) proves the body at the port
        # carries it before returning, and the CLI shutter re-checks it. Without one the connection
        # is unverified, and capture()/movie start refuse on it unless the caller explicitly passes
        # allow_unverified_identity=True (owner-run dev tools only; never a product path).
        self._expected_serial = serial or None
        self._allow_unverified_identity = allow_unverified_identity is True
        self.verified_serial: str | None = None
        self.serial_verification: dict | None = None
        # Where identity-unproven downloads are preserved (None: _default_quarantine_dir()).
        self.quarantine_dir: Path | None = None
        self._selected: tuple[str, str] | None = None
        self.last_capture_receipt: dict | None = None
        self.last_reconnect_attempts: list[dict] | None = None
        self._camera: gp.Camera | None = None
        self._port: str = "usb"
        self._recording_quarantined = False
        self._active_recording_token: tuple[int, int, str] | None = None
        # The A7 II movie button is a toggle, so retrying an uncertain stop could restart recording.
        self.stop_movie_retry_safe = False

    def __enter__(self) -> Camera:
        self.connect()
        return self

    def __exit__(self, *exc) -> None:
        self.disconnect()

    # --- connection -------------------------------------------------------
    def connect(self) -> CameraInfo:
        if self._camera is not None:
            return self.info()
        with _wrap_gphoto("detect cameras"):
            detected = detect()
        model, port = self._selected or (self._requested_model, self._requested_port)
        matches = [(name, address) for name, address in detected
                   if (model is None or name == model) and (port is None or address == port)]
        available = "; ".join(f"{name} at {address}" for name, address in detected) or "none"
        if not matches:
            raise CameraError(
                f"No camera matches model={model!r}, port={port!r}. Detected: {available}. "
                "Check power, cable, and USB Connection = PC Remote."
            )
        if len(matches) != 1:
            raise CameraError(
                f"Multiple cameras match: {available}. Select one with Camera(model=..., port=...) "
                "or CAMERA_MODEL/CAMERA_PORT; refusing to choose a camera automatically."
            )
        selected_model, selected_port = matches[0]
        camera = gp.Camera()
        try:
            ports = gp.PortInfoList()
            ports.load()
            camera.set_port_info(ports[ports.lookup_path(selected_port)])
            abilities = gp.CameraAbilitiesList()
            abilities.load()
            camera.set_abilities(abilities[abilities.lookup_model(selected_model)])
            _release_macos_camera_service()
            camera.init()
            actual = (camera.get_abilities().model, camera.get_port_info().get_path())
            if actual != (selected_model, selected_port):
                camera.exit()
                raise CameraError(f"Camera identity changed while connecting: expected "
                                  f"{(selected_model, selected_port)!r}, got {actual!r}.")
            verification = None
            if getattr(self, "_expected_serial", None):
                # Checked while this process holds the port, before any caller can write or fire.
                try:
                    verification = verify_serial_at_port(selected_model, selected_port, self._expected_serial)
                except CameraIdentityError:
                    try:
                        camera.exit()
                    except gp.GPhoto2Error:
                        pass  # the refusal, not the release, is the outcome to report
                    raise
        except gp.GPhoto2Error as exc:
            try:
                camera.exit()
            except gp.GPhoto2Error:
                pass
            raise CameraError(
                "Could not connect to the camera. Check that it is ON, the USB cable is seated, "
                "and USB Connection is set to 'PC Remote' (MENU -> Setup -> USB Connection)."
            ) from exc
        self._camera = camera
        self._selected = (selected_model, selected_port)
        self._port = selected_port
        self.serial_verification = verification
        self.verified_serial = verification["serial"] if verification else None
        return self.info()

    def disconnect(self) -> None:
        if self._camera is not None:
            try:
                self._camera.exit()
            except gp.GPhoto2Error:
                pass  # already gone / link dropped — nothing to release
            self._camera = None

    def info(self) -> CameraInfo:
        cam = self._require()
        with _wrap_gphoto("read camera info"):
            return CameraInfo(model=cam.get_abilities().model, port=self._port)

    def _require_verified_identity(self, action: str) -> None:
        """Nothing fires on a body whose serial this connection did not prove, unless the caller
        explicitly opted out with allow_unverified_identity=True (owner-run dev tools only)."""
        if getattr(self, "verified_serial", None) or getattr(self, "_allow_unverified_identity", False):
            return
        raise CameraIdentityError(
            f"Refusing to {action}: this connection's USB serial was not verified. Construct the "
            "Camera with serial=... (or camera_for_serial()).", reason="serial_unverified",
            expected_serial=getattr(self, "_expected_serial", None),
            model=getattr(self, "_requested_model", None), port=getattr(self, "_requested_port", None))

    def _serial_receipt(self) -> dict:
        """The serial this connection proved at connect (None when no serial was expected)."""
        return {"serial": getattr(self, "verified_serial", None),
                "serial_verification": getattr(self, "serial_verification", None)}

    def capabilities(self) -> dict:
        """Conservative implemented capabilities, separate from advertised driver choices."""
        model = self.info().model
        supported = model in {"Sony ILCE-7M2 (Control)", "Sony Alpha-A7 III (PC Control)"}
        return {
            "model": model, "port": self._port, "still_capture": supported,
            "usb_preview": False,
            "native_movie": model == "Sony ILCE-7M2 (Control)",
            "native_movie_evidence": "historical A7 II only; inspect recording safety state",
            "manual_focus_motor": False, "focus_coordinates": False,
            "settings_evidence": "describe_setting reports driver metadata, not verified behavior",
        }

    def shooting_mode(self) -> str:
        """The PASM mode the dial is in (e.g. 'M', 'A', 'P'), or 'unknown'."""
        cam = self._require()
        with _wrap_gphoto("read shooting mode"):
            config = cam.get_config()
        for node in _MODE_NODES:
            try:
                return str(config.get_child_by_name(node).get_value())
            except gp.GPhoto2Error:
                continue
        return "unknown"

    def wake(self, timeout: float = 20.0) -> bool:
        """Nudge the camera out of power-save and wait until it reports real settings.

        The A7 II forces a ~1-min auto-power-off in PC Remote (no PTP property disables it). A
        sleeping body reports degenerate values (f/0, shutter 'Bulb', iso 0) and drops USB mid-
        capture. Repeated reads wake it. On newer bodies, valid ISO and shutter also establish
        readiness: a manual/adapted lens can legitimately report f/0."""
        model = self.info().model
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            try:
                settings = self.get_settings()
                aperture = settings.get("aperture")
            except CameraError:
                settings = {}
                aperture = None
            if aperture and aperture not in ("f/0", "0"):
                return True  # real aperture from the lens => body is awake
            if model != "Sony ILCE-7M2 (Control)":
                iso = str(settings.get("iso", ""))
                shutter = str(settings.get("shutter", ""))
                try:
                    parts = shutter.split("/")
                    seconds = float(parts[0]) / (float(parts[1]) if len(parts) == 2 else 1)
                    valid_iso = (iso in ("Auto ISO", "Auto") or float(iso) > 0)
                    if valid_iso and math.isfinite(seconds) and seconds > 0 and shutter != "65535/65535":
                        return True
                except (ValueError, ZeroDivisionError):
                    pass
            time.sleep(1.0)
        return False

    # --- settings ---------------------------------------------------------
    def get_settings(self) -> dict[str, str | None]:
        """Current value of every exposed setting (None if the body doesn't report it)."""
        cam = self._require()
        with _wrap_gphoto("read settings"):
            config = cam.get_config()
        settings: dict[str, str | None] = {}
        for key, node in SETTING_NODES.items():
            try:
                settings[key] = config.get_child_by_name(node).get_value()
            except gp.GPhoto2Error:
                settings[key] = None  # body doesn't expose this node — not an error
        return settings

    def list_options(self, key: str) -> list[str]:
        """Valid values for a setting, so callers only pick values the camera accepts."""
        node = SETTING_NODES.get(key, key)
        cam = self._require()
        with _wrap_gphoto(f"list options for {key}"):
            widget = cam.get_config().get_child_by_name(node)
            return [widget.get_choice(i) for i in range(widget.count_choices())]

    def describe_setting(self, key: str) -> dict:
        """Read a widget's value, type and access flag without writing it.

        Sony drivers may advertise a superset of the mounted body's valid values.
        Neither choices nor readonly=False prove a setting write will succeed.
        """
        node = SETTING_NODES.get(key, key)
        with _wrap_gphoto(f"describe {key}"):
            widget = self._require().get_config().get_child_by_name(node)
            kind = widget.get_type()
            result = {"node": node, "value": widget.get_value(),
                      "readonly": bool(widget.get_readonly()), "type": kind}
            if kind in (gp.GP_WIDGET_MENU, gp.GP_WIDGET_RADIO):
                result["choices"] = [widget.get_choice(i) for i in range(widget.count_choices())]
            elif kind == gp.GP_WIDGET_RANGE:
                result["range"] = list(widget.get_range())
            return result

    def set_setting(self, key: str, value: str, max_seconds: float = 8.0) -> str:
        """Set a setting and verify it took.

        Sony often *steps* toward a value one notch per command rather than jumping, so we
        re-issue the target and re-read until it matches. We keep going as long as the value is
        still moving (handles large jumps), but bail out fast if it stalls (a setting the mode
        dial or a physical dial has locked won't budge no matter how many times we try).
        """
        node = SETTING_NODES.get(key, key)
        cam = self._require()
        target = str(value)
        # The camera labels apertures "f/2.8"; accept bare "2.8" or "8" too.
        if key == "aperture" and not target.startswith("f/"):
            target = f"f/{target}"

        with _wrap_gphoto(f"set {key}"):
            deadline = time.monotonic() + max_seconds
            previous: str | None = None
            stalled = 0
            while time.monotonic() < deadline:
                config = cam.get_config()
                widget = config.get_child_by_name(node)
                current = str(widget.get_value())
                if _widget_value_matches(widget, current, target):
                    return widget.get_value()
                stalled = stalled + 1 if current == previous else 0
                if stalled >= 12:  # genuinely not moving — locked by mode/physical dial
                    break
                previous = current
                widget.set_value(_coerce_widget_target(widget, target))
                cam.set_config(config)
                time.sleep(0.15)
            final_widget = cam.get_config().get_child_by_name(node)
            final = str(final_widget.get_value())

        if not _widget_value_matches(final_widget, final, target):
            try:
                valid_options = self.list_options(key)
            except CameraError:
                valid_options = []
            raise SettingNotApplied(
                f"Could not set {key} to {value!r}; camera stayed at {final!r}. It may be locked "
                f"by the mode dial or a physical dial (e.g. exposure comp). "
                f"Valid options: {valid_options}",
                key=key, requested=str(value), final=final,
            )
        return final

    def set_exposure(
        self,
        iso: str | None = None,
        aperture: str | None = None,
        shutter: str | None = None,
        white_balance: str | None = None,
        exposure_comp: str | None = None,
    ) -> dict[str, str]:
        """Convenience: set any subset of the core exposure controls in one call."""
        wanted = {
            "iso": iso,
            "aperture": aperture,
            "shutter": shutter,
            "white_balance": white_balance,
            "exposure_comp": exposure_comp,
        }
        return {key: self.set_setting(key, val) for key, val in wanted.items() if val is not None}

    def set_color_temp(self, kelvin: int) -> bool:
        """Set the camera's WB color temperature in Kelvin. Returns True on success, False otherwise.

        Setting readback is required; this is not a photographic color-fidelity proof. It is
        deliberately FAIL-SAFE: any gphoto2/state failure is swallowed and returns False so the
        caller (e.g. the auto_expose loop) keeps going — color temp is a nicety, never a blocker.

        Two-step, because of a firmware precondition: `colortemperature` (Sony PTP 0xD20F) is a
        2500-9900 / step-100 RANGE that is ONLY writable once `whitebalance` is in its Kelvin /
        "Color Temperature" choice. So we set that whitebalance choice FIRST, then the Kelvin value.

        We do NOT hardcode the whitebalance choice string (it varies across libgphoto2 builds:
        "Color Temperature", "Color Temperature/Filter", "Choose Color Temperature", ...). Instead
        we enumerate the node's choices at runtime and pick the one whose name contains "color temp"
        (case-insensitive). If no such choice exists, the body doesn't support manual Kelvin WB and
        we bail (False) rather than guess.

        NOTE on DIRECTION (classic WB gotcha — do not "fix" this backwards): a HIGHER Kelvin makes
        the image look WARMER (more orange); a LOWER Kelvin looks COOLER (more blue). The caller is
        responsible for that mapping (warm -> high K, cool -> low K); here we only clamp + set.
        """
        try:
            kelvin = int(kelvin)
        except (TypeError, ValueError):
            return False
        # Clamp into the documented range and snap to the 100 K step grid.
        kelvin = max(2500, min(9900, kelvin))
        kelvin = round(kelvin / 100) * 100

        try:
            cam = self._require()
            with _wrap_gphoto("set color temperature"):
                # Step 1: put whitebalance into its Kelvin/Color-Temp choice so 0xD20F unlocks.
                config = cam.get_config()
                wb = config.get_child_by_name("whitebalance")
                choices = [wb.get_choice(i) for i in range(wb.count_choices())]
                kelvin_choice = next(
                    (choice for choice in choices if "color temp" in choice.lower()), None
                )
                if kelvin_choice is None:
                    return False  # body has no manual-Kelvin WB mode — nothing to set
                wb.set_value(kelvin_choice)
                cam.set_config(config)

                # Step 2: now colortemperature is writable — set the clamped Kelvin value.
                config = cam.get_config()
                temp = config.get_child_by_name("colortemperature")
                temp.set_value(kelvin)
                cam.set_config(config)
                # Sony applies this asynchronously. Poll readback for at most two seconds;
                # never repeat either actuation just because its value has not arrived yet.
                deadline = time.monotonic() + 2.0
                for _ in range(20):
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        break
                    time.sleep(min(0.1, remaining))
                    readback = cam.get_config()
                    if (str(readback.get_child_by_name("whitebalance").get_value()) == kelvin_choice
                            and abs(float(readback.get_child_by_name("colortemperature").get_value())
                                    - kelvin) <= 0.01):
                        return True
                return False
        except (CameraError, gp.GPhoto2Error, TypeError, ValueError):
            # Read-only/absent node, or the set was refused — log-and-continue, never crash the loop.
            print(f"set_color_temp({kelvin}K) skipped: color-temp node not settable on this body.")
            return False

    # --- capture ----------------------------------------------------------
    def capture(self, download: bool = True, dest_dir: Path | None = None) -> Path | None:
        """Capture once and measure transport phases without inferring sensor exposure."""
        self._require_verified_identity("fire the shutter")
        started = time.monotonic()
        self.last_capture_receipt = {"transport": "gphoto2_python", "timings": {
            "clock": "monotonic", "sensor_exposure_s": None,
            "sensor_exposure_reason": "Transport APIs do not expose physical shutter-open timestamps."},
            **self._serial_receipt()}
        try:
            return self._capture_with_receipt(download=download, dest_dir=dest_dir)
        finally:
            if isinstance(self.last_capture_receipt, dict):
                self.last_capture_receipt.setdefault("timings", {})["total_s"] = time.monotonic() - started

    def _capture_with_receipt(self, download: bool = True, dest_dir: Path | None = None) -> Path | None:
        """Trigger the shutter. When download=True, pull the JPEG to disk and return its path.

        NOT idempotent — each call trips the shutter. Callers must not blindly retry it.
        """
        if self._has_sony_action_capture():
            return self._capture_sony_action(download=download, dest_dir=dest_dir)

        # The generic transport keeps the ordinary 1 s cap too (it has no long-exposure handling);
        # an unreadable, placeholder or Bulb shutter refuses here, before any trigger call, as a
        # typed pre-trigger refusal (the Sony action branch does the same for the same validation).
        try:
            self.last_capture_receipt["capture_deadline"] = validate_sony_capture_shutter(
                self.get_settings().get("shutter"))
        except CameraIdentityError:
            raise
        except CameraError as exc:
            raise CapturePreconditionError(str(exc)) from exc
        if download:
            dest_dir = _capture_directory(dest_dir)
        cam = self._require()
        trigger_started = time.monotonic()
        try:
            path = cam.capture(gp.GP_CAPTURE_IMAGE)
        except gp.GPhoto2Error as exc:
            # A generic transport error cannot establish that the shutter did not fire.
            raise CaptureOutcomeUncertain(str(exc)) from exc
        finally:
            self.last_capture_receipt["timings"]["trigger_to_return_s"] = time.monotonic() - trigger_started
        if not download:
            return None
        # In RAW+JPEG the camera writes both a raw (.ARW) and a .JPG, and capture() may report
        # either one. We want the JPEG for the vision/measure path, so if we got a raw, find its
        # JPEG sibling (same stem) in the same folder.
        download_started = time.monotonic()
        folder, name = path.folder, path.name
        if not name.lower().endswith((".jpg", ".jpeg")):
            stem = name.rsplit(".", 1)[0].lower()
            jpeg = None
            try:
                for _ in range(6):  # the JPG twin is written a beat after the RAW in RAW+JPEG
                    siblings = [n for n, _ in cam.folder_list_files(folder)]
                    jpeg = next((n for n in siblings if n.lower().endswith((".jpg", ".jpeg"))
                                 and n.rsplit(".", 1)[0].lower() == stem), None)
                    if jpeg:
                        break
                    time.sleep(0.3)
            except gp.GPhoto2Error as exc:
                raise CaptureOutcomeUncertain(f"Could not locate the captured JPEG: {exc}") from exc
            if jpeg is None:
                raise CaptureOutcomeUncertain(
                    "Captured a RAW file with no JPEG companion. Set the camera's Image "
                    "Quality to include JPEG (e.g. 'Fine'), or shoot JPEG-only."
                )
            name = jpeg
        dest = self._unique_dest(dest_dir, name)
        # Download happens AFTER the shutter fired — wrap it but never re-fire on failure.
        try:
            _reserve_private_capture(dest)
            cam.file_get(folder, name, gp.GP_FILE_TYPE_NORMAL).save(str(dest))
            dest.chmod(0o600)
        except (gp.GPhoto2Error, OSError) as exc:
            raise CaptureOutcomeUncertain(f"Could not download the captured image: {exc}") from exc
        self.last_capture_receipt.update(path=str(dest), capture_confirmed=True)
        self.last_capture_receipt["timings"]["resolve_and_download_s"] = time.monotonic() - download_started
        return dest

    def _has_sony_action_capture(self) -> bool:
        """True when this Sony session exposes the action-capture path instead of standard capture."""
        cam = self._require()
        try:
            config = cam.get_config()
            config.get_child_by_name("actions").get_child_by_name("capture")
            return True
        except (gp.GPhoto2Error, AttributeError):
            return False

    def _capture_sony_action(self, download: bool = True, dest_dir: Path | None = None) -> Path | None:
        """Capture through gphoto2's Sony-aware CLI actuator.

        The A7 II reports "Sony Capture" but "No Image Capture" in PC Remote mode. The python-gphoto2
        standard capture path uses the unsupported generic operation and can drop USB. The CLI's
        `--capture-image-and-download --keep` path does fire on this body and downloads the JPEG/RAW
        pair while leaving the camera-side files in place, so we briefly release the Python claim,
        let the CLI perform that Sony-aware capture, then reconnect for the rest of the exposure loop.
        """
        gphoto = shutil.which("gphoto2")
        if not gphoto:
            raise CapturePreconditionError("Sony action capture needs the gphoto2 CLI on PATH; no shutter was issued.")

        # Everything up to the CLI call below provably precedes any trigger: a setup failure there
        # is a CapturePreconditionError (nothing fired). disconnect() and the daemon release never
        # raise; an identity refusal keeps its own type.
        try:
            dest_dir = _capture_directory(dest_dir)
            identity = self.info()
        except CameraIdentityError:
            raise
        except (CameraError, OSError) as exc:
            raise CapturePreconditionError(f"Capture setup failed ({exc}); no shutter was issued.") from exc
        capture_started = time.monotonic()
        trigger_started = None
        timings = {"clock": "monotonic", "sensor_exposure_s": None,
                   "sensor_exposure_reason": "Sony CLI combines trigger, camera wait and download; sensor timestamps are unavailable."}
        self.last_capture_receipt = {"model": identity.model, "port": identity.port,
                                     "transport": "sony_gphoto2_cli", "timings": timings,
                                     "path": None, "capture_confirmed": False,
                                     "reconnect_ok": None, "warning": None,
                                     **self._serial_receipt()}
        # Read while Python still owns the exact camera. Unknown/Bulb state fails
        # before disconnect or any non-idempotent shutter command.
        try:
            deadline = sony_capture_deadline(self.get_settings().get("shutter"))
            self.last_capture_receipt["capture_deadline"] = deadline
            self.last_capture_receipt["capture_policy"] = {
                "max_enabled_shutter_s": SONY_ACTION_MAX_ENABLED_SHUTTER_S,
                "status": "provisional_short_exposure_policy",
                "reason": "Long-shutter Sony image retrieval failed physical validation; alternate trigger/wait transport is unverified."}
            validate_sony_capture_shutter(deadline["confirmed_shutter_setting"])
        except CameraIdentityError:
            raise
        except CameraError as exc:
            raise CapturePreconditionError(str(exc)) from exc
        timings["configured_shutter_s"] = deadline["configured_shutter_s"]
        timings["capture_timeout_s"] = deadline["subprocess_timeout_s"]
        # Pin the CLI and subsequent reconnect, including when this is the only body now.
        self._selected = (identity.model, identity.port)
        release_started = time.monotonic()
        self.disconnect()
        timings["usb_release_s"] = time.monotonic() - release_started
        staging: Path | None = None
        uncertain = False   # set by the handlers below: the receipt then names the host's camera clients
        try:
            settle_started = time.monotonic()
            time.sleep(0.7)
            timings["settle_s"] = time.monotonic() - settle_started
            # The daemon can reclaim USB during the settle gap between stack frames.
            checks_started = time.monotonic()
            _release_macos_camera_service()
            if getattr(self, "_expected_serial", None):
                # The CLI re-opens the port by address after the Python claim was released, so
                # prove the port still holds this body immediately before the shutter.
                try:
                    self.last_capture_receipt["pre_shutter_serial_verification"] = verify_serial_at_port(
                        identity.model, identity.port, self._expected_serial)
                except CameraIdentityError:
                    raise
                except (CameraError, OSError) as exc:
                    raise CapturePreconditionError(
                        f"The pre-shutter serial check could not run ({exc}); no shutter was issued.") from exc
            # Staged only after the pre-shutter check passed, so a refusal leaves nothing behind.
            # The CLI downloads straight into this private quarantine directory: no temporary
            # directory cleanup can ever delete a photograph that may be the only copy.
            try:
                staging = _make_staging_dir(self._quarantine_root())
            except OSError as exc:
                raise CapturePreconditionError(f"Private capture staging is unavailable ({exc}); no shutter was issued") from exc
            timings["pre_trigger_checks_s"] = time.monotonic() - checks_started
            argv = [
                gphoto,
                "--camera", identity.model,
                "--port", identity.port,
                "--capture-image-and-download",
                "--keep",
            ]
            # Diagnostics only (argv holds no secret): what ran, when by the wall clock, and how it ended.
            cli = self.last_capture_receipt["cli"] = {
                "argv": list(argv), "timeout_s": deadline["subprocess_timeout_s"],
                "started_at": _wall_clock(), "ended_at": None, "outcome": None, "exit_code": None}
            trigger_started = time.monotonic()
            try:
                result = subprocess.run(
                    argv,
                    cwd=staging,
                    text=True,
                    capture_output=True,
                    timeout=deadline["subprocess_timeout_s"],
                    check=False,
                )
            except subprocess.TimeoutExpired as exc:
                # subprocess.run has already killed the CLI (SIGKILL) and reaped it.
                _record_cli_end(cli, "timeout_killed", None, exc.stdout, exc.stderr, staging=staging)
                raise
            _record_cli_end(cli, "exited", result.returncode, result.stdout, result.stderr)
            timings["trigger_and_download_s"] = time.monotonic() - trigger_started
            try:
                # Retention expires a receipt-less staging dir by mtime; refresh it once the download
                # is done so a clock jump or long suspend cannot expire an active capture.
                os.utime(staging)
            except OSError:
                pass
            if getattr(self, "_expected_serial", None):
                # The pre-shutter check and the CLI's own open of the port are not atomic.
                # Prove the port still holds this body now; if not, the frame may come from
                # another body, so it is reported uncertain and never delivered as success.
                try:
                    self.last_capture_receipt["post_shutter_serial_verification"] = verify_serial_at_port(
                        identity.model, identity.port, self._expected_serial)
                except CameraIdentityError as refusal:
                    self.last_capture_receipt["post_shutter_identity_refusal"] = refusal.as_dict()
                    preserved = self._preserve_unproven_download(staging, identity, refusal)
                    staging = None  # sealed (or kept as staging-<id>): owned by the quarantine now
                    self.last_capture_receipt["quarantine"] = preserved
                    kept = [Path(name) for name in preserved.get("files", [])]
                    raise CaptureFromUnprovenBody(
                        f"the USB serial at {identity.port} could not be proven after the shutter "
                        f"({refusal.reason}); the download was preserved in quarantine, not delivered",
                        refusal=refusal, capture_path=_pick_downloaded_jpeg(kept) or (kept[0] if kept else None),
                        quarantine=preserved) from refusal
            if result.returncode != 0:
                detail = (result.stderr or result.stdout).strip()
                raise CaptureOutcomeUncertain(f"Sony action capture failed: {detail}",
                                              capture_path=_pick_downloaded_jpeg(_staged_files(staging)))
            self.last_capture_receipt["capture_confirmed"] = True
            staged = _staged_files(staging)
            jpeg = _pick_downloaded_jpeg(staged) if download else None
            # Board-proof evidence (deploy/raw_proof.py): name, size and SHA-256 of every file the
            # camera delivered, hashed from the bytes as stored before the non-JPEG files are
            # discarded. It proves what crossed USB; the ARW itself is not kept.
            hash_started = time.monotonic()
            self.last_capture_receipt["downloaded_files"] = [
                {**_download_record(path), "retained": path == jpeg} for path in staged]
            timings["download_hash_s"] = time.monotonic() - hash_started
            if download and jpeg is None:
                detail = (result.stderr or result.stdout).strip()
                raise CaptureOutcomeUncertain(f"Sony action capture did not download a JPEG. Output: {detail}")
            # Delivered exactly as before: only the JPEG leaves; the other downloads are discarded,
            # first, so a crash after the move leaves at most an empty staging directory.
            for other in staged:
                if other != jpeg:
                    other.unlink()
            if not download:
                _remove_empty_staging(staging)
                staging = None
                return None
            copy_started = time.monotonic()
            dest = self._unique_dest(dest_dir, jpeg.name)
            reservation = _reserve_private_capture(dest)
            try:
                # Private before the move: a same-volume rename publishes this very inode, and the
                # CLI created it under the umask (typically 0644). The EXDEV copy is 0600 at creation.
                _make_private(jpeg)
                delivery = _move_durably(jpeg, dest)
            except BaseException:
                _release_reservation(dest, reservation)  # never leave an empty file under the name
                raise
            self.last_capture_receipt["path"] = str(dest)
            self.last_capture_receipt["delivery"] = delivery
            if (durability := _delivery_durability(delivery)) is not None:
                # Not a failure (the photo is delivered; failing would invite a reshoot), but it
                # must be visible: the shot's evidence and summary carry it (backend/engine.py).
                self.last_capture_receipt["delivery_durability"] = durability
            if not delivery["source_removed"]:
                # The destination's directory entry is not known to be durable: keep the staged
                # copy as a duplicate (never served; retention and delete-all remove it).
                self.last_capture_receipt["staged_duplicate"] = str(jpeg)
            timings["local_copy_s"] = time.monotonic() - copy_started
            _remove_empty_staging(staging)
            staging = None
            return dest
        except CaptureOutcomeUncertain as exc:
            # Every uncertain outcome carries this attempt's receipt (the same dict, so the
            # reconnect evidence recorded below reaches it too): the refusal must not lose it.
            uncertain = True
            exc.transport_receipt = self.last_capture_receipt
            raise
        except subprocess.TimeoutExpired as exc:
            uncertain = True
            failure = CaptureOutcomeUncertain(
                "Sony action capture timed out waiting for the camera.",
                capture_path=_pick_downloaded_jpeg(_staged_files(staging)) if staging else None)
            failure.transport_receipt = self.last_capture_receipt
            raise failure from exc
        except OSError as exc:
            uncertain = True
            saved = self.last_capture_receipt.get("path")
            failure = CaptureOutcomeUncertain(f"Sony action capture transfer failed: {exc}",
                                              capture_path=Path(saved) if saved else None)
            failure.transport_receipt = self.last_capture_receipt
            raise failure from exc
        finally:
            if staging is not None:
                # Any other outcome: an empty staging directory goes; downloaded bytes stay in the
                # managed quarantine tree, where retention and delete-all can reach them.
                _remove_empty_staging(staging)
            # Reclaiming USB is a separate outcome. Never erase a downloaded photograph or
            # replace the original uncertainty with an apparently retryable connection error.
            if trigger_started is not None and "trigger_and_download_s" not in timings:
                timings["trigger_and_download_s"] = time.monotonic() - trigger_started
            reconnect_started = time.monotonic()
            self.last_reconnect_attempts = None
            try:
                self._reconnect_with_backoff()
                self.last_capture_receipt["reconnect_ok"] = True
            except CameraIdentityError as exc:
                self.last_capture_receipt["reconnect_ok"] = False
                self.last_capture_receipt["reconnect_identity_refusal"] = exc.as_dict()
                self.last_capture_receipt["warning"] = f"Capture reconnect refused: {exc}"
            except Exception as exc:
                self.last_capture_receipt["reconnect_ok"] = False
                self.last_capture_receipt["warning"] = f"Capture reconnect failed: {exc}"
            finally:
                timings["reconnect_s"] = time.monotonic() - reconnect_started
                timings["total_s"] = time.monotonic() - capture_started
                self.last_capture_receipt["reconnect_attempts"] = self.last_reconnect_attempts
            if uncertain and isinstance(self.last_capture_receipt.get("cli"), dict):
                # After the reconnect, which it must not delay (and which usually covers the
                # window's +1 s). One bounded query, no wait. Nothing in this block, not even an
                # interrupt, may replace the uncertain outcome propagating through this finally.
                receipt = self.last_capture_receipt
                try:
                    receipt["host_camera_clients"] = {"status": "unavailable", "reason": "interrupted"}
                    query_started = time.monotonic()
                    receipt["host_camera_clients"] = host_camera_clients(receipt["cli"])
                    timings["host_log_query_s"] = time.monotonic() - query_started
                except BaseException:  # noqa: BLE001 — evidence never changes a capture outcome
                    pass

    def capture_long_exposure(self, shutter_setting: str, dest_dir: Path | None = None) -> Path:
        """Fire ONE long exposure (> 1 s, <= SONY_LONG_EXPOSURE_MAX_SHUTTER_S) and deliver its JPEG.

        The separately gated long route (see SONY_LONG_EXPOSURE_MAX_SHUTTER_S); capture() keeps the
        1 s cap. The body must already read back `shutter_setting`; nothing is written here.
        Sequence: release the Python claim; prove the USB serial; drain objects already pending in
        camera RAM (read-only; any found refuses the trigger and is preserved in quarantine); prove
        the serial again; ONE `--trigger-capture` with a bounded `--wait-event-and-download` window
        into private quarantine staging; prove the serial after; accept exactly one JPEG whose EXIF
        names this model and this exposure time; deliver it; then reconnect() with bounded backoff.

        NOT idempotent and never retried: any doubt after the trigger raises CaptureOutcomeUncertain
        (or CaptureFromUnprovenBody) with the downloaded bytes kept in quarantine. Refusals before
        the trigger raise CameraError / CameraIdentityError and fired nothing.
        """
        # A fresh receipt before any refusal: callers read trigger_commands to learn whether this
        # call fired, so an earlier capture's receipt must never stand in for this one.
        self.last_capture_receipt = {"route": "long_exposure", "trigger_commands": 0,
                                     "capture_confirmed": False, "automatic_retry": False}
        if not getattr(self, "_expected_serial", None):
            raise CameraIdentityError("A long exposure requires the body's USB serial.",
                                      reason="serial_required", model=self._requested_model,
                                      port=self._requested_port)
        self._require_verified_identity("fire a long exposure")
        identity = self.info()
        if identity.model not in _EXIF_MODEL_FOR_USB_MODEL:
            raise CameraError(f"The long-exposure route does not support {identity.model}; no shutter was issued")
        gphoto = shutil.which("gphoto2")
        if not gphoto:
            raise CameraError("The long-exposure route needs the gphoto2 CLI on PATH; no shutter was issued")
        deadline = validate_sony_long_exposure_shutter(shutter_setting, self.list_options("shutter"))
        confirmed = self.get_settings().get("shutter")
        if str(confirmed).strip() != deadline["confirmed_shutter_setting"]:
            raise CameraError(f"The body reads back shutter {confirmed!r}, not {shutter_setting!r}; "
                              "no shutter was issued")
        dest_dir = _capture_directory(dest_dir)
        started = time.monotonic()
        timings = {"clock": "monotonic", "sensor_exposure_s": None,
                   "configured_shutter_s": deadline["configured_shutter_s"],
                   "event_wait_s": deadline["event_wait_s"],
                   "capture_timeout_s": deadline["subprocess_timeout_s"],
                   "sensor_exposure_reason": "The CLI trigger/wait does not expose sensor-open timestamps."}
        receipt = {"model": identity.model, "port": identity.port, "route": "long_exposure",
                   "transport": "sony_gphoto2_cli_trigger_wait", "timings": timings,
                   "capture_deadline": deadline, "path": None, "capture_confirmed": False,
                   "trigger_commands": 0, "automatic_retry": False,
                   "reconnect_ok": None, "warning": None, **self._serial_receipt()}
        self.last_capture_receipt = receipt
        base = [gphoto, "--camera", identity.model, "--port", identity.port]
        self._selected = (identity.model, identity.port)
        release_started = time.monotonic()
        self.disconnect()
        timings["usb_release_s"] = time.monotonic() - release_started
        prior: Path | None = None
        staging: Path | None = None
        trigger_started = None
        try:
            time.sleep(0.7)
            _release_macos_camera_service()
            receipt["pre_drain_serial_verification"] = verify_serial_at_port(
                identity.model, identity.port, self._expected_serial)
            try:
                prior = _make_staging_dir(self._quarantine_root())
            except OSError as exc:
                raise CameraError(f"Private capture staging is unavailable ({exc}); no shutter was issued") from exc
            # --trigger-capture does not clear camera RAM, so a file already pending there could be
            # mistaken for this exposure. Drain read-only first; anything found refuses the trigger.
            drain_argv = base + [f"--wait-event-and-download={LONG_EXPOSURE_PRE_DRAIN_WAIT_S}s", "--keep"]
            try:
                drained = subprocess.run(drain_argv, cwd=prior, text=True, capture_output=True,
                                         timeout=LONG_EXPOSURE_PRE_DRAIN_TIMEOUT_S, check=False)
                drain_error = drained.returncode != 0 or bool(_CLI_ERROR.search(f"{drained.stdout}\n{drained.stderr}"))
                drain_detail = (drained.stderr or drained.stdout or "").strip()[-2000:]
            except subprocess.TimeoutExpired:
                drain_error, drain_detail = True, "the read-only drain timed out"
            pending = _staged_files(prior)
            receipt["pre_drain"] = {"argv": drain_argv[1:], "pending_objects": [path.name for path in pending],
                                    "error": drain_error, "detail": drain_detail}
            if pending or drain_error:
                if pending:
                    try:
                        receipt["pre_drain"]["quarantine"] = _seal_unproven(prior, {
                            "status": "prior_camera_objects", "published": False, "retry_capture": False,
                            "model": identity.model, "port": identity.port,
                            "note": "Objects already pending in camera RAM before a long exposure; "
                                    "kept for review, never published."})
                    except OSError as exc:
                        receipt["pre_drain"]["quarantine_error"] = str(exc)
                    prior = None  # sealed (or left as staging-<id> with its bytes): owned by quarantine
                raise CameraError(
                    "The camera had images pending in its memory (or the read-only drain failed), so a "
                    "new long exposure could not be told apart from them; they were preserved in "
                    "quarantine. Try again; no shutter was issued.")
            _remove_empty_staging(prior)
            prior = None
            _release_macos_camera_service()
            receipt["pre_shutter_serial_verification"] = verify_serial_at_port(
                identity.model, identity.port, self._expected_serial)
            try:
                staging = _make_staging_dir(self._quarantine_root())
            except OSError as exc:
                raise CameraError(f"Private capture staging is unavailable ({exc}); no shutter was issued") from exc
            argv = base + ["--trigger-capture", f"--wait-event-and-download={deadline['event_wait_s']}s", "--keep"]
            receipt["trigger_argv"] = argv[1:]
            receipt["trigger_commands"] = 1
            trigger_started = time.monotonic()
            result = subprocess.run(argv, cwd=staging, text=True, capture_output=True,
                                    timeout=deadline["subprocess_timeout_s"], check=False)
            timings["trigger_and_download_s"] = time.monotonic() - trigger_started
            try:
                os.utime(staging)  # retention expires receipt-less staging by mtime
            except OSError:
                pass
            try:
                receipt["post_shutter_serial_verification"] = self._post_long_exposure_serial_check(identity)
            except CameraIdentityError as refusal:
                receipt["post_shutter_identity_refusal"] = refusal.as_dict()
                preserved = self._preserve_unproven_download(staging, identity, refusal)
                staging = None
                receipt["quarantine"] = preserved
                kept = [Path(name) for name in preserved.get("files", [])]
                raise CaptureFromUnprovenBody(
                    f"the USB serial at {identity.port} could not be proven after the long exposure "
                    f"({refusal.reason}); the download was preserved in quarantine, not delivered",
                    refusal=refusal, capture_path=_pick_downloaded_jpeg(kept) or (kept[0] if kept else None),
                    quarantine=preserved) from refusal
            staged = _staged_files(staging)
            detail = (result.stderr or result.stdout or "").strip()[-2000:]
            if result.returncode != 0 or _CLI_ERROR.search(f"{result.stdout}\n{result.stderr}"):
                raise CaptureOutcomeUncertain(
                    f"The long-exposure capture reported an error; any files stay in quarantine: {detail}",
                    capture_path=_pick_downloaded_jpeg(staged))
            jpegs = [path for path in staged if path.suffix.lower() in {".jpg", ".jpeg"}]
            if len(jpegs) != 1:
                raise CaptureOutcomeUncertain(
                    f"Expected exactly one JPEG from the long exposure, found {len(jpegs)}; any files stay "
                    "in quarantine", capture_path=jpegs[-1] if jpegs else None)
            jpeg = jpegs[0]
            attribution = _long_exposure_attribution(jpeg, identity.model, deadline["configured_shutter_s"])
            receipt["attribution"] = attribution
            if not attribution["matches"]:
                try:
                    preserved = {"status": "quarantined", **_seal_unproven(staging, {
                        "status": "attribution_unproven", "published": False, "retry_capture": False,
                        "model": identity.model, "port": identity.port, "attribution": attribution,
                        "note": "Downloaded after a long-exposure trigger, but its EXIF does not match "
                                "this body and shutter. Review before use; never published automatically."})}
                except OSError as exc:
                    preserved = {"status": "staged_without_receipt", "directory": str(staging),
                                 "quarantine_error": str(exc),
                                 "files": [str(path) for path in _staged_files(staging)]}
                staging = None
                receipt["quarantine"] = preserved
                kept = [Path(name) for name in preserved.get("files", [])]
                raise CaptureOutcomeUncertain(
                    f"The downloaded JPEG could not be attributed to this long exposure by its EXIF "
                    f"({attribution.get('reason')}); it was preserved in quarantine, not delivered",
                    capture_path=_pick_downloaded_jpeg(kept))
            receipt["capture_confirmed"] = True
            # Same board-proof evidence as the ordinary path: every downloaded file, hashed as stored.
            receipt["downloaded_files"] = [{**_download_record(path), "retained": path == jpeg} for path in staged]
            copy_started = time.monotonic()
            dest = self._unique_dest(dest_dir, jpeg.name)
            reservation = _reserve_private_capture(dest)
            try:
                _make_private(jpeg)
                delivery = _move_durably(jpeg, dest)
            except BaseException:
                _release_reservation(dest, reservation)
                raise  # every staged file (the RAW too) stays in quarantine
            receipt["path"] = str(dest)
            receipt["delivery"] = delivery
            # Only after the JPEG is delivered are the other downloads (the RAW twin) discarded. A
            # failed removal is a warning: the photo is delivered, and the file stays in quarantine.
            for other in staged:
                if other != jpeg:
                    try:
                        other.unlink()
                    except OSError as exc:
                        receipt["cleanup_warning"] = f"{other.name} stays in quarantine ({exc})"
            if (durability := _delivery_durability(delivery)) is not None:
                receipt["delivery_durability"] = durability
            if not delivery["source_removed"]:
                receipt["staged_duplicate"] = str(jpeg)
            timings["local_copy_s"] = time.monotonic() - copy_started
            _remove_empty_staging(staging)
            staging = None
            return dest
        except subprocess.TimeoutExpired as exc:
            raise CaptureOutcomeUncertain(
                "The long-exposure capture timed out waiting for the camera; any files stay in quarantine.",
                capture_path=_pick_downloaded_jpeg(_staged_files(staging)) if staging else None) from exc
        except OSError as exc:
            if trigger_started is None:
                raise CameraError(f"Long-exposure preparation failed ({exc}); no shutter was issued") from exc
            saved = receipt.get("path")
            raise CaptureOutcomeUncertain(f"Long-exposure transfer failed: {exc}",
                                          capture_path=Path(saved) if saved else None) from exc
        finally:
            for directory in (prior, staging):
                if directory is not None:
                    _remove_empty_staging(directory)  # rmdir only: bytes stay for retention
            if trigger_started is not None and "trigger_and_download_s" not in timings:
                timings["trigger_and_download_s"] = time.monotonic() - trigger_started
            reconnect_started = time.monotonic()
            try:
                self.reconnect()
                receipt["reconnect_ok"] = True
                receipt["reconnect_port"] = self._port
            except CameraIdentityError as exc:
                receipt["reconnect_ok"] = False
                receipt["reconnect_identity_refusal"] = exc.as_dict()
                receipt["warning"] = f"Long-exposure reconnect refused: {exc}"
            except Exception as exc:  # noqa: BLE001 — recorded; never replaces the capture outcome
                receipt["reconnect_ok"] = False
                receipt["warning"] = f"Long-exposure reconnect failed: {exc}"
            finally:
                timings["reconnect_s"] = time.monotonic() - reconnect_started
                timings["total_s"] = time.monotonic() - started

    def _post_long_exposure_serial_check(self, identity: CameraInfo) -> dict:
        """Prove the serial at the SAME port after the CLI. Only a momentary absence from the OS
        listing is re-read, a bounded few times; after that, absence is terminal, as is a
        different, missing or duplicated serial. A serial that reappears at another port is never
        accepted: nothing binds the downloaded photo to that body (EXIF names only the model and
        the exposure time), so a same-model body could have taken it at the old port. The caller
        quarantines the download as unproven_* with its receipt for manual recovery."""
        last: CameraIdentityError | None = None
        for delay in LONG_EXPOSURE_POST_CHECK_DELAYS_S:
            if delay:
                time.sleep(delay)
            try:
                return verify_serial_at_port(identity.model, identity.port, self._expected_serial)
            except CameraIdentityError as refusal:
                if refusal.reason != "not_present":
                    raise
                last = refusal
        assert last is not None
        raise last

    def reconnect(self) -> CameraInfo:
        """Re-open the body after a long exposure, before any setting write (restoration reconnects
        first). No-op when already connected. Bounded backoff (LONG_EXPOSURE_RECONNECT_DELAYS_S);
        ports re-enumerate, so after a failed attempt the body is looked up again by its USB serial
        and connect() proves that serial at whatever port it now has. A refused identity is terminal."""
        if self._camera is not None:
            return self.info()
        last: Exception | None = None
        for attempt, delay in enumerate(LONG_EXPOSURE_RECONNECT_DELAYS_S):
            if delay:
                time.sleep(delay)
            if attempt and getattr(self, "_expected_serial", None):
                self._reselect_port_by_serial()
            try:
                return self.connect()
            except CameraIdentityError:
                raise
            except Exception as exc:  # noqa: BLE001 — re-raised after the last attempt
                last = exc
        assert last is not None
        raise last

    def _reselect_port_by_serial(self) -> None:
        """Point the next connect() at the one port where the OS now reports this body's serial and
        model. Discovery only; connect() still proves the serial before anything is written."""
        model = (self._selected or (self._requested_model, None))[0]
        try:
            rows = [row for row in detect_identified() if row.get("serial") == self._expected_serial]
        except Exception:  # noqa: BLE001 — discovery failure: keep the last known port
            return
        if len(rows) == 1 and (model is None or rows[0]["model"] == model):
            self._selected = (rows[0]["model"], rows[0]["port"])

    def _quarantine_root(self) -> Path:
        return Path(getattr(self, "quarantine_dir", None) or _default_quarantine_dir())

    def _preserve_unproven_download(self, staging: Path, identity: CameraInfo,
                                    refusal: CameraIdentityError) -> dict:
        """Never delete downloaded bytes on an identity-uncertain outcome. They are already in the
        quarantine tree; add a receipt and rename the directory unproven_<time>_<id>. If even the
        receipt cannot be written, the bytes stay in staging-<id> (retention expires it by mtime)."""
        files = _staged_files(staging)
        if not files:
            _remove_empty_staging(staging)
            return {"status": "nothing_downloaded", "files": []}
        pre = (self.last_capture_receipt or {}).get("pre_shutter_serial_verification") or {}
        receipt = {"status": "body_unproven", "published": False, "retry_capture": False,
                   "model": identity.model, "port": identity.port,
                   "expected_serial": getattr(self, "_expected_serial", None),
                   "pre_shutter_serial": pre.get("serial"),
                   "post_shutter": {"reason": refusal.reason, "observed_serial": refusal.observed_serial,
                                    "detail": str(refusal)},
                   "note": "Body unproven: the port's USB serial could not be verified after the shutter. "
                           "Review before use; never published automatically."}
        try:
            return {"status": "quarantined", **_seal_unproven(staging, receipt)}
        except OSError as exc:
            return {"status": "staged_without_receipt", "directory": str(staging),
                    "quarantine_error": str(exc), "files": [str(path) for path in _staged_files(staging)]}

    RECONNECT_ATTEMPT_DELAYS_S = (0.0, 1.5, 3.0, 5.0)

    def _reconnect_with_backoff(self) -> CameraInfo:
        """Re-open the body after a CLI capture. Longer exposures re-enumerate on USB more slowly,
        so a single immediate attempt can miss it; a bounded retry keeps the handle usable for
        settings restoration without hiding a body that is truly gone. Each attempt's delay,
        duration and error are kept in `last_reconnect_attempts` (evidence only)."""
        last: Exception | None = None
        attempts = self.last_reconnect_attempts = []
        for delay in self.RECONNECT_ATTEMPT_DELAYS_S:
            if delay:
                time.sleep(delay)
            record = {"delay_s": delay, "started_at": _wall_clock()}
            attempts.append(record)
            started = _note_elapsed(record)
            try:
                info = self.connect()
                _note_elapsed(record, started, ok=True)
                return info
            except CameraIdentityError as exc:
                _note_elapsed(record, started, ok=False, error=type(exc).__name__)
                raise  # a refused identity is terminal: retrying cannot make it this body
            except Exception as exc:  # noqa: BLE001 — re-raised after the last attempt
                _note_elapsed(record, started, ok=False, error=_describe(exc))
                last = exc
        assert last is not None
        raise last

    def liveview_frame(self, dest: Path | None = None) -> Path:
        """Disabled because libgphoto2 exposes no bounded preview read API.

        ``get_data_and_size`` materializes an attacker-controlled camera payload
        before Python can enforce a byte budget.  Use still capture or the Wi-Fi
        live-view client, both of which have bounded ingress paths.
        """
        raise CameraError("USB live view is disabled: gphoto2 cannot enforce a preview byte limit")

    # --- optical focus over the libgphoto2 driver route (A7 III, 2026-09-15) ------------
    # Thin adapters over the same config helpers iso/shutter use. Presence is read from the
    # connected body's config tree, never assumed: the A7 II descriptor has no manualfocus node.
    # `manualfocus` is a RELATIVE adjust (the widget reads back 0 after every write), so it must
    # be written exactly once per step; set_setting's re-issue loop would send it repeatedly.
    def _focus_widget(self, node: str):
        """Fresh widget for `node` from a fresh config tree, or None when the body lacks it."""
        cam = self._require()
        with _wrap_gphoto(f"read {node}"):
            config = cam.get_config()
        try:
            return config, config.get_child_by_name(node)
        except gp.GPhoto2Error:
            return config, None

    def _write_focus_widget_once(self, node: str, value) -> str:
        """One set_config write to `node`; returns the immediate readback (may differ by design)."""
        cam = self._require()
        config, widget = self._focus_widget(node)
        if widget is None:
            raise CameraError(f"{self.info().model} exposes no '{node}' node; refusing to write it.")
        with _wrap_gphoto(f"write {node}"):
            widget.set_value(value)
            cam.set_config(config)
            _, readback = self._focus_widget(node)
            return str(readback.get_value()) if readback is not None else ""

    def focus_capabilities(self) -> dict:
        """Which focus nodes THIS body's driver exposes, read from the config tree.

        Presence and driver metadata only: an advertised node does not prove optical motion.
        `manual_focus_route` is True only when a writable `manualfocus` RANGE node exists.
        """
        identity = self.info()
        nodes: dict[str, dict | None] = {}
        for node in FOCUS_ROUTE_NODES:
            _, widget = self._focus_widget(node)
            if widget is None:
                nodes[node] = None
                continue
            with _wrap_gphoto(f"describe {node}"):
                kind = widget.get_type()
                entry = {"value": str(widget.get_value()), "readonly": bool(widget.get_readonly()),
                         "type": int(kind)}
                if kind in (gp.GP_WIDGET_MENU, gp.GP_WIDGET_RADIO):
                    entry["choices"] = [widget.get_choice(i) for i in range(widget.count_choices())]
                elif kind == gp.GP_WIDGET_RANGE:
                    entry["range"] = [float(v) for v in widget.get_range()]
            nodes[node] = entry
        manual = nodes.get("manualfocus")
        return {
            "model": identity.model, "port": identity.port, "nodes": nodes,
            "manual_focus_route": bool(manual and not manual["readonly"]
                                       and manual["type"] == gp.GP_WIDGET_RANGE),
            "evidence": "driver descriptors read from the connected body; presence is not optical proof",
        }

    def get_focus_mode(self) -> str:
        _, widget = self._focus_widget(SETTING_NODES["focus_mode"])
        if widget is None:
            raise CameraError(f"{self.info().model} exposes no focusmode node.")
        with _wrap_gphoto("read focusmode"):
            return str(widget.get_value())

    def set_focus_mode(self, name: str) -> str:
        """Set focusmode (e.g. 'Manual', 'Automatic', 'AF-C') with the verified-readback path."""
        return str(self.set_setting("focus_mode", str(name)))

    def manual_focus_step(self, value: int) -> dict:
        """Send ONE relative manualfocus adjust in -7..7 (0 is rejected: it is not a step).

        The driver reads back 0 after every write, so the readback cannot confirm motion; the
        caller measures preview sharpness for that. Raises before writing if the node is absent.
        """
        if isinstance(value, bool) or not isinstance(value, int):
            raise ValueError("manual focus step must be an int in -7..7")
        if value == 0 or not MANUAL_FOCUS_STEP_MIN <= value <= MANUAL_FOCUS_STEP_MAX:
            raise ValueError(f"manual focus step {value} outside {MANUAL_FOCUS_STEP_MIN}..{MANUAL_FOCUS_STEP_MAX} or zero")
        _, widget = self._focus_widget("manualfocus")
        if widget is None:
            raise CameraError(f"{self.info().model} exposes no 'manualfocus' node; optical focus route unavailable.")
        with _wrap_gphoto("describe manualfocus"):
            if widget.get_readonly() or widget.get_type() != gp.GP_WIDGET_RANGE:
                raise CameraError("manualfocus node is not a writable RANGE widget on this body.")
            low, high, _step = widget.get_range()
        if not float(low) <= value <= float(high):
            raise ValueError(f"manual focus step {value} outside the driver range {low}..{high}")
        started = time.monotonic()
        readback = self._write_focus_widget_once("manualfocus", float(value))
        return {"node": "manualfocus", "sent": value, "readback": readback,
                "elapsed_s": time.monotonic() - started,
                "note": "relative adjust; readback returns to 0 by driver design"}

    def autofocus_half_press(self, hold_s: float = 1.0) -> str:
        """Half-press (autofocus=1), hold at most 3 s, read focusindication, always release."""
        if isinstance(hold_s, bool) or not isinstance(hold_s, (int, float)) \
                or not math.isfinite(hold_s) or not 0.0 < hold_s <= AUTOFOCUS_MAX_HOLD_S:
            raise ValueError(f"autofocus hold must be in (0, {AUTOFOCUS_MAX_HOLD_S}] seconds")
        for node in ("autofocus", "focusindication"):
            if self._focus_widget(node)[1] is None:
                raise CameraError(f"{self.info().model} exposes no '{node}' node; cannot half-press.")
        self._write_focus_widget_once("autofocus", 1)
        try:
            time.sleep(float(hold_s))
            _, indication = self._focus_widget("focusindication")
            with _wrap_gphoto("read focusindication"):
                return str(indication.get_value()) if indication is not None else "unavailable"
        finally:
            self._write_focus_widget_once("autofocus", 0)

    def read_focus_indication(self) -> str | None:
        """Read /main/status/focusindication once, or None when this body lacks the node.

        A bare status read: no autofocus half-press, no write, no shutter. A reported lock
        confirms the driver believes AF converged; it is not proof of optical sharpness. The
        A7 II descriptor has no writable focus nodes but does expose this status node.
        """
        _, indication = self._focus_widget("focusindication")
        if indication is None:
            return None
        with _wrap_gphoto("read focusindication"):
            return str(indication.get_value())

    def capture_preview(self, dest: Path) -> Path:
        """Save one liveview JPEG (not the shutter) to `dest`, rejecting frames over 4 MiB.

        Unlike `sonyremote.isolated_preview`, this runs in-process because the bracket already
        owns the USB handle; the byte check happens after libgphoto2 materializes the frame, so
        it bounds what is written to disk, not the driver's allocation. Use only from operator
        tooling on a physically owned body (tools/focus_bracket_capture.py).
        """
        cam = self._require()
        dest = Path(dest)
        data = b""
        for attempt in range(PREVIEW_READ_ATTEMPTS):
            # A liveview read is not the shutter; bounded re-reads are safe. On the A7 III the
            # driver hands back roughly every other frame with its first 32 bytes replaced by a
            # 32-byte liveview header (seen 2026-09-15, independent of captures).
            with _wrap_gphoto("capture preview"):
                blob = cam.capture_preview().get_data_and_size()
            size = len(blob)
            if size < 2 or size > MAX_PREVIEW_BYTES:
                raise CameraError(f"preview frame is {size} bytes; the bound is 2..{MAX_PREVIEW_BYTES}")
            data = bytes(blob)
            if data[:2] == b"\xff\xd8":
                self._preview_prelude = data[:PREVIEW_HEADER_BYTES]  # same stream, same tables
                break
            if attempt + 1 < PREVIEW_READ_ATTEMPTS:
                time.sleep(PREVIEW_RETRY_DELAY_S)
        if data[:2] != b"\xff\xd8" and data[:4] == PREVIEW_HEADER_MAGIC \
                and getattr(self, "_preview_prelude", None) and len(data) > PREVIEW_HEADER_BYTES:
            # Every read in the bound was headered: the JPEG body after the header is intact
            # (byte-aligned with a good frame at the same offsets), only the SOI/DQT prelude is
            # overwritten. Restore it from the last good frame of this connection.
            data = self._preview_prelude + data[PREVIEW_HEADER_BYTES:]
            self.preview_reconstructed = getattr(self, "preview_reconstructed", 0) + 1
        if data[:2] != b"\xff\xd8":
            dest.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            diagnostic = dest.with_suffix(".not-jpeg.bin")  # kept for driver diagnosis; private
            diagnostic.write_bytes(data)
            diagnostic.chmod(0o600)
            raise CameraError("preview frame is not a JPEG (missing SOI marker); "
                              f"head={data[:8].hex()} size={len(data)} after {PREVIEW_READ_ATTEMPTS} reads; "
                              f"payload kept at {diagnostic}")
        dest.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        _reserve_private_capture(dest)
        dest.write_bytes(data)
        dest.chmod(0o600)
        return dest

    # --- native movie recording (the camera's REAL video, to the SD card) --------
    def supports_native_movie(self) -> bool:
        """True if the body exposes gphoto2's Sony 'movie' control (PTP 0xD2C8). The node is
        registered for all Sony bodies, but only firmware-capable ones actually honor it — the
        only sure test is to call this on the live camera."""
        if self._selected and self._selected[0] != "Sony ILCE-7M2 (Control)":
            return False
        cam = self._require()
        try:
            cam.get_config().get_child_by_name("movie")
            return True
        except gp.GPhoto2Error:
            return False

    def _press_movie_button(self) -> None:
        """Press and release the Sony MOVIE button (PTP 0xD2C8: press=2, release=1).

        The A7 II treats this like the physical MOVIE button: one full press/release starts
        recording, and a second full press/release stops recording.
        """
        cam = self._require()
        with _wrap_gphoto("movie control"):
            config = cam.get_config()
            widget = config.get_child_by_name("movie")
            widget.set_value(2)
            cam.set_config(config)
            time.sleep(0.15)
            config = cam.get_config()
            widget = config.get_child_by_name("movie")
            widget.set_value(1)
            cam.set_config(config)

    def start_movie(self) -> None:
        """Start the camera's NATIVE movie recording (full quality + audio, to the SD card)."""
        assert_recording_allowed()
        self._require_verified_movie_model()
        self._require_verified_identity("start a movie")
        if self._recording_quarantined:
            raise _quarantine_error("this camera instance is quarantined")
        # Persist BEFORE the non-idempotent toggle. A process kill at any later point
        # leaves a durable stop-unverified marker that blocks a fresh instance/process.
        self._active_recording_token = quarantine_recording_stop(
            "Recording operation in progress; stop action not yet confirmed", _exclusive=True)
        try:
            self._press_movie_button()
        except BaseException as exc:
            self._recording_quarantined = True
            self._active_recording_token = None
            raise _quarantine_error("start toggle uncertain; durable marker retained") from exc

    def stop_movie(self) -> None:
        """Stop native movie recording."""
        self._require_verified_movie_model()
        token = self._active_recording_token
        if token is None or self._recording_quarantined:
            raise _quarantine_error("no owned live recording operation; physically verify the device is idle")
        try:
            _owned_recording_marker(token)
            self._press_movie_button()
            _owned_recording_marker(token, clear=True)
            self._active_recording_token = None
        except BaseException as exc:
            self._recording_quarantined = True
            self._active_recording_token = None
            try:
                quarantine_recording_stop(str(exc))
            except RecordingStopUncertain as sentinel_exc:
                raise sentinel_exc from exc
            raise _quarantine_error("stop command failed; camera is quarantined") from exc

    def record_native(self, seconds: float = 6.0) -> dict:
        """Record `seconds` of REAL camera video — far better than record_clip()'s frame burst.
        The clip lands on the SD card. Always stops, even if the timed wait is interrupted."""
        assert_recording_allowed()
        self._require_verified_movie_model()
        # Before the toggle and its quarantine handling: a refusal here pressed nothing.
        self._require_verified_identity("record a movie")
        if self._recording_quarantined:
            raise _quarantine_error("this camera instance is quarantined")
        duration, max_seconds = bounded_record_seconds(seconds)
        try:
            self.start_movie()
        except Exception as exc:
            # A toggle failure is not a verified recording state.  Another
            # toggle could start recording, so fail closed durably instead;
            # never send a compensating toggle.
            self._recording_quarantined = True
            quarantine_recording_stop(f"start toggle uncertain: {exc}")
            raise _quarantine_error("start toggle uncertain; camera is quarantined") from exc
        started = time.monotonic()
        try:
            time.sleep(duration)
        finally:
            self.stop_movie()
        return {
            "recorded_seconds": round(time.monotonic() - started, 1),
            "requested_seconds": float(seconds),
            "max_seconds": max_seconds,
            "saved_to": "the camera's SD card (native movie — pull it off the card)",
        }

    def record_clip(self, seconds: float = 6.0, dest: Path | None = None):
        """Disabled pending a cancellable preview transport.

        ``capture_preview()`` is a synchronous libgphoto2 call and cannot be
        interrupted safely.  A frame-burst encoder therefore cannot truthfully
        promise the caller's wall-clock recording deadline, even if ffmpeg is
        separately timed out.  Do not create partial outputs until that transport
        has a cancellable API; use the camera's bounded native movie path instead.
        """
        assert_recording_allowed()
        bounded_record_seconds(seconds)  # retain the public input contract
        raise CameraError(
            "record_clip is disabled: USB live preview cannot enforce a hard recording deadline; "
            "use record_native() instead"
        )

    # --- internal ---------------------------------------------------------
    def _require_verified_movie_model(self) -> None:
        model = self._selected[0] if self._selected else self._requested_model
        if model is None:
            # An unselected client must already be connected so its exact driver identity
            # can be read. Do this before creating an operation marker or sending a toggle.
            model = self.info().model
        if model != "Sony ILCE-7M2 (Control)":
            raise NotImplementedError("Native movie requires start/stop verification on this camera model")

    # --- body-shutter (handheld) event listening -------------------------
    # These methods observe files the camera pushes AFTER the user presses the body's
    # own shutter. They NEVER call capture()/set_config-trigger — the host issues no
    # shutter command. See backend/handheld.py for the metering loop that drives them.

    def drain_events(self, max_ms: int = 500) -> int:
        """Discard any queued camera events before listening; return how many were dropped.

        Sony bodies can surface a "late image" file event carried over in RAM from a prior
        shot. Frames must be attributed by the event that actually delivered them, so the
        handheld loop clears the event queue first and records how many stale events it
        discarded. This drains only — it never triggers the shutter or downloads a file.
        """
        if not isinstance(max_ms, int) or isinstance(max_ms, bool) or max_ms < 0:
            raise CameraError("drain_events max_ms must be a non-negative integer.")
        cam = self._require()
        discarded = 0
        deadline = time.monotonic() + max_ms / 1000.0
        with _wrap_gphoto("drain camera events"):
            while time.monotonic() < deadline:
                event_type, _ = cam.wait_for_event(50)
                if event_type == gp.GP_EVENT_TIMEOUT:
                    break
                discarded += 1
        return discarded

    def listen_for_files(
        self,
        dest_dir: Path | str,
        *,
        timeout_s: float,
        quiet_window_s: float = 0.8,
        max_total_s: float = 10.0,
        cancel: "threading.Event | None" = None,
    ) -> list[Path]:
        """Wait for the camera to push file(s) after a body-driven shutter press; download them.

        The host issues NO shutter command here — it only observes what the body sends. Waits up
        to ``timeout_s`` for the first ``GP_EVENT_FILE_ADDED``; once one arrives it downloads that
        file and any siblings that follow within ``quiet_window_s`` (so a slow RAW twin of a
        RAW+JPEG pair is never dropped), bounded overall by ``max_total_s``. Returns the downloaded
        paths (empty list on timeout — including the case where the body's "Still Img. Save Dest."
        keeps files on the card only, so no file event is ever delivered). Raises CameraError on a
        camera/transport error. Non-image event types are ignored. Never calls capture().
        """
        for name, value in (("timeout_s", timeout_s), ("quiet_window_s", quiet_window_s),
                            ("max_total_s", max_total_s)):
            if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value < 0:
                raise CameraError(f"listen_for_files {name} must be a non-negative number.")
        cam = self._require()
        dest_dir = Path(dest_dir)
        dest_dir.mkdir(parents=True, exist_ok=True)
        downloaded: list[Path] = []
        seen: set[tuple[str, str]] = set()
        first_deadline = time.monotonic() + timeout_s
        collect_deadline: float | None = None
        last_file = 0.0
        with _wrap_gphoto("listen for camera files"):
            while True:
                if cancel is not None and cancel.is_set():
                    break
                now = time.monotonic()
                if not downloaded:
                    if now >= first_deadline:
                        break  # nothing pushed within the window -> timeout
                else:
                    if now - last_file > quiet_window_s or now >= collect_deadline:
                        break  # quiet window elapsed, or the hard total bound is reached
                event_type, data = cam.wait_for_event(300)
                if event_type == gp.GP_EVENT_FILE_ADDED:
                    path = self._download_event_file(data, dest_dir, seen)
                    if path is not None:
                        downloaded.append(path)
                        last_file = time.monotonic()
                        if collect_deadline is None:
                            collect_deadline = last_file + max_total_s
                # GP_EVENT_TIMEOUT and every other event type (FILE_CHANGED, FOLDER_ADDED,
                # CAPTURE_COMPLETE, UNKNOWN) are ignored — we only act on delivered files.
        return downloaded

    def _download_event_file(self, event, dest_dir: Path, seen: set) -> Path | None:
        """Download one pushed camera file to a unique path, or return None to ignore it."""
        folder = getattr(event, "folder", None)
        name = getattr(event, "name", None)
        if not isinstance(folder, str) or not isinstance(name, str) or not name:
            return None
        if Path(name).name != name:  # never trust a name carrying a path separator
            return None
        if Path(name).suffix.lower() not in _LISTEN_IMAGE_SUFFIXES:
            return None  # non-image event (movie/unknown) — ignore, never re-trigger to recover
        key = (folder, name)
        if key in seen:
            return None
        seen.add(key)
        cam = self._require()
        dest = self._unique_dest(dest_dir, name)
        tmp = dest.with_suffix(dest.suffix + ".part")
        camera_file = cam.file_get(folder, name, gp.GP_FILE_TYPE_NORMAL)
        camera_file.save(str(tmp))
        tmp.rename(dest)
        return dest

    @staticmethod
    def _unique_dest(dest_dir: Path, name: str) -> Path:
        """A collision-proof destination path: timestamped, with a counter if needed."""
        stamp = time.strftime("%Y%m%d-%H%M%S")
        candidate = dest_dir / f"{stamp}_{name}"
        counter = 1
        while candidate.exists():
            candidate = dest_dir / f"{stamp}_{counter}_{name}"
            counter += 1
        return candidate

    def _require(self) -> gp.Camera:
        if self._camera is None:
            raise CameraError("Not connected. Call connect() first.")
        return self._camera
