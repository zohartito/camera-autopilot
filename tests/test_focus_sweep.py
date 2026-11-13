"""Camera-free regressions for the shutter-free focus sweep and sweep-planned brackets.

A synthetic lens (optional hard rails, liveview lag, command slip) drives
`tools/focus_sweep.run_sweep`: plateau detection in both directions, the command caps, the
exact-inverse return verification (honest mismatch when a rail absorbs commands or the lens
slips), mid-tone exposure search and the restore order; no shutter method is ever called and
every command has the sweep's own step magnitude. `plan_from_sweep` is checked in cumulative
coordinates for frames 2..5, coverage edge cases, peak inclusion and step-multiple moves;
`run_bracket` is checked on the explicit-plan path (initial move, multi-step moves, never a
chunk other than +-step) against the FakeCamera used by the uniform-plan tests.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))
sys.path.insert(0, str(ROOT / "tools"))
sys.path.insert(0, str(ROOT))

import focus_bracket  # noqa: E402
import focus_bracket_capture as bracket_tool  # noqa: E402
import focus_sweep as sweep_tool  # noqa: E402
import sonyremote.camera as camera_module  # noqa: E402
from test_focus_bracket import A7II, A7III, PORT, FakeCamera, ShutterCounter, _jpeg_bytes  # noqa: E402
from sonyremote.camera import CameraError, PlaceholderSettingsError  # noqa: E402

SERIAL = "A7III0SERIAL"  # the A7 III's USB serial (lessons: identification)
SHUTTERS = ["1/4000", "1/2000", "1/1000", "1/500", "1/250", "1/125", "1/60", "1/30", "1/15", "1/8",
            "1/4", "1/2", "1", "2", "4", "8", "15", "30", "Bulb"]
LIVE_SWEEP = ROOT / "artifacts" / "stage-wave-20260915" / "focus-sweep-03" / "sweep.json"


def bell(pos: float, peak: float = 5.0, width: float = 6.0, top: float = 700.0, floor: float = 40.0) -> float:
    """Sharpness vs. position: a bell that bottoms out on a flat plateau (the metric floor)."""
    return max(floor, 10.0 + top / (1.0 + ((pos - peak) / width) ** 2))


class FakeLensCamera:
    """Duck-typed Camera over a synthetic lens: integer position, optional rails, laggy liveview."""

    def __init__(self, *, curve=bell, near_rail=-1000, far_rail=1000, start=0, mode="Automatic",
                 scene_gain=1.0, iso="3200", shutter="1/8", af_nodes=True, manual_node=True,
                 slip_after_commands=None, model=A7III, tiles=None):
        self.curve, self.near_rail, self.far_rail, self.pos = curve, near_rail, far_rail, start
        # pos -> 3x4 per-tile sharpness; the default is a flat grid with no focus evidence.
        self.tiles = tiles or (lambda pos: [[10.0] * 4 for _ in range(3)])
        self.stale_tiles = None
        self.tiles_by_path: dict[str, list] = {}
        self.mode, self.scene_gain = mode, scene_gain
        self.settings = {"iso": iso, "shutter": shutter, "aperture": "f/4", "white_balance": "Auto",
                         "exposure_comp": "0", "focus_mode": mode, "image_quality": "RAW+JPEG"}
        self.af_nodes, self.manual_node, self.model = af_nodes, manual_node, model
        self.slip_after_commands = slip_after_commands
        self.events: list = []
        self.commands = 0
        self.stale: float | None = None  # sharpness the liveview still shows after a step
        self.sharpness_by_path: dict[str, float] = {}
        self.luma_by_path: dict[str, float] = {}
        self.written = 0

    # --- capabilities / mode (same shape as FakeCamera) ---
    def focus_capabilities(self):
        self.events.append("capabilities")
        nodes = {"focusmode": {"value": self.mode, "readonly": False, "type": 5},
                 "focusarea": None, "focusmagnifier": None,
                 "manualfocus": ({"value": "0", "readonly": False, "type": 3, "range": [-7.0, 7.0, 1.0]}
                                 if self.manual_node else None),
                 "autofocus": {"value": "2", "readonly": False, "type": 4} if self.af_nodes else None,
                 "focusindication": {"value": "Unlock", "readonly": True, "type": 5} if self.af_nodes else None}
        return {"model": self.model, "port": PORT, "nodes": nodes, "manual_focus_route": bool(self.manual_node)}

    def get_focus_mode(self):
        self.events.append("get_mode")
        return self.mode

    def set_focus_mode(self, name):
        self.events.append(("set_mode", name))
        self.mode = self.settings["focus_mode"] = name
        return name

    def autofocus_half_press(self, hold_s):
        self.events.append(("af", hold_s))
        return "Focus Locked"

    # --- exposure settings ---
    def get_settings(self):
        self.events.append("get_settings")
        return dict(self.settings)

    def set_setting(self, key, value, max_seconds=8.0):
        self.events.append(("set", key, str(value)))
        self.settings[key] = str(value)
        return str(value)

    def list_options(self, key):
        self.events.append(("options", key))
        assert key == "shutter"
        return list(SHUTTERS)

    # --- focus motor ---
    def manual_focus_step(self, value):
        assert isinstance(value, int) and value != 0 and -7 <= value <= 7
        self.events.append(("step", value))
        if self.stale is None:
            self.stale = self.curve(self.pos)
            self.stale_tiles = self.tiles(self.pos)
        self.commands += 1
        move = value
        if self.slip_after_commands is not None and self.commands > self.slip_after_commands:
            move = value - (1 if value > 0 else -1)  # irreproducible lens response
        self.pos = max(self.near_rail, min(self.far_rail, self.pos + move))
        return {"node": "manualfocus", "sent": value, "readback": "0", "elapsed_s": 0.01}

    # --- liveview (never the shutter) ---
    def luma(self) -> float:
        seconds = sweep_tool.shutter_seconds(self.settings["shutter"])
        value = 128.0 * (seconds / (1 / 60)) * (float(self.settings["iso"]) / 400.0) * self.scene_gain
        return max(0.0, min(255.0, value))

    def capture_preview(self, dest: Path):
        self.events.append(("preview", dest.name))
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(_jpeg_bytes(seed=self.written))
        self.written += 1
        sharpness = self.stale if self.stale is not None else self.curve(self.pos)
        tiles = self.stale_tiles if self.stale is not None else self.tiles(self.pos)
        self.stale = self.stale_tiles = None
        self.sharpness_by_path[str(dest)] = sharpness
        self.tiles_by_path[str(dest)] = tiles
        self.luma_by_path[str(dest)] = self.luma()
        return dest

    # --- every shutter path must stay untouched ---
    def capture(self, *a, **k): raise AssertionError("shutter")
    def _capture_with_receipt(self, *a, **k): raise AssertionError("shutter")
    def _capture_sony_action(self, *a, **k): raise AssertionError("shutter")
    def record_native(self, *a, **k): raise AssertionError("shutter")
    def record_clip(self, *a, **k): raise AssertionError("shutter")


@pytest.fixture
def instant(monkeypatch, module_time):
    """Zero the settle sleep and route the image metrics to the fake's per-path truth."""
    monkeypatch.setattr(sweep_tool, "STEP_SETTLE_S", 0)
    slept = []
    module_time(sweep_tool, sleep=slept.append)

    def wire(camera):
        monkeypatch.setattr(sweep_tool, "preview_sharpness", lambda p: camera.sharpness_by_path[str(p)])
        monkeypatch.setattr(sweep_tool, "preview_mean_luma", lambda p: camera.luma_by_path[str(p)])
        monkeypatch.setattr(sweep_tool, "preview_tile_sharpness", lambda p: camera.tiles_by_path[str(p)])
        return camera
    wire.slept = slept
    return wire


def steps(events):
    return [e[1] for e in events if isinstance(e, tuple) and e[0] == "step"]


# --- sweep ---------------------------------------------------------------------------------
def test_sweep_detects_both_plateaus_and_returns_by_the_exact_inverse(instant, tmp_path):
    cam = instant(FakeLensCamera())
    sweep = sweep_tool.run_sweep(cam, step=3, dest_dir=tmp_path / "sweep")
    assert sweep["status"] == "complete" and sweep["schema"] == "focus_sweep/2"
    assert instant.slept == []  # STEP_SETTLE_S = 0 means no sleep at all
    assert set(steps(cam.events)) == {-3, 3}  # only step-sized commands, ever
    # Near walk: the metric bottoms out at -24; two flat reads later it stops at -30.
    assert sweep["near_walk"] == {"commands": 10, "step": -3, "stop_reason": "plateau", "end_offset": -30}
    assert sweep["near_rail_offset"] == -30
    # Far walk from -30 crosses the peak and flattens at +36/+39.
    assert sweep["far_walk"] == {"commands": 23, "step": 3, "stop_reason": "plateau", "end_offset": 39}
    assert sweep["far_rail_offset"] == 39
    far = [p for p in sweep["curve"] if p["walk"] == "far"]
    assert [p["offset"] for p in far] == [-30 + 3 * k for k in range(1, 24)]
    assert [p["from_near_rail"] for p in far] == [3 * k for k in range(1, 24)]  # informational only
    # Every sample equals the lens truth at that position: the stale frame was drained.
    for point in sweep["curve"]:
        assert point["sharpness"] == pytest.approx(bell(point["offset"]))
    assert sweep["peak_offset"] == 6 and sweep["peak_sharpness"] == pytest.approx(bell(6))
    # 11 far commands were sent after the peak sample; the return is their exact inverse.
    assert sweep["return_check"]["commands_sent"] == [-3] * 11
    assert sweep["return_check"]["offset"] == 6 and cam.pos == 6
    assert sweep["return_check"]["ratio"] == pytest.approx(1.0) and sweep["return_verified"] is True
    assert all(p["label"] != "return" for p in sweep["curve"])  # the check is not a sample
    for point in sweep["curve"]:
        assert Path(point["preview"]["path"]).exists() and point["preview"]["bytes"] > 0
    assert sweep["calibration"] == "scene-relative sharpness curve; offsets are cumulative commands, not distances"
    on_disk = json.loads((tmp_path / "sweep" / "sweep.json").read_text())
    assert on_disk["status"] == "complete" and on_disk["restoration"]["matches_original"] is True
    assert on_disk["shutter_fired"] is False and on_disk["lens"] is None
    assert "rail" not in json.dumps([on_disk["near_walk"], on_disk["far_walk"]])


def test_sweep_never_calls_a_shutter_and_restores_in_order(instant, tmp_path):
    cam = instant(FakeLensCamera())
    sweep_tool.run_sweep(cam, step=3, dest_dir=tmp_path)
    assert not any(isinstance(e, tuple) and e[0] == "capture" for e in cam.events)
    head = cam.events[:9]
    assert head == ["capabilities", "get_mode", "get_settings", ("set", "iso", "400"), ("set", "shutter", "1/60"),
                    ("preview", "preview_00_exposure.jpg"), ("af", 1.0), ("set_mode", "Manual"),
                    ("preview", "preview_01_stale.jpg")]
    assert cam.events[9] == ("preview", "preview_02_start.jpg")
    assert cam.events[10:13] == [("step", -3), ("preview", "preview_03_stale.jpg"), ("preview", "preview_04_near01.jpg")]
    assert cam.events[-6:] == [("set_mode", "Automatic"), ("af", 1.0), ("set", "iso", "3200"),
                               ("set", "shutter", "1/8"), "get_settings", "get_mode"]
    assert cam.settings["iso"] == "3200" and cam.settings["shutter"] == "1/8" and cam.mode == "Automatic"


def test_sweep_hits_the_command_caps_when_the_curve_never_flattens(instant, tmp_path):
    cam = instant(FakeLensCamera(curve=lambda pos: 2000.0 + 50.0 * pos))
    sweep = sweep_tool.run_sweep(cam, step=3, dest_dir=tmp_path)
    assert sweep["near_walk"]["commands"] == sweep_tool.NEAR_CAP == 12
    assert sweep["near_walk"]["stop_reason"] == "cap" and sweep["near_rail_offset"] == -36
    assert sweep["far_walk"]["commands"] == sweep_tool.FAR_CAP == 24
    assert sweep["far_walk"]["stop_reason"] == "cap" and sweep["far_walk"]["end_offset"] == 36
    # A far walk that ends on its cap explores on until the (linear) centre changes by less than the
    # noise floor per step with every tile flat: two such steps read as a static image.
    extension = sweep["far_extension"]
    assert extension["explore"] is True and extension["stop_reason"] == "image_static"
    assert 2 <= extension["commands"] <= sweep_tool.FAR_EXTENSION_CAP
    assert sweep["far_rail_offset"] == 36 + 3 * extension["commands"] == sweep["peak_offset"]
    assert sweep["return_check"]["commands_sent"] == []
    assert sweep["return_verified"] is True and sweep["status"] == "complete"


def test_sweep_peak_at_start_returns_through_the_whole_far_walk(instant, tmp_path):
    # Like focus-sweep-03: the AF-locked start is the sharpest sample of all.
    cam = instant(FakeLensCamera(curve=lambda pos: bell(pos, peak=0.0)))
    sweep = sweep_tool.run_sweep(cam, step=3, dest_dir=tmp_path)
    assert sweep["peak_offset"] == 0  # the start sample wins its tie with the far-walk re-sample
    since_peak = sweep["near_walk"]["commands"] + sweep["far_walk"]["commands"]
    check = sweep["return_check"]
    assert check["commands_planned"] == since_peak
    # The inverse route first retraces the far walk, which crosses offset 0: the walk-back stops
    # there, where the sample matches the peak, instead of retracing the near walk too.
    far_end = sweep["far_walk"]["end_offset"]
    assert check["commands_sent"] == [-3] * (far_end // 3) and check["stop_reason"] == "matched_peak"
    assert sweep["return_verified"] is True and cam.pos == 0 and check["offset"] == 0


def test_sweep_records_a_return_mismatch_honestly(instant, tmp_path):
    # A lens that slips one command per write on the way back never reaches the peak: say so.
    slipping = instant(FakeLensCamera(slip_after_commands=10 + 23))
    sweep = sweep_tool.run_sweep(slipping, step=3, dest_dir=tmp_path / "slip")
    assert slipping.pos == 39 - 22 and sweep["return_check"]["measured_sharpness"] == pytest.approx(bell(17))
    assert sweep["return_verified"] is False and sweep["return_check"]["stop_reason"] == "route_end"
    assert len(sweep["return_check"]["commands_sent"]) == sweep["return_check"]["commands_planned"] == 11
    # Each return sample sits beside the outbound sample at the offset the sweep believes it is at.
    walk = sweep["return_check"]["walk"]
    assert walk[0]["offset"] == 36 and walk[0]["outbound_sharpness"] == pytest.approx(bell(36))
    assert walk[0]["sharpness"] == pytest.approx(bell(37)) and walk[-1]["offset"] == 6


def test_a_far_rail_absorbing_commands_no_longer_sends_the_return_past_the_peak(instant, tmp_path):
    # Commands at a far rail move nothing, so the full inverse route would overshoot toward near
    # (the old burst return reported ratio < 0.5 here). Walking back while measuring stops at the peak.
    railed = instant(FakeLensCamera(near_rail=-20, far_rail=30))
    sweep = sweep_tool.run_sweep(railed, step=3, dest_dir=tmp_path)
    check = sweep["return_check"]
    assert sweep["status"] == "complete" and sweep["return_verified"] is True
    assert check["stop_reason"] == "matched_peak" and len(check["commands_sent"]) < check["commands_planned"]
    assert abs(railed.pos - 5) <= 3 and check["ratio"] == pytest.approx(bell(railed.pos) / sweep["peak_sharpness"])
    assert all(abs(command) == 3 for command in check["commands_sent"])


class BurstDroppingLens(FakeLensCamera):
    """A lens that ignores a focus command sent while it is still moving: one sent right after
    another, with no settled preview in between (the live 2026-09-29 returns landed 9 commands
    from the far end after a 22-23 command burst)."""

    def manual_focus_step(self, value):
        busy = bool(self.events) and isinstance(self.events[-1], tuple) and self.events[-1][0] == "step"
        if busy:
            self.events.append(("dropped", value))
            return {"node": "manualfocus", "sent": value, "readback": "0", "elapsed_s": 0.01}
        return super().manual_focus_step(value)


def test_the_return_sends_one_settled_command_at_a_time_so_none_is_dropped(instant, tmp_path):
    cam = instant(BurstDroppingLens())
    sweep = sweep_tool.run_sweep(cam, step=3, dest_dir=tmp_path)
    assert not any(isinstance(e, tuple) and e[0] == "dropped" for e in cam.events)
    first = next(i for i, e in enumerate(cam.events)
                 if isinstance(e, tuple) and e[0] == "preview" and "return01" in e[1])
    assert cam.events[first - 2] == ("step", -3) and "stale" in cam.events[first - 1][1]  # settled, drained, measured
    check = sweep["return_check"]
    assert sweep["return_verified"] is True and cam.pos == sweep["peak_offset"] == check["offset"]
    assert [sample["offset"] for sample in check["walk"]] == list(range(36, 5, -3))
    for sample in check["walk"]:  # every settled step reproduced the outbound curve
        assert sample["sharpness"] == pytest.approx(sample["outbound_sharpness"])
    assert all(point["walk"] != "return" for point in sweep["curve"])
    on_disk = json.loads((tmp_path / sweep_tool.SWEEP_NAME).read_text())
    assert on_disk["return_check"]["walk"] == check["walk"]


def test_sweep_finds_a_mid_tone_shutter_one_stop_at_a_time(instant, tmp_path):
    bright = instant(FakeLensCamera(scene_gain=4.0))
    sweep = sweep_tool.run_sweep(bright, step=3, dest_dir=tmp_path / "bright")
    tries = sweep["preview_exposure"]["tries"]
    assert [t["shutter"] for t in tries] == ["1/60", "1/125", "1/250"]
    assert tries[0]["mean_luma"] == 255.0 and 40 <= tries[-1]["mean_luma"] <= 190
    assert sweep["preview_exposure"]["shutter"] == "1/250" and sweep["preview_exposure"]["iso"] == "400"
    assert [e for e in bright.events if isinstance(e, tuple) and e[0] == "set"][:4] == [
        ("set", "iso", "400"), ("set", "shutter", "1/60"), ("set", "shutter", "1/125"), ("set", "shutter", "1/250")]
    dark = instant(FakeLensCamera(scene_gain=0.1))
    sweep = sweep_tool.run_sweep(dark, step=3, dest_dir=tmp_path / "dark")
    assert [t["shutter"] for t in sweep["preview_exposure"]["tries"]] == ["1/60", "1/30", "1/15"]
    assert dark.settings["shutter"] == "1/8"  # restored


def test_sweep_refuses_a_scene_it_cannot_bring_to_mid_tone_and_still_restores(instant, tmp_path):
    cam = instant(FakeLensCamera(scene_gain=1000.0))
    with pytest.raises(sweep_tool.FocusSweepError, match="stayed outside"):
        sweep_tool.run_sweep(cam, step=3, dest_dir=tmp_path)
    assert steps(cam.events) == [] and cam.mode == "Automatic"
    assert cam.settings["iso"] == "3200" and cam.settings["shutter"] == "1/8"
    on_disk = json.loads((tmp_path / "sweep.json").read_text())
    assert on_disk["status"] == "failed" and "FocusSweepError" in on_disk["error"]
    assert len(on_disk["preview_exposure"]["tries"]) == sweep_tool.MAX_EXPOSURE_TRIES
    assert on_disk["restoration"]["attempted"] is True and on_disk["restoration"]["error"] is None


def test_sweep_refuses_bodies_without_the_route_before_any_write(instant, tmp_path):
    cam = instant(FakeLensCamera(manual_node=False, model=A7II))
    with pytest.raises(sweep_tool.FocusSweepError, match="ILCE-7M2"):
        sweep_tool.run_sweep(cam, step=3, dest_dir=tmp_path / "out")
    assert cam.events == ["capabilities"] and not (tmp_path / "out").exists()
    for bad in (0, 8, True, 2.0):
        with pytest.raises(ValueError):
            sweep_tool.run_sweep(cam, step=bad, dest_dir=tmp_path / "out")


def test_sweep_refuses_a_reused_output_directory(instant, tmp_path):
    first = instant(FakeLensCamera())
    sweep_tool.run_sweep(first, step=3, dest_dir=tmp_path)
    cam = instant(FakeLensCamera())
    with pytest.raises(sweep_tool.FocusSweepError, match="fresh"):
        sweep_tool.run_sweep(cam, step=3, dest_dir=tmp_path)
    assert cam.events == ["capabilities"]


def test_manual_original_mode_skips_both_half_presses(instant, tmp_path):
    cam = instant(FakeLensCamera(mode="Manual"))
    sweep = sweep_tool.run_sweep(cam, step=2, dest_dir=tmp_path)
    assert not any(isinstance(e, tuple) and e[0] == "af" for e in cam.events)
    assert sweep["start_af"] is None and sweep["restoration"]["af_half_press"] is None
    assert set(steps(cam.events)) == {-2, 2}


def test_shutter_one_stop_helper():
    assert sweep_tool.shutter_one_stop(SHUTTERS, "1/60", brighter=False) == "1/125"
    assert sweep_tool.shutter_one_stop(SHUTTERS, "1/60", brighter=True) == "1/30"
    assert sweep_tool.shutter_one_stop(SHUTTERS, "1", brighter=True) is None  # bounded at 1 s
    assert sweep_tool.shutter_one_stop(SHUTTERS, "Bulb", brighter=True) is None
    assert sweep_tool.shutter_seconds('2"') == 2.0 and sweep_tool.shutter_seconds("1/0") is None


def test_sweep_cli_prints_a_summary_and_writes_sweep_json(instant, monkeypatch, capsys, tmp_path):
    class Ctx(FakeLensCamera):
        def __enter__(self): return self
        def __exit__(self, *exc): return False
    created = []

    def factory(model=None, port=None, serial=None):
        created.append(instant(Ctx(model=model)))
        created[-1].serial = serial
        return created[-1]
    monkeypatch.setattr(sweep_tool, "Camera", factory)
    code = sweep_tool.main(["--model", A7III, "--port", PORT, "--serial", SERIAL, "--step", "3",
                            "--out", str(tmp_path / "s")])
    payload = json.loads(capsys.readouterr().out)
    assert code == 0 and payload["status"] == "complete" and payload["return_verified"] is True
    assert created[-1].serial == SERIAL  # the body is proven by its serial before any write
    assert payload["preview_exposure"] == {"iso": "400", "shutter": "1/60", "mean_luma": 128.0}
    assert payload["walks"]["near"]["stop_reason"] == "plateau"
    assert Path(payload["sweep_path"]) == tmp_path / "s" / "sweep.json"
    assert sweep_tool.main(["--model", A7III, "--port", PORT, "--serial", SERIAL, "--step", "9",
                            "--out", str(tmp_path)]) == 2


def test_sweep_cli_without_serial_constructs_no_camera(monkeypatch, capsys, tmp_path):
    constructed = []
    monkeypatch.setattr(sweep_tool, "Camera", lambda **kwargs: constructed.append(kwargs))
    code = sweep_tool.main(["--model", A7III, "--port", PORT, "--step", "3", "--out", str(tmp_path / "s")])
    payload = json.loads(capsys.readouterr().out)
    assert code == 2 and payload["status"] == "invalid" and "--serial is required" in payload["error"]
    assert constructed == []  # no connection, so no focus/ISO/shutter write on an unproven body
    assert not (tmp_path / "s").exists()


def test_sweep_cli_identity_mismatch_refuses_before_any_write(monkeypatch, capsys, tmp_path):
    from sonyremote.camera import CameraIdentityError
    bodies = []

    class Mismatched(FakeLensCamera):
        def __enter__(self):  # Camera(serial=...) proves the serial on connect, before any write
            raise CameraIdentityError("another body holds this port", reason="serial_mismatch",
                                      expected_serial=SERIAL, observed_serial="A7II00SERIAL",
                                      model=A7III, port=PORT)
        def __exit__(self, *exc): return False

    def factory(model=None, port=None, serial=None):
        bodies.append(Mismatched(model=model))
        bodies[-1].serial = serial
        return bodies[-1]
    monkeypatch.setattr(sweep_tool, "Camera", factory)
    code = sweep_tool.main(["--model", A7III, "--port", PORT, "--serial", SERIAL, "--step", "3",
                            "--out", str(tmp_path / "s")])
    payload = json.loads(capsys.readouterr().out)
    assert code == 1 and payload["status"] == "identity_refused"
    assert payload["identity_refusal"]["reason"] == "serial_mismatch"
    assert payload["identity_refusal"]["may_have_fired"] is False
    assert len(bodies) == 1 and bodies[0].serial == SERIAL  # one attempt, never retried elsewhere
    assert bodies[0].events == []  # nothing read, nothing written
    assert not (tmp_path / "s").exists()


# --- plan_from_sweep -----------------------------------------------------------------------
def synthetic_sweep(curve: dict[int, float], *, step=3, stop="plateau"):
    """A focus_sweep/1 document in cumulative coordinates (as run_sweep writes it)."""
    offsets = sorted(curve)
    points = [{"label": "start", "walk": None, "offset": 0, "from_near_rail": None, "sharpness": curve.get(0, 0.0)}]
    points += [{"label": f"near{i:02d}", "walk": "near", "offset": o, "from_near_rail": None, "sharpness": curve[o]}
               for i, o in enumerate([o for o in reversed(offsets) if o < 0], 1)]
    points += [{"label": f"far{i:02d}", "walk": "far", "offset": o, "from_near_rail": o - offsets[0],
                "sharpness": curve[o]} for i, o in enumerate([o for o in offsets if o > offsets[0]], 1)]
    return {"schema": "focus_sweep/1", "step": step, "near_rail_offset": offsets[0], "far_rail_offset": offsets[-1],
            "near_walk": {"stop_reason": stop}, "curve": points, "calibration": sweep_tool.CALIBRATION_NOTE}


LENS_SWEEP = synthetic_sweep({3 * i: bell(3 * i) for i in range(-10, 14)})  # -30..39, peak at +6


@pytest.mark.parametrize("frames,expected", [
    (2, [0, 6]), (3, [0, 6, 9]), (4, [0, 3, 6, 9]), (5, [-3, 0, 3, 6, 9]),
])
def test_plan_spans_the_coverage_band_and_always_includes_the_peak(frames, expected):
    plan = focus_bracket.plan_from_sweep(LENS_SWEEP, frames)
    assert [p["cumulative_offset"] for p in plan["positions"]] == expected
    assert plan["peak_offset"] == 6 and 6 in expected and plan["step"] == 3
    assert plan["band"] == [expected[0], 9] and plan["threshold"] == pytest.approx(0.5 * bell(6))  # widened for 5
    assert plan["initial_move"] == expected[0]
    assert plan["commands"] == [b - a for a, b in zip(expected, expected[1:])]
    assert all(c % 3 == 0 for c in plan["commands"]) and plan["initial_move"] % 3 == 0
    assert [p["command_after"] for p in plan["positions"]] == plan["commands"] + [None]
    assert [p["predicted_sharpness"] for p in plan["positions"]] == [pytest.approx(bell(o)) for o in expected]
    assert plan["source"] == "sweep" and plan["calibration"] == sweep_tool.CALIBRATION_NOTE
    assert "AF half-press" in plan["assumption"] and "step magnitude" in plan["assumption"]


def test_plan_coverage_edge_cases():
    only_peak = focus_bracket.plan_from_sweep(LENS_SWEEP, 3, coverage=1.0)
    assert [p["cumulative_offset"] for p in only_peak["positions"]] == [3, 6, 9]
    assert only_peak["band"] == [3, 9] and only_peak["initial_move"] == 3
    everything = focus_bracket.plan_from_sweep(LENS_SWEEP, 2, coverage=0.01)
    assert [p["cumulative_offset"] for p in everything["positions"]] == [-30, 6]
    assert everything["initial_move"] == -30 and everything["commands"] == [36]
    wide = focus_bracket.plan_from_sweep(LENS_SWEEP, 7, coverage=0.01)
    offsets = [p["cumulative_offset"] for p in wide["positions"]]
    assert offsets[0] == -30 and offsets[-1] == 39 and 6 in offsets and len(set(offsets)) == 7
    for bad in (0.0, 1.5, -0.1, float("nan"), True):
        with pytest.raises(ValueError):
            focus_bracket.plan_from_sweep(LENS_SWEEP, 3, coverage=bad)


def test_plan_rejects_bad_frames_and_malformed_sweeps():
    for frames in (1, 8, True, 3.0):
        with pytest.raises(ValueError):
            focus_bracket.plan_from_sweep(LENS_SWEEP, frames)
    with pytest.raises(ValueError, match="focus_sweep/1"):
        focus_bracket.plan_from_sweep({"schema": "other"}, 3)
    with pytest.raises(ValueError, match="step"):
        focus_bracket.plan_from_sweep({"schema": "focus_sweep/1", "curve": LENS_SWEEP["curve"]}, 3)
    with pytest.raises(ValueError, match="not a multiple"):
        focus_bracket.plan_from_sweep({**LENS_SWEEP, "step": 2}, 3)
    with pytest.raises(ValueError, match="fewer than two"):
        focus_bracket.plan_from_sweep({**LENS_SWEEP, "curve": LENS_SWEEP["curve"][:1]}, 3)
    with pytest.raises(ValueError, match="cannot place"):
        focus_bracket.plan_from_sweep(synthetic_sweep({0: 700.0, 3: 100.0}), 3)  # two positions only
    capped = focus_bracket.plan_from_sweep(synthetic_sweep({-3: 100.0, 0: 700.0, 3: 400.0}, stop="cap"), 2)
    assert [p["cumulative_offset"] for p in capped["positions"]] == [0, 3] and capped["initial_move"] == 0


def test_step_commands_only_ever_repeats_the_sweep_step():
    assert focus_bracket.step_commands(36, 3) == [3] * 12
    assert focus_bracket.step_commands(-9, 3) == [-3] * 3
    assert focus_bracket.step_commands(0, 3) == [] and focus_bracket.step_commands(7, 7) == [7]
    for delta, step in ((7, 3), (True, 3), (3, 0), (3, 8), (2.0, 1)):
        with pytest.raises(ValueError):
            focus_bracket.step_commands(delta, step)
    assert not hasattr(focus_bracket, "split_command")  # +-7 chunking is gone from the plan path


@pytest.mark.skipif(not LIVE_SWEEP.exists(), reason="private live sweep receipt not present")
def test_plan_from_the_live_sweep_03_receipt():
    sweep = json.loads(LIVE_SWEEP.read_text())
    plan = focus_bracket.plan_from_sweep(sweep, 3)
    assert plan["step"] == 3 and plan["peak_offset"] == 0
    assert [p["cumulative_offset"] for p in plan["positions"]] == [-9, 0, 6]
    assert plan["initial_move"] == -9 and plan["commands"] == [9, 6]
    assert focus_bracket.step_commands(plan["initial_move"], 3) == [-3] * 3


# --- run_bracket with an explicit plan -----------------------------------------------------
def test_run_bracket_executes_the_sweep_plan_with_step_sized_commands(tmp_path, monkeypatch, module_time):
    module_time(focus_bracket, sleep=lambda _: None)
    camera = FakeCamera()
    shutter = ShutterCounter(camera)
    plan = focus_bracket.plan_from_sweep(LENS_SWEEP, 5)  # -3, 0, 3, 6, 9
    manifest = focus_bracket.run_bracket(camera, plan=plan, dest_dir=tmp_path, capture=shutter)
    assert camera.events == [
        "capabilities", "get_mode", ("set_mode", "Manual"),
        ("step", -3), ("preview", "preview_00_stale.jpg"),
        ("preview", "preview_00.jpg"), ("capture", 0), ("step", 3), ("preview", "preview_01_stale.jpg"),
        ("preview", "preview_01.jpg"), ("capture", 1), ("step", 3), ("preview", "preview_02_stale.jpg"),
        ("preview", "preview_02.jpg"), ("capture", 2), ("step", 3), ("preview", "preview_03_stale.jpg"),
        ("preview", "preview_03.jpg"), ("capture", 3), ("step", 3), ("preview", "preview_04_stale.jpg"),
        ("preview", "preview_04.jpg"), ("capture", 4),
        ("set_mode", "Automatic"), ("af", 1.0),
    ]
    assert manifest["status"] == "complete" and shutter.calls == 5
    assert [f["cumulative_offset"] for f in manifest["frames"]] == [-3, 0, 3, 6, 9]
    assert manifest["steps_sent"][0]["before_frame"] == 0
    assert manifest["steps_sent"][0]["drained_preview"] == str(tmp_path / "preview_00_stale.jpg")
    assert [s["after_frame"] for s in manifest["steps_sent"][1:]] == [0, 1, 2, 3]


def test_run_bracket_repeats_step_commands_for_multi_step_moves(tmp_path, monkeypatch, module_time):
    module_time(focus_bracket, sleep=lambda _: None)
    camera = FakeCamera()
    plan = focus_bracket.plan_from_sweep(LENS_SWEEP, 2, coverage=0.01)  # -30 then +36
    manifest = focus_bracket.run_bracket(camera, plan=plan, dest_dir=tmp_path, capture=ShutterCounter(camera))
    assert steps(camera.events) == [-3] * 10 + [3] * 12
    assert set(abs(s) for s in steps(camera.events)) == {3}
    # One settle+drain per move, after its last command.
    drained = [s.get("drained_preview") for s in manifest["steps_sent"]]
    assert drained[9] == str(tmp_path / "preview_00_stale.jpg") and drained[:9] == [None] * 9
    assert drained[21] == str(tmp_path / "preview_01_stale.jpg") and drained[10:21] == [None] * 11
    assert [f["cumulative_offset"] for f in manifest["frames"]] == [-30, 6]
    on_disk = json.loads((tmp_path / focus_bracket.MANIFEST_NAME).read_text())
    assert on_disk["plan"]["initial_move"] == -30 and on_disk["plan"]["commands"] == [36]


def test_run_bracket_rejects_mixed_or_malformed_plans_before_touching_the_camera(tmp_path):
    camera = FakeCamera()
    plan = focus_bracket.plan_from_sweep(LENS_SWEEP, 2)
    with pytest.raises(ValueError, match="not both"):
        focus_bracket.run_bracket(camera, plan=plan, frames=2, dest_dir=tmp_path, capture=ShutterCounter(camera))
    two = [{"index": 0, "command_after": 3}, {"index": 1, "command_after": None}]
    for broken in ({"step": 3, "positions": []},
                   {"positions": two},  # no step
                   {"step": 3, "positions": [{"index": 0, "command_after": 0}, {"index": 1, "command_after": None}]},
                   {"step": 3, "positions": [{"index": 0, "command_after": 3}, {"index": 1, "command_after": 3}]},
                   {"step": 3, "positions": [{"index": 0, "command_after": 4}, {"index": 1, "command_after": None}]},
                   {"step": 3, "initial_move": 2, "positions": two},
                   {"step": 3, "initial_move": [3], "positions": two}):
        with pytest.raises(ValueError):
            focus_bracket.run_bracket(camera, plan=broken, dest_dir=tmp_path, capture=ShutterCounter(camera))
    assert camera.events == []


def test_uniform_plan_path_is_unchanged_by_the_explicit_plan_support(tmp_path, monkeypatch, module_time):
    module_time(focus_bracket, sleep=lambda _: None)
    camera = FakeCamera()
    manifest = focus_bracket.run_bracket(camera, frames=2, step=4, direction="far",
                                         dest_dir=tmp_path, capture=ShutterCounter(camera))
    assert "initial_move" not in manifest["plan"] and "source" not in manifest["plan"]
    assert manifest["steps_sent"] == [{"after_frame": 0, "node": "manualfocus", "sent": 4, "readback": "0",
                                       "elapsed_s": 0.01, "drained_preview": str(tmp_path / "preview_01_stale.jpg")}]
    assert not list(tmp_path.glob("preview_00_stale*"))


# --- bracket CLI with --sweep --------------------------------------------------------------
class _Ctx(FakeCamera):
    def __init__(self, model, port, **kw):
        super().__init__(model=model, **kw)
        self.shutter = ShutterCounter(self)

    def __enter__(self): return self
    def __exit__(self, *exc): return False
    def capture(self, dest_dir=None): return self.shutter(dest_dir)


def test_bracket_cli_dry_run_prints_the_sweep_plan(monkeypatch, capsys, tmp_path):
    sweep_path = tmp_path / "sweep.json"
    sweep_path.write_text(json.dumps(LENS_SWEEP))
    created = []

    def factory(model=None, port=None):
        created.append(_Ctx(model, port))
        return created[-1]
    monkeypatch.setattr(bracket_tool, "Camera", factory)
    code = bracket_tool.main(["--model", A7III, "--port", PORT, "--frames", "3", "--sweep", str(sweep_path),
                              "--out", str(tmp_path / "out"), "--dry-run"])
    payload = json.loads(capsys.readouterr().out)
    assert code == 0 and payload["status"] == "dry_run" and payload["setting_writes"] == 0
    assert payload["plan"]["source"] == "sweep" and payload["plan"]["initial_move"] == 0
    assert payload["plan"]["commands"] == [6, 3] and payload["plan"]["sweep_path"] == str(sweep_path)
    assert [p["cumulative_offset"] for p in payload["plan"]["positions"]] == [0, 6, 9]
    assert created[0].events == ["capabilities"] and not (tmp_path / "out").exists()


def test_bracket_cli_runs_the_sweep_plan_and_stacks(monkeypatch, capsys, tmp_path, module_time):
    import numpy as np
    module_time(focus_bracket, sleep=lambda _: None)
    sweep_path = tmp_path / "sweep.json"
    sweep_path.write_text(json.dumps(LENS_SWEEP))
    cameras = []

    def factory(model=None, port=None, serial=None):
        cameras.append(_Ctx(model, port))
        return cameras[-1]
    monkeypatch.setattr(bracket_tool, "Camera", factory)
    monkeypatch.setattr(bracket_tool, "focus_stack", lambda paths: np.full((16, 24, 3), 128, dtype=np.uint8))
    # Firing requires the body's USB serial; without it the tool refuses before any Camera exists.
    code = bracket_tool.main(["--model", A7III, "--port", PORT, "--frames", "2", "--sweep", str(sweep_path),
                              "--coverage", "0.5", "--out", str(tmp_path / "no_serial")])
    capsys.readouterr()
    assert code != 0 and cameras == [], "the bracket fired without a serial"
    out = tmp_path / "bracket"
    code = bracket_tool.main(["--model", A7III, "--port", PORT, "--serial", "A7III0SERIAL", "--frames", "2",
                              "--sweep", str(sweep_path), "--coverage", "0.5", "--baseline", "current",
                              "--out", str(out)])
    payload = json.loads(capsys.readouterr().out)
    assert code == 0 and payload["status"] == "complete" and payload["frames"] == 2
    receipt = json.loads((out / "focus_stack.json").read_text())
    assert [i["cumulative_offset"] for i in receipt["inputs"]] == [0, 6]
    assert receipt["calibration"] == sweep_tool.CALIBRATION_NOTE
    assert steps(cameras[0].events) == [3, 3]


def test_bracket_cli_requires_direction_or_sweep(monkeypatch, capsys, tmp_path):
    monkeypatch.setattr(bracket_tool, "Camera", lambda **kw: pytest.fail("camera must not be opened"))
    code = bracket_tool.main(["--model", A7III, "--port", PORT, "--out", str(tmp_path)])
    assert code == 2 and json.loads(capsys.readouterr().out)["status"] == "invalid"
    code = bracket_tool.main(["--model", A7III, "--port", PORT, "--sweep", str(tmp_path / "missing.json"),
                              "--out", str(tmp_path)])
    assert code == 2 and json.loads(capsys.readouterr().out)["status"] == "invalid"


def test_the_serial_help_points_at_a_listing_that_prints_the_usb_serial(monkeypatch, capsys):
    # Review thread on #76: --serial says where to find it; that command must actually print it.
    import inspect_usb_camera
    rows = [{"model": A7III, "port": PORT, "serial": SERIAL, "serial_status": "present", "method": "autodetect"},
            {"model": A7II, "port": "usb:000,005", "serial": None, "serial_status": "missing", "method": "autodetect"}]
    monkeypatch.setattr(inspect_usb_camera, "detect_identified", lambda: rows)
    monkeypatch.setattr(inspect_usb_camera, "Camera", lambda **kw: pytest.fail("--list must not claim a camera"))
    assert inspect_usb_camera.main(["--list"]) == 0
    listed = json.loads(capsys.readouterr().out)["identified"]
    assert [(row["serial"], row["serial_status"]) for row in listed] == [(SERIAL, "present"), (None, "missing")]
    parser_help = sweep_tool.main.__code__.co_consts
    assert any("inspect_usb_camera.py --list" in str(const) for const in parser_help)


# --- per-tile sweep (focus_sweep/2) and depth-spanning plans ---------------------------------
def flat_tile(pos: float) -> float:
    """A tile with no focus evidence: tiny liveview jitter around a constant level."""
    return 10.0 + (0.2 if int(pos) % 2 else 0.0)


def grid_of(tiles: dict) -> "callable":
    """pos -> 3x4 grid; tiles maps (row, col) -> pos -> sharpness, every other tile is flat."""
    return lambda pos: [[float(tiles[(row, col)](pos)) if (row, col) in tiles else flat_tile(pos)
                         for col in range(4)] for row in range(3)]


def centre_object(pos: float) -> float:  # the near object in the middle of the frame (like 2026-09-29)
    return bell(pos, peak=-3.0, width=6.0, top=700.0, floor=40.0)


TABLETOP = grid_of({(1, 1): centre_object, (1, 2): centre_object,
                    (2, 0): lambda pos: bell(pos, peak=24.0, width=5.0, top=80.0, floor=12.0),   # rear bottle
                    (1, 3): lambda pos: bell(pos, peak=33.0, width=5.0, top=60.0, floor=12.0)})  # background


def tiled_sweep(centre, grid, offsets, *, step=3):
    """A focus_sweep/2 document: synthetic_sweep's samples plus a tile grid on each of them."""
    sweep = synthetic_sweep({offset: centre(offset) for offset in offsets}, step=step)
    sweep["schema"] = "focus_sweep/2"
    sweep["tile_grid"] = {"rows": 3, "cols": 4, "metric": focus_bracket.TILE_METRIC}
    for point in sweep["curve"]:
        point["tiles"] = grid(point["offset"])
    return sweep


LIVE_RANGE = list(range(-36, 37, 3))  # what the 2026-09-29 sweep sampled (both walks hit their caps)


def test_tiled_plan_spans_the_near_centre_object_and_the_far_edge_objects():
    sweep = tiled_sweep(centre_object, TABLETOP, LIVE_RANGE)
    old_way = focus_bracket.plan_from_sweep({**sweep, "schema": "focus_sweep/1"}, 5)
    assert [p["cumulative_offset"] for p in old_way["positions"]] == [-9, -6, -3, 0, 3]  # the defect
    plan = focus_bracket.plan_from_sweep(sweep, 5)
    offsets = [p["cumulative_offset"] for p in plan["positions"]]
    assert plan["method"] == "tiles" and plan["frames_requested"] == 5
    assert offsets == sorted(offsets) and -3 in offsets  # near to far, centre peak kept
    # Each object's own peak; a fourth and fifth frame would sharpen nothing, so none is spent.
    assert offsets == [-3, 24, 33] and plan["frames"] == 3
    targets = {t["id"]: t for t in plan["targets"]}
    assert set(targets) == {"centre", "r1c1", "r1c2", "r2c0", "r1c3"}
    assert targets["r2c0"]["peak_offset"] == 24 and targets["r1c3"]["peak_offset"] == 33
    assert all(t["covered"] and t["band"][0] <= t["best_offset"] <= t["band"][1] for t in plan["targets"])
    assert plan["uncovered"] == [] and plan["unresolved"] == []
    assert sorted(plan["ignored_tiles"]) == sorted(f"r{r}c{c}" for r in range(3) for c in range(4)
                                                   if (r, c) not in {(1, 1), (1, 2), (2, 0), (1, 3)})
    assert {p["cumulative_offset"]: p["covers"] for p in plan["positions"]}[-3] == ["centre", "r1c1", "r1c2"]
    assert plan["initial_move"] == -3 and all(c % 3 == 0 and c > 0 for c in plan["commands"])
    assert [p["predicted_sharpness"] for p in plan["positions"]] == [pytest.approx(centre_object(o)) for o in offsets]
    focus_bracket.validate_plan(plan)


def test_tiled_plan_reports_what_too_few_frames_leave_uncovered():
    narrow = grid_of({(1, 1): lambda pos: bell(pos, peak=-3.0, width=3.0),
                      (2, 0): lambda pos: bell(pos, peak=-27.0, width=3.0, top=90.0, floor=12.0),
                      (1, 3): lambda pos: bell(pos, peak=30.0, width=3.0, top=90.0, floor=12.0)})
    centre = lambda pos: bell(pos, peak=-3.0, width=3.0)  # noqa: E731
    two = focus_bracket.plan_from_sweep(tiled_sweep(centre, narrow, LIVE_RANGE), 2)
    assert two["frames"] == 2 and -3 in [p["cumulative_offset"] for p in two["positions"]]
    assert len(two["uncovered"]) == 1 and two["uncovered"][0] in {"r2c0", "r1c3"}  # named, not dropped
    three = focus_bracket.plan_from_sweep(tiled_sweep(centre, narrow, LIVE_RANGE), 3)
    assert [p["cumulative_offset"] for p in three["positions"]] == [-27, -3, 30] and three["uncovered"] == []


def test_tiled_plan_flags_a_tile_still_rising_at_the_end_of_the_sweep():
    rising = grid_of({(1, 1): centre_object, (1, 3): lambda pos: 20.0 + max(0.0, 2.0 * pos)})
    plan = focus_bracket.plan_from_sweep(tiled_sweep(centre_object, rising, LIVE_RANGE), 3)
    assert plan["unresolved"] == ["r1c3"] and 36 in [p["cumulative_offset"] for p in plan["positions"]]
    edge = {t["id"]: t for t in plan["targets"]}["r1c3"]
    assert edge["peak_at_sweep_edge"] == "far" and edge["covered"] is True


def test_tiled_plan_uses_fewer_frames_when_more_add_nothing():
    same_depth = grid_of({(1, 1): centre_object, (2, 2): centre_object})
    plan = focus_bracket.plan_from_sweep(tiled_sweep(centre_object, same_depth, LIVE_RANGE), 7, coverage=1.0)
    assert plan["method"] == "tiles" and plan["frames_requested"] == 7
    assert [p["cumulative_offset"] for p in plan["positions"]] == [-3, 0] and plan["frames"] == 2  # never below 2
    focus_bracket.validate_plan(plan)


def test_flat_tiles_fall_back_to_the_centre_band_and_old_sweeps_still_load():
    flat = tiled_sweep(lambda pos: bell(pos), grid_of({}), [3 * i for i in range(-10, 14)])
    flat_plan = focus_bracket.plan_from_sweep(flat, 5)
    old_plan = focus_bracket.plan_from_sweep(LENS_SWEEP, 5)
    assert flat_plan["method"] == "centre_band" == old_plan["method"]
    assert [p["cumulative_offset"] for p in flat_plan["positions"]] == [-3, 0, 3, 6, 9] == \
        [p["cumulative_offset"] for p in old_plan["positions"]]
    assert len(flat_plan["ignored_tiles"]) == 12 and "tile_grid" not in old_plan
    assert all("tiles" not in point for point in LENS_SWEEP["curve"]) and LENS_SWEEP["schema"] == "focus_sweep/1"
    # A /1 document that happens to carry stray tile values is still planned on its centre curve.
    stray = tiled_sweep(centre_object, TABLETOP, LIVE_RANGE)
    assert focus_bracket.plan_from_sweep({**stray, "schema": "focus_sweep/1"}, 3)["method"] == "centre_band"


def test_malformed_tile_grids_are_refused():
    sweep = tiled_sweep(centre_object, TABLETOP, LIVE_RANGE)
    for grid in (None, {"rows": 0, "cols": 4}, {"rows": 3, "cols": True}, {"rows": 3}):
        with pytest.raises(ValueError, match="tile_grid"):
            focus_bracket.plan_from_sweep({**sweep, "tile_grid": grid}, 3)
    short = json.loads(json.dumps(sweep))
    short["curve"][4]["tiles"] = short["curve"][4]["tiles"][:2]
    with pytest.raises(ValueError, match="3x4 tile grid"):
        focus_bracket.plan_from_sweep(short, 3)
    bad = json.loads(json.dumps(sweep))
    bad["curve"][4]["tiles"][0][0] = None
    with pytest.raises(ValueError, match="non-finite"):
        focus_bracket.plan_from_sweep(bad, 3)


def test_tile_signal_needs_contrast_and_range_above_the_noise_floor():
    assert focus_bracket.tile_has_signal([10.0, 10.2, 10.0, 10.2]) is False  # jitter
    assert focus_bracket.tile_has_signal([2.0, 2.4, 2.9]) is False  # 1.45x: under the contrast bar
    assert focus_bracket.tile_has_signal([1.0, 2.5]) is False  # 2.5x but only 1.5 above: under 4 floors
    assert focus_bracket.tile_has_signal([11.0, 23.0]) is True
    assert focus_bracket.tile_has_signal([]) is False


def test_preview_tile_sharpness_localises_detail(tmp_path):
    import numpy as np
    from PIL import Image
    pixels = np.full((60, 80), 128, dtype="uint8")
    pixels[40:60, 60:80] = np.random.default_rng(3).integers(0, 256, (20, 20), dtype="uint8")  # bottom-right
    path = tmp_path / "p.png"
    Image.fromarray(pixels).save(path)
    grid = focus_bracket.preview_tile_sharpness(path)
    assert len(grid) == 3 and all(len(row) == 4 for row in grid)
    assert grid[2][3] > 100 * max(grid[0][0], 1e-9) and grid[0][0] == pytest.approx(0.0)


def test_sweep_records_a_tile_grid_on_every_sample_and_ignores_jitter(instant, tmp_path):
    cam = instant(FakeLensCamera())  # every tile flat (with jitter)
    sweep = sweep_tool.run_sweep(cam, step=3, dest_dir=tmp_path)
    assert sweep["tile_grid"] == {"rows": 3, "cols": 4, "metric": focus_bracket.TILE_METRIC}
    assert all(len(p["tiles"]) == 3 and all(len(row) == 4 for row in p["tiles"]) for p in sweep["curve"])
    assert sweep["far_extension"]["commands"] == 0 and sweep["far_extension"]["stop_reason"] == "no_tile_rising"
    assert sweep["far_rail_offset"] == sweep["far_walk"]["end_offset"]


def test_sweep_extends_the_far_walk_past_the_cap_to_a_far_tile_peak(instant, tmp_path):
    background = lambda pos: bell(pos, peak=48.0, width=6.0, top=100.0, floor=12.0)  # noqa: E731
    cam = instant(FakeLensCamera(curve=lambda pos: bell(pos, peak=0.0),
                                 tiles=grid_of({(1, 1): lambda pos: bell(pos, peak=0.0), (1, 3): background})))
    sweep = sweep_tool.run_sweep(cam, step=3, dest_dir=tmp_path)
    assert sweep["far_walk"]["end_offset"] == 36  # where the centre curve alone would have stopped
    extension = sweep["far_extension"]
    assert extension["rising_at_far_walk_end"] == ["r1c3"] and extension["stop_reason"] == "no_tile_rising"
    assert extension["commands"] == 5 and extension["end_offset"] == 51 and sweep["far_rail_offset"] == 51
    extra = [p for p in sweep["curve"] if p["walk"] == "far_extension"]
    assert [p["offset"] for p in extra] == [39, 42, 45, 48, 51]
    assert [p["from_near_rail"] for p in extra] == [o - sweep["near_rail_offset"] for o in (39, 42, 45, 48, 51)]
    assert set(steps(cam.events)) == {-3, 3}  # still only step-sized commands
    assert sweep["peak_offset"] == 0 and cam.pos == 0 and sweep["return_verified"] is True
    plan = focus_bracket.plan_from_sweep(sweep, 3)
    offsets = [p["cumulative_offset"] for p in plan["positions"]]
    assert 0 in offsets and 48 in offsets and plan["uncovered"] == [] and plan["unresolved"] == []


def test_sweep_far_extension_is_bounded(instant, tmp_path):
    forever = lambda pos: 20.0 + max(0.0, 5.0 * pos)  # noqa: E731  (a tile that never stops rising)
    cam = instant(FakeLensCamera(curve=lambda pos: bell(pos, peak=0.0), tiles=grid_of({(1, 3): forever})))
    sweep = sweep_tool.run_sweep(cam, step=3, dest_dir=tmp_path)
    extension = sweep["far_extension"]
    assert extension["commands"] == sweep_tool.FAR_EXTENSION_CAP == 12 and extension["stop_reason"] == "cap"
    assert extension["rising_at_stop"] == ["r1c3"] and sweep["far_rail_offset"] == 36 + 3 * 12
    assert not any(isinstance(e, tuple) and e[0] == "capture" for e in cam.events)
    assert cam.mode == "Automatic" and sweep["restoration"]["matches_original"] is True


def test_bracket_cli_dry_run_prints_tile_coverage(monkeypatch, capsys, tmp_path):
    sweep_path = tmp_path / "sweep.json"
    sweep_path.write_text(json.dumps(tiled_sweep(centre_object, TABLETOP, LIVE_RANGE)))
    monkeypatch.setattr(bracket_tool, "Camera", lambda model=None, port=None: _Ctx(model, port))
    code = bracket_tool.main(["--model", A7III, "--port", PORT, "--frames", "5", "--sweep", str(sweep_path),
                              "--out", str(tmp_path / "out"), "--dry-run"])
    payload = json.loads(capsys.readouterr().out)
    assert code == 0 and payload["setting_writes"] == 0 and payload["plan"]["method"] == "tiles"
    assert payload["plan_note"].startswith("3 frames at offsets -3, 24, 33 (of up to 5).")
    assert "Every target reaches the coverage threshold" in payload["plan_note"]
    assert {t["id"] for t in payload["plan"]["targets"]} >= {"r2c0", "r1c3"} and not (tmp_path / "out").exists()


def test_a_capped_far_walk_probes_on_to_a_subject_still_flat_at_the_cap(instant, tmp_path):
    # The centre is still changing at +36 (cap, not plateau); a far subject is fully defocused
    # there and only starts to show two commands later: only the probe can find it.
    late = lambda pos: 10.0 if pos < 39 else bell(pos, peak=45.0, width=3.0, top=100.0, floor=10.0)  # noqa: E731
    cam = instant(FakeLensCamera(curve=lambda pos: bell(pos, peak=0.0, width=30.0),
                                 tiles=grid_of({(1, 1): lambda pos: bell(pos, peak=0.0, width=30.0), (1, 3): late})))
    sweep = sweep_tool.run_sweep(cam, step=3, dest_dir=tmp_path)
    assert sweep["far_walk"]["stop_reason"] == "cap" and sweep["far_walk"]["end_offset"] == 36
    extension = sweep["far_extension"]
    assert extension["rising_at_far_walk_end"] == [] and extension["explore"] is True
    # Peak at +45, falling from +48; three quiet samples (+48, +51, +54) before it stops.
    assert extension["end_offset"] == 54 and extension["stop_reason"] == "passed_peak"
    assert extension["passed_peak"] == ["r1c3"] and extension["commands"] == 6
    plan = focus_bracket.plan_from_sweep(sweep, 3)
    assert {t["id"]: t["peak_offset"] for t in plan["targets"]}["r1c3"] == 45
    assert 45 in [p["cumulative_offset"] for p in plan["positions"]] and plan["uncovered"] == []


def test_a_plateaued_far_walk_is_not_probed_blindly(instant, tmp_path):
    cam = instant(FakeLensCamera())  # the default bell flattens on both sides: plateau, flat tiles
    sweep = sweep_tool.run_sweep(cam, step=3, dest_dir=tmp_path)
    assert sweep["far_walk"]["stop_reason"] == "plateau" and sweep["far_extension"]["explore"] is False
    assert sweep["far_extension"]["commands"] == 0 and sweep["far_extension"]["stop_reason"] == "no_tile_rising"


def test_tiled_plan_prefers_covering_a_target_over_a_compromise_that_covers_none():
    narrow = grid_of({(1, 1): lambda pos: bell(pos, peak=0.0, width=1.5),
                      (2, 0): lambda pos: bell(pos, peak=6.0, width=1.5, top=90.0, floor=12.0),
                      (1, 3): lambda pos: bell(pos, peak=12.0, width=1.5, top=90.0, floor=12.0)})
    plan = focus_bracket.plan_from_sweep(tiled_sweep(lambda pos: bell(pos, peak=0.0, width=1.5), narrow,
                                                     LIVE_RANGE), 2)
    offsets = [p["cumulative_offset"] for p in plan["positions"]]
    assert offsets in ([0, 6], [0, 12]) and 9 not in offsets
    assert len(plan["uncovered"]) == 1


def test_a_capped_far_walk_explores_across_a_long_flat_gap(instant, tmp_path):
    # The far subject only starts to show 6 commands past the cap: still found within the extension cap.
    late = lambda pos: 10.0 if pos < 54 else bell(pos, peak=60.0, width=3.0, top=100.0, floor=10.0)  # noqa: E731
    cam = instant(FakeLensCamera(curve=lambda pos: bell(pos, peak=0.0, width=30.0),
                                 tiles=grid_of({(1, 1): lambda pos: bell(pos, peak=0.0, width=30.0), (1, 3): late})))
    sweep = sweep_tool.run_sweep(cam, step=3, dest_dir=tmp_path)
    extension = sweep["far_extension"]
    assert extension["stop_reason"] == "passed_peak" and extension["end_offset"] == 69
    assert extension["commands"] == 11 <= sweep_tool.FAR_EXTENSION_CAP
    assert set(steps(cam.events)) == {-3, 3}
    plan = focus_bracket.plan_from_sweep(sweep, 3)
    assert {t["id"]: t["peak_offset"] for t in plan["targets"]}["r1c3"] == 60 and plan["uncovered"] == []


def test_far_exploration_stops_when_the_image_stops_changing(instant, tmp_path):
    # A rail at +40 absorbs commands: two unchanged samples in a row end the walk (never 12 blind pushes).
    cam = instant(FakeLensCamera(curve=lambda pos: 2000.0 + 50.0 * pos, far_rail=40))
    sweep = sweep_tool.run_sweep(cam, step=3, dest_dir=tmp_path)
    extension = sweep["far_extension"]
    assert sweep["far_walk"]["stop_reason"] == "cap" and extension["stop_reason"] == "image_static"
    assert extension["commands"] == 3 and extension["end_offset"] == 45  # lens at +39, +40 (rail), +40


def test_rising_and_passed_peak_rules_ignore_plateau_jitter():
    assert sweep_tool.tile_is_rising([20.0, 20.2, 21.0, 22.9]) is False  # 1.15x overall: no signal yet
    assert sweep_tool.tile_is_rising([11.0, 15.0, 20.2, 21.0, 22.9]) is True  # the live r1c3 climb
    assert sweep_tool.tile_is_rising([40.0, 60.0, 80.0, 100.0, 98.0, 100.0, 102.0]) is False  # +-2 % plateau
    assert sweep_tool.tile_is_rising([40.0, 60.0, 80.0, 100.0]) is True
    assert sweep_tool.tile_passed_peak([10.0, 10.0, 30.0, 110.0, 60.0], since=2) is True
    assert sweep_tool.tile_passed_peak([10.0, 10.0, 30.0, 110.0, 108.0], since=2) is False  # still at the top
    assert sweep_tool.tile_passed_peak([100.0, 60.0, 40.0, 20.0], since=2) is False  # only fell
    assert sweep_tool.tile_passed_peak([10.0, 30.0], since=0) is False


def test_tiled_plan_moves_a_compromise_frame_to_cover_everything():
    # Offsets 0..9: tile A peaks at 3, tile B at 9; a frame at 6 half-covers both and covers neither.
    tiles = {3: [[110.0, 10.0]], 6: [[50.0, 50.0]], 9: [[10.0, 110.0]], 0: [[10.0, 10.0]]}
    sweep = {"schema": "focus_sweep/2", "step": 3, "tile_grid": {"rows": 1, "cols": 2},
             "curve": [{"offset": o, "sharpness": s, "tiles": tiles[o]}
                       for o, s in ((0, 700.0), (3, 300.0), (6, 200.0), (9, 100.0))]}
    plan = focus_bracket.plan_from_sweep(sweep, 3)
    assert [p["cumulative_offset"] for p in plan["positions"]] == [0, 3, 9] and plan["uncovered"] == []


def test_a_peak_tied_with_the_sweep_end_is_unresolved():
    tied = grid_of({(1, 1): centre_object, (1, 3): lambda pos: min(100.0, 10.0 + 10.0 * max(0.0, pos))})
    plan = focus_bracket.plan_from_sweep(tiled_sweep(centre_object, tied, LIVE_RANGE), 3)
    edge = {t["id"]: t for t in plan["targets"]}["r1c3"]
    assert edge["peak_offset"] == 9 and edge["peak_at_sweep_edge"] == "far" and "r1c3" in plan["unresolved"]


def test_negative_tile_values_are_refused_before_the_camera_is_touched(monkeypatch, capsys, tmp_path):
    bad = json.loads(json.dumps(tiled_sweep(centre_object, TABLETOP, LIVE_RANGE)))
    bad["curve"][3]["tiles"][1][3] = -100.0
    with pytest.raises(ValueError, match="negative"):
        focus_bracket.plan_from_sweep(bad, 3)
    sweep_path = tmp_path / "sweep.json"
    sweep_path.write_text(json.dumps(bad))
    created = []
    monkeypatch.setattr(bracket_tool, "Camera", lambda *a, **k: created.append(1))
    code = bracket_tool.main(["--model", A7III, "--port", PORT, "--serial", SERIAL, "--frames", "3",
                              "--sweep", str(sweep_path), "--out", str(tmp_path / "out")])
    assert code == 2 and json.loads(capsys.readouterr().out)["status"] == "invalid" and created == []


def test_far_exploration_keeps_going_after_one_peak_to_find_a_farther_subject(instant, tmp_path):
    # Two subjects beyond the far-walk cap: A peaks at +42, B stays flat until +51 and peaks at +54.
    # A has passed its peak (+45, +48 quiet) before B shows any signal; the walk must not stop there.
    first = lambda pos: bell(pos, peak=42.0, width=2.0, top=100.0, floor=10.0) if pos > 37 else 10.0  # noqa: E731
    second = lambda pos: bell(pos, peak=54.0, width=2.0, top=100.0, floor=10.0) if pos > 49 else 10.0  # noqa: E731
    cam = instant(FakeLensCamera(curve=lambda pos: bell(pos, peak=0.0, width=30.0),
                                 tiles=grid_of({(1, 1): lambda pos: bell(pos, peak=0.0, width=30.0),
                                                (1, 3): first, (2, 0): second})))
    sweep = sweep_tool.run_sweep(cam, step=3, dest_dir=tmp_path)
    extension = sweep["far_extension"]
    assert sweep["far_walk"]["stop_reason"] == "cap" and extension["explore"] is True
    assert extension["stop_reason"] == "passed_peak" and sorted(extension["passed_peak"]) == ["r1c3", "r2c0"]
    assert extension["end_offset"] == 63 and extension["commands"] == 9 <= sweep_tool.FAR_EXTENSION_CAP
    assert set(steps(cam.events)) == {-3, 3}
    plan = focus_bracket.plan_from_sweep(sweep, 3)
    peaks = {t["id"]: t["peak_offset"] for t in plan["targets"]}
    assert peaks["r1c3"] == 42 and peaks["r2c0"] == 54
    offsets = [p["cumulative_offset"] for p in plan["positions"]]
    assert 42 in offsets and 54 in offsets and plan["uncovered"] == []


def test_a_broad_flat_topped_peak_does_not_use_up_the_quiet_stretch(instant, tmp_path):
    # A is sharp and level from +39 to +48 and only falls at +51; B starts to rise at +54.
    # Quiet samples count only after A has passed its peak, so the walk is still going when B appears.
    def broad(pos):
        return 10.0 if pos < 38 else 110.0 if pos < 50 else 100.0
    second = lambda pos: bell(pos, peak=57.0, width=2.0, top=100.0, floor=10.0) if pos > 52 else 10.0  # noqa: E731
    cam = instant(FakeLensCamera(curve=lambda pos: bell(pos, peak=0.0, width=30.0),
                                 tiles=grid_of({(1, 1): lambda pos: bell(pos, peak=0.0, width=30.0),
                                                (1, 3): broad, (2, 0): second})))
    sweep = sweep_tool.run_sweep(cam, step=3, dest_dir=tmp_path)
    extension = sweep["far_extension"]
    assert extension["stop_reason"] == "passed_peak" and "r2c0" in extension["passed_peak"]
    assert extension["end_offset"] == 66 and extension["commands"] == 10 <= sweep_tool.FAR_EXTENSION_CAP
    peaks = {t["id"]: t["peak_offset"] for t in focus_bracket.plan_from_sweep(sweep, 3)["targets"]}
    assert peaks["r2c0"] == 57


# --- placeholder focus mode and per-field restoration (live 2026-09-29) ------------------------
# The A7 III had just woken from auto power-off: the sweep recorded focus_mode "Undefined" (beside
# ISO 0 / 65535/65535 placeholders), restoring it failed, and that failure skipped the ISO and
# shutter restore while sweep.json still said "complete".

FOCUS_CHOICES = ["Manual", "Automatic", "AF-C", "AF-A", "DMF", "Preset Focus"]


class WakingLensCamera(FakeLensCamera):
    """Answers placeholder focus modes for the first `placeholder_reads` reads, then the real mode."""

    def __init__(self, *, placeholders=("Undefined",), **kw):
        super().__init__(**kw)
        self.placeholders = list(placeholders)

    def focus_capabilities(self):
        capabilities = super().focus_capabilities()
        capabilities["nodes"]["focusmode"]["choices"] = list(FOCUS_CHOICES)
        return capabilities

    def get_focus_mode(self):
        self.events.append("get_mode")
        return self.placeholders.pop(0) if self.placeholders else self.mode


def writes(events):
    return [e for e in events if isinstance(e, tuple) and e[0] in ("set", "set_mode", "step", "af")]


def test_an_undefined_focus_mode_after_wake_is_reread_and_the_sweep_proceeds(instant, module_time, tmp_path):
    pauses = []
    module_time(camera_module, sleep=pauses.append)
    cam = instant(WakingLensCamera(placeholders=["Undefined"]))
    sweep = sweep_tool.run_sweep(cam, step=3, dest_dir=tmp_path)
    assert cam.events[:3] == ["capabilities", "get_mode", "get_mode"]  # re-read before any write
    assert pauses == [camera_module.PLACEHOLDER_SETTLE_S]
    assert sweep["original"]["focus_mode"] == "Automatic" and sweep["status"] == "complete"
    assert sweep["original_focus_mode_read"]["placeholder_reads"] == [
        {"read": 1, "fields": ["focus_mode"], "values": {"focus_mode": "Undefined"}}]
    assert sweep["restoration"]["matches_original"] is True and sweep["restore_error"] is None
    assert cam.mode == "Automatic" and cam.settings["iso"] == "3200" and cam.settings["shutter"] == "1/8"


@pytest.mark.parametrize("placeholder", ["Undefined", "Unknown value 0000"])
def test_a_persistent_placeholder_focus_mode_refuses_before_any_write(instant, module_time, tmp_path, placeholder):
    module_time(camera_module, sleep=lambda seconds: None)
    cam = instant(WakingLensCamera(placeholders=[placeholder] * (camera_module.PLACEHOLDER_REREADS + 1)))
    with pytest.raises(PlaceholderSettingsError) as caught:
        sweep_tool.run_sweep(cam, step=3, dest_dir=tmp_path / "out")
    assert writes(cam.events) == [] and steps(cam.events) == []  # no setting write, no motor command
    assert cam.events == ["capabilities"] + ["get_mode"] * (camera_module.PLACEHOLDER_REREADS + 1)
    refusal = caught.value.as_dict()
    assert refusal["reason"] == "placeholder_settings" and refusal["may_have_fired"] is False
    assert refusal["reads"][-1]["values"] == {"focus_mode": placeholder}
    assert not (tmp_path / "out" / sweep_tool.SWEEP_NAME).exists()


def test_the_cli_reports_a_placeholder_focus_mode_refusal_and_writes_nothing(instant, module_time, monkeypatch,
                                                                               capsys, tmp_path):
    module_time(camera_module, sleep=lambda seconds: None)

    class Ctx(WakingLensCamera):
        def __enter__(self): return self
        def __exit__(self, *exc): return False
    created = []

    def factory(model=None, port=None, serial=None):
        created.append(instant(Ctx(placeholders=["Undefined"] * 9)))
        return created[-1]
    monkeypatch.setattr(sweep_tool, "Camera", factory)
    code = sweep_tool.main(["--model", A7III, "--port", PORT, "--serial", SERIAL, "--out", str(tmp_path / "s")])
    payload = json.loads(capsys.readouterr().out)
    assert code == 1 and payload["status"] == "failed" and "placeholder values" in payload["error"]
    assert writes(created[-1].events) == []


def test_one_failed_restore_field_still_restores_the_others_and_flags_the_sweep(instant, tmp_path):
    cam = instant(FakeLensCamera())  # original: Automatic, ISO 3200, 1/8
    real_set_mode = cam.set_focus_mode

    def set_focus_mode(name):
        if name != "Manual":
            cam.events.append(("set_mode_failed", name))
            raise CameraError(f"Could not set focus_mode to {name!r}; camera stayed at 'Manual'.")
        return real_set_mode(name)
    cam.set_focus_mode = set_focus_mode
    sweep = sweep_tool.run_sweep(cam, step=3, dest_dir=tmp_path)
    restoration = sweep["restoration"]
    # The focus-mode failure is recorded, and the AF half-press, ISO and shutter are still attempted.
    assert cam.events[-6:] == [("set_mode_failed", "Automatic"), ("af", 1.0), ("set", "iso", "3200"),
                               ("set", "shutter", "1/8"), "get_settings", "get_mode"]
    assert cam.settings["iso"] == "3200" and cam.settings["shutter"] == "1/8" and cam.mode == "Manual"
    assert restoration["failed"] == ["focus_mode"] and restoration["focus_mode_readback"] is None
    assert restoration["iso_readback"] == "3200" and restoration["shutter_readback"] == "1/8"
    assert restoration["af_half_press"] == {"hold_s": 1.0, "indication": "Focus Locked"}
    assert restoration["readback"]["focus_mode"] == "Manual" and restoration["matches_original"] is False
    assert restoration["error"].startswith("focus_mode: CameraError: Could not set focus_mode")
    assert sweep["status"] == "complete_restoration_failed" and sweep["restore_error"] == restoration["error"]
    on_disk = json.loads((tmp_path / sweep_tool.SWEEP_NAME).read_text())
    assert on_disk["status"] == "complete_restoration_failed" and on_disk["restore_error"]
    assert on_disk["peak_offset"] == sweep["peak_offset"]  # the curve is kept; plans still load it
    focus_bracket.plan_from_sweep(on_disk, 3)


def test_every_restore_field_is_attempted_when_each_one_fails(instant, tmp_path):
    cam = instant(FakeLensCamera())
    real_set, real_set_mode = cam.set_setting, cam.set_focus_mode
    restoring = []

    def set_setting(key, value, max_seconds=8.0):
        if restoring:
            cam.events.append(("set_failed", key))
            raise CameraError(f"{key} write refused")
        return real_set(key, value)

    def set_focus_mode(name):
        if name != "Manual":
            restoring.append(True)
            raise CameraError("focus mode write refused")
        return real_set_mode(name)

    def autofocus_half_press(hold_s):
        cam.events.append(("af_failed", hold_s))
        raise CameraError("half-press refused")
    cam.set_setting, cam.set_focus_mode, cam.autofocus_half_press = set_setting, set_focus_mode, autofocus_half_press
    cam.mode = "Automatic"
    start_failure = None
    try:
        sweep_tool.run_sweep(cam, step=3, dest_dir=tmp_path)
    except CameraError as exc:  # the start half-press fails first: the sweep itself fails
        start_failure = exc
    sweep = json.loads((tmp_path / sweep_tool.SWEEP_NAME).read_text())
    assert start_failure is not None and sweep["status"] == "failed"
    assert [e for e in cam.events if isinstance(e, tuple) and e[0].endswith("_failed")][-3:] == [
        ("af_failed", 1.0), ("set_failed", "iso"), ("set_failed", "shutter")]
    assert sweep["restoration"]["failed"] == ["focus_mode", "af_half_press", "iso", "shutter"]
    assert sweep["restoration"]["readback"] is not None  # the readback still ran and says what the body holds
    assert sweep["restore_error"] and sweep["restoration"]["matches_original"] is False


def test_the_cli_exits_nonzero_and_says_restoration_failed(instant, monkeypatch, capsys, tmp_path):
    class Ctx(FakeLensCamera):
        def __enter__(self): return self
        def __exit__(self, *exc): return False

        def set_focus_mode(self, name):
            if name != "Manual":
                raise CameraError("camera stayed at 'Manual'")
            return super().set_focus_mode(name)
    monkeypatch.setattr(sweep_tool, "Camera", lambda model=None, port=None, serial=None: instant(Ctx()))
    code = sweep_tool.main(["--model", A7III, "--port", PORT, "--serial", SERIAL, "--out", str(tmp_path / "s")])
    payload = json.loads(capsys.readouterr().out)
    assert code == 1 and payload["status"] == "complete_restoration_failed"
    assert payload["restore_error"].startswith("focus_mode: CameraError")
    assert payload["restoration"]["iso_readback"] == "3200" and payload["restoration"]["shutter_readback"] == "1/8"


def test_an_interruption_during_restoration_still_restores_the_rest_then_propagates(instant, tmp_path):
    cam = instant(FakeLensCamera())
    presses = []

    def autofocus_half_press(hold_s):
        presses.append(hold_s)
        if len(presses) == 2:  # the restore half-press (the first is the start AF)
            raise KeyboardInterrupt
        return "Focus Locked"
    cam.autofocus_half_press = autofocus_half_press
    with pytest.raises(KeyboardInterrupt):
        sweep_tool.run_sweep(cam, step=3, dest_dir=tmp_path)
    assert cam.settings["iso"] == "3200" and cam.settings["shutter"] == "1/8" and cam.mode == "Automatic"
    on_disk = json.loads((tmp_path / sweep_tool.SWEEP_NAME).read_text())
    assert on_disk["restoration"]["failed"] == ["af_half_press"]
    assert on_disk["restoration"]["iso_readback"] == "3200" and on_disk["restoration"]["readback"] is not None
    assert on_disk["status"] == "complete_restoration_failed" and "KeyboardInterrupt" in on_disk["restore_error"]


def test_a_return_sample_sharper_than_the_peak_counts_as_verified(instant, tmp_path):
    # The walk-back stops on any sample at least as sharp as the peak within noise; verification
    # uses the same one-sided rule (a 120 % reading is a sharper return, not a miss).
    cam = instant(FakeLensCamera())
    real_preview = cam.capture_preview

    def capture_preview(dest):
        path = real_preview(dest)
        if "return" in dest.name:
            cam.sharpness_by_path[str(path)] *= 1.2
        return path
    cam.capture_preview = capture_preview
    sweep = sweep_tool.run_sweep(cam, step=3, dest_dir=tmp_path)
    check = sweep["return_check"]
    assert check["stop_reason"] == "matched_peak" and check["ratio"] > 1.1
    assert sweep["return_verified"] is True
