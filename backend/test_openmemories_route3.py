"""Camera-free checks for the OpenMemories Route 3 proof-app scaffold."""

from __future__ import annotations

import os
import subprocess
import sys
import tempfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "tools"))

import falsify_openmemories_route3


def _legacy_generated_proof_app_exposes_required_plan_endpoints() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        result = falsify_openmemories_route3.generate_proof_app(Path(tmp))
        app_root = Path(result["path"])
        java = (
            app_root
            / "app"
            / "src"
            / "main"
            / "java"
            / "com"
            / "cameraautopilot"
            / "route3proof"
            / "Route3ProofActivity.java"
        ).read_text(encoding="utf-8")
        readme = (app_root / "README.md").read_text(encoding="utf-8")
        app_gradle = (app_root / "app" / "build.gradle").read_text(encoding="utf-8")
        revert_store = (
            app_root
            / "app"
            / "src"
            / "main"
            / "java"
            / "com"
            / "cameraautopilot"
            / "route3proof"
            / "RetryableRevertPlanStore.java"
        ).read_text(encoding="utf-8")

    for route in (
        '"/health"',
        '"/capabilities"',
        '"/diagnostics"',
        '"/settings"',
        '"/settings/"',
        '"/preview"',
        '"/focus"',
        '"/capture"',
        '"/exit"',
        '"/shutdown"',
        '"/rollback"',
        '"/diagnostics/rollback"',
        '"/parameters"',
    ):
        assert route in java, route
    assert "versionName \"0.2.2-vnext\"" in app_gradle
    assert "versionCode 4" in app_gradle
    assert "GET /capabilities" in readme
    assert "GET /diagnostics" in readme
    assert "GET /settings/{id}" in readme
    assert "GET /settings/param.dump" in readme
    assert "GET /parameters" in readme
    assert "POST /preview" in readme
    assert "POST /focus" in readme
    assert "POST /capture" in readme
    assert "POST /exit" in readme
    assert "POST /shutdown" in readme
    assert "GET /rollback" in readme
    assert "GET /diagnostics/rollback" in readme
    assert "guardToken" in readme
    assert "guard_token=<token>" in readme
    assert "revertToken" in readme
    assert "recoveryToken" in readme
    assert "revert_token=<token>" in readme
    assert "Back button" in readme
    assert "Menu button" in readme
    assert "No formatting, deletion, reset" in readme
    assert "GET /capabilities" in result["endpoints"]
    assert "GET /diagnostics" in result["endpoints"]
    assert "GET /settings/{id}" in result["endpoints"]
    assert "GET /settings/param.dump" in result["endpoints"]
    assert "GET /parameters" in result["endpoints"]
    assert "POST /exit" in result["endpoints"]
    assert "POST /shutdown" in result["endpoints"]
    assert "GET /rollback" in result["endpoints"]
    assert "GET /diagnostics/rollback" in result["endpoints"]
    assert "POST /settings/{id}?value=<value>&dry_run=true" in result["endpoints"]
    assert (
        "POST /settings/{id}?value=<value>&guard_token=<token>" in result["endpoints"]
    )
    assert (
        "POST /settings/{id}/revert?revert_token=<token>" in result["endpoints"]
    )
    assert 'methodNotAllowed("POST")' in java
    assert '"/capture".equals(uri)' in java
    assert '"/exit".equals(uri)' in java
    assert '"/shutdown".equals(uri)' in java
    assert '"/rollback".equals(uri)' in java
    assert "Exit Route3 Proof" in java
    assert "onKeyDown" in java
    assert "KeyEvent.KEYCODE_BACK" in java
    assert "KeyEvent.KEYCODE_MENU" in java
    assert "onDestroy" in java
    assert "shutdownResources" in java
    assert "import android.content.SharedPreferences;" in java
    assert "RECOVERY_TTL_MS = 604800000L" in java
    assert "persistRevertPlan(plan);" in java
    assert "restorePersistedRevertPlan();" in java
    assert "clearPersistedRevertPlan(plan.token);" in java
    assert "pending_recovery_requires_revert" in java
    assert java.count("String recoveryToken = issueRevertPlan(") >= 2
    assert java.index("String recoveryToken = issueRevertPlan(") < java.index(
        "camera.setParameters(params);"
    )
    shutdown_body = java[
        java.index("private void shutdownResources(") : java.index(
            "private void releaseCameraNow("
        )
    ]
    assert "revertPlans.clear();" not in shutdown_body
    assert "startServerIfNeeded" in java
    assert "stopServerIfNeeded" in java
    assert "serverStartCount" in java
    assert "serverStopCount" in java
    assert "cameraOpenCount" in java
    assert "previewStartCount" in java
    assert 'log(label + " already running")' in java
    assert "networkJson" in java
    assert "developerPortsJson" in java
    assert "camera_local_socket_probe" in java
    assert "externalFinalScanRequired" in java
    assert "writeNotImplementedJson" in java
    assert "setting_write_not_implemented" in java
    assert "setAllowlistedParamJson" in java
    assert "allowlistedParamKey" in java
    assert "unsupported_camera_parameter_value" in java
    assert "write_guard_required" in java
    assert "stale_or_mismatched_write_guard" in java
    assert "stale_revert_target" in java
    assert "beginRevertPlan" in java
    assert "releaseRevertPlan" in java
    assert "completeRevertPlan" in java
    assert "private RevertPlan consumeRevertPlan" not in java
    assert java.count("releaseRevertPlan(plan);") >= 7
    assert java.count("completeRevertPlan(plan)") >= 4
    assert "revertRetryable" in java
    assert "revertGuardConsumed" in java
    assert "stored.inFlight = true" in revert_store
    assert "stored.inFlight = false" in revert_store
    assert "plans.remove(token)" in revert_store
    assert "revert_guard_in_flight" in revert_store
    assert "Camera-open, apply, support" in readme
    assert "consumed only after the original value is read back" in readme
    assert "survives activity pause" in readme
    assert "durable recovery plan is intentionally preserved" in readme
    assert "guard_token" in java
    assert "revert_token" in java
    assert "destructive_operation_forbidden" in java
    assert "destructiveOperationsEnabled" in java
    assert "Route3 vNext contains no implementation for destructive camera operations" in java
    assert "writableSettingJson" in java
    assert "commitRequires" in java
    assert "dryRunEndpoint" in java
    assert "revertEndpoint" in java
    assert 'writableSettingJson("param.iso_sensitivity"' in java
    assert 'writableSettingJson("param.image_aspect"' in java
    assert 'writableSettingJson("param.raw_rec_mode"' in java
    assert "param.iso_sensitivity" in java
    assert "param.image_aspect" in java
    assert "param.raw_rec_mode" in java
    assert "Response.Status.NOT_IMPLEMENTED" not in java
    assert "Response.Status.BAD_REQUEST" in java
    assert "parametersDumpJson" in java
    assert "parameterValueJson" in java
    assert "postDelayed" in java
    assert "dry_run" in java
    assert "captureCountBefore" in java
    assert '"param.dump"' in java
    assert '"capture.count"' in java
    assert '"capture.bytes"' in java
    assert '"ok\\":" + (camera != null)' in java
    assert "GET /media/latest" in readme
    assert "/settings/param.iso_sensitivity?value=800&dry_run=true" in readme
    assert "/settings/param.image_aspect?value=16:9&dry_run=true" in readme
    assert "/settings/param.raw_rec_mode?value=compress&dry_run=true" in readme
    print("  OpenMemories proof app endpoint scaffold: ok")


def _legacy_generated_revert_plan_store_lifecycle() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        result = falsify_openmemories_route3.generate_proof_app(Path(tmp))
        app_root = Path(result["path"])
        source = (
            app_root
            / "app"
            / "src"
            / "main"
            / "java"
            / "com"
            / "cameraautopilot"
            / "route3proof"
            / "RetryableRevertPlanStore.java"
        )
        harness = app_root / "tests" / "RetryableRevertPlanStoreTest.java"
        harness_text = harness.read_text(encoding="utf-8")
        for failure_name in (
            "camera-open-failure",
            "apply-failure",
            "readback-verification-failure",
        ):
            assert failure_name in harness_text
        classes = app_root / "build" / "host-revert-plan-test"
        classes.mkdir(parents=True)
        java_probe = falsify_openmemories_route3.java_probe()
        java_home = falsify_openmemories_route3._selected_java_home(java_probe)
        assert java_home is not None, "Java 8 toolchain is required for the host lifecycle test"
        java_bin = Path(java_home) / "bin" / "java"
        javac_bin = Path(java_home) / "bin" / "javac"
        assert java_bin.is_file(), java_bin
        assert javac_bin.is_file(), javac_bin

        compile_result = subprocess.run(
            [str(javac_bin), "-d", str(classes), str(source), str(harness)],
            capture_output=True,
            text=True,
            check=False,
        )
        assert compile_result.returncode == 0, (
            compile_result.stdout + compile_result.stderr
        )

        run_result = subprocess.run(
            [
                str(java_bin),
                "-cp",
                str(classes),
                "com.cameraautopilot.route3proof.RetryableRevertPlanStoreTest",
            ],
            capture_output=True,
            text=True,
            check=False,
        )
        assert run_result.returncode == 0, run_result.stdout + run_result.stderr
        assert "RetryableRevertPlanStoreTest: PASS" in run_result.stdout

    print("  OpenMemories retryable revert-plan lifecycle: ok")


def test_disabled_openmemories_generator_fails_closed() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        try:
            falsify_openmemories_route3.generate_proof_app(Path(tmp))
        except RuntimeError as exc:
            assert "disabled" in str(exc).lower()
        else:
            raise AssertionError("unsafe OpenMemories proof-app generation was enabled")
    assert falsify_openmemories_route3.main(["--generate-app"]) == 2
    print("  OpenMemories proof app remains permanently disabled: ok")


def test_route3_cli_exits_before_any_toolchain_or_output_side_effect() -> None:
    traps = {
        "android_sdk_probe": falsify_openmemories_route3.android_sdk_probe,
        "java_probe": falsify_openmemories_route3.java_probe,
        "generate_proof_app": falsify_openmemories_route3.generate_proof_app,
    }

    def forbidden(*_args, **_kwargs):
        raise AssertionError("Route 3 touched a toolchain or output before its disabled exit")

    try:
        for name in traps:
            setattr(falsify_openmemories_route3, name, forbidden)
        assert falsify_openmemories_route3.main(["--generate-app", "--out-dir", "/must-not-be-read"]) == 2
    finally:
        for name, value in traps.items():
            setattr(falsify_openmemories_route3, name, value)
    print("  Route 3 CLI exits before all side effects: ok")


def test_preflight_includes_private_android_toolchain_candidates() -> None:
    sdk = falsify_openmemories_route3.android_sdk_probe()
    sdk_paths = {item["path"] for item in sdk["candidates"]}
    assert "/tmp/a7ii-android-toolchain/android-sdk" in sdk_paths

    with tempfile.TemporaryDirectory() as tmp:
        java_home = Path(tmp)
        java_bin = java_home / "bin" / "java"
        java_bin.parent.mkdir()
        java_bin.write_text("#!/bin/sh\necho 'openjdk version \"1.8.0_402\"' >&2\n", encoding="utf-8")
        java_bin.chmod(0o755)
        old_java_home = os.environ.get("JAVA_HOME")
        os.environ["JAVA_HOME"] = str(java_home)
        try:
            java = falsify_openmemories_route3.java_probe()
        finally:
            if old_java_home is None:
                os.environ.pop("JAVA_HOME", None)
            else:
                os.environ["JAVA_HOME"] = old_java_home

    java_paths = {item["path"] for item in java["candidates"]}
    assert "/tmp/a7ii-android-toolchain/java8/amazon-corretto-8.jdk/Contents/Home/bin/java" in java_paths
    # The cached-toolchain candidate is $HOME-derived: /Users/<you>/... on this
    # Mac, /home/runner/... on the ubuntu CI runner. Derive the expected path from the
    # same constant the probe uses so the assertion is host-independent.
    cached_toolchain = falsify_openmemories_route3.CACHED_ANDROID_TOOLCHAIN
    cached_java = (
        cached_toolchain / "java8" / "amazon-corretto-8.jdk" / "Contents" / "Home" / "bin" / "java"
    )
    assert str(cached_java) in java_paths
    assert java["has_java8_like"] is True

    gradle = falsify_openmemories_route3.gradle_probe(java_home=str(java_home))
    gradle_paths = {item["path"] for item in gradle["candidates"]}
    assert str(cached_toolchain / "gradle-4.1" / "bin" / "gradle") in gradle_paths

    pmca_candidates = falsify_openmemories_route3.pmca_candidates()
    pmca_console_paths = {str(item["console"]) for item in pmca_candidates}
    # TOOLS_DIR is env-overridable and defaults to a Mac path; derive from the module
    # constant rather than hard-coding it so a different host/override still matches.
    tools_dir = falsify_openmemories_route3.TOOLS_DIR
    assert str(tools_dir / "Sony-PMCA-RE" / "pmca-console.py") in pmca_console_paths
    assert "/tmp/pmca-spike/pmca-console.py" in pmca_console_paths
    print("  OpenMemories preflight private Android toolchain candidates: ok")


def test_preflight_cached_toolchain_candidate_tracks_a_linux_runner_home() -> None:
    # Regression for the CI runner: the $HOME-derived cache candidates must follow
    # $HOME so a job on ubuntu-latest (/home/runner) emits the same-shaped candidate the
    # Mac does. Simulate the runner by reloading the module with a Linux HOME.
    import importlib

    old_home = os.environ.get("HOME")
    old_java_home = os.environ.get("JAVA_HOME")
    os.environ["HOME"] = "/home/runner"
    os.environ.pop("JAVA_HOME", None)
    try:
        reloaded = importlib.reload(falsify_openmemories_route3)
        assert reloaded.CACHED_ANDROID_TOOLCHAIN == Path(
            "/home/runner/Library/Caches/camera-autopilot/toolchains"
        )
        java_paths = {item["path"] for item in reloaded.java_probe()["candidates"]}
        assert (
            "/home/runner/Library/Caches/camera-autopilot/toolchains/java8/"
            "amazon-corretto-8.jdk/Contents/Home/bin/java"
        ) in java_paths
        gradle_paths = {
            item["path"] for item in reloaded.gradle_probe()["candidates"]
        }
        assert (
            "/home/runner/Library/Caches/camera-autopilot/toolchains/gradle-4.1/bin/gradle"
            in gradle_paths
        )
    finally:
        if old_home is None:
            os.environ.pop("HOME", None)
        else:
            os.environ["HOME"] = old_home
        if old_java_home is not None:
            os.environ["JAVA_HOME"] = old_java_home
        importlib.reload(falsify_openmemories_route3)
    print("  OpenMemories preflight cache candidates track a Linux runner home: ok")


if __name__ == "__main__":
    test_disabled_openmemories_generator_fails_closed()
    test_route3_cli_exits_before_any_toolchain_or_output_side_effect()
    test_preflight_includes_private_android_toolchain_candidates()
    test_preflight_cached_toolchain_candidate_tracks_a_linux_runner_home()
    print("openmemories_route3 tests: PASS")
