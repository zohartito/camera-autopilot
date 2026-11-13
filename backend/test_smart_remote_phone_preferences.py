"""Camera-free checks for Smart Remote phone preference proof artifacts."""

from __future__ import annotations

import sys
import tempfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "backend"))
sys.path.insert(0, str(REPO_ROOT / "tools"))

import a7ii_phone_preference_proof
import review_smart_remote_phone_preferences
import settings_evidence


def test_phone_preference_proof_reviews_into_setting_evidence() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        proof_report, proof_path = a7ii_phone_preference_proof.build_report(
            setting_id="phone.grid_line",
            observed_value="Rule of 3rds Grid",
            phone_state="Smart Remote phone settings screen is open.",
            evidence_notes=["Operator saw the grid overlay preference in the phone app."],
            out_dir=root / "manual",
        )
        review_report, review_path = review_smart_remote_phone_preferences.build_report(
            root / "manual",
            out_dir=root / "review",
        )
        index = settings_evidence.load_review_index(
            sources=(
                settings_evidence.ReviewSource(
                    route="smart_remote_phone",
                    label="fake phone preferences",
                    directory=root / "review",
                    patterns=("*phone-preference-review.json",),
                ),
            )
        )
        item = settings_evidence.evidence_for_setting("phone.grid_line", review_index=index)

        assert proof_path.exists()
        assert review_path.exists()
        assert proof_report["verdict"] == "phone_app_preference_observed_manual_route"
        assert review_report["artifact_summary"]["phone_preference_proof_count"] == 1
        assert review_report["registry_mapping_summary"]["phone_app_observed_rows"] >= 1
        assert item["reviews"][0]["verdict"] == "phone_app_manual_preference_observed"
        assert item["reviews"][0]["observed_value"] == "Rule of 3rds Grid"
        assert item["missing_review_routes"] == []
    print("  Smart Remote phone preference proof review: ok")


def test_phone_preference_proof_requires_real_observation() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        try:
            a7ii_phone_preference_proof.build_report(
                setting_id="phone.grid_line",
                observed_value="<visible phone-app value>",
                phone_state="Smart Remote phone settings screen is open.",
                out_dir=Path(tmp),
            )
        except ValueError as exc:
            assert "--observed-value" in str(exc)
        else:
            raise AssertionError("phone preference proof must reject placeholder observed values")
    print("  Smart Remote phone preference placeholder guard: ok")


if __name__ == "__main__":
    test_phone_preference_proof_reviews_into_setting_evidence()
    test_phone_preference_proof_requires_real_observation()
    print("smart_remote_phone_preferences tests: PASS")
