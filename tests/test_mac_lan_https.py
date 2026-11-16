"""Pure-function and dry-run coverage for the Mac LAN HTTPS bootstrap tool.

No sockets, no subprocess, no caddy/mkcert execution: shutil.which is mocked and
detection is fed static text.
"""
import importlib.util
import os
import sys
from pathlib import Path

import pytest

_MODULE_PATH = Path(__file__).resolve().parents[1] / "deploy" / "mac_lan_https.py"
_spec = importlib.util.spec_from_file_location("mac_lan_https", _MODULE_PATH)
mac_lan_https = importlib.util.module_from_spec(_spec)
sys.modules["mac_lan_https"] = mac_lan_https
_spec.loader.exec_module(mac_lan_https)


def test_parse_ipconfig_addr_filters_loopback_and_junk():
    assert mac_lan_https.parse_ipconfig_addr("192.168.1.50\n") == "192.168.1.50"
    assert mac_lan_https.parse_ipconfig_addr("127.0.0.1") is None
    assert mac_lan_https.parse_ipconfig_addr("169.254.1.1") is None
    assert mac_lan_https.parse_ipconfig_addr("") is None
    assert mac_lan_https.parse_ipconfig_addr("not-an-ip") is None


def test_parse_ifconfig_ipv4_extracts_lan_only():
    text = """
en0: flags=8863<UP,BROADCAST> mtu 1500
	inet 127.0.0.1 netmask 0xff000000
	inet 192.168.1.50 netmask 0xffffff00 broadcast 192.168.1.255
en1: flags=8863<UP> mtu 1500
	inet 169.254.10.10 netmask 0xffff0000
	inet 10.0.0.4 netmask 0xffffff00
"""
    assert mac_lan_https.parse_ifconfig_ipv4(text) == ["192.168.1.50", "10.0.0.4"]


def test_select_ip_override_and_ambiguity():
    assert mac_lan_https.select_ip([], "192.168.1.9") == "192.168.1.9"
    assert mac_lan_https.select_ip(["192.168.1.9"]) == "192.168.1.9"
    with pytest.raises(mac_lan_https.DetectionError):
        mac_lan_https.select_ip([])
    with pytest.raises(mac_lan_https.DetectionError):
        mac_lan_https.select_ip(["192.168.1.9", "10.0.0.4"])
    with pytest.raises(mac_lan_https.DetectionError):
        mac_lan_https.select_ip([], "127.0.0.1")


def test_render_caddyfile_internal_variant():
    text = mac_lan_https.render_caddyfile("192.168.1.50", tls="internal")
    assert "https://192.168.1.50:8443 {" in text
    assert "bind 192.168.1.50" in text
    assert "tls internal" in text
    assert "reverse_proxy 127.0.0.1:8000" in text
    assert ".pem" not in text


def test_render_caddyfile_mkcert_variant_and_ports():
    text = mac_lan_https.render_caddyfile("10.0.0.4", tls="mkcert",
                                          backend_port=8001, https_port=9443)
    assert "https://10.0.0.4:9443 {" in text
    assert "tls 10.0.0.4.pem 10.0.0.4-key.pem" in text
    assert "reverse_proxy 127.0.0.1:8001" in text
    assert "tls internal" not in text


def test_render_caddyfile_rejects_unknown_tls():
    with pytest.raises(ValueError):
        mac_lan_https.render_caddyfile("192.168.1.50", tls="letsencrypt")


def test_render_env_line_uses_exact_cors_var():
    line = mac_lan_https.render_env_line("192.168.1.50")
    assert "CAMERA_AUTOPILOT_EXPOSURE=lan-proxy" in line
    assert "CAMERA_AUTOPILOT_TLS_PROXY_CONFIRMED=1" in line
    assert "CAMERA_AUTOPILOT_PUBLIC_HTTPS_URL=https://192.168.1.50:8443" in line
    assert "CAMERA_AUTOPILOT_CORS_ORIGINS=https://192.168.1.50:8443" in line
    assert line.endswith("CAMERA_ENGINE=real ./run.sh")


def test_resolve_tls_mode_auto_prefers_mkcert():
    assert mac_lan_https._resolve_tls_mode("auto", {"mkcert": "/x/mkcert"}) == "mkcert"
    assert mac_lan_https._resolve_tls_mode("auto", {"mkcert": None}) == "internal"
    assert mac_lan_https._resolve_tls_mode("internal", {"mkcert": "/x/mkcert"}) == "internal"


def test_write_caddyfile_writes_once_and_refuses_system_paths(tmp_path):
    target = tmp_path / "proxy"
    destination = mac_lan_https.write_caddyfile(target, "config\n")
    assert destination.read_text() == "config\n"
    assert oct(destination.stat().st_mode & 0o777) == "0o600"
    # Exclusive create: a second write refuses rather than clobbering.
    with pytest.raises(FileExistsError):
        mac_lan_https.write_caddyfile(target, "other\n")
    with pytest.raises(ValueError):
        mac_lan_https.write_caddyfile(Path("/etc/mac_lan_https_test"), "config\n")


def test_cli_no_flags_writes_nothing(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(mac_lan_https, "detect_lan_candidates", lambda *a, **k: ["192.168.1.50"])
    monkeypatch.setattr(mac_lan_https.shutil, "which", lambda name: None)
    monkeypatch.chdir(tmp_path)
    assert mac_lan_https.main([]) == 0
    assert list(tmp_path.iterdir()) == []  # dry run leaves the cwd untouched
    out = capsys.readouterr().out
    assert "192.168.1.50" in out
    assert "brew install caddy mkcert nss" in out
    assert "Nothing was written or started" in out


def test_cli_write_creates_caddyfile(tmp_path, monkeypatch):
    monkeypatch.setattr(mac_lan_https, "detect_lan_candidates", lambda *a, **k: [])
    monkeypatch.setattr(mac_lan_https.shutil, "which",
                        lambda name: f"/usr/local/bin/{name}")
    monkeypatch.setattr(mac_lan_https, "mkcert_caroot", lambda: "/Users/x/Library/mkcert")
    out_dir = tmp_path / "cfg"
    assert mac_lan_https.main(["--ip", "192.168.1.77", "--write", str(out_dir)]) == 0
    written = (out_dir / "Caddyfile").read_text()
    assert "https://192.168.1.77:8443 {" in written
    assert "tls 192.168.1.77.pem 192.168.1.77-key.pem" in written  # mkcert present -> auto


def test_cli_run_without_write_is_rejected(monkeypatch, capsys):
    monkeypatch.setattr(mac_lan_https, "detect_lan_candidates", lambda *a, **k: ["192.168.1.50"])
    monkeypatch.setattr(mac_lan_https.shutil, "which", lambda name: None)
    assert mac_lan_https.main(["--run"]) == 2
    assert "requires --write" in capsys.readouterr().err


def test_runbook_starts_the_existing_caddyfile_without_rewriting(tmp_path, monkeypatch):
    """CAM-07: execute the documented write/start sequence with Caddy replaced."""
    import shlex
    doc = (_MODULE_PATH.parents[1] / "docs/HUMAN_GATED_RECEIPTS_2026-09-07.md").read_text()
    section = doc.split("1. Render (and inspect)", 1)[1].split("2. Start the backend", 1)[0]
    commands = [shlex.split(line.strip()) for line in section.splitlines()
                if line.strip().startswith((".venv/bin/python", "caddy run"))]
    monkeypatch.setattr(mac_lan_https, "detect_lan_candidates", lambda: ["192.0.2.10"])
    monkeypatch.setattr(mac_lan_https, "check_tools", lambda: {"caddy": "/fake/caddy", "mkcert": None})
    executions = []
    monkeypatch.setattr(mac_lan_https.os, "execvp", lambda *args: executions.append(args))
    target = tmp_path / "proxy"
    for command in commands:
        command = [value.replace("/tmp/camera-lan-https", str(target)) for value in command]
        if command[0] == "caddy":
            config = Path(command[command.index("--config") + 1])
            assert config == target / "Caddyfile" and config.is_file()
            executions.append(("caddy", command))
        else:
            assert mac_lan_https.main(command[2:]) == 0
    assert len(executions) == 1
    assert executions[0][1] == ["caddy", "run", "--config", str(target / "Caddyfile")]


def test_rendered_guidance_starts_saved_config_directly():
    report = mac_lan_https._build_report("192.0.2.10", "internal", 8000, 8443,
                                        {"caddy": None, "mkcert": None}, None)
    assert "caddy run --config <dir>/Caddyfile" in report
    assert "then --write <dir> --run" not in report
