"""HandheldSession — body-shutter metering loop, no camera and no model.

A fake Camera pushes pre-made JPEGs frame by frame; an injected recommend_fn stands in for the
taste/vision brain. The loop must meter each frame, apply the recommendation for the NEXT frame,
never trigger the shutter, and always restore the base settings in its ``finally``.
"""
from __future__ import annotations

import json
import sys
import tempfile
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "backend"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import handheld
from handheld import HandheldSession
from _runner import run_module_tests

_BASE = {"iso": "100", "aperture": "f/8", "shutter": "1/125",
         "white_balance": "Automatic", "exposure_comp": "0"}


def _make_jpeg(path: Path) -> Path:
    from PIL import Image
    path.parent.mkdir(parents=True, exist_ok=True)
    Image.new("RGB", (16, 16), (90, 120, 150)).save(path, "JPEG")
    return path


class FakeCamera:
    """Pushes scripted frames; records set_exposure calls; the shutter must never fire."""

    def __init__(self, frames, *, base=None, fail_when=None):
        # frames: list where each item is a list of file paths, or an Exception to raise.
        self._frames = list(frames)
        self._idx = 0
        self._settings = dict(base or _BASE)
        self._fail_when = fail_when  # callable(kwargs) -> bool, to fail a specific apply
        self.set_exposure_calls: list[dict] = []
        self.connected = False
        self.disconnected = False
        self.capture_called = False

    def connect(self):
        self.connected = True
        return self

    def wake(self):
        return True

    def get_settings(self):
        return dict(self._settings)

    def set_exposure(self, **kwargs):
        self.set_exposure_calls.append(dict(kwargs))
        if self._fail_when is not None and self._fail_when(kwargs):
            raise RuntimeError("could not set exposure (locked by mode dial)")
        self._settings.update({k: str(v) for k, v in kwargs.items()})
        return dict(kwargs)

    def drain_events(self, max_ms=500):
        return 4  # pretend four stale events were discarded

    def listen_for_files(self, dest_dir, *, timeout_s, quiet_window_s=0.8,
                         max_total_s=10.0, cancel=None):
        if self._idx >= len(self._frames):
            return []
        item = self._frames[self._idx]
        self._idx += 1
        if isinstance(item, Exception):
            raise item
        return list(item)

    def capture(self, *a, **k):  # pragma: no cover - must never run
        self.capture_called = True
        raise AssertionError("HandheldSession must never trigger the shutter")

    def disconnect(self):
        self.disconnected = True


def _session_dirs(name: str):
    root = Path(tempfile.mkdtemp(prefix=f"handheld_{name}_"))
    frames_dir = root / "frames"
    out_dir = root / "out"
    frames_dir.mkdir(parents=True, exist_ok=True)
    return frames_dir, out_dir


def _frames(frames_dir: Path, count: int):
    """Return [[jpeg_path], ...] of `count` distinct pushed single-JPEG frames."""
    return [[_make_jpeg(frames_dir / f"frame_{i}.jpg")] for i in range(count)]


# --- tests ------------------------------------------------------------------

def test_three_frames_three_receipts():
    frames_dir, out_dir = _session_dirs("three")
    pushed = _frames(frames_dir, 3)
    cam = FakeCamera(pushed)
    seen_jpegs: list[str] = []

    def recommend(jpeg_path, current, lens_exif):
        seen_jpegs.append(jpeg_path)
        return {"iso_label": f"{200 + 100 * len(seen_jpegs)}", "aperture_label": "f/8",
                "shutter_label": "1/250", "why": ["brighten one stop"], "source": "test"}

    session = HandheldSession(lambda: cam, recommend, max_frames=3, max_duration_s=30, out_dir=out_dir)
    state = session.run()

    assert state["status"] == "stopped"
    assert state["stop_reason"] == "max_frames"
    assert len(state["frames"]) == 3
    assert all(f["receipt"] and f["receipt"]["sha256"] for f in state["frames"])
    assert len(seen_jpegs) == 3 and seen_jpegs == [p[0].__str__() for p in pushed]
    assert state["applied_count"] == 3
    assert state["stale_events_discarded"] == 4
    # The first apply carries frame-0's recommendation — proving metering happened before it.
    assert cam.set_exposure_calls[0]["iso"] == "300"
    assert cam.capture_called is False
    # each frame exposes an explanation + a settings_for_next_frame block for the UI
    assert state["frames"][0]["why"] == ["brighten one stop"]
    assert state["frames"][0]["settings_for_next_frame"]["iso"] == "300"


def test_set_exposure_not_called_before_first_frame_metered():
    frames_dir, out_dir = _session_dirs("order")
    pushed = _frames(frames_dir, 1)
    cam = FakeCamera(pushed)
    order: list[str] = []

    def recommend(jpeg_path, current, lens_exif):
        order.append("recommend")
        return {"iso_label": "400", "why": [], "source": "test"}

    original = cam.set_exposure

    def tracking(**kwargs):
        order.append("set_exposure")
        return original(**kwargs)

    cam.set_exposure = tracking
    HandheldSession(lambda: cam, recommend, max_frames=1, max_duration_s=30, out_dir=out_dir).run()
    # first recommendation precedes the first set_exposure; restore's set_exposure comes last
    assert order[0] == "recommend"
    assert order[1] == "set_exposure"


def test_apply_failure_recorded_and_loop_continues():
    frames_dir, out_dir = _session_dirs("fail")
    pushed = _frames(frames_dir, 3)
    cam = FakeCamera(pushed, fail_when=lambda kw: kw.get("iso") == "FAIL")

    def recommend(jpeg_path, current, lens_exif):
        idx = int(Path(jpeg_path).stem.split("_")[1])
        iso = "FAIL" if idx == 1 else "800"
        return {"iso_label": iso, "why": ["test"], "source": "test"}

    session = HandheldSession(lambda: cam, recommend, max_frames=3, max_duration_s=30, out_dir=out_dir)
    state = session.run()

    assert len(state["frames"]) == 3  # loop continued past the failure
    assert state["frames"][1]["apply_status"] == "failed"
    assert state["frames"][0]["apply_status"] == "applied"
    assert state["frames"][2]["apply_status"] == "applied"
    assert state["failure_count"] == 1
    assert state["applied_count"] == 2


def test_cancel_mid_loop_restores_base_settings():
    frames_dir, out_dir = _session_dirs("cancel")
    pushed = _frames(frames_dir, 5)
    cam = FakeCamera(pushed)
    cancel = threading.Event()

    def recommend(jpeg_path, current, lens_exif):
        cancel.set()  # cancel right after the first frame is metered
        return {"iso_label": "1600", "aperture_label": "f/8", "shutter_label": "1/500",
                "why": [], "source": "test"}

    session = HandheldSession(lambda: cam, recommend, max_frames=5, max_duration_s=30, out_dir=out_dir)
    state = session.run(cancel)

    assert state["stop_reason"] == "cancelled"
    assert len(state["frames"]) == 1
    assert state["restored"] is True
    # camera ends back at the base exposure it started from
    assert cam.get_settings()["iso"] == _BASE["iso"]
    assert cam.get_settings()["shutter"] == _BASE["shutter"]


def test_max_frames_stops():
    frames_dir, out_dir = _session_dirs("maxframes")
    pushed = _frames(frames_dir, 5)
    cam = FakeCamera(pushed)

    def recommend(jpeg_path, current, lens_exif):
        return {"iso_label": "200", "why": [], "source": "test"}

    state = HandheldSession(lambda: cam, recommend, max_frames=2, max_duration_s=30,
                            out_dir=out_dir).run()
    assert state["stop_reason"] == "max_frames"
    assert len(state["frames"]) == 2


def test_recommend_timeout_skips_adjustment():
    frames_dir, out_dir = _session_dirs("rectimeout")
    pushed = _frames(frames_dir, 1)
    cam = FakeCamera(pushed)

    def slow_recommend(jpeg_path, current, lens_exif):
        time.sleep(0.5)
        return {"iso_label": "6400", "why": [], "source": "test"}

    session = HandheldSession(lambda: cam, slow_recommend, max_frames=1, max_duration_s=30,
                              out_dir=out_dir, recommend_timeout_s=0.1)
    state = session.run()

    assert state["frames"][0]["apply_status"] == "skipped"
    assert state["applied_count"] == 0
    assert state["failure_count"] == 0
    # only the restore call touched set_exposure; no frame adjustment was applied
    assert all(call.get("iso") != "6400" for call in cam.set_exposure_calls)


def test_restoration_runs_when_loop_raises():
    frames_dir, out_dir = _session_dirs("raise")
    boom = RuntimeError("transport dropped while listening")
    cam = FakeCamera([boom])

    def recommend(jpeg_path, current, lens_exif):
        return {"iso_label": "100", "why": [], "source": "test"}

    session = HandheldSession(lambda: cam, recommend, max_frames=3, max_duration_s=30, out_dir=out_dir)
    raised = False
    try:
        session.run()
    except RuntimeError:
        raised = True

    assert raised, "a camera error inside the loop must propagate"
    state = session.state()
    assert state["status"] == "error"
    assert state["restored"] is True  # restoration still ran in the finally
    assert cam.disconnected is True


def test_journal_is_valid_jsonl():
    frames_dir, out_dir = _session_dirs("journal")
    pushed = _frames(frames_dir, 2)
    cam = FakeCamera(pushed)

    def recommend(jpeg_path, current, lens_exif):
        return {"iso_label": "320", "why": ["nudge"], "source": "test"}

    HandheldSession(lambda: cam, recommend, max_frames=2, max_duration_s=30, out_dir=out_dir).run()
    journal = out_dir / handheld.JOURNAL_NAME
    lines = [line for line in journal.read_text().splitlines() if line.strip()]
    records = [json.loads(line) for line in lines]  # raises if any line is not valid JSON
    types = [r["type"] for r in records]
    assert "session_start" in types and "summary" in types
    assert types.count("frame_metered") == 2
    summary = records[-1]
    assert summary["type"] == "summary"
    assert summary["frames"] == 2 and summary["applied"] == 2


if __name__ == "__main__":
    raise SystemExit(run_module_tests(__name__))


def test_apply_retries_while_the_body_is_still_writing(monkeypatch, tmp_path):
    """A refused write right after a body-shutter capture is retried within a bounded window."""
    import handheld as handheld_mod
    monkeypatch.setattr(handheld_mod.time, "sleep", lambda s: None)
    calls = {"n": 0}

    class Cam:
        def set_exposure(self, **kw):
            calls["n"] += 1
            if calls["n"] < 3:
                raise RuntimeError("Could not set aperture; camera stayed")
            return dict(kw)

    session = handheld_mod.HandheldSession(lambda: Cam(), lambda *a: {}, max_frames=1, max_duration_s=5, out_dir=tmp_path)
    assert session._apply_with_settle(Cam(), {"iso": "640"}) == {"iso": "640"} and calls["n"] == 3
    calls["n"] = -10
    import pytest as _pytest
    with _pytest.raises(RuntimeError):
        session._apply_with_settle(Cam(), {"iso": "640"})
