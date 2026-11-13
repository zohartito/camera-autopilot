"""API-level contract for video autopilot recording.

Run standalone:
    python3 tests/test_video_api.py
"""

from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(ROOT / "backend"))

import app as backend_app
from fastapi import HTTPException
from _runner import run_module_tests


def test_record_route_returns_poster_and_live_adjustment_report() -> None:
    assert hasattr(backend_app, "RecordVideoRequest"), "RecordVideoRequest should exist"
    assert hasattr(backend_app, "record_video"), "record_video route should exist"

    response = backend_app.record_video(backend_app.RecordVideoRequest(intent="moody handheld video", seconds=4))

    assert response["ok"] is True
    assert Path(response["image"]).suffix.lower() in {".jpg", ".jpeg", ".png"}
    assert response["summary"] == "Recorded it — kept it moody."
    assert response["mood"]["heard"] == "moody handheld video"
    assert response["recording"]["recorded_seconds"] == 4.0
    assert response["recording"]["saved_to"]
    assert "samples" in response["recording"]


def test_record_route_reports_unsupported_body_without_server_error() -> None:
    with patch.object(backend_app.engine, "record", side_effect=NotImplementedError("A7 III start/stop unverified")):
        try:
            backend_app.record_video(backend_app.RecordVideoRequest(seconds=1))
        except HTTPException as exc:
            assert exc.status_code == 501
            assert "A7 III" in exc.detail
        else:
            raise AssertionError("unsupported recording was accepted")


if __name__ == "__main__":
    raise SystemExit(run_module_tests(__name__))
