"""One-time phone pairing code: 6 digits, 10 minutes, single use, five tries.

In lan-proxy exposure the server issues a code at startup and prints it on the Mac terminal only.
The phone trades it once for the API token (POST /api/pair). A code is cleared on success, on
expiry and after MAX_FAILURES wrong or malformed attempts; a new one needs a restart or the
loopback-only, token-authenticated POST /api/pair/new. With 5 guesses in a space of 10**6 an
attacker's chance per issued code is 5e-6.
"""
from __future__ import annotations

import hmac
import os
import re
import secrets
import threading
import time

CODE_DIGITS = 6
CODE_TTL_SECONDS = 600
MAX_FAILURES = 5
_CODE_PATTERN = re.compile(r"[0-9]{6}")


def enabled(env=None) -> bool:
    """Pairing exists only behind the confirmed HTTPS proxy (run.sh exports the exposure)."""
    env = os.environ if env is None else env
    return env.get("CAMERA_AUTOPILOT_EXPOSURE", "local") == "lan-proxy"


class PairingCodes:
    def __init__(self, *, ttl_seconds: float = CODE_TTL_SECONDS, max_failures: int = MAX_FAILURES,
                 clock=time.monotonic):
        self._ttl = ttl_seconds
        self._max_failures = max_failures
        self._clock = clock
        self._lock = threading.Lock()
        self._code: str | None = None
        self._expires_at = 0.0
        self._failures = 0

    def issue(self) -> str:
        """Replace any active code with a fresh one and reset the failure count."""
        code = f"{secrets.randbelow(10 ** CODE_DIGITS):0{CODE_DIGITS}d}"
        with self._lock:
            self._code = code
            self._expires_at = self._clock() + self._ttl
            self._failures = 0
        return code

    def active(self) -> bool:
        with self._lock:
            self._expire_locked()
            return self._code is not None

    def redeem(self, candidate) -> bool:
        """True exactly once for the active code; every other attempt counts as a failure."""
        with self._lock:
            self._expire_locked()
            if self._code is None:
                return False
            text = candidate if isinstance(candidate, str) else ""
            well_formed = bool(_CODE_PATTERN.fullmatch(text))
            # Constant-time compare on six ASCII bytes; a malformed candidate is swapped for
            # filler that can never match, so the comparison is the same size every time.
            probe = text if well_formed else "x" * CODE_DIGITS
            matched = hmac.compare_digest(probe.encode("ascii"), self._code.encode("ascii")) and well_formed
            if matched:
                self._clear_locked()
                return True
            self._failures += 1
            if self._failures >= self._max_failures:
                self._clear_locked()
            return False

    def _expire_locked(self) -> None:
        if self._code is not None and self._clock() >= self._expires_at:
            self._clear_locked()

    def _clear_locked(self) -> None:
        self._code = None
        self._expires_at = 0.0
        self._failures = 0
