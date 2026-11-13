"""Camera-free checks for Smart Remote set-and-observe proof runner."""

from __future__ import annotations

import sys
import tempfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "tools"))

import a7ii_smart_remote_set_observe
import review_smart_remote_api_surface


class FakeRemote:
    def __init__(self):
        self.calls = []

    def begin_session(self):
        self.calls.append(("begin_session", []))
        return ["setFocusMode", "getFocusMode", "getAvailableFocusMode"]

    def call(self, method: str, params: list):
        self.calls.append((method, params))
        if method == "getFocusMode":
            return {"result": ["AF-S"], "id": 1}
        if method == "getAvailableFocusMode":
            return {"result": [["AF-S", "MF"]], "id": 1}
        return {"result": [0], "id": 2}


def test_smart_remote_set_observe_dry_run_does_not_touch_camera() -> None:
    fake = FakeRemote()
    with tempfile.TemporaryDirectory() as tmp:
        report, report_path = a7ii_smart_remote_set_observe.build_report(
            setting_id="camera.focus_mode",
            value="MF",
            method="setFocusMode",
            out_dir=Path(tmp),
            smart_remote_factory=lambda: fake,
        )

    assert report["ok"] is True
    assert report["executed"] is False
    assert report["request"]["method"] == "setFocusMode"
    assert report["request"]["params"] == ["MF"]
    assert not report_path.exists()
    assert fake.calls == []
    print("  Smart Remote set-observe dry run: ok")


def test_smart_remote_set_observe_parses_json_scalar_values() -> None:
    assert a7ii_smart_remote_set_observe._parse_value("1") == 1
    assert a7ii_smart_remote_set_observe._parse_value("-1") == -1
    assert a7ii_smart_remote_set_observe._parse_value("1.5") == 1.5
    assert a7ii_smart_remote_set_observe._parse_value("true") is True
    assert a7ii_smart_remote_set_observe._parse_value("MF") == "MF"
    print("  Smart Remote set-observe JSON scalar parsing: ok")


def test_smart_remote_parameter_shapes_match_live_a7ii() -> None:
    assert a7ii_smart_remote_set_observe._smart_remote_params("setIsoSpeedRate", 3200) == ["3200"]
    assert a7ii_smart_remote_set_observe._smart_remote_params("setIsoSpeedRate", "6400") == ["6400"]
    assert a7ii_smart_remote_set_observe._smart_remote_params("setWhiteBalance", "Daylight") == [
        "Daylight",
        False,
        -1,
    ]
    assert a7ii_smart_remote_set_observe._smart_remote_params(
        "setWhiteBalance",
        {"whiteBalanceMode": "Auto WB", "colorTemperature": -1},
    ) == ["Auto WB", False, -1]
    print("  Smart Remote set-observe A7 II parameter shapes: ok")


def test_smart_remote_set_observe_auto_value_dry_run_does_not_touch_camera() -> None:
    fake = FakeRemote()
    with tempfile.TemporaryDirectory() as tmp:
        report, report_path = a7ii_smart_remote_set_observe.build_report(
            setting_id="camera.focus_mode",
            auto_value="first-available-alternate",
            method="setFocusMode",
            out_dir=Path(tmp),
            smart_remote_factory=lambda: fake,
        )

    assert report["ok"] is True
    assert report["executed"] is False
    assert report["request"]["value_source"] == "auto"
    assert report["request"]["auto_value"] == "first-available-alternate"
    assert not report_path.exists()
    assert fake.calls == []
    print("  Smart Remote set-observe auto-value dry run: ok")


def test_smart_remote_set_observe_requires_write_approval() -> None:
    try:
        a7ii_smart_remote_set_observe.build_report(
            setting_id="camera.focus_mode",
            value="MF",
            method="setFocusMode",
            execute=True,
            allow_camera_write=False,
        )
    except ValueError as exc:
        assert "--allow-camera-write" in str(exc)
    else:
        raise AssertionError("live Smart Remote setter attempt must require write approval")
    print("  Smart Remote set-observe approval guard: ok")


def test_smart_remote_set_observe_checks_observations_before_live_write() -> None:
    fake = FakeRemote()
    try:
        a7ii_smart_remote_set_observe.build_report(
            setting_id="camera.focus_mode",
            value="MF",
            method="setFocusMode",
            execute=True,
            allow_camera_write=True,
            human_verdict="proven",
            smart_remote_factory=lambda: fake,
        )
    except ValueError as exc:
        assert "--observed-before" in str(exc)
    else:
        raise AssertionError("behavior proof observations must be validated before live setter calls")
    assert fake.calls == []
    print("  Smart Remote set-observe observation preflight: ok")


def test_smart_remote_set_observe_writes_attempt_and_behavior_proof() -> None:
    fake = FakeRemote()
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        report, attempt_path = a7ii_smart_remote_set_observe.build_report(
            setting_id="camera.focus_mode",
            value="MF",
            method="setFocusMode",
            execute=True,
            allow_camera_write=True,
            observed_before="AF-S",
            observed_after="MF",
            observed_behavior="getFocusMode reported MF after the setter.",
            human_verdict="proven",
            out_dir=root / "r1_api_surface",
            smart_remote_factory=lambda: fake,
        )
        review_report, review_path = review_smart_remote_api_surface.build_report(
            root / "r1_api_surface",
            out_dir=root / "review",
        )

        assert attempt_path.exists()
        assert review_path.exists()
        assert report["ok"] is True
        assert report["executed"] is True
        assert report["behavior_proof_artifact"]
        assert ("setFocusMode", ["MF"]) in fake.calls
        mapped = {row["setting_id"]: row for row in review_report["registry_mapping"]}
        assert mapped["camera.focus_mode"]["verdict"] == "behavior_proven"
        assert mapped["camera.focus_mode"]["behavior_evidence"][0]["observed_after"] == "MF"
    print("  Smart Remote set-observe behavior proof review: ok")


def test_smart_remote_set_observe_resolves_auto_value_before_live_write() -> None:
    fake = FakeRemote()
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        report, attempt_path = a7ii_smart_remote_set_observe.build_report(
            setting_id="camera.focus_mode",
            auto_value="first-available-alternate",
            method="setFocusMode",
            execute=True,
            allow_camera_write=True,
            observed_before="AF-S",
            observed_after="MF",
            observed_behavior="getFocusMode reported MF after the setter.",
            human_verdict="proven",
            out_dir=root / "r1_api_surface",
            smart_remote_factory=lambda: fake,
        )

        assert attempt_path.exists()
        assert report["ok"] is True
        assert report["request"]["value"] == "MF"
        assert report["request"]["params"] == ["MF"]
        assert report["auto_value_evidence"]["strategy"]["available_getter"] == "getAvailableFocusMode"
    assert ("getFocusMode", []) in fake.calls
    assert ("getAvailableFocusMode", []) in fake.calls
    assert ("setFocusMode", ["MF"]) in fake.calls
    print("  Smart Remote set-observe auto-value live resolution: ok")


def test_smart_remote_set_observe_failed_attempt_is_reviewed() -> None:
    fake = FakeRemote()
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        report, attempt_path = a7ii_smart_remote_set_observe.build_report(
            setting_id="camera.aspect_ratio",
            auto_value="first-available-alternate",
            method="setStillSize",
            execute=True,
            allow_camera_write=True,
            out_dir=root / "r1_api_surface",
            smart_remote_factory=lambda: fake,
        )
        review_report, _review_path = review_smart_remote_api_surface.build_report(
            root / "r1_api_surface",
            out_dir=root / "review",
        )

        assert attempt_path.exists()
        assert report["ok"] is False
        assert report["error"]["type"] == "SettingControlUnavailable"
        mapped = {row["setting_id"]: row for row in review_report["registry_mapping"]}
        assert mapped["camera.aspect_ratio"]["verdict"] == "setter_attempt_failed"
        assert mapped["camera.aspect_ratio"]["setter_attempt_evidence"][0]["method"] == "setStillSize"
        assert review_report["registry_mapping_summary"]["setter_attempt_failed_rows"] == 1
    print("  Smart Remote failed set-observe review: ok")


if __name__ == "__main__":
    test_smart_remote_set_observe_dry_run_does_not_touch_camera()
    test_smart_remote_set_observe_parses_json_scalar_values()
    test_smart_remote_parameter_shapes_match_live_a7ii()
    test_smart_remote_set_observe_auto_value_dry_run_does_not_touch_camera()
    test_smart_remote_set_observe_requires_write_approval()
    test_smart_remote_set_observe_checks_observations_before_live_write()
    test_smart_remote_set_observe_writes_attempt_and_behavior_proof()
    test_smart_remote_set_observe_resolves_auto_value_before_live_write()
    test_smart_remote_set_observe_failed_attempt_is_reviewed()
    print("smart_remote_set_observe tests: PASS")
