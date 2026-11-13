"""Camera-free checks for Smart Remote behavior proof artifacts."""

from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "tools"))

import a7ii_smart_remote_behavior_proof
import review_smart_remote_api_surface


def _write_json(path: Path, payload: dict) -> None:
    path.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")


def test_smart_remote_behavior_proof_reviews_as_behavior_proven() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        source = root / "api-surface"
        source.mkdir()
        _write_json(
            source / "camera-after-startLiveview.json",
            {
                "ok": True,
                "request": {"method": "getAvailableApiList"},
                "json": {"result": [["getIsoSpeedRate", "setIsoSpeedRate"]]},
                "apis": ["getIsoSpeedRate", "setIsoSpeedRate"],
            },
        )
        proof_report, proof_path = a7ii_smart_remote_behavior_proof.build_report(
            setting_id="camera.iso",
            method="setIsoSpeedRate",
            requested_value="200",
            observed_before="AUTO",
            observed_after="200",
            observed_behavior="getEvent/getIsoSpeedRate reported ISO 200 after the setter.",
            human_verdict="proven",
            source_artifacts=[str(source)],
            notes=["Operator reverted ISO after proof."],
            out_dir=source,
        )
        review_report, review_path = review_smart_remote_api_surface.build_report(
            source,
            out_dir=root / "review",
        )

        assert proof_path.exists()
        assert review_path.exists()
        mapped = {row["setting_id"]: row for row in review_report["registry_mapping"]}
        iso = mapped["camera.iso"]
        assert proof_report["behavior_proven"] is True
        assert iso["verdict"] == "behavior_proven"
        assert iso["behavior_evidence"][0]["method"] == "setIsoSpeedRate"
        assert iso["behavior_evidence"][0]["observed_before"] == "AUTO"
        assert iso["behavior_evidence"][0]["observed_after"] == "200"
        assert review_report["artifact_summary"]["behavior_proof_count"] == 1
        assert review_report["registry_mapping_summary"]["behavior_proven_rows"] == 1
    print("  Smart Remote behavior proof review: ok")


def test_smart_remote_behavior_proof_requires_observation_and_source_for_proven() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        try:
            a7ii_smart_remote_behavior_proof.build_report(
                setting_id="camera.iso",
                method="<Smart Remote setter method>",
                requested_value="200",
                observed_before="AUTO",
                observed_after="200",
                human_verdict="proven",
                out_dir=Path(tmp),
            )
        except ValueError as exc:
            assert "--method" in str(exc)
        else:
            raise AssertionError("Smart Remote proof must reject placeholder methods")

        try:
            a7ii_smart_remote_behavior_proof.build_report(
                setting_id="camera.iso",
                method="setIsoSpeedRate",
                requested_value="200",
                observed_before="AUTO",
                observed_after="200",
                human_verdict="proven",
                out_dir=Path(tmp),
            )
        except ValueError as exc:
            assert "--source-artifact" in str(exc)
        else:
            raise AssertionError("proven Smart Remote proof must cite source artifacts or photos")
    print("  Smart Remote behavior proof guardrails: ok")


if __name__ == "__main__":
    test_smart_remote_behavior_proof_reviews_as_behavior_proven()
    test_smart_remote_behavior_proof_requires_observation_and_source_for_proven()
    print("smart_remote_behavior_proof tests: PASS")
