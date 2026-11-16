#!/usr/bin/env python3
"""Night stack acceptance receipt.

Creates a `night` computational session on ONE explicitly selected Sony body,
submits it as a durable background job, polls to completion, downloads the
published stacked image through the session artifacts route, hash-verifies it
against the session's own output receipt, and writes the session sidecar
evidence into the receipt.

Preconditions the owner must have met (after sunset, camera on a tripod):
  - The mode dial is physically on M and the camera is awake.
  - The server is running with the real USB engine on this base URL.

When `--meter-image <saved shot>` is given, the tool prefers the
`/night-assist/plan` route: it plans the stack from that saved meter frame and
the body's measured noise table, and creates the session from the plan's ready
`session_request`. Without it, the session is created directly from `--frames`.

Exit 0 only on an observed pass (job completed, image published, download
hash-verified).
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import _common as common

_TERMINAL = {"completed", "failed", "restore_failed", "cancelled",
             "uncertain", "recovery_required", "error"}


def _night_plan(client: common.Client, *, image: str, model: str, port: str,
                serial: str, max_frames: int) -> tuple[dict | None, dict | None, str | None]:
    """Call /night-assist/plan for a saved meter frame.

    Returns (session_request, plan_payload, error). A non-ok plan (too dark,
    unreliable meter) yields (None, payload, reason) so the caller fails honestly.
    """
    status, body = client.post("/night-assist/plan", {
        "image": image, "camera_model": model, "camera_port": port,
        "camera_serial": serial,
        "max_frames": max(2, min(16, max_frames)), "tripod": True,
    })
    if status != 200 or not isinstance(body, dict):
        detail = body.get("detail") if isinstance(body, dict) else body
        return None, body if isinstance(body, dict) else None, f"plan HTTP {status}: {detail}"
    if body.get("status") != "ok" or not isinstance(body.get("session_request"), dict):
        return None, body, f"night plan status={body.get('status')} (not ok)"
    return body["session_request"], body, None


def run(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--model", required=True, help='exact camera model')
    parser.add_argument("--port", required=True, help="USB port, e.g. usb:001,004")
    parser.add_argument("--serial", required=True, help="USB serial from camera discovery")
    parser.add_argument("--frames", type=int, default=8,
                        help="frames to stack, or the plan cap with --meter-image (default 8)")
    parser.add_argument("--meter-image", default=None,
                        help="saved gallery shot to plan the stack from via /night-assist/plan")
    parser.add_argument("--poll-timeout", type=float, default=1800.0,
                        help="max seconds to wait for the job (default 1800)")
    parser.add_argument("--poll-interval", type=float, default=3.0, help="poll interval (s)")
    parser.add_argument("--delay-seconds", type=float, default=0.0,
                        help="optional durable start delay for the job")
    common.add_common_args(parser, default_base_url="http://127.0.0.1:8001")
    args = parser.parse_args(argv)

    token = common.read_token(args.token_file)
    client = common.Client(args.base_url, token, timeout=60.0)

    plan = None
    create_body = {"mode": "night", "frames": args.frames,
                   "camera_model": args.model, "camera_port": args.port, "camera_serial": args.serial}
    frames = args.frames
    if args.meter_image:
        session_request, plan, plan_error = _night_plan(
            client, image=args.meter_image, model=args.model, port=args.port,
            serial=args.serial, max_frames=args.frames)
        if session_request is None:
            return common.finish(
                what="night-session", passed=False, out_dir=args.out_dir,
                receipt_name="night-session",
                payload={"model": args.model, "port": args.port,
                         "meter_image": args.meter_image, "night_plan": plan},
                lines=[f"FAIL CAUSE: night plan unusable: {plan_error}",
                       "Too dark / unreliable meter, or the route rejected the request."])
        create_body = session_request
        frames = session_request.get("frames", args.frames)
        print(f"/night-assist/plan: planned {frames} frames from {args.meter_image}.")

    status, created = client.post("/sessions", create_body)
    if status != 200 or not isinstance(created, dict) or not created.get("id"):
        detail = created.get("detail") if isinstance(created, dict) else created
        return common.finish(
            what="night-session", passed=False, out_dir=args.out_dir,
            receipt_name="night-session",
            payload={"model": args.model, "port": args.port, "create_status": status,
                     "create_error": detail, "night_plan": plan},
            lines=[f"could not create night session (HTTP {status}): {detail}"])

    session_id = created["id"]
    print(f"Night session {session_id} created ({frames} frames). Submitting job...")

    js, job = client.post(f"/sessions/{session_id}/jobs",
                          {"delay_seconds": args.delay_seconds})
    if js != 202 or not isinstance(job, dict):
        detail = job.get("detail") if isinstance(job, dict) else job
        return common.finish(
            what="night-session", passed=False, out_dir=args.out_dir,
            receipt_name="night-session",
            payload={"session_id": session_id, "submit_status": js, "submit_error": detail},
            lines=[f"could not submit night job (HTTP {js}): {detail}"])

    print("Job submitted. Polling to completion...")
    final = common.poll_until(
        lambda: client.get(f"/sessions/{session_id}/jobs")[1],
        predicate=lambda r: isinstance(r, dict) and r.get("status") in _TERMINAL,
        timeout_s=args.poll_timeout, interval_s=args.poll_interval)

    job_state = final if isinstance(final, dict) else {"status": "unknown", "raw": final}
    session_state = job_state.get("session") if isinstance(job_state.get("session"), dict) else {}
    job_status = job_state.get("status")
    session_status = session_state.get("status")
    publication = session_state.get("publication") or {}
    published_image = session_state.get("image") or publication.get("image")
    output_receipt = (session_state.get("output") or {}).get("receipt") or {}

    # Download the published stacked image through the session artifacts route and
    # hash-verify it against the session's own output receipt.
    download = {"attempted": False}
    download_ok = False
    if published_image:
        download["attempted"] = True
        code, data = client.get_bytes(f"/sessions/{session_id}/artifacts/result.jpg")
        got = common.sha256_bytes(data) if code == 200 else None
        expected = output_receipt.get("sha256") or publication.get("sha256")
        download_ok = code == 200 and expected is not None and got == expected
        download.update({"artifact": "result.jpg", "http_status": code,
                         "expected_sha256": expected, "observed_sha256": got,
                         "bytes": len(data) if code == 200 else None,
                         "hash_verified": download_ok})

    passed = bool(job_status == "completed" and session_status == "completed"
                  and publication.get("status") == "published" and published_image
                  and download_ok)

    lines = [
        f"session: {session_id}  body: {args.model} ({args.port})  frames: {frames}",
        f"job status: {job_status}  session status: {session_status}",
        f"published image: {published_image}  publication: {publication.get('status')}",
        f"artifact download hash-verified: {download_ok}",
    ]
    if job_status != "completed":
        lines.append(f"FAIL CAUSE: job did not complete ({job_status}); {job_state.get('error')}")
    elif not published_image:
        lines.append("FAIL CAUSE: no published image was produced")
    elif not download_ok:
        lines.append("FAIL CAUSE: published image could not be downloaded/hash-verified")

    return common.finish(
        what="night-session", passed=passed, out_dir=args.out_dir,
        receipt_name="night-session",
        payload={
            "model": args.model, "port": args.port, "session_id": session_id,
            "frames": frames, "night_plan": plan,
            "job": {k: v for k, v in job_state.items() if k != "session"},
            "session_status": session_status,
            "publication": publication,
            "published_image": published_image,
            "output_receipt": output_receipt,
            "restore": session_state.get("restore"),
            "download": download,
        },
        lines=lines)


if __name__ == "__main__":
    raise SystemExit(run())
