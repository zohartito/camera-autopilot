"""Camera-free tests for Smart Remote API-surface registry mapping."""

from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "tools"))

import falsify_smart_remote_api_surface
import review_smart_remote_api_surface


def _write_json(path: Path, payload: dict) -> None:
    path.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")


def test_smart_remote_review_maps_unlocked_api_dump_to_registry_rows() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        apis = [
            "getAvailableApiList",
            "getAvailableIsoSpeedRate",
            "getAvailableFocusMode",
            "getEvent",
            "setIsoSpeedRate",
            "setFocusMode",
            "setTouchAFPosition",
            "setPostviewImageSize",
            "getPostviewImageSize",
            "getAvailablePostviewImageSize",
            "actTakePicture",
        ]
        _write_json(
            root / "camera-after-startLiveview.json",
            {
                "ok": True,
                "request": {"method": "getAvailableApiList"},
                "json": {"result": [apis]},
                "apis": apis,
            },
        )
        _write_json(
            root / "safe-getAvailableIsoSpeedRate.json",
            {
                "ok": True,
                "request": {"method": "getAvailableIsoSpeedRate"},
                "json": {"result": [["AUTO", "100", "200"]]},
            },
        )
        _write_json(
            root / "safe-getAvailableFocusMode.json",
            {
                "ok": True,
                "request": {"method": "getAvailableFocusMode"},
                "json": {"result": [["AF-S", "AF-C", "MF"]]},
            },
        )
        _write_json(
            root / "summary.json",
            {
                "setter_candidates_after_unlock": {
                    "count": 3,
                    "methods": ["setIsoSpeedRate", "setFocusMode", "setTouchAFPosition"],
                },
                "safe_camera_methods_after_unlock": {
                    "getAvailableIsoSpeedRate": {"ok": True},
                    "getAvailableFocusMode": {"ok": True},
                    "getPostviewImageSize": {"ok": True},
                    "getAvailablePostviewImageSize": {"ok": True},
                    "getEvent": {"ok": True},
                },
            },
        )

        report, report_path = review_smart_remote_api_surface.build_report(
            root,
            out_dir=root / "out",
        )

    assert report_path.name.endswith("-smart-remote-review.json")
    assert report["artifact_summary"]["discovered_method_count"] >= 8
    assert report["registry_mapping_summary"]["smart_remote_rows"] >= 30
    assert report["registry_mapping_summary"]["mapped_candidate_rows"] >= 4

    mapped = {row["setting_id"]: row for row in report["registry_mapping"]}
    assert "setIsoSpeedRate" in mapped["camera.iso"]["matched_setters"]
    assert mapped["camera.iso"]["verdict"] == "route_setter_seen_in_artifact"
    assert "setFocusMode" in mapped["camera.focus_mode"]["matched_setters"]
    assert "getAvailableFocusMode" in mapped["camera.focus_mode"]["matched_getters"]
    assert "setTouchAFPosition" in mapped["camera.focus_area"]["matched_setters"]
    assert mapped["camera.focus_area"]["verdict"] == "route_setter_seen_in_artifact"
    assert "setPostviewImageSize" in mapped["phone.review_image_size"]["matched_setters"]
    assert "setPostviewImageSize" not in mapped["camera.quality"]["matched_setters"]
    assert "getEvent" in mapped["phone.camera_information"]["matched_getters"]
    print("  Smart Remote artifact-to-registry mapping: ok")


def test_smart_remote_probe_repeat_records_3x_failure_signature() -> None:
    original_call = falsify_smart_remote_api_surface._call

    def fake_call(
        endpoint: str,
        method: str,
        params: list | None = None,
        timeout: float = 1.5,
        transport: str = "urllib",
    ) -> dict:
        return {
            "ok": False,
            "endpoint": endpoint,
            "request": {"method": method, "params": params or [], "id": 1, "version": "1.0"},
            "error_type": "URLError",
            "error": "fake timeout",
        }

    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        falsify_smart_remote_api_surface._call = fake_call
        try:
            rc = falsify_smart_remote_api_surface.main(
                [
                    "--base",
                    "http://fake-camera/sony",
                    "--service",
                    "camera",
                    "--repeat",
                    "3",
                    "--no-network-snapshot",
                    "--out-dir",
                    str(root),
                ]
            )
        finally:
            falsify_smart_remote_api_surface._call = original_call

        repeat_reports = list(root.glob("*-repeat-summary.json"))
        attempts = list(root.glob("*-camera-getAvailableApiList.json"))
        report = json.loads(repeat_reports[0].read_text(encoding="utf-8"))

    assert rc == 0
    assert len(repeat_reports) == 1
    assert len(attempts) == 3
    repeated = report["repeated_3x_signatures"]["camera"][0]
    assert repeated["count"] == 3
    assert repeated["signature"]["error"] == "fake timeout"
    assert "not automatically PROVEN-NO" in report["note"]
    print("  Smart Remote repeat probe 3x signature: ok")


def test_smart_remote_probe_extracts_get_event_api_list() -> None:
    payload = {
        "ok": True,
        "json": {
            "result": [
                {"type": "cameraStatus", "cameraStatus": "IDLE"},
                {
                    "type": "availableApiList",
                    "names": ["getEvent", "startLiveview", "setLiveviewFrameInfo"],
                },
            ]
        },
    }

    apis = falsify_smart_remote_api_surface._extract_api_list(payload)

    assert apis == ["getEvent", "startLiveview", "setLiveviewFrameInfo"]
    assert falsify_smart_remote_api_surface._setter_candidates(apis) == ["setLiveviewFrameInfo"]
    print("  Smart Remote getEvent API-list extraction: ok")


if __name__ == "__main__":
    test_smart_remote_review_maps_unlocked_api_dump_to_registry_rows()
    test_smart_remote_probe_repeat_records_3x_failure_signature()
    test_smart_remote_probe_extracts_get_event_api_list()
    print("smart_remote_api_review tests: PASS")
