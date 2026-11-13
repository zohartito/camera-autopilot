"""Camera-free tests for the A7 II photo-source inventory."""

from __future__ import annotations

import sys
from pathlib import Path
from tempfile import TemporaryDirectory

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "backend"))

import settings_photo_inventory
import settings_registry


def test_photo_inventory_requires_explicit_untracked_source_folder() -> None:
    registry = settings_registry.load_registry()
    default_report = settings_photo_inventory.build_report(registry=registry)
    assert default_report["summary"]["status"] == "source_dir_missing"
    assert default_report["summary"]["non_registry_sources"] == 0

    with TemporaryDirectory() as tmp:
        source = Path(tmp)
        for filename in settings_photo_inventory.registry_photo_source_index(registry):
            (source / filename).write_bytes(b"stub")
        report = settings_photo_inventory.build_report(registry=registry, source_dir=source)

    summary = report["summary"]
    assert summary["coverage_complete"] is True, summary
    assert summary["source_files"] == summary["accounted_source_files"]
    assert summary["registry_photo_sources"] >= 40
    print("  photo inventory requires an explicit untracked source folder: ok")


def test_photo_inventory_reports_unaccounted_temp_file() -> None:
    registry = settings_registry.load_registry()
    with TemporaryDirectory() as tmp:
        source = Path(tmp)
        for filename in settings_photo_inventory.registry_photo_source_index(registry):
            (source / filename).write_bytes(b"stub")
        for item in settings_photo_inventory.load_inventory()["non_registry_sources"]:
            (source / item["filename"]).write_bytes(b"stub")
        (source / "IMG_9999.jpg").write_bytes(b"new photo")

        report = settings_photo_inventory.build_report(registry=registry, source_dir=source)

    assert report["summary"]["coverage_complete"] is False
    assert report["summary"]["unaccounted_source_files"] == ["IMG_9999.jpg"]
    print("  photo inventory detects unaccounted photos: ok")


def test_photo_inventory_markdown_does_not_embed_private_context() -> None:
    report = settings_photo_inventory.build_report()
    markdown = settings_photo_inventory.to_markdown(report, limit_records=0)

    assert "# A7 II Photo Inventory" in markdown
    assert "UNTRACKED_LOCAL_CAMERA_PHOTO_DIR" in markdown
    assert "IMG_5811" not in markdown
    assert "pairing" not in markdown.lower()
    print("  photo inventory markdown: ok")


def test_photo_inventory_api_projection_redacts_names_and_paths() -> None:
    report = settings_photo_inventory.build_report(redact_identifiers=True)
    assert report["summary"]["source_dir"] is None
    assert report["records"]
    assert all("filename" not in record and len(record["source_id"]) == 16 for record in report["records"])
    for key in ("unaccounted_source_files", "missing_registry_photo_files", "missing_non_registry_files", "inventory_overlaps_registry"):
        assert key not in report["summary"]
        assert f"{key}_count" in report["summary"]
    print("  photo inventory API projection redacts identifiers: ok")


if __name__ == "__main__":
    test_photo_inventory_requires_explicit_untracked_source_folder()
    test_photo_inventory_reports_unaccounted_temp_file()
    test_photo_inventory_markdown_does_not_embed_private_context()
    test_photo_inventory_api_projection_redacts_names_and_paths()
    print("settings_photo_inventory tests: PASS")
