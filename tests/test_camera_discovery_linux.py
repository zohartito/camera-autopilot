"""Linux identity-first discovery: the USB serial comes from sysfs, the same stable identity macOS
reads from ioreg. No hardware and no real /sys: every test builds a fake sysfs tree in a temporary
directory, and gphoto2 autodetect and the read-only probe are mocked.

Ports re-enumerate; the serial is the only stable identity (.claude/lessons.md, "Identification";
docs/ROUTE_MATRIX_2026-09-15.md). gphoto2 once listed the A7 II as "Apple iPad Air".
"""
from __future__ import annotations

import json
import re
import subprocess
import sys
import tempfile
from contextlib import contextmanager
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "backend"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import sonyremote.camera as camera_mod
from sonyremote.camera import detect_identified
from _runner import run_module_tests

A7II = "Sony ILCE-7M2 (Control)"
A7III = "Sony Alpha-A7 III (PC Control)"
A7III_SERIAL = "A7III0SERIAL"
A7II_SERIAL = "A7II00SERIAL"
IPAD_SERIAL = "IPADSERIAL00000000000000"
INDEX = (ROOT / "backend" / "web" / "index.html").read_text(encoding="utf-8")


def _node(root: Path, name: str, **attrs: str | None) -> None:
    """One USB device the way the kernel lays it out: a real node under /sys/devices and a
    symlink to it from /sys/bus/usb/devices. An attr of None means the file does not exist."""
    real = root / "devices" / "platform" / "xhci-hcd.0" / name
    real.mkdir(parents=True)
    for key, value in attrs.items():
        if value is not None:
            (real / key).write_text(f"{value}\n", encoding="ascii")
    links = root / "bus" / "usb" / "devices"
    links.mkdir(parents=True, exist_ok=True)
    (links / name).symlink_to(real, target_is_directory=True)


def _sony(root: Path, name: str, product: str, serial: str | None, busnum: int, devnum: int,
          id_product: str = "0c34") -> None:
    _node(root, name, idVendor="054c", idProduct=id_product, product=product, serial=serial,
          busnum=str(busnum), devnum=str(devnum))


def _background(root: Path) -> None:
    """What every real tree also has: a root hub and an interface node of the Sony device."""
    _node(root, "usb1", idVendor="1d6b", idProduct="0002", product="xHCI Host Controller",
          serial="xhci-hcd.0", busnum="1", devnum="1")
    _node(root, "1-1:1.0", bInterfaceClass="06")


@contextmanager
def _fake_sysfs():
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        _background(root)
        yield root


@contextmanager
def _linux(root: Path, rows, probe_result: bool = True):
    """Run discovery as Linux against the fake tree; gphoto and the probe are mocks."""
    with patch.object(camera_mod.sys, "platform", "linux"), \
         patch.object(camera_mod, "SYSFS_ROOT", root), \
         patch.object(camera_mod, "_autodetect_rows", return_value=list(rows)), \
         patch.object(camera_mod, "_probe_camera_answers", return_value=probe_result) as probe:
        yield probe


# --- the sysfs reader ------------------------------------------------------------------

def test_two_bodies_on_different_bus_dev_numbers_get_their_own_serial_model_and_port():
    with _fake_sysfs() as root:
        _sony(root, "1-1", "ILCE-7M3", A7III_SERIAL, busnum=1, devnum=5)
        _sony(root, "2-1", "ILCE-7M2", A7II_SERIAL, busnum=2, devnum=3, id_product="0a6b")
        bodies = camera_mod._sysfs_supported_bodies(camera_mod._read_sysfs_usb_devices(root))
        rows = [(A7III, "usb:001,005"), (A7II, "usb:002,003")]
        with _linux(root, rows) as probe:
            result = detect_identified()
    assert {b["port"]: b for b in bodies} == {
        "usb:001,005": {"model": A7III, "port": "usb:001,005", "serial": A7III_SERIAL},
        "usb:002,003": {"model": A7II, "port": "usb:002,003", "serial": A7II_SERIAL},
    }
    probe.assert_not_called()
    assert {r["port"]: (r["model"], r["serial"], r["method"], r["os_source"]) for r in result} == {
        "usb:001,005": (A7III, A7III_SERIAL, "autodetect", "sysfs"),
        "usb:002,003": (A7II, A7II_SERIAL, "autodetect", "sysfs"),
    }


def test_serial_is_paired_with_its_own_node_not_with_directory_order():
    # Directory names sort as 1-1, 3-4, but the bus/dev numbers sort the other way round.
    with _fake_sysfs() as root:
        _sony(root, "1-1", "ILCE-7M2", A7II_SERIAL, busnum=3, devnum=12)
        _sony(root, "3-4", "ILCE-7M3", A7III_SERIAL, busnum=1, devnum=2)
        devices = camera_mod._read_sysfs_usb_devices(root)
        rows = [(A7III, "usb:001,002"), (A7II, "usb:003,012")]  # gphoto lists in port order
        with _linux(root, rows):
            result = detect_identified()
    by_node = {d["sysfs_name"]: d for d in devices}
    assert (by_node["1-1"]["serial"], by_node["1-1"]["busnum"], by_node["1-1"]["devnum"]) == \
        (A7II_SERIAL, "3", "12")
    assert (by_node["3-4"]["serial"], by_node["3-4"]["busnum"], by_node["3-4"]["devnum"]) == \
        (A7III_SERIAL, "1", "2")
    by_port = {r["port"]: r for r in result}
    assert by_port["usb:001,002"]["serial"] == A7III_SERIAL
    assert by_port["usb:003,012"]["serial"] == A7II_SERIAL


def test_the_same_serial_identifies_the_body_after_re_enumeration():
    with _fake_sysfs() as root:
        _sony(root, "1-1", "ILCE-7M3", A7III_SERIAL, busnum=1, devnum=5)
        with _linux(root, [(A7III, "usb:001,005")]):
            before = detect_identified()
    # Unplugged and replugged into another socket: new bus, new devnum, new sysfs name.
    with _fake_sysfs() as root:
        _sony(root, "2-3", "ILCE-7M3", A7III_SERIAL, busnum=2, devnum=11)
        with _linux(root, [(A7III, "usb:002,011")]):
            after = detect_identified()
        # And when gphoto misses it after the move, recovery finds it at the new port by serial.
        with _linux(root, []) as probe:
            recovered = detect_identified()
    assert [(r["port"], r["serial"]) for r in before] == [("usb:001,005", A7III_SERIAL)]
    assert [(r["port"], r["serial"]) for r in after] == [("usb:002,011", A7III_SERIAL)]
    probe.assert_called_once_with(A7III, "usb:002,011")
    assert recovered == [{"model": A7III, "port": "usb:002,011", "serial": A7III_SERIAL,
                          "method": "recovered_probe", "os_source": "sysfs"}]


def test_non_sony_device_is_ignored_and_never_probed():
    # The live case: gphoto names the A7 II "Apple iPad Air" while a real iPad is attached. The
    # iPad even claims an ILCE product string here; its Apple vendor id still excludes it.
    with _fake_sysfs() as root:
        _node(root, "1-2", idVendor="05ac", idProduct="12ab", product="ILCE-7M2",
              serial=IPAD_SERIAL, busnum="1", devnum="4")
        _sony(root, "2-1", "ILCE-7M2", A7II_SERIAL, busnum=2, devnum=3)
        devices = camera_mod._read_sysfs_usb_devices(root)
        rows = [("Apple iPad Air", "usb:001,004"), ("Apple iPad Air", "usb:002,003")]
        with _linux(root, rows) as probe:
            result = detect_identified()
    assert [d["sysfs_name"] for d in devices] == ["2-1"]  # root hub, iPad and interface dropped
    assert IPAD_SERIAL not in json.dumps(devices)  # a non-Sony serial is never even collected
    probe.assert_called_once_with(A7II, "usb:002,003")
    assert "usb:001,004" not in {call.args[1] for call in probe.call_args_list}
    assert IPAD_SERIAL not in json.dumps(result)
    by_port = {r["port"]: r for r in result}
    assert by_port["usb:002,003"] == {"model": A7II, "port": "usb:002,003", "serial": A7II_SERIAL,
                                      "method": "recovered_probe", "os_source": "sysfs"}
    assert by_port["usb:001,004"] == {"model": "Apple iPad Air", "port": "usb:001,004",
                                      "serial": None, "method": "autodetect", "os_source": "sysfs"}


def test_missing_or_empty_serial_fails_closed():
    for serial in (None, "", "   "):  # no serial file at all, an empty one, whitespace only
        with _fake_sysfs() as root:
            _sony(root, "1-1", "ILCE-7M3", serial, busnum=1, devnum=5)
            devices = camera_mod._read_sysfs_usb_devices(root)
            # gphoto names the body correctly: the row stays, with no identity.
            with _linux(root, [(A7III, "usb:001,005")]) as probe:
                listed = detect_identified()
            # gphoto misses or mislabels it: no serial means nothing to recover, so no probe.
            with _linux(root, [("Apple iPad Air", "usb:001,005")]) as probe_missed:
                mislabelled = detect_identified()
        assert devices[0]["serial"] is None, serial
        probe.assert_not_called()
        probe_missed.assert_not_called()
        assert listed == [{"model": A7III, "port": "usb:001,005", "serial": None,
                           "method": "autodetect", "os_source": "sysfs"}]
        assert mislabelled == [{"model": "Apple iPad Air", "port": "usb:001,005", "serial": None,
                                "method": "autodetect", "os_source": "sysfs"}]
        # Never guessed from the model: the known A7 III serial is not attached anywhere.
        assert A7III_SERIAL not in json.dumps(listed + mislabelled)


def test_a_serial_shared_by_two_nodes_identifies_neither_and_is_never_probed():
    with _fake_sysfs() as root:
        _sony(root, "1-1", "ILCE-7M3", "DUP", busnum=1, devnum=5)
        _sony(root, "2-1", "ILCE-7M2", "DUP", busnum=2, devnum=3)
        with _linux(root, [(A7III, "usb:001,005"), (A7II, "usb:002,003")]):
            listed = detect_identified()
        # gphoto lists neither: two different models, so only the shared serial stops the probe.
        with _linux(root, []) as probe_missed:
            recovered = detect_identified()
    assert {r["port"]: r for r in listed} == {
        "usb:001,005": {"model": A7III, "port": "usb:001,005", "serial": None, "method": "autodetect",
                        "os_source": "sysfs", "serial_status": "duplicate"},
        "usb:002,003": {"model": A7II, "port": "usb:002,003", "serial": None, "method": "autodetect",
                        "os_source": "sysfs", "serial_status": "duplicate"},
    }
    probe_missed.assert_not_called()
    assert recovered == []
    assert "DUP" not in json.dumps(listed)


def test_the_consumer_never_sees_a_duplicate_serial_as_identity():
    from engine import RealEngine
    with _fake_sysfs() as root:
        _sony(root, "1-1", "ILCE-7M3", "DUP", busnum=1, devnum=5)
        _sony(root, "2-1", "ILCE-7M3", "DUP", busnum=2, devnum=3)
        with _linux(root, [(A7III, "usb:001,005"), (A7III, "usb:002,003")]):
            status = RealEngine().status()
    assert [(c["port"], c["serial"]) for c in status["cameras"]] == \
        [("usb:001,005", None), ("usb:002,003", None)]


def test_a_node_replaced_between_reads_is_dropped():
    # The A7 II at 1-1 (usb:001,005) is unplugged and the A7 III lands on the same sysfs path as
    # usb:001,006 while the scan is reading it: the serial read belongs to one enumeration and the
    # bus/dev numbers may belong to the other, so the row must not be reported at all.
    real_read = camera_mod._read_sysfs_attr
    with _fake_sysfs() as root:
        _sony(root, "1-1", "ILCE-7M2", A7II_SERIAL, busnum=1, devnum=5)
        node = (root / "bus" / "usb" / "devices" / "1-1").resolve()

        def read_during_replug(path, *args, **kwargs):
            value = real_read(path, *args, **kwargs)
            if path.name == "serial" and (node / "devnum").read_text() == "5\n":
                for attr, new in (("product", "ILCE-7M3"), ("serial", A7III_SERIAL), ("devnum", "6")):
                    (node / attr).write_text(f"{new}\n", encoding="ascii")
            return value

        with patch.object(camera_mod, "_read_sysfs_attr", side_effect=read_during_replug):
            devices = camera_mod._read_sysfs_usb_devices(root)
        after = camera_mod._read_sysfs_usb_devices(root)  # the next scan sees the new body whole
    assert devices == []
    assert [(d["serial"], d["busnum"], d["devnum"]) for d in after] == [(A7III_SERIAL, "1", "6")]


def test_a_node_swapped_to_a_non_sony_device_after_the_vendor_read_is_dropped():
    real_read = camera_mod._read_sysfs_attr
    with _fake_sysfs() as root:
        _sony(root, "1-1", "ILCE-7M2", A7II_SERIAL, busnum=1, devnum=5)
        node = (root / "bus" / "usb" / "devices" / "1-1").resolve()

        def read_during_swap(path, *args, **kwargs):
            value = real_read(path, *args, **kwargs)
            if path.name == "idVendor" and value == "054c":
                # An iPad takes the path right after the Sony vendor check, claiming an ILCE name.
                for attr, new in (("idVendor", "05ac"), ("product", "ILCE-7M2"),
                                  ("serial", IPAD_SERIAL), ("devnum", "6")):
                    (node / attr).write_text(f"{new}\n", encoding="ascii")
            return value

        with patch.object(camera_mod, "_read_sysfs_attr", side_effect=read_during_swap):
            devices = camera_mod._read_sysfs_usb_devices(root)
    assert devices == []
    assert IPAD_SERIAL not in json.dumps(devices)


def test_one_device_recorded_twice_is_not_a_duplicate_serial():
    # The same body (same port, same serial) seen at two sysfs nodes mid-scan...
    with _fake_sysfs() as root:
        _sony(root, "1-1", "ILCE-7M3", A7III_SERIAL, busnum=1, devnum=5)
        _sony(root, "1-1.1", "ILCE-7M3", A7III_SERIAL, busnum=1, devnum=5)
        with _linux(root, [(A7III, "usb:001,005")]):
            linux_rows = detect_identified()
    # ...and two identical ioreg records for one body.
    a7iii_ioreg = {"USB Product Name": "ILCE-7M3", "kUSBSerialNumberString": A7III_SERIAL,
                   "locationID": "1245184", "USB Address": "3", "USB Vendor Name": "Sony",
                   "idVendor": "1356"}
    with patch.object(camera_mod, "_autodetect_rows", return_value=[(A7III, "usb:000,003")]), \
         patch.object(camera_mod, "_read_os_usb_devices", return_value=[a7iii_ioreg, dict(a7iii_ioreg)]), \
         patch.object(camera_mod, "_probe_camera_answers") as probe:
        mac_rows = detect_identified()
    probe.assert_not_called()
    assert linux_rows == [{"model": A7III, "port": "usb:001,005", "serial": A7III_SERIAL,
                           "method": "autodetect", "os_source": "sysfs"}]
    assert mac_rows == [{"model": A7III, "port": "usb:000,003", "serial": A7III_SERIAL,
                         "method": "autodetect", "os_source": "ioreg"}]


def test_a_carriage_return_in_a_serial_is_not_newline_normalised():
    with _fake_sysfs() as root:
        _sony(root, "1-1", "ILCE-7M3", A7III_SERIAL, busnum=1, devnum=5)
        _sony(root, "2-1", "ILCE-7M3", A7III_SERIAL, busnum=2, devnum=3)
        ((root / "bus/usb/devices/2-1").resolve() / "serial").write_bytes(f"{A7III_SERIAL}\r\n".encode())
        devices = camera_mod._read_sysfs_usb_devices(root)
        with _linux(root, [(A7III, "usb:001,005"), (A7III, "usb:002,003")]):
            result = detect_identified()
    assert {d["sysfs_name"]: d["serial"] for d in devices} == \
        {"1-1": A7III_SERIAL, "2-1": f"{A7III_SERIAL}\r"}
    assert {r["port"]: r["serial"] for r in result} == \
        {"usb:001,005": A7III_SERIAL, "usb:002,003": f"{A7III_SERIAL}\r"}
    assert all("serial_status" not in r for r in result)


def test_serial_whitespace_is_part_of_the_identity():
    padded = f" {A7III_SERIAL} "
    with _fake_sysfs() as root:
        _sony(root, "1-1", "ILCE-7M3", A7III_SERIAL, busnum=1, devnum=5)
        _sony(root, "2-1", "ILCE-7M3", padded, busnum=2, devnum=3)
        _node(root, "3-2", idVendor="054c", product="ILCE-7M2", busnum="3", devnum="2")
        ((root / "bus/usb/devices/3-2").resolve() / "serial").write_bytes(b"CA19\xe9802E7FC4\n")
        devices = camera_mod._read_sysfs_usb_devices(root)
        with _linux(root, [(A7III, "usb:001,005"), (A7III, "usb:002,003")]):
            result = detect_identified()
    # Only the kernel's trailing newline is removed; " S " and "S" stay two identities.
    assert {d["sysfs_name"]: d["serial"] for d in devices} == \
        {"1-1": A7III_SERIAL, "2-1": padded, "3-2": None}  # non-ASCII -> unavailable
    assert {r["port"]: r["serial"] for r in result} == {"usb:001,005": A7III_SERIAL,
                                                        "usb:002,003": padded}
    assert all("serial_status" not in r for r in result)


def test_unreadable_sysfs_falls_back_to_plain_autodetect():
    with tempfile.TemporaryDirectory() as tmp:  # no bus/usb/devices at all
        with _linux(Path(tmp), [(A7III, "usb:001,005")]) as probe:
            result = detect_identified()
    probe.assert_not_called()
    assert result == [{"model": A7III, "port": "usb:001,005", "serial": None,
                       "method": "autodetect", "os_source": "sysfs_unavailable"}]


def test_probe_budget_is_bounded_on_sysfs():
    with _fake_sysfs() as root:
        _sony(root, "1-1", "ILCE-7M3", A7III_SERIAL, busnum=1, devnum=5)
        _sony(root, "2-1", "ILCE-7M2", A7II_SERIAL, busnum=2, devnum=3)
        with _linux(root, []) as probe, patch.object(camera_mod, "MAX_RECOVERY_PROBES", 1):
            result = detect_identified()
    assert probe.call_count == 1
    assert sum(r["method"] == "recovered_probe" for r in result) == 1


# --- the selection that consumes these rows --------------------------------------------

def _restore_index(saved: dict | None, cameras: list[dict]) -> int:
    """Run the page's own restore code (every line from the parsed saved pair up to and including
    `const restored = ...;`, extracted from index.html) in node."""
    block = re.search(r"try \{ savedPair = JSON\.parse\(savedCamera\); \} catch \(_\) \{\}\n"
                      r"(.*?const restored = [^\n]*)\n", INDEX, re.S).group(1)
    script = (f"const savedPair = {json.dumps(saved)};\n"
              f"const availableCameras = {json.dumps(cameras)};\n"
              f"{block}\nprocess.stdout.write(String(restored));")
    return int(subprocess.run(["node", "-e", script], check=True, capture_output=True,
                              text=True).stdout)


def test_a_remembered_camera_is_restored_only_by_its_serial():
    a7iii = {"model": A7III, "port": "usb:001,005", "serial": A7III_SERIAL}
    saved = dict(a7iii)
    assert _restore_index(saved, [a7iii]) == 0
    # Same label and port, but another body (or no serial): never restored by port or label.
    assert _restore_index(saved, [dict(a7iii, serial="AAAA1111BBBB")]) == -1
    assert _restore_index(saved, [dict(a7iii, serial=None)]) == -1
    # A remembered pair without a serial (older build, or a serial-less row) restores nothing.
    assert _restore_index({"model": A7III, "port": "usb:001,005"}, [a7iii]) == -1
    assert _restore_index({"model": A7III, "port": "usb:001,005", "serial": None},
                          [dict(a7iii, serial=None)]) == -1


def test_a_remembered_camera_is_restored_after_its_port_re_enumerates():
    # Ports move on every reconnect (the A7 III went 000,003 -> 000,005 -> 000,004 on 2026-09-22).
    # The serial is the identity; requiring the old port too would make the memory useless.
    saved = {"model": A7III, "port": "usb:001,005", "serial": A7III_SERIAL}
    moved = {"model": A7III, "port": "usb:001,009", "serial": A7III_SERIAL}
    other = {"model": A7II, "port": "usb:001,005", "serial": A7II_SERIAL}
    assert _restore_index(saved, [other, moved]) == 1


def test_two_rows_with_the_remembered_serial_restore_neither():
    saved = {"model": A7III, "port": "usb:001,005", "serial": "DUP"}
    first = {"model": A7III, "port": "usb:001,005", "serial": "DUP"}
    second = {"model": A7III, "port": "usb:002,003", "serial": "DUP"}
    assert _restore_index(saved, [first, second]) == -1
    assert _restore_index(saved, [second]) == 0  # exactly one match still restores


def test_only_a_serial_identified_camera_is_remembered():
    handler = re.search(r'\$\("camera-select"\)\.addEventListener\("change".*?\n  \}\);', INDEX, re.S)
    assert handler, "camera-select change handler not found"
    body = handler.group(0)
    assert ("if (camera?.serial) Autopilot.deviceStore.write(Autopilot.deviceStore.CAMERA_KEY, "
            "JSON.stringify({model:camera.model,port:camera.port,serial:camera.serial}));") in body
    assert "else Autopilot.deviceStore.remove(Autopilot.deviceStore.CAMERA_KEY);" in body


if __name__ == "__main__":
    raise SystemExit(run_module_tests(__name__))
