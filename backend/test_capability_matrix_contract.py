"""Camera-free contract tests for the falsification capability matrix."""

from __future__ import annotations

import sys
import tempfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "backend"))
sys.path.insert(0, str(REPO_ROOT / "tests"))

import capability_matrix_contract
from _runner import run_module_tests

MATRIX_PATH = REPO_ROOT / "docs" / "falsification" / "capability_matrix.md"
RECOMMENDATION_PATH = REPO_ROOT / "artifacts" / "falsification" / "a7ii_single_daemon_recommendation.md"


def test_matrix_rows_use_goal_status_vocabulary() -> None:
    summary = capability_matrix_contract.validate_matrix_contract(
        MATRIX_PATH,
        recommendation_path=RECOMMENDATION_PATH,
    )

    assert summary["matrix_tombstoned"] is True
    assert summary["rows"] == 0
    assert summary["bad_status_rows"] == []
    assert summary["unknown_status_rows"] == []
    assert set(summary["statuses"]) <= {"PROVEN-YES", "PROVEN-NO", "NEEDS-HARDWARE", "NEEDS-PACKAGE"}
    assert summary["statuses"] == []
    print("  capability matrix status vocabulary: ok")


def test_matrix_links_single_daemon_recommendation() -> None:
    summary = capability_matrix_contract.validate_matrix_contract(
        MATRIX_PATH,
        recommendation_path=RECOMMENDATION_PATH,
    )

    assert summary["recommendation_linked"] is False
    assert summary["recommendation_exists"] is False
    assert summary["recommendation_mentions_no_final_hardware"] is False
    assert summary["recommendation_mentions_route_gates"] is False
    assert summary["recommendation_uses_runnable_camera_remote_command_examples"] is False
    print("  capability matrix recommendation link: ok")


def test_recommendation_rejects_inline_camera_remote_command_env_expansion() -> None:
    with tempfile.TemporaryDirectory() as temp_dir:
        temp_matrix = Path(temp_dir) / MATRIX_PATH.name
        temp_recommendation = Path(temp_dir) / RECOMMENDATION_PATH.name
        temp_matrix.write_text("| Route | Capability | Current Status | Evidence | Next |\n")
        recommendation_text = "\n".join(
            [
                "Gate A: Official Command Package",
                "Gate B: Smart Remote Current Live Surface",
                "Gate C: USB Behavior, Not Just Inventory",
                "Gate D: Raw PTP Focus Point",
                "Gate E: OpenMemories Proof App",
            ]
        )
        temp_recommendation.write_text(
            recommendation_text
            + "\nCAMERA_REMOTE_COMMAND_PACKAGE=/path/to/pkg.zip "
            + 'python3 tools/review_camera_remote_command_package.py "$CAMERA_REMOTE_COMMAND_PACKAGE"\n'
        )

        summary = capability_matrix_contract.validate_matrix_contract(
            temp_matrix,
            recommendation_path=temp_recommendation,
        )

    assert summary["recommendation_uses_runnable_camera_remote_command_examples"] is False
    print("  camera remote command examples avoid inline env expansion: ok")


def test_recommendation_route_gate_check_includes_raw_ptp_gate() -> None:
    with tempfile.TemporaryDirectory() as temp_dir:
        temp_matrix = Path(temp_dir) / MATRIX_PATH.name
        temp_recommendation = Path(temp_dir) / RECOMMENDATION_PATH.name
        temp_matrix.write_text("| Route | Capability | Current Status | Evidence | Next |\n")
        recommendation_text = "\n".join(
            [
                "Gate A: Official Command Package",
                "Gate B: Smart Remote Current Live Surface",
                "Gate C: USB Behavior, Not Just Inventory",
                "Gate D: Raw PTP Focus Point",
                "Gate E: OpenMemories Proof App",
            ]
        )
        temp_recommendation.write_text(
            recommendation_text.replace("Gate D: Raw PTP Focus Point", "Missing Raw PTP Focus Point"),
        )

        summary = capability_matrix_contract.validate_matrix_contract(
            temp_matrix,
            recommendation_path=temp_recommendation,
        )

    assert summary["recommendation_mentions_route_gates"] is False
    print("  capability matrix recommendation route gates include raw PTP: ok")


def test_matrix_statuses_have_required_evidence_shape() -> None:
    summary = capability_matrix_contract.validate_matrix_contract(
        MATRIX_PATH,
        recommendation_path=RECOMMENDATION_PATH,
    )

    assert summary["proven_yes_without_artifact"] == []
    assert summary["needs_hardware_without_specific_gate"] == []
    assert summary["needs_package_without_review_gate"] == []
    assert summary["proven_no_without_repeated_artifact"] == []
    assert summary["missing_evidence_artifacts"] == []
    print("  capability matrix evidence shape: ok")


def test_matrix_evidence_artifact_paths_must_exist() -> None:
    broken_matrix = (
        "| Route | Capability | Current Status | Evidence | Next |\n"
        "| R1 | test | PROVEN-YES | artifact `artifacts/falsification/does-not-exist-for-contract-test.md` | none |\n"
    )

    with tempfile.TemporaryDirectory() as temp_dir:
        temp_matrix = Path(temp_dir) / "capability_matrix.md"
        temp_matrix.write_text(broken_matrix)
        summary = capability_matrix_contract.validate_matrix_contract(
            temp_matrix,
            recommendation_path=RECOMMENDATION_PATH,
            repo_root=REPO_ROOT,
        )

    missing = summary["missing_evidence_artifacts"]
    assert missing
    assert missing[0]["path"] == "artifacts/falsification/does-not-exist-for-contract-test.md"
    print("  capability matrix evidence artifact existence: ok")


if __name__ == "__main__":
    raise SystemExit(run_module_tests(__name__))
