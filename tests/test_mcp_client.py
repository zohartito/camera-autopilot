"""End-to-end MCP transport test for sony-camera-mcp.

Drives server.py over the REAL MCP stdio transport using the official client SDK
(no in-process shortcuts), so it proves the actual wire protocol works: spawn the
server as a subprocess, initialize a session, list tools, and call two of them.

Runnable standalone:

    ./.venv/bin/python tests/test_mcp_client.py

or under pytest:

    ./.venv/bin/python -m pytest tests/test_mcp_client.py

What it can verify WITHOUT a camera:
  - the full stdio handshake + exact default tool registry
  - sun_times() round-trips real data back over the transport
  - detect_camera() reports mock state without probing USB; server stays alive.

What it CANNOT verify here (needs the Sony A7 II tethered in PC Remote mode):
  - capture_photo(), auto_expose(), get_liveview() returning real frames.
"""

from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

REPO_ROOT = Path(__file__).resolve().parent.parent
EXPECTED_TOOLS = {"shoot", "sun_times", "detect_camera", "get_camera_settings"}


def _result_text(result) -> str:
    """Flatten a CallToolResult's content blocks into one searchable string."""
    parts = []
    for block in result.content:
        text = getattr(block, "text", None)
        if text:
            parts.append(text)
    # structuredContent (dicts like sun_times' return) isn't a text block, so include it too.
    if result.structured_content:
        parts.append(str(result.structured_content))
    return "\n".join(parts)


async def run_checks() -> list[tuple[str, bool, str]]:
    """Run every check against a live server subprocess; return (name, passed, detail) rows."""
    results: list[tuple[str, bool, str]] = []

    server = StdioServerParameters(
        command=sys.executable,
        args=["server.py"],
        cwd=str(REPO_ROOT),
        env={**os.environ, "CAMERA_ENGINE": "mock", "CAMERA_MCP_EXPERT": "0"},
    )

    async with stdio_client(server) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()

            # --- Check 1: exact default registry ---------------------------
            listed = await session.list_tools()
            names = {tool.name for tool in listed.tools}
            missing = EXPECTED_TOOLS - names
            extra = names - EXPECTED_TOOLS
            ok = not missing and not extra
            detail = f"found {len(names)} tools"
            if missing:
                detail += f"; MISSING {sorted(missing)}"
            if extra:
                detail += f"; extra {sorted(extra)}"
            results.append(("list_tools has exactly four agent tools", ok, detail))

            # --- Check 2: sun_times round-trips real data (no camera) --------
            sun = await session.call_tool(
                "sun_times",
                {
                    "latitude": 39.09,
                    "longitude": -120.04,
                    "date": "2026-06-14",
                    "tz": "America/Los_Angeles",
                },
            )
            sun_text = _result_text(sun)
            ok = (
                not sun.is_error
                and "sunset" in sun_text
                and "golden_hour_evening" in sun_text
            )
            detail = "no error; contains sunset + golden_hour_evening"
            if sun.is_error:
                detail = f"isError=True: {sun_text[:200]}"
            elif "sunset" not in sun_text or "golden_hour_evening" not in sun_text:
                detail = f"missing expected keys in: {sun_text[:200]}"
            results.append(("sun_times returns sunset + golden_hour_evening", ok, detail))

            # --- Check 3: discovery is camera-free in mock mode -------------
            detect = await session.call_tool("detect_camera", {})
            state = detect.structured_content or {}
            ok = (not detect.is_error and state.get("cameras") == []
                  and state.get("camera_state", {}).get("connected") is False and bool(state.get("next")))
            results.append(("detect_camera reports mock state without USB", ok, "mock discovery + guidance"))

            # Prove the server is still alive after mock discovery: a second,
            # camera-free call must still succeed over the same session.
            still_alive = await session.call_tool("sun_times", {"latitude": 0.0, "longitude": 0.0})
            ok = not still_alive.is_error
            results.append(
                (
                    "server still responsive after mock discovery",
                    ok,
                    "second sun_times call succeeded" if ok else "server unresponsive",
                )
            )

    return results


def test_mcp_transport():
    results = asyncio.run(run_checks())
    assert all(passed for _, passed, _ in results), results


def main() -> int:
    results = asyncio.run(run_checks())

    print("\n=== MCP transport test results ===")
    for name, passed, detail in results:
        flag = "PASS" if passed else "FAIL"
        print(f"[{flag}] {name} — {detail}")

    passed_count = sum(1 for _, ok, _ in results if ok)
    total = len(results)
    print(f"\nSummary: {passed_count}/{total} checks passed.")
    print(
        "Blocked on real hardware (Sony A7 II in PC Remote mode): "
        "capture_photo / auto_expose / get_liveview live frame verification."
    )

    all_passed = passed_count == total
    print("OVERALL: PASS" if all_passed else "OVERALL: FAIL")
    return 0 if all_passed else 1


if __name__ == "__main__":
    sys.exit(main())
