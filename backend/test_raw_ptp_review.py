"""Camera-free tests for raw PTP artifact registry mapping."""

from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "tools"))

import review_raw_ptp_artifacts


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def _write_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")


def test_raw_ptp_review_maps_protocol_and_behavior_artifacts() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        protocol = root / "protocol"
        _write(
            protocol / "attempt-1.log",
            "[SELF-CHECK] PASS (blob>1000B & ISO & battery present & Sony marker)\n"
            "=== SUMMARY ===\n"
            "  W1: ResponseCode 0x2001 (PTP_RC_OK) -> HONORED\n"
            "  W2: ResponseCode 0x2001 (PTP_RC_OK) -> HONORED\n",
        )
        behavior = root / "20260623-raw"
        _write_json(
            behavior / "af-point-metrics.json",
            {
                "files": {
                    "before": str(behavior / "before.jpg"),
                    "after_point_a": str(behavior / "after-point-a.jpg"),
                    "after_point_b": str(behavior / "after-point-b.jpg"),
                },
                "full_frame_mean_abs_diff": {
                    "point_a_vs_before": 4.5,
                    "point_b_vs_point_a": 5.5,
                },
                "roi_sharpness_gradient_energy": {
                    "foreground_table": {"before": 10, "after_point_a": 20},
                },
                "interpretation": "fake behavior metrics for tests",
            },
        )
        for image_name in ("before.jpg", "after-point-a.jpg", "after-point-b.jpg"):
            (behavior / image_name).write_bytes(b"fake-jpeg")
        _write_json(
            behavior / "human-proof.json",
            {
                "setting_id": "camera.focus_settings",
                "behavior_type": "manual_focus",
                "behavior_proven": True,
                "human_verdict": "proven",
                "notes": "Manual focus moved forward and returned in previews.",
            },
        )
        _write_json(
            behavior / "af-point-visual-proof" / "human-proof.json",
            {
                "setting_id": "camera.focus_area",
                "behavior_type": "af_point",
                "behavior_proven": False,
                "human_verdict": "rejected",
                "notes": "AF-point writes returned OK but did not create distinct focus outcomes.",
            },
        )

        report, report_path = review_raw_ptp_artifacts.build_report(
            [protocol, behavior],
            out_dir=root / "out",
        )

    assert report_path.name.endswith("-raw-ptp-review.json")
    assert report["registry_mapping_summary"]["raw_ptp_rows"] >= 2

    mapped = {row["setting_id"]: row for row in report["registry_mapping"]}
    focus_area = mapped["camera.focus_area"]
    assert focus_area["verdict"] == "behavior_rejected"
    assert focus_area["protocol_evidence"][0]["protocol_verdict"] == "HONORED"
    assert focus_area["behavior_evidence"][0]["preview_files_exist"] is True
    assert any(item.get("human_verdict") == "rejected" for item in focus_area["behavior_evidence"])

    focus_settings = mapped["camera.focus_settings"]
    assert focus_settings["verdict"] == "behavior_proven"
    assert focus_settings["behavior_evidence"][0]["human_verdict"] == "proven"
    print("  raw PTP artifact-to-registry mapping: ok")


if __name__ == "__main__":
    test_raw_ptp_review_maps_protocol_and_behavior_artifacts()
    print("raw_ptp_review tests: PASS")
