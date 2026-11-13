"""Lossless, immutable taste corpus with bounded top-K and mapped metadata.

Preparation is explicit and offline. Runtime verifies every member against the
manifest, then maps the original NPY bytes read-only; no quantization or subset.
"""
from __future__ import annotations

import hashlib
import json
import mmap
import os
from pathlib import Path
import shutil
import tempfile
import zipfile

import numpy as np

KEYS = ("embeddings", "iso", "f_number", "shutter_s", "focal_mm", "titles")
SCHEMA_VERSION = 1
BLOCK_ROWS = 4096


def sha256(path: Path) -> str:
    with path.open("rb") as handle:
        return hashlib.file_digest(handle, "sha256").hexdigest()


def _validate_arrays(arrays: dict) -> tuple[int, int]:
    vectors = arrays["embeddings"]
    if vectors.ndim != 2 or vectors.dtype != np.dtype("float32"):
        raise ValueError("embeddings must be a float32 matrix")
    rows, dimensions = vectors.shape
    if not rows or not dimensions:
        raise ValueError("empty embedding matrix")
    for key in KEYS[1:]:
        if arrays[key].shape != (rows,):
            raise ValueError(f"metadata length mismatch: {key}")
        if key == "titles":
            if arrays[key].dtype.kind not in "US":
                raise ValueError("titles must be a fixed-width string array")
        elif arrays[key].dtype.kind not in "iuf":
            raise ValueError(f"metadata must be numeric: {key}")
    return rows, dimensions


def prepare(source: Path, destination: Path, expected_sha256: str | None = None) -> dict:
    """Extract exact array bytes into a new directory; never replace an artifact."""
    source, destination = Path(source), Path(destination)
    if destination.exists() or destination.is_symlink():
        raise FileExistsError(f"compact destination already exists: {destination}")
    source_digest = sha256(source)
    if expected_sha256 is not None and source_digest != expected_sha256:
        raise ValueError("source archive does not match its pinned SHA-256")
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = Path(tempfile.mkdtemp(prefix=".taste-compact-", dir=destination.parent))
    try:
        with zipfile.ZipFile(source) as archive:
            names = archive.namelist()
            for key in KEYS:
                member = key + ".npy"
                if names.count(member) != 1:
                    raise ValueError(f"missing or duplicate array: {member}")
                # Member names are constants, never archive-selected paths.
                with archive.open(member) as reader, (temporary / member).open("xb") as writer:
                    shutil.copyfileobj(reader, writer, length=1024 * 1024)
                    writer.flush()
                    os.fsync(writer.fileno())
        arrays = {key: np.load(temporary / (key + ".npy"), mmap_mode="r", allow_pickle=False) for key in KEYS}
        rows, dimensions = _validate_arrays(arrays)
        for key in KEYS[:-1]:
            for start in range(0, rows, BLOCK_ROWS):
                if not np.isfinite(arrays[key][start:start + BLOCK_ROWS]).all():
                    raise ValueError(f"non-finite corpus value: {key}")
            _release_pages(arrays[key])
        files = {key + ".npy": {"bytes": (temporary / (key + ".npy")).stat().st_size,
                                 "sha256": sha256(temporary / (key + ".npy")),
                                 "shape": list(arrays[key].shape), "dtype": arrays[key].dtype.str}
                 for key in KEYS}
        manifest = {"schema_version": SCHEMA_VERSION, "format": "lossless-float32-npy-mmap",
                    "rows": rows, "dimensions": dimensions, "quantization": "none",
                    "source": {"bytes": source.stat().st_size, "sha256": source_digest}, "files": files}
        # Refuse a source that changed while extraction was in progress.
        if sha256(source) != source_digest:
            raise ValueError("source archive changed during conversion")
        (temporary / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n")
        for path in temporary.iterdir():
            path.chmod(0o444)
        for array in arrays.values():
            array._mmap.close()
        temporary.chmod(0o555)
        temporary.rename(destination)
        return manifest
    except BaseException:
        temporary.chmod(0o700)
        shutil.rmtree(temporary)
        raise


def load(directory: Path, expected_source_sha256: str | None = None) -> dict:
    """Validate immutable files and map; corrupt compact artifacts fail closed."""
    from runtime_resources import _file_receipt
    directory = Path(directory)
    if directory.is_symlink() or directory.stat().st_mode & 0o222:
        raise ValueError("compact directory must be an immutable regular directory")
    manifest_path = directory / "manifest.json"
    if manifest_path.is_symlink() or manifest_path.stat().st_mode & 0o222:
        raise ValueError("compact manifest must be immutable")
    if manifest_path.stat().st_size > 64 * 1024:
        raise ValueError("compact manifest exceeds bound")
    manifest = json.loads(manifest_path.read_text())
    if manifest.get("schema_version") != SCHEMA_VERSION or manifest.get("format") != "lossless-float32-npy-mmap":
        raise ValueError("unsupported compact corpus format")
    if expected_source_sha256 is not None and manifest["source"]["sha256"] != expected_source_sha256:
        raise ValueError("compact corpus belongs to a different source archive")
    arrays = {}
    try:
        for key in KEYS:
            name = key + ".npy"
            path = directory / name
            if path.is_symlink() or path.stat().st_mode & 0o222:
                raise ValueError(f"compact array must be immutable: {name}")
            meta = manifest["files"][name]
            if _file_receipt(path, meta["bytes"], meta["sha256"])["state"] != "verified":
                raise ValueError(f"compact array digest failed: {name}")
            arrays[key] = np.load(path, mmap_mode="r", allow_pickle=False)
            if list(arrays[key].shape) != meta["shape"] or arrays[key].dtype.str != meta["dtype"]:
                raise ValueError(f"compact array header mismatch: {name}")
        rows, dimensions = _validate_arrays(arrays)
        if (rows, dimensions) != (manifest["rows"], manifest["dimensions"]):
            raise ValueError("compact corpus dimensions mismatch")
        return arrays
    except BaseException:
        for array in arrays.values():
            array._mmap.close()
        raise


def _release_pages(array: np.ndarray) -> None:
    """Allow clean corpus pages to leave process RSS after a bounded scan.

    macOS/Linux support madvise; platforms without it retain normal OS-managed
    mappings. This never changes backing bytes or releases query/model memory.
    """
    mapped = getattr(array, "_mmap", None)
    if mapped is not None and hasattr(mapped, "madvise") and hasattr(mmap, "MADV_DONTNEED"):
        try:
            mapped.madvise(mmap.MADV_DONTNEED)
        except (OSError, ValueError):
            pass


def top_k(vectors: np.ndarray, query: np.ndarray, k: int, block_rows: int = BLOCK_ROWS) -> tuple[np.ndarray, np.ndarray]:
    """Return row IDs and cosine scores; scratch storage O(block_rows + k).

    All original rows are scored. Ties resolve by row ID, making the result
    deterministic across chunk sizes; no full-corpus score or argsort array.
    """
    if k < 1 or block_rows < 1:
        raise ValueError("k and block_rows must be positive")
    if query.shape != (vectors.shape[1],) or not np.isfinite(query).all():
        raise ValueError("invalid query vector")
    k = min(k, len(vectors))
    if not k:
        raise ValueError("empty corpus")
    best_ids = np.empty(0, dtype=np.int64)
    best_scores = np.empty(0, dtype=np.float32)
    for start in range(0, len(vectors), block_rows):
        scores = np.asarray(vectors[start:start + block_rows] @ query)
        if not np.isfinite(scores).all():
            raise ValueError("non-finite corpus score")
        count = min(k, len(scores))
        # Partition the block; include boundary ties to select canonical row IDs.
        threshold = np.partition(scores, len(scores) - count)[len(scores) - count]
        candidates = np.flatnonzero(scores >= threshold)
        chosen = candidates[np.lexsort((candidates, -scores[candidates]))[:count]]
        ids = np.concatenate((best_ids, chosen + start))
        values = np.concatenate((best_scores, scores[chosen]))
        order = np.lexsort((ids, -values))[:k]
        best_ids, best_scores = ids[order], values[order]
        _release_pages(vectors)
    return best_ids, best_scores
