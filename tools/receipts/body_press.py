#!/usr/bin/env python3
"""Body-shutter acceptance receipt.

Starts a bounded handheld metering session on ONE explicitly selected Sony body,
prompts the owner to physically press the shutter N times, and for each pushed
frame records the EXIF, the settings the host applied for the NEXT frame, and the
final restoration result. Every frame is downloaded through the server's frames
route and its bytes are hash-verified against the receipt the server computed
before any setting changed.

The host never fires the shutter; the owner does. The tool only meters what the
camera pushes. Exit 0 only on an observed pass.

Pass requires: at least one frame arrived, the applied settings changed at least
once (applied_count >= 1), the camera's original settings were restored, and every
downloaded frame's sha256 matched its receipt.
"""
from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import _common as common


def _terminal(status: str) -> bool:
    return status in {"stopped", "error"}


def _frame_settled(frame: dict) -> bool:
    """A frame is done being processed once its apply decision reached a terminal state."""
    return frame.get("apply_status") in {"applied", "no_change", "skipped", "failed", "unmeterable"}


def _cancel(client, session_id: str) -> None:
    try:
        client.post(f"/handheld/{session_id}/cancel")
    except Exception:  # noqa: BLE001 — best effort; the server bounds the session anyway
        pass


def run(argv: list[str] | None = None) -> int:
    try:
        return _run(argv)
    except KeyboardInterrupt:
        session_id = _ACTIVE.get("session_id")
        if session_id:
            _cancel(_ACTIVE["client"], session_id)
            print(f"\nInterrupted: cancelled handheld session {session_id}; settings are being restored.")
        return 130


_ACTIVE: dict = {}


def _run(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--model", required=True,
                        help='exact camera model, e.g. "Sony Alpha-A7 III (PC Control)"')
    parser.add_argument("--port", required=True, help="USB port, e.g. usb:001,004")
    parser.add_argument("--presses", type=int, default=3,
                        help="number of shutter presses to prompt for (default 3)")
    parser.add_argument("--timeout", type=float, default=600.0,
                        help="max seconds to wait for all presses (default 600)")
    parser.add_argument("--intent", default=None, help="optional intent phrase for the brain")
    parser.add_argument("--poll-interval", type=float, default=1.0, help="status poll interval (s)")
    common.add_common_args(parser, default_base_url="http://127.0.0.1:8001")
    args = parser.parse_args(argv)

    if args.presses < 1:
        parser.error("--presses must be >= 1")

    token = common.read_token(args.token_file)
    client = common.Client(args.base_url, token, timeout=30.0)

    start_body = {
        "camera_model": args.model,
        "camera_port": args.port,
        "max_frames": args.presses,
        "max_duration_s": max(1.0, min(3600.0, args.timeout)),
    }
    if args.intent:
        start_body["intent"] = args.intent

    status, body = client.post("/handheld", start_body)
    if status != 202 or not isinstance(body, dict) or not body.get("id"):
        detail = body.get("detail") if isinstance(body, dict) else body
        return common.finish(
            what="body-press", passed=False, out_dir=args.out_dir,
            receipt_name=f"body-press-{_slug(args.model)}",
            payload={"model": args.model, "port": args.port, "start_status": status,
                     "start_error": detail},
            lines=[f"could not start handheld session (HTTP {status}): {detail}"])

    session_id = body["id"]
    _ACTIVE.update(session_id=session_id, client=client)
    print(f"Handheld session {session_id} started on {args.model} ({args.port}).")
    print(f"The host will NOT fire the shutter. Press it yourself {args.presses} time(s).\n")

    deadline = time.monotonic() + args.timeout
    seen = 0
    print(f">>> PRESS THE CAMERA SHUTTER NOW (1 of {args.presses}) <<<")

    last_state: dict = {}
    while True:
        s, state = client.get(f"/handheld/{session_id}")
        if s != 200 or not isinstance(state, dict):
            last_state = state if isinstance(state, dict) else {"detail": state}
            break
        last_state = state
        frames = state.get("frames") or []
        # Announce each newly settled frame and prompt for the next press.
        settled = [f for f in frames if _frame_settled(f)]
        if len(settled) > seen:
            for frame in settled[seen:]:
                idx = frame.get("index")
                nxt = frame.get("settings_for_next_frame")
                applied = frame.get("apply_status")
                print(f"  frame {idx}: metered (apply={applied}); next-frame settings: {nxt}")
            seen = len(settled)
            if seen < args.presses:
                print(f"\n>>> PRESS THE CAMERA SHUTTER NOW ({seen + 1} of {args.presses}) <<<")
        if _terminal(state.get("status", "")):
            break
        if seen >= args.presses:
            # Every requested press was metered and its next-frame settings applied.
            break
        if time.monotonic() >= deadline:
            break
        time.sleep(args.poll_interval)

    # Ask the session to stop and read the final, restored state.
    _cancel(client, session_id)
    final = common.poll_until(
        lambda: client.get(f"/handheld/{session_id}")[1],
        predicate=lambda st: isinstance(st, dict) and _terminal(st.get("status", "")),
        timeout_s=60.0, interval_s=args.poll_interval)
    if isinstance(final, dict):
        last_state = final

    frames = last_state.get("frames") or []
    applied_count = last_state.get("applied_count", 0)
    restored = last_state.get("restored")

    # Download and hash-verify every metered frame through the frames route.
    frame_receipts = []
    all_downloads_ok = True
    for frame in frames:
        receipt = frame.get("receipt")
        entry = {
            "index": frame.get("index"),
            "apply_status": frame.get("apply_status"),
            "exif": (receipt or {}).get("exif"),
            "applied": frame.get("applied"),
            "settings_for_next_frame": frame.get("settings_for_next_frame"),
            "why": frame.get("why"),
        }
        if receipt and receipt.get("path"):
            name = Path(receipt["path"]).name
            code, data = client.get_bytes(f"/handheld/{session_id}/frames/{name}")
            got = common.sha256_bytes(data) if code == 200 else None
            verified = code == 200 and got == receipt.get("sha256") and len(data) == receipt.get("bytes")
            entry["download"] = {
                "name": name, "http_status": code,
                "expected_sha256": receipt.get("sha256"), "observed_sha256": got,
                "expected_bytes": receipt.get("bytes"), "observed_bytes": len(data) if code == 200 else None,
                "hash_verified": verified,
            }
            if not verified:
                all_downloads_ok = False
        else:
            entry["download"] = {"skipped": "frame had no downloadable JPEG receipt"}
        frame_receipts.append(entry)

    frames_arrived = len(frames) > 0
    applied_changed = applied_count >= 1
    restoration_reported = restored is not None
    passed = bool(frames_arrived and applied_changed and restored is True and all_downloads_ok)

    lines = [
        f"session: {session_id}  body: {args.model} ({args.port})",
        f"frames metered: {len(frames)} / requested {args.presses}",
        f"applied changes: {applied_count} (need >= 1)",
        f"restoration reported: {restoration_reported}; restored: {restored}",
        f"all frame downloads hash-verified: {all_downloads_ok}",
        f"stop reason: {last_state.get('stop_reason')}  status: {last_state.get('status')}",
    ]
    if not frames_arrived:
        lines.append("FAIL CAUSE: no frame arrived (was the shutter pressed?)")
    if frames_arrived and not applied_changed:
        lines.append("FAIL CAUSE: applied settings never changed")
    if not restoration_reported:
        lines.append("FAIL CAUSE: restoration was not reported")
    elif restored is not True:
        lines.append("FAIL CAUSE: camera settings were not restored")
    if not all_downloads_ok:
        lines.append("FAIL CAUSE: a downloaded frame did not match its receipt")

    return common.finish(
        what="body-press", passed=passed, out_dir=args.out_dir,
        receipt_name=f"body-press-{_slug(args.model)}",
        payload={
            "model": args.model, "port": args.port, "session_id": session_id,
            "requested_presses": args.presses,
            "frames_metered": len(frames), "applied_count": applied_count,
            "restored": restored, "stop_reason": last_state.get("stop_reason"),
            "status": last_state.get("status"), "summary": last_state.get("summary"),
            "base_settings": last_state.get("base_settings"),
            "frames": frame_receipts,
        },
        lines=lines)


def _slug(text: str) -> str:
    return "".join(c if c.isalnum() else "-" for c in text).strip("-").lower() or "body"


if __name__ == "__main__":
    raise SystemExit(run())
