"""Partial timelapse movie from the accepted frames of a session blocked by an ambiguous frame.

Owner decision (2026-10-02, "Yes, encode as partial"): a fixed, Holy Grail or SmartLapse timelapse
left in ``needs_reconciliation`` may have its accepted frames encoded into a movie marked partial.
The encode reads only hash-verified accepted originals, opens no camera and fires nothing; the
ambiguous (or in-flight ``capturing``) frame is never used and the session keeps its status.

This module imports the encoder and deflicker lazily: the split runtime's control role queues the
work and must never load them (``runtime_roles.CONTROL_ABSENT_MODULES``).
"""
from __future__ import annotations

import hashlib
import os
import shutil
import time
from pathlib import Path

PARTIAL_NAME = "timelapse_partial.mp4"
RECEIPT_NAME = "timelapse_partial.json"
PENDING_NAME = "timelapse_partial.pending.mp4"
WORK_DIR_NAME = "partial_frames"
MAX_VIDEO_BYTES = 128 * 1024 * 1024
RECORD_STATUSES = ("queued", "running", "completed", "unavailable", "failed")
NOTE = ("Partial movie: only the accepted frames are used; the ambiguous frame is not used, "
        "nothing was shot and the session stays blocked (needs_reconciliation).")


class PartialRefused(ValueError):
    """The session is not a blocked timelapse with at least two accepted frames."""

    def __init__(self, reason: str, detail: str):
        super().__init__(detail)
        self.reason = reason

    def as_dict(self) -> dict:
        return {"reason": self.reason, "detail": str(self)}


def plan(state: dict) -> dict:
    """Which frames a partial movie uses, or PartialRefused. Reads the manifest only."""
    if state.get("mode") != "timelapse":
        raise PartialRefused("not_timelapse", f"Only a timelapse session can be encoded as a partial movie; "
                                              f"this is a {state.get('mode')} session.")
    # Every exposure policy (fixed, auto_custom, holy_grail; SmartLapse is a frame plan on top of
    # one) renders the same way; read() has already validated the configuration.
    config = state.get("timelapse") or {}
    status = state.get("status")
    if status != "needs_reconciliation":
        raise PartialRefused("status", "A partial movie is made only for a timelapse blocked by an ambiguous "
                                       f"frame (needs_reconciliation); this session is {status}.")
    frames = sorted(state.get("frames") or [], key=lambda frame: frame["index"])
    ambiguous = [frame["index"] for frame in frames if frame["status"] == "ambiguous"]
    capturing = [frame["index"] for frame in frames if frame["status"] == "capturing"]
    if not ambiguous and not capturing:
        raise PartialRefused("no_ambiguous_frame", "This session records no ambiguous frame; nothing to encode partially.")
    accepted = [frame for frame in frames if frame["status"] == "accepted"]
    if len(accepted) < 2:
        raise PartialRefused("too_few_frames", f"A partial movie needs at least 2 accepted frames; "
                                               f"this session has {len(accepted)}.")
    return {"policy": config.get("mode", "fixed"), "fps": config.get("fps", 24), "frames": accepted,
            "ambiguous_indices": ambiguous, "capturing_indices": capturing,
            "pending_count": sum(frame["status"] == "pending" for frame in frames)}


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def remove_work_dir(path: Path, *, limit: int = 1024) -> None:
    """Bounded removal of the private render folder; never follows a symlink."""
    path = Path(path)
    if path.is_symlink():
        path.unlink()
        return
    if not path.is_dir():
        return
    count = 0
    for root, dirnames, filenames in os.walk(path, topdown=False, followlinks=False):
        root_path = Path(root)
        for name in filenames:
            count += 1
            if count > limit:
                raise ValueError("partial render folder exceeds its removal budget")
            (root_path / name).unlink(missing_ok=True)
        for name in dirnames:
            entry = root_path / name
            entry.unlink(missing_ok=True) if entry.is_symlink() else entry.rmdir()
    path.rmdir()


def clean_stale(work_dir: Path, pending: Path) -> None:
    """Remove what an interrupted encode left behind: the render folder and the unfinished movie."""
    remove_work_dir(work_dir)
    Path(pending).unlink(missing_ok=True)


def _deflicker(sources: list[Path], control: dict, out_dir: Path) -> tuple[list[Path], dict]:
    import deflicker
    luma = deflicker.luminance_series(sources)
    gains = deflicker.deflicker_gains(luma, window=control["window"], max_gain_stops=control["max_gain_stops"])
    produced = deflicker.apply_gains_to_frames(sources, gains, out_dir)
    return produced, {"window": control["window"], "max_gain_stops": control["max_gain_stops"],
                      "gains": gains, "frame_count": len(gains),
                      "frames": deflicker.frame_receipts(sources, produced, gains)}


def encode(sources: list[dict], *, fps, deflicker_control, work_dir: Path, pending: Path,
           destination: Path, tools=None) -> dict:
    """Encode ``sources`` (``{"index", "path", "sha256"}``, index order) to ``destination``.

    The caller has verified each source against its accepted receipt. Every input is re-hashed in
    the private render folder before the encode, so the movie is made from exactly those bytes.
    Deflicker follows the completed path: corrected copies when enabled, the untouched originals
    (recorded as ``deflicker_error``) when it fails. Raises EncoderUnavailable before any copy is
    written when ffmpeg/ffprobe do not execute.
    """
    from timelapse_video import EncoderUnavailable, probe_encoder, render_h264
    work_dir, pending, destination = Path(work_dir), Path(pending), Path(destination)
    clean_stale(work_dir, pending)  # a killed earlier attempt's folder and movie, even if no encoder now
    tools = tools or probe_encoder()
    if not tools:
        raise EncoderUnavailable("Local ffmpeg and ffprobe must execute (-version); the accepted JPEG frames are preserved.")
    work_dir.mkdir(mode=0o700)
    try:
        paths = [Path(item["path"]) for item in sources]
        meta: dict = {}
        render_sources = paths
        if deflicker_control:
            try:
                produced, info = _deflicker(paths, deflicker_control, work_dir / "deflicker")
            except Exception as exc:  # noqa: BLE001 — same fallback as the completed render
                meta = {"deflicker_error": f"{type(exc).__name__}: {exc}"}
            else:
                # The corrected copies must come from exactly the accepted bytes.
                if [row["source_sha256"] for row in info["frames"]] != [item["sha256"] for item in sources]:
                    raise ValueError("an accepted frame changed while its deflicker copy was made")
                render_sources, meta["deflicker"] = produced, info
        frames_dir = work_dir / "frames"
        frames_dir.mkdir(mode=0o700)
        for position, (item, source) in enumerate(zip(sources, render_sources)):
            target = frames_dir / f"frame_{position:04d}.jpg"
            try:
                os.link(source, target)
            except OSError:
                shutil.copyfile(source, target)
            if "deflicker" not in meta and _sha256(target) != item["sha256"]:
                raise ValueError(f"accepted frame {item['index']} changed before the partial encode")
        pending.unlink(missing_ok=True)
        # Exclusive private file: an unfinished encode never becomes a downloadable artifact.
        os.close(os.open(pending, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600))
        info = render_h264(frames_dir, len(sources), fps, pending, tools=tools)
        os.replace(pending, destination)
    finally:
        pending.unlink(missing_ok=True)
        remove_work_dir(work_dir)
    return {"probe": info, "playback_duration_s": info["playback_duration_s"], **meta}


def receipt_document(*, session_id: str, partial_plan: dict, sources: list[dict], encoded: dict,
                     output: dict) -> dict:
    """The portable receipt written beside the movie (the manifest keeps a compact view)."""
    return {
        "kind": "timelapse_partial", "partial": True, "session_id": session_id,
        "policy": partial_plan["policy"], "fps": partial_plan["fps"], "width": 1920, "height": 1080,
        "frame_indices": [item["index"] for item in sources], "frame_count": len(sources),
        "ambiguous_indices": partial_plan["ambiguous_indices"],
        "capturing_indices": partial_plan["capturing_indices"],
        "pending_frames_not_shot": partial_plan["pending_count"],
        "inputs": [{"index": item["index"], "file": f"frame_{item['index']:04d}.jpg", "sha256": item["sha256"]}
                   for item in sources],
        "output": {"file": PARTIAL_NAME, "sha256": output["sha256"], "bytes": output["bytes"]},
        "playback_duration_s": encoded["playback_duration_s"], "probe": encoded["probe"],
        "full_decode_verified": True,
        **({"deflicker": encoded["deflicker"]} if "deflicker" in encoded else {}),
        **({"deflicker_error": encoded["deflicker_error"]} if "deflicker_error" in encoded else {}),
        "encoded_at": time.time(), "camera_opened": False, "shutter_fired": False, "note": NOTE,
    }


def manifest_record(*, session_id: str, document: dict, video_receipt: dict, document_receipt: dict) -> dict:
    """Compact ``partial_video`` manifest entry; per-frame hashes and gains stay in the receipt file."""
    record = {
        "status": "completed", "partial": True, "name": PARTIAL_NAME,
        "url": f"/sessions/{session_id}/artifacts/{PARTIAL_NAME}", "receipt": video_receipt,
        "receipt_file": {"name": RECEIPT_NAME, "url": f"/sessions/{session_id}/artifacts/{RECEIPT_NAME}",
                         "receipt": document_receipt},
        "frame_count": document["frame_count"], "frame_indices": document["frame_indices"],
        "ambiguous_indices": document["ambiguous_indices"], "capturing_indices": document["capturing_indices"],
        "fps": document["fps"], "width": 1920, "height": 1080,
        "playback_duration_s": document["playback_duration_s"], "full_decode_verified": True,
        "encoded_at": document["encoded_at"], "note": NOTE,
    }
    if "deflicker" in document:
        info = document["deflicker"]
        record["deflicker"] = {key: info[key] for key in ("window", "max_gain_stops", "frame_count")}
    if "deflicker_error" in document:
        record["deflicker_error"] = document["deflicker_error"]
    return record


def validate_record(record, directory: Path) -> None:
    """Manifest read check: a completed partial movie names only its own two files."""
    if not isinstance(record, dict) or record.get("status") not in RECORD_STATUSES:
        raise ValueError("invalid partial movie record")
    if record["status"] != "completed":
        return
    receipt = record.get("receipt")
    document = (record.get("receipt_file") or {}).get("receipt") if isinstance(record.get("receipt_file"), dict) else None
    if (not isinstance(receipt, dict) or receipt.get("path") != str(directory / PARTIAL_NAME)
            or not isinstance(document, dict) or document.get("path") != str(directory / RECEIPT_NAME)):
        raise ValueError("invalid partial movie receipt path")
