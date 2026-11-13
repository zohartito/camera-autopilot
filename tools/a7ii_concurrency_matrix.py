#!/usr/bin/env python3
"""Archive a USB plus Smart Remote Wi-Fi coexistence proof attempt.

This is a dry-run-first orchestrator.  It does not prove the routes work by itself; it creates the
repeatable evidence packet for the claim Zohar cares about: can USB/PTP and Smart Remote Wi-Fi be
reachable during the same powered camera session?
"""

from __future__ import annotations

import argparse
import json
import shlex
import subprocess
import sys
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_OUT_DIR = REPO_ROOT / "artifacts" / "falsification" / "concurrency_matrix"


@dataclass(frozen=True)
class CommandSpec:
    phase: str
    route: str
    title: str
    command: tuple[str, ...]
    artifact_dir: Path

    def to_dict(self) -> dict[str, Any]:
        return {
            "phase": self.phase,
            "route": self.route,
            "title": self.title,
            "command": list(self.command),
            "command_text": command_text(self.command),
            "artifact_dir": str(self.artifact_dir),
        }


@dataclass(frozen=True)
class CommandResult:
    phase: str
    route: str
    title: str
    command: tuple[str, ...]
    artifact_dir: Path
    returncode: int
    stdout: str
    stderr: str
    elapsed_s: float

    def to_dict(self) -> dict[str, Any]:
        return {
            "phase": self.phase,
            "route": self.route,
            "title": self.title,
            "command": list(self.command),
            "command_text": command_text(self.command),
            "artifact_dir": str(self.artifact_dir),
            "returncode": self.returncode,
            "ok": self.returncode == 0,
            "elapsed_s": self.elapsed_s,
        }


CommandRunner = Callable[[CommandSpec], CommandResult]
ParallelRunner = Callable[[list[CommandSpec]], list[CommandResult]]


def command_text(command: tuple[str, ...]) -> str:
    return " ".join(shlex.quote(part) for part in command)


def _stamp() -> str:
    return f"{time.strftime('%Y%m%d-%H%M%S')}-{time.time_ns() % 1_000_000_000:09d}"


def build_plan(
    *,
    out_dir: Path = DEFAULT_OUT_DIR,
    stamp: str | None = None,
    python_bin: str = sys.executable,
    usb_python: str = sys.executable,
    smart_base: str = "http://192.168.122.1:8080/sony",
    smart_timeout: float = 1.5,
    smart_repeat: int = 3,
    usb_state: str = "stills",
) -> dict[str, Any]:
    run_stamp = stamp or _stamp()
    run_dir = out_dir / f"{run_stamp}-usb-wifi-concurrency"
    smart_args = (
        python_bin,
        "tools/falsify_smart_remote_api_surface.py",
        "--base",
        smart_base,
        "--session-unlock",
        "--timeout",
        str(smart_timeout),
        "--repeat",
        str(smart_repeat),
    )
    usb_args = (
        python_bin,
        "tools/a7ii_usb_state_matrix.py",
        "--python",
        usb_python,
        "--state",
        usb_state,
        "--no-prompt",
    )
    specs = [
        CommandSpec(
            phase="baseline_smart_remote",
            route="smart_remote_wifi",
            title="Smart Remote baseline reachability and unlocked API surface",
            command=(
                *smart_args,
                "--out-dir",
                str(run_dir / "baseline_smart_remote"),
            ),
            artifact_dir=run_dir / "baseline_smart_remote",
        ),
        CommandSpec(
            phase="baseline_usb",
            route="usb_gphoto",
            title="USB/gphoto baseline capability probe",
            command=(
                *usb_args,
                "--out-dir",
                str(run_dir / "baseline_usb"),
            ),
            artifact_dir=run_dir / "baseline_usb",
        ),
        CommandSpec(
            phase="concurrent_pair",
            route="smart_remote_wifi",
            title="Smart Remote probe while USB probe is running",
            command=(
                *smart_args,
                "--out-dir",
                str(run_dir / "concurrent_pair" / "smart_remote"),
            ),
            artifact_dir=run_dir / "concurrent_pair" / "smart_remote",
        ),
        CommandSpec(
            phase="concurrent_pair",
            route="usb_gphoto",
            title="USB/gphoto probe while Smart Remote probe is running",
            command=(
                *usb_args,
                "--out-dir",
                str(run_dir / "concurrent_pair" / "usb"),
            ),
            artifact_dir=run_dir / "concurrent_pair" / "usb",
        ),
    ]
    return {
        "tool": "a7ii_concurrency_matrix",
        "timestamp": run_stamp,
        "run_dir": str(run_dir),
        "smart_base": smart_base,
        "smart_timeout": smart_timeout,
        "smart_repeat": smart_repeat,
        "usb_state": usb_state,
        "commands": [spec.to_dict() for spec in specs],
    }


def _run_one(spec: CommandSpec) -> CommandResult:
    start = time.time()
    proc = subprocess.run(
        list(spec.command),
        cwd=REPO_ROOT,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )
    return CommandResult(
        phase=spec.phase,
        route=spec.route,
        title=spec.title,
        command=spec.command,
        artifact_dir=spec.artifact_dir,
        returncode=proc.returncode,
        stdout=proc.stdout,
        stderr=proc.stderr,
        elapsed_s=round(time.time() - start, 3),
    )


def _run_parallel(specs: list[CommandSpec]) -> list[CommandResult]:
    started = []
    for spec in specs:
        proc = subprocess.Popen(
            list(spec.command),
            cwd=REPO_ROOT,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        started.append((spec, time.time(), proc))
    results = []
    for spec, start, proc in started:
        stdout, stderr = proc.communicate()
        results.append(
            CommandResult(
                phase=spec.phase,
                route=spec.route,
                title=spec.title,
                command=spec.command,
                artifact_dir=spec.artifact_dir,
                returncode=proc.returncode,
                stdout=stdout or "",
                stderr=stderr or "",
                elapsed_s=round(time.time() - start, 3),
            )
        )
    return results


def _write_result(run_dir: Path, result: CommandResult) -> dict[str, Any]:
    result_dir = run_dir / "command-results" / result.phase / result.route
    result_dir.mkdir(parents=True, exist_ok=True)
    (result_dir / "stdout.txt").write_text(result.stdout, encoding="utf-8")
    (result_dir / "stderr.txt").write_text(result.stderr, encoding="utf-8")
    (result_dir / "exit_code.txt").write_text(f"{result.returncode}\n", encoding="utf-8")
    payload = result.to_dict()
    payload.update(
        {
            "stdout_path": str(result_dir / "stdout.txt"),
            "stderr_path": str(result_dir / "stderr.txt"),
            "exit_code_path": str(result_dir / "exit_code.txt"),
        }
    )
    (result_dir / "result.json").write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")
    return payload


def _classification(results: list[dict[str, Any]]) -> str:
    by_phase_route = {(item["phase"], item["route"]): item["ok"] for item in results}
    baseline_smart = by_phase_route.get(("baseline_smart_remote", "smart_remote_wifi"), False)
    baseline_usb = by_phase_route.get(("baseline_usb", "usb_gphoto"), False)
    concurrent_smart = by_phase_route.get(("concurrent_pair", "smart_remote_wifi"), False)
    concurrent_usb = by_phase_route.get(("concurrent_pair", "usb_gphoto"), False)
    if concurrent_smart and concurrent_usb:
        return "proven_concurrent_success"
    if baseline_smart and baseline_usb:
        return "separate_success_concurrent_failed"
    if baseline_smart or baseline_usb or concurrent_smart or concurrent_usb:
        return "partial_route_reachable"
    return "needs_camera_state_or_network_setup"


def _markdown(report: dict[str, Any]) -> str:
    lines = [
        "# A7 II USB + Wi-Fi Concurrency Matrix",
        "",
        f"- execute: `{report['execute']}`",
        f"- allow_hardware: `{report['allow_hardware']}`",
        f"- classification: `{report['classification']}`",
        f"- run_dir: `{report['run_dir']}`",
        "",
        "## Commands",
        "",
    ]
    for command in report["commands"]:
        lines.append(f"### {command['phase']} / {command['route']}")
        lines.append("")
        lines.append(f"- title: `{command['title']}`")
        lines.append(f"- artifact_dir: `{command['artifact_dir']}`")
        if "returncode" in command:
            lines.append(f"- returncode: `{command['returncode']}`")
            lines.append(f"- ok: `{command['ok']}`")
            lines.append(f"- elapsed_s: `{command['elapsed_s']}`")
        lines.append("")
        lines.append("```bash")
        lines.append(command["command_text"])
        lines.append("```")
        lines.append("")
    lines.extend(
        [
            "## Interpretation Rules",
            "",
            "- `proven_concurrent_success` means both Smart Remote and USB probes exited 0 in the concurrent pair.",
            "- Any failure is evidence about the current camera/network state, not PROVEN-NO, until repeated across controlled states.",
            "- Save this folder with the matching Smart Remote/USB setup notes before promoting a route recommendation.",
            "",
        ]
    )
    return "\n".join(lines)


def run_matrix(
    *,
    execute: bool = False,
    allow_hardware: bool = False,
    out_dir: Path = DEFAULT_OUT_DIR,
    stamp: str | None = None,
    python_bin: str = sys.executable,
    usb_python: str = sys.executable,
    smart_base: str = "http://192.168.122.1:8080/sony",
    smart_timeout: float = 1.5,
    smart_repeat: int = 3,
    usb_state: str = "stills",
    command_runner: CommandRunner | None = None,
    parallel_runner: ParallelRunner | None = None,
) -> dict[str, Any]:
    if execute and not allow_hardware:
        raise ValueError("--execute requires --allow-hardware because this touches live camera routes")
    plan = build_plan(
        out_dir=out_dir,
        stamp=stamp,
        python_bin=python_bin,
        usb_python=usb_python,
        smart_base=smart_base,
        smart_timeout=smart_timeout,
        smart_repeat=smart_repeat,
        usb_state=usb_state,
    )
    report = {
        **plan,
        "execute": execute,
        "allow_hardware": allow_hardware,
        "classification": "planned",
        "results": [],
        "summary_path": None,
        "report_path": None,
    }
    if not execute:
        return report

    run_dir = Path(plan["run_dir"])
    run_dir.mkdir(parents=True, exist_ok=True)
    specs = [
        CommandSpec(
            phase=item["phase"],
            route=item["route"],
            title=item["title"],
            command=tuple(item["command"]),
            artifact_dir=Path(item["artifact_dir"]),
        )
        for item in plan["commands"]
    ]
    one = command_runner or _run_one
    parallel = parallel_runner or _run_parallel
    baseline_results = [one(specs[0]), one(specs[1])]
    concurrent_results = parallel(specs[2:])
    result_payloads = [_write_result(run_dir, result) for result in [*baseline_results, *concurrent_results]]
    report["results"] = result_payloads
    report["commands"] = result_payloads
    report["classification"] = _classification(result_payloads)
    report_path = run_dir / "concurrency-matrix.json"
    summary_path = run_dir / "summary.md"
    report["report_path"] = str(report_path)
    report["summary_path"] = str(summary_path)
    report_path.write_text(json.dumps(report, indent=2, sort_keys=True), encoding="utf-8")
    summary_path.write_text(_markdown(report), encoding="utf-8")
    return report


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--execute", action="store_true")
    parser.add_argument("--allow-hardware", action="store_true")
    parser.add_argument("--out-dir", default=str(DEFAULT_OUT_DIR))
    parser.add_argument("--python", default=sys.executable)
    parser.add_argument("--usb-python", default=sys.executable)
    parser.add_argument("--smart-base", default="http://192.168.122.1:8080/sony")
    parser.add_argument("--smart-timeout", type=float, default=1.5)
    parser.add_argument("--smart-repeat", type=int, default=3)
    parser.add_argument("--usb-state", default="stills")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)
    try:
        report = run_matrix(
            execute=args.execute,
            allow_hardware=args.allow_hardware,
            out_dir=Path(args.out_dir),
            python_bin=args.python,
            usb_python=args.usb_python,
            smart_base=args.smart_base,
            smart_timeout=args.smart_timeout,
            smart_repeat=args.smart_repeat,
            usb_state=args.usb_state,
        )
    except ValueError as exc:
        parser.error(str(exc))
    if args.json or not args.execute:
        print(json.dumps(report, indent=2, sort_keys=True))
    else:
        print(report["summary_path"])
    if not args.execute:
        return 0
    return 0 if report["classification"] == "proven_concurrent_success" else 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
