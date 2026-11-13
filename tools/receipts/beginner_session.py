#!/usr/bin/env python3
"""Beginner-study (UX validation) acceptance receipt for one participant.

Drives the exact anonymous session contract the web form at /validation uses:
POST /api/ux-validation/sessions with the participant's real answers, then GET
/api/ux-validation/report for the earliest-10 cohort gate status. Prints the
in-app steps the participant physically follows, and writes a receipt keyed by a
pseudonym (e.g. P01). No direct identifier is ever sent; the server assigns an
anonymous ux_<hex> session id.

Consent is mandatory and comes from the participant: pass --consent only after
the participant checked the consent box. Declined consent is never stored.

Exit 0 only on an observed pass (session saved and report fetched).
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import _common as common

WOULD_USE = ("yes", "no", "maybe", "unsure")
CONFUSION = ("none", "setup", "prompt", "waiting", "result", "controls", "review_share", "other")
EXPERIENCE = ("live_capture", "plan_only")
COMPLETION = ("completed", "incomplete")

IN_APP_STEPS = [
    "1. On the test phone, open the app and go to the Review/validation surface (/validation).",
    "2. Read the consent line: \"Save my anonymous answers and the photo filename for this",
    "   study.\" and tick the Consent checkbox (nothing is saved without it).",
    "3. Choose what happened: \"A photo was taken\" (live_capture) or \"I only saw the plan\"",
    "   (plan_only).",
    "4. Rate \"How magic did that feel?\" 1-5 (1 = meh, 5 = actual magic).",
    "5. Answer \"Would you use this?\" Yes / Maybe / No.",
    "6. Pick a confusion area (or None) and optionally add a short note.",
    "7. Tap \"Save feedback\". The screen shows the anonymous session ID.",
]


def run(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--participant", required=True,
                        help="pseudonym for the receipt only, e.g. P01 (never sent to the server)")
    parser.add_argument("--consent", action="store_true",
                        help="the participant checked the consent box (required to store anything)")
    parser.add_argument("--magic", type=int, choices=range(1, 6), default=None,
                        help="magic rating 1-5 (required when completion is 'completed')")
    parser.add_argument("--would-use", choices=WOULD_USE, default="unsure")
    parser.add_argument("--confusion-area", choices=CONFUSION, default="none")
    parser.add_argument("--confusion-note", default=None)
    parser.add_argument("--experience-mode", choices=EXPERIENCE, default="live_capture")
    parser.add_argument("--capture-succeeded", dest="capture_succeeded",
                        action="store_true", default=True)
    parser.add_argument("--capture-failed", dest="capture_succeeded", action="store_false")
    parser.add_argument("--intent-provided", action="store_true", default=False)
    parser.add_argument("--completion-status", choices=COMPLETION, default="completed")
    parser.add_argument("--interface", default="web")
    parser.add_argument("--duration-seconds", type=float, default=None)
    parser.add_argument("--study-id", default="magic-v0")
    common.add_common_args(parser, default_base_url="http://127.0.0.1:8001")
    args = parser.parse_args(argv)

    print(f"Participant {args.participant} -- in-app steps:")
    for step in IN_APP_STEPS:
        print("  " + step)
    print("")

    if not args.consent:
        return common.finish(
            what="beginner-session", passed=False, out_dir=args.out_dir,
            receipt_name=f"beginner-{_slug(args.participant)}",
            payload={"participant": args.participant, "consent_given": False},
            lines=["FAIL CAUSE: --consent not passed; declined consent is never stored.",
                   "Re-run with --consent only after the participant checked the box."])

    if args.completion_status == "completed" and args.magic is None:
        return common.finish(
            what="beginner-session", passed=False, out_dir=args.out_dir,
            receipt_name=f"beginner-{_slug(args.participant)}",
            payload={"participant": args.participant, "completion_status": "completed"},
            lines=["FAIL CAUSE: a completed session requires --magic 1-5."])

    token = common.read_token(args.token_file)
    client = common.Client(args.base_url, token, timeout=30.0)

    body = {
        "consent_given": True,
        "magic_rating": args.magic,
        "would_use": args.would_use,
        "confusion_area": args.confusion_area,
        "confusion_note": args.confusion_note,
        "interface": args.interface,
        "experience_mode": args.experience_mode,
        "capture_succeeded": args.capture_succeeded,
        "intent_provided": args.intent_provided,
        "duration_seconds": args.duration_seconds,
        "completion_status": args.completion_status,
        "study_id": args.study_id,
    }
    status, saved = client.post("/api/ux-validation/sessions", body)
    if status != 200 or not isinstance(saved, dict) or not saved.get("saved"):
        detail = saved.get("detail") if isinstance(saved, dict) else saved
        return common.finish(
            what="beginner-session", passed=False, out_dir=args.out_dir,
            receipt_name=f"beginner-{_slug(args.participant)}",
            payload={"participant": args.participant, "save_status": status,
                     "save_error": detail, "request": _redact(body)},
            lines=[f"FAIL CAUSE: session was not saved (HTTP {status}): {detail}"])

    rstatus, report = client.get(f"/api/ux-validation/report?study_id={args.study_id}")
    report_ok = rstatus == 200 and isinstance(report, dict)
    gate = (report.get("gate") if report_ok else {}) or {}

    passed = bool(saved.get("saved") and report_ok)

    lines = [
        f"participant: {args.participant}  study: {args.study_id}",
        f"session saved: {saved.get('saved')}  anon id: {saved.get('session_id')}",
        f"gate_eligible: {saved.get('gate_eligible')}  completion: {saved.get('completion_status')}",
        f"report fetched: {report_ok}  gate status: {gate.get('status')}",
        f"cohort {gate.get('cohort_sessions')}/{gate.get('cohort_size_required')} "
        f"passes={gate.get('passes')} remaining={gate.get('remaining_sessions')}",
    ]
    if not report_ok:
        lines.append(f"FAIL CAUSE: report not fetched (HTTP {rstatus})")

    return common.finish(
        what="beginner-session", passed=passed, out_dir=args.out_dir,
        receipt_name=f"beginner-{_slug(args.participant)}",
        payload={
            "participant": args.participant,
            "request": _redact(body),
            "saved": saved,
            "report": report if report_ok else {"http_status": rstatus, "body": report},
        },
        lines=lines)


def _redact(body: dict) -> dict:
    """Keep the free-text note out of the receipt; it is participant prose."""
    clean = dict(body)
    if clean.get("confusion_note"):
        clean["confusion_note"] = f"<{len(clean['confusion_note'])} chars, not stored in receipt>"
    return clean


def _slug(text: str) -> str:
    return "".join(c if c.isalnum() else "-" for c in text).strip("-").lower() or "participant"


if __name__ == "__main__":
    raise SystemExit(run())
