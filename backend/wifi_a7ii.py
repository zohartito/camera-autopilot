"""Legacy Sony Camera Remote API client for the A7 II — the WiFi path to AI focus-point.

VERIFIED LIVE (2026-06-21): with Smart Remote Control v4.30 side-loaded (the built-in "Embedded" stub
only exposes shoot/EV/timer), setTouchAFPosition(50,50) and (25,45) both returned AFResult:true — the
camera focuses at a commanded (x%, y%). After begin_session() the API exposes ~73 methods incl. the
exposure setters, liveview, and actTakePicture. The patent-core focus-point feature works on the A7 II.

USB-PTP is no longer a clean "can't": raw Sony PTP probes show this A7 II accepts focus-area,
AF-position, and manual-focus control writes, and a USB bridge can expose those calls over HTTP. What
is not yet proven is Smart-Remote-equivalent point-specific A/B focus behavior over USB. Sony's legacy
Camera Remote API — HTTP/JSON-RPC over the camera's own WiFi AP, served by the on-camera "Smart
Remote" app — remains the proven focus-point path because it exposes **setTouchAFPosition(x%, y%)**,
the SAME call the Arsenal device uses to focus the A7 II. It's an all-WiFi transport: exposure
(setFNumber / setShutterSpeed / setIsoSpeedRate) AND focus (setTouchAFPosition) over one channel.
Sony's API matrix marks "Touch AF position: Y" for the ILCE-7M2; this client confirms it live via
getAvailableApiList.

Flow: launch "Smart Remote" on the camera (it becomes a WiFi AP) -> join that AP -> SSDP-discover
the ScalarWebAPI endpoint -> startRecMode -> getAvailableApiList -> drive focus + exposure.

NOTE: the controller must be ON the camera's WiFi AP (which has no internet). Run this from a host
that can reach the camera's subnet — it needs no internet itself.

    python3 backend/wifi_a7ii.py        # discover + connect + print the live API list (the verify)
"""
from __future__ import annotations

import json
import ipaddress
import os
import re
import socket
import time
import urllib.parse
import urllib.request
from pathlib import Path
from dataclasses import dataclass
from defusedxml.ElementTree import fromstring

from security_bounds import json_response, read_bounded
from image_safety import MAX_IMAGE_SOURCE_BYTES, image_size_from_bytes

_SSDP_ADDR = ("239.255.255.250", 1900)
_SSDP_ST = "urn:schemas-sony-com:service:ScalarWebAPI:1"
_TOUCH_AF_ENV = "CAMERA_AUTOPILOT_ENABLE_TOUCH_AF"
_SAFE_FILENAME_RE = re.compile(r"[^A-Za-z0-9_.-]+")
_RAW_SUFFIXES = {".arw", ".raw", ".dng", ".nef", ".cr2", ".cr3", ".rw2", ".orf", ".raf"}
DEFAULT_CAMERA_ENDPOINT = "http://192.168.122.1:8080/sony/camera"
ENDPOINT_ENV = "SONY_CAMERA_REMOTE_ENDPOINT"
DEVICE_DESCRIPTION_ENV = "SONY_CAMERA_REMOTE_DD_URL"
GENERIC_SONY_MODEL_NAME = "SonyImagingDevice"
MAX_CAMERA_RESPONSE_BYTES = 1 * 1024 * 1024
MAX_LIVEVIEW_FRAME_BYTES = 25 * 1024 * 1024
MAX_CAMERA_DOWNLOAD_BYTES = MAX_IMAGE_SOURCE_BYTES


class CameraIdentityError(ValueError):
    """The endpoint has not established the exact camera model this driver supports."""


class CameraSettingError(RuntimeError):
    """An exposure write was rejected or its requested value could not be verified."""

    verified = False


class SettingUnsupported(RuntimeError):
    """The camera advertises no control for this setting on the current route.

    Distinct from CameraSettingError: the setting was never attempted because the
    body offered no available values, so the caller may legitimately skip it (and
    record that the intent was NOT applied) rather than fail the whole capture.
    """


def _parse_white_balance(values: list):
    """Pull (mode, kelvin) out of a getWhiteBalance result array.

    The A7 II reports either a flat [mode, tempEnabled, kelvin] or a
    [{whiteBalanceMode|currentWhiteBalanceMode, colorTemperature|currentColorTemperature}]
    wrapper; both are handled. Returns (mode_or_None, kelvin_or_None).
    """
    if not values:
        return None, None
    head = values[0]
    if isinstance(head, dict):
        mode = head.get("whiteBalanceMode", head.get("currentWhiteBalanceMode"))
        kelvin = head.get("colorTemperature", head.get("currentColorTemperature"))
        return mode, kelvin
    # flat [mode, tempEnabled, kelvin]
    kelvin = values[2] if len(values) >= 3 else None
    return head, kelvin


def _kelvin_matches(got_temp, color_temp: int) -> bool:
    try:
        return int(got_temp) == int(color_temp)
    except (TypeError, ValueError):
        return False


@dataclass(frozen=True)
class CameraIdentity:
    endpoint: str
    model_name: str
    udn: str
    description_url: str


_DISCOVERED_IDENTITIES: dict[str, CameraIdentity] = {}


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: D401, ANN001
        return None


_CAMERA_OPENER = urllib.request.build_opener(_NoRedirect)


def _camera_urlopen(url: str, *, data: bytes | None = None, timeout: float = 10.0):
    return _CAMERA_OPENER.open(url, data=data, timeout=timeout)


def _validated_http_url(url: str, *, context: str) -> str:
    parsed = urllib.parse.urlsplit(str(url).strip())
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise ValueError(f"{context} must be an absolute http(s) URL")
    if parsed.username or parsed.password:
        raise ValueError(f"{context} must not include credentials")
    try:
        address = ipaddress.ip_address(parsed.hostname)
    except ValueError:
        address = None
    if address is not None and (address.is_loopback or address.is_multicast or address.is_unspecified):
        raise ValueError(f"{context} has an unsafe camera address")
    return urllib.parse.urlunsplit((parsed.scheme, parsed.netloc, parsed.path, parsed.query, ""))


def _same_camera_host(left: str, right: str) -> bool:
    a = urllib.parse.urlsplit(left)
    b = urllib.parse.urlsplit(right)
    return (
        a.scheme.lower() == b.scheme.lower()
        and (a.hostname or "").lower() == (b.hostname or "").lower()
    )


def _camera_url(url: str, base_url: str, *, context: str) -> str:
    candidate = _validated_http_url(urllib.parse.urljoin(base_url, str(url).strip()), context=context)
    base = _validated_http_url(base_url, context="camera endpoint")
    if not _same_camera_host(candidate, base):
        raise ValueError(f"{context} must stay on the discovered camera host")
    return candidate


def _safe_camera_filename(name: str | None, fallback: str = "download") -> str:
    raw = str(name or "").replace("\x00", "")
    base = raw.replace("\\", "/").rsplit("/", 1)[-1]
    safe = _SAFE_FILENAME_RE.sub("_", base).strip("._")
    return safe or fallback


def _contained_transfer_path(out_dir, name: str):
    from pathlib import Path as _Path
    root = _Path(out_dir).resolve()
    dest = (root / _safe_camera_filename(name)).resolve()
    try:
        dest.relative_to(root)
    except ValueError as exc:
        raise ValueError("camera filename escapes transfer directory") from exc
    return dest


def touch_af_enabled() -> bool:
    """Shipping/legal gate for the focus-drive actuator; live tests opt in with env=1."""
    value = os.getenv(_TOUCH_AF_ENV, "").strip().lower()
    return value in {"1", "true", "yes", "on"}


def discover(timeout_s: float = 3.0) -> str | None:
    """SSDP M-SEARCH for the camera's device-description URL (the LOCATION header)."""
    msg = ("M-SEARCH * HTTP/1.1\r\n"
           f"HOST: {_SSDP_ADDR[0]}:{_SSDP_ADDR[1]}\r\n"
           'MAN: "ssdp:discover"\r\n'
           f"ST: {_SSDP_ST}\r\nMX: 1\r\n\r\n").encode()
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock.settimeout(timeout_s)
    try:
        sock.sendto(msg, _SSDP_ADDR)
        while True:
            data, peer = sock.recvfrom(2048)
            for line in data.decode(errors="ignore").splitlines():
                if line.lower().startswith("location:"):
                    location = line.split(":", 1)[1].strip()
                    try:
                        candidate = _validated_http_url(location, context="SSDP LOCATION")
                        if urllib.parse.urlsplit(candidate).hostname != peer[0]:
                            continue
                        return candidate
                    except ValueError:
                        continue
    except socket.timeout:
        return None
    finally:
        sock.close()


def identity_from_dd(dd_url: str) -> CameraIdentity | None:
    """Read bounded device identity and its same-host Sony camera service together."""
    dd_url = _validated_http_url(dd_url, context="device description URL")
    xml = read_bounded(_camera_urlopen(dd_url, timeout=5), limit=MAX_CAMERA_RESPONSE_BYTES)
    root = fromstring(xml)
    names = {node.tag.rsplit("}", 1)[-1]: (node.text or "").strip() for node in root.iter()}
    for service in root.iter():
        if service.tag.rsplit("}", 1)[-1] != "X_ScalarWebAPI_Service":
            continue
        values = {node.tag.rsplit("}", 1)[-1]: (node.text or "").strip() for node in service.iter()}
        if values.get("X_ScalarWebAPI_ServiceType") != "camera":
            continue
        url = values.get("X_ScalarWebAPI_ActionList_URL")
        if url:
            endpoint = _camera_url(url, dd_url, context="ActionList_URL").rstrip("/")
            if not endpoint.endswith("/camera"):
                endpoint += "/camera"
            # Sony's description carries the generic "SonyImagingDevice" as modelName and the real
            # body name ("ILCE-7M2") as friendlyName; the identity gate needs the body name.
            model_name = names.get("modelName", "")
            if model_name in ("", GENERIC_SONY_MODEL_NAME):
                model_name = names.get("friendlyName", "") or model_name
            identity = CameraIdentity(endpoint, model_name, names.get("UDN", ""), dd_url)
            _DISCOVERED_IDENTITIES[endpoint] = identity
            return identity
    return None


def endpoint_from_dd(dd_url: str) -> str | None:
    """Compatibility endpoint lookup; retain modelName/UDN for the constructed client."""
    identity = identity_from_dd(dd_url)
    return identity.endpoint if identity else None


def endpoint_from_device_description(url: str, timeout: float = 5.0) -> str | None:
    """Compatibility wrapper for the settings-route client API.

    `endpoint_from_dd` owns URL validation and same-origin enforcement. The
    timeout argument remains part of the public surface for callers that used
    the settings-only client; Sony device descriptions are still fetched with
    the transport's bounded timeout.
    """
    del timeout
    return endpoint_from_dd(url)


class CameraRemote:
    """Thin JSON-RPC client for the Camera Remote API 'camera' service."""

    def __init__(self, endpoint: str, *, device_description_url: str | None = None):
        self.endpoint = _validated_http_url(endpoint, context="camera endpoint")
        self._id = 0
        self.identity = _DISCOVERED_IDENTITIES.get(self.endpoint)
        self.device_description_url = (device_description_url or os.getenv(DEVICE_DESCRIPTION_ENV)
                                       or (self.identity.description_url if self.identity else None))
        self._identity_verified = False

    def verify_identity(self, *, refresh: bool = False) -> CameraIdentity:
        """Fail closed before writing: only a same-host ILCE-7M2 description is accepted."""
        if self._identity_verified and not refresh:
            return self.identity
        self._identity_verified = False
        description = self.device_description_url or discover()
        if not description:
            raise CameraIdentityError("Camera identity unknown: provide SONY_CAMERA_REMOTE_DD_URL "
                                      "or discover a same-host device description; no camera command sent")
        description = _camera_url(description, self.endpoint, context="device description URL")
        identity = identity_from_dd(description)
        if identity is None or identity.endpoint != self.endpoint:
            raise CameraIdentityError("Device description does not identify this camera endpoint")
        if self.identity and self.identity.udn and self.identity.udn != identity.udn:
            raise CameraIdentityError("Camera UDN changed during this client session; no camera command sent")
        self.identity = identity
        self.device_description_url = identity.description_url
        if identity.model_name != "ILCE-7M2" or not identity.udn.startswith("uuid:") or len(identity.udn) <= 5:
            raise CameraIdentityError(f"Smart Remote requires ILCE-7M2 with a device UDN; "
                                      f"reported model is {identity.model_name or 'unknown'!r}. No camera command sent")
        self._identity_verified = True
        return identity

    def camera_url(self, url: str, *, context: str = "camera URL") -> str:
        return _camera_url(url, self.endpoint, context=context)

    def call(self, method: str, params=None, version: str = "1.0", timeout: float = 10.0) -> dict:
        if not method.startswith("get"):
            self.verify_identity(refresh=method in {"startRecMode", "actTakePicture"})
        self._id += 1
        body = json.dumps({"method": method, "params": params or [], "id": self._id,
                           "version": version}).encode()
        return json_response(
            _camera_urlopen(self.endpoint, data=body, timeout=timeout),
            limit=MAX_CAMERA_RESPONSE_BYTES,
        )

    def start_rec_mode(self) -> None:
        self.call("startRecMode")

    def available_apis(self) -> list[str]:
        return self.call("getAvailableApiList").get("result", [[]])[0]

    def start_liveview(self) -> str | None:
        """Start the liveview stream (returns its URL). REQUIRED before touch_af + the exposure
        setters appear — the API gates them behind an active liveview (verified live)."""
        url = self.call("startLiveview").get("result", [None])[0]
        return self.camera_url(url, context="liveview URL") if url else None

    def set_focus_mode(self, mode: str) -> None:
        self.call("setFocusMode", [mode])

    def begin_session(self) -> list[str]:
        """The PROVEN unlock sequence (verified live on the A7 II, Smart Remote Control v4.30):
        startRecMode -> startLiveview, after which setTouchAFPosition + setFNumber/setShutterSpeed/
        setIsoSpeedRate become available. Returns the now-available API list (~73 methods)."""
        self.start_rec_mode()
        self.start_liveview()
        return self.available_apis()

    def safe_capability_methods(self) -> list[str]:
        """Return discovered getters that are safe to call during an API dump."""
        return sorted(
            api
            for api in self.available_apis()
            if api.startswith("getAvailable") or api.startswith("getSupported")
        )

    def setter_candidates(self) -> list[str]:
        """Return discovered setters without claiming their behavior is proven."""
        return sorted(api for api in self.available_apis() if api.startswith("set"))

    def call_safe_capability_methods(self, timeout: float = 3.0) -> dict[str, dict]:
        """Call safe discovered getters and preserve successes and errors as evidence."""
        results: dict[str, dict] = {}
        for method in self.safe_capability_methods():
            try:
                results[method] = {"ok": True, "response": self.call(method, timeout=timeout)}
            except Exception as exc:  # evidence collection must retain individual failures
                results[method] = {
                    "ok": False,
                    "error_type": exc.__class__.__name__,
                    "error": str(exc),
                }
        return results

    def grab_liveview_frame(self, stream_url: str | None = None, save_path: str | None = None) -> bytes:
        """Pull ONE JPEG frame off the liveview stream — NO shutter. Sony format (verified live):
        8-byte common header + 128-byte payload header (bytes 4-6 = JPEG size, big-endian) + the JPEG.
        This is the metering + subject-detection frame source for the autopilot loop."""
        url = stream_url or self.start_liveview()
        if not url:
            raise OSError("camera did not return a liveview URL")
        url = self.camera_url(url, context="liveview stream URL")
        stream = _camera_urlopen(url, timeout=10)

        def read_exact(n: int) -> bytes:
            buf = b""
            while len(buf) < n:
                chunk = stream.read(n - len(buf))
                if not chunk:
                    raise OSError("liveview stream ended before a full frame")
                buf += chunk
            return buf

        try:
            read_exact(8)                                 # common header
            jpeg_size = int.from_bytes(read_exact(128)[4:7], "big")  # payload header
            if not 0 < jpeg_size <= MAX_LIVEVIEW_FRAME_BYTES:
                raise ValueError("liveview frame exceeds byte limit")
            buf = read_exact(jpeg_size)
        finally:
            stream.close()
        # Validate dimensions and source mode before any caller persists or decodes it.
        image_size_from_bytes(buf)
        if save_path:
            with open(save_path, "wb") as handle:
                handle.write(buf)
        return buf

    # --- focus (the patent-core: vision (x,y) -> the camera focuses there) -----------------
    def touch_af(self, x_pct: float, y_pct: float) -> bool:
        """Focus at (x_pct, y_pct), each 0-100 of the LIVEVIEW frame, origin top-left.
        Returns True when the camera reports AF locked at that point."""
        if not touch_af_enabled():
            return False
        result = self.call("setTouchAFPosition", [x_pct, y_pct]).get("result", [None, {}])
        return bool(result[1].get("AFResult", False)) if len(result) > 1 else False

    def cancel_touch_af(self) -> None:
        if not touch_af_enabled():
            return
        self.call("cancelTouchAFPosition")

    def half_press(self) -> None:
        """Half-press the shutter — drives AF (and the AF-assist lamp in low light) at the current
        touch-AF point. Often locks where setTouchAFPosition alone reports AFResult:false."""
        self.call("actHalfPressShutter")

    def cancel_half_press(self) -> None:
        self.call("cancelHalfPressShutter")

    def focus_status(self) -> str:
        """Live focus state from getEvent: 'Focused' / 'Focusing' / 'Not Focusing' / 'Failed' / '?'."""
        for block in self.call("getEvent", [False]).get("result", []):
            if isinstance(block, dict) and block.get("type") == "focusStatus":
                return block.get("focusStatus", "?")
        return "?"

    # --- exposure (so the physics brain can drive the camera over the same WiFi link) -------
    def _set_exposure_verified(self, setter: str, getter: str, value: str) -> str:
        """Issue one setting write and require its current-value getter to confirm it.

        Available-choice lists and a successful setter acknowledgement alone are not proof.
        Missing/unsupported getters leave the write unverified; do not silently report success.
        """
        target = str(value)
        response = self.call(setter, [target])
        acknowledgement = response.get("result") if isinstance(response, dict) else None
        if (not isinstance(response, dict) or "error" in response
                or not isinstance(acknowledgement, list) or len(acknowledgement) != 1
                or type(acknowledgement[0]) is not int or acknowledgement[0] != 0):
            raise CameraSettingError(f"{setter} rejected or did not acknowledge {target!r}; not verified")
        deadline = time.monotonic() + 2.0
        current = None
        for attempt in range(20):
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            try:
                readback = self.call(getter, timeout=min(1.0, remaining))
            except Exception as exc:
                raise CameraSettingError(f"{setter} wrote {target!r}, but {getter} is unavailable; not verified") from exc
            values = readback.get("result") if isinstance(readback, dict) else None
            if (not isinstance(readback, dict) or "error" in readback or not isinstance(values, list)
                    or len(values) != 1 or not isinstance(values[0], (str, int, float))
                    or isinstance(values[0], bool)):
                raise CameraSettingError(f"{getter} did not return a supported current value; {setter} not verified")
            current = str(values[0])
            if current == target:
                return current
            if attempt < 19:
                time.sleep(min(0.1, max(0.0, deadline - time.monotonic())))
        raise CameraSettingError(f"{setter} requested {target!r}, but {getter} stayed at {current!r}; not verified")

    def set_iso(self, value: str) -> str:
        return self._set_exposure_verified("setIsoSpeedRate", "getIsoSpeedRate", value)

    def set_aperture(self, value: str) -> str:
        return self._set_exposure_verified("setFNumber", "getFNumber", value)

    def set_shutter(self, value: str) -> str:
        return self._set_exposure_verified("setShutterSpeed", "getShutterSpeed", value)

    def _verify_current(self, getter: str, matches, describe: str) -> object:
        """Poll `getter` until its current value satisfies `matches(value)`.

        Shared readback loop for the non-scalar setters (WB, exposure comp) whose
        confirm shape differs from the ISO/F/shutter path. Raises CameraSettingError
        if the getter is unavailable, returns an unsupported shape, or never agrees.
        """
        deadline = time.monotonic() + 2.0
        current = None
        for attempt in range(20):
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            try:
                readback = self.call(getter, timeout=min(1.0, remaining))
            except Exception as exc:
                raise CameraSettingError(f"{describe} wrote, but {getter} is unavailable; not verified") from exc
            values = readback.get("result") if isinstance(readback, dict) else None
            if not isinstance(readback, dict) or "error" in readback or not isinstance(values, list) or not values:
                raise CameraSettingError(f"{getter} did not return a supported current value; {describe} not verified")
            current = values[0]
            if matches(current):
                return current
            if attempt < 19:
                time.sleep(min(0.1, max(0.0, deadline - time.monotonic())))
        raise CameraSettingError(f"{describe} requested, but {getter} stayed at {current!r}; not verified")

    def set_white_balance(self, mode: str, kelvin: int | None = None) -> object:
        """Set white balance and require getWhiteBalance to confirm it.

        `mode` is a Sony white-balance mode string ("Auto WB", "Color Temperature",
        "Daylight", ...). Only "Color Temperature" carries a kelvin value; every other
        mode ignores `kelvin` and writes the Sony not-applicable sentinel. When
        getAvailableWhiteBalance advertises a mode list that excludes `mode`, raises
        SettingUnsupported (skippable). A rejected write or a disagreeing readback
        raises CameraSettingError.
        """
        temp_enabled = mode == "Color Temperature"
        if temp_enabled and not kelvin:
            raise CameraSettingError("Color Temperature white balance requires a kelvin value; not verified")
        color_temp = int(kelvin) if temp_enabled else -1

        advertised = self._available_white_balance_modes()
        if advertised and mode not in advertised:
            raise SettingUnsupported(f"setWhiteBalance mode {mode!r} not offered by this body ({advertised})")

        response = self.call("setWhiteBalance", [mode, temp_enabled, color_temp])
        if (not isinstance(response, dict) or "error" in response
                or not isinstance(response.get("result"), list)):
            raise CameraSettingError(f"setWhiteBalance rejected {[mode, temp_enabled, color_temp]!r}; not verified")

        # getWhiteBalance is confirmed against the WHOLE result array, not just result[0]: the A7 II
        # returns either a flat [mode, tempEnabled, kelvin] or a [{whiteBalanceMode, colorTemperature}]
        # wrapper, and the kelvin lives beyond index 0 in the flat shape.
        deadline = time.monotonic() + 2.0
        current = None
        for attempt in range(20):
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            try:
                readback = self.call("getWhiteBalance", timeout=min(1.0, remaining))
            except Exception as exc:
                raise CameraSettingError(f"setWhiteBalance {mode!r} wrote, but getWhiteBalance is unavailable; not verified") from exc
            values = readback.get("result") if isinstance(readback, dict) else None
            if not isinstance(readback, dict) or "error" in readback or not isinstance(values, list) or not values:
                raise CameraSettingError(f"getWhiteBalance did not return a supported current value; setWhiteBalance {mode!r} not verified")
            current = values
            got_mode, got_temp = _parse_white_balance(values)
            if str(got_mode) == mode and (not temp_enabled or _kelvin_matches(got_temp, color_temp)):
                return values
            if attempt < 19:
                time.sleep(min(0.1, max(0.0, deadline - time.monotonic())))
        raise CameraSettingError(f"setWhiteBalance requested {mode!r}, but getWhiteBalance stayed at {current!r}; not verified")

    def _available_white_balance_modes(self) -> list[str]:
        """Best-effort mode names from getAvailableWhiteBalance; empty if unparseable."""
        try:
            result = self.call("getAvailableWhiteBalance").get("result")
        except Exception:
            return []
        if not isinstance(result, list) or len(result) < 2 or not isinstance(result[1], list):
            return []
        modes = []
        for entry in result[1]:
            if isinstance(entry, dict) and isinstance(entry.get("whiteBalanceMode"), str):
                modes.append(entry["whiteBalanceMode"])
            elif isinstance(entry, str):
                modes.append(entry)
        return modes

    def set_exposure_compensation(self, index: int) -> int:
        """Set exposure compensation and require getExposureCompensation to confirm it.

        The value is an integer index in units of 1/3 EV steps (per the Smart Remote
        value strategy for camera.exposure_comp). The requested index is clamped to the
        body's advertised range from getAvailableExposureCompensation; if the body
        advertises no range (unsupported on this route) raises SettingUnsupported
        (skippable). A rejected write or a disagreeing readback raises CameraSettingError.
        Returns the clamped index that was verified.
        """
        bounds = self._exposure_compensation_bounds()
        if bounds is None:
            raise SettingUnsupported("getAvailableExposureCompensation advertised no range; exposure compensation unsupported")
        low, high = bounds
        target = max(low, min(high, int(index)))

        response = self.call("setExposureCompensation", [target])
        if (not isinstance(response, dict) or "error" in response
                or not isinstance(response.get("result"), list)):
            raise CameraSettingError(f"setExposureCompensation rejected {target!r}; not verified")

        def matches(current) -> bool:
            if isinstance(current, bool) or not isinstance(current, (int, float, str)):
                return False
            try:
                return int(current) == target
            except (TypeError, ValueError):
                return False

        self._verify_current("getExposureCompensation", matches, f"setExposureCompensation {target!r}")
        return target

    def exposure_compensation_step(self) -> int:
        """Step unit advertised by getAvailableExposureCompensation: 1 = 1/3 EV, 2 = 1/2 EV.
        Defaults to 1 when the body does not advertise it."""
        try:
            result = self.call("getAvailableExposureCompensation").get("result")
        except Exception:
            return 1
        if not isinstance(result, list):
            return 1
        tail = result[1] if len(result) > 1 and isinstance(result[1], list) else result
        ints = [v for v in tail if isinstance(v, int) and not isinstance(v, bool)]
        step = ints[-1] if len(ints) >= 4 or (tail is not result and len(ints) >= 3) else 1
        return 2 if step == 2 else 1

    def _exposure_compensation_bounds(self) -> tuple[int, int] | None:
        """Parse (min_index, max_index) from getAvailableExposureCompensation, or None.

        Tolerates both documented shapes: flat [current, max, min, step] and nested
        [current, [max, min, step]]. Returns None when no range is advertised.
        """
        try:
            result = self.call("getAvailableExposureCompensation").get("result")
        except Exception:
            return None
        if not isinstance(result, list) or len(result) < 2:
            return None
        tail = result[1]
        if isinstance(tail, list):
            ints = [v for v in tail if isinstance(v, int) and not isinstance(v, bool)]
            if len(ints) >= 2:
                return min(ints[0], ints[1]), max(ints[0], ints[1])
            return None
        ints = [v for v in result if isinstance(v, int) and not isinstance(v, bool)]
        if len(ints) >= 3:                    # [current, max, min, step] -> ints[1], ints[2]
            return min(ints[1], ints[2]), max(ints[1], ints[2])
        return None

    def take_picture(self, timeout: float = 40.0) -> list[str]:
        """Trigger the shutter; returns the postview image URL(s). VERIFIED BUG (2026-06-22): actTakePicture
        blocks until the exposure (+ long-exposure NR) finishes, so the default 10s HTTP timeout aborts any
        slow shot — an 8" exposure already exceeded it. A 1/60 shot returns in ~1s; a 30" exposure (+ NR)
        can take ~60s, so long-exposure callers should pass a bigger timeout."""
        return self.call("actTakePicture", version="1.0", timeout=timeout).get("result", [[]])[0]

    def liveview_url(self, size: str = "L") -> str | None:
        r = self.call("startLiveviewWithSize", [size]).get("result")
        return self.camera_url(r[0], context="liveview URL") if r else None

    # --- lens / focal length (so the WiFi path stops defaulting to 50mm) --------------------
    def lens_info(self) -> dict:
        """Read the live camera state from getEvent and pull out lens facts. Sony's getEvent
        returns a fixed-index array of typed blocks; the lens block has type 'lensInformation'
        with fields like {currentFocalLength, minFocalLength, maxFocalLength, ...}. Some bodies
        instead surface focal length inside the 'zoomInformation' block. Returns a dict with any
        of: focal_mm, min_focal_mm, max_focal_mm, lens_model. Empty dict if nothing is exposed —
        the caller then falls back to EXIF-from-a-still or the 50mm default."""
        info: dict = {}
        for block in self.call("getEvent", [False]).get("result", []):
            if not isinstance(block, dict):
                continue
            btype = block.get("type")
            if btype == "lensInformation":
                # field names vary by firmware; probe the common ones
                for key, dst in (("currentFocalLength", "focal_mm"),
                                 ("focalLength", "focal_mm"),
                                 ("minFocalLength", "min_focal_mm"),
                                 ("maxFocalLength", "max_focal_mm"),
                                 ("lensModelName", "lens_model"),
                                 ("lensModel", "lens_model")):
                    val = block.get(key)
                    if val not in (None, "", 0):
                        info[dst] = val
            elif btype == "zoomInformation":
                # prime lenses report no zoom; a real focal sometimes only shows here
                for key, dst in (("currentFocalLength", "focal_mm"), ("focalLength", "focal_mm")):
                    if block.get(key):
                        info.setdefault(dst, block[key])
        return info

    def focal_length_mm(self, default: float = 50.0) -> float:
        """Best-effort real focal length over WiFi (no shutter): getEvent lens/zoom info first,
        else `default`. The shutter floor is set from this, so a wrong-but-plausible default is
        safer than a fantasy: a prime at its true 35/50/85 is what we want; an unknown stays 50."""
        info = self.lens_info()
        focal = info.get("focal_mm")
        try:
            return float(focal) if focal else float(default)
        except (TypeError, ValueError):
            return float(default)

    # --- Contents Transfer: pull full-res JPEGs off the card over WiFi -----------------------
    def set_camera_function(self, function: str) -> dict:
        """Switch the camera between 'Remote Shooting' (liveview/shoot/focus) and 'Contents
        Transfer' (browse + download the card). These are MUTUALLY EXCLUSIVE camera functions on
        the A7 II — focus/exposure setters disappear in Contents Transfer and the avContent
        browse methods disappear in Remote Shooting, so the shoot loop must shoot first, then
        switch to transfer, then switch back."""
        return self.call("setCameraFunction", [function])

    def _av_call(self, method: str, params=None, version: str = "1.0") -> dict:
        """avContent service lives at a sibling endpoint ('/avContent' instead of '/camera').
        Same host/port, different service path."""
        av_endpoint = self.endpoint.rsplit("/", 1)[0] + "/avContent"
        self._id += 1
        body = json.dumps({"method": method, "params": params or [], "id": self._id,
                           "version": version}).encode()
        return json_response(
            _camera_urlopen(av_endpoint, data=body, timeout=15),
            limit=MAX_CAMERA_RESPONSE_BYTES,
        )

    def list_full_res(self, max_items: int = 200, scheme: str = "storage") -> list[dict]:
        """Browse the card and return the most-recent stills as
        [{'name', 'url', 'kind', 'created'}], newest first. kind is 'RAW'/'JPEG'/... from the
        content's original record. Requires the camera to already be in 'Contents Transfer'
        (call set_camera_function('Contents Transfer') first). Implements the canonical avContent
        walk: getSchemeList -> getSourceList(scheme) -> getContentList(uri) paged in <=100 chunks.
        """
        # 1) scheme: pick the requested scheme if present, else the first offered
        schemes = self._av_call("getSchemeList").get("result", [[]])[0]
        scheme_names = [s.get("scheme") for s in schemes if isinstance(s, dict)]
        chosen_scheme = scheme if scheme in scheme_names else (scheme_names[0] if scheme_names else scheme)
        # 2) source URI for that scheme (e.g. 'storage:memoryCard1')
        sources = self._av_call("getSourceList", [{"scheme": chosen_scheme}]).get("result", [[]])[0]
        source_uri = None
        for src in sources:
            uri = src.get("source") if isinstance(src, dict) else None
            if uri and ("memoryCard" in uri or source_uri is None):
                source_uri = uri
                if "memoryCard" in uri:
                    break
        if not source_uri:
            return []
        # 3) page the flat content list, newest first, capped at max_items
        items: list[dict] = []
        start = 0
        list_version = "1.3"  # newest A7 II firmwares; falls back to 1.0 below if rejected
        while len(items) < max_items:
            count = min(100, max_items - len(items))  # API hard cap is 100 per call
            params = [{"uri": source_uri, "stIdx": start, "cnt": count,
                       "view": "flat", "sort": "descending"}]
            response = self._av_call("getContentList", params, version=list_version)
            # some A7 II firmwares only expose getContentList at v1.0 -> retry once on a version error
            if response.get("error") and list_version != "1.0":
                list_version = "1.0"
                response = self._av_call("getContentList", params, version="1.0")
            # a remaining error after the v1.3->v1.0 retry leaves result empty -> treated as the
            # last (empty) page and the walk stops; an error here yields [] rather than raising.
            result = response.get("result")
            page = result[0] if result else []
            if not page:
                break
            for entry in page:
                originals = entry.get("content", {}).get("original", []) or []
                for orig in originals:
                    url = orig.get("url")
                    if not url:
                        continue
                    try:
                        safe_url = self.camera_url(url, context="content URL")
                    except ValueError:
                        continue
                    items.append({
                        "name": _safe_camera_filename(
                            orig.get("fileName") or entry.get("title")
                            or urllib.parse.urlsplit(safe_url).path.rsplit("/", 1)[-1]
                        ),
                        "url": safe_url,
                        "kind": orig.get("stillObject") or entry.get("contentKind") or "still",
                        "created": entry.get("createdTime"),
                    })
            if len(page) < count:
                break  # last page
            start += count
        return items[:max_items]

    def download(self, url: str, save_path: str) -> str:
        """Stream a content URL (from list_full_res) to disk in bounded chunks. Returns save_path."""
        url_suffix = Path(urllib.parse.urlsplit(url).path).suffix.lower()
        destination_suffix = Path(save_path).suffix.lower()
        if url_suffix in _RAW_SUFFIXES or destination_suffix in _RAW_SUFFIXES:
            raise ValueError("RAW download is disabled; request JPEG output")
        safe_url = self.camera_url(url, context="download URL")
        received = 0
        try:
            with _camera_urlopen(safe_url, timeout=120) as resp, open(save_path, "wb") as out:
                declared = getattr(resp, "headers", {}).get("Content-Length")
                if declared and int(declared) > MAX_CAMERA_DOWNLOAD_BYTES:
                    raise ValueError("camera download exceeds byte limit")
                while True:
                    chunk = resp.read(1 << 16)
                    if not chunk:
                        break
                    received += len(chunk)
                    if received > MAX_CAMERA_DOWNLOAD_BYTES:
                        raise ValueError("camera download exceeds byte limit")
                    out.write(chunk)
        except Exception:
            from pathlib import Path as _Path
            _Path(save_path).unlink(missing_ok=True)
            raise
        return save_path

    def transfer_latest_full_res(self, save_dir: str, count: int = 1,
                                 prefer_raw: bool = False) -> list[str]:
        """END-TO-END full-res pull, newest first: enter Contents Transfer, browse, download the
        latest `count` stills to `save_dir`, then return to Remote Shooting (so the focus/exposure
        loop keeps working). Only JPEG files are transferred; RAW transfers are deliberately
        unsupported. NOTE: this DROPS the live remote session — re-run begin_session() for the next
        shot."""
        if prefer_raw:
            raise ValueError("RAW transfer is disabled; request JPEG output")
        from pathlib import Path as _Path
        out_dir = _Path(save_dir)
        out_dir.mkdir(parents=True, exist_ok=True)
        saved: list[str] = []
        try:
            # VERIFIED LIVE (2026-06-22): setCameraFunction("Contents Transfer") returns
            # error[1] "Not Available Now" while a liveview is running — you MUST stop it first.
            # With that, avContent serves the full-res walk and a 24MP JPEG downloads over WiFi.
            self.call("stopLiveview")
            time.sleep(0.8)
            switch = self.set_camera_function("Contents Transfer")
            # setCameraFunction returns {"error":[...]} (not a raise) when the switch fails; treat
            # that as a hard stop — avContent isn't mounted, so browsing/downloading would be junk.
            if isinstance(switch, dict) and switch.get("error"):
                return []
            time.sleep(1.5)  # the body needs a beat to mount the card as a content source
            listing = self.list_full_res(max_items=max(count * 3, 10))
            listing = _jpeg_only(listing)
            for entry in listing[:count]:
                dest = _contained_transfer_path(out_dir, entry["name"])
                self.download(entry["url"], str(dest))
                saved.append(str(dest))
        finally:
            # ALWAYS return to shooting, even if a download failed mid-way, and re-arm rec mode so
            # the next begin_session()/shot works (the function switch tore down the live session).
            # Wrap the restore so a restore failure can't MASK the original download/listing error
            # (a bare raise here would replace the real exception propagating out of the try).
            try:
                self.set_camera_function("Remote Shooting")
                time.sleep(1.0)
                self.start_rec_mode()
            except Exception:  # noqa: BLE001 - swallow restore errors; let the primary exception win
                pass
        return saved


def _jpeg_only(items: list[dict]) -> list[dict]:
    """Retain only JPEG card entries, preserving the camera's newest-first ordering."""
    from pathlib import Path as _Path
    return [item for item in items if _Path(item["name"]).suffix.lower() in {".jpg", ".jpeg"}]


# Sony "DIRECT-" AP-mode cameras serve the Camera Remote API at a fixed gateway + path. CONFIRMED
# live on the A7 II: http://192.168.122.1:8080/sony/camera. SSDP multicast proved unreliable when the
# host is dual-homed (internet on Ethernet, camera on Wi-Fi — the M-SEARCH exits the default route,
# not the camera interface), so we fall back to this known endpoint, which is what actually worked.
_SONY_AP_ENDPOINT = DEFAULT_CAMERA_ENDPOINT


def connect(
    timeout_s: float = 3.0,
    camera_endpoint: str | None = None,
    *,
    require_live: bool = True,
    device_description_url: str | None = None,
) -> CameraRemote | None:
    """Build a client from explicit/env/SSDP/fallback endpoints.

    A selected endpoint must answer `getAvailableApiList` unless
    `require_live=False`. This keeps status and dry-run callers camera-free
    while preventing live callers from receiving a dead client.
    """
    endpoint = camera_endpoint or os.getenv(ENDPOINT_ENV)
    if not endpoint:
        try:
            dd = discover(timeout_s)
            endpoint = endpoint_from_dd(dd) if dd else None
        except Exception:  # noqa: BLE001 - SSDP/DD failures fall through to the proven AP
            endpoint = None

    cam = CameraRemote(endpoint or _SONY_AP_ENDPOINT, device_description_url=device_description_url)
    if not require_live:
        return cam
    try:
        cam.verify_identity()
        cam.available_apis()
        return cam
    except CameraIdentityError:
        raise
    except Exception:  # noqa: BLE001 - not reachable means no usable camera
        return None


if __name__ == "__main__":
    cam = connect()
    if not cam:
        print("No camera found via SSDP.\n"
              "  1. On the A7 II: MENU -> Application -> Smart Remote Embedded (camera becomes a WiFi AP)\n"
              "  2. Join that AP (DIRECT-xxxx:ILCE-7M2) from THIS host\n"
              "  3. Re-run. (This host needs the camera's subnet, not internet.)")
        raise SystemExit(1)
    cam.start_rec_mode()
    apis = cam.available_apis()
    print(f"endpoint: {cam.endpoint}")
    print(f"available APIs ({len(apis)}):")
    for api in sorted(apis):
        mark = "   <<< FOCUS-POINT!" if api == "setTouchAFPosition" else ""
        print(f"  {api}{mark}")
    have_focus = "setTouchAFPosition" in apis
    have_exposure = {"setFNumber", "setShutterSpeed", "setIsoSpeedRate"} <= set(apis)
    print(f"\n  FOCUS-POINT (setTouchAFPosition): {'YES' if have_focus else 'NO'}")
    print(f"  EXPOSURE (F/shutter/ISO setters):  {'YES' if have_exposure else 'partial/NO'}")
