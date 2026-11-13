#!/usr/bin/env python3
"""Map guided physical/admin gate proof artifacts back to registry rows."""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parent.parent
BACKEND = REPO_ROOT / "backend"
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))

import settings_registry


def _repo_path(path: Path) -> str:
    try:
        return str(path.resolve().relative_to(REPO_ROOT))
    except ValueError:
        return str(path)


def read_json(path: Path) -> dict[str, Any] | None:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return payload if isinstance(payload, dict) else None


def iter_physical_proofs(source_dir: Path) -> list[tuple[Path, dict[str, Any]]]:
    return [
        (path, payload)
        for path in sorted(source_dir.rglob("physical-gate.json"))
        if (payload := read_json(path)) is not None
    ]


def _proof_quality(proof: dict[str, Any]) -> bool:
    quality = proof.get("evidence_quality") or {}
    if not quality.get("has_observed_state"):
        return False
    if proof.get("admin_guarded") and not proof.get("admin_observation_only"):
        return False
    if proof.get("destructive_action_performed"):
        return False
    return True


def _latest_proofs_by_setting(
    proofs: list[tuple[Path, dict[str, Any]]],
) -> dict[str, list[tuple[Path, dict[str, Any]]]]:
    by_setting: dict[str, list[tuple[Path, dict[str, Any]]]] = {}
    for path, proof in proofs:
        setting_id = proof.get("setting_id")
        if isinstance(setting_id, str):
            by_setting.setdefault(setting_id, []).append((path, proof))
    return by_setting


def _reviewed_rows(registry: dict[str, Any]) -> list[dict[str, Any]]:
    return [
        row
        for row in registry["settings"]
        if row["status"] in {"manual_physical_gate", "state_gated", "dangerous_admin"}
        or "guided_physical" in row.get("routes", {})
    ]


def map_registry(
    proofs: list[tuple[Path, dict[str, Any]]],
    *,
    registry: dict[str, Any] | None = None,
) -> list[dict[str, Any]]:
    registry = registry or settings_registry.load_registry()
    by_setting = _latest_proofs_by_setting(proofs)
    mapped = []
    for row in _reviewed_rows(registry):
        row_proofs = by_setting.get(row["setting_id"], [])
        proof_summaries = []
        for path, proof in row_proofs:
            proof_summaries.append(
                {
                    "artifact": _repo_path(path),
                    "verdict": proof.get("verdict"),
                    "gate_class": proof.get("gate_class"),
                    "observed_state": proof.get("observed_state"),
                    "admin_guarded": proof.get("admin_guarded") is True,
                    "admin_observation_only": proof.get("admin_observation_only") is True,
                    "destructive_action_performed": proof.get("destructive_action_performed") is True,
                    "copied_evidence": proof.get("copied_evidence") or [],
                    "quality_ok": _proof_quality(proof),
                }
            )
        if any(item["quality_ok"] for item in proof_summaries):
            verdict = proof_summaries[-1]["verdict"] or "physical_gate_observed"
        else:
            verdict = "needs_physical_gate_observation"
        mapped.append(
            {
                "setting_id": row["setting_id"],
                "label": row["label"],
                "registry_status": row["status"],
                "route_status": row.get("routes", {}).get("guided_physical", {}).get("status"),
                "routes": row.get("routes", {}),
                "verdict": verdict,
                "evidence_count": len(proof_summaries),
                "manual_proof_artifacts": proof_summaries,
                "gate_class": proof_summaries[-1]["gate_class"] if proof_summaries else None,
                "observed_state": proof_summaries[-1]["observed_state"] if proof_summaries else None,
                "admin_guarded": row["status"] == "dangerous_admin"
                or any(route.get("status") == "dangerous_admin" for route in row.get("routes", {}).values()),
                "control_surface": "guided_physical_or_admin_observation",
                "api_exposure": "not_software_control_proof",
                "next_action": row["next_action"],
            }
        )
    return mapped


def build_report(
    source_dir: Path,
    *,
    out_dir: Path | None = None,
    registry: dict[str, Any] | None = None,
) -> tuple[dict[str, Any], Path]:
    source_dir = source_dir.expanduser().resolve()
    if not source_dir.exists():
        raise FileNotFoundError(f"Physical gate proof directory not found: {source_dir}")

    out_dir = out_dir or REPO_ROOT / "artifacts" / "falsification" / "manual_gates_review"
    out_dir.mkdir(parents=True, exist_ok=True)
    stamp = time.strftime("%Y%m%d-%H%M%S")
    report_path = out_dir / f"{stamp}-physical-gate-review.json"

    proofs = iter_physical_proofs(source_dir)
    mapping = map_registry(proofs, registry=registry)
    observed_rows = [row for row in mapping if row["verdict"] != "needs_physical_gate_observation"]
    admin_rows = [row for row in observed_rows if row["admin_guarded"]]

    report = {
        "generated_at": stamp,
        "source_dir": str(source_dir),
        "artifact_summary": {
            "physical_gate_proof_count": len(proofs),
            "proof_setting_ids": sorted({proof.get("setting_id") for _, proof in proofs if proof.get("setting_id")}),
            "quality_ok_count": sum(1 for _, proof in proofs if _proof_quality(proof)),
        },
        "registry_mapping_summary": {
            "guided_physical_rows": len(mapping),
            "physical_or_admin_observed_rows": len(observed_rows),
            "admin_observed_no_action_rows": len(admin_rows),
            "needs_physical_gate_observation_rows": len(mapping) - len(observed_rows),
        },
        "registry_mapping": mapping,
    }
    report_path.write_text(json.dumps(report, indent=2, sort_keys=True), encoding="utf-8")
    return report, report_path


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "source_dir",
        nargs="?",
        default="artifacts/falsification/manual_gates",
        help="directory containing physical-gate.json artifacts",
    )
    parser.add_argument(
        "--out-dir",
        default="artifacts/falsification/manual_gates_review",
        help="directory for the registry-mapped physical gate review report",
    )
    args = parser.parse_args(argv)

    report, report_path = build_report(Path(args.source_dir), out_dir=Path(args.out_dir))
    print(
        json.dumps(
            {
                "report": str(report_path),
                "artifact_summary": report["artifact_summary"],
                "registry_mapping_summary": report["registry_mapping_summary"],
            },
            indent=2,
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
