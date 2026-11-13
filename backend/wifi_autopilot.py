"""All-WiFi autopilot for the A7 II — the complete loop over the Camera Remote API.

VERIFIED LIVE (2026-06-21): grab a liveview frame (no shutter) -> the PC depth model picks the
subject + the brain meters & recommends exposure -> set ISO/aperture/shutter and focus the subject
(setTouchAFPosition) -> capture. Patent-clean: the subject is chosen AUTOMATICALLY (saliency+depth),
never voice-named, then handed to the camera's native AF.

Requires: the A7 II running Smart Remote Control (WiFi AP, v3.10+) with this host joined to it, and
the PC depth server reachable (vision_banana). Mutually exclusive with the USB/PC-Remote path.

    backend/.venv-ml/bin/python backend/wifi_autopilot.py [--no-capture]
"""
from __future__ import annotations

import sys
import time
import shutil
import tempfile
import json
import os
from pathlib import Path

from image_safety import image_size, open_rgb

sys.path.insert(0, str(Path(__file__).resolve().parent))

import brain
import camera_caps
from intent import motion_intent_floor_s
import live_verify
import vision_banana
import wifi_a7ii

# Session cache of the LAST lens-reported focal length (from a shot's postview EXIF). getEvent has no
# lens info on the A7 II, so the first shot falls back to 50mm; once a real focal is read off a
# postview it's cached here and preferred over the 50mm fallback on subsequent shots in the session.
_LAST_FOCAL_MM_ACTUAL: float | None = None


def _capture_receipt(attempt: Path, **fields) -> None:
    """Persist shutter intent before action and exact download identity afterward."""
    destination = attempt / "capture-receipt.json"
    previous = json.loads(destination.read_text()) if destination.exists() else {}
    previous.update(fields)
    temporary = attempt / ".capture-receipt.tmp"
    with temporary.open("w") as handle:
        json.dump(previous, handle)
        handle.flush()
        os.fsync(handle.fileno())
    temporary.replace(destination)
    directory_fd = os.open(attempt, os.O_RDONLY)
    try:
        os.fsync(directory_fd)
    finally:
        os.close(directory_fd)


def _uncertain_capture(attempt: Path, detail: str):
    from sonyremote import CaptureOutcomeUncertain
    image = attempt / "postview.jpg"
    valid_image = None
    try:
        with open_rgb(image):
            valid_image = image
    except Exception:
        # Preserve the durable URL/intent receipt; invalid pixels are not proof.
        try:
            image.unlink(missing_ok=True)
        except OSError:
            pass  # Keep the typed uncertainty even if cleanup itself fails.
    receipt = attempt / "capture-receipt.json"
    error = CaptureOutcomeUncertain(
        f"{detail} Recovery receipt retained at {receipt}", capture_path=valid_image)
    error.receipt_path = str(receipt)
    return error


def _num(value) -> float | None:
    """Parse a camera value string to a number: '5.6'->5.6, '1/40'->0.025, 'f/8'->8, ISO '320'->320."""
    s = str(value).replace("f/", "").strip()
    if s in ("", "?", "None", "0"):
        return None
    try:
        if s.endswith('"'):                       # Sony long shutter, e.g. '30"' = 30s
            return float(s.rstrip('"'))
        return camera_caps._shutter_seconds(s) if "/" in s else float(s)
    except Exception:
        return None


def _available(cam, method: str) -> list:
    """getAvailableX -> [current, [candidates...]] -> the candidate list."""
    r = cam.call(method).get("result")
    return r[1] if r and len(r) > 1 and isinstance(r[1], list) else []


class IntentNotApplied(RuntimeError):
    """A verified exposure/white-balance/exposure-compensation write failed before the shutter,
    so the capture was refused (fail-closed). `.report` carries the recommended values, the setter
    that could not be verified, and its readback, so the caller can surface a truthful Shot(ok=False)
    instead of advertising settings that were never applied."""

    def __init__(self, message: str, *, report: dict):
        super().__init__(message)
        self.report = report


def _not_applied_report(field: str, setter_name: str, recommended: str, applied: dict, detail: str) -> dict:
    """Assemble the fail-closed report: what was asked for, what stuck, and where it broke."""
    return {
        "recommended": recommended,
        "failed_field": field,
        "failed_setter": setter_name,
        "applied_over_wifi": dict(applied),
        "readback": detail,
        "intent_applied": False,
    }


def _apply_snapped(cam, target, get_method: str, setter) -> dict:
    """Snap `target` to the camera's nearest advertised value and apply it via a verified setter.

    Returns {"applied": value} on success, or {"skipped": reason} when the body advertises no
    usable value (unsupported on this route). Propagates CameraSettingError from the verified
    setter unchanged so the shoot path can fail closed rather than silently drop the intent.
    """
    want = _num(target)
    parsed = [(v, _num(v)) for v in _available(cam, get_method)]
    parsed = [(v, n) for v, n in parsed if n is not None]
    if want is None:
        return {"skipped": f"no numeric target for {target!r}"}
    if not parsed:
        return {"skipped": f"{get_method} advertised no available values"}
    pick = min(parsed, key=lambda vn: abs(vn[1] - want))[0]
    setter(pick)  # verified: raises CameraSettingError if the readback disagrees
    return {"applied": pick}


def _snap_set(cam, target, get_method: str, setter):
    """Best-effort snap-and-set for callers that tolerate a skipped setting (returns the value set,
    or None on an unsupported/unverifiable write). The verified shoot path uses `_apply_snapped`
    directly so it can distinguish "unsupported" from "verification failed" and fail closed."""
    try:
        outcome = _apply_snapped(cam, target, get_method, setter)
    except wifi_a7ii.CameraSettingError:
        return None
    return outcome.get("applied")


def _white_balance_params(white_balance: str) -> tuple[str, int | None]:
    """Map an intent white-balance opinion to a (Sony mode, kelvin) pair for the verified setter."""
    if white_balance == "auto":
        return "Auto WB", None
    if white_balance == "warm":
        return "Color Temperature", 7500
    if white_balance == "cool":
        return "Color Temperature", 4000
    # An unrecognized opinion is treated as a named preset with no kelvin push.
    return white_balance, None


# Exposure-bias intent in EV: under = -1 EV, over = +1 EV. The index sent to the body depends on
# its advertised step unit (1 = 1/3 EV, 2 = 1/2 EV); `_EXPOSURE_BIAS_STEPS` is the 1/3-EV form.
_EXPOSURE_BIAS_EV = {"under": -1.0, "neutral": 0.0, "over": 1.0}
_EXPOSURE_BIAS_STEPS = {"under": -3, "neutral": 0, "over": 3}


def _bias_index(cam, exposure_bias: str) -> int | None:
    ev = _EXPOSURE_BIAS_EV.get(exposure_bias)
    if ev is None:
        return None
    step_fn = getattr(cam, "exposure_compensation_step", None)
    step = step_fn() if callable(step_fn) else 1
    per_ev = 2 if step == 2 else 3
    return int(round(ev * per_ev))


def _verified_white_balance(cam, mode: str, kelvin: int | None):
    """Apply white balance through the camera's typed verified setter when present, else a verified
    inline write for camera doubles/clients that only expose `.call`. Returns the getWhiteBalance
    readback; raises CameraSettingError if the write cannot be confirmed, SettingUnsupported if the
    body does not offer the mode."""
    setter = getattr(cam, "set_white_balance", None)
    if callable(setter):
        return setter(mode, kelvin)
    temp_enabled = mode == "Color Temperature"
    color_temp = int(kelvin) if (temp_enabled and kelvin) else -1
    response = cam.call("setWhiteBalance", [mode, temp_enabled, color_temp])
    if isinstance(response, dict) and response.get("error"):
        raise wifi_a7ii.CameraSettingError(f"setWhiteBalance rejected {mode!r}; not verified")
    readback = cam.call("getWhiteBalance")
    values = readback.get("result") if isinstance(readback, dict) else None
    if not isinstance(values, list) or not values:
        raise wifi_a7ii.CameraSettingError(f"getWhiteBalance did not confirm {mode!r}; not verified")
    got_mode, got_temp = wifi_a7ii._parse_white_balance(values)
    if str(got_mode) != mode or (temp_enabled and not wifi_a7ii._kelvin_matches(got_temp, color_temp)):
        raise wifi_a7ii.CameraSettingError(f"getWhiteBalance stayed at {values!r}; setWhiteBalance {mode!r} not verified")
    return values


def _verified_exposure_compensation(cam, index: int) -> int:
    """Apply exposure compensation through the camera's typed verified setter. Raises
    SettingUnsupported when the camera exposes no such setter (skippable)."""
    setter = getattr(cam, "set_exposure_compensation", None)
    if not callable(setter):
        raise wifi_a7ii.SettingUnsupported("camera exposes no exposure-compensation setter on this route")
    return setter(index)


def _postview_focus_distance_m(path: str):
    """The lens's EXIF focus distance on the postview, or None (neither body was seen writing it)."""
    import aperture_optimizer
    from image_safety import open_image
    try:
        with open_image(path) as image:
            exif = image.getexif()
            ifd = exif.get_ifd(34665) if 34665 in exif else exif
            return aperture_optimizer.subject_distance_from_exif(ifd.get(37382, exif.get(37382)))
    except Exception:  # noqa: BLE001 - postview EXIF is optional evidence
        return None


_ASSESS_RECOMMENDED = object()


def _reconcile_depth_after_focus(rec, *, focus_ran: bool, final_distance_m, focal_mm,
                                 captured_f_number=_ASSESS_RECOMMENDED) -> None:
    """Apply the USB engine's one depth rule (constraint_cards.settle_depth_after_focus) after
    focus_subject: the liveview carries no EXIF focus distance, so depth is certified only if the
    postview's EXIF proves the final focus plane; otherwise it stays pending with its reason.
    `focal_mm` is the focal measured for this shot, or None."""
    import constraint_cards
    import physics
    # shoot() passes the verified applied aperture (None when unverified -> pending); callers that
    # omit it assess the recommended aperture.
    kwargs = {} if captured_f_number is _ASSESS_RECOMMENDED else {"captured_f_number": captured_f_number}
    constraint_cards.settle_depth_after_focus(
        rec, af_search="driven_half_press" if focus_ran else None,
        final_distance_m=final_distance_m, focal_mm=focal_mm, sensor=physics.A7II, **kwargs)


def focus_subject(cam, analysis: dict, w: int, h: int):
    """Robustly drive native AF to the auto-chosen subject. Tries the saliency centroid, then the
    subject-box center + edges (smooth centers fail AF; rims/edges have contrast). For each:
    setTouchAFPosition + a half-press (engages the AF-assist lamp in dim light), then reads the live
    focusStatus. Returns (native_af_locked, applied_point_pct, status). A generic
    Focused status is not evidence that a requested coordinate was applied, and
    neither response certifies sharpness of the selected subject in the photo."""
    cx, cy = analysis["saliency_centroid"]
    candidates = [(cx, cy)]
    box = analysis.get("subject_box")
    if box:
        x, y, bw, bh = box
        candidates += [(x + bw / 2, y + bh / 2),       # box center
                       (x + bw / 2, y + bh * 0.15),    # top edge (often the rim)
                       (x + bw * 0.15, y + bh / 2)]    # left edge
    result = (False, None, "?")
    for px, py in candidates:
        xp = max(0.0, min(100.0, 100.0 * px / w))
        yp = max(0.0, min(100.0, 100.0 * py / h))
        af = cam.touch_af(xp, yp)
        status = "?"
        try:
            cam.half_press()                           # drive AF + assist lamp
            time.sleep(0.7)                            # let AF settle before reading status
            status = cam.focus_status()
        finally:
            cam.cancel_half_press()
        locked = af or status == "Focused"
        # touch_af returns False when its opt-in gate is off as well as when the
        # requested-point AF result is unconfirmed. Generic native AF can still
        # lock, but must not attach the requested coordinate to that result.
        applied_point = (round(xp, 1), round(yp, 1)) if af else None
        result = (locked, applied_point, status)
        if locked:
            return result                              # keep the touch-AF point set for the capture
        cam.cancel_touch_af()
    return result


def shoot(cam, capture: bool = True, frame: str | None = None,
          focal_mm: float | None = None, full_res_dir: str | None = None,
          target_brightness: float = 112.0, white_balance: str | None = None,
          motion_intent: str | None = None, work_dir: str | None = None,
          exposure_bias: str | None = None) -> dict:
    """Capture into a private, unique attempt directory; never reuse an old image.

    `frame` and `full_res_dir` remain accepted for callers, but global liveview
    destinations and newest-card transfers cannot establish capture identity.
    Successful CLI callers own the reported attempt directory; engine callers
    provide a temporary parent that is cleaned after the served image is copied.
    """
    attempt = Path(tempfile.mkdtemp(prefix="wifi-attempt-", dir=work_dir))
    try:
        report = _shoot_attempt(cam, capture=capture, frame=str(attempt / "liveview.jpg"),
                                focal_mm=focal_mm, target_brightness=target_brightness,
                                white_balance=white_balance, motion_intent=motion_intent,
                                exposure_bias=exposure_bias, attempt=attempt)
        report["output_contract"] = {
            "full_res_requested": full_res_dir is not None,
            # This autopilot returns the verified postview only; its own newest-card transfer cannot
            # establish capture identity, so it stays unavailable here. The engine layer
            # (backend/wifi_full_res.py) does the content-proved pull when CAMERA_WIFI_FULL_RES=1.
            "full_res_available": False,
            "full_res_identity_basis": "content+time",
            "frame_destination": "private_attempt_directory",
            "legacy_frame_destination_overridden": frame is not None,
        }
        return report
    except Exception as exc:
        if (attempt / "capture-receipt.json").exists():
            # Any error from shutter issuance onward is non-retryable. Keep the
            # only recovery evidence even when transfer or serialization failed.
            raise _uncertain_capture(attempt, str(exc)) from exc
        shutil.rmtree(attempt)
        raise


def _shoot_attempt(cam, *, capture: bool, frame: str, focal_mm: float | None,
                   target_brightness: float, white_balance: str | None,
                   motion_intent: str | None, exposure_bias: str | None, attempt: Path) -> dict:
    """Meter, apply exposure, and fetch this capture's postview.

    Focal length comes from camera events, a prior postview EXIF measurement, or
    the documented 50 mm fallback. Full-resolution card transfers are excluded
    until their identity can be matched to this shutter response.
    """
    global _LAST_FOCAL_MM_ACTUAL
    cam.begin_session()
    cam.set_focus_mode("AF-S")
    from capture_analysis import motion_between_files, motion_evidence_for_shot
    import math
    import time
    first = str(Path(frame).with_name(Path(frame).stem + "_motion.jpg"))
    first_time = time.monotonic()
    cam.grab_liveview_frame(save_path=first)
    time.sleep(0.12)
    second_time = time.monotonic()
    cam.grab_liveview_frame(save_path=frame)
    motion = motion_between_files(first,frame,second_time-first_time)
    w, h = image_size(frame)

    # REAL focal length over WiFi (no liveview EXIF) -> getEvent lens/zoom info, else 50mm default.
    # The A7 II's getEvent carries no lens info, so prefer the focal cached from a previous shot's
    # postview EXIF (the real one) over the 50mm fallback; 50mm stays only for the cold-start shot.
    # Only a focal measured NOW (getEvent) plans depth: the cached postview focal is from the last
    # shot (the user may have zoomed) and 50mm is a guess, so both feed the shutter floor alone.
    focal_is_current = focal_mm is not None
    if focal_mm is None:
        lens_focal = cam.focal_length_mm(default=0.0)
        focal_is_current = bool(lens_focal)
        focal_mm = lens_focal or _LAST_FOCAL_MM_ACTUAL or 50.0

    # the liveview frame is exposed at the camera's current settings -> meter from those
    iso = _num(cam.call("getIsoSpeedRate").get("result", ["800"])[0]) or 800.0
    f_number = _num(cam.call("getFNumber").get("result", ["5.6"])[0]) or 5.6
    shutter_s = _num(cam.call("getShutterSpeed").get("result", ["1/60"])[0]) or (1 / 60)

    analysis = vision_banana.analyze(frame)
    # SALIENCY: meter for the subject region, not the whole frame — else a dark room around a bright
    # subject (or vice-versa) drags the meter off the subject.
    box = vision_banana.metering_region(analysis)
    region = (box[0] / w, box[1] / h, box[2] / w, box[3] / h) if box else None
    # from_liveview=True: correct the 2MP preview's low-light over-brightness so we expose for the
    # STILL, not the gained-up preview (else the brain under-exposes — the field-noted ISO-6400 bug).
    ev100, _clip = live_verify.meter_ev100(frame, iso, f_number, shutter_s, region=region,
                                           from_liveview=True)
    ev100 -= 2.2 * math.log2(float(target_brightness)/112.0)
    motion_floor = motion.get("subject_motion_floor_s")
    intent_floor = motion_intent_floor_s(motion_intent)  # freeze 1/500, walk 1/250
    if intent_floor is not None:
        motion_floor = min(motion_floor or 1.0,intent_floor)
    rec = brain.recommend_settings(
        frame, ev100, focal_mm=focal_mm, focal_is_current=focal_is_current,
        subject_motion_floor_s=motion_floor,
        subject_distance_m=vision_banana.subject_distance_m(analysis),
        dof_depth_m=vision_banana.dof_depth_m(analysis),
        scene_class=vision_banana.scene_class(analysis), fallback_aperture=f_number)

    recommended = f"ISO {rec.iso_label}  f/{rec.aperture_label}  {rec.shutter_label}"

    # Focus FIRST, at the current (wider) aperture — Codex flagged that stopping down before AF
    # starves it of light in dim scenes (the main code-level cause of AF misses). THEN apply the
    # brain's exposure, then capture.
    af_locked, point, focus_state = (focus_subject(cam, analysis, w, h) if analysis else (None, None, None))

    # `applied` records ONLY verifiably-applied writes so the report never advertises a value the
    # camera did not confirm. Every write below is verified (getter after setter); ISO/aperture/
    # shutter are load-bearing, so an unsupported OR unverifiable write on any of them fails the
    # whole capture (fail-closed) rather than firing the shutter on stale settings.
    applied: dict = {}
    for field, target, get_method, setter_name, setter in (
        ("iso", rec.iso_label, "getAvailableIsoSpeedRate", "setIsoSpeedRate", cam.set_iso),
        ("f", rec.aperture_label, "getAvailableFNumber", "setFNumber", cam.set_aperture),
        ("shutter", rec.shutter_label, "getAvailableShutterSpeed", "setShutterSpeed", cam.set_shutter),
    ):
        try:
            outcome = _apply_snapped(cam, target, get_method, setter)
        except wifi_a7ii.CameraSettingError as exc:
            raise IntentNotApplied(str(exc), report=_not_applied_report(
                field, setter_name, recommended, applied, str(exc))) from exc
        if "skipped" in outcome:
            reason = (f"{setter_name} advertised no values ({outcome['skipped']}); the body does not "
                      "expose ISO/aperture/shutter in its current exposure mode. Set the mode dial to M.")
            raise IntentNotApplied(reason, report=_not_applied_report(
                field, setter_name, recommended, applied, reason))
        applied[field] = outcome["applied"]

    # White balance: skip entirely when the intent has no opinion (None). "auto"/"warm"/"cool" map
    # to a Sony mode (+ kelvin). An unsupported mode is a skip (capture proceeds, intent recorded as
    # not applied); a rejected or disagreeing write is fail-closed.
    wb_result = {"requested": white_balance, "applied": False, "verified": False}
    if white_balance:
        wb_mode, kelvin = _white_balance_params(white_balance)
        try:
            wb_readback = _verified_white_balance(cam, wb_mode, kelvin)
        except wifi_a7ii.SettingUnsupported as exc:
            wb_result.update(applied=False, verified=False, skipped=True, reason=str(exc))
            applied["white_balance"] = {"skipped": str(exc)}
        except wifi_a7ii.CameraSettingError as exc:
            # A secondary axis: the report says the look was not applied; the exposure is still verified.
            wb_result.update(applied=False, verified=False, skipped=True, reason=str(exc))
            applied["white_balance"] = {"skipped": str(exc)}
        else:
            wb_result.update(applied=True, verified=True, mode=wb_mode, kelvin=kelvin, readback=wb_readback)
            applied["white_balance"] = {"mode": wb_mode, "kelvin": kelvin}

    # Exposure compensation from the intent's exposure_bias. Unsupported on this route -> skip and
    # note the intent was not applied; a rejected or disagreeing write is fail-closed.
    exposure_comp_result = {"requested_bias": exposure_bias, "applied": False}
    if exposure_bias is not None:
        index = _bias_index(cam, exposure_bias)
        if index is None:
            exposure_comp_result.update(skipped=True, reason=f"unknown exposure_bias {exposure_bias!r}")
            applied["exposure_comp"] = {"skipped": exposure_comp_result["reason"]}
        elif index == 0:
            exposure_comp_result.update(skipped=True, reason="neutral bias needs no exposure-compensation write")
        else:
            try:
                applied_index = _verified_exposure_compensation(cam, index)
            except (wifi_a7ii.SettingUnsupported, wifi_a7ii.CameraSettingError) as exc:
                # Secondary axis: recorded as not applied; the verified exposure still captures.
                exposure_comp_result.update(skipped=True, reason=str(exc))
                applied["exposure_comp"] = {"skipped": str(exc)}
            else:
                exposure_comp_result.update(applied=True, index=applied_index)
                applied["exposure_comp"] = {"index": applied_index}

    if capture:
        _capture_receipt(attempt, capture_id=attempt.name, state="shutter_issued",
                         may_have_fired=True, retry_safe=False)
    postview = cam.take_picture() if capture else None
    if capture and (not isinstance(postview,list) or not postview or not isinstance(postview[0],str)):
        raise RuntimeError("Capture response has no image identity; refusing old-card fallback")
    if capture:
        _capture_receipt(attempt, state="download_pending", postview_url=postview[0])
    # REAL focal length: getEvent carries no lens info on the A7 II (verified live), so read it from the
    # postview EXIF — the lens stamps FocalLength there. The brain already metered at `focal_mm` (50mm
    # fallback) for this shot's floor; a caller/loop caches focal_mm_actual to use it for the next shot.
    focal_actual = None
    postview_focus_distance = None
    postview_path = None
    postview_dimensions = None
    if postview:
        destination = attempt / "postview.jpg"
        # A successful shutter response is not a successful download. The exact
        # returned URL must produce a decodable file for this attempt to succeed.
        cam.download(postview[0], str(destination))
        if not destination.is_file():
            raise RuntimeError("Current postview download produced no image file")
        with open_rgb(destination) as decoded:
            postview_dimensions = list(decoded.size)
        postview_path = str(destination)
        _capture_receipt(attempt, state="postview_verified", postview_path=postview_path,
                         image_dimensions=postview_dimensions)
        try:
            focal_actual = live_verify.focal_from_exif(str(destination), default=None)
            postview_focus_distance = _postview_focus_distance_m(str(destination))
        except Exception:  # noqa: BLE001 - postview is ephemeral; missing EXIF is non-fatal
            focal_actual = None
    # cache the lens-reported focal for the NEXT shot (getEvent has none on this body)
    if focal_actual:
        _LAST_FOCAL_MM_ACTUAL = focal_actual
    # focus_subject ran AFTER the depth plan: recheck or mark the plan unverified before the cards go out.
    _reconcile_depth_after_focus(rec, focus_ran=analysis is not None,
                                 final_distance_m=postview_focus_distance,
                                 # only the captured frame's own focal (postview EXIF); never the
                                 # pre-shot or cached focal. Unreadable -> the assessment stays pending.
                                 focal_mm=focal_actual,
                                 captured_f_number=_num(applied.get("f")))
    return {
        "attempt_dir":str(attempt), "capture_id":attempt.name,
        "postview_path":postview_path, "postview_downloaded":postview_path is not None,
        "image_kind":"postview" if postview_path else None,
        "image_dimensions":postview_dimensions,
        # Default: the postview is the published image. When CAMERA_WIFI_FULL_RES=1 the engine's
        # wifi_full_res pull can upgrade this to verified_by_content[_and_time] or, on a failed
        # identity match, unverified_postview_kept — never a bare newest-card fallback.
        "full_res_status":"postview_only",
        "cards":[card.to_dict() for card in rec.cards],
        "motion":motion_evidence_for_shot(motion),"motion_analysis":motion,"white_balance":wb_result,"exposure_comp":exposure_comp_result,
        "target_brightness":target_brightness,
        "scene_ev100": round(ev100, 2),
        "focal_mm": round(focal_mm, 1),               # used for the shutter floor (getEvent / 50mm fallback)
        "focal_mm_actual": focal_actual,              # lens-reported, from the postview EXIF (the real one)
        "scene_class": vision_banana.scene_class(analysis),
        "subject_distance_m": vision_banana.subject_distance_m(analysis),
        "recommended": recommended,                   # what the brain asked for (distinct from applied)
        "applied_over_wifi": applied,                 # only verifiably-applied writes / explicit skips
        "af_locked": af_locked,
        "focus_point_pct": point,
        "focus_status": focus_state,
        "focus_result_source": ("touch_af_result" if point is not None else
                                "generic_native_af_status" if af_locked else "not_verified"),
        "focus_coordinate_applied": point is not None,
        "chosen_subject_focus_verified": False,
        "postview": postview,
        "full_res": [],
    }


if __name__ == "__main__":
    camera = wifi_a7ii.connect()
    if not camera:
        print("No camera. Launch Smart Remote Control on the A7 II + join its WiFi AP first.")
        raise SystemExit(1)
    import json
    # Legacy flag is explicit: a newest-card file is not capture identity proof.
    full_res_dir = None
    if "--full-res" in sys.argv:
        print("--full-res is unavailable without current-capture identity matching; returning the verified postview only.", file=sys.stderr)
        idx = sys.argv.index("--full-res")
        full_res_dir = (sys.argv[idx + 1] if idx + 1 < len(sys.argv)
                        and not sys.argv[idx + 1].startswith("--") else "/tmp/wifi_fullres")
    print(json.dumps(shoot(camera, capture="--no-capture" not in sys.argv,
                           full_res_dir=full_res_dir), indent=2, default=str))
