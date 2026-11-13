"""Tests for the seamless camera-config layer — fuzzy value matching + best-effort push."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import camera_config


def test_fuzzy_match_to_real_choices() -> None:
    assert camera_config._match("AF-S", ["AF-S", "AF-C", "DMF", "Manual Focus"]) == "AF-S"
    # the A7 II may report long names — "AF-S" must still resolve
    assert camera_config._match("AF-S", ["Single-shot AF", "Continuous AF", "Manual"]) == "Single-shot AF"
    # the REAL A7 II gphoto2 choices (read live) — AF-S is reported as "Automatic"
    assert camera_config._match("AF-S", ["Manual", "Automatic", "AF-C", "AF-A", "DMF"]) == "Automatic"
    assert camera_config._match("RAW+JPEG", ["Standard", "Fine", "RAW", "RAW+JPEG (Fine)"]) == "RAW+JPEG (Fine)"
    assert camera_config._match("RAW+JPEG", ["RAW", "RAW & JPEG", "Fine"]) == "RAW & JPEG"
    assert camera_config._match("Single", ["Single Shooting", "Cont. Shooting"]) == "Single Shooting"
    assert camera_config._match("Manual", ["AF-S", "AF-C"]) is None
    print("  fuzzy match: ok")


def test_configure_pushes_and_is_best_effort() -> None:
    choices = {"focusmode": ["AF-S", "AF-C", "Manual"],
               "imagequality": ["RAW", "RAW+JPEG", "Fine"],
               "drivemode": ["Single Shooting", "Cont. Shooting"],
               "whitebalance": ["Auto WB", "Daylight", "Cloudy"]}
    sets = {}
    res = camera_config.configure("default", lambda n: choices.get(n, []),
                                  lambda n, v: sets.__setitem__(n, v))
    assert sets["focusmode"] == "AF-S", sets
    assert sets["imagequality"] == "RAW+JPEG", sets
    assert sets["drivemode"] == "Single Shooting", sets  # "Single" fuzzy-matches

    # a node that errors mid-push is reported + skipped, never raised
    def boom(node, value):
        raise RuntimeError("usb drop")
    res2 = camera_config.configure("default", lambda n: choices.get(n, []), boom)
    assert all("skipped" in v for v in res2.values()), res2
    print(f"  configure best-effort: ok ({res['focus_mode']}, {res['image_quality']})")


if __name__ == "__main__":
    test_fuzzy_match_to_real_choices()
    test_configure_pushes_and_is_best_effort()
    print("camera_config tests: PASS")
