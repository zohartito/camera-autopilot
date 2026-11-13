"""The Sessions panel and Review say, in plain words, when a save was not confirmed durable.

PR #61 put `evidence.delivery_durability` on single shots; session frames now roll it up into the
session state and the published record (tests/test_capture_serial.py, tests/test_capture_policy.py).
Camera-free: the shared helper is executed in node; the pages are pinned at source level.
"""
from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
WEB = ROOT / "backend" / "web"
CLIENT = (WEB / "client.js").read_text(encoding="utf-8")
INDEX = (WEB / "index.html").read_text(encoding="utf-8")
REVIEW = (WEB / "review.html").read_text(encoding="utf-8")

SESSION_WARNING = ("Warning: saving was not confirmed durable for frame 2 of 3 — the disk reported an error "
                   "while flushing the camera download's folder.")
SINGLE_REASON = "the photo's folder could not be flushed to disk (failed: [Errno 5] Input/output error)"


def _node() -> str:
    path = shutil.which("node")
    if path is None:
        pytest.skip("node is not on PATH")
    probe = subprocess.run([path, "--version"], check=False, capture_output=True, text=True)
    if probe.returncode != 0:
        pytest.skip("node --version failed")
    return path


def _warnings(records: list) -> list:
    script = ("const window = {};\n" + CLIENT + "\n"
              f"const records = {json.dumps(records)};\n"
              "process.stdout.write(JSON.stringify(records.map(r => window.Autopilot.deliveryWarning(r))));")
    out = subprocess.run([_node(), "-e", script], check=True, capture_output=True, text=True).stdout
    return json.loads(out)


def test_delivery_warning_reads_session_state_session_record_and_single_shot():
    rollup = {"status": "unconfirmed", "frames": [1], "reasons": [SINGLE_REASON], "warning": SESSION_WARNING}
    session_state = {"id": "a" * 32, "status": "completed", "delivery_durability": rollup}
    session_record = {"image": "session_x.jpg", "evidence": {"delivery_durability": rollup}}
    single_shot = {"image": "shot_1.jpg", "evidence": {"delivery_durability": {"status": "unconfirmed",
                                                                               "reason": SINGLE_REASON}}}
    clean = [{"id": "b" * 32, "status": "completed"}, {"image": "shot_2.jpg", "evidence": {}}, None,
             {"delivery_durability": {"status": "confirmed", "warning": "not shown"}}]
    shown = _warnings([session_state, session_record, single_shot, *clean])
    assert shown[0] == SESSION_WARNING and shown[1] == SESSION_WARNING
    assert shown[2] == f"Warning: saving was not confirmed durable — {SINGLE_REASON}."
    assert shown[3:] == ["", "", "", ""]


def _stripped(records: list) -> list:
    script = ("const window = {};\n" + CLIENT + "\n"
              f"const records = {json.dumps(records)};\n"
              "process.stdout.write(JSON.stringify(records.map(r => window.Autopilot.summaryWithoutDeliveryWarning(r))));")
    out = subprocess.run([_node(), "-e", script], check=True, capture_output=True, text=True).stdout
    return json.loads(out)


def test_review_shows_the_warning_once_for_session_and_single_shot_records():
    """The stored summaries keep the sentence (API/record unchanged); the card's summary drops it
    because the dedicated warning line already shows it."""
    rollup = {"status": "unconfirmed", "frames": [1], "reasons": [SINGLE_REASON], "warning": SESSION_WARNING}
    session_record = {"summary": "hdr completed from 3 frames. " + SESSION_WARNING,
                      "evidence": {"delivery_durability": rollup}}
    # Exactly what backend/engine.py (#61) appends to a single shot's summary.
    single_summary = ("Photo captured. Review the result."
                      f" Warning: saving was not confirmed durable — {SINGLE_REASON}.")
    single_shot = {"summary": single_summary,
                   "evidence": {"delivery_durability": {"status": "unconfirmed", "reason": SINGLE_REASON}}}
    clean = {"summary": "Photo captured. Review the result.", "evidence": {}}
    unrelated = {"summary": "Photo captured.", "evidence": {"delivery_durability": rollup}}
    shown = _stripped([session_record, single_shot, clean, unrelated, {}])
    assert shown == ["hdr completed from 3 frames.", "Photo captured. Review the result.",
                     "Photo captured. Review the result.", "Photo captured.", ""]
    assert "not confirmed durable" not in " ".join(shown)
    card = REVIEW.split("function makeCard(shot) {", 1)[1].split("card.innerHTML", 1)[1].split("`;", 1)[0]
    assert "esc(shot.summary" not in card
    assert '<strong class="summary">${esc(Autopilot.summaryWithoutDeliveryWarning(shot) || \'Shot\')}</strong>' in card


def test_sessions_panel_and_review_show_the_warning_with_fresh_scripts():
    show_session = INDEX.split("function showSession(state) {", 1)[1].split("\n  }\n", 1)[0]
    assert "const deliveryWarning=Autopilot.deliveryWarning(state);" in show_session
    assert "if(deliveryWarning)$('session-status').textContent+=' · '+deliveryWarning;" in show_session
    card = REVIEW.split("function makeCard(shot) {", 1)[1].split("card.innerHTML", 1)[1].split("`;", 1)[0]
    assert ('${Autopilot.deliveryWarning(shot) ? `<p class="help delivery-warning" role="note">'
            '${esc(Autopilot.deliveryWarning(shot))}</p>` : \'\'}') in card
    # client.js changed: both pages that call the helper request the new version (2026-09-17 lesson).
    for page in (INDEX, REVIEW):
        assert '<script src="/client.js?v=20260929a"></script>' in page
        assert '<script src="/workflows.js?v=20261003c"></script>' in page
