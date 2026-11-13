#!/usr/bin/env python3
"""Owner receipt for the long-exposure route: ONE exposure per explicit shutter above 1 s.

    .venv/bin/python tools/long_exposure_capture.py --serial SERIAL --shutter 2 --shutter 5 \
        --shutter 15 --out artifacts/long-exposure-receipt-DATE/<body> [--aperture f/8] [--iso 100] [--run]

Without `--run` nothing opens USB: it prints, per shutter, the envelope the route would use (event
wait, process timeout, settle bound) and the CLI commands. With `--run` it finds the body by its
USB serial (ports re-enumerate), requires the mode dial at M, sets each shutter (and the optional
aperture/ISO) with exact readback, fires `Camera.capture_long_exposure` once per shutter, and
restores the body's original exposure after each one, reconnecting first. An uncertain outcome is
recorded and ends the run: never retried, and restoration waits until the camera is known idle.
Each JPEG, its EXIF attribution and the transport receipt land in `--out/receipt.json`.

Shutter labels are the body's own spelling (the A7 II lists 3.2 s as "32/10"). Requires exclusive
ownership of the body: stop the server and any handheld listener first.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import sys
import time
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_REPO_ROOT))

from sonyremote.camera import (CameraError, CaptureOutcomeUncertain,  # noqa: E402
                               LONG_EXPOSURE_PRE_DRAIN_WAIT_S, read_settings_checked,
                               validate_sony_long_exposure_shutter)

HARDWARE_GALLERY = (_REPO_ROOT / "backend" / "shots").resolve()


def gallery_roots() -> set[Path]:
    """The shot store the engine would use right now (engine._resolve_shots_dir, honouring
    CAMERA_AUTOPILOT_SHOTS_DIR and CAMERA_ENGINE), plus the hardware gallery backend/shots."""
    sys.path.insert(0, str(_REPO_ROOT / "backend"))
    import engine
    return {engine._resolve_shots_dir().resolve(), HARDWARE_GALLERY}


def plan(shutters: list[str]) -> list[dict]:
    """The envelope per shutter; camera-free. Refuses a shutter the route could never accept."""
    rows = []
    for label in shutters:
        # The route's own checks (above 1 s, at most 30 s, on Sony's ladder, no placeholder); whether
        # this body lists the label is checked by capture_long_exposure with --run.
        deadline = validate_sony_long_exposure_shutter(label, [label])
        rows.append({"shutter": label, "configured_shutter_s": deadline["configured_shutter_s"],
                     "event_wait_s": deadline["event_wait_s"],
                     "process_timeout_s": deadline["subprocess_timeout_s"],
                     "minimum_settle_s": deadline["minimum_settle_s"],
                     "commands": [
                         f"gphoto2 --camera <model> --port <port> --wait-event-and-download="
                         f"{LONG_EXPOSURE_PRE_DRAIN_WAIT_S}s --keep   (read-only drain; any file refuses)",
                         f"gphoto2 --camera <model> --port <port> --trigger-capture "
                         f"--wait-event-and-download={deadline['event_wait_s']}s --keep   (the one shutter)"]})
    return rows


def _sha256(path: Path) -> str:
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def _write(out: Path, receipt: dict) -> None:
    receipt["updated_at"] = time.time()
    target = out / "receipt.json"
    partial = out / "receipt.json.partial"
    partial.write_text(json.dumps(receipt, indent=2, default=str))
    partial.replace(target)


def _settled(attempted: bool, delivered: bool, transport: dict | None, minimum_settle_s: float) -> bool:
    """Whether the body is known idle, so settings may be written. Judged from the transport's own
    post-trigger time, never the whole call (that includes the pre-drain). Anything unknown,
    an interrupt included, counts as possibly still exposing."""
    if not attempted or delivered:
        return True
    transport = transport if isinstance(transport, dict) else {}
    if transport.get("trigger_commands") == 0:
        return True   # refused before the shutter
    elapsed = (transport.get("timings") or {}).get("trigger_and_download_s")
    return isinstance(elapsed, (int, float)) and elapsed >= minimum_settle_s


def run(args, rows: list[dict], out: Path) -> tuple[dict, int]:
    from sonyremote import camera_for_serial
    receipt = {"status": "started", "serial": args.serial, "requested": rows, "shots": [],
               "aperture": args.aperture, "iso": args.iso}
    camera = camera_for_serial(args.serial)
    camera.quarantine_dir = out / "quarantine"
    with camera:
        identity = camera.info()
        receipt.update(model=identity.model, port=identity.port, serial_verification=camera.serial_verification)
        if camera.shooting_mode() != "M":
            receipt["status"] = "refused_mode_dial_not_m"
            return receipt, 2
        # #79's checked read: a persistent A7 III placeholder (ISO 0, f/0, 65535/65535) refuses here,
        # before any write or shutter, and is never recorded as a baseline to restore.
        baseline, baseline_reread = read_settings_checked(camera, purpose="long-exposure receipt baseline")
        receipt["baseline"] = baseline
        if baseline_reread:
            receipt["baseline_rereads"] = baseline_reread
        restore = {key: baseline[key] for key in ("iso", "aperture", "shutter") if baseline.get(key)}
        if (baseline_reread or {}).get("aperture_unreported"):
            restore.pop("aperture", None)  # a manual lens reports f/0; never write it back
        original_focus = baseline.get("focus_mode")
        if not isinstance(original_focus, str) or not original_focus.strip():
            # No focus restore target: the tool would leave the body in Manual. Refuse before any write.
            receipt["status"] = "refused_focus_mode_unknown"
            receipt["error"] = "The body reports no focus mode to restore; no setting was written and no shutter was issued."
            return receipt, 2
        for row in rows:
            shot = {"shutter": row["shutter"], "status": "preflight"}
            receipt["shots"].append(shot)
            _write(out, receipt)
            wanted = {"shutter": row["shutter"], **({"aperture": args.aperture} if args.aperture else {}),
                      **({"iso": args.iso} if args.iso else {})}
            attempted = delivered = False
            try:
                camera.set_exposure(**wanted)
                # Focus first, by hand: a dark scene can stop AF-priority from releasing the shutter.
                camera.set_setting("focus_mode", "Manual")
                readback, _reread = read_settings_checked(camera, purpose="before the long-exposure trigger")
                shot["before_trigger_readback"] = readback
                if (any(str(readback.get(key)) != str(value) for key, value in wanted.items())
                        or readback.get("focus_mode") != "Manual"):
                    raise CameraError("settings readback did not match the request; no shutter was issued")
                attempted = True
                path = camera.capture_long_exposure(row["shutter"], dest_dir=out)
                shot.update(status="delivered", path=str(path), sha256=_sha256(path))
                delivered = True
            except CaptureOutcomeUncertain as exc:
                shot.update(status="uncertain_not_retried", error=str(exc),
                            capture_path=str(getattr(exc, "capture_path", "") or ""))
            except CameraError as exc:
                shot.update(status="refused_no_shutter", error=str(exc))
            finally:
                settled = _settled(attempted, delivered, camera.last_capture_receipt, row["minimum_settle_s"])
                shot["transport_receipt"] = camera.last_capture_receipt if attempted else None
                if settled:
                    try:
                        camera.reconnect()   # restoration reconnects before it writes
                        camera.set_exposure(**restore)
                        if original_focus:
                            camera.set_setting("focus_mode", original_focus)
                        restored, _reread = read_settings_checked(camera, purpose="after restoring the baseline")
                        shot["restored_readback"] = restored
                        expected = {**restore, **({"focus_mode": original_focus} if original_focus else {})}
                        shot["restore"] = ("verified" if all(str(restored.get(k)) == str(v) for k, v in expected.items())
                                           else "readback_mismatch")
                    except Exception as exc:  # noqa: BLE001 — recorded for the owner
                        shot["restore"] = f"failed: {exc}"
                else:
                    shot["restore"] = "deferred_until_physical_idle_verified"
                    shot["baseline_to_restore_by_hand"] = {**restore, "focus_mode": original_focus}
                _write(out, receipt)
            if shot["status"] != "delivered":
                receipt["status"] = "stopped"
                break
        else:
            receipt["status"] = "completed"
    ok = receipt["status"] == "completed" and all(s.get("restore") == "verified" for s in receipt["shots"])
    return receipt, 0 if ok else 2


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--serial", help="the body's USB serial; required with --run")
    parser.add_argument("--shutter", action="append", required=True,
                        help="a shutter above 1 s in the body's own spelling; repeat for several")
    parser.add_argument("--aperture", help="optional aperture to set first, e.g. f/8")
    parser.add_argument("--iso", help="optional ISO to set first, e.g. 100")
    parser.add_argument("--out", required=True, help="receipt directory (outside the shot gallery and backend/shots)")
    parser.add_argument("--run", action="store_true", help="open the body and fire; otherwise plan only")
    args = parser.parse_args(argv)
    try:
        rows = plan(args.shutter)
    except CameraError as exc:
        print(json.dumps({"status": "refused", "error": str(exc)}))
        return 2
    if not args.run:
        print(json.dumps({"status": "plan_only", "shots": rows}, indent=2))
        return 0
    if not args.serial:
        print(json.dumps({"status": "serial_required", "error": "--serial is required with --run"}))
        return 2
    out = Path(args.out).expanduser().resolve()
    inside = sorted(str(root) for root in gallery_roots() if out == root or root in out.parents)
    if inside:
        # Receipts and quarantine inside a gallery would be listed or swept by delete-all.
        print(json.dumps({"status": "refused",
                          "error": f"--out must be outside the shot gallery ({', '.join(inside)}) and backend/shots"}))
        return 2
    out.mkdir(mode=0o700, parents=True, exist_ok=True)
    try:
        receipt, code = run(args, rows, out)
    except (CameraError, OSError) as exc:
        # Raised before or between shutters (discovery, connect, a setting write); nothing is retried.
        receipt, code = {"status": "failed", "error": f"{type(exc).__name__}: {exc}", "shots": []}, 2
    _write(out, receipt)
    print(json.dumps({key: receipt[key] for key in ("status", "model", "port") if key in receipt}
                     | {"shots": [{k: s.get(k) for k in ("shutter", "status", "restore", "path")}
                                  for s in receipt["shots"]], "receipt": str(out / "receipt.json")}, indent=2))
    return code


if __name__ == "__main__":
    raise SystemExit(main())
