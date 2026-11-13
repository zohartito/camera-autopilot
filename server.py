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
import sys
import threading
from datetime import date as date_cls
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

# ``server.py`` is also launched from the repository root.  Make backend-owned
# resource guards importable before importing any of them.
_BACKEND = Path(__file__).resolve().parent / "backend"
if str(_BACKEND) not in sys.path:
    sys.path.insert(0, str(_BACKEND))

from astral import Observer
from astral.sun import SunDirection, blue_hour, golden_hour, sun
from mcp.server.mcpserver import Image, MCPServer
from mcp.types import CallToolResult, TextContent
from PIL import Image as PILImage
from image_safety import image_size, open_rgb

from sonyremote import (Camera, CameraError, CameraIdentityError, CaptureOutcomeUncertain,
                        RecordingStopUncertain, assert_recording_allowed, camera_for_serial,
                        PlaceholderSettingsError, detect_identified, placeholder_refusal,
                        read_settings_checked)
from sonyremote.autoexpose import auto_expose as run_auto_expose

# The autopilot's *intelligence* (intent brain, engines, the closed reflexion loop) lives in
# backend/. Put it on the path so the high-level `shoot` tool below can reuse it as a thin wrapper —
# exactly the same modules the FastAPI app and the `shoot.py` CLI already drive. The 11 low-level
# camera tools above do NOT depend on this; if backend/ ever moved, they keep working.
mcp = MCPServer("sony-camera")

# --- single reused, lock-guarded camera connection ----------------------------
_lock = threading.RLock()
_camera: Camera | None = None


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
                          structuredContent=structured, isError=True)


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
@mcp.tool()
def detect_camera() -> dict:
    """List attached cameras with their USB serial. Call this first; pass a camera's `serial` to
    every tool that fires the shutter or records. USB ports re-enumerate, so the serial is the only
    stable identity; a camera whose serial_status is not "present" cannot be used for capture."""
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


@mcp.tool()
def get_camera_settings() -> dict:
    """Read the camera's current exposure settings (iso, aperture, shutter, white balance, ...)
    plus the shooting (PASM) mode the dial is in."""
    # Re-reads a placeholder answer (f/0, ISO 0, 65535/65535); a persistent one is an error, not 0s.
    settings, reread = _read(lambda cam: read_settings_checked(cam, purpose="get_camera_settings"))
    if reread:
        settings["settings_read"] = reread  # e.g. aperture_unreported for a manual lens
    settings["shooting_mode"] = _read(lambda cam: cam.shooting_mode())
    return settings


@mcp.tool()
def list_setting_options(setting: str) -> list[str]:
    """List the valid values for a setting (e.g. 'iso', 'aperture', 'shutter', 'white_balance')
    so you only ever set values the camera will accept."""
    return _read(lambda cam: cam.list_options(setting))


@mcp.tool()
def set_exposure(
    iso: str | None = None,
    aperture: str | None = None,
    shutter: str | None = None,
    white_balance: str | None = None,
    exposure_comp: str | None = None,
) -> dict:
    """Set any subset of exposure controls. Values are strings matching the camera's own labels
    (e.g. iso='100', aperture='8', shutter='1/250'). Use list_setting_options first if unsure.
    Returns the settings that were applied and verified."""
    return _read(
        lambda cam: cam.set_exposure(
            iso=iso,
            aperture=aperture,
            shutter=shutter,
            white_balance=white_balance,
            exposure_comp=exposure_comp,
        )
    )


# These are ordinary settings, never shutter/movie/focus actions or arbitrary nodes.
_SAFE_CONTROL_SETTINGS = frozenset({
    "iso", "aperture", "shutter", "shutterspeed", "white_balance", "whitebalance",
    "exposure_comp", "exposurecompensation", "imagequality", "image_quality",
    "capturemode", "exposuremetermode", "flashmode", "imagesize", "aspectratio",
    "dro", "focusmode", "focus_mode", "focusarea", "sensorcrop", "colortemperature",
})


@mcp.tool()
def set_control(setting: str, value: str) -> dict:
    """Set an allowlisted ordinary setting without reconnecting to retry a failed write.

    Supported nodes include imagequality, capturemode, exposuremetermode, flashmode,
    imagesize, aspectratio, dro, whitebalance, focusmode/focusarea and numeric exposure
    controls. Actual menu/radio/range type and writability are checked before setting.
    Actions, buttons, toggles, movie/capture triggers, autofocus and bulb are rejected.
    Sony may step repeatedly toward the requested value within the same connection.
    """
    if setting not in _SAFE_CONTROL_SETTINGS:
        raise ValueError("Control is not an allowed ordinary setting; action nodes are unavailable here")
    if setting in {"shutter", "shutterspeed"} and str(value).strip().casefold() == "bulb":
        raise ValueError("Bulb is unavailable through this bounded setting tool")

    def set_once(cam):
        from sonyremote.camera import gp
        description = cam.describe_setting(setting)
        if description.get("type") not in {gp.GP_WIDGET_MENU, gp.GP_WIDGET_RADIO, gp.GP_WIDGET_RANGE}:
            raise ValueError("Only ordinary menu/radio/range settings are allowed; actions and toggles are rejected")
        if description.get("readonly"):
            raise CameraError("This setting is read-only on the connected body")
        return cam.set_setting(setting, value)

    applied = _once(set_once)
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
                          structuredContent=structured, isError=True)


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
                          structuredContent=structured, isError=True)


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


@mcp.tool()
def capture_photo(serial: str) -> CallToolResult:
    """Capture once and preserve the JPEG even when readback or preview rendering fails.

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


@mcp.tool()
def auto_expose(serial: str, target_brightness: int = 112) -> CallToolResult:
    """Bounded JPEG exposure feedback: at most two captures; deliver the last saved frame.

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


@mcp.tool()
def get_liveview() -> list:
    """EXPERIMENTAL: grab a single live-view frame. Sony USB preview is unreliable and may fail
    — prefer capture_photo() for a dependable look at the scene."""
    path: Path = _read(lambda cam: cam.liveview_frame())
    return [Image(data=_downscale_for_vision(path), format="jpeg"), "Live-view frame (experimental)."]


@mcp.tool()
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


@mcp.tool()
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
@mcp.tool()
def sun_times(latitude: float, longitude: float, date: str = "today", tz: str | None = None) -> dict:
    """Golden hour, blue hour, sunrise/sunset for a location and date — for planning when to
    shoot. Pass latitude/longitude (e.g. Lake Tahoe ~ 39.09, -120.04). date is 'today' or
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


@mcp.tool()
def shoot(intent: str, auto_correct: bool = False, serial: str | None = None) -> list:
    """Take the whole shot from one plain-language vibe — the autopilot does ALL the reasoning.

    This is the high-level entry point: instead of dialing ISO/aperture/shutter yourself with the
    low-level tools, just say what you want and get a FINISHED photo back. The autopilot interprets
    the intent into an exposure plan (a LOCAL model, with a deterministic keyword fallback so it
    never stalls), takes over the camera, and returns the image plus the settings it chose and a
    plain-language "why".

    intent: what you want, in plain words. Examples: "moody golden hour", "bright and airy for
        instagram", "freeze my kid running", "noir film still". Pass "" to just read the light and
        set a clean, balanced exposure.
    auto_correct: when True, engage the closed reflexion loop — after the first shot the autopilot
        critiques its own frame and RE-SHOOTS at a corrected exposure target until it's happy (or a
        small cap is hit). Needs the local vision model; if it's unavailable (e.g. mock with no
        Ollama) it degrades cleanly to a single shot. Default False = one decisive shot.
    serial: the camera's USB serial from detect_camera. Required with CAMERA_ENGINE=real (the
        server's CAMERA_SERIAL is the fallback); the body is proven by it before any shutter.

    Returns a downscaled image to look at, a short human summary, and a structured record under
    `structuredContent`: {ok, engine, intent, auto_correct, image_path, settings, summary, why}
    (plus attempts/stopped_reason when auto_correct ran). Honors CAMERA_ENGINE (default mock, so
    this works with no camera attached; set CAMERA_ENGINE=real for the tethered Sony A7 II).
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


if __name__ == "__main__":
    mcp.run()
