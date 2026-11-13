"""Camera-free checks for raw PTP visual proof artifacts."""

from __future__ import annotations

import sys
import tempfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "tools"))

import a7ii_raw_ptp_visual_proof
import review_raw_ptp_artifacts


def test_raw_ptp_visual_proof_reviews_as_behavior_proven() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        source = root / "focus-behavior"
        source.mkdir()
        (source / "image-metrics.json").write_text("{}", encoding="utf-8")
        protocol = root / "protocol"
        protocol.mkdir()
        (protocol / "attempt.log").write_text(
            "[SELF-CHECK] PASS (blob>1000B & ISO & battery present & Sony marker)\n"
            "=== SUMMARY ===\n"
            "  W2: ResponseCode 0x2001 (PTP_RC_OK) -> HONORED\n",
            encoding="utf-8",
        )
        proof_report, proof_path = a7ii_raw_ptp_visual_proof.build_report(
            setting_id="camera.focus_settings",
            human_verdict="proven",
            observed_behavior="Manual focus nudge visibly shifted focus to the near marker, then returned.",
            target_setup="Near ruler at 0.5m and far box at 2m under fixed light.",
            source_artifacts=[str(source)],
            notes=["Operator compared before/after previews."],
            out_dir=root / "r2_usb_ptp",
        )
        review_report, review_path = review_raw_ptp_artifacts.build_report(
            [protocol, root / "r2_usb_ptp"],
            out_dir=root / "review",
        )

        assert proof_path.exists()
        assert review_path.exists()
        mapped = {row["setting_id"]: row for row in review_report["registry_mapping"]}
        focus_settings = mapped["camera.focus_settings"]
        assert proof_report["behavior_proven"] is True
        assert focus_settings["verdict"] == "behavior_proven"
        assert focus_settings["behavior_evidence"][0]["observed_behavior"].startswith("Manual focus")
        assert focus_settings["behavior_evidence"][0]["target_setup"].startswith("Near ruler")
    print("  raw PTP visual proof review: ok")


def test_raw_ptp_visual_proof_requires_observation_and_source_for_proven() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        try:
            a7ii_raw_ptp_visual_proof.build_report(
                setting_id="camera.focus_area",
                human_verdict="proven",
                observed_behavior="<describe near/far visual behavior>",
                target_setup="Near/far target setup",
                out_dir=Path(tmp),
            )
        except ValueError as exc:
            assert "--observed-behavior" in str(exc)
        else:
            raise AssertionError("raw PTP proof must reject placeholder behavior observations")

        try:
            a7ii_raw_ptp_visual_proof.build_report(
                setting_id="camera.focus_area",
                human_verdict="proven",
                observed_behavior="AF point A sharpened near target; point B sharpened far target.",
                target_setup="Near/far target setup",
                out_dir=Path(tmp),
            )
        except ValueError as exc:
            assert "--source-artifact" in str(exc)
        else:
            raise AssertionError("proven raw PTP visual proof must cite source artifacts or photos")
    print("  raw PTP visual proof guardrails: ok")


if __name__ == "__main__":
    test_raw_ptp_visual_proof_reviews_as_behavior_proven()
    test_raw_ptp_visual_proof_requires_observation_and_source_for_proven()
    print("raw_ptp_visual_proof tests: PASS")
