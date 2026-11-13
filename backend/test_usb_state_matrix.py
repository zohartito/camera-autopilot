"""Camera-free tests for the USB state-matrix parser."""

from __future__ import annotations

import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "tools"))

import a7ii_usb_state_matrix


def test_usb_state_matrix_parses_writable_readonly_and_absent_watch_rows() -> None:
    stdout = """
=== nodes that decide our hard limits ===
  [PRESENT] movie                  writable   =                2  (native video record (0xD2C8) -> limit #9)
  [PRESENT] sensorcrop             READ-ONLY  =            'Off'  (APS-C crop (0xD219) -> limit #6)
  [ABSENT ] spotfocusarea          —                            (AF point X/Y (0xD2DC) -> limit #10) -> firmware wall
  [PRESENT] shutterspeed           writable   =          '1/800'  (shutter (0xD20D))
  [PRESENT] iso                    writable   =            '800'  (ISO (0xD21E))
  [PRESENT] f-number               writable   =          'f/2.8'  (aperture (0x5007))
"""
    parsed = a7ii_usb_state_matrix._parse_watch(stdout)

    assert parsed["movie"] == {
        "presence": "PRESENT",
        "writability": "writable",
        "value": "2",
    }
    assert parsed["sensorcrop"] == {
        "presence": "PRESENT",
        "writability": "READ-ONLY",
        "value": "Off",
    }
    assert parsed["spotfocusarea"] == {
        "presence": "ABSENT",
        "writability": "",
        "value": "",
    }
    assert parsed["shutterspeed"]["writability"] == "writable"
    assert parsed["iso"]["value"] == "800"
    assert parsed["f-number"]["value"] == "f/2.8"
    print("  USB state matrix parser watch rows: ok")


if __name__ == "__main__":
    test_usb_state_matrix_parses_writable_readonly_and_absent_watch_rows()
    print("usb_state_matrix tests: PASS")
