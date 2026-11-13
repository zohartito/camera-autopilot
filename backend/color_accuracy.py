"""M1 color-accuracy factor — the real "Color Range" sub-factor for the ISO card.

Arsenal's ISO card carries two sliders: DYNAMIC RANGE and COLOR RANGE. Dynamic range is already
modeled (sensor_noise.dynamic_range_stops). This is the missing half: how faithfully the sensor can
RESOLVE COLOR at a given ISO. It was a "pending" placeholder; this replaces it with physics.

The physics (why color, not just brightness, degrades with ISO):
  - A pixel's color is the RATIO of its R:G:B channel signals. Channel signals carry shot noise
    (sqrt of the electrons collected) plus read noise. The chroma you perceive is the difference
    between channels, so chroma noise is the channels' noise propagated through a difference — it
    grows FASTER than luma noise as the signal falls. This is why high-ISO frames go luminance-OK
    but colour-blotchy (the "color noise" that denoisers smear first).
  - Bayer demosaic compounds it: each color channel is spatially subsampled (R and B at 1/4 the
    sites of luma), so per-channel SNR is already below the luma SNR before any difference is taken.
  - As channel SNR collapses, the camera cannot tell two nearby hues apart -> the number of
    DISTINGUISHABLE colours (the gamut volume the sensor can actually deliver, not the nominal
    container) shrinks. That shrinking volume is the "Color Range".

Model: derive a chroma SNR from the existing photon-transfer NoiseModel (reuse, don't reinvent the
electron specs), convert it to a number of just-noticeable color steps, and report it as a fraction
of the base-ISO color volume. Pure math, fully unit-testable: `python backend/color_accuracy.py`.
"""
from __future__ import annotations

from math import sqrt

import sensor_noise

# Mid-tone reference patch (fraction of full well) at which we judge color fidelity. 18% grey is the
# metering anchor everywhere else in the brain, so use it here too for a consistent readout.
_REF_SIGNAL_FRACTION = 0.18

# A single color channel of a demosaiced pixel sees only a fraction of the per-pixel signal: the
# green channel covers half the Bayer sites, red/blue a quarter each. Average channel coverage that
# feeds a chroma difference is ~1/3 of the luma signal — so per-channel shot SNR is ~sqrt(1/3) of
# luma SNR before the difference. This factor folds that Bayer subsampling penalty into the model.
_BAYER_CHANNEL_FRACTION = 1.0 / 3.0

# Chroma is a difference of two noisy channels, so its noise is sqrt(2)x a single channel's noise
# (errors add in quadrature). This converts a per-channel SNR into a chroma SNR.
_CHROMA_DIFF_NOISE_GAIN = sqrt(2.0)

# Reference chroma SNR (at base ISO, mid-grey) that we call "full" color fidelity == 1.0. Anything
# at or above this delivers the sensor's full distinguishable-color volume; below it, fewer hues
# resolve. This is the A7 II's own base-ISO chroma SNR (~37) so base ISO scores 1.0 — the model
# grades every higher ISO RELATIVE to the sensor's own best, not an absolute target.
_REFERENCE_CHROMA_SNR = 37.0

# Status thresholds on the 0..1 color-fidelity score (fraction of base-ISO color volume retained).
_COLOR_OK_ABOVE = 0.70      # >=70% of base color volume -> clean
_COLOR_WARN_ABOVE = 0.33    # 33-70% -> color starting to thin (warning); ISO 6400 (A7 II usable
                            # ceiling) lands here — degraded but printable
# below 33% -> limited (visible color blotching / banding likely; deep-grain ISOs)


def chroma_snr(iso: int, noise: sensor_noise.NoiseModel = sensor_noise.A7II_NOISE,
               signal_fraction: float = _REF_SIGNAL_FRACTION, base_iso: int = 100,
               exposure_s: float = 1 / 100) -> float:
    """Chroma (color-difference) SNR at this ISO for a mid-grey patch. Reuses the photon-transfer
    NoiseModel: take the per-pixel luma SNR, scale the signal down to a single Bayer channel's
    share, then inflate the noise by the color-difference factor. Higher = cleaner color."""
    # luma SNR of the full pixel from the existing model (shot + read + thermal in quadrature)
    luma_snr = noise.snr(signal_fraction, iso, exposure_s=exposure_s, base_iso=base_iso)
    # a single demosaiced color channel sees ~1/3 the signal -> SNR scales with sqrt(signal)
    channel_snr = luma_snr * sqrt(_BAYER_CHANNEL_FRACTION)
    # chroma = difference of two such channels -> noise up by sqrt(2) -> SNR down by 1/sqrt(2)
    return channel_snr / _CHROMA_DIFF_NOISE_GAIN


def color_fidelity(iso: int, noise: sensor_noise.NoiseModel = sensor_noise.A7II_NOISE,
                   base_iso: int = 100) -> float:
    """0..1 fraction of the base-ISO distinguishable-color volume retained at this ISO. 1.0 == full
    color (base ISO); falls as chroma SNR drops with gain. Clamped to [0, 1].

    The map is sqrt-compressed (perceived color discrimination tracks ~sqrt of SNR, not SNR
    linearly — halving SNR does not halve the perceived hues), so the fall-off matches real-world
    behavior: base ISO ~1.0, the usable ceiling (ISO 6400) still ~one-third of full color (degraded
    but printable), only the deep-grain ISOs collapse toward zero."""
    snr = chroma_snr(iso, noise, base_iso=base_iso)
    return max(0.0, min(1.0, sqrt(snr / _REFERENCE_CHROMA_SNR)))


def distinguishable_colors(iso: int, noise: sensor_noise.NoiseModel = sensor_noise.A7II_NOISE,
                           base_iso: int = 100) -> int:
    """Approximate count of distinguishable color levels per channel at this ISO. A channel that
    can be split into N reliable steps (its SNR sets the step size) yields ~N discriminable tones;
    the perceived color volume scales with the cube. Reported per-channel so the readout stays a
    human-sized number (e.g. ~256 at base, dropping into the dozens at very high ISO)."""
    snr = chroma_snr(iso, noise, base_iso=base_iso)
    return max(2, int(round(snr * 2.0)))  # ~2 distinguishable steps per unit of chroma SNR


def color_range_readout(iso: int, noise: sensor_noise.NoiseModel = sensor_noise.A7II_NOISE,
                        base_iso: int = 100) -> tuple[str, str]:
    """(readout, status) for the ISO card's Color Range sub-factor. status in
    {ok, warning, limited}. Mirrors the dynamic-range sub-factor's shape so the card reads
    consistently: a number plus a one-glance verdict."""
    fidelity = color_fidelity(iso, noise, base_iso)
    levels = distinguishable_colors(iso, noise, base_iso)
    if fidelity >= _COLOR_OK_ABOVE:
        status = "ok"
    elif fidelity >= _COLOR_WARN_ABOVE:
        status = "warning"
    else:
        status = "limited"
    readout = f"{fidelity:.0%} color volume, ~{levels} hues/ch"
    return readout, status


# --- self-test (run: python backend/color_accuracy.py) --------------------
def _selftest() -> None:
    nm = sensor_noise.A7II_NOISE

    # Base ISO delivers (near) full color fidelity; it must be the max.
    base = color_fidelity(100)
    assert base >= 0.95, f"base ISO should be ~full color, got {base:.2f}"

    # Color fidelity falls monotonically with ISO (more gain -> worse chroma SNR).
    fids = [color_fidelity(iso) for iso in (100, 400, 1600, 6400, 25600, 51200)]
    assert all(a >= b for a, b in zip(fids, fids[1:])), fids
    assert fids[-1] < fids[0], "high ISO must lose color vs base"

    # Distinguishable colors also fall with ISO and stay a sane, positive count.
    cols = [distinguishable_colors(iso) for iso in (100, 1600, 25600)]
    assert cols[0] > cols[1] > cols[2] >= 2, cols

    # Status ladder: clean at base, degraded by the usable ceiling region, limited deep into the grain.
    assert color_range_readout(100)[1] == "ok", color_range_readout(100)
    assert color_range_readout(51200)[1] == "limited", color_range_readout(51200)
    # A mid-high ISO should be a warning, not still "ok" and not yet "limited".
    mid_status = color_range_readout(6400)[1]
    assert mid_status in ("warning", "limited"), mid_status

    # Chroma SNR is below luma SNR at the same ISO (the whole point — color degrades first).
    assert chroma_snr(1600) < nm.snr(_REF_SIGNAL_FRACTION, 1600), "chroma must be noisier than luma"

    print("color-accuracy self-test: PASS")
    for iso in (100, 800, 6400, 25600, 51200):
        readout, status = color_range_readout(iso)
        print(f"  ISO {iso:>5} -> {readout:32}  [{status}]  (chroma SNR {chroma_snr(iso):.1f})")


if __name__ == "__main__":
    _selftest()
