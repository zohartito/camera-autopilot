"""Keep the evidence of an uncertain capture outcome next to the shot store.

A "shutter may have fired" answer is often the only record of what the transport saw. Its body is
written to ``<store>/uncertain/<UTC time>_<id>.json``: the directory is made 0700, the file 0600 and
created exclusively, and no symlink is followed at the store, the directory or the file. It holds
diagnostics only (no image bytes). The gallery never lists it; delete-all removes it
(``photo_lifecycle.UNCERTAIN_DIR``). Writing it never changes the capture outcome.
"""
from __future__ import annotations

import json
import os
import secrets
import time
from pathlib import Path

UNCERTAIN_DIR = "uncertain"
_DIR_FLAGS = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW


def _text(value) -> str:
    try:
        return str(value)
    except Exception:  # noqa: BLE001 — evidence is best effort
        return f"<unprintable {type(value).__name__}>"


def json_safe(value):
    """A strict, UTF-8-encodable JSON copy: unknown objects become their string form, NaN/Infinity
    become null, keys JSON cannot hold are dropped and unencodable characters (a surrogate from an
    undecodable filename) become "?". Raises ValueError or RecursionError for circular data."""
    text = json.dumps(value, default=_text, skipkeys=True, ensure_ascii=False)
    text = text.encode("utf-8", errors="replace").decode("utf-8")
    return json.loads(text, parse_constant=lambda _name: None)


def persist(store: Path, content: dict, *, now: float | None = None) -> str:
    """Write one receipt and return its store-relative path. Raises OSError on failure."""
    stamp = time.time() if now is None else now
    name = f"{time.strftime('%Y%m%dT%H%M%SZ', time.gmtime(stamp))}_{secrets.token_hex(4)}.json"
    payload = json.dumps({"recorded_at": stamp, **content}, indent=2, sort_keys=True, default=str).encode()
    store_fd = os.open(store, _DIR_FLAGS)
    try:
        try:
            os.mkdir(UNCERTAIN_DIR, 0o700, dir_fd=store_fd)
        except FileExistsError:
            pass
        directory = os.open(UNCERTAIN_DIR, _DIR_FLAGS, dir_fd=store_fd)
        try:
            os.fchmod(directory, 0o700)  # also tightens a directory that already existed
            descriptor = os.open(name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600,
                                 dir_fd=directory)
            try:
                view = memoryview(payload)
                while view:
                    view = view[os.write(descriptor, view):]
                os.fsync(descriptor)
            finally:
                os.close(descriptor)
        finally:
            os.close(directory)
    finally:
        os.close(store_fd)
    return f"{UNCERTAIN_DIR}/{name}"
