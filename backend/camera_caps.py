"""Camera capability ladders — snap the brain's continuous (iso, aperture, shutter) onto the
values the body actually offers, so a recommendation is *settable*, not a fantasy like ISO 297.

The ladders below are the Sony A7 II's real enumerated options (read live via the sony-camera
MCP `list_setting_options`). Snapping is done in stops (log space), since exposure is
logarithmic. Each snap returns (value, label) — `label` is the exact string `set_exposure`
expects (e.g. shutter "1/30" or "8/10" for 0.8s).
"""
from __future__ import annotations

from dataclasses import dataclass
from math import log2

import physics


def _shutter_seconds(label: str) -> float:
    """Parse a Sony shutter label to seconds: '1/30' -> 0.0333, '30' -> 30, '8/10' -> 0.8."""
    if "/" in label:
        num, den = label.split("/")
        return float(num) / float(den)
    return float(label)


# A7 II native ladders, numeric options only (Auto / Multi-Frame-NR variants dropped). Read live.
_A7II_ISO = [50, 64, 80, 100, 125, 160, 200, 250, 320, 400, 500, 640, 800, 1000, 1250, 1600,
             2000, 2500, 3200, 4000, 5000, 6400, 8000, 10000, 12800, 16000, 20000, 25600,
             32000, 40000, 51200]
_A7II_APERTURE = [1.0, 1.1, 1.2, 1.4, 1.6, 1.8, 2.0, 2.2, 2.5, 2.8, 3.2, 3.5, 4.0, 4.5, 5.0,
                  5.6, 6.3, 7.1, 8.0, 9.0, 10.0, 11.0, 13.0, 14.0, 16.0, 18.0, 20.0, 22.0,
                  25.0, 29.0, 32.0]
_A7II_SHUTTER_LABELS = ["30", "25", "20", "15", "13", "10", "8", "6", "5", "4", "32/10",
                        "25/10", "2", "16/10", "13/10", "1", "8/10", "6/10", "5/10", "4/10",
                        "1/3", "1/4", "1/5", "1/6", "1/8", "1/10", "1/13", "1/15", "1/20",
                        "1/25", "1/30", "1/40", "1/50", "1/60", "1/80", "1/100", "1/125",
                        "1/160", "1/200", "1/250", "1/320", "1/400", "1/500", "1/640", "1/800",
                        "1/1000", "1/1250", "1/1600", "1/2000", "1/2500", "1/3200", "1/4000",
                        "1/5000", "1/6400", "1/8000"]


@dataclass(frozen=True)
class CameraCapabilities:
    """The body+lens's real, settable ISO / aperture / shutter ladders."""
    name: str
    isos: list[int]
    apertures: list[float]          # f-numbers, already clipped to the mounted lens
    shutters: list[float]           # seconds, parallel to shutter_labels
    shutter_labels: list[str]
    aperture_labels: list[str] | None = None

    def snap_iso(self, iso: float) -> tuple[int, str]:
        value = min(self.isos, key=lambda v: abs(log2(v) - log2(max(iso, 1.0))))
        return value, str(value)

    def snap_aperture(self, f_number: float) -> tuple[float, str]:
        value = min(self.apertures, key=lambda v: abs(log2(v) - log2(max(f_number, 0.5))))
        label = (self.aperture_labels[self.apertures.index(value)]
                 if self.aperture_labels else str(value))
        return value, label

    def snap_shutter(self, shutter_s: float) -> tuple[float, str]:
        idx = min(range(len(self.shutters)),
                  key=lambda i: abs(log2(self.shutters[i]) - log2(max(shutter_s, 1e-6))))
        return self.shutters[idx], self.shutter_labels[idx]


def for_lens(lens: physics.LensProfile) -> CameraCapabilities:
    """A7 II ladders with the aperture list clipped to the mounted lens's usable range."""
    apertures = [f for f in _A7II_APERTURE
                 if lens.min_aperture - 1e-6 <= f <= lens.max_aperture + 1e-6]
    shutters = [_shutter_seconds(s) for s in _A7II_SHUTTER_LABELS]
    return CameraCapabilities("Sony A7 II", list(_A7II_ISO), apertures, shutters,
                              list(_A7II_SHUTTER_LABELS))


A7II_CAPS = for_lens(physics.TAMRON_28_75)  # the dev rig


# --- self-test (run: python backend/camera_caps.py) -----------------------
def _selftest() -> None:
    caps = A7II_CAPS
    # aperture clipped to the Tamron f/2.8-22
    assert caps.apertures[0] == 2.8 and caps.apertures[-1] == 22.0, caps.apertures
    # snapping happens in stops, onto real ladder values
    assert caps.snap_iso(297) == (320, "320"), caps.snap_iso(297)
    assert caps.snap_iso(796) == (800, "800"), caps.snap_iso(796)
    assert caps.snap_iso(151) == (160, "160"), caps.snap_iso(151)
    assert caps.snap_aperture(5.6) == (5.6, "5.6"), caps.snap_aperture(5.6)
    assert caps.snap_aperture(21.38)[0] == 22.0, caps.snap_aperture(21.38)
    assert caps.snap_shutter(1 / 29)[1] == "1/30", caps.snap_shutter(1 / 29)
    assert caps.snap_shutter(0.5)[1] == "5/10", caps.snap_shutter(0.5)
    assert caps.snap_shutter(3.0)[1] == "32/10", caps.snap_shutter(3.0)  # 3s -> 3.2s label

    print("camera-caps self-test: PASS")
    print(f"  ISO 297->{caps.snap_iso(297)[1]}, 796->{caps.snap_iso(796)[1]}, 151->{caps.snap_iso(151)[1]}")
    print(f"  f/21.38->f/{caps.snap_aperture(21.38)[0]};  1/29s->{caps.snap_shutter(1/29)[1]}, "
          f"0.5s->{caps.snap_shutter(0.5)[1]}")


if __name__ == "__main__":
    _selftest()


def from_live_choices(model: str, iso_choices: list[str], aperture_choices: list[str],
                      shutter_choices: list[str]) -> CameraCapabilities:
    """Build only from this connected body's enumeration; never borrow another body's ladder.

    Auto, bulb, unknown values and Sony's invalid placeholder are excluded. A missing
    aperture ladder (for example a manual lens) is explicit rather than invented.
    """
    from math import isfinite

    isos = []
    for label in iso_choices:
        text = str(label).strip()
        if text.isdigit() and 0 < int(text) <= 409600:
            isos.append(int(text))
    apertures, aperture_labels = [], []
    for label in aperture_choices:
        try:
            value = float(str(label).strip().lower().removeprefix("f/"))
        except ValueError:
            continue
        if isfinite(value) and 0.5 <= value <= 128:
            apertures.append(value)
            aperture_labels.append(str(label))
    shutters, shutter_labels = [], []
    for label in shutter_choices:
        if str(label).strip() == "65535/65535":
            continue
        try:
            value = _shutter_seconds(str(label).strip())
        except (ValueError, ZeroDivisionError):
            continue
        if isfinite(value) and 0 < value <= 30:
            shutters.append(value)
            shutter_labels.append(str(label))
    if not isos or not apertures or not shutters:
        raise ValueError("Live numeric ISO, aperture and shutter choices are required")
    return CameraCapabilities(model, sorted(set(isos)), apertures, shutters,
                              shutter_labels, aperture_labels=aperture_labels)


def model_profile(model: str) -> dict:
    """Evidence metadata, deliberately separate from sensor calibration parameters."""
    a7ii = model in {"Sony ILCE-7M2", "Sony ILCE-7M2 (Control)", "Sony A7 II"}
    a7iii = model in {"Sony Alpha-A7 III", "Sony Alpha-A7 III (PC Control)", "Sony ILCE-7M3"}
    return {
        "model": model,
        "supported_body": a7ii or a7iii,
        "metering": "existing_a7ii_physics" if a7ii else "live_jpeg_feedback",
        "calibration_status": "existing_profile" if a7ii else "uncalibrated",
        "noise_model": "A7II_NOISE" if a7ii else None,
        "lens_profile": "requires_mounted_lens_identification",
        "capability_source": "live_choices_required",
        "usable_iso_ceiling": None,
        "notes": ([] if a7ii else [
            "No measured sensor noise or lens transmission calibration is available.",
            "A7 II noise, lens and stabilization parameters must not be reused.",
            "Numeric live choices establish settable values, not image quality limits.",
        ]),
    }


def constrain_mounted_lens(caps: CameraCapabilities, current_aperture: str, *,
                           lens_specification=None, max_aperture_value=None,
                           verified_profile: tuple[float, float] | None = None):
    """Intersect driver ranges with the lens on the accepted metering JPEG.

    LensSpecification and MaxApertureValue identify maximum opening, not the
    smallest physical aperture. Without a verified lens profile we therefore
    never stop down beyond the currently confirmed aperture. Missing or
    conflicting metadata retains only that confirmed aperture.
    """
    from math import isfinite
    from dataclasses import replace
    current = float(str(current_aperture).strip().lower().removeprefix("f/"))
    if not isfinite(current) or not .5 <= current <= 128:
        raise ValueError("confirmed mounted-lens aperture must be finite")
    lower = []
    sources = []
    try:
        if lens_specification is not None and len(lens_specification) == 4:
            candidates = [float(value) for value in lens_specification[2:]]
            if all(isfinite(value) and .5 <= value <= 128 for value in candidates):
                lower.append(max(candidates))
                sources.append("accepted_jpeg_lens_specification")
    except (TypeError, ValueError, ZeroDivisionError, OverflowError):
        pass
    try:
        apex = float(max_aperture_value)
        if isfinite(apex) and -2 <= apex <= 14:
            value = 2 ** (apex / 2)
            if .5 <= value <= 128:
                lower.append(value)
                sources.append("accepted_jpeg_max_aperture_value")
    except (TypeError, ValueError, ZeroDivisionError, OverflowError):
        pass
    upper = current
    if verified_profile is not None:
        profile_min, profile_max = map(float, verified_profile)
        if not all(isfinite(value) for value in (profile_min, profile_max)) or not .5 <= profile_min <= profile_max <= 128:
            raise ValueError("invalid verified lens aperture profile")
        lower.append(profile_min)
        upper = profile_max
        sources.append("verified_lens_profile")
    minimum = max(lower) if lower else current
    # EXIF APEX is quantized; permit 1% rounding at a known f-stop boundary.
    conflict = minimum > current * 1.01 or current > upper * 1.01
    if not lower or conflict:
        minimum = upper = current
        sources = ["current_readback_only"]
    selected = [i for i, value in enumerate(caps.apertures)
                if minimum / 1.01 <= value <= upper * 1.01]
    if sources == ["current_readback_only"]:
        selected = [i for i, value in enumerate(caps.apertures) if abs(value - current) < 1e-6]
    values = [caps.apertures[i] for i in selected]
    labels = [caps.aperture_labels[i] if caps.aperture_labels else str(caps.apertures[i]) for i in selected]
    if not values:
        values, labels = [current], [str(current_aperture)]
        sources = ["current_readback_only"]
    evidence = {"sources": sources, "raw_driver_apertures": list(caps.apertures),
                "usable_apertures": values, "usable_aperture_labels": labels,
                "current_confirmed_aperture": current, "metadata_conflict": conflict,
                "minimum_f_number": minimum, "maximum_f_number": upper,
                "unverified_stop_down_beyond_current_blocked": verified_profile is None}
    return replace(caps, apertures=values, aperture_labels=labels), evidence


ELECTRONIC_STOP_DOWN_LIMIT = 16.0   # f-number; nearly every electronic full-frame lens reaches it


def electronic_stop_down(caps: CameraCapabilities, lens_limits: dict, *, lens_model: str | None,
                         limit_f_number: float = ELECTRONIC_STOP_DOWN_LIMIT):
    """Aperture choices past the confirmed one for an unprofiled lens that controls its aperture
    electronically, or (None, reason) when that is not shown.

    `caps` carries the body's listed apertures (raw, before `constrain_mounted_lens`); `lens_limits`
    is that function's evidence. Electronic control is shown when the lens names itself in the
    meter JPEG (LensModel), its maximum opening came from the JPEG lens metadata without conflict,
    and the body reported a real f-number (a manual or adapted lens reads f/0 and never gets here).
    The listed values are the body's generic ladder, not the lens's range, so this only PLANS a
    stop-down: the caller must read the aperture back before any shutter and drop to the confirmed
    aperture when it does not match. Never past `limit_f_number`.
    """
    from dataclasses import replace
    evidence = {"basis": "electronic_lens_readback", "limit_f_number": limit_f_number,
                "lens_model": lens_model or None}
    sources = list(lens_limits.get("sources") or [])
    if "verified_lens_profile" in sources:
        return None, {**evidence, "status": "not_needed", "reason": "a verified lens profile sets the range"}
    if not (lens_model or "").strip():
        return None, {**evidence, "status": "not_shown",
                      "reason": "the meter JPEG names no lens, so electronic aperture control is not shown"}
    if lens_limits.get("metadata_conflict") or not any(
            source in sources for source in ("accepted_jpeg_lens_specification",
                                             "accepted_jpeg_max_aperture_value")):
        return None, {**evidence, "status": "not_shown",
                      "reason": "the meter JPEG's lens aperture metadata is missing or disagrees with the body"}
    minimum = float(lens_limits["minimum_f_number"])
    current = float(lens_limits["current_confirmed_aperture"])
    labels = caps.aperture_labels or [str(value) for value in caps.apertures]
    selected = [(value, label) for value, label in zip(caps.apertures, labels)
                if minimum / 1.01 <= value <= limit_f_number * 1.01]
    if not any(value > current * 1.01 for value, _ in selected):
        return None, {**evidence, "status": "not_shown",
                      "reason": f"the body lists no aperture between f/{current:g} and f/{limit_f_number:g}"}
    return (replace(caps, apertures=[value for value, _ in selected],
                    aperture_labels=[label for _, label in selected]),
            {**evidence, "status": "pending_readback", "usable_apertures": [value for value, _ in selected],
             "current_confirmed_aperture": current})


def constrain_capture_duration(caps: CameraCapabilities, *, max_shutter_s: float):
    """Keep raw driver choices separate from the enabled transport policy."""
    from dataclasses import replace
    from math import isfinite
    if not isfinite(max_shutter_s) or not 0 < max_shutter_s <= 30:
        raise ValueError("invalid enabled capture duration")
    min_shutter_s = 1 / 8000
    indices = [i for i, value in enumerate(caps.shutters) if min_shutter_s <= value <= max_shutter_s]
    if not indices:
        raise ValueError("No live shutter choices satisfy the enabled capture transport policy")
    values = [caps.shutters[i] for i in indices]
    labels = [caps.shutter_labels[i] for i in indices]
    return replace(caps, shutters=values, shutter_labels=labels), {
        "raw_driver_shutters_s": list(caps.shutters), "usable_shutters_s": values,
        "usable_shutter_labels": labels, "max_enabled_shutter_s": max_shutter_s,
        "min_enabled_shutter_s": min_shutter_s,
        "status": "provisional_short_exposure_policy",
        "reason": "Long-shutter Sony image retrieval failed physical validation; this is an application policy, not a sensor limit."}
