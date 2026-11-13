"""Camera-free tests for the generic OpenMemories parameter write proof runner."""

from __future__ import annotations

import json
import sys
import tempfile
import urllib.error
from pathlib import Path
from urllib.parse import parse_qs, urlparse
from unittest.mock import patch

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "tools"))

import prove_openmemories_param_set_read_revert


class _FakeResponse:
    def __init__(self, payload: dict, status: int = 200) -> None:
        self.payload = payload
        self.status = status
        self.headers = {"Content-Type": "application/json"}

    def read(self) -> bytes:
        return json.dumps(self.payload).encode("utf-8")

    def getcode(self) -> int:
        return self.status

    def __enter__(self) -> _FakeResponse:
        return self

    def __exit__(self, *_args: object) -> None:
        return None


def _route3_fake(
    *,
    baseline_payload: dict | None = None,
    dry_run_overrides: dict | None = None,
    omit_revert_token: bool = False,
    commit_mode: str = "ok",
    guard_reuse_mode: str = "expected",
    revert_reuse_mode: str = "expected",
):
    state: dict[str, object] = {
        "param.iso_sensitivity": "800",
        "guard_used": False,
        "revert_used": False,
        "get_count": 0,
    }
    calls: list[tuple[str, str]] = []

    def rejection(kind: str, mode: str) -> _FakeResponse:
        if mode == "transport":
            raise urllib.error.URLError(f"{kind} transport lost")
        if kind == "guard":
            error = (
                "invalid_write_guard"
                if mode in {"expected", "wrong_status"}
                else "unrelated_failure"
            )
            phase = "commit"
        else:
            error = (
                "invalid_revert_guard"
                if mode in {"expected", "wrong_status"}
                else "unrelated_failure"
            )
            phase = "revert"
        return _FakeResponse(
            {
                "ok": False,
                "id": "param.iso_sensitivity",
                "phase": phase,
                "error": error,
                "writeGuardRequired": True,
            },
            status=500 if mode == "wrong_status" else 200,
        )

    def fake_urlopen(request, timeout=5.0):
        del timeout
        method = request.get_method()
        parsed = urlparse(request.full_url)
        endpoint = parsed.path
        calls.append((method, request.full_url))
        if endpoint not in {
            "/settings/param.iso_sensitivity",
            "/settings/param.iso_sensitivity/revert",
        }:
            return _FakeResponse({"ok": False, "error": "unexpected_endpoint"}, status=404)

        query = parse_qs(parsed.query)
        value = query.get("value", [None])[0]
        dry_run = query.get("dry_run", ["false"])[0] == "true"
        guard_token = query.get("guard_token", [None])[0]
        revert_token = query.get("revert_token", [None])[0]
        before = str(state["param.iso_sensitivity"])

        if method == "GET":
            state["get_count"] = int(state["get_count"]) + 1
            if state["get_count"] == 1 and baseline_payload is not None:
                return _FakeResponse(baseline_payload)
            return _FakeResponse(
                {
                    "ok": True,
                    "id": "param.iso_sensitivity",
                    "registrySettingId": "camera.iso",
                    "value": before,
                    "supported": ["640", "800"],
                    "readOnly": False,
                }
            )

        if method == "POST" and value and dry_run:
            payload = {
                "ok": True,
                "id": "param.iso_sensitivity",
                "registrySettingId": "camera.iso",
                "before": before,
                "requested": value,
                "after": before,
                "dry_run": True,
                "wouldWrite": True,
                "writeGuardRequired": True,
                "guardToken": "write-token-1",
                "recoveryToken": "revert-token-1",
                "supported": ["640", "800"],
            }
            payload.update(dry_run_overrides or {})
            return _FakeResponse(payload)

        if method == "POST" and value:
            if guard_token != "write-token-1" or state["guard_used"]:
                return rejection("guard", guard_reuse_mode)
            state["guard_used"] = True
            state["param.iso_sensitivity"] = value
            if commit_mode == "transport_after_apply":
                raise urllib.error.URLError("commit response lost after apply")
            payload = {
                "ok": True,
                "id": "param.iso_sensitivity",
                "registrySettingId": "camera.iso",
                "before": before,
                "requested": value,
                "after": value,
                "dry_run": False,
                "supported": ["640", "800"],
            }
            if not omit_revert_token:
                payload["revertToken"] = "revert-token-1"
            return _FakeResponse(payload)

        if method == "POST" and endpoint.endswith("/revert"):
            if revert_token != "revert-token-1" or state["revert_used"]:
                return rejection("revert", revert_reuse_mode)
            state["revert_used"] = True
            state["param.iso_sensitivity"] = "800"
            return _FakeResponse(
                {
                    "ok": True,
                    "id": "param.iso_sensitivity",
                    "registrySettingId": "camera.iso",
                    "before": "640",
                    "requested": "800",
                    "after": "800",
                }
            )
        return _FakeResponse({"ok": False, "error": "unexpected_method"}, status=400)

    return fake_urlopen, state, calls


def _run_with_fake(fake_urlopen, *, execute: bool = True):
    temporary = tempfile.TemporaryDirectory()
    summary, summary_path = prove_openmemories_param_set_read_revert.run_proof(
        base="http://camera.local:8080",
        setting_id="param.iso_sensitivity",
        value="640",
        execute=execute,
        out_dir=Path(temporary.name),
        recovery_dir=Path(temporary.name) / "private-recovery",
        opener=fake_urlopen,
    )
    return temporary, summary, summary_path


def test_openmemories_param_runner_dry_runs_sets_reads_and_reverts() -> None:
    fake_urlopen, _state, calls = _route3_fake()
    temporary, summary, summary_path = _run_with_fake(fake_urlopen)
    try:
        artifact_dir = summary_path.parent
        for filename in (
            "before.json",
            "dry_run_set_640.json",
            "set_640.json",
            "after_set.json",
            "reused_guard_token.json",
            "revert_800.json",
            "after_revert.json",
            "reused_revert_token.json",
        ):
            assert (artifact_dir / filename).exists(), filename
        artifact_text = "\n".join(
            path.read_text(encoding="utf-8")
            for path in sorted(artifact_dir.glob("*.json"))
        )
    finally:
        temporary.cleanup()

    assert summary["artifact"] == "openmemories_param_set_read_revert"
    assert summary["setting_id"] == "param.iso_sensitivity"
    assert summary["registry_setting_id"] == "camera.iso"
    assert summary["before"] == "800"
    assert summary["baseline_ok"] is True
    assert summary["requested"] == "640"
    assert summary["after_set"] == "640"
    assert summary["after_revert"] == "800"
    assert summary["dry_run_ok"] is True
    assert summary["guarded_protocol"] is True
    assert summary["guarded_dry_run_ok"] is True
    assert summary["recovery_token_received"] is True
    assert summary["recovery_token_consistent"] is True
    assert summary["recovery_cleanup_ok"] is True
    assert summary["emergency_recovery_record_created"] is True
    assert summary["emergency_recovery_record_cleared"] is True
    assert summary["emergency_recovery_record_retained"] is False
    assert summary["guard_reuse_rejected"] is True
    assert summary["revert_reuse_rejected"] is True
    assert summary["executed"] is True
    assert summary["execution_blocked"] is False
    assert summary["ok"] is True
    assert [method for method, _ in calls] == [
        "GET",
        "POST",
        "POST",
        "GET",
        "POST",
        "POST",
        "GET",
        "POST",
    ]
    assert "write-token-1" not in artifact_text
    assert "revert-token-1" not in artifact_text
    assert prove_openmemories_param_set_read_revert.REDACTED in artifact_text
    print("  guarded set/read/revert and artifact redaction: ok")


def test_executable_proof_aborts_without_valid_baseline() -> None:
    cases = (
        (
            {"ok": True, "id": "param.iso_sensitivity"},
            "baseline_value_missing",
        ),
        (
            {"ok": False, "id": "param.iso_sensitivity", "error": "camera_not_open"},
            "baseline_read_failed",
        ),
    )
    for baseline_payload, expected_reason in cases:
        fake_urlopen, state, calls = _route3_fake(baseline_payload=baseline_payload)
        temporary, summary, summary_path = _run_with_fake(fake_urlopen)
        try:
            persisted = summary_path.read_text(encoding="utf-8")
        finally:
            temporary.cleanup()

        assert len(calls) == 1
        assert calls[0][0] == "GET"
        assert state["param.iso_sensitivity"] == "800"
        assert summary["baseline_ok"] is False
        assert summary["revert_value"] is None
        assert summary["execution_blocked"] is True
        assert summary["failure_reason"] == expected_reason
        assert summary["ok"] is False
        assert '"None"' not in persisted
    print("  invalid baselines block all POST requests and never stringify None: ok")


def test_executable_proof_aborts_without_valid_guarded_dry_run() -> None:
    fake_urlopen, state, calls = _route3_fake(
        dry_run_overrides={"writeGuardRequired": False}
    )
    temporary, summary, summary_path = _run_with_fake(fake_urlopen)
    try:
        artifact_names = {path.name for path in summary_path.parent.glob("*.json")}
    finally:
        temporary.cleanup()

    assert [method for method, _ in calls] == ["GET", "POST", "POST", "GET"]
    assert state["param.iso_sensitivity"] == "800"
    assert summary["baseline_ok"] is True
    assert summary["dry_run_ok"] is True
    assert summary["guarded_protocol"] is True
    assert summary["guarded_dry_run_ok"] is False
    assert summary["execution_blocked"] is True
    assert summary["failure_reason"] == "guarded_dry_run_invalid"
    assert "set_640.json" not in artifact_names
    print("  malformed guarded dry-run blocks commit: ok")


def test_missing_commit_revert_token_uses_prewrite_recovery_token() -> None:
    fake_urlopen, state, calls = _route3_fake(omit_revert_token=True)
    temporary, summary, summary_path = _run_with_fake(fake_urlopen)
    try:
        artifact_names = {path.name for path in summary_path.parent.glob("*.json")}
    finally:
        temporary.cleanup()

    assert [method for method, _ in calls] == [
        "GET",
        "POST",
        "POST",
        "GET",
        "POST",
        "POST",
        "GET",
        "POST",
    ]
    assert not any("value=800" in url for _method, url in calls)
    assert state["param.iso_sensitivity"] == "800"
    assert summary["recovery_token_received"] is True
    assert summary["revert_token_received"] is True
    assert summary["recovery_cleanup_ok"] is True
    assert summary["emergency_recovery_record_retained"] is False
    assert summary["failure_reason"] is None
    assert summary["ok"] is True
    assert "revert_800.json" in artifact_names
    print("  missing commit response token falls back to pre-write recovery token: ok")


def test_commit_transport_loss_after_apply_still_reverts() -> None:
    fake_urlopen, state, calls = _route3_fake(
        commit_mode="transport_after_apply"
    )
    temporary, summary, summary_path = _run_with_fake(fake_urlopen)
    try:
        artifact_text = "\n".join(
            path.read_text(encoding="utf-8")
            for path in sorted(summary_path.parent.glob("*.json"))
        )
        recovery_file = (
            Path(temporary.name)
            / "private-recovery"
            / "route3-param.iso_sensitivity.json"
        )
        assert not recovery_file.exists()
    finally:
        temporary.cleanup()

    assert state["param.iso_sensitivity"] == "800"
    assert [method for method, _url in calls] == [
        "GET",
        "POST",
        "POST",
        "GET",
        "POST",
        "GET",
        "POST",
    ]
    assert summary["failure_reason"] == "set_failed"
    assert summary["recovery_cleanup_ok"] is True
    assert summary["emergency_recovery_record_created"] is True
    assert summary["emergency_recovery_record_cleared"] is True
    assert summary["emergency_recovery_record_retained"] is False
    assert summary["ok"] is False
    assert "write-token-1" not in artifact_text
    assert "revert-token-1" not in artifact_text
    print("  lost commit response after apply still restores with pre-write token: ok")


def test_transport_failure_is_not_guard_reuse_proof() -> None:
    fake_urlopen, state, _calls = _route3_fake(guard_reuse_mode="transport")
    temporary, summary, _summary_path = _run_with_fake(fake_urlopen)
    temporary.cleanup()

    assert state["param.iso_sensitivity"] == "800"
    assert summary["guard_reuse_rejected"] is False
    assert summary["revert_reuse_rejected"] is True
    assert summary["failure_reason"] == "guard_token_reuse_not_rejected"
    assert summary["ok"] is False
    print("  guard-token transport failure is not rejection proof: ok")


def test_transport_failure_is_not_revert_reuse_proof() -> None:
    fake_urlopen, state, _calls = _route3_fake(revert_reuse_mode="transport")
    temporary, summary, _summary_path = _run_with_fake(fake_urlopen)
    temporary.cleanup()

    assert state["param.iso_sensitivity"] == "800"
    assert summary["guard_reuse_rejected"] is True
    assert summary["revert_reuse_rejected"] is False
    assert summary["failure_reason"] == "revert_token_reuse_not_rejected"
    assert summary["ok"] is False
    print("  revert-token transport failure is not rejection proof: ok")


def test_wrong_application_error_is_not_token_reuse_proof() -> None:
    fake_urlopen, state, _calls = _route3_fake(guard_reuse_mode="wrong")
    temporary, summary, _summary_path = _run_with_fake(fake_urlopen)
    temporary.cleanup()

    assert state["param.iso_sensitivity"] == "800"
    assert summary["guard_reuse_rejected"] is False
    assert summary["failure_reason"] == "guard_token_reuse_not_rejected"
    assert summary["ok"] is False
    print("  unrelated application error is not rejection proof: ok")


def test_wrong_http_status_is_not_token_reuse_proof() -> None:
    fake_urlopen, state, _calls = _route3_fake(guard_reuse_mode="wrong_status")
    temporary, summary, _summary_path = _run_with_fake(fake_urlopen)
    temporary.cleanup()

    assert state["param.iso_sensitivity"] == "800"
    assert summary["guard_reuse_rejected"] is False
    assert summary["failure_reason"] == "guard_token_reuse_not_rejected"
    assert summary["ok"] is False
    print("  unexpected HTTP status is not rejection proof: ok")


def test_cli_rejects_legacy_unguarded_dry_run() -> None:
    summary = {
        "ok": False,
        "baseline_ok": True,
        "dry_run_ok": True,
        "guarded_protocol": False,
        "guarded_dry_run_ok": False,
    }
    with patch.object(
        prove_openmemories_param_set_read_revert,
        "parse_args",
        return_value=type(
            "Args",
            (),
            {
                "base": "http://camera.local:8080",
                "setting_id": "param.iso_sensitivity",
                "value": "640",
                "revert_value": None,
                "execute": False,
                "out_dir": Path("/tmp"),
                "recovery_dir": Path("/tmp/private-recovery"),
                "timeout": 5.0,
            },
        )(),
    ), patch.object(
        prove_openmemories_param_set_read_revert,
        "run_proof",
        return_value=(summary, Path("/tmp/legacy-summary.json")),
    ):
        assert prove_openmemories_param_set_read_revert.main([]) == 2
    print("  legacy unguarded Route3 dry-run is not credited: ok")


if __name__ == "__main__":
    test_openmemories_param_runner_dry_runs_sets_reads_and_reverts()
    test_executable_proof_aborts_without_valid_baseline()
    test_executable_proof_aborts_without_valid_guarded_dry_run()
    test_missing_commit_revert_token_uses_prewrite_recovery_token()
    test_commit_transport_loss_after_apply_still_reverts()
    test_transport_failure_is_not_guard_reuse_proof()
    test_transport_failure_is_not_revert_reuse_proof()
    test_wrong_application_error_is_not_token_reuse_proof()
    test_wrong_http_status_is_not_token_reuse_proof()
    test_cli_rejects_legacy_unguarded_dry_run()
    print("openmemories_param_set_read_revert tests: PASS")
