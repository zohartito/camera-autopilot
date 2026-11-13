"""Camera-free tests for deploy/pi_bringup.sh.

These never touch a camera, network, apt, or systemd. The dry-run must not spawn
apt-get or systemctl; we prove that by prepending a PATH directory holding fake
apt-get/systemctl that write a sentinel and abort if the script ever calls them.
"""
from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
SCRIPT = REPO / "deploy" / "pi_bringup.sh"
PHASES = ["host-facts", "board-config", "apt", "udev", "venv", "readiness", "service", "proof", "soak"]


def _printed_receipts(stdout: str) -> dict[str, dict]:
    """A dry run prints each receipt as one `RECEIPT <phase> <json>` line instead of writing it."""
    receipts = {}
    for line in stdout.splitlines():
        if line.startswith("  RECEIPT "):
            _, phase, payload = line.strip().split(" ", 2)
            receipts[phase] = json.loads(payload)
    return receipts


def test_script_exists():
    assert SCRIPT.is_file(), "deploy/pi_bringup.sh is missing"


def test_bash_syntax_check():
    result = subprocess.run(["bash", "-n", str(SCRIPT)], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr


def test_help_exits_zero():
    result = subprocess.run(["bash", str(SCRIPT), "--help"], capture_output=True, text=True)
    assert result.returncode == 0
    assert "--dry-run" in result.stdout and "--apply" in result.stdout


def _fake_path_dir(tmp_path: Path) -> tuple[Path, Path]:
    """A PATH dir whose fake apt-get/systemctl abort and log to a sentinel."""
    fake_bin = tmp_path / "fakebin"
    fake_bin.mkdir()
    sentinel = tmp_path / "forbidden-calls.log"
    for name in ("apt-get", "systemctl", "sudo", "udevadm"):
        script = fake_bin / name
        script.write_text(
            "#!/usr/bin/env bash\n"
            f'echo "{name} $*" >> "{sentinel}"\n'
            "exit 99\n"
        )
        script.chmod(0o755)
    return fake_bin, sentinel


def _run_dry(tmp_path: Path, *extra: str) -> tuple[subprocess.CompletedProcess, Path, Path]:
    fake_bin, sentinel = _fake_path_dir(tmp_path)
    receipts = tmp_path / "receipts"
    env = dict(os.environ)
    env["PATH"] = f"{fake_bin}{os.pathsep}{env['PATH']}"
    result = subprocess.run(
        ["bash", str(SCRIPT), "--dry-run", "--receipt-dir", str(receipts), *extra],
        capture_output=True, text=True, env=env,
    )
    return result, sentinel, receipts


def test_dry_run_never_spawns_apt_or_systemctl(tmp_path):
    result, sentinel, _ = _run_dry(tmp_path)
    assert result.returncode == 0, result.stderr
    assert not sentinel.exists(), (
        "dry-run invoked a forbidden command:\n" + sentinel.read_text()
    )


def test_dry_run_shows_every_phase_as_would_run_or_collected(tmp_path):
    result, _, _ = _run_dry(tmp_path)
    out = result.stdout
    for phase in PHASES:
        assert f"phase: {phase}" in out, f"phase {phase} not announced"
    # Side-effecting phases advertise WOULD-RUN; none claim to have executed apt/systemctl.
    assert "WOULD-RUN" in out
    assert "apt-get install" in out  # printed, not executed


def test_host_facts_receipt_schema_printed_even_off_linux(tmp_path):
    result, _, receipts = _run_dry(tmp_path)
    assert not receipts.exists(), "a dry run must not create the receipt dir"
    data = _printed_receipts(result.stdout)["host-facts"]
    for key in ("phase", "mode", "executed", "arch", "is_arm64", "is_raspberry_pi",
                "os_supported", "mem_total_kib", "free_disk_kib", "thermal_zones",
                "warnings", "written_at_unix"):
        assert key in data, f"host-facts receipt missing key: {key}"
    assert data["phase"] == "host-facts"
    assert data["mode"] == "dry-run"
    assert data["executed"] is True  # host-facts is read-only and always runs
    assert isinstance(data["thermal_zones"], list)
    assert isinstance(data["warnings"], list)


def test_every_phase_prints_a_receipt_and_writes_nothing(tmp_path):
    result, _, receipts = _run_dry(tmp_path)
    printed = _printed_receipts(result.stdout)
    for phase in PHASES:
        assert printed.get(phase, {}).get("mode") == "dry-run", f"no printed receipt for phase {phase}"
        assert f"WOULD-WRITE: {receipts / (phase + '.json')}" in result.stdout
    assert not receipts.exists(), "a dry run wrote under the receipt dir"


def test_single_phase_selection(tmp_path):
    fake_bin, sentinel = _fake_path_dir(tmp_path)
    receipts = tmp_path / "receipts"
    env = dict(os.environ)
    env["PATH"] = f"{fake_bin}{os.pathsep}{env['PATH']}"
    result = subprocess.run(
        ["bash", str(SCRIPT), "--dry-run", "--phase", "host-facts",
         "--receipt-dir", str(receipts)],
        capture_output=True, text=True, env=env,
    )
    assert result.returncode == 0, result.stderr
    # Only the requested phase ran, and it wrote nothing.
    assert list(_printed_receipts(result.stdout)) == ["host-facts"]
    assert not receipts.exists()
    assert not sentinel.exists()


def test_unknown_phase_rejected(tmp_path):
    result = subprocess.run(
        ["bash", str(SCRIPT), "--dry-run", "--phase", "not-a-phase"],
        capture_output=True, text=True,
    )
    assert result.returncode == 2
