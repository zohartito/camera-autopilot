"""Camera-free bounded ARW isolation and adversarial fixtures. No photographs."""
from __future__ import annotations

import io
import os
import signal
import struct
import sys
import time
from pathlib import Path
from unittest.mock import Mock

import pytest
from PIL import Image

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))
import isolated_raw as raw


# The worker path exists only where a hard address-space ceiling can be enforced. On any other
# host `decode_arw_isolated` refuses with `unsupported_memory_limit` before a worker is spawned,
# which is the correct fail-closed behaviour and is asserted by its own test above. Tests that
# exercise what the worker returns therefore have to run on Linux, or assert the refusal.
# (Added 2026-09-20: these two failed on the owner's macOS machine and turned the project's own
# gate red, while hosted CI stayed green because it runs on Linux.)
LINUX_ONLY = pytest.mark.skipif(not sys.platform.startswith("linux"),
                                reason="the bounded worker path is Linux-only; macOS fails closed first")


def _ifd(entries: list[tuple[int, int, int, bytes]], next_ifd: int = 0) -> bytes:
    body = struct.pack("<H", len(entries))
    for tag, kind, count, slot in sorted(entries):
        body += struct.pack("<HHI", tag, kind, count) + slot
    return body + struct.pack("<I", next_ifd)


def _short(tag: int, value: int) -> tuple[int, int, int, bytes]:
    return tag, 3, 1, struct.pack("<HH", value, 0)


def _long(tag: int, value: int) -> tuple[int, int, int, bytes]:
    return tag, 4, 1, struct.pack("<I", value)


def synthetic_arw(*, width: int = 64, height: int = 48, make: str = "SONY",
                  model: str = "ILCE-7M2", compression: int = 32767, subifd=None,
                  loop: bool = False) -> bytes:
    make_b = make.encode("ascii") + b"\x00"
    model_b = model.encode("ascii") + b"\x00"
    strip = b"\x00" * 16
    extras = make_b + model_b + strip
    ifd0_count = 11 if subifd else 10
    extras_at = 8 + 2 + 12 * ifd0_count + 4
    make_at, model_at, strip_at = extras_at, extras_at + len(make_b), extras_at + len(make_b) + len(model_b)
    entries = [
        _long(256, width), _long(257, height), _short(258, 14), _short(259, compression),
        (271, 2, len(make_b), struct.pack("<I", make_at)),
        (272, 2, len(model_b), struct.pack("<I", model_at)),
        _long(273, strip_at), _short(277, 1), _long(278, height), _long(279, len(strip)),
    ]
    if subifd:
        sub_w, sub_h = subifd
        sub_at = extras_at + len(extras)
        entries.append(_long(330, sub_at))
        sub = _ifd([_long(256, sub_w), _long(257, sub_h), _short(259, 32767)])
        extras += sub
    next_ifd = 8 if loop else 0
    return b"II*\x00" + struct.pack("<I", 8) + _ifd(entries, next_ifd) + extras


def write_arw(tmp_path: Path, name: str = "scene.arw", **kwargs) -> Path:
    path = tmp_path / name
    path.write_bytes(synthetic_arw(**kwargs))
    return path


def test_truncated_arw_is_typed_error(tmp_path, monkeypatch):
    path = tmp_path / "trunc.arw"
    path.write_bytes(synthetic_arw()[:20])
    spawn = Mock()
    monkeypatch.setattr(raw, "_collect_worker", spawn)
    with pytest.raises(raw.IsolatedRawError) as error:
        raw.decode_arw_isolated(path)
    assert error.value.code == "truncated"
    spawn.assert_not_called()


def test_enormous_claimed_dimensions_rejected_before_spawn(tmp_path, monkeypatch):
    path = write_arw(tmp_path, width=100_000, height=100_000)
    spawn = Mock()
    monkeypatch.setattr(raw, "_collect_worker", spawn)
    with pytest.raises(raw.IsolatedRawError) as error:
        raw.decode_arw_isolated(path)
    assert error.value.code == "dimension_limit"
    spawn.assert_not_called()


def test_enormous_subifd_dimensions_rejected_before_spawn(tmp_path, monkeypatch):
    path = write_arw(tmp_path, subifd=(120_000, 80_000))
    spawn = Mock()
    monkeypatch.setattr(raw, "_collect_worker", spawn)
    with pytest.raises(raw.IsolatedRawError) as error:
        raw.decode_arw_isolated(path)
    assert error.value.code == "dimension_limit"
    spawn.assert_not_called()


@pytest.mark.parametrize("payload,suffix", [
    (b"not a tiff", ".arw"),
    (b"II*\x00\x08\x00\x00\x00" + b"\xff" * 8, ".arw"),
])
def test_corrupt_file_is_typed_error(tmp_path, monkeypatch, payload, suffix):
    path = tmp_path / f"bad{suffix}"
    path.write_bytes(payload)
    spawn = Mock()
    monkeypatch.setattr(raw, "_collect_worker", spawn)
    with pytest.raises(raw.IsolatedRawError) as error:
        raw.decode_arw_isolated(path)
    assert error.value.code in {"corrupt", "truncated", "not_raw"}
    spawn.assert_not_called()


def test_ifd_loop_is_corrupt_not_a_hang(tmp_path, monkeypatch):
    path = write_arw(tmp_path, loop=True)
    spawn = Mock()
    monkeypatch.setattr(raw, "_collect_worker", spawn)
    started = time.monotonic()
    with pytest.raises(raw.IsolatedRawError) as error:
        raw.decode_arw_isolated(path)
    assert error.value.code == "corrupt"
    assert time.monotonic() - started < 2
    spawn.assert_not_called()


def test_not_raw_jpeg_and_png_are_typed_errors(tmp_path, monkeypatch):
    spawn = Mock()
    monkeypatch.setattr(raw, "_collect_worker", spawn)
    jpeg = tmp_path / "photo.arw"
    Image.new("RGB", (8, 8), "red").save(jpeg, "JPEG")
    png = tmp_path / "photo2.arw"
    Image.new("RGB", (8, 8), "blue").save(png, "PNG")
    for path in (jpeg, png):
        with pytest.raises(raw.IsolatedRawError) as error:
            raw.decode_arw_isolated(path)
        assert error.value.code == "not_raw"
    spawn.assert_not_called()


def test_non_sony_tiff_is_not_raw(tmp_path, monkeypatch):
    path = write_arw(tmp_path, make="Canon", model="EOS", compression=1)
    spawn = Mock()
    monkeypatch.setattr(raw, "_collect_worker", spawn)
    with pytest.raises(raw.IsolatedRawError) as error:
        raw.decode_arw_isolated(path)
    assert error.value.code == "not_raw"
    spawn.assert_not_called()


def test_over_limit_decode_rejected_before_spawn(tmp_path, monkeypatch):
    path = write_arw(tmp_path, width=64, height=48)
    spawn = Mock()
    monkeypatch.setattr(raw, "_collect_worker", spawn)
    with pytest.raises(raw.IsolatedRawError) as error:
        raw.decode_arw_isolated(path, address_space_bytes=4096)
    assert error.value.code == "memory_limit"
    spawn.assert_not_called()


def test_oversized_file_rejected_before_spawn(tmp_path, monkeypatch):
    path = tmp_path / "huge.arw"
    path.write_bytes(synthetic_arw() + b"\x00" * 2000)
    spawn = Mock()
    monkeypatch.setattr(raw, "_collect_worker", spawn)
    with pytest.raises(raw.IsolatedRawError) as error:
        raw.decode_arw_isolated(path, max_source_bytes=1024)
    assert error.value.code == "byte_limit"
    spawn.assert_not_called()


def test_symlink_rejected_before_open(tmp_path, monkeypatch):
    target = write_arw(tmp_path, name="real.arw")
    link = tmp_path / "link.arw"
    link.symlink_to(target)
    spawn = Mock()
    monkeypatch.setattr(raw, "_collect_worker", spawn)
    with pytest.raises(raw.IsolatedRawError) as error:
        raw.decode_arw_isolated(link)
    assert error.value.code == "path_rejected"
    spawn.assert_not_called()


def test_child_timeout_kills_and_reaps(tmp_path):
    pidfile = tmp_path / "pid"
    command = [sys.executable, "-c",
               "import os,time,pathlib; pathlib.Path(" + repr(str(pidfile)) +
               ").write_text(str(os.getpid())); time.sleep(30)"]
    start = time.monotonic()
    with pytest.raises(raw.IsolatedRawError) as error:
        raw._collect_worker(command, timeout_seconds=0.3, max_output=100)
    assert error.value.code == "timeout"
    assert time.monotonic() - start < 2
    with pytest.raises(ProcessLookupError):
        os.kill(int(pidfile.read_text()), 0)


def test_child_crash_does_not_take_down_caller():
    with pytest.raises(raw.IsolatedRawError) as error:
        raw._collect_worker([sys.executable, "-c", "raise SystemExit(42)"], timeout_seconds=2, max_output=1024)
    assert error.value.code == "worker_failed"


def test_os_memory_ceiling_typed_error_and_reap(tmp_path):
    pidfile = tmp_path / "pid"
    script = (
        "import os, pathlib, resource\n"
        f"pathlib.Path({str(pidfile)!r}).write_text(str(os.getpid()))\n"
        "resource.setrlimit(resource.RLIMIT_AS, (256 * 1024 * 1024, 256 * 1024 * 1024))\n"
        "blob = []\n"
        "while True:\n"
        "    blob.append(bytearray(16 * 1024 * 1024))\n"
    )
    start = time.monotonic()
    with pytest.raises(raw.IsolatedRawError) as error:
        raw._collect_worker([sys.executable, "-c", script], timeout_seconds=5, max_output=1024)
    assert error.value.code in {"memory_limit", "worker_failed"}
    assert time.monotonic() - start < 5
    with pytest.raises(ProcessLookupError):
        os.kill(int(pidfile.read_text()), 0)


@LINUX_ONLY
def test_synthetic_sony_tiff_does_not_crash_or_hang(tmp_path):
    path = write_arw(tmp_path)
    start = time.monotonic()
    with pytest.raises(raw.IsolatedRawError) as error:
        raw.decode_arw_isolated(path, timeout_seconds=8)
    assert error.value.code in {"decoder_unavailable", "corrupt"}
    assert time.monotonic() - start < 8
    assert path.exists()


def test_native_unpack_checks_sizes_before_unpack(tmp_path, monkeypatch):
    path = write_arw(tmp_path)
    decoder = Mock()
    decoder.sizes.raw_width = 100000
    decoder.sizes.raw_height = 100000
    decoder.sizes.width = 100000
    decoder.sizes.height = 100000
    ctx = Mock()
    ctx.__enter__ = Mock(return_value=decoder)
    ctx.__exit__ = Mock(return_value=False)
    fake = Mock()
    fake.RawPy.return_value = ctx
    monkeypatch.setitem(sys.modules, "rawpy", fake)
    with pytest.raises(raw.IsolatedRawError) as error:
        raw._native_unpack(str(path), max_pixels=raw.MAX_PIXELS,
                           max_source_bytes=raw.MAX_SOURCE_BYTES,
                           address_space_bytes=raw.ADDRESS_SPACE_BYTES)
    assert error.value.code == "dimension_limit"
    decoder.unpack.assert_not_called()


def test_unavailable_address_limit_fails_before_decode(monkeypatch):
    native = Mock()
    monkeypatch.setattr(raw, "_native_unpack", native)

    def denied(*args, **kwargs):
        raise ValueError("current limit exceeds maximum limit")
    monkeypatch.setattr("resource.setrlimit", denied)
    with pytest.raises(raw.IsolatedRawError) as error:
        raw._set_worker_limits(raw.ADDRESS_SPACE_BYTES, raw.MAX_SOURCE_BYTES)
    assert error.value.code == "unsupported_memory_limit"
    native.assert_not_called()


def test_non_linux_fails_closed_after_header_gate(tmp_path, monkeypatch):
    path = write_arw(tmp_path)
    spawn = Mock()
    monkeypatch.setattr(raw, "_collect_worker", spawn)
    monkeypatch.setattr(raw.sys, "platform", "darwin")
    with pytest.raises(raw.IsolatedRawError) as error:
        raw.decode_arw_isolated(path)
    assert error.value.code == "unsupported_memory_limit"
    spawn.assert_not_called()


@LINUX_ONLY
def test_worker_error_json_does_not_kill_caller(monkeypatch, tmp_path):
    path = write_arw(tmp_path)
    monkeypatch.setattr(raw, "_collect_worker",
                        lambda *a, **k: b'{"ok":false,"code":"corrupt","error":"LibRaw rejected the ARW"}\n')
    with pytest.raises(raw.IsolatedRawError) as error:
        raw.decode_arw_isolated(path)
    assert error.value.code == "corrupt"


def test_header_accepts_bounded_synthetic_sony_tiff(tmp_path):
    path = write_arw(tmp_path, width=64, height=48)
    header = raw.inspect_arw_header(path)
    assert header["width"] == 64 and header["height"] == 48
    assert header["source_bytes"] == path.stat().st_size
    assert header["make"] == "SONY" and header["model"] == "ILCE-7M2"


@LINUX_ONLY
def test_linear_render_records_camera_matrix_not_a_guess(tmp_path, monkeypatch):
    import hashlib
    import json
    import numpy as np
    from raw_colour import write_linear_tiff
    path = write_arw(tmp_path)
    out = tmp_path / "linear.tiff"
    pixels = np.full((4, 6, 3), 1000, dtype=np.uint16)
    payload = {
        "ok": True, "width": 6, "height": 4, "dtype": "uint16",
        "pixel_sha256": hashlib.sha256(np.asarray(pixels, dtype="<u2").tobytes()).hexdigest(),
        "camera_whitebalance": [2.0, 1.0, 1.5],
        "rgb_xyz_matrix": [[0.5, 0.3, 0.2], [0.2, 0.7, 0.1], [0.1, 0.2, 0.7]],
        "colour_source": "camera", "unpacked": True,
        "decoder": "LibRaw via rawpy; camera WB and RGB->XYZ",
    }

    def fake_collect(command, **kwargs):
        assert "--worker-linear" in command
        write_linear_tiff(out, pixels)
        return json.dumps(payload, separators=(",", ":")).encode() + b"\n"

    monkeypatch.setattr(raw, "_collect_worker", fake_collect)
    result = raw.render_arw_linear_isolated(path, out)
    assert result.colour_source == "camera"
    assert result.camera_whitebalance == (2.0, 1.0, 1.5)
    assert result.rgb_xyz_matrix[1][1] == 0.7
    assert result.width == 6 and result.height == 4
    assert out.is_file()


def test_linear_render_missing_camera_colour_is_not_replaced_with_a_guess(tmp_path, monkeypatch):
    import numpy as np
    path = write_arw(tmp_path)
    decoder = Mock()
    decoder.sizes.raw_width = 8
    decoder.sizes.raw_height = 8
    decoder.sizes.width = 8
    decoder.sizes.height = 8
    decoder.camera_whitebalance = [0, 0, 0, 0]
    decoder.rgb_xyz_matrix = [[0, 0, 0], [0, 0, 0], [0, 0, 0]]
    decoder.postprocess.return_value = np.zeros((8, 8, 3), np.uint16)
    ctx = Mock()
    ctx.__enter__ = Mock(return_value=decoder)
    ctx.__exit__ = Mock(return_value=False)
    fake = Mock()
    fake.RawPy.return_value = ctx
    fake.ColorSpace.raw = 0
    monkeypatch.setitem(sys.modules, "rawpy", fake)
    with pytest.raises(raw.IsolatedRawError) as error:
        raw._native_linear(str(path), str(tmp_path / "out.tiff"), max_pixels=raw.MAX_PIXELS,
                           max_source_bytes=raw.MAX_SOURCE_BYTES,
                           address_space_bytes=raw.ADDRESS_SPACE_BYTES)
    assert error.value.code == "colour_missing"
    decoder.postprocess.assert_not_called()
    assert not (tmp_path / "out.tiff").exists()


def test_linear_render_non_linux_fails_closed(tmp_path, monkeypatch):
    path = write_arw(tmp_path)
    spawn = Mock()
    monkeypatch.setattr(raw, "_collect_worker", spawn)
    monkeypatch.setattr(raw.sys, "platform", "darwin")
    with pytest.raises(raw.IsolatedRawError) as error:
        raw.render_arw_linear_isolated(path, tmp_path / "linear.tiff")
    assert error.value.code == "unsupported_memory_limit"
    spawn.assert_not_called()
