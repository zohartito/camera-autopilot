from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
TOOLS = REPO_ROOT / "tools"
if str(TOOLS) not in sys.path:
    sys.path.insert(0, str(TOOLS))

import a7ii_usb_movie_record_proof as movie_proof


def _only_artifact(root: Path) -> dict:
    paths = list(root.rglob("usb-movie-record-proof.json"))
    assert len(paths) == 1
    return json.loads(paths[0].read_text(encoding="utf-8"))


def test_dry_run_guard_writes_artifact_without_camera() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        tmp_path = Path(tmp)
        rc = movie_proof.main(["--seconds", "1.5", "--out-dir", str(tmp_path), "--json"])

        assert rc == 0
        payload = _only_artifact(tmp_path)
        assert payload["route"] == "usb_gphoto"
        assert payload["setting_id"] == "camera.movie_mode"
        assert payload["node"] == "movie"
        assert payload["dry_run"] is True
        assert payload["verdict"] == "dry_run_guarded"
        assert "Movie mode dial".lower() in " ".join(payload["notes"]).lower()
    print("  USB movie record dry-run guard: ok")


def test_execute_requires_explicit_recording_acknowledgement() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        tmp_path = Path(tmp)
        rc = movie_proof.main(["--execute", "--out-dir", str(tmp_path), "--json"])

        assert rc == 2
        payload = _only_artifact(tmp_path)
        assert payload["dry_run"] is False
        assert payload["allow_camera_record"] is False
        assert payload["verdict"] == "guarded_missing_allow_camera_record"
    print("  USB movie record explicit acknowledgement guard: ok")


if __name__ == "__main__":
    test_dry_run_guard_writes_artifact_without_camera()
    test_execute_requires_explicit_recording_acknowledgement()
    print("usb_movie_record_proof tests: PASS")
