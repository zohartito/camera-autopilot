"""Correct the metering exposure when the first meter frame could not be read.

A JPEG that is saturated (or black) pins its own mean at the encoder's limit, so the brightness
error it reports is a *bound*, not a measurement: a frame six stops over and a frame one stop over
both read ~255. Trusting that bound is what produced a blown-out photograph on 2026-09-08, when a
body left at ISO 6400 / f/2.8 / 1/15 metered a daylight room at 98.5% clipped.

A metering frame is judged only on readability — its noise and depth of field never reach the
gallery — so corrections spend ISO first and shutter second, and never move the aperture the
recommendation is still reasoning about.
"""
from __future__ import annotations

import math

import live_verify

MAX_REMETER_PASSES = 2
DECISIVE_STEP_STOPS = 4.0


def correction_stops(mean_luma: float, *, target: float = 112.0,
                     max_stops: float = DECISIVE_STEP_STOPS) -> float:
    """Stops to move the metering exposure. Negative exposes less, positive exposes more.

    A pinned frame under-reports its own error, so it takes the full decisive step rather than the
    (useless) measured difference. A merely dim or bright frame takes the measured difference.
    """
    if not math.isfinite(mean_luma) or mean_luma <= 0:
        return max_stops
    if mean_luma >= live_verify.METER_HIGH_LUMA:
        return -max_stops
    if mean_luma <= live_verify.METER_LOW_LUMA:
        return max_stops
    return max(-max_stops, min(max_stops, math.log2(target / mean_luma)))


def corrected_exposure(caps, iso: float, f_number: float, shutter_s: float,
                       stops: float) -> dict | None:
    """Snap a stop correction onto this body's live ladders, ISO first then shutter.

    Returns the exposure to apply plus the stops it actually achieves, or None when the ladders
    cannot move any further in the requested direction (the caller then stops re-metering rather
    than burning another shutter on an identical frame).
    """
    if not math.isfinite(stops) or abs(stops) < 0.5 or not caps.isos or not caps.shutters:
        return None
    iso_value, iso_label = caps.snap_iso(max(iso, 1.0) * 2 ** stops)
    iso_stops = math.log2(max(iso_value, 1) / max(iso, 1.0))
    remaining = stops - iso_stops
    shutter_value, shutter_label = caps.snap_shutter(max(shutter_s, 1e-6) * 2 ** remaining)
    shutter_stops = math.log2(max(shutter_value, 1e-6) / max(shutter_s, 1e-6))
    achieved = iso_stops + shutter_stops
    if abs(achieved) < 0.5:
        return None
    return {"iso": iso_label, "shutter": shutter_label, "aperture_held": f"f/{f_number:g}",
            "requested_stops": round(stops, 2), "achieved_stops": round(achieved, 2),
            "iso_stops": round(iso_stops, 2), "shutter_stops": round(shutter_stops, 2),
            "iso_value": iso_value, "shutter_value": shutter_value}


__all__ = ["MAX_REMETER_PASSES", "DECISIVE_STEP_STOPS", "correction_stops", "corrected_exposure"]
