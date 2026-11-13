"""Network-free tests for the multi-source taste-index build.

No network, no GPU, no CLIP weights: the model load, thumbnail fetch, and GPU embed are stubbed,
so build() runs end-to-end against two on-disk CSVs and we assert it ingests BOTH sources. This
guards the path that lets the taste index grow past the Wikimedia-only ceiling toward ~450k.
Imports embed_index (torch/open_clip), so it runs in the .venv-ml lane of run_tests.sh.

    backend/.venv-ml/bin/python backend/test_embed_index.py
"""
from __future__ import annotations

import csv
import io
import sys
import tempfile
import urllib.error
from pathlib import Path

import numpy as np
from PIL import Image

sys.path.insert(0, str(Path(__file__).resolve().parent))

import embed_index as ei

_FIELDS = ["title", "iso", "f_number", "shutter_s", "focal_mm", "focal_35mm",
           "make", "model", "license", "url"]


def _write_csv(path: Path, rows: list[dict]) -> None:
    """Write a settings CSV with the harvesters' shared columns."""
    with path.open("w", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=_FIELDS)
        writer.writeheader()
        for row in rows:
            writer.writerow(row)


def _row(title: str, iso: int = 100, f_number: float = 2.8,
         shutter: float = 0.004, focal: float = 50.0) -> dict:
    return {"title": title, "iso": iso, "f_number": f_number, "shutter_s": shutter,
            "focal_mm": focal, "focal_35mm": focal, "make": "SONY", "model": "ILCE-7M2",
            "license": "CC BY 2.0", "url": "https://upload.wikimedia.org/wikipedia/commons/thumb/a/ab/Example.jpg/320px-Example.jpg"}


def test_read_rows_concatenates_and_skips_missing() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        commons = Path(tmp) / "wikimedia_qi.csv"
        flickr = Path(tmp) / "flickr_settings.csv"
        missing = Path(tmp) / "does_not_exist.csv"
        _write_csv(commons, [_row("File:Alps.jpg"), _row("File:Fjord.jpg")])
        _write_csv(flickr, [_row("flickr:Golden hour")])

        rows = ei._read_rows([commons, flickr, missing])
        titles = [r["title"] for r in rows]

    assert len(rows) == 3, titles                              # both files read, missing one skipped
    assert titles == ["File:Alps.jpg", "File:Fjord.jpg", "flickr:Golden hour"], titles
    assert any(t.startswith("flickr:") for t in titles), titles  # 2nd source present
    print("  ok  _read_rows concatenates multiple CSVs and skips missing ones")


def test_build_skips_untrusted_dataset_sources() -> None:
    """Build cannot embed third-party dataset rows, even with a stubbed fetcher."""
    saved = {"_load_model": ei._load_model, "_fetch": ei._fetch,
             "_embed_batch": ei._embed_batch, "INDEX": ei.INDEX,
             "_INDEX_CACHE": ei._INDEX_CACHE}
    with tempfile.TemporaryDirectory() as tmp:
        commons = Path(tmp) / "wikimedia_qi.csv"
        flickr = Path(tmp) / "flickr_settings.csv"
        _write_csv(commons, [_row("File:Alps.jpg"), _row("File:Fjord.jpg")])
        _write_csv(flickr, [_row("flickr:Golden hour"), _row("flickr:Blue hour")])

        # Stub the network + GPU so build() exercises only ingest/resume/save, offline.
        def fake_fetch(row):
            if not row["title"].startswith("File:"):
                return None
            return row["title"], (int(row["iso"]), float(row["f_number"]),
                                  float(row["shutter_s"]), float(row["focal_mm"])), Image.new("RGB", (8, 8))

        ei._load_model = lambda device: (object(), object())
        ei._fetch = fake_fetch
        ei._embed_batch = lambda model, preprocess, device, images: np.zeros((len(images), 4), dtype="float32")
        ei.INDEX = Path(tmp) / "taste_index.npz"
        try:
            ei.build(sources=[commons, flickr])
            idx = np.load(ei.INDEX, allow_pickle=False)
            titles = set(idx["titles"].tolist())
        finally:
            for name, value in saved.items():
                setattr(ei, name, value)

    assert titles == {"File:Alps.jpg", "File:Fjord.jpg"}, titles
    print("  ok  build skips third-party dataset rows")


def _png_bytes() -> bytes:
    """Minimal valid PNG so _fetch's Image.open(...).convert('RGB') succeeds offline."""
    buf = io.BytesIO()
    Image.new("RGB", (8, 8), (123, 222, 64)).save(buf, format="PNG")
    return buf.getvalue()


def test_fetch_requires_a_validated_direct_wikimedia_upload_url() -> None:
    saved = {"_download_url": ei._download_url}
    calls: dict = {}
    png = _png_bytes()

    def fake_download(url, timeout=20):
        calls["upload_url"] = url
        return png

    try:
        ei._download_url = fake_download

        commons = ei._fetch(_row("File:Alps.jpg"))
        flickr = ei._fetch({**_row("flickr:Golden hour"),
                            "url": "https://live.staticflickr.com/65535/123_abc_n.jpg"})
        title_only = ei._fetch({**_row("File:No direct URL.jpg"), "url": ""})
        bad_host = ei._fetch({**_row("File:Wrong host.jpg"), "url": "https://commons.wikimedia.org/wiki/File:X.jpg"})
    finally:
        for name, value in saved.items():
            setattr(ei, name, value)

    assert commons is not None and commons[0] == "File:Alps.jpg", commons
    assert calls["upload_url"].startswith("https://upload.wikimedia.org/wikipedia/"), calls
    assert flickr is None and title_only is None and bad_host is None
    assert ei.MAX_INDEX_IMAGE_BYTES <= ei.MAX_IMAGE_SOURCE_BYTES
    print("  ok  _fetch requires a direct, bounded Wikimedia upload URL")


def test_download_deadline_stops_after_a_31_second_socket_operation() -> None:
    class Clock:
        value = 0.0
        def monotonic(self): return self.value
        def sleep(self, seconds): self.value += seconds

    class Response:
        headers = {}
        def __enter__(self): return self
        def __exit__(self, *_args): return False
        def read(self, _size): raise AssertionError("deadline must be checked before a post-timeout read")

    class Opener:
        calls = 0
        def open(self, _request, timeout):
            self.calls += 1
            assert timeout == 20
            clock.value += 31.0
            return Response()

    clock = Clock()
    saved = (ei.time.monotonic, ei.time.sleep, ei.urllib.request.build_opener)
    try:
        ei.time.monotonic = clock.monotonic
        ei.time.sleep = clock.sleep
        ei.urllib.request.build_opener = lambda *_handlers: Opener()
        assert ei._download_url("https://upload.wikimedia.org/wikipedia/commons/a.jpg") is None
    finally:
        ei.time.monotonic, ei.time.sleep, ei.urllib.request.build_opener = saved
    print("  ok  aggregate download deadline rejects a 31-second socket operation")


def test_download_deadline_caps_malformed_retry_after_and_40_second_oversleep() -> None:
    class Clock:
        value = 0.0
        sleeps: list[float] = []
        def monotonic(self): return self.value
        def sleep(self, seconds):
            self.sleeps.append(seconds)
            self.value += 40.0

    class Opener:
        calls = 0
        def open(self, request, timeout):
            self.calls += 1
            raise urllib.error.HTTPError(request.full_url, 429, "rate limited", {"Retry-After": "not-a-number"}, None)

    clock = Clock()
    opener = Opener()
    saved = (ei.time.monotonic, ei.time.sleep, ei.urllib.request.build_opener)
    try:
        ei.time.monotonic = clock.monotonic
        ei.time.sleep = clock.sleep
        ei.urllib.request.build_opener = lambda *_handlers: opener
        assert ei._download_url("https://upload.wikimedia.org/wikipedia/commons/a.jpg") is None
    finally:
        ei.time.monotonic, ei.time.sleep, ei.urllib.request.build_opener = saved
    assert opener.calls == 1
    assert clock.sleeps == [5]
    print("  ok  malformed Retry-After and a 40-second oversleep cannot bypass deadline")


def test_download_deadline_rebounds_socket_timeout_before_each_trickled_read() -> None:
    class Clock:
        value = 0.0
        def monotonic(self): return self.value
        def sleep(self, _seconds): raise AssertionError("unexpected retry sleep")

    class Response:
        headers = {}
        timeouts: list[float] = []
        reads = 0
        def __enter__(self): return self
        def __exit__(self, *_args): return False
        def settimeout(self, seconds): self.timeouts.append(seconds)
        def read(self, _size):
            self.reads += 1
            clock.value += 20.0
            return b"first" if self.reads == 1 else b"second"

    class Opener:
        def open(self, _request, timeout):
            assert timeout == 20
            return response

    clock = Clock()
    response = Response()
    saved = (ei.time.monotonic, ei.time.sleep, ei.urllib.request.build_opener)
    try:
        ei.time.monotonic = clock.monotonic
        ei.time.sleep = clock.sleep
        ei.urllib.request.build_opener = lambda *_handlers: Opener()
        assert ei._download_url("https://upload.wikimedia.org/wikipedia/commons/a.jpg") is None
    finally:
        ei.time.monotonic, ei.time.sleep, ei.urllib.request.build_opener = saved
    assert response.reads == 2
    assert response.timeouts == [20.0, 10.0]
    print("  ok  each blocking read receives the remaining aggregate socket budget")


def test_save_is_atomic_and_invalidates_runtime_cache() -> None:
    saved = {"INDEX": ei.INDEX, "_INDEX_CACHE": ei._INDEX_CACHE}
    with tempfile.TemporaryDirectory() as tmp:
        ei.INDEX = Path(tmp) / "taste_index.npz"
        ei._INDEX_CACHE = {"stale": np.array([1])}
        vectors = [
            np.array([1.0, 0.0], dtype="float32"),
            np.array([0.0, 1.0], dtype="float32"),
        ]
        meta = {
            "iso": [100, 200],
            "f_number": [2.8, 4.0],
            "shutter_s": [1 / 125, 1 / 60],
            "focal_mm": [35.0, 50.0],
            "titles": ["File:A.jpg", "File:B.jpg"],
        }
        try:
            ei._save(vectors, meta)
            temporary = ei.INDEX.with_name(f".{ei.INDEX.name}.tmp")
            with np.load(ei.INDEX, allow_pickle=False) as archive:
                shape = archive["embeddings"].shape
            assert shape == (2, 2), shape
            assert not temporary.exists(), temporary
            assert ei._INDEX_CACHE is None
        finally:
            for name, value in saved.items():
                setattr(ei, name, value)
    print("  ok  _save atomically replaces checkpoints and invalidates the runtime cache")


def test_get_index_materializes_arrays_once() -> None:
    saved = {"INDEX": ei.INDEX, "_INDEX_CACHE": ei._INDEX_CACHE}
    with tempfile.TemporaryDirectory() as tmp:
        ei.INDEX = Path(tmp) / "taste_index.npz"
        np.savez(
            ei.INDEX,
            embeddings=np.array([[1.0, 0.0], [0.0, 1.0]], dtype="float32"),
            iso=np.array([100, 200]),
            f_number=np.array([2.8, 4.0]),
            shutter_s=np.array([1 / 125, 1 / 60]),
            focal_mm=np.array([35.0, 50.0]),
            titles=np.array(["File:A.jpg", "File:B.jpg"]),
        )
        ei._INDEX_CACHE = None
        try:
            first = ei._get_index()
            ei.INDEX.unlink()
            second = ei._get_index()
            assert isinstance(first, dict), type(first)
            assert first is second
            assert first["embeddings"].shape == (2, 2)
            assert first["titles"].tolist() == ["File:A.jpg", "File:B.jpg"]
        finally:
            for name, value in saved.items():
                setattr(ei, name, value)
    print("  ok  _get_index materializes each NPZ member once for the warm path")


def _run_all() -> int:
    tests = [test_read_rows_concatenates_and_skips_missing,
             test_fetch_requires_a_validated_direct_wikimedia_upload_url,
             test_download_deadline_stops_after_a_31_second_socket_operation,
             test_download_deadline_caps_malformed_retry_after_and_40_second_oversleep,
             test_download_deadline_rebounds_socket_timeout_before_each_trickled_read,
             test_build_skips_untrusted_dataset_sources,
             test_save_is_atomic_and_invalidates_runtime_cache,
             test_get_index_materializes_arrays_once]
    failures = 0
    for test in tests:
        try:
            test()
        except AssertionError as error:
            failures += 1
            print(f"  FAIL {test.__name__}: {error}")
    print(f"\n{len(tests) - failures}/{len(tests)} embed_index multi-source tests passed")
    return failures


if __name__ == "__main__":
    print("embed_index multi-source tests:")
    raise SystemExit(_run_all())
