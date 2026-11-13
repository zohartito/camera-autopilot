"""Camera-free checks for OpenMemories rollback port proof capture."""

from __future__ import annotations

import sys
import tempfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "tools"))

import prove_openmemories_rollback_ports
import review_openmemories_artifacts


class FakeConnection:
    def close(self) -> None:
        return None


def test_openmemories_rollback_ports_records_closed_evidence_for_review() -> None:
    def closed_connector(address: tuple[str, int], timeout: float) -> FakeConnection:
        raise OSError("connection refused")

    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        report, report_path = prove_openmemories_rollback_ports.capture_ports(
            host="camera.local",
            ports=(23, 5555, 8080),
            out_dir=root,
            network_context_proven=True,
            connector=closed_connector,
        )
        review, _ = review_openmemories_artifacts.build_report([root], out_dir=root / "review")

        assert report_path.exists()
        assert report["rollback_closed_evidence"] is True
        assert report["complete_required_scan"] is True
        assert report["missing_required_ports"] == []
        assert report["unexpected_ports"] == []
        assert len(report["closed_ports"]) == 3
        assert review["artifact_summary"]["rollback_port_scan_count"] == 1
        assert review["artifact_summary"]["rollback_closed_evidence"] is True
    print("  OpenMemories rollback closed-port proof: ok")


def test_openmemories_rollback_ports_requires_network_context_for_closed_evidence() -> None:
    def closed_connector(address: tuple[str, int], timeout: float) -> FakeConnection:
        raise OSError("connection refused")

    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        report, _ = prove_openmemories_rollback_ports.capture_ports(
            host="camera.local",
            ports=(23, 5555, 8080),
            out_dir=root,
            connector=closed_connector,
        )
        review, _ = review_openmemories_artifacts.build_report([root], out_dir=root / "review")

    assert report["network_context_proven"] is False
    assert report["rollback_closed_evidence"] is False
    assert review["artifact_summary"]["rollback_closed_evidence"] is False
    print("  OpenMemories rollback requires network context: ok")


def test_openmemories_rollback_ports_records_open_port_as_incomplete() -> None:
    def mixed_connector(address: tuple[str, int], timeout: float) -> FakeConnection:
        host, port = address
        if port == 5555:
            return FakeConnection()
        raise OSError(f"{host}:{port} closed")

    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        report, _ = prove_openmemories_rollback_ports.capture_ports(
            host="camera.local",
            ports=(23, 5555, 8080),
            out_dir=root,
            connector=mixed_connector,
        )
        review, _ = review_openmemories_artifacts.build_report([root], out_dir=root / "review")

    assert report["rollback_closed_evidence"] is False
    assert [item["port"] for item in report["open_ports"]] == [5555]
    assert review["artifact_summary"]["rollback_closed_evidence"] is False
    assert review["artifact_summary"]["open_developer_ports"][0]["port"] == 5555
    print("  OpenMemories rollback open-port detection: ok")


def test_openmemories_rollback_ports_rejects_partial_closed_scan() -> None:
    def closed_connector(address: tuple[str, int], timeout: float) -> FakeConnection:
        raise OSError("connection refused")

    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        report, _ = prove_openmemories_rollback_ports.capture_ports(
            host="camera.local",
            ports=(8080,),
            out_dir=root,
            network_context_proven=True,
            connector=closed_connector,
        )
        review, _ = review_openmemories_artifacts.build_report([root], out_dir=root / "review")

    assert report["complete_required_scan"] is False
    assert report["missing_required_ports"] == [23, 5555]
    assert report["rollback_closed_evidence"] is False
    assert review["artifact_summary"]["rollback_closed_evidence"] is False
    print("  OpenMemories rollback partial scan rejection: ok")


def test_openmemories_rollback_ports_rejects_extra_or_duplicate_ports() -> None:
    def closed_connector(address: tuple[str, int], timeout: float) -> FakeConnection:
        raise OSError("connection refused")

    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        extra, _ = prove_openmemories_rollback_ports.capture_ports(
            host="camera.local",
            ports=(23, 5555, 8080, 9000),
            out_dir=root / "extra",
            network_context_proven=True,
            connector=closed_connector,
        )
        duplicate, _ = prove_openmemories_rollback_ports.capture_ports(
            host="camera.local",
            ports=(23, 5555, 8080, 8080),
            out_dir=root / "duplicate",
            network_context_proven=True,
            connector=closed_connector,
        )

    assert extra["complete_required_scan"] is False
    assert extra["unexpected_ports"] == [9000]
    assert extra["rollback_closed_evidence"] is False
    assert duplicate["complete_required_scan"] is False
    assert duplicate["rollback_closed_evidence"] is False
    print("  OpenMemories rollback exact-port-set enforcement: ok")


if __name__ == "__main__":
    test_openmemories_rollback_ports_records_closed_evidence_for_review()
    test_openmemories_rollback_ports_requires_network_context_for_closed_evidence()
    test_openmemories_rollback_ports_records_open_port_as_incomplete()
    test_openmemories_rollback_ports_rejects_partial_closed_scan()
    test_openmemories_rollback_ports_rejects_extra_or_duplicate_ports()
    print("openmemories_rollback_ports tests: PASS")
