"""M1 lens optical model — 3 more of Arsenal's 22 factors, all deterministic:
LENS TRANSMISSION, LENS ABERRATIONS, and COMA.

- TRANSMISSION: glass absorbs light, so the T-stop (what governs exposure) is a touch slower
  than the f-stop (what governs depth of field). It is DATA per lens (`LensTransmission`), never a
  default guess: a lens with no published measurement is `unknown` and changes nothing. A meter
  frame taken through the same lens already includes its light loss, so the offset only enters
  when a lens-independent scene luminance is converted to settings (physics.lens_adjusted_ev100).
- ABERRATIONS: a lens is softest wide open (spherical aberration) and sharpest a couple stops
  down — its "sweet spot" — until diffraction takes over. We model relative sharpness vs.
  aperture so the brain can prefer the sweet spot when DoF/exposure leave it free.
- COMA: off-axis points smear into comet shapes, worst wide open; clears on stopping down.
  Decisive for astro (stars at the frame edge), so we expose it as a severity 0..1.

Pure math, fully unit-testable: `python backend/lens_optics.py`. Sharpness depends on the
sensor (its diffraction-limited aperture), so those calls take a SensorProfile.
"""
from __future__ import annotations

from dataclasses import dataclass
from math import isfinite, log2


def _stops_down(f_number: float, wide_open_f: float) -> float:
    """How many full stops `f_number` is stopped down from wide open (each stop = x sqrt(2))."""
    return 2.0 * log2(max(f_number, wide_open_f) / wide_open_f)


@dataclass(frozen=True)
class LensTransmission:
    """A lens's light transmission as a published T-stop measurement, or explicitly `unknown`.

    `t_stop_offset_stops` is the light the glass loses relative to the f-number, in stops:
    2*log2(T/f). It is derived from the published pair (`reported_t_stop` at `reported_f_number`)
    and applies at every aperture (a constant transmittance). None means unknown: nothing is
    assumed and every consumer treats the offset as 0 while saying so."""
    reported_t_stop: float | None = None
    reported_f_number: float | None = None
    source_url: str | None = None
    source_date: str | None = None       # the publication date of the measurement
    retrieved: str | None = None         # when this project read it
    measured_on: str | None = None       # the body the source measured with
    note: str = ""

    def __post_init__(self) -> None:
        pair = (self.reported_t_stop, self.reported_f_number)
        if (pair[0] is None) != (pair[1] is None):
            raise ValueError("a transmission measurement needs both the T-stop and its f-number")
        if pair[0] is not None:
            if not all(isfinite(v) and v > 0 for v in pair) or pair[0] < pair[1]:
                raise ValueError(f"T-stop must be finite and no faster than its f-number, got {pair}")
            if not self.source_url or not self.source_date:
                raise ValueError("a transmission measurement must cite its source URL and date")

    @property
    def known(self) -> bool:
        return self.reported_t_stop is not None

    @property
    def status(self) -> str:
        return "published_measurement" if self.known else "unknown"

    @property
    def t_stop_offset_stops(self) -> float | None:
        """Stops of light lost to the glass (>= 0), or None when unmeasured."""
        if not self.known:
            return None
        return 2.0 * log2(self.reported_t_stop / self.reported_f_number)

    @property
    def transmittance(self) -> float | None:
        offset = self.t_stop_offset_stops
        return None if offset is None else 2.0 ** -offset

    def t_stop(self, f_number: float) -> float | None:
        """The T-stop at this f-number, or None when the transmission is unknown."""
        offset = self.t_stop_offset_stops
        return None if offset is None else f_number * 2.0 ** (offset / 2.0)

    def to_dict(self) -> dict:
        return {"status": self.status, "t_stop_offset_stops": self.t_stop_offset_stops,
                "reported_t_stop": self.reported_t_stop,
                "reported_f_number": self.reported_f_number, "source_url": self.source_url,
                "source_date": self.source_date, "retrieved": self.retrieved,
                "measured_on": self.measured_on, "note": self.note}


UNKNOWN_TRANSMISSION = LensTransmission(note="No published transmission measurement is recorded "
                                             "for this lens; nothing is assumed.")

# Sony FE 24-105mm F4 G OSS: DxOMark's review quotes "transmission, measured at T4.4" (A7R IV,
# published 2021-01-19). The same review describes the loss as -0.3 EV at 24mm to -0.4 EV at
# 105mm, while T4.4 at f/4 is 0.28 stops, so treat the offset as good to about 0.1 EV.
FE_24_105_F4_G_TRANSMISSION = LensTransmission(
    reported_t_stop=4.4, reported_f_number=4.0,
    source_url="https://www.dxomark.com/sony-fe-24-105mm-f4-g-oss-lens-review-good-performer-but-pricey/",
    source_date="2021-01-19", retrieved="2026-09-27", measured_on="Sony A7R IV (DxOMark)",
    note="DxOMark T4.4 score; its text also quotes -0.3 EV (24mm) to -0.4 EV (105mm), "
         "so the offset is uncertain by about 0.1 EV.")


@dataclass(frozen=True)
class LensOptics:
    name: str
    wide_open_f: float        # the lens's widest aperture (smallest f-number)
    wide_open_mtf: float      # relative sharpness wide open (aberration-limited), 0..1
    sweet_spot_stops: float   # stops down from wide open where aberrations have cleared (~2)
    coma_clears_stops: float  # stops down from wide open where coma becomes negligible (~2)
    transmission: LensTransmission = UNKNOWN_TRANSMISSION

    def __post_init__(self) -> None:
        assert self.sweet_spot_stops > 0 and self.coma_clears_stops > 0, "stop-counts must be positive"

    def t_stop(self, f_number: float) -> float | None:
        """The T-stop at this f-number, or None when the lens's transmission is unmeasured."""
        return self.transmission.t_stop(f_number)

    def aberration_mtf(self, f_number: float) -> float:
        """Relative sharpness limited by spherical aberration: rises from `wide_open_mtf` toward
        1.0 by the sweet spot, then stays there (diffraction is handled separately)."""
        frac = min(1.0, _stops_down(f_number, self.wide_open_f) / self.sweet_spot_stops)
        return self.wide_open_mtf + (1.0 - self.wide_open_mtf) * frac

    def diffraction_mtf(self, f_number: float, sensor) -> float:
        """Relative sharpness limited by diffraction: ~1.0 until the sensor's diffraction-limited
        aperture, then falls off as soft/f."""
        soft = sensor.diffraction_soft_aperture
        return 1.0 if f_number <= soft else soft / f_number

    def sharpness(self, f_number: float, sensor) -> float:
        """Overall relative sharpness 0..1 = aberration MTF x diffraction MTF."""
        return self.aberration_mtf(f_number) * self.diffraction_mtf(f_number, sensor)

    def optimal_aperture(self, sensor, narrowest_f: float = 22.0,
                         step: float = 2 ** (1 / 6)) -> float:
        """The *widest* aperture at peak sharpness — ties favor the wider stop (more light, less
        diffraction risk). Scans wide-open..narrowest in third-stop f-number steps (x2**(1/6))
        where the aberration gain and the diffraction loss balance."""
        if step <= 1.0:
            step = 2 ** (1 / 6)  # must widen the search each iteration
        best_f, best_s = self.wide_open_f, -1.0
        f = self.wide_open_f
        while f <= narrowest_f + 1e-9:
            s = self.sharpness(f, sensor)
            if s > best_s:
                best_s, best_f = s, f
            f *= step
        return round(best_f, 1)

    def coma_severity(self, f_number: float) -> float:
        """0 (clean) .. 1 (severe). Worst wide open, clears as you stop down. Astro-decisive."""
        return max(0.0, 1.0 - _stops_down(f_number, self.wide_open_f) / self.coma_clears_stops)


# Tamron 28-75 f/2.8: decent wide open, sharpest ~2 stops down (f/5.6). Its transmission is
# UNKNOWN: DxOMark lists the A036 but publishes no T-stop for it (checked 2026-09-27), so the old
# "~85% (~T3.0)" figure was an assumption and is not used.
TAMRON_28_75_OPTICS = LensOptics(name="Tamron 28-75 f/2.8", wide_open_f=2.8,
                                 wide_open_mtf=0.78, sweet_spot_stops=2.0, coma_clears_stops=2.0)


# --- self-test (run: python backend/lens_optics.py) -----------------------
def _selftest() -> None:
    import physics

    def close(a: float, b: float, tol: float = 0.03) -> bool:
        return abs(a - b) <= tol * max(1.0, abs(b))

    lens = TAMRON_28_75_OPTICS
    sensor = physics.A7II

    # Transmission: the Tamron is unmeasured, so no T-stop is claimed; the FE 24-105's published
    # T4.4 at f/4 is 0.28 stops of loss and holds at every aperture.
    assert lens.t_stop(2.8) is None and not lens.transmission.known
    fe = FE_24_105_F4_G_TRANSMISSION
    assert close(fe.t_stop(4.0), 4.4, 0.001) and close(fe.t_stop_offset_stops, 0.275, 0.01)
    assert close(fe.t_stop(8.0), 8.8, 0.001), fe.t_stop(8.0)

    # Aberrations: soft wide open, sharper stopped down to the sweet spot.
    assert lens.aberration_mtf(2.8) < lens.aberration_mtf(5.6), "should sharpen stopping down"
    assert close(lens.aberration_mtf(5.6), 1.0, 0.001), lens.aberration_mtf(5.6)

    # Sweet spot sits a couple stops down (f/5.6-8), and is sharper than both extremes.
    sweet = lens.optimal_aperture(sensor)
    assert 5.0 <= sweet <= 8.5, sweet
    assert lens.sharpness(sweet, sensor) > lens.sharpness(2.8, sensor), "sweet > wide open"
    assert lens.sharpness(sweet, sensor) > lens.sharpness(22.0, sensor), "sweet > diffracted f/22"

    # Coma: severe wide open, gone by the time it clears (~f/5.6) — the astro rule.
    assert close(lens.coma_severity(2.8), 1.0, 0.001), lens.coma_severity(2.8)
    assert lens.coma_severity(4.0) < lens.coma_severity(2.8), "coma should ease stopping down"
    assert lens.coma_severity(5.6) == 0.0, lens.coma_severity(5.6)

    print("lens-optics self-test: PASS")
    print(f"  transmission -> Tamron {lens.transmission.status}; FE 24-105 f/4 = T/{fe.t_stop(4.0):.1f}")
    print(f"  sweet spot   -> f/{sweet} (sharpest); wide-open MTF {lens.sharpness(2.8, sensor):.2f}, "
          f"f/22 MTF {lens.sharpness(22.0, sensor):.2f}")
    print(f"  coma         -> f/2.8:{lens.coma_severity(2.8):.1f}  f/4:{lens.coma_severity(4.0):.1f}  "
          f"f/5.6:{lens.coma_severity(5.6):.1f}")


if __name__ == "__main__":
    _selftest()
