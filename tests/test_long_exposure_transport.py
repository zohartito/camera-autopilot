"""The separately gated long-exposure transport (> 1 s, up to 30 s) with fakes only.

The ordinary CLI capture stays capped at 1 s (libgphoto2 2.5.34's fixed 35 s Sony image wait
failed real 30 s captures). The long route fires once with --trigger-capture and collects the files
in a bounded event wait that scales with the exposure, proves the USB serial before and after,
attributes the JPEG by its own EXIF, reconnects with bounded backoff after the exposure (by serial
when the port moved), and never retries an uncertain shutter.

Camera-free: libgphoto2, gphoto2 autodetect, the OS USB reader and every subprocess are fakes.
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import gphoto2 as gp
import pytest
from PIL import Image

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import sonyremote.camera as camera_mod
from sonyremote.camera import (Camera, CameraError, CameraIdentityError, CaptureFromUnprovenBody,
                               CaptureOutcomeUncertain, sony_long_exposure_deadline,
                               validate_sony_capture_shutter, validate_sony_long_exposure_shutter)

A7II, A7III = "Sony ILCE-7M2 (Control)", "Sony Alpha-A7 III (PC Control)"
PRODUCT = {A7II: "ILCE-7M2", A7III: "ILCE-7M3"}
SERIAL, OTHER_SERIAL = "A7III0SERIAL", "AAAA1111BBBB"
A7II_LONG = ["30", "25", "20", "15", "13", "10", "8", "6", "5", "4", "32/10", "25/10", "2", "16/10",
             "13/10", "1", "8/10", "1/60", "1/125"]


def _ioreg(serial, address, product):
    return {"USB Vendor Name": "Sony", "USB Product Name": product,
            "kUSBSerialNumberString": serial, "locationID": "1245184", "USB Address": str(address)}


def _jpeg(path, *, exif_model, exposure_s):
    exif = Image.Exif()
    exif[272] = exif_model
    exif.get_ifd(0x8769)[33434] = exposure_s
    Image.new("RGB", (16, 12), "gray").save(path, "JPEG", exif=exif)


class World:
    """The fake USB world: one body, its OS listing, the CLI and every sleep."""

    def __init__(self, model=A7III):
        self.model, self.address = model, 3
        self.serial = SERIAL
        self.settings = {"iso": "100", "aperture": "f/8", "shutter": "10", "focus_mode": "Manual"}
        self.shutters = A7II_LONG if model == A7II else ["30", "15", "10", "5", "2", "1.3", "1", "1/60"]
        self.commands, self.timeouts, self.sleeps, self.inits = [], [], [], []
        self.triggers = 0
        self.present = True
        self.init_failures = 0
        self.move_to = None
        self.after_trigger = []   # callables run once the trigger CLI returns
        self.prior_files = False
        self.cli_result = 0
        self.cli_timeout = False
        self.exif_exposure = None  # None: the configured shutter

    @property
    def port(self):
        return f"usb:000,{self.address:03d}"

    def os_devices(self):
        return [_ioreg(self.serial, self.address, PRODUCT[self.model])] if self.present else []

    def autodetect(self):
        return [(self.model, self.port)] if self.present else []

    def run(self, command, **kwargs):
        command = list(command)
        self.commands.append(command)
        self.timeouts.append(kwargs.get("timeout"))
        cwd = Path(kwargs["cwd"])
        if "--trigger-capture" in command:
            self.triggers += 1
            if self.cli_timeout:
                raise subprocess.TimeoutExpired(command, kwargs.get("timeout"))
            exposure = self.exif_exposure
            if exposure is None:
                exposure = camera_mod.sony_capture_deadline(self.settings["shutter"])["configured_shutter_s"]
            _jpeg(cwd / "DSC01234.JPG", exif_model=PRODUCT[self.model], exposure_s=exposure)
            (cwd / "DSC01234.ARW").write_bytes(b"fixture raw")
            for hook in self.after_trigger:
                hook()
            return SimpleNamespace(returncode=self.cli_result, stdout="", stderr="")
        if self.prior_files:
            (cwd / "DSC00999.JPG").write_bytes(b"an older object still in camera RAM")
        return SimpleNamespace(returncode=0, stdout="", stderr="")


class FakeGPCamera:
    def __init__(self, world):
        self.world, self.path, self.model = world, None, None

    def set_port_info(self, path): self.path = path
    def set_abilities(self, model): self.model = model

    def init(self):
        self.world.inits.append(self.path)
        if self.world.init_failures:
            self.world.init_failures -= 1
            if self.world.move_to is not None:
                self.world.address, self.world.move_to = self.world.move_to, None
            raise gp.GPhoto2Error(-7)

    def exit(self): pass
    def get_abilities(self): return SimpleNamespace(model=self.model)
    def get_port_info(self): return SimpleNamespace(get_path=lambda: self.path)


class _Lookup:
    def load(self): pass
    def lookup_path(self, path): return path
    def lookup_model(self, model): return model
    def __getitem__(self, key): return key


@pytest.fixture
def world(monkeypatch, tmp_path, module_time):
    world = World()
    monkeypatch.setattr(camera_mod.gp, "Camera", lambda: FakeGPCamera(world))
    monkeypatch.setattr(camera_mod.gp, "PortInfoList", _Lookup)
    monkeypatch.setattr(camera_mod.gp, "CameraAbilitiesList", _Lookup)
    monkeypatch.setattr(camera_mod, "_autodetect_rows", lambda: world.autodetect())
    monkeypatch.setattr(camera_mod, "_read_os_usb_devices", lambda: world.os_devices())
    monkeypatch.setattr(camera_mod, "_read_sysfs_usb_devices", lambda *a: None)
    monkeypatch.setattr(camera_mod, "_probe_camera_answers", lambda *a: pytest.fail("probe opened a camera"))
    monkeypatch.setattr(camera_mod, "_release_macos_camera_service", lambda: None)
    monkeypatch.setattr(camera_mod.subprocess, "run", lambda *a, **k: world.run(*a, **k))
    monkeypatch.setattr(camera_mod.shutil, "which", lambda _name: "/fake/gphoto2")
    module_time(camera_mod, sleep=world.sleeps.append)  # this module's clock only
    monkeypatch.setattr(Camera, "get_settings", lambda self: dict(world.settings))
    monkeypatch.setattr(Camera, "list_options", lambda self, key: list(world.shutters) if key == "shutter" else [])
    monkeypatch.setattr(camera_mod, "CAPTURE_DIR", tmp_path / "captures")
    world.store = tmp_path / "shots"
    world.dest = tmp_path / "delivered"
    return world


def _camera(world, serial=SERIAL):
    camera = Camera(model=world.model, port=world.port, serial=serial)
    camera.quarantine_dir = world.store / "quarantine"
    camera.connect()
    return camera


def _trigger_commands(world):
    return [command for command in world.commands if "--trigger-capture" in command]


# --- the envelope ---------------------------------------------------------------------------------

@pytest.mark.parametrize("label, wait_s, timeout_s", [("2", 19, 34), ("10", 35, 50), ("15", 45, 60),
                                                       ("30", 75, 90), ("13/10", 18, 33)])
def test_the_event_wait_and_the_process_timeout_scale_with_the_exposure(label, wait_s, timeout_s):
    deadline = sony_long_exposure_deadline(label)
    # 30 s reproduces the only physical evidence: the 2026-09-05 A7 II trigger/75 s wait probe.
    assert deadline["event_wait_s"] == wait_s
    assert deadline["subprocess_timeout_s"] == timeout_s
    assert deadline["minimum_settle_s"] == pytest.approx(2 * deadline["configured_shutter_s"] + 5)


@pytest.mark.parametrize("label, live", [
    ("3.2", A7II_LONG),                 # the A7 II lists 3.2 s as "32/10"; never another body's label
    ("7", A7II_LONG + ["7"]),           # not a Sony third-stop, even if a driver advertised it
    ("1", A7II_LONG),                   # one second belongs to the ordinary route
    ("8/10", A7II_LONG),
    ("60", A7II_LONG + ["60"]),         # beyond the bodies' 30" ladder
    ("Bulb", A7II_LONG + ["Bulb"]),
    ("2", None),                        # no live ladder, no long exposure
    ("2", []),
])
def test_the_long_route_refuses_a_shutter_not_on_the_bodys_long_ladder(label, live):
    with pytest.raises(CameraError, match="no shutter was issued"):
        validate_sony_long_exposure_shutter(label, live)


@pytest.mark.parametrize("label", ["32/10", "13/10", "2", "30"])
def test_the_long_route_accepts_the_bodys_own_long_labels(label):
    assert validate_sony_long_exposure_shutter(label, A7II_LONG)["configured_shutter_s"] > 1


@pytest.mark.parametrize("label", ["13/10", "2", "15", "30"])
def test_the_ordinary_capture_policy_still_refuses_longer_than_one_second(label):
    with pytest.raises(CameraError, match="longer than 1 second"):
        validate_sony_capture_shutter(label)


def test_the_ordinary_cli_capture_refuses_a_two_second_shutter_before_any_command(world):
    world.settings["shutter"] = "2"
    camera = _camera(world)
    with pytest.raises(CameraError, match="longer than 1 second"):
        camera._capture_sony_action(dest_dir=world.dest)
    assert world.commands == []


# --- one long exposure ----------------------------------------------------------------------------

@pytest.mark.parametrize("model", [A7II, A7III])
def test_a_ten_second_exposure_fires_once_and_delivers_the_attributed_jpeg(world, model):
    world.model = model
    world.shutters = A7II_LONG if model == A7II else world.shutters
    camera = _camera(world)
    path = camera.capture_long_exposure("10", dest_dir=world.dest)
    assert path.parent == world.dest and path.suffix == ".JPG" and path.is_file()
    assert oct(path.stat().st_mode & 0o777) == "0o600"
    triggers = _trigger_commands(world)
    assert len(triggers) == 1 and world.triggers == 1
    assert triggers[0][1:5] == ["--camera", model, "--port", world.port]
    assert "--wait-event-and-download=35s" in triggers[0] and "--keep" in triggers[0]
    assert world.timeouts[world.commands.index(triggers[0])] == 50
    # An empty pre-drain ran first and fired nothing.
    assert "--trigger-capture" not in world.commands[0]
    receipt = camera.last_capture_receipt
    assert receipt["route"] == "long_exposure" and receipt["automatic_retry"] is False
    assert receipt["trigger_commands"] == 1 and receipt["capture_confirmed"] is True
    for check in ("pre_drain_serial_verification", "pre_shutter_serial_verification",
                  "post_shutter_serial_verification"):
        assert receipt[check]["serial"] == SERIAL
    assert receipt["attribution"]["matches"] is True
    assert receipt["attribution"]["exif_exposure_s"] == 10.0
    assert receipt["reconnect_ok"] is True
    assert {(f["name"], f["retained"]) for f in receipt["downloaded_files"]} == {
        ("DSC01234.JPG", True), ("DSC01234.ARW", False)}
    assert all(len(f["sha256"]) == 64 for f in receipt["downloaded_files"])
    # Only the JPEG leaves quarantine; the staging directory is gone.
    assert list((world.store / "quarantine").iterdir()) == []


def test_slow_usb_reenumeration_after_a_ten_second_exposure_is_absorbed_by_bounded_backoff(world):
    camera = _camera(world)
    world.after_trigger.append(lambda: setattr(world, "init_failures", 3))
    path = camera.capture_long_exposure("10", dest_dir=world.dest)
    assert path.is_file()
    receipt = camera.last_capture_receipt
    assert receipt["reconnect_ok"] is True
    assert world.triggers == 1
    # The first three claims fail while the body re-enumerates; the backoff waits between them.
    backoff = [s for s in world.sleeps if s in camera_mod.LONG_EXPOSURE_RECONNECT_DELAYS_S]
    assert backoff[:3] == list(camera_mod.LONG_EXPOSURE_RECONNECT_DELAYS_S[1:4])
    assert camera.verified_serial == SERIAL


def test_a_body_that_re_enumerates_at_a_new_port_is_found_again_by_its_serial(world):
    camera = _camera(world)

    def reenumerate_on_the_next_claim():
        world.init_failures = 1
        world.move_to = 5   # the failed claim is the body dropping off and coming back at usb:000,005
    world.after_trigger.append(reenumerate_on_the_next_claim)
    path = camera.capture_long_exposure("10", dest_dir=world.dest)
    assert path.is_file()
    receipt = camera.last_capture_receipt
    assert receipt["reconnect_ok"] is True
    assert receipt["reconnect_port"] == "usb:000,005"
    assert camera.info().port == "usb:000,005" and camera.verified_serial == SERIAL
    assert world.triggers == 1


def test_a_body_that_never_comes_back_keeps_the_photo_and_reports_the_failed_reconnect(world):
    camera = _camera(world)
    world.after_trigger.append(lambda: setattr(world, "init_failures", 99))
    path = camera.capture_long_exposure("10", dest_dir=world.dest)
    assert path.is_file()
    receipt = camera.last_capture_receipt
    assert receipt["reconnect_ok"] is False and "reconnect" in receipt["warning"].lower()
    assert world.triggers == 1
    assert len(world.inits) == 1 + len(camera_mod.LONG_EXPOSURE_RECONNECT_DELAYS_S)


def test_a_momentary_absence_after_the_download_is_rechecked_at_the_same_port(world):
    camera = _camera(world)
    reads = {"n": 0}
    original = world.os_devices

    def flicker():
        reads["n"] += 1
        return [] if reads["n"] == 1 else original()

    world.after_trigger.append(lambda: setattr(world, "os_devices", flicker))
    assert camera.capture_long_exposure("10", dest_dir=world.dest).is_file()
    assert camera.last_capture_receipt["post_shutter_serial_verification"]["serial"] == SERIAL


# --- never retried, never misattributed -----------------------------------------------------------

def test_a_timed_out_long_capture_is_uncertain_and_never_retried(world):
    camera = _camera(world)
    world.settings["shutter"] = "15"
    world.cli_timeout = True
    with pytest.raises(CaptureOutcomeUncertain, match="timed out"):
        camera.capture_long_exposure("15", dest_dir=world.dest)
    assert world.triggers == 1 and len(_trigger_commands(world)) == 1
    assert camera.last_capture_receipt["capture_confirmed"] is False


def test_a_cli_error_is_uncertain_keeps_the_bytes_in_quarantine_and_never_retries(world):
    camera = _camera(world)
    world.cli_result = 1
    with pytest.raises(CaptureOutcomeUncertain) as uncertain:
        camera.capture_long_exposure("10", dest_dir=world.dest)
    assert uncertain.value.retry_safe is False
    assert world.triggers == 1
    assert not world.dest.exists() or list(world.dest.iterdir()) == []
    kept = [p for p in (world.store / "quarantine").rglob("*") if p.is_file()]
    assert {p.name for p in kept} >= {"DSC01234.JPG"}


def test_a_jpeg_whose_exif_exposure_is_not_this_shutter_is_quarantined_not_delivered(world):
    camera = _camera(world)
    world.exif_exposure = 1 / 25   # e.g. a delayed older frame
    with pytest.raises(CaptureOutcomeUncertain, match="EXIF"):
        camera.capture_long_exposure("10", dest_dir=world.dest)
    assert not world.dest.exists() or list(world.dest.iterdir()) == []
    sealed = [p for p in (world.store / "quarantine").iterdir() if p.name.startswith("unproven_")]
    assert len(sealed) == 1 and (sealed[0] / "receipt.json").is_file()
    assert world.triggers == 1


def test_pending_camera_objects_refuse_the_trigger_and_are_preserved(world):
    camera = _camera(world)
    world.prior_files = True
    with pytest.raises(CameraError, match="no shutter was issued") as refused:
        camera.capture_long_exposure("10", dest_dir=world.dest)
    assert not isinstance(refused.value, CaptureOutcomeUncertain)
    assert world.triggers == 0
    kept = [p for p in (world.store / "quarantine").rglob("DSC00999.JPG")]
    assert len(kept) == 1
    assert camera.last_capture_receipt["reconnect_ok"] is True


def test_a_body_swapped_in_before_the_trigger_is_refused_before_the_shutter(world):
    camera = _camera(world)
    original = world.run

    def swap_after_the_drain(command, **kwargs):
        result = original(command, **kwargs)
        world.serial = OTHER_SERIAL
        return result
    world.run = swap_after_the_drain
    with pytest.raises(CameraIdentityError) as refused:
        camera.capture_long_exposure("10", dest_dir=world.dest)
    assert refused.value.reason == "serial_mismatch"
    assert world.triggers == 0


def test_a_body_that_changed_after_the_shutter_is_never_delivered(world):
    camera = _camera(world)
    world.after_trigger.append(lambda: setattr(world, "serial", OTHER_SERIAL))
    with pytest.raises(CaptureFromUnprovenBody):
        camera.capture_long_exposure("10", dest_dir=world.dest)
    assert not world.dest.exists() or list(world.dest.iterdir()) == []
    assert world.triggers == 1


def test_a_long_exposure_needs_the_bodys_serial(world):
    camera = Camera(model=world.model, port=world.port)
    camera.connect()
    with pytest.raises(CameraIdentityError):
        camera.capture_long_exposure("10", dest_dir=world.dest)
    assert world.commands == []


def test_the_confirmed_shutter_must_be_the_requested_one(world):
    camera = _camera(world)
    world.settings["shutter"] = "5"
    with pytest.raises(CameraError, match="no shutter was issued"):
        camera.capture_long_exposure("10", dest_dir=world.dest)
    assert world.commands == []


def test_a_failed_delivery_keeps_every_downloaded_file_including_the_raw(world, monkeypatch):
    camera = _camera(world)

    def disk_full(source, destination):
        raise OSError(28, "No space left on device")
    monkeypatch.setattr(camera_mod, "_move_durably", disk_full)
    with pytest.raises(CaptureOutcomeUncertain):
        camera.capture_long_exposure("10", dest_dir=world.dest)
    kept = {p.name for p in (world.store / "quarantine").rglob("*") if p.is_file()}
    assert {"DSC01234.JPG", "DSC01234.ARW"} <= kept
    assert world.triggers == 1


def test_a_refusal_never_leaves_an_earlier_captures_receipt_in_place(world):
    camera = _camera(world)
    camera.last_capture_receipt = {"trigger_commands": 1, "route": "an earlier capture"}
    world.settings["shutter"] = "5"
    with pytest.raises(CameraError, match="no shutter was issued"):
        camera.capture_long_exposure("10", dest_dir=world.dest)
    assert camera.last_capture_receipt["trigger_commands"] == 0


def test_a_raw_cleanup_failure_after_delivery_is_a_warning_not_an_uncertain_capture(world, monkeypatch):
    camera = _camera(world)
    original_unlink = Path.unlink

    def unlink(self, *args, **kwargs):
        if self.suffix == ".ARW":
            raise PermissionError(13, "fixture: cannot remove")
        return original_unlink(self, *args, **kwargs)
    monkeypatch.setattr(Path, "unlink", unlink)
    path = camera.capture_long_exposure("10", dest_dir=world.dest)
    assert path.is_file()
    receipt = camera.last_capture_receipt
    assert receipt["path"] == str(path) and receipt["capture_confirmed"] is True
    assert "DSC01234.ARW" in receipt["cleanup_warning"]
    assert world.triggers == 1


def test_a_serial_that_reappears_at_another_port_does_not_prove_who_took_the_photo(world):
    # Cross-review of 821959f7: body A passes preflight at usb:000,003; a same-model body B takes the
    # long exposure there and leaves; A reappears at usb:000,005. EXIF names only the model and the
    # exposure time, so nothing binds the JPEG to A: it must stay quarantined, never published.
    camera = _camera(world)
    world.after_trigger.append(lambda: setattr(world, "address", 5))
    with pytest.raises(CaptureFromUnprovenBody) as unproven:
        camera.capture_long_exposure("10", dest_dir=world.dest)
    assert unproven.value.retry_safe is False
    assert not world.dest.exists() or list(world.dest.iterdir()) == []
    receipt = camera.last_capture_receipt
    assert receipt["capture_confirmed"] is False
    assert "post_shutter_serial_verification" not in receipt
    assert receipt["post_shutter_identity_refusal"]["reason"] == "not_present"
    sealed = [p for p in (world.store / "quarantine").iterdir() if p.name.startswith("unproven_")]
    assert len(sealed) == 1 and (sealed[0] / "receipt.json").is_file()
    assert {"DSC01234.JPG", "DSC01234.ARW"} <= {p.name for p in sealed[0].iterdir()}
    assert world.triggers == 1                                   # never retried
    # Reconnection for later restore writes may still find the body by serial at its new port.
    assert receipt["reconnect_port"] == "usb:000,005"


def test_a_moved_port_is_not_accepted_while_another_body_holds_the_old_one(world, monkeypatch):
    camera = _camera(world)

    def both():
        return [_ioreg(OTHER_SERIAL, 3, PRODUCT[world.model]), _ioreg(SERIAL, 5, PRODUCT[world.model])]
    world.after_trigger.append(lambda: setattr(world, "os_devices", both))
    with pytest.raises(CaptureFromUnprovenBody):
        camera.capture_long_exposure("10", dest_dir=world.dest)
    assert not world.dest.exists() or list(world.dest.iterdir()) == []


def test_the_generic_capture_fallback_also_refuses_longer_than_one_second(world):
    # A body without the Sony action-capture widget falls back to gp capture(); the 1 s cap holds.
    world.settings["shutter"] = "2"
    camera = _camera(world)
    fired = []
    camera._camera.capture = lambda *args: fired.append(args)
    camera._has_sony_action_capture = lambda: False
    with pytest.raises(CameraError, match="longer than 1 second"):
        camera.capture(download=False)
    assert fired == [] and world.commands == []


@pytest.mark.parametrize("path", ["generic", "sony_action"])
def test_sonys_uninitialised_shutter_placeholder_refuses_before_any_shutter_or_file(world, path):
    # Cross-review of 821959f7: "65535/65535" parsed to 1.0 s and fired with an unknown shutter.
    world.settings["shutter"] = "65535/65535"
    camera = _camera(world)
    fired = []
    camera._camera.capture = lambda *args: fired.append(args)
    with pytest.raises(CameraError, match="placeholder") as refused:
        if path == "generic":
            camera._has_sony_action_capture = lambda: False
            camera.capture(download=True, dest_dir=world.dest)
        else:
            camera._capture_sony_action(dest_dir=world.dest)
    assert not isinstance(refused.value, CaptureOutcomeUncertain)
    assert fired == [] and world.commands == [] and world.triggers == 0
    assert not world.dest.exists() or list(world.dest.iterdir()) == []
    quarantine = world.store / "quarantine"
    assert not quarantine.exists() or not any(quarantine.rglob("*"))


def test_the_placeholder_is_refused_by_every_shutter_validator():
    for validate in (sony_long_exposure_deadline, validate_sony_capture_shutter,
                     lambda label: validate_sony_long_exposure_shutter(label, [label])):
        with pytest.raises(CameraError, match="placeholder"):
            validate("65535/65535")


def _failing_reader(world, mode, fail_from_call):
    """An OS USB reader that fails from its Nth call on: raises OSError / RuntimeError, or returns None."""
    real, calls = camera_mod._os_identity, {"n": 0}

    def reader():
        calls["n"] += 1
        if calls["n"] < fail_from_call:
            return real()
        if mode == "oserror":
            raise OSError(5, "fixture: ioreg read failed")
        if mode == "runtime":
            raise RuntimeError("fixture: reader crashed")
        return None
    return reader


@pytest.mark.parametrize("mode", ["oserror", "runtime", "none"])
def test_an_os_reader_failure_before_the_shutter_is_a_clean_identity_refusal(world, monkeypatch, mode):
    # Cross-review of 14282f57/0e819935: reader failures escaped as generic errors, not identity refusals.
    camera = _camera(world)
    monkeypatch.setattr(camera_mod, "_os_identity", _failing_reader(world, mode, fail_from_call=1))
    with pytest.raises(CameraIdentityError) as refused:
        camera.capture_long_exposure("10", dest_dir=world.dest)
    assert refused.value.reason == "serial_unavailable"
    assert world.triggers == 0 and world.commands == []


@pytest.mark.parametrize("mode", ["oserror", "runtime", "none"])
def test_an_os_reader_failure_after_the_shutter_quarantines_the_photo_as_unproven(world, monkeypatch, mode):
    camera = _camera(world)
    world.after_trigger.append(lambda: monkeypatch.setattr(
        camera_mod, "_os_identity", _failing_reader(world, mode, fail_from_call=1)))
    with pytest.raises(CaptureFromUnprovenBody) as unproven:
        camera.capture_long_exposure("10", dest_dir=world.dest)
    assert unproven.value.identity["reason"] == "serial_unavailable"
    assert not world.dest.exists() or list(world.dest.iterdir()) == []
    sealed = [p for p in (world.store / "quarantine").iterdir() if p.name.startswith("unproven_")]
    assert len(sealed) == 1 and (sealed[0] / "receipt.json").is_file()
    assert world.triggers == 1


@pytest.mark.parametrize("mode", ["oserror", "runtime", "none"])
def test_the_ordinary_cli_capture_also_quarantines_after_an_os_reader_failure(world, monkeypatch, mode):
    world.settings["shutter"] = "1/60"
    camera = _camera(world)
    camera._has_sony_action_capture = lambda: True
    original = world.run

    def run(command, **kwargs):
        if "--capture-image-and-download" in command:
            (Path(kwargs["cwd"]) / "DSC00077.JPG").write_bytes(b"fixture")
            monkeypatch.setattr(camera_mod, "_os_identity", _failing_reader(world, mode, fail_from_call=1))
            return SimpleNamespace(returncode=0, stdout="", stderr="")
        return original(command, **kwargs)
    world.run = run
    with pytest.raises(CaptureFromUnprovenBody):
        camera._capture_sony_action(dest_dir=world.dest)
    sealed = [p for p in (world.store / "quarantine").iterdir() if p.name.startswith("unproven_")]
    assert len(sealed) == 1 and (sealed[0] / "receipt.json").is_file()


@pytest.mark.parametrize("shutter", ["2", "65535/65535", "Bulb", None])
def test_the_generic_fallback_cap_refusal_is_a_typed_pre_trigger_refusal(world, shutter):
    # Review (PR #81): the generic transport's cap raised a plain CameraError before capture(), so
    # callers that had marked the capture as started reported "may have fired".
    from sonyremote.camera import CapturePreconditionError
    world.settings["shutter"] = shutter
    camera = _camera(world)
    fired = []
    camera._camera.capture = lambda *args: fired.append(args)
    camera._has_sony_action_capture = lambda: False
    with pytest.raises(CapturePreconditionError):
        camera.capture(download=False)
    assert fired == [] and world.commands == []
