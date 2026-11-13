"""Privacy-conscious storage and reporting for the beginner UX validation study.

This ledger is deliberately separate from ``feedback.jsonl``:

* ordinary thumbs feedback can continue feeding product/eval work;
* validation sessions have an auditable schema and fixed cohort rule;
* the report never exposes free-text notes or photo references.

No participant name, IP address, user agent, raw spoken intent, or device identifier is requested
or stored. The local ledger lives outside the repository by default so a real study cannot be
committed accidentally.
"""

from __future__ import annotations

import json
import os
import re
import uuid
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

SCHEMA_VERSION = 1
DEFAULT_STUDY_ID = "magic-v0"
COHORT_SIZE = 10
PASS_RATING = 4
REQUIRED_PASSES = 7

VALIDATION_FILE = Path(
    os.getenv(
        "CAMERA_AUTOPILOT_UX_VALIDATION_FILE",
        Path.home() / ".camera-autopilot" / "ux-validation-sessions.jsonl",
    )
).expanduser()

INTERFACES = {"cli", "web", "ios", "other"}
EXPERIENCE_MODES = {"live_capture", "plan_only"}
COMPLETION_STATUSES = {"completed", "incomplete"}
WOULD_USE_VALUES = {"yes", "no", "maybe", "unsure"}
CONFUSION_AREAS = {
    "none",
    "setup",
    "prompt",
    "waiting",
    "result",
    "controls",
    "review_share",
    "other",
}

_STUDY_ID_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{1,63}$")
_SESSION_ID_RE = re.compile(r"^ux_[a-f0-9]{32}$")
_EMAIL_RE = re.compile(r"\b[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}\b", re.IGNORECASE)
_PHONE_RE = re.compile(r"(?<!\w)(?:\+?\d[\d().\s-]{6,}\d)(?!\w)")
_URL_RE = re.compile(r"\bhttps?://\S+", re.IGNORECASE)
_CONTROL_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")


class UXValidationError(ValueError):
    """Raised when a validation row would violate the study contract."""


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _new_session_id() -> str:
    return f"ux_{uuid.uuid4().hex}"


def _validate_study_id(study_id: str) -> str:
    value = str(study_id or "").strip().lower()
    if not _STUDY_ID_RE.fullmatch(value):
        raise UXValidationError(
            "study_id must be 2-64 lowercase letters, numbers, dots, underscores, or hyphens"
        )
    return value


def _validate_session_id(session_id: str | None) -> str:
    value = session_id or _new_session_id()
    if not _SESSION_ID_RE.fullmatch(value):
        raise UXValidationError("session_id must use the server-generated ux_<32 hex> format")
    return value


def _choice(value: str, allowed: set[str], field: str) -> str:
    normalized = str(value or "").strip().lower().replace("-", "_")
    if normalized not in allowed:
        raise UXValidationError(f"{field} must be one of: {', '.join(sorted(allowed))}")
    return normalized


def _sanitize_note(value: str | None, limit: int = 500) -> tuple[str | None, bool]:
    """Bound free text and redact common direct-contact patterns.

    This is a backstop, not a claim that arbitrary prose can be perfectly anonymized. The
    facilitator and form both tell testers not to enter names or contact details.
    """

    if not value:
        return None, False
    text = _CONTROL_RE.sub(" ", str(value))
    redacted = False
    for pattern, replacement in (
        (_EMAIL_RE, "[redacted-email]"),
        (_PHONE_RE, "[redacted-phone]"),
        (_URL_RE, "[redacted-url]"),
    ):
        updated = pattern.sub(replacement, text)
        redacted = redacted or updated != text
        text = updated
    text = " ".join(text.split()).strip()
    if len(text) > limit:
        text = text[:limit].rstrip()
        redacted = True
    return text or None, redacted


def _photo_reference(value: str | None) -> str | None:
    if not value:
        return None
    name = Path(str(value)).name
    return name[:160] or None


def _read_raw_rows(path: Path) -> tuple[list[dict[str, Any]], int]:
    if not path.exists():
        return [], 0
    rows: list[dict[str, Any]] = []
    malformed = 0
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except (OSError, UnicodeDecodeError):
        return [], 1
    for line in lines:
        if not line.strip():
            continue
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            malformed += 1
            continue
        if not isinstance(row, dict):
            malformed += 1
            continue
        rows.append(row)
    return rows, malformed


def _valid_recorded_at(value: Any) -> bool:
    if not isinstance(value, str) or not value:
        return False
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return False
    return parsed.tzinfo is not None


def _is_valid_stored_row(row: dict[str, Any]) -> bool:
    """Validate the stored schema before a row can affect the preregistered gate."""

    session_id = row.get("session_id")
    study_id = row.get("study_id")
    consent = row.get("consent")
    experience = row.get("experience")
    responses = row.get("responses")
    privacy = row.get("privacy")
    completion_status = row.get("completion_status")

    if (
        row.get("schema_version") != SCHEMA_VERSION
        or not isinstance(session_id, str)
        or not _SESSION_ID_RE.fullmatch(session_id)
        or not isinstance(study_id, str)
        or not _STUDY_ID_RE.fullmatch(study_id)
        or not _valid_recorded_at(row.get("recorded_at"))
        or completion_status not in COMPLETION_STATUSES
        or row.get("interface") not in INTERFACES
        or not isinstance(consent, dict)
        or consent.get("given") is not True
        or consent.get("scope") != "anonymous_answers_and_photo_reference"
        or not isinstance(experience, dict)
        or not isinstance(responses, dict)
        or not isinstance(privacy, dict)
    ):
        return False

    duration = experience.get("duration_seconds")
    photo_reference = experience.get("photo_reference")
    if (
        experience.get("mode") not in EXPERIENCE_MODES
        or not isinstance(experience.get("capture_succeeded"), bool)
        or not isinstance(experience.get("intent_provided"), bool)
        or (
            duration is not None
            and (
                isinstance(duration, bool)
                or not isinstance(duration, (int, float))
                or not 0 <= duration <= 7200
            )
        )
        or (
            photo_reference is not None
            and (
                not isinstance(photo_reference, str)
                or not photo_reference
                or len(photo_reference) > 160
                or Path(photo_reference).name != photo_reference
            )
        )
    ):
        return False

    rating = responses.get("magic_rating")
    note = responses.get("confusion_note")
    if (
        (
            rating is not None
            and (
                isinstance(rating, bool)
                or not isinstance(rating, int)
                or not 1 <= rating <= 5
            )
        )
        or (completion_status == "completed" and rating is None)
        or responses.get("would_use") not in WOULD_USE_VALUES
        or responses.get("confusion_area") not in CONFUSION_AREAS
        or (
            note is not None
            and (not isinstance(note, str) or not note or len(note) > 500)
        )
        or privacy.get("raw_intent_stored") is not False
        or privacy.get("ip_address_stored") is not False
        or privacy.get("user_agent_stored") is not False
        or privacy.get("direct_identifier_requested") is not False
        or not isinstance(privacy.get("note_redacted"), bool)
    ):
        return False

    expected_eligibility = completion_status == "completed" and rating is not None
    return row.get("gate_eligible") is expected_eligibility


def _active_rows(path: Path) -> tuple[list[dict[str, Any]], dict[str, int]]:
    rows, malformed = _read_raw_rows(path)
    active: list[dict[str, Any]] = []
    seen: set[str] = set()
    duplicate = 0
    invalid = 0
    for row in rows:
        session_id = row.get("session_id")
        if not _is_valid_stored_row(row):
            invalid += 1
            continue
        if session_id in seen:
            duplicate += 1
            continue
        seen.add(session_id)
        active.append(row)
    return active, {
        "malformed_rows_ignored": malformed,
        "invalid_rows_ignored": invalid,
        "duplicate_session_ids_ignored": duplicate,
    }


def record_session(
    *,
    consent_given: bool,
    magic_rating: int | None,
    would_use: str,
    confusion_area: str,
    confusion_note: str | None = None,
    interface: str = "cli",
    experience_mode: str = "live_capture",
    capture_succeeded: bool = True,
    intent_provided: bool = False,
    duration_seconds: float | None = None,
    photo_reference: str | None = None,
    completion_status: str = "completed",
    study_id: str = DEFAULT_STUDY_ID,
    session_id: str | None = None,
    path: Path | None = None,
    recorded_at: str | None = None,
) -> dict[str, Any]:
    """Validate and append one anonymous UX session.

    Declined consent is never written. Completed sessions require a 1-5 magic rating; incomplete
    sessions may omit it and remain visible as dropouts without entering the gate cohort.
    """

    if consent_given is not True:
        raise UXValidationError("declined consent is not stored")
    study_id = _validate_study_id(study_id)
    session_id = _validate_session_id(session_id)
    interface = _choice(interface, INTERFACES, "interface")
    experience_mode = _choice(experience_mode, EXPERIENCE_MODES, "experience_mode")
    completion_status = _choice(
        completion_status, COMPLETION_STATUSES, "completion_status"
    )
    would_use = _choice(would_use, WOULD_USE_VALUES, "would_use")
    confusion_area = _choice(confusion_area, CONFUSION_AREAS, "confusion_area")

    if magic_rating is not None and (
        isinstance(magic_rating, bool)
        or not isinstance(magic_rating, int)
        or not 1 <= magic_rating <= 5
    ):
        raise UXValidationError("magic_rating must be an integer from 1 to 5")
    if completion_status == "completed" and magic_rating is None:
        raise UXValidationError("completed sessions require magic_rating")

    if duration_seconds is not None:
        if isinstance(duration_seconds, bool):
            raise UXValidationError("duration_seconds must be a non-negative number")
        try:
            duration_seconds = round(float(duration_seconds), 1)
        except (TypeError, ValueError) as exc:
            raise UXValidationError("duration_seconds must be a non-negative number") from exc
        if not 0 <= duration_seconds <= 7200:
            raise UXValidationError("duration_seconds must be between 0 and 7200")

    note, note_redacted = _sanitize_note(confusion_note)
    target = Path(path or VALIDATION_FILE).expanduser()
    existing, _ = _active_rows(target)
    if any(row["session_id"] == session_id for row in existing):
        raise UXValidationError("session_id already exists")

    gate_eligible = completion_status == "completed" and magic_rating is not None
    row = {
        "schema_version": SCHEMA_VERSION,
        "session_id": session_id,
        "study_id": study_id,
        "recorded_at": recorded_at or _utc_now(),
        "completion_status": completion_status,
        "gate_eligible": gate_eligible,
        "consent": {
            "given": True,
            "scope": "anonymous_answers_and_photo_reference",
        },
        "interface": interface,
        "experience": {
            "mode": experience_mode,
            "capture_succeeded": bool(capture_succeeded),
            "intent_provided": bool(intent_provided),
            "duration_seconds": duration_seconds,
            "photo_reference": _photo_reference(photo_reference),
        },
        "responses": {
            "magic_rating": magic_rating,
            "would_use": would_use,
            "confusion_area": confusion_area,
            "confusion_note": note,
        },
        "privacy": {
            "raw_intent_stored": False,
            "ip_address_stored": False,
            "user_agent_stored": False,
            "direct_identifier_requested": False,
            "note_redacted": note_redacted,
        },
    }
    target.parent.mkdir(parents=True, exist_ok=True)
    payload = (json.dumps(row, ensure_ascii=False, separators=(",", ":")) + "\n").encode("utf-8")
    fd = os.open(target, os.O_APPEND | os.O_CREAT | os.O_WRONLY, 0o600)
    try:
        os.write(fd, payload)
    finally:
        os.close(fd)
    try:
        target.chmod(0o600)
    except OSError:
        pass
    return row


def delete_session(session_id: str, path: Path | None = None) -> bool:
    """Physically remove a session when a participant withdraws consent."""

    session_id = _validate_session_id(session_id)
    target = Path(path or VALIDATION_FILE).expanduser()
    rows, _ = _read_raw_rows(target)
    kept = [row for row in rows if row.get("session_id") != session_id]
    if len(kept) == len(rows):
        return False
    target.parent.mkdir(parents=True, exist_ok=True)
    temp = target.with_suffix(target.suffix + ".tmp")
    temp.write_text(
        "".join(json.dumps(row, ensure_ascii=False, separators=(",", ":")) + "\n" for row in kept),
        encoding="utf-8",
    )
    temp.chmod(0o600)
    os.replace(temp, target)
    return True


def build_report(
    *,
    path: Path | None = None,
    study_id: str = DEFAULT_STUDY_ID,
) -> dict[str, Any]:
    """Build the fixed earliest-10 cohort report without exposing session-level prose."""

    study_id = _validate_study_id(study_id)
    target = Path(path or VALIDATION_FILE).expanduser()
    rows, quality = _active_rows(target)
    study_rows = [row for row in rows if row.get("study_id") == study_id]
    completed = [
        row
        for row in study_rows
        if row.get("completion_status") == "completed"
        and row.get("gate_eligible") is True
        and isinstance(row["responses"].get("magic_rating"), int)
        and not isinstance(row["responses"].get("magic_rating"), bool)
        and 1 <= row["responses"]["magic_rating"] <= 5
    ]
    completed.sort(key=lambda row: (str(row.get("recorded_at") or ""), row["session_id"]))
    cohort = completed[:COHORT_SIZE]
    ratings = [row["responses"]["magic_rating"] for row in cohort]
    passes = sum(rating >= PASS_RATING for rating in ratings)

    if len(cohort) < COHORT_SIZE:
        gate_status = "not_enough_sessions"
        next_action = (
            f"Run {COHORT_SIZE - len(cohort)} more consented, completed session"
            f"{'s' if COHORT_SIZE - len(cohort) != 1 else ''} for study {study_id}."
        )
    elif passes >= REQUIRED_PASSES:
        gate_status = "pass"
        next_action = (
            "The preregistered earliest-10 cohort met the magic gate; review aggregate confusion "
            "and capture-failure counts before a release decision."
        )
    else:
        gate_status = "fail"
        next_action = (
            "The preregistered earliest-10 cohort did not meet the gate. Fix the leading confusion "
            "or reliability issue, then use a new study_id for the next 10-person cohort."
        )

    confusion_counts = Counter(
        row["responses"].get("confusion_area", "other") for row in cohort
    )
    would_use_counts = Counter(row["responses"].get("would_use", "unsure") for row in cohort)
    mode_counts = Counter(row["experience"].get("mode", "plan_only") for row in cohort)
    rating_counts = Counter(ratings)

    return {
        "schema_version": SCHEMA_VERSION,
        "generated_at": _utc_now(),
        "study_id": study_id,
        "gate": {
            "definition": (
                f"At least {REQUIRED_PASSES} of the earliest {COHORT_SIZE} eligible sessions "
                f"rate magic {PASS_RATING} or 5."
            ),
            "status": gate_status,
            "cohort_policy": "earliest_eligible_sessions_no_cherry_picking",
            "cohort_size_required": COHORT_SIZE,
            "required_passes": REQUIRED_PASSES,
            "pass_rating_minimum": PASS_RATING,
            "cohort_sessions": len(cohort),
            "remaining_sessions": max(0, COHORT_SIZE - len(cohort)),
            "passes": passes,
            "failures": len(cohort) - passes,
            "pass_rate": round(passes / len(cohort), 3) if cohort else None,
            "held_out_completed_sessions": max(0, len(completed) - COHORT_SIZE),
            "next_action": next_action,
        },
        "study": {
            "stored_sessions": len(study_rows),
            "eligible_completed_sessions": len(completed),
            "incomplete_sessions": sum(
                row.get("completion_status") == "incomplete" for row in study_rows
            ),
        },
        "cohort_summary": {
            "rating_distribution": {str(value): rating_counts.get(value, 0) for value in range(1, 6)},
            "would_use": {value: would_use_counts.get(value, 0) for value in sorted(WOULD_USE_VALUES)},
            "confusion_areas": {
                value: confusion_counts.get(value, 0) for value in sorted(CONFUSION_AREAS)
            },
            "experience_modes": {
                value: mode_counts.get(value, 0) for value in sorted(EXPERIENCE_MODES)
            },
            "capture_succeeded": sum(
                row["experience"].get("capture_succeeded") is True for row in cohort
            ),
            "capture_failed": sum(
                row["experience"].get("capture_succeeded") is not True for row in cohort
            ),
            "notes_present": sum(
                bool(row["responses"].get("confusion_note")) for row in cohort
            ),
            "notes_redacted": sum(
                row.get("privacy", {}).get("note_redacted") is True for row in cohort
            ),
        },
        "privacy": {
            "report_contains_session_ids": False,
            "report_contains_free_text": False,
            "report_contains_photo_references": False,
            "report_contains_raw_intents": False,
            "participant_identity_tracked": False,
            "distinct_real_people_verification": "operator_required",
            "ledger_file_name": target.name,
            "ledger_exists": target.exists(),
        },
        "data_quality": quality,
    }


def report_markdown(report: dict[str, Any]) -> str:
    """Render an aggregate-only operator report."""

    gate = report["gate"]
    study = report["study"]
    cohort = report["cohort_summary"]
    lines = [
        "# UX Validation Operator Report",
        "",
        f"- generated_at: `{report['generated_at']}`",
        f"- study_id: `{report['study_id']}`",
        f"- gate_status: `{gate['status']}`",
        f"- gate_definition: {gate['definition']}",
        "",
        "## Cohort",
        "",
        f"- eligible completed: `{study['eligible_completed_sessions']}`",
        f"- gate cohort: `{gate['cohort_sessions']}/{gate['cohort_size_required']}`",
        f"- passes: `{gate['passes']}/{gate['cohort_sessions']}`",
        f"- remaining sessions: `{gate['remaining_sessions']}`",
        f"- held out after earliest cohort: `{gate['held_out_completed_sessions']}`",
        f"- incomplete sessions: `{study['incomplete_sessions']}`",
        "",
        "## Experience",
        "",
        f"- live capture: `{cohort['experience_modes']['live_capture']}`",
        f"- plan only: `{cohort['experience_modes']['plan_only']}`",
        f"- capture succeeded: `{cohort['capture_succeeded']}`",
        f"- capture failed: `{cohort['capture_failed']}`",
        f"- rating distribution: `{json.dumps(cohort['rating_distribution'], sort_keys=True)}`",
        f"- would use: `{json.dumps(cohort['would_use'], sort_keys=True)}`",
        f"- confusion areas: `{json.dumps(cohort['confusion_areas'], sort_keys=True)}`",
        "",
        "## Privacy And Integrity",
        "",
        "- No participant names, IP addresses, user agents, raw intents, session IDs, photo references,",
        "  or free-text responses are included in this report.",
        "- Participant identities are not tracked. The operator must verify that each eligible session",
        "  came from one distinct, real beginner; this report does not claim that automatically.",
        f"- malformed rows ignored: `{report['data_quality']['malformed_rows_ignored']}`",
        f"- invalid rows ignored: `{report['data_quality']['invalid_rows_ignored']}`",
        f"- duplicate session IDs ignored: `{report['data_quality']['duplicate_session_ids_ignored']}`",
        "- Cohort policy: earliest eligible sessions; extra sessions cannot replace an earlier result.",
        "",
        "## Operator Action",
        "",
        gate["next_action"],
        "",
    ]
    return "\n".join(lines)
