"""Camera-free tests for the A7 II all-route live evidence marathon."""

from __future__ import annotations

import sys
import tempfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "backend"))

import settings_live_marathon

EXPECTED_ACTIVE_ROUTES = [
    "smart_remote_wifi", "usb_gphoto", "smart_remote_phone", "camera_remote_command",
    "guided_physical",
]


def test_live_marathon_dry_run_plans_every_route() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        report = settings_live_marathon.run_marathon(out_dir=Path(tmp))
        assert Path(report["manifest_path"]).exists()
        assert Path(report["summary_path"]).exists()

    assert report["execute"] is False
    assert report["summary"]["routes"] == len(EXPECTED_ACTIVE_ROUTES)
    assert report["summary"]["sessions_created"] == len(EXPECTED_ACTIVE_ROUTES)
    assert report["summary"]["planned"] == len(EXPECTED_ACTIVE_ROUTES)
    assert report["summary"]["needs_attention"] == 0
    assert report["routes_requested"] == EXPECTED_ACTIVE_ROUTES
    assert all(entry["status"] == "planned" for entry in report["routes"])
    assert all(entry["outcome"] == "planned_only" for entry in report["routes"])
    assert report["routes"][0]["command"].startswith(
        "python3 tools/a7ii_live_session_runner.py --route smart_remote_wifi --recommended-sequence"
    )
    markdown = settings_live_marathon.render_markdown(report)
    assert "# A7 II Live Evidence Marathon" in markdown
    assert "Smart Remote" in markdown
    print("  live marathon dry-run plan: ok")


def test_live_marathon_route_filter() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        closed = settings_live_marathon.run_marathon(routes=["usb_gphoto"], out_dir=Path(tmp))

    assert closed["summary"]["routes"] == 1
    assert closed["routes_requested"] == ["usb_gphoto"]
    assert closed["routes"][0]["route"] == "usb_gphoto"

    with tempfile.TemporaryDirectory() as tmp:
        active = settings_live_marathon.run_marathon(routes=["camera_remote_command"], out_dir=Path(tmp))

    assert active["summary"]["routes"] == 1
    assert active["routes_requested"] == ["camera_remote_command"]
    assert active["routes"][0]["route"] == "camera_remote_command"
    assert active["routes"][0]["status"] == "planned"
    print("  live marathon route filter: ok")


def test_live_marathon_execute_stops_after_first_unresolved_route() -> None:
    calls = []

    def fake_runner(
        command: str,
        cwd: Path,
    ) -> settings_live_marathon.settings_session_runs.CommandResult:
        calls.append(command)
        return settings_live_marathon.settings_session_runs.CommandResult(
            0,
            "package locator test stub\n",
            "",
        )

    with tempfile.TemporaryDirectory() as tmp:
        report = settings_live_marathon.run_marathon(
            execute=True,
            allow_hardware=False,
            out_dir=Path(tmp),
            command_runner=fake_runner,
        )

    assert report["execute"] is True
    assert report["allow_hardware"] is False
    assert report["summary"]["sessions_created"] == 1
    assert report["summary"]["needs_attention"] == 1
    assert report["summary"]["skipped"] == 4
    assert report["summary"]["first_unresolved_route"] == "smart_remote_wifi"
    assert report["routes"][0]["route"] == "smart_remote_wifi"
    assert report["routes"][0]["status"] == "needs_attention"
    assert report["routes"][0]["outcome"] == "needs_hardware_approval"
    assert report["summary"]["next_commands"]
    assert calls == []  # hardware policy blocks before invoking a command runner
    print("  live marathon stop on unresolved route: ok")


def test_live_marathon_rejects_unknown_route() -> None:
    try:
        settings_live_marathon.run_marathon(routes=["nope"])
    except ValueError as exc:
        assert "unknown route" in str(exc)
    else:
        raise AssertionError("unknown route should fail")
    print("  live marathon unknown route guard: ok")


if __name__ == "__main__":
    test_live_marathon_dry_run_plans_every_route()
    test_live_marathon_route_filter()
    test_live_marathon_execute_stops_after_first_unresolved_route()
    test_live_marathon_rejects_unknown_route()
    print("settings_live_marathon tests: PASS")
