"""M1 sensor noise model — the photon-transfer physics behind 5 of Arsenal's 22 factors:
SHOT NOISE, THERMAL NOISE, SENSOR NOISE FLOOR (read noise), SIGNAL TO NOISE, SENSOR DYNAMIC
RANGE. It derives the *real* usable-ISO ceiling from where dynamic range falls below an
acceptable bar — replacing the hand-set `max_usable_iso = 6400` in physics.py with physics.

(SENSOR SIZE and MEGAPIXELS — the other two noise-bucket factors — feed this via pixel area,
which sets the full-well capacity.)

Pure math, fully unit-testable: `python backend/sensor_noise.py`. The electron specs are
measured quantities (e.g. photonstophotos.net); the defaults below are the Sony A7 II and
reproduce its ~13.4-stop base dynamic range and ~ISO-6400 practical ceiling.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from math import log2, sqrt


@dataclass(frozen=True)
class NoiseModel:
    """Per-sensor electron-domain noise parameters (measured, not nominal)."""
    full_well_e: float          # saturation capacity at base ISO, electrons (SHOT-noise pool)
    read_noise_e_base: float    # read noise at base ISO, electrons — the SENSOR NOISE FLOOR
    read_noise_e_high: float    # read-noise floor in the high-gain regime (dual-conversion gain)
    dark_current_e_s: float     # THERMAL: dark electrons per second per pixel near room temp

    def read_noise_e(self, iso: int, base_iso: int = 100) -> float:
        """Input-referred read noise falls as analog gain rises (dual-gain sensors step down),
        toward a high-gain floor. Modeled as read_base / sqrt(gain), clamped at the floor — a
        smooth heuristic; measured per-ISO read-noise data (photonstophotos) can swap in here."""
        gain = max(1.0, iso / base_iso)
        return max(self.read_noise_e_high, self.read_noise_e_base / sqrt(gain))

    def well_e(self, iso: int, base_iso: int = 100) -> float:
        """Usable capacity scales down with ISO — higher ISO saturates on less light."""
        return self.full_well_e * base_iso / max(base_iso, iso)

    def dynamic_range_stops(self, iso: int, base_iso: int = 100) -> float:
        """Engineering dynamic range = log2(full well / read noise floor)."""
        return log2(self.well_e(iso, base_iso) / self.read_noise_e(iso, base_iso))

    def color_depth_bits(self, iso: int, base_iso: int = 100,
                         base_color_depth_bits: float = 24.4) -> float:
        """Tonal/color depth in bits (à la DxOMark 'Portrait' color-depth) — the Color Range sub-factor.
        Color discrimination tracks per-channel SNR, which tracks dynamic range: each stop of DR lost to
        gain/read-noise costs ~1 bit of distinguishable color. Anchored to the A7 II's measured base depth
        (~24.4 bits @ ISO 100, DxOMark). Yields ~22.4 bits @ ISO 1600, ~19 @ ISO 6400, ~17 @ ISO 25600 —
        matching the published curve — so the ISO card can say *why* color desaturates as ISO climbs."""
        dr_loss = self.dynamic_range_stops(base_iso, base_iso) - self.dynamic_range_stops(iso, base_iso)
        return base_color_depth_bits - dr_loss

    def snr(self, signal_fraction: float, iso: int, exposure_s: float = 1 / 100,
            base_iso: int = 100) -> float:
        """SNR (linear ratio) for a patch at `signal_fraction` of full well, combining shot
        noise (sqrt of signal), read noise, and thermal/dark noise in quadrature. Dark noise
        grows with exposure time, so long night exposures lose SNR even at the same ISO."""
        signal_e = self.well_e(iso, base_iso) * max(0.0, min(1.0, signal_fraction))
        shot = sqrt(signal_e)
        read = self.read_noise_e(iso, base_iso)
        dark = sqrt(self.dark_current_e_s * max(0.0, exposure_s))
        return signal_e / sqrt(shot * shot + read * read + dark * dark)

    def usable_iso_ceiling(self, *, min_dr_stops: float = 8.0, base_iso: int = 100,
                           max_iso: int = 102400) -> int:
        """Highest full-stop ISO whose dynamic range still clears `min_dr_stops` — the
        principled replacement for a hand-set noise ceiling. The default ~8 stops puts the
        A7 II at ISO 6400, matching its accepted practical limit. Returns `base_iso` as a floor
        even if the bar exceeds the sensor's base-ISO DR (you can always shoot at base ISO)."""
        ceiling = base_iso
        iso = base_iso
        while iso <= max_iso:
            if self.dynamic_range_stops(iso, base_iso) >= min_dr_stops:
                ceiling = iso
                iso *= 2
            else:
                break
        return ceiling


# Sony A7 II (ILCE-7M2): measured electron specs. Base read ~4.3 e-, high-gain floor ~1.6 e-,
# full well ~46 ke- -> base DR log2(46000/4.3) ~= 13.4 stops (measured ~13.6).
A7II_NOISE = NoiseModel(full_well_e=46000.0, read_noise_e_base=4.3,
                        read_noise_e_high=1.6, dark_current_e_s=2.0)


# --- measured JPEG-domain noise (a body without an electron-domain model) -------------------
def max_usable_iso_from_table(isos: list[int], sigma_luma: list[float], *, base_iso: int = 100,
                              threshold_ratio: float = 2.5) -> int:
    """Highest ISO in the table whose luma noise still sits at or below `threshold_ratio` times
    the base-ISO luma noise. This is a *documented, relative* JPEG-domain ceiling (not a DR bar):
    the point past which the in-camera JPEG's noise has grown more than 2.5x over base ISO. Noise
    rises monotonically with ISO, so this is the largest ISO still under the bar."""
    if not isos or len(isos) != len(sigma_luma):
        raise ValueError("ISO and sigma_luma tables must be non-empty and the same length")
    pairs = sorted(zip(isos, sigma_luma))
    base = min(pairs, key=lambda p: abs(log2(max(p[0], 1)) - log2(max(base_iso, 1))))[1]
    if not base > 0:
        raise ValueError("base-ISO luma sigma must be positive")
    ceiling = pairs[0][0]
    for iso, sigma in pairs:
        if sigma > threshold_ratio * base + 1e-9:
            break  # the first ISO over the bar ends the usable range, even if a higher one dips back
        ceiling = iso
    return ceiling


@dataclass(frozen=True)
class MeasuredJpegNoise:
    """JPEG-domain luma/chroma noise measured from flat patches across an ISO ladder.

    This is NOT electron-domain read noise: it is the standard deviation of 8-bit luma in
    low-gradient patches of in-camera JPEGs, so it folds in the tone curve and any in-camera
    noise reduction. It yields an honest *relative* noise-vs-ISO curve and a usable-ISO ceiling,
    but never a dynamic-range or color-depth figure — those stay unknown for such a body. It is a
    drop-in only for the ISO-ceiling role (`usable_iso_ceiling`) that the exposure layer needs."""
    body: str
    base_iso: int
    isos: tuple[int, ...]
    sigma_luma: tuple[float, ...]     # 8-bit luma std, parallel to isos
    sigma_chroma: tuple[float, ...]   # 8-bit chroma std, parallel to isos
    ceiling_threshold_ratio: float = 2.5
    provenance: dict = field(default_factory=dict)

    def _interp(self, table: tuple[float, ...], iso: int) -> float:
        pairs = sorted(zip(self.isos, table))
        x = log2(max(iso, 1))
        if x <= log2(max(pairs[0][0], 1)):
            return pairs[0][1]
        if x >= log2(max(pairs[-1][0], 1)):
            return pairs[-1][1]
        for (lo_iso, lo), (hi_iso, hi) in zip(pairs, pairs[1:]):
            lo_x, hi_x = log2(lo_iso), log2(hi_iso)
            if lo_x <= x <= hi_x:
                t = (x - lo_x) / (hi_x - lo_x)
                return lo + t * (hi - lo)
        return pairs[-1][1]

    def sigma_luma_8bit(self, iso: int) -> float:
        return self._interp(self.sigma_luma, iso)

    def sigma_chroma_8bit(self, iso: int) -> float:
        return self._interp(self.sigma_chroma, iso)

    def max_usable_iso(self) -> int:
        return max_usable_iso_from_table(list(self.isos), list(self.sigma_luma),
                                         base_iso=self.base_iso,
                                         threshold_ratio=self.ceiling_threshold_ratio)

    def usable_iso_ceiling(self, *, base_iso: int | None = None, **_ignored) -> int:
        """Drop-in for NoiseModel.usable_iso_ceiling: the measured JPEG-noise ceiling. Extra
        keyword args (a DR bar, max_iso) are accepted and ignored — a JPEG table has no DR."""
        return self.max_usable_iso()


def load_measured_noise(data: dict) -> MeasuredJpegNoise:
    """Build a MeasuredJpegNoise from a parsed sensor-profile JSON (see tools/calibrate_sensor_noise.py)."""
    base_iso = int(data.get("base_iso", 100))
    table = data.get("noise_table") or []
    if not table:
        raise ValueError("sensor profile JSON has an empty noise_table")
    rows = sorted(table, key=lambda r: int(r["iso"]))
    isos = tuple(int(r["iso"]) for r in rows)
    sigma_luma = tuple(float(r["sigma_luma_8bit"]) for r in rows)
    sigma_chroma = tuple(float(r.get("sigma_chroma_8bit", 0.0)) for r in rows)
    if any(s <= 0 for s in sigma_luma):
        raise ValueError("every noise_table row needs a positive sigma_luma_8bit")
    threshold = float(data.get("ceiling_threshold_ratio", 2.5))
    return MeasuredJpegNoise(body=str(data.get("body", "unknown")), base_iso=base_iso,
                             isos=isos, sigma_luma=sigma_luma, sigma_chroma=sigma_chroma,
                             ceiling_threshold_ratio=threshold,
                             provenance=dict(data.get("provenance", {})))


# --- self-test (run: python backend/sensor_noise.py) ----------------------
def _selftest() -> None:
    def close(a: float, b: float, tol: float = 0.05) -> bool:
        return abs(a - b) <= tol * max(1.0, abs(b))

    nm = A7II_NOISE

    # Base dynamic range ~13.4 stops (A7 II measured ~13.6).
    assert 13.0 < nm.dynamic_range_stops(100) < 14.0, nm.dynamic_range_stops(100)

    # DR falls monotonically with ISO (more gain -> less headroom).
    drs = [nm.dynamic_range_stops(iso) for iso in (100, 400, 1600, 6400, 25600)]
    assert all(a > b for a, b in zip(drs, drs[1:])), drs

    # Read noise (the noise floor) drops with gain then plateaus at the high-gain floor.
    assert close(nm.read_noise_e(100), 4.3), nm.read_noise_e(100)
    assert nm.read_noise_e(6400) <= nm.read_noise_e(100), "read noise should fall with gain"
    assert close(nm.read_noise_e(25600), 1.6, 0.01), nm.read_noise_e(25600)

    # The derived ceiling lands at the A7 II's accepted ISO 6400 (default 8-stop DR bar).
    assert nm.usable_iso_ceiling() == 6400, nm.usable_iso_ceiling()
    # A stricter bar pulls it down; a looser bar lets it climb.
    assert nm.usable_iso_ceiling(min_dr_stops=10.0) < 6400, nm.usable_iso_ceiling(min_dr_stops=10.0)
    assert nm.usable_iso_ceiling(min_dr_stops=6.5) > 6400, nm.usable_iso_ceiling(min_dr_stops=6.5)

    # SNR: clean at base mid-gray, much noisier at high ISO; long exposure adds thermal noise.
    snr_base = nm.snr(0.18, 100)
    snr_high = nm.snr(0.18, 6400)
    assert snr_base > 40 and snr_base > snr_high, (snr_base, snr_high)
    assert nm.snr(0.05, 6400, exposure_s=20) < nm.snr(0.05, 6400, exposure_s=1 / 100), "thermal"

    # Measured JPEG-domain noise: monotone table -> a relative ceiling, interpolation, drop-in.
    measured = load_measured_noise({
        "body": "TEST", "base_iso": 100, "ceiling_threshold_ratio": 2.5,
        "noise_table": [{"iso": 100, "sigma_luma_8bit": 2.0, "sigma_chroma_8bit": 1.0},
                        {"iso": 400, "sigma_luma_8bit": 3.0, "sigma_chroma_8bit": 1.5},
                        {"iso": 1600, "sigma_luma_8bit": 4.5, "sigma_chroma_8bit": 2.2},
                        {"iso": 6400, "sigma_luma_8bit": 8.0, "sigma_chroma_8bit": 4.0}]})
    # base sigma 2.0, 2.5x bar = 5.0 -> ISO 1600 (4.5) passes, 6400 (8.0) does not.
    assert measured.max_usable_iso() == 1600, measured.max_usable_iso()
    assert measured.usable_iso_ceiling(base_iso=100, min_dr_stops=8) == 1600
    assert 3.0 < measured.sigma_luma_8bit(800) < 4.5, measured.sigma_luma_8bit(800)
    assert measured.sigma_luma_8bit(50) == 2.0 and measured.sigma_luma_8bit(999999) == 8.0
    assert max_usable_iso_from_table([100, 200, 400], [2.0, 3.0, 9.0]) == 200

    print("sensor-noise self-test: PASS")
    print(f"  A7 II base DR -> {nm.dynamic_range_stops(100):.2f} stops")
    print("  DR by ISO     -> " + ", ".join(
        f"{iso}:{nm.dynamic_range_stops(iso):.1f}" for iso in (100, 800, 6400, 25600)))
    print(f"  derived ISO ceiling -> {nm.usable_iso_ceiling()} (8-stop DR bar)")
    print(f"  mid-gray SNR  -> ISO100:{snr_base:.0f}  ISO6400:{snr_high:.0f}")


if __name__ == "__main__":
    _selftest()
