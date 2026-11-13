"""A rendering failure after capture must not become another shutter request."""
import sys
from types import SimpleNamespace

import pytest


@pytest.fixture(autouse=True)
def _stop_mcp_keepalive():
    """Importing server starts its keep-alive thread; stop and join it after each test."""
    yield
    server = sys.modules.get("server")
    if server is not None:
        server._stop_keepalive()

# Every MCP tool that fires the shutter names the body by its USB serial (ports re-enumerate).
SERIAL = "A7III0SERIAL"
A7III = "Sony Alpha-A7 III (PC Control)"


def _one_body(monkeypatch, server):
    """Discovery reports one A7 III with SERIAL; the product engine is resolved from it."""
    monkeypatch.setattr(server, "detect_identified", lambda: [
        {"model": A7III, "port": "usb:000,004", "serial": SERIAL, "method": "autodetect", "os_source": "ioreg"}])


def test_saved_mcp_photo_survives_preview_failure(monkeypatch, tmp_path):
    import server
    import engine
    import product_capture
    from PIL import Image

    image = tmp_path / 'shot_saved.jpg'
    Image.new('RGB', (8, 8), 'gray').save(image)
    shot = engine.Shot(ok=True, image=image.name, summary='Photo saved', why='verified capture', settings={}, mood={})
    calls = []
    monkeypatch.setattr(engine, 'SHOTS_DIR', tmp_path)
    monkeypatch.setattr(engine, 'get_engine', lambda: SimpleNamespace(name='real'))
    monkeypatch.setattr(server, '_reset_camera', lambda: None)
    def capture(*args, **kwargs):
        calls.append(1)
        return shot
    monkeypatch.setattr(product_capture, 'shoot', capture)
    def failed_preview(*args):
        raise OSError('display encoder failed')
    monkeypatch.setattr(server, '_downscale_for_vision', failed_preview)
    _one_body(monkeypatch, server)
    result = server.shoot('natural', serial=SERIAL)
    assert calls == [1]
    assert result.is_error is False
    assert result.structured_content['ok'] is True
    assert result.structured_content['image_path'] == str(image)
    assert result.structured_content['preview']['retry_capture'] is False


import pytest
from pathlib import Path


@pytest.fixture
def mcp_camera(monkeypatch, tmp_path):
    import server
    from PIL import Image
    image = tmp_path / "captured.jpg"
    Image.new("RGB", (12, 8), "gray").save(image)
    class Camera:
        captures = 0
        reads = 0
        readback_fails = False
        last_capture_receipt = None
        def capture(self, download=True):
            self.captures += 1
            return image
        def get_settings(self):
            self.reads += 1
            if self.readback_fails and self.reads > 1:
                raise OSError("readback lost")
            return {"iso": "100", "aperture": "8", "shutter": "1/125"}
    camera = Camera()
    monkeypatch.setattr(server, "_ensure_connected", lambda serial=None: camera)
    monkeypatch.setattr(server, "_reset_camera", lambda: None)
    return camera, image


@pytest.mark.parametrize("preserved", [False, True])
def test_high_level_uncertainty_is_structured_and_never_retried(monkeypatch, tmp_path, preserved):
    import server
    import engine
    import product_capture
    from sonyremote import CaptureOutcomeUncertain
    from PIL import Image
    path = tmp_path / "preserved.jpg" if preserved else None
    if path:
        Image.new("RGB", (8, 8), "gray").save(path)
    calls = []
    def capture(*args, **kwargs):
        calls.append(1)
        raise CaptureOutcomeUncertain("transport ambiguous", capture_path=path)
    monkeypatch.setattr(engine, "get_engine", lambda: SimpleNamespace(name="real"))
    monkeypatch.setattr(server, "_reset_camera", lambda: None)
    monkeypatch.setattr(product_capture, "shoot", capture)
    _one_body(monkeypatch, server)
    result = server.shoot("natural", serial=SERIAL)
    assert calls == [1]
    assert result.is_error is True
    assert result.structured_content["ok"] is False
    assert result.structured_content["may_have_fired"] is True
    assert result.structured_content["retry_capture"] is False
    assert result.structured_content["capture_path"] == (str(path) if path else None)
    assert result.structured_content["preserved_image"] is preserved


@pytest.mark.parametrize("preview_fails", [False, True])
def test_low_level_capture_readback_failure_preserves_photo_once(monkeypatch, mcp_camera, preview_fails):
    import server
    camera, image = mcp_camera
    camera.readback_fails = True
    if preview_fails:
        monkeypatch.setattr(server, "_downscale_for_vision", lambda path: (_ for _ in ()).throw(OSError("display failed")))
    result = server.capture_photo(SERIAL)
    assert camera.captures == 1
    assert result.is_error is False
    assert result.structured_content["capture_path"] == str(image)
    assert result.structured_content["preserved_image"] is True
    assert result.structured_content["retry_capture"] is False
    assert result.structured_content["settings"] == {"iso": "100", "aperture": "8", "shutter": "1/125"}
    assert result.structured_content["settings_source"] == "before_capture"
    assert result.structured_content["warnings"]
    if preview_fails:
        assert result.structured_content["preview"]["status"] == "unavailable"


def test_low_level_capture_exception_is_structured_once(monkeypatch, mcp_camera):
    import server
    from sonyremote import CaptureOutcomeUncertain
    camera, _ = mcp_camera
    def capture(download=True):
        camera.captures += 1
        raise CaptureOutcomeUncertain("download missing")
    camera.capture = capture
    result = server.capture_photo(SERIAL)
    assert camera.captures == 1
    assert result.is_error is True
    assert result.structured_content["retry_capture"] is False
    assert result.structured_content["may_have_fired"] is True


def test_low_level_autoexpose_processing_error_delivers_last_photo_without_another_shutter(monkeypatch, mcp_camera):
    import server
    camera, image = mcp_camera
    def auto(cam, target_mean, max_iter):
        assert max_iter == 2
        cam.capture(download=True)
        raise ValueError("meter could not finish")
    monkeypatch.setattr(server, "run_auto_expose", auto)
    result = server.auto_expose(SERIAL)
    assert camera.captures == 1
    assert result.is_error is False
    assert result.structured_content["capture_path"] == str(image)
    assert result.structured_content["quality_target_met"] is False
    assert result.structured_content["retry_capture"] is False
    assert result.structured_content["settings_source"] == "before_capture_or_unknown"


def test_low_level_autoexpose_preview_failure_preserves_nonconverged_photo(monkeypatch, mcp_camera):
    import server
    camera, image = mcp_camera
    def auto(cam, target_mean, max_iter):
        assert max_iter == 2
        path = cam.capture(download=True)
        return {"ok": False, "final_image": str(path), "mode": "M", "note": "not converged",
                "final_mean": 40, "final_blown_pct": 0, "final_settings": {"iso": "100"},
                "iterations": [{"iter": 1, "mean": 40}]}
    monkeypatch.setattr(server, "run_auto_expose", auto)
    monkeypatch.setattr(server, "_downscale_for_vision", lambda path: (_ for _ in ()).throw(OSError("display failed")))
    result = server.auto_expose(SERIAL)
    assert camera.captures == 1
    assert result.is_error is False
    assert result.structured_content["quality_target_met"] is False
    assert result.structured_content["capture_path"] == str(image)
    assert result.structured_content["preview"]["retry_capture"] is False


def test_high_level_nonconverged_photo_is_delivered_partial_success(monkeypatch, mcp_camera):
    import server
    import engine
    import product_capture
    _, image = mcp_camera
    shot = engine.Shot(False, image.name, "Review exposure", "target not met", {}, {})
    calls = []
    def capture(*args, **kwargs):
        calls.append(1)
        return shot
    monkeypatch.setattr(engine, "SHOTS_DIR", image.parent)
    monkeypatch.setattr(engine, "get_engine", lambda: SimpleNamespace(name="real"))
    monkeypatch.setattr(product_capture, "shoot", capture)
    _one_body(monkeypatch, server)
    result = server.shoot("natural", serial=SERIAL)
    assert calls == [1]
    assert result.is_error is False
    assert result.structured_content["ok"] is False
    assert result.structured_content["quality_target_met"] is False
    assert result.structured_content["retry_capture"] is False
    assert result.structured_content["image_path"] == str(image)
    assert any(block.type == "image" for block in result.content)


def test_low_level_autoexpose_report_format_fault_does_not_discard_capture(monkeypatch, mcp_camera):
    import server
    camera, image = mcp_camera
    def auto(cam, **kwargs):
        return {"ok": True, "final_image": str(cam.capture(download=True)), "final_settings": {}}
    monkeypatch.setattr(server, "run_auto_expose", auto)
    monkeypatch.setattr(server, "_format_autoexpose", lambda report: (_ for _ in ()).throw(ValueError("format failed")))
    result = server.auto_expose(SERIAL)
    assert camera.captures == 1
    assert result.is_error is False
    assert result.structured_content["partial"] is True
    assert result.structured_content["image_path"] == str(image)
    assert result.structured_content["retry_capture"] is False


def test_truncated_pixels_are_not_a_verified_preserved_photo(tmp_path):
    import server
    from PIL import Image
    from sonyremote import CaptureOutcomeUncertain
    path = tmp_path / 'truncated.jpg'
    Image.new('RGB', (64, 64), 'gray').save(path)
    path.write_bytes(path.read_bytes()[:-20])
    assert server.image_size(path) == (64, 64)  # The header alone passes.
    assert server._verified_saved_image(path) is False
    uncertain = server._capture_uncertain_result(CaptureOutcomeUncertain('transfer failed', capture_path=path))
    assert uncertain.structured_content['preserved_image'] is False
    delivered = server._saved_photo_result(path, {}, 'saved')
    assert delivered.is_error is True
    assert delivered.structured_content['retry_capture'] is False


def test_header_only_truncated_jpeg_is_not_verified_preserved_image(tmp_path):
    import server
    from PIL import Image
    from sonyremote import CaptureOutcomeUncertain
    path = tmp_path / "truncated.jpg"
    Image.new("RGB", (128, 128), "gray").save(path)
    original = path.read_bytes()
    path.write_bytes(original[:-70])
    with Image.open(path) as header:
        assert header.size == (128, 128)
    assert server._verified_saved_image(path) is False
    result = server._capture_uncertain_result(CaptureOutcomeUncertain("truncated", capture_path=path))
    assert result.structured_content["preserved_image"] is False
    delivered = server._saved_photo_result(path, {"ok": True}, "saved")
    assert delivered.is_error is True
    assert delivered.structured_content["preserved_image"] is False


@pytest.mark.parametrize("function", ["record_native_video", "start_movie"])
def test_movie_mcp_returns_only_action_evidence(monkeypatch, function):
    import server
    calls = []
    camera = SimpleNamespace(record_native=lambda seconds: calls.append(seconds) or {
        "recorded_seconds": seconds, "requested_seconds": seconds, "max_seconds": 10,
        "saved_to": "unverified transport wording"})
    monkeypatch.setattr(server, "assert_recording_allowed", lambda: None)
    monkeypatch.setattr(server, "_ensure_connected", lambda serial=None: camera)
    result = getattr(server, function)(serial=SERIAL)
    assert calls == [6.0]
    assert result["action_completed"] is True
    assert result["clip_existence_verified"] is False
    assert result["clip_playback_verified"] is False
    assert result["audio_verified"] is False
    assert result["retry_record"] is False
    assert "saved_to" not in result
    assert "inspect" in result["detail"].lower()


@pytest.mark.parametrize("setting", ["capture", "movie", "autofocus", "bulb", "manualfocusdrive", "arbitrary", "/main/actions/movie"])
def test_set_control_rejects_action_nodes_before_connecting(monkeypatch, setting):
    import server
    def connected():
        raise AssertionError("action node attempted camera connection")
    monkeypatch.setattr(server, "_ensure_connected", connected)
    with pytest.raises(ValueError, match="ordinary setting"):
        server.set_control(setting, "1")


def test_set_control_preserves_documented_settings_but_rejects_toggle_widgets(monkeypatch):
    import server
    from sonyremote.camera import gp
    writes = []
    camera = SimpleNamespace(describe_setting=lambda setting: {"type": gp.GP_WIDGET_MENU, "readonly": False},
                             set_setting=lambda setting, value: writes.append((setting, value)) or value)
    monkeypatch.setattr(server, "_ensure_connected", lambda serial=None: camera)
    for setting in ("imagequality", "capturemode", "exposuremetermode", "flashmode", "imagesize", "aspectratio", "dro", "whitebalance", "focusmode"):
        assert server.set_control(setting, "fixture")["reconnect_retry"] is False
    assert len(writes) == 9
    camera.describe_setting = lambda setting: {"type": gp.GP_WIDGET_TOGGLE, "readonly": False}
    with pytest.raises(ValueError, match="toggles"):
        server.set_control("imagequality", "1")
    assert len(writes) == 9


def test_set_control_write_failure_never_reconnects_and_repeats_setter(monkeypatch):
    import server
    from sonyremote.camera import gp
    from sonyremote import CameraError
    writes = []
    def set_once(setting, value):
        writes.append((setting, value))
        raise CameraError("readback failed after write")
    camera = SimpleNamespace(describe_setting=lambda setting: {"type": gp.GP_WIDGET_MENU, "readonly": False},
                             set_setting=set_once)
    monkeypatch.setattr(server, "_ensure_connected", lambda serial=None: camera)
    monkeypatch.setattr(server, "_reset_camera", lambda: None)
    with pytest.raises(CameraError):
        server.set_control("imagequality", "Fine")
    assert writes == [("imagequality", "Fine")]


@pytest.mark.parametrize("evidence,quality_met,evaluated", [
    ({}, None, False),
    ({"refinement": {"stopped_reason": "no_critique", "attempts": 1}}, None, False),
    ({"refinement": {"stopped_reason": "max_attempts", "attempts": 2}}, False, False),
    ({"refinement": {"stopped_reason": "satisfied", "attempts": 1}}, True, True),
    ({"exposure_converged": True}, True, True),
    ({"exposure_converged": False}, False, True),
])
def test_mcp_quality_requires_explicit_evidence_not_capture_success(monkeypatch, mcp_camera, evidence, quality_met, evaluated):
    import server
    import engine
    import product_capture
    _, image = mcp_camera
    shot = engine.Shot(True, image.name, "Captured", "Review", {}, {}, evidence=evidence)
    monkeypatch.setattr(engine, "SHOTS_DIR", image.parent)
    monkeypatch.setattr(engine, "get_engine", lambda: SimpleNamespace(name="real"))
    monkeypatch.setattr(product_capture, "shoot", lambda *args, **kwargs: shot)
    _one_body(monkeypatch, server)
    result = server.shoot("natural", auto_correct=bool(evidence.get("refinement")), serial=SERIAL)
    assert result.structured_content["quality_target_met"] is quality_met
    assert result.structured_content["quality_evaluated"] is evaluated
    if "exposure_converged" in evidence:
        assert "measured_exposure_feedback" in result.structured_content["quality_evidence_sources"]
        assert result.structured_content["quality_evidence_source"] == "jpeg_exposure_feedback"
    elif evidence.get("refinement", {}).get("stopped_reason") == "satisfied":
        assert result.structured_content["quality_evidence_sources"] == ["model_critique_satisfied"]
        assert result.structured_content["quality_evidence_source"] == "vision_critique"
    else:
        assert result.structured_content["quality_evidence_sources"] == []
        assert result.structured_content["quality_evidence_source"] == "unavailable"


def test_low_level_autoexpose_keeps_a_later_identity_refusal(monkeypatch, mcp_camera):
    import server
    from sonyremote import CameraIdentityError
    camera, image = mcp_camera

    def auto(cam, target_mean, max_iter):
        cam.capture(download=True)
        raise CameraIdentityError("fixture", reason="serial_mismatch", expected_serial=SERIAL,
                                  observed_serial="AAAA1111BBBB")
    monkeypatch.setattr(server, "run_auto_expose", auto)
    result = server.auto_expose(SERIAL)
    assert camera.captures == 1 and result.is_error is False
    assert result.structured_content["capture_path"] == str(image)
    refusal = result.structured_content["identity_refusal"]
    assert (refusal["reason"], refusal["observed_serial"], refusal["may_have_fired"]) == \
        ("serial_mismatch", "AAAA1111BBBB", False)


def test_mcp_shoot_returns_a_long_exposure_refused_before_its_trigger_as_structured_no_fire(monkeypatch, tmp_path):
    # Review (PR #81): MCP shoot caught only uncertain/identity errors, so a long exposure refused
    # before its trigger (e.g. pending camera objects) became a generic tool failure that dropped the
    # preserved meter frame, the transport receipt and the no-retry guidance HTTP /shoot returns.
    import server
    import engine
    import product_capture
    from PIL import Image
    from sonyremote import CameraError

    meter = tmp_path / "meter.jpg"
    Image.new("RGB", (8, 8), "gray").save(meter)
    refusal = CameraError("The camera had images pending in its memory; no shutter was issued")
    refusal.final_shutter_not_issued = True
    refusal.capture_path = meter
    refusal.focus_policy = {"restore": "verified", "exposure_restore": "verified"}
    refusal.transport_receipt = {"route": "long_exposure", "trigger_commands": 0}
    calls = []

    def refuse(*args, **kwargs):
        calls.append(1)
        raise refusal
    monkeypatch.setattr(engine, "SHOTS_DIR", tmp_path)
    monkeypatch.setattr(engine, "get_engine", lambda: SimpleNamespace(name="real"))
    monkeypatch.setattr(server, "_reset_camera", lambda: None)
    monkeypatch.setattr(product_capture, "shoot", refuse)
    _one_body(monkeypatch, server)
    result = server.shoot("light trails of the room at night", serial=SERIAL)
    assert calls == [1]
    assert result.is_error is True
    body = result.structured_content
    assert body["ok"] is False and body["may_have_fired"] is False and body["final_shutter_issued"] is False
    assert body["retry_capture"] is False
    assert body["capture_path"] == str(meter)
    assert body["transport_receipt"]["trigger_commands"] == 0
    assert body["focus_policy"]["exposure_restore"] == "verified"


def test_mcp_identity_refusal_keeps_the_long_restore_state_and_warning(monkeypatch, tmp_path):
    # Review (PR #81): the MCP identity serializer dropped focus_policy and meter_restore.
    import server
    import engine
    import product_capture
    from sonyremote import CameraIdentityError
    meter = tmp_path / "meter.jpg"
    meter.write_bytes(b"fixture")
    refusal = CameraIdentityError("fixture: another body at the port", reason="serial_mismatch")
    refusal.final_shutter_not_issued = True
    refusal.capture_path = meter
    refusal.focus_policy = {"restore": "failed", "exposure_restore": "failed"}
    refusal.meter_restore = {"status": "failed", "reason": "identity refused on reconnect"}

    def refuse(*args, **kwargs):
        raise refusal
    monkeypatch.setattr(engine, "SHOTS_DIR", tmp_path)
    monkeypatch.setattr(engine, "get_engine", lambda: SimpleNamespace(name="real"))
    monkeypatch.setattr(server, "_reset_camera", lambda: None)
    monkeypatch.setattr(product_capture, "shoot", refuse)
    _one_body(monkeypatch, server)
    result = server.shoot("light trails of the room at night", serial=SERIAL)
    assert result.is_error is True
    body = result.structured_content
    assert body["reason"] == "serial_mismatch" and body["may_have_fired"] is False
    assert body["focus_policy"]["restore"] == "failed"
    assert body["meter_restore"]["status"] == "failed"
    assert "ISO, shutter and focus mode" in body["warning"]
