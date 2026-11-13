"""Compile and run JpegGate on the desktop JVM. No Gradle, device, or photos."""

from __future__ import annotations

import shutil
import subprocess
import tempfile
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
GATE = ROOT / "mobile" / "android" / "app" / "src" / "main" / "java" / "com" / "zohar" / "autopilot" / "JpegGate.java"
ASSERTIONS = ROOT / "mobile" / "android" / "app" / "src" / "test" / "java" / "com" / "zohar" / "autopilot" / "JpegGateAssertions.java"


def _usable(tool: str) -> str | None:
    """Return the tool's path only if it actually runs.

    macOS ships stubs at /usr/bin/java and /usr/bin/javac that exist with no JDK installed
    and fail at exec time with "Unable to locate a Java Runtime". `shutil.which` finds them,
    so a presence check skips nothing and this test fails on a Mac while passing on Linux CI.
    """
    path = shutil.which(tool)
    if path is None:
        return None
    try:
        probe = subprocess.run([path, "-version"], check=False, capture_output=True, text=True)
    except OSError:
        return None
    return path if probe.returncode == 0 else None


def test_jpeg_gate_jvm() -> None:
    javac = _usable("javac")
    java = _usable("java")
    if javac is None or java is None:
        pytest.skip("no working JDK; JVM JPEG gates are not scored here")
    with tempfile.TemporaryDirectory() as tmp:
        compile_run = subprocess.run(
            [javac, "-d", tmp, str(GATE), str(ASSERTIONS)],
            check=False,
            capture_output=True,
            text=True,
        )
        assert compile_run.returncode == 0, compile_run.stderr
        run = subprocess.run(
            [java, "-cp", tmp, "com.zohar.autopilot.JpegGateAssertions"],
            check=False,
            capture_output=True,
            text=True,
        )
        assert run.returncode == 0, run.stdout + run.stderr
        assert "PASS: JpegGate JVM assertions" in run.stdout
