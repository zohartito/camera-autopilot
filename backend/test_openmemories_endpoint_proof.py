"""Camera-free checks for OpenMemories proof-app HTTP endpoint capture."""

from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path
from urllib.parse import urlparse

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "tools"))

import prove_openmemories_endpoint_surface
import review_openmemories_artifacts


class FakeResponse:
    def __init__(self, status: int, payload: dict):
        self.status = status
        self.headers = {"Content-Type": "application/json"}
        self._raw = json.dumps(payload).encode("utf-8")

    def read(self) -> bytes:
        return self._raw

    def getcode(self) -> int:
        return self.status


def _fake_opener(request, timeout: float = 5.0) -> FakeResponse:
    path = urlparse(request.full_url).path
    method = request.get_method()
    if method == "GET" and path == "/health":
        return FakeResponse(200, {"ok": True, "route": "openmemories"})
    if method == "GET" and path == "/settings":
        return FakeResponse(200, {"ok": True, "settings": ["preview.running"]})
    if method == "GET" and path == "/parameters":
        return FakeResponse(
            200,
            {
                "ok": True,
                "id": "param.dump",
                "readOnly": True,
                "parameterCount": 2,
                "parameters": {"preview-size": "640x480", "sony-exposure-mode": "P"},
            },
        )
    if method == "GET" and path.startswith("/settings/"):
        setting_id = path.removeprefix("/settings/")
        if setting_id == "param.dump":
            return FakeResponse(
                200,
                {
                    "ok": True,
                    "id": setting_id,
                    "readOnly": True,
                    "parameterCount": 2,
                    "parameters": {"preview-size": "640x480", "sony-exposure-mode": "P"},
                },
            )
        if setting_id == "capture.count":
            return FakeResponse(200, {"ok": True, "id": setting_id, "value": 1})
        if setting_id == "capture.bytes":
            return FakeResponse(200, {"ok": True, "id": setting_id, "value": 12345})
        if setting_id == "preview.running":
            return FakeResponse(200, {"ok": True, "id": setting_id, "value": True})
        return FakeResponse(200, {"ok": True, "id": setting_id, "value": False})
    if method == "POST" and path == "/preview":
        return FakeResponse(200, {"ok": True, "preview": "started"})
    if method == "POST" and path == "/focus":
        return FakeResponse(200, {"accepted": True, "action": "autofocus"})
    if method == "POST" and path == "/capture":
        return FakeResponse(200, {"accepted": True, "captureCountBefore": 0})
    if method == "GET" and path == "/media/latest":
        return FakeResponse(
            200,
            {
                "cameraOpen": True,
                "captureCount": 1,
                "captureInFlight": False,
                "latestCapture": "/sdcard/DCIM/100MSDCF/DSC00001.JPG",
                "latestCaptureBytes": 12345,
                "latestError": "",
            },
        )
    return FakeResponse(404, {"ok": False, "error": "missing"})


def _false_ok_opener(request, timeout: float = 5.0) -> FakeResponse:
    path = urlparse(request.full_url).path
    return FakeResponse(200, {"ok": False, "endpoint": path, "error": "camera_not_open"})


def _accepted_only_opener(request, timeout: float = 5.0) -> FakeResponse:
    path = urlparse(request.full_url).path
    method = request.get_method()
    if method == "GET" and path == "/health":
        return FakeResponse(200, {"ok": True, "cameraOpen": True})
    if method == "GET" and path == "/settings":
        return FakeResponse(200, {"ok": True, "settings": ["capture.count", "capture.bytes"]})
    if method == "GET" and path == "/settings/capture.count":
        return FakeResponse(200, {"ok": True, "id": "capture.count", "value": 0})
    if method == "GET" and path == "/settings/capture.bytes":
        return FakeResponse(200, {"ok": True, "id": "capture.bytes", "value": 0})
    if method == "GET" and path == "/media/latest":
        return FakeResponse(
            200,
            {
                "cameraOpen": True,
                "captureCount": 0,
                "captureInFlight": False,
                "latestCapture": "",
                "latestCaptureBytes": 0,
                "latestError": "",
            },
        )
    if method == "GET" and path.startswith("/settings/"):
        return FakeResponse(200, {"ok": True, "id": path.removeprefix("/settings/"), "value": False})
    if method == "POST" and path in {"/preview", "/focus", "/capture"}:
        payload = {"accepted": True, "action": path.strip("/")}
        if path == "/capture":
            payload["captureCountBefore"] = 0
        return FakeResponse(200, payload)
    return FakeResponse(404, {"ok": False, "error": "missing"})


def _write_preflight(root: Path) -> None:
    preflight = root / "route3-preflight" / "route3-preflight.json"
    preflight.parent.mkdir(parents=True, exist_ok=True)
    preflight.write_text(
        json.dumps(
            {
                "status": "NEEDS-TOOLING",
                "generated_app": {
                    "path": str(root / "route3_openmemories_proof_app"),
                    "endpoints": [
                        "GET /health",
                        "GET /settings",
                        "GET /settings/{id}",
                        "POST /preview",
                        "POST /focus",
                        "POST /capture",
                    ],
                },
            },
            indent=2,
            sort_keys=True,
        ),
        encoding="utf-8",
    )


def test_openmemories_endpoint_capture_records_safe_proofs_for_review() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        _write_preflight(root)
        summary, summary_path = prove_openmemories_endpoint_surface.capture_surface(
            base="http://camera.local:8080",
            setting_id="camera.raw_file_type",
            out_dir=root,
            opener=_fake_opener,
        )
        report, _ = review_openmemories_artifacts.build_report([root], out_dir=root / "review")

    assert summary_path.name == "summary.json"
    assert summary["result_count"] == 3
    assert summary["ok_count"] == 3
    assert report["artifact_summary"]["endpoint_proof_count"] == 3
    assert "GET /health" in report["artifact_summary"]["proven_endpoints"]
    assert "GET /settings/camera.raw_file_type" in report["artifact_summary"]["proven_endpoints"]
    assert report["registry_mapping_summary"]["proof_app_setting_endpoint_proven_rows"] == 1
    mapped = {row["setting_id"]: row for row in report["registry_mapping"]}
    assert mapped["camera.raw_file_type"]["verdict"] == "proof_app_setting_endpoint_proven"
    print("  OpenMemories safe endpoint proof capture: ok")


def test_openmemories_endpoint_capture_can_include_parameter_dump() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        _write_preflight(root)
        summary, _ = prove_openmemories_endpoint_surface.capture_surface(
            base="http://camera.local:8080",
            setting_id="preview.running",
            include_parameter_dump=True,
            out_dir=root,
            opener=_fake_opener,
        )
        report, _ = review_openmemories_artifacts.build_report([root], out_dir=root / "review")

    assert summary["include_parameter_dump"] is True
    assert summary["result_count"] == 5
    assert summary["ok_count"] == 5
    assert "GET /parameters" in report["artifact_summary"]["proven_endpoints"]
    assert "GET /settings/param.dump" in report["artifact_summary"]["proven_endpoints"]
    print("  OpenMemories parameter dump proof capture: ok")


def test_openmemories_action_capture_is_explicit_and_reviewable() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        _write_preflight(root)
        try:
            prove_openmemories_endpoint_surface.capture_surface(
                base="http://camera.local:8080",
                include_actions=True,
                out_dir=root,
                opener=_fake_opener,
            )
        except ValueError as exc:
            assert "--allow-camera-actions" in str(exc)
        else:
            raise AssertionError("action endpoint capture must require explicit camera-action approval")

        summary, _ = prove_openmemories_endpoint_surface.capture_surface(
            base="http://camera.local:8080",
            include_actions=True,
            allow_camera_actions=True,
            out_dir=root,
            opener=_fake_opener,
        )
        report, _ = review_openmemories_artifacts.build_report([root], out_dir=root / "review")

    assert summary["include_actions"] is True
    assert summary["result_count"] == 10
    assert summary["ok_count"] == 8
    assert any(
        result["endpoint"] == "/media/latest#capture-completion" and result["ok"]
        for result in summary["results"]
    )
    assert "POST /capture" not in report["artifact_summary"]["proven_endpoints"]
    assert "POST /focus" not in report["artifact_summary"]["proven_endpoints"]
    assert "GET /settings/capture.count" in report["artifact_summary"]["proven_endpoints"]
    assert "GET /settings/capture.bytes" in report["artifact_summary"]["proven_endpoints"]
    assert "GET /media/latest" in report["artifact_summary"]["proven_endpoints"]
    assert report["registry_mapping_summary"]["route_level_verdict"] == "proof_app_action_endpoint_proven"
    assert report["registry_mapping_summary"]["proof_app_action_endpoint_proven_rows"] == 0
    assert report["registry_mapping_summary"]["proof_app_setting_not_exposed_rows"] >= 1
    print("  OpenMemories action endpoint proof capture: ok")


def test_openmemories_focus_observation_requires_actions_and_saved_artifact() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        artifact = root / "focus-proof.txt"
        artifact.write_text("before/after focus observation")

        try:
            prove_openmemories_endpoint_surface.capture_surface(
                base="http://camera.local:8080",
                focus_observed_before="near target sharp",
                focus_observed_after="far target sharp",
                focus_observation_note="focus moved after /focus",
                focus_proof_artifact=artifact,
                out_dir=root,
                opener=_fake_opener,
            )
        except ValueError as exc:
            assert "requires --include-actions" in str(exc)
        else:
            raise AssertionError("focus observation must require explicit camera actions")

        summary, _ = prove_openmemories_endpoint_surface.capture_surface(
            base="http://camera.local:8080",
            include_actions=True,
            allow_camera_actions=True,
            focus_observed_before="near target sharp",
            focus_observed_after="far target sharp",
            focus_observation_note="focus moved after /focus",
            focus_proof_artifact=artifact,
            out_dir=root,
            opener=_fake_opener,
        )
        report, _ = review_openmemories_artifacts.build_report([root], out_dir=root / "review")

    assert summary["focus_observation_ok"] is True
    assert report["artifact_summary"]["focus_observation_count"] == 1
    assert report["artifact_summary"]["focus_observation_proven"] is True
    print("  OpenMemories focus observation proof capture: ok")


def test_openmemories_accepted_actions_do_not_prove_completion_without_post_action_state() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        _write_preflight(root)
        summary, _ = prove_openmemories_endpoint_surface.capture_surface(
            base="http://camera.local:8080",
            include_actions=True,
            allow_camera_actions=True,
            out_dir=root,
            opener=_accepted_only_opener,
        )
        report, _ = review_openmemories_artifacts.build_report([root], out_dir=root / "review")

    assert summary["include_actions"] is True
    assert summary["result_count"] == 10
    assert summary["ok_count"] < 10
    assert "POST /capture" not in report["artifact_summary"]["proven_endpoints"]
    assert "POST /focus" not in report["artifact_summary"]["proven_endpoints"]
    assert report["registry_mapping_summary"]["proof_app_action_endpoint_proven_rows"] == 0
    print("  OpenMemories accepted-only action guard: ok")


def test_openmemories_endpoint_capture_does_not_prove_json_ok_false() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        _write_preflight(root)
        summary, _ = prove_openmemories_endpoint_surface.capture_surface(
            base="http://camera.local:8080",
            setting_id="preview.running",
            out_dir=root,
            opener=_false_ok_opener,
        )
        report, _ = review_openmemories_artifacts.build_report([root], out_dir=root / "review")

    assert summary["result_count"] == 3
    assert summary["ok_count"] == 0
    assert report["artifact_summary"]["endpoint_proof_count"] == 3
    assert report["artifact_summary"]["proven_endpoints"] == []
    print("  OpenMemories endpoint ok:false guard: ok")


if __name__ == "__main__":
    test_openmemories_endpoint_capture_records_safe_proofs_for_review()
    test_openmemories_endpoint_capture_can_include_parameter_dump()
    test_openmemories_action_capture_is_explicit_and_reviewable()
    test_openmemories_focus_observation_requires_actions_and_saved_artifact()
    test_openmemories_accepted_actions_do_not_prove_completion_without_post_action_state()
    test_openmemories_endpoint_capture_does_not_prove_json_ok_false()
    print("openmemories_endpoint_proof tests: PASS")
