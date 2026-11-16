"""CAM-32: the retired installer must not create an unpinned home environment."""
import os
from pathlib import Path
import subprocess


def test_legacy_gpu_setup_refuses_without_installing(tmp_path):
    root = Path(__file__).resolve().parents[1]
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    sentinel = tmp_path / "python-called"
    python = fake_bin / "python3"
    python.write_text(f"#!/bin/sh\nprintf called > '{sentinel}'\nexit 99\n")
    python.chmod(0o755)
    home = tmp_path / "home"
    home.mkdir()
    env = {**os.environ, "HOME": str(home), "PATH": str(fake_bin) + os.pathsep + os.environ["PATH"]}
    result = subprocess.run(["bash", str(root / "setup_venv.sh")], env=env, capture_output=True, text=True)
    assert not sentinel.exists(), "legacy setup attempted to create an environment"
    assert result.returncode == 2
    assert "retired" in result.stderr and "README.md" in result.stderr
    assert "requirements-test.lock" in result.stderr
    assert not (home / "cam-venv").exists()
