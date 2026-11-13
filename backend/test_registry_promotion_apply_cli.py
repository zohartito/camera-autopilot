"""Command-line guard tests for registry promotion application."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
CLI = REPO_ROOT / "tools" / "a7ii_apply_registry_promotions.py"


def _run_cli(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(CLI), *args],
        cwd=REPO_ROOT,
        text=True,
        capture_output=True,
        check=False,
    )


def test_apply_cli_stays_dry_run_by_default() -> None:
    result = _run_cli("--route", "usb_gphoto", "--json")
    assert result.returncode == 0, result.stderr
    assert '"dry_run": true' in result.stdout
    print("  promotion apply CLI dry-run default: ok")


def test_apply_cli_requires_explicit_write_target() -> None:
    result = _run_cli("--apply", "--route", "usb_gphoto")
    assert result.returncode != 0
    assert "--out PATH" in result.stderr
    assert "--in-place" in result.stderr
    print("  promotion apply CLI explicit write target guard: ok")


def test_apply_cli_rejects_ambiguous_write_targets() -> None:
    result = _run_cli("--apply", "--out", "artifacts/tmp-registry.json", "--in-place")
    assert result.returncode != 0
    assert "mutually exclusive" in result.stderr
    print("  promotion apply CLI ambiguous target guard: ok")


if __name__ == "__main__":
    test_apply_cli_stays_dry_run_by_default()
    test_apply_cli_requires_explicit_write_target()
    test_apply_cli_rejects_ambiguous_write_targets()
    print("registry_promotion_apply_cli tests: PASS")
