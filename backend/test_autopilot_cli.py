"""Camera-free tests for the unified autopilot CLI (autopilot.py).

No live camera or model downloads: wifi_a7ii.connect is monkeypatched to hand back a FAKE camera, and
each mode function (wifi_autopilot.shoot, wifi_stack.live_stack, ...) is replaced with a recorder that
captures the call and returns a trivial dict. We then drive each subcommand through autopilot.main()
and assert it dispatched to the RIGHT function with the RIGHT mapped arguments (e.g. `longexp` ->
wifi_stack.live_stack(mode='long_exposure', ...)). `modes` / `--list` are asserted to run with NO
camera. Mirrors test_wifi_lastwave.py's stub style.

    backend/.venv-ml/bin/python backend/test_autopilot_cli.py
    python3 backend/test_autopilot_cli.py                          (requires the numerical test dependencies)
"""
from __future__ import annotations

import io
import sys
import types
from contextlib import redirect_stdout
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

# Import the actual modules: process-wide dependency stubs contaminate aggregate tests.

import autopilot


# --- a fake camera + a recorder that captures the dispatched call -------------------------
class FakeCam:
    """Stand-in for a connected CameraRemote — never touched, just an identity to thread through."""


class Recorder:
    """Records the (args, kwargs) of the single call it receives and returns a trivial dict."""

    def __init__(self, name: str, ret=None):
        self.name = name
        self.ret = ret if ret is not None else {"stubbed": name}
        self.calls: list[tuple[tuple, dict]] = []

    def __call__(self, *args, **kwargs):
        self.calls.append((args, kwargs))
        return self.ret

    @property
    def kwargs(self) -> dict:
        assert self.calls, f"{self.name} was never called"
        return self.calls[-1][1]

    @property
    def args(self) -> tuple:
        assert self.calls, f"{self.name} was never called"
        return self.calls[-1][0]


def _patch(monkeypatches: list, module, attr: str, value) -> None:
    """Set module.attr to value and remember the original so _run can restore it afterward."""
    monkeypatches.append((module, attr, getattr(module, attr)))
    setattr(module, attr, value)


def _run(argv: list[str], patches: dict) -> str:
    """Run autopilot.main(argv) with the given {(module, attr): value} patches applied, a fake camera
    forced from wifi_a7ii.connect, stdout captured. Restores every patch afterward. Returns stdout."""
    import wifi_a7ii

    undo: list = []
    _patch(undo, wifi_a7ii, "connect", lambda *a, **k: FakeCam())
    for (module, attr), value in patches.items():
        _patch(undo, module, attr, value)
    buf = io.StringIO()
    try:
        with redirect_stdout(buf):
            autopilot.main(argv)
    finally:
        for module, attr, original in reversed(undo):
            setattr(module, attr, original)
    return buf.getvalue()


# --- tests --------------------------------------------------------------------------------
def test_shot_dispatches_to_shoot():
    import wifi_autopilot
    rec = Recorder("shoot")
    _run(["shot", "--full-res", "/tmp/fr"], {(wifi_autopilot, "shoot"): rec})
    assert len(rec.calls) == 1, rec.calls
    assert isinstance(rec.args[0], FakeCam), rec.args            # cam threaded through
    assert rec.kwargs == {"full_res_dir": "/tmp/fr"}, rec.kwargs
    print("  ok  shot -> wifi_autopilot.shoot(cam, full_res_dir=...)")


def test_shot_full_res_defaults_to_none():
    import wifi_autopilot
    rec = Recorder("shoot")
    _run(["shot"], {(wifi_autopilot, "shoot"): rec})
    assert rec.kwargs == {"full_res_dir": None}, rec.kwargs
    print("  ok  shot (no --full-res) -> full_res_dir=None")


def test_stack_modes_map_to_live_stack_mode():
    """Each stacking verb maps onto the right wifi_stack.live_stack `mode`, carrying frames/ev/out/save_dir."""
    import wifi_stack
    expected = {"hdr": "hdr", "longexp": "long_exposure",
                "focus": "focus_stack", "crowd": "crowd_remove"}
    for verb, mode in expected.items():
        rec = Recorder("live_stack")
        _run([verb, "--frames", "5", "--ev-step", "1.5", "--out", "/tmp/x.jpg",
              "--full-res", f"/tmp/{verb}_raw"],
             {(wifi_stack, "live_stack"): rec})
        assert isinstance(rec.args[0], FakeCam), rec.args
        assert rec.kwargs == {"mode": mode, "frames": 5, "ev_step": 1.5,
                              "out_path": "/tmp/x.jpg", "save_dir": f"/tmp/{verb}_raw"}, \
            (verb, rec.kwargs)
    print("  ok  hdr/longexp/focus/crowd -> live_stack(..., save_dir=...)")


def test_hdr_full_res_prompt_shape_dispatches_save_dir():
    """The prompt's RAW-stack command shape: `hdr --full-res /tmp/raw_stack` parses and dispatches."""
    import wifi_stack
    rec = Recorder("live_stack")
    _run(["hdr", "--full-res", "/tmp/raw_stack"], {(wifi_stack, "live_stack"): rec})
    assert rec.kwargs["mode"] == "hdr", rec.kwargs
    assert rec.kwargs["save_dir"] == "/tmp/raw_stack", rec.kwargs
    print("  ok  hdr --full-res /tmp/raw_stack -> live_stack(save_dir='/tmp/raw_stack')")


def test_longexp_frames_example():
    """The prompt's worked example: `longexp --frames 5` -> live_stack(mode='long_exposure', frames=5)."""
    import wifi_stack
    rec = Recorder("live_stack")
    _run(["longexp", "--frames", "5"], {(wifi_stack, "live_stack"): rec})
    assert rec.kwargs["mode"] == "long_exposure" and rec.kwargs["frames"] == 5, rec.kwargs
    assert rec.kwargs["save_dir"] == "/tmp/wifi_stack", rec.kwargs
    print("  ok  longexp --frames 5 -> live_stack(mode='long_exposure', frames=5, ...)")


def test_pano_dispatches_to_live_pano():
    import wifi_pano
    rec = Recorder("live_pano")
    _run(["pano", "--frames", "6", "--out", "/tmp/p.jpg"], {(wifi_pano, "live_pano"): rec})
    assert isinstance(rec.args[0], FakeCam), rec.args
    assert rec.kwargs == {"frames": 6, "out_path": "/tmp/p.jpg"}, rec.kwargs
    print("  ok  pano -> wifi_pano.live_pano(cam, frames=.., out_path=..)")


def test_night_dispatches_to_night_stack():
    import wifi_night
    rec = Recorder("night_stack")
    _run(["night", "--frames", "8", "--shutter", "15", "--iso", "3200"],
         {(wifi_night, "night_stack"): rec})
    assert isinstance(rec.args[0], FakeCam), rec.args
    assert rec.kwargs == {"frames": 8, "shutter": "15", "iso": "3200"}, rec.kwargs
    print("  ok  night -> wifi_night.night_stack(cam, frames=.., shutter=.., iso=..)")


def test_timelapse_dispatches_to_intervalometer():
    import wifi_night
    rec = Recorder("intervalometer")
    _run(["timelapse", "--frames", "30", "--interval", "5"],
         {(wifi_night, "intervalometer"): rec})
    assert isinstance(rec.args[0], FakeCam), rec.args
    assert rec.kwargs == {"frames": 30, "interval_s": 5.0}, rec.kwargs
    print("  ok  timelapse -> wifi_night.intervalometer(cam, frames=.., interval_s=..)")


def test_track_dispatches_to_track():
    import wifi_tracking
    # track's return is summarized via attributes, so hand back a tiny object with them.
    state = types.SimpleNamespace(frames=10, mean_fps=4.8, af_commands=7, af_locks=5, misses=1)
    rec = Recorder("track", ret=state)
    out = _run(["track", "--seconds", "12", "--fps", "4"], {(wifi_tracking, "track"): rec})
    assert isinstance(rec.args[0], FakeCam), rec.args
    assert rec.kwargs == {"fps": 4.0, "seconds": 12.0}, rec.kwargs
    assert "tracked 10 frames" in out and "4.8 fps" in out, out   # summary printed from the state
    print("  ok  track -> wifi_tracking.track(cam, fps=.., seconds=..) + summary")


def test_demo_dispatches_to_narrated_shot_and_track():
    import wifi_demo
    shot = Recorder("narrated_shot")
    track = Recorder("narrated_track")
    _run(["demo"], {(wifi_demo, "narrated_shot"): shot, (wifi_demo, "narrated_track"): track})
    assert len(shot.calls) == 1 and len(track.calls) == 0, (shot.calls, track.calls)
    assert isinstance(shot.args[0], FakeCam), shot.args

    shot2 = Recorder("narrated_shot")
    track2 = Recorder("narrated_track")
    _run(["demo", "--track"],
         {(wifi_demo, "narrated_shot"): shot2, (wifi_demo, "narrated_track"): track2})
    assert len(track2.calls) == 1 and len(shot2.calls) == 0, (shot2.calls, track2.calls)
    assert isinstance(track2.args[0], FakeCam), track2.args
    print("  ok  demo -> narrated_shot ; demo --track -> narrated_track")


def test_modes_and_list_run_with_no_camera():
    """`modes` and `--list` must list every mode WITHOUT importing wifi_a7ii or touching a camera.

    We make wifi_a7ii.connect EXPLODE if called, proving these paths never reach for the camera, and
    assert every mode name from MODES appears in the listing."""
    import wifi_a7ii

    def _boom(*_a, **_k):
        raise AssertionError("connect() must NOT be called for modes/--list (camera-free)")

    undo = wifi_a7ii.connect
    wifi_a7ii.connect = _boom
    try:
        for argv in (["modes"], ["--list"]):
            buf = io.StringIO()
            with redirect_stdout(buf):
                autopilot.main(argv)
            text = buf.getvalue()
            for name, _desc in autopilot.MODES:
                assert name in text, (argv, name, "missing from modes listing")
    finally:
        wifi_a7ii.connect = undo
    print("  ok  modes / --list run camera-free and list every mode")


def test_no_subcommand_prints_help():
    """Bare invocation (no subcommand) prints help and does not try to connect."""
    import wifi_a7ii

    def _boom(*_a, **_k):
        raise AssertionError("connect() must NOT be called when no subcommand is given")

    undo = wifi_a7ii.connect
    wifi_a7ii.connect = _boom
    try:
        buf = io.StringIO()
        with redirect_stdout(buf):
            autopilot.main([])
        assert "usage" in buf.getvalue().lower(), buf.getvalue()
    finally:
        wifi_a7ii.connect = undo
    print("  ok  no subcommand -> prints help, no camera")


if __name__ == "__main__":
    print("autopilot CLI tests:")
    test_shot_dispatches_to_shoot()
    test_shot_full_res_defaults_to_none()
    test_stack_modes_map_to_live_stack_mode()
    test_hdr_full_res_prompt_shape_dispatches_save_dir()
    test_longexp_frames_example()
    test_pano_dispatches_to_live_pano()
    test_night_dispatches_to_night_stack()
    test_timelapse_dispatches_to_intervalometer()
    test_track_dispatches_to_track()
    test_demo_dispatches_to_narrated_shot_and_track()
    test_modes_and_list_run_with_no_camera()
    test_no_subcommand_prints_help()
    print("ALL autopilot CLI tests PASS")
