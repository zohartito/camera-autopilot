#!/usr/bin/env python3
"""Explicit single-camera preview/focus probe. Stop other camera operations first.

No setting writes, wake loop, still captures, or hidden fallback. A preview is
written only after a complete bounded JPEG decode; failure produces no JPEG.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from dataclasses import asdict
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from sonyremote.isolated_preview import capture_preview_isolated, probe_focus_isolated


def _write_private(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, "wb") as stream:
        stream.write(data)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--model", required=True)
    parser.add_argument("--port", required=True)
    parser.add_argument("--mode", choices=("preview", "focus"), required=True)
    parser.add_argument("--timeout", type=float, default=8.0)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--receipt", type=Path)
    args = parser.parse_args(argv)
    if args.mode == "preview" and args.output is None:
        parser.error("preview requires --output")
    try:
        if args.mode == "preview":
            result = capture_preview_isolated(args.model, args.port, timeout_seconds=args.timeout)
            payload = asdict(result)
            payload.pop("jpeg")
            _write_private(args.output, result.jpeg)
            payload.update(ok=True, output=str(args.output.resolve()), still_exposures=0)
        else:
            payload = probe_focus_isolated(args.model, args.port, timeout_seconds=args.timeout)
        status = 0
    except Exception as exc:
        payload = {"ok": False, "model": args.model, "port": args.port, "mode": args.mode,
                   "error": str(exc), "code": getattr(exc, "code", "probe_failed"),
                   "still_exposures": 0}
        status = 1
    if args.receipt:
        _write_private(args.receipt, (json.dumps(payload, indent=2) + "\n").encode())
    print(json.dumps(payload, indent=2))
    return status


if __name__ == "__main__":
    raise SystemExit(main())
