"""Camera-free checks for the USB plus Smart Remote concurrency proof runner."""

from __future__ import annotations

import sys
import tempfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "tools"))

import a7ii_concurrency_matrix


def _result(spec: a7ii_concurrency_matrix.CommandSpec, returncode: int = 0) -> a7ii_concurrency_matrix.CommandResult:
    return a7ii_concurrency_matrix.CommandResult(
        phase=spec.phase,
        route=spec.route,
        title=spec.title,
        command=spec.command,
        artifact_dir=spec.artifact_dir,
        returncode=returncode,
        stdout=f"{spec.route} ok\n",
        stderr="",
        elapsed_s=0.01,
    )


def test_concurrency_matrix_dry_run_plans_exact_route_commands() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        report = a7ii_concurrency_matrix.run_matrix(
            out_dir=Path(tmp),
            stamp="test",
            python_bin="python3",
            usb_python=".venv/bin/python",
        )

    assert report["execute"] is False
    assert report["classification"] == "planned"
    assert len(report["commands"]) == 4
    command_texts = [item["command_text"] for item in report["commands"]]
    assert any("falsify_smart_remote_api_surface.py" in command for command in command_texts)
    assert all("--repeat 3" in command for command in command_texts if "falsify_smart_remote_api_surface.py" in command)
    assert any("a7ii_usb_state_matrix.py" in command for command in command_texts)
    assert any("concurrent_pair/smart_remote" in command for command in command_texts)
    assert any("concurrent_pair/usb" in command for command in command_texts)
    print("  concurrency matrix dry-run plan: ok")


def test_concurrency_matrix_requires_hardware_approval() -> None:
    try:
        a7ii_concurrency_matrix.run_matrix(execute=True)
    except ValueError as exc:
        assert "--allow-hardware" in str(exc)
    else:
        raise AssertionError("live concurrency matrix must require hardware approval")
    print("  concurrency matrix approval guard: ok")


def test_concurrency_matrix_writes_successful_fake_run() -> None:
    calls: list[tuple[str, str]] = []

    def fake_runner(spec: a7ii_concurrency_matrix.CommandSpec) -> a7ii_concurrency_matrix.CommandResult:
        calls.append((spec.phase, spec.route))
        return _result(spec)

    def fake_parallel(specs: list[a7ii_concurrency_matrix.CommandSpec]) -> list[a7ii_concurrency_matrix.CommandResult]:
        calls.extend((spec.phase, spec.route) for spec in specs)
        return [_result(spec) for spec in specs]

    with tempfile.TemporaryDirectory() as tmp:
        report = a7ii_concurrency_matrix.run_matrix(
            execute=True,
            allow_hardware=True,
            out_dir=Path(tmp),
            stamp="test",
            python_bin="python3",
            usb_python=".venv/bin/python",
            command_runner=fake_runner,
            parallel_runner=fake_parallel,
        )

        assert Path(report["report_path"]).exists()
        assert Path(report["summary_path"]).exists()
        assert all(Path(item["stdout_path"]).exists() for item in report["results"])

    assert report["classification"] == "proven_concurrent_success"
    assert calls == [
        ("baseline_smart_remote", "smart_remote_wifi"),
        ("baseline_usb", "usb_gphoto"),
        ("concurrent_pair", "smart_remote_wifi"),
        ("concurrent_pair", "usb_gphoto"),
    ]
    print("  concurrency matrix fake successful run: ok")


if __name__ == "__main__":
    test_concurrency_matrix_dry_run_plans_exact_route_commands()
    test_concurrency_matrix_requires_hardware_approval()
    test_concurrency_matrix_writes_successful_fake_run()
    print("a7ii_concurrency_matrix tests: PASS")
