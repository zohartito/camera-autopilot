"""The per-frame CLIP query cache must never hand back a different image's embedding.

`embed_index.embed_query` lets taste retrieval and subject recognition share one embedding of
the meter frame. The cache is process-global, so it is keyed on the file's contents identity
(path, mtime, size), not the path alone: a caller that rewrites a fixed path must get a fresh
embedding. Camera-free and model-free: the encoder is stubbed.
"""
import os
import sys
from pathlib import Path

import numpy as np
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))

import embed_index


@pytest.fixture
def counted_embed(monkeypatch):
    calls = []

    def fake_embed(_model, _preprocess, _device, image):
        calls.append(image)
        return np.full(4, float(len(calls)), dtype=np.float32)

    monkeypatch.setattr(embed_index, "_embed", fake_embed)
    monkeypatch.setattr(embed_index, "open_rgb", lambda path: Path(path).read_bytes())
    monkeypatch.setattr(embed_index, "_QUERY_CACHE", None)
    return calls


def test_the_same_frame_is_embedded_once(tmp_path, counted_embed):
    frame = tmp_path / "meter.jpg"
    frame.write_bytes(b"frame-one")
    first = embed_index.embed_query(str(frame), None, None, None)
    second = embed_index.embed_query(str(frame), None, None, None)
    assert len(counted_embed) == 1
    assert np.array_equal(first, second)


def test_a_rewritten_file_at_the_same_path_is_embedded_again(tmp_path, counted_embed):
    frame = tmp_path / "query.jpg"
    frame.write_bytes(b"old image")
    first = embed_index.embed_query(str(frame), None, None, None)
    frame.write_bytes(b"a different, longer image")
    # Force a distinct mtime even on filesystems with coarse timestamps.
    stat = frame.stat()
    os.utime(frame, ns=(stat.st_atime_ns, stat.st_mtime_ns + 1_000_000_000))
    second = embed_index.embed_query(str(frame), None, None, None)
    assert len(counted_embed) == 2, "a stale embedding was returned for a rewritten file"
    assert not np.array_equal(first, second)


def test_a_new_path_replaces_the_entry(tmp_path, counted_embed):
    a, b = tmp_path / "a.jpg", tmp_path / "b.jpg"
    a.write_bytes(b"a"); b.write_bytes(b"b")
    embed_index.embed_query(str(a), None, None, None)
    embed_index.embed_query(str(b), None, None, None)
    embed_index.embed_query(str(a), None, None, None)
    assert len(counted_embed) == 3
