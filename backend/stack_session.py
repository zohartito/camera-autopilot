"""Bounded multi-frame sessions with durable capture intent and exact JPEG provenance.

The caller owns the camera lock and supplies a single-shot adapter. An adapter must
write the *new* photograph to the requested destination and must never retry an
ambiguous shutter result. A persisted in-flight frame always blocks automatic
resume: a network timeout cannot establish whether the physical shutter fired.
"""
from __future__ import annotations

import fcntl
import hashlib
import json
import math
import os
import re
import stat
import contextlib
import time
import uuid
from pathlib import Path
from typing import Callable

from image_safety import image_size_from_bytes, source_bytes

MAX_STACK_FRAMES = 16
MAX_SEQUENCE_FRAMES = 240
MAX_SESSION_SOURCE_BYTES = 512 * 1024 * 1024
# Deletion bound: frames, their evidence sidecars and deflicker copies, plus fixed artifacts.
MAX_SESSION_ENTRIES = 3 * MAX_SEQUENCE_FRAMES + 256
# Longest stretch of a between-frame wait whose run bookkeeping a hard kill can lose.
PROGRESS_CHECKPOINT_S = 30.0
CROWD_AUTO_MAX_FRAMES = 8  # N full-resolution label masks are held in RAM
MODES = ("hdr", "long_exposure", "crowd_remove", "night", "timelapse", "panorama")
# Modes whose accepted prefix is a complete result on its own: the user (or an automatic
# threshold) may stop at a frame boundary and merge exactly the frames accepted so far.
STOPPABLE_MODES = ("crowd_remove",)
# A merge needs at least two frames; a stop with fewer accepted becomes a resumable cancel.
MIN_STOP_MERGE_FRAMES = 2
# Live crowd previews are computed on copies whose long edge is at most this many pixels.
CROWD_PREVIEW_EDGE = 640
# Every session stops capturing after one hour of execution, except a Holy Grail
# timelapse: day to night (golden hour through nautical dusk) routinely runs 60 to
# 120 minutes, so it gets three hours. Accepted frames are preserved either way.
# The budget belongs to the session: time spent in earlier runs (before a cancel,
# failure or server restart) counts against it; only a new session starts afresh.
SESSION_EXECUTION_BUDGET_S = 3600.0
HOLY_GRAIL_EXECUTION_BUDGET_S = 3 * 3600.0


def execution_budget_s(mode, exposure_config):
    """Seconds a session may keep capturing; Holy Grail alone gets the longer budget."""
    if mode == "timelapse" and (exposure_config or {}).get("mode") == "holy_grail":
        return HOLY_GRAIL_EXECUTION_BUDGET_S
    return SESSION_EXECUTION_BUDGET_S


def execution_budget_status(mode, exposure_config, used_s):
    """Report which budget applies to this session and how much of it is spent."""
    budget = execution_budget_s(mode, exposure_config)
    return {"budget_s": budget,
            "applies": "holy_grail_timelapse" if budget == HOLY_GRAIL_EXECUTION_BUDGET_S else "session",
            "scope": "per_session", "used_s": used_s, "remaining_s": max(0.0, budget - used_s),
            "exhausted": used_s >= budget}


def validate_frames(frames: int, *, sequence: bool = False, panorama: bool = False) -> int:
    if panorama:
        from panorama import MAX_FRAMES
        limit = MAX_FRAMES
    else:
        limit = MAX_SEQUENCE_FRAMES if sequence else MAX_STACK_FRAMES
    if isinstance(frames, bool) or not isinstance(frames, int) or not 2 <= frames <= limit:
        raise ValueError(f"frames must be an integer between 2 and {limit}")
    return frames


# Wall-clock gap slack before an interval counts as overrun: the runner's 0.1 s sleep
# granularity plus the controls read that precedes every shutter.
CADENCE_GAP_TOLERANCE_S = 0.25


def _median(values: list[float]) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    middle = len(ordered) // 2
    if len(ordered) % 2:
        return round(ordered[middle], 3)
    return round((ordered[middle - 1] + ordered[middle]) / 2, 3)


def timelapse_cadence(state: dict) -> dict:
    """Achieved-versus-requested cadence measured from the frames' own timestamps.

    ``requested_*`` fields restate what the session asked for; every ``achieved_*`` and
    ``measured_*`` field is derived from ``capture_started_at`` (wall clock) and the
    per-frame ``capture_s`` cost the runner recorded. This only reports; it never retimes
    or drops a frame. ``cadence_overrun`` is set as soon as one accepted frame cost more
    than the requested interval, so the warning exists before the next shutter fires.
    """
    requested_raw = state.get("requested_interval_s", state.get("interval_s", 0.0))
    requested = float(requested_raw) if type(requested_raw) in (int, float) else 0.0
    cadence_requested = requested > 0
    frames = state.get("frames") or []
    accepted = sorted((f for f in frames if f.get("status") == "accepted"), key=lambda f: f["index"])
    counts = {"requested": len(frames), "accepted": len(accepted),
              "missing": len(frames) - len(accepted),
              "ambiguous": sum(f.get("status") == "ambiguous" for f in frames)}
    gaps, unreliable = [], 0
    for previous, current in zip(accepted, accepted[1:]):
        earlier, later = previous.get("capture_started_at"), current.get("capture_started_at")
        if (current["index"] != previous["index"] + 1 or type(earlier) not in (int, float)
                or type(later) not in (int, float) or later < earlier):
            unreliable += 1  # a gap across a missing frame or a clock that moved backwards
            continue
        gaps.append(round(later - earlier, 3))
    costs = [round(f["capture_s"], 3) for f in accepted if type(f.get("capture_s")) in (int, float)]
    pipeline = [round(f["capture_pipeline_s"], 3) for f in accepted
                if type(f.get("capture_pipeline_s")) in (int, float)]
    first, last = (accepted[0].get("capture_started_at"), accepted[-1].get("capture_started_at")) if accepted else (None, None)
    span = (round(last - first, 3) if type(first) in (int, float) and type(last) in (int, float)
            and last >= first and not unreliable else None)
    median_gap = _median(gaps)
    median_cost = _median(costs)
    tolerance = round(max(CADENCE_GAP_TOLERANCE_S, 0.05 * requested), 3)
    overrun_intervals = sum(gap > requested + tolerance for gap in gaps) if cadence_requested else 0
    costly_frames = sum(cost > requested for cost in costs) if cadence_requested else 0
    reason = None
    if cadence_requested and costly_frames:
        reason = (f"this body needs about {median_cost:.1f} s per frame, so the requested "
                  f"{requested:g} s interval cannot be met")
    elif cadence_requested and overrun_intervals:
        reason = (f"{overrun_intervals} of {len(gaps)} intervals overran the requested "
                  f"{requested:g} s by more than {tolerance:g} s")
    return {
        "requested_interval_s": requested,
        "cadence_requested": cadence_requested,
        "gap_source": "capture_started_at_wall_clock",
        "achieved_gaps_s": gaps,
        "achieved_interval_count": len(gaps),
        "unreliable_gap_count": unreliable,
        "achieved_interval_median_s": median_gap,
        "achieved_interval_mean_s": round(sum(gaps) / len(gaps), 3) if gaps else None,
        "achieved_interval_min_s": min(gaps) if gaps else None,
        "achieved_interval_max_s": max(gaps) if gaps else None,
        "achieved_span_first_to_last_s": span,
        "requested_span_first_to_last_s": round(requested * max(len(accepted) - 1, 0), 3) if cadence_requested else None,
        "overrun_tolerance_s": tolerance,
        "overrun_interval_count": overrun_intervals,
        "measured_frame_cost_s": costs,
        "measured_frame_cost_median_s": median_cost,
        "measured_frame_cost_max_s": max(costs) if costs else None,
        "measured_pipeline_median_s": _median(pipeline),
        "minimum_feasible_interval_s": median_cost,
        "frames_costlier_than_interval": costly_frames,
        "cadence_overrun": bool(reason),
        "cadence_overrun_reason": reason,
        # Each frame stands for one interval of real time, so the finished video covers
        # the first-to-last span plus one achieved interval; never the requested span.
        "video_real_time_span_s": round(span + median_gap, 3) if span is not None and median_gap is not None else None,
        "video_requested_real_time_span_s": round(requested * len(accepted), 3) if cadence_requested else None,
        "frames": counts,
    }


def _record_cadence(state: dict) -> None:
    if state.get("mode") == "timelapse":
        state["timelapse_cadence"] = timelapse_cadence(state)
        state["cadence_overrun"] = state["timelapse_cadence"]["cadence_overrun"]


MAX_ROLLUP_REASONS = 3   # distinct failure texts kept in a session rollup; the rest are counted
_WARNING_FRAME_NUMBERS = 5  # frame numbers spelled out in the warning sentence
_REASON_REFERENCE = "transport_receipt.delivery_durability"


def _record_delivery_durability(state: dict) -> None:
    """Roll up frames whose camera download was not confirmed durable (#61's evidence).

    Unconfirmed means the directory fsync of the camera's download folder failed after the frame
    was delivered there. An accepted frame's session copy was fsynced and its folder flushed by the
    manifest save that accepted it, so the warning names the failing disk, not a lost frame.
    Runs on every manifest save: each frame's flag follows its current transport receipt (older
    manifests are backfilled) and the count follows the current frame_count (Smartlapse).
    Bounded for a 240-frame timelapse under MAX_MANIFEST_BYTES: the frame references its receipt's
    reason instead of copying it, and the rollup keeps frame indices plus at most MAX_ROLLUP_REASONS
    distinct reasons. It reads only the manifest: an accepted frame's compact transport_receipt keeps
    delivery_durability (TRANSPORT_INLINE_KEYS), identical to the one in its evidence sidecar."""
    flagged = []
    for frame in state.get("frames") or []:
        receipt = frame.get("transport_receipt")
        durability = receipt.get("delivery_durability") if isinstance(receipt, dict) else None
        # Only a frame whose shutter may have fired has a delivery; a pending frame (never fired, or
        # refused before its shutter) never inherits a warning from a leftover receipt.
        fired = frame.get("status") in ("capturing", "accepted", "ambiguous")
        if fired and isinstance(durability, dict) and durability.get("status") == "unconfirmed":
            frame["delivery_durability"] = {"status": "unconfirmed", "reason_at": _REASON_REFERENCE}
            flagged.append((frame, durability.get("reason")))
        else:
            frame.pop("delivery_durability", None)
    if not flagged:
        state.pop("delivery_durability", None)
        return
    reasons = list(dict.fromkeys(reason for _, reason in flagged))
    numbers = [str(frame["index"] + 1) for frame, _ in flagged]
    if len(numbers) == 1:
        which = f"frame {numbers[0]} of {state['frame_count']}"
    elif len(numbers) <= _WARNING_FRAME_NUMBERS:
        which = f"frames {', '.join(numbers)} of {state['frame_count']}"
    else:
        which = (f"{len(numbers)} of {state['frame_count']} frames ({', '.join(numbers[:_WARNING_FRAME_NUMBERS])}"
                 f" and {len(numbers) - _WARNING_FRAME_NUMBERS} more)")
    state["delivery_durability"] = {
        "status": "unconfirmed",
        "frames": [frame["index"] for frame, _ in flagged],
        "reasons": reasons[:MAX_ROLLUP_REASONS],
        "reasons_omitted": max(0, len(reasons) - MAX_ROLLUP_REASONS),
        "warning": (f"Warning: saving was not confirmed durable for {which}"
                    " — the disk reported an error while flushing the camera download's folder."
                    " Every frame the session accepted was also written to the session folder and flushed;"
                    " check the disk."),
    }


def _atomic_json(path: Path, value: dict) -> None:
    _atomic_write(path, json.dumps(value, indent=2, allow_nan=False).encode())


def _atomic_write(path: Path, data: bytes) -> None:
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    replaced = False
    try:
        descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        replaced = True
    finally:
        if not replaced:  # a failed write never leaves its temporary behind (saves retry sidecars)
            with contextlib.suppress(OSError):
                temporary.unlink()
    descriptor = os.open(path.parent, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


# Manifest byte budget (measured 2026-09-27 on the real A7 II/A7 III sessions under
# backend/shots/sessions and artifacts/). Every save rewrites the whole manifest, so what grows
# with the frame count stays small: an accepted frame's full camera transport receipt and full
# applied settings (a metered timelapse also records the lens and shutter ladders) are written
# once to frame_NNNN.evidence.json beside the frame, hashed from the manifest, which keeps a
# compact view. Inline, a real frame cost 2.8-3.8 KB (9.6 KB for Holy Grail), and a 240-frame
# run passed the old fixed 512 KiB read limit part way and could not be read or recovered.
FRAME_RECORD_BUDGET_BYTES = 2048      # one accepted, compacted frame record: ~0.8-1.45 KB measured
                                      # plus its two absolute paths (destination, receipt.path) of
                                      # up to 256 characters each (a production path is ~115)
FRAME_ROLLUP_BUDGET_BYTES = 512       # its share of rollups: cadence series, deflicker gains and receipts
INLINE_FRAME_BUDGET_BYTES = 32 * 1024  # the one frame whose evidence is still inline (in flight or ambiguous)
SESSION_BASE_BUDGET_BYTES = 128 * 1024  # everything that does not grow with frames (plans, publication, video)
MANIFEST_BUDGET_MARGIN = 2
MAX_MANIFEST_BYTES = MANIFEST_BUDGET_MARGIN * (
    SESSION_BASE_BUDGET_BYTES + INLINE_FRAME_BUDGET_BYTES
    + MAX_SEQUENCE_FRAMES * (FRAME_RECORD_BUDGET_BYTES + FRAME_ROLLUP_BUDGET_BYTES))
MAX_EVIDENCE_SIDECAR_BYTES = 256 * 1024
# A manifest written before sidecars (no "evidence_format") had no size check on write: an
# uninterrupted 240-frame Holy Grail run held ~240 x 9.6 KB (the measured real frame; 10.7 KB with a
# current A7 III receipt and a delivery-durability flag) of inline evidence, ~2.3-2.6 MB. It is read
# under its own hard ceiling, 240 x 16 KiB (1.5x the 10.7 KB worst case) + the 128 KiB base =
# 4,063,232 bytes, and compacted to sidecars on its first write. Sidecar-format manifests keep
# MAX_MANIFEST_BYTES.
EVIDENCE_FORMAT = "sidecar"
LEGACY_FRAME_MAX_BYTES = 16 * 1024
LEGACY_MAX_MANIFEST_BYTES = SESSION_BASE_BUDGET_BYTES + MAX_SEQUENCE_FRAMES * LEGACY_FRAME_MAX_BYTES
# What the manifest keeps of the evidence; the sidecar keeps all of it. The meter keys are every
# key a restart reads to continue a metered ramp (product_sessions.run), plus range_limited.
TRANSPORT_INLINE_KEYS = ("capture_confirmed", "reconnect_ok", "warning", "serial", "delivery_durability")
METER_INLINE_KEYS = ("requested_ev", "actual_ev", "measured_ev", "scene_ev_estimate", "range_limited")


class ManifestBudgetError(ValueError):
    """The session manifest would exceed the byte limit its reader enforces."""


class CancelRefused(RuntimeError):
    """Cancel refused and nothing was changed: the capture is over, so a Cancel could only be ignored."""


class MergeAlreadyRunning(CancelRefused):
    """The session is merging its accepted frames, or (split runtime) its heavy job is queued or running."""
    merge_running = True


class NothingToCancel(CancelRefused):
    """Split runtime: every photo is captured and the heavy job has ended (completed or failed)."""


MERGE_RUNNING_DETAIL = ("The merge is already running; it will finish shortly. Cancel is available while "
                        "frames are being captured.")
NOTHING_TO_CANCEL_DETAIL = ("Every photo of this session is captured and nothing is running, so there is nothing "
                            "to cancel. Cancel is available while frames are being captured.")


def refuse_cancel(state: dict, *, split_control: bool = False) -> None:
    """Raise CancelRefused when a Cancel could only be ignored. ``merging`` is entered under the lock
    cancel() takes, after run()'s last Cancel check; in the split runtime the session then becomes
    ``completed`` with its ``heavy_job`` queued in one manifest write, and the result follows from the
    saved frames whatever a Cancel says (heavy_jobs.process_locked never reads a Cancel).
    ``split_control`` (the split runtime's control role) refuses every ``completed`` session: one
    written before its job was queued is queued by its next run and published like any other."""
    job = state.get("heavy_job")
    job_status = job.get("status") if isinstance(job, dict) else None
    if state["status"] == "merging" or (state["status"] == "completed" and job_status in ("queued", "running")):
        raise MergeAlreadyRunning(MERGE_RUNNING_DETAIL)
    if state["status"] == "completed" and (job_status is not None or split_control):
        # A failed job's retry (POST /sessions/{id}/run) publishes; an accepted Cancel would be ignored.
        raise NothingToCancel(NOTHING_TO_CANCEL_DETAIL)


def _manifest_bytes(state: dict) -> bytes:
    return json.dumps(state, separators=(",", ":"), allow_nan=False).encode()


def _write_manifest(directory: Path, state: dict) -> None:
    """Never write a manifest SessionRunner.read would refuse. Every accepted frame whose full
    evidence is still inline (a legacy manifest, or a sidecar write that failed earlier) is moved
    to its sidecar on each write; while that keeps failing the evidence stays inline, and a
    manifest over the limit is not written."""
    for frame in state.get("frames") or []:
        if (frame.get("status") == "accepted" and isinstance(frame.get("receipt"), dict)
                and "evidence_sidecar" not in frame and not frame.get("evidence_not_stored")):
            _store_frame_evidence(directory, frame, keep_inline_on_failure=True)
    state["evidence_format"] = EVIDENCE_FORMAT
    data = _manifest_bytes(state)
    if len(data) > MAX_MANIFEST_BYTES:
        raise ManifestBudgetError(f"session manifest would be {len(data)} bytes, over its "
                                  f"{MAX_MANIFEST_BYTES}-byte limit; it was not written")
    _atomic_write(directory / "manifest.json", data)


def manifest_projection_bytes(state: dict) -> int:
    """Worst case once every frame not yet accepted is accepted: the manifest as it is now, each
    remaining frame's budgeted record, one frame with inline evidence, and every frame's rollup
    share (the final render adds per-frame receipts for accepted frames too)."""
    frames = state.get("frames") or []
    remaining = sum(frame.get("status") != "accepted" for frame in frames)
    return (len(_manifest_bytes(state)) + INLINE_FRAME_BUDGET_BYTES
            + remaining * FRAME_RECORD_BUDGET_BYTES + len(frames) * FRAME_ROLLUP_BUDGET_BYTES)


def check_manifest_plan(state: dict) -> None:
    projected = manifest_projection_bytes(state)
    if projected > MAX_MANIFEST_BYTES:
        raise ManifestBudgetError(
            f"the session manifest could grow to {projected} bytes, over its {MAX_MANIFEST_BYTES}-byte "
            "limit, so this plan is refused before any shutter; shorten the session or its metadata")


def evidence_sidecar_name(index: int) -> str:
    return f"frame_{index:04d}.evidence.json"


def _compact_evidence(frame: dict) -> dict:
    """The manifest's view of a frame's applied settings and transport receipt."""
    compact = {}
    applied = frame.get("applied")
    if isinstance(applied, dict):
        view = {key: value for key, value in applied.items() if not isinstance(value, (dict, list))}
        meter = applied.get("timelapse_meter")
        if isinstance(meter, dict):
            view["timelapse_meter"] = {key: meter[key] for key in METER_INLINE_KEYS if key in meter}
        transition = applied.get("exposure_transition")
        if isinstance(transition, dict):
            view["exposure_transition"] = {key: value for key, value in transition.items()
                                           if not isinstance(value, (dict, list))}
        compact["applied"] = view
    receipt = frame.get("transport_receipt")
    if isinstance(receipt, dict):
        view = {key: receipt[key] for key in TRANSPORT_INLINE_KEYS if key in receipt}
        timings = receipt.get("timings")
        if isinstance(timings, dict) and "total_s" in timings:
            view["timings"] = {"total_s": timings["total_s"]}
        compact["transport_receipt"] = view
    return compact


def _store_frame_evidence(directory: Path, frame: dict, *, keep_inline_on_failure: bool = False) -> None:
    """Write an accepted frame's full evidence to its sidecar, then compact the frame in place.

    The sidecar is flushed before the manifest save that references it; a crash in between
    leaves the frame `capturing` in the manifest (never re-shot) beside an unreferenced sidecar.
    If the sidecar cannot be written, the acceptance and the JPEG receipt must still be saved:
    the evidence stays inline when it fits the manifest's reserved inline slot, otherwise only
    its compact view does (``evidence_not_stored``), and ``evidence_sidecar_error`` says so.
    Retrying inline evidence (``keep_inline_on_failure``) never drops it; a success clears the
    error marker.
    """
    if not any(isinstance(frame.get(key), dict) for key in ("applied", "transport_receipt")):
        return
    payload = {"schema": 1, "index": frame["index"], "frame_sha256": frame["receipt"]["sha256"]}
    payload.update({key: frame[key] for key in ("applied", "transport_receipt") if key in frame})
    data = json.dumps(payload, indent=2, allow_nan=False).encode()
    name = evidence_sidecar_name(frame["index"])
    try:
        if len(data) > MAX_EVIDENCE_SIDECAR_BYTES:
            raise ValueError(f"frame evidence is {len(data)} bytes, over its {MAX_EVIDENCE_SIDECAR_BYTES}-byte sidecar limit")
        _atomic_write(directory / name, data)
    except (OSError, ValueError) as exc:
        if keep_inline_on_failure:
            return  # the evidence stays inline, untouched; the next write retries
        frame["evidence_sidecar_error"] = f"{type(exc).__name__}: {exc}"
        if len(_manifest_bytes(payload)) > INLINE_FRAME_BUDGET_BYTES:
            frame.update(_compact_evidence(frame))
            frame["evidence_not_stored"] = True  # only its compact view remains; never retried
            frame["evidence_sidecar_error"] += "; the full evidence was too large to keep inline and is not stored"
        return
    frame.pop("evidence_sidecar_error", None)
    frame.update(_compact_evidence(frame))
    frame["evidence_sidecar"] = {"file": name, "bytes": len(data), "sha256": hashlib.sha256(data).hexdigest()}


def frame_receipt(path: str | Path) -> dict:
    """Validate one exact JPEG and fingerprint the same bounded byte snapshot."""
    path = Path(path)
    if path.suffix.lower() not in (".jpg", ".jpeg") or path.is_symlink():
        raise ValueError("session frames must be regular JPEG files, not RAW or symlinks")
    from image_safety import MAX_IMAGE_SOURCE_BYTES
    data = regular_bytes(path, MAX_IMAGE_SOURCE_BYTES)
    if not data.startswith(b"\xff\xd8"):
        raise ValueError("session frame is not JPEG")
    width, height = image_size_from_bytes(data)
    # Verify the complete encoded image, not just a readable header.
    from image_safety import open_rgb_from_bytes
    with open_rgb_from_bytes(data) as image:
        image.load()
    return {"path": str(path.resolve()), "bytes": len(data), "width": width,
            "height": height, "sha256": hashlib.sha256(data).hexdigest()}


def regular_bytes(path: Path, limit: int) -> bytes:
    """Read one bounded regular file without following the final symlink."""
    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(descriptor, "rb") as stream:
        before = os.fstat(stream.fileno())
        if not stat.S_ISREG(before.st_mode) or not 0 < before.st_size <= limit:
            raise ValueError("session artifact exceeds byte budget or is not regular")
        data = stream.read(limit + 1)
        after = os.fstat(stream.fileno())
    if len(data) != before.st_size or before.st_size != after.st_size:
        raise ValueError("session artifact changed during read")
    return data


def binary_receipt(path: Path, limit: int = MAX_SESSION_SOURCE_BYTES + 1024 * 1024) -> dict:
    """Stream-hash a bounded archive, avoiding a whole ZIP allocation."""
    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    digest, count = hashlib.sha256(), 0
    with os.fdopen(descriptor, "rb") as stream:
        before = os.fstat(stream.fileno())
        if not stat.S_ISREG(before.st_mode) or not 0 < before.st_size <= limit:
            raise ValueError("session artifact exceeds byte budget or is not regular")
        while chunk := stream.read(1024 * 1024):
            count += len(chunk)
            if count > before.st_size:
                raise ValueError("session artifact grew during read")
            digest.update(chunk)
        if count != before.st_size or os.fstat(stream.fileno()).st_size != before.st_size:
            raise ValueError("session artifact changed during read")
    return {"path": str(path.resolve()), "bytes": count, "sha256": digest.hexdigest()}


def _enumerate_for_deletion(directory: Path):
    """Collect regular files and (bottom-up) subdirectories, flagging any symlink.

    os.walk with followlinks=False never descends a symlinked directory, so an
    escape can only appear as an entry name; we flag it and delete nothing.
    """
    files, subdirs, symlink_seen = [], [], False
    for root, dirnames, filenames in os.walk(directory, topdown=False, followlinks=False):
        root_path = Path(root)
        for name in filenames:
            path = root_path / name
            if path.is_symlink():
                symlink_seen = True
            elif path.is_file():
                files.append(path)
            else:
                raise ValueError("session deletion refuses non-regular entries")
        for name in dirnames:
            path = root_path / name
            if path.is_symlink():
                symlink_seen = True
            else:
                subdirs.append(path)
    return files, subdirs, symlink_seen


class SessionRunner:
    def __init__(self, root: str | Path):
        self.root = Path(root).resolve()
        self.root.mkdir(parents=True, exist_ok=True)

    def _directory(self, session_id: str) -> Path:
        if not re.fullmatch(r"[0-9a-f]{32}", session_id):
            raise ValueError("invalid session id")
        directory = self.root / session_id
        if directory.is_symlink():
            raise ValueError("session directory cannot be a symlink")
        return directory

    @contextlib.contextmanager
    def _control(self, session_id: str):
        descriptor = os.open(self._directory(session_id) / "control.lock",
                             os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
        with os.fdopen(descriptor, "a") as lock:
            if not stat.S_ISREG(os.fstat(lock.fileno()).st_mode):
                raise ValueError("session control lock must be regular")
            fcntl.flock(lock, fcntl.LOCK_EX)
            yield

    @contextlib.contextmanager
    def operation(self, session_id: str):
        """Serialize full adapter/publication/deletion operations, beyond camera capture."""
        directory = self._directory(session_id)
        with (directory / "operation.lock").open("a") as lock:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as exc:
                raise RuntimeError("session has an active operation") from exc
            yield

    def save(self, state: dict) -> None:
        _record_delivery_durability(state)
        state["updated_at"] = time.time()
        _write_manifest(self._directory(state["id"]), state)

    def artifact(self, session_id: str, name: str) -> Path:
        """Return only a manifest-owned, accepted, hash-verified bounded artifact."""
        state = self.read(session_id)
        directory = self._directory(session_id)
        receipt = None
        match = re.fullmatch(r"frame_([0-9]{4})\.jpg", name)
        if match:
            index = int(match.group(1))
            if index >= len(state["frames"]):
                raise FileNotFoundError(name)
            frame = state["frames"][index]
            if frame["status"] == "accepted":
                receipt = frame.get("receipt")
        elif name == "result.jpg":
            receipt = (state.get("output") or {}).get("receipt")
        elif re.fullmatch(r"preview_[0-9]{4}\.mp4", name):
            preview = state.get("preview_video") or {}
            if preview.get("status") == "completed" and preview.get("name") == name:
                receipt = preview.get("receipt")
        elif name == "timelapse.mp4":
            receipt = (state.get("video") or {}).get("receipt")
        elif name == "sequence.zip":
            receipt = (state.get("sequence") or {}).get("receipt")
        elif name in ("timelapse_partial.mp4", "timelapse_partial.json"):
            partial = state.get("partial_video") or {}
            if partial.get("status") == "completed":
                receipt = (partial.get("receipt") if name.endswith(".mp4")
                           else (partial.get("receipt_file") or {}).get("receipt"))
        if not receipt:
            raise FileNotFoundError("artifact is not an accepted session output")
        path = directory / name
        if Path(receipt["path"]).resolve() != path.resolve():
            raise ValueError("artifact receipt escaped its session directory")
        actual = binary_receipt(path) if name.endswith((".zip", ".mp4", ".json")) else frame_receipt(path)
        if actual["sha256"] != receipt["sha256"] or actual["bytes"] != receipt["bytes"]:
            raise ValueError("session artifact changed after acceptance")
        return path

    def delete(self, session_id: str, *, discard_unresolved: bool = False) -> dict:
        with self.operation(session_id):
            return self._delete_locked(session_id, discard_unresolved=discard_unresolved)

    def _delete_locked(self, session_id: str, *, discard_unresolved: bool = False) -> dict:
        with self._control(session_id):
            return self._delete_controlled(session_id, discard_unresolved=discard_unresolved)

    def _delete_controlled(self, session_id: str, *, discard_unresolved: bool = False) -> dict:
        directory = self._directory(session_id)
        with (directory / "runner.lock").open("a") as lock:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as exc:
                raise RuntimeError("session is actively capturing; cancel and wait before deleting") from exc
            state = self.read(session_id)
            job_path = directory / "job.json"
            if job_path.exists():
                job = json.loads(regular_bytes(job_path, 65536))
                if job.get("status") in ("queued", "running"):
                    raise RuntimeError("session has an active background job; cancel and wait before deleting")
            unresolved = state.get("restore") in ("pending", "failed") or any(
                frame["status"] in ("capturing", "ambiguous") for frame in state["frames"])
            if unresolved and not discard_unresolved:
                raise RuntimeError("session has unresolved capture/restoration evidence; explicit discard is required")
            # Panorama previews nest job trees under the session; remove them bottom-up,
            # still refusing any symlink so deletion never follows an escape.
            files, subdirs, symlink_seen = _enumerate_for_deletion(directory)
            if symlink_seen:
                raise ValueError("session deletion refuses symlinks or nested directories")
            if len(files) + len(subdirs) > MAX_SESSION_ENTRIES:
                raise ValueError("session file count exceeds deletion budget")
            for path in files:
                path.unlink()
            for path in subdirs:  # children precede parents (os.walk topdown=False)
                path.rmdir()
            directory.rmdir()
            return {"id": session_id, "deleted_count": len(files), "discarded_unresolved": unresolved,
                    "scope": "local_session_only", "camera_card_deleted": False}

    def create(self, mode: str, frames: int, *, settings: dict | None = None,
               metadata: dict | None = None, ev_step: float = 2.0,
               interval_s: float = 0.0, timelapse_mode: str = "fixed",
               timelapse_options: dict | None = None, duration_s: float | None = None,
               restore_baseline: dict | None = None, dry_run: bool = False) -> dict:
        """Create a session; ``dry_run`` runs every check and returns the manifest without saving it
        (POST /sessions/meter-plan validates the whole request before its meter photo)."""
        if mode not in MODES:
            raise ValueError("unsupported mode; focus bracketing requires a verified actuator")
        validate_frames(frames, sequence=mode == "timelapse", panorama=mode == "panorama")
        if not math.isfinite(ev_step) or not 0 <= ev_step <= 3:
            raise ValueError("ev_step must be finite and between 0 and 3")
        if mode == "hdr" and (frames - 1) * ev_step / 2 > 6:
            raise ValueError("HDR bracket cannot exceed +/-6 EV")
        if not math.isfinite(interval_s) or not 0 <= interval_s <= 3600:
            raise ValueError("interval_s must be finite and between 0 and 3600")
        from long_exposure_plan import validate_duration
        duration_s = validate_duration(duration_s, mode=mode, interval_s=interval_s)
        from stack_planner import timelapse_config
        deflicker_option = None
        if isinstance(timelapse_options, dict) and "deflicker" in timelapse_options:
            # Deflicker is a render control, not an exposure option; it is validated and stored
            # through the same controls path the UI patches mid-run.
            from timelapse_controls import deflicker_settings
            timelapse_options = dict(timelapse_options)
            deflicker_option = deflicker_settings(timelapse_options.pop("deflicker"))
        elif mode == "timelapse" and timelapse_mode == "holy_grail":
            # On by default for Holy Grail when the request is silent; an explicit
            # {"enabled": false} above keeps it off. Other policies stay off unless asked.
            from timelapse_controls import HOLY_GRAIL_DEFLICKER_DEFAULT, deflicker_settings
            deflicker_option = deflicker_settings(HOLY_GRAIL_DEFLICKER_DEFAULT)
        configuration = timelapse_config(timelapse_mode, timelapse_options)
        if mode != "timelapse" and (timelapse_mode != "fixed" or timelapse_options or deflicker_option):
            raise ValueError("timelapse configuration requires timelapse mode")
        session_id = uuid.uuid4().hex
        directory = self._directory(session_id)
        manifest = {"id": session_id, "schema": 1, "mode": mode, "status": "ready",
                    "created_at": time.time(), "updated_at": time.time(),
                    "settings": settings or {}, "metadata": metadata or {},
                    "interval_s": interval_s, "requested_interval_s": interval_s, "duration_s": duration_s,
                    "timelapse": configuration, "frame_count": frames, "frames": [],
                    "restore": "not_started", "output": None, "error": None}
        if restore_baseline:
            # Settings read before this session existed (a Fixed stack's pre-meter exposure) are
            # written with the manifest itself, so no resumable session ever lacks them.
            manifest.update(restore_baseline=dict(restore_baseline), baseline_saved_at=time.time())
        for index in range(frames):
            manifest["frames"].append({"index": index, "status": "pending",
                                       "ev_offset": (index - (frames - 1) / 2) * ev_step
                                       if mode == "hdr" else 0.0,
                                       "destination": str(directory / f"frame_{index:04d}.jpg")})
        check_manifest_plan(manifest)  # Refused before the session exists, so before any shutter.
        if dry_run:
            return manifest
        directory.mkdir(mode=0o700)
        _write_manifest(directory, manifest)
        if deflicker_option is not None:
            from timelapse_controls import update_controls
            update_controls(self, session_id, {"deflicker": deflicker_option})
            manifest = self.read(session_id)
        return manifest

    def read(self, session_id: str) -> dict:
        directory = self._directory(session_id)
        data = regular_bytes(directory / "manifest.json", LEGACY_MAX_MANIFEST_BYTES)
        result = json.loads(data)
        if (isinstance(result, dict) and result.get("evidence_format") == EVIDENCE_FORMAT
                and len(data) > MAX_MANIFEST_BYTES):
            raise ValueError("session artifact exceeds byte budget or is not regular")
        if not isinstance(result, dict) or result.get("id") != session_id or result.get("schema") != 1:
            raise ValueError("invalid session manifest identity/schema")
        if result.get("mode") not in MODES:
            raise ValueError("invalid session mode")
        validate_frames(result.get("frame_count"), sequence=result["mode"] == "timelapse",
                        panorama=result["mode"] == "panorama")
        if result.get("status") not in {"ready", "running", "merging", "completed", "failed", "cancelled", "needs_reconciliation", "restore_failed", "identity_refused", "precondition_refused"}:
            raise ValueError("invalid session status")
        if not isinstance(result.get("metadata"), dict) or not isinstance(result.get("settings"), dict):
            raise ValueError("invalid session metadata/settings")
        if "timelapse" in result:
            from stack_planner import timelapse_config
            config = result["timelapse"]
            if not isinstance(config, dict):
                raise ValueError("invalid timelapse configuration")
            timelapse_config(config.get("mode"), {k: v for k, v in config.items() if k != "mode"})
        from long_exposure_plan import validate_duration
        validate_duration(result.get("duration_s"), mode=result["mode"])
        interval = result.get("interval_s")
        if type(interval) not in (int, float) or not math.isfinite(interval) or not 0 <= interval <= 3600:
            raise ValueError("invalid session interval")
        frames = result.get("frames")
        if not isinstance(frames, list) or len(frames) != result["frame_count"]:
            raise ValueError("invalid session frame count")
        for index, frame in enumerate(frames):
            expected = str(directory / f"frame_{index:04d}.jpg")
            if (not isinstance(frame, dict) or type(frame.get("index")) is not int or frame["index"] != index
                    or frame.get("destination") != expected
                    or frame.get("status") not in {"pending", "capturing", "ambiguous", "accepted"}):
                raise ValueError("invalid session frame identity/path/status")
            offset = frame.get("ev_offset")
            if type(offset) not in (int, float) or not math.isfinite(offset) or not -6 <= offset <= 6:
                raise ValueError("invalid frame EV offset")
            receipt = frame.get("receipt")
            if frame["status"] == "accepted" and not isinstance(receipt, dict):
                raise ValueError("accepted frame lacks receipt")
            if receipt and (receipt.get("path") != expected or not re.fullmatch(r"[0-9a-f]{64}", str(receipt.get("sha256", "")))):
                raise ValueError("invalid frame receipt identity")
            sidecar = frame.get("evidence_sidecar")
            if sidecar is not None and (frame["status"] != "accepted" or not isinstance(sidecar, dict)
                                        or sidecar.get("file") != evidence_sidecar_name(index)
                                        or not re.fullmatch(r"[0-9a-f]{64}", str(sidecar.get("sha256", "")))):
                raise ValueError("invalid frame evidence sidecar reference")
        for field, name in (("output", "result.jpg"), ("sequence", "sequence.zip"), ("video", "timelapse.mp4")):
            item = result.get(field)
            if field == "video" and item and item.get("status") != "completed":
                continue
            if item and (not isinstance(item, dict) or not isinstance(item.get("receipt"), dict)
                         or item["receipt"].get("path") != str(directory / name)):
                raise ValueError("invalid session output receipt path")
        if "partial_video" in result:
            from timelapse_partial import validate_record
            validate_record(result["partial_video"], directory)
        preview = result.get("preview_video")
        if preview:
            if (not isinstance(preview, dict) or preview.get("status") != "completed"
                    or not re.fullmatch(r"preview_[0-9]{4}\.mp4", str(preview.get("name", "")))
                    or not isinstance(preview.get("receipt"), dict)
                    or preview["receipt"].get("path") != str(directory / preview["name"])):
                raise ValueError("invalid incremental preview identity/path")
        if "stopped_early" in result:
            requested = result.get("requested_frame_count")
            if (result["mode"] not in STOPPABLE_MODES or not isinstance(result["stopped_early"], dict)
                    or type(requested) is not int or requested < result["frame_count"]):
                raise ValueError("invalid early-stop record")
        accepted = sum(frame["status"] == "accepted" for frame in result["frames"])
        result["progress"] = {"accepted": accepted, "total": result["frame_count"],
                              "fraction": accepted / result["frame_count"]}
        _record_delivery_durability(result)  # manifests saved before the rollup existed
        return result

    def list(self) -> list[dict]:
        return [self.read(path.name) for path in sorted(self.root.iterdir())
                if re.fullmatch(r"[0-9a-f]{32}", path.name) and (path / "manifest.json").is_file()]

    def read_detail(self, session_id: str) -> dict:
        """read() with every frame's full ``applied`` and ``transport_receipt`` restored from its
        evidence sidecar: the frame shape from before sidecars, for GET /sessions/{id}. read() and
        list() stay compact. A sidecar that cannot be verified leaves that frame compact with
        ``evidence_error`` instead of failing the whole session."""
        state = self.read(session_id)
        for frame in state["frames"]:
            if frame.get("evidence_sidecar"):
                try:
                    frame.update(self.frame_evidence(state, frame["index"]))
                except (OSError, ValueError) as exc:
                    frame["evidence_error"] = f"{type(exc).__name__}: {exc}"
        return state

    def frame_evidence(self, state: dict, index: int) -> dict:
        """One frame's full ``applied`` settings and camera ``transport_receipt``.

        ``state`` comes from read(). An accepted frame keeps them in its evidence sidecar, verified
        here against the manifest's hash and the frame's JPEG hash; a manifest written before
        sidecars, or a frame not accepted, keeps them inline and they are returned as stored.
        """
        frame = state["frames"][index]
        reference = frame.get("evidence_sidecar")
        if not reference:
            return {key: frame[key] for key in ("applied", "transport_receipt") if key in frame}
        data = regular_bytes(self._directory(state["id"]) / evidence_sidecar_name(index), MAX_EVIDENCE_SIDECAR_BYTES)
        if hashlib.sha256(data).hexdigest() != reference["sha256"]:
            raise ValueError("frame evidence sidecar changed after acceptance")
        payload = json.loads(data)
        if payload.get("index") != index or payload.get("frame_sha256") != frame["receipt"]["sha256"]:
            raise ValueError("frame evidence sidecar belongs to another frame")
        return {key: payload[key] for key in ("applied", "transport_receipt") if key in payload}

    def cancel(self, session_id: str, *, split_control: bool = False) -> dict:
        with self._control(session_id):
            directory = self._directory(session_id)
            state = self.read(session_id)  # Delete may have finished while the short control lock was pending.
            # run() enters merging under this lock after its last Cancel check, and the status
            # stays merging until the outcome is saved: a Cancel here could only be ignored.
            refuse_cancel(state, split_control=split_control)
            descriptor = os.open(directory / "cancel", os.O_WRONLY | os.O_CREAT | os.O_NOFOLLOW, 0o600)
            with os.fdopen(descriptor, "wb") as marker:
                if not stat.S_ISREG(os.fstat(marker.fileno()).st_mode):
                    raise ValueError("cancel marker must be regular")
        return {"id": session_id, "cancel_requested": True,
                "note": "An exposure already in progress completes before cancellation."}

    def stop(self, session_id: str) -> dict:
        """Request a clean stop at the next frame boundary: no further shutter, and the frames
        accepted so far are merged into the result. Never a failure and never a reshoot."""
        with self._control(session_id):
            directory = self._directory(session_id)
            state = self.read(session_id)
            if state["mode"] not in STOPPABLE_MODES:
                raise ValueError("Stop when clean applies to crowd removal sessions only; use Cancel")
            if state["status"] != "running":
                raise ValueError(f"Stop when clean needs a capturing session; this one is {state['status']}")
            descriptor = os.open(directory / "stop", os.O_WRONLY | os.O_CREAT | os.O_NOFOLLOW, 0o600)
            with os.fdopen(descriptor, "wb") as marker:
                if not stat.S_ISREG(os.fstat(marker.fileno()).st_mode):
                    raise ValueError("stop marker must be regular")
        return {"id": session_id, "stop_requested": True,
                "note": ("An exposure already in progress completes; then the frames accepted so far are "
                         f"merged. With fewer than {MIN_STOP_MERGE_FRAMES} accepted frames the session is "
                         "cancelled instead and can be resumed.")}

    @staticmethod
    def _stop_requested(directory: Path, state: dict) -> bool:
        return state["mode"] in STOPPABLE_MODES and (
            bool(state.get("stop_request")) or (directory / "stop").exists())

    @staticmethod
    def _stop_early(directory: Path, state: dict) -> None:
        """Truncate the manifest to its accepted prefix so the merge sees exactly those frames.

        Pending frames never fired, so dropping them loses nothing; the requested count and the
        reason stay on record. Too few frames to merge leaves a resumable cancellation instead.
        """
        automatic = state.pop("stop_request", None)
        # A manual Stop marker takes precedence over an automatic request recorded in the same window.
        manual = (directory / "stop").exists() or not automatic
        request = {"reason": "user", "requested_at": time.time()} if manual else automatic
        accepted = [frame for frame in state["frames"] if frame["status"] == "accepted"]
        remaining = state["frames"][len(accepted):]
        if (any(frame["index"] != index for index, frame in enumerate(accepted))
                or any(frame["status"] != "pending" for frame in remaining)):
            raise ValueError("stop requires an accepted prefix followed only by pending frames")
        (directory / "stop").unlink(missing_ok=True)
        if len(accepted) < MIN_STOP_MERGE_FRAMES:
            state["status"] = "cancelled"
            state["stop_outcome"] = {**request, "outcome": "cancelled_too_few_frames",
                                     "accepted_frames": len(accepted)}
            return
        state["stopped_early"] = {**request, "outcome": "merged_accepted_frames",
                                  "accepted_frames": len(accepted),
                                  "requested_frames": state["frame_count"],
                                  "stopped_at": time.time()}
        state["requested_frame_count"] = state["frame_count"]
        state["frames"] = accepted
        state["frame_count"] = len(accepted)

    def resume(self, session_id: str) -> dict:
        with self.operation(session_id):
            return self._resume_locked(session_id)

    def _resume_locked(self, session_id: str) -> dict:
        """Explicitly clear a cancellation before run(); ambiguous shutters stay blocked."""
        directory = self._directory(session_id)
        with (directory / "runner.lock").open("a") as lock:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as exc:
                raise RuntimeError("session is already running") from exc
            # Under the lock cancel() takes: a Cancel lands wholly before this Resume (which clears
            # it) or after it (and is honoured by the run), never acknowledged and then erased.
            with self._control(session_id):
                state = self.read(session_id)
                if any(frame["status"] in ("capturing", "ambiguous") for frame in state["frames"]):
                    raise RuntimeError("ambiguous shutter requires reconciliation; automatic recapture is blocked")
                if state["status"] not in ("cancelled", "failed", "restore_failed", "ready", "identity_refused",
                                           "precondition_refused"):
                    raise ValueError("session is not resumable")
                (directory / "cancel").unlink(missing_ok=True)
                (directory / "stop").unlink(missing_ok=True)
                state.pop("stop_request", None)
                state.update(status="ready", error=None, updated_at=time.time())
                _write_manifest(directory, state)
            return self.read(session_id)

    def run(self, session_id: str, capture_frame: Callable, merge_frames: Callable | None = None,
            *, restore_settings: Callable | None = None,
            prepare_frame: Callable | None = None,
            after_frame: Callable | None = None,
            progress: Callable | None = None,
            queue_deferred_merge: Callable | None = None) -> dict:
        """Run/resume pending frames; callbacks are synchronous under the caller's camera lock.

        capture_frame(index, frame_spec, destination) -> exact destination path.
        prepare_frame(index, frame_spec) -> JSON metadata (optional).
        merge_frames(paths, destination) -> output path or {out_path, ...} (optional).
        restore_settings() runs in finally, including cancellation and any failure.
        progress(state) updates run bookkeeping only; it is persisted before each frame's
        preparation, before every shutter intent, after each accepted frame's callback and
        every PROGRESS_CHECKPOINT_S while waiting between frames.
        Without a merger, completion means a captured sequence, never a merged image.
        queue_deferred_merge(state) (split runtime, no merger) records the queued heavy job on a
        completed state; the session is merging from the last Cancel check until that save.
        """
        directory = self._directory(session_id)
        with (directory / "runner.lock").open("a") as lock:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as exc:
                raise RuntimeError("session is already running") from exc
            state = self.read(session_id)
            def save():
                _record_delivery_durability(state)
                state["updated_at"] = time.time()
                _write_manifest(directory, state)
            if state["status"] == "completed":
                return state
            result_follows = bool(merge_frames or queue_deferred_merge)  # in-process merge or a queued one
            if any(frame["status"] in ("capturing", "ambiguous") for frame in state["frames"]):
                state.update(status="needs_reconciliation", error="A shutter may have fired; automatic recapture is blocked.")
                save()
                return state
            if state.get("duration_s") is not None and not state.get("long_exposure_plan"):
                raise ValueError("Duration session needs confirmed-baseline planning before capture")
            state.update(status="running", error=None, restore="pending")
            state.pop("identity_refusal", None)
            state.pop("restore_identity_refusal", None)
            state.pop("precondition_refusal", None)
            save()
            try:
                # Revalidate accepted files before any new camera command.
                source_total = 0
                for frame in state["frames"]:
                    if frame["status"] == "accepted":
                        receipt = frame_receipt(frame["destination"])
                        if receipt["sha256"] != frame["receipt"]["sha256"]:
                            raise ValueError("accepted frame changed; refusing resume")
                        source_total += receipt["bytes"]
                previous_capture_start = None
                for frame in state["frames"]:
                    if frame["status"] == "accepted":
                        continue
                    if (directory / "cancel").exists():
                        state["status"] = "cancelled"
                        break
                    if self._stop_requested(directory, state):
                        break  # A clean stop at a frame boundary: no further shutter.
                    check_manifest_plan(state)  # Before any camera command for this frame.
                    started = time.monotonic()
                    destination = Path(frame["destination"])
                    if destination.exists():
                        raise ValueError("unreconciled destination already exists; refusing recapture")
                    if progress and prepare_frame:
                        progress(state)  # Durable before preparation's camera commands, too.
                        save()
                    if prepare_frame:
                        frame["applied"] = prepare_frame(frame["index"], dict(frame))
                    if progress:
                        progress(state)  # Before the frame is marked capturing; saved with the intent.
                    # The last Stop check and the durable capture intent happen under the lock that
                    # stop() takes: a Stop acknowledged before this point is seen here and fires
                    # nothing; one acknowledged after it finds this exposure already in progress.
                    with self._control(session_id):
                        if self._stop_requested(directory, state):
                            frame.pop("applied", None)
                            break  # Stop arrived during preparation or a callback: no shutter.
                        frame.update(status="capturing", capture_started_at=time.time())
                        capture_start = time.monotonic()
                        if previous_capture_start is not None:
                            frame["actual_interval_s"] = capture_start - previous_capture_start
                            frame["actual_interval_source"] = "monotonic"
                        elif frame["index"] > 0:
                            previous = state["frames"][frame["index"] - 1].get("capture_started_at")
                            if previous is not None and frame["capture_started_at"] >= previous:
                                frame["actual_interval_s"] = frame["capture_started_at"] - previous
                                frame["actual_interval_source"] = "wall_clock_after_resume"
                            else:
                                frame["actual_interval_s"] = None
                                frame["actual_interval_source"] = "unknown_clock_changed_after_resume"
                        previous_capture_start = capture_start
                        save()  # Durable intent precedes the irreversible physical shutter.
                    try:
                        capture_spec = dict(frame)
                        try:
                            captured = capture_frame(frame["index"], capture_spec, str(destination))
                        finally:
                            if isinstance(capture_spec.get("transport_receipt"), dict):
                                frame["transport_receipt"] = capture_spec["transport_receipt"]
                        if not captured or Path(captured).resolve() != destination.resolve():
                            raise ValueError("capture must return the exact new destination; latest-card substitution is forbidden")
                        receipt = frame_receipt(destination)
                        if source_total + receipt["bytes"] > MAX_SESSION_SOURCE_BYTES:
                            raise ValueError("session source-byte budget exceeded")
                    except BaseException as exc:
                        if getattr(exc, "identity_refused", False) is True:
                            # A camera identity refusal fired nothing: the frame stays pending and
                            # the session records a distinct, resumable refusal, not a reconciliation.
                            frame["status"] = "pending"
                            frame.pop("capture_started_at", None)
                            state["status"] = "identity_refused"
                            state["identity_refusal"] = exc.as_dict()
                        elif getattr(exc, "precondition_refused", False) is True:
                            # Refused by code that runs before any trigger (sonyremote
                            # CapturePreconditionError): nothing fired. The frame stays pending
                            # and the session is resumable; this run never retries it.
                            frame["status"] = "pending"
                            frame.pop("capture_started_at", None)
                            state["status"] = "precondition_refused"
                            state["precondition_refusal"] = {"detail": str(exc), "frame": frame["index"],
                                                             "at": time.time(), "may_have_fired": False}
                        else:
                            frame["status"] = "ambiguous"
                            state["status"] = "needs_reconciliation"
                        save()
                        raise
                    source_total += receipt["bytes"]
                    frame.update(status="accepted", receipt=receipt, capture_s=time.monotonic() - started,
                                 capture_pipeline_s=time.monotonic() - capture_start)
                    _store_frame_evidence(directory, frame)
                    _record_cadence(state)  # Overrun is visible before the next shutter, never acted on.
                    save()
                    if after_frame or progress:
                        # The accepted receipt is durable before optional adaptation/rendering.
                        # Callback owns only pending suffix/timing/derived preview metadata.
                        if after_frame:
                            after_frame(state)
                        if progress:
                            progress(state)  # After boundary work, so the wait starts from a fresh record.
                        save()
                    wait_until = started + state["interval_s"]
                    if frame["index"] + 1 < state["frame_count"]:
                        checkpoint_at = time.monotonic() + PROGRESS_CHECKPOINT_S
                        while (time.monotonic() < wait_until and not (directory / "cancel").exists()
                               and not self._stop_requested(directory, state)):
                            time.sleep(max(0.0, min(0.1, wait_until - time.monotonic())))
                            if progress and time.monotonic() >= checkpoint_at:
                                progress(state)
                                save()
                                checkpoint_at = time.monotonic() + PROGRESS_CHECKPOINT_S
                if (directory / "cancel").exists():
                    state["status"] = "cancelled"
                elif self._stop_requested(directory, state):
                    # Reason selection, marker removal and the resulting status are committed under
                    # the lock stop() takes: a racing Stop is either counted here as the user's, or
                    # waits, finds the session merging or cancelled and is refused. It is never
                    # acknowledged and then recorded as automatic.
                    with self._control(session_id):
                        if (directory / "cancel").exists():
                            # A Cancel acknowledged since the check above wins: nothing is merged,
                            # accepted frames are kept and the session resumes like any cancel.
                            state["status"] = "cancelled"
                        elif any(frame["status"] != "accepted" for frame in state["frames"]):
                            self._stop_early(directory, state)
                        else:  # Every requested frame was already accepted: an ordinary completion.
                            state.pop("stop_request", None)
                            (directory / "stop").unlink(missing_ok=True)
                        if state["status"] != "cancelled" and result_follows:
                            state["status"] = "merging"
                        save()
                if state["status"] != "cancelled" and result_follows:
                    # The last Cancel check and the durable merging status happen under the lock
                    # cancel() takes: a Cancel acknowledged before this point stops the session
                    # here (no merge); one after it finds the session merging and is refused.
                    # The split runtime's deferred merge is entered the same way.
                    with self._control(session_id):
                        state["status"] = "cancelled" if (directory / "cancel").exists() else "merging"
                        save()
                if state["status"] != "cancelled":
                    paths = [frame["destination"] for frame in state["frames"]]
                    if merge_frames:
                        output = merge_frames(paths, str(directory / "result.jpg"))
                        output_path = output.get("out_path") if isinstance(output, dict) else output
                        if not output_path or Path(output_path).resolve() != (directory / "result.jpg").resolve():
                            raise ValueError("merge did not produce the requested output")
                        receipt = frame_receipt(output_path)
                        verify_anchor_output(output, receipt,
                                             [(frame.get("receipt") or {}).get("sha256") for frame in state["frames"]])
                        state["output"] = {"receipt": receipt,
                                           "details": output if isinstance(output, dict) else {}}
                    state["status"] = "completed"
                    state.setdefault("completed_at", time.time())
                    (directory / "stop").unlink(missing_ok=True)
            except Exception as exc:
                if state["status"] not in ("needs_reconciliation", "identity_refused", "precondition_refused"):
                    state["status"] = "failed"
                state["error"] = f"{type(exc).__name__}: {exc}"
            finally:
                if restore_settings:
                    try:
                        restore_settings()
                        state["restore"] = "completed"
                    except Exception as exc:
                        state["restore"] = "failed"
                        state["restore_error"] = f"{type(exc).__name__}: {exc}"
                        if getattr(exc, "identity_refused", False) is True:
                            state["restore_identity_refusal"] = exc.as_dict()
                        if state["status"] == "completed":
                            state["status"] = "restore_failed"
                else:
                    state["restore"] = "not_requested"
                if state.get("long_exposure_plan"):
                    from long_exposure_plan import timing_receipt
                    state["long_exposure_timing"] = timing_receipt(state["long_exposure_plan"], state["frames"])
                _record_cadence(state)
                if queue_deferred_merge and state["status"] == "completed":
                    # Completion and the queued heavy job land in one write: never completed-and-unqueued.
                    queue_deferred_merge(state)
                save()
            return self.read(session_id)


def panorama_receipt_summary(receipt: dict) -> dict:
    """Portable subset of a build_panorama receipt for the session sidecar/manifest."""
    return {"job_id": receipt.get("job_id"), "algorithm": receipt.get("algorithm"),
            "projection": receipt.get("projection"),
            "horizontal_fov_deg": receipt.get("horizontal_fov_deg"),
            "rotation_deg": receipt.get("rotation_deg"), "autocrop": receipt.get("autocrop"),
            "level_horizon": receipt.get("level_horizon"), "horizon": receipt.get("horizon"),
            "crop": receipt.get("crop"), "crop_xyxy": receipt.get("crop_xyxy"),
            "layout": receipt.get("layout"),
            "output": receipt.get("output"), "coverage": receipt.get("coverage"),
            "alignment": receipt.get("alignment"), "elapsed_seconds": receipt.get("elapsed_seconds"),
            "capture_performed": receipt.get("capture_performed", False)}


def _merge_panorama(paths: list[str], out_path: str, options: dict) -> dict:
    """Stitch the accepted frames with the supervised panorama engine into result.jpg.

    build_panorama owns its own private job tree; we run it in a scratch directory
    outside the session, then atomically place only the finished JPEG as result.jpg
    so the session directory stays a flat set of publishable artifacts.
    """
    import shutil
    import tempfile
    from panorama import build_panorama
    out_path = Path(out_path)
    scratch = Path(tempfile.mkdtemp(prefix="camera_pano_merge_"))
    try:
        receipt = build_panorama(list(paths), scratch, **options)
        produced = scratch / receipt["job_id"] / receipt["output"]["filename"]
        data = regular_bytes(produced, MAX_SESSION_SOURCE_BYTES)
        pending = out_path.with_name(out_path.name + ".pending")
        pending.unlink(missing_ok=True)
        descriptor = os.open(pending, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(pending, out_path)
    finally:
        shutil.rmtree(scratch, ignore_errors=True)
    return {"out_path": str(out_path), "mode": "panorama", "frames_merged": len(paths),
            "panorama": panorama_receipt_summary(receipt)}


def verify_anchor_output(details, receipt: dict, frame_sha256: list) -> None:
    """An HDR output published as the anchor bracket's own file must be exactly the accepted frame:
    the file is read again after the merge, so a frame replaced meanwhile must fail the merge,
    never become the photograph. ``frame_sha256`` is the accepted receipts in merge-input order."""
    hdr = details.get("hdr") if isinstance(details, dict) else None
    if not isinstance(hdr, dict) or hdr.get("output_source") != "anchor_bracket_file":
        return
    index = hdr.get("anchor_index")
    if (not isinstance(index, int) or not 0 <= index < len(frame_sha256)
            or frame_sha256[index] != receipt["sha256"]):
        raise ValueError("the published anchor bracket is not the accepted frame")


def _unregistered_anchor_bytes(paths: list[str], report: dict, shape: tuple) -> tuple[bytes, float] | None:
    """The anchor bracket's own JPEG bytes when a nothing-to-recover verdict also holds for them.

    The verdict is measured on the anchor as registered to the first frame, and registration can
    shift a clipped edge out of view; the file is published only if its own pixels have the same
    dimensions and also clip less than the threshold. Returns the bytes and their own clipped
    share; ``None`` means keep the measured frame."""
    import cv2
    import numpy as np
    import stacking
    from image_safety import MAX_IMAGE_SOURCE_BYTES
    if report.get("recovery") != "nothing_to_recover":
        return None
    anchor = Path(paths[report["anchor_index"]])
    if anchor.suffix.lower() not in (".jpg", ".jpeg"):
        return None
    data = regular_bytes(anchor, MAX_IMAGE_SOURCE_BYTES)
    if not data.startswith(b"\xff\xd8") or image_size_from_bytes(data) != (shape[1], shape[0]):
        return None
    pixels = cv2.imdecode(np.frombuffer(data, dtype=np.uint8), cv2.IMREAD_COLOR)
    if pixels is None or pixels.shape != tuple(shape):
        return None
    lightness = cv2.cvtColor(pixels, cv2.COLOR_BGR2LAB)[:, :, 0]
    clipped = float(np.count_nonzero(lightness > stacking.HDR_RECOVERY_HIGHLIGHT)) / lightness.size
    del pixels, lightness
    return (data, clipped) if clipped < stacking.HDR_NOTHING_TO_RECOVER_FRACTION else None


def merge_session_frames(mode: str, paths: list[str], out_path: str, *,
                         panorama_options: dict | None = None,
                         night_options: dict | None = None) -> dict:
    """Bounded JPEG merge adapter; panorama retains its measured output dimensions.

    ``night_options`` (mode ``night`` only): ``{"deghost": bool}``, default true.
    """
    if mode == "panorama":
        return _merge_panorama(paths, out_path, panorama_options or {"projection": "cylindrical"})
    import cv2
    import stacking
    mergers = {"hdr": stacking.hdr_merge, "long_exposure": stacking.long_exposure}
    if mode == "crowd_remove":
        return _merge_crowd_remove(paths, out_path)
    if mode == "night":
        return _merge_night(paths, out_path, night_options or {})
    if mode == "hdr":
        # The report says whether the metered frame had anything to recover; below the
        # threshold it IS the photograph, and the evidence must say so.
        image, report = stacking.hdr_merge_with_report(paths)
        original = _unregistered_anchor_bytes(paths, report, image.shape)
        if original is not None:
            # Nothing to recover: the photograph is the anchor bracket itself, so publish the
            # camera's own file (its exact bytes and EXIF), never a re-encode of its pixels.
            data, clipped = original
            Path(out_path).write_bytes(data)
            # The evidence describes the published pixels: the share measured on the original,
            # with the registered frame's share kept beside it.
            registered = report["clipped_fraction"]
            report["summary"] = report["summary"].replace(f"clipped {registered * 100:.2f} %",
                                                          f"clipped {clipped * 100:.2f} %")
            report.update(clipped_fraction=clipped, registered_clipped_fraction=registered,
                          output_source="anchor_bracket_file")
        else:
            if not cv2.imwrite(out_path, image):
                raise OSError("could not write merged image")
            report["output_source"] = "merged"
        # The motion repair's evidence (#92) travels beside the verdict as ``hdr_motion``.
        motion = {key: value for key, value in report.items() if key.startswith("motion_")}
        return {"out_path": out_path, "mode": mode, "frames_merged": len(paths),
                "hdr": report, "hdr_motion": motion, "summary": report["summary"]}
    if mode not in mergers:
        raise ValueError("mode has no supported image merger")
    image = mergers[mode](paths)
    if not cv2.imwrite(out_path, image):
        raise OSError("could not write merged image")
    return {"out_path": out_path, "mode": mode, "frames_merged": len(paths)}


def _merge_night(paths: list[str], out_path: str, options: dict) -> dict:
    """Aligned average with motion-aware deghosting (default on): pixels that moved
    between frames come from the middle frame alone, so a subject that shifted across
    the seconds-apart frames appears once instead of as a translucent ghost. Those
    pixels keep single-frame noise. ``motion_mask.png`` (255 = fully deghosted)
    persists beside the output so the manifest records what was not averaged."""
    import cv2
    import stacking
    deghost = bool(options.get("deghost", True))
    image, report = stacking.long_exposure_with_report(paths, deghost=deghost)
    if not cv2.imwrite(out_path, image):
        raise OSError("could not write merged image")
    summary = {"out_path": out_path, "mode": "night", "frames_merged": len(paths),
               "deghost": report["deghost"], "moving_fraction": round(report["moving_fraction"], 6),
               "reference_index": report["reference_index"]}
    if report["mask"] is not None:
        maps_dir = Path(out_path).parent
        if not cv2.imwrite(str(maps_dir / "motion_mask.png"), report["mask"]):
            raise OSError("could not write motion mask")
        summary.update({"maps_dir": str(maps_dir), "maps": ["motion_mask.png"]})
    return summary


def _crowd_clean_plate(paths: list[str], work: Path) -> dict:
    """Clean-plate estimate plus the 25% visibility classification, shared by the final merge
    and the live preview so both report exactly the same rule on the same frames."""
    import cv2
    import numpy as np
    import stacking
    import crowd_auto_mask
    # The whole pipeline (median, registered staging, estimator, recovery, maps) is checked
    # against the per-merge memory budget before any pixel is decoded.
    stacking._validate_sources(paths, mode="crowd_auto")
    aligned_dir = work / "aligned"
    aligned_dir.mkdir(parents=True, exist_ok=True)
    # The estimator assumes a registered grid; feed it the same registered frames the median saw.
    aligned = []
    with stacking._staged_stack(paths) as (files, shape, _histograms):
        for index, raw in enumerate(files):
            frame = np.memmap(raw, dtype=np.uint8, mode="r", shape=shape)
            target = aligned_dir / f"{index:02d}.jpg"
            if not cv2.imwrite(str(target), np.asarray(frame), [cv2.IMWRITE_JPEG_QUALITY, 95]):
                raise OSError("could not stage registered frame")
            del frame
            aligned.append(str(target))
    # Fill only where the background cleared Arsenal's visibility floor (>= 25% of frames); the
    # never-revealed rest keeps the temporal-median fallback untouched (never averaged or invented).
    visibility_floor = crowd_auto_mask.min_frames_for_visibility(len(paths))
    outcome = crowd_auto_mask.recover_background_auto(aligned, work, minimum_observations=visibility_floor)
    result = outcome["result"]
    # The median fallback is computed only now so it is not held through the estimator, and
    # recovered pixels are copied into it in place rather than into a third full frame.
    image = stacking.crowd_remove(paths)
    np.copyto(image, result.image, where=(result.valid_mask == 255)[:, :, None])
    # Counts stay uint8 (at most 16 frames); the maps below are the same values the uint32/int64
    # arithmetic produced, without its 4-8 bytes per pixel of temporaries.
    support = np.asarray(result.support_count, dtype=np.uint8)
    not_cleared = support < visibility_floor  # background visible in fewer than 25% of frames
    return {"image": image, "support": support, "not_cleared": not_cleared,
            "visibility_floor": visibility_floor, "outcome": outcome}


def _replace_image(destination: Path, image, params=()) -> None:
    """Write via a freshly created pending file and rename it over ``destination``: an existing
    symlink at either name is replaced, never followed."""
    import cv2
    destination = Path(destination)
    pending = destination.with_name(destination.stem + ".pending" + destination.suffix)
    pending.unlink(missing_ok=True)
    if not cv2.imwrite(str(pending), image, list(params)):
        raise OSError(f"could not write {destination.name}")
    os.replace(pending, destination)


def crowd_preview_source(source: str | Path, destination: str | Path, *, edge: int = CROWD_PREVIEW_EDGE) -> Path:
    """Write a lossless copy of one accepted frame with its long edge at most ``edge`` pixels."""
    import cv2
    import stacking
    image = stacking._read_one(source)
    height, width = image.shape[:2]
    scale = min(1.0, edge / max(height, width))
    if scale < 1.0:
        image = cv2.resize(image, (max(1, round(width * scale)), max(1, round(height * scale))),
                           interpolation=cv2.INTER_AREA)
    destination = Path(destination)
    _replace_image(destination, image)
    return destination


# Not-cleared regions are tinted this BGR colour (magenta) at half strength in the live preview.
_CROWD_PREVIEW_TINT = (255, 0, 255)


def crowd_preview(paths: list[str], out_path: str | Path, mask_path: str | Path) -> dict:
    """Live clean-plate estimate from the accepted frames so far (already downscaled copies).

    Runs the same estimator and 25% rule as the final merge; regions not yet cleared are tinted
    in the JPEG and written as a binary mask (255 = not cleared) to ``mask_path``. ``cleared_share`` is the fraction of preview pixels whose background was judged
    visible in at least 25% of these frames. It is measured on the downscaled copies, so the
    final full-resolution merge reports its own share. The floor rises with the frame count
    (1 of 4, 2 of 5..8), so the share can dip by a step when a frame is added.

    ``regions`` is the auto-stop's spatial signal (``crowd_clear_regions``): whether every
    region that was ever occupied has been seen clear again in the newest frames. The share is
    for display; an automatic stop also needs ``regions["all_seen_clear"]``.
    """
    import tempfile
    import cv2
    import numpy as np
    import crowd_auto_mask
    import crowd_clear_regions
    import stacking
    with tempfile.TemporaryDirectory(prefix="camera-crowd-preview-") as scratch:
        plate = _crowd_clean_plate(list(paths), Path(scratch))
        # The auto-stop's spatial signal reads the same registered frames the plate was built from.
        registered = [stacking._read_one(Path(scratch) / "aligned" / f"{index:02d}.jpg")
                      for index in range(len(paths))]
        regions = crowd_clear_regions.seen_clear_regions(registered, plate["image"])["report"]
        del registered
    mask = plate["not_cleared"]
    image = plate["image"].astype(np.float32)
    image[mask] = 0.5 * image[mask] + 0.5 * np.array(_CROWD_PREVIEW_TINT, np.float32)
    _replace_image(Path(out_path), np.clip(image, 0, 255).astype(np.uint8), [cv2.IMWRITE_JPEG_QUALITY, 85])
    _replace_image(Path(mask_path), mask.astype(np.uint8) * np.uint8(255))
    cleared = float(np.count_nonzero(~mask) / mask.size)
    return {"accepted_frames": len(paths), "cleared_share": cleared, "not_cleared_share": 1.0 - cleared,
            "visibility_floor_frames": plate["visibility_floor"],
            "min_background_visible_fraction": crowd_auto_mask.MIN_BACKGROUND_VISIBLE_FRACTION,
            "background_estimate": plate["outcome"]["status"],
            "width": int(mask.shape[1]), "height": int(mask.shape[0]),
            "static_subject_limit": crowd_auto_mask.STATIC_SUBJECT_LIMIT, "regions": regions}


def _merge_crowd_remove(paths: list[str], out_path: str) -> dict:
    """Automatic per-pixel background estimate; the plain temporal median fills pixels the
    estimator could not label. Support/never-observed maps persist beside the output."""
    import cv2
    import numpy as np
    import stacking
    import crowd_auto_mask
    if len(paths) > CROWD_AUTO_MAX_FRAMES:
        fallback = stacking.crowd_remove(paths)
        if not cv2.imwrite(out_path, fallback):
            raise OSError("could not write merged image")
        return {"out_path": out_path, "mode": "crowd_remove", "frames_merged": len(paths),
                "background_estimate": "auto_mask_skipped_frame_budget",
                "reason": f"automatic labeling is bounded to {CROWD_AUTO_MAX_FRAMES} frames; temporal median used"}
    work = Path(out_path).parent / "crowd_auto"
    plate = _crowd_clean_plate(paths, work)
    outcome, support, not_cleared = plate["outcome"], plate["support"], plate["not_cleared"]
    if not cv2.imwrite(out_path, plate["image"]):
        raise OSError("could not write merged image")
    cv2.imwrite(str(work / "support_count.png"),
                (support.astype(np.uint16) * 255 // max(len(paths), 1)).astype(np.uint8))
    cv2.imwrite(str(work / "never_observed.png"), np.where(support == 0, np.uint8(255), np.uint8(0)))
    cv2.imwrite(str(work / "not_cleared.png"), not_cleared.astype(np.uint8) * np.uint8(255))
    not_cleared_fraction = float(not_cleared.mean())
    revealed_fraction = float((~not_cleared).mean())
    threshold_pct = int(round(crowd_auto_mask.MIN_BACKGROUND_VISIBLE_FRACTION * 100))
    summary = (f"{not_cleared_fraction * 100:.0f}% of the frame could not be cleared "
               f"(background visible in fewer than {threshold_pct}% of frames); "
               f"those regions keep the temporal-median fallback and are marked in not_cleared.png. "
               f"{crowd_auto_mask.STATIC_SUBJECT_LIMIT}")
    return {"out_path": out_path, "mode": "crowd_remove", "frames_merged": len(paths),
            "background_estimate": outcome["status"],
            "low_confidence_fraction": outcome["low_confidence_fraction"],
            "revealed_fraction": revealed_fraction,
            "not_cleared_fraction": not_cleared_fraction,
            "min_background_visible_fraction": crowd_auto_mask.MIN_BACKGROUND_VISIBLE_FRACTION,
            "summary": summary,
            "estimator": outcome["estimator"], "maps_dir": str(work),
            "maps": ["support_count.png", "never_observed.png", "not_cleared.png"]}
