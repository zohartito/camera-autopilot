"""sony-camera-mcp — an MCP server that lets an LLM drive the Sony A7 II.

Wraps the `sonyremote` mini-SDK as MCP tools so a host like Claude Desktop can run the
capture -> see -> adjust loop: read settings, set exposure, trigger the shutter, get the
resulting JPEG back as an image it can look at, and auto-expose a scene. Also exposes
`sun_times` for golden/blue-hour planning. Location scouting ("where should I shoot here?")
is left to the host LLM's own web search.

The camera holds an exclusive USB lock and gphoto2 is not thread-safe, so ALL camera access is
serialized behind a single lock and one reused connection. Reads/sets reconnect-and-retry once
if the link drops; capture is fired at most once per call (never retried — it is not idempotent).
"""

from __future__ import annotations

import io
import os
from functools import wraps
from typing import Annotated, Literal
import sys
import threading
from datetime import date as date_cls
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

# ``server.py`` is also launched from the repository root.  Make backend-owned
# resource guards importable before importing any of them.
_BACKEND = Path(__file__).resolve().parent / "backend"
if str(_BACKEND) not in sys.path:
    sys.path.insert(0, str(_BACKEND))

from astral import Observer
from astral.sun import SunDirection, blue_hour, golden_hour, sun
from mcp.server.mcpserver import Image, MCPServer
from mcp.types import CallToolResult, TextContent
from mcp.server.mcpserver.exceptions import ToolError
from pydantic import BeforeValidator
from camera_caps import A7II_CAPS
from PIL import Image as PILImage
from image_safety import image_size, open_rgb

from sonyremote import (Camera, CameraError, CameraIdentityError, CaptureOutcomeUncertain,
                        RecordingStopUncertain, assert_recording_allowed, camera_for_serial,
                        PlaceholderSettingsError, detect_identified, placeholder_refusal,
                        read_settings_checked)
from sonyremote.autoexpose import auto_expose as run_auto_expose

# The SDK validates arguments before invoking tool functions. Keep schema errors as useful
# structured responses too, without replacing the backend capture or intent pipeline.
class GuidedMCPServer(MCPServer):
    async def call_tool(self, name, arguments, context=None):
        # Schema validation runs before the function; preserve guidance on those errors too.
        try:
            return await super().call_tool(name, arguments, context)
        except ToolError as exc:
            return _error_result(str(exc), tool=name)


mcp = GuidedMCPServer("sony-camera")

# --- single reused, lock-guarded camera connection ----------------------------
_lock = threading.RLock()
_camera: Camera | None = None


# Observations only: formatting a response must never open a USB connection or query a model.
_last_observation: dict = {}


def _engine_name() -> str:
    mode = os.getenv("CAMERA_ENGINE")
    return mode if mode in {"real", "wifi"} else "mock"


def _camera_state() -> dict:
    mode = _engine_name()
    cached_serial = getattr(_camera, "verified_serial", None) if mode == "real" else None
    observed = _last_observation if _last_observation.get("engine") == mode else {}
    if cached_serial and cached_serial != observed.get("serial"):
        observed = {}
    return {"engine": mode, "serial": cached_serial or observed.get("serial"),
            "connected": False if mode == "mock" else (True if cached_serial else None),
            "last_settings": observed.get("settings"),
            "settings_source": observed.get("source", "unknown")}


def _remember_settings(settings, *, serial=None, source, engine=None):
    global _last_observation
    _last_observation = {"engine": engine or _engine_name(), "serial": serial,
                         "settings": dict(settings), "source": source}


def _guidance(tool: str, *, error=False) -> list[dict]:
    if error:
        return [{"tool": "get_camera_settings",
                 "when": "Inspect state and correct the request before any new action; never automatically retry a shutter or recording."}]
    if tool == "detect_camera":
        return [{"tool": "get_camera_settings", "when": "Read exposure and optional setting choices without taking a photo."},
                {"tool": "shoot", "when": "A photo is requested; pass a discovered serial in real mode."}]
    if tool == "sun_times":
        return [{"tool": "detect_camera", "when": "Ready to shoot at the planned time; discover the body first in real mode."}]
    return [{"tool": "get_camera_settings", "when": "Read current exposure without another capture."},
            {"tool": "shoot", "when": "A new photo is explicitly requested after reviewing the previous result."}]


def _with_context(data: dict, *, tool="shoot", error=False, next_steps=None) -> dict:
    return {**data, "camera_state": data.get("camera_state", _camera_state()),
            "next": next_steps if next_steps is not None else data.get("next", _guidance(tool, error=error))}


def _error_result(message: str, *, tool="shoot", next_steps=None) -> CallToolResult:
    return CallToolResult(content=[TextContent(type="text", text=message)], isError=True,
                          structuredContent=_with_context({"ok": False, "detail": message},
                                                          tool=tool, error=True, next_steps=next_steps))


def _instructive(fn):
    """Add response guidance without changing the underlying capture path or retry policy."""
    @wraps(fn)
    def wrapped(*args, **kwargs):
        with _lock:
            try:
                result = fn(*args, **kwargs)
            except (CameraIdentityError, PlaceholderSettingsError) as exc:
                return _identity_refusal_result(exc)
            except (CameraError, ValueError, ZoneInfoNotFoundError) as exc:
                return _error_result(str(exc), tool=fn.__name__)
            if isinstance(result, CallToolResult):
                result.structured_content = _with_context(result.structured_content or {},
                                                          tool=fn.__name__, error=result.is_error)
                return result
            if isinstance(result, dict):
                return _with_context(result, tool=fn.__name__)
            # Legacy image/list expert tools keep their content blocks on the wire.
            content = [item.to_image_content() if isinstance(item, Image) else
                       TextContent(type="text", text=str(item)) for item in result]
            return CallToolResult(content=content,
                                  structuredContent=_with_context({"ok": True}, tool=fn.__name__))
    return wrapped


def _choice(value, values, parameter, example):
    if value not in values:
        raise ValueError(f"{parameter} must be one of {list(values)} (got {value!r}). Example: {example}")
    return value


_ISO_VALUES = tuple(str(value) for value in A7II_CAPS.isos)
_SHUTTER_VALUES = tuple(A7II_CAPS.shutter_labels)
_RECORD_MODES = ("liveview_clip", "native_movie")
ExposureISO = Annotated[Literal[_ISO_VALUES], BeforeValidator(
    lambda value: _choice(value, _ISO_VALUES, "iso", "set_exposure(iso='200')"))]
ExposureShutter = Annotated[Literal[_SHUTTER_VALUES], BeforeValidator(
    lambda value: _choice(value, _SHUTTER_VALUES, "shutter", "set_exposure(shutter='1/250')"))]
RecordMode = Annotated[Literal["liveview_clip", "native_movie"], BeforeValidator(
    lambda value: _choice(value, _RECORD_MODES, "mode", "record(duration_s=6, mode='native_movie', serial='SERIAL')"))]


def _get_camera(serial: str | None = None) -> Camera:
    """The cached connection. With `serial`, the body is chosen by that USB serial (ports
    re-enumerate) and Camera.connect proves it before returning; a cached connection to any
    other (or an unverified) body is dropped first."""
    global _camera
    if serial and _camera is not None and getattr(_camera, "verified_serial", None) != serial:
        _reset_camera()
    if _camera is None:
        cam = _camera_for_serial(serial) if serial else Camera()
        cam.connect()
        _camera = cam
    return _camera


def _body_for_serial(serial: str) -> dict:
    """The one discovery row carrying `serial` (its port is today's; ports re-enumerate)."""
    rows = [row for row in detect_identified() if row.get("serial") == serial]
    if len(rows) != 1:
        raise CameraIdentityError(
            f"USB serial {serial} is {'not attached' if not rows else 'reported by more than one row'}; "
            "call detect_camera and pass the serial it reports.",
            reason="not_present" if not rows else "serial_duplicate", expected_serial=serial)
    return rows[0]


def _camera_for_serial(serial: str) -> Camera:
    import engine  # the product shot store: identity-unproven downloads are quarantined there
    camera = camera_for_serial(serial)
    camera.quarantine_dir = engine.SHOTS_DIR / "quarantine"
    return camera


def _real_engine_for_serial(engine_mod, serial: str | None):
    """A product USB engine bound to the body that carries `serial` at its current port."""
    body = _body_for_serial(_require_serial(serial))
    return engine_mod.RealEngine(camera_model=body["model"], camera_port=body["port"], camera_serial=serial)


def _require_serial(serial: str | None) -> str:
    """Tools that fire the shutter or record never run on an unproven body."""
    if not serial:
        raise CameraIdentityError("Pass the camera's USB serial (from detect_camera) to this tool.",
                                  reason="serial_required")
    return serial


def _verified_identity(cam) -> dict:
    """What the connection itself proved (never an echo of the requested serial)."""
    verification = getattr(cam, "serial_verification", None)
    return {"camera_serial": getattr(cam, "verified_serial", None),
            "serial_verification": dict(verification) if isinstance(verification, dict) else None}


def _identity_refusal_result(exc: CameraIdentityError | PlaceholderSettingsError) -> CallToolResult:
    import engine as engine_mod
    # A long-route refusal after its settings were written carries whether the body was put back.
    structured = {"ok": False, **exc.as_dict(), **engine_mod.long_refusal_restore_state(exc)}
    text = str(exc) + (f" Warning: {structured['warning']}" if structured.get("warning") else "")
    return CallToolResult(content=[TextContent(type="text", text=text)],
                          structuredContent=_with_context(structured, error=True, next_steps=[
                              {"tool": "detect_camera", "when": "Check power/USB and discover the body, then pass its serial to a new request."},
                              {"tool": "get_camera_settings", "when": "Wake the camera and verify valid exposure readback before any new action."}]), isError=True)


def _reset_camera() -> None:
    global _camera
    if _camera is not None:
        try:
            _camera.disconnect()
        except CameraError:
            pass
    _camera = None


def _ensure_connected(serial: str | None = None) -> Camera:
    """Return a live camera, reconnecting once if the cached connection is dead."""
    try:
        return _get_camera(serial)
    except CameraIdentityError:
        raise  # a refused identity is not a dead link; reconnecting cannot fix it
    except CameraError:
        _reset_camera()
        return _get_camera(serial)


def _read(action, attempts: int = 2):
    """For idempotent reads/sets: reconnect-and-retry once if the USB link drops."""
    with _lock:
        last_error: CameraError | None = None
        for _ in range(attempts):
            try:
                return action(_get_camera())
            except CameraIdentityError:
                raise  # terminal: a refused identity is never retried
            except CameraError as exc:
                last_error = exc
                _reset_camera()
        raise last_error  # type: ignore[misc]


def _once(action, serial: str | None = None):
    """For non-idempotent actions (capture): connect, run exactly once, never retry the action.
    On failure, drop the connection so the NEXT call reconnects — without re-firing now."""
    with _lock:
        cam = _ensure_connected(serial)
        try:
            return action(cam)
        except RecordingStopUncertain:
            # Cache reset is transport cleanup only; the durable recording quarantine remains set.
            _reset_camera()
            raise
        except CameraError:
            _reset_camera()
            raise


# --- keep-alive: defeat the camera's auto-power-off -----------------------------
# There is NO Sony PTP property to disable auto-power-off on the A7 II, but every PTP
# read resets the camera's idle timer. A steady trickle of cheap reads on its own thread
# holds the one PTP session open indefinitely (empirically proven: 10/10 pings, 0 drops
# over 2.5 min vs ~1 min idle before). Runs only when a connection already exists; never
# forces one, and yields the lock instantly to real tool calls.
_KEEPALIVE_SECONDS = 8.0
# The loop waits on this event, not time.sleep, so it can be stopped and a test that patches the
# shared time module never turns it into a spin loop feeding the test's fake sleep or clock.
_keepalive_stop = threading.Event()


def _keepalive_loop() -> None:
    while not _keepalive_stop.wait(_KEEPALIVE_SECONDS):
        with _lock:
            if _camera is None:
                continue  # nothing tethered yet — don't force a connection
            try:
                _camera.get_settings()  # a real PTP read = the idle-timer reset
            except CameraError:
                _reset_camera()  # link dropped; the next tool call reconnects


_keepalive_thread = threading.Thread(target=_keepalive_loop, name="sony-keepalive", daemon=True)
_keepalive_thread.start()


def _stop_keepalive(timeout: float = 5.0) -> None:
    """Stop and join the keep-alive thread (tests that import this module call it at teardown)."""
    _keepalive_stop.set()
    _keepalive_thread.join(timeout)


def _downscale_for_vision(src: Path, max_edge: int = 1568) -> bytes:
    """Shrink a full-res capture to a vision-friendly JPEG (Claude's optimal long edge)."""
    image = open_rgb(src)
    image.thumbnail((max_edge, max_edge))
    buffer = io.BytesIO()
    image.save(buffer, format="JPEG", quality=85)
    return buffer.getvalue()


# --- camera tools -------------------------------------------------------------
@_instructive
def detect_camera() -> dict[str, object]:
    """Use to discover USB identities before shooting; use get_camera_settings for exposure reads.

    Pass a discovered serial to shoot or expert recording/capture tools in real mode.
    Discovery does not prove an open connection. Mock mode returns no physical cameras.
    """
    if _engine_name() == "mock":
        return {"cameras": [], "detail": "Mock engine: no physical camera was probed; shoot produces a demo."}
    with _lock:
        rows = detect_identified()
    if not rows:
        raise CameraError("No camera detected. Check power, cable, and USB Connection = PC Remote.")
    cameras = [{"model": row["model"], "port": row["port"], "serial": row.get("serial"),
                "serial_status": row.get("serial_status") or ("present" if row.get("serial") else "missing"),
                "identification": row.get("method")} for row in rows]
    return {"cameras": cameras,
            "detail": "; ".join(f"{c['model']} at {c['port']} serial {c['serial'] or 'unavailable'} "
                                f"({c['serial_status']})" for c in cameras)}


@_instructive
def get_camera_settings(setting: str | None = None) -> dict[str, object]:
    """Use to read exposure or valid setting choices without a shutter; use shoot for a photo.

    Optional setting (for example 'iso' or 'white_balance') includes that control's options,
    replacing list_setting_options. Expert set_exposure changes numeric exposure; set_control
    handles other ordinary controls. Mock mode reports only the last simulated settings and
    reference ISO/shutter choices, never a hardware read.
    """
    if setting is not None and setting not in _SAFE_CONTROL_SETTINGS:
        _choice(setting, sorted(_SAFE_CONTROL_SETTINGS), "setting", "get_camera_settings(setting='iso')")
    if _engine_name() == "mock":
        result = {"settings": _camera_state()["last_settings"], "shooting_mode": None,
                  "detail": "Mock engine: settings are simulated, not a hardware read."}
        if setting:
            result.update({"setting": setting, "options": list({"iso": _ISO_VALUES,
                           "shutter": _SHUTTER_VALUES}.get(setting, ())),
                           "options_source": "reference_only; other choices require a connected body"})
        return result
    # Keep the legacy top-level setting keys, adding options only when requested.
    settings, reread = _read(lambda cam: read_settings_checked(cam, purpose="get_camera_settings"))
    if reread:
        settings["settings_read"] = reread
    settings["shooting_mode"] = _read(lambda cam: cam.shooting_mode())
    _remember_settings(settings, serial=getattr(_camera, "verified_serial", None), source="last_read")
    if setting:
        settings.update({"setting": setting, "options": list_setting_options(setting),
                         "options_source": "connected_body"})
    return settings


def list_setting_options(setting: str) -> list[str]:
    """List the valid values for a setting (e.g. 'iso', 'aperture', 'shutter', 'white_balance')
    so you only ever set values the camera will accept."""
    return _read(lambda cam: cam.list_options(setting))


def set_exposure(
    iso: ExposureISO | None = None,
    aperture: str | None = None,
    shutter: ExposureShutter | None = None,
    white_balance: str | None = None,
    exposure_comp: str | None = None,
) -> dict:
    """Use for explicit exposure values; use shoot for a visual goal or set_control for other knobs.

    ISO and shutter enums are the numeric A7 II reference subset from camera_caps, not a
    promise of support on every body. Aperture, white balance and compensation are body/lens
    dependent strings. Read get_camera_settings(setting='iso') for live options first.
    All supplied values are checked against live choices before any write. For other
    body-specific ISO/shutter labels, experts can use set_control after reading options.
    """
    for name, value, values, example in (
        ("iso", iso, _ISO_VALUES, "set_exposure(iso='200')"),
        ("shutter", shutter, _SHUTTER_VALUES, "set_exposure(shutter='1/250')"),
    ):
        if value is not None:
            _choice(value, values, name, example)
    if _engine_name() != "real":
        return _error_result("set_exposure requires CAMERA_ENGINE=real; mock mode never writes hardware.", tool="set_exposure")
    values = dict(iso=iso, aperture=aperture, shutter=shutter,
                  white_balance=white_balance, exposure_comp=exposure_comp)

    def apply(cam):
        for name, value in values.items():
            if value is not None:
                options = cam.list_options(name)
                example = (f"set_exposure({name}={options[0]!r})" if options else
                           f"get_camera_settings(setting={name!r})")
                # Preserve the existing SDK shorthand: aperture='8' also means 'f/8'.
                label = f"f/{value}" if name == "aperture" and f"f/{value}" in options else value
                _choice(label, options, name, example)
        return cam.set_exposure(**values)

    applied = _read(apply)
    _remember_settings(applied, serial=getattr(_camera, "verified_serial", None), source="last_write_readback")
    return applied


# These are ordinary settings, never shutter/movie/focus actions or arbitrary nodes.
_SAFE_CONTROL_SETTINGS = frozenset({
    "iso", "aperture", "shutter", "shutterspeed", "white_balance", "whitebalance",
    "exposure_comp", "exposurecompensation", "imagequality", "image_quality",
    "capturemode", "exposuremetermode", "flashmode", "imagesize", "aspectratio",
    "dro", "focusmode", "focus_mode", "focusarea", "sensorcrop", "colortemperature",
})


def set_control(setting: str, value: str) -> dict:
    """Use for an explicit ordinary control such as focusmode; prefer set_exposure for exposure.

    Read get_camera_settings(setting=...) first for body-specific choices. No reconnect retry.

    Supported nodes include imagequality, capturemode, exposuremetermode, flashmode,
    imagesize, aspectratio, dro, whitebalance, focusmode/focusarea and numeric exposure
    controls. Actual menu/radio/range type and writability are checked before setting.
    Actions, buttons, toggles, movie/capture triggers, autofocus and bulb are rejected.
    Sony may step repeatedly toward the requested value within the same connection.
    """
    if setting not in _SAFE_CONTROL_SETTINGS:
        raise ValueError(f"Control must be an allowed ordinary setting: {sorted(_SAFE_CONTROL_SETTINGS)}. "
                         "Example: set_control(setting='focusmode', value='Manual')")
    if setting in {"shutter", "shutterspeed"} and str(value).strip().casefold() == "bulb":
        raise ValueError(f"Bulb is unavailable through this bounded setting tool. Valid reference values: {list(_SHUTTER_VALUES)}. "
                         "Example: set_control(setting='shutter', value='1/250')")

    def set_once(cam):
        from sonyremote.camera import gp
        description = cam.describe_setting(setting)
        if description.get("type") not in {gp.GP_WIDGET_MENU, gp.GP_WIDGET_RADIO, gp.GP_WIDGET_RANGE}:
            raise ValueError("Only ordinary menu/radio/range settings are allowed; actions and toggles are rejected")
        if description.get("readonly"):
            raise CameraError("This setting is read-only on the connected body")
        if "choices" in description:
            choices = description["choices"]
            label = f"f/{value}" if setting == "aperture" and f"f/{value}" in choices else value
            example = (f"set_control(setting={setting!r}, value={choices[0]!r})" if choices else
                       f"get_camera_settings(setting={setting!r})")
            _choice(label, choices, "value", example)
        elif "range" in description:
            from math import isfinite, isclose
            minimum, maximum, step = description["range"]
            try:
                number = float(value)
                valid = isfinite(number) and minimum <= number <= maximum
                if valid and step > 0:
                    offset = (number - minimum) / step
                    valid = isclose(offset, round(offset), abs_tol=1e-6)
            except (TypeError, ValueError):
                valid = False
            if not valid:
                raise ValueError(f"value must be in [{minimum}, {maximum}] with step {step} (got {value!r}). "
                                 f"Example: set_control(setting={setting!r}, value={str(minimum)!r})")
        return cam.set_setting(setting, value)

    applied = _once(set_once)
    _remember_settings({setting: applied}, serial=getattr(_camera, "verified_serial", None), source="last_write_readback")
    return {"setting": setting, "applied": applied, "reconnect_retry": False}


def _verified_saved_image(path) -> bool:
    if not path:
        return False
    try:
        from image_safety import open_rgb
        with open_rgb(Path(path)) as decoded:
            decoded.load()
        return True
    except Exception:
        return False


def _capture_uncertain_result(exc: CaptureOutcomeUncertain) -> CallToolResult:
    path = str(exc.capture_path) if exc.capture_path else None
    structured = {"ok": False, "may_have_fired": True, "retry_capture": False,
                  "capture_path": path, "image_path": path, "detail": str(exc),
                  "preserved_image": _verified_saved_image(path)}
    return CallToolResult(content=[TextContent(type="text", text=str(exc))],
                          structuredContent=_with_context(structured, error=True, next_steps=[
                              {"tool": "get_camera_settings", "when": "After inspecting any preserved image and the camera, read state; do not automatically retry capture."}]), isError=True)


def _final_shutter_refused_result(exc: CameraError) -> CallToolResult:
    """A long exposure refused before its trigger: only the meter frame fired (kept at capture_path).
    Same fields as HTTP /shoot; asking again is a new request, never an automatic retry."""
    path = str(exc.capture_path) if getattr(exc, "capture_path", None) else None
    structured = {"ok": False, "may_have_fired": False, "final_shutter_issued": False,
                  "retry_capture": False, "capture_path": path, "detail": str(exc),
                  "focus_policy": getattr(exc, "focus_policy", None),
                  "transport_receipt": getattr(exc, "transport_receipt", None),
                  **({"meter_restore": exc.meter_restore} if getattr(exc, "meter_restore", None) else {})}
    return CallToolResult(content=[TextContent(type="text", text=str(exc))],
                          structuredContent=_with_context(structured, error=True, next_steps=[
                              {"tool": "get_camera_settings", "when": "After inspecting any preserved image and the camera, read state; do not automatically retry capture."}]), isError=True)


def _saved_photo_result(path: Path, structured: dict, detail: str) -> CallToolResult:
    """Delivery of a saved image is separate from quality, readback and display success."""
    if not _verified_saved_image(path):
        return _capture_uncertain_result(CaptureOutcomeUncertain(
            "Capture returned a file that could not be validated; inspect it before any new shutter.",
            capture_path=path))
    structured.update({"capture_path": str(path), "image_path": str(path),
                       "preserved_image": True, "may_have_fired": True,
                       "retry_capture": False})
    content = []
    try:
        content.append(Image(data=_downscale_for_vision(path), format="jpeg").to_image_content())
    except Exception as exc:
        structured["partial"] = True
        structured["preview"] = {"status": "unavailable", "reason": type(exc).__name__,
                                 "retry_capture": False}
        detail += " Preview unavailable; use the preserved file instead of capturing again."
    structured["detail"] = detail
    content.append(TextContent(type="text", text=detail))
    return CallToolResult(content=content, structuredContent=structured, isError=False)


def capture_photo(serial: str) -> CallToolResult:
    """Use to capture once at the existing exposure; use shoot to choose settings from a goal.

    serial: the camera's USB serial from detect_camera. The body is proven by it before any shutter.
    """
    state = {"started": False, "path": None, "settings": {}, "settings_source": "unknown", "identity": {}}
    warnings = []

    def capture(cam):
        state["identity"] = _verified_identity(cam)
        try:
            state["settings"] = read_settings_checked(cam, purpose="before capture")[0]
            state["settings_source"] = "before_capture"
        except PlaceholderSettingsError:
            raise  # the body is not reporting real exposure yet: refuse before the shutter
        except Exception as exc:
            warnings.append(f"Pre-capture settings unavailable: {type(exc).__name__}")
        state["started"] = True
        path = cam.capture(download=True)
        state["path"] = Path(path) if path else None
        receipt = dict(getattr(cam, "last_capture_receipt", None) or {})
        if receipt.get("reconnect_ok") is False:
            warnings.append("Photo preserved; capture reconnect failed, final settings unavailable.")
        else:
            try:
                state["settings"] = read_settings_checked(cam, purpose="after capture")[0]
                state["settings_source"] = "after_capture"
            except Exception as exc:
                warnings.append(f"Photo preserved; final settings unavailable: {type(exc).__name__}")
                if (refusal := placeholder_refusal(exc)):
                    state["settings_read_refused"] = refusal
        return path

    try:
        _once(capture, _require_serial(serial))
    except CaptureOutcomeUncertain as exc:
        return _capture_uncertain_result(exc)
    except CameraIdentityError as exc:
        return _identity_refusal_result(exc)
    except PlaceholderSettingsError as exc:
        if not state["started"]:
            return _identity_refusal_result(exc)  # structured no-shutter refusal: wake and retry
        return _capture_uncertain_result(CaptureOutcomeUncertain(str(exc), capture_path=state["path"]))
    except Exception as exc:
        if not state["started"]:
            raise
        return _capture_uncertain_result(CaptureOutcomeUncertain(str(exc), capture_path=state["path"]))
    if state["path"] is None:
        return _capture_uncertain_result(CaptureOutcomeUncertain("No downloaded image path was returned."))
    return _saved_photo_result(state["path"],
        {"ok": True, "partial": bool(warnings), "settings": state["settings"],
         "settings_source": state["settings_source"], "warnings": warnings, **state["identity"],
         **({"settings_read_refused": state["settings_read_refused"]} if state.get("settings_read_refused") else {})},
        f"Full-resolution capture preserved at {state['path']}. Settings source: {state['settings_source']}.")


def auto_expose(serial: str, target_brightness: int = 112) -> CallToolResult:
    """Use for a numeric JPEG brightness target (at most two captures); use shoot for a visual goal.

    serial: the camera's USB serial from detect_camera. The body is proven by it before any shutter.
    Exposure convergence, settings readback and MCP preview are reported independently.
    A preserved frame never requires another shutter merely to repair its display.
    """
    state = {"started": False, "path": None, "settings": {}}
    report = None
    failure = None
    try:
        with _lock:
            cam = _ensure_connected(_require_serial(serial))
            state["identity"] = _verified_identity(cam)
            class TrackedCamera:
                def __getattr__(self, name):
                    return getattr(cam, name)

                def capture(self, *args, **kwargs):
                    try:
                        state["settings"] = read_settings_checked(cam, purpose="before capture")[0]
                    except PlaceholderSettingsError:
                        raise  # refuse before the shutter; an earlier frame is still delivered
                    except Exception:
                        state["settings"] = {}
                    state["started"] = True
                    path = cam.capture(*args, **kwargs)
                    state["path"] = Path(path) if path else None
                    return path
            report = run_auto_expose(TrackedCamera(), target_mean=float(target_brightness), max_iter=2)
    except CaptureOutcomeUncertain as exc:
        return _capture_uncertain_result(exc)
    except (CameraIdentityError, PlaceholderSettingsError) as exc:
        if state["path"] is None:
            return _identity_refusal_result(exc)  # structured refusal; no frame was saved
        failure = exc  # refused before a later shutter; the earlier frame is delivered below
    except Exception as exc:
        if not state["started"]:
            raise
        failure = exc
    if report is not None and report.get("final_image"):
        state["path"] = Path(report["final_image"])
    if not _verified_saved_image(state["path"]):
        return _capture_uncertain_result(CaptureOutcomeUncertain(
            str(failure) if failure else "Exposure processing returned no verified image.",
            capture_path=state["path"]))
    settings = report.get("final_settings", {}) if report else state["settings"]
    quality_met = bool(report and report.get("ok"))
    structured = {"ok": quality_met, "quality_target_met": quality_met, **state.get("identity", {}),
                  "partial": not quality_met or failure is not None,
                  "settings": settings,
                  "settings_source": "photographed_report" if report else "before_capture_or_unknown",
                  "iterations": report.get("iterations", []) if report else [],
                  "warnings": ([f"Image preserved; exposure processing stopped: {type(failure).__name__}"]
                               if failure else []),
                  "capture_warning": report.get("capture_warning") if report else None,
                  **({"settings_read_refused": report["settings_read_refused"]}
                     if report and report.get("settings_read_refused") else {})}
    if isinstance(failure, CameraIdentityError):
        # The later shutter was refused (it fired nothing); keep the refusal's structured fields.
        structured["identity_refusal"] = failure.as_dict()
    if isinstance(failure, PlaceholderSettingsError):
        structured.setdefault("settings_read_refused", failure.as_dict())
    detail = "Photograph preserved; exposure processing stopped. Inspect this image before another capture."
    if report is not None:
        try:
            detail = _format_autoexpose(report)
        except Exception as exc:
            structured["partial"] = True
            structured["warnings"].append(f"Photograph preserved; report formatting unavailable: {type(exc).__name__}")
    return _saved_photo_result(state["path"], structured, detail)


def get_liveview() -> list:
    """Use for one experimental preview without a saved still; use shoot for a finished photo.

    Sony USB live view is unreliable and may fail. No shutter is issued by this tool.
    """
    path: Path = _read(lambda cam: cam.liveview_frame())
    return [Image(data=_downscale_for_vision(path), format="jpeg"), "Live-view frame (experimental)."]


def record_video(seconds: float = 6.0, serial: str | None = None) -> list:
    """Record a short clip from the camera's live-view and save it as an MP4 to disk — for
    watching a scene change over time (e.g. a device reacting to input). Returns the saved MP4
    path plus its final frame as a still you can look at. Live-view preview is unreliable, so
    the clip is best-effort: if USB drops mid-burst, whatever was captured is still encoded.
    serial (required): the camera's USB serial from detect_camera."""
    assert_recording_allowed()
    mp4_path, last_frame = _once(lambda cam: cam.record_clip(float(seconds)), _require_serial(serial))
    return [
        Image(data=_downscale_for_vision(last_frame), format="jpeg"),
        f"Recorded ~{seconds:.0f}s clip to {mp4_path} (final frame shown).",
    ]


def record_native_video(seconds: float = 6.0, serial: str | None = None) -> dict:
    """Issue bounded native movie start/stop commands and return action evidence only.

    serial (required): the camera's USB serial from detect_camera; proven before any command.
    The elapsed action interval does not prove clip existence, playback, or recorded audio.
    Inspect the camera SD card to verify a clip. Duration follows the configured native
    recording limit; uncertain start/stop remains quarantined and must not be retried.
    """
    assert_recording_allowed()
    identity = {}
    report = _once(lambda cam: identity.update(_verified_identity(cam)) or cam.record_native(float(seconds)),
                   _require_serial(serial))
    return {"ok": True, "action_completed": True, "actions": ["start_movie", "stop_movie"], **identity,
            "elapsed_seconds": report.get("recorded_seconds"),
            "requested_seconds": report.get("requested_seconds", float(seconds)),
            "duration_limit_seconds": report.get("max_seconds"),
            "clip_existence_verified": False, "clip_playback_verified": False,
            "audio_verified": False, "retry_record": False,
            "detail": "Movie start/stop commands completed; inspect the camera SD card to verify a clip."}


@_instructive
def record(duration_s: float = 6.0, mode: RecordMode = "liveview_clip",
           serial: str | None = None) -> dict[str, object]:
    """Use for a bounded video request; use shoot for a still or get_liveview for a single preview.

    Expert-only. liveview_clip saves a best-effort MP4 from unreliable USB preview frames.
    native_movie issues start/stop commands to the body; it does not verify a saved SD-card clip,
    playback or audio. Set the physical movie mode as required and inspect the card afterwards.
    duration_s must be finite and positive; existing transport duration limits still apply.
    Pass the serial from detect_camera. Mock mode refuses recording without accessing hardware.
    Uncertain start/stop remains quarantined; never automatically retry it.
    """
    from math import isfinite
    _choice(mode, _RECORD_MODES, "mode", "record(duration_s=6, mode='native_movie', serial='SERIAL')")
    if not isfinite(duration_s) or duration_s <= 0:
        raise ValueError("duration_s must be finite and > 0. Example: record(duration_s=6, mode='liveview_clip', serial='SERIAL')")
    if _engine_name() != "real":
        return _error_result("Recording requires CAMERA_ENGINE=real; mock mode records nothing.", tool="record")
    try:
        if mode == "native_movie":
            result = record_native_video(seconds=duration_s, serial=serial)
            result["mode"] = mode
            result["next"] = [{"tool": "get_camera_settings",
                               "when": "After inspecting the SD card for clip/playback/audio evidence; never retry to infer success."}]
            return result
        result = record_video(seconds=duration_s, serial=serial)
        return CallToolResult(content=[item.to_image_content() if isinstance(item, Image) else
                                      TextContent(type="text", text=item) for item in result],
                              structuredContent={"ok": True, "mode": mode, "retry_record": False,
                                                 "requested_seconds": duration_s})
    except (CameraIdentityError, PlaceholderSettingsError) as exc:
        return _identity_refusal_result(exc)
    except RecordingStopUncertain as exc:
        result = _error_result(str(exc), tool="record", next_steps=[
            {"tool": "get_camera_settings", "when": "Only after the operator verifies recording has stopped and resolves quarantine; never retry record automatically."}])
        result.structured_content.update({"recording_stop_uncertain": True, "retry_record": False})
        return result


def start_movie(serial: str | None = None) -> dict:
    """Issue a bounded start/stop pair; return action evidence without asserting a clip."""
    return record_native_video(6.0, serial)


def stop_movie() -> str:
    """Indefinite movie control is disabled; use record_native_video instead."""
    raise RuntimeError("stop_movie is disabled; use bounded record_native_video")


def _format_autoexpose(report: dict) -> str:
    lines = [
        f"Auto-expose {'succeeded' if report['ok'] else 'incomplete'} "
        f"(mode {report['mode']}): {report['note']}",
        f"Final mean luminance {report['final_mean']}/255, blown {report['final_blown_pct']}%.",
        f"Final settings: {report['final_settings']}",
        "Steps:",
    ]
    for step in report["iterations"]:
        lines.append(
            f"  shot {step['iter']}: mean {step['mean']}"
            + (f" -> {step['correction']}" if step.get("correction") else "")
        )
    return "\n".join(lines)


# --- planning tool (no camera needed) -----------------------------------------
@_instructive
def sun_times(latitude: float, longitude: float, date: str = "today", tz: str | None = None) -> dict[str, object]:
    """Use to plan golden/blue hour or sunrise/sunset; use shoot only when ready to take a photo.

    No camera or network is needed. Pass latitude/longitude (e.g. Lake Tahoe ~ 39.09, -120.04). date is 'today' or
    ISO 'YYYY-MM-DD'. tz is an optional IANA name (e.g. 'America/Los_Angeles'); defaults to the
    computer's local timezone."""
    tzinfo = ZoneInfo(tz) if tz else datetime.now().astimezone().tzinfo
    # "today" is resolved in the TARGET timezone, not the server's, to avoid an off-by-one day.
    day = datetime.now(tzinfo).date() if date in (None, "", "today") else date_cls.fromisoformat(date)
    observer = Observer(latitude=latitude, longitude=longitude)
    events = sun(observer, date=day, tzinfo=tzinfo)

    def window(direction: SunDirection, kind) -> list[str]:
        start, end = kind(observer, day, direction, tzinfo)
        return [start.isoformat(timespec="minutes"), end.isoformat(timespec="minutes")]

    def fmt(moment):
        return moment.isoformat(timespec="minutes")
    return {
        "date": day.isoformat(),
        "timezone": str(tzinfo),
        "dawn": fmt(events["dawn"]),
        "sunrise": fmt(events["sunrise"]),
        "noon": fmt(events["noon"]),
        "sunset": fmt(events["sunset"]),
        "dusk": fmt(events["dusk"]),
        "golden_hour_morning": window(SunDirection.RISING, golden_hour),
        "golden_hour_evening": window(SunDirection.SETTING, golden_hour),
        "blue_hour_morning": window(SunDirection.RISING, blue_hour),
        "blue_hour_evening": window(SunDirection.SETTING, blue_hour),
    }


# --- high-level autopilot tool: one call, a finished shot ----------------------
# Everything above is a KNOB (set ISO, fire the shutter, ...). This is the whole autopilot in one
# tool: say a vibe, the camera reasons about it and hands back a FINISHED shot. It reuses the SAME
# intent brain + engine + reflexion loop the FastAPI app and `shoot.py` CLI drive — this MCP tool is
# a thin wrapper, NOT a reimplementation. Honors CAMERA_ENGINE (default mock, no hardware needed).


def _mock_placeholder_jpeg(dest: Path) -> None:
    """Write a tiny valid JPEG so the Image block still works on a FRESH clone under
    CAMERA_ENGINE=mock — where `captures/` is empty/gitignored, so the mock engine saves a shot
    record with NO image bytes on disk. Real captures always overwrite this; it never fires for the
    real/wifi engines (those write camera bytes). Best-effort: a failure just means no Image block."""
    try:
        with PILImage.new("RGB", (1568, 1045), (90, 80, 70)) as placeholder:
            placeholder.save(dest, format="JPEG", quality=85)
    except Exception:
        pass


def _vision_for(intent: str | None):
    """Return a critique callable(report) -> dict|None for the reflexion loop, or None if vision is
    unavailable. The closed loop only ever moves the exposure TARGET; it never invents settings."""
    try:
        import vision_critique  # local Ollama vision model — optional
    except Exception:
        return None

    def critique(report) -> dict | None:
        image_path = report.get("final_image") if isinstance(report, dict) else None
        if not image_path or not Path(image_path).is_file():
            return None  # no real bytes to look at -> no critique -> the loop degrades to one shot
        return vision_critique.critique(Path(image_path), intent)

    return critique


@_instructive
def shoot(intent: str, auto_correct: bool = False, serial: str | None = None) -> list:
    """Use to take a photo from one plain-language goal; use get_camera_settings for read-only status.

    Examples: "make it moody", "bright and airy", "freeze my kid running". Empty intent asks
    for balanced exposure. Use sun_times to plan light instead of taking a photo now.
    Verified hardware scope is a USB-tethered Mac and two tested bodies: Sony A7 II and A7 III.
    CAMERA_ENGINE defaults to mock (a simulated demo, no shutter). In real mode pass the USB
    serial from detect_camera; CAMERA_SERIAL is the fallback and identity is checked before firing.

    auto_correct=False runs one product capture request, which may include meter frames.
    True allows up to two capture attempts, adding time and shutters for critique/correction.
    Real capture can use a local vision model or a deterministic luma fallback; mock/no critique
    stops after one attempt. It is not a promise of one physical shutter per attempt.
    This tool does not expose HDR bracket, timelapse or panorama controls; those have separate
    backend/operator workflows. Long exposures and refinement retain the product's refusal rules.

    Returns an image preview when available, settings, summary/why, quality and delivery evidence,
    camera_state and next steps in structuredContent. Last-shot settings are historical, not a
    fresh read of the body's restored settings. A preview failure never warrants another shutter.
    """
    import engine as engine_mod

    intent_value = intent.strip() or None
    eng = engine_mod.get_engine()

    import product_capture
    try:
        with _lock:
            # Release the low-level cached handle before the product opens an exact body.
            _reset_camera()
            # USB identity is the serial. The Wi-Fi engine proves identity differently (its own
            # verify_identity on each file) and is out of scope here; the mock has no body.
            if eng.name == "real":
                eng = _real_engine_for_serial(engine_mod, serial or getattr(eng, "camera_serial", None))
            shot = product_capture.shoot(eng, intent_value, refine=auto_correct)
    except CaptureOutcomeUncertain as exc:
        return _capture_uncertain_result(exc)
    except (CameraIdentityError, PlaceholderSettingsError) as exc:
        # A clean refusal of the next step; an earlier meter frame is named, never re-shot.
        return _identity_refusal_result(exc)
    except CameraError as exc:
        if not getattr(exc, "final_shutter_not_issued", False):
            raise
        return _final_shutter_refused_result(exc)
    report = {"ok": bool(shot.ok),
              "final_image": str(engine_mod.SHOTS_DIR / shot.image) if shot.image else None,
              "final_settings": shot.settings or {}}
    refinement = shot.evidence.get("refinement", {})
    extra = ({"attempts": refinement.get("attempts", 1),
              "stopped_reason": refinement.get("stopped_reason", "no_vision")}
             if auto_correct else {})

    # The explicit structured record (codex's design): set structuredContent directly instead of
    # letting FastMCP bury a list-returned dict in a text block.
    structured = {
        "ok": bool(report["ok"]),
        "engine": eng.name,
        "intent": intent_value,
        "auto_correct": auto_correct,
        "image_path": report["final_image"],
        "settings": report["final_settings"],
        "summary": shot.summary,
        "why": shot.why,
        "camera_serial": (shot.evidence or {}).get("camera_serial"),
        "evidence": shot.evidence,
        "cards": shot.cards,
        "applied": shot.applied,
    }
    structured.update(extra)
    _remember_settings(report["final_settings"], serial=structured["camera_serial"],
                       source="simulated_shot" if eng.name == "mock" else "last_shot", engine=eng.name)
    structured["camera_state"] = {**_camera_state(), "engine": eng.name,
                                  "serial": structured["camera_serial"],
                                  "connected": False if eng.name == "mock" else None,
                                  "last_settings": report["final_settings"],
                                  "settings_source": "simulated_shot" if eng.name == "mock" else "last_shot"}

    delivered = _verified_saved_image(report["final_image"])
    convergence = shot.evidence.get("exposure_converged")
    refinement_satisfied = refinement.get("stopped_reason") == "satisfied"
    quality_evaluated = isinstance(convergence, bool) or refinement_satisfied
    if convergence is True or refinement_satisfied:
        quality_met = True
    elif convergence is False or not report["ok"] or refinement.get("stopped_reason") in {"max_attempts", "stalled"}:
        quality_met = False
    else:
        quality_met = None
    quality_sources = []
    if isinstance(convergence, bool):
        quality_sources.append("measured_exposure_feedback")
    if refinement_satisfied:
        quality_sources.append("model_critique_satisfied")
    structured.update({"quality_target_met": quality_met, "quality_evaluated": quality_evaluated,
                       "quality_evidence_sources": quality_sources,
                       "quality_evidence_source": ("vision_critique" if refinement_satisfied else
                                                   "jpeg_exposure_feedback" if isinstance(convergence, bool) else
                                                   "unavailable"),
                       "retry_capture": False, "preserved_image": delivered,
                       "partial": delivered and not report["ok"]})
    if not structured["ok"] and not delivered:
        # Structured, host-readable failure: the autopilot ran but couldn't land the frame.
        message = shot.summary or shot.why or "autopilot could not get the shot"
        return CallToolResult(
            content=[TextContent(type="text", text=message)],
            structuredContent=structured,
            isError=True,
        )

    content: list = []
    image_path = report["final_image"]
    try:
        if image_path and Path(image_path).is_file():
            block = Image(data=_downscale_for_vision(Path(image_path)), format="jpeg")
            content.append(block.to_image_content())
    except Exception as exc:
        # Display is downstream of the physical outcome: never spend another shutter
        # merely because an MCP image block could not be generated.
        structured["preview"] = {"status": "unavailable", "reason": type(exc).__name__,
                                 "retry_capture": False}
        content.append(TextContent(type="text", text="Photo saved; preview unavailable. Use the saved image path; do not recapture to repair display."))
    line = f"{shot.summary} {shot.why}".strip()
    if extra.get("stopped_reason") not in (None, "no_vision"):
        line += f"  [{extra['attempts']} shot(s), {extra['stopped_reason']}]"
    content.append(TextContent(type="text", text=f"{line}\nSettings: {report['final_settings']}"))

    return CallToolResult(content=content, structuredContent=structured, isError=False)


# Register goals first. The legacy functions remain importable but options/video wrappers
# have no separate MCP registration. Only the exact flag value "1" enables expert controls.
AGENT_TOOLS = (shoot, sun_times, detect_camera, get_camera_settings)
EXPERT_TOOLS = (set_exposure, set_control, capture_photo, auto_expose, get_liveview, record)


def _expert_entry(fn):
    @wraps(fn)
    def guarded(*args, **kwargs):
        if _engine_name() != "real":
            return _error_result(f"{fn.__name__} requires CAMERA_ENGINE=real; mock mode never accesses hardware.",
                                 tool=fn.__name__)
        return fn(*args, **kwargs)
    return _instructive(guarded)


for _tool in AGENT_TOOLS:
    mcp.tool()(_tool)
if os.getenv("CAMERA_MCP_EXPERT") == "1":
    for _tool in EXPERT_TOOLS:
        mcp.tool()(_tool if _tool is record else _expert_entry(_tool))


if __name__ == "__main__":
    mcp.run()
