"""Append-only feedback logger.

One job: persist a single tester reaction ("was this shot good?") as one JSON line in
backend/feedback.jsonl. Pure stdlib, no deps — so it works in mock mode with nothing installed
beyond FastAPI. The orchestrator wires a POST /feedback route in app.py that calls log_feedback().
"""

from __future__ import annotations

import json
import os
import stat
import threading
import time
from collections import deque
from datetime import datetime, timezone
from pathlib import Path

# Sits next to this module so it travels with the backend regardless of where uvicorn is launched.
FEEDBACK_FILE = Path(__file__).resolve().parent / "feedback.jsonl"
MAX_FEEDBACK_RECORDS = 1_000
MAX_FEEDBACK_BYTES = 1 * 1024 * 1024
MAX_FEEDBACK_LINE_BYTES = 4 * 1024
MAX_FEEDBACK_WRITES_PER_MINUTE = 60
_lock = threading.Lock()
_recent_writes: deque[float] = deque()


def _read_feedback_snapshot(path: Path, limit: int) -> bytes:
    """Read one owned regular feedback file through a single non-following descriptor."""
    try:
        pre_open = path.lstat()
    except FileNotFoundError:
        raise
    if stat.S_ISLNK(pre_open.st_mode) or not stat.S_ISREG(pre_open.st_mode):
        raise ValueError("feedback log must be a regular file")
    flags = os.O_RDONLY | os.O_NOFOLLOW
    fd = os.open(path, flags)
    try:
        before = os.fstat(fd)
        if (
            not stat.S_ISREG(before.st_mode)
            or before.st_uid != os.geteuid()
            or before.st_nlink != 1
        ):
            raise ValueError("feedback log must be an owned single-link regular file")
        if before.st_size > limit:
            raise ValueError("feedback log exceeds retained byte limit")
        data = bytearray()
        remaining = limit + 1
        while remaining:
            chunk = os.read(fd, min(64 * 1024, remaining))
            if not chunk:
                break
            data.extend(chunk)
            remaining -= len(chunk)
        after = os.fstat(fd)
        if after.st_size != before.st_size or len(data) != before.st_size or len(data) > limit:
            raise ValueError("feedback log changed while being read")
        return bytes(data)
    finally:
        os.close(fd)


def _read_tail(path: Path, limit: int) -> bytes:
    """Read at most the retained budget from a safely captured feedback snapshot."""
    return _read_feedback_snapshot(path, limit)[-limit:]


def _validated_rows(path: Path) -> list[bytes]:
    """Return complete UTF-8 JSONL records or fail closed on a corrupted retained log."""
    data = _read_feedback_snapshot(path, MAX_FEEDBACK_BYTES)
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ValueError("feedback log is not valid UTF-8") from exc
    rows = [line.encode("utf-8") for line in text.splitlines() if line]
    for row in rows:
        try:
            if not isinstance(json.loads(row), dict):
                raise ValueError("feedback log contains a non-object record")
        except json.JSONDecodeError as exc:
            raise ValueError("feedback log contains invalid JSON") from exc
    return rows


def log_feedback(
    intent: str | None,
    verdict: str,
    image: str,
    timestamp: str | None = None,
    comment: str | None = None,
) -> dict:
    """Append one reaction to feedback.jsonl and return the row that was written.

    intent    — what the tester asked for (may be None / empty).
    verdict   — "up" or "down".
    image     — the shot filename the reaction is about.
    timestamp — ISO 8601 string; generated in UTC if not supplied by the caller.
    """
    row = {
        "timestamp": timestamp or datetime.now(timezone.utc).isoformat(),
        "intent": intent or None,
        "verdict": verdict,
        "image": image,
    }
    if verdict not in {"up", "down", "comment"}:
        raise ValueError("verdict must be up, down or comment")
    if comment is not None:
        if len(comment) > 2000:
            raise ValueError("comment exceeds 2000 characters")
        row["comment"] = comment.strip()
    encoded = (json.dumps(row, ensure_ascii=False) + "\n").encode("utf-8")
    if len(encoded) > MAX_FEEDBACK_LINE_BYTES:
        raise ValueError("feedback record exceeds size limit")
    with _lock:
        now = time.monotonic()
        while _recent_writes and now - _recent_writes[0] >= 60.0:
            _recent_writes.popleft()
        if len(_recent_writes) >= MAX_FEEDBACK_WRITES_PER_MINUTE:
            raise ValueError("feedback rate limit exceeded")
        rows = _validated_rows(FEEDBACK_FILE) if FEEDBACK_FILE.exists() else []
        rows.append(encoded.rstrip(b"\n"))
        while len(rows) > MAX_FEEDBACK_RECORDS or len(b"\n".join(rows)) + 1 > MAX_FEEDBACK_BYTES:
            rows.pop(0)
        if not rows or len(rows[-1]) + 1 > MAX_FEEDBACK_BYTES:
            raise ValueError("feedback record exceeds storage limit")
        # Atomic replacement makes the post-append count and byte invariants exact.
        tmp = FEEDBACK_FILE.with_suffix(".jsonl.tmp")
        tmp.write_bytes(b"\n".join(rows) + b"\n")
        tmp.replace(FEEDBACK_FILE)
        _recent_writes.append(now)
    return row


def verdict_for(image: str) -> str | None:
    """Return the most recent thumbs verdict ("up"/"down") logged for `image`, or None.

    Fail-safe: a missing or malformed feedback.jsonl never raises — the gallery just shows
    no badge. Later rows win, so a tester can change their mind and the newest reaction sticks.
    """
    if not FEEDBACK_FILE.exists():
        return None
    latest: str | None = None
    try:
        for line in _read_tail(FEEDBACK_FILE, MAX_FEEDBACK_BYTES).decode("utf-8", "replace").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(row, dict) and row.get("image") == image and row.get("verdict"):
                latest = row["verdict"]
    except (OSError, UnicodeDecodeError, ValueError):
        return None
    return latest


def verdict_index(images: set[str]) -> dict[str, str]:
    """Read a bounded feedback log once for a complete gallery request."""
    if not FEEDBACK_FILE.exists():
        return {}
    try:
        lines = _read_tail(FEEDBACK_FILE, MAX_FEEDBACK_BYTES).splitlines()[-MAX_FEEDBACK_RECORDS:]
    except (OSError, ValueError):
        return {}
    latest: dict[str, str] = {}
    for line in lines:
        try:
            row = json.loads(line)
        except (UnicodeDecodeError, json.JSONDecodeError):
            continue
        if isinstance(row, dict) and row.get("image") in images and isinstance(row.get("verdict"), str):
            latest[row["image"]] = row["verdict"]
    return latest
