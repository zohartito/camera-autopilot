"""Resource availability must not be mistaken for model execution."""
import hashlib
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))
import runtime_resources as resources


def test_digest_detects_replacement_and_same_size_corruption(tmp_path):
    target = tmp_path / "weights.bin"
    target.write_bytes(b"correct")
    digest = hashlib.sha256(b"correct").hexdigest()
    assert resources._file_receipt(target, 7, digest)["state"] == "verified"
    replacement = tmp_path / "new.bin"
    replacement.write_bytes(b"corrupt")
    replacement.replace(target)
    assert resources._file_receipt(target, 7, digest)["state"] == "hash_mismatch"
    target.unlink()
    assert resources._file_receipt(target, 7, digest)["state"] == "missing"


def test_report_distinguishes_verified_files_from_missing_encoder(tmp_path, monkeypatch):
    (tmp_path / "data").mkdir()
    index = tmp_path / "data/taste_index.npz"
    index.write_bytes(b"index")
    (tmp_path / "data/taste_index.MANIFEST.json").write_text(json.dumps({
        "bytes": 5, "sha256": hashlib.sha256(b"index").hexdigest(), "photos_embedded": 123,
    }))
    monkeypatch.setattr(resources, "REPO", tmp_path)
    monkeypatch.setattr(resources, "_module_available", lambda _: False)
    monkeypatch.delenv("CAMERA_AUTOPILOT_CLIP_WEIGHTS", raising=False)
    result = resources.report()
    assert result["taste"]["state"] == "verified"
    assert result["taste"]["rows"] == 123
    assert result["clip"]["state"] == "missing"
    assert result["taste_ready"] is False
    assert result["fallback"] == "physics_only"


def test_missing_weights_fail_before_any_model_download(tmp_path, monkeypatch):
    import pytest
    monkeypatch.setenv("CAMERA_AUTOPILOT_CLIP_WEIGHTS", str(tmp_path / "absent.bin"))
    with pytest.raises(RuntimeError, match="run tools/prepare_taste_runtime.py"):
        resources.verified_clip_weights()


def test_vision_status_reports_failed_actual_request(monkeypatch):
    import vision_banana
    original = dict(vision_banana._LAST_RESULT)
    try:
        assert vision_banana.analyze("/nonexistent/runtime-proof.jpg") is None
        result = vision_banana.runtime_status()
        assert result["state"] == "unavailable"
        assert result["fallback"] == "physics_only"
        result["state"] = "pretend"
        assert vision_banana.runtime_status()["state"] == "unavailable"
    finally:
        vision_banana._LAST_RESULT = original
