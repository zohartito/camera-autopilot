"""M1 vision factors — the Mac-side client for the PC depth/subject model. Three of the 22 factors
(DEPTH MAP, SALIENCY, SUBJECT PROFILES) come from a Depth-Anything-V2-Metric model running on the
PC's 4070 (`vision_banana_server.py`); this calls it over the local network at capture time.

**FAIL-SAFE is the whole contract**: every function returns None on ANY error (server down, timeout,
bad/empty response) so the brain degrades cleanly to physics-only. It must NEVER raise into the
capture path. The brain uses these for: subject distance -> depth-of-field requirement (the M9
aperture optimizer); the salient subject box -> subject-weighted metering; the scene class -> M11
coma gating + the AI focus driver.
"""
from __future__ import annotations

import os
import math
import urllib.request
import uuid
import time

from security_bounds import json_response
from image_safety import source_bytes

VISION_HOST = os.environ.get("VISION_BANANA_HOST", "http://127.0.0.1:8077")
_TIMEOUT_S = 4.0
_LAST_RESULT = {"state": "not_attempted", "fallback": "physics_only"}


def runtime_status() -> dict:
    """Last actual request result; reading status never contacts the remote host."""
    return dict(_LAST_RESULT)
_SCENE_CLASSES = frozenset({"portrait", "closeup", "landscape", "generic"})
_ANALYSIS_FIELDS = frozenset({
    "subject_distance_m", "subject_box", "scene_class", "saliency_centroid",
    "depth_min_m", "depth_max_m",
})


def _finite_number(value: object, *, minimum: float, maximum: float) -> float | None:
    """Accept JSON numbers only: bools and strings are not measurements."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    number = float(value)
    return number if math.isfinite(number) and minimum <= number <= maximum else None


def _validated_analysis(result: object) -> dict | None:
    """Return only a small, typed model result; malformed LAN data is no result."""
    if not isinstance(result, dict):
        return None
    if set(result) != _ANALYSIS_FIELDS:
        return None
    distance = _finite_number(result["subject_distance_m"], minimum=0.05, maximum=1000.0)
    depth_min = _finite_number(result["depth_min_m"], minimum=0.0, maximum=1000.0)
    depth_max = _finite_number(result["depth_max_m"], minimum=0.0, maximum=1000.0)
    box = result["subject_box"]
    centroid = result["saliency_centroid"]
    scene = result["scene_class"]
    if (
        distance is None or depth_min is None or depth_max is None
        or not depth_min <= distance <= depth_max
        or not isinstance(box, (list, tuple)) or len(box) != 4
        or any(isinstance(value, bool) or not isinstance(value, int) for value in box)
        or any(value < 0 or value > 100_000 for value in box)
        or box[2] == 0 or box[3] == 0
        or not isinstance(centroid, (list, tuple)) or len(centroid) != 2
        or any(_finite_number(value, minimum=0.0, maximum=100_000.0) is None for value in centroid)
        or scene not in _SCENE_CLASSES
    ):
        return None
    return {
        "subject_distance_m": distance,
        "subject_box": tuple(box),
        "scene_class": scene,
        "saliency_centroid": tuple(float(value) for value in centroid),
        "depth_min_m": depth_min,
        "depth_max_m": depth_max,
    }


def analyze(image_path: str, timeout_s: float = _TIMEOUT_S) -> dict | None:
    """POST the frame to the PC depth/subject server; return its analysis dict or None on any
    failure. Keys: subject_distance_m, subject_box [x,y,w,h], scene_class, saliency_centroid [x,y],
    depth_min_m, depth_max_m. The server returns nulls on its own failures, which we map to None."""
    global _LAST_RESULT
    started = time.monotonic()
    try:
        image = source_bytes(image_path)
        boundary = uuid.uuid4().hex
        body = b"".join([
            f"--{boundary}\r\n".encode(),
            b'Content-Disposition: form-data; name="image"; filename="frame.jpg"\r\n',
            b"Content-Type: image/jpeg\r\n\r\n",
            image,
            f"\r\n--{boundary}--\r\n".encode(),
        ])
        req = urllib.request.Request(
            f"{VISION_HOST}/analyze", data=body,
            headers={"Content-Type": f"multipart/form-data; boundary={boundary}"})
        with urllib.request.urlopen(req, timeout=timeout_s) as resp:
            result = json_response(resp)
        analysis = _validated_analysis(result)
        _LAST_RESULT = {"state": "available" if analysis else "invalid_response", "fallback": None if analysis else "physics_only", "elapsed_ms": round((time.monotonic() - started) * 1000, 1)}
        return analysis
    except Exception:  # noqa: BLE001 - any failure -> physics-only fallback, never raise
        _LAST_RESULT = {"state": "unavailable", "fallback": "physics_only", "elapsed_ms": round((time.monotonic() - started) * 1000, 1)}
        return None


def subject_distance_m(analysis: dict | None) -> float | None:
    """DEPTH MAP -> the subject's distance in meters (drives the depth-of-field requirement)."""
    if not analysis:
        return None
    distance = analysis.get("subject_distance_m")
    try:
        value = float(distance)
    except (TypeError, ValueError):
        return None
    return value if math.isfinite(value) and value > 0 else None


def metering_region(analysis: dict | None) -> tuple[int, int, int, int] | None:
    """SALIENCY -> the subject bounding box (x, y, w, h) to meter for, or None for whole-frame."""
    if not analysis:
        return None
    box = analysis.get("subject_box")
    if not isinstance(box, (list, tuple)) or len(box) != 4:
        return None
    try:
        values = tuple(int(v) for v in box)
    except (TypeError, ValueError, OverflowError):
        return None
    return values if all(value >= 0 for value in values) else None


def scene_class(analysis: dict | None) -> str | None:
    """SUBJECT PROFILES -> the scene class: portrait / closeup / landscape / generic."""
    if not analysis:
        return None
    scene = analysis.get("scene_class")
    return scene if scene in _SCENE_CLASSES else None


def dof_depth_m(analysis: dict | None) -> float | None:
    """How much subject depth (m) to keep sharp, for the M9 aperture optimizer — a fraction of the
    subject distance (DoF shrinks as you get closer; the table lives in aperture_optimizer). None for
    landscape / unknown: a landscape is planned by the hyperfocal rule, not a depth fraction."""
    import aperture_optimizer
    return aperture_optimizer.required_dof_depth_m(scene_class(analysis), subject_distance_m(analysis))
