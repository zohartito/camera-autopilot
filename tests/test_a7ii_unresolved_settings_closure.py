"""Camera-free tests for the deterministic unresolved-settings closure reporter.

Run standalone:
    python3 tests/test_a7ii_unresolved_settings_closure.py
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "tools"))

import a7ii_unresolved_settings_closure as closure


def _write_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def _minimal_registry() -> dict:
    return {
        "camera_model": "ILCE-7M2",
        "settings": [
            {
                "setting_id": "camera.flash_mode",
                "label": "Flash Mode",
                "menu_group": "camera_settings",
                "menu_page": "camera_2",
                "photo_sources": [],
                "current_photo_value": "Flash Off",
                "greyed_in_photo": False,
                "preconditions": ["Flash-compatible still mode"],
                "status": "proven_read",
                "routes": {
                    "usb_gphoto": {
                        "status": "state_gated",
                        "node": "flashmode",
                    }
                },
                "proof_artifacts": [],
                "next_action": "Retest only with a compatible flash attached.",
            }
        ],
    }


def _minimal_fixture(root: Path) -> tuple[Path, Path, Path, Path]:
    registry_path = root / "registry.json"
    completion_path = root / "completion.json"
    legacy_path = root / "legacy.json"
    artifact_root = root / "artifacts"
    _write_json(registry_path, _minimal_registry())
    _write_json(
        completion_path,
        {
            "summary": {
                "total_settings": 1,
                "completion_state": "needs_live_evidence",
            }
        },
    )
    _write_json(legacy_path, {"summary": {"total_settings": 1}})
    _write_json(
        artifact_root / "r2_usb_gphoto_review" / "20260102-usb-gphoto-review.json",
        {
            "registry_mapping": [
                {
                    "setting_id": "camera.flash_mode",
                    "verdict": "failed_set_read_revert",
                },
                {
                    "setting_id": "camera.flash_mode",
                    "verdict": "failed_set_read_revert",
                },
            ]
        },
    )
    return registry_path, completion_path, legacy_path, artifact_root


def test_current_repo_without_private_evidence_does_not_claim_live_closure() -> None:
    # A clean checkout cannot depend on an agent's ignored July proof files.
    # Actual review-to-live-queue behavior is tested with explicit fixtures below.
    with tempfile.TemporaryDirectory() as tmp:
        evidence = Path(tmp)
        report = closure.build_report(
            artifact_root=evidence,
            completion_audit_path=evidence / "missing-completion.json",
            legacy_closure_path=evidence / "missing-legacy.json",
        )
    summary = report["summary"]
    assert summary["registry_settings"] == 162
    assert summary["inventory_complete"] is True
    assert summary["route_adjudication_complete"] is False
    assert summary["full_behavior_control_complete"] is False
    # Route 3 promotions were explicitly removed by the security tombstone.
    assert summary["proven_set_settings"] == 16
    assert summary["proven_read_settings"] == 10
    assert summary["needs_probe_settings"] == 89
    assert summary["live_test_groups"] == 0
    assert summary["live_test_items"] == 0
    assert summary["external_blocker_groups"] == 1
    assert summary["external_blocker_items"] == 37
    assert summary["external_blocker_globally_unresolved_items"] == 36
    assert summary["external_blocker_route_redundant_items"] == 1
    assert summary["closed_without_behavior_proof_route_rows"] == 0
    assert summary["unique_unproven_closed_settings"] == 0
    assert summary["unproven_closed_not_live_ready"] == 0
    assert report["live_test_groups"] == []
    assert report["data_quality"]["exhausted_repeat_tests"] == []
    assert report["data_quality"]["disposition_counts"] == {}
    blocker = report["external_blocker_groups"][0]
    assert blocker["blocker_id"] == "sony_camera_remote_command_official_package"
    assert blocker["setting_count"] == 37
    assert blocker["route_redundant_setting_ids"] == ["camera.focus_area"]
    assert report["validation_errors"] == []
    items = [item for group in report["external_blocker_groups"] for item in group["items"]]
    keys = [item["dedupe_key"] for item in items]
    assert len(keys) == len(set(keys))


def test_latest_artifact_selection_ignores_mtime() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        directory = Path(tmp)
        earlier_name = directory / "20260101-review.json"
        later_name = directory / "20260102-review.json"
        _write_json(earlier_name, {"name": "earlier"})
        _write_json(later_name, {"name": "later"})
        os.utime(earlier_name, (2_000_000_000, 2_000_000_000))
        os.utime(later_name, (1_000_000_000, 1_000_000_000))

        selected = closure.deterministic_latest(directory, ("*-review.json",))
        assert selected == later_name


def test_locator_selection_prefers_complete_scan_over_newer_partial_scan() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        directory = Path(tmp)
        complete = directory / "20260101-command-package-locator.json"
        partial = directory / "20260102-command-package-locator.json"
        _write_json(
            complete,
            {
                "candidates": [{"local_project_material": False}],
                "search": {"complete_with_policy_exclusions": True},
                "summary": {
                    "result": "official_package_not_found_locally",
                    "reviewable_candidates": 0,
                },
            },
        )
        _write_json(
            partial,
            {
                "candidates": [{"local_project_material": False}],
                "search": {"complete_with_policy_exclusions": False},
                "summary": {
                    "result": "package_not_found_scan_incomplete",
                    "reviewable_candidates": 0,
                },
            },
        )

        assert closure.deterministic_best_locator(directory) == complete


def test_locator_selection_prefers_reviewable_package_even_from_partial_scan() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        directory = Path(tmp)
        complete = directory / "20260102-command-package-locator.json"
        actionable = directory / "20260101-command-package-locator.json"
        _write_json(
            complete,
            {
                "candidates": [{"local_project_material": False}],
                "search": {"complete_with_policy_exclusions": True},
                "summary": {"reviewable_candidates": 0},
            },
        )
        _write_json(
            actionable,
            {
                "candidates": [{"local_project_material": False}],
                "search": {"complete_with_policy_exclusions": False},
                "summary": {"reviewable_candidates": 1},
            },
        )

        assert closure.deterministic_best_locator(directory) == actionable


def test_locator_selection_ignores_stale_false_positive_schema() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        directory = Path(tmp)
        stale = directory / "20260102-command-package-locator.json"
        authoritative = directory / "20260101-command-package-locator.json"
        _write_json(
            stale,
            {
                "candidates": [{"reviewable": True}],
                "search": {"complete_with_policy_exclusions": False},
                "summary": {"reviewable_candidates": 1},
            },
        )
        _write_json(
            authoritative,
            {
                "candidates": [
                    {
                        "reviewable": False,
                        "local_project_material": True,
                    }
                ],
                "search": {"complete_with_policy_exclusions": True},
                "summary": {"reviewable_candidates": 0},
            },
        )

        assert closure.deterministic_best_locator(directory) == authoritative


def test_duplicate_review_rows_collapse_to_one_setting_state_route_item() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        registry, completion, legacy, artifacts = _minimal_fixture(Path(tmp))
        report = closure.build_report(
            registry_path=registry,
            completion_audit_path=completion,
            legacy_closure_path=legacy,
            artifact_root=artifacts,
        )
        assert report["summary"]["live_test_items"] == 1
        group = report["live_test_groups"][0]
        assert group["setting_ids"] == ["camera.flash_mode"]
        assert [item["dedupe_key"] for item in group["items"]] == [
            "camera.flash_mode|flash_attached|usb_gphoto"
        ]

        duplicate_items = closure._dedupe_items(
            [
                {
                    "setting_id": "camera.flash_mode",
                    "state_id": "flash_attached",
                    "route": "usb_gphoto",
                    "label": "first",
                },
                {
                    "setting_id": "camera.flash_mode",
                    "state_id": "flash_attached",
                    "route": "usb_gphoto",
                    "label": "duplicate",
                },
            ]
        )
        assert len(duplicate_items) == 1
        assert duplicate_items[0]["label"] == "first"


def test_report_serialization_is_stable_for_identical_inputs() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        registry, completion, legacy, artifacts = _minimal_fixture(Path(tmp))
        kwargs = {
            "registry_path": registry,
            "completion_audit_path": completion,
            "legacy_closure_path": legacy,
            "artifact_root": artifacts,
        }
        first = closure.build_report(**kwargs)
        second = closure.build_report(**kwargs)
        first_json = json.dumps(first, indent=2, sort_keys=True)
        second_json = json.dumps(second, indent=2, sort_keys=True)
        assert first_json == second_json
        assert "generated_at" not in first
        assert first["sources"]["input_fingerprint_sha256"] == second["sources"][
            "input_fingerprint_sha256"
        ]


if __name__ == "__main__":
    test_current_repo_without_private_evidence_does_not_claim_live_closure()
    test_latest_artifact_selection_ignores_mtime()
    test_locator_selection_prefers_complete_scan_over_newer_partial_scan()
    test_locator_selection_prefers_reviewable_package_even_from_partial_scan()
    test_locator_selection_ignores_stale_false_positive_schema()
    test_duplicate_review_rows_collapse_to_one_setting_state_route_item()
    test_report_serialization_is_stable_for_identical_inputs()
    print("a7ii_unresolved_settings_closure tests: PASS")
