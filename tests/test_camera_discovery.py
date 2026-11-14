"""Identity-first USB discovery: establish which bodies are present from the OS, then trust or
recover each gphoto row instead of believing its label. No hardware; ioreg and gphoto are mocked.

Grounded in the 2026-09-20 live diagnosis (private/unpublished field notes): gphoto2
--auto-detect listed the A7 II as "Apple iPad Air" while a real iPad was also attached, so the
A7 II never appeared in the picker and could not be selected.
"""
from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import sonyremote.camera as camera_mod
from sonyremote.camera import detect, detect_identified
from _runner import run_module_tests

A7II = "Sony ILCE-7M2 (Control)"
A7III = "Sony Alpha-A7 III (PC Control)"


def _dev(product, serial, location, address, vendor="Sony", vendor_id="1356"):
    """One ioreg USB property record, as _read_os_usb_devices() returns them."""
    return {"USB Product Name": product, "kUSBSerialNumberString": serial,
            "locationID": str(location), "USB Address": str(address),
            "USB Vendor Name": vendor, "idVendor": vendor_id}


# Real values captured 2026-09-20; the derived gphoto ports are usb:000,003 / usb:000,004.
A7III_DEV = _dev("ILCE-7M3", "A7III0SERIAL", 1245184, 3)
A7II_DEV = _dev("ILCE-7M2", "A7II00SERIAL", 1310720, 4)
IPAD_DEV = _dev("iPad", "IPADSERIAL00000000000000", 34799616, 4,
                vendor="Apple Inc.", vendor_id="1452")


def _autodetect(rows):
    return patch.object(camera_mod, "_autodetect_rows", return_value=list(rows))


def _ioreg(devices):
    return patch.object(camera_mod, "_read_os_usb_devices", return_value=list(devices))


# --- ioreg parsing and port derivation ------------------------------------------------

IOREG_SAMPLE = """  +-o ILCE-7M3@00130000  <class IOUSBHostDevice, id 0x1, registered>
  |     "USB Product Name" = "ILCE-7M3"
  |     "locationID" = 1245184
  |     "kUSBSerialNumberString" = "A7III0SERIAL"
  |     "USB Address" = 3
  |     "USB Vendor Name" = "Sony"
  |     "idVendor" = 1356
  +-o ILCE-7M2@00140000  <class IOUSBHostDevice, id 0x2, registered>
  |     "USB Product Name" = "ILCE-7M2"
  |     "locationID" = 1310720
  |     "kUSBSerialNumberString" = "A7II00SERIAL"
  |     "USB Address" = 4
  |     "USB Vendor Name" = "Sony"
  |     "idVendor" = 1356
  +-o iPad@02130000  <class IOUSBHostDevice, id 0x3, registered>
  |         "USB Product Name" = "iPad"
  |         "kUSBSerialNumberString" = "IPADSERIAL00000000000000"
  |         "USB Vendor Name" = "Apple Inc."
  |         "USB Address" = 4
  |         "locationID" = 34799616
  |         "idVendor" = 1452
"""


def test_parser_derives_gphoto_ports_and_keeps_only_supported_sony_bodies():
    devices = camera_mod._parse_ioreg_usb(IOREG_SAMPLE)
    bodies = camera_mod._os_supported_bodies(devices)
    by_port = {b["port"]: b for b in bodies}
    assert set(by_port) == {"usb:000,003", "usb:000,004"}  # the iPad is not a supported body
    assert by_port["usb:000,003"] == {"model": A7III, "port": "usb:000,003", "serial": "A7III0SERIAL"}
    assert by_port["usb:000,004"] == {"model": A7II, "port": "usb:000,004", "serial": "A7II00SERIAL"}


def test_ioreg_is_not_read_off_macos():
    with patch.object(camera_mod.sys, "platform", "linux"):
        assert camera_mod._read_os_usb_devices() is None


# --- discovery scenarios ---------------------------------------------------------------

def test_correct_autodetect_needs_no_probe():
    rows = [(A7II, "usb:000,004"), (A7III, "usb:000,003")]
    with _autodetect(rows), _ioreg([A7II_DEV, A7III_DEV]), \
         patch.object(camera_mod, "_probe_camera_answers") as probe:
        result = detect_identified()
    probe.assert_not_called()
    by_port = {r["port"]: r for r in result}
    assert all(r["method"] == "autodetect" for r in result)
    assert by_port["usb:000,004"]["serial"] == "A7II00SERIAL"
    assert by_port["usb:000,003"]["serial"] == "A7III0SERIAL"


def test_supported_body_missing_from_autodetect_is_recovered():
    # gphoto lists only the A7 III; the OS proves the A7 II is present too.
    rows = [(A7III, "usb:000,003")]
    with _autodetect(rows), _ioreg([A7II_DEV, A7III_DEV]), \
         patch.object(camera_mod, "_probe_camera_answers", return_value=True) as probe:
        result = detect_identified()
    probe.assert_called_once_with(A7II, "usb:000,004")
    recovered = [r for r in result if r["method"] == "recovered_probe"]
    assert len(recovered) == 1
    assert recovered[0] == {"model": A7II, "port": "usb:000,004", "serial": "A7II00SERIAL",
                            "method": "recovered_probe", "os_source": "ioreg"}


def test_mislabelled_row_is_recovered_and_non_sony_port_never_probed():
    # The exact live case: two "Apple iPad Air" rows (one is the real iPad, one is the A7 II),
    # plus the A7 III. The A7 II must be recovered; the real iPad port must never be probed.
    rows = [("Apple iPad Air", "usb:002,004"), ("Apple iPad Air", "usb:000,004"),
            (A7III, "usb:000,003")]
    with _autodetect(rows), _ioreg([A7III_DEV, A7II_DEV, IPAD_DEV]), \
         patch.object(camera_mod, "_probe_camera_answers", return_value=True) as probe:
        result = detect_identified()
    # Only the OS-confirmed Sony port is probed; the iPad's usb:002,004 is left alone.
    probe.assert_called_once_with(A7II, "usb:000,004")
    probed_ports = {call.args[1] for call in probe.call_args_list}
    assert "usb:002,004" not in probed_ports

    by_model = {r["model"]: r for r in result}
    assert by_model[A7II] == {"model": A7II, "port": "usb:000,004", "serial": "A7II00SERIAL",
                              "method": "recovered_probe", "os_source": "ioreg"}
    assert by_model[A7III]["method"] == "autodetect"
    # The mislabelled iPad row at the A7 II's port is dropped; the real iPad still appears once.
    ipad_rows = [r for r in result if r["model"] == "Apple iPad Air"]
    assert [r["port"] for r in ipad_rows] == ["usb:002,004"]


def test_failed_probe_does_not_recover_the_body():
    rows = [(A7III, "usb:000,003")]
    with _autodetect(rows), _ioreg([A7II_DEV, A7III_DEV]), \
         patch.object(camera_mod, "_probe_camera_answers", return_value=False) as probe:
        result = detect_identified()
    probe.assert_called_once_with(A7II, "usb:000,004")
    assert all(r["method"] != "recovered_probe" for r in result)
    assert A7II not in {r["model"] for r in result}


def test_probe_budget_is_bounded():
    rows: list[tuple[str, str]] = []  # nothing named by gphoto; both bodies must be recovered
    with _autodetect(rows), _ioreg([A7II_DEV, A7III_DEV]), \
         patch.object(camera_mod, "MAX_RECOVERY_PROBES", 1), \
         patch.object(camera_mod, "_probe_camera_answers", return_value=True) as probe:
        result = detect_identified()
    assert probe.call_count == 1  # the named maximum is respected
    assert sum(r["method"] == "recovered_probe" for r in result) == 1


def test_two_identical_models_fail_closed():
    # The OS reports two A7 II bodies; a probe cannot say which serial is at which port.
    second_a7ii = _dev("ILCE-7M2", "AAAA1111BBBB2222", 0, 6)  # -> usb:000,006
    with _autodetect([]), _ioreg([A7II_DEV, second_a7ii]), \
         patch.object(camera_mod, "_probe_camera_answers", return_value=True) as probe:
        result = detect_identified()
    probe.assert_not_called()
    assert all(r["method"] != "recovered_probe" for r in result)


# --- fallback + compatible view --------------------------------------------------------

def test_ioreg_unavailable_falls_back_to_plain_autodetect():
    rows = [(A7II, "usb:000,004"), ("USB PTP Class Camera", "usb:001,001")]
    # The sysfs patch keeps a Linux CI run off the runner's real /sys; macOS never reads sysfs.
    with _autodetect(rows), patch.object(camera_mod, "_read_os_usb_devices", return_value=None), \
         patch.object(camera_mod, "_read_sysfs_usb_devices", return_value=None), \
         patch.object(camera_mod, "_probe_camera_answers") as probe:
        result = detect_identified()
    probe.assert_not_called()
    assert [(r["model"], r["port"]) for r in result] == rows
    assert all(r["method"] == "autodetect" and r["serial"] is None for r in result)
    assert {r["os_source"] for r in result} <= {"ioreg_unavailable", "non_darwin", "sysfs_unavailable"}


def test_detect_is_the_compatible_view_over_detect_identified():
    rows = [(A7III, "usb:000,003")]
    with _autodetect(rows), _ioreg([A7II_DEV, A7III_DEV]), \
         patch.object(camera_mod, "_probe_camera_answers", return_value=True):
        pairs = detect()
    assert (A7II, "usb:000,004") in pairs
    assert (A7III, "usb:000,003") in pairs
    assert all(isinstance(p, tuple) and len(p) == 2 for p in pairs)


def test_a_body_labelled_as_the_other_supported_model_is_recovered_with_its_serial():
    # Live case (lessons, 2026-09-13): gphoto lists the A7 III as "Sony ILCE-7M2 (Control)".
    # A supported-but-wrong label must not suppress recovery: the OS says the port holds the
    # A7 III, a read-only probe confirms it, and its serial becomes discoverable.
    rows = [(A7II, "usb:000,003"), (A7II, "usb:000,004")]
    with _autodetect(rows), _ioreg([A7III_DEV, A7II_DEV]), \
         patch.object(camera_mod, "_probe_camera_answers", return_value=True) as probe:
        result = detect_identified()
    probe.assert_called_once_with(A7III, "usb:000,003")
    by_port = {r["port"]: r for r in result}
    assert by_port["usb:000,003"] == {"model": A7III, "port": "usb:000,003", "serial": "A7III0SERIAL",
                                      "method": "recovered_probe", "os_source": "ioreg"}
    assert by_port["usb:000,004"]["serial"] == "A7II00SERIAL" and by_port["usb:000,004"]["model"] == A7II
    assert len(result) == 2


def test_a_mislabelled_supported_row_whose_probe_fails_keeps_no_serial():
    rows = [(A7II, "usb:000,003")]
    with _autodetect(rows), _ioreg([A7III_DEV]), \
         patch.object(camera_mod, "_probe_camera_answers", return_value=False):
        result = detect_identified()
    assert result == [{"model": A7II, "port": "usb:000,003", "serial": None,
                       "method": "autodetect", "os_source": "ioreg"}]


def test_a_port_the_os_lists_with_two_bodies_is_never_probed_or_given_a_serial():
    # The OS lists an A7 II and an A7 III at the same port: neither identity can be attributed, so
    # the mislabelled-row recovery must not pick the last record and probe it.
    rows = [(A7II, "usb:000,003")]
    a7ii_at_same_port = _dev("ILCE-7M2", "A7II00SERIAL", 1245184, 3)
    with _autodetect(rows), _ioreg([a7ii_at_same_port, A7III_DEV]), \
         patch.object(camera_mod, "_probe_camera_answers", return_value=True) as probe:
        result = detect_identified()
    probe.assert_not_called()
    assert result == [{"model": A7II, "port": "usb:000,003", "serial": None, "method": "autodetect",
                       "os_source": "ioreg", "serial_status": "duplicate"}]


def test_a_mislabelled_body_the_os_records_twice_is_still_recovered():
    # Codex review: two identical OS records of one body are one body, so the same-model guard
    # must not treat them as two A7 IIIs and leave the camera without a serial.
    rows = [(A7II, "usb:000,003")]
    with _autodetect(rows), _ioreg([A7III_DEV, dict(A7III_DEV)]), \
         patch.object(camera_mod, "_probe_camera_answers", return_value=True) as probe:
        result = detect_identified()
    probe.assert_called_once_with(A7III, "usb:000,003")
    assert result == [{"model": A7III, "port": "usb:000,003", "serial": "A7III0SERIAL",
                       "method": "recovered_probe", "os_source": "ioreg"}]


def test_a_mislabelled_row_whose_os_serial_is_shared_is_marked_duplicate():
    rows = [(A7II, "usb:000,003")]
    shared_a7iii = _dev("ILCE-7M3", "DUP000000001", 1245184, 3)
    shared_a7ii = _dev("ILCE-7M2", "DUP000000001", 1310720, 4)
    with _autodetect(rows), _ioreg([shared_a7iii, shared_a7ii]), \
         patch.object(camera_mod, "_probe_camera_answers", return_value=True) as probe:
        result = detect_identified()
    probe.assert_not_called()
    assert result == [{"model": A7II, "port": "usb:000,003", "serial": None, "method": "autodetect",
                       "os_source": "ioreg", "serial_status": "duplicate"}]


if __name__ == "__main__":
    raise SystemExit(run_module_tests(__name__))
