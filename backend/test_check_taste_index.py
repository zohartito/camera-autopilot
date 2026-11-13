"""Hardware-free contract tests for check_taste_index.py."""

from __future__ import annotations

import hashlib
import json
import subprocess
import sys
import tempfile
from pathlib import Path

import numpy as np

import check_taste_index


SCRIPT = Path(__file__).with_name("check_taste_index.py")


def _arrays(
    *,
    duplicate: bool = False,
    nonfinite: bool = False,
    normalized: bool = True,
    focal_zero: bool = False,
) -> dict[str, np.ndarray]:
    embeddings = np.array([[1.0, 0.0], [0.0, 1.0]], dtype="float32")
    if nonfinite:
        embeddings[1, 0] = np.nan
    if not normalized:
        embeddings[1] = [2.0, 0.0]
    return {
        "embeddings": embeddings,
        "iso": np.array([100, 400], dtype="int64"),
        "f_number": np.array([2.8, 8.0], dtype="float64"),
        "shutter_s": np.array([1 / 125, 1 / 30], dtype="float64"),
        "focal_mm": np.array([35.0, 0.0 if focal_zero else 85.0], dtype="float64"),
        "titles": np.array(["File:A.jpg", "File:A.jpg" if duplicate else "File:B.jpg"]),
    }


def _write_index(path: Path, arrays: dict[str, np.ndarray] | None = None) -> dict[str, np.ndarray]:
    values = arrays or _arrays()
    np.savez(path, **values)
    return values


def _write_manifest(
    path: Path,
    artifact: Path,
    arrays: dict[str, np.ndarray],
    **overrides,
) -> Path:
    manifest = {
        "schema_version": 1,
        "artifact": "data/taste_index.npz",
        "status": "test",
        "photos_embedded": len(arrays["embeddings"]),
        "rows_harvested": len(arrays["embeddings"]),
        "bytes": artifact.stat().st_size,
        "sha256": hashlib.sha256(artifact.read_bytes()).hexdigest(),
        "arrays": {
            key: {"shape": list(value.shape), "dtype": str(value.dtype)}
            for key, value in arrays.items()
        },
    }
    manifest.update(overrides)
    path.write_text(json.dumps(manifest), encoding="utf-8")
    return path


def _expect_failure(code: str, function) -> check_taste_index.IntegrityError:
    try:
        function()
    except check_taste_index.IntegrityError as exc:
        assert exc.code == code, (exc.code, str(exc))
        assert exc.report["ok"] is False
        assert exc.report["error"]["code"] == code
        return exc
    raise AssertionError(f"expected integrity failure: {code}")


def test_valid_index_matches_manifest_and_reports_known_quality_warning() -> None:
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        artifact = root / "taste_index.npz"
        arrays = _write_index(artifact, _arrays(focal_zero=True))
        manifest = _write_manifest(root / "manifest.json", artifact, arrays)
        result = check_taste_index.audit(artifact, manifest_path=manifest)
    assert result["ok"] is True
    assert result["rows"] == 2 and result["dimensions"] == 2
    assert result["arrays"]["embeddings"]["dtype"] == "float32"
    assert result["warnings"][0]["code"] == "focal_mm_unknown"
    assert result["warnings"][0]["sample_titles"] == ["File:B.jpg"]


def test_manifest_only_verifies_identity_without_loading_numpy_arrays() -> None:
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        artifact = root / "taste_index.npz"
        arrays = _write_index(artifact)
        manifest = _write_manifest(root / "manifest.json", artifact, arrays)
        original_load = check_taste_index.np.load
        check_taste_index.np.load = lambda *args, **kwargs: (_ for _ in ()).throw(
            AssertionError("np.load must not run in manifest-only mode")
        )
        try:
            result = check_taste_index.audit(artifact, manifest_path=manifest, full=False)
        finally:
            check_taste_index.np.load = original_load
    assert result["ok"] is True
    assert result["mode"] == "manifest_only"
    assert result["arrays"] == {}


def test_manifest_sha256_mismatch_fails() -> None:
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        artifact = root / "taste_index.npz"
        arrays = _write_index(artifact)
        manifest = _write_manifest(root / "manifest.json", artifact, arrays, sha256="0" * 64)
        _expect_failure(
            "sha256_mismatch",
            lambda: check_taste_index.audit(artifact, manifest_path=manifest),
        )


def test_manifest_byte_size_mismatch_fails_before_hashing() -> None:
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        artifact = root / "taste_index.npz"
        arrays = _write_index(artifact)
        manifest = _write_manifest(root / "manifest.json", artifact, arrays, bytes=1)
        _expect_failure(
            "byte_size_mismatch",
            lambda: check_taste_index.audit(artifact, manifest_path=manifest),
        )


def test_manifest_schema_is_versioned() -> None:
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        artifact = root / "taste_index.npz"
        arrays = _write_index(artifact)
        manifest = _write_manifest(root / "manifest.json", artifact, arrays, schema_version=99)
        _expect_failure(
            "manifest_schema",
            lambda: check_taste_index.audit(artifact, manifest_path=manifest),
        )


def test_missing_array_fails() -> None:
    with tempfile.TemporaryDirectory() as directory:
        artifact = Path(directory) / "missing.npz"
        arrays = _arrays()
        arrays.pop("focal_mm")
        _write_index(artifact, arrays)
        _expect_failure(
            "missing_arrays",
            lambda: check_taste_index.audit(artifact, manifest_path=None),
        )


def test_metadata_row_mismatch_fails() -> None:
    with tempfile.TemporaryDirectory() as directory:
        artifact = Path(directory) / "rows.npz"
        arrays = _arrays()
        arrays["iso"] = np.array([100], dtype="int64")
        _write_index(artifact, arrays)
        _expect_failure(
            "metadata_row_mismatch",
            lambda: check_taste_index.audit(artifact, manifest_path=None),
        )


def test_nonfinite_embeddings_fail() -> None:
    with tempfile.TemporaryDirectory() as directory:
        artifact = Path(directory) / "nonfinite.npz"
        _write_index(artifact, _arrays(nonfinite=True))
        _expect_failure(
            "embeddings_nonfinite",
            lambda: check_taste_index.audit(artifact, manifest_path=None),
        )


def test_non_normalized_embeddings_fail() -> None:
    with tempfile.TemporaryDirectory() as directory:
        artifact = Path(directory) / "norm.npz"
        _write_index(artifact, _arrays(normalized=False))
        _expect_failure(
            "embeddings_not_normalized",
            lambda: check_taste_index.audit(artifact, manifest_path=None),
        )


def test_duplicate_titles_fail() -> None:
    with tempfile.TemporaryDirectory() as directory:
        artifact = Path(directory) / "duplicate.npz"
        _write_index(artifact, _arrays(duplicate=True))
        _expect_failure(
            "duplicate_titles",
            lambda: check_taste_index.audit(artifact, manifest_path=None),
        )


def test_numeric_hard_bounds_fail() -> None:
    with tempfile.TemporaryDirectory() as directory:
        artifact = Path(directory) / "bounds.npz"
        arrays = _arrays()
        arrays["iso"][1] = 0
        _write_index(artifact, arrays)
        _expect_failure(
            "metadata_out_of_bounds",
            lambda: check_taste_index.audit(artifact, manifest_path=None),
        )


def test_cli_json_report_and_exit_code() -> None:
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        artifact = root / "taste_index.npz"
        arrays = _write_index(artifact)
        manifest = _write_manifest(root / "manifest.json", artifact, arrays)
        report_path = root / "reports" / "audit.json"
        result = subprocess.run(
            [
                sys.executable,
                str(SCRIPT),
                str(artifact),
                "--manifest",
                str(manifest),
                "--json",
                "--output",
                str(report_path),
            ],
            text=True,
            capture_output=True,
        )
        stdout = json.loads(result.stdout)
        saved = json.loads(report_path.read_text(encoding="utf-8"))
    assert result.returncode == 0, result.stderr
    assert stdout["ok"] is True
    assert saved["artifact"]["sha256"] == stdout["artifact"]["sha256"]


def _main() -> int:
    tests = (
        test_valid_index_matches_manifest_and_reports_known_quality_warning,
        test_manifest_only_verifies_identity_without_loading_numpy_arrays,
        test_manifest_sha256_mismatch_fails,
        test_manifest_byte_size_mismatch_fails_before_hashing,
        test_manifest_schema_is_versioned,
        test_missing_array_fails,
        test_metadata_row_mismatch_fails,
        test_nonfinite_embeddings_fail,
        test_non_normalized_embeddings_fail,
        test_duplicate_titles_fail,
        test_numeric_hard_bounds_fail,
        test_cli_json_report_and_exit_code,
    )
    failures = 0
    for test in tests:
        try:
            test()
            print(f"  ok   {test.__name__}")
        except Exception as exc:  # noqa: BLE001 - standalone test runner
            failures += 1
            print(f"  FAIL {test.__name__}: {exc}")
    print(f"{len(tests) - failures}/{len(tests)} taste-index integrity tests passed")
    return failures


if __name__ == "__main__":
    raise SystemExit(_main())
