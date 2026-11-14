"""Day-one board tests for the CM5 on the Waveshare CM5-NANO-A (see CM5_BRINGUP.md).

Each test is a DRY RUN by default: it prints the plan and the receipt it WOULD write, and writes
and runs nothing. ``--run`` executes it on the board and writes one timestamped JSON receipt,
``<receipt-dir>/day1-<test>-<UTC>.json`` (directory 0700, file 0600). The receipt dir must lie
outside the repository and outside system directories (deploy/safe_paths.py), in both modes.

  autodetect      1. ``gphoto2 --auto-detect``, matched to the Sony device in sysfs by USB serial,
                     plus the desktop-automounter check.
  capture-loop    2. the EXISTING 20-shot no-drop proof (``deploy/pi_bringup.sh --phase proof``) for
                     one body, identified by USB serial, with throttle and temperature sampled
                     throughout. The RAW criterion is the proof's ``raw_transfer`` verdict: every
                     accepted shot's ARW present, hashed and inside the body's size band
                     (deploy/raw_proof.py). ``kill_criterion_passed`` needs all of it.
  usb-replug-idle 3. with the service STOPPED and the body idle: unplug and replug the camera's USB,
                     then climb the recovery ladder (re-enumeration, sysfs ``unbind``/``bind``,
                     physical power cycle) and record which rung worked. NOT a mid-session test:
                     interrupting USB during a live capture is not covered (see CM5_BRINGUP.md).
  sleep-wake      3b. with the service stopped: let the body auto-power-off, wake it, and climb the
                     same ladder; records whether sleep needed more than a wake.
  ap-soak         4. the onboard-Wi-Fi access point (the ``camera-puck`` profile) for 8 hours with a
                     phone connected, logging disconnects and brcmfmac kernel errors. An unreadable
                     kernel log makes the soak inconclusive, never a pass.
  merge-thermal   5. sustained 24 MP merges through ``tools/puck_workload_bench.py`` with
                     temperature and throttle logging; label the run open-air or closed-box.

Camera receipts carry ``identity``: the USB serial read back from the device. ``--body`` is only
a claim; a serial that disagrees with it fails the test. Exit codes: 0 pass or dry run, 1 fail,
3 inconclusive. Nothing touches a camera, the network, sysfs or a subprocess at import time or in
dry run. Receipts are hardware receipts only when they come from the board itself.
"""
from __future__ import annotations

import sys

sys.dont_write_bytecode = True  # "writes nothing" includes __pycache__

import argparse  # noqa: E402
from datetime import datetime, timezone  # noqa: E402
import hashlib  # noqa: E402
import json  # noqa: E402
import os  # noqa: E402
from pathlib import Path  # noqa: E402
import re  # noqa: E402
import subprocess  # noqa: E402
import tempfile  # noqa: E402
import threading  # noqa: E402
import time  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from deploy.cm5_nano import MODEL_PATH, board_facts, read_model  # noqa: E402
from deploy import raw_proof  # noqa: E402
from deploy.safe_paths import path_arg, path_text, refusal, resolve_output  # noqa: E402

SCHEMA = "camera-day-one-v1"
SONY_VENDOR_ID = "054c"
# Legacy placeholder identities, overridable with --expect-serial. The gphoto label
# is not an identity: an asleep A7 III can list as the A7 II.
KNOWN_BODY_SERIALS = {"a7ii": "A7II00SERIAL", "a7iii": "A7III0SERIAL"}
AUTOMOUNTERS = ("gvfs-gphoto2-volume-monitor", "gvfsd-gphoto2")
SERVICE_PATTERN = "uvicorn app:app"
KILL_CRITERION_SHOTS = 20
# The USB serial is read from THIS machine's sysfs, so the service that fires must be on this
# machine too. The proof passes that serial to /shoot, and the service checks it at the port on
# ITS host (sonyremote reads Linux sysfs as well as macOS ioreg; missing or duplicate serials fail
# closed), but the before/after identity and throttle evidence is this machine's, so a remote
# --host is refused. IPv6 ::1 is left
# out: pi_bringup.sh builds http://HOST:PORT without brackets, so it would not be a valid URL.
LOOPBACK_HOSTS = ("127.0.0.1", "localhost")
EXIT_INCONCLUSIVE = 3
# vcgencmd get_throttled bits (Raspberry Pi docs, "vcgencmd get_throttled").
THROTTLE_BITS = {
    0: "under_voltage", 1: "arm_frequency_capped", 2: "throttled", 3: "soft_temperature_limit",
    16: "under_voltage_occurred", 17: "arm_frequency_capping_occurred",
    18: "throttling_occurred", 19: "soft_temperature_limit_occurred",
}
BRCMF_ERROR_WORDS = ("error", "fail", "timeout", "crash", "trap")


# ---- host access (the only code that touches the machine) -------------------------------------
class Host:
    """Everything that touches the machine. Tests substitute a fake; nothing runs at import."""

    def __init__(self, sys_root: Path = Path("/sys"), model_path: Path = MODEL_PATH):
        self.sys_root = sys_root
        self.model_path = model_path

    def run(self, argv: list[str], timeout: float = 60) -> dict:
        started = time.monotonic()
        try:
            done = subprocess.run(argv, capture_output=True, text=True, timeout=timeout, check=False)
            result = {"returncode": done.returncode, "stdout": done.stdout, "stderr": done.stderr[-4000:]}
        except FileNotFoundError:
            result = {"returncode": 127, "stdout": "", "stderr": f"{argv[0]}: not found"}
        except subprocess.TimeoutExpired:
            result = {"returncode": None, "stdout": "", "stderr": f"timed out after {timeout}s",
                      "timed_out": True}
        result.update(argv=[str(a) for a in argv], wall_s=round(time.monotonic() - started, 3))
        return result

    def read(self, path: Path) -> str | None:
        try:
            return Path(path).read_text().strip()
        except OSError:
            return None

    def write(self, path: Path, text: str) -> None:
        Path(path).write_text(text)

    def monotonic(self) -> float:
        return time.monotonic()

    def sleep(self, seconds: float) -> None:
        time.sleep(seconds)

    def prompt(self, message: str) -> str:
        return input(message)

    def euid(self) -> int:
        return os.geteuid()


# ---- pure parsers ------------------------------------------------------------------------------
def parse_throttled(text: str | None) -> dict | None:
    """``throttled=0x50005`` -> value plus the named flags that are set now / since boot."""
    match = re.search(r"0x[0-9a-fA-F]+", text or "")
    if not match:
        return None
    value = int(match.group(0), 16)
    names = [name for bit, name in THROTTLE_BITS.items() if value & (1 << bit)]
    return {"raw": match.group(0), "value": value,
            "now": [n for n in names if not n.endswith("_occurred")],
            "since_boot": [n for n in names if n.endswith("_occurred")]}


def parse_measure_temp(text: str | None) -> float | None:
    match = re.search(r"temp=([0-9.]+)", text or "")
    return float(match.group(1)) if match else None


def parse_measure_clock(text: str | None) -> int | None:
    match = re.search(r"=\s*([0-9]+)", text or "")
    return int(match.group(1)) if match else None


def parse_autodetect(text: str | None) -> list[dict]:
    """Rows of ``gphoto2 --auto-detect``: model names contain spaces, the port is the last token."""
    rows = []
    for line in (text or "").splitlines():
        match = re.match(r"^(?P<model>\S.*?)\s+(?P<port>(?:usb|ptpip|serial):\S*)\s*$", line)
        if match and not line.startswith("Model"):
            rows.append({"model": match.group("model"), "port": match.group("port")})
    return rows


def parse_iw_info(text: str | None) -> dict:
    info = {"type": None, "channel": None, "ssid": None}
    for line in (text or "").splitlines():
        parts = line.strip().split()
        if len(parts) >= 2 and parts[0] == "type":
            info["type"] = parts[1]
        elif len(parts) >= 2 and parts[0] == "channel" and parts[1].isdigit():
            info["channel"] = int(parts[1])
        elif len(parts) >= 2 and parts[0] == "ssid":
            info["ssid"] = line.strip()[5:]
    return info


def station_id(mac: str) -> str:
    """Stations are logged by a short hash so receipts carry no phone MAC address."""
    return hashlib.sha256(mac.lower().encode()).hexdigest()[:12]


def parse_station_dump(text: str | None) -> dict[str, dict]:
    stations: dict[str, dict] = {}
    current = None
    for line in (text or "").splitlines():
        head = re.match(r"^Station ([0-9a-fA-F:]{17})", line)
        if head:
            current = {"connected_time_s": None, "signal_dbm": None, "inactive_ms": None}
            stations[station_id(head.group(1))] = current
            continue
        if current is None:
            continue
        key, _, value = line.strip().partition(":")
        number = re.match(r"\s*(-?[0-9]+)", value)
        if not number:
            continue
        if key == "connected time":
            current["connected_time_s"] = int(number.group(1))
        elif key == "signal":
            current["signal_dbm"] = int(number.group(1))
        elif key == "inactive time":
            current["inactive_ms"] = int(number.group(1))
    return stations


def station_events(previous: dict[str, dict], current: dict[str, dict]) -> list[dict]:
    """Joins, leaves, and reassociations (connected time went backwards between samples)."""
    events = [{"event": "join", "station": s} for s in current if s not in previous]
    events += [{"event": "leave", "station": s} for s in previous if s not in current]
    for s in current.keys() & previous.keys():
        before, after = previous[s].get("connected_time_s"), current[s].get("connected_time_s")
        if before is not None and after is not None and after < before:
            events.append({"event": "reassociate", "station": s})
    return events


def brcmf_error_lines(journal: str | None) -> list[str]:
    return [line for line in (journal or "").splitlines()
            if "brcmf" in line and any(word in line.lower() for word in BRCMF_ERROR_WORDS)]


# ---- health sampling (vcgencmd + thermal zone) -------------------------------------------------
def health_sample(host: Host, t0: float) -> dict:
    throttled = host.run(["vcgencmd", "get_throttled"], timeout=5)
    temp = host.run(["vcgencmd", "measure_temp"], timeout=5)
    clock = host.run(["vcgencmd", "measure_clock", "arm"], timeout=5)
    zone = host.read(host.sys_root / "class/thermal/thermal_zone0/temp")
    return {"t_s": round(host.monotonic() - t0, 1),
            "throttled": parse_throttled(throttled["stdout"]) if throttled["returncode"] == 0 else None,
            "soc_temp_c": parse_measure_temp(temp["stdout"]) if temp["returncode"] == 0 else None,
            "thermal_zone0_c": round(int(zone) / 1000.0, 2) if zone and zone.isdigit() else None,
            "arm_clock_hz": parse_measure_clock(clock["stdout"]) if clock["returncode"] == 0 else None}


class Sampler(threading.Thread):
    """Samples health immediately, every ``interval`` seconds, and once more on stop."""

    def __init__(self, host: Host, interval: float, t0: float):
        super().__init__(daemon=True)
        self.host, self.interval, self.t0 = host, interval, t0
        self.samples: list[dict] = []
        self._stop_event = threading.Event()

    def run(self) -> None:
        while True:
            self.samples.append(health_sample(self.host, self.t0))
            if self._stop_event.wait(self.interval):
                return

    def stop(self) -> list[dict]:
        self._stop_event.set()
        self.join()
        self.samples.append(health_sample(self.host, self.t0))
        return self.samples


def summarize_health(samples: list[dict]) -> dict:
    measured = [s["throttled"] for s in samples if s.get("throttled")]
    temps = [t for s in samples for t in (s.get("soc_temp_c"), s.get("thermal_zone0_c")) if t is not None]
    clocks = [s["arm_clock_hz"] for s in samples if s.get("arm_clock_hz")]
    summary = {"samples": len(samples), "peak_temp_c": max(temps) if temps else None,
               "min_arm_clock_hz": min(clocks) if clocks else None,
               "flags_now_seen": sorted({f for m in measured for f in m["now"]}),
               "since_boot_first": measured[0]["since_boot"] if measured else None,
               "since_boot_last": measured[-1]["since_boot"] if measured else None,
               "first_flag_at_s": next((s["t_s"] for s in samples
                                        if s.get("throttled") and s["throttled"]["now"]), None)}
    if not measured:
        summary["new_since_boot"] = None
        summary["throttle_clean"] = None  # vcgencmd unavailable: unmeasured, never "clean"
    else:
        summary["new_since_boot"] = sorted(set(measured[-1]["since_boot"]) - set(measured[0]["since_boot"]))
        summary["throttle_clean"] = not summary["flags_now_seen"] and not summary["new_since_boot"]
    return summary


# ---- sysfs: the Sony body on USB ---------------------------------------------------------------
def sony_usb_devices(host: Host) -> list[dict]:
    devices = []
    base = host.sys_root / "bus/usb/devices"
    try:
        entries = sorted(p for p in base.iterdir() if ":" not in p.name)
    except OSError:
        return devices
    for entry in entries:
        if (host.read(entry / "idVendor") or "").lower() != SONY_VENDOR_ID:
            continue
        busnum, devnum = host.read(entry / "busnum"), host.read(entry / "devnum")
        serial = host.read(entry / "serial")
        port = (f"usb:{int(busnum):03d},{int(devnum):03d}"
                if busnum and devnum and busnum.isdigit() and devnum.isdigit() else None)
        devices.append({"sysfs_name": entry.name, "product": host.read(entry / "product"),
                        "serial": serial, "busnum": busnum, "devnum": devnum, "port": port,
                        "speed_mbps": host.read(entry / "speed"),
                        "body": next((b for b, s in KNOWN_BODY_SERIALS.items() if s == serial), None)})
    return devices


def expected_serial(body: str | None, expect_serial: str | None) -> str | None:
    return expect_serial or KNOWN_BODY_SERIALS.get(body or "")


def identity_of(device: dict | None) -> dict | None:
    """The receipt key: the serial the device itself reports, never the --body claim."""
    if not device:
        return None
    return {"usb_serial": device["serial"], "body_from_serial": device["body"],
            "port": device["port"], "sysfs_name": device["sysfs_name"],
            "source": "sysfs USB device descriptor (serial read back from the body)"}


def identity_error(device: dict | None, body: str | None, expect_serial: str | None,
                   where: str) -> str | None:
    """Why this device cannot be the claimed body, or None. Ports re-enumerate; serials don't."""
    if device is None:
        return f"no Sony USB device at {where}; ports move on re-enumeration, so re-detect"
    if not device["serial"]:
        return f"the Sony device at {where} reports no USB serial, so its identity is unknown"
    wanted = expected_serial(body, expect_serial)
    if body and wanted is None:
        return f"no known USB serial for --body {body}; pass --expect-serial"
    if wanted and device["serial"] != wanted:
        return (f"identity mismatch: --body {body or '?'} expects USB serial {wanted}, but the device at "
                f"{where} reports {device['serial']} ({device['body'] or 'unknown body'})")
    return None


# ---- receipts ----------------------------------------------------------------------------------
def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def reserve_receipt(receipt_dir: Path, test: str) -> Path:
    """Create a NEW timestamped receipt file (never overwrites another run's receipt)."""
    receipt_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    for suffix in [""] + [f"-{n}" for n in range(2, 100)]:
        path = receipt_dir / f"day1-{test}-{stamp}{suffix}.json"
        try:
            os.close(os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0), 0o600))
            return path
        except FileExistsError:
            continue
    raise FileExistsError("too many receipts in the same second")


def planned_receipt(receipt_dir: Path, test: str) -> Path:
    """The path a --run would create now; a dry run only prints it."""
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    return receipt_dir / f"day1-{test}-{stamp}.json"


def write_receipt(path: Path, payload: dict) -> None:
    """Atomic rewrite, so an interrupted soak still leaves a readable receipt. The temporary file
    is NEW and uniquely named (mkstemp: O_CREAT|O_EXCL), so a planted symlink or file is never
    followed or truncated; the rename replaces the directory entry, never a link's target."""
    descriptor, temp = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(descriptor, "w") as stream:
            stream.write(json.dumps(payload, indent=2, default=str) + "\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temp, path)
    except BaseException:
        if os.path.lexists(temp):
            os.unlink(temp)
        raise


def base_payload(test: str, run: bool, host: Host, plan: list[str]) -> dict:
    return {"schema": SCHEMA, "test": test, "mode": "run" if run else "dry-run", "executed": run,
            "started_at": utc_now(), "finished_at": None,
            "board": board_facts(read_model(host.model_path)),
            "hardware_receipt": False, "passed": None, "identity": None, "plan": plan, "errors": []}


def quote(argv: list) -> str:
    return " ".join(f"'{a}'" if (" " in str(a) or not str(a)) else str(a) for a in argv)


# ---- test 1: autodetect ------------------------------------------------------------------------
def run_autodetect(host: Host, args, run: bool) -> dict:
    expected = expected_serial(args.body, args.expect_serial)
    plan = ["gphoto2 --auto-detect",
            f"read {host.sys_root}/bus/usb/devices/*/{{idVendor,serial,busnum,devnum}} for Sony "
            f"({SONY_VENDOR_ID}); identity is the USB serial"
            + (f"; require serial {expected} for --body {args.body}" if expected else ""),
            *[f"pgrep -fa {name}   # must find nothing" for name in AUTOMOUNTERS]]
    payload = base_payload("autodetect", run, host, plan)
    payload.update(body_claimed=args.body, expected_serial=expected)
    if not run:
        return payload
    detect = host.run(["gphoto2", "--auto-detect"], timeout=30)
    rows = parse_autodetect(detect["stdout"])
    sony = sony_usb_devices(host)
    listed_ports = {r["port"] for r in rows}
    automount = []
    for name in AUTOMOUNTERS:
        found = host.run(["pgrep", "-fa", name], timeout=10)
        if found["returncode"] == 0 and found["stdout"].strip():
            automount.append({"name": name, "processes": found["stdout"].strip().splitlines()})
    if args.body or args.expect_serial:
        device = next((d for d in sony if d["serial"] == expected), None) if expected else None
        if expected is None:
            error = f"no known USB serial for --body {args.body}; pass --expect-serial"
        elif device is None:
            reported = [f"{d['serial'] or 'no serial'} on {d['port']}" for d in sony] or ["no Sony device"]
            error = (f"identity mismatch: --body {args.body or '?'} expects USB serial {expected}, but USB "
                     f"reports: {'; '.join(reported)}")
        elif device["port"] not in listed_ports:
            error = f"serial {device['serial']} is on {device['port']} but gphoto2 does not list that port"
        else:
            error = None
        payload["identity"] = identity_of(device)
    else:
        listed = [d for d in sony if d["port"] in listed_ports and d["serial"]]
        error = None if listed else "no Sony device with a USB serial is listed by gphoto2"
        payload["identity"] = [identity_of(d) for d in listed]
    if error:
        payload["errors"].append(error)
    payload.update(gphoto2=detect, gphoto2_rows=rows, sony_sysfs=sony, automounters_running=automount,
                   identity_note="Identity is the USB serial; the gphoto label can name the wrong body.")
    if automount:
        payload["errors"].append("a desktop automounter is running and can take the camera from gphoto2")
    payload["passed"] = bool(detect["returncode"] == 0 and not error and not automount)
    return payload


# ---- test 2: capture-loop (reuses the existing 20-shot proof) ----------------------------------
def proof_verdict(proof: dict | None, shots: int) -> tuple[bool, str | None]:
    """The no-drop half of the proof. The inherited proof reports 0/0 as pass=True; this never
    does. Everything must be equal and at least the kill criterion's 20 shots."""
    if not isinstance(proof, dict):
        return False, "proof.json missing or unreadable"
    counts = [proof.get(k) for k in ("requested_shots", "recorded_shots", "http_200", "verified_readback")]
    if not all(isinstance(c, int) for c in counts):
        return False, "proof.json lacks shot counts"
    # A proof written before RAW evidence existed has no no_drop_pass: its "pass" meant exactly that.
    no_drop = proof["no_drop_pass"] if "no_drop_pass" in proof else proof.get("pass")
    if no_drop is not True:
        return False, "proof.json no-drop pass is not true"
    if len(set(counts)) != 1 or counts[0] != shots:
        return False, f"proof counts {counts} do not all equal the {shots} requested shots"
    if counts[0] < KILL_CRITERION_SHOTS:
        return False, f"{counts[0]} shots is below the {KILL_CRITERION_SHOTS}-shot kill criterion"
    if proof.get("reenumeration_failures"):
        return False, "proof recorded re-enumeration failures"
    return True, None


def raw_verdict(proof: dict | None, shots: int) -> tuple[bool | None, str | None]:
    """The RAW half: the proof's raw_transfer verdict over all ``shots``. None (unverified) only
    when the proof carries no raw_transfer at all; any recorded verdict but "passed" fails."""
    raw = proof.get("raw_transfer") if isinstance(proof, dict) else None
    if not isinstance(raw, dict):
        return None, "proof.json has no raw_transfer verdict: RAW transfer unverified"
    if raw.get("verdict") != "passed" or raw.get("passed") is not True:
        return False, f"RAW transfer verdict {raw.get('verdict')!r}"
    if raw.get("shots_with_raw") != shots:
        return False, f"RAW transfer covers {raw.get('shots_with_raw')} of the {shots} requested shots"
    return True, None


def run_capture_loop(host: Host, args, run: bool, receipt: Path) -> dict:
    proof_dir = receipt.with_name(receipt.stem + "-proof")
    expected = expected_serial(args.body, None)

    def proof_command(serial: str | None) -> list[str]:
        # /shoot refuses a request that names a model and port without a serial, so the proof
        # carries the serial this machine's sysfs reported at --port (checked below).
        return ["bash", str(ROOT / "deploy/pi_bringup.sh"), "--apply", "--phase", "proof",
                "--model", args.model or "<exact gphoto model>", "--port", args.port or "<usb:BBB,DDD>",
                "--serial", serial or "<usb serial>",
                "--token-file", args.token_file or "<token-file>", "--shots", str(args.shots),
                "--host", args.host, "--http-port", str(args.http_port), "--receipt-dir", str(proof_dir)]

    plan = [f"--host must be this machine ({', '.join(LOOPBACK_HOSTS)}): the serial is read from local "
            "sysfs and the throttle log is this machine's, so a remote service is refused",
            f"identity: the Sony device at --port must report USB serial {expected or '<serial for --body>'}",
            f"sample vcgencmd get_throttled / measure_temp / measure_clock arm every {args.interval}s",
            quote(proof_command(expected)),
            "identity again afterwards: the same serial must still be on the bus (its port may move)",
            f"kill criterion: proof pass with requested == recorded == HTTP 200 == verified == "
            f"{args.shots} >= {KILL_CRITERION_SHOTS}, plus a clean throttle log",
            "RAW criterion: the proof's raw_transfer verdict must be 'passed' for every shot: "
            + raw_proof.criterion(args.model or "")]
    payload = base_payload("capture-loop", run, host, plan)
    payload.update(body_claimed=args.body, expected_serial=expected, shots=args.shots,
                   kill_criterion_passed=None,
                   criteria={"identity_usb_serial": None, "twenty_shot_no_drop": None,
                             "throttle_clean": None, "raw_plus_jpeg_download": None},
                   raw_transfer_note=("On the Sony CLI capture path the RAW+JPEG pair is downloaded to "
                                      "the board; the service records each file's size and SHA-256 "
                                      "before it discards the ARW (the product keeps only the JPEG). "
                                      "The hashes prove what crossed USB, not a stored ARW copy."))
    if not run:
        return payload
    missing = [flag for flag, value in (("--model", args.model), ("--port", args.port),
                                        ("--token-file", args.token_file), ("--body", args.body)) if not value]
    if missing:
        payload["errors"].append(f"missing {', '.join(missing)}")
        payload["passed"] = False
        return payload
    if args.host not in LOOPBACK_HOSTS:
        payload["errors"].append(
            f"refusing --host {args.host}: the USB serial and the throttle log are read on THIS "
            "machine, so they would not describe the camera and board a remote service fires. "
            "Run capture-loop on the board itself with --host 127.0.0.1.")
        payload["passed"] = False
        return payload
    device = next((d for d in sony_usb_devices(host) if d["port"] == args.port), None)
    error = identity_error(device, args.body, None, args.port)
    payload["identity"] = identity_of(device)
    if error:
        payload["criteria"]["identity_usb_serial"] = False
        payload["errors"].append(error)
        payload["passed"] = False
        return payload  # never fire the wrong body
    t0 = host.monotonic()
    sampler = Sampler(host, args.interval, t0)
    sampler.start()
    try:
        result = host.run(proof_command(device["serial"]), timeout=args.shots * 180 + 60)
    finally:
        samples = sampler.stop()
    after = next((d for d in sony_usb_devices(host) if d["serial"] == device["serial"]), None)
    payload["identity_after"] = identity_of(after)
    identity_ok = after is not None
    if not identity_ok:
        payload["errors"].append(f"USB serial {device['serial']} is no longer on the bus after the proof")
    proof = None
    try:
        proof = json.loads((proof_dir / "proof.json").read_text())
    except (OSError, ValueError) as exc:
        payload["errors"].append(f"proof.json unreadable: {type(exc).__name__}")
    proof_ok, proof_error = proof_verdict(proof, args.shots)
    if proof_error:
        payload["errors"].append(proof_error)
    raw_ok, raw_error = raw_verdict(proof, args.shots)
    if raw_error:
        payload["errors"].append(raw_error)
    health = summarize_health(samples)
    payload.update(proof_command=result, proof_receipt=str(proof_dir / "proof.json"),
                   proof_summary={k: (proof or {}).get(k) for k in (
                       "pass", "no_drop_pass", "requested_shots", "recorded_shots", "http_200",
                       "verified_readback", "reenumeration_failures", "elapsed_s", "peak_vmhwm_kib")},
                   raw_transfer=(proof or {}).get("raw_transfer"),
                   health_samples=samples, health=health)
    payload["criteria"].update(identity_usb_serial=identity_ok, twenty_shot_no_drop=proof_ok,
                               throttle_clean=health["throttle_clean"], raw_plus_jpeg_download=raw_ok)
    payload["kill_criterion_passed"] = bool(identity_ok and proof_ok and health["throttle_clean"] is True
                                            and raw_ok is True)
    failed = [k for k, v in payload["criteria"].items() if v is False]
    unverified = [k for k, v in payload["criteria"].items() if v is None]
    payload["passed"] = False if failed else None if unverified else True
    payload["verdict_reason"] = (f"failed: {', '.join(failed)}" if failed else
                                 f"unverified: {', '.join(unverified)}" if unverified else
                                 "kill criterion passed, including RAW+JPEG download")
    return payload


# ---- tests 3 and 3b: usb-replug-idle, sleep-wake -----------------------------------------------
def _find_by_serial(host: Host, serial: str | None) -> dict | None:
    return next((d for d in sony_usb_devices(host) if serial and d["serial"] == serial), None)


def _wait_for(host: Host, serial: str, timeout_s: float) -> dict | None:
    deadline = host.monotonic() + timeout_s
    while True:
        device = _find_by_serial(host, serial)
        if device or host.monotonic() >= deadline:
            return device
        host.sleep(1.0)


def _probe(host: Host, serial: str) -> dict:
    """Read-only: re-detect the port (ports move on re-enumeration), then ``gphoto2 --summary``."""
    device = _find_by_serial(host, serial)
    if not device:
        return {"present": False, "responsive": False}
    rows = parse_autodetect(host.run(["gphoto2", "--auto-detect"], timeout=30)["stdout"])
    row = next((r for r in rows if r["port"] == device["port"]), None)
    if not row:
        return {"present": True, "port": device["port"], "listed_by_gphoto2": False, "responsive": False}
    summary = host.run(["gphoto2", "--camera", row["model"], "--port", row["port"], "--summary"], timeout=60)
    return {"present": True, "port": row["port"], "model": row["model"], "listed_by_gphoto2": True,
            "responsive": summary["returncode"] == 0, "summary_returncode": summary["returncode"]}


def _camera_preflight(host: Host, args, payload: dict) -> dict | None:
    """Service stopped, exactly one Sony body with a serial that agrees with any --body claim."""
    if host.run(["pgrep", "-f", SERVICE_PATTERN], timeout=10)["returncode"] == 0:
        payload["errors"].append("camera-autopilot service is running; stop it first (single camera owner)")
        return None
    devices = sony_usb_devices(host)
    if len(devices) != 1:
        payload["errors"].append(f"need exactly one Sony device on USB; found {len(devices)}")
        return None
    device = devices[0]
    error = identity_error(device, args.body, args.expect_serial, device["port"] or device["sysfs_name"])
    payload["identity"] = identity_of(device)
    if error:
        payload["errors"].append(error)
        return None
    return device


def _climb_ladder(host: Host, args, serial: str, probe: dict, step, disturbance_rung: str) -> str:
    """After a disturbance: the first rung that makes the body answer again."""
    if probe["responsive"]:
        return disturbance_rung
    device = _find_by_serial(host, serial)
    if device:
        name = device["sysfs_name"]
        driver = host.sys_root / "bus/usb/drivers/usb"
        if host.euid() == 0:
            host.write(driver / "unbind", name)
            host.sleep(2.0)
            host.write(driver / "bind", name)
            performed_by = "script"
        else:
            host.prompt("Not root, so run these yourself in another shell, then press Enter:\n"
                        f"  echo {name} | sudo tee {driver}/unbind\n  sleep 2\n"
                        f"  echo {name} | sudo tee {driver}/bind\n")
            performed_by = "operator"
        _wait_for(host, serial, args.settle)
        probe = _probe(host, serial)
        step("probe_after_unbind_bind", performed_by=performed_by, sysfs_name=name, **probe)
        if probe["responsive"]:
            return "unbind_bind"
    host.prompt("Still unresponsive. Switch the camera OFF and ON again, then press Enter. ")
    _wait_for(host, serial, args.settle)
    probe = _probe(host, serial)
    step("probe_after_power_cycle", **probe)
    return "physical_power_cycle" if probe["responsive"] else "not_recovered"


LADDER_PLAN = [
    "rung 2 (if unresponsive): echo <dev> > /sys/bus/usb/drivers/usb/unbind; sleep 2; "
    "echo <dev> > /sys/bus/usb/drivers/usb/bind (root; else printed for the operator)",
    "rung 3 (if still unresponsive): operator power-cycles the body, probe again",
    "record which rung recovered it: physical_power_cycle_needed",
]


def run_usb_replug_idle(host: Host, args, run: bool) -> dict:
    plan = ["refuse if the camera-autopilot service is running (one camera owner at a time)",
            "exactly one Sony device; its USB serial must match --body/--expect-serial when given",
            "baseline gphoto2 --auto-detect + --summary (read-only)",
            "operator unplugs the camera's USB cable ~5 s with the body IDLE, plugs it back, presses Enter",
            "rung 1: wait for re-enumeration (devnum must change), probe again", *LADDER_PLAN]
    payload = base_payload("usb-replug-idle", run, host, plan)
    payload.update(recovered_by=None, physical_power_cycle_needed=None, steps=[],
                   scope="service stopped, body idle; NOT a mid-session (during capture) interruption")
    if not run:
        return payload
    t0 = host.monotonic()

    def step(name: str, **fields):
        payload["steps"].append({"step": name, "t_s": round(host.monotonic() - t0, 1), **fields})

    before = _camera_preflight(host, args, payload)
    if before is None:
        payload["passed"] = False
        return payload
    serial = before["serial"]
    baseline = _probe(host, serial)
    step("baseline", **baseline)
    if not baseline["responsive"]:
        payload["errors"].append("body unresponsive before the interruption; nothing to measure")
        payload["passed"] = False
        return payload
    host.prompt("Interrupt USB power now: unplug the camera's USB cable for about 5 seconds, plug it "
                "back in, then press Enter. ")
    after = _wait_for(host, serial, args.settle)
    interrupted = bool(after and after["devnum"] != before["devnum"])
    step("replug", reappeared=after is not None, reenumerated=interrupted,
         devnum_before=before["devnum"], devnum_after=(after or {}).get("devnum"))
    payload["interruption_observed"] = interrupted
    probe = _probe(host, serial)
    step("probe_after_replug", **probe)
    recovered_by = _climb_ladder(host, args, serial, probe, step, "replug_reenumeration")
    payload["identity_after"] = identity_of(_find_by_serial(host, serial))
    payload["recovered_by"] = recovered_by
    payload["physical_power_cycle_needed"] = recovered_by in ("physical_power_cycle", "not_recovered")
    payload["passed"] = recovered_by in ("replug_reenumeration", "unbind_bind") and interrupted
    if not interrupted:
        payload["errors"].append("no re-enumeration observed (devnum unchanged): the interruption did not happen")
    return payload


def run_sleep_wake(host: Host, args, run: bool) -> dict:
    plan = ["refuse if the camera-autopilot service is running (one camera owner at a time)",
            "exactly one Sony device; its USB serial must match --body/--expect-serial when given",
            "baseline gphoto2 --auto-detect + --summary (read-only)",
            "operator leaves the body untouched until it auto-powers-off (Power Save Start Time), "
            "presses Enter",
            "while asleep, PASSIVE only: sysfs presence and the gphoto2 --auto-detect label "
            "(believed not to wake the body; unverified). No --summary.",
            "operator wakes the body (half-press the shutter), presses Enter",
            "rung 1: probe again after the wake", *LADDER_PLAN]
    payload = base_payload("sleep-wake", run, host, plan)
    payload.update(recovered_by=None, physical_power_cycle_needed=None, steps=[])
    if not run:
        return payload
    t0 = host.monotonic()

    def step(name: str, **fields):
        payload["steps"].append({"step": name, "t_s": round(host.monotonic() - t0, 1), **fields})

    before = _camera_preflight(host, args, payload)
    if before is None:
        payload["passed"] = False
        return payload
    serial = before["serial"]
    baseline = _probe(host, serial)
    step("baseline", **baseline)
    if not baseline["responsive"]:
        payload["errors"].append("body unresponsive before sleep; nothing to measure")
        payload["passed"] = False
        return payload
    host.prompt("Leave the camera untouched until it powers itself off (set Power Save Start Time to "
                "the shortest value to speed this up). Press Enter once its screen is off. ")
    asleep = _find_by_serial(host, serial)
    rows = parse_autodetect(host.run(["gphoto2", "--auto-detect"], timeout=30)["stdout"])
    step("asleep", present_on_usb=asleep is not None, port=(asleep or {}).get("port"),
         gphoto2_label=next((r["model"] for r in rows if asleep and r["port"] == asleep["port"]), None))
    host.prompt("Wake the camera (half-press the shutter button), then press Enter. ")
    _wait_for(host, serial, args.settle)
    probe = _probe(host, serial)
    step("probe_after_wake", **probe)
    recovered_by = _climb_ladder(host, args, serial, probe, step, "wake_only")
    payload["identity_after"] = identity_of(_find_by_serial(host, serial))
    payload["recovered_by"] = recovered_by
    payload["physical_power_cycle_needed"] = recovered_by in ("physical_power_cycle", "not_recovered")
    payload["passed"] = recovered_by in ("wake_only", "unbind_bind")
    return payload


# ---- test 4: ap-soak ---------------------------------------------------------------------------
def run_ap_soak(host: Host, args, run: bool, receipt: Path) -> dict:
    interface, connection = args.interface, args.connection
    plan = [f"nmcli -t -f NAME connection show   # '{connection}' must exist (deploy/pi_bringup.sh board-config)",
            f"nmcli connection up id {connection}   # DROPS the client Wi-Fi and this SSH session",
            f"every {args.interval}s for {args.hours} h: iw dev {interface} info; iw dev {interface} station dump; "
            "vcgencmd get_throttled / measure_temp",
            "journalctl -k --since @<start> --no-pager   # brcmfmac errors",
            f"nmcli connection down id {connection}   # unless --leave-up; client Wi-Fi autoconnects back",
            "run it detached so it survives the SSH drop:",
            f"  nohup {quote([sys.executable, str(Path(__file__).resolve()), 'ap-soak', '--run', '--receipt-dir', str(args.receipt_dir)])} "
            f"> {args.receipt_dir}/ap-soak.log 2>&1 &"]
    payload = base_payload("ap-soak", run, host, plan)
    payload.update(interface=interface, connection=connection, hours=args.hours, samples=[], events=[])
    if not run:
        return payload
    names = host.run(["nmcli", "-t", "-f", "NAME", "connection", "show"], timeout=30)
    if connection not in names["stdout"].splitlines():
        payload["errors"].append(f"NetworkManager connection '{connection}' not found; install the staged profile")
        payload["passed"] = False
        return payload
    start_epoch = int(time.time())
    up = host.run(["nmcli", "connection", "up", "id", connection], timeout=90)
    payload["ap_up"] = up
    if up["returncode"] != 0:
        payload["errors"].append("nmcli connection up failed (permissions? run the soak as a user NetworkManager allows)")
        payload["passed"] = False
        return payload
    t0 = host.monotonic()
    deadline = t0 + args.hours * 3600
    join_deadline = t0 + args.join_timeout * 60
    previous: dict[str, dict] = {}
    first_join = None
    empty_after_join = 0
    non_ap = 0
    while True:
        info = parse_iw_info(host.run(["iw", "dev", interface, "info"], timeout=15)["stdout"])
        stations = parse_station_dump(host.run(["iw", "dev", interface, "station", "dump"], timeout=15)["stdout"])
        health = health_sample(host, t0)
        now_s = health["t_s"]
        for event in station_events(previous, stations):
            payload["events"].append({"t_s": now_s, **event})
        if stations and first_join is None:
            first_join = now_s
        if first_join is not None and not stations:
            empty_after_join += 1
        if info["type"] != "AP":
            non_ap += 1
        payload["samples"].append({"t_s": now_s, "type": info["type"], "channel": info["channel"],
                                   "stations": stations, "throttled": (health["throttled"] or {}).get("raw"),
                                   "soc_temp_c": health["soc_temp_c"]})
        previous = stations
        payload["first_join_s"] = first_join
        write_receipt(receipt, payload)  # partial evidence survives a crash or power loss
        if host.monotonic() >= deadline or (first_join is None and host.monotonic() >= join_deadline):
            break
        host.sleep(args.interval)
    journal = host.run(["journalctl", "-k", "--no-pager", "--since", f"@{start_epoch}"], timeout=120)
    log_read = journal["returncode"] == 0
    payload["kernel_log"] = {"read_ok": log_read, "returncode": journal["returncode"],
                             "stderr": (journal.get("stderr") or "")[-500:]}
    errors = brcmf_error_lines(journal["stdout"]) if log_read else []
    if not args.leave_up:
        payload["ap_down"] = host.run(["nmcli", "connection", "down", "id", connection], timeout=60)
    disconnects = [e for e in payload["events"] if e["event"] in ("leave", "reassociate")]
    completed = host.monotonic() - t0 >= args.hours * 3600
    payload.update(brcmf_error_lines=errors[-50:], brcmf_error_count=len(errors) if log_read else None,
                   disconnect_count=len(disconnects), samples_without_station_after_join=empty_after_join,
                   samples_not_in_ap_mode=non_ap, completed_full_duration=completed,
                   soak_s=round(host.monotonic() - t0, 1))
    if first_join is None:
        payload["errors"].append(f"no phone joined within {args.join_timeout} min")
    payload["passed"] = bool(first_join is not None and completed and not disconnects
                             and not empty_after_join and not non_ap and not errors)
    if payload["passed"] and not log_read:
        # Everything else held, but without the kernel log the brcmfmac check never happened.
        payload["passed"] = None
        payload["errors"].append(f"kernel log unreadable (journalctl exit {journal['returncode']}); "
                                 "brcmfmac errors unchecked, so the soak is inconclusive "
                                 "(run it as a user in the systemd-journal or adm group)")
    return payload


# ---- test 5: merge-thermal ---------------------------------------------------------------------
def run_merge_thermal(host: Host, args, run: bool, receipt: Path) -> dict:
    bench = ROOT / "tools/puck_workload_bench.py"
    frames = [str(Path(p).expanduser()) for p in args.jpegs] or ["<frame0.jpg>", "<frame1.jpg>", "<frame2.jpg>"]

    def command(n: int) -> list[str]:
        return [args.python, str(bench), *frames, "--mode", args.mode, "--repeats", "3",
                "--output", str(receipt.with_name(f"{receipt.stem}-bench-{n:03d}.json"))]

    plan = [f"sample vcgencmd + thermal_zone0 every {args.interval}s",
            f"repeat for {args.minutes} min: {quote(command(1))}",
            "pass only if every iteration completes, the inputs are >= 20 MP, and no throttle flag appears"]
    payload = base_payload("merge-thermal", run, host, plan)
    payload.update(enclosure=args.enclosure, minutes=args.minutes, merge_mode=args.mode, iterations=[])
    if not run:
        return payload
    if len(args.jpegs) < 2 or not args.enclosure:
        payload["errors"].append("--run needs at least two existing 24 MP JPEGs and --enclosure")
        payload["passed"] = False
        return payload
    t0 = host.monotonic()
    sampler = Sampler(host, args.interval, t0)
    sampler.start()
    megapixels = None
    try:
        n = 0
        while host.monotonic() - t0 < args.minutes * 60:
            n += 1
            result = host.run(command(n), timeout=900)
            row = {"n": n, "returncode": result["returncode"], "wall_s": result["wall_s"]}
            try:
                report = json.loads(Path(command(n)[-1]).read_text())
                row["statuses"] = [r.get("status") for r in report.get("runs", [])]
                row["merge_wall_s"] = [r.get("merge_wall_s") for r in report.get("runs", [])]
                first = (report.get("inputs") or [{}])[0]
                if megapixels is None and first.get("width") and first.get("height"):
                    megapixels = round(first["width"] * first["height"] / 1e6, 2)
            except (OSError, ValueError):
                row["statuses"] = None
            payload["iterations"].append(row)
            if result["returncode"] != 0:
                break
    finally:
        samples = sampler.stop()
    health = summarize_health(samples)
    ok_iterations = [r for r in payload["iterations"] if r["returncode"] == 0
                     and r.get("statuses") and all(s == "completed" for s in r["statuses"])]
    payload.update(input_megapixels=megapixels, health_samples=samples, health=health,
                   completed_iterations=len(ok_iterations))
    payload["passed"] = bool(payload["iterations"] and len(ok_iterations) == len(payload["iterations"])
                             and megapixels is not None and megapixels >= 20
                             and health["throttle_clean"] is True)
    return payload


# ---- CLI ---------------------------------------------------------------------------------------
def kill_criterion_shots(text: str) -> int:
    value = int(text)
    if value < KILL_CRITERION_SHOTS:
        raise argparse.ArgumentTypeError(f"the kill criterion needs at least {KILL_CRITERION_SHOTS} shots")
    return value


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0],
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="test", required=True)

    def add(name: str, help_text: str) -> argparse.ArgumentParser:
        p = sub.add_parser(name, help=help_text)
        p.add_argument("--receipt-dir", type=path_arg, required=True,
                       help="directory for the timestamped day1-<test>-<UTC>.json receipt")
        p.add_argument("--run", action="store_true", help="execute on the board (default: dry run)")
        p.add_argument("--allow-system-dir", action="store_true",
                       help="with --run only: permit a --receipt-dir inside /etc, /boot, /usr, ...")
        return p

    def identity_args(p: argparse.ArgumentParser) -> None:
        p.add_argument("--body", choices=sorted(KNOWN_BODY_SERIALS), help="claimed body; checked against the USB serial")
        p.add_argument("--expect-serial", help="USB serial to require (default: the known serial for --body)")

    p = add("autodetect", "1. gphoto2 --auto-detect, identity by USB serial, automounter check")
    identity_args(p)

    p = add("capture-loop", "2. the existing 20-shot proof with throttle/temperature sampling")
    p.add_argument("--body", choices=sorted(KNOWN_BODY_SERIALS))
    p.add_argument("--model", help="exact gphoto2 model string (re-detect; ports move)")
    p.add_argument("--port", help="exact usb:BBB,DDD port")
    p.add_argument("--token-file", type=path_text, help="API token file for the running service")
    p.add_argument("--shots", type=kill_criterion_shots, default=KILL_CRITERION_SHOTS,
                   help=f"at least {KILL_CRITERION_SHOTS} (the kill criterion)")
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--http-port", type=int, default=8000)
    p.add_argument("--interval", type=float, default=2.0)

    p = add("usb-replug-idle", "3. USB replug with the service stopped and the body idle, then the recovery ladder")
    identity_args(p)
    p.add_argument("--settle", type=float, default=20.0, help="seconds to wait for re-enumeration")

    p = add("sleep-wake", "3b. body auto-power-off, wake, then the recovery ladder")
    identity_args(p)
    p.add_argument("--settle", type=float, default=20.0, help="seconds to wait for re-enumeration")

    p = add("ap-soak", "4. onboard-Wi-Fi access point soak with a phone connected")
    p.add_argument("--connection", default="camera-puck")
    p.add_argument("--interface", default="wlan0")
    p.add_argument("--hours", type=float, default=8.0)
    p.add_argument("--interval", type=float, default=30.0)
    p.add_argument("--join-timeout", type=float, default=10.0, help="minutes for the phone to join")
    p.add_argument("--leave-up", action="store_true", help="keep the AP up afterwards")

    p = add("merge-thermal", "5. sustained 24 MP merges with temperature logging")
    p.add_argument("jpegs", nargs="*", type=path_text, help="2..16 existing same-size JPEGs from a real bracket")
    p.add_argument("--enclosure", choices=["open-air", "closed-box"])
    p.add_argument("--minutes", type=float, default=30.0)
    p.add_argument("--interval", type=float, default=5.0)
    p.add_argument("--mode", choices=["hdr", "night", "crowd"], default="hdr")
    p.add_argument("--python", type=path_text, default=sys.executable)
    return parser


def main(argv=None, host: Host | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.allow_system_dir and not args.run:
        parser.error("--allow-system-dir needs --run")
    args.receipt_dir = resolve_output(args.receipt_dir)  # resolved once: checked and written as-is
    reason = refusal(args.receipt_dir, ROOT, args.allow_system_dir)
    if reason:
        print(f"day-one {args.test}: {reason}", file=sys.stderr)
        return 2
    host = host or Host()
    # A dry run creates nothing: not the receipt dir, not a receipt.
    receipt = reserve_receipt(args.receipt_dir, args.test) if args.run else planned_receipt(args.receipt_dir, args.test)
    runners = {"autodetect": lambda: run_autodetect(host, args, args.run),
               "capture-loop": lambda: run_capture_loop(host, args, args.run, receipt),
               "usb-replug-idle": lambda: run_usb_replug_idle(host, args, args.run),
               "sleep-wake": lambda: run_sleep_wake(host, args, args.run),
               "ap-soak": lambda: run_ap_soak(host, args, args.run, receipt),
               "merge-thermal": lambda: run_merge_thermal(host, args, args.run, receipt)}
    payload = runners[args.test]()
    payload["finished_at"] = utc_now()
    payload["hardware_receipt"] = bool(args.run and payload["board"]["board"] in ("cm5", "pi5"))
    print(f"day-one {args.test} [{payload['mode']}]")
    for line in payload["plan"]:
        print(f"  {'RAN' if args.run else 'WOULD-RUN'}: {line}")
    for error in payload["errors"]:
        print(f"  ERROR: {error}")
    if not args.run:
        print(f"  WOULD-WRITE: {receipt}")
        print(f"  RECEIPT {json.dumps(payload, sort_keys=True, default=str)}")
        return 0
    write_receipt(receipt, payload)
    verdict = {True: "PASS", False: "FAIL", None: "INCONCLUSIVE"}[payload["passed"]]
    print(f"  verdict: {verdict}\n  receipt: {receipt}")
    return {True: 0, False: 1, None: EXIT_INCONCLUSIVE}[payload["passed"]]


if __name__ == "__main__":
    raise SystemExit(main())
