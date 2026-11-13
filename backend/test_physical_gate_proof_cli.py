"""Command-line tests for guided physical/admin gate proof artifacts."""

from __future__ import annotations

import json
import subprocess
import sys
import tempfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "tools"))

import a7ii_remaining_gates_report
import review_physical_gate_artifacts

CLI = REPO_ROOT / "tools" / "a7ii_physical_gate_proof.py"
REMAINING_GATES_CLI = REPO_ROOT / "tools" / "a7ii_remaining_gates_report.py"


def _run_cli(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(CLI), *args],
        cwd=REPO_ROOT,
        text=True,
        capture_output=True,
        check=False,
    )


def _run_remaining_gates_cli(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(REMAINING_GATES_CLI), *args],
        cwd=REPO_ROOT,
        text=True,
        capture_output=True,
        check=False,
    )


def test_physical_gate_proof_requires_observed_state_for_real_artifacts() -> None:
    result = _run_cli("--setting", "setup.usb_connection")
    assert result.returncode != 0
    assert "--observed-state" in result.stderr

    placeholder = _run_cli(
        "--setting",
        "setup.usb_connection",
        "--observed-state",
        "<describe camera/menu state>",
    )
    assert placeholder.returncode != 0
    assert "visible camera/menu state" in placeholder.stderr
    print("  physical proof observed-state guard: ok")


def test_physical_gate_proof_writes_observation_artifact() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        result = _run_cli(
            "--setting",
            "setup.usb_connection",
            "--observed-state",
            "Setup menu page 4 shows USB Connection set to PC Remote; changing it would drop USB control.",
            "--evidence-note",
            "Operator verified this on the camera body before the USB proof run.",
            "--out-dir",
            tmp,
        )
        assert result.returncode == 0, result.stderr
        out_dir = Path(result.stdout.strip())
        report_path = out_dir / "physical-gate.json"
        summary_path = out_dir / "summary.md"
        assert report_path.exists()
        assert summary_path.exists()
        report = json.loads(report_path.read_text(encoding="utf-8"))
        assert report["setting_id"] == "setup.usb_connection"
        assert report["evidence_quality"]["has_observed_state"] is True
        assert report["evidence_quality"]["note_count"] == 1
        assert "PC Remote" in report["observed_state"]
    print("  physical proof artifact write: ok")


def test_admin_gate_proof_requires_observation_only_ack() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        missing_ack = _run_cli(
            "--setting",
            "setup.format",
            "--observed-state",
            "Setup page 5 shows the Format row; operator did not enter or confirm it.",
            "--out-dir",
            tmp,
        )
        assert missing_ack.returncode != 0
        assert "--admin-observation-only" in missing_ack.stderr

        result = _run_cli(
            "--setting",
            "setup.format",
            "--observed-state",
            "Setup page 5 shows the Format row; operator did not enter or confirm it.",
            "--admin-observation-only",
            "--evidence-note",
            "Observation only. No card format or confirmation screen was triggered.",
            "--out-dir",
            tmp,
        )
        assert result.returncode == 0, result.stderr
        out_dir = Path(result.stdout.strip())
        report = json.loads((out_dir / "physical-gate.json").read_text(encoding="utf-8"))
        summary = (out_dir / "summary.md").read_text(encoding="utf-8")
        assert report["setting_id"] == "setup.format"
        assert report["admin_guarded"] is True
        assert report["admin_observation_only"] is True
        assert report["destructive_action_performed"] is False
        assert report["verdict"] == "dangerous_admin_observed_no_action"
        assert "No destructive/admin action was performed" in summary
    print("  admin proof observation-only guard: ok")


def test_remaining_gates_report_marks_admin_commands_observation_only() -> None:
    report = a7ii_remaining_gates_report.build_report()
    markdown = a7ii_remaining_gates_report._markdown(report)
    rows = {item["setting_id"]: item for item in report["gate_rows"]}

    assert "dangerous_admin_proofs_require_observation_only_ack" in markdown
    assert "setup.format" in markdown
    assert "--setting setup.format --observed-state" in markdown
    assert "--admin-observation-only --out-dir" in markdown
    assert "setup.usb_connection" in markdown
    usb_line = next(line for line in markdown.splitlines() if "--setting setup.usb_connection" in line)
    assert "--admin-observation-only" not in usb_line

    assert len(report["gate_rows"]) == report["summary"]["remaining_gate_rows"]
    assert all(item["exact_next_commands"] for item in report["gate_rows"])
    assert rows["setup.format"]["gate_category"] == "dangerous_admin_guarded"
    assert rows["setup.format"]["requires_admin_observation_only_ack"] is True
    assert rows["setup.format"]["guided_physical_command"]
    assert "--admin-observation-only" in rows["setup.format"]["guided_physical_command"]
    assert rows["setup.usb_connection"]["gate_category"] == "physical_or_guided"
    assert rows["setup.usb_connection"]["requires_admin_observation_only_ack"] is False
    assert "--admin-observation-only" not in rows["setup.usb_connection"]["guided_physical_command"]
    assert any(
        command["route"] == "usb_gphoto"
        for command in rows["camera.raw_file_type"]["proof_commands"]
    )
    print("  remaining gates admin proof commands: ok")


def test_remaining_gates_report_writes_quiet_json_artifact() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        out = Path(tmp) / "remaining-gates.json"
        result = _run_remaining_gates_cli("--json", "--quiet", "--out", str(out))
        assert result.returncode == 0, result.stderr
        assert result.stdout == ""
        report = json.loads(out.read_text(encoding="utf-8"))

    assert report["summary"]["remaining_gate_rows"] == len(report["gate_rows"])
    assert all(item["exact_next_commands"] for item in report["gate_rows"])
    print("  remaining gates quiet JSON artifact: ok")


def test_remaining_gates_report_cli_filters_to_one_setting() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        out = Path(tmp) / "format-gate.json"
        result = _run_remaining_gates_cli(
            "--json",
            "--quiet",
            "--setting",
            "setup.format",
            "--out",
            str(out),
        )
        assert result.returncode == 0, result.stderr
        report = json.loads(out.read_text(encoding="utf-8"))

    assert report["summary"]["remaining_gate_rows"] >= 100
    assert report["summary"]["returned_gate_rows"] == 1
    assert report["gate_rows"][0]["setting_id"] == "setup.format"
    assert report["gate_rows"][0]["requires_admin_observation_only_ack"] is True
    assert all(command["setting_ids"] == ["setup.format"] for command in report["route_commands"])

    markdown = _run_remaining_gates_cli("--setting", "setup.format")
    assert markdown.returncode == 0, markdown.stderr
    assert "returned_gate_rows: `1`" in markdown.stdout
    assert "### dangerous_admin_guarded" in markdown.stdout
    assert "setup.format" in markdown.stdout
    assert "setup.setting_reset" not in markdown.stdout
    print("  remaining gates CLI setting filter: ok")


def test_physical_gate_review_maps_observed_artifacts() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        proof_result = _run_cli(
            "--setting",
            "camera.panorama_size",
            "--observed-state",
            "Sweep Panorama mode shows Panorama Size Standard/Wide; operator did not claim remote control.",
            "--out-dir",
            tmp,
        )
        assert proof_result.returncode == 0, proof_result.stderr
        report, report_path = review_physical_gate_artifacts.build_report(
            Path(tmp),
            out_dir=Path(tmp) / "review",
        )

    assert report_path.name.endswith("-physical-gate-review.json")
    summary = report["registry_mapping_summary"]
    assert summary["physical_or_admin_observed_rows"] >= 1
    rows = {row["setting_id"]: row for row in report["registry_mapping"]}
    assert rows["camera.panorama_size"]["verdict"] == "manual_gate_recorded_not_software_controlled"
    assert rows["camera.panorama_size"]["evidence_count"] == 1
    print("  physical gate review mapping: ok")


if __name__ == "__main__":
    test_physical_gate_proof_requires_observed_state_for_real_artifacts()
    test_physical_gate_proof_writes_observation_artifact()
    test_admin_gate_proof_requires_observation_only_ack()
    test_remaining_gates_report_marks_admin_commands_observation_only()
    test_remaining_gates_report_writes_quiet_json_artifact()
    test_remaining_gates_report_cli_filters_to_one_setting()
    test_physical_gate_review_maps_observed_artifacts()
    print("physical_gate_proof_cli tests: PASS")
