#!/usr/bin/env python3
"""Archive gphoto capability probes across named A7 II camera states.

This wraps tests/probe_capabilities.py. The wrapper exists because greyed-out menu items often
become writable only after a state change: still/movie mode, AF/MF, lens/adapter, Smart Remote exit,
or USB mode. Each state gets its own stdout/stderr/exit artifact.
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
import time
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
WATCH_PRESENT_RE = re.compile(
    r"\[PRESENT\]\s+([A-Za-z0-9_-]+)\s+([A-Za-z -]+?)\s*=\s*(.*?)\s+\("
)
WATCH_ABSENT_RE = re.compile(r"\[ABSENT\s*\]\s+([A-Za-z0-9_-]+)\b")


STATE_HINTS = {
    "stills": "Set mode dial to a still mode such as M/A/S/P; USB Connection -> PC Remote.",
    "movie_capable": "Enter a movie-capable state or movie mode; keep USB Connection -> PC Remote.",
    "manual_focus": "Set lens/body focus to MF or DMF; keep USB Connection -> PC Remote.",
    "autofocus": "Set focus mode to AF-S/AF-C and a normal AF area; keep USB Connection -> PC Remote.",
    "smart_remote_exited": "Exit Smart Remote on camera, reconnect USB PC Remote, then run this state.",
}


def _default_python() -> str:
    venv_python = REPO_ROOT / ".venv" / "bin" / "python"
    if venv_python.exists():
        return str(venv_python)
    return sys.executable


def _slug(value: str) -> str:
    return re.sub(r"[^A-Za-z0-9_.-]+", "_", value).strip("_") or "state"


def _parse_watch(stdout: str) -> dict[str, dict[str, str]]:
    found: dict[str, dict[str, str]] = {}
    for line in stdout.splitlines():
        present = WATCH_PRESENT_RE.search(line)
        if present:
            name, flag, value = present.groups()
            found[name] = {
                "presence": "PRESENT",
                "writability": flag.strip(),
                "value": value.strip().strip("'"),
            }
            continue
        absent = WATCH_ABSENT_RE.search(line)
        if absent:
            found[absent.group(1)] = {
                "presence": "ABSENT",
                "writability": "",
                "value": "",
            }
    return found


def _prompt_for_state(state: str, no_prompt: bool) -> None:
    hint = STATE_HINTS.get(state, "Put the camera in the requested state.")
    print(f"\n== Prepare state: {state} ==")
    print(hint)
    if no_prompt or not sys.stdin.isatty():
        return
    input("Press Enter when the camera is ready...")


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--state",
        action="append",
        default=[],
        help="state label to archive; repeatable. Defaults to stills.",
    )
    parser.add_argument("--out-dir", default="artifacts/falsification/r2_usb_state_matrix")
    parser.add_argument("--python", default=_default_python())
    parser.add_argument("--no-prompt", action="store_true")
    parser.add_argument("--dry-run", action="store_true", help="print planned per-state commands only")
    args = parser.parse_args(argv)

    states = args.state or ["stills"]
    stamp = time.strftime("%Y%m%d-%H%M%S")
    run_dir = (REPO_ROOT / args.out_dir / f"{stamp}-usb-state-matrix").resolve()
    command = [args.python, "tests/probe_capabilities.py"]

    if args.dry_run:
        print(
            json.dumps(
                {
                    "run_dir": str(run_dir),
                    "states": states,
                    "command": command,
                    "state_hints": {state: STATE_HINTS.get(state, "") for state in states},
                },
                indent=2,
                sort_keys=True,
            )
        )
        return 0

    run_dir.mkdir(parents=True, exist_ok=True)
    summary = {
        "timestamp": stamp,
        "run_dir": str(run_dir),
        "command": command,
        "states": {},
    }

    exit_failed = 0
    for state in states:
        _prompt_for_state(state, args.no_prompt)
        state_dir = run_dir / _slug(state)
        state_dir.mkdir(parents=True, exist_ok=True)
        started = time.time()
        proc = subprocess.run(
            command,
            cwd=REPO_ROOT,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
        )
        elapsed = round(time.time() - started, 3)
        (state_dir / "stdout.txt").write_text(proc.stdout, encoding="utf-8")
        (state_dir / "stderr.txt").write_text(proc.stderr, encoding="utf-8")
        (state_dir / "exit_code.txt").write_text(f"{proc.returncode}\n", encoding="utf-8")
        watch = _parse_watch(proc.stdout)
        (state_dir / "watch-nodes.json").write_text(
            json.dumps(watch, indent=2, sort_keys=True),
            encoding="utf-8",
        )
        summary["states"][state] = {
            "artifact_dir": str(state_dir),
            "exit_code": proc.returncode,
            "elapsed_s": elapsed,
            "watch_nodes": watch,
        }
        if proc.returncode != 0:
            exit_failed = 1

    (run_dir / "summary.json").write_text(json.dumps(summary, indent=2, sort_keys=True), encoding="utf-8")
    lines = ["# A7 II USB State Matrix", "", f"Run: `{run_dir}`", ""]
    for state, result in summary["states"].items():
        lines.append(f"## {state}")
        lines.append("")
        lines.append(f"- exit_code: `{result['exit_code']}`")
        lines.append(f"- artifact_dir: `{result['artifact_dir']}`")
        for node, node_result in sorted(result["watch_nodes"].items()):
            lines.append(
                f"- `{node}`: {node_result['presence']} {node_result['writability']} "
                f"value={node_result['value']}"
            )
        lines.append("")
    (run_dir / "summary.md").write_text("\n".join(lines).rstrip() + "\n", encoding="utf-8")
    print(run_dir)
    return exit_failed


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
