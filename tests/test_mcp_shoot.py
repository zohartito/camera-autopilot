"""MCP transport test for the high-level `shoot` autopilot tool — fully hardware-free.

Drives server.py over the REAL MCP stdio transport (official client SDK, no in-process
shortcuts), with CAMERA_ENGINE=mock so NO camera and NO Ollama are needed. It proves the
MERGED design:

  - `shoot` is registered ALONGSIDE every pre-existing low-level camera tool (additive — the 11
    low-level tools all survive),
  - its input schema exposes `intent` (required str) + `auto_correct` (optional bool),
  - calling shoot("moody golden hour") returns the finished-shot SHAPE: an Image block to look at
    plus an EXPLICIT structuredContent record {ok, engine, intent, auto_correct, image_path,
    settings, summary, why} — the codex design (structuredContent set directly, not buried in a
    text block),
  - the autopilot actually reasoned (engine == 'mock', ok == True, a non-empty plain-language
    "why", a settings dict),
  - the mock-placeholder safeguard means a viewable Image block comes back even on a FRESH clone
    with no sample captures under `captures/`,
  - auto_correct=True still returns a valid shot, degrading CLEANLY to a single frame when the
    vision model is unavailable (a small attempts count + a degrade stopped_reason),
  - the server stays alive across calls.

Runnable standalone:

    CAMERA_ENGINE=mock ./.venv/bin/python tests/test_mcp_shoot.py

or under pytest:

    ./.venv/bin/python -m pytest tests/test_mcp_shoot.py
"""

from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
from mcp.types import ImageContent

REPO_ROOT = Path(__file__).resolve().parent.parent

# Every safe low-level tool that survives the addition of `shoot`. Indefinite movie toggles were
# deliberately removed in favor of bounded native recording.
EXISTING_TOOLS = {
    "detect_camera",
    "get_camera_settings",
    "list_setting_options",
    "set_exposure",
    "set_control",
    "capture_photo",
    "auto_expose",
    "get_liveview",
    "record_video",
    "record_native_video",
    "sun_times",
}

# The keys the explicit structuredContent record must carry (the codex return shape).
EXPECTED_RECORD_KEYS = {
    "ok",
    "engine",
    "intent",
    "auto_correct",
    "image_path",
    "settings",
    "summary",
    "why",
}


def _result_text(result) -> str:
    """Flatten a CallToolResult's text content + structured content into one searchable string."""
    parts = []
    for block in result.content:
        text = getattr(block, "text", None)
        if text:
            parts.append(text)
    if result.structured_content:
        parts.append(str(result.structured_content))
    return "\n".join(parts)


async def run_checks() -> list[tuple[str, bool, str]]:
    results: list[tuple[str, bool, str]] = []

    env = dict(os.environ)
    env["CAMERA_ENGINE"] = "mock"               # no hardware
    # Point BOTH local models at a dead port so the test is deterministic and portable: the intent
    # brain takes its keyword fallback, and the reflexion loop's critique degrades cleanly. (On a box
    # with a live Ollama/vision model the closed loop genuinely engages — also valid, just non-CI.)
    env["OLLAMA_HOST"] = "http://127.0.0.1:1"   # intent brain -> deterministic keyword fallback
    env["VISION_HOST"] = "http://127.0.0.1:1"   # vision critique -> unreachable -> clean degrade

    server = StdioServerParameters(
        command=sys.executable,
        args=["server.py"],
        cwd=str(REPO_ROOT),
        env=env,
    )

    async with stdio_client(server) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()

            # --- Check 1: shoot is registered AND every low-level tool survived -----------
            listed = await session.list_tools()
            tools = {tool.name: tool for tool in listed.tools}
            has_shoot = "shoot" in tools
            missing_existing = EXISTING_TOOLS - set(tools)
            ok = has_shoot and not missing_existing
            detail = f"{len(tools)} tools; shoot={'present' if has_shoot else 'MISSING'}"
            if missing_existing:
                detail += f"; LOST existing {sorted(missing_existing)}"
            results.append(("shoot registered + all 11 low-level tools intact", ok, detail))

            # --- Check 2: shoot's input schema is well-formed ----------------------------
            schema = tools["shoot"].input_schema if has_shoot else {}
            props = schema.get("properties", {})
            required = set(schema.get("required", []))
            ok = (
                "intent" in props
                and "intent" in required
                and props["intent"].get("type") == "string"
                and "auto_correct" in props
                and props["auto_correct"].get("type") == "boolean"
                and "auto_correct" not in required
            )
            results.append(
                ("shoot schema: intent(required str) + auto_correct(bool)", ok,
                 f"props={sorted(props)}, required={sorted(required)}")
            )

            # --- Check 3: shoot('moody golden hour') returns a finished shot --------------
            shot = await session.call_tool("shoot", {"intent": "moody golden hour"})
            record = shot.structured_content  # EXPLICIT structuredContent (codex design)
            has_image = any(isinstance(b, ImageContent) for b in shot.content)
            ok = (
                not shot.is_error
                and isinstance(record, dict)
                and EXPECTED_RECORD_KEYS <= set(record)
                and record.get("ok") is True
                and record.get("engine") == "mock"
                and record.get("intent") == "moody golden hour"
                and record.get("auto_correct") is False
                and bool(record.get("why"))
                and isinstance(record.get("settings"), dict)
                and record.get("settings")
            )
            detail = (
                f"isError={shot.is_error}, image_block={has_image}, "
                f"engine={record.get('engine') if record else None}, "
                f"keys={sorted(record) if isinstance(record, dict) else None}, "
                f"why={(record.get('why') if record else '')[:50]!r}"
            )
            if not isinstance(record, dict):
                detail = f"NO structuredContent; text={_result_text(shot)[:200]!r}"
            results.append(("shoot('moody golden hour') -> finished shot record", ok, detail))

            # --- Check 4: the returned image is something the host can look at -----------
            results.append(
                ("shoot returns a viewable Image block (mock placeholder safeguard)", has_image,
                 "ImageContent present" if has_image else "no ImageContent in result")
            )

            # --- Check 5: auto_correct=True still returns a valid shot, degrades cleanly --
            ac = await session.call_tool(
                "shoot", {"intent": "freeze the action", "auto_correct": True}
            )
            ac_record = ac.structured_content
            ok = (
                not ac.is_error
                and isinstance(ac_record, dict)
                and ac_record.get("ok") is True
                and ac_record.get("auto_correct") is True
                and ac_record.get("engine") == "mock"
                and ac_record.get("attempts", 0) >= 1
                # In mock with no reachable vision model the loop must degrade to a single shot.
                and ac_record.get("stopped_reason") in ("no_vision", "no_critique")
                and any(isinstance(b, ImageContent) for b in ac.content)
            )
            detail = (
                f"isError={ac.is_error}, "
                f"attempts={ac_record.get('attempts') if ac_record else None}, "
                f"stopped_reason={ac_record.get('stopped_reason') if ac_record else None}"
            )
            results.append(("shoot(auto_correct=True) returns a valid shot, clean degrade", ok, detail))

            # --- Check 6: server is still responsive after the autopilot calls ----------
            alive = await session.call_tool("sun_times", {"latitude": 0.0, "longitude": 0.0})
            results.append(
                ("server still responsive after shoot calls", not alive.is_error,
                 "follow-up sun_times succeeded" if not alive.is_error else "server unresponsive")
            )

    return results


def main() -> int:
    results = asyncio.run(run_checks())

    print("\n=== MCP `shoot` autopilot tool test (CAMERA_ENGINE=mock) ===")
    for name, passed, detail in results:
        print(f"[{'PASS' if passed else 'FAIL'}] {name} — {detail}")

    passed_count = sum(1 for _, ok, _ in results if ok)
    total = len(results)
    print(f"\nSummary: {passed_count}/{total} checks passed.")
    all_passed = passed_count == total
    print("OVERALL: PASS" if all_passed else "OVERALL: FAIL")
    return 0 if all_passed else 1


if __name__ == "__main__":
    sys.exit(main())
