"""Camera-free tests for USB/gphoto artifact registry mapping."""

from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "tools"))

import review_usb_gphoto_artifacts


def _write_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")


def test_usb_review_maps_set_read_revert_and_state_matrix_to_registry_rows() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        registry = {
            "settings": [
                {
                    "setting_id": "camera.iso",
                    "label": "ISO",
                    "status": "protocol_ok_needs_behavior",
                    "routes": {"usb_gphoto": {"status": "protocol_ok_needs_behavior", "node": "iso"}},
                    "next_action": "prove ISO",
                },
                {
                    "setting_id": "camera.white_balance",
                    "label": "White Balance",
                    "status": "protocol_ok_needs_behavior",
                    "routes": {
                        "usb_gphoto": {
                            "status": "protocol_ok_needs_behavior",
                            "node": "whitebalance",
                        }
                    },
                    "next_action": "prove WB",
                },
                {
                    "setting_id": "camera.focus_mode",
                    "label": "Focus Mode",
                    "status": "protocol_ok_needs_behavior",
                    "routes": {
                        "usb_gphoto": {
                            "status": "protocol_ok_needs_behavior",
                            "node": "focusmode",
                        }
                    },
                    "next_action": "prove focus mode",
                },
                {
                    "setting_id": "camera.movie_mode",
                    "label": "Movie",
                    "status": "manual_physical_gate",
                    "routes": {
                        "usb_gphoto": {
                            "status": "protocol_ok_needs_behavior",
                            "node": "movie",
                        }
                    },
                    "next_action": "prove movie action separately from Movie mode gate",
                },
            ]
        }
        state_root = root / "r2_usb_state_matrix"
        set_root = root / "r2_usb_set_read_revert"
        movie_root = root / "r2_usb_movie_record"
        _write_json(
            state_root / "run" / "summary.json",
            {
                "states": {
                    "stills": {
                        "watch_nodes": {
                            "iso": {
                                "presence": "PRESENT",
                                "writability": "writable",
                                "value": "400",
                            },
                            "whitebalance": {
                                "presence": "PRESENT",
                                "writability": "READONLY",
                                "value": "Auto",
                            },
                            "focusmode": {
                                "presence": "ABSENT",
                                "writability": "",
                                "value": "",
                            },
                        }
                    }
                }
            },
        )
        _write_json(
            set_root / "run" / "summary.json",
            {
                "results": [
                    {
                        "setting_id": "camera.iso",
                        "label": "ISO",
                        "node": "iso",
                        "ok": True,
                        "skipped": False,
                        "set_ok": True,
                        "revert_ok": True,
                        "before": {"value": "400", "readonly": False},
                        "target": "800",
                        "after_set": {"value": "800"},
                        "after_revert": {"value": "400"},
                    },
                    {
                        "setting_id": "camera.white_balance",
                        "label": "White Balance",
                        "node": "whitebalance",
                        "ok": False,
                        "skipped": True,
                        "error": "live widget is read-only in this camera state",
                        "before": {"value": "Auto", "readonly": True},
                    },
                ]
            },
        )
        _write_json(
            movie_root / "run" / "usb-movie-record-proof.json",
            {
                "route": "usb_gphoto",
                "setting_id": "camera.movie_mode",
                "node": "movie",
                "operation": "native_movie_record_start_stop",
                "ok": True,
                "verdict": "movie_record_action_proven",
                "seconds_requested": 1.5,
                "result": {"recorded_seconds": 1.7},
                "notes": [
                    "This proves the USB/gphoto movie record operation only.",
                    "It does not prove software control of the physical Movie mode dial.",
                ],
            },
        )

        report, report_path = review_usb_gphoto_artifacts.build_report(
            [state_root, set_root, movie_root],
            out_dir=root / "out",
            registry=registry,
        )
        canonical = json.loads(
            (root / "out" / review_usb_gphoto_artifacts.CANONICAL_SUMMARY_NAME).read_text()
        )

    assert report_path.name.endswith("-usb-gphoto-review.json")
    summary = report["registry_mapping_summary"]
    assert summary["usb_rows"] == len(report["registry_mapping"])
    assert summary["usb_rows"] == 4
    assert summary["proven_set_read_revert_rows"] >= 1
    assert summary["registry_proven_set_without_set_read_revert_rows"] == 0
    assert report["artifact_summary"]["movie_record_action_artifacts"] == 1
    assert report["artifact_summary"]["movie_record_action_proven"] == 1

    mapped = {row["setting_id"]: row for row in report["registry_mapping"]}
    assert mapped["camera.iso"]["verdict"] == "proven_set_read_revert"
    assert mapped["camera.iso"]["set_read_revert_evidence"][0]["target"] == "800"
    assert mapped["camera.white_balance"]["verdict"] == "present_read_only_state_gated"
    assert mapped["camera.focus_mode"]["verdict"] == "absent_in_recorded_states"
    assert mapped["camera.movie_mode"]["verdict"] == "movie_record_action_proven"
    assert mapped["camera.movie_mode"]["action_evidence_count"] == 1
    assert mapped["camera.movie_mode"]["action_evidence"][0]["verdict"] == "movie_record_action_proven"
    assert mapped["camera.movie_mode"]["set_read_revert_evidence"] == []
    assert canonical["schema_version"] == 2
    assert canonical["registry_mapping_summary"] == report["registry_mapping_summary"]
    canonical_iso = {
        row["setting_id"]: row for row in canonical["registry_mapping"]
    }["camera.iso"]
    assert canonical_iso["verdict"] == "proven_set_read_revert"
    assert "set_read_revert_evidence" not in canonical_iso
    assert "source_dirs" not in canonical
    print("  USB/gphoto artifact-to-registry mapping: ok")


def test_usb_review_flags_registry_proven_set_without_behavior_evidence() -> None:
    registry = {
        "settings": [
            {
                "setting_id": "camera.fake",
                "label": "Fake",
                "status": "proven_set",
                "next_action": "prove it",
                "routes": {"usb_gphoto": {"status": "proven_set", "node": "fake-node"}},
            }
        ]
    }

    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        state_root = root / "r2_usb_state_matrix"
        _write_json(
            state_root / "run" / "summary.json",
            {
                "states": {
                    "stills": {
                        "watch_nodes": {
                            "fake-node": {
                                "presence": "PRESENT",
                                "writability": "writable",
                                "value": "A",
                            },
                        }
                    }
                }
            },
        )

        report, _ = review_usb_gphoto_artifacts.build_report(
            [state_root],
            out_dir=root / "out",
            registry=registry,
        )

    summary = report["registry_mapping_summary"]
    assert summary["registry_proven_set_without_set_read_revert_rows"] == 1
    assert report["registry_proven_set_without_set_read_revert"][0]["setting_id"] == "camera.fake"
    print("  USB/gphoto proven_set mismatch reporting: ok")


def test_usb_review_prioritizes_explicit_failed_write_over_old_target_skip() -> None:
    registry = {
        "settings": [
            {
                "setting_id": "camera.zoom",
                "label": "Zoom",
                "status": "proven_read",
                "next_action": "prove zoom",
                "routes": {"usb_gphoto": {"status": "available_not_wrapped", "node": "zoom"}},
            }
        ]
    }

    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        set_root = root / "r2_usb_set_read_revert"
        _write_json(
            set_root / "old" / "summary.json",
            {
                "results": [
                    {
                        "setting_id": "camera.zoom",
                        "node": "zoom",
                        "ok": False,
                        "skipped": True,
                        "error": "no alternate choice; pass --value setting_id=value",
                    }
                ]
            },
        )
        _write_json(
            set_root / "new" / "summary.json",
            {
                "results": [
                    {
                        "setting_id": "camera.zoom",
                        "node": "zoom",
                        "ok": False,
                        "skipped": False,
                        "set_ok": False,
                        "revert_ok": True,
                        "error": "no candidate target produced a set/read/revert match",
                        "target": "35.22",
                    }
                ]
            },
        )

        report, _ = review_usb_gphoto_artifacts.build_report(
            [set_root],
            out_dir=root / "out",
            registry=registry,
        )

    mapped = {row["setting_id"]: row for row in report["registry_mapping"]}
    assert mapped["camera.zoom"]["verdict"] == "failed_set_read_revert"
    print("  USB/gphoto failed write priority over target skip: ok")


if __name__ == "__main__":
    test_usb_review_maps_set_read_revert_and_state_matrix_to_registry_rows()
    test_usb_review_flags_registry_proven_set_without_behavior_evidence()
    test_usb_review_prioritizes_explicit_failed_write_over_old_target_skip()
    print("usb_gphoto_review tests: PASS")
