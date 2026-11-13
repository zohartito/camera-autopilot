"""Recommendation function for body-shutter (handheld) sessions.

Mirrors the metering/recommendation block of ``engine.RealEngine._brain_shoot`` without
issuing any shutter: the frame the user just shot is metered from its own EXIF exposure,
the mounted rig is resolved to the calibration path it earns, and the brain returns the
exposure for the NEXT frame. Everything is bounded to the body's live ladders and the
enabled transport shutter policy, exactly like an ordinary capture.
"""
from __future__ import annotations

import math

import brain
import camera_caps
from intent import motion_intent_floor_s
import live_verify
import rig_profile
from image_safety import open_image
from sonyremote.camera import SONY_ACTION_MAX_ENABLED_SHUTTER_S


def _frame_exposure_exif(jpeg_path: str) -> tuple[str, object, object, float | None, float | None, float | None]:
    with open_image(jpeg_path) as image:
        exif = image.getexif()
        ifd = exif.get_ifd(34665) if 34665 in exif else exif
        lens_model = str(ifd.get(42036, exif.get(42036, ""))).strip()
        lens_specification = ifd.get(42034, exif.get(42034))
        max_aperture_value = ifd.get(37381, exif.get(37381))
        iso = ifd.get(34855, exif.get(34855))
        f_number = ifd.get(33437, exif.get(33437))
        shutter = ifd.get(33434, exif.get(33434))
    to_float = lambda value: float(value) if value not in (None, "") else None  # noqa: E731
    return lens_model, lens_specification, max_aperture_value, to_float(iso), to_float(f_number), to_float(shutter)


def make_recommend_fn(model: str, iso_choices: list[str], aperture_choices: list[str],
                      shutter_choices: list[str], *, target_brightness: float = 112.0,
                      motion_intent: str | None = None):
    """Build ``recommend_fn(jpeg_path, current_settings, lens_exif) -> dict`` for HandheldSession."""
    if not 0 < float(target_brightness) <= 255:
        raise ValueError("target_brightness must be in (0, 255]")

    def recommend(jpeg_path: str, current_settings: dict, _lens_exif: dict) -> dict:
        lens_model, lens_specification, max_aperture_value, iso, f_number, shutter_s = _frame_exposure_exif(jpeg_path)
        if iso is None or f_number is None or shutter_s is None or shutter_s <= 0:
            raise ValueError("Frame EXIF lacks ISO/aperture/shutter; the frame cannot be metered")
        rig = rig_profile.resolve_rig(model, lens_model=lens_model, lens_specification=lens_specification,
                                      max_aperture_value=max_aperture_value)
        scene_ev100, clipped = live_verify.meter_ev100(jpeg_path, iso, f_number, shutter_s)
        scene_ev100 -= 2.2 * math.log2(float(target_brightness) / 112.0)
        focal = live_verify.focal_from_exif(jpeg_path, default=None)
        floor = motion_intent_floor_s(motion_intent)
        caps = camera_caps.from_live_choices(model, iso_choices, aperture_choices, shutter_choices)
        caps, transport_limits = camera_caps.constrain_capture_duration(
            caps, max_shutter_s=SONY_ACTION_MAX_ENABLED_SHUTTER_S)
        current_aperture = str(current_settings.get("aperture") or f"f/{f_number:g}")
        caps, mounted_lens_limits = camera_caps.constrain_mounted_lens(
            caps, current_aperture, lens_specification=lens_specification,
            max_aperture_value=max_aperture_value,
            verified_profile=(2.8, 22.0) if rig.lens_profiled else None)
        if rig.calibration_status == "existing_profile":
            rec = brain.recommend_settings(jpeg_path, scene_ev100, focal_mm=focal, caps=caps,
                                           subject_motion_floor_s=floor, fallback_aperture=f_number)
            source = "a7ii_physics_taste_unavailable" if brain.taste_fell_back(rec) else "a7ii_physics_retrieval"
        else:
            rec = brain.recommend_live(jpeg_path, scene_ev100, model, caps, f_number, focal,
                                       subject_motion_floor_s=floor, rig=rig)
            source = ("a7iii_measured_jpeg_noise_physics" if rig.calibration_status == "measured_jpeg_noise"
                      else "live_capabilities_conservative_physics")
        recommended_f = float(str(rec.aperture_label).lower().removeprefix("f/"))
        if camera_caps._shutter_seconds(rec.shutter_label) > SONY_ACTION_MAX_ENABLED_SHUTTER_S:
            raise ValueError("Recommended shutter exceeds the enabled transport policy; no adjustment")
        if not any(abs(recommended_f - value) < 1e-6 for value in caps.apertures):
            raise ValueError("Recommended aperture is outside verified mounted-lens limits; no adjustment")
        why = [str(note) for note in (getattr(rec, "notes", None) or [])][:8]
        return {
            "iso_label": str(rec.iso_label), "aperture_label": str(rec.aperture_label),
            "shutter_label": str(rec.shutter_label), "why": why, "source": source,
            "scene_ev100": round(float(scene_ev100), 3), "clipped": clipped,
            "metered_from": {"iso": iso, "f_number": f_number, "shutter_s": shutter_s},
            "calibration_status": rig.calibration_status, "lens_id": rig.lens_id,
            "mounted_lens_limits": mounted_lens_limits, "capture_transport_limits": transport_limits,
        }

    return recommend


__all__ = ["make_recommend_fn"]
