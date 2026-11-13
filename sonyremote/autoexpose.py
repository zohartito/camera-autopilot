"""Closed-loop auto-exposure for the Sony A7 II.

Automates the manual loop proven by hand: take a frame, measure its brightness, work out how
many stops off it is, change the camera, and re-shoot until the mean luminance lands near the
target. Mode-aware: in M it moves the shutter (then ISO at the limits); in A/P/S it nudges
exposure compensation. Uses a damped gain so it converges without oscillating.
"""

from __future__ import annotations

from pathlib import Path
import math

from backend.image_safety import open_rgb

from .camera import (
    Camera,
    CameraError,
    PlaceholderSettingsError,
    RecordingStopUncertain,
    placeholder_refusal,
    assert_recording_allowed,
    bounded_record_seconds,
    quarantine_recording_stop,
    read_settings_checked,
)

# Roughly how many discrete option steps equal one stop on this body (1/3-stop increments).
_STEPS_PER_STOP = 3
# Feedforward gain from pixel-log-error to exposure stops. Because JPEG is gamma-encoded
# (pixel ~ exposure^0.45), the exact conversion is ~2.2; we use a conservative 1.4 so the
# closed loop converges in a couple of shots without overshooting.
_GAIN = 1.4


def measure_jpeg(path: Path | str, sample: int = 220) -> dict:
    """Mean luminance (0-255), clipped-highlight %, crushed-shadow %, and RGB means of a JPEG."""
    rgb = open_rgb(path)
    rgb.thumbnail((sample, sample))
    data = rgb.tobytes()
    reds, greens, blues = data[0::3], data[1::3], data[2::3]
    count = len(reds)
    grays = [(299 * r + 587 * g + 114 * b) // 1000 for r, g, b in zip(reds, greens, blues)]
    return {
        "mean": sum(grays) / count,
        "blown_pct": 100 * sum(1 for v in grays if v >= 250) / count,
        "black_pct": 100 * sum(1 for v in grays if v <= 5) / count,
        "rgb": [round(sum(reds) / count, 1), round(sum(greens) / count, 1), round(sum(blues) / count, 1)],
    }


def _to_float(label: str) -> float | None:
    """Parse an exposure-comp label like '+2', '-1.7', '0' to a float; None if not numeric."""
    try:
        return float(str(label).replace("+", ""))
    except ValueError:
        return None


def _shift_in_list(options: list[str], current: str, steps: int, brighter_toward_start: bool) -> str | None:
    """Move `steps` option-notches brighter within an ordered settings list.

    Shutter lists run slow->fast (brighter is toward index 0); ISO lists run low->high
    (brighter is toward the end). Non-numeric entries (Auto ISO, Bulb) are excluded.
    """
    usable = [o for o in options if _exposure_value(o) is not None]
    usable.sort(key=_exposure_value, reverse=brighter_toward_start)
    if current not in usable:
        return None
    index = usable.index(current)
    delta = -steps if brighter_toward_start else steps
    new_index = max(0, min(len(usable) - 1, index + delta))
    return usable[new_index]


def _exposure_value(label: str) -> float | None:
    """Positive finite shutter/ISO value; reject Sony's uninitialized sentinel."""
    if str(label) == "65535/65535":
        return None
    try:
        parts = str(label).replace("f/", "").split("/")
        value = float(parts[0]) / float(parts[1]) if len(parts) == 2 else float(parts[0])
        return value if math.isfinite(value) and value > 0 else None
    except (ValueError, ZeroDivisionError):
        return None


def auto_expose(
    camera: Camera,
    target_mean: float = 112.0,
    tolerance: float = 14.0,
    max_iter: int = 5,
    max_shutter_s: float | None = None,
) -> dict:
    """Drive the camera to a well-exposed frame. Returns a report dict with every iteration,
    the final settings, and the path to the final image."""
    target_mean = max(1.0, float(target_mean))  # guard log2 domain for silly inputs
    if max_iter < 1:
        raise ValueError("max_iter must be at least one")
    if max_shutter_s is not None:
        max_shutter_s = float(max_shutter_s)
        if not math.isfinite(max_shutter_s) or max_shutter_s <= 0:
            raise ValueError("max_shutter_s must be positive and finite")
    mode = camera.shooting_mode()
    if max_shutter_s is not None and not str(mode).upper().startswith("M"):
        raise CameraError("A shutter motion limit requires M mode with fixed ISO; change the physical mode dial")
    history: list[dict] = []
    final_path: Path | None = None
    rereads: list[dict] = []  # placeholder reads re-read before this iteration's record

    def read_settings(purpose: str) -> dict:
        # Never plan from or record a placeholder read (f/0, ISO 0, 65535/65535).
        settings, reread = read_settings_checked(camera, purpose=purpose)
        if reread:
            rereads.append(reread)
        return settings

    if str(mode).upper().startswith("M"):
        settings = read_settings("auto-expose starting settings")
        # Auto ISO counteracts manual shutter corrections, preventing convergence.
        if "auto" in str(settings.get("iso", "")).lower():
            fixed = [value for value in camera.list_options("iso") if _exposure_value(value) is not None]
            if not fixed:
                raise CameraError("Manual exposure needs a fixed ISO; the body exposes no numeric ISO choices")
            camera.set_setting("iso", min(fixed, key=_exposure_value))
        if max_shutter_s is not None:
            choices = [value for value in camera.list_options("shutter")
                       if (seconds := _exposure_value(value)) is not None and seconds <= max_shutter_s]
            if not choices:
                raise CameraError("No shutter choice satisfies the requested motion limit")
            current = _exposure_value(str(settings.get("shutter")))
            if current is None or current > max_shutter_s:
                camera.set_setting("shutter", max(choices, key=_exposure_value))

    for iteration in range(1, max_iter + 1):
        try:
            before_capture = read_settings("before capture")
            captured = camera.capture(download=True)  # a wrapper may refuse before its shutter
        except PlaceholderSettingsError as exc:
            if not history:
                raise  # nothing captured yet: a clean refusal before the first shutter
            result = _result(False, mode, history, camera, final_path,
                             f"Photograph retained; the next shutter was refused. {exc}")
            result["settings_read_refused"] = exc.as_dict()
            return result
        final_path = captured
        post_capture_refusal = None
        m = measure_jpeg(final_path)
        receipt = getattr(camera, "last_capture_receipt", None)
        warning = receipt.get("warning") if isinstance(receipt, dict) and receipt.get("reconnect_ok") is False else None
        if isinstance(receipt, dict) and receipt.get("reconnect_ok") is False:
            warning = warning or "Camera reconnect failed after capture"
            photographed_settings = before_capture
        else:
            try:
                photographed_settings = read_settings("after capture")
            except CameraError as exc:
                warning = f"Post-capture settings unavailable: {exc}"
                photographed_settings = before_capture
                if isinstance(exc, PlaceholderSettingsError):
                    post_capture_refusal = exc.as_dict()
        history.append({"iter": iteration, "settings": photographed_settings,
                        "settings_source": "pre_capture_readback" if warning else "post_capture_readback",
                        "mean": round(m["mean"], 1), "blown_pct": round(m["blown_pct"], 1)})
        if rereads:
            history[-1]["settings_rereads"] = list(rereads)
            rereads.clear()
        if warning:
            result = _result(abs(m["mean"] - target_mean) <= tolerance, mode, history, camera, final_path,
                             f"Photograph retained; exposure loop stopped without another shutter. {warning}")
            result["capture_warning"] = warning
            result["capture_receipt"] = receipt
            if post_capture_refusal:
                result["settings_read_refused"] = post_capture_refusal  # wake and retry, not a transport fault
            return result

        if abs(m["mean"] - target_mean) <= tolerance:
            return _result(True, mode, history, camera, final_path,
                           f"Converged in {iteration} shot(s).")
        if iteration == max_iter:
            break  # Do not change settings after the final photographed exposure.

        # How many stops of light to add (+) or remove (-). Damped to avoid overshoot.
        ratio = target_mean / max(m["mean"], 1.0)
        stops = max(-5.0, min(5.0, _GAIN * math.log2(ratio)))  # cap a single move at 5 stops
        steps = round(stops * _STEPS_PER_STOP)
        if steps == 0:
            return _result(False, mode, history, camera, final_path, "No finer exposure step; outside tolerance.")

        try:
            applied = _apply_correction(camera, mode, steps, stops, max_shutter_s=max_shutter_s)
        except CameraError as exc:
            result = _result(False, mode, history, camera, final_path,
                             f"Couldn't adjust exposure ({exc}). In {mode} mode this lever may be "
                             f"locked — switch the mode dial to M for full control.")
            if (refusal := placeholder_refusal(exc)):
                result["settings_read_refused"] = refusal
            return result
        history[-1]["correction"] = applied

    return _result(False, mode, history, camera, final_path,
                   f"Did not converge in {max_iter} shots; last mean {history[-1]['mean']}.")


def record_auto_exposed_movie(
    camera: Camera,
    target_mean: float = 112.0,
    seconds: float = 10.0,
    sample_interval: float = 1.0,
    tolerance: float = 14.0,
    max_samples: int | None = None,
    sleep=None,
) -> dict:
    """Record native movie while nudging exposure from live-view samples.

    The real video is recorded by the camera to its SD card. Live-view frames are only sampled as
    a feedback signal and poster frame; they are not the recorded movie.
    """
    import math
    import time

    target_mean = max(1.0, float(target_mean))
    seconds, _max_seconds = bounded_record_seconds(seconds)
    sample_interval = max(0.0, float(sample_interval))
    # Work is capped independently of caller input; a zero interval cannot create a hot loop.
    # ``max_samples`` remains accepted for backward-compatible calls, but cannot relax this cap.
    sample_budget = min(60, max(4, round(seconds / max(sample_interval, 0.5))))

    assert_recording_allowed()
    if getattr(camera, "_recording_quarantined", False):
        raise RecordingStopUncertain("recording_stop_uncertain: this camera instance is quarantined")
    mode = camera.shooting_mode()
    samples: list[dict] = []
    poster_frame: Path | None = None
    started = time.monotonic()
    sleeper = sleep or time.sleep

    deadline = time.monotonic() + seconds
    started_movie = False
    try:
        camera.start_movie()
        started_movie = True
        for sample_index in range(1, sample_budget + 1):
            if time.monotonic() >= deadline:
                break
            try:
                poster_frame = camera.liveview_frame()
            except CameraError as exc:
                samples.append({
                    "sample": sample_index,
                    "settings": _observed_settings(camera),
                    "error": str(exc),
                })
                break
            measured = measure_jpeg(poster_frame)
            sample = {
                "sample": sample_index,
                "settings": _observed_settings(camera),
                "mean": round(measured["mean"], 1),
                "blown_pct": round(measured["blown_pct"], 1),
                "frame": str(poster_frame),
            }
            samples.append(sample)

            if abs(measured["mean"] - target_mean) > tolerance:
                ratio = target_mean / max(measured["mean"], 1.0)
                stops = max(-5.0, min(5.0, _GAIN * math.log2(ratio)))
                steps = round(stops * _STEPS_PER_STOP)
                if steps:
                    try:
                        sample["correction"] = _apply_correction(camera, mode, steps, stops)
                    except CameraError as exc:
                        sample["correction_error"] = str(exc)
                        break

            if sample_interval:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    break
                sleeper(min(sample_interval, remaining))
                if time.monotonic() >= deadline:
                    break
    finally:
        if started_movie:
            _stop_movie_or_quarantine(camera)

    final = samples[-1] if samples else {}
    ok = bool(samples) and "error" not in final and abs(float(final.get("mean", 0.0)) - target_mean) <= tolerance
    note = "Native movie recorded while exposure was adjusted from live-view samples."
    if final.get("error"):
        note = "Native movie recorded, but USB live view was unavailable during recording."
    elif not ok:
        note = "Native movie recorded, but exposure did not settle within tolerance."
    return {
        "ok": ok,
        "mode": mode,
        "samples": samples,
        "final_settings": _observed_settings(camera),
        "final_mean": final.get("mean", 0.0),
        "final_blown_pct": final.get("blown_pct", 0.0),
        "poster_frame": str(poster_frame) if poster_frame else None,
        "recorded_seconds": round(time.monotonic() - started, 1),
        "saved_to": "the camera's SD card (native movie)",
        "note": note,
    }


def _observed_settings(camera: Camera) -> dict:
    """A movie-sample record: checked settings, or the refusal instead of placeholder values.
    Never raises for a placeholder, so the recording's stop path is never skipped."""
    try:
        return read_settings_checked(camera, purpose="movie sample settings")[0]
    except PlaceholderSettingsError as exc:
        return {"placeholder_refused": exc.as_dict()}


def _stop_movie_or_quarantine(camera: Camera) -> None:
    """Stop exactly once unless the transport explicitly proves a retry cannot toggle recording."""
    attempts = 2 if getattr(camera, "stop_movie_retry_safe", False) else 1
    failure: Exception | None = None
    for _ in range(attempts):
        try:
            camera.stop_movie()
            return
        except Exception as exc:
            failure = exc
    try:
        setattr(camera, "_recording_quarantined", True)
    except Exception:
        pass
    try:
        quarantine_recording_stop(str(failure or "movie stop failed"))
    except RecordingStopUncertain as sentinel_exc:
        raise sentinel_exc from failure
    raise RecordingStopUncertain(
        "recording_stop_uncertain: stop could not be confirmed; camera is quarantined"
    ) from failure


def _apply_correction(camera: Camera, mode: str, steps: int, stops: float,
                      max_shutter_s: float | None = None) -> str:
    """Pick the right lever for the current mode and apply the brightness correction."""
    settings, _ = read_settings_checked(camera, purpose="auto-expose correction")
    if str(mode).upper().startswith("M"):
        # Manual: move shutter; if it can't move far enough, also nudge ISO.
        shutter_opts = camera.list_options("shutter")
        if max_shutter_s is not None:
            shutter_opts = [value for value in shutter_opts
                           if (seconds := _exposure_value(value)) is not None and seconds <= max_shutter_s]
        new_shutter = _shift_in_list(shutter_opts, str(settings.get("shutter")), steps, brighter_toward_start=True)
        if new_shutter and new_shutter != settings.get("shutter"):
            camera.set_setting("shutter", new_shutter)
            return f"shutter -> {new_shutter} ({stops:+.1f} stop)"
        iso_opts = camera.list_options("iso")
        new_iso = _shift_in_list(iso_opts, str(settings.get("iso")), steps, brighter_toward_start=False)
        if new_iso and new_iso != settings.get("iso"):
            camera.set_setting("iso", new_iso)
            return f"iso -> {new_iso} ({stops:+.1f} stop)"
        raise CameraError("shutter and ISO already at their limits")
    # A/P/S: use exposure compensation.
    ev_opts = camera.list_options("exposure_comp")
    current_ev = _to_float(str(settings.get("exposure_comp"))) or 0.0
    wanted_ev = current_ev + stops
    pick = min(ev_opts, key=lambda o: abs((_to_float(o) if _to_float(o) is not None else 99) - wanted_ev))
    camera.set_setting("exposure_comp", pick)
    return f"exposure_comp -> {pick} ({stops:+.1f} stop)"


def _result(ok: bool, mode: str, history: list[dict], camera: Camera, path: Path | None, note: str) -> dict:
    final = measure_jpeg(path) if path else {}
    return {
        "ok": ok,
        "mode": mode,
        "iterations": history,
        "final_settings": history[-1]["settings"] if history else camera.get_settings(),
        "final_mean": round(final.get("mean", 0), 1),
        "final_blown_pct": round(final.get("blown_pct", 0), 1),
        "final_image": str(path) if path else None,
        "note": note,
    }
