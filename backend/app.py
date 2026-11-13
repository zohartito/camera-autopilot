"""Camera Autopilot backend.

One job: the phone says "get the shot" (optionally with what they want), the autopilot takes
over the camera and returns the finished image plus a one-line, plain-language note. No settings
are surfaced unless the user explicitly asks why.
"""

from __future__ import annotations

import hmac
import json
import logging
import ipaddress
import math
import os
import sys
import asyncio
import threading
from contextlib import contextmanager
from dataclasses import asdict
from pathlib import Path

import camera_config
import engine as engine_mod
import feedback
import intent_brain
import pairing
import runtime_roles
import settings_acceptance
import settings_closure
import settings_completion_audit
import settings_control
import settings_evidence
import settings_live_checklist
import settings_live_marathon
import settings_offline_work
import settings_ownership
import settings_photo_inventory
import settings_preconditions
import settings_probe_plan
import settings_promotions
import settings_readiness
import settings_registry
import settings_remaining_gates
import settings_session_ledger
import settings_session_runs
import settings_sessions
import ux_validation
from engine import SHOTS_DIR, get_engine
from fastapi import Depends, FastAPI, Header, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse, Response, StreamingResponse
from starlette.background import BackgroundTask
from pydantic import BaseModel, Field, StrictInt

WEB_DIR = Path(__file__).resolve().parent / "web"
IMAGE_SUFFIXES = {".jpg", ".jpeg", ".png", ".webp", ".tif", ".tiff"}
CARD_VIEWABLE_SUFFIXES = {".jpg", ".jpeg", ".png"}
MAX_API_BODY_BYTES = 64 * 1024


def _cors_origins() -> list[str]:
    configured = os.getenv("CAMERA_AUTOPILOT_CORS_ORIGINS", "")
    origins = [origin.strip() for origin in configured.split(",") if origin.strip()]
    return origins or ["http://localhost:8000", "http://127.0.0.1:8000"]


API_TOKEN = os.getenv("CAMERA_AUTOPILOT_API_TOKEN", "").strip()


def _is_loopback_client(request: Request) -> bool:
    host = request.client.host if request.client else ""
    if host in {"localhost", "testclient"}:
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def _token_transport_is_secure(request: Request) -> bool:
    """Bearer credentials never cross plaintext non-loopback HTTP."""
    return request.url.scheme == "https" or _is_loopback_client(request)


def _check_api_token(
    request: Request,
    x_camera_autopilot_token: str | None = Header(default=None),
) -> None:
    """Apply the shared fail-closed bearer-token policy on every camera route."""
    if API_TOKEN:
        if x_camera_autopilot_token and hmac.compare_digest(x_camera_autopilot_token, API_TOKEN):
            if not _token_transport_is_secure(request):
                raise HTTPException(
                    status_code=403,
                    detail="HTTPS is required before sending the camera token over LAN",
                )
            return
        raise HTTPException(status_code=401, detail="camera token required")
    raise HTTPException(
        status_code=503,
        detail="set CAMERA_AUTOPILOT_API_TOKEN before using camera API",
    )


def _require_api_token(
    request: Request,
    x_camera_autopilot_token: str | None = Header(default=None),
) -> None:
    """Fail closed for camera control and private shot data unless a token is configured."""
    _check_api_token(request, x_camera_autopilot_token)


def _require_api_token_strict(
    request: Request,
    x_camera_autopilot_token: str | None = Header(default=None),
) -> None:
    """Require a configured token and encrypted transport for every camera recording request."""
    _check_api_token(request, x_camera_autopilot_token)


app = FastAPI(title="Camera Autopilot")


@app.middleware("http")
async def enforce_camera_api_boundaries(request: Request, call_next):
    """Authenticate every camera namespace request before route lookup or handler execution."""
    if request.url.path == "/status" or request.url.path.startswith("/api/camera/"):
        try:
            _check_api_token(request, request.headers.get("X-Camera-Autopilot-Token"))
        except HTTPException as exc:
            return JSONResponse(status_code=exc.status_code, content={"detail": exc.detail})
    if request.method in {"POST", "PUT", "PATCH", "DELETE"}:
        raw_length = request.headers.get("content-length")
        try:
            length = int(raw_length) if raw_length is not None else -1
        except ValueError:
            length = -1
        if raw_length is None and request.method == "DELETE":
            raw_length, length = "0", 0
        if raw_length is None:
            return JSONResponse(status_code=411, content={"detail": "Content-Length is required"})
        if length < 0 or length > MAX_API_BODY_BYTES:
            return JSONResponse(status_code=413, content={"detail": "request body exceeds API byte limit"})
        body = bytearray()
        async for chunk in request.stream():
            body.extend(chunk)
            if len(body) > MAX_API_BODY_BYTES:
                return JSONResponse(status_code=413, content={"detail": "request body exceeds API byte limit"})
        request._body = bytes(body)
    # Registry probes and writes share the same physical-camera exclusion as capture.
    if request.url.path.startswith("/api/camera/") and (
        request.method in {"POST", "PUT", "PATCH", "DELETE"}
        or request.query_params.get("live", "").lower() in {"true", "1", "on", "yes", "t", "y"}
    ):
        try:
            with _exclusive_camera():
                return await call_next(request)
        except HTTPException as exc:
            return JSONResponse(status_code=exc.status_code, content={"detail": exc.detail}, headers=exc.headers)
    return await call_next(request)
app.add_middleware(
    CORSMiddleware,
    allow_origins=_cors_origins(),
    allow_methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"],
    allow_headers=["Content-Type", "X-Camera-Autopilot-Token"],
)

engine = get_engine()
if SHOTS_DIR.is_symlink():
    raise ValueError("gallery root must not be a symlink")
SHOTS_DIR.mkdir(mode=0o700, parents=True, exist_ok=True)
os.chmod(SHOTS_DIR, 0o700)

from photo_lifecycle import PhotoLifecycle, RETENTION_SWEEP_SECONDS
from security_bounds import RequestBudget

_camera_lock = threading.Lock()
_capture_budget = RequestBudget(limit=12, window_seconds=60)
# Phone pairing: one active code (5 tries) behind a global budget for every /api/pair caller.
_pairing_codes = pairing.PairingCodes()
_pairing_budget = RequestBudget(limit=10, window_seconds=60)
MAX_PAIRING_BODY_BYTES = 256


def _selected_engine(request, *, require_serial: bool = True):
    model = getattr(request, "camera_model", None)
    port = getattr(request, "camera_port", None)
    serial = getattr(request, "camera_serial", None)
    if bool(model) != bool(port):
        raise HTTPException(status_code=400, detail="Camera model and USB port must be supplied together")
    # USB bodies are identified by serial below. The Wi-Fi engine is out of scope: it has no USB
    # port to re-enumerate and proves each downloaded file's identity itself (wifi_full_res).
    if engine.name == "real":
        # The server's CAMERA_SERIAL belongs to its CAMERA_MODEL/CAMERA_PORT; a request that
        # selects its own body must also name that body's serial.
        if not (model or port or serial):
            serial = getattr(engine, "camera_serial", None)
        model = model or getattr(engine, "camera_model", None)
        port = port or getattr(engine, "camera_port", None)
        if not model or not port:
            raise HTTPException(status_code=400, detail="Select both camera model and USB port")
        if require_serial:
            _require_serial(serial, model, port)
    if model or port:
        if not model or not port:
            raise HTTPException(status_code=400, detail="Camera model and USB port must be supplied together")
        if engine.name != "real":
            raise HTTPException(status_code=400, detail="USB selection requires the real USB engine")
        if model and model not in engine_mod.USB_CONTROL_MODELS:
            raise HTTPException(status_code=400, detail="Unsupported camera model")
        return engine_mod.RealEngine(camera_model=model, camera_port=port, camera_serial=serial)
    return engine


def _require_serial(serial, model, port) -> None:
    """USB ports re-enumerate; the serial is the only stable identity. No serial, no capture."""
    if not serial:
        from sonyremote import CameraIdentityError
        raise CameraIdentityError("Select the camera again: its USB serial is required before capture.",
                                  reason="serial_required", model=model, port=port)


def _with_restore_warning(content: dict, restore: dict | None) -> dict:
    """Attach a Fixed stack's meter restore receipt; when the pre-meter exposure was not put back,
    add the warning to detail too, because the web client displays only detail/error."""
    if restore:
        content["meter_restore"] = restore
        if restore.get("status") in ("failed", "skipped_unverified_body"):
            content["warning"] = (f"The exposure metering changed was not restored ({restore.get('reason')}); "
                                  "check the camera's ISO and shutter before the next shot.")
            content["detail"] = f"{content.get('detail', '')} Warning: {content['warning']}".strip()
    return content


def _identity_refusal(exc) -> JSONResponse | None:
    """A typed identity refusal (raised before any write or shutter), or None for other errors."""
    try:
        from sonyremote import CameraIdentityError
    except ImportError:  # a mock server without the USB package has no USB identity to refuse
        return None
    if not isinstance(exc, CameraIdentityError):
        return None
    long_state = engine_mod.long_refusal_restore_state(exc)
    if long_state:
        # Refused after the long settings were written (before any trigger): say whether the body
        # was put back, and warn in detail too because the web client shows only detail/error.
        content = {**exc.as_dict(), **long_state}
        if "warning" in content:
            content["detail"] = f"{content['detail']} Warning: {content['warning']}"
    else:
        # A Fixed stack refused after its meter photo also says whether the pre-meter exposure is back.
        content = _with_restore_warning(exc.as_dict(), getattr(exc, "meter_restore", None))
    return JSONResponse(status_code=400 if exc.reason == "serial_required" else 409, content=content)


def _is_camera_error(exc) -> bool:
    try:
        from sonyremote import CameraError
    except ImportError:
        return False
    return isinstance(exc, CameraError)


def _capture_precondition_refused(exc) -> bool:
    """A capture sonyremote refused before any trigger call (nothing fired)."""
    try:
        from sonyremote import CapturePreconditionError
    except ImportError:
        return False
    return isinstance(exc, CapturePreconditionError)


@contextmanager
def _exclusive_camera():
    retry = _capture_budget.check()
    if retry:
        raise HTTPException(status_code=429, detail="Camera request limit reached", headers={"Retry-After":str(int(retry)+1)})
    if not _camera_lock.acquire(blocking=False):
        raise HTTPException(status_code=409, detail="Camera is busy with another operation")
    try:
        yield
    finally:
        _camera_lock.release()


# A phone WebView may hold a cached copy of one asset while re-fetching another, which on
# 2026-09-17 left a real iPhone running a new index.html against a stale client.js: the page
# called an API the cached script did not define and never reached /status. These assets carry
# no fingerprint in their URL, so they must always be revalidated against their ETag.
WEB_ASSET_CACHE_CONTROL = "no-cache, must-revalidate"


def _web_asset(name: str, media_type: str | None = None) -> FileResponse:
    """Serve a web asset that clients must revalidate rather than reuse blindly."""
    return FileResponse(WEB_DIR / name, media_type=media_type,
                        headers={"Cache-Control": WEB_ASSET_CACHE_CONTROL})


@app.get("/client.js")
def client_script():
    return _web_asset("client.js", "text/javascript")


@app.get('/workflows.js')
def workflows_script():
    return _web_asset('workflows.js', 'text/javascript')


@app.get('/panorama')
def panorama_page():
    return _web_asset('panorama.html', 'text/html')


class ShootRequest(BaseModel):
    intent: str | None = Field(default=None, max_length=2000)
    variant_id: str | None = Field(default=None, max_length=64)
    camera_model: str | None = Field(default=None, max_length=100)
    camera_port: str | None = Field(default=None, max_length=64)
    camera_serial: str | None = Field(default=None, min_length=1, max_length=128)
    refine: bool = False
    stack_policy: dict | None = None


class SettingControlRequest(BaseModel):
    value: object
    route: str = "auto"
    dry_run: bool = True
    allow_behavior_probe: bool = False
    camera_model: str | None = Field(default=None, max_length=100)
    camera_port: str | None = Field(default=None, max_length=64)
    # Required for any live read or write: the OS USB serial is proven before any setting read or write.
    camera_serial: str | None = Field(default=None, min_length=1, max_length=128)


class PromotionApplyRequest(BaseModel):
    setting_id: str | None = None
    route: str | None = None
    action: str | None = None
    dry_run: bool = True
    allow_write: bool = False
    out_path: str | None = None


class SessionRunRequest(BaseModel):
    route: str
    setting_id: str | None = None
    phases: list[str] | None = None
    recommended_sequence: bool = False
    execute: bool = False
    allow_hardware: bool = False
    out_dir: str | None = None
    package_path: str | None = None
    observed_state: str | None = None
    observed_value: str | None = None
    phone_state: str | None = None
    observed_behavior: str | None = None
    target_setup: str | None = None
    smart_remote_method: str | None = None
    smart_remote_requested_value: str | None = None
    smart_remote_observed_before: str | None = None
    smart_remote_observed_after: str | None = None
    smart_remote_observed_behavior: str | None = None
    # The selected body: USB setting writes run only with its exact model, port and USB serial.
    camera_model: str | None = Field(default=None, max_length=100)
    camera_port: str | None = Field(default=None, max_length=64)
    camera_serial: str | None = Field(default=None, min_length=1, max_length=128)


class LiveMarathonRequest(BaseModel):
    routes: list[str] | None = None
    execute: bool = False
    allow_hardware: bool = False
    continue_on_block: bool = False
    out_dir: str | None = None
    session_out_dir: str | None = None
    package_path: str | None = None


class ReadinessRequest(BaseModel):
    route: str | None = None
    setting_id: str | None = None
    package_path: str | None = None


class RecordVideoRequest(BaseModel):
    intent: str | None = Field(default=None, max_length=2000)
    seconds: float = Field(default=10.0, ge=0.5, le=300)
    camera_model: str | None = None
    camera_port: str | None = None
    camera_serial: str | None = Field(default=None, min_length=1, max_length=128)


class IntentPreviewRequest(BaseModel):
    intent: str | None = None


@app.get("/")
def index() -> FileResponse:
    """The mobile autopilot UI (open this on your phone)."""
    return _web_asset("index.html")


@app.get("/status")
def status() -> dict:
    """Is the autopilot ready (camera attached, or mock)?"""
    result = engine.status()
    import runtime_resources
    result["resources"] = runtime_resources.report()
    from device_health import known_server_identity
    result["server_identity"] = known_server_identity()
    return result


_PAIRING_REFUSED = "pairing code not accepted"
_NO_STORE = {"Cache-Control": "no-store"}


def _print_pairing_code(code: str) -> None:
    """The Mac terminal only: stderr, never the logging system, never a response."""
    minutes = pairing.CODE_TTL_SECONDS // 60
    print(f"\n  Phone pairing code: {code}  (valid {minutes} minutes, single use)\n"
          "  On the phone: Device status and camera settings > Pair with code.\n",
          file=sys.stderr, flush=True)


@app.on_event("startup")
async def issue_startup_pairing_code():
    if API_TOKEN and pairing.enabled():
        _print_pairing_code(_pairing_codes.issue())


@app.post("/api/pair")
async def pair_phone(request: Request):
    """Trade the one-time code for the API token. Same answer for every refusal: 401."""
    if not _token_transport_is_secure(request):
        raise HTTPException(status_code=403, detail="HTTPS is required before pairing over LAN")
    # JSON only: a cross-site form or text/plain post cannot skip the CORS preflight.
    content_type = request.headers.get("content-type", "").split(";", 1)[0].strip().lower()
    if content_type != "application/json":
        raise HTTPException(status_code=415, detail="pairing expects application/json")
    retry_after = _pairing_budget.check()
    if retry_after:
        raise HTTPException(status_code=429, detail="too many pairing attempts",
                            headers={"Retry-After": str(max(1, math.ceil(retry_after)))})
    body = await request.body()
    try:
        payload = json.loads(body) if len(body) <= MAX_PAIRING_BODY_BYTES else None
    except (ValueError, RecursionError):
        payload = None
    candidate = payload.get("code") if isinstance(payload, dict) else None
    # Every well-formed or malformed attempt counts against the code's five tries.
    if API_TOKEN and pairing.enabled() and _pairing_codes.redeem(candidate):
        return JSONResponse({"token": API_TOKEN}, headers=_NO_STORE)
    raise HTTPException(status_code=401, detail=_PAIRING_REFUSED)


@app.post("/api/pair/new", dependencies=[Depends(_require_api_token)])
def new_pairing_code(request: Request):
    """Re-pair: a fresh code for a caller on this computer that already holds the token."""
    if not _is_loopback_client(request):
        raise HTTPException(status_code=403, detail="pairing codes are issued only on this computer")
    if not pairing.enabled():
        raise HTTPException(status_code=409, detail="pairing runs only in lan-proxy exposure")
    code = _pairing_codes.issue()
    return JSONResponse({"code": code, "expires_in_seconds": pairing.CODE_TTL_SECONDS}, headers=_NO_STORE)


@app.get('/device-health', dependencies=[Depends(_require_api_token)])
def host_health():
    from device_health import device_health
    result = device_health(SHOTS_DIR)
    if runtime_roles.role() == "control":
        # Split runtime: are the heavy-jobs worker and the perception service alive?
        import product_sessions
        result["runtime"] = runtime_roles.liveness(product_sessions.runner().root,
                                                   os.environ.get(runtime_roles.PERCEPTION_SOCKET_ENV))
    return result


class RadioPlanRequest(BaseModel):
    band: str = Field(default='2.4ghz', max_length=10)
    channel: StrictInt | None = None


@app.post('/device/radio-plan', dependencies=[Depends(_require_api_token)])
def radio_plan(request: RadioPlanRequest):
    from device_health import radio_configuration
    try:
        return {'configuration': radio_configuration(request.band, request.channel),
                'host_changes': False, 'access_point_started': False,
                'purpose': 'Candidate configuration for portable target validation'}
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


class PreviewRequest(BaseModel):
    camera_model: str = Field(max_length=100)
    camera_port: str = Field(max_length=64)


@app.post('/api/camera/preview', dependencies=[Depends(_require_api_token)])
def isolated_preview(request: PreviewRequest):
    # Namespace middleware retains camera ownership for the entire child lifetime.
    _selected_engine(request, require_serial=False)  # read-only: no setting write, no shutter
    from sonyremote.isolated_preview import capture_preview_isolated, IsolatedProbeError
    try:
        frame = capture_preview_isolated(request.camera_model, request.camera_port)
    except IsolatedProbeError as exc:
        raise HTTPException(status_code=503, detail={'code': exc.code, 'message': str(exc),
                                                   'retry_capture': False}) from exc
    return Response(frame.jpeg, media_type='image/jpeg', headers={
        'X-Preview-Sha256': frame.sha256, 'X-Camera-Model': frame.model,
        'Cache-Control': 'no-store'})


class CardListRequest(BaseModel):
    camera_model: str = Field(max_length=100)
    camera_port: str = Field(max_length=64)
    camera_serial: str | None = Field(default=None, min_length=1, max_length=128)


@app.post('/api/camera/card/list', dependencies=[Depends(_require_api_token)])
def camera_card_list(request: CardListRequest):
    from card_history import list_card, CardHistoryError
    try:
        _selected_engine(request)
        return list_card(request.camera_model, request.camera_port, serial=request.camera_serial)
    except CardHistoryError as exc:
        raise HTTPException(status_code=503, detail={'reason': exc.reason, 'message': str(exc),
                                                   'retry_capture': False}) from exc
    except RuntimeError as exc:
        if (refusal := _identity_refusal(exc)) is not None:
            return refusal
        raise


class CardImportRequest(BaseModel):
    camera_model: str = Field(max_length=100)
    camera_port: str = Field(max_length=64)
    camera_serial: str | None = Field(default=None, min_length=1, max_length=128)
    folder: str = Field(max_length=2048)
    name: str = Field(max_length=255)


def _card_import_job_id(job_id: str) -> str:
    if (not isinstance(job_id, str) or not job_id.startswith("card_") or len(job_id) != 37
            or any(character not in "0123456789abcdef" for character in job_id[5:])):
        raise HTTPException(status_code=404, detail="Card import not found")
    return job_id


@app.post('/api/camera/card/import', dependencies=[Depends(_require_api_token)])
def camera_card_import(request: CardImportRequest):
    from card_history import download_card_file, CardHistoryError
    try:
        _selected_engine(request)
        result = download_card_file(request.camera_model, request.camera_port, request.folder,
                                    request.name, SHOTS_DIR / 'card-imports',
                                    serial=request.camera_serial)
    except CardHistoryError as exc:
        raise HTTPException(status_code=503, detail={'reason': exc.reason, 'message': str(exc),
                                                     'retry_capture': False, 'card_deleted': False}) from exc
    except RuntimeError as exc:
        if (refusal := _identity_refusal(exc)) is not None:
            return refusal
        raise
    suffix = Path(result.get('image') or '').suffix.lower()
    viewable = suffix in CARD_VIEWABLE_SUFFIXES
    result['gallery_enrolled'] = False
    result['read_only'] = True
    result['card_deleted'] = False
    result['viewable'] = viewable
    job_id = result.get('job_id')
    result['url'] = f"/api/camera/card/imports/{job_id}/{result['image']}" if viewable and job_id else None
    return result


@app.get('/api/camera/card/imports/{job_id}/{name}', dependencies=[Depends(_require_api_token)])
def camera_card_import_file(job_id: str, name: str):
    import hashlib
    import json
    import stat
    job_id = _card_import_job_id(job_id)
    safe_name = Path(name).name
    if safe_name != name or Path(safe_name).suffix.lower() not in CARD_VIEWABLE_SUFFIXES:
        raise HTTPException(status_code=404, detail="Card import not found")
    root = SHOTS_DIR / 'card-imports' / job_id
    receipt_path = root / 'receipt.json'
    image_path = root / safe_name
    if root.is_symlink() or receipt_path.is_symlink() or image_path.is_symlink():
        raise HTTPException(status_code=404, detail="Card import not found")
    try:
        receipt = json.loads(receipt_path.read_text(encoding='utf-8'))
    except (OSError, json.JSONDecodeError) as exc:
        raise HTTPException(status_code=404, detail="Card import not found") from exc
    if receipt.get('image') != safe_name or receipt.get('job_id') != job_id:
        raise HTTPException(status_code=404, detail="Card import not found")
    try:
        fd = os.open(image_path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail="Card import not found") from exc
    stream = os.fdopen(fd, 'rb')
    try:
        info = os.fstat(stream.fileno())
        expected_bytes = receipt.get('bytes')
        from card_history import MAX_IMAGE_BYTES
        if (not stat.S_ISREG(info.st_mode) or info.st_size != expected_bytes
                or info.st_size > MAX_IMAGE_BYTES):
            raise HTTPException(status_code=409, detail="Card import changed after its receipt")
        if hashlib.file_digest(stream, 'sha256').hexdigest() != receipt.get('sha256'):
            raise HTTPException(status_code=409, detail="Card import changed after its receipt")
        stream.seek(0)
    except BaseException:
        stream.close()
        raise
    media = 'image/jpeg' if Path(safe_name).suffix.lower() in {'.jpg', '.jpeg'} else 'image/png'
    return StreamingResponse(iter(lambda: stream.read(1024 * 1024), b''), media_type=media,
        headers={'Content-Length': str(info.st_size), 'Cache-Control': 'no-store'},
        background=BackgroundTask(stream.close))


@app.get("/shots/{image_name}", dependencies=[Depends(_require_api_token)])
def shot_image(image_name: str) -> FileResponse:
    """Serve only shot image files; sidecar JSON stays private to /gallery."""
    safe_name = Path(image_name).name
    if safe_name != image_name or Path(safe_name).suffix.lower() not in IMAGE_SUFFIXES:
        raise HTTPException(status_code=404, detail="shot image not found")
    path = SHOTS_DIR / safe_name
    if path.is_symlink() or not path.is_file():
        raise HTTPException(status_code=404, detail="shot image not found")
    return FileResponse(path)


@app.get("/api/camera/settings")
def camera_settings(menu_group: str | None = None, status: str | None = None) -> dict:
    """Registry-backed control map for every photographed A7 II setting."""
    registry = settings_registry.load_registry()
    gate_report = settings_remaining_gates.build_report(registry=registry)
    gates_by_id = {row["setting_id"]: row for row in gate_report["gate_rows"]}
    settings = []
    for row in settings_registry.list_settings(
        menu_group=menu_group,
        status=status,
        registry=registry,
    ):
        item = dict(row)
        gate = gates_by_id.get(row["setting_id"])
        item["has_remaining_gate"] = gate is not None
        item["remaining_gate_category"] = gate["gate_category"] if gate else None
        item["next_concrete_command"] = gate["next_concrete_command"] if gate else None
        item["recommended_sequence_session_command"] = (
            gate["recommended_sequence_session_command"] if gate else None
        )
        settings.append(item)
    return {
        "summary": {
            **settings_registry.registry_summary(registry),
            "selected_settings": len(settings),
            "settings_with_remaining_gates": sum(
                1 for item in settings if item["has_remaining_gate"]
            ),
            "settings_with_next_concrete_command": sum(
                1 for item in settings if item["next_concrete_command"]
            ),
            "settings_with_recommended_sequence_session_command": sum(
                1 for item in settings if item["recommended_sequence_session_command"]
            ),
        },
        "settings": settings,
    }


@app.get("/api/camera/setting-probes")
def camera_setting_probes(
    setting_id: str | None = None,
    menu_group: str | None = None,
    status: str | None = None,
    route: str | None = None,
) -> dict:
    """Exact proof commands for remaining settings gaps, derived from the registry."""
    registry = settings_registry.load_registry()
    try:
        return settings_probe_plan.build_probe_report(
            registry,
            setting_id=setting_id,
            menu_group=menu_group,
            status=status,
            route=route,
        )
    except settings_registry.SettingNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@app.get("/api/camera/setting-preconditions")
def camera_setting_preconditions(
    setting_id: str | None = None,
    status: str | None = None,
    greyed_only: bool = False,
    gated_only: bool = False,
) -> dict:
    """Structured unlock guidance for greyed/state-gated settings."""
    registry = settings_registry.load_registry()
    try:
        if setting_id:
            return {
                "summary": {"settings": 1},
                "settings": [settings_preconditions.solve_setting(setting_id, registry)],
            }
        return settings_preconditions.build_precondition_report(
            registry,
            status=status,
            greyed_only=greyed_only,
            gated_only=gated_only,
        )
    except settings_registry.SettingNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@app.get("/api/camera/setting-evidence")
def camera_setting_evidence(
    setting_id: str | None = None,
    menu_group: str | None = None,
    route: str | None = None,
) -> dict:
    """Latest artifact-review evidence mapped back onto registry rows."""
    registry = settings_registry.load_registry()
    try:
        return settings_evidence.build_evidence_report(
            setting_id=setting_id,
            menu_group=menu_group,
            route=route,
            registry=registry,
        )
    except settings_registry.SettingNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@app.get("/api/camera/setting-evidence/{setting_id}")
def camera_setting_evidence_detail(setting_id: str) -> dict:
    """Latest artifact-review evidence for one photographed setting."""
    registry = settings_registry.load_registry()
    try:
        return settings_evidence.evidence_for_setting(setting_id, registry=registry)
    except settings_registry.SettingNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@app.get("/api/camera/settings-closure")
def camera_settings_closure(sample_limit: int = 4) -> dict:
    """Operator-ready closure map for every remaining settings-control gap."""
    registry = settings_registry.load_registry()
    bounded_limit = max(0, min(sample_limit, 50))
    return settings_closure.build_report(registry=registry, sample_limit=bounded_limit)


@app.get("/api/camera/settings-gates")
def camera_settings_gates(
    setting_id: str | None = None,
    menu_group: str | None = None,
    status: str | None = None,
    gate_category: str | None = None,
    limit: int | None = None,
) -> dict:
    """Structured remaining hardware/state/admin gates with exact per-setting proof commands."""
    registry = settings_registry.load_registry()
    bounded_limit = None if limit is None else max(0, min(limit, 500))
    try:
        return settings_remaining_gates.build_report(
            registry=registry,
            setting_id=setting_id,
            menu_group=menu_group,
            status=status,
            gate_category=gate_category,
            limit=bounded_limit,
        )
    except settings_registry.SettingNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@app.get("/api/camera/settings-acceptance")
def camera_settings_acceptance(route: str | None = None, package_path: str | None = None) -> dict:
    """Compact acceptance state and next live-session commands for A7 II settings control."""
    registry = settings_registry.load_registry()
    try:
        return settings_acceptance.build_report(
            registry=registry,
            route=route,
            package_path=package_path,
        )
    except settings_registry.SettingNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@app.get("/api/camera/settings-completion-audit")
def camera_settings_completion_audit(package_path: str | None = None) -> dict:
    """Requirement-level completion audit for the A7 II full settings-control plan."""
    registry = settings_registry.load_registry()
    return settings_completion_audit.build_report(registry=registry, package_path=package_path)


@app.get("/api/camera/settings-photo-inventory")
def camera_settings_photo_inventory(include_registry_sources: bool = True) -> dict:
    """Reconcile the user-provided photo folder against registry and route-context evidence."""
    registry = settings_registry.load_registry()
    return settings_photo_inventory.build_report(
        registry=registry,
        include_registry_sources=include_registry_sources,
        redact_identifiers=True,
    )


@app.get("/api/camera/settings-live-checklist")
def camera_settings_live_checklist(
    route: str | None = None,
    package_path: str | None = None,
    limit_direct_commands: int = 3,
) -> dict:
    """Operator checklist for the next live settings-evidence sessions."""
    if route is not None and route not in settings_closure.ROUTE_ORDER:
        raise HTTPException(status_code=400, detail=f"unknown route: {route}")
    registry = settings_registry.load_registry()
    return settings_live_checklist.build_report(
        registry=registry,
        route=route,
        package_path=package_path,
        direct_command_limit=max(0, min(limit_direct_commands, 100)),
    )


@app.get("/api/camera/settings-offline-work")
def camera_settings_offline_work(route: str | None = None, package_path: str | None = None) -> dict:
    """Camera-free work queue plus the routes that still truly require live proof."""
    if route is not None and route not in settings_closure.ROUTE_ORDER:
        raise HTTPException(status_code=400, detail=f"unknown route: {route}")
    registry = settings_registry.load_registry()
    return settings_offline_work.build_report(
        registry=registry,
        route=route,
        package_path=package_path,
    )


@app.get("/api/camera/settings-ownership")
def camera_settings_ownership(
    setting_id: str | None = None,
    menu_group: str | None = None,
    status: str | None = None,
    route: str | None = None,
    greyed_only: bool = False,
    limit: int | None = None,
) -> dict:
    """Per-setting ownership, blockers, and exact next proof commands."""
    registry = settings_registry.load_registry()
    try:
        return settings_ownership.build_report(
            registry=registry,
            setting_id=setting_id,
            menu_group=menu_group,
            status=status,
            route=route,
            greyed_only=greyed_only,
            limit=limit,
        )
    except settings_registry.SettingNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@app.get("/api/camera/setting-promotions")
def camera_setting_promotions(
    setting_id: str | None = None,
    route: str | None = None,
    action: str | None = None,
    applicable_only: bool = False,
    limit: int | None = None,
) -> dict:
    """Read-only promotion plan from latest review artifacts into registry updates."""
    registry = settings_registry.load_registry()
    try:
        return settings_promotions.build_promotion_plan(
            registry=registry,
            setting_id=setting_id,
            route=route,
            action=action,
            applicable_only=applicable_only,
            limit=limit,
        )
    except settings_registry.SettingNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


def _repo_bounded_path(path_value: str | None, *, field_name: str = "path") -> Path | None:
    if path_value is None:
        return None
    root = settings_registry.REPO_ROOT.resolve()
    path = Path(path_value)
    target = path if path.is_absolute() else root / path
    target = target.resolve()
    try:
        target.relative_to(root)
    except ValueError as exc:
        raise HTTPException(status_code=403, detail=f"{field_name} must stay inside this repository") from exc
    return target


@app.post("/api/camera/setting-promotions/apply")
def apply_camera_setting_promotions(request: PromotionApplyRequest) -> dict:
    """Dry-run or explicitly write behavior-proven registry promotion updates."""
    if not request.dry_run:
        if not request.allow_write:
            raise HTTPException(status_code=403, detail="registry writes require allow_write=true")
        if not request.out_path:
            raise HTTPException(status_code=403, detail="registry writes require an explicit out_path")
    try:
        return settings_promotions.apply_promotion_plan(
            setting_id=request.setting_id,
            route=request.route,
            action=request.action,
            dry_run=request.dry_run,
            out_path=_repo_bounded_path(request.out_path, field_name="out_path"),
        )
    except settings_registry.SettingNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@app.get("/api/camera/settings-sessions")
def camera_settings_sessions(
    route: str | None = None,
    setting_id: str | None = None,
) -> dict:
    """Route-centric live proof playbooks for hardware settings sessions."""
    if route in settings_registry.DISABLED_ROUTES:
        raise HTTPException(status_code=400, detail=f"route permanently disabled: {route}")
    registry = settings_registry.load_registry()
    try:
        return settings_sessions.build_session_playbook(
            registry=registry,
            route=route,
            setting_id=setting_id,
        )
    except settings_registry.SettingNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@app.get("/api/camera/settings-readiness")
def camera_settings_readiness(
    route: str | None = None,
    setting_id: str | None = None,
    package_path: str | None = None,
) -> dict:
    """Local/package/operator readiness for remaining live proof sessions."""
    if route is not None and route not in settings_closure.ROUTE_ORDER:
        raise HTTPException(status_code=400, detail=f"unknown route: {route}")
    registry = settings_registry.load_registry()
    try:
        return settings_readiness.build_readiness_report(
            registry=registry,
            route=route,
            setting_id=setting_id,
            package_path=package_path,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except settings_registry.SettingNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@app.get("/api/camera/settings-sessions/ledger")
def camera_settings_session_ledger(
    route: str | None = None,
    setting_id: str | None = None,
    out_dir: str | None = None,
    limit_recent: int = 20,
) -> dict:
    """Latest audited live-session outcomes grouped by route."""
    if limit_recent < 1 or limit_recent > 200:
        raise HTTPException(status_code=400, detail="limit_recent must be between 1 and 200")
    try:
        return settings_session_ledger.build_ledger(
            out_dir=_repo_bounded_path(out_dir, field_name="out_dir"),
            route=route,
            setting_id=setting_id,
            limit_recent=limit_recent,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.post("/api/camera/settings-sessions/run")
def run_camera_settings_session(request: SessionRunRequest) -> dict:
    """Create or execute an audited route-session manifest with hardware guardrails."""
    if request.route not in settings_closure.ROUTE_ORDER:
        raise HTTPException(status_code=400, detail=f"unknown route: {request.route}")
    if request.route in settings_registry.DISABLED_ROUTES:
        raise HTTPException(status_code=400, detail=f"route permanently disabled: {request.route}")
    out_dir = _repo_bounded_path(request.out_dir, field_name="out_dir")
    package_path = _repo_bounded_path(request.package_path, field_name="package_path")
    try:
        return settings_session_runs.run_session(
            route=request.route,
            setting_id=request.setting_id,
            phases=request.phases,
            recommended_sequence=request.recommended_sequence,
            execute=request.execute,
            allow_hardware=request.allow_hardware,
            out_dir=out_dir,
            package_path=package_path,
            observed_state=request.observed_state,
            observed_value=request.observed_value,
            phone_state=request.phone_state,
            observed_behavior=request.observed_behavior,
            target_setup=request.target_setup,
            smart_remote_method=request.smart_remote_method,
            smart_remote_requested_value=request.smart_remote_requested_value,
            smart_remote_observed_before=request.smart_remote_observed_before,
            smart_remote_observed_after=request.smart_remote_observed_after,
            smart_remote_observed_behavior=request.smart_remote_observed_behavior,
            camera_model=request.camera_model,
            camera_port=request.camera_port,
            camera_serial=request.camera_serial,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except settings_registry.SettingNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@app.post("/api/camera/settings-live-marathon/run")
def run_camera_settings_live_marathon(request: LiveMarathonRequest) -> dict:
    """Plan or execute all remaining route-session manifests with dry-run-first guardrails."""
    if request.routes:
        unknown = sorted(set(request.routes) - set(settings_closure.ROUTE_ORDER))
        if unknown:
            raise HTTPException(status_code=400, detail=f"unknown route(s): {', '.join(unknown)}")
    try:
        return settings_live_marathon.run_marathon(
            routes=request.routes,
            execute=request.execute,
            allow_hardware=request.allow_hardware,
            continue_on_block=request.continue_on_block,
            out_dir=_repo_bounded_path(request.out_dir, field_name="out_dir"),
            session_out_dir=_repo_bounded_path(request.session_out_dir, field_name="session_out_dir"),
            package_path=_repo_bounded_path(request.package_path, field_name="package_path"),
            registry=settings_registry.load_registry(),
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except settings_registry.SettingNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@app.get("/api/camera/setup-checklist")
def camera_setup_checklist() -> dict:
    """Registry-backed one-time camera setup guidance for physical/deep-menu gates."""
    items = [
        {"label": label, "where": where, "target": target, "why": why}
        for label, where, target, why in camera_config.one_time_checklist()
    ]
    return {
        "summary": {"items": len(items), "source": "settings_registry"},
        "items": items,
    }


@contextmanager
def _manual_camera(model, port, serial, journal=None, evidence=None):
    """Bind registry USB controls to an explicit body proven by its USB serial; never silently
    choose one. Ports re-enumerate, so the serial is checked by the OS before any read or write."""
    import re
    import time
    import uuid
    from sonyremote import Camera, PlaceholderSettingsError, read_settings_checked
    from stack_session import _atomic_json
    if engine.name != "real" or model not in engine_mod.USB_CONTROL_MODELS or not re.fullmatch(r"usb:[0-9]+,[0-9]+", port or ""):
        raise HTTPException(status_code=400, detail="Live USB controls require the real engine and an exact supported camera model/port")
    if model != "Sony ILCE-7M2 (Control)":
        raise HTTPException(status_code=409, detail="This settings registry is A7 II-specific; its behavior proofs do not authorize another body's controls")
    _require_serial(serial, model, port)  # CameraIdentityError(serial_required) before any journal or USB
    receipt = {"camera_model": model, "camera_port": port, "camera_serial": serial, "capture_attempts": 0,
               "status": "opening", "created_at": time.time()}
    record = None
    if journal is not None:
        directory = SHOTS_DIR / "manual-controls"
        if directory.is_symlink():
            raise HTTPException(status_code=409, detail="manual control journal is unavailable")
        directory.mkdir(mode=0o700, exist_ok=True)
        record = directory / (uuid.uuid4().hex + ".json")
        receipt["operation_id"] = record.stem
        receipt["request"] = journal
        _atomic_json(record, receipt)
    try:
        engine_mod._free_usb()
        with Camera(model=model, port=port, serial=serial) as camera:
            identity = camera.info()
            if (identity.model, identity.port) != (model, port):
                raise HTTPException(status_code=409, detail="Camera identity changed before manual control")
            receipt["serial_verification"] = getattr(camera, "serial_verification", None)
            # A placeholder read (f/0, ISO 0, 65535/65535) is never recorded as the prior state.
            receipt["before"], before_read = read_settings_checked(camera, purpose="manual control before")
            if before_read:
                receipt["before_read"] = before_read
            receipt["status"] = "operation_started"
            if record:
                _atomic_json(record, receipt)
            try:
                yield camera
                receipt["status"] = "completed"
            finally:
                try:
                    receipt["after"] = read_settings_checked(camera, purpose="manual control after")[0]
                except PlaceholderSettingsError as exc:
                    receipt["after"], receipt["after_read_refused"] = None, exc.as_dict()
    except BaseException as exc:
        receipt.update(status="failed", error=str(exc))
        raise
    finally:
        if record:
            receipt["updated_at"] = time.time()
            _atomic_json(record, receipt)
        if evidence is not None:
            evidence.update(receipt)


@app.get("/api/camera/settings/{setting_id}")
def camera_setting(setting_id: str, live: bool = False, route: str = "auto",
                   camera_model: str | None = None, camera_port: str | None = None,
                   camera_serial: str | None = None) -> dict:
    """One setting's ownership, current value, route hypotheses, proofs, and next action.

    Without live=true this reads only the registry (no camera, no serial needed). A live read
    opens the selected body only after its USB serial (`camera_serial`) is proven at the port."""
    try:
        registry = settings_registry.load_registry()
        row = dict(settings_registry.get_setting(setting_id, registry))
        read_result = settings_control.read_setting(setting_id, live=live, route=route, registry=registry,
            camera_factory=lambda: _manual_camera(camera_model, camera_port, camera_serial))
        gate_report = settings_remaining_gates.build_report(registry=registry, setting_id=setting_id)
        remaining_gate = gate_report["gate_rows"][0] if gate_report["gate_rows"] else None
        row["current"] = read_result["current"]
        row["current_route"] = read_result["route"]
        row["precondition_solution"] = settings_preconditions.solve_setting(setting_id, registry)
        row["evidence_reviews"] = settings_evidence.evidence_for_setting(setting_id, registry=registry)["reviews"]
        row["remaining_gate"] = remaining_gate
        row["exact_next_commands"] = remaining_gate["exact_next_commands"] if remaining_gate else []
        row["next_concrete_command"] = (
            remaining_gate.get("next_concrete_command") if remaining_gate else None
        )
        row["recommended_sequence_session_command"] = (
            remaining_gate.get("recommended_sequence_session_command") if remaining_gate else None
        )
        return row
    except settings_registry.SettingNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except settings_registry.SettingControlUnavailable as exc:
        raise HTTPException(status_code=501, detail=str(exc)) from exc
    except RuntimeError as exc:
        if (refusal := _identity_refusal(exc)) is not None:
            return refusal  # the body at the port was not proven: nothing was read
        if getattr(exc, "reason", None) == "placeholder_settings":
            return JSONResponse(status_code=409, content=exc.as_dict())  # wake the body and retry
        if _is_camera_error(exc):
            raise HTTPException(status_code=503, detail=str(exc)) from exc  # e.g. the port is gone
        raise


@app.post("/api/camera/settings/{setting_id}")
def plan_camera_setting_control(setting_id: str, request: SettingControlRequest) -> dict:
    """Plan or attempt a setting write with registry-backed guardrails.

    A dry run touches no camera. A live write (dry_run=false) goes only to a USB body and needs
    `camera_serial`; the body at the port is proven by its OS USB serial before any setting read or
    write, and a mismatch writes nothing. Wi-Fi and raw-PTP setters cannot prove which body they
    reach, so this route refuses them live."""
    try:
        if request.allow_behavior_probe and not request.dry_run:
            raise HTTPException(
                status_code=403,
                detail=(
                    "Live behavior probes are not allowed through /api/camera/settings/{setting_id}; "
                    "use /api/camera/settings-sessions/run with execute=true and allow_hardware=true "
                    "so the proof is logged."
                ),
            )
        def unprovable(route_name):
            # Reached only after every registry guard passed and a non-USB write would start.
            def refuse(*args, **kwargs):
                # Every live write names its body first: a missing serial is the structured
                # serial_required refusal, whichever backend the route resolved to.
                _require_serial(request.camera_serial, request.camera_model, request.camera_port)
                raise HTTPException(status_code=409, detail=(
                    f"{setting_id} would be written over {route_name}, which cannot prove the camera's "
                    "USB serial; live writes through this route go only to a serial-proven USB body. "
                    "Nothing was written."))
            return refuse
        manual_evidence = {}
        result = settings_control.apply_setting(
            setting_id,
            request.value,
            route=request.route,
            dry_run=request.dry_run,
            allow_behavior_probe=request.allow_behavior_probe,
            camera_factory=lambda: _manual_camera(request.camera_model, request.camera_port, request.camera_serial,
                {"setting_id": setting_id, "value": request.value, "route": "usb_gphoto"}, manual_evidence),
            smart_remote_factory=unprovable("smart_remote_wifi"),
            raw_ptp_runner=unprovable("raw_ptp"),
        )
        plan = result.get("control_plan", {})
        import re
        identity_bound = (engine.name == "real" and request.camera_model == "Sony ILCE-7M2 (Control)"
                          and bool(re.fullmatch(r"usb:[0-9]+,[0-9]+", request.camera_port or ""))
                          and bool(request.camera_serial))
        result["camera_identity"] = {"model": request.camera_model, "port": request.camera_port,
                                     "serial": request.camera_serial, "explicit_selection": identity_bound}
        result["identity_bound_live_write"] = bool(identity_bound and plan.get("live_backend") == "usb_gphoto"
            and plan.get("can_attempt_live_write") and not plan.get("requires_behavior_proof"))
        if manual_evidence:
            result["manual_control_receipt"] = manual_evidence
            result["verified"] = manual_evidence.get("status") == "completed"
        return result
    except settings_registry.SettingNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except settings_registry.SettingGuardError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc
    except settings_registry.SettingBehaviorProofRequired as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except settings_registry.SettingControlUnavailable as exc:
        raise HTTPException(status_code=501, detail=str(exc)) from exc
    except RuntimeError as exc:
        if (refusal := _identity_refusal(exc)) is not None:
            return refusal  # refused before any write: nothing on the camera changed
        if getattr(exc, "reason", None) == "placeholder_settings":
            return JSONResponse(status_code=409, content=exc.as_dict())  # wake the body and retry
        if _is_camera_error(exc):
            raise HTTPException(status_code=503, detail=str(exc)) from exc  # e.g. the port is gone
        raise


@app.post("/intent/preview")
def intent_preview(request: IntentPreviewRequest) -> dict:
    """Preview the spoken vibe as a mood card without touching the camera."""
    return intent_brain.preview(request.intent)


@app.post("/intent/variants")
def intent_variants(request: IntentPreviewRequest) -> dict:
    """Preview same-scene mood variants without touching the camera."""
    return intent_brain.variants(request.intent)


def _uncertain_capture_response(request, content: dict, *, route: str = "/shoot") -> JSONResponse:
    """The 409 for a capture that may have fired, with its evidence kept beside the store.

    The body (and the camera selection) is written to <store>/uncertain/ and named in
    `uncertain_receipt`; a failed write is named in `uncertain_receipt_error` and never changes
    the outcome. Diagnostics only: nothing here retries or fires."""
    import uncertain_receipts
    try:
        content = uncertain_receipts.json_safe(content)
    except Exception as error:  # noqa: BLE001 — evidence never changes the uncertain answer
        # The evidence cannot be encoded; the outcome must still be answered as uncertain.
        content = {key: content.get(key) for key in ("detail", "may_have_fired", "retry_capture", "capture_path")}
        content = {**uncertain_receipts.json_safe(content), "transport_receipt": None,
                   "evidence_error": type(error).__name__}
    try:
        content["uncertain_receipt"] = uncertain_receipts.persist(SHOTS_DIR, {
            "route": route, "response": content,
            "camera": {"model": request.camera_model, "port": request.camera_port,
                       "serial": request.camera_serial}})
    except Exception as error:  # noqa: BLE001 — a failed write never changes the uncertain answer
        content["uncertain_receipt_error"] = f"{type(error).__name__}: {uncertain_receipts.json_safe(error)}"
    return JSONResponse(status_code=409, content=content)


def _host_camera_preflight() -> dict | None:
    """Running ImageCapture client apps (Preview, Photos, Image Capture) that can take the USB
    body mid-capture; None unless the USB engine is in use. A warning only: never refuses."""
    if engine.name != "real":
        return None
    import host_camera_clients
    return host_camera_clients.running_clients()


def _with_host_camera_clients(response, preflight: dict | None):
    """A copy of a session/job response carrying the preflight and, when an app is running,
    its one warning line (`host_camera_warning`). Nothing is persisted."""
    if preflight is None or not isinstance(response, dict):
        return response
    import host_camera_clients
    response = {**response, "host_camera_clients": preflight}
    if line := host_camera_clients.summary_line(preflight):
        response["host_camera_warning"] = line
    return response


def _capture_failure_response(request, exc: RuntimeError, preflight: dict | None, *, route: str = "/shoot"):
    """The answer to a capture that failed: /shoot's, shared by /sessions/meter-plan (Session
    meter-first), so a refused, precondition-refused or uncertain meter frame is answered the same way.
    An uncertain outcome is a 409 with its receipt kept; nothing here retries or fires."""
    if (refusal := _identity_refusal(exc)) is not None:
        return refusal
    if getattr(exc, "reason", None) == "placeholder_settings":
        # The body answered placeholder exposure; the refused step fired nothing. An earlier
        # meter frame of this request is named (earlier_capture_path), never re-shot.
        content = _with_restore_warning({**exc.as_dict(), "retry_capture": False},
                                        getattr(exc, "meter_restore", None))
        return JSONResponse(status_code=409, content=content)
    long_refused = getattr(exc, "final_shutter_not_issued", False)
    if _capture_precondition_refused(exc) or long_refused:
        # Refused before any trigger: nothing fired for this step (#72's precondition refusal,
        # or the long exposure refused before its trigger with only the meter frame taken).
        # Metering may have changed ISO or the shutter first; the engine put them back and
        # reports whether that worked (meter_restore, with a warning in detail on failure).
        restore = getattr(exc, "meter_restore", None)
        content = {"detail": str(exc), "may_have_fired": False, "retry_capture": False}
        if long_refused:
            content.update(final_shutter_issued=False,
                           capture_path=str(getattr(exc, "capture_path", "") or ""),
                           focus_policy=getattr(exc, "focus_policy", None),
                           transport_receipt=getattr(exc, "transport_receipt", None))
        if restore:
            content["meter_restore"] = restore
            if restore.get("status") == "failed":
                content["warning"] = ("The exposure changed for metering could not be restored "
                                      f"({restore.get('reason')}); check the camera's ISO and shutter.")
                # The web client shows only detail/error, so the warning must be in it.
                content["detail"] = f"{content['detail']} Warning: {content['warning']}"
        return JSONResponse(status_code=503, content=content)
    if getattr(exc, "retry_safe", True) is False:
        warning = _with_host_camera_clients({}, preflight)
        if warning.get("host_camera_warning"):
            warning["detail"] = f"{exc} {warning['host_camera_warning']}"
        return _uncertain_capture_response(request, {"detail": str(exc), "may_have_fired": True,
            "retry_capture": False, "capture_path": str(getattr(exc, "capture_path", "") or ""),
            "focus_policy": getattr(exc, "focus_policy", None),
            "transport_receipt": getattr(exc, "transport_receipt", None),
            # A Fixed stack that stopped after its meter photo: was the pre-meter exposure put back?
            **({"meter_restore": exc.meter_restore} if getattr(exc, "meter_restore", None) else {}),
            # Exposures of this request known to have fired before the uncertain step.
            **({"prior_exposures": exc.prior_exposures}
               if getattr(exc, "prior_exposures", None) is not None else {}),
            **({"wrong_body_possible": True, "identity": exc.identity} if getattr(exc, "identity", None) else {}),
            **warning}, route=route)
    restore = getattr(exc, "meter_restore", None)
    if restore:
        # Refused before or without any uncertain shutter, after metering changed ISO or the
        # shutter (e.g. live shutter choices unavailable): say whether the exposure was put back
        # (a failed restore is a warning in detail too).
        return JSONResponse(status_code=503, content=_with_restore_warning(
            {"detail": str(exc), "may_have_fired": False, "retry_capture": False}, restore))
    raise HTTPException(status_code=503, detail=str(exc)) from exc


@app.post("/shoot", dependencies=[Depends(_require_api_token)])
def shoot(request: ShootRequest) -> dict:
    """Capture on the explicitly selected body, with optional bounded critique/reshoot."""
    import product_capture
    preflight = _host_camera_preflight()
    try:
        with _exclusive_camera():
            shot = product_capture.shoot(_selected_engine(request), request.intent,
                                         variant_id=request.variant_id, refine=request.refine,
                                         stack_policy=request.stack_policy)
        if preflight is not None:
            import host_camera_clients
            shot.evidence["host_camera_clients"] = preflight
            if line := host_camera_clients.summary_line(preflight):
                shot.summary = f"{shot.summary} {line}"
        return asdict(shot)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except NotImplementedError as exc:
        raise HTTPException(status_code=501, detail=str(exc)) from exc
    except RuntimeError as exc:
        return _capture_failure_response(request, exc, preflight)


@app.get("/gallery", dependencies=[Depends(_require_api_token)])
def gallery() -> list[dict]:
    """All persisted shots, newest-first, with their record fields + thumbs verdict."""
    import product_sessions
    records = product_sessions.project_delivery_durability(engine_mod.gallery_records())
    verdicts = feedback.verdict_index({Path(row["image"]).name for row in records})
    for row in records:
        row["verdict"] = verdicts.get(Path(row["image"]).name)
    return records


@app.get("/review")
def review() -> FileResponse:
    """The review/share gallery surface (open this on your phone)."""
    return _web_asset("review.html")


@app.get("/validation")
def validation() -> FileResponse:
    """The anonymous beginner-study response form (open this on the test phone)."""
    return _web_asset("validation.html")


@app.post("/record", dependencies=[Depends(_require_api_token_strict)])
def record_video(request: RecordVideoRequest) -> dict:
    """Record a supported native movie to the SD card; no live exposure adjustment."""
    try:
        selected = _selected_engine(request)
    except RuntimeError as exc:
        if (refusal := _identity_refusal(exc)) is not None:
            return refusal
        raise
    if not hasattr(selected, "record"):
        # The Wi-Fi engine has no proven movie-record path (Smart Remote exposes none; CRSDK is
        # hardware-gated). Refuse honestly instead of 500-ing on the missing method.
        raise HTTPException(
            status_code=501,
            detail=f"video recording is not supported by the {engine.name} engine yet",
        )
    from sonyremote import RecordingStopUncertain
    try:
        with _exclusive_camera():
            return asdict(selected.record(request.intent, request.seconds))
    except RecordingStopUncertain as exc:
        return JSONResponse(status_code=409, content={"detail": str(exc),
            "may_still_be_recording": True, "retry_record": False, "quarantined": True})
    except NotImplementedError as exc:
        raise HTTPException(status_code=501, detail=str(exc)) from exc
    except RuntimeError as exc:
        if (refusal := _identity_refusal(exc)) is not None:
            return refusal
        raise HTTPException(status_code=503, detail=str(exc)) from exc


class FeedbackRequest(BaseModel):
    image: str = Field(max_length=250)
    verdict: str = Field(pattern="^(up|down|comment)$")
    intent: str | None = Field(default=None, max_length=2000)
    comment: str | None = Field(default=None, max_length=2000)


class UXValidationSessionRequest(BaseModel):
    consent_given: bool
    magic_rating: int | None = None
    would_use: str = "unsure"
    confusion_area: str = "none"
    confusion_note: str | None = None
    interface: str = "web"
    experience_mode: str = "live_capture"
    capture_succeeded: bool = True
    intent_provided: bool = False
    duration_seconds: float | None = None
    photo_reference: str | None = None
    completion_status: str = "completed"
    study_id: str = ux_validation.DEFAULT_STUDY_ID


@app.post("/feedback", dependencies=[Depends(_require_api_token)])
def feedback_route(request: FeedbackRequest) -> dict:
    """Log ordinary thumbs feedback on a shot; formal UX sessions use /api/ux-validation."""
    try:
        return feedback.log_feedback(intent=request.intent, verdict=request.verdict, image=request.image,
                                     **({"comment": request.comment} if request.comment is not None else {}))
    except ValueError as exc:
        raise HTTPException(status_code=429, detail=str(exc)) from exc


@app.post("/api/ux-validation/sessions", dependencies=[Depends(_require_api_token)])
def record_ux_validation_session(request: UXValidationSessionRequest) -> dict:
    """Save one anonymous, structured beginner-study session."""

    try:
        row = ux_validation.record_session(
            consent_given=request.consent_given,
            magic_rating=request.magic_rating,
            would_use=request.would_use,
            confusion_area=request.confusion_area,
            confusion_note=request.confusion_note,
            interface=request.interface,
            experience_mode=request.experience_mode,
            capture_succeeded=request.capture_succeeded,
            intent_provided=request.intent_provided,
            duration_seconds=request.duration_seconds,
            photo_reference=request.photo_reference,
            completion_status=request.completion_status,
            study_id=request.study_id,
        )
    except ux_validation.UXValidationError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return {
        "saved": True,
        "session_id": row["session_id"],
        "study_id": row["study_id"],
        "recorded_at": row["recorded_at"],
        "completion_status": row["completion_status"],
        "gate_eligible": row["gate_eligible"],
        "privacy": {
            "raw_intent_stored": False,
            "ip_address_stored": False,
            "user_agent_stored": False,
            "direct_identifier_requested": False,
        },
    }


@app.get("/api/ux-validation/report", dependencies=[Depends(_require_api_token)])
def ux_validation_report(study_id: str = ux_validation.DEFAULT_STUDY_ID) -> dict:
    """Return aggregate study status only; never return session prose or photo references."""

    try:
        return ux_validation.build_report(study_id=study_id)
    except ux_validation.UXValidationError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@app.delete(
    "/api/ux-validation/sessions/{session_id}",
    dependencies=[Depends(_require_api_token)],
)
def delete_ux_validation_session(session_id: str) -> dict:
    """Honor participant withdrawal by physically removing the matching local row."""

    try:
        deleted = ux_validation.delete_session(session_id)
    except ux_validation.UXValidationError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    if not deleted:
        raise HTTPException(status_code=404, detail="validation session not found")
    return {"deleted": True, "session_id": session_id}


class EditRequest(BaseModel):
    image: str = Field(max_length=250)
    exposure_ev: float = Field(default=0, ge=-3, le=3)
    crop: list[float] | None = None


class RetentionRequest(BaseModel):
    days: StrictInt | None = Field(default=None, ge=1, le=3650)


class DeleteAllRequest(BaseModel):
    confirm: str = ""


def _photo_action(fn):
    try:
        return fn(PhotoLifecycle(SHOTS_DIR))
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail="Photo not found") from exc
    except (ValueError, OSError) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.get("/shots/{image}/cards", dependencies=[Depends(_require_api_token)])
def photo_cards(image: str):
    return _photo_action(lambda photos: photos.cards(image))


@app.get("/thumbnails/{image}", dependencies=[Depends(_require_api_token)])
def photo_thumbnail(image: str):
    return FileResponse(_photo_action(lambda photos: photos.thumbnail(image)), media_type="image/jpeg")


@app.delete("/photos/{image}", dependencies=[Depends(_require_api_token)])
def delete_photo(image: str):
    return _photo_action(lambda photos: photos.delete(image))


@app.delete("/photos", dependencies=[Depends(_require_api_token)])
def delete_all_photos(request: DeleteAllRequest):
    return _photo_action(lambda photos: photos.delete_all(confirm=request.confirm))


@app.get("/retention", dependencies=[Depends(_require_api_token)])
def get_retention():
    return _photo_action(lambda photos: photos.retention())


@app.put("/retention", dependencies=[Depends(_require_api_token)])
def set_retention(request: RetentionRequest):
    return _photo_action(lambda photos: photos.set_retention(request.days))


@app.post("/edit", dependencies=[Depends(_require_api_token)])
def edit_photo(request: EditRequest):
    from photo_lifecycle import _LOCK
    with _LOCK:
        from edits import edit_image
        result = _photo_action(lambda photos: edit_image(SHOTS_DIR, request.image, request.exposure_ev, request.crop))
        record = engine_mod.Shot(ok=True,image=result["image"],summary=result["summary"],why=result["why"],
                                mood={},settings={},evidence={"source":"jpeg_edit","original":request.image},
                                applied={"exposure_ev":request.exposure_ev,"crop":request.crop})
        engine_mod.save_shot_record(record, None)
        return {**asdict(record), **result}


class DevelopRequest(BaseModel):
    image: str = Field(max_length=250)
    exposure_ev: float = Field(default=0, ge=-3, le=3)
    strength: float = Field(default=0, ge=0, le=1)
    highlights: float = Field(default=0, ge=0, le=1)
    wb_red: float = Field(default=1, ge=.5, le=2)
    wb_blue: float = Field(default=1, ge=.5, le=2)
    include_dng: bool = False


class PanoramaRequest(BaseModel):
    images: list[str] = Field(min_length=2, max_length=12)
    projection: str = Field(default='spherical', max_length=40)
    horizontal_fov_deg: float = Field(default=90, ge=20, le=140)
    rotation_deg: float = Field(default=0, ge=-45, le=45)
    autocrop: bool = True
    level_horizon: bool = True
    crop: list[float] | None = None
    layout: dict | None = None


class PanoramaPlanRequest(BaseModel):
    rows: int = Field(default=1, ge=1, le=12)
    columns: int = Field(default=3, ge=1, le=12)
    overlap: float = Field(default=.35, ge=.3, le=.8)
    hfov_deg: float = Field(default=60, ge=10, le=100)
    vfov_deg: float = Field(default=40, ge=10, le=100)
    focal_length_mm: float | None = Field(default=None, ge=8, le=400)
    sensor_width_mm: float | None = Field(default=None, ge=1, le=50)
    sensor_height_mm: float | None = Field(default=None, ge=1, le=50)
    yaw_span_deg: float | None = Field(default=None, ge=1, le=360)
    pitch_span_deg: float | None = Field(default=None, ge=1, le=180)


@app.post('/panoramas/plan', dependencies=[Depends(_require_api_token)])
def panorama_plan(request: PanoramaPlanRequest):
    from panorama import plan_from_mapping
    return _photo_action(lambda _: plan_from_mapping(request.model_dump()))


@app.post('/panoramas', dependencies=[Depends(_require_api_token)])
def make_panorama(request: PanoramaRequest):
    from panorama import build_panorama, PanoramaError
    from photo_lifecycle import _LOCK, _image_name, manage_shot, MAX_IMAGE_BYTES
    with _LOCK:
        photos = PhotoLifecycle(SHOTS_DIR)
        for name in request.images:
            _photo_action(lambda p: p.cards(_image_name(name)))
        try:
            result = build_panorama([SHOTS_DIR / name for name in request.images], SHOTS_DIR / 'panoramas',
                projection=request.projection, horizontal_fov_deg=request.horizontal_fov_deg,
                rotation_deg=request.rotation_deg, autocrop=request.autocrop,
                level_horizon=request.level_horizon, crop=request.crop, layout=request.layout)
        except (PanoramaError, ValueError) as exc:
            raise HTTPException(status_code=422, detail={'reason': getattr(exc, 'reason', 'invalid_request'),
                'message': str(exc), 'job_id': getattr(exc, 'job_id', None), 'retry_capture': False}) from exc
        relative = 'panoramas/' + result['job_id']
        name = 'panorama_' + result['job_id'] + '.jpg'
        try:
            photos._write(name, photos._read(relative + '/panorama.jpg', MAX_IMAGE_BYTES))
            record = engine_mod.Shot(ok=True, image=name, summary='Panorama copy saved.',
                why='Aligned saved JPEGs using planar geometry and the selected virtual projection.',
                mood={}, settings={}, evidence=result, applied={'projection': request.projection})
            engine_mod.save_shot_record(record, None)
            manage_shot(SHOTS_DIR, name, [str(path.relative_to(SHOTS_DIR))
                for path in (SHOTS_DIR / relative).iterdir() if path.is_file() and not path.is_symlink()],
                owned_dirs=[relative])
        except Exception as exc:
            import shutil
            try:
                photos.delete(name)
            except FileNotFoundError:
                pass
            shutil.rmtree(SHOTS_DIR / relative)
            raise HTTPException(status_code=503, detail={'reason': 'publication_failed',
                'message': 'Panorama copy could not be saved; originals preserved.', 'retry_capture': False}) from exc
        return asdict(record)


@app.post('/develop', dependencies=[Depends(_require_api_token)])
def develop_photo(request: DevelopRequest):
    from photo_lifecycle import _LOCK, _image_name, manage_shot, MAX_IMAGE_BYTES
    from isolated_development import run_development, DevelopmentError
    with _LOCK:
        photos = PhotoLifecycle(SHOTS_DIR)
        _photo_action(lambda p: p.cards(_image_name(request.image)))
        try:
            result = run_development(SHOTS_DIR / request.image, SHOTS_DIR / 'development', {
                'exposure_ev': request.exposure_ev, 'tone_strength': request.strength,
                'highlight_compression': request.highlights, 'wb_red': request.wb_red,
                'wb_blue': request.wb_blue})
        except DevelopmentError as exc:
            raise HTTPException(status_code=422, detail={'reason': exc.reason,
                                'message': str(exc), 'retry_capture': False}) from exc
        relative = 'development/' + result['job_id']
        name = 'developed_' + result['job_id'] + '.jpg'
        try:
            if request.include_dng:
                import shutil
                from linear_dng import export_linear_dng
                job = SHOTS_DIR / relative
                dng_root = job / 'dng-export'
                dng = export_linear_dng(job / 'linear.tiff', job / 'receipt.json', dng_root)
                source = dng_root / dng['job_id'] / 'output.dng'
                shutil.copyfile(source, job / 'output.dng')
                os.chmod(job / 'output.dng', 0o600)
                result['outputs']['output.dng'] = dng['output']
                result['linear_dng'] = dng
                from stack_session import _atomic_json
                _atomic_json(job / 'receipt.json', result)
                shutil.rmtree(dng_root)
            result['linear_tiff_url'] = '/development/' + result['job_id'] + '/linear.tiff'
            if request.include_dng:
                result['linear_dng_url'] = '/development/' + result['job_id'] + '/output.dng'
            photos._write(name, photos._read(relative + '/developed.jpg', MAX_IMAGE_BYTES))
            record = engine_mod.Shot(ok=True, image=name, summary='Developed copy saved.',
                why='Exposure, white balance, and tone applied to the saved photo; original preserved.',
                mood={}, settings={}, evidence=result, applied=result['options'])
            engine_mod.save_shot_record(record, None)
            owned = [str(path.relative_to(SHOTS_DIR)) for path in (SHOTS_DIR / relative).iterdir()
                     if path.is_file() and not path.is_symlink()]
            manage_shot(SHOTS_DIR, name, owned, owned_dirs=[relative])
        except Exception as exc:
            # Roll back only this new UUID's derived files; a retry never takes a photo.
            import shutil
            try:
                photos.delete(name)
            except FileNotFoundError:
                pass
            shutil.rmtree(SHOTS_DIR / relative)
            dng_reason = getattr(exc, 'reason', None) if request.include_dng else None
            raise HTTPException(status_code=422 if dng_reason else 503, detail={'reason': dng_reason or 'publication_failed',
                'message': 'DNG verification failed; original preserved.' if dng_reason else 'Developed copy could not be saved; original preserved.',
                'retry_capture': False}) from exc
        return {**asdict(record), 'linear_tiff_url': '/development/' + result['job_id'] + '/linear.tiff',
                **({'linear_dng_url': '/development/' + result['job_id'] + '/output.dng'} if request.include_dng else {})}


@app.get('/development/{job_id}/{name}', dependencies=[Depends(_require_api_token)])
def development_artifact(job_id: str, name: str):
    import re
    import hashlib
    from photo_lifecycle import _LOCK
    if not re.fullmatch('[0-9a-f]{32}', job_id) or name not in {'linear.tiff', 'output.dng', 'preview.jpg', 'developed.jpg'}:
        raise HTTPException(status_code=404, detail='Development artifact not found')
    with _LOCK:
        photos = PhotoLifecycle(SHOTS_DIR)
        receipt = _photo_action(lambda p: p._json('development/' + job_id + '/receipt.json'))
        if receipt.get('status') != 'completed':
            raise HTTPException(status_code=404, detail='Development artifact not found')
        expected = receipt.get('outputs', {}).get(name, {})
        import stat
        try:
            with photos._parent('development/' + job_id + '/' + name) as (parent, filename):
                fd = os.open(filename, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=parent)
        except FileNotFoundError as exc:
            raise HTTPException(status_code=404, detail='Development artifact not found') from exc
        stream = os.fdopen(fd, 'rb')
        try:
            info = os.fstat(stream.fileno())
            if not stat.S_ISREG(info.st_mode) or info.st_size != expected.get('bytes') or info.st_size > 256 * 1024**2:
                raise HTTPException(status_code=409, detail='Development artifact changed')
            if hashlib.file_digest(stream, 'sha256').hexdigest() != expected.get('sha256'):
                raise HTTPException(status_code=409, detail='Development artifact changed')
            stream.seek(0)
        except BaseException:
            stream.close()
            raise
        def chunks():
            with stream:
                yield from iter(lambda: stream.read(1024 * 1024), b'')
        # The verified descriptor survives concurrent gallery deletion without a
        # second path lookup or buffering a full-resolution TIFF in memory.
        return StreamingResponse(chunks(), media_type='image/x-adobe-dng' if name.endswith('.dng') else 'image/tiff' if name.endswith('.tiff') else 'image/jpeg',
            headers={'Content-Length': str(info.st_size), 'Content-Disposition': f'attachment; filename="{name}"'},
            background=BackgroundTask(stream.close))


async def _retention_loop():
    while True:
        try:
            await asyncio.to_thread(PhotoLifecycle(SHOTS_DIR).enforce_retention)
        except Exception:  # noqa: BLE001 — one failed sweep never ends the background task
            # A failed sweep must not delete more or prevent camera access; next cycle retries.
            pass
        await asyncio.sleep(RETENTION_SWEEP_SECONDS)


DEFERRED_PUBLISH_SECONDS = 2.0


async def _deferred_publish_loop():
    """Split runtime: publish sessions the heavy-jobs role finished (docs/RUNTIME_SPLIT_2026-09-28.md)."""
    import product_sessions
    while True:
        try:
            await asyncio.to_thread(product_sessions.publish_deferred)
        except Exception:  # noqa: BLE001 — one failed sweep never ends the background task
            logging.getLogger("camera_autopilot.runtime").exception("deferred publication sweep failed")
        await asyncio.sleep(DEFERRED_PUBLISH_SECONDS)


@app.on_event("startup")
async def start_retention():
    app.state.retention_task = asyncio.create_task(_retention_loop())
    if runtime_roles.heavy_jobs_deferred():
        app.state.deferred_publish_task = asyncio.create_task(_deferred_publish_loop())
    if engine.name == 'real':
        _session_jobs()


@app.on_event("shutdown")
async def stop_retention():
    for name in ("retention_task", "deferred_publish_task"):
        task = getattr(app.state, name, None)
        if task:
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
    with _handheld_lock:
        running = [entry for entry in _handheld_sessions.values() if entry['thread'].is_alive()]
    for entry in running:
        entry['cancel'].set()
    for entry in running:
        await asyncio.to_thread(entry['thread'].join, 30)
    jobs = getattr(app.state, 'session_jobs', None)
    if jobs:
        await asyncio.to_thread(jobs.close)
        del app.state.session_jobs


_jobs_initialization_lock = threading.Lock()


def _session_jobs():
    import product_sessions
    from session_jobs import SessionJobs
    with _jobs_initialization_lock:
        if not getattr(app.state, 'session_jobs', None):
            app.state.session_jobs = SessionJobs(product_sessions.runner, product_sessions.run,
                                                 camera_exclusion=_exclusive_camera)
        return app.state.session_jobs


class NightAssistMetadata(BaseModel):
    iso: str = Field(max_length=16)
    aperture_label: str = Field(max_length=16)
    shutter_label: str = Field(max_length=16)
    frames: int = Field(ge=2, le=16)
    evidence: dict | None = None
    camera_serial: str | None = Field(default=None, min_length=1, max_length=128)  # the planned body


class SessionCreateRequest(BaseModel):
    mode: str
    # 240 = stack_session.MAX_SEQUENCE_FRAMES; the runner enforces each mode's own limit.
    frames: int = Field(default=3, ge=1, le=240)
    ev_step: float = Field(default=2.0, ge=0, le=3)
    interval_s: float = Field(default=0, ge=0, le=3600)
    camera_model: str | None = None
    camera_port: str | None = None
    camera_serial: str | None = Field(default=None, min_length=1, max_length=128)
    timelapse_mode: str = 'fixed'
    timelapse_options: dict = Field(default_factory=dict)
    duration_s: float | None = Field(default=None, ge=1, le=3600)
    panorama: dict | None = None
    preview_every: int | None = Field(default=None, ge=0, le=12)
    night_assist: NightAssistMetadata | None = None
    # Crowd removal only: stop cleanly once this share of the frame has cleared (0.5..1.0).
    stop_when_cleared: float | None = Field(default=None, ge=0.5, le=1.0)
    # Session meter-first: the frame-0 exposure {iso, aperture, shutter} (from POST /sessions/meter-plan's
    # recommended_start), validated before the session exists; `meter_plan` is that plan, as provenance.
    start_settings: dict | None = None
    meter_plan: dict | None = None


class MeterPlanRequest(BaseModel):
    camera_model: str = Field(max_length=100)
    camera_port: str = Field(max_length=64)
    camera_serial: str | None = Field(default=None, min_length=1, max_length=128)
    # The POST /sessions body this plan is for (required): checked by the same code path as POST /sessions
    # before the meter photo fires, so an invalid session costs no shutter. Its mode, frames and ev_step
    # shape the meter (HDR reserves bracket headroom under the 1 s USB cap, #72's rule).
    session: SessionCreateRequest
    # The timelapse controls the panel applies after creating the session, checked on a scratch copy.
    controls: dict | None = None
    intent: str | None = Field(default=None, max_length=2000)


@app.post('/sessions/meter-plan', dependencies=[Depends(_require_api_token)])
def session_meter_plan(request: MeterPlanRequest):
    """Session meter-first: ONE meter frame (plus the bounded re-meter) through the /shoot Fixed-stack
    metering path, which puts the inherited exposure back before it returns. Returns the recommended
    start settings for POST /sessions `start_settings`; saves no session. A failure is answered as
    /shoot answers it (an uncertain meter is a 409 with its receipt, never retried)."""
    import product_sessions
    if engine.name != 'real':
        raise HTTPException(status_code=400, detail='Metering requires the real USB engine')
    if request.camera_model not in engine_mod.USB_CONTROL_MODELS or not request.camera_port:
        raise HTTPException(status_code=400, detail='Select an exact supported camera')
    try:
        _require_serial(request.camera_serial, request.camera_model, request.camera_port)
    except RuntimeError as exc:
        return _identity_refusal(exc)
    session = request.session
    if (session.camera_model, session.camera_port, session.camera_serial) != (
            request.camera_model, request.camera_port, request.camera_serial):
        raise HTTPException(status_code=400, detail='The session is for another camera than this meter plan')
    if session.night_assist is not None:
        raise HTTPException(status_code=400, detail='A Night Assist session has its own exposure; it takes no meter photo')
    if session.start_settings is not None or session.meter_plan is not None:
        raise HTTPException(status_code=400, detail='The session to meter for cannot carry start settings yet')
    # Exactly POST /sessions' checks, then SessionRunner.create without saving: before any shutter.
    _prepare_session(session, dry_run=True)
    if session.duration_s is not None:
        try:  # a window too short even for the fastest shutter can never be metered into a plan
            from long_exposure_plan import plan_long_exposure
            plan_long_exposure(session.duration_s, 1 / 8000, frame_budget=session.frames)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
    exposure = None
    if request.controls is not None:
        if session.mode != 'timelapse':
            raise HTTPException(status_code=400, detail='Timelapse controls need the timelapse session they apply to')
        try:
            exposure = product_sessions.check_timelapse_controls(dict(
                mode='timelapse', frames=session.frames, ev_step=session.ev_step, interval_s=session.interval_s,
                timelapse_mode=session.timelapse_mode, timelapse_options=session.timelapse_options,
                duration_s=session.duration_s), request.controls)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
    offsets = product_sessions.bracket_offsets(session.mode, session.frames, session.ev_step)
    preflight = _host_camera_preflight()
    try:
        with _exclusive_camera():
            meter = _selected_engine(request).meter_baseline(request.intent, bracket_ev_offsets=offsets)
    except RuntimeError as exc:
        return _capture_failure_response(request, exc, preflight, route='/sessions/meter-plan')
    plan = product_sessions.meter_plan_response(meter, frames=session.frames, mode=session.mode,
                                                camera=(request.camera_model, request.camera_port, request.camera_serial),
                                                ev_step=session.ev_step)
    if not plan.get('error'):
        # The recommendation must fit the session it is for: POST /sessions checks the session's own
        # timelapse options, and the run then uses the controls' bounds.
        configurations = [(session.timelapse_mode, session.timelapse_options)]
        if exposure is not None:
            configurations.append((exposure.get('mode', session.timelapse_mode),
                                   {key: value for key, value in exposure.items() if key != 'mode'}))
        try:
            for timelapse_mode, timelapse_options in configurations:
                product_sessions.validate_start_settings(
                    plan['recommended_start'], mode=session.mode, frames=session.frames, ev_step=session.ev_step,
                    timelapse_mode=timelapse_mode, timelapse_options=timelapse_options, meter_plan=plan,
                    duration_s=session.duration_s,
                    camera=(session.camera_model, session.camera_port, session.camera_serial))
        except ValueError as exc:
            plan.update(error='recommendation_outside_session', recommended_start=None, retry_capture=False,
                        may_have_fired=False,
                        detail=(f"The metered exposure does not fit this session: {exc}. No session was created; "
                                "widen the session's bounds, or keep the camera's current exposure."))
    if plan.get('error') in ('meter_unreliable', 'meter_restore_failed', 'recommendation_outside_session'):
        return JSONResponse(status_code=409, content=_with_restore_warning(plan, plan.get('meter_restore')))
    return _with_host_camera_clients(plan, preflight)


@app.post('/sessions', dependencies=[Depends(_require_api_token)])
def create_photo_session(request: SessionCreateRequest):
    import product_sessions
    if engine.name != 'real':
        raise HTTPException(status_code=400, detail='Computational capture requires the real USB engine')
    if request.camera_model not in engine_mod.USB_CONTROL_MODELS or not request.camera_port:
        raise HTTPException(status_code=400, detail='Select an exact supported camera')
    try:
        _require_serial(request.camera_serial, request.camera_model, request.camera_port)
    except RuntimeError as exc:
        return _identity_refusal(exc)
    return _with_host_camera_clients(_prepare_session(request), _host_camera_preflight())


def _prepare_session(request: SessionCreateRequest, *, dry_run: bool = False) -> dict:
    """Every check of a POST /sessions request, then the session (``dry_run``: the manifest it would
    write, nothing saved). Raises HTTPException. Opens no camera."""
    import product_sessions
    budget_s = product_sessions.execution_budget_s(request.mode, {'mode': request.timelapse_mode})
    if request.interval_s * max(0, request.frames - 1) > budget_s:
        raise HTTPException(status_code=400, detail=f'A local session must fit within {budget_s / 3600:g} hour(s)')
    if len(product_sessions.runner().list()) >= 100:
        raise HTTPException(status_code=409, detail='Delete an existing session before creating more (limit 100)')
    if request.night_assist is not None:
        if request.mode != 'night':
            raise HTTPException(status_code=400, detail='night_assist applies to night sessions only')
        if request.night_assist.frames != request.frames:
            raise HTTPException(status_code=400, detail='night_assist frames must match the session frame count')
        # A plan that records its body's serial runs only on that body (plans without one are not checked).
        if request.night_assist.camera_serial is not None and request.night_assist.camera_serial != request.camera_serial:
            raise HTTPException(status_code=400, detail='This Night Assist plan was made for another camera; re-plan for the selected camera')
    if request.stop_when_cleared is not None:
        if request.mode != 'crowd_remove':
            raise HTTPException(status_code=400, detail='stop_when_cleared applies to crowd removal sessions only')
        try:
            product_sessions._crowd_options({'frame_count': request.frames,
                                             'metadata': {'stop_when_cleared': request.stop_when_cleared}})
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
    if request.mode == 'panorama':
        try:  # projection options and the preview cadence, as the run checks them before its shutter
            probe = {'frame_count': request.frames, 'metadata': {
                **({'panorama': request.panorama} if request.panorama else {}),
                **({'preview_every': request.preview_every} if request.preview_every is not None else {})}}
            product_sessions._panorama_options(probe)
            product_sessions._panorama_preview_every(probe)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
    start = baseline_record = None
    if request.meter_plan is not None and request.start_settings is None:
        raise HTTPException(status_code=400, detail='meter_plan needs the start_settings it recommended')
    if request.start_settings is not None:
        if request.night_assist is not None:
            raise HTTPException(status_code=400, detail='A Night Assist plan sets its own exposure; omit start_settings')
        try:
            start, baseline_record = product_sessions.validate_start_settings(
                request.start_settings, mode=request.mode, frames=request.frames, ev_step=request.ev_step,
                timelapse_mode=request.timelapse_mode, timelapse_options=request.timelapse_options,
                meter_plan=request.meter_plan, duration_s=request.duration_s,
                camera=(request.camera_model, request.camera_port, request.camera_serial))
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
    try:
        session = product_sessions.runner().create(request.mode,request.frames,ev_step=request.ev_step,
            settings=start,
            interval_s=request.interval_s,metadata={'camera_model':request.camera_model,'camera_port':request.camera_port,
                'camera_serial':request.camera_serial,
                **({'panorama': request.panorama} if request.panorama else {}),
                **({'preview_every': request.preview_every} if request.preview_every is not None else {}),
                **({'night_assist': request.night_assist.model_dump()} if request.night_assist else {}),
                **({'stop_when_cleared': request.stop_when_cleared} if request.stop_when_cleared is not None else {}),
                **({'baseline': baseline_record} if baseline_record else {})},
            timelapse_mode=request.timelapse_mode, timelapse_options=request.timelapse_options,
            duration_s=request.duration_s, dry_run=dry_run)
    except ValueError as exc:
        raise HTTPException(status_code=400,detail=str(exc)) from exc
    return session


@app.get('/sessions', dependencies=[Depends(_require_api_token)])
def photo_sessions():
    """Every session with compact frames; each frame's full applied settings and camera receipt
    stay in its evidence sidecar (GET /sessions/{id} returns them)."""
    import product_sessions
    return product_sessions.runner().list()


@app.get('/sessions/{session_id}', dependencies=[Depends(_require_api_token)])
def photo_session(session_id: str):
    """One session with full frames: applied settings and camera receipts from the sidecars."""
    import product_sessions
    return _photo_action(lambda _: product_sessions.runner().read_detail(session_id))


@app.get('/sessions/{session_id}/controls', dependencies=[Depends(_require_api_token)])
def get_timelapse_controls(session_id: str):
    import product_sessions
    from timelapse_controls import read_controls
    return _photo_action(lambda _: read_controls(product_sessions.runner(), session_id))


@app.post('/sessions/{session_id}/controls', dependencies=[Depends(_require_api_token)])
def set_timelapse_controls(session_id: str, patch: dict):
    import product_sessions
    from timelapse_controls import update_controls
    if engine.name != 'real':
        raise HTTPException(status_code=400, detail='Session control requires the real USB engine')
    try:
        return _photo_action(lambda _: update_controls(product_sessions.runner(), session_id, patch))
    except RuntimeError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@app.post('/sessions/{session_id}/run', dependencies=[Depends(_require_api_token)])
def run_photo_session(session_id: str):
    import product_sessions
    if engine.name != 'real':
        raise HTTPException(status_code=400, detail='Computational capture requires the real USB engine')
    try:
        with _exclusive_camera():
            return product_sessions.run(session_id)
    except NotImplementedError as exc:
        raise HTTPException(status_code=501,detail=str(exc)) from exc
    except (ValueError, FileNotFoundError) as exc:
        raise HTTPException(status_code=400,detail=str(exc)) from exc
    except RuntimeError as exc:
        if (refusal := _identity_refusal(exc)) is not None:
            return refusal
        if getattr(exc, "ladder_refused", False) is True:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        raise HTTPException(status_code=503,detail=str(exc)) from exc


@app.post('/sessions/{session_id}/cancel', dependencies=[Depends(_require_api_token)])
def cancel_photo_session(session_id: str):
    if engine.name != 'real':
        raise HTTPException(status_code=400, detail='Session control requires the real USB engine')
    try:
        return _photo_action(lambda _: _session_jobs().cancel(session_id))
    except RuntimeError as exc:  # another process (e.g. the heavy-jobs worker) holds the session
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@app.post('/sessions/{session_id}/stop', dependencies=[Depends(_require_api_token)])
def stop_photo_session(session_id: str):
    """Stop when clean: no further shutter; the frames accepted so far are merged into the result."""
    import product_sessions
    if engine.name != 'real':
        raise HTTPException(status_code=400, detail='Session control requires the real USB engine')
    if _photo_action(lambda _: _session_jobs().status(session_id))['status'] == 'queued':
        raise HTTPException(status_code=409, detail='This session has not started capturing yet; use Cancel')
    return _photo_action(lambda _: product_sessions.runner().stop(session_id))


class SessionJobRequest(BaseModel):
    delay_seconds: float = Field(default=0, ge=0, le=36000)


@app.post('/sessions/{session_id}/jobs', status_code=202, dependencies=[Depends(_require_api_token)])
def submit_photo_session(session_id: str, request: SessionJobRequest | None = None):
    if engine.name != 'real':
        raise HTTPException(status_code=400, detail='Computational capture requires the real USB engine')
    preflight = _host_camera_preflight()   # read before the job can start the camera
    try:
        job = _photo_action(lambda _: _session_jobs().submit(session_id,
                                delay_s=request.delay_seconds if request else 0))
    except RuntimeError as exc:
        if (refusal := _identity_refusal(exc)) is not None:
            return refusal
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return _with_host_camera_clients(job, preflight)


@app.get('/sessions/{session_id}/jobs', dependencies=[Depends(_require_api_token)])
def photo_session_job(session_id: str):
    import product_sessions
    def read(photos):
        state = product_sessions.runner().read(session_id)
        if engine.name == 'real':
            job = _session_jobs().status(session_id)
        else:
            # A demo viewer must not acquire the live server's scheduler lease.
            job = photos._json(f'sessions/{session_id}/job.json',
                               {'id': session_id, 'status': 'not_submitted'})
            if job.get('id') != session_id:
                raise ValueError('Job identity mismatch')
        if runtime_roles.role() == 'control':
            # Split runtime: lets the session panel say when the processing service is down.
            job = {**job, 'runtime': runtime_roles.liveness(product_sessions.runner().root,
                                                            os.environ.get(runtime_roles.PERCEPTION_SOCKET_ENV))}
        return {**job, 'session': state}
    return _photo_action(read)


@app.post('/sessions/{session_id}/resume', dependencies=[Depends(_require_api_token)])
def resume_photo_session(session_id: str):
    import product_sessions
    if engine.name != 'real':
        raise HTTPException(status_code=400, detail='Session control requires the real USB engine')
    if _photo_action(lambda _: _session_jobs().status(session_id))['status'] in ('queued', 'running'):
        raise HTTPException(status_code=409, detail='Wait for the active session job before resuming')
    try:
        # A session saved before serial binding never resumes by port alone.
        _photo_action(lambda _: product_sessions.require_camera_serial(product_sessions.runner().read(session_id)))
    except RuntimeError as exc:
        if (refusal := _identity_refusal(exc)) is not None:
            return refusal
        raise
    try:
        # Its saved baseline decides the whole bracket, so a rerun would refuse the same way.
        product_sessions.require_runnable_ladder(_photo_action(lambda _: product_sessions.runner().read(session_id)))
    except product_sessions.LadderRefused as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return _photo_action(lambda _: product_sessions.runner().resume(session_id))


class SessionCameraRequest(BaseModel):
    camera_model: str = Field(max_length=100)
    camera_port: str = Field(max_length=64)
    camera_serial: str = Field(min_length=1, max_length=128)


@app.post('/sessions/{session_id}/camera', dependencies=[Depends(_require_api_token)])
def reselect_session_camera(session_id: str, request: SessionCameraRequest):
    """Explicitly re-select a session's body: binds a legacy session to a serial, or moves a bound
    session to the port its (same) serial re-enumerated to. The next run re-verifies the serial."""
    import product_sessions
    if engine.name != 'real':
        raise HTTPException(status_code=400, detail='Session control requires the real USB engine')
    if _photo_action(lambda _: _session_jobs().status(session_id))['status'] in ('queued', 'running'):
        raise HTTPException(status_code=409, detail='Wait for the active session job before re-selecting its camera')
    try:
        return _photo_action(lambda _: product_sessions.reselect_camera(
            session_id, request.camera_model, request.camera_port, request.camera_serial))
    except RuntimeError as exc:
        if (refusal := _identity_refusal(exc)) is not None:
            return refusal  # the OS does not report that serial at that port; nothing changed
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@app.post('/sessions/{session_id}/encode-partial', dependencies=[Depends(_require_api_token)])
def encode_partial_session_movie(session_id: str):
    """Owner decision 2026-10-02: encode the accepted frames of a timelapse blocked by an ambiguous
    frame (needs_reconciliation) into timelapse_partial.mp4. Opens no camera, fires nothing, never
    uses the ambiguous frame and leaves the session status unchanged. 409 for any other session."""
    import product_sessions
    from timelapse_partial import PartialRefused

    def encode(_):
        try:
            return product_sessions.encode_partial(session_id)
        except PartialRefused as exc:
            raise HTTPException(status_code=409, detail=exc.as_dict()) from exc
        except RuntimeError as exc:  # another operation (a run, deletion or the jobs worker) holds it
            raise HTTPException(status_code=409, detail=str(exc)) from exc
    return _photo_action(encode)



def _stream_session_preview(session_id: str, key: str, label: str):
    """Stream one hash-verified preview JPEG recorded under ``state[key]`` inside the session."""
    import product_sessions
    import hashlib
    import stat
    sessions = product_sessions.runner()
    state = _photo_action(lambda _: sessions.read(session_id))
    preview = state.get(key) or {}
    if not preview.get('path') or not preview.get('sha256'):
        raise HTTPException(status_code=404, detail=f'No {label} preview yet')
    directory = sessions._directory(session_id).resolve()
    recorded = Path(preview['path'])
    path = recorded.resolve()
    if recorded.is_symlink() or directory not in path.parents:
        raise HTTPException(status_code=404, detail=f'{label.capitalize()} preview unavailable')
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=f'{label.capitalize()} preview was replaced; refresh progress') from exc
    stream = os.fdopen(fd, 'rb')
    try:
        info = os.fstat(stream.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_size != preview['bytes'] or info.st_size > 64 * 1024**2:
            raise HTTPException(status_code=409, detail=f'{label.capitalize()} preview changed after acceptance')
        if hashlib.file_digest(stream, 'sha256').hexdigest() != preview['sha256']:
            raise HTTPException(status_code=409, detail=f'{label.capitalize()} preview changed after acceptance')
        stream.seek(0)
    except BaseException:
        stream.close()
        raise
    return StreamingResponse(iter(lambda: stream.read(1024 * 1024), b''), media_type='image/jpeg',
        headers={'Content-Length': str(info.st_size)}, background=BackgroundTask(stream.close))


@app.get('/sessions/{session_id}/panorama-preview', dependencies=[Depends(_require_api_token)])
def session_panorama_preview(session_id: str):
    """Stream the latest verified incremental panorama preview for a running/finished session."""
    return _stream_session_preview(session_id, 'preview_panorama', 'panorama')


@app.get('/sessions/{session_id}/crowd-preview', dependencies=[Depends(_require_api_token)])
def session_crowd_preview(session_id: str):
    """Stream the latest verified clean-plate preview (not-cleared regions tinted) of a crowd session."""
    return _stream_session_preview(session_id, 'preview_crowd', 'crowd')


@app.get("/moods/preview", dependencies=[Depends(_require_api_token)])
def mood_preview_route(image: str) -> dict:
    """Server-rendered mood previews derived from the recipe's applied axes, not a CSS filter."""
    import base64
    import intent
    import mood_preview
    safe_name = Path(image).name
    if safe_name != image or Path(safe_name).suffix.lower() not in IMAGE_SUFFIXES:
        raise HTTPException(status_code=404, detail="shot image not found")
    path = SHOTS_DIR / safe_name
    if path.is_symlink() or not path.is_file():
        raise HTTPException(status_code=404, detail="shot image not found")
    try:
        previews = mood_preview.render_all_previews(path, intent.MOOD_VARIANTS)
    except (OSError, ValueError) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {"image": safe_name, "previews": {variant_id: "data:image/jpeg;base64," + base64.b64encode(data).decode()
                                              for variant_id, data in previews.items()},
            "recipes": {variant["id"]: variant["recipe"] for variant in intent.MOOD_VARIANTS}}


class NightAssistRequest(BaseModel):
    image: str = Field(max_length=255)
    camera_model: str = Field(max_length=100)
    camera_port: str = Field(max_length=64)
    camera_serial: str | None = Field(default=None, min_length=1, max_length=128)
    max_frames: int = Field(default=16, ge=2, le=16)
    tripod: bool = True


@app.post('/night-assist/plan', dependencies=[Depends(_require_api_token)])
def night_assist_plan(request: NightAssistRequest) -> dict:
    """Plan a night session from a saved meter frame and the selected body's measured noise table.
    Reads the body's live ladders (no shutter); returns the plan and a ready /sessions request."""
    import re
    from dataclasses import asdict as _asdict
    import night_assist
    from sonyremote.camera import Camera
    safe_name = Path(request.image).name
    if safe_name != request.image or Path(safe_name).suffix.lower() not in IMAGE_SUFFIXES:
        raise HTTPException(status_code=404, detail="shot image not found")
    path = SHOTS_DIR / safe_name
    if path.is_symlink() or not path.is_file():
        raise HTTPException(status_code=404, detail="shot image not found")
    if engine.name != 'real':
        raise HTTPException(status_code=400, detail='Night assist requires the real USB engine')
    if request.camera_model not in engine_mod.USB_CONTROL_MODELS or not re.fullmatch(r'usb:[0-9]+,[0-9]+', request.camera_port):
        raise HTTPException(status_code=400, detail='Select an exact supported camera and USB port')
    try:
        with _exclusive_camera():
            engine_mod._free_usb()
            with Camera(model=request.camera_model, port=request.camera_port) as probe:
                ladders = (probe.list_options('iso'), probe.list_options('aperture'), probe.list_options('shutter'))
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(status_code=503, detail=f'Camera probe failed: {exc}') from exc
    try:
        plan = night_assist.plan_from_meter_frame(str(path), request.camera_model, *ladders,
                                                  max_frames=request.max_frames, tripod=request.tripod)
    except (OSError, ValueError) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    payload = _asdict(plan)
    payload['image'] = safe_name
    payload['session_request'] = (night_assist.to_session_request(plan, request.camera_model, request.camera_port,
                                                                  request.camera_serial)
                                  if plan.status == 'ok' else None)
    if payload['session_request'] is not None:
        payload['session_request']['night_assist'] = {
            'iso': str(plan.iso), 'aperture_label': plan.aperture_label, 'shutter_label': plan.shutter_label,
            'frames': plan.frames, 'evidence': plan.evidence, 'camera_serial': request.camera_serial}
    return payload


# --- Handheld (body-shutter) sessions -------------------------------------------------------
_handheld_lock = threading.Lock()
_handheld_sessions: dict[str, dict] = {}
HANDHELD_HISTORY = 20


class HandheldStartRequest(BaseModel):
    camera_model: str = Field(max_length=100)
    camera_port: str = Field(max_length=64)
    camera_serial: str | None = Field(default=None, min_length=1, max_length=128)
    max_frames: int = Field(default=20, ge=1, le=200)
    max_duration_s: float = Field(default=600, gt=0, le=3600)
    intent: str | None = Field(default=None, max_length=2000)


def _handheld_entry(session_id: str) -> dict:
    with _handheld_lock:
        entry = _handheld_sessions.get(session_id)
    if not entry:
        raise HTTPException(status_code=404, detail='Handheld session not found')
    return entry


@app.post('/handheld', dependencies=[Depends(_require_api_token)], status_code=202)
def start_handheld(request: HandheldStartRequest) -> dict:
    """Start a bounded body-shutter session: the host never fires the shutter, it only meters
    each frame the camera pushes and sets the exposure for the next one."""
    import re
    import uuid
    from handheld import HandheldSession
    from handheld_recommend import make_recommend_fn
    from sonyremote.camera import Camera
    if engine.name != 'real':
        raise HTTPException(status_code=400, detail='Handheld sessions require the real USB engine')
    if request.camera_model not in engine_mod.USB_CONTROL_MODELS or not re.fullmatch(r'usb:[0-9]+,[0-9]+', request.camera_port):
        raise HTTPException(status_code=400, detail='Select an exact supported camera and USB port')
    try:
        _require_serial(request.camera_serial, request.camera_model, request.camera_port)
    except RuntimeError as exc:
        return _identity_refusal(exc)
    with _handheld_lock:
        if any(entry['thread'].is_alive() for entry in _handheld_sessions.values()):
            raise HTTPException(status_code=409, detail='A handheld session is already running')
    retry = _capture_budget.check()
    if retry:
        raise HTTPException(status_code=429, detail='Camera request limit reached', headers={'Retry-After': str(int(retry) + 1)})
    if not _camera_lock.acquire(blocking=False):
        raise HTTPException(status_code=409, detail='Camera is busy with another operation')
    model, port, serial = request.camera_model, request.camera_port, request.camera_serial
    try:
        plan = intent_brain.interpret(request.intent) if request.intent else None
        target = float((plan or {}).get('target_brightness') or 112.0)
        engine_mod._free_usb()
        # Every (re)connect of the session proves the serial before any setting write.
        with Camera(model=model, port=port, serial=serial) as probe:
            ladders = (probe.list_options('iso'), probe.list_options('aperture'), probe.list_options('shutter'))
        recommend = make_recommend_fn(model, *ladders, target_brightness=target,
                                      motion_intent=(plan or {}).get('motion_intent'))
    except Exception as exc:
        _camera_lock.release()
        if (refusal := _identity_refusal(exc)) is not None:
            return refusal
        raise HTTPException(status_code=503, detail=f'Camera probe failed: {exc}') from exc
    session_id = uuid.uuid4().hex
    out_dir = SHOTS_DIR / 'handheld' / session_id
    cancel = threading.Event()
    try:
        session = HandheldSession(lambda: (engine_mod._free_usb(), Camera(model=model, port=port, serial=serial))[1], recommend,
                                  max_frames=request.max_frames, max_duration_s=request.max_duration_s, out_dir=out_dir)

        def worker() -> None:
            try:
                session.run(cancel)
            except Exception:
                pass  # state() carries the failure; the lock release below is what matters
            finally:
                _camera_lock.release()

        thread = threading.Thread(target=worker, name=f'handheld-{session_id[:8]}', daemon=True)
        with _handheld_lock:
            _handheld_sessions[session_id] = {'session': session, 'cancel': cancel, 'thread': thread,
                                              'camera_model': model, 'camera_port': port, 'camera_serial': serial,
                                              'intent': request.intent, 'why': (plan or {}).get('why')}
            for stale in list(_handheld_sessions)[:-HANDHELD_HISTORY]:
                if not _handheld_sessions[stale]['thread'].is_alive():
                    del _handheld_sessions[stale]
        thread.start()
    except Exception as exc:
        with _handheld_lock:
            _handheld_sessions.pop(session_id, None)
        _camera_lock.release()
        raise HTTPException(status_code=503, detail=f'Handheld session could not start: {exc}') from exc
    return {'id': session_id, 'status': 'started', 'camera_model': model, 'camera_port': port,
            'camera_serial': serial, 'max_frames': request.max_frames, 'max_duration_s': request.max_duration_s,
            'note': 'Press the camera shutter; the host only meters each pushed frame and sets the next exposure.'}


@app.get('/handheld/{session_id}', dependencies=[Depends(_require_api_token)])
def handheld_status(session_id: str) -> dict:
    entry = _handheld_entry(session_id)
    state = entry['session'].state()
    state.update({'id': session_id, 'camera_model': entry['camera_model'], 'camera_port': entry['camera_port'],
                  'camera_serial': entry.get('camera_serial'),
                  'intent': entry['intent'], 'intent_why': entry['why']})
    return state


@app.post('/handheld/{session_id}/cancel', dependencies=[Depends(_require_api_token)])
def handheld_cancel(session_id: str) -> dict:
    entry = _handheld_entry(session_id)
    entry['cancel'].set()
    return {'id': session_id, 'cancel_requested': True, 'status': entry['session'].state()['status']}


@app.get('/handheld/{session_id}/frames/{name}', dependencies=[Depends(_require_api_token)])
def handheld_frame(session_id: str, name: str):
    import hashlib
    import stat
    entry = _handheld_entry(session_id)
    state = entry['session'].state()
    safe_name = Path(name).name
    receipt = next((frame.get('receipt') for frame in state.get('frames', [])
                    if frame.get('receipt') and Path(frame['receipt'].get('path', '')).name == safe_name), None)
    if safe_name != name or not receipt or Path(safe_name).suffix.lower() not in IMAGE_SUFFIXES:
        raise HTTPException(status_code=404, detail='Handheld frame not found')
    path = (SHOTS_DIR / 'handheld' / session_id / safe_name)
    if path.is_symlink():
        raise HTTPException(status_code=404, detail='Handheld frame not found')
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail='Handheld frame not found') from exc
    stream = os.fdopen(fd, 'rb')
    try:
        info = os.fstat(stream.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_size != receipt.get('bytes') or info.st_size > 64 * 1024**2:
            raise HTTPException(status_code=409, detail='Handheld frame changed after its receipt')
        if hashlib.file_digest(stream, 'sha256').hexdigest() != receipt.get('sha256'):
            raise HTTPException(status_code=409, detail='Handheld frame changed after its receipt')
        stream.seek(0)
    except BaseException:
        stream.close()
        raise
    return StreamingResponse(iter(lambda: stream.read(1024 * 1024), b''), media_type='image/jpeg',
        headers={'Content-Length': str(info.st_size)}, background=BackgroundTask(stream.close))


@app.get('/sessions/{session_id}/artifacts/{name}', dependencies=[Depends(_require_api_token)])
def session_artifact(session_id: str, name: str):
    import product_sessions
    import hashlib
    import stat
    import mimetypes
    sessions = product_sessions.runner()
    state = _photo_action(lambda _: sessions.read(session_id))
    path = _photo_action(lambda _: sessions.artifact(session_id, name))
    receipts = [frame.get('receipt') for frame in state['frames']]
    receipts += [(state.get(key) or {}).get('receipt') for key in
                 ('output', 'sequence', 'video', 'preview_video', 'partial_video')]
    receipts.append(((state.get('partial_video') or {}).get('receipt_file') or {}).get('receipt'))
    expected = next((receipt for receipt in receipts if receipt and
                     Path(receipt['path']).name == name), None)
    if not expected:
        raise HTTPException(status_code=404, detail='Session artifact changed; refresh progress')
    photos = PhotoLifecycle(SHOTS_DIR)
    try:
        with photos._parent(str(path.relative_to(SHOTS_DIR))) as (parent, filename):
            fd = os.open(filename, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=parent)
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail='Session artifact was replaced or deleted; refresh progress') from exc
    stream = os.fdopen(fd, 'rb')
    try:
        info = os.fstat(stream.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_size != expected['bytes'] or info.st_size > 513 * 1024**2:
            raise HTTPException(status_code=409, detail='Session artifact changed after acceptance')
        if hashlib.file_digest(stream, 'sha256').hexdigest() != expected['sha256']:
            raise HTTPException(status_code=409, detail='Session artifact changed after acceptance')
        stream.seek(0)
    except BaseException:
        stream.close()
        raise
    def chunks():
        with stream:
            yield from iter(lambda: stream.read(1024 * 1024), b'')
    # No long lifecycle lock: live preview requests remain responsive while the
    # camera runs. Open verified bytes survive preview rotation or deletion.
    return StreamingResponse(chunks(), media_type=mimetypes.guess_type(name)[0] or 'application/octet-stream',
        headers={'Content-Length': str(info.st_size), 'Content-Disposition': f'attachment; filename="{name}"'},
        background=BackgroundTask(stream.close))


@app.delete('/sessions/{session_id}', dependencies=[Depends(_require_api_token)])
def delete_photo_session(session_id: str, discard_unresolved: bool = False):
    import product_sessions
    try:
        return product_sessions.delete(session_id, discard_unresolved=discard_unresolved)
    except RuntimeError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except (ValueError, OSError) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
