"""M0 — fast, resilient capture for the autopilot (hardened per cross-review).

Wraps ``sonyremote.Camera`` with the three things M0 needs — validated live on a
Sony A7 II + Tamron 28-75, and matched against libgphoto2's documented Sony
behavior:

1. **Event-based capture + download.** One shutter trigger; pull every file the
   camera reports (RAW *and* JPEG) via ``GP_EVENT_FILE_ADDED``, deduped, waiting a
   real quiet window so the slow RAW twin is never dropped. No filename guessing.

2. **Auto-reconnect on the USB drop.** The A7 II re-enumerates after a capture
   (libgphoto2 #981 / gphoto2 #153). Reconnect is bounded and never re-fires the
   shutter after it has fired (which would double-expose).

3. **Focus stays native.** We do NOT drive autofocus (flaky over gphoto2 on Sony,
   not the autopilot's job, and the patent zone). The user / camera focuses; we
   only set exposure. Driving AF was the entire 39s hunt — native focus = ~3s.

Benchmark on the dev rig (A7 II, indoor): 4/4 shots, ~3-4s each, 0 reconnects.

    from m0_capture import FastCapture
    with FastCapture(serial="A7II00SERIAL") as cam:   # the body's USB serial; ports re-enumerate
        cam.set_exposure(iso="800", shutter="1/60")   # the autopilot's job
        result = cam.shoot()                            # -> {files, secs, single_drive}
"""
from __future__ import annotations

import sys
import time
from pathlib import Path

_REPO = Path(__file__).resolve().parent.parent
if str(_REPO) not in sys.path:
    sys.path.insert(0, str(_REPO))

import gphoto2 as gp

from sonyremote import CameraError, CameraIdentityError, camera_for_serial

DEFAULT_DEST = _REPO / "captures"


class CameraUnavailable(RuntimeError):
    """Raised when the camera can't be (re)connected within the retry budget."""


class FastCapture:
    """A resilient capture session: connect once, reconnect on drop, capture fast.

    Focus is intentionally left to the camera/user — this class only sets exposure
    and captures. See the module docstring for why.
    """

    def __init__(self, serial: str) -> None:
        # shoot() fires through the raw gphoto2 handle, bypassing Camera.capture's guard, so the
        # body must be proven by its USB serial on every (re)connect. No serial, no session.
        if not serial:
            raise CameraIdentityError("FastCapture needs the body's USB serial.", reason="serial_required")
        self._serial = serial
        self._cam = None
        self.single_drive_ok: bool | None = None
        self._last_exposure: dict = {}  # remembered so we can restore it after a USB reconnect

    # --- session ----------------------------------------------------------
    def connect(self) -> FastCapture:
        self._cam = camera_for_serial(self._serial)
        self._cam.connect()
        self._cam.wake()
        self.single_drive_ok = self._enforce_single_drive()
        return self

    def close(self) -> None:
        if self._cam is not None:
            try:
                self._cam.disconnect()
            except CameraError:
                pass
            self._cam = None

    def __enter__(self) -> FastCapture:
        return self.connect()

    def __exit__(self, *exc) -> None:
        self.close()

    def _reconnect(self, attempts: int = 4, backoff: float = 0.6) -> None:
        """Documented fix for the post-capture USB re-enumeration. Bounded and safe:
        drops the stale handle, retries with backoff, raises CameraUnavailable if the
        camera is genuinely gone (instead of hanging or aborting the caller)."""
        try:
            if self._cam is not None:
                self._cam.disconnect()
        except CameraError:
            pass
        self._cam = None
        last = None
        for i in range(attempts):
            time.sleep(backoff * (i + 1))
            try:
                cam = camera_for_serial(self._serial)  # today's port for this serial, then proven
                cam.connect()
                cam.wake()
                self._cam = cam
                self._restore_state()  # re-assert single-drive + our exposure (the drop can reset them)
                return
            except CameraIdentityError:
                raise  # terminal: never reconnect to a body that is not this serial
            except CameraError as exc:
                last = exc
        raise CameraUnavailable(f"camera did not return after {attempts} reconnects ({last})")

    def _enforce_single_drive(self) -> bool:
        """One frame per trigger. The node name varies by body/firmware (drivemode
        vs capturemode); try the known variants and VERIFY the value stuck rather
        than silently swallowing. Returns False if single-shot can't be confirmed."""
        for node, value in (("drivemode", "Single"), ("capturemode", "Single Shot"),
                            ("drivemode", "Single Shot"), ("capturemode", "Single")):
            try:
                got = self._cam.set_setting(node, value)
                if "single" in str(got).lower():
                    return True
            except CameraError:
                continue
        return False

    def _restore_state(self) -> None:
        """After a reconnect the camera comes back fresh — re-assert the state we rely on:
        single-shot drive, and the last exposure we set, so a mid-session USB drop can't
        silently strand the camera at different settings than the autopilot intends.
        (Arsenal's 'settings get stuck on disconnect' lesson, applied to our set-and-keep model.)"""
        self.single_drive_ok = self._enforce_single_drive()
        if self._last_exposure:
            try:
                self._cam.set_exposure(**self._last_exposure)
            except CameraError:
                pass

    # --- exposure (the autopilot's job) -----------------------------------
    def set_exposure(self, *, iso=None, aperture=None, shutter=None,
                     white_balance=None, exposure_comp=None) -> dict:
        result = self._cam.set_exposure(
            iso=iso, aperture=aperture, shutter=shutter,
            white_balance=white_balance, exposure_comp=exposure_comp,
        )
        # remember what we set, so _restore_state() can re-apply it after a USB reconnect
        for key, value in (("iso", iso), ("aperture", aperture), ("shutter", shutter),
                           ("white_balance", white_balance), ("exposure_comp", exposure_comp)):
            if value is not None:
                self._last_exposure[key] = value
        return result

    # --- capture ----------------------------------------------------------
    def _drain_events(self, ms: int = 50) -> None:
        """Discard stale events so a fresh capture doesn't collect a prior shot's files."""
        cam = self._cam._camera
        while True:
            event_type, _ = cam.wait_for_event(ms)
            if event_type == gp.GP_EVENT_TIMEOUT:
                return

    def _save(self, folder, name, dest_dir: Path, seen: set, names: list) -> None:
        """Download one camera file to a unique path (temp then rename), deduped."""
        key = (folder, name)
        if key in seen:
            return
        seen.add(key)
        stamp = time.strftime("%Y%m%d-%H%M%S")
        out = dest_dir / f"{stamp}_{name}"
        counter = 1
        while out.exists():
            out = dest_dir / f"{stamp}_{counter}_{name}"
            counter += 1
        tmp = out.with_suffix(out.suffix + ".part")
        self._cam._camera.file_get(folder, name, gp.GP_FILE_TYPE_NORMAL).save(str(tmp))
        tmp.rename(out)
        names.append(out.name)

    def _collect(self, dest_dir: Path, seen: set, names: list, first=None,
                 settle: float = 0.8, total: float = 10.0) -> None:
        """Download the captured file(s). Waits for a genuine quiet window (`settle`)
        after the last file — so a slow RAW twin is never dropped — bounded by `total`."""
        cam = self._cam._camera
        if first is not None:
            self._save(first.folder, first.name, dest_dir, seen, names)
        deadline = time.monotonic() + total
        last_file = time.monotonic()
        while time.monotonic() < deadline:
            event_type, data = cam.wait_for_event(300)
            if event_type == gp.GP_EVENT_FILE_ADDED:
                self._save(data.folder, data.name, dest_dir, seen, names)
                last_file = time.monotonic()
            elif event_type == gp.GP_EVENT_TIMEOUT:
                if names and (time.monotonic() - last_file) > settle:
                    return

    def shoot(self, *, dest_dir: Path | str = DEFAULT_DEST, retries: int = 2,
              **exposure) -> dict:
        """Set any given exposure values, then capture one frame. Returns
        ``{files, secs, single_drive}``. The shutter is triggered exactly once;
        a download/USB error reconnects to restore the session but never re-fires."""
        dest_dir = Path(dest_dir)
        dest_dir.mkdir(parents=True, exist_ok=True)

        # Phase A — connect + set exposure. Safe to retry; no shutter has fired.
        for attempt in range(retries + 1):
            try:
                if self._cam is None:
                    self._reconnect()
                if any(v is not None for v in exposure.values()):
                    self.set_exposure(**exposure)
                break
            except CameraIdentityError:
                raise  # terminal: no retry, no shutter on an unproven body
            except (CameraError, gp.GPhoto2Error):
                if attempt < retries:
                    self._reconnect()
                else:
                    raise

        # Phase B — fire ONCE. Never re-fire on a download error (would double-expose).
        seen: set = set()
        names: list[str] = []
        started = time.time()
        self._drain_events()
        try:
            first = self._cam._camera.capture(gp.GP_CAPTURE_IMAGE)  # the only shutter trigger
            self._collect(dest_dir, seen, names, first=first)
        except (CameraError, gp.GPhoto2Error):
            # The shutter may have fired; do NOT re-fire. Restore the session for the
            # next shot. Files downloaded before the drop are already in `names`.
            try:
                self._reconnect()
            except (CameraUnavailable, CameraIdentityError):
                pass  # the next shoot() reconnects (and refuses an unproven body) before firing
        return {"files": names, "secs": round(time.time() - started, 2),
                "single_drive": self.single_drive_ok}


if __name__ == "__main__":
    if len(sys.argv) != 2:
        raise SystemExit("usage: m0_capture.py <USB serial>   (fires 4 shutters on that body)")
    with FastCapture(serial=sys.argv[1]) as cam:
        print(f"single-shot enforced: {cam.single_drive_ok}")
        cam.set_exposure(iso="800", shutter="1/60")
        for i in range(4):
            r = cam.shoot(dest_dir="/tmp/m0test")
            print(f"shot {i}: {r['secs']:>5}s  files={r['files']}")
