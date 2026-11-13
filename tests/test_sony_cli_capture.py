"""Contracts for Sony-specific CLI capture fallback.

Run standalone:
    python3 tests/test_sony_cli_capture.py
"""

from __future__ import annotations

import sys
import types
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
try:
    import gphoto2  # noqa: F401 — prefer the installed binding
except ModuleNotFoundError:
    sys.modules.setdefault(
        "gphoto2",
        types.SimpleNamespace(GPhoto2Error=RuntimeError, GP_CAPTURE_IMAGE=0, GP_FILE_TYPE_NORMAL=0),
    )

from _runner import run_module_tests

from sonyremote.camera import _pick_downloaded_jpeg


def test_pick_downloaded_jpeg_prefers_jpeg_over_raw() -> None:
    files = [
        Path("/tmp/capt_DSC00001.ARW"),
        Path("/tmp/capt_DSC00001.JPG"),
    ]

    assert _pick_downloaded_jpeg(files) == files[1]


def test_pick_downloaded_jpeg_uses_newest_jpeg_when_many_exist() -> None:
    files = [
        Path("/tmp/capt_DSC00001.JPG"),
        Path("/tmp/capt_DSC00002.ARW"),
        Path("/tmp/capt_DSC00002.JPG"),
    ]

    assert _pick_downloaded_jpeg(files) == files[2]


if __name__ == "__main__":
    raise SystemExit(run_module_tests(__name__))
