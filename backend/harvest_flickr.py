"""Harvest (excellent photo -> camera settings) pairs from Flickr — the SECOND taste source.

Why a 2nd source: the Wikimedia Quality-Images pool caps around ~234k harvestable rows
(the flat "Quality images" category). To push the taste index toward the ~450k target we
add a structurally different, EXIF-rich, freely-licensed source. Flickr is the original
photographer-EXIF corpus: every CC-licensed photo retains the make/model + ISO/aperture/
shutter/focal the camera wrote, and `sort=interestingness-desc` surfaces the community-
juried *excellent* end of it — the analogue of Commons' juried Quality Images.

Patent/IP-clean, identical to harvest_wikimedia.py: we learn the numeric settings
(uncopyrightable facts) and **never redistribute pixels** — metadata only (settings +
license + url) into a CSV with the SAME columns harvest_wikimedia.py writes, so
embed_index.py can ingest both sources interchangeably. Only commercial-use-OK licenses
are kept (CC BY / CC BY-SA / CC0 / no-known-restrictions / US-Gov).

Needs a free Flickr API key (no billing) in FLICKR_API_KEY. Without it, the script prints
how to get one and exits 0 (so a CI run that lacks the secret is a clean no-op, not a fail).

Built for long unattended runs like the Wikimedia harvester: retries throttling/gateway
errors AND timeouts, streams rows to disk per page, and **resumes** via a sidecar page
cursor so a restart continues instead of re-fetching.

    FLICKR_API_KEY=xxxx python backend/harvest_flickr.py [num_pages] [output_csv]
"""
from __future__ import annotations

import csv
import json
import math
import os
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

REST = "https://api.flickr.com/services/rest/"
UA = "camera-autopilot-research/0.1 (https://github.com/zohartito/camera-autopilot)"
OUT = Path(__file__).resolve().parent.parent / "data" / "flickr_settings.csv"

# Same columns as harvest_wikimedia.py so embed_index.py reads either source unchanged.
FIELDNAMES = ["title", "iso", "f_number", "shutter_s", "focal_mm", "focal_35mm",
              "make", "model", "license", "url"]

# Commercial-use-OK licenses only (matches the Wikimedia harvester's CC0/CC-BY/CC-BY-SA scope).
# Flickr license ids: 4=CC BY, 5=CC BY-SA, 7=No known copyright, 8=US Gov, 9=CC0, 10=Public Domain Mark.
_LICENSES = "4,5,7,8,9,10"
_LICENSE_NAME = {"4": "CC BY 2.0", "5": "CC BY-SA 2.0", "7": "No known copyright restrictions",
                 "8": "United States Government Work", "9": "CC0 1.0", "10": "Public Domain Mark 1.0"}

_PER_PAGE = 100        # Flickr max is 500; 100 keeps each search + its EXIF fan-out modest.
_EXIF_SLEEP = 0.2      # be polite between the per-photo getExif calls


def _get(method: str, api_key: str, **params) -> dict:
    """Call a Flickr REST method as JSON, retrying through throttling / transient errors and
    timeouts — none should kill a long run. Returns the parsed JSON (may carry stat:fail)."""
    query = {"method": method, "api_key": api_key, "format": "json",
             "nojsoncallback": "1", **params}
    url = REST + "?" + urllib.parse.urlencode(query)
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
    raise RuntimeError("Flickr API: too many retries")


# First numeric/rational token in an EXIF string: an optional sign, a number (int or
# decimal), and an optional "/denominator". Matches the leading value of strings like
# "50 mm", "1/125 sec", "f/2.8", "100 ISO" — any units after it are ignored.
_NUM_TOKEN = re.compile(r"[-+]?\d*\.?\d+(?:/\d*\.?\d+)?")


def _ratio(value) -> float | None:
    """Parse a Flickr EXIF raw value to a float, tolerating units and prefixes.

    Flickr's `raw` EXIF strings often carry units or labels ("50 mm", "1/125 sec",
    "f/2.8", "100 ISO"). We strip the 'f/' aperture prefix, then take the FIRST
    numeric/rational token and evaluate it (a plain number or an 'a/b' rational like
    '1/250'). Returns None if there is no parseable number or the denominator is zero.
    """
    if value is None:
        return None
    text = str(value).strip()
    if text[:2].lower() == "f/":          # aperture prefix 'f/2.8' -> '2.8' (drop the 'f', keep the value)
        text = text[2:].strip()
    match = _NUM_TOKEN.search(text)
    if not match:
        return None
    token = match.group(0)
    try:
        if "/" in token:
            num, den = token.split("/", 1)
            return float(num) / float(den) if float(den) else None
        return float(token)
    except (ValueError, ZeroDivisionError):
        return None


def _exif_map(exif_resp: dict) -> dict:
    """Flatten flickr.photos.getExif's list into {tag: raw_value}. Flickr nests the value under
    'raw' (and sometimes 'clean'); prefer 'raw' (the unformatted EXIF string we can parse)."""
    out: dict = {}
    for item in (exif_resp.get("photo", {}) or {}).get("exif", []) or []:
        tag = item.get("tag")
        if not tag:
            continue
        raw = item.get("raw")
        out[tag] = raw.get("_content") if isinstance(raw, dict) else raw
    return out


def _f_number(exif: dict) -> float | None:
    """Real f-number from EXIF. Prefer the FNumber tag (already an f-stop); else convert
    the APEX ApertureValue (Av), where f = sqrt(2**Av). Returns None if neither is present."""
    f_tag = _ratio(exif.get("FNumber"))
    if f_tag is not None:
        return f_tag
    av = _ratio(exif.get("ApertureValue"))
    if av is not None:
        return math.sqrt(2.0 ** av)
    return None


def _shutter_s(exif: dict) -> float | None:
    """Real shutter in seconds from EXIF. Prefer the ExposureTime tag (already seconds);
    else convert the APEX ShutterSpeedValue (Tv), where seconds = 2**(-Tv). None if neither."""
    et = _ratio(exif.get("ExposureTime"))
    if et is not None:
        return et
    tv = _ratio(exif.get("ShutterSpeedValue"))
    if tv is not None:
        return 2.0 ** (-tv)
    return None


def _parse_photo(photo: dict, exif: dict) -> dict | None:
    """Build a settings row from a search-result photo + its EXIF map, or None if any of the four
    core settings is missing / out of a sane range (same filter as the Wikimedia harvester)."""
    iso = _ratio(exif.get("ISO") or exif.get("ISOSpeedRatings") or exif.get("PhotographicSensitivity"))
    # ApertureValue/ShutterSpeedValue are APEX, NOT f-number/seconds — prefer the real
    # FNumber/ExposureTime tags, converting APEX only as a fallback (else skip the photo).
    f_number = _f_number(exif)
    shutter = _shutter_s(exif)
    focal = _ratio(exif.get("FocalLength"))
    if None in (iso, f_number, shutter, focal):
        return None
    if not (0.7 <= f_number <= 64 and 0 < shutter <= 120 and 12 <= iso <= 409600 and focal > 0):
        return None
    pid = photo.get("id")
    title = photo.get("title") or pid
    server, secret = photo.get("server"), photo.get("secret")
    # Direct, static Flickr image URL so embed_index._fetch can download the actual pixels. The
    # page URL Flickr search hands back is HTML, not an image, and embed_index uses this stored url
    # verbatim for flickr: rows.
    # RESOLVED (Codex 2026-06-22): we now request the real per-photo size URLs via search
    # `extras=url_o,url_l,url_c,url_z,url_n` and store the LARGEST size Flickr ACTUALLY returns
    # (original/large > medium-800 > medium-640 > small-320). This fixes the prior data loss where
    # the synthesized 'b'/1024px derivative isn't generated for every photo (old/small uploads lack
    # it) — those used to 404 and silently drop from the corpus. We fall back to synthesizing the
    # '_b' URL ONLY when the search returned none of the url_* fields (then embed_index skips the
    # row rather than 404ing if even that is unavailable).
    image_url = (photo.get("url_o") or photo.get("url_l") or photo.get("url_c")
                 or photo.get("url_z") or photo.get("url_n"))
    if not image_url:
        image_url = (f"https://live.staticflickr.com/{server}/{pid}_{secret}_b.jpg"
                     if server and secret and pid else None)
    return {
        # Include the photo id so non-unique titles ("Untitled", "Sunset") don't collapse
        # under embed_index's title-based dedup — every Flickr row stays distinct.
        "title": f"flickr:{pid}:{title}",                 # namespaced + id-keyed (no Commons collision, no dedup loss)
        "iso": int(iso),
        "f_number": round(f_number, 2),
        "shutter_s": shutter,
        "focal_mm": round(focal, 1),
        "focal_35mm": _ratio(exif.get("FocalLengthIn35mmFormat")
                             or exif.get("FocalLengthIn35mmFilm")),
        "make": exif.get("Make"),
        "model": exif.get("Model"),
        "license": _LICENSE_NAME.get(str(photo.get("license")), str(photo.get("license"))),
        "url": image_url,
    }


def _read_cursor(cont_file: Path) -> dict:
    """Read the sidecar resume cursor as {"query": str|None, "page": int|None}.

    Stored as JSON `{"query": <text>, "page": <n or null>}`. page=None means the query is
    exhausted. A legacy sidecar (bare page number, or empty for exhausted, written before the
    query was tracked) is read with query=None so it never matches the current query and the
    run restarts cleanly rather than resuming on the wrong page."""
    text = cont_file.read_text().strip()
    if not text:
        return {"query": None, "page": None}     # legacy "exhausted" sentinel -> no query match
    try:
        data = json.loads(text)
        if isinstance(data, dict):
            page = data.get("page")
            return {"query": data.get("query"), "page": int(page) if page is not None else None}
    except (ValueError, TypeError):
        pass
    return {"query": None, "page": None}          # legacy bare page number -> unknown query


def _write_cursor(cont_file: Path, query: str, page: int | None) -> None:
    """Persist the resume cursor keyed by query. page=None marks the query exhausted."""
    cont_file.write_text(json.dumps({"query": query, "page": page}))


def harvest(api_key: str, text: str = "landscape", max_pages: int = 10,
            out: Path = OUT) -> int:
    """Stream CC-licensed, interestingness-sorted photos (with EXIF settings) from Flickr to CSV.

    Resumes from a sidecar page cursor; appends rather than clobbering. `text` is the search seed
    — vary it across runs (landscape, portrait, street, night, wildlife, ...) to widen the corpus
    beyond Flickr's per-query result cap and broaden the taste distribution. Lighter per-page size
    keeps the search + per-photo EXIF fan-out under the gateway timeout."""
    out.parent.mkdir(parents=True, exist_ok=True)
    cont_file = out.with_suffix(".page")
    start_page = 1
    mode = "w"
    total = 0
    # The sidecar cursor is keyed BY the query text: resuming a different `text` against the
    # same file would otherwise jump to the stored page and skip the new query's early pages.
    # Only resume when the stored query matches the current one; a changed query starts fresh.
    if out.exists() and cont_file.exists():
        saved = _read_cursor(cont_file)
        if saved.get("query") == text:
            if saved.get("page") is None:
                print("query already exhausted for this file; nothing to do")
                return 0
            start_page, mode = int(saved["page"]), "a"
            total = max(0, sum(1 for _ in out.open()) - 1)
            print(f"resuming append at page {start_page}, {total} rows", flush=True)
        else:
            print(f"query changed ({saved.get('query')!r} -> {text!r}); starting fresh", flush=True)

    with out.open(mode, newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=FIELDNAMES)
        if mode == "w":
            writer.writeheader()
        page = start_page
        for _ in range(max_pages):
            try:
                search = _get("flickr.photos.search", api_key, text=text,
                              license=_LICENSES, sort="interestingness-desc",
                              content_type="1",            # photos only (not screenshots/art)
                              media="photos", per_page=str(_PER_PAGE), page=str(page),
                              # license + per-photo size URLs (original / large-1024+ / medium-800 /
                              # medium-640 / small-320) so the row builder can pick the largest size
                              # Flickr ACTUALLY returns instead of synthesizing a possibly-404ing '_b'.
                              extras="license,url_o,url_l,url_c,url_z,url_n")
            except Exception as exc:  # noqa: BLE001 - keep streamed rows, stop cleanly
                print(f"  stopping at page {page} ({exc}); kept {total} rows", flush=True)
                break
            if search.get("stat") != "ok":
                print(f"  Flickr returned stat={search.get('stat')} "
                      f"({search.get('message')}); stopping at {total} rows", flush=True)
                break
            photos = search.get("photos", {})
            page_photos = photos.get("photo", []) or []
            pages_total = int(photos.get("pages", 0) or 0)
            for photo in page_photos:
                try:
                    exif_resp = _get("flickr.photos.getExif", api_key, photo_id=photo["id"])
                except Exception:  # noqa: BLE001 - a single bad EXIF fetch must not stop the run
                    continue
                if exif_resp.get("stat") != "ok":
                    continue
                row = _parse_photo(photo, _exif_map(exif_resp))
                if row is not None:
                    writer.writerow(row)
                    total += 1
                time.sleep(_EXIF_SLEEP)
            handle.flush()
            page += 1
            exhausted = pages_total and page > pages_total
            # Persist the resume point keyed by the query: page=None marks this query exhausted.
            _write_cursor(cont_file, text, None if exhausted else page)
            print(f"  page {page - 1}/{pages_total or '?'}: {total} rows so far", flush=True)
            if exhausted:
                print("query exhausted", flush=True)
                break
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
    key = os.environ.get("FLICKR_API_KEY", "").strip()
    if not key:
        print("FLICKR_API_KEY not set — this 2nd taste source needs a FREE Flickr API key.\n"
              "  1. Get one at https://www.flickr.com/services/apps/create/apply/ (no billing).\n"
              "  2. export FLICKR_API_KEY=<your_key>\n"
              "  3. python backend/harvest_flickr.py [num_pages] [output_csv]\n"
              "Skipping (clean no-op).")
        raise SystemExit(0)
    pages = int(sys.argv[1]) if len(sys.argv) > 1 else 1
    out_path = Path(sys.argv[2]) if len(sys.argv) > 2 else OUT
    count = harvest(key, max_pages=pages, out=out_path)
    print(f"harvested -> {count} rows in {out_path}")
    _report(out_path)
