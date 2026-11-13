"""RAW-transfer verdict for the 20-shot no-drop proof (``deploy/pi_bringup.sh --phase proof``).

Every accepted ``/shoot`` answer carries ``evidence.transport_receipt.downloaded_files``: the name,
size and SHA-256 of each file the camera delivered for the FINAL capture, computed by the service on
the board (``sonyremote.camera``) from the bytes as stored in its private staging directory. The
product keeps only the JPEG, so the ARW is hashed and then discarded: its record proves what crossed
USB to the board, not that a copy is kept.

The proof passes RAW transfer only when every accepted shot has exactly one non-empty ARW whose size
lies in the body's known band and whose SHA-256 is recorded, and no file hash repeats across shots.
Anything else is an explicit verdict (``raw_missing``, ``raw_implausible``, ...), never a pass.
Standard library only: pi_bringup.sh runs it with the system python3.
"""
from __future__ import annotations

import json
import re

RAW_SUFFIXES = (".arw",)
JPEG_SUFFIXES = (".jpg", ".jpeg")
_SHA256 = re.compile(r"[0-9a-f]{64}")

# Compressed-ARW sizes from the owner's bodies, read (sizes only, never copied) on 2026-09-27 from
# the local artifacts/ and backend/shots/ trees: ILCE-7M2 n=18, 24,777,472-25,047,808 bytes;
# ILCE-7M3 n=4, 24,976,128-24,992,512 bytes. A compressed 24 MP frame is nearly fixed length (only
# the embedded preview varies with the scene), so each band keeps about 3% either side of what was
# seen. A truncated, empty or uncompressed (~49 MB) ARW falls outside it and fails.
ARW_SIZE_BOUNDS = {
    "Sony ILCE-7M2 (Control)": (24_000_000, 26_000_000),
    "Sony Alpha-A7 III (PC Control)": (24_000_000, 26_000_000),
}
BOUNDS_SOURCE = ("observed compressed ARW sizes: ILCE-7M2 n=18 24777472-25047808 B, "
                 "ILCE-7M3 n=4 24976128-24992512 B (2026-09-27), about 3% margin")

# In order: the first that applies is the verdict.
VERDICTS = ("no_accepted_shots", "raw_bounds_unknown", "raw_missing", "raw_ambiguous",
            "raw_unhashed", "raw_implausible", "duplicate_hash", "passed")


def _size(value) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else None


def _sha256(value) -> str | None:
    return value if isinstance(value, str) and _SHA256.fullmatch(value) else None


def file_kind(name: str) -> str:
    lower = name.lower()
    return "raw" if lower.endswith(RAW_SUFFIXES) else "jpeg" if lower.endswith(JPEG_SUFFIXES) else "other"


def shot_files(payload) -> list[dict] | None:
    """The final capture's downloaded files from one /shoot answer, or None when it names none.

    Only ``evidence.transport_receipt`` is read: that is the final photograph's receipt (the meter
    frame's is ``meter_transport_receipt``), so no other field can stand in for it."""
    evidence = payload.get("evidence") if isinstance(payload, dict) else None
    receipt = evidence.get("transport_receipt") if isinstance(evidence, dict) else None
    files = receipt.get("downloaded_files") if isinstance(receipt, dict) else None
    if not isinstance(files, list):
        return None
    rows = []
    for entry in files:
        if not isinstance(entry, dict) or not isinstance(entry.get("name"), str):
            continue
        rows.append({"name": entry["name"], "kind": file_kind(entry["name"]),
                     "size": _size(entry.get("size")), "sha256": _sha256(entry.get("sha256")),
                     "retained": entry.get("retained") is True})
    return rows


def raw_transfer_verdict(rows: list[dict], model: str) -> dict:
    """Judge RAW transfer over the proof's shot rows (each with ``index``, ``http_status``, ``files``)."""
    accepted = [row for row in rows if row.get("http_status") == 200]
    bounds = ARW_SIZE_BOUNDS.get(model)
    missing, ambiguous, unhashed, implausible = [], [], [], []
    seen: dict[str, list[dict]] = {}
    for row in accepted:
        files = row.get("files") or []
        raws = [f for f in files if f.get("kind") == "raw"]
        if not raws:
            missing.append(row["index"])
        elif len(raws) > 1:
            ambiguous.append(row["index"])
        for f in files:
            if _sha256(f.get("sha256")):
                seen.setdefault(f["sha256"], []).append({"index": row["index"], "name": f["name"]})
        for f in raws:
            # Re-validated here, not only in shot_files(): the judge never trusts its input's shape.
            if _size(f.get("size")) is None or _sha256(f.get("sha256")) is None:
                unhashed.append(row["index"])
            elif bounds is not None and not bounds[0] <= f["size"] <= bounds[1]:
                implausible.append({"index": row["index"], "name": f["name"], "size": f["size"]})
    # The same bytes delivered twice means one frame stood in for another: a dropped shot.
    duplicates = [{"sha256": digest, "files": where} for digest, where in seen.items() if len(where) > 1]
    failing = {"no_accepted_shots": not accepted, "raw_bounds_unknown": bounds is None,
               "raw_missing": bool(missing), "raw_ambiguous": bool(ambiguous),
               "raw_unhashed": bool(unhashed), "raw_implausible": bool(implausible),
               "duplicate_hash": bool(duplicates)}
    verdict = next((name for name in VERDICTS if failing.get(name)), "passed")
    raw_sizes = [f["size"] for row in accepted for f in row.get("files") or []
                 if f.get("kind") == "raw" and _size(f.get("size")) is not None]
    return {
        "verdict": verdict, "passed": verdict == "passed", "model": model,
        "arw_size_bounds": list(bounds) if bounds else None, "bounds_source": BOUNDS_SOURCE,
        "accepted_shots": len(accepted), "shots_with_raw": len(accepted) - len(missing),
        "raw_size_min": min(raw_sizes) if raw_sizes else None,
        "raw_size_max": max(raw_sizes) if raw_sizes else None,
        "raw_missing": missing, "raw_ambiguous": ambiguous, "raw_unhashed": sorted(set(unhashed)),
        "raw_implausible": implausible, "duplicate_hashes": duplicates,
        "raw_retained": False,
        "note": ("Hashes are computed by the service on this machine from each downloaded file before "
                 "the ARW is discarded (the product keeps only the JPEG); they prove what crossed USB, "
                 "not a stored copy."),
    }


def shot_log(files: list[dict] | None) -> str:
    """One short per-shot fragment for the proof's console log."""
    raws = [f for f in files or [] if f.get("kind") == "raw"]
    if not raws:
        return "arw=MISSING"
    return " ".join(f"arw={f['size'] if f.get('size') is not None else '?'}B "
                    f"sha256={(f.get('sha256') or '?')[:12]}" for f in raws)


def shot_line(row: dict) -> str:
    """The proof's per-shot console line (kept here: an f-string with escaped quotes inside a bash
    single-quoted ``python3 -c`` is a SyntaxError before Python 3.12, the board's is 3.11)."""
    return (f"HTTP {row.get('http_status')} img={row.get('image') or '-'} "
            f"after_final={row.get('after_final')} {shot_log(row.get('files'))} "
            f"{float(row.get('elapsed_s') or 0):.2f}s")


def criterion(model: str) -> str:
    """The RAW criterion in words, for the proof's dry-run plan."""
    bounds = ARW_SIZE_BOUNDS.get(model)
    if bounds is None:
        return f"no ARW size bounds for {model or '<unset>'!r}, so RAW transfer cannot pass (raw_bounds_unknown)"
    return (f"every accepted shot has one ARW of {bounds[0]}-{bounds[1]} bytes with a SHA-256, "
            "and no file hash repeats across shots")


if __name__ == "__main__":
    import sys

    if len(sys.argv) == 3 and sys.argv[1] == "criterion":
        print(criterion(sys.argv[2]))
        sys.exit(0)
    if len(sys.argv) == 2 and sys.argv[1] == "shot-line":
        print(shot_line(json.load(sys.stdin)))
        sys.exit(0)
    sys.exit("usage: raw_proof.py criterion MODEL | raw_proof.py shot-line < ROW_JSON")
