"""Lossless corpus equivalence, bounded lookup and corruption refusal, no models."""
import hashlib
import json
from pathlib import Path
import sys

import numpy as np
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))
import compact_taste_index as compact


def corpus(tmp_path, rows=101, dimensions=8):
    rng = np.random.default_rng(31)
    vectors = rng.normal(size=(rows, dimensions)).astype("float32")
    vectors /= np.linalg.norm(vectors, axis=1, keepdims=True)
    source = tmp_path / "taste_index.npz"
    np.savez(source, embeddings=vectors, iso=np.arange(rows) + 100,
             f_number=np.full(rows, 5.6), shutter_s=np.full(rows, 1 / 125),
             focal_mm=np.full(rows, 50.), titles=np.array([f"File:{i}.jpg" for i in range(rows)]))
    return source, vectors


def test_lossless_mapping_and_all_rows_top_k(tmp_path):
    source, vectors = corpus(tmp_path)
    destination = tmp_path / "taste_index.compact"
    manifest = compact.prepare(source, destination, compact.sha256(source))
    arrays = compact.load(destination, compact.sha256(source))
    assert manifest["rows"] == len(vectors)
    assert manifest["quantization"] == "none"
    assert all(isinstance(value, np.memmap) and not value.flags.writeable for value in arrays.values())
    with np.load(source, allow_pickle=False) as original:
        for key in compact.KEYS:
            np.testing.assert_array_equal(arrays[key], original[key])
    query = vectors[-1]
    scores = vectors @ query
    expected = np.lexsort((np.arange(len(vectors)), -scores))[:15]
    ids, selected = compact.top_k(arrays["embeddings"], query, 15, block_rows=13)
    np.testing.assert_array_equal(ids, expected)
    np.testing.assert_allclose(selected, scores[expected], atol=2e-7)
    assert ids[0] == len(vectors) - 1


def test_boundary_ties_are_stable_and_k_clamped():
    vectors = np.ones((41, 2), dtype="float32")
    ids, scores = compact.top_k(vectors, np.ones(2, dtype="float32"), 8, block_rows=3)
    assert ids.tolist() == list(range(8))
    assert compact.top_k(vectors, np.ones(2, dtype="float32"), 80)[0].tolist() == list(range(41))
    with pytest.raises(ValueError):
        compact.top_k(vectors, np.array([float("nan"), 1]), 2)


def test_digest_identity_and_permissions_fail_closed(tmp_path):
    source, _ = corpus(tmp_path)
    target = tmp_path / "compact"
    compact.prepare(source, target)
    with pytest.raises(ValueError, match="different source"):
        compact.load(target, "0" * 64)
    member = target / "iso.npy"
    member.chmod(0o644)
    with pytest.raises(ValueError, match="immutable"):
        compact.load(target)
    content = member.read_bytes()
    member.write_bytes(content[:-1] + bytes([content[-1] ^ 1]))
    member.chmod(0o444)
    with pytest.raises(ValueError, match="digest"):
        compact.load(target)
    with pytest.raises(FileExistsError):
        compact.prepare(source, target)


def test_refuses_unpinned_source_and_nonfinite_values(tmp_path):
    source, _ = corpus(tmp_path)
    with pytest.raises(ValueError, match="pinned"):
        compact.prepare(source, tmp_path / "wrong", "0" * 64)
    with np.load(source) as archive:
        arrays = dict(archive)
    arrays["embeddings"][0, 0] = float("nan")
    np.savez(source, **arrays)
    with pytest.raises(ValueError, match="non-finite"):
        compact.prepare(source, tmp_path / "nonfinite")
    assert not (tmp_path / "nonfinite").exists()
    assert not list(tmp_path.glob(".taste-compact-*"))


def test_runtime_selects_compact_and_preserves_legacy_fallback(tmp_path, monkeypatch):
    import embed_index
    source, _ = corpus(tmp_path)
    manifest = {"sha256": compact.sha256(source), "bytes": source.stat().st_size, "photos_embedded": 101}
    source.with_suffix(".MANIFEST.json").write_text(json.dumps(manifest))
    compact.prepare(source, source.with_suffix(".compact"))
    monkeypatch.setattr(embed_index, "INDEX", source)
    monkeypatch.setattr(embed_index, "_INDEX_CACHE", None)
    monkeypatch.setattr(embed_index, "_INDEX_STORAGE", "not_loaded")
    monkeypatch.setattr(embed_index, "_INDEX_COMPACT_ERROR", None)
    monkeypatch.delenv("CAMERA_AUTOPILOT_TASTE_COMPACT", raising=False)
    monkeypatch.delenv("CAMERA_AUTOPILOT_TASTE_STORAGE", raising=False)
    assert isinstance(embed_index._get_index()["embeddings"], np.memmap)
    assert embed_index._INDEX_STORAGE == "lossless_mmap"
    embed_index._INDEX_CACHE = None
    monkeypatch.setenv("CAMERA_AUTOPILOT_TASTE_STORAGE", "legacy")
    assert not isinstance(embed_index._get_index()["embeddings"], np.memmap)
    assert embed_index._INDEX_STORAGE == "legacy_resident_npz"


def test_compact_only_health(tmp_path, monkeypatch):
    import runtime_resources
    data = tmp_path / "data"
    data.mkdir()
    source, _ = corpus(data)
    source.with_suffix(".MANIFEST.json").write_text(json.dumps({"sha256": compact.sha256(source), "bytes": source.stat().st_size, "photos_embedded": 101}))
    compact.prepare(source, source.with_suffix(".compact"))
    source.unlink()
    monkeypatch.setattr(runtime_resources, "REPO", tmp_path)
    monkeypatch.delenv("CAMERA_AUTOPILOT_TASTE_COMPACT", raising=False)
    result = runtime_resources.report()
    assert result["taste"]["state"] == "verified"
    assert result["taste"]["available_storage"] == "lossless_mmap"
    assert result["taste"]["rows"] == 101


def test_compact_intent_never_calls_model_and_preserves_explicit_recipe(monkeypatch):
    import intent_brain
    monkeypatch.setenv("CAMERA_INTENT_PROFILE", "compact")
    def forbidden(_):
        raise AssertionError("compact essential intent must not call a model")
    monkeypatch.setattr(intent_brain, "_ask_ollama", forbidden)
    plan = intent_brain.interpret("freeze my kid running")
    assert plan["source"] == "compact_deterministic"
    assert plan["motion_intent"] == "freeze"
    assert intent_brain.interpret(None)["source"] == "compact_deterministic"
    assert intent_brain.interpret("cool", variant_id="cool_blue")["source"] == "variant"


def test_full_intent_profile_remains_model_capable(monkeypatch):
    import intent_brain
    monkeypatch.delenv("CAMERA_INTENT_PROFILE", raising=False)
    monkeypatch.setattr(intent_brain, "_ask_ollama", lambda _: {"target_brightness": 100})
    assert intent_brain.interpret("moody")["source"] == "model"


def test_corrupt_compact_falls_back_with_explicit_reason(tmp_path, monkeypatch):
    import embed_index
    source, _ = corpus(tmp_path)
    source.with_suffix(".MANIFEST.json").write_text(json.dumps({"sha256": compact.sha256(source), "bytes": source.stat().st_size, "photos_embedded": 101}))
    destination = source.with_suffix(".compact")
    compact.prepare(source, destination)
    (destination / "iso.npy").chmod(0o644)
    monkeypatch.setattr(embed_index, "INDEX", source)
    monkeypatch.setattr(embed_index, "_INDEX_CACHE", None)
    monkeypatch.setattr(embed_index, "_INDEX_STORAGE", "not_loaded")
    monkeypatch.setattr(embed_index, "_INDEX_COMPACT_ERROR", None)
    monkeypatch.delenv("CAMERA_AUTOPILOT_TASTE_COMPACT", raising=False)
    monkeypatch.delenv("CAMERA_AUTOPILOT_TASTE_STORAGE", raising=False)
    result = embed_index._get_index()
    assert len(result["embeddings"]) == 101
    assert embed_index._INDEX_STORAGE == "legacy_resident_npz"
    assert "immutable" in embed_index._INDEX_COMPACT_ERROR


def test_compact_only_capture_loads_all_rows(tmp_path, monkeypatch):
    import embed_index
    source, _ = corpus(tmp_path)
    source.with_suffix(".MANIFEST.json").write_text(json.dumps({"sha256": compact.sha256(source), "bytes": source.stat().st_size, "photos_embedded": 101}))
    compact.prepare(source, source.with_suffix(".compact"))
    source.unlink()
    monkeypatch.setattr(embed_index, "INDEX", source)
    monkeypatch.setattr(embed_index, "_INDEX_CACHE", None)
    monkeypatch.setattr(embed_index, "_INDEX_STORAGE", "not_loaded")
    monkeypatch.setattr(embed_index, "_INDEX_COMPACT_ERROR", None)
    monkeypatch.delenv("CAMERA_AUTOPILOT_TASTE_COMPACT", raising=False)
    monkeypatch.delenv("CAMERA_AUTOPILOT_TASTE_STORAGE", raising=False)
    assert len(embed_index._get_index()["embeddings"]) == 101
    assert embed_index._INDEX_STORAGE == "lossless_mmap"
