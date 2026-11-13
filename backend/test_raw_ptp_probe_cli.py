"""Camera-free checks for the raw PTP probe CLI contract."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
PROBE = REPO_ROOT / "tests" / "probe_raw_control.py"


def _run(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(PROBE), *args],
        cwd=REPO_ROOT,
        text=True,
        capture_output=True,
        check=False,
    )


def test_raw_ptp_probe_help_is_pyusb_free_and_matches_behavior_scripts() -> None:
    result = _run("--help")
    assert result.returncode == 0, result.stderr
    assert "--focus-area" in result.stdout
    assert "--af-x" in result.stdout
    assert "--af-y" in result.stdout
    assert "--mf-step" in result.stdout
    assert "--no-return" in result.stdout
    assert "pyusb is required" not in result.stderr
    print("  raw PTP probe --help stays pyusb-free: ok")


def test_raw_ptp_probe_rejects_bad_focus_area_before_pyusb_import() -> None:
    result = _run("--w1", "--focus-area", "not-a-number")
    assert result.returncode == 2
    assert "expected integer or 0x-prefixed integer" in result.stderr
    assert "pyusb is required" not in result.stderr
    print("  raw PTP probe parser rejects bad focus-area before hardware import: ok")


if __name__ == "__main__":
    test_raw_ptp_probe_help_is_pyusb_free_and_matches_behavior_scripts()
    test_raw_ptp_probe_rejects_bad_focus_area_before_pyusb_import()
    print("raw_ptp_probe_cli tests: PASS")
