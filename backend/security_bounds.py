"""Small fail-closed resource guards for untrusted camera/model input."""
from __future__ import annotations

import json
from typing import Any, BinaryIO

MAX_HTTP_RESPONSE_BYTES = 1 * 1024 * 1024


def read_bounded(response: BinaryIO, *, limit: int = MAX_HTTP_RESPONSE_BYTES) -> bytes:
    """Read at most ``limit`` bytes, rejecting declared and actual overflow."""
    declared = getattr(response, "headers", {}).get("Content-Length")
    if declared:
        try:
            if int(declared) > limit:
                raise ValueError("upstream response exceeds byte limit")
        except ValueError as exc:
            if str(exc) == "upstream response exceeds byte limit":
                raise
            raise ValueError("upstream response has invalid Content-Length") from exc
    chunks: list[bytes] = []
    total = 0
    while True:
        try:
            chunk = response.read(min(64 * 1024, limit + 1 - total))
        except TypeError:
            # Minimal offline response fakes may expose only ``read()``.  They
            # are still checked immediately before any parse occurs.
            chunk = response.read()
            total += len(chunk)
            if total > limit:
                raise ValueError("upstream response exceeds byte limit")
            chunks.append(chunk)
            return b"".join(chunks)
        if not chunk:
            return b"".join(chunks)
        total += len(chunk)
        if total > limit:
            raise ValueError("upstream response exceeds byte limit")
        chunks.append(chunk)


def json_response(response: BinaryIO, *, limit: int = MAX_HTTP_RESPONSE_BYTES) -> Any:
    """Decode only a bounded JSON response; callers retain their schema validation."""
    return json.loads(read_bounded(response, limit=limit))


import threading
import time
from collections import deque


class RequestBudget:
    def __init__(self, *, limit: int = 12, window_seconds: float = 60.0):
        if limit < 1 or window_seconds <= 0:
            raise ValueError("request budget must be positive")
        self.limit = limit
        self.window_seconds = window_seconds
        self._events = deque()
        self._lock = threading.Lock()

    def check(self, *, now: float | None = None) -> float:
        """Return 0 if accepted, otherwise seconds until a retry is permitted."""
        now = time.monotonic() if now is None else now
        with self._lock:
            while self._events and self._events[0] <= now - self.window_seconds:
                self._events.popleft()
            if len(self._events) >= self.limit:
                return max(0.001, self._events[0] + self.window_seconds - now)
            self._events.append(now)
            return 0.0


def bounded_body_length(raw_length: str | None, *, maximum: int = 65536) -> int:
    """Validate declared body length; middleware must ALSO bound bytes actually read."""
    if raw_length is None:
        raise ValueError("Content-Length is required")
    try:
        size = int(raw_length)
    except (ValueError, TypeError) as exc:
        raise ValueError("invalid Content-Length") from exc
    if size < 0 or size > maximum:
        raise ValueError("request body exceeds byte budget")
    return size
