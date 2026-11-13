"""Live-camera smoke test. Run with the A7 II connected in PC Remote mode:

    ./.venv/bin/python smoke_test.py --serial A7II00SERIAL

The body is chosen and proven by its USB serial (ports re-enumerate), so the capture step can
never fire a different body that took its port.

Exercises the full capture->see->adjust path: detect, connect, read settings, list ISO
options, set ISO with verify, and capture+download a JPEG. Prints PASS/FAIL per step.
"""

import argparse

from sonyremote import CameraError, camera_for_serial, detect


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Live-camera smoke test (fires one shutter).")
    parser.add_argument("--serial", required=True, help="the body's USB serial (tools/inspect_usb_camera.py)")
    args = parser.parse_args(argv)
    found = detect()
    print(f"detect(): {found or 'NOTHING FOUND'}")
    if not found:
        print("FAIL: no camera. Set USB Connection -> PC Remote and reconnect the cable.")
        return 1

    try:
        with camera_for_serial(args.serial) as cam:
            print(f"PASS connect: {cam.info().model}")

            settings = cam.get_settings()
            print(f"PASS get_settings: {settings}")

            iso_options = cam.list_options("iso")
            print(f"PASS list_options(iso): {iso_options[:8]}{' ...' if len(iso_options) > 8 else ''}")

            target_iso = "100" if "100" in iso_options else iso_options[0]
            applied = cam.set_setting("iso", target_iso)
            print(f"PASS set_setting(iso={target_iso}): now {applied}")

            photo = cam.capture(download=True)
            ok = photo is not None and photo.exists() and photo.stat().st_size > 0
            print(f"{'PASS' if ok else 'FAIL'} capture: {photo} ({photo.stat().st_size if ok else 0} bytes)")
            return 0 if ok else 1
    except CameraError as exc:
        print(f"FAIL: {exc}")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
