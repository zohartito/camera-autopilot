"""Camera-free tests for registry promotion planning from review artifacts."""

from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "backend"))

import settings_evidence
import settings_promotions
import settings_registry


def _write_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")


def _fake_review_index(root: Path) -> dict:
    usb_dir = root / "usb"
    smart_dir = root / "smart"
    raw_dir = root / "raw"
    _write_json(
        usb_dir / "20260101-usb-gphoto-review.json",
        {
            "generated_at": "20260101",
            "registry_mapping_summary": {"usb_rows": 1},
            "registry_mapping": [
                {
                    "setting_id": "camera.aspect_ratio",
                    "label": "Aspect Ratio",
                    "registry_status": "available_not_wrapped",
                    "route_status": "available_not_wrapped",
                    "verdict": "proven_set_read_revert",
                    "node": "aspectratio",
                    "evidence_count": 1,
                    "next_action": "Set/read/revert is proven.",
                }
            ],
        },
    )
    _write_json(
        smart_dir / "20260101-smart-remote-review.json",
        {
            "generated_at": "20260101",
            "registry_mapping_summary": {"smart_remote_rows": 2},
            "registry_mapping": [
                {
                    "setting_id": "camera.creative_style",
                    "label": "Creative Style",
                    "registry_status": "needs_probe",
                    "route_status": "needs_probe",
                    "verdict": "route_setter_seen_in_artifact",
                    "matched_setters": ["setCreativeStyle"],
                    "evidence_count": 1,
                    "next_action": "Behavior proof still required.",
                },
                {
                    "setting_id": "camera.focus_mode",
                    "label": "Focus Mode",
                    "registry_status": "proven_set",
                    "route_status": "available_not_wrapped",
                    "verdict": "behavior_proven",
                    "matched_setters": ["setFocusMode"],
                    "behavior_evidence": [
                        {
                            "method": "setFocusMode",
                            "requested_value": "MF",
                            "observed_before": "AF-S",
                            "observed_after": "MF",
                            "human_verdict": "proven",
                        }
                    ],
                    "evidence_count": 2,
                    "next_action": "Behavior proof accepted.",
                }
            ],
        },
    )
    _write_json(
        raw_dir / "20260101-raw-ptp-review.json",
        {
            "generated_at": "20260101",
            "registry_mapping_summary": {"raw_ptp_rows": 1},
            "registry_mapping": [
                {
                    "setting_id": "camera.focus_area",
                    "label": "Focus Area",
                    "registry_status": "protocol_ok_needs_behavior",
                    "route_status": "protocol_ok_needs_behavior",
                    "verdict": "behavior_proven",
                    "propcode": "0xd2c1",
                    "evidence_count": 2,
                    "next_action": "Behavior proof accepted.",
                }
            ],
        },
    )
    sources = (
        settings_evidence.ReviewSource(
            route="usb_gphoto",
            label="fake usb",
            directory=usb_dir,
            patterns=("*usb-gphoto-review.json",),
        ),
        settings_evidence.ReviewSource(
            route="smart_remote_wifi",
            label="fake smart",
            directory=smart_dir,
            patterns=("*smart-remote-review.json",),
        ),
        settings_evidence.ReviewSource(
            route="raw_ptp",
            label="fake raw",
            directory=raw_dir,
            patterns=("*raw-ptp-review.json",),
        ),
    )
    return settings_evidence.load_review_index(sources=sources)


def _promotion_test_registry() -> dict:
    registry = settings_registry.load_registry()
    rows = {row["setting_id"]: row for row in registry["settings"]}

    aspect = rows["camera.aspect_ratio"]
    aspect["status"] = "available_not_wrapped"
    aspect["routes"]["usb_gphoto"]["status"] = "available_not_wrapped"

    focus_area = rows["camera.focus_area"]
    focus_area["status"] = "protocol_ok_needs_behavior"
    focus_area["routes"]["raw_ptp"]["status"] = "protocol_ok_needs_behavior"

    focus_mode = rows["camera.focus_mode"]
    focus_mode["status"] = "proven_set"
    focus_mode["routes"]["smart_remote_wifi"]["status"] = "available_not_wrapped"
    focus_mode["routes"]["smart_remote_wifi"].pop("method", None)
    focus_mode["routes"]["smart_remote_wifi"].pop("method_hint", None)

    creative = rows["camera.creative_style"]
    creative["status"] = "needs_probe"
    creative["routes"]["smart_remote_wifi"]["status"] = "needs_probe"
    creative["next_action"] = "Probe Smart Remote setter behavior before promotion."

    return registry


def test_promotion_plan_promotes_behavior_proof_only() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        review_index = _fake_review_index(Path(tmp))
        registry = _promotion_test_registry()
        report = settings_promotions.build_promotion_plan(registry=registry, review_index=review_index)

    by_setting_route = {
        (item["setting_id"], item["route"]): item
        for item in report["promotions"]
    }
    usb = by_setting_route[("camera.aspect_ratio", "usb_gphoto")]
    assert usb["action"] == "usb_evidence_requires_operator_attestation"
    assert usb["can_apply"] is False
    assert usb["registry_patch"] is None

    raw = by_setting_route[("camera.focus_area", "raw_ptp")]
    assert raw["action"] == "evidence_requires_operator_attestation"
    assert raw["can_apply"] is False

    smart = by_setting_route[("camera.creative_style", "smart_remote_wifi")]
    assert smart["action"] == "needs_behavior_proof"
    assert smart["can_apply"] is False
    assert smart["recommended_route_status"] is None

    smart_proven = by_setting_route[("camera.focus_mode", "smart_remote_wifi")]
    assert smart_proven["action"] == "evidence_requires_operator_attestation"
    assert smart_proven["can_apply"] is False
    assert smart_proven["registry_patch"] is None

    assert report["summary"]["applicable_promotions"] == 0
    assert report["summary"]["actions"]["needs_behavior_proof"] == 1
    assert report["summary"]["actions"]["usb_evidence_requires_operator_attestation"] == 1
    print("  settings promotion planning: ok")


def test_promotion_plan_does_not_auto_promote_guarded_rows() -> None:
    review_index = {
        "sources": [],
        "by_route_verdict": {"openmemories": {"proof_app_action_endpoint_proven": 1}},
        "by_setting": {
            "openmem.enable_adb": [
                {
                    "route": "openmemories",
                    "review_label": "fake",
                    "review_artifact": "fake.json",
                    "verdict": "proof_app_action_endpoint_proven",
                    "evidence_count": 1,
                }
            ]
        },
    }
    report = settings_promotions.build_promotion_plan(review_index=review_index, setting_id="openmem.enable_adb")
    item = report["promotions"][0]
    assert item["action"] == "route_permanently_disabled"
    assert item["guarded"] is True
    assert item["can_apply"] is False
    print("  guarded promotion planning: ok")


def test_promotion_plan_classifies_phone_observations_without_api_promotion() -> None:
    review_index = {
        "sources": [],
        "by_route_verdict": {
            "smart_remote_phone": {
                "phone_app_manual_preference_observed": 1,
                "needs_phone_app_observation": 1,
            }
        },
        "by_setting": {
            "phone.grid_line": [
                {
                    "route": "smart_remote_phone",
                    "review_label": "fake phone preferences",
                    "review_artifact": "fake-phone.json",
                    "verdict": "phone_app_manual_preference_observed",
                    "evidence_count": 1,
                    "observed_value": "Off",
                }
            ],
            "custom.grid_line": [
                {
                    "route": "smart_remote_phone",
                    "review_label": "fake phone preferences",
                    "review_artifact": "fake-phone.json",
                    "verdict": "needs_phone_app_observation",
                    "evidence_count": 0,
                }
            ],
        },
    }
    report = settings_promotions.build_promotion_plan(review_index=review_index)
    by_setting = {item["setting_id"]: item for item in report["promotions"]}

    observed = by_setting["phone.grid_line"]
    assert observed["action"] == "observation_recorded_no_api_promotion"
    assert observed["can_apply"] is False
    assert observed["recommended_route_status"] is None

    missing = by_setting["custom.grid_line"]
    assert missing["action"] == "needs_more_evidence"
    assert missing["can_apply"] is False
    print("  phone observation promotion policy: ok")


def test_promotion_plan_keeps_raw_ptp_behavior_rejection_out_of_promotions() -> None:
    review_index = {
        "sources": [],
        "by_route_verdict": {"raw_ptp": {"behavior_rejected": 1}},
        "by_setting": {
            "camera.focus_area": [
                {
                    "route": "raw_ptp",
                    "review_label": "fake raw",
                    "review_artifact": "fake-raw-review.json",
                    "verdict": "behavior_rejected",
                    "evidence_count": 3,
                    "human_verdict": "rejected",
                    "observed_behavior": "AF-point writes returned OK but did not change focus target.",
                }
            ]
        },
    }
    report = settings_promotions.build_promotion_plan(review_index=review_index, setting_id="camera.focus_area")
    item = report["promotions"][0]
    assert item["action"] == "route_behavior_rejected"
    assert item["can_apply"] is False
    assert item["recommended_route_status"] is None
    print("  raw PTP rejection promotion policy: ok")


def test_promotion_plan_keeps_failed_smart_remote_setter_out_of_promotions() -> None:
    review_index = {
        "sources": [],
        "by_route_verdict": {"smart_remote_wifi": {"setter_attempt_failed": 1}},
        "by_setting": {
            "camera.aspect_ratio": [
                {
                    "route": "smart_remote_wifi",
                    "review_label": "fake smart remote",
                    "review_artifact": "fake-smart-remote-review.json",
                    "verdict": "setter_attempt_failed",
                    "evidence_count": 1,
                    "method": "setStillSize",
                    "error": "Smart Remote method setStillSize is not available after session unlock.",
                }
            ]
        },
    }
    report = settings_promotions.build_promotion_plan(
        review_index=review_index,
        setting_id="camera.aspect_ratio",
    )
    item = report["promotions"][0]
    assert item["action"] == "route_behavior_rejected"
    assert item["can_apply"] is False
    assert item["recommended_route_status"] is None
    print("  Smart Remote failed setter promotion policy: ok")


def test_promotion_plan_keeps_failed_usb_set_read_revert_out_of_promotions() -> None:
    review_index = {
        "sources": [],
        "by_route_verdict": {"usb_gphoto": {"failed_set_read_revert": 1}},
        "by_setting": {
            "camera.flash_mode": [
                {
                    "route": "usb_gphoto",
                    "review_label": "fake usb gphoto",
                    "review_artifact": "fake-usb-gphoto-review.json",
                    "verdict": "failed_set_read_revert",
                    "evidence_count": 1,
                    "node": "flashmode",
                    "error": "no candidate target produced a set/read/revert match",
                }
            ]
        },
    }
    report = settings_promotions.build_promotion_plan(
        review_index=review_index,
        setting_id="camera.flash_mode",
    )
    item = report["promotions"][0]
    assert item["action"] == "usb_evidence_requires_operator_attestation"
    assert item["can_apply"] is False
    assert item["recommended_route_status"] is None
    print("  USB evidence requires operator attestation: ok")


def test_promotion_plan_keeps_usb_movie_action_out_of_setting_promotions() -> None:
    review_index = {
        "sources": [],
        "by_route_verdict": {"usb_gphoto": {"movie_record_action_proven": 1}},
        "by_setting": {
            "camera.movie_mode": [
                {
                    "route": "usb_gphoto",
                    "review_label": "fake usb gphoto",
                    "review_artifact": "fake-usb-gphoto-review.json",
                    "verdict": "movie_record_action_proven",
                    "action_evidence_count": 1,
                    "node": "movie",
                }
            ]
        },
    }
    report = settings_promotions.build_promotion_plan(
        review_index=review_index,
        setting_id="camera.movie_mode",
    )
    item = report["promotions"][0]
    assert item["action"] == "usb_evidence_requires_operator_attestation"
    assert item["can_apply"] is False
    assert item["recommended_route_status"] is None
    print("  USB movie action promotion policy: ok")


def test_apply_promotion_plan_is_dry_run_first_and_can_write_copy() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        registry_path = root / "a7ii_settings_registry.json"
        out_path = root / "promoted_registry.json"
        test_registry = _promotion_test_registry()
        registry_path.write_text(
            json.dumps(test_registry, indent=2, sort_keys=False),
            encoding="utf-8",
        )
        original = registry_path.read_text(encoding="utf-8")
        review_index = _fake_review_index(root / "reviews")

        dry_run = settings_promotions.apply_promotion_plan(
            registry_path=registry_path,
            review_index=review_index,
        )
        assert dry_run["summary"]["dry_run"] is True
        assert dry_run["summary"]["selected_promotions"] == 0
        assert dry_run["summary"]["written"] is False
        assert registry_path.read_text(encoding="utf-8") == original

        applied = settings_promotions.apply_promotion_plan(
            registry_path=registry_path,
            out_path=out_path,
            review_index=review_index,
            dry_run=False,
        )
        assert applied["summary"]["written"] is False
        assert applied["summary"]["target_path"] == str(out_path)
        assert registry_path.read_text(encoding="utf-8") == original

        assert not out_path.exists()
    print("  settings promotion apply workflow: ok")


if __name__ == "__main__":
    test_promotion_plan_promotes_behavior_proof_only()
    test_promotion_plan_does_not_auto_promote_guarded_rows()
    test_promotion_plan_classifies_phone_observations_without_api_promotion()
    test_promotion_plan_keeps_raw_ptp_behavior_rejection_out_of_promotions()
    test_promotion_plan_keeps_failed_smart_remote_setter_out_of_promotions()
    test_promotion_plan_keeps_failed_usb_set_read_revert_out_of_promotions()
    test_promotion_plan_keeps_usb_movie_action_out_of_setting_promotions()
    test_apply_promotion_plan_is_dry_run_first_and_can_write_copy()
    print("settings_promotions tests: PASS")
