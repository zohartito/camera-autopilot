"""Camera-free tests for the A7 II offline/live work split."""

from __future__ import annotations

import contextlib

import settings_offline_work
import settings_readiness


@contextlib.contextmanager
def _pinned_local_toolchain(*, missing: tuple[str, ...] = ()):
    """Pin CLI discovery so the offline/live split does not depend on host-installed tools.

    Route readiness resolves tools through ``shutil.which``. The Mac dev box has
    gphoto2/curl/file on PATH; the ubuntu CI runner does not, which silently reclassifies
    live-camera routes and flips the headline counts. Pinning the resolver makes the split
    deterministic on both hosts without changing the classification logic under test.
    """
    original = settings_readiness.shutil.which

    def resolver(tool: str) -> str | None:
        return None if tool in missing else f"/usr/bin/{tool}"

    settings_readiness.shutil.which = resolver
    try:
        yield
    finally:
        settings_readiness.shutil.which = original


def test_offline_work_report_splits_local_work_from_live_tests() -> None:
    with _pinned_local_toolchain():
        report = settings_offline_work.build_report()
    summary = report["summary"]
    routes = {item["route"]: item for item in report["routes"]}

    assert summary["completion_state"] == "needs_live_evidence"
    assert summary["remaining_live_test_routes"] == 4
    # The headline count must match the report's own live-test bucket.
    assert summary["remaining_live_test_routes"] == len(report["remaining_live_tests"])
    assert summary["external_package_routes"] == 1
    assert summary["no_action_or_stopped_routes"] == 2
    assert summary["camera_free_action_count"] >= 8
    assert routes["camera_remote_command"]["offline_class"] == "waiting_external_package"
    assert routes["raw_ptp"]["offline_class"] == "no_action_or_stopped"
    assert routes["smart_remote_wifi"]["camera_required"] is True
    assert all(item["camera_required"] for item in report["remaining_live_tests"])
    assert all(item["offline_class"] == "waiting_external_package" for item in report["waiting_external_package"])
    assert any(
        "find_camera_remote_command_package.py --include-common-search" in action["command"]
        for action in report["no_camera_route_actions"]
    )
    assert any(
        "review_smart_remote_phone_preferences.py artifacts/falsification/r1_phone_settings_manual" in action["command"]
        for action in report["no_camera_route_actions"]
    )
    assert not any("openmemories" in action["command"] for action in report["no_camera_route_actions"])
    assert summary["closed_without_proof_rows"] == 0
    assert routes["openmemories"]["status"] == "no_action"
    markdown = settings_offline_work.to_markdown(report)
    assert "Remaining Live Tests Only" in markdown
    assert "closed_without_proof_rows:" in markdown
    assert "closed without behavior proof" not in markdown  # zero rows must not produce a warning
    print("  settings offline work split: ok")


def test_offline_work_report_defers_live_route_missing_a_local_tool() -> None:
    # Regression for the ubuntu CI runner, where gphoto2 is not installed: the USB
    # route must drop out of the live-test queue into offline-prerequisite-first (install
    # the local tool first), not vanish, and the headline count must fall to match.
    with _pinned_local_toolchain(missing=("gphoto2",)):
        report = settings_offline_work.build_report()
    routes = {item["route"]: item for item in report["routes"]}
    assert routes["usb_gphoto"]["offline_class"] == "offline_prerequisite_first"
    assert routes["usb_gphoto"]["camera_required"] is False
    summary = report["summary"]
    assert summary["remaining_live_test_routes"] == 3
    assert summary["remaining_live_test_routes"] == len(report["remaining_live_tests"])
    print("  settings offline work defers a tool-missing live route: ok")


if __name__ == "__main__":
    test_offline_work_report_splits_local_work_from_live_tests()
    test_offline_work_report_defers_live_route_missing_a_local_tool()
    print("settings_offline_work tests: PASS")
