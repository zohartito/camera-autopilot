"""Camera-free tests for OpenMemories artifact registry mapping."""

from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "tools"))

import review_openmemories_artifacts


def _write_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")


def test_preflight_closed_ports_are_not_rollback_evidence() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        preflight = root / "20260623-route3-preflight"
        _write_json(
            preflight / "route3-preflight.json",
            {
                "status": "READY-FOR-BUILD",
                "build_blockers": [],
                "generated_app": {
                    "path": str(preflight / "route3_openmemories_proof_app"),
                    "endpoints": [
                        "GET /health",
                        "GET /settings",
                        "GET /settings/{id}",
                        "POST /preview",
                        "POST /focus",
                        "POST /capture",
                    ],
                },
                "ports": [
                    {"host": "192.0.2.96", "port": 23, "open": False, "error": "closed"},
                    {"host": "192.0.2.96", "port": 5555, "open": False, "error": "closed"},
                    {"host": "192.0.2.96", "port": 8080, "open": False, "error": "closed"},
                ],
            },
        )

        report, _ = review_openmemories_artifacts.build_report(
            [root],
            out_dir=root / "out",
        )

    assert report["artifact_summary"]["rollback_port_scan_count"] == 0
    assert report["artifact_summary"]["rollback_closed_evidence"] is False
    mapped = {row["setting_id"]: row for row in report["registry_mapping"]}
    assert mapped["openmem.enable_adb"]["verdict"] == "needs_rollback_port_scan"
    assert mapped["openmem.enable_telnet"]["verdict"] == "needs_rollback_port_scan"
    print("  OpenMemories preflight closed ports are not rollback proof: ok")


def test_openmemories_review_maps_preflight_endpoint_and_rollback_evidence() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        preflight = root / "20260623-route3-preflight"
        _write_json(
            preflight / "route3-preflight.json",
            {
                "status": "NEEDS-TOOLING",
                "build_blockers": ["gradle is missing"],
                "generated_app": {
                    "path": str(preflight / "route3_openmemories_proof_app"),
                    "endpoints": [
                        "GET /health",
                        "GET /settings",
                        "GET /settings/{id}",
                        "POST /preview",
                        "POST /focus",
                        "POST /capture",
                        "GET /media/latest",
                        "POST /exit",
                        "POST /shutdown",
                        "GET /rollback",
                    ],
                },
                "ports": [
                    {"host": "192.0.2.96", "port": 23, "open": False, "error": "closed"},
                    {"host": "192.0.2.96", "port": 5555, "open": False, "error": "closed"},
                    {"host": "192.0.2.96", "port": 8080, "open": False, "error": "closed"},
                ],
            },
        )
        _write_json(
            root / "rollback" / "openmemories-rollback-ports.json",
            {
                "tool": "prove_openmemories_rollback_ports",
                "host": "192.0.2.96",
                "network_context_proven": True,
                "rollback_closed_evidence": True,
                "ports": [
                    {"host": "192.0.2.96", "port": 23, "open": False, "error": "closed"},
                    {"host": "192.0.2.96", "port": 5555, "open": False, "error": "closed"},
                    {"host": "192.0.2.96", "port": 8080, "open": False, "error": "closed"},
                ],
            },
        )
        endpoint_root = root / "endpoint-proof"
        _write_json(
            endpoint_root / "health.json",
            {"method": "GET", "endpoint": "/health", "status_code": 200, "ok": True},
        )
        _write_json(
            endpoint_root / "capture.json",
            {"method": "POST", "endpoint": "/capture", "status_code": 200, "accepted": True},
        )
        _write_json(
            endpoint_root / "capture-count.json",
            {
                "method": "GET",
                "endpoint": "/settings/capture.count",
                "status_code": 200,
                "ok": True,
                "value": 1,
            },
        )
        _write_json(
            endpoint_root / "capture-bytes.json",
            {
                "method": "GET",
                "endpoint": "/settings/capture.bytes",
                "status_code": 200,
                "ok": True,
                "value": 12345,
            },
        )
        _write_json(
            endpoint_root / "camera-raw-file-type.json",
            {
                "method": "GET",
                "endpoint": "/settings/camera.raw_file_type",
                "status_code": 200,
                "ok": True,
            },
        )
        _write_json(
            endpoint_root / "self-timer-proof.json",
            {
                "artifact": "openmemories_param_self_timer_set_read_revert",
                "route": "openmemories_route3_proof_app",
                "setting_id": "param.self_timer",
                "ok": True,
                "before": 0,
                "after_set": 2,
                "after_revert": 0,
                "steps": [{}, {}, {}, {}, {}],
            },
        )
        _write_json(
            endpoint_root / "raw-rec-mode-proof.json",
            {
                "artifact": "openmemories_param_set_read_revert",
                "route": "openmemories_route3_proof_app",
                "setting_id": "param.raw_rec_mode",
                "registry_setting_id": "camera.raw_file_type",
                "ok": True,
                "executed": True,
                "before": "compress",
                "requested": "uncompress",
                "after_set": "uncompress",
                "after_revert": "compress",
                "steps": [{}, {}, {}, {}, {}, {}],
            },
        )
        proof_file = root / "focus-proof.txt"
        proof_file.write_text("focus moved")
        _write_json(
            endpoint_root / "focus-observation.json",
            {
                "tool": "prove_openmemories_focus_observation",
                "method": "POST",
                "endpoint": "/focus",
                "kind": "manual_visual_focus_completion",
                "observed_before": "near target sharp",
                "observed_after": "far target sharp",
                "observation_note": "focus moved after /focus",
                "proof_artifact": str(proof_file),
                "proof_artifact_exists": True,
                "ok": True,
            },
        )

        report, report_path = review_openmemories_artifacts.build_report(
            [root],
            out_dir=root / "out",
        )
        canonical = json.loads(
            (root / "out" / review_openmemories_artifacts.CANONICAL_SUMMARY_NAME).read_text()
        )

    assert report_path.name.endswith("-openmemories-review.json")
    assert report["artifact_summary"]["preflight_count"] == 1
    assert report["artifact_summary"]["endpoint_proof_count"] == 5
    assert report["artifact_summary"]["setting_write_proof_count"] == 2
    assert report["artifact_summary"]["generated_ergonomic_endpoints"] == [
        "GET /rollback",
        "POST /exit",
        "POST /shutdown",
    ]
    assert report["artifact_summary"]["missing_ergonomic_endpoints"] == []
    assert report["artifact_summary"]["proven_setting_writes"] == [
        "camera.raw_file_type",
        "phone.self_timer",
    ]
    assert report["artifact_summary"]["rollback_closed_evidence"] is True
    assert report["artifact_summary"]["focus_observation_count"] == 1
    assert report["artifact_summary"]["focus_observation_proven"] is True
    assert "POST /capture" not in report["artifact_summary"]["proven_endpoints"]

    mapped = {row["setting_id"]: row for row in report["registry_mapping"]}
    assert report["registry_mapping_summary"]["route_level_verdict"] == "proof_app_action_endpoint_proven"
    assert mapped["application.application_list"]["verdict"] == "proof_app_setting_not_exposed"
    assert mapped["application.application_list"]["endpoint_proofs"] == []
    assert mapped["application.application_list"]["behavior_evidence"] == []
    assert mapped["camera.raw_file_type"]["verdict"] == "proof_app_setting_write_proven"
    assert mapped["phone.self_timer"]["verdict"] == "proof_app_setting_write_proven"
    raw_file_type_evidence = mapped["camera.raw_file_type"]["behavior_evidence"]
    assert len(raw_file_type_evidence) == 1
    assert raw_file_type_evidence[0]["behavior_type"] == "set_read_revert"
    assert raw_file_type_evidence[0]["observed_before"] == "compress"
    assert raw_file_type_evidence[0]["observed_after"] == "uncompress"
    assert raw_file_type_evidence[0]["value_changed"] is True
    assert raw_file_type_evidence[0]["after_matches_request"] is True
    assert len(mapped["camera.raw_file_type"]["endpoint_proofs"]) == 1
    assert report["registry_mapping_summary"]["proof_app_setting_endpoint_proven_rows"] == 0
    assert report["registry_mapping_summary"]["proof_app_setting_write_proven_rows"] == 2
    assert report["registry_mapping_summary"]["proof_app_setting_not_exposed_rows"] > 0
    assert mapped["openmem.enable_adb"]["verdict"] == "rollback_ports_closed"
    assert mapped["openmem.enable_telnet"]["verdict"] == "rollback_ports_closed"
    assert mapped["openmem.rx100m4_4k_limit"]["verdict"] == "unsupported_on_a7ii"
    assert mapped["camera.raw_file_type"]["generated_endpoints"]
    assert canonical["schema_version"] == 2
    assert canonical["registry_mapping_summary"] == report["registry_mapping_summary"]
    canonical_mapped = {row["setting_id"]: row for row in canonical["registry_mapping"]}
    assert canonical_mapped["camera.raw_file_type"]["verdict"] == "proof_app_setting_write_proven"
    assert canonical_mapped["camera.raw_file_type"]["behavior_evidence"][0][
        "behavior_type"
    ] == "set_read_revert"
    assert canonical_mapped["application.application_list"]["behavior_evidence"] == []
    assert canonical_mapped["openmem.enable_adb"]["rollback_port_evidence"] == {
        "open_ports": [],
        "has_rollback_closed_evidence": True,
    }
    assert "preflight_evidence" not in canonical_mapped["camera.raw_file_type"]
    assert "setting_write_proofs" not in canonical_mapped["camera.raw_file_type"]
    assert "source_dirs" not in canonical
    print("  OpenMemories artifact-to-registry mapping: ok")


def test_openmemories_review_uses_latest_rollback_scan_as_current_state() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        preflight = root / "20260623-route3-preflight"
        _write_json(
            preflight / "route3-preflight.json",
            {
                "status": "READY-FOR-BUILD",
                "build_blockers": [],
                "generated_app": {
                    "path": str(preflight / "route3_openmemories_proof_app"),
                    "endpoints": [
                        "GET /health",
                        "GET /settings",
                        "GET /settings/{id}",
                        "POST /preview",
                        "POST /focus",
                        "POST /capture",
                    ],
                },
                "ports": [
                    {"host": "192.0.2.96", "port": 23, "open": True, "error": None},
                    {"host": "192.0.2.96", "port": 5555, "open": True, "error": None},
                    {"host": "192.0.2.96", "port": 8080, "open": True, "error": None},
                ],
            },
        )
        _write_json(
            root / "20260625-145208-open-rollback" / "openmemories-rollback-ports.json",
            {
                "tool": "prove_openmemories_rollback_ports",
                "host": "192.0.2.96",
                "network_context_proven": True,
                "rollback_closed_evidence": False,
                "ports": [
                    {"host": "192.0.2.96", "port": 23, "open": True, "error": None},
                    {"host": "192.0.2.96", "port": 5555, "open": True, "error": None},
                    {"host": "192.0.2.96", "port": 8080, "open": False, "error": "closed"},
                ],
            },
        )
        _write_json(
            root / "20260625-145555-closed-rollback" / "openmemories-rollback-ports.json",
            {
                "tool": "prove_openmemories_rollback_ports",
                "host": "192.0.2.96",
                "network_context_proven": True,
                "rollback_closed_evidence": True,
                "ports": [
                    {"host": "192.0.2.96", "port": 23, "open": False, "error": "closed"},
                    {"host": "192.0.2.96", "port": 5555, "open": False, "error": "closed"},
                    {"host": "192.0.2.96", "port": 8080, "open": False, "error": "closed"},
                ],
            },
        )

        report, _ = review_openmemories_artifacts.build_report(
            [root],
            out_dir=root / "out",
        )

    assert report["artifact_summary"]["rollback_port_scan_count"] == 2
    assert report["artifact_summary"]["rollback_closed_evidence"] is True
    assert report["artifact_summary"]["open_developer_ports"] == []
    mapped = {row["setting_id"]: row for row in report["registry_mapping"]}
    assert mapped["openmem.enable_adb"]["verdict"] == "rollback_ports_closed"
    assert mapped["openmem.enable_telnet"]["verdict"] == "rollback_ports_closed"
    ports = mapped["openmem.enable_adb"]["rollback_port_evidence"]
    assert ports["latest_rollback_scan"]["artifact"].startswith("20260625-145555")
    assert {item["port"] for item in ports["historical_open_ports"]} == {23, 5555, 8080}
    print("  OpenMemories latest rollback scan wins current port state: ok")


def test_openmemories_review_independently_rejects_forged_partial_rollback_claim() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        _write_json(
            root / "rollback" / "openmemories-rollback-ports.json",
            {
                "tool": "prove_openmemories_rollback_ports",
                "host": "192.0.2.96",
                "network_context_proven": True,
                "rollback_closed_evidence": True,
                "ports": [
                    {"host": "192.0.2.96", "port": 8080, "open": False, "error": "closed"},
                ],
            },
        )

        report, _ = review_openmemories_artifacts.build_report(
            [root],
            out_dir=root / "out",
        )

    assert report["artifact_summary"]["rollback_closed_evidence"] is False
    mapped = {row["setting_id"]: row for row in report["registry_mapping"]}
    assert mapped["openmem.enable_adb"]["verdict"] == "needs_rollback_port_scan"
    assert mapped["openmem.enable_telnet"]["verdict"] == "needs_rollback_port_scan"
    ports = mapped["openmem.enable_adb"]["rollback_port_evidence"]
    assert ports["latest_complete_required_scan"] is False
    assert ports["latest_scanned_ports"] == [8080]
    print("  OpenMemories review rejects forged partial rollback claim: ok")


def test_openmemories_review_rejects_unknown_required_port_state() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        _write_json(
            root / "rollback" / "openmemories-rollback-ports.json",
            {
                "tool": "prove_openmemories_rollback_ports",
                "host": "192.0.2.96",
                "network_context_proven": True,
                "rollback_closed_evidence": True,
                "ports": [
                    {"host": "192.0.2.96", "port": 23, "open": False, "error": "closed"},
                    {"host": "192.0.2.96", "port": 5555, "error": "unknown"},
                    {"host": "192.0.2.96", "port": 8080, "open": False, "error": "closed"},
                ],
            },
        )

        report, _ = review_openmemories_artifacts.build_report(
            [root],
            out_dir=root / "out",
        )

    assert report["artifact_summary"]["rollback_closed_evidence"] is False
    mapped = {row["setting_id"]: row for row in report["registry_mapping"]}
    ports = mapped["openmem.enable_adb"]["rollback_port_evidence"]
    assert ports["latest_complete_required_scan"] is True
    assert ports["latest_all_required_closed"] is False
    assert [item["port"] for item in ports["unknown_ports"]] == [5555]
    print("  OpenMemories review rejects unknown port state: ok")


if __name__ == "__main__":
    test_preflight_closed_ports_are_not_rollback_evidence()
    test_openmemories_review_maps_preflight_endpoint_and_rollback_evidence()
    test_openmemories_review_uses_latest_rollback_scan_as_current_state()
    test_openmemories_review_independently_rejects_forged_partial_rollback_claim()
    test_openmemories_review_rejects_unknown_required_port_state()
    print("openmemories_review tests: PASS")
