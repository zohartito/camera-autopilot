"""Hardware-free contract tests for the unattended PC taste-index pipeline."""

from __future__ import annotations

import os
import subprocess
import tempfile
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent
SCRIPT = ROOT / "backend" / "pc_overnight.sh"


def _dry_run(source: str) -> subprocess.CompletedProcess[str]:
    environment = os.environ.copy()
    environment.update(
        {
            "PIPELINE_DRY_RUN": "1",
            "HARVEST_SOURCE": source,
            "HARVEST_PAGES": "123",
            "CAM_VENV": "/tmp/camera-autopilot-missing-venv",
        }
    )
    return subprocess.run(
        ["bash", str(SCRIPT)],
        cwd=ROOT,
        env=environment,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
    )


def test_shell_syntax() -> None:
    result = subprocess.run(["bash", "-n", str(SCRIPT)], text=True, capture_output=True)
    assert result.returncode == 0, result.stderr


def test_broad_is_the_default_roadmap_lane() -> None:
    environment = os.environ.copy()
    environment.update({"PIPELINE_DRY_RUN": "1", "CAM_VENV": "/tmp/missing"})
    environment.pop("HARVEST_SOURCE", None)
    result = subprocess.run(
        ["bash", str(SCRIPT)],
        cwd=ROOT,
        env=environment,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
    )
    assert result.returncode == 0, result.stdout
    assert "source=broad" in result.stdout, result.stdout
    assert "harvest_wikimedia_broad.py" in result.stdout, result.stdout
    assert "check_taste_index.py" in result.stdout, result.stdout
    assert "--no-manifest" in result.stdout, result.stdout


def test_qi_lane_remains_available() -> None:
    result = _dry_run("qi")
    assert result.returncode == 0, result.stdout
    assert "source=qi" in result.stdout, result.stdout
    assert "harvest_wikimedia.py" in result.stdout, result.stdout
    assert "wikimedia_qi.csv" in result.stdout, result.stdout


def test_unknown_source_fails_before_preflight() -> None:
    result = _dry_run("flickr")
    assert result.returncode == 2, result.stdout
    assert "must be 'broad' or 'qi'" in result.stdout, result.stdout


def test_dry_run_does_not_create_pipeline_outputs() -> None:
    with tempfile.TemporaryDirectory() as directory:
        environment = os.environ.copy()
        environment.update(
            {
                "PIPELINE_DRY_RUN": "1",
                "HARVEST_SOURCE": "broad",
                "CAM_VENV": "/tmp/missing",
                "PIPELINE_AUDIT_REPORT": str(Path(directory) / "audit.json"),
            }
        )
        result = subprocess.run(
            ["bash", str(SCRIPT)],
            cwd=ROOT,
            env=environment,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
        )
        assert result.returncode == 0, result.stdout
        assert not Path(environment["PIPELINE_AUDIT_REPORT"]).exists()


def _main() -> int:
    tests = (
        test_shell_syntax,
        test_broad_is_the_default_roadmap_lane,
        test_qi_lane_remains_available,
        test_unknown_source_fails_before_preflight,
        test_dry_run_does_not_create_pipeline_outputs,
    )
    failures = 0
    for test in tests:
        try:
            test()
            print(f"  ok   {test.__name__}")
        except Exception as exc:  # noqa: BLE001 - standalone test runner
            failures += 1
            print(f"  FAIL {test.__name__}: {exc}")
    print(f"{len(tests) - failures}/{len(tests)} pc_overnight tests passed")
    return failures


if __name__ == "__main__":
    raise SystemExit(_main())
