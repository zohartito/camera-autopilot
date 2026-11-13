"""Resolve a body+lens *rig* to the sensor/noise/lens calibration path it actually earns.

This replaces the blanket "A7 II is calibrated, everything else is uncalibrated" gate with a
data-driven decision that is honest about *what* is known for the mounted rig:

  - A7 II + its profiled Tamron 28-75 -> the existing measured electron-noise + optics profile.
  - A7 III + a measured JPEG-noise table (data/sensor_profiles/ilce-7m3.json) -> a real, if
    JPEG-domain, noise ceiling on top of the manufacturer's spec geometry.
  - A7 III with no table -> spec geometry only (IBIS, base ISO), noise unknown.
  - Anything else (or A7 II without its matched lens) -> the conservative uncalibrated policy.

Pure/deterministic and camera-free: it reads EXIF values already pulled off the metering JPEG
plus, optionally, an on-disk profile JSON. It never opens a camera.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field, replace
from math import isfinite, log2
from pathlib import Path

import lens_optics
import physics
import sensor_noise

_DATA_DIR = Path(__file__).resolve().parent.parent / "data" / "sensor_profiles"

# Body-string aliases seen from the driver/EXIF -> canonical body id.
_BODY_ALIASES = {
    "sony ilce-7m2": "ILCE-7M2", "sony ilce-7m2 (control)": "ILCE-7M2", "sony a7 ii": "ILCE-7M2",
    "ilce-7m2": "ILCE-7M2",
    "sony alpha-a7 iii": "ILCE-7M3", "sony alpha-a7 iii (pc control)": "ILCE-7M3",
    "sony ilce-7m3": "ILCE-7M3", "ilce-7m3": "ILCE-7M3",
}

_F_NUMBER_PREFIX = re.compile(r"\bf\s*/\s*(?=\d)")     # "f/2.8", "f / 2.8" -> "f2.8"
_FOCAL_MM = re.compile(r"(?<=\d)\s*mm\b")               # "28-75mm", "28-75 mm" -> "28-75"


def normalize_lens(name) -> str:
    """One comparable form for a lens name: casefolded, whitespace collapsed, the aperture written
    "f2.8" whatever the spelling ("F2.8", "f2.8", "f/2.8", "F/2.8") and the focal range without "mm",
    so a user's spelling and the body's EXIF strings compare equal."""
    text = " ".join(str(name or "").casefold().split())
    return _FOCAL_MM.sub("", _F_NUMBER_PREFIX.sub("f", text))


# Lens names for which a measured optics/MTF+transmission profile exists (only the Tamron so far).
# "E 28-75mm F2.8-2.8" is the Sony body's EXIF LensModel form of the mounted Tamron 28-75 f/2.8;
# it is corroborated by LensSpecification 28-75 / f2.8-2.8, so it maps to the same profiled lens.
GENERIC_SONY_TAMRON_ALIAS = normalize_lens("E 28-75mm F2.8-2.8")
# The LensSpecification that corroborates the generic form (28-75 mm, f/2.8-2.8).
GENERIC_SONY_TAMRON_SPEC = (28.0, 75.0, 2.8, 2.8)


def _spec_tuple(lens_specification):
    try:
        values = tuple(round(float(v), 2) for v in lens_specification)
    except (TypeError, ValueError):
        return None
    return values if len(values) == 4 else None


# Accepted spellings (any aperture form, with or without "mm"; compared after normalize_lens).
# "Tamron 28-75mm F2.8 Di III RXD" is the A036's EXIF LensID; "E 28-75mm F2.8-2.8" its LensModel.
PROFILED_TAMRON_SPELLINGS = ("Tamron 28-75mm F2.8", "Tamron 28-75mm F2.8 Di III RXD",
                             "28-75mm F2.8 Di III RXD", "E 28-75mm F2.8-2.8")
PROFILED_TAMRON_NAMES = {normalize_lens(name) for name in PROFILED_TAMRON_SPELLINGS}
OTHER_KNOWN_LENS_SPELLINGS = ("FE 24-105mm F4 G OSS",)

# Known lens geometry: widest f (min_f), narrowest f (max_f), focal range. Spec-sourced.
_TAMRON_28_75 = ("Tamron 28-75mm F2.8", 2.8, 22.0, (28.0, 75.0))
_LENS_REGISTRY = {
    **{name: _TAMRON_28_75 for name in PROFILED_TAMRON_NAMES},
    normalize_lens("FE 24-105mm F4 G OSS"): ("Sony FE 24-105mm F4 G OSS", 4.0, 22.0, (24.0, 105.0)),
}


# Published lens transmission (T-stop) measurements, keyed by the canonical lens id above. A lens
# absent here is `unknown` — no transmission is ever assumed. The Tamron 28-75 has no published
# T-stop (DxOMark lists it untested, checked 2026-09-27).
_LENS_TRANSMISSION = {
    "Sony FE 24-105mm F4 G OSS": lens_optics.FE_24_105_F4_G_TRANSMISSION,
}


def lens_transmission(lens_id) -> lens_optics.LensTransmission:
    """The mounted lens's published transmission, or the explicit unknown record."""
    return _LENS_TRANSMISSION.get(lens_id, lens_optics.UNKNOWN_TRANSMISSION)


def is_profiled_tamron(lens_model) -> bool:
    """True when the mounted lens is the profiled Tamron 28-75 (in any known EXIF form)."""
    return normalize_lens(lens_model) in PROFILED_TAMRON_NAMES


def body_id(model) -> str | None:
    return _BODY_ALIASES.get(normalize_lens(model))


@dataclass(frozen=True)
class LensLimits:
    min_f: float | None                       # widest opening (smallest f-number)
    max_f: float | None                       # narrowest opening (largest f-number)
    focal_range: tuple[float, float] | None


@dataclass(frozen=True)
class RigProfile:
    body_id: str | None
    sensor: physics.SensorProfile | None
    noise: object | None                      # NoiseModel | MeasuredJpegNoise | None
    lens_id: str | None
    lens_limits: LensLimits
    ibis: bool
    calibration_status: str                   # existing_profile|measured_jpeg_noise|geometry_only|uncalibrated
    sources: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    lens_profiled: bool = False               # a measured lens optics/MTF profile exists for this lens
    lens_transmission: lens_optics.LensTransmission = lens_optics.UNKNOWN_TRANSMISSION


def _min_f_from_exif(lens_specification, max_aperture_value) -> float | None:
    """Widest usable f-number implied by EXIF: LensSpecification's max-aperture pair, or the
    MaxApertureValue APEX. Identifies the opening, never the smallest physical aperture."""
    candidates: list[float] = []
    try:
        if lens_specification is not None and len(lens_specification) == 4:
            values = [float(v) for v in lens_specification[2:]]
            if all(isfinite(v) and 0.5 <= v <= 128 for v in values):
                candidates.append(max(values))
    except (TypeError, ValueError, ZeroDivisionError, OverflowError):
        pass
    try:
        apex = float(max_aperture_value)
        if isfinite(apex) and -2 <= apex <= 14:
            value = 2 ** (apex / 2)
            if 0.5 <= value <= 128:
                candidates.append(value)
    except (TypeError, ValueError, ZeroDivisionError, OverflowError):
        pass
    return min(candidates) if candidates else None


def _focal_range_from_exif(lens_specification) -> tuple[float, float] | None:
    try:
        if lens_specification is not None and len(lens_specification) == 4:
            lo, hi = float(lens_specification[0]), float(lens_specification[1])
            if all(isfinite(v) and 0 < v <= 2000 for v in (lo, hi)) and lo <= hi:
                return (lo, hi)
    except (TypeError, ValueError, ZeroDivisionError, OverflowError):
        pass
    return None


def lens_is_profiled_tamron(lens_model, lens_specification=None) -> bool:
    """True when the mounted lens is the profiled Tamron 28-75 (A036) by any accepted spelling.

    Sony bodies write the generic "E 28-75mm F2.8-2.8" for third-party 28-75/2.8 lenses: that form
    counts only when LensSpecification corroborates 28-75 f/2.8-2.8. The maker-note LensID
    (exiftool) is the authoritative discriminator against the G2 and is not read here.
    """
    norm = normalize_lens(lens_model)
    if norm == GENERIC_SONY_TAMRON_ALIAS:
        return _spec_tuple(lens_specification) == GENERIC_SONY_TAMRON_SPEC
    return norm in PROFILED_TAMRON_NAMES


def _resolve_lens(lens_model, lens_specification, max_aperture_value):
    """Return (lens_id, LensLimits, profiled)."""
    norm = normalize_lens(lens_model)
    if norm in _LENS_REGISTRY:
        name, min_f, max_f, focal = _LENS_REGISTRY[norm]
        profiled = lens_is_profiled_tamron(lens_model, lens_specification)
        return name, LensLimits(min_f, max_f, focal), profiled
    # Unknown lens: derive what EXIF honestly supports; narrowest aperture stays unknown.
    limits = LensLimits(_min_f_from_exif(lens_specification, max_aperture_value), None,
                        _focal_range_from_exif(lens_specification))
    return (str(lens_model).strip() or None) if lens_model else None, limits, False


def _load_measured(body: str, profiles_dir: Path | None):
    directory = profiles_dir if profiles_dir is not None else _DATA_DIR
    path = Path(directory) / f"{body.lower()}.json"
    if not path.is_file():
        return None
    import json
    return sensor_noise.load_measured_noise(json.loads(path.read_text()))


def resolve_rig(model, *, lens_model=None, lens_specification=None, max_aperture_value=None,
                profiles_dir: Path | None = None) -> RigProfile:
    """Resolve the mounted body+lens to its earned calibration path. `profiles_dir` overrides the
    default data/sensor_profiles location (used by tests)."""
    rig = _resolve_rig(model, lens_model=lens_model, lens_specification=lens_specification,
                       max_aperture_value=max_aperture_value, profiles_dir=profiles_dir)
    # Transmission belongs to the glass, not the body, so it follows the lens on every path.
    return replace(rig, lens_transmission=lens_transmission(rig.lens_id))


def _resolve_rig(model, *, lens_model, lens_specification, max_aperture_value,
                 profiles_dir: Path | None) -> RigProfile:
    body = body_id(model)
    lens_id, lens_limits, profiled = _resolve_lens(lens_model, lens_specification, max_aperture_value)

    if body is None:
        return RigProfile(None, None, None, lens_id, lens_limits, False, "uncalibrated",
                          ["unsupported_body"],
                          ["Unsupported body: no sensor geometry or noise profile is available."],
                          lens_profiled=False)

    if body == "ILCE-7M2":
        sensor = physics.A7II
        if profiled:
            return RigProfile(
                body, sensor, sensor_noise.A7II_NOISE, lens_id, lens_limits, True,
                "existing_profile",
                ["a7ii_electron_noise_model", "tamron_28_75_optics_profile"],
                ["A7 II with its profiled Tamron 28-75: existing measured sensor-noise and optics "
                 "calibration."],
                lens_profiled=True)
        # A7 II calibration is lens-matched; a different/unknown lens does not inherit its noise.
        return RigProfile(
            body, sensor, None, lens_id, lens_limits, True, "uncalibrated",
            ["a7ii_body_without_matching_lens"],
            ["A7 II sensor noise is only applied with its profiled lens; this lens is unverified, "
             "so the conservative uncalibrated policy is used."],
            lens_profiled=False)

    # ILCE-7M3
    sensor = physics.A7III
    measured = _load_measured(body, profiles_dir)
    if measured is not None:
        ceiling = measured.max_usable_iso()
        sources = ["a7iii_spec_geometry", "measured_jpeg_flat_patch_noise"]
        if profiled:
            sources.append("tamron_28_75_optics_profile")
        return RigProfile(
            body, sensor, measured, lens_id, lens_limits, True, "measured_jpeg_noise", sources,
            [f"A7 III with measured JPEG-domain noise (usable-ISO ceiling {ceiling}); geometry is "
             "spec-derived and no dynamic-range or lens MTF is measured."],
            lens_profiled=profiled)
    return RigProfile(
        body, sensor, None, lens_id, lens_limits, True, "geometry_only",
        ["a7iii_spec_geometry"],
        ["A7 III geometry is manufacturer spec, not measured; no sensor-noise or lens MTF "
         "calibration. Run tools/calibrate_sensor_noise.py on an ISO-ladder to add a measured "
         "table."],
        lens_profiled=profiled)


# --- self-test (run: python backend/rig_profile.py) -----------------------
def _selftest() -> None:
    a7ii = resolve_rig("Sony ILCE-7M2 (Control)", lens_model="E 28-75mm F2.8-2.8",
                       lens_specification=(28, 75, 2.8, 2.8), max_aperture_value=2.96875)
    assert a7ii.calibration_status == "existing_profile", a7ii
    assert a7ii.lens_limits.min_f == 2.8 and a7ii.lens_limits.max_f == 22.0, a7ii.lens_limits
    assert a7ii.noise is sensor_noise.A7II_NOISE and a7ii.lens_profiled

    a7ii_other = resolve_rig("Sony ILCE-7M2 (Control)", lens_model="FE 50mm F1.8")
    assert a7ii_other.calibration_status == "uncalibrated" and a7ii_other.noise is None

    a7iii = resolve_rig("Sony Alpha-A7 III (PC Control)", lens_model="FE 24-105mm F4 G OSS",
                        lens_specification=(24, 105, 4, 4))
    assert a7iii.calibration_status == "geometry_only", a7iii
    assert a7iii.lens_limits.min_f == 4.0 and a7iii.lens_limits.max_f == 22.0, a7iii.lens_limits
    assert a7iii.noise is None and a7iii.sensor is physics.A7III and a7iii.ibis

    unknown = resolve_rig("Nikon Z6")
    assert unknown.calibration_status == "uncalibrated" and unknown.sensor is None

    print("rig-profile self-test: PASS")
    print(f"  A7II+Tamron -> {a7ii.calibration_status}; A7III+24-105 -> {a7iii.calibration_status} "
          f"(limits {a7iii.lens_limits.min_f}-{a7iii.lens_limits.max_f})")


if __name__ == "__main__":
    _selftest()
