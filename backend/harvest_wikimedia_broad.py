"""Harvest the BROAD Wikimedia Commons EXIF pool — the 2nd taste source toward ~450k.

Why this exists: Flickr paywalled its free API keys (2026), and Wikimedia *Quality Images* caps
~234k. The bigger FREE, commercial-safe pool is the rest of Commons' juried / own-work photography:
**Featured Pictures, Valued Images, and the deep `Photographs by camera` tree.** Those are CONTAINER
categories (mostly subcategories), so a flat `categorymembers` walk undershoots badly — this RECURSES.

Reuses `harvest_wikimedia.py` wholesale (`_get`, `_parse_file`, the all-four-settings filter, the
CSV schema) and adds three things:
  1. recursive category traversal — BFS with a visited-set + a depth cap (the camera tree is deep);
  2. de-dup against the existing QI corpus — skip titles already harvested (saves API calls;
     `embed_index` also dedups on `title`, so this is an efficiency win, not the correctness gate);
  3. a resumable, crash-safe JSON state sidecar (current category + cont-token + queue + visited) so
     a multi-day unattended run continues instead of restarting.

Patent/IP-clean, identical to the QI harvester: we learn the numeric settings (uncopyrightable
facts) and **never redistribute pixels** — metadata only, into a CSV with the SAME columns
`harvest_wikimedia.py` writes, so `embed_index` ingests it unchanged.

    python backend/harvest_wikimedia_broad.py [max_pages]
"""
from __future__ import annotations

import csv
import json
import os
import sys
import time
from collections import deque
from pathlib import Path

from harvest_wikimedia import FIELDNAMES, _get, _parse_file, _report
from harvest_wikimedia import OUT as QI_OUT

OUT = Path(__file__).resolve().parent.parent / "data" / "wikimedia_broad.csv"

# Juried / own-work photography roots beyond Quality Images. All are container categories whose
# value is in their SUBcategories, so we recurse. (Order = breadth-first seed order.)
SEED_CATEGORIES = [
    "Featured pictures on Wikimedia Commons",  # tiny, pristine — top community-juried tier
    "Valued images",                            # mid tier, best-in-scope
    "Photographs by camera",                    # the volume — deep tree of own-work camera photos
]
MAX_DEPTH = 6          # cap recursion; the camera tree is deep + branchy
PAGE_LIMIT = 100       # files+subcats per query (matches the QI harvester; under the gateway timeout)
CHECKPOINT_PAGES = 25  # persist traversal state every N pages (a crash re-pages only the gap)


def _state_path(out: Path) -> Path:
    return out.with_suffix(".state.json")


def _load_seen(out: Path) -> set[str]:
    """Titles already harvested (QI corpus + our own prior output) — skip them to save API calls."""
    seen: set[str] = set()
    for path in (QI_OUT, out):
        if path.exists():
            with path.open(newline="") as handle:
                seen.update(row["title"] for row in csv.DictReader(handle) if row.get("title"))
    return seen


def _load_state(out: Path):
    """Resume the traversal. Returns (current, queue, visited):
    current = [category, depth, cont] of the in-progress category, or None."""
    path = _state_path(out)
    if path.exists():
        data = json.loads(path.read_text())
        current = data.get("current")
        queue = deque(tuple(item) for item in data.get("queue", []))
        visited = set(data.get("visited", []))
        return current, queue, visited
    queue = deque((f"Category:{c}", 0) for c in SEED_CATEGORIES)
    visited = {f"Category:{c}" for c in SEED_CATEGORIES}
    return None, queue, visited


def _save_state(out: Path, current, queue, visited) -> None:
    """Atomic write (temp + os.replace) so a crash mid-write can't leave a half/empty state file
    that would crash `_load_state` on the next run."""
    payload = json.dumps({"current": current, "queue": list(queue), "visited": sorted(visited)})
    path = _state_path(out)
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(payload)
    os.replace(tmp, path)


def _get_pages(params: dict) -> dict:
    """`_get` + surface MediaWiki API errors. Those come back as HTTP-200 bodies with an `error`
    field, so `_get`'s HTTP-level retries miss them entirely. Retry the retryable ones
    (maxlag / ratelimited) with backoff; raise the rest so the caller stops cleanly with state intact
    rather than silently treating an error as an exhausted category."""
    for attempt in range(5):
        data = _get(params)
        error = data.get("error") if isinstance(data, dict) else None
        if not error:
            return data
        if error.get("code") in {"maxlag", "ratelimited"} and attempt < 4:
            time.sleep(2 ** attempt)
            continue
        raise RuntimeError(f"Wikimedia API error: {error}")
    raise RuntimeError("Wikimedia API: persistent error after retries")


def harvest(max_pages: int = 100_000, out: Path = OUT) -> int:
    """Walk the seed category trees, streaming files (with all four settings) to CSV. Resumable."""
    out.parent.mkdir(parents=True, exist_ok=True)
    seen = _load_seen(out)
    current, queue, visited = _load_state(out)
    total = max(0, sum(1 for _ in out.open()) - 1) if out.exists() else 0
    write_header = not out.exists()
    print(f"resuming: {len(queue)} categories queued, {len(visited)} visited, "
          f"{total} rows, {len(seen)} titles seen", flush=True)

    pages = 0
    with out.open("a", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=FIELDNAMES)
        if write_header:
            writer.writeheader()
            handle.flush()

        while (current or queue) and pages < max_pages:
            if current is None:
                category, depth = queue.popleft()
                cont = None
            else:
                category, depth, cont = current
            # Persist the in-progress category BEFORE the network call: popleft already removed it
            # from the queue, so if `_get_pages` raises (retry-exhaustion / API error) we must not
            # lose it — `current` now holds it and the end-of-run save resumes it next time.
            current = [category, depth, cont]

            params = {
                "action": "query", "generator": "categorymembers",
                "gcmtitle": category, "gcmtype": "file|subcat", "gcmlimit": str(PAGE_LIMIT),
                "prop": "imageinfo", "iiprop": "metadata|extmetadata|url",
            }
            if cont:
                params["gcmcontinue"] = cont
            try:
                data = _get_pages(params)
            except Exception as exc:  # noqa: BLE001 - keep every streamed row; stop cleanly, state intact
                print(f"  stopping ({exc}); kept {total} rows", flush=True)
                break
            pages += 1

            for page in data.get("query", {}).get("pages", {}).values():
                title = page.get("title", "")
                if title.startswith("Category:"):
                    if title not in visited and depth + 1 <= MAX_DEPTH:
                        visited.add(title)
                        queue.append((title, depth + 1))
                    continue
                if title in seen:
                    continue
                row = _parse_file(page)
                if row is not None:
                    writer.writerow(row)
                    seen.add(title)
                    total += 1
            handle.flush()

            cont = data.get("continue", {}).get("gcmcontinue")
            current = [category, depth, cont] if cont else None
            if pages % CHECKPOINT_PAGES == 0:
                print(f"  {pages} pages, {total} rows, {len(queue)} queued, {len(visited)} cats",
                      flush=True)
                os.fsync(handle.fileno())  # land the rows on disk BEFORE the state that points past them
                _save_state(out, current, queue, visited)
            if cont:
                time.sleep(0.5)

    _save_state(out, current, queue, visited)
    return total


if __name__ == "__main__":
    pages = int(sys.argv[1]) if len(sys.argv) > 1 else 100_000
    count = harvest(max_pages=pages)
    print(f"harvested -> {count} rows in {OUT}")
    _report(OUT)
