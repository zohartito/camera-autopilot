#!/usr/bin/env python3
"""Capture OpenMemories rollback proof that developer/proof ports are closed.

This tool only attempts short TCP connects to the camera IP and records whether Telnet, ADB, and
the proof-app HTTP port are open. It does not authenticate, send commands, or change camera state.
Run it after uninstalling/stopping the proof app and disabling developer ports.
"""

from __future__ import annotations

import argparse
import json
import os
import socket
import sys
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_HOST = os.environ.get("CAMERA_LAN_HOST", "").strip()
DEFAULT_PORTS = (23, 5555, 8080)
REQUIRED_PORTS = frozenset(DEFAULT_PORTS)
DEFAULT_OUT_DIR = REPO_ROOT / "artifacts" / "falsification" / "r3_openmemories"
PORT_NAMES = {23: "telnet", 5555: "adb", 8080: "proof_http"}
Connector = Callable[[tuple[str, int], float], Any]


def _stamp() -> str:
    return f"{time.strftime('%Y%m%d-%H%M%S')}-{time.time_ns() % 1_000_000_000:09d}"


def _repo_path(path: Path) -> str:
    try:
        return str(path.resolve().relative_to(REPO_ROOT))
    except ValueError:
        return str(path)


def _connect(host: str, port: int, timeout: float, connector: Connector) -> dict[str, Any]:
    started = time.time()
    try:
        conn = connector((host, port), timeout)
        close = getattr(conn, "close", None)
        if callable(close):
            close()
        return {
            "host": host,
            "port": port,
            "name": PORT_NAMES.get(port, "port"),
            "open": True,
            "error": None,
            "elapsed_s": round(time.time() - started, 3),
        }
    except OSError as exc:
        return {
            "host": host,
            "port": port,
            "name": PORT_NAMES.get(port, "port"),
            "open": False,
            "error": f"{exc.__class__.__name__}: {exc}",
            "elapsed_s": round(time.time() - started, 3),
        }


def capture_ports(
    *,
    host: str = DEFAULT_HOST,
    ports: tuple[int, ...] = DEFAULT_PORTS,
    timeout: float = 1.0,
    out_dir: Path = DEFAULT_OUT_DIR,
    network_context_proven: bool = False,
    connector: Connector = socket.create_connection,
) -> tuple[dict[str, Any], Path]:
    if not host:
        raise ValueError("camera host is required")
    run_dir = out_dir / f"{_stamp()}-openmemories-rollback-ports"
    run_dir.mkdir(parents=True, exist_ok=True)
    results = [_connect(host, int(port), timeout, connector) for port in ports]
    open_ports = [item for item in results if item["open"]]
    closed_ports = [item for item in results if not item["open"]]
    scanned_ports = [int(item["port"]) for item in results]
    scanned_port_set = set(scanned_ports)
    missing_required_ports = sorted(REQUIRED_PORTS - scanned_port_set)
    unexpected_ports = sorted(scanned_port_set - REQUIRED_PORTS)
    complete_required_scan = (
        len(scanned_ports) == len(REQUIRED_PORTS)
        and scanned_port_set == REQUIRED_PORTS
    )
    rollback_closed_evidence = (
        network_context_proven
        and complete_required_scan
        and not open_ports
        and len(closed_ports) == len(REQUIRED_PORTS)
    )
    report = {
        "tool": "prove_openmemories_rollback_ports",
        "generated_at": time.strftime("%Y%m%d-%H%M%S"),
        "host": host,
        "network_context_proven": network_context_proven,
        "required_ports": sorted(REQUIRED_PORTS),
        "scanned_ports": scanned_ports,
        "missing_required_ports": missing_required_ports,
        "unexpected_ports": unexpected_ports,
        "complete_required_scan": complete_required_scan,
        "ports": results,
        "open_ports": open_ports,
        "closed_ports": closed_ports,
        "rollback_closed_evidence": rollback_closed_evidence,
        "policy": {
            "sends_commands": False,
            "authenticates": False,
            "changes_camera_state": False,
            "tcp_connect_only": True,
        },
        "interpretation": (
            "rollback closed: all required developer/proof ports explicitly refused TCP connections"
            if rollback_closed_evidence
            else "rollback unproven: network context was not proven for the same camera/session"
            if not network_context_proven
            else "rollback unproven: the exact required developer/proof port set was not scanned"
            if not complete_required_scan
            else "rollback incomplete: one or more developer/proof ports accepted TCP connections"
        ),
    }
    report_path = run_dir / "openmemories-rollback-ports.json"
    report["artifact"] = _repo_path(report_path)
    report_path.write_text(json.dumps(report, indent=2, sort_keys=True), encoding="utf-8")
    return report, report_path


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--host",
        default=DEFAULT_HOST,
        help="camera LAN host; defaults to CAMERA_LAN_HOST",
    )
    parser.add_argument("--port", type=int, action="append", default=[], help="port to scan; repeatable")
    parser.add_argument("--timeout", type=float, default=1.0)
    parser.add_argument("--out-dir", default=str(DEFAULT_OUT_DIR))
    parser.add_argument(
        "--network-context-proven",
        action="store_true",
        help="operator has saved same-session network evidence proving this host is the camera before rollback scan",
    )
    args = parser.parse_args(argv)
    if not args.host:
        parser.error("--host is required unless CAMERA_LAN_HOST is set")
    ports = tuple(args.port) if args.port else DEFAULT_PORTS
    report, report_path = capture_ports(
        host=args.host,
        ports=ports,
        timeout=max(0.1, args.timeout),
        out_dir=Path(args.out_dir),
        network_context_proven=args.network_context_proven,
    )
    print(
        json.dumps(
            {
                "report": str(report_path),
                "rollback_closed_evidence": report["rollback_closed_evidence"],
                "network_context_proven": report["network_context_proven"],
                "complete_required_scan": report["complete_required_scan"],
                "missing_required_ports": report["missing_required_ports"],
                "unexpected_ports": report["unexpected_ports"],
                "open_ports": report["open_ports"],
                "closed_ports": len(report["closed_ports"]),
            },
            indent=2,
            sort_keys=True,
        )
    )
    return 0 if report["rollback_closed_evidence"] else 2


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
