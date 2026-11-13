#!/usr/bin/env python3
"""Map Smart Remote phone-app preference proof artifacts back to registry rows."""

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


def iter_phone_proofs(source_dir: Path) -> list[tuple[Path, dict[str, Any]]]:
    return [
        (path, payload)
        for path in sorted(source_dir.rglob("phone-preference-proof.json"))
        if (payload := read_json(path)) is not None
    ]


def route_rows(registry: dict[str, Any] | None = None) -> list[dict[str, Any]]:
    registry = registry or settings_registry.load_registry()
    rows = []
    for row in registry["settings"]:
        if row.get("menu_group") == "smart_remote_phone" or "smart_remote_phone" in row.get("routes", {}):
            rows.append(row)
    return rows


def _proof_quality(proof: dict[str, Any]) -> bool:
    quality = proof.get("evidence_quality") or {}
    return bool(quality.get("has_observed_value") and quality.get("has_phone_state"))


def map_registry(
    proofs: list[tuple[Path, dict[str, Any]]],
    *,
    source_dir: Path,
    registry: dict[str, Any] | None = None,
) -> list[dict[str, Any]]:
    by_setting: dict[str, list[tuple[Path, dict[str, Any]]]] = {}
    for path, proof in proofs:
        setting_id = proof.get("setting_id")
        if isinstance(setting_id, str):
            by_setting.setdefault(setting_id, []).append((path, proof))

    mapped = []
    for row in route_rows(registry):
        row_proofs = by_setting.get(row["setting_id"], [])
        proof_summaries = []
        for path, proof in row_proofs:
            proof_summaries.append(
                {
                    "artifact": _repo_path(path),
                    "observed_value": proof.get("observed_value"),
                    "phone_state": proof.get("phone_state"),
                    "control_surface": proof.get("control_surface"),
                    "api_exposure": proof.get("api_exposure"),
                    "copied_evidence": proof.get("copied_evidence", []),
                    "quality_ok": _proof_quality(proof),
                }
            )
        verdict = (
            "phone_app_manual_preference_observed"
            if any(item["quality_ok"] for item in proof_summaries)
            else "needs_phone_app_observation"
        )
        mapped.append(
            {
                "setting_id": row["setting_id"],
                "label": row["label"],
                "registry_status": row["status"],
                "route_status": row.get("routes", {}).get("smart_remote_phone", {}).get("status"),
                "routes": row.get("routes", {}),
                "verdict": verdict,
                "evidence_count": len(proof_summaries),
                "manual_proof_artifacts": proof_summaries,
                "observed_value": proof_summaries[-1]["observed_value"] if proof_summaries else None,
                "phone_state": proof_summaries[-1]["phone_state"] if proof_summaries else None,
                "control_surface": "smart_remote_phone_app",
                "api_exposure": "not_proven_by_phone_preference_review",
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
        raise FileNotFoundError(f"Smart Remote phone proof directory not found: {source_dir}")

    out_dir = out_dir or REPO_ROOT / "artifacts" / "falsification" / "r1_phone_settings_api_surface_review"
    out_dir.mkdir(parents=True, exist_ok=True)
    stamp = time.strftime("%Y%m%d-%H%M%S")
    report_path = out_dir / f"{stamp}-phone-preference-review.json"

    proofs = iter_phone_proofs(source_dir)
    mapping = map_registry(proofs, source_dir=source_dir, registry=registry)
    observed_rows = [row for row in mapping if row["verdict"] == "phone_app_manual_preference_observed"]

    report = {
        "generated_at": stamp,
        "source_dir": str(source_dir),
        "artifact_summary": {
            "phone_preference_proof_count": len(proofs),
            "proof_setting_ids": sorted({proof.get("setting_id") for _, proof in proofs if proof.get("setting_id")}),
            "quality_ok_count": sum(1 for _, proof in proofs if _proof_quality(proof)),
        },
        "registry_mapping_summary": {
            "smart_remote_phone_rows": len(mapping),
            "phone_app_observed_rows": len(observed_rows),
            "needs_phone_app_observation_rows": len(mapping) - len(observed_rows),
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
        default="artifacts/falsification/r1_phone_settings_manual",
        help="directory containing phone-preference-proof.json artifacts",
    )
    parser.add_argument(
        "--out-dir",
        default="artifacts/falsification/r1_phone_settings_api_surface_review",
        help="directory for the registry-mapped phone preference review report",
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
