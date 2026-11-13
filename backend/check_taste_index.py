"""Validate a CLIP taste index before it is trusted, transferred, or deployed.

The full audit verifies the immutable artifact against its manifest, then checks
the semantic invariants used by retrieval. The manifest-only mode is a fast
transfer check: byte size plus SHA-256, without allocating the large arrays.
"""

from __future__ import annotations

import argparse
import gc
import hashlib
import json
import os
import statistics
import time
from pathlib import Path
from typing import Any

import numpy as np


MANIFEST_SCHEMA_VERSION = 1
REQUIRED_ARRAYS = ("embeddings", "iso", "f_number", "shutter_s", "focal_mm", "titles")
NUMERIC_HARD_BOUNDS = {
    "iso": (1.0, 10_000_000.0),
    "f_number": (0.1, 128.0),
    "shutter_s": (1e-7, 86_400.0),
    # A zero focal length is legacy EXIF for "unknown". It is reported as a
    # quality warning, but is safe for retrieval and is not archive corruption.
    "focal_mm": (0.0, 10_000.0),
}
NORM_RTOL = 1e-3
NORM_ATOL = 1e-3


class IntegrityError(ValueError):
    """The artifact is unreadable or violates a deterministic contract."""

    def __init__(self, code: str, message: str, report: dict[str, Any] | None = None):
        super().__init__(message)
        self.code = code
        self.report = report or {}


def _fail(report: dict[str, Any], code: str, message: str) -> None:
    report["ok"] = False
    report["error"] = {"code": code, "message": message}
    raise IntegrityError(code, message, report)


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(8 * 1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _load_manifest(path: Path, report: dict[str, Any]) -> dict[str, Any]:
    try:
        manifest = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        _fail(report, "manifest_missing", f"manifest not found: {path}")
    except (OSError, json.JSONDecodeError) as exc:
        _fail(report, "manifest_unreadable", f"cannot read manifest {path}: {exc}")
    if not isinstance(manifest, dict):
        _fail(report, "manifest_schema", "manifest root must be a JSON object")
    if manifest.get("schema_version") != MANIFEST_SCHEMA_VERSION:
        _fail(
            report,
            "manifest_schema",
            f"manifest schema_version must be {MANIFEST_SCHEMA_VERSION}, "
            f"got {manifest.get('schema_version')!r}",
        )
    for key in ("artifact", "photos_embedded", "bytes", "sha256", "arrays"):
        if key not in manifest:
            _fail(report, "manifest_schema", f"manifest is missing required field: {key}")
    if not isinstance(manifest["arrays"], dict):
        _fail(report, "manifest_schema", "manifest arrays must be a JSON object")
    missing_specs = [key for key in REQUIRED_ARRAYS if key not in manifest["arrays"]]
    if missing_specs:
        _fail(report, "manifest_schema", f"manifest is missing array specs: {missing_specs}")
    return manifest


def _check_manifest_array(
    report: dict[str, Any],
    manifest: dict[str, Any] | None,
    key: str,
    array: np.ndarray,
) -> None:
    if manifest is None:
        return
    spec = manifest["arrays"][key]
    if not isinstance(spec, dict) or "shape" not in spec or "dtype" not in spec:
        _fail(report, "manifest_schema", f"manifest array spec for {key} needs shape and dtype")
    expected_shape = tuple(spec["shape"])
    if tuple(array.shape) != expected_shape:
        _fail(
            report,
            "array_shape_mismatch",
            f"{key} shape {tuple(array.shape)} does not match manifest {expected_shape}",
        )
    expected_dtype = np.dtype(spec["dtype"])
    if array.dtype != expected_dtype:
        _fail(
            report,
            "array_dtype_mismatch",
            f"{key} dtype {array.dtype} does not match manifest {expected_dtype}",
        )


def _retrieval_benchmark(embeddings: np.ndarray, runs: int = 7, k: int = 15) -> dict[str, Any]:
    """Measure the exact full-sort vector lookup currently used by recommend()."""
    query = embeddings[0].copy()
    samples_ms: list[float] = []
    top_index = -1
    for _ in range(runs):
        started = time.perf_counter()
        similarities = embeddings @ query
        top = np.argsort(-similarities)[:k]
        samples_ms.append((time.perf_counter() - started) * 1000.0)
        top_index = int(top[0])
    warm = samples_ms[1:] if len(samples_ms) > 1 else samples_ms
    return {
        "operation": "embeddings @ query plus full argsort",
        "rows": int(embeddings.shape[0]),
        "dimensions": int(embeddings.shape[1]),
        "k": k,
        "runs": runs,
        "samples_ms": [round(value, 3) for value in samples_ms],
        "warm_median_ms": round(statistics.median(warm), 3),
        "warm_min_ms": round(min(warm), 3),
        "warm_max_ms": round(max(warm), 3),
        "self_match_top_index": top_index,
        "scope": "retrieval only; excludes CLIP image encoding",
    }


def audit(
    path: Path,
    *,
    manifest_path: Path | None = None,
    full: bool = True,
    verify_sha256: bool = True,
    benchmark: bool = False,
) -> dict[str, Any]:
    """Return a machine-readable audit or raise IntegrityError with a partial report."""
    started = time.perf_counter()
    artifact = path.expanduser().resolve()
    report: dict[str, Any] = {
        "ok": False,
        "mode": "full" if full else "manifest_only",
        "artifact": {"path": str(artifact)},
        "manifest": None,
        "arrays": {},
        "warnings": [],
        "timings_seconds": {},
    }
    if not artifact.is_file():
        _fail(report, "artifact_missing", f"taste index not found: {artifact}")

    manifest: dict[str, Any] | None = None
    if manifest_path is not None:
        manifest_file = manifest_path.expanduser().resolve()
        manifest = _load_manifest(manifest_file, report)
        report["manifest"] = {
            "path": str(manifest_file),
            "schema_version": manifest["schema_version"],
            "declared_artifact": manifest["artifact"],
            "status": manifest.get("status"),
        }

    actual_bytes = artifact.stat().st_size
    report["artifact"]["bytes"] = actual_bytes
    if manifest is not None and actual_bytes != int(manifest["bytes"]):
        _fail(
            report,
            "byte_size_mismatch",
            f"artifact has {actual_bytes} bytes; manifest requires {manifest['bytes']}",
        )

    if verify_sha256:
        hash_started = time.perf_counter()
        actual_sha256 = _sha256(artifact)
        report["timings_seconds"]["sha256"] = round(time.perf_counter() - hash_started, 6)
        report["artifact"]["sha256"] = actual_sha256
        if manifest is not None and actual_sha256.lower() != str(manifest["sha256"]).lower():
            _fail(
                report,
                "sha256_mismatch",
                f"artifact SHA-256 {actual_sha256} does not match manifest {manifest['sha256']}",
            )
    else:
        report["artifact"]["sha256"] = None
        report["warnings"].append(
            {
                "code": "sha256_skipped",
                "message": "SHA-256 verification was explicitly skipped",
            }
        )

    if not full:
        report["ok"] = True
        report["timings_seconds"]["total"] = round(time.perf_counter() - started, 6)
        return report

    scan_started = time.perf_counter()
    focal_warning: dict[str, Any] | None = None
    try:
        with np.load(artifact, allow_pickle=False) as data:
            files = list(data.files)
            missing = [key for key in REQUIRED_ARRAYS if key not in files]
            if missing:
                _fail(report, "missing_arrays", f"archive is missing arrays: {missing}")
            if manifest is not None:
                expected_files = set(manifest["arrays"])
                unexpected = sorted(set(files) - expected_files)
                omitted = sorted(expected_files - set(files))
                if unexpected or omitted:
                    _fail(
                        report,
                        "archive_members_mismatch",
                        f"archive members differ from manifest; unexpected={unexpected}, missing={omitted}",
                    )

            array_started = time.perf_counter()
            embeddings = np.asarray(data["embeddings"])
            if embeddings.ndim != 2 or embeddings.shape[0] < 1 or embeddings.shape[1] < 1:
                _fail(
                    report,
                    "embeddings_shape",
                    f"embeddings must be a non-empty matrix, got {embeddings.shape}",
                )
            if embeddings.dtype != np.dtype("float32"):
                _fail(
                    report,
                    "embeddings_dtype",
                    f"embeddings must use float32, got {embeddings.dtype}",
                )
            _check_manifest_array(report, manifest, "embeddings", embeddings)
            rows, dimensions = embeddings.shape
            finite_embeddings = bool(np.isfinite(embeddings).all())
            if not finite_embeddings:
                _fail(report, "embeddings_nonfinite", "embeddings contain NaN or infinity")
            norms = np.linalg.norm(embeddings, axis=1)
            bad_norms = ~np.isclose(norms, 1.0, rtol=NORM_RTOL, atol=NORM_ATOL)
            if bad_norms.any():
                _fail(
                    report,
                    "embeddings_not_normalized",
                    f"{int(np.count_nonzero(bad_norms))} embeddings are not unit-normalized; "
                    f"range={float(norms.min()):.9f}..{float(norms.max()):.9f}",
                )
            report["arrays"]["embeddings"] = {
                "shape": [int(value) for value in embeddings.shape],
                "dtype": str(embeddings.dtype),
                "nbytes": int(embeddings.nbytes),
                "finite": finite_embeddings,
                "norm_min": float(norms.min()),
                "norm_max": float(norms.max()),
            }
            report["timings_seconds"]["embeddings_scan"] = round(
                time.perf_counter() - array_started, 6
            )
            if benchmark:
                report["benchmark"] = _retrieval_benchmark(embeddings)
                if report["benchmark"]["self_match_top_index"] != 0:
                    _fail(
                        report,
                        "retrieval_self_match",
                        "row zero did not retrieve itself as the nearest neighbor",
                    )
            del norms, embeddings
            gc.collect()

            for key in ("iso", "f_number", "shutter_s", "focal_mm"):
                array_started = time.perf_counter()
                values = np.asarray(data[key])
                if values.ndim != 1 or len(values) != rows:
                    _fail(
                        report,
                        "metadata_row_mismatch",
                        f"{key} must be a {rows}-row vector, got {values.shape}",
                    )
                if not np.issubdtype(values.dtype, np.number):
                    _fail(report, "metadata_dtype", f"{key} must be numeric, got {values.dtype}")
                _check_manifest_array(report, manifest, key, values)
                finite = bool(np.isfinite(values).all())
                if not finite:
                    _fail(report, "metadata_nonfinite", f"{key} contains NaN or infinity")
                minimum, maximum = NUMERIC_HARD_BOUNDS[key]
                outside = (values < minimum) | (values > maximum)
                if outside.any():
                    _fail(
                        report,
                        "metadata_out_of_bounds",
                        f"{key} has {int(np.count_nonzero(outside))} values outside "
                        f"{minimum}..{maximum}",
                    )
                report["arrays"][key] = {
                    "shape": [int(value) for value in values.shape],
                    "dtype": str(values.dtype),
                    "nbytes": int(values.nbytes),
                    "finite": finite,
                    "min": float(values.min()),
                    "max": float(values.max()),
                }
                if key == "focal_mm":
                    nonpositive = np.flatnonzero(values <= 0)
                    if nonpositive.size:
                        focal_warning = {
                            "code": "focal_mm_unknown",
                            "message": "zero focal length is retained legacy EXIF and treated as unknown",
                            "count": int(nonpositive.size),
                            "sample_indices": [int(value) for value in nonpositive[:5]],
                        }
                report["timings_seconds"][f"{key}_scan"] = round(
                    time.perf_counter() - array_started, 6
                )
                del values
                gc.collect()

            array_started = time.perf_counter()
            titles = np.asarray(data["titles"])
            if titles.ndim != 1 or len(titles) != rows:
                _fail(
                    report,
                    "metadata_row_mismatch",
                    f"titles must be a {rows}-row vector, got {titles.shape}",
                )
            if titles.dtype.kind not in ("U", "S"):
                _fail(report, "titles_dtype", f"titles must be fixed-width strings, got {titles.dtype}")
            _check_manifest_array(report, manifest, "titles", titles)
            seen: set[str] = set()
            empty_indices: list[int] = []
            duplicate_titles: list[str] = []
            for index, raw_title in enumerate(titles):
                title = str(raw_title).strip()
                if not title:
                    if len(empty_indices) < 5:
                        empty_indices.append(index)
                    continue
                if title in seen:
                    if len(duplicate_titles) < 5:
                        duplicate_titles.append(title)
                else:
                    seen.add(title)
            if empty_indices:
                _fail(
                    report,
                    "empty_titles",
                    f"titles contain empty values at sample indices {empty_indices}",
                )
            if duplicate_titles:
                _fail(
                    report,
                    "duplicate_titles",
                    f"titles contain duplicates; samples={duplicate_titles}",
                )
            if focal_warning is not None:
                focal_warning["sample_titles"] = [
                    str(titles[index]) for index in focal_warning["sample_indices"]
                ]
                report["warnings"].append(focal_warning)
            report["arrays"]["titles"] = {
                "shape": [int(value) for value in titles.shape],
                "dtype": str(titles.dtype),
                "nbytes": int(titles.nbytes),
                "empty": 0,
                "unique": len(seen),
            }
            report["timings_seconds"]["titles_scan"] = round(
                time.perf_counter() - array_started, 6
            )
            del titles, seen
            gc.collect()
    except IntegrityError:
        raise
    except Exception as exc:  # noqa: BLE001 - normalize corrupt/truncated archive failures
        _fail(report, "archive_unreadable", f"cannot read index: {exc}")

    if manifest is not None and rows != int(manifest["photos_embedded"]):
        _fail(
            report,
            "manifest_row_mismatch",
            f"archive has {rows} rows; manifest requires {manifest['photos_embedded']}",
        )

    report["rows"] = int(rows)
    report["dimensions"] = int(dimensions)
    if manifest is not None and manifest.get("rows_harvested"):
        harvested = int(manifest["rows_harvested"])
        report["embedding_yield"] = {
            "harvested_rows": harvested,
            "embedded_rows": int(rows),
            "skipped_rows": harvested - int(rows),
            "percent": round((rows / harvested) * 100.0, 6),
        }
    report["timings_seconds"]["semantic_scan"] = round(time.perf_counter() - scan_started, 6)
    report["timings_seconds"]["total"] = round(time.perf_counter() - started, 6)
    report["ok"] = True
    return report


def _resolve_artifact(
    explicit: Path | None,
    manifest_path: Path | None,
) -> Path:
    if explicit is not None:
        return explicit
    environment_path = os.environ.get("TASTE_INDEX_PATH")
    if environment_path:
        return Path(environment_path)
    if manifest_path is not None and manifest_path.is_file():
        try:
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            declared = Path(str(manifest["artifact"]))
            if declared.is_absolute():
                return declared
            return Path.cwd() / declared
        except (OSError, KeyError, json.JSONDecodeError, TypeError):
            pass
    return Path("data/taste_index.npz")


def _write_report(path: Path, report: dict[str, Any]) -> None:
    destination = path.expanduser()
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_name(destination.name + ".tmp")
    temporary.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    temporary.replace(destination)


def _human_summary(report: dict[str, Any]) -> str:
    artifact = report.get("artifact", {})
    if not report.get("ok"):
        error = report.get("error", {})
        return f"taste index FAIL [{error.get('code', 'unknown')}]: {error.get('message', '')}"
    parts = [
        "taste index PASS",
        f"mode={report['mode']}",
        f"bytes={artifact.get('bytes')}",
    ]
    if report.get("rows") is not None:
        parts.extend((f"rows={report['rows']}", f"dimensions={report['dimensions']}"))
    if artifact.get("sha256"):
        parts.append(f"sha256={artifact['sha256']}")
    parts.append(f"seconds={report['timings_seconds']['total']}")
    lines = [" ".join(parts)]
    for warning in report.get("warnings", []):
        lines.append(
            f"  warning [{warning['code']}]: {warning['message']}"
            + (f" (count={warning['count']})" if "count" in warning else "")
        )
    if report.get("benchmark"):
        lines.append(
            "  retrieval benchmark: "
            f"warm median {report['benchmark']['warm_median_ms']} ms "
            f"for k={report['benchmark']['k']}"
        )
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("path", nargs="?", type=Path, help="NPZ artifact path")
    manifest_group = parser.add_mutually_exclusive_group()
    manifest_group.add_argument(
        "--manifest",
        type=Path,
        default=Path("data/taste_index.MANIFEST.json"),
        help="manifest to enforce (default: data/taste_index.MANIFEST.json)",
    )
    manifest_group.add_argument(
        "--no-manifest",
        action="store_true",
        help="audit semantic invariants without enforcing a release manifest",
    )
    parser.add_argument(
        "--manifest-only",
        action="store_true",
        help="verify bytes and SHA-256 only; do not load arrays",
    )
    parser.add_argument(
        "--skip-sha256",
        action="store_true",
        help="skip artifact hashing (not suitable for release or transfer verification)",
    )
    parser.add_argument(
        "--benchmark",
        action="store_true",
        help="benchmark the current full-sort retrieval during a full audit",
    )
    parser.add_argument("--json", action="store_true", help="print only machine-readable JSON")
    parser.add_argument("--output", type=Path, help="atomically write the JSON report to this path")
    args = parser.parse_args(argv)
    if args.manifest_only and args.benchmark:
        parser.error("--benchmark requires a full audit")

    manifest_path = None if args.no_manifest else args.manifest
    artifact = _resolve_artifact(args.path, manifest_path)
    try:
        report = audit(
            artifact,
            manifest_path=manifest_path,
            full=not args.manifest_only,
            verify_sha256=not args.skip_sha256,
            benchmark=args.benchmark,
        )
        exit_code = 0
    except IntegrityError as exc:
        report = exc.report or {
            "ok": False,
            "artifact": {"path": str(artifact)},
            "error": {"code": exc.code, "message": str(exc)},
        }
        exit_code = 1

    if args.output:
        _write_report(args.output, report)
    if args.json:
        print(json.dumps(report, sort_keys=True))
    else:
        print(_human_summary(report))
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
