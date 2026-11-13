"""Camera-free tests for the CM5 / CM5-NANO-A bring-up kit (lane 0F).

Nothing here touches a camera, the network, sysfs or NetworkManager. Dry runs are checked with a
PATH full of sentinel binaries that record any call; the ``--run`` logic of the day-one tests is
exercised against a fake host (scripted command output, a temporary sysfs tree, a virtual clock).
"""
from __future__ import annotations

import configparser
import errno
import hashlib
import json
import os
import shlex
import shutil
import stat
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from deploy import cm5_nano, day_one, safe_paths
from deploy import host_readiness

REPO = Path(__file__).resolve().parents[1]
BRINGUP = REPO / "deploy" / "pi_bringup.sh"
CM5_MODEL = "Raspberry Pi Compute Module 5 Rev 1.0"
# Anything that changes the host or talks to a camera / radio. host-facts may still run the
# read-only `vcgencmd get_throttled` / `get_config` queries it always ran.
FORBIDDEN_SIDE_EFFECTS = ("apt-get", "systemctl", "sudo", "udevadm", "nmcli", "gphoto2", "iw")
# A day-one dry run runs nothing at all.
FORBIDDEN_ALL = FORBIDDEN_SIDE_EFFECTS + ("vcgencmd", "journalctl", "pgrep")


def _sentinel_path(tmp_path: Path, forbidden=FORBIDDEN_SIDE_EFFECTS) -> tuple[dict, Path]:
    fake_bin = tmp_path / "fakebin"
    fake_bin.mkdir(parents=True)
    sentinel = tmp_path / "forbidden-calls.log"
    for name in forbidden:
        script = fake_bin / name
        script.write_text(f'#!/usr/bin/env bash\necho "{name} $*" >> "{sentinel}"\nexit 99\n')
        script.chmod(0o755)
    env = dict(os.environ)
    env["PATH"] = f"{fake_bin}{os.pathsep}{env['PATH']}"
    return env, sentinel


def _printed_receipts(stdout: str) -> dict[str, dict]:
    receipts = {}
    for line in stdout.splitlines():
        if line.startswith("  RECEIPT "):
            _, phase, payload = line.strip().split(" ", 2)
            receipts[phase] = json.loads(payload)
    return receipts


def _all_files(root: Path) -> list[Path]:
    return sorted(q for q in root.rglob("*") if q.is_file()) if root.exists() else []


def _model_file(tmp_path: Path, text: str) -> Path:
    path = tmp_path / "model"
    path.write_bytes(text.encode() + b"\x00")  # the device tree NUL-terminates it
    return path


# ---- board classification -----------------------------------------------------------------------
@pytest.mark.parametrize("model,board", [
    (CM5_MODEL, "cm5"),
    ("Raspberry Pi Compute Module 5 Lite Rev 1.0", "cm5"),
    ("Raspberry Pi 5 Model B Rev 1.0", "pi5"),
    ("Raspberry Pi 500 Rev 1.0", "raspberry-pi-other"),
    ("Raspberry Pi Compute Module 4 Rev 1.1", "raspberry-pi-other"),
    (None, "unknown"),
    ("", "unknown"),
])
def test_classify_model(model, board):
    assert cm5_nano.classify_model(model) == board


def test_cm5_assumes_the_nano_a_carrier_and_says_it_is_an_assumption():
    facts = cm5_nano.board_facts(CM5_MODEL)
    assert facts["board"] == "cm5" and facts["carrier"] == "cm5-nano-a"
    assert facts["carrier_detected"] is False and "assumption" in facts["carrier_basis"]
    assert facts["carrier_facts"]["usb_host_ports"] == 1
    assert facts["camera_usb_port"] == "the single USB-A port"


def test_pi5_is_the_named_fallback_without_a_carrier():
    facts = cm5_nano.board_facts("Raspberry Pi 5 Model B Rev 1.0")
    assert facts["board"] == "pi5" and facts["carrier"] == "none"
    assert "fallback" in facts["carrier_basis"]


def test_read_model_strips_nul_and_tolerates_absence(tmp_path):
    assert cm5_nano.read_model(_model_file(tmp_path, CM5_MODEL)) == CM5_MODEL
    assert cm5_nano.read_model(tmp_path / "missing") is None


# ---- config.txt fragment ------------------------------------------------------------------------
def test_config_fragment_is_cm5_scoped_and_every_setting_is_sourced():
    text = cm5_nano.render_config_fragment()
    lines = text.splitlines()
    settings = [i for i, line in enumerate(lines) if line and not line.startswith("#")]
    assert lines[settings[0]] == "[cm5]" and lines[settings[-1]] == "[all]"
    for index in settings:
        if lines[index].startswith("["):
            continue
        block = []
        cursor = index - 1
        while cursor >= 0 and (lines[cursor].startswith("#") or lines[cursor].startswith("dtparam=")):
            block.append(lines[cursor])
            cursor -= 1
        assert any("Source:" in line for line in block), f"unsourced setting: {lines[index]}"


def test_config_fragment_has_i2c_and_the_waveshare_fan_curve_verbatim():
    text = cm5_nano.render_config_fragment()
    assert "\ndtparam=i2c_arm=on\n" in text
    for line in ("dtparam=fan_temp0=36000,fan_temp0_hyst=2000,fan_temp0_speed=90",
                 "dtparam=fan_temp1=40000,fan_temp1_hyst=3000,fan_temp1_speed=150",
                 "dtparam=fan_temp2=52000,fan_temp2_hyst=4000,fan_temp2_speed=200",
                 "dtparam=fan_temp3=58000,fan_temp3_hyst=5000,fan_temp3_speed=255"):
        assert f"\n{line}\n" in text
    setting_lines = [l for l in text.splitlines() if l and not l.startswith("#")]
    # Unsourced parameters stay out: no USB current override, no dwc2, no antenna change.
    assert not any(l.startswith(("usb_max_current_enable", "dtoverlay=dwc2", "dtparam=ant")) for l in setting_lines)


# ---- onboard-Wi-Fi hotspot profile --------------------------------------------------------------
def _profile(channel=6):
    return cm5_nano.render_hotspot_profile("ab" * 16, "00000000-0000-4000-8000-000000000000", channel=channel)


def test_hotspot_profile_avoids_each_brcmfmac_pitfall():
    parser = configparser.ConfigParser(interpolation=None)
    parser.read_string(_profile())
    assert parser["connection"]["interface-name"] == "wlan0"   # no virtual AP interface
    assert parser["connection"]["autoconnect"] == "false"      # no AP+STA / reload cycling
    assert parser["wifi"]["mode"] == "ap"
    assert parser["wifi"]["band"] == "bg"                      # 2.4 GHz: no DFS
    assert parser["wifi"]["channel"] in {"1", "6", "11"}
    assert parser["wifi-security"]["key-mgmt"] == "wpa-psk"    # PSK, no SAE
    assert parser["ipv4"]["method"] == "shared"
    assert parser["ipv4"]["address1"] == "192.168.77.1/24"    # matches portable_bundle's Caddyfile
    lowered = _profile().lower()
    for pitfall in ("dfs", "wpa3", "sae", "virtual-ap", "ht avail"):
        assert pitfall in lowered, f"profile does not explain the {pitfall} choice"
    settings = [l for l in lowered.splitlines() if l and not l.startswith("#")]
    assert not any("sae" in l or "uap0" in l for l in settings)


def test_hotspot_profile_is_rsn_only_without_forced_ciphers():
    # Defect 2: key-mgmt=wpa-psk alone admits legacy WPA. proto=rsn excludes it; pairwise/group stay
    # at defaults because the cited forum thread saw forced ciphers break clients.
    parser = configparser.ConfigParser(interpolation=None)
    parser.read_string(_profile())
    security = parser["wifi-security"]
    assert security["proto"] == "rsn"
    assert "pairwise" not in security and "group" not in security
    text = _profile()
    assert "t=357998" in text and "UNTESTED" in text


@pytest.mark.parametrize("channel", [36, 52, 13, 0])
def test_hotspot_profile_refuses_5ghz_and_off_plan_channels(channel):
    with pytest.raises(ValueError):
        _profile(channel)


def test_render_dry_run_never_prints_the_passphrase(capsys):
    assert cm5_nano.main(["render"]) == 0
    out = capsys.readouterr().out
    assert "psk=" not in out and '"credentials_printed": false' in out


def test_render_out_is_private_and_never_rotates(tmp_path):
    target = tmp_path / "staged"
    report = cm5_nano.write_files(target, cm5_nano.render_files())
    assert report["installed"] is False
    assert stat.S_IMODE(target.stat().st_mode) == 0o700
    assert all(stat.S_IMODE(p.stat().st_mode) == 0o600 for p in target.iterdir())
    with pytest.raises(FileExistsError):
        cm5_nano.write_files(target, cm5_nano.render_files())


def test_render_out_without_apply_writes_nothing(tmp_path, capsys):
    target = tmp_path / "staged"
    assert cm5_nano.main(["render", "--out", str(target)]) == 0
    assert not target.exists()
    assert '"written": false' in capsys.readouterr().out


def test_render_refuses_to_put_the_passphrase_in_the_repo(tmp_path, capsys):
    inside = REPO / "artifacts" / "never-created-by-tests"
    assert cm5_nano.main(["render", "--apply", "--out", str(inside)]) == 2
    assert not inside.exists()
    with pytest.raises(ValueError):
        cm5_nano.write_files(inside, cm5_nano.render_files())
    assert not inside.exists()


# ---- output-directory guard ------------------------------------------------------------------------
def test_safe_paths_refuses_repo_system_and_root(tmp_path):
    assert safe_paths.refusal(tmp_path / "receipts", REPO) is None
    assert "inside the repository" in safe_paths.refusal(REPO / "receipts", REPO)
    assert "inside the repository" in safe_paths.refusal(REPO, REPO)
    assert "system directory" in safe_paths.refusal("/etc/camera-autopilot", REPO)
    assert safe_paths.refusal("/etc/camera-autopilot", REPO, allow_system=True) is None
    # A system root and / are refused even when system dirs are allowed (the kit chmods dirs).
    assert safe_paths.refusal("/etc", REPO, allow_system=True)
    assert safe_paths.refusal("/", REPO, allow_system=True)
    # A sibling whose name merely starts with the repo's is not inside it.
    assert safe_paths.refusal(REPO.parent / (REPO.name + "-receipts"), REPO) is None


def test_safe_paths_follows_symlinks_into_the_repo(tmp_path):
    link = tmp_path / "looks-outside"
    link.symlink_to(REPO / "artifacts")
    assert "inside the repository" in safe_paths.refusal(link / "receipts", REPO)


@pytest.mark.parametrize("target_name", ["receipts\n", "rec\teipts"])
def test_safe_paths_refuses_a_symlink_whose_target_has_a_control_character(tmp_path, capsys, target_name):
    # The path passed is clean; only its resolved target carries the control character. The shell's
    # command substitution would strip a trailing newline and pi_bringup.sh would write "receipts".
    out = tmp_path / "out"
    out.mkdir()
    link = out / "link"
    link.symlink_to(out / target_name)
    assert "control character" in safe_paths.refusal(link, REPO)
    assert safe_paths.main(["--repo", str(REPO), "--print-resolved", str(link)]) == 2
    assert capsys.readouterr().out == ""  # no resolved path for the shell to write to
    result, _, _ = _bringup(tmp_path, "--phase", "host-facts", mode="--apply", receipts=link)
    assert result.returncode == 2 and "control character" in result.stderr
    assert [p.name for p in out.iterdir()] == ["link"], "something was written beside the link"


# ---- pi_bringup.sh CM5 awareness ----------------------------------------------------------------
def _bringup(tmp_path, *extra, model=CM5_MODEL, mode="--dry-run", receipts=None):
    env, sentinel = _sentinel_path(tmp_path)
    env["CAMERA_AUTOPILOT_MODEL_FILE"] = str(_model_file(tmp_path, model))
    receipts = receipts or tmp_path / "receipts"
    result = subprocess.run(["bash", str(BRINGUP), mode, "--receipt-dir", str(receipts), *extra],
                            capture_output=True, text=True, env=env)
    return result, sentinel, receipts


def test_bringup_dry_run_on_a_cm5_records_the_carrier_and_writes_nothing(tmp_path):
    result, sentinel, receipts = _bringup(tmp_path)
    assert result.returncode == 0, result.stderr
    assert not sentinel.exists(), sentinel.read_text()
    assert not receipts.exists(), "a dry run created the receipt dir"
    printed = _printed_receipts(result.stdout)
    facts = printed["host-facts"]
    assert facts["board"]["board"] == "cm5" and facts["board"]["carrier"] == "cm5-nano-a"
    assert facts["is_raspberry_pi"] is True
    assert any("single USB-A port" in w for w in facts["warnings"])
    board = printed["board-config"]
    assert board["applies_to_board"] is True and board["created_now"] is False
    # The fragment is shown; no passphrase is generated, printed or stored.
    assert "dtparam=i2c_arm=on" in result.stdout and "psk=" not in result.stdout
    assert f"WOULD-WRITE: {receipts / 'board-config'}" in result.stdout


def test_bringup_apply_stages_board_config_privately_and_keeps_it(tmp_path):
    result, sentinel, receipts = _bringup(tmp_path, "--phase", "board-config", mode="--apply")
    assert result.returncode == 0, result.stderr
    assert not sentinel.exists()
    staged = receipts / "board-config"
    profile = (staged / "camera-puck.nmconnection").read_text()
    psk = next(l for l in profile.splitlines() if l.startswith("psk="))
    assert psk not in result.stdout
    assert stat.S_IMODE((staged / "camera-puck.nmconnection").stat().st_mode) == 0o600
    assert stat.S_IMODE(receipts.stat().st_mode) == 0o700
    assert json.loads((receipts / "board-config.json").read_text())["created_now"] is True
    # Re-running keeps the staged passphrase (idempotent, no silent rotation).
    rerun, _, _ = _bringup(tmp_path / "again", "--phase", "board-config", mode="--apply", receipts=receipts)
    assert rerun.returncode == 0, rerun.stderr
    assert (staged / "camera-puck.nmconnection").read_text() == profile
    assert json.loads((receipts / "board-config.json").read_text())["created_now"] is False


@pytest.mark.parametrize("mode", ["--dry-run", "--apply"])
def test_bringup_refuses_a_receipt_dir_inside_the_repo(tmp_path, mode):
    inside = REPO / "receipts-never-created-by-tests"
    result, _, _ = _bringup(tmp_path, "--phase", "board-config", mode=mode, receipts=inside)
    assert result.returncode == 2 and "inside the repository" in result.stderr
    assert not inside.exists()


def test_bringup_refuses_system_dirs_unless_named_with_apply(tmp_path):
    result, _, _ = _bringup(tmp_path, "--phase", "host-facts", mode="--apply",
                            receipts=Path("/etc/camera-autopilot-never-created"))
    assert result.returncode == 2 and "system directory" in result.stderr
    dry, _, _ = _bringup(tmp_path / "b", "--allow-system-dir", receipts=Path("/etc/x-never-created"))
    assert dry.returncode == 2 and "--allow-system-dir needs --apply" in dry.stderr
    assert not Path("/etc/camera-autopilot-never-created").exists()


@pytest.mark.skipif(os.geteuid() == 0, reason="root ignores directory permissions")
def test_bringup_board_config_render_failure_is_a_failure(tmp_path):
    receipts = tmp_path / "receipts"
    receipts.mkdir(mode=0o500)  # the renderer cannot create board-config/ here
    try:
        result, _, _ = _bringup(tmp_path, "--phase", "board-config", mode="--apply", receipts=receipts)
    finally:
        receipts.chmod(0o700)
    assert result.returncode != 0
    assert "rendering the board config failed" in result.stdout
    assert "staged:" not in result.stdout
    assert not (receipts / "board-config").exists()


def test_bringup_rejects_zero_or_bad_shots(tmp_path):
    for shots in ("0", "-3", "abc"):
        result, _, _ = _bringup(tmp_path / shots, "--phase", "proof", "--shots", shots)
        assert result.returncode == 2 and "--shots" in result.stderr


PROOF_BODY = ("--model", "Sony Alpha-A7 III (PC Control)", "--port", "usb:001,005")


def test_bringup_proof_dry_run_body_carries_the_serial(tmp_path):
    # /shoot answers serial_required to a request naming a model and port without a serial.
    result, _, _ = _bringup(tmp_path / "set", "--phase", "proof", *PROOF_BODY, "--serial", "A7III0SERIAL")
    assert result.returncode == 0, result.stderr
    proof = _printed_receipts(result.stdout)["proof"]
    assert proof["request_body"] == {"camera_model": "Sony Alpha-A7 III (PC Control)",
                                     "camera_port": "usb:001,005", "camera_serial": "A7III0SERIAL"}
    assert proof["serial"] == "A7III0SERIAL" and '"camera_serial":"A7III0SERIAL"' in proof["curl_command"]
    result, _, _ = _bringup(tmp_path / "unset", "--phase", "proof", *PROOF_BODY)
    assert _printed_receipts(result.stdout)["proof"]["serial"] == "<unset>"


def test_bringup_proof_apply_requires_a_serial(tmp_path):
    token = tmp_path / "token"
    token.write_text("t" * 32)
    receipts = tmp_path / "receipts"
    result, _, _ = _bringup(tmp_path, "--phase", "proof", *PROOF_BODY, "--token-file", str(token),
                            "--http-port", "9", mode="--apply", receipts=receipts)
    assert result.returncode == 2 and "--serial" in result.stderr
    assert not (receipts / "proof.json").exists() and not (receipts / "proof-shots.jsonl").exists()


@pytest.mark.parametrize("serial", ["SERIAL1\n", "SERIAL1\nX", "SERI\tAL1", "SERI\x7fAL1"])
def test_bringup_refuses_a_serial_with_control_characters(tmp_path, serial):
    for mode in ("--dry-run", "--apply"):
        result, _, receipts = _bringup(tmp_path / mode, "--phase", "proof", *PROOF_BODY, "--serial", serial,
                                       mode=mode)
        assert result.returncode == 2 and "--serial" in result.stderr, (mode, result.stdout)
        assert "RECEIPT" not in result.stdout and not receipts.exists()


@pytest.mark.parametrize("serial", ['SERI"AL1', "SERI'AL1", "SERI\\AL1", "SERI AL1", "SERIAL1\u00e9"])
def test_bringup_keeps_an_unusual_serial_exact_in_the_body(tmp_path, serial):
    # Linux discovery keeps sysfs serial bytes exactly; a JSON encoder builds the body, so a quote,
    # space, backslash or non-ASCII serial reaches /shoot unchanged instead of being refused.
    model = 'Sony "odd" \\ model'
    result, _, _ = _bringup(tmp_path, "--phase", "proof", "--model", model, "--port", "usb:001,005",
                            "--serial", serial)
    assert result.returncode == 0, result.stderr
    proof = _printed_receipts(result.stdout)["proof"]
    assert proof["request_body"] == {"camera_model": model, "camera_port": "usb:001,005", "camera_serial": serial}
    assert proof["serial"] == serial


def test_bringup_on_a_pi5_keeps_the_fallback_path(tmp_path):
    result, sentinel, receipts = _bringup(tmp_path, model="Raspberry Pi 5 Model B Rev 1.0")
    assert result.returncode == 0, result.stderr
    assert not sentinel.exists()
    printed = _printed_receipts(result.stdout)
    facts = printed["host-facts"]
    assert facts["board"]["board"] == "pi5" and facts["board"]["carrier"] == "none"
    assert not any("single USB-A port" in w for w in facts["warnings"])
    assert printed["board-config"]["applies_to_board"] is False
    assert not receipts.exists()


def test_bringup_rejects_an_unknown_carrier(tmp_path):
    result, _, _ = _bringup(tmp_path, "--carrier", "mystery")
    assert result.returncode == 2


# ---- host_readiness board facts -----------------------------------------------------------------
def test_host_readiness_reports_board_without_changing_the_verdict(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(host_readiness, "portable_readiness", lambda *a, **k: {"ready": False})
    code = host_readiness.main(["--storage", str(tmp_path), "--token-file", str(tmp_path / "t"),
                                "--manifest", str(tmp_path / "m"), "--artifact-root", str(tmp_path),
                                "--model-file", str(_model_file(tmp_path, CM5_MODEL))])
    report = json.loads(capsys.readouterr().out)
    assert code == 1 and report["ready"] is False
    assert report["board"]["board"] == "cm5" and report["board"]["carrier"] == "cm5-nano-a"


# ---- day_one: import and dry runs are inert -----------------------------------------------------
DAY_ONE_TESTS = ("autodetect", "capture-loop", "usb-replug-idle", "sleep-wake", "ap-soak", "merge-thermal")


def test_day_one_import_and_every_dry_run_run_and_write_nothing(tmp_path):
    env, sentinel = _sentinel_path(tmp_path, FORBIDDEN_ALL)
    receipts = tmp_path / "day1"
    imported = subprocess.run([sys.executable, "-c", "import deploy.day_one"], cwd=REPO,
                              capture_output=True, text=True, env=env)
    assert imported.returncode == 0, imported.stderr
    for test in DAY_ONE_TESTS:
        result = subprocess.run([sys.executable, str(REPO / "deploy/day_one.py"), test,
                                 "--receipt-dir", str(receipts)], capture_output=True, text=True, env=env)
        assert result.returncode == 0, result.stderr
        assert "WOULD-RUN" in result.stdout and "WOULD-WRITE" in result.stdout
        payload = json.loads(result.stdout.split("  RECEIPT ", 1)[1])
        assert payload["mode"] == "dry-run" and payload["executed"] is False
        assert payload["passed"] is None and payload["hardware_receipt"] is False
    assert not sentinel.exists(), sentinel.read_text()
    assert not receipts.exists(), "a dry run created the receipt dir"


@pytest.mark.parametrize("receipt_dir", [REPO / "receipts-never-created-by-tests", Path("/etc/day1-never-created")])
def test_day_one_refuses_repo_and_system_receipt_dirs(receipt_dir, capsys):
    assert day_one.main(["autodetect", "--run", "--receipt-dir", str(receipt_dir)], host=_ExplodingHost()) == 2
    assert not receipt_dir.exists()
    with pytest.raises(SystemExit):
        day_one.main(["autodetect", "--allow-system-dir", "--receipt-dir", "/etc/x"])


class _ExplodingHost(day_one.Host):
    """Any attempt to touch the machine fails the test."""

    def __init__(self, model_path: Path | None = None):
        super().__init__(sys_root=Path("/nonexistent-sys"), model_path=model_path or Path("/nonexistent"))

    def run(self, argv, timeout=60):
        raise AssertionError(f"dry run executed {argv}")

    def write(self, path, text):
        raise AssertionError(f"dry run wrote {path}")

    def prompt(self, message):
        raise AssertionError("dry run prompted the operator")


def _all_args(tmp_path):
    return _args(body="a7iii", expect_serial=None, model="M", port="usb:001,005", token_file="/dev/null",
                 shots=20, host="127.0.0.1", http_port=8000, interval=0.01, settle=1.0,
                 interface="wlan0", connection="camera-puck", hours=0.01, join_timeout=1.0,
                 leave_up=False, receipt_dir=tmp_path, jpegs=["a.jpg", "b.jpg"], enclosure="open-air",
                 minutes=1.0, mode="hdr", python=sys.executable)


@pytest.mark.parametrize("name", ["autodetect", "capture_loop", "usb_replug_idle", "sleep_wake",
                                  "ap_soak", "merge_thermal"])
def test_every_runner_honours_the_dry_run_guard(tmp_path, name):
    # Defect 8: removing any runner's `if not run: return` makes this fail (the host explodes).
    runner = getattr(day_one, f"run_{name}")
    receipt = tmp_path / "planned.json"
    extra = (receipt,) if name in ("capture_loop", "ap_soak", "merge_thermal") else ()
    payload = runner(_ExplodingHost(_model_file(tmp_path, CM5_MODEL)), _all_args(tmp_path), False, *extra)
    assert payload["executed"] is False and payload["passed"] is None
    assert _all_files(tmp_path) == [tmp_path / "model"]


def test_receipts_never_overwrite_each_other(tmp_path):
    first = day_one.reserve_receipt(tmp_path, "autodetect")
    second = day_one.reserve_receipt(tmp_path, "autodetect")
    assert first != second and first.exists() and second.exists()


# ---- day_one parsers ----------------------------------------------------------------------------
def test_parse_throttled_names_current_and_historic_flags():
    parsed = day_one.parse_throttled("throttled=0x50005")
    assert parsed["now"] == ["under_voltage", "throttled"]
    assert parsed["since_boot"] == ["under_voltage_occurred", "throttling_occurred"]
    assert day_one.parse_throttled("throttled=0x0")["now"] == []
    assert day_one.parse_throttled("") is None


def test_parse_vcgencmd_values():
    assert day_one.parse_measure_temp("temp=48.3'C") == 48.3
    assert day_one.parse_measure_clock("frequency(0)=2400000000") == 2400000000


AUTODETECT = """Model                          Port
----------------------------------------------------------
Sony Alpha-A7 III (PC Control) usb:001,005
Sony ILCE-7M2 (Control)        usb:003,002
"""


def test_parse_autodetect_keeps_model_names_with_spaces():
    assert day_one.parse_autodetect(AUTODETECT) == [
        {"model": "Sony Alpha-A7 III (PC Control)", "port": "usb:001,005"},
        {"model": "Sony ILCE-7M2 (Control)", "port": "usb:003,002"}]


def _station_dump(*stations):
    blocks = []
    for mac, connected in stations:
        blocks.append(f"Station {mac} (on wlan0)\n\tinactive time:\t300 ms\n\tsignal:  \t-52 [-52] dBm\n"
                      f"\tconnected time:\t{connected} seconds\n")
    return "".join(blocks)


def test_station_dump_hashes_macs_and_events_catch_reassociation():
    mac = "aa:bb:cc:dd:ee:ff"
    first = day_one.parse_station_dump(_station_dump((mac, 100)))
    sid = day_one.station_id(mac)
    assert mac not in json.dumps(first) and first[sid]["connected_time_s"] == 100
    assert first[sid]["signal_dbm"] == -52
    later = day_one.parse_station_dump(_station_dump((mac, 5)))
    assert day_one.station_events(first, later) == [{"event": "reassociate", "station": sid}]
    assert day_one.station_events(first, {}) == [{"event": "leave", "station": sid}]
    assert day_one.station_events({}, first) == [{"event": "join", "station": sid}]


def test_iw_info_and_brcmf_lines():
    info = day_one.parse_iw_info("Interface wlan0\n\tssid Camera-Puck\n\ttype AP\n\tchannel 6 (2437 MHz), width: 20 MHz\n")
    assert info == {"type": "AP", "channel": 6, "ssid": "Camera-Puck"}
    journal = ("kernel: brcmfmac: brcmf_sdio_htclk: HT Avail timeout (1000000): clkctl 0x50\n"
               "kernel: brcmfmac: brcmf_c_preinit_dcmds: Firmware: BCM4345/6\n")
    assert len(day_one.brcmf_error_lines(journal)) == 1


def test_summarize_health_never_calls_unmeasured_clean():
    unmeasured = day_one.summarize_health([{"t_s": 0, "throttled": None, "soc_temp_c": None,
                                             "thermal_zone0_c": 41.0, "arm_clock_hz": None}])
    assert unmeasured["throttle_clean"] is None and unmeasured["peak_temp_c"] == 41.0
    flagged = day_one.summarize_health([
        {"t_s": 0, "throttled": day_one.parse_throttled("0x0"), "soc_temp_c": 50.0},
        {"t_s": 9, "throttled": day_one.parse_throttled("0x80008"), "soc_temp_c": 81.5}])
    assert flagged["throttle_clean"] is False and flagged["first_flag_at_s"] == 9
    assert flagged["new_since_boot"] == ["soft_temperature_limit_occurred"]


# ---- day_one --run logic against a fake host ----------------------------------------------------
class FakeHost(day_one.Host):
    """Scripted commands, a temporary sysfs tree and a virtual clock. No real subprocess."""

    def __init__(self, tmp_path: Path, handler, model: str = CM5_MODEL, euid: int = 1000):
        super().__init__(sys_root=tmp_path / "sys", model_path=_model_file(tmp_path, model))
        self.handler, self.clock, self.calls, self.prompts, self.writes = handler, 0.0, [], [], []
        self._euid = euid
        self.on_prompt = None

    def run(self, argv, timeout=60):
        self.calls.append([str(a) for a in argv])
        rc, out = self.handler([str(a) for a in argv], self)
        return {"argv": [str(a) for a in argv], "returncode": rc, "stdout": out, "stderr": "", "wall_s": 0.0}

    def write(self, path, text):
        self.writes.append((str(path), text))

    def monotonic(self):
        return self.clock

    def sleep(self, seconds):
        self.clock += seconds

    def prompt(self, message):
        self.prompts.append(message)
        if self.on_prompt:
            self.on_prompt(len(self.prompts))
        return ""

    def euid(self):
        return self._euid


def _sony(host: FakeHost, serial="A7III0SERIAL", devnum=5, name="1-1"):
    device = host.sys_root / "bus/usb/devices" / name
    device.mkdir(parents=True, exist_ok=True)
    for key, value in {"idVendor": "054c", "busnum": "1", "devnum": str(devnum), "serial": serial,
                       "product": "ILCE-7M3", "speed": "480"}.items():
        (device / key).write_text(value + "\n")
    (host.sys_root / "bus/usb/devices" / f"{name}:1.0").mkdir(exist_ok=True)  # interface, ignored
    return device


def _vcgencmd(argv, throttled="0x0"):
    if argv[1] == "get_throttled":
        return 0, f"throttled={throttled}\n"
    if argv[1] == "measure_temp":
        return 0, "temp=52.1'C\n"
    return 0, "frequency(0)=2400000000\n"


def _args(**kw):
    return SimpleNamespace(**kw)


def test_autodetect_run_matches_body_by_serial_and_blocks_automounter(tmp_path):
    running = {"gvfs": False}

    def handler(argv, host):
        if argv[:2] == ["gphoto2", "--auto-detect"]:
            return 0, AUTODETECT
        if argv[0] == "pgrep":
            return (0, "123 /usr/libexec/gvfs-gphoto2-volume-monitor\n") if running["gvfs"] and "monitor" in argv[-1] else (1, "")
        raise AssertionError(argv)

    host = FakeHost(tmp_path, handler)
    _sony(host)
    ok = day_one.run_autodetect(host, _args(body="a7iii", expect_serial=None), True)
    assert ok["passed"] is True
    assert ok["identity"]["usb_serial"] == "A7III0SERIAL" and ok["identity"]["body_from_serial"] == "a7iii"
    running["gvfs"] = True
    blocked = day_one.run_autodetect(host, _args(body="a7iii", expect_serial=None), True)
    assert blocked["passed"] is False and blocked["automounters_running"]


def test_autodetect_fails_when_the_claimed_body_is_not_the_serial_on_usb(tmp_path):
    # Defect 4: --body a7ii with only the A7 III attached must not produce a passing a7ii receipt.
    host = FakeHost(tmp_path, lambda argv, h: (0, AUTODETECT) if argv[0] == "gphoto2" else (1, ""))
    _sony(host, serial="A7III0SERIAL")
    wrong = day_one.run_autodetect(host, _args(body="a7ii", expect_serial=None), True)
    assert wrong["passed"] is False
    assert "identity mismatch" in wrong["errors"][0] and "A7II00SERIAL" in wrong["errors"][0]
    assert "body" not in wrong and wrong["body_claimed"] == "a7ii"


def _usb_host(tmp_path, responsive_after, euid=1000, serial="A7III0SERIAL"):
    """responsive_after: probe index (0 = baseline) from which --summary succeeds."""
    state = {"summaries": 0}

    def handler(argv, host):
        if argv[0] == "pgrep":
            return 1, ""
        if argv[:2] == ["gphoto2", "--auto-detect"]:
            devnum = int((host.sys_root / "bus/usb/devices/1-1/devnum").read_text())
            return 0, f"Model Port\n---\nSony Alpha-A7 III (PC Control) usb:001,{devnum:03d}\n"
        if "--summary" in argv:
            index = state["summaries"]
            state["summaries"] += 1
            return (0, "ok") if index == 0 or index >= responsive_after else (1, "")
        raise AssertionError(argv)

    host = FakeHost(tmp_path, handler, euid=euid)
    _sony(host, devnum=5, serial=serial)
    host.on_prompt = lambda n: _sony(host, devnum=5 + n, serial=serial)  # every replug/rebind re-enumerates
    return host


def _idle_args(**kw):
    base = dict(settle=5, body=None, expect_serial=None)
    base.update(kw)
    return _args(**base)


def test_usb_replug_idle_plain_replug(tmp_path):
    host = _usb_host(tmp_path, responsive_after=1)
    result = day_one.run_usb_replug_idle(host, _idle_args(body="a7iii"), True)
    assert result["recovered_by"] == "replug_reenumeration" and result["passed"] is True
    assert result["physical_power_cycle_needed"] is False and result["interruption_observed"] is True
    assert result["identity"]["usb_serial"] == "A7III0SERIAL"
    assert "NOT a mid-session" in result["scope"]


def test_usb_replug_idle_unbind_bind_by_operator_when_not_root(tmp_path):
    host = _usb_host(tmp_path, responsive_after=2)
    result = day_one.run_usb_replug_idle(host, _idle_args(), True)
    assert result["recovered_by"] == "unbind_bind" and result["passed"] is True
    assert result["steps"][-1]["performed_by"] == "operator" and host.writes == []
    assert "sudo tee" in host.prompts[1] and "/bus/usb/drivers/usb/unbind" in host.prompts[1]


def test_usb_replug_idle_unbind_bind_by_script_as_root(tmp_path):
    host = _usb_host(tmp_path, responsive_after=2, euid=0)
    result = day_one.run_usb_replug_idle(host, _idle_args(), True)
    assert result["recovered_by"] == "unbind_bind"
    assert [Path(p).name for p, _ in host.writes] == ["unbind", "bind"]
    assert all(text == "1-1" for _, text in host.writes)


def test_usb_replug_idle_records_a_needed_power_cycle(tmp_path):
    host = _usb_host(tmp_path, responsive_after=3)
    result = day_one.run_usb_replug_idle(host, _idle_args(), True)
    assert result["recovered_by"] == "physical_power_cycle"
    assert result["physical_power_cycle_needed"] is True and result["passed"] is False


def test_usb_replug_idle_refuses_a_body_claim_the_serial_contradicts(tmp_path):
    host = _usb_host(tmp_path, responsive_after=1)
    result = day_one.run_usb_replug_idle(host, _idle_args(body="a7ii"), True)
    assert result["passed"] is False and "identity mismatch" in result["errors"][0]
    assert not host.prompts and not any("--summary" in c for c in host.calls)


def test_usb_replug_idle_refuses_while_the_service_owns_the_camera(tmp_path):
    host = FakeHost(tmp_path, lambda argv, h: (0, "999 uvicorn app:app") if argv[0] == "pgrep" else (1, ""))
    _sony(host)
    result = day_one.run_usb_replug_idle(host, _idle_args(), True)
    assert result["passed"] is False and "service is running" in result["errors"][0]
    assert not any(c[0] == "gphoto2" for c in host.calls)


def test_the_old_usb_recover_name_is_gone():
    # Defect 6b: the test is named for what it does (service stopped, body idle).
    with pytest.raises(SystemExit):
        day_one.build_parser().parse_args(["usb-recover", "--receipt-dir", "/tmp/x"])


def test_sleep_wake_wake_alone_recovers(tmp_path):
    host = _usb_host(tmp_path, responsive_after=1)
    result = day_one.run_sleep_wake(host, _idle_args(), True)
    assert result["recovered_by"] == "wake_only" and result["passed"] is True
    asleep = next(s for s in result["steps"] if s["step"] == "asleep")
    assert asleep["present_on_usb"] is True and asleep["gphoto2_label"]
    # While asleep only passive reads happen: the one --summary after baseline follows the wake.
    assert sum("--summary" in c for c in host.calls) == 2


def test_sleep_wake_records_a_needed_power_cycle(tmp_path):
    host = _usb_host(tmp_path, responsive_after=3)
    result = day_one.run_sleep_wake(host, _idle_args(), True)
    assert result["recovered_by"] == "physical_power_cycle"
    assert result["physical_power_cycle_needed"] is True and result["passed"] is False


def _soak_host(tmp_path, dumps, journal=(0, "kernel: brcmfmac: brcmf_c_preinit_dcmds: Firmware: BCM4345/6\n")):
    """dumps: station-dump text per sample; the clock advances by --interval between samples."""
    state = {"i": 0}

    def handler(argv, host):
        if argv[:2] == ["nmcli", "-t"]:
            return 0, "preconfigured\ncamera-puck\n"
        if argv[0] == "nmcli":
            return 0, ""
        if argv[:2] == ["iw", "dev"] and argv[-1] == "info":
            return 0, "Interface wlan0\n\ttype AP\n\tchannel 6 (2437 MHz)\n"
        if argv[:2] == ["iw", "dev"]:
            text = dumps[min(state["i"], len(dumps) - 1)]
            state["i"] += 1
            return 0, text
        if argv[0] == "vcgencmd":
            return _vcgencmd(argv)
        if argv[0] == "journalctl":
            return journal
        raise AssertionError(argv)

    return FakeHost(tmp_path, handler)


def _soak_args(tmp_path, **kw):
    base = dict(interface="wlan0", connection="camera-puck", hours=0.01, interval=10.0,
                join_timeout=10.0, leave_up=False, receipt_dir=tmp_path)
    base.update(kw)
    return _args(**base)


def test_ap_soak_passes_with_a_steady_phone_and_writes_partial_receipts(tmp_path):
    mac = "aa:bb:cc:dd:ee:ff"
    dumps = [""] + [_station_dump((mac, 10 * n)) for n in range(1, 10)]
    host = _soak_host(tmp_path, dumps)
    receipt = day_one.reserve_receipt(tmp_path, "ap-soak")
    result = day_one.run_ap_soak(host, _soak_args(tmp_path), True, receipt)
    assert result["passed"] is True, result["errors"]
    assert result["disconnect_count"] == 0 and result["first_join_s"] == 10.0
    assert json.loads(receipt.read_text())["samples"]  # written during the soak
    assert ["nmcli", "connection", "down", "id", "camera-puck"] in host.calls


def test_ap_soak_with_an_unreadable_kernel_log_is_inconclusive(tmp_path):
    # Defect 7: access denied + empty stdout used to read as "0 brcmfmac errors, passed".
    mac = "aa:bb:cc:dd:ee:ff"
    dumps = [""] + [_station_dump((mac, 10 * n)) for n in range(1, 10)]
    host = _soak_host(tmp_path, dumps, journal=(1, ""))
    receipt = day_one.reserve_receipt(tmp_path, "ap-soak")
    result = day_one.run_ap_soak(host, _soak_args(tmp_path), True, receipt)
    assert result["passed"] is None
    assert result["kernel_log"]["read_ok"] is False and result["brcmf_error_count"] is None
    assert any("kernel log unreadable" in e for e in result["errors"])


def test_ap_soak_fails_on_a_reassociation(tmp_path):
    mac = "aa:bb:cc:dd:ee:ff"
    dumps = [_station_dump((mac, 100)), _station_dump((mac, 110)), _station_dump((mac, 3)),
             _station_dump((mac, 13)), _station_dump((mac, 23))]
    host = _soak_host(tmp_path, dumps)
    receipt = day_one.reserve_receipt(tmp_path, "ap-soak")
    result = day_one.run_ap_soak(host, _soak_args(tmp_path), True, receipt)
    assert result["passed"] is False and result["disconnect_count"] == 1


def test_ap_soak_needs_the_installed_profile(tmp_path):
    host = FakeHost(tmp_path, lambda argv, h: (0, "preconfigured\n"))
    receipt = day_one.reserve_receipt(tmp_path, "ap-soak")
    result = day_one.run_ap_soak(host, _soak_args(tmp_path), True, receipt)
    assert result["passed"] is False and "not found" in result["errors"][0]
    assert not any(c[:3] == ["nmcli", "connection", "up"] for c in host.calls)


RAW_PASSED = {"verdict": "passed", "passed": True, "accepted_shots": 20, "shots_with_raw": 20}


def _capture_host(tmp_path, throttled="0x0", proof=None, serial="A7III0SERIAL"):
    proof = proof if proof is not None else {
        "pass": True, "no_drop_pass": True, "requested_shots": 20, "recorded_shots": 20, "http_200": 20,
        "verified_readback": 20, "reenumeration_failures": [], "raw_transfer": RAW_PASSED}

    def handler(argv, host):
        if argv[0] == "vcgencmd":
            return _vcgencmd(argv, throttled)
        if argv[0] == "bash":
            proof_dir = Path(argv[argv.index("--receipt-dir") + 1])
            proof_dir.mkdir(parents=True)
            (proof_dir / "proof.json").write_text(json.dumps(proof))
            _sony(host, devnum=9, serial=serial)  # the proof re-enumerates the body: the port moves
            return (0 if proof.get("pass") else 1), ""
        raise AssertionError(argv)

    host = FakeHost(tmp_path, handler)
    _sony(host, devnum=5, serial=serial)
    return host


def _capture_args(**kw):
    base = dict(body="a7iii", model="Sony Alpha-A7 III (PC Control)", port="usb:001,005",
                token_file="/dev/null", shots=20, host="127.0.0.1", http_port=8000, interval=0.01)
    base.update(kw)
    return _args(**base)


def _capture(tmp_path, name, **kw):
    folder = tmp_path / name
    folder.mkdir()
    args = kw.pop("args", _capture_args())
    host = _capture_host(folder, **kw)
    return host, day_one.run_capture_loop(host, args, True, day_one.reserve_receipt(folder, "capture-loop"))


def test_capture_loop_reuses_the_20_shot_proof_and_passes_with_raw_evidence(tmp_path):
    host, result = _capture(tmp_path, "ok")
    bash = next(c for c in host.calls if c[0] == "bash")
    assert bash[1].endswith("deploy/pi_bringup.sh") and bash[bash.index("--phase") + 1] == "proof"
    assert result["kill_criterion_passed"] is True
    assert result["criteria"]["raw_plus_jpeg_download"] is True
    assert result["passed"] is True and result["raw_transfer"]["verdict"] == "passed"
    assert result["identity"]["usb_serial"] == "A7III0SERIAL"
    assert result["identity_after"]["port"] == "usb:001,009"  # same serial, new port
    # /shoot refuses a capture without a serial: the proof gets the one sysfs reported at --port.
    assert bash[bash.index("--serial") + 1] == "A7III0SERIAL"
    assert result["health"]["samples"] >= 2


def _proof_with_raw(raw):
    proof = {"pass": raw is not None and raw.get("passed") is True, "no_drop_pass": True, "requested_shots": 20,
             "recorded_shots": 20, "http_200": 20, "verified_readback": 20, "reenumeration_failures": []}
    if raw is not None:
        proof["raw_transfer"] = raw
    return proof


@pytest.mark.parametrize("raw", [
    {"verdict": "raw_missing", "passed": False, "accepted_shots": 20, "shots_with_raw": 19},
    {"verdict": "duplicate_hash", "passed": False, "accepted_shots": 20, "shots_with_raw": 20},
    {"verdict": "passed", "passed": True, "accepted_shots": 20, "shots_with_raw": 19},
])
def test_capture_loop_fails_the_kill_criterion_on_any_raw_verdict_but_passed(tmp_path, raw):
    _, result = _capture(tmp_path, "raw", proof=_proof_with_raw(raw))
    assert result["criteria"]["twenty_shot_no_drop"] is True
    assert result["criteria"]["raw_plus_jpeg_download"] is False
    assert result["passed"] is False and result["kill_criterion_passed"] is False
    assert "raw_plus_jpeg_download" in result["verdict_reason"]


@pytest.mark.parametrize("legacy", [False, True])
def test_capture_loop_without_a_raw_verdict_is_inconclusive_never_a_pass(tmp_path, legacy):
    # Defect 6a: a proof that records no RAW evidence can never be reported as a pass. A proof in
    # the schema before RAW evidence (no no_drop_pass; its "pass" was the no-drop verdict) keeps
    # its no-drop result and stays inconclusive, never a spurious fail.
    proof = _proof_with_raw(None)
    if legacy:
        proof.pop("no_drop_pass")
        proof["pass"] = True
    _, result = _capture(tmp_path, "noraw", proof=proof)
    assert result["criteria"]["twenty_shot_no_drop"] is True
    assert result["criteria"]["raw_plus_jpeg_download"] is None
    assert result["passed"] is None and result["kill_criterion_passed"] is False
    assert result["verdict_reason"] == "unverified: raw_plus_jpeg_download"


def test_capture_loop_throttling_fails_the_kill_criterion(tmp_path):
    _, hot = _capture(tmp_path, "hot", throttled="0x80008")
    assert hot["passed"] is False and hot["kill_criterion_passed"] is False
    assert hot["criteria"]["throttle_clean"] is False


@pytest.mark.parametrize("counts", [(0, 0, 0, 0), (20, 19, 19, 19), (20, 20, 20, 19)])
def test_capture_loop_never_passes_zero_or_partial_proofs(tmp_path, counts):
    # Defect 3: the inherited proof reports 0/0 as pass=True; this tool must not.
    requested, recorded, ok, verified = counts
    proof = {"pass": True, "no_drop_pass": True, "requested_shots": requested, "recorded_shots": recorded,
             "http_200": ok, "verified_readback": verified, "reenumeration_failures": [],
             "raw_transfer": RAW_PASSED}
    _, result = _capture(tmp_path, "c", proof=proof)
    assert result["passed"] is False and result["kill_criterion_passed"] is False
    assert result["criteria"]["twenty_shot_no_drop"] is False


def test_proof_verdict_rejects_zero_and_below_twenty():
    zero = {"no_drop_pass": True, "requested_shots": 0, "recorded_shots": 0, "http_200": 0, "verified_readback": 0}
    assert day_one.proof_verdict(zero, 0)[0] is False
    ten = {"no_drop_pass": True, "requested_shots": 10, "recorded_shots": 10, "http_200": 10, "verified_readback": 10}
    assert day_one.proof_verdict(ten, 10)[0] is False
    assert day_one.proof_verdict(None, 20)[0] is False


def test_capture_loop_cli_refuses_fewer_than_twenty_shots_and_the_raw_flag():
    parser = day_one.build_parser()
    for bad in (["--shots", "0"], ["--shots", "19"], ["--raw-plus-jpeg-confirmed"]):
        with pytest.raises(SystemExit):
            parser.parse_args(["capture-loop", "--receipt-dir", "/tmp/x", *bad])
    assert parser.parse_args(["capture-loop", "--receipt-dir", "/tmp/x"]).shots == 20


def test_capture_loop_refuses_a_body_claim_the_port_serial_contradicts(tmp_path):
    # Defect 4: --body a7ii on the A7 III's port used to record body="a7ii", passed=True.
    host, result = _capture(tmp_path, "wrong", args=_capture_args(body="a7ii"))
    assert result["passed"] is False and "identity mismatch" in result["errors"][0]
    assert result["identity"]["usb_serial"] == "A7III0SERIAL"
    assert not any(c[0] == "bash" for c in host.calls), "the wrong body must never be fired"


@pytest.mark.parametrize("raw,code,passed", [(RAW_PASSED, 0, True), (None, day_one.EXIT_INCONCLUSIVE, None)])
def test_capture_loop_run_via_main_writes_the_receipt_the_raw_verdict_allows(tmp_path, raw, code, passed):
    host = _capture_host(tmp_path, proof=_proof_with_raw(raw))
    receipts = tmp_path / "day1"
    exit_code = day_one.main(["capture-loop", "--run", "--body", "a7iii", "--model", "M", "--port", "usb:001,005",
                              "--token-file", "/dev/null", "--interval", "0.01", "--receipt-dir", str(receipts)],
                             host=host)
    assert exit_code == code
    written = json.loads(next(receipts.glob("day1-capture-loop-*.json")).read_text())
    assert written["passed"] is passed and written["kill_criterion_passed"] is (raw is not None)


def test_merge_thermal_loops_the_existing_bench_until_time_is_up(tmp_path):
    def handler(argv, host):
        if argv[0] == "vcgencmd":
            return _vcgencmd(argv)
        if argv[1].endswith("tools/puck_workload_bench.py"):
            host.clock += 60
            Path(argv[argv.index("--output") + 1]).write_text(json.dumps(
                {"inputs": [{"width": 6000, "height": 4000}],
                 "runs": [{"status": "completed", "merge_wall_s": 9.5}] * 3}))
            return 0, ""
        raise AssertionError(argv)

    host = FakeHost(tmp_path, handler)
    receipt = day_one.reserve_receipt(tmp_path, "merge-thermal")
    args = _args(jpegs=["a.jpg", "b.jpg", "c.jpg"], enclosure="open-air", minutes=2.0, interval=0.01,
                 mode="hdr", python=sys.executable)
    result = day_one.run_merge_thermal(host, args, True, receipt)
    assert result["passed"] is True and result["completed_iterations"] == 2
    assert result["input_megapixels"] == 24.0 and result["enclosure"] == "open-air"


# ---- round 3: remote host, path resolution, links, atomic staging, bytecode ------------------------
def test_capture_loop_refuses_a_remote_host(tmp_path):
    # The serial is read from local sysfs; a remote service could fire another body at that address.
    host, result = _capture(tmp_path, "remote", args=_capture_args(host="192.0.2.20"))
    assert result["passed"] is False and "refusing --host 192.0.2.20" in result["errors"][0]
    assert result["kill_criterion_passed"] is None
    assert not any(c[0] == "bash" for c in host.calls), "a remote service must never be fired"
    _, local = _capture(tmp_path, "local", args=_capture_args(host="localhost"))
    assert local["kill_criterion_passed"] is True


def test_render_resolves_a_quoted_tilde_once(tmp_path, monkeypatch):
    home, cwd = tmp_path / "home", tmp_path / "cwd"
    home.mkdir()
    cwd.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.chdir(cwd)
    assert cm5_nano.main(["render", "--apply", "--out", "~/stage"]) == 0
    assert (home / "stage" / "STAGED.json").is_file()
    assert not (cwd / "~").exists(), "checked $HOME/stage but wrote ./~/stage"


def test_day_one_resolves_a_quoted_tilde_once(tmp_path, monkeypatch):
    home, cwd = tmp_path / "home", tmp_path / "cwd"
    home.mkdir()
    cwd.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.chdir(cwd)
    host = FakeHost(tmp_path, lambda argv, h: (1, ""))
    day_one.main(["autodetect", "--run", "--receipt-dir", "~/day1"], host=host)
    assert list((home / "day1").glob("day1-autodetect-*.json"))
    assert not (cwd / "~").exists()


def test_bringup_resolves_a_quoted_tilde_once(tmp_path):
    home, cwd = tmp_path / "home", tmp_path / "cwd"
    home.mkdir()
    cwd.mkdir()
    env, _ = _sentinel_path(tmp_path)
    env.update(HOME=str(home), CAMERA_AUTOPILOT_MODEL_FILE=str(_model_file(tmp_path, CM5_MODEL)))
    result = subprocess.run(["bash", str(BRINGUP), "--apply", "--phase", "host-facts", "--receipt-dir", "~/r"],
                            cwd=cwd, capture_output=True, text=True, env=env)
    assert result.returncode == 0, result.stderr
    assert (home / "r" / "host-facts.json").is_file()
    assert not (cwd / "~").exists()


def test_safe_paths_catches_a_differently_cased_spelling():
    variant = Path(str(REPO).swapcase())
    if not variant.exists() or not os.path.samefile(variant, REPO):
        pytest.skip("case-sensitive filesystem: no second spelling of the repo exists")
    assert "inside the repository" in safe_paths.refusal(variant / "receipts", REPO)
    assert "inside the repository" in safe_paths.refusal(variant, REPO)
    etc = Path("/ETC")
    if etc.exists() and os.path.samefile(etc, "/etc"):
        assert safe_paths.refusal(etc / "camera-autopilot", REPO)


def test_bringup_refuses_a_receipt_file_symlinked_elsewhere(tmp_path):
    victim = tmp_path / "victim.txt"
    victim.write_text("do not touch\n")
    receipts = tmp_path / "receipts"
    receipts.mkdir()
    (receipts / "host-facts.json").symlink_to(victim)
    result, _, _ = _bringup(tmp_path, "--phase", "host-facts", mode="--apply", receipts=receipts)
    assert result.returncode == 2 and "symlink" in result.stderr
    assert victim.read_text() == "do not touch\n"


def test_day_one_receipt_writes_never_follow_planted_links(tmp_path):
    victim = tmp_path / "victim.txt"
    victim.write_text("do not touch\n")
    receipts = tmp_path / "receipts"
    receipts.mkdir()
    receipt = receipts / "day1-x.json"
    (receipts / "day1-x.json.tmp").symlink_to(victim)  # the old fixed temp name
    day_one.write_receipt(receipt, {"ok": True})
    assert victim.read_text() == "do not touch\n"
    receipt.unlink()
    receipt.symlink_to(victim)                            # the receipt path itself swapped for a link
    day_one.write_receipt(receipt, {"ok": True})
    assert victim.read_text() == "do not touch\n" and not receipt.is_symlink()


def test_staging_is_atomic_when_a_write_fails(tmp_path, monkeypatch):
    real, calls = cm5_nano._write_new, []

    def enospc_on_second(path, text):
        calls.append(path)
        if len(calls) == 2:
            raise OSError(errno.ENOSPC, "No space left on device")
        real(path, text)

    monkeypatch.setattr(cm5_nano, "_write_new", enospc_on_second)
    target = tmp_path / "board-config"
    with pytest.raises(OSError):
        cm5_nano.write_files(target, cm5_nano.render_files())
    assert not target.exists()
    assert list(tmp_path.iterdir()) == [], "a partial staging directory was left behind"


def test_verify_staged_rejects_a_partial_directory(tmp_path):
    staged = tmp_path / "board-config"
    cm5_nano.write_files(staged, cm5_nano.render_files())
    assert cm5_nano.verify_staged(staged) is None
    (staged / "camera-puck.nmconnection").write_text("tampered\n")
    assert "manifest hash" in cm5_nano.verify_staged(staged)
    partial = tmp_path / "partial"
    partial.mkdir()
    (partial / "config.txt.cm5-nano-a").write_text("x\n")
    assert "STAGED.json" in cm5_nano.verify_staged(partial)


def test_bringup_never_reports_a_partial_staging_as_staged(tmp_path):
    receipts = tmp_path / "receipts"
    (receipts / "board-config").mkdir(parents=True)
    (receipts / "board-config" / "config.txt.cm5-nano-a").write_text("half\n")  # no profile, no manifest
    result, _, _ = _bringup(tmp_path, "--phase", "board-config", mode="--apply", receipts=receipts)
    assert result.returncode != 0
    assert "STAGED.json" in result.stdout and "kept existing" not in result.stdout
    assert json.loads((receipts / "board-config.json").read_text())["error"]


def _mini_repo(tmp_path: Path, cm5_stub: str | None = None) -> Path:
    """A throwaway copy of the scripts: the script's own REPO is then a tree this test can watch."""
    repo = tmp_path / "mini-repo"
    (repo / "deploy").mkdir(parents=True)
    for name in ("pi_bringup.sh", "cm5_nano.py", "safe_paths.py", "day_one.py", "host_readiness.py", "raw_proof.py"):
        shutil.copy2(REPO / "deploy" / name, repo / "deploy" / name)
    (repo / "backend").mkdir()
    shutil.copy2(REPO / "backend" / "device_health.py", repo / "backend" / "device_health.py")  # host_readiness
    if cm5_stub is not None:
        (repo / "deploy" / "cm5_nano.py").write_text(cm5_stub)
    return repo


def _tree(root: Path) -> dict[str, str]:
    """Every path under root with its content hash (not mtime), so a rewrite with the same bytes
    is equal and any changed byte or new file is not."""
    tree = {}
    for path in root.rglob("*"):
        if path.is_symlink():
            tree[str(path.relative_to(root))] = "link:" + os.readlink(path)
        elif path.is_dir():
            tree[str(path.relative_to(root))] = "dir"
        else:
            tree[str(path.relative_to(root))] = hashlib.sha256(path.read_bytes()).hexdigest()
    return tree


def _mini_env(tmp_path: Path) -> dict:
    env, _ = _sentinel_path(tmp_path / "mini-bin")
    env.pop("PYTHONDONTWRITEBYTECODE", None)  # the scripts must suppress bytecode themselves
    env["CAMERA_AUTOPILOT_MODEL_FILE"] = str(_model_file(tmp_path, CM5_MODEL))
    return env


def test_bringup_preview_render_failure_is_a_failure(tmp_path):
    stub = ("import sys\n"
            "if sys.argv[1] == 'facts':\n    print('{\"board\": \"cm5\"}')\n    sys.exit(0)\n"
            "sys.exit(17)\n")
    repo = _mini_repo(tmp_path, stub)
    result = subprocess.run(["bash", str(repo / "deploy/pi_bringup.sh"), "--dry-run", "--phase", "board-config",
                             "--receipt-dir", str(tmp_path / "receipts")],
                            capture_output=True, text=True, env=_mini_env(tmp_path))
    assert result.returncode != 0
    printed = _printed_receipts(result.stdout)["board-config"]
    assert printed["error"] == "config render failed" and printed["render_exit_code"] == 17


def test_the_shell_wrapper_alone_suppresses_bytecode(tmp_path):
    # Strip the entry points' own flag from the copies: only PYTHONDONTWRITEBYTECODE=1 exported by
    # pi_bringup.sh can keep deploy/__pycache__ from appearing now.
    repo = _mini_repo(tmp_path)
    for name in ("cm5_nano.py", "safe_paths.py"):
        path = repo / "deploy" / name
        path.write_text(path.read_text().replace("sys.dont_write_bytecode = True", "pass"))
    before = _tree(repo)
    receipts = tmp_path / "receipts"
    dry = subprocess.run(["bash", str(repo / "deploy/pi_bringup.sh"), "--dry-run", "--receipt-dir", str(receipts)],
                         capture_output=True, text=True, env=_mini_env(tmp_path))
    assert dry.returncode == 0, dry.stderr
    assert _tree(repo) == before, "the shell run left files (e.g. deploy/__pycache__) in the repo"
    assert not receipts.exists()


def test_entry_points_alone_suppress_bytecode(tmp_path):
    # No PYTHONDONTWRITEBYTECODE in the environment: each script's own flag must do it.
    repo = _mini_repo(tmp_path)
    before = _tree(repo)
    env = _mini_env(tmp_path)
    # script -> (arguments, expected exit code). host_readiness exits 1 here: nothing is ready,
    # which is its verdict, not a crash.
    runs = {"cm5_nano.py": (["render"], 0),
            "safe_paths.py": (["--repo", str(repo), str(tmp_path / "x")], 0),
            "day_one.py": (["autodetect", "--receipt-dir", str(tmp_path / "d1")], 0),
            "host_readiness.py": (["--storage", str(tmp_path), "--token-file", str(tmp_path / "t"),
                                   "--manifest", str(tmp_path / "m"), "--artifact-root", str(tmp_path),
                                   "--model-file", str(_model_file(tmp_path, CM5_MODEL))], 1)}
    # Every deploy script that claims to suppress bytecode itself must be exercised here.
    flagged = {p.name for p in (REPO / "deploy").glob("*.py")
               if "sys.dont_write_bytecode = True" in p.read_text()}
    assert flagged == set(runs), f"entry points with the flag {sorted(flagged)} != exercised {sorted(runs)}"
    for name, (argv, code) in runs.items():
        result = subprocess.run([sys.executable, str(repo / "deploy" / name), *argv],
                                capture_output=True, text=True, env=env)
        assert result.returncode == code and "Traceback" not in result.stderr, (name, result.stderr)
    assert _tree(repo) == before, "an entry point left files (e.g. __pycache__) in the repo"


@pytest.mark.parametrize("mode", ["--dry-run", "--apply"])
def test_an_existing_receipt_dir_keeps_its_mode(tmp_path, mode):
    repo = _mini_repo(tmp_path)
    before_repo = _tree(repo)
    receipts = tmp_path / "receipts"
    receipts.mkdir()
    receipts.chmod(0o750)
    (receipts / "operator-notes.txt").write_text("mine\n")
    before_receipts = _tree(receipts)
    result = subprocess.run(["bash", str(repo / "deploy/pi_bringup.sh"), mode, "--phase", "host-facts",
                             "--receipt-dir", str(receipts)], capture_output=True, text=True, env=_mini_env(tmp_path))
    assert result.returncode == 0, result.stderr
    assert stat.S_IMODE(receipts.stat().st_mode) == 0o750, "an existing receipt dir was chmodded"
    assert _tree(repo) == before_repo
    after = _tree(receipts)
    if mode == "--dry-run":
        assert after == before_receipts
    else:
        assert set(after) - set(before_receipts) == {"host-facts.json"}
        assert after["operator-notes.txt"] == before_receipts["operator-notes.txt"]


# ---- round 4 -----------------------------------------------------------------------------------------
def test_capture_loop_refuses_ipv6_loopback_it_cannot_address(tmp_path):
    # pi_bringup.sh builds http://HOST:PORT/shoot unbracketed, so ::1 would not be a valid URL.
    assert "::1" not in day_one.LOOPBACK_HOSTS and "[::1]" not in day_one.LOOPBACK_HOSTS
    host, result = _capture(tmp_path, "v6", args=_capture_args(host="::1"))
    assert result["passed"] is False and "refusing --host ::1" in result["errors"][0]
    assert not any(c[0] == "bash" for c in host.calls)


def _staged(tmp_path: Path) -> Path:
    staged = tmp_path / "board-config"
    cm5_nano.write_files(staged, cm5_nano.render_files())
    return staged


@pytest.mark.parametrize("manifest", [
    {"files": {}},
    {"schema": "camera-cm5-staged-v1", "files": {}},
    {"files": "x"},
    {"schema": "something-else", "files": None},
])
def test_verify_staged_requires_schema_and_both_files(tmp_path, manifest):
    staged = _staged(tmp_path)
    (staged / "STAGED.json").write_text(json.dumps(manifest))
    assert cm5_nano.verify_staged(staged) is not None


def test_verify_staged_requires_exactly_the_expected_files(tmp_path):
    staged = _staged(tmp_path)
    manifest = json.loads((staged / "STAGED.json").read_text())
    manifest["files"].pop("camera-puck.nmconnection")
    (staged / "STAGED.json").write_text(json.dumps(manifest))
    assert "exactly" in cm5_nano.verify_staged(staged)
    manifest["files"]["camera-puck.nmconnection"] = "0" * 64
    manifest["files"]["extra.txt"] = "0" * 64
    (staged / "STAGED.json").write_text(json.dumps(manifest))
    assert "exactly" in cm5_nano.verify_staged(staged)


def test_verify_staged_refuses_a_manifest_missing_its_schema(tmp_path):
    # Every file and hash is still right; only the schema key is gone. That is a foreign or older
    # manifest, never a staging to accept with a default schema.
    staged = _staged(tmp_path)
    manifest = json.loads((staged / "STAGED.json").read_text())
    del manifest["schema"]
    (staged / "STAGED.json").write_text(json.dumps(manifest))
    assert "no valid STAGED.json" in cm5_nano.verify_staged(staged)
    assert cm5_nano.main(["verify", "--dir", str(staged)]) == 1


def test_verify_staged_hashes_bytes_so_crlf_is_a_change(tmp_path):
    staged = _staged(tmp_path)
    fragment = staged / "config.txt.cm5-nano-a"
    fragment.write_bytes(fragment.read_bytes().replace(b"\n", b"\r\n"))
    assert "manifest hash" in cm5_nano.verify_staged(staged)


def _hardlinked_victim(tmp_path: Path, receipts: Path, name: str) -> Path:
    victim = tmp_path / "victim.txt"
    victim.write_text("do not touch\n")
    receipts.mkdir(exist_ok=True)
    os.link(victim, receipts / name)  # a hard link is not a symlink: the dir scan cannot see it
    return victim


def test_host_facts_writer_never_writes_through_a_hard_link(tmp_path):
    receipts = tmp_path / "receipts"
    victim = _hardlinked_victim(tmp_path, receipts, "host-facts.json")
    result, _, _ = _bringup(tmp_path, "--phase", "host-facts", mode="--apply", receipts=receipts)
    assert result.returncode == 0, result.stderr
    assert victim.read_text() == "do not touch\n"
    assert json.loads((receipts / "host-facts.json").read_text())["phase"] == "host-facts"


def test_soak_writer_never_writes_through_a_hard_link(tmp_path):
    receipts = tmp_path / "receipts"
    victim = _hardlinked_victim(tmp_path, receipts, "soak.json")
    result, _, _ = _bringup(tmp_path, "--phase", "soak", mode="--apply", receipts=receipts)
    assert result.returncode == 0, result.stderr
    assert victim.read_text() == "do not touch\n"
    assert json.loads((receipts / "soak.json").read_text())["phase"] == "soak"


def test_proof_writer_never_writes_through_a_hard_link(tmp_path):
    receipts = tmp_path / "receipts"
    victim = _hardlinked_victim(tmp_path, receipts, "proof.json")
    env, _ = _sentinel_path(tmp_path)
    fake_curl = tmp_path / "fakebin" / "curl"  # never reaches a real service or camera
    fake_curl.write_text("#!/usr/bin/env bash\nexit 7\n")
    fake_curl.chmod(0o755)
    token = tmp_path / "token"
    token.write_text("t" * 32)
    env["CAMERA_AUTOPILOT_MODEL_FILE"] = str(_model_file(tmp_path, CM5_MODEL))
    result = subprocess.run(["bash", str(BRINGUP), "--apply", "--phase", "proof", "--shots", "1",
                             "--model", "Sony Alpha-A7 III (PC Control)", "--port", "usb:001,005",
                             "--serial", "A7III0SERIAL",
                             "--token-file", str(token), "--http-port", "9", "--receipt-dir", str(receipts)],
                            capture_output=True, text=True, env=env)
    assert result.returncode == 1, result.stderr  # the fake transport fails the proof, as it must
    assert victim.read_text() == "do not touch\n"
    assert json.loads((receipts / "proof.json").read_text())["pass"] is False


def _proof_apply(tmp_path, answered_serial=None, serial="A7III0SERIAL", response=None):
    env, _ = _sentinel_path(tmp_path)
    # A fake /shoot answers 200 with a verified readback naming answered_serial; no service or camera.
    payload = json.dumps(response or {"ok": True, "image": "shot_1.jpg",
                                      "evidence": {"camera_serial": answered_serial,
                                                   "settings_readback": {"after_final": "verified"}}})
    fake_curl = tmp_path / "fakebin" / "curl"
    fake_curl.write_text("#!/usr/bin/env bash\n"
                         "out=''; while [[ $# -gt 0 ]]; do [[ $1 == -o ]] && out=$2; shift; done\n"
                         f"printf '%s' {shlex.quote(payload)} > \"$out\"\n"
                         "printf '200 0.50'\n")
    fake_curl.chmod(0o755)
    token = tmp_path / "token"
    token.write_text("t" * 32)
    receipts = tmp_path / "receipts"
    env["CAMERA_AUTOPILOT_MODEL_FILE"] = str(_model_file(tmp_path, CM5_MODEL))
    result = subprocess.run(["bash", str(BRINGUP), "--apply", "--phase", "proof", "--shots", "2",
                             *PROOF_BODY, "--serial", serial,
                             "--token-file", str(token), "--http-port", "9", "--receipt-dir", str(receipts)],
                            capture_output=True, text=True, env=env)
    return result, json.loads((receipts / "proof.json").read_text())


def test_applied_proof_receipt_records_the_serial_each_shot_answered(tmp_path):
    result, proof = _proof_apply(tmp_path, answered_serial="A7III0SERIAL")
    assert proof["no_drop_pass"] is True and proof["serial"] == "A7III0SERIAL" and proof["serial_mismatches"] == []
    # This fake answer carries no download record, so RAW transfer (tests/test_raw_proof.py) fails it.
    assert proof["raw_transfer"]["verdict"] == "raw_missing" and proof["pass"] is False
    assert result.returncode == 1, (result.stdout, result.stderr)
    assert [shot["camera_serial"] for shot in proof["shots"]] == ["A7III0SERIAL", "A7III0SERIAL"]


def test_applied_proof_fails_when_another_body_answered(tmp_path):
    result, proof = _proof_apply(tmp_path, answered_serial="A7II00SERIAL")
    assert result.returncode == 1
    assert proof["pass"] is False and proof["serial_mismatches"] == [1, 2]


@pytest.mark.parametrize("evidence", [None, {"settings_readback": {"after_final": "verified"}}])
def test_applied_proof_ignores_a_serial_outside_the_shot_evidence(tmp_path, evidence):
    # An echoed request field is not the body that answered: only evidence.camera_serial counts.
    response = {"camera_serial": "A7III0SERIAL", "request": {"camera_serial": "A7III0SERIAL"},
                "ok": True, "image": "shot_1.jpg", "evidence": evidence,
                "settings_readback": {"after_final": "verified"}}
    result, proof = _proof_apply(tmp_path, response=response)
    assert result.returncode == 1
    assert proof["pass"] is False and proof["serial_mismatches"] == [1, 2]
    assert [shot["camera_serial"] for shot in proof["shots"]] == [None, None]


def test_bringup_proof_dry_run_curl_command_parses_back_to_the_exact_body(tmp_path):
    result, _, _ = _bringup(tmp_path, "--phase", "proof", *PROOF_BODY, "--serial", "SERI'AL1")
    assert result.returncode == 0, result.stderr
    proof = _printed_receipts(result.stdout)["proof"]
    words = shlex.split(proof["curl_command"])
    assert json.loads(words[words.index("--data") + 1]) == proof["request_body"]
    assert proof["request_body"]["camera_serial"] == "SERI'AL1"


def test_control_characters_are_refused_in_paths(tmp_path, capsys):
    assert "control character" in safe_paths.refusal("/boot\n", REPO)
    assert "control character" in safe_paths.refusal(str(tmp_path / "r\x1b"), REPO)
    with pytest.raises(SystemExit):
        day_one.build_parser().parse_args(["autodetect", "--receipt-dir", str(tmp_path / "r\n")])
    with pytest.raises(SystemExit):
        day_one.build_parser().parse_args(["merge-thermal", "--receipt-dir", str(tmp_path), "a.jpg\n"])
    with pytest.raises(SystemExit):
        cm5_nano.main(["render", "--apply", "--out", str(tmp_path / "s\n")])
    assert not any(p.name.startswith(("r", "s")) for p in tmp_path.iterdir())


@pytest.mark.parametrize("flag", ["--receipt-dir", "--token-file", "--env-file", "--manifest"])
def test_bringup_refuses_control_characters_in_path_flags(tmp_path, flag):
    value = str(tmp_path / "x") + "\n"
    extra = [flag, value] if flag != "--receipt-dir" else []
    receipts = Path(value) if flag == "--receipt-dir" else tmp_path / "receipts"
    result, _, _ = _bringup(tmp_path, "--phase", "host-facts", *extra, mode="--apply", receipts=receipts)
    assert result.returncode == 2 and "control character" in result.stderr
    assert not (tmp_path / "x").exists() and not (tmp_path / "receipts").exists()
