#!/usr/bin/env python3
"""Preflight and scaffold the Route 3 OpenMemories on-camera proof app.

This is a falsification harness, not a production app builder. It records whether the local Mac has the
tooling and camera reachability needed to prove a clean-room on-camera HTTP API, and can generate a tiny
proof app skeleton into the artifact directory.

The generated app is intentionally ours: own package, own endpoints, public OpenMemories/PMCADemo-style
dependencies, no Sony APK redistribution, and no Smart Remote patching.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import socket
import subprocess
import sys
import textwrap
import time
from pathlib import Path

_CONFIGURED_LAN_HOST = os.environ.get("CAMERA_LAN_HOST", "").strip()
DEFAULT_HOSTS = tuple(
    dict.fromkeys(
        host for host in (_CONFIGURED_LAN_HOST, "192.168.122.1") if host
    )
)
DEFAULT_PORTS = (23, 5555, 8080)
TOOLS_DIR = Path(os.environ.get("TOOLS_DIR", str(Path.home() / "tools")))
CACHED_ANDROID_TOOLCHAIN = (
    Path.home() / "Library" / "Caches" / "camera-autopilot" / "toolchains"
)


def first_existing_path(*paths: Path) -> Path:
    for path in paths:
        if path.exists():
            return path
    return paths[0]

REFERENCE_PATHS = {
    "OpenMemories-Framework": Path("/tmp/OpenMemories-Framework"),
    "PMCADemo": Path("/tmp/PMCADemo"),
    "OpenMemories-Tweak": first_existing_path(
        TOOLS_DIR / "OpenMemories-Tweak",
        Path("/tmp/OpenMemories-Tweak"),
    ),
    "alpha-fairy": Path("/tmp/alpha-fairy"),
    "pmca-console.py": first_existing_path(
        TOOLS_DIR / "Sony-PMCA-RE" / "pmca-console.py",
        Path("/tmp/pmca-spike/pmca-console.py"),
    ),
}

PRIVATE_ANDROID_TOOLCHAIN = Path("/tmp/a7ii-android-toolchain")


def run(
    cmd: list[str],
    timeout: float = 8.0,
    cwd: Path | None = None,
    env: dict[str, str] | None = None,
) -> dict:
    started = time.time()
    try:
        proc = subprocess.run(
            cmd,
            cwd=str(cwd) if cwd else None,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            timeout=timeout,
            check=False,
            env=env,
        )
        return {
            "cmd": cmd,
            "ok": proc.returncode == 0,
            "returncode": proc.returncode,
            "duration_s": round(time.time() - started, 3),
            "output": proc.stdout[-4000:],
        }
    except FileNotFoundError as exc:
        return {
            "cmd": cmd,
            "ok": False,
            "returncode": None,
            "duration_s": round(time.time() - started, 3),
            "error_type": exc.__class__.__name__,
            "output": str(exc),
        }
    except subprocess.TimeoutExpired as exc:
        return {
            "cmd": cmd,
            "ok": False,
            "returncode": None,
            "duration_s": round(time.time() - started, 3),
            "error_type": exc.__class__.__name__,
            "output": (exc.stdout or "")[-4000:] if isinstance(exc.stdout, str) else str(exc),
        }


def command_probe(command: str, args: list[str], timeout: float = 8.0) -> dict:
    path = shutil.which(command)
    if not path:
        return {"command": command, "status": "MISSING", "path": None, "probe": None}
    probe = run([path, *args], timeout=timeout)
    return {
        "command": command,
        "status": "PRESENT" if probe["ok"] else "ERROR",
        "path": path,
        "probe": probe,
    }


def _candidate_java8_paths() -> list[Path]:
    return [
        PRIVATE_ANDROID_TOOLCHAIN
        / "java8"
        / "amazon-corretto-8.jdk"
        / "Contents"
        / "Home"
        / "bin"
        / "java",
        CACHED_ANDROID_TOOLCHAIN
        / "java8"
        / "amazon-corretto-8.jdk"
        / "Contents"
        / "Home"
        / "bin"
        / "java",
    ]


def _java_home_from_bin(path: Path) -> str:
    return str(path.parent.parent)


def _selected_java_home(java: dict) -> str | None:
    for item in java.get("candidates", []):
        if item.get("exists") and item.get("java8_like"):
            return str(item.get("home") or _java_home_from_bin(Path(item["path"])))
    return None


def _gradle_candidate_paths() -> list[Path]:
    paths: list[Path] = []
    gradle_bin = os.environ.get("GRADLE_BIN")
    if gradle_bin:
        paths.append(Path(gradle_bin).expanduser())
    paths.extend(
        [
            CACHED_ANDROID_TOOLCHAIN / "gradle-4.1" / "bin" / "gradle",
            CACHED_ANDROID_TOOLCHAIN / "gradle-3.3" / "bin" / "gradle",
            CACHED_ANDROID_TOOLCHAIN / "gradle-2.14.1" / "bin" / "gradle",
            PRIVATE_ANDROID_TOOLCHAIN / "gradle-4.1" / "bin" / "gradle",
            PRIVATE_ANDROID_TOOLCHAIN / "gradle-3.3" / "bin" / "gradle",
            PRIVATE_ANDROID_TOOLCHAIN / "gradle-2.14.1" / "bin" / "gradle",
        ]
    )
    system_gradle = shutil.which("gradle")
    if system_gradle:
        paths.append(Path(system_gradle))
    return paths


def gradle_probe(java_home: str | None = None) -> dict:
    env = dict(os.environ)
    if java_home:
        env["JAVA_HOME"] = java_home
    candidates = []
    selected = None
    seen = set()
    for path in _gradle_candidate_paths():
        if str(path) in seen:
            continue
        seen.add(str(path))
        if not path.exists():
            candidates.append({"path": str(path), "exists": False, "probe": None, "ok": False})
            continue
        probe = run([str(path), "--version"], timeout=8, env=env)
        ok = probe.get("ok") is True
        candidates.append({"path": str(path), "exists": True, "probe": probe, "ok": ok})
        if selected is None and ok:
            selected = candidates[-1]
    return {
        "command": "gradle",
        "status": "PRESENT" if selected else "ERROR" if any(item["exists"] for item in candidates) else "MISSING",
        "path": selected["path"] if selected else None,
        "probe": selected["probe"] if selected else None,
        "candidates": candidates,
    }


def pmca_candidates() -> list[dict]:
    return [
        {
            "python": TOOLS_DIR / "Sony-PMCA-RE" / ".venv" / "bin" / "python",
            "console": TOOLS_DIR / "Sony-PMCA-RE" / "pmca-console.py",
        },
        {
            "python": Path("/tmp/pmca-venv/bin/python"),
            "console": Path("/tmp/pmca-spike/pmca-console.py"),
        },
    ]


def pmca_probe() -> dict:
    candidates = pmca_candidates()
    for candidate in candidates:
        python_path = candidate["python"]
        console_path = candidate["console"]
        if python_path.is_file() and console_path.is_file():
            probe = run([str(python_path), str(console_path), "--help"], timeout=8)
            return {
                "command": "pmca-console.py",
                "status": "PRESENT" if probe.get("ok") else "ERROR",
                "path": str(console_path),
                "python": str(python_path),
                "probe": probe,
                "candidates": [
                    {"python": str(item["python"]), "console": str(item["console"])}
                    for item in candidates
                ],
            }
    return {
        "command": "pmca-console.py",
        "status": "MISSING",
        "path": "",
        "python": "",
        "probe": None,
        "candidates": [
            {"python": str(item["python"]), "console": str(item["console"])}
            for item in candidates
        ],
    }


def path_probe(name: str, path: Path) -> dict:
    item = {
        "name": name,
        "path": str(path),
        "exists": path.exists(),
        "kind": "directory" if path.is_dir() else "file" if path.is_file() else None,
        "git_head": None,
    }
    if (path / ".git").is_dir():
        item["git_head"] = run(["git", "log", "--oneline", "-1"], cwd=path, timeout=4)
    return item


def socket_probe(host: str, port: int, timeout: float) -> dict:
    started = time.time()
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.settimeout(timeout)
    try:
        sock.connect((host, port))
        ok = True
        error = None
    except OSError as exc:
        ok = False
        error = f"{exc.__class__.__name__}: {exc}"
    finally:
        sock.close()
    return {
        "host": host,
        "port": port,
        "open": ok,
        "duration_s": round(time.time() - started, 3),
        "error": error,
    }


def read_text(path: Path, limit: int = 12000) -> str:
    try:
        return path.read_text(encoding="utf-8", errors="replace")[:limit]
    except OSError:
        return ""


def extract_gradle_facts(project: Path) -> dict:
    root_gradle = read_text(project / "build.gradle")
    app_gradle = read_text(project / "app" / "build.gradle")
    facts = {
        "project": str(project),
        "root_build_gradle": bool(root_gradle),
        "app_build_gradle": bool(app_gradle),
        "android_gradle_plugin": None,
        "compile_sdk": None,
        "build_tools": None,
        "dependencies": [],
    }
    plugin = re.search(r"com\.android\.tools\.build:gradle:([^'\"\s]+)", root_gradle)
    compile_sdk = re.search(r"compileSdkVersion\s+([0-9]+)", app_gradle)
    build_tools = re.search(r"buildToolsVersion\s+['\"]([^'\"]+)['\"]", app_gradle)
    deps = re.findall(r"(?:compile|provided|implementation)\s+['\"]([^'\"]+)['\"]", app_gradle)
    if plugin:
        facts["android_gradle_plugin"] = plugin.group(1)
    if compile_sdk:
        facts["compile_sdk"] = compile_sdk.group(1)
    if build_tools:
        facts["build_tools"] = build_tools.group(1)
    facts["dependencies"] = deps
    return facts


def android_sdk_probe() -> dict:
    roots = []
    for env_name in ("ANDROID_HOME", "ANDROID_SDK_ROOT"):
        value = os.environ.get(env_name)
        if value:
            roots.append(Path(value).expanduser())
    roots.append(PRIVATE_ANDROID_TOOLCHAIN / "android-sdk")
    roots.append(Path.home() / "Library" / "Android" / "sdk")

    seen = set()
    candidates = []
    for root in roots:
        if str(root) in seen:
            continue
        seen.add(str(root))
        candidates.append(
            {
                "path": str(root),
                "exists": root.exists(),
                "platform_10": (root / "platforms" / "android-10").exists(),
                "build_tools_25_0_2": (root / "build-tools" / "25.0.2").exists(),
                "platforms": sorted(p.name for p in (root / "platforms").glob("*"))[:40]
                if (root / "platforms").is_dir()
                else [],
                "build_tools": sorted(p.name for p in (root / "build-tools").glob("*"))[:40]
                if (root / "build-tools").is_dir()
                else [],
            }
        )
    return {"candidates": candidates}


def _selected_android_sdk(android_sdk: dict) -> str | None:
    for item in android_sdk.get("candidates", []):
        if item.get("exists") and item.get("platform_10"):
            return str(item["path"])
    for item in android_sdk.get("candidates", []):
        if item.get("exists"):
            return str(item["path"])
    return None


def java_probe() -> dict:
    candidates = []
    paths = []
    java_home = os.environ.get("JAVA_HOME")
    if java_home:
        paths.append(Path(java_home).expanduser() / "bin" / "java")
    paths.extend(_candidate_java8_paths())
    paths.extend([
        Path("/usr/bin/java"),
        Path("/opt/homebrew/opt/openjdk/bin/java"),
    ])
    paths.extend(sorted(Path("/Library/Java/JavaVirtualMachines").glob("*/Contents/Home/bin/java")))
    seen = set()
    for path in paths:
        if str(path) in seen:
            continue
        seen.add(str(path))
        if not path.exists():
            candidates.append({
                "path": str(path),
                "home": _java_home_from_bin(path),
                "exists": False,
                "version": None,
                "java8_like": False,
            })
            continue
        probe = run([str(path), "-version"], timeout=8)
        output = probe.get("output", "")
        candidates.append(
            {
                "path": str(path),
                "home": _java_home_from_bin(path),
                "exists": True,
                "version": output.splitlines()[:3],
                "java8_like": '"1.8' in output or 'version "8' in output,
                "probe": probe,
            }
        )
    return {"candidates": candidates, "has_java8_like": any(item["java8_like"] for item in candidates)}


def build_environment(android_sdk: dict, java: dict) -> dict[str, str]:
    env = dict(os.environ)
    java_home = _selected_java_home(java)
    android_home = _selected_android_sdk(android_sdk)
    if java_home:
        env["JAVA_HOME"] = java_home
    if android_home:
        env["ANDROID_HOME"] = android_home
        env["ANDROID_SDK_ROOT"] = android_home
    return env


def write(path: Path, data: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(textwrap.dedent(data).strip() + "\n", encoding="utf-8")


def _disabled_legacy_generate_proof_app(root: Path) -> dict:
    """Retained only as historical source context; never invoke this unsafe generator."""
    app = root / "route3_openmemories_proof_app"
    package_dir = app / "app" / "src" / "main" / "java" / "com" / "cameraautopilot" / "route3proof"
    write(
        app / "settings.gradle",
        """
        include ':app'
        """,
    )
    write(
        app / "build.gradle",
        """
        buildscript {
            repositories {
                maven { url "https://jcenter.bintray.com/" }
                maven { url "https://maven.google.com" }
                mavenCentral()
            }
            dependencies {
                classpath 'com.android.tools.build:gradle:3.0.1'
            }
        }

        allprojects {
            repositories {
                maven { url "https://jcenter.bintray.com/" }
                maven { url "https://maven.google.com" }
                mavenCentral()
                maven { url "https://jitpack.io" }
            }
        }
        """,
    )
    write(
        app / "app" / "build.gradle",
        """
        apply plugin: 'com.android.application'

        android {
            compileSdkVersion 10
            buildToolsVersion "25.0.2"

            defaultConfig {
                applicationId "com.cameraautopilot.route3proof"
                minSdkVersion 10
                targetSdkVersion 10
                versionCode 4
                versionName "0.2.2-vnext"
            }

            compileOptions {
                sourceCompatibility JavaVersion.VERSION_1_6
                targetCompatibility JavaVersion.VERSION_1_6
            }
        }

        dependencies {
            // Tombstoned: this legacy generator must never resolve mutable dependencies.
            // A future separately approved workflow must use checked-in immutable hashes.
            compile 'com.nanohttpd:nanohttpd:2.1.1'
        }
        """,
    )
    write(
        app / "app" / "src" / "main" / "AndroidManifest.xml",
        """
        <manifest xmlns:android="http://schemas.android.com/apk/res/android"
            package="com.cameraautopilot.route3proof">

            <uses-permission android:name="android.permission.INTERNET" />
            <uses-permission android:name="android.permission.WRITE_EXTERNAL_STORAGE" />
            <uses-permission android:name="android.permission.CAMERA" />

            <application
                android:label="Route3 Proof"
                android:theme="@android:style/Theme.Black.NoTitleBar.Fullscreen">
                <activity
                    android:name=".Route3ProofActivity"
                    android:screenOrientation="landscape"
                    android:exported="true">
                    <intent-filter>
                        <action android:name="android.intent.action.MAIN" />
                        <category android:name="android.intent.category.LAUNCHER" />
                    </intent-filter>
                </activity>
            </application>
        </manifest>
        """,
    )
    write(
        package_dir / "RetryableRevertPlanStore.java",
        r'''
        package com.cameraautopilot.route3proof;

        import java.util.HashMap;
        import java.util.Map;

        /**
         * Stores one-time revert plans without destroying the rollback path on a
         * transient camera failure. A plan is claimed while a revert is in flight,
         * released after failure, and consumed only after verified restoration.
         */
        final class RetryableRevertPlanStore {
            static final class Claim {
                final Object value;
                final String error;

                Claim(Object value, String error) {
                    this.value = value;
                    this.error = error;
                }

                boolean ok() {
                    return value != null && error.length() == 0;
                }
            }

            private static final class StoredPlan {
                final String id;
                final Object value;
                final long expiresAt;
                boolean inFlight;

                StoredPlan(String id, Object value, long expiresAt) {
                    this.id = id;
                    this.value = value;
                    this.expiresAt = expiresAt;
                    this.inFlight = false;
                }
            }

            private final Map plans = new HashMap();

            synchronized void put(String token, String id, Object value, long expiresAt) {
                plans.put(token, new StoredPlan(id, value, expiresAt));
            }

            synchronized Claim claim(String token, String id, long now) {
                if (token == null || token.length() == 0) {
                    return new Claim(null, "revert_guard_required");
                }
                Object rawStored = plans.get(token);
                if (!(rawStored instanceof StoredPlan)) {
                    return new Claim(null, "invalid_revert_guard");
                }
                StoredPlan stored = (StoredPlan) rawStored;
                if (now > stored.expiresAt) {
                    plans.remove(token);
                    return new Claim(null, "expired_revert_guard");
                }
                if (!id.equals(stored.id)) {
                    return new Claim(null, "mismatched_revert_guard");
                }
                if (stored.inFlight) {
                    return new Claim(null, "revert_guard_in_flight");
                }
                stored.inFlight = true;
                return new Claim(stored.value, "");
            }

            synchronized boolean release(String token, Object value) {
                Object rawStored = plans.get(token);
                if (!(rawStored instanceof StoredPlan)) {
                    return false;
                }
                StoredPlan stored = (StoredPlan) rawStored;
                if (stored.value != value || !stored.inFlight) {
                    return false;
                }
                stored.inFlight = false;
                return true;
            }

            synchronized boolean consume(String token, Object value) {
                Object rawStored = plans.get(token);
                if (!(rawStored instanceof StoredPlan)) {
                    return false;
                }
                StoredPlan stored = (StoredPlan) rawStored;
                if (stored.value != value || !stored.inFlight) {
                    return false;
                }
                plans.remove(token);
                return true;
            }

            synchronized int size() {
                return plans.size();
            }

            synchronized void clear() {
                plans.clear();
            }
        }
        ''',
    )
    write(
        app / "tests" / "RetryableRevertPlanStoreTest.java",
        r'''
        package com.cameraautopilot.route3proof;

        public final class RetryableRevertPlanStoreTest {
            private static void check(boolean condition, String message) {
                if (!condition) {
                    throw new AssertionError(message);
                }
            }

            private static void checkFailureLeavesRetryable(String failureName) {
                RetryableRevertPlanStore store = new RetryableRevertPlanStore();
                Object plan = new Object();
                String token = failureName + "-token";
                String id = "param.iso_sensitivity";

                store.put(token, id, plan, 2000L);
                RetryableRevertPlanStore.Claim first = store.claim(token, id, 1000L);
                check(first.ok(), failureName + " should start with a valid claim");
                check(
                    store.release(token, plan),
                    failureName + " should release rather than consume the token"
                );
                check(store.size() == 1, failureName + " must preserve the revert plan");

                RetryableRevertPlanStore.Claim retry = store.claim(token, id, 1001L);
                check(retry.ok(), failureName + " token should remain retryable");
                check(
                    store.release(token, plan),
                    failureName + " retry claim should remain releasable"
                );
            }

            public static void main(String[] args) {
                checkFailureLeavesRetryable("camera-open-failure");
                checkFailureLeavesRetryable("apply-failure");
                checkFailureLeavesRetryable("readback-verification-failure");

                RetryableRevertPlanStore store = new RetryableRevertPlanStore();
                Object plan = new Object();
                long expiresAt = 2000L;

                store.put("retry-token", "param.iso_sensitivity", plan, expiresAt);
                RetryableRevertPlanStore.Claim first =
                    store.claim("retry-token", "param.iso_sensitivity", 1000L);
                check(first.ok(), "first claim should succeed");
                check(first.value == plan, "claim should return the stored plan");

                RetryableRevertPlanStore.Claim concurrent =
                    store.claim("retry-token", "param.iso_sensitivity", 1000L);
                check(!concurrent.ok(), "concurrent claim should be rejected");
                check(
                    "revert_guard_in_flight".equals(concurrent.error),
                    "concurrent claim should report in-flight state"
                );

                check(store.release("retry-token", plan), "failed revert should release its claim");
                check(store.size() == 1, "released plan must remain retryable");

                RetryableRevertPlanStore.Claim retry =
                    store.claim("retry-token", "param.iso_sensitivity", 1001L);
                check(retry.ok(), "released token should be claimable again");
                check(store.consume("retry-token", plan), "verified revert should consume its claim");
                check(store.size() == 0, "successful revert must remove the plan");

                RetryableRevertPlanStore.Claim reused =
                    store.claim("retry-token", "param.iso_sensitivity", 1002L);
                check(!reused.ok(), "successfully consumed token must reject reuse");
                check(
                    "invalid_revert_guard".equals(reused.error),
                    "reused token should be reported as invalid"
                );

                Object mismatchPlan = new Object();
                store.put("mismatch-token", "param.image_aspect", mismatchPlan, expiresAt);
                RetryableRevertPlanStore.Claim mismatch =
                    store.claim("mismatch-token", "param.iso_sensitivity", 1000L);
                check(!mismatch.ok(), "mismatched setting should be rejected");
                check(
                    "mismatched_revert_guard".equals(mismatch.error),
                    "mismatched setting should report the guard mismatch"
                );
                RetryableRevertPlanStore.Claim correct =
                    store.claim("mismatch-token", "param.image_aspect", 1000L);
                check(correct.ok(), "mismatch must not consume or claim the plan");
                check(store.release("mismatch-token", mismatchPlan), "correct claim should release");

                Object expiredPlan = new Object();
                store.put("expired-token", "param.raw_rec_mode", expiredPlan, 999L);
                RetryableRevertPlanStore.Claim expired =
                    store.claim("expired-token", "param.raw_rec_mode", 1000L);
                check(!expired.ok(), "expired token should be rejected");
                check(
                    "expired_revert_guard".equals(expired.error),
                    "expired token should report expiry"
                );
                RetryableRevertPlanStore.Claim expiredReuse =
                    store.claim("expired-token", "param.raw_rec_mode", 1000L);
                check(
                    "invalid_revert_guard".equals(expiredReuse.error),
                    "expired token should be removed"
                );

                System.out.println("RetryableRevertPlanStoreTest: PASS");
            }
        }
        ''',
    )
    write(
        package_dir / "Route3ProofActivity.java",
        r'''
        package com.cameraautopilot.route3proof;

        import android.app.Activity;
        import android.content.SharedPreferences;
        import android.hardware.Camera;
        import android.os.Bundle;
        import android.os.Handler;
        import android.view.KeyEvent;
        import android.view.SurfaceHolder;
        import android.view.SurfaceView;
        import android.view.View;
        import android.widget.Button;
        import android.widget.LinearLayout;
        import android.widget.TextView;

        import com.github.ma1co.openmemories.framework.DeviceInfo;
        import com.sony.scalar.hardware.CameraEx;

        import java.io.IOException;
        import java.net.Inet4Address;
        import java.net.InetAddress;
        import java.net.InetSocketAddress;
        import java.net.NetworkInterface;
        import java.net.Socket;
        import java.util.Enumeration;
        import java.util.HashMap;
        import java.util.List;
        import java.util.Map;
        import java.util.UUID;

        import fi.iki.elonen.NanoHTTPD;

        public class Route3ProofActivity extends Activity implements SurfaceHolder.Callback {
            private static final int PORT = 8080;
            private static final int API_SCHEMA_VERSION = 2;
            private static final String APP_VERSION = "0.2.2-vnext";
            private static final long WRITE_GUARD_TTL_MS = 300000L;
            private static final long RECOVERY_TTL_MS = 604800000L;
            private static final String RECOVERY_PREFS = "route3_recovery";
            private static final String RECOVERY_TOKEN = "token";
            private static final String RECOVERY_ID = "id";
            private static final String RECOVERY_KEY = "key";
            private static final String RECOVERY_ORIGINAL = "original";
            private static final String RECOVERY_WRITTEN = "written";
            private static final String RECOVERY_EXPIRES_AT = "expires_at";

            private final Handler handler = new Handler();
            private final Object lifecycleLock = new Object();
            private final Map pendingWrites = new HashMap();
            private final RetryableRevertPlanStore revertPlans = new RetryableRevertPlanStore();
            private TextView logView;
            private SurfaceHolder surfaceHolder;
            private CameraEx cameraEx;
            private Camera camera;
            private volatile ProofServer server;
            private volatile boolean previewRunning = false;
            private volatile boolean captureInFlight = false;
            private volatile boolean exitRequested = false;
            private volatile boolean activityResumed = false;
            private volatile String latestCapture = "";
            private volatile String latestError = "";
            private volatile String serverState = "stopped";
            private volatile String lastLifecycleEvent = "created";
            private volatile String exitSource = "";
            private volatile String latestGuardError = "";
            private volatile int captureCount = 0;
            private volatile int serverStartCount = 0;
            private volatile int serverStopCount = 0;
            private volatile int cameraOpenCount = 0;
            private volatile int cameraReleaseCount = 0;
            private volatile int previewStartCount = 0;
            private volatile int resumeCount = 0;
            private volatile int pauseCount = 0;
            private volatile int destroyCount = 0;
            private volatile int exitRequestCount = 0;
            private volatile long latestCaptureBytes = 0;
            private volatile long latestCaptureCompletedAt = 0;
            private volatile long serverStartedAt = 0;
            private final long activityStartedAt = System.currentTimeMillis();
            private Runnable closeRunnable;

            private static class PendingWrite {
                final String token;
                final String id;
                final String key;
                final String before;
                final String requested;
                final String recoveryToken;
                final long expiresAt;

                PendingWrite(
                    String token,
                    String id,
                    String key,
                    String before,
                    String requested,
                    String recoveryToken,
                    long expiresAt
                ) {
                    this.token = token;
                    this.id = id;
                    this.key = key;
                    this.before = before;
                    this.requested = requested;
                    this.recoveryToken = recoveryToken;
                    this.expiresAt = expiresAt;
                }
            }

            private static class RevertPlan {
                final String token;
                final String id;
                final String key;
                final String original;
                final String written;
                final long expiresAt;

                RevertPlan(String token, String id, String key, String original, String written, long expiresAt) {
                    this.token = token;
                    this.id = id;
                    this.key = key;
                    this.original = original;
                    this.written = written;
                    this.expiresAt = expiresAt;
                }
            }

            @Override
            protected void onCreate(Bundle savedInstanceState) {
                super.onCreate(savedInstanceState);
                restorePersistedRevertPlan();

                LinearLayout layout = new LinearLayout(this);
                layout.setOrientation(LinearLayout.VERTICAL);
                Button exitButton = new Button(this);
                exitButton.setText("Exit Route3 Proof");
                exitButton.setFocusable(true);
                exitButton.setFocusableInTouchMode(true);
                exitButton.setOnClickListener(new View.OnClickListener() {
                    public void onClick(View view) {
                        closeProofApp("button");
                    }
                });
                logView = new TextView(this);
                SurfaceView surfaceView = new SurfaceView(this);
                layout.addView(exitButton, new LinearLayout.LayoutParams(
                    LinearLayout.LayoutParams.MATCH_PARENT,
                    LinearLayout.LayoutParams.WRAP_CONTENT
                ));
                layout.addView(logView, new LinearLayout.LayoutParams(
                    LinearLayout.LayoutParams.MATCH_PARENT,
                    LinearLayout.LayoutParams.WRAP_CONTENT
                ));
                layout.addView(surfaceView, new LinearLayout.LayoutParams(
                    LinearLayout.LayoutParams.MATCH_PARENT,
                    1,
                    1.0f
                ));
                setContentView(layout);

                surfaceHolder = surfaceView.getHolder();
                surfaceHolder.setType(SurfaceHolder.SURFACE_TYPE_PUSH_BUFFERS);
                surfaceHolder.addCallback(this);
                exitButton.requestFocus();
                log("Route3 proof starting");
            }

            @Override
            public void onBackPressed() {
                closeProofApp("back");
            }

            @Override
            public boolean onKeyDown(int keyCode, KeyEvent event) {
                if (keyCode == KeyEvent.KEYCODE_BACK || keyCode == KeyEvent.KEYCODE_MENU) {
                    closeProofApp(keyCode == KeyEvent.KEYCODE_MENU ? "menu_key" : "back_key");
                    return true;
                }
                return super.onKeyDown(keyCode, event);
            }

            @Override
            protected void onResume() {
                super.onResume();
                activityResumed = true;
                exitRequested = false;
                exitSource = "";
                lastLifecycleEvent = "resume";
                resumeCount += 1;
                openCameraNow("open");
                startServerIfNeeded();
            }

            @Override
            protected void onPause() {
                activityResumed = false;
                lastLifecycleEvent = "pause";
                pauseCount += 1;
                shutdownResources("pause");
                super.onPause();
            }

            @Override
            protected void onDestroy() {
                lastLifecycleEvent = "destroy";
                destroyCount += 1;
                shutdownResources("destroy");
                handler.removeCallbacksAndMessages(null);
                logView = null;
                super.onDestroy();
            }

            private void closeProofApp(final String source) {
                closeProofAppDelayed(source, 0);
            }

            private void closeProofAppDelayed(final String source, long delayMs) {
                synchronized (lifecycleLock) {
                    if (exitRequested) {
                        log("exit already requested: " + exitSource);
                        return;
                    }
                    exitRequested = true;
                    exitSource = source;
                    lastLifecycleEvent = "exit_requested";
                    exitRequestCount += 1;
                }
                log("exit requested: " + source);
                closeRunnable = new Runnable() {
                    public void run() {
                        lastLifecycleEvent = "exit_running";
                        shutdownResources("exit:" + source);
                        finish();
                    }
                };
                handler.postDelayed(closeRunnable, delayMs);
            }

            private boolean startServerIfNeeded() {
                synchronized (lifecycleLock) {
                    if (exitRequested) {
                        latestError = "server start blocked: exit requested";
                        return false;
                    }
                    if (server != null) {
                        log("HTTP proof server already running on :" + PORT);
                        return true;
                    }
                    serverState = "starting";
                    try {
                        ProofServer candidate = new ProofServer();
                        candidate.start();
                        server = candidate;
                        serverState = "running";
                        serverStartedAt = System.currentTimeMillis();
                        serverStartCount += 1;
                        lastLifecycleEvent = "server_started";
                        latestError = "";
                        log("HTTP proof server on :" + PORT);
                        return true;
                    } catch (IOException e) {
                        server = null;
                        serverState = "error";
                        latestError = "server: " + e.toString();
                        lastLifecycleEvent = "server_start_failed";
                        log(latestError);
                        return false;
                    }
                }
            }

            private void stopServerIfNeeded(String reason) {
                ProofServer toStop = null;
                synchronized (lifecycleLock) {
                    if (server == null) {
                        serverState = "stopped";
                        return;
                    }
                    toStop = server;
                    server = null;
                    serverState = "stopping";
                }
                try {
                    toStop.stop();
                } catch (Throwable t) {
                    latestError = "server stop: " + t.toString();
                    log(latestError);
                } finally {
                    synchronized (lifecycleLock) {
                        serverState = "stopped";
                        serverStartedAt = 0;
                        serverStopCount += 1;
                        lastLifecycleEvent = "server_stopped:" + reason;
                    }
                }
            }

            private void shutdownResources(String reason) {
                stopServerIfNeeded(reason);
                releaseCameraNow();
                synchronized (lifecycleLock) {
                    pendingWrites.clear();
                }
            }

            private void releaseCameraNow() {
                boolean released = camera != null || cameraEx != null;
                if (camera != null) {
                    try {
                        camera.stopPreview();
                    } catch (Throwable ignored) {
                    }
                }
                if (cameraEx != null) {
                    cameraEx.release();
                    cameraEx = null;
                }
                camera = null;
                previewRunning = false;
                captureInFlight = false;
                if (released) {
                    cameraReleaseCount += 1;
                    log("camera released");
                }
            }

            private boolean openCameraNow(String label) {
                try {
                    if (cameraEx == null || camera == null) {
                        cameraEx = CameraEx.open(0, null);
                        camera = cameraEx.getNormalCamera();
                        cameraOpenCount += 1;
                        log("CameraEx opened");
                    }
                    return true;
                } catch (Throwable t) {
                    latestError = label + ": " + t.toString();
                    log(latestError);
                    return false;
                }
            }

            private boolean reopenCameraNow(String label) {
                releaseCameraNow();
                if (!openCameraNow(label)) {
                    return false;
                }
                startPreviewNow(label + " preview");
                return previewRunning;
            }

            @Override
            public void surfaceCreated(SurfaceHolder holder) {
                startPreviewOnUiThread();
            }

            @Override
            public void surfaceChanged(SurfaceHolder holder, int format, int width, int height) {
            }

            @Override
            public void surfaceDestroyed(SurfaceHolder holder) {
                previewRunning = false;
            }

            private void startPreviewOnUiThread() {
                handler.post(new Runnable() {
                    public void run() {
                        startPreviewNow("preview");
                    }
                });
            }

            private void startPreviewNow(String label) {
                try {
                    if (camera == null) {
                        latestError = label + ": camera null";
                        log(latestError);
                        return;
                    }
                    if (previewRunning) {
                        log(label + " already running");
                        return;
                    }
                    camera.setPreviewDisplay(surfaceHolder);
                    camera.startPreview();
                    previewRunning = true;
                    previewStartCount += 1;
                    log(label + " started");
                } catch (Throwable t) {
                    previewRunning = false;
                    latestError = label + ": " + t.toString();
                    log(latestError);
                }
            }

            private void restartPreviewOnUiThread() {
                handler.post(new Runnable() {
                    public void run() {
                        try {
                            if (camera != null) {
                                try {
                                    camera.stopPreview();
                                } catch (Throwable ignored) {
                                }
                                previewRunning = false;
                                startPreviewNow("preview restart");
                            }
                        } catch (Throwable t) {
                            previewRunning = false;
                            latestError = "preview restart: " + t.toString();
                            log(latestError);
                        }
                    }
                });
            }

            private void autoFocusOnUiThread() {
                handler.post(new Runnable() {
                    public void run() {
                        try {
                            if (camera != null) {
                                camera.autoFocus(new Camera.AutoFocusCallback() {
                                    public void onAutoFocus(boolean success, Camera c) {
                                        log("autofocus success=" + success);
                                    }
                                });
                            }
                        } catch (Throwable t) {
                            latestError = "autofocus: " + t.toString();
                            log(latestError);
                        }
                    }
                });
            }

            private void captureOnUiThread() {
                handler.post(new Runnable() {
                    public void run() {
                        try {
                            if (camera == null) {
                                latestError = "capture: camera null";
                                captureInFlight = false;
                                log(latestError);
                                return;
                            }
                            captureInFlight = true;
                            latestError = "";
                            if (captureCount > 0 && !reopenCameraNow("reopen before capture")) {
                                captureInFlight = false;
                                return;
                            }
                            try {
                                camera.cancelAutoFocus();
                            } catch (Throwable ignored) {
                            }
                            if (!previewRunning) {
                                startPreviewNow("preview before capture");
                            }
                            camera.takePicture(null, null, new Camera.PictureCallback() {
                                public void onPictureTaken(byte[] data, Camera c) {
                                    try {
                                        latestCapture = "memory://route3_proof_" + System.currentTimeMillis() + ".jpg";
                                        latestCaptureBytes = data.length;
                                        latestCaptureCompletedAt = System.currentTimeMillis();
                                        captureCount += 1;
                                        latestError = "";
                                        log("captured " + latestCapture + " bytes=" + data.length);
                                    } catch (Throwable t) {
                                        latestError = "write capture: " + t.toString();
                                        log(latestError);
                                    } finally {
                                        captureInFlight = false;
                                    }
                                    previewRunning = false;
                                    restartPreviewOnUiThread();
                                }
                            });
                        } catch (Throwable t) {
                            captureInFlight = false;
                            previewRunning = false;
                            latestError = "capture: " + t.toString();
                            log(latestError);
                            restartPreviewOnUiThread();
                        }
                    }
                });
            }

            private void log(final String line) {
                if (logView == null) {
                    return;
                }
                handler.post(new Runnable() {
                    public void run() {
                        if (logView != null) {
                            logView.append(line + "\n");
                        }
                    }
                });
            }

            private static String q(String value) {
                if (value == null) {
                    return "";
                }
                return value
                    .replace("\\", "\\\\")
                    .replace("\"", "\\\"")
                    .replace("\n", "\\n")
                    .replace("\r", "\\r")
                    .replace("\t", "\\t");
            }

            private boolean serverIsRunning() {
                return server != null && "running".equals(serverState);
            }

            private String lifecycleState() {
                if (exitRequested) {
                    return "exiting";
                }
                if (activityResumed) {
                    return "running";
                }
                return "paused";
            }

            private String networkJson() {
                StringBuilder addresses = new StringBuilder("[");
                boolean first = true;
                int count = 0;
                try {
                    Enumeration interfaces = NetworkInterface.getNetworkInterfaces();
                    while (interfaces != null && interfaces.hasMoreElements()) {
                        NetworkInterface iface = (NetworkInterface) interfaces.nextElement();
                        Enumeration inetAddresses = iface.getInetAddresses();
                        while (inetAddresses != null && inetAddresses.hasMoreElements()) {
                            InetAddress address = (InetAddress) inetAddresses.nextElement();
                            if (!(address instanceof Inet4Address) || address.isLoopbackAddress()) {
                                continue;
                            }
                            if (!first) {
                                addresses.append(",");
                            }
                            addresses.append("{\"interface\":\"")
                                .append(q(iface.getName()))
                                .append("\",\"address\":\"")
                                .append(q(address.getHostAddress()))
                                .append("\"}");
                            first = false;
                            count += 1;
                        }
                    }
                } catch (Throwable t) {
                    latestError = "network diagnostics: " + t.toString();
                }
                addresses.append("]");
                return "{"
                    + "\"ipv4Addresses\":" + addresses.toString() + ","
                    + "\"addressCount\":" + count
                    + "}";
            }

            private boolean localPortOpen(int port) {
                Socket socket = new Socket();
                try {
                    socket.connect(new InetSocketAddress("127.0.0.1", port), 150);
                    return true;
                } catch (Throwable ignored) {
                    return false;
                } finally {
                    try {
                        socket.close();
                    } catch (Throwable ignored) {
                    }
                }
            }

            private String developerPortsJson() {
                boolean telnetOpen = localPortOpen(23);
                boolean adbOpen = localPortOpen(5555);
                return "{"
                    + "\"telnet23\":{\"open\":" + telnetOpen + ",\"verification\":\"camera_local_socket_probe\"},"
                    + "\"adb5555\":{\"open\":" + adbOpen + ",\"verification\":\"camera_local_socket_probe\"},"
                    + "\"externalFinalScanRequired\":true"
                    + "}";
            }

            private String statusJson() {
                return "{"
                    + "\"schemaVersion\":" + API_SCHEMA_VERSION + ","
                    + "\"appVersion\":\"" + APP_VERSION + "\","
                    + "\"route\":\"openmemories_route3\","
                    + "\"ok\":" + (camera != null) + ","
                    + "\"cameraOpen\":" + (camera != null) + ","
                    + "\"previewRunning\":" + previewRunning + ","
                    + "\"captureInFlight\":" + captureInFlight + ","
                    + "\"captureCount\":" + captureCount + ","
                    + "\"latestCapture\":\"" + q(latestCapture) + "\","
                    + "\"latestCaptureBytes\":" + latestCaptureBytes + ","
                    + "\"latestCaptureCompletedAt\":" + latestCaptureCompletedAt + ","
                    + "\"latestError\":\"" + q(latestError) + "\","
                    + "\"lifecycleState\":\"" + lifecycleState() + "\","
                    + "\"serverState\":\"" + q(serverState) + "\""
                    + "}";
            }

            private String healthJson() {
                String model = "";
                String firmware = "";
                try {
                    DeviceInfo info = DeviceInfo.getInstance();
                    model = info.getModel();
                    firmware = info.getFirmwareVersion();
                } catch (Throwable ignored) {
                }
                return "{"
                    + "\"schemaVersion\":" + API_SCHEMA_VERSION + ","
                    + "\"appVersion\":\"" + APP_VERSION + "\","
                    + "\"route\":\"openmemories_route3\","
                    + "\"ok\":" + (camera != null && serverIsRunning() && !exitRequested) + ","
                    + "\"model\":\"" + q(model) + "\","
                    + "\"firmware\":\"" + q(firmware) + "\","
                    + "\"cameraOpen\":" + (camera != null) + ","
                    + "\"previewRunning\":" + previewRunning + ","
                    + "\"warning\":\"" + (camera == null ? "camera_not_open" : exitRequested ? "exit_requested" : "") + "\","
                    + "\"captureInFlight\":" + captureInFlight + ","
                    + "\"captureCount\":" + captureCount + ","
                    + "\"latestCapture\":\"" + q(latestCapture) + "\","
                    + "\"latestCaptureBytes\":" + latestCaptureBytes + ","
                    + "\"latestCaptureCompletedAt\":" + latestCaptureCompletedAt + ","
                    + "\"latestError\":\"" + q(latestError) + "\","
                    + "\"latestGuardError\":\"" + q(latestGuardError) + "\","
                    + "\"lifecycleState\":\"" + lifecycleState() + "\","
                    + "\"lastLifecycleEvent\":\"" + q(lastLifecycleEvent) + "\","
                    + "\"exitRequested\":" + exitRequested + ","
                    + "\"exitSource\":\"" + q(exitSource) + "\","
                    + "\"serverState\":\"" + q(serverState) + "\","
                    + "\"serverPort\":" + PORT + ","
                    + "\"serverStartCount\":" + serverStartCount + ","
                    + "\"serverStopCount\":" + serverStopCount + ","
                    + "\"uptimeMs\":" + (System.currentTimeMillis() - activityStartedAt) + ","
                    + "\"network\":" + networkJson() + ","
                    + "\"capabilities\":\"GET /capabilities\","
                    + "\"diagnostics\":\"GET /diagnostics\","
                    + "\"rollback\":\"GET /rollback\","
                    + "\"destructiveOperationsEnabled\":false,"
                    + "\"writeGuardRequired\":true"
                    + "}";
            }

            private String capabilitiesJson() {
                return "{"
                    + "\"schemaVersion\":" + API_SCHEMA_VERSION + ","
                    + "\"appVersion\":\"" + APP_VERSION + "\","
                    + "\"route\":\"openmemories_route3\","
                    + "\"ok\":true,"
                    + "\"destructiveOperationsEnabled\":false,"
                    + "\"writePolicy\":{"
                    + "\"mode\":\"allowlist_two_phase\","
                    + "\"dryRunRequired\":true,"
                    + "\"guardTokenRequired\":true,"
                    + "\"guardTtlMs\":" + WRITE_GUARD_TTL_MS + ","
                    + "\"recoveryTtlMs\":" + RECOVERY_TTL_MS + ","
                    + "\"supportedValueValidationRequired\":true,"
                    + "\"readbackRequired\":true,"
                    + "\"oneTimeRevertToken\":true,"
                    + "\"recoveryTokenIssuedBeforeWrite\":true,"
                    + "\"recoveryPlanPersistsAcrossPause\":true"
                    + "},"
                    + "\"endpoints\":["
                    + "\"GET /health\","
                    + "\"GET /capabilities\","
                    + "\"GET /diagnostics\","
                    + "\"GET /settings\","
                    + "\"GET /settings/{id}\","
                    + "\"POST /settings/{id}?value=<value>&dry_run=true\","
                    + "\"POST /settings/{id}?value=<value>&guard_token=<token>\","
                    + "\"POST /settings/{id}/revert?revert_token=<token>\","
                    + "\"GET /parameters\","
                    + "\"POST /preview\","
                    + "\"POST /focus\","
                    + "\"POST /capture\","
                    + "\"GET /media/latest\","
                    + "\"GET /rollback\","
                    + "\"POST /exit\","
                    + "\"POST /shutdown\""
                    + "],"
                    + "\"forbiddenOperations\":["
                    + "\"format\","
                    + "\"delete\","
                    + "\"setting_reset\","
                    + "\"factory_reset\","
                    + "\"initialize\","
                    + "\"network_reset\","
                    + "\"cleaning_mode\","
                    + "\"firmware_patch\","
                    + "\"service_opcode\""
                    + "]"
                    + "}";
            }

            private String diagnosticsJson() {
                return "{"
                    + "\"schemaVersion\":" + API_SCHEMA_VERSION + ","
                    + "\"appVersion\":\"" + APP_VERSION + "\","
                    + "\"route\":\"openmemories_route3\","
                    + "\"ok\":true,"
                    + "\"lifecycleState\":\"" + lifecycleState() + "\","
                    + "\"lastLifecycleEvent\":\"" + q(lastLifecycleEvent) + "\","
                    + "\"activityResumed\":" + activityResumed + ","
                    + "\"exitRequested\":" + exitRequested + ","
                    + "\"exitSource\":\"" + q(exitSource) + "\","
                    + "\"lifecycleCounts\":{"
                    + "\"resume\":" + resumeCount + ","
                    + "\"pause\":" + pauseCount + ","
                    + "\"destroy\":" + destroyCount + ","
                    + "\"exitRequest\":" + exitRequestCount
                    + "},"
                    + "\"server\":{"
                    + "\"state\":\"" + q(serverState) + "\","
                    + "\"port\":" + PORT + ","
                    + "\"startCount\":" + serverStartCount + ","
                    + "\"stopCount\":" + serverStopCount + ","
                    + "\"startedAt\":" + serverStartedAt
                    + "},"
                    + "\"camera\":{"
                    + "\"open\":" + (camera != null) + ","
                    + "\"openCount\":" + cameraOpenCount + ","
                    + "\"previewRunning\":" + previewRunning + ","
                    + "\"previewStartCount\":" + previewStartCount + ","
                    + "\"releaseCount\":" + cameraReleaseCount
                    + "},"
                    + "\"network\":" + networkJson() + ","
                    + "\"developerPorts\":" + developerPortsJson() + ","
                    + "\"guards\":{"
                    + "\"pendingWriteCount\":" + pendingWriteCount() + ","
                    + "\"pendingRevertCount\":" + pendingRevertCount() + ","
                    + "\"latestError\":\"" + q(latestGuardError) + "\""
                    + "},"
                    + "\"latestError\":\"" + q(latestError) + "\""
                    + "}";
            }

            private String writableSettingJson(String id, String label) {
                return "{"
                    + "\"id\":\"" + q(id) + "\","
                    + "\"label\":\"" + q(label) + "\","
                    + "\"route\":\"openmemories\","
                    + "\"safe\":true,"
                    + "\"readOnly\":false,"
                    + "\"dryRunEndpoint\":\"POST /settings/" + q(id) + "?value=<camera-value>&dry_run=true\","
                    + "\"commitEndpoint\":\"POST /settings/" + q(id) + "?value=<same-value>&guard_token=<token>\","
                    + "\"commitRequires\":\"one-time guard_token from matching dry-run\","
                    + "\"revertEndpoint\":\"POST /settings/" + q(id) + "/revert?revert_token=<token>\""
                    + "}";
            }

            private String settingsJson() {
                return "{"
                    + "\"schemaVersion\":" + API_SCHEMA_VERSION + ","
                    + "\"appVersion\":\"" + APP_VERSION + "\","
                    + "\"route\":\"openmemories_route3\","
                    + "\"ok\":true,"
                    + "\"readOnly\":false,"
                    + "\"writesAreAllowlisted\":true,"
                    + "\"destructiveOperationsEnabled\":false,"
                    + "\"writeGuardRequired\":true,"
                    + "\"writePolicy\":\"dry-run returns a one-time guard token and a durable recovery token before mutation; commit and revert each require their token\","
                    + "\"capabilitiesEndpoint\":\"GET /capabilities\","
                    + "\"diagnosticsEndpoint\":\"GET /diagnostics\","
                    + "\"revertEndpoint\":\"POST /settings/{id}/revert?revert_token=<token>\","
                    + "\"safeWriteEndpoints\":["
                    + "\"POST /settings/param.self_timer?value=<seconds>&dry_run=true\","
                    + "\"POST /settings/param.self_timer?value=<seconds>&guard_token=<token>\","
                    + "\"POST /settings/param.self_timer/revert?revert_token=<token>\","
                    + "\"POST /settings/param.<allowlisted-key>?value=<camera-value>&dry_run=true\","
                    + "\"POST /settings/param.<allowlisted-key>?value=<camera-value>&guard_token=<token>\","
                    + "\"POST /settings/param.<allowlisted-key>/revert?revert_token=<token>\""
                    + "],"
                    + "\"rawReadEndpoints\":[\"GET /parameters\",\"GET /settings/param.dump\",\"GET /settings/param.<camera-parameter-key>\"],"
                    + "\"settings\":["
                    + "{\"id\":\"device.model\",\"label\":\"Device model\",\"route\":\"openmemories\",\"safe\":true},"
                    + "{\"id\":\"preview.running\",\"label\":\"Preview running\",\"route\":\"openmemories\",\"safe\":true},"
                    + "{\"id\":\"param.dump\",\"label\":\"Raw Camera.Parameters dump\",\"route\":\"openmemories\",\"safe\":true,\"readOnly\":true,\"readEndpoint\":\"GET /parameters\"},"
                    + "{\"id\":\"param.<camera-parameter-key>\",\"label\":\"Raw Camera.Parameters key read\",\"route\":\"openmemories\",\"safe\":true,\"readOnly\":true,\"readEndpoint\":\"GET /settings/param.<camera-parameter-key>\"},"
                    + writableSettingJson("param.self_timer", "Self timer parameter") + ","
                    + writableSettingJson("param.iso_sensitivity", "ISO sensitivity parameter") + ","
                    + writableSettingJson("param.image_aspect", "Image aspect parameter") + ","
                    + writableSettingJson("param.image_size", "Image size parameter") + ","
                    + writableSettingJson("param.metering_mode", "Metering mode parameter") + ","
                    + writableSettingJson("param.dro_mode", "DRO mode parameter") + ","
                    + writableSettingJson("param.color_mode", "Creative style/color mode parameter") + ","
                    + writableSettingJson("param.picture_effect", "Picture effect parameter") + ","
                    + writableSettingJson("param.face_detection_mode", "Face detection parameter") + ","
                    + writableSettingJson("param.flash_mode", "Flash mode parameter") + ","
                    + writableSettingJson("param.red_eye_reduction_mode", "Red-eye reduction parameter") + ","
                    + writableSettingJson("param.color_space", "Color space parameter") + ","
                    + writableSettingJson("param.apsc_mode", "APS-C mode parameter") + ","
                    + writableSettingJson("param.raw_rec_mode", "RAW recording mode parameter") + ","
                    + writableSettingJson("param.drive_mode", "Drive mode parameter") + ","
                    + writableSettingJson("param.soft_skin_effect", "Soft skin effect parameter") + ","
                    + writableSettingJson("param.focus_mode", "Focus mode parameter") + ","
                    + "{\"id\":\"focus.af\",\"label\":\"Autofocus action\",\"route\":\"openmemories\",\"safe\":true},"
                    + "{\"id\":\"capture.latest\",\"label\":\"Latest capture path\",\"route\":\"openmemories\",\"safe\":true},"
                    + "{\"id\":\"capture.count\",\"label\":\"Capture count\",\"route\":\"openmemories\",\"safe\":true},"
                    + "{\"id\":\"capture.bytes\",\"label\":\"Latest capture bytes\",\"route\":\"openmemories\",\"safe\":true},"
                    + "{\"id\":\"error.latest\",\"label\":\"Latest error\",\"route\":\"openmemories\",\"safe\":true}"
                    + "]"
                    + "}";
            }

            private String rollbackJson() {
                boolean telnetOpen = localPortOpen(23);
                boolean adbOpen = localPortOpen(5555);
                return "{"
                    + "\"schemaVersion\":" + API_SCHEMA_VERSION + ","
                    + "\"appVersion\":\"" + APP_VERSION + "\","
                    + "\"ok\":true,"
                    + "\"route\":\"openmemories_route3\","
                    + "\"lifecycleState\":\"" + lifecycleState() + "\","
                    + "\"exitRequested\":" + exitRequested + ","
                    + "\"serverState\":\"" + q(serverState) + "\","
                    + "\"cameraOpen\":" + (camera != null) + ","
                    + "\"previewRunning\":" + previewRunning + ","
                    + "\"proofServer\":\"POST /exit or POST /shutdown\","
                    + "\"portState\":["
                    + "{\"port\":8080,\"owner\":\"route3_proof\",\"state\":\"" + (serverIsRunning() ? "open_in_process" : "stopped_in_process") + "\",\"verification\":\"in_process\"},"
                    + "{\"port\":5555,\"owner\":\"adb\",\"state\":\"" + (adbOpen ? "open_local_probe" : "closed_local_probe") + "\",\"verification\":\"camera_local_socket_probe\"},"
                    + "{\"port\":23,\"owner\":\"telnet\",\"state\":\"" + (telnetOpen ? "open_local_probe" : "closed_local_probe") + "\",\"verification\":\"camera_local_socket_probe\"}"
                    + "],"
                    + "\"expectedAfterExit\":{"
                    + "\"cameraReleased\":true,"
                    + "\"previewStopped\":true,"
                    + "\"port8080Closed\":true,"
                    + "\"pendingWriteTokensCleared\":true,"
                    + "\"pendingRevertTokensCleared\":true"
                    + "},"
                    + "\"operatorSteps\":["
                    + "\"POST /exit to close Route3 Proof\","
                    + "\"Disable ADB and Telnet in OpenMemories Tweak\","
                    + "\"Disable camera Wi-Fi if it was enabled only for proof\","
                    + "\"Run prove_openmemories_rollback_ports.py and confirm ports 23, 5555, and 8080 are closed\""
                    + "]"
                    + "}";
            }

            private String writeNotImplementedJson(String id) {
                return "{"
                    + "\"ok\":false,"
                    + "\"id\":\"" + q(id) + "\","
                    + "\"error\":\"setting_write_not_implemented\","
                    + "\"allowlistedWrite\":false,"
                    + "\"safe\":true,"
                    + "\"message\":\"This parameter is not in the Route3 allowlist; no write path exists\""
                    + "}";
            }

            private String allowlistedParamKey(String id) {
                if (id == null || !id.startsWith("param.")) {
                    return null;
                }
                String suffix = id.substring("param.".length()).replace('_', '-');
                if ("iso-sensitivity".equals(suffix)) return "iso-sensitivity";
                if ("image-aspect".equals(suffix)) return "image-aspect";
                if ("image-size".equals(suffix)) return "image-size";
                if ("metering-mode".equals(suffix)) return "metering-mode";
                if ("dro-mode".equals(suffix)) return "dro-mode";
                if ("color-mode".equals(suffix)) return "color-mode";
                if ("picture-effect".equals(suffix)) return "picture-effect";
                if ("face-detection-mode".equals(suffix)) return "face-detection-mode";
                if ("flash-mode".equals(suffix)) return "flash-mode";
                if ("red-eye-reduction-mode".equals(suffix)) return "red-eye-reduction-mode";
                if ("color-space".equals(suffix)) return "color-space";
                if ("apsc-mode".equals(suffix)) return "apsc-mode";
                if ("raw-rec-mode".equals(suffix)) return "raw-rec-mode";
                if ("drive-mode".equals(suffix)) return "drive-mode";
                if ("soft-skin-effect".equals(suffix)) return "soft-skin-effect";
                if ("focus-mode".equals(suffix)) return "focus-mode";
                return null;
            }

            private String registryIdForParamKey(String key) {
                if ("iso-sensitivity".equals(key)) return "camera.iso";
                if ("image-aspect".equals(key)) return "camera.aspect_ratio";
                if ("image-size".equals(key)) return "camera.image_size";
                if ("metering-mode".equals(key)) return "camera.metering_mode";
                if ("dro-mode".equals(key)) return "camera.dro_auto_hdr";
                if ("color-mode".equals(key)) return "camera.creative_style";
                if ("picture-effect".equals(key)) return "camera.picture_effect";
                if ("face-detection-mode".equals(key)) return "camera.smile_face_detect";
                if ("flash-mode".equals(key)) return "camera.flash_mode";
                if ("red-eye-reduction-mode".equals(key)) return "camera.red_eye_reduction";
                if ("color-space".equals(key)) return "camera.color_space";
                if ("apsc-mode".equals(key)) return "custom.aps_c_size_capture";
                if ("raw-rec-mode".equals(key)) return "camera.raw_file_type";
                if ("drive-mode".equals(key)) return "camera.drive_mode";
                if ("soft-skin-effect".equals(key)) return "camera.soft_skin_effect";
                if ("focus-mode".equals(key)) return "camera.focus_mode";
                return "";
            }

            private String csvJson(String values) {
                String json = "[";
                if (values != null && values.length() > 0) {
                    String[] parts = values.split(",");
                    boolean first = true;
                    for (int i = 0; i < parts.length; i += 1) {
                        String value = parts[i].trim();
                        if (value.length() == 0) {
                            continue;
                        }
                        if (!first) {
                            json += ",";
                        }
                        first = false;
                        json += "\"" + q(value) + "\"";
                    }
                }
                return json + "]";
            }

            private boolean csvContains(String values, String requested) {
                if (values == null || requested == null) {
                    return false;
                }
                String[] parts = values.split(",");
                for (int i = 0; i < parts.length; i += 1) {
                    if (requested.equals(parts[i].trim())) {
                        return true;
                    }
                }
                return false;
            }

            private String listJson(List values) {
                String json = "[";
                if (values != null) {
                    for (int i = 0; i < values.size(); i += 1) {
                        if (i > 0) {
                            json += ",";
                        }
                        Object value = values.get(i);
                        if (value instanceof Number || value instanceof Boolean) {
                            json += String.valueOf(value);
                        } else {
                            json += "\"" + q(String.valueOf(value)) + "\"";
                        }
                    }
                }
                return json + "]";
            }

            private boolean truthy(String value) {
                if (value == null) {
                    return false;
                }
                return "true".equalsIgnoreCase(value) || "1".equals(value) || "yes".equalsIgnoreCase(value);
            }

            private boolean destructiveSettingId(String id) {
                if (id == null) {
                    return false;
                }
                String normalized = id.toLowerCase().replace('-', '_').replace('.', '_');
                return normalized.indexOf("format") >= 0
                    || normalized.indexOf("delete") >= 0
                    || normalized.indexOf("factory_reset") >= 0
                    || normalized.indexOf("setting_reset") >= 0
                    || normalized.indexOf("network_reset") >= 0
                    || normalized.indexOf("initialize") >= 0
                    || normalized.indexOf("cleaning_mode") >= 0
                    || normalized.indexOf("firmware_patch") >= 0
                    || normalized.indexOf("service_opcode") >= 0;
            }

            private String destructiveWriteForbiddenJson(String id) {
                return "{"
                    + "\"ok\":false,"
                    + "\"id\":\"" + q(id) + "\","
                    + "\"error\":\"destructive_operation_forbidden\","
                    + "\"destructiveOperationsEnabled\":false,"
                    + "\"message\":\"Route3 vNext contains no implementation for destructive camera operations\""
                    + "}";
            }

            private int pendingWriteCount() {
                synchronized (lifecycleLock) {
                    return pendingWrites.size();
                }
            }

            private int pendingRevertCount() {
                synchronized (lifecycleLock) {
                    return revertPlans.size();
                }
            }

            private String issueWriteGuard(
                String id,
                String key,
                String before,
                String requested,
                String recoveryToken
            ) {
                String token = "write-" + UUID.randomUUID().toString();
                PendingWrite plan = new PendingWrite(
                    token,
                    id,
                    key,
                    before,
                    requested,
                    recoveryToken,
                    System.currentTimeMillis() + WRITE_GUARD_TTL_MS
                );
                synchronized (lifecycleLock) {
                    pendingWrites.put(token, plan);
                }
                return token;
            }

            private PendingWrite consumeWriteGuard(
                String token,
                String id,
                String key,
                String before,
                String requested
            ) {
                synchronized (lifecycleLock) {
                    latestGuardError = "";
                    if (token == null || token.length() == 0) {
                        latestGuardError = "write_guard_required";
                        return null;
                    }
                    Object rawPlan = pendingWrites.get(token);
                    if (!(rawPlan instanceof PendingWrite)) {
                        latestGuardError = "invalid_write_guard";
                        return null;
                    }
                    PendingWrite plan = (PendingWrite) rawPlan;
                    if (System.currentTimeMillis() > plan.expiresAt) {
                        pendingWrites.remove(token);
                        latestGuardError = "expired_write_guard";
                        return null;
                    }
                    if (!id.equals(plan.id)
                        || !key.equals(plan.key)
                        || !requested.equals(plan.requested)
                        || !before.equals(plan.before)) {
                        latestGuardError = "stale_or_mismatched_write_guard";
                        return null;
                    }
                    pendingWrites.remove(token);
                    return plan;
                }
            }

            private SharedPreferences recoveryPreferences() {
                return getSharedPreferences(RECOVERY_PREFS, MODE_PRIVATE);
            }

            private void persistRevertPlan(RevertPlan plan) {
                SharedPreferences.Editor editor = recoveryPreferences().edit();
                editor.putString(RECOVERY_TOKEN, plan.token);
                editor.putString(RECOVERY_ID, plan.id);
                editor.putString(RECOVERY_KEY, plan.key);
                editor.putString(RECOVERY_ORIGINAL, plan.original);
                editor.putString(RECOVERY_WRITTEN, plan.written);
                editor.putLong(RECOVERY_EXPIRES_AT, plan.expiresAt);
                if (!editor.commit()) {
                    throw new IllegalStateException("recovery_plan_persist_failed");
                }
            }

            private void clearPersistedRevertPlan(String token) {
                SharedPreferences preferences = recoveryPreferences();
                String storedToken = preferences.getString(RECOVERY_TOKEN, "");
                if (token == null || token.length() == 0 || token.equals(storedToken)) {
                    preferences.edit().clear().commit();
                }
            }

            private void restorePersistedRevertPlan() {
                SharedPreferences preferences = recoveryPreferences();
                String token = preferences.getString(RECOVERY_TOKEN, "");
                if (token.length() == 0) {
                    return;
                }
                long expiresAt = preferences.getLong(RECOVERY_EXPIRES_AT, 0L);
                if (expiresAt <= System.currentTimeMillis()) {
                    clearPersistedRevertPlan(token);
                    synchronized (lifecycleLock) {
                        revertPlans.clear();
                    }
                    latestGuardError = "expired_revert_guard";
                    return;
                }
                RevertPlan plan = new RevertPlan(
                    token,
                    preferences.getString(RECOVERY_ID, ""),
                    preferences.getString(RECOVERY_KEY, ""),
                    preferences.getString(RECOVERY_ORIGINAL, ""),
                    preferences.getString(RECOVERY_WRITTEN, ""),
                    expiresAt
                );
                synchronized (lifecycleLock) {
                    revertPlans.put(plan.token, plan.id, plan, plan.expiresAt);
                }
            }

            private boolean hasPendingRecoveryPlan() {
                restorePersistedRevertPlan();
                return pendingRevertCount() > 0;
            }

            private String pendingRecoveryFailureJson(String id) {
                return "{"
                    + "\"ok\":false,"
                    + "\"id\":\"" + q(id) + "\","
                    + "\"phase\":\"dry_run\","
                    + "\"error\":\"pending_recovery_requires_revert\","
                    + "\"message\":\"Complete the outstanding recovery before starting another write\","
                    + "\"revertEndpoint\":\"POST /settings/" + q(id) + "/revert?revert_token=<recovery-token>\""
                    + "}";
            }

            private String issueRevertPlan(String id, String key, String original, String written) {
                String token = "revert-" + UUID.randomUUID().toString();
                RevertPlan plan = new RevertPlan(
                    token,
                    id,
                    key,
                    original,
                    written,
                    System.currentTimeMillis() + RECOVERY_TTL_MS
                );
                persistRevertPlan(plan);
                synchronized (lifecycleLock) {
                    revertPlans.put(token, id, plan, plan.expiresAt);
                }
                return token;
            }

            private RevertPlan beginRevertPlan(String token, String id) {
                RetryableRevertPlanStore.Claim claim =
                    revertPlans.claim(token, id, System.currentTimeMillis());
                latestGuardError = claim.error;
                if ("expired_revert_guard".equals(claim.error)) {
                    clearPersistedRevertPlan(token);
                }
                if (!claim.ok() || !(claim.value instanceof RevertPlan)) {
                    return null;
                }
                return (RevertPlan) claim.value;
            }

            private void releaseRevertPlan(RevertPlan plan) {
                if (plan != null && !revertPlans.release(plan.token, plan)) {
                    latestGuardError = "invalid_revert_guard";
                }
            }

            private boolean completeRevertPlan(RevertPlan plan) {
                if (plan == null || !revertPlans.consume(plan.token, plan)) {
                    latestGuardError = "invalid_revert_guard";
                    return false;
                }
                clearPersistedRevertPlan(plan.token);
                latestGuardError = "";
                return true;
            }

            private String guardFailureJson(String id, String phase) {
                return "{"
                    + "\"ok\":false,"
                    + "\"id\":\"" + q(id) + "\","
                    + "\"phase\":\"" + q(phase) + "\","
                    + "\"error\":\"" + q(latestGuardError) + "\","
                    + "\"writeGuardRequired\":true,"
                    + "\"guardTtlMs\":" + WRITE_GUARD_TTL_MS
                    + "}";
            }

            private boolean listContainsString(List values, String requested) {
                if (values == null || requested == null) {
                    return false;
                }
                for (int i = 0; i < values.size(); i += 1) {
                    if (requested.equals(String.valueOf(values.get(i)))) {
                        return true;
                    }
                }
                return false;
            }

            private int parameterCount(String flattened) {
                if (flattened == null || flattened.length() == 0) {
                    return 0;
                }
                int count = 0;
                String[] pairs = flattened.split(";");
                for (int i = 0; i < pairs.length; i += 1) {
                    if (pairs[i].indexOf('=') > 0) {
                        count += 1;
                    }
                }
                return count;
            }

            private String parametersObjectJson(String flattened) {
                String json = "{";
                boolean first = true;
                if (flattened != null && flattened.length() > 0) {
                    String[] pairs = flattened.split(";");
                    for (int i = 0; i < pairs.length; i += 1) {
                        int eq = pairs[i].indexOf('=');
                        if (eq <= 0) {
                            continue;
                        }
                        if (!first) {
                            json += ",";
                        }
                        first = false;
                        String key = pairs[i].substring(0, eq);
                        String value = pairs[i].substring(eq + 1);
                        json += "\"" + q(key) + "\":\"" + q(value) + "\"";
                    }
                }
                return json + "}";
            }

            private String parametersDumpJson() {
                try {
                    if (!openCameraNow("parameters dump")) {
                        return "{\"ok\":false,\"id\":\"param.dump\",\"error\":\"camera_not_open\",\"latestError\":\"" + q(latestError) + "\"}";
                    }
                    Camera.Parameters params = camera.getParameters();
                    String flattened = params.flatten();
                    return "{"
                        + "\"ok\":true,"
                        + "\"id\":\"param.dump\","
                        + "\"readOnly\":true,"
                        + "\"safe\":true,"
                        + "\"parameterCount\":" + parameterCount(flattened) + ","
                        + "\"flattened\":\"" + q(flattened) + "\","
                        + "\"parameters\":" + parametersObjectJson(flattened)
                        + "}";
                } catch (Throwable t) {
                    latestError = "parameters dump: " + t.toString();
                    log(latestError);
                    return "{\"ok\":false,\"id\":\"param.dump\",\"error\":\"" + q(latestError) + "\"}";
                }
            }

            private String parameterValueJson(String id) {
                try {
                    if (!openCameraNow("parameter read")) {
                        return "{\"ok\":false,\"id\":\"" + q(id) + "\",\"error\":\"camera_not_open\",\"latestError\":\"" + q(latestError) + "\"}";
                    }
                    String suffix = id.substring("param.".length());
                    if (suffix.length() == 0 || "dump".equals(suffix)) {
                        return parametersDumpJson();
                    }
                    String key = suffix.replace('_', '-');
                    Camera.Parameters params = camera.getParameters();
                    String value = params.get(key);
                    if (value == null && !key.equals(suffix)) {
                        value = params.get(suffix);
                        key = suffix;
                    }
                    if (value == null) {
                        return "{"
                            + "\"ok\":false,"
                            + "\"id\":\"" + q(id) + "\","
                            + "\"key\":\"" + q(key) + "\","
                            + "\"error\":\"unknown_camera_parameter\","
                            + "\"readOnly\":true"
                            + "}";
                    }
                    return "{"
                        + "\"ok\":true,"
                        + "\"id\":\"" + q(id) + "\","
                        + "\"key\":\"" + q(key) + "\","
                        + "\"value\":\"" + q(value) + "\","
                        + "\"readOnly\":true,"
                        + "\"safe\":true"
                        + "}";
                } catch (Throwable t) {
                    latestError = "parameter read: " + t.toString();
                    log(latestError);
                    return "{\"ok\":false,\"id\":\"" + q(id) + "\",\"error\":\"" + q(latestError) + "\"}";
                }
            }

            private String selfTimerJson(String note) {
                try {
                    if (!openCameraNow("self timer read")) {
                        return "{\"ok\":false,\"id\":\"param.self_timer\",\"error\":\"camera_not_open\",\"latestError\":\"" + q(latestError) + "\"}";
                    }
                    Camera.Parameters params = camera.getParameters();
                    CameraEx.ParametersModifier modifier = cameraEx.createParametersModifier(params);
                    int current = modifier.getSelfTimer();
                    List supported = modifier.getSupportedSelfTimers();
                    return "{"
                        + "\"ok\":true,"
                        + "\"id\":\"param.self_timer\","
                        + "\"value\":" + current + ","
                        + "\"supported\":" + listJson(supported) + ","
                        + "\"readOnly\":false,"
                        + "\"note\":\"" + q(note) + "\""
                        + "}";
                } catch (Throwable t) {
                    latestError = "self timer read: " + t.toString();
                    log(latestError);
                    return "{\"ok\":false,\"id\":\"param.self_timer\",\"error\":\"" + q(latestError) + "\"}";
                }
            }

            private String setSelfTimerJson(String rawValue, String rawDryRun, String rawGuardToken) {
                if (rawValue == null || rawValue.length() == 0) {
                    return "{\"ok\":false,\"id\":\"param.self_timer\",\"error\":\"missing_value_query_param\"}";
                }
                try {
                    if (!openCameraNow("self timer set")) {
                        return "{\"ok\":false,\"id\":\"param.self_timer\",\"error\":\"camera_not_open\",\"latestError\":\"" + q(latestError) + "\"}";
                    }
                    int requested = Integer.parseInt(rawValue);
                    boolean dryRun = truthy(rawDryRun);
                    Camera.Parameters params = camera.getParameters();
                    CameraEx.ParametersModifier modifier = cameraEx.createParametersModifier(params);
                    int before = modifier.getSelfTimer();
                    List supported = modifier.getSupportedSelfTimers();
                    if (!listContainsString(supported, String.valueOf(requested))) {
                        return "{"
                            + "\"ok\":false,"
                            + "\"id\":\"param.self_timer\","
                            + "\"before\":" + before + ","
                            + "\"requested\":" + requested + ","
                            + "\"error\":\"unsupported_camera_parameter_value\","
                            + "\"supported\":" + listJson(supported) + ","
                            + "\"dry_run\":" + dryRun + ","
                            + "\"readOnly\":false,"
                            + "\"safe\":true"
                            + "}";
                    }
                    if (dryRun) {
                        if (hasPendingRecoveryPlan()) {
                            return pendingRecoveryFailureJson("param.self_timer");
                        }
                        String recoveryToken = issueRevertPlan(
                            "param.self_timer",
                            "self-timer",
                            String.valueOf(before),
                            String.valueOf(requested)
                        );
                        String guardToken = issueWriteGuard(
                            "param.self_timer",
                            "self-timer",
                            String.valueOf(before),
                            String.valueOf(requested),
                            recoveryToken
                        );
                        return "{"
                            + "\"ok\":true,"
                            + "\"id\":\"param.self_timer\","
                            + "\"before\":" + before + ","
                            + "\"requested\":" + requested + ","
                            + "\"after\":" + before + ","
                            + "\"dry_run\":true,"
                            + "\"wouldWrite\":true,"
                            + "\"writeGuardRequired\":true,"
                            + "\"guardToken\":\"" + q(guardToken) + "\","
                            + "\"guardExpiresAt\":" + (System.currentTimeMillis() + WRITE_GUARD_TTL_MS) + ","
                            + "\"recoveryToken\":\"" + q(recoveryToken) + "\","
                            + "\"recoveryExpiresAt\":" + (System.currentTimeMillis() + RECOVERY_TTL_MS) + ","
                            + "\"commitEndpoint\":\"POST /settings/param.self_timer?value=" + requested + "&guard_token=<token>\","
                            + "\"revertEndpoint\":\"POST /settings/param.self_timer/revert?revert_token=<recovery-token>\","
                            + "\"supported\":" + listJson(supported) + ","
                            + "\"readOnly\":false"
                            + "}";
                    }
                    PendingWrite writePlan = consumeWriteGuard(
                        rawGuardToken,
                        "param.self_timer",
                        "self-timer",
                        String.valueOf(before),
                        String.valueOf(requested)
                    );
                    if (writePlan == null) {
                        return guardFailureJson("param.self_timer", "commit");
                    }
                    modifier.setSelfTimer(requested);
                    camera.setParameters(params);
                    Camera.Parameters afterParams = camera.getParameters();
                    CameraEx.ParametersModifier afterModifier = cameraEx.createParametersModifier(afterParams);
                    int after = afterModifier.getSelfTimer();
                    boolean ok = after == requested;
                    String revertToken = writePlan.recoveryToken;
                    latestError = ok ? "" : "self timer set readback mismatch";
                    log("self timer " + before + " -> " + after);
                    return "{"
                        + "\"ok\":" + ok + ","
                        + "\"id\":\"param.self_timer\","
                        + "\"before\":" + before + ","
                        + "\"requested\":" + requested + ","
                        + "\"after\":" + after + ","
                        + "\"dry_run\":false,"
                        + "\"writeGuardConsumed\":true,"
                        + "\"revertToken\":\"" + q(revertToken) + "\","
                        + "\"revertEndpoint\":\"POST /settings/param.self_timer/revert?revert_token=<token>\","
                        + "\"supported\":" + listJson(supported) + ","
                        + "\"readOnly\":false"
                        + "}";
                } catch (Throwable t) {
                    latestError = "self timer set: " + t.toString();
                    log(latestError);
                    return "{\"ok\":false,\"id\":\"param.self_timer\",\"error\":\"" + q(latestError) + "\"}";
                }
            }

            private String allowlistedParamJson(String id, String note) {
                try {
                    String key = allowlistedParamKey(id);
                    if (key == null) {
                        return writeNotImplementedJson(id);
                    }
                    if (!openCameraNow("allowlisted parameter read")) {
                        return "{\"ok\":false,\"id\":\"" + q(id) + "\",\"error\":\"camera_not_open\",\"latestError\":\"" + q(latestError) + "\"}";
                    }
                    Camera.Parameters params = camera.getParameters();
                    String value = params.get(key);
                    String supported = params.get(key + "-values");
                    if (value == null) {
                        return "{"
                            + "\"ok\":false,"
                            + "\"id\":\"" + q(id) + "\","
                            + "\"key\":\"" + q(key) + "\","
                            + "\"registrySettingId\":\"" + q(registryIdForParamKey(key)) + "\","
                            + "\"error\":\"unknown_camera_parameter\","
                            + "\"readOnly\":false,"
                            + "\"safe\":true"
                            + "}";
                    }
                    return "{"
                        + "\"ok\":true,"
                        + "\"id\":\"" + q(id) + "\","
                        + "\"key\":\"" + q(key) + "\","
                        + "\"registrySettingId\":\"" + q(registryIdForParamKey(key)) + "\","
                        + "\"value\":\"" + q(value) + "\","
                        + "\"supported\":" + csvJson(supported) + ","
                        + "\"rawSupported\":\"" + q(supported) + "\","
                        + "\"readOnly\":false,"
                        + "\"safe\":true,"
                        + "\"note\":\"" + q(note) + "\""
                        + "}";
                } catch (Throwable t) {
                    latestError = "allowlisted parameter read: " + t.toString();
                    log(latestError);
                    return "{\"ok\":false,\"id\":\"" + q(id) + "\",\"error\":\"" + q(latestError) + "\"}";
                }
            }

            private String setAllowlistedParamJson(
                String id,
                String rawValue,
                String rawDryRun,
                String rawGuardToken
            ) {
                if (rawValue == null || rawValue.length() == 0) {
                    return "{\"ok\":false,\"id\":\"" + q(id) + "\",\"error\":\"missing_value_query_param\"}";
                }
                try {
                    String key = allowlistedParamKey(id);
                    if (key == null) {
                        return writeNotImplementedJson(id);
                    }
                    if (!openCameraNow("allowlisted parameter set")) {
                        return "{\"ok\":false,\"id\":\"" + q(id) + "\",\"error\":\"camera_not_open\",\"latestError\":\"" + q(latestError) + "\"}";
                    }
                    boolean dryRun = truthy(rawDryRun);
                    Camera.Parameters params = camera.getParameters();
                    String before = params.get(key);
                    String supported = params.get(key + "-values");
                    if (before == null) {
                        return "{"
                            + "\"ok\":false,"
                            + "\"id\":\"" + q(id) + "\","
                            + "\"key\":\"" + q(key) + "\","
                            + "\"registrySettingId\":\"" + q(registryIdForParamKey(key)) + "\","
                            + "\"error\":\"unknown_camera_parameter\","
                            + "\"readOnly\":false,"
                            + "\"safe\":true"
                            + "}";
                    }
                    if (supported == null || supported.length() == 0) {
                        return "{"
                            + "\"ok\":false,"
                            + "\"id\":\"" + q(id) + "\","
                            + "\"key\":\"" + q(key) + "\","
                            + "\"registrySettingId\":\"" + q(registryIdForParamKey(key)) + "\","
                            + "\"before\":\"" + q(before) + "\","
                            + "\"requested\":\"" + q(rawValue) + "\","
                            + "\"error\":\"missing_supported_values\","
                            + "\"dry_run\":" + dryRun + ","
                            + "\"readOnly\":false,"
                            + "\"safe\":true"
                            + "}";
                    }
                    if (!csvContains(supported, rawValue)) {
                        return "{"
                            + "\"ok\":false,"
                            + "\"id\":\"" + q(id) + "\","
                            + "\"key\":\"" + q(key) + "\","
                            + "\"registrySettingId\":\"" + q(registryIdForParamKey(key)) + "\","
                            + "\"before\":\"" + q(before) + "\","
                            + "\"requested\":\"" + q(rawValue) + "\","
                            + "\"error\":\"unsupported_camera_parameter_value\","
                            + "\"supported\":" + csvJson(supported) + ","
                            + "\"dry_run\":" + dryRun + ","
                            + "\"readOnly\":false,"
                            + "\"safe\":true"
                            + "}";
                    }
                    if (dryRun) {
                        if (hasPendingRecoveryPlan()) {
                            return pendingRecoveryFailureJson(id);
                        }
                        String recoveryToken = issueRevertPlan(id, key, before, rawValue);
                        String guardToken = issueWriteGuard(
                            id,
                            key,
                            before,
                            rawValue,
                            recoveryToken
                        );
                        return "{"
                            + "\"ok\":true,"
                            + "\"id\":\"" + q(id) + "\","
                            + "\"key\":\"" + q(key) + "\","
                            + "\"registrySettingId\":\"" + q(registryIdForParamKey(key)) + "\","
                            + "\"before\":\"" + q(before) + "\","
                            + "\"requested\":\"" + q(rawValue) + "\","
                            + "\"after\":\"" + q(before) + "\","
                            + "\"dry_run\":true,"
                            + "\"wouldWrite\":true,"
                            + "\"writeGuardRequired\":true,"
                            + "\"guardToken\":\"" + q(guardToken) + "\","
                            + "\"guardExpiresAt\":" + (System.currentTimeMillis() + WRITE_GUARD_TTL_MS) + ","
                            + "\"recoveryToken\":\"" + q(recoveryToken) + "\","
                            + "\"recoveryExpiresAt\":" + (System.currentTimeMillis() + RECOVERY_TTL_MS) + ","
                            + "\"commitEndpoint\":\"POST /settings/" + q(id) + "?value=" + q(rawValue) + "&guard_token=<token>\","
                            + "\"revertEndpoint\":\"POST /settings/" + q(id) + "/revert?revert_token=<recovery-token>\","
                            + "\"supported\":" + csvJson(supported) + ","
                            + "\"readOnly\":false,"
                            + "\"safe\":true"
                            + "}";
                    }
                    PendingWrite writePlan = consumeWriteGuard(
                        rawGuardToken,
                        id,
                        key,
                        before,
                        rawValue
                    );
                    if (writePlan == null) {
                        return guardFailureJson(id, "commit");
                    }
                    params.set(key, rawValue);
                    camera.setParameters(params);
                    String after = camera.getParameters().get(key);
                    boolean ok = rawValue.equals(after);
                    String revertToken = writePlan.recoveryToken;
                    latestError = ok ? "" : "allowlisted parameter set readback mismatch";
                    log(key + " " + before + " -> " + after);
                    return "{"
                        + "\"ok\":" + ok + ","
                        + "\"id\":\"" + q(id) + "\","
                        + "\"key\":\"" + q(key) + "\","
                        + "\"registrySettingId\":\"" + q(registryIdForParamKey(key)) + "\","
                        + "\"before\":\"" + q(before) + "\","
                        + "\"requested\":\"" + q(rawValue) + "\","
                        + "\"after\":\"" + q(after) + "\","
                        + "\"dry_run\":false,"
                        + "\"writeGuardConsumed\":true,"
                        + "\"revertToken\":\"" + q(revertToken) + "\","
                        + "\"revertEndpoint\":\"POST /settings/" + q(id) + "/revert?revert_token=<token>\","
                        + "\"supported\":" + csvJson(supported) + ","
                        + "\"readOnly\":false,"
                        + "\"safe\":true"
                        + "}";
                } catch (Throwable t) {
                    latestError = "allowlisted parameter set: " + t.toString();
                    log(latestError);
                    return "{\"ok\":false,\"id\":\"" + q(id) + "\",\"error\":\"" + q(latestError) + "\"}";
                }
            }

            private String revertSettingJson(String id, String rawRevertToken) {
                if (destructiveSettingId(id)) {
                    return destructiveWriteForbiddenJson(id);
                }
                RevertPlan plan = beginRevertPlan(rawRevertToken, id);
                if (plan == null) {
                    return guardFailureJson(id, "revert");
                }
                try {
                    if (!openCameraNow("guarded revert")) {
                        releaseRevertPlan(plan);
                        return "{"
                            + "\"ok\":false,"
                            + "\"id\":\"" + q(id) + "\","
                            + "\"phase\":\"revert\","
                            + "\"error\":\"camera_not_open\","
                            + "\"latestError\":\"" + q(latestError) + "\","
                            + "\"revertGuardConsumed\":false,"
                            + "\"revertRetryable\":true"
                            + "}";
                    }
                    if ("param.self_timer".equals(id)) {
                        Camera.Parameters params = camera.getParameters();
                        CameraEx.ParametersModifier modifier = cameraEx.createParametersModifier(params);
                        int current = modifier.getSelfTimer();
                        int original = Integer.parseInt(plan.original);
                        if (current == original) {
                            boolean consumed = completeRevertPlan(plan);
                            return "{"
                                + "\"ok\":" + consumed + ","
                                + "\"id\":\"" + q(id) + "\","
                                + "\"before\":" + current + ","
                                + "\"restored\":" + original + ","
                                + "\"after\":" + current + ","
                                + "\"alreadyRestored\":true,"
                                + "\"revertGuardConsumed\":" + consumed + ","
                                + "\"revertRetryable\":false,"
                                + "\"safe\":true"
                                + "}";
                        }
                        if (!String.valueOf(current).equals(plan.written)) {
                            latestGuardError = "stale_revert_target";
                            releaseRevertPlan(plan);
                            return "{"
                                + "\"ok\":false,"
                                + "\"id\":\"" + q(id) + "\","
                                + "\"phase\":\"revert\","
                                + "\"error\":\"stale_revert_target\","
                                + "\"expectedCurrent\":\"" + q(plan.written) + "\","
                                + "\"actualCurrent\":\"" + current + "\","
                                + "\"revertGuardConsumed\":false,"
                                + "\"revertRetryable\":true"
                                + "}";
                        }
                        List supported = modifier.getSupportedSelfTimers();
                        if (!listContainsString(supported, plan.original)) {
                            latestGuardError = "original_value_no_longer_supported";
                            releaseRevertPlan(plan);
                            return "{"
                                + "\"ok\":false,"
                                + "\"id\":\"" + q(id) + "\","
                                + "\"phase\":\"revert\","
                                + "\"error\":\"original_value_no_longer_supported\","
                                + "\"original\":" + original + ","
                                + "\"supported\":" + listJson(supported) + ","
                                + "\"revertGuardConsumed\":false,"
                                + "\"revertRetryable\":true"
                                + "}";
                        }
                        modifier.setSelfTimer(original);
                        camera.setParameters(params);
                        CameraEx.ParametersModifier afterModifier =
                            cameraEx.createParametersModifier(camera.getParameters());
                        int after = afterModifier.getSelfTimer();
                        boolean ok = after == original;
                        latestGuardError = ok ? "" : "revert_readback_mismatch";
                        log("self timer revert " + current + " -> " + after);
                        boolean consumed = false;
                        if (ok) {
                            consumed = completeRevertPlan(plan);
                            ok = consumed;
                        } else {
                            releaseRevertPlan(plan);
                        }
                        return "{"
                            + "\"ok\":" + ok + ","
                            + "\"id\":\"" + q(id) + "\","
                            + "\"before\":" + current + ","
                            + "\"restored\":" + original + ","
                            + "\"after\":" + after + ","
                            + "\"revertGuardConsumed\":" + consumed + ","
                            + "\"revertRetryable\":" + (!consumed) + ","
                            + "\"safe\":true"
                            + "}";
                    }

                    String key = allowlistedParamKey(id);
                    if (key == null || !key.equals(plan.key)) {
                        latestGuardError = "setting_no_longer_allowlisted";
                        releaseRevertPlan(plan);
                        return guardFailureJson(id, "revert");
                    }
                    Camera.Parameters params = camera.getParameters();
                    String current = params.get(key);
                    if (plan.original.equals(current)) {
                        boolean consumed = completeRevertPlan(plan);
                        return "{"
                            + "\"ok\":" + consumed + ","
                            + "\"id\":\"" + q(id) + "\","
                            + "\"key\":\"" + q(key) + "\","
                            + "\"registrySettingId\":\"" + q(registryIdForParamKey(key)) + "\","
                            + "\"before\":\"" + q(current) + "\","
                            + "\"restored\":\"" + q(plan.original) + "\","
                            + "\"after\":\"" + q(current) + "\","
                            + "\"alreadyRestored\":true,"
                            + "\"revertGuardConsumed\":" + consumed + ","
                            + "\"revertRetryable\":false,"
                            + "\"safe\":true"
                            + "}";
                    }
                    if (current == null || !current.equals(plan.written)) {
                        latestGuardError = "stale_revert_target";
                        releaseRevertPlan(plan);
                        return "{"
                            + "\"ok\":false,"
                            + "\"id\":\"" + q(id) + "\","
                            + "\"key\":\"" + q(key) + "\","
                            + "\"phase\":\"revert\","
                            + "\"error\":\"stale_revert_target\","
                            + "\"expectedCurrent\":\"" + q(plan.written) + "\","
                            + "\"actualCurrent\":\"" + q(current) + "\","
                            + "\"revertGuardConsumed\":false,"
                            + "\"revertRetryable\":true"
                            + "}";
                    }
                    String supported = params.get(key + "-values");
                    if (supported == null || !csvContains(supported, plan.original)) {
                        latestGuardError = "original_value_no_longer_supported";
                        releaseRevertPlan(plan);
                        return "{"
                            + "\"ok\":false,"
                            + "\"id\":\"" + q(id) + "\","
                            + "\"key\":\"" + q(key) + "\","
                            + "\"phase\":\"revert\","
                            + "\"error\":\"original_value_no_longer_supported\","
                            + "\"original\":\"" + q(plan.original) + "\","
                            + "\"supported\":" + csvJson(supported) + ","
                            + "\"revertGuardConsumed\":false,"
                            + "\"revertRetryable\":true"
                            + "}";
                    }
                    params.set(key, plan.original);
                    camera.setParameters(params);
                    String after = camera.getParameters().get(key);
                    boolean ok = plan.original.equals(after);
                    latestGuardError = ok ? "" : "revert_readback_mismatch";
                    log(key + " revert " + current + " -> " + after);
                    boolean consumed = false;
                    if (ok) {
                        consumed = completeRevertPlan(plan);
                        ok = consumed;
                    } else {
                        releaseRevertPlan(plan);
                    }
                    return "{"
                        + "\"ok\":" + ok + ","
                        + "\"id\":\"" + q(id) + "\","
                        + "\"key\":\"" + q(key) + "\","
                        + "\"registrySettingId\":\"" + q(registryIdForParamKey(key)) + "\","
                        + "\"before\":\"" + q(current) + "\","
                        + "\"restored\":\"" + q(plan.original) + "\","
                        + "\"after\":\"" + q(after) + "\","
                        + "\"revertGuardConsumed\":" + consumed + ","
                        + "\"revertRetryable\":" + (!consumed) + ","
                        + "\"safe\":true"
                        + "}";
                } catch (Throwable t) {
                    releaseRevertPlan(plan);
                    latestGuardError = "guarded revert: " + t.toString();
                    latestError = latestGuardError;
                    log(latestError);
                    return "{"
                        + "\"ok\":false,"
                        + "\"id\":\"" + q(id) + "\","
                        + "\"phase\":\"revert\","
                        + "\"error\":\"" + q(latestGuardError) + "\","
                        + "\"revertGuardConsumed\":false,"
                        + "\"revertRetryable\":true"
                        + "}";
                }
            }

            private String settingJson(String id) {
                if ("device.model".equals(id)) {
                    String model = "";
                    try {
                        model = DeviceInfo.getInstance().getModel();
                    } catch (Throwable ignored) {
                    }
                    return "{\"ok\":true,\"id\":\"device.model\",\"value\":\"" + q(model) + "\",\"readOnly\":true}";
                }
                if ("preview.running".equals(id)) {
                    return "{\"ok\":true,\"id\":\"preview.running\",\"value\":" + previewRunning + ",\"readOnly\":true}";
                }
                if ("param.self_timer".equals(id)) {
                    return selfTimerJson("read");
                }
                if ("param.dump".equals(id)) {
                    return parametersDumpJson();
                }
                if (allowlistedParamKey(id) != null) {
                    return allowlistedParamJson(id, "read");
                }
                if (id.startsWith("param.")) {
                    return parameterValueJson(id);
                }
                if ("focus.af".equals(id)) {
                    return "{\"ok\":true,\"id\":\"focus.af\",\"value\":\"action\",\"readOnly\":true}";
                }
                if ("capture.latest".equals(id)) {
                    return "{\"ok\":true,\"id\":\"capture.latest\",\"value\":\"" + q(latestCapture) + "\",\"readOnly\":true}";
                }
                if ("capture.count".equals(id)) {
                    return "{\"ok\":true,\"id\":\"capture.count\",\"value\":" + captureCount + ",\"readOnly\":true}";
                }
                if ("capture.bytes".equals(id)) {
                    return "{\"ok\":true,\"id\":\"capture.bytes\",\"value\":" + latestCaptureBytes + ",\"readOnly\":true}";
                }
                if ("error.latest".equals(id)) {
                    return "{\"ok\":true,\"id\":\"error.latest\",\"value\":\"" + q(latestError) + "\",\"readOnly\":true}";
                }
                return "{\"ok\":false,\"error\":\"unknown setting\",\"id\":\"" + q(id) + "\"}";
            }

            private class ProofServer extends NanoHTTPD {
                ProofServer() {
                    super(PORT);
                }

                private boolean isMethod(IHTTPSession session, String expected) {
                    return expected.equals(String.valueOf(session.getMethod()));
                }

                private Response methodNotAllowed(String expected) {
                    return new Response(
                        Response.Status.METHOD_NOT_ALLOWED,
                        "application/json",
                        "{\"schemaVersion\":" + API_SCHEMA_VERSION
                            + ",\"appVersion\":\"" + APP_VERSION
                            + "\",\"ok\":false,\"error\":\"method_not_allowed\",\"expected\":\""
                            + expected + "\"}"
                    );
                }

                @Override
                public Response serve(IHTTPSession session) {
                    String uri = session.getUri();
                    if ("/health".equals(uri)) {
                        if (!isMethod(session, "GET")) {
                            return methodNotAllowed("GET");
                        }
                        return new Response(Response.Status.OK, "application/json", healthJson());
                    }
                    if ("/capabilities".equals(uri)) {
                        if (!isMethod(session, "GET")) {
                            return methodNotAllowed("GET");
                        }
                        return new Response(Response.Status.OK, "application/json", capabilitiesJson());
                    }
                    if ("/diagnostics".equals(uri)) {
                        if (!isMethod(session, "GET")) {
                            return methodNotAllowed("GET");
                        }
                        return new Response(Response.Status.OK, "application/json", diagnosticsJson());
                    }
                    if ("/settings".equals(uri)) {
                        if (!isMethod(session, "GET")) {
                            return methodNotAllowed("GET");
                        }
                        return new Response(Response.Status.OK, "application/json", settingsJson());
                    }
                    if ("/parameters".equals(uri)) {
                        if (!isMethod(session, "GET")) {
                            return methodNotAllowed("GET");
                        }
                        return new Response(Response.Status.OK, "application/json", parametersDumpJson());
                    }
                    if (uri.startsWith("/settings/") && uri.endsWith("/revert")) {
                        if (!isMethod(session, "POST")) {
                            return methodNotAllowed("POST");
                        }
                        String settingId = uri.substring(
                            "/settings/".length(),
                            uri.length() - "/revert".length()
                        );
                        if (destructiveSettingId(settingId)) {
                            return new Response(
                                Response.Status.FORBIDDEN,
                                "application/json",
                                destructiveWriteForbiddenJson(settingId)
                            );
                        }
                        return new Response(
                            Response.Status.OK,
                            "application/json",
                            revertSettingJson(settingId, session.getParms().get("revert_token"))
                        );
                    }
                    if ("/settings/param.self_timer".equals(uri) && isMethod(session, "POST")) {
                        return new Response(
                            Response.Status.OK,
                            "application/json",
                            setSelfTimerJson(
                                session.getParms().get("value"),
                                session.getParms().get("dry_run"),
                                session.getParms().get("guard_token")
                            )
                        );
                    }
                    if (uri.startsWith("/settings/")) {
                        String settingId = uri.substring("/settings/".length());
                        if (isMethod(session, "POST")) {
                            if (destructiveSettingId(settingId)) {
                                return new Response(
                                    Response.Status.FORBIDDEN,
                                    "application/json",
                                    destructiveWriteForbiddenJson(settingId)
                                );
                            }
                            if (allowlistedParamKey(settingId) != null) {
                                return new Response(
                                    Response.Status.OK,
                                    "application/json",
                                    setAllowlistedParamJson(
                                        settingId,
                                        session.getParms().get("value"),
                                        session.getParms().get("dry_run"),
                                        session.getParms().get("guard_token")
                                    )
                                );
                            }
                            return new Response(
                                Response.Status.BAD_REQUEST,
                                "application/json",
                                writeNotImplementedJson(settingId)
                            );
                        }
                        if (!isMethod(session, "GET")) {
                            return methodNotAllowed("GET");
                        }
                        return new Response(
                            Response.Status.OK,
                            "application/json",
                            settingJson(settingId)
                        );
                    }
                    if ("/preview".equals(uri) || "/preview/start".equals(uri)) {
                        if (!isMethod(session, "POST")) {
                            return methodNotAllowed("POST");
                        }
                        startPreviewOnUiThread();
                        return new Response(Response.Status.OK, "application/json", statusJson());
                    }
                    if ("/focus".equals(uri) || "/focus/af".equals(uri)) {
                        if (!isMethod(session, "POST")) {
                            return methodNotAllowed("POST");
                        }
                        autoFocusOnUiThread();
                        return new Response(Response.Status.OK, "application/json", "{\"ok\":true,\"accepted\":true,\"action\":\"autofocus\",\"completion\":\"observe callback or visual focus proof\"}");
                    }
                    if ("/capture".equals(uri)) {
                        if (!isMethod(session, "POST")) {
                            return methodNotAllowed("POST");
                        }
                        captureOnUiThread();
                        return new Response(Response.Status.OK, "application/json", "{\"ok\":true,\"accepted\":true,\"captureCountBefore\":" + captureCount + ",\"completion\":\"poll GET /media/latest\"}");
                    }
                    if ("/rollback".equals(uri) || "/diagnostics/rollback".equals(uri)) {
                        if (!isMethod(session, "GET")) {
                            return methodNotAllowed("GET");
                        }
                        return new Response(Response.Status.OK, "application/json", rollbackJson());
                    }
                    if ("/exit".equals(uri) || "/shutdown".equals(uri)) {
                        if (!isMethod(session, "POST")) {
                            return methodNotAllowed("POST");
                        }
                        closeProofAppDelayed("/exit".equals(uri) ? "http_exit" : "http_shutdown", 350);
                        return new Response(Response.Status.OK, "application/json", "{\"schemaVersion\":" + API_SCHEMA_VERSION + ",\"appVersion\":\"" + APP_VERSION + "\",\"ok\":true,\"accepted\":true,\"action\":\"exit\",\"rollback\":\"GET /rollback\",\"expectedPort8080State\":\"closed_after_response\"}");
                    }
                    if ("/media/latest".equals(uri)) {
                        if (!isMethod(session, "GET")) {
                            return methodNotAllowed("GET");
                        }
                        return new Response(Response.Status.OK, "application/json", statusJson());
                    }
                    return new Response(
                        Response.Status.NOT_FOUND,
                        "application/json",
                        "{\"schemaVersion\":" + API_SCHEMA_VERSION
                            + ",\"appVersion\":\"" + APP_VERSION
                            + "\",\"ok\":false,\"error\":\"endpoint_not_found\",\"uri\":\""
                            + q(uri) + "\"}"
                    );
                }
            }
        }
        ''',
    )
    write(
        app / "README.md",
        """
        # Route 3 OpenMemories Proof App vNext

        This generated project is a clean-room proof scaffold for the A7 II falsification work.
        API schema version 2 uses one-time tokens for every safe setting commit and revert.
        No formatting, deletion, reset, initialization, cleaning, firmware, or service-opcode
        operation is implemented.

        It exposes:

        - `GET /health`
        - `GET /capabilities`
        - `GET /diagnostics`
        - `GET /settings`
        - `GET /settings/{id}`
        - `GET /settings/param.dump`
        - `GET /parameters`
        - `POST /settings/param.self_timer?value=<seconds>&dry_run=true`
        - `POST /settings/param.iso_sensitivity?value=<camera-value>&dry_run=true`
        - `POST /settings/param.image_aspect?value=<camera-value>&dry_run=true`
        - `POST /settings/param.image_size?value=<camera-value>&dry_run=true`
        - `POST /settings/param.metering_mode?value=<camera-value>&dry_run=true`
        - `POST /settings/param.dro_mode?value=<camera-value>&dry_run=true`
        - `POST /settings/param.color_mode?value=<camera-value>&dry_run=true`
        - `POST /settings/param.picture_effect?value=<camera-value>&dry_run=true`
        - `POST /settings/param.face_detection_mode?value=<camera-value>&dry_run=true`
        - `POST /settings/param.flash_mode?value=<camera-value>&dry_run=true`
        - `POST /settings/param.red_eye_reduction_mode?value=<camera-value>&dry_run=true`
        - `POST /settings/param.color_space?value=<camera-value>&dry_run=true`
        - `POST /settings/param.apsc_mode?value=<camera-value>&dry_run=true`
        - `POST /settings/param.raw_rec_mode?value=<camera-value>&dry_run=true`
        - `POST /settings/param.drive_mode?value=<camera-value>&dry_run=true`
        - `POST /settings/param.soft_skin_effect?value=<camera-value>&dry_run=true`
        - `POST /settings/param.focus_mode?value=<camera-value>&dry_run=true`
        - `POST /preview`
        - `POST /focus`
        - `POST /capture`
        - `POST /exit`
        - `POST /shutdown`
        - `GET /rollback`
        - `GET /diagnostics/rollback`
        - `GET /media/latest`

        Compatibility aliases are also exposed for older proof notes:

        - `POST /preview/start`
        - `POST /focus/af`
        - `POST /shutdown`

        Build target follows PMCADemo's old camera-compatible Android shape:

        ```bash
        gradle assembleDebug
        $TOOLS_DIR/Sony-PMCA-RE/.venv/bin/python $TOOLS_DIR/Sony-PMCA-RE/pmca-console.py install -f app/build/outputs/apk/debug/app-debug.apk
        ```

        ## Guarded write and revert protocol

        A setting can only be committed after a matching dry-run. The one-time guard token
        expires after five minutes and is bound to the setting ID, requested value, and
        observed before value.

        ```bash
        # 1. Read the current state and supported values.
        curl 'http://<camera-ip>:8080/settings/param.iso_sensitivity'

        # 2. Dry-run the exact requested value; save guardToken and recoveryToken.
        curl -X POST \
          'http://<camera-ip>:8080/settings/param.iso_sensitivity?value=800&dry_run=true'

        # 3. Commit the same value with that one-time token. The returned revertToken
        #    is the same durable recovery token minted before mutation.
        curl -X POST \
          'http://<camera-ip>:8080/settings/param.iso_sensitivity?value=800&guard_token=<token>'

        # 4. Read back, then restore the original value with the one-time revert token.
        curl 'http://<camera-ip>:8080/settings/param.iso_sensitivity'
        curl -X POST \
          'http://<camera-ip>:8080/settings/param.iso_sensitivity/revert?revert_token=<token>'
        ```

        The recovery token is persisted on-camera before mutation and survives activity pause,
        exit, or process restart for seven days. A second dry-run is blocked until the outstanding
        recovery plan is consumed, including a no-op recovery after a dry-run-only inspection.

        A stale camera value, changed requested value, reused token, expired token, unsupported
        value, non-allowlisted parameter, or destructive setting ID is rejected without writing.
        A revert token is claimed while restoration is in flight. Camera-open, apply, support,
        stale-target, exception, and readback-verification failures release that claim so the same
        token can be retried. The token is consumed only after the original value is read back, or
        a retry verifies that the camera already reached the original value. Reuse after verified
        restoration is rejected.

        ## Exit and rollback

        The on-screen `Exit Route3 Proof` button, the camera Back button, the camera Menu button,
        `POST /exit`, and `POST /shutdown` all converge on one idempotent cleanup path. Cleanup
        stops preview, releases CameraEx, closes port 8080, and clears uncommitted write guards.
        A durable recovery plan is intentionally preserved until verified restoration consumes it.
        `GET /diagnostics` shows lifecycle counters and current in-process state.
        `GET /rollback` explains the expected post-exit state. Ports 23 and 5555 belong to
        OpenMemories Tweak, so their final state must still be checked externally after disabling
        Telnet and ADB.

        Proof bar:

        - PMCA install log saved.
        - App launches on camera.
        - Mac can `curl http://<camera-ip>:8080/health`.
        - Mac can `curl http://<camera-ip>:8080/capabilities`.
        - Mac can `curl http://<camera-ip>:8080/diagnostics`.
        - Mac can `curl http://<camera-ip>:8080/settings`.
        - Mac can `curl http://<camera-ip>:8080/settings/preview.running`.
        - Mac can `curl http://<camera-ip>:8080/parameters` for a read-only Camera.Parameters dump.
        - Mac can `curl http://<camera-ip>:8080/settings/param.dump` for the registry-shaped dump row.
        - Mac can dry-run, token-commit, read back, token-revert, and re-read an allowlisted safe write:
          `curl -X POST 'http://<camera-ip>:8080/settings/param.iso_sensitivity?value=800&dry_run=true'`.
        - Mac can dry-run aspect changes, for example:
          `curl -X POST 'http://<camera-ip>:8080/settings/param.image_aspect?value=16:9&dry_run=true'`.
        - Mac can dry-run RAW compression changes, for example:
          `curl -X POST 'http://<camera-ip>:8080/settings/param.raw_rec_mode?value=compress&dry_run=true'`.
        - Mac can `curl http://<camera-ip>:8080/rollback` before shutdown.
        - `/capture` produces a new file on the camera SD card.
        - On-screen Exit, Back, Menu, `/exit`, and `/shutdown` each close the proof activity without power-cycling.
        - Repeated resume/pause or exit calls do not duplicate the server or camera resources.
        - Rollback scan confirms ADB/telnet/proof HTTP are off when disabled.
        """,
    )
    return {
        "generated": True,
        "path": str(app),
        "endpoints": [
            "GET /health",
            "GET /capabilities",
            "GET /diagnostics",
            "GET /settings",
            "GET /settings/{id}",
            "GET /settings/param.dump",
            "GET /parameters",
            "POST /settings/{id}?value=<value>&dry_run=true",
            "POST /settings/{id}?value=<value>&guard_token=<token>",
            "POST /settings/{id}/revert?revert_token=<token>",
            "POST /preview",
            "POST /focus",
            "POST /capture",
            "POST /exit",
            "POST /shutdown",
            "GET /rollback",
            "GET /diagnostics/rollback",
            "GET /media/latest",
        ],
    }


_disabled_legacy_generate_proof_app = None


def generate_proof_app(root: Path) -> dict:
    """Disable the unauthenticated OpenMemories physical-control proof app permanently.

    The old generated NanoHTTPD app cannot meet the pairing, TLS, lifecycle, and bounded
    capture guarantees required for a camera-hosted server. Keeping it unavailable is safer
    than presenting a demo artifact as a supported control path.
    """
    del root
    raise RuntimeError(
        "OpenMemories Route 3 generation is disabled: no authenticated, bounded camera-hosted "
        "control service is supported"
    )


def build_markdown(report: dict) -> str:
    lines = [
        "# OpenMemories Route 3 Preflight",
        "",
        f"Date: {report['timestamp_human']}",
        "",
        "This artifact records whether the current Mac/camera state can build and run a clean-room",
        "OpenMemories proof app. It is not proof that Route 3 works until an APK is built, installed,",
        "launched, and hit over HTTP on the camera.",
        "",
        "## Tooling",
        "",
        "| Tool | Status | Path | Note |",
        "|---|---:|---|---|",
    ]
    for item in report["commands"]:
        note = ""
        probe = item.get("probe")
        if probe:
            note = f"exit={probe.get('returncode')} duration={probe.get('duration_s')}s"
        lines.append(f"| `{item['command']}` | {item['status']} | `{item.get('path') or ''}` | {note} |")

    lines.extend(["", "## Android SDK", ""])
    for sdk in report["android_sdk"]["candidates"]:
        lines.append(
            f"- `{sdk['path']}` exists={sdk['exists']} android-10={sdk['platform_10']} "
            f"build-tools-25.0.2={sdk['build_tools_25_0_2']}"
        )

    lines.extend(["", "## Java", ""])
    for item in report["java"]["candidates"]:
        version = " / ".join(line.strip() for line in (item.get("version") or [])).strip()
        lines.append(f"- `{item['path']}` exists={item['exists']} java8_like={item['java8_like']} {version}")

    lines.extend(["", "## Reference Sources", "", "| Source | Exists | Path |", "|---|---:|---|"])
    for item in report["paths"]:
        lines.append(f"| {item['name']} | {item['exists']} | `{item['path']}` |")

    lines.extend(["", "## Camera Ports", "", "| Host | Port | Open | Error |", "|---|---:|---:|---|"])
    for item in report["ports"]:
        lines.append(f"| {item['host']} | {item['port']} | {item['open']} | {item.get('error') or ''} |")

    if report.get("adb"):
        lines.extend(["", "## ADB", "", "```text", report["adb"].get("output", "").strip(), "```"])

    lines.extend(["", "## Build Blockers", ""])
    for blocker in report["build_blockers"]:
        lines.append(f"- {blocker}")
    if not report["build_blockers"]:
        lines.append("- None detected by preflight.")

    if report.get("reference_warnings"):
        lines.extend(["", "## Reference Warnings", ""])
        for warning in report["reference_warnings"]:
            lines.append(f"- {warning}")

    if report.get("generated_app"):
        lines.extend(["", "## Generated Proof App", "", f"- Path: `{report['generated_app']['path']}`"])

    if report.get("build"):
        build = report["build"]
        lines.extend(
            [
                "",
                "## Build Attempt",
                "",
                f"- Command: `{' '.join(build.get('cmd', []))}`",
                f"- ok={build.get('ok')} returncode={build.get('returncode')} duration={build.get('duration_s')}s",
            ]
        )
        output = (build.get("output") or "").strip()
        if output:
            lines.extend(["", "```text", output[-2400:], "```"])

    lines.extend(
        [
            "",
            "## Pass Bar",
            "",
            "Route 3 stays `NEEDS-TOOLING` while the Android build stack is missing, and it stays",
            "`NEEDS-BUILD-FIX` if `--try-build` cannot produce an APK. After a generated or equivalent",
            "clean-room app is built, it stays unproven until the app is installed by PMCA, launched on",
            "the A7 II, and answers `/health`, `/settings`, and one camera action endpoint.",
            "",
        ]
    )
    return "\n".join(lines)


def main(argv: list[str]) -> int:
    # Route 3 is permanently tombstoned.  Do not parse output locations or
    # resolve/probe tools before returning the fixed fail-closed result.
    print("OpenMemories Route 3 is permanently disabled.", file=sys.stderr)
    return 2
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out-dir", default="artifacts/falsification/r3_openmemories")
    parser.add_argument("--host", action="append", default=[],
                        help="candidate camera host to probe; defaults to CAMERA_LAN_HOST plus the A7 II AP")
    parser.add_argument("--port", action="append", type=int, default=[],
                        help="port to probe; defaults to 23, 5555, 8080")
    parser.add_argument("--connect-timeout", type=float, default=0.75)
    parser.add_argument("--adb-host", default="",
                        help="optional host:port for read-only adb connect/probe, e.g. <camera-ip>:5555")
    parser.add_argument("--generate-app", action="store_true",
                        help="generate a clean-room proof app skeleton into the artifact folder")
    parser.add_argument("--try-build", action="store_true",
                        help="after --generate-app, run gradle assembleDebug and save the output")
    args = parser.parse_args(argv)

    if args.generate_app or args.try_build:
        parser.error(
            "OpenMemories proof-app generation/build is permanently disabled because it would expose "
            "unauthenticated physical camera controls"
        )

    stamp = time.strftime("%Y%m%d-%H%M%S")
    out_dir = Path(args.out_dir) / f"{stamp}-route3-preflight"
    out_dir.mkdir(parents=True, exist_ok=True)

    hosts = tuple(args.host) if args.host else DEFAULT_HOSTS
    ports = tuple(args.port) if args.port else DEFAULT_PORTS

    paths = [path_probe(name, path) for name, path in REFERENCE_PATHS.items()]
    gradle_facts = {
        "PMCADemo": extract_gradle_facts(REFERENCE_PATHS["PMCADemo"]),
        "OpenMemories-Tweak": extract_gradle_facts(REFERENCE_PATHS["OpenMemories-Tweak"]),
    }
    android_sdk = android_sdk_probe()
    java = java_probe()
    gradle = gradle_probe(java_home=_selected_java_home(java))
    commands = [
        command_probe("adb", ["version"]),
        gradle,
        command_probe("java", ["-version"]),
        command_probe("sdkmanager", ["--version"]),
        pmca_probe(),
    ]
    ports_report = [socket_probe(host, port, args.connect_timeout) for host in hosts for port in ports]

    adb_report = None
    if args.adb_host:
        adb_report = run(["adb", "connect", args.adb_host], timeout=8)
        adb_devices = run(["adb", "devices", "-l"], timeout=8)
        adb_report["devices"] = adb_devices

    build_blockers = []
    command_status = {item["command"]: item["status"] for item in commands}
    if command_status.get("gradle") != "PRESENT":
        build_blockers.append("gradle is missing or failed; cannot run the old Android build locally yet.")
    if not any(sdk["platform_10"] for sdk in android_sdk["candidates"]):
        build_blockers.append("Android SDK platform android-10 is missing; PMCADemo-style camera apps compileSdkVersion 10.")
    if not any(sdk["build_tools_25_0_2"] for sdk in android_sdk["candidates"]):
        build_blockers.append("Android build-tools 25.0.2 is missing; PMCADemo references that exact buildToolsVersion.")
    if not java["has_java8_like"]:
        build_blockers.append(
            "No Java 8-compatible JDK detected; the PMCADemo-era Android Gradle plugin is likely to need Java 8."
        )
    reference_warnings = []
    for ref in ("OpenMemories-Framework", "PMCADemo"):
        if not REFERENCE_PATHS[ref].exists():
            reference_warnings.append(
                f"{ref} source is missing at {REFERENCE_PATHS[ref]}; build can still pass from cached/JitPack dependencies."
            )

    generated_app = generate_proof_app(out_dir) if args.generate_app else None
    build_report = None
    if args.try_build:
        if not generated_app:
            build_report = {"ok": False, "output": "--try-build requires --generate-app"}
        elif gradle.get("status") != "PRESENT" or not gradle.get("path"):
            build_report = {"ok": False, "output": "No compatible gradle command selected"}
        else:
            build_report = run(
                [str(gradle["path"]), "assembleDebug"],
                cwd=Path(generated_app["path"]),
                timeout=180,
                env=build_environment(android_sdk, java),
            )
            (out_dir / "gradle-assembleDebug.txt").write_text(
                build_report.get("output", ""),
                encoding="utf-8",
            )

    if build_report and build_report.get("ok"):
        status = "NEEDS-HARDWARE-PROOF"
    elif build_report and not build_report.get("ok"):
        status = "NEEDS-BUILD-FIX"
    elif build_blockers:
        status = "NEEDS-TOOLING"
    elif generated_app:
        status = "READY-FOR-BUILD"
    else:
        status = "NEEDS-SCAFFOLD"

    report = {
        "timestamp": stamp,
        "timestamp_human": time.strftime("%Y-%m-%d %H:%M:%S %Z"),
        "commands": commands,
        "paths": paths,
        "gradle_facts": gradle_facts,
        "android_sdk": android_sdk,
        "java": java,
        "ports": ports_report,
        "adb": adb_report,
        "build_blockers": build_blockers,
        "reference_warnings": reference_warnings,
        "generated_app": generated_app,
        "build": build_report,
        "status": status,
    }

    json_path = out_dir / "route3-preflight.json"
    summary_path = out_dir / "summary.md"
    json_path.write_text(json.dumps(report, indent=2, sort_keys=True), encoding="utf-8")
    summary_path.write_text(build_markdown(report).rstrip() + "\n", encoding="utf-8")
    print(json.dumps({"out_dir": str(out_dir), "summary": str(summary_path)}, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
