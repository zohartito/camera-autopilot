#!/usr/bin/env python3
"""
NON-DESTRUCTIVE raw-PTP probe for the Sony A7 II (ILCE-7M2).

Question: does the camera FIRMWARE honor the Sony control opcodes that gphoto2
hides on this mode-2 body? We bypass gphoto2 and send the raw transactions
ourselves to read the true ResponseCode.

Walls tested (read-only intent + tiny reversible nudge):
  W1  AFAreaPosition       propcode 0xD2DC  value UINT32 (x<<16)|y
  W2  ManualFocusAdjust    propcode 0xD2D1  value INT16  +3 then -3 to return

HARD CONSTRAINTS (enforced by this script never issuing them):
  - Only read ops (0x1001/0x1002/0x9201/0x9202/0x9209) and 0x9207 control writes.
  - NO 0x9280/0x9281/0x9282 service/debug, NO firmware/SPK/updater, NO 0x9205.

Self-check first: we parse GetDeviceInfo for "Sony"/"ILCE-7M2" and dump the
0x9209 property blob, confirming known props (ISO 0xD21E, battery 0xD218) are
present. Only if a known read clearly works do we trust the 0xD2DC/0xD2D1
ResponseCodes.

Usage:
    python probe_raw_control.py            # run both walls
    python probe_raw_control.py --w1       # AF-point only (needs AF focusmode)
    python probe_raw_control.py --w1 --af-x 320 --af-y 240 --focus-area 0x102
    python probe_raw_control.py --w2       # manual focus only (needs Manual)
    python probe_raw_control.py --w2 --mf-step 40 --no-return
    python probe_raw_control.py --no-precondition   # skip gphoto2 mode setup
"""

import argparse
import struct
import time
import subprocess
import sys
import time

# ---- constants ----------------------------------------------------------
SONY_VID = 0x054C
A7M2_PID = 0x0A6A

# PTP container types
PTP_CONTAINER_COMMAND = 1
PTP_CONTAINER_DATA = 2
PTP_CONTAINER_RESPONSE = 3
PTP_CONTAINER_EVENT = 4

# Operations
OP_GET_DEVICE_INFO = 0x1001
OP_OPEN_SESSION = 0x1002
OP_CLOSE_SESSION = 0x1003
OP_SDIO_CONNECT = 0x9201
OP_SDIO_GET_EXT_DEVICE_INFO = 0x9202
OP_SDIO_GET_ALL_EXT_DEVICE_PROP_INFO = 0x9209
OP_SDIO_CONTROL_DEVICE = 0x9207

# Sony protocol version to request in 0x9202 for this mode-2 body ("2.00")
SDIO_PROTO_VERSION = 0x00C8

# Property codes
PROP_AF_AREA_POSITION = 0xD2DC  # W1
PROP_FOCUS_AREA = 0xD22C  # optional W1 focus-area mode companion
PROP_MANUAL_FOCUS_ADJUST = 0xD2D1  # W2
PROP_ISO = 0xD21E  # known prop for self-check
PROP_BATTERY = 0xD218  # known prop for self-check

# Response codes
RC_OK = 0x2001
RC_HONORED = {RC_OK}
RC_REJECTED = {
    0x2005: "OperationNotSupported",
    0x200A: "DevicePropNotSupported",
    0x2006: "ParameterNotSupported",
    0x201C: "InvalidDevicePropValue",
    0x200F: "AccessDenied",
}

# USB class for still image / PTP
USB_CLASS_IMAGE = 6

TIMEOUT_MS = 5000
MAX_PTP_CONTAINER_BYTES = 4 * 1024 * 1024
MAX_PTP_TRANSACTION_BYTES = 8 * 1024 * 1024
MAX_PTP_CONTAINERS_PER_TRANSACTION = 64
usb_core = None
usb_util = None


def require_pyusb():
    """Import pyusb only after argparse has handled --help/argument errors."""
    global usb_core, usb_util
    if usb_core is not None and usb_util is not None:
        return
    try:
        import usb.core as _usb_core
        import usb.util as _usb_util
    except ModuleNotFoundError as e:
        raise RuntimeError(
            "pyusb is required for live raw-PTP probes. Install/use the PMCA "
            "environment, for example PYTHON=/tmp/pmca-venv/bin/python."
        ) from e
    usb_core = _usb_core
    usb_util = _usb_util


def rc_name(rc):
    if rc == RC_OK:
        return "PTP_RC_OK"
    if rc in RC_REJECTED:
        return RC_REJECTED[rc]
    return f"unknown(0x{rc:04x})"


def verdict(rc):
    if rc in RC_HONORED:
        return "HONORED"
    if rc in RC_REJECTED:
        return "REJECTED"
    return "UNKNOWN"


# ---- PTP-over-USB transport --------------------------------------------
class PTPUSB:
    def __init__(self, dev, ep_in, ep_out):
        self.dev = dev
        self.ep_in = ep_in
        self.ep_out = ep_out
        self.txid = 0

    def _next_txid(self):
        self.txid += 1
        return self.txid

    def _remaining_timeout_ms(self, deadline):
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise OSError("PTP transaction deadline exceeded")
        return max(1, int(remaining * 1000))

    def _send_command(self, opcode, params, deadline):
        # container: u32 length, u16 type, u16 code, u32 txid, params...
        txid = self._next_txid()
        body = b"".join(struct.pack("<I", p) for p in params)
        length = 12 + len(body)
        if length > MAX_PTP_CONTAINER_BYTES:
            raise ValueError("PTP command exceeds container byte budget")
        packet = struct.pack("<IHHI", length, PTP_CONTAINER_COMMAND, opcode, txid) + body
        self.ep_out.write(packet, self._remaining_timeout_ms(deadline))
        return txid

    def _send_data(self, opcode, txid, payload, deadline):
        length = 12 + len(payload)
        if length > MAX_PTP_CONTAINER_BYTES:
            raise ValueError("PTP data exceeds container byte budget")
        packet = struct.pack("<IHHI", length, PTP_CONTAINER_DATA, opcode, txid) + payload
        self.ep_out.write(packet, self._remaining_timeout_ms(deadline))

    def _read_container(self, deadline):
        # Read first chunk; parse header to learn full length.
        raw = self.ep_in.read(512, self._remaining_timeout_ms(deadline)).tobytes()
        if len(raw) < 12:
            raise OSError(f"short container read: {len(raw)} bytes")
        length, ctype, code, txid = struct.unpack("<IHHI", raw[:12])
        if length < 12 or length > MAX_PTP_CONTAINER_BYTES:
            raise OSError(f"PTP container exceeds byte budget: {length}")
        data = bytearray(raw)
        while len(data) < length:
            chunk = self.ep_in.read(
                min(16384, length - len(data) + 512), self._remaining_timeout_ms(deadline)
            ).tobytes()
            if not chunk:
                break
            data.extend(chunk)
        if len(data) != length:
            raise OSError(f"short PTP container read: expected {length}, got {len(data)}")
        return ctype, code, txid, bytes(data[12:length])

    def transact(self, opcode, params=None, data_out=None, want_data_in=False):
        """Run one PTP transaction.

        Returns (response_code, data_in_bytes). data_in_bytes is b'' if none.
        If data_out is given -> host->device data phase.
        If want_data_in -> expect device->host data phase before response.
        """
        params = params or []
        deadline = time.monotonic() + (TIMEOUT_MS / 1000.0)
        txid = self._send_command(opcode, params, deadline)

        if data_out is not None:
            self._send_data(opcode, txid, data_out, deadline)

        data_in = b""
        container_count = 0
        while True:
            ctype, code, rtxid, payload = self._read_container(deadline)
            container_count += 1
            if container_count > MAX_PTP_CONTAINERS_PER_TRANSACTION:
                raise OSError("PTP transaction exceeds container-count budget")
            if ctype in {PTP_CONTAINER_DATA, PTP_CONTAINER_RESPONSE} and rtxid != txid:
                raise OSError("PTP transaction id mismatch")
            if ctype == PTP_CONTAINER_DATA:
                if len(data_in) + len(payload) > MAX_PTP_TRANSACTION_BYTES:
                    raise OSError("PTP transaction exceeds aggregate byte budget")
                data_in += payload
                continue
            if ctype == PTP_CONTAINER_RESPONSE:
                return code, data_in
            if ctype == PTP_CONTAINER_EVENT:
                # ignore async events interleaved on the IN endpoint
                continue
            raise OSError(f"unexpected container type {ctype} (code 0x{code:04x})")


# ---- device discovery ---------------------------------------------------
def find_camera():
    require_pyusb()
    dev = usb_core.find(idVendor=SONY_VID, idProduct=A7M2_PID)
    if dev is None:
        # fall back: any Sony device whose interface is still-image class
        for d in usb_core.find(find_all=True, idVendor=SONY_VID):
            for cfg in d:
                for intf in cfg:
                    if intf.bInterfaceClass == USB_CLASS_IMAGE:
                        return d
        return None
    return dev


def claim_ptp_interface(dev):
    """Return (intf, ep_in, ep_out). Detaches kernel/macOS driver if needed."""
    dev.set_configuration()
    cfg = dev.get_active_configuration()

    ptp_intf = None
    for intf in cfg:
        if intf.bInterfaceClass == USB_CLASS_IMAGE:
            ptp_intf = intf
            break
    if ptp_intf is None:
        raise RuntimeError("no still-image (class 6) interface found on device")

    # On macOS pyusb cannot detach the kernel driver, but ptpcamerad should not
    # be holding the device in PC Remote mode. Try claim; report precisely.
    try:
        usb_util.claim_interface(dev, ptp_intf)
    except usb_core.USBError as e:
        raise RuntimeError(f"claim_interface failed: {e}") from e

    ep_in = ep_out = None
    for ep in ptp_intf:
        direction = usb_util.endpoint_direction(ep.bEndpointAddress)
        ttype = usb_util.endpoint_type(ep.bmAttributes)
        if ttype != usb_util.ENDPOINT_TYPE_BULK:
            continue
        if direction == usb_util.ENDPOINT_IN:
            ep_in = ep
        else:
            ep_out = ep
    if ep_in is None or ep_out is None:
        raise RuntimeError("could not find bulk IN/OUT endpoints")
    return ptp_intf, ep_in, ep_out


# ---- ptp string/blob helpers -------------------------------------------
def find_ascii_markers(blob, markers):
    """Look for ASCII or UTF-16LE occurrences of marker strings in a blob."""
    found = {}
    for m in markers:
        b_ascii = m.encode("ascii")
        b_utf16 = m.encode("utf-16-le")
        found[m] = (b_ascii in blob) or (b_utf16 in blob)
    return found


def parse_propcodes_present(blob, codes):
    """0x9209 returns a packed array of DevicePropDesc structs. We don't fully
    parse; we just check the 2-byte little-endian propcode appears in the blob."""
    present = {}
    for c in codes:
        present[c] = struct.pack("<H", c) in blob
    return present


# ---- preconditions via gphoto2 -----------------------------------------
def gphoto2_set_focusmode(token):
    print(f"[precondition] gphoto2 --set-config focusmode={token}")
    r = subprocess.run(
        ["gphoto2", "--set-config", f"focusmode={token}"],
        capture_output=True, text=True,
    )
    if r.returncode != 0:
        print(f"[precondition] WARNING set-config failed: {r.stderr.strip()}")
        return False
    print("[precondition] focusmode set OK")
    return True


def kill_gphoto_holders():
    """Make sure no gphoto2/PTPCamera process is holding the device."""
    for name in ("gphoto2", "gvfsd-gphoto2", "gvfs-gphoto2-volume-monitor"):
        subprocess.run(["pkill", "-x", name], capture_output=True)
    # ptpcamerad is Apple's; we do NOT kill it (PC Remote mode releases it).
    time.sleep(1.0)


# ---- handshake ----------------------------------------------------------
def open_and_handshake(ptp):
    print("\n=== TRANSPORT + HANDSHAKE ===")

    # GetDeviceInfo (known-good read #1)
    rc, info = ptp.transact(OP_GET_DEVICE_INFO, want_data_in=True)
    print(f"GetDeviceInfo(0x1001) rc=0x{rc:04x} {rc_name(rc)} datalen={len(info)}")
    if rc != RC_OK:
        raise RuntimeError(f"GetDeviceInfo failed rc=0x{rc:04x}")
    markers = find_ascii_markers(info, ["Sony", "ILCE-7M2", "Sony PTP"])
    print(f"  DeviceInfo markers: {markers}")

    # OpenSession
    rc, _ = ptp.transact(OP_OPEN_SESSION, params=[1])
    print(f"OpenSession(0x1002, sid=1) rc=0x{rc:04x} {rc_name(rc)}")
    if rc not in (RC_OK, 0x201E):  # 0x201E SessionAlreadyOpen is fine
        raise RuntimeError(f"OpenSession failed rc=0x{rc:04x}")

    # Sony SDIO_Connect x3: param1 = 1, then 2, then 3
    for step in (1, 2, 3):
        rc, _ = ptp.transact(OP_SDIO_CONNECT, params=[step, 0, 0])
        print(f"SDIO_Connect(0x9201, p1={step}) rc=0x{rc:04x} {rc_name(rc)}")
        if rc != RC_OK:
            raise RuntimeError(f"SDIO_Connect step {step} failed rc=0x{rc:04x}")

    # SDIO_GetExtDeviceInfo: param1 = protocol version
    rc, ext = ptp.transact(
        OP_SDIO_GET_EXT_DEVICE_INFO, params=[SDIO_PROTO_VERSION], want_data_in=True
    )
    print(
        f"SDIO_GetExtDeviceInfo(0x9202, ver=0x{SDIO_PROTO_VERSION:04x}) "
        f"rc=0x{rc:04x} {rc_name(rc)} datalen={len(ext)}"
    )
    if rc != RC_OK:
        raise RuntimeError(f"SDIO_GetExtDeviceInfo failed rc=0x{rc:04x}")

    # SDIO_GetAllExtDevicePropInfo (known-good read #2): expect multi-KB blob
    rc, blob = ptp.transact(
        OP_SDIO_GET_ALL_EXT_DEVICE_PROP_INFO, want_data_in=True
    )
    print(
        f"SDIO_GetAllExtDevicePropInfo(0x9209) rc=0x{rc:04x} {rc_name(rc)} "
        f"bloblen={len(blob)}"
    )
    if rc != RC_OK:
        raise RuntimeError(f"SDIO_GetAllExtDevicePropInfo failed rc=0x{rc:04x}")

    present = parse_propcodes_present(
        blob, [PROP_ISO, PROP_BATTERY, PROP_FOCUS_AREA, PROP_AF_AREA_POSITION, PROP_MANUAL_FOCUS_ADJUST]
    )
    print("  propcodes present in 0x9209 blob:")
    print(f"    ISO            0xD21E: {present[PROP_ISO]}")
    print(f"    Battery        0xD218: {present[PROP_BATTERY]}")
    print(f"    FocusArea      0xD22C: {present[PROP_FOCUS_AREA]}")
    print(f"    AFAreaPosition 0xD2DC: {present[PROP_AF_AREA_POSITION]}")
    print(f"    ManualFocus    0xD2D1: {present[PROP_MANUAL_FOCUS_ADJUST]}")

    # Self-check verdict
    selfcheck_ok = (
        len(blob) > 1000
        and present[PROP_ISO]
        and present[PROP_BATTERY]
        and (markers["Sony"] or markers["ILCE-7M2"])
    )
    print(f"\n[SELF-CHECK] {'PASS' if selfcheck_ok else 'FAIL'} "
          f"(blob>{1000}B & ISO & battery present & Sony marker)")
    return selfcheck_ok, blob


# ---- control writes -----------------------------------------------------
def control_write(ptp, propcode, value_bytes, label):
    print(f"\n--- {label}: SDIO_ControlDevice(0x9207, prop=0x{propcode:04x}) "
          f"value={value_bytes.hex()} ---")
    rc, _ = ptp.transact(
        OP_SDIO_CONTROL_DEVICE, params=[propcode], data_out=value_bytes
    )
    v = verdict(rc)
    print(f"    ResponseCode = 0x{rc:04x} ({rc_name(rc)})  -> {v}")
    return rc, v


def probe_w1(ptp, x, y, focus_area=None):
    # AFAreaPosition: UINT32 = (x<<16)|y.
    if focus_area is not None:
        control_write(
            ptp, PROP_FOCUS_AREA, struct.pack("<H", focus_area),
            f"W1 focus-area mode 0x{focus_area:x}"
        )
    value = struct.pack("<I", (x << 16) | y)
    return control_write(
        ptp, PROP_AF_AREA_POSITION, value,
        f"W1 AF-point (x={x},y={y})"
    )


def probe_w2(ptp, step, return_after=True):
    # ManualFocusAdjust: INT16 nudge, optionally followed by inverse nudge.
    rc1, v1 = control_write(
        ptp, PROP_MANUAL_FOCUS_ADJUST, struct.pack("<h", step),
        f"W2 manual focus {step:+d}"
    )
    if return_after:
        time.sleep(0.4)
        rc2, v2 = control_write(
            ptp, PROP_MANUAL_FOCUS_ADJUST, struct.pack("<h", -step),
            f"W2 manual focus {-step:+d} (return)"
        )
    # report on the forward nudge as the primary result
    return rc1, v1


def parse_int(value):
    try:
        return int(value, 0)
    except ValueError as e:
        raise argparse.ArgumentTypeError(f"expected integer or 0x-prefixed integer: {value}") from e


# ---- main ---------------------------------------------------------------
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--w1", action="store_true", help="AF-point only")
    ap.add_argument("--w2", action="store_true", help="manual focus only")
    ap.add_argument("--af-x", type=int, default=320, help="AF-point x coordinate for W1")
    ap.add_argument("--af-y", type=int, default=240, help="AF-point y coordinate for W1")
    ap.add_argument(
        "--focus-area",
        type=parse_int,
        default=None,
        help="optional FocusArea mode value to send before W1, e.g. 0x102",
    )
    ap.add_argument("--mf-step", type=int, default=3, help="signed manual-focus step for W2")
    ap.add_argument("--no-return", action="store_true", help="do not send the inverse W2 return step")
    ap.add_argument("--no-precondition", action="store_true",
                    help="skip gphoto2 focusmode setup")
    args = ap.parse_args()

    do_w1 = args.w1 or not (args.w1 or args.w2)
    do_w2 = args.w2 or not (args.w1 or args.w2)

    # Preconditions via gphoto2 BEFORE pyusb claims the device.
    if not args.no_precondition:
        # W2 requires Manual; if both requested, W2's Manual mode is harmless
        # for W1 too (W1 just exercises the AF-point write handler). We set
        # Manual when W2 is in scope, else leave AF default for W1.
        if do_w2:
            gphoto2_set_focusmode("Manual")
        elif do_w1:
            gphoto2_set_focusmode("Automatic")
        kill_gphoto_holders()

    dev = find_camera()
    if dev is None:
        print("ERROR: Sony A7 II (054c:0a6a) not found on USB.")
        sys.exit(2)
    print(f"Found device: VID=0x{dev.idVendor:04x} PID=0x{dev.idProduct:04x}")

    intf = None
    try:
        intf, ep_in, ep_out = claim_ptp_interface(dev)
    except Exception as e:
        print(f"TRANSPORT BLOCKER: could not claim PTP interface: {e}")
        print("  (On macOS this usually means ptpcamerad or another process "
              "holds the device. Confirm camera is in PC Remote mode and no "
              "gphoto2/Image Capture/Lightroom is connected.)")
        sys.exit(3)

    ptp = PTPUSB(dev, ep_in, ep_out)

    results = {}
    try:
        selfcheck_ok, _ = open_and_handshake(ptp)
        if not selfcheck_ok:
            print("\nSELF-CHECK FAILED — refusing to trust control results. "
                  "Reporting transport blocker, not a firmware reject.")
            sys.exit(4)

        if do_w1:
            results["W1"] = probe_w1(ptp, args.af_x, args.af_y, args.focus_area)
        if do_w2:
            results["W2"] = probe_w2(ptp, args.mf_step, return_after=not args.no_return)

        # close session cleanly
        try:
            ptp.transact(OP_CLOSE_SESSION)
        except Exception:
            pass

    finally:
        try:
            if usb_util is not None:
                usb_util.release_interface(dev, intf)
                usb_util.dispose_resources(dev)
        except Exception:
            pass

    print("\n=== SUMMARY ===")
    for wall, (rc, v) in results.items():
        print(f"  {wall}: ResponseCode 0x{rc:04x} ({rc_name(rc)}) -> {v}")


if __name__ == "__main__":
    main()
