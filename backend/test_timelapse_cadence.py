"""Honest timelapse cadence accounting derived from the per-frame timestamps.

``stack_session.timelapse_cadence`` reads the requested interval and each accepted
frame's ``capture_started_at`` (wall clock) and ``capture_s`` (pipeline cost) and
reports the achieved cadence without ever retiming or dropping a frame. These tests
drive it over synthetic timestamp sequences (on time, overrun, severe overrun, single
frame, missing frame) and assert the derived numbers and the overrun flag.

The last test re-derives the accounting for a real A7 III session when its private
manifest is present on this machine; it is skipped everywhere else and never writes.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

import stack_session as ss

_REAL_SESSION = Path(__file__).resolve().parent / "shots" / "sessions" / "cd33a1e9d86b438cbb59a8160c0e3c27"


def _frame(index, *, started=None, cost=None, status="accepted", pipeline=None):
    frame = {"index": index, "status": status,
             "destination": f"frame_{index:04d}.jpg"}
    if started is not None:
        frame["capture_started_at"] = started
    if cost is not None:
        frame["capture_s"] = cost
    if pipeline is not None:
        frame["capture_pipeline_s"] = pipeline
    return frame


def _state(requested, frames, *, mode="timelapse"):
    return {"mode": mode, "requested_interval_s": requested, "interval_s": requested,
            "frames": frames, "frame_count": len(frames)}


def test_on_time_sequence_reports_no_overrun():
    # Gaps sit right at the requested interval and each frame is cheaper than it.
    frames = [_frame(i, started=100.0 + 6.0 * i, cost=5.5) for i in range(4)]
    cadence = ss.timelapse_cadence(_state(6.0, frames))
    assert cadence["cadence_requested"] is True
    assert cadence["achieved_gaps_s"] == [6.0, 6.0, 6.0]
    assert cadence["achieved_interval_median_s"] == 6.0
    assert cadence["achieved_interval_mean_s"] == 6.0
    assert cadence["achieved_interval_min_s"] == 6.0
    assert cadence["achieved_interval_max_s"] == 6.0
    assert cadence["achieved_span_first_to_last_s"] == 18.0
    assert cadence["unreliable_gap_count"] == 0
    assert cadence["overrun_interval_count"] == 0
    assert cadence["frames_costlier_than_interval"] == 0
    assert cadence["cadence_overrun"] is False
    assert cadence["cadence_overrun_reason"] is None
    # Four frames = four real-world intervals: span (3 gaps) + one more.
    assert cadence["video_real_time_span_s"] == 24.0
    assert cadence["video_requested_real_time_span_s"] == 24.0
    assert cadence["frames"] == {"requested": 4, "accepted": 4, "missing": 0, "ambiguous": 0}


def test_overrun_sequence_flags_and_names_per_frame_cost():
    # A 4 s request against ~6.8 s pipeline cost: physically impossible to keep.
    starts = [0.0, 6.8, 13.6, 20.4]
    frames = [_frame(i, started=s, cost=6.7) for i, s in enumerate(starts)]
    cadence = ss.timelapse_cadence(_state(4.0, frames))
    assert cadence["achieved_gaps_s"] == [6.8, 6.8, 6.8]
    assert cadence["achieved_interval_median_s"] == 6.8
    assert cadence["overrun_interval_count"] == 3
    assert cadence["frames_costlier_than_interval"] == 4
    assert cadence["cadence_overrun"] is True
    assert "per frame" in cadence["cadence_overrun_reason"]
    # Requested field restates the ask; it is never confused with the achieved gaps.
    assert cadence["requested_interval_s"] == 4.0
    assert cadence["requested_span_first_to_last_s"] == 12.0
    assert cadence["achieved_span_first_to_last_s"] == 20.4
    # The finished video's honest real-world span exceeds the requested-interval span.
    assert cadence["video_real_time_span_s"] == pytest.approx(27.2, abs=1e-6)
    assert cadence["video_requested_real_time_span_s"] == 16.0
    assert cadence["video_real_time_span_s"] > cadence["video_requested_real_time_span_s"]


def test_severe_overrun_still_only_reports():
    # A half-second request against 6.7 s frames; the accounting reports, never retimes.
    starts = [0.0, 6.75, 13.5]
    frames = [_frame(i, started=s, cost=6.7) for i, s in enumerate(starts)]
    cadence = ss.timelapse_cadence(_state(0.5, frames))
    assert cadence["cadence_overrun"] is True
    assert cadence["frames_costlier_than_interval"] == 3
    assert cadence["overrun_interval_count"] == 2
    assert cadence["minimum_feasible_interval_s"] == 6.7
    # Every accepted frame is preserved; nothing was dropped to keep up.
    assert cadence["frames"]["accepted"] == 3 and cadence["frames"]["missing"] == 0


def test_single_frame_warns_from_the_first_frame():
    # No gap exists yet, but the one measured frame already exceeds the request.
    frames = [_frame(0, started=100.0, cost=6.7)]
    cadence = ss.timelapse_cadence(_state(4.0, frames))
    assert cadence["achieved_gaps_s"] == []
    assert cadence["achieved_interval_median_s"] is None
    assert cadence["achieved_span_first_to_last_s"] == 0.0
    assert cadence["frames_costlier_than_interval"] == 1
    assert cadence["cadence_overrun"] is True
    # With no achieved interval there is no honest video span to claim.
    assert cadence["video_real_time_span_s"] is None
    assert cadence["frames"] == {"requested": 1, "accepted": 1, "missing": 0, "ambiguous": 0}


def test_missing_frame_makes_the_gap_and_span_unreliable():
    # Frame 1 never landed (still pending); the surviving gap crosses a hole.
    frames = [_frame(0, started=0.0, cost=6.7),
              _frame(1, status="pending"),
              _frame(2, started=13.6, cost=6.7)]
    cadence = ss.timelapse_cadence(_state(4.0, frames))
    assert cadence["achieved_gaps_s"] == []
    assert cadence["unreliable_gap_count"] == 1
    assert cadence["achieved_span_first_to_last_s"] is None
    assert cadence["video_real_time_span_s"] is None
    assert cadence["frames"] == {"requested": 3, "accepted": 2, "missing": 1, "ambiguous": 0}


def test_backwards_clock_gap_is_unreliable_not_negative():
    frames = [_frame(0, started=1000.0, cost=6.7), _frame(1, started=990.0, cost=6.7)]
    cadence = ss.timelapse_cadence(_state(4.0, frames))
    assert cadence["achieved_gaps_s"] == []
    assert cadence["unreliable_gap_count"] == 1
    assert cadence["achieved_span_first_to_last_s"] is None


def test_record_cadence_sets_session_flag_for_timelapse():
    starts = [0.0, 6.8, 13.6]
    frames = [_frame(i, started=s, cost=6.7) for i, s in enumerate(starts)]
    state = _state(4.0, frames)
    ss._record_cadence(state)
    assert state["cadence_overrun"] is True
    assert state["timelapse_cadence"]["achieved_interval_median_s"] == 6.8
    assert state["timelapse_cadence"]["requested_interval_s"] == 4.0


def test_record_cadence_is_a_noop_off_the_timelapse_path():
    frames = [_frame(i, started=1000.0 * i, cost=1.0) for i in range(3)]
    state = _state(4.0, frames, mode="night")
    ss._record_cadence(state)
    assert "cadence_overrun" not in state and "timelapse_cadence" not in state


@pytest.mark.skipif(not (_REAL_SESSION / "manifest.json").is_file()
                    or os.environ.get("CAMERA_REAL_DATA_TESTS") != "1",
                    reason="private A7 III timelapse session cd33a1e9 absent or CAMERA_REAL_DATA_TESTS!=1")
def test_real_a7iii_timelapse_cadence_is_overrun():
    state = json.loads((_REAL_SESSION / "manifest.json").read_text())
    assert state["mode"] == "timelapse"
    cadence = ss.timelapse_cadence(state)
    assert cadence["frames"] == {"requested": 6, "accepted": 6, "missing": 0, "ambiguous": 0}
    assert cadence["requested_interval_s"] == 4.0
    # The body needs ~6.8 s per frame, so the 4 s interval was physically impossible.
    assert cadence["achieved_interval_median_s"] == pytest.approx(6.8, abs=0.2)
    assert cadence["measured_frame_cost_median_s"] == pytest.approx(6.8, abs=0.2)
    assert cadence["cadence_overrun"] is True
    assert cadence["overrun_interval_count"] == 5
    # Six frames at ~6.8 s is ~41 s of real time, not the 24 s a 4 s interval implies.
    assert cadence["video_real_time_span_s"] == pytest.approx(41.0, abs=1.0)
    assert cadence["video_requested_real_time_span_s"] == 24.0
