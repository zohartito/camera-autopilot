#!/usr/bin/env python3
"""Explicit experimental Sony trigger/wait probe. Never used by product capture."""
from __future__ import annotations
import argparse
import hashlib
import json
import math
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import time
import uuid

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT), str(ROOT / "backend")]
MODELS = {"Sony ILCE-7M2 (Control)": "ILCE-7M2", "Sony Alpha-A7 III (PC Control)": "ILCE-7M3"}


def command_plan(model, port, shutter):
    if model not in MODELS or not re.fullmatch(r"usb:[0-9]+,[0-9]+", port):
        raise ValueError("exact supported Sony model and USB port are required")
    if str(shutter) not in {"15", "30"}:
        raise ValueError("experimental probe supports only explicit 15 or 30 second settings")
    base = ["gphoto2", "--camera", model, "--port", port]
    return {"pre_drain": base + ["--wait-event-and-download=5s", "--keep"],
            "capture": base + ["--trigger-capture", "--wait-event-and-download=75s", "--keep"],
            "pre_drain_timeout_s": 20, "capture_timeout_s": 90,
            "possible_post_failure_settle_s": 2 * float(shutter) + 5,
            "trigger_commands": 1, "automatic_retry": False,
            "all_outputs_quarantined": True}


def _files(directory):
    files = []
    total = 0
    for path in sorted(directory.iterdir()):
        if path.is_symlink() or not path.is_file():
            raise ValueError("probe output is not a regular file")
        size = path.stat().st_size
        total += size
        if total > 512 * 1024 * 1024 or len(files) >= 32:
            raise ValueError("probe output exceeds inspection budget; preserve quarantine")
        path.chmod(0o600)
        with path.open("rb") as stream:
            digest = hashlib.file_digest(stream, "sha256").hexdigest()
        files.append({"file": path.name, "bytes": size, "sha256": digest})
    return files


def run_probe(args):
    from sonyremote import Camera
    from sonyremote.camera import read_settings_checked, verify_serial_at_port
    from stack_session import _atomic_json
    import engine
    plan = command_plan(args.model, args.port, args.shutter)
    if not args.execute:
        return {"status": "plan_only", **plan}, 0
    # The probe fires through its own CLI command, so it proves the body's USB serial itself:
    # at the settings connection, and immediately before and after each CLI open of the port.
    serial = getattr(args, "serial", None)
    if not serial:
        return {"status": "serial_required_no_trigger",
                "error": "--serial is required with --execute (the port alone is not an identity)"}, 2
    binary = shutil.which("gphoto2")
    if not binary:
        raise ValueError("gphoto2 is required")
    root = Path(args.out).resolve() / ("long-probe-" + uuid.uuid4().hex)
    root.mkdir(parents=True, mode=0o700)
    root.chmod(0o700)
    prior, outputs = root / "prior-ram", root / "trigger-output"
    prior.mkdir(mode=0o700)
    outputs.mkdir(mode=0o700)
    receipt = {"status": "preflight", "model": args.model, "port": args.port,
               "requested": {"iso": "100", "aperture": args.aperture, "shutter": args.shutter, "focus_mode": "Manual"},
               "plan": plan, "root": str(root), "trigger_attempted": False,
               "attribution_verified": False, "publication": "quarantined", "restore": "not_needed"}
    def save():
        receipt["updated_at"] = time.time()
        _atomic_json(root / "receipt.json", receipt)
    def cli(argv, directory, timeout, name):
        engine._free_usb()
        began = time.monotonic()
        try:
            result = subprocess.run([binary, *argv[1:]], cwd=directory, capture_output=True,
                                    text=True, timeout=timeout, check=False)
            receipt[name] = {"argv": argv, "returncode": result.returncode,
                             "stdout": result.stdout[-65536:], "stderr": result.stderr[-65536:],
                             "elapsed_s": time.monotonic() - began, "timeout_s": timeout}
            return result
        except subprocess.TimeoutExpired as exc:
            receipt[name] = {"argv": argv, "timeout": True, "timeout_s": timeout,
                             "elapsed_s": time.monotonic() - began,
                             "stdout": str(exc.stdout or "")[-65536:], "stderr": str(exc.stderr or "")[-65536:]}
            raise
        finally:
            save()
    baseline = None
    settle_after_failure = False
    try:
        save()
        # The trigger-only route does not clean old RAM. Any pending file aborts this probe.
        receipt["pre_drain_serial_verification"] = verify_serial_at_port(args.model, args.port, serial)
        drained = cli(plan["pre_drain"], prior, plan["pre_drain_timeout_s"], "pre_drain")
        receipt["prior_files"] = _files(prior)
        drain_errors = bool(re.search(r"(?im)^\s*(?:ERROR:|\*\*\* Error)", drained.stdout + "\n" + drained.stderr))
        if drained.returncode or drain_errors or receipt["prior_files"]:
            receipt["status"] = "prior_ram_unresolved_no_trigger"
            save()
            return receipt, 2
        engine._free_usb()
        with Camera(model=args.model, port=args.port, serial=serial) as camera:
            identity = camera.info()
            if (identity.model, identity.port) != (args.model, args.port) or camera.shooting_mode() != "M":
                raise ValueError("camera identity or manual exposure mode does not match")
            # Restored afterwards: never keep a placeholder read (f/0, ISO 0, 65535/65535) as the baseline.
            original, baseline_read = read_settings_checked(camera, purpose="probe baseline")
            if baseline_read:
                receipt["baseline_read"] = baseline_read
            if any(key not in original for key in ("iso", "aperture", "shutter", "focus_mode")):
                raise ValueError("complete original settings are required before experimental writes")
            baseline = dict(original)
            receipt["baseline"] = dict(baseline)
            receipt["restore"] = "pending"
            save()
            camera.set_exposure(iso="100", aperture=args.aperture, shutter=args.shutter)
            camera.set_setting("focus_mode", "Manual")
            actual, _ = read_settings_checked(camera, purpose="probe experimental readback")
            if any(str(actual.get(k)) != str(v) for k,v in receipt["requested"].items()):
                raise ValueError("experimental settings did not match exact readback")
            receipt["before_trigger_readback"] = actual
        receipt["pre_trigger_serial_verification"] = verify_serial_at_port(args.model, args.port, serial)
        receipt.update(status="trigger_intent_persisted", trigger_attempted=True,
                       trigger_started_at=time.time())
        save()
        settle_after_failure = True
        result = cli(plan["capture"], outputs, plan["capture_timeout_s"], "capture")
        try:
            receipt["post_trigger_serial_verification"] = verify_serial_at_port(args.model, args.port, serial)
        except Exception as exc:  # the port's body changed around the trigger: never attributable
            receipt["post_trigger_identity_error"] = str(exc)
            settle_after_failure = True
            raise
        if result.returncode == 0 and receipt["capture"]["elapsed_s"] >= 2 * float(args.shutter) + 5:
            settle_after_failure = False
        receipt["files"] = _files(outputs)
        from image_safety import open_image
        photos = []
        for item in receipt["files"]:
            if not item["file"].lower().endswith((".jpg", ".jpeg")):
                continue
            with open_image(outputs / item["file"]) as image:
                exif = image.getexif()
                detail = exif.get_ifd(34665) if 34665 in exif else exif
                metadata = {"camera_model": str(exif.get(272, "")),
                            "iso": float(detail.get(34855, 0)), "aperture": float(detail.get(33437, 0)),
                            "shutter_s": float(detail.get(33434, 0)), "datetime_original": str(detail.get(36867, "")),
                            "width": image.width, "height": image.height}
                metadata["readback_matches"] = (metadata["camera_model"] == MODELS[args.model]
                    and metadata["iso"] == 100 and abs(metadata["aperture"] - float(args.aperture.removeprefix("f/"))) < .01
                    and abs(metadata["shutter_s"] - float(args.shutter)) < .01)
                photos.append({**item, "exif": metadata})
        receipt["jpeg_receipts"] = photos
        receipt["readback_and_exif_verified"] = bool(photos) and all(p["exif"]["readback_matches"] for p in photos)
        receipt["cli_reported_error"] = bool(result.returncode or re.search(
            r"(?im)^\s*(?:ERROR:|\*\*\* Error)", result.stdout + "\n" + result.stderr))
        receipt["status"] = ("downloaded_error_quarantined" if photos and receipt["cli_reported_error"] else
                             "downloaded_quarantined" if receipt["readback_and_exif_verified"] else
                             "downloaded_mismatch_quarantined" if photos else "no_jpeg_uncertain")
        receipt["association_note"] = "An empty pre-drain and matching EXIF support review, but old delayed RAM objects cannot be excluded; no output is automatically attributed or published."
    except BaseException as exc:
        receipt.update(status="probe_failed_or_interrupted", error=f"{type(exc).__name__}: {exc}")
    finally:
        if baseline is not None:
            try:
                if settle_after_failure:
                    # A timeout does not reveal when the CLI last issued a trigger.
                    # Leave settings untouched until the operator verifies physical idle.
                    receipt["restore"] = "deferred_until_physical_idle_verified"
                    receipt["recommended_wait_before_readback_s"] = plan["possible_post_failure_settle_s"]
                    receipt["restore_note"] = "No automatic settings restoration after an uncertain trigger; baseline is retained for the operator."
                else:
                    engine._free_usb()
                    with Camera(model=args.model, port=args.port, serial=serial) as camera:
                        identity = camera.info()
                        if (identity.model,identity.port) != (args.model,args.port):
                            raise ValueError("restore camera identity changed")
                        camera.set_exposure(iso=baseline["iso"], aperture=baseline["aperture"], shutter=baseline["shutter"])
                        camera.set_setting("focus_mode",baseline["focus_mode"])
                        actual, _ = read_settings_checked(camera, purpose="probe restore readback")
                        if any(str(actual.get(k)) != str(baseline[k]) for k in ("iso","aperture","shutter","focus_mode")):
                            raise ValueError("original settings restoration was not confirmed")
                        receipt["restore"] = "verified"
                        receipt["restored_readback"] = actual
            except BaseException as exc:
                receipt["restore"] = "failed_or_deferred"
                receipt["restore_error"] = f"{type(exc).__name__}: {exc}"
        save()
    return receipt, 0 if receipt["status"] == "downloaded_quarantined" and receipt["restore"] == "verified" else 2


def main(argv=None):
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--model",required=True,choices=sorted(MODELS))
    parser.add_argument("--port",required=True)
    parser.add_argument("--serial",help="the body's USB serial; required with --execute and proven before every CLI open")
    parser.add_argument("--shutter",required=True,choices=["15","30"])
    parser.add_argument("--aperture",default="f/5.6")
    parser.add_argument("--out",default=str(ROOT/"artifacts/falsification/implementation-wave-20260905/experimental-long"))
    parser.add_argument("--execute",action="store_true",help="Execute this explicit experimental single-trigger probe; otherwise show plan only")
    args=parser.parse_args(argv)
    try:
        aperture=float(args.aperture.removeprefix("f/"))
        if not math.isfinite(aperture) or not .5<=aperture<=64:
            raise ValueError("invalid requested aperture")
        receipt,code=run_probe(args)
        print(json.dumps(receipt,indent=2))
        return code
    except (ValueError,OSError) as exc:
        print(json.dumps({"status":"preflight_failed","error":str(exc)}))
        return 2


if __name__=="__main__":
    raise SystemExit(main())
