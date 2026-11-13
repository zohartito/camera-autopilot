"""Harvest (excellent photo -> camera settings) pairs from Wikimedia Commons.

Why Wikimedia Commons (verified by two independent research passes, 2026-06-20):
its **Quality Images** (~450k) and **Featured Pictures** (~22k) are *juried for
excellence*, freely licensed (CC0 / CC-BY / CC-BY-SA / public-domain — commercial
use OK with attribution), and retain full camera EXIF retrievable in ONE API call.

We learn the numeric settings (uncopyrightable facts) and **never redistribute
pixels** — so this pulls metadata only (settings + license + url) into a CSV.

Built for long unattended runs: retries throttling/gateway errors AND timeouts,
streams rows to disk per page, and **resumes** via a sidecar continue-token so a
restart continues instead of re-fetching. Smaller page size avoids gateway 504s.

    python backend/harvest_wikimedia.py [num_pages] [output_csv]
"""
from __future__ import annotations

import csv
import re
import statistics
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from collections import Counter
from pathlib import Path

from security_bounds import json_response

API = "https://commons.wikimedia.org/w/api.php"
UA = "camera-autopilot-research/0.1 (https://github.com/zohartito/camera-autopilot)"
OUT = Path(__file__).resolve().parent.parent / "data" / "wikimedia_qi.csv"

FIELDNAMES = ["title", "iso", "f_number", "shutter_s", "focal_mm", "focal_35mm",
              "make", "model", "license", "url"]


def _license_allowed(name: str | None) -> bool:
    """Conservative commercial-use filter: CC0/PD/CC-BY/CC-BY-SA only, never NC/ND/unknown."""
    text = str(name or "").strip().lower()
    if not text:
        return False
    if re.search(r"(^|[\s_-])(nc|noncommercial|non-commercial)($|[\s_-])", text):
        return False
    if re.search(r"(^|[\s_-])(nd|no-derivative|noderivatives)($|[\s_-])", text):
        return False
    compact = text.replace("-", " ").replace("_", " ")
    if "cc0" in compact or "public domain" in compact or "no known copyright" in compact:
        return True
    if text.startswith("pd"):
        return True
    return "cc by" in compact


def _get(params: dict) -> dict:
    """GET the API, retrying through throttling and transient gateway errors
    (429/500/502/503/504) AND network timeouts — none should kill a long run."""
    query = {**params, "format": "json", "maxlag": "5"}
    url = API + "?" + urllib.parse.urlencode(query)
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    for attempt in range(7):
        try:
            with urllib.request.urlopen(req, timeout=60) as resp:
                return json_response(resp)
        except urllib.error.HTTPError as exc:
            if exc.code in {429, 500, 502, 503, 504} and attempt < 6:
                wait = exc.headers.get("Retry-After")
                time.sleep(min(int(wait) if wait else 2 ** attempt, 30))
                continue
            raise
        except (urllib.error.URLError, TimeoutError, ConnectionError):
            if attempt < 6:
                time.sleep(2 ** attempt)
                continue
            raise
    raise RuntimeError("Wikimedia API: too many retries")


def _ratio(value) -> float | None:
    if value is None:
        return None
    text = str(value).strip()
    try:
        if "/" in text:
            num, den = text.split("/", 1)
            return float(num) / float(den) if float(den) else None
        return float(text)
    except (ValueError, ZeroDivisionError):
        return None


def _meta_map(metadata) -> dict:
    out = {}
    for item in metadata or []:
        if isinstance(item, dict) and "name" in item:
            out[item["name"]] = item.get("value")
    return out


def _parse_file(page: dict) -> dict | None:
    info = (page.get("imageinfo") or [{}])[0]
    meta = _meta_map(info.get("metadata"))
    iso = _ratio(meta.get("ISOSpeedRatings"))
    f_number = _ratio(meta.get("FNumber"))
    shutter = _ratio(meta.get("ExposureTime"))
    focal = _ratio(meta.get("FocalLength"))
    if None in (iso, f_number, shutter, focal):
        return None
    if not (0.7 <= f_number <= 64 and 0 < shutter <= 120 and 12 <= iso <= 409600 and focal > 0):
        return None
    ext = info.get("extmetadata") or {}
    license_name = (ext.get("LicenseShortName") or {}).get("value")
    if not _license_allowed(license_name):
        return None
    return {
        "title": page.get("title"),
        "iso": int(iso),
        "f_number": round(f_number, 2),
        "shutter_s": shutter,
        "focal_mm": round(focal, 1),
        "focal_35mm": _ratio(meta.get("FocalLengthIn35mmFilm")),
        "make": meta.get("Make"),
        "model": meta.get("Model"),
        "license": license_name,
        "url": info.get("url"),
    }


def harvest(category: str = "Quality images", limit: int = 100,
            max_pages: int = 10000, out: Path = OUT) -> int:
    """Stream files (with full settings) from a juried Commons category to CSV.
    Resumes from a sidecar continue-token; appends rather than clobbering. Lighter
    page size (limit) keeps each query under the gateway timeout."""
    out.parent.mkdir(parents=True, exist_ok=True)
    cont_file = out.with_suffix(".cont")
    cont = None
    mode = "w"
    total = 0
    if out.exists() and cont_file.exists():
        token = cont_file.read_text().strip()
        if not token:
            print("category already exhausted for this file; nothing to do")
            return 0
        cont, mode = token, "a"
        total = max(0, sum(1 for _ in out.open()) - 1)
        print(f"resuming append at {total} rows", flush=True)

    with out.open(mode, newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=FIELDNAMES)
        if mode == "w":
            writer.writeheader()
        for page_index in range(max_pages):
            params = {
                "action": "query", "generator": "categorymembers",
                "gcmtitle": f"Category:{category}", "gcmtype": "file",
                "gcmlimit": str(limit),
                "prop": "imageinfo", "iiprop": "metadata|extmetadata|url",
            }
            if cont:
                params["gcmcontinue"] = cont
            try:
                data = _get(params)
            except Exception as exc:  # noqa: BLE001 - keep all streamed rows, stop cleanly
                print(f"  stopping at page {page_index + 1} ({exc}); kept {total} rows", flush=True)
                break
            for page in data.get("query", {}).get("pages", {}).values():
                row = _parse_file(page)
                if row is not None:
                    writer.writerow(row)
                    total += 1
            handle.flush()
            cont = data.get("continue", {}).get("gcmcontinue")
            cont_file.write_text(cont or "")  # persist resume point (empty = exhausted)
            if not cont:
                print("category exhausted", flush=True)
                break
            if page_index % 25 == 0:
                print(f"  page {page_index + 1}: {total} rows so far", flush=True)
            time.sleep(0.5)
    return total


def _report(path: Path) -> None:
    with path.open() as handle:
        rows = list(csv.DictReader(handle))
    if not rows:
        print("no rows yet")
        return
    isos = sorted(int(r["iso"]) for r in rows)
    fs = sorted(float(r["f_number"]) for r in rows)
    print(f"  ISO    : {min(isos)}..{max(isos)} (median {int(statistics.median(isos))})")
    print(f"  f-num  : {min(fs):.1f}..{max(fs):.1f}")
    print(f"  cameras: {dict(Counter((r['model'] or '?') for r in rows).most_common(5))}")


if __name__ == "__main__":
    pages = int(sys.argv[1]) if len(sys.argv) > 1 else 1
    out_path = Path(sys.argv[2]) if len(sys.argv) > 2 else OUT
    count = harvest(max_pages=pages, out=out_path)
    print(f"harvested -> {count} rows in {out_path}")
    _report(out_path)
