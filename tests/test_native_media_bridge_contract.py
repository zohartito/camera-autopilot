"""Static contract: the web native-media bridge is not iOS-only.

Android parity must register the same Capacitor plugin name and methods.
This does not build or run an Android app.
"""

from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
CLIENT = ROOT / "backend" / "web" / "client.js"
REVIEW = ROOT / "backend" / "web" / "review.html"
IOS_PLUGIN = ROOT / "mobile" / "ios" / "App" / "App" / "AppDelegate.swift"
ANDROID_PLUGIN = ROOT / "mobile" / "android" / "app" / "src" / "main" / "java" / "com" / "zohar" / "autopilot" / "AutopilotMediaPlugin.java"
ANDROID_GATE = ROOT / "mobile" / "android" / "app" / "src" / "main" / "java" / "com" / "zohar" / "autopilot" / "JpegGate.java"
ANDROID_ACTIVITY = ROOT / "mobile" / "android" / "app" / "src" / "main" / "java" / "com" / "zohar" / "autopilot" / "MainActivity.java"
ANDROID_MANIFEST = ROOT / "mobile" / "android" / "app" / "src" / "main" / "AndroidManifest.xml"
ANDROID_NETWORK = ROOT / "mobile" / "android" / "app" / "src" / "main" / "res" / "xml" / "network_security_config.xml"
ANDROID_RES = ROOT / "mobile" / "android" / "app" / "src" / "main" / "res"
PACKAGE_JSON = ROOT / "mobile" / "package.json"
CI = ROOT / ".github" / "workflows" / "ci.yml"
TEST_SAFE = ROOT / "scripts" / "test_safe.sh"


def test_native_media_is_any_native_platform_not_ios() -> None:
    client = CLIENT.read_text(encoding="utf-8")
    start = client.index("nativeMedia()")
    body = client[start : client.index("downloadArtifact", start)]
    assert "isNativePlatform" in body
    assert "getPlatform" not in body
    assert "'ios'" not in body
    assert '"ios"' not in body
    for method in ("savePhoto", "sharePhoto", "safeArea"):
        assert method in body
    assert "registerPlugin('AutopilotMedia')" in body


def test_export_image_uses_the_same_plugin_methods() -> None:
    client = CLIENT.read_text(encoding="utf-8")
    start = client.index("async exportImage")
    body = client[start : client.index("if (window.Capacitor", start)]
    assert "32 * 1024 * 1024" in body
    assert "native[saveToPhotos ? 'savePhoto' : 'sharePhoto']" in body


def test_review_save_label_follows_the_plugin_not_the_os() -> None:
    html = REVIEW.read_text(encoding="utf-8")
    assert "Autopilot.nativeMedia() ? 'Save to Photos' : 'Download'" in html
    assert 'button class="share">Share</button>' in html
    assert "this page never deletes from the card" in html
    assert 'id="card-history"' in html


def test_review_share_copy_does_not_claim_android_completion() -> None:
    html = REVIEW.read_text(encoding="utf-8")
    assert (
        "outcome.cancelled ? 'Cancelled' : outcome.downloaded ? 'Download requested' : "
        "outcome.shared ? 'Share completed' : 'Share sheet dismissed'"
    ) in html
    assert "Autopilot.photosSaveConfirmed(outcome) ? 'Saved to Photos'" in html


def test_ios_plugin_is_the_contract_android_must_match() -> None:
    swift = IOS_PLUGIN.read_text(encoding="utf-8")
    assert 'public let jsName = "AutopilotMedia"' in swift
    for method in ("sharePhoto", "savePhoto", "safeArea"):
        assert f'CAPPluginMethod(name: "{method}", returnType: CAPPluginReturnPromise)' in swift
    assert "data.count <= 32 * 1024 * 1024" in swift
    assert "encoded.count <= 45_000_000" in swift
    assert "width <= 40000000 / height" in swift
    assert "PHAssetCreationRequest.forAsset()" in swift
    assert "UIActivityViewController" in swift


def test_android_plugin_matches_ios_contract() -> None:
    plugin = ANDROID_PLUGIN.read_text(encoding="utf-8")
    gate = ANDROID_GATE.read_text(encoding="utf-8")
    activity = ANDROID_ACTIVITY.read_text(encoding="utf-8")
    assert '@CapacitorPlugin(name = "AutopilotMedia")' in plugin
    for method in ("sharePhoto", "savePhoto", "safeArea"):
        assert f"public void {method}(PluginCall call)" in plugin
    assert "MAX_BASE64_CHARS = 45_000_000" in gate
    assert "MAX_BYTES = 32 * 1024 * 1024" in gate
    assert "MAX_PIXELS = 40_000_000" in gate
    assert "width > MAX_PIXELS / height" in gate
    assert "MediaStore.Images.Media" in plugin
    assert "stream.write(jpeg.bytes)" in plugin
    assert "Bitmap.compress" not in plugin
    assert "Bitmap" not in plugin
    assert "Intent.ACTION_SEND" in plugin
    assert "FileProvider.getUriForFile" in plugin
    assert "WindowInsetsCompat" in plugin
    assert "registerPlugin(AutopilotMediaPlugin.class)" in activity
    assert activity.index("registerPlugin(AutopilotMediaPlugin.class)") < activity.index("super.onCreate")


def test_android_manifest_has_no_broad_storage_and_debug_user_ca() -> None:
    manifest = ANDROID_MANIFEST.read_text(encoding="utf-8")
    network = ANDROID_NETWORK.read_text(encoding="utf-8")
    assert "android.permission.INTERNET" in manifest
    assert "WRITE_EXTERNAL_STORAGE" not in manifest
    assert "READ_MEDIA_IMAGES" not in manifest
    assert "READ_EXTERNAL_STORAGE" not in manifest
    assert 'android:name="androidx.core.content.FileProvider"' in manifest
    assert 'android:networkSecurityConfig="@xml/network_security_config"' in manifest
    assert '<certificates src="user" />' in network
    assert "<debug-overrides>" in network
    base = network[network.index("<base-config") : network.index("</base-config>")]
    assert '<certificates src="system" />' in base
    assert '<certificates src="user" />' not in base


def test_no_third_party_media_plugin_and_no_hosted_gradle() -> None:
    package = PACKAGE_JSON.read_text(encoding="utf-8")
    assert '"@capacitor/android": "8.4.2"' in package
    assert "@capacitor/share" not in package
    assert "@capacitor-community/media" not in package
    gitignore = (ROOT / "mobile" / ".gitignore").read_text(encoding="utf-8")
    assert not any(line.strip() == "android/" for line in gitignore.splitlines())
    variables = (ROOT / "mobile" / "android" / "variables.gradle").read_text(encoding="utf-8")
    assert "minSdkVersion = 29" in variables
    ci = CI.read_text(encoding="utf-8")
    assert "gradle" not in ci.lower()
    test_safe = TEST_SAFE.read_text(encoding="utf-8")
    assert "gradle" not in test_safe.lower()


def test_android_style_color_resources_exist() -> None:
    """styles.xml @color names must be defined in values/. No Gradle, no AAPT."""
    referenced: set[str] = set()
    for path in ANDROID_RES.rglob("*.xml"):
        referenced.update(re.findall(r"@color/([A-Za-z0-9_]+)", path.read_text(encoding="utf-8")))
    defined: set[str] = set()
    for path in (ANDROID_RES / "values").glob("*.xml"):
        defined.update(re.findall(r'<color\s+name="([A-Za-z0-9_]+)"', path.read_text(encoding="utf-8")))
    missing = sorted(referenced - defined)
    assert not missing, f"undefined color resources: {missing}"
    for name in ("colorPrimary", "colorPrimaryDark", "colorAccent"):
        assert name in referenced
        assert name in defined
    assert (ANDROID_RES / "values" / "colors.xml").is_file()


def test_android_drawable_resources_exist() -> None:
    """styles.xml @drawable names must resolve under res/drawable*. No Gradle, no AAPT."""
    referenced: set[str] = set()
    for path in ANDROID_RES.rglob("*.xml"):
        referenced.update(re.findall(r"@drawable/([A-Za-z0-9_]+)", path.read_text(encoding="utf-8")))
    defined: set[str] = set()
    for folder in ANDROID_RES.iterdir():
        if not (folder.is_dir() and folder.name.startswith("drawable")):
            continue
        for path in folder.iterdir():
            if path.is_file():
                name = path.name
                defined.add(name[: -len(".9.png")] if name.endswith(".9.png") else path.stem)
    missing = sorted(referenced - defined)
    assert not missing, f"undefined drawable resources: {missing}"
    assert "splash" in referenced
    assert "splash" in defined
    assert (ANDROID_RES / "drawable" / "splash.xml").is_file()


def test_android_share_chooser_result_is_completion_unknown() -> None:
    plugin = ANDROID_PLUGIN.read_text(encoding="utf-8")
    start = plugin.index("private void shareFinished")
    body = plugin[start : plugin.index("static String displayName")]
    assert "Activity.RESULT_CANCELED" not in body
    assert 'out.put("cancelled", true)' not in body
    assert "file.delete()" not in body
    assert "pendingShareFile" not in plugin
    assert 'out.put("shared", false)' in body
    assert 'out.put("cancelled", false)' in body
    assert 'out.put("completedUnknown", true)' in body
    assert "retireOldShares" in plugin
    assert "3600_000L" in plugin
    swift = IOS_PLUGIN.read_text(encoding="utf-8")
    assert 'call.resolve(["shared": completed, "cancelled": !completed])' in swift
