"""Body-shutter-driven handheld mode (Arsenal row A06).

The user presses the camera's own shutter; the host NEVER issues a shutter command. Each frame
the camera pushes to the host is downloaded, metered, and the exposure for the *next* frame is
applied — with a durable per-frame receipt and human-readable explanations. The session is
bounded (max frames / max duration), cancellable, and restores the camera's original settings
when it stops. It never calls ``camera.capture()`` and never opens more than one camera.

The metering itself is injected: ``recommend_fn(jpeg_path, current_settings, lens_exif)`` returns
a recommendation dict (``iso_label`` / ``aperture_label`` / ``shutter_label``, optional
``kelvin``, a ``why`` list and a ``source``). Keeping the taste/vision brain out of this module
keeps the loop deterministic and testable with no camera and no model.
"""
from __future__ import annotations

import copy
import hashlib
import json
import os
import threading
import time
from pathlib import Path
from typing import Callable

# Exposure keys we snapshot and later restore, in the order set_exposure accepts them.
_EXPOSURE_KEYS = ("iso", "aperture", "shutter", "white_balance", "exposure_comp")

JOURNAL_NAME = "handheld_journal.jsonl"


def _identity_refused(exc: BaseException) -> bool:
    """sonyremote.CameraIdentityError, duck-typed so this module stays camera-agnostic."""
    return getattr(exc, "identity_refused", False) is True


def _pick_jpeg(files: list[Path]) -> Path | None:
    """The JPEG we can meter from a pushed batch (a RAW+JPEG pair yields both files)."""
    jpegs = [Path(path) for path in files if Path(path).suffix.lower() in {".jpg", ".jpeg"}]
    return jpegs[-1] if jpegs else None


def _exif_str(value) -> str | None:
    if value is None:
        return None
    text = str(value).strip().replace("\x00", "")
    return text or None


def _exif_number(value) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        if isinstance(value, tuple):  # PIL rationals can arrive as (num, den)
            value = value[0] / value[1] if len(value) == 2 and value[1] else value[0]
        number = float(value)
    except (TypeError, ValueError, ZeroDivisionError):
        return None
    return number if number == number else None  # drop NaN


def _frame_exif(jpeg_path: Path) -> dict:
    """Read ISO / aperture / shutter / lens from EXIF via PIL; missing fields become None."""
    info = {"iso": None, "aperture": None, "shutter": None, "lens": None, "camera_model": None}
    try:
        from image_safety import open_image
        with open_image(jpeg_path) as image:
            exif = image.getexif()
            info["camera_model"] = _exif_str(exif.get(272))
            info["lens"] = _exif_str(exif.get(42036))  # LensModel
            sub = exif.get_ifd(34665)  # Exif SubIFD
            info["iso"] = _exif_number(sub.get(34855, exif.get(34855)))
            info["aperture"] = _exif_number(sub.get(33437, exif.get(33437)))  # FNumber
            info["shutter"] = _exif_number(sub.get(33434, exif.get(33434)))  # ExposureTime
            if info["lens"] is None:
                info["lens"] = _exif_str(sub.get(42036))
    except Exception:
        pass  # EXIF is best-effort context; a receipt is still valid without it
    return info


def _frame_receipt(jpeg_path: Path) -> dict:
    """A durable fingerprint of the exact metered JPEG, computed before any settings change."""
    jpeg_path = Path(jpeg_path)
    data = jpeg_path.read_bytes()
    return {
        "path": str(jpeg_path),
        "bytes": len(data),
        "sha256": hashlib.sha256(data).hexdigest(),
        "exif": _frame_exif(jpeg_path),
    }


def _recommendation_to_exposure(recommendation: dict) -> dict:
    """Map a recommendation's labels to set_exposure kwargs (kelvin is carried separately)."""
    kwargs: dict[str, str] = {}
    for label_key, exposure_key in (("iso_label", "iso"), ("aperture_label", "aperture"),
                                    ("shutter_label", "shutter")):
        value = recommendation.get(label_key)
        if value is not None:
            kwargs[exposure_key] = str(value)
    return kwargs


class HandheldSession:
    """One bounded body-shutter metering session over a single camera."""

    def __init__(
        self,
        camera_factory: Callable[[], object],
        recommend_fn: Callable[[str, dict, dict], dict],
        *,
        max_frames: int,
        max_duration_s: float,
        out_dir: Path | str,
        restore: bool = True,
        recommend_timeout_s: float = 8.0,
        frame_timeout_s: float = 2.0,
        drain_ms: int = 500,
        quiet_window_s: float = 0.8,
        max_total_s: float = 10.0,
    ) -> None:
        if isinstance(max_frames, bool) or not isinstance(max_frames, int) or not 1 <= max_frames <= 200:
            raise ValueError("max_frames must be an integer in 1..200")
        if isinstance(max_duration_s, bool) or not isinstance(max_duration_s, (int, float)) or not 0 < max_duration_s <= 3600:
            raise ValueError("max_duration_s must be a number in (0, 3600]")
        self.camera_factory = camera_factory
        self.recommend_fn = recommend_fn
        self.max_frames = int(max_frames)
        self.max_duration_s = float(max_duration_s)
        self.out_dir = Path(out_dir)
        self.restore = bool(restore)
        self.recommend_timeout_s = float(recommend_timeout_s)
        self.frame_timeout_s = float(frame_timeout_s)
        self.drain_ms = int(drain_ms)
        self.quiet_window_s = float(quiet_window_s)
        self.max_total_s = float(max_total_s)
        self._lock = threading.Lock()
        self._state = {
            "status": "created",
            "frames": [],
            "stale_events_discarded": 0,
            "applied_count": 0,
            "failure_count": 0,
            "restored": None,
            "base_settings": None,
            "stop_reason": None,
            "started_at": None,
            "finished_at": None,
            "summary": None,
        }

    # --- public -----------------------------------------------------------
    def state(self) -> dict:
        """A JSON-safe snapshot of the current session state."""
        with self._lock:
            return copy.deepcopy(self._state)

    def run(self, cancel_event: "threading.Event | None" = None) -> dict:
        """Drive the metering loop until a stop condition; always restore in the ``finally``."""
        if cancel_event is None:
            cancel_event = threading.Event()
        journal = self.out_dir / JOURNAL_NAME
        camera = None
        base_settings: dict | None = None
        try:
            self.out_dir.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            with self._lock:
                self._state["status"] = "error"
                self._state["error"] = repr(exc)
                self._state["finished_at"] = time.time()
            raise
        with self._lock:
            self._state["status"] = "running"
            self._state["started_at"] = time.time()
        deadline = time.monotonic() + self.max_duration_s
        try:
            camera = self.camera_factory()
            connect = getattr(camera, "connect", None)
            if callable(connect):
                connect()
            wake = getattr(camera, "wake", None)
            if callable(wake):
                try:
                    wake()
                except Exception:
                    pass  # a body that won't wake still meters from whatever it pushes
            from sonyremote.camera import read_settings_checked
            # The base is restored at the end: never keep a placeholder read (f/0, ISO 0, 65535/65535).
            base_settings, base_reread = read_settings_checked(camera, purpose="handheld base settings")
            verification = getattr(camera, "serial_verification", None)
            verification = dict(verification) if isinstance(verification, dict) else None
            with self._lock:
                self._state["base_settings"] = dict(base_settings)
                self._state["serial_verification"] = verification
                if base_reread:
                    self._state["base_settings_read"] = base_reread
            self._append(journal, {"type": "session_start", "base_settings": base_settings,
                                   "serial_verification": verification,
                                   "max_frames": self.max_frames, "max_duration_s": self.max_duration_s})
            discarded = int(camera.drain_events(max_ms=self.drain_ms))
            with self._lock:
                self._state["stale_events_discarded"] = discarded
            self._append(journal, {"type": "drain", "stale_events_discarded": discarded})

            while True:
                if cancel_event.is_set():
                    self._set_stop_reason("cancelled")
                    break
                if len(self._state["frames"]) >= self.max_frames:
                    self._set_stop_reason("max_frames")
                    break
                if time.monotonic() >= deadline:
                    self._set_stop_reason("max_duration")
                    break
                files = camera.listen_for_files(
                    self.out_dir,
                    timeout_s=self.frame_timeout_s,
                    quiet_window_s=self.quiet_window_s,
                    max_total_s=self.max_total_s,
                    cancel=cancel_event,
                )
                if not files:
                    continue  # no frame this poll; bounds/cancel are re-checked at the top
                jpeg = _pick_jpeg([Path(f) for f in files])
                if jpeg is None:
                    self._record_unmeterable(journal, files)
                    continue
                self._process_frame(camera, jpeg, [Path(f) for f in files], journal)
        except Exception as exc:  # camera/transport error propagates AFTER restoration
            refusal = exc.as_dict() if _identity_refused(exc) else None
            with self._lock:
                # A camera identity refusal is a distinct, structured outcome that fired nothing.
                self._state["status"] = "identity_refused" if refusal else "error"
                self._state["error"] = str(exc) if refusal else repr(exc)
                if refusal:
                    self._state["identity_refusal"] = refusal
            self._append(journal, {"type": "identity_refused" if refusal else "error", "error": str(exc),
                                   **({"identity_refusal": refusal} if refusal else {})})
            raise
        finally:
            self._restore(camera, base_settings, journal)
            if camera is not None:
                disconnect = getattr(camera, "disconnect", None)
                if callable(disconnect):
                    try:
                        disconnect()
                    except Exception:
                        pass
            with self._lock:
                if self._state["status"] not in ("error", "identity_refused"):
                    self._state["status"] = "stopped"
                self._state["finished_at"] = time.time()
            self._write_summary(journal)
        return self.state()

    # --- internals --------------------------------------------------------
    def _set_stop_reason(self, reason: str) -> None:
        with self._lock:
            if self._state["stop_reason"] is None:
                self._state["stop_reason"] = reason

    def _safe_get_settings(self, camera) -> dict:
        return self._current_settings(camera)[0]

    def _current_settings(self, camera) -> tuple[dict, dict | None]:
        """(settings, placeholder refusal). A persistent placeholder read (f/0, ISO 0, 65535/65535)
        yields ({}, refusal) so the caller can skip planning from it; other failures yield ({}, None)."""
        from sonyremote.camera import PlaceholderSettingsError, read_settings_checked
        try:
            return read_settings_checked(camera, purpose="handheld current settings")[0], None
        except PlaceholderSettingsError as exc:
            return {}, exc.as_dict()
        except Exception:
            return {}, None

    def _record_unmeterable(self, journal: Path, files: list) -> None:
        """A push with no JPEG (e.g. RAW-only) is recorded but cannot drive a recommendation."""
        index = len(self._state["frames"])
        record = {
            "type": "frame", "index": index, "jpeg": None,
            "downloaded": [str(f) for f in files], "receipt": None,
            "observed_at": time.time(), "apply_status": "unmeterable",
            "why": ["no JPEG in this push; cannot meter without a JPEG twin"],
            "applied": None, "readback": None, "source": None, "settings_for_next_frame": None,
        }
        with self._lock:
            self._state["frames"].append(record)
        self._append(journal, {"type": "frame_unmeterable", "index": index,
                               "downloaded": [str(f) for f in files]})

    def _process_frame(self, camera, jpeg: Path, files: list, journal: Path) -> None:
        index = len(self._state["frames"])
        receipt = _frame_receipt(jpeg)
        current, current_refused = self._current_settings(camera)
        record = {
            "type": "frame", "index": index, "jpeg": str(jpeg),
            "downloaded": [str(f) for f in files], "receipt": receipt,
            "sharpness": None,
            "current_settings": current, "observed_at": time.time(),
            "apply_status": "pending", "applied": None, "readback": None,
            "why": [], "source": None, "settings_for_next_frame": None,
        }
        # The receipt is journaled BEFORE any settings change, so a durable record of the exact
        # frame exists even if the recommendation or the apply below fails. The record joins the
        # shared state only once it is complete, so state() never observes a half-written dict.
        self._append(journal, {"type": "frame_metered", "index": index,
                               "receipt": receipt, "current_settings": current})
        try:
            if current_refused:
                # The body still answers placeholders: keep this frame's record, change nothing.
                record["apply_status"] = "skipped"
                record["why"] = ["camera settings read returned placeholder values; no adjustment"]
                record["settings_read_refused"] = current_refused
                self._append(journal, {"type": "frame_skip", "index": index,
                                       "reason": "placeholder_settings", "refusal": current_refused})
            else:
                self._decide_and_apply(camera, jpeg, index, receipt, current, record, journal)
        finally:
            # Scored after the next-frame write so a 24 MP decode never delays the exposure change.
            try:
                import focus_verify
                record["sharpness"] = focus_verify.sharpness_evidence(str(jpeg))
            except (ImportError, OSError, ValueError) as exc:
                record["sharpness"] = {"verdict": "undetermined", "reason": f"sharpness scoring failed: {exc}",
                                       "calibration": "uncalibrated_relative"}
            with self._lock:
                self._state["frames"].append(record)

    def _decide_and_apply(self, camera, jpeg: Path, index: int, receipt: dict, current: dict,
                          record: dict, journal: Path) -> None:

        recommendation, rec_error = self._recommend_bounded(jpeg, current, receipt["exif"])
        if recommendation is None:
            record["apply_status"] = "skipped"
            record["why"] = ["recommendation unavailable"] + ([rec_error] if rec_error else [])
            self._append(journal, {"type": "frame_skip", "index": index,
                                   "reason": rec_error or "no recommendation"})
            return

        record["why"] = list(recommendation.get("why") or [])
        record["source"] = recommendation.get("source")
        exposure_kwargs = _recommendation_to_exposure(recommendation)
        next_block = dict(exposure_kwargs)
        if recommendation.get("kelvin") is not None:
            next_block["kelvin"] = recommendation.get("kelvin")  # UI context; not a set_exposure arg
        record["settings_for_next_frame"] = next_block

        if not exposure_kwargs:
            record["apply_status"] = "no_change"
            self._append(journal, {"type": "frame_no_change", "index": index, "why": record["why"]})
            return
        try:
            applied = self._apply_with_settle(camera, exposure_kwargs)  # verifies readback internally
            readback = self._safe_get_settings(camera)
            record["applied"] = applied
            record["readback"] = readback
            record["apply_status"] = "applied"
            with self._lock:
                self._state["applied_count"] += 1
            self._append(journal, {"type": "frame_applied", "index": index, "applied": applied,
                                   "readback": readback, "why": record["why"],
                                   "source": record["source"], "settings_for_next_frame": next_block})
        except Exception as exc:
            if _identity_refused(exc):
                raise  # terminal: stop the session rather than keep writing to an unproven body
            # Apply failed: record it and keep listening with settings unchanged. We never retry
            # the shutter and never re-download — the next real frame drives the next attempt.
            record["apply_status"] = "failed"
            record["apply_error"] = str(exc)
            with self._lock:
                self._state["failure_count"] += 1
            self._append(journal, {"type": "frame_apply_failed", "index": index, "error": str(exc)})

    APPLY_SETTLE_DELAYS_S = (0.0, 2.0, 3.0)

    def _apply_with_settle(self, camera, exposure_kwargs: dict) -> dict:
        """Right after a body-shutter capture the body is still writing RAW+JPEG and can refuse
        setting writes for a few seconds. Retry within a bounded window; the last error surfaces."""
        last: Exception | None = None
        for delay in self.APPLY_SETTLE_DELAYS_S:
            if delay:
                time.sleep(delay)
            try:
                return camera.set_exposure(**exposure_kwargs)
            except Exception as exc:  # noqa: BLE001 — retried within the bounded window
                if _identity_refused(exc):
                    raise  # a refused camera identity is terminal, never retried
                last = exc
        assert last is not None
        raise last

    def _recommend_bounded(self, jpeg: Path, current: dict, exif: dict) -> tuple[dict | None, str | None]:
        """Run recommend_fn under a deadline in a worker thread; on timeout skip the adjustment."""
        result: dict = {}

        def worker() -> None:
            try:
                result["value"] = self.recommend_fn(str(jpeg), current, exif)
            except Exception as exc:  # noqa: BLE001 — surfaced as a skip reason, never crashes the loop
                result["error"] = exc

        thread = threading.Thread(target=worker, name="handheld-recommend", daemon=True)
        thread.start()
        thread.join(self.recommend_timeout_s)
        if thread.is_alive():
            return None, "recommendation timed out"
        if "error" in result:
            return None, f"recommendation error: {result['error']}"
        value = result.get("value")
        if not isinstance(value, dict):
            return None, "recommendation was not a dict"
        return value, None

    def _restore(self, camera, base_settings: dict | None, journal: Path) -> None:
        if not self.restore:
            with self._lock:
                self._state["restored"] = None
            return
        if camera is None or not base_settings:
            with self._lock:
                self._state["restored"] = False
            return
        try:
            kwargs = {key: base_settings[key] for key in _EXPOSURE_KEYS
                      if base_settings.get(key) is not None}
            if kwargs:
                camera.set_exposure(**kwargs)
            readback = self._safe_get_settings(camera)
            restored_ok = bool(readback) and all(
                str(readback.get(key)) == str(base_settings.get(key)) for key in kwargs
            )
            with self._lock:
                self._state["restored"] = restored_ok
            self._append(journal, {"type": "restore", "restored": restored_ok,
                                   "target": kwargs, "readback": readback})
        except Exception as exc:
            with self._lock:
                self._state["restored"] = False
            self._append(journal, {"type": "restore_failed", "error": str(exc)})

    def _write_summary(self, journal: Path) -> None:
        with self._lock:
            summary = {
                "type": "summary",
                "frames": len(self._state["frames"]),
                "stale_events_discarded": self._state["stale_events_discarded"],
                "applied": self._state["applied_count"],
                "failures": self._state["failure_count"],
                "restored": bool(self._state["restored"]),
                "stop_reason": self._state["stop_reason"],
                "status": self._state["status"],
            }
            self._state["summary"] = summary
        self._append(journal, summary)

    def _append(self, path: Path, record: dict) -> None:
        """Append one JSON line and fsync it, so the journal survives a crash mid-session."""
        line = json.dumps(record, default=str, allow_nan=False, sort_keys=True)
        with open(path, "a", encoding="utf-8") as stream:
            stream.write(line + "\n")
            stream.flush()
            os.fsync(stream.fileno())
