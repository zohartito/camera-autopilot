"""Camera-free regressions for the A7 III optical focus-bracket route.

Covers the planner shapes, refusal before any write when the body has no manualfocus node,
the exact preview -> capture -> step ordering (no step after the last frame), restoration in
`finally` when a capture outcome is uncertain (re-raised, never retried), step validation on the
camera adapter, the AF half-press readback in the manifest, the manifest schema, the bounded
in-process preview save and the CLI dry-run / uncertain paths.
"""
from __future__ import annotations

import hashlib
import io
import json
import sys
from pathlib import Path

import cv2
import numpy as np
import pytest
from PIL import Image

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))
sys.path.insert(0, str(ROOT / "tools"))
sys.path.insert(0, str(ROOT))

import focus_bracket  # noqa: E402
import focus_bracket_capture as tool  # noqa: E402
import sonyremote.camera as camera_module  # noqa: E402
from sonyremote.camera import Camera, CameraError, CameraInfo, CaptureOutcomeUncertain, gp  # noqa: E402

A7III = "Sony Alpha-A7 III (PC Control)"
A7II = "Sony ILCE-7M2 (Control)"
PORT = "usb:000,004"


def _jpeg_bytes(seed: int = 0, blur: bool = False, size=(96, 64)) -> bytes:
    rng = np.random.default_rng(seed)
    pixels = rng.integers(0, 256, (size[1], size[0], 3), dtype="uint8")
    if blur:
        pixels = cv2.GaussianBlur(pixels, (0, 0), 6)
    stream = io.BytesIO()
    Image.fromarray(pixels).save(stream, "JPEG", quality=95)
    return stream.getvalue()


# --- fake camera (duck-typed like sonyremote.camera.Camera's focus adapters) -----------------
class FakeCamera:
    def __init__(self, *, manual_node=True, mode="Automatic", af_nodes=True, model=A7III):
        self.events: list = []
        self.mode = mode
        self.manual_node = manual_node
        self.af_nodes = af_nodes
        self.model = model
        self.last_capture_receipt = None
        self.previews = 0

    def get_settings(self):
        # A real body always answers a settings read; the bracket checks one before every shutter.
        return {"aperture": "f/8", "iso": "100", "shutter": "1/125"}

    def focus_capabilities(self):
        self.events.append("capabilities")
        nodes = {"focusmode": {"value": self.mode, "readonly": False, "type": 5},
                 "focusarea": {"value": "Center", "readonly": False, "type": 5},
                 "focusmagnifier": {"value": "2", "readonly": False, "type": 4}}
        nodes["manualfocus"] = ({"value": "0", "readonly": False, "type": 3, "range": [-7.0, 7.0, 1.0]}
                                if self.manual_node else None)
        nodes["autofocus"] = {"value": "2", "readonly": False, "type": 4} if self.af_nodes else None
        nodes["focusindication"] = {"value": "Unlock", "readonly": True, "type": 5} if self.af_nodes else None
        return {"model": self.model, "port": PORT, "nodes": nodes,
                "manual_focus_route": bool(self.manual_node)}

    def get_focus_mode(self):
        self.events.append("get_mode")
        return self.mode

    def set_focus_mode(self, name):
        self.events.append(("set_mode", name))
        self.mode = name
        return name

    def capture_preview(self, dest: Path):
        self.events.append(("preview", dest.name))
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(_jpeg_bytes(seed=self.previews, blur=self.previews % 2 == 1))
        if not dest.name.endswith("_stale.jpg"):  # drained frames do not advance the fixture
            self.previews += 1
        return dest

    def manual_focus_step(self, value):
        self.events.append(("step", value))
        return {"node": "manualfocus", "sent": value, "readback": "0", "elapsed_s": 0.01}

    def autofocus_half_press(self, hold_s):
        self.events.append(("af", hold_s))
        return "Focus Locked"


class ShutterCounter:
    """Stand-in for Camera.capture: writes a JPEG per call; can raise an uncertain outcome."""

    def __init__(self, camera: FakeCamera, uncertain_at: int | None = None):
        self.camera = camera
        self.calls = 0
        self.uncertain_at = uncertain_at

    def __call__(self, frame_dir: Path):
        index = self.calls
        self.calls += 1
        self.camera.events.append(("capture", index))
        if self.uncertain_at == index:
            raise CaptureOutcomeUncertain("Sony action capture timed out waiting for the camera.")
        path = Path(frame_dir) / f"DSC{index:05d}.JPG"
        path.write_bytes(_jpeg_bytes(seed=100 + index))
        self.camera.last_capture_receipt = {"transport": "sony_gphoto2_cli", "path": str(path),
                                            "capture_confirmed": True}
        return path


# --- planner ---------------------------------------------------------------------------------
def test_plan_shapes_for_three_frames_toward_near():
    plan = focus_bracket.plan_bracket(3, 3, "near")
    assert plan["signed_step"] == -3 and plan["commands"] == [-3, -3]
    assert [p["cumulative_offset"] for p in plan["positions"]] == [0, -3, -6]
    assert [p["command_after"] for p in plan["positions"]] == [-3, -3, None]
    assert plan["calibration"] == focus_bracket.CALIBRATION_NOTE
    far = focus_bracket.plan_bracket(7, 1, "far")
    assert far["signed_step"] == 1 and len(far["commands"]) == 6


@pytest.mark.parametrize("frames,step,direction", [
    (1, 3, "far"), (8, 3, "far"), (3, 0, "far"), (3, 8, "far"), (3, -1, "far"),
    (True, 3, "far"), (3, True, "far"), (3, 3, "up"), (3, 3.0, "far"),
])
def test_plan_rejects_out_of_range_inputs(frames, step, direction):
    with pytest.raises(ValueError):
        focus_bracket.plan_bracket(frames, step, direction)


# --- runner ----------------------------------------------------------------------------------
def test_refuses_before_any_write_when_manualfocus_node_is_absent(tmp_path):
    camera = FakeCamera(manual_node=False, model=A7II)
    shutter = ShutterCounter(camera)
    with pytest.raises(focus_bracket.FocusBracketError, match=A7II.replace("(", r"\(").replace(")", r"\)")):
        focus_bracket.run_bracket(camera, frames=3, step=3, direction="far",
                                  dest_dir=tmp_path / "out", capture=shutter)
    assert camera.events == ["capabilities"]  # no set_mode, preview, step, capture or AF
    assert shutter.calls == 0
    assert not (tmp_path / "out").exists()


def test_exact_ordering_preview_capture_step_and_no_step_after_last(tmp_path):
    camera = FakeCamera()
    shutter = ShutterCounter(camera)
    manifest = focus_bracket.run_bracket(camera, frames=3, step=2, direction="far",
                                         dest_dir=tmp_path, capture=shutter)
    assert camera.events == [
        "capabilities", "get_mode", ("set_mode", "Manual"),
        ("preview", "preview_00.jpg"), ("capture", 0), ("step", 2), ("preview", "preview_01_stale.jpg"),
        ("preview", "preview_01.jpg"), ("capture", 1), ("step", 2), ("preview", "preview_02_stale.jpg"),
        ("preview", "preview_02.jpg"), ("capture", 2),
        ("set_mode", "Automatic"), ("af", 1.0),
    ]
    assert manifest["status"] == "complete" and len(manifest["frames"]) == 3
    assert [s["after_frame"] for s in manifest["steps_sent"]] == [0, 1]
    assert [s["sent"] for s in manifest["steps_sent"]] == [2, 2]


def test_uncertain_capture_is_reraised_not_retried_and_focus_mode_restored(tmp_path):
    camera = FakeCamera()
    shutter = ShutterCounter(camera, uncertain_at=1)
    with pytest.raises(CaptureOutcomeUncertain):
        focus_bracket.run_bracket(camera, frames=3, step=3, direction="near",
                                  dest_dir=tmp_path, capture=shutter)
    assert shutter.calls == 2  # frame 0 accepted, frame 1 uncertain, never a third attempt
    assert camera.events[-2:] == [("set_mode", "Automatic"), ("af", 1.0)]
    assert ("step", -3) in camera.events and camera.events.count(("step", -3)) == 1
    manifest = json.loads((tmp_path / focus_bracket.MANIFEST_NAME).read_text())
    assert manifest["status"] == "failed" and "CaptureOutcomeUncertain" in manifest["error"]
    assert len(manifest["frames"]) == 1
    assert manifest["restoration"]["attempted"] is True
    assert manifest["restoration"]["focus_mode_readback"] == "Automatic"
    assert manifest["restoration"]["af_half_press"]["indication"] == "Focus Locked"


def test_restoration_failure_never_masks_the_capture_outcome(tmp_path):
    camera = FakeCamera()
    shutter = ShutterCounter(camera, uncertain_at=0)

    def set_focus_mode(name):
        camera.events.append(("set_mode", name))
        if name != "Manual":
            raise CameraError("camera stayed at 'Manual'")
        return name
    camera.set_focus_mode = set_focus_mode
    with pytest.raises(CaptureOutcomeUncertain):
        focus_bracket.run_bracket(camera, frames=2, step=1, direction="far",
                                  dest_dir=tmp_path, capture=shutter)
    manifest = json.loads((tmp_path / focus_bracket.MANIFEST_NAME).read_text())
    assert "camera stayed at" in manifest["restoration"]["error"]
    assert "CaptureOutcomeUncertain" in manifest["error"]


def test_a_placeholder_focus_mode_is_reread_and_refused_before_any_write_if_it_persists(tmp_path, module_time):
    # 2026-09-29: an A7 III just woken from auto power-off answered focus mode "Undefined".
    module_time(camera_module, sleep=lambda seconds: None)
    camera = FakeCamera()
    reads = iter(["Undefined"])
    camera.get_focus_mode = lambda: (camera.events.append("get_mode"), next(reads, camera.mode))[1]
    manifest = focus_bracket.run_bracket(camera, frames=2, step=1, direction="far",
                                         dest_dir=tmp_path / "ok", capture=ShutterCounter(camera))
    assert manifest["original_focus_mode"] == "Automatic" and manifest["status"] == "complete"
    assert manifest["original_focus_mode_read"]["placeholder_reads"][0]["values"] == {"focus_mode": "Undefined"}

    stuck = FakeCamera()
    stuck.get_focus_mode = lambda: (stuck.events.append("get_mode"), "Undefined")[1]
    with pytest.raises(camera_module.PlaceholderSettingsError):
        focus_bracket.run_bracket(stuck, frames=2, step=1, direction="far",
                                  dest_dir=tmp_path / "stuck", capture=ShutterCounter(stuck))
    assert stuck.events == ["capabilities"] + ["get_mode"] * (camera_module.PLACEHOLDER_REREADS + 1)


def test_a_failed_focus_mode_restore_still_half_presses_and_flags_the_manifest(tmp_path):
    camera = FakeCamera()

    def set_focus_mode(name):
        camera.events.append(("set_mode", name))
        if name != "Manual":
            raise CameraError("camera stayed at 'Manual'")
        return name
    camera.set_focus_mode = set_focus_mode
    manifest = focus_bracket.run_bracket(camera, frames=2, step=1, direction="far",
                                         dest_dir=tmp_path, capture=ShutterCounter(camera))
    assert camera.events[-2:] == [("set_mode", "Automatic"), ("af", 1.0)]
    assert manifest["restoration"]["af_half_press"] == {"hold_s": 1.0, "indication": "Focus Locked"}
    assert manifest["status"] == "complete_restoration_failed"
    assert "camera stayed at" in manifest["restore_error"] == manifest["restoration"]["error"]
    on_disk = json.loads((tmp_path / focus_bracket.MANIFEST_NAME).read_text())
    assert on_disk["status"] == "complete_restoration_failed" and len(on_disk["frames"]) == 2


def test_manual_original_mode_skips_half_press(tmp_path):
    camera = FakeCamera(mode="Manual")
    manifest = focus_bracket.run_bracket(camera, frames=2, step=1, direction="far",
                                         dest_dir=tmp_path, capture=ShutterCounter(camera))
    assert not any(isinstance(e, tuple) and e[0] == "af" for e in camera.events)
    assert manifest["restoration"]["af_half_press"] is None
    assert manifest["restoration"]["focus_mode_readback"] == "Manual"


def test_af_mode_without_autofocus_nodes_records_skip(tmp_path):
    camera = FakeCamera(af_nodes=False, mode="AF-C")
    manifest = focus_bracket.run_bracket(camera, frames=2, step=1, direction="far",
                                         dest_dir=tmp_path, capture=ShutterCounter(camera))
    assert manifest["restoration"]["af_half_press"] == {"skipped": "autofocus/focusindication node absent"}
    assert camera.events[-1] == ("set_mode", "AF-C")


def test_manifest_schema_and_receipts(tmp_path):
    camera = FakeCamera()
    manifest = focus_bracket.run_bracket(camera, frames=2, step=4, direction="far",
                                         dest_dir=tmp_path, capture=ShutterCounter(camera))
    on_disk = json.loads(Path(manifest["manifest_path"]).read_text())
    assert on_disk["schema"] == "focus_bracket_manifest/1"
    assert on_disk["calibration"] == "uncalibrated relative steps; asymmetric rail behaviour observed 2026-09-15"
    assert on_disk["body"] == {"model": A7III, "port": PORT}
    assert on_disk["original_focus_mode"] == "Automatic" and on_disk["manual_mode_readback"] == "Manual"
    assert on_disk["plan"]["commands"] == [4]
    for index, frame in enumerate(on_disk["frames"]):
        assert frame["index"] == index and frame["cumulative_offset"] == 4 * index
        assert isinstance(frame["preview"]["sharpness"], float)
        for receipt in (frame["preview"], frame["capture"]):
            data = Path(receipt["path"]).read_bytes()
            assert receipt["sha256"] == hashlib.sha256(data).hexdigest()
            assert receipt["bytes"] == len(data)
        assert frame["capture_receipt"]["capture_confirmed"] is True
    assert on_disk["steps_sent"] == [{"after_frame": 0, "node": "manualfocus", "sent": 4,
                                      "readback": "0", "elapsed_s": 0.01,
                                      "drained_preview": str(tmp_path / "preview_01_stale.jpg")}]
    assert on_disk["restoration"] == {"attempted": True, "focus_mode_readback": "Automatic",
                                      "af_half_press": {"hold_s": 1.0, "indication": "Focus Locked"},
                                      "error": None}
    # frame 1's preview was deliberately blurred by the fake: the metric must order them.
    assert on_disk["frames"][0]["preview"]["sharpness"] > on_disk["frames"][1]["preview"]["sharpness"]


# --- aperture selection (Arsenal 2: bracket at the lens's sharpest aperture) -----------------
# Full-stop ladders. "2" (2 s) is advertised by the driver but lies outside the enabled 1.0 s
# capture envelope, so compensation must never pick it.
SHUTTER_LADDER = ["2", "1", "1/2", "1/4", "1/8", "1/15", "1/30", "1/60", "1/125", "1/250",
                  "1/500", "1/1000"]
ISO_LADDER = ["100", "200", "400", "800", "1600", "3200", "6400"]
APERTURE_LADDER = ["f/2.8", "f/4", "f/5.6", "f/8", "f/11", "f/16", "f/22"]
TAMRON = "Tamron 28-75 f/2.8"
FE_24_105 = "FE 24-105mm F4 G OSS"


class ApertureFakeCamera(FakeCamera):
    """FakeCamera plus the settings surface the aperture picker and exposure restore use."""

    def __init__(self, *, settings=None, aperture_options=None, iso_options=None, **kw):
        super().__init__(**kw)
        self.settings = dict(settings or {"aperture": "f/8", "iso": "100", "shutter": "1/125"})
        self.options = {"aperture": list(aperture_options or APERTURE_LADDER),
                        "shutter": list(SHUTTER_LADDER), "iso": list(iso_options or ISO_LADDER)}
        self.set_calls: list = []

    def get_settings(self):
        self.events.append("get_settings")
        return dict(self.settings)

    def list_options(self, key):
        self.events.append(("list_options", key))
        return list(self.options.get(key, []))

    def set_setting(self, key, value, max_seconds=8.0):
        self.events.append(("set_setting", key, value))
        self.set_calls.append((key, value))
        self.settings[key] = value
        return self.settings[key]


def _stops(old_shutter, new_shutter):
    import math
    import camera_caps
    return math.log2(camera_caps._shutter_seconds(new_shutter) / camera_caps._shutter_seconds(old_shutter))


def test_choose_bracket_aperture_matches_the_optimizer_sweet_spot():
    import lens_optics
    import physics
    decision = focus_bracket.choose_bracket_aperture(
        body_model=A7III, lens_model=TAMRON, current_aperture="f/11", aperture_options=APERTURE_LADDER)
    sweet = lens_optics.TAMRON_28_75_OPTICS.optimal_aperture(physics.A7III)
    assert decision["reason"] == focus_bracket.APERTURE_REASON_SWEET_SPOT
    assert decision["sweet_spot_f"] == sweet
    assert focus_bracket._parse_f_number(decision["aperture"]) == sweet  # exact on this ladder


def test_choose_bracket_aperture_snaps_to_nearest_settable_wider_on_ties():
    # Sweet spot f/5.6 is absent from this coarse ladder; nearest by stops is f/4 (0.485 vs 0.514).
    decision = focus_bracket.choose_bracket_aperture(
        body_model=A7III, lens_model=TAMRON, current_aperture="f/8",
        aperture_options=["f/2.8", "f/4", "f/8", "f/16"])
    assert decision["reason"] == focus_bracket.APERTURE_REASON_SWEET_SPOT
    assert decision["aperture"] == "f/4"


def test_choose_bracket_aperture_unknown_lens_keeps_current_with_reason():
    decision = focus_bracket.choose_bracket_aperture(
        body_model=A7III, lens_model="Sigma 105mm macro", current_aperture="f/9",
        aperture_options=["f/2.8", "f/8"])
    assert decision["reason"] == focus_bracket.APERTURE_REASON_UNKNOWN_LENS
    assert decision["aperture"] is None and decision["sweet_spot_f"] is None
    assert decision["f_number"] == pytest.approx(9.0)


def test_mounted_a7iii_lens_has_no_profile_so_the_aperture_is_kept():
    # The A7 III as mounted carries the FE 24-105 F4: in rig_profile's registry, no optics profile.
    decision = focus_bracket.choose_bracket_aperture(
        body_model=A7III, lens_model=FE_24_105, current_aperture="f/8", aperture_options=APERTURE_LADDER)
    assert decision["reason"] == focus_bracket.APERTURE_REASON_UNKNOWN_LENS
    assert decision["lens_id"] == "Sony FE 24-105mm F4 G OSS" and decision["aperture"] is None
    assert "no measured aperture-sharpness profile" in decision["note"]


def test_choose_bracket_aperture_without_a_lens_keeps_current():
    decision = focus_bracket.choose_bracket_aperture(
        body_model=A7III, lens_model=None, current_aperture="f/6.3", aperture_options=["f/2.8", "f/8"])
    assert decision["reason"] == focus_bracket.APERTURE_REASON_NO_LENS
    assert decision["aperture"] is None and decision["f_number"] == pytest.approx(6.3)


def test_f4_to_f56_lengthens_the_shutter_by_one_stop():
    camera = ApertureFakeCamera(settings={"aperture": "f/4", "iso": "100", "shutter": "1/125"})
    decision = focus_bracket.resolve_bracket_aperture(camera, body_model=A7III, lens_model=TAMRON)
    comp = decision["compensation"]
    assert decision["reason"] == focus_bracket.APERTURE_REASON_SWEET_SPOT
    assert (decision["aperture"], decision["shutter"], decision["iso"]) == ("f/5.6", "1/60", "100")
    assert comp["stops_needed"] == pytest.approx(0.971, abs=0.01)
    assert _stops("1/125", decision["shutter"]) == pytest.approx(1.0, abs=0.1)
    assert abs(comp["residual_stops"]) <= focus_bracket.EXPOSURE_MATCH_TOLERANCE_STOPS
    assert comp["from"] == {"aperture": "f/4", "shutter": "1/125", "iso": "100"}
    assert camera.set_calls == []  # resolving is read-only


def test_compensation_beyond_the_shutter_envelope_moves_the_remainder_to_iso():
    from sonyremote.camera import SONY_ACTION_MAX_ENABLED_SHUTTER_S
    # f/2.8 -> f/5.6 needs 2 stops; 1/2 s x4 = 2 s exceeds the enabled 1.0 s envelope.
    camera = ApertureFakeCamera(settings={"aperture": "f/2.8", "iso": "100", "shutter": "1/2"})
    decision = focus_bracket.resolve_bracket_aperture(camera, body_model=A7III, lens_model=TAMRON)
    assert decision["compensation"]["max_shutter_s"] == SONY_ACTION_MAX_ENABLED_SHUTTER_S == 1.0
    assert (decision["aperture"], decision["shutter"], decision["iso"]) == ("f/5.6", "1", "200")
    assert decision["compensation"]["residual_stops"] == pytest.approx(0.0, abs=1e-9)


def test_infeasible_compensation_keeps_the_aperture_and_records_why(tmp_path):
    # Already at the 1.0 s envelope and the top of the ISO ladder: two stops cannot be paid back.
    camera = ApertureFakeCamera(settings={"aperture": "f/2.8", "iso": "6400", "shutter": "1"})
    decision = focus_bracket.resolve_bracket_aperture(camera, body_model=A7III, lens_model=TAMRON)
    assert decision["reason"] == focus_bracket.APERTURE_REASON_EXPOSURE_INFEASIBLE
    assert decision["aperture"] is None and decision["shutter"] is None and decision["iso"] is None
    assert decision["compensation"]["feasible"] is False
    assert decision["compensation"]["residual_stops"] > focus_bracket.EXPOSURE_MATCH_TOLERANCE_STOPS
    assert "will be kept" in decision["note"]
    manifest = focus_bracket.run_bracket(camera, frames=2, step=1, direction="far",
                                         dest_dir=tmp_path, capture=ShutterCounter(camera),
                                         aperture=decision)
    assert camera.set_calls == [] and "exposure" not in manifest["restoration"]
    assert manifest["aperture"]["held_constant"] is True


def test_run_bracket_writes_aperture_shutter_iso_once_holds_them_and_restores(tmp_path):
    camera = ApertureFakeCamera(settings={"aperture": "f/8", "iso": "100", "shutter": "1/125"})
    decision = focus_bracket.resolve_bracket_aperture(camera, body_model=A7III, lens_model=TAMRON)
    assert (decision["aperture"], decision["shutter"], decision["iso"]) == ("f/5.6", "1/250", "100")
    manifest = focus_bracket.run_bracket(camera, frames=3, step=2, direction="far",
                                         dest_dir=tmp_path, capture=ShutterCounter(camera),
                                         aperture=decision)
    ap = manifest["aperture"]
    assert ap["set_readback"] == {"aperture": "f/5.6", "shutter": "1/250", "iso": "100"}
    # Written once before frame 0, restored to the metered originals afterwards; nothing between.
    assert camera.set_calls == [("aperture", "f/5.6"), ("shutter", "1/250"), ("iso", "100"),
                                ("aperture", "f/8"), ("shutter", "1/125"), ("iso", "100")]
    first_capture = camera.events.index(("capture", 0))
    assert max(camera.events.index(("set_setting", k, v)) for k, v in camera.set_calls[:3]) < first_capture
    assert ap["frame_settings"] == [{"aperture": "f/5.6", "iso": "100", "shutter": "1/250"}] * 3
    assert ap["held_constant"] is True
    assert manifest["restoration"]["exposure"] == {
        "attempted": ["aperture", "shutter", "iso"],
        "readback": {"aperture": "f/8", "shutter": "1/125", "iso": "100"}, "error": None}
    assert camera.settings == {"aperture": "f/8", "iso": "100", "shutter": "1/125"}


def test_exposure_is_restored_after_a_mid_bracket_exception(tmp_path):
    camera = ApertureFakeCamera(settings={"aperture": "f/4", "iso": "100", "shutter": "1/125"})
    decision = focus_bracket.resolve_bracket_aperture(camera, body_model=A7III, lens_model=TAMRON)
    with pytest.raises(CaptureOutcomeUncertain):
        focus_bracket.run_bracket(camera, frames=3, step=2, direction="far", dest_dir=tmp_path,
                                  capture=ShutterCounter(camera, uncertain_at=1), aperture=decision)
    on_disk = json.loads((tmp_path / focus_bracket.MANIFEST_NAME).read_text())
    assert on_disk["status"] == "failed"
    assert on_disk["restoration"]["exposure"]["readback"] == {"aperture": "f/4", "shutter": "1/125", "iso": "100"}
    assert camera.settings == {"aperture": "f/4", "iso": "100", "shutter": "1/125"}
    assert camera.events[-1] == ("set_setting", "iso", "100")


def test_exposure_restore_runs_even_when_the_focus_mode_restore_fails(tmp_path):
    camera = ApertureFakeCamera(settings={"aperture": "f/4", "iso": "100", "shutter": "1/125"})
    decision = focus_bracket.resolve_bracket_aperture(camera, body_model=A7III, lens_model=TAMRON)

    def set_focus_mode(name):
        camera.events.append(("set_mode", name))
        if name != "Manual":
            raise CameraError("camera stayed at 'Manual'")
        return name
    camera.set_focus_mode = set_focus_mode
    focus_bracket.run_bracket(camera, frames=2, step=1, direction="far", dest_dir=tmp_path,
                              capture=ShutterCounter(camera), aperture=decision)
    assert camera.settings == {"aperture": "f/4", "iso": "100", "shutter": "1/125"}


def test_an_interruption_during_the_focus_mode_restore_still_restores_exposure(tmp_path):
    camera = ApertureFakeCamera(settings={"aperture": "f/4", "iso": "100", "shutter": "1/125"})
    decision = focus_bracket.resolve_bracket_aperture(camera, body_model=A7III, lens_model=TAMRON)

    def set_focus_mode(name):
        camera.events.append(("set_mode", name))
        if name != "Manual":
            raise KeyboardInterrupt
        return name
    camera.set_focus_mode = set_focus_mode
    with pytest.raises(KeyboardInterrupt):
        focus_bracket.run_bracket(camera, frames=2, step=1, direction="far", dest_dir=tmp_path,
                                  capture=ShutterCounter(camera), aperture=decision)
    assert camera.settings == {"aperture": "f/4", "iso": "100", "shutter": "1/125"}
    manifest = json.loads((tmp_path / focus_bracket.MANIFEST_NAME).read_text())
    assert manifest["status"] == "complete_restoration_failed" and "KeyboardInterrupt" in manifest["restore_error"]
    assert manifest["restoration"]["af_half_press"]["indication"] == "Focus Locked"


def test_run_bracket_unknown_lens_keeps_current_aperture_and_writes_nothing(tmp_path):
    camera = ApertureFakeCamera(settings={"aperture": "f/9", "iso": "200", "shutter": "1/60"})
    decision = focus_bracket.resolve_bracket_aperture(camera, body_model=A7III, lens_model="Sigma 105mm macro")
    assert decision["reason"] == focus_bracket.APERTURE_REASON_UNKNOWN_LENS
    manifest = focus_bracket.run_bracket(camera, frames=2, step=1, direction="far",
                                         dest_dir=tmp_path, capture=ShutterCounter(camera),
                                         aperture=decision)
    ap = manifest["aperture"]
    assert ap["reason"] == "unknown_lens_kept_current" and ap["set_readback"] is None
    assert camera.set_calls == []  # nothing written to the body, so nothing to restore
    assert "exposure" not in manifest["restoration"]
    assert {f["aperture"] for f in ap["frame_settings"]} == {"f/9"}
    assert ap["held_constant"] is True


def test_a7ii_refuses_focus_bracket_before_any_aperture_write(tmp_path):
    camera = ApertureFakeCamera(manual_node=False, model=A7II)
    decision = {"reason": focus_bracket.APERTURE_REASON_SWEET_SPOT, "aperture": "f/5.6",
                "shutter": "1/60", "iso": "100", "held": {}}
    shutter = ShutterCounter(camera)
    with pytest.raises(focus_bracket.FocusBracketError):
        focus_bracket.run_bracket(camera, frames=2, step=1, direction="far",
                                  dest_dir=tmp_path / "out", capture=shutter, aperture=decision)
    assert camera.set_calls == [] and camera.events == ["capabilities"]  # refused before any write
    assert shutter.calls == 0 and not (tmp_path / "out").exists()


class _ApertureCameraContext(ApertureFakeCamera):
    def __init__(self, model, port, **kw):
        super().__init__(model=model, **kw)
        self.port = port
        self.shutter = ShutterCounter(self)

    def __enter__(self): return self
    def __exit__(self, *exc): return False
    def capture(self, dest_dir=None): return self.shutter(dest_dir)


def _dry_run(monkeypatch, capsys, tmp_path, lens, settings=None):
    created = []

    def factory(model=None, port=None):
        cam = _ApertureCameraContext(model, port, settings=settings)
        created.append(cam)
        return cam
    monkeypatch.setattr(tool, "Camera", factory)
    code = tool.main(["--model", A7III, "--port", PORT, "--frames", "3", "--step", "3",
                      "--direction", "far", "--lens", lens, "--out", str(tmp_path / "out"), "--dry-run",
                      "--baseline", "current"])
    return code, json.loads(capsys.readouterr().out), created[0]


def test_cli_dry_run_with_lens_reports_chosen_exposure_without_writing(monkeypatch, capsys, tmp_path):
    code, payload, camera = _dry_run(monkeypatch, capsys, tmp_path, TAMRON,
                                     settings={"aperture": "f/4", "iso": "100", "shutter": "1/125"})
    assert code == 0 and payload["setting_writes"] == 0
    assert payload["aperture"]["reason"] == "lens_sweet_spot"
    assert (payload["aperture"]["aperture"], payload["aperture"]["shutter"]) == ("f/5.6", "1/60")
    assert "restore the originals" in payload["aperture_note"]
    assert camera.set_calls == []  # dry-run writes nothing to the camera
    assert not (tmp_path / "out").exists()


def test_cli_dry_run_says_plainly_the_mounted_24_105_has_no_profile(monkeypatch, capsys, tmp_path):
    code, payload, camera = _dry_run(monkeypatch, capsys, tmp_path, FE_24_105)
    assert code == 0 and payload["aperture"]["reason"] == "unknown_lens_kept_current"
    assert "no measured aperture-sharpness profile" in payload["aperture_note"]
    assert "will be kept" in payload["aperture_note"] and camera.set_calls == []


def test_preview_sharpness_orders_sharp_over_blurred(tmp_path):
    sharp, blurred = tmp_path / "sharp.jpg", tmp_path / "blur.jpg"
    sharp.write_bytes(_jpeg_bytes(seed=3, size=(1024, 680)))
    blurred.write_bytes(_jpeg_bytes(seed=3, blur=True, size=(1024, 680)))
    assert focus_bracket.preview_sharpness(sharp) > 5 * focus_bracket.preview_sharpness(blurred)


# --- sonyremote.camera adapters over fake gphoto widgets ---------------------------------------
class FakeWidget:
    def __init__(self, widget_type, value, *, readonly=False, rng=None, choices=()):
        self.widget_type, self.value, self.readonly = widget_type, value, readonly
        self.rng, self.choices, self.values = rng, list(choices), []

    def get_type(self): return self.widget_type
    def get_value(self): return self.value
    def get_readonly(self): return self.readonly
    def get_range(self): return self.rng
    def count_choices(self): return len(self.choices)
    def get_choice(self, i): return self.choices[i]

    def set_value(self, value):
        self.values.append(value)
        # A relative adjust reads back 0 (manualfocus); everything else keeps the written value.
        self.value = 0 if self.rng == (-7.0, 7.0, 1.0) else value


class FakeConfig:
    def __init__(self, widgets): self.widgets = widgets

    def get_child_by_name(self, name):
        if name not in self.widgets:
            raise gp.GPhoto2Error(-2)
        return self.widgets[name]


class FakeNative:
    def __init__(self, widgets, preview=b""):
        self.widgets, self.set_configs, self.preview = widgets, 0, preview

    def get_config(self): return FakeConfig(self.widgets)
    def set_config(self, config): self.set_configs += 1

    def capture_preview(self):
        class _File:
            def __init__(self, data): self.data = data
            def get_data_and_size(self): return memoryview(self.data)
        return _File(self.preview)


def a7iii_widgets(indication="Unlock"):
    return {
        "manualfocus": FakeWidget(gp.GP_WIDGET_RANGE, 0, rng=(-7.0, 7.0, 1.0)),
        "autofocus": FakeWidget(gp.GP_WIDGET_TOGGLE, 2),
        "focusindication": FakeWidget(gp.GP_WIDGET_RADIO, indication, readonly=True,
                                      choices=("Unlock", "Focus Locked", "No Focus - Low Contrast")),
        "focusmode": FakeWidget(gp.GP_WIDGET_RADIO, "Automatic", choices=("Manual", "Automatic", "AF-C")),
        "focusarea": FakeWidget(gp.GP_WIDGET_RADIO, "Center", choices=("Wide", "Center")),
        "focusmagnifier": FakeWidget(gp.GP_WIDGET_TOGGLE, 2),
    }


def connected(monkeypatch, widgets, model=A7III, preview=b""):
    cam = Camera()
    cam._camera = FakeNative(widgets, preview=preview)
    monkeypatch.setattr(cam, "info", lambda: CameraInfo(model, PORT))
    return cam


def test_focus_capabilities_reads_the_tree_and_never_assumes(monkeypatch):
    cam = connected(monkeypatch, a7iii_widgets())
    caps = cam.focus_capabilities()
    assert caps["manual_focus_route"] is True and caps["model"] == A7III
    assert caps["nodes"]["manualfocus"]["range"] == [-7.0, 7.0, 1.0]
    assert caps["nodes"]["focusindication"]["readonly"] is True
    assert "Focus Locked" in caps["nodes"]["focusindication"]["choices"]
    a7ii = connected(monkeypatch, {"focusmode": a7iii_widgets()["focusmode"]}, model=A7II)
    caps = a7ii.focus_capabilities()
    assert caps["manual_focus_route"] is False and caps["nodes"]["manualfocus"] is None
    assert a7ii._camera.set_configs == 0 and cam._camera.set_configs == 0


@pytest.mark.parametrize("value", [0, 8, -8, 3.5, True, "3"])
def test_manual_focus_step_validates_before_writing(monkeypatch, value):
    cam = connected(monkeypatch, a7iii_widgets())
    with pytest.raises(ValueError):
        cam.manual_focus_step(value)
    assert cam._camera.set_configs == 0 and cam._camera.widgets["manualfocus"].values == []


def test_manual_focus_step_writes_once_and_reports_relative_readback(monkeypatch):
    widgets = a7iii_widgets()
    cam = connected(monkeypatch, widgets)
    result = cam.manual_focus_step(-7)
    assert widgets["manualfocus"].values == [-7.0] and cam._camera.set_configs == 1
    assert result["sent"] == -7 and result["readback"] == "0"
    cam.manual_focus_step(7)
    assert widgets["manualfocus"].values == [-7.0, 7.0] and cam._camera.set_configs == 2


def test_manual_focus_step_refuses_absent_node_naming_the_body(monkeypatch):
    cam = connected(monkeypatch, {"focusmode": a7iii_widgets()["focusmode"]}, model=A7II)
    with pytest.raises(CameraError, match="ILCE-7M2.*manualfocus"):
        cam.manual_focus_step(3)
    assert cam._camera.set_configs == 0


def test_autofocus_half_press_holds_bounded_reads_indication_and_always_releases(monkeypatch):
    widgets = a7iii_widgets(indication="Focus Locked")
    cam = connected(monkeypatch, widgets)
    slept = []
    monkeypatch.setattr(camera_module.time, "sleep", slept.append)
    assert cam.autofocus_half_press(0.5) == "Focus Locked"
    assert widgets["autofocus"].values == [1, 0] and slept == [0.5]
    for bad in (0, 3.5, -1, float("nan"), True):
        with pytest.raises(ValueError):
            cam.autofocus_half_press(bad)
    assert widgets["autofocus"].values == [1, 0]
    # Release must happen even when the indication read fails mid-hold.
    widgets["focusindication"].get_value = lambda: (_ for _ in ()).throw(gp.GPhoto2Error(-1))
    with pytest.raises(CameraError):
        cam.autofocus_half_press(0.2)
    assert widgets["autofocus"].values == [1, 0, 1, 0]


def test_set_and_get_focus_mode_use_verified_readback(monkeypatch):
    widgets = a7iii_widgets()
    cam = connected(monkeypatch, widgets)
    monkeypatch.setattr(camera_module.time, "sleep", lambda _: None)
    assert cam.get_focus_mode() == "Automatic"
    assert cam.set_focus_mode("Manual") == "Manual"
    assert widgets["focusmode"].values == ["Manual"] and cam.get_focus_mode() == "Manual"


def test_capture_preview_saves_bounded_jpeg_privately(monkeypatch, tmp_path):
    data = _jpeg_bytes(seed=9)
    cam = connected(monkeypatch, a7iii_widgets(), preview=data)
    dest = cam.capture_preview(tmp_path / "nested" / "preview_00.jpg")
    assert dest.read_bytes() == data and (dest.stat().st_mode & 0o777) == 0o600
    assert focus_bracket.preview_sharpness(dest) > 0


@pytest.mark.parametrize("payload", [b"\xff\xd8" + b"x" * (camera_module.MAX_PREVIEW_BYTES - 1),
                                     b"", b"\x00\x01notjpeg"])
def test_capture_preview_rejects_oversize_empty_or_non_jpeg(monkeypatch, tmp_path, payload):
    cam = connected(monkeypatch, a7iii_widgets(), preview=payload)
    with pytest.raises(CameraError):
        cam.capture_preview(tmp_path / "preview.jpg")
    assert not (tmp_path / "preview.jpg").exists()


def test_existing_liveview_tombstone_is_untouched():
    with pytest.raises(CameraError, match="USB live view is disabled"):
        Camera().liveview_frame()


# --- CLI ------------------------------------------------------------------------------------
class _FakeCameraContext(FakeCamera):
    def __init__(self, model, port, **kw):
        super().__init__(model=model, **kw)
        self.port = port
        self.shutter = ShutterCounter(self)

    def __enter__(self): return self
    def __exit__(self, *exc): return False
    def capture(self, dest_dir=None): return self.shutter(dest_dir)


def test_cli_dry_run_reads_capabilities_and_plan_without_writes(monkeypatch, capsys, tmp_path):
    created = []

    def factory(model=None, port=None):
        cam = _FakeCameraContext(model, port)
        created.append(cam)
        return cam
    monkeypatch.setattr(tool, "Camera", factory)
    code = tool.main(["--model", A7III, "--port", PORT, "--frames", "3", "--step", "3",
                      "--direction", "far", "--out", str(tmp_path / "out"), "--dry-run"])
    payload = json.loads(capsys.readouterr().out)
    assert code == 0 and payload["status"] == "dry_run" and payload["setting_writes"] == 0
    assert payload["capabilities"]["manual_focus_route"] is True
    assert payload["plan"]["commands"] == [3, 3]
    assert created[0].events == ["capabilities"]
    assert not (tmp_path / "out").exists()


def test_cli_dry_run_reports_missing_route_with_distinct_exit(monkeypatch, capsys, tmp_path):
    monkeypatch.setattr(tool, "Camera", lambda model=None, port=None: _FakeCameraContext(model, port, manual_node=False))
    code = tool.main(["--model", A7II, "--port", PORT, "--direction", "near", "--out", str(tmp_path), "--dry-run"])
    assert code == 3 and json.loads(capsys.readouterr().out)["capabilities"]["manual_focus_route"] is False


def test_cli_runs_bracket_then_merges_with_focus_stack(monkeypatch, capsys, tmp_path):
    cameras = []

    def factory(model=None, port=None, serial=None):
        cameras.append(_FakeCameraContext(model, port))
        return cameras[-1]
    stacked = []

    def fake_stack(paths):
        stacked.append([Path(p).name for p in paths])
        return np.full((16, 24, 3), 128, dtype=np.uint8)
    monkeypatch.setattr(tool, "Camera", factory)
    monkeypatch.setattr(tool, "focus_stack", fake_stack)
    out = tmp_path / "bracket"
    code = tool.main(["--model", A7III, "--port", PORT, "--serial", "A7III0SERIAL", "--frames", "2", "--step", "1",
                      "--direction", "far", "--baseline", "current", "--out", str(out)])
    payload = json.loads(capsys.readouterr().out)
    assert code == 0 and payload["status"] == "complete" and payload["frames"] == 2
    assert stacked == [["DSC00000.JPG", "DSC00001.JPG"]]
    receipt = json.loads((out / "focus_stack.json").read_text())
    assert receipt["output"]["sha256"] == hashlib.sha256((out / "focus_stack.jpg").read_bytes()).hexdigest()
    assert [i["cumulative_offset"] for i in receipt["inputs"]] == [0, 1]
    assert all("preview_sharpness" in i and "sha256" in i for i in receipt["inputs"])
    assert cameras[0].events[-2:] == [("set_mode", "Automatic"), ("af", 1.0)]


def test_cli_uncertain_capture_exits_two_and_never_stacks(monkeypatch, capsys, tmp_path):
    class Uncertain(_FakeCameraContext):
        def capture(self, dest_dir=None):
            self.events.append(("capture", 0))
            raise CaptureOutcomeUncertain("transfer failed")
    monkeypatch.setattr(tool, "Camera", lambda model=None, port=None, serial=None: Uncertain(model, port))
    monkeypatch.setattr(tool, "focus_stack", lambda paths: pytest.fail("stack must not run"))
    out = tmp_path / "bracket"
    code = tool.main(["--model", A7III, "--port", PORT, "--serial", "A7III0SERIAL", "--direction", "near",
                      "--baseline", "current", "--out", str(out)])
    payload = json.loads(capsys.readouterr().out)
    assert code == 2 and payload["status"] == "uncertain" and payload["retry_safe"] is False
    assert json.loads((out / "bracket_manifest.json").read_text())["status"] == "failed"
    assert not (out / "focus_stack.jpg").exists()


@pytest.mark.parametrize("argv", [["--frames", "1"], ["--step", "9"], ["--frames", "8"]])
def test_cli_rejects_invalid_plan_before_touching_the_camera(monkeypatch, capsys, tmp_path, argv):
    monkeypatch.setattr(tool, "Camera", lambda **kw: pytest.fail("camera must not be opened"))
    code = tool.main(["--model", A7III, "--port", PORT, "--direction", "far", "--out", str(tmp_path), *argv])
    assert code == 2 and json.loads(capsys.readouterr().out)["status"] == "invalid"


def test_reused_output_directory_is_refused_before_any_setting_write(tmp_path):
    first = FakeCamera()
    focus_bracket.run_bracket(first, frames=2, step=2, direction="far",
                              dest_dir=tmp_path, capture=ShutterCounter(first))
    camera = FakeCamera()
    shutter = ShutterCounter(camera)
    with pytest.raises(focus_bracket.FocusBracketError, match="fresh --out"):
        focus_bracket.run_bracket(camera, frames=2, step=2, direction="far",
                                  dest_dir=tmp_path, capture=shutter)
    assert camera.events == ["capabilities"]
    assert shutter.calls == 0
    assert not (tmp_path / (focus_bracket.MANIFEST_NAME + ".tmp")).exists()


class SequencedNative(FakeNative):
    """Fake gp camera whose liveview hands back one payload per read (the last one repeats)."""

    def __init__(self, widgets, payloads):
        super().__init__(widgets)
        self.payloads, self.reads = list(payloads), 0

    def capture_preview(self):
        self.reads += 1
        self.preview = self.payloads.pop(0) if len(self.payloads) > 1 else self.payloads[0]
        return super().capture_preview()


NOT_JPEG = bytes.fromhex("01000000") + b"\x00" * 1000  # the post-still payload seen 2026-09-15


def test_capture_preview_rereads_a_non_jpeg_payload_a_bounded_number_of_times(monkeypatch, tmp_path):
    good = _jpeg_bytes(seed=11)
    cam = connected(monkeypatch, a7iii_widgets())
    cam._camera = SequencedNative(a7iii_widgets(), [NOT_JPEG, NOT_JPEG, good])
    slept = []
    monkeypatch.setattr(camera_module.time, "sleep", slept.append)
    dest = cam.capture_preview(tmp_path / "preview_00.jpg")
    assert dest.read_bytes() == good and cam._camera.reads == 3
    assert slept == [camera_module.PREVIEW_RETRY_DELAY_S] * 2
    assert sorted(p.name for p in tmp_path.iterdir()) == ["preview_00.jpg"]  # one file, no diagnostic


def test_capture_preview_gives_up_after_the_bound_and_keeps_the_payload_for_diagnosis(monkeypatch, tmp_path):
    cam = connected(monkeypatch, a7iii_widgets())
    cam._camera = SequencedNative(a7iii_widgets(), [NOT_JPEG])
    slept = []
    monkeypatch.setattr(camera_module.time, "sleep", slept.append)
    with pytest.raises(CameraError, match=f"after {camera_module.PREVIEW_READ_ATTEMPTS} reads"):
        cam.capture_preview(tmp_path / "preview_00.jpg")
    assert cam._camera.reads == camera_module.PREVIEW_READ_ATTEMPTS == 12
    assert len(slept) == camera_module.PREVIEW_READ_ATTEMPTS - 1
    assert not (tmp_path / "preview_00.jpg").exists()
    diagnostic = tmp_path / "preview_00.not-jpeg.bin"
    assert diagnostic.read_bytes() == NOT_JPEG and (diagnostic.stat().st_mode & 0o777) == 0o600


def test_capture_preview_reconstructs_a_headered_frame_from_the_last_good_prelude(tmp_path, monkeypatch):
    """Every read in the bound headered: the JPEG body is intact, the 32-byte prelude is restored."""
    good = _jpeg_bytes(seed=7, blur=False)
    assert len(good) > 64
    headered = b"\x01\x00\x00\x00" + b"\x00" * 28 + good[32:]
    cam = connected(monkeypatch, a7iii_widgets())
    cam._camera = SequencedNative(a7iii_widgets(), [good, headered])
    monkeypatch.setattr(camera_module.time, "sleep", lambda *_: None)
    first = cam.capture_preview(tmp_path / "p0.jpg")
    assert first.read_bytes() == good
    second = cam.capture_preview(tmp_path / "p1.jpg")
    assert second.read_bytes() == good  # prelude + intact body reproduces the frame
    assert cam.preview_reconstructed == 1
    assert not (tmp_path / "p1.not-jpeg.bin").exists()


@pytest.mark.parametrize("spelling", [
    "Tamron 28-75 f/2.8", "Tamron 28-75mm F2.8", "Tamron 28-75mm f2.8", "Tamron 28-75mm F/2.8",
    "Tamron 28-75mm F2.8 Di III RXD",   # the A036's EXIF LensID
    "E 28-75mm F2.8-2.8",               # the body's EXIF LensModel, which spells out its specification
])
def test_every_accepted_lens_spelling_gets_the_sweet_spot(spelling):
    decision = focus_bracket.choose_bracket_aperture(
        body_model=A7III, lens_model=spelling, current_aperture="f/11", aperture_options=APERTURE_LADDER)
    assert decision["reason"] == focus_bracket.APERTURE_REASON_SWEET_SPOT
    assert decision["lens_id"] == "Tamron 28-75mm F2.8"


def test_cli_help_lists_the_accepted_lens_spellings(capsys):
    focus_bracket_capture = tool
    with pytest.raises(SystemExit):
        focus_bracket_capture.main(["--help"])
    text = " ".join(capsys.readouterr().out.split())
    for spelling in ("Tamron 28-75mm F2.8", "Tamron 28-75mm F2.8 Di III RXD", "E 28-75mm F2.8-2.8", "f/2.8"):
        assert spelling in text
