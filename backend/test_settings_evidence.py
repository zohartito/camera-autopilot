"""Camera-free tests for setting evidence aggregation."""

from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "backend"))

import settings_evidence


def _write_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")


def test_evidence_aggregator_maps_latest_reviews_to_settings() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        smart_dir = root / "smart"
        usb_dir = root / "usb"
        _write_json(
            smart_dir / "20260101-smart-remote-review.json",
            {
                "generated_at": "20260101",
                "artifact_summary": {"discovered_method_count": 2},
                "registry_mapping_summary": {"smart_remote_rows": 1},
                "registry_mapping": [
                    {
                        "setting_id": "camera.iso",
                        "label": "ISO",
                        "registry_status": "proven_set",
                        "route_status": "proven_set",
                        "verdict": "route_setter_seen_in_artifact",
                        "matched_setters": ["setIsoSpeedRate"],
                        "matched_getters": ["getAvailableIsoSpeedRate"],
                        "evidence_count": 2,
                        "next_action": "Keep as regression row.",
                    }
                ],
            },
        )
        _write_json(
            usb_dir / "20260101-usb-gphoto-review.json",
            {
                "generated_at": "20260101",
                "artifact_summary": {"json_artifacts": 1},
                "registry_mapping_summary": {"usb_rows": 1},
                "registry_mapping": [
                    {
                        "setting_id": "camera.iso",
                        "label": "ISO",
                        "registry_status": "proven_set",
                        "route_status": "proven_set",
                        "verdict": "proven_set_read_revert",
                        "node": "iso",
                        "evidence_count": 1,
                        "next_action": "Keep as regression row.",
                    }
                ],
            },
        )

        sources = (
            settings_evidence.ReviewSource(
                route="smart_remote_wifi",
                label="fake smart",
                directory=smart_dir,
                patterns=("*smart-remote-review.json",),
            ),
            settings_evidence.ReviewSource(
                route="usb_gphoto",
                label="fake usb",
                directory=usb_dir,
                patterns=("*usb-gphoto-review.json",),
            ),
        )
        index = settings_evidence.load_review_index(sources=sources)
        item = settings_evidence.evidence_for_setting("camera.iso", review_index=index)

    assert item["setting_id"] == "camera.iso"
    assert {review["route"] for review in item["reviews"]} == {"smart_remote_wifi", "usb_gphoto"}
    assert any(review["matched_setters"] == ["setIsoSpeedRate"] for review in item["reviews"])
    assert any(review.get("node") == "iso" for review in item["reviews"])
    assert item["missing_review_routes"] == []
    assert index["by_route_verdict"]["usb_gphoto"]["proven_set_read_revert"] == 1
    print("  settings evidence aggregation: ok")


def test_evidence_aggregator_prefers_canonical_review_summary() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        review_dir = Path(tmp)
        _write_json(
            review_dir / "20991231-usb-gphoto-review.json",
            {
                "generated_at": "20991231",
                "registry_mapping": [
                    {
                        "setting_id": "camera.iso",
                        "verdict": "stale_private_detail",
                    }
                ],
            },
        )
        _write_json(
            review_dir / settings_evidence.CANONICAL_REVIEW_NAME,
            {
                "generated_at": "20260727",
                "artifact_summary": {"json_artifacts": 1},
                "registry_mapping_summary": {"usb_rows": 1},
                "registry_mapping": [
                    {
                        "setting_id": "camera.iso",
                        "registry_status": "proven_set",
                        "route_status": "proven_set",
                        "verdict": "proven_set_read_revert",
                        "node": "iso",
                        "evidence_count": 1,
                        "next_action": "Keep as a regression row.",
                    }
                ],
            },
        )
        source = settings_evidence.ReviewSource(
            route="usb_gphoto",
            label="canonical fake USB",
            directory=review_dir,
            patterns=("*usb-gphoto-review.json",),
        )
        selected = settings_evidence.latest_review_file(source)
        index = settings_evidence.load_review_index(sources=(source,))

    assert selected is not None
    assert selected.name == settings_evidence.CANONICAL_REVIEW_NAME
    assert index["sources"][0]["artifact"].endswith(settings_evidence.CANONICAL_REVIEW_NAME)
    assert index["by_setting"]["camera.iso"][0]["verdict"] == "proven_set_read_revert"
    print("  canonical review summary precedence: ok")


if __name__ == "__main__":
    test_evidence_aggregator_maps_latest_reviews_to_settings()
    test_evidence_aggregator_prefers_canonical_review_summary()
    print("settings_evidence tests: PASS")
