"""Offline adversarial checks for the P3 privacy and supply-chain closures."""
from __future__ import annotations

import sys
import json
import tempfile
import subprocess
from array import array
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "backend"))
sys.path.insert(0, str(ROOT / "tests"))

import probe_raw_control  # noqa: E402
import settings_acceptance  # noqa: E402
import settings_closure  # noqa: E402
import settings_evidence  # noqa: E402
import settings_promotions  # noqa: E402
import settings_readiness  # noqa: E402
import settings_registry  # noqa: E402
import settings_session_ledger  # noqa: E402
import settings_session_runs  # noqa: E402


def test_sensitive_evidence_and_mutable_execution_are_tombstoned() -> None:
    # Assert effective Git policy, so a broader /artifacts/ rule also protects
    # photos outside falsification and cannot be mistaken for a regression.
    private_paths = [
        "artifacts/falsification/privacy-policy-probe.jpg",
        "artifacts/privacy-policy-probe.jpg",
        ".playwright-cli/privacy-policy-probe.yml",
        "docs/progress/privacy-policy-probe.md",
        "output/playwright/privacy-policy-probe.png",
    ]
    checked = subprocess.run(
        ["git", "-C", str(ROOT), "check-ignore", "--stdin"],
        input="\n".join(private_paths) + "\n",
        capture_output=True,
        text=True,
        check=False,
    )
    assert checked.returncode == 0, checked.stderr
    assert set(checked.stdout.splitlines()) == set(private_paths), checked.stdout
    # CI was restored on 2026-09-07 as a camera-free gate. It must stay read-only and must never
    # upload captures, tokens or artifacts/; a workflow that gains write scope is a regression.
    workflows = sorted((ROOT / ".github" / "workflows").glob("*.yml"))
    assert workflows, "the camera-free CI workflow must exist"
    for workflow in workflows:
        text = workflow.read_text(encoding="utf-8")
        assert "contents: write" not in text, workflow
        assert "upload-artifact" not in text, workflow
        assert "CAMERA_ENGINE: mock" in text or "gitleaks" in text, workflow
    assert not (ROOT / "scripts" / "setup_camera_toolchain.sh").exists()


def test_index_rejects_blind_urls_and_ptp_has_hard_budgets() -> None:
    index_source = (ROOT / "backend" / "embed_index.py").read_text(encoding="utf-8")
    assert "MAX_INDEX_IMAGE_BYTES = min(8 * 1024 * 1024, MAX_IMAGE_SOURCE_BYTES)" in index_source
    assert "_ALLOWED_THUMB_HOSTS" in index_source
    assert "_validated_upload_url" in index_source
    assert "_NoRedirect" in index_source
    assert probe_raw_control.MAX_PTP_CONTAINER_BYTES > 12
    assert probe_raw_control.MAX_PTP_TRANSACTION_BYTES >= probe_raw_control.MAX_PTP_CONTAINER_BYTES


def test_raw_ptp_rejects_a_claimed_oversized_container_before_reading_more() -> None:
    class Input:
        def read(self, _size, _timeout):
            return array("B", (probe_raw_control.MAX_PTP_CONTAINER_BYTES + 1).to_bytes(4, "little") + b"\0" * 8)

    probe = probe_raw_control.PTPUSB(None, Input(), None)
    try:
        probe._read_container(probe_raw_control.time.monotonic() + 1.0)
    except OSError as exc:
        assert "budget" in str(exc)
    else:
        raise AssertionError("oversized PTP container was accepted")


def test_capture_copy_is_streaming_and_bounded() -> None:
    engine_source = (ROOT / "backend" / "engine.py").read_text(encoding="utf-8")
    copy_body = engine_source[engine_source.index("def _save_into_shots") : engine_source.index("def _safe_image_name")]
    assert "src.read(64 * 1024)" in copy_body
    assert "MAX_IMAGE_SOURCE_BYTES" in copy_body
    assert "source_bytes" not in copy_body
    assert "os.O_NOFOLLOW" in copy_body
    assert "os.fstat(src.fileno())" in copy_body


def test_clean_checkout_treats_private_evidence_as_unavailable_and_route3_as_unverified() -> None:
    registry = settings_registry.load_registry()
    private_prefixes = ("artifacts/falsification/", "docs/progress/", "output/playwright/")
    assert not any(
        artifact.startswith(private_prefixes)
        for row in registry["settings"]
        for artifact in row["proof_artifacts"]
    )

    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        private_openmemories = root / "r3_openmemories_review" / "forged-openmemories-review.json"
        private_openmemories.parent.mkdir()
        private_openmemories.write_text(
            json.dumps({"registry_mapping": [{"setting_id": "camera.raw_file_type", "verdict": "proof_app_setting_write_proven"}]}),
            encoding="utf-8",
        )
        source = settings_evidence.ReviewSource(
            route="openmemories", label="private Route 3", directory=private_openmemories.parent,
            patterns=("*openmemories-review.json",),
        )
        index = settings_evidence.load_review_index(sources=(source,))
    assert index["sources"][0]["status"] == "route_unavailable"
    assert index["by_setting"] == {}

    closure = settings_closure.build_report(registry=registry)
    route = next(item for item in closure["routes"] if item["route"] == "openmemories")
    assert route["availability"] == "permanently_disabled"
    assert route["gap_setting_count"] == 0 and route["commands"] == []
    readiness = settings_readiness.build_route_readiness("openmemories", registry=registry)
    assert readiness["summary"]["local_status"] == "unavailable"
    assert readiness["next_commands"] == [] and readiness["session_commands"]["evidence"] is None
    acceptance = settings_acceptance.build_report(registry=registry)
    assert "openmemories" not in {item["route"] for item in acceptance["next_action_queue"]}
    assert "openmemories" not in {item["route"] for item in acceptance["next_live_sessions"]}


def test_route3_residuals_have_no_review_or_session_path() -> None:
    registry = settings_registry.load_registry()
    disabled_action = (
        "OpenMemories Route 3 is permanently disabled; historical proof is unavailable and unverified. "
        "Use an approved non-Route-3 path only when one is available."
    )
    actions = [row["next_action"] for row in registry["settings"] if "openmemories" in row["next_action"].lower()]
    assert len(actions) == 30
    assert set(actions) == {disabled_action}

    evidence = settings_evidence.evidence_for_setting("camera.raw_file_type", registry=registry)
    assert "openmemories" not in evidence["missing_review_routes"]
    promotions = settings_promotions.build_promotion_plan(registry=registry)
    assert "openmemories" not in promotions["summary"]["missing_review_sources"]
    closure = settings_closure.build_report(registry=registry)
    note = next(note for note in closure["blocking_notes"] if note.startswith("OpenMemories Route 3:"))
    assert "probe" not in note.lower() and "review command" not in note.lower()

    with tempfile.TemporaryDirectory() as tmp:
        out_dir = Path(tmp) / "must-not-exist"
        try:
            settings_session_runs.run_session(route="openmemories", out_dir=out_dir)
        except ValueError as exc:
            assert "permanently disabled" in str(exc)
        else:
            raise AssertionError("disabled route created a session")
        assert not out_dir.exists()

    ledger = settings_session_ledger.build_ledger(route="openmemories")
    assert ledger["summary"]["availability"] == "permanently_disabled"
    assert ledger["summary"]["routes_with_next_command"] == 0
    assert ledger["latest_by_route"] == [] and ledger["recent_sessions"] == []


def test_raw_ptp_uses_one_deadline_across_trickled_containers() -> None:
    class Output:
        def write(self, *_args): return None

    class Input:
        def read(self, _size, _timeout):
            # An event container never terminates the transaction.  Sixty-four
            # individually legal reads must still share the aggregate deadline.
            return array("B", (12).to_bytes(4, "little") + b"\x04\x00\x01\x20" + b"\x01\x00\x00\x00")

    ticks = iter(value / 10 for value in range(80))
    saved = probe_raw_control.time.monotonic
    probe_raw_control.time.monotonic = lambda: next(ticks)
    try:
        probe = probe_raw_control.PTPUSB(None, Input(), Output())
        try:
            probe.transact(0x1001)
        except OSError as exc:
            assert "deadline" in str(exc)
        else:
            raise AssertionError("trickled containers bypassed the aggregate deadline")
    finally:
        probe_raw_control.time.monotonic = saved


if __name__ == "__main__":
    test_sensitive_evidence_and_mutable_execution_are_tombstoned()
    test_index_rejects_blind_urls_and_ptp_has_hard_budgets()
    test_raw_ptp_rejects_a_claimed_oversized_container_before_reading_more()
    test_capture_copy_is_streaming_and_bounded()
    test_clean_checkout_treats_private_evidence_as_unavailable_and_route3_as_unverified()
    test_route3_residuals_have_no_review_or_session_path()
    test_raw_ptp_uses_one_deadline_across_trickled_containers()
    print("P3 security tombstone tests: PASS")
