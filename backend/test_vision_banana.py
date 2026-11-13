"""Tests for the vision-model client. The FAIL-SAFE path is asserted unconditionally (so this
passes with the PC off); the live call is exercised only if the depth server is reachable."""
import sys
import json
import tempfile
from pathlib import Path

import numpy as np
from PIL import Image

sys.path.insert(0, str(Path(__file__).resolve().parent))

import vision_banana


def test_fail_safe_never_raises() -> None:
    # bad path -> None
    assert vision_banana.analyze("/no/such/file.jpg") is None
    # a real file but a dead host -> None (the network step fails cleanly, never raises)
    saved_host = vision_banana.VISION_HOST
    try:
        vision_banana.VISION_HOST = "http://127.0.0.1:9"  # nothing listening
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "x.jpg"
            Image.fromarray(np.zeros((8, 8, 3), dtype="uint8")).save(p)
            assert vision_banana.analyze(str(p), timeout_s=0.5) is None
    finally:
        vision_banana.VISION_HOST = saved_host
    # the extractors are all None-safe
    assert vision_banana.subject_distance_m(None) is None
    assert vision_banana.metering_region(None) is None
    assert vision_banana.scene_class(None) is None
    print("  fail-safe (bad path / dead host / None): ok")


def test_wifi_response_requires_exact_schema_and_preserves_all_fields() -> None:
    valid = {
        "subject_distance_m": 2.5, "subject_box": [11, 12, 100, 101],
        "scene_class": "portrait", "saliency_centroid": [61.5, 62.0],
        "depth_min_m": 1.0, "depth_max_m": 5.0,
    }
    assert vision_banana._validated_analysis(valid) == {
        **valid, "subject_box": (11, 12, 100, 101), "saliency_centroid": (61.5, 62.0),
    }
    for malformed in (
        {**valid, "extra": 1}, {key: value for key, value in valid.items() if key != "depth_max_m"},
        {**valid, "subject_distance_m": "2.5"}, {**valid, "subject_distance_m": True},
        {**valid, "subject_box": [11.5, 12, 100, 101]}, {**valid, "subject_box": [11, 12, 0, 101]},
        {**valid, "scene_class": "night"}, {**valid, "depth_min_m": 3.0},
    ):
        assert vision_banana._validated_analysis(malformed) is None, malformed

    class Response:
        headers = {"Content-Length": str(len(json.dumps(valid)))}
        def __init__(self): self._used = False
        def read(self, _size=-1):
            if self._used: return b""
            self._used = True
            return json.dumps(valid).encode()
        def __enter__(self): return self
        def __exit__(self, *_): return False

    saved = vision_banana.urllib.request.urlopen
    vision_banana.urllib.request.urlopen = lambda *_args, **_kwargs: Response()
    try:
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "wifi-frame.jpg"
            Image.fromarray(np.zeros((8, 8, 3), dtype="uint8")).save(path)
            result = vision_banana.analyze(str(path))
    finally:
        vision_banana.urllib.request.urlopen = saved
    assert result is not None
    dist = vision_banana.subject_distance_m(result)
    box = vision_banana.metering_region(result)
    cls = vision_banana.scene_class(result)
    assert dist == 2.5
    assert box and len(box) == 4, box
    assert cls in {"portrait", "closeup", "landscape", "generic"}, cls
    assert result["saliency_centroid"] == (61.5, 62.0)
    print("  Wi-Fi response: exact schema accepted; malformed data rejected")


if __name__ == "__main__":
    test_fail_safe_never_raises()
    test_wifi_response_requires_exact_schema_and_preserves_all_fields()
    print("vision_banana tests: PASS")
