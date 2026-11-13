"""Camera-free tests for the privacy-conscious UX validation workflow."""

from __future__ import annotations

import json
import os
import sys
import tempfile
from pathlib import Path

os.environ["CAMERA_ENGINE"] = "mock"
os.environ["CAMERA_AUTOPILOT_API_TOKEN"] = "test-token"

BACKEND = Path(__file__).resolve().parent
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))

import ux_validation


def _record(path: Path, index: int, rating: int, **overrides):
    values = {
        "consent_given": True,
        "magic_rating": rating,
        "would_use": "yes" if rating >= 4 else "no",
        "confusion_area": "none" if rating >= 4 else "waiting",
        "interface": "web",
        "experience_mode": "live_capture",
        "capture_succeeded": True,
        "intent_provided": True,
        "duration_seconds": 12.5,
        "study_id": "magic-v0",
        "recorded_at": f"2026-07-27T12:{index:02d}:00+00:00",
        "path": path,
    }
    values.update(overrides)
    return ux_validation.record_session(**values)


def test_structured_private_row_and_redaction() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        ledger = Path(tmp) / "sessions.jsonl"
        row = _record(
            ledger,
            1,
            5,
            confusion_area="other",
            confusion_note="Email me at tester@example.com or +1 (415) 555-1212 https://example.com",
            photo_reference="/private/path/shot_123.jpg",
        )
        assert row["session_id"].startswith("ux_")
        assert row["experience"]["photo_reference"] == "shot_123.jpg"
        assert row["privacy"]["raw_intent_stored"] is False
        assert row["privacy"]["ip_address_stored"] is False
        assert row["privacy"]["user_agent_stored"] is False
        assert row["privacy"]["note_redacted"] is True
        serialized = ledger.read_text(encoding="utf-8")
        assert "tester@example.com" not in serialized
        # The random session id is hex and can contain "415"; check the phone number where it could leak.
        assert "555-1212" not in serialized and "(415)" not in serialized
        assert "415" not in row["responses"]["confusion_note"]
        assert "https://example.com" not in serialized
        assert ledger.stat().st_mode & 0o777 == 0o600


def test_validation_and_incomplete_sessions() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        ledger = Path(tmp) / "sessions.jsonl"
        for kwargs in (
            {"consent_given": False, "magic_rating": 5},
            {"consent_given": True, "magic_rating": 6},
            {"consent_given": True, "magic_rating": None, "completion_status": "completed"},
        ):
            try:
                ux_validation.record_session(
                    would_use="yes",
                    confusion_area="none",
                    path=ledger,
                    **kwargs,
                )
            except ux_validation.UXValidationError:
                pass
            else:
                raise AssertionError(f"expected validation failure for {kwargs}")

        row = ux_validation.record_session(
            consent_given=True,
            magic_rating=None,
            would_use="unsure",
            confusion_area="other",
            completion_status="incomplete",
            capture_succeeded=False,
            experience_mode="plan_only",
            path=ledger,
        )
        assert row["gate_eligible"] is False
        report = ux_validation.build_report(path=ledger)
        assert report["study"]["incomplete_sessions"] == 1
        assert report["gate"]["cohort_sessions"] == 0


def test_fixed_earliest_ten_cohort_passes_without_cherry_picking() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        ledger = Path(tmp) / "sessions.jsonl"
        for index, rating in enumerate([5, 4, 5, 4, 5, 4, 5, 3, 2, 1], start=1):
            _record(ledger, index, rating)
        _record(ledger, 11, 1)  # held out; cannot replace an earlier result

        report = ux_validation.build_report(path=ledger)
        gate = report["gate"]
        assert gate["status"] == "pass"
        assert gate["cohort_sessions"] == 10
        assert gate["passes"] == 7
        assert gate["held_out_completed_sessions"] == 1
        assert report["cohort_summary"]["rating_distribution"] == {
            "1": 1,
            "2": 1,
            "3": 1,
            "4": 3,
            "5": 4,
        }
        markdown = ux_validation.report_markdown(report)
        assert "gate_status: `pass`" in markdown
        assert "session_id" not in markdown
        assert "confusion_note" not in markdown


def test_late_strong_rating_cannot_rescue_failed_cohort() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        ledger = Path(tmp) / "sessions.jsonl"
        for index, rating in enumerate([5, 4, 5, 4, 5, 4, 3, 3, 2, 1], start=1):
            _record(ledger, index, rating)
        _record(ledger, 11, 5)

        report = ux_validation.build_report(path=ledger)
        assert report["gate"]["status"] == "fail"
        assert report["gate"]["passes"] == 6
        assert report["gate"]["held_out_completed_sessions"] == 1
        assert report["privacy"]["distinct_real_people_verification"] == "operator_required"


def test_not_enough_sessions_and_study_isolation() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        ledger = Path(tmp) / "sessions.jsonl"
        _record(ledger, 1, 5, study_id="magic-v0")
        _record(ledger, 2, 1, study_id="magic-v0-r2")
        report = ux_validation.build_report(path=ledger, study_id="magic-v0")
        assert report["gate"]["status"] == "not_enough_sessions"
        assert report["gate"]["cohort_sessions"] == 1
        assert report["gate"]["remaining_sessions"] == 9


def test_duplicate_rejected_delete_supported_and_bad_rows_ignored() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        ledger = Path(tmp) / "sessions.jsonl"
        row = _record(ledger, 1, 5)
        try:
            _record(ledger, 2, 4, session_id=row["session_id"])
        except ux_validation.UXValidationError:
            pass
        else:
            raise AssertionError("duplicate session_id should fail")

        with ledger.open("a", encoding="utf-8") as handle:
            handle.write("{bad json\n")
            handle.write(json.dumps({"not": "a session"}) + "\n")
            tampered = dict(row)
            tampered["session_id"] = "ux_" + "a" * 32
            tampered["responses"] = dict(row["responses"])
            tampered["responses"]["would_use"] = "definitely"
            handle.write(json.dumps(tampered) + "\n")
        report = ux_validation.build_report(path=ledger)
        assert report["data_quality"]["malformed_rows_ignored"] == 1
        assert report["data_quality"]["invalid_rows_ignored"] == 2
        assert ux_validation.delete_session(row["session_id"], path=ledger) is True
        assert ux_validation.delete_session(row["session_id"], path=ledger) is False
        assert ux_validation.build_report(path=ledger)["gate"]["cohort_sessions"] == 0


def test_api_and_validation_page() -> None:
    try:
        import app
        from fastapi.testclient import TestClient
    except ModuleNotFoundError:
        return

    with tempfile.TemporaryDirectory() as tmp:
        original = ux_validation.VALIDATION_FILE
        ux_validation.VALIDATION_FILE = Path(tmp) / "api-sessions.jsonl"
        try:
            client = TestClient(app.app)
            payload = {
                "consent_given": True,
                "magic_rating": 4,
                "would_use": "maybe",
                "confusion_area": "controls",
                "confusion_note": "No contact details here.",
                "interface": "web",
                "experience_mode": "live_capture",
                "capture_succeeded": True,
                "intent_provided": True,
                "duration_seconds": 31.2,
                "photo_reference": "../shot_abc.jpg",
                "study_id": "magic-v0",
            }
            assert client.post("/api/ux-validation/sessions", json=payload).status_code == 401
            auth = {"X-Camera-Autopilot-Token": "test-token"}
            response = client.post(
                "/api/ux-validation/sessions", json=payload, headers=auth
            )
            assert response.status_code == 200, response.text
            ack = response.json()
            assert ack["saved"] is True
            assert ack["gate_eligible"] is True
            assert "confusion_note" not in ack
            assert "photo_reference" not in ack

            report_response = client.get(
                "/api/ux-validation/report?study_id=magic-v0", headers=auth
            )
            assert report_response.status_code == 200
            aggregate = report_response.json()
            assert aggregate["gate"]["cohort_sessions"] == 1
            assert "sessions" not in aggregate

            page = client.get("/validation")
            assert page.status_code == 200
            assert "Session feedback" in page.text
            assert "raw vibe" in page.text
            assert "Secure access is required" in page.text

            session_id = ack["session_id"]
            deleted = client.delete(
                f"/api/ux-validation/sessions/{session_id}", headers=auth
            )
            assert deleted.status_code == 200
            assert deleted.json() == {"deleted": True, "session_id": session_id}
        finally:
            ux_validation.VALIDATION_FILE = original


def main() -> int:
    tests = [
        test_structured_private_row_and_redaction,
        test_validation_and_incomplete_sessions,
        test_fixed_earliest_ten_cohort_passes_without_cherry_picking,
        test_late_strong_rating_cannot_rescue_failed_cohort,
        test_not_enough_sessions_and_study_isolation,
        test_duplicate_rejected_delete_supported_and_bad_rows_ignored,
        test_api_and_validation_page,
    ]
    for test in tests:
        test()
        print(f"PASS {test.__name__}")
    print(f"OK: {len(tests)} UX validation tests passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
