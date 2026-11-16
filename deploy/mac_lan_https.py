"""Bootstrap a Mac-local HTTPS reverse proxy so a phone on the same Wi-Fi can
reach the autopilot over TLS.

Dry-run by default: this renders the exact Caddyfile, the run.sh environment
line, and the iOS trust steps, and reports which host tools are missing WITHOUT
installing anything or changing any system state. It never edits system paths,
never runs mkcert, and only touches the filesystem with `--write` (writes the
Caddyfile into a directory you name) or the network with `--run` (execs
`caddy run` in the foreground on a Caddyfile you already wrote).

The proxy terminates TLS on the LAN address and forwards to the loopback
autopilot exactly as docs/SECURITY_PRIVACY_RELEASE.md requires; it does not
prove the phone connected, trusts the CA, or that reconnect works.
"""
from __future__ import annotations

import argparse
import ipaddress
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

# run.sh binds the app on 127.0.0.1:CAMERA_AUTOPILOT_PORT (default 8000); the
# proxy forwards there. Both ports stay overridable on the command line.
DEFAULT_BACKEND_PORT = 8000
DEFAULT_HTTPS_PORT = 8443
CORS_ENV_VAR = "CAMERA_AUTOPILOT_CORS_ORIGINS"
# The proxy must never overwrite anything under a shared system location.
SYSTEM_PREFIXES = ("/etc", "/System", "/usr", "/bin", "/sbin",
                   "/Library", "/Applications", "/private/etc")


class DetectionError(ValueError):
    """Raised when the LAN IPv4 cannot be resolved unambiguously."""


def _is_lan_ipv4(candidate: str) -> bool:
    try:
        address = ipaddress.IPv4Address(candidate)
    except ipaddress.AddressValueError:
        return False
    return not (address.is_loopback or address.is_link_local
                or address.is_multicast or address.is_unspecified)


def parse_ipconfig_addr(text: str) -> str | None:
    """Parse one `ipconfig getifaddr <iface>` line (a bare address or empty)."""
    candidate = text.strip()
    return candidate if _is_lan_ipv4(candidate) else None


def parse_ifconfig_ipv4(text: str) -> list[str]:
    """Extract LAN IPv4 addresses from `ifconfig` output, order-preserving."""
    found: list[str] = []
    for match in re.findall(r"\binet (\d+\.\d+\.\d+\.\d+)", text):
        if _is_lan_ipv4(match) and match not in found:
            found.append(match)
    return found


def detect_lan_candidates(interfaces=("en0", "en1")) -> list[str]:
    """Best-effort local detection; never raises, returns de-duplicated hits."""
    found: list[str] = []
    for interface in interfaces:
        try:
            result = subprocess.run(["ipconfig", "getifaddr", interface],
                                    capture_output=True, text=True, timeout=5)
        except (OSError, subprocess.SubprocessError):
            continue
        address = parse_ipconfig_addr(result.stdout)
        if address and address not in found:
            found.append(address)
    if not found:
        try:
            result = subprocess.run(["ifconfig"], capture_output=True, text=True, timeout=5)
            for address in parse_ifconfig_ipv4(result.stdout):
                if address not in found:
                    found.append(address)
        except (OSError, subprocess.SubprocessError):
            pass
    return found


def select_ip(candidates, override: str | None = None) -> str:
    """Resolve to exactly one LAN IPv4, honouring an explicit override."""
    if override is not None:
        if not _is_lan_ipv4(override):
            raise DetectionError(f"--ip {override!r} is not a usable LAN IPv4 address")
        return override
    unique = list(dict.fromkeys(candidates))
    if not unique:
        raise DetectionError("no LAN IPv4 detected on en0/en1; pass --ip <address>")
    if len(unique) > 1:
        joined = ", ".join(unique)
        raise DetectionError(f"multiple LAN IPv4 candidates ({joined}); pass --ip to choose")
    return unique[0]


def mkcert_cert_names(ip: str) -> tuple[str, str]:
    """File names `mkcert <ip>` produces in the current directory."""
    return f"{ip}.pem", f"{ip}-key.pem"


def render_caddyfile(ip: str, *, backend_port: int = DEFAULT_BACKEND_PORT,
                     https_port: int = DEFAULT_HTTPS_PORT, tls: str = "internal",
                     cert_path: str | None = None, key_path: str | None = None) -> str:
    """Render a Caddyfile that terminates TLS on the LAN IP and forwards to loopback."""
    site = f"https://{ip}:{https_port}"
    upstream = f"127.0.0.1:{backend_port}"
    if tls == "mkcert":
        if not cert_path or not key_path:
            cert_path, key_path = mkcert_cert_names(ip)
        tls_line = f"    tls {cert_path} {key_path}"
        header = "# TLS via a mkcert-issued certificate; trust its root CA on every client first."
    elif tls == "internal":
        tls_line = "    tls internal"
        header = "# TLS via Caddy's internal CA; trust that CA on every client first."
    else:
        raise ValueError("tls must be 'mkcert' or 'internal'")
    return (f"{header}\n"
            "{\n"
            "    # No port-80 redirect listener: it needs root and the phone only ever uses the HTTPS URL.\n"
            "    auto_https disable_redirects\n"
            "}\n"
            f"{site} {{\n"
            f"    bind {ip}\n"
            f"{tls_line}\n"
            f"    reverse_proxy {upstream}\n"
            f"}}\n")


def render_env_line(ip: str, *, https_port: int = DEFAULT_HTTPS_PORT) -> str:
    """The exact one-line run.sh invocation for LAN HTTPS mode."""
    url = f"https://{ip}:{https_port}"
    return (f"CAMERA_AUTOPILOT_EXPOSURE=lan-proxy CAMERA_AUTOPILOT_TLS_PROXY_CONFIRMED=1 "
            f"CAMERA_AUTOPILOT_PUBLIC_HTTPS_URL={url} {CORS_ENV_VAR}={url} "
            f"CAMERA_ENGINE=real ./run.sh")


def check_tools() -> dict[str, str | None]:
    return {tool: shutil.which(tool) for tool in ("caddy", "mkcert")}


def mkcert_caroot() -> str | None:
    if shutil.which("mkcert") is None:
        return None
    try:
        result = subprocess.run(["mkcert", "-CAROOT"], capture_output=True, text=True, timeout=5)
    except (OSError, subprocess.SubprocessError):
        return None
    root = result.stdout.strip()
    return root or None


def ios_trust_steps(caroot: str | None) -> str:
    root = caroot or "$(mkcert -CAROOT)"
    return "\n".join([
        f"1. On this Mac, run `mkcert -install`, then find rootCA.pem in {root}.",
        "2. Send that rootCA.pem to the phone via AirDrop or Mail (never a web link).",
        "3. iOS: Settings > General > VPN & Device Management > install the profile.",
        "4. iOS: Settings > General > About > Certificate Trust Settings > enable full "
        "trust for the mkcert root CA.",
    ])


def _resolve_tls_mode(requested: str, tools: dict[str, str | None]) -> str:
    if requested in ("mkcert", "internal"):
        return requested
    # auto: prefer a real mkcert certificate when the tool is available.
    return "mkcert" if tools.get("mkcert") else "internal"


def _reject_system_path(directory: Path) -> None:
    resolved = str(directory.resolve())
    for prefix in SYSTEM_PREFIXES:
        if resolved == prefix or resolved.startswith(prefix + "/"):
            raise ValueError(f"refusing to write inside a system location: {resolved}")


def write_caddyfile(directory: Path, text: str) -> Path:
    _reject_system_path(directory)
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    destination = directory / "Caddyfile"
    descriptor = os.open(destination, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "w") as stream:
        stream.write(text)
    return destination


def _build_report(ip: str, tls_mode: str, backend_port: int, https_port: int,
                  tools: dict[str, str | None], caroot: str | None) -> str:
    caddyfile = render_caddyfile(ip, backend_port=backend_port, https_port=https_port,
                                 tls=tls_mode)
    lines = [
        f"LAN IPv4 selected: {ip}",
        f"TLS mode: {tls_mode}   backend: 127.0.0.1:{backend_port}   listen: {ip}:{https_port}",
        "",
        "Host tools:",
        f"  caddy:  {tools['caddy'] or 'NOT INSTALLED'}",
        f"  mkcert: {tools['mkcert'] or 'NOT INSTALLED'}",
    ]
    if not tools["caddy"] or not tools["mkcert"]:
        lines.append("  install with: brew install caddy mkcert nss")
    if tls_mode == "mkcert":
        cert, key = mkcert_cert_names(ip)
        lines += ["", f"Issue the certificate (writes {cert} and {key} to the current dir):",
                  f"  mkcert -install   # one time, needs your confirmation",
                  f"  mkcert {ip}"]
    lines += ["", "Caddyfile:", caddyfile.rstrip("\n"),
              "", "run.sh environment line:", render_env_line(ip, https_port=https_port),
              "", "Trust the CA on the phone:", ios_trust_steps(caroot),
              "", "Nothing was written or started. Re-run with --write <dir> to save the "
              "Caddyfile, then caddy run --config <dir>/Caddyfile to serve it. "
              "The helper refuses to overwrite an existing Caddyfile."]
    return "\n".join(lines)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Render (dry-run) a Mac LAN HTTPS proxy config.")
    parser.add_argument("--ip", help="LAN IPv4 to bind (skips auto-detection)")
    parser.add_argument("--backend-port", type=int, default=DEFAULT_BACKEND_PORT,
                        help=f"loopback app port to forward to (default {DEFAULT_BACKEND_PORT})")
    parser.add_argument("--https-port", type=int, default=DEFAULT_HTTPS_PORT,
                        help=f"LAN HTTPS port to listen on (default {DEFAULT_HTTPS_PORT})")
    parser.add_argument("--tls", choices=("auto", "mkcert", "internal"), default="auto",
                        help="certificate strategy (default auto: mkcert if installed)")
    parser.add_argument("--write", metavar="DIR",
                        help="write the Caddyfile into DIR (the only filesystem side effect)")
    parser.add_argument("--run", action="store_true",
                        help="exec `caddy run` in the foreground on the written Caddyfile")
    args = parser.parse_args(argv)

    tools = check_tools()
    try:
        ip = select_ip(detect_lan_candidates() if args.ip is None else [], args.ip)
    except DetectionError as error:
        print(f"error: {error}", file=sys.stderr)
        return 2
    tls_mode = _resolve_tls_mode(args.tls, tools)
    caroot = mkcert_caroot() if tls_mode == "mkcert" else None

    if args.run and not args.write:
        print("error: --run requires --write <dir> so the Caddyfile exists on disk first",
              file=sys.stderr)
        return 2

    print(_build_report(ip, tls_mode, args.backend_port, args.https_port, tools, caroot))

    if args.write:
        caddyfile = render_caddyfile(ip, backend_port=args.backend_port,
                                     https_port=args.https_port, tls=tls_mode)
        try:
            destination = write_caddyfile(Path(args.write), caddyfile)
        except (OSError, ValueError) as error:
            print(f"error: could not write Caddyfile: {error}", file=sys.stderr)
            return 2
        print(f"\nWrote {destination}")
        if args.run:
            if not tools["caddy"]:
                print("error: caddy is not installed; run `brew install caddy` first",
                      file=sys.stderr)
                return 2
            print(f"Starting: caddy run --config {destination}")
            os.execvp("caddy", ["caddy", "run", "--config", str(destination)])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
