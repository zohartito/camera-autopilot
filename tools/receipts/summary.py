#!/usr/bin/env python3
"""Aggregate every receipt in the falsification directory into one markdown table.

Reads the JSON receipts written by the other receipt tools and prints a single
markdown table (what, when, pass/fail, image ids). Never opens the network and
never handles the token.

Exit 0 always (reporting tool); the table's PASS/FAIL column carries the verdicts.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import _common as common


def _image_ids(payload: dict) -> str:
    """Pull whatever image/session identity a receipt carries, for the table."""
    ids: list[str] = []
    if payload.get("published_image"):
        ids.append(str(payload["published_image"]))
    for frame in payload.get("frames", []) or []:
        dl = frame.get("download") or {}
        if dl.get("name"):
            ids.append(str(dl["name"]))
    saved = payload.get("saved") or {}
    if saved.get("session_id"):
        ids.append(str(saved["session_id"]))
    if payload.get("session_id") and not ids:
        ids.append(str(payload["session_id"]))
    if not ids:
        return "-"
    seen = list(dict.fromkeys(ids))
    return ", ".join(seen[:4]) + (" ..." if len(seen) > 4 else "")


def _escape(text: str) -> str:
    return str(text).replace("|", "\\|").replace("\n", " ")


def run(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--out-dir", default=str(common.DEFAULT_OUT_DIR),
                        help="directory holding the JSON receipts")
    args = parser.parse_args(argv)

    directory = Path(args.out_dir).expanduser()
    files = sorted(directory.glob("*.json")) if directory.is_dir() else []
    rows = []
    for path in files:
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (ValueError, OSError):
            continue
        if not isinstance(payload, dict):
            continue
        rows.append({
            "what": payload.get("what", path.stem),
            "when": payload.get("recorded_at", "-"),
            "verdict": "PASS" if payload.get("passed") else "FAIL",
            "images": _image_ids(payload),
            "file": path.name,
        })

    print(f"# Human-gated acceptance receipts\n")
    print(f"Directory: `{directory}`  \nReceipts found: {len(rows)}\n")
    if not rows:
        print("_No receipts yet. Run the body-press, wifi, night, and beginner tools first._")
        return 0

    passed = sum(1 for r in rows if r["verdict"] == "PASS")
    print("| What | When | Result | Image / session ids | Receipt |")
    print("|------|------|--------|---------------------|---------|")
    for r in rows:
        print(f"| {_escape(r['what'])} | {_escape(r['when'])} | {r['verdict']} "
              f"| {_escape(r['images'])} | `{_escape(r['file'])}` |")
    print(f"\n**{passed}/{len(rows)} receipts passing.**")
    return 0


if __name__ == "__main__":
    raise SystemExit(run())
