"""M1 retrieval (taste) layer — match a scene to similar excellent photos.

Run with the ML venv (torch + open_clip):
    python backend/embed_index.py build [n]
    python backend/embed_index.py recommend <image>

Downloads small thumbnails of the harvested Quality Images, embeds each with CLIP,
and saves an index of (vector -> the camera settings used). At capture time we embed
the live scene, find the nearest *excellent* photos, and read off the settings they
chose — the taste. physics.py then makes those correctly exposed.

**Fast + unattended-safe:**
- Downloads run concurrently (the real bottleneck), via the *direct* upload.wikimedia.org
  thumbnail URL (skips the Special:FilePath 302 redirect) with a Special:FilePath fallback.
- Images embed in *small* GPU batches that fit comfortably even in tight VRAM (no OOM thrash).
- Resumable + checkpointed (skips already-embedded photos; logs the live rate at each
  checkpoint). Safe to re-run — it continues where it left off.
"""
from __future__ import annotations

import concurrent.futures
import csv
import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

import numpy as np
try:
    import open_clip
    import torch
except ModuleNotFoundError:  # Keep the download/index safety helpers testable without the ML lane.
    open_clip = None
    torch = None
from PIL import Image
from image_safety import MAX_IMAGE_SOURCE_BYTES, open_rgb, open_rgb_from_bytes

_REPO = Path(__file__).resolve().parent.parent
CSV = _REPO / "data" / "wikimedia_qi.csv"            # source 1: Wikimedia Quality Images (~234k harvested)
BROAD_CSV = _REPO / "data" / "wikimedia_broad.csv"   # source 3: broad Commons EXIF (Featured/Valued/by-camera) toward ~450k
INDEX = _REPO / "data" / "taste_index.npz"
# Only local Commons-derived CSVs are accepted. Dataset rows must never select
# arbitrary outbound hosts; the historical Flickr URL path is tombstoned until
# it has a pinned, reviewed source policy.
SOURCES = [CSV, BROAD_CSV]
UA = "camera-autopilot-research/0.1 (https://github.com/zohartito/camera-autopilot)"

MODEL_NAME, PRETRAINED = "ViT-B-32", "laion2b_s34b_b79k"
THUMB_WIDTH = 320        # a *cached* Wikimedia size (~0.5s); 256 is generated on-demand (~2.7s)
DOWNLOAD_WORKERS = 1     # Wikimedia blocks concurrency (1/36 succeed @ 6+); serial @320 is ~2/s, reliable
DOWNLOAD_CHUNK = 16
GPU_BATCH = 16
CHECKPOINT_EVERY = 200   # finer crash-safety + live rate visibility
_META_KEYS = ("iso", "f_number", "shutter_s", "focal_mm", "titles")
# Keep the public ingestion limit explicitly coupled to the shared image-source
# contract.  A dataset cannot make an image source less bounded than the API.
MAX_INDEX_IMAGE_BYTES = min(8 * 1024 * 1024, MAX_IMAGE_SOURCE_BYTES)
DOWNLOAD_DEADLINE_S = 30.0
_ALLOWED_THUMB_HOSTS = frozenset({"upload.wikimedia.org"})


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """Do not let a response select another request target."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise urllib.error.HTTPError(req.full_url, code, "redirects are not allowed", headers, fp)

# Lazy singletons so repeated recommend() calls reuse the loaded CLIP model and index
# instead of paying the load cost every time.
_MODEL_CACHE: tuple | None = None   # (model, preprocess, device)
_QUERY_CACHE: tuple | None = None   # ((path, mtime_ns, size), unit embedding) — most recent only
_INDEX_CACHE: dict[str, np.ndarray] | None = None
_INDEX_STORAGE = "not_loaded"
_INDEX_COMPACT_ERROR: str | None = None


def _device() -> str:
    if torch is None:
        return "cpu"
    if torch.cuda.is_available():
        return "cuda"  # the PC's 4070
    if torch.backends.mps.is_available():
        return "mps"   # the Mac's GPU
    return "cpu"


def _load_model(device: str):
    if open_clip is None or torch is None:
        raise RuntimeError(
            "CLIP embedding requires the ML environment (install torch and open_clip_torch)."
        )
    # Capture must use the exact encoder used for the corpus. Provision/download
    # is explicit; never block the shutter on a model download or a broken cache.
    from runtime_resources import verified_clip_weights
    model, _, preprocess = open_clip.create_model_and_transforms(
        MODEL_NAME, pretrained=str(verified_clip_weights()))
    return model.to(device).eval(), preprocess


def _validated_upload_url(value: object) -> str | None:
    """Permit only a direct Wikimedia upload URL already present in a dataset row.

    Commons File titles resolve through a redirecting endpoint and cannot safely be
    transformed into a content-addressed upload path without the media metadata.
    Title-only rows are therefore intentionally unavailable.
    """
    if not isinstance(value, str):
        return None
    try:
        parsed = urllib.parse.urlsplit(value)
        port = parsed.port
    except ValueError:
        return None
    if (
        parsed.scheme != "https" or parsed.hostname != "upload.wikimedia.org"
        or parsed.username is not None or parsed.password is not None
        or port not in (None, 443) or parsed.query or parsed.fragment
        or not parsed.path.startswith("/wikipedia/")
    ):
        return None
    return value


def _download_url(url: str, timeout: int = 20) -> bytes | None:
    """GET image bytes with polite retries. Hosts rate-limit per IP; on 429 wait the Retry-After
    they ask for and retry — self-pacing under the limit instead of hammering it (which gets us
    throttled harder). Transient 500/503 and network errors back off and retry."""
    try:
        parsed = urllib.parse.urlsplit(url)
        port = parsed.port
    except ValueError:
        return None
    if (
        parsed.scheme != "https"
        or parsed.hostname not in _ALLOWED_THUMB_HOSTS
        or parsed.username is not None
        or parsed.password is not None
        or port not in (None, 443)
    ):
        return None
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    opener = urllib.request.build_opener(_NoRedirect)
    deadline = time.monotonic() + DOWNLOAD_DEADLINE_S

    def remaining() -> float:
        return deadline - time.monotonic()

    def sleep_with_deadline(seconds: float) -> bool:
        budget = remaining()
        if budget <= 0:
            return False
        time.sleep(min(max(0.0, seconds), budget))
        return remaining() > 0

    def limit_response_read_timeout(resp: object) -> bool:
        """Bound the *next* blocking read by the remaining aggregate budget.

        ``urlopen`` applies its timeout to connection/open, not reliably to every
        later body read.  urllib responses expose the socket either directly or
        under ``fp.raw._sock`` depending on the handler/version, so set the
        shortest live budget through every supported path before reading.
        Refuse a response whose transport cannot be bounded.
        """
        budget = remaining()
        if budget <= 0:
            return False
        read_timeout = max(0.001, min(float(timeout), budget))
        fp = getattr(resp, "fp", None)
        raw = getattr(fp, "raw", None)
        candidates = (
            resp,
            getattr(resp, "_sock", None),
            fp,
            getattr(fp, "_sock", None),
            raw,
            getattr(raw, "_sock", None),
        )
        for candidate in candidates:
            setter = getattr(candidate, "settimeout", None)
            if callable(setter):
                setter(read_timeout)
                return True
        return False

    for attempt in range(6):
        budget = remaining()
        if budget <= 0:
            return None
        try:
            with opener.open(req, timeout=min(timeout, budget)) as resp:
                if remaining() <= 0:
                    return None
                declared = resp.headers.get("Content-Length")
                if declared is not None:
                    try:
                        if int(declared) > MAX_INDEX_IMAGE_BYTES:
                            return None
                    except ValueError:
                        return None
                chunks: list[bytes] = []
                total = 0
                while True:
                    chunk_budget = remaining()
                    if chunk_budget <= 0:
                        return None
                    if not limit_response_read_timeout(resp):
                        return None
                    chunk = resp.read(min(64 * 1024, MAX_INDEX_IMAGE_BYTES + 1 - total))
                    if remaining() <= 0:
                        return None
                    if not chunk:
                        return b"".join(chunks)
                    total += len(chunk)
                    if total > MAX_INDEX_IMAGE_BYTES:
                        return None
                    chunks.append(chunk)
        except urllib.error.HTTPError as exc:
            if exc.code == 429 and attempt < 5:
                try:
                    retry_after = int(exc.headers.get("Retry-After", 5))
                except (TypeError, ValueError):
                    retry_after = 5
                if not sleep_with_deadline(min(max(retry_after, 0), 60)):
                    return None
                continue
            if exc.code in (500, 503) and attempt < 5:
                if not sleep_with_deadline(2 ** attempt):
                    return None
                continue
            return None
        except (urllib.error.URLError, TimeoutError, ConnectionError):
            if attempt < 5:
                if not sleep_with_deadline(2 ** attempt):
                    return None
                continue
            return None
    return None


def _embed(model, preprocess, device, img: Image.Image) -> np.ndarray:
    """Embed a single image (used by recommend)."""
    if torch is None:
        raise RuntimeError("CLIP embedding requires the ML environment (install torch and open_clip_torch).")
    with torch.no_grad():
        tensor = preprocess(img).unsqueeze(0).to(device)
        vec = model.encode_image(tensor)
        vec = vec / vec.norm(dim=-1, keepdim=True)
    return vec.cpu().numpy()[0].astype("float32")


def _embed_batch(model, preprocess, device, images: list) -> np.ndarray:
    """Embed a batch in one GPU call; halve on the rare OOM."""
    if torch is None:
        raise RuntimeError("CLIP embedding requires the ML environment (install torch and open_clip_torch).")
    try:
        tensors = torch.stack([preprocess(im) for im in images]).to(device)
        with torch.no_grad():
            vecs = model.encode_image(tensors)
            vecs = vecs / vecs.norm(dim=-1, keepdim=True)
        return vecs.cpu().numpy().astype("float32")
    except RuntimeError as exc:
        if "out of memory" not in str(exc).lower() or len(images) == 1:
            raise
        if device == "cuda":
            torch.cuda.empty_cache()
        mid = len(images) // 2
        return np.vstack([_embed_batch(model, preprocess, device, images[:mid]),
                          _embed_batch(model, preprocess, device, images[mid:])])


def _fetch(row: dict):
    """Download + decode an image and parse its settings. (title, settings, image) or None.

    Only a validated, direct upload.wikimedia.org URL is fetched.  Historical
    third-party and title-only Commons rows are skipped before network I/O."""
    title = row.get("title")
    if not title:
        return None
    try:
        settings = (int(row["iso"]), float(row["f_number"]),
                    float(row["shutter_s"]), float(row["focal_mm"]))
    except (KeyError, ValueError):
        return None
    if not isinstance(title, str) or not title.startswith("File:"):
        return None
    url = _validated_upload_url(row.get("url"))
    if url is None:
        return None
    data = _download_url(url)
    if not data:
        return None
    try:
        return title, settings, open_rgb_from_bytes(data)
    except Exception:  # noqa: BLE001 - bad/truncated image, skip
        return None


def _save(vectors: list, meta: dict) -> None:
    """Atomically replace a checkpoint so interruption cannot corrupt the last good index."""
    global _INDEX_CACHE, _INDEX_STORAGE, _INDEX_COMPACT_ERROR
    temporary = INDEX.with_name(f".{INDEX.name}.tmp")
    with temporary.open("wb") as handle:
        np.savez(handle, embeddings=np.vstack(vectors),
                 iso=np.array(meta["iso"]), f_number=np.array(meta["f_number"]),
                 shutter_s=np.array(meta["shutter_s"]), focal_mm=np.array(meta["focal_mm"]),
                 titles=np.array(meta["titles"]))
        handle.flush()
        os.fsync(handle.fileno())
    temporary.replace(INDEX)
    _INDEX_CACHE = None
    _INDEX_STORAGE = "not_loaded"
    _INDEX_COMPACT_ERROR = None


def _read_rows(sources: list[Path]) -> list[dict]:
    """Concatenate the rows of every source CSV that exists, skipping any that don't.
    Each harvester writes the same columns, so the rows are directly compatible with _fetch."""
    rows: list = []
    for path in sources:
        if not path.exists():
            print(f"  source not found, skipping: {path}", flush=True)
            continue
        with path.open(newline="") as handle:
            rows.extend(csv.DictReader(handle))
    return rows


def build(n: int | None = None, sources: list | None = None) -> None:
    """Embed harvested photos into the taste index — concurrent + batched + resumable.

    Reads from every CSV in `sources` (default: Wikimedia + Flickr), skipping any that don't
    exist, and concatenates the rows before the resume/embed loop. Dedup is by `title`, and the
    Flickr titles are namespaced `flickr:...` so the two sources can't collide."""
    device = _device()
    print(f"loading CLIP ({MODEL_NAME}) on {device} ...", flush=True)
    model, preprocess = _load_model(device)

    rows = _read_rows(SOURCES if sources is None else sources)
    if n:
        rows = rows[:n]

    done: set = set()
    vectors: list = []
    meta: dict = {k: [] for k in _META_KEYS}
    if INDEX.exists():
        with np.load(INDEX, allow_pickle=False) as prev:
            done = set(prev["titles"].tolist())
            vectors = list(prev["embeddings"])
            for key in _META_KEYS:
                meta[key] = list(prev[key])
        print(f"resuming from {len(done)} already embedded", flush=True)

    todo = [r for r in rows if r.get("title") and r["title"] not in done]
    print(f"{len(todo)} to embed — {DOWNLOAD_WORKERS} parallel downloads, GPU batch {GPU_BATCH}", flush=True)

    since_ckpt = 0
    last_t, last_n = time.time(), len(vectors)
    with concurrent.futures.ThreadPoolExecutor(max_workers=DOWNLOAD_WORKERS) as pool:
        for start in range(0, len(todo), DOWNLOAD_CHUNK):
            chunk = todo[start:start + DOWNLOAD_CHUNK]
            fetched = [r for r in pool.map(_fetch, chunk) if r is not None]
            for i in range(0, len(fetched), GPU_BATCH):
                sub = fetched[i:i + GPU_BATCH]
                vecs = _embed_batch(model, preprocess, device, [img for _, _, img in sub])
                for (title, (iso, f_number, shutter, focal), _), vec in zip(sub, vecs):
                    if title in done:
                        continue
                    vectors.append(vec)
                    meta["iso"].append(iso)
                    meta["f_number"].append(f_number)
                    meta["shutter_s"].append(shutter)
                    meta["focal_mm"].append(focal)
                    meta["titles"].append(title)
                    done.add(title)
                    since_ckpt += 1
            if since_ckpt >= CHECKPOINT_EVERY:
                _save(vectors, meta)
                now = time.time()
                rate = (len(vectors) - last_n) / max(0.1, now - last_t)
                print(f"  checkpoint: {len(vectors)} embedded ({rate:.1f}/s)", flush=True)
                last_t, last_n, since_ckpt = now, len(vectors), 0

    _save(vectors, meta)
    print(f"index now: {len(vectors)} photos -> {INDEX}", flush=True)


def embed_query(image_path: str, model, preprocess, device) -> np.ndarray:
    """Embed a query frame, caching the most recent (path -> unit vector).

    A single shot embeds the meter frame for BOTH taste retrieval (`recommend`) and subject
    recognition. This one-entry cache lets the second consumer of the same frame reuse the CLIP
    embedding instead of paying for it twice (~1s on the puck's CPU). A new path replaces the
    entry. The key includes the file's mtime and size, not just its path: this cache is
    process-global, so any caller that rewrites a fixed path (a temp file, a re-saved upload)
    must get a fresh embedding rather than the previous image's."""
    global _QUERY_CACHE
    stat = Path(image_path).stat()
    key = (str(image_path), stat.st_mtime_ns, stat.st_size)
    if _QUERY_CACHE is not None and _QUERY_CACHE[0] == key:
        return _QUERY_CACHE[1]
    vec = _embed(model, preprocess, device, open_rgb(image_path))
    _QUERY_CACHE = (key, vec)
    return vec


def _get_model():
    """Load the CLIP model + preprocess once, then reuse it across calls."""
    global _MODEL_CACHE
    if _MODEL_CACHE is None:
        device = _device()
        model, preprocess = _load_model(device)
        _MODEL_CACHE = (model, preprocess, device)
    return _MODEL_CACHE


def _get_index():
    """Prefer a verified lossless mapped corpus, with legacy resident fallback.

    Prepared arrays stay read-only and metadata pages load on demand. The legacy
    fallback caches real arrays, because an NpzFile reloads each accessed member.
    """
    global _INDEX_CACHE, _INDEX_STORAGE, _INDEX_COMPACT_ERROR
    if _INDEX_CACHE is None:
        # Explicit legacy mode remains useful for reproducible comparisons. Tests
        # and alternate datasets resolve their own sibling, never the main corpus.
        compact = Path(os.environ.get("CAMERA_AUTOPILOT_TASTE_COMPACT", str(INDEX.with_suffix(".compact"))))
        if os.environ.get("CAMERA_AUTOPILOT_TASTE_STORAGE", "auto") != "legacy" and compact.exists():
            try:
                from compact_taste_index import load
                from runtime_resources import _file_receipt
                original_manifest = json.loads(INDEX.with_suffix(".MANIFEST.json").read_text())
                if INDEX.exists() and _file_receipt(INDEX, original_manifest["bytes"], original_manifest["sha256"])["state"] != "verified":
                    raise ValueError("source archive does not match canonical manifest")
                _INDEX_CACHE = load(compact, original_manifest["sha256"])
                _INDEX_STORAGE = "lossless_mmap"
                _INDEX_COMPACT_ERROR = None
                return _INDEX_CACHE
            except (OSError, ValueError, KeyError, TypeError) as exc:
                _INDEX_COMPACT_ERROR = str(exc)
                # Preserve the original compatible path; health exposes this fallback.
        if not INDEX.exists():
            raise FileNotFoundError(
                f"taste index not found at {INDEX} — run `python {Path(__file__).name} build` first")
        required = ("embeddings", *_META_KEYS)
        with np.load(INDEX, allow_pickle=False) as archive:
            missing = [key for key in required if key not in archive.files]
            if missing:
                raise ValueError(f"taste index at {INDEX} is missing arrays: {missing}")
            idx = {key: np.asarray(archive[key]) for key in required}
        rows = idx["embeddings"].shape[0]
        if rows == 0:
            raise ValueError(f"taste index at {INDEX} is empty — rebuild it with `build`")
        bad_lengths = {
            key: len(idx[key])
            for key in _META_KEYS
            if len(idx[key]) != rows
        }
        if bad_lengths:
            raise ValueError(
                f"taste index metadata lengths differ from {rows} embeddings: {bad_lengths}")
        _INDEX_CACHE = idx
        _INDEX_STORAGE = "legacy_resident_npz"
    return _INDEX_CACHE


def _weighted_median(values: list, weights: list) -> float:
    """Median of `values` weighted by `weights` — the point where cumulative weight
    crosses half the total. Robust to outliers while letting closer matches count more."""
    order = np.argsort(values)
    vals = np.asarray(values, dtype="float64")[order]
    wts = np.asarray(weights, dtype="float64")[order]
    cutoff = wts.sum() / 2.0
    crossed = np.searchsorted(np.cumsum(wts), cutoff)
    return float(vals[min(crossed, len(vals) - 1)])


def recommend(image_path: str, k: int = 15) -> dict:
    """Embed a query scene, find the k nearest excellent photos, summarize their settings."""
    if k < 1:
        raise ValueError(f"k must be >= 1, got {k}")
    import perception_client
    if perception_client.remote():  # split runtime: CLIP runs in the perception role
        return perception_client.recommend(image_path, k)
    idx = _get_index()
    model, preprocess, device = _get_model()
    query = embed_query(image_path, model, preprocess, device)
    from compact_taste_index import top_k
    top, scores = top_k(idx["embeddings"], query, k)
    sims = dict(zip(top.tolist(), scores.tolist()))
    fnums = [float(idx["f_number"][i]) for i in top]
    isos = [int(idx["iso"][i]) for i in top]
    focals = [float(idx["focal_mm"][i]) for i in top]
    # Weight each neighbor by its cosine similarity (clamped to >=0) so closer matches
    # dominate the summary; a uniform fallback keeps it defined if all sims are <=0.
    weights = [max(float(sims[i]), 0.0) for i in top]
    if sum(weights) <= 0.0:
        weights = [1.0] * len(top)
    summary = {
        "f_number": round(_weighted_median(fnums, weights), 1),
        "iso": int(round(_weighted_median(isos, weights))),
        "focal_mm": int(round(_weighted_median(focals, weights))),
    }
    print(f"nearest {k} excellent photos to {Path(image_path).name}:")
    print(f"  taste -> f/{summary['f_number']}  ISO {summary['iso']}  {summary['focal_mm']}mm")
    print(f"  (aperture range f/{min(fnums):.1f}-{max(fnums):.1f}, sim {sims[top[0]]:.2f}-{sims[top[-1]]:.2f})")
    for i in top[:5]:
        print(f"    - {idx['titles'][i]}  ISO{idx['iso'][i]} f/{idx['f_number'][i]} {idx['focal_mm'][i]}mm")
    return summary


if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else "build"
    arg = sys.argv[2] if len(sys.argv) > 2 else None
    if cmd == "build":
        build(int(arg) if arg else None)
    elif cmd == "recommend":
        recommend(arg)
    else:
        print(f"usage: {sys.argv[0]} build [n] | recommend <image>")
