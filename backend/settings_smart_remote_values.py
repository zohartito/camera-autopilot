"""Smart Remote setter value strategies for behavior-proof commands.

These helpers keep speculative Smart Remote writes dry-run-first.  A proof command can ask the
live runner to choose the first camera-advertised alternate value, but only for setters whose
available/current getter pair is known well enough to validate before writing.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

AUTO_VALUE_MODE = "first-available-alternate"


@dataclass(frozen=True)
class SmartRemoteValueStrategy:
    setting_id: str
    method: str
    available_getter: str
    current_getter: str | None
    notes: str


_STRATEGIES = {
    "camera.aspect_ratio": SmartRemoteValueStrategy(
        setting_id="camera.aspect_ratio",
        method="setStillSize",
        available_getter="getAvailableStillSize",
        current_getter="getStillSize",
        notes="Choose a camera-advertised still-size pair so setStillSize receives aspect and size.",
    ),
    "camera.creative_style": SmartRemoteValueStrategy(
        setting_id="camera.creative_style",
        method="setCreativeStyle",
        available_getter="getAvailableCreativeStyle",
        current_getter="getCreativeStyle",
        notes="Choose a camera-advertised creative style alternate.",
    ),
    "camera.drive_mode": SmartRemoteValueStrategy(
        setting_id="camera.drive_mode",
        method="setShootMode",
        available_getter="getAvailableShootMode",
        current_getter="getShootMode",
        notes="Choose a camera-advertised shoot mode alternate.",
    ),
    "camera.dro_auto_hdr": SmartRemoteValueStrategy(
        setting_id="camera.dro_auto_hdr",
        method="setDRO",
        available_getter="getAvailableDRO",
        current_getter="getDRO",
        notes="Choose a camera-advertised DRO alternate.",
    ),
    "camera.exposure_comp": SmartRemoteValueStrategy(
        setting_id="camera.exposure_comp",
        method="setExposureCompensation",
        available_getter="getAvailableExposureCompensation",
        current_getter="getExposureCompensation",
        notes="Choose a camera-advertised exposure-compensation alternate.",
    ),
    "camera.file_format": SmartRemoteValueStrategy(
        setting_id="camera.file_format",
        method="setMovieFileFormat",
        available_getter="getAvailableMovieFileFormat",
        current_getter="getMovieFileFormat",
        notes="Choose a camera-advertised movie file-format alternate.",
    ),
    "camera.flash_comp": SmartRemoteValueStrategy(
        setting_id="camera.flash_comp",
        method="setFlashCompensation",
        available_getter="getAvailableFlashCompensation",
        current_getter="getFlashCompensation",
        notes="Choose a camera-advertised flash-compensation alternate.",
    ),
    "camera.flash_mode": SmartRemoteValueStrategy(
        setting_id="camera.flash_mode",
        method="setFlashMode",
        available_getter="getAvailableFlashMode",
        current_getter="getFlashMode",
        notes="Choose a camera-advertised flash mode alternate.",
    ),
    "camera.focus_mode": SmartRemoteValueStrategy(
        setting_id="camera.focus_mode",
        method="setFocusMode",
        available_getter="getAvailableFocusMode",
        current_getter="getFocusMode",
        notes="Choose a camera-advertised focus mode alternate such as MF or AF-S.",
    ),
    "camera.image_size": SmartRemoteValueStrategy(
        setting_id="camera.image_size",
        method="setStillSize",
        available_getter="getAvailableStillSize",
        current_getter="getStillSize",
        notes="Choose a camera-advertised still-size pair so setStillSize receives aspect and size.",
    ),
    "camera.iso": SmartRemoteValueStrategy(
        setting_id="camera.iso",
        method="setIsoSpeedRate",
        available_getter="getAvailableIsoSpeedRate",
        current_getter="getIsoSpeedRate",
        notes="Choose a camera-advertised ISO alternate.",
    ),
    "camera.metering_mode": SmartRemoteValueStrategy(
        setting_id="camera.metering_mode",
        method="setMeteringMode",
        available_getter="getAvailableMeteringMode",
        current_getter="getMeteringMode",
        notes="Choose a camera-advertised metering mode alternate.",
    ),
    "camera.smile_face_detect": SmartRemoteValueStrategy(
        setting_id="camera.smile_face_detect",
        method="setFaceDetection",
        available_getter="getAvailableFaceDetection",
        current_getter="getFaceDetection",
        notes="Choose a camera-advertised face-detection alternate.",
    ),
    "phone.review_image_size": SmartRemoteValueStrategy(
        setting_id="phone.review_image_size",
        method="setPostviewImageSize",
        available_getter="getAvailablePostviewImageSize",
        current_getter="getPostviewImageSize",
        notes="Choose a camera-advertised Smart Remote postview/review image size alternate.",
    ),
    "camera.white_balance": SmartRemoteValueStrategy(
        setting_id="camera.white_balance",
        method="setWhiteBalance",
        available_getter="getAvailableWhiteBalance",
        current_getter="getWhiteBalance",
        notes="Choose a camera-advertised white-balance alternate.",
    ),
}


def strategy_for(setting_id: str, method: str | None = None) -> SmartRemoteValueStrategy | None:
    strategy = _STRATEGIES.get(setting_id)
    if not strategy:
        return None
    if method and strategy.method != method:
        return None
    return strategy


def behavior_value_argument(setting_id: str, method: str) -> str:
    if strategy_for(setting_id, method):
        return f"--auto-value {AUTO_VALUE_MODE}"
    return '--value "<value sent>"'


def behavior_requested_value_for_session(setting_id: str, method: str) -> str | None:
    if strategy_for(setting_id, method):
        return None
    return "<value sent>"


def _canonical(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":")).lower()


def _dedupe(values: list[Any]) -> list[Any]:
    out = []
    seen = set()
    for value in values:
        key = _canonical(value)
        if key in seen:
            continue
        seen.add(key)
        out.append(value)
    return out


def _leaf_values(value: Any) -> list[Any]:
    if isinstance(value, dict):
        return [item for child in value.values() for item in _leaf_values(child)]
    if isinstance(value, (list, tuple)):
        return [item for child in value for item in _leaf_values(child)]
    if isinstance(value, (str, int, float, bool)):
        return [value]
    return []


def _looks_like_aspect(value: Any) -> bool:
    text = str(value)
    return ":" in text and all(part.strip().isdigit() for part in text.split(":", 1))


def _looks_like_still_size(value: Any) -> bool:
    text = str(value).upper()
    return text.startswith(("L", "M", "S")) or "M" in text


def _still_size_candidates(value: Any) -> list[list[Any]]:
    candidates: list[list[Any]] = []
    if isinstance(value, dict):
        lower = {str(key).lower(): child for key, child in value.items()}
        aspect = lower.get("aspect") or lower.get("aspectratio") or lower.get("aspect_ratio")
        size = lower.get("size") or lower.get("stillimagesize") or lower.get("imagesize")
        if aspect is not None and size is not None:
            candidates.append([aspect, size])
        for child in value.values():
            candidates.extend(_still_size_candidates(child))
        return candidates
    if isinstance(value, (list, tuple)):
        atoms = _leaf_values(value)
        for index in range(len(atoms) - 1):
            if _looks_like_aspect(atoms[index]) and _looks_like_still_size(atoms[index + 1]):
                candidates.append([atoms[index], atoms[index + 1]])
        for child in value:
            candidates.extend(_still_size_candidates(child))
    return _dedupe(candidates)


def _white_balance_candidates(value: Any) -> list[list[Any]]:
    candidates: list[list[Any]] = []
    if isinstance(value, dict):
        lower = {str(key).lower(): child for key, child in value.items()}
        mode = lower.get("whitebalancemode") or lower.get("mode")
        if mode is not None:
            color_temperature = lower.get("colortemperature", -1)
            candidates.append([mode, False, color_temperature])
        for child in value.values():
            candidates.extend(_white_balance_candidates(child))
        return candidates
    if isinstance(value, (list, tuple)):
        for child in value:
            candidates.extend(_white_balance_candidates(child))
    return _dedupe(candidates)


def _result(payload: dict[str, Any] | None) -> Any:
    if not isinstance(payload, dict):
        return payload
    if isinstance(payload.get("json"), dict):
        return payload["json"].get("result")
    return payload.get("result")


def _current_values(strategy: SmartRemoteValueStrategy, current_response: dict[str, Any] | None) -> list[Any]:
    current = _result(current_response)
    if strategy.method == "setStillSize":
        pairs = _still_size_candidates(current)
        return pairs or ([current] if current is not None else [])
    if strategy.method == "setWhiteBalance":
        values = _white_balance_candidates(current)
        return values or ([current] if current is not None else [])
    atoms = _leaf_values(current)
    return atoms or ([current] if current is not None else [])


def available_candidates(
    strategy: SmartRemoteValueStrategy,
    available_response: dict[str, Any],
) -> list[Any]:
    available = _result(available_response)
    if strategy.method == "setStillSize":
        return _still_size_candidates(available)
    if strategy.method == "setWhiteBalance":
        return _white_balance_candidates(available)
    return _dedupe(_leaf_values(available))


def choose_first_available_alternate(
    strategy: SmartRemoteValueStrategy,
    available_response: dict[str, Any],
    current_response: dict[str, Any] | None = None,
) -> Any:
    candidates = available_candidates(strategy, available_response)
    if not candidates:
        raise ValueError(
            f"{strategy.available_getter} did not expose parseable candidates for {strategy.setting_id}"
        )
    current_keys = {_canonical(value) for value in _current_values(strategy, current_response)}
    for candidate in candidates:
        if _canonical(candidate) not in current_keys:
            return candidate
    raise ValueError(
        f"{strategy.available_getter} exposed candidates for {strategy.setting_id}, "
        "but none differed from the current value"
    )
