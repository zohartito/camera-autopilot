"""Pin the committed Android gradle-wrapper.jar to Gradle's published checksum. No Gradle, network or device.

Official values were read from services.gradle.org on 2026-09-27; the URLs are recorded below.
A swapped jar, or a version bump without re-pinning both checksums, fails here.
"""

from __future__ import annotations

import hashlib
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
WRAPPER = ROOT / "mobile" / "android" / "gradle" / "wrapper"
GRADLE_VERSION = "8.14.3"
# https://services.gradle.org/distributions/gradle-8.14.3-wrapper.jar.sha256
OFFICIAL_WRAPPER_JAR_SHA256 = "7d3a4ac4de1c32b59bc6a4eb8ecb8e612ccd0cf1ae1e99f66902da64df296172"
# https://services.gradle.org/distributions/gradle-8.14.3-all.zip.sha256
OFFICIAL_DISTRIBUTION_SHA256 = "ed1a8d686605fd7c23bdf62c7fc7add1c5b23b2bbc3721e661934ef4a4911d7c"


def _properties() -> dict[str, str]:
    pairs = {}
    for line in (WRAPPER / "gradle-wrapper.properties").read_text().splitlines():
        if line.strip() and not line.lstrip().startswith("#") and "=" in line:
            key, value = line.split("=", 1)
            pairs[key.strip()] = value.strip()
    return pairs


def test_wrapper_jar_matches_official_checksum() -> None:
    digest = hashlib.sha256((WRAPPER / "gradle-wrapper.jar").read_bytes()).hexdigest()
    assert digest == OFFICIAL_WRAPPER_JAR_SHA256


def test_wrapper_properties_pin_the_recorded_version_and_distribution_checksum() -> None:
    properties = _properties()
    assert properties["distributionUrl"] == (
        rf"https\://services.gradle.org/distributions/gradle-{GRADLE_VERSION}-all.zip"
    )
    assert properties.get("distributionSha256Sum") == OFFICIAL_DISTRIBUTION_SHA256
