"""Camera-free tests for locating local Camera Remote Command packages."""

from __future__ import annotations

import json
import sys
import tempfile
import zipfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "tools"))

import find_camera_remote_command_package


def test_package_locator_records_metadata_without_copying_package() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        package = root / "CameraRemoteCommand-package.zip"
        with zipfile.ZipFile(package, "w") as archive:
            archive.writestr("README.txt", "Sony Camera Remote Command package for ILCE-7M2.\n")
        report, report_path = find_camera_remote_command_package.build_report(
            candidates=[package],
            search_dirs=[],
            out_dir=root / "out",
        )

        assert report_path.exists()
        assert report["policy"]["downloads_package"] is False
        assert report["policy"]["copies_package"] is False
        assert report["official_sources"]
        assert report["summary"]["reviewable_candidates"] == 1
        assert report["summary"]["result"] == "found_reviewable_package"
        assert report["summary"]["best_candidate"] == str(package.resolve())
        assert "review_camera_remote_command_package.py" in report["summary"]["next_review_command"]
        candidate = report["candidates"][0]
        assert candidate["sha256"]
        assert candidate["reviewable"] is True
        assert candidate["kind"] == "file"
        assert candidate["candidate_status"] == "reviewable"
        assert candidate["content_inspection"]["ilce_7m2_marker"] is True
        canonical = json.loads(
            (
                root
                / "out"
                / find_camera_remote_command_package.CANONICAL_SUMMARY_NAME
            ).read_text(encoding="utf-8")
        )
        assert canonical["summary"]["reviewable_candidates"] == 1
        assert canonical["summary"]["best_candidate_present"] is True
        assert canonical["summary"]["review_command_available"] is True
        serialized = json.dumps(canonical)
        assert str(root) not in serialized
        assert "best_candidate" not in canonical["summary"]
        assert "next_review_command" not in canonical["summary"]
    print("  Camera Remote Command package locator metadata: ok")


def test_package_locator_handles_missing_explicit_candidate() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        missing = root / "CameraRemoteCommand-package.zip"
        report, _ = find_camera_remote_command_package.build_report(
            candidates=[missing],
            search_dirs=[],
            out_dir=root / "out",
        )

    assert report["summary"]["reviewable_candidates"] == 0
    assert report["summary"]["result"] == "official_package_not_found_locally"
    assert report["summary"]["next_review_command"] is None
    assert report["candidates"][0]["exists"] is False
    print("  Camera Remote Command package locator missing path: ok")


def test_package_locator_does_not_treat_repo_scripts_as_packages() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        script = root / "review_camera_remote_command_package.py"
        script.write_text("print('not a Sony package')\n", encoding="utf-8")
        report, _ = find_camera_remote_command_package.build_report(
            candidates=[script],
            search_dirs=[],
            out_dir=root / "out",
        )

    assert report["summary"]["reviewable_candidates"] == 0
    assert report["summary"]["next_review_command"] is None
    candidate = report["candidates"][0]
    assert candidate["exists"] is True
    assert candidate["name_matches"] is True
    assert candidate["plausible_package"] is False
    assert candidate["reviewable"] is False
    print("  Camera Remote Command package locator script false-positive guard: ok")


def test_package_locator_does_not_treat_sdk_archives_as_command_packages() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        downloads = root / "Downloads"
        downloads.mkdir()
        sdk = downloads / "CRSDK_v2.02.00.zip"
        sdk.write_bytes(b"fake camera remote sdk package")
        report, _ = find_camera_remote_command_package.build_report(
            search_dirs=[downloads],
            out_dir=root / "out",
        )

    assert report["summary"]["candidates"] == 1
    assert report["summary"]["reviewable_candidates"] == 0
    assert report["summary"]["next_review_command"] is None
    assert report["candidates"][0]["candidate_status"] == "camera_remote_sdk_not_command"
    print("  Camera Remote Command package locator SDK false-positive guard: ok")


def test_package_locator_rejects_explicit_sdk_archive() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        sdk = root / "CRSDK_v2.02.00.zip"
        sdk.write_bytes(b"fake camera remote sdk package")
        report, _ = find_camera_remote_command_package.build_report(
            candidates=[sdk],
            search_dirs=[],
            out_dir=root / "out",
        )

    assert report["summary"]["candidates"] == 1
    assert report["summary"]["reviewable_candidates"] == 0
    assert report["summary"]["next_review_command"] is None
    candidate = report["candidates"][0]
    assert candidate["exists"] is True
    assert candidate["plausible_package"] is True
    assert candidate["sdk_name_matches"] is True
    assert candidate["excluded_reason"] == "camera_remote_sdk_not_command"
    assert candidate["reviewable"] is False
    assert candidate["review_command"] is None
    print("  Camera Remote Command package locator explicit SDK guard: ok")


def test_package_locator_deduplicates_search_dirs_in_report() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        downloads = root / "Downloads"
        downloads.mkdir()
        report, _ = find_camera_remote_command_package.build_report(
            search_dirs=[downloads, downloads],
            out_dir=root / "out",
        )

    assert report["search"]["search_dirs"] == [str(downloads)]
    assert report["summary"]["candidates"] == 0
    print("  Camera Remote Command package locator search-dir dedupe: ok")


def test_package_locator_ignores_own_artifact_tree() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        artifact_dir = root / "artifacts" / "falsification" / "r4_camera_remote_command"
        artifact_dir.mkdir(parents=True)
        readiness = root / "artifacts" / "falsification" / "a7ii_camera_remote_command_readiness.md"
        readiness.write_text("camera remote command readiness, not a Sony package\n", encoding="utf-8")
        report, _ = find_camera_remote_command_package.build_report(
            search_dirs=[root],
            out_dir=root / "out",
        )

    assert report["summary"]["candidates"] == 0
    assert report["summary"]["reviewable_candidates"] == 0
    assert report["summary"]["next_review_command"] is None
    print("  Camera Remote Command package locator artifact-tree guard: ok")


def test_package_locator_common_search_dirs_are_opt_in_and_recorded() -> None:
    common = [str(path) for path in find_camera_remote_command_package._common_search_dirs()]
    assert str(Path.home() / "SynologyDrive") in common
    assert str(Path.home() / "Development") in common

    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        report, _ = find_camera_remote_command_package.build_report(
            search_dirs=[root, root / "missing"],
            out_dir=root / "out",
        )

    assert str(root) in report["search"]["existing_search_dirs"]
    assert str(root / "missing") in report["search"]["missing_search_dirs"]
    assert report["search"]["complete_with_policy_exclusions"] is False
    print("  Camera Remote Command package locator common-search metadata: ok")


def test_package_locator_finds_renamed_zip_by_internal_markers() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        downloads = root / "Downloads"
        downloads.mkdir()
        package = downloads / "download-8f64d3.zip"
        with zipfile.ZipFile(package, "w") as archive:
            archive.writestr(
                "docs/README.txt",
                "Sony Camera Remote Command Ver. 2.02.00\nSupported device: ILCE-7M2\n",
            )
        report, _ = find_camera_remote_command_package.build_report(
            search_dirs=[downloads],
            max_depth=None,
            out_dir=root / "out",
        )

    assert report["summary"]["result"] == "found_reviewable_package"
    assert report["search"]["complete_with_policy_exclusions"] is True
    candidate = report["candidates"][0]
    assert candidate["name_matches"] is False
    assert candidate["command_identity_signal"] is True
    assert candidate["content_inspection"]["inspection_kind"] == "zip_manifest_and_bounded_text"
    assert candidate["reviewable"] is True
    print("  Camera Remote Command package locator renamed ZIP inspection: ok")


def test_package_locator_rejects_invalid_matching_zip() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        package = root / "CameraRemoteCommand-package.zip"
        package.write_bytes(b"not a zip archive")
        report, _ = find_camera_remote_command_package.build_report(
            candidates=[package],
            search_dirs=[],
            out_dir=root / "out",
        )

    candidate = report["candidates"][0]
    assert candidate["candidate_status"] == "invalid_zip"
    assert candidate["reviewable"] is False
    assert report["summary"]["result"] == "official_package_not_found_locally"
    print("  Camera Remote Command package locator invalid ZIP guard: ok")


def test_package_locator_reports_depth_limited_search_as_incomplete() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        nested = root / "one" / "two"
        nested.mkdir(parents=True)
        report, _ = find_camera_remote_command_package.build_report(
            search_dirs=[root],
            max_depth=0,
            out_dir=root / "out",
        )

    assert report["search"]["complete_with_policy_exclusions"] is False
    assert report["summary"]["result"] == "package_not_found_scan_incomplete"
    assert report["search"]["coverage"][0]["depth_pruned_dirs"] >= 1
    print("  Camera Remote Command package locator incomplete-search truth: ok")


def test_package_locator_does_not_promote_own_project_documentation() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp) / "camera-autopilot"
        docs = root / "docs" / "falsification"
        docs.mkdir(parents=True)
        document = docs / "camera_remote_command_package_review.md"
        document.write_text(
            "Sony Camera Remote Command Ver. 2.02.00 lists ILCE-7M2, but the official package is missing.\n",
            encoding="utf-8",
        )
        report, _ = find_camera_remote_command_package.build_report(
            search_dirs=[root],
            max_depth=None,
            out_dir=Path(tmp) / "out",
        )

    assert report["summary"]["reviewable_candidates"] == 0
    assert report["summary"]["manual_reference_candidates"] == 0
    candidate = report["candidates"][0]
    assert candidate["candidate_status"] == "local_project_material_not_official_package"
    assert candidate["local_project_material"] is True
    print("  Camera Remote Command package locator project-doc guard: ok")


if __name__ == "__main__":
    test_package_locator_records_metadata_without_copying_package()
    test_package_locator_handles_missing_explicit_candidate()
    test_package_locator_does_not_treat_repo_scripts_as_packages()
    test_package_locator_does_not_treat_sdk_archives_as_command_packages()
    test_package_locator_rejects_explicit_sdk_archive()
    test_package_locator_deduplicates_search_dirs_in_report()
    test_package_locator_ignores_own_artifact_tree()
    test_package_locator_common_search_dirs_are_opt_in_and_recorded()
    test_package_locator_finds_renamed_zip_by_internal_markers()
    test_package_locator_rejects_invalid_matching_zip()
    test_package_locator_reports_depth_limited_search_as_incomplete()
    test_package_locator_does_not_promote_own_project_documentation()
    print("camera_remote_command_package_locator tests: PASS")
