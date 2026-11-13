"""Tests for the M9 aperture optimizer — the physics gates stay authoritative over taste."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import aperture_optimizer
import camera_caps
import lens_optics
import physics

SENSOR = physics.A7II
LENS = lens_optics.TAMRON_28_75_OPTICS
APS = camera_caps.A7II_CAPS.apertures  # f/2.8 .. f/22


def test_shallow_subject_stays_wide() -> None:
    # A thin subject (0.2 m deep) at 2 m, taste wide (f/2.8): DoF is easily covered wide, so keep it
    # wide for separation/light.
    f, status = aperture_optimizer.choose_aperture(2.8, 2.0, 0.2, focal_mm=50, sensor=SENSOR,
                                                   lens_opt=LENS, apertures=APS)
    assert status == "ok" and f <= 3.5, (f, status)
    print(f"  shallow subject -> f/{f} (stays wide)")


def test_deep_subject_stops_down() -> None:
    # A deep subject (1.0 m) at 2 m needs more DoF -> must stop down well past the wide taste.
    f, status = aperture_optimizer.choose_aperture(2.8, 2.0, 1.0, focal_mm=50, sensor=SENSOR,
                                                   lens_opt=LENS, apertures=APS)
    assert status == "ok" and f >= 8.0, (f, status)
    print(f"  deep subject -> f/{f} (stopped down to cover it)")


def test_never_past_diffraction() -> None:
    # An impossible DoF demand (huge depth, very close) -> flagged, and never past the diffraction cap.
    f, status = aperture_optimizer.choose_aperture(2.8, 0.5, 50.0, focal_mm=50, sensor=SENSOR,
                                                   lens_opt=LENS, apertures=APS)
    assert status == "dof_exceeds_diffraction", status
    assert f <= SENSOR.diffraction_hard_aperture + 0.1, f
    print(f"  impossible DoF -> f/{f} (capped at diffraction, flagged)")


def test_exposure_gate_respected() -> None:
    # If exposure can only work at <= f/5.6, don't exceed it even though taste (f/8) + DoF would allow more.
    f, status = aperture_optimizer.choose_aperture(8.0, 3.0, 0.3, focal_mm=50, sensor=SENSOR,
                                                   lens_opt=LENS, apertures=APS,
                                                   exposure_feasible=lambda n: n <= 5.6)
    assert f <= 5.6, (f, status)
    print(f"  exposure-gated -> f/{f} (<= the feasible 5.6)")


if __name__ == "__main__":
    test_shallow_subject_stays_wide()
    test_deep_subject_stops_down()
    test_never_past_diffraction()
    test_exposure_gate_respected()
    print("aperture_optimizer tests: PASS")
