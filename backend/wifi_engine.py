"""Wi-Fi / remote transport for Sony bodies — the product path for the autopilot.

Default live path: Sony A7 II **Smart Remote Control** over the legacy Camera Remote API.
That all-WiFi loop is proven in `wifi_a7ii.py` + `wifi_autopilot.py`: connect, liveview, subject
analysis, exposure/focus, shutter, optional full-res transfer. `WiFiEngine.shoot()` bridges that
path into the FastAPI engine interface and returns the standard `engine.Shot`.

Future modern-body path: Sony **Camera Remote SDK (CRSDK)**, v1.12+ (2024+).
Supported bodies (per Sony, as of 2026-06): Alpha 1, Alpha 9 III, Alpha 7S III, Alpha 7 IV,
    plus ILX-/FX-class and newer Alpha bodies that ship CRSDK firmware. The dev mule (A7 II) is
    NOT a CRSDK body, so it uses the Smart Remote path here.
Untested (REQUIRES the CRSDK binaries + a modern body on hand — none available this session):
    EVERYTHING inside `_crsdk_session()` — i.e. the actual SDK Init/Connect, SetDeviceProperty
    (ISO / F-number / shutter / WB), the shutter-release down/up sequence, the Live View pull, and
    the property-code/value encodings. The binding strategy (ctypes over libCr_Core) and the exact
    CrDeviceProperty codes are modeled from Sony's documented CRSDK surface but have NOT been run.
    The exposure-plan mapping, Shot assembly, save-into-/shots flow, and status()/shoot() interface
    ARE exercisable without hardware (see the verify step in the build notes).

Why CRSDK and not the legacy Camera Remote API
----------------------------------------------
The A7 II is our **dev rig**: either USB-tethered through `RealEngine`, or all-WiFi through the
Smart Remote path wired below.

There are two ways to drive a Sony remotely, and only one survives on modern bodies:

  1. Legacy **Camera Remote API** — HTTP/JSON-RPC over the camera's own Wi-Fi AP (the shape the
     community `pysony` / `sonypy` / `libsonyapi` libraries target: SSDP discovery, then
     `setIsoSpeedRate` / `actTakePicture`, etc.). This rode on the camera's **"Smart Remote
     Embedded" / PlayMemories** app, which Sony **discontinued on 2025-08-31**. On modern bodies
     (a7 IV, a7C II, ZV-E1) that app is not installable, and even where a legacy app lingers it
     **cannot set exposure or focus**. So this path cannot drive the real product — it is exactly
     the gap this module exists to close. (The previous stub modeled this dead-end API; replaced.)

  2. **Camera Remote SDK (CRSDK)** — Sony's official, actively-maintained remote surface. As of
     **v1.12 (2024)** Sony removed the third-party restrictions and added **Wi-Fi** alongside USB
     and wired LAN. It gives all three things the autopilot needs: **set exposure**, **shutter
     release / capture**, and **Live View**. Official support covers a7 IV / a9 III / a1 / a7S III
     and newer. This is the product transport, so this module models CRSDK.

The Python wrinkle (and why the transport is isolated)
------------------------------------------------------
CRSDK ships as **C++ headers + binary libraries** (`libCr_Core` and friends). There is **no
official Python package** — real Python control means a thin ctypes / pybind11 binding around the
shipped shared library (the community "CrSDKHelper" approach). We cannot import or test that without
the SDK binaries AND a modern body, so EVERY SDK call is funneled through a single, clearly-marked
method — `_crsdk_session()` — which raises `CameraError` until verified on hardware. The rest of the
class (interface, intent->plan mapping, Shot assembly) is hardware-independent and runs today.

Same interface as MockEngine / RealEngine in engine.py: `name`, `status() -> dict`,
`shoot(intent) -> Shot`, reusing the `Shot` dataclass. Selected with CAMERA_ENGINE=wifi.
"""

from __future__ import annotations

import os
import tempfile
import shutil
import json
import time
from pathlib import Path

import engine as engine_module
import intent_brain
import wifi_full_res
from engine import Shot, _save_into_shots

# The CRSDK shared library (libCr_Core.so / Cr_Core.dll / libCr_Core.dylib) and the CrAdapter
# plugins must be on disk and discoverable for the real transport. Point this at the unpacked SDK's
# `external/crsdk` (or wherever the libs live) on the product machine. Absent here by design.
CRSDK_LIB_DIR = os.getenv("CRSDK_LIB_DIR")

# How a modern body is reached. CRSDK v1.12+ supports all three; Wi-Fi is the product default.
#   "wifi"     -> camera joined to the same network / its AP, reached by IP.
#   "usb"      -> USB-tethered modern body (also valid under CRSDK, distinct from the A7 II path).
#   "ethernet" -> wired LAN.
CRSDK_TRANSPORT = os.getenv("CRSDK_TRANSPORT", "wifi")

# For the Wi-Fi/Ethernet transports the SDK connects to the camera by IP; for USB it enumerates.
CAMERA_IP = os.getenv("CAMERA_IP")

# Seconds to wait for the camera to answer SDK Connect before giving up.
CONNECT_TIMEOUT_S = 10.0



class CameraError(RuntimeError):
    """Raised when the CRSDK transport can't talk to a real modern Sony body."""


def _plan_exposure_bias(plan: dict) -> str | None:
    """Pull the intent's exposure-bias opinion ('under'|'neutral'|'over') from the mood card, or
    None when the plan carries no mood. Forwarded to the Smart Remote path as exposure compensation."""
    mood = plan.get("mood")
    if isinstance(mood, dict):
        settings = mood.get("settings")
        if isinstance(settings, dict):
            bias = settings.get("exposure_bias")
            if bias is not None:
                return bias
    return None


def _settings_for_target(target: float) -> dict:
    """Pick a starting ISO / aperture / shutter / white balance for a luminance target.

    Hardware-independent heuristic. On a live body these would be clamped to the values the camera
    actually exposes via CRSDK's GetDeviceProperty value-enumerations (ISO / F-number / shutter are
    per-body); here we return a sensible neutral set and nudge ISO for darker/brighter intents. The
    real `_crsdk_session()` is responsible for snapping these to allowed values before applying.
    """
    if target <= 95:        # moody / dark
        iso = "800"
    elif target >= 130:     # bright / airy
        iso = "200"
    else:                   # neutral
        iso = "400"
    return {
        "iso": iso,
        "aperture": "2.8",
        "shutter": "1/60",
        "white_balance": "Auto WB",
    }


def _best_image_source(report: dict, attempt_root: Path | None = None) -> Path | None:
    """Accept the exact downloaded postview in this capture's private folder — or, when the full-res
    pull has PROVEN its identity as this capture (`image_kind == "full_res"` + a verified path set by
    `_apply_full_res_to_report`), the verified full-res JPEG living under the same capture work dir.

    A newest-card file is never accepted on its own: the only way `full_res_verified_path` is set is
    a passing `wifi_full_res.verify_identity`, so this stays a content-proved gate, not a heuristic.
    """
    from image_safety import image_size
    if report.get("image_kind") == "full_res" and report.get("full_res_verified_path"):
        try:
            source = Path(report["full_res_verified_path"]).resolve(strict=True)
            if attempt_root is not None and attempt_root.resolve() not in source.parents:
                return None
            image_size(source)
            return source
        except (KeyError, TypeError, ValueError, OSError):
            return None
    if report.get("postview_downloaded") is not True:
        return None
    try:
        source = Path(report["postview_path"]).resolve(strict=True)
        attempt = Path(report["attempt_dir"]).resolve(strict=True)
        if (source.parent != attempt or source.name != "postview.jpg"
                or report.get("capture_id") != attempt.name):
            return None
        if attempt_root is not None and attempt.parent != attempt_root.resolve():
            return None
        image_size(source)
        return source
    except (KeyError, TypeError, ValueError, OSError):
        return None


def _apply_full_res_to_report(report: dict, pull: dict) -> None:
    """Fold a `wifi_full_res.pull_full_res_for_capture` result into the report the Shot is built from.

    On a verified pull the report is switched to advertise the full-res image (`image_kind`,
    `image_dimensions`, `full_res_verified_path` for `_best_image_source`, and the receipt/identity
    under `full_res`). On an unverified pull the postview is kept and the report says why.
    """
    report["session_resumed"] = pull.get("session_resumed")
    if pull.get("status") == "verified":
        receipt = pull["receipt"]
        report["image_kind"] = "full_res"
        report["image_dimensions"] = [receipt["width"], receipt["height"]]
        report["full_res_verified_path"] = receipt["path"]
        report["full_res"] = {"receipt": receipt, "identity": pull["identity"]}
        report["full_res_status"] = ("verified_by_content_and_time"
                                     if pull.get("identity_basis") == "content+time"
                                     else "verified_by_content")
    else:
        report["full_res"] = {"status": "unverified", "reason": pull.get("reason")}
        report["full_res_status"] = "unverified_postview_kept"


def _shot_settings(report: dict, image_source: Path | None) -> dict:
    """Keep the Shot.settings payload compact, serializable, and useful for the UI sidecar."""
    keys = (
        "recommended",
        "applied_over_wifi",
        "scene_ev100",
        "focal_mm",
        "focal_mm_actual",
        "scene_class",
        "subject_distance_m",
        "af_locked",
        "focus_point_pct",
        "focus_result_source",
        "focus_coordinate_applied",
        "chosen_subject_focus_verified",
        "focus_status",
        "postview",
        "full_res",
        "capture_id", "image_kind", "image_dimensions", "full_res_status",
    )
    settings = {key: report[key] for key in keys if key in report}
    settings["image_source"] = str(image_source) if image_source else None
    return settings


def _intent_not_applied_shot(exc, plan: dict, variant_id: str | None, intent: str | None) -> Shot:
    """Build the truthful fail-closed Shot when a verified write was refused before the shutter.

    `settings.recommended` (what the brain asked for) and `settings.applied` (what actually stuck)
    are kept distinct so the UI never presents an unapplied value as applied.
    """
    report = getattr(exc, "report", {}) or {}
    failed = report.get("failed_setter")
    return Shot(
        ok=False,
        image="",
        summary="Capture refused: the camera did not verifiably apply the requested settings.",
        why=(f"{failed} could not be verified over Smart Remote, so the shutter was not released. "
             f"Recommended {report.get('recommended')!r}; readback: {report.get('readback')!r}."),
        mood=plan.get("mood") or intent_brain.mood_card_from_intent(
            intent, plan["target_brightness"], plan.get("white_balance")),
        settings={
            "recommended": report.get("recommended"),
            "applied": report.get("applied_over_wifi"),
            "failed_setter": failed,
            "failed_field": report.get("failed_field"),
            "readback": report.get("readback"),
            "intent_applied": False,
        },
        applied={"target_brightness": plan.get("target_brightness"), "variant_id": variant_id,
                 "white_balance": None},
        evidence={"source": "smart_remote", "physical_capture": False,
                  "intent_applied": False, "failed_setter": failed, "postview": False},
    )


class WiFiEngine:
    """Drives the A7 II Smart Remote Wi-Fi path, preserving the future CRSDK scaffold.

    Same interface as MockEngine / RealEngine. The live A7 II bridge is in `shoot()`; the CRSDK
    binding (ctypes over libCr_Core) remains isolated in `_crsdk_session()` for modern bodies.
    """

    name = "wifi"

    def __init__(self, full_res: bool | None = None):
        # The transport/target are latched from env at construction so status() can report them
        # honestly even with no SDK present. No SDK handle is created here (that needs hardware).
        self._transport = CRSDK_TRANSPORT
        self._camera_ip = CAMERA_IP
        self._lib_dir = CRSDK_LIB_DIR
        # Full-res escape-the-postview pull. OFF unless explicitly requested or CAMERA_WIFI_FULL_RES=1,
        # so today's postview behavior is unchanged for every existing caller.
        self._full_res = (full_res if full_res is not None
                          else os.getenv("CAMERA_WIFI_FULL_RES") == "1")
        # `sonyremote` lives at the repo root, while the server runs from backend/. RealEngine
        # adds it in its own constructor; the Wi-Fi path needs the same guard for the shared
        # CaptureOutcomeUncertain type it raises on an uncertain shutter.
        import sys
        if str(engine_module.ENGINE_REPO) not in sys.path:
            sys.path.insert(0, str(engine_module.ENGINE_REPO))

    # -- public interface -------------------------------------------------

    def status(self) -> dict:
        """Report the configured Wi-Fi path without probing the camera.

        The live A7 II path is checked on shoot via wifi_a7ii.connect(); status stays fast and
        side-effect-free so it does not hit the camera while the phone polls.
        """
        sdk_present = bool(self._lib_dir) and Path(self._lib_dir).exists()
        return {
            "engine": "wifi",
            "ready": False,            # unknown until shoot() opens the Smart Remote session
            "camera": self._camera_ip,
            "transport": "sony-a7ii-smart-remote",
            "link": "wifi",
            "sdk_present": sdk_present,
            "crsdk_link": self._transport,
            "reason": "A7 II Smart Remote path is used on shoot; status does not probe hardware",
        }

    def shoot(self, intent: str | None, target_override: float | None = None, variant_id: str | None = None, applied_plan: dict | None = None) -> Shot:
        """Take over the A7 II over Wi-Fi, capture, and return the standard Shot.

        The plan comes from the SAME brain the other engines use (`intent_brain.interpret`): plain
        language -> {target_brightness, feel, why, white_balance}. The proven `wifi_autopilot`
        loop does the liveview/vision/exposure/focus/capture work; this method just adapts its
        report into the FastAPI response shape.

        target_override (from the closed reflexion loop) carries a CORRECTED brightness target into
        the plan so a re-shoot aims at the fixed exposure, uniform with the Mock/Real engines.
        """
        import copy
        plan = copy.deepcopy(applied_plan) if applied_plan is not None else (intent_brain.interpret(intent, variant_id=variant_id) if variant_id else intent_brain.interpret(intent))
        if target_override is not None:
            plan["target_brightness"] = target_override
        cam = self._smart_remote_camera()

        import wifi_autopilot

        engine_module.SHOTS_DIR.mkdir(parents=True, exist_ok=True)
        from sonyremote import CaptureOutcomeUncertain
        work_dir = Path(tempfile.mkdtemp(prefix=".wifi-capture-", dir=engine_module.SHOTS_DIR))
        report = None
        source = None
        saved_image = None
        try:
            # Approximate shutter instant for the full-res createdTime window. It is only consulted
            # when the card exposes a per-file createdTime (content+time); otherwise identity is by
            # content only and this value is unused.
            shutter_time = time.time()
            report = wifi_autopilot.shoot(cam, work_dir=str(work_dir),
                                          target_brightness=plan["target_brightness"],
                                          white_balance=plan.get("white_balance"),
                                          motion_intent=plan.get("motion_intent"),
                                          exposure_bias=_plan_exposure_bias(plan))
            with (work_dir / "publication-receipt.json").open("w") as receipt_file:
                json.dump({"state": "publication_pending", "retry_safe": False,
                           "capture_id": report.get("capture_id"),
                           "postview": report.get("postview"),
                           "postview_path": report.get("postview_path")}, receipt_file)
                receipt_file.flush()
                os.fsync(receipt_file.fileno())
            source = _best_image_source(report, Path(work_dir))
            if source is None:
                raise CameraError("Smart Remote returned no verified captured image file.")
            # Escape the postview: only when explicitly enabled, and only after the postview itself is
            # verified, try to pull the full 24 MP JPEG and publish it IN PLACE OF the postview — but
            # exclusively when its identity as this capture is proved by content (`verify_identity`),
            # never by "newest file". An unverified pull keeps the postview and says why.
            if self._full_res and report.get("postview"):
                pull = wifi_full_res.pull_full_res_for_capture(
                    cam, str(source), str(work_dir), shutter_time=shutter_time)
                _apply_full_res_to_report(report, pull)
                promoted = _best_image_source(report, Path(work_dir))
                if promoted is not None:
                    source = promoted
                else:
                    report["image_kind"] = "postview"
                    report["image_dimensions"] = report.get("postview_dimensions") or report.get("image_dimensions")
                    report["full_res_status"] = "unverified_postview_kept"
            saved_image = _save_into_shots(source)
            settings = _shot_settings(report, engine_module.SHOTS_DIR / saved_image)

            image_kind = report.get("image_kind", "postview")
            full_res_verified = image_kind == "full_res"
            evidence = {"source":"smart_remote", "physical_capture":True,
                        "motion":report.get("motion"), "postview":True,
                        "focus_result_source":report.get("focus_result_source"),
                        "focus_coordinate_applied":report.get("focus_coordinate_applied",False),
                        "chosen_subject_focus_verified":report.get("chosen_subject_focus_verified",False),
                        "image_kind":image_kind, "image_dimensions":report.get("image_dimensions"),
                        "capture_id":report.get("capture_id")}
            if report.get("full_res_status"):
                evidence["full_res_status"] = report["full_res_status"]
            if report.get("full_res"):
                evidence["full_res"] = report["full_res"]

            shot = Shot(
                ok=True,
                image=saved_image,
                summary=("Full-resolution JPEG captured over Smart Remote (identity verified). Review the result."
                         if full_res_verified else
                         "Postview captured over Smart Remote. Review the result."),
                why=("Published the verified full-resolution JPEG for this capture "
                     f"({report.get('full_res_status')}); exposure readback and white-balance verification recorded, "
                     "image quality and chosen-subject focus remain unverified."
                     if full_res_verified else
                     "Captured a new Smart Remote postview. Review the recorded exposure readback "
                     "and white-balance verification; image quality and chosen-subject focus remain unverified."),
                mood=plan.get("mood") or intent_brain.mood_card_from_intent(
                    intent,
                    plan["target_brightness"],
                    plan.get("white_balance"),
                ),
                settings=settings,
                cards=report.get("cards"),
                applied={"target_brightness":plan["target_brightness"], "variant_id":variant_id,
                         "white_balance":report.get("white_balance")},
                evidence=evidence,
            )
        except CaptureOutcomeUncertain:
            # The nested attempt contains the only URL/valid-image recovery
            # evidence. A TemporaryDirectory context must not erase it.
            raise
        except Exception as exc:
            # A verified exposure/WB/exposure-comp write failed before the shutter (fail-closed):
            # no capture, no receipt, so the work dir is safe to remove. Report the refusal honestly
            # rather than advertising settings the camera never applied. (getattr keeps this robust
            # when wifi_autopilot is a lightweight test stub without the exception attribute.)
            if report is None and isinstance(exc, getattr(wifi_autopilot, "IntentNotApplied", ())):
                shutil.rmtree(work_dir, ignore_errors=True)
                return _intent_not_applied_shot(exc, plan, variant_id, intent)
            if report is not None:
                preserved = (engine_module.SHOTS_DIR / saved_image) if saved_image else source
                receipt = work_dir / "publication-receipt.json"
                error = CaptureOutcomeUncertain(
                    f"Smart Remote publication failed. Recovery data retained at {work_dir}; {exc}",
                    capture_path=preserved)
                error.receipt_path = str(receipt)
                raise error from exc
            shutil.rmtree(work_dir)
            raise
        shutil.rmtree(work_dir, ignore_errors=True)
        return shot

    def _smart_remote_camera(self):
        """Connect to the A7 II Smart Remote API, raising CameraError if it is unreachable."""
        import wifi_a7ii

        cam = wifi_a7ii.connect()
        if not cam:
            raise CameraError(
                "No A7 II Smart Remote camera found. Launch Smart Remote Control on the camera, "
                "join its DIRECT-* Wi-Fi network from this host, then retry."
            )
        return cam

    # -- the ONLY hardware-bound method -----------------------------------
    # ======================================================================
    # !!! UNTESTED — REQUIRES CRSDK BINARIES + A MODERN SONY BODY ON HAND !!!
    # Every real SDK call lives here and nowhere else. Until this is run against
    # an a7 IV (or sibling) with the SDK libs present, it raises CameraError. The
    # body below is the documented CRSDK flow, kept as the implementation TODO.
    # ======================================================================
    def _crsdk_session(self, settings: dict, wb_kelvin: int | None) -> Path:
        """Open a CRSDK session, apply exposure, release the shutter, fetch the frame.

        Returns the Path to the captured/postview JPEG on disk. Raises CameraError until verified.

        Documented CRSDK sequence this will implement (ctypes over libCr_Core):
          1. Load the SDK lib from CRSDK_LIB_DIR (ctypes.CDLL) and call Init().
          2. EnumCameraObjects() for USB, or CreateCameraObjectInfoEthernetConnection(CAMERA_IP)
             for the wifi/ethernet transports; then Connect() and wait <= CONNECT_TIMEOUT_S.
          3. SetDeviceProperty for each of: CrDeviceProperty_IsoSensitivity (settings["iso"]),
             CrDeviceProperty_FNumber (settings["aperture"]), CrDeviceProperty_ShutterSpeed
             (settings["shutter"]), and white balance — either CrDeviceProperty_WhiteBalance for a
             preset or CrDeviceProperty_ColortempIndicate (wb_kelvin) for a Kelvin mood. Each value
             must first be snapped to the body's enumerated allowed values from GetDeviceProperty.
          4. SendCommand(CrCommandId_Release, Down) then (Release, Up) to trigger the shutter.
          5. Pull the frame: either GetLiveViewImage for a quick postview, or wait for the
             content-transfer callback and download the saved JPEG.
          6. Disconnect() and Release() the SDK — always, even on error.

        Why it raises instead of trying: CRSDK is C++ binaries with NO official Python package, so
        there is nothing to import without the SDK on disk, and nothing to talk to without a CRSDK
        body. Both are absent in dev. Keeping this single guarded method means the rest of the
        autopilot (intent brain, Shot assembly, the engine interface) is fully exercisable now, and
        wiring the real transport later touches only this one place.
        """
        # import ctypes
        target = self._camera_ip or "an enumerated USB device"
        raise CameraError(
            f"CRSDK transport over {self._transport!r} to {target} is unverified — needs the "
            "Camera Remote SDK binaries (CRSDK_LIB_DIR) and a modern Sony body (a7 IV / a9 III / "
            "a1 / a7S III) on hand. The A7 II dev mule uses the USB RealEngine path instead."
        )
        # TODO(hardware): implement the documented CRSDK sequence above; return the JPEG Path.


if __name__ == "__main__":
    # Smoke check that needs no hardware: the engine constructs and reports honestly.
    engine = WiFiEngine()
    print("status:", engine.status())
