"""Offline surface/description contracts, not a measurement of LLM tool-choice accuracy."""
from __future__ import annotations

import asyncio
from itertools import combinations
import json
import os
from pathlib import Path
import re
import sys
from types import SimpleNamespace

import pytest
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

ROOT = Path(__file__).resolve().parents[1]
AGENT = ["shoot", "sun_times", "detect_camera", "get_camera_settings"]
EXPERT = AGENT + ["set_exposure", "set_control", "capture_photo", "auto_expose", "get_liveview", "record"]
BEFORE = set(EXPERT) - {"record"} | {"list_setting_options", "record_video", "record_native_video"}
CASES = json.loads((Path(__file__).with_name("mcp_tool_choice_cases.json")).read_text())


def assert_guidance(data):
    assert {"engine", "serial", "connected", "last_settings", "settings_source"} <= data["camera_state"].keys()
    assert data["next"]
    for step in data["next"]:
        assert set(step) == {"tool", "when"}
        assert step["tool"] in EXPERT and step["when"]


def enum_values(schema):
    if "enum" in schema:
        return schema["enum"]
    for branch in schema.get("anyOf", []):
        if values := enum_values(branch):
            return values
    return []


@pytest.mark.parametrize("expert_flag", ["0", "1", "true"])
def test_stdio_surface_contract(expert_flag, tmp_path):
    async def check():
        env = {**os.environ, "CAMERA_ENGINE": "mock", "CAMERA_MCP_EXPERT": expert_flag,
               "CAMERA_AUTOPILOT_SHOTS_DIR": str(tmp_path)}
        params = StdioServerParameters(command=sys.executable, args=["server.py"], cwd=str(ROOT), env=env)
        async with stdio_client(params) as (read, write):
            async with ClientSession(read, write) as session:
                await session.initialize()
                listed = (await session.list_tools()).tools
                names = [tool.name for tool in listed]
                assert names == (EXPERT if expert_flag == "1" else AGENT)
                assert len(names) <= 10
                schemas = {tool.name: tool.input_schema["properties"] for tool in listed}
                if expert_flag == "1":
                    assert enum_values(schemas["record"]["mode"]) == ["liveview_clip", "native_movie"]
                    assert "200" in enum_values(schemas["set_exposure"]["iso"])
                    assert "1/250" in enum_values(schemas["set_exposure"]["shutter"])
                for tool in listed:
                    assert tool.description.startswith("Use ")
                for left, right in combinations(listed, 2):
                    a = set(re.findall(r"[a-z_]+", left.description.lower()))
                    b = set(re.findall(r"[a-z_]+", right.description.lower()))
                    assert len(a & b) / len(a | b) <= .6, (left.name, right.name)
                # Ten human-labelled requests: check availability in the intended mode, not semantics.
                assert len(CASES) == 10
                for case in CASES:
                    assert case["before"] in BEFORE
                    assert (case["expected"] in names) == (expert_flag == "1" or case["mode"] == "agent")
                for tool in listed:
                    for parameter, schema in tool.input_schema.get("properties", {}).items():
                        if values := enum_values(schema):
                            result = await session.call_tool(tool.name, {parameter: "INVALID"})
                            text = "\n".join(getattr(block, "text", "") for block in result.content)
                            assert result.is_error and "Example:" in text
                            assert all(str(value) in text for value in values)
                            assert_guidance(result.structured_content)
                for name, arguments in [
                    ("detect_camera", {}), ("get_camera_settings", {}),
                    ("get_camera_settings", {"setting": "iso"}),
                    ("sun_times", {"latitude": 0, "longitude": 0, "date": "2026-10-01"}),
                ]:
                    result = await session.call_tool(name, arguments)
                    assert not result.is_error
                    assert_guidance(result.structured_content)
                    assert result.structured_content["camera_state"]["engine"] == "mock"
                    assert result.structured_content["camera_state"]["connected"] is False
                # Both tool-body and schema errors retain the same envelope.
                for name, arguments in [
                    ("sun_times", {"latitude": 0, "longitude": 0, "date": "invalid"}),
                    ("sun_times", {"latitude": 0, "longitude": 0, "tz": "Invalid/Zone"}),
                    ("shoot", {}), ("get_camera_settings", {"setting": "capture"}),
                ]:
                    result = await session.call_tool(name, arguments)
                    assert result.is_error
                    assert_guidance(result.structured_content)
                if expert_flag == "1":
                    for name, arguments in [
                        ("record", {"mode": "native_movie"}), ("record", {"mode": "liveview_clip"}),
                        ("set_exposure", {"iso": "200"}), ("set_control", {"setting": "focusmode", "value": "Manual"}),
                        ("capture_photo", {"serial": "SERIAL"}), ("auto_expose", {"serial": "SERIAL"}),
                        ("get_liveview", {}),
                    ]:
                        result = await session.call_tool(name, arguments)
                        assert result.is_error and "mock mode" in result.structured_content["detail"]
                        assert_guidance(result.structured_content)
    asyncio.run(check())


@pytest.fixture
def server_module(monkeypatch):
    import server
    monkeypatch.setenv("CAMERA_ENGINE", "mock")
    monkeypatch.setattr(server, "_last_observation", {})
    monkeypatch.setattr(server, "_camera", None)
    def forbidden(*args, **kwargs):
        raise AssertionError("No real camera or model may be reached")
    monkeypatch.setattr(server, "detect_identified", forbidden)
    monkeypatch.setattr(server, "_get_camera", forbidden)
    yield server
    server._stop_keepalive()


def test_mock_status_and_planning_never_probe_usb(server_module):
    server = server_module
    for result in (server.detect_camera(), server.get_camera_settings(),
                   server.get_camera_settings("iso"), server.sun_times(0, 0, "2026-10-01")):
        assert_guidance(result)
        assert result["camera_state"]["serial"] is None
        assert result["camera_state"]["last_settings"] is None
    assert server.record(mode="native_movie").is_error
    for name in BEFORE:
        assert callable(getattr(server, name))


@pytest.mark.parametrize("mode,legacy", [("native_movie", "record_native_video"), ("liveview_clip", "record_video")])
def test_record_delegates_once_and_preserves_evidence(server_module, monkeypatch, mode, legacy):
    server = server_module
    monkeypatch.setattr(server, "_engine_name", lambda: "real")  # fakes only; _get_camera still forbidden
    calls = []
    def fake(seconds, serial):
        calls.append((seconds, serial))
        return ({"ok": True, "clip_existence_verified": False, "audio_verified": False,
                 "retry_record": False} if mode == "native_movie" else ["Saved clip at /ABS/PATH/clip.mp4"])
    monkeypatch.setattr(server, legacy, fake)
    result = server.record(10, mode, "SERIAL")
    data = result if isinstance(result, dict) else result.structured_content
    assert calls == [(10, "SERIAL")]
    assert data["mode"] == mode and data["retry_record"] is False
    assert_guidance(data)
    if mode == "native_movie":
        assert data["clip_existence_verified"] is False and data["audio_verified"] is False


def test_record_uncertainty_never_retries(server_module, monkeypatch):
    server = server_module
    monkeypatch.setattr(server, "_engine_name", lambda: "real")
    calls = []
    def uncertain(**kwargs):
        calls.append(kwargs)
        raise server.RecordingStopUncertain("Stop unproven")
    monkeypatch.setattr(server, "record_native_video", uncertain)
    result = server.record(6, "native_movie", "SERIAL")
    assert len(calls) == 1
    assert result.is_error and result.structured_content["retry_record"] is False
    assert_guidance(result.structured_content)


def test_exposure_validates_all_live_choices_before_writing(server_module, monkeypatch):
    server = server_module
    monkeypatch.setattr(server, "_engine_name", lambda: "real")
    writes = []
    cam = SimpleNamespace(list_options=lambda setting: {"iso": ["200"], "aperture": ["f/4"]}[setting],
                          set_exposure=lambda **values: writes.append(values) or values)
    monkeypatch.setattr(server, "_read", lambda action: action(cam))
    with pytest.raises(ValueError, match="Example:"):
        server.set_exposure(iso="150")
    with pytest.raises(ValueError, match="Example:"):
        server.set_exposure(iso="200", aperture="2.8")
    assert writes == []
    assert server.set_exposure(iso="200", aperture="4")["iso"] == "200"
    assert len(writes) == 1


def test_refusals_include_recovery_without_retry(server_module):
    server = server_module
    results = [server._identity_refusal_result(server.CameraIdentityError("Pass serial", reason="serial_required")),
               server._capture_uncertain_result(server.CaptureOutcomeUncertain("Uncertain shutter")),
               server._final_shutter_refused_result(server.CameraError("Final shutter refused"))]
    for result in results:
        assert result.is_error
        assert_guidance(result.structured_content)
    assert results[0].structured_content["next"][0]["tool"] == "detect_camera"
    assert results[1].structured_content["retry_capture"] is False
    assert results[2].structured_content["final_shutter_issued"] is False


@pytest.mark.parametrize("description,invalid,valid", [
    ({"choices": ["Fine", "RAW"]}, "invalid", "Fine"),
    ({"range": [2000.0, 10000.0, 100.0]}, "2050", "3000"),
])
def test_control_value_errors_include_choices_and_example(server_module, monkeypatch, description, invalid, valid):
    server = server_module
    from sonyremote.camera import gp
    writes = []
    kind = gp.GP_WIDGET_MENU if "choices" in description else gp.GP_WIDGET_RANGE
    cam = SimpleNamespace(describe_setting=lambda setting: {**description, "type": kind, "readonly": False},
                          set_setting=lambda setting, value: writes.append(value) or value)
    monkeypatch.setattr(server, "_ensure_connected", lambda serial=None: cam)
    with pytest.raises(ValueError, match="Example:"):
        server.set_control("imagequality", invalid)
    assert writes == []
    assert server.set_control("imagequality", valid)["applied"] == valid
    assert writes == [valid]
