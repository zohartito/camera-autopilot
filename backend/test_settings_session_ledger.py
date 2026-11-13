"""Tests for latest A7 II live-session ledger reporting."""

from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "backend"))

import settings_session_ledger
import settings_session_runs


def test_session_ledger_uses_repo_relative_output_path() -> None:
    root = REPO_ROOT / "artifacts" / "falsification" / "live_sessions"
    report = settings_session_ledger.build_ledger(out_dir=root, limit_recent=0)

    assert report["summary"]["out_dir"] == "artifacts/falsification/live_sessions"
    assert str(REPO_ROOT) not in report["summary"]["out_dir"]
    print("  session ledger repo-relative output path: ok")


def test_session_ledger_groups_latest_outcome_by_route() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        first = settings_session_runs.run_session(
            route="usb_gphoto",
            phases=["review"],
            out_dir=root,
        )
        latest = settings_session_runs.run_session(
            route="usb_gphoto",
            phases=["evidence"],
            execute=True,
            out_dir=root,
        )
        report = settings_session_ledger.build_ledger(out_dir=root)

    assert report["summary"]["sessions"] == 2
    assert report["summary"]["outcomes"]["planned_only"] == 1
    assert report["summary"]["outcomes"]["needs_hardware_approval"] == 1
    usb = next(row for row in report["latest_by_route"] if row["route"] == "usb_gphoto")
    assert usb["has_session"] is True
    assert usb["latest_session"]["manifest_path"] == latest["manifest_path"]
    assert usb["latest_session"]["outcome"] == "needs_hardware_approval"
    assert "--allow-hardware" in usb["next_command"]
    raw_ptp = next(row for row in report["latest_by_route"] if row["route"] == "raw_ptp")
    assert raw_ptp["has_session"] is False
    assert raw_ptp["next_command"] == "python3 tools/a7ii_live_session_runner.py --route raw_ptp --recommended-sequence"
    assert first["manifest_path"] in [item["manifest_path"] for item in report["recent_sessions"]]
    print("  session ledger latest by route: ok")


def test_session_ledger_filters_setting_scoped_sessions() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        settings_session_runs.run_session(
            route="usb_gphoto",
            phases=["review"],
            out_dir=root,
        )
        scoped = settings_session_runs.run_session(
            route="usb_gphoto",
            setting_id="camera.raw_file_type",
            recommended_sequence=True,
            out_dir=root,
        )
        report = settings_session_ledger.build_ledger(
            out_dir=root,
            route="usb_gphoto",
            setting_id="camera.raw_file_type",
        )

    assert report["summary"]["sessions"] == 1
    assert report["summary"]["routes_with_next_command"] >= 1
    assert report["recent_sessions"][0]["manifest_path"] == scoped["manifest_path"]
    assert report["recent_sessions"][0]["setting_id"] == "camera.raw_file_type"
    assert report["latest_by_route"][0]["route"] == "smart_remote_wifi"
    usb = next(row for row in report["latest_by_route"] if row["route"] == "usb_gphoto")
    assert usb["latest_session"]["setting_id"] == "camera.raw_file_type"
    print("  session ledger setting filter: ok")


def test_session_ledger_tolerates_legacy_manifests_without_aftermath() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        session_dir = Path(tmp) / "20260624-legacy-smart"
        session_dir.mkdir()
        manifest_path = session_dir / "session-manifest.json"
        manifest_path.write_text(
            json.dumps(
                {
                    "timestamp": "20260624-legacy-smart",
                    "route": "smart_remote_wifi",
                    "setting_id": None,
                    "manifest_path": str(manifest_path),
                    "summary": {
                        "commands": 1,
                        "planned": 1,
                        "executed": 0,
                        "blocked": 0,
                        "failed": 0,
                    },
                    "commands": [
                        {
                            "index": 1,
                            "phase": "evidence",
                            "status": "planned",
                            "command": "python3 tools/falsify_smart_remote_api_surface.py",
                        }
                    ],
                },
                indent=2,
            ),
            encoding="utf-8",
        )
        report = settings_session_ledger.build_ledger(out_dir=Path(tmp))

    assert report["summary"]["sessions"] == 1
    smart = next(row for row in report["latest_by_route"] if row["route"] == "smart_remote_wifi")
    assert smart["latest_session"]["outcome"] == "planned_only"
    assert "Legacy manifest" in smart["latest_session"]["note"]
    print("  session ledger legacy manifest fallback: ok")


def test_session_ledger_markdown_includes_next_command() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        settings_session_runs.run_session(
            route="guided_physical",
            setting_id="setup.usb_connection",
            phases=["evidence"],
            execute=True,
            allow_hardware=True,
            out_dir=root,
        )
        report = settings_session_ledger.build_ledger(out_dir=root)
        markdown = settings_session_ledger.render_markdown(report)

    assert "# A7 II Live Session Ledger" in markdown
    assert "needs_operator_observation" in markdown
    assert "--observed-state" in markdown
    assert "python3 tools/a7ii_live_session_runner.py --route raw_ptp --recommended-sequence" in markdown
    print("  session ledger markdown: ok")


if __name__ == "__main__":
    test_session_ledger_uses_repo_relative_output_path()
    test_session_ledger_groups_latest_outcome_by_route()
    test_session_ledger_filters_setting_scoped_sessions()
    test_session_ledger_tolerates_legacy_manifests_without_aftermath()
    test_session_ledger_markdown_includes_next_command()
    print("settings_session_ledger tests: PASS")
