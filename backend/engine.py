"""The autopilot's camera engine.

Implementations behind one interface, so the phone UI is identical whether we're driving the
real Sony A7 II (RealEngine — reuses the proven `sonyremote` take-over loop), a modern Sony body
over Wi-Fi (WiFiEngine), or returning a stand-in shot for UX work with no hardware (MockEngine).
Pick with the CAMERA_ENGINE env var (default mock). The whole point: the user never sees a
setting — they say what they want (or nothing) and get the shot. `intent_brain.interpret` turns
that plain-language intent into an exposure target + a plain-language "what it did".
"""

from __future__ import annotations

import json
import math
import os
import struct
import stat
import subprocess
import sys
import time
import zlib
from copy import deepcopy
from dataclasses import dataclass, field
from pathlib import Path

import intent_brain
from intent import motion_intent_floor_s
from image_safety import MAX_IMAGE_SOURCE_BYTES


def _free_usb() -> None:
    """macOS auto-launches `ptpcamerad`, which grabs the camera's USB between gphoto2 calls and
    makes capture fail intermittently (PTP -52). SIP blocks disabling it; killing it right before
    we claim the bus clears the contention (it respawns harmlessly). No-op off macOS."""
    if sys.platform != "darwin":
        return
    try:
        subprocess.run(["killall", "-9", "ptpcamerad"], capture_output=True, timeout=2)
    except Exception:
        pass

# The engine (sonyremote) + sample frames live at the monorepo root.
ENGINE_REPO = Path(__file__).resolve().parent.parent

# Autofocus-lock confirmation before freezing focus into Manual.
# Measured 2026-09-20 on the A7 II (body A7II00SERIAL): `focusindication` reads "Unlock"
# whenever nothing is driving AF, and reaches "Focus Locked" only WHILE an autofocus
# half-press is actively held. By the time we freeze focus the meter capture has already
# released AF, so a passive read/poll can only ever time out on this body — no deadline
# fixes it. So: read once (the A7 III still reports the meter lock here, fast path), and if
# that is not locked, drive ONE bounded half-press to acquire focus before freezing.
# Hold time: on the A7 II 2.5 s locked reliably while held, 1.5 s did not; capped by the
# camera's own AUTOFOCUS_MAX_HOLD_S (3.0 s). This is a real AF search, recorded as such.
FOCUS_AF_DRIVE_HOLD_S = 2.5
# Sony `focusindication` states that mean AF converged, and the one definitive negative.
FOCUS_LOCKED_INDICATIONS = frozenset({"Focus Locked", "Tracking Focused"})
FOCUS_NEGATIVE_INDICATIONS = frozenset({"No Focus - Low Contrast"})


def _resolve_shots_dir() -> Path:
    """Where finished shots land for the API to serve.

    CAMERA_AUTOPILOT_SHOTS_DIR overrides everything. Without it, only a hardware engine
    (CAMERA_ENGINE=real|wifi) writes to the real gallery `backend/shots/`; the mock engine
    (the default when CAMERA_ENGINE is unset) keeps its demo captures in the gitignored
    `artifacts/demo-shots/` so simulator smokes and tests never interleave with photographs.
    """
    override = os.getenv("CAMERA_AUTOPILOT_SHOTS_DIR")
    if override:
        return Path(override).expanduser().absolute()
    if os.getenv("CAMERA_ENGINE") in ("real", "wifi"):
        return Path(__file__).resolve().parent / "shots"
    return ENGINE_REPO / "artifacts" / "demo-shots"


SHOTS_DIR = _resolve_shots_dir()
SAMPLE_DIR = ENGINE_REPO / "captures"


def _png_chunk(kind: bytes, data: bytes) -> bytes:
    return (
        struct.pack(">I", len(data))
        + kind
        + data
        + struct.pack(">I", zlib.crc32(kind + data) & 0xFFFFFFFF)
    )


def _mock_frame_png(width: int = 720, height: int = 480) -> bytes:
    """Generate a small stand-in frame when the checkout has no sample captures."""
    rows = bytearray()
    for y in range(height):
        rows.append(0)  # PNG filter: none
        ny = y / max(1, height - 1)
        for x in range(width):
            nx = x / max(1, width - 1)
            glow = max(0.0, 1.0 - (((nx - 0.58) ** 2 + (ny - 0.44) ** 2) * 5.5))
            r = int(18 + (95 * glow) + (30 * nx))
            g = int(19 + (70 * glow) + (16 * ny))
            b = int(26 + (38 * glow) + (38 * (1.0 - ny)))
            rows.extend((min(r, 255), min(g, 255), min(b, 255)))

    raw = zlib.compress(bytes(rows), level=6)
    return (
        b"\x89PNG\r\n\x1a\n"
        + _png_chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0))
        + _png_chunk(b"IDAT", raw)
        + _png_chunk(b"IEND", b"")
    )


@dataclass
class Shot:
    ok: bool
    image: str        # filename served under /shots
    summary: str      # one-line, plain-language "what it did"
    why: str          # the optional deeper explanation (shown only if the user taps)
    mood: dict        # visible mood/vibe card for the result overlay
    settings: dict    # final readback, not just requested values
    cards: list | None = None
    applied: dict = field(default_factory=dict)
    evidence: dict = field(default_factory=dict)


@dataclass
class Recording:
    ok: bool
    image: str         # poster frame served under /shots
    summary: str       # one-line, plain-language "what it did"
    why: str           # optional deeper explanation
    mood: dict         # visible mood/vibe card for the result overlay
    settings: dict     # final settings
    recording: dict    # native recording report: duration, SD-card destination, samples


def _save_into_shots(source: Path | None) -> str:
    if SHOTS_DIR.is_symlink():
        raise ValueError("gallery root must not be a symlink")
    SHOTS_DIR.mkdir(mode=0o700, parents=True, exist_ok=True)
    os.chmod(SHOTS_DIR, 0o700)
    suffix = source.suffix.lower() if source else ".png"
    name = f"shot_{time.time_ns()}{suffix}"
    if source:
        # Camera files may be RAW and should never be materialized as one Python
        # bytes object just to copy them into the private gallery.  A bounded
        # streaming copy also rejects a file that changes under us.
        destination = SHOTS_DIR / name
        temporary = destination.with_name(f".{destination.name}.tmp")
        copied = 0
        try:
            # Open the object first, never follow a symlink, and use the one
            # descriptor for both identity checks and copying.  Path stats are
            # intentionally absent: an attacker cannot swap the pathname after
            # validation and change what we copy.
            source_fd = os.open(source, os.O_RDONLY | os.O_NOFOLLOW)
            with os.fdopen(source_fd, "rb", closefd=True) as src, os.fdopen(
                os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600), "wb") as dst:
                before = os.fstat(src.fileno())
                if not stat.S_ISREG(before.st_mode):
                    raise ValueError("camera capture must be a regular file")
                expected_size = before.st_size
                if expected_size < 1 or expected_size > MAX_IMAGE_SOURCE_BYTES:
                    raise ValueError("camera capture exceeds gallery byte budget")
                while chunk := src.read(64 * 1024):
                    copied += len(chunk)
                    if copied > expected_size:
                        raise ValueError("camera capture changed during gallery copy")
                    dst.write(chunk)
                dst.flush()
                os.fsync(dst.fileno())
                after = os.fstat(src.fileno())
            if (
                copied != expected_size
                or (before.st_dev, before.st_ino, before.st_size)
                != (after.st_dev, after.st_ino, after.st_size)
            ):
                raise ValueError("camera capture changed during gallery copy")
            temporary.replace(destination)
        except Exception:
            temporary.unlink(missing_ok=True)
            raise
    else:
        with os.fdopen(os.open(SHOTS_DIR / name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600), "wb") as output:
            output.write(_mock_frame_png())
    return name


def _safe_image_name(value, fallback: str) -> str:
    """Keep gallery URLs inside /shots even if a sidecar is hand-edited or corrupt."""
    name = Path(str(value or fallback)).name
    return name or fallback


def save_shot_record(shot: Shot, intent: str | None) -> Path:
    """Persist a shot's record beside its image as SHOTS_DIR/<image-stem>.json.

    The image bytes already land in SHOTS_DIR (via _save_into_shots); this writes the metadata
    sidecar so the shot can be reviewed later. Returns the sidecar path.
    """
    SHOTS_DIR.mkdir(parents=True, exist_ok=True)
    stem = Path(shot.image).stem
    sidecar = SHOTS_DIR / f"{stem}.json"
    record = {
        "image": shot.image,
        "summary": shot.summary,
        "why": shot.why,
        "settings": shot.settings,
        "cards": getattr(shot, "cards", None),
        "mood": getattr(shot, "mood", {}),
        "applied": getattr(shot, "applied", {}),
        "evidence": getattr(shot, "evidence", {}),
        "intent": intent or None,
        # Explicit provenance: the mock engine stamps evidence.engine="mock" so Review can tell a
        # demo record from a photograph even if both ever share a directory.
        "engine": (getattr(shot, "evidence", None) or {}).get("engine"),
        "ts": time.time(),
    }
    # Atomic write so a GET /gallery running concurrently never reads a half-written sidecar.
    tmp = sidecar.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(record, ensure_ascii=False), encoding="utf-8")
    os.chmod(tmp, 0o600)
    os.replace(tmp, sidecar)
    from photo_lifecycle import manage_shot
    manage_shot(SHOTS_DIR, shot.image)
    return sidecar


def _as_ts(value) -> float:
    """Coerce a sidecar's ts to a finite sortable float; corrupted/non-finite values sort oldest."""
    try:
        ts = float(value)
    except (TypeError, ValueError):
        return 0.0
    return ts if math.isfinite(ts) else 0.0


def gallery_records(verdict_for=None) -> list[dict]:
    """Enumerate the shot_*.json sidecars in SHOTS_DIR, newest-first.

    Each record becomes {image: '/shots/<name>', summary, why, settings, intent, ts, verdict}.
    `verdict_for` is an optional callable(image_filename) -> str|None used to attach the thumbs
    reaction. Malformed or partial sidecars are tolerated; a shot image with no sidecar is simply
    not listed (the caller should not crash on it).
    """
    records: list[dict] = []
    for sidecar in [*SHOTS_DIR.glob("shot_*.json"), *SHOTS_DIR.glob("edit_*.json"), *SHOTS_DIR.glob("session_*.json")]:
        if sidecar.is_symlink() or sidecar.stat().st_size > 64 * 1024:
            continue
        try:
            data = json.loads(sidecar.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        image = _safe_image_name(data.get("image"), f"{sidecar.stem}.jpg")
        verdict = verdict_for(image) if verdict_for else None
        records.append({
            "image": f"/shots/{image}",
            "engine": _record_engine(data),
            "summary": data.get("summary", ""),
            "why": data.get("why", ""),
            "settings": data.get("settings", {}),
            "cards": data.get("cards"),
            "mood": data.get("mood", {}),
            "applied": data.get("applied", {}),
            "evidence": data.get("evidence", {}),
            "sequence": data.get("sequence"),
            "thumbnail": f"/thumbnails/{image}",
            "intent": data.get("intent"),
            "ts": _as_ts(data.get("ts")),
            "verdict": verdict,
        })
    records.sort(key=lambda record: record["ts"], reverse=True)
    return records


def _record_engine(data: dict) -> str | None:
    """Provenance of a sidecar. New mock records carry a top-level `engine`; older demo records
    only say so in their evidence (source "mock", no physical capture)."""
    engine_name = data.get("engine")
    if isinstance(engine_name, str) and engine_name:
        return engine_name
    evidence = data.get("evidence") if isinstance(data.get("evidence"), dict) else {}
    if evidence.get("source") == "mock" or evidence.get("physical_capture") is False:
        return "mock"
    return None


def _readback_cards(settings: dict, reason: str) -> list[dict]:
    return [{"axis":axis,"value":str(settings.get(key,"unknown")),"reason":reason,
             "status":"pending","sub_factors":[],"evidence":"readback",
             "outcome_verified":False} for axis,key in
            (("SHUTTER","shutter"),("APERTURE","aperture"),("ISO","iso"))]


class MockEngine:
    """No hardware: returns the balanced frame from the real session so the UX is fully testable."""

    name = "mock"

    def status(self) -> dict:
        return {"engine": "mock", "ready": True, "camera": "demo"}

    def shoot(self, intent: str | None, target_override: float | None = None,
              variant_id: str | None = None, applied_plan: dict | None = None) -> Shot:
        plan = (deepcopy(applied_plan) if applied_plan is not None else
                intent_brain.interpret(intent, variant_id=variant_id) if variant_id else intent_brain.interpret(intent))
        # target_override lets the closed reflexion loop re-shoot at a CORRECTED brightness target
        # (the vision critique only ever moves this number). Mock has no real sensor, so the override
        # just rides along in the plan/why; the contract is what matters: a corrected re-shoot path.
        if target_override is not None:
            plan["target_brightness"] = target_override
        name = _save_into_shots(None)
        from image_safety import open_rgb
        original = SHOTS_DIR / name
        with open_rgb(original) as image:
            jpeg = original.with_suffix(".jpg")
            image.save(jpeg, format="JPEG", quality=90)
        os.chmod(jpeg, 0o600)
        original.unlink()
        name = jpeg.name
        return Shot(
            ok=True,
            image=name,
            summary=f"Demo preview — {plan['feel']}.",
            why=plan["why"],
            mood=plan["mood"],
            settings={"iso": "400", "aperture": "f/2.8", "shutter": "1/30", "white_balance": "Demo"},
            cards=_readback_cards({"iso": "400", "aperture": "f/2.8", "shutter": "1/30"}, "Demonstration only"),
            applied={"target_brightness": plan["target_brightness"], "variant_id": variant_id},
            evidence={"source": "mock", "engine": "mock", "physical_capture": False},
        )

    def record(self, intent: str | None, seconds: float = 10.0) -> Recording:
        plan = intent_brain.interpret(intent)
        name = _save_into_shots(None)
        try:
            seconds = float(seconds)
        except (TypeError, ValueError):
            raise ValueError("recording duration must be numeric") from None
        if not math.isfinite(seconds) or seconds <= 0:
            raise ValueError("recording duration must be finite and positive")
        seconds = round(min(max(0.5, seconds), 300.0), 1)
        settings = {"iso": "400", "aperture": "f/2.8", "shutter": "1/60", "white_balance": "Auto"}
        return Recording(
            ok=True,
            image=name,
            summary=f"Recorded it — {plan['feel']}.",
            why="Simulated native video recording while the autopilot kept checking the light.",
            mood=plan["mood"],
            settings=settings,
            recording={
                "recorded_seconds": seconds,
                "saved_to": "demo recording (mock)",
                "samples": [{"sample": 1, "mean": plan["target_brightness"], "settings": settings}],
                "final_mean": plan["target_brightness"],
            },
        )


USB_CONTROL_MODELS = {"Sony ILCE-7M2 (Control)", "Sony Alpha-A7 III (PC Control)"}
EXPOSURE_KEYS = ("iso", "aperture", "shutter")


def _meter_restore_receipt(status: str | None, error: str | None = None) -> dict | None:
    """The long route's restore outcome in #72's meter_restore shape (completed / unchanged /
    failed + reason), so HTTP and MCP report restore warnings the same way for both refusals."""
    if status == "verified":
        return {"status": "completed"}
    if status == "not_needed":
        return {"status": "unchanged"}
    if status == "failed":
        return {"status": "failed", "reason": error or "restore not confirmed"}
    return None


def _restore_exposure(cam, target: dict) -> dict:
    """Put ISO, aperture and shutter back to ``target`` on this connection: writes and readback
    only, never a shutter. Reports the outcome; never raises over the caller's own failure."""
    try:
        from sonyremote.camera import read_settings_checked
        # Never write onto a body still answering placeholder exposure (refused, and reported).
        current, _ = read_settings_checked(cam, purpose="pre-meter exposure restore")
        if all(str(current.get(key)) == str(target[key]) for key in EXPOSURE_KEYS):
            return {"status": "unchanged"}
        cam.set_exposure(iso=target["iso"], aperture=target["aperture"], shutter=target["shutter"])
        actual = cam.get_settings()
        if any(str(actual.get(key)) != str(target[key]) for key in EXPOSURE_KEYS):
            return {"status": "failed", "reason": "readback differs from the pre-meter exposure"}
        return {"status": "completed"}
    except Exception as exc:  # noqa: BLE001 — reported, never masks the original failure
        return {"status": "failed", "reason": f"{type(exc).__name__}: {exc}"}


def reserve_bracket_headroom(metered: dict, caps, ev_offsets, max_shutter_s: float, *,
                             min_iso: int, max_iso: int) -> dict | None:
    """Keep every bracket frame on the live shutter ladder by moving stops between shutter and ISO.

    A dim scene can meter to the USB shutter cap, so positive frames would exceed it; a bright scene
    can meter to the fastest shutter, so negative frames would be clamped back onto the baseline and
    duplicate it. The baseline shutter becomes the live label closest to the metered one whose every
    frame (snapped as the session snaps it) stays on the ladder and within the transport limits,
    and ISO moves by the stops the shutter changed (aperture held) within [``min_iso``, ``max_iso``],
    the rig's base ISO and usable-ISO ceiling (brain.usable_iso_range, the recommendation's own
    quality gate; extended low ISOs lie below it). Returns None when nothing needs to change. Raises CameraError,
    before any bracket frame, when that range cannot absorb it: never a silent exposure change.
    """
    import camera_caps
    from sonyremote import CameraError
    from sonyremote.camera import validate_sony_capture_shutter
    every = sorted({float(ev) for ev in ev_offsets})
    offsets = [ev for ev in every if ev != 0]
    if not offsets:
        return None

    values = sorted(caps.shutters)
    # A frame past either end of the live ladder by more than half that end's own step would be
    # silently clamped onto it (the same rule as the session's whole-ladder check).
    fastest_half_step = math.log2(values[1] / values[0]) / 2 if len(values) > 1 else 1 / 6
    slowest_half_step = math.log2(values[-1] / values[-2]) / 2 if len(values) > 1 else 1 / 6

    def fits(seconds):
        # Judge the shutter the session will apply: each frame is snapped on this live ladder, and
        # no two frames (the baseline included) may land on the same label.
        if len({caps.snap_shutter(seconds * 2 ** ev)[1] for ev in every}) != len(every):
            return False
        for ev in offsets:
            wanted = seconds * 2 ** ev
            snapped, label = caps.snap_shutter(wanted)
            if snapped > max_shutter_s * (1 + 1e-6):
                return False
            try:
                validate_sony_capture_shutter(label)   # the USB transport's own limits
            except CameraError:
                return False
            if wanted > values[-1] and math.log2(wanted / values[-1]) > slowest_half_step + 1e-9:
                return False
            if wanted < values[0] and math.log2(values[0] / wanted) > fastest_half_step + 1e-9:
                return False
        return True
    base_s = camera_caps._shutter_seconds(str(metered["shutter"]))
    iso = float(metered["iso"])
    if fits(base_s):
        # Nothing to move, but the baseline itself must lie inside the range compensation uses.
        if iso > max_iso or iso < min_iso:
            raise CameraError(
                f"The Fixed HDR baseline ISO {metered['iso']} is outside this rig's usable ISO range "
                f"({min_iso} to the usable ISO ceiling {max_iso}); no bracket frame was issued.")
        return None
    candidates = sorted(((abs(math.log2(value / base_s)), value, label)
                         for value, label in zip(caps.shutters, caps.shutter_labels) if fits(value)))
    if not candidates:
        raise CameraError("No shutter on this body keeps every Fixed HDR frame on its shutter ladder "
                          f"within the {max_shutter_s:g} s USB limit; no bracket frame was issued. Use fewer frames.")
    _, shutter_s, shutter_label = candidates[0]
    stops = math.log2(base_s / shutter_s)       # positive: shorter shutter, more ISO
    wanted_iso = iso * 2 ** stops
    usable = [value for value in caps.isos if min_iso <= value <= max_iso]
    iso_value = min(usable, key=lambda value: abs(math.log2(value) - math.log2(wanted_iso))) if usable else None
    if iso_value is None or abs(math.log2(iso_value / wanted_iso)) > 1 / 6 + 1e-9:
        limit = (f"above this rig's usable ISO ceiling {min(max_iso, max(caps.isos))}" if stops > 0
                 else f"below this rig's lowest ISO {max(min_iso, min(caps.isos))} (its base ISO)")
        raise CameraError(
            f"The Fixed HDR bracket needs ISO {wanted_iso:.0f} (shutter {shutter_label} instead of "
            f"{metered['shutter']}) to keep every frame on the shutter ladder, {limit}; no bracket "
            "frame was issued. Use fewer frames, or change the light or the aperture.")
    iso_label = str(iso_value)
    positive = [ev for ev in offsets if ev > 0]
    negative = [ev for ev in offsets if ev < 0]
    return {"max_positive_ev": max(positive) if positive else None,
            "min_negative_ev": min(negative) if negative else None, "max_shutter_s": max_shutter_s,
            "recommended": {key: metered[key] for key in EXPOSURE_KEYS},
            "adjusted": {"iso": iso_label, "aperture": metered["aperture"], "shutter": shutter_label},
            "stops_moved_to_iso": round(stops, 3),
            "iso_stops_achieved": round(math.log2(iso_value / iso), 3), "usable_iso_ceiling": max_iso,
            "base_iso": min_iso}

# The long single-exposure route (> 1 s shutters for light trails / silky water) is off unless the
# server is started with CAMERA_LONG_EXPOSURE=1: its transport is proven only once (A7 II, 30 s).
LONG_EXPOSURE_DISABLED_NOTE = ("Long exposure is off on this server, so this photo is limited to 1 s; "
                               "start it with CAMERA_LONG_EXPOSURE=1 to let light trails lengthen the shutter.")


def long_exposure_enabled() -> bool:
    return os.environ.get("CAMERA_LONG_EXPOSURE", "").strip() == "1"


LONG_RESTORE_WARNING = "check the camera's ISO, shutter and focus mode before the next shot"
_UNRESTORED = ("failed", "deferred_until_physical_idle_verified")


def long_refusal_restore_state(exc) -> dict:
    """What a long-route refusal (final_shutter_not_issued) must carry in ANY response, identity
    refusals included: the focus policy, the meter restore receipt and, when putting the body back
    did not succeed, a warning to check it. Empty for every other error."""
    if not getattr(exc, "final_shutter_not_issued", False):
        return {}
    policy = getattr(exc, "focus_policy", None) or {}
    restore = getattr(exc, "meter_restore", None)
    state = {"final_shutter_issued": False, "focus_policy": policy or None}
    if restore:
        state["meter_restore"] = restore
    if ((restore or {}).get("status") == "failed" or policy.get("restore") in _UNRESTORED
            or policy.get("exposure_restore") in _UNRESTORED):
        state["warning"] = f"The long exposure's settings were not fully restored; {LONG_RESTORE_WARNING}."
    return state


WB_KELVIN = {"warm": 7500, "cool": 4000}  # higher Kelvin renders warmer; see Camera.set_color_temp


def apply_kelvin_white_balance(cam, requested: str, kelvin: int, before: str | None) -> dict:
    """Set a warm/cool Kelvin white balance, or put the earlier white balance back before any shutter.

    `set_color_temp` switches `whitebalance` to its Kelvin choice and then writes the Kelvin value.
    On the A7 II (live, 2026-09-29) the choice took but 7500 K did not, and the "warm" photo was taken
    at the body's 4000 K, cooler than Auto. So when the value is not set and read back, the white
    balance read before the write (`before`; Auto when that was itself a Kelvin choice or unknown) is
    restored and read back, and the result says why. If even that fails, CapturePreconditionError
    refuses the capture before any shutter: a half-applied Kelvin mode is never photographed.
    """
    from sonyremote import CameraError, CapturePreconditionError
    wb = {"requested": requested, "kelvin": kelvin, "verified": bool(cam.set_color_temp(kelvin))}
    if wb["verified"]:
        return wb
    target = before if before and "color temp" not in str(before).lower() else None
    try:
        if target is None:
            target = next((x for x in cam.list_options("white_balance") if "auto" in x.lower()), None)
            if target is None:
                raise CameraError("the body lists no Auto white balance to fall back to")
        cam.set_setting("white_balance", target)
        readback = cam.get_settings().get("white_balance")
        if readback != target:
            raise CameraError(f"white balance read back {readback!r}, not {target!r}")
    except CameraError as exc:
        # Refused before any trigger of this capture: a later attempt of the same request (Refine)
        # is then reported by product_capture with the earlier photo kept, never as "nothing fired".
        raise CapturePreconditionError(
            f"The {requested} white balance was not applied: the camera did not take {kelvin} K, and putting "
            f"its white balance back to {target!r} failed ({exc}). No shutter was issued for this capture; "
            "set the white balance on the camera and try again.") from exc
    wb.update(white_balance_before=before, restored_white_balance=target,
              reason=f"the camera did not take {kelvin} K (set and read back), so white balance was put back "
                     f"to {target} before the shutter")
    return wb


def white_balance_note(wb: dict) -> str:
    """The sentence a result carries when a warm/cool request was not applied on this body."""
    if wb.get("kelvin") and wb.get("verified") is False and wb.get("restored_white_balance"):
        return (f" The {wb['requested']} colour was not applied: this camera did not take {wb['kelvin']} K, "
                f"so it shot with {wb['restored_white_balance']} white balance.")
    return ""


def serial_evidence(cam) -> dict:
    """The USB serial this connection proved (sonyremote.Camera), for shot and session receipts."""
    serial = getattr(cam, "verified_serial", None)
    verification = getattr(cam, "serial_verification", None)
    return {"camera_serial": serial if isinstance(serial, str) else None,
            "serial_verification": dict(verification) if isinstance(verification, dict) else None}


def _readback_f_number(label) -> float | None:
    """The verified applied aperture from a settings readback ("f/5.6", "5.6"), or None."""
    try:
        value = float(str(label).strip().lower().removeprefix("f/").removeprefix("f"))
    except (TypeError, ValueError):
        return None
    return value if math.isfinite(value) and value > 0 else None


class RealEngine:
    """USB Sony control; new bodies use live metering until their physics is calibrated."""

    name = "real"

    def __init__(self, camera_model: str | None = None, camera_port: str | None = None,
                 camera_serial: str | None = None):
        self.camera_model = camera_model or os.getenv("CAMERA_MODEL")
        self.camera_port = camera_port or os.getenv("CAMERA_PORT")
        # USB ports re-enumerate; the serial is the only stable identity. No capture without it.
        self.camera_serial = camera_serial or os.getenv("CAMERA_SERIAL") or None
        if str(ENGINE_REPO) not in sys.path:
            sys.path.insert(0, str(ENGINE_REPO))

    def _camera(self):
        from sonyremote import Camera, CameraIdentityError
        if not self.camera_serial:
            raise CameraIdentityError(
                "Select the camera again (or set CAMERA_SERIAL): product capture requires the "
                "body's USB serial.", reason="serial_required",
                model=self.camera_model, port=self.camera_port)
        kwargs = {"serial": self.camera_serial}
        if self.camera_model:
            kwargs["model"] = self.camera_model
        if self.camera_port:
            kwargs["port"] = self.camera_port
        camera = Camera(**kwargs)
        # Identity-unproven downloads are preserved privately in the shot store, never published.
        camera.quarantine_dir = SHOTS_DIR / "quarantine"
        return camera

    @staticmethod
    def _serial_status(row: dict) -> str:
        """"duplicate" (shared by two OS nodes), "missing" (none readable) or "present"."""
        return row.get("serial_status") or ("present" if row.get("serial") else "missing")

    def status(self) -> dict:
        from sonyremote import detect_identified
        rows = detect_identified()
        matching = [r for r in rows
                    if (not self.camera_model or r["model"] == self.camera_model)
                    and (not self.camera_port or r["port"] == self.camera_port)
                    and (not self.camera_serial or r.get("serial") == self.camera_serial)]
        model = matching[0]["model"] if len(matching) == 1 else None
        serial = matching[0].get("serial") if len(matching) == 1 else None
        serial_status = self._serial_status(matching[0]) if len(matching) == 1 else None
        # A body without a unique USB serial cannot be told apart from a second body of the same
        # model that takes its port, so it is never "ready" and never a capture target.
        ready = model in USB_CONTROL_MODELS and serial_status == "present"
        if len(matching) > 1:
            hint = "Choose the camera to use."
        elif model not in USB_CONTROL_MODELS:
            hint = "Set USB Connection to PC Remote and wake the camera."
        elif serial_status == "duplicate":
            hint = "Two USB devices report this camera's serial; its identity cannot be proven, so capture is disabled."
        elif serial_status != "present":
            hint = "This camera reports no USB serial; its identity cannot be proven, so capture is disabled."
        else:
            hint = None
        # `model`/`port`/`supported` stay intact for the web picker and test_camera_labels;
        # `identification` and `serial` tell which physical body a row is, since the driver
        # labels lie and the owner has two Sony bodies (.claude/lessons.md, Identification).
        return {"engine": "real", "ready": ready,
                "camera": model, "serial": serial, "serial_status": serial_status,
                "cameras": [{"model": r["model"], "port": r["port"],
                  "supported": r["model"] in USB_CONTROL_MODELS,
                  "identification": r["method"], "serial": r.get("serial"),
                  "serial_status": self._serial_status(r)} for r in rows],
                "selection_required": len(matching) > 1,
                "hint": hint}

    def shoot(self, intent: str | None, target_override: float | None = None,
              variant_id: str | None = None, applied_plan: dict | None = None) -> Shot:
        from sonyremote import CameraError, CameraIdentityError, CaptureOutcomeUncertain, PlaceholderSettingsError
        engine_started = time.monotonic()
        plan = (deepcopy(applied_plan) if applied_plan is not None else
                intent_brain.interpret(intent, variant_id=variant_id) if variant_id else intent_brain.interpret(intent))
        if target_override is not None:
            plan["target_brightness"] = target_override
        _free_usb()
        with self._camera() as cam:
            identity = cam.info()
            model = identity.model
            if model not in USB_CONTROL_MODELS:
                raise CameraError(f"USB Autopilot is not configured for {model}.")
            if not cam.wake():
                raise CameraError("Camera is not reporting active exposure settings. Wake it and exit menus.")
            if hasattr(cam, "shooting_mode") and cam.shooting_mode() != "M":
                raise CameraError("Turn the physical mode dial to M for predictable autonomous exposure.")
            from sonyremote.camera import read_settings_checked
            # A body still answering placeholders (f/0, ISO 0, 65535/65535) refuses before the first
            # write below (white balance), not after it.
            pre_write_settings, pre_write_read = read_settings_checked(cam, purpose="before first write")
            wb = {"requested": plan.get("white_balance"), "verified": False}
            kelvin = WB_KELVIN.get(plan.get("white_balance"))
            if kelvin:
                wb = apply_kelvin_white_balance(cam, plan["white_balance"], kelvin,
                                                pre_write_settings.get("white_balance"))
            # No colour direction in the request means Auto. Leaving white balance untouched let an
            # earlier "warm" shot's 7500 K carry into every later neutral shot (live, 2026-09-22).
            elif plan.get("white_balance") in ("auto", None):
                wb["requested"] = "auto"
                try:
                    options = cam.list_options("white_balance")
                    auto = next((x for x in options if "auto" in x.lower()), None)
                    if auto:
                        cam.set_setting("white_balance", auto)
                        wb["verified"] = cam.get_settings().get("white_balance") == auto
                except CameraError as exc:
                    wb["reason"] = str(exc)
            plan["white_balance_result"] = wb
            try:
                shot = self._brain_shoot(cam, plan)
            except CaptureOutcomeUncertain:
                raise
            except (CameraIdentityError, PlaceholderSettingsError) as exc:
                # A clean refusal of the next operation: never wrapped as "may have fired".
                # An earlier exposure of this request (the meter frame) stays preserved and counted.
                exc.capture_path = (exc.capture_path or plan.get("_last_capture_path")
                                    or plan.get("_meter_capture_path"))
                if plan.get("_meter_exposures"):
                    exc.prior_exposures = plan["_meter_exposures"]
                raise
            except Exception as exc:
                if plan.get("_camera_started") and getattr(exc, "final_shutter_not_issued", False):
                    # Only the meter frame fired; the long exposure was refused before its trigger.
                    exc.capture_path = plan.get("_meter_capture_path")
                    raise
                if plan.get("_camera_started"):
                    failure = CaptureOutcomeUncertain(
                        f"Processing stopped after capture; original files were preserved: {exc}",
                        capture_path=plan.get("_last_capture_path"))
                    failure.focus_policy = plan.get("_final_focus_policy")
                    failure.transport_receipt = getattr(cam, "last_capture_receipt", None)
                    raise failure from exc
                if not isinstance(exc, (ImportError, FileNotFoundError, ValueError)):
                    raise
                if plan.get("exposure_route") == "long_single_exposure" and long_exposure_enabled():
                    # The JPEG-feedback fallback only takes an ordinary <= 1 s photo; a long request
                    # must be refused, not silently shortened. Nothing was written or fired yet.
                    from sonyremote import CapturePreconditionError
                    raise CapturePreconditionError(
                        f"A long exposure cannot be planned here ({exc}): it needs the planner's known "
                        "aperture, and the fallback only takes ordinary photos of up to 1 s. No shutter "
                        "was issued.") from exc
                shot = self._autoexpose_shoot(cam, plan, fallback=str(exc))
            shot.applied.update({"white_balance": wb, "target_brightness": plan["target_brightness"],
                                 "variant_id": variant_id})
            shot.summary += white_balance_note(wb)
            shot.evidence.update({"camera": model, "port": getattr(identity, "port", None),
                                  **serial_evidence(cam), "physical_capture": True,
                                  **({"pre_write_settings_read": pre_write_read} if pre_write_read else {})})
            shot.evidence.setdefault("timings", {}).update(
                clock="monotonic", engine_until_result_s=time.monotonic() - engine_started)
            return shot

    def meter_baseline(self, intent: str | None, variant_id: str | None = None,
                       applied_plan: dict | None = None, bracket_ev_offsets=()) -> dict:
        """Meter a Fixed stack's baseline: the /shoot meter frame and bounded re-meter, no final photo.

        The body's exposure before metering is returned as ``inherited`` and ``restore_baseline``:
        it is what the stack restores afterwards, never what it brackets around. Any failure after
        the meter shutter is uncertain and terminal; no stack frame follows it.
        """
        from sonyremote import (CameraError, CameraIdentityError, CaptureOutcomeUncertain,
                                PlaceholderSettingsError)
        from sonyremote.camera import read_settings_checked
        plan = (deepcopy(applied_plan) if applied_plan is not None else
                intent_brain.interpret(intent, variant_id=variant_id) if variant_id else intent_brain.interpret(intent))
        _free_usb()
        with self._camera() as cam:
            identity = cam.info()
            if identity.model not in USB_CONTROL_MODELS:
                raise CameraError(f"USB Autopilot is not configured for {identity.model}.")
            if not cam.wake():
                raise CameraError("Camera is not reporting active exposure settings. Wake it and exit menus.")
            if hasattr(cam, "shooting_mode") and cam.shooting_mode() != "M":
                raise CameraError("Turn the physical mode dial to M for predictable autonomous exposure.")
            # The stack's restore target: a placeholder read refuses here, before the meter shutter.
            inherited, _ = read_settings_checked(cam, purpose="stack restore baseline")
            if any(key not in inherited for key in ("iso", "aperture", "shutter", "focus_mode")):
                raise CameraError("Cannot establish the body's current settings; no meter photograph was issued.")
            exposure_keys = EXPOSURE_KEYS

            def restore_inherited() -> dict:
                # Metering may have replaced Auto ISO or the shutter, or re-metered; no session
                # exists yet to restore it.
                return _restore_exposure(cam, inherited)

            try:
                metered = self._brain_shoot(cam, plan, meter_only=True)
                headroom = None
                if any(ev != 0 for ev in bracket_ev_offsets):
                    import camera_caps
                    from sonyremote.camera import SONY_ACTION_MAX_ENABLED_SHUTTER_S
                    # The session snaps each frame on this unconstrained live ladder.
                    caps = camera_caps.from_live_choices(identity.model, cam.list_options("iso"),
                                                         cam.list_options("aperture"), cam.list_options("shutter"))
                    headroom = reserve_bracket_headroom(metered["metered"], caps, bracket_ev_offsets,
                                                        SONY_ACTION_MAX_ENABLED_SHUTTER_S,
                                                        min_iso=metered["usable_iso_range"][0],
                                                        max_iso=metered["usable_iso_range"][1])
                    if headroom:
                        metered["metered"] = dict(headroom["adjusted"])
                metered["bracket_headroom"] = headroom
            except (CameraIdentityError, PlaceholderSettingsError) as exc:
                # Never write settings onto a body whose serial was refused or that answers
                # placeholder exposure. It stays a clean refusal of this operation; meter frames
                # this request already fired are counted.
                exc.capture_path = exc.capture_path or plan.get("_meter_capture_path")
                if plan.get("_meter_exposures"):
                    exc.prior_exposures = plan["_meter_exposures"]
                if plan.get("_exposure_written"):
                    # Metering changed ISO or the shutter, and the body can no longer be trusted
                    # with a write: nothing is written back, so say the exposure may still be the
                    # meter's.
                    why = ("the body answered placeholder exposure" if isinstance(exc, PlaceholderSettingsError)
                           else "the camera's identity could not be verified")
                    exc.meter_restore = {"status": "skipped_unverified_body",
                                         "reason": f"{why} after metering changed the exposure, so no restore was written"}
                raise
            except Exception as exc:
                restore = restore_inherited()
                restored = ("the pre-meter exposure was restored" if restore["status"] in ("completed", "unchanged")
                            else f"the pre-meter exposure could not be restored ({restore.get('reason')})")
                if isinstance(exc, CaptureOutcomeUncertain):
                    exc.args = (f"{exc.args[0]} {restored[0].upper()}{restored[1:]}.",) + exc.args[1:]
                    exc.meter_restore = restore
                    if plan.get("_meter_exposures") and getattr(exc, "prior_exposures", None) is None:
                        exc.prior_exposures = plan["_meter_exposures"]
                    raise
                if plan.get("_camera_started"):
                    failure = CaptureOutcomeUncertain(
                        f"Metering stopped after the meter photograph; no stack frame was issued and "
                        f"{restored}: {exc}", capture_path=plan.get("_meter_capture_path"))
                    failure.transport_receipt = getattr(cam, "last_capture_receipt", None)
                    failure.meter_restore = restore
                    if plan.get("_meter_exposures"):
                        failure.prior_exposures = plan["_meter_exposures"]  # frames known to have fired
                    raise failure from exc
                if isinstance(exc, CameraError):
                    exc.meter_restore = restore
                    raise
                failure = CameraError(f"Stack metering is unavailable ({type(exc).__name__}: {exc}); "
                                      f"no photograph was issued and {restored}.")
                failure.meter_restore = restore
                raise failure from exc
            # The stack session sets each frame's exposure itself; the body waits at the exposure it
            # had, so a request that stops before the session exists leaves nothing behind.
            metered["meter_restore"] = restore_inherited()
            metered.update(inherited={key: inherited.get(key) for key in exposure_keys},
                           restore_baseline=dict(inherited), camera=identity.model,
                           port=getattr(identity, "port", None), **serial_evidence(cam))
            return metered

    def _confirm_focus_lock(self, cam, policy, original_mode):
        """Confirm — or actively acquire — an AF lock before focus is frozen into Manual.

        Fast path: read `focusindication` once; if the body already reports a locked state
        (the A7 III still reports the meter lock here) freeze immediately with no further AF.
        Otherwise drive ONE bounded autofocus half-press and use its indication, plus one
        post-release read (the A7 II reports "Focus Locked" only after release), then freeze.

        Records the outcome and which branch ran under `policy["af_focus_lock"]`, and rewrites
        `policy["reason"]` to describe what actually happened. Confirming an AF lock is not
        proof of optical sharpness. Never raises, never fires the shutter, never adds an
        exposure: a missing node, a missing helper or any read/half-press error proceeds
        exactly as a capture with no confirmation.
        """
        af_lock = {"node": "focusindication", "confirmed": False, "indication": None,
                   "recommend_refine": False, "af_search": "none",
                   "note": "focusindication reports AF convergence, not optical sharpness"}
        policy["af_focus_lock"] = af_lock
        started = time.monotonic()
        try:
            if original_mode == "Manual":
                # Metering used manual focus; there is no autofocus lock to confirm or drive.
                af_lock.update(basis="manual_focus_no_af_search", af_search="none_manual_focus")
                policy["reason"] = ("Held the manually-focused meter position in Manual for the single "
                                    "final exposure; no autofocus search applies. Optical focus is unverified.")
                return
            read_indication = getattr(cam, "read_focus_indication", None)
            if not callable(read_indication):
                af_lock.update(basis="confirmation_unavailable", af_search="unavailable_no_indication")
                return
            try:
                indication = read_indication()
            except Exception as exc:
                af_lock.update(basis="confirmation_unavailable", af_search="none", read_error=str(exc))
                return
            af_lock["indication"] = indication
            if indication is None:
                af_lock.update(basis="node_absent", af_search="none")
                return
            if indication in FOCUS_LOCKED_INDICATIONS:
                # Already locked (A7 III meter lock persists here): no further AF search.
                af_lock.update(confirmed=True, basis="confirmed", af_search="none_already_locked")
                policy["reason"] = ("Held the accepted meter focus position in Manual for the single final "
                                    "exposure; the body already reported AF locked, so no additional autofocus "
                                    "search was performed. Optical focus is unverified.")
                return
            # Not locked passively (the A7 II reads "Unlock" once AF is released): drive ONE
            # bounded half-press to acquire focus. Fires no shutter and adds no exposure.
            af_lock["af_search"] = "driven_half_press"
            policy["reason"] = ("The accepted meter focus was not locked, so ONE bounded autofocus half-press was "
                                "driven to acquire focus before freezing into Manual for the single final "
                                "exposure. Optical focus is unverified.")
            driver = getattr(cam, "autofocus_half_press", None)
            if not callable(driver):
                af_lock.update(basis="confirmation_unavailable", af_search="unavailable_no_driver")
                return
            try:
                held = driver(FOCUS_AF_DRIVE_HOLD_S)
            except Exception as exc:
                # A failed AF drive must not fail the capture: proceed as with no confirmation.
                af_lock.update(basis="confirmation_unavailable", read_error=str(exc))
                return
            af_lock["indication_during_half_press"] = held
            indication = held
            if held not in FOCUS_LOCKED_INDICATIONS:
                # The A7 II reported "Focus Locked" on the post-release read even when the held
                # read said "Unlock"; consult it once more before deciding.
                try:
                    after = read_indication()
                except Exception:
                    after = None
                af_lock["indication_after_release"] = after
                if after is not None:
                    indication = after
            af_lock["indication"] = indication
            if indication in FOCUS_LOCKED_INDICATIONS:
                af_lock.update(confirmed=True, basis="confirmed")
            elif indication in FOCUS_NEGATIVE_INDICATIONS:
                af_lock.update(basis="negative_no_lock", recommend_refine=True)
            else:
                af_lock.update(basis="drove_af_no_lock", recommend_refine=True)
        finally:
            af_lock["wait_s"] = time.monotonic() - started

    @staticmethod
    def _restore_pre_request_exposure(cam, meter_before) -> tuple[str, str | None]:
        """Put ISO, aperture and shutter back as the request found them, when no final photograph
        was taken: reconnect first, write only what changed, verify the readback. Never raises;
        returns (status, error) with status not_needed | verified | failed."""
        from sonyremote import CameraError
        target = {key: meter_before[key] for key in ("iso", "aperture", "shutter")
                  if meter_before.get(key) is not None}
        try:
            # Reconnect (bounded, serial-proven; a no-op when connected) before the comparison read:
            # a dropped handle on a reconnectable body must not turn into "failed".
            getattr(cam, "reconnect", lambda: None)()
            current = cam.get_settings()
            changed = {key: value for key, value in target.items() if str(current.get(key)) != str(value)}
            if not changed:
                return "not_needed", None
            cam.set_exposure(**changed)
            readback = cam.get_settings()
            if any(str(readback.get(key)) != str(value) for key, value in changed.items()):
                raise CameraError("Exposure restoration before any final shutter was not confirmed")
            return "verified", None
        except Exception as exc:  # noqa: BLE001 — reported, never raised
            return "failed", str(exc)

    def _capture_with_meter_focus(self, cam, plan, settings, original_mode, focus_check=None, *,
                                  long_exposure=None):
        """Carry the accepted meter's focus position through one final exposure.

        Manual mode prevents another AF search; it does not establish optical
        sharpness or prove that the preceding autofocus found the subject.

        With `long_exposure` ({"live_shutter_labels": [...], "restore_exposure": {...}}) the final
        exposure goes through the separately gated long route (cam.capture_long_exposure) instead
        of the 1 s-capped capture(). Restoration then reconnects before it writes (the body
        re-enumerates slowly after a long exposure) and also returns ISO, aperture and shutter to
        their values before the request, under the same settled-or-deferred rule as focus.
        """
        from sonyremote import CameraError, CaptureOutcomeUncertain
        from sonyremote.camera import validate_sony_capture_shutter, validate_sony_long_exposure_shutter
        budget = (validate_sony_capture_shutter(settings.get("shutter")) if long_exposure is None
                  else validate_sony_long_exposure_shutter(settings.get("shutter"),
                                                           long_exposure["live_shutter_labels"]))
        policy = {"policy": "carry_meter_focus_position", "original_mode": original_mode,
                  "requested_mode": "Manual", "sharpness_verified": False,
                  "reason": "Hold the accepted meter focus position without another autofocus search; optical focus is unverified.",
                  "restore": "not_started", "setting_writes": []}
        plan["_final_focus_policy"] = policy
        if not isinstance(original_mode, str) or not original_mode.strip():
            raise CameraError("Original focus mode is unknown; no final shutter was issued.")
        capture_started = False
        focus_started = time.monotonic()
        trigger_started = None
        final = None
        failure = None
        try:
            # Confirm the meter frame's autofocus actually locked BEFORE freezing focus into
            # Manual. The body is still in its metering focus mode here; switching to Manual
            # below stops any further AF. This never blocks the shutter: a negative, a missing
            # node, an unavailable read, or the bounded deadline all proceed exactly as before
            # and record the truth. An AF lock is not proof of optical sharpness.
            if focus_check is not None:
                # Already settled before the final exposure was chosen (a depth-driven plan): reuse it
                # so autofocus is never driven twice and the recorded outcome is the one acted on.
                policy["af_focus_lock"] = focus_check["af_focus_lock"]
                if focus_check.get("reason"):
                    policy["reason"] = focus_check["reason"]
            else:
                self._confirm_focus_lock(cam, policy, original_mode)
            if cam.get_settings().get("focus_mode") != "Manual":
                policy["setting_writes"].append({"focus_mode": "Manual"})
                cam.set_setting("focus_mode", "Manual")
            policy["before_final_readback"] = cam.get_settings().get("focus_mode")
            if policy["before_final_readback"] != "Manual":
                raise CameraError("Manual focus lock was not confirmed; no final shutter was issued.")
            capture_started = True
            plan["_last_capture_path"] = None
            policy["focus_lock_s"] = time.monotonic() - focus_started
            import camera_clock
            plan["_final_host_time"] = camera_clock.host_now()   # local zone; None if unreadable
            trigger_started = time.monotonic()
            final = (cam.capture(download=True) if long_exposure is None
                     else cam.capture_long_exposure(settings.get("shutter")))
            plan["_final_host_time_after"] = camera_clock.host_now()
            return final
        except BaseException as exc:
            failure = exc
            attempt = getattr(cam, "last_capture_receipt", None)
            refused_before_trigger = (isinstance(exc, CameraError) and not isinstance(exc, CaptureOutcomeUncertain)
                                      and isinstance(attempt, dict) and attempt.get("trigger_commands") == 0)
            if (long_exposure is not None and isinstance(exc, Exception)
                    and not isinstance(exc, CaptureOutcomeUncertain)
                    and (not capture_started or refused_before_trigger)):
                # Nothing fired for the final photo: either the focus preparation failed before the
                # long route was called, or the long route refused before its trigger.
                exc.final_shutter_not_issued = True
            raise
        finally:
            restore_started = time.monotonic()
            if trigger_started is not None:
                policy["capture_call_s"] = restore_started - trigger_started
            receipt = getattr(cam, "last_capture_receipt", None)
            receipt = receipt if isinstance(receipt, dict) else {}
            transport_elapsed = receipt.get("timings", {}).get("trigger_and_download_s")
            minimum_settle = 2 * budget["configured_shutter_s"] + 5
            # The long route's restoration reconnects itself before writing, so a failed reconnect
            # inside the capture does not by itself defer it; being settled still does.
            reconnected = receipt.get("reconnect_ok") is True or long_exposure is not None
            known_settled = (isinstance(transport_elapsed, (int, float))
                             and math.isfinite(transport_elapsed)
                             and transport_elapsed >= minimum_settle
                             and reconnected)
            downloaded = final is not None and Path(final).is_file()
            # The long route records trigger_commands; zero means it refused before the shutter.
            nothing_fired = long_exposure is not None and receipt.get("trigger_commands") == 0
            safe_restore = (not capture_started or nothing_fired or
                            (downloaded and (receipt.get("reconnect_ok") is not False or long_exposure is not None))
                            or known_settled)
            policy.update(final_capture_attempted=capture_started,
                          restore_basis=("before_final_trigger" if not capture_started else
                                         "downloaded_file_and_reconnected" if downloaded and safe_restore else
                                         "bounded_short_capture_settle_and_reconnected" if known_settled else
                                         "physical_idle_unverified"),
                          minimum_uncertain_settle_s=minimum_settle,
                          observed_trigger_and_download_s=transport_elapsed)
            if safe_restore:
                try:
                    if long_exposure is not None:
                        cam.reconnect()  # restoration reconnects before it writes (bounded backoff)
                    if cam.get_settings().get("focus_mode") != original_mode:
                        policy["setting_writes"].append({"focus_mode": original_mode})
                        cam.set_setting("focus_mode", original_mode)
                    policy["restored_readback"] = cam.get_settings().get("focus_mode")
                    if policy["restored_readback"] != original_mode:
                        raise CameraError("Original focus mode restoration was not confirmed")
                    policy["restore"] = "verified"
                except Exception as exc:
                    policy.update(restore="failed", restore_error=str(exc))
                if long_exposure is not None:
                    target = dict(long_exposure.get("restore_exposure") or {})
                    try:
                        cam.reconnect()
                        if target:
                            policy["setting_writes"].append(dict(target))
                            cam.set_exposure(**target)
                        readback = cam.get_settings()
                        policy["exposure_restored_readback"] = {key: readback.get(key) for key in target}
                        if any(str(readback.get(key)) != str(value) for key, value in target.items()):
                            raise CameraError("Exposure restoration after the long exposure was not confirmed")
                        policy["exposure_restore"] = "verified"
                    except Exception as exc:
                        policy.update(exposure_restore="failed", exposure_restore_error=str(exc))
            else:
                policy["restore"] = "deferred_until_physical_idle_verified"
                if long_exposure is not None:
                    policy["exposure_restore"] = "deferred_until_physical_idle_verified"
            receipt["final_focus_policy"] = policy
            policy["focus_restore_s"] = time.monotonic() - restore_started
            cam.last_capture_receipt = receipt
            if failure is not None:
                failure.focus_policy = policy
                failure.transport_receipt = receipt
                if getattr(failure, "final_shutter_not_issued", False):
                    failure.meter_restore = _meter_restore_receipt(policy.get("exposure_restore"),
                                                                   policy.get("exposure_restore_error"))

    def _brain_shoot(self, cam, plan: dict, *, meter_only: bool = False) -> Shot | dict:
        import brain
        import camera_caps
        import rig_profile
        import live_verify
        import vision_banana
        import aperture_optimizer
        import constraint_cards
        from sonyremote import CameraError, CameraIdentityError, CaptureOutcomeUncertain, CapturePreconditionError
        from image_safety import open_image
        identity = cam.info()
        from sonyremote.camera import validate_sony_capture_shutter, sony_capture_deadline, SONY_ACTION_MAX_ENABLED_SHUTTER_S
        from sonyremote.camera import read_settings_checked
        settings_rereads = []

        def read_settings(purpose):
            # A placeholder read (f/0, ISO 0, 65535/65535) is never metered, planned from or restored.
            read, reread = read_settings_checked(cam, purpose=purpose)
            if reread:
                settings_rereads.append(reread)
            return read

        settings = read_settings("meter baseline")
        if settings_rereads and settings_rereads[-1].get("aperture_unreported"):
            # A manual/adapted lens reports f/0: the calibrated planner cannot use it. A ValueError
            # before the first shutter selects the aperture-independent JPEG feedback fallback.
            raise ValueError("the mounted lens reports no aperture (f/0); using JPEG exposure feedback")
        meter_before = dict(settings)
        meter_writes = []
        if "auto" in str(settings.get("iso", "")).lower():
            numeric = [int(x) for x in cam.list_options("iso") if str(x).isdigit() and int(x) >= 100]
            if not numeric:
                raise CameraError("No fixed ISO value is available for metering.")
            cam.set_exposure(iso=str(min(numeric)))
            plan["_exposure_written"] = True
            meter_writes.append({"iso": str(min(numeric))})
        try:
            validate_sony_capture_shutter(settings.get("shutter"))
        except CameraError:
            enabled = []
            for label in cam.list_options("shutter"):
                try:
                    candidate = validate_sony_capture_shutter(str(label))
                    enabled.append((candidate["configured_shutter_s"], str(label)))
                except CameraError:
                    continue
            if not enabled:
                raise CameraError("No live shutter permits a bounded metering exposure; no shutter was issued")
            _, label = max(enabled)
            cam.set_exposure(shutter=label)
            plan["_exposure_written"] = True
            meter_writes.append({"shutter": label})
        meter_after = read_settings("meter preflight readback") if meter_writes else settings
        meter_deadline = validate_sony_capture_shutter(meter_after.get("shutter"))
        meter_preflight = {"before": meter_before, "after": dict(meter_after),
                           "setting_writes": meter_writes, "capture_deadline": meter_deadline,
                           "policy": "provisional Sony USB shutter maximum 1 second; no autofocus or noise-reduction change"}
        _free_usb()
        plan["_camera_started"] = True
        timings = {"clock": "monotonic"}
        meter_started = time.monotonic()
        try:
            meter_frame = cam.capture(download=True)
            plan["_meter_exposures"] = 1
        except CapturePreconditionError as exc:
            # Refused by sonyremote before any trigger call, and this is the request's first
            # capture: nothing has fired. Every other capture error stays uncertain. Metering may
            # already have replaced Auto ISO or an unusable shutter; put them back and say so.
            plan["_camera_started"] = False
            exc.meter_restore = (_restore_exposure(cam, meter_before) if meter_writes
                                 else {"status": "unchanged"})
            raise
        timings["meter_capture_s"] = time.monotonic() - meter_started
        analysis_started = time.monotonic()
        if not meter_frame or not Path(meter_frame).is_file():
            raise CameraError("Meter capture returned no new JPEG.")
        plan["_last_capture_path"] = plan["_meter_capture_path"] = Path(meter_frame)
        meter_receipt = dict(getattr(cam, "last_capture_receipt", None) or {})
        if meter_receipt.get("reconnect_ok") is False:
            failure = CaptureOutcomeUncertain(
                "Meter JPEG was preserved but reconnect failed; no further shutter was issued.",
                capture_path=Path(meter_frame))
            failure.transport_receipt = meter_receipt   # diagnostics only
            raise failure
        # Full-resolution USB transfers are seconds apart, outside the motion model's
        # temporal range. One meter frame avoids a second shutter with no usable evidence.
        from capture_analysis import usb_motion_unavailable
        motion = usb_motion_unavailable("USB still-transfer intervals cannot provide a valid short motion sample")
        try:
            with open_image(meter_frame) as meter_image:
                meter_width, meter_height = meter_image.size
                exif = meter_image.getexif()
                exif_ifd = exif.get_ifd(34665) if 34665 in exif else exif
                lens_model = str(exif_ifd.get(42036, exif.get(42036, ""))).strip()
                lens_specification = exif_ifd.get(42034, exif.get(42034))
                max_aperture_value = exif_ifd.get(37381, exif.get(37381))
                # EXIF SubjectDistance: the lens's focus distance when the body writes it (neither
                # the A7 II nor the A7 III did on 202 local files, 2026-09-27; vision depth stands in).
                exif_subject_distance = exif_ifd.get(37382, exif.get(37382))
        except (OSError, ValueError) as exc:
            raise CameraError(f"Meter image validation failed; no further capture: {exc}") from exc
        # Resolve the mounted body+lens to the calibration path it actually earns: A7 II + its
        # profiled Tamron -> existing profile; A7 III + a measured JPEG-noise table -> that table's
        # ceiling; A7 III alone -> spec geometry only; anything else -> uncalibrated policy.
        rig = rig_profile.resolve_rig(
            identity.model, lens_model=lens_model, lens_specification=lens_specification,
            max_aperture_value=max_aperture_value)
        calibrated_lens = rig.lens_profiled
        settings = read_settings("meter exposure")
        iso = float(settings["iso"])
        f_number = float(str(settings["aperture"]).replace("f/", ""))
        shutter_s = camera_caps._shutter_seconds(settings["shutter"])
        live_caps = camera_caps.from_live_choices(identity.model,cam.list_options("iso"),
                                                  cam.list_options("aperture"),cam.list_options("shutter"))
        caps, transport_limits = camera_caps.constrain_capture_duration(
            live_caps, max_shutter_s=SONY_ACTION_MAX_ENABLED_SHUTTER_S)
        caps, mounted_lens_limits = camera_caps.constrain_mounted_lens(
            caps, settings["aperture"], lens_specification=lens_specification,
            max_aperture_value=max_aperture_value,
            verified_profile=(2.8, 22.0) if calibrated_lens else None)
        # A saturated or black meter frame only *bounds* the scene: its mean is pinned at the
        # encoder limit, so the exposure error it reports is far smaller than the real one.
        # Correct the metering exposure and read again before recommending, bounded to
        # remeter.MAX_REMETER_PASSES extra frames. The final photograph is still one shutter.
        import remeter
        meter_reliability = live_verify.meter_reliability(str(meter_frame))
        meter_passes = [{"pass": 0, "frame": Path(meter_frame).name, "iso": settings["iso"],
                         "aperture": settings["aperture"], "shutter": settings["shutter"],
                         "reliability": meter_reliability}]
        while (meter_reliability["status"] == "low"
               and len(meter_passes) <= remeter.MAX_REMETER_PASSES):
            correction = remeter.corrected_exposure(
                caps, iso, f_number, shutter_s,
                remeter.correction_stops(meter_reliability["mean_luma"],
                                         target=float(plan["target_brightness"])))
            if correction is None:
                meter_passes[-1]["stopped"] = "live ladders cannot correct the metering exposure further"
                break
            cam.set_exposure(iso=correction["iso"], shutter=correction["shutter"])
            plan["_exposure_written"] = True
            validate_sony_capture_shutter(read_settings("re-meter preflight readback").get("shutter"))
            _free_usb()
            retry_started = time.monotonic()
            retry_frame = cam.capture(download=True)
            plan["_meter_exposures"] = plan.get("_meter_exposures", 0) + 1
            timings[f"remeter_capture_{len(meter_passes)}_s"] = time.monotonic() - retry_started
            if not retry_frame or not Path(retry_frame).is_file():
                raise CameraError("Re-metering capture returned no new JPEG.")
            plan["_last_capture_path"] = plan["_meter_capture_path"] = Path(retry_frame)
            retry_receipt = dict(getattr(cam, "last_capture_receipt", None) or {})
            if retry_receipt.get("reconnect_ok") is False:
                failure = CaptureOutcomeUncertain(
                    "Re-metering JPEG was preserved but reconnect failed; no further shutter was issued.",
                    capture_path=Path(retry_frame))
                failure.transport_receipt = retry_receipt   # diagnostics only
                raise failure
            try:
                with open_image(retry_frame) as retry_image:
                    meter_width, meter_height = retry_image.size
                    # Focus may move between meter frames: plan from the accepted frame's distance.
                    retry_exif = retry_image.getexif()
                    retry_ifd = retry_exif.get_ifd(34665) if 34665 in retry_exif else retry_exif
                    exif_subject_distance = retry_ifd.get(37382, retry_exif.get(37382))
            except (OSError, ValueError) as exc:
                raise CameraError(f"Re-metering image validation failed; no further capture: {exc}") from exc
            meter_frame, meter_receipt = retry_frame, retry_receipt
            settings = read_settings("re-meter exposure")
            iso = float(settings["iso"])
            f_number = float(str(settings["aperture"]).replace("f/", ""))
            shutter_s = camera_caps._shutter_seconds(settings["shutter"])
            meter_reliability = live_verify.meter_reliability(str(meter_frame))
            meter_passes.append({"pass": len(meter_passes), "frame": Path(meter_frame).name,
                                 "iso": settings["iso"], "aperture": settings["aperture"],
                                 "shutter": settings["shutter"], "correction": correction,
                                 "reliability": meter_reliability})
        analysis = vision_banana.analyze(str(meter_frame))
        box = vision_banana.metering_region(analysis)
        region = None
        if box:
            region = (box[0]/meter_width,box[1]/meter_height,
                      box[2]/meter_width,box[3]/meter_height)
        scene_ev100, clipped = live_verify.meter_ev100(str(meter_frame),iso,f_number,shutter_s,region=region)
        scene_ev100 -= 2.2 * math.log2(float(plan["target_brightness"])/112.0)
        focal = live_verify.focal_from_exif(str(meter_frame), default=None)
        floor = motion.get("subject_motion_floor_s")
        intent_floor = motion_intent_floor_s(plan.get("motion_intent"))  # freeze 1/500, walk 1/250
        if intent_floor is not None:
            floor = min(floor or 1.0, intent_floor)
        # Subject recognition: the CLIP encoder reads the meter frame and, when confident, sets a
        # subject-specific shutter floor (e.g. a fast shutter for a bird). Deterministic mapping;
        # degrades to `unavailable` (no floor) when CLIP is absent. Folds into the binding floor so
        # physics honours it as a hard constraint (raise ISO before breaking it).
        import perception_client
        import subject_recognition
        # B04: the aperture is planned from the subject distance (the vision subject range, else the
        # EXIF focus distance) judged at the lens's EXIF focus plane, and the scene class; unknown
        # distance keeps today's aperture choice.
        exif_focus_distance = aperture_optimizer.subject_distance_from_exif(exif_subject_distance)
        subject_distance, distance_source = aperture_optimizer.resolve_subject_distance(
            exif_focus_distance, vision_banana.subject_distance_m(analysis))
        scene = vision_banana.scene_class(analysis)
        # Split runtime: one perception deadline covers both CLIP calls of this shot. The long
        # single-exposure branch makes no perception call; it sits inside so the ordinary
        # recommendation below keeps sharing the subject-recognition budget.
        with perception_client.shot_deadline():
            subject = subject_recognition.subject_evidence(
                str(meter_frame), focal_mm=focal, motion_intent=plan.get("motion_intent"))
            if subject.get("floor_s"):
                floor = min(floor or 1.0, subject["floor_s"])
            use_existing_profile = rig.calibration_status == "existing_profile"
            # Light trails / silky water / long exposure: ONE long exposure, only when the separately
            # gated long route is enabled. Otherwise the request stays on the 1 s-capped path and says so.
            long_plan = None
            stop_down_caps = None   # set only when an electronic stop-down is planned
            # A Fixed stack's metering (meter_only) brackets on the ordinary capped route; it never
            # plans or refuses a long exposure (its session cannot fire one).
            if plan.get("exposure_route") == "long_single_exposure" and not meter_only:
                if not long_exposure_enabled():
                    long_plan = {"status": "disabled", "subject": plan.get("long_exposure_subject"),
                                 "reason": LONG_EXPOSURE_DISABLED_NOTE}
                else:
                    import long_single_exposure
                    from dataclasses import replace
                    base_iso = getattr(getattr(rig, "sensor", None), "base_iso", None) or 100
                    iso_ceiling = 6400
                    ceiling_of = getattr(getattr(rig, "noise", None), "usable_iso_ceiling", None)
                    if rig.calibration_status == "measured_jpeg_noise" and callable(ceiling_of):
                        iso_ceiling = int(ceiling_of(base_iso=base_iso))
                    meter_note = None
                    if meter_reliability["status"] == "low":
                        meter_note = (f"The meter frame was unreliable ({meter_reliability['reason']}); its "
                                      "scene brightness is only a bound, so this exposure may be off.")
                    # The plan the confirmed aperture allows (today's behaviour for an unprofiled lens);
                    # also the fallback when a planned electronic stop-down does not read back.
                    def plan_confirmed_aperture():
                        return long_single_exposure.plan_long_single_exposure(
                            scene_ev100, replace(caps, shutters=live_caps.shutters, shutter_labels=live_caps.shutter_labels),
                            subject=plan.get("long_exposure_subject"), base_iso=base_iso, iso_ceiling=iso_ceiling,
                            stop_down_verified=calibrated_lens, meter_note=meter_note,
                            stop_down=stop_down_evidence)
                    # An unprofiled lens that controls its aperture electronically may be stopped down
                    # (to f/16 at most) for the subject's length; the readback before the shutter
                    # confirms the aperture, else the confirmed-aperture plan above is used.
                    stop_down_caps, stop_down_evidence = (None, None) if calibrated_lens else \
                        camera_caps.electronic_stop_down(live_caps, mounted_lens_limits, lens_model=lens_model)
                    try:
                        if stop_down_caps is not None:
                            long_plan, long_cards = long_single_exposure.plan_long_single_exposure(
                                scene_ev100, stop_down_caps, subject=plan.get("long_exposure_subject"),
                                base_iso=base_iso, iso_ceiling=iso_ceiling, stop_down_verified=False,
                                meter_note=meter_note, stop_down=stop_down_evidence)
                            if long_plan["status"] == "ok" and long_plan["aperture"] <= (
                                    mounted_lens_limits["current_confirmed_aperture"] * 1.01):
                                # No stop-down needed: the confirmed aperture's own plan, unchanged.
                                stop_down_caps = None
                                stop_down_evidence = {**stop_down_evidence, "status": "not_needed",
                                                      "reason": "the scene needs no aperture past the confirmed one"}
                                long_plan, long_cards = plan_confirmed_aperture()
                        else:
                            long_plan, long_cards = plan_confirmed_aperture()
                    except ValueError as exc:
                        # Nothing to plan (e.g. no live shutter above 1 s): a definite refusal before any
                        # final shutter, restored like an ND refusal, never "may have fired".
                        long_plan, long_cards = long_single_exposure.unplannable(str(exc))
                    if long_plan["status"] != "ok":
                        # Too bright even stopped down at base ISO: say so (ND filter); fire nothing more.
                        timings["meter_analysis_and_recommendation_s"] = time.monotonic() - analysis_started
                        # Metering fixed Auto ISO / capped the shutter; no final photo was taken, so put
                        # the body back as the request found it (reconnect first, then verify readback).
                        exposure_restore, exposure_restore_error = self._restore_pre_request_exposure(
                            cam, meter_before)
                        # Read before the gallery copy: a body that stopped answering must not turn this
                        # no-final-shutter refusal into an error (or leave an unindexed copy behind).
                        try:
                            refusal_settings = cam.get_settings()
                            refusal_readback = {"source": "after_refusal"}
                        except Exception as exc:  # noqa: BLE001 — reported, never raised
                            refusal_settings = dict(settings)
                            refusal_readback = {"source": "last_read_before_refusal", "warning": str(exc)}
                        return Shot(ok=False, image=_save_into_shots(Path(meter_frame)),
                                    summary=(f"No long exposure taken. {long_plan['reason']} "
                                             "The picture shown is the meter frame."),
                                    why=long_plan["reason"], mood=deepcopy(plan["mood"]), settings=refusal_settings,
                                    cards=long_plan["cards"], applied={"requested_settings": None},
                                    evidence={"source": "long_single_exposure_plan", "long_exposure": long_plan,
                                              "motion": motion, "subject": subject,
                                              "meter_reliability": meter_reliability, "meter_passes": meter_passes,
                                              "physical_exposure_count": len(meter_passes),
                                              "final_capture_attempted": False,
                                              "meter_transport_receipt": meter_receipt, "timings": timings,
                                              "meter_preflight": meter_preflight,
                                              "exposure_restore": exposure_restore,
                                              "settings_readback": refusal_readback,
                                              **({"exposure_restore_error": exposure_restore_error}
                                                 if exposure_restore_error else {}),
                                              "capture_files": [str(meter_frame)]})
            long_route = long_plan is not None and long_plan["status"] == "ok"
            if long_route:
                rec = long_single_exposure.as_recommendation(long_plan, long_cards)
                source = "long_single_exposure_plan"
            else:
                def recommend_for(focus_distance_m, taste_preference=None):
                    if use_existing_profile:
                        return brain.recommend_settings(str(meter_frame),scene_ev100,focal_mm=focal,caps=caps,
                            subject_motion_floor_s=floor,subject_distance_m=subject_distance,
                            scene_class=scene,distance_source=distance_source,fallback_aperture=f_number,
                            focus_distance_m=focus_distance_m,taste_preference=taste_preference)
                    return brain.recommend_live(str(meter_frame),scene_ev100,identity.model,caps,
                                                f_number,focal,subject_motion_floor_s=floor,rig=rig,
                                                subject_distance_m=subject_distance,scene_class=scene,
                                                distance_source=distance_source,
                                                focus_distance_m=focus_distance_m,
                                                taste_preference=taste_preference)
                rec = recommend_for(exif_focus_distance)
        # B04: settle the focus policy BEFORE the final exposure is applied. A depth-driven exposure
        # stands only when the meter frame's EXIF focus proof will hold for the final frame (the AF
        # lock held, or manual focus). If autofocus will search again, or its state is unconfirmed,
        # the taste/current exposure is solved without the proof and applied instead.
        focus_check = None
        # The long route plans its own exposure (no depth rule), so this never applies to it.
        if not long_route and aperture_optimizer.plan_is_driven(getattr(rec, "aperture_plan", None)):
            focus_check = {"reason": ""}
            self._confirm_focus_lock(cam, focus_check, meter_before.get("focus_mode"))
            if focus_check["af_focus_lock"].get("af_search") not in ("none_already_locked",
                                                                     "none_manual_focus"):
                # Only the deterministic solve is repeated: the FIRST taste/current preference is
                # reused, so a second retrieval can never move anything but depth.
                rec = recommend_for(None, taste_preference=getattr(rec, "taste_preference", None))
                rec.notes.append("The meter frame's focus will not hold for the final frame (autofocus "
                                 "searched again or its lock was unconfirmed), so depth of field did not "
                                 "choose the aperture.")
        if not long_route:  # the long route set its source with its plan above
            if use_existing_profile:
                source = "a7ii_physics_taste_unavailable" if brain.taste_fell_back(rec) else "a7ii_physics_retrieval"
            else:
                source = ("a7iii_measured_jpeg_noise_physics"
                          if rig.calibration_status == "measured_jpeg_noise"
                          else "live_capabilities_conservative_physics")
        recommended_f = float(str(rec.aperture_label).lower().removeprefix("f/"))
        if not long_route and camera_caps._shutter_seconds(rec.shutter_label) > SONY_ACTION_MAX_ENABLED_SHUTTER_S:
            raise CameraError("Recommended shutter exceeds enabled Sony transport policy; no final capture was issued.")
        allowed_apertures = stop_down_caps.apertures if stop_down_caps is not None else caps.apertures
        if not any(abs(recommended_f - value) < 1e-6 for value in allowed_apertures):
            raise CameraError("Recommended aperture is outside verified mounted-lens limits; no final capture was issued.")
        timings["meter_analysis_and_recommendation_s"] = time.monotonic() - analysis_started
        if meter_only:
            # A Fixed stack's baseline is the exposure this meter would give the final photograph.
            # No final capture here: the stack session brackets around it.
            return {"metered": {"iso": rec.iso_label, "aperture": rec.aperture_label,
                                "shutter": rec.shutter_label},
                    "usable_iso_range": list(brain.usable_iso_range(rig, existing_profile=use_existing_profile)),
                    "recommendation_source": source, "meter_passes": meter_passes,
                    "meter_reliability": meter_reliability, "meter_clipped_pct": clipped,
                    "meter_preflight": meter_preflight, "meter_transport_receipt": meter_receipt,
                    "physical_exposure_count": len(meter_passes), "timings": timings,
                    "cards": [c.to_dict() for c in rec.cards]}
        apply_started = time.monotonic()
        if not long_route:
            cam.set_exposure(iso=rec.iso_label,aperture=rec.aperture_label,shutter=rec.shutter_label)
            before_final_settings = read_settings("before final capture")
        else:
            # Nothing can fire until the readback proves the planned long shutter. A missing or
            # different value (e.g. a body that clamped it) is a clean refusal: restore the body
            # as the request found it and report that no final shutter was issued.
            from sonyremote.camera import SettingNotApplied, validate_sony_long_exposure_shutter
            try:
                original_focus = meter_before.get("focus_mode")
                if not isinstance(original_focus, str) or not original_focus.strip():
                    # Checked before anything is written: the focus restore target must be known.
                    raise CameraError("Original focus mode is unknown; no shutter was issued")
                # An electronic stop-down stands only when the body takes and reads back the planned
                # aperture. Otherwise drop to the confirmed aperture's plan (today's behaviour), apply
                # it, and require that aperture on readback too.
                def read_f_number(value):
                    try:
                        number = float(str(value).strip().lower().removeprefix("f/"))
                    except (TypeError, ValueError):
                        return None
                    return number if math.isfinite(number) and number > 0 else None

                def aperture_reads_back(value, planned_label):
                    read_f, planned_f = read_f_number(value), read_f_number(planned_label)
                    return read_f is not None and planned_f is not None and abs(read_f - planned_f) <= planned_f * 0.01

                aperture_refused = None
                try:
                    cam.set_exposure(iso=rec.iso_label,aperture=rec.aperture_label,shutter=rec.shutter_label)
                except SettingNotApplied as exc:
                    # Only a planned stop-down's aperture may fall back; anything else is a refusal.
                    if stop_down_caps is None or exc.key != "aperture":
                        raise
                    aperture_refused = exc
                if aperture_refused is None:
                    before_final_settings = read_settings("before final capture")
                if stop_down_caps is not None:
                    readback_aperture = (aperture_refused.final if aperture_refused is not None
                                         else before_final_settings.get("aperture"))
                    read_back = {"planned_aperture": str(rec.aperture_label), "readback_aperture": readback_aperture,
                                 **({"set_refused": str(aperture_refused)} if aperture_refused is not None else {})}
                    if aperture_refused is None and aperture_reads_back(readback_aperture, rec.aperture_label):
                        long_plan["stop_down_verified"] = True
                        long_plan["stop_down"] = {**stop_down_evidence, **read_back,
                                                  "status": "verified_by_readback"}
                        long_plan["notes"].append(
                            f"Stopped down to {rec.aperture_label} on an unprofiled electronic lens; the body "
                            "read that aperture back before the shutter.")
                    else:
                        stop_down_caps = None
                        stop_down_evidence = {**stop_down_evidence, **read_back, "status": "readback_mismatch",
                                              "reason": "the body did not take or read back the planned aperture, "
                                                        "so the confirmed aperture was used"}
                        planned_label = rec.aperture_label
                        long_plan, long_cards = plan_confirmed_aperture()
                        long_plan["notes"].append(
                            f"The lens did not confirm {planned_label} (read back {readback_aperture!r}), so the "
                            "exposure was planned at the confirmed aperture instead.")
                        if long_plan["status"] != "ok":
                            raise CameraError(f"The planned stop-down did not read back, and at the confirmed "
                                              f"aperture: {long_plan['reason']} No shutter was issued")
                        rec = long_single_exposure.as_recommendation(long_plan, long_cards)
                        cam.set_exposure(iso=rec.iso_label,aperture=rec.aperture_label,shutter=rec.shutter_label)
                        before_final_settings = read_settings("before final capture")
                        if not aperture_reads_back(before_final_settings.get("aperture"), rec.aperture_label):
                            raise CameraError(f"The body read back aperture {before_final_settings.get('aperture')!r}, "
                                              f"not the planned {rec.aperture_label!r}; no shutter was issued")
                if str(before_final_settings.get("shutter")) != str(rec.shutter_label):
                    raise CameraError(f"The body read back shutter {before_final_settings.get('shutter')!r}, "
                                      f"not the planned {rec.shutter_label!r}; no shutter was issued")
                validate_sony_long_exposure_shutter(before_final_settings.get("shutter"), live_caps.shutter_labels)
            except Exception as exc:
                refusal = exc if isinstance(exc, CameraError) else CameraError(
                    f"Applying the long exposure failed ({exc}); no shutter was issued")
                if isinstance(refusal, CaptureOutcomeUncertain):
                    raise
                # An identity refusal stays a CameraIdentityError, but it too restores (reconnect
                # first) and carries the restore state for the response.
                status, error = self._restore_pre_request_exposure(cam, meter_before)
                refusal.final_shutter_not_issued = True
                refusal.meter_restore = _meter_restore_receipt(status, error)
                refusal.focus_policy = {"exposure_restore": status,
                                        **({"exposure_restore_error": error} if error else {})}
                refusal.transport_receipt = None
                raise refusal from (None if refusal is exc else exc)
        _free_usb()
        timings["apply_and_readback_s"] = time.monotonic() - apply_started
        final_started = time.monotonic()
        import camera_clock
        long_exposure = None
        if long_route:
            long_exposure = {"live_shutter_labels": list(live_caps.shutter_labels),
                             "restore_exposure": {key: meter_before[key] for key in ("iso", "aperture", "shutter")
                                                  if meter_before.get(key) is not None}}
        final = self._capture_with_meter_focus(cam, plan, before_final_settings, meter_before.get("focus_mode"),
                                               focus_check=focus_check, long_exposure=long_exposure)
        before_final_settings["focus_mode"] = "Manual"
        timings["final_capture_with_focus_policy_s"] = time.monotonic() - final_started
        timings["final_capture_s"] = plan["_final_focus_policy"]["capture_call_s"]
        timings["final_focus_lock_s"] = plan["_final_focus_policy"]["focus_lock_s"]
        timings["final_focus_restore_s"] = plan["_final_focus_policy"]["focus_restore_s"]
        validation_started = time.monotonic()
        plan["_last_capture_path"] = Path(final) if final else None
        if not final or not Path(final).is_file():
            raise CameraError("Final capture has no verified new image file.")
        final_receipt = dict(getattr(cam, "last_capture_receipt", None) or {})
        try:
            with open_image(final) as final_image:
                final_width, final_height = final_image.size
                final_exif = final_image.getexif()
                final_ifd = final_exif.get_ifd(34665) if 34665 in final_exif else final_exif
                final_subject_distance = final_ifd.get(37382, final_exif.get(37382))
                final_clock_fields = camera_clock.safe_exif_time(final_exif)
            final_focal = live_verify.focal_from_exif(str(final), default=None)
        except (OSError, ValueError) as exc:
            raise CameraError(f"Final image validation failed; no further capture: {exc}") from exc
        # Sharpness is measured, not suggested: a relative verdict against the frame's own best
        # detail, on the subject box the meter already chose. Uncalibrated, and labelled so.
        box_in_final_pixels = None
        if region is not None:
            box_in_final_pixels = (round(region[0] * final_width), round(region[1] * final_height),
                                   round(region[2] * final_width), round(region[3] * final_height))
        try:
            import focus_verify  # OpenCV-backed; a host without it must still keep the photograph
            evidence_sharpness = focus_verify.sharpness_evidence(str(final), subject_box=box_in_final_pixels)
        except (ImportError, OSError, ValueError) as exc:
            evidence_sharpness = {"verdict": "undetermined", "reason": f"sharpness scoring failed: {exc}",
                                  "calibration": "uncalibrated_relative"}
        profile = camera_caps.model_profile(identity.model)
        noise_model = ("A7II_NOISE" if rig.calibration_status == "existing_profile"
                       else "MEASURED_JPEG_NOISE" if rig.calibration_status == "measured_jpeg_noise"
                       else None)
        profile.update({"detected_lens": lens_model or None,
                        "mounted_lens_limits": mounted_lens_limits,
                        "capture_transport_limits": transport_limits,
                        "lens_profile": "TAMRON_28_75_OPTICS" if use_existing_profile else None,
                        "lens_id": rig.lens_id,
                        "lens_identity_verified": calibrated_lens,
                        "calibration_status": rig.calibration_status,
                        "calibration_sources": list(rig.sources),
                        "noise_model": noise_model,
                        "metering": "existing_a7ii_physics" if use_existing_profile else "live_capabilities_geometry"})
        readback = {"after_final": "verified", "source": "after_final"}
        final_read_refused = None
        if long_route:
            # Restoration returned the body to its pre-request exposure, so the readback that
            # describes this photograph is the one taken just before the long exposure.
            actual = before_final_settings
            readback = {"after_final": "not_used", "source": "before_final",
                        "note": "Read back just before the long exposure; the body's exposure was then restored."}
        elif final_receipt.get("reconnect_ok") is False:
            actual = before_final_settings
            readback = {"after_final": "unavailable", "source": "before_final",
                        "warning": final_receipt.get("warning", "Capture reconnect failed")}
        else:
            try:
                actual = read_settings("after final capture")
            except Exception as exc:
                actual = before_final_settings
                readback = {"after_final": "unavailable", "source": "before_final",
                            "warning": f"Photo preserved; final settings readback failed: {exc}"}
                from sonyremote.camera import placeholder_refusal
                final_read_refused = placeholder_refusal(exc)
        timings["final_validation_and_readback_s"] = time.monotonic() - validation_started
        gallery_started = time.monotonic()
        image = _save_into_shots(Path(final))
        timings["gallery_save_s"] = time.monotonic() - gallery_started
        mood = deepcopy(plan["mood"])
        focus_policy = plan["_final_focus_policy"]
        meter_focus = ("manual focus for metering" if focus_policy["original_mode"] == "Manual"
                       else "native camera focus for metering")
        restoration = ("original focus mode restored" if focus_policy["restore"] == "verified"
                       else "focus restoration deferred" if focus_policy["restore"].startswith("deferred")
                       else "focus restoration unverified")
        mood["camera"] = (f"Used {meter_focus}; held that focus position in Manual for the final photo; {restoration}. "
                          f"Sharpness: {evidence_sharpness.get('verdict')} ({evidence_sharpness.get('reason')}).")
        mood.setdefault("settings", {})["focus"] = f"Meter focus position held in Manual for final capture; {restoration}; sharpness unverified"
        summary = "Photo captured. Review the result."
        corrections = len(meter_passes) - 1
        if meter_reliability["status"] == "low":
            summary = ("Photo captured from a low-reliability meter frame (" + meter_reliability["reason"]
                       + (f"), after {corrections} corrected metering pass(es). " if corrections
                          else "). ") + "Enable Refine for a critique-corrected exposure.")
        elif corrections:
            summary = (f"Photo captured after {corrections} corrected metering pass(es); the first "
                       "meter frame could not be read. Review the result.")
        af_lock = plan["_final_focus_policy"].get("af_focus_lock", {})
        if af_lock.get("recommend_refine"):
            summary += (" The camera did not confirm autofocus lock before the shutter"
                        f" (focus indication: {af_lock.get('indication')}), so the photo may be"
                        " soft. Enable Refine for a critique-corrected result.")
        # B04: depth is certified only against an EXIF-proven final focus plane (the final frame's,
        # or the meter frame's when focus provably held); otherwise it stays pending with a reason.
        # The assessment uses the FINAL frame's own EXIF focal (None when absent), never a fallback.
        depth_note = constraint_cards.settle_depth_after_focus(
            rec, af_search=af_lock.get("af_search"),
            final_distance_m=aperture_optimizer.subject_distance_from_exif(final_subject_distance),
            focal_mm=final_focal, sensor=rig.sensor,
            captured_f_number=_readback_f_number(actual.get("aperture")))
        if depth_note:
            summary += " " + depth_note
        durability = final_receipt.get("delivery_durability")
        if durability:
            # The photo is kept and returned (never a reshoot); the warning must not stay buried.
            summary += f" Warning: saving was not confirmed durable — {durability['reason']}."
        if long_route:
            # Refine is refused for this route (a reshoot would fire another long exposure), so
            # never recommend it; point at what the user can actually do.
            summary = (summary.replace("Enable Refine for a critique-corrected exposure.",
                                       "Review it and ask again for another long exposure if it is off.")
                       .replace("Enable Refine for a critique-corrected result.",
                                "Refocus by hand and ask again if it is soft."))
            summary = f"Long exposure: {long_plan['reason']} {summary}"
            if focus_policy.get("exposure_restore") != "verified":
                summary += " The camera's earlier exposure settings were not restored; check them before the next shot."
            if focus_policy.get("restore") != "verified":
                summary += (f" The camera's earlier focus mode ({focus_policy['original_mode']}) was not "
                            "restored; it may still be in Manual focus. Check it before the next shot.")
        elif long_plan is not None:
            summary += f" {long_plan['reason']}"
        # The camera's own clock, read from the delivered JPEG; reported, never changed.
        clock = camera_clock.safe_check(*final_clock_fields, plan.get("_final_host_time"),
                                        plan.get("_final_host_time_after"))
        clock_line = camera_clock.summary_line(clock)
        if clock_line:
            summary += " " + clock_line
        return Shot(ok=True,image=image,summary=summary,
                    why="; ".join(c.reason for c in rec.cards),mood=mood,settings=actual,
                    cards=[c.to_dict() for c in rec.cards],
                    applied={"requested_settings":{"iso":rec.iso_label,"aperture":rec.aperture_label,
                                                  "shutter":rec.shutter_label}},
                    evidence={"source":source,"motion":motion,"subject":subject,
                              "meter_clipped_pct":clipped,
                              "meter_reliability":meter_reliability,
                              "profile":profile, "meter_passes":meter_passes,
                              "aperture_plan":getattr(rec, "aperture_plan", None),
                              "exposure_reference":getattr(rec, "exposure_reference", None),
                              "physical_exposure_count":len(meter_passes) + 1,
                              "settings_readback":readback, "transport_receipt":final_receipt,
                              "meter_transport_receipt":meter_receipt, "timings":timings,
                              "meter_preflight":meter_preflight,
                              "final_focus_policy":plan["_final_focus_policy"],
                              "sharpness":evidence_sharpness,
                              "camera_clock":clock,
                              "capture_files":[str(meter_frame),str(final)],
                              **({"long_exposure":long_plan} if long_plan is not None else {}),
                              **({"settings_rereads":settings_rereads} if settings_rereads else {}),
                              **({"settings_read_refused":final_read_refused} if final_read_refused else {}),
                              **({"delivery_durability":durability} if durability else {})})

    def _autoexpose_shoot(self, cam, plan: dict, fallback: str = "") -> Shot:
        from sonyremote.autoexpose import auto_expose
        from sonyremote import CameraError, CameraIdentityError, CaptureOutcomeUncertain, PlaceholderSettingsError
        state = {"started": False, "path": None, "captures": [], "in_flight": False, "receipt_before_call": None,
                 "host_times": {}}
        fallback_started = time.monotonic()

        class TrackedCamera:
            def __getattr__(self, name):
                return getattr(cam, name)

            def capture(self, *args, **kwargs):
                state["started"] = True
                import camera_clock
                host_time = camera_clock.host_now()   # local zone; None if unreadable
                state["receipt_before_call"] = getattr(cam, "last_capture_receipt", None)
                state["in_flight"] = True
                capture_started = time.monotonic()
                result = cam.capture(*args, **kwargs)
                state["in_flight"] = False
                capture_s = time.monotonic() - capture_started
                state["path"] = Path(result) if result else None
                if result:
                    state["host_times"][str(Path(result))] = (host_time, camera_clock.host_now())
                receipt = dict(getattr(cam, "last_capture_receipt", None) or {})
                state["captures"].append({"capture_s": capture_s, "transport_receipt": receipt})
                if receipt.get("reconnect_ok") is False:
                    failure = CaptureOutcomeUncertain(
                        "Fallback JPEG was preserved but reconnect failed; no further shutter was issued.",
                        capture_path=state["path"])
                    failure.transport_receipt = receipt   # diagnostics only
                    raise failure
                return result

        floor = motion_intent_floor_s(plan.get("motion_intent")) or 1/125
        try:
            report = auto_expose(TrackedCamera(),target_mean=plan["target_brightness"],
                                 max_shutter_s=floor,max_iter=2)
            source = Path(report["final_image"]) if report.get("final_image") else None
            if source is None or not source.is_file():
                raise CameraError("Exposure loop returned no verified image.")
            note = report.get("note", "")
            if fallback:
                note = f"Full brain unavailable: {fallback}. {note}"
            settings = report.get("final_settings", {})
            import camera_clock
            clock = camera_clock.check_file(source, *state["host_times"].get(str(source), (None, None)))
            summary = "Photo captured; review exposure." if not report.get("ok") else "Photo captured."
            clock_line = camera_clock.summary_line(clock)
            if clock_line:
                summary += " " + clock_line
            return Shot(ok=report.get("ok",False),image=_save_into_shots(source),
                        summary=summary,
                        why=note,mood=plan["mood"],settings=settings,
                        cards=_readback_cards(settings,"JPEG feedback; calibrated lens/noise analysis unavailable"),
                        applied={"max_shutter_s":floor},
                        evidence={"source":"jpeg_feedback_fallback","fallback_reason":fallback,
                                  "exposure_converged":report.get("ok",False),"iterations":report.get("iterations",[]),
                                  "camera_clock":clock,
                                  "timings":{"clock":"monotonic", "capture_calls":state["captures"],
                                             "fallback_capture_analysis_and_save_s":time.monotonic()-fallback_started},
                                  **({"settings_read_refused":report["settings_read_refused"]}
                                     if report.get("settings_read_refused") else {})})
        except CaptureOutcomeUncertain:
            raise
        except (CameraIdentityError, PlaceholderSettingsError) as exc:
            exc.capture_path = exc.capture_path or state["path"]
            raise  # a refusal fired nothing; never reported as "may have fired"
        except Exception as exc:
            if state["started"]:
                failure = CaptureOutcomeUncertain(
                    f"Fallback processing stopped after capture; original file was preserved: {exc}",
                    capture_path=state["path"])
                # Diagnostics only. A capture call that raised: the receipt it recorded, if it
                # recorded one (never an earlier capture's). Otherwise the last completed capture's.
                if state["in_flight"]:
                    current = getattr(cam, "last_capture_receipt", None)
                    failure.transport_receipt = current if current is not state["receipt_before_call"] else None
                else:
                    failure.transport_receipt = (state["captures"][-1]["transport_receipt"]
                                                 if state["captures"] else None)
                raise failure from exc
            raise

    def record(self, intent: str | None, seconds: float = 10.0) -> Recording:
        from sonyremote import assert_recording_allowed
        assert_recording_allowed()
        plan = intent_brain.interpret(intent)
        with self._camera() as cam:
            if cam.info().model != "Sony ILCE-7M2 (Control)":
                raise NotImplementedError(
                    "Native movie control is not yet verified for this body. "
                    "A7 III recording requires a separate start/stop verification."
                )
            from sonyremote.camera import read_settings_checked
            wb_kelvin = WB_KELVIN.get(plan.get("white_balance"))
            wb = None
            if wb_kelvin is not None:
                pre_write, _ = read_settings_checked(cam, purpose="before first write")  # refuse before the write
                wb = apply_kelvin_white_balance(cam, plan["white_balance"], wb_kelvin, pre_write.get("white_balance"))
            before_settings, before_reread = read_settings_checked(cam, purpose="before recording")
            camera_serial = serial_evidence(cam)["camera_serial"]
            native = cam.record_native(seconds)
            readback = {"after_recording": "verified", "source": "after_recording"}
            after_read_refused = None
            try:
                settings, _ = read_settings_checked(cam, purpose="after recording")
            except Exception as exc:
                settings = before_settings
                readback = {"after_recording": "unavailable", "source": "before_recording",
                            "warning": f"Movie action completed; settings readback failed: {exc}"}
                from sonyremote.camera import placeholder_refusal
                after_read_refused = placeholder_refusal(exc)
        report = {
            **native,
            "ok": True,
            "mode": "native-movie",
            "samples": [],
            "final_settings": settings,
            "poster_frame": None,
            "settings_readback": readback,
            "retry_record": False,
            "native_action_completed": True,
            "camera_serial": camera_serial,
            **({"settings_reread": before_reread} if before_reread else {}),
            **({"settings_read_refused": after_read_refused} if after_read_refused else {}),
            **({"white_balance": wb} if wb else {}),
            "note": (
                "Native movie start/stop commands completed; verify the clip on the camera SD card. This USB path does not adjust exposure "
                "during recording; USB live view is disabled in the current transport."
            ),
        }
        receipt_dir = SHOTS_DIR / "recordings"
        receipt = receipt_dir / f"recording_{time.time_ns()}.json"
        temporary = receipt.with_suffix(".tmp")
        report.update({"receipt": str(receipt.relative_to(SHOTS_DIR)),
                       "clip_downloaded": False, "clip_playback_verified": False, "clip_verified": False,
                       "evidence": "native start/stop action receipt",
                       "receipt_persisted": True})
        try:
            receipt_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
            fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(fd, "w") as handle:
                json.dump(report, handle, indent=2, allow_nan=False)
                handle.write("\n")
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, receipt)
        except Exception as exc:
            # The physical movie action already completed. A local disk error must
            # not be represented as a failed movie that invites a second recording.
            report.update({"receipt": None, "receipt_persisted": False,
                           "receipt_persistence_warning":
                           f"Movie action completed; local receipt could not be saved: {exc}"})
        finally:
            try:
                temporary.unlink(missing_ok=True)
            except OSError as exc:
                report["receipt_cleanup_warning"] = str(exc)
        return Recording(
            ok=report.get("ok", False),
            image="",
            summary=("Native movie start/stop commands completed; verify the clip on the camera SD card."
                     + white_balance_note(wb or {})),
            why=report.get("note", ""),
            mood=plan["mood"],
            settings=report.get("final_settings", {}),
            recording=report,
        )


def get_engine():
    """Select the engine by CAMERA_ENGINE: 'real' (A7 II/USB), 'wifi' (modern Sony), else mock."""
    mode = os.getenv("CAMERA_ENGINE")
    if mode == "real":
        return RealEngine()
    if mode == "wifi":
        from wifi_engine import WiFiEngine
        return WiFiEngine()
    return MockEngine()
